"""
KXBTC15M Backtest — directional momentum stack, around-the-clock

Goal: predict BTC up/down correctly for every window. Make the RIGHT BETS.
Signals:
  1. Markov gap ≥ 0.15    — 5-min chain, 65%+ directional confidence
  2. Markov persist ≥ 0.82— chain locked in state (not noise)
    3. ER >= threshold      — Kaufman efficiency; choppy regime = skip
  4. Velocity gate        — price not rushing toward strike >40% of crossing speed
  5. Vol ≤ 1.25×ref       — skip chaotic high-vol windows
  6. Entry 6–9 min left   — 6-9min = 98.3% WR on live fills
Sizing: fixed allowance per wager — bankroll × configured fraction
Daily loss cap 3%. No price gates — ANY profit after fees is acceptable.
"""

import argparse
import math
import time
import logging
import json
import os
from datetime import datetime, timezone, timedelta

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = logging.getLogger(__name__)

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
YAHOO_BASE  = "https://query2.finance.yahoo.com/v8/finance/chart"

_S = requests.Session()
_S.headers["User-Agent"] = "Mozilla/5.0"
_S.verify = False   # local backtest — macOS cert chain issue


def _load_env_local() -> None:
    """Load .env.local once, preferring workspace root then python-service."""
    base_dir = os.path.dirname(__file__)
    env_paths = [
        os.path.join(os.path.dirname(base_dir), ".env.local"),
        os.path.join(base_dir, ".env.local"),
    ]
    for env_path in env_paths:
        if not os.path.exists(env_path):
            continue
        with open(env_path, encoding="utf-8", errors="ignore") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[7:].strip()
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                    value = value[1:-1]
                os.environ.setdefault(key, value)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(float(raw.strip()))


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw.strip())


def _env_str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip()


def _env_optional_int(name: str, default: int | None) -> int | None:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    lowered = raw.strip().lower()
    if lowered in {"none", "null", "inf", "infinite", "unlimited"}:
        return None
    return int(float(raw.strip()))


def _env_int_set(name: str, default: set[int]) -> set[int]:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return set(default)
    values: set[int] = set()
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        values.add(int(token))
    return values


_load_env_local()

# ── Strategy parameters ───────────────────────────────────────────────────────
STARTING_CASH         = _env_float("STARTING_CASH", 200.00)
DAYS_BACK             = _env_int("DAYS_BACK", 30)
RESET_TRIGGER_DEFAULT = _env_float("RESET_TRIGGER", 300.0)  # Skim and reset bankroll when cash reaches this threshold.
RESET_TO_DEFAULT      = _env_float("RESET_TO", 200.0)       # Bankroll to continue trading with after each skim event.

# No d-gate
D_THRESHOLD           = _env_float("D_THRESHOLD", 0.0)
D_MAX_THRESHOLD       = _env_float("D_MAX_THRESHOLD", 99.0)

# Timing
MIN_MINUTES_LEFT      = _env_float("MIN_MINUTES_LEFT", 3)
MAX_MINUTES_LEFT      = _env_float("MAX_MINUTES_LEFT", 9)

# Kaufman Efficiency Ratio (ER)
# Backward compatibility: if MIN_EFFICIENCY_RATIO is unset, fall back to legacy MIN_HURST.
MIN_EFFICIENCY_RATIO  = _env_float("MIN_EFFICIENCY_RATIO", _env_float("MIN_HURST", 0.24))
ER_PERIOD             = _env_int("ER_PERIOD", 8)
# Legacy aliases kept for older imports.
MIN_HURST             = MIN_EFFICIENCY_RATIO
# Velocity gate
VEL_SAFETY_RATIO      = _env_float("VEL_SAFETY_RATIO", 0.40)

# Markov: 65%+ conviction + state must be locked in (not noise)
MARKOV_MIN_GAP        = _env_float("MARKOV_MIN_GAP", 0.11)
MIN_PERSIST           = _env_float("MIN_PERSIST", 0.82)
LT65_MIN_GAP          = _env_float("LT65_MIN_GAP", 0.14)  # For <65c entries, require stronger confidence.

# Vol regime
MAX_VOL_MULT          = _env_float("MAX_VOL_MULT", 1.35)

# UI-aligned allowance sizing.
# Use a fixed allowance equal to a configured fraction of current bankroll,
# then buy as many contracts as that allowance can afford at the live cost.

# Risk / sizing — side-specific entry price caps from live trade analysis (147 fills, Apr 19-22):
# YES≤72¢: all buckets ≤72¢ are +EV. YES 72¢+ = -$9.34/trade (67% WR vs 76% needed).
# NO≤65¢:  NO 65-72¢ = -$7.71/trade (53% WR vs 69% needed) — consensus-following bad payout.
# Backtest unchanged: EMPIRICAL_PRICE_BY_D never generates NO above ~38¢, so NO cap is implicit.
MIN_ENTRY_PRICE_RM    = _env_int("MIN_ENTRY_PRICE_RM", 0)     # no floor — let any price through
MAX_ENTRY_PRICE_YES   = _env_int("MAX_ENTRY_PRICE_YES", 72)   # ¢ — YES cap: market underprices our momentum signal at ≤72¢
MAX_ENTRY_PRICE_NO    = _env_int("MAX_ENTRY_PRICE_NO", 65)    # ¢ — NO cap: above 65¢ NO = consensus trade with bad payout ratio
MAX_ENTRY_PRICE_RM    = _env_int("MAX_ENTRY_PRICE_RM", MAX_ENTRY_PRICE_YES)  # kept for external imports expecting this name
MIN_DIST_PCT          = _env_float("MIN_DIST_PCT", 0.04)
MAX_CONTRACTS_RM      = _env_int("MAX_CONTRACTS_RM", 500)
REF_VOL_15M           = _env_float("REF_VOL_15M", 0.002)
MAX_TRADE_PCT         = _env_float("MAX_TRADE_PCT", 0.20)
KELLY_FRACTION        = _env_float("KELLY_FRACTION", 0.18)
DAEMON_MAX_TRADE_PCT  = _env_float("DAEMON_MAX_TRADE_PCT", 0.35)
DAEMON_MAX_CONTRACTS  = _env_optional_int("DAEMON_MAX_CONTRACTS", None)  # No hard contract cap; sizing is allowance-driven.
SIZING_MODE           = _env_str("SIZING_MODE", "allowance")
MAX_TRADES_PER_DAY    = _env_int("MAX_TRADES_PER_DAY", 48)
MAX_DAILY_LOSS_PCT    = _env_float("MAX_DAILY_LOSS_PCT", 25)
MAX_DAILY_LOSS_FLOOR  = _env_float("MAX_DAILY_LOSS_FLOOR", 0)
MAX_DAILY_LOSS_CAP    = _env_float("MAX_DAILY_LOSS_CAP", 500)
MAX_GIVEBACK_MULT     = _env_float("MAX_GIVEBACK_MULT", 1.4)
POLLER_INTERVAL_MIN   = _env_float("POLLER_INTERVAL_MIN", 0.5)
BLOCKED_UTC_HOURS     = _env_int_set("BLOCKED_UTC_HOURS", {8, 11, 16, 18, 21})  # live data: 8=44%WR, 16=36%WR, 21=40%WR (147 fills Apr 19-22)

