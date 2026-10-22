"""Venue interface and the plain data types exchanged with it.

These types carry no validation on purpose: the risk gate is the single
place that decides whether their contents are usable, and it fails closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import pandas as pd

# Largest tolerated |server time - local time| before the runtime halts.
MAX_CLOCK_SKEW_MS = 1000


class VenueError(RuntimeError):
    """A venue call failed. Messages never contain credentials."""


class ClockSkewError(VenueError):
    """Local clock differs from the venue's by more than MAX_CLOCK_SKEW_MS."""


class OrderNotFilledError(VenueError):
    """The venue DEFINITELY did not fill the order (terminal status, nothing
    executed). Any other failure of ``place_order`` means "unknown"."""


class OrderStatusUnknownError(VenueError):
    """The order was accepted but its outcome is not known yet (e.g. status
    NEW with nothing executed); it may still fill."""


@dataclass(frozen=True)
class Position:
    symbol: str
    qty: float  # signed: positive long, negative short
    entry_price: float


@dataclass(frozen=True)
class SymbolRules:
    tick: float
    lot: float
    min_notional: float


@dataclass(frozen=True)
class Order:
    symbol: str
    side: str  # "buy" | "sell"
    qty: float
    price_ref: float
    stop_price: float | None
    strategy: str
    # Deterministic client order id (strategy/symbol/bar time/kind) so a
    # resubmission of the same decision is recognisable; "" = venue assigns.
    client_id: str = ""
    # Exchange-enforced: the order may only reduce an existing position.
    reduce_only: bool = False


@dataclass(frozen=True)
class OrderStatus:
    client_id: str
    status: str  # venue status, e.g. "NEW", "FILLED", "EXPIRED"
    executed_qty: float
    avg_price: float


@dataclass(frozen=True)
class OpenOrder:
    symbol: str
    side: str  # "buy" | "sell"
    qty: float
    kind: str  # exchange order type, e.g. "STOP_MARKET"
    stop_price: float | None
    client_id: str
    reduce_only: bool


@dataclass(frozen=True)
class Fill:
    symbol: str
    side: str
    qty: float
    price: float
    fee: float
    time: datetime


@dataclass(frozen=True)
class AccountState:
    equity: float
    positions: dict[str, Position]
    day_start_equity: float
    peak_equity: float
    last_bar_time: datetime
    halted: bool = False


class Venue(Protocol):
    def server_time(self) -> datetime: ...

    def get_account(self) -> AccountState: ...

    def get_bars(self, symbol: str, interval: str, limit: int) -> pd.DataFrame: ...

    def symbol_rules(self, symbol: str) -> SymbolRules: ...

    def get_mark_price(self, symbol: str) -> float: ...

    def get_funding(self, symbol: str, limit: int) -> pd.Series:
        """Funding rates stamped at funding time (UTC index). Raises
        NotImplementedError if the venue cannot supply them."""
        ...

    def get_open_orders(self) -> list[OpenOrder]: ...

    def place_order(self, order: Order) -> Fill:
        """Market order for exactly ``order.qty``."""
        ...

    def place_stop(self, order: Order) -> str:
        """Exchange-side reduce-only STOP_MARKET at ``order.stop_price`` for
        ``order.qty``; returns the venue's id for it."""
        ...

    def get_order(self, symbol: str, client_id: str) -> OrderStatus | None:
        """Look an order up by its client id; None if the venue has none."""
        ...

    def cancel_stop(self, symbol: str, client_id: str) -> None:
        """Cancel one protective stop by its client id."""
        ...

    def cancel_open_orders(self, symbol: str) -> None: ...

    def flatten_all(self) -> None:
        """Cancel every open order and close every position. Kill-switch
        path: never routed through the RiskGate."""
        ...
