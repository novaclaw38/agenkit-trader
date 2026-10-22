"""Live mode: locked by default, human-only unlock, no live execution built."""

from __future__ import annotations

import ast
import copy
import io
import json
import pickle
import re
import shutil
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from agenkit_trader import cli, live_unlock
from agenkit_trader.config import LiveConfig, load_live_config
from agenkit_trader.execution import live
from agenkit_trader.execution.live import (
    LiveLocked,
    LiveNotImplemented,
    LiveVenue,
)
from agenkit_trader.execution.venue import Order, Venue
from agenkit_trader.live_unlock import (
    PHRASE,
    DecisionLogMalformed,
    KeysMissing,
    LiveHalted,
    LiveNotEnabled,
    PaperDaysTooLow,
    PaperStats,
    PaperStatsMismatch,
    PaperTradesTooLow,
    PhraseMismatch,
    RiskConfigNotRecorded,
    UnlockError,
    UnlockToken,
    paper_stats_from_log,
    verify,
)
from agenkit_trader.log import DecisionLog, file_sha256

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "agenkit_trader"
KEY = "k3y-VALUE-should-never-leak-1234"
SECRET = "s3cret-VALUE-should-never-leak-5678"
GOOD_ENV = {"BINANCE_LIVE_KEY": KEY, "BINANCE_LIVE_SECRET": SECRET}
T0 = datetime(2026, 9, 1, tzinfo=UTC)

# --------------------------------------------------------------------------
# helpers


def _rec(ts: datetime, kind: str, result: str, inputs: Any = None) -> str:
    return json.dumps(
        {
            "ts": ts.isoformat(),
            "kind": kind,
            "inputs": inputs or {},
            "output": {},
            "result": result,
        },
        sort_keys=True,
    )


def write_paper_log(
    path: Path,
    risk_sha: str | None,
    *,
    days: float = 15.0,
    trades: int = 31,
    extra: list[str] | None = None,
) -> None:
    lines = [
        _rec(
            T0 - timedelta(minutes=1),
            "startup",
            "starting",
            {"risk_config_sha256": risk_sha},
        )
    ]
    for i in range(trades):
        frac = i / (trades - 1) if trades > 1 else 0.0
        ts = T0 + timedelta(days=days * frac)
        lines.append(_rec(ts, "order", "filled", {"kind": "entry"}))
        lines.append(_rec(ts, "stop", "placed"))
    lines += extra or []
    path.write_text("\n".join(lines) + "\n")


@pytest.fixture
def risk_path(tmp_path: Path) -> Path:
    p = tmp_path / "risk.yaml"
    shutil.copy(ROOT / "config" / "risk.yaml", p)
    return p


@pytest.fixture
def good_log(tmp_path: Path, risk_path: Path) -> DecisionLog:
    p = tmp_path / "paper.jsonl"
    write_paper_log(p, file_sha256(risk_path))
    return DecisionLog(p)


ENABLED = LiveConfig(enabled=True, min_paper_days=14, min_paper_trades=30)
STATS_OK = PaperStats(days=15.0, trades=31)


class RecordingEnv(Mapping[str, str]):
    """Mapping that records every access to it."""

    def __init__(self, data: dict[str, str]) -> None:
        self._d = dict(data)
        self.accessed: list[str] = []

    def __getitem__(self, k: str) -> str:
        self.accessed.append(k)
        return self._d[k]

    def __iter__(self) -> Iterator[str]:
        self.accessed.append("<iter>")
        return iter(self._d)

    def __len__(self) -> int:
        self.accessed.append("<len>")
        return len(self._d)

    def __contains__(self, k: object) -> bool:
        self.accessed.append(str(k))
        return k in self._d


def records(log: DecisionLog) -> list[dict[str, Any]]:
    return [json.loads(x) for x in log.path.read_text().splitlines() if x.strip()]


# --------------------------------------------------------------------------
# LiveConfig and config/live.yaml


def test_committed_live_yaml_is_disabled() -> None:
    cfg = load_live_config(ROOT / "config" / "live.yaml")
    assert cfg.enabled is False
    assert cfg.min_paper_days == 14
    assert cfg.min_paper_trades == 30


def test_live_config_default_is_disabled() -> None:
    assert LiveConfig().enabled is False


