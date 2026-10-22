# Pre-registration: research round 2 (written before any round-2 data is loaded)

Status: DRAFT for user approval. Nothing in this document has been run. Once
committed, the sections below are frozen: any change after results are seen
is a new, counted round and must say so in `research/NOTES.md`.

## Why a second round

Round 1 (BTCUSDT/ETHUSDT, 1h, 60 trials) passed 0 of 6 candidates. Its
candidates were fast (1h) and only two symbols. This round tests whether
slower trend rules on a wider, liquid universe survive the SAME gate. It is a
search for survivors, not an expectation of them. The prior is that most or
all candidates will be rejected; a null result is an acceptable outcome and
will be reported as such.

## Universe (chosen by me, on liquidity and history, before seeing any data)

BTCUSDT, ETHUSDT, BNBUSDT, SOLUSDT, XRPUSDT, DOGEUSDT, ADAUSDT, LINKUSDT,
AVAXUSDT, LTCUSDT. All were USDT-M perpetuals with history from 2021-01-01
(to be confirmed by the archive loader; a symbol with a gap or late listing
is DROPPED and recorded, never repaired or substituted). The list is not
altered after results.

## Timeframe

4h bars. Reason: round 1 used 1h with `lookback` up to 336 bars (14 days) and
still turned over often; costs are per trade, so fewer, slower trades give
costs less weight. 4h also keeps a runtime window (1000 closed bars ~ 166
days) long enough for the warm-up (722 bars for the existing volatility
reference). Period: 2021-01-01 to the latest complete month in the archive;
hold-out = last 30% (gate.yaml, unchanged).

## Candidates and grids (all counted as trials)

A. `trend_breakout` (existing code, unchanged), 4h.
   lookback in {42, 84, 168} bars (7, 14, 28 days), vol_cap fixed at 1.5.
   3 params x 10 symbols = 30 trials.
B. `trend_funding_filter` (NEW; trend_breakout signal plus a crowding filter):
   suppress a new long entry when the latest available funding rate is
   >= +thr, suppress a new short entry when it is <= -thr; exits unchanged.
   lookback in {42, 84, 168}; thr in {0.0003, 0.0006} (per 8h funding
   interval); vol_cap fixed at 1.5. 6 params x 10 symbols = 60 trials.
   Edge claim: extreme funding marks crowded positioning, so trend entries
   against low-funding sides fail less often. Funding enters with the
   existing availability lag; no lookahead.

Total new trials: 90. The trial counter goes from 60 to 150 and the deflated
Sharpe uses 150. Not run: cross-symbol relative strength (needs runtime
work), any parameter outside the grids above.

## Procedure (unchanged from round 1)

- Policy: `--policy runtime` (1% risk at a 3xATR(14) stop, 3%/10% halts,
  intrabar halt check, rolling 1000-bar window).
- Costs: research.yaml as committed (taker 5 bps/side, 1 bp half-spread, 2 bp
  slippage; the spread/slippage numbers are assumptions, not measurements).
- Gate: config/gate.yaml exactly as committed; 2x cost check; regime check;
  parameter-neighbourhood sweep; deflated Sharpe over 150 trials.
- One pass. No rule, threshold, grid, symbol or period changes after seeing
  results. The runtime refuses a deployment not validated under `runtime`.
- A (strategy, symbol) pair is a separate deployment decision; passing for
  one symbol does not deploy the others.

## What happens with the result

- Zero survivors: reported as a null result. `agenkit paper` still refuses to
  start. No promise of profit; no loosening of the gate.
- Survivors: they are paper-trade candidates only (testnet, then the live
  lock in docs/GOING_LIVE.md), after the first-paper-run checklist.

## Known limits of this design

- 90 trials on 10 correlated crypto symbols is multiple testing; the deflated
  Sharpe treats trials as independent, which may be too lenient for
  correlated symbols and too strict for the neighbourhood sweep.
- Hold-out is one 30% slice (~1.5-2 years) of one regime-heavy period.
- Costs are assumptions; funding paid/received is simulated, not logged by
  the runtime.
- Runtime-policy sweeps are slow (see README); the run is long.
