# Strategy validation report

Generated from real backtest runs only. Sharpe is annualised, net of costs, on the hold-out. Cost Sensitivity is hold-out net return at the configured cost multiplier.

Simulation policy: runtime (sign of the signal, 1% risk at the runtime's ATR stop, intrabar exchange stop, no re-entry without a fresh signal transition; see backtest/runtime_policy.py).

| Strategy | Edge | Market | Timeframe | Sharpe | Max Drawdown | Trades | Cost Sensitivity | Verdict |
|---|---|---|---|---|---|---|---|---|
| trend_breakout_r2 | slow trend persistence: closes beyond the prior range tend to continue | BTCUSDT | 4h | 0.24 | 7.3% | 50 | 0.8% | REJECT: hold-out Sharpe 0.240 < min 0.5 (annualised); hold-out profit factor 1.139 < min 1.2; hold-out win rate 0.320 < min 0.35; trial-adjusted p-value 0.9890 > max 0.05 (deflated Sharpe, n_trials=123) |
| trend_funding_filter | trend entries into a funding-crowded side fail more often; skipping them improves trend_breakout | BTCUSDT | 4h | 0.24 | 7.3% | 50 | 0.8% | REJECT: hold-out Sharpe 0.240 < min 0.5 (annualised); hold-out profit factor 1.139 < min 1.2; hold-out win rate 0.320 < min 0.35; trial-adjusted p-value 0.9890 > max 0.05 (deflated Sharpe, n_trials=123) |
| trend_breakout_r2 | slow trend persistence: closes beyond the prior range tend to continue | ETHUSDT | 4h | 1.05 | 7.3% | 28 | 15.0% | REJECT: hold-out trades 28 < min 30; trial-adjusted p-value 0.8910 > max 0.05 (deflated Sharpe, n_trials=123) |
| trend_funding_filter | trend entries into a funding-crowded side fail more often; skipping them improves trend_breakout | ETHUSDT | 4h | 1.05 | 7.3% | 28 | 15.0% | REJECT: hold-out trades 28 < min 30; parameter sweep unstable: neighbour score 0.093 < 0.120 (50% below best 0.240); trial-adjusted p-value 0.8910 > max 0.05 (deflated Sharpe, n_trials=123) |
| trend_breakout_r2 | slow trend persistence: closes beyond the prior range tend to continue | BNBUSDT | 4h | -0.42 | 7.2% | 13 | -5.7% | REJECT: hold-out would have halted the runtime (kill switch: daily_loss); hold-out trades 13 < min 30; hold-out net return -0.0516 <= 0 (no positive expectancy); hold-out Sharpe -0.422 < min 0.5 (annualised); hold-out profit factor 0.497 < min 1.2; hold-out win rate 0.154 < min 0.35; parameter sweep unstable: neighbour score -0.660 < 0.027 (50% below best 0.053); cost sensitivity: at 2.0x costs net return -0.0573, profit factor 0.466, halted (need return > 0 and PF > 1); trial-adjusted p-value 0.9993 > max 0.05 (deflated Sharpe, n_trials=123); no regime shows an edge on the hold-out: regime range excluded: 3 trades < min 10, net return -0.0126 <= 0, Sharpe -0.297 <= 0; regime trend excluded: net return -0.0394 <= 0, Sharpe -0.526 <= 0 |
| trend_funding_filter | trend entries into a funding-crowded side fail more often; skipping them improves trend_breakout | BNBUSDT | 4h | -0.42 | 7.2% | 13 | -5.7% | REJECT: hold-out would have halted the runtime (kill switch: daily_loss); hold-out trades 13 < min 30; hold-out net return -0.0516 <= 0 (no positive expectancy); hold-out Sharpe -0.422 < min 0.5 (annualised); hold-out profit factor 0.497 < min 1.2; hold-out win rate 0.154 < min 0.35; parameter sweep unstable: neighbour score -0.598 < 0.055 (50% below best 0.110); cost sensitivity: at 2.0x costs net return -0.0573, profit factor 0.466, halted (need return > 0 and PF > 1); trial-adjusted p-value 0.9993 > max 0.05 (deflated Sharpe, n_trials=123); no regime shows an edge on the hold-out: regime range excluded: 3 trades < min 10, net return -0.0126 <= 0, Sharpe -0.297 <= 0; regime trend excluded: net return -0.0394 <= 0, Sharpe -0.526 <= 0 |
| trend_breakout_r2 | slow trend persistence: closes beyond the prior range tend to continue | DOGEUSDT | 4h | -0.03 | 8.8% | 48 | -2.4% | REJECT: hold-out net return -0.0124 <= 0 (no positive expectancy); hold-out Sharpe -0.031 < min 0.5 (annualised); hold-out profit factor 0.962 < min 1.2; parameter sweep unstable: neighbour score -0.047 < 0.109 (50% below best 0.219); cost sensitivity: at 2.0x costs net return -0.0242, profit factor 0.913 (need return > 0 and PF > 1); trial-adjusted p-value 0.9959 > max 0.05 (deflated Sharpe, n_trials=123) |
| trend_funding_filter | trend entries into a funding-crowded side fail more often; skipping them improves trend_breakout | DOGEUSDT | 4h | 0.12 | 8.2% | 25 | 0.5% | REJECT: hold-out trades 25 < min 30; hold-out Sharpe 0.118 < min 0.5 (annualised); hold-out profit factor 1.098 < min 1.2; hold-out win rate 0.320 < min 0.35; parameter sweep unstable: best score -0.027 is not positive; trial-adjusted p-value 0.9928 > max 0.05 (deflated Sharpe, n_trials=123) |
| trend_breakout_r2 | slow trend persistence: closes beyond the prior range tend to continue | ADAUSDT | 4h | -0.11 | 8.0% | 14 | -1.7% | REJECT: hold-out trades 14 < min 30; hold-out net return -0.0139 <= 0 (no positive expectancy); hold-out Sharpe -0.110 < min 0.5 (annualised); hold-out profit factor 0.852 < min 1.2; parameter sweep unstable: neighbour score -0.040 < 0.350 (50% below best 0.700); cost sensitivity: at 2.0x costs net return -0.0166, profit factor 0.823 (need return > 0 and PF > 1); trial-adjusted p-value 0.9970 > max 0.05 (deflated Sharpe, n_trials=123); no regime shows an edge on the hold-out: regime range excluded: 0 trades < min 10, net return -0.0105 <= 0, Sharpe 0.000 <= 0; regime trend excluded: net return -0.0034 <= 0, Sharpe -0.018 <= 0 |
| trend_funding_filter | trend entries into a funding-crowded side fail more often; skipping them improves trend_breakout | ADAUSDT | 4h | -0.09 | 7.7% | 14 | -1.4% | REJECT: hold-out trades 14 < min 30; hold-out net return -0.0114 <= 0 (no positive expectancy); hold-out Sharpe -0.086 < min 0.5 (annualised); hold-out profit factor 0.883 < min 1.2; parameter sweep unstable: neighbour score -0.040 < 0.422 (50% below best 0.844); cost sensitivity: at 2.0x costs net return -0.0142, profit factor 0.852 (need return > 0 and PF > 1); trial-adjusted p-value 0.9967 > max 0.05 (deflated Sharpe, n_trials=123); no regime shows an edge on the hold-out: regime range excluded: 0 trades < min 10, net return -0.0110 <= 0, Sharpe 0.000 <= 0; regime trend excluded: net return -0.0004 <= 0 |
| trend_breakout_r2 | slow trend persistence: closes beyond the prior range tend to continue | LINKUSDT | 4h | -0.52 | 10.2% | 41 | -7.3% | REJECT: hold-out would have halted the runtime (kill switch: drawdown); hold-out net return -0.0735 <= 0 (no positive expectancy); hold-out Sharpe -0.516 < min 0.5 (annualised); hold-out profit factor 0.690 < min 1.2; hold-out win rate 0.317 < min 0.35; parameter sweep unstable: neighbour score -0.226 < 0.006 (50% below best 0.013); cost sensitivity: at 2.0x costs net return -0.0731, profit factor 0.688, halted (need return > 0 and PF > 1); trial-adjusted p-value 0.9994 > max 0.05 (deflated Sharpe, n_trials=123); no regime shows an edge on the hold-out: regime range excluded: net return -0.0520 <= 0, Sharpe -0.902 <= 0; regime trend excluded: net return -0.0227 <= 0, Sharpe -0.248 <= 0 |
| trend_funding_filter | trend entries into a funding-crowded side fail more often; skipping them improves trend_breakout | LINKUSDT | 4h | -0.52 | 10.2% | 41 | -7.3% | REJECT: hold-out would have halted the runtime (kill switch: drawdown); hold-out net return -0.0735 <= 0 (no positive expectancy); hold-out Sharpe -0.516 < min 0.5 (annualised); hold-out profit factor 0.690 < min 1.2; hold-out win rate 0.317 < min 0.35; parameter sweep unstable: neighbour score -0.158 < 0.198 (50% below best 0.396); cost sensitivity: at 2.0x costs net return -0.0731, profit factor 0.688, halted (need return > 0 and PF > 1); trial-adjusted p-value 0.9994 > max 0.05 (deflated Sharpe, n_trials=123); no regime shows an edge on the hold-out: regime range excluded: net return -0.0520 <= 0, Sharpe -0.902 <= 0; regime trend excluded: net return -0.0227 <= 0, Sharpe -0.248 <= 0 |
| trend_breakout_r2 | slow trend persistence: closes beyond the prior range tend to continue | AVAXUSDT | 4h | -1.36 | 10.3% | 24 | -9.5% | REJECT: hold-out would have halted the runtime (kill switch: drawdown); hold-out trades 24 < min 30; hold-out net return -0.0982 <= 0 (no positive expectancy); hold-out Sharpe -1.356 < min 0.5 (annualised); hold-out profit factor 0.197 < min 1.2; hold-out win rate 0.250 < min 0.35; cost sensitivity: at 2.0x costs net return -0.0954, profit factor 0.196, halted (need return > 0 and PF > 1); trial-adjusted p-value 1.0000 > max 0.05 (deflated Sharpe, n_trials=123); no regime shows an edge on the hold-out: regime range excluded: net return -0.0042 <= 0, Sharpe -0.106 <= 0; regime trend excluded: net return -0.0944 <= 0, Sharpe -2.488 <= 0 |
| trend_funding_filter | trend entries into a funding-crowded side fail more often; skipping them improves trend_breakout | AVAXUSDT | 4h | -1.36 | 10.3% | 24 | -9.5% | REJECT: hold-out would have halted the runtime (kill switch: drawdown); hold-out trades 24 < min 30; hold-out net return -0.0982 <= 0 (no positive expectancy); hold-out Sharpe -1.356 < min 0.5 (annualised); hold-out profit factor 0.197 < min 1.2; hold-out win rate 0.250 < min 0.35; cost sensitivity: at 2.0x costs net return -0.0954, profit factor 0.196, halted (need return > 0 and PF > 1); trial-adjusted p-value 1.0000 > max 0.05 (deflated Sharpe, n_trials=123); no regime shows an edge on the hold-out: regime range excluded: net return -0.0042 <= 0, Sharpe -0.106 <= 0; regime trend excluded: net return -0.0944 <= 0, Sharpe -2.488 <= 0 |

## Evidence

### trend_breakout_r2 (BTCUSDT 4h)

- params (chosen on train): `{"lookback": 42, "vol_cap": 1.5}`
- deflated-Sharpe p-value 0.989 with n_trials=123
- allowed regimes: trend
- sweep stable: True 
- halted: False
- train: Sharpe 0.20, return 3.9%, max DD 10.1%, PF 1.17, win rate 31.9%, trades 47
- hold-out: Sharpe 0.24, return 3.3%, max DD 7.3%, PF 1.14, win rate 32.0%, trades 50
- cost x: Sharpe 0.10, return 0.8%, max DD 8.0%, PF 1.05, win rate 32.0%, trades 50
- regime range: Sharpe -0.30, return -2.3%, trades 29
- regime trend: Sharpe 0.63, return 5.7%, trades 21
- reason: hold-out Sharpe 0.240 < min 0.5 (annualised)
- reason: hold-out profit factor 1.139 < min 1.2
- reason: hold-out win rate 0.320 < min 0.35
- reason: trial-adjusted p-value 0.9890 > max 0.05 (deflated Sharpe, n_trials=123)
- flag: regime range excluded: net return -0.0227 <= 0, Sharpe -0.303 <= 0
- flag: edge only in trend regime: restricted to it (stay out otherwise)

### trend_funding_filter (BTCUSDT 4h)

- params (chosen on train): `{"lookback": 42, "thr": 0.0003, "vol_cap": 1.5}`
- deflated-Sharpe p-value 0.989 with n_trials=123
- allowed regimes: trend
- sweep stable: True 
- halted: False
- train: Sharpe 0.30, return 6.2%, max DD 10.1%, PF 1.27, win rate 33.3%, trades 45
- hold-out: Sharpe 0.24, return 3.3%, max DD 7.3%, PF 1.14, win rate 32.0%, trades 50
- cost x: Sharpe 0.10, return 0.8%, max DD 8.0%, PF 1.05, win rate 32.0%, trades 50
- regime range: Sharpe -0.30, return -2.3%, trades 29
- regime trend: Sharpe 0.63, return 5.7%, trades 21
- reason: hold-out Sharpe 0.240 < min 0.5 (annualised)
- reason: hold-out profit factor 1.139 < min 1.2
- reason: hold-out win rate 0.320 < min 0.35
- reason: trial-adjusted p-value 0.9890 > max 0.05 (deflated Sharpe, n_trials=123)
- flag: regime range excluded: net return -0.0227 <= 0, Sharpe -0.303 <= 0
- flag: edge only in trend regime: restricted to it (stay out otherwise)

### trend_breakout_r2 (ETHUSDT 4h)

- params (chosen on train): `{"lookback": 84, "vol_cap": 1.5}`
- deflated-Sharpe p-value 0.891 with n_trials=123
- allowed regimes: trend
- sweep stable: True 
- halted: False
- train: Sharpe 0.24, return 3.6%, max DD 6.8%, PF 2.80, win rate 60.0%, trades 5
- hold-out: Sharpe 1.05, return 15.9%, max DD 7.3%, PF 1.89, win rate 35.7%, trades 28
- cost x: Sharpe 1.00, return 15.0%, max DD 7.6%, PF 1.82, win rate 35.7%, trades 28
- regime range: Sharpe 0.12, return 0.5%, trades 7
- regime trend: Sharpe 1.86, return 15.3%, trades 21
- reason: hold-out trades 28 < min 30
- reason: trial-adjusted p-value 0.8910 > max 0.05 (deflated Sharpe, n_trials=123)
- flag: regime range excluded: 7 trades < min 10
- flag: edge only in trend regime: restricted to it (stay out otherwise)

### trend_funding_filter (ETHUSDT 4h)

- params (chosen on train): `{"lookback": 84, "thr": 0.0006, "vol_cap": 1.5}`
- deflated-Sharpe p-value 0.891 with n_trials=123
- allowed regimes: trend
- sweep stable: False neighbour score 0.093 < 0.120 (50% below best 0.240)
- halted: False
- train: Sharpe 0.24, return 3.6%, max DD 6.8%, PF 2.80, win rate 60.0%, trades 5
- hold-out: Sharpe 1.05, return 15.9%, max DD 7.3%, PF 1.89, win rate 35.7%, trades 28
- cost x: Sharpe 1.00, return 15.0%, max DD 7.6%, PF 1.82, win rate 35.7%, trades 28
- regime range: Sharpe 0.12, return 0.5%, trades 7
- regime trend: Sharpe 1.86, return 15.3%, trades 21
- reason: hold-out trades 28 < min 30
- reason: parameter sweep unstable: neighbour score 0.093 < 0.120 (50% below best 0.240)
- reason: trial-adjusted p-value 0.8910 > max 0.05 (deflated Sharpe, n_trials=123)
- flag: regime range excluded: 7 trades < min 10
- flag: edge only in trend regime: restricted to it (stay out otherwise)

### trend_breakout_r2 (BNBUSDT 4h)

- params (chosen on train): `{"lookback": 84, "vol_cap": 1.5}`
- deflated-Sharpe p-value 0.9993 with n_trials=123
- allowed regimes: none
- sweep stable: False neighbour score -0.660 < 0.027 (50% below best 0.053)
- halted: True (daily_loss)
- train: Sharpe 0.05, return 0.6%, max DD 8.6%, PF 1.06, win rate 32.1%, trades 28
- hold-out: Sharpe -0.42, return -5.2%, max DD 7.2%, PF 0.50, win rate 15.4%, trades 13
- cost x: Sharpe -0.47, return -5.7%, max DD 7.7%, PF 0.47, win rate 15.4%, trades 13
- regime range: Sharpe -0.30, return -1.3%, trades 3
- regime trend: Sharpe -0.53, return -3.9%, trades 10
- reason: hold-out would have halted the runtime (kill switch: daily_loss)
- reason: hold-out trades 13 < min 30
- reason: hold-out net return -0.0516 <= 0 (no positive expectancy)
- reason: hold-out Sharpe -0.422 < min 0.5 (annualised)
- reason: hold-out profit factor 0.497 < min 1.2
- reason: hold-out win rate 0.154 < min 0.35
- reason: parameter sweep unstable: neighbour score -0.660 < 0.027 (50% below best 0.053)
- reason: cost sensitivity: at 2.0x costs net return -0.0573, profit factor 0.466, halted (need return > 0 and PF > 1)
- reason: trial-adjusted p-value 0.9993 > max 0.05 (deflated Sharpe, n_trials=123)
- reason: no regime shows an edge on the hold-out: regime range excluded: 3 trades < min 10, net return -0.0126 <= 0, Sharpe -0.297 <= 0; regime trend excluded: net return -0.0394 <= 0, Sharpe -0.526 <= 0

### trend_funding_filter (BNBUSDT 4h)

- params (chosen on train): `{"lookback": 84, "thr": 0.0006, "vol_cap": 1.5}`
- deflated-Sharpe p-value 0.9993 with n_trials=123
- allowed regimes: none
- sweep stable: False neighbour score -0.598 < 0.055 (50% below best 0.110)
- halted: True (daily_loss)
- train: Sharpe 0.11, return 1.9%, max DD 8.6%, PF 1.16, win rate 33.3%, trades 27
- hold-out: Sharpe -0.42, return -5.2%, max DD 7.2%, PF 0.50, win rate 15.4%, trades 13
- cost x: Sharpe -0.47, return -5.7%, max DD 7.7%, PF 0.47, win rate 15.4%, trades 13
- regime range: Sharpe -0.30, return -1.3%, trades 3
- regime trend: Sharpe -0.53, return -3.9%, trades 10
- reason: hold-out would have halted the runtime (kill switch: daily_loss)
- reason: hold-out trades 13 < min 30
- reason: hold-out net return -0.0516 <= 0 (no positive expectancy)
- reason: hold-out Sharpe -0.422 < min 0.5 (annualised)
- reason: hold-out profit factor 0.497 < min 1.2
- reason: hold-out win rate 0.154 < min 0.35
- reason: parameter sweep unstable: neighbour score -0.598 < 0.055 (50% below best 0.110)
- reason: cost sensitivity: at 2.0x costs net return -0.0573, profit factor 0.466, halted (need return > 0 and PF > 1)
- reason: trial-adjusted p-value 0.9993 > max 0.05 (deflated Sharpe, n_trials=123)
- reason: no regime shows an edge on the hold-out: regime range excluded: 3 trades < min 10, net return -0.0126 <= 0, Sharpe -0.297 <= 0; regime trend excluded: net return -0.0394 <= 0, Sharpe -0.526 <= 0

### trend_breakout_r2 (DOGEUSDT 4h)

- params (chosen on train): `{"lookback": 42, "vol_cap": 1.5}`
- deflated-Sharpe p-value 0.9959 with n_trials=123
- allowed regimes: trend
- sweep stable: False neighbour score -0.047 < 0.109 (50% below best 0.219)
- halted: False
- train: Sharpe 0.22, return 2.0%, max DD 4.9%, PF inf, win rate 100.0%, trades 1
- hold-out: Sharpe -0.03, return -1.2%, max DD 8.8%, PF 0.96, win rate 35.4%, trades 48
- cost x: Sharpe -0.11, return -2.4%, max DD 8.9%, PF 0.91, win rate 35.4%, trades 48
- regime range: Sharpe -1.96, return -10.1%, trades 24
- regime trend: Sharpe 1.00, return 9.8%, trades 24
- reason: hold-out net return -0.0124 <= 0 (no positive expectancy)
- reason: hold-out Sharpe -0.031 < min 0.5 (annualised)
- reason: hold-out profit factor 0.962 < min 1.2
- reason: parameter sweep unstable: neighbour score -0.047 < 0.109 (50% below best 0.219)
- reason: cost sensitivity: at 2.0x costs net return -0.0242, profit factor 0.913 (need return > 0 and PF > 1)
- reason: trial-adjusted p-value 0.9959 > max 0.05 (deflated Sharpe, n_trials=123)
- flag: regime range excluded: net return -0.1007 <= 0, Sharpe -1.964 <= 0
- flag: edge only in trend regime: restricted to it (stay out otherwise)

### trend_funding_filter (DOGEUSDT 4h)

- params (chosen on train): `{"lookback": 84, "thr": 0.0003, "vol_cap": 1.5}`
- deflated-Sharpe p-value 0.9928 with n_trials=123
- allowed regimes: trend
- sweep stable: False best score -0.027 is not positive
- halted: False
- train: Sharpe -0.03, return -0.7%, max DD 9.1%, PF 0.91, win rate 36.4%, trades 11
- hold-out: Sharpe 0.12, return 1.1%, max DD 8.2%, PF 1.10, win rate 32.0%, trades 25
- cost x: Sharpe 0.07, return 0.5%, max DD 8.4%, PF 1.06, win rate 32.0%, trades 25
- regime range: Sharpe -0.22, return -1.2%, trades 4
- regime trend: Sharpe 0.34, return 2.3%, trades 21
- reason: hold-out trades 25 < min 30
- reason: hold-out Sharpe 0.118 < min 0.5 (annualised)
- reason: hold-out profit factor 1.098 < min 1.2
- reason: hold-out win rate 0.320 < min 0.35
- reason: parameter sweep unstable: best score -0.027 is not positive
- reason: trial-adjusted p-value 0.9928 > max 0.05 (deflated Sharpe, n_trials=123)
- flag: regime range excluded: 4 trades < min 10, net return -0.0121 <= 0, Sharpe -0.220 <= 0
- flag: edge only in trend regime: restricted to it (stay out otherwise)

### trend_breakout_r2 (ADAUSDT 4h)

- params (chosen on train): `{"lookback": 168, "vol_cap": 1.5}`
- deflated-Sharpe p-value 0.997 with n_trials=123
- allowed regimes: none
- sweep stable: False neighbour score -0.040 < 0.350 (50% below best 0.700)
- halted: False
- train: Sharpe 0.70, return 11.5%, max DD 5.2%, PF 6.62, win rate 33.3%, trades 3
- hold-out: Sharpe -0.11, return -1.4%, max DD 8.0%, PF 0.85, win rate 35.7%, trades 14
- cost x: Sharpe -0.14, return -1.7%, max DD 8.2%, PF 0.82, win rate 35.7%, trades 14
- regime range: Sharpe 0.00, return -1.1%, trades 0
- regime trend: Sharpe -0.02, return -0.3%, trades 14
- reason: hold-out trades 14 < min 30
- reason: hold-out net return -0.0139 <= 0 (no positive expectancy)
- reason: hold-out Sharpe -0.110 < min 0.5 (annualised)
- reason: hold-out profit factor 0.852 < min 1.2
- reason: parameter sweep unstable: neighbour score -0.040 < 0.350 (50% below best 0.700)
- reason: cost sensitivity: at 2.0x costs net return -0.0166, profit factor 0.823 (need return > 0 and PF > 1)
- reason: trial-adjusted p-value 0.9970 > max 0.05 (deflated Sharpe, n_trials=123)
- reason: no regime shows an edge on the hold-out: regime range excluded: 0 trades < min 10, net return -0.0105 <= 0, Sharpe 0.000 <= 0; regime trend excluded: net return -0.0034 <= 0, Sharpe -0.018 <= 0

### trend_funding_filter (ADAUSDT 4h)

- params (chosen on train): `{"lookback": 168, "thr": 0.0003, "vol_cap": 1.5}`
- deflated-Sharpe p-value 0.9967 with n_trials=123
- allowed regimes: none
- sweep stable: False neighbour score -0.040 < 0.422 (50% below best 0.844)
- halted: False
- train: Sharpe 0.84, return 12.6%, max DD 3.5%, PF 13.73, win rate 50.0%, trades 2
- hold-out: Sharpe -0.09, return -1.1%, max DD 7.7%, PF 0.88, win rate 35.7%, trades 14
- cost x: Sharpe -0.11, return -1.4%, max DD 8.0%, PF 0.85, win rate 35.7%, trades 14
- regime range: Sharpe 0.00, return -1.1%, trades 0
- regime trend: Sharpe 0.03, return -0.0%, trades 14
- reason: hold-out trades 14 < min 30
- reason: hold-out net return -0.0114 <= 0 (no positive expectancy)
- reason: hold-out Sharpe -0.086 < min 0.5 (annualised)
- reason: hold-out profit factor 0.883 < min 1.2
- reason: parameter sweep unstable: neighbour score -0.040 < 0.422 (50% below best 0.844)
- reason: cost sensitivity: at 2.0x costs net return -0.0142, profit factor 0.852 (need return > 0 and PF > 1)
- reason: trial-adjusted p-value 0.9967 > max 0.05 (deflated Sharpe, n_trials=123)
- reason: no regime shows an edge on the hold-out: regime range excluded: 0 trades < min 10, net return -0.0110 <= 0, Sharpe 0.000 <= 0; regime trend excluded: net return -0.0004 <= 0

### trend_breakout_r2 (LINKUSDT 4h)

- params (chosen on train): `{"lookback": 42, "vol_cap": 1.5}`
- deflated-Sharpe p-value 0.9994 with n_trials=123
- allowed regimes: none
- sweep stable: False neighbour score -0.226 < 0.006 (50% below best 0.013)
- halted: True (drawdown)
- train: Sharpe 0.01, return 0.0%, max DD 3.4%, PF inf, win rate 100.0%, trades 1
- hold-out: Sharpe -0.52, return -7.3%, max DD 10.2%, PF 0.69, win rate 31.7%, trades 41
- cost x: Sharpe -0.52, return -7.3%, max DD 10.1%, PF 0.69, win rate 32.5%, trades 40
- regime range: Sharpe -0.90, return -5.2%, trades 25
- regime trend: Sharpe -0.25, return -2.3%, trades 16
- reason: hold-out would have halted the runtime (kill switch: drawdown)
- reason: hold-out net return -0.0735 <= 0 (no positive expectancy)
- reason: hold-out Sharpe -0.516 < min 0.5 (annualised)
- reason: hold-out profit factor 0.690 < min 1.2
- reason: hold-out win rate 0.317 < min 0.35
- reason: parameter sweep unstable: neighbour score -0.226 < 0.006 (50% below best 0.013)
- reason: cost sensitivity: at 2.0x costs net return -0.0731, profit factor 0.688, halted (need return > 0 and PF > 1)
- reason: trial-adjusted p-value 0.9994 > max 0.05 (deflated Sharpe, n_trials=123)
- reason: no regime shows an edge on the hold-out: regime range excluded: net return -0.0520 <= 0, Sharpe -0.902 <= 0; regime trend excluded: net return -0.0227 <= 0, Sharpe -0.248 <= 0

### trend_funding_filter (LINKUSDT 4h)

- params (chosen on train): `{"lookback": 42, "thr": 0.0003, "vol_cap": 1.5}`
- deflated-Sharpe p-value 0.9994 with n_trials=123
- allowed regimes: none
- sweep stable: False neighbour score -0.158 < 0.198 (50% below best 0.396)
- halted: True (drawdown)
- train: Sharpe 0.40, return 5.3%, max DD 3.7%, PF 2.67, win rate 50.0%, trades 8
- hold-out: Sharpe -0.52, return -7.3%, max DD 10.2%, PF 0.69, win rate 31.7%, trades 41
- cost x: Sharpe -0.52, return -7.3%, max DD 10.1%, PF 0.69, win rate 32.5%, trades 40
- regime range: Sharpe -0.90, return -5.2%, trades 25
- regime trend: Sharpe -0.25, return -2.3%, trades 16
- reason: hold-out would have halted the runtime (kill switch: drawdown)
- reason: hold-out net return -0.0735 <= 0 (no positive expectancy)
- reason: hold-out Sharpe -0.516 < min 0.5 (annualised)
- reason: hold-out profit factor 0.690 < min 1.2
- reason: hold-out win rate 0.317 < min 0.35
- reason: parameter sweep unstable: neighbour score -0.158 < 0.198 (50% below best 0.396)
- reason: cost sensitivity: at 2.0x costs net return -0.0731, profit factor 0.688, halted (need return > 0 and PF > 1)
- reason: trial-adjusted p-value 0.9994 > max 0.05 (deflated Sharpe, n_trials=123)
- reason: no regime shows an edge on the hold-out: regime range excluded: net return -0.0520 <= 0, Sharpe -0.902 <= 0; regime trend excluded: net return -0.0227 <= 0, Sharpe -0.248 <= 0

### trend_breakout_r2 (AVAXUSDT 4h)

- params (chosen on train): `{"lookback": 42, "vol_cap": 1.5}`
- deflated-Sharpe p-value 1 with n_trials=123
- allowed regimes: none
- sweep stable: True 
- halted: True (drawdown)
- train: Sharpe 1.15, return 34.1%, max DD 4.8%, PF 14.99, win rate 62.5%, trades 8
- hold-out: Sharpe -1.36, return -9.8%, max DD 10.3%, PF 0.20, win rate 25.0%, trades 24
- cost x: Sharpe -1.34, return -9.5%, max DD 10.0%, PF 0.20, win rate 26.1%, trades 23
- regime range: Sharpe -0.11, return -0.4%, trades 14
- regime trend: Sharpe -2.49, return -9.4%, trades 10
- reason: hold-out would have halted the runtime (kill switch: drawdown)
- reason: hold-out trades 24 < min 30
- reason: hold-out net return -0.0982 <= 0 (no positive expectancy)
- reason: hold-out Sharpe -1.356 < min 0.5 (annualised)
- reason: hold-out profit factor 0.197 < min 1.2
- reason: hold-out win rate 0.250 < min 0.35
- reason: cost sensitivity: at 2.0x costs net return -0.0954, profit factor 0.196, halted (need return > 0 and PF > 1)
- reason: trial-adjusted p-value 1.0000 > max 0.05 (deflated Sharpe, n_trials=123)
- reason: no regime shows an edge on the hold-out: regime range excluded: net return -0.0042 <= 0, Sharpe -0.106 <= 0; regime trend excluded: net return -0.0944 <= 0, Sharpe -2.488 <= 0

### trend_funding_filter (AVAXUSDT 4h)

- params (chosen on train): `{"lookback": 42, "thr": 0.0003, "vol_cap": 1.5}`
- deflated-Sharpe p-value 1 with n_trials=123
- allowed regimes: none
- sweep stable: True 
- halted: True (drawdown)
- train: Sharpe 1.17, return 34.6%, max DD 4.8%, PF 19.08, win rate 71.4%, trades 7
- hold-out: Sharpe -1.36, return -9.8%, max DD 10.3%, PF 0.20, win rate 25.0%, trades 24
- cost x: Sharpe -1.34, return -9.5%, max DD 10.0%, PF 0.20, win rate 26.1%, trades 23
- regime range: Sharpe -0.11, return -0.4%, trades 14
- regime trend: Sharpe -2.49, return -9.4%, trades 10
- reason: hold-out would have halted the runtime (kill switch: drawdown)
- reason: hold-out trades 24 < min 30
- reason: hold-out net return -0.0982 <= 0 (no positive expectancy)
- reason: hold-out Sharpe -1.356 < min 0.5 (annualised)
- reason: hold-out profit factor 0.197 < min 1.2
- reason: hold-out win rate 0.250 < min 0.35
- reason: cost sensitivity: at 2.0x costs net return -0.0954, profit factor 0.196, halted (need return > 0 and PF > 1)
- reason: trial-adjusted p-value 1.0000 > max 0.05 (deflated Sharpe, n_trials=123)
- reason: no regime shows an edge on the hold-out: regime range excluded: net return -0.0042 <= 0, Sharpe -0.106 <= 0; regime trend excluded: net return -0.0944 <= 0, Sharpe -2.488 <= 0
