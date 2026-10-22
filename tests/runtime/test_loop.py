"""Runtime loop against FakeVenue: every order via the gate, fail closed."""

import json
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from agenkit_trader.config import RiskConfig, RuntimeConfig
from agenkit_trader.execution.venue import (
    ClockSkewError,
    Order,
    OrderStatus,
    OrderStatusUnknownError,
    Position,
    SymbolRules,
    VenueError,
)
from agenkit_trader.killswitch import KillSwitch
from agenkit_trader.log import DecisionLog
from agenkit_trader.regime import REGIMES
from agenkit_trader.risk.gate import RiskGate
from agenkit_trader.runtime.loop import (
    DeployedError,
    Deployment,
    Runtime,
    StartupRefused,
    check_clock_skew,
    client_order_id,
    last_unreset_halt,
    load_deployed,
    reset_halt,
    run_loop,
    validate_deployments,
)
from fakes import T0, FakeVenue

STEP = pd.Timedelta(hours=1)
NOW = T0 + timedelta(seconds=30)  # bar opened 11:00 closed at T0=12:00
LAST = pd.Timestamp(T0) - STEP  # open time of the latest CLOSED bar


def make_bars(
    n: int = 80, last_open: pd.Timestamp = LAST, forming: bool = True, seed: int = 0
) -> pd.DataFrame:
    idx = pd.date_range(end=last_open, periods=n, freq="1h", tz="UTC")
    if forming:
        idx = idx.append(pd.DatetimeIndex([last_open + STEP]))
    rng = np.random.default_rng(seed)
    close = 100.0 + np.cumsum(rng.normal(0, 1, len(idx)))
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": 1.0,
        },
        index=idx,
    )


class Scripted:
    """Target position scripted per bar open time; 0 elsewhere."""

    name = "scripted"
    edge = "test"
    param_grid: dict[str, list[Any]] = {"k": [1, 2]}

    def __init__(self, targets: dict[pd.Timestamp, float] | None = None) -> None:
        self.targets = targets or {}
        self.seen_last: list[pd.Timestamp] = []

    def signal(
        self, bars: pd.DataFrame, params: dict[str, Any], aux: dict[str, pd.Series]
    ) -> pd.Series:
        self.seen_last.append(bars.index[-1])
        s = pd.Series(0.0, index=bars.index)
        for t, v in self.targets.items():
            if t in s.index:
                s[t] = v
        return s


class NeedsFunding(Scripted):
    name = "needs_funding"
    aux_required = ("funding",)

    def __init__(self) -> None:
        super().__init__()
        self.aux_seen: list[dict[str, pd.Series]] = []

    def signal(
        self, bars: pd.DataFrame, params: dict[str, Any], aux: dict[str, pd.Series]
    ) -> pd.Series:
        self.aux_seen.append(aux)
        return super().signal(bars, params, aux)


def dep(
    name: str = "scripted",
    symbol: str = "BTCUSDT",
    allowed: tuple[str, ...] = REGIMES,
) -> Deployment:
    return Deployment(
        name=name,
        symbol=symbol,
        interval="1h",
        params={"k": 1},
        allowed_regimes=list(allowed),
    )


class Harness:
    def __init__(
        self,
        tmp_path: Path,
        venue: FakeVenue | None = None,
        strategy: Scripted | None = None,
        deployments: list[Deployment] | None = None,
        risk: RiskConfig | None = None,
        rt: RuntimeConfig | None = None,
        now: Any = NOW,
    ) -> None:
        self.venue = venue or FakeVenue(now=NOW)
        if "BTCUSDT" not in self.venue.bars:
            self.venue.bars["BTCUSDT"] = make_bars()
        self.strategy = strategy or Scripted()
        self.risk = risk or RiskConfig()
        self.log_path = tmp_path / "decisions.jsonl"
        self.log = DecisionLog(self.log_path)
        self.kill = KillSwitch(self.venue, self.log, self.risk)
        self.gate = RiskGate(self.risk, self.venue.symbol_rules, self.log)
        self.now = now
        self.state_path = tmp_path / "state.json"
        self.deployments = deployments or [dep(self.strategy.name)]
        self.rt_cfg = rt or RuntimeConfig()
        self.rt = self.make_runtime()

    def make_runtime(self) -> Runtime:
        return Runtime(
            self.venue,
            [self.strategy],
            self.deployments,
            self.gate,
            self.kill,
            self.log,
            lambda: self.now,
            runtime_cfg=self.rt_cfg,
            state_path=self.state_path,
            init_state=True,
        )

    def records(self, kind: str | None = None) -> list[dict[str, Any]]:
        recs = [json.loads(x) for x in self.log_path.read_text().splitlines()]
        return [r for r in recs if kind is None or r["kind"] == kind]


def enter_long(tmp_path: Path, **kw: Any) -> Harness:
    return Harness(tmp_path, strategy=Scripted({LAST: 1.0}), **kw)


# ---------------------------------------------------------------- entries


