import os
import time
import hmac
import hashlib
import requests
from datetime import datetime, timezone
import json

# ============= CONFIGURATION =============
BYBIT_API_KEY = os.environ.get('BYBIT_API_KEY')
BYBIT_API_SECRET = os.environ.get('BYBIT_API_SECRET')
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')

SYMBOL = "BTCUSDT"
CHECK_INTERVAL = 900  # 15 minutes (for monitoring trades)
ALERT_COOLDOWN = 3600  # 1 hour between same alert types

# ============= TRADE TRACKING =============
active_trades = {}
last_alerts = {}

# ============= BYBIT API =============
def create_signature(params, secret):
    param_str = '&'.join([f"{k}={params[k]}" for k in sorted(params.keys())])
    return hmac.new(secret.encode('utf-8'), param_str.encode('utf-8'), hashlib.sha256).hexdigest()

def get_klines(category, symbol, interval, limit=200):
    url = "https://api.bybit.com/v5/market/kline"
    params = {"category": category, "symbol": symbol, "interval": interval, "limit": limit}
    try:
        response = requests.get(url, params=params, timeout=10)
        data = response.json()
        if data.get("retCode") == 0:
            return data["result"]["list"]
        return None
    except:
        return None

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
            return delta, bid_volume, ask_volume
        return 0, 0, 0
    except:
        return 0, 0, 0

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

# ============= TPO CALCULATION =============
def calculate_tpo_levels(klines_30m):
    if not klines_30m or len(klines_30m) < 48:
        return None
    
    yesterday_klines = klines_30m[:48]
    
    tpo_counts = {}
    for candle in yesterday_klines:
        high = float(candle[2])
        low = float(candle[3])
        
        tick_size = 50
        low_tick = int(low / tick_size) * tick_size
        high_tick = int(high / tick_size) * tick_size
        
        for price in range(low_tick, high_tick + tick_size, tick_size):
            tpo_counts[price] = tpo_counts.get(price, 0) + 1
    
    if not tpo_counts:
        return None
    
    sorted_levels = sorted(tpo_counts.items(), key=lambda x: x[1], reverse=True)
    total_tpo = sum(tpo_counts.values())
    
    cumulative = 0
    val_high = None
    val_low = None
    
    for price, count in sorted_levels:
        cumulative += count
        if cumulative >= total_tpo * 0.70:
            if val_high is None:
                val_high = price
            val_low = price
    
    poc = sorted_levels[0][0] if sorted_levels else None
    
    return {
        'poc': poc,
        'vah': val_high,
        'val': val_low,
        'day_high': max(float(c[2]) for c in yesterday_klines),
        'day_low': min(float(c[3]) for c in yesterday_klines)
    }

# ============= TELEGRAM =============
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=10)
        print(f"✅ Sent: {message[:50]}...")
    except Exception as e:
        print(f"❌ Telegram error: {e}")

# ============= TRADE MONITORING =============
def track_trade(trade_type, entry_price, target, stop, entry_delta):
    """Start tracking a new trade"""
    active_trades[trade_type] = {
        'entry_price': entry_price,
        'target': target,
        'stop': stop,
        'entry_delta': entry_delta,
        'entry_time': datetime.now(timezone.utc),
        'last_update': datetime.now(timezone.utc),
        'alerts_sent': []
    }
    print(f"📊 Tracking new trade: {trade_type} @ ${entry_price}")

