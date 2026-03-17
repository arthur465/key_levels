"""
ORDER FLOW V2 - SIMPLIFIED (INDICATOR LEVELS ONLY)
===================================================

Monitors order flow at levels from your TradingView indicator:
- Market Structure Breaks (MSBs)
- Order Blocks (OBs)
- Breaker Blocks

No external API calls - just pure order flow monitoring
at the levels YOU actually trade!
"""

import websocket
import json
import requests
import time
import os
from collections import defaultdict
from datetime import datetime
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

# Active levels to monitor
# Format: {'msb_74858': {'type': 'msb', 'price': 74858, 'direction': 'bullish'}}
# Or: {'ob_bullish_1': {'type': 'ob', 'high': 75200, 'low': 74900, 'direction': 'bullish'}}
active_levels = {}
level_counter = {'msb': 0, 'ob': 0, 'breaker': 0}

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

# ===== ORDER FLOW MONITORING =====

def monitor_price_level(level_id, level_data, current_price):
    """Monitor order flow at a specific price level"""
    global last_alert_time
    
    level_type = level_data['type']
    
    # For MSB - single price point
    if level_type == 'msb':
        target_price = level_data['price']
        
        # Check if current price is near MSB (+/- $100)
        if abs(current_price - target_price) > 100:
            return
        
        # Get order flow at this level
        range_low = round_price(target_price - 50)
        range_high = round_price(target_price + 50)
        
    # For OB/Breaker - zone (high/low)
    else:
        zone_high = level_data['high']
        zone_low = level_data['low']
        
        # Check if current price is in zone
        if not (zone_low - 50 <= current_price <= zone_high + 50):
            return
        
        # Get order flow in zone
        range_low = round_price(zone_low)
        range_high = round_price(zone_high)
    
    # Extract footprint data for this range
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
    
    direction = level_data.get('direction', 'neutral')
    
    # Alert cooldown
    alert_key = f"{level_id}_confirmation"
    cooldown = 600  # 10 minutes
    
    # ===== SIGNAL DETECTION =====
    
    # BULLISH LEVEL - Buyers stepping in
    if (direction == 'bullish' and bid_ratio > 0.65 and delta > 150 and 
        time.time() - last_alert_time[alert_key] > cooldown):
        
        level_name = get_level_name(level_type, level_data)
        
        msg = (
            f"🟢 <b>BUYERS STEPPING IN</b>\n\n"
            f"Level: <b>{level_name}</b>\n"
            f"Price: ${current_price:,.0f}\n\n"
            f"📊 <b>Order Flow:</b>\n"
            f"Delta: +{delta:.0f} BTC\n"
            f"Bid: {total_bid:.0f} BTC ({bid_ratio*100:.0f}%)\n"
            f"Ask: {total_ask:.0f} BTC\n\n"
            f"✅ <b>STRONG DEMAND</b>\n"
            f"Bullish level holding"
        )
        send_telegram(msg)
        last_alert_time[alert_key] = time.time()
    
    # BEARISH LEVEL - Sellers stepping in
    elif (direction == 'bearish' and bid_ratio < 0.35 and delta < -150 and
          time.time() - last_alert_time[alert_key] > cooldown):
        
        level_name = get_level_name(level_type, level_data)
        
        msg = (
            f"🔴 <b>SELLERS STEPPING IN</b>\n\n"
            f"Level: <b>{level_name}</b>\n"
            f"Price: ${current_price:,.0f}\n\n"
            f"📊 <b>Order Flow:</b>\n"
            f"Delta: {delta:.0f} BTC\n"
            f"Bid: {total_bid:.0f} BTC\n"
            f"Ask: {total_ask:.0f} BTC ({(1-bid_ratio)*100:.0f}%)\n\n"
            f"✅ <b>STRONG SUPPLY</b>\n"
            f"Bearish level holding"
        )
        send_telegram(msg)
        last_alert_time[alert_key] = time.time()
    
    # LEVEL REJECTION (opposite of expected direction)
    elif (direction == 'bullish' and bid_ratio < 0.35 and delta < -200 and
          time.time() - last_alert_time[f"{alert_key}_reject"] > cooldown):
        
        level_name = get_level_name(level_type, level_data)
        
        msg = (
            f"⚠️ <b>LEVEL REJECTION</b>\n\n"
            f"Level: <b>{level_name}</b>\n"
            f"Expected: Bullish support\n"
            f"Actual: Sellers overwhelming\n\n"
            f"📊 Delta: {delta:.0f} BTC\n"
            f"Ask: {(1-bid_ratio)*100:.0f}%\n\n"
            f"❌ <b>AVOID LONGS</b>\n"
            f"Level failing"
        )
        send_telegram(msg)
        last_alert_time[f"{alert_key}_reject"] = time.time()

