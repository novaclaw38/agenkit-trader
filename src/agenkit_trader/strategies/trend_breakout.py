"""Channel breakout with a volatility filter (spec section 4, candidate 2)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from agenkit_trader.strategies._util import finish, realised_vol

VOL_SHORT = 24  # bars, recent realised vol
VOL_LONG = 720  # bars (~30 days), reference realised vol


class TrendBreakout:
    """Follow breakouts of the prior ``lookback``-bar range, unless vol is hot.

    Edge: crypto trends persist over days-to-weeks because information and
    flows diffuse slowly, so a close beyond the prior range tends to continue.
    Other side: range/mean-reversion sellers and late hedgers who fade or
    resist the move.
    Entry: at a bar close above the prior ``lookback``-bar high go long (below
    the prior low go short), only if 24-bar realised vol up to the prior bar
    is at most ``vol_cap`` x its 720-bar level (avoid chasing blow-off spikes).
    Exit: flat when the close crosses the opposite ``lookback``/2-bar channel
    (a trailing exit), or on an opposite breakout which flips the position.
    Stop: the trailing ``lookback``/2 channel is the stop, evaluated at bar
    close only (intrabar stops are not simulated).
    Holding period: days to weeks.
    Expected frequency: a few trades per month per symbol.
    Why it should persist: behavioural under-reaction and slow institutional
    allocation are structural, and trend following is capacity-limited by
    drawdown tolerance rather than by arbitrage.
    """

    name = "trend_breakout"
    edge = "slow trend persistence: closes beyond the prior range tend to continue"
    param_grid: dict[str, list[Any]] = {
        "lookback": [48, 96, 168, 336],
        "vol_cap": [1.0, 1.5, 2.0],
    }

    def warmup(self, params: dict[str, Any]) -> int:
        return max(int(params["lookback"]), VOL_LONG + 2)

    def signal(
        self, bars: pd.DataFrame, params: dict[str, Any], aux: dict[str, pd.Series]
    ) -> pd.Series:
        n_look = int(params["lookback"])
        n_exit = max(n_look // 2, 1)
        cap = float(params["vol_cap"])
        close = bars["close"].astype("float64")
        hi = bars["high"].astype("float64").rolling(n_look).max().shift(1)
        lo = bars["low"].astype("float64").rolling(n_look).min().shift(1)
        ex_hi = bars["high"].astype("float64").rolling(n_exit).max().shift(1)
        ex_lo = bars["low"].astype("float64").rolling(n_exit).min().shift(1)
        # vol measured up to the previous bar: the breakout bar itself is excluded
        rv_s = realised_vol(close, VOL_SHORT).shift(1)
        calm = rv_s <= cap * realised_vol(close, VOL_LONG).shift(1)
        c = close.to_numpy()
        h, lw, eh, el = (x.to_numpy() for x in (hi, lo, ex_hi, ex_lo))
        ok = calm.to_numpy()
        pos = np.zeros(len(c))
        cur = 0.0
        for t in range(len(c)):
            if np.isnan(h[t]) or np.isnan(eh[t]):
                cur = 0.0
                continue
            if cur > 0 and c[t] < el[t] or cur < 0 and c[t] > eh[t]:
                cur = 0.0
            if ok[t]:
                if c[t] > h[t]:
                    cur = 1.0
                elif c[t] < lw[t]:
                    cur = -1.0
            pos[t] = cur
        return finish(pos, bars.index, self.warmup(params))
