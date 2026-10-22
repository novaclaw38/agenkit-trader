"""Scriptable fake venue implementing the Venue protocol."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pandas as pd

from agenkit_trader.execution.venue import (
    AccountState,
    Fill,
    OpenOrder,
    Order,
    OrderStatus,
    Position,
    SymbolRules,
    VenueError,
)

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


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
    # Exchange-side protective stops (reduce-only STOP_MARKET) currently open.
    stops: list[Order] = field(default_factory=list)
    # Every stop ever placed (stays after cancel), for assertions.
    stops_placed: list[Order] = field(default_factory=list)
    cancel_calls: list[str] = field(default_factory=list)
    marks: dict[str, float] = field(default_factory=dict)
    funding: dict[str, pd.Series] = field(default_factory=dict)
    supports_funding: bool = True
    account_halted: bool = False
    # Fraction of each market order's qty that fills (partial-fill injection).
    fill_fraction: float = 1.0
    order_status: dict[str, OrderStatus] = field(default_factory=dict)

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
            halted=self.account_halted,
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
        qty = (
            order.qty
            if self.fill_fraction == 1.0
            else round(order.qty * self.fill_fraction, 9)
        )
        signed = qty if order.side == "buy" else -qty
        old = self.positions.get(order.symbol)
        new_qty = (old.qty if old else 0.0) + signed
        if abs(new_qty) < 1e-12:
            self.positions.pop(order.symbol, None)
        else:
            self.positions[order.symbol] = Position(
                order.symbol, new_qty, order.price_ref
            )
        if order.client_id:
            self.order_status[order.client_id] = OrderStatus(
                order.client_id, "FILLED", qty, order.price_ref
            )
        return Fill(
            symbol=order.symbol,
            side=order.side,
            qty=qty,
            price=order.price_ref,
            fee=0.0,
            time=self.now,
        )

    def get_mark_price(self, symbol: str) -> float:
        self._maybe_fail()
        if symbol in self.marks:
            return self.marks[symbol]
        pos = self.positions.get(symbol)
        if pos is not None:
            return pos.entry_price
        df = self.bars.get(symbol)
        if df is not None and len(df):
            return float(df["close"].iloc[-1])
        raise VenueError(f"no mark for {symbol}")

    def get_funding(self, symbol: str, limit: int) -> pd.Series:
        self._maybe_fail()
        if not self.supports_funding:
            raise NotImplementedError("funding not available on this venue")
        return self.funding.get(symbol, pd.Series(dtype="float64")).tail(limit)

    def get_open_orders(self) -> list[OpenOrder]:
        self._maybe_fail()
        return [
            OpenOrder(
                symbol=o.symbol,
                side=o.side,
                qty=o.qty,
                kind="STOP_MARKET",
                stop_price=o.stop_price,
                client_id=o.client_id,
                reduce_only=True,
            )
            for o in self.stops
        ]

    def place_stop(self, order: Order) -> str:
        self._maybe_fail()
        self.stops.append(order)
        self.stops_placed.append(order)
        return order.client_id

    def get_order(self, symbol: str, client_id: str) -> OrderStatus | None:
        self._maybe_fail()
        return self.order_status.get(client_id)

    def cancel_stop(self, symbol: str, client_id: str) -> None:
        self._maybe_fail()
        self.cancel_calls.append(f"{symbol}:{client_id}")
        self.stops = [
            o
            for o in self.stops
            if not (o.symbol == symbol and o.client_id == client_id)
        ]

    def cancel_open_orders(self, symbol: str) -> None:
        self._maybe_fail()
        self.cancel_calls.append(symbol)
        self.stops = [o for o in self.stops if o.symbol != symbol]

    def flatten_all(self) -> None:
        self.flatten_calls += 1
        if self.fail_flatten:
            raise VenueError("injected flatten error")
        self.positions.clear()
        self.stops.clear()
