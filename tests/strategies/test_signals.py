"""Signal-level tests for the three research candidates (synthetic data only)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pytest

from agenkit_trader.data.lookahead import assert_no_lookahead
from agenkit_trader.strategies import REGISTRY
from agenkit_trader.strategies.base import Strategy
from agenkit_trader.strategies.funding_reversion import FundingReversion
from agenkit_trader.strategies.short_reversion import ShortReversion
from agenkit_trader.strategies.trend_breakout import TrendBreakout


def _bars(close: np.ndarray) -> pd.DataFrame:
    idx = pd.date_range("2022-01-01", periods=len(close), freq="1h", tz="UTC")
    c = pd.Series(close, index=idx, dtype="float64")
    o = c.shift(1).fillna(c.iloc[0])
    return pd.DataFrame(
        {
            "open": o,
            "high": np.maximum(o, c) * 1.001,
            "low": np.minimum(o, c) * 0.999,
            "close": c,
            "volume": 1.0,
        }
    )


def _random_bars(n: int = 1500, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    return _bars(100 * np.exp(np.cumsum(rng.normal(0, 0.01, n))))


def _random_aux(bars: pd.DataFrame, seed: int = 1) -> dict[str, pd.Series]:
    rng = np.random.default_rng(seed)
    f = rng.normal(0, 0.0006, len(bars) // 8 + 1).repeat(8)[: len(bars)]
    return {"funding": pd.Series(f, index=bars.index)}


STRATS: list[Strategy] = [FundingReversion(), TrendBreakout(), ShortReversion()]


def _first(grid: dict[str, list[Any]]) -> dict[str, Any]:
    return {k: v[0] for k, v in grid.items()}


def _all_params(s: Strategy) -> list[dict[str, Any]]:
    keys = list(s.param_grid)
    import itertools

    return [
        dict(zip(keys, vals, strict=True))
        for vals in itertools.product(*s.param_grid.values())
    ]


def test_registry_holds_all_three() -> None:
    assert set(REGISTRY) == {s.name for s in STRATS}
    for name, s in REGISTRY.items():
        assert isinstance(s, Strategy)
        assert s.name == name


@pytest.mark.parametrize("s", STRATS, ids=lambda s: s.name)
def test_grid_is_small_and_sorted(s: Strategy) -> None:
    assert 2 <= len(_all_params(s)) <= 12
    for vals in s.param_grid.values():
        assert vals == sorted(vals)


@pytest.mark.parametrize("s", STRATS, ids=lambda s: s.name)
def test_values_in_set_and_no_lookahead(s: Strategy) -> None:
    bars = _random_bars()
    aux = _random_aux(bars)
    for p in _all_params(s):
        out = s.signal(bars, p, aux)
        assert out.index.equals(bars.index)
        assert set(out.unique()) <= {-1.0, 0.0, 1.0}

        def fn(b: pd.DataFrame, p: dict[str, Any] = p) -> pd.Series:
            return s.signal(b, p, {k: v.reindex(b.index) for k, v in aux.items()})

        assert_no_lookahead(fn, bars, sample=15)


@pytest.mark.parametrize("s", STRATS, ids=lambda s: s.name)
def test_some_trading_happens(s: Strategy) -> None:
    bars = _random_bars(3000, seed=3)
    out = s.signal(bars, _first(s.param_grid), _random_aux(bars, 4))
    assert (out != 0).any()


@pytest.mark.parametrize("s", STRATS, ids=lambda s: s.name)
def test_zero_during_warmup(s: Strategy) -> None:
    bars = _random_bars()
    aux = _random_aux(bars)
    for p in _all_params(s):
        w = s.warmup(p)  # type: ignore[attr-defined]
        assert w >= 1
        out = s.signal(bars, p, aux)
        assert (out.iloc[:w] == 0).all()
    if isinstance(s, FundingReversion):
        return  # needs no price history; its warm-up is missing funding
    # extreme input during warm-up still flat
    short = _bars(np.r_[np.full(5, 100.0), np.full(5, 300.0)])
    a = {"funding": pd.Series(0.01, index=short.index)}
    assert (s.signal(short, _first(s.param_grid), a) == 0).all()


def test_funding_above_threshold_gives_short_below_gives_long() -> None:
    s = FundingReversion()
    bars = _bars(np.full(100, 100.0))
    thr = s.param_grid["entry"][0]
    p = _first(s.param_grid)
    f = pd.Series(0.0001, index=bars.index)
    f.iloc[50:] = thr * 2
    out = s.signal(bars, p, {"funding": f})
    assert out.iloc[49] == 0
    assert out.iloc[50] == -1
    f2 = pd.Series(0.0001, index=bars.index)
    f2.iloc[50:] = -thr * 2
    assert s.signal(bars, p, {"funding": f2}).iloc[50] == 1


def test_funding_exits_on_normalisation_and_max_hold() -> None:
    s = FundingReversion()
    p = _first(s.param_grid)
    bars = _bars(np.full(400, 100.0))
    f = pd.Series(0.0, index=bars.index)
    f.iloc[50:60] = p["entry"] * 2
    out = s.signal(bars, p, {"funding": f})
    assert out.iloc[59] == -1 and out.iloc[60] == 0
    f.iloc[50:] = p["entry"] * 2  # never normalises -> time exit
    out = s.signal(bars, p, {"funding": f})
    assert out.iloc[50 + p["max_hold"] - 1] == -1
    assert out.iloc[50 + p["max_hold"]] == 0


def test_funding_stop_on_adverse_close() -> None:
    s = FundingReversion()
    p = _first(s.param_grid)
    close = np.full(100, 100.0)
    close[55:] = 100 * (1 + s.stop_pct + 0.01)  # rally against the short
    bars = _bars(close)
    f = pd.Series(0.0, index=bars.index)
    f.iloc[50:] = p["entry"] * 2
    out = s.signal(bars, p, {"funding": f})
    assert out.iloc[54] == -1 and out.iloc[55] == 0


def test_funding_missing_is_flat() -> None:
    s = FundingReversion()
    bars = _bars(np.full(100, 100.0))
    out = s.signal(bars, _first(s.param_grid), {})
    assert (out == 0).all()


def test_breakout_goes_long_on_new_high_and_short_on_new_low() -> None:
    s = TrendBreakout()
    p = {"lookback": 48, "vol_cap": 2.0}
    rng = np.random.default_rng(5)
    base = 100 * np.exp(np.cumsum(rng.normal(0, 0.002, 800)))
    up = base.copy()
    up[-1] = base[-200:].max() * 1.03
    assert s.signal(_bars(up), p, {}).iloc[-1] == 1
    dn = base.copy()
    dn[-1] = base[-200:].min() * 0.97
    assert s.signal(_bars(dn), p, {}).iloc[-1] == -1


def test_breakout_vol_filter_blocks_entry_in_high_vol() -> None:
    s = TrendBreakout()
    p = {"lookback": 48, "vol_cap": 1.0}
    rng = np.random.default_rng(6)
    r = rng.normal(0, 0.002, 800)
    r[-24:-1] = rng.normal(-0.01, 0.03, 23)  # recent vol >> long-run vol
    c = 100 * np.exp(np.cumsum(r))
    c[-1] = c[:-1].max() * 1.05
    assert s.signal(_bars(c), p, {}).iloc[-1] == 0
    p2 = {"lookback": 48, "vol_cap": 1e9}  # same bars, filter off -> long
    assert s.signal(_bars(c), p2, {}).iloc[-1] == 1


def test_reversion_buys_crash_in_range_and_not_in_trend() -> None:
    s = ShortReversion()
    p = {"z_entry": 2.0, "er_max": 0.4}
    rng = np.random.default_rng(7)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.003, 800) - 0.0))
    # flatten the recent path into a range, then crash
    c[-60:-6] = c[-61] * (1 + 0.002 * np.sin(np.arange(54)))
    crash = c.copy()
    crash[-6:] = c[-7] * np.linspace(0.997, 0.98, 6)
    assert s.signal(_bars(crash), p, {}).iloc[-1] == 1
    pop = c.copy()
    pop[-6:] = c[-7] * np.linspace(1.003, 1.02, 6)
    assert s.signal(_bars(pop), p, {}).iloc[-1] == -1
    trend = 100 * np.exp(np.linspace(0, 0.8, 800))  # clean trend, ER ~ 1
    trend[-1] = trend[-2] * 0.9
    assert s.signal(_bars(trend), p, {}).iloc[-1] == 0
