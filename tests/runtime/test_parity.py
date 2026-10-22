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
switch's daily-loss/drawdown limits are disabled.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

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
# daily-loss / drawdown kill switch off: the simulator does not model it
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
    tmp_path: Path, bars: pd.DataFrame, sig: pd.Series
) -> tuple[list[dict[str, Any]], int]:
    venue = FakeVenue()
    log = DecisionLog(tmp_path / "decisions.jsonl")
    kill = KillSwitch(venue, log, RISK)
    gate = RiskGate(RISK, venue.symbol_rules, log)
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
    realized = 1000.0
    trades: list[dict[str, Any]] = []
    open_t: dict[str, Any] | None = None
    seen_orders = 0
    started = False
    for k in range(20, N):
        bar = bars.iloc[k]
        # --- the exchange during bar k: the reduce-only stop may trigger ---
        pos = venue.positions.get(SYM)
        if pos is not None and started:
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
                realized += pos.qty * (fill - pos.entry_price)
                venue.positions.pop(SYM)
                venue.stops.clear()
                assert open_t is not None
                open_t.update(exit_bar=k, reason="stop", exit_price=fill)
                trades.append(open_t)
                open_t = None
        # --- venue state at the close of bar k ---
        pos = venue.positions.get(SYM)
        close = float(bar["close"])
        venue.equity = realized + (pos.qty * (close - pos.entry_price) if pos else 0.0)
        forming = bars.iloc[[k]].copy()
        forming.index = forming.index + STEP
        venue.bars[SYM] = pd.concat([bars.iloc[: k + 1], forming])
        now = (bars.index[k] + STEP + pd.Timedelta(seconds=30)).to_pydatetime()
        clock["now"] = venue.now = now
        if not started:
            rt.start()
            started = True
        rt.cycle()
        assert not kill.halted
        for o in venue.orders[seen_orders:]:
            if o.reduce_only:  # strategy exit at the close of bar k
                assert open_t is not None
                realized += open_t["signed"] * (o.price_ref - open_t["entry_price"])
                target = float(np.sign(sig.iloc[k]))
                reason = "flip" if target == -open_t["side"] else "signal"
                open_t.update(exit_bar=k, reason=reason, exit_price=o.price_ref)
                trades.append(open_t)
                open_t = None
            else:
                side = 1.0 if o.side == "buy" else -1.0
                stops = [s for s in venue.stops if s.client_id != ""]
                assert stops and stops[-1].stop_price == o.stop_price
                assert stops[-1].qty == o.qty
                open_t = {
                    "entry_bar": k,
                    "side": side,
                    "qty": o.qty,
                    "stop": o.stop_price,
                    "entry_price": o.price_ref,
                    "signed": side * o.qty,
                }
        seen_orders = len(venue.orders)
        # entry price as the venue records it (used by the stop/exit P&L)
        p = venue.positions.get(SYM)
        if p is not None:
            assert isinstance(p, Position) and p.entry_price == close
    if open_t is not None:
        open_t.update(
            exit_bar=N - 1, reason="end", exit_price=float(bars["close"].iloc[-1])
        )
        trades.append(open_t)
    skipped = 0

    for line in (tmp_path / "decisions.jsonl").read_text().splitlines():
        r = json.loads(line)
        if r["kind"] == "order" and r["inputs"]["kind"] == "entry":
            skipped += r["result"] in ("rejected", "refused_qty_mismatch")
        elif r["kind"] == "entry" and r["result"] in ("size_zero", "no_atr"):
            skipped += 1
    return trades, skipped


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


def test_simulator_matches_runtime_loop(tmp_path: Path) -> None:
    bars, sig = scenario()
    rules = FakeVenue().rules[SYM]
    res = run_backtest_runtime(bars, sig, FREE, None, 1000.0, RISK, RT, rules)
    rt_trades, rt_skipped = run_runtime(tmp_path, bars, sig)
    rt_rows = [
        (
            t["entry_bar"],
            t["side"],
            t["qty"],
            t["stop"],
            t["exit_bar"],
            t["reason"],
            t["exit_price"],
        )
        for t in rt_trades
    ]
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
