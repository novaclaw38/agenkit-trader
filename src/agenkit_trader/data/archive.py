"""Binance USD-M futures public bulk archive (data.binance.vision) loader.

Layout (verified against the live archive)::

    {BASE_URL}/{monthly|daily}/klines/{SYM}/{interval}/{SYM}-{interval}-{YYYY-MM[-DD]}.zip
    {BASE_URL}/monthly/fundingRate/{SYM}/{SYM}-fundingRate-{YYYY-MM}.zip
    {BASE_URL}/daily/metrics/{SYM}/{SYM}-metrics-{YYYY-MM-DD}.zip

Every ``.zip`` has a sibling ``.zip.CHECKSUM`` ("<sha256>  <file name>"). The
checksum is verified on download and again on every cache read.

Date semantics: all loaders take calendar dates in UTC and the range is
**start-inclusive, end-exclusive**: ``[start 00:00 UTC, end 00:00 UTC)``.

Unpublished periods: the monthly archive only has completed, published months.
For klines, a month that is not in the monthly archive falls back to the daily
files for the requested days of that month; a missing daily file raises
``ArchiveNotFoundError`` (never silently skipped). Funding has no daily
archive and open interest only has daily files, so a missing file raises.

Raw timestamps are milliseconds, or microseconds in newer files; both are
normalised to UTC. CSVs may or may not carry a header row.
"""

from __future__ import annotations

import hashlib
import io
import re
import time
import zipfile
from datetime import date, timedelta
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

BASE_URL = "https://data.binance.vision/data/futures/um"
OI_STEP = pd.Timedelta(minutes=5)


class DataGapError(Exception):
    """Gaps, duplicates or non-monotonic timestamps in a time series."""


class ChecksumError(Exception):
    """A downloaded or cached archive does not match its published sha256."""


class ArchiveNotFoundError(Exception):
    """A required archive file does not exist (e.g. period not yet published)."""


# --------------------------------------------------------------------------
# transport
# --------------------------------------------------------------------------


def _http_get(url: str, retries: int = 3) -> bytes | None:
    """GET with retries on network errors / 5xx. Returns None on HTTP 404."""
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            r = httpx.get(url, timeout=60.0, follow_redirects=True)
        except httpx.TransportError as e:
            last = e
        else:
            if r.status_code == 404:
                return None
            if r.status_code < 500:
                r.raise_for_status()
                return r.content
            last = httpx.HTTPStatusError(
                f"HTTP {r.status_code}", request=r.request, response=r
            )
        time.sleep(min(2.0**attempt, 8.0) * 0.5)
    raise ConnectionError(f"failed to fetch {url} after {retries + 1} tries: {last}")


def _expected_sha(text: bytes, url: str) -> str:
    parts = text.decode("ascii", errors="replace").split()
    if not parts or not re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
        raise ChecksumError(f"malformed checksum file for {url}")
    return parts[0].lower()


def _fetch_zip(rel: str, cache_dir: Path) -> bytes | None:
    """Return verified zip bytes for ``rel`` (path under BASE_URL), or None if 404.

    Cached under ``cache_dir/rel`` with a ``.CHECKSUM`` sidecar; the cache is
    re-verified on every read and only verified files are ever written.
    """
    zpath = cache_dir / rel
    cpath = cache_dir / (rel + ".CHECKSUM")
    url = f"{BASE_URL}/{rel}"
    if zpath.exists() and cpath.exists():
        blob = zpath.read_bytes()
        want = _expected_sha(cpath.read_bytes(), url)
        if hashlib.sha256(blob).hexdigest() != want:
            raise ChecksumError(f"cached file fails checksum: {zpath}")
        return blob
    fetched = _http_get(url)
    if fetched is None:
        return None
    blob = fetched
    csum = _http_get(url + ".CHECKSUM")
    if csum is None:
        raise ChecksumError(f"no .CHECKSUM published for {url}")
    want = _expected_sha(csum, url)
    got = hashlib.sha256(blob).hexdigest()
    if got != want:
        raise ChecksumError(f"checksum mismatch for {url}: got {got}, want {want}")
    zpath.parent.mkdir(parents=True, exist_ok=True)
    zpath.write_bytes(blob)
    cpath.write_bytes(csum)
    return blob


