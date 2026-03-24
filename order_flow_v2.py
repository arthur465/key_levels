import os
import time
import requests
from datetime import datetime, timezone, timedelta
from flask import Flask, request, jsonify
import threading
import json
import csv
from collections import deque
import numpy as np
import statistics

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
OKX_INST_ID = "BTC-USDT-SWAP"
OKX_CANDLE_BAR = "15m"

# ============= LUXALGO SWING DETECTION SETTINGS =============
SWING_LENGTH = 50  # Number of candles before/after to confirm swing
# A swing high = current high is highest out of 50 candles before AND after
# A swing low = current low is lowest out of 50 candles before AND after

# ============= ORDER BLOCK SETTINGS =============
MAX_ORDER_BLOCKS = 5  # Show last 5 order blocks
ORDER_BLOCK_MITIGATION_METHOD = 'close'  # 'close', 'wick', or 'avg'

# ============= LIVE PIVOT + ORDERFLOW SCORING SETTINGS =============
LIVE_PIVOT_LEFT_BARS = 8
LIVE_PIVOT_MIN_RANGE_MULTIPLIER = 1.2
PIVOT_NEAR_TOLERANCE_PCT = 0.2
ORDERFLOW_PERSISTENCE_REQUIRED = 2
SIGNAL_MODE = os.environ.get('SIGNAL_MODE', 'hybrid').lower()  # legacy, v10, hybrid
ENABLE_DYNAMIC_THRESHOLDS = os.environ.get('ENABLE_DYNAMIC_THRESHOLDS', '1') == '1'
ENABLE_SIGNAL_CSV_LOG = os.environ.get('ENABLE_SIGNAL_CSV_LOG', '1') == '1'
SIGNAL_CSV_PATH = os.environ.get('SIGNAL_CSV_PATH', 'orderflow_signals.csv')
ABSORPTION_FLOOR = 700.0
SETUP_SCORE_MIN = 65.0
ACTIONABLE_SCORE_MIN = 75.0
RUN_WEB = os.environ.get('RUN_WEB', '1') == '1'
RUN_SCANNER = os.environ.get('RUN_SCANNER', '1') == '1'
SEND_STARTUP_TELEGRAM = os.environ.get('SEND_STARTUP_TELEGRAM', '1') == '1'

# Estimated BTC-per-contract multipliers (tune if exchange contract specs change).
EXCHANGE_BTC_PER_CONTRACT = {
    'okx': 0.01,     # BTC-USDT-SWAP
    'bitget': 0.001, # BTCUSDT USDT-FUTURES
    'kucoin': 0.001  # XBTUSDTM
}

EXCHANGE_BASE_WEIGHTS = {
    'okx': 1.0,
    'bitget': 0.9,
    'kucoin': 0.85
}

# ============= ORDERFLOW + ORDER BLOCK INTEGRATION =============
# The KEY insight: Order blocks are LAGGING (form after the move)
# But orderflow shows accumulation/distribution LIVE
# So we detect: Accumulation AT swing low = bullish OB forming NOW

OB_ORDERFLOW_THRESHOLDS = {
    # For detecting accumulation at swing lows (bullish OB forming)
    'bullish_ob_forming': {
        'delta_min': -20,          # Heavy selling at the low (normalized BTC)
        'price_change_max': 0.12,  # Price barely drops
        'ratio_min': 2.5,          # Must be 2.5x heavier than normal
        'at_swing': True,          # Must be AT a swing low
        'severity_min': 5.0        # Minimum severity
    },
    
    # For detecting distribution at swing highs (bearish OB forming)
    'bearish_ob_forming': {
        'delta_min': 20,           # Heavy buying at the high (normalized BTC)
        'price_change_max': 0.12,  # Price barely rises
        'ratio_min': 2.5,          # Must be 2.5x heavier than normal
        'at_swing': True,          # Must be AT a swing high
        'severity_min': 5.0        # Minimum severity
    },
    
    # For determining which OBs will HOLD (when retested)
    'ob_will_hold': {
        'delta_confirmation': 12,    # Opposite orderflow confirming the block (normalized BTC)
        'volume_spike': 1.5,         # 1.5x normal volume when price hits OB
        'rejection_candle': True     # Price rejection from OB zone
    },
    
    # For determining which OBs will BREAK
    'ob_will_break': {
        'delta_against': -15,        # Strong orderflow AGAINST the block (normalized BTC)
        'no_reaction': True,         # Price doesn't react at OB
        'volume_weak': 0.7           # Weak volume at OB = no interest
    }
}

