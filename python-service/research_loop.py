"""
research_loop.py — Nightly self-evolution engine

1. Fetches 30-day market + candle data (once, cached for all runs)
2. Runs baseline backtest with current params
3. Ablation study: varies one param at a time, finds best improvement
4. Parses daemon + webUI trade logs for live performance data
5. Calls Claude API — analyzes results, writes a research report
6. If any variation beats baseline by > 5%, creates a git branch
   with the proposed param change ready to review & merge

Usage:
  source ~/.sentient-venv313/bin/activate
  python3 research_loop.py              # full run
  python3 research_loop.py --no-claude  # skip API call, just backtest grid
  python3 research_loop.py --days 14    # shorten backtest window
"""

import argparse, copy, csv, json, math, os, re, subprocess, sys, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

# ── Env ────────────────────────────────────────────────────────────────────────
_env_path = Path(__file__).parent.parent / ".env.local"
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

sys.path.insert(0, os.path.dirname(__file__))
import run_backtest as _bt

# ── Param grid ─────────────────────────────────────────────────────────────────
# Each entry: (param_name, list_of_values_to_try, human_label)
# "current" marks the baseline value so we can skip it cleanly.
PARAM_GRID = [
    ("MARKOV_MIN_GAP",    [0.08, 0.09, 0.10, 0.11, 0.13, 0.15],  "Markov min gap"),
    ("MIN_PERSIST",       [0.78, 0.80, 0.82, 0.85, 0.87],         "Min persist"),
    ("MAX_ENTRY_PRICE_YES", [68, 70, 71, 72, 73, 74],              "Max YES entry price (¢)"),
    ("MAX_ENTRY_PRICE_NO",  [58, 60, 62, 65, 68, 70],              "Max NO entry price (¢)"),
    ("MIN_MINUTES_LEFT",  [3,    4,    5,    6,    7],              "Min minutes left"),
    ("MAX_MINUTES_LEFT",  [8,    9,    10,   11,   12],             "Max minutes left"),
    ("MAX_VOL_MULT",      [1.10, 1.15, 1.25, 1.35, 1.50],         "Max vol multiplier"),
    ("MIN_HURST",         [0.45, 0.48, 0.50, 0.52, 0.55],         "Min Hurst exponent"),
]

# Score = total_return_pct × win_rate_pct / max(max_drawdown_pct, 1)
def score(stats: dict) -> float:
    ret = stats.get("total_return_pct", 0)
    wr  = stats.get("win_rate_pct", 0)
    dd  = max(stats.get("max_drawdown_pct", 1), 1)
    n   = stats.get("total_trades", 0)
    if n < 10:
        return -9999  # not enough trades to be meaningful
    return ret * wr / dd


# ── Backtest runner ────────────────────────────────────────────────────────────
def _run_with_params(markets, c15, c5, overrides: dict, days: int) -> dict:
    """Run simulate() on pre-fetched data with temporary param overrides."""
    original = {}
    for k, v in overrides.items():
        original[k] = getattr(_bt, k)
        setattr(_bt, k, v)
    try:
        records = []
        for mkt in markets:
            r = _bt.process_market(mkt, c15, c5)
            if r:
                records.append(r)
        records.sort(key=lambda r: r["entry_dt"])
        final_cash = _bt.simulate(records, _bt.SIZING_MODE)
        executed   = [r for r in records if r.get("contracts", 0) > 0]
        wins       = [r for r in executed if r.get("outcome_sim", r["outcome"]) == "WIN"]
        losses     = [r for r in executed if r.get("outcome_sim", r["outcome"]) == "LOSS"]
        gw = sum(r["pnl"] for r in wins)
        gl = abs(sum(r["pnl"] for r in losses))
        peak = _bt.STARTING_CASH; max_dd = 0.0
        for r in executed:
            peak   = max(peak, r["cash_after"])
            max_dd = max(max_dd, (peak - r["cash_after"]) / peak * 100)
        buckets = {}
        for lo, hi, label in [(0,65,"<65¢"),(65,73,"65-73¢"),(73,79,"73-79¢"),(79,85,"79-85¢")]:
            bt_ = [r for r in executed if lo <= r["limit_price_cents"] < hi]
            bw  = [r for r in bt_ if r.get("outcome_sim", r["outcome"]) == "WIN"]
            if bt_:
                buckets[label] = {
                    "trades": len(bt_),
                    "wr":     round(len(bw)/len(bt_)*100, 1),
                    "pnl":    round(sum(r["pnl"] for r in bt_), 2),
                }
        time_buckets = {}
        # Keep windows adjacent and non-overlapping: [6,9) and [3,6)
        for lo, hi, label in [(6, 9, "9-6m"), (3, 6, "6-3m")]:
            bt_ = [r for r in executed if lo <= (r.get("minutes_left") or 0) < hi]
            bw  = [r for r in bt_ if r.get("outcome_sim", r["outcome"]) == "WIN"]
            if bt_:
                time_buckets[label] = {
                    "trades": len(bt_),
                    "wr":     round(len(bw)/len(bt_)*100, 1),
                    "pnl":    round(sum(r["pnl"] for r in bt_), 2),
                }
        return {
            "days_back":         days,
            "sizing_mode":       _bt.SIZING_MODE,
            "allowance_pct":     round(_bt.KELLY_FRACTION * 100, 2),
            "starting_cash":     _bt.STARTING_CASH,
            "final_cash":        round(final_cash, 2),
            "total_return_pct":  round((final_cash / _bt.STARTING_CASH - 1) * 100, 1),
            "win_rate_pct":      round(len(wins) / max(len(executed), 1) * 100, 1),
            "profit_factor":     round(gw / max(gl, 0.01), 2),
            "max_drawdown_pct":  round(max_dd, 1),
            "total_trades":      len(executed),
            "total_wins":        len(wins),
            "total_losses":      len(losses),
            "price_buckets":     buckets,
            "time_buckets":      time_buckets,
            "params":            dict(overrides),
        }
    finally:
        for k, v in original.items():
            setattr(_bt, k, v)


