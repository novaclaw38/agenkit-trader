"""Transaction cost model.

All parameters are explicit inputs; nothing here is an exchange constant. Fees
are fractions of notional (``0.0004`` = 4 bps); spread and slippage are in basis
points because that is how they are usually quoted.

The engine always trades as a taker (a bar simulator cannot know whether a
passive order would have been filled), so ``maker_fee`` is carried for
reporting and for later fill-model work but is not used by ``run_backtest``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

_BPS = 1e4


def _check(name: str, value: float) -> None:
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be finite and >= 0, got {value!r}")


@dataclass(frozen=True)
class CostModel:
    """Per-side cost of trading one unit of notional.

    ``per_side_rate = taker_fee + half_spread_bps/1e4 + slippage_bps/1e4``:
    crossing the spread costs half of it per side, slippage is the additional
    adverse price move, and the taker fee is charged on top.
    """

    taker_fee: float
    maker_fee: float
    half_spread_bps: float
    slippage_bps: float

    def __post_init__(self) -> None:
        _check("taker_fee", self.taker_fee)
        _check("maker_fee", self.maker_fee)
        _check("half_spread_bps", self.half_spread_bps)
        _check("slippage_bps", self.slippage_bps)

    @property
    def per_side_rate(self) -> float:
        """Fraction of traded notional lost on one fill (taker)."""
        return self.taker_fee + self.half_spread_bps / _BPS + self.slippage_bps / _BPS

    def scaled(self, factor: float) -> CostModel:
        """Return a model with every component multiplied by ``factor``.

        Used for cost-sensitivity checks (e.g. 2x fees and slippage).
        """
        _check("factor", factor)
        return CostModel(
            self.taker_fee * factor,
            self.maker_fee * factor,
            self.half_spread_bps * factor,
            self.slippage_bps * factor,
        )

    def round_trip_cost(self, notional: float) -> float:
        """Cost of entering and exiting ``notional``: two taker fills."""
        return 2.0 * abs(notional) * self.per_side_rate
