"""
analysis/cvd.py
Cumulative Volume Delta (CVD) approximation from OHLCV.

Since we use public REST endpoints (no tick data), we estimate delta:
  Bullish candle (close > open)  →  +volume  (buying pressure)
  Bearish candle (close < open)  →  -volume  (selling pressure)
  Doji (close == open)           →   0

CVD trend (rising / falling) is used to confirm or reject signals:
  Going SHORT → CVD should be falling (sellers dominating)
  Going LONG  → CVD should be rising  (buyers dominating)
"""
import pandas as pd
import numpy as np
import config


def compute_cvd(df: pd.DataFrame) -> pd.Series:
    """Return raw CVD series (cumulative estimated delta)."""
    delta = pd.Series(0.0, index=df.index)
    bull  = df["close"] > df["open"]
    bear  = df["close"] < df["open"]
    delta[bull] =  df.loc[bull, "volume"]
    delta[bear] = -df.loc[bear, "volume"]
    return delta.cumsum()


def cvd_slope(df: pd.DataFrame, lookback: int = None) -> float:
    """Return linear regression slope of CVD over the last N candles."""
    if lookback is None:
        lookback = config.CVD_LOOKBACK
    cvd    = compute_cvd(df).iloc[-lookback:]
    x      = np.arange(len(cvd))
    slope  = np.polyfit(x, cvd.values, 1)[0]
    return float(slope)


def cvd_trend(df: pd.DataFrame, lookback: int = None) -> str:
    """'rising' | 'falling' | 'neutral' over the last N candles."""
    slope = cvd_slope(df, lookback)
    # Normalize by recent CVD range to avoid noise threshold issues
    cvd   = compute_cvd(df).iloc[-(lookback or config.CVD_LOOKBACK):]
    rng   = float(cvd.max() - cvd.min())
    if rng == 0:
        return "neutral"
    norm  = slope / rng * 100   # slope as % of range per bar

    if norm > 1.5:
        return "rising"
    elif norm < -1.5:
        return "falling"
    return "neutral"


def cvd_confirms(direction: str, df: pd.DataFrame) -> bool:
    """
    Returns True if CVD direction confirms the trade.
    If USE_CVD is disabled in config, always returns True.
    """
    if not config.USE_CVD:
        return True

    trend = cvd_trend(df)
    if direction == "short":
        return trend == "falling"
    elif direction == "long":
        return trend == "rising"
    return False


def cvd_summary(df: pd.DataFrame) -> str:
    trend = cvd_trend(df)
    slope = cvd_slope(df)
    emoji = {"rising": "📈", "falling": "📉", "neutral": "➡️"}.get(trend, "")
    return f"CVD: {emoji} {trend.upper()} (slope: {slope:+.1f})"
