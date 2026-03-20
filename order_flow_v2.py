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
ORDERFLOW_INTERVAL = 900  # 15 minutes
TRADE_CHECK_INTERVAL = 900  # 15 minutes

# Order flow thresholds
DELTA_THRESHOLD_STRONG = 2000
IMBALANCE_RATIO_MIN = 3.0
BUY_PRESSURE_MIN = 60

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
            total_volume = bid_volume + ask_volume
            
            delta = bid_volume - ask_volume
            bid_pct = (bid_volume / total_volume * 100) if total_volume > 0 else 50
            
            return {
                'delta': delta,
                'bid_volume': bid_volume,
                'ask_volume': ask_volume,
                'bid_pct': bid_pct
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
            return float(data["result"]["list"][0]["lastPrice"])
        return None
    except:
        return None

def get_recent_trades_analysis(price_level=None, range_size=100):
    url = "https://api.bybit.com/v5/market/recent-trade"
    params = {"category": "linear", "symbol": SYMBOL, "limit": 1000}
    try:
        response = requests.get(url, params=params, timeout=10)
        data = response.json()
        if data.get("retCode") == 0:
            trades = data["result"]["list"]
            
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
        return None
    except:
        return None

# ============= ORDER FLOW CONFIRMATION =============
def confirm_order_flow(direction, entry_price):
    orderbook = get_orderbook_delta()
    trades = get_recent_trades_analysis(price_level=entry_price, range_size=100)
    
    if not orderbook or not trades:
        return False, "Unable to fetch order flow data", None
    
    delta = orderbook['delta']
    buy_pct = trades['buy_pct']
    imbalance = trades['imbalance']
    
    data = {
        'delta': delta,
        'buy_pct': buy_pct,
        'imbalance_ratio': imbalance['ratio'] if imbalance else None
    }
    
    if direction == "LONG":
        reasons = []
        score = 0
        
        if delta > DELTA_THRESHOLD_STRONG:
            reasons.append(f"✅ Strong delta: +{delta:,.0f} BTC")
            score += 3
        elif delta > 0:
            reasons.append(f"⚠️ Weak delta: +{delta:,.0f} BTC")
            score += 1
        else:
            reasons.append(f"❌ Negative delta: {delta:,.0f} BTC")
        
        if buy_pct >= BUY_PRESSURE_MIN:
            reasons.append(f"✅ Strong buys: {buy_pct:.1f}%")
            score += 2
        else:
            reasons.append(f"❌ Weak buys: {buy_pct:.1f}%")
        
        if imbalance and imbalance['is_buy_imbalance']:
            reasons.append(f"✅ Buy imbalance: {imbalance['ratio']:.1f}:1")
            score += 2
        
        if score >= 5:
            return True, "\n".join(reasons), data
        else:
            return False, "\n".join(reasons), data
    
    elif direction == "SHORT":
        reasons = []
        score = 0
        
        if delta < -DELTA_THRESHOLD_STRONG:
            reasons.append(f"✅ Strong delta: {delta:,.0f} BTC")
            score += 3
        elif delta < 0:
            reasons.append(f"⚠️ Weak delta: {delta:,.0f} BTC")
            score += 1
        else:
            reasons.append(f"❌ Positive delta: +{delta:,.0f} BTC")
        
        sell_pct = 100 - buy_pct
        if sell_pct >= BUY_PRESSURE_MIN:
            reasons.append(f"✅ Strong sells: {sell_pct:.1f}%")
            score += 2
        else:
            reasons.append(f"❌ Weak sells: {sell_pct:.1f}%")
        
        if imbalance and imbalance['is_sell_imbalance']:
            reasons.append(f"✅ Sell imbalance: 1:{imbalance['ratio']:.1f}")
            score += 2
        
        if score >= 5:
            return True, "\n".join(reasons), data
        else:
            return False, "\n".join(reasons), data
    
    return False, "Unknown direction", data

# ============= TELEGRAM =============
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=10)
        print(f"✅ Sent")
    except:
        pass

