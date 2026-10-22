"""Run every candidate strategy x symbol through the validation gate.

Writes research/REPORT.md (+ evidence), research/trials.json (via the trial
counter, never reset) and deployed.json (written last). Data is cached under
data/cache (git-ignored).
"""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

import pandas as pd
import yaml

from agenkit_trader.backtest.costs import CostModel
from agenkit_trader.config import load_gate_config
from agenkit_trader.data.align import align_asof
from agenkit_trader.data.archive import load_funding, load_klines
from agenkit_trader.strategies import REGISTRY
from agenkit_trader.strategies.funding_reversion import FUNDING_AVAILABLE_LAG
from agenkit_trader.validation.gate import Candidate, evaluate_candidates
from agenkit_trader.validation.report import write_report
from agenkit_trader.validation.trials import TrialCounter

ROOT = Path(__file__).resolve().parent.parent


def load_costs(path: Path) -> CostModel:
    with open(path) as fh:
        d = yaml.safe_load(fh)
    return CostModel(
        taker_fee=float(d["taker_fee"]),
        maker_fee=float(d["maker_fee"]),
        half_spread_bps=float(d["half_spread_bps"]),
        slippage_bps=float(d["slippage_bps"]),
    )


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
    return ap


def main(argv: list[str] | None = None) -> None:
    a = build_parser().parse_args(argv)
    start, end = date.fromisoformat(a.start), date.fromisoformat(a.end)

    gate_cfg = load_gate_config(ROOT / "config" / "gate.yaml")
    costs = load_costs(ROOT / "config" / "research.yaml")
    counter = TrialCounter(a.out.parent / "trials.json")
    candidates: list[Candidate] = []
    for sym in a.symbols:
        bars = load_klines(sym, a.interval, start, end, a.cache)
        funding = load_funding(sym, start, end, a.cache)
        aux = {"funding": align_asof(bars, funding, FUNDING_AVAILABLE_LAG)}
        print(f"{sym}: {len(bars)} bars {bars.index[0]} .. {bars.index[-1]}")
        for strat in REGISTRY.values():
            candidates.append(
                Candidate(strat, bars, aux, sym, a.interval, funding=funding)
            )
    # every candidate's grid points are counted BEFORE any is evaluated, and
    # all are tested against that final total (order-independent p-values)
    verdicts = evaluate_candidates(candidates, costs, gate_cfg, counter)
    for v in verdicts:
        print(f"  {v.symbol} {v.name}: {'PASS' if v.passed else 'REJECT'}")
    write_report(verdicts, a.out)
    print(f"total trials counted: {counter.total}")
    print(pd.Series([v.passed for v in verdicts]).sum(), "of", len(verdicts), "passed")


if __name__ == "__main__":
    main()
