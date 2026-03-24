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

# ============= SWING DETECTION SETTINGS =============
SWING_LENGTH = 25
LIVE_PIVOT_LEFT_BARS = 8
LIVE_PIVOT_MIN_RANGE_MULTIPLIER = 1.2
PIVOT_NEAR_TOLERANCE_PCT = 0.2

# ============= ORDER BLOCK SETTINGS =============
MAX_ORDER_BLOCKS = 5
ORDER_BLOCK_MITIGATION_METHOD = 'close'

# ============= NEW ORDER FLOW SETTINGS =============
CVD_LOOKBACK = 100  # Track CVD over 100 periods
LARGE_ORDER_THRESHOLD_BTC = 5.0  # 5 BTC = large order
ORDERBOOK_DEPTH_LEVELS = 20  # Track 20 levels deep
IMBALANCE_THRESHOLD = 2.0  # 2:1 ratio = significant imbalance
DELTA_DIVERGENCE_PERIODS = 10  # Check for divergence over 10 candles

# ============= ORDERFLOW THRESHOLDS =============
ORDERFLOW_PERSISTENCE_REQUIRED = 2
ABSORPTION_FLOOR = 500.0
SETUP_SCORE_MIN = 55.0
ACTIONABLE_SCORE_MIN = 70.0
RUN_WEB = os.environ.get('RUN_WEB', '1') == '1'
RUN_SCANNER = os.environ.get('RUN_SCANNER', '1') == '1'
SEND_STARTUP_TELEGRAM = os.environ.get('SEND_STARTUP_TELEGRAM', '1') == '1'
ENABLE_SIGNAL_CSV_LOG = os.environ.get('ENABLE_SIGNAL_CSV_LOG', '1') == '1'
SIGNAL_CSV_PATH = os.environ.get('SIGNAL_CSV_PATH', 'orderflow_signals.csv')

# ============= OB THRESHOLDS =============
OB_ORDERFLOW_THRESHOLDS = {
    'bullish_ob_forming': {
        'delta_min': -10,
        'price_change_max': 0.20,
        'ratio_min': 1.8,
        'at_swing': True,
        'severity_min': 3.0
    },
    'bearish_ob_forming': {
        'delta_min': 10,
        'price_change_max': 0.20,
        'ratio_min': 1.8,
        'at_swing': True,
        'severity_min': 3.0
    },
    'ob_will_hold': {
        'delta_confirmation': 8,
        'volume_spike': 1.3,
        'rejection_candle': True
    },
    'ob_will_break': {
        'delta_against': -10,
        'no_reaction': True,
        'volume_weak': 0.75
    }
}

# ============= DATA STRUCTURES =============
active_trades = {}
orderflow_history = deque(maxlen=100)
candle_history = deque(maxlen=200)
orderflow_feature_history = deque(maxlen=200)
swing_highs = deque(maxlen=20)
swing_lows = deque(maxlen=20)
live_swing_highs = deque(maxlen=20)
live_swing_lows = deque(maxlen=20)
bullish_order_blocks = deque(maxlen=MAX_ORDER_BLOCKS)
bearish_order_blocks = deque(maxlen=MAX_ORDER_BLOCKS)
forming_order_blocks = []
last_pattern_alerts = {}
signal_streaks = {'bullish': 0, 'bearish': 0}

# NEW: Enhanced order flow tracking
cvd_history = deque(maxlen=CVD_LOOKBACK)  # Cumulative Volume Delta
large_orders_history = deque(maxlen=50)  # Large order tracking
orderbook_snapshots = deque(maxlen=20)  # Order book depth snapshots
delta_divergences = deque(maxlen=20)  # Delta-price divergences

# NEW: Track OB quality assessments
ob_quality_cache = {}  # Store quality assessment for each OB

# ============= SWING DETECTION =============

def detect_swing_highs_lows(candles, swing_length=SWING_LENGTH):
    if len(candles) < (swing_length * 2 + 1):
        return []
    
    swings = []
    for i in range(swing_length, len(candles) - swing_length):
        current_high = candles[i]['high']
        current_low = candles[i]['low']
        
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
                'time': current['time']
            })
        
        if is_live_low:
            pivots.append({
                'type': 'low',
                'price': current['low'],
                'index': i,
                'time': current['time']
            })

    return pivots


# ============= ORDER BLOCK DETECTION =============

def create_order_block_from_swing(swing, candles):
    swing_index = swing['index']
    if swing_index < 1:
        return None
    
    ob_candle = candles[swing_index - 1]
    
    if swing['type'] == 'high':
        return {
            'type': 'bearish',
            'top': ob_candle['high'],
            'bottom': ob_candle['low'],
            'time': ob_candle['time'],
            'swing_price': swing['price'],
            'mitigated': False
        }
    else:
        return {
            'type': 'bullish',
            'top': ob_candle['high'],
            'bottom': ob_candle['low'],
            'time': ob_candle['time'],
            'swing_price': swing['price'],
            'mitigated': False
        }


def is_price_in_ob_zone(price, ob):
    return ob['bottom'] <= price <= ob['top']


def check_ob_mitigation(ob, current_candle):
    method = ORDER_BLOCK_MITIGATION_METHOD
    
    if method == 'close':
        if ob['type'] == 'bullish':
            return current_candle['close'] < ob['bottom']
        else:
            return current_candle['close'] > ob['top']
    
    elif method == 'wick':
        if ob['type'] == 'bullish':
            return current_candle['low'] < ob['bottom']
        else:
            return current_candle['high'] > ob['top']
    
    return False


# ============= ENHANCED ORDER FLOW FETCHING =============

def fetch_okx_orderbook():
    """Fetch order book depth from OKX"""
    try:
        url = "https://www.okx.com/api/v5/market/books"
        params = {"instId": OKX_INST_ID, "sz": ORDERBOOK_DEPTH_LEVELS}
        response = requests.get(url, params=params, timeout=10)
        data = response.json()
        
        if data.get('code') == '0' and data.get('data'):
            book = data['data'][0]
            
            bids = [[float(b[0]), float(b[1])] for b in book.get('bids', [])]  # [price, size]
            asks = [[float(a[0]), float(a[1])] for a in book.get('asks', [])]
            
            # Calculate bid/ask totals
            total_bid_volume = sum(b[1] for b in bids)
            total_ask_volume = sum(a[1] for a in asks)
            
            # Calculate weighted mid price
            if bids and asks:
                best_bid = bids[0][0]
                best_ask = asks[0][0]
                spread = best_ask - best_bid
                spread_pct = (spread / best_bid) * 100
            else:
                best_bid = best_ask = spread = spread_pct = 0
            
            return {
                'exchange': 'okx',
                'bids': bids,
                'asks': asks,
                'total_bid_volume': total_bid_volume,
                'total_ask_volume': total_ask_volume,
                'best_bid': best_bid,
                'best_ask': best_ask,
                'spread': spread,
                'spread_pct': spread_pct,
                'timestamp': datetime.now(timezone.utc)
            }
    except Exception as e:
        print(f"❌ OKX orderbook error: {e}")
    return None


