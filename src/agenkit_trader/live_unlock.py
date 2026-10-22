"""Human-only live-trading unlock gate (spec section 7).

``verify`` checks every condition for going live and returns an in-memory
``UnlockToken`` only if all of them hold. It is called from exactly one
place: the interactive ``agenkit live`` command (a static test enforces
this). The paper runtime never imports this module.

Unlocking validates the gate ONLY. Live order placement is not implemented
in this build: ``LiveVenue`` still refuses every call after an unlock.

Conditions, checked in this order (cheap, non-secret checks first; the
API keys are read last, only once everything else has passed):

1. ``config/live.yaml`` has ``enabled`` set to the boolean true
   (``LiveNotEnabled``).
2. The decision log is readable and well formed (``DecisionLogMalformed``)
   and has no halt without a later ``runtime/halt_reset`` (``LiveHalted``).
3. Paper record (from the decision log, never the state file) spans at least
   ``min_paper_days`` (``PaperDaysTooLow``) with at least
   ``min_paper_trades`` filled orders (``PaperTradesTooLow``).
4. Every paper run that filled an order recorded, at its startup, the
   sha256 of the risk config, and each of those equals the CURRENT risk
   config's hash, so limits were not loosened after paper trading
   (``RiskConfigNotRecorded``).
5. The typed phrase is exactly ``PHRASE`` (case-sensitive; only one
   trailing newline is removed) (``PhraseMismatch``).
6. ``BINANCE_LIVE_KEY`` and ``BINANCE_LIVE_SECRET`` are set and non-empty
   (``KeysMissing``).

Every attempt is appended to the decision log as a ``live_unlock`` record
(``unlocked`` or ``refused`` with the error class). The log never holds the
keys or the typed text, only whether the phrase matched.

The token cannot be built by hand (private sentinel), pickled or copied, is
never written to disk and is never read from the environment or a file:
``LiveVenue`` accepts only a token object that ``verify`` issued in this
process.
"""

from __future__ import annotations

import hmac
import json
import math
import weakref
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

from agenkit_trader.config import LiveConfig
from agenkit_trader.log import DecisionLog, file_sha256

PHRASE = "I UNDERSTAND THIS USES REAL MONEY"
KEY_ENV = "BINANCE_LIVE_KEY"
SECRET_ENV = "BINANCE_LIVE_SECRET"
LOG_KIND = "live_unlock"

# Records the paper runtime writes (runtime/loop.py, killswitch.py, cli.py).
_FILLED = ("filled", "filled_after_error")  # kind "order"
_HALT_KINDS = ("kill_switch", "halt")  # cleared by kind "runtime"/"halt_reset"
_STARTUP = ("startup", "starting")  # inputs.risk_config_sha256


class UnlockError(RuntimeError):
    """Live unlock refused. Messages never contain credentials or the
    typed phrase."""


class LiveNotEnabled(UnlockError):
    pass


class DecisionLogMalformed(UnlockError):
    pass


class LiveHalted(UnlockError):
    pass


class PaperDaysTooLow(UnlockError):
    pass


class PaperTradesTooLow(UnlockError):
    pass


class RiskConfigNotRecorded(UnlockError):
    pass


class PhraseMismatch(UnlockError):
    pass


class KeysMissing(UnlockError):
    pass


@dataclass(frozen=True)
class PaperStats:
    """Paper record from the decision log: ``days`` between the first and
    last filled paper order, ``trades`` = number of filled paper orders."""

    days: float
    trades: int

    def __post_init__(self) -> None:
        d, t = self.days, self.trades
        if isinstance(d, bool) or not isinstance(d, int | float):
            raise ValueError(f"days must be a number, got {d!r}")
        if not math.isfinite(d) or d < 0:
            raise ValueError(f"days must be finite and >= 0, got {d!r}")
        if isinstance(t, bool) or not isinstance(t, int) or t < 0:
            raise ValueError(f"trades must be an int >= 0, got {t!r}")


