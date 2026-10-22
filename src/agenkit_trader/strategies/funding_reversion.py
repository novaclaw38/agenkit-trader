"""Funding-rate extreme mean-reversion (spec section 4, candidate 1)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from agenkit_trader.strategies._util import finish

# Funding settlements are stamped at the funding time; the settled rate is
# published at settlement. The research script aligns them with
# ``align_asof(bars, funding, FUNDING_AVAILABLE_LAG)``: a rate stamped 08:00
# is first usable by the bar that closes after 08:01. One minute covers the
# archive's documented stamp jitter (data.archive.FUNDING_JITTER).
FUNDING_AVAILABLE_LAG = pd.Timedelta(minutes=1)


class FundingReversion:
    """Fade crowded perpetual positioning when the funding rate is extreme.

    Edge: when funding is far above (below) its normal level, longs (shorts) are
    crowded and leveraged, so price tends to mean-revert as they are squeezed
    or unwind, and the faded side collects funding meanwhile.
    Other side: over-leveraged momentum traders paying extreme funding to hold.
    Entry: at the close of a bar whose latest known funding rate is above
    ``entry`` go short; below ``-entry`` go long.
    Exit: flat once the latest known funding is back inside +/- ``entry``/2, or
    after ``max_hold`` bars, whichever comes first.
    Stop: flat at the close of any bar whose close is more than ``stop_pct``
    adverse to the entry close (bar-close stop; intrabar stops are not
    simulated, so real losses can exceed it); no re-entry until funding
    normalises.
    Holding period: hours to ``max_hold`` bars (1-3 days on 1h bars).
    Expected frequency: a handful of episodes per month in heated markets, near
    zero in quiet ones (funding extremes cluster).
    Why it should persist: retail leverage demand is pro-cyclical and funding
    caps arbitrage capital, so crowding recurs each cycle.
    """

    name = "funding_reversion"
    edge = "extreme funding marks crowded leveraged positioning that tends to unwind"
    param_grid: dict[str, list[Any]] = {
        "entry": [0.0003, 0.0005, 0.001],
        "max_hold": [24, 48, 72],
    }
    stop_pct = 0.05  # fixed, not optimised

    def warmup(self, params: dict[str, Any]) -> int:
        return 1

    def signal(
        self, bars: pd.DataFrame, params: dict[str, Any], aux: dict[str, pd.Series]
    ) -> pd.Series:
        n = len(bars)
        if "funding" not in aux:
            return finish(np.zeros(n), bars.index, 0)
        f = aux["funding"].reindex(bars.index).to_numpy(dtype="float64")
        close = bars["close"].to_numpy(dtype="float64")
        entry = float(params["entry"])
        max_hold = int(params["max_hold"])
        pos = np.zeros(n)
        cur, held, px, blocked = 0.0, 0, 0.0, False
        for t in range(n):
            ft = f[t]
            if np.isnan(ft):
                cur, held, blocked = 0.0, 0, False
                continue
            if cur != 0.0:
                held += 1
                adverse = cur * (close[t] / px - 1.0) < -self.stop_pct
                normal = abs(ft) < entry / 2
                if adverse or held >= max_hold:
                    cur, blocked = 0.0, True
                elif normal:
                    cur = 0.0
            if blocked and abs(ft) < entry / 2:
                blocked = False
            if cur == 0.0 and not blocked:
                if ft > entry:
                    cur, held, px = -1.0, 0, close[t]
                elif ft < -entry:
                    cur, held, px = 1.0, 0, close[t]
            pos[t] = cur
        return finish(pos, bars.index, self.warmup(params))
