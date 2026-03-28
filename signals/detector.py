"""
signals/detector.py
Main signal orchestration pipeline.

Flow per scan:
  1. Fetch OHLCV for entry TF + 1H + 4H + Daily
  2. Compute key levels
  3. Check if current price is near any level (liquidity sweep candidate)
  4. Detect BOS on entry timeframe
  5. Build Fibonacci OTE setup
  6. Check if price is retracing into the 0.71 zone
  7. Get HTF bias (1H + 4H)
  8. Apply filters: ADX chop, session window, CVD
  9. Classify: with-trend vs counter-trend
  10. Return Signal
"""
import pandas as pd
from dataclasses import dataclass, field
from typing import Optional, List
from datetime import datetime, timezone

from data.fetcher import fetch_ohlcv, fetch_current_price
from analysis.levels import compute_levels, KeyLevels
from analysis.structure import detect_bos, BOSEvent
from analysis.fibonacci import build_fib_setup, FibSetup
from analysis.bias import get_htf_bias
from analysis.cvd_realtime import cvd_confirms_realtime, get_stream
from filters.chop import market_state
from filters.session import is_active_session, current_session, session_emoji
import config


@dataclass
class Signal:
    timestamp: datetime
    timeframe: str             # '15m' | '1h' | '4h'
    trade_mode: str            # 'day_trade' | 'swing'
    fib: FibSetup
    bias: dict
    swept_level_name: str
    swept_level_price: float
    htf_trend: str             # 'with_trend' | 'counter_trend' | 'neutral'
    adx: float
    cvd_ok: bool
    cvd_desc: str
    session: str
    valid: bool                # Passed all hard filters?
    reject_reasons: List[str] = field(default_factory=list)

    def label(self) -> str:
        arrow     = "🔴 SHORT" if self.fib.direction == "short" else "🟢 LONG"
        trend_tag = {
            "counter_trend": " ⚠️ COUNTER-TREND",
            "with_trend":    " ✅ WITH-TREND",
        }.get(self.htf_trend, "")
        status = "✅ VALID" if self.valid else f"❌ FILTERED ({', '.join(self.reject_reasons)})"
        return f"{arrow}{trend_tag} | {self.trade_mode.upper()} | {self.fib.mode.upper()} | {status}"

    def telegram_message(self) -> str:
        arrow  = "🔴 *SHORT*" if self.fib.direction == "short" else "🟢 *LONG*"
        ct_tag = ""
        if self.htf_trend == "counter_trend":
            ct_tag = "\n⚠️ *COUNTER-TREND — trade with caution*"
        elif self.htf_trend == "with_trend":
            ct_tag = "\n✅ *WITH-TREND*"

        reject_block = ""
        if self.reject_reasons:
            reject_block = f"\n\n❌ *Filtered:* {', '.join(self.reject_reasons)}"

        sess = session_emoji(self.session) + " " + self.session.replace("_", " ").title()

        return (
            f"{arrow} | {self.timeframe} | {self.fib.mode.upper()}"
            f"{ct_tag}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💧 Swept:  {self.swept_level_name} @ ${self.swept_level_price:,.2f}\n"
            f"📌 Entry:  ${self.fib.entry:,.2f}\n"
            f"🛑 SL:     ${self.fib.stop_loss:,.2f}\n"
            f"🎯 TP:     ${self.fib.take_profit:,.2f}\n"
            f"⚖️  R:R:    {self.fib.risk_reward:.2f}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{self.bias.get('summary', '')}\n"
            f"ADX: {self.adx:.1f} | {self.cvd_desc}\n"
            f"Session: {sess}"
            f"{reject_block}"
        )


