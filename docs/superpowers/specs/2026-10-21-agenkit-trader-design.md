# AgenKit Trader: Design Spec

Date: 2026-10-21. Status: draft for review.

## 1. Purpose

A 24/7 autonomous trading agent for Binance USDT-M perpetual futures that trades only strategies which survive rigorous, cost-aware, out-of-sample validation. Survival is prioritised over an impressive backtest.

Success criteria:
- Several candidate strategies are generated; weak ones are rejected with logged reasons; only survivors run.
- Risk limits and the kill switch are enforced in code and cannot be overridden by any model or strategy.
- Paper trading on the Binance Futures testnet runs unattended with a full decision log.
- No performance figure appears in any document or log unless a recorded backtest or paper run produced it.
- Live trading is impossible without the human-only unlock in section 7.

Non-goals: guaranteeing profit, an LLM in the order path, a web UI, multi-exchange support in v1, HFT.

## 2. Venue and constraints

- Market: Binance USDT-M perpetuals. Paper venue: Binance Futures testnet (reachable from the cloud sandbox).
- Historical data: the official bulk archive at `data.binance.vision` (klines, funding rate, metrics including open interest), reachable from the sandbox.
- Binance mainnet API and Bybit are blocked from the sandbox (HTTP 451 / 403). Live mode must run on a machine the user controls.
- Fees, minimum notional, tick/lot sizes, latency and rate limits are read from the exchange info endpoint and stored with each backtest, never hard-coded.

## 3. Architecture

Python 3.12+, `src/agenkit_trader/`, tests in `tests/` (pytest, written first).

Two halves with a strict boundary:

Research (offline):
- `data/` download, verify and cache archive files; align by timestamp using only information available at bar close.
- `backtest/` event-by-bar simulator with taker/maker fees, spread, slippage and funding payments.
- `validation/` time-based train/hold-out split (never random), parameter-neighbourhood sweep, trial counter, multiple-testing adjustment, report writer.
- `strategies/` plain versioned rule functions: `signal(bars, params) -> target position`. Signals are separate from execution.

Runtime (online):
- `risk/` single choke point `RiskGate.check(order, state)`. Every order passes through it.
- `execution/` `Venue` interface with `BinanceTestnetVenue` (v1) and a `LiveVenue` stub that raises unless unlocked.
- `runtime/` loop: fetch bars, run deployed strategies, size, risk-check, execute, reconcile.
- `killswitch/` flatten all positions and halt.
- `log/` append-only JSONL: inputs, output, result for each decision.

No module in `runtime/` imports `validation/` internals; it reads a signed-off `deployed.json` produced by the validation gate.

## 4. Strategy candidates

Each candidate must state in one sentence the edge, who is on the other side, entry, exit, stop placement, holding period and expected trade frequency, and why the edge should persist. Initial candidates, to be tested rather than assumed to work:

1. Funding-rate extreme mean-reversion: crowded positioning pays funding to the other side.
2. Trend/breakout with a volatility filter: slow trend persistence in crypto.
3. Short-horizon mean-reversion after liquidity-driven overshoots, with a trend/regime filter.

Prefer simple rules with an economic reason over complex rules fitted to history.

## 5. Validation gate

A strategy is deployable only if all hold, net of realistic costs:
- Positive expectancy and acceptable Sharpe, max drawdown, win rate and profit factor on the hold-out period (thresholds fixed in `config/gate.yaml` before the first run).
- Minimum trade count on the hold-out.
- Parameter-neighbourhood sweep: performance degrades gracefully; a single-setting win is rejected.
- Cost sensitivity: still positive at 2x fees and slippage.
- Multiple-testing: every variant tried is counted and the reported significance is adjusted for that count.
- Regime check: results split by trend/volatility regime; dependence on one regime is flagged and the strategy is restricted to it with a stay-out rule.

Output: a comparison table (Strategy, Edge, Market, Timeframe, Sharpe, Max Drawdown, Trades, Cost Sensitivity, Verdict) generated from real runs only. Rejected strategies are kept with reasons.

## 6. Risk and kill switch (hard limits in code)

Defaults, configurable only by editing `config/risk.yaml`, with the file hash logged at startup:
- Paper account: 1,000 USDT.
- Max risk per trade: 1% of equity.
- Max daily loss: 3%. Max drawdown: 10%.
- Sizing: volatility targeting or fractional Kelly capped at 0.25; full Kelly is never used.
- Max leverage and max position notional per symbol and in total.
- Stale-data check: reject if the latest bar is older than a threshold.
- API error limit: consecutive or per-window errors trigger halt.

