"""Trend/range regime labeller shared by research and the runtime.

Kept outside ``validation/`` so the runtime can use the exact labeller the
validation gate used (spec: ``runtime/`` imports no ``validation/``
internals). ``validation.gate`` re-exports both names.
"""

from __future__ import annotations

import pandas as pd

REGIMES = ("range", "trend")  # label 0.0, 1.0


def label_regimes(bars: pd.DataFrame, window: int) -> pd.Series:
    """1.0 = trend, 0.0 = range, NaN = warm-up; causal.

    Kaufman's efficiency ratio over ``window`` bars; a bar is *trend* when its
    ER is above the expanding median of all ER values up to and including it.
    """
    c = bars["close"].astype("float64")
    path = c.diff().abs().rolling(window).sum()
    er = (c - c.shift(window)).abs() / path.where(path > 0)
    med = er.expanding(min_periods=window).median()
    lab = (er > med).astype("float64")
    return lab.where(er.notna() & med.notna())
