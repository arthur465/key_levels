"""
analysis/structure.py
Fractal pivot detection and Break of Structure (BOS) engine.

── Fractal Definition ────────────────────────────────────────────────────────
  Fractal High: bar[i].high is the highest among N bars on each side.
  Fractal Low : bar[i].low  is the lowest  among N bars on each side.

── BOS Logic (Reversal Short) ───────────────────────────────────────────────
  1. Price sweeps above a resistance level (PDH / PWH / session high)
  2. A fractal high forms — this is our swing high (Fib anchor top)
  3. A higher low (fractal low) forms after the swing high
  4. A bearish candle closes BELOW that fractal low = BOS
  → Anchor Fib from swing high → BOS candle close
  → Look for retracement to 0.71 for short entry

── BOS Logic (Reversal Long) ────────────────────────────────────────────────
  Symmetric inverse of the above.

── BOS Logic (Continuation) ─────────────────────────────────────────────────
  If subsequent fractal highs/lows keep going in the same direction
  after a sweep (no reversal BOS), we label it continuation.
"""
import pandas as pd
import numpy as np
from dataclasses import dataclass
from typing import Optional, List
import config


@dataclass
class Fractal:
    idx: int
    timestamp: pd.Timestamp
    price: float
    kind: str   # 'high' | 'low'


@dataclass
class BOSEvent:
    direction: str          # 'bearish' | 'bullish'
    swing_point: float      # The fractal high (for short) or fractal low (for long)
    bos_price: float        # Close of the BOS candle
    bos_ts: pd.Timestamp
    broken_fractal: float   # The structure level that was broken
    mode: str               # 'reversal' | 'continuation'


def find_fractals(df: pd.DataFrame, n: int = None) -> tuple:
    """Return (List[Fractal highs], List[Fractal lows])."""
    if n is None:
        n = config.FRACTAL_N

    highs, lows = [], []
    length = len(df)

    for i in range(n, length - n):
        hi = df["high"].iloc[i]
        lo = df["low"].iloc[i]
        ts = df.index[i]

        if all(hi > df["high"].iloc[i - j] for j in range(1, n + 1)) and \
           all(hi > df["high"].iloc[i + j] for j in range(1, n + 1)):
            highs.append(Fractal(i, ts, hi, "high"))

        if all(lo < df["low"].iloc[i - j] for j in range(1, n + 1)) and \
           all(lo < df["low"].iloc[i + j] for j in range(1, n + 1)):
            lows.append(Fractal(i, ts, lo, "low"))

    return highs, lows


def is_bearish_engulfing(df: pd.DataFrame, i: int) -> bool:
    """True if candle i is a bearish engulfing of candle i-1."""
    if i < 1 or i >= len(df):
        return False
    prev = df.iloc[i - 1]
    curr = df.iloc[i]
    return (
        curr["close"] < curr["open"]
        and prev["close"] >= prev["open"]
        and curr["open"] >= prev["close"]
        and curr["close"] <= prev["open"]
    )


def is_bullish_engulfing(df: pd.DataFrame, i: int) -> bool:
    if i < 1 or i >= len(df):
        return False
    prev = df.iloc[i - 1]
    curr = df.iloc[i]
    return (
        curr["close"] > curr["open"]
        and prev["close"] <= prev["open"]
        and curr["open"] <= prev["close"]
        and curr["close"] >= prev["open"]
    )


def detect_bos(
    df: pd.DataFrame,
    swept_level: float,
    sweep_direction: str,     # 'high' → swept above resistance | 'low' → swept below support
    lookback: int = None,
) -> Optional[BOSEvent]:
    """
    After a liquidity sweep, scan the most recent `lookback` candles for a BOS.
    Returns a BOSEvent or None.
    """
    if lookback is None:
        lookback = config.BOS_LOOKBACK

    df_s = df.iloc[-lookback:].copy()
    df_s = df_s.reset_index(drop=False)     # keep timestamp column accessible

    frac_highs, frac_lows = find_fractals(df_s, n=config.FRACTAL_N)

    # ── CASE: swept above resistance → look for BEARISH reversal or BULLISH continuation
    if sweep_direction == "high":

        # ── Reversal SHORT ──────────────────────────────────────────────────
        if frac_highs and frac_lows:
            # Swing high = highest fractal high in window
            swing_high = max(frac_highs, key=lambda f: f.price)

            # Fractal lows formed AFTER the swing high (these are the higher lows)
            post_lows = [f for f in frac_lows if f.idx > swing_high.idx]

            if post_lows:
                structure_low = post_lows[-1]   # most recent higher low

                # Scan candles after that fractal low for a close below it
                for i in range(structure_low.idx + 1, len(df_s)):
                    c = df_s.iloc[i]
                    if float(c["close"]) < structure_low.price:
                        return BOSEvent(
                            direction="bearish",
                            swing_point=swing_high.price,
                            bos_price=float(c["close"]),
                            bos_ts=c["timestamp"],
                            broken_fractal=structure_low.price,
                            mode="reversal",
                        )

        # ── Continuation LONG ──────────────────────────────────────────────
        # Price swept high and keeps making higher highs — bullish continuation
        if len(frac_highs) >= 2:
            frac_highs_sorted = sorted(frac_highs, key=lambda f: f.idx)
            if frac_highs_sorted[-1].price > frac_highs_sorted[-2].price:
                if frac_lows:
                    last_low = sorted(frac_lows, key=lambda f: f.idx)[-1]
                    c = df_s.iloc[frac_highs_sorted[-1].idx]
                    return BOSEvent(
                        direction="bullish",
                        swing_point=last_low.price,
                        bos_price=float(c["close"]),
                        bos_ts=c["timestamp"],
                        broken_fractal=frac_highs_sorted[-2].price,
                        mode="continuation",
                    )

    # ── CASE: swept below support → look for BULLISH reversal or BEARISH continuation
    else:
        # ── Reversal LONG ───────────────────────────────────────────────────
        if frac_lows and frac_highs:
            swing_low = min(frac_lows, key=lambda f: f.price)

            post_highs = [f for f in frac_highs if f.idx > swing_low.idx]

            if post_highs:
                structure_high = post_highs[-1]

                for i in range(structure_high.idx + 1, len(df_s)):
                    c = df_s.iloc[i]
                    if float(c["close"]) > structure_high.price:
                        return BOSEvent(
                            direction="bullish",
                            swing_point=swing_low.price,
                            bos_price=float(c["close"]),
                            bos_ts=c["timestamp"],
                            broken_fractal=structure_high.price,
                            mode="reversal",
                        )

        # ── Continuation BEARISH ────────────────────────────────────────────
        if len(frac_lows) >= 2:
            frac_lows_sorted = sorted(frac_lows, key=lambda f: f.idx)
            if frac_lows_sorted[-1].price < frac_lows_sorted[-2].price:
                if frac_highs:
                    last_high = sorted(frac_highs, key=lambda f: f.idx)[-1]
                    c = df_s.iloc[frac_lows_sorted[-1].idx]
                    return BOSEvent(
                        direction="bearish",
                        swing_point=last_high.price,
                        bos_price=float(c["close"]),
                        bos_ts=c["timestamp"],
                        broken_fractal=frac_lows_sorted[-2].price,
                        mode="continuation",
                    )

    return None
