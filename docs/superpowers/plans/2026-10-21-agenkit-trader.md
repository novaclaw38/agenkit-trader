# AgenKit Trader Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a research-first autonomous trading agent for Binance USDT-M perps that deploys only validated strategies, enforces risk limits in code, and paper trades on the Binance Futures testnet.

**Architecture:** An offline research half (data, backtest, validation, strategies) produces a `deployed.json`; an online runtime half (risk gate, venue, loop, kill switch) reads it. Every order passes through `RiskGate`. No LLM sits in the order path.

**Tech Stack:** Python 3.12+, pandas, numpy, httpx, pyyaml, pytest, hypothesis, ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-10-21-agenkit-trader-design.md`

## Global Constraints

- Paper account 1,000 USDT; max risk per trade 1% of equity; max daily loss 3%; max drawdown 10%; fractional Kelly capped at 0.25, never full Kelly.
- Validation splits are by time, never random. Every variant tried is counted.
- Fees, tick/lot sizes and min notional come from the exchange info endpoint, never hard-coded.
- No performance number appears in any doc or log unless a recorded backtest or paper run produced it.
- Live mode is off by default; the agent cannot enable it; mainnet is unreachable from the sandbox.
- Every runtime decision is logged as append-only JSONL with inputs, output and result.
- Tests first. Package at `src/agenkit_trader/`, tests at `tests/`.

## Review Focus

- Missing or duplicated bars in the archive: gaps must be detected and refused, not silently interpolated.
- A strategy with zero trades on the hold-out: stats return a rejection, not a ZeroDivisionError or NaN pass.
- Order quantity rounds below the exchange min notional or lot size: order is rejected with a reason, not sent.
- Process restarts while a position is open: runtime reconciles with venue state before trading.
- Local clock skew against the exchange: signed requests use server time offset; skew beyond a threshold halts.

---

### Task 1: Scaffold, config, decision log

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `README.md`, `config/risk.yaml`, `config/gate.yaml`, `src/agenkit_trader/__init__.py`, `src/agenkit_trader/config.py`, `src/agenkit_trader/log.py`
- Test: `tests/test_config.py`, `tests/test_log.py`

**Interfaces:**
- Produces: `RiskConfig` and `GateConfig` frozen dataclasses; `load_risk_config(path: Path) -> RiskConfig`; `load_gate_config(path: Path) -> GateConfig`; `DecisionLog(path: Path).write(kind: str, inputs: dict, output: dict, result: str) -> None`; `file_sha256(path: Path) -> str`.
- `RiskConfig` fields and defaults: `start_equity=1000.0, max_risk_per_trade=0.01, max_daily_loss=0.03, max_drawdown=0.10, kelly_cap=0.25, max_leverage=3.0, max_symbol_notional_frac=1.0, stale_after_s=120, max_api_errors=5, error_window_s=300`.
- `GateConfig` fields: `holdout_frac, min_trades, min_sharpe, max_drawdown, min_profit_factor, cost_multiplier=2.0, max_trial_adjusted_pvalue`. Numeric thresholds are proposed in `config/gate.yaml` and committed before any backtest runs; the user approves them in review of this task.

- [ ] **Step 1: Write failing tests.** `test_config.py::test_risk_defaults_match_spec` asserts the defaults above; `test_rejects_nonpositive_limits` asserts `ValueError` for `max_daily_loss<=0`, `kelly_cap>0.25` or `kelly_cap<=0`. `test_log.py::test_append_only_jsonl` writes two records and asserts two lines, each parseable with keys `ts, kind, inputs, output, result`, and that a second `DecisionLog` on the same path appends.
- [ ] **Step 2: Run** `pytest tests/test_config.py tests/test_log.py -v`. Expected: FAIL (modules missing).
- [ ] **Step 3: Implement** the interfaces above; `pyproject.toml` with the stack and `ruff`/`mypy` config.
- [ ] **Step 4: Run** the tests, `ruff check .`, `mypy src`. Expected: all pass.
- [ ] **Step 5: Commit** `feat: scaffold, config and decision log`.

### Task 2: Data layer and lookahead guard

**Files:**
- Create: `src/agenkit_trader/data/archive.py`, `src/agenkit_trader/data/align.py`, `src/agenkit_trader/data/lookahead.py`
- Test: `tests/data/test_archive.py`, `tests/data/test_align.py`, `tests/data/test_lookahead.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `load_klines(symbol: str, interval: str, start: date, end: date, cache_dir: Path) -> pd.DataFrame` indexed by UTC `open_time`, columns `open, high, low, close, volume`.
  - `load_funding(symbol: str, start: date, end: date, cache_dir: Path) -> pd.Series` indexed by UTC `funding_time`.
  - `load_open_interest(symbol: str, start: date, end: date, cache_dir: Path) -> pd.Series` from the daily metrics files.
  - `check_continuity(bars: pd.DataFrame, interval: str) -> None` raises `DataGapError` on gaps, duplicates or non-monotonic index.
  - `align_asof(bars: pd.DataFrame, series: pd.Series, available_lag: pd.Timedelta) -> pd.Series` aligns using backward as-of join on `series.index + available_lag`.
  - `assert_no_lookahead(signal_fn: Callable[[pd.DataFrame], pd.Series], bars: pd.DataFrame, sample: int = 50) -> None` raises `LookaheadError` if the signal at any sampled bar t differs between `signal_fn(bars[:t+1])` and `signal_fn(bars)`.
