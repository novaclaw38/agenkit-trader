import math
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from agenkit_trader.config import RiskConfig
from agenkit_trader.execution.venue import (
    AccountState,
    Order,
    Position,
    SymbolRules,
)
from agenkit_trader.log import DecisionLog
from agenkit_trader.risk.gate import Decision, Reason, RiskGate

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
RULES = SymbolRules(tick=0.1, lot=0.001, min_notional=5.0)
CFG = RiskConfig()


def rules_provider(symbol: str) -> SymbolRules:
    if symbol != "BTCUSDT":
        raise KeyError(symbol)
    return RULES


def gate() -> RiskGate:
    return RiskGate(CFG, rules_provider)


def order(**kw: object) -> Order:
    base: dict[str, object] = dict(
        symbol="BTCUSDT",
        side="buy",
        qty=0.02,
        price_ref=50_000.0,
        stop_price=49_500.0,
        strategy="t",
    )
    base.update(kw)
    return Order(**base)  # type: ignore[arg-type]


def state(**kw: object) -> AccountState:
    base: dict[str, object] = dict(
        equity=1000.0,
        positions={},
        day_start_equity=1000.0,
        peak_equity=1000.0,
        last_bar_time=NOW - timedelta(seconds=30),
    )
    base.update(kw)
    return AccountState(**base)  # type: ignore[arg-type]


def check(o: Order | None = None, s: AccountState | None = None) -> Decision:
    return gate().check(o or order(), s or state(), NOW)


def test_allows_order_within_limits() -> None:
    d = check()
    assert d.allowed, d.reason
    assert d.reason == Reason.OK
    assert d.max_qty == pytest.approx(0.02)


def test_reasons_are_distinct_stable_strings() -> None:
    values = [r.value for r in Reason]
    assert len(values) == len(set(values))
    assert Reason.NO_STOP == "no_stop"


def test_rejects_risk_per_trade_above_limit() -> None:
    # 0.021 * 500 = 10.5 USDT > 10 USDT
    d = check(order(qty=0.021))
    assert not d.allowed
    assert d.reason == Reason.RISK_PER_TRADE
    assert d.max_qty == pytest.approx(0.02)


def test_rejects_missing_stop() -> None:
    d = check(order(stop_price=None))
    assert (d.allowed, d.reason) == (False, Reason.NO_STOP)


def test_rejects_stop_on_wrong_side() -> None:
    d = check(order(stop_price=50_500.0))
    assert (d.allowed, d.reason) == (False, Reason.STOP_WRONG_SIDE)


def test_rejects_daily_loss_at_limit() -> None:
    d = check(s=state(equity=970.0, day_start_equity=1000.0, peak_equity=1000.0))
    assert (d.allowed, d.reason) == (False, Reason.DAILY_LOSS)


def test_allows_daily_loss_just_below_limit() -> None:
    d = check(order(qty=0.019), state(equity=971.0))
    assert d.allowed, d.reason


def test_rejects_drawdown_at_limit() -> None:
    d = check(s=state(equity=900.0, day_start_equity=900.0, peak_equity=1000.0))
    assert (d.allowed, d.reason) == (False, Reason.DRAWDOWN)


def test_rejects_leverage_above_limit() -> None:
    # Existing 0.05 BTC long + 0.02 => 0.07 * 50k = 3500 / 1000 = 3.5x > 3x
    s = state(positions={"BTCUSDT": Position("BTCUSDT", 0.05, 50_000.0)})
    d = check(s=s)
    assert (d.allowed, d.reason) == (False, Reason.LEVERAGE)


def test_rejects_stale_data() -> None:
    d = check(s=state(last_bar_time=NOW - timedelta(seconds=121)))
    assert (d.allowed, d.reason) == (False, Reason.STALE_DATA)


def test_rejects_bar_time_in_future() -> None:
    d = check(s=state(last_bar_time=NOW + timedelta(seconds=5)))
    assert (d.allowed, d.reason) == (False, Reason.INVALID_STATE)


