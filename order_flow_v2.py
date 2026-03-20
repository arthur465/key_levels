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
ORDERFLOW_INTERVAL = 900  # 15 minutes - continuous order flow updates
TRADE_CHECK_INTERVAL = 900  # 15 minutes - trade monitoring

# ============= TRADE TRACKING =============
active_trades = {}
trade_lock = threading.Lock()

# ============= FLASK APP =============
app = Flask(__name__)

# ============= BYBIT API =============
def get_orderbook_delta():
    """Get current orderbook delta and imbalance"""
    url = "https://api.bybit.com/v5/market/orderbook"
    params = {"category": "linear", "symbol": SYMBOL, "limit": 200}
    try:
        response = requests.get(url, params=params, timeout=10)
        data = response.json()
        if data.get("retCode") == 0:
            bids = data["result"]["b"]
            asks = data["result"]["a"]
            
            # Calculate volumes
            bid_volume = sum(float(b[1]) for b in bids)
            ask_volume = sum(float(a[1]) for a in asks)
            total_volume = bid_volume + ask_volume
            
            # Delta
            delta = bid_volume - ask_volume
            
            # Imbalance percentage
            bid_pct = (bid_volume / total_volume * 100) if total_volume > 0 else 50
            ask_pct = (ask_volume / total_volume * 100) if total_volume > 0 else 50
            
            # Top of book
            best_bid = float(bids[0][0]) if bids else 0
            best_ask = float(asks[0][0]) if asks else 0
            spread = best_ask - best_bid
            
            return {
                'delta': delta,
                'bid_volume': bid_volume,
                'ask_volume': ask_volume,
                'bid_pct': bid_pct,
                'ask_pct': ask_pct,
                'best_bid': best_bid,
                'best_ask': best_ask,
                'spread': spread
            }
        return None
    except:
        return None

def get_current_price():
    url = "https://api.bybit.com/v5/market/tickers"
    params = {"category": "linear", "symbol": SYMBOL}
    try:
        response = requests.get(url, params=params, timeout=10)
        data = response.json()
        if data.get("retCode") == 0:
            ticker = data["result"]["list"][0]
            return {
                'price': float(ticker["lastPrice"]),
                'volume_24h': float(ticker["volume24h"]),
                'turnover_24h': float(ticker["turnover24h"])
            }
        return None
    except:
        return None

def get_recent_trades():
    """Get recent trades to analyze buying/selling pressure"""
    url = "https://api.bybit.com/v5/market/recent-trade"
    params = {"category": "linear", "symbol": SYMBOL, "limit": 500}
    try:
        response = requests.get(url, params=params, timeout=10)
        data = response.json()
        if data.get("retCode") == 0:
            trades = data["result"]["list"]
            
            buy_volume = sum(float(t["size"]) for t in trades if t["side"] == "Buy")
            sell_volume = sum(float(t["size"]) for t in trades if t["side"] == "Sell")
            total = buy_volume + sell_volume
            
            buy_pct = (buy_volume / total * 100) if total > 0 else 50
            sell_pct = (sell_volume / total * 100) if total > 0 else 50
            
            cvd = buy_volume - sell_volume
            
            return {
                'buy_volume': buy_volume,
                'sell_volume': sell_volume,
                'buy_pct': buy_pct,
                'sell_pct': sell_pct,
                'cvd': cvd
            }
        return None
    except:
        return None

# ============= TELEGRAM =============
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=10)
    except:
        pass

