"""Live (real-money) venue: LOCKED, and not implemented in this build.

Every method raises ``LiveLocked`` unless the venue was given an
``UnlockToken`` that ``live_unlock.verify`` issued in this process. Even
then every method raises ``LiveNotImplemented``: real-money order placement
is not built here. It needs a separate, reviewed task and must run
off-sandbox (mainnet is geo-blocked from the development sandbox).

This module has no network code, no request signing and no exchange URL.
"""

from __future__ import annotations

from datetime import datetime
from typing import NoReturn

import pandas as pd

from agenkit_trader import live_unlock
from agenkit_trader.execution.venue import (
    AccountState,
    Fill,
    OpenOrder,
    Order,
    OrderStatus,
    SymbolRules,
    Venue,
)

NOT_IMPLEMENTED = "live order placement is not implemented in this build"


class LiveError(RuntimeError):
    """Base class: the live venue refused a call."""


class LiveLocked(LiveError):
    """No valid unlock token: live mode is locked."""


class LiveNotImplemented(LiveError):
    """Unlocked, but live execution does not exist in this build."""


class LiveVenue:
    """Implements the ``Venue`` protocol by refusing every call."""

    def __init__(self, token: live_unlock.UnlockToken | None = None) -> None:
        self._token = token

    def __repr__(self) -> str:
        return f"LiveVenue(unlocked={live_unlock.is_issued(self._token)})"

    def _refuse(self, method: str) -> NoReturn:
        if not live_unlock.is_issued(self._token):
            raise LiveLocked(
                f"LiveVenue.{method}: live mode is locked (no unlock from "
                "`agenkit live`)"
            )
        raise LiveNotImplemented(NOT_IMPLEMENTED)

    def server_time(self) -> datetime:
        self._refuse("server_time")

    def get_account(self) -> AccountState:
        self._refuse("get_account")

    def get_bars(self, symbol: str, interval: str, limit: int) -> pd.DataFrame:
        self._refuse("get_bars")

    def symbol_rules(self, symbol: str) -> SymbolRules:
        self._refuse("symbol_rules")

    def get_mark_price(self, symbol: str) -> float:
        self._refuse("get_mark_price")

    def get_funding(self, symbol: str, limit: int) -> pd.Series:
        self._refuse("get_funding")

    def get_open_orders(self) -> list[OpenOrder]:
        self._refuse("get_open_orders")

    def place_order(self, order: Order) -> Fill:
        self._refuse("place_order")

    def place_stop(self, order: Order) -> str:
        self._refuse("place_stop")

    def get_order(self, symbol: str, client_id: str) -> OrderStatus | None:
        self._refuse("get_order")

    def cancel_stop(self, symbol: str, client_id: str) -> None:
        self._refuse("cancel_stop")

    def cancel_open_orders(self, symbol: str) -> None:
        self._refuse("cancel_open_orders")

    def flatten_all(self) -> None:
        self._refuse("flatten_all")


def _conforms(v: LiveVenue) -> Venue:  # static check: LiveVenue is a Venue
    return v
