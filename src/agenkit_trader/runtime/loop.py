"""Paper-trading runtime: closed bars -> deployed strategies -> gate -> venue.

Invariants (each is tested in ``tests/runtime/test_loop.py``):

* Every order, including the exchange-side protective stop, passes
  ``RiskGate.check`` and is submitted with exactly the quantity the gate
  allowed. Only the kill switch (``venue.flatten_all``) bypasses the gate.
* Nothing is placed while the kill switch is halted. The ``AccountState``
  handed to the gate has ``halted = kill.halted or venue_state.halted``.
* Only CLOSED bars are used. "Closed" is judged on the VENUE clock: a bar is
  closed when ``open + interval + CLOSE_GRACE <= min(server time, local
  time)``, so clock skew can never make the forming bar look closed. One
  decision per bar per strategy, recorded in the state file before any order
  is submitted, so processing a bar twice (also across a restart) places
  nothing new. Client order ids are derived from strategy/symbol/bar/kind.
* Entries happen only on a signal transition at the latest closed bar
  (previous bar's target != this bar's target): a restart never enters
  mid-signal and a disaster-stop exit is not re-entered until a fresh signal.
* Regimes (v1, fail closed): runtime regime labels cannot yet reproduce the
  research labels exactly (research used an expanding median over years of
  history; the runtime sees at most 1499 bars), so a deployment whose
  ``allowed_regimes`` is a strict subset of ``REGIMES`` is refused at start.
  Unrestricted deployments need no label.
* Protective stop: ``close -/+ stop_atr_mult * ATR(atr_window)`` (rounded to
  the tick toward the entry, so the loss at the stop never exceeds the sized
  budget); size via ``size_position`` (floored to lot); after the fill a
  reduce-only STOP_MARKET is placed.
* Stop coverage, EVERY cycle, for every owned position: the reduce-only
  stops on the closing side must cover ``abs(qty)``. If not, ONE repair per
  uncovered episode: a replacement for the full position is placed (through
  the gate, client id deterministic per symbol/size/bar), read back, and only
  if the read-back shows coverage are the old stops cancelled. If the repair
  fails, the read-back is not covered, or the next check after a repair is
  still not covered, the kill switch flattens and halts (no repair loop).
  A flat symbol with open orders has them cancelled (orphans). This also
  repairs the outcome of an ambiguous order (filled on the exchange but the
  response was lost) and of a partial exit.
* Ambiguous orders: ``OrderNotFilledError`` is the only failure treated as
  "not filled". Any other ``place_order`` failure (timeout, 5xx, status NEW)
  is "unknown": the order is looked up by its client id, the position is
  re-read, and the per-cycle coverage check protects whatever exists.
* Exits: after an exit (whatever its outcome) the position is re-read. Only
  when flat are the symbol's stops cancelled; a residual keeps its stop.
* Gate valuation: ``Position.entry_price`` is used by the gate only to value
  OTHER symbols' notional for the leverage cap (and checked > 0). The runtime
  passes positions re-valued at the venue's current MARK price.
* Gate data time: entries/exits use the decided bar's close time as
  ``last_bar_time``; a protective stop placed by the coverage check uses the
  time its inputs (account, mark, bars) were just read, because it only
  reduces exposure and must not be blocked mid-bar.
* Paper equity = ``risk.start_equity + (venue equity - venue equity at first
  start)``. Baseline, peak and UTC day-start equity live in the state file;
  daily loss or drawdown at/over the limit trips the kill switch.
* Staleness: newest closed bar older than ``interval + stale_after_s`` ->
  kill switch. Clock skew > MAX_CLOCK_SKEW_MS (measured at the request
  midpoint; a venue with ``sync_time`` is re-synced each cycle) -> kill
  switch. Every venue exception is counted with ``record_api_error``; gate
  rejections are logged and the loop continues; unexpected internal
  exceptions trip the kill switch.
* Restart reconciliation (``start``): positions and open orders are read
  BEFORE trading. Unowned position/order -> logged, refuse (default) or kill
  switch flatten (``flatten_unowned_on_start``). Owned positions are adopted
  and stop coverage is enforced. One deployed strategy per symbol.
* State: a MISSING state file refuses start unless ``init_state`` is given
  (logged ``state/init``); a corrupt one always refuses. A halt is written to
  the state file and to the decision log (``halt``; the kill switch also logs
  ``kill_switch``); start refuses while the decision log's latest halt has no
  later ``runtime/halt_reset`` record, so deleting the state file does not
  bypass a halt.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

import pandas as pd

from agenkit_trader.config import RuntimeConfig
from agenkit_trader.data.align import align_asof
from agenkit_trader.data.archive import (
    DataGapError,
    check_continuity,
    interval_to_timedelta,
)
from agenkit_trader.execution.venue import (
    MAX_CLOCK_SKEW_MS,
    AccountState,
    ClockSkewError,
    Fill,
    OpenOrder,
    Order,
    OrderNotFilledError,
    Position,
    RateLimited,
    SymbolRules,
    Venue,
)
from agenkit_trader.killswitch import KillSwitch
from agenkit_trader.log import DecisionLog
from agenkit_trader.regime import REGIMES
from agenkit_trader.risk.gate import RiskGate
from agenkit_trader.risk.sizing import floor_to_lot, size_position
from agenkit_trader.strategies.base import Strategy
from agenkit_trader.strategies.funding_reversion import FUNDING_AVAILABLE_LAG

FUNDING_LIMIT = 1000  # Binance fundingRate max per request
SUPPORTED_AUX = ("funding",)
CLOSE_GRACE = pd.Timedelta(seconds=2)
HALT_KINDS = ("kill_switch", "halt")
# Rate-limit back-off (HTTP 429): wait the venue's Retry-After, at least one
# poll interval, at most MAX_BACKOFF_S; DEFAULT_BACKOFF_S without the header.
MAX_BACKOFF_S = 900.0
DEFAULT_BACKOFF_S = 60.0
_T = TypeVar("_T")


class DeployedError(Exception):
    """deployed.json is missing, unreadable, malformed or empty."""


class StartupRefused(Exception):
    """The runtime will not trade; the message tells the user why."""


class _VenueFailed(Exception):
    """A venue call failed; it has been logged and counted."""


@dataclass(frozen=True)
class Deployment:
    name: str
    symbol: str
    interval: str
    params: dict[str, Any]
    allowed_regimes: list[str]

    @property
    def key(self) -> str:
        return f"{self.name}|{self.symbol}|{self.interval}"


_DEPLOYED_KEYS = {"name", "symbol", "interval", "params", "allowed_regimes"}


def load_deployed(path: Path) -> list[Deployment]:
    """Read the validation gate's ``deployed.json`` (a JSON list of
    ``{name, symbol, interval, params, allowed_regimes}``). Raises
    ``DeployedError`` if it is missing, unreadable, malformed or EMPTY."""
    path = Path(path)
    if not path.exists():
        raise DeployedError(
            f"{path} not found: no strategy has been signed off by the "
            "validation gate (run `agenkit research`)"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise DeployedError(f"{path} is unreadable: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise DeployedError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise DeployedError(f"{path}: expected a JSON list of deployments")
    if not data:
        raise DeployedError(
            f"{path} is empty: no strategy passed the validation gate, so there "
            "is nothing to paper trade"
        )
    out: list[Deployment] = []
    for i, e in enumerate(data):
        if not isinstance(e, dict) or set(e) != _DEPLOYED_KEYS:
            raise DeployedError(
                f"{path}[{i}]: expected exactly the keys {sorted(_DEPLOYED_KEYS)}"
            )
        ok = (
            all(isinstance(e[k], str) and e[k] for k in ("name", "symbol", "interval"))
            and isinstance(e["params"], dict)
            and isinstance(e["allowed_regimes"], list)
            and all(isinstance(r, str) for r in e["allowed_regimes"])
        )
        if not ok:
            raise DeployedError(f"{path}[{i}]: wrong field types")
        out.append(
            Deployment(
                e["name"],
                e["symbol"],
                e["interval"],
                dict(e["params"]),
                list(e["allowed_regimes"]),
            )
        )
    return out


def validate_deployments(
    deployed: Iterable[Deployment], strategies: Mapping[str, Strategy]
) -> None:
    """Refuse unless every deployment names a known strategy with params from
    its grid, a valid interval, all regimes allowed, one per symbol."""
    seen: set[str] = set()
    for d in deployed:
        s = strategies.get(d.name)
        if s is None:
            raise StartupRefused(f"unknown strategy {d.name!r} in deployed.json")
        grid = s.param_grid
        if set(d.params) != set(grid) or any(d.params[k] not in grid[k] for k in grid):
            raise StartupRefused(
                f"{d.name}: params {d.params} are not a point of its parameter "
                f"grid {grid}"
            )
        try:
            interval_to_timedelta(d.interval)
        except ValueError as exc:
            raise StartupRefused(f"{d.name}: bad interval {d.interval!r}") from exc
        bad = [r for r in d.allowed_regimes if r not in REGIMES]
        if bad:
            raise StartupRefused(f"{d.name}: unknown regime(s) {bad}")
        if set(d.allowed_regimes) != set(REGIMES):
            raise StartupRefused(
                f"{d.name} on {d.symbol}: regime-restricted deployment "
                f"(allowed_regimes={d.allowed_regimes}) is refused in v1: the "
                "runtime cannot yet reproduce the research regime labels exactly "
                "(research used an expanding median over the full archive "
                "history, the runtime sees at most 1499 bars), so the stay-out "
                "rule could not be honoured reliably"
            )
        unknown_aux = [
            a for a in getattr(s, "aux_required", ()) if a not in SUPPORTED_AUX
        ]
        if unknown_aux:
            raise StartupRefused(f"{d.name}: unsupported aux {unknown_aux}")
        if d.symbol in seen:
            raise StartupRefused(
                f"{d.symbol}: one deployed strategy per symbol (positions net "
                "per symbol on the venue, so ownership would be ambiguous)"
            )
        seen.add(d.symbol)


def client_order_id(name: str, symbol: str, bar_open: pd.Timestamp, kind: str) -> str:
    """Deterministic, Binance-valid (<= 36 chars of [A-Za-z0-9]) client id."""
    raw = f"{name}|{symbol}|{pd.Timestamp(bar_open).isoformat()}|{kind}"
    return "ak" + hashlib.sha256(raw.encode()).hexdigest()[:30]


def check_clock_skew(venue: Venue, clock: Callable[[], datetime]) -> datetime:
    """Re-sync the venue offset (if it has ``sync_time``), then compare the
    venue time with the MIDPOINT of the local request window. Raises
    ``ClockSkewError`` above MAX_CLOCK_SKEW_MS; returns the venue time."""
    sync = getattr(venue, "sync_time", None)
    if callable(sync):
        sync()
    t0 = clock()
    server = venue.server_time()
    t1 = clock()
    mid = t0 + (t1 - t0) / 2
    skew_ms = (server - mid).total_seconds() * 1000.0
    if not abs(skew_ms) <= MAX_CLOCK_SKEW_MS:
        raise ClockSkewError(
            f"clock skew {skew_ms:.0f} ms vs venue exceeds {MAX_CLOCK_SKEW_MS} ms"
        )
    return server


def closed_bars(bars: pd.DataFrame, step: pd.Timedelta, now: datetime) -> pd.DataFrame:
    """Drop the forming bar (and anything later): keep bars with
    ``open + step + CLOSE_GRACE <= now`` (``now`` = venue-time based)."""
    if len(bars) == 0:
        return bars
    idx = pd.DatetimeIndex(bars.index)
    keep = (idx + (step + CLOSE_GRACE)) <= pd.Timestamp(now)
    out: pd.DataFrame = bars.loc[keep]
    return out


def atr(bars: pd.DataFrame, window: int) -> float:
    """Simple-mean Average True Range over ``window`` closed bars."""
    h, lo, c = (bars[k].astype("float64") for k in ("high", "low", "close"))
    pc = c.shift()
    tr = pd.concat([h - lo, (h - pc).abs(), (lo - pc).abs()], axis=1).max(axis=1)
    return float(tr.rolling(window).mean().iloc[-1])


def _sign(x: object) -> float:
    try:
        v = float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(v) or v == 0:
        return 0.0
    return 1.0 if v > 0 else -1.0


def _decimals(step: float) -> int:
    return max(0, -int(f"{step:e}".split("e")[1])) + 2


def protective_stop(close: float, side: float, dist: float, tick: float) -> float:
    """``close - side*dist`` rounded to the tick TOWARD the entry."""
    raw = close - side * dist
    n = raw / tick
    k = math.ceil(n - 1e-9) if side > 0 else math.floor(n + 1e-9)
    return round(k * tick, _decimals(tick))


@dataclass
class _PaperState:
    baseline_equity: float | None = None  # venue equity at first start
    peak_equity: float | None = None  # paper
    day: str | None = None  # UTC date of day_start_equity
    day_start_equity: float | None = None  # paper
    last_bar: dict[str, str] = field(default_factory=dict)
    halted: str | None = None

    @classmethod
    def load(cls, path: Path) -> _PaperState:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            return cls(**data)
        except (OSError, ValueError, TypeError) as exc:
            raise StartupRefused(
                f"runtime state file {path} is unreadable ({exc}); refusing to "
                "start rather than lose drawdown/halt state"
            ) from exc

    def save(self, path: Path | None) -> None:
        if path is None:
            return
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(asdict(self), indent=2, sort_keys=True) + "\n"
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


def last_unreset_halt(log_path: Path) -> str | None:
    """Timestamp of the decision log's latest halt record (``kill_switch`` or
    ``halt``) that has no later ``runtime/halt_reset`` record, else None.
    An unreadable log refuses (fail closed)."""
    path = Path(log_path)
    if not path.exists():
        return None
    halt: str | None = None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise StartupRefused(
            f"decision log {path} is unreadable ({exc}); cannot verify that no "
            "halt is outstanding"
        ) from exc
    for n, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
            kind, result = rec.get("kind"), rec.get("result")
        except (ValueError, AttributeError) as exc:
            raise StartupRefused(
                f"decision log {path} line {n} is malformed ({exc}); cannot "
                "verify that no halt is outstanding"
            ) from exc
        if kind in HALT_KINDS:
            halt = str(rec.get("ts"))
        elif kind == "runtime" and result == "halt_reset":
            halt = None
    return halt


def check_state_ready(
    state_path: Path | None, log_path: Path, init_state: bool
) -> None:
    """Refuse on an outstanding logged halt (even with ``init_state``), on a
    MISSING state file without ``init_state``, and on a corrupt one."""
    halt = last_unreset_halt(log_path)
    if halt is not None:
        raise StartupRefused(
            f"the decision log {log_path} records a halt at {halt} with no later "
            "reset; inspect it, then run `agenkit reset-halt`"
        )
    if state_path is None:
        return
    if not Path(state_path).exists():
        if not init_state:
            raise StartupRefused(
                f"state file {state_path} does not exist; it holds the paper "
                "account's P&L baseline, peak/day-start equity and any halt. If "
                "this really is a NEW paper account, pass --init-state"
            )
        return
    st = _PaperState.load(Path(state_path))
    if st.halted is not None:
        raise StartupRefused(
            f"a previous run halted (at {st.halted}); inspect the decision log, "
            "then run `agenkit reset-halt`"
        )


def reset_halt(state_path: Path, log: DecisionLog) -> bool:
    """Clear a persisted halt in the state file (if any) and log the reset
    (the CLI asks for the confirmation phrase first)."""
    was_logged = last_unreset_halt(log.path)
    was_state: str | None = None
    if Path(state_path).exists():
        st = _PaperState.load(Path(state_path))
        was_state, st.halted = st.halted, None
        st.save(state_path)
    log.write(
        "runtime",
        {"state": str(state_path)},
        {"was_state": was_state, "was_logged": was_logged},
        "halt_reset",
    )
    return was_state is not None or was_logged is not None


def _fill_output(fill: Fill) -> dict[str, Any]:
    """Decision-record output for a fill; an unreported commission is logged
    as "fee unknown", never as 0."""
    return {
        "fill": asdict(fill),
        "fee": "fee unknown" if fill.fee is None else fill.fee,
    }


class Runtime:
    def __init__(
        self,
        venue: Venue,
        strategies: Iterable[Strategy] | Mapping[str, Strategy],
        deployed: list[Deployment],
        gate: RiskGate,
        kill: KillSwitch,
        log: DecisionLog,
        clock: Callable[[], datetime],
        *,
        runtime_cfg: RuntimeConfig,
        state_path: Path | None = None,
        init_state: bool = False,
    ) -> None:
        if isinstance(strategies, Mapping):
            self.strategies = dict(strategies)
        else:
            self.strategies = {s.name: s for s in strategies}
        self.venue = venue
        self.deployed = list(deployed)
        self.gate = gate
        self.kill = kill
        self.log = log
        self.clock = clock
        self.cfg = runtime_cfg
        self.risk = gate.cfg
        self.state_path = state_path
        self.init_state = init_state
        self._st = (
            _PaperState.load(Path(state_path))
            if state_path is not None and Path(state_path).exists()
            else _PaperState()
        )
        # Nothing is written to the state file until start() has passed its
        # state checks (a refused start never creates or alters it).
        self._may_write = False
        self._started = False
        self._data_now: datetime | None = None
        # symbol -> position size at the repair attempt of the current
        # uncovered episode (cleared once a check sees coverage or flat)
        self._repaired: dict[str, float] = {}
        # largest Retry-After seen this cycle (seconds); run_loop sleeps it
        self.backoff_s = 0.0

    # ------------------------------------------------------------ helpers

    def _call(self, fn: Callable[..., _T], *args: Any) -> _T:
        """Call the venue; on any exception log it, count it as an API error
        (or trip the kill switch on clock skew) and raise ``_VenueFailed``."""
        try:
            return fn(*args)
        except Exception as exc:
            name = getattr(fn, "__name__", "venue_call")
            self.log.write(
                "venue_error",
                {"call": name, "args": [str(a) for a in args]},
                {"error": repr(exc)},
                "error",
            )
            if isinstance(exc, ClockSkewError):
                self.kill.trigger("clock_skew")
            else:
                self.kill.record_api_error(self.clock())
                if isinstance(exc, RateLimited):
                    self.note_rate_limit(exc)
            raise _VenueFailed(name) from exc

    def note_rate_limit(self, exc: RateLimited) -> None:
        """A 429 sets the back-off run_loop sleeps; a 418 (IP ban) halts."""
        self.log.write(
            "rate_limited",
            {"retry_after": exc.retry_after, "banned": exc.banned},
            {"error": str(exc)},
            "halt" if exc.banned else "backoff",
        )
        if exc.banned:
            self.kill.trigger("ip_banned")
            return
        wait = DEFAULT_BACKOFF_S if exc.retry_after is None else exc.retry_after
        self.backoff_s = max(self.backoff_s, wait)

    def _save(self) -> None:
        if self.kill.halted and self._st.halted is None:
            self._st.halted = self.clock().isoformat()
            self.log.write("halt", {"state": str(self.state_path)}, {}, "halted")
        if self._may_write:
            self._st.save(self.state_path)

    def _paper_equity(self, venue_equity: float, now: datetime) -> float:
        st = self._st
        if st.baseline_equity is None:
            st.baseline_equity = venue_equity
        eq = self.risk.start_equity + (venue_equity - st.baseline_equity)
        day = pd.Timestamp(now).tz_convert("UTC").date().isoformat()
        if st.day != day or st.day_start_equity is None:
            st.day, st.day_start_equity = day, eq
        st.peak_equity = eq if st.peak_equity is None else max(st.peak_equity, eq)
        self._save()
        return eq

    def build_state(self, acct: AccountState, last_bar_time: datetime) -> AccountState:
        """The gate's view: paper equity, positions valued at MARK, halted from
        the kill switch OR the venue state."""
        eq = self._paper_equity(acct.equity, self.clock())
        positions = {
            s: Position(s, p.qty, self._call(self.venue.get_mark_price, s))
            for s, p in acct.positions.items()
        }
        assert self._st.day_start_equity is not None
        assert self._st.peak_equity is not None
        return AccountState(
            equity=eq,
            positions=positions,
            day_start_equity=self._st.day_start_equity,
            peak_equity=self._st.peak_equity,
            last_bar_time=last_bar_time,
            halted=bool(self.kill.halted or acct.halted),
        )

    def _order_inputs(self, order: Order, state: AccountState, kind: str) -> Any:
        return {"kind": kind, "order": asdict(order), "state": asdict(state)}

    def submit(self, order: Order, state: AccountState, kind: str) -> Fill | None:
        """Gate-check then place a market order. Returns the fill, or None if
        refused, rejected, definitely not filled, or of unknown outcome (the
        per-cycle stop coverage check then protects whatever exists)."""
        now = self.clock()
        inputs = self._order_inputs(order, state, kind)
        if self.kill.halted:
            self.log.write("order", inputs, {"reason": "kill_switch_halted"}, "refused")
            return None
        decision = self.gate.check(order, state, now)
        if not decision.allowed:
            self.log.write("order", inputs, asdict(decision), "rejected")
            return None
        if decision.max_qty != order.qty:  # never submit what was not approved
            self.log.write("order", inputs, asdict(decision), "refused_qty_mismatch")
            return None
        try:
            fill = self._call(self.venue.place_order, order)
        except _VenueFailed as exc:
            if isinstance(exc.__cause__, OrderNotFilledError):
                self.log.write("order", inputs, asdict(decision), "not_filled")
                return None
            resolved = self._resolve(order)
            if resolved is not None:
                self.log.write(
                    "order", inputs, _fill_output(resolved), "filled_after_error"
                )
                return resolved
            self.log.write("order", inputs, asdict(decision), "unknown")
            return None
        self.log.write("order", inputs, _fill_output(fill), "filled")
        return fill

    def _resolve(self, order: Order) -> Fill | None:
        """Look an order of unknown outcome up by its client id."""
        try:
            st = self._call(self.venue.get_order, order.symbol, order.client_id)
        except _VenueFailed:
            return None
        if st is None or not st.executed_qty > 0:
            return None
        return Fill(
            symbol=order.symbol,
            side=order.side,
            qty=st.executed_qty,
            price=st.avg_price if st.avg_price > 0 else order.price_ref,
            fee=None,  # an order lookup reports no commission: fee unknown
            time=self.clock(),
        )

    def _protect(
        self,
        dep: Deployment,
        pos_side: float,
        qty: float,
        price_ref: float,
        stop: float,
        bar_open: pd.Timestamp,
        data_time: datetime,
        rules: SymbolRules,
        kind: str = "stop",
    ) -> bool:
        """Gate-check and place the reduce-only exchange stop; on any failure
        trip the kill switch (no unprotected position is kept)."""
        order = Order(
            symbol=dep.symbol,
            side="sell" if pos_side > 0 else "buy",
            qty=floor_to_lot(qty, rules.lot),
            price_ref=price_ref,
            stop_price=stop,
            strategy=dep.name,
            client_id=client_order_id(dep.name, dep.symbol, bar_open, kind),
            reduce_only=True,
        )
        try:
            state = self.build_state(self._call(self.venue.get_account), data_time)
        except _VenueFailed:
            self.kill.trigger("disaster_stop_failed")
            return False
        inputs = self._order_inputs(order, state, kind)
        if self.kill.halted:
            self.log.write("stop", inputs, {"reason": "kill_switch_halted"}, "refused")
            return False
        decision = self.gate.check(order, state, self.clock())
        if not decision.allowed or decision.max_qty != order.qty:
            self.log.write("stop", inputs, asdict(decision), "rejected")
            self.kill.trigger("disaster_stop_rejected")
            return False
        try:
            ref = self._call(self.venue.place_stop, order)
        except _VenueFailed:
            self.log.write("stop", inputs, asdict(decision), "error")
            self.kill.trigger("disaster_stop_failed")
            return False
        self.log.write("stop", inputs, {"venue_id": ref}, "placed")
        return True

    def _closed(self, dep: Deployment) -> pd.DataFrame:
        step = interval_to_timedelta(dep.interval)
        raw = self._call(
            self.venue.get_bars, dep.symbol, dep.interval, self.cfg.bars_limit + 1
        )
        assert self._data_now is not None
        return closed_bars(raw, step, self._data_now)

    def _aux(
        self, dep: Deployment, strat: Strategy, bars: pd.DataFrame
    ) -> dict[str, pd.Series]:
        aux: dict[str, pd.Series] = {}
        if "funding" in getattr(strat, "aux_required", ()):
            f = self._call(self.venue.get_funding, dep.symbol, FUNDING_LIMIT)
            aux["funding"] = align_asof(bars, f, FUNDING_AVAILABLE_LAG)
        return aux

    def _clock_check(self, now: datetime) -> bool:
        """Skew check + venue-time snapshot for this cycle. False (and the kill
        switch or the error counter engaged) if it fails."""
        try:
            server = check_clock_skew(self.venue, self.clock)
        except ClockSkewError as exc:
            self.log.write("clock_skew", {"now": now}, {"error": str(exc)}, "halt")
            self.kill.trigger("clock_skew")
            return False
        except Exception as exc:
            self.log.write(
                "venue_error", {"call": "server_time"}, {"error": repr(exc)}, "error"
            )
            self.kill.record_api_error(now)
            if isinstance(exc, RateLimited):
                self.note_rate_limit(exc)
            return False
        self._data_now = min(server, now)
        return True

    # ------------------------------------------------------------ protection

    def _coverage(self, d: Deployment) -> tuple[Position | None, list[OpenOrder]]:
        """Read the position and the open orders on ``d.symbol``."""
        pos = self._call(self.venue.get_account).positions.get(d.symbol)
        orders = [
            o for o in self._call(self.venue.get_open_orders) if o.symbol == d.symbol
        ]
        return pos, orders

    @staticmethod
    def _stops_for(pos: Position, orders: list[OpenOrder]) -> list[OpenOrder]:
        close_side = "sell" if pos.qty > 0 else "buy"
        return [
            o
            for o in orders
            if o.reduce_only and o.stop_price is not None and o.side == close_side
        ]

    @staticmethod
    def _is_covered(pos: Position, stops: list[OpenOrder]) -> bool:
        return sum(o.qty for o in stops) >= abs(pos.qty) * (1 - 1e-9)

    def _ensure_protected(self, d: Deployment, now: datetime) -> bool:
        """Every owned position must be covered by reduce-only stops on its
        closing side. Repair is bounded: ONE attempt per uncovered episode
        (place first, verify by read-back, only then cancel the old stops).
        Escalate to the kill switch if the read-back right after the repair is
        not covered, or if the next check after a repair is still not covered.
        Cancels orphan orders on a flat symbol."""
        pos, orders = self._coverage(d)
        if pos is None or pos.qty == 0:
            self._repaired.pop(d.symbol, None)
            if orders:
                self.log.write(
                    "orphan_orders",
                    {"orders": [asdict(o) for o in orders]},
                    {},
                    "cancel",
                )
                self._call(self.venue.cancel_open_orders, d.symbol)
            return True
        stops = self._stops_for(pos, orders)
        inputs = {"position": asdict(pos), "stops": [asdict(o) for o in stops]}
        need = abs(pos.qty)
        if self._is_covered(pos, stops):
            self._repaired.pop(d.symbol, None)  # episode over
            return True
        covered = {"covered": sum(o.qty for o in stops), "needed": need}
        if d.symbol in self._repaired:
            # repaired last time and still not covered: never repair again
            self.log.write("stop_coverage", inputs, covered, "escalate")
            self.kill.trigger("stop_not_confirmed")
            return False
        self.log.write("stop_coverage", inputs, covered, "repair")
        self._repaired[d.symbol] = need
        if not self._restore_stop(d, pos, now):
            return False
        pos2, orders2 = self._coverage(d)
        if pos2 is not None and pos2.qty != 0:
            stops2 = self._stops_for(pos2, orders2)
            if not self._is_covered(pos2, stops2):
                self.log.write(
                    "stop_coverage",
                    {"position": asdict(pos2), "stops": [asdict(o) for o in stops2]},
                    {"covered": sum(o.qty for o in stops2), "needed": abs(pos2.qty)},
                    "escalate",
                )
                self.kill.trigger("stop_not_confirmed")
                return False
        for o in stops:  # the new stop is confirmed: drop the old ones
            # a failure is logged and counted; a leftover reduce-only stop is
            # harmless
            with contextlib.suppress(_VenueFailed):
                self._call(self.venue.cancel_stop, d.symbol, o.client_id)
        return True

    def _restore_stop(self, d: Deployment, pos: Position, now: datetime) -> bool:
        try:
            bars = self._closed(d)
            rules = self._call(self.venue.symbol_rules, d.symbol)
        except _VenueFailed:
            self.kill.trigger("unprotected_position")
            return False
        a = atr(bars, self.cfg.atr_window) if len(bars) else float("nan")
        side = _sign(pos.qty)
        if not (math.isfinite(a) and a > 0):
            self.kill.trigger("unprotected_position")
            return False
        close = float(bars["close"].iloc[-1])
        stop = protective_stop(close, side, self.cfg.stop_atr_mult * a, rules.tick)
        return self._protect(
            d,
            side,
            abs(pos.qty),
            close,
            stop,
            bars.index[-1],
            self.clock(),  # inputs were read just now (see module docs)
            rules,
            # deterministic per (symbol, size, bar): a retry is idempotent
            kind=f"restop|{abs(pos.qty):.12g}",
        )

    # ------------------------------------------------------------ start

    def start(self) -> None:
        """Validate, check state/halts and the clock, reconcile with the venue.
        Raises ``StartupRefused`` (after logging) instead of trading."""
        now = self.clock()
        try:
            self._start(now)
        except StartupRefused as exc:
            self.log.write("runtime", {}, {"reason": str(exc)}, "start_refused")
            self._save()
            raise
        self._started = True
        self.log.write(
            "runtime",
            {"deployed": [asdict(d) for d in self.deployed]},
            {},
            "started",
        )

    def _start(self, now: datetime) -> None:
        if self.kill.halted:
            raise StartupRefused("kill switch is halted; manual reset required")
        check_state_ready(self.state_path, self.log.path, self.init_state)
        validate_deployments(self.deployed, self.strategies)
        if self.state_path is not None and not Path(self.state_path).exists():
            self.log.write("state", {"state": str(self.state_path)}, {}, "init")
        self._may_write = True
        self._save()
        if not self._clock_check(now):
            if self.kill.halted:
                raise StartupRefused("clock skew vs venue; kill switch halted")
            raise StartupRefused("venue unreachable at start (server time)")
        for d in self.deployed:
            if "funding" in getattr(self.strategies[d.name], "aux_required", ()):
                try:
                    f = self.venue.get_funding(d.symbol, 1)
                except Exception as exc:
                    if not isinstance(exc, NotImplementedError | AttributeError):
                        self.kill.record_api_error(now)
                    raise StartupRefused(
                        f"{d.name} needs funding data the venue cannot supply: {exc!r}"
                    ) from exc
                if len(f) == 0:
                    raise StartupRefused(
                        f"{d.name} needs funding data; venue returned none"
                    )
        try:
            acct = self.venue.get_account()
            orders = self.venue.get_open_orders()
        except Exception as exc:
            self.kill.record_api_error(now)
            raise StartupRefused(f"cannot read account/open orders: {exc!r}") from exc
        owned = {d.symbol: d for d in self.deployed}
        unowned = sorted(
            {s for s in acct.positions if s not in owned}
            | {o.symbol for o in orders if o.symbol not in owned}
        )
        if unowned:
            action = "flatten" if self.cfg.flatten_unowned_on_start else "refuse"
            self.log.write(
                "unowned_position",
                {
                    "positions": [asdict(p) for p in acct.positions.values()],
                    "open_orders": [asdict(o) for o in orders],
                },
                {"unowned_symbols": unowned},
                action,
            )
            if self.cfg.flatten_unowned_on_start:
                self.kill.trigger("unowned_position_at_start")
                raise StartupRefused(
                    f"positions/orders on {unowned} are not owned by any deployed "
                    "strategy; kill switch flattened everything and halted"
                )
            raise StartupRefused(
                f"positions/orders on {unowned} are not owned by any deployed "
                "strategy; close them yourself, or set flatten_unowned_on_start: "
                "true in config/runtime.yaml to have the kill switch flatten"
            )
        self._paper_equity(acct.equity, now)
        for sym, pos in acct.positions.items():
            self.log.write(
                "adopted_position",
                {
                    "position": asdict(pos),
                    "orders": [asdict(o) for o in orders if o.symbol == sym],
                },
                {"owner": owned[sym].key},
                "adopted",
            )
        for d in self.deployed:
            try:
                ok = self._ensure_protected(d, now)
            except _VenueFailed as exc:
                raise StartupRefused(
                    f"cannot verify stop coverage for {d.symbol}"
                ) from exc
            if not ok:
                raise StartupRefused(
                    f"could not place a protective stop for the open {d.symbol} "
                    "position; kill switch engaged"
                )

    # ------------------------------------------------------------ cycle

    def cycle(self) -> None:
        if not self._started:
            raise StartupRefused("start() has not succeeded; refusing to trade")
        now = self.clock()
        if self.kill.halted:
            self.log.write("runtime", {"now": now}, {}, "refused_halted")
            return
        try:
            self._cycle(now)
        finally:
            self._save()

    def _cycle(self, now: datetime) -> None:
        if not self._clock_check(now):
            return
        try:
            acct = self._call(self.venue.get_account)
        except _VenueFailed:
            return
        eq = self._paper_equity(acct.equity, now)
        st = self._st
        assert st.day_start_equity is not None and st.peak_equity is not None
        daily = (st.day_start_equity - eq) / st.day_start_equity
        dd = (st.peak_equity - eq) / st.peak_equity
        if not daily < self.risk.max_daily_loss:
            self.kill.trigger("daily_loss")
            return
        if not dd < self.risk.max_drawdown:
            self.kill.trigger("drawdown")
            return
        for d in self.deployed:
            for action in (self._step, self._ensure_protected):
                if self.kill.halted:
                    return
                try:
                    action(d, now)
                except _VenueFailed:
                    continue
                except Exception as exc:
                    self.log.write(
                        "runtime_error",
                        {"deployment": d.key},
                        {"error": repr(exc)},
                        "halt",
                    )
                    self.kill.trigger("internal_error")
                    return

    def _step(self, d: Deployment, now: datetime) -> None:
        strat = self.strategies[d.name]
        step = interval_to_timedelta(d.interval)
        bars = self._closed(d)
        if len(bars) == 0:
            self.log.write("stale_data", {"deployment": d.key}, {"bars": 0}, "halt")
            self.kill.trigger("stale_data")
            return
        last_open = pd.Timestamp(bars.index[-1])
        last_close = (last_open + step).to_pydatetime()
        age = pd.Timestamp(now) - pd.Timestamp(last_close)
        if age > step + pd.Timedelta(seconds=self.risk.stale_after_s):
            self.log.write(
                "stale_data",
                {"deployment": d.key, "last_bar_close": last_close, "now": now},
                {"age_s": age.total_seconds()},
                "halt",
            )
            self.kill.trigger("stale_data")
            return
        done = self._st.last_bar.get(d.key)
        if done is not None and last_open <= pd.Timestamp(done):
            return  # this bar has already been decided
        try:
            check_continuity(bars, d.interval)
            aux = self._aux(d, strat, bars)
        except (DataGapError, ValueError) as exc:
            self.log.write(
                "data_error", {"deployment": d.key}, {"error": str(exc)}, "skip"
            )
            self.kill.record_api_error(now)
            return
        sig = strat.signal(bars, d.params, aux)
        target = _sign(sig.iloc[-1])
        prev = _sign(sig.iloc[-2]) if len(sig) > 1 else 0.0
        state = self.build_state(self._call(self.venue.get_account), last_close)
        # One decision per bar, recorded BEFORE any order is submitted (a
        # failed read above leaves the bar retriable; nothing was placed).
        self._st.last_bar[d.key] = last_open.isoformat()
        self._save()
        pos = state.positions.get(d.symbol)
        cur = pos.qty if pos is not None else 0.0
        close = float(bars["close"].iloc[-1])
        self.log.write(
            "signal",
            {
                "deployment": d.key,
                "bar_open": last_open,
                "bar_close": last_close,
                "close": close,
                "position": cur,
            },
            {"target": target, "prev_target": prev},
            "decided",
        )
        held = _sign(cur)
        if held != 0 and target != held:
            exit_order = Order(
                symbol=d.symbol,
                side="sell" if held > 0 else "buy",
                qty=abs(cur),
                price_ref=close,
                stop_price=None,
                strategy=d.name,
                client_id=client_order_id(d.name, d.symbol, last_open, "exit"),
                reduce_only=True,
            )
            self.submit(exit_order, state, "exit")
            # Whatever the outcome, re-read before touching the stops.
            after = self._call(self.venue.get_account).positions.get(d.symbol)
            if after is not None and after.qty != 0:
                # Unfilled, unknown or partial: the existing stops stay; the
                # coverage check after this step verifies the residual.
                return
            self._call(self.venue.cancel_open_orders, d.symbol)
            held = 0.0
        if target == 0 or held != 0 or target == prev:
            return
        entry_inputs = {"deployment": d.key, "bar_open": last_open, "target": target}
        self._enter(d, bars, target, last_open, last_close, entry_inputs)

    def _enter(
        self,
        d: Deployment,
        bars: pd.DataFrame,
        side: float,
        bar_open: pd.Timestamp,
        bar_close: datetime,
        inputs: dict[str, Any],
    ) -> None:
        rules = self._call(self.venue.symbol_rules, d.symbol)
        close = float(bars["close"].iloc[-1])
        a = atr(bars, self.cfg.atr_window)
        if not (math.isfinite(a) and a > 0):
            self.log.write("entry", {**inputs, "atr": a}, {}, "no_atr")
            return
        stop = protective_stop(close, side, self.cfg.stop_atr_mult * a, rules.tick)
        state = self.build_state(self._call(self.venue.get_account), bar_close)
        qty = size_position(state.equity, close, stop, self.risk, rules=rules)
        sized = {
            **inputs,
            "atr": a,
            "close": close,
            "stop": stop,
            "equity": state.equity,
        }
        if not qty > 0 or not stop > 0:
            self.log.write("entry", sized, {"qty": qty}, "size_zero")
            return
        order = Order(
            symbol=d.symbol,
            side="buy" if side > 0 else "sell",
            qty=qty,
            price_ref=close,
            stop_price=stop,
            strategy=d.name,
            client_id=client_order_id(d.name, d.symbol, bar_open, "entry"),
            reduce_only=False,
        )
        self.log.write("entry", sized, {"qty": qty}, "sized")
        fill = self.submit(order, state, "entry")
        if fill is None:
            return  # unknown outcomes are protected by the coverage check
        self._protect(d, side, fill.qty, close, stop, bar_open, bar_close, rules)


def run_loop(
    venue: Venue,
    strategies: Iterable[Strategy] | Mapping[str, Strategy],
    deployed: list[Deployment],
    gate: RiskGate,
    kill: KillSwitch,
    log: DecisionLog,
    clock: Callable[[], datetime],
    *,
    runtime_cfg: RuntimeConfig,
    sleep: Callable[[float], None] = time.sleep,
    max_cycles: int | None = None,
    state_path: Path | None = None,
    init_state: bool = False,
) -> None:
    """Reconcile, then run cycles until the kill switch halts (or
    ``max_cycles``). Refuses (returns, logged) if already halted; raises
    ``StartupRefused`` if startup refuses."""
    if kill.halted:
        log.write("runtime", {}, {"reason": "kill switch halted"}, "refused_halted")
        return
    rt = Runtime(
        venue,
        strategies,
        deployed,
        gate,
        kill,
        log,
        clock,
        runtime_cfg=runtime_cfg,
        state_path=state_path,
        init_state=init_state,
    )
    rt.start()
    cycles = 0
    failed_in_a_row = 0
    while True:
        rt.backoff_s = 0.0
        try:
            rt.cycle()
            failed_in_a_row = 0
        except StartupRefused:
            raise
        except Exception as exc:
            # anything that escaped the cycle (a failed state save on a full
            # disk, a broken invariant, ...): log it, count it like an API
            # error, and trip the kill switch at the configured limit
            failed_in_a_row += 1
            log.write(
                "runtime_error",
                {"where": "cycle", "in_a_row": failed_in_a_row},
                {"error": repr(exc)},
                "error",
            )
            if isinstance(exc, RateLimited):
                rt.note_rate_limit(exc)
            if not kill.halted:
                kill.record_api_error(clock())
            if not kill.halted and failed_in_a_row >= kill.cfg.max_api_errors:
                kill.trigger("unexpected_error_limit")
        cycles += 1
        if kill.halted:
            log.write("runtime", {"cycles": cycles}, {}, "halted")
            return
        if max_cycles is not None and cycles >= max_cycles:
            log.write("runtime", {"cycles": cycles}, {}, "stopped")
            return
        wait = float(runtime_cfg.poll_interval_s)
        if rt.backoff_s > 0:
            wait = min(MAX_BACKOFF_S, max(wait, rt.backoff_s))
            log.write("runtime", {"backoff_s": wait}, {}, "rate_limit_backoff")
        sleep(wait)