def _read_csv(blob: bytes, first_col: str) -> pd.DataFrame:
    """Read the single CSV in a zip, with or without a header row."""
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        names = [n for n in z.namelist() if n.endswith(".csv")]
        if len(names) != 1:
            raise ValueError(f"expected exactly one csv in archive, got {names}")
        raw = z.read(names[0])
    first = raw.split(b"\n", 1)[0].decode("ascii", errors="replace")
    has_header = first.split(",")[0].strip() == first_col
    return pd.read_csv(io.BytesIO(raw), header=0 if has_header else None)


def _to_utc(col: pd.Series) -> pd.DatetimeIndex:
    """Epoch ms or us (auto-detected by magnitude) -> tz-aware UTC index."""
    v = col.astype("int64")
    if len(v) and v.abs().max() >= 10**14:  # >= year ~5138 in ms: must be us
        v = v // 1000
    return pd.DatetimeIndex(pd.to_datetime(v, unit="ms", utc=True))


# --------------------------------------------------------------------------
# continuity
# --------------------------------------------------------------------------

_INTERVAL_RE = re.compile(r"^(\d+)([mhdw])$")
_UNITS = {
    "m": pd.Timedelta(minutes=1),
    "h": pd.Timedelta(hours=1),
    "d": pd.Timedelta(days=1),
    "w": pd.Timedelta(weeks=1),
}


def interval_to_timedelta(interval: str) -> pd.Timedelta:
    m = _INTERVAL_RE.match(interval)
    if not m:
        raise ValueError(f"unsupported interval {interval!r} (use e.g. 5m, 1h, 1d)")
    return int(m.group(1)) * _UNITS[m.group(2)]


def _check_index(idx: pd.Index, what: str) -> pd.DatetimeIndex:
    if not isinstance(idx, pd.DatetimeIndex) or idx.tz is None:
        raise DataGapError(f"{what}: index must be a tz-aware DatetimeIndex")
    if len(idx) == 0:
        raise DataGapError(f"{what}: no data")
    if idx.has_duplicates:
        n = int(idx.duplicated().sum())
        raise DataGapError(
            f"{what}: {n} duplicate timestamp(s), first at {idx[idx.duplicated()][0]}"
        )
    if not idx.is_monotonic_increasing:
        raise DataGapError(f"{what}: index is not monotonic increasing")
    return idx


def check_continuity(bars: pd.DataFrame, interval: str) -> None:
    """Raise ``DataGapError`` on gaps, duplicates or a non-monotonic index."""
    idx = _check_index(bars.index, "bars")
    step = interval_to_timedelta(interval)
    bad = np.asarray((idx[1:] - idx[:-1]) != step)
    if bad.any():
        i = int(bad.argmax())
        raise DataGapError(
            f"gap in bars: expected {step} after {idx[i]}, next bar is {idx[i + 1]} "
            f"({int(bad.sum())} irregular step(s))"
        )


# --------------------------------------------------------------------------
# loaders
# --------------------------------------------------------------------------


def _check_range(start: date, end: date) -> None:
    if end <= start:
        raise ValueError(
            f"empty range: end {end} must be after start {start} (end-exclusive)"
        )


def _months(start: date, end: date) -> list[tuple[int, int]]:
    out, y, m = [], start.year, start.month
    last = end - timedelta(days=1)
    while (y, m) <= (last.year, last.month):
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _utc(d: date) -> pd.Timestamp:
    return pd.Timestamp(d, tz="UTC")


_KLINE_COLS = ["open_time", "open", "high", "low", "close", "volume"]


def _parse_klines(blob: bytes) -> pd.DataFrame:
    df = _read_csv(blob, "open_time")
    df = df.iloc[:, :6]
    df.columns = _KLINE_COLS
    out = df[["open", "high", "low", "close", "volume"]].astype("float64")
    out.index = _to_utc(df["open_time"])
    out.index.name = "open_time"
    return out


