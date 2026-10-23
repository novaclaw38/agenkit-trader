import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from ui_helpers import fill, make_paths, rec, stop, write_log

from agenkit_trader.ui import readers

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)


def test_decisions_missing_log(tmp_path: Path) -> None:
    out = readers.read_decisions(make_paths(tmp_path))
    assert out["rows"] == []
    assert "no decision log" in out["message"].lower()
    assert str(tmp_path) not in json.dumps(out)


def test_decisions_rows_and_fields(tmp_path: Path) -> None:
    p = make_paths(tmp_path)
    write_log(
        p.log,
        [
            rec(
                "2026-10-22T10:00:00+00:00",
                "signal",
                {"deployment": "trend_breakout|ETHUSDT|1h"},
                {"target": 1, "prev_target": 0},
                "decided",
            ),
            rec(
                "2026-10-22T10:00:01+00:00",
                "order",
                {"order": {"symbol": "ETHUSDT", "side": "buy", "client_id": "c1"}},
                {"allowed": False, "reason": "leverage too high", "max_qty": 0.0},
                "rejected",
            ),
            fill("2026-10-22T10:00:02+00:00", "buy", 0.1),
        ],
    )
    out = readers.read_decisions(p)
    sig, rej, fil = out["rows"]
    assert sig["symbol"] == "ETHUSDT" and sig["signal"] == "1"
    assert rej["gate"] == "rejected: leverage too high" and rej["client_id"] == "c1"
    assert fil["fee"] == "fee unknown" and fil["action"] == "order/filled"
    assert out["total"] == 3 and out["skipped"] == 0


def test_decisions_corrupt_lines_skipped_and_counted(tmp_path: Path) -> None:
    p = make_paths(tmp_path)
    write_log(
        p.log,
        [rec("2026-10-22T10:00:00+00:00", "runtime", result="started")],
        extra='{"truncated\n[1, 2]\n',
    )
    out = readers.read_decisions(p)
    assert len(out["rows"]) == 1 and out["skipped"] == 2


def test_decisions_limit_keeps_newest(tmp_path: Path) -> None:
    p = make_paths(tmp_path)
    write_log(p.log, [rec(f"2026-10-22T10:00:{i:02d}+00:00", "x") for i in range(30)])
    out = readers.read_decisions(p, limit=10)
    assert len(out["rows"]) == 10 and out["truncated"] is True
    assert out["rows"][-1]["ts"].endswith("10:00:29+00:00")


def test_decisions_non_utf8_is_clean_error(tmp_path: Path) -> None:
    p = make_paths(tmp_path)
    p.log.parent.mkdir()
    p.log.write_bytes(b"\xff\xfe\x00bad")
    out = readers.read_decisions(p)
    assert out["rows"] == [] and out["message"]


def test_status_research_only_when_no_log(tmp_path: Path) -> None:
    s = readers.read_status(make_paths(tmp_path), now=NOW)
    assert s["mode"] == "research-only"
    assert s["no_trades"] is True
    assert s["equity"] == [] and s["positions"] == []
    assert s["last_decision_ts"] is None


def test_status_paper_and_stale(tmp_path: Path) -> None:
    p = make_paths(tmp_path)
    write_log(p.log, [rec("2026-10-22T11:00:00+00:00", "runtime", result="started")])
    s = readers.read_status(p, now=NOW)
    assert s["mode"] == "paper" and s["stale"] is False
    late = datetime(2026, 10, 22, 14, 0, tzinfo=UTC)
    assert readers.read_status(p, now=late)["stale"] is True


def test_status_halted_from_log_until_reset(tmp_path: Path) -> None:
    p = make_paths(tmp_path)
    write_log(
        p.log,
        [
            rec("2026-10-22T09:00:00+00:00", "runtime", result="started"),
            rec(
                "2026-10-22T10:00:00+00:00",
                "kill_switch",
                {"reason": "max_drawdown"},
                {"halted": True},
                "flattened",
            ),
        ],
    )
    s = readers.read_status(p, now=NOW)
    assert s["mode"] == "halted" and "max_drawdown" in s["halt_reason"]
    write_log(
        p.log,
        [
            rec(
                "2026-10-22T10:00:00+00:00",
                "kill_switch",
                {"reason": "max_drawdown"},
                result="flattened",
            ),
            rec("2026-10-22T10:30:00+00:00", "runtime", result="halt_reset"),
        ],
    )
    assert readers.read_status(p, now=NOW)["mode"] == "research-only"