# ============= CONTINUOUS ORDER FLOW =============
def monitor_orderflow():
    print("📊 Order flow monitoring started")
    
    while True:
        try:
            time.sleep(ORDERFLOW_INTERVAL)
            
            current_price = get_current_price()
            orderbook = get_orderbook_delta()
            trades = get_recent_trades_analysis()
            
            if not current_price or not orderbook or not trades:
                continue
            
            delta = orderbook['delta']
            bid_pct = orderbook['bid_pct']
            buy_pct = trades['buy_pct']
            cvd = trades['cvd']
            
            if delta > 2000 and cvd > 50:
                bias = "🟢 BULLISH"
            elif delta < -2000 and cvd < -50:
                bias = "🔴 BEARISH"
            else:
                bias = "⚪ NEUTRAL"
            
            message = f"""
📊 <b>ORDER FLOW UPDATE</b>

💰 Price: ${current_price:,.0f}

<b>ORDERBOOK</b>
Delta: {delta:+,.0f} BTC
Bids: {bid_pct:.1f}%

<b>TRADES</b>
CVD: {cvd:+,.1f} BTC
Buys: {buy_pct:.1f}%

<b>{bias}</b>

⏰ {datetime.now(timezone.utc).strftime('%H:%M UTC')}
            """
            
            send_telegram(message)
            
        except Exception as e:
            print(f"❌ Order flow error: {e}")