- Archive layout used: `https://data.binance.vision/data/futures/um/{monthly|daily}/{klines|fundingRate|metrics}/...`, with `.CHECKSUM` verification of each zip.

- [ ] **Step 1: Write failing tests.** Archive parsing from small zip fixtures built in the test (columns mapped, UTC index, checksum mismatch raises `ChecksumError`). `check_continuity` raises on a bar removed from a synthetic frame and on a duplicated bar. `align_asof` test: a funding value stamped at 08:00 with a 0 lag is not visible to the bar closing at 07:59. `assert_no_lookahead` fails for `lambda b: b.close.shift(-1)` and passes for `lambda b: b.close.rolling(5).mean()`.
- [ ] **Step 2: Run** `pytest tests/data -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the interfaces. Use `httpx` with retries; cache zips under `cache_dir`; no network in unit tests.
- [ ] **Step 4: Add** one `@pytest.mark.network` test that downloads one month of `BTCUSDT` 1h klines and passes `check_continuity`. Run `pytest tests/data -v -m "not network"` (pass), then `pytest -m network` once (expected pass; if archive layout differs, fix the URLs and record the corrected layout in the README).
- [ ] **Step 5: Commit** `feat: archive data layer and lookahead guard`.

### Task 3: Backtester with costs

**Files:**
- Create: `src/agenkit_trader/backtest/costs.py`, `src/agenkit_trader/backtest/engine.py`, `src/agenkit_trader/backtest/stats.py`
- Test: `tests/backtest/test_costs.py`, `tests/backtest/test_engine.py`, `tests/backtest/test_stats.py`

**Interfaces:**
- Consumes: bars frame from Task 2.
- Produces:
  - `CostModel(taker_fee: float, maker_fee: float, half_spread_bps: float, slippage_bps: float)` with `.scaled(factor: float) -> CostModel` and `.round_trip_cost(notional: float) -> float`.
  - `run_backtest(bars: pd.DataFrame, positions: pd.Series, costs: CostModel, funding: pd.Series | None, equity0: float) -> BacktestResult`. A position decided at the close of bar t earns the return from close t to close t+1 (positions are shifted internally so same-bar fills are impossible). Costs are charged on every position change; funding is paid when `funding_time` falls in the holding window.
  - `BacktestResult(equity: pd.Series, trades: pd.DataFrame, stats: Stats)`.
  - `compute_stats(equity: pd.Series, trades: pd.DataFrame, periods_per_year: float) -> Stats` with `sharpe, max_drawdown, win_rate, profit_factor, n_trades, total_return`.

- [ ] **Step 1: Write failing tests.** Flat price and zero position gives constant equity. A long held on a known +1% bar with zero costs returns exactly +1%; with costs it returns less by `round_trip_cost`. A strategy that "buys the bar's own close when it is the day's max" cannot profit (no same-bar fill). `test_stats_zero_trades` asserts `n_trades == 0`, `profit_factor == 0.0`, `sharpe == 0.0` and no exceptions. Funding paid on a long with positive funding reduces equity by the expected amount.
- [ ] **Step 2: Run** `pytest tests/backtest -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the interfaces. Vectorised pandas; trades are runs of constant non-zero position.
- [ ] **Step 4: Run** `pytest tests/backtest -v`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: cost-aware backtester`.

### Task 4: Risk gate, sizing and kill switch

**Files:**
- Create: `src/agenkit_trader/risk/gate.py`, `src/agenkit_trader/risk/sizing.py`, `src/agenkit_trader/killswitch.py`, `src/agenkit_trader/execution/venue.py` (interface only)
- Test: `tests/risk/test_gate.py`, `tests/risk/test_sizing.py`, `tests/test_killswitch.py`, `tests/fakes.py`

**Interfaces:**
- Consumes: `RiskConfig`, `DecisionLog` (Task 1).
- Produces:
  - `Venue` protocol: `server_time() -> datetime`, `get_account() -> AccountState`, `get_bars(symbol, interval, limit) -> pd.DataFrame`, `symbol_rules(symbol) -> SymbolRules(tick, lot, min_notional)`, `place_order(order: Order) -> Fill`, `flatten_all() -> None`.
  - `Order(symbol, side, qty, price_ref, stop_price, strategy)`; `AccountState(equity, positions: dict[str, Position], day_start_equity, peak_equity, last_bar_time)`.
  - `RiskGate(cfg, rules_provider).check(order, state, now) -> Decision(allowed: bool, reason: str, max_qty: float)`. Rejects: risk per trade above `max_risk_per_trade`, daily loss at or above `max_daily_loss`, drawdown at or above `max_drawdown`, leverage above `max_leverage`, stale data older than `stale_after_s`, quantity below lot or notional below min notional after rounding.
  - `size_position(equity: float, entry: float, stop: float, cfg: RiskConfig, kelly_fraction: float | None = None, rules: SymbolRules | None = None) -> float` returns quantity risking at most `max_risk_per_trade`; Kelly input is clipped to `cfg.kelly_cap`; result rounded down to lot size.
  - `KillSwitch(venue, log, cfg).trigger(reason: str) -> None` (idempotent, flattens then sets `halted=True`); `.record_api_error(now)` triggers when `max_api_errors` fall in `error_window_s`; `.reset(confirm: str)` requires the exact phrase `RESUME`.
  - `tests/fakes.py::FakeVenue` implementing `Venue` with scriptable state and error injection.

- [ ] **Step 1: Write failing tests.** One test per rejection reason above with the 1,000 USDT / 1% / 3% / 10% defaults; `size_position` never risks more than 10 USDT at the default; a Kelly input of 1.0 is treated as 0.25; a hypothesis property test that for random equity, entry and stop, `size_position * |entry-stop| <= equity * max_risk_per_trade + epsilon`. Kill switch: trigger twice flattens once; five injected errors in 300 s halts; `reset("no")` raises; after halt `RiskGate.check` is never reached because the runtime refuses to run (covered in Task 7).
- [ ] **Step 2: Run** `pytest tests/risk tests/test_killswitch.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the interfaces; the gate is a pure function of its inputs.
- [ ] **Step 4: Run** the tests, `ruff`, `mypy`. Expected: all pass.
- [ ] **Step 5: Commit** `feat: risk gate, sizing and kill switch`.

