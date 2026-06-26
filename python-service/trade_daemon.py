"""
trade_daemon.py — Autonomous Kalshi BTC trading daemon

Runs 24/7. Wakes up for each 15-min KXBTC15M window, runs the full
Markov signal stack, places a real order if all gates pass, and logs
every decision to logs/daemon_YYYYMMDD.log.

Usage:
  source ~/.sentient-venv313/bin/activate
  python3 trade_daemon.py              # live trading
  python3 trade_daemon.py --dry-run    # simulate only (no real orders)
  python3 trade_daemon.py --bankroll 500
"""

import argparse, asyncio, base64, json, logging, math, os, sys, time, uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import httpx

sys.path.insert(0, os.path.dirname(__file__))
from run_backtest import (
    fetch_candles_5m, fetch_candles_15m,
    build_markov_history, build_transition_matrix, predict_from_momentum,
    price_change_to_state, gk_vol, compute_hurst,
    MARKOV_MIN_GAP, MIN_PERSIST, KELLY_FRACTION, MAX_TRADE_PCT,
    MIN_MINUTES_LEFT, MAX_MINUTES_LEFT,
    MAX_ENTRY_PRICE_RM, MAX_ENTRY_PRICE_YES, MAX_ENTRY_PRICE_NO,
    MAKER_FEE_RATE, EMPIRICAL_PRICE_BY_D, BLOCKED_UTC_HOURS,
    allowance_contracts,
)

# ── Env / config ────────────────────────────────────────────────────────────[...]
_env_path = Path(__file__).parent.parent / ".env.local"
if _env_path.exists():
    for line in _env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

KALSHI_HOST    = "https://api.elections.kalshi.com"
API_PREFIX     = "/trade-api/v2"
KALSHI_API_KEY = os.environ.get("KALSHI_API_KEY", "")
KALSHI_PEM     = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "./kalshi_private.pem")

MAX_DAILY_LOSS      = 50.0   # $ hard stop for the day
MAX_GIVEBACK_X      = 1.5    # stop if peak P&L drops by this × MAX_DAILY_LOSS
MAX_DAILY_TRADES    = 48
DAEMON_MAX_TRADE_PCT = 0.35
DAEMON_MAX_CONTRACTS = 50
ENTRY_WINDOW_MINUTES_LEFT = float(MIN_MINUTES_LEFT)
ENTRY_WINDOW_MAX_MINUTES_LEFT = float(MAX_MINUTES_LEFT)
TAKE_PROFIT_LIMIT_SELL_CENTS = 99

# ── Logging ─────────────────────────────────────────────────────────────[...]
_log_dir = Path(__file__).parent / "logs"
_log_dir.mkdir(exist_ok=True)

def _make_logger() -> logging.Logger:
    logger = logging.getLogger("daemon")
    # Avoid duplicate output when imported modules configure root logging.
    # Also make this setup idempotent if the module is reloaded.
    if getattr(logger, "_sentient_configured", False):
        return logger

    logger.setLevel(logging.INFO)
    logger.propagate = False
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S UTC")

    if logger.handlers:
        logger.handlers.clear()

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    fh = logging.FileHandler(_log_dir / f"daemon_{datetime.now().strftime('%Y%m%d')}.log")
    fh.setFormatter(fmt)
    logger.addHandler(sh)
    logger.addHandler(fh)
    logger._sentient_configured = True
    return logger

log = _make_logger()

# ── Kalshi v2 auth + HTTP ──────────────────────────────────────────────────────
def _sign_path(endpoint: str) -> str:
    """Full URL path for RSA signing — includes /trade-api/v2, no query string."""
    path = endpoint if endpoint.startswith("/") else f"/{endpoint}"
    if not path.startswith(API_PREFIX):
        path = API_PREFIX + path
    return path.split("?")[0]

def _api_url(endpoint: str) -> str:
    path = endpoint if endpoint.startswith("/") else f"/{endpoint}"
    if not path.startswith(API_PREFIX):
        path = API_PREFIX + path
    return f"{KALSHI_HOST}{path}"

def _load_private_key():
    from cryptography.hazmat.primitives import serialization
    pem_env = os.environ.get("KALSHI_PRIVATE_KEY", "")
    if pem_env:
        return serialization.load_pem_private_key(pem_env.replace("\\n", "\n").encode(), password=None)
    key_path = Path(KALSHI_PEM)
    if not key_path.is_absolute():
        key_path = Path(__file__).parent.parent / KALSHI_PEM
    return serialization.load_pem_private_key(key_path.read_bytes(), password=None)

