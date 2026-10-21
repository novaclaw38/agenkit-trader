import pandas as pd
import pytest

from agenkit_trader.data.align import align_asof
from agenkit_trader.data.archive import DataGapError, check_continuity


def hourly(n: int, start: str = "2024-01-01 05:00") -> pd.DataFrame:
    idx = pd.date_range(start, periods=n, freq="1h", tz="UTC", name="open_time")
    return pd.DataFrame({"close": range(n)}, index=idx, dtype="float64")


def test_check_continuity_ok() -> None:
    check_continuity(hourly(10), "1h")


def test_check_continuity_missing_bar() -> None:
    bars = hourly(10).drop(hourly(10).index[4])
    with pytest.raises(DataGapError, match="gap"):
        check_continuity(bars, "1h")


def test_check_continuity_duplicate_bar() -> None:
    bars = hourly(10)
    bars = pd.concat([bars, bars.iloc[[3]]]).sort_index()
    with pytest.raises(DataGapError, match="duplicate"):
        check_continuity(bars, "1h")


def test_check_continuity_non_monotonic() -> None:
    bars = hourly(10).iloc[::-1]
    with pytest.raises(DataGapError, match="monotonic"):
        check_continuity(bars, "1h")


def test_check_continuity_rejects_naive_and_empty() -> None:
    with pytest.raises(DataGapError):
        check_continuity(hourly(3).tz_localize(None), "1h")
    with pytest.raises(DataGapError):
        check_continuity(hourly(3).iloc[:0], "1h")


def test_funding_at_0800_not_visible_to_bar_closing_0759() -> None:
    bars = hourly(6)  # opens 05:00 .. 10:00
    funding = pd.Series([0.01], index=pd.DatetimeIndex(["2024-01-01 08:00"], tz="UTC"))
    out = align_asof(bars, funding, pd.Timedelta(0))
    # bar opened 07:00 closes 07:59:59.999 -> cannot know the 08:00 value
    assert pd.isna(out[pd.Timestamp("2024-01-01 07:00", tz="UTC")])
    # bar opened 08:00 closes 08:59:59.999 -> value is known
    assert out[pd.Timestamp("2024-01-01 08:00", tz="UTC")] == 0.01
    assert out[pd.Timestamp("2024-01-01 10:00", tz="UTC")] == 0.01
    assert out.index.equals(bars.index)


def test_available_lag_delays_visibility() -> None:
    bars = hourly(6)
    s = pd.Series([1.0], index=pd.DatetimeIndex(["2024-01-01 06:00"], tz="UTC"))
    out = align_asof(bars, s, pd.Timedelta(hours=2))  # usable from 08:00
    assert pd.isna(out[pd.Timestamp("2024-01-01 07:00", tz="UTC")])
    assert out[pd.Timestamp("2024-01-01 08:00", tz="UTC")] == 1.0


def test_backward_picks_latest_available() -> None:
    bars = hourly(6)
    s = pd.Series(
        [1.0, 2.0],
        index=pd.DatetimeIndex(["2024-01-01 05:00", "2024-01-01 07:00"], tz="UTC"),
    )
    out = align_asof(bars, s, pd.Timedelta(0))
    assert out[pd.Timestamp("2024-01-01 06:00", tz="UTC")] == 1.0
    assert out[pd.Timestamp("2024-01-01 07:00", tz="UTC")] == 2.0


def test_negative_lag_and_bad_series_rejected() -> None:
    bars = hourly(4)
    s = pd.Series([1.0], index=pd.DatetimeIndex(["2024-01-01 05:00"], tz="UTC"))
    with pytest.raises(ValueError):
        align_asof(bars, s, pd.Timedelta(hours=-1))
    dup = pd.concat([s, s])
    with pytest.raises(DataGapError):
        align_asof(bars, dup, pd.Timedelta(0))


def test_subsampled_bars_need_explicit_bar_length() -> None:
    bars = hourly(72).iloc[::24]  # 05:00 each day, spacing 24h
    s = pd.Series([1.0], index=pd.DatetimeIndex(["2024-01-01 20:00"], tz="UTC"))
    # uniform 24h spacing would infer 24h bars; that is only OK if explicit
    out = align_asof(bars, s, pd.Timedelta(0), bar_length=pd.Timedelta(hours=1))
    assert out.isna().iloc[0]  # 20:00 value invisible to the 05:00 1h bar
    assert out.iloc[1] == 1.0


def test_gappy_bars_without_bar_length_raise() -> None:
    bars = hourly(10).drop(hourly(10).index[4])
    s = pd.Series([1.0], index=pd.DatetimeIndex(["2024-01-01 05:00"], tz="UTC"))
    with pytest.raises(ValueError, match="uniformly"):
        align_asof(bars, s, pd.Timedelta(0))
    out = align_asof(bars, s, pd.Timedelta(0), bar_length=pd.Timedelta(hours=1))
    assert len(out) == 9


def test_irregular_subsample_raises_and_explicit_blocks_future() -> None:
    bars = hourly(72).iloc[[0, 1, 5, 30]]
    s = pd.Series([1.0], index=pd.DatetimeIndex(["2024-01-01 09:30"], tz="UTC"))
    with pytest.raises(ValueError):
        align_asof(bars, s, pd.Timedelta(0))
    out = align_asof(bars, s, pd.Timedelta(0), bar_length=pd.Timedelta(hours=1))
    # bar at 10:00 (index 5) closes 10:59 -> sees it; bars at 05:00/06:00 do not
    assert out.isna().tolist() == [True, True, False, False]