# ============= DATA STRUCTURES =============
active_trades = {}
orderflow_history = deque(maxlen=100)  # Need more history for swing detection
candle_history = deque(maxlen=200)     # Store OHLCV data for structure
orderflow_feature_history = deque(maxlen=200)
swing_highs = deque(maxlen=20)         # Last 20 swing highs
swing_lows = deque(maxlen=20)          # Last 20 swing lows
live_swing_highs = deque(maxlen=20)
live_swing_lows = deque(maxlen=20)
bullish_order_blocks = deque(maxlen=MAX_ORDER_BLOCKS)
bearish_order_blocks = deque(maxlen=MAX_ORDER_BLOCKS)
forming_order_blocks = []  # Order blocks forming RIGHT NOW (not confirmed yet)
last_pattern_alerts = {}
signal_streaks = {'bullish': 0, 'bearish': 0}
dynamic_thresholds = {
    'bullish_delta_min': OB_ORDERFLOW_THRESHOLDS['bullish_ob_forming']['delta_min'],
    'bearish_delta_min': OB_ORDERFLOW_THRESHOLDS['bearish_ob_forming']['delta_min'],
    'hold_delta_confirmation': OB_ORDERFLOW_THRESHOLDS['ob_will_hold']['delta_confirmation'],
    'break_delta_against': OB_ORDERFLOW_THRESHOLDS['ob_will_break']['delta_against']
}

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


def detect_live_pivots(candles, left_bars=LIVE_PIVOT_LEFT_BARS):
    """
    Detect candidate pivots using ONLY left-side candles (live, non-confirmed).
    Returns latest candidate high/low pivots that can later be confirmed or invalidated.
    """
    if len(candles) < left_bars + 2:
        return []

    recent_ranges = [c['high'] - c['low'] for c in candles[-30:] if c['high'] > c['low']]
    base_range = statistics.median(recent_ranges) if recent_ranges else 0.0
    min_required_range = base_range * LIVE_PIVOT_MIN_RANGE_MULTIPLIER

    pivots = []
    for i in range(left_bars, len(candles)):
        left_window = candles[i - left_bars:i]
        current = candles[i]
        if not left_window:
            continue

        is_live_high = all(current['high'] >= c['high'] for c in left_window)
        is_live_low = all(current['low'] <= c['low'] for c in left_window)
        candle_range = current['high'] - current['low']

        if candle_range < min_required_range:
            continue

        if is_live_high:
            pivots.append({
                'type': 'high',
                'price': current['high'],
                'index': i,
                'time': current['time'],
                'confirmed': False
            })
        if is_live_low:
            pivots.append({
                'type': 'low',
                'price': current['low'],
                'index': i,
                'time': current['time'],
                'confirmed': False
            })

    return pivots[-6:]


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


def normalize_contracts_to_btc(exchange_key, contracts):
    multiplier = EXCHANGE_BTC_PER_CONTRACT.get(exchange_key, 0.0)
    return contracts * multiplier


def rolling_zscore(values, latest):
    if len(values) < 10:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    std_dev = variance ** 0.5
    if std_dev == 0:
        return 0.0
    return (latest - mean) / std_dev


