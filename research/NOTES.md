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
