"""
main.py — BTC Liquidity System
Entry point for Railway deployment.

Runs as a persistent background worker:
  • Every 60s  → scan all timeframes for signals
  • Every 60s  → update open paper trades with latest price
  • 08:00 UTC  → send daily P&L report to Telegram
"""
import time
import schedule
from datetime import datetime, timezone

import config
from data.fetcher import fetch_current_price
from signals.detector import scan_for_signals
from simulator.engine import SimulatorEngine
from simulator.reporter import (
    generate_full_report,
    format_new_trade,
    format_closed_trade,
)
from notifications.telegram import send_message, send_startup
from analysis.cvd_realtime import start_stream

# ── Global simulator instance (persists state to sim_state.json) ──────────────
sim = SimulatorEngine()

# ── Scan configuration ────────────────────────────────────────────────────────
SCAN_CONFIGS = [
    {"timeframe": "15m", "trade_mode": "day_trade"},
    {"timeframe": "1h",  "trade_mode": "swing"},
    {"timeframe": "4h",  "trade_mode": "swing"},
]


def run_scan() -> None:
    ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"\n{'─'*55}")
    print(f"  SCAN @ {ts}")
    print(f"{'─'*55}")

    # ── Update open trades ────────────────────────────────────────────────────
    try:
        price = fetch_current_price()
        print(f"  BTC: ${price:,.2f}")

        resolved = sim.update_open_trades(price)
        for t in resolved:
            msg = format_closed_trade(t)
            send_message(msg)
            print(f"  Trade #{t.id} closed → {t.status.upper()} ${t.pnl:+,.2f}")

    except Exception as e:
        print(f"  [price] Error: {e}")
        return

    # ── Scan each timeframe ───────────────────────────────────────────────────
    seen_setups = set()   # Deduplicate signals across TFs pointing at same level

    for cfg in SCAN_CONFIGS:
        tf   = cfg["timeframe"]
        mode = cfg["trade_mode"]
        print(f"\n  [{tf}] Scanning ({mode})...")

        signal = scan_for_signals(timeframe=tf, trade_mode=mode)

        if signal is None:
            print(f"  [{tf}] No setup found.")
            continue

        # Dedup key: direction + swept level + timeframe
        dedup_key = (signal.fib.direction, signal.swept_level_name, tf)
        if dedup_key in seen_setups:
            continue
        seen_setups.add(dedup_key)

        print(f"  [{tf}] {signal.label()}")

        # ── Always notify on Telegram (including filtered signals) ─────────────
        # Valid signals → trade opened + full alert
        # Invalid signals → alert with filter reason (informational)
        telegram_msg = signal.telegram_message()
        send_message(telegram_msg)

        if signal.valid:
            trade = sim.process_signal(signal)
            if trade:
                trade_msg = format_new_trade(trade)
                send_message(trade_msg)
                print(f"  [{tf}] Trade #{trade.id} opened (sim) — Entry ${trade.entry:,.2f}")
        else:
            print(f"  [{tf}] Signal filtered: {', '.join(signal.reject_reasons)}")


def send_daily_report() -> None:
    print("\n  Sending daily report...")
    report = generate_full_report(sim)
    send_message(report)
    print("  Daily report sent.")


def main() -> None:
    print("=" * 55)
    print("  BTC LIQUIDITY SYSTEM")
    print(f"  Capital:  ${config.INITIAL_CAPITAL:,}")
    print(f"  Risk:     {config.RISK_PER_TRADE*100:.0f}% per trade")
    print(f"  Symbol:   {config.SYMBOL}")
    print(f"  Interval: {config.POLL_INTERVAL_SECONDS}s")
    print(f"  CVD:      {'ON' if config.USE_CVD else 'OFF'}")
    print("=" * 55)

    send_startup()

    # ── Start real-time CVD stream ─────────────────────────────────────────
    print("  Starting CVD WebSocket stream...")
    cvd_stream = start_stream()
    print("  CVD stream live ✓")

    # ── Schedule ──────────────────────────────────────────────────────────────
    schedule.every(config.POLL_INTERVAL_SECONDS).seconds.do(run_scan)
    schedule.every().day.at(config.DAILY_REPORT_TIME_UTC).do(send_daily_report)

    # Run immediately on startup
    run_scan()

    # ── Main loop ─────────────────────────────────────────────────────────────
    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    main()
