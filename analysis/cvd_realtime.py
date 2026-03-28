"""
analysis/cvd_realtime.py
Real CVD from OKX WebSocket trade stream.

Instead of approximating from candles, this consumes every individual
trade from OKX's public trades channel and tags each one as:
  - Buy-initiated  (aggressor = buyer)  → +size
  - Sell-initiated (aggressor = seller) → -size

This gives you true Cumulative Volume Delta — the actual footprint
of who is in control at any moment.

Key signals:
  • Price making new highs but CVD rolling over → distribution (reversal fuel)
  • Price making new lows but CVD flattening/rising → absorption (reversal fuel)
  • CVD and price in sync → genuine momentum, don't fade it

Usage:
    from analysis.cvd_realtime import CVDStream
    stream = CVDStream()
    stream.start()                     # non-blocking, runs in background thread
    snapshot = stream.snapshot()       # get current CVD state anytime
    stream.stop()
"""

import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, List
import websocket
import numpy as np

import config

# ─────────────────────────────────────────────────────────────────────────────
# OKX WebSocket endpoints
# ─────────────────────────────────────────────────────────────────────────────
OKX_WS_PUBLIC = "wss://ws.okx.com:8443/ws/v5/public"

# Normalize symbol: BTC/USDT:USDT → BTC-USDT-SWAP
def _okx_inst_id() -> str:
    sym = config.SYMBOL  # e.g. "BTC/USDT:USDT"
    base, quote = sym.split("/")
    quote_clean = quote.split(":")[0]
    return f"{base}-{quote_clean}-SWAP"


# ─────────────────────────────────────────────────────────────────────────────
# Trade tick
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class TradeTick:
    ts:        datetime
    price:     float
    size:      float          # contract size in BTC
    side:      str            # 'buy' | 'sell'
    delta:     float          # +size if buy, -size if sell


