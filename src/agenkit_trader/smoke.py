"""``agenkit smoke``: one-shot, labelled TESTNET plumbing checklist.

Purpose: find out, on the user's own machine with their own testnet keys,
whether the order plumbing works against the real testnet (connectivity,
rules, entry through the RiskGate, the exchange-side stop via the Algo
Service, reconcile, stop coverage, close, kill-switch flatten). It uses no
strategy and no signal, and it reads or writes none of the paper runtime's
files (deployed.json, the paper decision log, the paper state): its own log
(``data/smoke/smoke_log.jsonl``) is separate, every record is tagged
``"mode": "smoke"`` and nothing that counts paper days or trades reads it.

Steps (each recorded pass / fail / skip with a short reason):
1 connectivity, clock skew, exchangeInfo rules == config/symbol_rules.yaml
2 account balance read (and: the account must be flat; smoke never touches
  positions it did not open)
3 MARKET entry at the smallest valid size through the real RiskGate
4 the exchange-side stop appears (reports exactly what the exchange said)
5 reconcile: position and stop as the runtime's reconcile expects
6 stop-coverage check, as the runtime does it
7 cancel the stop, close with a reduce-only order, verify flat
8 KillSwitch flatten path with a second tiny position

A failed step stops the run, attempts a flatten and reports. The run always
ends by verifying the account is flat. Exit code: 0 all passed and flat,
1 a step failed (account verified flat), 130 interrupted (account verified
flat), 4 flat NOT verified or not flat. Once smoke has placed an order, cleanup
and the flat check run whatever ends the run (a hard kill, SIGKILL or power
loss, cannot be handled: check the testnet account by hand).
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pandas as pd

from agenkit_trader.config import RiskConfig
from agenkit_trader.execution.binance_testnet import TESTNET_HOST
from agenkit_trader.execution.venue import (
    AccountState,
    OpenOrder,
    Order,
    SymbolRules,
    Venue,
    VenueError,
)
from agenkit_trader.killswitch import KillSwitch
from agenkit_trader.log import DecisionLog
from agenkit_trader.risk.gate import RiskGate
from agenkit_trader.risk.sizing import floor_to_lot
from agenkit_trader.runtime.loop import (
    Runtime,
    check_clock_skew,
    client_order_id,
    protective_stop,
)

MODE = "smoke"
CONFIRM_PHRASE = "SMOKE TESTNET"
EXIT_OK, EXIT_FAILED, EXIT_NOT_FLAT, EXIT_INTERRUPTED = 0, 1, 4, 130
STOP_DISTANCE_FRAC = 0.05  # protective stop 5% below the entry
NOTIONAL_BUFFER = 1.05  # price-drift buffer over the exchange minimum notional
VISIBLE_TRIES = 5
VISIBLE_WAIT_S = 0.6

STEP_NAMES = {
    1: "connectivity, clock skew, symbol rules",
    2: "account balance read (account flat)",
    3: "MARKET entry through RiskGate",
    4: "exchange-side stop visible",
    5: "reconcile position and stop",
    6: "stop-coverage check",
    7: "cancel stop, close reduce-only, flat",
    8: "KillSwitch flatten path",
}


class SmokeRefused(Exception):
    """The run must not start (message never holds credentials)."""


class _Fail(Exception):
    pass


class SmokeLog(DecisionLog):
    """The smoke log: every record carries ``"mode": "smoke"``. Refuses the
    paper log's file name so the two can never be mixed."""

    def __init__(self, path: Path) -> None:
        if Path(path).name == "paper.jsonl":
            raise ValueError("the smoke log must not be the paper decision log")
        super().__init__(path)

    def write(
        self, kind: str, inputs: dict[str, Any], output: dict[str, Any], result: str
    ) -> None:
        record = {
            "ts": datetime.now(UTC).isoformat(),
            "mode": MODE,
            "kind": kind,
            "inputs": inputs,
            "output": output,
            "result": result,
        }
        line = json.dumps(record, sort_keys=True, default=str) + "\n"
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line)
            f.flush()


@dataclass
class StepResult:
    n: int
    name: str
    status: str  # pass | fail | skip
    reason: str


