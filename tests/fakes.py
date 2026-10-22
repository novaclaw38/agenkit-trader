"""Scriptable fake venue implementing the Venue protocol."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pandas as pd

from agenkit_trader.execution.venue import (
    AccountState,
    Fill,
    Order,
    Position,
    SymbolRules,
)

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


class VenueError(RuntimeError):
    pass


@dataclass
class FakeVenue:
    now: datetime = T0
    equity: float = 1000.0
    day_start_equity: float = 1000.0
    peak_equity: float = 1000.0
    last_bar_time: datetime = T0
    positions: dict[str, Position] = field(default_factory=dict)
    rules: dict[str, SymbolRules] = field(
        default_factory=lambda: {
            "BTCUSDT": SymbolRules(tick=0.1, lot=0.001, min_notional=5.0)
        }
    )
    bars: dict[str, pd.DataFrame] = field(default_factory=dict)
    # Error injection: number of upcoming calls that raise, and whether
    # flatten_all should raise.
    fail_next: int = 0
    fail_flatten: bool = False
    flatten_calls: int = 0
    orders: list[Order] = field(default_factory=list)

    def _maybe_fail(self) -> None:
        if self.fail_next > 0:
            self.fail_next -= 1
            raise VenueError("injected error")

    def server_time(self) -> datetime:
        self._maybe_fail()
        return self.now

    def get_account(self) -> AccountState:
        self._maybe_fail()
        return AccountState(
            equity=self.equity,
            positions=dict(self.positions),
            day_start_equity=self.day_start_equity,
            peak_equity=self.peak_equity,
            last_bar_time=self.last_bar_time,
        )

    def get_bars(self, symbol: str, interval: str, limit: int) -> pd.DataFrame:
        self._maybe_fail()
        return self.bars.get(symbol, pd.DataFrame()).tail(limit)

    def symbol_rules(self, symbol: str) -> SymbolRules:
        self._maybe_fail()
        return self.rules[symbol]

    def place_order(self, order: Order) -> Fill:
        self._maybe_fail()
        self.orders.append(order)
        signed = order.qty if order.side == "buy" else -order.qty
        old = self.positions.get(order.symbol)
        new_qty = (old.qty if old else 0.0) + signed
        if abs(new_qty) < 1e-12:
            self.positions.pop(order.symbol, None)
        else:
            self.positions[order.symbol] = Position(
                order.symbol, new_qty, order.price_ref
            )
        return Fill(
            symbol=order.symbol,
            side=order.side,
            qty=order.qty,
            price=order.price_ref,
            fee=0.0,
            time=self.now,
        )

    def flatten_all(self) -> None:
        self.flatten_calls += 1
        if self.fail_flatten:
            raise VenueError("injected flatten error")
        self.positions.clear()
