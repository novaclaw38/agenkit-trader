"""Pre-trade risk gate. Every order passes through ``RiskGate.check``.

The decision is a pure function of (cfg, rules, order, state, now). It fails
closed: any malformed, missing or non-finite input yields a rejection with a
stable reason string, never an exception and never an implicit allow.

Orders that only reduce an existing position (same symbol, opposite side,
size not exceeding the position) must still pass the sanity, kill-switch and
stale-data checks, but are exempt from the risk-per-trade, daily-loss,
drawdown and leverage limits so a flatten can never be blocked by them.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from agenkit_trader.config import RiskConfig
from agenkit_trader.execution.venue import AccountState, Order, Position, SymbolRules
from agenkit_trader.log import DecisionLog
from agenkit_trader.risk.sizing import floor_to_lot, is_lot_multiple


class Reason(StrEnum):
    OK = "ok"
    INVALID_ORDER = "invalid_order"
    INVALID_STATE = "invalid_state"
    NO_STOP = "no_stop"
    STOP_WRONG_SIDE = "stop_wrong_side"
    HALTED = "halted"
    STALE_DATA = "stale_data"
    RULES_UNAVAILABLE = "rules_unavailable"
    BELOW_LOT = "below_lot"
    QTY_NOT_LOT_MULTIPLE = "qty_not_lot_multiple"
    BELOW_MIN_NOTIONAL = "below_min_notional"
    RISK_PER_TRADE = "risk_per_trade"
    DAILY_LOSS = "daily_loss"
    DRAWDOWN = "drawdown"
    LEVERAGE = "leverage"
    SYMBOL_NOTIONAL = "symbol_notional"
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    max_qty: float


RulesProvider = Callable[[str], SymbolRules]

# Relative tolerance for float comparisons against limits; always in the
# conservative direction is not possible for equality, so it is tiny.
_EPS = 1e-12


def _num(x: object) -> bool:
    return isinstance(x, int | float) and not isinstance(x, bool) and math.isfinite(x)


def _pos(x: object) -> bool:
    return _num(x) and x > 0  # type: ignore[operator]


def _aware(t: object) -> bool:
    return isinstance(t, datetime) and t.utcoffset() is not None


def _reject(reason: Reason, max_qty: float = 0.0) -> Decision:
    return Decision(False, reason.value, max_qty)


def _valid_order(o: object) -> Reason | None:
    if not isinstance(o, Order):
        return Reason.INVALID_ORDER
    if not isinstance(o.symbol, str) or not o.symbol:
        return Reason.INVALID_ORDER
    if o.side not in ("buy", "sell") or not isinstance(o.side, str):
        return Reason.INVALID_ORDER
    if not _pos(o.qty) or not _pos(o.price_ref):
        return Reason.INVALID_ORDER
    if o.stop_price is not None and not _pos(o.stop_price):
        return Reason.INVALID_ORDER
    return None


def _stop_rule(o: Order) -> Reason | None:
    """Stop requirements; applied only to opening/increasing orders."""
    if o.stop_price is None:
        return Reason.NO_STOP
    if o.side == "buy" and not o.stop_price < o.price_ref:
        return Reason.STOP_WRONG_SIDE
    if o.side == "sell" and not o.stop_price > o.price_ref:
        return Reason.STOP_WRONG_SIDE
    return None


def _valid_state(s: object, now: object) -> bool:
    if not isinstance(s, AccountState) or not _aware(now):
        return False
    if not (_pos(s.equity) and _pos(s.day_start_equity) and _pos(s.peak_equity)):
        return False
    if not isinstance(s.halted, bool) or not _aware(s.last_bar_time):
        return False
    if s.last_bar_time > now:  # type: ignore[operator]
        return False
    if not isinstance(s.positions, dict):
        return False
    for sym, p in s.positions.items():
        if not isinstance(p, Position) or p.symbol != sym:
            return False
        if not _num(p.qty) or not _pos(p.entry_price):
            return False
    return True


def _valid_rules(r: object) -> bool:
    return (
        isinstance(r, SymbolRules)
        and _pos(r.lot)
        and _pos(r.tick)
        and _num(r.min_notional)
        and r.min_notional >= 0
    )


class RiskGate:
    def __init__(
        self,
        cfg: RiskConfig,
        rules_provider: RulesProvider,
        log: DecisionLog | None = None,
    ) -> None:
        self.cfg = cfg
        self.rules_provider = rules_provider
        self.log = log

    def check(self, order: Order, state: AccountState, now: datetime) -> Decision:
        try:
            decision = self._decide(order, state, now)
        except Exception:  # fail closed on anything unexpected
            decision = _reject(Reason.INTERNAL_ERROR)
        if self.log is not None:
            self.log.write(
                "risk_gate",
                inputs={
                    "order": _safe_asdict(order),
                    "state": _safe_asdict(state),
                    "now": now,
                },
                output=asdict(decision),
                result="allowed" if decision.allowed else "rejected",
            )
        return decision

    def _decide(self, order: Order, state: AccountState, now: datetime) -> Decision:
        cfg = self.cfg
        bad = _valid_order(order)
        if bad is not None:
            return _reject(bad)
        if not _valid_state(state, now):
            return _reject(Reason.INVALID_STATE)
        if state.halted:
            return _reject(Reason.HALTED)
        age = (now - state.last_bar_time).total_seconds()
        if not age <= cfg.stale_after_s:
            return _reject(Reason.STALE_DATA)
        try:
            rules = self.rules_provider(order.symbol)
        except Exception:
            return _reject(Reason.RULES_UNAVAILABLE)
        if not _valid_rules(rules):
            return _reject(Reason.RULES_UNAVAILABLE)

        if floor_to_lot(order.qty, rules.lot) <= 0:
            return _reject(Reason.BELOW_LOT)
        if not is_lot_multiple(order.qty, rules.lot):
            return _reject(Reason.QTY_NOT_LOT_MULTIPLE)
        # All checks run on the exact submitted quantity.
        qty = order.qty
        price = order.price_ref
        if qty * price < rules.min_notional:
            return _reject(Reason.BELOW_MIN_NOTIONAL)

        sign = 1.0 if order.side == "buy" else -1.0
        signed = sign * qty
        pos = state.positions.get(order.symbol)
        cur = pos.qty if pos is not None else 0.0
        reducing = cur * signed < 0 and qty <= abs(cur)
        if reducing:
            return Decision(True, Reason.OK.value, qty)
        stop_bad = _stop_rule(order)
        if stop_bad is not None:
            return _reject(stop_bad)
        assert order.stop_price is not None

        equity = state.equity
        risk_budget = equity * cfg.max_risk_per_trade
        dist = abs(price - order.stop_price)
        # Cap from risk-per-trade.
        cap_risk = risk_budget / dist
        # Cap from total leverage and per-symbol notional, in the order's
        # direction, using entry prices for other symbols.
        other = sum(
            abs(p.qty) * p.entry_price
            for s, p in state.positions.items()
            if s != order.symbol
        )
        lev_room = (cfg.max_leverage * equity - other) / price
        sym_room = cfg.max_symbol_notional_frac * equity / price
        room = min(lev_room, sym_room)
        cap_dir = max(0.0, room - sign * cur) if room > 0 else 0.0
        cap_qty = min(cap_risk, cap_dir, qty)
        max_qty = floor_to_lot(cap_qty, rules.lot) if cap_qty > 0 else 0.0

        if (state.day_start_equity - equity) / state.day_start_equity >= (
            cfg.max_daily_loss
        ):
            return _reject(Reason.DAILY_LOSS)
        if (state.peak_equity - equity) / state.peak_equity >= cfg.max_drawdown:
            return _reject(Reason.DRAWDOWN)
        if dist * qty > risk_budget * (1 + _EPS):
            return _reject(Reason.RISK_PER_TRADE, floor_to_lot(cap_risk, rules.lot))
        new_sym_notional = abs(cur + signed) * price
        if (other + new_sym_notional) / equity > cfg.max_leverage * (1 + _EPS):
            return _reject(Reason.LEVERAGE, max_qty)
        if new_sym_notional / equity > cfg.max_symbol_notional_frac * (1 + _EPS):
            return _reject(Reason.SYMBOL_NOTIONAL, max_qty)
        return Decision(True, Reason.OK.value, qty)


def _safe_asdict(x: object) -> Any:
    try:
        return asdict(x)  # type: ignore[call-overload]
    except Exception:
        return repr(x)
