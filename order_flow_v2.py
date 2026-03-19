"""
═══════════════════════════════════════════════════════════════════════════
BEAST MODE v3.1 - PURE TPO + ORDER FLOW
═══════════════════════════════════════════════════════════════════════════

TPO Signals Only:
- Naked POC (Daily/Weekly/Monthly)
- Single Prints  
- Poor Highs/Lows
- Value Area Re-Entries (80% Rule)

Order Flow Analysis:
- Dual exchange (Bybit + Binance)
- 10-minute monitoring
- Simple alerts (entry + final result)

═══════════════════════════════════════════════════════════════════════════
"""

import os
import json
import time
import threading
import requests
from collections import defaultdict
from datetime import datetime
from flask import Flask, request, jsonify
import websocket

# ===== CONFIGURATION =====
SYMBOL = "BTCUSDT"
PORT = int(os.environ.get("PORT", 10000))
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# ===== GLOBAL STATE =====
app = Flask(__name__)

# Order flow data
footprint_bybit = defaultdict(lambda: {'bid': 0, 'ask': 0, 'trades': 0})
footprint_binance = defaultdict(lambda: {'bid': 0, 'ask': 0, 'trades': 0})
cumulative_delta_bybit = 0.0
cumulative_delta_binance = 0.0
trade_count_bybit = 0
trade_count_binance = 0

# Active TPO levels being monitored
active_levels = {}
level_counter = {'tpo': 0}

# Monitoring threads
monitoring_threads = {}

# ===== HELPERS =====

def round_price(price):
    """Round to nearest $10"""
    return round(price / 10) * 10

