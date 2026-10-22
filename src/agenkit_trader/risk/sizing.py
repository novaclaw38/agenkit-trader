"""Position sizing: fixed-fractional risk, optionally reduced by capped Kelly."""

from __future__ import annotations

import math

from agenkit_trader.config import RiskConfig
from agenkit_trader.execution.venue import SymbolRules


def _pos_finite(x: object) -> bool:
    return (
        isinstance(x, int | float)
        and not isinstance(x, bool)
        and math.isfinite(x)
        and x > 0
    )


def floor_to_lot(qty: float, lot: float) -> float:
    """Round ``qty`` down to a whole number of lots, never up."""
    n = math.floor(qty / lot)
    q = n * lot
    while n > 0 and q > qty:  # guard against float error rounding up
        n -= 1
        q = n * lot
    return q if n > 0 else 0.0


def size_position(
    equity: float,
    entry: float,
    stop: float,
    cfg: RiskConfig,
    kelly_fraction: float | None = None,
    rules: SymbolRules | None = None,
) -> float:
    """Return a quantity whose loss at ``stop`` is at most the risk budget.

    The risk budget is ``equity * min(max_risk_per_trade, k)`` where ``k`` is
    the Kelly fraction clipped to ``cfg.kelly_cap`` (full Kelly is never used).
    Any unusable input returns 0.0 (no position), never an exception.
    """
    if not (_pos_finite(equity) and _pos_finite(entry) and _pos_finite(stop)):
        return 0.0
    dist = abs(entry - stop)
    if not dist > 0:
        return 0.0
    frac = cfg.max_risk_per_trade
    if kelly_fraction is not None:
        if not _pos_finite(kelly_fraction):
            return 0.0
        frac = min(frac, min(kelly_fraction, cfg.kelly_cap))
    budget = equity * frac
    qty = budget / dist
    # Float division can land a hair above the budget; step down if so.
    while qty > 0 and qty * dist > budget:
        qty = math.nextafter(qty, 0.0)
    if rules is not None:
        if not (_pos_finite(rules.lot) and math.isfinite(rules.min_notional)):
            return 0.0
        qty = floor_to_lot(qty, rules.lot)
        if qty * entry < rules.min_notional:
            return 0.0
    if not math.isfinite(qty) or qty <= 0:
        return 0.0
    return qty
