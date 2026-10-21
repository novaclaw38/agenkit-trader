from pathlib import Path

import pytest

from agenkit_trader.config import (
    GateConfig,
    RiskConfig,
    load_gate_config,
    load_risk_config,
)

ROOT = Path(__file__).resolve().parent.parent


def test_risk_defaults_match_spec() -> None:
    c = RiskConfig()
    assert c.start_equity == 1000.0
    assert c.max_risk_per_trade == 0.01
    assert c.max_daily_loss == 0.03
    assert c.max_drawdown == 0.10
    assert c.kelly_cap == 0.25
    assert c.max_leverage == 3.0
    assert c.max_symbol_notional_frac == 1.0
    assert c.stale_after_s == 120
    assert c.max_api_errors == 5
    assert c.error_window_s == 300


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_daily_loss": 0.0},
        {"max_daily_loss": -0.01},
        {"kelly_cap": 0.26},
        {"kelly_cap": 0.0},
        {"kelly_cap": -0.1},
    ],
)
def test_rejects_nonpositive_limits(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        RiskConfig(**kwargs)


def test_risk_config_is_frozen() -> None:
    with pytest.raises(AttributeError):
        RiskConfig().kelly_cap = 0.1  # type: ignore[misc]


def test_load_risk_config_file() -> None:
    assert load_risk_config(ROOT / "config" / "risk.yaml") == RiskConfig()


def test_load_gate_config_file() -> None:
    g = load_gate_config(ROOT / "config" / "gate.yaml")
    assert isinstance(g, GateConfig)
    assert g.cost_multiplier == 2.0
    assert 0 < g.holdout_frac < 1
    assert 0 < g.neighbour_tolerance <= 1
    assert 0 < g.max_trial_adjusted_pvalue <= 0.05


def test_gate_rejects_missing_key(tmp_path: Path) -> None:
    p = tmp_path / "g.yaml"
    p.write_text("holdout_frac: 0.3\n")
    with pytest.raises((ValueError, TypeError)):
        load_gate_config(p)


def test_gate_rejects_bad_values() -> None:
    with pytest.raises(ValueError):
        GateConfig(
            holdout_frac=0.0,
            min_trades=30,
            min_sharpe=0.5,
            max_drawdown=0.25,
            min_profit_factor=1.2,
            max_trial_adjusted_pvalue=0.05,
            neighbour_tolerance=0.5,
        )
