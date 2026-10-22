"""Paper-trading runtime: closed bars -> deployed strategies -> gate -> venue.

Invariants (each is tested in ``tests/runtime/test_loop.py``):

* Every order, including the exchange-side protective stop, passes
  ``RiskGate.check`` and is submitted with exactly the quantity the gate
  allowed. Only the kill switch (``venue.flatten_all``) bypasses the gate.
* Nothing is placed while the kill switch is halted. The ``AccountState``
  handed to the gate has ``halted = kill.halted or venue_state.halted``.
* Only CLOSED bars are used (a bar whose ``open + interval > now`` is the
  forming bar and is dropped); one decision per bar per strategy, recorded in
  the state file before acting, so processing a bar twice (also across a
  restart) places nothing new. Client order ids are derived from
  strategy/symbol/bar time/kind.
* Entries happen only on a signal transition at the latest closed bar
  (previous bar's target != this bar's target) and only when the current
  regime (``agenkit_trader.regime.label_regimes`` with the gate's
  ``regime_window``) is in the deployment's ``allowed_regimes``. Exits (target
  differs from the held side) are always allowed. Entry-on-transition means a
  restart never enters mid-signal and a disaster-stop exit is not re-entered
  until the strategy signals afresh.
* Protective stop: ``close -/+ stop_atr_mult * ATR(atr_window)`` (rounded to
  the tick toward the entry, so the loss at the stop never exceeds the
  sized budget); size via ``size_position`` (floored to lot); after the fill
  a reduce-only STOP_MARKET is placed. If that stop is rejected or fails, the
  kill switch flattens (an unprotected position is not kept).
* Gate valuation: ``Position.entry_price`` is used by the gate only to value
  OTHER symbols' notional for the leverage cap (and checked > 0). The runtime
  therefore passes positions re-valued at the venue's current MARK price, so
  the leverage check uses current, not historical, exposure.
* Paper equity = ``risk.start_equity + (venue equity - venue equity at first
  start)``: the testnet wallet size is irrelevant; the configured paper
  account is what is risked. Day-start and peak paper equity are tracked in
  the state file (UTC days), so drawdown survives restarts. Daily loss or
  drawdown at/over the limit trips the kill switch.
* Data staleness: if the newest closed bar closed more than
  ``interval + stale_after_s`` ago, the feed is stale and the kill switch
  flattens. Clock skew > MAX_CLOCK_SKEW_MS at start or in any cycle trips the
  kill switch. Every venue exception is counted with
  ``KillSwitch.record_api_error`` (5 in the window halt); a gate rejection is
  logged and the loop continues; an unexpected internal exception trips the
  kill switch.
* Restart reconciliation (``start``): positions and open orders are read
  BEFORE trading. A position/order on a symbol no deployed strategy owns is
  logged and, by default, startup is refused (the user decides); with
  ``flatten_unowned_on_start: true`` the kill switch flattens instead. An
  owned position is adopted (logged) and gets a protective stop if it has
  none. One deployed strategy per symbol (one-way mode nets positions).
* A halt is persisted in the state file; a new process refuses to start until
  the user runs ``agenkit reset-halt``.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

import numpy as np
import pandas as pd

from agenkit_trader.config import RuntimeConfig
from agenkit_trader.data.align import align_asof
from agenkit_trader.data.archive import (
    DataGapError,
    check_continuity,
    interval_to_timedelta,
)
from agenkit_trader.execution.venue import (
    MAX_CLOCK_SKEW_MS,
    AccountState,
    ClockSkewError,
    Fill,
    Order,
    Position,
    SymbolRules,
    Venue,
)
from agenkit_trader.killswitch import KillSwitch
from agenkit_trader.log import DecisionLog
from agenkit_trader.regime import REGIMES, label_regimes
from agenkit_trader.risk.gate import RiskGate
from agenkit_trader.risk.sizing import floor_to_lot, size_position
from agenkit_trader.strategies.base import Strategy
from agenkit_trader.strategies.funding_reversion import FUNDING_AVAILABLE_LAG

FUNDING_LIMIT = 1000  # Binance fundingRate max per request
SUPPORTED_AUX = ("funding",)
_T = TypeVar("_T")


class DeployedError(Exception):
    """deployed.json is missing, unreadable, malformed or empty."""


class StartupRefused(Exception):
    """The runtime will not trade; the message tells the user why."""


class _VenueFailed(Exception):
    """A venue call failed; it has been logged and counted."""


@dataclass(frozen=True)
class Deployment:
    name: str
    symbol: str
    interval: str
    params: dict[str, Any]
    allowed_regimes: list[str]

    @property
    def key(self) -> str:
        return f"{self.name}|{self.symbol}|{self.interval}"


_DEPLOYED_KEYS = {"name", "symbol", "interval", "params", "allowed_regimes"}


def load_deployed(path: Path) -> list[Deployment]:
    """Read the validation gate's ``deployed.json`` (a JSON list of
    ``{name, symbol, interval, params, allowed_regimes}``). Raises
    ``DeployedError`` if it is missing, unreadable, malformed or EMPTY."""
    path = Path(path)
    if not path.exists():
        raise DeployedError(
            f"{path} not found: no strategy has been signed off by the "
            "validation gate (run `agenkit research`)"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise DeployedError(f"{path} is unreadable: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise DeployedError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise DeployedError(f"{path}: expected a JSON list of deployments")
    if not data:
        raise DeployedError(
            f"{path} is empty: no strategy passed the validation gate, so there "
            "is nothing to paper trade"
        )
    out: list[Deployment] = []
    for i, e in enumerate(data):
        if not isinstance(e, dict) or set(e) != _DEPLOYED_KEYS:
            raise DeployedError(
                f"{path}[{i}]: expected exactly the keys {sorted(_DEPLOYED_KEYS)}"
            )
        ok = (
            all(isinstance(e[k], str) and e[k] for k in ("name", "symbol", "interval"))
            and isinstance(e["params"], dict)
            and isinstance(e["allowed_regimes"], list)
            and all(isinstance(r, str) for r in e["allowed_regimes"])
        )
        if not ok:
            raise DeployedError(f"{path}[{i}]: wrong field types")
        out.append(
            Deployment(
                e["name"],
                e["symbol"],
                e["interval"],
                dict(e["params"]),
                list(e["allowed_regimes"]),
            )
        )
    return out


def validate_deployments(
    deployed: Iterable[Deployment], strategies: Mapping[str, Strategy]
) -> None:
    """Refuse unless every deployment names a known strategy with params from
    its grid, a valid interval and known regimes, one per symbol."""
    seen: set[str] = set()
    for d in deployed:
        s = strategies.get(d.name)
        if s is None:
            raise StartupRefused(f"unknown strategy {d.name!r} in deployed.json")
        grid = s.param_grid
        if set(d.params) != set(grid) or any(d.params[k] not in grid[k] for k in grid):
            raise StartupRefused(
                f"{d.name}: params {d.params} are not a point of its parameter "
                f"grid {grid}"
            )
        try:
            interval_to_timedelta(d.interval)
        except ValueError as exc:
            raise StartupRefused(f"{d.name}: bad interval {d.interval!r}") from exc
        bad = [r for r in d.allowed_regimes if r not in REGIMES]
        if bad:
            raise StartupRefused(f"{d.name}: unknown regime(s) {bad}")
        unknown_aux = [
            a for a in getattr(s, "aux_required", ()) if a not in SUPPORTED_AUX
        ]
        if unknown_aux:
            raise StartupRefused(f"{d.name}: unsupported aux {unknown_aux}")
        if d.symbol in seen:
            raise StartupRefused(
                f"{d.symbol}: one deployed strategy per symbol (positions net "
                "per symbol on the venue, so ownership would be ambiguous)"
            )
        seen.add(d.symbol)


def client_order_id(name: str, symbol: str, bar_open: pd.Timestamp, kind: str) -> str:
    """Deterministic, Binance-valid (<= 36 chars of [A-Za-z0-9]) client id."""
    raw = f"{name}|{symbol}|{pd.Timestamp(bar_open).isoformat()}|{kind}"
    return "ak" + hashlib.sha256(raw.encode()).hexdigest()[:30]


def check_clock_skew(venue: Venue, now: datetime) -> None:
    """Raise ``ClockSkewError`` if venue time and ``now`` differ by more than
    MAX_CLOCK_SKEW_MS."""
    skew_ms = (venue.server_time() - now).total_seconds() * 1000.0
    if not abs(skew_ms) <= MAX_CLOCK_SKEW_MS:
        raise ClockSkewError(
            f"clock skew {skew_ms:.0f} ms vs venue exceeds {MAX_CLOCK_SKEW_MS} ms"
        )


def closed_bars(bars: pd.DataFrame, step: pd.Timedelta, now: datetime) -> pd.DataFrame:
    """Drop the forming bar (and anything later): keep bars with
    ``open + step <= now``."""
    if len(bars) == 0:
        return bars
    keep = (bars.index + step) <= pd.Timestamp(now)
    return bars.loc[keep]


def atr(bars: pd.DataFrame, window: int) -> float:
    """Simple-mean Average True Range over ``window`` closed bars."""
    h, lo, c = (bars[k].astype("float64") for k in ("high", "low", "close"))
    pc = c.shift()
    tr = pd.concat([h - lo, (h - pc).abs(), (lo - pc).abs()], axis=1).max(axis=1)
    return float(tr.rolling(window).mean().iloc[-1])


def _sign(x: object) -> float:
    try:
        v = float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(v) or v == 0:
        return 0.0
    return 1.0 if v > 0 else -1.0


def _decimals(step: float) -> int:
    return max(0, -int(f"{step:e}".split("e")[1])) + 2


def protective_stop(close: float, side: float, dist: float, tick: float) -> float:
    """``close - side*dist`` rounded to the tick TOWARD the entry."""
    raw = close - side * dist
    n = raw / tick
    k = math.ceil(n - 1e-9) if side > 0 else math.floor(n + 1e-9)
    return round(k * tick, _decimals(tick))


@dataclass
class _PaperState:
    baseline_equity: float | None = None  # venue equity at first start
    peak_equity: float | None = None  # paper
    day: str | None = None  # UTC date of day_start_equity
    day_start_equity: float | None = None  # paper
    last_bar: dict[str, str] = field(default_factory=dict)
    halted: str | None = None

    @classmethod
    def load(cls, path: Path | None) -> _PaperState:
        if path is None or not Path(path).exists():
            return cls()
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            return cls(**data)
        except (OSError, ValueError, TypeError) as exc:
            raise StartupRefused(
                f"runtime state file {path} is unreadable ({exc}); refusing to "
                "start rather than lose drawdown/halt state"
            ) from exc

    def save(self, path: Path | None) -> None:
        if path is None:
            return
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(asdict(self), indent=2, sort_keys=True) + "\n"
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


def reset_halt(state_path: Path, log: DecisionLog) -> bool:
    """Clear a persisted halt (the CLI asks for the confirmation phrase)."""
    st = _PaperState.load(state_path)
    was = st.halted
    st.halted = None
    st.save(state_path)
    log.write("runtime", {"state": str(state_path)}, {"was": was}, "halt_reset")
    return was is not None


class Runtime:
    def __init__(
        self,
        venue: Venue,
        strategies: Iterable[Strategy] | Mapping[str, Strategy],
        deployed: list[Deployment],
        gate: RiskGate,
        kill: KillSwitch,
        log: DecisionLog,
        clock: Callable[[], datetime],
        *,
        runtime_cfg: RuntimeConfig,
        regime_window: int,
        state_path: Path | None = None,
    ) -> None:
        if isinstance(strategies, Mapping):
            self.strategies = dict(strategies)
        else:
            self.strategies = {s.name: s for s in strategies}
        self.venue = venue
        self.deployed = list(deployed)
        self.gate = gate
        self.kill = kill
        self.log = log
        self.clock = clock
        self.cfg = runtime_cfg
        self.risk = gate.cfg
        self.regime_window = regime_window
        self.state_path = state_path
        self._st = _PaperState.load(state_path)
        self._started = False

    # ------------------------------------------------------------ helpers

    def _call(self, fn: Callable[..., _T], *args: Any) -> _T:
        """Call the venue; on any exception log it, count it as an API error
        (or trip the kill switch on clock skew) and raise ``_VenueFailed``."""
        try:
            return fn(*args)
        except Exception as exc:
            name = getattr(fn, "__name__", "venue_call")
            self.log.write(
                "venue_error",
                {"call": name, "args": [str(a) for a in args]},
                {"error": repr(exc)},
                "error",
            )
            if isinstance(exc, ClockSkewError):
                self.kill.trigger("clock_skew")
            else:
                self.kill.record_api_error(self.clock())
            raise _VenueFailed(name) from exc

    def _save(self) -> None:
        if self.kill.halted and self._st.halted is None:
            self._st.halted = self.clock().isoformat()
        self._st.save(self.state_path)

    def _paper_equity(self, venue_equity: float, now: datetime) -> float:
        st = self._st
        if st.baseline_equity is None:
            st.baseline_equity = venue_equity
        eq = self.risk.start_equity + (venue_equity - st.baseline_equity)
        day = pd.Timestamp(now).tz_convert("UTC").date().isoformat()
        if st.day != day or st.day_start_equity is None:
            st.day, st.day_start_equity = day, eq
        st.peak_equity = eq if st.peak_equity is None else max(st.peak_equity, eq)
        self._save()
        return eq

    def build_state(self, acct: AccountState, last_bar_time: datetime) -> AccountState:
        """The gate's view: paper equity, positions valued at MARK, halted from
        the kill switch OR the venue state."""
        eq = self._paper_equity(acct.equity, self.clock())
        positions = {
            s: Position(s, p.qty, self._call(self.venue.get_mark_price, s))
            for s, p in acct.positions.items()
        }
        assert self._st.day_start_equity is not None
        assert self._st.peak_equity is not None
        return AccountState(
            equity=eq,
            positions=positions,
            day_start_equity=self._st.day_start_equity,
            peak_equity=self._st.peak_equity,
            last_bar_time=last_bar_time,
            halted=bool(self.kill.halted or acct.halted),
        )

    def _order_inputs(self, order: Order, state: AccountState, kind: str) -> Any:
        return {"kind": kind, "order": asdict(order), "state": asdict(state)}

    def submit(self, order: Order, state: AccountState, kind: str) -> Fill | None:
        """Gate-check then place a market order. None if refused/rejected/failed."""
        now = self.clock()
        inputs = self._order_inputs(order, state, kind)
        if self.kill.halted:
            self.log.write("order", inputs, {"reason": "kill_switch_halted"}, "refused")
            return None
        decision = self.gate.check(order, state, now)
        if not decision.allowed:
            self.log.write("order", inputs, asdict(decision), "rejected")
            return None
        if decision.max_qty != order.qty:  # never submit what was not approved
            self.log.write("order", inputs, asdict(decision), "refused_qty_mismatch")
            return None
        try:
            fill = self._call(self.venue.place_order, order)
        except _VenueFailed:
            self.log.write("order", inputs, asdict(decision), "error")
            return None
        self.log.write("order", inputs, {"fill": asdict(fill)}, "filled")
        return fill

    def _protect(
        self,
        dep: Deployment,
        pos_side: float,
        qty: float,
        price_ref: float,
        stop: float,
        bar_open: pd.Timestamp,
        bar_close: datetime,
        rules: SymbolRules,
        kind: str = "stop",
    ) -> bool:
        """Gate-check and place the reduce-only exchange stop; on any failure
        trip the kill switch (no unprotected position is kept)."""
        order = Order(
            symbol=dep.symbol,
            side="sell" if pos_side > 0 else "buy",
            qty=floor_to_lot(qty, rules.lot),
            price_ref=price_ref,
            stop_price=stop,
            strategy=dep.name,
            client_id=client_order_id(dep.name, dep.symbol, bar_open, kind),
            reduce_only=True,
        )
        try:
            state = self.build_state(self._call(self.venue.get_account), bar_close)
        except _VenueFailed:
            self.kill.trigger("disaster_stop_failed")
            return False
        inputs = self._order_inputs(order, state, kind)
        if self.kill.halted:
            self.log.write("stop", inputs, {"reason": "kill_switch_halted"}, "refused")
            return False
        decision = self.gate.check(order, state, self.clock())
        if not decision.allowed or decision.max_qty != order.qty:
            self.log.write("stop", inputs, asdict(decision), "rejected")
            self.kill.trigger("disaster_stop_rejected")
            return False
        try:
            ref = self._call(self.venue.place_stop, order)
        except _VenueFailed:
            self.log.write("stop", inputs, asdict(decision), "error")
            self.kill.trigger("disaster_stop_failed")
            return False
        self.log.write("stop", inputs, {"venue_id": ref}, "placed")
        return True

    def _closed(self, dep: Deployment, now: datetime) -> pd.DataFrame:
        step = interval_to_timedelta(dep.interval)
        raw = self._call(
            self.venue.get_bars, dep.symbol, dep.interval, self.cfg.bars_limit + 1
        )
        return closed_bars(raw, step, now)

    def _aux(
        self, dep: Deployment, strat: Strategy, bars: pd.DataFrame
    ) -> dict[str, pd.Series]:
        aux: dict[str, pd.Series] = {}
        if "funding" in getattr(strat, "aux_required", ()):
            f = self._call(self.venue.get_funding, dep.symbol, FUNDING_LIMIT)
            aux["funding"] = align_asof(bars, f, FUNDING_AVAILABLE_LAG)
        return aux

    # ------------------------------------------------------------ start

    def start(self) -> None:
        """Validate, check the clock, reconcile with the venue. Raises
        ``StartupRefused`` (after logging) instead of trading."""
        now = self.clock()
        try:
            self._start(now)
        except StartupRefused as exc:
            self.log.write("runtime", {}, {"reason": str(exc)}, "start_refused")
            self._save()
            raise
        self._started = True
        self.log.write(
            "runtime",
            {"deployed": [asdict(d) for d in self.deployed]},
            {},
            "started",
        )

    def _start(self, now: datetime) -> None:
        if self.kill.halted:
            raise StartupRefused("kill switch is halted; manual reset required")
        if self._st.halted is not None:
            raise StartupRefused(
                f"a previous run halted (at {self._st.halted}); inspect the "
                "decision log, then run `agenkit reset-halt`"
            )
        validate_deployments(self.deployed, self.strategies)
        try:
            check_clock_skew(self.venue, now)
        except ClockSkewError as exc:
            self.log.write("clock_skew", {"now": now}, {"error": str(exc)}, "halt")
            self.kill.trigger("clock_skew")
            raise StartupRefused(f"clock skew: {exc}") from exc
        except Exception as exc:
            self.kill.record_api_error(now)
            raise StartupRefused(f"venue unreachable at start: {exc!r}") from exc
        for d in self.deployed:
            if "funding" in getattr(self.strategies[d.name], "aux_required", ()):
                try:
                    f = self.venue.get_funding(d.symbol, 1)
                except Exception as exc:
                    if not isinstance(exc, NotImplementedError | AttributeError):
                        self.kill.record_api_error(now)
                    raise StartupRefused(
                        f"{d.name} needs funding data the venue cannot supply: {exc!r}"
                    ) from exc
                if len(f) == 0:
                    raise StartupRefused(
                        f"{d.name} needs funding data; venue returned none"
                    )
        try:
            acct = self.venue.get_account()
            orders = self.venue.get_open_orders()
        except Exception as exc:
            self.kill.record_api_error(now)
            raise StartupRefused(f"cannot read account/open orders: {exc!r}") from exc
        owned = {d.symbol: d for d in self.deployed}
        unowned = sorted(
            {s for s in acct.positions if s not in owned}
            | {o.symbol for o in orders if o.symbol not in owned}
        )
        if unowned:
            action = "flatten" if self.cfg.flatten_unowned_on_start else "refuse"
            self.log.write(
                "unowned_position",
                {
                    "positions": [asdict(p) for p in acct.positions.values()],
                    "open_orders": [asdict(o) for o in orders],
                },
                {"unowned_symbols": unowned},
                action,
            )
            if self.cfg.flatten_unowned_on_start:
                self.kill.trigger("unowned_position_at_start")
                raise StartupRefused(
                    f"positions/orders on {unowned} are not owned by any deployed "
                    "strategy; kill switch flattened everything and halted"
                )
            raise StartupRefused(
                f"positions/orders on {unowned} are not owned by any deployed "
                "strategy; close them yourself, or set flatten_unowned_on_start: "
                "true in config/runtime.yaml to have the kill switch flatten"
            )
        self._paper_equity(acct.equity, now)
        for sym, pos in acct.positions.items():
            d = owned[sym]
            stops = [
                o
                for o in orders
                if o.symbol == sym and o.reduce_only and o.stop_price is not None
            ]
            self.log.write(
                "adopted_position",
                {"position": asdict(pos), "stops": [asdict(o) for o in stops]},
                {"owner": d.key},
                "adopted",
            )
            if not stops and not self._restore_stop(d, pos, now):
                raise StartupRefused(
                    f"could not place a protective stop for the open {sym} position"
                )

    def _restore_stop(self, d: Deployment, pos: Position, now: datetime) -> bool:
        try:
            bars = self._closed(d, now)
            rules = self._call(self.venue.symbol_rules, d.symbol)
        except _VenueFailed:
            self.kill.trigger("unprotected_position")
            return False
        a = atr(bars, self.cfg.atr_window) if len(bars) else float("nan")
        side = _sign(pos.qty)
        if not (math.isfinite(a) and a > 0):
            self.kill.trigger("unprotected_position")
            return False
        close = float(bars["close"].iloc[-1])
        stop = protective_stop(close, side, self.cfg.stop_atr_mult * a, rules.tick)
        step = interval_to_timedelta(d.interval)
        return self._protect(
            d,
            side,
            abs(pos.qty),
            close,
            stop,
            bars.index[-1],
            (bars.index[-1] + step).to_pydatetime(),
            rules,
            kind="restop",
        )

    # ------------------------------------------------------------ cycle

    def cycle(self) -> None:
        if not self._started:
            raise StartupRefused("start() has not succeeded; refusing to trade")
        now = self.clock()
        if self.kill.halted:
            self.log.write("runtime", {"now": now}, {}, "refused_halted")
            return
        try:
            self._cycle(now)
        finally:
            self._save()

    def _cycle(self, now: datetime) -> None:
        try:
            check_clock_skew(self.venue, now)
        except ClockSkewError as exc:
            self.log.write("clock_skew", {"now": now}, {"error": str(exc)}, "halt")
            self.kill.trigger("clock_skew")
            return
        except Exception as exc:
            self.log.write(
                "venue_error", {"call": "server_time"}, {"error": repr(exc)}, "error"
            )
            self.kill.record_api_error(now)
            return
        try:
            acct = self._call(self.venue.get_account)
        except _VenueFailed:
            return
        eq = self._paper_equity(acct.equity, now)
        st = self._st
        assert st.day_start_equity is not None and st.peak_equity is not None
        daily = (st.day_start_equity - eq) / st.day_start_equity
        dd = (st.peak_equity - eq) / st.peak_equity
        if not daily < self.risk.max_daily_loss:
            self.kill.trigger("daily_loss")
            return
        if not dd < self.risk.max_drawdown:
            self.kill.trigger("drawdown")
            return
        for d in self.deployed:
            if self.kill.halted:
                return
            try:
                self._step(d, now)
            except _VenueFailed:
                continue
            except Exception as exc:
                self.log.write(
                    "runtime_error", {"deployment": d.key}, {"error": repr(exc)}, "halt"
                )
                self.kill.trigger("internal_error")
                return

    def _step(self, d: Deployment, now: datetime) -> None:
        strat = self.strategies[d.name]
        step = interval_to_timedelta(d.interval)
        bars = self._closed(d, now)
        if len(bars) == 0:
            self.log.write("stale_data", {"deployment": d.key}, {"bars": 0}, "halt")
            self.kill.trigger("stale_data")
            return
        last_open = pd.Timestamp(bars.index[-1])
        last_close = (last_open + step).to_pydatetime()
        age = pd.Timestamp(now) - pd.Timestamp(last_close)
        if age > step + pd.Timedelta(seconds=self.risk.stale_after_s):
            self.log.write(
                "stale_data",
                {"deployment": d.key, "last_bar_close": last_close, "now": now},
                {"age_s": age.total_seconds()},
                "halt",
            )
            self.kill.trigger("stale_data")
            return
        done = self._st.last_bar.get(d.key)
        if done is not None and last_open <= pd.Timestamp(done):
            return  # this bar has already been decided
        try:
            check_continuity(bars, d.interval)
            aux = self._aux(d, strat, bars)
        except (DataGapError, ValueError) as exc:
            self.log.write(
                "data_error", {"deployment": d.key}, {"error": str(exc)}, "skip"
            )
            self.kill.record_api_error(now)
            return
        sig = strat.signal(bars, d.params, aux)
        target = _sign(sig.iloc[-1])
        prev = _sign(sig.iloc[-2]) if len(sig) > 1 else 0.0
        lab = label_regimes(bars, self.regime_window).iloc[-1]
        regime = None if np.isnan(lab) else REGIMES[int(lab)]
        state = self.build_state(self._call(self.venue.get_account), last_close)
        # One decision per bar, recorded BEFORE any order is submitted (a
        # failed read above leaves the bar retriable; nothing was placed).
        self._st.last_bar[d.key] = last_open.isoformat()
        self._save()
        pos = state.positions.get(d.symbol)
        cur = pos.qty if pos is not None else 0.0
        close = float(bars["close"].iloc[-1])
        self.log.write(
            "signal",
            {
                "deployment": d.key,
                "bar_open": last_open,
                "bar_close": last_close,
                "close": close,
                "regime": regime,
                "position": cur,
            },
            {"target": target, "prev_target": prev},
            "decided",
        )
        held = _sign(cur)
        if held != 0 and target != held:
            exit_order = Order(
                symbol=d.symbol,
                side="sell" if held > 0 else "buy",
                qty=abs(cur),
                price_ref=close,
                stop_price=None,
                strategy=d.name,
                client_id=client_order_id(d.name, d.symbol, last_open, "exit"),
                reduce_only=True,
            )
            fill = self.submit(exit_order, state, "exit")
            if fill is None:
                return
            self._call(self.venue.cancel_open_orders, d.symbol)
            if fill.qty < abs(cur):
                return  # partial exit: no new entry on top
            held = 0.0
        elif held == 0:
            orphans = [
                o
                for o in self._call(self.venue.get_open_orders)
                if o.symbol == d.symbol
            ]
            if orphans:  # e.g. the stop's sibling after an exchange-side stop-out
                self.log.write(
                    "orphan_orders",
                    {"orders": [asdict(o) for o in orphans]},
                    {},
                    "cancel",
                )
                self._call(self.venue.cancel_open_orders, d.symbol)
        if target == 0 or held != 0 or target == prev:
            return
        entry_inputs = {
            "deployment": d.key,
            "bar_open": last_open,
            "target": target,
            "regime": regime,
            "allowed_regimes": d.allowed_regimes,
        }
        if regime not in d.allowed_regimes:
            self.log.write("entry", entry_inputs, {}, "regime_blocked")
            return
        self._enter(d, bars, target, last_open, last_close, entry_inputs)

    def _enter(
        self,
        d: Deployment,
        bars: pd.DataFrame,
        side: float,
        bar_open: pd.Timestamp,
        bar_close: datetime,
        inputs: dict[str, Any],
    ) -> None:
        rules = self._call(self.venue.symbol_rules, d.symbol)
        close = float(bars["close"].iloc[-1])
        a = atr(bars, self.cfg.atr_window)
        if not (math.isfinite(a) and a > 0):
            self.log.write("entry", {**inputs, "atr": a}, {}, "no_atr")
            return
        stop = protective_stop(close, side, self.cfg.stop_atr_mult * a, rules.tick)
        state = self.build_state(self._call(self.venue.get_account), bar_close)
        qty = size_position(state.equity, close, stop, self.risk, rules=rules)
        sized = {
            **inputs,
            "atr": a,
            "close": close,
            "stop": stop,
            "equity": state.equity,
        }
        if not qty > 0 or not stop > 0:
            self.log.write("entry", sized, {"qty": qty}, "size_zero")
            return
        order = Order(
            symbol=d.symbol,
            side="buy" if side > 0 else "sell",
            qty=qty,
            price_ref=close,
            stop_price=stop,
            strategy=d.name,
            client_id=client_order_id(d.name, d.symbol, bar_open, "entry"),
            reduce_only=False,
        )
        self.log.write("entry", sized, {"qty": qty}, "sized")
        fill = self.submit(order, state, "entry")
        if fill is None:
            return
        self._protect(d, side, fill.qty, close, stop, bar_open, bar_close, rules)


def run_loop(
    venue: Venue,
    strategies: Iterable[Strategy] | Mapping[str, Strategy],
    deployed: list[Deployment],
    gate: RiskGate,
    kill: KillSwitch,
    log: DecisionLog,
    clock: Callable[[], datetime],
    *,
    runtime_cfg: RuntimeConfig,
    regime_window: int,
    sleep: Callable[[float], None] = time.sleep,
    max_cycles: int | None = None,
    state_path: Path | None = None,
) -> None:
    """Reconcile, then run cycles until the kill switch halts (or
    ``max_cycles``). Refuses (returns, logged) if already halted; raises
    ``StartupRefused`` if startup reconciliation refuses."""
    if kill.halted:
        log.write("runtime", {}, {"reason": "kill switch halted"}, "refused_halted")
        return
    rt = Runtime(
        venue,
        strategies,
        deployed,
        gate,
        kill,
        log,
        clock,
        runtime_cfg=runtime_cfg,
        regime_window=regime_window,
        state_path=state_path,
    )
    rt.start()
    cycles = 0
    while True:
        rt.cycle()
        cycles += 1
        if kill.halted:
            log.write("runtime", {"cycles": cycles}, {}, "halted")
            return
        if max_cycles is not None and cycles >= max_cycles:
            log.write("runtime", {"cycles": cycles}, {}, "stopped")
            return
        sleep(runtime_cfg.poll_interval_s)
