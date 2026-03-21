import os
import time
import requests
from datetime import datetime, timezone
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
ACCOUNT_SIZE = 10000  # USD - ADJUST TO YOUR ACCOUNT
RISK_PER_TRADE = 0.02  # 2% risk per trade

# Market scanner settings
HISTORY_SIZE = 20  # Keep last 20 orderflow readings
DIVERGENCE_LOOKBACK = 10  # Check last 10 readings for divergences
PATTERN_ALERT_COOLDOWN = 3600  # Don't spam same pattern (1 hour cooldown)

# ============= TRADE TRACKING =============
active_trades = {}
trade_lock = threading.Lock()

# Market bias tracking
last_orderflow_check = {
    'bias': 'NEUTRAL',
    'delta': 0,
    'cvd': 0,
    'timestamp': None
}

# ============= MARKET SCANNER DATA =============
# Historical orderflow data (rolling window)
orderflow_history = deque(maxlen=HISTORY_SIZE)

# Pattern alert tracking (avoid spam)
last_pattern_alerts = {}

# ============= FLASK APP =============
app = Flask(__name__)

# ============= BYBIT API =============
def get_orderbook_delta():
    url = "https://api.bybit.com/v5/market/orderbook"
    params = {"category": "linear", "symbol": SYMBOL, "limit": 200}
    try:
        print(f"   🌐 Calling Bybit orderbook API: {url}")
        response = requests.get(url, params=params, timeout=10)
        print(f"   ✅ Response status: {response.status_code}")
        
        data = response.json()
        print(f"   📦 Response data keys: {list(data.keys())}")
        
        if data.get("retCode") == 0:
            print(f"   ✅ Bybit API success (retCode=0)")
            bids = data["result"]["b"]
            asks = data["result"]["a"]
            
            bid_volume = sum(float(b[1]) for b in bids)
            ask_volume = sum(float(a[1]) for a in asks)
            total_volume = bid_volume + ask_volume
            
            delta = bid_volume - ask_volume
            bid_pct = (bid_volume / total_volume * 100) if total_volume > 0 else 50
            
            return {
                'delta': delta,
                'bid_volume': bid_volume,
                'ask_volume': ask_volume,
                'bid_pct': bid_pct
            }
        else:
            print(f"   ❌ Bybit API error: retCode={data.get('retCode')}, msg={data.get('retMsg')}")
            return None
    except requests.exceptions.Timeout as e:
        print(f"   ❌ Bybit API timeout: {str(e)}")
        return None
    except Exception as e:
        print(f"   ❌ Bybit API exception: {type(e).__name__} - {str(e)}")
        import traceback
        traceback.print_exc()
        return None

def get_current_price():
    url = "https://api.bybit.com/v5/market/tickers"
    params = {"category": "linear", "symbol": SYMBOL}
    try:
        response = requests.get(url, params=params, timeout=10)
        data = response.json()
        if data.get("retCode") == 0:
            return float(data["result"]["list"][0]["lastPrice"])
        return None
    except:
        return None

def get_recent_trades_analysis(price_level=None, range_size=100):
    url = "https://api.bybit.com/v5/market/recent-trade"
    params = {"category": "linear", "symbol": SYMBOL, "limit": 1000}
    try:
        print(f"   🌐 Calling Bybit recent trades API: {url}")
        response = requests.get(url, params=params, timeout=10)
        print(f"   ✅ Response status: {response.status_code}")
        
        data = response.json()
        print(f"   📦 Response data keys: {list(data.keys())}")
        
        if data.get("retCode") == 0:
            print(f"   ✅ Bybit API success (retCode=0)")
            trades = data["result"]["list"]
            print(f"   📊 Received {len(trades)} trades")
            
            buy_volume = sum(float(t["size"]) for t in trades if t["side"] == "Buy")
            sell_volume = sum(float(t["size"]) for t in trades if t["side"] == "Sell")
            total = buy_volume + sell_volume
            
            buy_pct = (buy_volume / total * 100) if total > 0 else 50
            cvd = buy_volume - sell_volume
            
            imbalance_data = None
            if price_level:
                level_low = price_level - range_size
                level_high = price_level + range_size
                
                level_buys = sum(float(t["size"]) for t in trades 
                                if t["side"] == "Buy" and level_low <= float(t["price"]) <= level_high)
                level_sells = sum(float(t["size"]) for t in trades 
                                 if t["side"] == "Sell" and level_low <= float(t["price"]) <= level_high)
                
                if level_sells > 0:
                    imbalance_ratio = level_buys / level_sells
                elif level_buys > 0:
                    imbalance_ratio = 999
                else:
                    imbalance_ratio = 1.0
                
                imbalance_data = {
                    'ratio': imbalance_ratio,
                    'is_buy_imbalance': imbalance_ratio >= IMBALANCE_RATIO_MIN,
                    'is_sell_imbalance': (1 / imbalance_ratio) >= IMBALANCE_RATIO_MIN if imbalance_ratio > 0 else False
                }
            
            return {
                'buy_pct': buy_pct,
                'cvd': cvd,
                'imbalance': imbalance_data
            }
        else:
            print(f"   ❌ Bybit API error: retCode={data.get('retCode')}, msg={data.get('retMsg')}")
            return None
    except requests.exceptions.Timeout as e:
        print(f"   ❌ Bybit API timeout: {str(e)}")
        return None
    except Exception as e:
        print(f"   ❌ Bybit API exception: {type(e).__name__} - {str(e)}")
        import traceback
        traceback.print_exc()
        return None

# ============= MARKET SCANNER - PATTERN DETECTION =============

def detect_price_trend(history, lookback=5):
    """Detect if price is making higher highs, lower lows, or ranging"""
    if len(history) < lookback:
        return "INSUFFICIENT_DATA"
    
    recent = list(history)[-lookback:]
    prices = [r['price'] for r in recent]
    
    # Find peaks and troughs
    highs = []
    lows = []
    
    for i in range(1, len(prices) - 1):
        if prices[i] > prices[i-1] and prices[i] > prices[i+1]:
            highs.append(prices[i])
        if prices[i] < prices[i-1] and prices[i] < prices[i+1]:
            lows.append(prices[i])
    
    if len(highs) >= 2:
        if highs[-1] > highs[-2]:
            return "HIGHER_HIGHS"
        elif highs[-1] < highs[-2]:
            return "LOWER_HIGHS"
    
    if len(lows) >= 2:
        if lows[-1] < lows[-2]:
            return "LOWER_LOWS"
        elif lows[-1] > lows[-2]:
            return "HIGHER_LOWS"
    
    return "RANGING"