@pytest.mark.parametrize(
    "kw",
    [
        {"enabled": "true"},
        {"enabled": 1},
        {"enabled": None},
        {"min_paper_days": 0},
        {"min_paper_days": -1},
        {"min_paper_days": float("nan")},
        {"min_paper_days": float("inf")},
        {"min_paper_days": True},
        {"min_paper_days": "14"},
        {"min_paper_trades": 0},
        {"min_paper_trades": 2.5},
        {"min_paper_trades": True},
        {"min_paper_trades": float("inf")},
    ],
)
def test_live_config_rejects_bad_values(kw: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        LiveConfig(**kw)


def test_load_live_config_rejects_string_enabled_and_unknown_keys(
    tmp_path: Path,
) -> None:
    p = tmp_path / "live.yaml"
    p.write_text('enabled: "true"\nmin_paper_days: 14\nmin_paper_trades: 30\n')
    with pytest.raises(ValueError):
        load_live_config(p)
    p.write_text("enabled: false\nbypass: 1\n")
    with pytest.raises(ValueError):
        load_live_config(p)


# --------------------------------------------------------------------------
# LiveVenue


VENUE_METHODS = sorted(
    n for n in dir(Venue) if not n.startswith("_") and callable(getattr(Venue, n))
)
ORDER = Order("BTCUSDT", "buy", 0.001, 50000.0, 49000.0, "x")
ARGS: dict[str, tuple[Any, ...]] = {
    "get_bars": ("BTCUSDT", "1h", 10),
    "symbol_rules": ("BTCUSDT",),
    "get_mark_price": ("BTCUSDT",),
    "get_funding": ("BTCUSDT", 1),
    "place_order": (ORDER,),
    "place_stop": (ORDER,),
    "get_order": ("BTCUSDT", "cid"),
    "cancel_stop": ("BTCUSDT", "cid"),
    "cancel_open_orders": ("BTCUSDT",),
}


def test_venue_method_list_is_complete() -> None:
    assert "place_order" in VENUE_METHODS and "flatten_all" in VENUE_METHODS
    assert len(VENUE_METHODS) >= 12


@pytest.mark.parametrize("name", VENUE_METHODS)
def test_every_live_venue_method_is_locked_by_default(name: str) -> None:
    v = LiveVenue()
    with pytest.raises(LiveLocked):
        getattr(v, name)(*ARGS.get(name, ()))


@pytest.mark.parametrize("name", VENUE_METHODS)
def test_forged_tokens_do_not_unlock(name: str) -> None:
    forged = object.__new__(UnlockToken)
    for tok in (forged, "token", True, {"unlocked": True}):
        v = LiveVenue(tok)  # type: ignore[arg-type]
        with pytest.raises(LiveLocked):
            getattr(v, name)(*ARGS.get(name, ()))


@pytest.mark.parametrize("name", VENUE_METHODS)
def test_unlocked_venue_still_places_nothing(
    name: str, good_log: DecisionLog, risk_path: Path
) -> None:
    tok = verify(ENABLED, GOOD_ENV, STATS_OK, PHRASE, risk_path, good_log)
    v = LiveVenue(tok)
    with pytest.raises(LiveNotImplemented, match="not implemented in this build"):
        getattr(v, name)(*ARGS.get(name, ()))


def test_live_module_has_no_network_code() -> None:
    src = (SRC / "execution" / "live.py").read_text()
    tree = ast.parse(src)
    imported = {
        a.name.split(".")[0]
        for n in ast.walk(tree)
        if isinstance(n, ast.Import)
        for a in n.names
    } | {
        (n.module or "").split(".")[0]
        for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom)
    }
    assert not imported & {"httpx", "requests", "urllib", "socket", "http", "hmac"}
    assert "https://" not in src and "http://" not in src


def test_no_mainnet_endpoint_anywhere_in_src() -> None:
    forbidden = ("fapi.binance.com", "api.binance.com", "dapi.binance.com")
    for f in SRC.rglob("*.py"):
        text = f.read_text()
        for s in forbidden:
            assert s not in text, f"{f} names mainnet host {s}"


# --------------------------------------------------------------------------
# UnlockToken


def test_token_cannot_be_constructed_directly() -> None:
    with pytest.raises(TypeError):
        UnlockToken()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        UnlockToken(object())  # type: ignore[call-arg]


def test_token_cannot_be_pickled_or_copied(
    good_log: DecisionLog, risk_path: Path
) -> None:
    tok = verify(ENABLED, GOOD_ENV, STATS_OK, PHRASE, risk_path, good_log)
    with pytest.raises(TypeError):
        pickle.dumps(tok)
    with pytest.raises(TypeError):
        copy.copy(tok)
    with pytest.raises(TypeError):
        copy.deepcopy(tok)


def test_successful_verify_writes_no_file_but_the_log(
    tmp_path: Path, good_log: DecisionLog, risk_path: Path
) -> None:
    before = sorted(p.name for p in tmp_path.iterdir())
    verify(ENABLED, GOOD_ENV, STATS_OK, PHRASE, risk_path, good_log)
    assert sorted(p.name for p in tmp_path.iterdir()) == before


# --------------------------------------------------------------------------
# verify: each missing condition has its own error


def _scenarios(
    tmp_path: Path, risk_path: Path, good_log: DecisionLog
) -> dict[str, tuple[Any, ...]]:
    halted = tmp_path / "halted.jsonl"
    write_paper_log(
        halted,
        file_sha256(risk_path),
        extra=[_rec(T0 + timedelta(days=16), "kill_switch", "flattened")],
    )
    other = tmp_path / "other.jsonl"
    write_paper_log(other, "0" * 64)
    bad = tmp_path / "bad.jsonl"
    write_paper_log(bad, file_sha256(risk_path), extra=["{truncated"])
    short = tmp_path / "short.jsonl"
    write_paper_log(short, file_sha256(risk_path), days=13.9)
    few = tmp_path / "few.jsonl"
    write_paper_log(few, file_sha256(risk_path), trades=29)
    return {
        "flag": (LiveConfig(), GOOD_ENV, STATS_OK, PHRASE, risk_path, good_log),
        "keys": (ENABLED, {}, STATS_OK, PHRASE, risk_path, good_log),
        "days": (
            ENABLED,
            GOOD_ENV,
            paper_stats_from_log(short),
            PHRASE,
            risk_path,
            DecisionLog(short),
        ),
        "trades": (
            ENABLED,
            GOOD_ENV,
            paper_stats_from_log(few),
            PHRASE,
            risk_path,
            DecisionLog(few),
        ),
        "stats_mismatch": (
            ENABLED,
            GOOD_ENV,
            PaperStats(100.0, 1000),
            PHRASE,
            risk_path,
            good_log,
        ),
        "risk": (ENABLED, GOOD_ENV, STATS_OK, PHRASE, risk_path, DecisionLog(other)),
        "phrase": (ENABLED, GOOD_ENV, STATS_OK, "nope", risk_path, good_log),
        "halted": (ENABLED, GOOD_ENV, STATS_OK, PHRASE, risk_path, DecisionLog(halted)),
        "malformed": (ENABLED, GOOD_ENV, STATS_OK, PHRASE, risk_path, DecisionLog(bad)),
    }


