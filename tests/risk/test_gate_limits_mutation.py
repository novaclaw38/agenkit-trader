"""Each portfolio limit in RiskGate, isolated: the order breaches ONLY that
limit (so removing the check lets it through, and inverting it rejects the
in-limit twin). Verified by mutating risk/gate.py locally; see the final fix
report."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from agenkit_trader.config import RiskConfig
from agenkit_trader.execution.venue import AccountState, Order, Position, SymbolRules
from agenkit_trader.risk.gate import Reason, RiskGate

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
RULES = SymbolRules(tick=0.1, lot=0.001, min_notional=5.0)


def _check(
    *,
    equity: float = 1000.0,
    day_start: float = 1000.0,
    peak: float = 1000.0,
    qty: float = 0.002,
    stop: float = 49_000.0,
    positions: dict[str, Position] | None = None,
    cfg: RiskConfig | None = None,
) -> tuple[bool, str]:
    o = Order("BTCUSDT", "buy", qty, 50_000.0, stop, "t")
    s = AccountState(
        equity=equity,
        positions=positions or {},
        day_start_equity=day_start,
        peak_equity=peak,
        last_bar_time=NOW - timedelta(seconds=30),
    )
    d = RiskGate(cfg or RiskConfig(), lambda sym: RULES).check(o, s, NOW)
    return d.allowed, d.reason


def test_baseline_is_allowed() -> None:
    assert _check() == (True, Reason.OK.value)


@pytest.mark.parametrize(
    ("kw_breach", "kw_ok", "reason"),
    [
        # daily loss 3.1% >= 3% (drawdown 3.1% < 10%) vs 2.5%
        ({"equity": 969.0}, {"equity": 975.0}, Reason.DAILY_LOSS),
        # drawdown 10.1% >= 10% with no daily loss vs 9.5%
        (
            {"equity": 899.0, "day_start": 899.0},
            {"equity": 905.0, "day_start": 905.0},
            Reason.DRAWDOWN,
        ),
        # leverage: 2950 other + 100 new = 3.05x > 3x vs 2.95x
        (
            {"positions": {"ETHUSDT": Position("ETHUSDT", 1.0, 2950.0)}},
            {"positions": {"ETHUSDT": Position("ETHUSDT", 1.0, 2850.0)}},
            Reason.LEVERAGE,
        ),
        # symbol notional: 550 > 0.5 x 1000 vs 450 (risk 5.5 / 4.5 < 10)
        (
            {
                "qty": 0.011,
                "stop": 49_500.0,
                "cfg": replace(RiskConfig(), max_symbol_notional_frac=0.5),
            },
            {
                "qty": 0.009,
                "stop": 49_500.0,
                "cfg": replace(RiskConfig(), max_symbol_notional_frac=0.5),
            },
            Reason.SYMBOL_NOTIONAL,
        ),
    ],
    ids=["daily_loss", "drawdown", "leverage", "symbol_notional"],
)
def test_limit_breach_rejected_and_in_limit_twin_allowed(
    kw_breach: dict[str, object], kw_ok: dict[str, object], reason: Reason
) -> None:
    assert _check(**kw_breach) == (False, reason.value)  # type: ignore[arg-type]
    assert _check(**kw_ok) == (True, Reason.OK.value)  # type: ignore[arg-type]