def fetch_okx_trades():
    """Fetch recent trades to detect aggressive buy/sell and large orders"""
    try:
        url = "https://www.okx.com/api/v5/market/trades"
        params = {"instId": OKX_INST_ID, "limit": 100}
        response = requests.get(url, params=params, timeout=10)
        data = response.json()
        
        if data.get('code') == '0' and data.get('data'):
            trades = data['data']
            
            aggressive_buys = 0
            aggressive_sells = 0
            large_orders = []
            
            for trade in trades:
                size = float(trade['sz']) * 0.01  # Convert to BTC
                price = float(trade['px'])
                side = trade['side']  # 'buy' or 'sell'
                timestamp = int(trade['ts']) / 1000
                
                # Taker side (aggressive)
                if side == 'buy':
                    aggressive_buys += size
                else:
                    aggressive_sells += size
                
                # Detect large orders
                if size >= LARGE_ORDER_THRESHOLD_BTC:
                    large_orders.append({
                        'size': size,
                        'price': price,
                        'side': side,
                        'timestamp': timestamp
                    })
            
            return {
                'exchange': 'okx',
                'aggressive_buy_btc': aggressive_buys,
                'aggressive_sell_btc': aggressive_sells,
                'delta_btc': aggressive_buys - aggressive_sells,
                'large_orders': large_orders,
                'total_trades': len(trades)
            }
    except Exception as e:
        print(f"❌ OKX trades error: {e}")
    return None


def fetch_binance_orderbook():
    """Fetch order book from Binance for comparison"""
    try:
        url = "https://api.binance.com/api/v3/depth"
        params = {"symbol": "BTCUSDT", "limit": ORDERBOOK_DEPTH_LEVELS}
        response = requests.get(url, params=params, timeout=10)
        data = response.json()
        
        if 'bids' in data and 'asks' in data:
            bids = [[float(b[0]), float(b[1])] for b in data['bids']]
            asks = [[float(a[0]), float(a[1])] for a in data['asks']]
            
            total_bid_volume = sum(b[1] for b in bids)
            total_ask_volume = sum(a[1] for a in asks)
            
            best_bid = bids[0][0] if bids else 0
            best_ask = asks[0][0] if asks else 0
            spread = best_ask - best_bid if best_bid and best_ask else 0
            spread_pct = (spread / best_bid) * 100 if best_bid else 0
            
            return {
                'exchange': 'binance',
                'bids': bids,
                'asks': asks,
                'total_bid_volume': total_bid_volume,
                'total_ask_volume': total_ask_volume,
                'best_bid': best_bid,
                'best_ask': best_ask,
                'spread': spread,
                'spread_pct': spread_pct,
                'timestamp': datetime.now(timezone.utc)
            }
    except Exception as e:
        print(f"❌ Binance orderbook error: {e}")
    return None


def fetch_binance_trades():
    """Fetch recent trades from Binance"""
    try:
        url = "https://api.binance.com/api/v3/aggTrades"
        params = {"symbol": "BTCUSDT", "limit": 100}
        response = requests.get(url, params=params, timeout=10)
        data = response.json()
        
        if isinstance(data, list):
            aggressive_buys = 0
            aggressive_sells = 0
            large_orders = []
            
            for trade in data:
                size = float(trade['q'])  # Already in BTC
                price = float(trade['p'])
                is_buyer_maker = trade['m']  # True if buyer is maker (sell was aggressive)
                timestamp = trade['T'] / 1000
                
                if not is_buyer_maker:  # Buyer was taker (aggressive buy)
                    aggressive_buys += size
                else:  # Seller was taker (aggressive sell)
                    aggressive_sells += size
                
                if size >= LARGE_ORDER_THRESHOLD_BTC:
                    large_orders.append({
                        'size': size,
                        'price': price,
                        'side': 'buy' if not is_buyer_maker else 'sell',
                        'timestamp': timestamp
                    })
            
            return {
                'exchange': 'binance',
                'aggressive_buy_btc': aggressive_buys,
                'aggressive_sell_btc': aggressive_sells,
                'delta_btc': aggressive_buys - aggressive_sells,
                'large_orders': large_orders,
                'total_trades': len(data)
            }
    except Exception as e:
        print(f"❌ Binance trades error: {e}")
    return None


def fetch_comprehensive_orderflow():
    """Fetch complete order flow data from multiple sources"""
    
    # Fetch trade data
    okx_trades = fetch_okx_trades()
    binance_trades = fetch_binance_trades()
    
    # Fetch order book data
    okx_book = fetch_okx_orderbook()
    binance_book = fetch_binance_orderbook()
    
    # Aggregate trade data
    total_aggressive_buy = 0
    total_aggressive_sell = 0
    all_large_orders = []
    
    if okx_trades:
        total_aggressive_buy += okx_trades['aggressive_buy_btc']
        total_aggressive_sell += okx_trades['aggressive_sell_btc']
        all_large_orders.extend(okx_trades['large_orders'])
    
    if binance_trades:
        total_aggressive_buy += binance_trades['aggressive_buy_btc']
        total_aggressive_sell += binance_trades['aggressive_sell_btc']
        all_large_orders.extend(binance_trades['large_orders'])
    
    delta_btc = total_aggressive_buy - total_aggressive_sell
    
    # Calculate order book imbalance (average across exchanges)
    bid_ask_ratios = []
    spreads = []
    
    if okx_book:
        if okx_book['total_ask_volume'] > 0:
            ratio = okx_book['total_bid_volume'] / okx_book['total_ask_volume']
            bid_ask_ratios.append(ratio)
        spreads.append(okx_book['spread_pct'])
        orderbook_snapshots.append(okx_book)
    
    if binance_book:
        if binance_book['total_ask_volume'] > 0:
            ratio = binance_book['total_bid_volume'] / binance_book['total_ask_volume']
            bid_ask_ratios.append(ratio)
        spreads.append(binance_book['spread_pct'])
        orderbook_snapshots.append(binance_book)
    
    avg_bid_ask_ratio = statistics.mean(bid_ask_ratios) if bid_ask_ratios else 1.0
    avg_spread = statistics.mean(spreads) if spreads else 0.0
    
    # Track large orders
    if all_large_orders:
        large_orders_history.extend(all_large_orders)
    
    return {
        'aggressive_buy_btc': total_aggressive_buy,
        'aggressive_sell_btc': total_aggressive_sell,
        'delta_btc': delta_btc,
        'bid_ask_ratio': avg_bid_ask_ratio,
        'spread_pct': avg_spread,
        'large_orders_count': len(all_large_orders),
        'large_buy_count': sum(1 for o in all_large_orders if o['side'] == 'buy'),
        'large_sell_count': sum(1 for o in all_large_orders if o['side'] == 'sell'),
        'timestamp': datetime.now(timezone.utc)
    }


