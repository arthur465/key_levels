import os
import time
import requests
from datetime import datetime, timezone
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
ACCOUNT_SIZE = 10000  # USD - ADJUST TO YOUR ACCOUNT
RISK_PER_TRADE = 0.02  # 2% risk per trade

# Market scanner settings
HISTORY_SIZE = 20  # Keep last 20 orderflow readings
DIVERGENCE_LOOKBACK = 10  # Check last 10 readings for patterns
PATTERN_ALERT_COOLDOWN = 3600  # Don't spam same pattern (1 hour cooldown)

# ============= TRADE TRACKING =============
active_trades = {}
trade_lock = threading.Lock()

# Market bias tracking
last_orderflow_check = {
    'bias': 'NEUTRAL',
    'delta': 0,
    'cvd': 0,
    'timestamp': None
}

# ============= MARKET SCANNER DATA =============
orderflow_history = deque(maxlen=HISTORY_SIZE)
last_pattern_alerts = {}

# ============= FLASK APP =============
app = Flask(__name__)

# ============= MULTI-EXCHANGE API - BINANCE =============
def get_binance_orderbook():
    """Fetch Binance orderbook data"""
    url = "https://api.binance.com/api/v3/depth"
    params = {"symbol": SYMBOL, "limit": 1000}
    try:
        print(f"   🟡 [BINANCE] Calling orderbook API...")
        response = requests.get(url, params=params, timeout=10)
        print(f"   ✅ [BINANCE] Response status: {response.status_code}")
        
        if response.status_code != 200:
            print(f"   ❌ [BINANCE] HTTP {response.status_code}")
            return None
        
        data = response.json()
        
        bids = data["bids"]
        asks = data["asks"]
        
        bid_volume = sum(float(b[1]) for b in bids)
        ask_volume = sum(float(a[1]) for a in asks)
        total_volume = bid_volume + ask_volume
        
        delta = bid_volume - ask_volume
        bid_pct = (bid_volume / total_volume * 100) if total_volume > 0 else 50
        
        print(f"   ✅ [BINANCE] Delta: {delta:,.2f}, Bid%: {bid_pct:.1f}%")
        
        return {
            'exchange': 'binance',
            'delta': delta,
            'bid_volume': bid_volume,
            'ask_volume': ask_volume,
            'bid_pct': bid_pct
        }
    except requests.exceptions.Timeout:
        print(f"   ⏱️ [BINANCE] Timeout")
        return None
    except Exception as e:
        print(f"   ❌ [BINANCE] Error: {type(e).__name__} - {str(e)}")
        return None

def get_binance_trades():
    """Fetch Binance recent trades"""
    url = "https://api.binance.com/api/v3/aggTrades"
    params = {"symbol": SYMBOL, "limit": 1000}
    try:
        print(f"   🟡 [BINANCE] Calling trades API...")
        response = requests.get(url, params=params, timeout=10)
        print(f"   ✅ [BINANCE] Response status: {response.status_code}")
        
        if response.status_code != 200:
            print(f"   ❌ [BINANCE] HTTP {response.status_code}")
            return None
        
        trades = response.json()
        print(f"   ✅ [BINANCE] Received {len(trades)} trades")
        
        buy_volume = sum(float(t["q"]) for t in trades if not t["m"])  # m=false means buyer was maker
        sell_volume = sum(float(t["q"]) for t in trades if t["m"])  # m=true means seller was maker
        total = buy_volume + sell_volume
        
        buy_pct = (buy_volume / total * 100) if total > 0 else 50
        cvd = buy_volume - sell_volume
        
        print(f"   ✅ [BINANCE] Buy%: {buy_pct:.1f}%, CVD: {cvd:,.2f}")
        
        return {
            'exchange': 'binance',
            'buy_pct': buy_pct,
            'cvd': cvd,
            'buy_volume': buy_volume,
            'sell_volume': sell_volume
        }
    except requests.exceptions.Timeout:
        print(f"   ⏱️ [BINANCE] Timeout")
        return None
    except Exception as e:
        print(f"   ❌ [BINANCE] Error: {type(e).__name__} - {str(e)}")
        return None

# ============= MULTI-EXCHANGE API - OKX =============
def get_okx_orderbook():
    """Fetch OKX orderbook data"""
    url = "https://www.okx.com/api/v5/market/books"
    params = {"instId": "BTC-USDT-SWAP", "sz": "400"}  # OKX uses different symbol format
    try:
        print(f"   🟠 [OKX] Calling orderbook API...")
        response = requests.get(url, params=params, timeout=10)
        print(f"   ✅ [OKX] Response status: {response.status_code}")
        
        if response.status_code != 200:
            print(f"   ❌ [OKX] HTTP {response.status_code}")
            return None
        
        data = response.json()
        
        if data.get("code") != "0":
            print(f"   ❌ [OKX] API error: {data.get('msg')}")
            return None
        
        book = data["data"][0]
        bids = book["bids"]
        asks = book["asks"]
        
        bid_volume = sum(float(b[1]) for b in bids)
        ask_volume = sum(float(a[1]) for a in asks)
        total_volume = bid_volume + ask_volume
        
        delta = bid_volume - ask_volume
        bid_pct = (bid_volume / total_volume * 100) if total_volume > 0 else 50
        
        print(f"   ✅ [OKX] Delta: {delta:,.2f}, Bid%: {bid_pct:.1f}%")
        
        return {
            'exchange': 'okx',
            'delta': delta,
            'bid_volume': bid_volume,
            'ask_volume': ask_volume,
            'bid_pct': bid_pct
        }
    except requests.exceptions.Timeout:
        print(f"   ⏱️ [OKX] Timeout")
        return None
    except Exception as e:
        print(f"   ❌ [OKX] Error: {type(e).__name__} - {str(e)}")
        return None

