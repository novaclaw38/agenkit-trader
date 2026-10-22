"""deployed.json entries may carry the validation ``policy`` (new schema)."""

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


@pytest.mark.parametrize("policy", ["research", "runtime"])
def test_policy_key_is_accepted(tmp_path: Path, policy: str) -> None:
    d = load_deployed(_write(tmp_path, {**BASE, "policy": policy}))
    assert d[0].name == "trend_breakout" and d[0].symbol == "ETHUSDT"


def test_entry_without_policy_still_loads(tmp_path: Path) -> None:
    assert load_deployed(_write(tmp_path, BASE))[0].interval == "1h"


@pytest.mark.parametrize("bad", ["paper", "", 1, None])
def test_unknown_policy_is_refused(tmp_path: Path, bad: object) -> None:
    with pytest.raises(DeployedError, match="policy"):
        load_deployed(_write(tmp_path, {**BASE, "policy": bad}))


def test_other_extra_keys_still_refused(tmp_path: Path) -> None:
    with pytest.raises(DeployedError):
        load_deployed(_write(tmp_path, {**BASE, "policy": "runtime", "x": 1}))