def test_rejects_below_lot_after_rounding() -> None:
    d = check(order(qty=0.0009))
    assert (d.allowed, d.reason) == (False, Reason.BELOW_LOT)


def test_rejects_below_min_notional() -> None:
    # 0.001 * 4000 = 4 USDT < 5
    d = check(order(qty=0.001, price_ref=4000.0, stop_price=3990.0))
    assert (d.allowed, d.reason) == (False, Reason.BELOW_MIN_NOTIONAL)


def test_rejects_when_halted() -> None:
    d = check(s=state(halted=True))
    assert (d.allowed, d.reason) == (False, Reason.HALTED)


def test_rejects_unknown_symbol_rules() -> None:
    d = check(order(symbol="ETHUSDT"))
    assert (d.allowed, d.reason) == (False, Reason.RULES_UNAVAILABLE)


@pytest.mark.parametrize(
    "kw",
    [
        {"qty": math.nan},
        {"qty": math.inf},
        {"qty": -0.01},
        {"qty": 0.0},
        {"price_ref": math.nan},
        {"price_ref": 0.0},
        {"stop_price": math.nan},
        {"stop_price": -1.0},
        {"side": "long"},
        {"symbol": ""},
        {"qty": "0.02"},
    ],
)
def test_invalid_order_fails_closed(kw: dict[str, object]) -> None:
    d = check(order(**kw))
    assert not d.allowed
    assert d.reason in (Reason.INVALID_ORDER, Reason.NO_STOP)
    assert d.max_qty == 0.0


@pytest.mark.parametrize(
    "kw",
    [
        {"equity": math.nan},
        {"equity": 0.0},
        {"equity": -5.0},
        {"day_start_equity": math.inf},
        {"peak_equity": math.nan},
        {"last_bar_time": None},
        {"last_bar_time": datetime(2026, 1, 1, 12, 0)},  # naive
        {"positions": {"BTCUSDT": Position("BTCUSDT", math.nan, 50_000.0)}},
        {"positions": {"BTCUSDT": Position("BTCUSDT", 0.01, math.inf)}},
        {"positions": {"X": Position("BTCUSDT", 0.01, 1.0)}},
        {"positions": None},
        {"halted": None},
    ],
)
def test_invalid_state_fails_closed(kw: dict[str, object]) -> None:
    d = check(s=state(**kw))
    assert (d.allowed, d.reason) == (False, Reason.INVALID_STATE)


def test_naive_now_fails_closed() -> None:
    d = gate().check(order(), state(), datetime(2026, 1, 1, 12, 0))
    assert (d.allowed, d.reason) == (False, Reason.INVALID_STATE)


def test_rules_provider_returning_garbage_fails_closed() -> None:
    g = RiskGate(CFG, lambda s: SymbolRules(tick=0.1, lot=math.nan, min_notional=5))
    d = g.check(order(), state(), NOW)
    assert (d.allowed, d.reason) == (False, Reason.RULES_UNAVAILABLE)


def test_check_never_raises_on_garbage() -> None:
    d = gate().check(None, None, None)  # type: ignore[arg-type]
    assert not d.allowed


# --- reduce-only exemption -------------------------------------------------

LOSING = dict(
    equity=850.0,
    day_start_equity=1000.0,
    peak_equity=1000.0,
    positions={"BTCUSDT": Position("BTCUSDT", 0.1, 50_000.0)},
)


def test_reducing_order_exempt_from_limits() -> None:
    # daily loss 15%, drawdown 15%, leverage ~5.9x, huge per-trade risk.
    o = order(side="sell", qty=0.1, stop_price=51_000.0)
    d = check(o, state(**LOSING))
    assert d.allowed, d.reason


def test_reducing_order_still_blocked_by_stale_data() -> None:
    o = order(side="sell", qty=0.1, stop_price=51_000.0)
    s = state(**LOSING, last_bar_time=NOW - timedelta(seconds=500))
    assert check(o, s).reason == Reason.STALE_DATA