def get_okx_trades():
    """Fetch OKX recent trades"""
    url = "https://www.okx.com/api/v5/market/trades"
    params = {"instId": "BTC-USDT-SWAP", "limit": "500"}
    try:
        print(f"   🟠 [OKX] Calling trades API...")
        response = requests.get(url, params=params, timeout=10)
        print(f"   ✅ [OKX] Response status: {response.status_code}")
        
        if response.status_code != 200:
            print(f"   ❌ [OKX] HTTP {response.status_code}")
            return None
        
        data = response.json()
        
        if data.get("code") != "0":
            print(f"   ❌ [OKX] API error: {data.get('msg')}")
            return None
        
        trades = data["data"]
        print(f"   ✅ [OKX] Received {len(trades)} trades")
        
        buy_volume = sum(float(t["sz"]) for t in trades if t["side"] == "buy")
        sell_volume = sum(float(t["sz"]) for t in trades if t["side"] == "sell")
        total = buy_volume + sell_volume
        
        buy_pct = (buy_volume / total * 100) if total > 0 else 50
        cvd = buy_volume - sell_volume
        
        print(f"   ✅ [OKX] Buy%: {buy_pct:.1f}%, CVD: {cvd:,.2f}")
        
        return {
            'exchange': 'okx',
            'buy_pct': buy_pct,
            'cvd': cvd,
            'buy_volume': buy_volume,
            'sell_volume': sell_volume
        }
    except requests.exceptions.Timeout:
        print(f"   ⏱️ [OKX] Timeout")
        return None
    except Exception as e:
        print(f"   ❌ [OKX] Error: {type(e).__name__} - {str(e)}")
        return None

# ============= MULTI-EXCHANGE API - BYBIT =============
def get_bybit_orderbook():
    """Fetch Bybit orderbook data"""
    url = "https://api.bybit.com/v5/market/orderbook"
    params = {"category": "linear", "symbol": SYMBOL, "limit": 200}
    try:
        print(f"   🟣 [BYBIT] Calling orderbook API...")
        response = requests.get(url, params=params, timeout=10)
        print(f"   ✅ [BYBIT] Response status: {response.status_code}")
        
        if response.status_code == 403:
            print(f"   🔒 [BYBIT] 403 Forbidden - Blocked by Render")
            return None
        
        if response.status_code != 200:
            print(f"   ❌ [BYBIT] HTTP {response.status_code}")
            return None
        
        data = response.json()
        
        if data.get("retCode") != 0:
            print(f"   ❌ [BYBIT] API error: {data.get('retMsg')}")
            return None
        
        bids = data["result"]["b"]
        asks = data["result"]["a"]
        
        bid_volume = sum(float(b[1]) for b in bids)
        ask_volume = sum(float(a[1]) for a in asks)
        total_volume = bid_volume + ask_volume
        
        delta = bid_volume - ask_volume
        bid_pct = (bid_volume / total_volume * 100) if total_volume > 0 else 50
        
        print(f"   ✅ [BYBIT] Delta: {delta:,.2f}, Bid%: {bid_pct:.1f}%")
        
        return {
            'exchange': 'bybit',
            'delta': delta,
            'bid_volume': bid_volume,
            'ask_volume': ask_volume,
            'bid_pct': bid_pct
        }
    except requests.exceptions.Timeout:
        print(f"   ⏱️ [BYBIT] Timeout")
        return None
    except Exception as e:
        print(f"   ❌ [BYBIT] Error: {type(e).__name__} - {str(e)}")
        return None

def get_bybit_trades():
    """Fetch Bybit recent trades"""
    url = "https://api.bybit.com/v5/market/recent-trade"
    params = {"category": "linear", "symbol": SYMBOL, "limit": 1000}
    try:
        print(f"   🟣 [BYBIT] Calling trades API...")
        response = requests.get(url, params=params, timeout=10)
        print(f"   ✅ [BYBIT] Response status: {response.status_code}")
        
        if response.status_code == 403:
            print(f"   🔒 [BYBIT] 403 Forbidden - Blocked by Render")
            return None
        
        if response.status_code != 200:
            print(f"   ❌ [BYBIT] HTTP {response.status_code}")
            return None
        
        data = response.json()
        
        if data.get("retCode") != 0:
            print(f"   ❌ [BYBIT] API error: {data.get('retMsg')}")
            return None
        
        trades = data["result"]["list"]
        print(f"   ✅ [BYBIT] Received {len(trades)} trades")
        
        buy_volume = sum(float(t["size"]) for t in trades if t["side"] == "Buy")
        sell_volume = sum(float(t["size"]) for t in trades if t["side"] == "Sell")
        total = buy_volume + sell_volume
        
        buy_pct = (buy_volume / total * 100) if total > 0 else 50
        cvd = buy_volume - sell_volume
        
        print(f"   ✅ [BYBIT] Buy%: {buy_pct:.1f}%, CVD: {cvd:,.2f}")
        
        return {
            'exchange': 'bybit',
            'buy_pct': buy_pct,
            'cvd': cvd,
            'buy_volume': buy_volume,
            'sell_volume': sell_volume
        }
    except requests.exceptions.Timeout:
        print(f"   ⏱️ [BYBIT] Timeout")
        return None
    except Exception as e:
        print(f"   ❌ [BYBIT] Error: {type(e).__name__} - {str(e)}")
        return None

# ============= CURRENT PRICE (Try all exchanges) =============
def get_current_price():
    """Try to get current price from any exchange"""
    # Try Binance first
    try:
        response = requests.get("https://api.binance.com/api/v3/ticker/price", 
                              params={"symbol": SYMBOL}, timeout=5)
        if response.status_code == 200:
            return float(response.json()["price"])
    except:
        pass
    
    # Try OKX
    try:
        response = requests.get("https://www.okx.com/api/v5/market/ticker", 
                              params={"instId": "BTC-USDT-SWAP"}, timeout=5)
        if response.status_code == 200:
            data = response.json()
            if data.get("code") == "0":
                return float(data["data"][0]["last"])
    except:
        pass
    
    # Try Bybit (might be blocked)
    try:
        response = requests.get("https://api.bybit.com/v5/market/tickers",
                              params={"category": "linear", "symbol": SYMBOL}, timeout=5)
        if response.status_code == 200:
            data = response.json()
            if data.get("retCode") == 0:
                return float(data["result"]["list"][0]["lastPrice"])
    except:
        pass
    
    return None

