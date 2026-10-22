# Addendum to the round-2 pre-registration (procedural clarifications)

The frozen spec (2026-10-22-preregistration-round2.md) is NOT edited. This
addendum is procedural and about naming, written before any round-2 data was
evaluated (no round-2 backtest, trial or result exists; trials.json is 60).
It changes no rule, grid, symbol, period, cost, gate or hypothesis.

## Names

- Candidate A is implemented as the registered strategy `trend_breakout_r2`:
  the `trend_breakout` signal (reused, not copied) with registered grid
  lookback {42, 84, 168}, vol_cap {1.5}. Reason: the runtime refuses a
  deployment whose params are not on the strategy's registered grid, and
  `trend_breakout`'s round-1 grid has no 42/84. `trend_breakout` and its grid
  are unchanged; A's 30 trials are unchanged (3 x 10).
- Candidate B is `trend_funding_filter` (60 trials). NaN funding suppresses
  new entries (fail safe); a suppressed trade is skipped for its whole life.
- Total new trials 90; counter 60 -> 150; deflated Sharpe uses 150.

## Crash recovery

Trials are counted once, before any candidate is evaluated. If a run crashes
after counting, those trials are SPENT. A rerun must be a NEW, noted
configuration (research/NOTES.md) with `expect.total_after: 240`, judged
against 240 trials. The config or counter is never edited to hide the crash.

## Symbol drop

A symbol that fails the loader's gap/listing checks is dropped BEFORE any
evaluation: the config's symbol list is edited and `expect` updated (9
symbols => per-strategy 27/54, new 81, total_after 141), and the drop is
recorded in research/NOTES.md with the loader's error text. The list is never
changed after any evaluation has run. With `--config`, CLI flags that differ
from the config are refused.

## Still frozen

10 symbols (subject to the drop rule), 4h, 2021-01-01 .. 2026-10-01
(end-exclusive), hold-out and gate as committed, `--policy runtime`.
