"""Point-in-time alignment of slower/irregular series onto bars."""

from __future__ import annotations

import pandas as pd

from agenkit_trader.data.archive import DataGapError, _check_index


def _bar_length(
    index: pd.DatetimeIndex, bar_length: pd.Timedelta | None
) -> pd.Timedelta:
    if bar_length is not None:
        if bar_length <= pd.Timedelta(0):
            raise ValueError("bar_length must be > 0")
        return bar_length
    if len(index) < 2:
        raise ValueError("need at least 2 bars to infer the bar length")
    steps = index[1:] - index[:-1]
    if steps.min() != steps.max():
        raise ValueError(
            "bars are not uniformly spaced; cannot infer bar length "
            "(pass bar_length explicitly)"
        )
    return pd.Timedelta(steps[0])


def align_asof(
    bars: pd.DataFrame,
    series: pd.Series,
    available_lag: pd.Timedelta,
    bar_length: pd.Timedelta | None = None,
) -> pd.Series:
    """Align ``series`` to ``bars`` without lookahead.

    ``bars`` is indexed by bar *open* time; a bar is complete (and its features
    usable) at its close, ``open + bar_length - 1ms``. A ``series`` observation
    stamped ``t`` becomes available at ``t + available_lag``. Each bar gets the
    latest observation whose availability time is <= the bar's close
    (backward as-of join); bars before any available observation get NaN.
    With zero lag, a value stamped 08:00 is invisible to the 07:00 bar of a 1h
    series (closes 07:59:59.999) and visible to the 08:00 bar.

    ``bar_length`` defaults to the (required uniform) spacing of ``bars``; a
    subsampled or gappy frame raises ``ValueError`` unless the true bar length
    is passed explicitly, so future values can never leak via a wrong length.
    """
    if available_lag < pd.Timedelta(0):
        raise ValueError("available_lag must be >= 0")
    b_idx = _check_index(bars.index, "bars")
    s_idx = _check_index(series.index, "series")
    close = b_idx + _bar_length(b_idx, bar_length) - pd.Timedelta(milliseconds=1)
    left = pd.DataFrame({"t": close.as_unit("ns"), "pos": range(len(b_idx))})
    right = pd.DataFrame(
        {"t": (s_idx + available_lag).as_unit("ns"), "v": series.to_numpy()}
    )
    merged = pd.merge_asof(left, right, on="t", direction="backward")
    return pd.Series(merged["v"].to_numpy(), index=bars.index, name=series.name)


__all__ = ["DataGapError", "align_asof"]
