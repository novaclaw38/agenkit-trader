"""Parity: the runtime-policy simulator vs the REAL runtime loop.

The same synthetic bars and scripted signal are fed (a) to
``run_backtest_runtime`` and (b) bar by bar, with an injected clock, to
``Runtime`` (``runtime/loop.py``) on a ``FakeVenue``. Both must produce the
same entries (decision bar, side, quantity, stop price), the same exits
(bar, reason) and the same skipped entries.

What this covers: the runtime's decision rules (sign only, entries only on a
transition, no re-entry after a stop-out or a skipped entry, flips, exit when
the signal changes), its ATR stop and tick rounding, 1% sizing floored to the
lot, gate rejections at the leverage cap, and a stop and a signal exit in the
same bar.

What it cannot cover: FakeVenue has no matching engine, so the exchange's
stop trigger is played by this harness with the SAME rule the simulator uses
(long: open <= stop fills at open, else low <= stop fills at the stop; short
mirrored). The parity test therefore checks that the runtime, given those
stop fills, makes the same decisions; it does not validate real exchange
stop fills, testnet fills, fees or slippage. Costs and funding are zero here
(the venue's equity is driven by the harness from fills), and the kill
switch's daily-loss/drawdown limits are disabled except in the halt tests.
The harness also plays the intrabar poll: one cycle per bar with the venue
equity marked at the bar's adverse extreme (the simulator's rule).
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from agenkit_trader.backtest.costs import CostModel
from agenkit_trader.backtest.runtime_policy import run_backtest_runtime
from agenkit_trader.config import RiskConfig, RuntimeConfig
from agenkit_trader.execution.venue import Position
from agenkit_trader.killswitch import KillSwitch
from agenkit_trader.log import DecisionLog
from agenkit_trader.regime import REGIMES
from agenkit_trader.risk.gate import RiskGate
from agenkit_trader.runtime.loop import Deployment, Runtime
from fakes import FakeVenue

STEP = pd.Timedelta(hours=1)
SYM = "BTCUSDT"
N = 140
# kill switch off for the long scenario (its losses would trip it); the
# halt scenarios below run with the committed limits
RISK = RiskConfig(max_daily_loss=1.0, max_drawdown=1.0)
RT = RuntimeConfig()
FREE = CostModel(0.0, 0.0, 0.0, 0.0)


class ScriptedSeries:
    """Signal = a fixed per-bar series (looked up by bar open time)."""

    name = "scripted"
    edge = "test"
    param_grid: dict[str, list[Any]] = {"k": [1, 2]}

    def __init__(self, s: pd.Series) -> None:
        self.s = s

    def signal(
        self, bars: pd.DataFrame, params: dict[str, Any], aux: dict[str, pd.Series]
    ) -> pd.Series:
        return self.s.reindex(bars.index).fillna(0.0)


def scenario() -> tuple[pd.DataFrame, pd.Series]:
    idx = pd.date_range("2026-01-01", periods=N, freq="1h", tz="UTC")
    c = np.full(N, 100.0)
    b = pd.DataFrame(
        {"open": c, "high": c + 1.0, "low": c - 1.0, "close": c.copy(), "volume": 1.0},
        index=idx,
    )
    s = pd.Series(0.0, index=idx)

    def span(a: int, z: int, v: float) -> None:
        s.iloc[a:z] = v

    # long at 30; intrabar stop at 34; signal stays +1 -> no re-entry
    span(30, 42, 1.0)
    b.iloc[34, b.columns.get_loc("low")] = 93.5
    # back to 0 at 42, fresh +1 at 43 -> re-entry; flip to short at 50
    span(43, 50, 1.0)
    span(50, 58, -1.0)
    # short stopped by a bar that GAPS through the stop (fill at the open)
    b.iloc[55, :4] = [109.0, 110.0, 108.0, 109.0]
    # long at 62; bar 66 hits the stop AND the signal exits at its close
    span(62, 66, 1.0)
    b.iloc[66, b.columns.get_loc("low")] = 85.0
    # short 70..74, plain signal exit at 75
    span(70, 75, -1.0)
    # quiet market: ATR 0.1 -> 1% sizing exceeds 3x leverage -> gate rejects
    for k in range(80, 101):
        b.iloc[k, b.columns.get_loc("high")] = 100.05
        b.iloc[k, b.columns.get_loc("low")] = 99.95
    span(100, 105, 1.0)
    # long from 125 to the end -> liquidated at the end
    span(125, N, 1.0)
    return b, s


def run_runtime(
    tmp_path: Path,
    bars: pd.DataFrame,
    sig: pd.Series,
    risk: RiskConfig = RISK,
    costs: CostModel = FREE,
    first: int = 20,
) -> tuple[list[dict[str, Any]], int, int | None]:
    """Drive the real Runtime bar by bar. The harness plays the exchange:
    stop triggers (simulator's fill rule), fills debit ``costs`` from the
    venue equity, and a kill-switch flatten closes at the current close.
    Returns (trades, skipped entries, bar of the halt or None)."""
    n = len(bars)
    venue = FakeVenue()
    log = DecisionLog(tmp_path / "decisions.jsonl")
    kill = KillSwitch(venue, log, risk)
    gate = RiskGate(risk, venue.symbol_rules, log)
    dep = Deployment("scripted", SYM, "1h", {"k": 1}, list(REGIMES))
    clock = {"now": venue.now}
    rt = Runtime(
        venue,
        [ScriptedSeries(sig)],
        [dep],
        gate,
        kill,
        log,
        lambda: clock["now"],
        runtime_cfg=RT,
        state_path=tmp_path / "state.json",
        init_state=True,
    )
    rate = costs.per_side_rate
    acc = {"realized": 1000.0, "k": first}
    trades: list[dict[str, Any]] = []
    cur: dict[str, Any] = {}

    def close_trade(qty: float, entry: float, price: float, reason: str) -> None:
        acc["realized"] += qty * (price - entry) - abs(qty) * price * rate
        cur.update(exit_bar=acc["k"], reason=reason, exit_price=price)
        trades.append(dict(cur))
        cur.clear()

    real_place, real_flatten = venue.place_order, venue.flatten_all

    def place(order: Any) -> Any:
        before = venue.positions.get(SYM)
        fill = real_place(order)
        if order.reduce_only:  # strategy exit at the close of bar k
            assert before is not None
            target = float(np.sign(sig.iloc[acc["k"]]))
            reason = "flip" if target == -np.sign(before.qty) else "signal"
            close_trade(before.qty, before.entry_price, order.price_ref, reason)
        else:
            acc["realized"] -= order.qty * order.price_ref * rate
            cur.update(
                entry_bar=acc["k"],
                side=1.0 if order.side == "buy" else -1.0,
                qty=order.qty,
                stop=order.stop_price,
            )
        venue.equity = acc["realized"]  # flat or just entered at price_ref
        return fill

    def flatten() -> None:
        pos = venue.positions.get(SYM)
        if pos is not None:
            px = acc.get("px", float(bars["close"].iloc[acc["k"]]))
            close_trade(pos.qty, pos.entry_price, px, "halt")
        real_flatten()
        venue.equity = acc["realized"]

    venue.place_order = place  # type: ignore[method-assign]
    venue.flatten_all = flatten  # type: ignore[method-assign]

    def cycle(k: int, now: pd.Timestamp, closed_upto: int) -> None:
        forming = (
            bars.iloc[[closed_upto]].copy()
            if closed_upto < n
            else bars.iloc[[-1]].copy()
        )
        forming.index = pd.DatetimeIndex([bars.index[closed_upto - 1] + STEP])
        venue.bars[SYM] = pd.concat([bars.iloc[:closed_upto], forming])
        clock["now"] = venue.now = now.to_pydatetime()
        rt.cycle()

    halt_bar: int | None = None
    for k in range(first, n):
        acc["k"] = k
        bar = bars.iloc[k]
        pos = venue.positions.get(SYM)
        if pos is not None and k > first:
            # --- the exchange during bar k: the reduce-only stop may trigger
            fill = None
            for st in venue.stops:
                sp = st.stop_price
                assert sp is not None
                if pos.qty > 0 and st.side == "sell":
                    if bar["open"] <= sp:
                        fill = float(bar["open"])
                    elif bar["low"] <= sp:
                        fill = sp
                elif pos.qty < 0 and st.side == "buy":
                    if bar["open"] >= sp:
                        fill = float(bar["open"])
                    elif bar["high"] >= sp:
                        fill = sp
            if fill is not None:
                close_trade(pos.qty, pos.entry_price, fill, "stop")
                venue.positions.pop(SYM)
                venue.stops.clear()
                venue.equity = acc["realized"]
                # the runtime polls within the bar: one cycle after the fill
                cycle(k, bars.index[k] + pd.Timedelta(minutes=30), k)
                if kill.halted:
                    halt_bar = k
                    break
        # --- venue state at the close of bar k ---
        pos = venue.positions.get(SYM)
        if pos is not None and k > first:
            # no stop hit: the runtime polls within the bar, at the bar's
            # adverse extreme (long: low, short: high)
            adv = float(bar["low"] if pos.qty > 0 else bar["high"])
            acc["px"] = adv
            venue.equity = acc["realized"] + pos.qty * (adv - pos.entry_price)
            cycle(k, bars.index[k] + pd.Timedelta(minutes=45), k)
            acc.pop("px")
            if kill.halted:
                halt_bar = k
                break
        close = float(bar["close"])
        venue.equity = acc["realized"] + (
            pos.qty * (close - pos.entry_price) if pos else 0.0
        )
        if k == first:
            clock["now"] = venue.now = (
                bars.index[k] + STEP + pd.Timedelta(seconds=30)
            ).to_pydatetime()
            venue.bars[SYM] = bars.iloc[: k + 1]
            rt.start()
        cycle(k, bars.index[k] + STEP + pd.Timedelta(seconds=30), k + 1)
        if kill.halted:
            halt_bar = k
            break
        if cur:
            stops = [s for s in venue.stops if s.client_id != ""]
            assert stops and stops[-1].stop_price == cur["stop"]
            assert stops[-1].qty == cur["qty"]
            p = venue.positions.get(SYM)
            assert isinstance(p, Position) and p.entry_price == close
    if cur and halt_bar is None:
        acc["k"] = n - 1
        p = venue.positions[SYM]
        close_trade(p.qty, p.entry_price, float(bars["close"].iloc[-1]), "end")
    skipped = 0
    for line in (tmp_path / "decisions.jsonl").read_text().splitlines():
        r = json.loads(line)
        if r["kind"] == "order" and r["inputs"]["kind"] == "entry":
            skipped += r["result"] in ("rejected", "refused_qty_mismatch")
        elif r["kind"] == "entry" and r["result"] in ("size_zero", "no_atr"):
            skipped += 1
    return trades, skipped, halt_bar


def _sim_rows(res: Any) -> list[tuple[Any, ...]]:
    t0 = res.equity.index[0]
    out = []
    for r in res.trades.itertuples():
        entry_bar = int((r.entry_time - STEP - t0) / STEP)
        exit_bar = int((r.exit_time - STEP - t0) / STEP)
        out.append(
            (
                entry_bar,
                r.side,
                r.qty,
                r.stop_price,
                exit_bar,
                r.exit_reason,
                r.exit_price,
            )
        )
    return out


def _rt_rows(trades: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    keys = ("entry_bar", "side", "qty", "stop", "exit_bar", "reason", "exit_price")
    return [tuple(t[k] for k in keys) for t in trades]


def test_simulator_matches_runtime_loop(tmp_path: Path) -> None:
    bars, sig = scenario()
    rules = FakeVenue().rules[SYM]
    res = run_backtest_runtime(bars, sig, FREE, None, 1000.0, RISK, RT, rules)
    rt_trades, rt_skipped, rt_halt = run_runtime(tmp_path, bars, sig)
    assert rt_halt is None and not res.halted
    rt_rows = _rt_rows(rt_trades)
    sim_rows = _sim_rows(res)
    assert sim_rows == rt_rows
    assert res.skipped_entries == rt_skipped == 1
    # the scenario really exercises every case
    reasons = [r[5] for r in sim_rows]
    assert reasons == ["stop", "flip", "stop", "stop", "signal", "end"]
    entries = [r[0] for r in sim_rows]
    assert entries == [30, 43, 50, 62, 70, 125]
    gap = sim_rows[2]
    assert gap[1] == -1.0 and gap[6] == 109.0 and gap[6] > gap[3]  # filled at open
    assert sim_rows[3][4] == 66  # stop and signal exit in the same bar: stop


def test_parity_scenario_timeline_is_not_trivial() -> None:
    bars, sig = scenario()
    assert (sig.iloc[34:42] == 1.0).all()  # stopped at 34, signal still long
    assert timedelta(hours=N - 1) == bars.index[-1] - bars.index[0]


def halt_scenario(gap_stop: bool) -> tuple[pd.DataFrame, pd.Series]:
    """Two stop-outs, then a third long whose close drop trips the 3% daily
    loss with the position OPEN (gap_stop=False), or one long stopped by a
    gap that alone loses more than 3% (gap_stop=True)."""
    n = 40
    idx = pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC")
    c = np.full(n, 100.0)
    b = pd.DataFrame(
        {"open": c, "high": c + 1.0, "low": c - 1.0, "close": c.copy(), "volume": 1.0},
        index=idx,
    )
    s = pd.Series(0.0, index=idx)
    if gap_stop:
        s.iloc[15:30] = 1.0
        b.iloc[17, :4] = [75.0, 76.0, 74.0, 75.0]
        return b, s
    s.iloc[15:17] = 1.0
    b.iloc[16, b.columns.get_loc("low")] = 93.5
    s.iloc[18:20] = 1.0
    b.iloc[19, b.columns.get_loc("low")] = 92.0
    s.iloc[21:30] = 1.0
    b.iloc[22, [b.columns.get_loc("low"), b.columns.get_loc("close")]] = [91.6, 91.6]
    return b, s


COSTS = CostModel(0.002, 0.0, 0.0, 0.0)


def test_kill_switch_halt_with_open_position_matches(tmp_path: Path) -> None:
    bars, sig = halt_scenario(gap_stop=False)
    risk = RiskConfig()  # committed limits: 3% daily loss, 10% drawdown
    rules = FakeVenue().rules[SYM]
    res = run_backtest_runtime(bars, sig, COSTS, None, 1000.0, risk, RT, rules)
    rt_trades, _, rt_halt = run_runtime(tmp_path, bars, sig, risk, COSTS, first=14)
    assert _sim_rows(res) == _rt_rows(rt_trades)
    assert [r[5] for r in _sim_rows(res)] == ["stop", "stop", "halt"]
    assert res.halted and res.halt_reason == "daily_loss"
    assert rt_halt == 22 and res.halt_time == bars.index[22]


def test_kill_switch_halt_after_gap_stop_matches(tmp_path: Path) -> None:
    bars, sig = halt_scenario(gap_stop=True)
    risk = RiskConfig()
    rules = FakeVenue().rules[SYM]
    res = run_backtest_runtime(bars, sig, COSTS, None, 1000.0, risk, RT, rules)
    rt_trades, _, rt_halt = run_runtime(tmp_path, bars, sig, risk, COSTS, first=14)
    assert _sim_rows(res) == _rt_rows(rt_trades)
    assert [r[5] for r in _sim_rows(res)] == ["stop"]
    assert res.halted and res.halt_reason == "daily_loss"
    assert rt_halt == 17 and res.halt_time == bars.index[17]


def test_costs_parity_without_halt(tmp_path: Path) -> None:
    bars, sig = scenario()
    rules = FakeVenue().rules[SYM]
    res = run_backtest_runtime(bars, sig, COSTS, None, 1000.0, RISK, RT, rules)
    rt_trades, rt_skipped, rt_halt = run_runtime(tmp_path, bars, sig, RISK, COSTS)
    assert rt_halt is None
    assert _sim_rows(res) == _rt_rows(rt_trades)
    assert res.skipped_entries == rt_skipped


def dip_scenario() -> tuple[pd.DataFrame, pd.Series]:
    """halt_scenario's two stop-outs, then a long whose bar 22 DIPS to 92.3
    (above its stop at 94) and closes back at 100: no close-based check
    sees a loss, the intrabar poll at the low does."""
    b, s = halt_scenario(gap_stop=False)
    b.iloc[22, [b.columns.get_loc("low"), b.columns.get_loc("close")]] = [92.3, 100.0]
    return b, s


def test_intrabar_dip_that_recovers_halts_like_the_runtime(tmp_path: Path) -> None:
    bars, sig = dip_scenario()
    risk = RiskConfig()
    rules = FakeVenue().rules[SYM]
    res = run_backtest_runtime(bars, sig, COSTS, None, 1000.0, risk, RT, rules)
    rt_trades, _, rt_halt = run_runtime(tmp_path, bars, sig, risk, COSTS, first=14)
    assert _sim_rows(res) == _rt_rows(rt_trades)
    assert [r[5] for r in _sim_rows(res)] == ["stop", "stop", "halt"]
    assert res.halted and res.halt_reason == "daily_loss"
    assert rt_halt == 22 and res.halt_time == bars.index[22]
    assert _sim_rows(res)[-1][6] == 92.3  # flattened at the low, not the close
    # without the intrabar check this bar (close 100) would not have halted
    assert bars["close"].iloc[22] == 100.0


# ---- window parity: signals computed on the runtime's rolling window -------


def _walk(seed: int, n: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    c = 100 * np.cumprod(1 + rng.normal(0, 0.01, n))
    h = c * (1 + np.abs(rng.normal(0, 0.004, n)))
    lo = c * (1 - np.abs(rng.normal(0, 0.004, n)))
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame(
        {"open": c, "high": h, "low": lo, "close": c, "volume": 1.0}, index=idx
    )


def _runtime_signals(
    tmp_path: Path, bars: pd.DataFrame, strat: Any, params: dict[str, Any], first: int
) -> dict[int, tuple[float, float]]:
    """(target, prev_target) the real Runtime logs at each closed bar."""
    venue = FakeVenue()
    log = DecisionLog(tmp_path / "decisions.jsonl")
    kill = KillSwitch(venue, log, RISK)
    gate = RiskGate(RISK, venue.symbol_rules, log)
    dep = Deployment(strat.name, SYM, "1h", params, list(REGIMES))
    clock = {"now": venue.now}
    rt = Runtime(
        venue,
        [strat],
        [dep],
        gate,
        kill,
        log,
        lambda: clock["now"],
        runtime_cfg=RT,
        state_path=tmp_path / "state.json",
        init_state=True,
    )
    for k in range(first, len(bars)):
        forming = bars.iloc[[k]].copy()
        forming.index = pd.DatetimeIndex([bars.index[k] + STEP])
        venue.bars[SYM] = pd.concat([bars.iloc[: k + 1], forming])
        now = bars.index[k] + STEP + pd.Timedelta(seconds=30)
        clock["now"] = venue.now = now.to_pydatetime()
        venue.equity = 1000.0
        if k == first:
            rt.start()
        rt.cycle()
        assert not kill.halted
    out: dict[int, tuple[float, float]] = {}
    for line in (tmp_path / "decisions.jsonl").read_text().splitlines():
        r = json.loads(line)
        if r["kind"] == "signal":
            t = int((pd.Timestamp(r["inputs"]["bar_open"]) - bars.index[0]) / STEP)
            out[t] = (r["output"]["target"], r["output"]["prev_target"])
    return out


def test_window_signals_equal_runtime_signals_trend_breakout_336(
    tmp_path: Path,
) -> None:
    from agenkit_trader.backtest.runtime_policy import window_signals
    from agenkit_trader.strategies import REGISTRY

    strat = REGISTRY["trend_breakout"]
    params = {"lookback": 336, "vol_cap": 1.0}
    first = RT.bars_limit + 20
    differs = 0
    for seed in (0, 1):
        bars = _walk(seed, first + 400)
        d = tmp_path / f"s{seed}"
        d.mkdir()
        rt = _runtime_signals(d, bars, strat, params, first)
        tgt, prv = window_signals(strat, bars, params, {}, RT.bars_limit, first)
        assert sorted(rt) == list(range(first, len(bars)))
        assert [rt[t] for t in range(first, len(bars))] == list(
            zip(tgt.tolist(), prv.tolist(), strict=True)
        )
        full = strat.signal(bars, params, {})
        differs += int((np.sign(full.iloc[first:].to_numpy()) != tgt).sum())
    assert differs > 0, "full-history signal differs from the window here"


def test_window_guard_refuses_lookback_plus_warmup_beyond_bars_limit() -> None:
    from agenkit_trader.backtest.runtime_policy import WindowError, window_signals
    from agenkit_trader.strategies import REGISTRY

    strat = REGISTRY["trend_breakout"]
    bars = _walk(0, 300)
    with pytest.raises(WindowError, match="runtime window"):
        window_signals(strat, bars, {"lookback": 336, "vol_cap": 1.0}, {}, 700, 0)
    window_signals(strat, bars, {"lookback": 48, "vol_cap": 1.0}, {}, 1000, 290)