def send_telegram(message):
    """Send Telegram message"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"📱 Would send: {message}")
        return
    
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        requests.post(url, json=data, timeout=5)
    except Exception as e:
        print(f"❌ Telegram error: {e}")

def get_level_name(level_type, data):
    """Get display name for level"""
    if level_type == 'tpo_naked_poc':
        timeframe = data.get('timeframe', 'daily')
        return f"{timeframe.upper()} NAKED POC"
    elif level_type == 'tpo_single_print':
        sp_type = data.get('sp_type', 'daily')
        return f"{sp_type.upper()} SINGLE PRINT"
    elif level_type == 'tpo_poor_hl':
        level_type_name = data.get('level_type', 'poor_hl')
        return "POOR HIGH" if level_type_name == 'poor_high' else "POOR LOW"
    elif level_type == 'value_area_reentry':
        direction = data.get('direction', 'unknown')
        return f"VA RE-ENTRY ({direction.upper()})"
    return level_type.upper()

# ===== 10-MINUTE MONITORING =====

def start_tpo_monitoring(level_id, level_type, level_data):
    """Start 10-minute order flow monitoring for TPO level"""
    
    def monitor():
        print(f"⏱️  Starting 10-min monitoring for {level_id}")
        
        # Take snapshots every 60 seconds for 10 minutes
        snapshots = []
        start_time = time.time()
        
        for minute in range(10):
            if level_id not in active_levels:
                print(f"⚠️  {level_id} removed during monitoring")
                return
            
            # Capture current state
            snapshot = {
                'minute': minute + 1,
                'delta_bybit': cumulative_delta_bybit,
                'delta_binance': cumulative_delta_binance,
                'delta_combined': cumulative_delta_bybit + cumulative_delta_binance,
                'trades_bybit': trade_count_bybit,
                'trades_binance': trade_count_binance,
                'timestamp': time.time()
            }
            snapshots.append(snapshot)
            
            # Wait 60 seconds (or less if monitoring should stop)
            for _ in range(60):
                if level_id not in active_levels:
                    return
                time.sleep(1)
        
        # Analysis complete
        if level_id not in active_levels:
            return
        
        # Calculate metrics
        total_delta = cumulative_delta_bybit + cumulative_delta_binance
        
        # Classify strength
        if abs(total_delta) > 1000:
            trade_class = "SWING"
            duration = "1-3 days"
        elif abs(total_delta) > 600:
            trade_class = "DAY TRADE"
            duration = "4-8 hours"
        else:
            trade_class = "SCALP"
            duration = "30min-2hr"
        
        # Determine bias
        if total_delta > 500:
            bias = "BULLISH"
            bias_emoji = "🟢"
        elif total_delta < -500:
            bias = "BEARISH"
            bias_emoji = "🔴"
        else:
            bias = "NEUTRAL"
            bias_emoji = "⚪"
        
        # Get level details
        level_name = get_level_name(level_type, level_data)
        
        # Build signal message
        if level_type == 'value_area_reentry':
            # VA re-entry specific message
            vah = level_data['vah']
            val = level_data['val']
            direction = level_data['direction']
            
            if direction == 'from_above':
                # Failed breakout above VAH - rotation to VAL
                entry = vah
                target = val
                stop = vah + 100
                setup_type = "SHORT"
                
                msg = (
                    f"{bias_emoji} <b>80% RULE - ROTATION TO VAL</b>\n\n"
                    f"Setup: Re-entered from above VAH\n"
                    f"Previous Day VA: ${val:,.0f} - ${vah:,.0f}\n\n"
                    f"═══════════════════════════\n"
                    f"<b>TRADE SETUP</b>\n"
                    f"═══════════════════════════\n"
                    f"Entry: ~${entry:,.0f}\n"
                    f"Stop: ${stop:,.0f} (${abs(entry-stop):.0f} risk)\n"
                    f"Target: ${target:,.0f} (${abs(target-entry):.0f} profit)\n"
                    f"R/R: 1:{abs(target-entry)/abs(entry-stop):.1f}\n\n"
                    f"═══════════════════════════\n"
                    f"<b>ORDER FLOW ANALYSIS</b>\n"
                    f"═══════════════════════════\n"
                    f"Combined Delta: {total_delta:+.0f} BTC\n"
                    f"  • Bybit: {cumulative_delta_bybit:+.0f} BTC\n"
                    f"  • Binance: {cumulative_delta_binance:+.0f} BTC\n\n"
                    f"Bias: {bias}\n"
                    f"Classification: {trade_class}\n"
                    f"Duration: {duration}\n\n"
                    f"<b>📉 {setup_type} RECOMMENDED</b>\n\n"
                    f"<i>Set your entry/stop/target and let it work.</i>"
                )
            else:
                # Failed breakdown below VAL - rotation to VAH
                entry = val
                target = vah
                stop = val - 100
                setup_type = "LONG"
                
                msg = (
                    f"{bias_emoji} <b>80% RULE - ROTATION TO VAH</b>\n\n"
                    f"Setup: Re-entered from below VAL\n"
                    f"Previous Day VA: ${val:,.0f} - ${vah:,.0f}\n\n"
                    f"═══════════════════════════\n"
                    f"<b>TRADE SETUP</b>\n"
                    f"═══════════════════════════\n"
                    f"Entry: ~${entry:,.0f}\n"
                    f"Stop: ${stop:,.0f} (${abs(entry-stop):.0f} risk)\n"
                    f"Target: ${target:,.0f} (${abs(target-entry):.0f} profit)\n"
                    f"R/R: 1:{abs(target-entry)/abs(entry-stop):.1f}\n\n"
                    f"═══════════════════════════\n"
                    f"<b>ORDER FLOW ANALYSIS</b>\n"
                    f"═══════════════════════════\n"
                    f"Combined Delta: {total_delta:+.0f} BTC\n"
                    f"  • Bybit: {cumulative_delta_bybit:+.0f} BTC\n"
                    f"  • Binance: {cumulative_delta_binance:+.0f} BTC\n\n"
                    f"Bias: {bias}\n"
                    f"Classification: {trade_class}\n"
                    f"Duration: {duration}\n\n"
                    f"<b>📈 {setup_type} RECOMMENDED</b>\n\n"
                    f"<i>Set your entry/stop/target and let it work.</i>"
                )
        
        else:
            # Other TPO levels (POC, Single Print, Poor H/L)
            level_price = level_data.get('level_price') or level_data.get('poc_price') or level_data.get('sp_top')
            
            if bias == "BULLISH":
                setup_type = "LONG"
                entry = level_price
                stop = level_price - 100
                target = level_price + 400
            elif bias == "BEARISH":
                setup_type = "SHORT"
                entry = level_price
                stop = level_price + 100
                target = level_price - 400
            else:
                setup_type = "SKIP"
                entry = level_price
                stop = 0
                target = 0
            
            if setup_type != "SKIP":
                msg = (
                    f"{bias_emoji} <b>{level_name} - {bias}</b>\n\n"
                    f"Level: ${level_price:,.0f}\n\n"
                    f"═══════════════════════════\n"
                    f"<b>TRADE SETUP</b>\n"
                    f"═══════════════════════════\n"
                    f"Entry: ${entry:,.0f}\n"
                    f"Stop: ${stop:,.0f} (${abs(entry-stop):.0f} risk)\n"
                    f"Target: ${target:,.0f} (${abs(target-entry):.0f} profit)\n"
                    f"R/R: 1:{abs(target-entry)/abs(entry-stop):.1f}\n\n"
                    f"═══════════════════════════\n"
                    f"<b>ORDER FLOW ANALYSIS</b>\n"
                    f"═══════════════════════════\n"
                    f"Combined Delta: {total_delta:+.0f} BTC\n"
                    f"  • Bybit: {cumulative_delta_bybit:+.0f} BTC\n"
                    f"  • Binance: {cumulative_delta_binance:+.0f} BTC\n\n"
                    f"Bias: {bias}\n"
                    f"Classification: {trade_class}\n"
                    f"Duration: {duration}\n\n"
                    f"<b>📈 {setup_type} RECOMMENDED</b>\n\n"
                    f"<i>Set your entry/stop/target and let it work.</i>"
                )
            else:
                msg = (
                    f"{bias_emoji} <b>{level_name} - SKIP</b>\n\n"
                    f"Level: ${level_price:,.0f}\n\n"
                    f"Combined Delta: {total_delta:+.0f} BTC\n"
                    f"Bias: {bias}\n\n"
                    f"<i>Neutral order flow - no clear trade setup.</i>"
                )
        
        send_telegram(msg)
        
        # Clean up
        if level_id in active_levels:
            del active_levels[level_id]
        if level_id in monitoring_threads:
            del monitoring_threads[level_id]
        
        print(f"✅ Monitoring complete: {level_id}")
    
    # Start monitoring thread
    thread = threading.Thread(target=monitor, daemon=True)
    monitoring_threads[level_id] = thread
    thread.start()

# ===== WEBHOOK ENDPOINT =====

@app.route('/webhook', methods=['POST'])
def webhook():
    """Receive TPO alerts from TradingView"""
    try:
        data = request.get_json()
        alert_type = data.get('type')
        
        if alert_type == 'tpo_naked_poc':
            # Naked POC approached
            poc_price = float(data.get('poc_price'))
            poc_type = data.get('poc_type', 'naked')
            timeframe = data.get('timeframe', 'daily')
            current_price = float(data.get('current_price'))
            
            level_counter['tpo'] += 1
            level_id = f"tpo_poc_{level_counter['tpo']}"
            
            active_levels[level_id] = {
                'type': 'tpo_naked_poc',
                'poc_price': poc_price,
                'poc_type': poc_type,
                'timeframe': timeframe,
                'timestamp': time.time()
            }
            
            # Send initial alert
            tf_emoji = '📅' if timeframe == 'daily' else '📆' if timeframe == 'weekly' else '📊'
            msg = (
                f"{tf_emoji} <b>{timeframe.upper()} NAKED POC APPROACHED</b>\n\n"
                f"POC: ${poc_price:,.0f}\n"
                f"Current: ${current_price:,.0f}\n\n"
                f"<i>Analyzing order flow for 10 minutes...</i>"
            )
            send_telegram(msg)
            
            # Start monitoring
            start_tpo_monitoring(level_id, 'tpo_naked_poc', active_levels[level_id])
            
            print(f"✅ TPO Naked POC: {timeframe} @ ${poc_price:,.0f}")
        
        elif alert_type == 'tpo_single_print':
            # Single print zone touched
            sp_top = float(data.get('sp_top'))
            sp_bottom = float(data.get('sp_bottom'))
            sp_type = data.get('sp_type', 'daily')
            current_price = float(data.get('current_price'))
            
            level_counter['tpo'] += 1
            level_id = f"tpo_sp_{level_counter['tpo']}"
            
            active_levels[level_id] = {
                'type': 'tpo_single_print',
                'sp_top': sp_top,
                'sp_bottom': sp_bottom,
                'sp_type': sp_type,
                'timestamp': time.time()
            }
            
            # Send initial alert
            zone_size = sp_top - sp_bottom
            msg = (
                f"💜 <b>{sp_type.upper()} SINGLE PRINT TOUCHED</b>\n\n"
                f"Zone: ${sp_bottom:,.0f} - ${sp_top:,.0f}\n"
                f"Size: ${zone_size:,.0f}\n"
                f"Current: ${current_price:,.0f}\n\n"
                f"<i>Analyzing gap fill order flow...</i>"
            )
            send_telegram(msg)
            
            # Start monitoring
            start_tpo_monitoring(level_id, 'tpo_single_print', active_levels[level_id])
            
            print(f"✅ TPO Single Print: {sp_type} ${sp_bottom:,.0f}-${sp_top:,.0f}")
        
        elif alert_type == 'tpo_poor_hl':
            # Poor high/low touched
            level_price = float(data.get('level_price'))
            level_type = data.get('level_type')  # "poor_high" or "poor_low"
            current_price = float(data.get('current_price'))
            
            level_counter['tpo'] += 1
            level_id = f"tpo_poorhl_{level_counter['tpo']}"
            
            active_levels[level_id] = {
                'type': 'tpo_poor_hl',
                'level_price': level_price,
                'level_type': level_type,
                'timestamp': time.time()
            }
            
            # Send initial alert
            label = "POOR HIGH" if level_type == "poor_high" else "POOR LOW"
            emoji = "🔴" if level_type == "poor_high" else "🟢"
            msg = (
                f"{emoji} <b>{label} RETESTED</b>\n\n"
                f"Level: ${level_price:,.0f}\n"
                f"Current: ${current_price:,.0f}\n\n"
                f"<i>Weak extreme retested - analyzing defense...</i>"
            )
            send_telegram(msg)
            
            # Start monitoring
            start_tpo_monitoring(level_id, 'tpo_poor_hl', active_levels[level_id])
            
            print(f"✅ TPO {label}: ${level_price:,.0f}")
        
        elif alert_type == 'value_area_reentry':
            # VA re-entry (80% rule)
            direction = data.get('direction')  # "from_above" or "from_below"
            vah = float(data.get('vah'))
            val = float(data.get('val'))
            current_price = float(data.get('current_price'))
            
            level_counter['tpo'] += 1
            level_id = f"tpo_va_{level_counter['tpo']}"
            
            active_levels[level_id] = {
                'type': 'value_area_reentry',
                'direction': direction,
                'vah': vah,
                'val': val,
                'timestamp': time.time()
            }
            
            # Send initial alert
            if direction == 'from_above':
                emoji = "🔴"
                title = "VA RE-ENTRY - ROTATION TO VAL"
                desc = f"Re-entered from above VAH\nExpected rotation: DOWN to VAL"
            else:
                emoji = "🟢"
                title = "VA RE-ENTRY - ROTATION TO VAH"
                desc = f"Re-entered from below VAL\nExpected rotation: UP to VAH"
            
            msg = (
                f"{emoji} <b>{title}</b>\n\n"
                f"{desc}\n\n"
                f"Previous Day VA: ${val:,.0f} - ${vah:,.0f}\n"
                f"Current: ${current_price:,.0f}\n\n"
                f"<i>Analyzing order flow for rotation confirmation...</i>"
            )
            send_telegram(msg)
            
            # Start monitoring
            start_tpo_monitoring(level_id, 'value_area_reentry', active_levels[level_id])
            
            print(f"✅ VA Re-entry: {direction} @ ${current_price:,.0f}")
        
        return jsonify({'status': 'success', 'active_levels': len(active_levels)}), 200
        
    except Exception as e:
        print(f"❌ Webhook error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 400

# ===== HEALTH CHECK =====

@app.route('/health')
def health():
    """Health check endpoint"""
    return jsonify({
        'status': 'running',
        'active_levels': len(active_levels),
        'bybit': {
            'cumulative_delta': round(cumulative_delta_bybit, 2),
            'trades_processed': trade_count_bybit
        },
        'binance': {
            'cumulative_delta': round(cumulative_delta_binance, 2),
            'trades_processed': trade_count_binance
        }
    })

@app.route('/')
def home():
    """Status page"""
    levels_html = ""
    if active_levels:
        for level_id, data in active_levels.items():
            level_name = get_level_name(data['type'], data)
            timeframe_display = f" ({data['timeframe']})" if 'timeframe' in data else ""
            levels_html += f"<li><b>{level_name}</b>{timeframe_display}</li>"
    else:
        levels_html = "<li>No active levels - waiting for TPO alerts</li>"
    
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Beast Mode v3.1 - Pure TPO</title>
        <style>
            body {{ font-family: monospace; background: #0a0e1a; color: #e0e0e0; padding: 20px; }}
            h1 {{ color: #00ff88; }}
            .status {{ color: #00ff88; font-weight: bold; }}
            hr {{ border-color: #2c3e50; }}
            .metric {{ margin: 5px 0; }}
        </style>
    </head>
    <body>
        <h1>⚡ Beast Mode v3.1 - Pure TPO</h1>
        <p class="status">Status: ACTIVE ✅</p>
        
        <hr>
        
        <h3>📊 Order Flow Data</h3>
        <div class="metric"><b>Bybit:</b> {cumulative_delta_bybit:+.0f} BTC ({trade_count_bybit:,} trades)</div>
        <div class="metric"><b>Binance:</b> {cumulative_delta_binance:+.0f} BTC ({trade_count_binance:,} trades)</div>
        <div class="metric"><b>Combined:</b> {cumulative_delta_bybit + cumulative_delta_binance:+.0f} BTC</div>
        
        <h3>🎯 Active Levels</h3>
        <div class="metric">{len(active_levels)} levels monitoring</div>
        <ul>{levels_html}</ul>
        
        <hr>
        
        <p><i>Webhook: {request.url_root}webhook</i></p>
        <p><i>Last updated: {datetime.now().strftime('%H:%M:%S')}</i></p>
    </body>
    </html>
    """