def check_active_trades(current_price, current_delta, tpo_levels):
    """Monitor all active trades and send alerts"""
    
    if not active_trades:
        return
    
    trades_to_remove = []
    
    for trade_type, trade_info in active_trades.items():
        entry = trade_info['entry_price']
        target = trade_info['target']
        stop = trade_info['stop']
        entry_delta = trade_info['entry_delta']
        entry_time = trade_info['entry_time']
        last_update = trade_info['last_update']
        alerts_sent = trade_info['alerts_sent']
        
        # Time in trade
        time_in_trade = (datetime.now(timezone.utc) - entry_time).total_seconds() / 3600
        
        # ===== INVALIDATION CHECKS =====
        
        # Long trade invalidation
        if 'long' in trade_type.lower():
            # Stop hit
            if current_price <= stop:
                send_telegram(f"""
🚨 <b>STOP HIT - {trade_type.upper()}</b>

Entry: ${entry:,.0f}
Stop: ${stop:,.0f}
Current: ${current_price:,.0f}

Trade closed. Monitoring ended.
                """)
                trades_to_remove.append(trade_type)
                continue
            
            # Delta massive reversal
            if entry_delta > 2000 and current_delta < -2000 and 'delta_reversed' not in alerts_sent:
                send_telegram(f"""
🚨 <b>ORDER FLOW REVERSED - {trade_type.upper()}</b>

Entry Delta: +{entry_delta:,.0f} BTC
Current Delta: {current_delta:,.0f} BTC

⚠️ <b>MAJOR REVERSAL - CONSIDER EXIT</b>

Current Price: ${current_price:,.0f}
Entry: ${entry:,.0f}
                """)
                alerts_sent.append('delta_reversed')
            
            # Price broke below VAL significantly
            if tpo_levels and current_price < tpo_levels['val'] - 200 and 'structure_broken' not in alerts_sent:
                send_telegram(f"""
🚨 <b>SETUP INVALIDATED - {trade_type.upper()}</b>

Price broke below VAL support
VAL: ${tpo_levels['val']:,.0f}
Current: ${current_price:,.0f}

⚠️ <b>STRUCTURE COMPROMISED</b>
                """)
                alerts_sent.append('structure_broken')
        
        # Short trade invalidation
        elif 'short' in trade_type.lower():
            # Stop hit
            if current_price >= stop:
                send_telegram(f"""
🚨 <b>STOP HIT - {trade_type.upper()}</b>

Entry: ${entry:,.0f}
Stop: ${stop:,.0f}
Current: ${current_price:,.0f}

Trade closed. Monitoring ended.
                """)
                trades_to_remove.append(trade_type)
                continue
            
            # Delta massive reversal
            if entry_delta < -2000 and current_delta > 2000 and 'delta_reversed' not in alerts_sent:
                send_telegram(f"""
🚨 <b>ORDER FLOW REVERSED - {trade_type.upper()}</b>

Entry Delta: {entry_delta:,.0f} BTC
Current Delta: +{current_delta:,.0f} BTC

⚠️ <b>MAJOR REVERSAL - CONSIDER EXIT</b>

Current Price: ${current_price:,.0f}
Entry: ${entry:,.0f}
                """)
                alerts_sent.append('delta_reversed')
            
            # Price broke above VAH significantly
            if tpo_levels and current_price > tpo_levels['vah'] + 200 and 'structure_broken' not in alerts_sent:
                send_telegram(f"""
🚨 <b>SETUP INVALIDATED - {trade_type.upper()}</b>

Price broke above VAH resistance
VAH: ${tpo_levels['vah']:,.0f}
Current: ${current_price:,.0f}

⚠️ <b>STRUCTURE COMPROMISED</b>
                """)
                alerts_sent.append('structure_broken')
        
        # ===== TARGET APPROACH =====
        
        # Long approaching target
        if 'long' in trade_type.lower():
            distance_to_target = target - current_price
            if distance_to_target < 500 and distance_to_target > 0 and 'approaching_target' not in alerts_sent:
                send_telegram(f"""
🎯 <b>APPROACHING TARGET - {trade_type.upper()}</b>

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
✅ <b>TARGET HIT - {trade_type.upper()}</b>

Entry: ${entry:,.0f}
Target: ${target:,.0f}
Current: ${current_price:,.0f}

Profit: ${pnl:,.0f}

🎉 Trade complete! Monitoring ended.
                """)
                trades_to_remove.append(trade_type)
                continue
        
        # Short approaching target
        elif 'short' in trade_type.lower():
            distance_to_target = current_price - target
            if distance_to_target < 500 and distance_to_target > 0 and 'approaching_target' not in alerts_sent:
                send_telegram(f"""
🎯 <b>APPROACHING TARGET - {trade_type.upper()}</b>

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
✅ <b>TARGET HIT - {trade_type.upper()}</b>

Entry: ${entry:,.0f}
Target: ${target:,.0f}
Current: ${current_price:,.0f}

Profit: ${pnl:,.0f}

🎉 Trade complete! Monitoring ended.
                """)
                trades_to_remove.append(trade_type)
                continue
        
        # ===== HOURLY STATUS UPDATES =====
        
        hours_since_update = (datetime.now(timezone.utc) - last_update).total_seconds() / 3600
        
        if hours_since_update >= 1.0:  # Every hour
            # Calculate status
            if 'long' in trade_type.lower():
                pnl = current_price - entry
                delta_status = "STRONG" if current_delta > 1500 else "WEAKENING" if current_delta < -1000 else "NEUTRAL"
            else:
                pnl = entry - current_price
                delta_status = "STRONG" if current_delta < -1500 else "WEAKENING" if current_delta > 1000 else "NEUTRAL"
            
            send_telegram(f"""
📊 <b>TRADE UPDATE - {trade_type.upper()}</b>

Time in Trade: {time_in_trade:.1f} hours

Entry: ${entry:,.0f}
Current: ${current_price:,.0f}
P&L: ${pnl:+,.0f}

Entry Delta: {entry_delta:+,.0f} BTC
Current Delta: {current_delta:+,.0f} BTC

Status: <b>{delta_status}</b>
            """)
            
            trade_info['last_update'] = datetime.now(timezone.utc)
    
    # Remove completed trades
    for trade_type in trades_to_remove:
        del active_trades[trade_type]
        print(f"❌ Stopped tracking: {trade_type}")

