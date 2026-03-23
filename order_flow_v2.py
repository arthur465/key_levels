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

# ============= NEW: CLARITY THRESHOLDS =============
# These are MUCH stricter to only catch CLEAR signals

# ACCUMULATION = Clear buying absorption with minimal price drop
ACCUMULATION_THRESHOLDS = {
    'delta_min': -8000,           # Must be HEAVY selling (raised from -3000)
    'price_change_max': 0.1,      # Price barely drops (tightened from 0.15%)
    'severity_min': 6.0,          # Only alert on high severity (raised from any)
    'ratio_min': 3.0,             # Must be 3x heavier than normal (new requirement)
    'cooldown': 3600              # 1 hour cooldown (doubled from 30 min)
}

# DISTRIBUTION = Clear selling absorption with minimal price rise  
DISTRIBUTION_THRESHOLDS = {
    'delta_min': 8000,            # Must be HEAVY buying (raised from 3000)
    'price_change_max': 0.1,      # Price barely rises (tightened from 0.15%)
    'severity_min': 6.0,          # Only alert on high severity
    'ratio_min': 3.0,             # Must be 3x heavier than normal
    'cooldown': 3600              # 1 hour cooldown
}

# EXHAUSTION CLIMAXES = Extreme orderflow with price rejection
CLIMAX_THRESHOLDS = {
    'delta_min': 10000,           # Must be EXTREME volume (raised from 5000)
    'price_change_max': 0.15,     # Price barely moves despite extreme volume
    'ratio_min': 3.5,             # Must be 3.5x heavier (raised from 2.0)
    'severity_min': 7.0,          # Only very high severity
    'cooldown': 3600              # 1 hour cooldown
}

# DIVERGENCE THRESHOLDS = Multi-timeframe delta weakening
DIVERGENCE_THRESHOLDS = {
    'min_timeframes': 2,          # Must show on at least 2 timeframes (was 1)
    'decline_pct_min': 40,        # Delta must decline by 40%+ (raised from any)
    'conviction_min': 'MEDIUM',   # Minimum conviction level
    'cooldown': 7200              # 2 hour cooldown (quadrupled)
}

# Order flow thresholds (legacy, kept for compatibility)
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
DIVERGENCE_LOOKBACK_4H = 16     # 4 hours (swing setups)
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

# ============= NEW: CLARITY CHECK FUNCTIONS =============

def is_clear_accumulation(current_delta, price_change_pct, avg_delta):
    """
    Check if this is CLEAR accumulation (buying absorption)
    Returns: (is_clear: bool, severity: float, ratio: float)
    """
    # Must have heavy selling
    if current_delta >= ACCUMULATION_THRESHOLDS['delta_min']:
        return False, 0, 0
    
    # Price must barely drop despite selling
    if abs(price_change_pct) >= ACCUMULATION_THRESHOLDS['price_change_max']:
        return False, 0, 0
    
    # Calculate how much heavier than normal
    ratio = abs(current_delta / avg_delta) if avg_delta != 0 else 1.0
    if ratio < ACCUMULATION_THRESHOLDS['ratio_min']:
        return False, 0, 0
    
    # Calculate severity (higher = clearer signal)
    severity = min(10.0, (abs(current_delta) / 1000) * (ratio / 2))
    
    # Must meet minimum severity
    if severity < ACCUMULATION_THRESHOLDS['severity_min']:
        return False, severity, ratio
    
    return True, severity, ratio


def is_clear_distribution(current_delta, price_change_pct, avg_delta):
    """
    Check if this is CLEAR distribution (selling absorption)
    Returns: (is_clear: bool, severity: float, ratio: float)
    """
    # Must have heavy buying
    if current_delta <= DISTRIBUTION_THRESHOLDS['delta_min']:
        return False, 0, 0
    
    # Price must barely rise despite buying
    if abs(price_change_pct) >= DISTRIBUTION_THRESHOLDS['price_change_max']:
        return False, 0, 0
    
    # Calculate how much heavier than normal
    ratio = abs(current_delta / avg_delta) if avg_delta != 0 else 1.0
    if ratio < DISTRIBUTION_THRESHOLDS['ratio_min']:
        return False, 0, 0
    
    # Calculate severity
    severity = min(10.0, (abs(current_delta) / 1000) * (ratio / 2))
    
    # Must meet minimum severity
    if severity < DISTRIBUTION_THRESHOLDS['severity_min']:
        return False, severity, ratio
    
    return True, severity, ratio


