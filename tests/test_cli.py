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
                }
            ]
        )
    )
    assert paper("--deployed", str(d), tmp_path=tmp_path) != 0
    assert "unknown strategy" in capsys.readouterr().err
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
                    "allowed_regimes": ["trend"],
                }
            ]
        )
    )
    assert paper("--deployed", str(d), "--runtime", RUNTIME, tmp_path=tmp_path) != 0
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
