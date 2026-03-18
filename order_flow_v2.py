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

# Exchange WebSocket URLs
BYBIT_WS_URL = "wss://stream.bybit.com/v5/public/linear"
BINANCE_WS_URL = "wss://fstream.binance.com/ws/btcusdt@aggTrade"
BINANCE_LIQUIDATION_URL = "wss://fstream.binance.com/ws/btcusdt@forceOrder"
BINANCE_ORDERBOOK_URL = "wss://fstream.binance.com/ws/btcusdt@depth20@100ms"

# ===== QUALITY FILTER SETTINGS =====
QUALITY_FILTERS = {
    'min_delta': 400,           # Minimum delta (BTC) - 2.5x higher than before
    'min_bid_ratio': 0.72,      # Minimum bid ratio for bullish (72%)
    'max_bid_ratio': 0.28,      # Maximum bid ratio for bearish (28%)
    'min_volume': 150,          # Minimum total volume (BTC)
    'sustained_checks': 3,      # Number of checks for sustained pressure
    'sustained_interval': 300,  # Time between checks (5 min = 300 sec)
    'chop_threshold': 3,        # Number of MSBs in range to consider chop
    'chop_range': 500,          # Price range to check for chop ($500)
    'chop_timeframe': 14400,    # Time window for chop detection (4H = 14400 sec)
    'absorption_volume': 300,   # Volume threshold for absorption
    'absorption_movement': 100, # Max price movement for absorption ($100)
    'require_both_exchanges': True,  # Require confirmation from BOTH exchanges
    'liquidation_cascade_threshold': 50_000_000,  # $50M for cascade alert
    'orderbook_wall_size': 100,  # 100 BTC = significant wall
}

# ===== STATE =====
# Separate tracking for each exchange
footprint_bybit = defaultdict(lambda: {'bid': 0, 'ask': 0, 'trades': 0})
footprint_binance = defaultdict(lambda: {'bid': 0, 'ask': 0, 'trades': 0})

cumulative_delta_bybit = 0
cumulative_delta_binance = 0
trade_count_bybit = 0
trade_count_binance = 0

# Liquidation tracking (last 5 minutes)
liquidations = []  # Format: {'timestamp': t, 'side': 'LONG'/'SHORT', 'quantity': q, 'price': p}

# Order book state (updated in real-time)
order_book = {
    'bids': [],  # Format: [{'price': p, 'quantity': q}, ...]
    'asks': []   # Format: [{'price': p, 'quantity': q}, ...]
}

# Active levels to monitor
active_levels = {}
level_counter = {'msb': 0, 'ob': 0, 'breaker': 0}

# Sustained pressure tracking
pressure_history = defaultdict(list)

last_alert_time = defaultdict(float)
last_cascade_alert = 0

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

# ===== QUALITY FILTER FUNCTIONS =====

def is_choppy_market(price):
    """Detect if market is choppy (too many MSBs in small range recently)"""
    current_time = time.time()
    chop_window = current_time - QUALITY_FILTERS['chop_timeframe']
    
    # Get recent MSB levels
    recent_msbs = [
        data for level_id, data in active_levels.items()
        if data['type'] == 'msb' 
        and data.get('timestamp', 0) > chop_window
    ]
    
    if len(recent_msbs) < QUALITY_FILTERS['chop_threshold']:
        return False
    
    # Check if they're all in a tight range
    prices = [data['price'] for data in recent_msbs]
    price_range = max(prices) - min(prices)
    
    if price_range < QUALITY_FILTERS['chop_range']:
        print(f"⚠️ Choppy market detected: {len(recent_msbs)} MSBs in ${price_range:.0f} range")
        return True
    
    return False

