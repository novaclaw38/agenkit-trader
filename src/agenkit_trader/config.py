"""Typed configuration for the risk gate, the validation gate and the runtime."""

from __future__ import annotations

import math
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    """Config rejected. The message names only our own field names and value
    TYPES, never a value or an unknown key from the file (it could be a
    secret pasted by mistake)."""


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
    # Sanity floor on hold-out win rate (Sharpe and profit factor bind).
    min_win_rate: float
    # A regime counts as having an edge only with at least this many trades.
    min_trades_per_regime: int
    # Look-back (bars) of the efficiency-ratio regime labeller.
    regime_window: int

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
        if not 0 <= self.min_win_rate <= 1:
            raise ValueError("min_win_rate must be in [0, 1]")
        for name in ("min_trades_per_regime", "regime_window"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
                raise ValueError(f"{name} must be a positive int, got {v!r}")


@dataclass(frozen=True)
class RuntimeConfig:
    """Paper/testnet runtime settings (``config/runtime.yaml``). No secrets:
    API keys only ever come from environment variables."""

    # Protective (disaster) stop: entry -/+ stop_atr_mult * ATR(atr_window),
    # placed on the exchange as a reduce-only STOP_MARKET after every entry.
    stop_atr_mult: float = 3.0
    atr_window: int = 14
    # Closed-bar history fetched per cycle. One extra (forming) bar is
    # requested, and Binance returns at most 1500 klines, so the cap is 1499.
    bars_limit: int = 1000
    poll_interval_s: float = 30.0
    recv_window_ms: int = 5000
    # Restart reconciliation: a position or open order on a symbol that no
    # deployed strategy owns. False: refuse to start and tell the user.
    # True: trip the kill switch (flatten everything, halt).
    flatten_unowned_on_start: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.flatten_unowned_on_start, bool):
            raise ValueError("flatten_unowned_on_start must be true or false")
        for name in ("stop_atr_mult", "poll_interval_s"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, int | float):
                raise ValueError(f"{name} must be a number, got {v!r}")
            if not math.isfinite(v) or v <= 0:
                raise ValueError(f"{name} must be finite and > 0, got {v!r}")
        for name, hi in (
            ("atr_window", None),
            ("bars_limit", 1499),
            ("recv_window_ms", 60_000),
        ):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
                raise ValueError(f"{name} must be a positive int, got {v!r}")
            if hi is not None and v > hi:
                raise ValueError(f"{name} must be <= {hi}, got {v!r}")
        if self.bars_limit <= self.atr_window + 1:
            raise ValueError("bars_limit must exceed atr_window + 1")


@dataclass(frozen=True)
class LiveConfig:
    """Live-trading gate settings (``config/live.yaml``). Only the human edits
    that file; no code in this package writes it. ``enabled`` must be a real
    YAML boolean (a quoted string is rejected) and defaults to false."""

    enabled: bool = False
    # Minimum paper (testnet) record before ``agenkit live`` accepts an
    # unlock, measured from the decision log. PROPOSALS, user to confirm.
    min_paper_days: float = 14.0
    min_paper_trades: int = 30

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ConfigError(
                "enabled must be the boolean true or false, got a value of type "
                f"{type(self.enabled).__name__}"
            )
        v = self.min_paper_days
        if isinstance(v, bool) or not isinstance(v, int | float):
            raise ConfigError(
                f"min_paper_days must be a number, got type {type(v).__name__}"
            )
        if not math.isfinite(v) or v <= 0:
            raise ConfigError("min_paper_days must be finite and > 0")
        t = self.min_paper_trades
        if isinstance(t, bool) or not isinstance(t, int) or t <= 0:
            raise ConfigError(
                "min_paper_trades must be a positive int, got a value of type "
                f"{type(t).__name__}"
            )


_KIND = {
    "RiskConfig": "risk config (e.g. config/risk.yaml)",
    "GateConfig": "gate config (e.g. config/gate.yaml)",
    "RuntimeConfig": "runtime config (e.g. config/runtime.yaml)",
    "LiveConfig": "live config (e.g. config/live.yaml)",
}


def _load_mapping(path: Path, cls: type | None = None) -> dict[str, Any]:
    kind = _KIND.get(cls.__name__, "config") if cls is not None else "config"
    if Path(path).is_dir():
        raise ConfigError(f"{path}: is a directory; expected a {kind} file")
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
        # count only: an unknown key could be a secret pasted by mistake
        kind = _KIND.get(cls.__name__, "config")
        raise ConfigError(
            f"{path}: {len(unknown)} unknown keys (names not shown); expected a "
            f"{kind} with keys: {', '.join(sorted(allowed))}"
        )


def load_risk_config(path: Path) -> RiskConfig:
    data = _load_mapping(path, RiskConfig)
    _check_keys(RiskConfig, data, path)
    return RiskConfig(**data)


def load_gate_config(path: Path) -> GateConfig:
    data = _load_mapping(path, GateConfig)
    _check_keys(GateConfig, data, path)
    return GateConfig(**data)


def load_runtime_config(path: Path) -> RuntimeConfig:
    data = _load_mapping(path, RuntimeConfig)
    _check_keys(RuntimeConfig, data, path)
    return RuntimeConfig(**data)


def load_live_config(path: Path) -> LiveConfig:
    data = _load_mapping(path, LiveConfig)
    _check_keys(LiveConfig, data, path)
    return LiveConfig(**data)
