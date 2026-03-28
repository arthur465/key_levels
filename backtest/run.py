"""
backtest/run.py
CLI runner for the backtest engine.

Usage:
    python -m backtest.run                  # 180 days default
    python -m backtest.run --days 365       # 1 year
    python -m backtest.run --days 90 --csv  # 90 days + export CSV
"""
import argparse
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.fetcher import fetch_ohlcv
from backtest.engine import BacktestEngine
from backtest.report import print_report, export_csv


def bars_for_days(days: int, minutes_per_bar: int) -> int:
    return int(days * 24 * 60 / minutes_per_bar) + 200  # +200 warmup


def main():
    parser = argparse.ArgumentParser(description="BTC Liquidity System Backtest")
    parser.add_argument("--days",  type=int,  default=180, help="Days of history (default: 180)")
    parser.add_argument("--csv",   action="store_true",    help="Export trades to CSV")
    parser.add_argument("--capital", type=float, default=None, help="Starting capital (overrides config)")
    args = parser.parse_args()

    days = args.days
    print(f"\n{'='*52}")
    print(f"  BTC ICT LIQUIDITY SYSTEM — BACKTEST")
    print(f"  Period: {days} days")
    print(f"{'='*52}\n")

    print("[data] Fetching historical OHLCV...")

    df_15m  = fetch_ohlcv("15m",  limit=bars_for_days(days, 15))
    df_1h   = fetch_ohlcv("1h",   limit=bars_for_days(days, 60))
    df_4h   = fetch_ohlcv("4h",   limit=bars_for_days(days, 240))
    df_daily = fetch_ohlcv("1d",  limit=days + 30)

    print(f"[data] 15m bars: {len(df_15m)} | 1h bars: {len(df_1h)} | 4h bars: {len(df_4h)} | Daily: {len(df_daily)}")

    import config
    capital = args.capital or config.INITIAL_CAPITAL

    engine = BacktestEngine(capital=capital)
    result = engine.run(
        df_15m   = df_15m,
        df_1h    = df_1h,
        df_4h    = df_4h,
        df_daily = df_daily,
    )

    print_report(result)

    if args.csv:
        export_csv(result, f"backtest_{days}d.csv")
        print(f"[backtest] Open backtest_{days}d.csv in Excel/Google Sheets for deep analysis.")


if __name__ == "__main__":
    main()