def check_sustained_pressure(level_id, delta_bybit, delta_binance, bid_ratio_bybit, bid_ratio_binance, volume):
    """Check if pressure is sustained over time on BOTH exchanges"""
    global pressure_history
    
    current_time = time.time()
    
    # Add current reading
    pressure_history[level_id].append({
        'timestamp': current_time,
        'delta_bybit': delta_bybit,
        'delta_binance': delta_binance,
        'bid_ratio_bybit': bid_ratio_bybit,
        'bid_ratio_binance': bid_ratio_binance,
        'volume': volume
    })
    
    # Clean old readings (keep only last 20 minutes)
    pressure_history[level_id] = [
        p for p in pressure_history[level_id]
        if current_time - p['timestamp'] < 1200  # 20 min
    ]
    
    # Need at least required number of checks
    if len(pressure_history[level_id]) < QUALITY_FILTERS['sustained_checks']:
        return False
    
    # Get last N checks
    recent_checks = pressure_history[level_id][-QUALITY_FILTERS['sustained_checks']:]
    
    # Check if they're spread out over time (not all at once)
    first_time = recent_checks[0]['timestamp']
    last_time = recent_checks[-1]['timestamp']
    time_span = last_time - first_time
    
    if time_span < QUALITY_FILTERS['sustained_interval']:
        return False  # Too quick, not sustained
    
    # Check if all readings show same pressure on BOTH exchanges
    avg_bid_ratio_bybit = sum(p['bid_ratio_bybit'] for p in recent_checks) / len(recent_checks)
    avg_bid_ratio_binance = sum(p['bid_ratio_binance'] for p in recent_checks) / len(recent_checks)
    
    # All checks should be consistent on BOTH exchanges (within 10% of average)
    consistent_bybit = all(
        abs(p['bid_ratio_bybit'] - avg_bid_ratio_bybit) < 0.10
        for p in recent_checks
    )
    
    consistent_binance = all(
        abs(p['bid_ratio_binance'] - avg_bid_ratio_binance) < 0.10
        for p in recent_checks
    )
    
    if consistent_bybit and consistent_binance:
        print(f"✓ Sustained pressure on BOTH exchanges for {level_id} over {time_span:.0f}s")
        return True
    
    return False

def detect_absorption(range_data, price_start, price_current):
    """Detect absorption pattern (high volume, low price movement)"""
    total_volume = sum(d['bid'] + d['ask'] for d in range_data.values())
    price_movement = abs(price_current - price_start)
    
    if (total_volume > QUALITY_FILTERS['absorption_volume'] and 
        price_movement < QUALITY_FILTERS['absorption_movement']):
        return True, total_volume, price_movement
    
    return False, 0, 0

# ===== LIQUIDATION ANALYSIS =====

def get_recent_liquidations(minutes=5):
    """Get liquidations from last N minutes"""
    global liquidations
    
    current_time = time.time()
    cutoff_time = current_time - (minutes * 60)
    
    # Clean old liquidations
    liquidations = [liq for liq in liquidations if liq['timestamp'] > cutoff_time]
    
    return liquidations

def analyze_liquidations():
    """Analyze recent liquidation data"""
    recent = get_recent_liquidations(5)
    
    if not recent:
        return None
    
    long_liqs = [liq for liq in recent if liq['side'] == 'LONG']
    short_liqs = [liq for liq in recent if liq['side'] == 'SHORT']
    
    long_value = sum(liq['quantity'] * liq['price'] for liq in long_liqs)
    short_value = sum(liq['quantity'] * liq['price'] for liq in short_liqs)
    
    long_btc = sum(liq['quantity'] for liq in long_liqs)
    short_btc = sum(liq['quantity'] for liq in short_liqs)
    
    return {
        'long_value': long_value,
        'short_value': short_value,
        'long_btc': long_btc,
        'short_btc': short_btc,
        'total_value': long_value + short_value,
        'count': len(recent)
    }

def detect_liquidation_cascade(current_price):
    """Detect if we're in a liquidation cascade"""
    global last_cascade_alert
    
    liq_data = analyze_liquidations()
    
    if not liq_data:
        return False, None
    
    # Cascade = >$50M liquidated in 5 min
    if liq_data['total_value'] > QUALITY_FILTERS['liquidation_cascade_threshold']:
        # Check cooldown (don't spam cascade alerts)
        if time.time() - last_cascade_alert > 600:  # 10 min cooldown
            last_cascade_alert = time.time()
            
            # Determine direction
            if liq_data['long_value'] > liq_data['short_value'] * 2:
                direction = 'LONG_CASCADE'  # Longs getting rekt = downward pressure
            elif liq_data['short_value'] > liq_data['long_value'] * 2:
                direction = 'SHORT_CASCADE'  # Shorts getting rekt = upward pressure
            else:
                direction = 'MIXED_CASCADE'
            
            return True, {
                'direction': direction,
                'long_value': liq_data['long_value'],
                'short_value': liq_data['short_value'],
                'long_btc': liq_data['long_btc'],
                'short_btc': liq_data['short_btc'],
                'total': liq_data['total_value']
            }
    
    return False, None

# ===== ORDER BOOK ANALYSIS =====