@dataclass
class SmokeReport:
    steps: list[StepResult] = field(default_factory=list)
    flat: bool | None = None  # None: could not be verified
    flat_note: str = ""
    interrupted: bool = False

    @property
    def exit_code(self) -> int:
        if self.flat is not True:
            return EXIT_NOT_FLAT
        if self.interrupted:
            return EXIT_INTERRUPTED
        return EXIT_OK if all(s.status == "pass" for s in self.steps) else EXIT_FAILED

    def table(self) -> str:
        w = max(len(s.name) for s in self.steps) if self.steps else 10
        lines = [f"{'#':>2}  {'step':<{w}}  {'result':<6}  reason"]
        for s in self.steps:
            lines.append(f"{s.n:>2}  {s.name:<{w}}  {s.status:<6}  {s.reason}")
        if self.flat is True:
            lines.append("Account verified FLAT (no positions, no open orders).")
        elif self.flat is False:
            lines.append(f"Account is NOT FLAT: {self.flat_note}. Check it by hand.")
        else:
            lines.append(
                "!! Account flat status UNKNOWN: the final check could not run "
                f"({self.flat_note}). CHECK THE TESTNET ACCOUNT BY HAND NOW."
            )
        return "\n".join(lines)


def _scrub(text: str) -> str:
    """Remove the API key and secret values from any text (defence in depth:
    venue errors never hold them)."""
    for name in ("BINANCE_TESTNET_KEY", "BINANCE_TESTNET_SECRET"):
        val = os.environ.get(name, "")
        if len(val) >= 6:
            text = text.replace(val, "[redacted]")
    return text


def _exc_text(exc: BaseException) -> str:
    if isinstance(exc, _Fail):
        return _scrub(str(exc))
    if isinstance(exc, VenueError | SmokeRefused):
        return _scrub(f"{type(exc).__name__}: {exc}")
    return f"{type(exc).__name__} (message not shown)"


def smallest_valid_qty(price: float, rules: SymbolRules) -> float:
    """Fewest whole lots whose notional clears the minimum (plus a small buffer
    for price drift between the mark read and the fill)."""
    n = max(
        1, math.ceil(rules.min_notional * NOTIONAL_BUFFER / (price * rules.lot) - 1e-9)
    )
    return round(n * rules.lot, 12)


def _summ(o: OpenOrder) -> str:
    return (
        f"{o.kind} {o.side} qty={o.qty} stop={o.stop_price} "
        f"reduce_only={o.reduce_only} id={o.client_id}"
    )


def _close(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12)


