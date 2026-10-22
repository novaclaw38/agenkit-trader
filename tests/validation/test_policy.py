"""Policy selection: 'research' is unchanged, 'runtime' uses the runtime
sizing/stop simulator everywhere (sweep, hold-out, cost re-run, regimes)."""

from __future__ import annotations

import hashlib
import json
import runpy
from pathlib import Path
from typing import Any

import numpy as np
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


@pytest.fixture(scope="module")
def ranged_market() -> tuple[pd.DataFrame, dict[str, pd.Series]]:
    bars, aux = market(1)
    return _ranged(bars), aux


@pytest.fixture(scope="module")
def research_verdict(
    tmp_path_factory: pytest.TempPathFactory,
    ranged_market: tuple[pd.DataFrame, dict[str, pd.Series]],
) -> vgate.Verdict:
    bars, aux = ranged_market
    counter = TrialCounter(tmp_path_factory.mktemp("r") / "t.json")
    return evaluate(FeatureStrategy(), bars, aux, costs(), gate_cfg(), counter)


@pytest.fixture(scope="module")
def runtime_run(
    tmp_path_factory: pytest.TempPathFactory,
    ranged_market: tuple[pd.DataFrame, dict[str, pd.Series]],
) -> tuple[vgate.Verdict, list[dict[str, Any]]]:
    """One runtime-policy evaluation, with every simulator call recorded and
    the research engine forbidden."""
    bars, aux = ranged_market
    calls: list[dict[str, Any]] = []
    real_rt = vsweep.run_backtest_runtime

    def spy_rt(*a, **k):  # type: ignore[no-untyped-def]
        calls.append({"bars": a[0], "costs": a[2], **k})
        return real_rt(*a, **k)

    def no_research(*a, **k):  # type: ignore[no-untyped-def]
        raise AssertionError("research engine used under policy='runtime'")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(vsweep, "run_backtest_runtime", spy_rt)
        mp.setattr(vsweep, "run_backtest", no_research)
        counter = TrialCounter(tmp_path_factory.mktemp("t") / "t.json")
        v = evaluate(
            FeatureStrategy(),
            bars,
            aux,
            costs(),
            gate_cfg(),
            counter,
            policy="runtime",
        )
    return v, calls


def test_runtime_policy_routes_every_backtest_through_the_simulator(
    runtime_run: tuple[vgate.Verdict, list[dict[str, Any]]],
    ranged_market: tuple[pd.DataFrame, dict[str, pd.Series]],
) -> None:
    v, calls = runtime_run
    n_grid = len(FeatureStrategy().param_grid["thr"])
    # sweep (grid) + train re-run + hold-out + cost-sensitivity re-run
    assert len(calls) == n_grid + 2 + 1
    assert v.policy == "runtime"
    assert v.holdout.n_trades > 0
    assert set(v.regimes) == set(vgate.REGIMES)
    assert v.cost_2x.total_return < v.holdout.total_return
    # the hold-out runs get the train bars as warm-up and the signal of the
    # last train bar as prev_signal
    bars, aux = ranged_market
    hold = [c for c in calls if c.get("warmup") is not None]
    assert len(hold) == 2
    train_end = hold[0]["warmup"].index[-1]
    full_sig = FeatureStrategy().signal(bars, v.params, aux)
    assert hold[0]["prev_signal"] == float(np.sign(full_sig.loc[train_end]))


def test_runtime_policy_differs_from_research(
    research_verdict: vgate.Verdict,
    runtime_run: tuple[vgate.Verdict, list[dict[str, Any]]],
) -> None:
    assert research_verdict.holdout != runtime_run[0].holdout


class AlwaysLong:
    name = "always_long"
    edge = "test"
    param_grid: dict[str, list[Any]] = {"k": [1, 2]}

    def signal(
        self, bars: pd.DataFrame, params: dict[str, Any], aux: dict[str, pd.Series]
    ) -> pd.Series:
        return pd.Series(1.0, index=bars.index)


def test_signal_on_across_the_split_is_not_a_holdout_entry(tmp_path: Path) -> None:
    bars, aux = market(1, n=600)
    v = evaluate(
        AlwaysLong(),  # type: ignore[arg-type]
        _ranged(bars),
        aux,
        costs(),
        gate_cfg(),
        TrialCounter(tmp_path / "t"),
        policy="runtime",
    )
    assert v.holdout.n_trades == 0  # the runtime would not enter mid-signal


def test_segment_reads_prev_from_the_window_with_warmup() -> None:
    """Under the runtime policy ``prev`` is read from the runtime's window, so
    warm-up history that already holds the signal on is not a fresh
    transition at the segment start; without it bar 0 is one (here skipped:
    no ATR yet)."""
    bars, aux = market(1, n=600)
    bars = _ranged(bars)
    warm, seg = bars.iloc[:300], bars.iloc[300:]
    s = AlwaysLong()
    kw: dict[str, Any] = dict(policy="runtime")
    p = s.param_grid and {k: v[0] for k, v in s.param_grid.items()}
    fresh = vsweep.backtest_params(s, seg, aux, p, costs(), **kw)  # type: ignore[arg-type]
    cont = vsweep.backtest_params(s, seg, aux, p, costs(), warmup=warm, **kw)  # type: ignore[arg-type]
    assert fresh.skipped_entries == 1 and fresh.stats.n_trades == 0
    assert cont.skipped_entries == 0 and cont.stats.n_trades == 0