EXPECTED = {
    "flag": LiveNotEnabled,
    "keys": KeysMissing,
    "days": PaperDaysTooLow,
    "trades": PaperTradesTooLow,
    "risk": RiskConfigNotRecorded,
    "phrase": PhraseMismatch,
    "halted": LiveHalted,
    "malformed": DecisionLogMalformed,
    "stats_mismatch": PaperStatsMismatch,
}


def test_each_missing_condition_raises_a_distinct_unlock_error(
    tmp_path: Path, risk_path: Path, good_log: DecisionLog
) -> None:
    raised: dict[str, type[BaseException]] = {}
    for name, args in _scenarios(tmp_path, risk_path, good_log).items():
        with pytest.raises(UnlockError) as ei:
            verify(*args)
        raised[name] = type(ei.value)
    assert raised == EXPECTED
    assert len(set(raised.values())) == len(raised)


def test_all_conditions_met_returns_token(
    good_log: DecisionLog, risk_path: Path
) -> None:
    tok = verify(ENABLED, GOOD_ENV, STATS_OK, PHRASE, risk_path, good_log)
    assert isinstance(tok, UnlockToken)


@pytest.mark.parametrize("which", ["BINANCE_LIVE_KEY", "BINANCE_LIVE_SECRET"])
def test_each_key_is_required(
    which: str, good_log: DecisionLog, risk_path: Path
) -> None:
    env = {k: v for k, v in GOOD_ENV.items() if k != which}
    with pytest.raises(KeysMissing) as ei:
        verify(ENABLED, env, STATS_OK, PHRASE, risk_path, good_log)
    assert which in str(ei.value)
    env = {**GOOD_ENV, which: ""}
    with pytest.raises(KeysMissing):
        verify(ENABLED, env, STATS_OK, PHRASE, risk_path, good_log)


def test_testnet_keys_do_not_count(good_log: DecisionLog, risk_path: Path) -> None:
    env = {"BINANCE_TESTNET_KEY": KEY, "BINANCE_TESTNET_SECRET": SECRET}
    with pytest.raises(KeysMissing):
        verify(ENABLED, env, STATS_OK, PHRASE, risk_path, good_log)


def test_enabled_must_be_real_true(good_log: DecisionLog, risk_path: Path) -> None:
    fake = object.__new__(LiveConfig)
    object.__setattr__(fake, "enabled", "true")
    object.__setattr__(fake, "min_paper_days", 14)
    object.__setattr__(fake, "min_paper_trades", 30)
    with pytest.raises(LiveNotEnabled):
        verify(fake, GOOD_ENV, STATS_OK, PHRASE, risk_path, good_log)
    with pytest.raises(LiveNotEnabled):
        verify(
            {"enabled": True},  # type: ignore[arg-type]
            GOOD_ENV,
            STATS_OK,
            PHRASE,
            risk_path,
            good_log,
        )


@pytest.mark.parametrize(
    "typed,ok",
    [
        (PHRASE, True),
        (PHRASE + "\n", True),
        (PHRASE + "\n\n", False),
        (PHRASE + " ", False),
        (" " + PHRASE, False),
        (PHRASE.lower(), False),
        (PHRASE + "\r\n", False),
        ("", False),
    ],
)
def test_phrase_is_exact(
    typed: str, ok: bool, good_log: DecisionLog, risk_path: Path
) -> None:
    assert PHRASE == "I UNDERSTAND THIS USES REAL MONEY"
    if ok:
        verify(ENABLED, GOOD_ENV, STATS_OK, typed, risk_path, good_log)
    else:
        with pytest.raises(PhraseMismatch):
            verify(ENABLED, GOOD_ENV, STATS_OK, typed, risk_path, good_log)


def test_risk_config_changed_after_paper_is_refused(
    good_log: DecisionLog, risk_path: Path
) -> None:
    risk_path.write_text(
        risk_path.read_text().replace("max_drawdown: 0.10", "max_drawdown: 0.50")
    )
    with pytest.raises(RiskConfigNotRecorded):
        verify(ENABLED, GOOD_ENV, STATS_OK, PHRASE, risk_path, good_log)


def test_risk_hash_must_cover_every_trading_run(
    tmp_path: Path, risk_path: Path
) -> None:
    p = tmp_path / "p.jsonl"
    # first run traded under a different risk file, second under the current
    write_paper_log(p, "f" * 64, trades=31)
    later = T0 + timedelta(days=20)
    extra = [
        _rec(
            later, "startup", "starting", {"risk_config_sha256": file_sha256(risk_path)}
        ),
        _rec(later + timedelta(hours=1), "order", "filled"),
    ]
    p.write_text(p.read_text() + "\n".join(extra) + "\n")
    st = paper_stats_from_log(p)
    with pytest.raises(RiskConfigNotRecorded):
        verify(ENABLED, GOOD_ENV, st, PHRASE, risk_path, DecisionLog(p))


def test_risk_hash_missing_from_log_is_refused(tmp_path: Path, risk_path: Path) -> None:
    p = tmp_path / "p.jsonl"
    write_paper_log(p, None)
    with pytest.raises(RiskConfigNotRecorded):
        verify(ENABLED, GOOD_ENV, STATS_OK, PHRASE, risk_path, DecisionLog(p))
    # fills with no startup record at all
    p.write_text("\n".join(p.read_text().splitlines()[1:]) + "\n")
    assert paper_stats_from_log(p) == STATS_OK
    with pytest.raises(RiskConfigNotRecorded):
        verify(ENABLED, GOOD_ENV, STATS_OK, PHRASE, risk_path, DecisionLog(p))