def fetch_okx_candles(limit=200):
    """Fetch OHLCV candles from OKX"""
    try:
        url = "https://www.okx.com/api/v5/market/candles"
        params = {
            "instId": OKX_INST_ID,
            "bar": OKX_CANDLE_BAR,
            "limit": limit
        }
        response = requests.get(url, params=params, timeout=10)
        data = response.json()
        
        if data.get('code') == '0' and data.get('data'):
            candles = []
            for c in reversed(data['data']):
                candles.append({
                    'time': datetime.fromtimestamp(int(c[0])/1000, tz=timezone.utc),
                    'open': float(c[1]),
                    'high': float(c[2]),
                    'low': float(c[3]),
                    'close': float(c[4]),
                    'volume': float(c[5])
                })
            return candles
    except Exception as e:
        print(f"❌ OKX candles error: {e}")
    return []


# ============= ENHANCED ORDER FLOW ANALYSIS =============

def update_cvd(current_delta):
    """Update Cumulative Volume Delta"""
    if cvd_history:
        last_cvd = cvd_history[-1]['cvd']
        new_cvd = last_cvd + current_delta
    else:
        new_cvd = current_delta
    
    cvd_history.append({
        'cvd': new_cvd,
        'delta': current_delta,
        'timestamp': datetime.now(timezone.utc)
    })
    
    return new_cvd


def detect_delta_divergence(candles, current_cvd):
    """Detect bearish/bullish divergences between price and CVD"""
    if len(candles) < DELTA_DIVERGENCE_PERIODS or len(cvd_history) < DELTA_DIVERGENCE_PERIODS:
        return None
    
    recent_candles = candles[-DELTA_DIVERGENCE_PERIODS:]
    recent_cvd = list(cvd_history)[-DELTA_DIVERGENCE_PERIODS:]
    
    # Price trend
    price_start = recent_candles[0]['close']
    price_end = recent_candles[-1]['close']
    price_trend = 'up' if price_end > price_start else 'down'
    
    # CVD trend
    cvd_start = recent_cvd[0]['cvd']
    cvd_end = recent_cvd[-1]['cvd']
    cvd_trend = 'up' if cvd_end > cvd_start else 'down'
    
    # Detect divergence
    if price_trend == 'up' and cvd_trend == 'down':
        divergence = {
            'type': 'bearish',
            'description': 'Price rising but CVD falling - Weak rally, possible reversal',
            'price_change': ((price_end - price_start) / price_start) * 100,
            'cvd_change': cvd_end - cvd_start,
            'confidence': 'HIGH' if abs(cvd_end - cvd_start) > 50 else 'MEDIUM'
        }
        delta_divergences.append(divergence)
        return divergence
    
    elif price_trend == 'down' and cvd_trend == 'up':
        divergence = {
            'type': 'bullish',
            'description': 'Price falling but CVD rising - Weak decline, possible reversal',
            'price_change': ((price_end - price_start) / price_start) * 100,
            'cvd_change': cvd_end - cvd_start,
            'confidence': 'HIGH' if abs(cvd_end - cvd_start) > 50 else 'MEDIUM'
        }
        delta_divergences.append(divergence)
        return divergence
    
    return None


def calculate_absorption_score(orderflow, features):
    """Calculate how much volume is being absorbed at current price"""
    bid_ask_ratio = orderflow.get('bid_ask_ratio', 1.0)
    large_orders = orderflow.get('large_orders_count', 0)
    delta = abs(orderflow.get('delta_btc', 0))
    
    # High absorption = lots of volume, low price movement, imbalanced book
    absorption_score = 0
    
    # Delta component (large delta = absorption)
    if delta > 20:
        absorption_score += 40
    elif delta > 10:
        absorption_score += 25
    elif delta > 5:
        absorption_score += 10
    
    # Bid/ask imbalance
    if bid_ask_ratio > IMBALANCE_THRESHOLD:
        absorption_score += 30  # Strong bid support
    elif bid_ask_ratio < (1 / IMBALANCE_THRESHOLD):
        absorption_score += 30  # Strong ask resistance
    
    # Large orders presence
    absorption_score += min(large_orders * 5, 30)
    
    return min(absorption_score, 100)


def calculate_orderflow_features(current_orderflow, history):
    """Calculate enhanced orderflow metrics - FIXED VERSION"""
    # CRITICAL FIX: Always return all keys, even with insufficient history
    if not history or len(history) < 10:
        return {
            'delta_normalized': 0,
            'delta_severity': 0,
            'volume_ratio': 1.0,
            'avg_delta': 0,
            'delta_std': 0,
            'cvd': 0,
            'bid_ask_ratio': 1.0,
            'spread_pct': 0.0,  # FIXED: Added missing key
            'absorption_score': 0,
            'large_orders_count': 0,
            'large_buy_count': 0,
            'large_sell_count': 0
        }
    
    recent = list(history)[-20:]
    deltas = [of['delta_btc'] for of in recent if 'delta_btc' in of]
    
    avg_delta = statistics.mean(deltas) if deltas else 0
    delta_std = statistics.stdev(deltas) if len(deltas) > 1 else 1
    
    delta_normalized = (current_orderflow['delta_btc'] - avg_delta) / delta_std if delta_std > 0 else 0
    delta_severity = abs(current_orderflow['delta_btc'])
    
    # Total volume
    current_volume = current_orderflow['aggressive_buy_btc'] + current_orderflow['aggressive_sell_btc']
    volumes = []
    for of in recent:
        if 'aggressive_buy_btc' in of and 'aggressive_sell_btc' in of:
            volumes.append(of['aggressive_buy_btc'] + of['aggressive_sell_btc'])
    
    avg_volume = statistics.mean(volumes) if volumes else 1
    volume_ratio = current_volume / avg_volume if avg_volume > 0 else 1.0
    
    # Update CVD
    current_cvd = update_cvd(current_orderflow['delta_btc'])
    
    # Calculate absorption
    absorption = calculate_absorption_score(current_orderflow, {
        'delta_severity': delta_severity,
        'volume_ratio': volume_ratio
    })
    
    return {
        'delta_normalized': delta_normalized,
        'delta_severity': delta_severity,
        'volume_ratio': volume_ratio,
        'avg_delta': avg_delta,
        'delta_std': delta_std,
        'cvd': current_cvd,
        'bid_ask_ratio': current_orderflow.get('bid_ask_ratio', 1.0),
        'spread_pct': current_orderflow.get('spread_pct', 0.0),  # FIXED: Use .get() with default
        'absorption_score': absorption,
        'large_orders_count': current_orderflow.get('large_orders_count', 0),
        'large_buy_count': current_orderflow.get('large_buy_count', 0),
        'large_sell_count': current_orderflow.get('large_sell_count', 0)
    }


def is_near_pivot(current_price, pivot, tolerance_pct=PIVOT_NEAR_TOLERANCE_PCT):
    pivot_price = pivot['price']
    tolerance = pivot_price * (tolerance_pct / 100)
    return abs(current_price - pivot_price) <= tolerance