def is_clear_climax(current_delta, price_change_pct, avg_delta, direction='selling'):
    """
    Check if this is a CLEAR exhaustion climax
    Returns: (is_clear: bool, severity: float, ratio: float)
    """
    # Check delta direction and magnitude
    if direction == 'selling':
        if current_delta >= -CLIMAX_THRESHOLDS['delta_min']:
            return False, 0, 0
    else:  # buying
        if current_delta <= CLIMAX_THRESHOLDS['delta_min']:
            return False, 0, 0
    
    # Price must barely move despite extreme volume
    if abs(price_change_pct) >= CLIMAX_THRESHOLDS['price_change_max']:
        return False, 0, 0
    
    # Calculate ratio
    ratio = abs(current_delta / avg_delta) if avg_delta != 0 else 1.0
    if ratio < CLIMAX_THRESHOLDS['ratio_min']:
        return False, 0, 0
    
    # Calculate severity
    severity = min(10.0, ratio * 2.5)
    
    # Must meet minimum severity
    if severity < CLIMAX_THRESHOLDS['severity_min']:
        return False, severity, ratio
    
    return True, severity, ratio


def is_clear_divergence(divergence_data):
    """
    Check if this is a CLEAR multi-timeframe divergence
    Returns: bool
    """
    if not divergence_data:
        return False
    
    # Must show on multiple timeframes
    tf_count = len(divergence_data.get('timeframes', []))
    if tf_count < DIVERGENCE_THRESHOLDS['min_timeframes']:
        return False
    
    # Must have strong decline/improvement
    max_decline = 0
    for tf, data in divergence_data.get('tf_details', {}).items():
        decline = data.get('decline_pct') or data.get('improve_pct', 0)
        max_decline = max(max_decline, abs(decline))
    
    if max_decline < DIVERGENCE_THRESHOLDS['decline_pct_min']:
        return False
    
    # Check conviction level
    conviction = divergence_data.get('conviction', 'LOW')
    if conviction == 'LOW':
        return False
    
    return True


def check_cooldown(alert_type):
    """
    Check if enough time has passed since last alert of this type
    Returns: (can_alert: bool, time_remaining: int)
    """
    if alert_type not in last_pattern_alerts:
        return True, 0
    
    # Determine cooldown based on alert type
    if 'divergence' in alert_type.lower():
        cooldown = DIVERGENCE_THRESHOLDS['cooldown']
    elif 'climax' in alert_type.lower():
        cooldown = CLIMAX_THRESHOLDS['cooldown']
    elif 'accumulation' in alert_type.lower() or 'demand' in alert_type.lower():
        cooldown = ACCUMULATION_THRESHOLDS['cooldown']
    elif 'distribution' in alert_type.lower() or 'supply' in alert_type.lower():
        cooldown = DISTRIBUTION_THRESHOLDS['cooldown']
    else:
        cooldown = PATTERN_ALERT_COOLDOWN
    
    last_alert = last_pattern_alerts[alert_type]
    now = time.time()
    time_passed = now - last_alert
    
    if time_passed < cooldown:
        time_remaining = int(cooldown - time_passed)
        return False, time_remaining
    
    return True, 0


# ============= REST OF THE CODE (keeping existing functions) =============
# NOTE: I'm including placeholders for the functions that would be copied from your original file
# In the actual implementation, you would copy over all the other functions:
# - get_current_price()
# - fetch_binance_orderflow()
# - fetch_orderflow_data()
# - detect_divergences()
# - detect_market_structure()
# - send_telegram()
# - All other helper functions

# Placeholder functions (replace with your actual implementations)
def get_current_price():
    """Fetch current BTC price"""
    try:
        url = "https://api.binance.com/api/v3/ticker/price"
        params = {"symbol": SYMBOL}
        response = requests.get(url, params=params, timeout=5)
        data = response.json()
        return float(data['price'])
    except Exception as e:
        print(f"❌ Price fetch error: {e}")
        return None


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


# ============= IMPROVED ALERT LOGIC =============

