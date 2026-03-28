"""
backtest/engine.py
Walk-forward backtest over historical OHLCV data.

No lookahead bias — at bar[i] the engine only sees bar[0..i].
Runs the full signal pipeline on each bar and resolves trades
when TP/SL is touched on subsequent bars.

Usage:
    python -m backtest.run --days 180
"""
import pandas as pd
import numpy as np
from dataclasses import dataclass, field
from typing import List, Optional, Dict
from datetime import datetime, timezone

from analysis.levels import compute_levels, KeyLevels
from analysis.structure import detect_bos
from analysis.fibonacci import build_fib_setup, FibSetup
from analysis.bias import get_htf_bias
from analysis.cvd import cvd_confirms
from filters.chop import market_state
import config


# ─────────────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class BacktestTrade:
    bar_idx:       int
    timestamp:     pd.Timestamp
    timeframe:     str
    trade_mode:    str
    direction:     str          # 'long' | 'short'
    fib_mode:      str          # 'reversal' | 'continuation'
    swept_level:   str
    htf_trend:     str          # 'with_trend' | 'counter_trend' | 'neutral'
    entry:         float
    stop_loss:     float
    take_profit:   float
    risk_reward:   float
    risk_usd:      float
    filters_passed: bool
    reject_reasons: List[str]
    # Resolved
    status:        str   = "open"   # 'win' | 'loss' | 'open'
    exit_price:    float = 0.0
    exit_bar:      int   = 0
    exit_ts:       Optional[pd.Timestamp] = None
    pnl:           float = 0.0
    bars_held:     int   = 0
    session:       str   = ""


@dataclass
class BacktestResult:
    trades:        List[BacktestTrade] = field(default_factory=list)
    capital:       float = config.INITIAL_CAPITAL
    peak_capital:  float = config.INITIAL_CAPITAL
    equity_curve:  List[float] = field(default_factory=list)

    # ── Aggregate stats ──────────────────────────────────────────────────────

    def closed(self) -> List[BacktestTrade]:
        return [t for t in self.trades if t.status != "open"]

    def wins(self)   -> List[BacktestTrade]:
        return [t for t in self.closed() if t.status == "win"]

    def losses(self) -> List[BacktestTrade]:
        return [t for t in self.closed() if t.status == "loss"]

    def win_rate(self) -> float:
        c = self.closed()
        return len(self.wins()) / len(c) * 100 if c else 0.0

    def total_pnl(self) -> float:
        return sum(t.pnl for t in self.closed())

    def return_pct(self) -> float:
        return (self.capital - config.INITIAL_CAPITAL) / config.INITIAL_CAPITAL * 100

    def max_drawdown(self) -> float:
        if not self.equity_curve:
            return 0.0
        curve  = np.array(self.equity_curve)
        peaks  = np.maximum.accumulate(curve)
        dds    = (peaks - curve) / peaks * 100
        return float(dds.max())

    def profit_factor(self) -> float:
        gross_win  = sum(t.pnl for t in self.wins())
        gross_loss = abs(sum(t.pnl for t in self.losses()))
        return gross_win / gross_loss if gross_loss > 0 else float("inf")

    def avg_rr(self) -> float:
        c = self.closed()
        return sum(t.risk_reward for t in c) / len(c) if c else 0.0

    def expectancy(self) -> float:
        """Expected $ per trade."""
        c = self.closed()
        return sum(t.pnl for t in c) / len(c) if c else 0.0

    def sharpe(self) -> float:
        """Simplified Sharpe on trade P&L."""
        pnls = [t.pnl for t in self.closed()]
        if len(pnls) < 2:
            return 0.0
        arr  = np.array(pnls)
        return float(arr.mean() / arr.std()) if arr.std() > 0 else 0.0

    def max_consecutive_losses(self) -> int:
        best = cur = 0
        for t in self.closed():
            cur = cur + 1 if t.status == "loss" else 0
            best = max(best, cur)
        return best

    def _filter_stats(self, subset: List[BacktestTrade]) -> dict:
        if not subset:
            return {"count": 0, "win_rate": 0, "pnl": 0}
        wins = [t for t in subset if t.status == "win"]
        return {
            "count":    len(subset),
            "win_rate": len(wins) / len(subset) * 100,
            "pnl":      sum(t.pnl for t in subset),
        }

    def breakdown(self) -> dict:
        c = self.closed()
        return {
            "day_trade":      self._filter_stats([t for t in c if t.trade_mode == "day_trade"]),
            "swing":          self._filter_stats([t for t in c if t.trade_mode == "swing"]),
            "with_trend":     self._filter_stats([t for t in c if t.htf_trend == "with_trend"]),
            "counter_trend":  self._filter_stats([t for t in c if t.htf_trend == "counter_trend"]),
            "reversal":       self._filter_stats([t for t in c if t.fib_mode == "reversal"]),
            "continuation":   self._filter_stats([t for t in c if t.fib_mode == "continuation"]),
            "long":           self._filter_stats([t for t in c if t.direction == "long"]),
            "short":          self._filter_stats([t for t in c if t.direction == "short"]),
            "london":         self._filter_stats([t for t in c if t.session == "london"]),
            "new_york":       self._filter_stats([t for t in c if t.session == "new_york"]),
            "filtered_out":   len([t for t in self.trades if not t.filters_passed]),
        }


