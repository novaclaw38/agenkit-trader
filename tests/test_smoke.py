"""`agenkit smoke`: testnet plumbing checklist, exercised against FakeVenue only."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from agenkit_trader import cli, smoke
from agenkit_trader.config import RiskConfig, load_symbol_rules
from agenkit_trader.execution.binance_testnet import BinanceAPIError
from agenkit_trader.execution.venue import Order, Position, SymbolRules
from agenkit_trader.live_unlock import paper_stats_from_log
from agenkit_trader.log import DecisionLog
from fakes import T0, FakeVenue

ROOT = Path(__file__).resolve().parents[1]
FILE_RULES = load_symbol_rules(ROOT / "config" / "symbol_rules.yaml")
BTC = FILE_RULES["BTCUSDT"]
KEY, SECRET = "smoke-key-AAAAAAAAAAAAAAAA", "smoke-secret-BBBBBBBBBBBBBBBB"


def make_venue(**kw: Any) -> FakeVenue:
    return FakeVenue(
        rules={"BTCUSDT": BTC}, marks={"BTCUSDT": 50_000.0}, equity=1000.0, **kw
    )


@dataclass
class Run:
    report: smoke.SmokeReport
    log_path: Path
    venue: FakeVenue

    def records(self) -> list[dict[str, Any]]:
        return [json.loads(x) for x in self.log_path.read_text().splitlines()]

    def status(self) -> dict[int, str]:
        return {s.n: s.status for s in self.report.steps}


def go(
    tmp_path: Path,
    venue: FakeVenue | None = None,
    file_rules: dict[str, SymbolRules] | None = None,
    symbol: str = "BTCUSDT",
) -> Run:
    v = venue or make_venue()
    p = tmp_path / "smoke" / "smoke_log.jsonl"
    rep = smoke.run_smoke(
        v,
        symbol=symbol,
        risk=RiskConfig(),
        file_rules=FILE_RULES if file_rules is None else file_rules,
        log=smoke.SmokeLog(p),
        clock=lambda: T0,
        sleep=lambda s: None,
    )
    return Run(rep, p, v)


class NoStopVisible(FakeVenue):
    def place_stop(self, order: Order) -> str:
        self.stops_placed.append(order)
        return "123"  # accepted, but never shows up as an open order


class StopRejected(FakeVenue):
    def place_stop(self, order: Order) -> str:
        raise BinanceAPIError(
            "POST", "/fapi/v1/algoOrder", 400, -4120, "STOP_ORDER_SWITCH_ALGO"
        )


def test_happy_path_all_eight_pass_and_flat(tmp_path: Path) -> None:
    r = go(tmp_path)
    assert [s.n for s in r.report.steps] == list(range(1, 9))
    assert all(s.status == "pass" for s in r.report.steps), r.report.table()
    assert r.report.flat is True and r.report.exit_code == 0
    assert r.venue.positions == {} and r.venue.stops == []
    assert "pass" in r.report.table()


def test_every_log_record_is_tagged_smoke(tmp_path: Path) -> None:
    recs = go(tmp_path).records()
    assert recs and all(x["mode"] == "smoke" for x in recs)
    kinds = {x["kind"] for x in recs}
    assert {"smoke_run", "smoke_step", "risk_gate", "kill_switch"} <= kinds


def test_entry_is_smallest_valid_size_through_gate(tmp_path: Path) -> None:
    r = go(tmp_path)
    entry = r.venue.orders[0]
    assert entry.side == "buy" and not entry.reduce_only
    assert entry.qty * 50_000.0 >= BTC.min_notional
    # one lot less would break the minimum notional (smallest valid size,
    # allowing the small price-drift buffer)
    assert (entry.qty - BTC.lot) * 50_000.0 < BTC.min_notional * 1.05
    gate = [x for x in r.records() if x["kind"] == "risk_gate"]
    assert gate and gate[0]["result"] == "allowed"


def test_client_ids_deterministic_and_valid(tmp_path: Path) -> None:
    a = go(tmp_path / "a").venue.orders
    b = go(tmp_path / "b").venue.orders
    ids_a = [o.client_id for o in a]
    assert ids_a == [o.client_id for o in b]
    assert len(set(ids_a)) == len(ids_a)
    assert all(i.isalnum() and len(i) <= 36 for i in ids_a)


def test_gate_rejection_fails_entry_and_places_nothing(tmp_path: Path) -> None:
    v = make_venue()
    v.equity = v.day_start_equity = v.peak_equity = (
        10.0  # 1% risk budget far below the minimum-size risk
    )
    r = go(tmp_path, v)
    assert r.status()[3] == "fail"
    assert "risk_per_trade" in r.report.steps[2].reason
    assert v.orders == [] and r.report.exit_code == 1
    assert r.status()[4] == "skip"


def test_rule_mismatch_fails_step1_without_orders(tmp_path: Path) -> None:
    bad = {"BTCUSDT": SymbolRules(tick=0.1, lot=0.001, min_notional=100.0)}
    v = make_venue()
    r = go(tmp_path, v, file_rules=bad)
    assert r.status()[1] == "fail"
    assert "min_notional" in r.report.steps[0].reason
    assert v.orders == [] and v.flatten_calls == 0
    assert all(s.status == "skip" for s in r.report.steps[1:])


def test_unknown_symbol_in_config_fails_step1(tmp_path: Path) -> None:
    r = go(tmp_path, file_rules={})
    assert r.status()[1] == "fail" and "symbol_rules" in r.report.steps[0].reason


def test_preexisting_position_refused_and_never_flattened(tmp_path: Path) -> None:
    v = make_venue(positions={"BTCUSDT": Position("BTCUSDT", 0.5, 40_000.0)})
    r = go(tmp_path, v)
    assert r.status()[2] == "fail" and "not flat" in r.report.steps[1].reason
    assert v.flatten_calls == 0 and v.orders == []
    assert v.positions["BTCUSDT"].qty == 0.5
    assert r.report.flat is False and r.report.exit_code == 4


def test_stop_not_visible_reports_what_exchange_returned(tmp_path: Path) -> None:
    v = NoStopVisible(rules={"BTCUSDT": BTC}, marks={"BTCUSDT": 50_000.0})
    r = go(tmp_path, v)
    st = r.status()
    assert st[3] == "pass" and st[4] == "fail"
    reason = r.report.steps[3].reason
    assert "123" in reason and "not" in reason  # the id it returned, not listed
    assert all(st[n] == "skip" for n in range(5, 9))
    assert v.flatten_calls >= 1 and v.positions == {}  # cleanup flattened
    assert r.report.flat is True and r.report.exit_code == 1


def test_stop_api_error_text_is_reported(tmp_path: Path) -> None:
    v = StopRejected(rules={"BTCUSDT": BTC}, marks={"BTCUSDT": 50_000.0})
    r = go(tmp_path, v)
    assert r.status()[4] == "fail"
    assert (
        "-4120" in r.report.steps[3].reason and "HTTP 400" in r.report.steps[3].reason
    )
    assert v.flatten_calls >= 1 and r.report.flat is True


def test_stop_wrong_details_fail_step4(tmp_path: Path) -> None:
    class WrongStop(FakeVenue):
        def place_stop(self, order: Order) -> str:
            from dataclasses import replace

            return super().place_stop(replace(order, stop_price=1.0))

    r = go(tmp_path, WrongStop(rules={"BTCUSDT": BTC}, marks={"BTCUSDT": 5e4}))
    assert r.status()[4] == "fail" and "stop_price" in r.report.steps[3].reason


def test_flatten_failure_reported_not_flat_exit_4(tmp_path: Path) -> None:
    class StuckFlatten(FakeVenue):
        def flatten_all(self) -> None:
            self.flatten_calls += 1
            raise smoke.VenueError("boom")

    v = StuckFlatten(rules={"BTCUSDT": BTC}, marks={"BTCUSDT": 50_000.0})
    r = go(tmp_path, v)
    assert r.status()[8] == "fail"
    assert r.report.flat is False and r.report.exit_code == 4
    assert "NOT FLAT" in r.report.table()


def test_unreadable_account_at_end_is_unknown_not_flat(tmp_path: Path) -> None:
    class Dies(FakeVenue):
        def get_account(self) -> Any:
            if self.orders:
                raise smoke.VenueError("down")
            return super().get_account()

    v = Dies(rules={"BTCUSDT": BTC}, marks={"BTCUSDT": 50_000.0})
    r = go(tmp_path, v)
    assert r.report.flat is None and r.report.exit_code == 4


def test_secrets_never_logged_or_printed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(cli.KEY_ENV, KEY)
    monkeypatch.setenv(cli.SECRET_ENV, SECRET)

    class Leaky(FakeVenue):
        def place_stop(self, order: Order) -> str:
            raise smoke.VenueError(f"oops {KEY} and {SECRET}")

    r = go(tmp_path, Leaky(rules={"BTCUSDT": BTC}, marks={"BTCUSDT": 50_000.0}))
    blob = r.log_path.read_text() + r.report.table()
    assert KEY not in blob and SECRET not in blob and "[redacted]" in blob


def test_smoke_log_refuses_paper_log_name(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        smoke.SmokeLog(tmp_path / "paper.jsonl")


def test_live_unlock_stats_ignore_smoke(tmp_path: Path) -> None:
    paper = tmp_path / "paper.jsonl"
    log = DecisionLog(paper)
    log.write("order", {}, {}, "filled")
    before = paper_stats_from_log(paper)
    r = go(tmp_path)
    assert r.records()  # smoke wrote plenty (orders, fills, kill_switch)
    assert paper_stats_from_log(paper) == before
    assert paper.read_text().count("\n") == 1  # paper log untouched
    # a smoke log is a different file the unlock never reads
    src = ROOT / "src" / "agenkit_trader"
    for name in ("live_unlock.py", "ui/readers.py", "runtime/loop.py"):
        assert "smoke" not in (src / name).read_text().lower()


def test_smoke_uses_no_strategy_or_deployed_or_paper_files() -> None:
    import ast

    src = (ROOT / "src" / "agenkit_trader" / "smoke.py").read_text()
    tree = ast.parse(src)
    doc = ast.get_docstring(tree) or ""
    code = src.replace(doc, "")
    for banned in (
        "deployed",
        "paper_state",
        "strategies",
        "REGISTRY",
        "load_deployed",
    ):
        assert banned not in code
    imports = {
        n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module
    }
    assert not any("strateg" in m for m in imports)


class Trip(FakeVenue):
    """Raises KeyboardInterrupt on the nth call of one method."""

    trip_on = ""
    trip_n = 1
    seen = 0

    def _t(self, name: str) -> None:
        if name == self.trip_on:
            self.seen += 1
            if self.seen == self.trip_n:
                raise KeyboardInterrupt

    def server_time(self) -> Any:
        self._t("server_time")
        return super().server_time()

    def place_order(self, order: Order) -> Any:
        self._t("place_order")
        return super().place_order(order)

    def place_stop(self, order: Order) -> str:
        self._t("place_stop")
        return super().place_stop(order)

    def cancel_stop(self, symbol: str, client_id: str) -> None:
        self._t("cancel_stop")
        super().cancel_stop(symbol, client_id)

    def flatten_all(self) -> None:
        self._t("flatten_all")
        super().flatten_all()


def trip(on: str, n: int = 1) -> Trip:
    v = Trip(rules={"BTCUSDT": BTC}, marks={"BTCUSDT": 50_000.0})
    v.trip_on, v.trip_n = on, n
    return v


@pytest.mark.parametrize(
    "on,n",
    [
        ("place_order", 1),  # entry sent, outcome unknown to smoke
        ("place_stop", 1),  # step 4
        ("cancel_stop", 1),  # step 7
        ("place_order", 2),  # step 7 close
        ("place_order", 3),  # step 8 second entry
        ("place_stop", 2),  # step 8 second stop
        ("flatten_all", 1),  # during step 8 kill switch
    ],
)
def test_interrupt_ends_flat_exit_130(tmp_path: Path, on: str, n: int) -> None:
    v = trip(on, n)
    r = go(tmp_path, v)
    assert r.report.interrupted and r.report.flat is True
    assert r.report.exit_code == 130
    assert v.positions == {} and v.stops == []
    assert any(s.status == "fail" and "interrupted" in s.reason for s in r.report.steps)
    assert [s.n for s in r.report.steps] == list(range(1, 9))


def test_interrupt_before_anything_touched_does_not_flatten(tmp_path: Path) -> None:
    v = trip("server_time")
    r = go(tmp_path, v)
    assert r.report.exit_code == 130 and r.report.flat is True
    assert v.flatten_calls == 0 and v.orders == []


def test_interrupt_and_unreadable_account_is_exit_4(tmp_path: Path) -> None:
    class Dead(Trip):
        def get_open_orders(self) -> Any:
            if self.seen:
                raise smoke.VenueError("down")
            return super().get_open_orders()

    v = Dead(rules={"BTCUSDT": BTC}, marks={"BTCUSDT": 50_000.0})
    v.trip_on, v.trip_n = "place_stop", 1
    r = go(tmp_path, v)
    assert r.report.flat is None and r.report.exit_code == 4
    assert "UNKNOWN" in r.report.table()


def test_log_write_failure_mid_run_still_flattens(tmp_path: Path) -> None:
    class FlakyLog(smoke.SmokeLog):
        def write(
            self, kind: str, inputs: dict[str, Any], output: dict[str, Any], result: str
        ) -> None:
            if kind == "smoke_step" and inputs.get("step") == 5:
                raise OSError("disk full")
            super().write(kind, inputs, output, result)

    v = make_venue()
    rep = smoke.run_smoke(
        v,
        symbol="BTCUSDT",
        risk=RiskConfig(),
        file_rules=FILE_RULES,
        log=FlakyLog(tmp_path / "s" / "smoke_log.jsonl"),
        clock=lambda: T0,
        sleep=lambda s: None,
    )
    assert v.flatten_calls >= 1 and v.positions == {} and v.stops == []
    assert rep.flat is True and rep.exit_code == 1 and not rep.interrupted
    assert [s.status for s in rep.steps[3:]] == ["pass", "pass", "skip", "skip", "skip"]
    assert "aborted" in rep.steps[5].reason


def test_untouched_failure_does_no_cleanup(tmp_path: Path) -> None:
    v = make_venue(positions={"BTCUSDT": Position("BTCUSDT", 0.5, 40_000.0)})
    r = go(tmp_path, v)
    assert v.flatten_calls == 0 and v.positions["BTCUSDT"].qty == 0.5
    assert r.report.exit_code == 4


def test_venue_without_base_url_refused_fail_closed(tmp_path: Path) -> None:
    class Anon(FakeVenue):
        base_url = None  # type: ignore[assignment]

    with pytest.raises(smoke.SmokeRefused):
        go(tmp_path, Anon(rules={"BTCUSDT": BTC}, marks={"BTCUSDT": 50_000.0}))


# ---------------------------------------------------------------- CLI


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setattr(
        cli, "SMOKE_LOG", tmp_path / "data" / "smoke" / "smoke_log.jsonl"
    )
    built: list[Any] = []

    def factory(**kw: Any) -> FakeVenue:
        v = make_venue()
        built.append(v)
        return v

    monkeypatch.setattr(cli, "BinanceTestnetVenue", factory)
    monkeypatch.setattr(cli, "_smoke_clock", lambda: T0)
    monkeypatch.setattr(smoke.time, "sleep", lambda s: None)
    return {"built": built, "log": cli.SMOKE_LOG}


def test_cli_refuses_without_keys(
    cli_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    monkeypatch.delenv(cli.KEY_ENV, raising=False)
    monkeypatch.delenv(cli.SECRET_ENV, raising=False)
    assert cli.main(["smoke", "--yes"]) == cli.EXIT_REFUSED
    assert cli_env["built"] == []
    assert cli.KEY_ENV in capsys.readouterr().err


def test_cli_needs_tty_confirmation_without_yes(
    cli_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(cli.KEY_ENV, KEY)
    monkeypatch.setenv(cli.SECRET_ENV, SECRET)
    assert cli.main(["smoke"]) == cli.EXIT_REFUSED  # pytest stdin is not a TTY
    assert cli_env["built"] == []


def test_cli_typed_confirmation(
    cli_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(cli.KEY_ENV, KEY)
    monkeypatch.setenv(cli.SECRET_ENV, SECRET)
    monkeypatch.setattr(cli, "_is_tty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda p="": "nope")
    assert cli.main(["smoke"]) == cli.EXIT_REFUSED
    assert cli_env["built"] == []
    monkeypatch.setattr("builtins.input", lambda p="": smoke.CONFIRM_PHRASE)
    assert cli.main(["smoke"]) == 0
    assert len(cli_env["built"]) == 1


def test_cli_yes_runs_prints_table_and_logs_separately(
    cli_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: Any,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(cli.KEY_ENV, KEY)
    monkeypatch.setenv(cli.SECRET_ENV, SECRET)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["smoke", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "TESTNET" in out and "pass" in out and "flat" in out.lower()
    assert KEY not in out and SECRET not in out
    assert cli_env["log"].exists()
    assert not (tmp_path / "logs").exists() and not (tmp_path / "state").exists()


def test_cli_bad_symbol_refused(
    cli_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(cli.KEY_ENV, KEY)
    monkeypatch.setenv(cli.SECRET_ENV, SECRET)
    assert cli.main(["smoke", "--yes", "--symbol", "btc/usdt"]) == cli.EXIT_REFUSED
    assert cli_env["built"] == []


def test_cli_has_no_base_url_option_and_mainnet_unreachable() -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(
            ["smoke", "--base-url", "https://fapi.binance.com"]
        )
    from agenkit_trader.execution.binance_testnet import BinanceTestnetVenue

    with pytest.raises(ValueError):
        BinanceTestnetVenue(base_url="https://fapi.binance.com")


def test_refuses_non_testnet_venue_object(tmp_path: Path) -> None:
    class Mainnet(FakeVenue):
        base_url = "https://fapi.binance.com"

    v = Mainnet(rules={"BTCUSDT": BTC}, marks={"BTCUSDT": 50_000.0})
    with pytest.raises(smoke.SmokeRefused):
        go(tmp_path, v)
    assert v.orders == []


def test_cli_refusal_creates_no_data_dir(
    cli_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(cli.KEY_ENV, raising=False)
    assert cli.main(["smoke", "--yes"]) == cli.EXIT_REFUSED
    assert cli.main(["smoke", "--yes", "--symbol", "x"]) == cli.EXIT_REFUSED
    assert not cli_env["log"].parent.exists()


def test_cli_sigterm_becomes_interrupt_and_handlers_restored(
    cli_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import signal

    monkeypatch.setenv(cli.KEY_ENV, KEY)
    monkeypatch.setenv(cli.SECRET_ENV, SECRET)
    before = (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGHUP))

    class Killed(FakeVenue):
        def place_stop(self, order: Order) -> str:
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)  # type: ignore[operator]
            return super().place_stop(order)

    made: list[Killed] = []

    def factory(**kw: Any) -> Killed:
        v = Killed(rules={"BTCUSDT": BTC}, marks={"BTCUSDT": 50_000.0})
        made.append(v)
        return v

    monkeypatch.setattr(cli, "BinanceTestnetVenue", factory)
    assert cli.main(["smoke", "--yes"]) == 130
    assert made[0].positions == {}  # flattened before exiting
    after = (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGHUP))
    assert before == after
