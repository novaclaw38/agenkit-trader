"""Kill switch: flatten everything and halt. Never routes through the RiskGate."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta

from agenkit_trader.config import RiskConfig
from agenkit_trader.execution.venue import Venue
from agenkit_trader.log import DecisionLog

RESUME_PHRASE = "RESUME"


class KillSwitch:
    def __init__(self, venue: Venue, log: DecisionLog, cfg: RiskConfig) -> None:
        self.venue = venue
        self.log = log
        self.cfg = cfg
        self.halted = False
        self._flattened = False
        self._errors: deque[datetime] = deque()

    def trigger(self, reason: str) -> None:
        """Flatten all positions then halt. Idempotent once a flatten succeeds.

        If flattening fails the switch is still halted (fail closed) and a
        later trigger retries the flatten.
        """
        if self._flattened:
            self.halted = True
            self.log.write(
                "kill_switch", {"reason": reason}, {"halted": True}, "already_halted"
            )
            return
        error: str | None = None
        try:
            self.venue.flatten_all()
            self._flattened = True
        except Exception as exc:
            error = repr(exc)
        finally:
            self.halted = True
        self.log.write(
            "kill_switch",
            {"reason": reason},
            {"halted": True, "flattened": self._flattened, "error": error},
            "flattened" if self._flattened else "flatten_failed",
        )

    def record_api_error(self, now: datetime) -> None:
        window = timedelta(seconds=self.cfg.error_window_s)
        self._errors.append(now)
        while self._errors and now - self._errors[0] >= window:
            self._errors.popleft()
        self.log.write(
            "api_error",
            {"now": now},
            {"count_in_window": len(self._errors)},
            "recorded",
        )
        if len(self._errors) >= self.cfg.max_api_errors:
            self.trigger("api_error_limit")

    def reset(self, confirm: str) -> None:
        if confirm != RESUME_PHRASE:
            self.log.write(
                "kill_switch_reset",
                {"confirm": confirm},
                {"halted": self.halted},
                "rejected",
            )
            raise ValueError(f"reset requires the exact phrase {RESUME_PHRASE!r}")
        self.halted = False
        self._flattened = False
        self._errors.clear()
        self.log.write("kill_switch_reset", {}, {"halted": False}, "reset")
