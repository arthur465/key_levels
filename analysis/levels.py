"""
analysis/levels.py
Compute all key price levels the system monitors for liquidity sweeps.

Levels tracked:
  PDH / PDL     — Previous Day High / Low
  PWH / PWL     — Previous Week High / Low
  Week Open     — Monday's opening price (current week)
  Session H/L   — Asia, London, NY session extremes (last 24h)
  Round Numbers — $5,000 increments near current price
"""
import pandas as pd
from dataclasses import dataclass, field
from typing import Dict, Optional
import config


@dataclass
class KeyLevels:
    pdh: float = 0.0
    pdl: float = 0.0
    pwh: float = 0.0
    pwl: float = 0.0
    week_open: float = 0.0
    session_highs: Dict[str, float] = field(default_factory=dict)
    session_lows:  Dict[str, float] = field(default_factory=dict)
    round_numbers: list = field(default_factory=list)

    def all_levels(self) -> Dict[str, float]:
        """All named levels as a flat dict."""
        out = {}
        if self.pdh:       out["PDH"]       = self.pdh
        if self.pdl:       out["PDL"]       = self.pdl
        if self.pwh:       out["PWH"]       = self.pwh
        if self.pwl:       out["PWL"]       = self.pwl
        if self.week_open: out["Week Open"] = self.week_open
        for s, v in self.session_highs.items():
            out[f"{s} Session High"] = v
        for s, v in self.session_lows.items():
            out[f"{s} Session Low"] = v
        return out

    def nearby(self, price: float, pct: float = None) -> Dict[str, float]:
        """Return levels within pct% of price (default = SWEEP_TOLERANCE_PCT)."""
        if pct is None:
            pct = config.SWEEP_TOLERANCE_PCT
        result = {}
        for name, lvl in self.all_levels().items():
            if lvl and abs(price - lvl) / price <= pct:
                result[name] = lvl
        return result

    def summary(self) -> str:
        lines = ["📐 KEY LEVELS:"]
        for name, lvl in self.all_levels().items():
            lines.append(f"  {name}: ${lvl:,.2f}")
        return "\n".join(lines)


def compute_levels(df_1h: pd.DataFrame, df_daily: pd.DataFrame) -> KeyLevels:
    """
    df_1h   : 1-hour OHLCV, UTC-indexed, at least 168 bars (1 week)
    df_daily: daily  OHLCV, UTC-indexed, at least 7 bars
    """
    levels = KeyLevels()

    # ── Previous Day High / Low ───────────────────────────────────────────────
    if len(df_daily) >= 2:
        prev_day = df_daily.iloc[-2]
        levels.pdh = float(prev_day["high"])
        levels.pdl = float(prev_day["low"])

    # ── Weekly levels from 1H data ────────────────────────────────────────────
    try:
        weekly = df_1h[["high", "low", "open", "close"]].resample(
            "W-MON", closed="left", label="left"
        ).agg({"high": "max", "low": "min", "open": "first", "close": "last"})

        if len(weekly) >= 2:
            pw = weekly.iloc[-2]
            levels.pwh       = float(pw["high"])
            levels.pwl       = float(pw["low"])
        if len(weekly) >= 1:
            levels.week_open = float(weekly.iloc[-1]["open"])
    except Exception as e:
        print(f"[levels] weekly resample error: {e}")

    # ── Session Highs / Lows (last 48h for recency) ───────────────────────────
    recent = df_1h.last("48h")
    session_map = {
        "Asia":     (0,  7),
        "London":   (7,  12),
        "New York": (12, 21),
    }
    for sess, (h0, h1) in session_map.items():
        mask = (recent.index.hour >= h0) & (recent.index.hour < h1)
        sdf  = recent[mask]
        if not sdf.empty:
            levels.session_highs[sess] = float(sdf["high"].max())
            levels.session_lows[sess]  = float(sdf["low"].min())

    # ── Round Numbers ($5k increments within ±$20k) ───────────────────────────
    if not df_daily.empty:
        price = float(df_daily.iloc[-1]["close"])
        base  = round(price / 5000) * 5000
        levels.round_numbers = [base + i * 5000 for i in range(-4, 5)]

    return levels