def detect_delta_trend(history, lookback=5):
    """Detect if delta is making higher highs or lower highs"""
    if len(history) < lookback:
        return "INSUFFICIENT_DATA"
    
    recent = list(history)[-lookback:]
    deltas = [r['delta'] for r in recent]
    
    # Find peaks
    peaks = []
    for i in range(1, len(deltas) - 1):
        if abs(deltas[i]) > abs(deltas[i-1]) and abs(deltas[i]) > abs(deltas[i+1]):
            peaks.append(deltas[i])
    
    if len(peaks) >= 2:
        if abs(peaks[-1]) > abs(peaks[-2]):
            return "STRENGTHENING"
        else:
            return "WEAKENING"
    
    return "STABLE"

def detect_divergence(history):
    """Detect bullish or bearish divergences between price and orderflow"""
    if len(history) < DIVERGENCE_LOOKBACK:
        return None
    
    price_trend = detect_price_trend(history, DIVERGENCE_LOOKBACK)
    delta_trend = detect_delta_trend(history, DIVERGENCE_LOOKBACK)
    
    # Bearish divergence: Price higher highs, Delta weakening
    if price_trend == "HIGHER_HIGHS" and delta_trend == "WEAKENING":
        recent = list(history)[-DIVERGENCE_LOOKBACK:]
        
        # Calculate divergence strength
        price_change_pct = ((recent[-1]['price'] - recent[0]['price']) / recent[0]['price']) * 100
        delta_change_pct = ((abs(recent[-1]['delta']) - abs(recent[0]['delta'])) / abs(recent[0]['delta'] + 1)) * 100
        
        strength = abs(price_change_pct - delta_change_pct) / 10  # Normalize to 0-10
        
        return {
            'type': 'BEARISH_DIVERGENCE',
            'strength': min(strength, 10),
            'price_trend': price_trend,
            'delta_trend': delta_trend,
            'current_price': recent[-1]['price'],
            'current_delta': recent[-1]['delta']
        }
    
    # Bullish divergence: Price lower lows, Delta strengthening
    if price_trend == "LOWER_LOWS" and delta_trend == "STRENGTHENING":
        recent = list(history)[-DIVERGENCE_LOOKBACK:]
        
        price_change_pct = ((recent[-1]['price'] - recent[0]['price']) / recent[0]['price']) * 100
        delta_change_pct = ((abs(recent[-1]['delta']) - abs(recent[0]['delta'])) / abs(recent[0]['delta'] + 1)) * 100
        
        strength = abs(price_change_pct - delta_change_pct) / 10
        
        return {
            'type': 'BULLISH_DIVERGENCE',
            'strength': min(strength, 10),
            'price_trend': price_trend,
            'delta_trend': delta_trend,
            'current_price': recent[-1]['price'],
            'current_delta': recent[-1]['delta']
        }
    
    return None

def detect_absorption(current, previous):
    """Detect absorption patterns - heavy volume, minimal price movement"""
    if not current or not previous:
        return None
    
    price_change = abs(current['price'] - previous['price'])
    delta_magnitude = abs(current['delta'])
    price_change_pct = (price_change / previous['price']) * 100
    
    # Significant orderflow (>3000 BTC) but small price move (<0.3%)
    if delta_magnitude > 3000 and price_change_pct < 0.3:
        if current['delta'] > 0:
            # Heavy buying but price not rising = Supply absorption (bearish)
            return {
                'type': 'SUPPLY_ABSORPTION',
                'delta': current['delta'],
                'price_change_pct': price_change_pct,
                'severity': min((delta_magnitude / 5000) * 10, 10)  # 0-10 scale
            }
        else:
            # Heavy selling but price not falling = Demand absorption (bullish)
            return {
                'type': 'DEMAND_ABSORPTION',
                'delta': current['delta'],
                'price_change_pct': price_change_pct,
                'severity': min((delta_magnitude / 5000) * 10, 10)
            }
    
    return None

def detect_exhaustion(current, history):
    """Detect buying/selling climax patterns"""
    if len(history) < 3:
        return None
    
    recent = list(history)[-3:]
    avg_delta = sum(abs(r['delta']) for r in recent) / len(recent)
    current_delta = abs(current['delta'])
    
    # Spike in delta (>2x average) with minimal price movement
    if current_delta > avg_delta * 2 and current_delta > 4000:
        recent_prices = [r['price'] for r in recent]
        price_momentum = abs(current['price'] - recent_prices[0]) / recent_prices[0] * 100
        
        # Strong orderflow but weak price movement = Exhaustion
        if price_momentum < 0.5:  # Less than 0.5% price move
            if current['delta'] > 0:
                return {
                    'type': 'BUYING_CLIMAX',
                    'delta': current['delta'],
                    'avg_delta': avg_delta,
                    'price_momentum': price_momentum,
                    'severity': min((current_delta / avg_delta) * 2, 10)
                }
            else:
                return {
                    'type': 'SELLING_CLIMAX',
                    'delta': current['delta'],
                    'avg_delta': avg_delta,
                    'price_momentum': price_momentum,
                    'severity': min((current_delta / avg_delta) * 2, 10)
                }
    
    return None

def detect_momentum_shift(history):
    """Detect when momentum is building or fading"""
    if len(history) < 5:
        return None
    
    recent = list(history)[-5:]
    
    # Check if delta is consistently increasing/decreasing
    deltas = [r['delta'] for r in recent]
    
    increasing = all(abs(deltas[i]) < abs(deltas[i+1]) for i in range(len(deltas)-1))
    decreasing = all(abs(deltas[i]) > abs(deltas[i+1]) for i in range(len(deltas)-1))
    
    if increasing and abs(deltas[-1]) > 2000:
        return {
            'type': 'MOMENTUM_BUILDING',
            'direction': 'BULLISH' if deltas[-1] > 0 else 'BEARISH',
            'current_delta': deltas[-1],
            'strength': min(abs(deltas[-1]) / 3000 * 10, 10)
        }
    
    if decreasing and abs(deltas[0]) > 2000:
        return {
            'type': 'MOMENTUM_FADING',
            'direction': 'BULLISH' if deltas[0] > 0 else 'BEARISH',
            'current_delta': deltas[-1],
            'strength': min(abs(deltas[0] - deltas[-1]) / 2000 * 10, 10)
        }
    
    return None