def analyze_order_book(current_price):
    """Analyze order book for support/resistance"""
    if not order_book['bids'] or not order_book['asks']:
        return None
    
    # Find bids below current price (support)
    support_levels = [
        {'price': b['price'], 'quantity': b['quantity']}
        for b in order_book['bids']
        if b['price'] < current_price
    ]
    
    # Find asks above current price (resistance)
    resistance_levels = [
        {'price': a['price'], 'quantity': a['quantity']}
        for a in order_book['asks']
        if a['price'] > current_price
    ]
    
    # Calculate total support/resistance
    total_support = sum(s['quantity'] for s in support_levels)
    total_resistance = sum(r['quantity'] for r in resistance_levels)
    
    # Find walls (large orders)
    support_walls = [s for s in support_levels if s['quantity'] > QUALITY_FILTERS['orderbook_wall_size']]
    resistance_walls = [r for r in resistance_levels if r['quantity'] > QUALITY_FILTERS['orderbook_wall_size']]
    
    # Get closest levels
    closest_support = max(support_levels, key=lambda x: x['price']) if support_levels else None
    closest_resistance = min(resistance_levels, key=lambda x: x['price']) if resistance_levels else None
    
    return {
        'total_support': total_support,
        'total_resistance': total_resistance,
        'support_walls': support_walls[:3],  # Top 3 walls
        'resistance_walls': resistance_walls[:3],
        'closest_support': closest_support,
        'closest_resistance': closest_resistance,
        'imbalance': total_support / (total_support + total_resistance) if (total_support + total_resistance) > 0 else 0.5
    }

# ===== ORDER FLOW MONITORING =====

