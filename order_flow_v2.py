"""
ORDER FLOW V2 - KEY LEVEL AUTOMATION
=====================================

Fully automated order flow system:
1. Receives TradingView MSB/OB/Breaker alerts
2. Calculates 9 institutional key levels
3. Monitors order flow at nearby levels
4. Sends continuation/reversal signals
5. Zero manual input required

Key Levels Monitored:
- Daily/Weekly/Monthly Opens
- Previous Day/Week/Month Highs/Lows
"""

import websocket
import json
import requests
import time
import os
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from threading import Thread
from flask import Flask, request, jsonify

# ===== CONFIGURATION =====
BOT_TOKEN = os.environ.get('BOT_TOKEN')
CHAT_ID = os.environ.get('CHAT_ID')
PORT = int(os.environ.get('PORT', 8080))
SYMBOL = "BTCUSDT"

# ===== STATE =====
footprint = defaultdict(lambda: {'bid': 0, 'ask': 0, 'trades': 0})
cumulative_delta = 0
trade_count = 0

# Active MSBs and their levels
active_msbs = {}
# Format: {'msb_74858': {'price': 74858, 'direction': 'bullish', 
#                         'key_levels': [...], 'monitoring': [...]}}

key_levels_cache = {}
last_alert_time = defaultdict(float)

def round_price(price, bucket=10):
    """Round price to nearest bucket"""
    return int(price / bucket) * bucket