def test_status_halted_from_state_file(tmp_path: Path) -> None:
    p = make_paths(tmp_path)
    p.state.parent.mkdir()
    p.state.write_text(
        json.dumps({"halted": "2026-10-22T10:00:00+00:00", "peak_equity": 1000.0})
    )
    s = readers.read_status(p, now=NOW)
    assert s["mode"] == "halted"
    assert s["state"]["peak_equity"] == 1000.0


def test_status_corrupt_state_file_is_message_not_trace(tmp_path: Path) -> None:
    p = make_paths(tmp_path)
    p.state.parent.mkdir()
    p.state.write_text("{not json")
    s = readers.read_status(p, now=NOW)
    assert s["state"] is None and s["state_error"]
    assert str(tmp_path) not in json.dumps(s, default=str)
    assert "Traceback" not in json.dumps(s, default=str)


def test_status_positions_stops_and_equity_series(tmp_path: Path) -> None:
    p = make_paths(tmp_path)
    write_log(
        p.log,
        [
            fill("2026-10-22T08:00:00+00:00", "buy", 0.5, equity=1000.0),
            stop("2026-10-22T08:00:01+00:00", 1900.0),
            fill("2026-10-22T09:00:00+00:00", "buy", 0.5, equity=900.0),
            fill("2026-10-22T10:00:00+00:00", "sell", 0.25, equity=950.0),
        ],
    )
    s = readers.read_status(p, now=NOW)
    (pos,) = s["positions"]
    assert pos["symbol"] == "ETHUSDT" and abs(pos["net_qty"] - 0.75) < 1e-9
    assert pos["stop_price"] == 1900.0
    eq = s["equity"]
    assert [e["equity"] for e in eq] == [1000.0, 900.0, 950.0]
    assert abs(eq[1]["drawdown"] - 0.1) < 1e-12 and eq[0]["drawdown"] == 0.0
    assert s["no_trades"] is False


def test_status_flat_position_not_listed(tmp_path: Path) -> None:
    p = make_paths(tmp_path)
    write_log(
        p.log,
        [
            fill("2026-10-22T08:00:00+00:00", "buy", 0.5),
            stop("2026-10-22T08:00:01+00:00", 1900.0),
            fill("2026-10-22T09:00:00+00:00", "sell", 0.5),
        ],
    )
    assert readers.read_status(p, now=NOW)["positions"] == []


def test_status_live_gate_locked_and_thresholds(tmp_path: Path) -> None:
    p = make_paths(tmp_path)
    write_log(
        p.log,
        [
            fill("2026-10-20T08:00:00+00:00", "buy", 0.5),
            fill("2026-10-22T08:00:00+00:00", "sell", 0.5),
        ],
    )
    g = readers.read_status(p, now=NOW)["live_gate"]
    assert g["state"] == "locked"
    assert g["paper_trades"] == 2 and g["min_paper_trades"] == 30
    assert abs(g["paper_days"] - 2.0) < 1e-9 and g["min_paper_days"] == 14
    assert g["enabled"] is False


def test_status_live_gate_unreadable_config(tmp_path: Path) -> None:
    p = make_paths(tmp_path)
    (p.config_dir / "live.yaml").write_text("enabled: [")
    g = readers.read_status(p, now=NOW)["live_gate"]
    assert g["state"] == "locked" and g["error"]
    assert str(tmp_path) not in json.dumps(g)


def test_research_real_repo_files() -> None:
    from ui_helpers import REPO

    p = readers.UiPaths.default()
    assert p.research_dir == REPO / "research"
    r = readers.read_research(p)
    assert r["trials_total"] == 123 and len(r["candidates"]) == 6
    assert r["deployed"] == [] and "nothing passed the gate" in r["deployed_message"]
    c = r["candidates"][0]
    assert c["policy"] == "research" and c["n_trials"] == 9
    assert c["holdout"]["n_trades"] == 0


def test_research_missing_and_corrupt(tmp_path: Path) -> None:
    p = make_paths(tmp_path)
    r = readers.read_research(p)
    assert r["candidates"] == [] and r["report_error"] and r["trials_error"]
    (p.research_dir / "REPORT.json").write_text("[{")
    (p.research_dir / "trials.json").write_text('{"total": "x"}')
    (p.research_dir / "deployed.json").write_text("nope")
    r = readers.read_research(p)
    assert r["report_error"] and r["trials_error"] and r["deployed_error"]
    assert str(tmp_path) not in json.dumps(r)