class _Smoke:
    def __init__(
        self,
        venue: Venue,
        symbol: str,
        risk: RiskConfig,
        file_rules: dict[str, SymbolRules],
        log: SmokeLog,
        clock: Callable[[], datetime],
        sleep: Callable[[float], None],
    ) -> None:
        self.venue, self.symbol, self.risk = venue, symbol, risk
        self.file_rules, self.log, self.clock, self.sleep = (
            file_rules,
            log,
            clock,
            sleep,
        )
        self.gate = RiskGate(risk, venue.symbol_rules, log)
        self.run_bar = pd.Timestamp(clock())
        self.cleaned = False
        self.touched = False  # an order was attempted: cleanup may flatten
        self.details: dict[str, Any] = {}
        self.qty = 0.0
        self.entry_price = 0.0
        self.stop_price = 0.0
        self.stop_cid = ""

    def cid(self, kind: str) -> str:
        return client_order_id("smoke", self.symbol, self.run_bar, kind)

    # ------------------------------------------------------------ helpers

    def _state(self) -> AccountState:
        acct = self.venue.get_account()
        return replace(acct, last_bar_time=self.clock())

    def _symbol_orders(self) -> list[OpenOrder]:
        return [o for o in self.venue.get_open_orders() if o.symbol == self.symbol]

    def _gated(self, order: Order, state: AccountState, what: str) -> None:
        d = self.gate.check(order, state, self.clock())
        if not d.allowed:
            raise _Fail(f"RiskGate rejected the {what}: {d.reason}")
        if d.max_qty != order.qty:
            raise _Fail(f"RiskGate approved {d.max_qty} not {order.qty} for the {what}")

    def _enter(self, tag: str) -> None:
        """Gated MARKET buy of the smallest valid size."""
        price = self.venue.get_mark_price(self.symbol)
        rules = self.venue.symbol_rules(self.symbol)
        qty = smallest_valid_qty(price, rules)
        stop = protective_stop(price, 1.0, price * STOP_DISTANCE_FRAC, rules.tick)
        entry = Order(
            symbol=self.symbol,
            side="buy",
            qty=qty,
            price_ref=price,
            stop_price=stop,
            strategy="smoke",
            client_id=self.cid(f"{tag}entry"),
        )
        self._gated(entry, self._state(), "entry")
        self.touched = True
        fill = self.venue.place_order(entry)
        if not _close(fill.qty, qty):
            raise _Fail(f"partial fill: asked {qty}, filled {fill.qty}")
        self.qty = fill.qty
        self.entry_price = fill.price if fill.price > 0 else price
        self.stop_price = stop
        self.details.update(qty=qty, mark=price, fill_price=fill.price, stop_price=stop)

    def _place_stop(self, tag: str, rules: SymbolRules) -> str:
        self.stop_cid = self.cid(f"{tag}stop")
        stop_order = Order(
            symbol=self.symbol,
            side="sell",
            qty=floor_to_lot(self.qty, rules.lot),
            price_ref=self.entry_price,
            stop_price=self.stop_price,
            strategy="smoke",
            client_id=self.stop_cid,
            reduce_only=True,
        )
        self._gated(stop_order, self._state(), "stop")
        return self.venue.place_stop(stop_order)

    def _flat_now(self) -> tuple[bool, str]:
        acct = self.venue.get_account()
        orders = self.venue.get_open_orders()
        pos = {s: p.qty for s, p in acct.positions.items() if p.qty != 0}
        if pos or orders:
            return False, f"positions={pos} open_orders={[_summ(o) for o in orders]}"
        return True, ""

    # ------------------------------------------------------------ steps

    def step1(self) -> str:
        server = check_clock_skew(self.venue, self.clock)
        skew_ms = (server - self.clock()).total_seconds() * 1000.0
        live = self.venue.symbol_rules(self.symbol)
        want = self.file_rules.get(self.symbol)
        if want is None:
            raise _Fail(f"{self.symbol} is not in config/symbol_rules.yaml")
        diffs = [
            f"{f} file={getattr(want, f)} exchange={getattr(live, f)}"
            for f in ("tick", "lot", "min_notional")
            if not _close(getattr(want, f), getattr(live, f))
        ]
        if diffs:
            raise _Fail(
                "exchangeInfo differs from symbol_rules.yaml: " + "; ".join(diffs)
            )
        return (
            f"reachable, skew ~{skew_ms:.0f} ms, rules match "
            f"(tick {live.tick}, lot {live.lot}, min_notional {live.min_notional})"
        )

    def step2(self) -> str:
        acct = self.venue.get_account()
        orders = self.venue.get_open_orders()
        open_pos = {s: p.qty for s, p in acct.positions.items() if p.qty != 0}
        if open_pos or orders:
            raise _Fail(
                "account is not flat (smoke never touches positions or orders it "
                f"did not create): positions={open_pos} open_orders={len(orders)}; "
                "close them yourself or use a fresh testnet account"
            )
        if not acct.equity > 0:
            raise _Fail(f"equity read as {acct.equity}")
        return f"equity {acct.equity:.2f} USDT, account flat"

    def step3(self) -> str:
        self._enter("")
        d = self.details
        return (
            f"bought {d['qty']} {self.symbol} at ~{d['fill_price']} "
            f"(min notional {self.venue.symbol_rules(self.symbol).min_notional}), "
            "RiskGate allowed"
        )

    def step4(self) -> str:
        rules = self.venue.symbol_rules(self.symbol)
        ref = self._place_stop("", rules)
        raw_fn = getattr(self.venue, "open_algo_orders_raw", None)
        seen: list[OpenOrder] = []
        found: OpenOrder | None = None
        raw: list[Any] = []
        for i in range(VISIBLE_TRIES):
            seen = self._symbol_orders()
            found = next((o for o in seen if o.client_id == self.stop_cid), None)
            if callable(raw_fn):
                raw = raw_fn(self.symbol)
            if found is not None:
                break
            if i < VISIBLE_TRIES - 1:
                self.sleep(VISIBLE_WAIT_S)
        self.details.update(place_stop_returned=str(ref), raw_algo_rows=raw)
        self.log.write(
            "smoke_detail",
            {"step": 4},
            {"place_stop_returned": str(ref), "raw_algo_rows": raw},
            "recorded",
        )
        if found is None:
            raise _Fail(
                f"place_stop returned id {ref!r} but the stop (client id "
                f"{self.stop_cid}) is not in the open orders after {VISIBLE_TRIES} "
                f"reads; open orders on {self.symbol}: "
                f"{[_summ(o) for o in seen] or 'none'}; raw algo rows: {raw or 'none'}"
            )
        bad = []
        if found.kind != "STOP_MARKET":
            bad.append(f"type {found.kind!r}, expected STOP_MARKET")
        if found.side != "sell":
            bad.append(f"side {found.side!r}, expected sell")
        if not found.reduce_only:
            bad.append("reduce_only False, expected True")
        if found.stop_price is None or not _close(found.stop_price, self.stop_price):
            bad.append(f"stop_price {found.stop_price}, expected {self.stop_price}")
        if not _close(found.qty, self.qty):
            bad.append(f"qty {found.qty}, expected {self.qty}")
        if bad:
            raise _Fail(f"stop is listed (id {ref!r}) but differs: " + "; ".join(bad))
        return f"place_stop returned id {ref!r}; listed as {_summ(found)}"

    def step5(self) -> str:
        pos = self._state().positions.get(self.symbol)
        if pos is None or pos.qty <= 0:
            raise _Fail(f"expected a long {self.symbol} position, exchange has {pos}")
        if not _close(pos.qty, self.qty):
            raise _Fail(f"position qty {pos.qty} != filled qty {self.qty}")
        stops = Runtime._stops_for(pos, self._symbol_orders())
        mine = [o for o in stops if o.client_id == self.stop_cid]
        if not mine:
            raise _Fail(
                "runtime reconcile would not see our stop: reduce-only closing-side "
                f"stops seen {[_summ(o) for o in stops] or 'none'}"
            )
        return f"position long {pos.qty} @ {pos.entry_price}, stop {self.stop_cid} seen"

    def step6(self) -> str:
        pos = self._state().positions.get(self.symbol)
        if pos is None:
            raise _Fail("position vanished before the coverage check")
        stops = Runtime._stops_for(pos, self._symbol_orders())
        if not Runtime._is_covered(pos, stops):
            raise _Fail(
                f"stops cover {sum(o.qty for o in stops)} of {abs(pos.qty)} (the "
                "runtime would repair, then halt)"
            )
        return f"stops cover {sum(o.qty for o in stops)} >= position {abs(pos.qty)}"

    def step7(self) -> str:
        self.venue.cancel_stop(self.symbol, self.stop_cid)
        left = [o for o in self._symbol_orders() if o.client_id == self.stop_cid]
        if left:
            raise _Fail("stop still listed after cancel_stop: " + _summ(left[0]))
        pos = self._state().positions.get(self.symbol)
        if pos is None or pos.qty <= 0:
            raise _Fail(f"nothing to close: position {pos}")
        close = Order(
            symbol=self.symbol,
            side="sell",
            qty=abs(pos.qty),
            price_ref=self.venue.get_mark_price(self.symbol),
            stop_price=None,
            strategy="smoke",
            client_id=self.cid("close"),
            reduce_only=True,
        )
        self._gated(close, self._state(), "close")
        self.venue.place_order(close)
        ok, why = self._flat_now()
        if not ok:
            raise _Fail("not flat after the reduce-only close: " + why)
        return "stop cancelled, reduce-only close filled, account flat"

    def step8(self) -> str:
        self._enter("ks")
        self._place_stop("ks", self.venue.symbol_rules(self.symbol))
        before = self._state().positions.get(self.symbol)
        if before is None or before.qty <= 0:
            raise _Fail("second position did not open")
        ks = KillSwitch(self.venue, self.log, self.risk)
        flattened_ok = ks.trigger("smoke_test")
        if not flattened_ok:
            raise _Fail("KillSwitch.trigger returned flattened=False")
        ok, why = self._flat_now()
        if not ok:
            raise _Fail("KillSwitch reported flattened but: " + why)
        return (
            "second position + stop opened; KillSwitch flattened_ok, "
            "no orders or positions"
        )