def check_ob_forming_conditions(current_orderflow, features, current_price, candles):
    """Check if orderflow suggests an OB is about to form"""
    if not candles or len(candles) < 2:
        return None
    
    current_candle = candles[-1]
    price_change_pct = abs(current_candle['close'] - current_candle['open']) / current_candle['open'] * 100
    
    live_pivots = detect_live_pivots(list(candles))
    if not live_pivots:
        return None
    
    latest_pivot = live_pivots[-1]
    
    if not is_near_pivot(current_price, latest_pivot):
        return None
    
    # BULLISH OB forming
    if latest_pivot['type'] == 'low':
        thresholds = OB_ORDERFLOW_THRESHOLDS['bullish_ob_forming']
        
        conditions_met = {
            'delta_negative': current_orderflow['delta_btc'] < thresholds['delta_min'],
            'price_small_move': price_change_pct < thresholds['price_change_max'],
            'bid_imbalance': features['bid_ask_ratio'] > thresholds['ratio_min'],
            'at_swing_low': True,
            'high_severity': features['delta_severity'] > thresholds['severity_min']
        }
        
        if sum(conditions_met.values()) >= 3:
            return {
                'type': 'bullish',
                'confidence': sum(conditions_met.values()) / len(conditions_met),
                'conditions': conditions_met,
                'pivot': latest_pivot,
                'expected_ob_zone': f"${latest_pivot['price'] * 0.999:.2f} - ${latest_pivot['price'] * 1.001:.2f}"
            }
    
    # BEARISH OB forming
    elif latest_pivot['type'] == 'high':
        thresholds = OB_ORDERFLOW_THRESHOLDS['bearish_ob_forming']
        
        conditions_met = {
            'delta_positive': current_orderflow['delta_btc'] > thresholds['delta_min'],
            'price_small_move': price_change_pct < thresholds['price_change_max'],
            'ask_imbalance': features['bid_ask_ratio'] < (1 / thresholds['ratio_min']),
            'at_swing_high': True,
            'high_severity': features['delta_severity'] > thresholds['severity_min']
        }
        
        if sum(conditions_met.values()) >= 3:
            return {
                'type': 'bearish',
                'confidence': sum(conditions_met.values()) / len(conditions_met),
                'conditions': conditions_met,
                'pivot': latest_pivot,
                'expected_ob_zone': f"${latest_pivot['price'] * 0.999:.2f} - ${latest_pivot['price'] * 1.001:.2f}"
            }
    
    return None


def assess_ob_quality(ob, current_orderflow, features):
    """
    NEW: Assess if a newly formed OB is worth taking based on orderflow
    Returns: {'quality': 'HIGH' | 'MEDIUM' | 'LOW', 'reason': str, 'score': int}
    """
    score = 0
    reasons = []
    
    # Check delta alignment
    if ob['type'] == 'bullish':
        # Want positive delta (buying pressure) at bullish OB
        if current_orderflow['delta_btc'] > 5:
            score += 30
            reasons.append("✅ Strong buying delta (+{:.1f} BTC)".format(current_orderflow['delta_btc']))
        elif current_orderflow['delta_btc'] > 0:
            score += 15
            reasons.append("⚠️ Weak buying delta (+{:.1f} BTC)".format(current_orderflow['delta_btc']))
        else:
            reasons.append("❌ Negative delta ({:.1f} BTC) - sellers in control".format(current_orderflow['delta_btc']))
        
        # Want bid-heavy orderbook
        if features['bid_ask_ratio'] > 1.5:
            score += 25
            reasons.append("✅ Heavy bid support ({:.2f}x)".format(features['bid_ask_ratio']))
        elif features['bid_ask_ratio'] > 1.0:
            score += 10
            reasons.append("⚠️ Mild bid support ({:.2f}x)".format(features['bid_ask_ratio']))
        else:
            reasons.append("❌ Ask-heavy book ({:.2f}x) - weak support".format(features['bid_ask_ratio']))
    
    else:  # bearish OB
        # Want negative delta (selling pressure) at bearish OB
        if current_orderflow['delta_btc'] < -5:
            score += 30
            reasons.append("✅ Strong selling delta ({:.1f} BTC)".format(current_orderflow['delta_btc']))
        elif current_orderflow['delta_btc'] < 0:
            score += 15
            reasons.append("⚠️ Weak selling delta ({:.1f} BTC)".format(current_orderflow['delta_btc']))
        else:
            reasons.append("❌ Positive delta (+{:.1f} BTC) - buyers in control".format(current_orderflow['delta_btc']))
        
        # Want ask-heavy orderbook
        if features['bid_ask_ratio'] < 0.67:
            score += 25
            reasons.append("✅ Heavy ask resistance ({:.2f}x)".format(features['bid_ask_ratio']))
        elif features['bid_ask_ratio'] < 1.0:
            score += 10
            reasons.append("⚠️ Mild ask resistance ({:.2f}x)".format(features['bid_ask_ratio']))
        else:
            reasons.append("❌ Bid-heavy book ({:.2f}x) - weak resistance".format(features['bid_ask_ratio']))
    
    # Volume context
    if features['volume_ratio'] > 1.5:
        score += 20
        reasons.append("✅ High volume ({:.1f}x avg)".format(features['volume_ratio']))
    elif features['volume_ratio'] < 0.8:
        score -= 10
        reasons.append("❌ Low volume ({:.1f}x avg)".format(features['volume_ratio']))
    
    # CVD trend alignment
    if len(cvd_history) >= 5:
        recent_cvd = [x['cvd'] for x in list(cvd_history)[-5:]]
        cvd_trend = 'up' if recent_cvd[-1] > recent_cvd[0] else 'down'
        
        if (ob['type'] == 'bullish' and cvd_trend == 'up') or (ob['type'] == 'bearish' and cvd_trend == 'down'):
            score += 15
            reasons.append(f"✅ CVD trending {cvd_trend} (confirms direction)")
        else:
            reasons.append(f"⚠️ CVD trending {cvd_trend} (divergence)")
    
    # Absorption
    if features['absorption_score'] > 60:
        score += 10
        reasons.append(f"✅ High absorption ({features['absorption_score']}/100)")
    
    # Determine quality
    if score >= 70:
        quality = 'HIGH'
    elif score >= 40:
        quality = 'MEDIUM'
    else:
        quality = 'LOW'
    
    return {
        'quality': quality,
        'score': score,
        'reasons': reasons,
        'take_trade': quality in ['HIGH', 'MEDIUM']
    }


