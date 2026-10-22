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
* ``agenkit live [--live-config config/live.yaml] [--config config/risk.yaml]
  [--log logs/paper.jsonl]``: interactive only (stdin and stdout must be a
  terminal). Checks the live unlock conditions (``live_unlock.verify``,
  the ONLY caller) after the user types the confirmation phrase. Live order
  placement is NOT implemented in this build: even a passed gate places
  nothing. See docs/GOING_LIVE.md.

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

import yaml

from agenkit_trader.config import (
    ConfigError,
    load_live_config,
    load_risk_config,
    load_runtime_config,
)
from agenkit_trader.execution.binance_testnet import BinanceTestnetVenue
from agenkit_trader.killswitch import RESUME_PHRASE, KillSwitch
from agenkit_trader.live_unlock import (
    PHRASE,
    UnlockError,
    paper_stats_from_log,
    record_refusal,
    verify,
)
from agenkit_trader.log import DecisionLog, file_sha256
from agenkit_trader.risk.gate import RiskGate
from agenkit_trader.runtime.loop import (
    DeployedError,
    StartupRefused,
    check_state_ready,
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


def _anchor(p: Path) -> Path:
    """Relative paths are anchored to the repository root, never the cwd, so
    the same command always finds the same state, log and configs."""
    p = Path(p).expanduser()
    return p if p.is_absolute() else ROOT / p


def _paper(a: argparse.Namespace) -> int:
    for name in ("config", "runtime", "gate", "deployed", "log", "state"):
        setattr(a, name, _anchor(getattr(a, name)))
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
            "state": str(a.state),
            "init_state": a.init_state,
        },
        {},
        "starting",
    )
    try:
        risk = load_risk_config(a.config)
        rt_cfg = load_runtime_config(a.runtime)
    except (OSError, ValueError, TypeError) as exc:
        return refuse(f"bad configuration: {exc}")
    try:
        deployed = load_deployed(a.deployed)
        validate_deployments(deployed, REGISTRY)
    except (DeployedError, StartupRefused) as exc:
        return refuse(str(exc))
    _err(f"paper: state file: {a.state}")
    _err(f"paper: decision log: {a.log}")
    try:
        check_state_ready(a.state, a.log, a.init_state)
    except StartupRefused as exc:
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
            max_cycles=1 if a.once else None,
            state_path=a.state,
            init_state=a.init_state,
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
    a.state, a.log = _anchor(a.state), _anchor(a.log)
    print(f"state file: {a.state}\ndecision log: {a.log}")
    phrase = input(f"Type {RESUME_PHRASE} to clear the halt: ")
    if phrase != RESUME_PHRASE:
        _err("reset-halt: confirmation phrase not given; halt kept")
        return 1
    try:
        cleared = reset_halt(a.state, DecisionLog(a.log))
    except StartupRefused as exc:
        _err(
            f"reset-halt: {exc}\n"
            f"Nothing was changed. To recover: inspect {a.log} and repair the "
            "malformed line by hand (e.g. a line truncated by a crash), or move "
            "the file aside (keep it for the record), then rerun "
            "`agenkit reset-halt`. With the log moved aside, the next "
            "`agenkit paper` start writes a fresh log; if the state file is also "
            "missing it needs --init-state, which is logged."
        )
        return EXIT_REFUSED
    print("halt cleared" if cleared else "no halt was recorded")
    return 0


class NotInteractive(UnlockError):
    pass


class BadLiveConfig(UnlockError):
    pass


def _yaml_where(exc: yaml.YAMLError) -> str:
    """Error class and line/column only; never the message or a snippet."""
    mark = getattr(exc, "problem_mark", None) or getattr(exc, "context_mark", None)
    where = (
        f" at line {mark.line + 1}, column {mark.column + 1}"
        if mark is not None
        else " (line unknown)"
    )
    return f"YAML parse error {type(exc).__name__}{where}; contents not shown"


def _live(a: argparse.Namespace) -> int:
    """Human-only unlock check. Never places an order (not built)."""
    for name in ("live_config", "config", "log"):
        setattr(a, name, _anchor(getattr(a, name)))
    log = DecisionLog(a.log)
    base = {"live_config": str(a.live_config), "risk_config": str(a.config)}

    def refuse(exc: UnlockError, log_it: bool = True) -> int:
        if log_it:
            record_refusal(log, exc, base)
        _err(f"live: refused: {exc}")
        return EXIT_REFUSED

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return refuse(
            NotInteractive(
                "`agenkit live` must be run by a person in an interactive "
                "terminal (stdin and stdout must be a TTY)"
            )
        )
    try:
        live_cfg = load_live_config(a.live_config)
    except yaml.YAMLError as exc:
        # PyYAML messages quote the offending line, which could hold a secret
        # pasted by mistake: report only the error class and the position.
        return refuse(
            BadLiveConfig(f"bad live config {a.live_config}: {_yaml_where(exc)}")
        )
    except ConfigError as exc:  # sanitized by config.py
        return refuse(BadLiveConfig(f"bad live config {a.live_config}: {exc}"))
    except OSError as exc:
        return refuse(
            BadLiveConfig(f"cannot read live config {a.live_config}: {exc.strerror}")
        )
    except (ValueError, TypeError) as exc:  # unsanitized: class name only
        return refuse(
            BadLiveConfig(f"bad live config {a.live_config}: {type(exc).__name__}")
        )
    try:
        stats = paper_stats_from_log(a.log)
    except UnlockError as exc:
        return refuse(exc)
    print(
        "agenkit live: checks the live-trading unlock conditions.\n"
        "Live order placement is NOT implemented in this build; nothing will be "
        "traded whatever the outcome. See docs/GOING_LIVE.md.\n"
        f"decision log: {a.log}\n"
        f"paper record: {stats.days:.2f} days, {stats.trades} filled orders "
        f"(need {live_cfg.min_paper_days} days, {live_cfg.min_paper_trades} orders)"
    )
    try:
        typed = input(f"Type {PHRASE} to continue: ")
    except EOFError:
        return refuse(UnlockError("no confirmation phrase was typed"))
    try:
        verify(live_cfg, os.environ, stats, typed, a.config, log)
    except UnlockError as exc:
        return refuse(exc, log_it=False)  # verify logged it
    print(
        "Unlock gate passed. Live execution is not implemented in this build: "
        "no order was placed and none can be. Going live needs a separate, "
        "reviewed task run off-sandbox (docs/GOING_LIVE.md)."
    )
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
    p.add_argument(
        "--init-state",
        action="store_true",
        help="create a NEW state file if none exists (new paper account)",
    )
    r = sub.add_parser("reset-halt", help="clear a persisted kill-switch halt")
    r.add_argument("--state", type=Path, default=Path("state/paper_state.json"))
    r.add_argument("--log", type=Path, default=Path("logs/paper.jsonl"))
    lv = sub.add_parser(
        "live", help="check the live unlock conditions (interactive; no trading)"
    )
    lv.add_argument("--live-config", type=Path, default=Path("config/live.yaml"))
    lv.add_argument("--config", type=Path, default=Path("config/risk.yaml"))
    lv.add_argument("--log", type=Path, default=Path("logs/paper.jsonl"))
    a = ap.parse_args(argv)
    if a.cmd == "paper":
        return _paper(a)
    if a.cmd == "live":
        return _live(a)
    return _reset_halt(a)


if __name__ == "__main__":
    sys.exit(main())
