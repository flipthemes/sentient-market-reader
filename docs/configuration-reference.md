# Trading Configuration Reference

This project now treats root `.env.local` as the centralized source for trading/backtest knobs.

## Load order

1. `/.env.local` (workspace root)
2. `/python-service/.env.local` (fallback)
3. Hardcoded defaults in `python-service/run_backtest.py`

Environment variables are loaded by `python-service/run_backtest.py` and reused by `python-service/trade_daemon.py` via imports.

## Key variables

| Variable | Default | What it controls | Read in | Used in |
|---|---:|---|---|---|
| `MARKOV_MIN_GAP` | `0.11` | Minimum Markov confidence gap from 50% | `run_backtest.py` | Backtest qualification, live daemon gate |
| `MIN_PERSIST` | `0.82` | Minimum Markov persistence | `run_backtest.py` | Backtest qualification, live daemon gate |
| `LT65_MIN_GAP` | `0.16` | Extra confidence required for entries priced under 65c | `run_backtest.py` | Backtest subset-skip, live daemon `<65c` gate |
| `MIN_EFFICIENCY_RATIO` | `0.24` | Kaufman ER floor (below = noisy/choppy regime skip) | `run_backtest.py` | Backtest gate, live daemon gate |
| `ER_PERIOD` | `8` | Kaufman ER lookback bars on 15m closes | `run_backtest.py` | Backtest gate, live daemon gate |
| `MIN_HURST` | `0.24` | Legacy alias fallback for ER floor | `run_backtest.py` | Compatibility only |
| `MAX_VOL_MULT` | `1.35` | GK volatility cap multiplier vs `REF_VOL_15M` | `run_backtest.py` | Backtest gate, live daemon gate |
| `VEL_SAFETY_RATIO` | `0.40` | Max allowed approach speed toward strike | `run_backtest.py` | Backtest velocity gate |
| `MIN_MINUTES_LEFT` | `3` | Latest minutes-left eligible for entry | `run_backtest.py` | Backtest and daemon timing windows |
| `MAX_MINUTES_LEFT` | `9` | Earliest minutes-left eligible for entry | `run_backtest.py` | Backtest and daemon timing windows |
| `MIN_DIST_PCT` | `0.04` | Min distance from strike to avoid near-50/50 noise | `run_backtest.py` | Backtest and daemon distance gate |
| `MAX_ENTRY_PRICE_YES` | `72` | YES-side entry price cap (cents) | `run_backtest.py` | Backtest and daemon price gate |
| `MAX_ENTRY_PRICE_NO` | `65` | NO-side entry price cap (cents) | `run_backtest.py` | Backtest and daemon price gate |
| `BLOCKED_UTC_HOURS` | `8,11,16,18,21` | Hours where entries are blocked | `run_backtest.py` | Backtest and daemon hour gate |
| `KELLY_FRACTION` | `0.18` | Base allowance fraction of bankroll | `run_backtest.py` | Allowance sizing in backtest/daemon |
| `DAEMON_MAX_TRADE_PCT` | `0.35` | Max scaled allowance fraction per trade | `run_backtest.py` | Allowance sizing cap in backtest/daemon |
| `DAEMON_MAX_CONTRACTS` | `none` | Optional hard contract cap (`none` disables cap) | `run_backtest.py` | Contract sizing in backtest/daemon |
| `DAEMON_INITIAL_IOC_USE_CAP` | `0` | If enabled, first IOC can lift from quoted entry to side cap to improve fill probability | `trade_daemon.py` | Live daemon execution logic |
| `DAEMON_INITIAL_IOC_MAX_LIFT_CENTS` | `6` | Maximum cents first IOC may lift toward side cap when cap-seeking is enabled | `trade_daemon.py` | Live daemon execution logic |
| `DAEMON_INITIAL_IOC_PAD_CENTS` | `0` | Baseline cents added to every first IOC before cap-seeking checks | `trade_daemon.py` | Live daemon execution logic |
| `DAEMON_INITIAL_IOC_PAD_LT65_EXTRA_CENTS` | `0` | Extra first-IOC pad added only when quoted entry price is under 65c | `trade_daemon.py` | Live daemon execution logic |
| `MAX_TRADES_PER_DAY` | `48` | Backtest risk-manager max trades/day | `run_backtest.py` | Backtest simulation |
| `MAX_DAILY_LOSS_PCT` | `25` | Backtest loss cap as % of bankroll | `run_backtest.py` | Backtest simulation |
| `MAX_DAILY_LOSS_CAP` | `500` | Backtest absolute daily loss cap dollars | `run_backtest.py` | Backtest simulation |
| `MAX_GIVEBACK_MULT` | `1.4` | Backtest giveback multiplier from session peak | `run_backtest.py` | Backtest simulation |
| `STARTING_CASH` | `200` | Initial bankroll in backtests | `run_backtest.py` | Backtest simulation/reporting |
| `RESET_TRIGGER` | `300` | Bankroll level that triggers skim/reset | `run_backtest.py` | Backtest simulation |
| `RESET_TO` | `200` | Post-skim trading bankroll | `run_backtest.py` | Backtest simulation |
| `DAEMON_MAX_DAILY_LOSS` | `50` | Daemon hard dollar stop for session | `trade_daemon.py` | Live daemon risk manager |
| `DAEMON_MAX_GIVEBACK_X` | `1.5` | Daemon giveback multiple from peak | `trade_daemon.py` | Live daemon risk manager |
| `DAEMON_MAX_DAILY_TRADES` | `48` | Daemon max fills per session | `trade_daemon.py` | Live daemon risk manager |
| `MCP_GOLDEN_MINUTES_LEFT_MIN` | `3` | Golden-zone lower bound for MCP timing gate | `mcp_server.py` | MCP `analyze_signal` timing gate |
| `MCP_GOLDEN_MINUTES_LEFT_MAX` | `12` | Golden-zone upper bound for MCP timing gate | `mcp_server.py` | MCP `analyze_signal` timing gate |
| `MCP_MIN_HISTORY` | `20` | Required Markov history length in MCP | `mcp_server.py` | MCP `analyze_signal` Markov readiness gate |
| `AGENT_ENTRY_MINUTES_LEFT` | `7.5` | Synthetic entry timing used by agent backtest replay | `backtest_agent.py` | Agent backtest market processing |
| `AGENT_DAYS_BACK` | `30` | Default days for CLI agent backtest run | `backtest_agent.py` | Agent backtest CLI defaults |
| `AGENT_STARTING_CASH` | `450` | Default starting bankroll for agent backtest run | `backtest_agent.py` | Agent backtest CLI defaults |