def should_alert_pattern(pattern_type):
    """Check if enough time has passed since last alert of this type"""
    now = time.time()
    last_alert = last_pattern_alerts.get(pattern_type, 0)
    
    if now - last_alert > PATTERN_ALERT_COOLDOWN:
        last_pattern_alerts[pattern_type] = now
        return True
    
    return False

def send_pattern_alert(pattern):
    """Send Telegram alert for detected pattern"""
    pattern_type = pattern['type']
    
    # Only alert if cooldown expired and pattern is significant
    if pattern.get('strength', 0) < 5 and pattern.get('severity', 0) < 5:
        print(f"⚠️ Pattern {pattern_type} detected but too weak to alert (strength: {pattern.get('strength', 0)})")
        return
    
    if not should_alert_pattern(pattern_type):
        print(f"⏳ Pattern {pattern_type} in cooldown, skipping alert")
        return
    
    # Build alert message based on pattern type
    if pattern_type == 'BEARISH_DIVERGENCE':
        message = f"""
🔴 <b>BEARISH DIVERGENCE DETECTED</b>

⚠️ <b>TOP WARNING SIGNAL</b>

📊 <b>Analysis:</b>
Price: ${pattern['current_price']:,.0f} (making higher highs)
Delta: {pattern['current_delta']:+,.0f} BTC (weakening)
Strength: {'⭐' * int(pattern['strength'])} ({pattern['strength']:.1f}/10)

💡 <b>What this means:</b>
Buyers are exhausting while price pushes higher
Classic distribution pattern at potential top

🎯 <b>Action:</b>
• Watch for SHORT setups (Poor High, PDVAH rejection)
• Consider taking profits on longs
• Prepare for reversal

⚠️ <b>This is a WARNING, not an entry signal!</b>
Wait for TPO setup + orderflow confirmation
        """
    
    elif pattern_type == 'BULLISH_DIVERGENCE':
        message = f"""
🟢 <b>BULLISH DIVERGENCE DETECTED</b>

⚠️ <b>BOTTOM WARNING SIGNAL</b>

📊 <b>Analysis:</b>
Price: ${pattern['current_price']:,.0f} (making lower lows)
Delta: {pattern['current_delta']:+,.0f} BTC (strengthening)
Strength: {'⭐' * int(pattern['strength'])} ({pattern['strength']:.1f}/10)

💡 <b>What this means:</b>
Buyers stepping in while price makes new lows
Classic accumulation pattern at potential bottom

🎯 <b>Action:</b>
• Watch for LONG setups (Poor Low, PDVAL support)
• Consider fading shorts
• Prepare for reversal

⚠️ <b>This is a WARNING, not an entry signal!</b>
Wait for TPO setup + orderflow confirmation
        """
    
    elif pattern_type == 'SUPPLY_ABSORPTION':
        message = f"""
🔴 <b>SUPPLY ABSORPTION DETECTED</b>

⚠️ <b>DISTRIBUTION PATTERN</b>

📊 <b>Analysis:</b>
Delta: {pattern['delta']:+,.0f} BTC (heavy buying)
Price Change: {pattern['price_change_pct']:.2f}% (minimal)
Severity: {'🔥' * int(pattern['severity']/2)} ({pattern['severity']:.1f}/10)

💡 <b>What this means:</b>
Market absorbing heavy buying without rising
Sellers stepping in at this level
Potential top forming

🎯 <b>Context:</b>
Smart money distributing to eager buyers
Price likely to reverse or stall

⚠️ <b>Consider fading rallies into this zone!</b>
        """
    
    elif pattern_type == 'DEMAND_ABSORPTION':
        message = f"""
🟢 <b>DEMAND ABSORPTION DETECTED</b>

⚠️ <b>ACCUMULATION PATTERN</b>

📊 <b>Analysis:</b>
Delta: {pattern['delta']:+,.0f} BTC (heavy selling)
Price Change: {pattern['price_change_pct']:.2f}% (minimal)
Severity: {'🔥' * int(pattern['severity']/2)} ({pattern['severity']:.1f}/10)

💡 <b>What this means:</b>
Market absorbing heavy selling without falling
Buyers stepping in at this level
Potential bottom forming

🎯 <b>Context:</b>
Smart money accumulating from weak hands
Price likely to reverse or hold

⚠️ <b>Consider buying dips into this zone!</b>
        """
    
    elif pattern_type == 'BUYING_CLIMAX':
        message = f"""
🚨 <b>BUYING CLIMAX DETECTED</b>

⚠️ <b>EXHAUSTION PATTERN</b>

📊 <b>Analysis:</b>
Current Delta: {pattern['delta']:+,.0f} BTC
Average Delta: {pattern['avg_delta']:+,.0f} BTC
Spike: {(pattern['delta'] / pattern['avg_delta']):.1f}x above average
Price Momentum: {pattern['price_momentum']:.2f}% (weak)

💡 <b>What this means:</b>
Massive buying spike with minimal price follow-through
Late buyers piling in at the top
Classic blow-off top pattern

🎯 <b>Famous Example:</b>
BTC $73,750 ATH - exactly this pattern!

⚠️ <b>EXTREME CAUTION - Potential reversal imminent!</b>
        """
    
    elif pattern_type == 'SELLING_CLIMAX':
        message = f"""
🚨 <b>SELLING CLIMAX DETECTED</b>

⚠️ <b>EXHAUSTION PATTERN</b>

📊 <b>Analysis:</b>
Current Delta: {pattern['delta']:+,.0f} BTC
Average Delta: {pattern['avg_delta']:+,.0f} BTC
Spike: {(abs(pattern['delta']) / pattern['avg_delta']):.1f}x above average
Price Momentum: {pattern['price_momentum']:.2f}% (weak)

💡 <b>What this means:</b>
Massive selling spike with minimal price follow-through
Panic sellers capitulating at the bottom
Classic washout pattern

🎯 <b>Action:</b>
Potential bottom forming - watch for reversal

⚠️ <b>EXTREME OPPORTUNITY - But wait for confirmation!</b>
        """
    
    elif pattern_type == 'MOMENTUM_BUILDING':
        direction_emoji = "🟢" if pattern['direction'] == 'BULLISH' else "🔴"
        message = f"""
{direction_emoji} <b>MOMENTUM BUILDING - {pattern['direction']}</b>

📊 <b>Analysis:</b>
Delta: {pattern['current_delta']:+,.0f} BTC (increasing)
Strength: {'⭐' * int(pattern['strength'])} ({pattern['strength']:.1f}/10)

💡 <b>What this means:</b>
Consistent orderflow building before price moves
Smart money positioning

🎯 <b>Action:</b>
Trend likely to continue in this direction
        """
    
    elif pattern_type == 'MOMENTUM_FADING':
        direction_emoji = "🟢" if pattern['direction'] == 'BULLISH' else "🔴"
        message = f"""
⚠️ <b>MOMENTUM FADING - {pattern['direction']} TREND</b>

📊 <b>Analysis:</b>
Delta: {pattern['current_delta']:+,.0f} BTC (declining)
Strength: {'⭐' * int(pattern['strength'])} ({pattern['strength']:.1f}/10)

💡 <b>What this means:</b>
Orderflow weakening while trend continues
Potential exhaustion approaching

🎯 <b>Action:</b>
Trend losing steam - watch for reversal signs
        """
    
    else:
        print(f"Unknown pattern type: {pattern_type}")
        return
    
    send_telegram(message)
    print(f"✅ Sent {pattern_type} alert to Telegram")

