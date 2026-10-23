"""Round-2 research tooling: config, --plan, grid override, shared counter,
archive addressing. No network, no data, no counted run."""

from __future__ import annotations

import json
import runpy
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml

from agenkit_trader.config import load_symbol_rules
from agenkit_trader.data import archive
from agenkit_trader.strategies import REGISTRY
from agenkit_trader.strategies.grid import with_grid
from agenkit_trader.validation.sweep import grid_points

ROOT = Path(__file__).resolve().parents[1]
CFG = ROOT / "config" / "research_round2.yaml"
SYMS = [
    "BTCUSDT",
    "ETHUSDT",
    "BNBUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "DOGEUSDT",
    "ADAUSDT",
    "LINKUSDT",
    "AVAXUSDT",
    "LTCUSDT",
]
# Dropped before any evaluation (4h archive gap 2022-02-26..28, see NOTES.md and
# the addendum symbol-drop rule). The frozen list above stays the reference.
DROPPED = {"SOLUSDT", "XRPUSDT", "LTCUSDT"}
RUN_SYMS = [s for s in SYMS if s not in DROPPED]


class Touched(Exception):
    pass


def _boom(*a: Any, **k: Any) -> Any:
    raise Touched("data or trial counter touched")


def _mod() -> dict[str, Any]:
    mod = runpy.run_path(str(ROOT / "scripts" / "run_research.py"))
    # run_path returns a COPY of the namespace: patch the one the functions use
    g = mod["main"].__globals__
    g["load_klines"] = _boom
    g["load_funding"] = _boom
    mod["_globals"] = g
    return mod


def test_round2_config_matches_the_preregistration() -> None:
    d = yaml.safe_load(CFG.read_text())
    assert d["symbols"] == RUN_SYMS and d["interval"] == "4h"
    assert str(d["start"]) == "2021-01-01" and str(d["end"]) == "2026-10-01"
    assert d["out"] == "research/round2/REPORT.md"
    assert d["trials"] == "research/trials.json"  # shared counter
    assert d["strategies"]["trend_breakout_r2"]["grid"] == {
        "lookback": [42, 84, 168],
        "vol_cap": [1.5],
    }
    assert d["strategies"]["trend_funding_filter"]["grid"] == {
        "lookback": [42, 84, 168],
        "thr": [0.0003, 0.0006],
        "vol_cap": [1.5],
    }
    assert d["expect"] == {
        "per_strategy": {"trend_breakout_r2": 21, "trend_funding_filter": 42},
        "new_trials": 63,
        "total_after": 123,
    }
    rules = load_symbol_rules(ROOT / "config" / "symbol_rules.yaml")
    assert set(SYMS) <= set(rules)


def test_plan_prints_and_verifies_63(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    mod = _mod()
    mod["_globals"]["TrialCounter"] = _boom  # --plan must not even instantiate it
    mod["main"](["--config", str(CFG), "--policy", "runtime", "--plan"])
    out = capsys.readouterr().out
    for s in RUN_SYMS:
        assert s in out
    for s in DROPPED:
        assert s not in out
    assert "trend_breakout_r2" in out and "trend_funding_filter" in out
    assert "3 grid points x 7 symbols = 21 trials" in out
    assert "6 grid points x 7 symbols = 42 trials" in out
    assert "trials that WOULD be added: 63" in out
    assert "expected counter total afterwards: 123" in out
    assert "VERIFIED" in out and "interval: 4h" in out


def test_plan_does_not_touch_the_committed_trial_file() -> None:
    p = ROOT / "research" / "trials.json"
    r2 = ROOT / "research" / "round2"

    def snap() -> list[tuple[str, bytes]]:
        return sorted((str(f), f.read_bytes()) for f in r2.rglob("*") if f.is_file())

    before, files = p.read_bytes(), snap()
    _mod()["main"](["--config", str(CFG), "--policy", "runtime", "--plan"])
    assert p.read_bytes() == before
    assert snap() == files  # --plan writes nothing


def _config_with(tmp_path: Path, edit: Any) -> Path:
    d = yaml.safe_load(CFG.read_text())
    edit(d)
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump(d))
    return p