def test_research_deployed_entry_and_policy(tmp_path: Path) -> None:
    p = make_paths(tmp_path)
    (p.research_dir / "REPORT.json").write_text(
        json.dumps(
            [
                {
                    "name": "a",
                    "symbol": "BTCUSDT",
                    "interval": "1h",
                    "passed": True,
                    "reasons": [],
                    "n_trials": 3,
                    "pvalue": 0.01,
                    "policy": "runtime",
                    "holdout": {"sharpe": 1.0, "n_trades": 40},
                }
            ]
        )
    )
    (p.research_dir / "trials.json").write_text('{"total": 3}')
    (p.research_dir / "deployed.json").write_text(
        json.dumps(
            [{"name": "a", "symbol": "BTCUSDT", "interval": "1h", "policy": "runtime"}]
        )
    )
    r = readers.read_research(p)
    assert r["candidates"][0]["policy"] == "runtime"
    assert r["trials_total"] == 3 and len(r["deployed"]) == 1


def test_risk_real_configs() -> None:
    r = readers.read_risk(readers.UiPaths.default())
    assert r["risk"]["max_drawdown"] == 0.10 and r["risk"]["max_leverage"] == 3.0
    assert r["runtime"]["stop_atr_mult"] == 3.0
    assert r["symbol_rules"]["BTCUSDT"]["tick"] == 0.1
    assert r["errors"] == {}


def test_risk_missing_and_corrupt(tmp_path: Path) -> None:
    p = make_paths(tmp_path, real_configs=False)
    (p.config_dir / "risk.yaml").write_text("max_drawdown: [")
    r = readers.read_risk(p)
    assert r["risk"] is None and set(r["errors"]) >= {"risk", "runtime", "gate"}
    assert str(tmp_path) not in json.dumps(r)


def test_failure_section_extracted(tmp_path: Path) -> None:
    f = readers.read_failure(make_paths(tmp_path))
    assert f["text"].startswith("## 12. HOW COULD THIS SYSTEM FAIL?")
    assert "## 11." not in f["text"]


def test_failure_missing(tmp_path: Path) -> None:
    p = make_paths(tmp_path)
    p.spec.write_text("# no section\n")
    f = readers.read_failure(p)
    assert f["text"] == "" and f["error"]
    p.spec.unlink()
    assert readers.read_failure(p)["error"]


def _big_log(p: Path, n: int = 400) -> None:
    write_log(
        p.log,
        [
            fill(f"2026-10-{1 + i // 24:02d}T{i % 24:02d}:00:00+00:00", "buy", 0.1)
            for i in range(n)
        ],
    )


def test_log_under_cap_not_truncated(tmp_path: Path) -> None:
    p = make_paths(tmp_path)
    _big_log(p, 5)
    assert readers.read_decisions(p)["log_truncated"] is False
    assert readers.read_status(p, now=NOW)["log_truncated"] is False


def test_large_log_reads_tail_only_and_is_honest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = make_paths(tmp_path)
    _big_log(p)
    monkeypatch.setattr(readers, "MAX_LOG_BYTES", 8192)
    d = readers.read_decisions(p)
    assert d["log_truncated"] is True
    assert 0 < len(d["rows"]) < 400
    assert d["skipped"] == 0  # partial first line dropped, not counted
    assert d["rows"][-1]["ts"].startswith("2026-10-17T15")
    s = readers.read_status(p, now=NOW)
    assert s["log_truncated"] is True
    assert s["no_trades"] is False
    assert s["positions"] == [] and "log too large" in s["positions_note"]
    g = s["live_gate"]
    assert g["paper_days"] is None and g["paper_trades"] is None
    assert "log too large" in g["paper_note"]
    assert s["mode"].startswith("unknown")
    assert s["last_decision_ts"].startswith("2026-10-17T15")


def test_nan_cleaning() -> None:
    out = readers.clean({"a": float("nan"), "b": [float("inf"), 1.5], "c": "x"})
    assert out == {"a": None, "b": [None, 1.5], "c": "x"}


def test_live_gate_thresholds_met_wording(tmp_path: Path) -> None:
    p = make_paths(tmp_path)
    (p.config_dir / "live.yaml").write_text(
        "enabled: true\nmin_paper_days: 1\nmin_paper_trades: 2\n"
    )
    write_log(
        p.log,
        [
            fill("2026-10-20T08:00:00+00:00", "buy", 0.5),
            fill("2026-10-22T08:00:00+00:00", "sell", 0.5),
        ],
    )
    g = readers.read_status(p, now=NOW)["live_gate"]
    assert g["state"] == "thresholds met"
    assert "not implemented in this build" in g["note"]