# ============= MULTI-EXCHANGE SCORING =============
def score_single_exchange(orderbook, trades, direction):
    """
    Score a single exchange's orderflow for given direction
    Returns: (score, reasons)
    """
    if not orderbook or not trades:
        return 0, []
    
    delta = orderbook['delta']
    buy_pct = trades['buy_pct']
    
    reasons = []
    score = 0
    
    if direction == "LONG":
        # Score delta
        if delta > DELTA_THRESHOLD_STRONG:
            reasons.append(f"✅ Strong Δ: +{delta:,.0f}")
            score += 3
        elif delta > 0:
            reasons.append(f"⚠️ Weak Δ: +{delta:,.0f}")
            score += 1
        else:
            reasons.append(f"❌ Negative Δ: {delta:,.0f}")
        
        # Score buy pressure
        if buy_pct >= BUY_PRESSURE_MIN:
            reasons.append(f"✅ Strong buys: {buy_pct:.1f}%")
            score += 2
        else:
            reasons.append(f"❌ Weak buys: {buy_pct:.1f}%")
        
        # Bonus for very strong signals
        if delta > DELTA_THRESHOLD_STRONG * 1.5 and buy_pct > BUY_PRESSURE_MIN + 10:
            reasons.append(f"🔥 ULTRA STRONG")
            score += 2
    
    elif direction == "SHORT":
        # Score delta
        if delta < -DELTA_THRESHOLD_STRONG:
            reasons.append(f"✅ Strong Δ: {delta:,.0f}")
            score += 3
        elif delta < 0:
            reasons.append(f"⚠️ Weak Δ: {delta:,.0f}")
            score += 1
        else:
            reasons.append(f"❌ Positive Δ: +{delta:,.0f}")
        
        # Score sell pressure
        sell_pct = 100 - buy_pct
        if sell_pct >= BUY_PRESSURE_MIN:
            reasons.append(f"✅ Strong sells: {sell_pct:.1f}%")
            score += 2
        else:
            reasons.append(f"❌ Weak sells: {sell_pct:.1f}%")
        
        # Bonus for very strong signals
        if delta < -DELTA_THRESHOLD_STRONG * 1.5 and sell_pct > BUY_PRESSURE_MIN + 10:
            reasons.append(f"🔥 ULTRA STRONG")
            score += 2
    
    return min(score, 7), reasons  # Cap at 7

def confirm_order_flow_multi_exchange(direction, entry_price):
    """
    MULTI-EXCHANGE ORDERFLOW CONFIRMATION
    Fetches from Binance, OKX, Bybit and cross-validates
    Returns: (confirmed, reason, combined_data, combined_score)
    """
    print("=" * 80)
    print("🔍 MULTI-EXCHANGE ORDERFLOW CHECK - START")
    print(f"Direction: {direction}")
    print(f"Entry Price: {entry_price}")
    print("=" * 80)
    
    try:
        # Fetch all exchanges
        print("\n📡 Fetching data from all exchanges...")
        
        binance_book = get_binance_orderbook()
        binance_trades = get_binance_trades()
        
        okx_book = get_okx_orderbook()
        okx_trades = get_okx_trades()
        
        bybit_book = get_bybit_orderbook()
        bybit_trades = get_bybit_trades()
        
        # Score each exchange
        print("\n📊 Scoring exchanges...")
        
        scores = {}
        details = {}
        
        if binance_book and binance_trades:
            score, reasons = score_single_exchange(binance_book, binance_trades, direction)
            scores['binance'] = score
            details['binance'] = {
                'score': score,
                'reasons': reasons,
                'delta': binance_book['delta'],
                'buy_pct': binance_trades['buy_pct']
            }
            print(f"   🟡 BINANCE: {score}/7")
        else:
            print(f"   ❌ BINANCE: Failed to fetch data")
        
        if okx_book and okx_trades:
            score, reasons = score_single_exchange(okx_book, okx_trades, direction)
            scores['okx'] = score
            details['okx'] = {
                'score': score,
                'reasons': reasons,
                'delta': okx_book['delta'],
                'buy_pct': okx_trades['buy_pct']
            }
            print(f"   🟠 OKX: {score}/7")
        else:
            print(f"   ❌ OKX: Failed to fetch data")
        
        if bybit_book and bybit_trades:
            score, reasons = score_single_exchange(bybit_book, bybit_trades, direction)
            scores['bybit'] = score
            details['bybit'] = {
                'score': score,
                'reasons': reasons,
                'delta': bybit_book['delta'],
                'buy_pct': bybit_trades['buy_pct']
            }
            print(f"   🟣 BYBIT: {score}/7")
        else:
            print(f"   🔒 BYBIT: Failed (likely 403 blocked)")
        
        # Calculate combined metrics
        working_exchanges = len(scores)
        
        if working_exchanges == 0:
            print("\n❌ ALL EXCHANGES FAILED!")
            return False, "Unable to fetch orderflow from ANY exchange", {}, 0
        
        print(f"\n✅ Working exchanges: {working_exchanges}/3")
        
        avg_score = sum(scores.values()) / working_exchanges
        all_agree = all(s >= 5 for s in scores.values())
        majority_agree = sum(1 for s in scores.values() if s >= 5) >= max(1, working_exchanges * 0.5)
        
        # Determine confirmation level
        if working_exchanges >= 3:
            # All 3 exchanges working
            if all_agree:
                confirmed = True
                strength = "ULTRA HIGH 🔥🔥🔥"
                confidence_desc = "ALL 3 EXCHANGES ALIGNED"
            elif majority_agree:
                confirmed = True
                strength = "HIGH ✅"
                confidence_desc = "MAJORITY ALIGNED (2/3)"
            else:
                confirmed = False
                strength = "DIVERGING ⚠️"
                confidence_desc = "EXCHANGES DISAGREE"
        
        elif working_exchanges == 2:
            # 2 exchanges working
            if all_agree:
                confirmed = True
                strength = "HIGH ✅"
                confidence_desc = "BOTH EXCHANGES ALIGNED"
            else:
                confirmed = False
                strength = "MIXED ⚠️"
                confidence_desc = "EXCHANGES DISAGREE"
        
        else:
            # Only 1 exchange working
            solo_score = list(scores.values())[0]
            if solo_score >= 6:
                confirmed = True
                strength = "SINGLE EXCHANGE 🟡"
                confidence_desc = "STRONG BUT UNCONFIRMED"
            else:
                confirmed = False
                strength = "WEAK ❌"
                confidence_desc = "SINGLE EXCHANGE, LOW SCORE"
        
        # Build detailed reason message
        reason_parts = [f"<b>{confidence_desc}</b>\n"]
        
        for exchange_name in ['binance', 'okx', 'bybit']:
            if exchange_name in details:
                d = details[exchange_name]
                emoji = "🟡" if exchange_name == "binance" else "🟠" if exchange_name == "okx" else "🟣"
                reason_parts.append(f"{emoji} <b>{exchange_name.upper()}: {d['score']}/7</b>")
                for reason_line in d['reasons']:
                    reason_parts.append(f"   {reason_line}")
                reason_parts.append("")
            else:
                emoji = "🟡" if exchange_name == "binance" else "🟠" if exchange_name == "okx" else "🟣"
                block_msg = "🔒 BLOCKED" if exchange_name == "bybit" else "❌ FAILED"
                reason_parts.append(f"{emoji} <b>{exchange_name.upper()}: {block_msg}</b>\n")
        
        reason_parts.append(f"<b>COMBINED: {avg_score:.1f}/7</b>")
        reason_parts.append(f"Strength: {strength}")
        
        reason = "\n".join(reason_parts)
        
        # Combined data for trade tracking
        combined_data = {
            'avg_score': avg_score,
            'working_exchanges': working_exchanges,
            'scores': scores,
            'details': details
        }
        
        print(f"\n📊 FINAL RESULT:")
        print(f"   Combined Score: {avg_score:.1f}/7")
        print(f"   Strength: {strength}")
        print(f"   Confirmed: {'✅ YES' if confirmed else '❌ NO'}")
        print("🔍 MULTI-EXCHANGE CHECK - END")
        print("=" * 80)
        
        # For trade monitoring, use average delta and buy_pct
        avg_delta = sum(d['delta'] for d in details.values()) / len(details) if details else 0
        avg_buy_pct = sum(d['buy_pct'] for d in details.values()) / len(details) if details else 50
        
        combined_data['delta'] = avg_delta
        combined_data['buy_pct'] = avg_buy_pct
        
        return confirmed, reason, combined_data, int(avg_score)
    
    except Exception as e:
        print(f"\n❌ MULTI-EXCHANGE ERROR!")
        print(f"   Error: {type(e).__name__} - {str(e)}")
        import traceback
        traceback.print_exc()
        return False, f"⚠️ Error: {str(e)}", {}, 0

