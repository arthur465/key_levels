import os
import time
import requests
from datetime import datetime, timezone, timedelta
from flask import Flask, request, jsonify
import threading
import json
from collections import deque

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
TRADE_CHECK_INTERVAL = 900  # 15 minutes

# Order flow thresholds
DELTA_THRESHOLD_STRONG = 2000
IMBALANCE_RATIO_MIN = 3.0
BUY_PRESSURE_MIN = 60

# Position sizing
ACCOUNT_SIZE = 10000
RISK_PER_TRADE = 0.02

# Market scanner settings
HISTORY_SIZE = 40  # 10 hours of data (for 8H lookback)

# Multi-timeframe divergence detection
DIVERGENCE_LOOKBACK_1H = 4      # 1 hour (scalp plays)
DIVERGENCE_LOOKBACK_2H = 8      # 2 hours (day trades)
DIVERGENCE_LOOKBACK_4H = 16     # 4 hours (swing trades)
DIVERGENCE_LOOKBACK_8H = 32     # 8 hours (macro context only)

PATTERN_ALERT_COOLDOWN = 3600

# ============= TRADE TRACKING =============
active_trades = {}
trade_lock = threading.Lock()

last_orderflow_check = {
    'bias': 'NEUTRAL',
    'delta': 0,
    'cvd': 0,
    'timestamp': None,
    'price': 0
}

# ============= MARKET SCANNER DATA =============
orderflow_history = deque(maxlen=HISTORY_SIZE)
last_pattern_alerts = {}

# ============= ALERT MEMORY SYSTEM =============
alert_memory = deque(maxlen=50)  # Track last 50 alerts

def add_to_memory(alert_type, data):
    """Track alerts for context"""
    alert_memory.append({
        'type': alert_type,
        'timestamp': datetime.now(timezone.utc),
        'data': data
    })

def get_recent_alerts(alert_type=None, hours=3):
    """Get recent alerts, optionally filtered by type"""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    alerts = [a for a in alert_memory if a['timestamp'] > cutoff]
    
    if alert_type:
        alerts = [a for a in alerts if a['type'] == alert_type]
    
    return alerts

def check_recent_divergence():
    """Check if we recently called a divergence"""
    recent_div = get_recent_alerts('divergence', hours=3)
    if recent_div:
        latest = recent_div[-1]
        return latest['data']
    return None

def check_recent_climax():
    """Check if we recently detected a climax"""
    recent_climax = get_recent_alerts('climax', hours=2)
    if recent_climax:
        latest = recent_climax[-1]
        return latest['data']
    return None

# ============= PERFORMANCE TRACKING SYSTEM =============
pattern_performance = {
    'divergences': {
        '3tf_bearish': {'wins': 0, 'losses': 0, 'pending': []},
        '2tf_bearish': {'wins': 0, 'losses': 0, 'pending': []},
        '1tf_bearish': {'wins': 0, 'losses': 0, 'pending': []},
        '3tf_bullish': {'wins': 0, 'losses': 0, 'pending': []},
        '2tf_bullish': {'wins': 0, 'losses': 0, 'pending': []},
        '1tf_bullish': {'wins': 0, 'losses': 0, 'pending': []},
    },
    'absorptions': {
        'demand': {'wins': 0, 'losses': 0, 'pending': []},
        'supply': {'wins': 0, 'losses': 0, 'pending': []},
    },
    'climaxes': {
        'selling': {'wins': 0, 'losses': 0, 'pending': []},
        'buying': {'wins': 0, 'losses': 0, 'pending': []},
    },
    'fibs': {
        '0.382': {'wins': 0, 'losses': 0, 'pending': []},
        '0.618': {'wins': 0, 'losses': 0, 'pending': []},
        '0.786': {'wins': 0, 'losses': 0, 'pending': []},
    }
}

def track_pattern_prediction(pattern_type, pattern_subtype, prediction_data):
    """Track a pattern prediction for later validation"""
    prediction = {
        'timestamp': datetime.now(timezone.utc),
        'price': prediction_data['price'],
        'direction': prediction_data['direction'],
        'strength': prediction_data.get('strength', 0),
        'data': prediction_data
    }
    
    if pattern_type == 'divergence':
        tf_count = len(prediction_data.get('timeframes', []))
        key = f"{tf_count}tf_{pattern_subtype}"
        pattern_performance['divergences'][key]['pending'].append(prediction)
    
    elif pattern_type == 'absorption':
        pattern_performance['absorptions'][pattern_subtype]['pending'].append(prediction)
    
    elif pattern_type == 'climax':
        pattern_performance['climaxes'][pattern_subtype]['pending'].append(prediction)
    
    elif pattern_type == 'fib':
        pattern_performance['fibs'][pattern_subtype]['pending'].append(prediction)

def validate_predictions():
    """Check if predictions played out (run every update)"""
    current_price = get_current_price()
    if not current_price:
        return
    
    now = datetime.now(timezone.utc)
    
    # Check divergences
    for key, data in pattern_performance['divergences'].items():
        for pred in list(data['pending']):
            age_hours = (now - pred['timestamp']).total_seconds() / 3600
            
            # Validate after 2-6 hours
            if age_hours > 2:
                direction = pred['direction']
                entry_price = pred['price']
                
                # Check if moved as predicted
                if direction == 'down':
                    moved = entry_price - current_price
                    if moved > (entry_price * 0.005):  # Moved down 0.5%+
                        data['wins'] += 1
                        data['pending'].remove(pred)
                        print(f"   ✅ Divergence WIN: {key}")
                    elif age_hours > 6:  # Didn't move after 6 hours
                        data['losses'] += 1
                        data['pending'].remove(pred)
                        print(f"   ❌ Divergence LOSS: {key}")
                
                elif direction == 'up':
                    moved = current_price - entry_price
                    if moved > (entry_price * 0.005):  # Moved up 0.5%+
                        data['wins'] += 1
                        data['pending'].remove(pred)
                        print(f"   ✅ Divergence WIN: {key}")
                    elif age_hours > 6:
                        data['losses'] += 1
                        data['pending'].remove(pred)
                        print(f"   ❌ Divergence LOSS: {key}")
    
    # Check absorptions
    for key, data in pattern_performance['absorptions'].items():
        for pred in list(data['pending']):
            age_hours = (now - pred['timestamp']).total_seconds() / 3600
            
            if age_hours > 1:  # Faster validation for absorption
                direction = pred['direction']
                entry_price = pred['price']
                
                if direction == 'up':  # Demand absorption (expect bounce)
                    moved = current_price - entry_price
                    if moved > (entry_price * 0.003):  # Moved up 0.3%+
                        data['wins'] += 1
                        data['pending'].remove(pred)
                        print(f"   ✅ Absorption WIN: {key}")
                    elif age_hours > 4:
                        data['losses'] += 1
                        data['pending'].remove(pred)
                        print(f"   ❌ Absorption LOSS: {key}")
                
                elif direction == 'down':  # Supply absorption (expect rejection)
                    moved = entry_price - current_price
                    if moved > (entry_price * 0.003):
                        data['wins'] += 1
                        data['pending'].remove(pred)
                        print(f"   ✅ Absorption WIN: {key}")
                    elif age_hours > 4:
                        data['losses'] += 1
                        data['pending'].remove(pred)
                        print(f"   ❌ Absorption LOSS: {key}")

