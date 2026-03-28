"""
simulator/reporter.py
Generate structured performance reports from simulator state.
"""
from typing import List
from simulator.engine import SimulatorEngine, SimTrade


def _wr(trades: List[SimTrade]) -> float:
    if not trades:
        return 0.0
    return len([t for t in trades if t.status == "win"]) / len(trades) * 100


def _pnl(trades: List[SimTrade]) -> float:
    return sum(t.pnl for t in trades)


def generate_full_report(sim: SimulatorEngine) -> str:
    c = sim.closed_trades
    o = sim.open_trades

    if not c:
        return (
            "📊 *SIMULATOR REPORT*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"No closed trades yet.\n"
            f"Capital: ${sim.capital:,.2f}\n"
            f"Open trades: {len(o)}"
        )

    wins   = [t for t in c if t.status == "win"]
    losses = [t for t in c if t.status == "loss"]
    avg_rr = sum(t.risk_reward for t in c) / len(c) if c else 0

    # By mode
    dt  = [t for t in c if t.trade_mode == "day_trade"]
    sw  = [t for t in c if t.trade_mode == "swing"]

    # By trend alignment
    wt  = [t for t in c if t.htf_trend == "with_trend"]
    ct  = [t for t in c if t.htf_trend == "counter_trend"]

    # By direction
    longs  = [t for t in c if t.direction == "long"]
    shorts = [t for t in c if t.direction == "short"]

    # By mode type
    rev  = [t for t in c if t.fib_mode == "reversal"]
    cont = [t for t in c if t.fib_mode == "continuation"]

    profit_factor = (
        abs(_pnl([t for t in c if t.status == "win"])) /
        abs(_pnl([t for t in c if t.status == "loss"]) or 1)
    )

    lines = [
        "📊 *SIMULATOR PERFORMANCE REPORT*",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"💰 Capital:        ${sim.capital:,.2f}",
        f"📈 Total Return:   {sim.return_pct:+.2f}%",
        f"📉 Max Drawdown:   {sim.drawdown_pct:.2f}%",
        f"💵 Total P&L:      ${sim.total_pnl:+,.2f}",
        f"━━━━━━━━━━━━━━━━━━━━━━",
        f"📋 Total Closed:   {len(c)}",
        f"✅ Wins:           {len(wins)}",
        f"❌ Losses:         {len(losses)}",
        f"🎯 Win Rate:       {sim.win_rate:.1f}%",
        f"⚖️  Avg R:R:        {avg_rr:.2f}",
        f"📊 Profit Factor:  {profit_factor:.2f}",
        f"━━━━━━━━━━━━━━━━━━━━━━",
        f"🕐 Day Trades:     {len(dt)} | WR {_wr(dt):.0f}% | ${_pnl(dt):+,.0f}",
        f"📅 Swings:         {len(sw)} | WR {_wr(sw):.0f}% | ${_pnl(sw):+,.0f}",
        f"━━━━━━━━━━━━━━━━━━━━━━",
        f"✅ With-Trend:     {len(wt)} | WR {_wr(wt):.0f}%",
        f"⚠️  Counter-Trend:  {len(ct)} | WR {_wr(ct):.0f}%",
        f"━━━━━━━━━━━━━━━━━━━━━━",
        f"🔄 Reversals:      {len(rev)} | WR {_wr(rev):.0f}%",
        f"➡️  Continuations:  {len(cont)} | WR {_wr(cont):.0f}%",
        f"━━━━━━━━━━━━━━━━━━━━━━",
        f"🟢 Longs:          {len(longs)} | WR {_wr(longs):.0f}%",
        f"🔴 Shorts:         {len(shorts)} | WR {_wr(shorts):.0f}%",
        f"━━━━━━━━━━━━━━━━━━━━━━",
        f"🔄 Open Trades:    {len(o)}",
    ]

    if o:
        lines.append("")
        lines.append("*Open Positions:*")
        for t in o:
            arrow = "🔴" if t.direction == "short" else "🟢"
            lines.append(
                f"  #{t.id} {arrow} {t.direction.upper()} | "
                f"Entry ${t.entry:,.0f} | "
                f"TP ${t.take_profit:,.0f} | "
                f"SL ${t.stop_loss:,.0f}"
            )

    return "\n".join(lines)


def format_new_trade(trade: SimTrade) -> str:
    arrow  = "🔴" if trade.direction == "short" else "🟢"
    ct_tag = " ⚠️ *COUNTER-TREND*" if trade.counter_trend else " ✅ *WITH-TREND*"
    return (
        f"{arrow} *NEW TRADE #{trade.id}*{ct_tag}\n"
        f"Mode: *{trade.trade_mode.upper()}* | {trade.fib_mode.upper()}\n"
        f"TF: {trade.timeframe} | Swept: {trade.swept_level}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Entry:  ${trade.entry:,.2f}\n"
        f"SL:     ${trade.stop_loss:,.2f}\n"
        f"TP:     ${trade.take_profit:,.2f}\n"
        f"R:R:    {trade.risk_reward:.2f}\n"
        f"Risk:   ${trade.risk_usd:,.2f} (1%)\n"
    )


def format_closed_trade(trade: SimTrade) -> str:
    if trade.status == "win":
        emoji = "✅ WIN"
    elif trade.status == "loss":
        emoji = "❌ LOSS"
    else:
        emoji = "➖ BREAKEVEN"

    return (
        f"{emoji} — Trade #{trade.id}\n"
        f"Entry ${trade.entry:,.2f} → Exit ${trade.exit_price:,.2f}\n"
        f"P&L: ${trade.pnl:+,.2f} | Mode: {trade.trade_mode}\n"
    )