def test_reducing_order_still_blocked_by_halt() -> None:
    o = order(side="sell", qty=0.1, stop_price=51_000.0)
    assert check(o, state(**LOSING, halted=True)).reason == Reason.HALTED


def test_reducing_order_still_needs_stop() -> None:
    o = order(side="sell", qty=0.1, stop_price=None)
    assert check(o, state(**LOSING)).reason == Reason.NO_STOP


def test_order_that_flips_position_is_not_exempt() -> None:
    o = order(side="sell", qty=0.2, stop_price=51_000.0)
    d = check(o, state(**LOSING))
    assert not d.allowed


def test_increasing_order_not_exempt() -> None:
    d = check(order(), state(**LOSING))
    assert not d.allowed


# --- logging ---------------------------------------------------------------


def test_decisions_are_logged(tmp_path: object) -> None:
    import json
    from pathlib import Path

    p = Path(str(tmp_path)) / "d.jsonl"
    g = RiskGate(CFG, rules_provider, log=DecisionLog(p))
    g.check(order(), state(), NOW)
    g.check(order(stop_price=None), state(), NOW)
    recs = [json.loads(line) for line in p.read_text().splitlines()]
    assert [r["kind"] for r in recs] == ["risk_gate", "risk_gate"]
    assert recs[0]["result"] == "allowed"
    assert recs[1]["result"] == "rejected"
    assert recs[1]["output"]["reason"] == "no_stop"
    assert recs[0]["inputs"]["order"]["qty"] == 0.02


def test_gate_is_pure() -> None:
    o, s = order(), state()
    assert gate().check(o, s, NOW) == gate().check(o, s, NOW)


# --- property: an allowed order never exceeds any limit ---------------------

finite = st.floats(allow_nan=False, allow_infinity=False)


@settings(max_examples=400, deadline=None)
@given(
    equity=st.floats(1.0, 1e6),
    day_frac=st.floats(0.5, 1.5),
    peak_frac=st.floats(1.0, 1.5),
    price=st.floats(1.0, 1e5),
    stop_frac=st.floats(0.5, 1.5),
    qty=st.floats(1e-4, 100.0),
    side=st.sampled_from(["buy", "sell"]),
    pos_qty=st.floats(-50.0, 50.0),
    age=st.integers(0, 300),
)
def test_allowed_orders_never_exceed_limits(
    equity: float,
    day_frac: float,
    peak_frac: float,
    price: float,
    stop_frac: float,
    qty: float,
    side: str,
    pos_qty: float,
    age: int,
) -> None:
    day_start = equity * day_frac
    peak = max(day_start, equity) * peak_frac
    stop = price * stop_frac
    positions = {"BTCUSDT": Position("BTCUSDT", pos_qty, price)} if pos_qty != 0 else {}
    s = state(
        equity=equity,
        day_start_equity=day_start,
        peak_equity=peak,
        positions=positions,
        last_bar_time=NOW - timedelta(seconds=age),
    )
    o = order(side=side, qty=qty, price_ref=price, stop_price=stop)
    d = check(o, s)
    if not d.allowed:
        return
    assert age <= CFG.stale_after_s
    assert d.max_qty <= qty + 1e-12
    signed = d.max_qty if side == "buy" else -d.max_qty
    reducing = pos_qty * signed < 0 and abs(signed) <= abs(pos_qty) + 1e-12
    assert d.max_qty * price >= RULES.min_notional - 1e-9
    if reducing:
        return
    eps = 1e-9 * max(1.0, equity)
    assert abs(price - stop) * d.max_qty <= equity * CFG.max_risk_per_trade + eps
    assert (day_start - equity) / day_start < CFG.max_daily_loss
    assert (peak - equity) / peak < CFG.max_drawdown
    assert abs(pos_qty + signed) * price / equity <= CFG.max_leverage + 1e-9
