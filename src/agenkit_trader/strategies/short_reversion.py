"""Short-horizon overshoot reversion with a regime filter (spec section 4, #3)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from agenkit_trader.strategies._util import efficiency_ratio, finish

MOVE = 6  # bars: the overshoot is a 6-bar return
Z_WINDOW = 168  # bars (~1 week) to scale that return
ER_WINDOW = 48  # bars for the regime (efficiency ratio) filter
MEAN = 24  # bars: exit when price is back to its 24-bar mean
MAX_HOLD = 24  # bars
STOP_Z = 1.5  # extra z beyond entry that triggers the bar-close stop


class ShortReversion:
    """Fade sharp short-horizon moves when the market is ranging, not trending.

    Edge: liquidity-driven overshoots (liquidation cascades, large market
    orders into thin books) partly retrace once liquidity providers return.
    Other side: forced sellers/buyers (liquidations, stop-outs) who demand
    immediacy regardless of price.
    Entry: at a bar close where the 6-bar return is below ``-z_entry`` (above
    ``+z_entry``) weekly standard deviations go long (short), only if the
    48-bar efficiency ratio, measured before the 6-bar move, is below
    ``er_max`` (range regime).
    Exit: flat when the close crosses back through its 24-bar mean, or after
    24 bars.
    Stop: flat at the close of a bar whose 6-bar z-score extends 1.5 beyond
    the entry threshold against the position (bar-close stop; intrabar stops
    are not simulated).
    Holding period: a few hours up to one day.
    Expected frequency: a few trades per month per symbol.
    Why it should persist: market makers are paid for absorbing forced flow;
    leverage-driven liquidations in crypto recur.
    """

    name = "short_reversion"
    edge = "liquidity-driven overshoots partly retrace in range-bound markets"
    param_grid: dict[str, list[Any]] = {
        "z_entry": [2.0, 2.5, 3.0],
        "er_max": [0.2, 0.3, 0.4],
    }

    def warmup(self, params: dict[str, Any]) -> int:
        return max(MOVE + Z_WINDOW, ER_WINDOW + MOVE)

    def signal(
        self, bars: pd.DataFrame, params: dict[str, Any], aux: dict[str, pd.Series]
    ) -> pd.Series:
        k = float(params["z_entry"])
        er_max = float(params["er_max"])
        close = bars["close"].astype("float64")
        mv = pd.Series(np.log(close.to_numpy()), index=close.index).diff(MOVE)
        sd = mv.rolling(Z_WINDOW, min_periods=Z_WINDOW).std()
        z = (mv / sd.where(sd > 0)).to_numpy()
        # regime judged before the overshoot window, so the move itself cannot
        # make the market look like a trend
        er = efficiency_ratio(close, ER_WINDOW).shift(MOVE).to_numpy()
        mean = close.rolling(MEAN, min_periods=MEAN).mean().to_numpy()
        c = close.to_numpy()
        pos = np.zeros(len(c))
        cur, held = 0.0, 0
        for t in range(len(c)):
            if np.isnan(z[t]) or np.isnan(er[t]) or np.isnan(mean[t]):
                cur, held = 0.0, 0
                continue
            if cur != 0.0:
                held += 1
                back = (c[t] >= mean[t]) if cur > 0 else (c[t] <= mean[t])
                stop = cur * z[t] < -(k + STOP_Z)
                if back or stop or held >= MAX_HOLD:
                    cur = 0.0
            if cur == 0.0 and er[t] < er_max:
                if z[t] < -k and z[t] >= -(k + STOP_Z):
                    cur, held = 1.0, 0
                elif z[t] > k and z[t] <= k + STOP_Z:
                    cur, held = -1.0, 0
            pos[t] = cur
        return finish(pos, bars.index, self.warmup(params))