def get_performance_stats():
    """Get formatted performance statistics"""
    stats = "\n📊 <b>PATTERN PERFORMANCE:</b>\n"
    
    # Divergences
    stats += "\n<b>DIVERGENCES:</b>\n"
    for key, data in pattern_performance['divergences'].items():
        total = data['wins'] + data['losses']
        if total > 0:
            win_rate = (data['wins'] / total) * 100
            emoji = "🔥" if win_rate >= 70 else "✅" if win_rate >= 60 else "⚠️"
            stats += f"  {key}: {win_rate:.0f}% ({data['wins']}/{total}) {emoji}\n"
    
    # Absorptions
    stats += "\n<b>ABSORPTIONS:</b>\n"
    for key, data in pattern_performance['absorptions'].items():
        total = data['wins'] + data['losses']
        if total > 0:
            win_rate = (data['wins'] / total) * 100
            emoji = "🔥" if win_rate >= 70 else "✅" if win_rate >= 60 else "⚠️"
            stats += f"  {key}: {win_rate:.0f}% ({data['wins']}/{total}) {emoji}\n"
    
    # Climaxes
    stats += "\n<b>CLIMAXES:</b>\n"
    for key, data in pattern_performance['climaxes'].items():
        total = data['wins'] + data['losses']
        if total > 0:
            win_rate = (data['wins'] / total) * 100
            emoji = "🔥" if win_rate >= 70 else "✅" if win_rate >= 60 else "⚠️"
            stats += f"  {key}: {win_rate:.0f}% ({data['wins']}/{total}) {emoji}\n"
    
    return stats

# ============= VOLUME SPIKE DETECTION =============
volume_history = deque(maxlen=20)  # Track recent volume

def track_volume(current_of):
    """Track volume for spike detection"""
    if not current_of:
        return
    
    # Calculate total volume from exchanges
    total_volume = 0
    for ex in current_of.get('exchanges_data', []):
        buy_vol = ex['trades']['buy_volume']
        sell_vol = ex['trades']['sell_volume']
        total_volume += (buy_vol + sell_vol)
    
    volume_history.append({
        'timestamp': datetime.now(timezone.utc),
        'volume': total_volume,
        'delta': current_of['delta']
    })

def detect_volume_spike():
    """Detect if current volume is significantly higher than average"""
    if len(volume_history) < 10:
        return None
    
    recent = list(volume_history)
    current_vol = recent[-1]['volume']
    avg_vol = sum(v['volume'] for v in recent[:-1]) / len(recent[:-1])
    
    if avg_vol == 0:
        return None
    
    ratio = current_vol / avg_vol
    
    if ratio > 3.0:  # 3x above average
        return {
            'ratio': ratio,
            'current': current_vol,
            'average': avg_vol,
            'severity': min(10.0, ratio * 2)
        }
    
    return None

# ============= EXIT SIGNAL TRACKING =============
active_positions = []  # Track positions for exit signals

def check_exit_signals():
    """Check if any active positions should exit"""
    if not active_positions:
        return []
    
    exit_signals = []
    current_price = get_current_price()
    if not current_price:
        return []
    
    current_of = get_aggregated_orderflow()
    if not current_of:
        return []
    
    for position in list(active_positions):
        direction = position['direction']
        entry_price = position['entry_price']
        entry_delta = position['entry_delta']
        current_delta = current_of['delta']
        
        # Check for exit conditions
        exit_reason = None
        exit_type = None
        
        # 1. Opposite divergence forming
        if direction == 'bearish':
            # Check if bullish divergence forming
            if len(current_market_state.get('divergences', [])) > 0:
                for div in current_market_state['divergences']:
                    if div['type'] == 'bullish':
                        exit_reason = f"Bullish divergence forming ({len(div['timeframes'])} TF)"
                        exit_type = "COUNTER_DIVERGENCE"
        
        elif direction == 'bullish':
            # Check if bearish divergence forming
            if len(current_market_state.get('divergences', [])) > 0:
                for div in current_market_state['divergences']:
                    if div['type'] == 'bearish':
                        exit_reason = f"Bearish divergence forming ({len(div['timeframes'])} TF)"
                        exit_type = "COUNTER_DIVERGENCE"
        
        # 2. Delta flipped against position
        if direction == 'bearish' and current_delta > 2000:
            exit_reason = f"Delta flipped positive ({current_delta:,.0f} BTC)"
            exit_type = "DELTA_FLIP"
        
        elif direction == 'bullish' and current_delta < -2000:
            exit_reason = f"Delta flipped negative ({current_delta:,.0f} BTC)"
            exit_type = "DELTA_FLIP"
        
        # 3. Profit target hit (fib level)
        fib_confluence = check_fib_confluence(current_price)
        if fib_confluence:
            if direction == 'bearish' and current_price < entry_price:
                # In profit, at fib level
                exit_reason = f"At {fib_confluence['fib_level']} fib support - consider profit"
                exit_type = "FIB_TARGET"
            
            elif direction == 'bullish' and current_price > entry_price:
                # In profit, at fib level
                exit_reason = f"At {fib_confluence['fib_level']} fib resistance - consider profit"
                exit_type = "FIB_TARGET"
        
        if exit_reason:
            exit_signals.append({
                'position': position,
                'reason': exit_reason,
                'type': exit_type,
                'current_price': current_price
            })
    
    return exit_signals

def add_position_for_tracking(direction, entry_price, entry_delta):
    """Add a position to track for exit signals"""
    active_positions.append({
        'direction': direction,
        'entry_price': entry_price,
        'entry_delta': entry_delta,
        'entry_time': datetime.now(timezone.utc)
    })

def add_to_memory(alert_type, data):
    """Track alerts for context"""
    alert_memory.append({
        'type': alert_type,
        'timestamp': datetime.now(timezone.utc),
        'data': data
    })

def get_recent_alerts(alert_type=None, hours=3):
    """Get recent alerts, optionally filtered by type"""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    alerts = [a for a in alert_memory if a['timestamp'] > cutoff]
    
    if alert_type:
        alerts = [a for a in alerts if a['type'] == alert_type]
    
    return alerts

def check_recent_divergence():
    """Check if we recently called a divergence"""
    recent_div = get_recent_alerts('divergence', hours=3)
    if recent_div:
        latest = recent_div[-1]
        return latest['data']
    return None

def check_recent_climax():
    """Check if we recently detected a climax"""
    recent_climax = get_recent_alerts('climax', hours=2)
    if recent_climax:
        latest = recent_climax[-1]
        return latest['data']
    return None

# ============= FIB RETRACEMENT TRACKING =============
recent_price_moves = deque(maxlen=10)  # Track last 10 significant moves

def track_price_moves():
    """Track significant price moves for fib calculations"""
    if len(orderflow_history) < 20:
        return
    
    recent = list(orderflow_history)[-20:]  # Last 5 hours
    prices = [h['price'] for h in recent]
    
    swing_high = max(prices)
    swing_low = min(prices)
    range_size = swing_high - swing_low
    
    # Only track significant moves (>$500 or >0.7%)
    if range_size > 500 and (range_size / swing_low * 100) > 0.7:
        # Check if this is a new move (not already tracked)
        if not recent_price_moves or abs(recent_price_moves[-1]['high'] - swing_high) > 100:
            
            # Determine direction (where is price now vs where it started?)
            price_now = prices[-1]
            price_start = prices[0]
            
            move = {
                'high': swing_high,
                'low': swing_low,
                'range': range_size,
                'timestamp': datetime.now(timezone.utc),
                'direction': 'bearish' if price_now < price_start else 'bullish'
            }
            recent_price_moves.append(move)
            print(f"   📊 New {move['direction']} move tracked: ${swing_high:,.0f} → ${swing_low:,.0f}")

def calculate_fib_retracements(move):
    """
    Calculate fib retracement levels for a move
    
    For BEARISH moves (down): fibs are where price retraces BACK UP
    For BULLISH moves (up): fibs are where price retraces BACK DOWN
    """
    high = move['high']
    low = move['low']
    range_size = move['range']
    direction = move['direction']
    
    if direction == 'bearish':
        # Price moved down, retracements go back UP (from low)
        fibs = {
            '0.236': {'price': low + (range_size * 0.236), 'type': 'very_shallow'},
            '0.382': {'price': low + (range_size * 0.382), 'type': 'shallow'},
            '0.500': {'price': low + (range_size * 0.500), 'type': 'mid'},
            '0.618': {'price': low + (range_size * 0.618), 'type': 'golden'},
            '0.786': {'price': low + (range_size * 0.786), 'type': 'deep'}
        }
    else:  # bullish
        # Price moved up, retracements go back DOWN (from high)
        fibs = {
            '0.236': {'price': high - (range_size * 0.236), 'type': 'very_shallow'},
            '0.382': {'price': high - (range_size * 0.382), 'type': 'shallow'},
            '0.500': {'price': high - (range_size * 0.500), 'type': 'mid'},
            '0.618': {'price': high - (range_size * 0.618), 'type': 'golden'},
            '0.786': {'price': high - (range_size * 0.786), 'type': 'deep'}
        }
    
    return fibs