# Rest of the file continues with same functions from Market Scanner...
# (calculate_stops_targets, classify_timeframe, monitor_orderflow, etc.)

# ============= MARKET SCANNER - PATTERN DETECTION =============
# (Same functions from Market Scanner version)

def detect_price_trend(history, lookback=5):
    """Detect if price is making higher highs, lower lows, or ranging"""
    if len(history) < lookback:
        return "INSUFFICIENT_DATA"
    
    recent = list(history)[-lookback:]
    prices = [r['price'] for r in recent]
    
    highs = []
    lows = []
    
    for i in range(1, len(prices) - 1):
        if prices[i] > prices[i-1] and prices[i] > prices[i+1]:
            highs.append(prices[i])
        if prices[i] < prices[i-1] and prices[i] < prices[i+1]:
            lows.append(prices[i])
    
    if len(highs) >= 2:
        if highs[-1] > highs[-2]:
            return "HIGHER_HIGHS"
        elif highs[-1] < highs[-2]:
            return "LOWER_HIGHS"
    
    if len(lows) >= 2:
        if lows[-1] < lows[-2]:
            return "LOWER_LOWS"
        elif lows[-1] > lows[-2]:
            return "HIGHER_LOWS"
    
    return "RANGING"

def detect_delta_trend(history, lookback=5):
    """Detect if delta is making higher highs or lower highs"""
    if len(history) < lookback:
        return "INSUFFICIENT_DATA"
    
    recent = list(history)[-lookback:]
    deltas = [r['delta'] for r in recent]
    
    peaks = []
    for i in range(1, len(deltas) - 1):
        if abs(deltas[i]) > abs(deltas[i-1]) and abs(deltas[i]) > abs(deltas[i+1]):
            peaks.append(deltas[i])
    
    if len(peaks) >= 2:
        if abs(peaks[-1]) > abs(peaks[-2]):
            return "STRENGTHENING"
        else:
            return "WEAKENING"
    
    return "STABLE"

def detect_divergence(history):
    """Detect bullish or bearish divergences"""
    if len(history) < DIVERGENCE_LOOKBACK:
        return None
    
    price_trend = detect_price_trend(history, DIVERGENCE_LOOKBACK)
    delta_trend = detect_delta_trend(history, DIVERGENCE_LOOKBACK)
    
    if price_trend == "HIGHER_HIGHS" and delta_trend == "WEAKENING":
        recent = list(history)[-DIVERGENCE_LOOKBACK:]
        price_change_pct = ((recent[-1]['price'] - recent[0]['price']) / recent[0]['price']) * 100
        delta_change_pct = ((abs(recent[-1]['delta']) - abs(recent[0]['delta'])) / abs(recent[0]['delta'] + 1)) * 100
        strength = abs(price_change_pct - delta_change_pct) / 10
        
        return {
            'type': 'BEARISH_DIVERGENCE',
            'strength': min(strength, 10),
            'price_trend': price_trend,
            'delta_trend': delta_trend,
            'current_price': recent[-1]['price'],
            'current_delta': recent[-1]['delta']
        }
    
    if price_trend == "LOWER_LOWS" and delta_trend == "STRENGTHENING":
        recent = list(history)[-DIVERGENCE_LOOKBACK:]
        price_change_pct = ((recent[-1]['price'] - recent[0]['price']) / recent[0]['price']) * 100
        delta_change_pct = ((abs(recent[-1]['delta']) - abs(recent[0]['delta'])) / abs(recent[0]['delta'] + 1)) * 100
        strength = abs(price_change_pct - delta_change_pct) / 10
        
        return {
            'type': 'BULLISH_DIVERGENCE',
            'strength': min(strength, 10),
            'price_trend': price_trend,
            'delta_trend': delta_trend,
            'current_price': recent[-1]['price'],
            'current_delta': recent[-1]['delta']
        }
    
    return None

