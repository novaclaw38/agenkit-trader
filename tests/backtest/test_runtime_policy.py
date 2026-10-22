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
        max_risk_per_trade=1.0,
        max_leverage=1000.0,
        max_symbol_notional_frac=1000.0,
        max_daily_loss=1.0,  # kill switch off: this test is about ruin
        max_drawdown=1.0,
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


# ---------------------------------------------------------------- kill switch


def test_daily_loss_halt_after_stop_fill_is_permanent() -> None:
    b = flat_bars(80)
    b.loc[b.index[22], ["open", "high", "low", "close"]] = [60.0, 61.0, 59.0, 60.0]
    res = run(b, sig(b, {(20, 30): 1.0, (40, 50): 1.0, (60, 70): -1.0}))
    assert res.halted and res.halt_reason == "daily_loss"
    assert res.halt_time == b.index[22]
    assert list(res.trades["exit_reason"]) == ["stop"]  # flat already
    assert (res.equity.iloc[22:] == res.equity.iloc[22]).all()
    assert res.equity.iloc[22] == pytest.approx(1000.0 - QTY * 40.0)


def test_halt_at_close_flattens_with_costs() -> None:
    c = CostModel(0.001, 0.0, 0.0, 0.0)
    b = flat_bars()
    b.loc[b.index[22], ["low", "close"]] = [96.0, 96.0]  # low == close
    risk = RiskConfig(max_daily_loss=0.005, max_drawdown=1.0)
    res = run(b, sig(b, {(20, 40): 1.0}), costs=c, risk=risk)
    assert res.halted and res.halt_reason == "daily_loss"
    t = res.trades.iloc[-1]
    assert t["exit_reason"] == "halt" and t["exit_price"] == 96.0
    q = t["qty"]
    after = 1000.0 - q * 100 * 0.001 - q * 4.0 - q * 96.0 * 0.001
    assert res.equity.iloc[23] == pytest.approx(after)
    assert (res.equity.iloc[23:] == res.equity.iloc[23]).all()
    assert len(res.trades) == 1


def test_drawdown_halt_reason() -> None:
    b = flat_bars()
    b.loc[b.index[22], ["low", "close"]] = [95.0, 96.0]
    res = run(b, sig(b, {(20, 40): 1.0}), risk=RiskConfig(max_drawdown=0.005))
    assert res.halted and res.halt_reason == "drawdown"


def test_halt_threshold_is_at_or_above_the_limit() -> None:
    b = flat_bars()
    b.loc[b.index[22], ["low", "close"]] = [95.0, 96.0]
    loss = (1000.0 - (1000.0 + QTY * -5.0)) / 1000.0  # at the bar's low, 95
    at = run(b, sig(b, {(20, 40): 1.0}), risk=RiskConfig(max_daily_loss=loss))
    above = run(b, sig(b, {(20, 40): 1.0}), risk=RiskConfig(max_daily_loss=loss * 1.01))
    assert at.halted and not above.halted


def test_daily_loss_resets_at_the_utc_day_boundary() -> None:
    b = flat_bars(60)
    b.loc[b.index[22], ["low", "close"]] = [97.6, 97.6]
    b.loc[b.index[32], ["low", "close"]] = [97.6, 97.6]
    risk = RiskConfig(max_daily_loss=0.005, max_drawdown=1.0)
    # day 1: -0.4% ; day 2 (from the 00:00 close): another -0.4% -> no halt
    res = run(b, sig(b, {(20, 22): 1.0, (30, 32): 1.0}), risk=risk)
    assert not res.halted
    assert res.stats.n_trades == 2
    b2 = b.copy()
    b2.loc[b2.index[16], ["low", "close"]] = [97.6, 97.6]
    same_day = run(b2, sig(b2, {(14, 16): 1.0, (20, 22): 1.0}), risk=risk)
    assert same_day.halted and same_day.halt_reason == "daily_loss"


def test_research_engine_never_halts() -> None:
    b = flat_bars()
    r = run_backtest(b, sig(b, {}), FREE, None, 1000.0)
    assert not r.halted and r.halt_reason is None and r.halt_time is None


# ---------------------------------------------------------------- segments


def test_prev_signal_suppresses_entry_at_segment_start() -> None:
    b = flat_bars(80)
    warm, rest = b.iloc[:40], b.iloc[40:]
    s = sig(rest, {(0, 20): 1.0})
    fresh = run(rest, s, warmup=warm)
    cont = run(rest, s, warmup=warm, prev_signal=1.0)
    assert (
        fresh.stats.n_trades == 1
        and fresh.trades.iloc[0]["entry_time"] == rest.index[1]
    )
    assert cont.stats.n_trades == 0 and cont.skipped_entries == 0
    flip = run(rest, s, warmup=warm, prev_signal=-1.0)
    assert flip.stats.n_trades == 1


# ---------------------------------------------------------------- funding/stop