def _check_testnet_only(venue: object) -> None:
    """Fail closed: a venue without a ``base_url`` is refused too."""
    base = getattr(venue, "base_url", None)
    if base is None:
        raise SmokeRefused("venue does not state its base_url; refusing (fail closed)")
    u = urlsplit(str(base))
    if u.scheme != "https" or u.hostname != TESTNET_HOST:
        raise SmokeRefused("venue is not the Binance futures testnet host")


def _safe_log(
    log: SmokeLog, kind: str, inputs: dict[str, Any], out: dict[str, Any], result: str
) -> None:
    with contextlib.suppress(Exception):  # a failing log must never stop cleanup
        log.write(kind, inputs, out, result)


def run_smoke(
    venue: Venue,
    *,
    symbol: str,
    risk: RiskConfig,
    file_rules: dict[str, SymbolRules],
    log: SmokeLog,
    clock: Callable[[], datetime],
    sleep: Callable[[float], None] = time.sleep,
) -> SmokeReport:
    """Run the checklist. Once smoke has touched the account, cleanup (flatten)
    and the final flat verification ALWAYS run, whatever ended the steps
    (a step failure, KeyboardInterrupt, SystemExit, a failing log write)."""
    _check_testnet_only(venue)
    s = _Smoke(venue, symbol, risk, file_rules, log, clock, sleep)
    report = SmokeReport()
    aborted: BaseException | None = None
    n = 0
    try:
        log.write("smoke_run", {"symbol": symbol}, {}, "start")
        failed = False
        for n in range(1, 9):
            name = STEP_NAMES[n]
            if failed:
                res = StepResult(n, name, "skip", "not run: an earlier step failed")
            else:
                try:
                    res = StepResult(n, name, "pass", getattr(s, f"step{n}")())
                except Exception as exc:
                    res = StepResult(n, name, "fail", _exc_text(exc))
                    failed = True
            report.steps.append(res)
            log.write(
                "smoke_step",
                {"step": n, "name": name},
                {"reason": res.reason},
                res.status,
            )
            if res.status == "fail" and s.touched:
                _cleanup(s)
    except BaseException as exc:
        aborted = exc
        report.interrupted = isinstance(exc, KeyboardInterrupt | SystemExit)
        why = (
            "interrupted"
            if report.interrupted
            else f"run aborted outside a step: {_exc_text(exc)}"
        )
        if len(report.steps) < n:  # the step in flight never reported
            report.steps.append(StepResult(n, STEP_NAMES[n], "fail", why))
        for k in range(len(report.steps) + 1, 9):
            report.steps.append(StepResult(k, STEP_NAMES[k], "skip", f"not run: {why}"))
    if aborted is not None and s.touched:
        _cleanup(s)
    _final_flat(s, report)
    _safe_log(
        log,
        "smoke_run",
        {"symbol": symbol},
        {"flat": report.flat, "exit_code": report.exit_code},
        "end",
    )
    return report


def _cleanup(s: _Smoke) -> None:
    """Smoke placed orders and the run is ending badly: flatten (the account
    was verified flat at step 2, so everything open is ours). Runs once."""
    if s.cleaned:
        return
    try:
        s.venue.flatten_all()
        s.cleaned = True
        _safe_log(s.log, "smoke_cleanup", {}, {}, "flatten_ok")
    except BaseException as exc:
        s.cleaned = True  # one attempt; the final check reports the truth
        _safe_log(
            s.log, "smoke_cleanup", {}, {"error": _exc_text(exc)}, "flatten_failed"
        )


def _final_flat(s: _Smoke, report: SmokeReport) -> None:
    try:
        ok, why = s._flat_now()
        report.flat, report.flat_note = ok, why
    except BaseException as exc:
        report.flat, report.flat_note = None, _exc_text(exc)
    _safe_log(
        s.log,
        "smoke_final",
        {},
        {"flat": report.flat, "note": report.flat_note},
        "flat" if report.flat else "not_flat",
    )