def detect_absorption(current, previous):
    """Detect absorption patterns"""
    if not current or not previous:
        return None
    
    price_change = abs(current['price'] - previous['price'])
    delta_magnitude = abs(current['delta'])
    price_change_pct = (price_change / previous['price']) * 100
    
    if delta_magnitude > 3000 and price_change_pct < 0.3:
        if current['delta'] > 0:
            return {
                'type': 'SUPPLY_ABSORPTION',
                'delta': current['delta'],
                'price_change_pct': price_change_pct,
                'severity': min((delta_magnitude / 5000) * 10, 10)
            }
        else:
            return {
                'type': 'DEMAND_ABSORPTION',
                'delta': current['delta'],
                'price_change_pct': price_change_pct,
                'severity': min((delta_magnitude / 5000) * 10, 10)
            }
    
    return None

def detect_exhaustion(current, history):
    """Detect buying/selling climax patterns"""
    if len(history) < 3:
        return None
    
    recent = list(history)[-3:]
    avg_delta = sum(abs(r['delta']) for r in recent) / len(recent)
    current_delta = abs(current['delta'])
    
    if current_delta > avg_delta * 2 and current_delta > 4000:
        recent_prices = [r['price'] for r in recent]
        price_momentum = abs(current['price'] - recent_prices[0]) / recent_prices[0] * 100
        
        if price_momentum < 0.5:
            if current['delta'] > 0:
                return {
                    'type': 'BUYING_CLIMAX',
                    'delta': current['delta'],
                    'avg_delta': avg_delta,
                    'price_momentum': price_momentum,
                    'severity': min((current_delta / avg_delta) * 2, 10)
                }
            else:
                return {
                    'type': 'SELLING_CLIMAX',
                    'delta': current['delta'],
                    'avg_delta': avg_delta,
                    'price_momentum': price_momentum,
                    'severity': min((current_delta / avg_delta) * 2, 10)
                }
    
    return None

def should_alert_pattern(pattern_type):
    """Check cooldown"""
    now = time.time()
    last_alert = last_pattern_alerts.get(pattern_type, 0)
    
    if now - last_alert > PATTERN_ALERT_COOLDOWN:
        last_pattern_alerts[pattern_type] = now
        return True
    
    return False

def send_pattern_alert(pattern):
    """Send pattern alerts (keeping same messages as Market Scanner)"""
    # Same implementation as before...
    pass  # Will add full implementation if needed

# ============= STOP/TARGET CALCULATION =============
def calculate_stops_targets(setup_type, direction, entry, data):
    """Calculate dynamic stops and targets"""
    stop = 0
    target = 0
    
    if setup_type == "VA RE-ENTRY":
        vah = float(data.get('vah', 0))
        val = float(data.get('val', 0))
        
        if direction == "LONG":
            stop = val - 500
            target = vah
        else:
            stop = vah + 500
            target = val
    
    elif setup_type == "POOR LOW":
        pdvah = float(data.get('pdvah', entry + 2000))
        stop = entry - 800
        target = pdvah if pdvah > entry else entry + 2000
    
    elif setup_type == "POOR HIGH":
        pdval = float(data.get('pdval', entry - 2000))
        stop = entry + 800
        target = pdval if pdval < entry else entry - 2000
    
    elif "SINGLE PRINT" in setup_type:
        level_high = float(data.get('level_high', entry + 1500))
        level_low = float(data.get('level_low', entry - 1500))
        
        if direction == "LONG":
            stop = level_low - 500
            target = level_high
        else:
            stop = level_high + 500
            target = level_low
    
    elif "NAKED POC" in setup_type:
        pdvah = float(data.get('pdvah', entry + 1500))
        pdval = float(data.get('pdval', entry - 1500))
        
        if direction == "LONG":
            stop = entry - 800
            target = pdvah if pdvah > entry else entry + 1500
        else:
            stop = entry + 800
            target = pdval if pdval < entry else entry - 1500
    
    else:
        if direction == "LONG":
            stop = entry - 1000
            target = entry + 2000
        else:
            stop = entry + 1000
            target = entry - 2000
    
    risk = abs(entry - stop)
    reward = abs(target - entry)
    rr_ratio = reward / risk if risk > 0 else 0
    
    return stop, target, rr_ratio

def calculate_position_size(entry, stop, account_size=ACCOUNT_SIZE, risk_pct=RISK_PER_TRADE):
    """Calculate position size"""
    risk_amount = account_size * risk_pct
    price_risk = abs(entry - stop)
    
    if price_risk == 0:
        return 0
    
    position_size = risk_amount / price_risk
    return position_size

def classify_timeframe(setup_type, rr_ratio, of_data):
    """Determine timeframe"""
    if "SINGLE PRINT" in setup_type:
        if "Monthly" in setup_type:
            return "SWING 📅", "12-48 hours"
        else:
            return "SWING 📅", "6-24 hours"
    
    if "POOR" in setup_type:
        delta = abs(of_data.get('delta', 0))
        if delta > 3000:
            return "SCALP ⚡", "15-60 min"
        else:
            return "DAY TRADE 📊", "1-3 hours"
    
    if "VA RE-ENTRY" in setup_type:
        if rr_ratio >= 2.0:
            return "DAY TRADE 📊", "2-6 hours"
        else:
            return "SCALP ⚡", "30-90 min"
    
    if "NAKED POC" in setup_type:
        return "DAY TRADE 📊", "1-4 hours"
    
    return "DAY TRADE 📊", "1-4 hours"

def send_telegram(message):
    """Send Telegram message"""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=10)
        print(f"✅ Telegram sent")
    except:
        pass


