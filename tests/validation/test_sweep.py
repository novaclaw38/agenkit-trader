import math

from vsynth import FeatureStrategy, SpikeStrategy, costs, gate_cfg, market

from agenkit_trader.validation.sweep import grid_points, neighbourhood_sweep


def test_grid_points_cartesian() -> None:
    pts = grid_points({"a": [1, 2], "b": ["x", "y", "z"]})
    assert len(pts) == 6
    assert pts[0] == {"a": 1, "b": "x"}


def test_smooth_edge_is_stable() -> None:
    bars, aux = market(1)
    s = FeatureStrategy()
    res = neighbourhood_sweep(
        s,
        bars,
        costs(),
        s.param_grid,
        aux=aux,
        tolerance=gate_cfg().neighbour_tolerance,
    )
    assert len(res.table) == 5
    assert res.stable, res.reason
    assert res.best_score > 0


def test_single_spike_is_unstable() -> None:
    bars, aux = market(2, n_noise=5)
    s = SpikeStrategy()
    res = neighbourhood_sweep(
        s,
        bars,
        costs(),
        s.param_grid,
        aux=aux,
        tolerance=gate_cfg().neighbour_tolerance,
    )
    assert res.best_params == {"k": 3}
    assert not res.stable
    assert "neighbour" in res.reason


def test_single_point_grid_cannot_prove_stability() -> None:
    bars, aux = market(1)
    s = FeatureStrategy()
    res = neighbourhood_sweep(s, bars, costs(), {"thr": [0.0]}, aux=aux, tolerance=0.5)
    assert not res.stable


def test_ruined_variant_scores_minus_inf() -> None:
    # always short (f = -1) while the price jumps 150%: equity is wiped out
    bars, aux = market(1, n=300)
    bars["close"] = [100.0, 250.0] * 150
    aux = {"f": aux["f"] * 0.0 - 1.0}
    s = FeatureStrategy()
    res = neighbourhood_sweep(
        s, bars, costs(), {"thr": [0.0, 0.1]}, aux=aux, tolerance=0.5
    )
    ruined = res.table[res.table["ruined"]]
    assert len(ruined) > 0
    assert all(math.isinf(x) and x < 0 for x in ruined["score"])
