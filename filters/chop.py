"""
filters/chop.py
ADX-based ranging market filter.
ADX < ADX_THRESHOLD  →  market is choppy/ranging  →  suppress signals.
"""
import pandas as pd
import numpy as np
import config


def compute_adx(df: pd.DataFrame, period: int = None) -> pd.Series:
    """
    Pure-pandas ADX implementation (no external TA library required).
    Uses Wilder exponential smoothing.
    """
    if period is None:
        period = config.ADX_PERIOD

    high  = df["high"]
    low   = df["low"]
    close = df["close"]

    prev_close = close.shift(1)

    # True Range
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)

    # Raw Directional Movement
    up_move   = high.diff()
    down_move = -low.diff()

    dm_plus  = up_move.where(  (up_move > down_move)   & (up_move > 0),   0.0)
    dm_minus = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

    alpha = 1.0 / period

    # Wilder smoothing (EWM with adjust=False approximates Wilder)
    atr      = tr.ewm(alpha=alpha, adjust=False).mean()
    di_plus  = 100 * dm_plus.ewm( alpha=alpha, adjust=False).mean() / atr.replace(0, np.nan)
    di_minus = 100 * dm_minus.ewm(alpha=alpha, adjust=False).mean() / atr.replace(0, np.nan)

    dx  = 100 * (di_plus - di_minus).abs() / (di_plus + di_minus).replace(0, np.nan)
    adx = dx.ewm(alpha=alpha, adjust=False).mean()

    return adx


def market_state(df: pd.DataFrame) -> dict:
    """
    Returns:
      adx      : latest ADX value
      trending : True if ADX >= threshold
      state    : 'trending' | 'choppy/ranging'
    """
    adx    = compute_adx(df)
    latest = float(adx.iloc[-1])
    trending = latest >= config.ADX_THRESHOLD

    return {
        "adx":      round(latest, 1),
        "trending": trending,
        "state":    "trending" if trending else "choppy/ranging",
    }


def is_trending(df: pd.DataFrame) -> bool:
    return market_state(df)["trending"]