# ============= STOP/TARGET CALCULATION =============
def calculate_stops_targets(setup_type, direction, entry, data):
    """
    Calculate dynamic stops and targets based on TPO structure
    Returns: (stop, target, rr_ratio)
    """
    stop = 0
    target = 0
    
    if setup_type == "VA RE-ENTRY":
        vah = float(data.get('vah', 0))
        val = float(data.get('val', 0))
        
        if direction == "LONG":
            stop = val - 500  # Below VAL
            target = vah  # Target VAH
        else:  # SHORT
            stop = vah + 500  # Above VAH
            target = val  # Target VAL
    
    elif setup_type == "POOR LOW":
        # Target previous day VAH or +2000
        pdvah = float(data.get('pdvah', entry + 2000))
        stop = entry - 800  # Tight stop below poor low
        target = pdvah if pdvah > entry else entry + 2000
    
    elif setup_type == "POOR HIGH":
        # Target previous day VAL or -2000
        pdval = float(data.get('pdval', entry - 2000))
        stop = entry + 800  # Tight stop above poor high
        target = pdval if pdval < entry else entry - 2000
    
    elif "SINGLE PRINT" in setup_type:
        # Already has good range from webhook
        level_high = float(data.get('level_high', entry + 1500))
        level_low = float(data.get('level_low', entry - 1500))
        
        if direction == "LONG":
            stop = level_low - 500
            target = level_high
        else:  # SHORT
            stop = level_high + 500
            target = level_low
    
    elif "NAKED POC" in setup_type:
        # Naked POC (Daily, Weekly, Monthly, or Combined)
        pdvah = float(data.get('pdvah', entry + 1500))
        pdval = float(data.get('pdval', entry - 1500))
        
        if direction == "LONG":
            stop = entry - 800
            # Target PDVAH if available and reasonable, otherwise +1500
            target = pdvah if pdvah > entry else entry + 1500
        else:  # SHORT
            stop = entry + 800
            # Target PDVAL if available and reasonable, otherwise -1500
            target = pdval if pdval < entry else entry - 1500
    
    else:
        # Default fallback
        if direction == "LONG":
            stop = entry - 1000
            target = entry + 2000
        else:
            stop = entry + 1000
            target = entry - 2000
    
    # Calculate R:R
    risk = abs(entry - stop)
    reward = abs(target - entry)
    rr_ratio = reward / risk if risk > 0 else 0
    
    return stop, target, rr_ratio

# ============= POSITION SIZING =============
def calculate_position_size(entry, stop, account_size=ACCOUNT_SIZE, risk_pct=RISK_PER_TRADE):
    """
    Calculate position size based on account risk
    Returns BTC position size
    """
    risk_amount = account_size * risk_pct
    price_risk = abs(entry - stop)
    
    if price_risk == 0:
        return 0
    
    position_size = risk_amount / price_risk
    return position_size

# ============= TIMEFRAME CLASSIFICATION =============
def classify_timeframe(setup_type, rr_ratio, of_data):
    """
    Determine if scalp, day trade, or swing trade
    Returns: (timeframe_label, expected_duration)
    """
    
    # Single prints = swing
    if "SINGLE PRINT" in setup_type:
        if "Monthly" in setup_type:
            return "SWING 📅", "12-48 hours"
        else:  # Weekly
            return "SWING 📅", "6-24 hours"
    
    # Poor highs/lows with strong orderflow = scalp
    if "POOR" in setup_type:
        delta = abs(of_data.get('delta', 0))
        if delta > 3000:
            return "SCALP ⚡", "15-60 min"
        else:
            return "DAY TRADE 📊", "1-3 hours"
    
    # VA re-entry with good R:R = day trade
    if "VA RE-ENTRY" in setup_type:
        if rr_ratio >= 2.0:
            return "DAY TRADE 📊", "2-6 hours"
        else:
            return "SCALP ⚡", "30-90 min"
    
    # Naked POC
    if "NAKED POC" in setup_type:
        return "DAY TRADE 📊", "1-4 hours"
    
    # Default
    return "DAY TRADE 📊", "1-4 hours"

