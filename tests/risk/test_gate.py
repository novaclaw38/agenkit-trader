import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import event, given, settings, target
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
from agenkit_trader.risk.sizing import is_lot_multiple

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


# --- fix round 1 ------------------------------------------------------------


def test_qty_not_lot_multiple_rejected() -> None:
    o = order(qty=0.0119, price_ref=1000.0, stop_price=100.0)
    d = check(o)
    assert (d.allowed, d.reason) == (False, Reason.QTY_NOT_LOT_MULTIPLE)
    assert Reason.QTY_NOT_LOT_MULTIPLE == "qty_not_lot_multiple"


def test_exact_lot_multiple_allowed() -> None:
    d = check(order(qty=0.011, price_ref=1000.0, stop_price=100.0))
    assert d.allowed, d.reason


def test_closing_long_without_stop_allowed() -> None:
    o = order(side="sell", qty=0.1, stop_price=None)
    d = check(o, state(**LOSING))
    assert d.allowed, d.reason


def test_closing_long_with_stop_on_wrong_side_allowed() -> None:
    o = order(side="sell", qty=0.05, stop_price=40_000.0)
    assert check(o, state(**LOSING)).allowed


def test_increasing_without_stop_rejected() -> None:
    s = state(positions={"BTCUSDT": Position("BTCUSDT", 0.001, 50_000.0)})
    assert check(order(stop_price=None), s).reason == Reason.NO_STOP


def test_flip_without_stop_rejected() -> None:
    o = order(side="sell", qty=0.2, stop_price=None)
    assert check(o, state(**LOSING)).reason == Reason.NO_STOP


def test_reduce_order_with_bad_stop_is_invalid() -> None:
    for bad in (math.nan, math.inf, -1.0, 0.0):
        o = order(side="sell", qty=0.05, stop_price=bad)
        assert check(o, state(**LOSING)).reason == Reason.INVALID_ORDER


# --- property: an allowed order never exceeds any limit ---------------------
#
# Quantities are generated as whole lots placed around the risk-budget
# boundary so a healthy share of examples is ALLOWED. ``_scenario`` is shared
# by the hypothesis tests and a deterministic grid test that proves the
# allowed branch is actually exercised (non-vacuity).

LOT = RULES.lot


def _scenario(
    equity: float,
    price: float,
    dist_frac: float,
    offset: int,
    side: str,
    pos_lots: int,
    other_notional_frac: float,
    day_loss: float,
    dd: float,
) -> tuple[Order, AccountState, float, float]:
    dist = price * dist_frac
    stop = price - dist if side == "buy" else price + dist
    n_max = math.floor(equity * CFG.max_risk_per_trade / dist / LOT)
    n = max(1, n_max + offset)
    qty = n * LOT
    positions = {}
    if pos_lots:
        positions["BTCUSDT"] = Position("BTCUSDT", pos_lots * LOT, price)
    if other_notional_frac > 0:
        positions["ETHUSDT"] = Position("ETHUSDT", 1.0, other_notional_frac * equity)
    day_start = equity / (1 - day_loss)
    peak = max(day_start, equity / (1 - dd))
    s = state(
        equity=equity,
        day_start_equity=day_start,
        peak_equity=peak,
        positions=positions,
    )
    return order(side=side, qty=qty, price_ref=price, stop_price=stop), s, qty, stop


def _assert_within_limits(o: Order, s: AccountState, d: Decision) -> None:
    assert o.stop_price is not None
    assert is_lot_multiple(o.qty, LOT)
    assert d.max_qty <= o.qty * (1 + 1e-12)
    assert o.qty * o.price_ref >= RULES.min_notional
    signed = o.qty if o.side == "buy" else -o.qty
    cur = s.positions["BTCUSDT"].qty if "BTCUSDT" in s.positions else 0.0
    if cur * signed < 0 and o.qty <= abs(cur):
        return  # reduce-only exemption
    eq = s.equity
    eps = 1e-9 * max(1.0, eq)
    assert o.qty * abs(o.price_ref - o.stop_price) <= eq * CFG.max_risk_per_trade + eps
    assert (s.day_start_equity - eq) / s.day_start_equity < CFG.max_daily_loss
    assert (s.peak_equity - eq) / s.peak_equity < CFG.max_drawdown
    other = sum(
        abs(p.qty) * p.entry_price for k, p in s.positions.items() if k != o.symbol
    )
    sym = abs(cur + signed) * o.price_ref
    assert (other + sym) / eq <= CFG.max_leverage + 1e-9
    assert sym / eq <= CFG.max_symbol_notional_frac + 1e-9


