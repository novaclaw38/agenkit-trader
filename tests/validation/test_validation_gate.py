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
    assert_no_lookahead(label_regimes, bars, sample=40)
    lab = label_regimes(bars)
    assert set(lab.dropna().unique()) <= {0.0, 1.0}
    assert lab.dropna().nunique() == 2


def test_regime_labeller_sees_trend() -> None:
    bars, _ = market(5, n=600)
    trending = bars.copy()
    ramp = 100.0 * 1.003 ** pd.Series(range(600), index=bars.index)
    trending.loc[bars.index[300:], "close"] = ramp.iloc[300:].values
    lab = label_regimes(trending)
    assert lab.iloc[400:].mean() > 0.9


def test_allowed_regimes_restricts_single_regime_edge() -> None:
    both = {"trend": _stats(0.05, 1.0, 20), "range": _stats(0.02, 0.6, 20)}
    assert allowed_regimes(both) == ["range", "trend"]
    one = {"trend": _stats(0.05, 1.0, 20), "range": _stats(-0.02, -0.6, 20)}
    assert allowed_regimes(one) == ["trend"]
    none_ = {"trend": _stats(0.0, 0.0, 0), "range": _stats(-0.02, -0.6, 20)}
    assert allowed_regimes(none_) == []