# ============= ORDER FLOW CONFIRMATION =============
def confirm_order_flow(direction, entry_price):
    print("=" * 80)
    print("🔍 ORDERFLOW DEBUG - START")
    print(f"Direction: {direction}")
    print(f"Entry Price: {entry_price}")
    print("=" * 80)
    
    try:
        print("📡 Step 1: Fetching orderbook delta...")
        orderbook = get_orderbook_delta()
        
        if orderbook:
            print(f"✅ Orderbook fetched successfully!")
            print(f"   Delta: {orderbook.get('delta', 'N/A')}")
            print(f"   Bid Volume: {orderbook.get('bid_volume', 'N/A')}")
            print(f"   Ask Volume: {orderbook.get('ask_volume', 'N/A')}")
            print(f"   Bid %: {orderbook.get('bid_pct', 'N/A')}")
        else:
            print("❌ Orderbook fetch FAILED - returned None")
        
        print("\n📡 Step 2: Fetching recent trades analysis...")
        trades = get_recent_trades_analysis(price_level=entry_price, range_size=100)
        
        if trades:
            print(f"✅ Trades fetched successfully!")
            print(f"   Buy %: {trades.get('buy_pct', 'N/A')}")
            print(f"   CVD: {trades.get('cvd', 'N/A')}")
            print(f"   Imbalance: {trades.get('imbalance', 'N/A')}")
        else:
            print("❌ Trades fetch FAILED - returned None")
        
        if not orderbook or not trades:
            print("\n❌ ORDERFLOW FETCH FAILED")
            print(f"   Orderbook: {'OK' if orderbook else 'FAILED'}")
            print(f"   Trades: {'OK' if trades else 'FAILED'}")
            return False, "Unable to fetch order flow data", {}, 0
        
        print("\n✅ All orderflow data fetched successfully!")
        
        delta = orderbook['delta']
        buy_pct = trades['buy_pct']
        imbalance = trades['imbalance']
        
        data = {
            'delta': delta,
            'buy_pct': buy_pct,
            'imbalance_ratio': imbalance['ratio'] if imbalance else None
        }
        
        print(f"\n📊 Scoring for {direction} trade...")
        
        if direction == "LONG":
            reasons = []
            score = 0
            
            if delta > DELTA_THRESHOLD_STRONG:
                reasons.append(f"✅ Strong delta: +{delta:,.0f} BTC")
                score += 3
                print(f"   ✅ Strong positive delta: +3 points")
            elif delta > 0:
                reasons.append(f"⚠️ Weak delta: +{delta:,.0f} BTC")
                score += 1
                print(f"   ⚠️ Weak positive delta: +1 point")
            else:
                reasons.append(f"❌ Negative delta: {delta:,.0f} BTC")
                print(f"   ❌ Negative delta: 0 points")
            
            if buy_pct >= BUY_PRESSURE_MIN:
                reasons.append(f"✅ Strong buys: {buy_pct:.1f}%")
                score += 2
                print(f"   ✅ Strong buy pressure: +2 points")
            else:
                reasons.append(f"❌ Weak buys: {buy_pct:.1f}%")
                print(f"   ❌ Weak buy pressure: 0 points")
            
            if imbalance and imbalance['is_buy_imbalance']:
                reasons.append(f"✅ Buy imbalance: {imbalance['ratio']:.1f}:1")
                score += 2
                print(f"   ✅ Buy imbalance detected: +2 points")
            
            print(f"\n📊 FINAL SCORE: {score}/7")
            print(f"   Threshold: 5 (need 5+ to confirm)")
            print(f"   Result: {'✅ CONFIRMED' if score >= 5 else '❌ REJECTED'}")
            
            confirmed = score >= 5
            return confirmed, "\n".join(reasons), data, score
        
        elif direction == "SHORT":
            reasons = []
            score = 0
            
            if delta < -DELTA_THRESHOLD_STRONG:
                reasons.append(f"✅ Strong delta: {delta:,.0f} BTC")
                score += 3
                print(f"   ✅ Strong negative delta: +3 points")
            elif delta < 0:
                reasons.append(f"⚠️ Weak delta: {delta:,.0f} BTC")
                score += 1
                print(f"   ⚠️ Weak negative delta: +1 point")
            else:
                reasons.append(f"❌ Positive delta: +{delta:,.0f} BTC")
                print(f"   ❌ Positive delta: 0 points")
            
            sell_pct = 100 - buy_pct
            if sell_pct >= BUY_PRESSURE_MIN:
                reasons.append(f"✅ Strong sells: {sell_pct:.1f}%")
                score += 2
                print(f"   ✅ Strong sell pressure: +2 points")
            else:
                reasons.append(f"❌ Weak sells: {sell_pct:.1f}%")
                print(f"   ❌ Weak sell pressure: 0 points")
            
            if imbalance and imbalance['is_sell_imbalance']:
                reasons.append(f"✅ Sell imbalance: 1:{imbalance['ratio']:.1f}")
                score += 2
                print(f"   ✅ Sell imbalance detected: +2 points")
            
            print(f"\n📊 FINAL SCORE: {score}/7")
            print(f"   Threshold: 5 (need 5+ to confirm)")
            print(f"   Result: {'✅ CONFIRMED' if score >= 5 else '❌ REJECTED'}")
            
            confirmed = score >= 5
            return confirmed, "\n".join(reasons), data, score
        
        return False, "Unknown direction", data, 0
    
    except requests.exceptions.Timeout as e:
        print(f"\n❌ TIMEOUT ERROR!")
        print(f"   Error: {str(e)}")
        import traceback
        traceback.print_exc()
        return False, "⚠️ Orderflow API Timeout", {}, 0
    
    except requests.exceptions.RequestException as e:
        print(f"\n❌ NETWORK ERROR!")
        print(f"   Error: {str(e)}")
        import traceback
        traceback.print_exc()
        return False, "⚠️ Network Error", {}, 0
    
    except Exception as e:
        print(f"\n❌ UNKNOWN ERROR!")
        print(f"   Error Type: {type(e).__name__}")
        print(f"   Error Message: {str(e)}")
        import traceback
        print("\nFull Traceback:")
        traceback.print_exc()
        return False, f"⚠️ Error: {str(e)}", {}, 0
    
    finally:
        print("🔍 ORDERFLOW DEBUG - END")
        print("=" * 80)

# ============= TELEGRAM =============
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=10)
        print(f"✅ Sent")
    except:
        pass

