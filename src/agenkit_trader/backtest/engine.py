"""Vectorised bar-by-bar backtester with mandatory costs and optional funding.

Conventions (all deliberate, all tested):

Timing. ``bars`` is indexed by bar *open* time on a uniform grid (checked with
the data layer's ``check_continuity``). ``positions[t]`` is the target position
decided at the close of bar ``t``. It is shifted by one bar internally, so the
position that is *effective* during bar ``t`` is ``positions[t-1]`` (flat for
the first bar). A decision at close ``t`` therefore earns only the return from
close ``t`` to close ``t+1``; same-bar fills are impossible. The final
position (decided at the last close) never earns anything.

Position size. Positions are fractions of current equity in ``[-1, 1]``
(+1 fully long, -1 fully short). Leverage is out of scope: ``|position| > 1``
and NaN positions raise ``ValueError`` (NaN is never treated as flat).

Return measurement. Per-bar compounding of equity. With effective position
``p``, close-to-close return ``r`` and start-of-bar equity ``E``, the bar's
P&L is ``p * r * E``; i.e. notional is ``|p| * E`` re-based every bar.
(A held position is thus implicitly rebalanced to the same fraction of equity
each bar; rebalancing is not charged, only explicit changes in ``positions``.)

Costs. Every unit of ``|Δp|`` is charged ``CostModel.per_side_rate``
(taker fee + half spread + slippage) times notional, where notional is
``E`` at the start of the bar in which the new position takes effect:
``cost = |p_t - p_{t-1}| * per_side_rate * E``. A flip from +1 to -1 is
``|Δp| = 2``. Costs are subtracted additively with that bar's P&L:
``E_t = E_{t-1} * (1 + p*r - funding - |Δp|*c)``. Fills are assumed at the
bar boundary price; the engine always pays taker costs.

Funding. ``funding`` is optional (``None`` means zero) and is a Series of
funding rates (fractions, e.g. 0.0001) indexed by ``funding_time``. A payment
at time ``f`` belongs to the bar whose window ``(open, open + step]`` contains
``f``. Longs pay positive funding and shorts receive it: the bar's funding
term is ``p * rate`` (subtracted from the return), applied with the effective
position, so funding is only paid when the position is held in that window.
Funding times outside the bars' time span are ignored.

Trades. A trade is a maximal run of bars with the same non-zero effective
position. ``ret`` is the compounded net return of the bars in the run
(including the entry cost, which is charged in its first bar) times the exit
cost bar if the run is followed by a flat bar. When the run is followed
directly by another position (flip or resize), the exit leg's cost lands on the
next trade's entry bar. A trade still open at the end has no exit cost.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from agenkit_trader.backtest.costs import CostModel
from agenkit_trader.backtest.stats import Stats, compute_stats
from agenkit_trader.data.archive import check_continuity

_SECONDS_PER_YEAR = 365.0 * 24 * 3600
_TRADE_COLUMNS = ["entry_time", "exit_time", "position", "n_bars", "ret"]


@dataclass(frozen=True)
class BacktestResult:
    """``equity`` is indexed like ``bars`` (``equity[0] == equity0``);
    ``trades`` has ``entry_time``, ``exit_time`` (end of last held bar),
    ``position``, ``n_bars`` and ``ret``."""

    equity: pd.Series
    trades: pd.DataFrame
    stats: Stats


def _bar_step(bars: pd.DataFrame) -> pd.Timedelta:
    if len(bars) < 2:
        raise ValueError("need at least 2 bars")
    step = pd.Timedelta(bars.index[1] - bars.index[0])
    seconds = step.total_seconds()
    if seconds <= 0 or seconds % 60:
        raise ValueError(f"unsupported bar spacing {step}")
    check_continuity(bars, f"{int(seconds // 60)}m")
    return step


def _funding_per_bar(
    funding: pd.Series | None, index: pd.DatetimeIndex, step: pd.Timedelta
) -> np.ndarray:
    out = np.zeros(len(index))
    if funding is None or len(funding) == 0:
        return out
    if not isinstance(funding.index, pd.DatetimeIndex) or funding.index.tz is None:
        raise ValueError("funding index must be a tz-aware DatetimeIndex")
    rates = funding.to_numpy(dtype="float64")
    if not np.isfinite(rates).all():
        raise ValueError("funding contains NaN or inf")
    if funding.index.has_duplicates:
        raise ValueError("funding has duplicate funding_time values")
    # bar i's window is (index[i], index[i] + step]
    pos = index.searchsorted(funding.index, side="left") - 1
    valid = pos >= 0
    safe = np.where(valid, pos, 0)
    valid &= np.asarray(funding.index <= index[safe] + step)
    np.add.at(out, pos[valid], rates[valid])
    return out


def _trades(
    index: pd.DatetimeIndex,
    eff: np.ndarray,
    net: np.ndarray,
    step: pd.Timedelta,
) -> pd.DataFrame:
    n = len(eff)
    changed = np.r_[True, eff[1:] != eff[:-1]]
    run_id = np.cumsum(changed)
    rows = []
    for rid in np.unique(run_id[eff != 0]):
        loc = np.flatnonzero(run_id == rid)
        i0, i1 = int(loc[0]), int(loc[-1])
        growth = float(np.prod(1.0 + net[i0 : i1 + 1]))
        if i1 + 1 < n and eff[i1 + 1] == 0:
            growth *= 1.0 + net[i1 + 1]
        rows.append(
            (index[i0], index[i1] + step, float(eff[i0]), i1 - i0 + 1, growth - 1.0)
        )
    return pd.DataFrame(rows, columns=_TRADE_COLUMNS)


def run_backtest(
    bars: pd.DataFrame,
    positions: pd.Series,
    costs: CostModel,
    funding: pd.Series | None,
    equity0: float,
) -> BacktestResult:
    """Simulate ``positions`` on ``bars``; see the module docstring for rules."""
    if not np.isfinite(equity0) or equity0 <= 0:
        raise ValueError(f"equity0 must be finite and > 0, got {equity0!r}")
    if "close" not in bars.columns:
        raise ValueError("bars needs a 'close' column")
    step = _bar_step(bars)
    index = pd.DatetimeIndex(bars.index)
    if not positions.index.equals(index):
        raise ValueError("positions index must equal bars index")

    close = bars["close"].to_numpy(dtype="float64")
    if not np.isfinite(close).all() or (close <= 0).any():
        raise ValueError("close must be finite and > 0")
    decided = positions.to_numpy(dtype="float64")
    if np.isnan(decided).any():
        raise ValueError("positions contain NaN (use 0.0 for flat explicitly)")
    if not np.isfinite(decided).all() or (np.abs(decided) > 1.0).any():
        raise ValueError("position must be within [-1, 1] (no leverage)")

    eff = np.r_[0.0, decided[:-1]]  # decided at close t-1 -> effective in bar t
    ret = np.r_[0.0, close[1:] / close[:-1] - 1.0]
    turnover = np.abs(np.diff(eff, prepend=0.0))
    fund = _funding_per_bar(funding, index, step)

    net = eff * ret - eff * fund - turnover * costs.per_side_rate
    equity = equity0 * np.cumprod(1.0 + net)
    equity_s = pd.Series(equity, index=index, name="equity")
    trades = _trades(index, eff, net, step)
    ppy = _SECONDS_PER_YEAR / step.total_seconds()
    return BacktestResult(equity_s, trades, compute_stats(equity_s, trades, ppy))