def monitor_price_level(level_id, level_data, current_price):
    """Monitor order flow at a specific price level with DUAL EXCHANGE CONFIRMATION"""
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
    
    # Extract footprint data from BOTH exchanges
    range_data_bybit = {k: v for k, v in footprint_bybit.items() if range_low <= k <= range_high}
    range_data_binance = {k: v for k, v in footprint_binance.items() if range_low <= k <= range_high}
    
    if not range_data_bybit and not range_data_binance:
        return
    
    # Calculate metrics for Bybit
    total_bid_bybit = sum(d['bid'] for d in range_data_bybit.values()) if range_data_bybit else 0
    total_ask_bybit = sum(d['ask'] for d in range_data_bybit.values()) if range_data_bybit else 0
    total_volume_bybit = total_bid_bybit + total_ask_bybit
    delta_bybit = total_bid_bybit - total_ask_bybit
    bid_ratio_bybit = total_bid_bybit / total_volume_bybit if total_volume_bybit > 0 else 0.5
    
    # Calculate metrics for Binance
    total_bid_binance = sum(d['bid'] for d in range_data_binance.values()) if range_data_binance else 0
    total_ask_binance = sum(d['ask'] for d in range_data_binance.values()) if range_data_binance else 0
    total_volume_binance = total_bid_binance + total_ask_binance
    delta_binance = total_bid_binance - total_ask_binance
    bid_ratio_binance = total_bid_binance / total_volume_binance if total_volume_binance > 0 else 0.5
    
    # Combined volume check
    total_volume_combined = total_volume_bybit + total_volume_binance
    if total_volume_combined < QUALITY_FILTERS['min_volume']:
        return
    
    direction = level_data.get('direction', 'neutral')
    
    # ===== QUALITY FILTER #1: CHOP DETECTION =====
    if is_choppy_market(current_price):
        # Still check for absorption on either exchange
        is_abs_bybit, abs_vol_bybit, abs_move_bybit = detect_absorption(range_data_bybit, target_price if level_type == 'msb' else zone_low, current_price)
        is_abs_binance, abs_vol_binance, abs_move_binance = detect_absorption(range_data_binance, target_price if level_type == 'msb' else zone_low, current_price)
        
        if is_abs_bybit or is_abs_binance:
            alert_key = f"{level_id}_absorption"
            cooldown = 900
            if time.time() - last_alert_time[alert_key] > cooldown:
                level_name = get_level_name(level_type, level_data)
                exchanges = []
                if is_abs_bybit:
                    exchanges.append(f"Bybit: {abs_vol_bybit:.0f} BTC")
                if is_abs_binance:
                    exchanges.append(f"Binance: {abs_vol_binance:.0f} BTC")
                    
                msg = (
                    f"⚠️ <b>ABSORPTION DETECTED</b>\n\n"
                    f"Level: <b>{level_name}</b>\n"
                    f"Price: ${current_price:,.0f}\n\n"
                    f"{' | '.join(exchanges)}\n\n"
                    f"<i>Large orders absorbed - potential reversal</i>"
                )
                send_telegram(msg)
                last_alert_time[alert_key] = time.time()
        return
    
    # ===== QUALITY FILTER #2: DUAL EXCHANGE THRESHOLD CHECK =====
    # BOTH exchanges must meet thresholds for confirmation
    meets_bullish_bybit = (delta_bybit > QUALITY_FILTERS['min_delta'] and 
                          bid_ratio_bybit > QUALITY_FILTERS['min_bid_ratio'])
    meets_bullish_binance = (delta_binance > QUALITY_FILTERS['min_delta'] and 
                            bid_ratio_binance > QUALITY_FILTERS['min_bid_ratio'])
    
    meets_bearish_bybit = (delta_bybit < -QUALITY_FILTERS['min_delta'] and 
                          bid_ratio_bybit < QUALITY_FILTERS['max_bid_ratio'])
    meets_bearish_binance = (delta_binance < -QUALITY_FILTERS['min_delta'] and 
                            bid_ratio_binance < QUALITY_FILTERS['max_bid_ratio'])
    
    # If require_both_exchanges is True, BOTH must agree
    if QUALITY_FILTERS['require_both_exchanges']:
        meets_bullish_threshold = meets_bullish_bybit and meets_bullish_binance
        meets_bearish_threshold = meets_bearish_bybit and meets_bearish_binance
    else:
        # Either exchange showing strong signal is enough
        meets_bullish_threshold = meets_bullish_bybit or meets_bullish_binance
        meets_bearish_threshold = meets_bearish_bybit or meets_bearish_binance
    
    if not (meets_bullish_threshold or meets_bearish_threshold):
        return
    
    # ===== QUALITY FILTER #3: SUSTAINED PRESSURE ON BOTH EXCHANGES =====
    if not check_sustained_pressure(level_id, delta_bybit, delta_binance, bid_ratio_bybit, bid_ratio_binance, total_volume_combined):
        return
    
    # ===== ALL FILTERS PASSED - SEND DUAL EXCHANGE CONFIRMATION =====
    
    alert_key = f"{level_id}_confirmation"
    cooldown = 600
    
    # Get liquidation data
    liq_data = analyze_liquidations()
    
    # Get order book data
    ob_data = analyze_order_book(current_price)
    
    # BULLISH LEVEL - Strong buyers on BOTH exchanges
    if (direction == 'bullish' and meets_bullish_threshold and 
        time.time() - last_alert_time[alert_key] > cooldown):
        
        level_name = get_level_name(level_type, level_data)
        
        # Build liquidation section
        liq_section = ""
        if liq_data and liq_data['total_value'] > 10_000_000:  # >$10M
            liq_section = (
                f"\n⚡ <b>Liquidations (5min):</b>\n"
                f"Shorts: ${liq_data['short_value']/1_000_000:.1f}M ({liq_data['short_btc']:.0f} BTC)\n"
                f"Longs: ${liq_data['long_value']/1_000_000:.1f}M ({liq_data['long_btc']:.0f} BTC)"
            )
            if liq_data['short_value'] > liq_data['long_value'] * 2:
                liq_section += "\n🔥 SHORT SQUEEZE ACTIVE"
        
        # Build order book section
        ob_section = ""
        if ob_data:
            ob_section = (
                f"\n\n📖 <b>Order Book:</b>\n"
                f"Support: {ob_data['total_support']:.0f} BTC below\n"
                f"Resistance: {ob_data['total_resistance']:.0f} BTC above"
            )
            if ob_data['support_walls']:
                largest_wall = max(ob_data['support_walls'], key=lambda x: x['quantity'])
                ob_section += f"\n💪 {largest_wall['quantity']:.0f} BTC wall @ ${largest_wall['price']:,.0f}"
        
        msg = (
            f"🟢 <b>BUYERS STEPPING IN</b>\n"
            f"<b>✅ CONFIRMED ON BOTH EXCHANGES</b>\n\n"
            f"Level: <b>{level_name}</b>\n"
            f"Price: ${current_price:,.0f}\n\n"
            f"📊 <b>Bybit:</b>\n"
            f"Delta: +{delta_bybit:.0f} BTC | Bid: {bid_ratio_bybit*100:.0f}%\n"
            f"Volume: {total_volume_bybit:.0f} BTC\n\n"
            f"📊 <b>Binance:</b>\n"
            f"Delta: +{delta_binance:.0f} BTC | Bid: {bid_ratio_binance*100:.0f}%\n"
            f"Volume: {total_volume_binance:.0f} BTC"
            f"{liq_section}"
            f"{ob_section}\n\n"
            f"✅ <b>MULTI-SIGNAL CONFIRMATION</b>\n"
            f"Strong sustained demand"
        )
        send_telegram(msg)
        last_alert_time[alert_key] = time.time()
        print(f"✅ DUAL EXCHANGE ALERT: Buyers at {level_name}")
    
    # BEARISH LEVEL - Strong sellers on BOTH exchanges
    elif (direction == 'bearish' and meets_bearish_threshold and
          time.time() - last_alert_time[alert_key] > cooldown):
        
        level_name = get_level_name(level_type, level_data)
        
        # Build liquidation section
        liq_section = ""
        if liq_data and liq_data['total_value'] > 10_000_000:  # >$10M
            liq_section = (
                f"\n⚡ <b>Liquidations (5min):</b>\n"
                f"Longs: ${liq_data['long_value']/1_000_000:.1f}M ({liq_data['long_btc']:.0f} BTC)\n"
                f"Shorts: ${liq_data['short_value']/1_000_000:.1f}M ({liq_data['short_btc']:.0f} BTC)"
            )
            if liq_data['long_value'] > liq_data['short_value'] * 2:
                liq_section += "\n🔥 LONG CASCADE ACTIVE"
        
        # Build order book section
        ob_section = ""
        if ob_data:
            ob_section = (
                f"\n\n📖 <b>Order Book:</b>\n"
                f"Support: {ob_data['total_support']:.0f} BTC below\n"
                f"Resistance: {ob_data['total_resistance']:.0f} BTC above"
            )
            if ob_data['resistance_walls']:
                largest_wall = max(ob_data['resistance_walls'], key=lambda x: x['quantity'])
                ob_section += f"\n💪 {largest_wall['quantity']:.0f} BTC wall @ ${largest_wall['price']:,.0f}"
        
        msg = (
            f"🔴 <b>SELLERS STEPPING IN</b>\n"
            f"<b>✅ CONFIRMED ON BOTH EXCHANGES</b>\n\n"
            f"Level: <b>{level_name}</b>\n"
            f"Price: ${current_price:,.0f}\n\n"
            f"📊 <b>Bybit:</b>\n"
            f"Delta: {delta_bybit:.0f} BTC | Ask: {(1-bid_ratio_bybit)*100:.0f}%\n"
            f"Volume: {total_volume_bybit:.0f} BTC\n\n"
            f"📊 <b>Binance:</b>\n"
            f"Delta: {delta_binance:.0f} BTC | Ask: {(1-bid_ratio_binance)*100:.0f}%\n"
            f"Volume: {total_volume_binance:.0f} BTC"
            f"{liq_section}"
            f"{ob_section}\n\n"
            f"✅ <b>MULTI-SIGNAL CONFIRMATION</b>\n"
            f"Strong sustained supply"
        )
        send_telegram(msg)
        last_alert_time[alert_key] = time.time()
        print(f"✅ DUAL EXCHANGE ALERT: Sellers at {level_name}")

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
            
            # Update footprint
            if side == "Buy":
                footprint_bybit[price_level]['bid'] += quantity
                cumulative_delta_bybit += quantity
            else:
                footprint_bybit[price_level]['ask'] += quantity
                cumulative_delta_bybit -= quantity
            
            footprint_bybit[price_level]['trades'] += 1
            trade_count_bybit += 1
            
            # Check active levels every 50 trades
            if active_levels and trade_count_bybit % 50 == 0:
                process_active_levels(price)
            
            # Status update
            if trade_count_bybit % 1000 == 0:
                print(f"✓ Bybit: {trade_count_bybit:,} trades | Delta: {cumulative_delta_bybit:+.0f}")
    
    except Exception as e:
        print(f"❌ Bybit trade processing error: {e}")