def _find_nearest_level(price: float, levels: KeyLevels) -> Optional[tuple]:
    """Return (name, level_price, sweep_direction) for the nearest key level."""
    best_name, best_lvl, best_dist = None, None, float("inf")

    for name, lvl in levels.all_levels().items():
        if not lvl:
            continue
        dist = abs(price - lvl) / price
        if dist <= config.SWEEP_TOLERANCE_PCT and dist < best_dist:
            best_name = name
            best_lvl  = lvl
            best_dist = dist

    if best_name is None:
        return None

    # Determine sweep direction from level name
    is_high = any(k in best_name for k in ("High", "PDH", "PWH"))
    return best_name, best_lvl, ("high" if is_high else "low")


def _classify_trend(direction: str, bias: dict) -> str:
    consensus = bias.get("consensus", "neutral")
    if consensus == "neutral":
        return "neutral"
    if (direction == "short" and consensus == "bearish") or \
       (direction == "long"  and consensus == "bullish"):
        return "with_trend"
    return "counter_trend"


def scan_for_signals(timeframe: str = "15m", trade_mode: str = "day_trade") -> Optional[Signal]:
    """
    Run a complete signal scan for the given timeframe.
    Returns a Signal (valid or filtered) or None if no setup exists.
    """
    try:
        # ── Fetch data ────────────────────────────────────────────────────────
        df_entry = fetch_ohlcv(timeframe, limit=150)
        df_1h    = fetch_ohlcv("1h",      limit=200)
        df_4h    = fetch_ohlcv("4h",      limit=200)
        df_daily = fetch_ohlcv("1d",      limit=30)
        price    = fetch_current_price()

        # ── Key levels & sweep check ──────────────────────────────────────────
        levels = compute_levels(df_1h, df_daily)
        sweep  = _find_nearest_level(price, levels)
        if sweep is None:
            return None

        level_name, level_price, sweep_dir = sweep
        print(f"  [{timeframe}] Price ${price:,.2f} near {level_name} @ ${level_price:,.2f}")

        # ── Break of Structure ────────────────────────────────────────────────
        bos = detect_bos(df_entry, level_price, sweep_dir)
        if bos is None:
            return None

        print(f"  [{timeframe}] BOS detected: {bos.direction} ({bos.mode})")

        # ── Fibonacci OTE setup ───────────────────────────────────────────────
        fib_dir = "short" if bos.direction == "bearish" else "long"
        sh = bos.swing_point if fib_dir == "short" else bos.bos_price
        sl = bos.bos_price   if fib_dir == "short" else bos.swing_point

        fib = build_fib_setup(sh, sl, fib_dir, bos.mode)
        if fib is None:
            return None

        # ── Wait for price to retrace into OTE zone ───────────────────────────
        if not fib.is_price_in_ote(price):
            return None

        print(f"  [{timeframe}] Price in OTE zone @ ${fib.ote_level:,.2f}")

        # ── HTF Bias ──────────────────────────────────────────────────────────
        bias      = get_htf_bias(df_1h, df_4h)
        htf_trend = _classify_trend(fib_dir, bias)

        # ── Filters ───────────────────────────────────────────────────────────
        mkt    = market_state(df_entry)
        sess   = current_session()
        cvd_ok = cvd_confirms_realtime(fib_dir)
        snap   = get_stream().last_snapshot()
        cvd_d  = snap.summary() if snap else "CVD: no data yet"

        rejects = []
        if not mkt["trending"]:
            rejects.append(f"ADX {mkt['adx']} < {config.ADX_THRESHOLD} (choppy)")
        if not is_active_session():
            rejects.append(f"off-session ({sess})")
        if config.USE_CVD and not cvd_ok:
            rejects.append("CVD not confirming")

        return Signal(
            timestamp=datetime.now(tz=timezone.utc),
            timeframe=timeframe,
            trade_mode=trade_mode,
            fib=fib,
            bias=bias,
            swept_level_name=level_name,
            swept_level_price=level_price,
            htf_trend=htf_trend,
            adx=mkt["adx"],
            cvd_ok=cvd_ok,
            cvd_desc=cvd_d,
            session=sess,
            valid=len(rejects) == 0,
            reject_reasons=rejects,
        )

    except Exception as e:
        print(f"  [detector/{timeframe}] Error: {e}")
        return None
