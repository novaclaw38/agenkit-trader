"""Pure readers: files on disk in, JSON-able dicts out. Nothing here writes,
touches the network or raises on bad input: a missing or corrupt file becomes a
short message (never a stack trace, never a file path). Every call re-reads the
files."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from agenkit_trader.config import (
    load_gate_config,
    load_live_config,
    load_risk_config,
    load_runtime_config,
    load_symbol_rules,
)

REPO = Path(__file__).resolve().parents[3]
DECISION_LIMIT = 1000
STALE_AFTER = timedelta(hours=2)  # 1h bars plus margin
_FILLED = ("filled", "filled_after_error")
_HALT_KINDS = ("kill_switch", "halt")
_MAX_TEXT = 300


def _cfg(name: str) -> str:
    return f"{name}.yaml"


@dataclass(frozen=True)
class PaperRecord:
    """Paper record as ``live_unlock`` defines it (days between the first and
    last filled order, count of filled orders), recomputed here from the
    tolerantly read log: live_unlock may be imported only by the CLI."""

    days: float
    trades: int


def _paper_record(recs: list[dict[str, Any]]) -> PaperRecord:
    times = [
        t
        for r in recs
        if r.get("kind") == "order" and r.get("result") in _FILLED
        if (t := _parse_ts(r.get("ts"))) is not None
    ]
    if not times:
        return PaperRecord(0.0, 0)
    return PaperRecord((max(times) - min(times)).total_seconds() / 86400.0, len(times))


@dataclass(frozen=True)
class UiPaths:
    log: Path
    state: Path
    config_dir: Path
    research_dir: Path
    spec: Path

    @classmethod
    def default(cls) -> UiPaths:
        return cls(
            log=REPO / "logs" / "paper.jsonl",
            state=REPO / "state" / "paper_state.json",
            config_dir=REPO / "config",
            research_dir=REPO / "research",
            spec=REPO
            / "docs"
            / "superpowers"
            / "specs"
            / "2026-10-21-agenkit-trader-design.md",
        )


def _text(v: Any) -> str:
    s = v if isinstance(v, str) else str(v)
    return s if len(s) <= _MAX_TEXT else s[:_MAX_TEXT] + "..."


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, int | float):
        return None
    return float(v) if math.isfinite(v) else None


def _d(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _parse_ts(v: Any) -> datetime | None:
    try:
        t = datetime.fromisoformat(v) if isinstance(v, str) else None
    except ValueError:
        return None
    return t.astimezone(UTC) if t is not None and t.tzinfo else None


def _load_json(path: Path, what: str) -> tuple[Any, str | None]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, f"{what} not found"
    except (OSError, UnicodeDecodeError):
        return None, f"{what} could not be read"
    try:
        return json.loads(text), None
    except ValueError:
        return None, f"{what} is not valid JSON"


def _load_records(path: Path) -> tuple[list[dict[str, Any]], int, str | None]:
    """(records, skipped line count, error message). Tolerant: bad lines are
    skipped and counted."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [], 0, "no decision log yet"
    except (OSError, UnicodeDecodeError):
        return [], 0, "the decision log could not be read"
    recs: list[dict[str, Any]] = []
    skipped = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except ValueError:
            skipped += 1
            continue
        if isinstance(r, dict):
            recs.append(r)
        else:
            skipped += 1
    return recs, skipped, None


def _symbol(r: dict[str, Any]) -> str:
    inp = _d(r.get("inputs"))
    order = _d(inp.get("order"))
    if "symbol" in order:
        return _text(order["symbol"])
    dep = inp.get("deployment")
    if isinstance(dep, str) and dep.count("|") >= 1:
        return _text(dep.split("|")[1])
    pos = _d(inp.get("position"))
    return _text(pos["symbol"]) if "symbol" in pos else ""


def _row(r: dict[str, Any]) -> dict[str, str]:
    inp, out = _d(r.get("inputs")), _d(r.get("output"))
    kind, result = _text(r.get("kind", "")), _text(r.get("result", ""))
    signal = ""
    if kind == "signal" and "target" in out:
        signal = _text(out["target"])
    elif "side" in _d(inp.get("order")):
        signal = _text(inp["order"]["side"])
    gate = ""
    if isinstance(out.get("allowed"), bool):
        gate = "allowed" if out["allowed"] else f"rejected: {_text(out.get('reason'))}"
    elif "reason" in out:
        gate = _text(out["reason"])
    return {
        "ts": _text(r.get("ts", "")),
        "kind": kind,
        "symbol": _symbol(r),
        "signal": signal,
        "action": f"{kind}/{result}",
        "gate": gate,
        "client_id": _text(_d(inp.get("order")).get("client_id", "")),
        "fee": _text(out["fee"]) if "fee" in out else "",
    }


