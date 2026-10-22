"""Venue interface and the plain data types exchanged with it.

These types carry no validation on purpose: the risk gate is the single
place that decides whether their contents are usable, and it fails closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import pandas as pd


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

    def place_order(self, order: Order) -> Fill: ...

    def flatten_all(self) -> None: ...
