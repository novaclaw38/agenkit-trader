"""Append-only JSONL decision log and file hashing."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class DecisionLog:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(
        self, kind: str, inputs: dict[str, Any], output: dict[str, Any], result: str
    ) -> None:
        record = {
            "ts": datetime.now(UTC).isoformat(),
            "kind": kind,
            "inputs": inputs,
            "output": output,
            "result": result,
        }
        line = json.dumps(record, sort_keys=True, default=str) + "\n"
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line)
            f.flush()


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