scenario_args = dict(
    equity=st.floats(100.0, 1e5),
    price=st.floats(10.0, 1e5),
    dist_frac=st.floats(0.001, 0.2),
    offset=st.integers(-3, 1),
    side=st.sampled_from(["buy", "sell"]),
    pos_lots=st.integers(-5, 5),
    other_notional_frac=st.sampled_from([0.0, 0.5, 2.0, 2.99]),
    day_loss=st.sampled_from([0.0, 0.01, 0.029, 0.03, 0.05]),
    dd=st.sampled_from([0.0, 0.05, 0.099, 0.1, 0.2]),
)


@settings(max_examples=600, deadline=None)
@given(**scenario_args)
def test_allowed_orders_never_exceed_limits(**kw: object) -> None:
    o, s, _, _ = _scenario(**kw)  # type: ignore[arg-type]
    d = check(o, s)
    event("allowed" if d.allowed else f"rejected:{d.reason}")
    target(1.0 if d.allowed else 0.0, label="allowed")
    if d.allowed:
        _assert_within_limits(o, s, d)


@settings(max_examples=400, deadline=None)
@given(
    **scenario_args,
    jitter=st.one_of(st.floats(1e-6, 0.4), st.floats(-0.4, -1e-6)),
)
def test_jittered_non_multiples_rejected(jitter: float, **kw: object) -> None:
    o, s, qty, _ = _scenario(**kw)  # type: ignore[arg-type]
    jq = qty * (1 + jitter)
    if jq < LOT:
        return  # below one lot is BELOW_LOT, an earlier check
    d = check(replace(o, qty=jq), s)
    # "Whole number of lots" is defined by the documented relative tolerance
    # (sizing.LOT_REL_TOL = 1e-12 of qty), computed independently here. At
    # large lot counts a jitter can land within it; such a qty IS a multiple
    # and must instead pass every limit on its exact value.
    n = round(jq / LOT)
    if n >= 1 and abs(n * LOT - jq) <= 1e-12 * jq:
        event("jitter landed on a whole lot")
        if d.allowed:
            _assert_within_limits(replace(o, qty=jq), s, d)
        return
    event("checked")
    assert not d.allowed
    assert d.reason == Reason.QTY_NOT_LOT_MULTIPLE


def test_property_scenarios_are_not_vacuous() -> None:
    allowed = rejected = 0
    for equity in (1000.0, 250.0, 7777.0):
        for price in (50_000.0, 1000.0, 37.3):
            for dist_frac in (0.01, 0.1):
                for offset in (-1, 0, 1):
                    for side in ("buy", "sell"):
                        o, s, _, _ = _scenario(
                            equity, price, dist_frac, offset, side, 0, 0.0, 0.0, 0.0
                        )
                        d = check(o, s)
                        if d.allowed:
                            allowed += 1
                            _assert_within_limits(o, s, d)
                        else:
                            rejected += 1
    assert allowed > 20
    assert rejected > 0


def test_boundary_exactly_at_budget_allowed_one_lot_over_rejected() -> None:
    # 10 USDT budget, dist 1000 -> exactly 0.01 allowed, 0.011 rejected.
    at = check(order(qty=0.01, price_ref=10_000.0, stop_price=9_000.0))
    assert at.allowed, at.reason
    over = check(order(qty=0.011, price_ref=10_000.0, stop_price=9_000.0))
    assert over.reason == Reason.RISK_PER_TRADE


def test_regression_jitter_within_rel_tol_of_whole_lot() -> None:
    # Hypothesis falsifying example (seeds 6, 7): equity=99999.9375,
    # price=10.0, dist_frac=0.001, offset=0, side=buy, jitter=1e-06.
    # jq = 100000.036999937 is 100000037 lots minus 6.3e-5 lot: within
    # 1e-12 relative, so it is a whole-lot qty by definition, and the gate
    # correctly rejects it on the exact qty as risk_per_trade.
    o, s, q, _ = _scenario(99999.9375, 10.0, 0.001, 0, "buy", 0, 0.0, 0.0, 0.0)
    jq = q * (1 + 1e-06)
    assert is_lot_multiple(jq, LOT)
    d = check(replace(o, qty=jq), s)
    assert (d.allowed, d.reason) == (False, Reason.RISK_PER_TRADE)
    # Just outside the tolerance at the same scale: lot-multiple rejection
    # fires before any risk check.
    off = 100000037 * LOT + 0.5 * LOT
    d2 = check(replace(o, qty=off), s)
    assert (d2.allowed, d2.reason) == (False, Reason.QTY_NOT_LOT_MULTIPLE)
    off2 = 100000037 * LOT * (1 + 5e-12)
    d3 = check(replace(o, qty=off2), s)
    assert (d3.allowed, d3.reason) == (False, Reason.QTY_NOT_LOT_MULTIPLE)
