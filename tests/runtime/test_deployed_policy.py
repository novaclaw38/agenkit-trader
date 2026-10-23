"""deployed.json entries must carry policy == \"runtime\": a research-policy
verdict does not validate what the runtime trades."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agenkit_trader.runtime.loop import DeployedError, load_deployed

BASE = {
    "name": "trend_breakout",
    "symbol": "ETHUSDT",
    "interval": "1h",
    "params": {"lookback": 48},
    "allowed_regimes": ["range", "trend"],
}


def _write(tmp_path: Path, entry: dict[str, object]) -> Path:
    p = tmp_path / "deployed.json"
    p.write_text(json.dumps([entry]))
    return p


def test_runtime_policy_is_accepted(tmp_path: Path) -> None:
    d = load_deployed(_write(tmp_path, {**BASE, "policy": "runtime"}))
    assert d[0].name == "trend_breakout" and d[0].symbol == "ETHUSDT"


def test_research_policy_is_refused_with_a_clear_message(tmp_path: Path) -> None:
    with pytest.raises(DeployedError, match="does not validate what the runtime"):
        load_deployed(_write(tmp_path, {**BASE, "policy": "research"}))


def test_entry_without_policy_is_refused(tmp_path: Path) -> None:
    with pytest.raises(DeployedError, match="does not validate what the runtime"):
        load_deployed(_write(tmp_path, BASE))


@pytest.mark.parametrize("bad", ["paper", "", 1, None, "Runtime", "runtime "])
def test_any_other_policy_is_refused(tmp_path: Path, bad: object) -> None:
    with pytest.raises(DeployedError, match="policy"):
        load_deployed(_write(tmp_path, {**BASE, "policy": bad}))


def test_one_research_entry_among_runtime_ones_refuses_the_file(
    tmp_path: Path,
) -> None:
    p = tmp_path / "deployed.json"
    p.write_text(
        json.dumps([{**BASE, "policy": "runtime"}, {**BASE, "policy": "research"}])
    )
    with pytest.raises(DeployedError, match=r"\[1\]"):
        load_deployed(p)


def test_other_extra_keys_still_refused(tmp_path: Path) -> None:
    with pytest.raises(DeployedError):
        load_deployed(_write(tmp_path, {**BASE, "policy": "runtime", "x": 1}))
