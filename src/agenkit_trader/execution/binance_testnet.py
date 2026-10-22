"""Binance USD-M Futures TESTNET venue (paper trading).

* Base URL defaults to ``https://testnet.binancefuture.com``. Mainnet is not
  supported here (live mode is a separate, locked adapter).
* API key and secret are read ONLY from environment variables (names given by
  ``key_env`` / ``secret_env``). They are held in a redacting holder, are
  never logged, never put in exception text and never shown by ``repr``.
  Unsigned (public) endpoints work without them.
* Signed requests: HMAC-SHA256 over the exact query string, with
  ``timestamp = local clock + server offset`` and ``recvWindow``. The offset
  is measured by ``sync_time`` (automatically before the first signed
  request); ``|offset| > MAX_CLOCK_SKEW_MS`` raises ``ClockSkewError``.
* Protective stops use the Algo Service (``POST /fapi/v1/algoOrder``,
  ``algoType=CONDITIONAL``, ``type=STOP_MARKET``, ``reduceOnly``): since
  2025-12-09 ``POST /fapi/v1/order`` rejects conditional order types with
  -4120 STOP_ORDER_SWITCH_ALGO (Binance derivatives change log, 2025-11-06).
* Every failure raises ``VenueError`` (``BinanceAPIError`` for an HTTP error
  answer); nothing is retried or swallowed here, so the caller can count it
  with ``KillSwitch.record_api_error``.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import httpx
import pandas as pd

from agenkit_trader.execution.venue import (
    MAX_CLOCK_SKEW_MS,
    AccountState,
    ClockSkewError,
    Fill,
    OpenOrder,
    Order,
    Position,
    SymbolRules,
    VenueError,
)

DEFAULT_BASE_URL = "https://testnet.binancefuture.com"
_RULES_TTL_MS = 3_600_000

__all__ = [
    "DEFAULT_BASE_URL",
    "BinanceAPIError",
    "BinanceTestnetVenue",
    "ClockSkewError",
    "CredentialsError",
]


class CredentialsError(VenueError):
    """A signed request was attempted without API credentials."""


class BinanceAPIError(VenueError):
    def __init__(
        self, method: str, path: str, status: int, code: object, msg: str
    ) -> None:
        self.status = status
        self.code = code
        super().__init__(f"{method} {path}: HTTP {status} code={code} msg={msg}")


class _Credentials:
    """Holds the key pair; its repr never reveals them."""

    __slots__ = ("_key", "_secret")

    def __init__(self, key: str, secret: str) -> None:
        self._key = key
        self._secret = secret

    def sign(self, payload: str) -> str:
        return hmac.new(
            self._secret.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()

    def headers(self) -> dict[str, str]:
        return {"X-MBX-APIKEY": self._key}

    def __repr__(self) -> str:
        return "<credentials redacted>"

    __str__ = __repr__


def _side(side: str) -> str:
    if side not in ("buy", "sell"):
        raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")
    return side.upper()


def _decimals(step: float) -> int:
    exp = Decimal(str(step)).normalize().as_tuple().exponent
    return max(0, -int(exp))


def _fmt(x: float, step: float) -> str:
    return f"{x:.{_decimals(step)}f}"


def _ts(ms: object) -> pd.Timestamp:
    return pd.Timestamp(int(str(ms)), unit="ms", tz="UTC")


class BinanceTestnetVenue:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        key_env: str = "BINANCE_TESTNET_KEY",
        secret_env: str = "BINANCE_TESTNET_SECRET",
        *,
        recv_window_ms: int = 5000,
        timeout_s: float = 10.0,
        transport: httpx.BaseTransport | None = None,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._key_env = key_env
        self._secret_env = secret_env
        key, secret = os.environ.get(key_env, ""), os.environ.get(secret_env, "")
        self._creds = _Credentials(key, secret) if key and secret else None
        self._recv_window_ms = int(recv_window_ms)
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._client = httpx.Client(
            base_url=self.base_url, timeout=timeout_s, transport=transport
        )
        self._offset_ms: int | None = None
        self._rules: dict[str, SymbolRules] = {}
        self._rules_at: int | None = None

    def __repr__(self) -> str:
        creds = "set" if self._creds else "missing"
        return f"BinanceTestnetVenue(base_url={self.base_url!r}, credentials={creds})"

    __str__ = __repr__

    @property
    def has_credentials(self) -> bool:
        return self._creds is not None

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------ transport

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        signed: bool = False,
    ) -> Any:
        params = dict(params or {})
        headers: dict[str, str] = {}
        if signed:
            creds = self._creds
            if creds is None:
                raise CredentialsError(
                    f"signed request {method} {path} needs API credentials in the "
                    f"environment variables {self._key_env} and {self._secret_env}"
                )
            if self._offset_ms is None:
                self.sync_time()
            assert self._offset_ms is not None
            params["recvWindow"] = self._recv_window_ms
            params["timestamp"] = self._clock_ms() + self._offset_ms
            qs = urlencode(params)
            qs = f"{qs}&signature={creds.sign(qs)}"
            headers = creds.headers()
        else:
            qs = urlencode(params)
        url = f"{path}?{qs}" if qs else path
        try:
            r = self._client.request(method, url, headers=headers)
        except httpx.HTTPError as exc:
            # Exception text is ours: no URL (signature) and no headers (key).
            raise VenueError(
                f"{method} {path}: transport error {type(exc).__name__}"
            ) from None
        if r.status_code >= 400:
            code: object = None
            msg = r.text[:200]
            try:
                body = r.json()
                if isinstance(body, dict):
                    code, msg = body.get("code"), str(body.get("msg", ""))[:200]
            except ValueError:
                pass
            raise BinanceAPIError(method, path, r.status_code, code, msg)
        try:
            return r.json()
        except ValueError:
            raise VenueError(f"{method} {path}: response is not JSON") from None

    # ------------------------------------------------------------ time

    def sync_time(self) -> int:
        """Measure ``server - local`` in ms (midpoint of the round trip)."""
        t0 = self._clock_ms()
        server = int(self._request("GET", "/fapi/v1/time")["serverTime"])
        t1 = self._clock_ms()
        offset = int(round(server - (t0 + t1) / 2))
        if abs(offset) > MAX_CLOCK_SKEW_MS:
            self._offset_ms = None
            raise ClockSkewError(
                f"clock skew {offset} ms vs venue exceeds {MAX_CLOCK_SKEW_MS} ms; "
                "fix the system clock (NTP)"
            )
        self._offset_ms = offset
        return offset

    def server_time(self) -> datetime:
        ms = int(self._request("GET", "/fapi/v1/time")["serverTime"])
        return datetime.fromtimestamp(ms / 1000, UTC)

    # ------------------------------------------------------------ market data

    def symbol_rules(self, symbol: str) -> SymbolRules:
        now = self._clock_ms()
        if self._rules_at is None or now - self._rules_at > _RULES_TTL_MS:
            info = self._request("GET", "/fapi/v1/exchangeInfo")
            rules: dict[str, SymbolRules] = {}
            for s in info.get("symbols", []):
                f = {x.get("filterType"): x for x in s.get("filters", [])}
                try:
                    lot_f = f.get("MARKET_LOT_SIZE") or f["LOT_SIZE"]
                    lot = float(lot_f["stepSize"])
                    if not lot > 0:
                        lot = float(f["LOT_SIZE"]["stepSize"])
                    rules[s["symbol"]] = SymbolRules(
                        tick=float(f["PRICE_FILTER"]["tickSize"]),
                        lot=lot,
                        min_notional=float(
                            f.get("MIN_NOTIONAL", {}).get("notional", 0)
                        ),
                    )
                except (KeyError, TypeError, ValueError):
                    continue  # unusable entry: the symbol stays unknown
            self._rules, self._rules_at = rules, now
        if symbol not in self._rules:
            raise VenueError(f"no trading rules for {symbol}")
        return self._rules[symbol]

    def get_bars(self, symbol: str, interval: str, limit: int) -> pd.DataFrame:
        """Klines indexed by UTC open time. The last row may be the FORMING
        bar; the runtime drops it."""
        rows = self._request(
            "GET",
            "/fapi/v1/klines",
            {"symbol": symbol, "interval": interval, "limit": int(limit)},
        )
        idx = pd.DatetimeIndex([_ts(r[0]) for r in rows], name="open_time")
        cols = ["open", "high", "low", "close", "volume"]
        data = {c: [float(r[i + 1]) for r in rows] for i, c in enumerate(cols)}
        return pd.DataFrame(data, index=idx, columns=cols)

    def get_funding(self, symbol: str, limit: int) -> pd.Series:
        rows = self._request(
            "GET", "/fapi/v1/fundingRate", {"symbol": symbol, "limit": int(limit)}
        )
        idx = pd.DatetimeIndex([_ts(r["fundingTime"]) for r in rows])
        vals = [float(r["fundingRate"]) for r in rows]
        return pd.Series(vals, index=idx, name="funding", dtype="float64")

    def get_mark_price(self, symbol: str) -> float:
        d = self._request("GET", "/fapi/v1/premiumIndex", {"symbol": symbol})
        return float(d["markPrice"])

    # ------------------------------------------------------------ account

    def _position_rows(self) -> list[dict[str, Any]]:
        rows = self._request("GET", "/fapi/v2/positionRisk", signed=True)
        return [r for r in rows if float(r["positionAmt"]) != 0.0]

    def get_account(self) -> AccountState:
        """Equity is ``totalMarginBalance``. The venue cannot know the day-start
        or peak equity of the paper account; the runtime tracks and replaces
        those (and ``last_bar_time``) before any risk check."""
        acct = self._request("GET", "/fapi/v2/account", signed=True)
        equity = float(acct["totalMarginBalance"])
        positions = {
            r["symbol"]: Position(
                r["symbol"], float(r["positionAmt"]), float(r["entryPrice"])
            )
            for r in self._position_rows()
        }
        now = datetime.fromtimestamp(self._clock_ms() / 1000, UTC)
        return AccountState(
            equity=equity,
            positions=positions,
            day_start_equity=equity,
            peak_equity=equity,
            last_bar_time=now,
        )

    def get_open_orders(self) -> list[OpenOrder]:
        out: list[OpenOrder] = []
        for o in self._request("GET", "/fapi/v1/openOrders", signed=True):
            sp = float(o.get("stopPrice") or 0)
            out.append(
                OpenOrder(
                    symbol=o["symbol"],
                    side=str(o["side"]).lower(),
                    qty=float(o["origQty"]),
                    kind=str(o["type"]),
                    stop_price=sp if sp > 0 else None,
                    client_id=str(o.get("clientOrderId", "")),
                    reduce_only=bool(o.get("reduceOnly", False)),
                )
            )
        for o in self._request("GET", "/fapi/v1/openAlgoOrders", signed=True):
            tp = float(o.get("triggerPrice") or 0)
            out.append(
                OpenOrder(
                    symbol=o["symbol"],
                    side=str(o["side"]).lower(),
                    qty=float(o.get("quantity") or 0),
                    kind=str(o.get("orderType", "")),
                    stop_price=tp if tp > 0 else None,
                    client_id=str(o.get("clientAlgoId", "")),
                    reduce_only=bool(o.get("reduceOnly", False)),
                )
            )
        return out

    # ------------------------------------------------------------ orders

    def _market(
        self, symbol: str, side: str, qty: str, client_id: str, reduce_only: bool
    ) -> Fill:
        params: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": qty,
            "newOrderRespType": "RESULT",
        }
        if client_id:
            params["newClientOrderId"] = client_id
        if reduce_only:
            params["reduceOnly"] = "true"
        r = self._request("POST", "/fapi/v1/order", params, signed=True)
        executed = float(r.get("executedQty") or 0)
        if not executed > 0:
            raise VenueError(
                f"market order {client_id or '?'} on {symbol} not filled: "
                f"status {r.get('status')}"
            )
        t = r.get("updateTime")
        when = (
            _ts(t).to_pydatetime()
            if t
            else datetime.fromtimestamp(self._clock_ms() / 1000, UTC)
        )
        return Fill(
            symbol=symbol,
            side=side.lower(),
            qty=executed,
            price=float(r.get("avgPrice") or 0),
            fee=0.0,  # not in the RESULT response; see account trades
            time=when,
        )

    def place_order(self, order: Order) -> Fill:
        side = _side(order.side)
        if not order.qty > 0:
            raise ValueError("qty must be > 0")
        rules = self.symbol_rules(order.symbol)
        return self._market(
            order.symbol,
            side,
            _fmt(order.qty, rules.lot),
            order.client_id,
            order.reduce_only,
        )

    def place_stop(self, order: Order) -> str:
        side = _side(order.side)
        if not order.reduce_only:
            raise ValueError("protective stops must be reduce-only")
        if order.stop_price is None or not order.stop_price > 0:
            raise ValueError("protective stop needs a positive stop_price")
        rules = self.symbol_rules(order.symbol)
        params: dict[str, Any] = {
            "algoType": "CONDITIONAL",
            "symbol": order.symbol,
            "side": side,
            "type": "STOP_MARKET",
            "quantity": _fmt(order.qty, rules.lot),
            "triggerPrice": _fmt(order.stop_price, rules.tick),
            "reduceOnly": "true",
            "workingType": "MARK_PRICE",
        }
        if order.client_id:
            params["clientAlgoId"] = order.client_id
        r = self._request("POST", "/fapi/v1/algoOrder", params, signed=True)
        return str(r.get("algoId", ""))

    def cancel_open_orders(self, symbol: str) -> None:
        errors: list[str] = []
        for path in ("/fapi/v1/allOpenOrders", "/fapi/v1/algoOpenOrders"):
            try:
                self._request("DELETE", path, {"symbol": symbol}, signed=True)
            except VenueError as exc:
                errors.append(str(exc))
        if errors:
            raise VenueError(f"cancel open orders {symbol}: " + "; ".join(errors))

    def flatten_all(self) -> None:
        """Kill-switch path (never through the RiskGate): cancel open orders,
        then close every position with a reduce-only market order. Attempts
        everything, then raises if anything failed."""
        rows = self._position_rows()  # without positions there is no flatten
        errors: list[str] = []
        symbols = {r["symbol"] for r in rows}
        try:
            symbols |= {o.symbol for o in self.get_open_orders()}
        except VenueError as exc:
            errors.append(f"open orders: {exc}")
        for sym in sorted(symbols):
            try:
                self.cancel_open_orders(sym)
            except VenueError as exc:
                errors.append(str(exc))
        for r in rows:
            amt = str(r["positionAmt"]).strip()
            side = "SELL" if float(amt) > 0 else "BUY"
            try:
                self._market(r["symbol"], side, amt.lstrip("-+"), "", True)
            except VenueError as exc:
                errors.append(f"close {r['symbol']}: {exc}")
        if errors:
            raise VenueError("flatten_all incomplete: " + " | ".join(errors))