def on_message_binance(ws, message):
    """Process Binance trades"""
    global trade_count_binance, cumulative_delta_binance
    
    try:
        data = json.loads(message)
        
        if data.get('e') != 'aggTrade':
            return
        
        price = float(data['p'])
        quantity = float(data['q'])
        is_buyer_maker = data['m']  # True = sell, False = buy
        
        price_level = round_price(price)
        
        # Update footprint
        # If buyer is maker, it's a sell order being filled
        # If buyer is taker, it's a buy order
        if not is_buyer_maker:  # Buyer is taker = BUY
            footprint_binance[price_level]['bid'] += quantity
            cumulative_delta_binance += quantity
        else:  # Buyer is maker = SELL
            footprint_binance[price_level]['ask'] += quantity
            cumulative_delta_binance -= quantity
        
        footprint_binance[price_level]['trades'] += 1
        trade_count_binance += 1
        
        # Check active levels every 50 trades
        if active_levels and trade_count_binance % 50 == 0:
            process_active_levels(price)
        
        # Status update
        if trade_count_binance % 1000 == 0:
            print(f"✓ Binance: {trade_count_binance:,} trades | Delta: {cumulative_delta_binance:+.0f}")
    
    except Exception as e:
        print(f"❌ Binance trade processing error: {e}")

