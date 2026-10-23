import hashlib
import io
import zipfile
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from agenkit_trader.data import archive
from agenkit_trader.data.archive import (
    ArchiveNotFoundError,
    ChecksumError,
    DataGapError,
    load_funding,
    load_klines,
    load_open_interest,
)

KL_HEADER = (
    "open_time,open,high,low,close,volume,close_time,quote_volume,count,"
    "taker_buy_volume,taker_buy_quote_volume,ignore"
)
H = 3_600_000


def make_zip(name: str, csv: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name.replace(".zip", ".csv"), csv)
    return buf.getvalue()


def kline_csv(start_ms: int, n: int, header: bool, scale: int = 1) -> str:
    rows = [KL_HEADER] if header else []
    for i in range(n):
        t = start_ms + i * H
        rows.append(
            f"{t * scale},{100 + i},{101 + i},{99 + i},{100.5 + i},{10 + i},"
            f"{(t + H - 1) * scale},1,1,1,1,0"
        )
    return "\n".join(rows) + "\n"


def checksum_for(blob: bytes, name: str) -> bytes:
    return f"{hashlib.sha256(blob).hexdigest()}  {name}\n".encode()


class FakeArchive:
    """Maps URL suffix -> bytes; unknown URLs are 404 (None)."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.calls: list[str] = []

    def add(self, url_path: str, blob: bytes, bad_checksum: bool = False) -> None:
        name = url_path.rsplit("/", 1)[1]
        self.files[url_path] = blob
        self.files[url_path + ".CHECKSUM"] = (
            b"0" * 64 + f"  {name}\n".encode()
            if bad_checksum
            else checksum_for(blob, name)
        )

    def __call__(self, url: str) -> bytes | None:
        self.calls.append(url)
        assert url.startswith(archive.BASE_URL + "/")
        return self.files.get(url[len(archive.BASE_URL) + 1 :])


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeArchive:
    f = FakeArchive()
    monkeypatch.setattr(archive, "_http_get", f)
    return f


JAN1 = int(pd.Timestamp("2024-01-01", tz="UTC").timestamp() * 1000)


def add_month_klines(f: FakeArchive, header: bool, scale: int = 1) -> None:
    name = "BTCUSDT-1h-2024-01.zip"
    f.add(
        f"monthly/klines/BTCUSDT/1h/{name}",
        make_zip(name, kline_csv(JAN1, 31 * 24, header, scale)),
    )


@pytest.mark.parametrize("header", [True, False])
def test_klines_columns_and_utc_index(
    fake: FakeArchive, tmp_path: Path, header: bool
) -> None:
    add_month_klines(fake, header)
    df = load_klines("BTCUSDT", "1h", date(2024, 1, 1), date(2024, 2, 1), tmp_path)
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df.index.name == "open_time"
    assert str(df.index.tz) == "UTC"
    assert len(df) == 31 * 24
    assert df.index[0] == pd.Timestamp("2024-01-01", tz="UTC")
    assert df["open"].iloc[0] == 100.0
    assert df.dtypes.map(lambda d: d.kind).eq("f").all()


def test_klines_microsecond_timestamps(fake: FakeArchive, tmp_path: Path) -> None:
    add_month_klines(fake, header=True, scale=1000)
    df = load_klines("BTCUSDT", "1h", date(2024, 1, 1), date(2024, 2, 1), tmp_path)
    assert df.index[0] == pd.Timestamp("2024-01-01", tz="UTC")
    assert df.index[-1] == pd.Timestamp("2024-01-31 23:00", tz="UTC")


def test_end_is_exclusive(fake: FakeArchive, tmp_path: Path) -> None:
    add_month_klines(fake, header=True)
    df = load_klines("BTCUSDT", "1h", date(2024, 1, 2), date(2024, 1, 4), tmp_path)
    assert df.index[0] == pd.Timestamp("2024-01-02", tz="UTC")
    assert df.index[-1] == pd.Timestamp("2024-01-03 23:00", tz="UTC")
    assert len(df) == 48


def test_checksum_mismatch_raises(fake: FakeArchive, tmp_path: Path) -> None:
    name = "BTCUSDT-1h-2024-01.zip"
    fake.add(
        f"monthly/klines/BTCUSDT/1h/{name}",
        make_zip(name, kline_csv(JAN1, 31 * 24, True)),
        bad_checksum=True,
    )
    with pytest.raises(ChecksumError):
        load_klines("BTCUSDT", "1h", date(2024, 1, 1), date(2024, 2, 1), tmp_path)
    assert not list(tmp_path.rglob("*.zip"))  # corrupt file is not cached


def test_cache_is_used_and_reverified(fake: FakeArchive, tmp_path: Path) -> None:
    add_month_klines(fake, True)
    args = ("BTCUSDT", "1h", date(2024, 1, 1), date(2024, 2, 1), tmp_path)
    load_klines(*args)
    n = len(fake.calls)
    load_klines(*args)
    assert len(fake.calls) == n  # no network the second time
    zp = next(tmp_path.rglob("*.zip"))
    zp.write_bytes(zp.read_bytes() + b"x")  # corrupt the cache
    with pytest.raises(ChecksumError):
        load_klines(*args)


def test_missing_month_falls_back_to_daily(fake: FakeArchive, tmp_path: Path) -> None:
    for d in (1, 2):
        name = f"BTCUSDT-1h-2024-01-0{d}.zip"
        start = JAN1 + (d - 1) * 24 * H
        fake.add(
            f"daily/klines/BTCUSDT/1h/{name}",
            make_zip(name, kline_csv(start, 24, True)),
        )
    df = load_klines("BTCUSDT", "1h", date(2024, 1, 1), date(2024, 1, 3), tmp_path)
    assert len(df) == 48


def test_missing_daily_raises_clear_error(fake: FakeArchive, tmp_path: Path) -> None:
    with pytest.raises(ArchiveNotFoundError, match="2024-01-01"):
        load_klines("BTCUSDT", "1h", date(2024, 1, 1), date(2024, 1, 2), tmp_path)


def test_rejects_empty_or_inverted_range(fake: FakeArchive, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        load_klines("BTCUSDT", "1h", date(2024, 1, 2), date(2024, 1, 2), tmp_path)


def test_funding(fake: FakeArchive, tmp_path: Path) -> None:
    name = "BTCUSDT-fundingRate-2024-01.zip"
    t0 = JAN1
    csv = "calc_time,funding_interval_hours,last_funding_rate\n" + "".join(
        f"{t0 + i * 8 * H},8,0.000{i}\n" for i in range(93)
    )
    fake.add(f"monthly/fundingRate/BTCUSDT/{name}", make_zip(name, csv))
    s = load_funding("BTCUSDT", date(2024, 1, 1), date(2024, 1, 2), tmp_path)
    assert s.index.name == "funding_time"
    assert str(s.index.tz) == "UTC"
    assert len(s) == 3  # end-exclusive: 00:00, 08:00, 16:00
    assert s.iloc[1] == pytest.approx(0.0001)


def funding_zip(rows: list[tuple[int, int]]) -> bytes:
    name = "BTCUSDT-fundingRate-2024-01.zip"
    csv = "calc_time,funding_interval_hours,last_funding_rate\n" + "".join(
        f"{t},{h},0.0001\n" for t, h in rows
    )
    return make_zip(name, csv)


def put_funding(f: FakeArchive, rows: list[tuple[int, int]]) -> None:
    f.add(
        "monthly/fundingRate/BTCUSDT/BTCUSDT-fundingRate-2024-01.zip",
        funding_zip(rows),
    )


def test_funding_clean_8h_passes(fake: FakeArchive, tmp_path: Path) -> None:
    put_funding(fake, [(JAN1 + i * 8 * H, 8) for i in range(93)])
    a = (date(2024, 1, 1), date(2024, 1, 11))
    assert len(load_funding("BTCUSDT", *a, tmp_path)) == 30


def test_funding_clean_4h_passes(fake: FakeArchive, tmp_path: Path) -> None:
    put_funding(fake, [(JAN1 + i * 4 * H, 4) for i in range(186)])
    a = (date(2024, 1, 1), date(2024, 1, 11))
    assert len(load_funding("BTCUSDT", *a, tmp_path)) == 60


def test_funding_ms_jitter_tolerated(fake: FakeArchive, tmp_path: Path) -> None:
    put_funding(fake, [(JAN1 + i * 8 * H + (i % 3), 8) for i in range(93)])
    s = load_funding("BTCUSDT", date(2024, 1, 1), date(2024, 2, 1), tmp_path)
    assert len(s) == 93


def test_funding_interval_change_passes(fake: FakeArchive, tmp_path: Path) -> None:
    rows = [(JAN1 + i * 8 * H, 8) for i in range(3)]  # day 1
    rows += [(JAN1 + 24 * H + i * 4 * H, 4) for i in range(6)]  # day 2 at 4h
    put_funding(fake, rows)
    s = load_funding("BTCUSDT", date(2024, 1, 1), date(2024, 1, 3), tmp_path)
    assert len(s) == 9


def test_funding_preceding_row_interval_governs(
    fake: FakeArchive, tmp_path: Path
) -> None:
    a = (date(2024, 1, 1), date(2024, 1, 2))
    # 00:00 (4h) then 08:00: the 04:00 event is missing -> refuse
    rows = [(JAN1, 4), (JAN1 + 8 * H, 8), (JAN1 + 16 * H, 8)]
    put_funding(fake, rows)
    with pytest.raises(DataGapError, match="missing funding"):
        load_funding("BTCUSDT", *a, tmp_path)


def test_funding_8h_then_4h_boundary_passes(fake: FakeArchive, tmp_path: Path) -> None:
    rows = [
        (JAN1, 8),
        (JAN1 + 8 * H, 4),
        (JAN1 + 12 * H, 4),
        (JAN1 + 16 * H, 4),
        (JAN1 + 20 * H, 4),
    ]
    put_funding(fake, rows)
    s = load_funding("BTCUSDT", date(2024, 1, 1), date(2024, 1, 2), tmp_path)
    assert len(s) == 5


def test_funding_nan_interval_raises(fake: FakeArchive, tmp_path: Path) -> None:
    name = "BTCUSDT-fundingRate-2024-01.zip"
    csv = (
        "calc_time,funding_interval_hours,last_funding_rate\n"
        f"{JAN1},8,0.0001\n{JAN1 + 8 * H},,0.0001\n{JAN1 + 16 * H},8,0.0001\n"
    )
    fake.add(f"monthly/fundingRate/BTCUSDT/{name}", make_zip(name, csv))
    with pytest.raises(DataGapError, match="interval"):
        load_funding("BTCUSDT", date(2024, 1, 1), date(2024, 1, 2), tmp_path)


def test_funding_dropped_middle_event_raises(fake: FakeArchive, tmp_path: Path) -> None:
    rows = [(JAN1 + i * 8 * H, 8) for i in range(93) if i != 10]
    put_funding(fake, rows)
    with pytest.raises(DataGapError, match="missing funding"):
        load_funding("BTCUSDT", date(2024, 1, 1), date(2024, 1, 31), tmp_path)


def test_funding_late_start_raises(fake: FakeArchive, tmp_path: Path) -> None:
    put_funding(fake, [(JAN1 + i * 8 * H, 8) for i in range(3, 93)])
    with pytest.raises(DataGapError, match="first event"):
        load_funding("BTCUSDT", date(2024, 1, 1), date(2024, 1, 31), tmp_path)


def test_funding_early_end_raises(fake: FakeArchive, tmp_path: Path) -> None:
    put_funding(fake, [(JAN1 + i * 8 * H, 8) for i in range(80)])
    with pytest.raises(DataGapError, match="last event"):
        load_funding("BTCUSDT", date(2024, 1, 1), date(2024, 2, 1), tmp_path)


def test_funding_missing_month_raises(fake: FakeArchive, tmp_path: Path) -> None:
    with pytest.raises(ArchiveNotFoundError):
        load_funding("BTCUSDT", date(2024, 1, 1), date(2024, 1, 2), tmp_path)


def metrics_csv(day: str, n: int = 288, skip: int | None = None) -> str:
    rows = [
        "create_time,symbol,sum_open_interest,sum_open_interest_value,"
        "count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,"
        "count_long_short_ratio,sum_taker_long_short_vol_ratio"
    ]
    base = pd.Timestamp(day)
    for i in range(n):
        if i == skip:
            continue
        ts = (base + pd.Timedelta(minutes=5 * i)).strftime("%Y-%m-%d %H:%M:%S")
        rows.append(f"{ts},BTCUSDT,{1000 + i}.5,1,1,1,1,1")
    return "\n".join(rows) + "\n"


def test_open_interest(fake: FakeArchive, tmp_path: Path) -> None:
    name = "BTCUSDT-metrics-2024-01-05.zip"
    fake.add(f"daily/metrics/BTCUSDT/{name}", make_zip(name, metrics_csv("2024-01-05")))
    s = load_open_interest("BTCUSDT", date(2024, 1, 5), date(2024, 1, 6), tmp_path)
    assert len(s) == 288
    assert s.index[0] == pd.Timestamp("2024-01-05", tz="UTC")
    assert s.iloc[0] == 1000.5
    assert s.name == "open_interest"


def test_open_interest_gap_is_refused(fake: FakeArchive, tmp_path: Path) -> None:
    from agenkit_trader.data.archive import DataGapError

    name = "BTCUSDT-metrics-2024-01-05.zip"
    fake.add(
        f"daily/metrics/BTCUSDT/{name}",
        make_zip(name, metrics_csv("2024-01-05", skip=10)),
    )
    with pytest.raises(DataGapError):
        load_open_interest("BTCUSDT", date(2024, 1, 5), date(2024, 1, 6), tmp_path)


@pytest.mark.network
def test_real_archive_btc_1h_month_is_continuous(tmp_path: Path) -> None:
    from agenkit_trader.data.archive import check_continuity

    df = load_klines("BTCUSDT", "1h", date(2024, 1, 1), date(2024, 2, 1), tmp_path)
    assert len(df) == 31 * 24
    check_continuity(df, "1h")


@pytest.mark.network
def test_real_archive_funding_and_open_interest(tmp_path: Path) -> None:
    f = load_funding("BTCUSDT", date(2024, 1, 1), date(2024, 2, 1), tmp_path)
    assert len(f) == 93
    oi = load_open_interest("BTCUSDT", date(2024, 1, 5), date(2024, 1, 6), tmp_path)
    assert len(oi) == 288


@pytest.mark.network
def test_real_archive_recent_month(tmp_path: Path) -> None:
    # Most recent published month (format drift check); us timestamps are unit-tested.
    df = load_klines("BTCUSDT", "1h", date(2026, 9, 1), date(2026, 10, 1), tmp_path)
    assert df.index[0] == pd.Timestamp("2026-09-01", tz="UTC")
    assert len(df) == 30 * 24