def send_telegram(message):
    """Send message to Telegram"""
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        data = {"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}
        response = requests.post(url, data=data, timeout=5)
        print(f"📱 Telegram: {message[:80]}...")
        return response.json()
    except Exception as e:
        print(f"❌ Telegram error: {e}")
        return None

# ===== KEY LEVEL CALCULATION =====

def fetch_bybit_candles(interval, limit=100):
    """Fetch historical candles from Bybit"""
    try:
        url = "https://api.bybit.com/v5/market/kline"
        params = {
            "category": "linear",
            "symbol": SYMBOL,
            "interval": interval,
            "limit": limit
        }
        headers = {
            "Content-Type": "application/json"
        }
        response = requests.get(url, params=params, headers=headers, timeout=15)
        
        # Check if response is valid
        if response.status_code != 200:
            print(f"❌ Bybit API returned status {response.status_code}")
            return None
        
        # Try to parse JSON
        try:
            data = response.json()
        except ValueError as e:
            print(f"❌ Invalid JSON from Bybit: {e}")
            print(f"Response text: {response.text[:200]}")
            return None
        
        if data.get('retCode') == 0 and 'result' in data and 'list' in data['result']:
            candles = data['result']['list']
            # Bybit returns: [timestamp, open, high, low, close, volume, turnover]
            return candles
        else:
            print(f"❌ Bybit error: {data.get('retMsg', 'Unknown error')}")
            return None
            
    except requests.exceptions.Timeout:
        print(f"❌ Bybit API timeout")
        return None
    except Exception as e:
        print(f"❌ Error fetching candles: {e}")
        return None

def calculate_key_levels():
    """Calculate all institutional key levels"""
    global key_levels_cache
    
    levels = {}
    
    try:
        # Get daily candles (last 30 days)
        daily = fetch_bybit_candles("D", 30)
        if daily and len(daily) >= 2:
            # Current daily candle (index 0 = most recent)
            current_day = daily[0]
            prev_day = daily[1]
            
            levels['daily_open'] = float(current_day[1])
            levels['prev_day_high'] = float(prev_day[2])
            levels['prev_day_low'] = float(prev_day[3])
            print(f"   ✓ Daily levels calculated")
        else:
            print(f"   ⚠️ Could not fetch daily candles")
        
        # Get weekly candles (last 12 weeks)
        weekly = fetch_bybit_candles("W", 12)
        if weekly and len(weekly) >= 2:
            current_week = weekly[0]
            prev_week = weekly[1]
            
            levels['weekly_open'] = float(current_week[1])
            levels['prev_week_high'] = float(prev_week[2])
            levels['prev_week_low'] = float(prev_week[3])
            print(f"   ✓ Weekly levels calculated")
        else:
            print(f"   ⚠️ Could not fetch weekly candles")
        
        # Get monthly candles (last 6 months)
        monthly = fetch_bybit_candles("M", 6)
        if monthly and len(monthly) >= 2:
            current_month = monthly[0]
            prev_month = monthly[1]
            
            levels['monthly_open'] = float(current_month[1])
            levels['prev_month_high'] = float(prev_month[2])
            levels['prev_month_low'] = float(prev_month[3])
            print(f"   ✓ Monthly levels calculated")
        else:
            print(f"   ⚠️ Could not fetch monthly candles")
        
        # If we got at least some levels, cache them
        if levels:
            key_levels_cache = levels
            print(f"✅ Key levels calculated: {len(levels)} levels")
            for name, price in levels.items():
                print(f"   {name}: ${price:,.2f}")
        else:
            print(f"⚠️ No levels calculated - will retry in 1 hour")
            # Keep old cache if exists
        
        return levels
        
    except Exception as e:
        print(f"❌ Key level calculation error: {e}")
        import traceback
        traceback.print_exc()
        return {}

def get_nearby_levels(msb_price, max_distance=2000, max_levels=5):
    """Get key levels near MSB price"""
    if not key_levels_cache:
        calculate_key_levels()
    
    # Calculate distance from MSB to each level
    distances = []
    for name, price in key_levels_cache.items():
        dist = abs(price - msb_price)
        if dist <= max_distance:
            distances.append({
                'name': name,
                'price': price,
                'distance': dist,
                'above': price > msb_price
            })
    
    # Sort by distance, get closest
    distances.sort(key=lambda x: x['distance'])
    return distances[:max_levels]

# ===== ORDER FLOW MONITORING =====

def monitor_level(level_info, msb_id):
    """Check if price is at a key level and analyze order flow"""
    global last_alert_time
    
    level_price = level_info['price']
    level_name = level_info['name']
    
    # Get order flow near this level (+/- $50)
    range_low = round_price(level_price - 50)
    range_high = round_price(level_price + 50)
    
    range_data = {k: v for k, v in footprint.items() if range_low <= k <= range_high}
    
    if not range_data:
        return
    
    # Calculate metrics
    total_bid = sum(d['bid'] for d in range_data.values())
    total_ask = sum(d['ask'] for d in range_data.values())
    total_volume = total_bid + total_ask
    
    if total_volume < 50:  # Need minimum volume
        return
    
    delta = total_bid - total_ask
    bid_ratio = total_bid / total_volume if total_volume > 0 else 0.5
    
    # Alert key for cooldown
    alert_key = f"{msb_id}_{level_name}"
    cooldown = 600  # 10 minutes
    
    # ===== SIGNAL DETECTION =====
    
    # Strong buying at level
    if (bid_ratio > 0.65 and delta > 150 and 
        time.time() - last_alert_time[alert_key] > cooldown):
        
        msg = (
            f"🟢 <b>BUYERS AT KEY LEVEL</b>\n\n"
            f"Level: <b>{level_name.replace('_', ' ').title()}</b>\n"
            f"Price: ${level_price:,.2f}\n\n"
            f"📊 <b>Order Flow:</b>\n"
            f"Delta: +{delta:.0f} BTC\n"
            f"Bid: {total_bid:.0f} BTC ({bid_ratio*100:.0f}%)\n"
            f"Ask: {total_ask:.0f} BTC\n\n"
            f"✅ <b>LEVEL HOLDING</b>\n"
            f"Strong bid support"
        )
        send_telegram(msg)
        last_alert_time[alert_key] = time.time()
    
    # Strong selling at level
    elif (bid_ratio < 0.35 and delta < -150 and
          time.time() - last_alert_time[alert_key] > cooldown):
        
        msg = (
            f"🔴 <b>SELLERS AT KEY LEVEL</b>\n\n"
            f"Level: <b>{level_name.replace('_', ' ').title()}</b>\n"
            f"Price: ${level_price:,.2f}\n\n"
            f"📊 <b>Order Flow:</b>\n"
            f"Delta: {delta:.0f} BTC\n"
            f"Bid: {total_bid:.0f} BTC\n"
            f"Ask: {total_ask:.0f} BTC ({(1-bid_ratio)*100:.0f}%)\n\n"
            f"❌ <b>LEVEL BREAKING</b>\n"
            f"Strong ask pressure"
        )
        send_telegram(msg)
        last_alert_time[alert_key] = time.time()

def process_active_msbs(current_price):
    """Check all active MSBs and their key levels"""
    for msb_id, msb_data in list(active_msbs.items()):
        nearby_levels = msb_data.get('nearby_levels', [])
        
        for level_info in nearby_levels:
            level_price = level_info['price']
            
            # Check if price is near this level (+/- $100)
            if abs(current_price - level_price) <= 100:
                monitor_level(level_info, msb_id)

# ===== BYBIT WEBSOCKET =====

def on_message(ws, message):
    """Process Bybit trades"""
    global trade_count, cumulative_delta
    
    try:
        data = json.loads(message)
        
        if 'data' not in data:
            return
        
        for trade in data['data']:
            price = float(trade['p'])
            quantity = float(trade['v'])
            side = trade['S']
            
            price_level = round_price(price)
            
            # Update footprint
            if side == "Buy":
                footprint[price_level]['bid'] += quantity
                cumulative_delta += quantity
            else:
                footprint[price_level]['ask'] += quantity
                cumulative_delta -= quantity
            
            footprint[price_level]['trades'] += 1
            trade_count += 1
            
            # Check active MSBs every 50 trades
            if trade_count % 50 == 0:
                process_active_msbs(price)
            
            # Status update
            if trade_count % 1000 == 0:
                active_count = len(active_msbs)
                print(f"✓ {trade_count:,} trades | Delta: {cumulative_delta:+.0f} | Active MSBs: {active_count}")
    
    except Exception as e:
        print(f"❌ Trade processing error: {e}")

def on_error(ws, error):
    print(f"❌ WebSocket error: {error}")

def on_close(ws, close_status_code, close_msg):
    print("🔴 WebSocket closed - reconnecting in 5s...")
    time.sleep(5)
    start_websocket()

def on_open(ws):
    print("✅ Connected to Bybit")
    subscribe = {"op": "subscribe", "args": [f"publicTrade.{SYMBOL}"]}
    ws.send(json.dumps(subscribe))

def start_websocket():
    """Start Bybit WebSocket"""
    ws_url = "wss://stream.bybit.com/v5/public/linear"
    ws = websocket.WebSocketApp(
        ws_url,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
        on_open=on_open
    )
    ws.run_forever()

def run_websocket_thread():
    """Run WebSocket in thread"""
    while True:
        try:
            start_websocket()
        except Exception as e:
            print(f"❌ WebSocket thread error: {e}")
            time.sleep(5)

# ===== FLASK WEBHOOK RECEIVER =====

app = Flask(__name__)

@app.route('/webhook', methods=['POST'])
def webhook():
    """Receive TradingView alerts"""
    try:
        data = request.json
        alert_type = data.get('type')
        
        print(f"\n🔔 Webhook received: {alert_type}")
        print(f"Data: {data}")
        
        if alert_type == 'msb':
            # MSB detected
            price = float(data.get('price'))
            direction = data.get('direction')
            msb_type = data.get('msb_type', 'continuation')
            
            # Create MSB ID
            msb_id = f"msb_{int(price)}"
            
            # Calculate nearby key levels
            nearby = get_nearby_levels(price)
            
            # Store active MSB
            active_msbs[msb_id] = {
                'price': price,
                'direction': direction,
                'msb_type': msb_type,
                'nearby_levels': nearby,
                'timestamp': time.time()
            }
            
            # Send alert with key levels
            levels_text = "\n".join([
                f"• {lv['name'].replace('_', ' ').title()}: ${lv['price']:,.2f} ({'above' if lv['above'] else 'below'})"
                for lv in nearby[:3]
            ])
            
            msg = (
                f"🎯 <b>MSB DETECTED</b>\n\n"
                f"Type: {msb_type.upper()}\n"
                f"Direction: {direction.upper()}\n"
                f"Price: ${price:,.2f}\n\n"
                f"<b>Nearby Key Levels:</b>\n{levels_text}\n\n"
                f"<i>Monitoring order flow at levels...</i>"
            )
            send_telegram(msg)
            print(f"✅ MSB {direction} @ ${price:,.2f} - Monitoring {len(nearby)} levels")
        
        return jsonify({'status': 'success'}), 200
        
    except Exception as e:
        print(f"❌ Webhook error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 400

@app.route('/recalculate', methods=['GET'])
def recalculate():
    """Manually trigger key level recalculation"""
    print("🔄 Manual recalculation triggered")
    calculate_key_levels()
    return jsonify({
        'status': 'recalculated',
        'key_levels': len(key_levels_cache),
        'levels': key_levels_cache
    }), 200

@app.route('/health', methods=['GET'])
def health():
    """Health check"""
    return jsonify({
        'status': 'running',
        'active_msbs': len(active_msbs),
        'trades_processed': trade_count,
        'key_levels': len(key_levels_cache)
    }), 200

@app.route('/', methods=['GET'])
def home():
    """Status page"""
    levels_html = ""
    if key_levels_cache:
        for name, price in key_levels_cache.items():
            levels_html += f"<li><b>{name.replace('_', ' ').title()}</b>: ${price:,.2f}</li>"
    
    msbs_html = ""
    if active_msbs:
        for msb_id, data in active_msbs.items():
            msbs_html += f"<li><b>{data['direction'].upper()}</b> @ ${data['price']:,.2f} - {len(data['nearby_levels'])} levels</li>"
    else:
        msbs_html = "<li>No active MSBs</li>"
    
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Order Flow V2</title>
        <style>
            body {{ font-family: monospace; background: #0a0e1a; color: #e0e0e0; padding: 20px; }}
            h1 {{ color: #00ff88; }}
            .status {{ color: #00ff88; font-weight: bold; }}
            hr {{ border-color: #2c3e50; }}
        </style>
    </head>
    <body>
        <h1>⚡ Order Flow V2 - RUNNING</h1>
        <p class="status">Status: ACTIVE ✅</p>
        
        <hr>
        
        <h3>📊 Statistics</h3>
        <div>Trades Processed: <b>{trade_count:,}</b></div>
        <div>Active MSBs: <b>{len(active_msbs)}</b></div>
        <div>Key Levels Cached: <b>{len(key_levels_cache)}</b></div>
        <div>Cumulative Delta: <b>{cumulative_delta:+.0f} BTC</b></div>
        
        <hr>
        
        <h3>🎯 Active MSBs</h3>
        <ul>{msbs_html}</ul>
        
        <hr>
        
        <h3>🔑 Key Levels</h3>
        <ul>{levels_html}</ul>
        
        <hr>
        
        <p><i>Webhook: {request.url_root}webhook</i></p>
        <p><i>Last updated: {datetime.now().strftime('%H:%M:%S')}</i></p>
    </body>
    </html>
    """

# ===== STARTUP =====
print("\n" + "="*60)
print("🚀 ORDER FLOW V2 - KEY LEVEL AUTOMATION")
print("="*60)
print(f"Symbol: {SYMBOL}")
print(f"Port: {PORT}")
print("="*60 + "\n")

# Calculate key levels on startup with retries
print("📊 Calculating key levels...")
for attempt in range(3):
    if attempt > 0:
        print(f"   Retry attempt {attempt + 1}/3...")
        time.sleep(5)  # Wait 5 seconds between retries
    
    calculate_key_levels()
    
    if key_levels_cache:
        print(f"✅ Successfully loaded {len(key_levels_cache)} key levels")
        break
    else:
        print(f"⚠️ Attempt {attempt + 1} failed")

if not key_levels_cache:
    print("⚠️ WARNING: Could not load key levels on startup")
    print("   System will still work for MSB alerts")
    print("   Levels will auto-refresh every hour")

# Send startup message
send_telegram(
    "🟢 <b>Order Flow V2 Started</b>\n\n"
    f"Key Levels: {len(key_levels_cache)}\n"
    "Monitoring: Bybit order flow\n\n"
    "<i>Ready for TradingView alerts</i>"
)

# Start WebSocket thread
ws_thread = Thread(target=run_websocket_thread, daemon=True)
ws_thread.start()
print("✅ WebSocket thread started\n")

# Refresh key levels every hour
def refresh_levels():
    while True:
        time.sleep(3600)  # 1 hour
        print("🔄 Refreshing key levels...")
        calculate_key_levels()

refresh_thread = Thread(target=refresh_levels, daemon=True)
refresh_thread.start()

# Run Flask app
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=PORT, debug=False)
