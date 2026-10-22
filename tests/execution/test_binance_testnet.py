"""BinanceTestnetVenue with mocked HTTP. No request leaves the process."""

import hashlib
import hmac
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import httpx
import pandas as pd
import pytest

from agenkit_trader.config import RiskConfig
from agenkit_trader.execution.binance_testnet import (
    DEFAULT_BASE_URL,
    BinanceAPIError,
    BinanceTestnetVenue,
    CredentialsError,
)
from agenkit_trader.execution.venue import (
    ClockSkewError,
    Order,
    OrderNotFilledError,
    OrderStatus,
    OrderStatusUnknownError,
    VenueError,
)
from agenkit_trader.killswitch import KillSwitch
from agenkit_trader.log import DecisionLog

KEY = "test-api-key-AAAAAAAAAAAAAAAA"
SECRET = "test-secret-BBBBBBBBBBBBBBBBBBBB"
LOCAL_MS = 1_767_268_800_000  # 2026-01-01T12:00:00Z


class Server:
    """Records requests and answers from a route table."""

    def __init__(self, server_ms: int = LOCAL_MS + 250) -> None:
        self.server_ms = server_ms
        self.requests: list[httpx.Request] = []
        self.routes: dict[tuple[str, str], Any] = {
            ("GET", "/fapi/v1/time"): lambda r: {"serverTime": self.server_ms},
            ("GET", "/fapi/v1/exchangeInfo"): lambda r: EXCHANGE_INFO,
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = self.routes.get((request.method, request.url.path))
        if route is None:
            return httpx.Response(404, json={"code": -1, "msg": "no route"})
        out = route(request)
        if isinstance(out, httpx.Response):
            return out
        return httpx.Response(200, json=out)

    def last(self, path: str) -> httpx.Request:
        return [r for r in self.requests if r.url.path == path][-1]


EXCHANGE_INFO = {
    "symbols": [
        {
            "symbol": "BTCUSDT",
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                {"filterType": "LOT_SIZE", "stepSize": "0.001"},
                {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.001"},
                {"filterType": "MIN_NOTIONAL", "notional": "100"},
            ],
        }
    ]
}


@pytest.fixture
def keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BINANCE_TESTNET_KEY", KEY)
    monkeypatch.setenv("BINANCE_TESTNET_SECRET", SECRET)


def venue(server: Server, **kw: Any) -> BinanceTestnetVenue:
    return BinanceTestnetVenue(
        transport=httpx.MockTransport(server), clock_ms=lambda: LOCAL_MS, **kw
    )


def query(r: httpx.Request) -> dict[str, str]:
    return dict(parse_qsl(r.url.query.decode()))


def assert_signed(r: httpx.Request, offset_ms: int = 250) -> dict[str, str]:
    raw = r.url.query.decode()
    assert raw.count("signature=") == 1 and raw.rsplit("&", 1)[1].startswith(
        "signature="
    )
    payload, sig = raw.rsplit("&signature=", 1)
    expected = hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    assert sig == expected
    assert r.headers["X-MBX-APIKEY"] == KEY
    q = query(r)
    assert int(q["timestamp"]) == LOCAL_MS + offset_ms
    assert q["recvWindow"] == "5000"
    return q


def test_defaults() -> None:
    assert DEFAULT_BASE_URL == "https://testnet.binancefuture.com"
    v = BinanceTestnetVenue(transport=httpx.MockTransport(Server()))
    assert v.base_url == DEFAULT_BASE_URL


def test_sync_time_measures_offset() -> None:
    s = Server(server_ms=LOCAL_MS + 400)
    v = venue(s)
    assert v.sync_time() == 400
    assert v.server_time() == datetime.fromtimestamp((LOCAL_MS + 400) / 1000, UTC)


@pytest.mark.parametrize("skew", [1001, -1001, 5000])
def test_skew_above_1000ms_raises(skew: int) -> None:
    v = venue(Server(server_ms=LOCAL_MS + skew))
    with pytest.raises(ClockSkewError):
        v.sync_time()


def test_skew_of_1000ms_is_accepted() -> None:
    assert venue(Server(server_ms=LOCAL_MS + 1000)).sync_time() == 1000


def test_signed_request_matches_hmac(keys: None) -> None:
    s = Server()
    s.routes[("GET", "/fapi/v2/account")] = lambda r: {"totalMarginBalance": "1234.5"}
    s.routes[("GET", "/fapi/v2/positionRisk")] = lambda r: [
        {"symbol": "BTCUSDT", "positionAmt": "0.010", "entryPrice": "50000.0"},
        {"symbol": "ETHUSDT", "positionAmt": "0", "entryPrice": "0.0"},
    ]
    v = venue(s)
    acct = v.get_account()
    assert acct.equity == 1234.5
    assert list(acct.positions) == ["BTCUSDT"]
    assert acct.positions["BTCUSDT"].qty == 0.01
    assert acct.positions["BTCUSDT"].entry_price == 50000.0
    assert_signed(s.last("/fapi/v2/account"))
    assert_signed(s.last("/fapi/v2/positionRisk"))
    # the time sync happened first, unsigned and without the key
    t = s.last("/fapi/v1/time")
    assert "X-MBX-APIKEY" not in t.headers and "signature" not in str(t.url)


def test_signed_request_auto_syncs_time_once(keys: None) -> None:
    s = Server()
    s.routes[("GET", "/fapi/v1/openOrders")] = lambda r: []
    s.routes[("GET", "/fapi/v1/openAlgoOrders")] = lambda r: []
    v = venue(s)
    v.get_open_orders()
    v.get_open_orders()
    assert len([r for r in s.requests if r.url.path == "/fapi/v1/time"]) == 1


def test_place_market_order(keys: None) -> None:
    s = Server()
    s.routes[("POST", "/fapi/v1/order")] = lambda r: {
        "status": "FILLED",
        "executedQty": "0.012",
        "avgPrice": "50010.5",
        "updateTime": LOCAL_MS,
    }
    v = venue(s)
    o = Order("BTCUSDT", "buy", 0.012, 50000.0, 49000.0, "trend", "akabc", False)
    fill = v.place_order(o)
    q = assert_signed(s.last("/fapi/v1/order"))
    assert q["symbol"] == "BTCUSDT" and q["side"] == "BUY" and q["type"] == "MARKET"
    assert q["quantity"] == "0.012"
    assert q["newClientOrderId"] == "akabc"
    assert q["newOrderRespType"] == "RESULT"
    assert "reduceOnly" not in q
    assert fill.qty == 0.012 and fill.price == 50010.5 and fill.side == "buy"


def test_place_reduce_only_order(keys: None) -> None:
    s = Server()
    s.routes[("POST", "/fapi/v1/order")] = lambda r: {
        "status": "FILLED",
        "executedQty": "0.5",
        "avgPrice": "100",
        "updateTime": LOCAL_MS,
    }
    v = venue(s)
    v.place_order(Order("BTCUSDT", "sell", 0.5, 100.0, None, "t", "x", True))
    q = query(s.last("/fapi/v1/order"))
    assert q["reduceOnly"] == "true" and q["side"] == "SELL"


def test_unfilled_market_order_raises(keys: None) -> None:
    s = Server()
    s.routes[("POST", "/fapi/v1/order")] = lambda r: {
        "status": "EXPIRED",
        "executedQty": "0",
        "avgPrice": "0",
        "updateTime": LOCAL_MS,
    }
    v = venue(s)
    with pytest.raises(OrderNotFilledError, match="EXPIRED"):
        v.place_order(Order("BTCUSDT", "buy", 0.012, 5e4, 4.9e4, "t", "x", False))


def test_new_status_with_zero_executed_is_unknown_not_unfilled(keys: None) -> None:
    s = Server()
    s.routes[("POST", "/fapi/v1/order")] = lambda r: {
        "status": "NEW",
        "executedQty": "0",
        "avgPrice": "0",
        "updateTime": LOCAL_MS,
    }
    v = venue(s)
    with pytest.raises(OrderStatusUnknownError) as ei:
        v.place_order(Order("BTCUSDT", "buy", 0.012, 5e4, 4.9e4, "t", "x", False))
    assert not isinstance(ei.value, OrderNotFilledError)


def test_get_order_by_client_id(keys: None) -> None:
    s = Server()

    def lookup(r: httpx.Request) -> Any:
        if query(r)["origClientOrderId"] == "gone":
            return httpx.Response(
                400, json={"code": -2013, "msg": "Order does not exist."}
            )
        return {
            "clientOrderId": "akx",
            "status": "FILLED",
            "executedQty": "0.012",
            "avgPrice": "50000.5",
        }

    s.routes[("GET", "/fapi/v1/order")] = lookup
    v = venue(s)
    st = v.get_order("BTCUSDT", "akx")
    q = assert_signed(s.last("/fapi/v1/order"))
    assert q["symbol"] == "BTCUSDT" and q["origClientOrderId"] == "akx"
    assert st == OrderStatus("akx", "FILLED", 0.012, 50000.5)
    assert v.get_order("BTCUSDT", "gone") is None


def test_get_order_other_errors_propagate(keys: None) -> None:
    s = Server()
    s.routes[("GET", "/fapi/v1/order")] = lambda r: httpx.Response(
        503, json={"code": -1007, "msg": "Timeout"}
    )
    with pytest.raises(BinanceAPIError):
        venue(s).get_order("BTCUSDT", "akx")


def test_cancel_stop_by_client_id(keys: None) -> None:
    s = Server()
    s.routes[("DELETE", "/fapi/v1/algoOrder")] = lambda r: {"code": "200"}
    venue(s).cancel_stop("BTCUSDT", "aks")
    q = assert_signed(s.last("/fapi/v1/algoOrder"))
    assert q["symbol"] == "BTCUSDT" and q["clientAlgoId"] == "aks"


@pytest.mark.parametrize(
    "url",
    [
        "https://fapi.binance.com",
        "http://testnet.binancefuture.com",
        "https://testnet.binancefuture.com.evil.example",
        "https://testnet.binancefuture.com:8443",
        "https://user@testnet.binancefuture.com",
        "https://testnet.binancefuture.com/fapi",
        "https://mock.test",
    ],
)
def test_base_url_must_be_the_testnet_host(url: str) -> None:
    with pytest.raises(ValueError, match="testnet"):
        BinanceTestnetVenue(base_url=url, transport=httpx.MockTransport(Server()))


def test_test_host_only_via_explicit_argument() -> None:
    v = BinanceTestnetVenue(
        base_url="https://mock.test",
        transport=httpx.MockTransport(Server()),
        _test_host="mock.test",
    )
    assert v.base_url == "https://mock.test"
    BinanceTestnetVenue(base_url="https://testnet.binancefuture.com/")


def test_place_stop_uses_algo_conditional_stop_market(keys: None) -> None:
    s = Server()
    s.routes[("POST", "/fapi/v1/algoOrder")] = lambda r: {"algoId": 77}
    v = venue(s)
    o = Order("BTCUSDT", "sell", 0.012, 50000.0, 48123.4, "trend", "aks", True)
    assert v.place_stop(o) == "77"
    q = assert_signed(s.last("/fapi/v1/algoOrder"))
    assert q["algoType"] == "CONDITIONAL" and q["type"] == "STOP_MARKET"
    assert q["side"] == "SELL" and q["quantity"] == "0.012"
    assert q["triggerPrice"] == "48123.4"
    assert q["reduceOnly"] == "true" and q["workingType"] == "MARK_PRICE"
    assert q["clientAlgoId"] == "aks"


def test_place_stop_requires_reduce_only_and_stop(keys: None) -> None:
    v = venue(Server())
    with pytest.raises(ValueError):
        v.place_stop(Order("BTCUSDT", "sell", 0.01, 5e4, 4.8e4, "t", "x", False))
    with pytest.raises(ValueError):
        v.place_stop(Order("BTCUSDT", "sell", 0.01, 5e4, None, "t", "x", True))


def test_open_orders_include_algo_orders(keys: None) -> None:
    s = Server()
    s.routes[("GET", "/fapi/v1/openOrders")] = lambda r: [
        {
            "symbol": "ETHUSDT",
            "side": "BUY",
            "origQty": "1.0",
            "type": "LIMIT",
            "stopPrice": "0",
            "clientOrderId": "c1",
            "reduceOnly": False,
        }
    ]
    s.routes[("GET", "/fapi/v1/openAlgoOrders")] = lambda r: [
        {
            "symbol": "BTCUSDT",
            "side": "SELL",
            "quantity": "0.012",
            "orderType": "STOP_MARKET",
            "triggerPrice": "48000",
            "clientAlgoId": "aks",
            "reduceOnly": True,
        }
    ]
    oo = venue(s).get_open_orders()
    assert {o.symbol for o in oo} == {"ETHUSDT", "BTCUSDT"}
    stop = next(o for o in oo if o.symbol == "BTCUSDT")
    assert stop.reduce_only and stop.stop_price == 48000.0 and stop.side == "sell"
    assert stop.kind == "STOP_MARKET" and stop.client_id == "aks"


def test_cancel_open_orders_cancels_regular_and_algo(keys: None) -> None:
    s = Server()
    s.routes[("DELETE", "/fapi/v1/allOpenOrders")] = lambda r: {"code": 200}
    s.routes[("DELETE", "/fapi/v1/algoOpenOrders")] = lambda r: {"code": 200}
    venue(s).cancel_open_orders("BTCUSDT")
    assert query(s.last("/fapi/v1/allOpenOrders"))["symbol"] == "BTCUSDT"
    assert query(s.last("/fapi/v1/algoOpenOrders"))["symbol"] == "BTCUSDT"


def test_flatten_all_cancels_and_closes_reduce_only(keys: None) -> None:
    s = Server()
    reads = {"n": 0}

    def positions(r: httpx.Request) -> Any:
        reads["n"] += 1  # flat after the close (verified by a re-read)
        amt = "-0.020" if reads["n"] == 1 else "0.000"
        return [
            {"symbol": "BTCUSDT", "positionAmt": amt, "entryPrice": "50000"},
            {"symbol": "ETHUSDT", "positionAmt": "0.000", "entryPrice": "0"},
        ]

    s.routes[("GET", "/fapi/v2/positionRisk")] = positions
    s.routes[("GET", "/fapi/v1/openOrders")] = lambda r: []
    s.routes[("GET", "/fapi/v1/openAlgoOrders")] = lambda r: [
        {
            "symbol": "BTCUSDT",
            "side": "BUY",
            "quantity": "0.02",
            "orderType": "STOP_MARKET",
            "triggerPrice": "52000",
            "clientAlgoId": "s",
            "reduceOnly": True,
        }
    ]
    s.routes[("DELETE", "/fapi/v1/allOpenOrders")] = lambda r: {"code": 200}
    s.routes[("DELETE", "/fapi/v1/algoOpenOrders")] = lambda r: {"code": 200}
    s.routes[("POST", "/fapi/v1/order")] = lambda r: {
        "status": "FILLED",
        "executedQty": "0.020",
        "avgPrice": "50000",
        "updateTime": LOCAL_MS,
    }
    venue(s).flatten_all()
    q = assert_signed(s.last("/fapi/v1/order"))
    assert q["side"] == "BUY" and q["quantity"] == "0.020"
    assert q["reduceOnly"] == "true" and q["type"] == "MARKET"
    assert query(s.last("/fapi/v1/algoOpenOrders"))["symbol"] == "BTCUSDT"
    posted = [r for r in s.requests if r.method == "POST"]
    assert len(posted) == 1  # only the BTC close; nothing for the flat ETH


def test_flatten_all_raises_after_attempting_everything(keys: None) -> None:
    s = Server()
    s.routes[("GET", "/fapi/v2/positionRisk")] = lambda r: [
        {"symbol": "BTCUSDT", "positionAmt": "0.020", "entryPrice": "50000"},
        {"symbol": "ETHUSDT", "positionAmt": "1.0", "entryPrice": "2000"},
    ]
    s.routes[("GET", "/fapi/v1/openOrders")] = lambda r: []
    s.routes[("GET", "/fapi/v1/openAlgoOrders")] = lambda r: []

    def order(r: httpx.Request) -> Any:
        if query(r)["symbol"] == "BTCUSDT":
            return httpx.Response(400, json={"code": -2019, "msg": "Margin"})
        return {"status": "FILLED", "executedQty": "1.0", "avgPrice": "2000"}

    s.routes[("POST", "/fapi/v1/order")] = order
    with pytest.raises(VenueError, match="BTCUSDT"):
        venue(s).flatten_all()
    assert len([r for r in s.requests if r.method == "POST"]) == 2


def test_symbol_rules_from_exchange_info() -> None:
    r = venue(Server()).symbol_rules("BTCUSDT")
    assert (r.tick, r.lot, r.min_notional) == (0.1, 0.001, 100.0)
    with pytest.raises(VenueError):
        venue(Server()).symbol_rules("NOPE")


def test_get_bars_parses_klines() -> None:
    s = Server()
    t0 = LOCAL_MS - 2 * 3600_000
    s.routes[("GET", "/fapi/v1/klines")] = lambda r: [
        [t0, "1", "2", "0.5", "1.5", "10", t0 + 3599_999, "0", 1, "0", "0", "0"],
        [
            t0 + 3600_000,
            "1.5",
            "2.5",
            "1",
            "2",
            "11",
            t0 + 7199_999,
            "0",
            1,
            "0",
            "0",
            "0",
        ],
    ]
    bars = venue(s).get_bars("BTCUSDT", "1h", 2)
    q = query(s.last("/fapi/v1/klines"))
    assert q == {"symbol": "BTCUSDT", "interval": "1h", "limit": "2"}
    assert list(bars.columns) == ["open", "high", "low", "close", "volume"]
    assert bars.index[0] == pd.Timestamp(t0, unit="ms", tz="UTC")
    assert bars["close"].tolist() == [1.5, 2.0]


def test_get_funding_and_mark_are_unsigned() -> None:
    s = Server()
    s.routes[("GET", "/fapi/v1/fundingRate")] = lambda r: [
        {
            "symbol": "BTCUSDT",
            "fundingTime": LOCAL_MS - 8 * 3600_000 + 1,
            "fundingRate": "0.0001",
        },
        {"symbol": "BTCUSDT", "fundingTime": LOCAL_MS + 2, "fundingRate": "-0.0002"},
    ]
    s.routes[("GET", "/fapi/v1/premiumIndex")] = lambda r: {"markPrice": "50001.2"}
    v = venue(s)
    f = v.get_funding("BTCUSDT", 2)
    assert f.tolist() == [0.0001, -0.0002]
    assert f.index[1] == pd.Timestamp(LOCAL_MS + 2, unit="ms", tz="UTC")
    assert v.get_mark_price("BTCUSDT") == 50001.2
    for r in s.requests:
        assert "signature" not in str(r.url) and "X-MBX-APIKEY" not in r.headers


def test_signed_call_without_keys_raises_naming_env_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BINANCE_TESTNET_KEY", raising=False)
    monkeypatch.delenv("BINANCE_TESTNET_SECRET", raising=False)
    s = Server()
    v = venue(s)
    with pytest.raises(CredentialsError, match="BINANCE_TESTNET_KEY"):
        v.get_account()
    assert not v.has_credentials
    assert [r for r in s.requests if "signature" in str(r.url)] == []


def test_keys_never_in_repr_errors_or_logs(keys: None, tmp_path: Path) -> None:
    s = Server()
    s.routes[("GET", "/fapi/v2/account")] = lambda r: httpx.Response(
        401, json={"code": -2015, "msg": "Invalid API-key, IP, or permissions"}
    )
    s.routes[("GET", "/fapi/v2/positionRisk")] = lambda r: httpx.Response(
        500, text="boom"
    )

    def broken(r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=r)

    s.routes[("GET", "/fapi/v1/openOrders")] = broken
    v = venue(s)
    texts = [repr(v), str(v)]
    for call in (v.get_account, v.flatten_all):
        with pytest.raises(VenueError) as ei:
            call()
        texts += [str(ei.value), repr(ei.value)]
        cause = ei.value.__cause__ or ei.value.__context__
        texts.append(repr(cause))
    assert isinstance(
        pytest.raises(BinanceAPIError, v.get_account).value, BinanceAPIError
    )
    log = DecisionLog(tmp_path / "d.jsonl")
    ks = KillSwitch(v, log, RiskConfig())
    assert ks.trigger("test") is False  # flatten failed: halted anyway
    log.write("venue", {"venue": v}, {"repr": repr(v)}, "ok")
    texts.append((tmp_path / "d.jsonl").read_text())
    blob = "\n".join(texts)
    assert KEY not in blob and SECRET not in blob
    assert "-2015" in blob  # the useful part of the error is kept
    assert not any(hasattr(v, a) for a in ("key", "secret", "api_key", "api_secret"))
    assert KEY not in json.dumps(vars(v), default=repr)
    assert SECRET not in json.dumps(vars(v), default=repr)


@pytest.mark.network
def test_testnet_smoke_unsigned_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reads public endpoints on the TESTNET with no keys at all."""
    monkeypatch.delenv("BINANCE_TESTNET_KEY", raising=False)
    monkeypatch.delenv("BINANCE_TESTNET_SECRET", raising=False)
    v = BinanceTestnetVenue()
    assert not v.has_credentials
    assert v.base_url == "https://testnet.binancefuture.com"
    t = v.server_time()
    assert abs((t - datetime.now(UTC)).total_seconds()) < 60
    r = v.symbol_rules("BTCUSDT")
    assert r.tick > 0 and r.lot > 0 and r.min_notional >= 0


# ------------------------------------------------ rate limits (429 / 418)


@pytest.mark.parametrize(("status", "banned"), [(429, False), (418, True)])
def test_rate_limit_raises_with_retry_after(status: int, banned: bool) -> None:
    from agenkit_trader.execution.venue import RateLimited

    s = Server()
    s.routes[("GET", "/fapi/v1/premiumIndex")] = lambda r: httpx.Response(
        status, headers={"Retry-After": "17"}, json={"code": -1003, "msg": "slow"}
    )
    with pytest.raises(RateLimited) as ei:
        venue(s).get_mark_price("BTCUSDT")
    assert ei.value.retry_after == 17.0
    assert ei.value.banned is banned
    assert isinstance(ei.value, VenueError)


def test_rate_limit_without_header_has_no_retry_after() -> None:
    from agenkit_trader.execution.venue import RateLimited

    s = Server()
    s.routes[("GET", "/fapi/v1/premiumIndex")] = lambda r: httpx.Response(
        429, json={"code": -1003, "msg": "slow"}
    )
    with pytest.raises(RateLimited) as ei:
        venue(s).get_mark_price("BTCUSDT")
    assert ei.value.retry_after is None


# ------------------------------------------------ flatten verification


def _flatten_server(rows: list[dict[str, str]]) -> Server:
    s = Server()
    state = {"rows": rows}
    s.routes[("GET", "/fapi/v2/positionRisk")] = lambda r: state["rows"]
    s.routes[("GET", "/fapi/v1/openOrders")] = lambda r: []
    s.routes[("GET", "/fapi/v1/openAlgoOrders")] = lambda r: []
    s.routes[("DELETE", "/fapi/v1/allOpenOrders")] = lambda r: {"code": 200}
    s.routes[("DELETE", "/fapi/v1/algoOpenOrders")] = lambda r: {"code": 200}
    s.routes[("POST", "/fapi/v1/order")] = lambda r: {
        "status": "FILLED",
        "executedQty": query(r)["quantity"],
        "avgPrice": "50000",
    }
    return s


def test_flatten_all_position_read_failure_raises_loudly(
    keys: None, caplog: pytest.LogCaptureFixture
) -> None:
    s = _flatten_server([])
    s.routes[("GET", "/fapi/v2/positionRisk")] = lambda r: httpx.Response(
        500, json={"code": -1000, "msg": "down"}
    )
    with pytest.raises(VenueError, match="positions"):
        venue(s).flatten_all()
    assert any("flatten" in rec.getMessage() for rec in caplog.records)
    assert not [r for r in s.requests if r.method == "POST"]


def test_flatten_all_residual_position_raises(
    keys: None, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # the close "fills" but the venue still reports the position afterwards
    s = _flatten_server(
        [{"symbol": "BTCUSDT", "positionAmt": "0.020", "entryPrice": "50000"}]
    )
    v = venue(s)
    with pytest.raises(VenueError, match="residual"):
        v.flatten_all()
    ks = KillSwitch(v, DecisionLog(tmp_path / "l.jsonl"), RiskConfig())
    assert ks.trigger("test") is False


def test_flatten_all_rereads_and_succeeds_when_flat(keys: None) -> None:
    s = _flatten_server(
        [{"symbol": "BTCUSDT", "positionAmt": "0.020", "entryPrice": "50000"}]
    )
    rows = [{"symbol": "BTCUSDT", "positionAmt": "0.020", "entryPrice": "50000"}]
    calls = {"n": 0}

    def pos(r: httpx.Request) -> Any:
        calls["n"] += 1
        return rows if calls["n"] == 1 else []

    s.routes[("GET", "/fapi/v2/positionRisk")] = pos
    venue(s).flatten_all()
    assert calls["n"] == 2


def test_market_fill_fee_is_unknown_not_zero(keys: None) -> None:
    s = _flatten_server([])
    o = Order("BTCUSDT", "buy", 0.012, 50000.0, 49000.0, "trend", "akabc", False)
    assert venue(s).place_order(o).fee is None
