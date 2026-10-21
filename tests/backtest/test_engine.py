import numpy as np
import pandas as pd
import pytest
from helpers import make_bars

from agenkit_trader.backtest.costs import CostModel
from agenkit_trader.backtest.engine import run_backtest
from agenkit_trader.data.archive import DataGapError

ZERO = CostModel(0.0, 0.0, 0.0, 0.0)


def pos(bars, vals):
    return pd.Series(vals, index=bars.index, dtype="float64")


def test_flat_price_zero_position_constant_equity():
    bars = make_bars([100.0] * 6)
    r = run_backtest(
        bars, pos(bars, [0] * 6), CostModel(0.0004, 0.0002, 1, 2), None, 1000.0
    )
    assert (r.equity == 1000.0).all()
    assert (
        r.stats.n_trades == 0 and r.stats.profit_factor == 0.0 and r.stats.sharpe == 0.0
    )
    assert r.equity.index.equals(bars.index)


def test_long_on_known_one_percent_bar_zero_costs_exact():
    bars = make_bars([100.0, 100.0, 101.0, 101.0])
    # decided at close of bar 1, earns close1 -> close2 (+1%)
    r = run_backtest(bars, pos(bars, [0, 1, 0, 0]), ZERO, None, 1000.0)
    assert r.equity.iloc[2] == pytest.approx(1010.0, rel=1e-12)
    assert r.equity.iloc[-1] == pytest.approx(1010.0, rel=1e-12)
    assert r.stats.total_return == pytest.approx(0.01)
    assert r.stats.n_trades == 1 and r.stats.win_rate == 1.0


def test_costs_reduce_return_by_round_trip_cost():
    bars = make_bars([100.0, 100.0, 101.0, 101.0, 101.0])
    costs = CostModel(0.0004, 0.0002, 1.0, 2.0)
    r = run_backtest(bars, pos(bars, [0, 1, 0, 0, 0]), costs, None, 1000.0)
    per_side = costs.round_trip_cost(1.0) / 2
    # bar return and entry cost are additive on start-of-bar equity; exit cost next bar
    expected = 1000.0 * (1 + 0.01 - per_side) * (1 - per_side)
    assert r.equity.iloc[-1] == pytest.approx(expected, rel=1e-12)
    assert r.equity.iloc[-1] < 1010.0
    # on a flat price the loss is exactly the round trip cost on notional
    flat = make_bars([100.0] * 5)
    r2 = run_backtest(flat, pos(flat, [0, 1, 0, 0, 0]), costs, None, 1000.0)
    assert 1000.0 - r2.equity.iloc[-1] == pytest.approx(
        costs.round_trip_cost(1000.0), rel=5e-3
    )


def test_no_same_bar_fill_buying_the_days_max_cannot_profit():
    rng = np.random.default_rng(0)
    closes = 100 * np.cumprod(1 + rng.normal(0, 0.01, 240))
    bars = make_bars(list(closes))
    day = pd.Series(closes, index=bars.index).groupby(bars.index.date)
    is_max = (pd.Series(closes, index=bars.index) == day.transform("max")).astype(float)
    # the "oracle" is long exactly on bars whose close is the day's max; it only
    # earns the NEXT bar's return, (weakly) negative after a max within the day.
    r = run_backtest(bars, is_max, ZERO, None, 1000.0)
    nxt = pd.Series(closes, index=bars.index).pct_change().shift(-1).fillna(0.0)
    # within-day: next bar after a day's max cannot be higher (except across midnight)
    same_day_next = bars.index.to_series().shift(-1).dt.date == bars.index.date
    mask = (is_max == 1.0) & same_day_next
    assert (nxt[mask] <= 0).all()
    assert r.stats.total_return <= 0.02  # only midnight crossings can contribute


def test_return_cannot_depend_on_same_bar_close():
    bars = make_bars([100.0, 100.0, 150.0])
    # position set on the jump bar (index 2) never earns the jump
    r = run_backtest(bars, pos(bars, [0, 0, 1]), ZERO, None, 1000.0)
    assert r.equity.iloc[-1] == 1000.0


def test_funding_long_pays_positive_funding():
    bars = make_bars([100.0] * 5)  # 1h bars, opens 00:00 .. 04:00
    # 02:00 lies in (01:00, 02:00] = bar 1's window; eff position there is pos[0]
    f = pd.Series([0.001], index=pd.DatetimeIndex(["2024-01-01 02:00"], tz="UTC"))
    r = run_backtest(bars, pos(bars, [1, 1, 1, 1, 1]), ZERO, f, 1000.0)
    assert r.equity.iloc[0] == 1000.0
    assert r.equity.iloc[1] == pytest.approx(999.0, rel=1e-12)
    assert r.equity.iloc[-1] == pytest.approx(1000.0 * (1 - 0.001), rel=1e-12)


def test_funding_short_receives_positive_and_long_receives_negative():
    bars = make_bars([100.0] * 4)
    idx = pd.DatetimeIndex(["2024-01-01 02:00"], tz="UTC")
    s = run_backtest(
        bars, pos(bars, [-1] * 4), ZERO, pd.Series([0.001], index=idx), 1000.0
    )
    assert s.equity.iloc[-1] == pytest.approx(1001.0)
    n = run_backtest(
        bars, pos(bars, [1] * 4), ZERO, pd.Series([-0.001], index=idx), 1000.0
    )
    assert n.equity.iloc[-1] == pytest.approx(1001.0)