def check_ob_strength_at_retest(ob, current_orderflow, current_price):
    """
    Check if OB will hold or break based on orderflow during retest
    """
    in_zone = is_price_in_ob_zone(current_price, ob)
    if not in_zone:
        return {'in_zone': False}
    
    # Bullish OB retest (price coming back down to support)
    if ob['type'] == 'bullish':
        # Positive delta = buying at support = likely hold
        # Negative delta = selling through support = likely break
        
        if current_orderflow['delta_btc'] > 8:
            return {
                'in_zone': True,
                'prediction': 'WILL_HOLD',
                'confidence': 'HIGH',
                'reason': f"Heavy buying ({current_orderflow['delta_btc']:.1f} BTC delta) at support"
            }
        elif current_orderflow['delta_btc'] > 3:
            return {
                'in_zone': True,
                'prediction': 'WILL_HOLD',
                'confidence': 'MEDIUM',
                'reason': f"Moderate buying ({current_orderflow['delta_btc']:.1f} BTC delta) at support"
            }
        elif current_orderflow['delta_btc'] < -8:
            return {
                'in_zone': True,
                'prediction': 'WILL_BREAK',
                'confidence': 'HIGH',
                'reason': f"Heavy selling ({current_orderflow['delta_btc']:.1f} BTC delta) through support"
            }
        elif current_orderflow['delta_btc'] < -3:
            return {
                'in_zone': True,
                'prediction': 'WILL_BREAK',
                'confidence': 'MEDIUM',
                'reason': f"Selling pressure ({current_orderflow['delta_btc']:.1f} BTC delta) at support"
            }
        else:
            return {
                'in_zone': True,
                'prediction': 'UNCERTAIN',
                'confidence': 'LOW',
                'reason': f"Neutral delta ({current_orderflow['delta_btc']:.1f} BTC) - wait for clearer signal"
            }
    
    # Bearish OB retest (price coming back up to resistance)
    else:
        # Negative delta = selling at resistance = likely hold
        # Positive delta = buying through resistance = likely break
        
        if current_orderflow['delta_btc'] < -8:
            return {
                'in_zone': True,
                'prediction': 'WILL_HOLD',
                'confidence': 'HIGH',
                'reason': f"Heavy selling ({current_orderflow['delta_btc']:.1f} BTC delta) at resistance"
            }
        elif current_orderflow['delta_btc'] < -3:
            return {
                'in_zone': True,
                'prediction': 'WILL_HOLD',
                'confidence': 'MEDIUM',
                'reason': f"Moderate selling ({current_orderflow['delta_btc']:.1f} BTC delta) at resistance"
            }
        elif current_orderflow['delta_btc'] > 8:
            return {
                'in_zone': True,
                'prediction': 'WILL_BREAK',
                'confidence': 'HIGH',
                'reason': f"Heavy buying ({current_orderflow['delta_btc']:.1f} BTC delta) through resistance"
            }
        elif current_orderflow['delta_btc'] > 3:
            return {
                'in_zone': True,
                'prediction': 'WILL_BREAK',
                'confidence': 'MEDIUM',
                'reason': f"Buying pressure ({current_orderflow['delta_btc']:.1f} BTC delta) at resistance"
            }
        else:
            return {
                'in_zone': True,
                'prediction': 'UNCERTAIN',
                'confidence': 'LOW',
                'reason': f"Neutral delta ({current_orderflow['delta_btc']:.1f} BTC) - wait for clearer signal"
            }


# ============= TELEGRAM =============

def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Telegram not configured")
        return
    
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }
        response = requests.post(url, data=data, timeout=10)
        if response.status_code == 200:
            print("✅ Telegram message sent")
        else:
            print(f"❌ Telegram failed: {response.text}")
    except Exception as e:
        print(f"❌ Telegram error: {e}")


def log_signal_csv(event_type, direction, tier, current_price, current_orderflow, features, extra=None):
    if not ENABLE_SIGNAL_CSV_LOG:
        return
    
    try:
        file_exists = os.path.isfile(SIGNAL_CSV_PATH)
        
        with open(SIGNAL_CSV_PATH, 'a', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=[
                'timestamp', 'event_type', 'direction', 'tier', 'price', 
                'delta_btc', 'cvd', 'bid_ask_ratio', 'volume_ratio', 
                'absorption_score', 'extra'
            ])
            
            if not file_exists:
                writer.writeheader()
            
            writer.writerow({
                'timestamp': datetime.now(timezone.utc).isoformat(),
                'event_type': event_type,
                'direction': direction,
                'tier': tier,
                'price': current_price,
                'delta_btc': current_orderflow['delta_btc'],
                'cvd': features['cvd'],
                'bid_ask_ratio': features['bid_ask_ratio'],
                'volume_ratio': features['volume_ratio'],
                'absorption_score': features['absorption_score'],
                'extra': json.dumps(extra) if extra else ''
            })
    except Exception as e:
        print(f"❌ CSV logging error: {e}")


# ============= MARKET SCANNER =============