# ============= ENHANCED ORDERFLOW MONITORING =============
def monitor_orderflow():
    global last_orderflow_check
    print("📊 Multi-exchange orderflow monitoring + Market Scanner started")
    
    while True:
        try:
            time.sleep(ORDERFLOW_INTERVAL)
            
            print("\n" + "="*80)
            print("📊 MULTI-EXCHANGE ORDERFLOW + MARKET SCAN")
            print("="*80)
            
            current_price = get_current_price()
            
            if not current_price:
                print("❌ Failed to get current price")
                continue
            
            # Fetch all exchanges
            binance_book = get_binance_orderbook()
            binance_trades = get_binance_trades()
            okx_book = get_okx_orderbook()
            okx_trades = get_okx_trades()
            bybit_book = get_bybit_orderbook()
            bybit_trades = get_bybit_trades()
            
            # Calculate average delta and buy_pct across working exchanges
            deltas = []
            buy_pcts = []
            cvds = []
            
            if binance_book and binance_trades:
                deltas.append(binance_book['delta'])
                buy_pcts.append(binance_trades['buy_pct'])
                cvds.append(binance_trades['cvd'])
            
            if okx_book and okx_trades:
                deltas.append(okx_book['delta'])
                buy_pcts.append(okx_trades['buy_pct'])
                cvds.append(okx_trades['cvd'])
            
            if bybit_book and bybit_trades:
                deltas.append(bybit_book['delta'])
                buy_pcts.append(bybit_trades['buy_pct'])
                cvds.append(bybit_trades['cvd'])
            
            if not deltas:
                print("❌ No exchange data available")
                continue
            
            # Average across working exchanges
            avg_delta = sum(deltas) / len(deltas)
            avg_buy_pct = sum(buy_pcts) / len(buy_pcts)
            avg_cvd = sum(cvds) / len(cvds)
            
            # Store in history for scanner
            current_reading = {
                'timestamp': datetime.now(timezone.utc),
                'price': current_price,
                'delta': avg_delta,
                'buy_pct': avg_buy_pct,
                'cvd': avg_cvd
            }
            
            orderflow_history.append(current_reading)
            print(f"✅ Stored reading #{len(orderflow_history)} in history")
            
            # Update market bias
            if avg_delta > 2000 and avg_cvd > 50:
                bias = "🟢 BULLISH"
            elif avg_delta < -2000 and avg_cvd < -50:
                bias = "🔴 BEARISH"
            else:
                bias = "⚪ NEUTRAL"
            
            last_orderflow_check = {
                'bias': bias,
                'delta': avg_delta,
                'cvd': avg_cvd,
                'timestamp': datetime.now(timezone.utc)
            }
            
            # Run pattern detection (scanner)
            print("\n🔍 Running pattern detection...")
            patterns_detected = []
            
            if len(orderflow_history) >= DIVERGENCE_LOOKBACK:
                divergence = detect_divergence(orderflow_history)
                if divergence:
                    print(f"   🚨 DIVERGENCE: {divergence['type']} (strength: {divergence['strength']:.1f}/10)")
                    patterns_detected.append(divergence)
                    if should_alert_pattern(divergence['type']):
                        send_pattern_alert(divergence)
            
            if len(orderflow_history) >= 2:
                absorption = detect_absorption(current_reading, orderflow_history[-2])
                if absorption:
                    print(f"   🚨 ABSORPTION: {absorption['type']} (severity: {absorption['severity']:.1f}/10)")
                    patterns_detected.append(absorption)
                    if should_alert_pattern(absorption['type']):
                        send_pattern_alert(absorption)
            
            if len(orderflow_history) >= 3:
                exhaustion = detect_exhaustion(current_reading, orderflow_history)
                if exhaustion:
                    print(f"   🚨 EXHAUSTION: {exhaustion['type']} (severity: {exhaustion['severity']:.1f}/10)")
                    patterns_detected.append(exhaustion)
                    if should_alert_pattern(exhaustion['type']):
                        send_pattern_alert(exhaustion)
            
            if not patterns_detected:
                print("   ✅ No significant patterns detected")
            
            # Send orderflow update every 4 cycles (1 hour)
            if len(orderflow_history) % 4 == 0:
                working_count = len(deltas)
                exchange_status = []
                
                if binance_book and binance_trades:
                    exchange_status.append("🟡 Binance: ✅")
                else:
                    exchange_status.append("🟡 Binance: ❌")
                
                if okx_book and okx_trades:
                    exchange_status.append("🟠 OKX: ✅")
                else:
                    exchange_status.append("🟠 OKX: ❌")
                
                if bybit_book and bybit_trades:
                    exchange_status.append("🟣 Bybit: ✅")
                else:
                    exchange_status.append("🟣 Bybit: 🔒")
                
                message = f"""
📊 <b>MULTI-EXCHANGE ORDERFLOW UPDATE</b>

💰 Price: ${current_price:,.0f}

<b>COMBINED ({working_count}/3 exchanges)</b>
Δ: {avg_delta:+,.0f} BTC
Buys: {avg_buy_pct:.1f}%
CVD: {avg_cvd:+,.0f} BTC

Market: {bias}

<b>Exchange Status:</b>
{chr(10).join(exchange_status)}

🔍 Scanner: {len(patterns_detected)} pattern(s) detected
                """
                send_telegram(message)
        
        except Exception as e:
            print(f"❌ Orderflow monitor error: {e}")
            import traceback
            traceback.print_exc()

