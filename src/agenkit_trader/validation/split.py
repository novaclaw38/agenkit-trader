"""Chronological train / hold-out split."""

from __future__ import annotations

import math

import pandas as pd


def split_by_time(
    bars: pd.DataFrame, holdout_frac: float
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Sort by time and return ``(train, holdout)``.

    The hold-out is the *last* ``round(len * holdout_frac)`` bars; the segments
    share no timestamp and ``train.index.max() < holdout.index.min()``. Each
    segment must have at least 2 bars. Duplicate timestamps raise.
    """
    if not math.isfinite(holdout_frac) or not 0 < holdout_frac < 1:
        raise ValueError(f"holdout_frac must be in (0, 1), got {holdout_frac!r}")
    if not isinstance(bars.index, pd.DatetimeIndex):
        raise ValueError("bars must have a DatetimeIndex")
    if bars.index.has_duplicates:
        raise ValueError("bars have duplicate timestamps")
    ordered = bars.sort_index()
    n = len(ordered)
    n_hold = int(round(n * holdout_frac))
    if n_hold < 2 or n - n_hold < 2:
        raise ValueError(f"{n} bars too few to split at holdout_frac={holdout_frac}")
    return ordered.iloc[: n - n_hold], ordered.iloc[n - n_hold :]