### Task 5: Validation harness

**Files:**
- Create: `src/agenkit_trader/validation/split.py`, `sweep.py`, `trials.py`, `gate.py`, `report.py`
- Test: `tests/validation/test_split.py`, `test_sweep.py`, `test_trials.py`, `test_gate.py`, `test_planted_edge.py`

**Interfaces:**
- Consumes: `run_backtest`, `CostModel` (Task 3), `GateConfig` (Task 1).
- Produces:
  - `Strategy` protocol in `strategies/base.py`: `name: str`, `edge: str`, `param_grid: dict[str, list]`, `signal(bars: pd.DataFrame, params: dict, aux: dict[str, pd.Series]) -> pd.Series` with values in `{-1, 0, 1}`.
  - `split_by_time(bars: pd.DataFrame, holdout_frac: float) -> tuple[pd.DataFrame, pd.DataFrame]` (chronological, no overlap).
  - `TrialCounter` with `.add(n: int)` and `.total`, persisted to `research/trials.json` so every variant ever tried is counted.
  - `deflated_sharpe_pvalue(sharpe: float, n_trials: int, n_obs: int, skew: float, kurt: float) -> float` (Bailey and López de Prado).
  - `neighbourhood_sweep(strategy, bars, costs, grid) -> SweepResult(table: pd.DataFrame, stable: bool)`; `stable` is False if the best setting's neighbours lose more than a configured fraction of its performance.
  - `evaluate(strategy, bars, aux, costs, gate_cfg, counter) -> Verdict(passed: bool, reasons: list[str], train: Stats, holdout: Stats, cost_2x: Stats, regimes: dict[str, Stats])`.
  - `write_report(verdicts: list[Verdict], path: Path)` writes the comparison table (Strategy, Edge, Market, Timeframe, Sharpe, Max Drawdown, Trades, Cost Sensitivity, Verdict) and `deployed.json` for passers only.

