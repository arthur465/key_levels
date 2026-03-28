"""
analysis/bias.py
Higher Timeframe (HTF) bias engine.

Bias is determined by fractal swing structure:
  Bullish: Higher Highs (HH) + Higher Lows (HL)
  Bearish: Lower Highs (LH) + Lower Lows (LL)
  Neutral: Mixed or insufficient data

We check both 1H and 4H and return alignment status + consensus.
"""
import pandas as pd
from typing import Literal
from analysis.structure import find_fractals
import config

BiasLabel = Literal["bullish", "bearish", "neutral"]


def _swing_bias(df: pd.DataFrame, lookback: int = None, fractal_n: int = 3) -> BiasLabel:
    """
    Determine bias from fractal swing structure on a DataFrame.
    fractal_n=3 gives cleaner swings on higher timeframes.
    """
    if lookback is None:
        lookback = config.STRUCTURE_LOOKBACK

    df_r = df.iloc[-lookback:]
    frac_highs, frac_lows = find_fractals(df_r, n=fractal_n)

    if len(frac_highs) < 2 or len(frac_lows) < 2:
        return "neutral"

    frac_highs = sorted(frac_highs, key=lambda f: f.idx)
    frac_lows  = sorted(frac_lows,  key=lambda f: f.idx)

    hh = frac_highs[-1].price > frac_highs[-2].price
    hl = frac_lows[-1].price  > frac_lows[-2].price
    lh = frac_highs[-1].price < frac_highs[-2].price
    ll = frac_lows[-1].price  < frac_lows[-2].price

    if hh and hl:
        return "bullish"
    elif lh and ll:
        return "bearish"
    return "neutral"


def get_htf_bias(df_1h: pd.DataFrame, df_4h: pd.DataFrame) -> dict:
    """
    Returns a dict with:
      '1h'        : BiasLabel
      '4h'        : BiasLabel
      'aligned'   : bool (both agree and non-neutral)
      'consensus' : BiasLabel (agreement result or 'neutral')
      'summary'   : human-readable string
    """
    bias_1h = _swing_bias(df_1h, fractal_n=3)
    bias_4h = _swing_bias(df_4h, fractal_n=3)

    aligned = (bias_1h == bias_4h) and (bias_1h != "neutral")
    consensus = bias_1h if aligned else "neutral"

    emoji_map = {"bullish": "🟢", "bearish": "🔴", "neutral": "⚪"}
    summary = (
        f"HTF Bias | 4H: {emoji_map[bias_4h]} {bias_4h.upper()} | "
        f"1H: {emoji_map[bias_1h]} {bias_1h.upper()} | "
        f"{'✅ ALIGNED' if aligned else '⚠️ MIXED'}"
    )

    return {
        "1h":       bias_1h,
        "4h":       bias_4h,
        "aligned":  aligned,
        "consensus": consensus,
        "summary":  summary,
    }
