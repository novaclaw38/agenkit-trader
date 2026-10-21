"""Lookahead guard: a signal at bar t must not depend on bars after t."""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pandas as pd


class LookaheadError(Exception):
    """A signal's value at some bar changes when future bars are removed."""


def assert_no_lookahead(
    signal_fn: Callable[[pd.DataFrame], pd.Series],
    bars: pd.DataFrame,
    sample: int = 50,
) -> None:
    """Raise ``LookaheadError`` if ``signal_fn`` peeks at the future.

    For ``sample`` bars t (evenly spaced, always including the first and the
    last-but-one; all bars if fewer), compares ``signal_fn(bars[:t+1])[-1]``
    with ``signal_fn(bars)[t]``. NaN equals NaN; numbers compare with
    ``np.isclose``.
    """
    full = signal_fn(bars)
    if len(full) != len(bars):
        raise LookaheadError(f"signal length {len(full)} != bars length {len(bars)}")
    n = len(bars)
    if n == 0:
        return
    k = min(max(sample, 1), n)
    positions = sorted({int(p) for p in np.linspace(0, n - 1, k)})
    for t in positions:
        part = signal_fn(bars.iloc[: t + 1])
        if len(part) != t + 1:
            raise LookaheadError(f"signal on bars[:{t + 1}] has length {len(part)}")
        a, b = full.iloc[t], part.iloc[-1]
        both_nan = pd.isna(a) and pd.isna(b)
        if not both_nan and not (
            not pd.isna(a) and not pd.isna(b) and np.isclose(a, b, equal_nan=False)
        ):
            raise LookaheadError(
                f"lookahead at {bars.index[t]}: full-history value {a!r} != "
                f"truncated-history value {b!r}"
            )