- [ ] **Step 1: Write failing tests.** Split has no overlap and holdout is the later segment. Planted edge: synthetic prices with a known predictive feature pass `evaluate`; pure random-walk prices fail across 20 seeds in at least 18. A single-spike parameter setting yields `stable=False`. Zero-trade holdout yields `passed=False` with a reason string. Doubling costs on a marginal strategy flips it to fail. Trial counter persists across instances and the p-value gets worse as `n_trials` grows.
- [ ] **Step 2: Run** `pytest tests/validation -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the interfaces. Regime split: trend vs range by a rolling ADX-style or volatility-quantile label, documented in the module docstring.
- [ ] **Step 4: Run** `pytest tests/validation -v`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: validation harness`.

### Task 6: Candidate strategies and the research run

**Files:**
- Create: `src/agenkit_trader/strategies/funding_reversion.py`, `trend_breakout.py`, `short_reversion.py`, `scripts/run_research.py`, `research/REPORT.md` (generated)
- Test: `tests/strategies/test_signals.py`

**Interfaces:**
- Consumes: `Strategy` protocol and `evaluate` (Task 5), data layer (Task 2).
- Produces: three `Strategy` implementations; `scripts/run_research.py --symbols BTCUSDT ETHUSDT --interval 1h --start YYYY-MM-DD --end YYYY-MM-DD` which loads data, runs every candidate through `evaluate`, and writes `research/REPORT.md` and `deployed.json`.

- [ ] **Step 1: Write failing tests.** Each strategy passes `assert_no_lookahead`, outputs only `{-1, 0, 1}`, and emits 0 during warm-up. Signal sanity on hand-built frames (e.g. funding above the entry threshold gives a short).
- [ ] **Step 2: Run** `pytest tests/strategies -v`. Expected: FAIL.
- [ ] **Step 3: Implement** each strategy with its one-sentence edge, counterparty, entry, exit, stop and holding period in the class docstring, per spec section 4.
- [ ] **Step 4: Run** the tests, then run `scripts/run_research.py` on real archive data with at least two years of history. Commit the generated `research/REPORT.md` with exactly what it printed. If nothing passes, the report says so and no strategy is deployed.
- [ ] **Step 5: Commit** `feat: candidate strategies and first research report`.

