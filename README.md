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

### Testnet smoke test

    agenkit smoke [--symbol BTCUSDT] [--yes]

A one-shot checklist of the order plumbing on the Binance futures testnet,
run by you with your own testnet keys (`BINANCE_TESTNET_KEY` /
`BINANCE_TESTNET_SECRET`). It places two tiny testnet orders with
exchange-side stops, closes them (one through the kill switch) and verifies
the account is flat. No strategy, no real money, its own log
(`data/smoke/smoke_log.jsonl`) that paper stats and the live unlock never read.
It has not been run against the real testnet by the developers. Instructions:
`docs/GOING_LIVE.md`.

### UI (read-only)

    agenkit ui              # http://127.0.0.1:8765/

A local, read-only web page: status, decisions, research, risk limits and
how the system could fail. It binds 127.0.0.1 only (any other `--host` is
refused), answers GET only, reads the log, state, research and config files
afresh on every request, and needs no keys. It shows only what is in those
files and changes nothing. See `docs/superpowers/specs/2026-10-22-ui-design.md`.

## What research validated vs what paper runs

A research pass does NOT validate the runtime's sizing or stop policy.

- The committed research run held +-1x equity (positions are always -1, 0
  or +1; trend_breakout's vol_cap is a calm-market filter, not position
  scaling), rebalanced every bar, with no stops.
- The paper runtime keeps only the sign of the signal. It sizes so that the
  loss at a 3 x ATR(14) exchange-side disaster stop is at most 1% of equity
  (roughly 0.4x equity notional on ETH 1h; an illustrative estimate, not a
  measurement). After a stop-out it does not re-enter until the signal makes
  a fresh transition.
- The runtime also halts on a 3% daily loss or a 10% drawdown.
- The committed run's backtest simulated none of the runtime's sizing, the
  disaster stop, the re-entry rule or the halt.

So paper P&L is not the P&L of what was validated.

A runtime-policy simulator now exists (`backtest/runtime_policy.py`,
`run_backtest_runtime`). It mirrors the runtime: sign of the signal, entries
only on a signal transition, 1% risk at the 3 x ATR(14) stop rounded to the
symbol's lot and tick (`config/symbol_rules.yaml`, a testnet snapshot), gate
rejections skipped, constant quantity, an intrabar exchange stop filled at
the stop or at a gapping open, no re-entry until a fresh transition, and the
3% daily-loss / 10% drawdown halt (a halted hold-out is rejected). A parity
test runs it and the real runtime loop on the same synthetic bars, with the
kill switch enabled and with costs, and checks they make the same trades and
halt on the same bar. `scripts/run_research.py --policy {research,runtime}`
is REQUIRED, so a counted run always chooses deliberately; the report and
`deployed.json` record the policy. The COMMITTED run used the research
policy; its numbers are unchanged and say nothing about the runtime policy.

Three changes made the simulator closer to the runtime (not yet used for any
counted run): (1) under the runtime policy each bar's signal is computed on
the runtime's rolling `bars_limit` window, as `runtime/loop.py` does, not on
the full history (this matters for trend_breakout lookback 336; a parity test
checks it against the real runtime), at roughly 1000x the signal cost (about
0.5-2 minutes per grid point on 17k bars); grid points whose strategy warm-up
reaches `bars_limit` are refused. (2) `load_deployed` refuses any
`deployed.json` entry whose `policy` is not exactly `"runtime"`, since a
research-policy verdict does not validate what the runtime trades; a file
from the committed research-policy run would be refused. (3) An open position
is marked at the bar's adverse extreme (low for longs, high for shorts) for
the 3% / 10% halt, so a dip that recovers by the close halts, as the
runtime's polling would; this assumes the poll catches the extreme.

Even the runtime policy cannot validate real exchange stop fills, testnet
fills and fees, how a 1h-ATR stop interacts with live slippage, or the real
polling interval of the kill switch. A new run is a new counted trial;
whether to make one is the user's decision.

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