def on_error_bybit(ws, error):
    print(f"❌ Bybit WebSocket error: {error}")

def on_error_binance(ws, error):
    print(f"❌ Binance WebSocket error: {error}")

def on_close_bybit(ws, close_status_code, close_msg):
    print("🔴 Bybit WebSocket closed - reconnecting in 5s...")
    time.sleep(5)
    start_websocket_bybit()

def on_close_binance(ws, close_status_code, close_msg):
    print("🔴 Binance WebSocket closed - reconnecting in 5s...")
    time.sleep(5)
    start_websocket_binance()

def on_open_bybit(ws):
    print("✅ Connected to Bybit")
    subscribe = {"op": "subscribe", "args": [f"publicTrade.{SYMBOL}"]}
    ws.send(json.dumps(subscribe))

def on_open_binance(ws):
    print("✅ Connected to Binance")
    # Binance doesn't need subscription message for aggTrade stream

def start_websocket_bybit():
    """Start Bybit WebSocket"""
    ws = websocket.WebSocketApp(
        BYBIT_WS_URL,
        on_message=on_message_bybit,
        on_error=on_error_bybit,
        on_close=on_close_bybit,
        on_open=on_open_bybit
    )
    ws.run_forever()

def start_websocket_binance():
    """Start Binance WebSocket"""
    ws = websocket.WebSocketApp(
        BINANCE_WS_URL,
        on_message=on_message_binance,
        on_error=on_error_binance,
        on_close=on_close_binance,
        on_open=on_open_binance
    )
    ws.run_forever()

def run_websocket_thread_bybit():
    """Run Bybit WebSocket in thread"""
    while True:
        try:
            start_websocket_bybit()
        except Exception as e:
            print(f"❌ Bybit WebSocket thread error: {e}")
            time.sleep(5)

def run_websocket_thread_binance():
    """Run Binance WebSocket in thread"""
    while True:
        try:
            start_websocket_binance()
        except Exception as e:
            print(f"❌ Binance WebSocket thread error: {e}")
            time.sleep(5)

# ===== LIQUIDATION WEBSOCKET =====

def on_message_liquidation(ws, message):
    """Process Binance liquidation events"""
    global liquidations
    
    try:
        data = json.loads(message)
        
        if data.get('e') != 'forceOrder':
            return
        
        order = data['o']
        
        # Add liquidation to tracking
        liquidations.append({
            'timestamp': time.time(),
            'side': order['S'],  # 'BUY' = long liquidated (forced sell), 'SELL' = short liquidated (forced buy)
            'quantity': float(order['q']),
            'price': float(order['p'])
        })
        
        # Check for cascade
        is_cascade, cascade_data = detect_liquidation_cascade(float(order['p']))
        
        if is_cascade:
            direction_text = ""
            if cascade_data['direction'] == 'SHORT_CASCADE':
                direction_text = "🔥 SHORT SQUEEZE\nShorts forced to buy"
            elif cascade_data['direction'] == 'LONG_CASCADE':
                direction_text = "🔥 LONG LIQUIDATION CASCADE\nLongs forced to sell"
            else:
                direction_text = "⚡ MIXED LIQUIDATIONS"
            
            msg = (
                f"⚡ <b>LIQUIDATION CASCADE DETECTED</b>\n\n"
                f"{direction_text}\n\n"
                f"<b>Last 5 Minutes:</b>\n"
                f"Shorts Liquidated: ${cascade_data['short_value']/1_000_000:.1f}M\n"
                f"Longs Liquidated: ${cascade_data['long_value']/1_000_000:.1f}M\n"
                f"Total: ${cascade_data['total']/1_000_000:.1f}M\n\n"
                f"<i>Cascading forced orders - expect volatility</i>"
            )
            send_telegram(msg)
            print(f"⚡ CASCADE: {cascade_data['direction']}")
    
    except Exception as e:
        print(f"❌ Liquidation processing error: {e}")