# ============= ENHANCED ORDER FLOW MONITORING WITH MARKET SCANNER =============
def monitor_orderflow():
    global last_orderflow_check
    print("📊 Order flow monitoring with MARKET SCANNER started")
    
    while True:
        try:
            time.sleep(ORDERFLOW_INTERVAL)
            
            print("\n" + "="*80)
            print("📊 ORDERFLOW CHECK + MARKET SCAN")
            print("="*80)
            
            current_price = get_current_price()
            orderbook = get_orderbook_delta()
            trades = get_recent_trades_analysis()
            
            if not current_price or not orderbook or not trades:
                print("❌ Failed to fetch data, skipping this cycle")
                continue
            
            delta = orderbook['delta']
            bid_pct = orderbook['bid_pct']
            buy_pct = trades['buy_pct']
            cvd = trades['cvd']
            
            # Store current reading in history
            current_reading = {
                'timestamp': datetime.now(timezone.utc),
                'price': current_price,
                'delta': delta,
                'bid_pct': bid_pct,
                'buy_pct': buy_pct,
                'cvd': cvd
            }
            
            orderflow_history.append(current_reading)
            print(f"✅ Stored reading #{len(orderflow_history)} in history")
            
            # Update market bias (existing logic)
            if delta > 2000 and cvd > 50:
                bias = "🟢 BULLISH"
            elif delta < -2000 and cvd < -50:
                bias = "🔴 BEARISH"
            else:
                bias = "⚪ NEUTRAL"
            
            last_orderflow_check = {
                'bias': bias,
                'delta': delta,
                'cvd': cvd,
                'timestamp': datetime.now(timezone.utc)
            }
            
            # ============= MARKET SCANNER - RUN PATTERN DETECTION =============
            print("\n🔍 Running pattern detection...")
            
            patterns_detected = []
            
            # 1. Check for divergences
            if len(orderflow_history) >= DIVERGENCE_LOOKBACK:
                divergence = detect_divergence(orderflow_history)
                if divergence:
                    print(f"   🚨 DIVERGENCE: {divergence['type']} (strength: {divergence['strength']:.1f}/10)")
                    patterns_detected.append(divergence)
                    send_pattern_alert(divergence)
            
            # 2. Check for absorption
            if len(orderflow_history) >= 2:
                absorption = detect_absorption(current_reading, orderflow_history[-2])
                if absorption:
                    print(f"   🚨 ABSORPTION: {absorption['type']} (severity: {absorption['severity']:.1f}/10)")
                    patterns_detected.append(absorption)
                    send_pattern_alert(absorption)
            
            # 3. Check for exhaustion
            if len(orderflow_history) >= 3:
                exhaustion = detect_exhaustion(current_reading, orderflow_history)
                if exhaustion:
                    print(f"   🚨 EXHAUSTION: {exhaustion['type']} (severity: {exhaustion['severity']:.1f}/10)")
                    patterns_detected.append(exhaustion)
                    send_pattern_alert(exhaustion)
            
            # 4. Check for momentum shifts
            if len(orderflow_history) >= 5:
                momentum = detect_momentum_shift(orderflow_history)
                if momentum:
                    print(f"   🚨 MOMENTUM: {momentum['type']} - {momentum['direction']} (strength: {momentum['strength']:.1f}/10)")
                    patterns_detected.append(momentum)
                    send_pattern_alert(momentum)
            
            if not patterns_detected:
                print("   ✅ No significant patterns detected")
            
            # Send regular orderflow update (less frequent now - every 4 cycles = 1 hour)
            # This prevents spam since we now have pattern alerts
            if len(orderflow_history) % 4 == 0:
                message = f"""
📊 <b>ORDER FLOW UPDATE</b>

💰 Price: ${current_price:,.0f}

<b>ORDERBOOK</b>
Δ: {delta:+,.0f} BTC
Bids: {bid_pct:.1f}%

<b>TRADES</b>
Buys: {buy_pct:.1f}%
CVD: {cvd:+,.0f} BTC

Market: {bias}

🔍 Scanner: {len(patterns_detected)} pattern(s) detected this cycle
                """
                send_telegram(message)
        
        except Exception as e:
            print(f"❌ Order flow monitor error: {e}")
            import traceback
            traceback.print_exc()

# Continue with the rest of the file (monitor_trades, webhook, etc.)...

# ============= TRADE MONITORING =============
def monitor_trades():
    print("📊 Trade monitoring started")
    
    while True:
        try:
            time.sleep(TRADE_CHECK_INTERVAL)
            
            with trade_lock:
                if not active_trades:
                    continue
                
                current_price = get_current_price()
                orderbook = get_orderbook_delta()
                
                if not current_price or not orderbook:
                    continue
                
                current_delta = orderbook['delta']
                trades_to_remove = []
                
                for trade_id, info in active_trades.items():
                    setup_type = info['setup_type']
                    direction = info['direction']
                    entry = info['entry']
                    target = info['target']
                    stop = info.get('stop', 0)
                    entry_delta = info['entry_delta']
                    alerts_sent = info['alerts_sent']
                    
                    now = datetime.now(timezone.utc)
                    hours_since_update = (now - info['last_update']).total_seconds() / 3600
                    
                    # Check stop loss
                    if direction == "LONG" and current_price <= stop:
                        send_telegram(f"""
🛑 <b>{setup_type} - STOP HIT</b>

Entry: ${entry:,.0f}
Stop: ${stop:,.0f}
Exit: ${current_price:,.0f}

Loss: ${current_price - entry:,.0f}
                        """)
                        trades_to_remove.append(trade_id)
                        continue
                    
                    elif direction == "SHORT" and current_price >= stop:
                        send_telegram(f"""
🛑 <b>{setup_type} - STOP HIT</b>

Entry: ${entry:,.0f}
Stop: ${stop:,.0f}
Exit: ${current_price:,.0f}

Loss: ${entry - current_price:,.0f}
                        """)
                        trades_to_remove.append(trade_id)
                        continue
                    
                    # Monitor direction
                    if direction == "LONG":
                        # Delta reversal warning
                        if entry_delta > 2000 and current_delta < -2000 and 'weakening' not in alerts_sent:
                            send_telegram(f"""
🚨 <b>{setup_type} WEAKENING</b>

Entry Δ: +{entry_delta:,.0f} BTC
Current Δ: {current_delta:,.0f} BTC

⚠️ <b>CONSIDER EXIT</b>

Price: ${current_price:,.0f}
P&L: ${current_price - entry:+,.0f}
                            """)
                            alerts_sent.append('weakening')
                        
                        # Target approach
                        if target - current_price < 500 and 'target_approach' not in alerts_sent:
                            send_telegram(f"🎯 <b>{setup_type} - APPROACHING TARGET</b>\n\nCurrent: ${current_price:,.0f}\nTarget: ${target:,.0f}")
                            alerts_sent.append('target_approach')
                        
                        # Target hit
                        if current_price >= target:
                            send_telegram(f"✅ <b>{setup_type} - TARGET HIT</b>\n\nProfit: ${target - entry:,.0f}\n🎉")
                            trades_to_remove.append(trade_id)
                            continue
                        
                        # Hourly update
                        if hours_since_update >= 1.0:
                            status = "STRONG 💪" if current_delta > 1500 else "WEAKENING ⚠️" if current_delta < -1000 else "NEUTRAL ⚪"
                            send_telegram(f"""
📊 <b>{setup_type} UPDATE</b>

Entry: ${entry:,.0f}
Current: ${current_price:,.0f}
P&L: ${current_price - entry:+,.0f}

Entry Δ: {entry_delta:+,.0f} BTC
Current Δ: {current_delta:+,.0f} BTC

Status: {status}
                            """)
                            info['last_update'] = datetime.now(timezone.utc)
                    
                    elif direction == "SHORT":
                        # Delta reversal warning
                        if entry_delta < -2000 and current_delta > 2000 and 'weakening' not in alerts_sent:
                            send_telegram(f"""
🚨 <b>{setup_type} WEAKENING</b>

Entry Δ: {entry_delta:,.0f} BTC
Current Δ: +{current_delta:,.0f} BTC

⚠️ <b>CONSIDER EXIT</b>

Price: ${current_price:,.0f}
P&L: ${entry - current_price:+,.0f}
                            """)
                            alerts_sent.append('weakening')
                        
                        # Target approach
                        if current_price - target < 500 and 'target_approach' not in alerts_sent:
                            send_telegram(f"🎯 <b>{setup_type} - APPROACHING TARGET</b>\n\nCurrent: ${current_price:,.0f}\nTarget: ${target:,.0f}")
                            alerts_sent.append('target_approach')
                        
                        # Target hit
                        if current_price <= target:
                            send_telegram(f"✅ <b>{setup_type} - TARGET HIT</b>\n\nProfit: ${entry - target:,.0f}\n🎉")
                            trades_to_remove.append(trade_id)
                            continue
                        
                        # Hourly update
                        if hours_since_update >= 1.0:
                            status = "STRONG 💪" if current_delta < -1500 else "WEAKENING ⚠️" if current_delta > 1000 else "NEUTRAL ⚪"
                            send_telegram(f"""
📊 <b>{setup_type} UPDATE</b>

Entry: ${entry:,.0f}
Current: ${current_price:,.0f}
P&L: ${entry - current_price:+,.0f}

Entry Δ: {entry_delta:,.0f} BTC
Current Δ: {current_delta:+,.0f} BTC

Status: {status}
                            """)
                            info['last_update'] = datetime.now(timezone.utc)
                
                for tid in trades_to_remove:
                    del active_trades[tid]
        
        except Exception as e:
            print(f"❌ Trade monitor error: {e}")
            import traceback
            traceback.print_exc()

