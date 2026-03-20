import os
import time
import requests
from datetime import datetime, timezone
from flask import Flask, request, jsonify
import threading
import json

# ============= CONFIGURATION =============
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
SYMBOL = "BTCUSDT"
MONITORING_INTERVAL = 900  # 15 minutes

# ============= TRADE TRACKING =============
active_trades = {}
trade_lock = threading.Lock()

# ============= FLASK APP =============
app = Flask(__name__)

# ============= BYBIT API =============
def get_orderbook_delta():
    url = "https://api.bybit.com/v5/market/orderbook"
    params = {"category": "linear", "symbol": SYMBOL, "limit": 200}
    try:
        response = requests.get(url, params=params, timeout=10)
        data = response.json()
        if data.get("retCode") == 0:
            bids = data["result"]["b"]
            asks = data["result"]["a"]
            bid_volume = sum(float(b[1]) for b in bids)
            ask_volume = sum(float(a[1]) for a in asks)
            delta = bid_volume - ask_volume
            return delta
        return 0
    except:
        return 0

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

# ============= TELEGRAM =============
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=10)
        print(f"✅ Telegram sent: {message[:50]}...")
    except Exception as e:
        print(f"❌ Telegram error: {e}")

# ============= TRADE MONITORING =============
def monitor_trades():
    """Background thread that monitors all active trades"""
    print("🔄 Trade monitoring thread started")
    
    while True:
        try:
            time.sleep(MONITORING_INTERVAL)
            
            if not active_trades:
                continue
            
            current_price = get_current_price()
            current_delta = get_orderbook_delta()
            
            if not current_price:
                continue
            
            with trade_lock:
                trades_to_remove = []
                
                for trade_id, trade_info in active_trades.items():
                    entry = trade_info['entry']
                    target = trade_info['target']
                    stop = trade_info.get('stop')
                    direction = trade_info['direction']
                    setup_type = trade_info['setup_type']
                    entry_delta = trade_info['entry_delta']
                    entry_time = trade_info['entry_time']
                    last_update = trade_info['last_update']
                    alerts_sent = trade_info['alerts_sent']
                    
                    # Time in trade
                    hours_in_trade = (datetime.now(timezone.utc) - entry_time).total_seconds() / 3600
                    hours_since_update = (datetime.now(timezone.utc) - last_update).total_seconds() / 3600
                    
                    # ===== LONG TRADES =====
                    if direction == "LONG":
                        # Stop hit
                        if stop and current_price <= stop:
                            send_telegram(f"""
🚨 <b>STOP HIT - {setup_type}</b>

Entry: ${entry:,.0f}
Stop: ${stop:,.0f}
Current: ${current_price:,.0f}

Trade closed. Monitoring ended.
                            """)
                            trades_to_remove.append(trade_id)
                            continue
                        
                        # Delta reversal
                        if entry_delta > 2000 and current_delta < -2000 and 'delta_reversed' not in alerts_sent:
                            send_telegram(f"""
🚨 <b>ORDER FLOW REVERSED - {setup_type}</b>

Entry Delta: +{entry_delta:,.0f} BTC
Current Delta: {current_delta:,.0f} BTC

⚠️ <b>MAJOR REVERSAL - CONSIDER EXIT</b>

Current Price: ${current_price:,.0f}
Entry: ${entry:,.0f}
                            """)
                            alerts_sent.append('delta_reversed')
                        
                        # Approaching target
                        distance_to_target = target - current_price
                        if distance_to_target < 500 and distance_to_target > 0 and 'approaching_target' not in alerts_sent:
                            send_telegram(f"""
🎯 <b>APPROACHING TARGET - {setup_type}</b>

Current: ${current_price:,.0f}
Target: ${target:,.0f}
Distance: ${distance_to_target:,.0f}

💰 Consider taking profit or scaling out
                            """)
                            alerts_sent.append('approaching_target')
                        
                        # Target hit
                        if current_price >= target and 'target_hit' not in alerts_sent:
                            pnl = target - entry
                            send_telegram(f"""
✅ <b>TARGET HIT - {setup_type}</b>

Entry: ${entry:,.0f}
Target: ${target:,.0f}
Current: ${current_price:,.0f}

Profit: ${pnl:,.0f}

🎉 Trade complete! Monitoring ended.
                            """)
                            trades_to_remove.append(trade_id)
                            continue
                        
                        # Hourly update
                        if hours_since_update >= 1.0:
                            pnl = current_price - entry
                            delta_status = "STRONG" if current_delta > 1500 else "WEAKENING" if current_delta < -1000 else "NEUTRAL"
                            
                            send_telegram(f"""
📊 <b>TRADE UPDATE - {setup_type}</b>

Time in Trade: {hours_in_trade:.1f} hours

Entry: ${entry:,.0f}
Current: ${current_price:,.0f}
P&L: ${pnl:+,.0f}

Entry Delta: {entry_delta:+,.0f} BTC
Current Delta: {current_delta:+,.0f} BTC

Status: <b>{delta_status}</b>
                            """)
                            
                            trade_info['last_update'] = datetime.now(timezone.utc)
                    
                    # ===== SHORT TRADES =====
                    elif direction == "SHORT":
                        # Stop hit
                        if stop and current_price >= stop:
                            send_telegram(f"""
🚨 <b>STOP HIT - {setup_type}</b>

Entry: ${entry:,.0f}
Stop: ${stop:,.0f}
Current: ${current_price:,.0f}

Trade closed. Monitoring ended.
                            """)
                            trades_to_remove.append(trade_id)
                            continue
                        
                        # Delta reversal
                        if entry_delta < -2000 and current_delta > 2000 and 'delta_reversed' not in alerts_sent:
                            send_telegram(f"""
🚨 <b>ORDER FLOW REVERSED - {setup_type}</b>

Entry Delta: {entry_delta:,.0f} BTC
Current Delta: +{current_delta:,.0f} BTC

⚠️ <b>MAJOR REVERSAL - CONSIDER EXIT</b>

Current Price: ${current_price:,.0f}
Entry: ${entry:,.0f}
                            """)
                            alerts_sent.append('delta_reversed')
                        
                        # Approaching target
                        distance_to_target = current_price - target
                        if distance_to_target < 500 and distance_to_target > 0 and 'approaching_target' not in alerts_sent:
                            send_telegram(f"""
🎯 <b>APPROACHING TARGET - {setup_type}</b>

Current: ${current_price:,.0f}
Target: ${target:,.0f}
Distance: ${distance_to_target:,.0f}

💰 Consider taking profit or scaling out
                            """)
                            alerts_sent.append('approaching_target')
                        
                        # Target hit
                        if current_price <= target and 'target_hit' not in alerts_sent:
                            pnl = entry - target
                            send_telegram(f"""
✅ <b>TARGET HIT - {setup_type}</b>

Entry: ${entry:,.0f}
Target: ${target:,.0f}
Current: ${current_price:,.0f}

Profit: ${pnl:,.0f}

🎉 Trade complete! Monitoring ended.
                            """)
                            trades_to_remove.append(trade_id)
                            continue
                        
                        # Hourly update
                        if hours_since_update >= 1.0:
                            pnl = entry - current_price
                            delta_status = "STRONG" if current_delta < -1500 else "WEAKENING" if current_delta > 1000 else "NEUTRAL"
                            
                            send_telegram(f"""
📊 <b>TRADE UPDATE - {setup_type}</b>

Time in Trade: {hours_in_trade:.1f} hours

Entry: ${entry:,.0f}
Current: ${current_price:,.0f}
P&L: ${pnl:+,.0f}

Entry Delta: {entry_delta:,.0f} BTC
Current Delta: {current_delta:+,.0f} BTC

Status: <b>{delta_status}</b>
                            """)
                            
                            trade_info['last_update'] = datetime.now(timezone.utc)
                
                # Remove completed trades
                for trade_id in trades_to_remove:
                    del active_trades[trade_id]
                    print(f"❌ Stopped tracking: {trade_id}")
        
        except Exception as e:
            print(f"❌ Monitoring error: {e}")