class _LiveCredentials:
    """Holds the live key pair; its repr never reveals them. (Nothing in this
    build signs or sends a request with them.)"""

    __slots__ = ("_key", "_secret")

    def __init__(self, key: str, secret: str) -> None:
        self._key = key
        self._secret = secret

    def __repr__(self) -> str:
        return "<credentials redacted>"

    __str__ = __repr__

    def __reduce_ex__(self, protocol: object) -> NoReturn:
        raise TypeError("credentials cannot be pickled or copied")


_SENTINEL = object()
_ISSUED: weakref.WeakSet[UnlockToken] = weakref.WeakSet()


class UnlockToken:
    """Proof, held in memory only, that ``verify`` passed in this process."""

    __slots__ = ("__weakref__", "_creds", "issued_at", "risk_config_sha256")

    def __init__(
        self,
        _sentinel: object,
        creds: _LiveCredentials,
        issued_at: datetime,
        risk_config_sha256: str,
    ) -> None:
        if _sentinel is not _SENTINEL:
            raise TypeError("UnlockToken is issued only by live_unlock.verify")
        self._creds = creds
        self.issued_at = issued_at
        self.risk_config_sha256 = risk_config_sha256

    def __repr__(self) -> str:
        when = self.issued_at.isoformat()
        return f"<UnlockToken issued_at={when} (credentials redacted)>"

    __str__ = __repr__

    def __reduce_ex__(self, protocol: object) -> NoReturn:
        raise TypeError("an UnlockToken cannot be pickled, copied or persisted")


def is_issued(token: object) -> bool:
    """True only for a token object issued by ``verify`` in this process."""
    try:
        return isinstance(token, UnlockToken) and token in _ISSUED
    except TypeError:
        return False


def _read_records(path: Path) -> list[dict[str, Any]]:
    """All decision-log records; a missing log is empty. Anything unreadable
    or malformed refuses (fail closed)."""
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise DecisionLogMalformed(f"decision log {path} is unreadable: {exc}") from exc
    out: list[dict[str, Any]] = []
    for n, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError as exc:
            raise DecisionLogMalformed(
                f"decision log {path} line {n} is not JSON ({exc})"
            ) from exc
        if not isinstance(rec, dict):
            raise DecisionLogMalformed(
                f"decision log {path} line {n} is not a JSON object"
            )
        rec["_ts"] = _parse_ts(rec.get("ts"), path, n)
        out.append(rec)
    return out


def _parse_ts(ts: object, path: Path, n: int) -> datetime:
    try:
        t = datetime.fromisoformat(str(ts)) if isinstance(ts, str) else None
    except ValueError:
        t = None
    if t is None or t.tzinfo is None:
        raise DecisionLogMalformed(
            f"decision log {path} line {n} has no valid UTC timestamp ({ts!r})"
        )
    return t.astimezone(UTC)


def _is_fill(rec: Mapping[str, Any]) -> bool:
    return rec.get("kind") == "order" and rec.get("result") in _FILLED


def _stats(recs: list[dict[str, Any]]) -> PaperStats:
    times = [r["_ts"] for r in recs if _is_fill(r)]
    if not times:
        return PaperStats(0.0, 0)
    days = (max(times) - min(times)).total_seconds() / 86400.0
    return PaperStats(days, len(times))


def paper_stats_from_log(log_path: Path) -> PaperStats:
    """Paper record from the decision log (never from the state file)."""
    return _stats(_read_records(Path(log_path)))


def _unreset_halt(recs: list[dict[str, Any]]) -> str | None:
    halt: str | None = None
    for r in recs:
        if r.get("kind") in _HALT_KINDS:
            halt = str(r.get("ts"))
        elif r.get("kind") == "runtime" and r.get("result") == "halt_reset":
            halt = None
    return halt


def _trading_risk_hashes(recs: list[dict[str, Any]]) -> set[str | None]:
    """Risk-config hash recorded at the startup of each paper run that filled
    an order (None: a fill with no recorded hash)."""
    current: str | None = None
    used: set[str | None] = set()
    for r in recs:
        if (r.get("kind"), r.get("result")) == _STARTUP:
            inputs = r.get("inputs")
            h = inputs.get("risk_config_sha256") if isinstance(inputs, dict) else None
            current = h if isinstance(h, str) and h else None
        elif _is_fill(r):
            used.add(current)
    return used


