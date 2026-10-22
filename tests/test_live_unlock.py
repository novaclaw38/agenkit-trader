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
    return {
        "flag": (LiveConfig(), GOOD_ENV, STATS_OK, PHRASE, risk_path, good_log),
        "keys": (ENABLED, {}, STATS_OK, PHRASE, risk_path, good_log),
        "days": (
            ENABLED,
            GOOD_ENV,
            PaperStats(13.9, 31),
            PHRASE,
            risk_path,
            good_log,
        ),
        "trades": (
            ENABLED,
            GOOD_ENV,
            PaperStats(15.0, 29),
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
    write_paper_log(p, "f" * 64, trades=20)
    later = T0 + timedelta(days=20)
    extra = [
        _rec(
            later, "startup", "starting", {"risk_config_sha256": file_sha256(risk_path)}
        ),
        _rec(later + timedelta(hours=1), "order", "filled"),
    ]
    p.write_text(p.read_text() + "\n".join(extra) + "\n")
    with pytest.raises(RiskConfigNotRecorded):
        verify(ENABLED, GOOD_ENV, STATS_OK, PHRASE, risk_path, DecisionLog(p))


def test_risk_hash_missing_from_log_is_refused(tmp_path: Path, risk_path: Path) -> None:
    p = tmp_path / "p.jsonl"
    write_paper_log(p, None)
    with pytest.raises(RiskConfigNotRecorded):
        verify(ENABLED, GOOD_ENV, STATS_OK, PHRASE, risk_path, DecisionLog(p))
    p.write_text(_rec(T0, "order", "filled") + "\n")  # fills with no startup
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
