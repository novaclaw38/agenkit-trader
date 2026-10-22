"""``agenkit`` command line.

* ``agenkit paper --config config/risk.yaml [--runtime config/runtime.yaml]
  [--deployed research/deployed.json] [--once]``: paper trading on the
  Binance Futures TESTNET. Refuses to start (non-zero exit, no network call)
  unless ``deployed.json`` lists at least one strategy signed off by the
  validation gate. There is no bypass. Keys come only from the environment
  variables BINANCE_TESTNET_KEY / BINANCE_TESTNET_SECRET.
* ``agenkit research [args...]``: runs ``scripts/run_research.py`` (repo
  checkout) with the given arguments.
* ``agenkit reset-halt``: clears a persisted kill-switch halt after the user
  types the confirmation phrase.

Exit codes: 0 ok, 2 refused to start / bad config, 3 halted by the kill
switch, 130 interrupted.
"""

from __future__ import annotations

import argparse
import os
import runpy
import sys
from datetime import UTC, datetime
from pathlib import Path

from agenkit_trader.config import (
    load_gate_config,
    load_risk_config,
    load_runtime_config,
)
from agenkit_trader.execution.binance_testnet import BinanceTestnetVenue
from agenkit_trader.killswitch import RESUME_PHRASE, KillSwitch
from agenkit_trader.log import DecisionLog, file_sha256
from agenkit_trader.risk.gate import RiskGate
from agenkit_trader.runtime.loop import (
    DeployedError,
    StartupRefused,
    load_deployed,
    reset_halt,
    run_loop,
    validate_deployments,
)
from agenkit_trader.strategies import REGISTRY

ROOT = Path(__file__).resolve().parents[2]
KEY_ENV = "BINANCE_TESTNET_KEY"
SECRET_ENV = "BINANCE_TESTNET_SECRET"

EXIT_REFUSED = 2
EXIT_HALTED = 3


def _err(msg: str) -> None:
    print(f"agenkit: {msg}", file=sys.stderr)


def _sha(path: Path) -> str | None:
    try:
        return file_sha256(path)
    except OSError:
        return None


def _paper(a: argparse.Namespace) -> int:
    log = DecisionLog(a.log)

    def refuse(msg: str) -> int:
        log.write("runtime", {}, {"reason": msg}, "refused")
        _err(f"paper: refusing to start: {msg}")
        return EXIT_REFUSED

    log.write(
        "startup",
        {
            "risk_config": str(a.config),
            "risk_config_sha256": _sha(a.config),
            "runtime_config": str(a.runtime),
            "runtime_config_sha256": _sha(a.runtime),
            "gate_config_sha256": _sha(a.gate),
            "deployed": str(a.deployed),
            "deployed_sha256": _sha(a.deployed),
            "once": a.once,
        },
        {},
        "starting",
    )
    try:
        risk = load_risk_config(a.config)
        rt_cfg = load_runtime_config(a.runtime)
        gate_cfg = load_gate_config(a.gate)
    except (OSError, ValueError, TypeError) as exc:
        return refuse(f"bad configuration: {exc}")
    try:
        deployed = load_deployed(a.deployed)
        validate_deployments(deployed, REGISTRY)
    except (DeployedError, StartupRefused) as exc:
        return refuse(str(exc))
    missing = [v for v in (KEY_ENV, SECRET_ENV) if not os.environ.get(v)]
    if missing:
        return refuse(
            "testnet API credentials missing: set the environment variables "
            + " and ".join(missing)
        )
    venue = BinanceTestnetVenue(
        key_env=KEY_ENV, secret_env=SECRET_ENV, recv_window_ms=rt_cfg.recv_window_ms
    )
    log.write("startup", {"venue": repr(venue)}, {}, "venue_ready")
    gate = RiskGate(risk, venue.symbol_rules, log)
    kill = KillSwitch(venue, log, risk)
    try:
        run_loop(
            venue,
            list(REGISTRY.values()),
            deployed,
            gate,
            kill,
            log,
            lambda: datetime.now(UTC),
            runtime_cfg=rt_cfg,
            regime_window=gate_cfg.regime_window,
            max_cycles=1 if a.once else None,
            state_path=a.state,
        )
    except StartupRefused as exc:
        return refuse(str(exc))
    except KeyboardInterrupt:
        log.write("runtime", {}, {}, "interrupted")
        _err("paper: interrupted; open positions and their stops are left as is")
        return 130
    if kill.halted:
        _err(
            f"paper: HALTED by the kill switch; see {a.log}. Positions were "
            "flattened (check the log for flatten_failed). Restart requires "
            "`agenkit reset-halt`."
        )
        return EXIT_HALTED
    return 0


def _reset_halt(a: argparse.Namespace) -> int:
    phrase = input(f"Type {RESUME_PHRASE} to clear the halt: ")
    if phrase != RESUME_PHRASE:
        _err("reset-halt: confirmation phrase not given; halt kept")
        return 1
    cleared = reset_halt(a.state, DecisionLog(a.log))
    print("halt cleared" if cleared else "no halt was recorded")
    return 0


def _research(args: list[str]) -> int:
    script = ROOT / "scripts" / "run_research.py"
    if not script.exists():
        _err(f"research: {script} not found (run from a repository checkout)")
        return EXIT_REFUSED
    old = sys.argv
    sys.argv = [str(script), *args]
    try:
        runpy.run_path(str(script), run_name="__main__")
    finally:
        sys.argv = old
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "research":
        return _research(argv[1:])
    ap = argparse.ArgumentParser(prog="agenkit", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("research", help="run scripts/run_research.py")
    p = sub.add_parser("paper", help="paper trade deployed strategies on testnet")
    p.add_argument("--config", type=Path, required=True, help="risk config")
    p.add_argument("--runtime", type=Path, default=Path("config/runtime.yaml"))
    p.add_argument("--deployed", type=Path, default=Path("research/deployed.json"))
    p.add_argument("--gate", type=Path, default=Path("config/gate.yaml"))
    p.add_argument("--log", type=Path, default=Path("logs/paper.jsonl"))
    p.add_argument("--state", type=Path, default=Path("state/paper_state.json"))
    p.add_argument("--once", action="store_true", help="run a single cycle")
    r = sub.add_parser("reset-halt", help="clear a persisted kill-switch halt")
    r.add_argument("--state", type=Path, default=Path("state/paper_state.json"))
    r.add_argument("--log", type=Path, default=Path("logs/paper.jsonl"))
    a = ap.parse_args(argv)
    if a.cmd == "paper":
        return _paper(a)
    return _reset_halt(a)


if __name__ == "__main__":
    sys.exit(main())