# ============= CONTINUOUS ORDER FLOW MONITORING =============
def monitor_orderflow():
    """Continuous order flow analysis - runs 24/7"""
    print("📊 Order flow monitoring thread started")
    
    while True:
        try:
            time.sleep(ORDERFLOW_INTERVAL)
            
            # Get all data
            price_data = get_current_price()
            orderbook_data = get_orderbook_delta()
            trades_data = get_recent_trades()
            
            if not price_data or not orderbook_data or not trades_data:
                continue
            
            # Build order flow report
            current_price = price_data['price']
            delta = orderbook_data['delta']
            bid_pct = orderbook_data['bid_pct']
            ask_pct = orderbook_data['ask_pct']
            spread = orderbook_data['spread']
            
            cvd = trades_data['cvd']
            buy_pct = trades_data['buy_pct']
            sell_pct = trades_data['sell_pct']
            
            # Determine bias
            if delta > 2000 and cvd > 50:
                bias = "🟢 BULLISH"
            elif delta < -2000 and cvd < -50:
                bias = "🔴 BEARISH"
            else:
                bias = "⚪ NEUTRAL"
            
            # Build message
            message = f"""
📊 <b>ORDER FLOW UPDATE</b>

💰 Price: ${current_price:,.0f}
Spread: ${spread:.2f}

<b>ORDERBOOK DELTA</b>
Delta: {delta:+,.0f} BTC
Bids: {bid_pct:.1f}% | Asks: {ask_pct:.1f}%

<b>RECENT TRADES (500)</b>
CVD: {cvd:+,.1f} BTC
Buys: {buy_pct:.1f}% | Sells: {sell_pct:.1f}%

<b>BIAS: {bias}</b>

⏰ {datetime.now(timezone.utc).strftime('%H:%M UTC')}
            """
            
            send_telegram(message)
            print(f"📊 Order flow update sent - Price: ${current_price:,.0f}, Delta: {delta:+,.0f}")
            
        except Exception as e:
            print(f"❌ Order flow error: {e}")

# ============= TRADE MONITORING =============
def monitor_trades():
    """Monitor active trades from TradingView signals"""
    print("🔄 Trade monitoring thread started")
    
    while True:
        try:
            time.sleep(TRADE_CHECK_INTERVAL)
            
            if not active_trades:
                continue
            
            price_data = get_current_price()
            orderbook_data = get_orderbook_delta()
            
            if not price_data or not orderbook_data:
                continue
            
            current_price = price_data['price']
            current_delta = orderbook_data['delta']
            
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

❌ Trade closed
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

Current: ${current_price:,.0f}
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

💰 Consider taking profit
                            """)
                            alerts_sent.append('approaching_target')
                        
                        # Target hit
                        if current_price >= target and 'target_hit' not in alerts_sent:
                            pnl = target - entry
                            send_telegram(f"""
✅ <b>TARGET HIT - {setup_type}</b>

Entry: ${entry:,.0f}
Target: ${target:,.0f}
Profit: ${pnl:,.0f}

🎉 Trade complete!
                            """)
                            trades_to_remove.append(trade_id)
                            continue
                        
                        # Hourly update
                        if hours_since_update >= 1.0:
                            pnl = current_price - entry
                            delta_status = "STRONG 💪" if current_delta > 1500 else "WEAKENING ⚠️" if current_delta < -1000 else "NEUTRAL ⚪"
                            
                            send_telegram(f"""
📊 <b>TRADE UPDATE - {setup_type}</b>

⏱ Time: {hours_in_trade:.1f}h

Entry: ${entry:,.0f}
Current: ${current_price:,.0f}
P&L: ${pnl:+,.0f}

Entry Δ: {entry_delta:+,.0f} BTC
Current Δ: {current_delta:+,.0f} BTC

Status: {delta_status}
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

❌ Trade closed
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

Current: ${current_price:,.0f}
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

💰 Consider taking profit
                            """)
                            alerts_sent.append('approaching_target')
                        
                        # Target hit
                        if current_price <= target and 'target_hit' not in alerts_sent:
                            pnl = entry - target
                            send_telegram(f"""
✅ <b>TARGET HIT - {setup_type}</b>

Entry: ${entry:,.0f}
Target: ${target:,.0f}
Profit: ${pnl:,.0f}

🎉 Trade complete!
                            """)
                            trades_to_remove.append(trade_id)
                            continue
                        
                        # Hourly update
                        if hours_since_update >= 1.0:
                            pnl = entry - current_price
                            delta_status = "STRONG 💪" if current_delta < -1500 else "WEAKENING ⚠️" if current_delta > 1000 else "NEUTRAL ⚪"
                            
                            send_telegram(f"""
📊 <b>TRADE UPDATE - {setup_type}</b>

⏱ Time: {hours_in_trade:.1f}h