# ── Log parser ─────────────────────────────────────────────────────────────────
def parse_daemon_logs(days: int = 7) -> dict:
    """Parse live trade logs (daemon + webUI CSV) into summary stats."""
    log_dir   = Path(__file__).parent / "logs"
    cutoff    = datetime.now(timezone.utc) - timedelta(days=days)
    trades    = []
    no_trades = []
    daemon_trade_count = 0
    webui_trade_count  = 0

    for log_file in sorted(log_dir.glob("daemon_*.log")):
        try:
            date_str = log_file.stem.replace("daemon_", "")
            file_dt  = datetime.strptime(date_str, "%Y%m%d").replace(tzinfo=timezone.utc)
            if file_dt < cutoff - timedelta(days=1):
                continue
        except ValueError:
            continue

        for line in log_file.read_text(errors="replace").splitlines():
            # SETTLED WIN/LOSS lines
            m = re.search(
                r"SETTLED (WIN|LOSS) ([+-]?\$[\d.]+) \| (\S+) \| BUY (\w+) @ (\d+)¢",
                line
            )
            if m:
                outcome, pnl_str, ticker, side, price = m.groups()
                try:
                    pnl = float(pnl_str.replace("$", "").replace("+", ""))
                    if "LOSS" in outcome:
                        pnl = -abs(pnl)
                    trades.append({
                        "outcome": outcome, "pnl": pnl,
                        "ticker": ticker, "side": side, "price": int(price),
                    })
                    daemon_trade_count += 1
                except ValueError:
                    pass
                continue

            # NO TRADE lines
            m2 = re.search(r"NO TRADE — (.+)", line)
            if m2:
                no_trades.append(m2.group(1).strip())

    # Parse webUI trade logs: trade-log-YYYY-MM-DD.csv
    for csv_file in sorted(log_dir.glob("trade-log-*.csv")):
        try:
            date_str = csv_file.stem.replace("trade-log-", "")
            file_dt  = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if file_dt < cutoff - timedelta(days=1):
                continue
        except ValueError:
            continue

        try:
            with csv_file.open("r", encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    status = (row.get("status") or "").strip().lower()
                    if status not in {"won", "lost", "win", "loss"}:
                        continue

                    outcome = "WIN" if status in {"won", "win"} else "LOSS"
                    try:
                        pnl_raw = float((row.get("pnl") or "0").strip())
                        price   = int(float((row.get("limitPrice") or "0").strip()))
                    except ValueError:
                        continue

                    pnl = abs(pnl_raw) if outcome == "WIN" else -abs(pnl_raw)
                    trades.append({
                        "outcome": outcome,
                        "pnl": pnl,
                        "ticker": (row.get("marketTicker") or row.get("windowKey") or "").strip(),
                        "side": (row.get("side") or "").strip().upper(),
                        "price": price,
                    })
                    webui_trade_count += 1
        except OSError:
            continue

    if not trades:
        return {"available": False, "message": "No settled trades in daemon/webUI logs yet"}

    wins   = [t for t in trades if t["outcome"] == "WIN"]
    losses = [t for t in trades if t["outcome"] == "LOSS"]
    total_pnl = sum(t["pnl"] for t in trades)
    wr        = len(wins) / len(trades) * 100

    # Rejection reason breakdown
    reason_counts: dict[str, int] = {}
    for reason in no_trades:
        key = reason[:60]
        reason_counts[key] = reason_counts.get(key, 0) + 1
    top_reasons = sorted(reason_counts.items(), key=lambda x: -x[1])[:5]

    return {
        "available":       True,
        "total_trades":    len(trades),
        "wins":            len(wins),
        "losses":          len(losses),
        "win_rate_pct":    round(wr, 1),
        "total_pnl":       round(total_pnl, 2),
        "avg_win":         round(sum(t["pnl"] for t in wins)   / max(len(wins), 1), 2),
        "avg_loss":        round(sum(t["pnl"] for t in losses) / max(len(losses), 1), 2),
        "no_trade_count":  len(no_trades),
        "top_skip_reasons": top_reasons,
        "source_counts": {
            "daemon": daemon_trade_count,
            "webui": webui_trade_count,
        },
        "by_price": {
            "65-73¢": _bucket_stats(trades, 65, 73),
            "73-79¢": _bucket_stats(trades, 73, 79),
            "<65¢":   _bucket_stats(trades, 0,  65),
        },
    }


def _bucket_stats(trades, lo, hi):
    bt = [t for t in trades if lo <= t["price"] < hi]
    if not bt:
        return None
    bw = [t for t in bt if t["outcome"] == "WIN"]
    return {
        "trades": len(bt),
        "wr":     round(len(bw)/len(bt)*100, 1),
        "pnl":    round(sum(t["pnl"] for t in bt), 2),
    }


def _append_bucket_section(lines: list[str], title: str, buckets: dict) -> None:
    lines += ["", f"**{title}:**", ""]
    if not buckets:
        lines.append("- No data in this sample")
        return
    for bucket, bdata in buckets.items():
        lines.append(f"- `{bucket}`: {bdata['trades']} trades, {bdata['wr']:.1f}% WR, P&L ${bdata['pnl']:+.2f}")


def _current_settings() -> dict:
    """Snapshot the key run_backtest settings used for this research run."""
    settings = {
        "MARKOV_MIN_GAP": _bt.MARKOV_MIN_GAP,
        "MIN_PERSIST": _bt.MIN_PERSIST,
        "MAX_ENTRY_PRICE_YES": _bt.MAX_ENTRY_PRICE_YES,
        "MAX_ENTRY_PRICE_NO": _bt.MAX_ENTRY_PRICE_NO,
        "MIN_MINUTES_LEFT": _bt.MIN_MINUTES_LEFT,
        "MAX_MINUTES_LEFT": _bt.MAX_MINUTES_LEFT,
        "MAX_VOL_MULT": _bt.MAX_VOL_MULT,
        "MIN_HURST": _bt.MIN_HURST,
        "BLOCKED_UTC_HOURS": sorted(_bt.BLOCKED_UTC_HOURS),
        "SIZING_MODE": _bt.SIZING_MODE,
        "ALLOWANCE_PCT": round(_bt.KELLY_FRACTION * 100, 2),
        "KELLY_FRACTION": _bt.KELLY_FRACTION,
    }
    if hasattr(_bt, "MAX_ENTRY_PRICE_RM"):
        settings["MAX_ENTRY_PRICE_RM"] = _bt.MAX_ENTRY_PRICE_RM
    return settings


# ── Claude analysis ────────────────────────────────────────────────────────────
def call_claude(baseline: dict, best: Optional[dict], live: dict, all_results: list) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return "No ANTHROPIC_API_KEY found — skipping Claude analysis."

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
    except ImportError:
        return "anthropic package not installed — skipping Claude analysis."

    # Build context
    ctx = f"""You are analyzing a live Kalshi BTC binary options trading algorithm.
The strategy bets YES/NO on whether BTC will be above/below a strike price at each
15-minute window expiry. Gates: Markov chain momentum signal, Hurst > 0.5 (trending
regime), Garman-Klass vol < 1.25×ref, timing window (6-9 min, or 3-12 for golden zone
65-73¢), UTC hour blocks (11, 18).

## Current strategy params
- MARKOV_MIN_GAP = {_bt.MARKOV_MIN_GAP}  (min directional conviction gap from 50%)
- MIN_PERSIST = {_bt.MIN_PERSIST}          (Markov state persistence threshold)
- MAX_ENTRY_PRICE_RM = {_bt.MAX_ENTRY_PRICE_RM}¢  (entry price cap — 65-73¢ golden zone)
- MIN_MINUTES_LEFT = {_bt.MIN_MINUTES_LEFT}, MAX_MINUTES_LEFT = {_bt.MAX_MINUTES_LEFT}
- BLOCKED_UTC_HOURS = {sorted(_bt.BLOCKED_UTC_HOURS)}
- MIN_HURST = {_bt.MIN_HURST}, MAX_VOL_MULT = {_bt.MAX_VOL_MULT}
- SIZING_MODE = {_bt.SIZING_MODE}
- ALLOWANCE_PCT = {_bt.KELLY_FRACTION * 100:.0f}%
- KELLY_FRACTION = {_bt.KELLY_FRACTION} (UI-aligned allowance fraction: bankroll × fraction per wager)

## 30-day backtest baseline
{json.dumps(baseline, indent=2)}

## Top parameter variations tested (ablation study)
{json.dumps(all_results[:10], indent=2)}
"""

    if best and score(best) > score(baseline) and score(baseline) > -9999:
        ctx += f"""
## Best improvement found
{json.dumps(best, indent=2)}
Score improvement: {score(best) - score(baseline):+.1f} (current: {score(baseline):.1f})
"""

    if live.get("available"):
        ctx += f"""
## Live performance (daemon + webUI, {live.get('total_trades', 0)} settled trades)
{json.dumps(live, indent=2)}
"""
    else:
        ctx += "\n## Live performance: not yet available (no settled daemon/webUI trades yet)\n"

    prompt = ctx + """
## Your task
Write a structured research report covering:

1. **Performance diagnosis** — what's working, what's not. Look at win rate by price bucket,
   trade count vs opportunities, any patterns in when we win vs lose.

2. **Parameter insights** — from the ablation study, which params have the most impact?
   Are the best variations consistent with the theory (e.g. tighter gap = fewer but higher
   quality trades)?

3. **Proposed changes** — list specific param changes to try next, with clear reasoning.
   Format as:
   ```
   PROPOSED: PARAM_NAME = new_value  (was old_value)
   REASON: ...
   ```

4. **Novel discovery ideas** — suggest 2-3 new signals or filters not currently in the
   algorithm that are worth backtesting. Be specific (e.g. "add RSI < 30 filter when
   betting YES in below-strike windows" not "try momentum indicators").

5. **Risk observations** — anything that looks fragile or over-fit.

Keep the report concise but data-driven. Every claim should reference specific numbers."""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


# ── Git branch ─────────────────────────────────────────────────────────────────
def propose_branch(best: dict, baseline_score: float) -> Optional[str]:
    """Create a git branch with proposed param changes if improvement is significant."""
    best_score = score(best)
    improvement_pct = (best_score / max(abs(baseline_score), 0.01) - 1) * 100
    if improvement_pct < 5:
        return None

    repo_root  = Path(__file__).parent.parent
    branch     = f"research/proposed-{datetime.now().strftime('%Y%m%d-%H%M')}"
    bt_file    = Path(__file__).parent / "run_backtest.py"
    content    = bt_file.read_text()

    # Apply param changes
    for param, new_val in best["params"].items():
        pattern = rf"^({re.escape(param)}\s*=\s*)(.+)$"
        content = re.sub(pattern, rf"\g<1>{new_val}", content, flags=re.MULTILINE)

    try:
        subprocess.run(["git", "-C", str(repo_root), "checkout", "-b", branch],
                       check=True, capture_output=True)
        bt_file.write_text(content)
        subprocess.run(["git", "-C", str(repo_root), "add",
                        str(bt_file.relative_to(repo_root))],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo_root), "commit", "-m",
                        f"research: proposed params ({improvement_pct:+.0f}% score)\n\n{json.dumps(best['params'])}"],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo_root), "checkout", "-"],
                       check=True, capture_output=True)
        return branch
    except subprocess.CalledProcessError as e:
        # Restore file if something went wrong
        try:
            subprocess.run(["git", "-C", str(repo_root), "checkout", "-"], capture_output=True)
            subprocess.run(["git", "-C", str(repo_root), "checkout", str(bt_file.relative_to(repo_root))],
                           capture_output=True)
        except Exception:
            pass
        return None


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Sentient research loop")
    parser.add_argument("--days",       type=int,  default=30,    help="Backtest lookback days")
    parser.add_argument("--allowance-pct", type=float, default=20.0,
                        help="Percent of bankroll allocated per wager in backtest (default: 20)")
    parser.add_argument("--sizing-mode", choices=["allowance", "legacy-kelly"], default="allowance",
                        help="Backtest sizing model for research runs")
    parser.add_argument("--no-claude",  action="store_true",      help="Skip Claude API call")
    parser.add_argument("--no-branch",  action="store_true",      help="Skip git branch creation")
    args = parser.parse_args()
    _bt.KELLY_FRACTION = args.allowance_pct / 100.0
    _bt.SIZING_MODE = args.sizing_mode

    research_dir = Path(__file__).parent / "research"
    research_dir.mkdir(exist_ok=True)
    report_path = research_dir / f"{datetime.now().strftime('%Y-%m-%d')}.md"

    print(f"\n{'='*60}")
    print(f"  Sentient Research Loop — {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  Backtest: {args.days} days | Mode: {args.sizing_mode} | Allowance: {args.allowance_pct:.0f}% | Claude: {not args.no_claude}")
    print(f"{'='*60}\n")

    # ── Step 1: Fetch data once ───────────────────────────────────────────────
    print("Fetching historical data (once)...")
    t0 = time.time()
    markets = _bt.fetch_settled_markets(args.days)
    # Keep a wide candle window for indicator warm-up and stable ablations.
    candle_days = max(args.days + 5, 60)
    c15     = _bt.fetch_candles_15m(candle_days)
    c5      = _bt.fetch_candles_5m(candle_days)
    print(f"  {len(markets)} markets, {len(c15)} 15m candles, {len(c5)} 5m candles "
          f"({time.time()-t0:.0f}s)\n")

    # ── Step 2: Baseline ──────────────────────────────────────────────────────
    print("Running baseline backtest...")
    baseline = _run_with_params(markets, c15, c5, {}, args.days)
    b_score  = score(baseline)
    print(f"  Baseline: {baseline['total_return_pct']:+.1f}% return | "
          f"{baseline['win_rate_pct']:.1f}% WR | "
          f"{baseline['total_trades']} trades | "
          f"{baseline['max_drawdown_pct']:.1f}% DD | "
          f"score={b_score:.1f}\n")

    # ── Step 3: Ablation grid ─────────────────────────────────────────────────
    print("Running parameter ablation study...")
    all_variations: list[dict] = []

    for param_name, values, label in PARAM_GRID:
        current_val = getattr(_bt, param_name, None)
        best_for_param = None

        for val in values:
            if val == current_val:
                continue
            override = {param_name: val}
            stats    = _run_with_params(markets, c15, c5, override, args.days)
            s        = score(stats)
            delta    = s - b_score
            marker   = " ★" if delta > 0 else ""
            print(f"  {label}={val}: {stats['total_return_pct']:+.1f}% | "
                  f"{stats['win_rate_pct']:.1f}% WR | "
                  f"{stats['total_trades']}T | score={s:.1f} ({delta:+.1f}){marker}")
            stats["variation_label"] = f"{param_name}={val}"
            all_variations.append(stats)
            if best_for_param is None or s > score(best_for_param):
                best_for_param = stats

        print()

    # Sort by score
    all_variations.sort(key=score, reverse=True)
    best = all_variations[0] if all_variations else None

    if b_score <= -9999:
        print("Baseline has <10 executed trades; score-based ranking is not reliable yet.\n")
    elif best and score(best) > b_score:
        print(f"Best variation: {best['variation_label']} "
              f"(score {score(best):.1f} vs baseline {b_score:.1f}, "
              f"{(score(best)/max(abs(b_score),0.01)-1)*100:+.0f}%)\n")
    else:
        print("No variation beat the baseline.\n")

    # ── Step 4: Parse live logs (daemon + webUI) ─────────────────────────────
    print("Parsing live logs (daemon + webUI)...")
    live_stats = parse_daemon_logs(days=7)
    if live_stats.get("available"):
        print(f"  Live: {live_stats['total_trades']} trades | "
              f"{live_stats['win_rate_pct']:.1f}% WR | "
              f"P&L ${live_stats['total_pnl']:+.2f}\n")
    else:
        print(f"  {live_stats.get('message', 'No live data')}\n")

    # ── Step 5: Claude analysis ───────────────────────────────────────────────
    analysis = ""
    if not args.no_claude:
        print("Calling Claude for analysis (this takes ~15s)...")
        analysis = call_claude(baseline, best, live_stats, all_variations[:12])
        print("  Done.\n")

    # ── Step 6: Write report ──────────────────────────────────────────────────
    current_settings = _current_settings()
    lines = [
        f"# Sentient Research Report — {datetime.now().strftime('%Y-%m-%d')}",
        f"",
        f"**Backtest:** {args.days} days | **Baseline score:** {b_score:.1f}",
        f"",
        f"## Current Settings",
        f"",
        f"| Parameter | Value |",
        f"|-----------|-------|",
    ]
    for key, val in current_settings.items():
        lines.append(f"| `{key}` | `{val}` |")

    lines += [
        f"",
        f"## Baseline ({args.days}d)",
        f"",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Return | {baseline['total_return_pct']:+.1f}% |",
        f"| Win rate | {baseline['win_rate_pct']:.1f}% |",
        f"| Trades | {baseline['total_trades']} |",
        f"| Profit factor | {baseline['profit_factor']:.2f} |",
        f"| Max drawdown | {baseline['max_drawdown_pct']:.1f}% |",
        f"",
        f"**Price buckets:**",
        f"",
    ]

    _append_bucket_section(lines, "Price buckets", baseline.get("price_buckets", {}))
    _append_bucket_section(lines, "Time buckets", baseline.get("time_buckets", {}))

    if live_stats.get("available"):
        lines += [
            f"",
            f"## Live Performance (last 7 days)",
            f"",
            f"| Metric | Value |",
            f"|--------|-------|",
            f"| Trades | {live_stats['total_trades']} |",
            f"| Win rate | {live_stats['win_rate_pct']:.1f}% |",
            f"| Total P&L | ${live_stats['total_pnl']:+.2f} |",
            f"| Avg win | ${live_stats['avg_win']:+.2f} |",
            f"| Avg loss | ${live_stats['avg_loss']:+.2f} |",
        ]
        source_counts = live_stats.get("source_counts") or {}
        if source_counts:
            lines += [
                f"| Daemon settled trades | {source_counts.get('daemon', 0)} |",
                f"| WebUI settled trades | {source_counts.get('webui', 0)} |",
            ]
        if live_stats.get("top_skip_reasons"):
            lines += ["", "**Top skip reasons:**", ""]
            for reason, count in live_stats["top_skip_reasons"]:
                lines.append(f"- ({count}×) {reason}")

    lines += [
        f"",
        f"## Ablation Study — Top 10 Variations",
        f"",
        f"| Variation | Return | WR | Trades | DD | Score | Δ |",
        f"|-----------|--------|-----|--------|-----|-------|---|",
    ]
    for v in all_variations[:10]:
        s = score(v)
        d = s - b_score
        lines.append(
            f"| `{v['variation_label']}` | "
            f"{v['total_return_pct']:+.1f}% | "
            f"{v['win_rate_pct']:.1f}% | "
            f"{v['total_trades']} | "
            f"{v['max_drawdown_pct']:.1f}% | "
            f"{s:.1f} | "
            f"{d:+.1f} |"
        )

    if analysis:
        lines += ["", "## Claude Analysis", "", analysis]

    report_path.write_text("\n".join(lines))
    print(f"Report saved → {report_path}")

    # ── Step 7: Propose git branch ────────────────────────────────────────────
    if not args.no_branch and b_score > -9999 and best and score(best) > b_score:
        branch = propose_branch(best, b_score)
        if branch:
            print(f"\nProposed branch created: {branch}")
            print(f"Review with: git diff main..{branch}")
            print(f"Merge with:  git checkout main && git merge {branch}")
        else:
            improvement = (score(best)/max(abs(b_score),0.01)-1)*100
            if improvement < 5:
                print(f"\nImprovement {improvement:.1f}% < 5% threshold — no branch created")
            else:
                print("\nBranch creation failed (check git status)")

    print(f"\nDone. Report: {report_path}\n")
    return baseline, all_variations, live_stats


if __name__ == "__main__":
    main()
