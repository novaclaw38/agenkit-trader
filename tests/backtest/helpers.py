import numpy as np
import pandas as pd


def make_bars(closes: list[float], freq: str = "1h") -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=len(closes), freq=freq, tz="UTC")
    c = np.asarray(closes, dtype="float64")
    return pd.DataFrame(
        {"open": c, "high": c, "low": c, "close": c, "volume": 1.0}, index=idx
    )
