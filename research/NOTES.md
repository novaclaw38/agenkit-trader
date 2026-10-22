# Research outcome note

This note summarises the committed research results. The numbers come from
`research/REPORT.md` and `research/trials.json`; the funding-settlement counts
come from the reviewer's probe of the archive data.

## What was run

The research was run once:

```
.venv/bin/python scripts/run_research.py --symbols BTCUSDT ETHUSDT --interval 1h --start 2021-01-01 --end 2026-10-01
```

These values are the script's defaults.

- `research/trials.json` counts 60 trials.
- 0 of 6 strategy x symbol candidates passed the validation gate.
- `research/deployed.json` is an empty list. Nothing is deployed.

## funding_reversion: untested, not refuted

The hold-out period starts around 2025-01-09. In it there were:

- 0 BTCUSDT funding settlements beyond +-0.03%, against 430 in the train period;
- 1 ETHUSDT funding settlement beyond +-0.03%, against 496 in the train period.

So the strategy made 0 trades (BTCUSDT) and 1 trade (ETHUSDT) on the hold-out.
The hold-out says nothing either way about this candidate. It is untested there,
not refuted.

## ETHUSDT trend_breakout

`REPORT.md` records a hold-out Sharpe for this candidate. It was rejected for:

- hold-out max drawdown;
- an unstable parameter sweep;
- the trial-adjusted p-value.

See `REPORT.md` for the exact numbers.

## Assumptions and limits

- Spread (1 bp half-spread) and slippage (2 bp) are unverified ASSUMPTIONS in
  `config/research.yaml`.
- Stops are checked at bar close only. Intrabar stops are not simulated.
- Only two symbols were tested (BTCUSDT, ETHUSDT). This is survivorship bias.
- The data is mainnet archive data. Testnet fills will differ.
- One-pass discipline: any change to a strategy or its parameter grid needs a
  new run, and that run's grid points must be added to `research/trials.json`.

None of this is a claim of profitability.

## Trial count used by the deflated Sharpe (added after the committed run)

The committed run tested each candidate against the cumulative trial count
reached so far (`n_trials` 9, 21, 30, 39, 51, 60 in `REPORT.json`), so
candidate order changed the p-value bar. This did not change any verdict:
every p-value was above 0.93, and the p-value only grows with `n_trials`, so
testing all six against the final total of 60 would also reject them all.
Future runs (`scripts/run_research.py` via `evaluate_candidates`) add the
grid points of every strategy x symbol to `research/trials.json` before any
candidate is evaluated, and test every candidate against that final total.
The committed report was not regenerated.

## What this research does not validate (added after the committed run)