def test_missing_risk_file_is_refused(good_log: DecisionLog, tmp_path: Path) -> None:
    with pytest.raises(RiskConfigNotRecorded):
        verify(ENABLED, GOOD_ENV, STATS_OK, PHRASE, tmp_path / "nope.yaml", good_log)


def test_halt_reset_clears_halt(tmp_path: Path, risk_path: Path) -> None:
    p = tmp_path / "p.jsonl"
    write_paper_log(
        p,
        file_sha256(risk_path),
        extra=[
            _rec(T0 + timedelta(days=16), "halt", "halted"),
            _rec(T0 + timedelta(days=17), "runtime", "halt_reset"),
        ],
    )
    verify(ENABLED, GOOD_ENV, STATS_OK, PHRASE, risk_path, DecisionLog(p))


def test_env_is_not_touched_unless_everything_else_passed(
    tmp_path: Path, risk_path: Path, good_log: DecisionLog
) -> None:
    for name, args in _scenarios(tmp_path, risk_path, good_log).items():
        if name == "keys":
            continue
        env = RecordingEnv(GOOD_ENV)
        with pytest.raises(UnlockError):
            verify(args[0], env, *args[2:])
        assert env.accessed == [], name


def test_check_order_cheap_checks_first(
    tmp_path: Path, risk_path: Path, good_log: DecisionLog
) -> None:
    # everything wrong at once: the flag is reported first
    with pytest.raises(LiveNotEnabled):
        verify(LiveConfig(), {}, PaperStats(0, 0), "x", tmp_path / "no", good_log)
    # flag ok, everything else wrong: keys are NOT the first complaint
    with pytest.raises(UnlockError) as ei:
        verify(ENABLED, {}, PaperStats(0, 0), "x", risk_path, good_log)
    assert not isinstance(ei.value, KeysMissing)


def test_secrets_never_in_logs_reprs_or_errors(
    tmp_path: Path,
    risk_path: Path,
    good_log: DecisionLog,
    capsys: pytest.CaptureFixture[str],
) -> None:
    texts: list[str] = []
    tok = verify(ENABLED, GOOD_ENV, STATS_OK, PHRASE, risk_path, good_log)
    texts += [repr(tok), str(tok), repr(LiveVenue(tok)), repr(vars(type(tok)))]
    for args in _scenarios(tmp_path, risk_path, good_log).values():
        with pytest.raises(UnlockError) as ei:
            verify(*args)
        texts += [str(ei.value), repr(ei.value)]
    with pytest.raises(KeysMissing) as ki:
        verify(
            ENABLED, {"BINANCE_LIVE_KEY": KEY}, STATS_OK, PHRASE, risk_path, good_log
        )
    texts += [str(ki.value), repr(ki.value)]
    for f in tmp_path.glob("*.jsonl"):
        texts.append(f.read_text())
    out = capsys.readouterr()
    texts += [out.out, out.err]
    blob = "\n".join(texts)
    assert KEY not in blob and SECRET not in blob


def test_every_attempt_is_logged_without_phrase_text(
    tmp_path: Path, risk_path: Path, good_log: DecisionLog
) -> None:
    typed = "my secret-ish typed words"
    with pytest.raises(PhraseMismatch):
        verify(ENABLED, GOOD_ENV, STATS_OK, typed, risk_path, good_log)
    with pytest.raises(LiveNotEnabled):
        verify(LiveConfig(), GOOD_ENV, STATS_OK, PHRASE, risk_path, good_log)
    verify(ENABLED, GOOD_ENV, STATS_OK, PHRASE, risk_path, good_log)
    recs = [r for r in records(good_log) if r["kind"] == "live_unlock"]
    assert [r["result"] for r in recs] == ["refused", "refused", "unlocked"]
    assert recs[0]["output"]["reason"] == "PhraseMismatch"
    assert recs[0]["inputs"]["phrase_matched"] is False
    assert recs[1]["output"]["reason"] == "LiveNotEnabled"
    assert recs[2]["inputs"]["phrase_matched"] is True
    text = good_log.path.read_text()
    assert typed not in text and PHRASE not in text
    assert KEY not in text and SECRET not in text


# --------------------------------------------------------------------------
# PaperStats from the decision log


def test_paper_stats_from_log(tmp_path: Path) -> None:
    p = tmp_path / "p.jsonl"
    write_paper_log(
        p,
        "a" * 64,
        days=10.0,
        trades=5,
        extra=[
            _rec(T0 + timedelta(days=30), "order", "rejected"),
            _rec(T0 + timedelta(days=30), "order", "unknown"),
            _rec(T0 + timedelta(days=11), "order", "filled_after_error"),
        ],
    )
    st = paper_stats_from_log(p)
    assert st.trades == 6
    assert st.days == pytest.approx(11.0)


def test_paper_stats_missing_or_empty_log_is_zero(tmp_path: Path) -> None:
    assert paper_stats_from_log(tmp_path / "none.jsonl") == PaperStats(0.0, 0)
    (tmp_path / "e.jsonl").write_text("")
    assert paper_stats_from_log(tmp_path / "e.jsonl") == PaperStats(0.0, 0)


def test_paper_stats_ignores_state_file(tmp_path: Path) -> None:
    (tmp_path / "paper_state.json").write_text('{"trades": 999, "days": 999}')
    assert paper_stats_from_log(tmp_path / "p.jsonl") == PaperStats(0.0, 0)


@pytest.mark.parametrize(
    "bad",
    [
        "{oops",
        "[1, 2]",
        '"str"',
        json.dumps({"kind": "order", "result": "filled", "ts": "yesterday"}),
        json.dumps({"kind": "order", "result": "filled"}),
        json.dumps({"kind": "order", "result": "filled", "ts": "2026-09-01T00:00:00"}),
    ],
)
def test_malformed_log_refuses(tmp_path: Path, bad: str) -> None:
    p = tmp_path / "p.jsonl"
    write_paper_log(p, "a" * 64, extra=[bad])
    with pytest.raises(DecisionLogMalformed):
        paper_stats_from_log(p)


