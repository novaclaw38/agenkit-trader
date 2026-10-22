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

## Commands

Every command below was checked with its `-h` or its refusal path.

### First paper run (testnet only)

`agenkit paper` currently refuses to start: `research/deployed.json` is empty
because no strategy passed the validation gate. The steps below are for when
one does.

Keys: create them on https://testnet.binancefuture.com only (never mainnet
keys). They are read only from the environment:

    export BINANCE_TESTNET_KEY=...      # testnet.binancefuture.com key
    export BINANCE_TESTNET_SECRET=...   # testnet.binancefuture.com secret

First run (creates a new paper state file; `--config` defaults to
`config/risk.yaml`, the RISK config, not `config/runtime.yaml`):

    agenkit paper --config config/risk.yaml --init-state

A single cycle you can watch, then exit:

    agenkit paper --config config/risk.yaml --once

After a kill-switch halt (exit code 3), read `logs/paper.jsonl`, then clear the
halt (asks you to type `RESUME`):

    agenkit reset-halt

Checklist:

- [ ] After the first entry, open the testnet UI and confirm the reduce-only
      STOP order for that position actually appears on the exchange. The
      algo-order behaviour of the testnet has NOT been verified by this
      project; if no stop is there, stop the runtime and treat the position as
      unprotected.
- [ ] Read the "What research validated vs what paper runs" note below.

### Research

    agenkit research -h     # flags of scripts/run_research.py

A real `agenkit research` run downloads data, adds every grid point to
`research/trials.json` (never reset) and rewrites `research/REPORT.md` and
`research/deployed.json`. Each run is a counted trial; see `research/NOTES.md`.

### Live

    agenkit live -h

Live trading is locked and NOT implemented. `agenkit live` (interactive
terminal only) checks the unlock conditions in `config/live.yaml` and places
nothing, whatever the outcome. See `docs/GOING_LIVE.md`.

## What research validated vs what paper runs

A research pass does NOT validate the runtime's sizing or stop policy.

- Research backtests each signal's fractional position as-is (for example,
  trend_breakout's vol_cap scaling goes up to 1x equity) and never stops out.
- The paper runtime keeps only the sign of the signal. It sizes so that the
  loss at a 3 x ATR(14) exchange-side disaster stop is at most 1% of equity
  (roughly 0.4x equity notional on ETH 1h; an illustrative estimate, not a
  measurement). After a stop-out it does not re-enter until the signal makes
  a fresh transition.
- The backtest simulates neither the disaster stop nor that re-entry rule.

So paper P&L is not the P&L of what was validated. Making the backtest
simulate runtime sizing and stops would be a new research design and a new
counted run; that is the user's decision.

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