def load_klines(
    symbol: str, interval: str, start: date, end: date, cache_dir: Path
) -> pd.DataFrame:
    """OHLCV bars for ``[start, end)`` (UTC dates, end-exclusive).

    Indexed by UTC ``open_time``. Refuses (``DataGapError``) if the result has
    gaps, duplicates, or does not cover the whole requested range.
    """
    _check_range(start, end)
    step = interval_to_timedelta(interval)
    frames: list[pd.DataFrame] = []
    for y, m in _months(start, end):
        ym = f"{y:04d}-{m:02d}"
        rel = f"monthly/klines/{symbol}/{interval}/{symbol}-{interval}-{ym}.zip"
        blob = _fetch_zip(rel, cache_dir)
        if blob is not None:
            frames.append(_parse_klines(blob))
            continue
        d = max(start, date(y, m, 1))
        while d < end and (d.year, d.month) == (y, m):
            rel = (
                f"daily/klines/{symbol}/{interval}/"
                f"{symbol}-{interval}-{d.isoformat()}.zip"
            )
            blob = _fetch_zip(rel, cache_dir)
            if blob is None:
                raise ArchiveNotFoundError(
                    f"no monthly archive for {ym} and no daily archive for "
                    f"{d.isoformat()} ({symbol} {interval}); period not published"
                )
            frames.append(_parse_klines(blob))
            d += timedelta(days=1)
    df = pd.concat(frames)
    lo, hi = _utc(start), _utc(end)
    df = df[(df.index >= lo) & (df.index < hi)]
    check_continuity(df, interval)
    if df.index[0] != lo or df.index[-1] != hi - step:
        raise DataGapError(
            f"{symbol} {interval}: data covers {df.index[0]}..{df.index[-1]}, "
            f"requested [{lo}, {hi}) - edge bars missing"
        )
    return df


def load_funding(symbol: str, start: date, end: date, cache_dir: Path) -> pd.Series:
    """Funding rates with ``funding_time`` in ``[start, end)`` (UTC, end-exclusive).

    Monthly files only; a month that is not published raises
    ``ArchiveNotFoundError``. Duplicates / disorder raise ``DataGapError``.
    """
    _check_range(start, end)
    parts: list[pd.Series] = []
    for y, m in _months(start, end):
        ym = f"{y:04d}-{m:02d}"
        rel = f"monthly/fundingRate/{symbol}/{symbol}-fundingRate-{ym}.zip"
        blob = _fetch_zip(rel, cache_dir)
        if blob is None:
            raise ArchiveNotFoundError(
                f"no monthly funding archive for {symbol} {ym} (not yet published?)"
            )
        df = _read_csv(blob, "calc_time")
        s = pd.Series(
            df.iloc[:, 2].astype("float64").to_numpy(),
            index=_to_utc(df.iloc[:, 0]),
            name="funding_rate",
        )
        parts.append(s)
    s = pd.concat(parts)
    s.index.name = "funding_time"
    s = s[(s.index >= _utc(start)) & (s.index < _utc(end))]
    _check_index(s.index, f"{symbol} funding")
    return pd.Series(s)


def load_open_interest(
    symbol: str, start: date, end: date, cache_dir: Path
) -> pd.Series:
    """5-minute ``sum_open_interest`` for days in ``[start, end)`` (UTC).

    Daily metrics files only. Each day must be present and gap-free
    (``ArchiveNotFoundError`` / ``DataGapError`` otherwise). The index is the
    observation stamp ``create_time``; callers must pass a realistic
    ``available_lag`` to ``align_asof``.
    """
    _check_range(start, end)
    parts: list[pd.Series] = []
    d = start
    while d < end:
        rel = f"daily/metrics/{symbol}/{symbol}-metrics-{d.isoformat()}.zip"
        blob = _fetch_zip(rel, cache_dir)
        if blob is None:
            raise ArchiveNotFoundError(
                f"no daily metrics archive for {symbol} {d.isoformat()}"
            )
        df = _read_csv(blob, "create_time")
        idx = pd.DatetimeIndex(pd.to_datetime(df.iloc[:, 0], utc=True))
        parts.append(
            pd.Series(
                df.iloc[:, 2].astype("float64").to_numpy(),
                index=idx,
                name="open_interest",
            )
        )
        d += timedelta(days=1)
    s = pd.concat(parts)
    s.index.name = "create_time"
    _check_index(s.index, f"{symbol} open interest")
    gaps = s.index[1:] - s.index[:-1] != OI_STEP
    if gaps.any():
        i = int(gaps.argmax())
        raise DataGapError(
            f"{symbol} open interest: expected {OI_STEP} after {s.index[i]}, "
            f"next is {s.index[i + 1]}"
        )
    exp = int((_utc(end) - _utc(start)) / OI_STEP)
    if len(s) != exp:
        raise DataGapError(f"{symbol} open interest: {len(s)} rows, expected {exp}")
    return pd.Series(s)