def read_decisions(paths: UiPaths, limit: int = DECISION_LIMIT) -> dict[str, Any]:
    recs, skipped, err = _load_records(paths.log)
    keep = recs[-limit:] if limit > 0 else []
    return {
        "rows": [_row(r) for r in keep],
        "total": len(recs),
        "skipped": skipped,
        "truncated": len(recs) > len(keep),
        "message": err,
        "funding_note": "funding is not recorded in the decision log",
    }


def _unreset_halt(recs: list[dict[str, Any]]) -> dict[str, Any] | None:
    halt: dict[str, Any] | None = None
    for r in recs:
        if r.get("kind") in _HALT_KINDS:
            halt = r
        elif r.get("kind") == "runtime" and r.get("result") == "halt_reset":
            halt = None
    return halt


def _positions(recs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    net: dict[str, float] = {}
    stops: dict[str, float] = {}
    for r in recs:
        out, inp = _d(r.get("output")), _d(r.get("inputs"))
        if r.get("kind") == "order" and r.get("result") in _FILLED:
            f = _d(out.get("fill"))
            q, sym = _num(f.get("qty")), f.get("symbol")
            if q is None or not isinstance(sym, str):
                continue
            net[sym] = net.get(sym, 0.0) + (q if f.get("side") == "buy" else -q)
            if abs(net[sym]) < 1e-9:
                net[sym] = 0.0
                stops.pop(sym, None)
        elif r.get("kind") == "stop" and r.get("result") == "placed":
            o = _d(inp.get("order"))
            p, sym = _num(o.get("stop_price")), o.get("symbol")
            if p is not None and isinstance(sym, str):
                stops[sym] = p
    return [
        {
            "symbol": s,
            "net_qty": q,
            "stop_price": stops.get(s),
            "source": "derived from the decision log, not read from the exchange",
        }
        for s, q in net.items()
        if q != 0.0
    ]


def _equity(recs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    peak = 0.0
    for r in recs:
        if r.get("kind") != "order":
            continue
        eq = _num(_d(_d(r.get("inputs")).get("state")).get("equity"))
        if eq is None or eq <= 0:
            continue
        peak = max(peak, eq)
        out.append(
            {"ts": _text(r.get("ts", "")), "equity": eq, "drawdown": 1 - eq / peak}
        )
    return out


def _live_gate(paths: UiPaths, stats: PaperRecord | None) -> dict[str, Any]:
    gate: dict[str, Any] = {
        "state": "locked",
        "enabled": None,
        "min_paper_days": None,
        "min_paper_trades": None,
        "paper_days": stats.days if stats else None,
        "paper_trades": stats.trades if stats else None,
        "error": None,
        "note": "the final unlock is only possible via the interactive "
        "`agenkit live`; this page cannot unlock anything",
    }
    try:
        cfg = load_live_config(paths.config_dir / _cfg("live"))
    except FileNotFoundError:
        gate["error"] = "live config not found"
        return gate
    except Exception:  # noqa: BLE001 - any config problem is shown as a message
        gate["error"] = "live config is unreadable or invalid"
        return gate
    gate.update(
        enabled=cfg.enabled,
        min_paper_days=cfg.min_paper_days,
        min_paper_trades=cfg.min_paper_trades,
    )
    if (
        cfg.enabled is True
        and stats is not None
        and stats.days >= cfg.min_paper_days
        and stats.trades >= cfg.min_paper_trades
    ):
        gate["state"] = "unlocked"
        gate["note"] = (
            "enabled and paper thresholds met; the log's risk-config check, the "
            "typed phrase and the keys are still checked by `agenkit live`"
        )
    return gate


def read_status(paths: UiPaths, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    recs, skipped, log_err = _load_records(paths.log)
    state: dict[str, Any] | None = None
    state_err: str | None = None
    raw, state_err = _load_json(paths.state, "state file")
    if state_err == "state file not found":
        state_err = None  # normal before the first paper run
    elif isinstance(raw, dict):
        state = {
            k: raw.get(k)
            for k in ("baseline_equity", "peak_equity", "day", "day_start_equity")
        }
        state["last_bar"] = _d(raw.get("last_bar"))
        state["halted"] = raw.get("halted")
    elif state_err is None:
        state_err = "state file has an unexpected shape"
    halt_rec = _unreset_halt(recs)
    state_halt = state.get("halted") if state else None
    halt_reason: str | None = None
    if halt_rec is not None:
        reason = _d(halt_rec.get("inputs")).get("reason") or halt_rec.get("result")
        halt_reason = f"{_text(halt_rec.get('kind'))}: {_text(reason)} at " + _text(
            halt_rec.get("ts")
        )
    elif state_halt:
        halt_reason = f"state file records a halt at {_text(state_halt)}"
    if halt_reason is not None:
        mode = "halted"
    elif any(r.get("kind") == "runtime" and r.get("result") == "started" for r in recs):
        mode = "paper"
    else:
        mode = "research-only"
    times = [t for t in (_parse_ts(r.get("ts")) for r in recs) if t is not None]
    last = max(times) if times else None
    stale = None if last is None else (now - last) > STALE_AFTER
    stats = _paper_record(recs) if log_err is None else None
    equity = _equity(recs)
    fills = sum(
        1 for r in recs if r.get("kind") == "order" and r.get("result") in _FILLED
    )
    return {
        "mode": mode,
        "halt_reason": halt_reason,
        "last_decision_ts": last.isoformat() if last else None,
        "stale": stale,
        "stale_after_hours": STALE_AFTER.total_seconds() / 3600,
        "state": state,
        "state_error": state_err,
        "log_message": log_err,
        "log_lines_skipped": skipped,
        "positions": _positions(recs),
        "equity": equity,
        "equity_note": "equity as logged with each order; drawdown from its peak",
        "no_trades": fills == 0,
        "live_gate": _live_gate(paths, stats),
    }


def _holdout_row(c: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": _text(c.get("name", "")),
        "symbol": _text(c.get("symbol", "")),
        "interval": _text(c.get("interval", "")),
        "passed": c.get("passed") is True,
        "policy": _text(c.get("policy", "research")),
        "n_trials": c.get("n_trials"),
        "pvalue": _num(c.get("pvalue")),
        "holdout": _d(c.get("holdout")),
        "reasons": [_text(x) for x in c.get("reasons", []) if isinstance(x, str)]
        if isinstance(c.get("reasons"), list)
        else [],
    }


def read_research(paths: UiPaths) -> dict[str, Any]:
    d = paths.research_dir
    rep, rep_err = _load_json(d / "REPORT.json", "research report")
    cands: list[dict[str, Any]] = []
    if rep_err is None:
        if isinstance(rep, list):
            cands = [_holdout_row(c) for c in rep if isinstance(c, dict)]
        else:
            rep_err = "research report has an unexpected shape"
    tr, tr_err = _load_json(d / "trials.json", "trial counter")
    total = None
    if tr_err is None:
        t = _d(tr).get("total")
        if isinstance(t, int) and not isinstance(t, bool) and t >= 0:
            total = t
        else:
            tr_err = "trial counter has an unexpected shape"
    dep, dep_err = _load_json(d / "deployed.json", "deployed list")
    deployed: list[dict[str, Any]] = []
    if dep_err is None:
        if isinstance(dep, list):
            deployed = [_holdout_row(x) for x in dep if isinstance(x, dict)]
        else:
            dep_err = "deployed list has an unexpected shape"
    return {
        "candidates": cands,
        "report_error": rep_err,
        "trials_total": total,
        "trials_error": tr_err,
        "deployed": deployed,
        "deployed_error": dep_err,
        "deployed_message": "nothing passed the gate"
        if dep_err is None and not deployed
        else None,
        "metric_note": "All metrics are hold-out, net of costs; the trial count "
        "is the number of configurations tried in total.",
    }


def read_risk(paths: UiPaths) -> dict[str, Any]:
    c = paths.config_dir
    errors: dict[str, str] = {}

    def load(name: str, fn: Any, file: str) -> dict[str, Any] | None:
        try:
            return asdict(fn(c / _cfg(file)))
        except FileNotFoundError:
            errors[name] = f"{file} config not found"
        except Exception:  # noqa: BLE001 - shown as a message, never a trace
            errors[name] = f"{file} config is unreadable or invalid"
        return None

    rules: dict[str, Any] | None = None
    try:
        rules = {
            k: asdict(v) for k, v in load_symbol_rules(c / _cfg("symbol_rules")).items()
        }
    except FileNotFoundError:
        errors["symbol_rules"] = "symbol rules file not found"
    except (OSError, ValueError, TypeError, yaml.YAMLError):
        errors["symbol_rules"] = "symbol rules file is unreadable or invalid"
    return {
        "risk": load("risk", load_risk_config, "risk"),
        "runtime": load("runtime", load_runtime_config, "runtime"),
        "gate": load("gate", load_gate_config, "gate"),
        "live": load("live", load_live_config, "live"),
        "symbol_rules": rules,
        "errors": errors,
    }


def read_failure(paths: UiPaths) -> dict[str, Any]:
    try:
        text = paths.spec.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {"text": "", "error": "the design spec could not be read"}
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.startswith("## 12.")), None)
    if start is None:
        return {"text": "", "error": "section 12 was not found in the design spec"}
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")),
        len(lines),
    )
    return {"text": "\n".join(lines[start:end]).strip(), "error": None}