def build_orderflow_features(current_orderflow):
    deltas = [h['delta'] for h in orderflow_history]
    cvds = [h['cvd'] for h in orderflow_history]
    prices = [h['price'] for h in orderflow_history]

    delta_z = rolling_zscore(deltas, current_orderflow['delta'])
    cvd_z = rolling_zscore(cvds, current_orderflow['cvd'])

    cvd_slope = 0.0
    if len(cvds) >= 4:
        cvd_slope = cvds[-1] - cvds[-4]

    price_displacement = 0.0
    if len(prices) >= 2 and prices[-2] > 0:
        price_displacement = abs((prices[-1] - prices[-2]) / prices[-2]) * 100

    absorption_score = 0.0
    if abs(current_orderflow['delta']) > 0:
        absorption_score = abs(current_orderflow['delta']) / max(price_displacement, 0.03)

    imbalance = current_orderflow.get('weighted_imbalance', 0.0)

    # Composite score (0-100): balances flow intensity, persistence and imbalance.
    score = (
        min(30, abs(delta_z) * 8) +
        min(20, abs(cvd_z) * 6) +
        min(25, abs(imbalance) * 40) +
        min(25, absorption_score / 1500)
    )

    return {
        'delta_z': delta_z,
        'cvd_z': cvd_z,
        'cvd_slope': cvd_slope,
        'absorption_score': absorption_score,
        'price_displacement_pct': price_displacement,
        'composite_score': min(100.0, score)
    }


def update_dynamic_thresholds():
    if not ENABLE_DYNAMIC_THRESHOLDS or len(orderflow_history) < 30:
        return dynamic_thresholds

    deltas = [h['delta'] for h in orderflow_history]
    abs_deltas = [abs(d) for d in deltas]
    if not abs_deltas:
        return dynamic_thresholds

    p70 = float(np.percentile(abs_deltas, 70))
    p80 = float(np.percentile(abs_deltas, 80))
    p60 = float(np.percentile(abs_deltas, 60))

    dynamic_thresholds['bullish_delta_min'] = -max(12.0, p70)
    dynamic_thresholds['bearish_delta_min'] = max(12.0, p70)
    dynamic_thresholds['hold_delta_confirmation'] = max(8.0, p60)
    dynamic_thresholds['break_delta_against'] = -max(10.0, p80)
    return dynamic_thresholds