The kill switch flattens all positions and halts on: daily loss or drawdown breach, stale data, error limit, a per-strategy live-failure rule (section 8), or a manual command. It is idempotent and tested with a fake venue. After a halt, restart requires a manual command.

## 7. Live-trading gate

- Live mode is off by default and the agent cannot enable it.
- Enabling requires the user to: set `live.enabled: true` in a config file, supply API keys through environment variables, set risk limits, and type a confirmation phrase at startup.
- Paper must have run for a minimum duration and trade count (set in config) before the CLI will accept the unlock.
- Start at the smallest allowed size. Nothing here promises profit.
- Live mode runs off-sandbox because mainnet is geo-blocked here.

## 8. Bear case and automatic stops

Each deployed strategy ships with stop criteria derived from its validated bounds, for example live drawdown beyond the hold-out maximum, hit rate or expectancy outside its confidence band after N trades, or cost drift above the assumed level. Breach halts that strategy automatically. The bear-case review covers overfitting, lookahead, survivorship (delisted symbols in the archive), cost sensitivity, regime dependence, crowded trades, liquidity traps and single points of failure.

## 9. Testing (test-first)

- Unit: risk gate, kill switch, sizing, cost model, lookahead guard (shifting future data must break the test), data alignment.
- Property tests: no order exceeds limits for random states.
- Integration: runtime loop against a fake venue; reconnect, stale data, error bursts.
- Validation harness tests with synthetic data where the true edge is known (a planted edge passes, noise fails).
- CI runs lint, types and tests.

## 10. Build order

1. Repo scaffold, config, logging.
2. Data layer and lookahead guard.
3. Backtester with costs.
4. Risk gate and kill switch.
5. Validation harness.
6. Strategy research (real runs, report committed).
7. Testnet paper executor and `paper` command.
8. Live adapter stub and unlock flow.

## 11. Assumptions to verify

- Archive coverage and quality for the chosen symbols and period.
- Testnet liquidity and fills are not representative of mainnet, so paper results overstate fill quality.
- Binance fee tier assumptions; maker/taker rates come from the user's actual tier.
- Edges found may already be arbitraged away. A null result (no strategy passes) is an acceptable outcome and will be reported as such.

## 12. HOW COULD THIS SYSTEM FAIL?

Filled in after the research run and the Task 7/8 builds. Everything below is either measured, observed in code review, or stated as an unverified assumption. Nothing here is a claim of profitability.

**Research: a null result.**
- One pass over BTCUSDT and ETHUSDT, 1h bars, 2021-01-01 to 2026-10-01: 60 trials.
- 0 of 6 strategy x symbol candidates passed the validation gate. `research/deployed.json` is empty, so `agenkit paper` refuses to start.
- The best-looking candidate, ETHUSDT trend_breakout, had a hold-out Sharpe of 0.58, but was rejected for:
  - hold-out drawdown of 52.1% (limit 25%);
  - an unstable parameter sweep;
  - a trial-adjusted p-value of 0.94.
- `funding_reversion` is untested, not refuted. The hold-out held 0 BTCUSDT and 1 ETHUSDT funding settlements beyond +-0.03%, against 430 and 496 in train, so it made 0 and 1 trades.
- Any change to a strategy needs a new research run, counted in `research/trials.json`.

**Costs are assumptions.** The 1 bp half-spread and 2 bp slippage in `config/research.yaml` were not measured, because the mainnet order book is unreachable from the sandbox. The gate re-tests at 2x costs, but real costs could still be higher, and the account's real fee tier is unknown. Fees are not read back from fills: `Fill.fee` is None and the decision log says "fee unknown".

**Testnet vs mainnet fills.** Paper trading runs on the Binance Futures testnet. Its liquidity, spreads, fills and funding are not mainnet's, so paper results can flatter or distort live behaviour. Research used mainnet archive data; the runtime uses testnet prices. No live fill has ever been observed.

