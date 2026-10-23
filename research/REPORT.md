# Strategy validation report

Generated from real backtest runs only. Sharpe is annualised, net of costs, on the hold-out. Cost Sensitivity is hold-out net return at the configured cost multiplier.

| Strategy | Edge | Market | Timeframe | Sharpe | Max Drawdown | Trades | Cost Sensitivity | Verdict |
|---|---|---|---|---|---|---|---|---|
| funding_reversion | extreme funding marks crowded leveraged positioning that tends to unwind | BTCUSDT | 1h | 0.00 | 0.0% | 0 | 0.0% | REJECT: hold-out has zero trades; hold-out net return 0.0000 <= 0 (no positive expectancy); hold-out Sharpe 0.000 < min 0.5 (annualised); hold-out profit factor 0.000 < min 1.2; hold-out win rate 0.000 < min 0.35; parameter sweep unstable: neighbour score -0.114 < 0.186 (50% below best 0.371); cost sensitivity: at 2.0x costs net return 0.0000, profit factor 0.000 (need return > 0 and PF > 1); trial-adjusted p-value 0.9358 > max 0.05 (deflated Sharpe, n_trials=9); no regime shows an edge on the hold-out: regime range excluded: 0 trades < min 10, net return 0.0000 <= 0, Sharpe 0.000 <= 0; regime trend excluded: 0 trades < min 10, net return 0.0000 <= 0, Sharpe 0.000 <= 0 |
| trend_breakout | slow trend persistence: closes beyond the prior range tend to continue | BTCUSDT | 1h | 0.02 | 40.6% | 60 | -18.6% | REJECT: hold-out net return -0.1044 <= 0 (no positive expectancy); hold-out Sharpe 0.019 < min 0.5 (annualised); hold-out max drawdown 0.406 > max 0.25; hold-out profit factor 1.002 < min 1.2; hold-out win rate 0.333 < min 0.35; parameter sweep unstable: neighbour score -0.261 < 0.214 (50% below best 0.427); cost sensitivity: at 2.0x costs net return -0.1865, profit factor 0.931 (need return > 0 and PF > 1); trial-adjusted p-value 0.9711 > max 0.05 (deflated Sharpe, n_trials=21); no regime shows an edge on the hold-out: regime range excluded: 5 trades < min 10; regime trend excluded: net return -0.1593 <= 0, Sharpe -0.288 <= 0 |
| short_reversion | liquidity-driven overshoots partly retrace in range-bound markets | BTCUSDT | 1h | -0.32 | 30.0% | 107 | -26.7% | REJECT: hold-out net return -0.1294 <= 0 (no positive expectancy); hold-out Sharpe -0.318 < min 0.5 (annualised); hold-out max drawdown 0.300 > max 0.25; hold-out profit factor 0.882 < min 1.2; parameter sweep unstable: best score -1.212 is not positive; cost sensitivity: at 2.0x costs net return -0.2666, profit factor 0.728 (need return > 0 and PF > 1); trial-adjusted p-value 0.9935 > max 0.05 (deflated Sharpe, n_trials=30) |
| funding_reversion | extreme funding marks crowded leveraged positioning that tends to unwind | ETHUSDT | 1h | 0.04 | 2.0% | 1 | -0.1% | REJECT: hold-out trades 1 < min 30; hold-out Sharpe 0.043 < min 0.5 (annualised); parameter sweep unstable: neighbour score -0.076 < 0.122 (50% below best 0.244); cost sensitivity: at 2.0x costs net return -0.0006, profit factor 0.000 (need return > 0 and PF > 1); trial-adjusted p-value 0.9845 > max 0.05 (deflated Sharpe, n_trials=39); no regime shows an edge on the hold-out: regime range excluded: 0 trades < min 10, net return 0.0000 <= 0, Sharpe 0.000 <= 0; regime trend excluded: 1 trades < min 10 |
| trend_breakout | slow trend persistence: closes beyond the prior range tend to continue | ETHUSDT | 1h | 0.58 | 52.1% | 30 | 27.5% | REJECT: hold-out max drawdown 0.521 > max 0.25; parameter sweep unstable: neighbour score 0.001 < 0.253 (50% below best 0.506); trial-adjusted p-value 0.9363 > max 0.05 (deflated Sharpe, n_trials=51) |
| short_reversion | liquidity-driven overshoots partly retrace in range-bound markets | ETHUSDT | 1h | -0.63 | 47.0% | 211 | -52.6% | REJECT: hold-out net return -0.3355 <= 0 (no positive expectancy); hold-out Sharpe -0.629 < min 0.5 (annualised); hold-out max drawdown 0.470 > max 0.25; hold-out profit factor 0.862 < min 1.2; parameter sweep unstable: best score -1.126 is not positive; cost sensitivity: at 2.0x costs net return -0.5261, profit factor 0.735 (need return > 0 and PF > 1); trial-adjusted p-value 0.9993 > max 0.05 (deflated Sharpe, n_trials=60) |

