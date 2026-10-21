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

To be completed with measured results after research. Known risks at design time: overfitting despite the gate, testnet fills flattering results, exchange outage or API change, data feed gaps, regime change, correlated positions across strategies, operator error when unlocking live mode.
