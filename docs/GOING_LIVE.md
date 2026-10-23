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

## Testnet smoke test (`agenkit smoke`)

A one-shot checklist that proves the order plumbing works against the real
Binance futures **testnet** before you trust a paper run. It runs on your
machine with your own testnet keys. It uses no strategy and no signal, uses
testnet funds only (never real money), and was NOT run by the developers (no keys exist in the
development sandbox): its first real run is yours. Anything it reports about
the real exchange is a first observation.

It is separate from paper trading. It writes only
`data/smoke/smoke_log.jsonl` (every record tagged `"mode": "smoke"`). It never
reads or writes `research/deployed.json`, the paper decision log or the paper
state, and neither `agenkit live` nor the paper stats read the smoke log, so it
never counts toward paper days or trades.

### Step by step

1. Create testnet keys: open the Binance Futures testnet
   (`https://testnet.binancefuture.com`), log in (GitHub login works), open
   the "API Key" tab and generate a key. These are testnet-only keys and hold
   test money. Never reuse real-account keys.
2. Export them in your own shell (not in a file in the repo, never in chat):

       export BINANCE_TESTNET_KEY=...
       export BINANCE_TESTNET_SECRET=...

3. Make sure the testnet account has **no open positions or orders**. The
   smoke test refuses otherwise (step 2 fails) and never touches what it did
   not open. If a step fails it flattens the whole account.
4. Run it from a checkout, with a synced clock (NTP):

       agenkit smoke                 # prints the plan, asks you to type SMOKE TESTNET
       agenkit smoke --symbol ETHUSDT --yes   # skip the prompt (e.g. no TTY)

   The symbol must be in `config/symbol_rules.yaml`. Only the testnet host is
   reachable; there is no option to change it.
5. Read the summary table. Exit code 0: all eight steps passed and the account
   was verified flat. 1: a step failed (account verified flat). 130: you
   interrupted it (account verified flat). 4: the account could not
   be verified flat; check the testnet account by hand. 2: refused to start.

### What each step means

| # | Step | pass | fail |
|---|------|------|------|
| 1 | connectivity, clock skew, exchangeInfo rules equal `config/symbol_rules.yaml` | exchange reachable, skew under 1 s, tick/lot/min notional match | network or clock problem, or the testnet rules drifted: update the yaml (a counted config change) or re-check |
| 2 | account balance read; account must be flat | signed reads work, equity shown | bad keys/permissions, or the account already holds a position/order |
| 3 | MARKET entry at the smallest valid size through the real RiskGate | gate allowed, order filled | gate rejection (reason shown, e.g. equity too small for the risk limit), or the exchange refused |
| 4 | the exchange-side stop appears | `place_stop` returned an id and the stop is listed as a reduce-only STOP_MARKET at the right price and size | the exact exchange answer is shown: an API error (code and text), or "returned id X but not listed", or a listed order that differs. This is the untested Algo Service path: send this back |
| 5 | reconcile: position and stop as the runtime's reconcile expects | position equals the fill and our stop is a closing-side reduce-only stop | the runtime would not recognise the stop |
| 6 | stop-coverage check as the runtime does it | stops cover the position | the runtime would repair, then halt |
| 7 | cancel the stop, close with a reduce-only order, verify flat | stop gone, position closed | cancel or reduce-only close not honoured |
| 8 | KillSwitch flatten with a second position (and stop) | `flattened_ok`, no orders, no positions | the kill-switch path does not flatten on the real exchange: serious |

A failed step stops the run, attempts a flatten and reports. Once smoke has
placed an order, the flatten and a final flat check also run after Ctrl-C,
SIGTERM or SIGHUP (exit 130 if the account was verified flat) and after an
unexpected error such as a failing log write. The final check reads the
account; if it cannot run, the table says so loudly (exit 4). A hard kill
(SIGKILL, power loss, a crashed machine) cannot be handled: it can still leave
a position or stop on the testnet account, so check the account by hand after
any run that did not print the "verified FLAT" line.

### What to send back

The summary table (copy it from the terminal) and `data/smoke/smoke_log.jsonl`.
Neither contains your keys. Look through the log once before sharing it: it
shows your testnet equity.

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
