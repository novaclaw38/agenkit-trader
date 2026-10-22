"""Policy selection: 'research' is unchanged, 'runtime' uses the runtime
sizing/stop simulator everywhere (sweep, hold-out, cost re-run, regimes)."""

from __future__ import annotations

import hashlib
import json
import runpy
from pathlib import Path

import pandas as pd
import pytest
from vsynth import FeatureStrategy, SpikeStrategy, costs, gate_cfg, market

from agenkit_trader.backtest.runtime_policy import RuntimePolicy
from agenkit_trader.config import RiskConfig, RuntimeConfig
from agenkit_trader.validation import gate as vgate
from agenkit_trader.validation import sweep as vsweep
from agenkit_trader.validation.gate import evaluate
from agenkit_trader.validation.report import write_report
from agenkit_trader.validation.sweep import neighbourhood_sweep
from agenkit_trader.validation.trials import TrialCounter

ROOT = Path(__file__).resolve().parents[2]

# sha256 of the verdict numbers produced by the code BEFORE the policy
# selector existed (base commit 1ca2b92), for the same fixtures.
GOLDEN = {
    "feature": "793dab7b7db6d2ef4803883514fa877444748e76483f8adcf1be13a825d51eeb",
    "spike": "055b30ce506b33419740395bb57af18dc44358260197b9add6497edcbebac6d3",
}


def _blob(v: vgate.Verdict) -> str:
    return hashlib.sha256(
        repr(
            (
                v.train,
                v.holdout,
                v.cost_2x,
                v.regimes,
                v.pvalue,
                v.params,
                v.sweep.table.to_dict(orient="records"),
                v.reasons,
            )
        ).encode()
    ).hexdigest()


@pytest.mark.parametrize(
    ("name", "strat", "seed"),
    [("feature", FeatureStrategy(), 1), ("spike", SpikeStrategy(), 2)],
)
def test_research_policy_is_byte_identical_to_before(
    tmp_path: Path, name: str, strat: object, seed: int
) -> None:
    bars, aux = market(seed, n_noise=5)
    for kw in ({}, {"policy": "research"}):
        v = evaluate(
            strat,  # type: ignore[arg-type]
            bars,
            aux,
            costs(),
            gate_cfg(),
            TrialCounter(tmp_path / f"{name}{len(kw)}.json"),
            **kw,  # type: ignore[arg-type]
        )
        assert _blob(v) == GOLDEN[name]
        assert v.policy == "research"


def _ranged(bars: pd.DataFrame) -> pd.DataFrame:
    b = bars.copy()
    b["high"] = b["close"] * 1.004
    b["low"] = b["close"] * 0.996
    return b


def test_runtime_policy_routes_every_backtest_through_the_simulator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bars, aux = market(1)
    bars = _ranged(bars)
    calls: list[str] = []
    real_rt = vsweep.run_backtest_runtime

    def spy_rt(*a, **k):  # type: ignore[no-untyped-def]
        calls.append("runtime")
        return real_rt(*a, **k)

    def no_research(*a, **k):  # type: ignore[no-untyped-def]
        raise AssertionError("research engine used under policy='runtime'")

    monkeypatch.setattr(vsweep, "run_backtest_runtime", spy_rt)
    monkeypatch.setattr(vsweep, "run_backtest", no_research)
    s = FeatureStrategy()
    v = evaluate(
        s,
        bars,
        aux,
        costs(),
        gate_cfg(),
        TrialCounter(tmp_path / "t"),
        policy="runtime",
    )
    n_grid = len(s.param_grid["thr"])
    # sweep (grid) + train re-run + hold-out + cost-sensitivity re-run
    assert len(calls) == n_grid + 3
    assert v.policy == "runtime"
    assert v.holdout.n_trades > 0
    assert set(v.regimes) == set(vgate.REGIMES)
    assert v.cost_2x.total_return < v.holdout.total_return


def test_runtime_policy_differs_from_research(tmp_path: Path) -> None:
    bars, aux = market(1)
    bars = _ranged(bars)
    kw = dict(costs=costs(), gate_cfg=gate_cfg())
    a = evaluate(
        FeatureStrategy(), bars, aux, counter=TrialCounter(tmp_path / "a"), **kw
    )  # type: ignore[arg-type]
    b = evaluate(
        FeatureStrategy(),
        bars,
        aux,
        counter=TrialCounter(tmp_path / "b"),
        policy="runtime",
        **kw,  # type: ignore[arg-type]
    )
    assert a.holdout != b.holdout


def test_runtime_policy_uses_given_configs(tmp_path: Path) -> None:
    bars, aux = market(1)
    bars = _ranged(bars)
    tight = RuntimePolicy(RiskConfig(max_leverage=0.01), RuntimeConfig())
    sw = neighbourhood_sweep(
        FeatureStrategy(),
        bars,
        costs(),
        FeatureStrategy().param_grid,
        aux=aux,
        tolerance=0.5,
        policy="runtime",
        runtime_policy=tight,
    )
    assert (sw.table["n_trades"] == 0).all()  # every entry over the cap


def test_unknown_policy_raises(tmp_path: Path) -> None:
    bars, aux = market(1)
    with pytest.raises(ValueError, match="policy"):
        evaluate(
            FeatureStrategy(),
            bars,
            aux,
            costs(),
            gate_cfg(),
            TrialCounter(tmp_path / "t"),
            policy="paper",
        )


def test_report_records_runtime_policy_only_when_used(tmp_path: Path) -> None:
    bars, aux = market(1)
    bars = _ranged(bars)
    v_r = evaluate(
        FeatureStrategy(), bars, aux, costs(), gate_cfg(), TrialCounter(tmp_path / "a")
    )
    v_t = evaluate(
        FeatureStrategy(),
        bars,
        aux,
        costs(),
        gate_cfg(),
        TrialCounter(tmp_path / "b"),
        policy="runtime",
    )
    p1, p2 = tmp_path / "r" / "REPORT.md", tmp_path / "t" / "REPORT.md"
    p1.parent.mkdir()
    p2.parent.mkdir()
    write_report([v_r], p1)
    write_report([v_t], p2)
    md1, md2 = p1.read_text(), p2.read_text()
    assert "policy" not in md1.lower()
    assert "policy" not in json.loads(p1.with_suffix(".json").read_text())[0]
    assert "Simulation policy: runtime" in md2
    assert json.loads(p2.with_suffix(".json").read_text())[0]["policy"] == "runtime"


def test_run_research_defaults_to_runtime_policy() -> None:
    mod = runpy.run_path(str(ROOT / "scripts" / "run_research.py"))
    p = mod["build_parser"]()
    assert p.parse_args([]).policy == "runtime"
    assert p.parse_args(["--policy", "research"]).policy == "research"
    with pytest.raises(SystemExit):
        p.parse_args(["--policy", "paper"])