def check_fib_confluence(current_price, tolerance=100):
    """Check if current price is near any fib retracement levels"""
    if not recent_price_moves:
        return None
    
    # Check most recent significant move
    latest_move = recent_price_moves[-1]
    
    # Skip if move is too old (>24 hours)
    age = (datetime.now(timezone.utc) - latest_move['timestamp']).total_seconds() / 3600
    if age > 24:
        return None
    
    fibs = calculate_fib_retracements(latest_move)
    
    # Check which fib level we're near
    for fib_level, fib_data in fibs.items():
        fib_price = fib_data['price']
        if abs(current_price - fib_price) < tolerance:
            return {
                'move': latest_move,
                'fib_level': fib_level,
                'fib_price': fib_price,
                'fib_type': fib_data['type'],
                'distance': current_price - fib_price
            }
    
    return None

# ============= UNIFIED MARKET UPDATE SYSTEM =============
current_market_state = {
    'divergences': [],
    'absorptions': [],
    'climaxes': [],
    'last_update': None
}

# ============= FLASK APP =============
app = Flask(__name__)

# ============= HELPER: FORMAT DELTA EXPLANATION =============
def format_delta_explanation(delta):
    """Add explanation for what delta means"""
    if delta > 0:
        pressure_type = "BUYING"
        symbol = "+"
        interpretation = "More buying than selling"
    else:
        pressure_type = "SELLING"
        symbol = ""
        interpretation = "More selling than buying"
    
    return f"{symbol}{delta:,.0f} BTC ({pressure_type} pressure) - {interpretation}"

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

