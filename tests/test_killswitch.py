import json
from datetime import timedelta
from pathlib import Path

import pytest

from agenkit_trader.config import RiskConfig
from agenkit_trader.execution.venue import Position
from agenkit_trader.killswitch import KillSwitch
from agenkit_trader.log import DecisionLog
from fakes import T0, FakeVenue


def make(tmp_path: Path) -> tuple[KillSwitch, FakeVenue, Path]:
    v = FakeVenue(positions={"BTCUSDT": Position("BTCUSDT", 0.02, 50_000.0)})
    p = tmp_path / "ks.jsonl"
    return KillSwitch(v, DecisionLog(p), RiskConfig()), v, p


def records(p: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in p.read_text().splitlines()]


def test_trigger_flattens_and_halts(tmp_path: Path) -> None:
    ks, v, p = make(tmp_path)
    assert not ks.halted
    ks.trigger("manual")
    assert ks.halted
    assert v.positions == {}
    assert v.flatten_calls == 1
    assert records(p)[-1]["kind"] == "kill_switch"


def test_trigger_twice_flattens_once(tmp_path: Path) -> None:
    ks, v, _ = make(tmp_path)
    ks.trigger("a")
    ks.trigger("b")
    assert v.flatten_calls == 1
    assert ks.halted


def test_flatten_failure_still_halts_and_retries(tmp_path: Path) -> None:
    ks, v, p = make(tmp_path)
    v.fail_flatten = True
    ks.trigger("a")
    assert ks.halted
    assert records(p)[-1]["result"] == "flatten_failed"
    v.fail_flatten = False
    ks.trigger("b")
    assert v.flatten_calls == 2
    assert v.positions == {}
    ks.trigger("c")
    assert v.flatten_calls == 2


def test_flatten_does_not_use_risk_gate(tmp_path: Path) -> None:
    # The kill switch calls venue.flatten_all directly; no order is placed.
    ks, v, _ = make(tmp_path)
    ks.trigger("x")
    assert v.orders == []


def test_five_errors_in_window_halts(tmp_path: Path) -> None:
    ks, v, _ = make(tmp_path)
    for i in range(4):
        ks.record_api_error(T0 + timedelta(seconds=60 * i))
    assert not ks.halted
    ks.record_api_error(T0 + timedelta(seconds=299))
    assert ks.halted
    assert v.flatten_calls == 1


def test_errors_outside_window_do_not_halt(tmp_path: Path) -> None:
    ks, _, _ = make(tmp_path)
    for i in range(10):
        ks.record_api_error(T0 + timedelta(seconds=100 * i))
    assert not ks.halted


def test_reset_requires_exact_phrase(tmp_path: Path) -> None:
    ks, _, p = make(tmp_path)
    ks.trigger("x")
    for bad in ("no", "resume", "RESUME ", ""):
        with pytest.raises(ValueError):
            ks.reset(bad)
        assert ks.halted
    ks.reset("RESUME")
    assert not ks.halted
    results = [r["result"] for r in records(p) if r["kind"] == "kill_switch_reset"]
    assert results == ["rejected"] * 4 + ["reset"]


def test_reset_clears_error_window(tmp_path: Path) -> None:
    ks, _, _ = make(tmp_path)
    for i in range(5):
        ks.record_api_error(T0 + timedelta(seconds=i))
    ks.reset("RESUME")
    ks.record_api_error(T0 + timedelta(seconds=10))
    assert not ks.halted
