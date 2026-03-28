"""
backtest/report.py
Print and export backtest results.
"""
import csv
import os
from backtest.engine import BacktestResult


def print_report(result: BacktestResult) -> None:
    c  = result.closed()
    bd = result.breakdown()

    sep = "─" * 52

    print(f"\n{'═'*52}")
    print(f"  BACKTEST RESULTS")
    print(f"{'═'*52}")
    print(f"  Initial Capital:    ${result.initial_capital:>12,.2f}")
    print(f"  Final Capital:      ${result.capital:>12,.2f}")
    print(f"  Total Return:       {result.return_pct():>+11.2f}%")
    print(f"  Max Drawdown:       {result.max_drawdown():>11.2f}%")
    print(sep)
    print(f"  Total Signals:      {len(result.trades):>12}")
    print(f"  Closed Trades:      {len(c):>12}")
    print(f"  Filtered Out:       {bd['filtered_out']:>12}")
    print(f"  Wins:               {len(result.wins()):>12}")
    print(f"  Losses:             {len(result.losses()):>12}")
    print(f"  Win Rate:           {result.win_rate():>11.1f}%")
    print(f"  Avg R:R:            {result.avg_rr():>12.2f}")
    print(f"  Profit Factor:      {result.profit_factor():>12.2f}")
    print(f"  Expectancy/Trade:   ${result.expectancy():>+11.2f}")
    print(f"  Sharpe (trades):    {result.sharpe():>12.2f}")
    print(f"  Max Consec Losses:  {result.max_consecutive_losses():>12}")
    print(sep)
    print(f"  BY MODE:")
    print(f"    Day Trades:  {bd['day_trade']['count']:>4}  |  WR {bd['day_trade']['win_rate']:>4.0f}%  |  ${bd['day_trade']['pnl']:>+9,.0f}")
    print(f"    Swings:      {bd['swing']['count']:>4}  |  WR {bd['swing']['win_rate']:>4.0f}%  |  ${bd['swing']['pnl']:>+9,.0f}")
    print(sep)
    print(f"  BY TREND ALIGNMENT:")
    print(f"    With-Trend:  {bd['with_trend']['count']:>4}  |  WR {bd['with_trend']['win_rate']:>4.0f}%  |  ${bd['with_trend']['pnl']:>+9,.0f}")
    print(f"    Counter:     {bd['counter_trend']['count']:>4}  |  WR {bd['counter_trend']['win_rate']:>4.0f}%  |  ${bd['counter_trend']['pnl']:>+9,.0f}")
    print(sep)
    print(f"  BY SETUP TYPE:")
    print(f"    Reversals:   {bd['reversal']['count']:>4}  |  WR {bd['reversal']['win_rate']:>4.0f}%  |  ${bd['reversal']['pnl']:>+9,.0f}")
    print(f"    Continuations:{bd['continuation']['count']:>3}  |  WR {bd['continuation']['win_rate']:>4.0f}%  |  ${bd['continuation']['pnl']:>+9,.0f}")
    print(sep)
    print(f"  BY DIRECTION:")
    print(f"    Longs:       {bd['long']['count']:>4}  |  WR {bd['long']['win_rate']:>4.0f}%  |  ${bd['long']['pnl']:>+9,.0f}")
    print(f"    Shorts:      {bd['short']['count']:>4}  |  WR {bd['short']['win_rate']:>4.0f}%  |  ${bd['short']['pnl']:>+9,.0f}")
    print(sep)
    print(f"  BY SESSION:")
    print(f"    London:      {bd['london']['count']:>4}  |  WR {bd['london']['win_rate']:>4.0f}%  |  ${bd['london']['pnl']:>+9,.0f}")
    print(f"    New York:    {bd['new_york']['count']:>4}  |  WR {bd['new_york']['win_rate']:>4.0f}%  |  ${bd['new_york']['pnl']:>+9,.0f}")
    print(f"{'═'*52}\n")


def export_csv(result: BacktestResult, path: str = "backtest_trades.csv") -> None:
    """Export all trades to CSV for further analysis in Excel/Sheets."""
    fields = [
        "id", "timestamp", "timeframe", "trade_mode", "direction",
        "fib_mode", "swept_level", "htf_trend", "session",
        "entry", "stop_loss", "take_profit", "risk_reward", "risk_usd",
        "status", "exit_price", "pnl", "bars_held", "filters_passed",
        "reject_reasons",
    ]

    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for idx, t in enumerate(result.trades):
            writer.writerow({
                "id":            idx + 1,
                "timestamp":     str(t.timestamp),
                "timeframe":     t.timeframe,
                "trade_mode":    t.trade_mode,
                "direction":     t.direction,
                "fib_mode":      t.fib_mode,
                "swept_level":   t.swept_level,
                "htf_trend":     t.htf_trend,
                "session":       t.session,
                "entry":         round(t.entry, 2),
                "stop_loss":     round(t.stop_loss, 2),
                "take_profit":   round(t.take_profit, 2),
                "risk_reward":   round(t.risk_reward, 2),
                "risk_usd":      round(t.risk_usd, 2),
                "status":        t.status,
                "exit_price":    round(t.exit_price, 2),
                "pnl":           round(t.pnl, 2),
                "bars_held":     t.bars_held,
                "filters_passed": t.filters_passed,
                "reject_reasons": "|".join(t.reject_reasons),
            })

    print(f"[backtest] Trades exported → {path}")
