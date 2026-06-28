"""
Analyze <65c gap-filtered opportunities from daemon logs.

Usage:
  python3 analyze_lt65_filter.py --days 7

It reads daemon logs for NO TRADE lines containing the lt65 gate reason,
maps each ticker to settled result, and reports how many filtered trades
would have been wins/losses for the suggested side at the time.
"""

import argparse
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

from run_backtest import fetch_settled_markets

NO_TRADE_RE = re.compile(
    r"NO TRADE — ticker=(?P<ticker>\S+) side=(?P<side>YES|NO) lp=(?P<lp>\d+)¢ gap=(?P<gap>[0-9.]+) \| (?P<reason>.+)"
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Analyze lt65 filter counterfactual outcomes")
    p.add_argument("--days", type=int, default=7, help="Lookback days for logs/results")
    return p.parse_args()


def load_filtered_candidates(log_dir: Path, days: int) -> dict[str, dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    by_ticker: dict[str, dict] = {}

    for log_file in sorted(log_dir.glob("daemon_*.log")):
        try:
            d = datetime.strptime(log_file.stem.replace("daemon_", ""), "%Y%m%d").replace(tzinfo=timezone.utc)
            if d < cutoff - timedelta(days=1):
                continue
        except ValueError:
            continue

        for line in log_file.read_text(errors="replace").splitlines():
            m = NO_TRADE_RE.search(line)
            if not m:
                continue
            reason = m.group("reason")
            if "<65" not in reason or "requires gap" not in reason:
                continue

            ticker = m.group("ticker")
            side = m.group("side").lower()
            lp = int(m.group("lp"))
            gap = float(m.group("gap"))

            # Keep latest observation per ticker in case of multiple retries.
            by_ticker[ticker] = {
                "ticker": ticker,
                "side": side,
                "lp": lp,
                "gap": gap,
                "reason": reason,
            }

    return by_ticker


def main() -> None:
    args = parse_args()
    log_dir = Path(__file__).parent / "logs"

    filtered = load_filtered_candidates(log_dir, args.days)
    if not filtered:
        print("No lt65 gap-filtered NO TRADE events found in lookback.")
        return

    settled = fetch_settled_markets(args.days)
    result_by_ticker = {m["ticker"]: m["result"] for m in settled}

    rows = []
    wins = losses = unresolved = 0

    for ticker, rec in sorted(filtered.items()):
        result = result_by_ticker.get(ticker)
        if result not in {"yes", "no"}:
            unresolved += 1
            outcome = "UNRESOLVED"
        else:
            would_win = rec["side"] == result
            outcome = "WIN" if would_win else "LOSS"
            if would_win:
                wins += 1
            else:
                losses += 1

        rows.append((ticker, rec["side"].upper(), rec["lp"], rec["gap"], outcome))

    resolved = wins + losses
    wr = (wins / resolved * 100.0) if resolved else 0.0

    print("\nLT65 GAP FILTER COUNTERFACTUAL")
    print("=" * 72)
    print(f"Lookback days:      {args.days}")
    print(f"Filtered tickers:   {len(filtered)}")
    print(f"Resolved outcomes:  {resolved}")
    print(f"Would-be wins:      {wins}")
    print(f"Would-be losses:    {losses}")
    print(f"Would-be win rate:  {wr:.1f}%")
    print(f"Unresolved:         {unresolved}")
    print("=" * 72)
    print(f"{'Ticker':<28} {'Side':<4} {'LP':>4} {'Gap':>6} {'Outcome':>10}")
    print("-" * 72)
    for t, s, lp, gap, out in rows:
        print(f"{t:<28} {s:<4} {lp:>3}¢ {gap:>6.3f} {out:>10}")


if __name__ == "__main__":
    main()
