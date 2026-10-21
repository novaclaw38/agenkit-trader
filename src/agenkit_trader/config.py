"""Typed configuration for the risk gate and the validation gate."""

from __future__ import annotations

import math
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

import yaml


def _require_finite(obj: object) -> None:
    for f in fields(obj):  # type: ignore[arg-type]
        v = getattr(obj, f.name)
        if not math.isfinite(v):
            raise ValueError(f"{f.name} must be finite, got {v!r}")


@dataclass(frozen=True)
class RiskConfig:
    start_equity: float = 1000.0
    max_risk_per_trade: float = 0.01
    max_daily_loss: float = 0.03
    max_drawdown: float = 0.10
    kelly_cap: float = 0.25
    max_leverage: float = 3.0
    max_symbol_notional_frac: float = 1.0
    stale_after_s: int = 120
    max_api_errors: int = 5
    error_window_s: int = 300

    def __post_init__(self) -> None:
        _require_finite(self)
        for name in (
            "start_equity",
            "max_risk_per_trade",
            "max_daily_loss",
            "max_drawdown",
            "max_leverage",
            "max_symbol_notional_frac",
            "stale_after_s",
            "max_api_errors",
            "error_window_s",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be > 0")
        if not 0 < self.kelly_cap <= 0.25:
            raise ValueError("kelly_cap must be in (0, 0.25]")
        for name in ("max_risk_per_trade", "max_daily_loss", "max_drawdown"):
            if getattr(self, name) > 1:
                raise ValueError(f"{name} must be <= 1")


@dataclass(frozen=True, kw_only=True)
class GateConfig:
    holdout_frac: float
    min_trades: int
    min_sharpe: float
    max_drawdown: float
    min_profit_factor: float
    cost_multiplier: float = 2.0
    max_trial_adjusted_pvalue: float
    # Max fractional performance loss of neighbouring parameter settings
    # relative to the best before a sweep is deemed unstable.
    neighbour_tolerance: float

    def __post_init__(self) -> None:
        _require_finite(self)
        if self.min_sharpe < 0:
            raise ValueError("min_sharpe must be >= 0")
        if not 0 < self.holdout_frac < 1:
            raise ValueError("holdout_frac must be in (0, 1)")
        if self.min_trades <= 0:
            raise ValueError("min_trades must be > 0")
        if not 0 < self.max_drawdown <= 1:
            raise ValueError("max_drawdown must be in (0, 1]")
        if self.min_profit_factor <= 0:
            raise ValueError("min_profit_factor must be > 0")
        if self.cost_multiplier < 1:
            raise ValueError("cost_multiplier must be >= 1")
        if not 0 < self.max_trial_adjusted_pvalue <= 1:
            raise ValueError("max_trial_adjusted_pvalue must be in (0, 1]")
        if not 0 < self.neighbour_tolerance <= 1:
            raise ValueError("neighbour_tolerance must be in (0, 1]")


def _load_mapping(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(Path(path).read_text())
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping at top level")
    return data


def _check_keys(cls: type, data: dict[str, Any], path: Path) -> None:
    allowed = {f.name for f in fields(cls)}
    unknown = set(data) - allowed
    if unknown:
        raise ValueError(f"{path}: unknown keys {sorted(unknown)}")


def load_risk_config(path: Path) -> RiskConfig:
    data = _load_mapping(path)
    _check_keys(RiskConfig, data, path)
    return RiskConfig(**data)


def load_gate_config(path: Path) -> GateConfig:
    data = _load_mapping(path)
    _check_keys(GateConfig, data, path)
    return GateConfig(**data)