def test_paper_stats_validates_values() -> None:
    for bad in (
        (float("nan"), 1),
        (-1.0, 1),
        (1.0, -1),
        (1.0, 1.5),
        (1.0, True),
        (float("inf"), 1),
    ):
        with pytest.raises(ValueError):
            PaperStats(*bad)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# CLI `agenkit live`


class FakeTTY(io.StringIO):
    def __init__(self, text: str = "", tty: bool = True) -> None:
        super().__init__(text)
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def _live_yaml(tmp_path: Path, enabled: bool) -> Path:
    p = tmp_path / "live.yaml"
    p.write_text(
        f"enabled: {'true' if enabled else 'false'}\n"
        "min_paper_days: 14\nmin_paper_trades: 30\n"
    )
    return p


def run_live(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    risk_path: Path,
    log: DecisionLog,
    *,
    stdin: FakeTTY,
    stdout: FakeTTY,
    enabled: bool = True,
    env: dict[str, str] | None = None,
) -> int:
    for k in ("BINANCE_LIVE_KEY", "BINANCE_LIVE_SECRET"):
        monkeypatch.delenv(k, raising=False)
    for k, v in (GOOD_ENV if env is None else env).items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr("sys.stdin", stdin)
    monkeypatch.setattr("sys.stdout", stdout)
    return cli.main(
        [
            "live",
            "--live-config",
            str(_live_yaml(tmp_path, enabled)),
            "--config",
            str(risk_path),
            "--log",
            str(log.path),
        ]
    )


def test_cli_live_success_says_not_implemented(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    risk_path: Path,
    good_log: DecisionLog,
) -> None:
    out = FakeTTY()
    rc = run_live(
        monkeypatch,
        tmp_path,
        risk_path,
        good_log,
        stdin=FakeTTY(PHRASE + "\n"),
        stdout=out,
    )
    text = out.getvalue()
    assert rc == 0
    assert "Unlock gate passed" in text and "not implemented in this build" in text
    assert KEY not in text and SECRET not in text
    assert [r["result"] for r in records(good_log) if r["kind"] == "live_unlock"] == [
        "unlocked"
    ]
    orders = [r for r in records(good_log) if r["kind"] in ("order", "stop")]
    assert len(orders) == 2 * 31  # only the fixture's records: nothing placed


def test_cli_live_wrong_phrase_refuses(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    risk_path: Path,
    good_log: DecisionLog,
) -> None:
    rc = run_live(
        monkeypatch,
        tmp_path,
        risk_path,
        good_log,
        stdin=FakeTTY("i understand this uses real money\n"),
        stdout=FakeTTY(),
    )
    assert rc != 0


def test_cli_live_disabled_refuses(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    risk_path: Path,
    good_log: DecisionLog,
) -> None:
    rc = run_live(
        monkeypatch,
        tmp_path,
        risk_path,
        good_log,
        stdin=FakeTTY(PHRASE + "\n"),
        stdout=FakeTTY(),
        enabled=False,
    )
    assert rc != 0


@pytest.mark.parametrize("which", ["stdin", "stdout", "both"])
def test_cli_live_refuses_without_tty(
    which: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    risk_path: Path,
    good_log: DecisionLog,
) -> None:
    called: list[int] = []
    monkeypatch.setattr(cli, "verify", lambda *a, **k: called.append(1))
    stdin = FakeTTY(PHRASE + "\n", tty=which == "stdout")
    stdout = FakeTTY(tty=which == "stdin")
    rc = run_live(
        monkeypatch, tmp_path, risk_path, good_log, stdin=stdin, stdout=stdout
    )
    assert rc != 0
    assert called == []
    assert stdin.read() == PHRASE + "\n"  # the phrase was never read


def test_cli_live_takes_no_phrase_from_argv(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as ei:
        cli.main(["live", "--phrase", PHRASE])
    assert ei.value.code != 0


def test_cli_live_eof_refuses(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    risk_path: Path,
    good_log: DecisionLog,
) -> None:
    rc = run_live(
        monkeypatch, tmp_path, risk_path, good_log, stdin=FakeTTY(""), stdout=FakeTTY()
    )
    assert rc != 0


def test_cli_live_malformed_log_refuses_and_is_logged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, risk_path: Path
) -> None:
    p = tmp_path / "bad.jsonl"
    write_paper_log(p, file_sha256(risk_path), extra=["{truncated"])
    log = DecisionLog(p)
    rc = run_live(
        monkeypatch,
        tmp_path,
        risk_path,
        log,
        stdin=FakeTTY(PHRASE + "\n"),
        stdout=FakeTTY(),
    )
    assert rc != 0
    assert '"kind": "live_unlock"' in p.read_text()


def test_paper_has_no_live_option() -> None:
    with pytest.raises(SystemExit):
        cli.main(["paper", "--config", "x", "--live"])
    with pytest.raises(SystemExit):
        cli.main(["paper", "--config", "x", "--enable-live"])


# --------------------------------------------------------------------------
# static guarantees over every source file


def _src_files() -> list[Path]:
    files = sorted(SRC.rglob("*.py"))
    assert len(files) > 20  # really scanning the package
    return files