The runtime does not trade what research validated. A research pass does NOT validate the runtime's sizing or stop policy. Research backtests each signal's fractional position (e.g. trend_breakout's vol_cap scaling up to 1x equity) with no stop-outs; the paper runtime keeps only the sign of the signal, sizes for 1% risk at a 3 x ATR(14) exchange-side disaster stop (roughly 0.4x equity notional on ETH 1h, an illustrative estimate) and does not re-enter after a stop-out until a fresh signal transition. The backtest simulates neither the stop nor that rule. Making it do so would be a new research design and a new counted run, for the user to decide.
Paper results therefore measure an unvalidated sizing/stop policy.

## Runtime-policy simulator (added after the committed run)

The committed run used the research policy (fractional positions, no stops);
everything above describes that run and stays true. Since then a
runtime-policy simulator exists (`backtest/runtime_policy.py`): sign of the
signal, entries only on a signal transition, 1% risk at the 3 x ATR(14)
stop, entries the risk gate would reject are skipped, constant quantity, an
intrabar exchange stop (filled at the stop, or at the open when a bar gaps
through it), no re-entry until a fresh transition. A parity test checks it
against the real runtime loop on synthetic bars. `scripts/run_research.py`
now takes `--policy {research,runtime}`; the default for the next run is
`runtime`, and REPORT.md/REPORT.json record the policy. No run has been made
with it, so there are no numbers for it. It cannot validate real exchange
stop fills, testnet fills or the interaction of a 1h-ATR stop with live
slippage, and it does not simulate the kill switch.

## Correction (added after the committed run)

The wording in "What this research does not validate" above was wrong:
research did not back-test "fractional positions" and trend_breakout's
vol_cap does not scale positions up to 1x equity. Strategy positions are
always -1, 0 or +1 (`validation/sweep.py` `to_positions`), and vol_cap is a
boolean calm-market filter. Corrected difference:

- committed run: +-1x equity, rebalanced every bar, no stops;
- runtime: risk-based sizing (1% of equity at a 3 x ATR(14) stop), constant
  quantity, exchange-side stop, no re-entry after a stop-out until a fresh
  signal transition, and a 3% daily-loss / 10% drawdown halt.

The corrected statement is in README.md ("What research validated vs what
paper runs") and in spec section 12.

Also superseded in the previous section ("Runtime-policy simulator"): the
simulator now DOES simulate the 3%/10% kill-switch halt (a halted hold-out
is rejected), rounds quantities and stops with static testnet symbol rules
(`config/symbol_rules.yaml`), and `--policy` has no default: every counted
run must choose it explicitly. The committed run's statements and numbers
are unchanged.

## Simulator fixes: signal window, intrabar halt, deployed policy (added later)

No research run was made for these changes; REPORT.*, trials.json and
deployed.json are untouched and the committed run's numbers are unchanged.

- Signal window. The runtime-policy simulator took signals computed on the
  whole history, while the runtime computes them on its rolling
  `bars_limit` window (`hist[t-bars_limit+1 : t+1]`), with `prev` read from
  that window. Under `--policy runtime` signals are now computed per bar on
  that window (`window_signals`), and a grid point whose strategy warm-up
  (lookback included) reaches `bars_limit` is refused. A parity test compares
  these signals with the real runtime for trend_breakout lookback 336 /
  vol_cap 1.0 on random-walk data. Cost: about 1000x the signal work, 2-7 ms
  per bar (0.5-2 minutes per grid point on 17k bars); a runtime-policy sweep
  will be much slower than the research one.
- Intrabar halt. The simulator checked the 3%/10% limits only at closes and
  after a stop fill. An open position is now also marked at the bar's adverse
  extreme and a breach halts and flattens at that price. This assumes the
  runtime's poll sees the extreme (conservative; a real poll interval may
  miss a brief wick), and the favourable extreme is not used to raise the
  peak. Parity-tested with a dip-that-recovers bar.
- deployed.json. `load_deployed` now refuses entries whose `policy` is not
  exactly `"runtime"` (missing included). The report already wrote `policy`
  for both policies. The committed research-policy run's file is empty, so
  nothing changes for it, but any future research-policy pass cannot be paper
  traded.

## Simulator follow-ups: pre-count guard, funding window, docs (added later)

No research run was made; REPORT.*, trials.json and deployed.json are
untouched and the committed run's numbers are unchanged.

- Guard before counting. Under `policy="runtime"`, `evaluate` and
  `evaluate_candidates` now check the warm-up of every grid point of every
  candidate against the runtime window before any `counter.add`, so a
  `WindowError` can no longer leave counted trials behind.
- Funding window. The runtime fetches only the last `FUNDING_LIMIT` (1000)
  funding records and as-of aligns them to its window. `window_signals` now
  rebuilds each window's funding that way from the raw records (last 1000
  stamped at or before the close of bar t), so bars whose record fell outside
  the tail get NaN, as in the runtime. With 8-hourly records and
  `bars_limit` 1000 hourly bars the tail covers about 125 records, so the cap
  never bites in practice; it matters only if the window held more than 1000
  records. Parity tests (real Runtime + FakeVenue) now cover funding_reversion,
  short_reversion, a tiny-FUNDING_LIMIT truncation case and a strategy whose
  warm-up is `bars_limit - 1`. No runtime/simulator mismatch was found. One
  approximation remains: a record stamped between the bar close and the
  runtime's poll time is counted as fetched only if stamped at or before the
  close.
- Docs/dead code. The gate docstring now says full-history signals apply to the
  research policy only, and the unused `prev_signal` computed from the full
  history under the runtime policy is gone (the per-bar window `prev` is what
  the simulator uses).

## Round-2 tooling built (2026-10-22); no round-2 run has happened

Built from the frozen pre-registration
(docs/superpowers/specs/2026-10-22-preregistration-round2.md): the
`trend_funding_filter` strategy (NaN funding suppresses new entries, fail
safe), `config/research_round2.yaml` (10 symbols, 4h, the two frozen grids,
output under `research/round2/`, shared `research/trials.json`),
`scripts/run_research.py --config/--trials/--plan` (round-1 defaults
unchanged; `--plan` loads no data and does not touch the counter) and the 8
new symbols in `config/symbol_rules.yaml`. Nothing was counted: the trial
counter is still 60, `research/REPORT.*` and `deployed.json` are untouched, and
no round-2 results exist. The run will add 90 trials (30 + 60) for a total of
150.

## Incident: stray public-data download during test development (2026-10-22)

While writing tests for `--plan`, a test stub patched the wrong namespace
(`runpy.run_path` returns a copy of the globals), so a test ran the real
research main and fetched public bulk archive data from data.binance.vision
(about 20:06-20:07): BTCUSDT and ETHUSDT 4h monthly kline zips, and BNBUSDT
monthly funding zips (2021-01..2022-09, 21 zips + CHECKSUMs) with an empty
BNBUSDT klines directory. This corrects the earlier, incomplete disclosure
(which named only BTC/ETH 4h klines). Nothing was parsed into an evaluation,
backtested or counted, no result was seen, and research/trials.json is still
{"total": 60}. Deleted: all `data/cache/**/4h` files and the BNBUSDT cache
directories (funding and klines). Kept: BTC/ETH 1h kline and funding caches
(round-1 data; funding is interval independent). The test now stubs
`load_klines`/`load_funding` in the namespace `main` really uses, and
tests/conftest.py has an autouse guard that fails any test attempting a real
network fetch (archive `_http_get`, `httpx.get`, real httpx transports).
Procedural clarifications are in
docs/superpowers/specs/2026-10-22-preregistration-round2-addendum.md.