def test_intrabar_funding_is_paid_before_a_stop_in_the_same_bar() -> None:
    idx = pd.date_range("2024-01-01", periods=60, freq="4h", tz="UTC")
    b = flat_bars(60).set_axis(idx)
    b.loc[b.index[25], "low"] = 93.0
    mid = b.index[25] + pd.Timedelta(hours=2)  # funding inside bar 25
    f = pd.Series([0.01], index=[mid])
    res = run(b, sig(b, {(20, 40): 1.0}), funding=f)
    assert res.trades.iloc[0]["exit_reason"] == "stop"
    assert res.equity.iloc[-1] == pytest.approx(1000.0 - QTY * 6.0 - QTY * 100 * 0.01)


# ---------------------------------------------------------------- ATR


def test_precomputed_atr_equals_runtime_atr_on_every_window() -> None:
    from agenkit_trader.backtest.runtime_policy import atr_series
    from agenkit_trader.runtime.loop import atr

    for seed in range(3):
        b, _ = _random(seed, n=1600)
        a = atr_series(b, 14)
        for end in range(1, len(b) + 1, 7):
            want = atr(b.iloc[max(0, end - RT.bars_limit) : end], 14)
            got = a[end - 1]
            assert (math.isnan(want) and math.isnan(got)) or want == got, end


def test_rules_min_notional_skips_small_entries() -> None:
    b = flat_bars()
    big = SymbolRules(tick=0.1, lot=0.001, min_notional=500.0)  # 166 < 500
    res = run(b, sig(b, {(20, 30): 1.0}), rules=big)
    assert res.stats.n_trades == 0 and res.skipped_entries == 1


def test_stop_in_the_last_bar_of_a_utc_day_halts_on_that_day() -> None:
    # bar 23 (23:00-24:00) gaps through the stop; its close is the next UTC
    # day, where day-start equity resets. The runtime polls within the bar,
    # so it sees the loss on the old day and halts.
    b = flat_bars(60)
    b.loc[b.index[23], ["open", "high", "low", "close"]] = [60.0, 61.0, 59.0, 60.0]
    res = run(b, sig(b, {(20, 40): 1.0}), risk=RiskConfig(max_drawdown=1.0))
    assert res.halted and res.halt_reason == "daily_loss"
    assert res.halt_time == b.index[23]


# ------------------------------------------------- intrabar kill-switch check

LIMIT = RiskConfig(max_daily_loss=0.005, max_drawdown=1.0)


def test_dip_that_recovers_halts_at_the_adverse_low() -> None:
    """Close recovers to 100 (no loss at any close), the low marks -2.4%
    of notional = 0.4% of equity... enough with a 0.3% limit: halt at the low."""
    b = flat_bars()
    b.loc[b.index[22], ["low", "close"]] = [97.6, 100.0]
    risk = RiskConfig(max_daily_loss=0.003, max_drawdown=1.0)
    res = run(b, sig(b, {(20, 40): 1.0}), risk=risk)
    assert res.halted and res.halt_reason == "daily_loss"
    assert res.halt_time == b.index[22]
    t = res.trades.iloc[-1]
    assert t["exit_reason"] == "halt" and t["exit_price"] == 97.6
    assert (res.equity.iloc[22:] == res.equity.iloc[22]).all()
    assert res.equity.iloc[22] == pytest.approx(1000.0 - QTY * 2.4)
    # a limit just above the dip does not halt
    ok = run(b, sig(b, {(20, 40): 1.0}), risk=RiskConfig(max_daily_loss=0.0041))
    assert not ok.halted


def test_short_is_marked_at_the_bar_high() -> None:
    b = flat_bars()
    b.loc[b.index[22], ["high", "close"]] = [102.4, 100.0]
    res = run(b, sig(b, {(20, 40): -1.0}), risk=RiskConfig(max_daily_loss=0.003))
    assert res.halted and res.trades.iloc[-1]["exit_price"] == 102.4
    assert res.trades.iloc[-1]["exit_reason"] == "halt"


def test_intrabar_halt_never_marks_the_favourable_extreme() -> None:
    b = flat_bars()
    b.loc[b.index[22], ["high", "close"]] = [110.0, 100.0]  # long: up-spike
    res = run(b, sig(b, {(20, 40): 1.0}), risk=RiskConfig(max_daily_loss=0.003))
    assert not res.halted


def test_stop_fill_replaces_the_adverse_extreme() -> None:
    """The bar's low is far below the stop, but the position was stopped at
    the stop price: the loss that is checked is the stop fill's."""
    b = flat_bars()
    b.loc[b.index[22], ["low", "close"]] = [50.0, 50.0]
    risk = RiskConfig(max_daily_loss=0.0101)  # stop loses exactly 1% of equity
    res = run(b, sig(b, {(20, 40): 1.0}), risk=risk)
    assert list(res.trades["exit_reason"]) == ["stop"]
    assert not res.halted
