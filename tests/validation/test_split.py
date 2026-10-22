import pandas as pd
import pytest
from vsynth import market

from agenkit_trader.validation.split import split_by_time


def test_chronological_split_no_overlap() -> None:
    bars, _ = market(0, n=1000)
    shuffled = bars.sample(frac=1.0, random_state=1)
    train, hold = split_by_time(shuffled, 0.3)
    assert len(hold) == 300 and len(train) == 700
    assert train.index.max() < hold.index.min()
    assert train.index.intersection(hold.index).empty
    pd.testing.assert_frame_equal(pd.concat([train, hold]), bars, check_freq=False)


@pytest.mark.parametrize("frac", [0.0, 1.0, -0.1, float("nan")])
def test_bad_fraction(frac: float) -> None:
    bars, _ = market(0, n=100)
    with pytest.raises(ValueError):
        split_by_time(bars, frac)


def test_too_short() -> None:
    bars, _ = market(0, n=3)
    with pytest.raises(ValueError):
        split_by_time(bars, 0.3)


def test_duplicate_timestamps_rejected() -> None:
    bars, _ = market(0, n=100)
    with pytest.raises(ValueError):
        split_by_time(pd.concat([bars, bars.iloc[:1]]), 0.3)