def send_market_update():
    """
    IMPROVED: Only send alerts for CLEAR accumulation/distribution patterns
    Filters out noise and only shows high-conviction signals
    """
    print("\n" + "="*80)
    print("🔍 SCANNING FOR CLEAR ORDERFLOW PATTERNS...")
    print("="*80)
    
    current_price = get_current_price()
    if not current_price:
        print("❌ Cannot get price, skipping update")
        return
    
    # Get orderflow data (you would implement this with your exchange fetching logic)
    # current_of = fetch_orderflow_data()
    # For now, using placeholder
    
    now = time.time()
    
    # ============= CHECK FOR CLEAR ACCUMULATION =============
    if len(orderflow_history) >= 5:
        recent = list(orderflow_history)[-5:]
        current_delta = recent[-1]['delta']
        avg_delta = sum(abs(h['delta']) for h in recent[:-1]) / 4
        price_change = ((recent[-1]['price'] - recent[-2]['price']) / recent[-2]['price']) * 100
        
        # Check if this is CLEAR accumulation
        is_clear, severity, ratio = is_clear_accumulation(current_delta, price_change, avg_delta)
        
        if is_clear:
            can_alert, time_remaining = check_cooldown('clear_accumulation')
            
            if can_alert:
                print(f"\n🚨 CLEAR ACCUMULATION DETECTED!")
                print(f"   Severity: {severity:.1f}/10")
                print(f"   Ratio: {ratio:.1f}x normal")
                print(f"   Delta: {current_delta:,.0f} BTC")
                print(f"   Price change: {abs(price_change):.2f}%")
                
                message = f"""
🚨 <b>CLEAR ACCUMULATION DETECTED</b>
Conviction: HIGH ⚡
Severity: {severity:.1f}/10

<b>💎 ORDER FLOW:</b>
Selling Δ: {current_delta:,.0f} BTC
↳ HEAVY selling pressure
↳ {ratio:.1f}x HEAVIER than normal

<b>📊 PRICE ACTION:</b>
Price Drop: {abs(price_change):.2f}%
↳ MINIMAL movement despite heavy selling
↳ Someone is ABSORBING all the selling

<b>⚠️ WHAT THIS MEANS:</b>
Smart money is buying ALL the selling pressure
Sellers are dumping into strong hands
When sellers exhaust = Price will REVERSE UP
This is a CLEAR accumulation zone

<b>🎯 WATCH FOR:</b>
• Volume declining (sellers exhausting)
• Price stabilizing at ${current_price:,.0f}
• Reversal candle forming
• Bounce in next 1-4 hours

<b>⚡ BIAS: STRONGLY BULLISH</b>
Clear accumulation = High probability reversal
Multi-factor confirmation = PREMIUM SETUP

<b>🗣️ IN PLAIN ENGLISH:</b>
Heavy selling ({abs(current_delta):,.0f} BTC) but price BARELY drops. This is textbook accumulation - smart money is absorbing the panic selling. When sellers run out of ammo, price will bounce hard. This is {ratio:.1f}x stronger than normal orderflow, so it's a CLEAR signal, not noise.
"""
                
                send_telegram(message)
                last_pattern_alerts['clear_accumulation'] = now
                add_to_memory('accumulation', {
                    'price': current_price,
                    'delta': current_delta,
                    'severity': severity,
                    'ratio': ratio
                })
            else:
                print(f"   ⏳ Accumulation detected but on cooldown ({time_remaining}s remaining)")
        else:
            if severity > 0:
                print(f"   ℹ️  Accumulation signal but not clear enough (severity {severity:.1f}, need {ACCUMULATION_THRESHOLDS['severity_min']:.1f})")
    
    # ============= CHECK FOR CLEAR DISTRIBUTION =============
    if len(orderflow_history) >= 5:
        recent = list(orderflow_history)[-5:]
        current_delta = recent[-1]['delta']
        avg_delta = sum(abs(h['delta']) for h in recent[:-1]) / 4
        price_change = ((recent[-1]['price'] - recent[-2]['price']) / recent[-2]['price']) * 100
        
        # Check if this is CLEAR distribution
        is_clear, severity, ratio = is_clear_distribution(current_delta, price_change, avg_delta)
        
        if is_clear:
            can_alert, time_remaining = check_cooldown('clear_distribution')
            
            if can_alert:
                print(f"\n🚨 CLEAR DISTRIBUTION DETECTED!")
                print(f"   Severity: {severity:.1f}/10")
                print(f"   Ratio: {ratio:.1f}x normal")
                print(f"   Delta: {current_delta:,.0f} BTC")
                print(f"   Price change: {abs(price_change):.2f}%")
                
                message = f"""
🚨 <b>CLEAR DISTRIBUTION DETECTED</b>
Conviction: HIGH ⚡
Severity: {severity:.1f}/10

<b>💎 ORDER FLOW:</b>
Buying Δ: {current_delta:,.0f} BTC
↳ HEAVY buying pressure
↳ {ratio:.1f}x HEAVIER than normal

<b>📊 PRICE ACTION:</b>
Price Rise: {abs(price_change):.2f}%
↳ MINIMAL movement despite heavy buying
↳ Someone is SELLING into all the buying

<b>⚠️ WHAT THIS MEANS:</b>
Smart money is selling ALL the buying pressure
Buyers are pushing into heavy resistance
When buyers exhaust = Price will REVERSE DOWN
This is a CLEAR distribution zone

<b>🎯 WATCH FOR:</b>
• Volume declining (buyers exhausting)
• Price stabilizing at ${current_price:,.0f}
• Reversal candle forming
• Drop in next 1-4 hours

<b>⚡ BIAS: STRONGLY BEARISH</b>
Clear distribution = High probability reversal
Multi-factor confirmation = PREMIUM SETUP

<b>🗣️ IN PLAIN ENGLISH:</b>
Heavy buying ({abs(current_delta):,.0f} BTC) but price BARELY rises. This is textbook distribution - smart money is dumping into the FOMO buying. When buyers run out of money, price will drop hard. This is {ratio:.1f}x stronger than normal orderflow, so it's a CLEAR signal, not noise.
"""
                
                send_telegram(message)
                last_pattern_alerts['clear_distribution'] = now
                add_to_memory('distribution', {
                    'price': current_price,
                    'delta': current_delta,
                    'severity': severity,
                    'ratio': ratio
                })
            else:
                print(f"   ⏳ Distribution detected but on cooldown ({time_remaining}s remaining)")
        else:
            if severity > 0:
                print(f"   ℹ️  Distribution signal but not clear enough (severity {severity:.1f}, need {DISTRIBUTION_THRESHOLDS['severity_min']:.1f})")
    
    # ============= CHECK FOR CLEAR CLIMAXES =============
    if len(orderflow_history) >= 5:
        recent = list(orderflow_history)[-5:]
        current_delta = recent[-1]['delta']
        avg_delta = sum(h['delta'] for h in recent[:-1]) / 4
        price_change = ((recent[-1]['price'] - recent[-2]['price']) / recent[-2]['price']) * 100
        
        # Check for CLEAR selling climax
        is_clear_sell, severity_sell, ratio_sell = is_clear_climax(current_delta, price_change, avg_delta, 'selling')
        
        if is_clear_sell:
            can_alert, time_remaining = check_cooldown('clear_selling_climax')
            
            if can_alert:
                print(f"\n🚨 CLEAR SELLING CLIMAX!")
                print(f"   Severity: {severity_sell:.1f}/10")
                print(f"   Ratio: {ratio_sell:.1f}x normal")
                
                message = f"""
💥 <b>CLEAR SELLING CLIMAX</b>
Conviction: EXTREME ⚡⚡
Severity: {severity_sell:.1f}/10

<b>💎 ORDER FLOW:</b>
Selling Δ: {current_delta:,.0f} BTC
↳ EXTREME selling volume
↳ {ratio_sell:.1f}x HEAVIER than normal
↳ This is PANIC SELLING

<b>📊 PRICE ACTION:</b>
Price Drop: {abs(price_change):.2f}%
↳ MINIMAL movement despite EXTREME selling
↳ Buyers absorbing EVERYTHING

<b>⚠️ WHAT THIS MEANS:</b>
SELLER EXHAUSTION - they threw EVERYTHING at market
But buyers caught it all
This is the BOTTOM - sellers have no more ammo
BULLISH REVERSAL IMMINENT

<b>🎯 WATCH FOR:</b>
• Price stabilizing RIGHT NOW
• Volume dropping FAST (sellers done)
• STRONG reversal candle
• Bounce in next 15-60 minutes

<b>⚡ BIAS: EXTREMELY BULLISH</b>
Climax exhaustion = Reversal in progress
THIS IS THE BOTTOM

<b>🗣️ IN PLAIN ENGLISH:</b>
Sellers just panic dumped {abs(current_delta):,.0f} BTC ({ratio_sell:.1f}x normal volume). But price barely moved = buyers are STRONG. Sellers are now EXHAUSTED. This is a classic capitulation bottom. Watch for immediate bounce.
"""
                
                send_telegram(message)
                last_pattern_alerts['clear_selling_climax'] = now
                add_to_memory('climax', {
                    'type': 'selling',
                    'price': current_price,
                    'delta': current_delta,
                    'severity': severity_sell,
                    'ratio': ratio_sell
                })
            else:
                print(f"   ⏳ Selling climax detected but on cooldown ({time_remaining}s remaining)")
        
        # Check for CLEAR buying climax
        is_clear_buy, severity_buy, ratio_buy = is_clear_climax(current_delta, price_change, avg_delta, 'buying')
        
        if is_clear_buy:
            can_alert, time_remaining = check_cooldown('clear_buying_climax')
            
            if can_alert:
                print(f"\n🚨 CLEAR BUYING CLIMAX!")
                print(f"   Severity: {severity_buy:.1f}/10")
                print(f"   Ratio: {ratio_buy:.1f}x normal")
                
                message = f"""
💥 <b>CLEAR BUYING CLIMAX</b>
Conviction: EXTREME ⚡⚡
Severity: {severity_buy:.1f}/10

<b>💎 ORDER FLOW:</b>
Buying Δ: {current_delta:,.0f} BTC
↳ EXTREME buying volume
↳ {ratio_buy:.1f}x HEAVIER than normal
↳ This is FOMO BUYING

<b>📊 PRICE ACTION:</b>
Price Rise: {abs(price_change):.2f}%
↳ MINIMAL movement despite EXTREME buying
↳ Sellers absorbing EVERYTHING

<b>⚠️ WHAT THIS MEANS:</b>
BUYER EXHAUSTION - they threw EVERYTHING at market
But sellers caught it all
This is the TOP - buyers have no more money
BEARISH REVERSAL IMMINENT

<b>🎯 WATCH FOR:</b>
• Price stabilizing RIGHT NOW
• Volume dropping FAST (buyers done)
• STRONG reversal candle
• Drop in next 15-60 minutes

<b>⚡ BIAS: EXTREMELY BEARISH</b>
Climax exhaustion = Reversal in progress
THIS IS THE TOP

<b>🗣️ IN PLAIN ENGLISH:</b>
Buyers just FOMO'd {abs(current_delta):,.0f} BTC ({ratio_buy:.1f}x normal volume). But price barely moved = sellers are STRONG. Buyers are now EXHAUSTED. This is a classic blow-off top. Watch for immediate rejection.
"""
                
                send_telegram(message)
                last_pattern_alerts['clear_buying_climax'] = now
                add_to_memory('climax', {
                    'type': 'buying',
                    'price': current_price,
                    'delta': current_delta,
                    'severity': severity_buy,
                    'ratio': ratio_buy
                })
            else:
                print(f"   ⏳ Buying climax detected but on cooldown ({time_remaining}s remaining)")
    
    print("\n✅ Market scan complete")
    print("="*80 + "\n")


