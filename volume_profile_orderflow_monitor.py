import os
import time
import requests
from datetime import datetime, timezone, timedelta
from flask import Flask, request, jsonify
import threading
import json
import csv
from collections import deque, defaultdict
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

# ============= VOLUME PROFILE SETTINGS =============
VP_LOOKBACK_CANDLES = 96  # 24 hours of 15min candles
VP_PRICE_LEVELS = 50  # Number of price levels to bucket volume
POC_PROXIMITY_PCT = 0.3  # Consider "at POC" if within 0.3%
VALUE_AREA_PERCENT = 70  # 70% of volume = value area

# ============= ORDER FLOW SETTINGS =============
CVD_LOOKBACK = 100
LARGE_ORDER_THRESHOLD_BTC = 5.0
ORDERBOOK_DEPTH_LEVELS = 20
ABSORPTION_THRESHOLD_BTC = 10.0  # Heavy orderflow threshold

# ============= APP SETTINGS =============
RUN_WEB = os.environ.get('RUN_WEB', '1') == '1'
RUN_SCANNER = os.environ.get('RUN_SCANNER', '1') == '1'
SEND_STARTUP_TELEGRAM = os.environ.get('SEND_STARTUP_TELEGRAM', '1') == '1'
ENABLE_SIGNAL_CSV_LOG = os.environ.get('ENABLE_SIGNAL_CSV_LOG', '1') == '1'
SIGNAL_CSV_PATH = os.environ.get('SIGNAL_CSV_PATH', 'vp_orderflow_signals.csv')

# ============= DATA STRUCTURES =============
orderflow_history = deque(maxlen=100)
candle_history = deque(maxlen=200)
cvd_history = deque(maxlen=CVD_LOOKBACK)
large_orders_history = deque(maxlen=50)
orderbook_snapshots = deque(maxlen=20)

# Volume Profile tracking
volume_profile_history = deque(maxlen=10)  # Track last 10 profiles
current_volume_profile = None
last_poc_price = None
poc_control_history = deque(maxlen=20)  # Track control at POC over time

# Alert cooldowns
last_pattern_alerts = {}

# ============= EXCHANGE API FUNCTIONS =============

def fetch_okx_orderbook():
    """Fetch order book from OKX"""
    try:
        url = "https://www.okx.com/api/v5/market/books"
        params = {"instId": OKX_INST_ID, "sz": ORDERBOOK_DEPTH_LEVELS}
        response = requests.get(url, params=params, timeout=10)
        data = response.json()
        
        if data.get('code') == '0' and data.get('data'):
            book = data['data'][0]
            bids = [[float(b[0]), float(b[1])] for b in book['bids']]
            asks = [[float(a[0]), float(a[1])] for a in book['asks']]
            
            total_bid_volume = sum(b[1] for b in bids)
            total_ask_volume = sum(a[1] for a in asks)
            
            best_bid = bids[0][0] if bids else 0
            best_ask = asks[0][0] if asks else 0
            spread = best_ask - best_bid if best_bid and best_ask else 0
            spread_pct = (spread / best_bid) * 100 if best_bid else 0
            
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
    """Fetch recent trades from OKX"""
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
                
                if side == 'buy':
                    aggressive_buys += size
                else:
                    aggressive_sells += size
                
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


