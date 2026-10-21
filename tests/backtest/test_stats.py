import math

import pandas as pd
import pytest

from agenkit_trader.backtest.stats import compute_stats

EMPTY = pd.DataFrame(columns=["entry_time", "exit_time", "position", "n_bars", "ret"])


def _eq(vals):
    return pd.Series(
        vals, index=pd.date_range("2024-01-01", periods=len(vals), freq="1h", tz="UTC")
    )


def test_stats_zero_trades():
    s = compute_stats(_eq([100.0] * 10), EMPTY, periods_per_year=8760)
    assert s.n_trades == 0
    assert s.profit_factor == 0.0
    assert s.sharpe == 0.0
    assert s.win_rate == 0.0
    assert s.max_drawdown == 0.0
    assert s.total_return == 0.0
    assert all(not math.isnan(v) for v in vars(s).values())


def test_stats_single_bar_equity_no_exception():
    s = compute_stats(_eq([100.0]), EMPTY, 8760)
    assert s.sharpe == 0.0 and s.n_trades == 0


def test_stats_values():
    eq = _eq([100.0, 110.0, 99.0, 108.9])
    trades = pd.DataFrame({"ret": [0.1, -0.1, 0.1]})
    s = compute_stats(eq, trades, periods_per_year=100)
    assert s.n_trades == 3
    assert s.win_rate == pytest.approx(2 / 3)
    assert s.profit_factor == pytest.approx(0.2 / 0.1)
    assert s.max_drawdown == pytest.approx(0.1)  # 110 -> 99
    assert s.total_return == pytest.approx(0.089)
    r = eq.pct_change().dropna()
    assert s.sharpe == pytest.approx(r.mean() / r.std(ddof=1) * 10)


def test_zero_volatility_with_trades_has_zero_sharpe():
    s = compute_stats(_eq([100.0, 100.0, 100.0]), pd.DataFrame({"ret": [0.0]}), 100)
    assert s.sharpe == 0.0 and s.n_trades == 1 and s.profit_factor == 0.0


def test_no_losses_profit_factor_is_inf():
    s = compute_stats(_eq([100.0, 101.0]), pd.DataFrame({"ret": [0.01]}), 100)
    assert s.profit_factor == math.inf


def test_invalid_equity_rejected():
    with pytest.raises(ValueError):
        compute_stats(_eq([100.0, float("nan")]), EMPTY, 100)
    with pytest.raises(ValueError):
        compute_stats(_eq([100.0, 0.0]), EMPTY, 100)
    with pytest.raises(ValueError):
        compute_stats(_eq([]), EMPTY, 100)
