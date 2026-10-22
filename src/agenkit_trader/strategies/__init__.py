"""Trading strategies (signal generators)."""

from agenkit_trader.strategies.base import Strategy
from agenkit_trader.strategies.funding_reversion import FundingReversion
from agenkit_trader.strategies.short_reversion import ShortReversion
from agenkit_trader.strategies.trend_breakout import TrendBreakout

REGISTRY: dict[str, Strategy] = {
    s.name: s for s in (FundingReversion(), TrendBreakout(), ShortReversion())
}

__all__ = ["REGISTRY", "Strategy"]