def get_level_name(level_type, level_data):
    """Format level name for display"""
    if level_type == 'msb':
        msb_type = level_data.get('msb_type', 'continuation')
        direction = level_data['direction']
        price = level_data['price']
        return f"MSB {direction.title()} @ ${price:,.0f}"
    
    elif level_type == 'order_block':
        direction = level_data['direction']
        return f"Order Block ({direction.title()})"
    
    elif level_type == 'breaker_block':
        direction = level_data['direction']
        return f"Breaker Block ({direction.title()})"
    
    return "Unknown Level"

def process_active_levels(current_price):
    """Check all active levels"""
    for level_id, level_data in list(active_levels.items()):
        monitor_price_level(level_id, level_data, current_price)

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
            
            # Check active levels every 50 trades
            if active_levels and trade_count % 50 == 0:
                process_active_levels(price)
            
            # Status update
            if trade_count % 1000 == 0:
                active_count = len(active_levels)
                print(f"✓ {trade_count:,} trades | Delta: {cumulative_delta:+.0f} | Active levels: {active_count}")
    
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
            timeframe = data.get('timeframe', '1H')
            
            # Create level ID
            level_counter['msb'] += 1
            level_id = f"msb_{level_counter['msb']}"
            
            # Store active level
            active_levels[level_id] = {
                'type': 'msb',
                'price': price,
                'direction': direction,
                'msb_type': msb_type,
                'timeframe': timeframe,
                'timestamp': time.time()
            }
            
            msg = (
                f"🎯 <b>MSB DETECTED</b>\n\n"
                f"Type: {msb_type.upper()}\n"
                f"Direction: {direction.upper()}\n"
                f"Price: ${price:,.0f}\n"
                f"Timeframe: {timeframe}\n\n"
                f"<i>Monitoring order flow at level...</i>"
            )
            send_telegram(msg)
            print(f"✅ MSB {direction} @ ${price:,.0f} - Monitoring started")
        
        elif alert_type == 'order_block':
            # Order Block detected
            high = float(data.get('high'))
            low = float(data.get('low'))
            direction = data.get('direction')
            timeframe = data.get('timeframe', '1H')
            
            level_counter['ob'] += 1
            level_id = f"ob_{level_counter['ob']}"
            
            active_levels[level_id] = {
                'type': 'order_block',
                'high': high,
                'low': low,
                'direction': direction,
                'timeframe': timeframe,
                'timestamp': time.time()
            }
            
            msg = (
                f"📦 <b>ORDER BLOCK FORMED</b>\n\n"
                f"Direction: {direction.upper()}\n"
                f"Zone: ${low:,.0f} - ${high:,.0f}\n"
                f"Width: ${high-low:,.0f}\n"
                f"Timeframe: {timeframe}\n\n"
                f"<i>Monitoring order flow in zone...</i>"
            )
            send_telegram(msg)
            print(f"✅ OB {direction} @ ${low:,.0f}-${high:,.0f}")
        
        elif alert_type == 'breaker_block':
            # Breaker Block detected
            high = float(data.get('high'))
            low = float(data.get('low'))
            direction = data.get('direction')
            timeframe = data.get('timeframe', '1H')
            
            level_counter['breaker'] += 1
            level_id = f"breaker_{level_counter['breaker']}"
            
            active_levels[level_id] = {
                'type': 'breaker_block',
                'high': high,
                'low': low,
                'direction': direction,
                'timeframe': timeframe,
                'timestamp': time.time()
            }
            
            msg = (
                f"⚡ <b>BREAKER BLOCK FORMED</b>\n\n"
                f"Direction: {direction.upper()}\n"
                f"Zone: ${low:,.0f} - ${high:,.0f}\n"
                f"Width: ${high-low:,.0f}\n"
                f"Timeframe: {timeframe}\n\n"
                f"<i>Monitoring order flow in zone...</i>"
            )
            send_telegram(msg)
            print(f"✅ Breaker {direction} @ ${low:,.0f}-${high:,.0f}")
        
        return jsonify({'status': 'success', 'active_levels': len(active_levels)}), 200
        
    except Exception as e:
        print(f"❌ Webhook error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 400

@app.route('/health', methods=['GET'])
def health():
    """Health check"""
    return jsonify({
        'status': 'running',
        'active_levels': len(active_levels),
        'trades_processed': trade_count,
        'cumulative_delta': cumulative_delta
    }), 200

@app.route('/', methods=['GET'])
def home():
    """Status page"""
    levels_html = ""
    if active_levels:
        for level_id, data in active_levels.items():
            level_name = get_level_name(data['type'], data)
            levels_html += f"<li><b>{level_name}</b> ({data['timeframe']})</li>"
    else:
        levels_html = "<li>No active levels - waiting for TradingView alerts</li>"
    
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Order Flow V2 - Simplified</title>
        <style>
            body {{ font-family: monospace; background: #0a0e1a; color: #e0e0e0; padding: 20px; }}
            h1 {{ color: #00ff88; }}
            .status {{ color: #00ff88; font-weight: bold; }}
            hr {{ border-color: #2c3e50; }}
        </style>
    </head>
    <body>
        <h1>⚡ Order Flow V2 - Simplified</h1>
        <p class="status">Status: ACTIVE ✅</p>
        
        <hr>
        
        <h3>📊 Statistics</h3>
        <div>Trades Processed: <b>{trade_count:,}</b></div>
        <div>Active Levels: <b>{len(active_levels)}</b></div>
        <div>Cumulative Delta: <b>{cumulative_delta:+.0f} BTC</b></div>
        
        <hr>
        
        <h3>🎯 Monitoring:</h3>
        <ul>{levels_html}</ul>
        
        <hr>
        
        <p><i>Webhook: {request.url_root}webhook</i></p>
        <p><i>Last updated: {datetime.now().strftime('%H:%M:%S')}</i></p>
    </body>
    </html>
    """

# ===== STARTUP =====
print("\n" + "="*60)
print("🚀 ORDER FLOW V2 - SIMPLIFIED")
print("="*60)
print(f"Symbol: {SYMBOL}")
print(f"Port: {PORT}")
print(f"Monitoring: MSBs, Order Blocks, Breakers")
print("="*60 + "\n")

# Send startup message
send_telegram(
    "🟢 <b>Order Flow V2 Started</b>\n\n"
    "Mode: Simplified (Indicator Levels)\n"
    "Monitoring: Bybit order flow\n\n"
    "<b>Tracking:</b>\n"
    "• Market Structure Breaks\n"
    "• Order Blocks\n"
    "• Breaker Blocks\n\n"
    "<i>Ready for TradingView alerts</i>"
)

# Start WebSocket thread
ws_thread = Thread(target=run_websocket_thread, daemon=True)
ws_thread.start()
print("✅ WebSocket thread started\n")
print("✅ Waiting for TradingView alerts...\n")

# Run Flask app
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=PORT, debug=False)