# ============= TRADE MONITORING =============
def monitor_trades():
    print("🔄 Trade monitoring started")
    
    while True:
        try:
            time.sleep(TRADE_CHECK_INTERVAL)
            
            if not active_trades:
                continue
            
            current_price = get_current_price()
            orderbook = get_orderbook_delta()
            
            if not current_price or not orderbook:
                continue
            
            current_delta = orderbook['delta']
            
            with trade_lock:
                trades_to_remove = []
                
                for trade_id, info in active_trades.items():
                    entry = info['entry']
                    target = info['target']
                    direction = info['direction']
                    setup_type = info['setup_type']
                    entry_delta = info['entry_delta']
                    last_update = info['last_update']
                    alerts_sent = info['alerts_sent']
                    
                    hours_since_update = (datetime.now(timezone.utc) - last_update).total_seconds() / 3600
                    
                    if direction == "LONG":
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
                        
                        if target - current_price < 500 and 'target_approach' not in alerts_sent:
                            send_telegram(f"🎯 <b>{setup_type} - APPROACHING TARGET</b>\n\nCurrent: ${current_price:,.0f}\nTarget: ${target:,.0f}")
                            alerts_sent.append('target_approach')
                        
                        if current_price >= target:
                            send_telegram(f"✅ <b>{setup_type} - TARGET HIT</b>\n\nProfit: ${target - entry:,.0f}\n🎉")
                            trades_to_remove.append(trade_id)
                            continue
                        
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
                        
                        if current_price - target < 500 and 'target_approach' not in alerts_sent:
                            send_telegram(f"🎯 <b>{setup_type} - APPROACHING TARGET</b>\n\nCurrent: ${current_price:,.0f}\nTarget: ${target:,.0f}")
                            alerts_sent.append('target_approach')
                        
                        if current_price <= target:
                            send_telegram(f"✅ <b>{setup_type} - TARGET HIT</b>\n\nProfit: ${entry - target:,.0f}\n🎉")
                            trades_to_remove.append(trade_id)
                            continue
                        
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
        
        # PARSE DIFFERENT ALERT TYPES
        if alert_type == "tpo_poor_hl":
            # Poor High/Low
            level_type = data.get('level_type')
            level_price = float(data.get('level_price', 0))
            
            if level_type == "poor_high":
                setup_type = "POOR HIGH"
                direction = "SHORT"
                entry = level_price
                target = level_price - 2000  # Example target
            elif level_type == "poor_low":
                setup_type = "POOR LOW"
                direction = "LONG"
                entry = level_price
                target = level_price + 2000
        
        elif alert_type == "value_area_reentry":
            # VA Re-Entry
            reentry_direction = data.get('direction')
            vah = float(data.get('vah', 0))
            val = float(data.get('val', 0))
            
            setup_type = "VA RE-ENTRY"
            if reentry_direction == "from_below":
                direction = "LONG"
                entry = val
                target = vah
            elif reentry_direction == "from_above":
                direction = "SHORT"
                entry = vah
                target = val
        
        elif alert_type == "naked_poc":
            # Naked POC (unified - no type tracking)
            level_price = float(data.get('level_price', 0))
            
            setup_type = "NAKED POC"
            
            # Determine direction based on price position
            current_price = get_current_price()
            if current_price and current_price < level_price:
                direction = "LONG"
                entry = level_price
                target = level_price + 2000
            else:
                direction = "SHORT"
                entry = level_price
                target = level_price - 2000
        
        elif alert_type == "single_print":
            # Single Print (Weekly or Monthly only)
            sp_type = data.get('sp_type', 'weekly')
            level_high = float(data.get('level_high', 0))
            level_low = float(data.get('level_low', 0))
            
            if sp_type == "weekly":
                setup_type = "SINGLE PRINT (Weekly)"
            elif sp_type == "monthly":
                setup_type = "SINGLE PRINT (Monthly)"
            else:
                setup_type = "SINGLE PRINT"
            
            # Determine direction
            current_price = get_current_price()
            if current_price and current_price < level_low:
                direction = "LONG"
                entry = level_low
                target = level_high
            else:
                direction = "SHORT"
                entry = level_high
                target = level_low
        
        # ORDER FLOW CONFIRMATION
        confirmed, reason, of_data = confirm_order_flow(direction, entry)
        
        if confirmed:
            alert_emoji = "🟢" if direction == "LONG" else "🔴"
            message = f"""
{alert_emoji} <b>{setup_type} - {direction} ✅ CONFIRMED</b>

Entry: ${entry:,.0f}
Target: ${target:,.0f}

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
                    'target': target,
                    'entry_delta': of_data['delta'],
                    'entry_time': datetime.now(timezone.utc),
                    'last_update': datetime.now(timezone.utc),
                    'alerts_sent': []
                }
            
            print(f"✅ Trade confirmed: {trade_id}")
        
        else:
            message = f"""
⚠️ <b>{setup_type} - {direction} ❌ REJECTED</b>

Entry: ${entry:,.0f}

<b>ORDER FLOW:</b>
{reason}

🚫 <b>SKIPPING</b>
            """
            
            send_telegram(message)
            print(f"❌ Trade rejected")
        
        return jsonify({"status": "success"}), 200
    
    except Exception as e:
        print(f"❌ Webhook error: {e}")
        return jsonify({"status": "error"}), 500

@app.route('/', methods=['GET'])
def home():
    return f"""
    <h1>Beast Mode FINAL</h1>
    <p>Active Trades: {len(active_trades)}</p>
    <ul>
        <li>✅ TPO Structure (TradingView)</li>
        <li>✅ Order Flow Filter</li>
        <li>✅ Continuous Monitoring</li>
        <li>✅ Trade Alerts</li>
    </ul>
    """, 200

if __name__ == '__main__':
    print("🚀 Beast Mode FINAL")
    
    orderflow_thread = threading.Thread(target=monitor_orderflow, daemon=True)
    orderflow_thread.start()
    
    trade_thread = threading.Thread(target=monitor_trades, daemon=True)
    trade_thread.start()
    
    send_telegram("🚀 <b>Beast Mode FINAL Started</b>\n\n📊 Order Flow: ON\n🔄 Monitoring: ON\n✅ Filter: ACTIVE")
    
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
