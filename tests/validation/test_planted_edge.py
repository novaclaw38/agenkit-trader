"""End-to-end statistical honesty: real edges pass, noise and fragility fail."""

from vsynth import FeatureStrategy, costs, gate_cfg, market

from agenkit_trader.validation.gate import evaluate
from agenkit_trader.validation.trials import TrialCounter


def test_planted_edge_passes(tmp_path) -> None:
    bars, aux = market(7)
    c = TrialCounter(tmp_path / "trials.json")
    v = evaluate(FeatureStrategy(), bars, aux, costs(), gate_cfg(), c, symbol="SYN")
    assert v.passed, v.reasons
    assert v.reasons == []
    assert v.holdout.n_trades >= gate_cfg().min_trades
    assert v.pvalue < gate_cfg().max_trial_adjusted_pvalue
    assert v.allowed_regimes == ["range", "trend"]


def _noise_failures(per_side_bps: float, tmp_path) -> int:
    fails = 0
    for seed in range(20):
        bars, aux = market(1000 + seed, k=0.0)
        c = TrialCounter(tmp_path / f"t{per_side_bps}_{seed}.json")
        v = evaluate(FeatureStrategy(), bars, aux, costs(per_side_bps), gate_cfg(), c)
        fails += not v.passed
    return fails


def test_pure_noise_fails_with_costs(tmp_path) -> None:
    assert _noise_failures(5.0, tmp_path) >= 18


def test_pure_noise_fails_even_without_costs(tmp_path) -> None:
    """Statistical checks alone (no cost drag) must reject noise."""
    assert _noise_failures(0.0, tmp_path) >= 18


# A market whose edge survives 1x costs but not 2x. k and the cost level are
# properties of the synthetic market (picked so 1x passes every hold-out
# check); the gate thresholds still come from config/gate.yaml.
MARGINAL_K = 0.003
MARGINAL_BPS = 14.0


def test_doubling_costs_flips_marginal_strategy(tmp_path) -> None:
    bars, aux = market(11, k=MARGINAL_K)
    v = evaluate(
        FeatureStrategy(),
        bars,
        aux,
        costs(MARGINAL_BPS),
        gate_cfg(),
        TrialCounter(tmp_path / "t.json"),
    )
    assert not v.passed
    assert len(v.reasons) == 1 and "cost" in v.reasons[0], v.reasons
    assert v.holdout.total_return > 0
    assert v.cost_2x.total_return <= 0 or v.cost_2x.profit_factor <= 1