MAKER_FEE_RATE      = _env_float("MAKER_FEE_RATE", 0.0175)
MAX_ORDER_DEPTH     = _env_int("MAX_ORDER_DEPTH", 25)
SLIPPAGE_FREE_CTRS  = _env_int("SLIPPAGE_FREE_CTRS", 10)
SLIPPAGE_CENTS_PER  = _env_float("SLIPPAGE_CENTS_PER", 0.5)

EMPIRICAL_PRICE_BY_D = [
    (0.0, 0.5,  62.3),
    (0.5, 0.8,  72.7),
    (0.8, 1.0,  79.1),
    (1.0, 1.2,  80.8),
    (1.2, 1.5,  84.6),
    (1.5, 2.0,  83.6),
    (2.0, 99.0, 71.0),
]

# ── Markov chain ──────────────────────────────────────────────────────────────
NUM_STATES    = 9
MARKOV_CANDLE = 5
MIN_HISTORY   = 20
MARKOV_WINDOW = 480

STATE_BOUNDS  = [-3.35, -2.24, -1.12, -0.45, 0.45, 1.12, 2.24, 3.35]
STATE_RETURNS = [-2.0, -1.25, -0.75, -0.35, 0.0,  0.35, 0.75, 1.25, 2.0]
STATE_VOL     = [ 1.0,  0.35,  0.25,  0.15, 0.10, 0.15, 0.25, 0.35, 1.0]


# ── Math helpers ──────────────────────────────────────────────────────────────

def norm_cdf(z: float) -> float:
    sign = 1 if z >= 0 else -1
    x    = abs(z)
    t    = 1.0 / (1.0 + 0.2316419 * x)
    poly = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937 + t * (-1.821255978 + t * 1.330274429))))
    pdf  = math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)
    return 0.5 + sign * (0.5 - pdf * poly)


def compute_efficiency_ratio(candles_newest: list, period: int = ER_PERIOD) -> float | None:
    """
    Kaufman's Efficiency Ratio on close prices.
    ER = abs(close_t - close_{t-period}) / sum(abs(delta_close_i))
    Range: 0..1 (higher = cleaner trend, lower = choppy/noisy).
    """
    closes = [float(c[4]) for c in reversed(candles_newest)]
    if period < 1 or len(closes) < period + 1:
        return None

    net_change = abs(closes[-1] - closes[-1 - period])
    path = 0.0
    for i in range(len(closes) - period, len(closes)):
        path += abs(closes[i] - closes[i - 1])

    if path <= 0:
        return 0.0
    return max(0.0, min(1.0, net_change / path))


def compute_hurst(candles_newest: list) -> float | None:
    """Legacy alias kept for compatibility; now returns Kaufman ER."""
    return compute_efficiency_ratio(candles_newest, period=ER_PERIOD)