def test_entry_places_gated_order_then_disaster_stop(tmp_path: Path) -> None:
    h = enter_long(tmp_path)
    h.rt.start()
    h.rt.cycle()
    assert len(h.venue.orders) == 1
    o = h.venue.orders[0]
    bars = h.venue.bars["BTCUSDT"].iloc[:-1]
    close = float(bars["close"].iloc[-1])
    assert o.side == "buy" and o.price_ref == close and not o.reduce_only
    assert o.stop_price is not None and o.stop_price < close
    # protective stop = close - 3 * ATR(14), rounded TOWARD entry to the tick
    tr = pd.concat(
        [
            bars["high"] - bars["low"],
            (bars["high"] - bars["close"].shift()).abs(),
            (bars["low"] - bars["close"].shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = float(tr.rolling(14).mean().iloc[-1])
    raw = close - 3.0 * atr
    assert o.stop_price == pytest.approx(np.ceil(raw / 0.1 - 1e-9) * 0.1)
    # risk at stop within 1% of 1000
    assert (close - o.stop_price) * o.qty <= 10.0 + 1e-9
    # exchange-side reduce-only stop at the same price for the filled qty
    assert len(h.venue.stops) == 1
    s = h.venue.stops[0]
    assert s.side == "sell" and s.reduce_only and s.qty == o.qty
    assert s.stop_price == o.stop_price
    assert o.client_id and s.client_id and o.client_id != s.client_id
    # the stop passed through the gate too
    gate_orders = [r["inputs"]["order"] for r in h.records("risk_gate")]
    assert [g["client_id"] for g in gate_orders] == [o.client_id, s.client_id]
    # every decision is logged
    sig = h.records("signal")
    assert sig and sig[-1]["output"]["target"] == 1.0
    assert h.records("order")[-1]["result"] == "filled"


def test_gate_rejection_places_no_order_and_logs_reason(tmp_path: Path) -> None:
    h = enter_long(tmp_path, risk=RiskConfig(max_leverage=0.01))
    h.rt.start()
    h.rt.cycle()
    assert h.venue.orders == [] and h.venue.stops == []
    rej = [r for r in h.records("order") if r["result"] == "rejected"]
    assert rej and rej[-1]["output"]["reason"] == "leverage"
    assert not h.kill.halted  # a rejection is logged, not an error


def test_disaster_stop_rejected_trips_kill_switch(tmp_path: Path) -> None:
    h = enter_long(tmp_path)
    h.rt.start()
    real_check = h.gate.check
    calls = {"n": 0}

    def check(order: Order, state: Any, now: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 2:  # the stop order
            from agenkit_trader.risk.gate import Decision

            return Decision(False, "test_reject", 0.0)
        return real_check(order, state, now)

    h.gate.check = check  # type: ignore[method-assign]
    h.rt.cycle()
    assert h.kill.halted and h.venue.positions == {}


def test_forming_bar_is_ignored(tmp_path: Path) -> None:
    forming_open = LAST + STEP
    h = Harness(tmp_path, strategy=Scripted({forming_open: 1.0}))
    h.rt.start()
    h.rt.cycle()
    assert h.venue.orders == []
    assert h.strategy.seen_last and max(h.strategy.seen_last) == LAST


def test_same_bar_twice_places_one_order(tmp_path: Path) -> None:
    h = enter_long(tmp_path)
    h.rt.start()
    h.rt.cycle()
    h.rt.cycle()
    assert len(h.venue.orders) == 1
    # a restarted runtime (same state file) does not reprocess the bar even
    # if the position was closed by the disaster stop in the meantime
    h.venue.positions.clear()
    h.venue.stops.clear()
    rt2 = h.make_runtime()
    rt2.start()
    rt2.cycle()
    assert len(h.venue.orders) == 1


def test_client_order_ids_are_deterministic_and_valid() -> None:
    a = client_order_id("trend_breakout", "BTCUSDT", LAST, "entry")
    assert a == client_order_id("trend_breakout", "BTCUSDT", LAST, "entry")
    assert a != client_order_id("trend_breakout", "BTCUSDT", LAST, "stop")
    assert a != client_order_id("trend_breakout", "BTCUSDT", LAST + STEP, "entry")
    import re

    assert re.fullmatch(r"[\.A-Z\:/a-z0-9_-]{1,36}", a)


def test_entry_only_on_transition(tmp_path: Path) -> None:
    # signal already long on the previous bar: no mid-signal entry
    h = Harness(tmp_path, strategy=Scripted({LAST - STEP: 1.0, LAST: 1.0}))
    h.rt.start()
    h.rt.cycle()
    assert h.venue.orders == []


def test_no_entry_when_size_rounds_to_zero(tmp_path: Path) -> None:
    v = FakeVenue(now=NOW)
    v.rules["BTCUSDT"] = SymbolRules(tick=0.1, lot=100.0, min_notional=5.0)
    h = Harness(tmp_path, venue=v, strategy=Scripted({LAST: 1.0}))
    h.rt.start()
    h.rt.cycle()
    assert v.orders == []
    assert any(r["result"] == "size_zero" for r in h.records("entry"))


# ---------------------------------------------------------------- exits


def test_exit_reduces_and_cancels_stop(tmp_path: Path) -> None:
    v = FakeVenue(now=NOW, positions={"BTCUSDT": Position("BTCUSDT", 0.5, 100.0)})
    v.stops.append(Order("BTCUSDT", "sell", 0.5, 100.0, 90.0, "scripted", "old", True))
    h = Harness(tmp_path, venue=v, strategy=Scripted({}))  # target 0
    h.rt.start()
    h.rt.cycle()
    assert len(v.orders) == 1
    o = v.orders[0]
    assert o.side == "sell" and o.qty == 0.5 and o.reduce_only
    assert v.positions == {} and v.stops == []
    assert "BTCUSDT" in v.cancel_calls


def test_flip_exits_then_enters(tmp_path: Path) -> None:
    v = FakeVenue(now=NOW, positions={"BTCUSDT": Position("BTCUSDT", 0.5, 100.0)})
    v.stops.append(Order("BTCUSDT", "sell", 0.5, 100.0, 90.0, "scripted", "o", True))
    h = Harness(tmp_path, venue=v, strategy=Scripted({LAST: -1.0}))
    h.rt.start()
    h.rt.cycle()
    assert [o.side for o in v.orders] == ["sell", "sell"]
    assert v.orders[0].reduce_only and not v.orders[1].reduce_only
    assert v.positions["BTCUSDT"].qty < 0
    assert len(v.stops) == 1 and v.stops[0].side == "buy"


# ---------------------------------------------------------------- regimes


def test_regime_restricted_deployment_is_refused_at_start(tmp_path: Path) -> None:
    # v1 fails closed: runtime labels cannot reproduce research's
    # full-history labels exactly, so a restricted deployment never runs.
    for allowed in (("trend",), ("range",), ()):
        h = Harness(
            tmp_path / "-".join(allowed or ("none",)),
            strategy=Scripted({LAST: 1.0}),
            deployments=[dep(allowed=allowed)],
        )
        with pytest.raises(StartupRefused, match="regime"):
            h.rt.start()
        assert h.venue.orders == []


def test_unrestricted_deployment_enters_without_regime_label(tmp_path: Path) -> None:
    h = Harness(tmp_path, strategy=Scripted({LAST: 1.0}), deployments=[dep()])
    h.rt.start()
    h.rt.cycle()
    assert len(h.venue.orders) == 1


# ---------------------------------------------------------------- restart


def test_restart_with_open_position_does_not_double_enter(tmp_path: Path) -> None:
    v = FakeVenue(now=NOW, positions={"BTCUSDT": Position("BTCUSDT", 0.5, 100.0)})
    v.stops.append(Order("BTCUSDT", "sell", 0.5, 100.0, 90.0, "scripted", "o", True))
    h = Harness(tmp_path, venue=v, strategy=Scripted({LAST: 1.0}))
    h.rt.start()
    h.rt.cycle()
    assert v.orders == [] and len(v.stops) == 1
    assert v.positions["BTCUSDT"].qty == 0.5
    assert h.records("adopted_position")


def test_restart_owned_position_without_stop_gets_one(tmp_path: Path) -> None:
    v = FakeVenue(now=NOW, positions={"BTCUSDT": Position("BTCUSDT", 0.5, 100.0)})
    h = Harness(tmp_path, venue=v, strategy=Scripted({LAST: 1.0}))
    h.rt.start()
    assert len(v.stops) == 1
    s = v.stops[0]
    assert s.side == "sell" and s.qty == 0.5 and s.reduce_only
    assert v.orders == []


def test_unowned_position_refuses_start_by_default(tmp_path: Path) -> None:
    v = FakeVenue(now=NOW, positions={"ETHUSDT": Position("ETHUSDT", 1.0, 2000.0)})
    h = Harness(tmp_path, venue=v)
    with pytest.raises(StartupRefused, match="ETHUSDT"):
        h.rt.start()
    assert v.flatten_calls == 0 and v.orders == []
    assert h.records("unowned_position")
    with pytest.raises(StartupRefused):
        h.rt.cycle()  # never trades after a refused start


def test_unowned_position_flattens_when_configured(tmp_path: Path) -> None:
    v = FakeVenue(now=NOW, positions={"ETHUSDT": Position("ETHUSDT", 1.0, 2000.0)})
    h = Harness(tmp_path, venue=v, rt=RuntimeConfig(flatten_unowned_on_start=True))
    with pytest.raises(StartupRefused):
        h.rt.start()
    assert h.kill.halted and v.flatten_calls == 1 and v.positions == {}


def test_unowned_open_order_refuses_start(tmp_path: Path) -> None:
    v = FakeVenue(now=NOW)
    v.stops.append(Order("ETHUSDT", "sell", 1.0, 2000.0, 1900.0, "x", "o", True))
    h = Harness(tmp_path, venue=v)
    with pytest.raises(StartupRefused, match="ETHUSDT"):
        h.rt.start()


# ---------------------------------------------------------------- kill paths


def test_stale_data_triggers_kill_switch_and_flattens(tmp_path: Path) -> None:
    v = FakeVenue(now=NOW, positions={"BTCUSDT": Position("BTCUSDT", 0.5, 100.0)})
    v.stops.append(Order("BTCUSDT", "sell", 0.5, 100.0, 90.0, "scripted", "o", True))
    v.bars["BTCUSDT"] = make_bars(last_open=LAST - 2 * STEP, forming=False)
    h = Harness(tmp_path, venue=v, strategy=Scripted({}))
    h.rt.start()
    h.rt.cycle()
    assert h.kill.halted
    assert v.flatten_calls == 1 and v.positions == {}
    assert v.orders == []  # flatten never goes through place_order / the gate
    assert h.records("kill_switch")[-1]["inputs"]["reason"] == "stale_data"


def test_five_api_errors_halt(tmp_path: Path) -> None:
    h = enter_long(tmp_path)
    h.rt.start()
    h.venue.fail_next = 5
    for _ in range(5):
        h.rt.cycle()
    assert h.kill.halted and h.venue.flatten_calls == 1
    assert h.venue.orders == []
    assert len(h.records("api_error")) == 5


def test_run_loop_stops_after_api_error_halt(tmp_path: Path) -> None:
    h = enter_long(tmp_path)
    slept: list[float] = []
    h.venue.fail_next = 10**6  # every venue call fails
    with pytest.raises(StartupRefused):
        run_loop(
            h.venue,
            [h.strategy],
            h.deployments,
            h.gate,
            h.kill,
            h.log,
            lambda: h.now,
            runtime_cfg=h.rt_cfg,
            init_state=True,
            sleep=slept.append,
            max_cycles=100,
            state_path=h.state_path,
        )
    assert h.venue.orders == []


def test_run_loop_cycles_until_halt(tmp_path: Path) -> None:
    h = enter_long(tmp_path)
    slept: list[float] = []

    def sleep(s: float) -> None:
        slept.append(s)
        h.venue.fail_next = 1  # an API error every cycle after the first

    run_loop(
        h.venue,
        [h.strategy],
        h.deployments,
        h.gate,
        h.kill,
        h.log,
        lambda: h.now,
        runtime_cfg=h.rt_cfg,
        init_state=True,
        sleep=sleep,
        max_cycles=100,
        state_path=h.state_path,
    )
    assert h.kill.halted
    assert len(slept) == 5  # 1 good cycle + 5 failing, halted on the 5th error
    assert len(h.venue.orders) == 1


def test_halted_loop_refuses_to_place_orders(tmp_path: Path) -> None:
    h = enter_long(tmp_path)
    h.kill.trigger("manual")
    run_loop(
        h.venue,
        [h.strategy],
        h.deployments,
        h.gate,
        h.kill,
        h.log,
        lambda: h.now,
        runtime_cfg=h.rt_cfg,
        init_state=True,
        sleep=lambda s: None,
        max_cycles=3,
        state_path=h.state_path,
    )
    assert h.venue.orders == []
    assert h.records("runtime")[-1]["result"] == "refused_halted"


def test_state_halted_comes_from_kill_switch(tmp_path: Path) -> None:
    h = enter_long(tmp_path)
    h.rt.start()
    acct = h.venue.get_account()
    assert not acct.halted
    h.kill.halted = True  # switch halted, venue state says not halted
    st = h.rt.build_state(acct, T0)
    assert st.halted
    o = Order("BTCUSDT", "buy", 0.01, 100.0, 95.0, "scripted", "cid", False)
    assert h.rt.submit(o, st, "entry") is None
    assert h.venue.orders == []


def test_state_halted_blocks_even_if_switch_not_halted(tmp_path: Path) -> None:
    h = enter_long(tmp_path)
    h.rt.start()
    h.venue.account_halted = True
    acct = h.venue.get_account()
    st = h.rt.build_state(acct, T0)
    assert st.halted and not h.kill.halted
    o = Order("BTCUSDT", "buy", 0.01, 100.0, 95.0, "scripted", "cid", False)
    assert h.rt.submit(o, st, "entry") is None
    assert h.venue.orders == []
    assert h.records("order")[-1]["output"]["reason"] == "halted"


def test_clock_skew_raises(tmp_path: Path) -> None:
    v = FakeVenue(now=NOW + timedelta(milliseconds=1500))
    with pytest.raises(ClockSkewError):
        check_clock_skew(v, lambda: NOW)
    check_clock_skew(FakeVenue(now=NOW + timedelta(milliseconds=900)), lambda: NOW)


def test_clock_skew_halts_and_flattens(tmp_path: Path) -> None:
    v = FakeVenue(now=NOW, positions={"BTCUSDT": Position("BTCUSDT", 0.5, 100.0)})
    v.stops.append(Order("BTCUSDT", "sell", 0.5, 100.0, 90.0, "scripted", "o", True))
    h = Harness(tmp_path, venue=v, strategy=Scripted({LAST: -1.0}))
    h.rt.start()
    v.now = NOW - timedelta(milliseconds=1200)
    h.rt.cycle()
    assert h.kill.halted and v.flatten_calls == 1 and v.orders == []
    assert h.records("kill_switch")[-1]["inputs"]["reason"] == "clock_skew"


def test_clock_skew_at_start_refuses(tmp_path: Path) -> None:
    v = FakeVenue(now=NOW + timedelta(seconds=5))
    h = Harness(tmp_path, venue=v)
    with pytest.raises(StartupRefused, match="skew"):
        h.rt.start()
    assert h.kill.halted and v.orders == []


def test_daily_loss_breach_trips_kill_switch(tmp_path: Path) -> None:
    v = FakeVenue(now=NOW, positions={"BTCUSDT": Position("BTCUSDT", 0.5, 100.0)})
    v.stops.append(Order("BTCUSDT", "sell", 0.5, 100.0, 90.0, "scripted", "o", True))
    h = Harness(tmp_path, venue=v, strategy=Scripted({}))
    h.rt.start()
    v.equity = 960.0  # -4% today, limit 3%
    h.rt.cycle()
    assert h.kill.halted and v.positions == {} and v.orders == []
    assert h.records("kill_switch")[-1]["inputs"]["reason"] == "daily_loss"


def test_paper_equity_is_start_equity_plus_pnl(tmp_path: Path) -> None:
    v = FakeVenue(now=NOW, equity=15_000.0, day_start_equity=15_000.0)
    v.peak_equity = 15_000.0
    h = Harness(tmp_path, venue=v)
    h.rt.start()
    v.equity = 15_050.0
    st = h.rt.build_state(v.get_account(), T0)
    assert st.equity == pytest.approx(1050.0)
    assert st.peak_equity == pytest.approx(1050.0)
    assert st.day_start_equity == pytest.approx(1000.0)


def test_positions_are_valued_at_mark_for_the_gate(tmp_path: Path) -> None:
    v = FakeVenue(now=NOW, positions={"BTCUSDT": Position("BTCUSDT", 0.5, 100.0)})
    v.stops.append(Order("BTCUSDT", "sell", 0.5, 100.0, 90.0, "scripted", "o", True))
    v.marks["BTCUSDT"] = 123.4
    h = Harness(tmp_path, venue=v, strategy=Scripted({}))
    st = h.rt.build_state(v.get_account(), T0)
    assert st.positions["BTCUSDT"].entry_price == 123.4
    assert st.positions["BTCUSDT"].qty == 0.5


# ---------------------------------------------------------------- funding aux


def test_funding_aux_is_aligned_with_research_lag(tmp_path: Path) -> None:
    v = FakeVenue(now=NOW)
    fidx = pd.date_range(end=LAST, periods=10, freq="8h", tz="UTC")
    v.funding["BTCUSDT"] = pd.Series(np.arange(10, dtype=float), index=fidx)
    s = NeedsFunding()
    h = Harness(tmp_path, venue=v, strategy=s)
    h.rt.start()
    h.rt.cycle()
    f = s.aux_seen[-1]["funding"]
    # the 11:00 bar closes 11:59:59.999; a rate stamped at 08:00 is usable after
    # 08:01, so the last closed bar sees the latest stamp <= its close - 1min
    from agenkit_trader.data.align import align_asof
    from agenkit_trader.strategies.funding_reversion import FUNDING_AVAILABLE_LAG

    bars = v.bars["BTCUSDT"].iloc[:-1]
    exp = align_asof(bars, v.funding["BTCUSDT"], FUNDING_AVAILABLE_LAG)
    pd.testing.assert_series_equal(f, exp, check_names=False)


def test_funding_needed_but_unavailable_refuses_start(tmp_path: Path) -> None:
    v = FakeVenue(now=NOW, supports_funding=False)
    h = Harness(tmp_path, venue=v, strategy=NeedsFunding())
    with pytest.raises(StartupRefused, match="funding"):
        h.rt.start()


def test_real_funding_strategy_declares_aux() -> None:
    from agenkit_trader.strategies import REGISTRY

    assert getattr(REGISTRY["funding_reversion"], "aux_required", ()) == ("funding",)


# ---------------------------------------------------------------- deployed.json


def test_load_deployed_rejects_missing_empty_malformed(tmp_path: Path) -> None:
    with pytest.raises(DeployedError, match="not found"):
        load_deployed(tmp_path / "nope.json")
    p = tmp_path / "d.json"
    p.write_text("[]\n")
    with pytest.raises(DeployedError, match="empty"):
        load_deployed(p)
    p.write_text("{not json")
    with pytest.raises(DeployedError):
        load_deployed(p)
    p.write_text(json.dumps([{"name": "x"}]))
    with pytest.raises(DeployedError):
        load_deployed(p)


def test_load_deployed_reads_entries(tmp_path: Path) -> None:
    p = tmp_path / "d.json"
    p.write_text(
        json.dumps(
            [
                {
                    "name": "trend_breakout",
                    "symbol": "BTCUSDT",
                    "interval": "1h",
                    "params": {"lookback": 96, "vol_cap": 1.5},
                    "allowed_regimes": ["trend"],
                }
            ]
        )
    )
    (d,) = load_deployed(p)
    assert d.name == "trend_breakout" and d.allowed_regimes == ["trend"]


def test_repo_deployed_json_is_empty_so_paper_refuses() -> None:
    root = Path(__file__).resolve().parents[2]
    with pytest.raises(DeployedError, match="empty"):
        load_deployed(root / "research" / "deployed.json")


def test_validate_deployments() -> None:
    reg = {"scripted": Scripted()}
    validate_deployments([dep()], reg)
    with pytest.raises(StartupRefused, match="unknown strategy"):
        validate_deployments([dep(name="nope")], reg)
    bad = Deployment("scripted", "BTCUSDT", "1h", {"k": 3}, ["trend"])
    with pytest.raises(StartupRefused, match="grid"):
        validate_deployments([bad], reg)
    bad = Deployment("scripted", "BTCUSDT", "1h", {"k": 1, "z": 1}, ["trend"])
    with pytest.raises(StartupRefused, match="grid"):
        validate_deployments([bad], reg)
    with pytest.raises(StartupRefused, match="one deployed strategy per symbol"):
        validate_deployments([dep(), dep()], reg)
    bad = Deployment("scripted", "BTCUSDT", "1h", {"k": 1}, ["sideways"])
    with pytest.raises(StartupRefused, match="regime"):
        validate_deployments([bad], reg)
    with pytest.raises(StartupRefused, match="regime-restricted"):
        validate_deployments([dep(allowed=("trend",))], reg)
    bad = Deployment("scripted", "BTCUSDT", "7x", {"k": 1}, ["trend"])
    with pytest.raises(StartupRefused, match="interval"):
        validate_deployments([bad], reg)


def test_failed_account_read_leaves_bar_retriable(tmp_path: Path) -> None:
    h = enter_long(tmp_path)
    h.rt.start()
    calls = {"n": 0}
    real = h.venue.get_account

    def flaky() -> Any:
        calls["n"] += 1
        if calls["n"] == 2:  # the read inside the strategy step
            raise RuntimeError("timeout")
        return real()

    h.venue.get_account = flaky  # type: ignore[method-assign]
    h.rt.cycle()
    assert h.venue.orders == [] and len(h.records("api_error")) == 1
    h.rt.cycle()  # same bar, next poll: decided now, exactly once
    h.rt.cycle()
    assert len(h.venue.orders) == 1


# ================================================================ fix round 1
# Issue 1: an open position must never run without an exchange-side stop.


def _ambiguous(v: FakeVenue) -> None:
    """place_order fills on the exchange, then the response is lost."""
    real = v.place_order

    def place(order: Order) -> Any:
        real(order)
        raise VenueError("POST /fapi/v1/order: transport error ReadTimeout")

    v.place_order = place  # type: ignore[method-assign]


def _next_bar(h: Harness, k: int, target: float) -> None:
    nb = make_bars(last_open=LAST + k * STEP)
    h.venue.bars["BTCUSDT"] = nb
    h.strategy.targets = {t: target for t in nb.index}
    h.now = NOW + k * STEP
    h.venue.now = h.now


def test_ambiguous_entry_error_is_resolved_and_protected(tmp_path: Path) -> None:
    h = enter_long(tmp_path)
    v = h.venue
    _ambiguous(v)
    h.rt.start()
    h.rt.cycle()
    qty = v.positions["BTCUSDT"].qty
    assert qty > 0 and not h.kill.halted
    assert len(v.stops) == 1 and v.stops[0].qty == qty and v.stops[0].side == "sell"
    assert any(r["result"] == "filled_after_error" for r in h.records("order"))
    del v.place_order  # the probe: several later bars, signal stays long
    for k in range(1, 4):
        _next_bar(h, k, 1.0)
        h.rt.cycle()
        assert len(v.stops) == 1 and not h.kill.halted
    assert len(v.orders) == 1


def test_unknown_entry_that_fills_later_is_protected_next_cycle(tmp_path: Path) -> None:
    h = enter_long(tmp_path)
    v = h.venue
    pending: list[Order] = []

    def unknown(order: Order) -> Any:
        pending.append(order)
        v.order_status[order.client_id] = OrderStatus(order.client_id, "NEW", 0.0, 0.0)
        raise OrderStatusUnknownError("market order status NEW, executedQty 0")

    v.place_order = unknown  # type: ignore[method-assign]
    h.rt.start()
    h.rt.cycle()
    assert v.positions == {} and v.stops == [] and not h.kill.halted
    assert h.records("order")[-1]["result"] == "unknown"
    del v.place_order
    v.place_order(pending[0])  # the exchange fills it afterwards
    h.now = NOW + timedelta(seconds=30)
    v.now = h.now
    h.rt.cycle()  # same bar: no new decision, but the coverage check repairs
    qty = v.positions["BTCUSDT"].qty
    assert len(v.stops) == 1 and v.stops[0].qty == qty and v.stops[0].side == "sell"
    assert len(v.orders) == 1 and not h.kill.halted


def _long_with_stop(stop_qty: float = 0.5) -> FakeVenue:
    v = FakeVenue(now=NOW, positions={"BTCUSDT": Position("BTCUSDT", 0.5, 100.0)})
    v.stops.append(
        Order("BTCUSDT", "sell", stop_qty, 100.0, 90.0, "scripted", "old", True)
    )
    return v


def test_partial_exit_keeps_residual_protected(tmp_path: Path) -> None:
    v = _long_with_stop()
    h = Harness(tmp_path, venue=v, strategy=Scripted({}))  # target 0: exit
    h.rt.start()
    v.fill_fraction = 0.4
    h.rt.cycle()
    assert v.positions["BTCUSDT"].qty == pytest.approx(0.3)
    covered = sum(s.qty for s in v.stops if s.side == "sell" and s.reduce_only)
    assert covered >= 0.3 and not h.kill.halted
    # the residual was never left without a stop: the old one was not cancelled
    assert [s.client_id for s in v.stops] == ["old"]
    assert v.cancel_calls == []


def test_ambiguous_exit_that_filled_cancels_stop(tmp_path: Path) -> None:
    v = _long_with_stop()
    h = Harness(tmp_path, venue=v, strategy=Scripted({}))
    h.rt.start()
    _ambiguous(v)
    h.rt.cycle()
    assert v.positions == {} and v.stops == [] and not h.kill.halted


def test_ambiguous_exit_not_filled_keeps_stop(tmp_path: Path) -> None:
    v = _long_with_stop()
    h = Harness(tmp_path, venue=v, strategy=Scripted({}))
    h.rt.start()

    def lost(order: Order) -> Any:
        raise VenueError("HTTP 503 execution status unknown")

    v.place_order = lost  # type: ignore[method-assign]
    h.rt.cycle()
    assert v.positions["BTCUSDT"].qty == 0.5
    assert len(v.stops) == 1 and v.stops[0].qty == 0.5


def test_cycle_repairs_missing_stop(tmp_path: Path) -> None:
    v = _long_with_stop()
    h = Harness(tmp_path, venue=v, strategy=Scripted({LAST: 1.0}))
    h.rt.start()
    h.rt.cycle()
    v.stops.clear()  # e.g. cancelled by hand on the exchange
    h.now = NOW + timedelta(seconds=30)
    v.now = h.now
    h.rt.cycle()
    assert len(v.stops) == 1
    assert v.stops[0].qty == 0.5 and v.stops[0].side == "sell"
    assert v.stops[0].stop_price is not None and v.stops[0].stop_price < 110
    assert not h.kill.halted and v.orders == []


def test_cycle_replaces_insufficient_stop_before_cancelling(tmp_path: Path) -> None:
    v = _long_with_stop(stop_qty=0.1)
    h = Harness(tmp_path, venue=v, strategy=Scripted({LAST: 1.0}))
    events: list[str] = []
    real_place, real_cancel = v.place_stop, v.cancel_stop

    def place(o: Order) -> str:
        events.append(f"place {o.qty}")
        return real_place(o)

    def cancel(sym: str, cid: str) -> None:
        events.append(f"cancel {cid}")
        real_cancel(sym, cid)

    v.place_stop = place  # type: ignore[method-assign]
    v.cancel_stop = cancel  # type: ignore[method-assign]
    h.rt.start()
    h.rt.cycle()
    assert events == ["place 0.5", "cancel old"]
    assert [s.qty for s in v.stops] == [0.5]
    assert not h.kill.halted


def test_stop_repair_failure_trips_kill_switch(tmp_path: Path) -> None:
    v = _long_with_stop()
    h = Harness(tmp_path, venue=v, strategy=Scripted({LAST: 1.0}))
    h.rt.start()
    v.stops.clear()

    def broken(o: Order) -> str:
        raise VenueError("algo order rejected")

    v.place_stop = broken  # type: ignore[method-assign]
    h.rt.cycle()
    assert h.kill.halted and v.positions == {}
    assert h.records("halt")


def test_restart_mid_bar_without_stop_places_stop(tmp_path: Path) -> None:
    later = NOW + timedelta(minutes=20)
    v = FakeVenue(now=later, positions={"BTCUSDT": Position("BTCUSDT", 0.5, 100.0)})
    h = Harness(tmp_path, venue=v, strategy=Scripted({LAST: 1.0}), now=later)
    h.rt.start()
    assert not h.kill.halted and v.flatten_calls == 0
    assert len(v.stops) == 1 and v.stops[0].qty == 0.5


# Issue 2: state file and persisted halts.


def _runtime(h: Harness, state: Path, init: bool, kill: KillSwitch) -> Runtime:
    return Runtime(
        h.venue,
        [h.strategy],
        h.deployments,
        h.gate,
        kill,
        h.log,
        lambda: h.now,
        runtime_cfg=h.rt_cfg,
        state_path=state,
        init_state=init,
    )


def test_missing_state_refuses_without_init(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    rt = _runtime(h, tmp_path / "missing.json", False, h.kill)
    with pytest.raises(StartupRefused, match="--init-state"):
        rt.start()
    assert not (tmp_path / "missing.json").exists()


def test_init_state_is_logged(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.rt.start()
    assert [r["result"] for r in h.records("state")] == ["init"]
    assert h.state_path.exists()


def test_logged_halt_blocks_restart_even_with_new_state(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.rt.start()
    h.kill.trigger("manual")
    fresh = tmp_path / "other" / "state.json"
    k2 = KillSwitch(h.venue, h.log, h.risk)
    with pytest.raises(StartupRefused, match="halt"):
        _runtime(h, fresh, True, k2).start()
    assert last_unreset_halt(h.log_path) is not None
    reset_halt(fresh, h.log)
    assert last_unreset_halt(h.log_path) is None
    _runtime(h, fresh, True, k2).start()


def test_halt_is_persisted_in_state_file(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.rt.start()
    h.venue.fail_next = 5
    for _ in range(5):
        h.rt.cycle()
    assert json.loads(h.state_path.read_text())["halted"]
    assert h.records("halt")


# Issue 4a/4b: closed bars by server time; skew at round-trip midpoint.


def test_forming_bar_uses_server_time_not_local_clock(tmp_path: Path) -> None:
    forming_open = LAST + STEP  # closes at T0 + 1h
    # local clock says the bar closed 2.5 s ago; the venue says only 1.6 s ago,
    # which is inside the 2 s grace, so the bar is still treated as forming
    local = pd.Timestamp(T0 + STEP + timedelta(milliseconds=2500)).to_pydatetime()
    server = pd.Timestamp(T0 + STEP + timedelta(milliseconds=1600)).to_pydatetime()
    v = FakeVenue(now=server)
    h = Harness(tmp_path, venue=v, strategy=Scripted({forming_open: 1.0}), now=local)
    h.rt.start()
    h.rt.cycle()
    assert max(h.strategy.seen_last) == LAST
    assert v.orders == []


def test_bar_close_needs_two_second_grace(tmp_path: Path) -> None:
    forming_open = LAST + STEP
    just_after = pd.Timestamp(T0 + STEP + timedelta(seconds=1)).to_pydatetime()
    v = FakeVenue(now=just_after)
    h = Harness(
        tmp_path, venue=v, strategy=Scripted({forming_open: 1.0}), now=just_after
    )
    h.rt.start()
    h.rt.cycle()
    assert max(h.strategy.seen_last) == LAST and v.orders == []
    later = pd.Timestamp(T0 + STEP + timedelta(seconds=3)).to_pydatetime()
    h.now = v.now = later
    h.rt.cycle()  # now past the grace: the bar is closed and decided
    assert max(h.strategy.seen_last) == forming_open and len(v.orders) == 1


def test_skew_check_uses_round_trip_midpoint() -> None:
    times = iter([NOW, NOW + timedelta(seconds=3)])  # the request took 3 s
    v = FakeVenue(now=NOW + timedelta(milliseconds=1500))  # stamped mid-flight
    check_clock_skew(v, lambda: next(times))


@dataclass
class SyncingVenue(FakeVenue):
    sync_calls: int = 0

    def sync_time(self) -> int:
        self.sync_calls += 1
        return 0


def test_venue_offset_is_resynced_every_cycle(tmp_path: Path) -> None:
    v = SyncingVenue(now=NOW)
    h = Harness(tmp_path, venue=v)
    h.rt.start()
    h.rt.cycle()
    h.rt.cycle()
    assert v.sync_calls == 3


def test_bars_request_never_exceeds_binance_max(tmp_path: Path) -> None:
    seen: list[int] = []
    v = FakeVenue(now=NOW)
    real = v.get_bars

    def get_bars(sym: str, interval: str, limit: int) -> pd.DataFrame:
        seen.append(limit)
        return real(sym, interval, limit)

    v.get_bars = get_bars  # type: ignore[method-assign]
    h = Harness(tmp_path, venue=v, rt=RuntimeConfig(bars_limit=1499))
    h.rt.start()
    h.rt.cycle()
    assert seen and max(seen) <= 1500


# ================================================================ fix round 2
# Stop repair is bounded: one attempt per uncovered episode, then escalate.


def _started_long(tmp_path: Path) -> Harness:
    h = Harness(tmp_path, venue=_long_with_stop(), strategy=Scripted({LAST: 1.0}))
    h.rt.start()
    return h


def _tick(h: Harness, k: int) -> None:
    h.now = NOW + timedelta(seconds=30 * k)
    h.venue.now = h.now
    h.rt.cycle()


def test_unreported_stop_escalates_instead_of_repairing_forever(
    tmp_path: Path,
) -> None:
    h = _started_long(tmp_path)
    v = h.venue
    before = len(v.stops_placed)
    v.get_open_orders = lambda: []  # type: ignore[method-assign]
    for k in range(1, 6):
        _tick(h, k)
    assert h.kill.halted and v.flatten_calls == 1 and v.positions == {}
    assert len(v.stops_placed) - before <= 1
    assert any(r["result"] == "escalate" for r in h.records("stop_coverage"))


def test_zero_quantity_read_back_escalates_without_churn(tmp_path: Path) -> None:
    h = _started_long(tmp_path)
    v = h.venue
    real = v.get_open_orders

    def zero_qty() -> Any:
        return [replace(o, qty=0.0) for o in real()]

    v.get_open_orders = zero_qty  # type: ignore[method-assign]
    for k in range(1, 4):
        _tick(h, k)
    assert h.kill.halted
    assert not any(c.startswith("BTCUSDT:") for c in v.cancel_calls)  # no churn
    assert len(v.stops_placed) <= 1


def test_verified_repair_clears_episode_and_later_gets_fresh_attempt(
    tmp_path: Path,
) -> None:
    h = _started_long(tmp_path)
    v = h.venue
    v.stops.clear()
    _tick(h, 1)  # repair, verified by read-back
    assert len(v.stops) == 1 and not h.kill.halted
    first = v.stops[0].client_id
    _tick(h, 2)  # covered: episode cleared
    assert not h.kill.halted
    v.stops.clear()
    _tick(h, 3)  # a new episode gets a fresh single attempt
    assert len(v.stops) == 1 and not h.kill.halted
    # deterministic per (symbol, size, bar): the retry reuses the client id
    assert v.stops[0].client_id == first


def test_stop_vanishing_right_after_repair_escalates(tmp_path: Path) -> None:
    h = _started_long(tmp_path)
    v = h.venue
    v.stops.clear()
    _tick(h, 1)  # repaired and verified
    placed = len(v.stops_placed)
    v.stops.clear()  # e.g. accepted, then expired asynchronously
    _tick(h, 2)
    assert h.kill.halted and v.positions == {}
    assert len(v.stops_placed) == placed  # no second repair in the episode


def test_repair_client_id_depends_on_size_and_bar() -> None:
    a = client_order_id("s", "BTCUSDT", LAST, "restop|0.5")
    assert a != client_order_id("s", "BTCUSDT", LAST, "restop|0.3")
    assert a != client_order_id("s", "BTCUSDT", LAST + STEP, "restop|0.5")