def log_signal_csv(event_type, direction, tier, current_price, current_orderflow, features, extra=None):
    if not ENABLE_SIGNAL_CSV_LOG:
        return

    row = {
        'timestamp_utc': datetime.now(timezone.utc).isoformat(),
        'event_type': event_type,
        'direction': direction,
        'tier': tier,
        'signal_mode': SIGNAL_MODE,
        'price': round(current_price, 2) if current_price else None,
        'delta_btc': round(current_orderflow.get('delta', 0.0), 4),
        'cvd_btc': round(current_orderflow.get('cvd', 0.0), 4),
        'avg_bid_pct': round(current_orderflow.get('avg_bid_pct', 0.0), 4),
        'avg_buy_pct': round(current_orderflow.get('avg_buy_pct', 0.0), 4),
        'weighted_imbalance': round(current_orderflow.get('weighted_imbalance', 0.0), 6),
        'delta_z': round(features.get('delta_z', 0.0), 4),
        'cvd_z': round(features.get('cvd_z', 0.0), 4),
        'cvd_slope': round(features.get('cvd_slope', 0.0), 4),
        'absorption_score': round(features.get('absorption_score', 0.0), 2),
        'price_displacement_pct': round(features.get('price_displacement_pct', 0.0), 5),
        'composite_score': round(features.get('composite_score', 0.0), 2),
        'dynamic_bullish_delta_min': round(dynamic_thresholds['bullish_delta_min'], 4),
        'dynamic_bearish_delta_min': round(dynamic_thresholds['bearish_delta_min'], 4)
    }
    if extra:
        for key, value in extra.items():
            row[key] = value

    fieldnames = list(row.keys())
    try:
        file_exists = os.path.exists(SIGNAL_CSV_PATH)
        with open(SIGNAL_CSV_PATH, mode='a', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()
            writer.writerow(row)
    except Exception as e:
        print(f"⚠️ CSV log error: {e}")


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
    swing_check = is_near_swing_point(current_price, swing_points, tolerance_pct=PIVOT_NEAR_TOLERANCE_PCT)
    
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
        delta_min = dynamic_thresholds['bullish_delta_min'] if ENABLE_DYNAMIC_THRESHOLDS else threshold['delta_min']
        
        # Heavy selling at the low
        if current_delta < delta_min:
            # Price barely drops (absorption!)
            if price_change_pct < threshold['price_change_max']:
                # Orderflow is significantly heavier
                if ratio >= threshold['ratio_min']:
                    severity = min(10.0, (abs(current_delta) / 10) * (ratio / 2))
                    
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
        delta_min = dynamic_thresholds['bearish_delta_min'] if ENABLE_DYNAMIC_THRESHOLDS else threshold['delta_min']
        
        # Heavy buying at the high
        if current_delta > delta_min:
            # Price barely rises (absorption!)
            if price_change_pct < threshold['price_change_max']:
                # Orderflow is significantly heavier
                if ratio >= threshold['ratio_min']:
                    severity = min(10.0, (abs(current_delta) / 10) * (ratio / 2))
                    
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
    hold_delta = dynamic_thresholds['hold_delta_confirmation'] if ENABLE_DYNAMIC_THRESHOLDS else hold_threshold['delta_confirmation']
    break_delta = dynamic_thresholds['break_delta_against'] if ENABLE_DYNAMIC_THRESHOLDS else break_threshold['delta_against']
    
    # ========== BULLISH OB: Will it hold? ==========
    if ob['type'] == 'bullish':
        # Good sign: Buying pressure when price hits the OB
        if current_delta > hold_delta:
            return {
                'in_zone': True,
                'prediction': 'WILL_HOLD',
                'reason': f'Strong buying ({current_delta:,.0f} BTC) confirms support',
                'confidence': 'HIGH'
            }
        
        # Bad sign: Continued selling or weak reaction
        elif current_delta < break_delta:
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
        if current_delta < -hold_delta:
            return {
                'in_zone': True,
                'prediction': 'WILL_HOLD',
                'reason': f'Strong selling ({current_delta:,.0f} BTC) confirms resistance',
                'confidence': 'HIGH'
            }
        
        # Bad sign: Continued buying or weak reaction
        elif current_delta > -break_delta:
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
def get_aggregated_orderflow(current_price):
    """Aggregate normalized orderflow from all exchanges with weighting."""
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
    
    # Normalize and weight each exchange contribution first.
    weighted_delta_sum = 0.0
    weighted_cvd_sum = 0.0
    weighted_bid_pct_sum = 0.0
    weighted_buy_pct_sum = 0.0
    weight_total = 0.0

    print("\n   🔍 INDIVIDUAL EXCHANGE DELTAS:")
    for ex in exchanges_data:
        name = ex['name']
        key = ex['orderbook']['exchange']
        raw_delta = ex['orderbook']['delta']
        raw_cvd = ex['trades']['cvd']
        delta_btc = normalize_contracts_to_btc(key, raw_delta)
        cvd_btc = normalize_contracts_to_btc(key, raw_cvd)
        depth_btc = normalize_contracts_to_btc(
            key, ex['orderbook']['bid_volume'] + ex['orderbook']['ask_volume']
        )
        liquidity_weight = min(1.5, max(0.5, depth_btc / 60.0))
        base_weight = EXCHANGE_BASE_WEIGHTS.get(key, 0.8)
        final_weight = base_weight * liquidity_weight

        print(f"      {name}: raw={raw_delta:,.0f} contracts, normalized={delta_btc:,.2f} BTC, w={final_weight:.2f}")

        weighted_delta_sum += delta_btc * final_weight
        weighted_cvd_sum += cvd_btc * final_weight
        weighted_bid_pct_sum += ex['orderbook']['bid_pct'] * final_weight
        weighted_buy_pct_sum += ex['trades']['buy_pct'] * final_weight
        weight_total += final_weight

    try:
        if weight_total == 0:
            return None
        total_delta = weighted_delta_sum / weight_total
        total_cvd = weighted_cvd_sum / weight_total
        avg_bid_pct = weighted_bid_pct_sum / weight_total
        avg_buy_pct = weighted_buy_pct_sum / weight_total
        weighted_imbalance = (avg_bid_pct - 50.0) / 50.0
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
        'weighted_imbalance': weighted_imbalance,
        'weight_total': weight_total,
        'exchanges_count': len(exchanges_data),
        'exchanges_data': exchanges_data
    }

