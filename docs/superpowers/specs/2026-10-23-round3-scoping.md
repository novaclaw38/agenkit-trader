# Round 3 scoping (DRAFT, not pre-registered, nothing run, no data loaded)

Purpose: decide what a third research round could test, what it costs to
build, and what must exist first. This is a scoping note, not a frozen
pre-registration. A frozen document (like round 2's) is written only after the
user approves a direction.

## Where we stand (facts)

- Round 1: 0 of 6 passed (60 trials). Round 2: 0 of 14 passed (63 more, total
  123). Nothing is deployable; `agenkit paper` refuses to start.
- Costs used so far (1 bp half-spread, 2 bp slippage, 5 bp taker fee) are
  assumptions. Fills have never been measured on any exchange.

## Step 0: measure real costs (blocked on the user)

The user runs `agenkit smoke` on the Binance futures testnet with testnet keys
and sends back `data/smoke/smoke_log.jsonl`. This settles whether the exchange
stop works and gives the first real fill prices. Caveat: testnet books are
thin and unrepresentative, so testnet slippage is not mainnet slippage. A
mainnet measurement would need read-only order-book snapshots from a machine
that can reach mainnet (the cloud sandbox is geo-blocked), recorded before
any research round and used as a fixed, committed cost assumption.

## Candidate directions

A. Cross-sectional relative strength (long the strongest, short the weakest of
   N liquid perps, rebalanced daily or every 4h). Feasible in principle: price
   data only, same exchange. Needs NEW work: a portfolio-level signal across
   symbols, per-symbol sizing under the 1% risk / 3% daily / 10% drawdown
   limits, a runtime that decides on several symbols per cycle, a
   portfolio-aware backtest simulator, and parity tests against the real
   runtime. Roughly the size of the earlier backtest-alignment work plus the
   runtime change, each needing independent review.
B. Funding or basis carry. NOT feasible in this build: a real carry trade needs
   an offsetting spot or cross-venue leg, and this system trades one futures
   venue with no spot account. A directional "funding-only" version is just
   the funding_reversion strategy, already tested (too few trades to judge).
   Do not build B without a hedge leg and a separate risk review.
C. Keep searching single-symbol rules: not recommended. Two rounds failed, and
   each new trial raises the deflated-Sharpe bar for everything later.

## Proposed round-3 design if A is approved (to be frozen before data)

- Universe: the 7 symbols that loaded cleanly in round 2 (BTC, ETH, BNB, DOGE,
  ADA, LINK, AVAX), 4h bars, same period, same 30% hold-out, same gate file
  unchanged, runtime policy extended to a portfolio.
- Signal family: rank by trailing return over L bars (L in a small fixed grid,
  e.g. 3 values), hold top-k / bottom-k (k in {1, 2}), rebalance every R bars
  (R in a small fixed grid). Total grid declared and counted before any run;
  the trial counter continues from 123.
- Costs: the real measured values from step 0 if available, else the current
  assumptions, plus the existing 2x-cost check.
- Pass criteria: unchanged from gate.yaml. Null result is acceptable.
- Prior: low. Zero survivors is the most likely outcome and would be reported
  as such.

## Decisions needed from the user

1. Approve building direction A (cost: a portfolio runtime and simulator plus
   reviews), or stop here and treat the system as a verified tool.
2. Run the smoke test and send back the log (step 0).
3. Confirm the gate stays strict: thresholds in gate.yaml are not loosened to
   obtain a survivor.
