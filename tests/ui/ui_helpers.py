"""Builders for a fake repo layout used by the UI tests."""

import json
import shutil
from pathlib import Path
from typing import Any

from agenkit_trader.ui.readers import UiPaths

REPO = Path(__file__).resolve().parents[2]


def rec(
    ts: str,
    kind: str,
    inputs: dict[str, Any] | None = None,
    output: dict[str, Any] | None = None,
    result: str = "ok",
) -> dict[str, Any]:
    return {
        "ts": ts,
        "kind": kind,
        "inputs": inputs or {},
        "output": output or {},
        "result": result,
    }


def write_log(path: Path, recs: list[dict[str, Any]], extra: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(r) + "\n" for r in recs) + extra
    path.write_text(text, encoding="utf-8")


def fill(
    ts: str, side: str, qty: float, equity: float = 1000.0, sym: str = "ETHUSDT"
) -> dict[str, Any]:
    return rec(
        ts,
        "order",
        {
            "kind": "entry",
            "order": {"symbol": sym, "side": side, "client_id": "cid-" + ts},
            "state": {"equity": equity},
        },
        {
            "fill": {"symbol": sym, "side": side, "qty": qty, "price": 2000.0},
            "fee": "fee unknown",
        },
        "filled",
    )


def stop(ts: str, price: float, sym: str = "ETHUSDT") -> dict[str, Any]:
    return rec(
        ts,
        "stop",
        {"order": {"symbol": sym, "stop_price": price, "client_id": "s-" + ts}},
        {"venue_id": "v1"},
        "placed",
    )


def make_paths(tmp_path: Path, real_configs: bool = True) -> UiPaths:
    """A temp layout; configs/spec/research copied from the repo by default."""
    cfg = tmp_path / "config"
    res = tmp_path / "research"
    cfg.mkdir()
    res.mkdir()
    if real_configs:
        for f in (REPO / "config").glob("*.yaml"):
            shutil.copy(f, cfg / f.name)
    spec = tmp_path / "spec.md"
    shutil.copy(
        REPO / "docs/superpowers/specs/2026-10-21-agenkit-trader-design.md", spec
    )
    return UiPaths(
        log=tmp_path / "logs" / "paper.jsonl",
        state=tmp_path / "state" / "paper_state.json",
        config_dir=cfg,
        research_dir=res,
        spec=spec,
    )
