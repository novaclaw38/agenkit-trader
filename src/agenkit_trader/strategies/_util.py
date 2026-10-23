"""Shared helpers for the research strategies."""

from __future__ import annotations

import numpy as np
import pandas as pd


def realised_vol(close: pd.Series, window: int) -> pd.Series:
    """Rolling std of 1-bar log returns over ``window`` bars (causal)."""
    c = close.astype("float64")
    r = pd.Series(np.log(c.to_numpy()), index=c.index).diff()
    return r.rolling(window, min_periods=window).std()


def efficiency_ratio(close: pd.Series, window: int) -> pd.Series:
    """|net move| / path length over ``window`` bars: ~1 trend, ~0 chop (causal)."""
    c = close.astype("float64")
    path = c.diff().abs().rolling(window, min_periods=window).sum()
    return (c - c.shift(window)).abs() / path.where(path > 0)


def finish(pos: np.ndarray, index: pd.Index, warmup: int) -> pd.Series:
    """Zero the warm-up bars and return a float Series in {-1, 0, 1}."""
    pos = pos.astype("float64")
    pos[: min(warmup, len(pos))] = 0.0
    return pd.Series(pos, index=index, dtype="float64")