def record_refusal(log: DecisionLog, exc: UnlockError, inputs: dict[str, Any]) -> None:
    """Log a refused unlock attempt (also used by the CLI for refusals that
    happen before ``verify`` is reached)."""
    log.write(
        LOG_KIND,
        inputs,
        {"reason": type(exc).__name__, "message": str(exc)},
        "refused",
    )


def _phrase_matches(typed: object) -> bool:
    if not isinstance(typed, str):
        return False
    if typed.endswith("\n"):
        typed = typed[:-1]
    return hmac.compare_digest(typed.encode("utf-8"), PHRASE.encode("utf-8"))


def verify(
    live_cfg: LiveConfig,
    env: Mapping[str, str],
    paper_stats: PaperStats,
    typed_phrase: str,
    risk_config_path: Path,
    log: DecisionLog,
) -> UnlockToken:
    """Return an ``UnlockToken`` if every live condition holds, else raise
    the ``UnlockError`` subclass of the first failed condition. Each attempt
    is logged. Does NOT enable any order placement (not built)."""
    phrase_ok = _phrase_matches(typed_phrase)
    inputs: dict[str, Any] = {
        "risk_config": str(risk_config_path),
        "paper_days": getattr(paper_stats, "days", None),
        "paper_trades": getattr(paper_stats, "trades", None),
        "phrase_matched": phrase_ok,
    }
    try:
        token = _verify(live_cfg, env, paper_stats, phrase_ok, risk_config_path, log)
    except UnlockError as exc:
        record_refusal(log, exc, inputs)
        raise
    log.write(
        LOG_KIND,
        {**inputs, "risk_config_sha256": token.risk_config_sha256},
        {"note": "gate passed; live execution is not implemented in this build"},
        "unlocked",
    )
    return token


def _verify(
    live_cfg: LiveConfig,
    env: Mapping[str, str],
    paper_stats: PaperStats,
    phrase_ok: bool,
    risk_config_path: Path,
    log: DecisionLog,
) -> UnlockToken:
    if not isinstance(live_cfg, LiveConfig) or live_cfg.enabled is not True:
        raise LiveNotEnabled(
            "live trading is not enabled: set `enabled` to true in config/live.yaml "
            "yourself (no code does this)"
        )
    recs = _read_records(log.path)
    halt = _unreset_halt(recs)
    if halt is not None:
        raise LiveHalted(
            f"the decision log {log.path} records a halt at {halt} with no later "
            "reset; inspect it and run `agenkit reset-halt` first"
        )
    if not isinstance(paper_stats, PaperStats):
        raise PaperDaysTooLow("no paper record was supplied")
    if paper_stats.days < live_cfg.min_paper_days:
        raise PaperDaysTooLow(
            f"paper record spans {paper_stats.days:.2f} days; "
            f"{live_cfg.min_paper_days} required"
        )
    if paper_stats.trades < live_cfg.min_paper_trades:
        raise PaperTradesTooLow(
            f"paper record has {paper_stats.trades} filled orders; "
            f"{live_cfg.min_paper_trades} required"
        )
    try:
        current = file_sha256(Path(risk_config_path))
    except OSError as exc:
        raise RiskConfigNotRecorded(
            f"risk config {risk_config_path} is unreadable ({exc})"
        ) from exc
    used = _trading_risk_hashes(recs)
    if not used or used != {current}:
        raise RiskConfigNotRecorded(
            f"the current risk config {risk_config_path} (sha256 {current[:12]}...) "
            "is not the one recorded by every paper run that traded; paper trade "
            "with the exact limits you intend to use live"
        )
    if not phrase_ok:
        raise PhraseMismatch("the confirmation phrase was not typed exactly")
    # Secrets last: read only after every other condition has passed.
    missing = [name for name in (KEY_ENV, SECRET_ENV) if not env.get(name)]
    if missing:
        raise KeysMissing(
            "live API credentials missing: set "
            + " and ".join(missing)
            + " in your own shell"
        )
    creds = _LiveCredentials(env[KEY_ENV], env[SECRET_ENV])
    token = UnlockToken(_SENTINEL, creds, datetime.now(UTC), current)
    _ISSUED.add(token)
    return token