## Evidence

### funding_reversion (BTCUSDT 1h)

- params (chosen on train): `{"entry": 0.0003, "max_hold": 24}`
- deflated-Sharpe p-value 0.9358 with n_trials=9
- allowed regimes: none
- sweep stable: False neighbour score -0.114 < 0.186 (50% below best 0.371)
- train: Sharpe 0.37, return 20.8%, max DD 31.0%, PF 1.31, win rate 61.9%, trades 63
- hold-out: Sharpe 0.00, return 0.0%, max DD 0.0%, PF 0.00, win rate 0.0%, trades 0
- cost x: Sharpe 0.00, return 0.0%, max DD 0.0%, PF 0.00, win rate 0.0%, trades 0
- regime range: Sharpe 0.00, return 0.0%, trades 0
- regime trend: Sharpe 0.00, return 0.0%, trades 0
- reason: hold-out has zero trades
- reason: hold-out net return 0.0000 <= 0 (no positive expectancy)
- reason: hold-out Sharpe 0.000 < min 0.5 (annualised)
- reason: hold-out profit factor 0.000 < min 1.2
- reason: hold-out win rate 0.000 < min 0.35
- reason: parameter sweep unstable: neighbour score -0.114 < 0.186 (50% below best 0.371)
- reason: cost sensitivity: at 2.0x costs net return 0.0000, profit factor 0.000 (need return > 0 and PF > 1)
- reason: trial-adjusted p-value 0.9358 > max 0.05 (deflated Sharpe, n_trials=9)
- reason: no regime shows an edge on the hold-out: regime range excluded: 0 trades < min 10, net return 0.0000 <= 0, Sharpe 0.000 <= 0; regime trend excluded: 0 trades < min 10, net return 0.0000 <= 0, Sharpe 0.000 <= 0

### trend_breakout (BTCUSDT 1h)

- params (chosen on train): `{"lookback": 168, "vol_cap": 2.0}`
- deflated-Sharpe p-value 0.9711 with n_trials=21
- allowed regimes: none
- sweep stable: False neighbour score -0.261 < 0.214 (50% below best 0.427)
- train: Sharpe 0.43, return 41.8%, max DD 58.5%, PF 1.24, win rate 36.6%, trades 123
- hold-out: Sharpe 0.02, return -10.4%, max DD 40.6%, PF 1.00, win rate 33.3%, trades 60
- cost x: Sharpe -0.13, return -18.6%, max DD 44.6%, PF 0.93, win rate 30.0%, trades 60
- regime range: Sharpe 0.39, return 6.5%, trades 5
- regime trend: Sharpe -0.29, return -15.9%, trades 55
- reason: hold-out net return -0.1044 <= 0 (no positive expectancy)
- reason: hold-out Sharpe 0.019 < min 0.5 (annualised)
- reason: hold-out max drawdown 0.406 > max 0.25
- reason: hold-out profit factor 1.002 < min 1.2
- reason: hold-out win rate 0.333 < min 0.35
- reason: parameter sweep unstable: neighbour score -0.261 < 0.214 (50% below best 0.427)
- reason: cost sensitivity: at 2.0x costs net return -0.1865, profit factor 0.931 (need return > 0 and PF > 1)
- reason: trial-adjusted p-value 0.9711 > max 0.05 (deflated Sharpe, n_trials=21)
- reason: no regime shows an edge on the hold-out: regime range excluded: 5 trades < min 10; regime trend excluded: net return -0.1593 <= 0, Sharpe -0.288 <= 0

### short_reversion (BTCUSDT 1h)

- params (chosen on train): `{"z_entry": 3.0, "er_max": 0.4}`
- deflated-Sharpe p-value 0.9935 with n_trials=30
- allowed regimes: range
- sweep stable: False best score -1.212 is not positive
- train: Sharpe -1.21, return -76.4%, max DD 80.3%, PF 0.62, win rate 41.3%, trades 254
- hold-out: Sharpe -0.32, return -12.9%, max DD 30.0%, PF 0.88, win rate 53.3%, trades 107
- cost x: Sharpe -0.83, return -26.7%, max DD 39.7%, PF 0.73, win rate 47.7%, trades 107
- regime range: Sharpe 0.80, return 8.6%, trades 29
- regime trend: Sharpe -0.94, return -19.8%, trades 78
- reason: hold-out net return -0.1294 <= 0 (no positive expectancy)
- reason: hold-out Sharpe -0.318 < min 0.5 (annualised)
- reason: hold-out max drawdown 0.300 > max 0.25
- reason: hold-out profit factor 0.882 < min 1.2
- reason: parameter sweep unstable: best score -1.212 is not positive
- reason: cost sensitivity: at 2.0x costs net return -0.2666, profit factor 0.728 (need return > 0 and PF > 1)
- reason: trial-adjusted p-value 0.9935 > max 0.05 (deflated Sharpe, n_trials=30)
- flag: regime trend excluded: net return -0.1985 <= 0, Sharpe -0.939 <= 0
- flag: edge only in range regime: restricted to it (stay out otherwise)