**Exchange API changes.**
- Since 2025-12-09, `POST /fapi/v1/order` rejects STOP_MARKET (-4120). Protective stops therefore use the algo-order endpoints.
- That change was verified against the production Binance docs only. Two things are unverified: that the testnet has the same migration, and the response shape of `openAlgoOrders`.
- The signed endpoints were tested against mocks only, because no testnet keys were available. The first real paper run must confirm that a stop is actually placed.
- Any future API change can break order or stop handling. Each such failure is counted as an API error, and five errors within 300 s trip the kill switch.

**In-memory repair state.** The stop-repair bookkeeping (one repair per uncovered episode) is held in memory only. A crash-looping process gets one repair attempt per restart. A persisted halt blocks restart once the kill switch has tripped, but repeated restarts without a halt are not bounded.

**Regime labels are not reproducible at runtime.** Research labelled regimes with an expanding median over years of history; the runtime fetches at most 1499 bars. With the default 1000-bar window (`bars_limit: 1000`), the labels differed on about 4% of bars, as measured in the Task 7 review; the difference at other window lengths was not measured. A deployment restricted to some regimes is therefore refused at start (fail closed). Only unrestricted deployments can run.

**Unowned positions.**
- Ownership is per symbol: one deployed strategy per symbol.
- A position or open order on a symbol no deployed strategy owns makes start refuse. With `flatten_unowned_on_start: true`, it instead trips the kill switch.
- The kill switch's `flatten_all` closes every position on the account, including manual ones.
- Hedge mode is not supported.
- Paper equity assumes no external transfers and no testnet resets.

**Narrow universe.** Only two symbols (BTCUSDT, ETHUSDT) were researched. This is survivorship bias: both are the largest, longest-lived perpetuals. Nothing says the results, null or otherwise, generalise.

**No live execution.** Live order placement is not built. `agenkit live` validates the unlock gate only, and `LiveVenue` refuses every call. Every live-specific failure mode (real fills, mainnet rate limits, real liquidation risk) is untested. Live would also have to run off-sandbox, because mainnet is geo-blocked here.

**Clock skew.**
- Skew over 1000 ms against the venue's server time, measured at the request midpoint and re-synced every cycle, trips the kill switch.
- Closed bars are judged with `min(server time, local time)` plus a 2 s grace.
- A host with a drifting clock will halt repeatedly rather than trade on wrong bars.

**Single points of failure.**
- One process on one host, with no supervisor or failover.
- One exchange (Binance) and one network path.
- One state file and one decision log on local disk. Deleting both and passing `--init-state` is an unpreventable bypass of a persisted halt, though it is printed and logged.
- The local clock.
- The environment variables that hold the keys.
- The kill switch flattens through the same venue API it guards. If that API is down, the flatten fails (logged `flatten_failed`, and the system stays halted). Only the exchange-side reduce-only stops then protect open positions.
- There is no client-side rate limiting. A 429 is counted as an API error and the loop waits the venue's `Retry-After` (at least one poll interval, at most 900 s, 60 s if absent); a 418 (IP ban) trips the kill switch, whose flatten then likely fails against the same ban, leaving the exchange-side stops as the only protection.

**The runtime does not trade what research validated.** A research pass does NOT validate the runtime's sizing or stop policy. Research backtests each signal's fractional position (e.g. trend_breakout's vol_cap scaling up to 1x equity) with no stop-outs; the paper runtime keeps only the sign of the signal, sizes for 1% risk at a 3 x ATR(14) exchange-side disaster stop (roughly 0.4x equity notional on ETH 1h, an illustrative estimate) and does not re-enter after a stop-out until a fresh signal transition. The committed run's backtest simulated neither the stop nor that rule. A runtime-policy simulator (`backtest/runtime_policy.py`) now mirrors them, checked by a parity test against the real runtime loop on synthetic bars; `scripts/run_research.py --policy runtime` is the default for the next run, and the report records the policy. It still cannot validate real exchange stop fills, testnet fills or how a 1h-ATR stop interacts with live slippage, and it does not simulate the kill switch. The next run is a new counted trial, for the user to decide.

**Operator error when unlocking.** The live gate is human-only and checks process, not judgement. The following are all outside what the code can prevent:
- editing `config/live.yaml` by mistake;
- loosening risk limits before paper trading (they must then hold for the whole paper record);
- exporting keys with withdrawal permission or without an IP allowlist;
- hand-editing the decision log, which the gate trusts;
- going live on a strategy that never passed validation.

`docs/GOING_LIVE.md` lists the checklist, and every unlock attempt is logged.