# ─────────────────────────────────────────────────────────────────────────────
# CVD Snapshot — what you read from outside the stream
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class CVDSnapshot:
    timestamp:       datetime
    cvd_raw:         float          # running cumulative delta
    delta_1m:        float          # delta last 60 seconds
    delta_5m:        float          # delta last 5 minutes
    delta_15m:       float          # delta last 15 minutes
    buy_vol_1m:      float
    sell_vol_1m:     float
    buy_vol_5m:      float
    sell_vol_5m:     float
    last_price:      float
    trend:           str            # 'rising' | 'falling' | 'neutral'
    divergence:      str            # 'bearish_div' | 'bullish_div' | 'none'
    conviction:      str            # 'high' | 'medium' | 'low'
    price_high_5m:   float
    price_low_5m:    float
    connected:       bool = True

    def confirms_short(self) -> bool:
        """CVD confirming a short setup — sellers in control."""
        return self.trend == "falling" or self.divergence == "bearish_div"

    def confirms_long(self) -> bool:
        """CVD confirming a long setup — buyers in control."""
        return self.trend == "rising" or self.divergence == "bullish_div"

    def confirms_direction(self, direction: str) -> bool:
        if direction == "short":
            return self.confirms_short()
        elif direction == "long":
            return self.confirms_long()
        return False

    def summary(self) -> str:
        trend_e = {"rising": "📈", "falling": "📉", "neutral": "➡️"}.get(self.trend, "")
        div_str = ""
        if self.divergence == "bearish_div":
            div_str = " ⚠️ BEARISH DIV"
        elif self.divergence == "bullish_div":
            div_str = " ⚠️ BULLISH DIV"

        return (
            f"CVD {trend_e} {self.trend.upper()}{div_str} | "
            f"Δ1m: {self.delta_1m:+.2f} | "
            f"Δ5m: {self.delta_5m:+.2f} | "
            f"Conviction: {self.conviction.upper()}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# CVD Stream — WebSocket consumer
# ─────────────────────────────────────────────────────────────────────────────
class CVDStream:
    """
    Persistent WebSocket connection to OKX trades channel.
    Runs in a background thread, accumulates ticks, exposes CVDSnapshot.
    Auto-reconnects on disconnect.
    """

    RECONNECT_DELAY = 5      # seconds between reconnect attempts
    MAX_TICKS       = 50_000 # rolling buffer size

    def __init__(self):
        self._inst_id   = _okx_inst_id()
        self._ticks: deque = deque(maxlen=self.MAX_TICKS)
        self._cvd_running = 0.0
        self._lock        = threading.Lock()
        self._ws          = None
        self._thread      = None
        self._stop_event  = threading.Event()
        self._connected   = False
        self._last_price  = 0.0
        self._snapshot_cache: Optional[CVDSnapshot] = None

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the WebSocket thread (non-blocking)."""
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        print(f"[CVD] Stream started → {self._inst_id}")

    def stop(self) -> None:
        self._stop_event.set()
        if self._ws:
            self._ws.close()
        print("[CVD] Stream stopped.")

    def snapshot(self) -> Optional[CVDSnapshot]:
        """
        Return a CVDSnapshot computed from current tick buffer.
        Returns None if no ticks yet received.
        """
        with self._lock:
            ticks = list(self._ticks)

        if not ticks:
            return None

        now = datetime.now(tz=timezone.utc)

        def ticks_in_window(seconds: int) -> List[TradeTick]:
            cutoff = now.timestamp() - seconds
            return [t for t in ticks if t.ts.timestamp() >= cutoff]

        t1m  = ticks_in_window(60)
        t5m  = ticks_in_window(300)
        t15m = ticks_in_window(900)

        def delta(ts): return sum(t.delta for t in ts)
        def buy_vol(ts): return sum(t.size for t in ts if t.side == "buy")
        def sell_vol(ts): return sum(t.size for t in ts if t.side == "sell")

        d1m  = delta(t1m)
        d5m  = delta(t5m)
        d15m = delta(t15m)

        bv1  = buy_vol(t1m)
        sv1  = sell_vol(t1m)
        bv5  = buy_vol(t5m)
        sv5  = sell_vol(t5m)

        # Price range over 5m
        prices_5m    = [t.price for t in t5m] or [self._last_price]
        price_hi_5m  = max(prices_5m)
        price_lo_5m  = min(prices_5m)

        # CVD trend: slope of 1-minute delta buckets over last 15m
        trend = self._compute_trend(ticks, now)

        # Divergence: price making new high but delta declining = bearish div
        divergence = self._compute_divergence(t5m, price_hi_5m, price_lo_5m, d5m)

        # Conviction based on volume imbalance
        total_1m = bv1 + sv1
        if total_1m > 0:
            imbalance = abs(bv1 - sv1) / total_1m
            conviction = "high" if imbalance > 0.65 else "medium" if imbalance > 0.40 else "low"
        else:
            conviction = "low"

        snap = CVDSnapshot(
            timestamp     = now,
            cvd_raw       = self._cvd_running,
            delta_1m      = round(d1m,  4),
            delta_5m      = round(d5m,  4),
            delta_15m     = round(d15m, 4),
            buy_vol_1m    = round(bv1,  4),
            sell_vol_1m   = round(sv1,  4),
            buy_vol_5m    = round(bv5,  4),
            sell_vol_5m   = round(sv5,  4),
            last_price    = self._last_price,
            trend         = trend,
            divergence    = divergence,
            conviction    = conviction,
            price_high_5m = price_hi_5m,
            price_low_5m  = price_lo_5m,
            connected     = self._connected,
        )
        self._snapshot_cache = snap
        return snap

    def last_snapshot(self) -> Optional[CVDSnapshot]:
        """Return cached snapshot without recomputing."""
        return self._snapshot_cache

    # ── Internal computation ──────────────────────────────────────────────────

    def _compute_trend(self, ticks: list, now: datetime) -> str:
        """Bucket ticks into 1-minute bins and compute slope."""
        if len(ticks) < 10:
            return "neutral"

        buckets: dict = {}
        for t in ticks:
            age = (now.timestamp() - t.ts.timestamp())
            if age > 900:   # 15 min max
                continue
            bucket = int(age // 60)
            buckets[bucket] = buckets.get(bucket, 0) + t.delta

        if len(buckets) < 3:
            return "neutral"

        keys   = sorted(buckets.keys())
        values = [buckets[k] for k in keys]
        # Reverse so index 0 = oldest
        values.reverse()

        x     = np.arange(len(values))
        slope = np.polyfit(x, values, 1)[0]

        # Normalize
        rng = max(abs(v) for v in values) or 1
        norm = slope / rng

        if norm > 0.1:
            return "rising"
        elif norm < -0.1:
            return "falling"
        return "neutral"

    def _compute_divergence(
        self,
        ticks_5m: list,
        price_hi: float,
        price_lo: float,
        delta_5m: float,
    ) -> str:
        """
        Bearish divergence: price at 5m high but delta is negative.
        Bullish divergence: price at 5m low but delta is positive.
        """
        if not ticks_5m or self._last_price == 0:
            return "none"

        price_range = price_hi - price_lo
        if price_range == 0:
            return "none"

        # Is current price near the 5m high?
        near_high = (price_hi - self._last_price) / price_range < 0.15
        near_low  = (self._last_price - price_lo) / price_range < 0.15

        if near_high and delta_5m < 0:
            return "bearish_div"
        if near_low and delta_5m > 0:
            return "bullish_div"
        return "none"

    # ── WebSocket machinery ───────────────────────────────────────────────────

    def _run_loop(self) -> None:
        """Reconnect loop — keeps the stream alive."""
        while not self._stop_event.is_set():
            try:
                self._connect()
            except Exception as e:
                print(f"[CVD] Connection error: {e}")
            if not self._stop_event.is_set():
                print(f"[CVD] Reconnecting in {self.RECONNECT_DELAY}s...")
                time.sleep(self.RECONNECT_DELAY)

    def _connect(self) -> None:
        self._ws = websocket.WebSocketApp(
            OKX_WS_PUBLIC,
            on_open    = self._on_open,
            on_message = self._on_message,
            on_error   = self._on_error,
            on_close   = self._on_close,
        )
        self._ws.run_forever(ping_interval=25, ping_timeout=10)

    def _on_open(self, ws) -> None:
        self._connected = True
        sub = {
            "op": "subscribe",
            "args": [{"channel": "trades", "instId": self._inst_id}]
        }
        ws.send(json.dumps(sub))
        print(f"[CVD] Subscribed to trades: {self._inst_id}")

    def _on_message(self, ws, message: str) -> None:
        try:
            data = json.loads(message)

            # Ping/pong handling
            if data.get("event") == "subscribe":
                return
            if "data" not in data:
                return

            for trade in data["data"]:
                side  = trade.get("side", "")          # 'buy' | 'sell'
                price = float(trade.get("px",  0))
                size  = float(trade.get("sz",  0))     # contracts
                ts_ms = int(  trade.get("ts",  0))

                if not side or price == 0 or size == 0:
                    continue

                delta = size if side == "buy" else -size
                tick  = TradeTick(
                    ts    = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc),
                    price = price,
                    size  = size,
                    side  = side,
                    delta = delta,
                )

                with self._lock:
                    self._ticks.append(tick)
                    self._cvd_running += delta
                    self._last_price   = price

        except Exception as e:
            print(f"[CVD] Parse error: {e}")

    def _on_error(self, ws, error) -> None:
        print(f"[CVD] WebSocket error: {error}")
        self._connected = False

    def _on_close(self, ws, code, msg) -> None:
        self._connected = False
        print(f"[CVD] Disconnected (code={code})")


# ─────────────────────────────────────────────────────────────────────────────
# Global singleton — shared across all modules
# ─────────────────────────────────────────────────────────────────────────────
_stream_instance: Optional[CVDStream] = None


def get_stream() -> CVDStream:
    """Return the global CVD stream, creating it if needed."""
    global _stream_instance
    if _stream_instance is None:
        _stream_instance = CVDStream()
    return _stream_instance


def start_stream() -> CVDStream:
    """Start the global stream and return it."""
    stream = get_stream()
    stream.start()
    # Give it 3 seconds to connect and receive first ticks
    time.sleep(3)
    return stream


def cvd_confirms_realtime(direction: str) -> bool:
    """
    Drop-in replacement for the old cvd_confirms().
    Uses real WebSocket data if stream is running,
    falls back to True (no filter) if stream not available.
    """
    stream = get_stream()
    snap   = stream.snapshot()
    if snap is None or not snap.connected:
        print("[CVD] No live data — skipping CVD filter")
        return True
    return snap.confirms_direction(direction)