def _cfg_trials(tmp_path: Path, trials: Path) -> Path:
    def edit(d: dict[str, Any]) -> None:
        d["trials"] = str(trials)

    return _config_with(tmp_path, edit)


def test_plan_fails_when_counts_differ(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    def edit(d: dict[str, Any]) -> None:
        d["strategies"]["trend_funding_filter"]["grid"]["thr"].append(0.001)

    p = _config_with(tmp_path, edit)
    with pytest.raises(SystemExit) as e:
        _mod()["main"](["--config", str(p), "--policy", "runtime", "--plan"])
    assert e.value.code == 1
    out = capsys.readouterr().out
    assert "MISMATCH" in out and "63" in out


def test_real_run_refused_before_data_or_counting_on_total_mismatch(
    tmp_path: Path,
) -> None:
    trials = tmp_path / "trials.json"
    trials.write_text(json.dumps({"total": 61}) + "\n")  # 61 + 63 != 123
    with pytest.raises(SystemExit, match="plan check failed"):
        _mod()["main"](
            ["--config", str(_cfg_trials(tmp_path, trials)), "--policy", "runtime"]
        )
    assert json.loads(trials.read_text()) == {"total": 61}


def test_real_run_with_right_total_proceeds_to_data_loading(tmp_path: Path) -> None:
    trials = tmp_path / "trials.json"
    trials.write_text(json.dumps({"total": 60}) + "\n")  # 60 + 63 == 123
    with pytest.raises(Touched):  # got past the plan check: first data load
        _mod()["main"](
            ["--config", str(_cfg_trials(tmp_path, trials)), "--policy", "runtime"]
        )
    assert json.loads(trials.read_text()) == {"total": 60}  # nothing counted yet


def test_round1_defaults_are_unchanged(capsys: pytest.CaptureFixture[str]) -> None:
    mod = _mod()
    a = mod["build_parser"]().parse_args(["--policy", "research"])
    assert a.symbols == ["BTCUSDT", "ETHUSDT"] and a.interval == "1h"
    assert a.out == ROOT / "research" / "REPORT.md"
    assert a.config is None and a.trials is None and a.plan is False
    plan = mod["build_plan"](a.symbols, a.interval, None)
    assert [s.name for s in plan.strategies] == [
        "funding_reversion",
        "trend_breakout",
        "short_reversion",
    ]  # the new strategy is NOT swept into a round-1 style run
    assert [s.param_grid for s in plan.strategies] == [
        REGISTRY[n].param_grid
        for n in ("funding_reversion", "trend_breakout", "short_reversion")
    ]
    mod["main"](["--policy", "research", "--plan"])
    out = capsys.readouterr().out
    assert "trend_funding_filter" not in out and "VERIFIED" not in out


def test_trials_default_sits_beside_out(tmp_path: Path) -> None:
    mod = _mod()
    with pytest.raises(Touched):
        mod["main"](["--policy", "research", "--out", str(tmp_path / "R.md")])
    assert not (tmp_path / "trials.json").exists()  # not created before data


def test_grid_override_leaves_the_registered_strategy_alone() -> None:
    base = REGISTRY["trend_breakout"]
    before = {k: list(v) for k, v in base.param_grid.items()}
    s = with_grid(base, {"lookback": [42, 84, 168], "vol_cap": [1.5]})
    assert s.name == "trend_breakout" and s.edge == base.edge
    assert len(grid_points(s.param_grid)) == 3
    assert (
        base.param_grid
        == before
        == {
            "lookback": [48, 96, 168, 336],
            "vol_cap": [1.0, 1.5, 2.0],
        }
    )
    assert s.warmup({"lookback": 42, "vol_cap": 1.5}) == base.warmup(  # type: ignore[attr-defined]
        {"lookback": 42, "vol_cap": 1.5}
    )
    assert getattr(s, "aux_required", ()) == ()
    f = with_grid(
        REGISTRY["trend_funding_filter"],
        dict(REGISTRY["trend_funding_filter"].param_grid),
    )
    assert getattr(f, "aux_required", ()) == ("funding",)


@pytest.mark.parametrize(
    "grid",
    [
        {"lookback": [42, 84]},  # missing key
        {"lookback": [84, 42], "vol_cap": [1.5]},  # unsorted
        {"lookback": [42, 42], "vol_cap": [1.5]},  # duplicate
        {"lookback": [], "vol_cap": [1.5]},
        {"lookback": [42], "vol_cap": [1.5], "x": [1]},
    ],
)
def test_bad_grid_override_refused(grid: dict[str, list[Any]]) -> None:
    with pytest.raises(ValueError):
        with_grid(REGISTRY["trend_breakout"], grid)


def test_unknown_strategy_in_config_refused(tmp_path: Path) -> None:
    p = _config_with(tmp_path, lambda d: d["strategies"].update({"nope": {}}))
    with pytest.raises(SystemExit, match="unknown strategy"):
        _mod()["main"](["--config", str(p), "--policy", "runtime", "--plan"])


def test_archive_addresses_every_round2_symbol(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[str] = []

    def fake(rel: str, cache: Path) -> bytes | None:
        seen.append(rel)
        raise Touched

    monkeypatch.setattr(archive, "_fetch_zip", fake)
    for s in SYMS:
        with pytest.raises(Touched):
            archive.load_klines(s, "4h", date(2021, 1, 1), date(2021, 2, 1), tmp_path)
        with pytest.raises(Touched):
            archive.load_funding(s, date(2021, 1, 1), date(2021, 2, 1), tmp_path)
    assert "monthly/klines/AVAXUSDT/4h/AVAXUSDT-4h-2021-01.zip" in seen
    assert "monthly/fundingRate/LTCUSDT/LTCUSDT-fundingRate-2021-01.zip" in seen
    assert len(seen) == 20


def test_r2_strategy_is_trend_breakout_on_the_round2_grid_and_deployable() -> None:
    import numpy as np
    import pandas as pd

    from agenkit_trader.regime import REGIMES
    from agenkit_trader.runtime.loop import Deployment, StartupRefused
    from agenkit_trader.runtime.loop import validate_deployments as vd

    r2, tb = REGISTRY["trend_breakout_r2"], REGISTRY["trend_breakout"]
    assert r2.param_grid == {"lookback": [42, 84, 168], "vol_cap": [1.5]}
    assert tb.param_grid == {"lookback": [48, 96, 168, 336], "vol_cap": [1.0, 1.5, 2.0]}
    rng = np.random.default_rng(0)
    c = pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.01, 1500))))
    c.index = pd.date_range("2022-01-01", periods=1500, freq="4h", tz="UTC")
    b = pd.DataFrame(
        {"open": c, "high": c * 1.002, "low": c * 0.998, "close": c, "volume": 1.0}
    )
    p = {"lookback": 42, "vol_cap": 1.5}
    pd.testing.assert_series_equal(r2.signal(b, p, {}), tb.signal(b, p, {}))

    def dep(name: str, lb: int) -> Deployment:
        return Deployment(
            name, "BTCUSDT", "4h", {"lookback": lb, "vol_cap": 1.5}, list(REGIMES)
        )

    vd([dep("trend_breakout_r2", 42)], REGISTRY)
    vd([dep("trend_breakout_r2", 168)], REGISTRY)
    with pytest.raises(StartupRefused, match="grid"):
        vd([dep("trend_breakout", 42)], REGISTRY)
    vd([dep("trend_breakout", 168)], REGISTRY)  # round 1 grid unchanged


def test_config_flags_that_conflict_with_the_config_are_refused() -> None:
    m = _mod()
    base = ["--config", str(CFG), "--policy", "runtime", "--plan"]
    for extra in (
        ["--interval", "1h"],
        ["--start", "2022-01-01"],
        ["--end", "2026-01-01"],
        ["--symbols", "BTCUSDT"],
        ["--out", "x/REPORT.md"],
        ["--trials", "x/trials.json"],
    ):
        with pytest.raises(SystemExit, match="conflicts with"):
            m["main"]([*base, *extra])
    # restating the config's own value is fine
    m["main"]([*base, "--interval", "4h"])