def _build_headers(method: str, endpoint: str) -> dict:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding as _pad
    pk        = _load_private_key()
    ts        = str(int(time.time() * 1000))
    sign_path = _sign_path(endpoint)
    sig       = pk.sign(
        (ts + method.upper() + sign_path).encode(),
        _pad.PSS(mgf=_pad.MGF1(hashes.SHA256()), salt_length=_pad.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY":       KALSHI_API_KEY,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        "Accept":                  "application/json",
        "Content-Type":            "application/json",
    }

async def _kget(endpoint: str, params: Optional[dict] = None) -> dict:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(_api_url(endpoint), params=params or {}, headers=_build_headers("GET", endpoint))
        r.raise_for_status()
        return r.json()

async def _kpost(endpoint: str, body: dict) -> dict:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(_api_url(endpoint), json=body, headers=_build_headers("POST", endpoint))
        r.raise_for_status()
        return r.json()


async def _kdelete(endpoint: str) -> dict:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.delete(_api_url(endpoint), headers=_build_headers("DELETE", endpoint))
        r.raise_for_status()
        if not r.content:
            return {}
        try:
            return r.json()
        except Exception:
            return {}


def _norm_market(m: dict) -> dict:
    """Normalize v2 market fields that may come back as dollar strings."""
    for field, dollar_field in [
        ("yes_ask", "yes_ask_dollars"), ("yes_bid", "yes_bid_dollars"),
        ("no_ask",  "no_ask_dollars"),  ("no_bid",  "no_bid_dollars"),
    ]:
        if not m.get(field) and m.get(dollar_field) is not None:
            try:
                m[field] = round(float(m[dollar_field]) * 100)
            except (ValueError, TypeError):
                pass
    return m


def _v2_book(leg: str, action: str, price_cents: int) -> tuple[str, str]:
    """Map yes/no leg + buy/sell + cents → V2 bid/ask + dollar price on the YES book."""
    if leg == "yes":
        side = "bid" if action == "buy" else "ask"
        return side, f"{price_cents / 100:.4f}"
    comp = (100 - price_cents) / 100
    side = "ask" if action == "buy" else "bid"
    return side, f"{comp:.4f}"

# ── Timing helpers ─────────────────────────────────────────────────────────────
def _et_offset() -> int:
    now = datetime.now(timezone.utc)
    yr  = now.year
    mar1 = datetime(yr, 3, 1, tzinfo=timezone.utc)
    edt_start = mar1 + timedelta(days=(6 - mar1.weekday()) % 7 + 7)
    nov1 = datetime(yr, 11, 1, tzinfo=timezone.utc)
    est_start = nov1 + timedelta(days=(6 - nov1.weekday()) % 7)
    return -4 if edt_start <= now < est_start else -5

def next_window_close() -> datetime:
    """UTC time of the next KXBTC15M window close (15-min ET boundary)."""
    off = _et_offset()
    now = datetime.now(timezone.utc)
    et  = now + timedelta(hours=off)
    nxt = (et.minute // 15 + 1) * 15
    et  = et.replace(second=0, microsecond=0)
    if nxt >= 60:
        et = et.replace(minute=0) + timedelta(hours=1)
    else:
        et = et.replace(minute=nxt)
    return et - timedelta(hours=off)

def fmt(secs: float) -> str:
    m, s = divmod(int(abs(secs)), 60)
    return f"{m}m{s:02d}s"


def should_retry_window(mins_left: float, reasons: list[str]) -> bool:
    """Continue reevaluating the window while it is still in the active entry period."""
    # Keep checking until we reach the same late cutoff used by the main loop.
    # This avoids dropping windows too early (e.g., around 3.4 min left) now that
    # the configured entry range is 3-9 minutes.
    return mins_left > max(2.5, ENTRY_WINDOW_MINUTES_LEFT - 0.5)


def retry_delay_for_window(mins_left: float) -> float:
    """Poll more aggressively inside the active 3–9 minute entry window."""
    if mins_left <= 6.0:
        return 3.0
    if mins_left <= ENTRY_WINDOW_MAX_MINUTES_LEFT:
        return 4.0
    return 5.0

# ── Session state ───────────────────────────────────────────────────────────[...]
class Session:
    def __init__(self, bankroll: float):
        self.bankroll      = bankroll
        self.daily_pnl     = 0.0
        self.daily_trades  = 0
        self.peak_pnl      = 0.0
        self.traded        : set[str] = set()   # window IDs already handled
        self.pending       : dict     = {}       # window_id → trade info (awaiting settlement)
        self._date         = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def new_day_check(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._date:
            log.info(f"── New day {today} — resetting P&L counters ──")
            self.daily_pnl    = 0.0
            self.daily_trades = 0
            self.peak_pnl     = 0.0
            self._date        = today

    def limit_hit(self) -> Optional[str]:
        max_loss = max(50.0, min(150.0, self.bankroll * 0.05))
        if self.daily_pnl <= -max_loss:
            return f"daily loss limit (${self.daily_pnl:.2f} / -${max_loss:.0f})"
        giveback = self.peak_pnl - self.daily_pnl if self.peak_pnl > 0 else 0
        if giveback >= max_loss * MAX_GIVEBACK_X:
            return f"session giveback limit (${giveback:.2f} from peak ${self.peak_pnl:.2f})"
        if self.daily_trades >= MAX_DAILY_TRADES:
            return f"daily trade cap ({MAX_DAILY_TRADES})"
        return None

    def record(self, pnl: float):
        self.daily_pnl   += pnl
        self.daily_trades += 1
        self.bankroll     += pnl
        if self.daily_pnl > self.peak_pnl:
            self.peak_pnl = self.daily_pnl

# ── Market + signal ───────────────────────────────────────────────────────────[...]
async def fetch_market() -> Optional[dict]:
    months = ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"]
    off = _et_offset()
    for delta in [0, 15, 30]:
        ts  = time.time() + delta * 60
        dt  = datetime.fromtimestamp(ts, tz=timezone.utc)
        et  = (dt + timedelta(hours=off)).replace(second=0, microsecond=0)
        nxt = (et.minute // 15 + 1) * 15
        if nxt >= 60:
            et = et.replace(minute=0) + timedelta(hours=1)
        else:
            et = et.replace(minute=nxt)
        event = f"KXBTC15M-{et.strftime('%y')}{months[et.month-1]}{et.strftime('%d%H%M')}"
        try:
            data = await _kget("/markets", {"event_ticker": event, "status": "open"})
            for m in data.get("markets", []):
                m = _norm_market(m)
                if m.get("yes_ask", 0) > 0:
                    return m
        except Exception:
            pass

    # Fallback: grab first open market in series
    try:
        data = await _kget("/markets", {"series_ticker": "KXBTC15M", "status": "open", "limit": 5})
        active = [m for m in (_norm_market(x) for x in data.get("markets", [])) if m.get("yes_ask", 0) > 0]
        if active:
            return active[0]
    except Exception:
        pass
    return None


async def get_btc_price() -> float:
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get("https://api.exchange.coinbase.com/products/BTC-USD/ticker")
            return float(r.json().get("price", 0))
    except Exception:
        return 0.0


async def run_signal(market: dict, bankroll: float, allowance_fraction: float) -> dict:
    strike   = float(market.get("floor_strike") or 0)
    yes_ask  = int(market.get("yes_ask") or 50)
    no_ask   = int(market.get("no_ask")  or 50)
    ticker   = market.get("ticker", "")
    close_ts = 0
    if market.get("close_time"):
        try:
            close_ts = datetime.fromisoformat(
                market["close_time"].replace("Z", "+00:00")
            ).timestamp()
        except Exception:
            pass

    btc_price    = await get_btc_price()
    minutes_left = max(0, (close_ts - time.time()) / 60) if close_ts else 7.5
    dist_pct     = (btc_price - strike) / strike * 100 if strike > 0 else 0.0

    # Candles (sync — run in executor to avoid blocking event loop)
    loop = asyncio.get_event_loop()
    candles_5m, candles_15m = await asyncio.gather(
        loop.run_in_executor(None, fetch_candles_5m,  2),
        loop.run_in_executor(None, fetch_candles_15m, 2),
    )

    check_ts = time.time()

    # GK vol + Hurst
    ctx15   = [c for c in candles_15m if c[0] + 900 <= check_ts]
    last15  = list(reversed(ctx15[-32:])) if len(ctx15) >= 12 else []
    gk      = gk_vol(last15[:16])    if last15 else None
    hurst   = compute_hurst(last15[:24]) if last15 else None

    # d-score
    d_score = None
    if gk and gk > 0 and strike > 0:
        try:
            candles_left = max(minutes_left / 15.0, 1/60)
            d_score = math.log(btc_price / strike) / (gk * math.sqrt(candles_left))
        except Exception:
            pass

    # Markov
    history       = build_markov_history(candles_5m, check_ts)
    c5_by_ts      = {c[0]: c for c in candles_5m}
    c5_ts         = int(check_ts // 300) * 300 - 300
    c5_bar        = c5_by_ts.get(c5_ts) or c5_by_ts.get(c5_ts - 300)
    c5_prev       = c5_by_ts.get(c5_ts - 300)
    current_state = 4
    if c5_bar and c5_prev and c5_prev[4] > 0:
        current_state = price_change_to_state(
            (c5_bar[4] - c5_prev[4]) / c5_prev[4] * 100.0
        )

    full_history = (history + [current_state]) if history else [current_state, current_state]
    P            = build_transition_matrix(full_history)
    forecast     = predict_from_momentum(P, current_state, minutes_left, dist_pct)
    p_yes        = forecast["p_yes"]
    gap          = abs(p_yes - 0.5)
    persist      = forecast["persist"]
    has_history  = len(full_history) >= 20

    # Gates
    utc_hour  = datetime.now(timezone.utc).hour
    blocked   = utc_hour in BLOCKED_UTC_HOURS
    vol_ok    = gk is None or gk <= 0.002 * 1.5  # Allow some extra room for volatility spikes (e.g., 0.0035) without rejecting the trade.
    hurst_ok  = hurst is None or hurst >= 0.45
    markov_ok = has_history and gap >= MARKOV_MIN_GAP and persist >= MIN_PERSIST
    # Informational tag only; entry timing now always uses ENTRY_WINDOW_*.
    is_golden = 65 <= yes_ask <= 73
    time_ok   = ENTRY_WINDOW_MINUTES_LEFT <= minutes_left <= ENTRY_WINDOW_MAX_MINUTES_LEFT
    side_is_yes = p_yes > 0.5
    limit_price = round(yes_ask if side_is_yes else no_ask)
    price_cap   = MAX_ENTRY_PRICE_YES if side_is_yes else MAX_ENTRY_PRICE_NO
    price_ok    = limit_price <= price_cap
    dist_ok   = abs(dist_pct) >= 0.02

    reasons: list[str] = []
    if not has_history:  reasons.append(f"building history ({len(full_history)}/20 candles)")
    if not markov_ok:    reasons.append(f"Markov gap {gap:.3f}<{MARKOV_MIN_GAP} or persist {persist:.2f}<{MIN_PERSIST}")
    if blocked:          reasons.append(f"blocked UTC hour {utc_hour}:00")
    if not vol_ok:       reasons.append(f"high vol (GK={gk:.5f})")
    if not hurst_ok:     reasons.append(f"mean-reverting (Hurst={hurst:.2f})")
    if not time_ok:      reasons.append(f"timing {minutes_left:.1f}min outside {ENTRY_WINDOW_MINUTES_LEFT:.0f}-{ENTRY_WINDOW_MAX_MINUTES_LEFT:.0f}min window")
    if not price_ok:     reasons.append(f"price {limit_price}¢ > {'YES' if side_is_yes else 'NO'} cap {price_cap}¢")
    if not dist_ok:      reasons.append(f"near-strike noise ({dist_pct:.4f}%)")

    all_ok = markov_ok and not blocked and vol_ok and hurst_ok and time_ok and price_ok and dist_ok
    rec    = ("YES" if p_yes > 0.5 else "NO") if all_ok else "NO_TRADE"

    # UI-aligned allowance sizing
    p_win     = p_yes if rec == "YES" else (1 - p_yes)
    p_d       = limit_price / 100
    fee_c     = MAKER_FEE_RATE * p_d * (1 - p_d)
    net_win   = (1 - p_d) - fee_c
    cost_c    = p_d + fee_c
    allowance, contracts = allowance_contracts(
        bankroll, cost_c, allowance_fraction=allowance_fraction, max_contracts=DAEMON_MAX_CONTRACTS,
    ) if rec != "NO_TRADE" else (max(1.0, bankroll * allowance_fraction), 0)
    max_loss  = round(cost_c * contracts, 2)
    ev        = round(contracts * (net_win * p_win - cost_c * (1 - p_win)), 2)

    return {
        "approved":        rec != "NO_TRADE",
        "recommendation":  rec,
        "ticker":          ticker,
        "limit_price":     limit_price,
        "allowance_pct":   round(allowance_fraction * 100, 2),
        "allowance_usd":   round(allowance, 2),
        "contracts":       contracts,
        "max_loss_usd":    max_loss,
        "expected_value":  ev,
        "rejection_reasons": reasons,
        "signal": {
            "p_yes":        round(p_yes, 4),
            "gap":          round(gap, 4),
            "persist":      round(persist, 3),
            "hurst":        round(hurst, 3) if hurst else None,
            "gk_vol":       round(gk, 6) if gk else None,
            "d_score":      round(d_score, 3) if d_score else None,
            "minutes_left": round(minutes_left, 1),
            "history_len":  len(full_history),
            "utc_hour":     utc_hour,
            "is_golden":    is_golden,
        },
        "market": {
            "btc_price": round(btc_price, 2),
            "strike":    strike,
            "dist_pct":  round(dist_pct, 4),
            "yes_ask":   yes_ask,
            "no_ask":    no_ask,
        },
    }


def _normalize_order_status(payload: dict) -> tuple[str, int, int]:
    """Normalize Kalshi order payloads into a simple status/fill summary."""
    data = payload or {}
    if isinstance(data.get("order"), dict):
        data = data["order"]
    elif isinstance(data.get("orders"), list) and data["orders"]:
        data = data["orders"][0]

    raw_status = str(data.get("status") or data.get("order_status") or "").strip().lower()
    if raw_status in {"filled", "complete", "completed", "executed", "execution_complete"}:
        status = "filled"
    elif raw_status in {"partially_filled", "partial", "partially-filled", "partially filled"}:
        status = "partially_filled"
    elif raw_status in {"canceled", "cancelled", "expired", "rejected"}:
        status = "canceled"
    elif raw_status in {"resting", "open", "pending", "new", "accepted", "submitted", "queued"}:
        status = "resting"
    else:
        status = raw_status or "unknown"

    def _to_int(value) -> int:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0

    filled_count = _to_int(
        data.get("filled_count")
        if data.get("filled_count") is not None else
        data.get("filled_quantity")
        if data.get("filled_quantity") is not None else
        data.get("filled")
        if data.get("filled") is not None else
        data.get("executed_count")
        if data.get("executed_count") is not None else
        data.get("matched_count")
        if data.get("matched_count") is not None else
        data.get("count_filled")
    )
    remaining_count = _to_int(
        data.get("remaining_count")
        if data.get("remaining_count") is not None else
        data.get("remaining_quantity")
        if data.get("remaining_quantity") is not None else
        data.get("remaining")
        if data.get("remaining") is not None else
        data.get("count_remaining")
    )

    # Some Kalshi payloads mark terminal execution via status/remaining only,
    # while leaving filled_count unset. Infer from known order size fields.
    total_count = _to_int(
        data.get("count")
        if data.get("count") is not None else
        data.get("order_count")
        if data.get("order_count") is not None else
        data.get("quantity")
        if data.get("quantity") is not None else
        data.get("initial_count")
        if data.get("initial_count") is not None else
        data.get("initial_quantity")
        if data.get("initial_quantity") is not None else
        data.get("original_count")
        if data.get("original_count") is not None else
        data.get("requested_count")
    )

    if status == "filled" and remaining_count <= 0:
        remaining_count = 0
        if filled_count <= 0 and total_count > 0:
            filled_count = total_count

    if status == "partially_filled" and filled_count <= 0 and total_count > 0 and remaining_count >= 0:
        inferred = total_count - remaining_count
        if inferred > 0:
            filled_count = inferred

    return status, filled_count, remaining_count


def _effective_filled_contracts(order_status: Optional[dict], requested_contracts: int) -> int:
    """Infer how many contracts were filled from status + counts.

    Kalshi can occasionally report terminal filled states with remaining=0 while
    leaving filled_count unset; in that case treat it as fully filled.
    """
    if not order_status:
        return 0

    status = str(order_status.get("status") or "").strip().lower()

    try:
        filled = int(order_status.get("filled_count") or 0)
    except (TypeError, ValueError):
        filled = 0

    try:
        remaining = int(order_status.get("remaining_count") or 0)
    except (TypeError, ValueError):
        remaining = 0

    requested_contracts = max(0, int(requested_contracts or 0))

    # Trust explicit fill quantity regardless of label (some payloads use odd status names).
    if filled > 0:
        return min(filled, requested_contracts) if requested_contracts > 0 else filled

    if status == "filled":
        if remaining <= 0 and requested_contracts > 0:
            return requested_contracts

    if status == "partially_filled":
        if filled > 0:
            return min(filled, requested_contracts) if requested_contracts > 0 else filled
        if requested_contracts > 0 and remaining >= 0:
            inferred = requested_contracts - remaining
            if inferred > 0:
                return inferred

    return 0


async def verify_order_status(order_id: str) -> dict:
    """Query Kalshi for the latest status of a placed order and normalize it."""
    try:
        payload = await _kget(f"/portfolio/orders/{quote(order_id, safe='')}")
    except Exception as exc:
        try:
            payload = await _kget("/portfolio/orders", {"limit": 20})
            if isinstance(payload, dict) and isinstance(payload.get("orders"), list):
                for item in payload["orders"]:
                    if str(item.get("order_id") or item.get("id") or "") == str(order_id):
                        payload = item
                        break
                else:
                    payload = {}
            else:
                payload = {}
        except Exception as fallback_exc:
            return {
                "order_id": order_id,
                "status": "unknown",
                "filled_count": 0,
                "remaining_count": 0,
                "raw": {},
                "error": f"{exc}; {fallback_exc}",
            }

    if isinstance(payload, dict) and isinstance(payload.get("orders"), list):
        for item in payload["orders"]:
            if str(item.get("order_id") or item.get("id") or "") == str(order_id):
                payload = item
                break
    if isinstance(payload, dict) and isinstance(payload.get("order"), dict):
        payload = payload["order"]

    status, filled_count, remaining_count = _normalize_order_status(payload)
    return {
        "order_id": order_id,
        "status": status,
        "filled_count": filled_count,
        "remaining_count": remaining_count,
        "raw": payload,
    }


async def place_limit_sell_order(ticker: str, side: str, contracts: int) -> dict:
    """Place a resting 99-cent GTC sell order to take profit on filled contracts."""
    if contracts <= 0:
        return {"ok": False, "error": "contracts must be > 0"}

    expected_sell_side, _ = _v2_book(side, "sell", TAKE_PROFIT_LIMIT_SELL_CENTS)

    # Cancel older resting sell orders on the same market/leg to avoid duplicate ladders.
    canceled_ids: list[str] = []
    cancel_errors: list[str] = []
    try:
        payload = await _kget("/portfolio/orders", {"limit": 200})
        orders = payload.get("orders", []) if isinstance(payload, dict) else []
        for order in orders:
            if not isinstance(order, dict):
                continue
            if str(order.get("ticker") or "") != ticker:
                continue

            status = str(order.get("status") or order.get("order_status") or "").strip().lower()
            if status not in {"resting", "open", "pending", "new", "accepted", "submitted", "queued"}:
                continue

            action = str(order.get("action") or "").strip().lower()
            if action and action != "sell":
                continue

            existing_side = str(order.get("side") or "").strip().lower()
            if existing_side and existing_side != expected_sell_side:
                continue

            order_id = str(order.get("order_id") or order.get("id") or "").strip()
            if not order_id:
                continue

            try:
                await _kdelete(f"/portfolio/events/orders/{quote(order_id, safe='')}")
                canceled_ids.append(order_id)
            except Exception as cancel_exc:
                cancel_errors.append(f"{order_id}: {cancel_exc}")
    except Exception as scan_exc:
        cancel_errors.append(f"scan: {scan_exc}")

    v2_side, price = _v2_book(side, "sell", TAKE_PROFIT_LIMIT_SELL_CENTS)
    body = {
        "ticker":                     ticker,
        "client_order_id":            str(uuid.uuid4()),
        "side":                       v2_side,
        "count":                      f"{contracts:.2f}",
        "price":                      price,
        "time_in_force":              "good_till_canceled",
        "self_trade_prevention_type": "taker_at_cross",
    }

    try:
        result = await _kpost("/portfolio/events/orders", body)
        return {
            "ok": True,
            "order_id": result.get("order_id") or "unknown",
            "canceled_order_ids": canceled_ids,
            "cancel_errors": cancel_errors,
            "raw": result,
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "canceled_order_ids": canceled_ids,
            "cancel_errors": cancel_errors,
        }


async def get_live_bankroll() -> Optional[float]:
    try:
        data = await _kget("/portfolio/balance")
        # v2: top-level balance + portfolio_value in cents
        if isinstance(data.get("balance"), (int, float)):
            return (data.get("balance", 0) + data.get("portfolio_value", 0)) / 100
        # legacy nested format
        bal = data.get("balance", {})
        if isinstance(bal, dict):
            return (bal.get("available_balance_cents", 0) + bal.get("portfolio_value_cents", 0)) / 100
    except Exception as e:
        log.warning(f"Could not fetch live balance: {e}")
    return None


async def check_settlement(ticker: str) -> Optional[str]:
    """Returns 'YES', 'NO', or None if not settled yet."""
    try:
        data = await _kget(f"/markets/{quote(ticker, safe='')}")
        m = _norm_market(data.get("market", data))
        result = m.get("result")
        if result in ("yes", "no"):
            return result.upper()
    except Exception:
        pass
    return None


# ── Main loop ─────────────────────────────────────────────────────────────[...]
async def main_loop(dry_run: bool, bankroll: float, allowance_fraction: float):
    session = Session(bankroll)
    log.info("=" * 65)
    log.info(f"  Sentient Trading Daemon  |  dry_run={dry_run}  |  bankroll=${bankroll:.0f}  |  allowance={allowance_fraction*100:.0f}%")
    log.info("=" * 65)

    # Try fetching live balance at startup
    live_bal = await get_live_bankroll()
    if live_bal and live_bal > 0:
        session.bankroll = live_bal
        log.info(f"Live balance: ${live_bal:.2f}")

    last_balance_refresh = time.time()

    while True:
        session.new_day_check()

        # Check pending settlements first
        settled_windows = []
        for wid, trade in list(session.pending.items()):
            result = await check_settlement(trade["ticker"])
            if result is not None:
                won    = (result == trade["side"].upper())
                pnl    = trade["net_win"] if won else -trade["cost"]
                emoji  = "WIN +" if won else "LOSS -"
                log.info(
                    f"SETTLED {emoji}${abs(pnl):.2f} | {trade['ticker']} | "
                    f"BUY {trade['side']} @ {trade['limit_price']}¢ | "
                    f"Result={result} | Daily P&L: ${session.daily_pnl + pnl:+.2f}"
                )
                session.record(pnl)
                settled_windows.append(wid)
        for wid in settled_windows:
            del session.pending[wid]

        # Refresh bankroll every 30 min
        if time.time() - last_balance_refresh > 1800:
            live_bal = await get_live_bankroll()
            if live_bal and live_bal > 0:
                session.bankroll = live_bal
                log.info(f"Balance refresh: ${live_bal:.2f}")
            last_balance_refresh = time.time()

        # Session limit check
        stop = session.limit_hit()
        if stop:
            log.warning(f"SESSION PAUSED — {stop}  (daily P&L: ${session.daily_pnl:+.2f})")
            await asyncio.sleep(300)
            continue

        # Timing
        close_dt  = next_window_close()
        now       = datetime.now(timezone.utc)
        mins_left = (close_dt - now).total_seconds() / 60
        window_id = close_dt.strftime("%Y%m%d%H%M")

        # Sleep until entry window opens.
        if mins_left > ENTRY_WINDOW_MAX_MINUTES_LEFT + 0.5:
            sleep_s = (mins_left - ENTRY_WINDOW_MAX_MINUTES_LEFT) * 60
            log.info(
                f"Next window {window_id} closes in {mins_left:.1f} min "
                f"— sleeping {fmt(sleep_s)}"
            )
            await asyncio.sleep(sleep_s)
            continue

        # Window is expiring — skip it
        if mins_left < 2.5:
            skip_s = (mins_left + 1.5) * 60
            if window_id not in session.traded:
                log.info(f"Window {window_id} closing ({mins_left:.1f} min) — skip")
                session.traded.add(window_id)
            await asyncio.sleep(skip_s)
            continue

        # Already traded this window — sleep briefly and loop back
        if window_id in session.traded:
            await asyncio.sleep(5)
            continue

        # ── Active window: fetch market + signal ──────────────────────────────
        log.info(f"Window {window_id} | {mins_left:.1f} min to close — running signal...")

        try:
            market = await fetch_market()
        except Exception as e:
            log.error(f"Market fetch error: {e}")
            await asyncio.sleep(30)
            continue

        if not market:
            log.warning("No active market found — retrying in 30s")
            await asyncio.sleep(30)
            continue

        try:
            signal = await run_signal(market, session.bankroll, allowance_fraction)
        except Exception as e:
            log.error(f"Signal error: {e}", exc_info=True)
            await asyncio.sleep(30)
            continue

        approved    = signal["approved"]
        rec         = signal["recommendation"]
        contracts   = signal["contracts"]
        limit_price = signal["limit_price"]
        ticker      = signal["ticker"]
        reasons     = signal["rejection_reasons"]
        sig         = signal["signal"]
        mkt         = signal["market"]

        # Status line
        log.info(
            f"BTC ${mkt['btc_price']:,.0f} | Strike ${mkt['strike']:,.0f} | "
            f"Δ{mkt['dist_pct']:+.3f}% | {sig['minutes_left']:.1f}min | "
            f"p(YES)={sig['p_yes']:.1%} gap={sig['gap']:.3f} persist={sig['persist']:.2f} "
            f"{'[YES65-73]' if sig['is_golden'] else ''}"
        )

        if not approved:
            reason_str = " | ".join(reasons) if reasons else "no edge"
            log.info(f"NO TRADE — {reason_str}")

            if should_retry_window(mins_left, reasons):
                delay_s = retry_delay_for_window(mins_left)
                log.info(f"No trade — re-evaluating in {delay_s:.0f}s...")
                await asyncio.sleep(delay_s)
            else:
                session.traded.add(window_id)
                wait_s = max(5, (mins_left + 0.75) * 60)
                log.info(f"Marked window as traded — waiting {fmt(wait_s)} for close...")
                await asyncio.sleep(wait_s)
            continue

        # ── TRADE ───────────────────────────────────────────────────────────[...]
        p_d      = limit_price / 100
        fee_c    = MAKER_FEE_RATE * p_d * (1 - p_d)
        cost_per = p_d + fee_c
        net_win  = (1 - p_d) - fee_c

        log.info(
            f"{'[DRY RUN] ' if dry_run else ''}TRADE  BUY {rec} {contracts}c @ {limit_price}¢  "
            f"allowance={signal['allowance_pct']:.0f}% (${signal['allowance_usd']:.2f})  "
            f"max_loss=${signal['max_loss_usd']:.2f}  EV=${signal['expected_value']:+.2f}  "
            f"ticker={ticker}"
        )

        order_status = None
        if dry_run:
            order_id = "dry-run"
            log.info(f"[DRY RUN] Order simulated — no real order sent")
        else:
            try:
                leg          = rec.lower()
                v2_side, price = _v2_book(leg, "buy", limit_price)
                body = {
                    "ticker":                     ticker,
                    "client_order_id":            str(uuid.uuid4()),
                    "side":                       v2_side,
                    "count":                      f"{contracts:.2f}",
                    "price":                      price,
                    "time_in_force":              "immediate_or_cancel",
                    "self_trade_prevention_type": "taker_at_cross",
                }
                result   = await _kpost("/portfolio/events/orders", body)
                order_id = result.get("order_id") or "unknown"
                log.info(f"ORDER PLACED — id={order_id}")

                # Verify the order status immediately so we only track it if it actually filled.
                order_status = None
                for _ in range(3):
                    order_status = await verify_order_status(order_id)
                    if _effective_filled_contracts(order_status, contracts) > 0:
                        break
                    await asyncio.sleep(2)

                if order_status:
                    status = order_status["status"]
                    filled_count = order_status["filled_count"]
                    remaining_count = order_status["remaining_count"]
                    inferred_filled = _effective_filled_contracts(order_status, contracts)
                    log.info(
                        f"ORDER STATUS — id={order_id} status={status} "
                        f"filled={filled_count}/{contracts} inferred_filled={inferred_filled}/{contracts} "
                        f"remaining={remaining_count}"
                    )
            except httpx.HTTPStatusError as e:
                log.error(f"Kalshi API error {e.response.status_code}: {e.response.text}")
                session.traded.add(window_id)
                await asyncio.sleep(max(5, (mins_left + 0.5) * 60))
                continue
            except Exception as e:
                log.error(f"Order failed: {e}")
                session.traded.add(window_id)
                await asyncio.sleep(max(5, (mins_left + 0.5) * 60))
                continue

        # If IOC did not fill, keep the same window alive and retry while time remains.
        if not dry_run:
            filled_contracts_now = _effective_filled_contracts(order_status, contracts)
            filled_now = filled_contracts_now > 0
            if not filled_now:
                status = order_status["status"] if order_status else "unknown"
                fill_info = f"status={status}"
                if order_status:
                    inferred_filled = _effective_filled_contracts(order_status, contracts)
                    fill_info = (
                        f"status={status} filled={order_status['filled_count']}/{contracts} "
                        f"inferred_filled={inferred_filled}/{contracts} "
                        f"remaining={order_status['remaining_count']}"
                    )
                mins_left_now = max(0.0, (close_dt - datetime.now(timezone.utc)).total_seconds() / 60)
                log.warning(f"Order {order_id} not filled ({fill_info})")

                # Safety first: avoid duplicate exposure when IOC comes back as terminal-canceled
                # with zero remaining but without a trustworthy fill count.
                if order_status and status == "canceled" and order_status.get("remaining_count", 0) == 0:
                    log.warning(
                        "Ambiguous terminal cancel (remaining=0) — rechecking order status before retry"
                    )

                    # Kalshi can briefly lag on fill fields; do a short recheck cycle before deciding.
                    for _ in range(3):
                        await asyncio.sleep(2)
                        latest_status = await verify_order_status(order_id)
                        if not latest_status:
                            continue
                        order_status = latest_status
                        if _effective_filled_contracts(order_status, contracts) > 0:
                            filled_now = True
                            log.info(
                                f"ORDER STATUS RECHECK — id={order_id} status={order_status['status']} "
                                f"filled={order_status['filled_count']}/{contracts} "
                                f"inferred_filled={_effective_filled_contracts(order_status, contracts)}/{contracts} "
                                f"remaining={order_status['remaining_count']}"
                            )
                            break

                if not filled_now:
                    if should_retry_window(mins_left_now, [fill_info]):
                        delay_s = retry_delay_for_window(mins_left_now)
                        log.info(f"Unfilled order — re-evaluating window in {delay_s:.0f}s...")
                        await asyncio.sleep(delay_s)
                    else:
                        session.traded.add(window_id)
                        wait_s = max(5, (mins_left_now + 0.75) * 60)
                        log.info(f"Window nearly closed after unfilled order — waiting {fmt(wait_s)} for close...")
                        await asyncio.sleep(wait_s)
                    continue

        session.traded.add(window_id)

        # Queue for settlement check only when the order actually filled.
        if not dry_run:
            filled_contracts = _effective_filled_contracts(order_status, contracts)
        else:
            filled_contracts = 0

        if not dry_run and filled_contracts > 0:

            # Mirror web UI behavior: rest a 99-cent take-profit order for filled contracts.
            sell_res = await place_limit_sell_order(ticker=ticker, side=rec.lower(), contracts=filled_contracts)
            if sell_res.get("ok"):
                canceled_cnt = len(sell_res.get("canceled_order_ids") or [])
                log.info(
                    f"LIMIT-SELL PLACED — id={sell_res.get('order_id')} side={rec} "
                    f"count={filled_contracts} @ {TAKE_PROFIT_LIMIT_SELL_CENTS}¢ GTC"
                )
                if canceled_cnt:
                    log.info(f"LIMIT-SELL REPLACED — canceled {canceled_cnt} prior resting sell order(s) on {ticker}")
            else:
                log.warning(
                    f"LIMIT-SELL FAILED — side={rec} count={filled_contracts} "
                    f"@ {TAKE_PROFIT_LIMIT_SELL_CENTS}¢ GTC | {sell_res.get('error', 'unknown error')}"
                )
            if sell_res.get("cancel_errors"):
                log.warning(f"LIMIT-SELL CANCEL WARNINGS — {' | '.join(sell_res.get('cancel_errors'))}")

            session.pending[window_id] = {
                "ticker":      ticker,
                "side":        rec,
                "contracts":   filled_contracts,
                "limit_price": limit_price,
                "cost":        round(cost_per * filled_contracts, 2),
                "net_win":     round(net_win  * filled_contracts, 2),
                "order_id":    order_id,
                "order_status": order_status["status"],
            }
        elif not dry_run:
            log.warning(f"Order {order_id} was not confirmed as filled — skipping settlement tracking")

        # Sleep until window closes + 45s buffer for settlement
        wait_s = max(5, (mins_left + 0.75) * 60)
        log.info(f"Waiting {fmt(wait_s)} for window to settle...")
        await asyncio.sleep(wait_s)


# ── Entry ───────────────────────────────────────────────────────────────[...]
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sentient autonomous trading daemon")
    parser.add_argument("--dry-run",   action="store_true", help="Simulate trades, no real orders")
    parser.add_argument("--bankroll",  type=float, default=200.0, help="Starting bankroll in USD")
    parser.add_argument("--allowance-pct", type=float, default=20.0,
                        help="Percent of bankroll allocated per wager (default: 20)")
    args = parser.parse_args()

    try:
        asyncio.run(main_loop(
            dry_run=args.dry_run,
            bankroll=args.bankroll,
            allowance_fraction=args.allowance_pct / 100.0,
        ))
    except KeyboardInterrupt:
        log.info("Daemon stopped by user.")
