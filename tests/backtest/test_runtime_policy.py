"""Runtime-policy simulator: sign-only signal, 1% risk at an ATR stop,
constant quantity, intrabar exchange stop, no re-entry without a transition."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from agenkit_trader.backtest.costs import CostModel
from agenkit_trader.backtest.engine import run_backtest
from agenkit_trader.backtest.runtime_policy import run_backtest_runtime
from agenkit_trader.config import RiskConfig, RuntimeConfig
from agenkit_trader.data.archive import DataGapError
from agenkit_trader.execution.venue import SymbolRules
from agenkit_trader.risk.sizing import size_position

FREE = CostModel(0.0, 0.0, 0.0, 0.0)
RISK = RiskConfig()
RT = RuntimeConfig()
RULES = SymbolRules(tick=0.1, lot=0.001, min_notional=5.0)
QTY = 10.0 / 6.0  # 1% of 1000 at a 3 x ATR(=2) stop distance


def flat_bars(n: int = 60, half_range: float = 1.0) -> pd.DataFrame:
    """Close 100 everywhere, high/low +-half_range: ATR = 2*half_range."""
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    c = np.full(n, 100.0)
    return pd.DataFrame(
        {
            "open": c,
            "high": c + half_range,
            "low": c - half_range,
            "close": c.copy(),
            "volume": 1.0,
        },
        index=idx,
    )


def sig(bars: pd.DataFrame, spans: dict[tuple[int, int], float]) -> pd.Series:
    """Signal ``v`` on bars ``a..b-1`` for each ``(a, b): v``; 0 elsewhere."""
    s = pd.Series(0.0, index=bars.index)
    for (a, b), v in spans.items():
        s.iloc[a:b] = v
    return s


def run(
    bars: pd.DataFrame,
    s: pd.Series,
    costs: CostModel = FREE,
    funding: pd.Series | None = None,
    risk: RiskConfig = RISK,
    rules: SymbolRules | None = None,
    **kw: object,
):  # type: ignore[no-untyped-def]
    return run_backtest_runtime(bars, s, costs, funding, 1000.0, risk, RT, rules, **kw)


# ---------------------------------------------------------------- sizing


def test_sizes_for_one_percent_risk_at_the_atr_stop() -> None:
    b = flat_bars()
    res = run(b, sig(b, {(20, 30): 1.0}))
    t = res.trades.iloc[0]
    assert t["side"] == 1.0
    assert t["stop_price"] == pytest.approx(94.0)
    assert t["qty"] == size_position(1000.0, 100.0, t["stop_price"], RISK)
    assert t["qty"] * (100.0 - t["stop_price"]) <= 10.0 + 1e-9
    assert t["entry_time"] == b.index[21]  # first bar the position is held
    assert t["exit_reason"] == "signal"
    assert t["exit_time"] == b.index[30] + pd.Timedelta(hours=1)


def test_signal_magnitude_is_ignored_only_sign_counts() -> None:
    b = flat_bars()
    a = run(b, sig(b, {(20, 30): 1.0}))
    h = run(b, sig(b, {(20, 30): 0.3}))
    assert a.trades.equals(h.trades)


def test_rules_floor_qty_to_lot_and_stop_to_tick() -> None:
    b = flat_bars()
    b.loc[b.index[20], "close"] = 100.03
    res = run(b, sig(b, {(20, 30): 1.0}), rules=RULES)
    t = res.trades.iloc[0]
    assert round(t["stop_price"] / 0.1, 9) == round(t["stop_price"] / 0.1)
    assert t["stop_price"] >= 100.03 - 3 * 2.0  # rounded toward the entry
    assert round(t["qty"] / 0.001, 6) == round(t["qty"] / 0.001)
    assert t["qty"] == size_position(1000.0, 100.03, t["stop_price"], RISK, rules=RULES)


def test_quantity_is_held_constant_while_equity_moves() -> None:
    b = flat_bars()
    b.iloc[21:30, b.columns.get_loc("close")] = 100.0 + np.arange(1, 10)
    b["high"] = b["close"] + 1
    b["low"] = b["close"] - 1
    b["open"] = b["close"]
    res = run(b, sig(b, {(20, 40): 1.0}))
    q = res.trades.iloc[0]["qty"]
    eq = res.equity.to_numpy()
    # each bar's P&L is qty * price change: no rebalancing to a fraction
    assert np.allclose(np.diff(eq[20:30]), q * 1.0)
    assert len(res.trades) == 1


# ---------------------------------------------------------------- stops


def test_intrabar_stop_fills_at_the_stop_price() -> None:
    b = flat_bars()
    b.loc[b.index[25], "low"] = 93.5
    res = run(b, sig(b, {(20, 40): 1.0}))
    t = res.trades.iloc[0]
    assert t["exit_reason"] == "stop" and t["exit_price"] == 94.0
    assert t["exit_time"] == b.index[25] + pd.Timedelta(hours=1)
    assert res.equity.iloc[25] == pytest.approx(1000.0 - QTY * 6.0)
    assert len(res.trades) == 1  # signal stays +1: no re-entry


def test_gap_through_stop_fills_at_the_worse_open() -> None:
    b = flat_bars()
    b.loc[b.index[25], ["open", "high", "low", "close"]] = [90.0, 91.0, 89.0, 90.0]
    res = run(b, sig(b, {(20, 40): 1.0}))
    t = res.trades.iloc[0]
    assert t["exit_reason"] == "stop" and t["exit_price"] == 90.0
    assert res.trades.iloc[0]["ret"] == pytest.approx(-QTY * 10.0 / 1000.0)


def test_short_stop_triggers_on_high_and_gaps_up() -> None:
    b = flat_bars()
    b.loc[b.index[25], "high"] = 106.5
    b.loc[b.index[35], ["open", "high", "low", "close"]] = [110.0, 111.0, 109.0, 110.0]
    res = run(b, sig(b, {(20, 30): -1.0, (32, 40): -1.0}))
    assert list(res.trades["exit_reason"]) == ["stop", "stop"]
    assert list(res.trades["exit_price"]) == [106.0, 110.0]


def test_stop_before_signal_exit_in_the_same_bar() -> None:
    b = flat_bars()
    b.loc[b.index[25], "low"] = 93.0
    res = run(b, sig(b, {(20, 25): 1.0}))  # signal goes 0 at close of bar 25
    assert len(res.trades) == 1
    t = res.trades.iloc[0]
    assert t["exit_reason"] == "stop" and t["exit_price"] == 94.0


def test_no_reentry_after_stop_until_fresh_transition() -> None:
    b = flat_bars(80)
    b.loc[b.index[25], "low"] = 93.0
    s = sig(b, {(20, 40): 1.0, (42, 50): 1.0})
    res = run(b, s)
    tr = res.trades
    assert len(tr) == 2
    assert tr.iloc[0]["exit_reason"] == "stop"
    assert tr.iloc[1]["entry_time"] == b.index[43]  # re-entered at bar 42's close


def test_flip_after_stop_enters_opposite_side() -> None:
    b = flat_bars(80)
    b.loc[b.index[25], "low"] = 93.0
    res = run(b, sig(b, {(20, 30): 1.0, (30, 40): -1.0}))
    tr = res.trades
    assert list(tr["side"]) == [1.0, -1.0]
    assert tr.iloc[1]["entry_time"] == b.index[31]


def test_flip_closes_then_opens() -> None:
    b = flat_bars(80)
    res = run(b, sig(b, {(20, 30): 1.0, (30, 40): -1.0}))
    tr = res.trades
    assert list(tr["exit_reason"]) == ["flip", "signal"]
    assert list(tr["side"]) == [1.0, -1.0]
    assert tr.iloc[0]["exit_time"] == tr.iloc[1]["entry_time"]


# ---------------------------------------------------------------- costs


def test_costs_on_entry_and_exit_legs() -> None:
    c = CostModel(0.0005, 0.0002, 1.0, 2.0)
    r = c.per_side_rate
    b = flat_bars()
    res = run(b, sig(b, {(20, 30): 1.0}), costs=c)
    q = res.trades.iloc[0]["qty"]
    assert res.equity.iloc[20] == 1000.0  # entry cost lands on the next bar
    assert res.equity.iloc[21] == pytest.approx(1000.0 - q * 100.0 * r)
    assert res.equity.iloc[-1] == pytest.approx(1000.0 - 2 * q * 100.0 * r)
    assert res.trades.iloc[0]["ret"] == pytest.approx(-2 * q * 100.0 * r / 1000.0)


def test_stop_leg_is_charged_at_the_fill_price() -> None:
    c = CostModel(0.0005, 0.0002, 1.0, 2.0)
    r = c.per_side_rate
    b = flat_bars()
    b.loc[b.index[25], ["open", "high", "low", "close"]] = [90.0, 91.0, 89.0, 90.0]
    res = run(b, sig(b, {(20, 40): 1.0}), costs=c)
    q = res.trades.iloc[0]["qty"]
    expect = 1000.0 - q * 100.0 * r - q * 10.0 - q * 90.0 * r
    assert res.equity.iloc[25] == pytest.approx(expect)


def test_flip_charges_both_legs() -> None:
    c = CostModel(0.001, 0.0, 0.0, 0.0)
    b = flat_bars(80)
    res = run(b, sig(b, {(20, 30): 1.0, (30, 40): -1.0}), costs=c)
    q1, q2 = res.trades["qty"]
    assert res.equity.iloc[31] == pytest.approx(
        1000.0 - q1 * 100 * 0.001 * 2 - q2 * 100 * 0.001
    )


def test_final_open_position_is_liquidated_and_charged() -> None:
    c = CostModel(0.001, 0.0, 0.0, 0.0)
    b = flat_bars()
    res = run(b, sig(b, {(20, 60): 1.0}), costs=c)
    t = res.trades.iloc[-1]
    assert t["exit_reason"] == "end"
    assert res.equity.iloc[-1] == pytest.approx(1000.0 - 2 * t["qty"] * 100 * 0.001)
    assert res.stats.total_return == pytest.approx(res.equity.iloc[-1] / 1000 - 1)


# ---------------------------------------------------------------- funding


def test_funding_paid_by_long_received_by_short_on_held_qty() -> None:
    b = flat_bars()
    f = pd.Series([0.001], index=[b.index[25] + pd.Timedelta(hours=1)])
    long_ = run(b, sig(b, {(20, 30): 1.0}), funding=f)
    short = run(b, sig(b, {(20, 30): -1.0}), funding=f)
    q = long_.trades.iloc[0]["qty"]
    assert long_.equity.iloc[-1] == pytest.approx(1000.0 - q * 100.0 * 0.001)
    assert short.equity.iloc[-1] == pytest.approx(1000.0 + q * 100.0 * 0.001)


def test_no_funding_outside_the_holding_window_or_after_stop() -> None:
    b = flat_bars()
    b.loc[b.index[25], "low"] = 93.0
    f = pd.Series(
        [0.01, 0.01],
        index=[
            b.index[10] + pd.Timedelta(hours=1),
            b.index[25] + pd.Timedelta(hours=1),
        ],
    )
    res = run(b, sig(b, {(20, 40): 1.0}), funding=f)
    assert res.equity.iloc[-1] == pytest.approx(1000.0 - QTY * 6.0)


# ---------------------------------------------------------------- ruin, skips


def test_ruin_clips_equity_and_stops_trading() -> None:
    b = flat_bars()
    b.loc[b.index[25], ["open", "high", "low", "close"]] = [80.0, 81.0, 79.0, 80.0]
    risky = RiskConfig(
        max_risk_per_trade=1.0, max_leverage=1000.0, max_symbol_notional_frac=1000.0
    )
    res = run(b, sig(b, {(20, 25): 1.0, (30, 40): 1.0}), risk=risky)
    assert res.ruined
    assert (res.equity.iloc[25:] == 0.0).all()
    assert len(res.trades) == 1
    assert res.stats.total_return == -1.0


def test_entry_rejected_by_leverage_limit_is_skipped_not_retried() -> None:
    b = flat_bars(half_range=0.05)  # ATR 0.1 -> qty 33 -> 3333 notional
    res = run(b, sig(b, {(20, 40): 1.0}))
    assert res.stats.n_trades == 0
    assert res.skipped_entries == 1


def test_skipped_entry_with_rules_uses_the_real_gate() -> None:
    b = flat_bars(half_range=0.05)
    res = run(b, sig(b, {(20, 30): 1.0, (35, 40): -1.0}), rules=RULES)
    assert res.stats.n_trades == 0
    assert res.skipped_entries == 2


def test_entry_without_atr_history_is_skipped() -> None:
    b = flat_bars()
    res = run(b, sig(b, {(5, 30): 1.0}))
    assert res.skipped_entries == 1 and res.stats.n_trades == 0


def test_warmup_bars_provide_atr_history() -> None:
    b = flat_bars(80)
    warm, rest = b.iloc[:40], b.iloc[40:]
    res = run(rest, sig(rest, {(2, 10): 1.0}), warmup=warm)
    assert res.stats.n_trades == 1 and res.skipped_entries == 0
    assert res.equity.index.equals(rest.index)


def test_zero_trades() -> None:
    b = flat_bars()
    res = run(b, sig(b, {}))
    assert res.stats.n_trades == 0
    assert res.stats.profit_factor == 0.0 and res.stats.sharpe == 0.0
    assert (res.equity == 1000.0).all() and res.skipped_entries == 0


def test_run_backtest_reports_no_skipped_entries() -> None:
    b = flat_bars()
    assert run_backtest(b, sig(b, {}), FREE, None, 1000.0).skipped_entries == 0


# ---------------------------------------------------------------- inputs


def test_rejects_bad_inputs() -> None:
    b = flat_bars()
    s = sig(b, {(20, 30): 1.0})
    bad = s.copy()
    bad.iloc[3] = math.nan
    with pytest.raises(ValueError):
        run(b, bad)
    bad.iloc[3] = math.inf
    with pytest.raises(ValueError):
        run(b, bad)
    b2 = b.copy()
    b2.iloc[4, b2.columns.get_loc("low")] = math.nan
    with pytest.raises(ValueError):
        run(b2, s)
    with pytest.raises(DataGapError):  # same as run_backtest
        run(b.drop(b.index[10]), s.drop(s.index[10]))
    with pytest.raises(ValueError):
        run(b, s.iloc[1:])
    with pytest.raises(ValueError):
        run_backtest_runtime(b, s, FREE, None, 0.0, RISK, RT)


# ---------------------------------------------------------------- properties


def _random(seed: int, n: int = 300) -> tuple[pd.DataFrame, pd.Series]:
    rng = np.random.default_rng(seed)
    close = 100.0 * np.cumprod(1.0 + rng.normal(0, 0.01, n))
    op = np.r_[100.0, close[:-1]] * (1 + rng.normal(0, 0.003, n))
    hi = np.maximum(op, close) * (1 + np.abs(rng.normal(0, 0.006, n)))
    lo = np.minimum(op, close) * (1 - np.abs(rng.normal(0, 0.006, n)))
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    b = pd.DataFrame(
        {"open": op, "high": hi, "low": lo, "close": close, "volume": 1.0}, index=idx
    )
    raw = rng.choice([-1.0, 0.0, 1.0], size=n, p=[0.3, 0.4, 0.3])
    s = pd.Series(raw, index=idx).rolling(5, min_periods=1).median().round()
    return b, s


def test_no_lookahead_truncation() -> None:
    b, s = _random(3)
    c = CostModel(0.0005, 0.0002, 1.0, 2.0)
    f = pd.Series(0.0001, index=b.index[::8] + pd.Timedelta(hours=1))
    full = run(b, s, costs=c, funding=f)
    assert full.stats.n_trades > 5
    for t in range(20, len(b), 17):
        part = run(b.iloc[: t + 1], s.iloc[: t + 1], costs=c, funding=f)
        # bars before the truncated final bar (its liquidation) are unchanged
        assert np.array_equal(part.equity.iloc[:t].to_numpy(), full.equity.iloc[:t])
        cut = b.index[t]
        done_p = part.trades[part.trades["exit_time"] <= cut]
        done_f = full.trades[full.trades["exit_time"] <= cut]
        pd.testing.assert_frame_equal(
            done_p.reset_index(drop=True), done_f.reset_index(drop=True)
        )


def test_deterministic() -> None:
    b, s = _random(5)
    a, z = run(b, s), run(b, s)
    assert a.equity.equals(z.equity) and a.trades.equals(z.trades)
    assert a.stats == z.stats and a.skipped_entries == z.skipped_entries
