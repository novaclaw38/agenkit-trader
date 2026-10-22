"""trend_breakout on round 2's frozen grid, under its own registry name.

The runtime refuses a deployment whose params are not a point of the
strategy's REGISTERED grid, and ``trend_breakout``'s grid (round 1) has no
lookback 42/84. This strategy is exactly the trend_breakout signal (reused,
not copied) with the round-2 grid, so a round-2 survivor is deployable
without touching trend_breakout or the runtime.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from agenkit_trader.strategies.trend_breakout import TrendBreakout

_BASE = TrendBreakout()


class TrendBreakoutR2:
    name = "trend_breakout_r2"
    edge = _BASE.edge
    param_grid: dict[str, list[Any]] = {"lookback": [42, 84, 168], "vol_cap": [1.5]}

    def warmup(self, params: dict[str, Any]) -> int:
        return _BASE.warmup(params)

    def signal(
        self, bars: pd.DataFrame, params: dict[str, Any], aux: dict[str, pd.Series]
    ) -> pd.Series:
        return _BASE.signal(bars, params, aux)
