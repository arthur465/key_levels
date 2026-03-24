import os
import time
import requests
from datetime import datetime, timezone, timedelta
from flask import Flask, request, jsonify
import threading
import json
from collections import deque
import numpy as np

import logging
import sys

# Force all output to stdout (Render logs)
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    stream=sys.stdout
)

# ============= CONFIGURATION =============
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
SYMBOL = "BTCUSDT"
ORDERFLOW_INTERVAL = 900  # 15 minutes

# ============= LUXALGO SWING DETECTION SETTINGS =============
SWING_LENGTH = 50  # Number of candles before/after to confirm swing
# A swing high = current high is highest out of 50 candles before AND after
# A swing low = current low is lowest out of 50 candles before AND after

# ============= ORDER BLOCK SETTINGS =============
MAX_ORDER_BLOCKS = 5  # Show last 5 order blocks
ORDER_BLOCK_MITIGATION_METHOD = 'close'  # 'close', 'wick', or 'avg'

# ============= ORDERFLOW + ORDER BLOCK INTEGRATION =============
# The KEY insight: Order blocks are LAGGING (form after the move)
# But orderflow shows accumulation/distribution LIVE
# So we detect: Accumulation AT swing low = bullish OB forming NOW

OB_ORDERFLOW_THRESHOLDS = {
    # For detecting accumulation at swing lows (bullish OB forming)
    'bullish_ob_forming': {
        'delta_min': -6000,        # Heavy selling at the low
        'price_change_max': 0.12,  # Price barely drops
        'ratio_min': 2.5,          # Must be 2.5x heavier than normal
        'at_swing': True,          # Must be AT a swing low
        'severity_min': 5.0        # Minimum severity
    },
    
    # For detecting distribution at swing highs (bearish OB forming)
    'bearish_ob_forming': {
        'delta_min': 6000,         # Heavy buying at the high
        'price_change_max': 0.12,  # Price barely rises
        'ratio_min': 2.5,          # Must be 2.5x heavier than normal
        'at_swing': True,          # Must be AT a swing high
        'severity_min': 5.0        # Minimum severity
    },
    
    # For determining which OBs will HOLD (when retested)
    'ob_will_hold': {
        'delta_confirmation': 3000,  # Opposite orderflow confirming the block
        'volume_spike': 1.5,         # 1.5x normal volume when price hits OB
        'rejection_candle': True     # Price rejection from OB zone
    },
    
    # For determining which OBs will BREAK
    'ob_will_break': {
        'delta_against': -4000,      # Strong orderflow AGAINST the block
        'no_reaction': True,         # Price doesn't react at OB
        'volume_weak': 0.7           # Weak volume at OB = no interest
    }
}

# ============= DATA STRUCTURES =============
active_trades = {}
orderflow_history = deque(maxlen=100)  # Need more history for swing detection
candle_history = deque(maxlen=200)     # Store OHLCV data for structure
swing_highs = deque(maxlen=20)         # Last 20 swing highs
swing_lows = deque(maxlen=20)          # Last 20 swing lows
bullish_order_blocks = deque(maxlen=MAX_ORDER_BLOCKS)
bearish_order_blocks = deque(maxlen=MAX_ORDER_BLOCKS)
forming_order_blocks = []  # Order blocks forming RIGHT NOW (not confirmed yet)
last_pattern_alerts = {}

# ============= LUXALGO SWING DETECTION =============

def detect_swing_highs_lows(candles, swing_length=SWING_LENGTH):
    """
    Detect swing highs and lows using LuxAlgo logic:
    - Swing High: current high is highest out of swing_length candles before AND after
    - Swing Low: current low is lowest out of swing_length candles before AND after
    
    Returns: list of {'type': 'high'/'low', 'price': float, 'index': int, 'time': datetime}
    """
    if len(candles) < (swing_length * 2 + 1):
        return []
    
    swings = []
    
    # Check each candle (excluding first/last swing_length)
    for i in range(swing_length, len(candles) - swing_length):
        current_high = candles[i]['high']
        current_low = candles[i]['low']
        
        # Check if swing high
        is_swing_high = True
        for j in range(i - swing_length, i + swing_length + 1):
            if j == i:
                continue
            if candles[j]['high'] > current_high:
                is_swing_high = False
                break
        
        if is_swing_high:
            swings.append({
                'type': 'high',
                'price': current_high,
                'index': i,
                'time': candles[i]['time']
            })
        
        # Check if swing low
        is_swing_low = True
        for j in range(i - swing_length, i + swing_length + 1):
            if j == i:
                continue
            if candles[j]['low'] < current_low:
                is_swing_low = False
                break
        
        if is_swing_low:
            swings.append({
                'type': 'low',
                'price': current_low,
                'index': i,
                'time': candles[i]['time']
            })
    
    return swings