def test_halted_holdout_is_rejected(
    tmp_path: Path, ranged_market: tuple[pd.DataFrame, dict[str, pd.Series]]
) -> None:
    bars, aux = ranged_market
    bars, aux = bars.iloc[:1200], {k: v.iloc[:1200] for k, v in aux.items()}
    touchy = RuntimePolicy(RiskConfig(max_daily_loss=0.002), RuntimeConfig())
    v = evaluate(
        FeatureStrategy(),
        bars,
        aux,
        costs(),
        gate_cfg(),
        TrialCounter(tmp_path / "t"),
        policy="runtime",
        runtime_policy=touchy,
    )
    assert v.halted and v.halt_reason in ("daily_loss", "drawdown")
    assert not v.passed
    assert any("would have halted the runtime" in r for r in v.reasons)


def test_holdout_reasons_halted() -> None:
    st = vgate.Stats(2.0, 0.01, 0.6, 3.0, 100, 0.5)
    assert vgate.holdout_reasons(st, gate_cfg(), ruined=False) == []
    r = vgate.holdout_reasons(st, gate_cfg(), ruined=False, halted=True)
    assert len(r) == 1 and "would have halted the runtime" in r[0]


def test_runtime_policy_uses_given_configs(
    ranged_market: tuple[pd.DataFrame, dict[str, pd.Series]],
) -> None:
    bars, aux = ranged_market
    tight = RuntimePolicy(RiskConfig(max_leverage=0.01), RuntimeConfig())
    sw = neighbourhood_sweep(
        FeatureStrategy(),
        bars.iloc[:800],
        costs(),
        FeatureStrategy().param_grid,
        aux=aux,
        tolerance=0.5,
        policy="runtime",
        runtime_policy=tight,
    )
    assert (sw.table["n_trades"] == 0).all()  # every entry over the cap


def test_unknown_policy_raises(tmp_path: Path) -> None:
    bars, aux = market(1, n=200)
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


def test_report_records_runtime_policy_only_when_used(
    tmp_path: Path,
    research_verdict: vgate.Verdict,
    runtime_run: tuple[vgate.Verdict, list[dict[str, Any]]],
) -> None:
    v_r, v_t = research_verdict, runtime_run[0]
    p1, p2 = tmp_path / "r" / "REPORT.md", tmp_path / "t" / "REPORT.md"
    p1.parent.mkdir()
    p2.parent.mkdir()
    write_report([v_r], p1)
    write_report([v_t], p2)
    md1, md2 = p1.read_text(), p2.read_text()
    assert "policy" not in md1.lower() and "halt" not in md1.lower()
    ev1 = json.loads(p1.with_suffix(".json").read_text())[0]
    assert "policy" not in ev1 and "halted" not in ev1
    assert "Simulation policy: runtime" in md2
    assert f"- halted: {v_t.halted}" in md2
    ev2 = json.loads(p2.with_suffix(".json").read_text())[0]
    assert ev2["policy"] == "runtime" and ev2["halted"] == v_t.halted
    assert "halt_reason" in ev2
    for p, pol in ((p1, "research"), (p2, "runtime")):
        dep = json.loads((p.parent / "deployed.json").read_text())
        assert all(d["policy"] == pol for d in dep)


def test_deployed_entries_carry_the_policy(
    tmp_path: Path, research_verdict: vgate.Verdict
) -> None:
    import dataclasses

    v = dataclasses.replace(research_verdict, passed=True, reasons=[])
    write_report([v], tmp_path / "REPORT.md")
    dep = json.loads((tmp_path / "deployed.json").read_text())
    assert dep[0]["policy"] == "research"


def test_run_research_requires_a_policy() -> None:
    mod = runpy.run_path(str(ROOT / "scripts" / "run_research.py"))
    p = mod["build_parser"]()
    with pytest.raises(SystemExit):
        p.parse_args([])  # a counted run must choose deliberately
    assert p.parse_args(["--policy", "research"]).policy == "research"
    assert p.parse_args(["--policy", "runtime"]).policy == "runtime"
    with pytest.raises(SystemExit):
        p.parse_args(["--policy", "paper"])


def test_grid_point_beyond_the_runtime_window_is_refused() -> None:
    from agenkit_trader.backtest.runtime_policy import WindowError
    from agenkit_trader.strategies import REGISTRY

    bars, aux = market(1, n=900)
    strat = REGISTRY["trend_breakout"]  # warm-up 722 bars
    rp = RuntimePolicy(RiskConfig(), RuntimeConfig(bars_limit=600))
    with pytest.raises(WindowError, match="runtime window"):
        vsweep.backtest_params(
            strat,
            _ranged(bars),
            aux,
            {"lookback": 48, "vol_cap": 1.0},
            costs(),
            policy="runtime",
            runtime_policy=rp,
        )
