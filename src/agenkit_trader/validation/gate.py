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
   NaN fails); win rate >= ``min_win_rate``; sweep stable; cost sensitivity: re-run at
   ``costs.scaled(cost_multiplier)`` and require net return > 0, profit factor
   > 1 and no ruin; deflated-Sharpe p-value (per-period Sharpe, cumulative
   trial count) <= ``max_trial_adjusted_pvalue``.
6. Regimes. ``label_regimes`` labels each bar *trend* (1.0) or *range* (0.0)
   with Kaufman's efficiency ratio over ``regime_window`` bars,
   ``ER_t = |c_t - c_{t-w}| / sum_{i=t-w+1..t} |c_i - c_{i-1}|``: the bar is
   *trend* when ``ER_t`` is above the expanding median of all ER values up to
   and including ``t`` (so only past and present information is used; NaN
   during warm-up). A bar's return is attributed to the regime known when its
   position was decided (the label of the previous bar). A regime "has edge"
   only with at least ``min_trades_per_regime`` hold-out trades in it,
   positive net return and positive Sharpe; excluded regimes are noted in
   ``flags``. If exactly one regime has edge, ``restricted_to`` names it and
   ``allowed_regimes`` contains only it: the runtime must stay out (flat) in
   the other. No regime with edge is a rejection.

Win rate is gated by ``min_win_rate``, a sanity floor (Sharpe and profit
factor are the binding checks).
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
    restricted_to: str | None
    flags: list[str]
    pvalue: float
    n_trials: int
    sweep: SweepResult
    ruined: bool


def label_regimes(bars: pd.DataFrame, window: int) -> pd.Series:
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


def allowed_regimes(
    regimes: dict[str, Stats], min_trades: int
) -> tuple[list[str], list[str]]:
    """``(allowed, notes)``: sorted regimes with an edge, and one note per
    excluded regime saying why. Edge = at least ``min_trades`` hold-out
    trades, net return > 0 and Sharpe > 0 (NaN fails)."""
    allowed: list[str] = []
    notes: list[str] = []
    for k in sorted(regimes):
        s = regimes[k]
        why = []
        if s.n_trades < min_trades:
            why.append(f"{s.n_trades} trades < min {min_trades}")
        if not s.total_return > 0:
            why.append(f"net return {s.total_return:.4f} <= 0")
        if not s.sharpe > 0:
            why.append(f"Sharpe {s.sharpe:.3f} <= 0")
        if why:
            notes.append(f"regime {k} excluded: " + ", ".join(why))
        else:
            allowed.append(k)
    return allowed, notes


def holdout_reasons(stats: Stats, cfg: GateConfig, *, ruined: bool) -> list[str]:
    """Rejection reasons from the hold-out statistics alone (NaN-safe:
    every comparison is written so that NaN fails)."""
    st = stats
    out: list[str] = []
    if ruined:
        out.append("hold-out backtest ruined (equity hit zero)")
    if st.n_trades == 0:
        out.append("hold-out has zero trades")
    elif st.n_trades < cfg.min_trades:
        out.append(f"hold-out trades {st.n_trades} < min {cfg.min_trades}")
    if not st.total_return > 0:
        out.append(
            f"hold-out net return {st.total_return:.4f} <= 0 (no positive expectancy)"
        )
    if not st.sharpe >= cfg.min_sharpe:
        out.append(
            f"hold-out Sharpe {st.sharpe:.3f} < min {cfg.min_sharpe} (annualised)"
        )
    if not st.max_drawdown <= cfg.max_drawdown:
        out.append(
            f"hold-out max drawdown {st.max_drawdown:.3f} > max {cfg.max_drawdown}"
        )
    if math.isnan(st.profit_factor) or not st.profit_factor >= cfg.min_profit_factor:
        out.append(
            f"hold-out profit factor {_fmt(st.profit_factor)} < min "
            f"{cfg.min_profit_factor}"
        )
    if not st.win_rate >= cfg.min_win_rate:
        out.append(f"hold-out win rate {st.win_rate:.3f} < min {cfg.min_win_rate}")
    return out


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

    reasons += holdout_reasons(st, gate_cfg, ruined=ho.ruined)
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

    regimes = _regime_stats(ho, label_regimes(full, gate_cfg.regime_window), ppy)
    allowed, notes = allowed_regimes(regimes, gate_cfg.min_trades_per_regime)
    flags: list[str] = []
    restricted_to: str | None = None
    if not allowed:
        reasons.append("no regime shows an edge on the hold-out: " + "; ".join(notes))
    else:
        flags += notes
        if len(allowed) == 1:
            restricted_to = allowed[0]
            flags.append(
                f"edge only in {restricted_to} regime: restricted to it "
                "(stay out otherwise)"
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
        restricted_to=restricted_to,
        flags=flags,
        pvalue=pvalue,
        n_trials=n_trials,
        sweep=sweep,
        ruined=ho.ruined,
    )