def compute_rsi(closes: list, period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains   = [max(c, 0.0) for c in changes]
    losses  = [max(-c, 0.0) for c in changes]
    avg_g   = sum(gains[:period])  / period
    avg_l   = sum(losses[:period]) / period
    for i in range(period, len(changes)):
        avg_g = (avg_g * (period - 1) + gains[i])  / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    return 100.0 if avg_l == 0 else 100.0 - 100.0 / (1.0 + avg_g / avg_l)


def compute_ema(closes: list, period: int) -> float | None:
    if len(closes) < period:
        return None
    k   = 2.0 / (period + 1)
    ema = sum(closes[:period]) / period
    for p in closes[period:]:
        ema = p * k + ema * (1.0 - k)
    return ema


# ── Markov chain ──────────────────────────────────────────────────────────────

def price_change_to_state(pct: float) -> int:
    for i, b in enumerate(STATE_BOUNDS):
        if pct < b:
            return i
    return NUM_STATES - 1


def build_transition_matrix(history: list) -> list:
    counts = [[0.0] * NUM_STATES for _ in range(NUM_STATES)]
    for i in range(len(history) - 1):
        s_from, s_to = history[i], history[i + 1]
        if 0 <= s_from < NUM_STATES and 0 <= s_to < NUM_STATES:
            counts[s_from][s_to] += 1.0
    P = []
    for row in counts:
        total = sum(row)
        P.append([v / total for v in row] if total > 0 else [1.0 / NUM_STATES] * NUM_STATES)
    return P


def predict_from_momentum(P: list, current_state: int,
                          minutes_left: float, dist_pct: float) -> dict:
    T              = max(1, round(minutes_left / MARKOV_CANDLE))
    required_drift = -dist_pct
    dist      = [0.0] * NUM_STATES
    dist[current_state] = 1.0
    exp_drift = 0.0
    var_sum   = 0.0
    for _ in range(T):
        step_mean = sum(dist[i] * STATE_RETURNS[i] for i in range(NUM_STATES))
        step_e2   = sum(dist[i] * (STATE_VOL[i] ** 2 + STATE_RETURNS[i] ** 2) for i in range(NUM_STATES))
        exp_drift += step_mean
        var_sum   += max(0.0, step_e2 - step_mean ** 2)
        nxt = [0.0] * NUM_STATES
        for i in range(NUM_STATES):
            for j in range(NUM_STATES):
                nxt[j] += dist[i] * P[i][j]
        dist = nxt
    sigma   = math.sqrt(max(var_sum, 0.01))
    z_score = (exp_drift - required_drift) / sigma
    p_yes   = norm_cdf(z_score)
    row     = P[current_state]
    j_star  = max(range(NUM_STATES), key=lambda j: row[j])
    persist = row[current_state]
    return {
        'p_yes': p_yes, 'p_no': 1.0 - p_yes,
        'expected_drift_pct': exp_drift, 'required_drift_pct': required_drift,
        'sigma': sigma, 'z_score': z_score, 'persist': persist, 'j_star': j_star,
        'enter_yes': p_yes >= 0.61 and persist >= 0.80,
        'enter_no':  p_yes <= 0.39 and persist >= 0.80,
    }


def build_markov_history(candles_5m_oldest: list, up_to_ts: float) -> list:
    relevant = [c for c in candles_5m_oldest if c[0] + MARKOV_CANDLE * 60 <= up_to_ts]
    if len(relevant) < 2:
        return []
    states = []
    for i in range(1, len(relevant)):
        prev, curr = relevant[i - 1][4], relevant[i][4]
        if prev > 0:
            states.append(price_change_to_state((curr - prev) / prev * 100.0))
    return states[-MARKOV_WINDOW:]


def gk_vol(candles_newest: list):
    K     = 2 * math.log(2) - 1
    terms = []
    for c in candles_newest:
        lo, hi, op, cl = c[1], c[2], c[3], c[4]
        if op <= 0 or lo <= 0 or hi <= 0:
            continue
        terms.append(0.5 * math.log(hi / lo) ** 2 - K * math.log(cl / op) ** 2)
    if len(terms) < 2:
        return None
    return math.sqrt(max(0.0, sum(terms) / len(terms)))


# ── Data fetch ────────────────────────────────────────────────────────────────

def _get(url: str, retries: int = 3):
    for attempt in range(retries):
        try:
            r = _S.get(url, timeout=30)
            if r.status_code == 429:
                time.sleep(2 ** attempt); continue
            r.raise_for_status()
            return r.json()
        except Exception:
            if attempt < retries - 1:
                time.sleep(1); continue
            raise


def _parse_yahoo(data: dict) -> list:
    result     = data['chart']['result'][0]
    timestamps = result['timestamp']
    quote      = result['indicators']['quote'][0]
    candles    = []
    for i, ts in enumerate(timestamps):
        o = quote['open'][i]; h = quote['high'][i]
        l = quote['low'][i];  c = quote['close'][i]
        v = (quote.get('volume') or [None])[i] or 0
        if o is None or h is None or l is None or c is None:
            continue
        candles.append([int(ts), float(l), float(h), float(o), float(c), float(v)])
    candles.sort(key=lambda c: c[0])
    return candles


def _cache(name: str, days_back: int | None = None) -> str:
    suffix = f"_{days_back}d" if days_back else ""
    return os.path.join(os.path.dirname(__file__), f"_backtest_cache_{name}{suffix}.json")


def _cache_is_sufficient(candles: list, days_back: int, interval_min: int) -> bool:
    # Yahoo can occasionally return sparse data; reject clearly undersized caches.
    expected = days_back * 24 * (60 // interval_min)
    min_expected = max(96, int(expected * 0.6))
    return len(candles) >= min_expected


def fetch_candles_15m(days_back: int = 60) -> list:
    p = _cache('15m', days_back)
    p_legacy = _cache('15m')
    if os.path.exists(p) and time.time() - os.path.getmtime(p) < 3600:
        with open(p) as f:
            data = json.load(f)
        if _cache_is_sufficient(data, days_back, 15):
            log.info(f"Loaded {len(data)} BTC/15m candles (cache)")
            return data
    if os.path.exists(p_legacy) and time.time() - os.path.getmtime(p_legacy) < 3600:
        with open(p_legacy) as f:
            data = json.load(f)
        if _cache_is_sufficient(data, days_back, 15):
            log.info(f"Loaded {len(data)} BTC/15m candles (legacy cache)")
            return data
    data = _parse_yahoo(_get(f"{YAHOO_BASE}/BTC-USD?interval=15m&range={days_back}d"))
    with open(p, 'w') as f: json.dump(data, f)
    log.info(f"Fetched {len(data)} BTC/15m candles"); return data


def fetch_candles_5m(days_back: int = 60) -> list:
    p = _cache('5m', days_back)
    p_legacy = _cache('5m')
    if os.path.exists(p) and time.time() - os.path.getmtime(p) < 3600:
        with open(p) as f:
            data = json.load(f)
        if _cache_is_sufficient(data, days_back, 5):
            log.info(f"Loaded {len(data)} BTC/5m candles (cache)")
            return data
    if os.path.exists(p_legacy) and time.time() - os.path.getmtime(p_legacy) < 3600:
        with open(p_legacy) as f:
            data = json.load(f)
        if _cache_is_sufficient(data, days_back, 5):
            log.info(f"Loaded {len(data)} BTC/5m candles (legacy cache)")
            return data
    data = _parse_yahoo(_get(f"{YAHOO_BASE}/BTC-USD?interval=5m&range={days_back}d"))
    with open(p, 'w') as f: json.dump(data, f)
    log.info(f"Fetched {len(data)} BTC/5m candles"); return data


def fetch_settled_markets(days_back: int) -> list:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    markets, cursor = [], None
    while True:
        url = f"{KALSHI_BASE}/markets?series_ticker=KXBTC15M&status=settled&limit=200"
        if cursor: url += f"&cursor={cursor}"
        try:
            data = _get(url)
        except Exception as e:
            log.error(f"Kalshi fetch failed: {e}"); break
        batch = data.get('markets', [])
        if not batch: break
        done = False
        for m in batch:
            ct = m.get('close_time') or m.get('expiration_time')
            if not ct: continue
            try:
                close_dt = datetime.fromisoformat(ct.replace('Z', '+00:00'))
            except ValueError:
                continue
            if close_dt < cutoff: done = True; break
            fs     = m.get('floor_strike')
            result = m.get('result')
            ticker = m.get('ticker', '')
            if fs is not None and result in ('yes', 'no') and ticker:
                try:
                    markets.append({'ticker': ticker, 'floor_strike': float(fs),
                                    'result': result, 'close_time': ct})
                except (ValueError, TypeError): pass
        if done: break
        cursor = data.get('cursor')
        if not cursor: break
        time.sleep(0.15)
    log.info(f"Fetched {len(markets)} settled markets ({days_back}d)")
    return markets


# ── Process one market window ─────────────────────────────────────────────────

def process_market(mkt: dict, candles_15m: list, candles_5m: list) -> dict | None:
    ticker  = mkt['ticker']
    strike  = mkt['floor_strike']
    result  = mkt['result']

    try:
        close_dt = datetime.fromisoformat(mkt['close_time'].replace('Z', '+00:00'))
    except ValueError:
        return None

    open_ts  = (close_dt - timedelta(minutes=15)).timestamp()
    close_ts = close_dt.timestamp()

    check_times = []
    t = open_ts + POLLER_INTERVAL_MIN * 60
    while t <= close_ts - MIN_MINUTES_LEFT * 60:
        check_times.append(t)
        t += POLLER_INTERVAL_MIN * 60

    c5_by_ts: dict[int, list] = {c[0]: c for c in candles_5m}

    for check_ts in check_times:
        minutes_left = (close_ts - check_ts) / 60.0
        if minutes_left > MAX_MINUTES_LEFT or minutes_left < MIN_MINUTES_LEFT:
            continue

        # UTC hour gate
        if datetime.fromtimestamp(check_ts, tz=timezone.utc).hour in BLOCKED_UTC_HOURS:
            continue

        # 15-min context for GK vol + ER
        ctx15 = [c for c in candles_15m if c[0] + 900 <= check_ts]
        if len(ctx15) < 12:
            continue
        last_32_15m = list(reversed(ctx15[-32:]))

        # Gate 6 — vol regime (check before spot lookup to fail fast)
        gk = gk_vol(last_32_15m[:16])
        if not gk or gk <= 0:
            continue
        if gk > REF_VOL_15M * MAX_VOL_MULT:
            continue   # high-vol regime: unpredictable

        # Gate 3 — Efficiency Ratio trending regime
        #efficiency_ratio = compute_efficiency_ratio(last_32_15m[:24], period=ER_PERIOD)
        #if efficiency_ratio is not None and efficiency_ratio < MIN_EFFICIENCY_RATIO:
        #    continue   # choppy regime: direction lock unreliable
        # Above is the old way; now we compute ER using the exact same data-feed structure as live, to avoid any lookahead bias.
        
        # 1. Determine exactly how many candles the function needs (period + 1)
        # For ER_PERIOD = 20, this will grab exactly 21 candles
        required_window = ER_PERIOD + 1

        # 2. Slice from the end [-required_window:] to get the most recent candles
        # Then reverse it so the newest candle sits at index 0, matching live data format
        backtest_candles_feed = list(reversed(last_32_15m[-required_window:]))

        # 3. Compute the ratio using the exact same restricted data-feed structure as live
        efficiency_ratio = compute_efficiency_ratio(backtest_candles_feed, period=ER_PERIOD)

        # 4. Filter out the choppy regimes cleanly
        if efficiency_ratio is None or efficiency_ratio < MIN_EFFICIENCY_RATIO:
            continue   # choppy regime: direction lock unreliable

        # Spot price from last completed 5-min candle
        c5_ts  = int(check_ts // (MARKOV_CANDLE * 60)) * MARKOV_CANDLE * 60 - MARKOV_CANDLE * 60
        c5_bar = c5_by_ts.get(c5_ts) or c5_by_ts.get(c5_ts - MARKOV_CANDLE * 60)
        spot   = c5_bar[4] if c5_bar else last_32_15m[0][4]
        if spot <= 0:
            continue

        dist_pct     = (spot - strike) / strike * 100.0
        above_strike = spot >= strike

        if abs(dist_pct) < MIN_DIST_PCT:
            continue

        # Gate 4 — velocity: price not rushing toward strike
        c5_prev3 = c5_by_ts.get(c5_ts - 3 * MARKOV_CANDLE * 60)   # 15 min ago
        if c5_bar and c5_prev3 and c5_prev3[4] > 0:
            vel_per_min  = (c5_bar[4] - c5_prev3[4]) / 15.0   # $/min over last 15 min
            dist_usd     = abs(spot - strike)
            crossing_vel = dist_usd / minutes_left if minutes_left > 0 else 1e9
            toward_strike = (above_strike and vel_per_min < 0) or (not above_strike and vel_per_min > 0)
            if toward_strike and crossing_vel > 0 and abs(vel_per_min) > VEL_SAFETY_RATIO * crossing_vel:
                continue   # price heading toward strike too fast

        # Gate 1 — d-score edge zone
        candles_left = minutes_left / 15.0
        try:
            d = math.log(spot / strike) / (gk * math.sqrt(candles_left))
        except (ValueError, ZeroDivisionError):
            continue
        d_abs = abs(d)
        if D_THRESHOLD > 0 and (d_abs < D_THRESHOLD or d_abs > D_MAX_THRESHOLD):
            continue

        # Markov chain signal
        history = build_markov_history(candles_5m, check_ts)
        if len(history) < MIN_HISTORY:
            continue

        c5_prev = c5_by_ts.get(c5_ts - MARKOV_CANDLE * 60)
        if c5_bar and c5_prev and c5_prev[4] > 0:
            current_state = price_change_to_state((c5_bar[4] - c5_prev[4]) / c5_prev[4] * 100.0)
        else:
            current_state = 4

        full_history = history + [current_state]
        P            = build_transition_matrix(full_history)
        forecast     = predict_from_momentum(P, current_state, minutes_left, dist_pct)
        p_yes        = forecast['p_yes']

        # Gate 5 — Markov confidence
        p_gap = abs(p_yes - 0.5)
        if p_gap < MARKOV_MIN_GAP:
            continue

        # Gate 6 — Markov persistence: chain locked in state (not noise)
        if forecast['persist'] < MIN_PERSIST:
            continue

        rec = 'yes' if p_yes > 0.5 else 'no'

        # Build 5-min close window (25 bars, oldest-first)
        c5_closes: list[float] = []
        for i in range(24, -1, -1):
            bar = c5_by_ts.get(c5_ts - i * MARKOV_CANDLE * 60)
            if bar:
                c5_closes.append(float(bar[4]))

        rsi = compute_rsi(c5_closes) if len(c5_closes) >= 15 else None

        # (no RSI gate — tested, hurts WR; EV/price gates already screen bad trades)
        # (no state-alignment gate — tested, counterproductive at gap=0.15)

        # Entry price from empirical table
        in_money_price = 80.0
        for d_lo, d_hi, emp_p in EMPIRICAL_PRICE_BY_D:
            if d_lo <= d_abs < d_hi:
                in_money_price = emp_p; break

        if above_strike:
            yes_ask, no_ask = in_money_price, 100.0 - in_money_price
        else:
            no_ask, yes_ask = in_money_price, 100.0 - in_money_price

        limit_price_cents = round(yes_ask if rec == 'yes' else no_ask)
        side_max_bt = MAX_ENTRY_PRICE_YES if rec == 'yes' else MAX_ENTRY_PRICE_NO
        if side_max_bt > 0 and limit_price_cents > side_max_bt:
            continue

        # Subset skip for weaker low-price entries: keep <65c only when confidence is stronger.
        if limit_price_cents < 65 and p_gap < LT65_MIN_GAP:
            continue

        confidence = 'high' if p_gap >= 0.15 else 'medium' if p_gap >= 0.07 else 'low'
        won        = (rec == result)

        return {
            'ticker':             ticker,
            'entry_dt':           datetime.fromtimestamp(check_ts, tz=timezone.utc).isoformat(),
            'expires_dt':         close_dt.isoformat(),
            'side':               rec,
            'spot':               round(spot, 2),
            'strike':             round(strike, 2),
            'dist_pct':           round(dist_pct, 4),
            'minutes_left':       round(minutes_left, 1),
            'd_score':            round(d, 3),
            'p_yes':              round(p_yes, 4),
            'p_no':               round(1.0 - p_yes, 4),
            'z_score':            round(forecast['z_score'], 3),
            'persist':            round(forecast['persist'], 3),
            'expected_drift_pct': round(forecast['expected_drift_pct'], 4),
            'required_drift_pct': round(forecast['required_drift_pct'], 4),
            'sigma':              round(forecast['sigma'], 4),
            'enter_signal':       forecast['enter_yes'] if rec == 'yes' else forecast['enter_no'],
            'limit_price_cents':  limit_price_cents,
            'gk_vol':             round(gk, 6),
            'efficiency_ratio':   round(efficiency_ratio, 3) if efficiency_ratio is not None else None,
            'hurst':              round(efficiency_ratio, 3) if efficiency_ratio is not None else None,
            'rsi':                round(rsi, 1) if rsi is not None else None,
            'confidence':         confidence,
            'history_len':        len(full_history),
            'above_strike':       above_strike,
            'result':             result,
            'outcome':            'WIN' if won else 'LOSS',
        }

    return None


# ── Fee + simulation ──────────────────────────────────────────────────────────

def kalshi_fee(contracts: int, price_cents: float) -> float:
    p = price_cents / 100.0
    return math.ceil(MAKER_FEE_RATE * contracts * p * (1 - p) * 100) / 100


def allowance_contracts(bankroll: float, total_cost_per_contract: float,
                        allowance_fraction: float | None = None,
                        max_contracts: int | None = DAEMON_MAX_CONTRACTS) -> tuple[float, int]:
    if allowance_fraction is None:
        allowance_fraction = KELLY_FRACTION
    allowance = max(1.0, bankroll * allowance_fraction)
    if total_cost_per_contract <= 0:
        return allowance, 0
    contracts = max(1, math.floor(allowance / total_cost_per_contract))
    if max_contracts is not None:
        contracts = min(contracts, max_contracts)
    return allowance, contracts


def price_bucket_sizing(limit_price_cents: int) -> tuple[float, str]:
    """Match live daemon: scale allowance by entry-price quality bucket."""
    if limit_price_cents < 65:
        return 0.50, "lt_65"
    if limit_price_cents <= 73:
        return 1.00, "65_73"
    return 1.00, "gt_73"


def legacy_kelly_contracts(bankroll: float, total_cost_per_contract: float,
                           p_win: float, limit_price_cents: float,
                           max_contracts: int | None = DAEMON_MAX_CONTRACTS) -> tuple[float, float, int]:
    p_dollars     = limit_price_cents / 100.0
    fee_per_c_raw = MAKER_FEE_RATE * p_dollars * (1 - p_dollars)
    net_win_per_c = (1.0 - p_dollars) - fee_per_c_raw
    b_odds        = net_win_per_c / total_cost_per_contract if total_cost_per_contract > 0 else 1.0
    kelly_full    = max(0.0, (b_odds * p_win - (1.0 - p_win)) / b_odds) if b_odds > 0 else 0.0

    if 65 <= limit_price_cents <= 73:
        frac = 0.35
    elif 73 < limit_price_cents <= 79:
        frac = 0.12
    elif 79 < limit_price_cents <= 85:
        frac = 0.08
    else:
        frac = 0.05

    risk_pct  = min(DAEMON_MAX_TRADE_PCT, frac * kelly_full)
    contracts = max(1, round(bankroll * risk_pct / total_cost_per_contract))
    if max_contracts is not None:
        contracts = min(contracts, max_contracts)
    return kelly_full, risk_pct, contracts


def simulate(records: list, sizing_mode: str | None = None,
             reset_trigger: float | None = None,
             reset_to: float | None = None) -> tuple[float, float, int]:
    if sizing_mode is None:
        sizing_mode = SIZING_MODE
    if reset_trigger is None:
        reset_trigger = RESET_TRIGGER_DEFAULT
    if reset_to is None:
        reset_to = RESET_TO_DEFAULT

    ET_OFFSET = timedelta(hours=5)
    cash = STARTING_CASH
    withdrawn_total = 0.0
    reset_count = 0
    session_daily_pnl = session_peak_pnl = 0.0
    session_trade_count = 0
    session_date_et = None

    for r in records:
        entry_et = datetime.fromisoformat(r['entry_dt']) - ET_OFFSET
        date_et  = entry_et.strftime('%Y-%m-%d')
        if date_et != session_date_et:
            session_date_et = date_et
            session_daily_pnl = session_peak_pnl = 0.0
            session_trade_count = 0

        lp = r['limit_price_cents']
        max_daily_loss   = -max(MAX_DAILY_LOSS_FLOOR, min(MAX_DAILY_LOSS_CAP, cash * MAX_DAILY_LOSS_PCT / 100))
        giveback_limit   = abs(max_daily_loss) * MAX_GIVEBACK_MULT
        giveback_dollars = (session_peak_pnl - session_daily_pnl) if session_peak_pnl > 0 else 0.0

        skip_reason = None
        side_max_sim = MAX_ENTRY_PRICE_YES if r['side'] == 'yes' else MAX_ENTRY_PRICE_NO
        if side_max_sim > 0 and lp > side_max_sim: skip_reason = f"price {lp}¢ > {'YES' if r['side']=='yes' else 'NO'} cap {side_max_sim}¢"
        elif session_daily_pnl <= max_daily_loss:      skip_reason = 'daily_loss_limit'
        elif giveback_dollars >= giveback_limit:        skip_reason = 'session_giveback'
        elif session_trade_count >= MAX_TRADES_PER_DAY: skip_reason = 'max_trades'

        if skip_reason:
            r.update(
                contracts=0,
                cost=0.0,
                pnl=0.0,
                cash_after=round(cash, 2),
                withdrawn_total_after=round(withdrawn_total, 2),
                net_equity_after=round(cash + withdrawn_total, 2),
                skipped_reason=skip_reason,
            )
            continue

        p_dollars        = lp / 100.0
        fee_per_c_raw    = MAKER_FEE_RATE * p_dollars * (1 - p_dollars)
        cost_per_contract = p_dollars + fee_per_c_raw
        p_win            = r['p_yes'] if r['side'] == 'yes' else (1.0 - r['p_yes'])

        allowance  = None
        kelly_full = None
        risk_pct   = None
        sizing_mult = None
        sizing_bucket = None
        scaled_allowance_fraction = None
        if sizing_mode == 'legacy-kelly':
            kelly_full, risk_pct, contracts = legacy_kelly_contracts(cash, cost_per_contract, p_win, lp)
        else:
            sizing_mult, sizing_bucket = price_bucket_sizing(lp)
            scaled_allowance_fraction = min(DAEMON_MAX_TRADE_PCT, max(0.01, KELLY_FRACTION * sizing_mult))
            allowance, contracts = allowance_contracts(
                cash,
                cost_per_contract,
                allowance_fraction=scaled_allowance_fraction,
                max_contracts=DAEMON_MAX_CONTRACTS,
            )

        avg_cents = (SLIPPAGE_FREE_CTRS * lp + (contracts - SLIPPAGE_FREE_CTRS) *
                     (lp + (contracts - SLIPPAGE_FREE_CTRS) * SLIPPAGE_CENTS_PER / 2)
                     ) / contracts if contracts > SLIPPAGE_FREE_CTRS else float(lp)

        p_eff     = avg_cents / 100.0
        fee_per_c = kalshi_fee(contracts, avg_cents) / contracts
        net_win   = (1.0 - p_eff) - fee_per_c
        net_loss  = -p_eff - fee_per_c

        pnl  = contracts * (net_win if r['outcome'] == 'WIN' else net_loss)
        cash = max(0.0, cash + pnl)

        # Optional bankroll skim/reset: lock in gains and keep betting from a fixed base bankroll.
        if reset_trigger and reset_trigger > 0 and reset_to is not None and cash >= reset_trigger and cash > reset_to:
            skim = cash - reset_to
            withdrawn_total += skim
            reset_count += 1
            cash = reset_to

        session_daily_pnl   += pnl
        session_peak_pnl     = max(session_peak_pnl, session_daily_pnl)
        session_trade_count += 1

        update = {
            'sizing_mode': sizing_mode,
            'contracts': contracts,
            'cost': round(contracts * (p_eff + fee_per_c), 2),
            'pnl': round(pnl, 2),
            'cash_after': round(cash, 2),
            'withdrawn_total_after': round(withdrawn_total, 2),
            'net_equity_after': round(cash + withdrawn_total, 2),
            'outcome_sim': r['outcome'],
        }
        if allowance is not None:
            update['base_allowance_pct'] = round(KELLY_FRACTION * 100, 2)
            update['allowance_pct'] = round((scaled_allowance_fraction or KELLY_FRACTION) * 100, 2)
            update['sizing_multiplier'] = round(sizing_mult or 1.0, 3)
            update['sizing_bucket'] = sizing_bucket or 'unknown'
            update['allowance'] = round(allowance, 2)
        if kelly_full is not None and risk_pct is not None:
            update['kelly_full'] = round(kelly_full, 6)
            update['risk_pct'] = round(risk_pct, 6)
        r.update(update)

    return cash, withdrawn_total, reset_count


def summarize_run(markets: list, records: list, final_cash: float, sizing_mode: str,
                  withdrawn_total: float = 0.0,
                  reset_count: int = 0) -> dict:
    executed   = [r for r in records if r.get('contracts', 0) > 0]
    skipped_rm = [r for r in records if r.get('contracts', 0) == 0]
    wins       = [r for r in executed if r.get('outcome_sim', r['outcome']) == 'WIN']
    losses     = [r for r in executed if r.get('outcome_sim', r['outcome']) == 'LOSS']
    strong     = [r for r in executed if r.get('enter_signal')]
    strong_w   = [r for r in strong if r.get('outcome_sim', r['outcome']) == 'WIN']

    skip_reasons: dict = {}
    for r in skipped_rm:
        key = r.get('skipped_reason', 'unknown')
        skip_reasons[key] = skip_reasons.get(key, 0) + 1

    max_cash = STARTING_CASH
    max_dd_trading = 0.0
    max_net_equity = STARTING_CASH
    max_dd_net = 0.0
    cur_s = max_ws = max_ls = 0
    last_oc = None
    for r in executed:
        oc = r.get('outcome_sim', r['outcome'])
        max_cash = max(max_cash, r['cash_after'])
        max_dd_trading = max(max_dd_trading, (max_cash - r['cash_after']) / max(max_cash, 1e-9) * 100)

        net_after = r.get('net_equity_after')
        if net_after is None:
            net_after = r['cash_after'] + r.get('withdrawn_total_after', 0.0)
        max_net_equity = max(max_net_equity, float(net_after))
        max_dd_net = max(max_dd_net, (max_net_equity - float(net_after)) / max(max_net_equity, 1e-9) * 100)

        cur_s    = cur_s + 1 if oc == last_oc else 1
        if oc == 'WIN':
            max_ws = max(max_ws, cur_s)
        else:
            max_ls = max(max_ls, cur_s)
        last_oc = oc

    wr       = len(wins) / max(len(executed), 1) * 100
    pnl      = final_cash - STARTING_CASH
    net_equity = final_cash + withdrawn_total
    net_pnl    = net_equity - STARTING_CASH
    ret_pct  = (final_cash / STARTING_CASH - 1) * 100
    net_ret_pct = (net_equity / STARTING_CASH - 1) * 100
    avg_win  = sum(r['pnl'] for r in wins) / max(len(wins), 1)
    avg_loss = sum(r['pnl'] for r in losses) / max(len(losses), 1)
    gw       = sum(r['pnl'] for r in wins)
    gl       = abs(sum(r['pnl'] for r in losses))

    return {
        'sizing_mode': sizing_mode,
        'markets': markets,
        'records': records,
        'final_cash': final_cash,
        'withdrawn_total': withdrawn_total,
        'reset_count': reset_count,
        'net_equity': net_equity,
        'executed': executed,
        'skipped_rm': skipped_rm,
        'wins': wins,
        'losses': losses,
        'strong': strong,
        'strong_w': strong_w,
        'skip_reasons': skip_reasons,
        'max_cash': max_cash,
        'max_dd': max_dd_trading,
        'max_dd_trading': max_dd_trading,
        'max_net_equity': max_net_equity,
        'max_dd_net': max_dd_net,
        'wr': wr,
        'pnl': pnl,
        'net_pnl': net_pnl,
        'ret_pct': ret_pct,
        'net_ret_pct': net_ret_pct,
        'avg_win': avg_win,
        'avg_loss': avg_loss,
        'gw': gw,
        'gl': gl,
        'max_ws': max_ws,
        'max_ls': max_ls,
    }


def print_compare_summary(allowance_summary: dict, legacy_summary: dict) -> None:
    print("\n" + "=" * 86)
    print("  SIZING MODE COMPARISON")
    print("=" * 86)
    print(f"  {'Metric':<24} {'Allowance':>14} {'Legacy Kelly':>16}")
    print("  " + "-" * 56)
    print(f"  {'Allowance pct':<24} {f'{KELLY_FRACTION*100:.0f}%':>14} {'edge-scaled':>16}")
    print(f"  {'Executed trades':<24} {len(allowance_summary['executed']):>14} {len(legacy_summary['executed']):>16}")
    print(f"  {'Win rate':<24} {f"{allowance_summary['wr']:.1f}%":>14} {f"{legacy_summary['wr']:.1f}%":>16}")
    print(f"  {'Return':<24} {f"{allowance_summary['ret_pct']:+.1f}%":>14} {f"{legacy_summary['ret_pct']:+.1f}%":>16}")
    print(f"  {'DD (trading bankroll)':<24} {f"{allowance_summary['max_dd_trading']:.1f}%":>14} {f"{legacy_summary['max_dd_trading']:.1f}%":>16}")
    print(f"  {'DD (net equity)':<24} {f"{allowance_summary['max_dd_net']:.1f}%":>14} {f"{legacy_summary['max_dd_net']:.1f}%":>16}")
    print(f"  {'Final cash':<24} {f"${allowance_summary['final_cash']:.2f}":>14} {f"${legacy_summary['final_cash']:.2f}":>16}")
    print(f"  {'Withdrawn':<24} {f"${allowance_summary['withdrawn_total']:.2f}":>14} {f"${legacy_summary['withdrawn_total']:.2f}":>16}")
    print(f"  {'Net equity':<24} {f"${allowance_summary['net_equity']:.2f}":>14} {f"${legacy_summary['net_equity']:.2f}":>16}")
    print(f"  {'Profit factor':<24} {f"{allowance_summary['gw']/max(allowance_summary['gl'],0.01):.2f}x":>14} {f"{legacy_summary['gw']/max(legacy_summary['gl'],0.01):.2f}x":>16}")
    print("=" * 86)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    global KELLY_FRACTION, SIZING_MODE

    # Configure console logging only for direct CLI execution.
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    parser = argparse.ArgumentParser(description="KXBTC15M sizing backtest")
    parser.add_argument("--allowance-pct", type=float, default=20.0,
                        help="Percent of bankroll allocated per wager (default: 20)")
    parser.add_argument("--reset-trigger", type=float, default=RESET_TRIGGER_DEFAULT,
                        help="If >0 and bankroll reaches this level, skim and reset bankroll (default: disabled)")
    parser.add_argument("--reset-to", type=float, default=RESET_TO_DEFAULT,
                        help="Bankroll to continue trading with after skim/reset (default: 200)")
    parser.add_argument("--sizing-mode", choices=["allowance", "legacy-kelly", "compare"], default="allowance",
                        help="Backtest sizing model: live-matching allowance, legacy Kelly, or side-by-side compare")
    args = parser.parse_args()
    KELLY_FRACTION = args.allowance_pct / 100.0
    SIZING_MODE = args.sizing_mode if args.sizing_mode != 'compare' else 'allowance'

    log.info(f"=== Directional KXBTC15M Backtest · {DAYS_BACK}d · ${STARTING_CASH} start ===")

    markets     = fetch_settled_markets(DAYS_BACK)
    candles_15m = fetch_candles_15m(60)
    candles_5m  = fetch_candles_5m(60)

    if not markets or len(candles_15m) < 100 or len(candles_5m) < 100:
        print("Insufficient data"); return

    records, skipped = [], 0
    for mkt in markets:
        try:
            r = process_market(mkt, candles_15m, candles_5m)
            if r: records.append(r)
            else: skipped += 1
        except Exception as e:
            log.warning(f"Skip {mkt.get('ticker','?')}: {e}"); skipped += 1

    records.sort(key=lambda r: r['entry_dt'])
    log.info(f"Qualified: {len(records)}, skipped {skipped} (of {len(markets)} total)")

    if not records:
        print("No qualifying trades"); return

    if args.sizing_mode == 'compare':
        allowance_records = [r.copy() for r in records]
        legacy_records    = [r.copy() for r in records]
        allowance_final, allowance_withdrawn, allowance_resets = simulate(
            allowance_records,
            'allowance',
            reset_trigger=args.reset_trigger,
            reset_to=args.reset_to,
        )
        legacy_final, legacy_withdrawn, legacy_resets = simulate(
            legacy_records,
            'legacy-kelly',
            reset_trigger=args.reset_trigger,
            reset_to=args.reset_to,
        )
        allowance_summary = summarize_run(
            markets, allowance_records, allowance_final, 'allowance',
            withdrawn_total=allowance_withdrawn, reset_count=allowance_resets,
        )
        legacy_summary    = summarize_run(
            markets, legacy_records, legacy_final, 'legacy-kelly',
            withdrawn_total=legacy_withdrawn, reset_count=legacy_resets,
        )
        print_compare_summary(allowance_summary, legacy_summary)
        return

    final, withdrawn_total, reset_count = simulate(
        records,
        args.sizing_mode,
        reset_trigger=args.reset_trigger,
        reset_to=args.reset_to,
    )
    summary    = summarize_run(
        markets,
        records,
        final,
        args.sizing_mode,
        withdrawn_total=withdrawn_total,
        reset_count=reset_count,
    )
    executed   = summary['executed']
    skipped_rm = summary['skipped_rm']
    wins       = summary['wins']
    losses     = summary['losses']
    strong     = summary['strong']
    strong_w   = summary['strong_w']
    skip_reasons = summary['skip_reasons']
    max_cash   = summary['max_cash']
    max_dd_trading = summary['max_dd_trading']
    max_dd_net = summary['max_dd_net']
    max_net_equity = summary['max_net_equity']
    max_ws     = summary['max_ws']
    max_ls     = summary['max_ls']
    wr         = summary['wr']
    pnl        = summary['pnl']
    ret_pct    = summary['ret_pct']
    avg_win    = summary['avg_win']
    avg_loss   = summary['avg_loss']
    gw         = summary['gw']
    gl         = summary['gl']
    net_pnl    = summary['net_pnl']
    net_equity = summary['net_equity']
    net_ret_pct = summary['net_ret_pct']
    withdrawn_total = summary['withdrawn_total']
    reset_count = summary['reset_count']
    period   = f"{records[0]['entry_dt'][:16]} → {records[-1]['entry_dt'][:16]} UTC"

    W = 108
    print("\n" + "=" * W)
    print(f"  DIRECTIONAL MODEL  ·  KXBTC15M  ·  {DAYS_BACK}-day  ·  ${STARTING_CASH:.0f} start  ·  {period}")
    price_gate_str = f"entry≤{MAX_ENTRY_PRICE_RM}¢ (market efficiency)" if MAX_ENTRY_PRICE_RM > 0 else "no price gate"
    print(f"  Filters: Markov gap≥{MARKOV_MIN_GAP} · 6-9min · ER≥{MIN_EFFICIENCY_RATIO:.2f} · vel<{VEL_SAFETY_RATIO:.0%}×cross · vol≤{MAX_VOL_MULT}×ref · {price_gate_str}")
    print("=" * W)
    if args.sizing_mode == 'legacy-kelly':
        print(f"  {'Sizing: legacy Kelly':<40} {'edge-scaled + price tier':>24}")
    else:
        print(f"  {f'Sizing: {int(KELLY_FRACTION*100)}% allowance per wager':<40} {f'bankroll × {KELLY_FRACTION:.2f}':>24}")
    print(f"  {'Windows total':<40} {len(markets):>8}")
    print(f"  {'Qualified (all gates pass)':<40} {len(records):>8}  ({len(records)/max(len(markets),1)*100:.1f}%)")
    print(f"  {'Executed by agent':<40} {len(executed):>8}")
    print(f"    {'↳ strong signal (enter★)':<38} {len(strong):>8}  WR {len(strong_w)/max(len(strong),1)*100:.1f}%")
    print(f"  {'Skipped by risk manager':<40} {len(skipped_rm):>8}")
    for k, v in sorted(skip_reasons.items(), key=lambda x: -x[1]):
        print(f"      ↳ {k:<36} {v:>8}")
    print(f"  {'-'*50}")
    print(f"  {'Win rate':<40} {wr:>7.1f}%")
    print(f"  {'Avg win per trade':<40} ${avg_win:>+7.2f}")
    print(f"  {'Avg loss per trade':<40} ${avg_loss:>+7.2f}")
    print(f"  {'Profit factor':<40} {gw/max(gl,0.01):>8.2f}×")
    print(f"  {'Longest win / loss streak':<40}   {max_ws:>3} / {max_ls}")
    print(f"  {'-'*50}")
    print(f"  {'Starting cash':<40} ${STARTING_CASH:>8.2f}")
    print(f"  {'Final cash':<40} ${final:>8.2f}")
    print(f"  {'Total P&L':<40} ${pnl:>+8.2f}")
    print(f"  {'Return':<40} {ret_pct:>+7.1f}%")
    print(f"  {'Withdrawn profits':<40} ${withdrawn_total:>8.2f}  ({reset_count} resets)")
    print(f"  {'Net equity (cash+withdrawn)':<40} ${net_equity:>8.2f}")
    print(f"  {'Net P&L':<40} ${net_pnl:>+8.2f}")
    print(f"  {'Net return':<40} {net_ret_pct:>+7.1f}%")
    print(f"  {'Max drawdown (trading bankroll)':<40} {max_dd_trading:>7.1f}%")
    print(f"  {'Max drawdown (net equity)':<40} {max_dd_net:>7.1f}%")
    print(f"  {'Peak trading bankroll':<40} ${max_cash:>8.2f}")
    print(f"  {'Peak net equity':<40} ${max_net_equity:>8.2f}")
    # ── Price bucket breakdown ────────────────────────────────────────────────
    buckets = [(0,65,'<65¢'),(65,73,'65-73¢'),(73,79,'73-79¢'),(79,85,'79-85¢'),(85,100,'85+¢')]
    print()
    print("  EDGE BY ENTRY PRICE  (break-even WR = entry price)")
    print(f"  {'Bucket':<10} {'Trades':>7} {'WR':>7} {'BE WR':>7} {'Edge':>7} {'Total PnL':>10}")
    print("  " + "-" * 55)
    for lo, hi, label in buckets:
        b_trades = [r for r in executed if lo <= r['limit_price_cents'] < hi]
        if not b_trades: continue
        b_wins   = [r for r in b_trades if r.get('outcome_sim', r['outcome']) == 'WIN']
        b_wr     = len(b_wins) / len(b_trades) * 100
        b_be_wr  = (lo + hi) / 2
        b_edge   = b_wr - b_be_wr
        b_pnl    = sum(r['pnl'] for r in b_trades)
        print(f"  {label:<10} {len(b_trades):>7} {b_wr:>6.1f}% {b_be_wr:>6.1f}% {b_edge:>+6.1f}pp ${b_pnl:>+8.2f}")
    print("=" * W)
    print()

    print("  TRADE LOG")
    hdr = (f"  {'#':<4} {'Entry (UTC)':<17} {'S':<3} {'LP¢':>4} {'Min':>4} "
            f"{'pYes':>6} {'Pers':>5} {'RSI':>5} {'ER':>5} "
           f"{'Spot':>9} {'Strike':>9} {'Dist%':>6} "
           f"{'Ctrs':>4} {'PnL':>8} {'Bal':>9}  Result")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    n = 0
    for r in records:
        if r.get('contracts', 0) == 0: continue
        n += 1
        oc    = r.get('outcome_sim', r['outcome'])
        sig   = "★" if r.get('enter_signal') else " "
        icon  = "✓ WIN" if oc == 'WIN' else "✗ LOSS"
        er_val = r.get('efficiency_ratio', r.get('hurst'))
        h_str = f"{er_val:.2f}" if er_val is not None else "  -  "
        r_str = f"{r['rsi']:.0f}" if r.get('rsi') is not None else "  -"
        print(f"  {n:<4} {r['entry_dt'][5:16].replace('T',' '):<17} {r['side']:<3} "
              f"{r['limit_price_cents']:>4} {r['minutes_left']:>4.1f} "
              f"{r['p_yes']:>6.3f} {r['persist']:>5.2f} {r_str:>5} {h_str:>5} "
              f"${r['spot']:>8,.0f} ${r['strike']:>8,.0f} {r['dist_pct']:>+6.3f} "
              f"{r['contracts']:>4} ${r['pnl']:>+7.2f} ${r['cash_after']:>8.2f}  "
              f"{sig}{icon}")
    print("=" * W)
    print()


if __name__ == '__main__':
    main()