### funding_reversion (ETHUSDT 1h)

- params (chosen on train): `{"entry": 0.0003, "max_hold": 24}`
- deflated-Sharpe p-value 0.9845 with n_trials=39
- allowed regimes: none
- sweep stable: False neighbour score -0.076 < 0.122 (50% below best 0.244)
- train: Sharpe 0.24, return 12.9%, max DD 29.1%, PF 1.17, win rate 50.0%, trades 80
- hold-out: Sharpe 0.04, return 0.1%, max DD 2.0%, PF inf, win rate 100.0%, trades 1
- cost x: Sharpe -0.01, return -0.1%, max DD 2.1%, PF 0.00, win rate 0.0%, trades 1
- regime range: Sharpe 0.00, return 0.0%, trades 0
- regime trend: Sharpe 0.06, return 0.1%, trades 1
- reason: hold-out trades 1 < min 30
- reason: hold-out Sharpe 0.043 < min 0.5 (annualised)
- reason: parameter sweep unstable: neighbour score -0.076 < 0.122 (50% below best 0.244)
- reason: cost sensitivity: at 2.0x costs net return -0.0006, profit factor 0.000 (need return > 0 and PF > 1)
- reason: trial-adjusted p-value 0.9845 > max 0.05 (deflated Sharpe, n_trials=39)
- reason: no regime shows an edge on the hold-out: regime range excluded: 0 trades < min 10, net return 0.0000 <= 0, Sharpe 0.000 <= 0; regime trend excluded: 1 trades < min 10

### trend_breakout (ETHUSDT 1h)

- params (chosen on train): `{"lookback": 336, "vol_cap": 1.0}`
- deflated-Sharpe p-value 0.9363 with n_trials=51
- allowed regimes: trend
- sweep stable: False neighbour score 0.001 < 0.253 (50% below best 0.506)
- train: Sharpe 0.51, return 65.1%, max DD 68.7%, PF 1.49, win rate 34.5%, trades 55
- hold-out: Sharpe 0.58, return 33.8%, max DD 52.1%, PF 1.43, win rate 43.3%, trades 30
- cost x: Sharpe 0.53, return 27.5%, max DD 53.2%, PF 1.38, win rate 43.3%, trades 30
- regime range: Sharpe -0.86, return -39.2%, trades 1
- regime trend: Sharpe 1.78, return 120.1%, trades 29
- reason: hold-out max drawdown 0.521 > max 0.25
- reason: parameter sweep unstable: neighbour score 0.001 < 0.253 (50% below best 0.506)
- reason: trial-adjusted p-value 0.9363 > max 0.05 (deflated Sharpe, n_trials=51)
- flag: regime range excluded: 1 trades < min 10, net return -0.3922 <= 0, Sharpe -0.862 <= 0
- flag: edge only in trend regime: restricted to it (stay out otherwise)

### short_reversion (ETHUSDT 1h)

- params (chosen on train): `{"z_entry": 2.0, "er_max": 0.2}`
- deflated-Sharpe p-value 0.9993 with n_trials=60
- allowed regimes: range
- sweep stable: False best score -1.126 is not positive
- train: Sharpe -1.13, return -88.1%, max DD 92.5%, PF 0.74, win rate 52.7%, trades 505
- hold-out: Sharpe -0.63, return -33.5%, max DD 47.0%, PF 0.86, win rate 58.3%, trades 211
- cost x: Sharpe -1.27, return -52.6%, max DD 60.7%, PF 0.73, win rate 55.0%, trades 211
- regime range: Sharpe 0.71, return 12.2%, trades 75
- regime trend: Sharpe -1.47, return -40.8%, trades 136
- reason: hold-out net return -0.3355 <= 0 (no positive expectancy)
- reason: hold-out Sharpe -0.629 < min 0.5 (annualised)
- reason: hold-out max drawdown 0.470 > max 0.25
- reason: hold-out profit factor 0.862 < min 1.2
- reason: parameter sweep unstable: best score -1.126 is not positive
- reason: cost sensitivity: at 2.0x costs net return -0.5261, profit factor 0.735 (need return > 0 and PF > 1)
- reason: trial-adjusted p-value 0.9993 > max 0.05 (deflated Sharpe, n_trials=60)
- flag: regime trend excluded: net return -0.4077 <= 0, Sharpe -1.467 <= 0
- flag: edge only in range regime: restricted to it (stay out otherwise)