# ===== BYBIT WEBSOCKET =====

def on_message_bybit(ws, message):
    """Process Bybit trades"""
    global trade_count_bybit, cumulative_delta_bybit
    
    try:
        data = json.loads(message)
        
        if 'data' not in data:
            return
        
        for trade in data['data']:
            price = float(trade['p'])
            quantity = float(trade['v'])
            side = trade['S']
            
            price_level = round_price(price)
            
            if side == "Buy":
                footprint_bybit[price_level]['bid'] += quantity
                cumulative_delta_bybit += quantity
            else:
                footprint_bybit[price_level]['ask'] += quantity
                cumulative_delta_bybit -= quantity
            
            footprint_bybit[price_level]['trades'] += 1
            trade_count_bybit += 1
            
            if trade_count_bybit % 1000 == 0:
                print(f"✓ Bybit: {trade_count_bybit:,} trades | Delta: {cumulative_delta_bybit:+.0f}")
    
    except Exception as e:
        print(f"❌ Bybit processing error: {e}")

def on_message_binance(ws, message):
    """Process Binance trades"""
    global trade_count_binance, cumulative_delta_binance
    
    try:
        data = json.loads(message)
        
        if data.get('e') != 'aggTrade':
            return
        
        price = float(data['p'])
        quantity = float(data['q'])
        is_buyer_maker = data['m']
        
        price_level = round_price(price)
        
        if not is_buyer_maker:  # Buy
            footprint_binance[price_level]['bid'] += quantity
            cumulative_delta_binance += quantity
        else:  # Sell
            footprint_binance[price_level]['ask'] += quantity
            cumulative_delta_binance -= quantity
        
        footprint_binance[price_level]['trades'] += 1
        trade_count_binance += 1
        
        if trade_count_binance % 1000 == 0:
            print(f"✓ Binance: {trade_count_binance:,} trades | Delta: {cumulative_delta_binance:+.0f}")
    
    except Exception as e:
        print(f"❌ Binance processing error: {e}")

