"""Parameter-neighbourhood sweep: is the best setting a plateau or a spike?

Score of a setting = annualised Sharpe (``Stats.sharpe``) of its backtest,
net of costs; a ruined backtest scores ``-inf``. Neighbours of a grid point
are the points that differ in exactly one parameter by one step in that
parameter's ``param_grid`` list. The sweep is ``stable`` only if the best
score is positive, the best point has at least one neighbour, and *every*
neighbour keeps at least ``(1 - tolerance)`` of the best score (i.e. no
neighbour loses more than ``tolerance`` of the performance). A grid with a
single setting can never be stable: it cannot show graceful degradation.

Callers must pass only the data the selection may see (the train segment).

Policy. ``policy="research"`` (default) backtests the signal with
``run_backtest`` (fraction of equity, no stops). ``policy="runtime"`` reduces
the signal to its sign and simulates what the paper runtime trades with
``run_backtest_runtime`` (1% risk at an ATR stop, intrabar exchange stop,
no re-entry without a transition), configured by ``runtime_policy``.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from agenkit_trader.backtest.costs import CostModel
from agenkit_trader.backtest.engine import BacktestResult, run_backtest
from agenkit_trader.backtest.runtime_policy import RuntimePolicy, run_backtest_runtime
from agenkit_trader.strategies.base import Strategy


@dataclass(frozen=True)
class SweepResult:
    """``table``: one row per grid point (parameter columns, ``sharpe``,
    ``total_return``, ``n_trades``, ``ruined``, ``score``)."""

    table: pd.DataFrame
    stable: bool
    best_params: dict[str, Any]
    best_score: float
    neighbour_scores: list[float]
    reason: str


def grid_points(grid: dict[str, list[Any]]) -> list[dict[str, Any]]:
    """Cartesian product of ``grid`` in key order (first key varies slowest)."""
    if not grid or any(len(v) == 0 for v in grid.values()):
        raise ValueError("param grid must have at least one value per parameter")
    keys = list(grid)
    return [
        dict(zip(keys, combo, strict=True))
        for combo in itertools.product(*grid.values())
    ]


def to_positions(sig: pd.Series, index: pd.DatetimeIndex) -> pd.Series:
    """Validate a strategy signal and return float positions (NaN -> 0)."""
    if not sig.index.equals(index):
        raise ValueError("signal index must equal bars index")
    vals = sig.astype("float64").fillna(0.0)
    if not vals.isin([-1.0, 0.0, 1.0]).all():
        raise ValueError("signal values must be in {-1, 0, 1}")
    return vals


POLICIES = ("research", "runtime")


def check_policy(policy: str) -> None:
    if policy not in POLICIES:
        raise ValueError(f"unknown policy {policy!r}; expected one of {POLICIES}")


def simulate(
    bars: pd.DataFrame,
    positions: pd.Series,
    costs: CostModel,
    funding: pd.Series | None,
    equity0: float,
    *,
    policy: str = "research",
    runtime_policy: RuntimePolicy | None = None,
    warmup: pd.DataFrame | None = None,
) -> BacktestResult:
    """Backtest ``positions`` under ``policy`` (see the module docstring).
    ``warmup`` (runtime policy only) is earlier history for the ATR."""
    check_policy(policy)
    if policy == "research":
        return run_backtest(bars, positions, costs, funding, equity0)
    rp = runtime_policy or RuntimePolicy()
    return run_backtest_runtime(
        bars,
        positions,
        costs,
        funding,
        equity0,
        rp.risk,
        rp.runtime,
        rp.rules,
        warmup=warmup,
    )


def backtest_params(
    strategy: Strategy,
    bars: pd.DataFrame,
    aux: dict[str, pd.Series],
    params: dict[str, Any],
    costs: CostModel,
    funding: pd.Series | None = None,
    equity0: float = 1000.0,
    *,
    policy: str = "research",
    runtime_policy: RuntimePolicy | None = None,
) -> BacktestResult:
    """Signal on exactly ``bars`` (aux restricted to its index) and backtest."""
    sub_aux = {k: v.reindex(bars.index) for k, v in aux.items()}
    pos = to_positions(
        strategy.signal(bars, params, sub_aux), pd.DatetimeIndex(bars.index)
    )
    return simulate(
        bars,
        pos,
        costs,
        funding,
        equity0,
        policy=policy,
        runtime_policy=runtime_policy,
    )


def score_of(res: BacktestResult) -> float:
    return -math.inf if res.ruined else float(res.stats.sharpe)


def _neighbours(i: int, grid: dict[str, list[Any]]) -> list[int]:
    sizes = [len(v) for v in grid.values()]
    coord = [int(c) for c in np.unravel_index(i, sizes)]
    out = []
    for d, size in enumerate(sizes):
        for step in (-1, 1):
            c = coord[d] + step
            if 0 <= c < size:
                nb = list(coord)
                nb[d] = c
                out.append(int(np.ravel_multi_index(nb, sizes)))
    return out


def neighbourhood_sweep(
    strategy: Strategy,
    bars: pd.DataFrame,
    costs: CostModel,
    grid: dict[str, list[Any]],
    *,
    aux: dict[str, pd.Series] | None = None,
    tolerance: float,
    funding: pd.Series | None = None,
    equity0: float = 1000.0,
    policy: str = "research",
    runtime_policy: RuntimePolicy | None = None,
) -> SweepResult:
    """Backtest every grid point on ``bars`` and judge the best's neighbourhood."""
    if not 0 < tolerance <= 1:
        raise ValueError("tolerance must be in (0, 1]")
    check_policy(policy)
    aux = aux or {}
    points = grid_points(grid)
    rows = []
    for p in points:
        res = backtest_params(
            strategy,
            bars,
            aux,
            p,
            costs,
            funding,
            equity0,
            policy=policy,
            runtime_policy=runtime_policy,
        )
        rows.append(
            {
                **p,
                "sharpe": res.stats.sharpe,
                "total_return": res.stats.total_return,
                "n_trades": res.stats.n_trades,
                "ruined": res.ruined,
                "score": score_of(res),
            }
        )
    table = pd.DataFrame(rows)
    scores = table["score"].to_numpy(dtype="float64")
    best = int(np.argmax(scores))
    best_score = float(scores[best])
    nbs = [float(scores[j]) for j in _neighbours(best, grid)]
    if not best_score > 0:
        stable, reason = False, f"best score {best_score:.3f} is not positive"
    elif not nbs:
        stable, reason = False, "single setting: no neighbours to show robustness"
    else:
        floor = (1.0 - tolerance) * best_score
        worst = min(nbs)
        stable = worst >= floor
        reason = (
            ""
            if stable
            else f"neighbour score {worst:.3f} < {floor:.3f} "
            f"({tolerance:.0%} below best {best_score:.3f})"
        )
    return SweepResult(table, stable, points[best], best_score, nbs, reason)
