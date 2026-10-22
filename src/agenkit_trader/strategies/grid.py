"""Run a registered strategy on a different parameter grid (research config).

``with_grid`` returns a thin view of the strategy: same name, signal, warm-up
and ``aux_required``, but its own ``param_grid``. The registered strategy and
its default grid are never mutated.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from agenkit_trader.strategies.base import Strategy


class GridOverride:
    def __init__(self, base: Strategy, grid: dict[str, list[Any]]) -> None:
        self._base = base
        self.name = base.name
        self.edge = base.edge
        self.param_grid = grid

    def signal(
        self, bars: pd.DataFrame, params: dict[str, Any], aux: dict[str, pd.Series]
    ) -> pd.Series:
        return self._base.signal(bars, params, aux)

    def __getattr__(self, item: str) -> Any:  # warmup, aux_required, ...
        return getattr(self._base, item)


def with_grid(base: Strategy, grid: dict[str, list[Any]]) -> Strategy:
    """Validate ``grid`` against ``base.param_grid`` and return the view.

    Same parameter names, every list non-empty, sorted and duplicate-free
    (adjacent values are neighbours for the stability sweep)."""
    if set(grid) != set(base.param_grid):
        raise ValueError(
            f"{base.name}: grid keys {sorted(grid)} != parameters "
            f"{sorted(base.param_grid)}"
        )
    for k, vals in grid.items():
        if not isinstance(vals, list) or not vals:
            raise ValueError(f"{base.name}.{k}: need a non-empty list")
        if vals != sorted(set(vals)):
            raise ValueError(f"{base.name}.{k}: values must be sorted and unique")
    ordered = {k: list(grid[k]) for k in base.param_grid}  # registered key order
    return GridOverride(base, ordered)