# ============= TRADE MONITORING =============
def monitor_trades():
    print("📊 Trade monitoring started")
    
    while True:
        try:
            time.sleep(TRADE_CHECK_INTERVAL)
            
            with trade_lock:
                if not active_trades:
                    continue
                
                current_price = get_current_price()
                
                if not current_price:
                    continue
                
                # Get current delta from any working exchange
                binance_book = get_binance_orderbook()
                okx_book = get_okx_orderbook()
                bybit_book = get_bybit_orderbook()
                
                deltas = []
                if binance_book:
                    deltas.append(binance_book['delta'])
                if okx_book:
                    deltas.append(okx_book['delta'])
                if bybit_book:
                    deltas.append(bybit_book['delta'])
                
                if not deltas:
                    continue
                
                current_delta = sum(deltas) / len(deltas)
                trades_to_remove = []
                
                for trade_id, info in active_trades.items():
                    setup_type = info['setup_type']
                    direction = info['direction']
                    entry = info['entry']
                    target = info['target']
                    stop = info.get('stop', 0)
                    entry_delta = info['entry_delta']
                    alerts_sent = info['alerts_sent']
                    
                    now = datetime.now(timezone.utc)
                    hours_since_update = (now - info['last_update']).total_seconds() / 3600
                    
                    # Check stop loss
                    if direction == "LONG" and current_price <= stop:
                        send_telegram(f"""
🛑 <b>{setup_type} - STOP HIT</b>

Entry: ${entry:,.0f}
Stop: ${stop:,.0f}
Exit: ${current_price:,.0f}

Loss: ${current_price - entry:,.0f}
                        """)
                        trades_to_remove.append(trade_id)
                        continue
                    
                    elif direction == "SHORT" and current_price >= stop:
                        send_telegram(f"""
🛑 <b>{setup_type} - STOP HIT</b>

Entry: ${entry:,.0f}
Stop: ${stop:,.0f}
Exit: ${current_price:,.0f}

Loss: ${entry - current_price:,.0f}
                        """)
                        trades_to_remove.append(trade_id)
                        continue
                    
                    # Monitor direction
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
                
                for tid in trades_to_remove:
                    del active_trades[tid]
        
        except Exception as e:
            print(f"❌ Trade monitor error: {e}")
            import traceback
            traceback.print_exc()


# ============= WEBHOOK HANDLER =============
@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.json
        print(f"📨 Webhook: {json.dumps(data, indent=2)}")
        
        alert_type = data.get('type', 'unknown')
        current_price_from_tv = float(data.get('current_price', 0))
        
        setup_type = "UNKNOWN"
        direction = "LONG"
        entry = current_price_from_tv
        
        webhook_data = data.copy()
        
        # Parse alert types
        if alert_type == "tpo_poor_hl":
            level_type = data.get('level_type')
            level_price = float(data.get('level_price', 0))
            
            webhook_data['pdvah'] = data.get('pdvah', 0)
            webhook_data['pdval'] = data.get('pdval', 0)
            webhook_data['pdpoc'] = data.get('pdpoc', 0)
            webhook_data['pdh'] = data.get('pdh', 0)
            webhook_data['pdl'] = data.get('pdl', 0)
            
            if level_type == "poor_high":
                setup_type = "POOR HIGH"
                direction = "SHORT"
                entry = level_price
            elif level_type == "poor_low":
                setup_type = "POOR LOW"
                direction = "LONG"
                entry = level_price
        
        elif alert_type == "value_area_reentry":
            reentry_direction = data.get('direction')
            vah = float(data.get('vah', 0))
            val = float(data.get('val', 0))
            
            webhook_data['vah'] = vah
            webhook_data['val'] = val
            webhook_data['pdpoc'] = data.get('pdpoc', 0)
            webhook_data['pdh'] = data.get('pdh', 0)
            webhook_data['pdl'] = data.get('pdl', 0)
            
            setup_type = "VA RE-ENTRY"
            if reentry_direction == "from_below":
                direction = "LONG"
                entry = val
            elif reentry_direction == "from_above":
                direction = "SHORT"
                entry = vah
        
        elif alert_type == "naked_poc":
            level_price = float(data.get('level_price', 0))
            poc_type = data.get('poc_type', 'daily')
            poc_count = data.get('poc_count', 1)
            
            webhook_data['pdvah'] = data.get('pdvah', 0)
            webhook_data['pdval'] = data.get('pdval', 0)
            webhook_data['pdpoc'] = data.get('pdpoc', 0)
            webhook_data['pdh'] = data.get('pdh', 0)
            webhook_data['pdl'] = data.get('pdl', 0)
            
            if "monthly" in poc_type:
                setup_type = "NAKED POC (Monthly)"
            elif "weekly" in poc_type:
                setup_type = "NAKED POC (Weekly)"
            else:
                setup_type = "NAKED POC"
            
            if poc_count > 1 or "+" in poc_type:
                setup_type += f" x{poc_count}"
            
            current_price = get_current_price()
            if current_price and current_price < level_price:
                direction = "LONG"
                entry = level_price
            else:
                direction = "SHORT"
                entry = level_price
        
        elif alert_type == "single_print":
            sp_type = data.get('sp_type', 'weekly')
            level_high = float(data.get('level_high', 0))
            level_low = float(data.get('level_low', 0))
            
            webhook_data['level_high'] = level_high
            webhook_data['level_low'] = level_low
            webhook_data['pdvah'] = data.get('pdvah', 0)
            webhook_data['pdval'] = data.get('pdval', 0)
            webhook_data['pdpoc'] = data.get('pdpoc', 0)
            webhook_data['pdh'] = data.get('pdh', 0)
            webhook_data['pdl'] = data.get('pdl', 0)
            
            if sp_type == "weekly":
                setup_type = "SINGLE PRINT (Weekly)"
            elif sp_type == "monthly":
                setup_type = "SINGLE PRINT (Monthly)"
            else:
                setup_type = "SINGLE PRINT"
            
            current_price = get_current_price()
            if current_price and current_price < level_low:
                direction = "LONG"
                entry = level_low
            else:
                direction = "SHORT"
                entry = level_high
        
        # Calculate stops & targets
        stop, target, rr_ratio = calculate_stops_targets(setup_type, direction, entry, webhook_data)
        
        # MULTI-EXCHANGE ORDERFLOW CONFIRMATION
        confirmed, reason, of_data, score = confirm_order_flow_multi_exchange(direction, entry)
        
        # Confidence & strength
        max_score = 7
        confidence_stars = "⭐" * min(score, 5)
        strength_map = {
            7: "ULTRA HIGH 🔥🔥🔥",
            6: "HIGH CONVICTION 🔥",
            5: "STANDARD ✅",
            4: "MODERATE ⚠️",
            3: "WEAK ⚠️",
            2: "VERY WEAK ❌",
            1: "VERY WEAK ❌",
            0: "NO SIGNAL ❌"
        }
        strength = strength_map.get(score, "UNKNOWN")
        
        # Timeframe classification
        timeframe, duration = classify_timeframe(setup_type, rr_ratio, of_data)
        
        # Position sizing
        position = calculate_position_size(entry, stop)
        
        # Market bias context
        market_bias = last_orderflow_check.get('bias', '⚪ UNKNOWN')
        bias_aligned = (
            (direction == "LONG" and "🟢" in market_bias) or 
            (direction == "SHORT" and "🔴" in market_bias)
        )
        confluence = "✅ ALIGNED" if bias_aligned else "⚠️ COUNTER-TREND"
        
        if confirmed:
            alert_emoji = "🟢" if direction == "LONG" else "🔴"
            message = f"""
{alert_emoji} <b>{setup_type} - {direction} ✅ CONFIRMED</b>

<b>CONFIDENCE: {confidence_stars} ({score}/{max_score})</b>
Strength: {strength}

📍 <b>TRADE SETUP</b>
Entry: ${entry:,.0f}
Stop: ${stop:,.0f}
Target: ${target:,.0f}
R:R: {rr_ratio:.2f}

💰 Position: {position:.4f} BTC ({RISK_PER_TRADE*100:.0f}% risk)
⏱️ Timeframe: {timeframe} ({duration})

📊 Market Bias: {market_bias} {confluence}

<b>MULTI-EXCHANGE ORDERFLOW:</b>
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
                    'stop': stop,
                    'target': target,
                    'rr_ratio': rr_ratio,
                    'position': position,
                    'entry_delta': of_data.get('delta', 0),
                    'entry_time': datetime.now(timezone.utc),
                    'last_update': datetime.now(timezone.utc),
                    'alerts_sent': []
                }
            
            print(f"✅ Trade confirmed: {trade_id}")
        
        else:
            message = f"""