def detect_order_blocks(candles, swings):
    """
    Detect order blocks using LuxAlgo logic:
    - Bullish OB: Candle before bullish swing low (last down candle before reversal)
    - Bearish OB: Candle before bearish swing high (last up candle before reversal)
    
    Returns: {'bullish': [...], 'bearish': [...]}
    """
    bullish_obs = []
    bearish_obs = []
    
    for swing in swings:
        if swing['type'] == 'low':
            # Bullish order block = candle BEFORE the swing low
            # This is the last bearish candle before price reversed up
            i = swing['index']
            if i > 0:
                ob_candle = candles[i - 1]
                
                bullish_obs.append({
                    'top': ob_candle['high'],
                    'bottom': ob_candle['low'],
                    'time': ob_candle['time'],
                    'swing_price': swing['price'],
                    'mitigated': False,
                    'volume': ob_candle.get('volume', 0)
                })
        
        elif swing['type'] == 'high':
            # Bearish order block = candle BEFORE the swing high
            # This is the last bullish candle before price reversed down
            i = swing['index']
            if i > 0:
                ob_candle = candles[i - 1]
                
                bearish_obs.append({
                    'top': ob_candle['high'],
                    'bottom': ob_candle['low'],
                    'time': ob_candle['time'],
                    'swing_price': swing['price'],
                    'mitigated': False,
                    'volume': ob_candle.get('volume', 0)
                })
    
    return {
        'bullish': bullish_obs[-MAX_ORDER_BLOCKS:],  # Keep last N
        'bearish': bearish_obs[-MAX_ORDER_BLOCKS:]
    }


# ============= ORDERFLOW + ORDER BLOCK INTEGRATION =============

def is_near_swing_point(current_price, swing_points, tolerance_pct=0.3):
    """
    Check if current price is near a swing high or low
    Returns: {'near': True/False, 'type': 'high'/'low'/'none', 'price': float}
    """
    if not swing_points:
        return {'near': False, 'type': 'none', 'price': 0}
    
    for swing in swing_points:
        price_diff_pct = abs((current_price - swing['price']) / swing['price']) * 100
        
        if price_diff_pct <= tolerance_pct:
            return {
                'near': True,
                'type': swing['type'],
                'price': swing['price']
            }
    
    return {'near': False, 'type': 'none', 'price': 0}


def detect_ob_forming_live(current_orderflow, current_price, swing_points, avg_delta):
    """
    EARLY DETECTION: Detect order block forming RIGHT NOW (before it's confirmed)
    
    This is the KEY function that gives us EARLY entries!
    
    Logic:
    1. Are we at a swing point?
    2. Is there heavy accumulation (for bullish) or distribution (for bearish)?
    3. Is price absorbing the orderflow (not moving much)?
    
    If YES to all 3 = ORDER BLOCK IS FORMING RIGHT NOW
    """
    swing_check = is_near_swing_point(current_price, swing_points, tolerance_pct=0.3)
    
    if not swing_check['near']:
        return None
    
    current_delta = current_orderflow['delta']
    
    # Get recent price change
    if len(orderflow_history) >= 2:
        prev_price = list(orderflow_history)[-2]['price']
        price_change_pct = abs((current_price - prev_price) / prev_price) * 100
    else:
        price_change_pct = 0
    
    ratio = abs(current_delta / avg_delta) if avg_delta != 0 else 1.0
    
    # ========== BULLISH OB FORMING (at swing low) ==========
    if swing_check['type'] == 'low':
        threshold = OB_ORDERFLOW_THRESHOLDS['bullish_ob_forming']
        
        # Heavy selling at the low
        if current_delta < threshold['delta_min']:
            # Price barely drops (absorption!)
            if price_change_pct < threshold['price_change_max']:
                # Orderflow is significantly heavier
                if ratio >= threshold['ratio_min']:
                    severity = min(10.0, (abs(current_delta) / 1000) * (ratio / 2))
                    
                    if severity >= threshold['severity_min']:
                        return {
                            'type': 'bullish',
                            'price': current_price,
                            'swing_price': swing_check['price'],
                            'delta': current_delta,
                            'ratio': ratio,
                            'severity': severity,
                            'top': current_price + (current_price * 0.002),  # Estimate zone
                            'bottom': current_price - (current_price * 0.002),
                            'confidence': 'HIGH' if ratio >= 3.5 else 'MEDIUM',
                            'time': datetime.now(timezone.utc)
                        }
    
    # ========== BEARISH OB FORMING (at swing high) ==========
    elif swing_check['type'] == 'high':
        threshold = OB_ORDERFLOW_THRESHOLDS['bearish_ob_forming']
        
        # Heavy buying at the high
        if current_delta > threshold['delta_min']:
            # Price barely rises (absorption!)
            if price_change_pct < threshold['price_change_max']:
                # Orderflow is significantly heavier
                if ratio >= threshold['ratio_min']:
                    severity = min(10.0, (abs(current_delta) / 1000) * (ratio / 2))
                    
                    if severity >= threshold['severity_min']:
                        return {
                            'type': 'bearish',
                            'price': current_price,
                            'swing_price': swing_check['price'],
                            'delta': current_delta,
                            'ratio': ratio,
                            'severity': severity,
                            'top': current_price + (current_price * 0.002),
                            'bottom': current_price - (current_price * 0.002),
                            'confidence': 'HIGH' if ratio >= 3.5 else 'MEDIUM',
                            'time': datetime.now(timezone.utc)
                        }
    
    return None