def _enclosing_functions(tree: ast.AST) -> dict[ast.AST, str]:
    owner: dict[ast.AST, str] = {}

    def visit(node: ast.AST, fn: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = (
                child.name
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
                else fn
            )
            owner[child] = name
            visit(child, name)

    visit(tree, "<module>")
    return owner


def test_verify_is_called_only_from_cli_live_command() -> None:
    sites: list[tuple[str, str]] = []
    for f in _src_files():
        tree = ast.parse(f.read_text())
        owner = _enclosing_functions(tree)
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call):
                continue
            fn = n.func
            name = (
                fn.id
                if isinstance(fn, ast.Name)
                else fn.attr
                if isinstance(fn, ast.Attribute)
                else None
            )
            if name == "verify":
                sites.append((f.relative_to(SRC).as_posix(), owner[n]))
    assert sites == [("cli.py", "_live")]


def test_textual_verify_calls_only_in_cli() -> None:
    pat = re.compile(r"(?<!def )\bverify\s*\(")
    hits = [
        f.relative_to(SRC).as_posix() for f in _src_files() if pat.search(f.read_text())
    ]
    assert hits == ["cli.py"]


def test_live_unlock_imported_only_by_cli_and_live_venue() -> None:
    importers: set[str] = set()
    for f in _src_files():
        tree = ast.parse(f.read_text())
        for n in ast.walk(tree):
            mods: list[str] = []
            if isinstance(n, ast.Import):
                mods = [a.name for a in n.names]
            elif isinstance(n, ast.ImportFrom):
                mods = [n.module or ""] + [f"{n.module}.{a.name}" for a in n.names]
            if any("live_unlock" in m for m in mods):
                importers.add(f.relative_to(SRC).as_posix())
    # cli.py runs the unlock; execution/live.py only asks whether a token
    # object was really issued by verify.
    assert importers == {"cli.py", "execution/live.py"}


def test_runtime_does_not_import_live_code() -> None:
    files = sorted((SRC / "runtime").rglob("*.py"))
    assert files
    for f in files:
        text = f.read_text()
        assert "live_unlock" not in text, f
        assert "execution.live" not in text, f
        tree = ast.parse(text)
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom):
                assert n.module not in ("agenkit_trader.execution.live",), f
                if n.module == "agenkit_trader.execution":
                    assert "live" not in {a.name for a in n.names}, f
            if isinstance(n, ast.Import):
                for a in n.names:
                    assert "live" not in a.name.split("."), f


def test_paper_command_does_not_reference_live_code() -> None:
    tree = ast.parse((SRC / "cli.py").read_text())
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == "_paper":
            body = ast.unparse(n)
            assert "verify" not in body and "live" not in body.lower()
            break
    else:
        pytest.fail("_paper not found")


def test_no_code_enables_live() -> None:
    pats = [
        re.compile(r"enabled\s*:\s*true", re.I),
        re.compile(r"""["']enabled["']\s*:\s*True"""),
        re.compile(r"enabled\s*=\s*True"),
    ]
    for f in _src_files():
        text = f.read_text()
        for p in pats:
            assert not p.search(text), f"{f}: {p.pattern}"


def test_no_code_writes_yaml_or_live_config() -> None:
    for f in _src_files():
        tree = ast.parse(f.read_text())
        for n in ast.walk(tree):
            if isinstance(n, ast.Attribute) and n.attr in (
                "dump",
                "safe_dump",
                "dump_all",
                "safe_dump_all",
            ):
                assert not (isinstance(n.value, ast.Name) and n.value.id == "yaml"), (
                    f"{f} writes YAML"
                )
        text = f.read_text()
        if "live.yaml" in text:
            # mentioned only in messages/docs of these; none of them writes it
            assert f.name in ("cli.py", "config.py", "live_unlock.py"), f


def test_token_never_read_from_env_or_files() -> None:
    for mod in (live_unlock, live):
        src = Path(mod.__file__ or "").read_text()
        tree = ast.parse(src)
        for n in ast.walk(tree):
            if isinstance(n, ast.Attribute):
                assert n.attr not in ("environ", "getenv"), mod.__name__
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
                assert n.func.id != "open", mod.__name__


def test_runs_that_filled_nothing_do_not_count_for_the_hash(
    tmp_path: Path, risk_path: Path
) -> None:
    p = tmp_path / "p.jsonl"
    later = T0 + timedelta(days=20)
    write_paper_log(
        p,
        file_sha256(risk_path),
        extra=[
            _rec(later, "startup", "starting", {"risk_config_sha256": "e" * 64}),
            _rec(later, "order", "rejected"),
            _rec(later, "order", "not_filled"),
        ],
    )
    verify(ENABLED, GOOD_ENV, STATS_OK, PHRASE, risk_path, DecisionLog(p))


# --------------------------------------------------------------------------
# fix round 1: verify derives the paper record from the log itself


def test_forged_paper_stats_are_refused_and_logged(
    tmp_path: Path, risk_path: Path
) -> None:
    p = tmp_path / "p.jsonl"
    write_paper_log(p, file_sha256(risk_path), trades=1)  # one fill only
    log = DecisionLog(p)
    with pytest.raises(PaperStatsMismatch):
        verify(ENABLED, GOOD_ENV, PaperStats(100.0, 1000), PHRASE, risk_path, log)
    recs = [r for r in records(log) if r["kind"] == "live_unlock"]
    assert recs[-1]["result"] == "refused"
    assert recs[-1]["output"]["reason"] == "PaperStatsMismatch"


def test_honest_stats_from_the_same_log_pass(
    good_log: DecisionLog, risk_path: Path
) -> None:
    st = paper_stats_from_log(good_log.path)
    assert st == STATS_OK
    assert isinstance(
        verify(ENABLED, GOOD_ENV, st, PHRASE, risk_path, good_log), UnlockToken
    )


@pytest.mark.parametrize("stats", [PaperStats(15.0, 30), PaperStats(14.9, 31)])
def test_understated_stats_are_also_a_mismatch(
    stats: PaperStats, good_log: DecisionLog, risk_path: Path
) -> None:
    with pytest.raises(PaperStatsMismatch):
        verify(ENABLED, GOOD_ENV, stats, PHRASE, risk_path, good_log)