# ============= WEBHOOK ENDPOINT =============
@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.json
        print(f"📨 Webhook received: {json.dumps(data, indent=2)}")
        
        # Extract data from TradingView alert
        setup_type = data.get('setup_type', 'UNKNOWN')
        direction = data.get('direction', 'UNKNOWN')
        entry = float(data.get('entry', 0))
        target = float(data.get('target', 0))
        stop = data.get('stop')  # Optional
        if stop:
            stop = float(stop)
        
        # Get current order flow
        current_delta = get_orderbook_delta()
        
        # Send initial alert
        alert_emoji = "🟢" if direction == "LONG" else "🔴"
        
        alert_message = f"""
{alert_emoji} <b>{setup_type} - {direction}</b>

Entry: ${entry:,.0f}
Target: ${target:,.0f}
"""
        
        if stop:
            alert_message += f"Stop: ${stop:,.0f}\n"
        else:
            alert_message += "<b>Stop: Use swing structure (see chart)</b>\n"
        
        alert_message += f"""
Order Flow Delta: {current_delta:+,.0f} BTC

📈 <b>{"LONG" if direction == "LONG" else "SHORT"} SIGNAL</b>

🔄 Now monitoring trade for live updates...
        """
        
        send_telegram(alert_message)
        
        # Track this trade
        trade_id = f"{setup_type}_{direction}_{int(time.time())}"
        
        with trade_lock:
            active_trades[trade_id] = {
                'setup_type': setup_type,
                'direction': direction,
                'entry': entry,
                'target': target,
                'stop': stop,
                'entry_delta': current_delta,
                'entry_time': datetime.now(timezone.utc),
                'last_update': datetime.now(timezone.utc),
                'alerts_sent': []
            }
        
        print(f"✅ Now tracking: {trade_id}")
        print(f"📊 Active trades: {len(active_trades)}")
        
        return jsonify({"status": "success", "message": "Trade tracked"}), 200
    
    except Exception as e:
        print(f"❌ Webhook error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/', methods=['GET'])
def home():
    return f"""
    <h1>Beast Mode v3.5 - HYBRID SYSTEM</h1>
    <p>Status: Running ✅</p>
    <p>Active Trades: {len(active_trades)}</p>
    <p>Monitoring Interval: {MONITORING_INTERVAL}s</p>
    <hr>
    <h3>Webhook Endpoint:</h3>
    <code>/webhook</code>
    <p>Method: POST</p>
    <p>Format: JSON</p>
    <hr>
    <h3>Expected JSON Format:</h3>
    <pre>
{{
  "setup_type": "VA RE-ENTRY" / "NAKED POC" / "SINGLE PRINT" / "POOR HIGH" / "POOR LOW",
  "direction": "LONG" / "SHORT",
  "entry": 70440,
  "target": 73392,
  "stop": 68500  (optional)
}}
    </pre>
    """, 200

# ============= STARTUP =============
if __name__ == '__main__':
    print("🚀 Beast Mode v3.5 - HYBRID SYSTEM")
    print("📡 TradingView Webhook Receiver + Trade Monitoring")
    print(f"🔄 Monitoring interval: {MONITORING_INTERVAL}s\n")
    
    # Start monitoring thread
    monitor_thread = threading.Thread(target=monitor_trades, daemon=True)
    monitor_thread.start()
    
    # Send startup message
    send_telegram("🚀 <b>Beast Mode v3.5 Started</b>\n\n📡 Webhook: READY\n🔄 Trade Monitoring: ENABLED\n\nWaiting for TradingView signals...")
    
    # Start Flask app
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