def on_error_bybit(ws, error):
    print(f"❌ Bybit WebSocket error: {error}")

def on_error_binance(ws, error):
    print(f"❌ Binance WebSocket error: {error}")

def on_close_bybit(ws, close_status_code, close_msg):
    print("⚠️  Bybit WebSocket closed - reconnecting...")
    time.sleep(5)
    start_bybit_websocket()

def on_close_binance(ws, close_status_code, close_msg):
    print("⚠️  Binance WebSocket closed - reconnecting...")
    time.sleep(5)
    start_binance_websocket()

def on_open_bybit(ws):
    print("✅ Bybit WebSocket connected")
    subscribe_msg = {
        "op": "subscribe",
        "args": [f"publicTrade.{SYMBOL}"]
    }
    ws.send(json.dumps(subscribe_msg))

def on_open_binance(ws):
    print("✅ Binance WebSocket connected")

def start_bybit_websocket():
    """Start Bybit WebSocket connection"""
    ws_url = "wss://stream.bybit.com/v5/public/linear"
    ws = websocket.WebSocketApp(
        ws_url,
        on_message=on_message_bybit,
        on_error=on_error_bybit,
        on_close=on_close_bybit,
        on_open=on_open_bybit
    )
    threading.Thread(target=ws.run_forever, daemon=True).start()

