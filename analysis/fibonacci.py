"""
analysis/fibonacci.py
Fibonacci Optimal Trade Entry (OTE) engine.

For BEARISH (short reversal):
  Swing High = fractal high (top of the move after sweep)
  Swing Low  = BOS candle close
  OTE        = Swing High - range × 0.71   ← price retraces UP into this after BOS
  Entry      = 0.71 level
  SL         = above Swing High + buffer
  TP         = Swing Low (or capped at MAX_RR)

For BULLISH (long reversal):
  Symmetric inverse of the above.

For CONTINUATION trades, Fib anchored to the new swing forming.
"""
from dataclasses import dataclass
from typing import Optional
import config


@dataclass
class FibSetup:
    direction: str        # 'long' | 'short'
    swing_high: float
    swing_low: float
    range_size: float     # swing_high - swing_low
    ote_level: float      # 0.71 retracement
    ote_optional: float   # 0.72 retracement
    entry: float
    stop_loss: float
    take_profit: float
    risk_reward: float
    mode: str             # 'reversal' | 'continuation'

    def is_price_in_ote(self, price: float, tolerance_pct: float = 0.002) -> bool:
        """True if price is within tolerance% of the OTE level."""
        return abs(price - self.ote_level) / self.ote_level <= tolerance_pct

    def stop_distance(self) -> float:
        return abs(self.entry - self.stop_loss)

    def summary(self) -> str:
        arrow = "🔴 SHORT" if self.direction == "short" else "🟢 LONG"
        return (
            f"{arrow} ({self.mode.upper()})\n"
            f"  Entry:  ${self.entry:,.2f}\n"
            f"  SL:     ${self.stop_loss:,.2f}\n"
            f"  TP:     ${self.take_profit:,.2f}\n"
            f"  R:R:    {self.risk_reward:.2f}\n"
            f"  OTE:    ${self.ote_level:,.2f} (0.71 Fib)"
        )


def build_fib_setup(
    swing_high: float,
    swing_low: float,
    direction: str,     # 'long' | 'short'
    mode: str = "reversal",
) -> Optional[FibSetup]:
    """
    Construct a full Fibonacci OTE setup from swing points.
    Returns None if the setup doesn't meet minimum R:R.
    """
    if swing_high <= swing_low:
        return None

    rng = swing_high - swing_low

    if direction == "short":
        # OTE = price retracing UP from the BOS low toward the swing high
        ote     = swing_high - rng * config.FIB_OTE       # 0.71 from the top
        ote_opt = swing_high - rng * config.FIB_OTE_OPT   # 0.72

        entry   = ote
        sl      = swing_high * (1 + config.FIB_STOP_BUFFER)
        tp_raw  = swing_low

        if entry <= sl:
            return None

        raw_rr = (entry - tp_raw) / (sl - entry)
        if raw_rr > config.MAX_RR:
            tp = entry - (sl - entry) * config.MAX_RR
        else:
            tp = tp_raw

        rr = (entry - tp) / (sl - entry)

    else:   # long
        # OTE = price retracing DOWN from the BOS high toward the swing low
        ote     = swing_low + rng * config.FIB_OTE
        ote_opt = swing_low + rng * config.FIB_OTE_OPT

        entry   = ote
        sl      = swing_low * (1 - config.FIB_STOP_BUFFER)
        tp_raw  = swing_high

        if entry >= sl:
            return None

        raw_rr = (tp_raw - entry) / (entry - sl)
        if raw_rr > config.MAX_RR:
            tp = entry + (entry - sl) * config.MAX_RR
        else:
            tp = tp_raw

        rr = (tp - entry) / (entry - sl)

    if rr < config.MIN_RR:
        return None

    return FibSetup(
        direction=direction,
        swing_high=swing_high,
        swing_low=swing_low,
        range_size=rng,
        ote_level=ote,
        ote_optional=ote_opt,
        entry=entry,
        stop_loss=sl,
        take_profit=tp,
        risk_reward=round(rr, 2),
        mode=mode,
    )
