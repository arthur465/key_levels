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

# Structure detection settings (from LuxAlgo indicator)
FRACTAL_LENGTH = 5  # Default from indicator
STRUCTURE_CANDLE_LIMIT = 100  # How many candles to fetch for structure

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

# ============= NEW: ACCUMULATION/DISTRIBUTION CONTEXT =============
# Track recent absorption events to detect if they lead to moves
absorption_tracker = deque(maxlen=10)

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
    alerts = [a for alert in alert_memory if a['timestamp'] > cutoff]
    
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
    # NEW: Track accumulation patterns
    'accumulation': {
        'bullish': {'wins': 0, 'losses': 0, 'pending': []},
        'bearish': {'wins': 0, 'losses': 0, 'pending': []},
    },
    'fibs': {
        '0.382': {'wins': 0, 'losses': 0, 'pending': []},
        '0.618': {'wins': 0, 'losses': 0, 'pending': []},
        '0.786': {'wins': 0, 'losses': 0, 'pending': []},
    }
}

# ============= MARKET STRUCTURE TRACKING (LuxAlgo Logic) =============
market_structure = {
    'trend': 'NEUTRAL',  # 'BULLISH', 'BEARISH', 'NEUTRAL'
    'last_event': None,  # 'BOS' or 'CHOCH'
    'last_event_type': None,  # 'bullish' or 'bearish'
    'last_event_price': 0,
    'last_event_time': None,
    'swing_high': None,  # {'price': float, 'index': int, 'crossed': bool}
    'swing_low': None,   # {'price': float, 'index': int, 'crossed': bool}
    'support_level': None,
    'resistance_level': None,
    'os': 0  # Order state: 1=bullish, -1=bearish, 0=neutral
}

