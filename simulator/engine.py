"""
simulator/engine.py
Paper-trading simulator with persistent state.

Opens simulated trades on valid signals, resolves them when TP/SL is hit,
and tracks P&L, win rate, and performance by mode (day trade vs swing)
and by trend alignment (with-trend vs counter-trend).
"""
import json
import os
from dataclasses import dataclass, field, asdict
from typing import List, Optional
from datetime import datetime, timezone

from signals.detector import Signal
import config

STATE_FILE = "sim_state.json"


@dataclass
class SimTrade:
    id: int
    opened_at: str          # ISO timestamp
    timeframe: str
    trade_mode: str         # 'day_trade' | 'swing'
    direction: str          # 'long' | 'short'
    fib_mode: str           # 'reversal' | 'continuation'
    swept_level: str
    htf_trend: str          # 'with_trend' | 'counter_trend' | 'neutral'
    counter_trend: bool
    entry: float
    stop_loss: float
    take_profit: float
    risk_reward: float
    risk_usd: float
    # Resolved fields
    status: str = "open"    # 'open' | 'win' | 'loss'
    exit_price: float = 0.0
    pnl: float = 0.0
    closed_at: str = ""


class SimulatorEngine:
    def __init__(self, state_file: str = STATE_FILE):
        self.state_file   = state_file
        self.capital      = float(config.INITIAL_CAPITAL)
        self.peak_capital = float(config.INITIAL_CAPITAL)
        self.trades: List[SimTrade] = []
        self._counter = 0
        self._load()

    # ── Public Interface ──────────────────────────────────────────────────────

    def process_signal(self, signal: Signal) -> Optional[SimTrade]:
        """Open a new paper trade from a valid signal."""
        if not signal.valid:
            return None

        stop_dist = abs(signal.fib.entry - signal.fib.stop_loss)
        if stop_dist == 0:
            return None

        risk_usd = self.capital * config.RISK_PER_TRADE
        self._counter += 1

        trade = SimTrade(
            id=self._counter,
            opened_at=datetime.now(tz=timezone.utc).isoformat(),
            timeframe=signal.timeframe,
            trade_mode=signal.trade_mode,
            direction=signal.fib.direction,
            fib_mode=signal.fib.mode,
            swept_level=signal.swept_level_name,
            htf_trend=signal.htf_trend,
            counter_trend=(signal.htf_trend == "counter_trend"),
            entry=signal.fib.entry,
            stop_loss=signal.fib.stop_loss,
            take_profit=signal.fib.take_profit,
            risk_reward=signal.fib.risk_reward,
            risk_usd=risk_usd,
        )
        self.trades.append(trade)
        self._save()
        return trade

    def update_open_trades(self, current_price: float) -> List[SimTrade]:
        """Check all open trades and resolve any that hit TP or SL."""
        resolved = []
        for trade in self.trades:
            if trade.status != "open":
                continue

            if trade.direction == "long":
                hit_tp = current_price >= trade.take_profit
                hit_sl = current_price <= trade.stop_loss
            else:
                hit_tp = current_price <= trade.take_profit
                hit_sl = current_price >= trade.stop_loss

            if hit_tp or hit_sl:
                trade.exit_price = trade.take_profit if hit_tp else trade.stop_loss
                trade.closed_at  = datetime.now(tz=timezone.utc).isoformat()
                trade.pnl        = trade.risk_usd * trade.risk_reward if hit_tp else -trade.risk_usd
                trade.status     = "win" if hit_tp else "loss"
                self.capital    += trade.pnl
                if self.capital > self.peak_capital:
                    self.peak_capital = self.capital
                resolved.append(trade)

        if resolved:
            self._save()
        return resolved

    # ── Stats ─────────────────────────────────────────────────────────────────

    @property
    def closed_trades(self) -> List[SimTrade]:
        return [t for t in self.trades if t.status != "open"]

    @property
    def open_trades(self) -> List[SimTrade]:
        return [t for t in self.trades if t.status == "open"]

    @property
    def win_rate(self) -> float:
        c = self.closed_trades
        if not c:
            return 0.0
        return len([t for t in c if t.status == "win"]) / len(c) * 100

    @property
    def total_pnl(self) -> float:
        return sum(t.pnl for t in self.closed_trades)

    @property
    def drawdown_pct(self) -> float:
        if self.peak_capital == 0:
            return 0.0
        return (self.peak_capital - self.capital) / self.peak_capital * 100

    @property
    def return_pct(self) -> float:
        return (self.capital - config.INITIAL_CAPITAL) / config.INITIAL_CAPITAL * 100

    # ── Persistence ───────────────────────────────────────────────────────────

    def _save(self):
        state = {
            "capital":      self.capital,
            "peak_capital": self.peak_capital,
            "counter":      self._counter,
            "trades":       [asdict(t) for t in self.trades],
        }
        with open(self.state_file, "w") as f:
            json.dump(state, f, indent=2, default=str)

    def _load(self):
        if not os.path.exists(self.state_file):
            return
        try:
            with open(self.state_file) as f:
                state = json.load(f)
            self.capital       = float(state.get("capital",      config.INITIAL_CAPITAL))
            self.peak_capital  = float(state.get("peak_capital", self.capital))
            self._counter      = int(  state.get("counter",      0))
            for td in state.get("trades", []):
                # Only include fields that exist in the dataclass
                valid_keys = SimTrade.__dataclass_fields__.keys()
                filtered   = {k: v for k, v in td.items() if k in valid_keys}
                self.trades.append(SimTrade(**filtered))
            print(f"[sim] Loaded {len(self.trades)} trades, capital=${self.capital:,.2f}")
        except Exception as e:
            print(f"[sim] State load error: {e}")
