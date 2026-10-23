"""The runtime computes each signal on its rolling window of ``bars_limit``
closed bars; research computes it on the whole history. Do they agree at the
window's last bar? (Random-walk data, every grid point.)

KNOWN MISMATCH (reported, not hidden): trend_breakout keeps its position as
state from its first defined bar onwards. It needs 722 warm-up bars, so in
the runtime's 1000-bar window it starts flat 278 bars before the decision
bar, while the full history may still hold a breakout position entered
earlier. With lookback 336 / vol_cap 1.0 this changes the final-bar signal
on a few percent of end points (seeds 0 and 1 below). The other strategies
agree exactly.
"""

from __future__ import annotations

import itertools
from typing import Any

import numpy as np
import pandas as pd
import pytest

from agenkit_trader.config import RuntimeConfig
from agenkit_trader.strategies import REGISTRY

W = RuntimeConfig().bars_limit


def _market(seed: int, n: int = 2600) -> tuple[pd.DataFrame, dict[str, pd.Series]]:
    rng = np.random.default_rng(seed)
    c = 100 * np.cumprod(1 + rng.normal(0, 0.01, n))
    h = c * (1 + np.abs(rng.normal(0, 0.004, n)))
    lo = c * (1 - np.abs(rng.normal(0, 0.004, n)))
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    bars = pd.DataFrame(
        {"open": c, "high": h, "low": lo, "close": c, "volume": 1.0}, index=idx
    )
    return bars, {"funding": pd.Series(rng.normal(0, 0.0004, n), index=idx)}


def _mismatches(name: str, seed: int) -> list[tuple[dict[str, Any], int]]:
    s = REGISTRY[name]
    bars, aux = _market(seed)
    out = []
    for vals in itertools.product(*s.param_grid.values()):
        p = dict(zip(s.param_grid, vals, strict=True))
        full = s.signal(bars, p, aux)
        for t in range(W + 25, len(bars), 61):
            w = bars.iloc[t + 1 - W : t + 1]
            part = s.signal(w, p, {k: v.reindex(w.index) for k, v in aux.items()})
            if float(part.iloc[-1]) != float(full.iloc[t]):
                out.append((p, t))
    return out


@pytest.mark.parametrize("name", ["funding_reversion", "short_reversion"])
@pytest.mark.parametrize("seed", [0, 1])
def test_window_signal_equals_full_history_signal(name: str, seed: int) -> None:
    assert _mismatches(name, seed) == []


def test_trend_breakout_window_mismatch_is_known_and_bounded() -> None:
    found = _mismatches("trend_breakout", 0) + _mismatches("trend_breakout", 1)
    # documented divergence: only the longest lookback, at few end points
    assert found, "the documented mismatch disappeared: update the docs"
    assert {p["lookback"] for p, _ in found} == {336}
