# Going live

**Live (real-money) execution is not implemented in this build.** This page
explains what the live gate does today, what it does not do, and what you
would have to do yourself before real money could ever be at risk.

Nothing in this project promises profit. As of this build, 0 of the 6
researched strategy candidates passed the validation gate, so nothing is
deployed, and paper trading refuses to start until something passes.

## What "unlock" means

`agenkit live` checks the conditions for live trading (spec section 7). It
passes only if all of these hold:

1. `config/live.yaml` has `enabled` set to the YAML boolean true. You edit
   that file yourself. No code in this repository writes it, and the paper
   runtime cannot change it.
2. The paper decision log (`logs/paper.jsonl` by default) is well formed and
   records no kill-switch halt without a later `agenkit reset-halt`.
3. The paper record in that log covers at least `min_paper_days` (from the
   first to the last filled paper order) and at least `min_paper_trades`
   filled paper orders. `min_paper_trades` counts FILLED ORDERS, not round
   trips: an entry and its exit are two. The defaults, 14 days and 30 orders, are proposals
   for you to confirm. The numbers come from the decision log only, never
   from the state file.
4. Every paper run that filled an order recorded the sha256 of the risk
   config at its startup, and that hash equals the hash of your **current**
   `config/risk.yaml`. If you change the risk limits after paper trading, the
   unlock is refused: paper trade again with the limits you intend to use.
5. You type `I UNDERSTAND THIS USES REAL MONEY` exactly (case-sensitive) at
   the interactive prompt. The phrase cannot come from an argument, an
   environment variable or a file.
6. `BINANCE_LIVE_KEY` and `BINANCE_LIVE_SECRET` are set in your own shell.
   They are read last, only after every other check has passed. They are
   never logged, printed or shown in error messages.

The command refuses unless both stdin and stdout are a terminal. Every
attempt, passed or refused, is appended to the decision log as a
`live_unlock` record. The record holds the reason, but never the keys and
never the typed text (only whether it matched).

## What "unlock" does not mean

- **It does not trade.** A passed gate produces an in-memory token. The
  token is never written to disk and cannot be loaded from a file or the
  environment, and it is forgotten when the command exits. Even with that
  token, every method of `LiveVenue` raises `LiveNotImplemented`. No code in
  this build signs or sends a request to the real-money exchange.
- It does not mean a strategy works. The gate checks process, not edge.
- It does not make the decision log tamper-proof. The gate trusts the log
  on your machine. It protects you from mistakes, not from yourself.

## What the paper record does not show

- **The runtime does not trade what research validated.** A research pass does NOT validate the runtime's sizing or stop policy. The committed research run held +-1x equity (positions are -1/0/+1; vol_cap is a filter, not scaling), rebalanced every bar, with no stop-outs; the paper runtime keeps only the sign of the signal, sizes for 1% risk at a 3 x ATR(14) exchange-side disaster stop (roughly 0.4x equity notional on ETH 1h, an illustrative estimate) and does not re-enter after a stop-out until a fresh signal transition. The committed run's backtest simulated neither; `backtest/runtime_policy.py` now does via `scripts/run_research.py --policy runtime`, and running it is a new counted run for the user to decide. The runtime now refuses a `deployed.json` entry unless its `policy` is exactly `"runtime"`.
  So a paper record measures the runtime's policy, which no research pass has
  validated.
- **Funding is not in the decision records.** Funding paid or received on
  held perpetual positions changes venue equity (and so the daily-loss and
  drawdown checks), but no decision record itemises it, and the paper P&L
  text does not include it.

## Live execution would need a separate, reviewed task

Building real-money order placement is out of scope here. It needs its own
task, a code review and your explicit approval. That task would also have to
cover reconciliation, fees read back from fills, rate limits, and the
open items in spec section 12.

It must run **off-sandbox**: the Binance mainnet API is geo-blocked from
the development sandbox where this project was built. That is why nothing
here was ever tested against mainnet.

## Checklist (you do these yourself)

1. Run research and get a strategy through the validation gate. Today none
   has passed.
2. Paper trade it on the testnet with `agenkit paper` for at least the
   configured days and trades. Check the decision log and fix any halts.
3. Confirm the risk limits in `config/risk.yaml`: risk per trade, daily
   loss, drawdown and leverage. Paper trade with exactly those limits, since
   changing them later blocks the unlock.
4. Confirm or change `min_paper_days` and `min_paper_trades` in
   `config/live.yaml`, then change `enabled` from false to true there by hand.
5. Create API keys on your own exchange account. Use trade-only permissions,
   no withdrawals, and an IP allowlist. Export them in your own shell as
   `BINANCE_LIVE_KEY` and `BINANCE_LIVE_SECRET`. Never commit them, and never
   paste them into a chat or a log.
6. Run `agenkit live` in an interactive terminal and type the phrase.
7. If live execution is ever built, start at the **smallest size the
   exchange allows** and only with money you can afford to lose entirely.

Undo at any time: set `enabled: false`, and unset the two environment
variables.