# ============= PRICE & CANDLE FETCHING =============

def get_current_price():
    """Fetch current BTC price from OKX."""
    try:
        url = "https://www.okx.com/api/v5/market/ticker"
        params = {"instId": OKX_INST_ID}
        response = requests.get(url, params=params, timeout=5)
        data = response.json()
        if data.get("code") == "0" and data.get("data"):
            return float(data["data"][0]["last"])
        return None
    except Exception as e:
        print(f"❌ Price fetch error: {e}")
        return None


def fetch_candle_data(limit=200):
    """
    Fetch recent candle data for swing detection
    Returns: list of {'time': datetime, 'open': float, 'high': float, 'low': float, 'close': float, 'volume': float}
    """
    try:
        url = "https://www.okx.com/api/v5/market/candles"
        params = {
            "instId": OKX_INST_ID,
            "bar": OKX_CANDLE_BAR,
            "limit": limit
        }
        response = requests.get(url, params=params, timeout=10)
        data = response.json()

        if data.get("code") != "0":
            return []

        raw_candles = data.get("data", [])
        raw_candles = list(reversed(raw_candles))  # oldest -> newest
        candles = []
        for candle in raw_candles:
            candles.append({
                'time': datetime.fromtimestamp(candle[0] / 1000, tz=timezone.utc),
                'open': float(candle[1]),
                'high': float(candle[2]),
                'low': float(candle[3]),
                'close': float(candle[4]),
                'volume': float(candle[5])  # contracts on OKX
            })
        
        return candles
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
    
    # Detect confirmed swing highs and lows (lagging, higher confidence)
    swings = detect_swing_highs_lows(list(candle_history), SWING_LENGTH)
    live_swings = detect_live_pivots(list(candle_history), LIVE_PIVOT_LEFT_BARS)
    
    # Separate into highs and lows
    swing_highs.clear()
    swing_lows.clear()
    for swing in swings:
        if swing['type'] == 'high':
            swing_highs.append(swing)
        else:
            swing_lows.append(swing)

    live_swing_highs.clear()
    live_swing_lows.clear()
    for swing in live_swings:
        if swing['type'] == 'high':
            live_swing_highs.append(swing)
        else:
            live_swing_lows.append(swing)
    
    print(f"\n📊 Structure Analysis:")
    print(f"   Swing Highs detected: {len(swing_highs)}")
    print(f"   Swing Lows detected: {len(swing_lows)}")
    print(f"   Live Pivot High candidates: {len(live_swing_highs)}")
    print(f"   Live Pivot Low candidates: {len(live_swing_lows)}")
    
    # Detect order blocks from swings
    order_blocks = detect_order_blocks(list(candle_history), swings)
    bullish_order_blocks.clear()
    bullish_order_blocks.extend(order_blocks['bullish'])
    bearish_order_blocks.clear()
    bearish_order_blocks.extend(order_blocks['bearish'])
    
    print(f"   Bullish Order Blocks: {len(bullish_order_blocks)}")
    print(f"   Bearish Order Blocks: {len(bearish_order_blocks)}")
    
    # Get current orderflow from exchanges
    current_orderflow = get_aggregated_orderflow(current_price)
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
    thresholds = update_dynamic_thresholds()

    features = build_orderflow_features(current_orderflow)
    orderflow_feature_history.append(features)
    print(
        f"   Score: {features['composite_score']:.1f}/100 | "
        f"delta_z={features['delta_z']:.2f} cvd_z={features['cvd_z']:.2f} "
        f"imb={current_orderflow.get('weighted_imbalance', 0):.2f}"
    )
    print(
        f"   Dynamic thresholds: bull<{thresholds['bullish_delta_min']:.2f} "
        f"bear>{thresholds['bearish_delta_min']:.2f} hold>{thresholds['hold_delta_confirmation']:.2f}"
    )
    
    # Calculate average delta
    if len(orderflow_history) >= 5:
        recent_deltas = [h['delta'] for h in list(orderflow_history)[-5:]]
        avg_delta = sum(abs(d) for d in recent_deltas) / len(recent_deltas)
    else:
        avg_delta = 1000
    
    # ========== KEY DETECTION: OB FORMING LIVE ==========
    # Use live pivots first for early detection, then confirmed swings as fallback.
    all_swings = list(live_swing_highs) + list(live_swing_lows)
    if not all_swings:
        all_swings = list(swing_highs) + list(swing_lows)
    ob_forming = detect_ob_forming_live(current_orderflow, current_price, all_swings, avg_delta)
    
    if ob_forming:
        direction = ob_forming['type']
        opp_direction = 'bearish' if direction == 'bullish' else 'bullish'
        if SIGNAL_MODE == 'legacy':
            is_signal_ready = True
        elif SIGNAL_MODE == 'v10':
            is_signal_ready = (
                features['composite_score'] >= SETUP_SCORE_MIN and
                features['absorption_score'] >= ABSORPTION_FLOOR
            )
        else:  # hybrid
            is_signal_ready = features['composite_score'] >= SETUP_SCORE_MIN

        if is_signal_ready:
            signal_streaks[direction] = signal_streaks.get(direction, 0) + 1
        else:
            signal_streaks[direction] = 0
        signal_streaks[opp_direction] = 0

        if signal_streaks[direction] < ORDERFLOW_PERSISTENCE_REQUIRED:
            print(f"   ⏳ {direction.upper()} setup seen but waiting persistence ({signal_streaks[direction]}/{ORDERFLOW_PERSISTENCE_REQUIRED})")
            log_signal_csv(
                event_type='ob_forming_candidate',
                direction=direction,
                tier='INFO',
                current_price=current_price,
                current_orderflow=current_orderflow,
                features=features,
                extra={'persistence': signal_streaks[direction], 'required_persistence': ORDERFLOW_PERSISTENCE_REQUIRED}
            )
        else:
            cooldown_key = f"ob_forming_{ob_forming['type']}"
            now = time.time()
            last_alert = last_pattern_alerts.get(cooldown_key, 0)
            
            if now - last_alert > 3600:  # 1 hour cooldown
                print(f"\n🚨 ORDER BLOCK FORMING LIVE!")
                print(f"   Type: {ob_forming['type'].upper()}")
                print(f"   Confidence: {ob_forming['confidence']}")
                print(f"   Severity: {ob_forming['severity']:.1f}/10")
                print(f"   Composite Score: {features['composite_score']:.1f}/100")
                tier = "ACTIONABLE" if features['composite_score'] >= ACTIONABLE_SCORE_MIN else "SETUP"
                
                message = f"""
🚨 <b>ORDER BLOCK FORMING - {tier}</b>
Type: <b>{ob_forming['type'].upper()} OB</b>
Confidence: <b>{ob_forming['confidence']}</b> ⚡
Tier: <b>{tier}</b>
Composite Score: <b>{features['composite_score']:.1f}/100</b>

<b>🎯 WHY THIS IS EARLY:</b>
Traditional OB: Forms AFTER price moves away (confirmed swings)
This signal: Detecting candidate pivot + orderflow LIVE as it forms

<b>💎 ORDERFLOW CONFIRMATION:</b>
Delta (normalized): {ob_forming['delta']:,.2f} BTC
Ratio: {ob_forming['ratio']:.1f}x heavier than normal
Severity: {ob_forming['severity']:.1f}/10
Delta Z-Score: {features['delta_z']:.2f}
CVD Z-Score: {features['cvd_z']:.2f}
Absorption Score: {features['absorption_score']:.0f}

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
{"Heavy selling ({abs(ob_forming['delta']):,.2f} BTC normalized) at a pivot low, but price barely drops. Smart money is absorbing it. Candidate bullish order block forming now." if ob_forming['type'] == 'bullish' else "Heavy buying ({ob_forming['delta']:,.2f} BTC normalized) at a pivot high, but price barely rises. Smart money is distributing. Candidate bearish order block forming now."}
"""
                
                send_telegram(message)
                last_pattern_alerts[cooldown_key] = now
                log_signal_csv(
                    event_type='ob_forming_alert',
                    direction=direction,
                    tier=tier,
                    current_price=current_price,
                    current_orderflow=current_orderflow,
                    features=features,
                    extra={
                        'ob_ratio': round(ob_forming['ratio'], 4),
                        'ob_severity': round(ob_forming['severity'], 4),
                        'swing_price': round(ob_forming['swing_price'], 2)
                    }
                )
    
    # ========== CHECK EXISTING OBs FOR STRENGTH ==========
    # Check if price is near any confirmed order blocks
    for ob in list(bullish_order_blocks):
        ob_strength = check_ob_strength_at_retest(ob, current_orderflow, current_price)
        
        if ob_strength['in_zone'] and ob_strength['prediction'] in ['WILL_HOLD', 'WILL_BREAK']:
            cooldown_key = f"ob_retest_bullish_{ob['time']}"
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
                log_signal_csv(
                    event_type='ob_retest',
                    direction='bullish',
                    tier='ACTIONABLE' if ob_strength['prediction'] == 'WILL_HOLD' else 'SETUP',
                    current_price=current_price,
                    current_orderflow=current_orderflow,
                    features=features,
                    extra={'prediction': ob_strength['prediction']}
                )
    
    for ob in list(bearish_order_blocks):
        ob_strength = check_ob_strength_at_retest(ob, current_orderflow, current_price)
        
        if ob_strength['in_zone'] and ob_strength['prediction'] in ['WILL_HOLD', 'WILL_BREAK']:
            cooldown_key = f"ob_retest_bearish_{ob['time']}"
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
                log_signal_csv(
                    event_type='ob_retest',
                    direction='bearish',
                    tier='ACTIONABLE' if ob_strength['prediction'] == 'WILL_HOLD' else 'SETUP',
                    current_price=current_price,
                    current_orderflow=current_orderflow,
                    features=features,
                    extra={'prediction': ob_strength['prediction']}
                )
    
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
        <li><b>Dual Pivot Engine</b> - Live pivot candidates + confirmed Lux swings</li>
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
        <li>Detect live pivot candidates in real-time</li>
        <li><b>Use ORDERFLOW to see OB forming LIVE</b></li>
        <li><b>Enter NOW while it's forming</b></li>
        <li>No waiting for retest</li>
    </ol>
    <p><b>Advantage:</b> Early entry before price moves away!</p>
    
    <h2>📊 HOW IT WORKS:</h2>
    <h3>Detecting OB Forming:</h3>
    <ul>
        <li>Are we at a live pivot or confirmed swing? ✓</li>
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
    print(f"  Run Web: {RUN_WEB}")
    print(f"  Run Scanner: {RUN_SCANNER}")
    print(f"  Startup Telegram: {SEND_STARTUP_TELEGRAM}")
    print("="*80 + "\n")
    
    if RUN_SCANNER:
        # Start monitoring thread
        orderflow_thread = threading.Thread(target=monitor_orderflow, daemon=True)
        orderflow_thread.start()
    else:
        print("ℹ️ Scanner disabled (RUN_SCANNER=0)")
    
    # Send startup message only when scanner is active (or explicitly desired).
    if RUN_SCANNER and SEND_STARTUP_TELEGRAM:
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

    if RUN_WEB:
        port = int(os.environ.get('PORT', 10000))
        app.run(host='0.0.0.0', port=port)
    elif RUN_SCANNER:
        # Keep process alive for scanner-only worker mode.
        print("🔁 Scanner-only mode active (RUN_WEB=0, RUN_SCANNER=1)")
        while True:
            time.sleep(60)
    else:
        print("⚠️ Both RUN_WEB and RUN_SCANNER are disabled; exiting.")