def send_market_update():
    print("\n" + "="*80)
    print("🔍 SCANNING MARKET - BEAST MODE V3.0")
    print("="*80)
    
    # Fetch fresh orderflow
    current_orderflow = fetch_comprehensive_orderflow()
    if not current_orderflow:
        print("❌ Failed to fetch orderflow")
        return
    
    orderflow_history.append(current_orderflow)
    
    print("\n📊 RAW ORDER FLOW METRICS:")
    print(f"  CVD (Cumulative Delta): {features.get('cvd', 0):.2f}")
    print(f"  Volume Ratio: {features.get('volume_ratio', 1.0):.2f}x")
    print(f"  Delta Severity: {features.get('delta_severity', 0):.2f}")
    print(f"  Bid/Ask Ratio: {features.get('bid_ask_ratio', 1.0):.2f}")
    print(f"  Spread: {features.get('spread_pct', 0):.4f}%")  # FIXED: Use .get()
    print(f"  Absorption Score: {features.get('absorption_score', 0):.0f}/100")
    
    # Fetch candles
    new_candles = fetch_okx_candles(limit=200)
    if not new_candles:
        print("❌ Failed to fetch candles")
        return
    
    candle_history.clear()
    candle_history.extend(new_candles)
    
    current_candle = new_candles[-1]
    current_price = current_candle['close']
    
    # Calculate enhanced features
    features = calculate_orderflow_features(current_orderflow, list(orderflow_history))
    orderflow_feature_history.append(features)
    
    # Detect delta divergence
    divergence = detect_delta_divergence(list(candle_history), features['cvd'])
    
    # 📊 ENHANCED DEBUG INFO
    print(f"\n📊 SYSTEM STATUS:")
    print(f"  Candles: {len(candle_history)} | Orderflow history: {len(orderflow_history)}")
    print(f"  Swing highs: {len(swing_highs)} | Swing lows: {len(swing_lows)}")
    print(f"  Bullish OBs: {len(bullish_order_blocks)} | Bearish OBs: {len(bearish_order_blocks)}")
    print(f"  CVD history: {len(cvd_history)} | Large orders tracked: {len(large_orders_history)}")
    
    print(f"\n💰 CURRENT STATE:")
    print(f"  Price: ${current_price:,.2f}")
    print(f"  Delta: {current_orderflow['delta_btc']:.2f} BTC")
    print(f"  Aggressive Buy: {current_orderflow['aggressive_buy_btc']:.2f} BTC")
    print(f"  Aggressive Sell: {current_orderflow['aggressive_sell_btc']:.2f} BTC")
    
    print(f"\n📈 RAW ORDER FLOW METRICS:")
    print(f"  CVD (Cumulative Delta): {features['cvd']:.2f}")
    print(f"  Volume Ratio: {features['volume_ratio']:.2f}x")
    print(f"  Delta Severity: {features['delta_severity']:.2f}")
    print(f"  Bid/Ask Ratio: {features['bid_ask_ratio']:.2f} ({'BID HEAVY' if features['bid_ask_ratio'] > 1.2 else 'ASK HEAVY' if features['bid_ask_ratio'] < 0.8 else 'BALANCED'})")
    print(f"  Spread: {features.get('spread_pct', 0):.4f}%")  # FIXED: Use .get() with default
    print(f"  Absorption Score: {features['absorption_score']:.0f}/100")
    
    print(f"\n🐋 LARGE ORDER ACTIVITY:")
    print(f"  Total large orders: {features['large_orders_count']}")
    print(f"  Large buys: {features['large_buy_count']} | Large sells: {features['large_sell_count']}")
    
    if divergence:
        print(f"\n⚠️ DELTA DIVERGENCE DETECTED:")
        print(f"  Type: {divergence['type'].upper()}")
        print(f"  {divergence['description']}")
        print(f"  Price change: {divergence['price_change']:.2f}%")
        print(f"  CVD change: {divergence['cvd_change']:.2f}")
        print(f"  Confidence: {divergence['confidence']}")
    
    # Detect swings and create OBs
    new_swings = detect_swing_highs_lows(list(candle_history))
    
    for swing in new_swings:
        if swing['type'] == 'high':
            is_new = True
            for existing in swing_highs:
                if abs(existing['price'] - swing['price']) < 10 and \
                   abs((existing['time'] - swing['time']).total_seconds()) < 3600:
                    is_new = False
                    break
            
            if is_new:
                swing_highs.append(swing)
                print(f"🔺 NEW SWING HIGH at ${swing['price']:,.2f}")
                
                ob = create_order_block_from_swing(swing, list(candle_history))
                if ob:
                    bearish_order_blocks.append(ob)
                    print(f"  📦 Bearish OB: ${ob['bottom']:,.2f} - ${ob['top']:,.2f}")
                    
                    # NEW: Assess OB quality immediately
                    quality = assess_ob_quality(ob, current_orderflow, features)
                    ob_quality_cache[ob['time']] = quality
                    
                    if quality['quality'] == 'LOW':
                        # Send "not quality" alert
                        message = f"""
⚠️ <b>BEARISH ORDER BLOCK DETECTED - LOW QUALITY</b>

<b>OB ZONE:</b>
Top: ${ob['top']:,.2f}
Bottom: ${ob['bottom']:,.2f}

<b>⚠️ ORDERFLOW ANALYSIS - NOT A QUALITY SETUP:</b>
Quality Score: {quality['score']}/100 (LOW)

<b>Issues:</b>
{chr(10).join(quality['reasons'])}

<b>RECOMMENDATION:</b> ❌ <b>SKIP THIS ONE</b>
Wait for stronger orderflow confirmation on next OB.
"""
                        send_telegram(message)
                        log_signal_csv(
                            event_type='ob_formed_low_quality',
                            direction='bearish',
                            tier='SKIP',
                            current_price=current_price,
                            current_orderflow=current_orderflow,
                            features=features,
                            extra={'quality': quality['quality'], 'score': quality['score']}
                        )
                    
                    elif quality['quality'] in ['MEDIUM', 'HIGH']:
                        # Send positive alert
                        message = f"""
✅ <b>BEARISH ORDER BLOCK - {quality['quality']} QUALITY</b>

<b>OB ZONE:</b>
Top: ${ob['top']:,.2f}
Bottom: ${ob['bottom']:,.2f}

<b>📊 ORDERFLOW CONFIRMATION:</b>
Quality Score: {quality['score']}/100 ({quality['quality']})

{chr(10).join(quality['reasons'])}

<b>SETUP:</b> ✅ Watch for retest
Stop: ${ob['top'] + 50:.2f}
Target: ${ob['bottom'] - 200:.2f}
"""
                        send_telegram(message)
                        log_signal_csv(
                            event_type='ob_formed_quality',
                            direction='bearish',
                            tier=quality['quality'],
                            current_price=current_price,
                            current_orderflow=current_orderflow,
                            features=features,
                            extra={'quality': quality['quality'], 'score': quality['score']}
                        )
        
        elif swing['type'] == 'low':
            is_new = True
            for existing in swing_lows:
                if abs(existing['price'] - swing['price']) < 10 and \
                   abs((existing['time'] - swing['time']).total_seconds()) < 3600:
                    is_new = False
                    break
            
            if is_new:
                swing_lows.append(swing)
                print(f"🔻 NEW SWING LOW at ${swing['price']:,.2f}")
                
                ob = create_order_block_from_swing(swing, list(candle_history))
                if ob:
                    bullish_order_blocks.append(ob)
                    print(f"  📦 Bullish OB: ${ob['bottom']:,.2f} - ${ob['top']:,.2f}")
                    
                    # NEW: Assess OB quality immediately
                    quality = assess_ob_quality(ob, current_orderflow, features)
                    ob_quality_cache[ob['time']] = quality
                    
                    if quality['quality'] == 'LOW':
                        # Send "not quality" alert
                        message = f"""
⚠️ <b>BULLISH ORDER BLOCK DETECTED - LOW QUALITY</b>

<b>OB ZONE:</b>
Top: ${ob['top']:,.2f}
Bottom: ${ob['bottom']:,.2f}

<b>⚠️ ORDERFLOW ANALYSIS - NOT A QUALITY SETUP:</b>
Quality Score: {quality['score']}/100 (LOW)

<b>Issues:</b>
{chr(10).join(quality['reasons'])}

<b>RECOMMENDATION:</b> ❌ <b>SKIP THIS ONE</b>
Wait for stronger orderflow confirmation on next OB.
"""
                        send_telegram(message)
                        log_signal_csv(
                            event_type='ob_formed_low_quality',
                            direction='bullish',
                            tier='SKIP',
                            current_price=current_price,
                            current_orderflow=current_orderflow,
                            features=features,
                            extra={'quality': quality['quality'], 'score': quality['score']}
                        )
                    
                    elif quality['quality'] in ['MEDIUM', 'HIGH']:
                        # Send positive alert
                        message = f"""
✅ <b>BULLISH ORDER BLOCK - {quality['quality']} QUALITY</b>

<b>OB ZONE:</b>
Top: ${ob['top']:,.2f}
Bottom: ${ob['bottom']:,.2f}

<b>📊 ORDERFLOW CONFIRMATION:</b>
Quality Score: {quality['score']}/100 ({quality['quality']})

{chr(10).join(quality['reasons'])}

<b>SETUP:</b> ✅ Watch for retest
Stop: ${ob['bottom'] - 50:.2f}
Target: ${ob['top'] + 200:.2f}
"""
                        send_telegram(message)
                        log_signal_csv(
                            event_type='ob_formed_quality',
                            direction='bullish',
                            tier=quality['quality'],
                            current_price=current_price,
                            current_orderflow=current_orderflow,
                            features=features,
                            extra={'quality': quality['quality'], 'score': quality['score']}
                        )
    
    # Check for OB forming (PREDICTION before LuxAlgo shows it)
    ob_forming = check_ob_forming_conditions(current_orderflow, features, current_price, list(candle_history))
    
    if ob_forming:
        cooldown_key = f"ob_forming_{ob_forming['type']}"
        now = time.time()
        last_alert = last_pattern_alerts.get(cooldown_key, 0)
        
        if now - last_alert > 1800:  # 30 min cooldown
            print(f"\n⚡ ORDER BLOCK LIKELY FORMING - {ob_forming['type'].upper()}")
            
            emoji = "🟢" if ob_forming['type'] == 'bullish' else "🔴"
            message = f"""
{emoji} <b>ORDERFLOW PREDICTS {ob_forming['type'].upper()} OB FORMING</b>

<b>📍 AT SWING {ob_forming['pivot']['type'].upper()}:</b>
Pivot Price: ${ob_forming['pivot']['price']:,.2f}
Expected OB Zone: {ob_forming['expected_ob_zone']}

<b>📊 ORDERFLOW SIGNALS:</b>
Delta: {current_orderflow['delta_btc']:.2f} BTC
Bid/Ask: {features['bid_ask_ratio']:.2f}
CVD: {features['cvd']:.2f}
Absorption: {features['absorption_score']}/100

<b>Conditions Met:</b>
{chr(10).join([f"{'✅' if v else '❌'} {k.replace('_', ' ').title()}" for k, v in ob_forming['conditions'].items()])}

<b>Confidence:</b> {ob_forming['confidence']*100:.0f}%

<b>WHAT THIS MEANS:</b>
{"🟢 Bullish OB may form soon - watch for price to sweep low then reverse UP" if ob_forming['type'] == 'bullish' else "🔴 Bearish OB may form soon - watch for price to sweep high then reverse DOWN"}

This is a PREDICTION - OB not yet confirmed on LuxAlgo indicator.
"""
            send_telegram(message)
            last_pattern_alerts[cooldown_key] = now
            log_signal_csv(
                event_type='ob_prediction',
                direction=ob_forming['type'],
                tier='PREDICTION',
                current_price=current_price,
                current_orderflow=current_orderflow,
                features=features,
                extra={'confidence': ob_forming['confidence']}
            )
    
    # Check OB retests
    for ob in list(bullish_order_blocks):
        ob_strength = check_ob_strength_at_retest(ob, current_orderflow, current_price)
        
        if ob_strength['in_zone'] and ob_strength['prediction'] in ['WILL_HOLD', 'WILL_BREAK']:
            cooldown_key = f"ob_retest_bullish_{ob['time']}"
            now = time.time()
            last_alert = last_pattern_alerts.get(cooldown_key, 0)
            
            if now - last_alert > 1800:
                print(f"\n📍 Price at Bullish OB - {ob_strength['prediction']}")
                
                message = f"""
📍 <b>PRICE AT BULLISH ORDER BLOCK</b>

<b>OB ZONE:</b>
Top: ${ob['top']:,.2f}
Bottom: ${ob['bottom']:,.2f}
Current: ${current_price:,.2f}

<b>📊 ORDER FLOW ANALYSIS:</b>
Delta: {current_orderflow['delta_btc']:.2f} BTC
CVD: {features['cvd']:.2f}
Bid/Ask: {features['bid_ask_ratio']:.2f}
Absorption: {features['absorption_score']:.0f}/100

<b>PREDICTION:</b> <b>{ob_strength['prediction']}</b>
Confidence: {ob_strength['confidence']}
Reason: {ob_strength['reason']}

<b>WHAT TO DO:</b>
{"✅ ENTER LONG - Strong support confirmed" if ob_strength['prediction'] == 'WILL_HOLD' else "❌ AVOID LONG - Support breaking"}
{"Stop: ${ob['bottom']:,.2f}" if ob_strength['prediction'] == 'WILL_HOLD' else "Wait for new setup"}
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
            
            if now - last_alert > 1800:
                print(f"\n📍 Price at Bearish OB - {ob_strength['prediction']}")
                
                message = f"""