# ─────────────────────────────────────────────────────────────────────────────
# Core engine
# ─────────────────────────────────────────────────────────────────────────────

class BacktestEngine:

    # Minimum bars of history needed before the first scan
    WARMUP_BARS = 100

    def __init__(self, capital: float = config.INITIAL_CAPITAL):
        self.initial_capital = capital

    def run(
        self,
        df_15m:  pd.DataFrame,
        df_1h:   pd.DataFrame,
        df_4h:   pd.DataFrame,
        df_daily: pd.DataFrame,
        scan_configs: List[dict] = None,
    ) -> BacktestResult:
        """
        Walk forward through df_15m bar by bar.
        df_1h, df_4h, df_daily are sliced at each step to avoid lookahead.
        """
        if scan_configs is None:
            scan_configs = [
                {"timeframe": "15m", "trade_mode": "day_trade",  "df": df_15m},
                {"timeframe": "1h",  "trade_mode": "swing",      "df": df_1h},
                {"timeframe": "4h",  "trade_mode": "swing",      "df": df_4h},
            ]

        result = BacktestResult(capital=float(self.initial_capital),
                                peak_capital=float(self.initial_capital))
        open_trades: List[BacktestTrade] = []
        seen_setups: set = set()

        total_bars = len(df_15m)
        print(f"[backtest] Running {total_bars} bars ({df_15m.index[0]} → {df_15m.index[-1]})")

        for i in range(self.WARMUP_BARS, total_bars):
            current_bar = df_15m.iloc[i]
            ts          = df_15m.index[i]
            price       = float(current_bar["close"])

            # ── 1. Resolve open trades ────────────────────────────────────────
            still_open = []
            for trade in open_trades:
                resolved = self._try_resolve(trade, current_bar, i)
                if resolved:
                    result.capital += trade.pnl
                    if result.capital > result.peak_capital:
                        result.peak_capital = result.capital
                    result.equity_curve.append(result.capital)
                else:
                    still_open.append(trade)
            open_trades = still_open

            # ── 2. Scan for new signals every N bars to save compute ──────────
            if i % 5 != 0:  # Scan every 5 bars (5-min equivalent)
                continue

            # ── 3. Build sliced history (no lookahead) ────────────────────────
            slice_15m  = df_15m.iloc[:i+1]
            slice_1h   = df_1h[df_1h.index <= ts]
            slice_4h   = df_4h[df_4h.index <= ts]
            slice_daily = df_daily[df_daily.index <= ts]

            if len(slice_1h) < 20 or len(slice_4h) < 10 or len(slice_daily) < 2:
                continue

            # ── 4. Key levels ─────────────────────────────────────────────────
            try:
                levels = compute_levels(slice_1h, slice_daily)
            except Exception:
                continue

            # ── 5. Scan each timeframe ────────────────────────────────────────
            for cfg in scan_configs:
                tf   = cfg["timeframe"]
                mode = cfg["trade_mode"]
                df_s = cfg["df"]

                slice_tf = df_s[df_s.index <= ts]
                if len(slice_tf) < 60:
                    continue

                signal = self._scan_bar(
                    slice_tf, slice_1h, slice_4h, levels, price, ts, tf, mode
                )
                if signal is None:
                    continue

                dedup = (signal["direction"], signal["swept_level"], tf)
                if dedup in seen_setups:
                    continue
                seen_setups.add(dedup)

                # Build trade
                risk_usd = result.capital * config.RISK_PER_TRADE
                trade    = BacktestTrade(
                    bar_idx        = i,
                    timestamp      = ts,
                    timeframe      = tf,
                    trade_mode     = mode,
                    direction      = signal["direction"],
                    fib_mode       = signal["fib_mode"],
                    swept_level    = signal["swept_level"],
                    htf_trend      = signal["htf_trend"],
                    entry          = signal["entry"],
                    stop_loss      = signal["stop_loss"],
                    take_profit    = signal["take_profit"],
                    risk_reward    = signal["risk_reward"],
                    risk_usd       = risk_usd,
                    filters_passed = signal["valid"],
                    reject_reasons = signal["rejects"],
                    session        = self._session(ts),
                )
                result.trades.append(trade)
                if signal["valid"]:
                    open_trades.append(trade)

            # Clear dedup set periodically
            if i % 500 == 0:
                seen_setups.clear()
                pct = i / total_bars * 100
                print(f"[backtest] {pct:.0f}% — capital ${result.capital:,.0f} | trades: {len(result.trades)}")

        # ── Close any remaining open trades at last price ────────────────────
        last_price = float(df_15m.iloc[-1]["close"])
        for trade in open_trades:
            trade.status     = "open"  # mark as unresolved
            trade.exit_price = last_price

        print(f"[backtest] Done. {len(result.trades)} signals, {len(result.closed())} closed.")
        return result

    # ─────────────────────────────────────────────────────────────────────────
    # Internals
    # ─────────────────────────────────────────────────────────────────────────

    def _try_resolve(self, trade: BacktestTrade, bar: pd.Series, bar_idx: int) -> bool:
        """Check if TP or SL was hit on this bar. Returns True if resolved."""
        hi = float(bar["high"])
        lo = float(bar["low"])

        if trade.direction == "long":
            hit_tp = hi >= trade.take_profit
            hit_sl = lo <= trade.stop_loss
        else:
            hit_tp = lo <= trade.take_profit
            hit_sl = hi >= trade.stop_loss

        if hit_tp or hit_sl:
            trade.exit_price = trade.take_profit if hit_tp else trade.stop_loss
            trade.exit_bar   = bar_idx
            trade.bars_held  = bar_idx - trade.bar_idx
            trade.pnl        = trade.risk_usd * trade.risk_reward if hit_tp else -trade.risk_usd
            trade.status     = "win" if hit_tp else "loss"
            return True

        return False

    def _scan_bar(
        self,
        df_entry:  pd.DataFrame,
        df_1h:     pd.DataFrame,
        df_4h:     pd.DataFrame,
        levels:    KeyLevels,
        price:     float,
        ts:        pd.Timestamp,
        timeframe: str,
        trade_mode: str,
    ) -> Optional[dict]:
        """Run full pipeline on a single historical bar."""
        try:
            # Nearest level check
            sweep = self._find_sweep(price, levels)
            if sweep is None:
                return None

            level_name, level_price, sweep_dir = sweep

            # BOS
            bos = detect_bos(df_entry, level_price, sweep_dir)
            if bos is None:
                return None

            # Fib
            fib_dir = "short" if bos.direction == "bearish" else "long"
            sh = bos.swing_point if fib_dir == "short" else bos.bos_price
            sl = bos.bos_price   if fib_dir == "short" else bos.swing_point
            fib = build_fib_setup(sh, sl, fib_dir, bos.mode)
            if fib is None:
                return None

            # OTE check
            if not fib.is_price_in_ote(price):
                return None

            # HTF bias
            bias      = get_htf_bias(df_1h, df_4h)
            consensus = bias.get("consensus", "neutral")
            if (fib_dir == "short" and consensus == "bearish") or \
               (fib_dir == "long"  and consensus == "bullish"):
                htf_trend = "with_trend"
            elif consensus == "neutral":
                htf_trend = "neutral"
            else:
                htf_trend = "counter_trend"

            # Filters
            rejects = []
            mkt = market_state(df_entry)
            if not mkt["trending"]:
                rejects.append(f"ADX {mkt['adx']:.0f}")
            if config.USE_CVD and not cvd_confirms(fib_dir, df_entry):
                rejects.append("CVD")

            return {
                "direction":    fib_dir,
                "fib_mode":     bos.mode,
                "swept_level":  level_name,
                "htf_trend":    htf_trend,
                "entry":        fib.entry,
                "stop_loss":    fib.stop_loss,
                "take_profit":  fib.take_profit,
                "risk_reward":  fib.risk_reward,
                "valid":        len(rejects) == 0,
                "rejects":      rejects,
            }

        except Exception:
            return None

    def _find_sweep(self, price: float, levels: KeyLevels):
        best_name, best_lvl, best_dist = None, None, float("inf")
        for name, lvl in levels.all_levels().items():
            if not lvl:
                continue
            dist = abs(price - lvl) / price
            if dist <= config.SWEEP_TOLERANCE_PCT and dist < best_dist:
                best_name, best_lvl, best_dist = name, lvl, dist
        if best_name is None:
            return None
        is_high = any(k in best_name for k in ("High", "PDH", "PWH"))
        return best_name, best_lvl, ("high" if is_high else "low")

    def _session(self, ts: pd.Timestamp) -> str:
        h = ts.hour
        for sess, (h0, h1) in config.ACTIVE_SESSIONS.items():
            if h0 <= h < h1:
                return sess
        return "off_hours"