def start_binance_websocket():
    """Start Binance WebSocket connection"""
    ws_url = f"wss://fstream.binance.com/ws/{SYMBOL.lower()}@aggTrade"
    ws = websocket.WebSocketApp(
        ws_url,
        on_message=on_message_binance,
        on_error=on_error_binance,
        on_close=on_close_binance,
        on_open=on_open_binance
    )
    threading.Thread(target=ws.run_forever, daemon=True).start()

# ===== STARTUP =====
print("\n" + "="*70)
print("🚀 BEAST MODE v3.1 - PURE TPO + ORDER FLOW")
print("="*70)
print(f"Symbol: {SYMBOL}")
print(f"Port: {PORT}")
print(f"\n📊 TPO SIGNALS:")
print(f"  • Naked POCs (Daily/Weekly/Monthly)")
print(f"  • Single Prints")
print(f"  • Poor Highs/Lows")
print(f"  • Value Area Re-Entries (80% Rule)")
print(f"\n📈 ORDER FLOW:")
print(f"  • Dual exchange (Bybit + Binance)")
print(f"  • 10-minute monitoring")
print(f"  • Simple alerts (entry + result)")
print("="*70 + "\n")

# Start WebSocket connections
start_bybit_websocket()
start_binance_websocket()

# Send startup message
send_telegram(
    "🟢 <b>BEAST MODE v3.1 - PURE TPO</b>\n\n"
    "System started!\n\n"
    "Monitoring TPO levels:\n"
    "• Naked POCs\n"
    "• Single Prints\n"
    "• Poor Highs/Lows\n"
    "• VA Re-Entries (80% Rule)\n\n"
    "<i>Waiting for TPO alerts from TradingView...</i>"
)

# Run Flask app
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=PORT)
