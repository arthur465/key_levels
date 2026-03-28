"""
data/fetcher.py
Fetch OHLCV and ticker data from OKX → KuCoin → Gate.io (fallback chain).
"""
import ccxt
import pandas as pd
from typing import Optional
import config

_exchange_cache: dict = {}


def _get_exchange(name: str) -> ccxt.Exchange:
    if name in _exchange_cache:
        return _exchange_cache[name]

    cls_map = {
        "okx":    ccxt.okx,
        "kucoin": ccxt.kucoin,
        "gateio": ccxt.gateio,
    }
    cls = cls_map.get(name)
    if cls is None:
        raise ValueError(f"Unsupported exchange: {name}")

    params: dict = {"enableRateLimit": True}
    if name == "okx" and config.OKX_API_KEY:
        params.update({
            "apiKey":   config.OKX_API_KEY,
            "secret":   config.OKX_SECRET,
            "password": config.OKX_PASSWORD,
        })

    ex = cls(params)
    _exchange_cache[name] = ex
    return ex


def fetch_ohlcv(
    timeframe: str,
    limit: int = 300,
    exchange_name: Optional[str] = None,
) -> pd.DataFrame:
    """
    Return OHLCV DataFrame (UTC-indexed) from the best available exchange.
    Falls back through config.EXCHANGES on error.
    """
    exchanges_to_try = [exchange_name] if exchange_name else config.EXCHANGES

    for name in exchanges_to_try:
        try:
            ex = _get_exchange(name)
            raw = ex.fetch_ohlcv(config.SYMBOL, timeframe, limit=limit)
            df = pd.DataFrame(
                raw, columns=["timestamp", "open", "high", "low", "close", "volume"]
            )
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
            df.set_index("timestamp", inplace=True)
            df = df.astype(float)
            print(f"[fetcher] {timeframe} OK from {name} ({len(df)} bars)")
            return df
        except Exception as e:
            print(f"[fetcher] {name} failed ({timeframe}): {e}")

    raise RuntimeError(f"All exchanges failed for timeframe={timeframe}")


def fetch_current_price() -> float:
    """Get latest BTC mid-price from first available exchange."""
    for name in config.EXCHANGES:
        try:
            ex = _get_exchange(name)
            ticker = ex.fetch_ticker(config.SYMBOL)
            return float(ticker["last"])
        except Exception as e:
            print(f"[fetcher] ticker error on {name}: {e}")
    raise RuntimeError("Cannot fetch current price from any exchange")
