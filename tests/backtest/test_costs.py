import pytest

from agenkit_trader.backtest.costs import CostModel


def test_round_trip_cost_is_two_sides_of_all_components():
    c = CostModel(
        taker_fee=0.0004, maker_fee=0.0002, half_spread_bps=1.0, slippage_bps=2.0
    )
    per_side = 0.0004 + 1.0 / 1e4 + 2.0 / 1e4
    assert c.round_trip_cost(1000.0) == pytest.approx(2 * 1000.0 * per_side)


def test_scaled_multiplies_every_component():
    c = CostModel(0.0004, 0.0002, 1.0, 2.0).scaled(2.0)
    assert (c.taker_fee, c.maker_fee, c.half_spread_bps, c.slippage_bps) == (
        0.0008,
        0.0004,
        2.0,
        4.0,
    )


def test_zero_scale_gives_zero_cost():
    assert CostModel(0.0004, 0.0002, 1.0, 2.0).scaled(0.0).round_trip_cost(1e6) == 0.0


@pytest.mark.parametrize("bad", [-0.1, float("nan"), float("inf")])
def test_invalid_parameters_rejected(bad):
    with pytest.raises(ValueError):
        CostModel(bad, 0.0, 0.0, 0.0)
    with pytest.raises(ValueError):
        CostModel(0.0, 0.0, bad, 0.0)
    with pytest.raises(ValueError):
        CostModel(0.0, 0.0, 0.0, 0.0).scaled(bad)
