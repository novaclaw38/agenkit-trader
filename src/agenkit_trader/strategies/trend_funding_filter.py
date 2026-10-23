"""Trend breakout gated by a funding-rate crowding filter (round 2)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from agenkit_trader.strategies._util import finish
from agenkit_trader.strategies.trend_breakout import TrendBreakout

_TREND = TrendBreakout()


class TrendFundingFilter:
    """``trend_breakout`` entries, skipped when funding says the side is crowded.

    Edge: extreme funding marks crowded positioning, so trend entries INTO the
    crowded side fail more often than entries against low-funding sides.
    Entry: the trend_breakout signal (reused, not copied) decides the side and
    the bar. A NEW entry (flat -> side, or a flip) is suppressed when the
    latest available funding ``f`` is ``>= +thr`` (new long) or ``<= -thr``
    (new short). A suppressed trade is skipped for its whole life: the filter
    is evaluated only at the raw signal's transition bar, never later while
    the raw trend persists (the runtime too enters only on a transition). A
    suppressed flip leaves the old position closed and the book flat.
    Exit: unchanged: whatever the trend signal does (zero or opposite) closes
    the position; funding never forces an exit.
    NaN funding (missing, or outside the runtime's funding window) is treated
    as UNKNOWN and suppresses new entries: fail safe, since the filter's
    purpose is to avoid an unverified crowded side. Open positions are not
    affected. A missing ``aux["funding"]`` behaves as all-NaN (never trades).
    Funding arrives aligned with FUNDING_AVAILABLE_LAG (see funding_reversion).
    """

    name = "trend_funding_filter"
    edge = (
        "trend entries into a funding-crowded side fail more often; "
        "skipping them improves trend_breakout"
    )
    param_grid: dict[str, list[Any]] = {
        "lookback": [42, 84, 168],
        "thr": [0.0003, 0.0006],
        "vol_cap": [1.5],
    }
    aux_required = ("funding",)

    def warmup(self, params: dict[str, Any]) -> int:
        return _TREND.warmup(params)

    def signal(
        self, bars: pd.DataFrame, params: dict[str, Any], aux: dict[str, pd.Series]
    ) -> pd.Series:
        raw = _TREND.signal(
            bars, {"lookback": params["lookback"], "vol_cap": params["vol_cap"]}, {}
        ).to_numpy(dtype="float64")
        n = len(bars)
        if "funding" in aux:
            f = aux["funding"].reindex(bars.index).to_numpy(dtype="float64")
        else:
            f = np.full(n, np.nan)
        thr = float(params["thr"])
        pos = np.zeros(n)
        cur, prev = 0.0, 0.0
        for t in range(n):
            r = raw[t]
            if r == 0.0:
                cur = 0.0
            elif r != prev:  # a new entry or a flip: apply the filter
                ft = f[t]
                crowded = (
                    np.isnan(ft) or (r > 0 and ft >= thr) or (r < 0 and ft <= -thr)
                )
                cur = 0.0 if crowded else r
            pos[t] = cur
            prev = r
        return finish(pos, bars.index, self.warmup(params))
