"""Performance statistics computed from an equity curve and a trade list.

Nothing here invents numbers: every field is derived from the inputs, and the
degenerate cases (no trades, no volatility, no losses) are defined explicitly
so the result never contains NaN.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class Stats:
    """Summary statistics.

    ``sharpe``: mean / sample std (ddof=1) of per-bar equity returns times
    ``sqrt(periods_per_year)``, risk-free rate taken as zero; ``0.0`` with no
    trades, fewer than two returns, or zero volatility.
    ``max_drawdown``: largest peak-to-trough fall as a positive fraction of the
    peak (``0.1`` = 10%).
    ``win_rate``: share of trades with ``ret > 0``.
    ``profit_factor``: sum of winning trade returns / absolute sum of losing
    trade returns; ``0.0`` with no trades or no profit, ``inf`` when there is
    profit and no losing trade.
    ``total_return``: ``equity[-1] / equity[0] - 1``.
    """

    sharpe: float
    max_drawdown: float
    win_rate: float
    profit_factor: float
    n_trades: int
    total_return: float


def compute_stats(
    equity: pd.Series, trades: pd.DataFrame, periods_per_year: float
) -> Stats:
    """Compute :class:`Stats`; ``trades`` needs a ``ret`` column (fractional)."""
    if len(equity) == 0:
        raise ValueError("equity is empty")
    eq = equity.to_numpy(dtype="float64")
    if not np.isfinite(eq).all() or (eq <= 0).any():
        raise ValueError("equity must be finite and strictly positive")
    if not math.isfinite(periods_per_year) or periods_per_year <= 0:
        raise ValueError("periods_per_year must be finite and > 0")

    n_trades = len(trades)
    total_return = float(eq[-1] / eq[0] - 1.0)
    drawdown = float((1.0 - eq / np.maximum.accumulate(eq)).max())

    sharpe = 0.0
    if n_trades > 0 and len(eq) > 2:
        r = eq[1:] / eq[:-1] - 1.0
        sd = float(r.std(ddof=1))
        if sd > 1e-12:
            sharpe = float(r.mean() / sd * math.sqrt(periods_per_year))

    win_rate = 0.0
    profit_factor = 0.0
    if n_trades > 0:
        rets = trades["ret"].to_numpy(dtype="float64")
        if not np.isfinite(rets).all():
            raise ValueError("trade returns must be finite")
        win_rate = float((rets > 0).mean())
        gains = float(rets[rets > 0].sum())
        losses = float(-rets[rets < 0].sum())
        if losses > 0:
            profit_factor = gains / losses
        elif gains > 0:
            profit_factor = math.inf
    return Stats(sharpe, drawdown, win_rate, profit_factor, n_trades, total_return)
