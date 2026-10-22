import dataclasses
import math

import pandas as pd
from vsynth import (
    FeatureStrategy,
    PeekingStrategy,
    SpikeStrategy,
    TrainOnlyStrategy,
    costs,
    gate_cfg,
    market,
)

from agenkit_trader.backtest.stats import Stats
from agenkit_trader.data.lookahead import assert_no_lookahead
from agenkit_trader.validation.gate import (
    allowed_regimes,
    evaluate,
    holdout_reasons,
    label_regimes,
    passes_cost_check,
)
from agenkit_trader.validation.split import split_by_time
from agenkit_trader.validation.trials import TrialCounter


def _stats(ret: float, sharpe: float, trades: int, pf: float = 1.5) -> Stats:
    return Stats(sharpe, 0.05, 0.5, pf, trades, ret)


def test_zero_trade_holdout_rejected(tmp_path) -> None:
    bars, aux = market(3)
    train, hold = split_by_time(bars, gate_cfg().holdout_frac)
    s = TrainOnlyStrategy(cutoff=hold.index[0])
    v = evaluate(s, bars, aux, costs(), gate_cfg(), TrialCounter(tmp_path / "t.json"))
    assert not v.passed
    assert v.holdout.n_trades == 0
    assert any("zero trades" in r for r in v.reasons)


def test_spike_rejected_as_unstable(tmp_path) -> None:
    bars, aux = market(2, n_noise=5)
    v = evaluate(
        SpikeStrategy(), bars, aux, costs(), gate_cfg(), TrialCounter(tmp_path / "t")
    )
    assert not v.passed
    assert any("unstable" in r for r in v.reasons)


def test_lookahead_strategy_rejected(tmp_path) -> None:
    bars, aux = market(4)
    v = evaluate(
        PeekingStrategy(), bars, aux, costs(), gate_cfg(), TrialCounter(tmp_path / "t")
    )
    assert not v.passed
    assert any("lookahead" in r for r in v.reasons)


def test_counter_counts_every_grid_point(tmp_path) -> None:
    bars, aux = market(1)
    c = TrialCounter(tmp_path / "t.json")
    c.add(100)
    v = evaluate(FeatureStrategy(), bars, aux, costs(), gate_cfg(), c)
    assert c.total == 105
    assert v.n_trials == 105


def test_selection_uses_train_only(tmp_path) -> None:
    """Changing hold-out prices must not change the chosen parameters."""
    bars, aux = market(1)
    _, hold = split_by_time(bars, gate_cfg().holdout_frac)
    tampered = bars.copy()
    tampered.loc[hold.index, "close"] = tampered.loc[hold.index, "close"][::-1].values
    v1 = evaluate(
        FeatureStrategy(), bars, aux, costs(), gate_cfg(), TrialCounter(tmp_path / "a")
    )
    v2 = evaluate(
        FeatureStrategy(),
        tampered,
        aux,
        costs(),
        gate_cfg(),
        TrialCounter(tmp_path / "b"),
    )
    assert v1.params == v2.params
    pd.testing.assert_frame_equal(v1.sweep.table, v2.sweep.table)
    assert v1.train == v2.train


def test_cost_check_needs_positive_return_and_pf_above_one() -> None:
    assert passes_cost_check(_stats(0.01, 1.0, 50, pf=1.1), ruined=False)
    assert not passes_cost_check(_stats(-0.01, 1.0, 50, pf=1.1), ruined=False)
    assert not passes_cost_check(_stats(0.01, 1.0, 50, pf=1.0), ruined=False)
    assert passes_cost_check(_stats(0.01, 1.0, 50, pf=math.inf), ruined=False)
    assert not passes_cost_check(_stats(0.01, 1.0, 50, pf=math.nan), ruined=False)
    assert not passes_cost_check(_stats(0.01, 1.0, 50), ruined=True)


def test_regime_labeller_has_no_lookahead() -> None:
    bars, _ = market(5, n=600)
    w = gate_cfg().regime_window

    def lab_fn(b: pd.DataFrame) -> pd.Series:
        return label_regimes(b, w)

    assert_no_lookahead(lab_fn, bars, sample=40)
    lab = lab_fn(bars)
    assert set(lab.dropna().unique()) <= {0.0, 1.0}
    assert lab.dropna().nunique() == 2