📍 <b>PRICE AT BEARISH ORDER BLOCK</b>

<b>OB ZONE:</b>
Top: ${ob['top']:,.2f}
Bottom: ${ob['bottom']:,.2f}
Current: ${current_price:,.2f}

<b>📊 ORDER FLOW ANALYSIS:</b>
Delta: {current_orderflow['delta_btc']:.2f} BTC
CVD: {features['cvd']:.2f}
Bid/Ask: {features['bid_ask_ratio']:.2f}
Absorption: {features['absorption_score']:.0f}/100

<b>PREDICTION:</b> <b>{ob_strength['prediction']}</b>
Confidence: {ob_strength['confidence']}
Reason: {ob_strength['reason']}

<b>WHAT TO DO:</b>
{"✅ ENTER SHORT - Strong resistance confirmed" if ob_strength['prediction'] == 'WILL_HOLD' else "❌ AVOID SHORT - Resistance breaking"}
{"Stop: ${ob['top']:,.2f}" if ob_strength['prediction'] == 'WILL_HOLD' else "Wait for new setup"}
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
    
    # Send divergence alert if detected
    if divergence and divergence['confidence'] == 'HIGH':
        cooldown_key = f"divergence_{divergence['type']}"
        now = time.time()
        last_alert = last_pattern_alerts.get(cooldown_key, 0)
        
        if now - last_alert > 3600:  # 1 hour cooldown for divergences
            message = f"""
⚠️ <b>DELTA DIVERGENCE DETECTED</b>

<b>Type:</b> {divergence['type'].upper()}
<b>Price:</b> ${current_price:,.2f}

<b>ANALYSIS:</b>
{divergence['description']}

Price change: {divergence['price_change']:.2f}%
CVD change: {divergence['cvd_change']:.2f}
Confidence: {divergence['confidence']}

<b>INTERPRETATION:</b>
{"⚠️ Bullish divergence suggests potential reversal UP" if divergence['type'] == 'bullish' else "⚠️ Bearish divergence suggests potential reversal DOWN"}

Watch for {' confirmation at nearby order blocks!' if bullish_order_blocks or bearish_order_blocks else 'price action confirmation!'}
"""
            
            send_telegram(message)
            last_pattern_alerts[cooldown_key] = now
    
    print("\n✅ Market scan complete")
    print("="*80 + "\n")


