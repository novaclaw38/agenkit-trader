# AgenKit Trader

Validation-gated crypto trading agent. See `docs/superpowers/specs/` for the design.

- No performance claims exist yet; none will be made until measured results exist.
- Live trading is locked by default. Only paper/testnet trading is available.

## Development

    python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
    .venv/bin/pytest && .venv/bin/ruff check . && .venv/bin/mypy src

## Data layer

Source: Binance public bulk archive (no API keys, no mainnet API), layout verified live:

    https://data.binance.vision/data/futures/um/monthly/klines/{SYM}/{interval}/{SYM}-{interval}-{YYYY-MM}.zip
    https://data.binance.vision/data/futures/um/daily/klines/{SYM}/{interval}/{SYM}-{interval}-{YYYY-MM-DD}.zip
    https://data.binance.vision/data/futures/um/monthly/fundingRate/{SYM}/{SYM}-fundingRate-{YYYY-MM}.zip
    https://data.binance.vision/data/futures/um/daily/metrics/{SYM}/{SYM}-metrics-{YYYY-MM-DD}.zip   (5-min open interest)

Each zip has a `.zip.CHECKSUM` sibling (sha256), verified on download and on every cache read.
Date ranges are start-inclusive, end-exclusive (UTC). Kline months missing from the monthly
archive fall back to daily files; a missing daily/funding/metrics file raises
`ArchiveNotFoundError`. Gaps, duplicates or disorder raise `DataGapError`; nothing is interpolated.
Run network tests with `pytest -m network`.