def test_regime_labeller_sees_trend() -> None:
    bars, _ = market(5, n=600)
    trending = bars.copy()
    ramp = 100.0 * 1.003 ** pd.Series(range(600), index=bars.index)
    trending.loc[bars.index[300:], "close"] = ramp.iloc[300:].values
    lab = label_regimes(trending, gate_cfg().regime_window)
    assert lab.iloc[400:].mean() > 0.9


def _wr(win_rate: float) -> Stats:
    # passes every other hold-out check comfortably
    return Stats(2.0, 0.05, win_rate, 2.0, 100, 0.3)


def test_win_rate_below_floor_is_only_reason() -> None:
    cfg = gate_cfg()
    reasons = holdout_reasons(_wr(cfg.min_win_rate - 0.01), cfg, ruined=False)
    assert len(reasons) == 1 and "win rate" in reasons[0]


def test_win_rate_at_or_above_floor_passes_check() -> None:
    cfg = gate_cfg()
    assert holdout_reasons(_wr(cfg.min_win_rate), cfg, ruined=False) == []
    assert holdout_reasons(_wr(0.9), cfg, ruined=False) == []


def test_win_rate_nan_rejected() -> None:
    reasons = holdout_reasons(_wr(math.nan), gate_cfg(), ruined=False)
    assert any("win rate" in r for r in reasons)


def test_evaluate_rejects_on_win_rate_only(tmp_path) -> None:
    """Planted edge passes everything; a floor above its win rate rejects it."""
    bars, aux = market(7)
    cfg = dataclasses.replace(gate_cfg(), min_win_rate=0.99)
    v = evaluate(
        FeatureStrategy(), bars, aux, costs(), cfg, TrialCounter(tmp_path / "t")
    )
    assert not v.passed
    assert len(v.reasons) == 1 and "win rate" in v.reasons[0], v.reasons


def test_regime_with_one_lucky_trade_excluded() -> None:
    m = gate_cfg().min_trades_per_regime
    regs = {"trend": _stats(0.2, 1.5, 29), "range": _stats(0.01, 3.0, 1)}
    allowed, notes = allowed_regimes(regs, m)
    assert allowed == ["trend"]
    assert any("range" in n and "trades" in n for n in notes)


def test_both_regimes_qualify() -> None:
    regs = {"trend": _stats(0.05, 1.0, 10), "range": _stats(0.02, 0.6, 12)}
    assert allowed_regimes(regs, gate_cfg().min_trades_per_regime) == (
        ["range", "trend"],
        [],
    )


def test_no_regime_qualifies() -> None:
    regs = {"trend": _stats(0.05, 1.0, 9), "range": _stats(-0.02, -0.6, 20)}
    allowed, notes = allowed_regimes(regs, gate_cfg().min_trades_per_regime)
    assert allowed == [] and len(notes) == 2


def test_single_regime_edge_restricts_verdict(tmp_path, monkeypatch) -> None:
    import agenkit_trader.validation.gate as g

    real = g._regime_stats

    def fake(*a, **k):  # range loses on the hold-out, trend wins
        out = real(*a, **k)
        r = out["range"]
        out["range"] = dataclasses.replace(r, total_return=-0.01, sharpe=-0.5)
        return out

    monkeypatch.setattr(g, "_regime_stats", fake)
    bars, aux = market(7)
    v = evaluate(
        FeatureStrategy(), bars, aux, costs(), gate_cfg(), TrialCounter(tmp_path / "t")
    )
    assert v.passed, v.reasons
    assert v.allowed_regimes == ["trend"] and v.restricted_to == "trend"
    assert any("range" in f for f in v.flags)


def test_no_regime_edge_rejects(tmp_path, monkeypatch) -> None:
    import agenkit_trader.validation.gate as g

    real = g._regime_stats

    def fake(*a, **k):
        return {n: dataclasses.replace(s, n_trades=1) for n, s in real(*a, **k).items()}

    monkeypatch.setattr(g, "_regime_stats", fake)
    bars, aux = market(7)
    v = evaluate(
        FeatureStrategy(), bars, aux, costs(), gate_cfg(), TrialCounter(tmp_path / "t")
    )
    assert not v.passed
    assert any("no regime" in r for r in v.reasons)
