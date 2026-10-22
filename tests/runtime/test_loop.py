"""Runtime loop against FakeVenue: every order via the gate, fail closed."""

import json
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
    Position,
    SymbolRules,
)
from agenkit_trader.killswitch import KillSwitch
from agenkit_trader.log import DecisionLog
from agenkit_trader.regime import REGIMES, label_regimes
from agenkit_trader.risk.gate import RiskGate
from agenkit_trader.runtime.loop import (
    DeployedError,
    Deployment,
    Runtime,
    StartupRefused,
    check_clock_skew,
    client_order_id,
    load_deployed,
    run_loop,
    validate_deployments,
)
from fakes import T0, FakeVenue

STEP = pd.Timedelta(hours=1)
NOW = T0 + timedelta(seconds=30)  # bar opened 11:00 closed at T0=12:00
LAST = pd.Timestamp(T0) - STEP  # open time of the latest CLOSED bar
WINDOW = 5


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
            regime_window=WINDOW,
            state_path=self.state_path,
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


def _last_regime() -> str:
    lab = label_regimes(make_bars(forming=False), WINDOW).iloc[-1]
    assert not np.isnan(lab)
    return REGIMES[int(lab)]


def test_regime_restriction_blocks_entry(tmp_path: Path) -> None:
    other = [r for r in REGIMES if r != _last_regime()]
    h = Harness(
        tmp_path,
        strategy=Scripted({LAST: 1.0}),
        deployments=[dep(allowed=tuple(other))],
    )
    h.rt.start()
    h.rt.cycle()
    assert h.venue.orders == []
    blocked = [r for r in h.records("entry") if r["result"] == "regime_blocked"]
    assert blocked and blocked[0]["inputs"]["regime"] == _last_regime()


def test_regime_allowed_enters(tmp_path: Path) -> None:
    h = Harness(
        tmp_path,
        strategy=Scripted({LAST: 1.0}),
        deployments=[dep(allowed=(_last_regime(),))],
    )
    h.rt.start()
    h.rt.cycle()
    assert len(h.venue.orders) == 1


def test_exit_allowed_in_any_regime(tmp_path: Path) -> None:
    v = FakeVenue(now=NOW, positions={"BTCUSDT": Position("BTCUSDT", 0.5, 100.0)})
    v.stops.append(Order("BTCUSDT", "sell", 0.5, 100.0, 90.0, "scripted", "o", True))
    h = Harness(tmp_path, venue=v, strategy=Scripted({}), deployments=[dep(allowed=())])
    h.rt.start()
    h.rt.cycle()
    assert len(v.orders) == 1 and v.positions == {}


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
            regime_window=WINDOW,
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
        regime_window=WINDOW,
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
        regime_window=WINDOW,
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
        check_clock_skew(v, NOW)
    check_clock_skew(FakeVenue(now=NOW + timedelta(milliseconds=900)), NOW)


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
