# Sentient Research Report — 2026-06-25

**Backtest:** 30 days | **Baseline score:** 1148.4

## Current Settings

| Parameter | Value |
|-----------|-------|
| `MARKOV_MIN_GAP` | `0.11` |
| `MIN_PERSIST` | `0.82` |
| `MAX_ENTRY_PRICE_YES` | `72` |
| `MAX_ENTRY_PRICE_NO` | `68` |
| `MIN_MINUTES_LEFT` | `3` |
| `MAX_MINUTES_LEFT` | `9` |
| `MAX_VOL_MULT` | `1.4` |
| `MIN_HURST` | `0.45` |
| `BLOCKED_UTC_HOURS` | `[8, 11, 16, 18, 21]` |
| `SIZING_MODE` | `allowance` |
| `ALLOWANCE_PCT` | `20.0` |
| `KELLY_FRACTION` | `0.2` |
| `MAX_ENTRY_PRICE_RM` | `72` |

## Baseline (30d)

| Metric | Value |
|--------|-------|
| Return | +448.1% |
| Win rate | 81.5% |
| Trades | 259 |
| Profit factor | 1.52 |
| Max drawdown | 31.8% |

**Price buckets:**


**Price buckets:**

- `<65¢`: 138 trades, 68.1% WR, P&L $-156.22
- `65-73¢`: 121 trades, 96.7% WR, P&L $+1052.35

**Time buckets:**

- `9-6m`: 30 trades, 73.3% WR, P&L $-22.60
- `6-3m`: 122 trades, 92.6% WR, P&L $+942.04

## Live Performance (last 7 days)

| Metric | Value |
|--------|-------|
| Trades | 95 |
| Win rate | 72.6% |
| Total P&L | $+126.17 |
| Avg win | $+8.08 |
| Avg loss | $-16.59 |
| Daemon settled trades | 1 |
| WebUI settled trades | 94 |

**Top skip reasons:**

- (147×) high vol (GK=0.00287) | mean-reverting (Hurst=0.44) | price 
- (122×) blocked UTC hour 21:00 | mean-reverting (Hurst=0.37) | price
- (86×) blocked UTC hour 11:00 | mean-reverting (Hurst=0.00) | price
- (65×) high vol (GK=0.00287) | mean-reverting (Hurst=0.44)
- (46×) price 92¢ > NO cap 65¢

## Ablation Study — Top 10 Variations

| Variation | Return | WR | Trades | DD | Score | Δ |
|-----------|--------|-----|--------|-----|-------|---|
| `MARKOV_MIN_GAP=0.13` | +483.8% | 82.3% | 254 | 29.5% | 1349.7 | +201.3 |
| `MAX_VOL_MULT=1.5` | +512.4% | 81.6% | 283 | 33.3% | 1255.6 | +107.2 |
| `MAX_MINUTES_LEFT=10` | +461.9% | 81.2% | 271 | 30.2% | 1241.9 | +93.5 |
| `MARKOV_MIN_GAP=0.15` | +444.6% | 81.9% | 249 | 29.5% | 1234.3 | +85.9 |
| `MARKOV_MIN_GAP=0.08` | +455.5% | 81.5% | 260 | 31.8% | 1167.4 | +19.0 |
| `MARKOV_MIN_GAP=0.09` | +455.5% | 81.5% | 260 | 31.8% | 1167.4 | +19.0 |
| `MARKOV_MIN_GAP=0.1` | +455.5% | 81.5% | 260 | 31.8% | 1167.4 | +19.0 |
| `MIN_PERSIST=0.78` | +448.1% | 81.5% | 259 | 31.8% | 1148.4 | +0.0 |
| `MIN_PERSIST=0.8` | +448.1% | 81.5% | 259 | 31.8% | 1148.4 | +0.0 |
| `MIN_PERSIST=0.85` | +448.1% | 81.5% | 259 | 31.8% | 1148.4 | +0.0 |