# ============= MEXC API (FIXED) =============
def get_mexc_orderbook():
    """MEXC orderbook"""
    url = "https://contract.mexc.com/api/v1/contract/depth/BTC_USDT"
    params = {"limit": 500}
    try:
        response = requests.get(url, params=params, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            if data.get("success"):
                book = data["data"]
                bids = book["bids"]
                asks = book["asks"]
                
                bid_volume = sum(float(b[1]) for b in bids) / 1000
                ask_volume = sum(float(a[1]) for a in asks) / 1000
                total_volume = bid_volume + ask_volume
                
                delta = bid_volume - ask_volume
                bid_pct = (bid_volume / total_volume * 100) if total_volume > 0 else 50
                
                return {
                    'exchange': 'mexc',
                    'delta': delta,
                    'bid_volume': bid_volume,
                    'ask_volume': ask_volume,
                    'bid_pct': bid_pct
                }
        return None
    except Exception as e:
        print(f"   ❌ [MEXC] Error: {type(e).__name__}")
        return None

def get_mexc_trades():
    """MEXC trades - FIXED for single-letter field names"""
    url = "https://contract.mexc.com/api/v1/contract/deals/BTC_USDT"
    params = {"limit": 1000}
    try:
        response = requests.get(url, params=params, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            if data.get("success"):
                trades = data.get("data", [])
                
                if not trades:
                    return None
                
                buy_volume = 0
                sell_volume = 0
                
                for t in trades:
                    try:
                        # MEXC uses single-letter keys: 'v' = volume, 'T' = type (1=buy, 2=sell)
                        vol = float(t.get('v', 0))
                        side = t.get('T', 0)
                        
                        if vol == 0:
                            continue
                        
                        if side == 1:
                            buy_volume += vol
                        elif side == 2:
                            sell_volume += vol
                    except:
                        continue
                
                # Normalize (MEXC uses contracts)
                buy_volume = buy_volume / 1000
                sell_volume = sell_volume / 1000
                total = buy_volume + sell_volume
                
                if total == 0:
                    return None
                
                buy_pct = (buy_volume / total * 100) if total > 0 else 50
                cvd = buy_volume - sell_volume
                
                return {
                    'exchange': 'mexc',
                    'buy_pct': buy_pct,
                    'cvd': cvd,
                    'buy_volume': buy_volume,
                    'sell_volume': sell_volume
                }
        return None
    except Exception as e:
        print(f"   ❌ [MEXC] Error: {type(e).__name__}")
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

# ============= GATE.IO API =============
def get_gateio_orderbook():
    """Gate.io orderbook"""
    url = "https://api.gateio.ws/api/v4/futures/usdt/order_book"
    params = {"contract": "BTC_USDT", "limit": 100}
    try:
        response = requests.get(url, params=params, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            bids = data["bids"]
            asks = data["asks"]
            
            bid_volume = sum(float(b["s"]) for b in bids)
            ask_volume = sum(float(a["s"]) for a in asks)
            total_volume = bid_volume + ask_volume
            
            delta = bid_volume - ask_volume
            bid_pct = (bid_volume / total_volume * 100) if total_volume > 0 else 50
            
            return {
                'exchange': 'gateio',
                'delta': delta,
                'bid_volume': bid_volume,
                'ask_volume': ask_volume,
                'bid_pct': bid_pct
            }
        return None
    except Exception as e:
        print(f"   ❌ [GATE.IO] Error: {type(e).__name__}")
        return None

def get_gateio_trades():
    """Gate.io trades"""
    url = "https://api.gateio.ws/api/v4/futures/usdt/trades"
    params = {"contract": "BTC_USDT", "limit": 1000}
    try:
        response = requests.get(url, params=params, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            
            buy_volume = sum(abs(float(t["size"])) for t in data if float(t["size"]) > 0)
            sell_volume = sum(abs(float(t["size"])) for t in data if float(t["size"]) < 0)
            total = buy_volume + sell_volume
            
            buy_pct = (buy_volume / total * 100) if total > 0 else 50
            cvd = buy_volume - sell_volume
            
            return {
                'exchange': 'gateio',
                'buy_pct': buy_pct,
                'cvd': cvd,
                'buy_volume': buy_volume,
                'sell_volume': sell_volume
            }
        return None
    except Exception as e:
        print(f"   ❌ [GATE.IO] Error: {type(e).__name__}")
        return None

# ============= AGGREGATED ORDERFLOW =============
def get_aggregated_orderflow():
    """Aggregate order flow from all 5 exchanges with robust error handling"""
    print("\n📊 AGGREGATING 5-EXCHANGE ORDERFLOW")
    
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
    
    # MEXC - TEMPORARILY DISABLED (normalization issue)
    # try:
    #     mexc_book = get_mexc_orderbook()
    #     mexc_trades = get_mexc_trades()
    #     if mexc_book and mexc_trades:
    #         exchanges_data.append({
    #             'name': 'MEXC',
    #             'emoji': '🟡',
    #             'orderbook': mexc_book,
    #             'trades': mexc_trades
    #         })
    # except Exception as e:
    #     print(f"   ❌ [MEXC] Aggregation error: {e}")
    
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
    
    # Gate.io
    try:
        gateio_book = get_gateio_orderbook()
        gateio_trades = get_gateio_trades()
        if gateio_book and gateio_trades:
            exchanges_data.append({
                'name': 'GATEIO',
                'emoji': '🔷',
                'orderbook': gateio_book,
                'trades': gateio_trades
            })
    except Exception as e:
        print(f"   ❌ [GATEIO] Aggregation error: {e}")
    
    if not exchanges_data:
        print("❌ No exchange data available")
        return None
    
    # Aggregate with error handling
    try:
        total_delta = sum(ex['orderbook']['delta'] for ex in exchanges_data)
        total_cvd = sum(ex['trades']['cvd'] for ex in exchanges_data)
        avg_bid_pct = sum(ex['orderbook']['bid_pct'] for ex in exchanges_data) / len(exchanges_data)
        avg_buy_pct = sum(ex['trades']['buy_pct'] for ex in exchanges_data) / len(exchanges_data)
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
    
    print(f"   Bias: {bias}, Delta: {total_delta:,.0f}, Exchanges: {len(exchanges_data)}/5")
    
    return {
        'bias': bias,
        'delta': total_delta,
        'cvd': total_cvd,
        'avg_bid_pct': avg_bid_pct,
        'avg_buy_pct': avg_buy_pct,
        'exchanges_count': len(exchanges_data),
        'exchanges_data': exchanges_data
    }

# ============= TELEGRAM =============
def send_telegram(message):
    """Send message to Telegram"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Telegram not configured")
        return False
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    
    try:
        response = requests.post(url, data=data, timeout=10)
        if response.status_code == 200:
            print("✅ Telegram message sent")
            return True
        else:
            print(f"❌ Telegram error: {response.status_code}")
            return False
    except Exception as e:
        print(f"❌ Telegram error: {e}")
        return False

# ============= PRICE FETCHING =============
def get_current_price():
    """Get current BTC price from OKX"""
    try:
        url = "https://www.okx.com/api/v5/market/ticker"
        params = {"instId": "BTC-USDT-SWAP"}
        response = requests.get(url, params=params, timeout=5)
        
        if response.status_code == 200:
            data = response.json()
            if data.get("code") == "0":
                price = float(data["data"][0]["last"])
                return price
    except:
        pass
    
    return None

# ============= PATTERN DETECTION (STORES PATTERNS, DOESN'T ALERT) =============
def detect_market_patterns(current_of):
    """
    Detect market patterns and STORE them for the unified update
    
    Patterns detected:
    - Multi-timeframe divergences (1H, 2H, 4H + 8H context)
    - Absorption (demand/supply)
    - Climax (selling/buying exhaustion)
    
    These are stored in current_market_state and included in the next 15-min update
    """
    if not current_of:
        return
    
    current_price = get_current_price()
    if not current_price:
        return
    
    # Store in history
    orderflow_history.append({
        'timestamp': datetime.now(timezone.utc),
        'price': current_price,
        'delta': current_of['delta'],
        'cvd': current_of['cvd'],
        'avg_bid_pct': current_of['avg_bid_pct'],
        'avg_buy_pct': current_of['avg_buy_pct']
    })
    
    if len(orderflow_history) < 5:
        return
    
    now = time.time()
    
    # ============= MULTI-TIMEFRAME DIVERGENCE DETECTION =============
    timeframes = {
        '1H': DIVERGENCE_LOOKBACK_1H,
        '2H': DIVERGENCE_LOOKBACK_2H,
        '4H': DIVERGENCE_LOOKBACK_4H,
        '8H': DIVERGENCE_LOOKBACK_8H
    }
    
    bearish_divs = {}
    bullish_divs = {}
    
    for tf_name, lookback in timeframes.items():
        if len(orderflow_history) >= lookback:
            recent = list(orderflow_history)[-lookback:]
            
            prices = [h['price'] for h in recent]
            deltas = [h['delta'] for h in recent]
            
            # Bearish divergence: price making higher highs, delta weakening
            if prices[-1] > max(prices[:-1]) and deltas[-1] < max(deltas[:-1]):
                delta_decline_pct = ((deltas[-1] - max(deltas[:-1])) / abs(max(deltas[:-1]))) * 100 if max(deltas[:-1]) != 0 else 0
                
                if abs(delta_decline_pct) > 30:  # At least 30% weaker
                    bearish_divs[tf_name] = {
                        'decline_pct': abs(delta_decline_pct),
                        'strength': min(10.0, abs(delta_decline_pct) / 10)
                    }
            
            # Bullish divergence: price making lower lows, delta strengthening
            elif prices[-1] < min(prices[:-1]) and deltas[-1] > min(deltas[:-1]):
                delta_improve_pct = ((deltas[-1] - min(deltas[:-1])) / abs(min(deltas[:-1]))) * 100 if min(deltas[:-1]) != 0 else 0
                
                if abs(delta_improve_pct) > 30:
                    bullish_divs[tf_name] = {
                        'improve_pct': abs(delta_improve_pct),
                        'strength': min(10.0, abs(delta_improve_pct) / 10)
                    }
    
    # Store bearish divergence if detected
    if bearish_divs:
        active_tfs = [tf for tf in ['1H', '2H', '4H'] if tf in bearish_divs]
        
        if active_tfs:
            cooldown_key = 'bearish_divergence'
            last_alert_time = last_pattern_alerts.get(cooldown_key, 0)
            
            if now - last_alert_time > PATTERN_ALERT_COOLDOWN:
                avg_strength = sum(bearish_divs[tf]['strength'] for tf in active_tfs) / len(active_tfs)
                tf_count = len(active_tfs)
                
                confidence_boost = 0
                if tf_count >= 2:
                    confidence_boost = 1.5
                if tf_count >= 3:
                    confidence_boost = 3.0
                
                final_strength = min(10.0, avg_strength + confidence_boost)
                
                if tf_count == 3:
                    conviction = "ULTRA HIGH 🔥🔥🔥"
                elif tf_count == 2:
                    conviction = "HIGH 🔥"
                else:
                    conviction = "MODERATE ⚠️"
                
                # Build explanation
                explanation = f"Price making higher highs but delta weakening across {tf_count} timeframe{'s' if tf_count > 1 else ''}"
                
                # Store in memory
                add_to_memory('divergence', {
                    'type': 'bearish',
                    'price': current_price,
                    'delta': current_of['delta'],
                    'strength': final_strength,
                    'direction': 'down',
                    'timeframes': list(bearish_divs.keys())
                })
                
                # Track for performance
                track_pattern_prediction('divergence', 'bearish', {
                    'price': current_price,
                    'direction': 'down',
                    'timeframes': active_tfs,
                    'strength': final_strength
                })
                
                # Add to current state for the unified update
                current_market_state['divergences'].append({
                    'type': 'bearish',
                    'timeframes': active_tfs,
                    'strength': final_strength,
                    'conviction': conviction,
                    'explanation': explanation,
                    'tf_details': bearish_divs
                })
                
                last_pattern_alerts[cooldown_key] = now
    
    # Store bullish divergence if detected
    if bullish_divs:
        active_tfs = [tf for tf in ['1H', '2H', '4H'] if tf in bullish_divs]
        
        if active_tfs:
            cooldown_key = 'bullish_divergence'
            last_alert_time = last_pattern_alerts.get(cooldown_key, 0)
            
            if now - last_alert_time > PATTERN_ALERT_COOLDOWN:
                avg_strength = sum(bullish_divs[tf]['strength'] for tf in active_tfs) / len(active_tfs)
                tf_count = len(active_tfs)
                
                confidence_boost = 0
                if tf_count >= 2:
                    confidence_boost = 1.5
                if tf_count >= 3:
                    confidence_boost = 3.0
                
                final_strength = min(10.0, avg_strength + confidence_boost)
                
                if tf_count == 3:
                    conviction = "ULTRA HIGH 🔥🔥🔥"
                elif tf_count == 2:
                    conviction = "HIGH 🔥"
                else:
                    conviction = "MODERATE ⚠️"
                
                explanation = f"Price making lower lows but delta strengthening across {tf_count} timeframe{'s' if tf_count > 1 else ''}"
                
                # Store in memory
                add_to_memory('divergence', {
                    'type': 'bullish',
                    'price': current_price,
                    'delta': current_of['delta'],
                    'strength': final_strength,
                    'direction': 'up',
                    'timeframes': list(bullish_divs.keys())
                })
                
                # Track for performance
                track_pattern_prediction('divergence', 'bullish', {
                    'price': current_price,
                    'direction': 'up',
                    'timeframes': active_tfs,
                    'strength': final_strength
                })
                
                current_market_state['divergences'].append({
                    'type': 'bullish',
                    'timeframes': active_tfs,
                    'strength': final_strength,
                    'conviction': conviction,
                    'explanation': explanation,
                    'tf_details': bullish_divs
                })
                
                last_pattern_alerts[cooldown_key] = now
    
    # ============= ABSORPTION PATTERNS =============
    if len(orderflow_history) >= 3:
        recent = list(orderflow_history)[-3:]
        current_delta = recent[-1]['delta']
        avg_delta = sum(abs(h['delta']) for h in recent[:-1]) / 2
        price_change = ((recent[-1]['price'] - recent[-2]['price']) / recent[-2]['price']) * 100
        
        # Demand absorption: Heavy selling with minimal price drop
        if current_delta < -3000 and abs(price_change) < 0.15:
            severity = min(10.0, abs(current_delta) / 2000)
            
            cooldown_key = 'demand_absorption'
            last_alert_time = last_pattern_alerts.get(cooldown_key, 0)
            
            if now - last_alert_time > 1800:
                add_to_memory('absorption', {
                    'type': 'demand',
                    'price': current_price,
                    'delta': current_delta,
                    'severity': severity
                })
                
                # Track for performance
                track_pattern_prediction('absorption', 'demand', {
                    'price': current_price,
                    'direction': 'up',
                    'severity': severity
                })
                
                explanation = f"Heavy selling ({abs(current_delta):,.0f} BTC) but price holding = Buyers absorbing"
                
                current_market_state['absorptions'].append({
                    'type': 'DEMAND_ABSORPTION',
                    'severity': severity,
                    'explanation': explanation,
                    'delta': current_delta
                })
                
                last_pattern_alerts[cooldown_key] = now
        
        # Supply absorption: Heavy buying with minimal price rise
        elif current_delta > 3000 and abs(price_change) < 0.15:
            severity = min(10.0, abs(current_delta) / 2000)
            
            cooldown_key = 'supply_absorption'
            last_alert_time = last_pattern_alerts.get(cooldown_key, 0)
            
            if now - last_alert_time > 1800:
                add_to_memory('absorption', {
                    'type': 'supply',
                    'price': current_price,
                    'delta': current_delta,
                    'severity': severity
                })
                
                # Track for performance
                track_pattern_prediction('absorption', 'supply', {
                    'price': current_price,
                    'direction': 'down',
                    'severity': severity
                })
                
                explanation = f"Heavy buying ({abs(current_delta):,.0f} BTC) but price can't rise = Sellers absorbing"
                
                current_market_state['absorptions'].append({
                    'type': 'SUPPLY_ABSORPTION',
                    'severity': severity,
                    'explanation': explanation,
                    'delta': current_delta
                })
                
                last_pattern_alerts[cooldown_key] = now
    
    # ============= CLIMAX PATTERNS =============
    if len(orderflow_history) >= 5:
        recent = list(orderflow_history)[-5:]
        current_delta = recent[-1]['delta']
        avg_delta = sum(h['delta'] for h in recent[:-1]) / 4
        price_change = ((recent[-1]['price'] - recent[-2]['price']) / recent[-2]['price']) * 100
        ratio = abs(current_delta / avg_delta) if avg_delta != 0 else 1
        
        # Selling climax
        if current_delta < -5000 and ratio > 2.0 and abs(price_change) < 0.2:
            severity = min(10.0, ratio * 2)
            
            cooldown_key = 'selling_climax'
            last_alert_time = last_pattern_alerts.get(cooldown_key, 0)
            
            if now - last_alert_time > 1800:
                add_to_memory('climax', {
                    'type': 'selling',
                    'price': current_price,
                    'delta': current_delta,
                    'severity': severity,
                    'ratio': ratio
                })
                
                # Track for performance
                track_pattern_prediction('climax', 'selling', {
                    'price': current_price,
                    'direction': 'up',
                    'severity': severity
                })
                
                explanation = f"Extreme selling ({ratio:.1f}x normal) = Seller exhaustion, expect bounce"
                
                current_market_state['climaxes'].append({
                    'type': 'SELLING_CLIMAX',
                    'severity': severity,
                    'ratio': ratio,
                    'explanation': explanation
                })
                
                last_pattern_alerts[cooldown_key] = now
        
        # Buying climax
        elif current_delta > 5000 and ratio > 2.0 and abs(price_change) < 0.2:
            severity = min(10.0, ratio * 2)
            
            cooldown_key = 'buying_climax'
            last_alert_time = last_pattern_alerts.get(cooldown_key, 0)
            
            if now - last_alert_time > 1800:
                add_to_memory('climax', {
                    'type': 'buying',
                    'price': current_price,
                    'delta': current_delta,
                    'severity': severity,
                    'ratio': ratio
                })
                
                # Track for performance
                track_pattern_prediction('climax', 'buying', {
                    'price': current_price,
                    'direction': 'down',
                    'severity': severity
                })
                
                explanation = f"Extreme buying ({ratio:.1f}x normal) = Buyer exhaustion, expect rejection"
                
                current_market_state['climaxes'].append({
                    'type': 'BUYING_CLIMAX',
                    'severity': severity,
                    'ratio': ratio,
                    'explanation': explanation
                })
                
                last_pattern_alerts[cooldown_key] = now

# ============= UNIFIED MARKET UPDATE =============
def compile_market_update(current_of, current_price):
    """
    Compile comprehensive 15-min market update with:
    - Orderflow summary
    - Divergences (if any)
    - Absorption/Climax (if any)
    - Fib retracement context
    - Confluence analysis
    - Plain English summary
    """
    
    # ========== ORDERFLOW SUMMARY ==========
    orderflow_summary = f"""🔷 ORDERFLOW ({current_of['exchanges_count']}/5 exchanges):
Bias: {current_of['bias']}
Delta: {format_delta_explanation(current_of['delta'])}
CVD: {current_of['cvd']:,.0f} BTC
Avg Buy%: {current_of['avg_buy_pct']:.1f}%"""
    
    # ========== PATTERNS DETECTED ==========
    patterns_text = ""
    
    # Divergences
    if current_market_state['divergences']:
        patterns_text += "\n\n⚠️ DIVERGENCES DETECTED:"
        for div in current_market_state['divergences']:
            div_emoji = "📉" if div['type'] == 'bearish' else "📈"
            tf_list = ", ".join(div['timeframes'])
            
            # Show detailed timeframe breakdown
            tf_breakdown = ""
            for tf, data in div['tf_details'].items():
                if tf in ['1H', '2H', '4H']:
                    pct = data.get('decline_pct') or data.get('improve_pct', 0)
                    tf_breakdown += f"\n  • {tf}: {pct:.1f}% delta change"
            
            patterns_text += f"""
{div_emoji} <b>{div['type'].upper()} Divergence</b> (Multi-TF)
  Timeframes: {tf_list}
  Strength: {div['strength']:.1f}/10
  Conviction: {div['conviction']}
{tf_breakdown}
  
  💡 What this means:
  {div['explanation']}
  = {"Major" if len(div['timeframes']) >= 2 else "Potential"} {"top" if div['type'] == 'bearish' else "bottom"} forming"""
    
    # Absorptions
    if current_market_state['absorptions']:
        patterns_text += "\n\n💎 ABSORPTION DETECTED:"
        for absorption in current_market_state['absorptions']:
            abs_emoji = "🔴" if "DEMAND" in absorption['type'] else "🟢"
            patterns_text += f"""
{abs_emoji} <b>{absorption['type']}</b>
  Severity: {absorption['severity']:.1f}/10
  
  💡 What this means:
  {absorption['explanation']}
  = {"Bullish" if "DEMAND" in absorption['type'] else "Bearish"} signal"""
    
    # Climaxes
    if current_market_state['climaxes']:
        patterns_text += "\n\n💥 CLIMAX DETECTED:"
        for climax in current_market_state['climaxes']:
            patterns_text += f"""
💥 <b>{climax['type']}</b>
  Severity: {climax['severity']:.1f}/10
  Ratio: {climax['ratio']:.1f}x above normal
  
  💡 What this means:
  {climax['explanation']}"""
    
    if not patterns_text:
        patterns_text = "\n\n⚠️ PATTERNS: None detected"
    
    # ========== VOLUME SPIKE DETECTION ==========
    volume_spike = detect_volume_spike()
    volume_text = ""
    
    if volume_spike:
        volume_text = f"""
💥 <b>VOLUME SPIKE DETECTED:</b>
  Current: {volume_spike['ratio']:.1f}x above average
  Severity: {volume_spike['severity']:.1f}/10
  
  💡 What this means:
  Institutional activity - smart money moving
  {"High conviction if aligns with patterns!" if (has_divergence or has_absorption or has_climax) else "Watch for pattern confirmation"}"""
    
    # ========== EXIT SIGNALS ==========
    exit_signals = check_exit_signals()
    exit_text = ""
    
    if exit_signals:
        exit_text = "\n\n⚠️ <b>EXIT SIGNALS:</b>"
        for signal in exit_signals:
            pos = signal['position']
            profit_pct = ((signal['current_price'] - pos['entry_price']) / pos['entry_price']) * 100
            if pos['direction'] == 'bearish':
                profit_pct = -profit_pct
            
            exit_emoji = "💰" if signal['type'] == "FIB_TARGET" else "🚨"
            exit_text += f"""
{exit_emoji} <b>{pos['direction'].upper()} from ${pos['entry_price']:,.0f}</b>
  Current: ${signal['current_price']:,.0f} ({profit_pct:+.2f}%)
  Reason: {signal['reason']}
  Type: {signal['type']}
  
  💡 Consider: {"Taking profit" if signal['type'] == "FIB_TARGET" else "Exiting or tightening stop"}"""
    
    # ========== FIB RETRACEMENT CONTEXT ==========
    fib_context = ""
    fib_confluence = check_fib_confluence(current_price)
    
    if fib_confluence:
        move = fib_confluence['move']
        fib_level = fib_confluence['fib_level']
        fib_price = fib_confluence['fib_price']
        fib_type = fib_confluence['fib_type']
        
        # Explain what this fib level means
        fib_meanings = {
            'very_shallow': """<b>Very Shallow (0.236)</b>
  = STRONG trend momentum
  = Barely any pullback
  = Institutions aggressively entering""",
            
            'shallow': """<b>Shallow (0.382)</b>
  = Strong trend
  = Quick entry opportunity
  = Momentum-driven move""",
            
            'mid': """<b>Mid-Level (0.500)</b>
  = Balanced pullback
  = Standard retracement
  = Good risk/reward""",
            
            'golden': """<b>GOLDEN ZONE (0.618) ✅</b>
  = Most reliable fib level
  = Classic retracement
  = Widely watched by traders
  = Best probability setup""",
            
            'deep': """<b>Deep (0.786) ⚠️</b>
  = Trend weakening
  = Risky continuation play
  = Might reverse instead"""
        }
        
        direction_context = ""
        if move['direction'] == 'bearish':
            direction_context = f"""After dump from ${move['high']:,.0f} → ${move['low']:,.0f}
Price retraced UP to {fib_level} fib
Watch for REJECTION here = Continuation SHORT"""
        else:
            direction_context = f"""After rally from ${move['low']:,.0f} → ${move['high']:,.0f}
Price retraced DOWN to {fib_level} fib
Watch for BOUNCE here = Continuation LONG"""
        
        fib_context = f"""
📊 <b>FIB RETRACEMENT ZONE:</b>
{direction_context}

{fib_meanings[fib_type]}

Current price: ${current_price:,.0f}
At {fib_level} fib: ${fib_price:,.0f} ✅

🎯 <b>What to watch:</b>
{"Rejection/reversal down" if move['direction'] == 'bearish' else "Bounce/continuation up"}
This is a {"RETRACEMENT SHORT" if move['direction'] == 'bearish' else "RETRACEMENT LONG"} setup"""
    else:
        fib_context = "\n📊 <b>FIB CONTEXT:</b> No active retracement zones"
    
    # ========== CONFLUENCE & SUMMARY ==========
    summary = "\n\n💡 <b>SUMMARY:</b>"
    
    has_divergence = len(current_market_state['divergences']) > 0
    has_absorption = len(current_market_state['absorptions']) > 0
    has_climax = len(current_market_state['climaxes']) > 0
    has_fib = fib_confluence is not None
    has_volume_spike = volume_spike is not None
    
    confluence_count = sum([has_divergence, has_absorption, has_climax, has_fib, has_volume_spike])
    
    if confluence_count >= 4:
        summary += "\n🔥🔥 <b>QUAD+ CONFLUENCE!</b> 🔥🔥"
        if has_divergence:
            div_type = current_market_state['divergences'][0]['type']
            summary += f"\n  • {div_type.capitalize()} divergence"
        if has_absorption:
            summary += f"\n  • Absorption pattern"
        if has_climax:
            climax_type = current_market_state['climaxes'][0]['type']
            summary += f"\n  • {climax_type}"
        if has_fib:
            summary += f"\n  • At {fib_confluence['fib_level']} fib"
        if has_volume_spike:
            summary += f"\n  • Volume spike ({volume_spike['ratio']:.1f}x)"
        summary += "\n\n= <b>EXTREME CONVICTION!</b> 💎💎💎"
        summary += "\n\n🗣️ <b>In Plain English:</b>"
        summary += "\nMASSIVE confluence - premium setup!"
        summary += "\nConsider max sizing if structure confirms."
    
    elif confluence_count >= 3:
        summary += "\n🔥 <b>TRIPLE CONFLUENCE DETECTED!</b> 🔥"
        if has_divergence:
            div_type = current_market_state['divergences'][0]['type']
            summary += f"\n  • {div_type.capitalize()} divergence"
        if has_absorption:
            summary += f"\n  • Absorption pattern"
        if has_climax:
            climax_type = current_market_state['climaxes'][0]['type']
            summary += f"\n  • {climax_type}"
        if has_fib:
            summary += f"\n  • At {fib_confluence['fib_level']} fib"
        if has_volume_spike:
            summary += f"\n  • Volume spike ({volume_spike['ratio']:.1f}x)"
        summary += "\n\n= <b>ULTRA HIGH CONVICTION!</b> 💎"
        summary += "\n\n🗣️ <b>In Plain English:</b>"
        summary += "\nMultiple signals aligning - major setup!"
        summary += "\nConsider sizing up if structure confirms."
    
    elif confluence_count == 2:
        summary += "\n💪 <b>CONFLUENCE DETECTED</b>"
        patterns = []
        if has_divergence:
            div_type = current_market_state['divergences'][0]['type']
            patterns.append(f"{div_type.capitalize()} divergence")
        if has_absorption:
            patterns.append("Absorption")
        if has_climax:
            patterns.append("Climax exhaustion")
        if has_fib:
            patterns.append(f"{fib_confluence['fib_level']} fib")
        if has_volume_spike:
            patterns.append(f"Volume spike")
        summary += f"\n  {' + '.join(patterns)}"
        summary += "\n\n= <b>HIGH CONVICTION</b> setup"
        summary += "\n\n🗣️ <b>In Plain English:</b>"
        summary += "\nTwo signals confirming - solid setup."
        summary += "\nWait for structure confirmation before entry."
    
    elif confluence_count == 1:
        if has_divergence:
            div_type = current_market_state['divergences'][0]['type']
            div_count = len(current_market_state['divergences'][0]['timeframes'])
            summary += f"\n{div_type.capitalize()} bias from divergence ({div_count} TF)"
        elif has_absorption:
            summary += f"\n{current_market_state['absorptions'][0]['explanation']}"
        elif has_climax:
            summary += f"\nExhaustion signal detected"
        elif has_fib:
            summary += f"\nAt {fib_confluence['fib_level']} fib retracement"
        summary += "\n\n= Standard setup, watch for additional confirmation"
        summary += "\n\n🗣️ <b>In Plain English:</b>"
        summary += "\nOne signal detected - wait for more confluence."
    
    else:
        summary += "\nQuiet market, no major setups detected"
        summary += "\n\n🗣️ <b>In Plain English:</b>"
        summary += "\nNo clear patterns yet - stay patient."
    
    # ========== PERFORMANCE STATS (if we have data) ==========
    perf_stats = ""
    total_predictions = sum(
        data['wins'] + data['losses'] 
        for category in pattern_performance.values() 
        for data in category.values()
    )
    
    if total_predictions >= 10:  # Only show if we have 10+ validated predictions
        perf_stats = get_performance_stats()
    
    # ========== COMPILE FULL MESSAGE ==========
    message = f"""
📊 <b>MARKET UPDATE</b>

{orderflow_summary}
{patterns_text}
{volume_text}
{exit_text}
{fib_context}
{summary}
{perf_stats}
    """
    
    return message

def send_market_update():
    """Send unified 15-min market update"""
    try:
        print("\n" + "="*80)
        print("📊 GENERATING MARKET UPDATE")
        print("="*80)
        
        # Get current orderflow
        current_of = get_aggregated_orderflow()
        if not current_of:
            print("❌ No orderflow data")
            return
        
        current_price = get_current_price()
        if not current_price:
            print("❌ No price data")
            return
        
        # Detect patterns (stores them in current_market_state)
        detect_market_patterns(current_of)
        
        # Track volume for spike detection
        track_volume(current_of)
        
        # Validate previous predictions (for performance tracking)
        validate_predictions()
        
        # Track price moves for fib calculations
        track_price_moves()
        
        # Compile and send update
        message = compile_market_update(current_of, current_price)
        send_telegram(message)
        
        # Reset patterns for next cycle
        current_market_state['divergences'] = []
        current_market_state['absorptions'] = []
        current_market_state['climaxes'] = []
        current_market_state['last_update'] = datetime.now(timezone.utc)
        
        print("✅ Market update sent")
        print("="*80)
        
    except Exception as e:
        print(f"❌ Market update error: {e}")
        import traceback
        traceback.print_exc()

# ============= CONTEXT-AWARE CONFIRMATION =============
def confirm_order_flow_five_exchange(direction, entry):
    """5-EXCHANGE orderflow confirmation with CONTEXT AWARENESS"""
    print(f"\n🔍 5-EXCHANGE CONFIRMATION FOR {direction} @ ${entry:,.0f}")
    
    of = get_aggregated_orderflow()
    if not of:
        return False, "Unable to fetch order flow data", {}, 0
    
    exchanges = of.get('exchanges_data', [])
    if len(exchanges) < 3:
        return False, f"Only {len(exchanges)}/5 exchanges available", of, 0
    
    # Context checks
    recent_div = check_recent_divergence()
    recent_climax = check_recent_climax()
    
    prediction_confirmed = False
    context_boost = 0
    context_note = ""
    
    if recent_div:
        div_type = recent_div.get('type')
        div_price = recent_div.get('price', 0)
        
        if div_type == 'bearish' and entry < div_price:
            prediction_confirmed = True
            context_boost = 2
            context_note = f"✅ Following bearish divergence at ${div_price:,.0f}"
        elif div_type == 'bullish' and entry > div_price:
            prediction_confirmed = True
            context_boost = 2
            context_note = f"✅ Following bullish divergence at ${div_price:,.0f}"
    
    if recent_climax and not prediction_confirmed:
        climax_type = recent_climax.get('type')
        
        if climax_type == 'selling' and direction == "LONG":
            prediction_confirmed = True
            context_boost = 2
            context_note = "✅ Following selling climax"
        elif climax_type == 'buying' and direction == "SHORT":
            prediction_confirmed = True
            context_boost = 2
            context_note = "✅ Following buying climax"
    
    # Analyze exchanges
    scores = []
    reasons = []
    
    for ex in exchanges:
        name = ex['name']
        emoji = ex['emoji']
        orderbook = ex['orderbook']
        trades = ex['trades']
        
        delta = orderbook['delta']
        bid_pct = orderbook['bid_pct']
        buy_pct = trades['buy_pct']
        
        score = 0
        
        # Context-aware delta interpretation
        if direction == "LONG":
            if prediction_confirmed and delta < 0 and abs(delta) > 1000:
                score += 1
            elif delta > 500:
                score += 1
        elif direction == "SHORT":
            if prediction_confirmed and delta > 0 and abs(delta) > 1000:
                score += 1
            elif delta < -500:
                score += 1
        
        # Bid/Ask
        if direction == "LONG" and bid_pct > 52:
            score += 1
        elif direction == "SHORT" and bid_pct < 48:
            score += 1
        
        # Buy/Sell pressure
        if direction == "LONG" and buy_pct > 52:
            score += 1
        elif direction == "SHORT" and buy_pct < 48:
            score += 1
        
        scores.append(score)
        
        status = "✅" if score >= 2 else "❌"
        reasons.append(f"{emoji} <b>{name}: {score}/3</b> {status}")
    
    total_score = sum(scores) + context_boost
    strong_count = sum(1 for s in scores if s >= 2)
    
    # Dynamic threshold
    if prediction_confirmed:
        required_score = 2
        required_strong = 0
    else:
        required_score = 5
        required_strong = 2
    
    confirmed = total_score >= required_score or strong_count >= required_strong
    
    reason_text = "\n".join(reasons)
    if context_note:
        reason_text = f"{context_note}\n\n{reason_text}"
    
    reason_text += f"\n\n<b>COMBINED: {total_score}/7 ({strong_count}/5 strong)</b>"
    
    return confirmed, reason_text, of, total_score

# ============= POSITION SIZING & TRADE MANAGEMENT =============
def calculate_position_size(entry, stop):
    """Calculate position size based on risk"""
    risk_amount = ACCOUNT_SIZE * RISK_PER_TRADE
    stop_distance = abs(entry - stop)
    position = risk_amount / stop_distance
    return position

def classify_timeframe(setup_type, rr_ratio, of_data):
    """Classify trade timeframe"""
    if "SINGLE PRINT" in setup_type:
        return "SWING", "3-7 days"
    elif rr_ratio > 5:
        return "POSITIONAL", "5-14 days"
    elif rr_ratio > 3:
        return "SWING", "2-5 days"
    else:
        return "INTRADAY", "4-24 hours"

def calculate_stops_targets(setup_type, direction, entry, webhook_data):
    """Calculate stops and targets"""
    if "VA RE-ENTRY" in setup_type:
        val = webhook_data.get('val', entry * 0.97)
        vah = webhook_data.get('vah', entry * 1.03)
        
        if direction == "LONG":
            stop = val * 0.997
            target = vah
        else:
            stop = vah * 1.003
            target = val
    
    elif "NAKED POC" in setup_type:
        if direction == "LONG":
            stop = entry * 0.995
            target = entry * 1.015
        else:
            stop = entry * 1.005
            target = entry * 0.985
    
    elif "SINGLE PRINT" in setup_type:
        level_high = webhook_data.get('level_high', entry * 1.01)
        level_low = webhook_data.get('level_low', entry * 0.99)
        
        if direction == "LONG":
            stop = level_low * 0.997
            target = level_high
        else:
            stop = level_high * 1.003
            target = level_low
    
    else:
        if direction == "LONG":
            stop = entry * 0.995
            target = entry * 1.015
        else:
            stop = entry * 1.005
            target = entry * 0.985
    
    rr_ratio = abs((target - entry) / (entry - stop)) if (entry - stop) != 0 else 1
    
    return stop, target, rr_ratio

def monitor_trades():
    """Monitor active trades"""
    print("🔄 Trade monitoring thread started")
    
    while True:
        try:
            time.sleep(TRADE_CHECK_INTERVAL)
            
            if not active_trades:
                continue
            
            current_of = get_aggregated_orderflow()
            if not current_of:
                continue
            
            current_price = get_current_price()
            if not current_price:
                continue
            
            with trade_lock:
                for trade_id, trade in list(active_trades.items()):
                    direction = trade['direction']
                    entry_delta = trade.get('entry_delta', 0)
                    current_delta = current_of['delta']
                    delta_change_pct = ((current_delta - entry_delta) / abs(entry_delta) * 100) if entry_delta != 0 else 0
                    
                    weakness_detected = False
                    weakness_reason = ""
                    
                    if direction == "LONG" and delta_change_pct < -50:
                        weakness_detected = True
                        weakness_reason = f"Delta weakened {abs(delta_change_pct):.0f}%"
                    elif direction == "SHORT" and delta_change_pct > 50:
                        weakness_detected = True
                        weakness_reason = f"Delta strengthened {abs(delta_change_pct):.0f}%"
                    
                    if weakness_detected and 'weakness' not in trade['alerts_sent']:
                        send_telegram(f"⚠️ TRADE WEAKNESS: {weakness_reason}")
                        trade['alerts_sent'].append('weakness')
        
        except Exception as e:
            print(f"❌ Trade monitoring error: {e}")

# ============= ORDERFLOW MONITORING =============
def monitor_orderflow():
    """Monitor orderflow and send unified updates every 15 minutes"""
    print("🔄 Orderflow monitoring thread started")
    
    while True:
        try:
            # Send unified market update
            send_market_update()
            
            # Update global state
            current_of = get_aggregated_orderflow()
            if current_of:
                global last_orderflow_check
                current_price = get_current_price()
                
                last_orderflow_check = {
                    'bias': current_of['bias'],
                    'delta': current_of['delta'],
                    'cvd': current_of['cvd'],
                    'timestamp': datetime.now(timezone.utc),
                    'price': current_price or 0
                }
            
            time.sleep(ORDERFLOW_INTERVAL)
        
        except Exception as e:
            print(f"❌ Orderflow monitoring error: {e}")
            time.sleep(ORDERFLOW_INTERVAL)

# ============= WEBHOOK ENDPOINT =============
@app.route('/webhook', methods=['POST'])
def webhook():
    """Webhook endpoint for TradingView alerts"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"status": "error", "message": "No data"}), 400
        
        print(f"\n📨 Webhook received: {json.dumps(data, indent=2)}")
        
        alert_type = data.get('alert_type', '').lower()
        webhook_data = data.copy()
        
        # Process webhook (simplified for now)
        # Full implementation would handle VA RE-ENTRY, NAKED POC, etc.
        
        return jsonify({"status": "success"}), 200
    
    except Exception as e:
        print(f"❌ Webhook error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/', methods=['GET'])
def home():
    total_predictions = sum(
        data['wins'] + data['losses'] 
        for category in pattern_performance.values() 
        for data in category.values()
    )
    
    return f"""
    <h1>🚀 Beast Mode V7 - ENHANCED EDITION</h1>
    <p>Active Trades: {len(active_trades)}</p>
    <p>Active Positions (tracking exits): {len(active_positions)}</p>
    <p>History: {len(orderflow_history)}/{HISTORY_SIZE}</p>
    <p>Alert Memory: {len(alert_memory)}/50</p>
    <p>Predictions Tracked: {total_predictions}</p>
    
    <h3>🆕 FEATURES:</h3>
    <ul>
        <li>✅ Multi-Timeframe Divergences (1H, 2H, 4H + 8H context)</li>
        <li>✅ Fib Retracement Tracking (0.382, 0.5, 0.618, 0.786)</li>
        <li>✅ Unified 15-Min Updates (no spam!)</li>
        <li>✅ Confluence Detection (patterns + fibs + volume)</li>
        <li>✅ Context-Aware Confidence</li>
        <li>✅ Full Explanations</li>
        <li>🆕 Exit Signals (when to take profit)</li>
        <li>🆕 Volume Spike Detection (smart money activity)</li>
        <li>🆕 Performance Tracking (learn what works)</li>
    </ul>
    
    <h3>4-Exchange Coverage (MEXC temporarily disabled):</h3>
    <ul>
        <li>🟠 OKX: $20B daily</li>
        <li>🟢 Bitget: $17B daily</li>
        <li>🔵 KuCoin: $12B daily</li>
        <li>🔷 Gate.io: $10B daily</li>
    </ul>
    <p><b>Total: $59B daily (39% coverage)</b></p>
    <p><i>Note: MEXC disabled due to normalization issue - will re-enable after fix</i></p>
    """, 200

if __name__ == '__main__':
    print("🚀 Beast Mode V7 - ENHANCED EDITION")
    print("="*80)
    print("🆕 MULTI-TIMEFRAME DIVERGENCES:")
    print("  ⚡ 1H - Scalp plays")
    print("  ⚡ 2H - Day trades")
    print("  ⚡ 4H - Swing setups")
    print("  🔷 8H - Macro context")
    print("="*80)
    print("📊 FIB RETRACEMENT LEVELS:")
    print("  • 0.382 - Shallow (strong trend)")
    print("  • 0.500 - Mid-level")
    print("  • 0.618 - Golden zone ⭐")
    print("  • 0.786 - Deep (weak trend)")
    print("="*80)
    print("💡 UNIFIED UPDATES:")
    print("  One message every 15 min with:")
    print("  • Orderflow summary")
    print("  • Divergences (if any)")
    print("  • Absorption/Climax (if any)")
    print("  • Volume spikes (smart money)")
    print("  • Exit signals (if positions active)")
    print("  • Fib context (if relevant)")
    print("  • Confluence analysis")
    print("  • Performance stats (after 10+ predictions)")
    print("  • Plain English summary")
    print("="*80)
    print("🆕 NEW FEATURES:")
    print("  💰 Exit Signals - Tells you when to take profit")
    print("  💥 Volume Spikes - Catches institutional activity")
    print("  📊 Performance Tracking - Learns which patterns work")
    print("="*80)
    
    # Start threads
    orderflow_thread = threading.Thread(target=monitor_orderflow, daemon=True)
    orderflow_thread.start()
    
    trade_thread = threading.Thread(target=monitor_trades, daemon=True)
    trade_thread.start()
    
    # Send startup message
    send_telegram(f"""
🚀 <b>Beast Mode V7 - ENHANCED Started!</b>

<b>🆕 MULTI-TIMEFRAME DIVERGENCES:</b>
⚡ 1H, 2H, 4H (active)
🔷 8H (macro context)

<b>📊 FIB RETRACEMENT TRACKING:</b>
Tracks ALL levels (0.382, 0.5, 0.618, 0.786)
Shows when price at key retracement zones

<b>💡 UNIFIED UPDATES:</b>
One clean message every 15 min with:
• Orderflow + Patterns
• Volume spikes (smart money)
• Exit signals (if positions tracked)
• Fib retracements
• Confluence analysis
• Performance stats (after 10+)

<b>🆕 NEW FEATURES:</b>
💰 <b>Exit Signals</b> - Tells you when to take profit
💥 <b>Volume Spikes</b> - Catches institutional moves
📊 <b>Performance Tracking</b> - Learns what works

<b>Coverage: $59B daily (39%)</b>
🟠 OKX • 🟢 Bitget • 🔵 KuCoin • 🔷 Gate.io
(MEXC temporarily disabled - fixing normalization)

💰 Account: ${ACCOUNT_SIZE:,.0f}
📊 Risk: {RISK_PER_TRADE*100:.0f}% per trade

<b>Updates every {ORDERFLOW_INTERVAL/60:.0f} minutes!</b>
    """)
    
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