def test_non_paperstats_object_is_refused(
    good_log: DecisionLog, risk_path: Path
) -> None:
    class Fake:
        days = 15.0
        trades = 31

    with pytest.raises(PaperStatsMismatch):
        verify(ENABLED, GOOD_ENV, Fake(), PHRASE, risk_path, good_log)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# fix round 1: a YAML parse error in live.yaml never echoes file content

LEAK = "SECRETLEAK0123456789abcdef"


@pytest.mark.parametrize(
    "content",
    [
        f'enabled: false\napi_key: "abc{LEAK}xyz\n',
        f"enabled: false\napi_key: 'abc{LEAK}xyz\n",
        f"enabled: [false\n{LEAK}: {{\n",
        f"\tapi_key: {LEAK}\n",
    ],
)
def test_yaml_parse_error_does_not_echo_file_content(
    content: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    risk_path: Path,
    good_log: DecisionLog,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live_cfg = tmp_path / "live.yaml"
    live_cfg.write_text(content)
    import yaml

    with pytest.raises(yaml.YAMLError):  # really a parse error
        yaml.safe_load(content)
    out = FakeTTY()
    monkeypatch.setattr("sys.stdin", FakeTTY(PHRASE + "\n"))
    monkeypatch.setattr("sys.stdout", out)
    rc = cli.main(
        [
            "live",
            "--live-config",
            str(live_cfg),
            "--config",
            str(risk_path),
            "--log",
            str(good_log.path),
        ]
    )
    err = capsys.readouterr().err
    assert rc != 0
    text = good_log.path.read_text()
    assert LEAK not in err and LEAK not in out.getvalue() and LEAK not in text
    assert "abc" not in err
    rec = [r for r in records(good_log) if r["kind"] == "live_unlock"][-1]
    assert rec["result"] == "refused"
    assert "line" in rec["output"]["message"]  # position is still reported


# --------------------------------------------------------------------------
# fix round 1: hardened static scanner for any route to verify / live code

_LIVE_MOD = re.compile(r"[\w.]*(live_unlock|execution\.live)[\w.]*")
_LIVE_ATTRS_OK = {"is_issued", "UnlockToken"}  # what execution/live.py may use


def scan_live_refs(source: str, rel: str) -> list[str]:
    """Violations: any route to ``verify`` or to the live modules outside
    ``cli._live``'s direct ``verify(...)`` call. ``rel`` is the path relative
    to the package (e.g. ``cli.py``)."""
    tree = ast.parse(source)
    owner = _enclosing_functions(tree)
    called = {
        id(n.func)
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    bad: list[str] = []

    def where(n: ast.AST) -> str:
        return f"{rel}:{getattr(n, 'lineno', '?')}"

    for n in ast.walk(tree):
        in_live = rel == "cli.py" and owner.get(n) == "_live"
        if isinstance(n, ast.Name) and n.id == "verify":
            if not (in_live and id(n) in called and isinstance(n.ctx, ast.Load)):
                bad.append(f"{where(n)} name 'verify' outside a direct call in _live")
        elif isinstance(n, ast.Name) and n.id in ("__import__", "import_module"):
            bad.append(f"{where(n)} dynamic import {n.id}")
        elif isinstance(n, ast.Attribute):
            if n.attr == "verify":
                bad.append(f"{where(n)} attribute .verify")
            if n.attr in ("import_module", "__import__"):
                bad.append(f"{where(n)} dynamic import .{n.attr}")
            if (
                isinstance(n.value, ast.Name)
                and n.value.id == "live_unlock"
                and n.attr not in _LIVE_ATTRS_OK
            ):
                bad.append(f"{where(n)} live_unlock.{n.attr}")
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            v = n.value
            if v == "verify":
                bad.append(f"{where(n)} string 'verify'")
            m = _LIVE_MOD.fullmatch(v)
            if m and not (rel == "live_unlock.py" and v == "live_unlock"):
                bad.append(f"{where(n)} string naming a live module: {v!r}")
        elif isinstance(n, ast.Import | ast.ImportFrom):
            mods = [a.name for a in n.names] if isinstance(n, ast.Import) else []
            if any(m.split(".")[0] == "importlib" for m in mods):
                bad.append(f"{where(n)} imports importlib")
            if isinstance(n, ast.ImportFrom):
                if (n.module or "").split(".")[0] == "importlib":
                    bad.append(f"{where(n)} imports from importlib")
                for a in n.names:
                    if a.name == "verify" and (
                        rel != "cli.py"
                        or a.asname is not None
                        or n.module != "agenkit_trader.live_unlock"
                    ):
                        bad.append(f"{where(n)} imports verify")
                    if "live" in a.name.split("_") and a.asname is not None:
                        bad.append(f"{where(n)} aliases {a.name}")
            for a in n.names if isinstance(n, ast.Import) else []:
                if _LIVE_MOD.fullmatch(a.name):
                    bad.append(f"{where(n)} import {a.name}")
    return bad


def test_scanner_finds_nothing_in_src_and_scripts() -> None:
    files = [(f, f.relative_to(SRC).as_posix()) for f in _src_files()]
    files += [(f, "scripts/" + f.name) for f in sorted((ROOT / "scripts").glob("*.py"))]
    found = [v for f, rel in files for v in scan_live_refs(f.read_text(), rel)]
    assert found == []


_LIVE_OK = """
from agenkit_trader.live_unlock import verify
def _live(a):
    tok = verify(1, 2, 3, 4, 5, 6)
    return tok
"""

EVASIONS = {
    "getattr": (
        "runtime/loop.py",
        "from agenkit_trader import live_unlock\ngetattr(live_unlock, 'verify')(1)\n",
    ),
    "getattr_in_cli": (
        "cli.py",
        "import agenkit_trader.live_unlock as m\n"
        "def _paper(a):\n    getattr(m, 'verify')(a)\n",
    ),
    "importlib": (
        "runtime/loop.py",
        "import importlib\n"
        "importlib.import_module('agenkit_trader.live_unlock').verify(1)\n",
    ),
    "importlib_from": (
        "cli.py",
        "from importlib import import_module\ndef _paper(a):\n    import_module('x')\n",
    ),
    "dunder_import": ("runtime/loop.py", "__import__('agenkit_trader.live_unlock')\n"),
    "sys_modules": (
        "cli.py",
        "import sys\ndef _paper(a):\n    sys.modules['agenkit_trader.live_unlock']\n",
    ),
    "alias_import": (
        "cli.py",
        "from agenkit_trader.live_unlock import verify as v\n"
        "def _paper(a):\n    v(a)\n",
    ),
    "rebind_module": ("cli.py", _LIVE_OK + "check = verify\n"),
    "rebind_in_live": (
        "cli.py",
        "from agenkit_trader.live_unlock import verify\n"
        "def _live(a):\n    v = verify\n    return v(a)\n",
    ),
    "pass_as_arg": (
        "cli.py",
        "from agenkit_trader.live_unlock import verify\n"
        "def _live(a):\n    return map(verify, a)\n",
    ),
    "call_outside_live": (
        "cli.py",
        "from agenkit_trader.live_unlock import verify\n"
        "def _paper(a):\n    verify(a)\n",
    ),
    "attribute_call": (
        "execution/live.py",
        "from agenkit_trader import live_unlock\nlive_unlock.verify(1)\n",
    ),
    "module_alias_attr": (
        "execution/live.py",
        "from agenkit_trader import live_unlock as lu\nlu.verify(1)\n",
    ),
    "live_attr_other": (
        "execution/live.py",
        "from agenkit_trader import live_unlock\nlive_unlock._verify(1)\n",
    ),
    "import_in_runtime": (
        "runtime/loop.py",
        "from agenkit_trader.live_unlock import verify\n",
    ),
    "import_live_module": ("runtime/loop.py", "import agenkit_trader.execution.live\n"),
    "globals_lookup": ("cli.py", "def _paper(a):\n    globals()['verify'](a)\n"),
}


def test_scanner_accepts_the_real_pattern() -> None:
    assert scan_live_refs(_LIVE_OK, "cli.py") == []


@pytest.mark.parametrize("name", sorted(EVASIONS))
def test_scanner_flags_evasions(name: str) -> None:
    rel, src = EVASIONS[name]
    assert scan_live_refs(src, rel), name


# --------------------------------------------------------------------------
# fix round 2: bad live.yaml values / key names are never echoed

SK = "sk_live_SECRET123"


@pytest.mark.parametrize(
    "content",
    [
        f"enabled: {SK}\n",
        f"min_paper_days: {SK}\n",
        f"min_paper_trades: {SK}\n",
        f"enabled: false\n{SK}: 1\n",
    ],
)
def test_bad_live_config_value_or_key_never_echoed(
    content: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    risk_path: Path,
    good_log: DecisionLog,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live_cfg = tmp_path / "live.yaml"
    live_cfg.write_text(content)
    out = FakeTTY()
    monkeypatch.setattr("sys.stdin", FakeTTY(PHRASE + "\n"))
    monkeypatch.setattr("sys.stdout", out)
    rc = cli.main(
        [
            "live",
            "--live-config",
            str(live_cfg),
            "--config",
            str(risk_path),
            "--log",
            str(good_log.path),
        ]
    )
    err = capsys.readouterr().err
    assert rc == 2
    assert SK not in err and SK not in out.getvalue()
    assert SK not in good_log.path.read_text()
    rec = [r for r in records(good_log) if r["kind"] == "live_unlock"][-1]
    assert rec["result"] == "refused" and rec["output"]["reason"] == "BadLiveConfig"


def test_check_keys_reports_count_only(tmp_path: Path) -> None:
    p = tmp_path / "live.yaml"
    p.write_text(f"enabled: false\n{SK}: 1\nother_{SK}: 2\n")
    with pytest.raises(ValueError, match="2 unknown keys") as ei:
        load_live_config(p)
    assert SK not in str(ei.value)


def test_live_config_value_errors_name_type_not_value() -> None:
    for kw in ({"enabled": SK}, {"min_paper_days": SK}, {"min_paper_trades": SK}):
        with pytest.raises(ValueError) as ei:
            LiveConfig(**kw)  # type: ignore[arg-type]
        assert SK not in str(ei.value) and "str" in str(ei.value)


class _AlwaysEq(float):
    def __eq__(self, other: object) -> bool:
        return True

    __hash__ = float.__hash__


def test_float_subclass_stats_are_refused(
    good_log: DecisionLog, risk_path: Path
) -> None:
    with pytest.raises(ValueError):
        PaperStats(_AlwaysEq(1000.0), 1000)
    forged = object.__new__(PaperStats)
    object.__setattr__(forged, "days", _AlwaysEq(1000.0))
    object.__setattr__(forged, "trades", 31)
    with pytest.raises(PaperStatsMismatch):
        verify(ENABLED, GOOD_ENV, forged, PHRASE, risk_path, good_log)


def test_paperstats_subclass_is_refused(good_log: DecisionLog, risk_path: Path) -> None:
    class Sub(PaperStats):
        def __eq__(self, other: object) -> bool:
            return True

        __hash__ = None  # type: ignore[assignment]

    with pytest.raises(PaperStatsMismatch):
        verify(ENABLED, GOOD_ENV, Sub(1.0, 1), PHRASE, risk_path, good_log)
