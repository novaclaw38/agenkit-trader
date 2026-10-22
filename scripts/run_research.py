"""Run every candidate strategy x symbol through the validation gate.

``--policy`` (required) selects what is simulated: ``runtime`` (what the
paper runtime trades; sizing, stops and kill-switch limits from
config/risk.yaml and config/runtime.yaml, lot/tick/min-notional from
config/symbol_rules.yaml) or ``research`` (the committed run's policy).

``--config`` loads a round configuration (symbols, interval, period, output,
strategy list with optional grid overrides, expected trial counts), e.g.
config/research_round2.yaml; without it the round-1 defaults below apply
unchanged. ``--plan`` prints what a run WOULD do and verifies the expected
trial counts, without loading data or touching the trial counter.

Writes research/REPORT.md (+ evidence), research/trials.json (via the trial
counter, never reset) and deployed.json (written last). Data is cached under
data/cache (git-ignored).
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from agenkit_trader.backtest.costs import CostModel
from agenkit_trader.backtest.runtime_policy import RuntimePolicy
from agenkit_trader.config import (
    load_gate_config,
    load_risk_config,
    load_runtime_config,
    load_symbol_rules,
)
from agenkit_trader.data.align import align_asof
from agenkit_trader.data.archive import load_funding, load_klines
from agenkit_trader.strategies import REGISTRY
from agenkit_trader.strategies.base import Strategy
from agenkit_trader.strategies.funding_reversion import FUNDING_AVAILABLE_LAG
from agenkit_trader.strategies.grid import with_grid
from agenkit_trader.validation.gate import Candidate, evaluate_candidates
from agenkit_trader.validation.report import write_report
from agenkit_trader.validation.sweep import grid_points
from agenkit_trader.validation.trials import TrialCounter

ROOT = Path(__file__).resolve().parent.parent

# Round 1's candidates. The registry also holds later strategies; a run
# without --config must keep testing exactly these (and counting their grids).
ROUND1_STRATEGIES = ("funding_reversion", "trend_breakout", "short_reversion")

_CONFIG_KEYS = {
    "symbols",
    "interval",
    "start",
    "end",
    "out",
    "trials",
    "strategies",
    "expect",
}


def load_round_config(path: Path) -> dict[str, Any]:
    """Parse and validate a round configuration file (no data, no network)."""
    with open(path) as fh:
        d = yaml.safe_load(fh)
    if not isinstance(d, dict):
        raise SystemExit(f"{path}: expected a mapping")
    unknown = set(d) - _CONFIG_KEYS
    if unknown:
        raise SystemExit(f"{path}: unknown keys {sorted(unknown)}")
    if not isinstance(d.get("strategies"), dict) or not d["strategies"]:
        raise SystemExit(f"{path}: 'strategies' must be a non-empty mapping")
    syms = d.get("symbols")
    if not isinstance(syms, list) or not syms or len(set(syms)) != len(syms):
        raise SystemExit(f"{path}: 'symbols' must be a non-empty list without repeats")
    return d


@dataclass(frozen=True)
class Plan:
    symbols: list[str]
    interval: str
    strategies: list[Strategy]
    per_strategy: dict[str, int]  # counted trials per strategy over all symbols

    @property
    def new_trials(self) -> int:
        return sum(self.per_strategy.values())


def build_plan(
    symbols: list[str],
    interval: str,
    cfg: dict[str, Any] | None,
) -> Plan:
    """Strategies (with any grid override) and the trials a run would count:
    one per grid point per symbol, exactly as ``evaluate_candidates``."""
    strategies: list[Strategy] = []
    if cfg is None:
        strategies = [REGISTRY[n] for n in ROUND1_STRATEGIES]
    else:
        for name, spec in cfg["strategies"].items():
            if name not in REGISTRY:
                raise SystemExit(f"config names unknown strategy {name!r}")
            spec = spec or {}
            extra = set(spec) - {"grid"}
            if extra:
                raise SystemExit(f"{name}: unknown keys {sorted(extra)}")
            base = REGISTRY[name]
            try:
                strategies.append(
                    with_grid(base, spec["grid"]) if "grid" in spec else base
                )
            except ValueError as e:
                raise SystemExit(str(e)) from e
    per = {s.name: len(grid_points(s.param_grid)) * len(symbols) for s in strategies}
    return Plan(list(symbols), interval, strategies, per)


def verify_plan(
    plan: Plan, expect: dict[str, Any] | None, current_total: int | None
) -> list[str]:
    """Mismatches between the plan and the config's ``expect`` block (empty =
    verified). ``current_total`` (the counter's value) enables the
    ``total_after`` check; pass None to skip it (plan mode never reads it)."""
    if not expect:
        return []
    bad: list[str] = []
    for name, n in (expect.get("per_strategy") or {}).items():
        got = plan.per_strategy.get(name)
        if got != n:
            bad.append(f"{name}: plan counts {got}, expected {n}")
    extra = set(plan.per_strategy) - set(
        expect.get("per_strategy") or plan.per_strategy
    )
    if extra:
        bad.append(f"strategies not in expect.per_strategy: {sorted(extra)}")
    if "new_trials" in expect and plan.new_trials != expect["new_trials"]:
        bad.append(
            f"new trials: plan counts {plan.new_trials}, "
            f"expected {expect['new_trials']}"
        )
    if (
        current_total is not None
        and "total_after" in expect
        and current_total + plan.new_trials != expect["total_after"]
    ):
        bad.append(
            f"trial counter: {current_total} + {plan.new_trials} != "
            f"expected {expect['total_after']}"
        )
    return bad


def print_plan(
    plan: Plan, a: argparse.Namespace, expect: dict[str, Any] | None
) -> None:
    print(f"PLAN (nothing loaded, nothing counted); policy: {a.policy}")
    print(f"symbols ({len(plan.symbols)}): {' '.join(plan.symbols)}")
    print(f"interval: {plan.interval}   period: {a.start} .. {a.end} (end-exclusive)")
    print(f"report/evidence dir: {a.out.parent}   trial counter: {a.trials} (not read)")
    for s in plan.strategies:
        pts = len(grid_points(s.param_grid))
        print(f"strategy {s.name}: grid {s.param_grid}")
        print(
            f"  {pts} grid points x {len(plan.symbols)} symbols = "
            f"{plan.per_strategy[s.name]} trials"
        )
    print(f"trials that WOULD be added: {plan.new_trials}")
    if expect:
        if "total_after" in expect:
            print(f"expected counter total afterwards: {expect['total_after']}")
        bad = verify_plan(plan, expect, None)
        print("VERIFIED: plan matches the expected counts" if not bad else "MISMATCH:")
        for line in bad:
            print(f"  - {line}")


def load_costs(path: Path) -> CostModel:
    with open(path) as fh:
        d = yaml.safe_load(fh)
    return CostModel(
        taker_fee=float(d["taker_fee"]),
        maker_fee=float(d["maker_fee"]),
        half_spread_bps=float(d["half_spread_bps"]),
        slippage_bps=float(d["slippage_bps"]),
    )


def runtime_policies(symbols: list[str]) -> dict[str, RuntimePolicy]:
    """Per-symbol runtime policy: risk.yaml, runtime.yaml and the static
    rules in symbol_rules.yaml (exits if a symbol has none: quantities and
    stops must be rounded like the runtime's)."""
    risk = load_risk_config(ROOT / "config" / "risk.yaml")
    rt = load_runtime_config(ROOT / "config" / "runtime.yaml")
    rules = load_symbol_rules(ROOT / "config" / "symbol_rules.yaml")
    missing = [s for s in symbols if s not in rules]
    if missing:
        raise SystemExit(
            f"no trading rules for {missing} in config/symbol_rules.yaml "
            "(needed by --policy runtime)"
        )
    return {s: RuntimePolicy(risk, rt, rules[s]) for s in symbols}


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--symbols",
        nargs="+",
        default=["BTCUSDT", "ETHUSDT"],
        help="USD-M perpetual symbols (default: %(default)s)",
    )
    ap.add_argument("--interval", default="1h", help="kline interval (default: 1h)")
    ap.add_argument("--start", default="2021-01-01", help="first day, ISO date")
    ap.add_argument("--end", default="2026-10-01", help="last day, ISO date")
    ap.add_argument(
        "--cache",
        type=Path,
        default=ROOT / "data" / "cache",
        help="download cache directory (default: data/cache)",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=ROOT / "research" / "REPORT.md",
        help="report path; trials.json and deployed.json are written beside it",
    )
    ap.add_argument(
        "--config",
        type=Path,
        default=None,
        help="round configuration YAML (e.g. config/research_round2.yaml); "
        "its symbols/interval/period/out/trials replace the defaults and its "
        "strategies carry grid overrides; default: round-1 behaviour",
    )
    ap.add_argument(
        "--trials",
        type=Path,
        default=None,
        help="trial counter file (default: trials.json beside --out); round 2 "
        "points it at the shared research/trials.json",
    )
    ap.add_argument(
        "--plan",
        action="store_true",
        help="print symbols, strategies, grid points and the trials that WOULD "
        "be added, verify them against the config's expected counts, then exit "
        "(loads no data, reads/writes no trial counter)",
    )
    ap.add_argument(
        "--policy",
        choices=["research", "runtime"],
        required=True,
        help="REQUIRED (a counted run must choose deliberately). 'runtime' = "
        "what the paper runtime trades: sign of the signal, 1%% risk at a "
        "3 x ATR(14) exchange-side stop, constant quantity, no re-entry "
        "after a stop-out, 3%%/10%% kill-switch halt; 'research' = the "
        "committed run's policy: +-1x equity rebalanced every bar, no stops",
    )
    return ap


def main(argv: list[str] | None = None) -> None:
    ap = build_parser()
    cfg_path = ap.parse_known_args(argv)[0].config
    cfg = load_round_config(cfg_path) if cfg_path else None
    if cfg:  # config values replace the defaults; explicit flags still win
        ap.set_defaults(
            **{
                k: (ROOT / cfg[k] if k in ("out", "trials") else cfg[k])
                for k in ("symbols", "interval", "start", "end", "out", "trials")
                if k in cfg
            }
        )
    a = ap.parse_args(argv)
    a.start, a.end = str(a.start), str(a.end)  # YAML may have parsed dates
    start, end = date.fromisoformat(a.start), date.fromisoformat(a.end)
    if a.trials is None:
        a.trials = a.out.parent / "trials.json"
    plan = build_plan(a.symbols, a.interval, cfg)
    expect = cfg.get("expect") if cfg else None
    if a.plan:
        print_plan(plan, a, expect)
        if verify_plan(plan, expect, None):
            raise SystemExit(1)
        return
    bad = verify_plan(plan, expect, TrialCounter(a.trials).total)
    if bad:
        raise SystemExit("plan check failed, nothing run: " + "; ".join(bad))

    gate_cfg = load_gate_config(ROOT / "config" / "gate.yaml")
    costs = load_costs(ROOT / "config" / "research.yaml")
    counter = TrialCounter(a.trials)
    policies = runtime_policies(a.symbols) if a.policy == "runtime" else {}
    candidates: list[Candidate] = []
    for sym in a.symbols:
        bars = load_klines(sym, a.interval, start, end, a.cache)
        funding = load_funding(sym, start, end, a.cache)
        aux = {"funding": align_asof(bars, funding, FUNDING_AVAILABLE_LAG)}
        print(f"{sym}: {len(bars)} bars {bars.index[0]} .. {bars.index[-1]}")
        for strat in plan.strategies:
            candidates.append(
                Candidate(
                    strat,
                    bars,
                    aux,
                    sym,
                    a.interval,
                    funding=funding,
                    runtime_policy=policies.get(sym),
                )
            )
    # every candidate's grid points are counted BEFORE any is evaluated, and
    # all are tested against that final total (order-independent p-values)
    print(f"simulation policy: {a.policy}")
    verdicts = evaluate_candidates(
        candidates, costs, gate_cfg, counter, policy=a.policy
    )
    for v in verdicts:
        print(f"  {v.symbol} {v.name}: {'PASS' if v.passed else 'REJECT'}")
    write_report(verdicts, a.out)
    print(f"total trials counted: {counter.total}")
    print(pd.Series([v.passed for v in verdicts]).sum(), "of", len(verdicts), "passed")


if __name__ == "__main__":
    main()
