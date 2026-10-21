import json
from pathlib import Path

from agenkit_trader.log import DecisionLog, file_sha256


def test_append_only_jsonl(tmp_path: Path) -> None:
    p = tmp_path / "sub" / "d.jsonl"
    log = DecisionLog(p)
    log.write("risk", {"a": 1}, {"ok": True}, "approved")
    log.write("risk", {"a": 2}, {"ok": False}, "rejected")
    lines = p.read_text().splitlines()
    assert len(lines) == 2
    for line in lines:
        rec = json.loads(line)
        assert set(rec) == {"ts", "kind", "inputs", "output", "result"}
    DecisionLog(p).write("x", {}, {}, "done")
    assert len(p.read_text().splitlines()) == 3
    assert json.loads(p.read_text().splitlines()[0])["inputs"] == {"a": 1}


def test_file_sha256(tmp_path: Path) -> None:
    f = tmp_path / "f"
    f.write_bytes(b"abc")
    assert file_sha256(f) == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )
