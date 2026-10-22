"""The validation gate: decides whether a strategy may be deployed.

Protocol (statistical honesty first):

1. ``split_by_time``: the last ``holdout_frac`` of history is the hold-out.
2. Every grid point is counted in the persistent ``TrialCounter`` *before*
   it is run, so the multiple-testing adjustment covers everything tried.
3. Parameters are chosen by ``neighbourhood_sweep`` on the TRAIN segment
   only (signals are computed on train bars alone; no hold-out data is seen).
4. The hold-out is evaluated exactly once, with the train-chosen setting.
   Signals there are computed on the full history (so indicators can warm up
   on train data) and the strategy is first checked with
   ``assert_no_lookahead``; any lookahead rejects it.
5. Checks on the hold-out (thresholds from ``GateConfig``, never tuned here):
   not ruined; trades >= ``min_trades`` (zero trades is its own reason); net
   return > 0; annualised Sharpe >= ``min_sharpe``; drawdown <=
   ``max_drawdown``; profit factor >= ``min_profit_factor`` (``inf`` passes,
   NaN fails); sweep stable; cost sensitivity: re-run at
   ``costs.scaled(cost_multiplier)`` and require net return > 0, profit factor
   > 1 and no ruin; deflated-Sharpe p-value (per-period Sharpe, cumulative
   trial count) <= ``max_trial_adjusted_pvalue``.
6. Regimes. ``label_regimes`` labels each bar *trend* (1.0) or *range* (0.0)
   with Kaufman's efficiency ratio over ``REGIME_WINDOW`` bars,
   ``ER_t = |c_t - c_{t-w}| / sum_{i=t-w+1..t} |c_i - c_{i-1}|``: the bar is
   *trend* when ``ER_t`` is above the expanding median of all ER values up to
   and including ``t`` (so only past and present information is used; NaN
   during warm-up). A bar's return is attributed to the regime known when its
   position was decided (the label of the previous bar). A regime "has edge"
   if the hold-out made trades in it with positive net return and positive
   Sharpe. If exactly one regime has edge, the verdict is flagged and
   ``allowed_regimes`` contains only that regime: the runtime must stay out
   (flat) in the other. No regime with edge is a rejection.

Win rate is reported but not gated: ``GateConfig`` defines no threshold for
it and this module does not invent one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from agenkit_trader.backtest.costs import CostModel
from agenkit_trader.backtest.engine import BacktestResult, run_backtest
from agenkit_trader.backtest.stats import Stats, compute_stats
from agenkit_trader.config import GateConfig
from agenkit_trader.data.lookahead import LookaheadError, assert_no_lookahead
from agenkit_trader.strategies.base import Strategy
from agenkit_trader.validation.split import split_by_time
from agenkit_trader.validation.sweep import (
    SweepResult,
    backtest_params,
    grid_points,
    neighbourhood_sweep,
    to_positions,
)
from agenkit_trader.validation.trials import TrialCounter, deflated_sharpe_pvalue

REGIME_WINDOW = 48
REGIMES = ("range", "trend")  # label 0.0, 1.0
_SECONDS_PER_YEAR = 365.0 * 24 * 3600


@dataclass(frozen=True)
class Verdict:
    """Outcome of ``evaluate``. ``reasons`` lists every failed check (empty
    iff ``passed``); ``flags`` are non-fatal notes such as a regime
    restriction. ``pvalue`` is the deflated-Sharpe p-value given
    ``n_trials`` (the cumulative count at evaluation time)."""

    passed: bool
    reasons: list[str]
    train: Stats
    holdout: Stats
    cost_2x: Stats
    regimes: dict[str, Stats]
    name: str
    edge: str
    symbol: str
    interval: str
    params: dict[str, Any]
    allowed_regimes: list[str]
    flags: list[str]
    pvalue: float
    n_trials: int
    sweep: SweepResult
    ruined: bool


def label_regimes(bars: pd.DataFrame, window: int = REGIME_WINDOW) -> pd.Series:
    """1.0 = trend, 0.0 = range, NaN = warm-up; causal (see module docs)."""
    c = bars["close"].astype("float64")
    path = c.diff().abs().rolling(window).sum()
    er = (c - c.shift(window)).abs() / path.where(path > 0)
    med = er.expanding(min_periods=window).median()
    lab = (er > med).astype("float64")
    return lab.where(er.notna() & med.notna())


def passes_cost_check(stats: Stats, *, ruined: bool) -> bool:
    """Still acceptable at inflated costs: positive return and PF > 1."""
    pf = stats.profit_factor
    return (not ruined) and stats.total_return > 0 and not math.isnan(pf) and pf > 1.0


def allowed_regimes(regimes: dict[str, Stats]) -> list[str]:
    """Sorted names of regimes in which the strategy showed an edge."""
    return sorted(
        k
        for k, s in regimes.items()
        if s.n_trades > 0 and s.total_return > 0 and s.sharpe > 0
    )


def _periods_per_year(bars: pd.DataFrame) -> float:
    step = pd.Timedelta(bars.index[1] - bars.index[0]).total_seconds()
    return _SECONDS_PER_YEAR / step


def _regime_stats(
    res: BacktestResult, labels: pd.Series, ppy: float
) -> dict[str, Stats]:
    eq = res.equity.to_numpy(dtype="float64")
    prev = eq[:-1]
    r = np.divide(eq[1:], prev, out=np.ones_like(prev), where=prev > 0) - 1.0
    # bar t's return comes from the position decided at close t-1
    decided = labels.shift(1).reindex(res.equity.index).to_numpy()[1:]
    entry_lab = labels.shift(1).reindex(res.trades["entry_time"]).to_numpy()
    out = {}
    for code, name in enumerate(REGIMES):
        mask = decided == float(code)
        e = pd.Series(np.r_[1.0, np.cumprod(1.0 + r[mask])])
        tr = res.trades[entry_lab == float(code)]
        out[name] = compute_stats(e, tr, ppy)
    return out


def _moments(r: np.ndarray) -> tuple[float, float, float]:
    """Per-period Sharpe (ddof=1), skewness, raw kurtosis of ``r``."""
    sd = float(r.std(ddof=1))
    if len(r) < 3 or sd <= 1e-12:
        return 0.0, 0.0, 3.0
    c = r - r.mean()
    m2 = float((c**2).mean())
    return (
        float(r.mean() / sd),
        float((c**3).mean() / m2**1.5),
        float((c**4).mean() / m2**2),
    )


def _fmt(x: float) -> str:
    return str(x) if not math.isfinite(x) else f"{x:.3f}"


def evaluate(
    strategy: Strategy,
    bars: pd.DataFrame,
    aux: dict[str, pd.Series],
    costs: CostModel,
    gate_cfg: GateConfig,
    counter: TrialCounter,
    *,
    symbol: str = "",
    interval: str = "",
    funding: pd.Series | None = None,
    equity0: float = 1000.0,
) -> Verdict:
    """Run the full validation protocol (module docstring) on one strategy."""
    train, hold = split_by_time(bars, gate_cfg.holdout_frac)
    full = pd.concat([train, hold])
    grid = strategy.param_grid
    n_trials = counter.add(len(grid_points(grid)))
    # the engine ignores funding outside each segment's span
    tr_fund = ho_fund = funding

    # --- selection on train only ---
    sweep = neighbourhood_sweep(
        strategy,
        train,
        costs,
        grid,
        aux=aux,
        tolerance=gate_cfg.neighbour_tolerance,
        funding=tr_fund,
        equity0=equity0,
    )
    params = sweep.best_params
    train_res = backtest_params(strategy, train, aux, params, costs, tr_fund, equity0)

    reasons: list[str] = []
    full_aux = {k: v.reindex(full.index) for k, v in aux.items()}

    def sig(b: pd.DataFrame) -> pd.Series:
        a = {k: v.reindex(b.index) for k, v in full_aux.items()}
        return to_positions(strategy.signal(b, params, a), pd.DatetimeIndex(b.index))

    try:
        assert_no_lookahead(sig, full, sample=20)
    except LookaheadError as e:
        reasons.append(f"lookahead detected: {e}")

    # --- the single hold-out evaluation ---
    pos = sig(full).loc[hold.index]
    ho = run_backtest(hold, pos, costs, ho_fund, equity0)
    c2 = run_backtest(
        hold, pos, costs.scaled(gate_cfg.cost_multiplier), ho_fund, equity0
    )
    st = ho.stats

    ppy = _periods_per_year(full)
    eq = ho.equity.to_numpy(dtype="float64")
    r = eq[1:] / np.where(eq[:-1] > 0, eq[:-1], 1.0) - 1.0
    sr_pp, skew, kurt = _moments(r)
    try:
        pvalue = deflated_sharpe_pvalue(sr_pp, n_trials, len(r), skew, kurt)
    except ValueError:
        pvalue = 1.0

    if ho.ruined:
        reasons.append("hold-out backtest ruined (equity hit zero)")
    if st.n_trades == 0:
        reasons.append("hold-out has zero trades")
    elif st.n_trades < gate_cfg.min_trades:
        reasons.append(f"hold-out trades {st.n_trades} < min {gate_cfg.min_trades}")
    if not st.total_return > 0:
        reasons.append(
            f"hold-out net return {st.total_return:.4f} <= 0 (no positive expectancy)"
        )
    if not st.sharpe >= gate_cfg.min_sharpe:
        reasons.append(
            f"hold-out Sharpe {st.sharpe:.3f} < min {gate_cfg.min_sharpe} (annualised)"
        )
    if not st.max_drawdown <= gate_cfg.max_drawdown:
        reasons.append(
            f"hold-out max drawdown {st.max_drawdown:.3f} > max {gate_cfg.max_drawdown}"
        )
    if (
        math.isnan(st.profit_factor)
        or not st.profit_factor >= gate_cfg.min_profit_factor
    ):
        reasons.append(
            f"hold-out profit factor {_fmt(st.profit_factor)} < min "
            f"{gate_cfg.min_profit_factor}"
        )
    if not sweep.stable:
        reasons.append(f"parameter sweep unstable: {sweep.reason}")
    if not passes_cost_check(c2.stats, ruined=c2.ruined):
        reasons.append(
            f"cost sensitivity: at {gate_cfg.cost_multiplier}x costs net return "
            f"{c2.stats.total_return:.4f}, profit factor {_fmt(c2.stats.profit_factor)}"
            f"{', ruined' if c2.ruined else ''} (need return > 0 and PF > 1)"
        )
    if not pvalue <= gate_cfg.max_trial_adjusted_pvalue:
        reasons.append(
            f"trial-adjusted p-value {pvalue:.4f} > max "
            f"{gate_cfg.max_trial_adjusted_pvalue} "
            f"(deflated Sharpe, n_trials={n_trials})"
        )

    regimes = _regime_stats(ho, label_regimes(full), ppy)
    allowed = allowed_regimes(regimes)
    flags: list[str] = []
    if not allowed:
        reasons.append("no regime shows an edge on the hold-out")
    elif len(allowed) < len(REGIMES):
        flags.append(
            f"edge only in {allowed[0]} regime: restricted to it (stay out otherwise)"
        )

    return Verdict(
        passed=not reasons,
        reasons=reasons,
        train=train_res.stats,
        holdout=st,
        cost_2x=c2.stats,
        regimes=regimes,
        name=strategy.name,
        edge=strategy.edge,
        symbol=symbol,
        interval=interval,
        params=dict(params),
        allowed_regimes=allowed,
        flags=flags,
        pvalue=pvalue,
        n_trials=n_trials,
        sweep=sweep,
        ruined=ho.ruined,
    )