def on_error_liquidation(ws, error):
    print(f"❌ Liquidation WebSocket error: {error}")

def on_close_liquidation(ws, close_status_code, close_msg):
    print("🔴 Liquidation WebSocket closed - reconnecting in 5s...")
    time.sleep(5)
    start_websocket_liquidation()

def on_open_liquidation(ws):
    print("✅ Connected to Binance Liquidations")

def start_websocket_liquidation():
    """Start Binance liquidation WebSocket"""
    ws = websocket.WebSocketApp(
        BINANCE_LIQUIDATION_URL,
        on_message=on_message_liquidation,
        on_error=on_error_liquidation,
        on_close=on_close_liquidation,
        on_open=on_open_liquidation
    )
    ws.run_forever()

def run_websocket_thread_liquidation():
    """Run liquidation WebSocket in thread"""
    while True:
        try:
            start_websocket_liquidation()
        except Exception as e:
            print(f"❌ Liquidation WebSocket thread error: {e}")
            time.sleep(5)

# ===== ORDER BOOK WEBSOCKET =====

def on_message_orderbook(ws, message):
    """Process Binance order book updates"""
    global order_book
    
    try:
        data = json.loads(message)
        
        if 'b' not in data or 'a' not in data:
            return
        
        # Update order book
        # Format: [['price', 'quantity'], ...]
        order_book['bids'] = [
            {'price': float(b[0]), 'quantity': float(b[1])}
            for b in data['b']
            if float(b[1]) > 0  # Only active orders
        ]
        
        order_book['asks'] = [
            {'price': float(a[0]), 'quantity': float(a[1])}
            for a in data['a']
            if float(a[1]) > 0  # Only active orders
        ]
    
    except Exception as e:
        print(f"❌ Order book processing error: {e}")

def on_error_orderbook(ws, error):
    print(f"❌ Order book WebSocket error: {error}")

def on_close_orderbook(ws, close_status_code, close_msg):
    print("🔴 Order book WebSocket closed - reconnecting in 5s...")
    time.sleep(5)
    start_websocket_orderbook()

def on_open_orderbook(ws):
    print("✅ Connected to Binance Order Book")

def start_websocket_orderbook():
    """Start Binance order book WebSocket"""
    ws = websocket.WebSocketApp(
        BINANCE_ORDERBOOK_URL,
        on_message=on_message_orderbook,
        on_error=on_error_orderbook,
        on_close=on_close_orderbook,
        on_open=on_open_orderbook
    )
    ws.run_forever()

