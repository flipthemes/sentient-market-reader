# Sentient Research Report — 2026-06-25

**Backtest:** 30 days | **Baseline score:** 835.2

## Baseline (30d)

| Metric | Value |
|--------|-------|
| Return | +364.0% |
| Win rate | 81.0% |
| Trades | 211 |
| Profit factor | 1.51 |
| Max drawdown | 35.3% |

**Price buckets:**

- `<65¢`: 122 trades, 68.9% WR, P&L $-93.18
- `65-73¢`: 89 trades, 97.8% WR, P&L $+821.15

## Live Performance (last 7 days)

| Metric | Value |
|--------|-------|
| Trades | 101 |
| Win rate | 70.3% |
| Total P&L | $+67.87 |
| Avg win | $+8.33 |
| Avg loss | $-17.44 |
| Daemon settled trades | 1 |
| WebUI settled trades | 100 |

**Top skip reasons:**

- (86×) blocked UTC hour 11:00 | mean-reverting (Hurst=0.00) | price
- (46×) price 92¢ > NO cap 65¢
- (37×) price 99¢ > NO cap 65¢
- (35×) price 97¢ > NO cap 65¢
- (24×) mean-reverting (Hurst=0.48)

## Ablation Study — Top 10 Variations

| Variation | Return | WR | Trades | DD | Score | Δ |
|-----------|--------|-----|--------|-----|-------|---|
| `MAX_VOL_MULT=1.15` | +426.2% | 85.5% | 159 | 18.1% | 2013.3 | +1178.0 |
| `MAX_ENTRY_PRICE_NO=58` | +340.6% | 85.4% | 144 | 17.0% | 1711.0 | +875.8 |
| `MAX_ENTRY_PRICE_NO=60` | +340.6% | 85.4% | 144 | 17.0% | 1711.0 | +875.8 |
| `MARKOV_MIN_GAP=0.13` | +389.5% | 81.7% | 208 | 28.1% | 1132.5 | +297.2 |
| `MAX_VOL_MULT=1.1` | +299.1% | 84.5% | 129 | 23.0% | 1098.9 | +263.6 |
| `MARKOV_MIN_GAP=0.15` | +359.9% | 81.4% | 204 | 28.1% | 1042.6 | +207.3 |
| `MAX_VOL_MULT=1.25` | +342.8% | 82.0% | 183 | 27.5% | 1022.2 | +186.9 |
| `MAX_VOL_MULT=1.5` | +430.2% | 81.0% | 242 | 35.1% | 992.8 | +157.5 |
| `MIN_MINUTES_LEFT=3` | +389.2% | 81.5% | 222 | 33.2% | 955.4 | +120.2 |
| `MARKOV_MIN_GAP=0.08` | +371.4% | 81.1% | 212 | 35.3% | 853.3 | +18.0 |