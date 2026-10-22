"""The ``Strategy`` protocol every research strategy implements."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

import pandas as pd


@runtime_checkable
class Strategy(Protocol):
    """A parameterised signal generator.

    ``name``: unique identifier. ``edge``: one-line statement of the market
    inefficiency the strategy claims to exploit. ``param_grid``: every value
    to try for each parameter, in sorted order (adjacent entries are treated
    as neighbours by the stability sweep).

    ``signal(bars, params, aux)`` returns a Series indexed like ``bars`` with
    values in ``{-1, 0, 1}``; the value at bar ``t`` is the target position
    decided at the close of bar ``t`` and must use only information available
    at that close. NaN (e.g. indicator warm-up) is treated as flat (0).
    ``aux`` holds extra time series (funding, features) indexed like ``bars``.

    Optional attribute ``aux_required: tuple[str, ...]`` names the ``aux``
    series the strategy needs; the runtime refuses to start a deployed
    strategy whose required aux the venue cannot supply.
    """

    name: str
    edge: str
    param_grid: dict[str, list[Any]]

    def signal(
        self, bars: pd.DataFrame, params: dict[str, Any], aux: dict[str, pd.Series]
    ) -> pd.Series: ...