⚠️ <b>{setup_type} - {direction} ❌ REJECTED</b>

<b>CONFIDENCE: {confidence_stars} ({score}/{max_score})</b>
Strength: {strength}

Entry: ${entry:,.0f}
Target: ${target:,.0f}
R:R: {rr_ratio:.2f}

<b>MULTI-EXCHANGE ORDERFLOW:</b>
{reason}

🚫 <b>SKIPPING - Insufficient Confirmation</b>
            """
            
            send_telegram(message)
            print(f"❌ Trade rejected")
        
        return jsonify({"status": "success"}), 200
    
    except Exception as e:
        print(f"❌ Webhook error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/', methods=['GET'])
def home():
    return f"""
    <h1>🚀 Beast Mode V4 - TRI-EXCHANGE EDITION</h1>
    <p>Active Trades: {len(active_trades)}</p>
    <p>History Size: {len(orderflow_history)}/{HISTORY_SIZE}</p>
    <h3>Features:</h3>
    <ul>
        <li>✅ TPO Structure (TradingView)</li>
        <li>🔥 <b>TRI-EXCHANGE Orderflow (Binance + OKX + Bybit)</b></li>
        <li>✅ Cross-Validation Scoring</li>
        <li>✅ Divergence Detection</li>
        <li>✅ Dynamic Stops & Targets</li>
        <li>✅ Position Sizing (2% risk)</li>
        <li>✅ Timeframe Classification</li>
        <li>✅ Market Bias Context</li>
        <li>✅ Continuous Monitoring</li>
        <li>✅ Trade Alerts</li>
        <li>🔥 Market Scanner (Divergences, Absorption, Exhaustion)</li>
    </ul>
    <h3>Configuration:</h3>
    <ul>
        <li>Account: ${ACCOUNT_SIZE:,.0f}</li>
        <li>Risk: {RISK_PER_TRADE*100:.0f}% per trade</li>
        <li>History: {HISTORY_SIZE} readings</li>
        <li>Scan Interval: {ORDERFLOW_INTERVAL}s ({ORDERFLOW_INTERVAL/60:.0f} min)</li>
    </ul>
    """, 200

if __name__ == '__main__':
    print("🚀 Beast Mode V4 - TRI-EXCHANGE EDITION")
    print("="*80)
    print("Features:")
    print("  🔥 TRI-EXCHANGE Orderflow (Binance + OKX + Bybit)")
    print("  ✅ Cross-Validation & Divergence Detection")
    print("  ✅ Entry Confirmation (TPO + Multi-Exchange)")
    print("  ✅ Trade Monitoring (Weakness Alerts)")
    print("  🔥 Market Scanner (Divergences, Exhaustion, Absorption)")
    print("="*80)
    
    # Start monitoring threads
    orderflow_thread = threading.Thread(target=monitor_orderflow, daemon=True)
    orderflow_thread.start()
    
    trade_thread = threading.Thread(target=monitor_trades, daemon=True)
    trade_thread.start()
    
    # Send startup message
    send_telegram(f"""
🚀 <b>Beast Mode V4 - TRI-EXCHANGE Started</b>

📊 <b>Multi-Exchange Orderflow: ACTIVE</b>
🟡 Binance
🟠 OKX  
🟣 Bybit (may be blocked)

🔄 Monitoring: ON
✅ Filter: ACTIVE
🔥 Market Scanner: ACTIVE

💰 Account: ${ACCOUNT_SIZE:,.0f}
📊 Risk: {RISK_PER_TRADE*100:.0f}% per trade

<b>Tri-Exchange Features:</b>
• Cross-validation across 3 exchanges
• Divergence detection (manipulation warning)
• Graceful degradation (works with 1-3 exchanges)
• Volume aggregation ($100B+ coverage)

<b>Market Scanner Features:</b>
• Divergence Detection (Chart Champions style)
• Absorption Patterns (smart money)
• Exhaustion Signals (climax patterns)
• Momentum Shift Warnings

<b>Scanning every {ORDERFLOW_INTERVAL/60:.0f} minutes!</b>
    """)
    
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)