def run_websocket_thread_orderbook():
    """Run order book WebSocket in thread"""
    while True:
        try:
            start_websocket_orderbook()
        except Exception as e:
            print(f"❌ Order book WebSocket thread error: {e}")
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
        'bybit': {
            'trades_processed': trade_count_bybit,
            'cumulative_delta': cumulative_delta_bybit
        },
        'binance': {
            'trades_processed': trade_count_binance,
            'cumulative_delta': cumulative_delta_binance
        }
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
        <title>Order Flow V2.1 - Beast Mode</title>
        <style>
            body {{ font-family: monospace; background: #0a0e1a; color: #e0e0e0; padding: 20px; }}
            h1 {{ color: #00ff88; }}
            .status {{ color: #00ff88; font-weight: bold; }}
            hr {{ border-color: #2c3e50; }}
            .metric {{ margin: 5px 0; }}
        </style>
    </head>
    <body>
        <h1>⚡ Order Flow V2.1 - Beast Mode</h1>
        <p class="status">Status: ACTIVE ✅</p>
        
        <hr>
        
        <h3>📊 Trade Statistics</h3>
        <div class="metric"><b>Bybit:</b></div>
        <div class="metric">  Trades: {trade_count_bybit:,}</div>
        <div class="metric">  Delta: {cumulative_delta_bybit:+.0f} BTC</div>
        <div class="metric"><b>Binance:</b></div>
        <div class="metric">  Trades: {trade_count_binance:,}</div>
        <div class="metric">  Delta: {cumulative_delta_binance:+.0f} BTC</div>
        
        <h3>⚡ Liquidations (5min)</h3>
        {"<div class='metric'>Longs: $" + f"{analyze_liquidations()['long_value']/1_000_000:.1f}M</div><div class='metric'>Shorts: ${analyze_liquidations()['short_value']/1_000_000:.1f}M</div>" if analyze_liquidations() else "<div class='metric'>No recent liquidations</div>"}
        
        <h3>📖 Order Book</h3>
        {"<div class='metric'>Bids: " + f"{len(order_book['bids'])} levels</div><div class='metric'>Asks: {len(order_book['asks'])} levels</div>" if order_book['bids'] and order_book['asks'] else "<div class='metric'>Loading...</div>"}
        
        <h3>🎯 Active Levels</h3>
        <div class="metric">{len(active_levels)} levels monitoring</div>
        
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
print("\n" + "="*70)
print("🚀 ORDER FLOW V2.1 - BEAST MODE (FULL INSTITUTIONAL SUITE)")
print("="*70)
print(f"Symbol: {SYMBOL}")
print(f"Port: {PORT}")
print(f"\n📊 DATA SOURCES:")
print(f"  • Bybit: Order flow + Trades")
print(f"  • Binance: Order flow + Trades + Liquidations + Order Book")
print(f"\n🎯 MONITORING:")
print(f"  • MSBs, Order Blocks, Breakers")
print(f"  • Dual exchange confirmation")
print(f"  • Liquidation cascades")
print(f"  • Order book walls")
print(f"\n🔧 QUALITY FILTERS:")
print(f"  Delta: {QUALITY_FILTERS['min_delta']}+ BTC (per exchange)")
print(f"  Bid Ratio: {QUALITY_FILTERS['min_bid_ratio']*100:.0f}%+")
print(f"  Volume: {QUALITY_FILTERS['min_volume']}+ BTC (combined)")
print(f"  Sustained: {QUALITY_FILTERS['sustained_checks']} checks")
print(f"  Dual Exchange: Required on BOTH")
print(f"  Cascade Threshold: ${QUALITY_FILTERS['liquidation_cascade_threshold']/1_000_000:.0f}M")
print(f"  Order Book Wall: {QUALITY_FILTERS['orderbook_wall_size']} BTC")
print("="*70 + "\n")

# Send startup message
send_telegram(
    "🟢 <b>Order Flow V2.1 - BEAST MODE</b>\n\n"
    "Mode: Full Institutional Suite\n"
    "Exchanges: <b>Bybit + Binance</b>\n\n"
    "<b>📊 Data Streams:</b>\n"
    "✅ Order Flow (dual exchange)\n"
    "✅ Liquidations (real-time)\n"
    "✅ Order Book Depth\n"
    "✅ Cascade Detection\n\n"
    "<b>🎯 Tracking:</b>\n"
    "• Market Structure Breaks\n"
    "• Order Blocks\n"
    "• Breaker Blocks\n\n"
    "<b>🔧 Quality Filters:</b>\n"
    f"• Delta: {QUALITY_FILTERS['min_delta']}+ BTC\n"
    f"• Bid Ratio: {QUALITY_FILTERS['min_bid_ratio']*100:.0f}%+\n"
    f"• Volume: {QUALITY_FILTERS['min_volume']}+ BTC\n"
    f"• Cascade: ${QUALITY_FILTERS['liquidation_cascade_threshold']/1_000_000:.0f}M+\n"
    "• <b>✅ Multi-signal confirmation</b>\n\n"
    "<i>Institutional-grade order flow system</i>"
)

# Start ALL WebSocket threads
ws_thread_bybit = Thread(target=run_websocket_thread_bybit, daemon=True)
ws_thread_bybit.start()
print("✅ Bybit trades started")

ws_thread_binance = Thread(target=run_websocket_thread_binance, daemon=True)
ws_thread_binance.start()
print("✅ Binance trades started")

ws_thread_liquidation = Thread(target=run_websocket_thread_liquidation, daemon=True)
ws_thread_liquidation.start()
print("✅ Binance liquidations started")

ws_thread_orderbook = Thread(target=run_websocket_thread_orderbook, daemon=True)
ws_thread_orderbook.start()
print("✅ Binance order book started")

print("\n✅ All data streams active - waiting for TradingView alerts...\n")

# Run Flask app
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=PORT, debug=False)
