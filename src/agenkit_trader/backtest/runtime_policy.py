"""Bar simulator of what the paper RUNTIME trades (``runtime/loop.py``).

``run_backtest`` (engine.py) evaluates a signal's position as a fraction of
equity, rebalanced every bar, with no stops. The runtime does something else;
this module simulates that instead, rule for rule. The runtime is the
reference: where they could differ, this file follows ``Runtime._step`` /
``Runtime._enter``.

Decisions. At the close of bar ``t`` (using bars up to ``t`` only) the
signal is reduced to its sign: ``target = sign(signal[t])``, ``prev =
sign(signal[t-1])`` (0 before the first bar).

* Exit: a held position whose side differs from ``target`` is closed at the
  close of ``t`` (reason ``flip`` if ``target`` is the opposite side, else
  ``signal``).
* Entry: only if flat (after any exit), ``target != 0`` and ``target !=
  prev``: a signal TRANSITION at bar ``t``. A stop-out or a skipped entry is
  therefore not re-entered while the signal keeps its value; it takes a change
  of the signal (to 0 and back, or to the opposite side) to enter again.
* No entry is opened at the final bar (it could never earn anything).

Sizing (``Runtime._enter``). ``atr`` = simple-mean ATR(``atr_window``) of the
last ``bars_limit`` closed bars (``warmup`` bars may precede ``bars`` for
history). Stop = ``protective_stop(close, side, stop_atr_mult * atr, tick)``
with ``rules`` (rounded toward the entry), else the unrounded ``close -
side * stop_atr_mult * atr``. ``qty = size_position(equity, close, stop,
risk_cfg, rules=rules)`` (1% risk at the stop, no Kelly, floored to the lot
when ``rules`` is given). The entry is SKIPPED (counted in
``skipped_entries``) if the ATR is unusable, the size is zero, or the risk
gate would reject the order: with ``rules`` the real ``RiskGate.check`` is
called; without, the gate's risk-per-trade, leverage and symbol-notional
inequalities are applied to the unrounded qty. The runtime does not cap a
quantity, it submits it and the gate rejects it, so neither does this.
``equity`` is the simulated equity at that close; the gate sees it as day
start and peak equity too (the runtime's daily-loss/drawdown kill switch is
NOT simulated). The quantity is then HELD CONSTANT until exit: no
rebalancing, so the fraction of equity drifts with price.

Stop. A reduce-only exchange stop at ``stop`` triggers INTRABAR in any bar
after the entry bar: long if ``low <= stop`` (short: ``high >= stop``). It
fills at ``stop``, or at the bar's ``open`` if the bar opened through it
(the worse price). The stop is checked before the bar's close decision, so a
stop and a signal exit in the same bar is a stop.

Costs. Every fill (entry, signal/flip exit, stop, final liquidation) pays
``CostModel.per_side_rate`` on ``qty * fill price``.

Funding. A payment at time ``f`` belongs to bar ``t`` whose window ``(open,
open + step]`` contains ``f`` (as in ``run_backtest``); it is paid on the
quantity held through that bar: ``signed_qty * close[t] * rate`` (longs pay
positive funding). A position stopped out within the bar pays nothing.

Equity. ``equity[t]`` is the marked-to-close equity at bar ``t`` BEFORE that
close's decision fills; their costs show from ``equity[t+1]`` on (so
``equity[0] == equity0``, as in ``run_backtest``). At the final bar every
exit, plus the liquidation of a position still open (reason ``end``), is
included in ``equity[-1]``. If equity reaches <= 0 at a close or after a stop
fill, it is 0 from that bar on, the position is closed (reason ``ruin`` if
still open) and ``ruined`` is True.

Trades. One row per position: ``entry_time`` (first bar the position is held,
= close of the decision bar), ``exit_time`` (close of the exit bar),
``side``, ``qty``, ``entry_price``, ``stop_price``, ``exit_price``,
``exit_reason`` (``signal``|``flip``|``stop``|``end``|``ruin``), ``n_bars``
and ``ret`` (net return on the equity at entry, including both legs' costs
and funding).

What this cannot reproduce: real exchange stop fills (the fill rule above is
a model), testnet fills and fees, live slippage against a 1h-ATR stop, the
kill switch, and strategies whose signal depends on the runtime's limited
``bars_limit`` window (research computes signals on the whole history).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from agenkit_trader.backtest.costs import CostModel
from agenkit_trader.backtest.engine import (
    _SECONDS_PER_YEAR,
    BacktestResult,
    _bar_step,
    _funding_per_bar,
)
from agenkit_trader.backtest.stats import compute_stats
from agenkit_trader.config import RiskConfig, RuntimeConfig
from agenkit_trader.execution.venue import AccountState, Order, SymbolRules
from agenkit_trader.risk.gate import RiskGate
from agenkit_trader.risk.sizing import size_position
from agenkit_trader.runtime.loop import atr, protective_stop

TRADE_COLUMNS = [
    "entry_time",
    "exit_time",
    "side",
    "qty",
    "entry_price",
    "stop_price",
    "exit_price",
    "exit_reason",
    "n_bars",
    "ret",
]
_OHLC = ["open", "high", "low", "close"]
_EPS = 1e-12  # the gate's relative tolerance on limits


@dataclass(frozen=True)
class RuntimePolicy:
    """Everything the runtime policy needs besides bars, signal and costs."""

    risk: RiskConfig = field(default_factory=RiskConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    rules: SymbolRules | None = None


def _dtypes(index: pd.DatetimeIndex) -> dict[str, Any]:
    t, f = index.dtype, "float64"
    kinds = [t, t, f, f, f, f, f, "object", "int64", f]
    return dict(zip(TRADE_COLUMNS, kinds, strict=True))


def _limit_rejects(
    qty: float, price: float, stop: float, equity: float, cfg: RiskConfig
) -> bool:
    """The gate's risk / leverage / symbol-notional checks for an opening
    order on a flat symbol with no other positions (``RiskGate._decide``)."""
    if qty * abs(price - stop) > equity * cfg.max_risk_per_trade * (1 + _EPS):
        return True
    lev = qty * price / equity
    return bool(
        lev > cfg.max_leverage * (1 + _EPS)
        or lev > cfg.max_symbol_notional_frac * (1 + _EPS)
    )


def _ohlc(bars: pd.DataFrame, what: str) -> pd.DataFrame:
    missing = [c for c in _OHLC if c not in bars.columns]
    if missing:
        raise ValueError(f"{what} needs columns {missing}")
    out = bars[_OHLC].astype("float64")
    v = out.to_numpy()
    if not np.isfinite(v).all() or (v <= 0).any():
        raise ValueError(f"{what} open/high/low/close must be finite and > 0")
    return out


def run_backtest_runtime(
    bars: pd.DataFrame,
    signal: pd.Series,
    costs: CostModel,
    funding: pd.Series | None,
    equity0: float,
    risk_cfg: RiskConfig,
    runtime_cfg: RuntimeConfig,
    rules: SymbolRules | None = None,
    *,
    warmup: pd.DataFrame | None = None,
) -> BacktestResult:
    """Simulate the runtime's policy on ``signal``; see the module docstring."""
    if not np.isfinite(equity0) or equity0 <= 0:
        raise ValueError(f"equity0 must be finite and > 0, got {equity0!r}")
    step = _bar_step(bars)
    index = pd.DatetimeIndex(bars.index)
    if not signal.index.equals(index):
        raise ValueError("signal index must equal bars index")
    raw = signal.to_numpy(dtype="float64")
    if not np.isfinite(raw).all():
        raise ValueError("signal contains NaN or inf (use 0.0 for flat explicitly)")
    sgn = np.sign(raw)
    px = _ohlc(bars, "bars")
    hist = px
    off = 0
    if warmup is not None and len(warmup):
        hist = pd.concat([_ohlc(warmup, "warmup"), px])
        _bar_step(hist)  # warm-up must continue the grid without a gap
        off = len(warmup)
    o, h, lo, cl = (px[c].to_numpy() for c in _OHLC)
    fund = _funding_per_bar(funding, index, step)
    rate = costs.per_side_rate
    gate = RiskGate(risk_cfg, lambda _s: rules) if rules is not None else None
    n = len(index)

    cash = equity0  # realised equity (costs, funding, closed P&L)
    q = 0.0  # signed quantity held
    entry_px = stop = entry_eq = 0.0
    entry_i = 0
    rows: list[tuple[object, ...]] = []
    equity = np.zeros(n)
    skipped = 0
    ruined = False
    prev = 0.0

    def close_pos(i: int, price: float, reason: str) -> None:
        nonlocal cash, q
        cash += q * (price - entry_px) - abs(q) * price * rate
        rows.append(
            (
                index[entry_i] + step,
                index[i] + step,
                float(np.sign(q)),
                abs(q),
                entry_px,
                stop,
                price,
                reason,
                i - entry_i,
                max(cash, 0.0) / entry_eq - 1.0,
            )
        )
        q = 0.0

    def try_enter(i: int, side: float) -> bool:
        nonlocal cash, q, entry_px, stop, entry_eq, entry_i
        end = off + i + 1
        a = atr(
            hist.iloc[max(0, end - runtime_cfg.bars_limit) : end],
            runtime_cfg.atr_window,
        )
        if not (math.isfinite(a) and a > 0):
            return False
        price = float(cl[i])
        dist = runtime_cfg.stop_atr_mult * a
        st = (
            protective_stop(price, side, dist, rules.tick)
            if rules is not None
            else price - side * dist
        )
        qty = size_position(cash, price, st, risk_cfg, rules=rules)
        if not qty > 0 or not st > 0:
            return False
        if gate is not None:
            t_close = pd.Timestamp(index[i] + step)
            if t_close.tzinfo is None:
                t_close = t_close.tz_localize("UTC")
            now = t_close.to_pydatetime()
            order = Order("SIM", "buy" if side > 0 else "sell", qty, price, st, "sim")
            state = AccountState(cash, {}, cash, cash, now, False)
            d = gate.check(order, state, now)
            if not d.allowed or d.max_qty != qty:
                return False
        elif _limit_rejects(qty, price, st, cash, risk_cfg):
            return False
        entry_eq, entry_px, stop, entry_i = cash, price, st, i
        q = side * qty
        cash -= qty * price * rate
        return True

    for t in range(n):
        if ruined:
            continue  # equity stays 0
        if q != 0:
            fill = None
            if q > 0:
                if o[t] <= stop:
                    fill = float(o[t])
                elif lo[t] <= stop:
                    fill = stop
            elif o[t] >= stop:
                fill = float(o[t])
            elif h[t] >= stop:
                fill = stop
            if fill is not None:
                close_pos(t, fill, "stop")
        if q != 0:
            cash -= q * cl[t] * fund[t]
        mtm = cash + q * (cl[t] - entry_px)
        if not mtm > 0:
            if q != 0:
                close_pos(t, float(cl[t]), "ruin")
            ruined = True
            continue
        equity[t] = mtm
        target = float(sgn[t])
        held = float(np.sign(q))
        if held != 0 and target != held:
            close_pos(t, float(cl[t]), "flip" if target == -held else "signal")
        if t == n - 1:
            if q != 0:
                close_pos(t, float(cl[t]), "end")
            equity[t] = max(cash, 0.0)
            break
        if target != 0 and q == 0 and target != prev and not try_enter(t, target):
            skipped += 1
        prev = target

    equity_s = pd.Series(equity, index=index, name="equity")
    trades = pd.DataFrame(rows, columns=TRADE_COLUMNS).astype(
        _dtypes(index)  # typed columns, also when empty
    )
    ppy = _SECONDS_PER_YEAR / step.total_seconds()
    return BacktestResult(
        equity_s,
        trades,
        compute_stats(equity_s, trades, ppy),
        ruined=ruined,
        skipped_entries=skipped,
    )
