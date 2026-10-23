import math

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from agenkit_trader.config import RiskConfig
from agenkit_trader.execution.venue import SymbolRules
from agenkit_trader.risk.sizing import size_position

CFG = RiskConfig()
RULES = SymbolRules(tick=0.1, lot=0.001, min_notional=5.0)


def test_default_risks_at_most_10_usdt() -> None:
    q = size_position(1000.0, 50_000.0, 49_500.0, CFG)
    assert q * 500.0 <= 10.0 + 1e-9
    assert q == pytest.approx(0.02)


def test_short_side_symmetric() -> None:
    assert size_position(1000.0, 50_000.0, 50_500.0, CFG) == pytest.approx(0.02)


def test_kelly_input_of_one_is_treated_as_cap() -> None:
    cfg = RiskConfig(max_risk_per_trade=1.0)
    full = size_position(1000.0, 100.0, 90.0, cfg, kelly_fraction=1.0)
    capped = size_position(1000.0, 100.0, 90.0, cfg, kelly_fraction=0.25)
    assert full == capped == pytest.approx(25.0)


def test_kelly_smaller_than_max_risk_reduces_size() -> None:
    q = size_position(1000.0, 100.0, 90.0, CFG, kelly_fraction=0.005)
    assert q == pytest.approx(0.5)


@pytest.mark.parametrize("k", [0.0, -0.3, math.nan, math.inf])
def test_nonpositive_or_bad_kelly_gives_zero(k: float) -> None:
    assert size_position(1000.0, 100.0, 90.0, CFG, kelly_fraction=k) == 0.0


def test_rounds_down_to_lot() -> None:
    q = size_position(1000.0, 30_000.0, 29_700.0, CFG, rules=RULES)
    # raw 10/300 = 0.0333.. -> 0.033
    assert q == pytest.approx(0.033)


def test_below_min_notional_gives_zero() -> None:
    rules = SymbolRules(tick=0.1, lot=0.001, min_notional=2000.0)
    assert size_position(1000.0, 50_000.0, 49_500.0, CFG, rules=rules) == 0.0


@pytest.mark.parametrize(
    "equity,entry,stop",
    [
        (0.0, 100.0, 90.0),
        (-1.0, 100.0, 90.0),
        (math.nan, 100.0, 90.0),
        (1000.0, math.nan, 90.0),
        (1000.0, 100.0, math.inf),
        (1000.0, 100.0, 100.0),
        (1000.0, -100.0, 90.0),
    ],
)
def test_degenerate_inputs_give_zero(equity: float, entry: float, stop: float) -> None:
    assert size_position(equity, entry, stop, CFG) == 0.0


@settings(max_examples=500, deadline=None)
@given(
    equity=st.floats(1e-3, 1e9),
    entry=st.floats(1e-6, 1e7),
    stop=st.floats(1e-6, 1e7),
    kelly=st.one_of(st.none(), st.floats(-1.0, 5.0)),
    lot=st.one_of(st.none(), st.floats(1e-6, 10.0)),
)
def test_size_never_exceeds_risk_budget(
    equity: float, entry: float, stop: float, kelly: float | None, lot: float | None
) -> None:
    rules = SymbolRules(tick=0.01, lot=lot, min_notional=0.0) if lot else None
    q = size_position(equity, entry, stop, CFG, kelly_fraction=kelly, rules=rules)
    assert q >= 0.0
    eps = 1e-9 * max(1.0, equity * CFG.max_risk_per_trade)
    assert q * abs(entry - stop) <= equity * CFG.max_risk_per_trade + eps


def test_lot_tolerance_is_symmetric() -> None:
    from agenkit_trader.risk.sizing import floor_to_lot, is_lot_multiple

    lot = 0.001
    for rel in (5e-13, -5e-13):
        q = lot * (1 + rel)
        assert is_lot_multiple(q, lot)
        assert floor_to_lot(q, lot) == lot
    for rel in (5e-10, -5e-10, 1e-6, -1e-6):
        q = lot * (1 + rel)
        assert not is_lot_multiple(q, lot)
        f = floor_to_lot(q, lot)
        assert f <= q
        assert f == (lot if rel > 0 else 0.0)
    assert is_lot_multiple(0.011, lot)
    assert floor_to_lot(0.011, lot) == 11 * lot
    for q in (0.011, 0.0119, 7 * lot, 123.456):
        f = floor_to_lot(q, lot)
        assert f <= q * (1 + 1e-12)
        if f > 0:
            assert is_lot_multiple(f, lot)
            assert floor_to_lot(f, lot) == f