# ============= SIGNAL DETECTION (WITH AUTO-TRACKING) =============
def check_va_reentry(current_price, tpo_levels, current_delta):
    if not tpo_levels:
        return
    
    val = tpo_levels['val']
    vah = tpo_levels['vah']
    
    # LONG: Re-entry from below VAL
    if current_price >= val - 50 and current_price <= val + 50:
        if current_delta > 2000:  # Bullish order flow
            alert_key = f"va_reentry_long_{int(time.time() // ALERT_COOLDOWN)}"
            if alert_key not in last_alerts:
                send_telegram(f"""
🟢 <b>80% RULE - ROTATION TO VAH</b>

Entry: ~${val:,.0f} (VAL)
<b>Stop: Place below swing low that created the move</b>
      (Use market structure - typically $1,500-$2,500 below)
Target: ${vah:,.0f} (VAH)

Combined Delta: {current_delta:+,.0f} BTC
Bias: <b>BULLISH</b>
Classification: SWING (1-3 days)

📈 <b>LONG RECOMMENDED</b>

⚠️ Set stop at swing pivot low, NOT tight stops!
                """)
                last_alerts[alert_key] = time.time()
                
                # AUTO-TRACK THIS TRADE
                # User will set their own stop, so we use a placeholder
                estimated_stop = val - 2000  # Placeholder
                track_trade('va_reentry_long', val, vah, estimated_stop, current_delta)
    
    # SHORT: Re-entry from above VAH
    elif current_price >= vah - 50 and current_price <= vah + 50:
        if current_delta < -2000:  # Bearish order flow
            alert_key = f"va_reentry_short_{int(time.time() // ALERT_COOLDOWN)}"
            if alert_key not in last_alerts:
                send_telegram(f"""
🔴 <b>80% RULE - ROTATION TO VAL</b>

Entry: ~${vah:,.0f} (VAH)
<b>Stop: Place above swing high that created the move</b>
      (Use market structure - typically $1,500-$2,500 above)
Target: ${val:,.0f} (VAL)

Combined Delta: {current_delta:,.0f} BTC
Bias: <b>BEARISH</b>
Classification: SWING (1-3 days)

📉 <b>SHORT RECOMMENDED</b>

⚠️ Set stop at swing pivot high, NOT tight stops!
                """)
                last_alerts[alert_key] = time.time()
                
                # AUTO-TRACK THIS TRADE
                estimated_stop = vah + 2000  # Placeholder
                track_trade('va_reentry_short', vah, val, estimated_stop, current_delta)

# ============= MAIN LOOP =============
def main():
    print("🚀 Beast Mode v3.1 - TRADE MONITORING ENABLED")
    print(f"✅ Checking every {CHECK_INTERVAL}s")
    print(f"📊 Monitoring active trades for follow-ups\n")
    
    send_telegram("🚀 <b>Beast Mode v3.1 Started</b>\n\n📊 Trade monitoring: ENABLED\n✅ Will track order flow after entries")
    
    while True:
        try:
            # Get data
            klines_30m = get_klines("linear", SYMBOL, "30", 200)
            current_price = get_current_price()
            current_delta, bid_vol, ask_vol = get_orderbook_delta()
            
            if not klines_30m or not current_price:
                print("⚠️ Data unavailable, retrying...")
                time.sleep(CHECK_INTERVAL)
                continue
            
            # Calculate TPO
            tpo_levels = calculate_tpo_levels(klines_30m)
            
            print(f"\n⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
            print(f"💰 Price: ${current_price:,.0f}")
            print(f"📊 Delta: {current_delta:+,.0f} BTC")
            if tpo_levels:
                print(f"📈 VAH: ${tpo_levels['vah']:,.0f} | VAL: ${tpo_levels['val']:,.0f}")
            print(f"🎯 Active trades: {len(active_trades)}")
            
            # Check for new signals
            check_va_reentry(current_price, tpo_levels, current_delta)
            
            # Monitor active trades
            check_active_trades(current_price, current_delta, tpo_levels)
            
            time.sleep(CHECK_INTERVAL)
            
        except Exception as e:
            print(f"❌ Error: {e}")
            time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
