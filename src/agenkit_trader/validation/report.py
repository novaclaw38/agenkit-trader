"""Comparison report and ``deployed.json``.

``write_report(verdicts, path)`` writes three files, each atomically:

* ``path`` (Markdown): the comparison table (Strategy, Edge, Market,
  Timeframe, Sharpe, Max Drawdown, Trades, Cost Sensitivity, Verdict) plus,
  per strategy, the evidence and every rejection reason.
* ``path.with_suffix('.json')``: the full evidence, machine-readable.
* ``path.parent / 'deployed.json'`` (written LAST, after both report files,
  so a crash can never leave a new deployed.json beside an old report): a JSON list of
  ``{name, symbol, interval, params, allowed_regimes}`` for passing verdicts
  only; an empty list when nothing passes, so a stale file never survives.

Policy. Verdicts produced under the runtime-sizing policy
(``policy="runtime"``) are labelled: the Markdown gets a ``Simulation
policy:`` line and each JSON entry a ``policy`` key. Research-policy verdicts
(the committed run) are written exactly as before, without either.

JSON never contains ``NaN``/``Infinity``: a non-finite float ``x`` stored
under key ``k`` is written as ``k: null`` plus ``k_nonfinite: "inf" | "-inf" |
"nan"``.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np

from agenkit_trader.backtest.stats import Stats
from agenkit_trader.validation.gate import Verdict
from agenkit_trader.validation.trials import atomic_write_text

COLUMNS = [
    "Strategy",
    "Edge",
    "Market",
    "Timeframe",
    "Sharpe",
    "Max Drawdown",
    "Trades",
    "Cost Sensitivity",
    "Verdict",
]


def _safe(obj: Any) -> Any:
    """Recursively replace non-finite floats with null + a ``*_nonfinite`` key."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if isinstance(v, float) and not math.isfinite(v):
                out[str(k)] = None
                out[f"{k}_nonfinite"] = str(v)
            else:
                out[str(k)] = _safe(v)
        return out
    if isinstance(obj, list | tuple):
        return [
            None if isinstance(v, float) and not math.isfinite(v) else _safe(v)
            for v in obj
        ]
    if isinstance(obj, np.generic):  # numpy bool/int from DataFrame records
        return _safe(obj.item())
    return obj


def _dumps(obj: Any) -> str:
    return json.dumps(_safe(obj), indent=2, allow_nan=False, sort_keys=False) + "\n"


def _stats(s: Stats) -> dict[str, Any]:
    return asdict(s)


def _f(x: float, pct: bool = False) -> str:
    if not math.isfinite(x):
        return str(x)
    return f"{x:.1%}" if pct else f"{x:.2f}"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def evidence(v: Verdict) -> dict[str, Any]:
    out = _evidence(v)
    if v.policy != "research":
        out = {"policy": v.policy, **out}
    return out


def _evidence(v: Verdict) -> dict[str, Any]:
    return {
        "name": v.name,
        "edge": v.edge,
        "symbol": v.symbol,
        "interval": v.interval,
        "params": v.params,
        "passed": v.passed,
        "reasons": v.reasons,
        "flags": v.flags,
        "allowed_regimes": v.allowed_regimes,
        "restricted_to": v.restricted_to,
        "pvalue": v.pvalue,
        "n_trials": v.n_trials,
        "ruined": v.ruined,
        "train": _stats(v.train),
        "holdout": _stats(v.holdout),
        "cost_2x": _stats(v.cost_2x),
        "regimes": {k: _stats(s) for k, s in v.regimes.items()},
        "sweep": {
            "stable": v.sweep.stable,
            "reason": v.sweep.reason,
            "best_score": v.sweep.best_score,
            "neighbour_scores": v.sweep.neighbour_scores,
            "table": v.sweep.table.to_dict(orient="records"),
        },
    }


def write_report(verdicts: list[Verdict], path: Path) -> None:
    """Write the Markdown report, its JSON evidence and ``deployed.json``."""
    path = Path(path)
    lines = [
        "# Strategy validation report",
        "",
        "Generated from real backtest runs only. Sharpe is annualised, net of costs, "
        "on the hold-out. Cost Sensitivity is hold-out net return at the "
        "configured cost multiplier.",
        "",
        "| " + " | ".join(COLUMNS) + " |",
        "|" + "---|" * len(COLUMNS),
    ]
    policies = sorted({v.policy for v in verdicts} - {"research"})
    if policies:
        note = [
            f"Simulation policy: {', '.join(policies)} (sign of the signal, 1% "
            "risk at the runtime's ATR stop, intrabar exchange stop, no "
            "re-entry without a fresh signal transition; see "
            "backtest/runtime_policy.py).",
            "",
        ]
        lines[4:4] = note
    for v in verdicts:
        h = v.holdout
        verdict = "PASS" if v.passed else "REJECT: " + "; ".join(v.reasons)
        if v.passed and v.flags:
            verdict += " (" + "; ".join(v.flags) + ")"
        cells = [
            v.name,
            v.edge,
            v.symbol,
            v.interval,
            _f(h.sharpe),
            _f(h.max_drawdown, pct=True),
            str(h.n_trades),
            _f(v.cost_2x.total_return, pct=True),
            verdict,
        ]
        lines.append("| " + " | ".join(_cell(c) for c in cells) + " |")
    lines += ["", "## Evidence", ""]
    for v in verdicts:
        lines += [
            f"### {v.name} ({v.symbol} {v.interval})",
            "",
            f"- params (chosen on train): `{json.dumps(_safe(v.params))}`",
            f"- deflated-Sharpe p-value {v.pvalue:.4g} with n_trials={v.n_trials}",
            f"- allowed regimes: {', '.join(v.allowed_regimes) or 'none'}",
            f"- sweep stable: {v.sweep.stable} {v.sweep.reason}",
        ]
        for label, s in (
            ("train", v.train),
            ("hold-out", v.holdout),
            ("cost x", v.cost_2x),
        ):
            lines.append(
                f"- {label}: Sharpe {_f(s.sharpe)}, return {_f(s.total_return, True)}, "
                f"max DD {_f(s.max_drawdown, True)}, PF {_f(s.profit_factor)}, "
                f"win rate {_f(s.win_rate, True)}, trades {s.n_trades}"
            )
        for k, s in v.regimes.items():
            lines.append(
                f"- regime {k}: Sharpe {_f(s.sharpe)}, "
                f"return {_f(s.total_return, True)}, trades {s.n_trades}"
            )
        lines += [f"- reason: {r}" for r in v.reasons] + [
            f"- flag: {f}" for f in v.flags
        ]
        lines.append("")
    deployed = [
        {
            "name": v.name,
            "symbol": v.symbol,
            "interval": v.interval,
            "params": v.params,
            "allowed_regimes": v.allowed_regimes,
        }
        for v in verdicts
        if v.passed
    ]
    deployed_text = _dumps(deployed)  # serialise everything before writing anything
    evidence_text = _dumps([evidence(v) for v in verdicts])
    atomic_write_text(path, "\n".join(lines))
    atomic_write_text(path.with_suffix(".json"), evidence_text)
    # order matters: deployed.json must be the last file replaced
    atomic_write_text(path.parent / "deployed.json", deployed_text)