Entry: ${entry:,.0f}
Current: ${current_price:,.0f}
P&L: ${pnl:+,.0f}

Entry Δ: {entry_delta:,.0f} BTC
Current Δ: {current_delta:+,.0f} BTC

Status: {delta_status}
                            """)
                            
                            trade_info['last_update'] = datetime.now(timezone.utc)
                
                # Remove completed trades
                for trade_id in trades_to_remove:
                    del active_trades[trade_id]
                    print(f"❌ Stopped tracking: {trade_id}")
        
        except Exception as e:
            print(f"❌ Trade monitoring error: {e}")

# ============= WEBHOOK ENDPOINT =============
@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.json
        print(f"📨 Webhook received: {json.dumps(data, indent=2)}")
        
        # Get current data
        orderbook_data = get_orderbook_delta()
        price_data = get_current_price()
        
        if not orderbook_data or not price_data:
            return jsonify({"status": "error", "message": "Could not fetch market data"}), 500
        
        current_delta = orderbook_data['delta']
        current_price = price_data['price']
        
        # Extract from webhook - FLEXIBLE FORMAT
        # Support both JSON format AND plain text
        if isinstance(data, dict):
            setup_type = data.get('setup_type', data.get('type', 'SIGNAL'))
            direction = data.get('direction', 'LONG')
            entry = float(data.get('entry', current_price))
            target = float(data.get('target', entry + 2000 if direction == "LONG" else entry - 2000))
            stop = data.get('stop')
            if stop:
                stop = float(stop)
        else:
            # Plain text format - parse it
            setup_type = "SIGNAL"
            direction = "LONG" if "long" in str(data).lower() else "SHORT"
            entry = current_price
            target = entry + 2000 if direction == "LONG" else entry - 2000
            stop = None
        
        # Send entry alert
        alert_emoji = "🟢" if direction == "LONG" else "🔴"
        
        alert_message = f"""
{alert_emoji} <b>{setup_type} - {direction}</b>

Entry: ${entry:,.0f}
Target: ${target:,.0f}
"""
        
        if stop:
            alert_message += f"Stop: ${stop:,.0f}\n"
        else:
            alert_message += "<b>Stop: Use swing structure</b>\n"
        
        alert_message += f"""
Current Delta: {current_delta:+,.0f} BTC

📈 <b>{direction} SIGNAL</b>

🔄 Monitoring started...
        """
        
        send_telegram(alert_message)
        
        # Track trade
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
        
        print(f"✅ Trade tracked: {trade_id}")
        
        return jsonify({"status": "success"}), 200
    
    except Exception as e:
        print(f"❌ Webhook error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/', methods=['GET'])
def home():
    return f"""
    <h1>Beast Mode ULTIMATE</h1>
    <p>Status: Running ✅</p>
    <p>Active Trades: {len(active_trades)}</p>
    <p>Order Flow Interval: {ORDERFLOW_INTERVAL}s</p>
    <hr>
    <h3>Features:</h3>
    <ul>
        <li>✅ Continuous Order Flow Monitoring (every 15 min)</li>
        <li>✅ TradingView Webhook Receiver</li>
        <li>✅ Live Trade Monitoring</li>
        <li>✅ Delta Reversal Alerts</li>
        <li>✅ Target Approach Alerts</li>
    </ul>
    """, 200

# ============= STARTUP =============
if __name__ == '__main__':
    print("🚀 Beast Mode ULTIMATE")
    print("📊 Continuous Order Flow + Trade Monitoring")
    print(f"🔄 Order flow updates every {ORDERFLOW_INTERVAL}s\n")
    
    # Start monitoring threads
    orderflow_thread = threading.Thread(target=monitor_orderflow, daemon=True)
    orderflow_thread.start()
    
    trade_monitor_thread = threading.Thread(target=monitor_trades, daemon=True)
    trade_monitor_thread.start()
    
    # Send startup message
    send_telegram("🚀 <b>Beast Mode ULTIMATE Started</b>\n\n📊 Continuous order flow: ON\n📡 Webhook: READY\n🔄 Trade monitoring: ON")
    
    # Start Flask
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
