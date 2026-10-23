"""Synthetic markets and strategies for validation-harness tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from agenkit_trader.backtest.costs import CostModel
from agenkit_trader.config import GateConfig, load_gate_config

GATE_YAML = Path(__file__).resolve().parents[2] / "config" / "gate.yaml"


def gate_cfg() -> GateConfig:
    """Thresholds come from the committed config, never from the tests."""
    return load_gate_config(GATE_YAML)


def costs(per_side_bps: float = 5.0) -> CostModel:
    # 4 bps taker + 0.5 half-spread + rest slippage; 0 means frictionless
    if per_side_bps == 0:
        return CostModel(0.0, 0.0, 0.0, 0.0)
    return CostModel(
        taker_fee=0.0004,
        maker_fee=0.0002,
        half_spread_bps=0.5,
        slippage_bps=per_side_bps - 4.5,
    )


def market(
    seed: int, n: int = 3000, k: float = 0.003, vol: float = 0.01, n_noise: int = 0
) -> tuple[pd.DataFrame, dict[str, pd.Series]]:
    """Hourly bars whose next return is ``k * f_t + vol * eps``.

    ``f`` is a feature known at the close of bar ``t`` (aux["f"]); with
    ``k = 0`` the prices are a pure random walk and ``f`` is useless. Extra
    independent features ``n1..n{n_noise}`` carry no information.
    """
    rng = np.random.default_rng(seed)
    f = rng.standard_normal(n)
    eps = rng.standard_normal(n)
    r = np.r_[0.0, k * f[:-1] + vol * eps[1:]]
    close = 100.0 * np.cumprod(1.0 + r)
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    bars = pd.DataFrame(
        {"open": close, "high": close, "low": close, "close": close, "volume": 1.0},
        index=idx,
    )
    aux = {"f": pd.Series(f, index=idx)}
    for j in range(1, n_noise + 1):
        aux[f"n{j}"] = pd.Series(rng.standard_normal(n), index=idx)
    return bars, aux


def _sign_above(x: pd.Series, thr: float) -> pd.Series:
    return pd.Series(np.where(x.abs() > thr, np.sign(x), 0.0), index=x.index)


@dataclass
class FeatureStrategy:
    """Trade the sign of aux['f'] when it is larger than ``thr``."""

    name: str = "feature"
    edge: str = "planted predictive feature"
    param_grid: dict[str, list[Any]] = field(
        default_factory=lambda: {"thr": [0.0, 0.25, 0.5, 0.75, 1.0]}
    )

    def signal(
        self, bars: pd.DataFrame, params: dict[str, Any], aux: dict[str, pd.Series]
    ) -> pd.Series:
        return _sign_above(aux["f"], float(params["thr"]))


@dataclass
class SpikeStrategy:
    """Only k == 3 uses the informative feature; neighbours use noise."""

    name: str = "spike"
    edge: str = "one lucky setting"
    param_grid: dict[str, list[Any]] = field(
        default_factory=lambda: {"k": [1, 2, 3, 4, 5]}
    )

    def signal(
        self, bars: pd.DataFrame, params: dict[str, Any], aux: dict[str, pd.Series]
    ) -> pd.Series:
        key = "f" if params["k"] == 3 else f"n{params['k']}"
        return _sign_above(aux[key], 0.0)


@dataclass
class TrainOnlyStrategy:
    """Trades the feature before ``cutoff`` and is flat afterwards."""

    cutoff: pd.Timestamp
    name: str = "train_only"
    edge: str = "goes quiet"
    param_grid: dict[str, list[Any]] = field(
        default_factory=lambda: {"thr": [0.0, 0.25, 0.5]}
    )

    def signal(
        self, bars: pd.DataFrame, params: dict[str, Any], aux: dict[str, pd.Series]
    ) -> pd.Series:
        s = _sign_above(aux["f"], float(params["thr"]))
        s[s.index >= self.cutoff] = 0.0
        return s


@dataclass
class PeekingStrategy:
    """Cheats: uses next bar's return."""

    name: str = "peek"
    edge: str = "lookahead"
    param_grid: dict[str, list[Any]] = field(
        default_factory=lambda: {"thr": [0.0, 0.0005, 0.001]}
    )

    def signal(
        self, bars: pd.DataFrame, params: dict[str, Any], aux: dict[str, pd.Series]
    ) -> pd.Series:
        nxt = bars["close"].pct_change().shift(-1)
        return _sign_above(nxt, float(params["thr"]))
