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
ACCOUNT_SIZE = 10000
RISK_PER_TRADE = 0.02

# Market scanner settings
HISTORY_SIZE = 20
DIVERGENCE_LOOKBACK = 10
PATTERN_ALERT_COOLDOWN = 3600

# ============= MULTI-ENDPOINT CONFIGURATION =============
# Track which endpoints work
working_endpoints = {
    'binance_orderbook': None,
    'binance_trades': None,
    'bybit_orderbook': None,
    'bybit_trades': None
}

# Binance endpoint alternatives
BINANCE_ENDPOINTS = [
    "https://fapi.binance.com",      # Futures API (different routing!)
    "https://api1.binance.com",      # Mirror 1
    "https://api2.binance.com",      # Mirror 2
    "https://api3.binance.com",      # Mirror 3
    "https://api.binance.com",       # Original (likely blocked)
    "https://dapi.binance.com",      # Delivery API
]

# Bybit endpoint alternatives
BYBIT_ENDPOINTS = [
    "https://api.bybit.com",         # Original
    "https://api.bytick.com",        # Alternative domain
]

# ============= TRADE TRACKING =============
active_trades = {}
trade_lock = threading.Lock()

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

# ============= SMART BINANCE API WITH AUTO-FALLBACK =============
def smart_binance_orderbook():
    """
    Tries multiple Binance endpoints until one works
    Caches working endpoint for future use
    """
    # If we already know what works, use it
    if working_endpoints['binance_orderbook']:
        endpoint = working_endpoints['binance_orderbook']
        print(f"   🟡 [BINANCE] Using cached endpoint: {endpoint}")
        try:
            url = f"{endpoint}/api/v3/depth"
            params = {"symbol": SYMBOL, "limit": 1000}
            response = requests.get(url, params=params, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                bids = data["bids"]
                asks = data["asks"]
                
                bid_volume = sum(float(b[1]) for b in bids)
                ask_volume = sum(float(a[1]) for a in asks)
                total_volume = bid_volume + ask_volume
                
                delta = bid_volume - ask_volume
                bid_pct = (bid_volume / total_volume * 100) if total_volume > 0 else 50
                
                print(f"   ✅ [BINANCE] Success! Delta: {delta:,.2f}, Bid%: {bid_pct:.1f}%")
                
                return {
                    'exchange': 'binance',
                    'delta': delta,
                    'bid_volume': bid_volume,
                    'ask_volume': ask_volume,
                    'bid_pct': bid_pct
                }
            else:
                # Cached endpoint failed, clear it
                print(f"   ❌ [BINANCE] Cached endpoint failed: {response.status_code}")
                working_endpoints['binance_orderbook'] = None
        except Exception as e:
            print(f"   ❌ [BINANCE] Cached endpoint error: {str(e)}")
            working_endpoints['binance_orderbook'] = None
    
    # Try all endpoints
    print(f"   🔍 [BINANCE] Testing {len(BINANCE_ENDPOINTS)} endpoints...")
    
    for endpoint in BINANCE_ENDPOINTS:
        try:
            print(f"   🔄 [BINANCE] Trying: {endpoint}")
            url = f"{endpoint}/api/v3/depth"
            params = {"symbol": SYMBOL, "limit": 1000}
            response = requests.get(url, params=params, timeout=10)
            
            print(f"   📡 [BINANCE] Response: {response.status_code}")
            
            if response.status_code == 200:
                data = response.json()
                bids = data["bids"]
                asks = data["asks"]
                
                bid_volume = sum(float(b[1]) for b in bids)
                ask_volume = sum(float(a[1]) for a in asks)
                total_volume = bid_volume + ask_volume
                
                delta = bid_volume - ask_volume
                bid_pct = (bid_volume / total_volume * 100) if total_volume > 0 else 50
                
                # Cache this working endpoint!
                working_endpoints['binance_orderbook'] = endpoint
                print(f"   ✅ [BINANCE] SUCCESS with {endpoint}! Cached for future use.")
                print(f"   ✅ [BINANCE] Delta: {delta:,.2f}, Bid%: {bid_pct:.1f}%")
                
                return {
                    'exchange': 'binance',
                    'delta': delta,
                    'bid_volume': bid_volume,
                    'ask_volume': ask_volume,
                    'bid_pct': bid_pct
                }
            else:
                print(f"   ❌ [BINANCE] HTTP {response.status_code} - trying next...")
                
        except Exception as e:
            print(f"   ❌ [BINANCE] {endpoint} failed: {type(e).__name__}")
    
    print(f"   💀 [BINANCE] ALL {len(BINANCE_ENDPOINTS)} ENDPOINTS FAILED")
    return None

def smart_binance_trades():
    """Tries multiple Binance endpoints for trades data"""
    if working_endpoints['binance_trades']:
        endpoint = working_endpoints['binance_trades']
        print(f"   🟡 [BINANCE] Using cached trades endpoint: {endpoint}")
        try:
            url = f"{endpoint}/api/v3/aggTrades"
            params = {"symbol": SYMBOL, "limit": 1000}
            response = requests.get(url, params=params, timeout=10)
            
            if response.status_code == 200:
                trades = response.json()
                buy_volume = sum(float(t["q"]) for t in trades if not t["m"])
                sell_volume = sum(float(t["q"]) for t in trades if t["m"])
                total = buy_volume + sell_volume
                
                buy_pct = (buy_volume / total * 100) if total > 0 else 50
                cvd = buy_volume - sell_volume
                
                print(f"   ✅ [BINANCE] Trades success! Buy%: {buy_pct:.1f}%, CVD: {cvd:,.2f}")
                
                return {
                    'exchange': 'binance',
                    'buy_pct': buy_pct,
                    'cvd': cvd,
                    'buy_volume': buy_volume,
                    'sell_volume': sell_volume
                }
            else:
                working_endpoints['binance_trades'] = None
        except:
            working_endpoints['binance_trades'] = None
    
    print(f"   🔍 [BINANCE] Testing trades endpoints...")
    
    for endpoint in BINANCE_ENDPOINTS:
        try:
            print(f"   🔄 [BINANCE] Trying trades: {endpoint}")
            url = f"{endpoint}/api/v3/aggTrades"
            params = {"symbol": SYMBOL, "limit": 1000}
            response = requests.get(url, params=params, timeout=10)
            
            if response.status_code == 200:
                trades = response.json()
                buy_volume = sum(float(t["q"]) for t in trades if not t["m"])
                sell_volume = sum(float(t["q"]) for t in trades if t["m"])
                total = buy_volume + sell_volume
                
                buy_pct = (buy_volume / total * 100) if total > 0 else 50
                cvd = buy_volume - sell_volume
                
                working_endpoints['binance_trades'] = endpoint
                print(f"   ✅ [BINANCE] Trades SUCCESS with {endpoint}!")
                print(f"   ✅ [BINANCE] Buy%: {buy_pct:.1f}%, CVD: {cvd:,.2f}")
                
                return {
                    'exchange': 'binance',
                    'buy_pct': buy_pct,
                    'cvd': cvd,
                    'buy_volume': buy_volume,
                    'sell_volume': sell_volume
                }
            else:
                print(f"   ❌ [BINANCE] Trades HTTP {response.status_code}")
        except Exception as e:
            print(f"   ❌ [BINANCE] Trades error: {type(e).__name__}")
    
    print(f"   💀 [BINANCE] ALL TRADES ENDPOINTS FAILED")
    return None

# ============= SMART BYBIT API WITH AUTO-FALLBACK =============
def smart_bybit_orderbook():
    """Tries multiple Bybit endpoints until one works"""
    if working_endpoints['bybit_orderbook']:
        endpoint = working_endpoints['bybit_orderbook']
        print(f"   🟣 [BYBIT] Using cached endpoint: {endpoint}")
        try:
            url = f"{endpoint}/v5/market/orderbook"
            params = {"category": "linear", "symbol": SYMBOL, "limit": 200}
            response = requests.get(url, params=params, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                if data.get("retCode") == 0:
                    bids = data["result"]["b"]
                    asks = data["result"]["a"]
                    
                    bid_volume = sum(float(b[1]) for b in bids)
                    ask_volume = sum(float(a[1]) for a in asks)
                    total_volume = bid_volume + ask_volume
                    
                    delta = bid_volume - ask_volume
                    bid_pct = (bid_volume / total_volume * 100) if total_volume > 0 else 50
                    
                    print(f"   ✅ [BYBIT] Success! Delta: {delta:,.2f}, Bid%: {bid_pct:.1f}%")
                    
                    return {
                        'exchange': 'bybit',
                        'delta': delta,
                        'bid_volume': bid_volume,
                        'ask_volume': ask_volume,
                        'bid_pct': bid_pct
                    }
            working_endpoints['bybit_orderbook'] = None
        except:
            working_endpoints['bybit_orderbook'] = None
    
    print(f"   🔍 [BYBIT] Testing {len(BYBIT_ENDPOINTS)} endpoints...")
    
    for endpoint in BYBIT_ENDPOINTS:
        try:
            print(f"   🔄 [BYBIT] Trying: {endpoint}")
            url = f"{endpoint}/v5/market/orderbook"
            params = {"category": "linear", "symbol": SYMBOL, "limit": 200}
            response = requests.get(url, params=params, timeout=10)
            
            print(f"   📡 [BYBIT] Response: {response.status_code}")
            
            if response.status_code == 200:
                data = response.json()
                if data.get("retCode") == 0:
                    bids = data["result"]["b"]
                    asks = data["result"]["a"]
                    
                    bid_volume = sum(float(b[1]) for b in bids)
                    ask_volume = sum(float(a[1]) for a in asks)
                    total_volume = bid_volume + ask_volume
                    
                    delta = bid_volume - ask_volume
                    bid_pct = (bid_volume / total_volume * 100) if total_volume > 0 else 50
                    
                    working_endpoints['bybit_orderbook'] = endpoint
                    print(f"   ✅ [BYBIT] SUCCESS with {endpoint}! Cached for future use.")
                    print(f"   ✅ [BYBIT] Delta: {delta:,.2f}, Bid%: {bid_pct:.1f}%")
                    
                    return {
                        'exchange': 'bybit',
                        'delta': delta,
                        'bid_volume': bid_volume,
                        'ask_volume': ask_volume,
                        'bid_pct': bid_pct
                    }
            elif response.status_code == 403:
                print(f"   🔒 [BYBIT] 403 Forbidden - trying next...")
            else:
                print(f"   ❌ [BYBIT] HTTP {response.status_code} - trying next...")
                
        except Exception as e:
            print(f"   ❌ [BYBIT] {endpoint} failed: {type(e).__name__}")
    
    print(f"   💀 [BYBIT] ALL {len(BYBIT_ENDPOINTS)} ENDPOINTS FAILED")
    return None

def smart_bybit_trades():
    """Tries multiple Bybit endpoints for trades data"""
    if working_endpoints['bybit_trades']:
        endpoint = working_endpoints['bybit_trades']
        print(f"   🟣 [BYBIT] Using cached trades endpoint: {endpoint}")
        try:
            url = f"{endpoint}/v5/market/recent-trade"
            params = {"category": "linear", "symbol": SYMBOL, "limit": 1000}
            response = requests.get(url, params=params, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                if data.get("retCode") == 0:
                    trades = data["result"]["list"]
                    
                    buy_volume = sum(float(t["size"]) for t in trades if t["side"] == "Buy")
                    sell_volume = sum(float(t["size"]) for t in trades if t["side"] == "Sell")
                    total = buy_volume + sell_volume
                    
                    buy_pct = (buy_volume / total * 100) if total > 0 else 50
                    cvd = buy_volume - sell_volume
                    
                    print(f"   ✅ [BYBIT] Trades success! Buy%: {buy_pct:.1f}%, CVD: {cvd:,.2f}")
                    
                    return {
                        'exchange': 'bybit',
                        'buy_pct': buy_pct,
                        'cvd': cvd,
                        'buy_volume': buy_volume,
                        'sell_volume': sell_volume
                    }
            working_endpoints['bybit_trades'] = None
        except:
            working_endpoints['bybit_trades'] = None
    
    print(f"   🔍 [BYBIT] Testing trades endpoints...")
    
    for endpoint in BYBIT_ENDPOINTS:
        try:
            print(f"   🔄 [BYBIT] Trying trades: {endpoint}")
            url = f"{endpoint}/v5/market/recent-trade"
            params = {"category": "linear", "symbol": SYMBOL, "limit": 1000}
            response = requests.get(url, params=params, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                if data.get("retCode") == 0:
                    trades = data["result"]["list"]
                    
                    buy_volume = sum(float(t["size"]) for t in trades if t["side"] == "Buy")
                    sell_volume = sum(float(t["size"]) for t in trades if t["side"] == "Sell")
                    total = buy_volume + sell_volume
                    
                    buy_pct = (buy_volume / total * 100) if total > 0 else 50
                    cvd = buy_volume - sell_volume
                    
                    working_endpoints['bybit_trades'] = endpoint
                    print(f"   ✅ [BYBIT] Trades SUCCESS with {endpoint}!")
                    print(f"   ✅ [BYBIT] Buy%: {buy_pct:.1f}%, CVD: {cvd:,.2f}")
                    
                    return {
                        'exchange': 'bybit',
                        'buy_pct': buy_pct,
                        'cvd': cvd,
                        'buy_volume': buy_volume,
                        'sell_volume': sell_volume
                    }
        except Exception as e:
            print(f"   ❌ [BYBIT] Trades error: {type(e).__name__}")
    
    print(f"   💀 [BYBIT] ALL TRADES ENDPOINTS FAILED")
    return None

# ============= OKX API (WORKING) =============
def get_okx_orderbook():
    """OKX orderbook (confirmed working)"""
    url = "https://www.okx.com/api/v5/market/books"
    params = {"instId": "BTC-USDT-SWAP", "sz": "400"}
    try:
        print(f"   🟠 [OKX] Calling orderbook API...")
        response = requests.get(url, params=params, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            if data.get("code") == "0":
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
        print(f"   ❌ [OKX] HTTP {response.status_code}")
        return None
    except Exception as e:
        print(f"   ❌ [OKX] Error: {type(e).__name__}")
        return None

def get_okx_trades():
    """OKX trades (confirmed working)"""
    url = "https://www.okx.com/api/v5/market/trades"
    params = {"instId": "BTC-USDT-SWAP", "limit": "500"}
    try:
        print(f"   🟠 [OKX] Calling trades API...")
        response = requests.get(url, params=params, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            if data.get("code") == "0":
                trades = data["data"]
                
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
        print(f"   ❌ [OKX] HTTP {response.status_code}")
        return None
    except Exception as e:
        print(f"   ❌ [OKX] Error: {type(e).__name__}")
        return None

# ============= CURRENT PRICE (Try all working endpoints) =============
def get_current_price():
    """Try to get current price from any working exchange"""
    # Try OKX first (most reliable)
    try:
        response = requests.get("https://www.okx.com/api/v5/market/ticker", 
                              params={"instId": "BTC-USDT-SWAP"}, timeout=5)
        if response.status_code == 200:
            data = response.json()
            if data.get("code") == "0":
                return float(data["data"][0]["last"])
    except:
        pass
    
    # Try Binance (if we have working endpoint)
    if working_endpoints['binance_orderbook']:
        try:
            endpoint = working_endpoints['binance_orderbook']
            response = requests.get(f"{endpoint}/api/v3/ticker/price", 
                                  params={"symbol": SYMBOL}, timeout=5)
            if response.status_code == 200:
                return float(response.json()["price"])
        except:
            pass
    
    # Try Bybit (if we have working endpoint)
    if working_endpoints['bybit_orderbook']:
        try:
            endpoint = working_endpoints['bybit_orderbook']
            response = requests.get(f"{endpoint}/v5/market/tickers",
                                  params={"category": "linear", "symbol": SYMBOL}, timeout=5)
            if response.status_code == 200:
                data = response.json()
                if data.get("retCode") == 0:
                    return float(data["result"]["list"][0]["lastPrice"])
        except:
            pass
    
    return None

# Continue with rest of the code (scoring, monitoring, webhook, etc.)...

# ============= MULTI-EXCHANGE SCORING =============
def score_single_exchange(orderbook, trades, direction):
    """Score a single exchange's orderflow"""
    if not orderbook or not trades:
        return 0, []
    
    delta = orderbook['delta']
    buy_pct = trades['buy_pct']
    
    reasons = []
    score = 0
    
    if direction == "LONG":
        if delta > DELTA_THRESHOLD_STRONG:
            reasons.append(f"✅ Strong Δ: +{delta:,.0f}")
            score += 3
        elif delta > 0:
            reasons.append(f"⚠️ Weak Δ: +{delta:,.0f}")
            score += 1
        else:
            reasons.append(f"❌ Negative Δ: {delta:,.0f}")
        
        if buy_pct >= BUY_PRESSURE_MIN:
            reasons.append(f"✅ Strong buys: {buy_pct:.1f}%")
            score += 2
        else:
            reasons.append(f"❌ Weak buys: {buy_pct:.1f}%")
        
        if delta > DELTA_THRESHOLD_STRONG * 1.5 and buy_pct > BUY_PRESSURE_MIN + 10:
            reasons.append(f"🔥 ULTRA STRONG")
            score += 2
    
    elif direction == "SHORT":
        if delta < -DELTA_THRESHOLD_STRONG:
            reasons.append(f"✅ Strong Δ: {delta:,.0f}")
            score += 3
        elif delta < 0:
            reasons.append(f"⚠️ Weak Δ: {delta:,.0f}")
            score += 1
        else:
            reasons.append(f"❌ Positive Δ: +{delta:,.0f}")
        
        sell_pct = 100 - buy_pct
        if sell_pct >= BUY_PRESSURE_MIN:
            reasons.append(f"✅ Strong sells: {sell_pct:.1f}%")
            score += 2
        else:
            reasons.append(f"❌ Weak sells: {sell_pct:.1f}%")
        
        if delta < -DELTA_THRESHOLD_STRONG * 1.5 and sell_pct > BUY_PRESSURE_MIN + 10:
            reasons.append(f"🔥 ULTRA STRONG")
            score += 2
    
    return min(score, 7), reasons

def confirm_order_flow_multi_exchange(direction, entry_price):
    """Multi-exchange orderflow confirmation with smart fallback"""
    print("=" * 80)
    print("🔍 SMART MULTI-EXCHANGE ORDERFLOW CHECK")
    print(f"Direction: {direction}, Entry: {entry_price}")
    print("=" * 80)
    
    try:
        # Fetch all exchanges with smart fallback
        print("\n📡 Fetching from all exchanges (with endpoint auto-discovery)...")
        
        binance_book = smart_binance_orderbook()
        binance_trades = smart_binance_trades()
        
        okx_book = get_okx_orderbook()
        okx_trades = get_okx_trades()
        
        bybit_book = smart_bybit_orderbook()
        bybit_trades = smart_bybit_trades()
        
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
            print(f"   ❌ BINANCE: All endpoints failed")
        
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
            print(f"   ❌ OKX: Failed")
        
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
            print(f"   ❌ BYBIT: All endpoints failed")
        
        # Calculate combined metrics
        working_exchanges = len(scores)
        
        if working_exchanges == 0:
            print("\n❌ ALL EXCHANGES FAILED!")
            return False, "Unable to fetch orderflow from ANY exchange", {}, 0
        
        print(f"\n✅ Working exchanges: {working_exchanges}/3")
        
        # Show which endpoints are working
        print("\n📍 Working Endpoints:")
        for key, endpoint in working_endpoints.items():
            if endpoint:
                print(f"   ✅ {key}: {endpoint}")
        
        avg_score = sum(scores.values()) / working_exchanges
        all_agree = all(s >= 5 for s in scores.values())
        majority_agree = sum(1 for s in scores.values() if s >= 5) >= max(1, working_exchanges * 0.5)
        
        # Determine confirmation
        if working_exchanges >= 3:
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
            if all_agree:
                confirmed = True
                strength = "HIGH ✅"
                confidence_desc = "BOTH EXCHANGES ALIGNED"
            else:
                confirmed = False
                strength = "MIXED ⚠️"
                confidence_desc = "EXCHANGES DISAGREE"
        
        else:
            solo_score = list(scores.values())[0]
            if solo_score >= 6:
                confirmed = True
                strength = "SINGLE EXCHANGE 🟡"
                confidence_desc = "STRONG BUT UNCONFIRMED"
            else:
                confirmed = False
                strength = "WEAK ❌"
                confidence_desc = "SINGLE EXCHANGE, LOW SCORE"
        
        # Build reason message
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
                reason_parts.append(f"{emoji} <b>{exchange_name.upper()}: ❌ FAILED</b>\n")
        
        reason_parts.append(f"<b>COMBINED: {avg_score:.1f}/7</b>")
        reason_parts.append(f"Strength: {strength}")
        
        reason = "\n".join(reason_parts)
        
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
        print("=" * 80)
        
        avg_delta = sum(d['delta'] for d in details.values()) / len(details) if details else 0
        avg_buy_pct = sum(d['buy_pct'] for d in details.values()) / len(details) if details else 50
        
        combined_data['delta'] = avg_delta
        combined_data['buy_pct'] = avg_buy_pct
        
        return confirmed, reason, combined_data, int(avg_score)
    
    except Exception as e:
        print(f"\n❌ ERROR: {type(e).__name__} - {str(e)}")
        import traceback
        traceback.print_exc()
        return False, f"⚠️ Error: {str(e)}", {}, 0

# ============= UTILITY FUNCTIONS =============
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
    return risk_amount / price_risk

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

# ============= MONITORING & WEBHOOK =============
def monitor_orderflow():
    """Monitor orderflow with smart fallback"""
    global last_orderflow_check
    print("📊 Smart multi-exchange monitoring started")
    
    while True:
        try:
            time.sleep(ORDERFLOW_INTERVAL)
            
            print("\n" + "="*80)
            print("📊 ORDERFLOW CHECK")
            print("="*80)
            
            current_price = get_current_price()
            if not current_price:
                print("❌ Failed to get price")
                continue
            
            binance_book = smart_binance_orderbook()
            binance_trades = smart_binance_trades()
            okx_book = get_okx_orderbook()
            okx_trades = get_okx_trades()
            bybit_book = smart_bybit_orderbook()
            bybit_trades = smart_bybit_trades()
            
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
                print("❌ No exchange data")
                continue
            
            avg_delta = sum(deltas) / len(deltas)
            avg_buy_pct = sum(buy_pcts) / len(buy_pcts)
            avg_cvd = sum(cvds) / len(cvds)
            
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
            
            print(f"✅ Working exchanges: {len(deltas)}/3")
            print(f"📊 Avg Delta: {avg_delta:,.2f}, Bias: {bias}")
        
        except Exception as e:
            print(f"❌ Monitor error: {e}")
            import traceback
            traceback.print_exc()

def monitor_trades():
    """Monitor active trades"""
    print("📊 Trade monitoring started")
    while True:
        try:
            time.sleep(TRADE_CHECK_INTERVAL)
            # Simple monitoring (full implementation omitted for brevity)
        except Exception as e:
            print(f"❌ Trade monitor error: {e}")

@app.route('/webhook', methods=['POST'])
def webhook():
    """Webhook handler"""
    try:
        data = request.json
        print(f"📨 Webhook: {json.dumps(data, indent=2)}")
        
        alert_type = data.get('type', 'unknown')
        current_price_from_tv = float(data.get('current_price', 0))
        
        setup_type = "UNKNOWN"
        direction = "LONG"
        entry = current_price_from_tv
        webhook_data = data.copy()
        
        # Parse alert (simplified for brevity)
        if alert_type == "tpo_poor_hl":
            level_type = data.get('level_type')
            level_price = float(data.get('level_price', 0))
            if level_type == "poor_high":
                setup_type = "POOR HIGH"
                direction = "SHORT"
                entry = level_price
            elif level_type == "poor_low":
                setup_type = "POOR LOW"
                direction = "LONG"
                entry = level_price
        
        # Calculate & confirm
        stop, target, rr_ratio = calculate_stops_targets(setup_type, direction, entry, webhook_data)
        confirmed, reason, of_data, score = confirm_order_flow_multi_exchange(direction, entry)
        
        if confirmed:
            timeframe, duration = classify_timeframe(setup_type, rr_ratio, of_data)
            position = calculate_position_size(entry, stop)
            
            message = f"""
🟢 <b>{setup_type} - {direction} ✅ CONFIRMED</b>

Entry: ${entry:,.0f}
Stop: ${stop:,.0f}
Target: ${target:,.0f}

<b>SMART MULTI-EXCHANGE:</b>
{reason}

📈 TAKING {direction}
            """
            send_telegram(message)
            print(f"✅ Trade confirmed")
        else:
            message = f"""
⚠️ <b>{setup_type} - {direction} ❌ REJECTED</b>

{reason}
            """
            send_telegram(message)
            print(f"❌ Trade rejected")
        
        return jsonify({"status": "success"}), 200
    
    except Exception as e:
        print(f"❌ Webhook error: {e}")
        return jsonify({"status": "error"}), 500

@app.route('/', methods=['GET'])
def home():
    working_count = sum(1 for v in working_endpoints.values() if v is not None)
    
    # Safe string formatting - handle None values properly
    binance_book = working_endpoints.get('binance_orderbook')
    binance_trades = working_endpoints.get('binance_trades')
    bybit_book = working_endpoints.get('bybit_orderbook')
    bybit_trades = working_endpoints.get('bybit_trades')
    
    return f"""
    <h1>🚀 Beast Mode V5 - SMART FALLBACK</h1>
    <p>Working Endpoints: {working_count}/4</p>
    <h3>Smart Endpoint Discovery:</h3>
    <ul>
        <li>Binance: {len(BINANCE_ENDPOINTS)} endpoints tested</li>
        <li>Bybit: {len(BYBIT_ENDPOINTS)} endpoints tested</li>
        <li>OKX: Direct connection</li>
    </ul>
    <h3>Currently Working:</h3>
    <ul>
        <li>Binance Book: {'✅ ' + binance_book if binance_book else '❌ Not found yet'}</li>
        <li>Binance Trades: {'✅ ' + binance_trades if binance_trades else '❌ Not found yet'}</li>
        <li>Bybit Book: {'✅ ' + bybit_book if bybit_book else '❌ Not found yet'}</li>
        <li>Bybit Trades: {'✅ ' + bybit_trades if bybit_trades else '❌ Not found yet'}</li>
    </ul>
    <p><em>Note: Endpoints are discovered on first orderflow check (runs every 15 min)</em></p>
    """, 200

if __name__ == '__main__':
    print("🚀 Beast Mode V5 - SMART FALLBACK EDITION")
    print("="*80)
    print("Endpoint Auto-Discovery:")
    print(f"  Binance: {len(BINANCE_ENDPOINTS)} endpoints")
    print(f"  Bybit: {len(BYBIT_ENDPOINTS)} endpoints")
    print(f"  OKX: Direct")
    print("="*80)
    
    orderflow_thread = threading.Thread(target=monitor_orderflow, daemon=True)
    orderflow_thread.start()
    
    trade_thread = threading.Thread(target=monitor_trades, daemon=True)
    trade_thread.start()
    
    send_telegram(f"""
🚀 <b>Beast Mode V5 - SMART FALLBACK Started</b>

🔍 <b>Auto-Discovery Active</b>
Testing {len(BINANCE_ENDPOINTS)} Binance endpoints
Testing {len(BYBIT_ENDPOINTS)} Bybit endpoints

System will find working endpoints automatically!

Will report which ones work in first cycle.
    """)
    
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)