def check_ob_strength_at_retest(ob, current_orderflow, current_price):
    """
    When price comes back to an order block, use orderflow to determine:
    - Will it HOLD (bounce from OB)?
    - Will it BREAK (push through OB)?
    
    This helps filter good OBs from bad ones!
    """
    # Check if price is in the OB zone
    in_zone = ob['bottom'] <= current_price <= ob['top']
    
    if not in_zone:
        return {'in_zone': False, 'prediction': 'N/A'}
    
    current_delta = current_orderflow['delta']
    hold_threshold = OB_ORDERFLOW_THRESHOLDS['ob_will_hold']
    break_threshold = OB_ORDERFLOW_THRESHOLDS['ob_will_break']
    
    # ========== BULLISH OB: Will it hold? ==========
    if ob['type'] == 'bullish':
        # Good sign: Buying pressure when price hits the OB
        if current_delta > hold_threshold['delta_confirmation']:
            return {
                'in_zone': True,
                'prediction': 'WILL_HOLD',
                'reason': f'Strong buying ({current_delta:,.0f} BTC) confirms support',
                'confidence': 'HIGH'
            }
        
        # Bad sign: Continued selling or weak reaction
        elif current_delta < break_threshold['delta_against']:
            return {
                'in_zone': True,
                'prediction': 'WILL_BREAK',
                'reason': f'Continued selling ({current_delta:,.0f} BTC) breaking support',
                'confidence': 'HIGH'
            }
        
        # Neutral: Not enough data
        else:
            return {
                'in_zone': True,
                'prediction': 'UNCERTAIN',
                'reason': 'Waiting for orderflow confirmation',
                'confidence': 'LOW'
            }
    
    # ========== BEARISH OB: Will it hold? ==========
    elif ob['type'] == 'bearish':
        # Good sign: Selling pressure when price hits the OB
        if current_delta < -hold_threshold['delta_confirmation']:
            return {
                'in_zone': True,
                'prediction': 'WILL_HOLD',
                'reason': f'Strong selling ({current_delta:,.0f} BTC) confirms resistance',
                'confidence': 'HIGH'
            }
        
        # Bad sign: Continued buying or weak reaction
        elif current_delta > -break_threshold['delta_against']:
            return {
                'in_zone': True,
                'prediction': 'WILL_BREAK',
                'reason': f'Continued buying ({current_delta:,.0f} BTC) breaking resistance',
                'confidence': 'HIGH'
            }
        
        # Neutral
        else:
            return {
                'in_zone': True,
                'prediction': 'UNCERTAIN',
                'reason': 'Waiting for orderflow confirmation',
                'confidence': 'LOW'
            }
    
    return {'in_zone': False, 'prediction': 'N/A'}


# ============= HELPER FUNCTIONS =============

# ============= ORDER FLOW FETCHING FUNCTIONS =============