def test_funding_only_paid_when_position_held_in_window():
    bars = make_bars([100.0] * 5)
    # 01:00 is in bar 0's window (00:00, 01:00]; effective position there is 0
    f = pd.Series([0.01], index=pd.DatetimeIndex(["2024-01-01 01:00"], tz="UTC"))
    r = run_backtest(bars, pos(bars, [1, 1, 1, 1, 1]), ZERO, f, 1000.0)
    assert r.equity.iloc[-1] == 1000.0
    # held in bars 1 and 2 (windows (01:00,02:00], (02:00,03:00]): 03:00 is paid
    f2 = pd.Series([0.01], index=pd.DatetimeIndex(["2024-01-01 03:00"], tz="UTC"))
    r2 = run_backtest(bars, pos(bars, [1, 1, 0, 0, 0]), ZERO, f2, 1000.0)
    assert r2.equity.iloc[-1] == pytest.approx(990.0)
    # 04:00 is in bar 3's window where the position is already flat
    f3 = pd.Series([0.01], index=pd.DatetimeIndex(["2024-01-01 04:00"], tz="UTC"))
    r3 = run_backtest(bars, pos(bars, [1, 1, 0, 0, 0]), ZERO, f3, 1000.0)
    assert r3.equity.iloc[-1] == 1000.0


def test_funding_none_equals_zero_funding():
    bars = make_bars([100.0, 101.0, 102.0, 101.0])
    p = pos(bars, [1, 1, -1, 0])
    a = run_backtest(bars, p, CostModel(0.0004, 0.0, 1, 1), None, 1000.0)
    z = pd.Series([0.0], index=pd.DatetimeIndex(["2024-01-01 02:00"], tz="UTC"))
    b = run_backtest(bars, p, CostModel(0.0004, 0.0, 1, 1), z, 1000.0)
    pd.testing.assert_series_equal(a.equity, b.equity)


def test_trades_are_runs_of_constant_nonzero_position():
    bars = make_bars([100.0, 101.0, 102.0, 103.0, 102.0, 101.0, 100.0, 100.0])
    r = run_backtest(bars, pos(bars, [0, 1, 1, 0, -1, -1, 0, 0]), ZERO, None, 1000.0)
    t = r.trades
    assert list(t["position"]) == [1.0, -1.0]
    assert list(t["n_bars"]) == [2, 2]
    # long earns close1->close3: 103/101-1 ; short earns close4->close6
    assert t["ret"].iloc[0] == pytest.approx(103 / 101 - 1)
    assert t["ret"].iloc[1] == pytest.approx(
        (1 - (101 / 102 - 1)) * (1 - (100 / 101 - 1)) - 1
    )
    assert r.stats.n_trades == 2


def test_trade_return_includes_entry_and_exit_costs():
    bars = make_bars([100.0] * 5)
    c = CostModel(0.001, 0.0, 0.0, 0.0)
    r = run_backtest(bars, pos(bars, [0, 1, 0, 0, 0]), c, None, 1000.0)
    assert r.trades["ret"].iloc[0] == pytest.approx((1 - 0.001) ** 2 - 1)


def test_open_trade_at_end_is_reported_without_exit_cost():
    bars = make_bars([100.0] * 4)
    c = CostModel(0.001, 0.0, 0.0, 0.0)
    r = run_backtest(bars, pos(bars, [0, 1, 1, 1]), c, None, 1000.0)
    assert len(r.trades) == 1
    assert r.trades["ret"].iloc[0] == pytest.approx(-0.001)


def test_flip_charges_both_legs():
    bars = make_bars([100.0] * 4)
    c = CostModel(0.001, 0.0, 0.0, 0.0)
    r = run_backtest(bars, pos(bars, [1, -1, -1, -1]), c, None, 1000.0)
    # bar0 eff 0; bar1 eff +1 (entry cost 0.001); bar2 eff -1 (turnover 2 -> 0.002)
    assert r.equity.iloc[-1] == pytest.approx(1000 * (1 - 0.001) * (1 - 0.002))


def test_nan_positions_raise():
    bars = make_bars([100.0] * 4)
    with pytest.raises(ValueError, match="NaN"):
        run_backtest(bars, pos(bars, [0, np.nan, 0, 0]), ZERO, None, 1000.0)


@pytest.mark.parametrize("bad", [1.5, -1.0001])
def test_position_beyond_unit_leverage_rejected(bad):
    bars = make_bars([100.0] * 4)
    with pytest.raises(ValueError, match="position"):
        run_backtest(bars, pos(bars, [0, bad, 0, 0]), ZERO, None, 1000.0)


def test_non_uniform_index_raises():
    bars = make_bars([100.0] * 5).drop(pd.Timestamp("2024-01-01 02:00", tz="UTC"))
    with pytest.raises(DataGapError):
        run_backtest(bars, pos(bars, [0] * 4), ZERO, None, 1000.0)


def test_position_index_mismatch_raises():
    bars = make_bars([100.0] * 4)
    p = pd.Series([0.0] * 4, index=bars.index + pd.Timedelta(hours=1))
    with pytest.raises(ValueError, match="index"):
        run_backtest(bars, p, ZERO, None, 1000.0)


def test_bad_equity_and_prices_raise():
    bars = make_bars([100.0] * 4)
    with pytest.raises(ValueError):
        run_backtest(bars, pos(bars, [0] * 4), ZERO, None, 0.0)
    bad = bars.copy()
    bad.iloc[2, bad.columns.get_loc("close")] = np.nan
    with pytest.raises(ValueError):
        run_backtest(bad, pos(bad, [0] * 4), ZERO, None, 1000.0)


def test_inputs_not_mutated():
    bars = make_bars([100.0, 101.0, 99.0, 100.0])
    p = pos(bars, [0, 1, -1, 0])
    b0, p0 = bars.copy(), p.copy()
    run_backtest(bars, p, CostModel(0.0004, 0, 1, 1), None, 1000.0)
    pd.testing.assert_frame_equal(bars, b0)
    pd.testing.assert_series_equal(p, p0)