# ============= MONITORING THREAD =============
def monitor_orderflow():
    print("🔄 Enhanced orderflow scanning started")
    
    while True:
        try:
            send_market_update()
            time.sleep(ORDERFLOW_INTERVAL)
        except Exception as e:
            print(f"❌ Scanning error: {e}")
            import traceback
            traceback.print_exc()
            time.sleep(ORDERFLOW_INTERVAL)


# ============= FLASK APP =============
app = Flask(__name__)

@app.route('/', methods=['GET'])
def home():
    return f"""
    <h1>🚀 Beast Mode V3.0 - FULL ORDER FLOW EDITION</h1>
    
    <h2>📊 RAW ORDER FLOW DATA TRACKED:</h2>
    <ul>
        <li><b>✅ Delta (Aggressive Buy - Sell)</b></li>
        <li><b>✅ CVD (Cumulative Volume Delta)</b> - {len(cvd_history)} periods tracked</li>
        <li><b>✅ Order Book Depth</b> - {ORDERBOOK_DEPTH_LEVELS} levels from multiple exchanges</li>
        <li><b>✅ Bid/Ask Imbalance</b> - Real-time ratio tracking</li>
        <li><b>✅ Large Order Detection</b> - Whale tracking ≥{LARGE_ORDER_THRESHOLD_BTC} BTC</li>
        <li><b>✅ Delta Divergences</b> - Price vs CVD divergence detection</li>
        <li><b>✅ Absorption Score</b> - Measures volume absorption at levels</li>
        <li><b>✅ Spread Analysis</b> - Market liquidity monitoring</li>
        <li><b>✅ Aggressive vs Passive Flow</b> - Taker buy/sell separation</li>
        <li><b>✅ Volume Ratios</b> - Current vs historical comparison</li>
    </ul>
    
    <h2>🆕 V3.0 ENHANCEMENTS:</h2>
    <ul>
        <li><b>✅ FIXED spread_pct KeyError</b></li>
        <li><b>✅ OB Prediction Signals</b> - Orderflow predicts OB BEFORE LuxAlgo shows it</li>
        <li><b>✅ Quality Assessment</b> - Alerts when OB forms but isn't worth taking</li>
        <li><b>✅ Retest Analysis</b> - Real-time orderflow verdict on OB holds/breaks</li>
        <li>Real order book depth from OKX + Binance</li>
        <li>Actual aggressive taker data (not 50/50 approximations)</li>
        <li>CVD tracking and divergence detection</li>
        <li>Large order / whale tracking</li>
    </ul>
    
    <h2>📈 CURRENT STATS:</h2>
    <ul>
        <li>CVD History: {len(cvd_history)} periods</li>
        <li>Large Orders Tracked: {len(large_orders_history)}</li>
        <li>Order Book Snapshots: {len(orderbook_snapshots)}</li>
        <li>Delta Divergences Found: {len(delta_divergences)}</li>
        <li>Scan Interval: {ORDERFLOW_INTERVAL/60:.0f} minutes</li>
    </ul>
    
    <p><b>Status:</b> Scanning with FULL order flow data! 🎯</p>
    """, 200


if __name__ == '__main__':
    print("🚀 Beast Mode V3.0 - FULL ORDER FLOW EDITION")
    print("="*80)
    print("📊 COMPLETE ORDER FLOW TRACKING:")
    print("  ✅ Delta (real aggressive taker data)")
    print("  ✅ CVD (Cumulative Volume Delta)")
    print("  ✅ Order Book Depth (20 levels)")
    print("  ✅ Bid/Ask Imbalance")
    print("  ✅ Large Order Detection (≥5 BTC)")
    print("  ✅ Delta Divergences")
    print("  ✅ Absorption Scoring")
    print("  ✅ Spread Analysis")
    print("="*80)
    print("\n🆕 V3.0 NEW FEATURES:")
    print("  ✅ FIXED: spread_pct KeyError")
    print("  ✅ OB Prediction (before LuxAlgo indicator)")
    print("  ✅ Quality Assessment (skip bad OBs)")
    print("  ✅ Retest Orderflow Analysis")
    print("="*80)
    print("\n🎯 DATA SOURCES:")
    print("  OKX: Trades + Order Book")
    print("  Binance: Trades + Order Book")
    print("  Multi-exchange aggregation")
    print("="*80)
    print(f"\n📊 SETTINGS:")
    print(f"  Scan Interval: {ORDERFLOW_INTERVAL/60:.0f} minutes")
    print(f"  CVD Lookback: {CVD_LOOKBACK} periods")
    print(f"  Large Order Threshold: {LARGE_ORDER_THRESHOLD_BTC} BTC")
    print(f"  Order Book Depth: {ORDERBOOK_DEPTH_LEVELS} levels")
    print(f"  Run Web: {RUN_WEB}")
    print(f"  Run Scanner: {RUN_SCANNER}")
    print("="*80 + "\n")
    
    if RUN_SCANNER:
        orderflow_thread = threading.Thread(target=monitor_orderflow, daemon=True)
        orderflow_thread.start()
        
        if SEND_STARTUP_TELEGRAM:
            send_telegram(f"""
🚀 <b>Beast Mode V3.0 - FULL ORDER FLOW Started!</b>

<b>📊 COMPLETE ORDER FLOW DATA:</b>
✅ Delta (real taker data, not approximations!)
✅ CVD (Cumulative Volume Delta)
✅ Order Book Depth ({ORDERBOOK_DEPTH_LEVELS} levels)
✅ Bid/Ask Imbalance
✅ Large Order Detection (≥{LARGE_ORDER_THRESHOLD_BTC} BTC)
✅ Delta Divergences (price vs CVD)
✅ Absorption Scoring
✅ Spread Analysis

<b>🆕 V3.0 ENHANCEMENTS:</b>
✅ <b>FIXED spread_pct error</b>
✅ <b>OB Prediction</b> - Get signals BEFORE LuxAlgo shows OB
✅ <b>Quality Filter</b> - Alerts when OB isn't worth taking
✅ <b>Retest Analysis</b> - Orderflow verdict on holds/breaks

<b>🎯 DATA SOURCES:</b>
• OKX: Real trades + order book
• Binance: Real trades + order book
• Multi-exchange aggregation

<b>⚡ YOU'LL NOW SEE:</b>
• Predictions when OB is about to form
• Alerts if OB forms but orderflow doesn't support it
• Real-time analysis when price retests OBs

No more guessing - full institutional-grade data! 🔥

Scanning every {ORDERFLOW_INTERVAL/60:.0f} minutes!
            """)
    else:
        print("ℹ️ Scanner disabled (RUN_SCANNER=0)")
    
    if RUN_WEB:
        port = int(os.environ.get('PORT', 10000))
        app.run(host='0.0.0.0', port=port)
    elif RUN_SCANNER:
        print("🔁 Scanner-only mode active")
        while True:
            time.sleep(60)
    else:
        print("⚠️ Both RUN_WEB and RUN_SCANNER disabled; exiting.")