### Task 7: Testnet venue, runtime loop and paper CLI

**Files:**
- Create: `src/agenkit_trader/execution/binance_testnet.py`, `src/agenkit_trader/runtime/loop.py`, `src/agenkit_trader/cli.py`
- Test: `tests/execution/test_binance_testnet.py`, `tests/runtime/test_loop.py`

**Interfaces:**
- Consumes: `Venue`, `RiskGate`, `KillSwitch`, `size_position` (Task 4), `deployed.json` (Task 5).
- Produces:
  - `BinanceTestnetVenue(base_url="https://testnet.binancefuture.com", key_env="BINANCE_TESTNET_KEY", secret_env="BINANCE_TESTNET_SECRET")` implementing `Venue`; HMAC-SHA256 signing using a server-time offset measured at startup; raises `ClockSkewError` above 1000 ms.
  - `run_loop(venue: Venue, strategies: list[Strategy], deployed: dict, gate: RiskGate, kill: KillSwitch, log: DecisionLog, clock: Callable[[], datetime]) -> None`: per closed bar, per deployed strategy: compute signal, size, `gate.check`, place order, log inputs, output and result. On start it reconciles positions with `venue.get_account()` before trading. Refuses to run while `kill.halted`.
  - CLI: `agenkit paper --config config/risk.yaml` and `agenkit research`; `paper` refuses to start if `deployed.json` is missing or empty.

- [ ] **Step 1: Write failing tests** with `FakeVenue`. Loop places no order when the gate rejects and logs the reason; restarts with an open position and does not double-enter; stale data triggers the kill switch and flattens; five API errors halt; halted loop refuses to place orders; signed request fixtures match an HMAC computed in the test; skew above 1000 ms raises.
- [ ] **Step 2: Run** `pytest tests/execution tests/runtime -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the interfaces. HTTP is mocked in unit tests; one `@pytest.mark.network` smoke test reads the testnet server time and exchange info with no keys.
- [ ] **Step 4: Run** `pytest -m "not network"` (pass) and the network smoke test (pass). Paper trading with real testnet keys is run by the user, who supplies keys through environment variables.
- [ ] **Step 5: Commit** `feat: testnet venue, runtime loop and paper CLI`.

### Task 8: Live-mode stub and unlock flow

**Files:**
- Create: `src/agenkit_trader/execution/live.py`, `src/agenkit_trader/live_unlock.py`, `docs/GOING_LIVE.md`
- Test: `tests/test_live_unlock.py`

**Interfaces:**
- Consumes: `Venue` (Task 4), `DecisionLog` and config loaders (Task 1).
- Produces: `LiveVenue(Venue)` whose every method raises `LiveLocked` unless `live_unlock.verify(live_cfg: dict, env: Mapping[str, str], paper_stats: PaperStats, typed_phrase: str) -> None` has succeeded. Verify requires `live.enabled: true` in config, `BINANCE_LIVE_KEY` and `BINANCE_LIVE_SECRET` set, paper run at least `min_paper_days` and `min_paper_trades` from the decision log, the risk config hash recorded in the log, and `typed_phrase == "I UNDERSTAND THIS USES REAL MONEY"`. The runtime never calls `verify` itself; only the CLI `agenkit live` does, and only interactively.

- [ ] **Step 1: Write failing tests.** Every `LiveVenue` method raises `LiveLocked` by default; each missing condition (flag, keys, paper days, paper trades, phrase) raises a distinct `UnlockError`; no code path other than the CLI command calls `verify`.
- [ ] **Step 2: Run** `pytest tests/test_live_unlock.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the interfaces. `GOING_LIVE.md` documents that live must run off-sandbox, start at minimum size, and carries no promise of profit.
- [ ] **Step 4: Run** the full suite, `ruff`, `mypy`. Expected: all pass.
- [ ] **Step 5: Commit** `feat: live-mode lock and unlock flow`. Then fill spec section 12 from measured results and commit `docs: how could this system fail`.
