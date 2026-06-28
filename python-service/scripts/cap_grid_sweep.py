#!/usr/bin/env python3
"""Cap-grid sweep utility for MAX_ENTRY_PRICE_YES / MAX_ENTRY_PRICE_NO.

Runs backtest scenarios across multiple lookback windows and cap combinations,
then prints ranked summaries and writes a CSV artifact.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import statistics
import sys

# Allow running this script from anywhere.
PY_SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(PY_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(PY_SERVICE_ROOT))

import run_backtest as bt  # noqa: E402


@dataclass
class SweepRow:
    window_days: int
    yes_cap: int
    no_cap: int
    qualified: int
    executed: int
    win_rate_pct: float
    net_return_pct: float
    max_dd_net_pct: float
    withdrawn_usd: float


def parse_int_list(raw: str) -> list[int]:
    vals: list[int] = []
    for token in raw.split(','):
        token = token.strip()
        if not token:
            continue
        vals.append(int(token))
    if not vals:
        raise ValueError("List cannot be empty")
    return vals


def run_scenario(days: int, yes_cap: int, no_cap: int, sizing_mode: str) -> SweepRow:
    bt.MAX_ENTRY_PRICE_YES = yes_cap
    bt.MAX_ENTRY_PRICE_NO = no_cap

    markets = bt.fetch_settled_markets(days)
    candles_15m = bt.fetch_candles_15m(days)
    candles_5m = bt.fetch_candles_5m(days)

    records = []
    for market in markets:
        rec = bt.process_market(market, candles_15m, candles_5m)
        if rec:
            records.append(rec)

    records.sort(key=lambda x: x["entry_dt"])
    sim_records = [dict(r) for r in records]

    final_cash, withdrawn, resets = bt.simulate(sim_records, sizing_mode=sizing_mode)
    summary = bt.summarize_run(
        markets,
        sim_records,
        final_cash,
        sizing_mode,
        withdrawn_total=withdrawn,
        reset_count=resets,
    )

    return SweepRow(
        window_days=days,
        yes_cap=yes_cap,
        no_cap=no_cap,
        qualified=len(records),
        executed=len(summary["executed"]),
        win_rate_pct=round(summary["wr"], 1),
        net_return_pct=round(summary["net_ret_pct"], 1),
        max_dd_net_pct=round(summary["max_dd_net"], 1),
        withdrawn_usd=round(withdrawn, 2),
    )


def default_output_path(windows: list[int]) -> Path:
    day_tag = "_".join(str(d) for d in windows)
    date_tag = datetime.now(timezone.utc).date().isoformat()
    return PY_SERVICE_ROOT / "research" / f"cap_grid_{day_tag}d_{date_tag}.csv"


def print_per_window_top(rows: list[SweepRow], windows: list[int], top_n: int) -> None:
    print("PER_WINDOW_TOP")
    for days in windows:
        subset = [r for r in rows if r.window_days == days]
        top = sorted(subset, key=lambda r: r.net_return_pct, reverse=True)[:top_n]
        print(f"WINDOW={days}d")
        for r in top:
            print(
                f"  YES={r.yes_cap} NO={r.no_cap} "
                f"NET={r.net_return_pct:+.1f}% DD={r.max_dd_net_pct:.1f}% "
                f"WR={r.win_rate_pct:.1f}% EXEC={r.executed}"
            )


def print_aggregate(rows: list[SweepRow], caps: list[int], top_n: int, dd_cap: float) -> None:
    agg: list[tuple[int, int, float, float, float, float, float]] = []
    # yes_cap, no_cap, avg_net, min_net, avg_dd, avg_wr, avg_exec
    for y in caps:
        for n in caps:
            subset = [r for r in rows if r.yes_cap == y and r.no_cap == n]
            avg_net = round(statistics.mean(r.net_return_pct for r in subset), 1)
            min_net = round(min(r.net_return_pct for r in subset), 1)
            avg_dd = round(statistics.mean(r.max_dd_net_pct for r in subset), 1)
            avg_wr = round(statistics.mean(r.win_rate_pct for r in subset), 1)
            avg_exec = round(statistics.mean(r.executed for r in subset), 1)
            agg.append((y, n, avg_net, min_net, avg_dd, avg_wr, avg_exec))

    print("AGG_TOP_BY_AVG_NET")
    for y, n, avg_net, min_net, avg_dd, avg_wr, avg_exec in sorted(agg, key=lambda x: x[2], reverse=True)[:top_n]:
        print(
            f"  YES={y} NO={n} AVG_NET={avg_net:+.1f}% MIN_NET={min_net:+.1f}% "
            f"AVG_DD={avg_dd:.1f}% AVG_WR={avg_wr:.1f}% AVG_EXEC={avg_exec}"
        )

    print(f"AGG_TOP_BY_AVG_NET_UNDER_DD_{dd_cap:g}")
    filtered = [x for x in sorted(agg, key=lambda x: x[2], reverse=True) if x[4] <= dd_cap]
    for y, n, avg_net, min_net, avg_dd, avg_wr, avg_exec in filtered[:top_n]:
        print(
            f"  YES={y} NO={n} AVG_NET={avg_net:+.1f}% MIN_NET={min_net:+.1f}% "
            f"AVG_DD={avg_dd:.1f}% AVG_WR={avg_wr:.1f}% AVG_EXEC={avg_exec}"
        )


def write_csv(rows: list[SweepRow], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="ascii") as f:
        w = csv.writer(f)
        w.writerow([
            "WINDOW_DAYS",
            "YES_CAP",
            "NO_CAP",
            "QUALIFIED",
            "EXECUTED",
            "WIN_RATE_PCT",
            "NET_RETURN_PCT",
            "MAX_DD_NET_PCT",
            "WITHDRAWN_USD",
        ])
        for r in rows:
            w.writerow([
                r.window_days,
                r.yes_cap,
                r.no_cap,
                r.qualified,
                r.executed,
                f"{r.win_rate_pct:.1f}",
                f"{r.net_return_pct:.1f}",
                f"{r.max_dd_net_pct:.1f}",
                f"{r.withdrawn_usd:.2f}",
            ])


def main() -> None:
    parser = argparse.ArgumentParser(description="Sweep YES/NO cap grid over multiple windows.")
    parser.add_argument("--windows", default="7,14,59", help="Comma-separated lookback windows in days")
    parser.add_argument("--caps", default="65,68,72,75", help="Comma-separated cap values for YES/NO")
    parser.add_argument("--sizing-mode", choices=["allowance", "legacy-kelly"], default="allowance")
    parser.add_argument("--top", type=int, default=8, help="Number of rows to print for rankings")
    parser.add_argument("--dd-cap", type=float, default=30.0, help="Drawdown ceiling for filtered ranking")
    parser.add_argument("--output", default="", help="Output CSV path (default under python-service/research)")
    args = parser.parse_args()

    windows = parse_int_list(args.windows)
    caps = parse_int_list(args.caps)

    out_path = Path(args.output) if args.output else default_output_path(windows)

    rows: list[SweepRow] = []
    for d in windows:
        print(f"WINDOW {d}d start", flush=True)
        for y in caps:
            for n in caps:
                row = run_scenario(d, y, n, args.sizing_mode)
                rows.append(row)
                print(
                    f"  d={d} YES={y} NO={n} "
                    f"net={row.net_return_pct:+.1f}% dd={row.max_dd_net_pct:.1f}% "
                    f"wr={row.win_rate_pct:.1f}% ex={row.executed}",
                    flush=True,
                )

    write_csv(rows, out_path)
    print(f"SAVED={out_path}")

    print_per_window_top(rows, windows, top_n=min(args.top, len(caps) * len(caps)))
    print_aggregate(rows, caps, top_n=min(args.top, len(caps) * len(caps)), dd_cap=args.dd_cap)


if __name__ == "__main__":
    main()