## Notes

- CLI args still override defaults where supported (for example, `--allowance-pct`, `--reset-trigger`, `--reset-to`).
- `trade_daemon.py --allowance-pct` now defaults to `KELLY_FRACTION * 100` (shared source of truth).
- `BLOCKED_UTC_HOURS` expects a comma-separated list like `8,11,16,18,21`.
- `DAEMON_MAX_CONTRACTS` accepts `none`, `null`, or empty value to disable the cap.
- `DAEMON_INITIAL_IOC_USE_CAP=1` enables cap-seeking first-shot IOC pricing: for approved entries, the first IOC may be submitted above the quoted `limit_price` up to `MAX_ENTRY_PRICE_YES`/`MAX_ENTRY_PRICE_NO`.
- `DAEMON_INITIAL_IOC_MAX_LIFT_CENTS` bounds that first-shot lift. Example: quoted `66c`, side cap `72c`, max lift `6` → first IOC submits at `72c`; max lift `4` → first IOC stays at `66c`.
- `DAEMON_INITIAL_IOC_PAD_CENTS` adds a baseline first-shot IOC pad before cap-seeking. Example: quoted `66c`, pad `2` → first IOC starts at `68c` (still capped by side cap).
- `DAEMON_INITIAL_IOC_PAD_LT65_EXTRA_CENTS` adds additional pad only for `<65c` entries. Example: quoted `59c`, base pad `2`, lt65 extra `2` → first IOC starts at `63c` before cap-seeking.
- Cap-seeking first IOC still uses normal limit-order price improvement: it matches cheapest available offers first and only pays up to submitted limit.
- `mcp_server.py`, `trade_daemon.py`, `research_loop.py`, and `backtest_agent.py` now all source shared trading thresholds from `run_backtest.py` (which loads `.env.local`).
