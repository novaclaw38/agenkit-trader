# AgenKit Trader

Validation-gated crypto trading agent. See `docs/superpowers/specs/` for the design.

- No performance claims exist, and none will be made without measured results.

## Status

What exists:

- An archive data layer.
- A cost-aware backtester.
- A risk gate and kill switch.
- A validation gate.
- Three strategy ideas.
- A paper-trading runtime on the Binance Futures testnet (`agenkit paper`).
- A locked live gate (`agenkit live`).

Status of each part:

- **Research:** 0 of 6 strategy x symbol candidates (3 strategies on BTCUSDT
  and ETHUSDT, 1h) passed the validation gate. See `research/REPORT.md` and
  `research/NOTES.md`. The funding candidate was effectively untested, since
  its hold-out had almost no extreme-funding events.
- **Deployment:** none. `research/deployed.json` is empty.
- **Paper trading:** `agenkit paper` refuses to start until a strategy passes
  the gate. There is no bypass.
- **Live trading:** locked, and not implemented. `agenkit live` only checks
  the unlock conditions. Even when they pass, no order can be placed in this
  build. See `docs/GOING_LIVE.md`.
- **Known failure modes:** see section 12 of the design spec.

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
