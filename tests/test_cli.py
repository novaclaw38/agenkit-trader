"""`agenkit` CLI: paper refuses to start without a signed-off deployed.json."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from agenkit_trader import cli

ROOT = Path(__file__).resolve().parents[1]
RISK = str(ROOT / "config" / "risk.yaml")
RUNTIME = str(ROOT / "config" / "runtime.yaml")


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Any attempt to build a venue or send HTTP is recorded and fails."""
    attempts: list[str] = []

    def boom(*a: Any, **k: Any) -> Any:
        attempts.append("network")
        raise AssertionError("network call attempted")

    monkeypatch.setattr(httpx.Client, "send", boom)
    monkeypatch.setattr(httpx, "get", boom)
    monkeypatch.setattr(httpx, "post", boom)
    monkeypatch.setattr(cli, "BinanceTestnetVenue", boom)
    return attempts


def paper(*extra: str, tmp_path: Path) -> int:
    return cli.main(
        ["paper", "--config", RISK, "--log", str(tmp_path / "paper.jsonl"), *extra]
    )


def test_paper_refuses_on_repo_deployed_json(
    no_network: list[str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(ROOT)  # default --deployed is research/deployed.json
    # research/deployed.json is [] today: nothing passed validation
    assert json.loads((ROOT / "research" / "deployed.json").read_text()) == []
    rc = paper(tmp_path=tmp_path)
    err = capsys.readouterr().err
    assert rc != 0
    assert "refusing to start" in err and "empty" in err
    assert no_network == []


def test_paper_refuses_on_empty_deployed(
    no_network: list[str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    d = tmp_path / "deployed.json"
    d.write_text("[]\n")
    rc = paper("--deployed", str(d), "--once", tmp_path=tmp_path)
    assert rc != 0
    assert "empty" in capsys.readouterr().err
    assert no_network == []


@pytest.mark.parametrize("content", [None, "{oops", '{"name": "x"}', "[1]"])
def test_paper_refuses_on_missing_or_bad_deployed(
    content: str | None, no_network: list[str], tmp_path: Path
) -> None:
    d = tmp_path / "deployed.json"
    if content is not None:
        d.write_text(content)
    assert paper("--deployed", str(d), tmp_path=tmp_path) != 0
    assert no_network == []


def test_paper_refuses_unknown_strategy(
    no_network: list[str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    d = tmp_path / "deployed.json"
    d.write_text(
        json.dumps(
            [
                {
                    "name": "made_up",
                    "symbol": "BTCUSDT",
                    "interval": "1h",
                    "params": {},
                    "allowed_regimes": ["trend"],
                    "policy": "runtime",
                }
            ]
        )
    )
    assert paper("--deployed", str(d), tmp_path=tmp_path) != 0
    assert "unknown strategy" in capsys.readouterr().err
    assert no_network == []


def test_paper_refuses_research_policy_entry(
    no_network: list[str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    d = tmp_path / "deployed.json"
    d.write_text(
        json.dumps(
            [
                {
                    "name": "trend_breakout",
                    "symbol": "BTCUSDT",
                    "interval": "1h",
                    "params": {"lookback": 48, "vol_cap": 1.0},
                    "allowed_regimes": ["range", "trend"],
                    "policy": "research",
                }
            ]
        )
    )
    assert paper("--deployed", str(d), "--once", tmp_path=tmp_path) != 0
    assert "does not validate what the runtime" in capsys.readouterr().err
    assert no_network == []


def test_paper_refuses_without_keys(
    no_network: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("BINANCE_TESTNET_KEY", raising=False)
    monkeypatch.delenv("BINANCE_TESTNET_SECRET", raising=False)
    d = tmp_path / "deployed.json"
    d.write_text(
        json.dumps(
            [
                {
                    "name": "trend_breakout",
                    "symbol": "BTCUSDT",
                    "interval": "1h",
                    "params": {"lookback": 96, "vol_cap": 1.5},
                    "allowed_regimes": ["range", "trend"],
                    "policy": "runtime",
                }
            ]
        )
    )
    rc = paper(
        "--deployed",
        str(d),
        "--runtime",
        RUNTIME,
        "--init-state",
        "--state",
        str(tmp_path / "s.json"),
        tmp_path=tmp_path,
    )
    assert rc != 0
    assert "BINANCE_TESTNET_KEY" in capsys.readouterr().err
    assert no_network == []


def test_paper_bad_risk_config(no_network: list[str], tmp_path: Path) -> None:
    bad = tmp_path / "risk.yaml"
    bad.write_text("max_leverage: -1\n")
    rc = cli.main(["paper", "--config", str(bad), "--log", str(tmp_path / "l")])
    assert rc != 0 and no_network == []


def test_paper_logs_config_hashes_before_refusing(
    no_network: list[str], tmp_path: Path
) -> None:
    paper(tmp_path=tmp_path)
    recs = [json.loads(x) for x in (tmp_path / "paper.jsonl").read_text().splitlines()]
    start = [r for r in recs if r["kind"] == "startup"][0]
    assert len(start["inputs"]["risk_config_sha256"]) == 64
    assert recs[-1]["result"] == "refused"


def test_research_delegates_to_script(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def run_path(path: str, run_name: str) -> dict[str, Any]:
        import sys

        seen["path"], seen["name"], seen["argv"] = path, run_name, list(sys.argv)
        return {}

    monkeypatch.setattr(cli.runpy, "run_path", run_path)
    assert cli.main(["research", "--symbols", "BTCUSDT"]) == 0
    assert Path(seen["path"]) == ROOT / "scripts" / "run_research.py"
    assert seen["name"] == "__main__"
    assert seen["argv"][1:] == ["--symbols", "BTCUSDT"]


def test_console_script_declared() -> None:
    text = (ROOT / "pyproject.toml").read_text()
    assert 'agenkit = "agenkit_trader.cli:main"' in text


def test_no_live_flag() -> None:
    with pytest.raises(SystemExit):
        cli.main(["paper", "--config", RISK, "--live"])


# ---------------------------------------------------------------- fix round 1


def _deployed(tmp_path: Path, regimes: list[str] | None = None) -> Path:
    d = tmp_path / "deployed.json"
    d.write_text(
        json.dumps(
            [
                {
                    "name": "trend_breakout",
                    "symbol": "BTCUSDT",
                    "interval": "1h",
                    "params": {"lookback": 96, "vol_cap": 1.5},
                    "allowed_regimes": regimes or ["range", "trend"],
                    "policy": "runtime",
                }
            ]
        )
    )
    return d


@pytest.fixture
def fake_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BINANCE_TESTNET_KEY", "k-not-real")
    monkeypatch.setenv("BINANCE_TESTNET_SECRET", "s-not-real")


def test_paper_refuses_regime_restricted_deployment(
    no_network: list[str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    d = _deployed(tmp_path, ["trend"])
    assert paper("--deployed", str(d), tmp_path=tmp_path) != 0
    assert "regime-restricted" in capsys.readouterr().err
    assert no_network == []


def test_paper_refuses_missing_state_without_init(
    no_network: list[str],
    fake_keys: None,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state = tmp_path / "new" / "state.json"
    rc = paper(
        "--deployed", str(_deployed(tmp_path)), "--state", str(state), tmp_path=tmp_path
    )
    err = capsys.readouterr().err
    assert rc != 0 and "--init-state" in err and str(state) in err
    assert no_network == [] and not state.exists()


def test_relative_state_path_is_anchored_to_repo_root(
    no_network: list[str],
    fake_keys: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    paper("--deployed", str(_deployed(tmp_path)), tmp_path=tmp_path)
    err = capsys.readouterr().err
    assert f"state file: {ROOT / 'state' / 'paper_state.json'}" in err


def test_logged_halt_refuses_even_with_init_state(
    no_network: list[str],
    fake_keys: None,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from agenkit_trader.log import DecisionLog

    log = tmp_path / "paper.jsonl"
    DecisionLog(log).write(
        "kill_switch", {"reason": "x"}, {"halted": True}, "flattened"
    )
    rc = paper(
        "--deployed",
        str(_deployed(tmp_path)),
        "--state",
        str(tmp_path / "fresh.json"),
        "--init-state",
        tmp_path=tmp_path,
    )
    assert rc != 0 and "halt" in capsys.readouterr().err
    assert no_network == []


def test_reset_halt_requires_phrase_and_is_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agenkit_trader.log import DecisionLog
    from agenkit_trader.runtime.loop import last_unreset_halt

    log = tmp_path / "paper.jsonl"
    state = tmp_path / "state.json"
    DecisionLog(log).write(
        "kill_switch", {"reason": "x"}, {"halted": True}, "flattened"
    )
    args = ["reset-halt", "--state", str(state), "--log", str(log)]
    monkeypatch.setattr("builtins.input", lambda prompt: "resume")
    assert cli.main(args) != 0
    assert last_unreset_halt(log) is not None
    monkeypatch.setattr("builtins.input", lambda prompt: "RESUME")
    assert cli.main(args) == 0
    assert last_unreset_halt(log) is None
    last = json.loads(log.read_text().splitlines()[-1])
    assert last["kind"] == "runtime" and last["result"] == "halt_reset"


def test_reset_halt_on_truncated_log_explains_and_does_not_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from agenkit_trader.log import DecisionLog

    log = tmp_path / "paper.jsonl"
    DecisionLog(log).write(
        "kill_switch", {"reason": "x"}, {"halted": True}, "flattened"
    )
    with log.open("a") as fh:
        fh.write('{"kind": "runtime", "res')  # crash mid-append
    before = log.read_bytes()
    monkeypatch.setattr("builtins.input", lambda prompt: "RESUME")
    rc = cli.main(
        ["reset-halt", "--state", str(tmp_path / "s.json"), "--log", str(log)]
    )
    err = capsys.readouterr().err
    assert rc == 2
    assert str(log) in err and "line 2" in err and "move" in err
    assert log.read_bytes() == before  # never auto-repaired


def _subparsers() -> dict[str, Any]:
    import argparse

    ap = cli.build_parser()
    sub = next(a for a in ap._actions if isinstance(a, argparse._SubParsersAction))
    return dict(sub.choices)


@pytest.mark.parametrize("cmd", ["paper", "reset-halt", "live", "research"])
def test_every_flag_has_help(cmd: str) -> None:
    p = _subparsers()[cmd]
    for act in p._actions:
        assert act.help, f"{cmd}: {act.option_strings or act.dest} has no help"


def test_research_script_flags_have_help() -> None:
    import runpy

    mod = runpy.run_path(str(ROOT / "scripts" / "run_research.py"))
    for act in mod["build_parser"]()._actions:
        assert act.help, act.option_strings


def test_paper_config_defaults_to_risk_yaml() -> None:
    a = cli.build_parser().parse_args(["paper"])
    assert a.config == Path("config/risk.yaml")


def test_paper_config_directory_is_friendly(
    no_network: list[str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = cli.main(["paper", "--config", str(tmp_path), "--log", str(tmp_path / "l")])
    err = capsys.readouterr().err
    assert rc == 2 and no_network == []
    assert "is a directory" in err and "risk config" in err


def test_paper_config_runtime_yaml_says_expected_kind(
    no_network: list[str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = cli.main(["paper", "--config", RUNTIME, "--log", str(tmp_path / "l")])
    err = capsys.readouterr().err
    assert rc == 2 and no_network == []
    assert "unknown keys" in err and "expected a risk config" in err