# ============= OKX API =============
def get_okx_orderbook():
    """OKX orderbook"""
    url = "https://www.okx.com/api/v5/market/books"
    params = {"instId": "BTC-USDT-SWAP", "sz": "400"}
    try:
        response = requests.get(url, params=params, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            if data.get("code") == "0":
                book = data["data"][0]
                bids = book["bids"]
                asks = book["asks"]
                
                bid_volume = sum(float(b[1]) for b in bids)
                ask_volume = sum(float(a[1]) for a in asks)
                total_volume = bid_volume + ask_volume
                
                delta = bid_volume - ask_volume
                bid_pct = (bid_volume / total_volume * 100) if total_volume > 0 else 50
                
                return {
                    'exchange': 'okx',
                    'delta': delta,
                    'bid_volume': bid_volume,
                    'ask_volume': ask_volume,
                    'bid_pct': bid_pct
                }
        return None
    except Exception as e:
        print(f"   ❌ [OKX] Error: {type(e).__name__}")
        return None

def get_okx_trades():
    """OKX trades"""
    url = "https://www.okx.com/api/v5/market/trades"
    params = {"instId": "BTC-USDT-SWAP", "limit": "500"}
    try:
        response = requests.get(url, params=params, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            if data.get("code") == "0":
                trades = data["data"]
                
                buy_volume = sum(float(t["sz"]) for t in trades if t["side"] == "buy")
                sell_volume = sum(float(t["sz"]) for t in trades if t["side"] == "sell")
                total = buy_volume + sell_volume
                
                buy_pct = (buy_volume / total * 100) if total > 0 else 50
                cvd = buy_volume - sell_volume
                
                return {
                    'exchange': 'okx',
                    'buy_pct': buy_pct,
                    'cvd': cvd,
                    'buy_volume': buy_volume,
                    'sell_volume': sell_volume
                }
        return None
    except Exception as e:
        print(f"   ❌ [OKX] Error: {type(e).__name__}")
        return None

# ============= BITGET API =============
def get_bitget_orderbook():
    """Bitget orderbook"""
    url = "https://api.bitget.com/api/v2/mix/market/merge-depth"
    params = {
        "symbol": "BTCUSDT",
        "productType": "USDT-FUTURES",
        "limit": "150"
    }
    try:
        response = requests.get(url, params=params, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            if data.get("code") == "00000":
                book = data["data"]
                bids = book["bids"]
                asks = book["asks"]
                
                bid_volume = sum(float(b[1]) for b in bids)
                ask_volume = sum(float(a[1]) for a in asks)
                total_volume = bid_volume + ask_volume
                
                delta = bid_volume - ask_volume
                bid_pct = (bid_volume / total_volume * 100) if total_volume > 0 else 50
                
                return {
                    'exchange': 'bitget',
                    'delta': delta,
                    'bid_volume': bid_volume,
                    'ask_volume': ask_volume,
                    'bid_pct': bid_pct
                }
        return None
    except Exception as e:
        print(f"   ❌ [BITGET] Error: {type(e).__name__}")
        return None

def get_bitget_trades():
    """Bitget trades"""
    url = "https://api.bitget.com/api/v2/mix/market/fills"
    params = {
        "symbol": "BTCUSDT",
        "productType": "USDT-FUTURES",
        "limit": "500"
    }
    try:
        response = requests.get(url, params=params, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            if data.get("code") == "00000":
                trades = data["data"]
                
                buy_volume = sum(float(t["size"]) for t in trades if t["side"] == "buy")
                sell_volume = sum(float(t["size"]) for t in trades if t["side"] == "sell")
                total = buy_volume + sell_volume
                
                buy_pct = (buy_volume / total * 100) if total > 0 else 50
                cvd = buy_volume - sell_volume
                
                return {
                    'exchange': 'bitget',
                    'buy_pct': buy_pct,
                    'cvd': cvd,
                    'buy_volume': buy_volume,
                    'sell_volume': sell_volume
                }
        return None
    except Exception as e:
        print(f"   ❌ [BITGET] Error: {type(e).__name__}")
        return None

# ============= KUCOIN API =============
def get_kucoin_orderbook():
    """KuCoin orderbook"""
    url = "https://api-futures.kucoin.com/api/v1/level2/depth100"
    params = {"symbol": "XBTUSDTM"}
    try:
        response = requests.get(url, params=params, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            if data.get("code") == "200000":
                book = data["data"]
                bids = book["bids"]
                asks = book["asks"]
                
                bid_volume = sum(float(b[1]) for b in bids)
                ask_volume = sum(float(a[1]) for a in asks)
                total_volume = bid_volume + ask_volume
                
                delta = bid_volume - ask_volume
                bid_pct = (bid_volume / total_volume * 100) if total_volume > 0 else 50
                
                return {
                    'exchange': 'kucoin',
                    'delta': delta,
                    'bid_volume': bid_volume,
                    'ask_volume': ask_volume,
                    'bid_pct': bid_pct
                }
        return None
    except Exception as e:
        print(f"   ❌ [KUCOIN] Error: {type(e).__name__}")
        return None

def get_kucoin_trades():
    """KuCoin trades"""
    url = "https://api-futures.kucoin.com/api/v1/trade/history"
    params = {"symbol": "XBTUSDTM"}
    try:
        response = requests.get(url, params=params, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            if data.get("code") == "200000":
                trades = data["data"]
                
                buy_volume = sum(float(t["size"]) for t in trades if t["side"] == "buy")
                sell_volume = sum(float(t["size"]) for t in trades if t["side"] == "sell")
                total = buy_volume + sell_volume
                
                buy_pct = (buy_volume / total * 100) if total > 0 else 50
                cvd = buy_volume - sell_volume
                
                return {
                    'exchange': 'kucoin',
                    'buy_pct': buy_pct,
                    'cvd': cvd,
                    'buy_volume': buy_volume,
                    'sell_volume': sell_volume
                }
        return None
    except Exception as e:
        print(f"   ❌ [KUCOIN] Error: {type(e).__name__}")
        return None

# ============= AGGREGATED ORDERFLOW =============
def get_aggregated_orderflow():
    """Aggregate order flow from all 3 exchanges with robust error handling"""
    print("\n📊 AGGREGATING 3-EXCHANGE ORDERFLOW")
    
    exchanges_data = []
    
    # OKX
    try:
        okx_book = get_okx_orderbook()
        okx_trades = get_okx_trades()
        if okx_book and okx_trades:
            exchanges_data.append({
                'name': 'OKX',
                'emoji': '🟠',
                'orderbook': okx_book,
                'trades': okx_trades
            })
    except Exception as e:
        print(f"   ❌ [OKX] Aggregation error: {e}")
    
    # Bitget
    try:
        bitget_book = get_bitget_orderbook()
        bitget_trades = get_bitget_trades()
        if bitget_book and bitget_trades:
            exchanges_data.append({
                'name': 'BITGET',
                'emoji': '🟢',
                'orderbook': bitget_book,
                'trades': bitget_trades
            })
    except Exception as e:
        print(f"   ❌ [BITGET] Aggregation error: {e}")
    
    # KuCoin
    try:
        kucoin_book = get_kucoin_orderbook()
        kucoin_trades = get_kucoin_trades()
        if kucoin_book and kucoin_trades:
            exchanges_data.append({
                'name': 'KUCOIN',
                'emoji': '🔵',
                'orderbook': kucoin_book,
                'trades': kucoin_trades
            })
    except Exception as e:
        print(f"   ❌ [KUCOIN] Aggregation error: {e}")
    
    if not exchanges_data:
        print("❌ No exchange data available")
        return None
    
    # DEBUG: Show individual exchange deltas
    print("\n   🔍 INDIVIDUAL EXCHANGE DELTAS:")
    for ex in exchanges_data:
        name = ex['name']
        delta = ex['orderbook']['delta']
        print(f"      {name}: {delta:,.0f} BTC")
    
    # Aggregate with error handling
    try:
        total_delta = sum(ex['orderbook']['delta'] for ex in exchanges_data)
        total_cvd = sum(ex['trades']['cvd'] for ex in exchanges_data)
        avg_bid_pct = sum(ex['orderbook']['bid_pct'] for ex in exchanges_data) / len(exchanges_data)
        avg_buy_pct = sum(ex['trades']['buy_pct'] for ex in exchanges_data) / len(exchanges_data)
        
        # TEMPORARY SANITY CAP (while debugging exchange normalization)
        # With 3 exchanges, normal delta should be -10k to +10k, cap at 15k for safety
        original_delta = total_delta
        if abs(total_delta) > 15000:
            total_delta = 15000 if total_delta > 0 else -15000
            print(f"   ⚠️ SANITY CAP APPLIED: {original_delta:,.0f} → {total_delta:,.0f}")
        
    except Exception as e:
        print(f"❌ Aggregation calculation error: {e}")
        return None
    
    # Determine bias
    if avg_bid_pct >= 55 and avg_buy_pct >= 55:
        bias = "🟢 BULLISH"
    elif avg_bid_pct <= 45 and avg_buy_pct <= 45:
        bias = "🔴 BEARISH"
    else:
        bias = "⚪ NEUTRAL"
    
    print(f"   Bias: {bias}, Delta: {total_delta:,.0f}, Exchanges: {len(exchanges_data)}/3")
    
    return {
        'bias': bias,
        'delta': total_delta,
        'cvd': total_cvd,
        'avg_bid_pct': avg_bid_pct,
        'avg_buy_pct': avg_buy_pct,
        'exchanges_count': len(exchanges_data),
        'exchanges_data': exchanges_data
    }

# ============= PRICE & CANDLE FETCHING =============

def get_current_price():
    """Fetch current BTC price from OKX"""
    try:
        url = "https://www.okx.com/api/v5/market/ticker"
        params = {"instId": "BTC-USDT"}         
        
        response = requests.get(url, params=params, timeout=5)
        response.raise_for_status()
        
        data = response.json()
        
        if data.get("code") == "0" and data.get("data"):
            price = float(data["data"][0]["last"])
            return price
        else:
            print(f"❌ OKX Price Error: {data.get('msg', data)}")
            return None
            
    except Exception as e:
        print(f"❌ Price fetch error: {e}")
        return None

def fetch_candle_data(limit=200):
    """Fetch recent 15m candle data from OKX for swing detection"""
    try:
        url = "https://www.okx.com/api/v5/market/candles"
        params = {
            "instId": "BTC-USDT",      # e.g. "BTC-USDT"
            "bar": "15m",          # 15 minute candles
            "limit": str(limit)
        }
        
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        
        data = response.json()
        
        if data.get("code") == "0" and data.get("data"):
            candles = []
            for candle in data["data"]:
                candles.append({
                    'time': datetime.fromtimestamp(int(candle[0]) / 1000, tz=timezone.utc),
                    'open': float(candle[1]),
                    'high': float(candle[2]),
                    'low': float(candle[3]),
                    'close': float(candle[4]),
                    'volume': float(candle[5])
                })
            return candles
        else:
            print(f"❌ OKX Candle Error: {data.get('msg', data)}")
            return []
            
    except Exception as e:
        print(f"❌ Candle fetch error: {e}")
        return []


def send_telegram(message):
    """Send message to Telegram"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"📱 [TELEGRAM WOULD SEND]:\n{message}\n")
        return
    
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            'chat_id': TELEGRAM_CHAT_ID,
            'text': message,
            'parse_mode': 'HTML'
        }
        response = requests.post(url, data=payload, timeout=5)
        if response.status_code == 200:
            print("✅ Telegram message sent")
        else:
            print(f"❌ Telegram error: {response.text}")
    except Exception as e:
        print(f"❌ Telegram send error: {e}")


# ============= MAIN MARKET SCANNING =============

def send_market_update():
    """
    Main scanning function with orderflow + order block integration
    """
    print("\n" + "="*80)
    print("🔍 SCANNING: Orderflow + LuxAlgo Swings + Order Blocks")
    print("="*80)
    
    current_price = get_current_price()
    if not current_price:
        print("❌ Cannot get price, skipping update")
        return
    
    # Fetch candle data for structure analysis
    candles = fetch_candle_data(limit=200)
    if not candles:
        print("❌ Cannot get candles, skipping update")
        return
    
    # Update candle history
    candle_history.clear()
    candle_history.extend(candles)
    
    # Detect swing highs and lows
    swings = detect_swing_highs_lows(list(candle_history), SWING_LENGTH)
    
    # Separate into highs and lows
    swing_highs.clear()
    swing_lows.clear()
    for swing in swings:
        if swing['type'] == 'high':
            swing_highs.append(swing)
        else:
            swing_lows.append(swing)
    
    print(f"\n📊 Structure Analysis:")
    print(f"   Swing Highs detected: {len(swing_highs)}")
    print(f"   Swing Lows detected: {len(swing_lows)}")
    
    # Detect order blocks from swings
    order_blocks = detect_order_blocks(list(candle_history), swings)
    bullish_order_blocks.clear()
    bullish_order_blocks.extend(order_blocks['bullish'])
    bearish_order_blocks.clear()
    bearish_order_blocks.extend(order_blocks['bearish'])
    
    print(f"   Bullish Order Blocks: {len(bullish_order_blocks)}")
    print(f"   Bearish Order Blocks: {len(bearish_order_blocks)}")
    
    # Get current orderflow from exchanges
    current_orderflow = get_aggregated_orderflow()
    if not current_orderflow:
        print("❌ Cannot get orderflow, skipping update")
        return
    
    # Track orderflow in history for average calculations
    orderflow_history.append({
        'timestamp': datetime.now(timezone.utc),
        'price': current_price,
        'delta': current_orderflow['delta'],
        'cvd': current_orderflow['cvd'],
        'avg_bid_pct': current_orderflow['avg_bid_pct'],
        'avg_buy_pct': current_orderflow['avg_buy_pct']
    })
    
    # Calculate average delta
    if len(orderflow_history) >= 5:
        recent_deltas = [h['delta'] for h in list(orderflow_history)[-5:]]
        avg_delta = sum(abs(d) for d in recent_deltas) / len(recent_deltas)
    else:
        avg_delta = 1000
    
    # ========== KEY DETECTION: OB FORMING LIVE ==========
    all_swings = list(swing_highs) + list(swing_lows)
    ob_forming = detect_ob_forming_live(current_orderflow, current_price, all_swings, avg_delta)
    
    if ob_forming:
        cooldown_key = f"ob_forming_{ob_forming['type']}"
        now = time.time()
        last_alert = last_pattern_alerts.get(cooldown_key, 0)
        
        if now - last_alert > 3600:  # 1 hour cooldown
            print(f"\n🚨 ORDER BLOCK FORMING LIVE!")
            print(f"   Type: {ob_forming['type'].upper()}")
            print(f"   Confidence: {ob_forming['confidence']}")
            print(f"   Severity: {ob_forming['severity']:.1f}/10")
            
            message = f"""
🚨 <b>ORDER BLOCK FORMING - EARLY ENTRY!</b>
Type: <b>{ob_forming['type'].upper()} OB</b>
Confidence: <b>{ob_forming['confidence']}</b> ⚡

<b>🎯 WHY THIS IS EARLY:</b>
Traditional OB: Forms AFTER price moves away
This signal: Detecting it LIVE as it forms!

<b>💎 ORDERFLOW CONFIRMATION:</b>
Delta: {ob_forming['delta']:,.0f} BTC
Ratio: {ob_forming['ratio']:.1f}x heavier than normal
Severity: {ob_forming['severity']:.1f}/10

<b>📍 LOCATION:</b>
Current Price: ${current_price:,.2f}
Swing Point: ${ob_forming['swing_price']:,.2f}
OB Zone: ${ob_forming['bottom']:,.2f} - ${ob_forming['top']:,.2f}

<b>⚠️ WHAT THIS MEANS:</b>
{"Smart money is ABSORBING heavy selling at this swing low" if ob_forming['type'] == 'bullish' else "Smart money is ABSORBING heavy buying at this swing high"}
Order block is forming RIGHT NOW
This is your EARLY ENTRY before price moves away

<b>🎯 TRADE SETUP:</b>
{"Entry: LONG at current price or slight dip" if ob_forming['type'] == 'bullish' else "Entry: SHORT at current price or slight rise"}
{"Stop: Below ${ob_forming['bottom']:,.2f}" if ob_forming['type'] == 'bullish' else "Stop: Above ${ob_forming['top']:,.2f}"}
{"Target: Next swing high" if ob_forming['type'] == 'bullish' else "Target: Next swing low"}

<b>💡 ADVANTAGE:</b>
You're entering NOW while OB forms
Not waiting for retest (which may never come)
This is the EARLY BIRD entry!

<b>🗣️ IN PLAIN ENGLISH:</b>
{"Heavy selling ({abs(ob_forming['delta']):,.0f} BTC) at a swing low, but price barely drops. Smart money is buying/absorbing it all. An order block is forming RIGHT NOW. Enter LONG before price pumps and you miss it!" if ob_forming['type'] == 'bullish' else "Heavy buying ({ob_forming['delta']:,.0f} BTC) at a swing high, but price barely rises. Smart money is selling/distributing. An order block is forming RIGHT NOW. Enter SHORT before price dumps and you miss it!"}
"""
            
            send_telegram(message)
            last_pattern_alerts[cooldown_key] = now
    
    # ========== CHECK EXISTING OBs FOR STRENGTH ==========
    # Check if price is near any confirmed order blocks
    for ob in list(bullish_order_blocks):
        ob_strength = check_ob_strength_at_retest(ob, current_orderflow, current_price)
        
        if ob_strength['in_zone'] and ob_strength['prediction'] in ['WILL_HOLD', 'WILL_BREAK']:
            cooldown_key = f"ob_retest_{ob['time']}"
            now = time.time()
            last_alert = last_pattern_alerts.get(cooldown_key, 0)
            
            if now - last_alert > 3600:
                print(f"\n📍 Price at Bullish OB - Prediction: {ob_strength['prediction']}")
                
                message = f"""
📍 <b>PRICE AT BULLISH ORDER BLOCK</b>

<b>OB ZONE:</b>
Top: ${ob['top']:,.2f}
Bottom: ${ob['bottom']:,.2f}
Current: ${current_price:,.2f}

<b>ORDERFLOW SAYS:</b>
Prediction: <b>{ob_strength['prediction']}</b>
Confidence: {ob_strength['confidence']}
Reason: {ob_strength['reason']}

<b>WHAT TO DO:</b>
{"✅ ENTER LONG - Orderflow confirms support" if ob_strength['prediction'] == 'WILL_HOLD' else "❌ AVOID LONG - Orderflow shows weakness"}
{"Stop below OB at ${ob['bottom']:,.2f}" if ob_strength['prediction'] == 'WILL_HOLD' else "Wait for clearer setup"}
"""
                
                send_telegram(message)
                last_pattern_alerts[cooldown_key] = now
    
    for ob in list(bearish_order_blocks):
        ob_strength = check_ob_strength_at_retest(ob, current_orderflow, current_price)
        
        if ob_strength['in_zone'] and ob_strength['prediction'] in ['WILL_HOLD', 'WILL_BREAK']:
            cooldown_key = f"ob_retest_{ob['time']}"
            now = time.time()
            last_alert = last_pattern_alerts.get(cooldown_key, 0)
            
            if now - last_alert > 3600:
                print(f"\n📍 Price at Bearish OB - Prediction: {ob_strength['prediction']}")
                
                message = f"""
📍 <b>PRICE AT BEARISH ORDER BLOCK</b>

<b>OB ZONE:</b>
Top: ${ob['top']:,.2f}
Bottom: ${ob['bottom']:,.2f}
Current: ${current_price:,.2f}

<b>ORDERFLOW SAYS:</b>
Prediction: <b>{ob_strength['prediction']}</b>
Confidence: {ob_strength['confidence']}
Reason: {ob_strength['reason']}

<b>WHAT TO DO:</b>
{"✅ ENTER SHORT - Orderflow confirms resistance" if ob_strength['prediction'] == 'WILL_HOLD' else "❌ AVOID SHORT - Orderflow shows weakness"}
{"Stop above OB at ${ob['top']:,.2f}" if ob_strength['prediction'] == 'WILL_HOLD' else "Wait for clearer setup"}
"""
                
                send_telegram(message)
                last_pattern_alerts[cooldown_key] = now
    
    print("\n✅ Market scan complete")
    print("="*80 + "\n")


# ============= MONITORING THREAD =============
def monitor_orderflow():
    """Scan market every 15 minutes"""
    print("🔄 Orderflow + Order Block scanning thread started")
    
    while True:
        try:
            send_market_update()
            time.sleep(ORDERFLOW_INTERVAL)
        except Exception as e:
            print(f"❌ Scanning error: {e}")
            time.sleep(ORDERFLOW_INTERVAL)


# ============= FLASK APP =============
app = Flask(__name__)

@app.route('/', methods=['GET'])
def home():
    return f"""
    <h1>🚀 Beast Mode V9 - ORDER BLOCK EDITION</h1>
    
    <h2>🆕 WHAT'S NEW:</h2>
    <ul>
        <li><b>LuxAlgo Swing Detection</b> - Accurate swing highs/lows (50-period)</li>
        <li><b>Order Blocks</b> - Last candle before swing reversals</li>
        <li><b>EARLY DETECTION</b> - Detect OB forming LIVE with orderflow ⚡</li>
        <li><b>OB Strength Prediction</b> - Which blocks will hold vs break</li>
    </ul>
    
    <h2>🎯 THE EDGE:</h2>
    <h3>Traditional Approach:</h3>
    <ol>
        <li>Wait for swing low to form (50 candles = 12.5 hours delay!)</li>
        <li>Identify the order block</li>
        <li>Wait for price to move away</li>
        <li>Wait for retest (IF it comes back)</li>
        <li>Enter on retest</li>
    </ol>
    <p><b>Problem:</b> Often price never comes back to retest!</p>
    
    <h3>Our Approach:</h3>
    <ol>
        <li>Detect swing lows in real-time</li>
        <li><b>Use ORDERFLOW to see OB forming LIVE</b></li>
        <li><b>Enter NOW while it's forming</b></li>
        <li>No waiting for retest</li>
    </ol>
    <p><b>Advantage:</b> Early entry before price moves away!</p>
    
    <h2>📊 HOW IT WORKS:</h2>
    <h3>Detecting OB Forming:</h3>
    <ul>
        <li>Are we at a swing point? ✓</li>
        <li>Heavy accumulation/distribution? ✓</li>
        <li>Price absorbing the orderflow (not moving)? ✓</li>
        <li>= <b>ORDER BLOCK FORMING NOW!</b></li>
    </ul>
    
    <h3>OB Strength at Retest:</h3>
    <ul>
        <li>Price hits confirmed OB</li>
        <li>Check orderflow reaction</li>
        <li>Predict: Will it HOLD or BREAK?</li>
        <li>Only trade the strong ones!</li>
    </ul>
    
    <h2>⚡ ALERTS YOU'LL GET:</h2>
    <ol>
        <li><b>OB FORMING LIVE</b> - Early entry signal (before confirmation)</li>
        <li><b>OB WILL HOLD</b> - Orderflow confirms support/resistance</li>
        <li><b>OB WILL BREAK</b> - Orderflow shows weakness (avoid)</li>
    </ol>
    
    <h2>🎨 REMOVED (as requested):</h2>
    <ul>
        <li>❌ BOS/CHoCH detection</li>
        <li>❌ Key levels (daily/weekly opens)</li>
        <li>❌ Structure tracking</li>
    </ul>
    
    <p><i>Scanning every {ORDERFLOW_INTERVAL/60:.0f} minutes for orderflow + order blocks!</i></p>
    <p><b>Swing Length:</b> {SWING_LENGTH} candles</p>
    <p><b>Max Order Blocks:</b> {MAX_ORDER_BLOCKS}</p>
    """, 200


if __name__ == '__main__':
    print("🚀 Beast Mode V9 - ORDER BLOCK EDITION")
    print("="*80)
    print("🆕 FEATURES:")
    print("  ✅ LuxAlgo Swing High/Low Detection (50-period)")
    print("  ✅ Order Blocks (last candle before swing reversal)")
    print("  ✅ EARLY DETECTION - OB forming LIVE with orderflow")
    print("  ✅ OB Strength Prediction - Which will hold vs break")
    print("="*80)
    print("\n🎯 THE EDGE:")
    print("  Traditional: Wait for swing → wait for retest → enter")
    print("  Our approach: Detect OB forming LIVE → enter NOW")
    print("  Advantage: Early entry before price moves away!")
    print("="*80)
    print("\n📊 SETTINGS:")
    print(f"  Swing Length: {SWING_LENGTH} candles")
    print(f"  Max Order Blocks: {MAX_ORDER_BLOCKS}")
    print(f"  Scan Interval: {ORDERFLOW_INTERVAL/60:.0f} minutes")
    print("="*80 + "\n")
    
    # Start monitoring thread
    orderflow_thread = threading.Thread(target=monitor_orderflow, daemon=True)
    orderflow_thread.start()
    
    # Send startup message
    send_telegram(f"""
🚀 <b>Beast Mode V9 - ORDER BLOCK EDITION Started!</b>

<b>🆕 WHAT'S NEW:</b>
✅ LuxAlgo Swing Detection (50-period)
✅ Order Blocks (last candle before reversal)
✅ <b>EARLY DETECTION</b> - OB forming LIVE ⚡
✅ OB Strength Prediction

<b>🎯 THE EDGE:</b>
Instead of waiting for retest, we detect order blocks forming LIVE using orderflow. This gives you early entries BEFORE price moves away!

<b>📊 HOW IT WORKS:</b>
1. Detect swing highs/lows (LuxAlgo logic)
2. Monitor orderflow at swing points
3. When we see accumulation/distribution at a swing = OB FORMING NOW
4. Alert you for early entry
5. Later, predict which OBs will hold vs break

<b>⚡ ALERTS:</b>
• OB FORMING LIVE - Early entry signal
• OB WILL HOLD - Strong OB confirmed
• OB WILL BREAK - Weak OB, avoid

<b>Scanning every {ORDERFLOW_INTERVAL/60:.0f} minutes!</b>
    """)
    
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