# ============= KEY LEVELS TRACKING (Institutional Zones) =============
key_levels = {
    'daily_open': None,
    'prev_day_high': None,
    'prev_day_low': None,
    'weekly_open': None,
    'prev_week_high': None,
    'prev_week_low': None,
    'monthly_open': None,
    'prev_month_high': None,
    'prev_month_low': None,
    'last_update': None
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
    
    elif pattern_type == 'accumulation':
        pattern_performance['accumulation'][pattern_subtype]['pending'].append(prediction)
    
    elif pattern_type == 'fib':
        pattern_performance['fibs'][pattern_subtype]['pending'].append(prediction)

# ============= NEW: CONTEXT-AWARE ABSORPTION DETECTION =============

def determine_absorption_context(current_price, current_delta, price_change):
    """
    Determine if heavy orderflow + minimal price movement is:
    - BULLISH ACCUMULATION (buyers loading up at demand before pump)
    - BEARISH DISTRIBUTION (sellers unloading at supply before dump)
    - BULLISH ABSORPTION (sellers trapped, buyers in control)
    - BEARISH ABSORPTION (buyers trapped, sellers in control)
    
    Returns: {
        'pattern': 'accumulation' | 'distribution' | 'absorption',
        'bias': 'bullish' | 'bearish',
        'confidence': float (1-10),
        'reason': str
    }
    """
    
    context = {
        'pattern': None,
        'bias': None,
        'confidence': 5.0,
        'reason': ''
    }
    
    # Get market structure
    structure_trend = market_structure.get('trend', 'NEUTRAL')
    os_value = market_structure.get('os', 0)
    
    # Check proximity to key levels
    nearby_levels = check_key_level_proximity(current_price, threshold_pct=0.3)
    
    # Classify nearby levels
    support_nearby = any(
        'Low' in level['name'] or 'Open' in level['name'] 
        for level in nearby_levels 
        if level['distance'] < 0
    )
    resistance_nearby = any(
        'High' in level['name'] 
        for level in nearby_levels 
        if level['distance'] > 0
    )
    
    # Check if price is consolidating vs moving
    if len(orderflow_history) >= 5:
        recent_prices = [h['price'] for h in list(orderflow_history)[-5:]]
        price_range_pct = ((max(recent_prices) - min(recent_prices)) / min(recent_prices)) * 100
        is_consolidating = price_range_pct < 0.5  # Less than 0.5% range = consolidation
    else:
        is_consolidating = False
    
    # ========== DECISION TREE ==========
    
    # HEAVY BUYING (positive delta)
    if current_delta > 0:
        
        # Case 1: Bullish structure + buying at support/demand = ACCUMULATION (BULLISH)
        if (structure_trend == 'BULLISH' or os_value == 1) and (support_nearby or is_consolidating):
            context['pattern'] = 'accumulation'
            context['bias'] = 'bullish'
            context['confidence'] = 7.5
            context['reason'] = 'Heavy buying at demand zone in bullish structure = Accumulation before markup'
        
        # Case 2: Bearish structure + buying at resistance = ABSORPTION (BEARISH)
        elif (structure_trend == 'BEARISH' or os_value == -1) and resistance_nearby:
            context['pattern'] = 'absorption'
            context['bias'] = 'bearish'
            context['confidence'] = 7.0
            context['reason'] = 'Heavy buying into resistance in bearish structure = Sellers absorbing, buyers trapped'
        
        # Case 3: Neutral structure + buying + consolidation = ACCUMULATION (BULLISH)
        elif structure_trend == 'NEUTRAL' and is_consolidating:
            context['pattern'] = 'accumulation'
            context['bias'] = 'bullish'
            context['confidence'] = 6.0
            context['reason'] = 'Heavy buying during consolidation = Likely accumulation phase'
        
        # Case 4: No clear context = check price momentum after
        else:
            context['pattern'] = 'uncertain'
            context['bias'] = 'neutral'
            context['confidence'] = 4.0
            context['reason'] = 'Heavy buying but no clear structural context - wait for confirmation'
    
    # HEAVY SELLING (negative delta)
    else:
        
        # Case 1: Bearish structure + selling at resistance/supply = DISTRIBUTION (BEARISH)
        if (structure_trend == 'BEARISH' or os_value == -1) and (resistance_nearby or is_consolidating):
            context['pattern'] = 'distribution'
            context['bias'] = 'bearish'
            context['confidence'] = 7.5
            context['reason'] = 'Heavy selling at supply zone in bearish structure = Distribution before markdown'
        
        # Case 2: Bullish structure + selling at support = ABSORPTION (BULLISH)
        elif (structure_trend == 'BULLISH' or os_value == 1) and support_nearby:
            context['pattern'] = 'absorption'
            context['bias'] = 'bullish'
            context['confidence'] = 7.0
            context['reason'] = 'Heavy selling into support in bullish structure = Buyers absorbing, sellers trapped'
        
        # Case 3: Neutral structure + selling + consolidation = DISTRIBUTION (BEARISH)
        elif structure_trend == 'NEUTRAL' and is_consolidating:
            context['pattern'] = 'distribution'
            context['bias'] = 'bearish'
            context['confidence'] = 6.0
            context['reason'] = 'Heavy selling during consolidation = Likely distribution phase'
        
        # Case 4: No clear context
        else:
            context['pattern'] = 'uncertain'
            context['bias'] = 'neutral'
            context['confidence'] = 4.0
            context['reason'] = 'Heavy selling but no clear structural context - wait for confirmation'
    
    return context

def track_absorption_outcome():
    """
    Track previous absorption events to see if they led to moves
    This helps us learn which contexts are most reliable
    """
    current_price = get_current_price()
    if not current_price:
        return
    
    now = datetime.now(timezone.utc)
    
    for event in list(absorption_tracker):
        # Check events 30-120 minutes old
        age_minutes = (now - event['timestamp']).total_seconds() / 60
        
        if age_minutes > 30 and age_minutes < 120 and not event.get('resolved'):
            entry_price = event['price']
            expected_direction = event['bias']
            
            # Check if it moved as expected
            price_move_pct = ((current_price - entry_price) / entry_price) * 100
            
            if expected_direction == 'bullish':
                if price_move_pct > 0.5:  # Moved up 0.5%+
                    event['resolved'] = True
                    event['outcome'] = 'win'
                    print(f"   ✅ {event['pattern'].upper()} prediction WIN: +{price_move_pct:.2f}%")
                elif age_minutes > 90 and price_move_pct < -0.3:  # Moved down
                    event['resolved'] = True
                    event['outcome'] = 'loss'
                    print(f"   ❌ {event['pattern'].upper()} prediction LOSS: {price_move_pct:.2f}%")
            
            elif expected_direction == 'bearish':
                if price_move_pct < -0.5:  # Moved down 0.5%+
                    event['resolved'] = True
                    event['outcome'] = 'win'
                    print(f"   ✅ {event['pattern'].upper()} prediction WIN: {price_move_pct:.2f}%")
                elif age_minutes > 90 and price_move_pct > 0.3:  # Moved up
                    event['resolved'] = True
                    event['outcome'] = 'loss'
                    print(f"   ❌ {event['pattern'].upper()} prediction LOSS: +{price_move_pct:.2f}%")

# ============= IMPROVED ABSORPTION/CLIMAX DETECTION =============

def detect_absorption_patterns(current_delta, current_price, price_change):
    """
    Detect absorption/accumulation/distribution patterns using context
    """
    
    if len(orderflow_history) < 5:
        return
    
    recent = list(orderflow_history)[-5:]
    avg_delta = sum(abs(h['delta']) for h in recent[:-1]) / 4
    ratio = abs(current_delta / avg_delta) if avg_delta != 0 else 1
    
    now = time.time()
    
    # Detect heavy orderflow with minimal price movement
    # BUYING PRESSURE
    if current_delta > 5000 and ratio > 2.0 and abs(price_change) < 0.3:
        
        # Get context-aware interpretation
        context = determine_absorption_context(current_price, current_delta, price_change)
        
        # Only alert if we have a clear bias
        if context['confidence'] >= 6.0:
            
            cooldown_key = f"absorption_{context['pattern']}_{context['bias']}"
            last_alert_time = last_pattern_alerts.get(cooldown_key, 0)
            
            if now - last_alert_time > 1800:  # 30 min cooldown
                
                # Track this event
                absorption_tracker.append({
                    'timestamp': datetime.now(timezone.utc),
                    'price': current_price,
                    'delta': current_delta,
                    'pattern': context['pattern'],
                    'bias': context['bias'],
                    'confidence': context['confidence'],
                    'resolved': False
                })
                
                # Track for performance
                track_pattern_prediction('accumulation', context['bias'], {
                    'price': current_price,
                    'direction': 'up' if context['bias'] == 'bullish' else 'down',
                    'strength': context['confidence']
                })
                
                # Get recent average delta
                avg_delta_display = sum(abs(d['delta']) for d in recent[:-1]) / 4
                
                # Build alert based on pattern type
                if context['pattern'] == 'accumulation' and context['bias'] == 'bullish':
                    emoji = "🟢"
                    title = "BULLISH ACCUMULATION"
                    interpretation = f"""<b>⚠️ WHAT THIS MEANS:</b>
Heavy buying ({abs(current_delta):,.0f} BTC) at demand zone
Price HOLDING despite absorption
Buyers are LOADING UP before markup
= BULLISH setup forming"""
                    
                    watch_for = f"""<b>🎯 WATCH FOR:</b>
• Support holding at ${current_price:,.0f}
• Volume increasing (more accumulation)
• Breakout candle forming
• Pump when accumulation complete"""
                    
                    bias_text = f"<b>⚡ BIAS: BULLISH</b>\nAccumulation at demand = markup incoming"
                    
                    plain_english = f"""<b>🗣️ IN PLAIN ENGLISH:</b>
Smart money is buying {abs(current_delta):,.0f} BTC at this level. Price is holding/consolidating = they're defending this zone. When they're done accumulating, price will pump. This is the phase BEFORE the markup."""
                
                elif context['pattern'] == 'absorption' and context['bias'] == 'bearish':
                    emoji = "🔴"
                    title = "SUPPLY ABSORPTION"
                    interpretation = f"""<b>⚠️ WHAT THIS MEANS:</b>
Heavy buying ({abs(current_delta):,.0f} BTC) into resistance
Sellers are ABSORBING all that buying
Buyers getting TRAPPED
= BEARISH reversal likely"""
                    
                    watch_for = f"""<b>🎯 WATCH FOR:</b>
• Resistance at ${current_price:,.0f}
• Volume declining (buyers exhausting)
• Reversal candle forming
• Drop to support"""
                    
                    bias_text = f"<b>⚡ BIAS: Cautiously BEARISH</b>\nHeavy buying absorbed = potential rejection"
                    
                    plain_english = f"""<b>🗣️ IN PLAIN ENGLISH:</b>
Buyers are pumping {abs(current_delta):,.0f} BTC into the market, but sellers are ABSORBING it all at resistance. When buyers run out, price will reject. Classic trap."""
                
                else:
                    return  # Skip uncertain patterns
                
                # Get structure context
                structure_info = ""
                if market_structure['trend'] != 'NEUTRAL':
                    structure_info = f"\n<b>🔷 STRUCTURE:</b> {market_structure['trend']}"
                    if market_structure.get('last_event'):
                        structure_info += f" ({market_structure['last_event']})"
                
                # Get key levels context
                nearby_levels = check_key_level_proximity(current_price, threshold_pct=0.3)
                levels_info = ""
                if nearby_levels:
                    levels_info = "\n<b>⚡ KEY LEVELS NEARBY:</b>"
                    for level in nearby_levels[:3]:
                        distance_str = f"+${level['distance']:,.0f}" if level['distance'] > 0 else f"-${abs(level['distance']):,.0f}"
                        levels_info += f"\n• {level['name']}: ${level['price']:,.0f} ({distance_str})"
                
                message = f"""
{emoji} <b>{title}</b>
Confidence: {context['confidence']:.1f}/10

<b>💎 ORDER FLOW:</b>
Δ: {current_delta:,.0f} BTC (BUYING pressure)
↳ More buying than selling
↳ HEAVY orderflow to upside

Avg Δ: {avg_delta_display:,.0f} BTC (Recent average)
↳ Current is {ratio:.1f}x heavier

<b>📊 PRICE ACTION:</b>
Price Change: {abs(price_change):.2f}% (Small movement)
↳ Despite heavy buying, price barely moved
{structure_info}
{levels_info}

{interpretation}

{watch_for}

{bias_text}

{plain_english}

<b>🔍 CONTEXT:</b>
{context['reason']}

⚠️ <i>Context-aware pattern detection v8</i>
"""
                
                send_telegram(message)
                last_pattern_alerts[cooldown_key] = now
                print(f"🚨 Alert sent: {title}")
    
    # SELLING PRESSURE
    elif current_delta < -5000 and ratio > 2.0 and abs(price_change) < 0.3:
        
        # Get context-aware interpretation
        context = determine_absorption_context(current_price, current_delta, price_change)
        
        # Only alert if we have a clear bias
        if context['confidence'] >= 6.0:
            
            cooldown_key = f"absorption_{context['pattern']}_{context['bias']}"
            last_alert_time = last_pattern_alerts.get(cooldown_key, 0)
            
            if now - last_alert_time > 1800:
                
                # Track this event
                absorption_tracker.append({
                    'timestamp': datetime.now(timezone.utc),
                    'price': current_price,
                    'delta': current_delta,
                    'pattern': context['pattern'],
                    'bias': context['bias'],
                    'confidence': context['confidence'],
                    'resolved': False
                })
                
                # Track for performance
                track_pattern_prediction('accumulation', context['bias'], {
                    'price': current_price,
                    'direction': 'down' if context['bias'] == 'bearish' else 'up',
                    'strength': context['confidence']
                })
                
                avg_delta_display = sum(abs(d['delta']) for d in recent[:-1]) / 4
                
                # Build alert based on pattern type
                if context['pattern'] == 'distribution' and context['bias'] == 'bearish':
                    emoji = "🔴"
                    title = "BEARISH DISTRIBUTION"
                    interpretation = f"""<b>⚠️ WHAT THIS MEANS:</b>
Heavy selling ({abs(current_delta):,.0f} BTC) at supply zone
Price HOLDING despite distribution
Sellers are UNLOADING before markdown
= BEARISH setup forming"""
                    
                    watch_for = f"""<b>🎯 WATCH FOR:</b>
• Resistance holding at ${current_price:,.0f}
• Volume increasing (more distribution)
• Breakdown candle forming
• Dump when distribution complete"""
                    
                    bias_text = f"<b>⚡ BIAS: BEARISH</b>\nDistribution at supply = markdown incoming"
                    
                    plain_english = f"""<b>🗣️ IN PLAIN ENGLISH:</b>
Smart money is selling {abs(current_delta):,.0f} BTC at this level. Price is holding/consolidating = they're methodically distributing. When they're done selling, price will dump. This is the phase BEFORE the markdown."""
                
                elif context['pattern'] == 'absorption' and context['bias'] == 'bullish':
                    emoji = "🟢"
                    title = "DEMAND ABSORPTION"
                    interpretation = f"""<b>⚠️ WHAT THIS MEANS:</b>
Heavy selling ({abs(current_delta):,.0f} BTC) into support
Buyers are ABSORBING all that selling
Sellers getting TRAPPED
= BULLISH reversal likely"""
                    
                    watch_for = f"""<b>🎯 WATCH FOR:</b>
• Support at ${current_price:,.0f}
• Volume declining (sellers exhausting)
• Reversal candle forming
• Pump to resistance"""
                    
                    bias_text = f"<b>⚡ BIAS: Cautiously BULLISH</b>\nHeavy selling absorbed = potential bounce"
                    
                    plain_english = f"""<b>🗣️ IN PLAIN ENGLISH:</b>
Sellers are dumping {abs(current_delta):,.0f} BTC into the market, but buyers are ABSORBING it all at support. When sellers run out, price will bounce. Classic trap."""
                
                else:
                    return
                
                # Get structure context
                structure_info = ""
                if market_structure['trend'] != 'NEUTRAL':
                    structure_info = f"\n<b>🔷 STRUCTURE:</b> {market_structure['trend']}"
                    if market_structure.get('last_event'):
                        structure_info += f" ({market_structure['last_event']})"
                
                # Get key levels context
                nearby_levels = check_key_level_proximity(current_price, threshold_pct=0.3)
                levels_info = ""
                if nearby_levels:
                    levels_info = "\n<b>⚡ KEY LEVELS NEARBY:</b>"
                    for level in nearby_levels[:3]:
                        distance_str = f"+${level['distance']:,.0f}" if level['distance'] > 0 else f"-${abs(level['distance']):,.0f}"
                        levels_info += f"\n• {level['name']}: ${level['price']:,.0f} ({distance_str})"
                
                message = f"""
{emoji} <b>{title}</b>
Confidence: {context['confidence']:.1f}/10

<b>💎 ORDER FLOW:</b>
Δ: {current_delta:,.0f} BTC (SELLING pressure)
↳ More selling than buying
↳ HEAVY orderflow to downside

Avg Δ: {avg_delta_display:,.0f} BTC (Recent average)
↳ Current is {ratio:.1f}x heavier

<b>📊 PRICE ACTION:</b>
Price Change: {abs(price_change):.2f}% (Small movement)
↳ Despite heavy selling, price barely moved
{structure_info}
{levels_info}

{interpretation}

{watch_for}

{bias_text}

{plain_english}

<b>🔍 CONTEXT:</b>
{context['reason']}

⚠️ <i>Context-aware pattern detection v8</i>
"""
                
                send_telegram(message)
                last_pattern_alerts[cooldown_key] = now
                print(f"🚨 Alert sent: {title}")

# ============= PLACEHOLDER FUNCTIONS =============
# (These would be imported from your existing code)

def send_telegram(message):
    """Send Telegram message"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"📤 [TELEGRAM DISABLED] Would send: {message[:100]}...")
        return
    
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {
            'chat_id': TELEGRAM_CHAT_ID,
            'text': message,
            'parse_mode': 'HTML'
        }
        response = requests.post(url, data=data, timeout=10)
        if response.status_code == 200:
            print("✅ Telegram message sent")
        else:
            print(f"❌ Telegram error: {response.status_code}")
    except Exception as e:
        print(f"❌ Telegram exception: {e}")

def get_current_price():
    """Get current BTC price"""
    try:
        url = "https://www.okx.com/api/v5/market/ticker"
        params = {'instId': 'BTC-USDT-SWAP'}
        response = requests.get(url, params=params, timeout=5)
        data = response.json()
        if data.get('code') == '0' and data.get('data'):
            return float(data['data'][0]['last'])
    except:
        pass
    return None

def check_key_level_proximity(current_price, threshold_pct=0.15):
    """Check if current price is near any key institutional levels"""
    nearby_levels = []
    
    if not current_price:
        return nearby_levels
    
    threshold = current_price * (threshold_pct / 100)
    
    levels_to_check = [
        ('Daily Open', key_levels.get('daily_open')),
        ('Prev Day High', key_levels.get('prev_day_high')),
        ('Prev Day Low', key_levels.get('prev_day_low')),
        ('Weekly Open', key_levels.get('weekly_open')),
        ('Prev Week High', key_levels.get('prev_week_high')),
        ('Prev Week Low', key_levels.get('prev_week_low')),
        ('Monthly Open', key_levels.get('monthly_open')),
        ('Prev Month High', key_levels.get('prev_month_high')),
        ('Prev Month Low', key_levels.get('prev_month_low')),
    ]
    
    for level_name, level_price in levels_to_check:
        if level_price and abs(current_price - level_price) <= threshold:
            distance = current_price - level_price
            nearby_levels.append({
                'name': level_name,
                'price': level_price,
                'distance': distance,
                'distance_pct': (distance / current_price) * 100
            })
    
    return nearby_levels

# ============= MAIN EXECUTION EXAMPLE =============
if __name__ == '__main__':
    print("🚀 Beast Mode V8 - ACCUMULATION-AWARE EDITION")
    print("="*80)
    print("🆕 CONTEXT-AWARE PATTERN DETECTION:")
    print("  ✅ Distinguishes ACCUMULATION from ABSORPTION")
    print("  ✅ Distinguishes DISTRIBUTION from ABSORPTION")
    print("  ✅ Uses market structure context (bullish/bearish)")
    print("  ✅ Uses key level proximity (support/resistance)")
    print("  ✅ Uses consolidation detection")
    print("  ✅ Tracks pattern outcomes for learning")
    print("="*80)
    print("🔍 PATTERN LOGIC:")
    print("  • Bullish structure + buying at demand = ACCUMULATION (bullish)")
    print("  • Bearish structure + buying at resistance = ABSORPTION (bearish)")
    print("  • Bullish structure + selling at support = ABSORPTION (bullish)")
    print("  • Bearish structure + selling at supply = DISTRIBUTION (bearish)")
    print("="*80)
    print("Ready to detect patterns correctly!")
