import pandas as pd
import pytest

from agenkit_trader.data.lookahead import LookaheadError, assert_no_lookahead


def bars(n: int = 200) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    close = pd.Series(range(n), index=idx, dtype="float64") ** 1.1
    return pd.DataFrame({"close": close})


def test_future_peek_detected() -> None:
    with pytest.raises(LookaheadError):
        assert_no_lookahead(lambda b: b.close.shift(-1), bars())


def test_causal_signal_passes() -> None:
    assert_no_lookahead(lambda b: b.close.rolling(5).mean(), bars())


def test_full_sample_normalisation_detected() -> None:
    with pytest.raises(LookaheadError):
        assert_no_lookahead(lambda b: b.close / b.close.mean(), bars())


def test_sample_larger_than_bars_ok() -> None:
    assert_no_lookahead(lambda b: b.close.diff(), bars(20), sample=500)


def test_wrong_length_signal_rejected() -> None:
    with pytest.raises(LookaheadError):
        assert_no_lookahead(lambda b: b.close.iloc[:-1], bars())