def fetch_kucoin_orderbook():
    """Fetch order book from KuCoin"""
    try:
        url = "https://api.kucoin.com/api/v1/market/orderbook/level2_20"
        params = {"symbol": "BTC-USDT"}
        response = requests.get(url, params=params, timeout=10)
        data = response.json()
        
        if data.get('code') == '200000' and data.get('data'):
            book = data['data']
            bids = [[float(b[0]), float(b[1])] for b in book['bids'][:ORDERBOOK_DEPTH_LEVELS]]
            asks = [[float(a[0]), float(a[1])] for a in book['asks'][:ORDERBOOK_DEPTH_LEVELS]]
            
            total_bid_volume = sum(b[1] for b in bids)
            total_ask_volume = sum(a[1] for a in asks)
            
            best_bid = bids[0][0] if bids else 0
            best_ask = asks[0][0] if asks else 0
            spread = best_ask - best_bid if best_bid and best_ask else 0
            spread_pct = (spread / best_bid) * 100 if best_bid else 0
            
            return {
                'exchange': 'kucoin',
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
        print(f"❌ KuCoin orderbook error: {e}")
    return None


def fetch_kucoin_trades():
    """Fetch recent trades from KuCoin"""
    try:
        url = "https://api.kucoin.com/api/v1/market/histories"
        params = {"symbol": "BTC-USDT"}
        response = requests.get(url, params=params, timeout=10)
        data = response.json()
        
        if data.get('code') == '200000' and data.get('data'):
            trades = data['data']
            
            aggressive_buys = 0
            aggressive_sells = 0
            large_orders = []
            
            for trade in trades:
                size = float(trade['size'])
                price = float(trade['price'])
                side = trade['side']
                timestamp = int(trade['time']) / 1000000000
                
                if side == 'buy':
                    aggressive_buys += size
                else:
                    aggressive_sells += size
                
                if size >= LARGE_ORDER_THRESHOLD_BTC:
                    large_orders.append({
                        'size': size,
                        'price': price,
                        'side': side,
                        'timestamp': timestamp
                    })
            
            return {
                'exchange': 'kucoin',
                'aggressive_buy_btc': aggressive_buys,
                'aggressive_sell_btc': aggressive_sells,
                'delta_btc': aggressive_buys - aggressive_sells,
                'large_orders': large_orders,
                'total_trades': len(trades)
            }
    except Exception as e:
        print(f"❌ KuCoin trades error: {e}")
    return None


def fetch_gateio_orderbook():
    """Fetch order book from Gate.io"""
    try:
        url = "https://api.gateio.ws/api/v4/spot/order_book"
        params = {"currency_pair": "BTC_USDT", "limit": ORDERBOOK_DEPTH_LEVELS}
        response = requests.get(url, params=params, timeout=10)
        data = response.json()
        
        if 'bids' in data and 'asks' in data:
            bids = [[float(b[0]), float(b[1])] for b in data['bids'][:ORDERBOOK_DEPTH_LEVELS]]
            asks = [[float(a[0]), float(a[1])] for a in data['asks'][:ORDERBOOK_DEPTH_LEVELS]]
            
            total_bid_volume = sum(b[1] for b in bids)
            total_ask_volume = sum(a[1] for a in asks)
            
            best_bid = bids[0][0] if bids else 0
            best_ask = asks[0][0] if asks else 0
            spread = best_ask - best_bid if best_bid and best_ask else 0
            spread_pct = (spread / best_bid) * 100 if best_bid else 0
            
            return {
                'exchange': 'gateio',
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
        print(f"❌ Gate.io orderbook error: {e}")
    return None


def fetch_gateio_trades():
    """Fetch recent trades from Gate.io"""
    try:
        url = "https://api.gateio.ws/api/v4/spot/trades"
        params = {"currency_pair": "BTC_USDT", "limit": 100}
        response = requests.get(url, params=params, timeout=10)
        data = response.json()
        
        if isinstance(data, list):
            aggressive_buys = 0
            aggressive_sells = 0
            large_orders = []
            
            for trade in data:
                size = float(trade['amount'])
                price = float(trade['price'])
                side = trade['side']
                timestamp = int(trade['create_time'])
                
                if side == 'buy':
                    aggressive_buys += size
                else:
                    aggressive_sells += size
                
                if size >= LARGE_ORDER_THRESHOLD_BTC:
                    large_orders.append({
                        'size': size,
                        'price': price,
                        'side': side,
                        'timestamp': timestamp
                    })
            
            return {
                'exchange': 'gateio',
                'aggressive_buy_btc': aggressive_buys,
                'aggressive_sell_btc': aggressive_sells,
                'delta_btc': aggressive_buys - aggressive_sells,
                'large_orders': large_orders,
                'total_trades': len(data)
            }
    except Exception as e:
        print(f"❌ Gate.io trades error: {e}")
    return None


def fetch_comprehensive_orderflow():
    """Fetch complete order flow data from multiple exchanges"""
    
    okx_trades = fetch_okx_trades()
    kucoin_trades = fetch_kucoin_trades()
    gateio_trades = fetch_gateio_trades()
    
    okx_book = fetch_okx_orderbook()
    kucoin_book = fetch_kucoin_orderbook()
    gateio_book = fetch_gateio_orderbook()
    
    total_aggressive_buy = 0
    total_aggressive_sell = 0
    all_large_orders = []
    exchanges_used = []
    
    if okx_trades:
        total_aggressive_buy += okx_trades['aggressive_buy_btc']
        total_aggressive_sell += okx_trades['aggressive_sell_btc']
        all_large_orders.extend(okx_trades['large_orders'])
        exchanges_used.append('OKX')
    
    if kucoin_trades:
        total_aggressive_buy += kucoin_trades['aggressive_buy_btc']
        total_aggressive_sell += kucoin_trades['aggressive_sell_btc']
        all_large_orders.extend(kucoin_trades['large_orders'])
        exchanges_used.append('KuCoin')
    
    if gateio_trades:
        total_aggressive_buy += gateio_trades['aggressive_buy_btc']
        total_aggressive_sell += gateio_trades['aggressive_sell_btc']
        all_large_orders.extend(gateio_trades['large_orders'])
        exchanges_used.append('Gate.io')
    
    delta_btc = total_aggressive_buy - total_aggressive_sell
    
    bid_ask_ratios = []
    spreads = []
    
    if okx_book:
        if okx_book['total_ask_volume'] > 0:
            ratio = okx_book['total_bid_volume'] / okx_book['total_ask_volume']
            bid_ask_ratios.append(ratio)
        spreads.append(okx_book['spread_pct'])
        orderbook_snapshots.append(okx_book)
    
    if kucoin_book:
        if kucoin_book['total_ask_volume'] > 0:
            ratio = kucoin_book['total_bid_volume'] / kucoin_book['total_ask_volume']
            bid_ask_ratios.append(ratio)
        spreads.append(kucoin_book['spread_pct'])
        orderbook_snapshots.append(kucoin_book)
    
    if gateio_book:
        if gateio_book['total_ask_volume'] > 0:
            ratio = gateio_book['total_bid_volume'] / gateio_book['total_ask_volume']
            bid_ask_ratios.append(ratio)
        spreads.append(gateio_book['spread_pct'])
        orderbook_snapshots.append(gateio_book)
    
    avg_bid_ask_ratio = statistics.mean(bid_ask_ratios) if bid_ask_ratios else 1.0
    avg_spread = statistics.mean(spreads) if spreads else 0.0
    
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
        'timestamp': datetime.now(timezone.utc),
        'exchanges_used': exchanges_used
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
    return None


# ============= VOLUME PROFILE CALCULATION =============

def calculate_volume_profile(candles, num_levels=VP_PRICE_LEVELS):
    """
    Calculate volume profile from candle data
    Returns: POC, VAH, VAL, and full profile distribution
    """
    if len(candles) < 10:
        return None
    
    # Get price range
    all_highs = [c['high'] for c in candles]
    all_lows = [c['low'] for c in candles]
    price_high = max(all_highs)
    price_low = min(all_lows)
    price_range = price_high - price_low
    
    if price_range == 0:
        return None
    
    # Create price levels (buckets)
    tick_size = price_range / num_levels
    price_levels = {}
    
    for i in range(num_levels):
        level_price = price_low + (i * tick_size)
        price_levels[level_price] = 0
    
    # Distribute volume across price levels
    for candle in candles:
        candle_range = candle['high'] - candle['low']
        if candle_range == 0:
            # All volume at close price
            closest_level = min(price_levels.keys(), key=lambda x: abs(x - candle['close']))
            price_levels[closest_level] += candle['volume']
        else:
            # Distribute volume proportionally across the candle's range
            for level in price_levels.keys():
                if candle['low'] <= level <= candle['high']:
                    # Volume distributed evenly across touched levels
                    price_levels[level] += candle['volume'] / max(1, (candle['high'] - candle['low']) / tick_size)
    
    # Find POC (Point of Control) - price with most volume
    poc_price = max(price_levels, key=price_levels.get)
    poc_volume = price_levels[poc_price]
    
    # Calculate Value Area (70% of total volume)
    total_volume = sum(price_levels.values())
    value_area_volume = total_volume * (VALUE_AREA_PERCENT / 100)
    
    # Sort levels by volume
    sorted_levels = sorted(price_levels.items(), key=lambda x: x[1], reverse=True)
    
    # Build value area starting from POC
    va_levels = [poc_price]
    accumulated_volume = poc_volume
    
    for price, volume in sorted_levels:
        if price == poc_price:
            continue
        if accumulated_volume >= value_area_volume:
            break
        va_levels.append(price)
        accumulated_volume += volume
    
    # Value Area High/Low
    vah = max(va_levels)
    val = min(va_levels)
    
    return {
        'poc': poc_price,
        'vah': vah,
        'val': val,
        'total_volume': total_volume,
        'value_area_volume': accumulated_volume,
        'price_levels': price_levels,
        'timestamp': datetime.now(timezone.utc)
    }


def is_price_near_poc(price, poc, tolerance_pct=POC_PROXIMITY_PCT):
    """Check if price is near POC"""
    tolerance = poc * (tolerance_pct / 100)
    return abs(price - poc) <= tolerance


def analyze_poc_control(vp, current_orderflow, current_price):
    """
    Analyze who's in control at the POC
    Returns control analysis with actionable insights
    """
    if not vp:
        return None
    
    poc = vp['poc']
    at_poc = is_price_near_poc(current_price, poc)
    
    delta = current_orderflow['delta_btc']
    bid_ask = current_orderflow['bid_ask_ratio']
    
    # Determine control
    control = 'NEUTRAL'
    confidence = 'LOW'
    action = 'WAIT'
    reasoning = []
    
    if at_poc:
        # Price IS at POC - this is where the analysis matters most
        
        # Strong buying at POC
        if delta > ABSORPTION_THRESHOLD_BTC:
            if bid_ask > 1.3:
                control = 'BUYERS_ABSORBING'
                confidence = 'HIGH'
                action = 'BULLISH_CONTROL'
                reasoning.append(f"Heavy buying ({delta:.1f} BTC) AT POC")
                reasoning.append(f"Bid support strong ({bid_ask:.2f})")
                reasoning.append("Buyers defending fair value aggressively")
            else:
                control = 'BUYERS_ACTIVE'
                confidence = 'MEDIUM'
                action = 'BULLISH_LEAN'
                reasoning.append(f"Strong buying ({delta:.1f} BTC) at POC")
                reasoning.append("But orderbook not confirming strength")
        
        # Strong selling at POC
        elif delta < -ABSORPTION_THRESHOLD_BTC:
            if bid_ask < 0.7:
                control = 'SELLERS_ABSORBING'
                confidence = 'HIGH'
                action = 'BEARISH_CONTROL'
                reasoning.append(f"Heavy selling ({delta:.1f} BTC) AT POC")
                reasoning.append(f"Ask pressure strong ({bid_ask:.2f})")
                reasoning.append("Sellers rejecting fair value aggressively")
            else:
                control = 'SELLERS_ACTIVE'
                confidence = 'MEDIUM'
                action = 'BEARISH_LEAN'
                reasoning.append(f"Strong selling ({delta:.1f} BTC) at POC")
                reasoning.append("But orderbook not confirming strength")
        
        # Moderate buying
        elif delta > 5:
            control = 'BUYERS_PRESENT'
            confidence = 'MEDIUM'
            action = 'BULLISH_LEAN'
            reasoning.append(f"Buying flow ({delta:.1f} BTC) at POC")
        
        # Moderate selling
        elif delta < -5:
            control = 'SELLERS_PRESENT'
            confidence = 'MEDIUM'
            action = 'BEARISH_LEAN'
            reasoning.append(f"Selling flow ({delta:.1f} BTC) at POC")
        
        else:
            control = 'BALANCED'
            confidence = 'LOW'
            action = 'WAIT'
            reasoning.append(f"Neutral delta ({delta:.1f} BTC) at POC")
            reasoning.append("No clear control - market in balance")
    
    else:
        # Price NOT at POC - where is it relative to POC?
        if current_price > poc:
            position = 'ABOVE_POC'
            if delta > 5:
                control = 'BUYERS_IN_CONTROL'
                confidence = 'MEDIUM'
                action = 'BULLISH_CONTROL'
                reasoning.append(f"Price above fair value (POC: ${poc:,.0f})")
                reasoning.append(f"Buyers pushing higher ({delta:.1f} BTC)")
            else:
                control = 'PRICE_ELEVATED'
                confidence = 'LOW'
                action = 'NEUTRAL'
                reasoning.append(f"Price above POC but weak buying")
        else:
            position = 'BELOW_POC'
            if delta < -5:
                control = 'SELLERS_IN_CONTROL'
                confidence = 'MEDIUM'
                action = 'BEARISH_CONTROL'
                reasoning.append(f"Price below fair value (POC: ${poc:,.0f})")
                reasoning.append(f"Sellers pushing lower ({delta:.1f} BTC)")
            else:
                control = 'PRICE_DEPRESSED'
                confidence = 'LOW'
                action = 'NEUTRAL'
                reasoning.append(f"Price below POC but weak selling")
    
    return {
        'at_poc': at_poc,
        'poc_price': poc,
        'current_price': current_price,
        'control': control,
        'confidence': confidence,
        'action': action,
        'delta': delta,
        'bid_ask_ratio': bid_ask,
        'reasoning': reasoning,
        'vah': vp['vah'],
        'val': vp['val']
    }


def detect_poc_shift(current_vp, previous_vp):
    """Detect if POC has shifted (value moving)"""
    if not current_vp or not previous_vp:
        return None
    
    current_poc = current_vp['poc']
    previous_poc = previous_vp['poc']
    
    shift_pct = ((current_poc - previous_poc) / previous_poc) * 100
    
    if abs(shift_pct) > 0.5:  # POC moved more than 0.5%
        direction = 'UP' if shift_pct > 0 else 'DOWN'
        significance = 'MAJOR' if abs(shift_pct) > 1.0 else 'MODERATE'
        
        return {
            'shifted': True,
            'direction': direction,
            'significance': significance,
            'previous_poc': previous_poc,
            'current_poc': current_poc,
            'shift_pct': shift_pct
        }
    
    return {'shifted': False}


# ============= ORDER FLOW ANALYSIS =============

def calculate_orderflow_features(current_orderflow, orderflow_hist):
    """Calculate advanced order flow features"""
    
    cvd = sum(o['delta_btc'] for o in orderflow_hist)
    cvd_history.append(cvd)
    
    recent_volumes = [abs(o['aggressive_buy_btc'] + o['aggressive_sell_btc']) 
                      for o in orderflow_hist[-20:]]
    current_volume = abs(current_orderflow['aggressive_buy_btc'] + 
                         current_orderflow['aggressive_sell_btc'])
    avg_volume = statistics.mean(recent_volumes) if recent_volumes else 1.0
    volume_ratio = current_volume / avg_volume if avg_volume > 0 else 1.0
    
    recent_deltas = [abs(o['delta_btc']) for o in orderflow_hist[-20:]]
    avg_delta = statistics.mean(recent_deltas) if recent_deltas else 1.0
    delta_severity = abs(current_orderflow['delta_btc']) / avg_delta if avg_delta > 0 else 1.0
    
    absorption_score = min(100, (volume_ratio * delta_severity) * 20)
    
    return {
        'cvd': cvd,
        'volume_ratio': volume_ratio,
        'delta_severity': delta_severity,
        'bid_ask_ratio': current_orderflow.get('bid_ask_ratio', 1.0),
        'spread_pct': current_orderflow.get('spread_pct', 0.0),
        'absorption_score': absorption_score,
        'large_orders_count': current_orderflow.get('large_orders_count', 0),
        'large_buy_count': current_orderflow.get('large_buy_count', 0),
        'large_sell_count': current_orderflow.get('large_sell_count', 0)
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


def log_signal_csv(event_type, control_state, current_price, current_orderflow, features, vp_data, extra=None):
    if not ENABLE_SIGNAL_CSV_LOG:
        return
    
    try:
        file_exists = os.path.isfile(SIGNAL_CSV_PATH)
        
        with open(SIGNAL_CSV_PATH, 'a', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=[
                'timestamp', 'event_type', 'control_state', 'price', 'poc', 
                'delta_btc', 'cvd', 'bid_ask_ratio', 'absorption_score',
                'vah', 'val', 'extra'
            ])
            
            if not file_exists:
                writer.writeheader()
            
            writer.writerow({
                'timestamp': datetime.now(timezone.utc).isoformat(),
                'event_type': event_type,
                'control_state': control_state,
                'price': current_price,
                'poc': vp_data.get('poc', 0) if vp_data else 0,
                'delta_btc': current_orderflow['delta_btc'],
                'cvd': features['cvd'],
                'bid_ask_ratio': features['bid_ask_ratio'],
                'absorption_score': features['absorption_score'],
                'vah': vp_data.get('vah', 0) if vp_data else 0,
                'val': vp_data.get('val', 0) if vp_data else 0,
                'extra': json.dumps(extra) if extra else ''
            })
    except Exception as e:
        print(f"❌ CSV logging error: {e}")


# ============= MARKET SCANNER =============

def send_market_update():
    global current_volume_profile, last_poc_price
    
    print("\n" + "="*80)
    print("🔍 VOLUME PROFILE + ORDER FLOW MONITOR")
    print("="*80)
    
    # Fetch orderflow
    current_orderflow = fetch_comprehensive_orderflow()
    if not current_orderflow:
        print("❌ Failed to fetch orderflow")
        return
    
    orderflow_history.append(current_orderflow)
    
    # Fetch candles
    new_candles = fetch_okx_candles(limit=VP_LOOKBACK_CANDLES)
    if not new_candles:
        print("❌ Failed to fetch candles")
        return
    
    candle_history.clear()
    candle_history.extend(new_candles)
    
    current_candle = new_candles[-1]
    current_price = current_candle['close']
    
    # Calculate orderflow features
    features = calculate_orderflow_features(current_orderflow, list(orderflow_history))
    
    # Calculate Volume Profile
    previous_vp = current_volume_profile
    current_volume_profile = calculate_volume_profile(new_candles[-VP_LOOKBACK_CANDLES:])
    
    if current_volume_profile:
        volume_profile_history.append(current_volume_profile)
    
    print(f"\n💰 CURRENT STATE:")
    print(f"  Price: ${current_price:,.2f}")
    print(f"  Exchanges: {', '.join(current_orderflow.get('exchanges_used', []))}")
    
    print(f"\n📊 VOLUME PROFILE:")
    if current_volume_profile:
        print(f"  POC: ${current_volume_profile['poc']:,.2f}")
        print(f"  VAH: ${current_volume_profile['vah']:,.2f}")
        print(f"  VAL: ${current_volume_profile['val']:,.2f}")
        print(f"  Total Volume: {current_volume_profile['total_volume']:,.0f}")
        
        distance_from_poc = ((current_price - current_volume_profile['poc']) / current_volume_profile['poc']) * 100
        print(f"  Distance from POC: {distance_from_poc:+.2f}%")
    else:
        print("  ⚠️ Volume Profile not available")
    
    print(f"\n📈 ORDER FLOW:")
    print(f"  Delta: {current_orderflow['delta_btc']:+.2f} BTC")
    print(f"  Aggressive Buy: {current_orderflow['aggressive_buy_btc']:.2f} BTC")
    print(f"  Aggressive Sell: {current_orderflow['aggressive_sell_btc']:.2f} BTC")
    print(f"  CVD: {features['cvd']:+.2f}")
    print(f"  Bid/Ask: {features['bid_ask_ratio']:.2f} ({'BID' if features['bid_ask_ratio'] > 1.2 else 'ASK' if features['bid_ask_ratio'] < 0.8 else 'BALANCED'})")
    print(f"  Absorption: {features['absorption_score']:.0f}/100")
    print(f"  Large Orders: {features['large_orders_count']} (Buy: {features['large_buy_count']}, Sell: {features['large_sell_count']})")
    
    # Analyze POC Control
    if current_volume_profile:
        poc_analysis = analyze_poc_control(current_volume_profile, current_orderflow, current_price)
        
        if poc_analysis:
            print(f"\n🎯 POC CONTROL ANALYSIS:")
            print(f"  Status: {'AT POC' if poc_analysis['at_poc'] else 'Away from POC'}")
            print(f"  Control: {poc_analysis['control']}")
            print(f"  Confidence: {poc_analysis['confidence']}")
            print(f"  Action: {poc_analysis['action']}")
            print(f"  Reasoning:")
            for reason in poc_analysis['reasoning']:
                print(f"    • {reason}")
            
            poc_control_history.append(poc_analysis)
            
            # Send Telegram alert for significant POC events
            if poc_analysis['at_poc'] and poc_analysis['confidence'] in ['HIGH', 'MEDIUM']:
                cooldown_key = f"poc_control_{poc_analysis['control']}"
                now = time.time()
                last_alert = last_pattern_alerts.get(cooldown_key, 0)
                
                if now - last_alert > 1800:  # 30 min cooldown
                    
                    emoji_map = {
                        'BUYERS_ABSORBING': '🟢🔥',
                        'SELLERS_ABSORBING': '🔴🔥',
                        'BUYERS_ACTIVE': '🟢',
                        'SELLERS_ACTIVE': '🔴',
                        'BUYERS_PRESENT': '🟢',
                        'SELLERS_PRESENT': '🔴',
                        'BALANCED': '⚪'
                    }
                    
                    emoji = emoji_map.get(poc_analysis['control'], '⚪')
                    
                    message = f"""
{emoji} <b>POC CONTROL DETECTED</b>

<b>📍 AT POINT OF CONTROL:</b>
POC: ${poc_analysis['poc_price']:,.2f}
Current: ${poc_analysis['current_price']:,.2f}

<b>🎯 CONTROL STATUS:</b>
{poc_analysis['control'].replace('_', ' ').title()}
Confidence: {poc_analysis['confidence']}

<b>📊 ORDER FLOW AT POC:</b>
Delta: {poc_analysis['delta']:+.2f} BTC
Bid/Ask: {poc_analysis['bid_ask_ratio']:.2f}
CVD: {features['cvd']:+.2f}

<b>💡 ANALYSIS:</b>
{chr(10).join(poc_analysis['reasoning'])}

<b>📌 ACTION:</b> <b>{poc_analysis['action']}</b>

<b>VALUE AREA:</b>
VAH: ${poc_analysis['vah']:,.2f}
VAL: ${poc_analysis['val']:,.2f}
"""
                    
                    send_telegram(message)
                    last_pattern_alerts[cooldown_key] = now
                    
                    log_signal_csv(
                        event_type='poc_control',
                        control_state=poc_analysis['control'],
                        current_price=current_price,
                        current_orderflow=current_orderflow,
                        features=features,
                        vp_data=current_volume_profile,
                        extra={'confidence': poc_analysis['confidence'], 'action': poc_analysis['action']}
                    )
        
        # Check for POC shift
        if previous_vp:
            poc_shift = detect_poc_shift(current_volume_profile, previous_vp)
            
            if poc_shift and poc_shift.get('shifted'):
                print(f"\n⚠️ POC SHIFTED:")
                print(f"  Direction: {poc_shift['direction']}")
                print(f"  Magnitude: {poc_shift['shift_pct']:+.2f}%")
                print(f"  Previous POC: ${poc_shift['previous_poc']:,.2f}")
                print(f"  Current POC: ${poc_shift['current_poc']:,.2f}")
                print(f"  Significance: {poc_shift['significance']}")
                
                cooldown_key = f"poc_shift_{poc_shift['direction']}"
                now = time.time()
                last_alert = last_pattern_alerts.get(cooldown_key, 0)
                
                if now - last_alert > 3600:  # 1 hour cooldown for shifts
                    
                    shift_emoji = "📈" if poc_shift['direction'] == 'UP' else "📉"
                    
                    message = f"""
{shift_emoji} <b>POC SHIFTED - VALUE MOVING {poc_shift['direction']}</b>

<b>🔄 SHIFT DETECTED:</b>
Previous POC: ${poc_shift['previous_poc']:,.2f}
Current POC: ${poc_shift['current_poc']:,.2f}
Change: {poc_shift['shift_pct']:+.2f}%

<b>SIGNIFICANCE:</b> {poc_shift['significance']}

<b>📊 CURRENT ORDER FLOW:</b>
Delta: {current_orderflow['delta_btc']:+.2f} BTC
CVD: {features['cvd']:+.2f}

<b>💡 INTERPRETATION:</b>
{"🟢 Fair value moving HIGHER - Buyers accepting higher prices" if poc_shift['direction'] == 'UP' else "🔴 Fair value moving LOWER - Sellers pushing prices down"}

Value is migrating {poc_shift['direction']} - this shows {'bullish' if poc_shift['direction'] == 'UP' else 'bearish'} acceptance.
"""
                    
                    send_telegram(message)
                    last_pattern_alerts[cooldown_key] = now
                    
                    log_signal_csv(
                        event_type='poc_shift',
                        control_state=poc_shift['direction'],
                        current_price=current_price,
                        current_orderflow=current_orderflow,
                        features=features,
                        vp_data=current_volume_profile,
                        extra={'shift_pct': poc_shift['shift_pct'], 'significance': poc_shift['significance']}
                    )
    
    print("\n✅ Market scan complete")
    print("="*80 + "\n")


# ============= MONITORING THREAD =============
def monitor_orderflow():
    print("🔄 Volume Profile + Order Flow monitoring started")
    
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
    poc_info = f"${current_volume_profile['poc']:,.2f}" if current_volume_profile else "Calculating..."
    
    return f"""
    <h1>🎯 Volume Profile + Order Flow Monitor</h1>
    
    <h2>📊 WHAT WE TRACK:</h2>
    <ul>
        <li><b>Volume Profile:</b> POC, VAH, VAL (Point of Control + Value Area)</li>
        <li><b>Order Flow:</b> Delta, CVD, Bid/Ask, Absorption</li>
        <li><b>POC Control:</b> Who's dominating at fair value</li>
        <li><b>POC Shifts:</b> When value moves up or down</li>
    </ul>
    
    <h2>🎯 CURRENT VOLUME PROFILE:</h2>
    <ul>
        <li><b>POC (Point of Control):</b> {poc_info}</li>
        <li><b>Profile History:</b> {len(volume_profile_history)} profiles tracked</li>
        <li><b>POC Control Events:</b> {len(poc_control_history)} recorded</li>
    </ul>
    
    <h2>📈 ORDER FLOW DATA:</h2>
    <ul>
        <li><b>CVD History:</b> {len(cvd_history)} periods</li>
        <li><b>Large Orders:</b> {len(large_orders_history)} tracked</li>
        <li><b>Orderbook Snapshots:</b> {len(orderbook_snapshots)}</li>
    </ul>
    
    <h2>🎯 EXCHANGES:</h2>
    <ul>
        <li>OKX - Trades + Order Book</li>
        <li>KuCoin - Trades + Order Book</li>
        <li>Gate.io - Trades + Order Book</li>
    </ul>
    
    <h2>⚡ ALERTS YOU'LL GET:</h2>
    <ul>
        <li><b>POC Control:</b> When buyers/sellers dominate at fair value</li>
        <li><b>Absorption:</b> Heavy orderflow at POC</li>
        <li><b>POC Shifts:</b> When value moves up/down</li>
    </ul>
    
    <p><b>Scan Interval:</b> {ORDERFLOW_INTERVAL/60:.0f} minutes</p>
    <p><b>Status:</b> Monitoring POC + Order Flow! 🎯</p>
    """, 200


if __name__ == '__main__':
    print("🎯 Volume Profile + Order Flow Monitor")
    print("="*80)
    print("📊 TRACKING:")
    print("  ✅ Volume Profile (POC, VAH, VAL)")
    print("  ✅ Order Flow Delta")
    print("  ✅ CVD (Cumulative Volume Delta)")
    print("  ✅ Bid/Ask Imbalance")
    print("  ✅ POC Control Analysis")
    print("  ✅ POC Shift Detection")
    print("  ✅ Absorption at POC")
    print("="*80)
    print("\n🎯 EXCHANGES:")
    print("  OKX + KuCoin + Gate.io")
    print("="*80)
    print(f"\n⚙️ SETTINGS:")
    print(f"  Scan Interval: {ORDERFLOW_INTERVAL/60:.0f} minutes")
    print(f"  Volume Profile Lookback: {VP_LOOKBACK_CANDLES} candles ({VP_LOOKBACK_CANDLES*15/60:.0f} hours)")
    print(f"  Price Levels: {VP_PRICE_LEVELS}")
    print(f"  POC Proximity: {POC_PROXIMITY_PCT}%")
    print(f"  Run Web: {RUN_WEB}")
    print(f"  Run Scanner: {RUN_SCANNER}")
    print("="*80 + "\n")
    
    if RUN_SCANNER:
        orderflow_thread = threading.Thread(target=monitor_orderflow, daemon=True)
        orderflow_thread.start()
        
        if SEND_STARTUP_TELEGRAM:
            send_telegram(f"""
🎯 <b>Volume Profile + Order Flow Monitor Started!</b>

<b>📊 TRACKING:</b>
✅ Volume Profile (POC, VAH, VAL)
✅ Order Flow Delta & CVD
✅ POC Control Analysis
✅ Absorption Detection at POC
✅ POC Shift Tracking

<b>🎯 DATA SOURCES:</b>
• OKX: Trades + Order Book
• KuCoin: Trades + Order Book
• Gate.io: Trades + Order Book

<b>⚡ YOU'LL GET ALERTS FOR:</b>
• Who's dominating AT the POC
• Heavy absorption at fair value
• POC shifting up/down (value moving)

<b>📍 FOCUS:</b>
Pure volume profile + orderflow analysis.
No sessions, no OBs - just raw market structure!

Scanning every {ORDERFLOW_INTERVAL/60:.0f} minutes! 🔥
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