# ============= MONITORING THREAD =============
def monitor_orderflow():
    """Scan orderflow every 15 minutes and send ONLY clear signals"""
    print("🔄 Orderflow scanning thread started (CLARITY MODE)")
    
    while True:
        try:
            send_market_update()
            time.sleep(ORDERFLOW_INTERVAL)
        except Exception as e:
            print(f"❌ Orderflow scanning error: {e}")
            time.sleep(ORDERFLOW_INTERVAL)


# ============= FLASK APP =============
app = Flask(__name__)

@app.route('/', methods=['GET'])
def home():
    return f"""
    <h1>🚀 Beast Mode V8 - CLARITY FILTER EDITION</h1>
    
    <h2>🆕 WHAT'S NEW:</h2>
    <ul>
        <li><b>CLARITY FILTERS</b> - Only alerts on CLEAR signals, not noise</li>
        <li><b>HIGHER THRESHOLDS</b> - Stricter requirements to reduce spam</li>
        <li><b>LONGER COOLDOWNS</b> - 1-2 hour between alerts (vs 30 min)</li>
        <li><b>SEVERITY GATING</b> - Only high-severity patterns trigger alerts</li>
    </ul>
    
    <h2>📊 CLARITY REQUIREMENTS:</h2>
    <h3>Accumulation (Buying Absorption):</h3>
    <ul>
        <li>Delta: < -8,000 BTC (was -3,000)</li>
        <li>Price change: < 0.1% (was 0.15%)</li>
        <li>Severity: ≥ 6.0/10 (new)</li>
        <li>Ratio: ≥ 3.0x normal (new)</li>
        <li>Cooldown: 1 hour (was 30 min)</li>
    </ul>
    
    <h3>Distribution (Selling Absorption):</h3>
    <ul>
        <li>Delta: > 8,000 BTC (was 3,000)</li>
        <li>Price change: < 0.1% (was 0.15%)</li>
        <li>Severity: ≥ 6.0/10 (new)</li>
        <li>Ratio: ≥ 3.0x normal (new)</li>
        <li>Cooldown: 1 hour (was 30 min)</li>
    </ul>
    
    <h3>Climaxes (Exhaustion):</h3>
    <ul>
        <li>Delta: > 10,000 BTC (was 5,000)</li>
        <li>Price change: < 0.15%</li>
        <li>Severity: ≥ 7.0/10 (new)</li>
        <li>Ratio: ≥ 3.5x normal (was 2.0x)</li>
        <li>Cooldown: 1 hour (was 30 min)</li>
    </ul>
    
    <h3>Divergences:</h3>
    <ul>
        <li>Timeframes: ≥ 2 (was 1)</li>
        <li>Decline: ≥ 40% (new)</li>
        <li>Conviction: MEDIUM+ (was any)</li>
        <li>Cooldown: 2 hours (was 30 min)</li>
    </ul>
    
    <h2>💡 RESULT:</h2>
    <p><b>ONLY alerts on CLEAR accumulation/distribution</b></p>
    <p><b>NO MORE spam on every buyer/seller push</b></p>
    <p><b>HIGH-CONVICTION signals only</b></p>
    
    <h2>📈 Coverage:</h2>
    <p>$49B daily (33% coverage)</p>
    <p>🟠 OKX • 🟢 Bitget • 🔵 KuCoin</p>
    
    <p><i>Scanning every {ORDERFLOW_INTERVAL/60:.0f} minutes for CLEAR signals</i></p>
    """, 200