# ============= WEBHOOK - HANDLES ALL TRADINGVIEW FORMATS =============
@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.json
        print(f"📨 Webhook: {json.dumps(data, indent=2)}")
        
        alert_type = data.get('type', 'unknown')
        current_price_from_tv = float(data.get('current_price', 0))
        
        # Initialize variables
        setup_type = "UNKNOWN"
        direction = "LONG"
        entry = current_price_from_tv
        target = 0
        stop = 0
        
        # Store all webhook data for stop/target calculation
        webhook_data = data.copy()
        
        # PARSE DIFFERENT ALERT TYPES
        if alert_type == "tpo_poor_hl":
            # Poor High/Low
            level_type = data.get('level_type')
            level_price = float(data.get('level_price', 0))
            
            webhook_data['pdvah'] = data.get('pdvah', 0)
            webhook_data['pdval'] = data.get('pdval', 0)
            webhook_data['pdpoc'] = data.get('pdpoc', 0)
            webhook_data['pdh'] = data.get('pdh', 0)
            webhook_data['pdl'] = data.get('pdl', 0)
            
            if level_type == "poor_high":
                setup_type = "POOR HIGH"
                direction = "SHORT"
                entry = level_price
            elif level_type == "poor_low":
                setup_type = "POOR LOW"
                direction = "LONG"
                entry = level_price
        
        elif alert_type == "value_area_reentry":
            # VA Re-Entry
            reentry_direction = data.get('direction')
            vah = float(data.get('vah', 0))
            val = float(data.get('val', 0))
            
            webhook_data['vah'] = vah
            webhook_data['val'] = val
            webhook_data['pdpoc'] = data.get('pdpoc', 0)
            webhook_data['pdh'] = data.get('pdh', 0)
            webhook_data['pdl'] = data.get('pdl', 0)
            
            setup_type = "VA RE-ENTRY"
            if reentry_direction == "from_below":
                direction = "LONG"
                entry = val
            elif reentry_direction == "from_above":
                direction = "SHORT"
                entry = vah
        
        elif alert_type == "naked_poc":
            # Naked POC
            level_price = float(data.get('level_price', 0))
            poc_type = data.get('poc_type', 'daily')
            poc_count = data.get('poc_count', 1)
            
            webhook_data['pdvah'] = data.get('pdvah', 0)
            webhook_data['pdval'] = data.get('pdval', 0)
            webhook_data['pdpoc'] = data.get('pdpoc', 0)
            webhook_data['pdh'] = data.get('pdh', 0)
            webhook_data['pdl'] = data.get('pdl', 0)
            
            if "monthly" in poc_type:
                setup_type = "NAKED POC (Monthly)"
            elif "weekly" in poc_type:
                setup_type = "NAKED POC (Weekly)"
            else:
                setup_type = "NAKED POC"
            
            if poc_count > 1 or "+" in poc_type:
                setup_type += f" x{poc_count}"
            
            current_price = get_current_price()
            if current_price and current_price < level_price:
                direction = "LONG"
                entry = level_price
            else:
                direction = "SHORT"
                entry = level_price
        
        elif alert_type == "single_print":
            # Single Print
            sp_type = data.get('sp_type', 'weekly')
            level_high = float(data.get('level_high', 0))
            level_low = float(data.get('level_low', 0))
            
            webhook_data['level_high'] = level_high
            webhook_data['level_low'] = level_low
            webhook_data['pdvah'] = data.get('pdvah', 0)
            webhook_data['pdval'] = data.get('pdval', 0)
            webhook_data['pdpoc'] = data.get('pdpoc', 0)
            webhook_data['pdh'] = data.get('pdh', 0)
            webhook_data['pdl'] = data.get('pdl', 0)
            
            if sp_type == "weekly":
                setup_type = "SINGLE PRINT (Weekly)"
            elif sp_type == "monthly":
                setup_type = "SINGLE PRINT (Monthly)"
            else:
                setup_type = "SINGLE PRINT"
            
            current_price = get_current_price()
            if current_price and current_price < level_low:
                direction = "LONG"
                entry = level_low
            else:
                direction = "SHORT"
                entry = level_high
        
        # CALCULATE STOPS & TARGETS
        stop, target, rr_ratio = calculate_stops_targets(setup_type, direction, entry, webhook_data)
        
        # ORDER FLOW CONFIRMATION
        confirmed, reason, of_data, score = confirm_order_flow(direction, entry)
        
        # CONFIDENCE & STRENGTH
        max_score = 7
        confidence_stars = "⭐" * min(score, 5)
        strength = "HIGH CONVICTION 🔥" if score >= 6 else "STANDARD ✅" if score >= 5 else "WEAK ⚠️"
        
        # TIMEFRAME CLASSIFICATION
        timeframe, duration = classify_timeframe(setup_type, rr_ratio, of_data)
        
        # POSITION SIZING
        position = calculate_position_size(entry, stop)
        
        # MARKET BIAS CONTEXT
        market_bias = last_orderflow_check.get('bias', '⚪ UNKNOWN')
        bias_aligned = (
            (direction == "LONG" and "🟢" in market_bias) or 
            (direction == "SHORT" and "🔴" in market_bias)
        )
        confluence = "✅ ALIGNED" if bias_aligned else "⚠️ COUNTER-TREND"
        
        if confirmed:
            alert_emoji = "🟢" if direction == "LONG" else "🔴"
            message = f"""
{alert_emoji} <b>{setup_type} - {direction} ✅ CONFIRMED</b>

<b>CONFIDENCE: {confidence_stars} ({score}/{max_score})</b>
Strength: {strength}

📍 <b>TRADE SETUP</b>
Entry: ${entry:,.0f}
Stop: ${stop:,.0f}
Target: ${target:,.0f}
R:R: {rr_ratio:.2f}

💰 Position: {position:.4f} BTC ({RISK_PER_TRADE*100:.0f}% risk)
⏱️ Timeframe: {timeframe} ({duration})

📊 Market Bias: {market_bias} {confluence}

<b>ORDER FLOW:</b>
{reason}

📈 <b>TAKING {direction}</b>
🔄 Monitoring...
            """
            
            send_telegram(message)
            
            trade_id = f"{setup_type}_{int(time.time())}"
            with trade_lock:
                active_trades[trade_id] = {
                    'setup_type': setup_type,
                    'direction': direction,
                    'entry': entry,
                    'stop': stop,
                    'target': target,
                    'rr_ratio': rr_ratio,
                    'position': position,
                    'entry_delta': of_data['delta'],
                    'entry_time': datetime.now(timezone.utc),
                    'last_update': datetime.now(timezone.utc),
                    'alerts_sent': []
                }
            
            print(f"✅ Trade confirmed: {trade_id}")
        
        else:
            message = f"""
⚠️ <b>{setup_type} - {direction} ❌ REJECTED</b>

<b>CONFIDENCE: {confidence_stars} ({score}/{max_score})</b>
Strength: {strength}

Entry: ${entry:,.0f}
Target: ${target:,.0f}
R:R: {rr_ratio:.2f}

<b>ORDER FLOW:</b>
{reason}

🚫 <b>SKIPPING - Insufficient Confirmation</b>
            """
            
            send_telegram(message)
            print(f"❌ Trade rejected")
        
        return jsonify({"status": "success"}), 200
    
    except Exception as e:
        print(f"❌ Webhook error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/', methods=['GET'])
def home():
    return f"""
    <h1>🚀 Beast Mode V3 - MARKET SCANNER EDITION</h1>
    <p>Active Trades: {len(active_trades)}</p>
    <p>History Size: {len(orderflow_history)}/{HISTORY_SIZE}</p>
    <h3>Features:</h3>
    <ul>
        <li>✅ TPO Structure (TradingView)</li>
        <li>✅ Order Flow Filter with Scoring</li>
        <li>✅ Dynamic Stops & Targets</li>
        <li>✅ Position Sizing (2% risk)</li>
        <li>✅ Timeframe Classification</li>
        <li>✅ Market Bias Context</li>
        <li>✅ Continuous Monitoring</li>
        <li>✅ Trade Alerts</li>
        <li>🔥 <b>MARKET SCANNER (NEW!)</b></li>
        <li>🔥 Divergence Detection</li>
        <li>🔥 Absorption Patterns</li>
        <li>🔥 Exhaustion Signals</li>
        <li>🔥 Momentum Shifts</li>
    </ul>
    <h3>Configuration:</h3>
    <ul>
        <li>Account: ${ACCOUNT_SIZE:,.0f}</li>
        <li>Risk: {RISK_PER_TRADE*100:.0f}% per trade</li>
        <li>History: {HISTORY_SIZE} readings</li>
        <li>Scan Interval: {ORDERFLOW_INTERVAL}s ({ORDERFLOW_INTERVAL/60:.0f} min)</li>
    </ul>
    """, 200

if __name__ == '__main__':
    print("🚀 Beast Mode V3 - MARKET SCANNER EDITION")
    print("="*80)
    print("Features:")
    print("  ✅ Entry Confirmation (TPO + Orderflow)")
    print("  ✅ Trade Monitoring (Weakness Alerts)")
    print("  🔥 MARKET SCANNER (Divergences, Exhaustion, Absorption)")
    print("="*80)
    
    # Start monitoring threads
    orderflow_thread = threading.Thread(target=monitor_orderflow, daemon=True)
    orderflow_thread.start()
    
    trade_thread = threading.Thread(target=monitor_trades, daemon=True)
    trade_thread.start()
    
    # Send startup message
    send_telegram(f"""
🚀 <b>Beast Mode V3 - MARKET SCANNER Started</b>

📊 Order Flow: ON
🔄 Monitoring: ON
✅ Filter: ACTIVE
🔥 <b>Market Scanner: ACTIVE</b>

💰 Account: ${ACCOUNT_SIZE:,.0f}
📊 Risk: {RISK_PER_TRADE*100:.0f}% per trade

<b>New Scanner Features:</b>
• Divergence Detection (bullish/bearish)
• Absorption Patterns (supply/demand)
• Exhaustion Signals (climax patterns)
• Momentum Shift Warnings

<b>Scanning every {ORDERFLOW_INTERVAL/60:.0f} minutes!</b>
    """)
    
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
