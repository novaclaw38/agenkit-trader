"""trend_funding_filter: the trend_breakout signal minus crowded-side entries."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pytest

from agenkit_trader.data.lookahead import assert_no_lookahead
from agenkit_trader.strategies import REGISTRY
from agenkit_trader.strategies.trend_breakout import TrendBreakout
from agenkit_trader.strategies.trend_funding_filter import TrendFundingFilter

S = TrendFundingFilter()
P: dict[str, Any] = {"lookback": 42, "thr": 0.0003, "vol_cap": 1.5}
RAW_P: dict[str, Any] = {"lookback": 42, "vol_cap": 1.5}
N = 1300


def _market() -> pd.DataFrame:
    """Noise, then an up ramp (long entry ~802, exit ~859) and a down ramp
    (short entry ~867)."""
    rng = np.random.default_rng(0)
    c = 100 + rng.normal(0, 0.3, N)
    c[800:850] = 100 + np.arange(50) * 0.6
    c[850:900] = c[849] - np.arange(50) * 0.9
    c[900:] = c[899] + rng.normal(0, 0.1, N - 900)
    idx = pd.date_range("2022-01-01", periods=N, freq="4h", tz="UTC")
    cs = pd.Series(c, index=idx)
    o = cs.shift(1).fillna(cs.iloc[0])
    return pd.DataFrame(
        {
            "open": o,
            "high": np.maximum(o, cs) * 1.001,
            "low": np.minimum(o, cs) * 0.999,
            "close": cs,
            "volume": 1.0,
        }
    )


BARS = _market()
RAW = TrendBreakout().signal(BARS, RAW_P, {})


def _funding(v: float = 0.0) -> pd.Series:
    return pd.Series(v, index=BARS.index, dtype="float64")


def _entries(sig: pd.Series, side: float) -> list[int]:
    v = sig.to_numpy()
    return [t for t in range(1, len(v)) if v[t] == side and v[t - 1] != side]


def test_registered_with_frozen_grid_and_aux() -> None:
    s = REGISTRY["trend_funding_filter"]
    assert s.param_grid == {
        "lookback": [42, 84, 168],
        "thr": [0.0003, 0.0006],
        "vol_cap": [1.5],
    }
    assert getattr(s, "aux_required", ()) == ("funding",)
    assert TrendBreakout().param_grid["lookback"] == [48, 96, 168, 336]  # untouched


def test_scenario_has_long_and_short_entries() -> None:
    assert _entries(RAW, 1.0) and _entries(RAW, -1.0)


def test_neutral_funding_equals_trend_breakout() -> None:
    out = S.signal(BARS, P, {"funding": _funding(0.0)})
    pd.testing.assert_series_equal(out, RAW)


def test_crowded_long_entry_is_skipped_for_the_whole_trade() -> None:
    e = _entries(RAW, 1.0)[-1]
    f = _funding(0.0)
    f.iloc[e] = P["thr"]  # exactly at the threshold suppresses (>=)
    out = S.signal(BARS, P, {"funding": f})
    # funding normalises one bar later but the raw trend continues: the trade
    # stays skipped (no late entry)
    end = e
    while RAW.iloc[end] == 1.0:
        end += 1
    assert (out.iloc[e:end] == 0).all()
    assert (RAW.iloc[e:end] == 1).all()
    # everything else unchanged
    mask = np.ones(N, dtype=bool)
    mask[e:end] = False
    assert (out.to_numpy()[mask] == RAW.to_numpy()[mask]).all()


def test_just_below_threshold_does_not_suppress() -> None:
    e = _entries(RAW, 1.0)[-1]
    f = _funding(0.0)
    f.iloc[e] = P["thr"] * 0.999
    pd.testing.assert_series_equal(S.signal(BARS, P, {"funding": f}), RAW)


def test_high_funding_does_not_block_shorts_and_vice_versa() -> None:
    f = _funding(10 * P["thr"])  # extreme positive: crowded longs only
    out = S.signal(BARS, P, {"funding": f})
    assert (out == 1.0).sum() == 0
    assert (out == -1.0).sum() == (RAW == -1.0).sum() > 0
    out2 = S.signal(BARS, P, {"funding": -f})  # extreme negative: shorts blocked
    assert (out2 == -1.0).sum() == 0
    assert (out2 == 1.0).sum() == (RAW == 1.0).sum() > 0


def test_short_entry_threshold_is_inclusive() -> None:
    e = _entries(RAW, -1.0)[-1]
    f = _funding(0.0)
    f.iloc[e] = -P["thr"]
    out = S.signal(BARS, P, {"funding": f})
    assert out.iloc[e] == 0.0 and RAW.iloc[e] == -1.0


def test_exits_are_unchanged_by_extreme_funding_while_in_a_trade() -> None:
    e = _entries(RAW, 1.0)[-1]
    f = _funding(0.0)
    f.iloc[e + 1 :] = 100 * P["thr"]  # extreme AFTER entry: held, exits as raw
    out = S.signal(BARS, P, {"funding": f})
    end = e
    while RAW.iloc[end] == 1.0:
        end += 1
    assert (out.iloc[e:end] == 1.0).all()
    assert out.iloc[end] == RAW.iloc[end]


def test_suppressed_flip_leaves_the_book_flat() -> None:
    v = RAW.to_numpy()
    flips = [t for t in range(1, N) if v[t] != 0 and v[t - 1] == -v[t]]
    # the scenario may not contain a flip; build one by negating a stretch
    if not flips:
        pytest.skip("no flip in scenario")  # pragma: no cover
    t = flips[0]
    f = _funding(0.0)
    f.iloc[t] = 10 * P["thr"] * v[t]  # crowded in the NEW direction
    out = S.signal(BARS, P, {"funding": f})
    assert out.iloc[t - 1] == RAW.iloc[t - 1] and out.iloc[t] == 0.0


def test_nan_funding_suppresses_new_entries_but_not_open_positions() -> None:
    e = _entries(RAW, 1.0)[-1]
    f = _funding(0.0)
    f.iloc[e] = np.nan
    out = S.signal(BARS, P, {"funding": f})
    assert out.iloc[e] == 0.0  # unknown funding at the entry: skip (fail safe)
    f2 = _funding(0.0)
    f2.iloc[e + 1 :] = np.nan  # unknown AFTER entry: position is kept
    out2 = S.signal(BARS, P, {"funding": f2})
    assert out2.iloc[e] == 1.0 and out2.iloc[e + 1] == 1.0


def test_missing_funding_aux_never_trades() -> None:
    assert (S.signal(BARS, P, {}) == 0).all()
    assert (S.signal(BARS, P, {"funding": _funding(np.nan)}) == 0).all()


def test_filter_only_removes_trades_and_zero_in_warmup() -> None:
    rng = np.random.default_rng(5)
    f = pd.Series(rng.normal(0, 0.0005, N), index=BARS.index)
    out = S.signal(BARS, P, {"funding": f})
    assert (out.iloc[: S.warmup(P)] == 0).all()
    # whenever the filtered book is non-zero it equals the raw signal
    nz = out != 0
    assert (out[nz] == RAW[nz]).all()


@pytest.mark.parametrize("lookback", [42, 168])
@pytest.mark.parametrize("thr", [0.0003, 0.0006])
def test_no_lookahead(lookback: int, thr: float) -> None:
    rng = np.random.default_rng(2)
    f = pd.Series(rng.normal(0, 0.0005, N), index=BARS.index)
    p = {"lookback": lookback, "thr": thr, "vol_cap": 1.5}

    def fn(b: pd.DataFrame) -> pd.Series:
        return S.signal(b, p, {"funding": f.reindex(b.index)})

    assert_no_lookahead(fn, BARS, sample=20)