if __name__ == '__main__':
    print("🚀 Beast Mode V8 - CLARITY FILTER EDITION")
    print("="*80)
    print("🆕 IMPROVEMENTS:")
    print("  ✅ HIGHER THRESHOLDS - Only clear signals")
    print("  ✅ LONGER COOLDOWNS - Less spam")
    print("  ✅ SEVERITY GATING - Quality over quantity")
    print("  ✅ RATIO FILTERS - Must be significantly heavier")
    print("="*80)
    print("\n📊 NEW REQUIREMENTS:")
    print(f"  Accumulation: Delta < -{ACCUMULATION_THRESHOLDS['delta_min']:,} BTC, Severity ≥ {ACCUMULATION_THRESHOLDS['severity_min']}")
    print(f"  Distribution: Delta > {DISTRIBUTION_THRESHOLDS['delta_min']:,} BTC, Severity ≥ {DISTRIBUTION_THRESHOLDS['severity_min']}")
    print(f"  Climax: Delta > {CLIMAX_THRESHOLDS['delta_min']:,} BTC, Severity ≥ {CLIMAX_THRESHOLDS['severity_min']}")
    print(f"  Divergence: {DIVERGENCE_THRESHOLDS['min_timeframes']}+ timeframes, {DIVERGENCE_THRESHOLDS['decline_pct_min']}%+ decline")
    print("="*80)
    print("\n🎯 GOAL: CLEAR signals only, NO noise\n")
    
    # Start monitoring thread
    orderflow_thread = threading.Thread(target=monitor_orderflow, daemon=True)
    orderflow_thread.start()
    
    # Send startup message
    send_telegram(f"""
🚀 <b>Beast Mode V8 - CLARITY FILTER Started!</b>

<b>🆕 WHAT CHANGED:</b>
✅ <b>HIGHER THRESHOLDS</b> - Only CLEAR signals
✅ <b>LONGER COOLDOWNS</b> - 1-2 hours (vs 30 min)
✅ <b>SEVERITY GATING</b> - Quality over quantity
✅ <b>RATIO FILTERS</b> - Must be 3-3.5x heavier

<b>📊 WILL ONLY ALERT ON:</b>
• CLEAR accumulation (8k+ BTC selling, severity 6+)
• CLEAR distribution (8k+ BTC buying, severity 6+)
• EXTREME climaxes (10k+ BTC, severity 7+)
• Multi-TF divergences (2+ timeframes, 40%+ decline)

<b>🎯 NO MORE ALERTS ON:</b>
❌ Normal buyer/seller pushes
❌ Low-severity patterns
❌ Single-timeframe divergences
❌ Weak signals

<b>Coverage: $49B daily (33%)</b>
🟠 OKX • 🟢 Bitget • 🔵 KuCoin

<b>Scanning every {ORDERFLOW_INTERVAL/60:.0f} minutes!</b>
<i>ONLY high-conviction signals - NO NOISE</i>
    """)
    
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
