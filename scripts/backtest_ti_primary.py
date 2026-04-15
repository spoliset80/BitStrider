"""
backtest_ti_primary.py
-----------------------
Backtest the latest Trade Ideas primary tickers from data/ti_primary.json.
Defaults to the most recent 14-day window and a limit of 50 tickers to keep
execution manageable.

Usage:
    python scripts/backtest_ti_primary.py --limit 50 --days 14
"""

import argparse
import datetime
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.equity.universe import get_ti_primary
from scripts.backtest_options import backtest_symbol, OPTIONS_ALLOCATION_PCT, OPTIONS_PROFIT_TARGET_PCT, OPTIONS_STOP_LOSS_PCT, OPTIONS_DTE_MIN, OPTIONS_DTE_MAX


def parse_date(value: str) -> datetime.date:
    return datetime.date.fromisoformat(value)


def main() -> int:
    parser = argparse.ArgumentParser(description="Backtest latest TI primary tickers")
    parser.add_argument("--limit", type=int, default=50, help="Maximum number of latest TI tickers to test")
    parser.add_argument("--days", type=int, default=14, help="Number of days to backtest")
    parser.add_argument("--start", help="Start date YYYY-MM-DD")
    parser.add_argument("--end", help="End date YYYY-MM-DD")
    parser.add_argument("--capital", type=float, default=10000.0, help="Initial capital")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--ignore-stale", action="store_true", help="Use raw data/ti_primary.json even if it is marked stale")
    parser.add_argument("--use-finnhub", action="store_true", help="Fetch bar history from Finnhub API if available")
    args = parser.parse_args()

    if args.use_finnhub:
        os.environ["USE_FINNHUB_HISTORICAL"] = "true"

    if args.start:
        start = parse_date(args.start)
    else:
        start = datetime.date.today() - datetime.timedelta(days=args.days)
    if args.end:
        end = parse_date(args.end)
    else:
        end = datetime.date.today()

    tickers = get_ti_primary()
    if not tickers and args.ignore_stale:
        import json
        from pathlib import Path
        raw_path = Path(ROOT) / "data" / "ti_primary.json"
        if raw_path.exists():
            data = json.loads(raw_path.read_text(encoding="utf-8"))
            tickers = [str(t).strip().upper() for t in data.get("tickers", []) if isinstance(t, str) and t.strip()]
            print("Warning: using raw ti_primary.json data even though it is stale")

    if not tickers:
        print("No fresh TI primary tickers available in data/ti_primary.json")
        return 1

    tickers = tickers[: args.limit] if args.limit and len(tickers) > args.limit else tickers
    print(f"Backtest TI primary latest tickers ({len(tickers)} symbols): {', '.join(tickers[:10])}{'...' if len(tickers) > 10 else ''}")
    print(f"Date range: {start} → {end}")
    print(f"Capital: ${args.capital:,.0f}")
    print(f"Rules: TP={OPTIONS_PROFIT_TARGET_PCT:.0f}% SL=-{OPTIONS_STOP_LOSS_PCT:.0f}% DTE={OPTIONS_DTE_MIN}–{OPTIONS_DTE_MAX}")
    print("=" * 80)

    for symbol in tickers:
        print(f"\n{symbol}:", end=" ")
        trades = backtest_symbol(symbol, start, end, args.capital, args.verbose)
        if trades.empty:
            print("no trades")
            continue
        wins = trades[trades['pnl_$'] > 0]
        total_pnl = trades['pnl_$'].sum()
        print(f"{len(trades)} trades | win={len(wins)}/{len(trades)} ({100*len(wins)/len(trades):.0f}%) | P&L=${total_pnl:+,.2f}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
