# Round 3 design: cross-sectional relative strength (DRAFT)

Status: design for review. Nothing here has been run, no data has touched the
strategy, no code is written. User approved direction A in
`2026-10-23-round3-scoping.md`. This document is NOT the pre-registration; the
list of what the pre-registration must freeze is in section 11. Where a number
is a parameter I chose, it is marked PROPOSED and gets frozen there.

Read first (what exists, so the design fits it): `runtime/loop.py` (one
deployment per symbol, one decision per bar, entry only on a signal
transition, ATR stop + reduce-only exchange stop, per-cycle stop coverage,
kill switch on daily 3% / drawdown 10%), `risk/gate.py` (per-ORDER checks;
other symbols enter only through the summed entry-price notional in the
leverage check), `risk/sizing.py`, `killswitch.py` (`venue.flatten_all()`
closes every position, bypasses the gate), `backtest/runtime_policy.py`
(single-symbol simulator that follows `Runtime._step/_enter` rule for rule),
`validation/{gate,sweep,trials,report}.py` (single-symbol `evaluate`,
`TrialCounter`, deflated Sharpe), `execution/venue.py` (`Venue` protocol),
`cli.py`, `config/*.yaml`, `tests/runtime/test_parity.py` (parity on
`FakeVenue`).

## 1. Strategy: `xs_momentum` (the unit tested is the portfolio)

Edge claim: among liquid perps, assets that outperformed their peers over the
last L bars tend to keep outperforming over the next rebalance period, and
laggards keep lagging (cross-sectional momentum), so a long-top / short-bottom
book earns a spread that is largely market-neutral. Prior is weak (section 9).

Definitions, all at the close of decision bar `t`, closed bars only:

- Universe U (fixed, in this order, which is also the tie-break order):
  `BNBUSDT, ADAUSDT, AVAXUSDT, BTCUSDT, DOGEUSDT, ETHUSDT, LINKUSDT`
  (alphabetical). Order is frozen; it is not a free choice.
- Score: `s_i(t) = close_i[t] / close_i[t-L] - 1` (simple return over exactly L
  bars; uses bars `t-L..t`, i.e. L+1 closes). No smoothing, no vol scaling.
- Eligible at `t`: symbol has a bar at `t` AND bars `t-L..t` are present and
  contiguous (4h spacing), all with finite OHLC and close > 0. Anything else
  (missing bar, gap inside the window, NaN) makes the symbol INELIGIBLE for
  that rebalance. It is never forward-filled or imputed.
- Rebalance bars: bar `t` is a rebalance bar iff
  `floor(open_ts_seconds / bar_seconds) mod R == 0`, i.e. the schedule is
  anchored to the Unix epoch, NOT to the start of any window. This is
  essential: the runtime sees a rolling window, so any "every R-th bar of my
  window" rule would differ between runtime, simulator and restarts.
- Ranking at a rebalance bar: sort eligible symbols by `s` descending; ties
  (exact float equality) broken by the frozen universe order. Longs = first k,
  shorts = last k. If fewer than `2k+1` symbols are eligible, the target is
  EMPTY (flat) for that rebalance (no partial book on thin information); a
  logged `insufficient_universe` reason.
- Target set `T(t)`: `{symbol: +1/-1}` for the 2k selected symbols. Between
  rebalance bars there is no decision at all (no signal evaluation); only
  stops, kill switch and stop-coverage run.
- Lookahead guard: the portfolio analogue of `assert_no_lookahead` (section 6):
  `T(t)` must be identical when every symbol's bars after `t` are removed or
  replaced with noise. Score uses only `close[t-L..t]`; the ATR for sizing
  uses only bars `<= t`.
- A symbol with missing data that is currently HELD: at a rebalance bar it is
  not in `T` (ineligible), so its leg is exited at that rebalance (if the bar
  is simply late, the runtime's existing stale-data rule still applies:
  newest closed bar older than `interval + stale_after_s` kills and halts).
  Between rebalances a gap changes nothing: the leg keeps its exchange stop.
  Historical data has no gaps (the loader raises `DataGapError`; the 7 symbols
  are exactly those that loaded cleanly), so in research the ineligible path
  only runs in synthetic parity tests; it exists because live can gap.

Position semantics (this is the policy being validated, so it is fixed):

