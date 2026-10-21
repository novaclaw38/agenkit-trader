"""Point-in-time alignment of slower/irregular series onto bars."""

from __future__ import annotations

import pandas as pd

from agenkit_trader.data.archive import DataGapError, _check_index


def _bar_length(index: pd.DatetimeIndex) -> pd.Timedelta:
    if len(index) < 2:
        raise ValueError("need at least 2 bars to infer the bar length")
    return pd.Timedelta((index[1:] - index[:-1]).min())


def align_asof(
    bars: pd.DataFrame, series: pd.Series, available_lag: pd.Timedelta
) -> pd.Series:
    """Align ``series`` to ``bars`` without lookahead.

    ``bars`` is indexed by bar *open* time; a bar is complete (and its features
    usable) at its close, ``open + bar_length - 1ms``. A ``series`` observation
    stamped ``t`` becomes available at ``t + available_lag``. Each bar gets the
    latest observation whose availability time is <= the bar's close
    (backward as-of join); bars before any available observation get NaN.
    With zero lag, a value stamped 08:00 is invisible to the 07:00 bar of a 1h
    series (closes 07:59:59.999) and visible to the 08:00 bar.
    """
    if available_lag < pd.Timedelta(0):
        raise ValueError("available_lag must be >= 0")
    b_idx = _check_index(bars.index, "bars")
    s_idx = _check_index(series.index, "series")
    close = b_idx + _bar_length(b_idx) - pd.Timedelta(milliseconds=1)
    left = pd.DataFrame({"t": close.as_unit("ns"), "pos": range(len(b_idx))})
    right = pd.DataFrame(
        {"t": (s_idx + available_lag).as_unit("ns"), "v": series.to_numpy()}
    )
    merged = pd.merge_asof(left, right, on="t", direction="backward")
    return pd.Series(merged["v"].to_numpy(), index=bars.index, name=series.name)


__all__ = ["DataGapError", "align_asof"]