- At a rebalance bar: (1) exit every held leg not in `T` or on the wrong side;
  (2) legs already held on the correct side are KEPT unchanged (quantity held
  constant, original stop kept, as the single-symbol policy does); (3) for each
  `T` symbol that is flat, attempt entry, in order: longs by rank, then shorts
  by reverse rank. A leg stopped out earlier in the period and still in `T`
  IS re-entered at the next rebalance (a rebalance is a fresh decision; this
  is the one deliberate difference from "re-enter only on a signal
  transition").
- Exits happen only at rebalance bars (market, reduce-only, at the bar close)
  or by the exchange stop intrabar, or by the kill switch. No other exits.
- No entry at the final simulated bar (same approximation as the existing
  simulator).

## 2. Grids and trial counting

Grid (PROPOSED, 18 points, all counted before any run):

- `lookback L` in {42, 84, 168} bars (7, 14, 28 days)
- `k` in {1, 2}
- `rebalance R` in {6, 18, 42} bars (1, 3, 7 days)

3 x 2 x 3 = 18 <= 24. Values are sorted per axis, so the neighbourhood sweep's
"adjacent entries are neighbours" rule applies on all three axes (k has only
two values; its single neighbour is the other value). All points satisfy the
warm-up rule `L + 1 + atr_window <= bars_limit` (169 + 14 << 1000), so no
`WindowError` path can strand counted trials.

Trial counting. The trial counter currently counts one trial per (grid point,
symbol) because each pair was a separate deployment decision. Here the
deployment decision is the whole portfolio at one (L, k, R): one grid point =
ONE trial. New trials = 18, `trials.json` goes 123 -> 141 and the deflated
Sharpe uses `n_trials = 141`. The 18 are added before any run and, as in
`evaluate`, after the warm-up pre-check so a guard failure leaves no counted
trials. The pre-registration declares `expect: {new_trials: 18, total_after:
141}` and the runner refuses on mismatch, like `research_round2.yaml`.

Why 1 and not 7 per point: the selected object is one return stream (the book);
the per-symbol returns are not separate candidates one of which is picked. But
the 123 earlier trials remain in the count (they were real selections on the
same price history), and the book is built from the same 7 correlated assets
whose hold-out period was already looked at by 123 evaluations. So 141 is the
number the gate uses (fixed in advance), and the report ALSO prints, as a
disclosed non-gating sensitivity, the DSR p-value at `123 + 18 x 7 = 249`
trials, so the reader can see whether a survivor depends on this choice. If
the result is only a pass at 141 and a fail at 249, that is reported as a
fragile pass; it is not silently accepted.

## 3. Portfolio risk

The existing `RiskGate.check` stays unchanged and still sees EVERY order (entry,
stop, exit), one order at a time. Facts about it that shape the design: it caps
loss at the stop per order at 1% of equity; it knows other symbols only via
`sum(|qty| * entry_price)` of other positions (leverage 3x, uncapped by
direction), per-symbol notional <= 1x equity; it has no net-exposure notion and
no aggregate stop-risk notion. So aggregate limits live in a new pure function
used identically by the runtime and the simulator.

- Per-leg stops: YES, kept. Each leg gets the same protective stop as the
  runtime today: `close -/+ stop_atr_mult * ATR(atr_window)` (3.0, 14 from
  `runtime.yaml`, unchanged), rounded toward entry to the tick, placed as a
  reduce-only STOP_MARKET after the fill, through the gate. Reason: a relative
  strength book can lose on both sides at once (a squeeze on the short leg), and
  an exit schedule of days with no stop is exactly the unprotected exposure the
  runtime was built to avoid. The stop is a disaster stop, not the strategy
  exit; rebalance-driven exit remains the normal exit.
- Leg risk budget: each leg is sized with `size_position` on a risk fraction
  `leg_risk = max_risk_per_trade / (2k)` (PROPOSED), so if all 2k stops are hit
  the book loses about 1% of equity at stops, the same stop-risk as one
  single-symbol trade today. Implementation: `size_position` takes `cfg`;
  the portfolio code passes a copy of `RiskConfig` with
  `max_risk_per_trade = leg_risk` (the gate itself still uses 1% per order, a
  looser bound that the smaller leg always satisfies). Consequence, stated
  plainly: positions are small. With a 3 x ATR stop that is a few percent away,
  each leg's notional is a fraction of equity well under 1x and gross exposure
  well under 1x; on a USDT 1000 paper account some legs (BTC min notional 50)
  may fall below `min_notional` and be skipped, which shrinks and unbalances the
  book. Skips are recorded, counted, reported, and simulated identically. The
  risk fraction cannot be raised to avoid this without a new user decision.
- Aggregate caps (new `config/portfolio.yaml`, PROPOSED, checked before each
  entry on the book AFTER the new leg, using the gate's own valuation basis):
  `max_gross_frac: 1.5` (sum of |notional| / equity; tighter than
  `risk.yaml` 3x), `max_net_frac: 0.5` (|long notional - short notional| /
  equity). An entry that would breach is SKIPPED (logged), not shrunk, in the
  deterministic order above. With the leg risk above these caps are expected
  not to bind; they exist as a backstop and are tested, not tuned.
- Daily loss 3%, drawdown 10%: unchanged and portfolio-wide by construction
  (they use account equity). Same `_PaperState` baseline/peak/day-start logic,
  checked every cycle; breach -> kill switch.
- Kill switch: `KillSwitch.trigger` already calls `venue.flatten_all()` (cancel
  every order, close every position) and halts; nothing changes. The
  portfolio runtime checks `kill.halted` before every order in the per-leg
  loops (as `_cycle` does between actions), so a halt in the middle of a
  rebalance stops the remaining legs. Partial-rebalance state after a halt is
  irrelevant (everything is flat).
- Rebalance with a rejected exit or entry: a gate rejection or venue failure
  on one leg does not abort the other legs (each leg's outcome is read back);
  the end state may be a book that differs from `T`. That is recorded and
  simulated only for the deterministic cases (skip on size/gate/limits), not
  for random venue failures.

## 4. Runtime changes (minimal): new module `runtime/portfolio.py`

Decision: a separate small module with class `PortfolioRuntime`; the
single-symbol `Runtime`, `Deployment`, `load_deployed` and their tests are not
modified (acceptance check: the existing test suite and `golden_pre.json`
comparison pass unchanged, and `git diff` of `runtime/loop.py` is empty).

Reused as-is (imported): `RiskGate`, `KillSwitch`, `DecisionLog`, `Venue`
types, `size_position`/`floor_to_lot`, `client_order_id`, `check_clock_skew`,
`closed_bars`, `atr`, `protective_stop`, `check_continuity`, `last_unreset_halt`,
`reset_halt`/state-ready checks, `_PaperState` (private name, imported on
purpose so equity baseline/peak/day-start are the same code).

New and portfolio-specific:

- `xs_momentum.py` (strategies): pure `rank_targets(closes_panel, params,
  universe_order) -> dict[symbol, side]` plus `is_rebalance_bar`. The ONE
  function both runtime and simulator call. Not a `Strategy` (that protocol is
  single-symbol); it has its own small protocol.
- `portfolio_limits.py` (risk): pure `book_limits_ok(positions_after, equity,
  cfg) -> reason | None` for gross/net caps. Also shared by both.
- Cycle: every poll, in this order: clock check; account read; kill-switch
  equity checks; staleness of every symbol's newest bar; for each symbol in
  U `_ensure_protected`-equivalent (stop coverage); then, if the newest closed
  bar `t` (common to all symbols; the cycle decides on the LATEST bar for which
  ALL held-or-candidate symbols have a closed bar, otherwise waits until
  `stale_after_s` and treats the late symbol as ineligible) is a new rebalance
  bar not yet decided, run the rebalance.
- One decision per bar: `last_rebalance_bar` and a per-leg ledger are written
  to the state file BEFORE the first order is submitted (same invariant as
  now). Re-processing the bar, also after a restart, places nothing new.
- Deterministic client ids: `client_order_id(name, symbol, bar_open, kind)`
  unchanged, with `name = f"{deploy_name}"` and kinds `entry|exit|stop` per
  symbol; since each (symbol, bar, kind) occurs once per rebalance, ids are
  unique and a retry of the same decision is recognisable. The repair stop id
  keeps the existing "per symbol/size/bar" derivation.
- Stop coverage for EVERY leg, every cycle: the existing algorithm applied per
  symbol in U (coverage of `abs(qty)` by reduce-only stops on the closing side,
  one repair per uncovered episode, read-back before cancelling old stops,
  second failure -> kill switch, orphan orders on a flat symbol cancelled).
  The method bodies in `Runtime` are bound to `Deployment`, so they are
  re-implemented over `symbol` rather than refactored (refactoring would touch
  the frozen runtime). Mitigation for copy risk: the existing coverage
  scenarios in `tests/runtime/test_loop.py` are ported as a parametrised
  test over a 3-symbol book, plus new multi-leg cases (two legs uncovered in
  one cycle; a repair on leg A while leg B is fine).
- Reconciliation at start: read positions + open orders before trading. Any
  position or order on a symbol outside U -> refuse (or flatten with
  `flatten_unowned_on_start`, existing flag). Any position/order on a U symbol
  must match the persisted leg ledger (side, qty within one lot); a U position
  with no ledger entry, or a ledger leg with no position, is "unowned drift":
  refuse by default. Matching legs are adopted and stop coverage enforced.
  A symbol may not belong to both a single-symbol deployment and a portfolio
  deployment; `agenkit paper` refuses to start both kinds in one process, and
  uses a separate state file path for the portfolio (so no mixing of ledgers).
- Halt persistence: same rules (halt in state file and decision log; start
  refuses while the latest halt has no later `runtime/halt_reset`).
- State file (`data/portfolio_state.json`, atomic write like `_PaperState`):
  `{paper: {baseline, peak, day, day_start}, last_rebalance_bar, legs:
  {symbol: {side, qty, entry_bar, stop_client_id}}, halted}`.
- CLI: `agenkit paper --portfolio research/round3/deployed_portfolio.json`
  (flag; absent flag leaves current behaviour byte-identical). `agenkit
  smoke` is out of scope; the user-run single-symbol smoke already tests stop
  placement, but a multi-leg testnet check is a task in section 10.

`deployed_portfolio.json` schema (a JSON OBJECT, not a list, so the existing
`load_deployed` fails closed on it with its "expected a JSON list" error):

```
{
  "kind": "portfolio",
  "name": "xs_momentum",
  "universe": ["BNBUSDT","ADAUSDT",...],      // frozen order, 7 symbols
  "interval": "4h",
  "params": {"lookback": 84, "k": 2, "rebalance": 18},
  "allowed_regimes": ["trend","range"],       // restricted -> refused (as v1)
  "policy": "runtime",
  "simulator": "portfolio_policy_v1",
  "config_sha256": {"risk": "...", "runtime": "...", "portfolio": "...",
                     "gate": "...", "research_round3": "..."},
  "parity_test": "tests/runtime/test_portfolio_parity.py"
}
```

`policy: "runtime"` keeps its meaning: validated by the simulator the runtime
mirrors. It is only written by the report code when the verdict passed under
`backtest/portfolio_policy.py`. `load_portfolio_deployed` additionally refuses
if the params are not a grid point, the universe order differs from the
strategy's, the config hashes differ from the files on disk at start (a changed
risk/portfolio/runtime config invalidates the validation), or the universe has
a symbol without symbol rules.

## 5. Portfolio simulator: `backtest/portfolio_policy.py`

`run_backtest_portfolio(panel, params, rules, costs, risk, runtime_cfg,
portfolio_cfg, funding, ...)` returns a result shaped like `BacktestResult`
(equity curve, leg trades, skipped entries, halt info) so `compute_stats` and
the gate reuse it. The runtime is the reference; rules below copy
`runtime_policy.py` per leg and add the cross-leg ordering.

Per bar, in this order (mirrors what the runtime does in time):
1. Intrabar, every held leg, in universe order: exchange stop triggers if
   `low <= stop` (short: `high >= stop`), fills at stop or at the open if
   gapped through (existing rule), cost on the fill. A stop in the entry bar
   does not trigger (existing rule).
2. Funding: per held leg exactly as `runtime_policy` (`signed_qty * close *
   rate`, window `(open, open+step]`, payment at the close only if the leg
   survived).
3. Intrabar halt check: portfolio equity marked with EVERY leg at its adverse
   extreme simultaneously (long at low, short at high). This is a pessimistic
   bound (extremes of different symbols do not coincide in time), chosen
   because OHLC cannot say which combination existed at a poll; it can only
   cause a halt (which the gate rejects) that a real poll might have missed.
   Stated limit; not tuned. A breach flattens all legs at their extreme
   prices with costs, permanently halts (reason `halt`).
4. Close decision at the bar close: marked-to-close equity; daily-loss and
   drawdown check (same `_PaperState` rules, UTC day from bar close) -> flatten
   all at the close + halt. Then, if a rebalance bar: ranking via the shared
   `rank_targets`, exits, entries in the shared order; each entry sized with
   `size_position` on the simulated equity with the real `RiskGate.check` and
   the shared `book_limits_ok`; skip counted otherwise. Costs on each fill use
   `CostModel.per_side_rate` on `qty * price`, per leg, entry and exit.
5. Signals are a pure function of the last `L+1` closes and the epoch-anchored
   schedule, so the rolling-window issue that required `window_signals` for
   single symbols does not arise for the signal. It still must be proven: the
   parity test feeds the runtime a rolling window of `bars_limit` rows and the
   simulator the full panel and requires identical targets.
6. Panel: all 7 symbols share one timestamp index (asserted at load, else
   `DataGapError`; no alignment repair). Ineligibility is representable (NaN
   row) for tests only.
7. Equity at bar `t` is marked before that close's decisions fill (existing
   convention, `equity[0] == equity0`); the last bar liquidates all open legs.

Parity test plan (`tests/runtime/test_portfolio_parity.py`, modelled on
`test_parity.py`): `FakeVenue` extended (in the test support module, not
production code) to serve 3 to 7 symbols and apply the same fill/stop rules as
the simulator. Run `PortfolioRuntime` bar by bar on seeded synthetic panels
and `run_backtest_portfolio` on the same panels; require equal, within 1e-9:
the order sequence (symbol, side, qty, client id), stop prices, equity per bar,
leg trade ledger, skipped entries with reasons, halt bar and reason.
Scenarios (each its own test): k=1 and k=2; ties; stop-out of one leg then
re-entry at the next rebalance; kept leg across rebalance; leg sized below
min_notional (skip); gross/net cap skip; daily-loss halt; intrabar halt;
drawdown halt; ineligible symbol (gap) incl. a held symbol going ineligible;
fewer than `2k+1` eligible; restart mid-period (new runtime on the persisted
state places nothing); funding on legs of both signs; rolling window vs full
history. A mutation check: perturbing each shared rule in the simulator (cost
side, stop fill price, tie-break) must make at least one parity test fail.
Cannot cover (same as today): real exchange stop fills, matching engine,
testnet fees, live slippage.

## 6. Validation gate for a portfolio

New `validation/portfolio_gate.py` with `evaluate_portfolio(...)` that REUSES
`split_by_time`, `compute_stats`, `holdout_reasons`, `passes_cost_check`,
`deflated_sharpe_pvalue`, `neighbourhood_sweep`'s scoring and `GateConfig`
(thresholds read from `config/gate.yaml`, unchanged; none is edited).

Protocol, identical to `evaluate` with the portfolio as the unit:
1. Split the common index at `holdout_frac = 0.3`; counter adds 18 before any
   run (section 2).
2. Lookahead check on the portfolio: truncation/perturbation test on every
   symbol's future bars; any change in `T(t)` rejects.
3. Selection by a neighbourhood sweep over the 18 points on TRAIN only: the
   chosen point is the one whose score and neighbours (adjacent L, k, R)
   keep >= `1 - neighbour_tolerance` of the best (same rule and
   `neighbour_tolerance` as `neighbourhood_sweep`, now 3-D adjacency).
4. Hold-out evaluated once with that point; hold-out simulator gets train bars
   as ATR history, rebalance schedule epoch-anchored so the split is invisible.
5. Checks: not ruined; not halted; trades >= `min_trades`; net return > 0;
   Sharpe >= `min_sharpe`; drawdown <= `max_drawdown`; PF >= `min_profit_factor`;
   win rate >= `min_win_rate`; sweep stable; 2x costs positive return, PF > 1,
   no ruin/halt; DSR p-value <= `max_trial_adjusted_pvalue` with `n_trials =
   141` and `n_obs` = hold-out bars, skew/kurt of the hold-out bar returns.
6. Regime check: labels from the efficiency ratio (`label_regimes`) computed
   on an equal-weight index of the 7 closes (fixed in advance; BTC alone would
   be an arbitrary choice). Bar return attribution and the per-regime rule
   (`min_trades_per_regime`, positive net, positive Sharpe) are as today, with
   a leg trade assigned the regime of the bar before its entry. As today, a
   result that "has edge" in only one regime passes the gate with
   `restricted_to` set and is then refused by the runtime (v1 rule): such a
   portfolio is not deployable. That is stated, not worked around.

What "trades" means. Counted: one trade = one leg round trip (entry fill to
exit fill of one symbol, including stop-outs), `ret` = net return on equity
at entry including both legs' costs and funding, exactly the existing row
format. Reasons: (a) profit factor and win rate are defined on trades and the
single-symbol rows already are leg round trips, so the same formulas apply
with no new definitions; (b) a kept leg across rebalances is one trade, so
turnover is not inflated by a rebalance that changes nothing; (c) counting
"per rebalance" would make a book that holds the same four legs for a year
count as 50 trades, which is an inflated sample. Caveat I do not hide: legs
are correlated (market beta, simultaneous entries), so 30 leg trades carry
far less than 30 independent observations; `min_trades = 30` alone is a weak
guard for a portfolio. To make it meaningful WITHOUT loosening anything, the
pre-registration adds one stricter, additive condition: the hold-out must
contain at least 30 rebalance bars at which a full book of 2k legs was
attempted (reported as `rebalances_attempted`). Rough scale only: the hold-out
is about 30% of roughly 12.6k bars (about 3.8k bars); R=42 gives on the order
of 90 rebalances there and R=6 on the order of 630, so the extra condition
rarely bites for these grids, but it is the honest unit and costs nothing.
Skipped entries are reported per leg and per rebalance.

## 7. Data

Reuse what round 2 loaded: 4h klines and funding for the 7 symbols,
2021-01-01 to 2026-09-30 (end-exclusive 2026-10-01), via `data/archive.py`
`load_klines` / `load_funding` from the checksummed `data/cache`; no network
needed if the cache is complete, and any cache miss or checksum error stops
the run (no substitution). Same period and the same 30% split. Panel build
asserts an identical index across symbols. Funding rate stamped at funding
time, applied per leg as in `runtime_policy`. Symbol rules from
`config/symbol_rules.yaml` (testnet snapshot; the runtime re-reads live rules).
The rule snapshot is pre-registered by file hash.

## 8. Costs

Current assumptions from `config/research.yaml` unchanged: taker 5 bps/side,
1 bp half-spread, 2 bp slippage, plus the existing 2x check. Applied per leg
fill. No smoke-measured costs exist yet. If the user sends the smoke log before
the pre-registration is frozen, one fixed cost file is committed in the
pre-registration; after freezing, costs do not change. Honest note: relative
strength trades the less liquid perps (DOGE, ADA, LINK, AVAX) on BOTH sides
with market orders, and 1 bp half-spread plus 2 bp slippage is likely
optimistic for them. The 2x check is the only robustness test of that.

## 9. Honest limits and prior

- Prior: low. Two rounds of single-symbol rules produced 0 survivors; a null
  result is the most likely outcome and is an acceptable one.
- 7 highly correlated crypto assets: ranking among them is dominated by one
  market factor and a few idiosyncratic episodes; the effective cross-section
  is small. k=2 of 7 means 4 of 7 assets are in the book at any time.
- The hold-out is not virgin. These symbols and this period were evaluated 123
  times in rounds 1 and 2, and I (and the user) have seen the history of what
  failed; design choices in this document are not independent of that. The
  DSR count of 141 does not capture this, which is why section 2 prints the
  249-trial sensitivity.
- Small paper account (1000 USDT) and 1% total stop-risk means small positions
  and possibly skipped legs; results are Sharpe-comparable but dollar returns
  are tiny.
- Simulator limits as today: stop fills are a model, costs are assumptions,
  funding is simulated, the intrabar halt is a conservative bound for a book.
- Short legs carry borrow-like risk only through funding here; squeezes in
  thin alts are not modelled beyond the ATR stop and its gap fill.
- One pass over one 30% slice; no claim beyond this period.

## 10. Task breakdown (each independently testable, each with its review)

Order matters; each task ends with a review (spec + code quality) before the next.

1. `xs_momentum.py` pure ranking + schedule + eligibility, with tests for
   definitions in section 1 (ties, ineligible, `<2k+1`, epoch anchor, no
   lookahead truncation property test). Small (~150 lines + tests).
2. `portfolio_limits.py` + `config/portfolio.yaml` loader + tests. Small.
3. `backtest/portfolio_policy.py` simulator + hand-computed-bar unit tests for each rule in section 5
   (the single-symbol simulator cannot be reused as an oracle: a book has no
   one-leg case). Large (~500 lines). Review focused on
   ordering rules in section 5.
4. `runtime/portfolio.py` `PortfolioRuntime` (cycle, rebalance, state,
   reconciliation, per-leg stop coverage) + tests ported from `test_loop.py`
   + multi-leg cases. Large (~700 lines). Review must verify the single-symbol
   runtime diff is empty.
5. Parity suite `test_portfolio_parity.py` with extended `FakeVenue` and the
   mutation check. Medium. This is the acceptance gate for 3 and 4; any
   disagreement is fixed in the simulator OR runtime by reading the runtime as
   the reference, with the review deciding which side is wrong.
6. `validation/portfolio_gate.py`, sweep over 3-D grid, portfolio lookahead
   check, DSR with 141, 249 sensitivity, `rebalances_attempted`; report writer
   and `deployed_portfolio.json` + loader (`load_portfolio_deployed`) with
   tests that a research-policy or hand-edited file is refused. Large.
7. `scripts/run_portfolio_research.py` + `config/research_round3.yaml` with
   `--plan` and `expect` check; plan-only run produces counts, no data load.
   Small. Runs only after the pre-registration is committed.
8. CLI wiring (`agenkit paper --portfolio`), refusal matrix tests (both kinds
   at once, symbol overlap, missing state, halted). Small-medium.
9. Optional, user-run: multi-leg testnet smoke (open 2 legs, verify stops,
   kill switch flattens both), added to `smoke.py`. Medium; needs testnet keys.

Tasks 1-2 and 3-4 can proceed in parallel; 5 needs 3 and 4; 6 needs 1-3; 7 needs 6.
No research run until 1-8 are reviewed and the pre-registration is frozen.

## 11. To freeze in a separate pre-registration BEFORE any data touches the strategy

- Universe and its frozen order; interval; start/end; hold-out fraction 0.3.
- Exact definitions in section 1 (score, eligibility, schedule anchor,
  tie-break, `2k+1` rule, keep/exit/re-enter semantics, rebalance order).
- The 18-point grid, `expect: new_trials 18, total_after 141`; the 249
  sensitivity as disclosed, non-gating.
- Risk: `leg_risk = 1%/(2k)`, `stop_atr_mult 3.0`, `atr_window 14`,
  `max_gross_frac`, `max_net_frac`, `bars_limit`; hashes of `risk.yaml`,
  `runtime.yaml`, `portfolio.yaml`, `gate.yaml`, `research.yaml` (costs),
  `symbol_rules.yaml`.
- Simulator rules of section 5 (event order, adverse-extreme halt) and the
  commit hash at which the parity suite passed.
- Gate: `gate.yaml` unchanged; definition of trade (leg round trip) and the
  additive `rebalances_attempted >= 30`; regime index definition; selection
  rule (3-D neighbourhood on train only); one pass, no changes after results.
- Cost file (current assumptions, or the single smoke-derived file if it
  exists before freezing).
- What happens with the result (null result reported; `agenkit paper` still
  refuses on empty/absent deployment; no loosening).

## 12. Open questions for the user

1. Leg risk size. Recommendation: `1%/(2k)` (book stop-risk equals one
   single-symbol trade). Alternative `1%/k` doubles position size but lets a
   bad rebalance lose about 2% at stops. Smaller means more legs skipped by
   min-notional on a 1000 USDT account. Decide before the pre-registration.
2. Include the multi-leg testnet smoke (task 9) as a precondition for
   `agenkit paper --portfolio`, even if the research finds a survivor?
   Recommendation: yes, and treat the cost smoke (step 0) as the same session.
3. Gating trial count: accept 141 (one trial per portfolio grid point) as the
   gate value with 249 reported only as sensitivity? Recommendation: yes;
   a stricter alternative is to gate on 249, at the price of making a survivor
   very unlikely.
4. Is a regime-restricted survivor acceptable to report as "gate-passed but
   not deployable" (the existing v1 rule)? Recommendation: yes, report it
   exactly so.
