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

# ============= OKX API (CONFIRMED WORKING) =============
def get_okx_orderbook():
    """OKX orderbook"""
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
    """OKX trades"""
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

# ============= BITGET API (FIXED - V2) =============
def get_bitget_orderbook():
    """Bitget orderbook - FIXED to use V2 API"""
    url = "https://api.bitget.com/api/v2/mix/market/merge-depth"
    params = {
        "symbol": "BTCUSDT",
        "productType": "USDT-FUTURES",
        "limit": "150"
    }
    try:
        print(f"   🟢 [BITGET] Calling orderbook API (V2)...")
        response = requests.get(url, params=params, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            if data.get("code") == "00000":
                book = data["data"]
                bids = book["bids"]  # Format: [price, size]
                asks = book["asks"]
                
                bid_volume = sum(float(b[1]) for b in bids)
                ask_volume = sum(float(a[1]) for a in asks)
                total_volume = bid_volume + ask_volume
                
                delta = bid_volume - ask_volume
                bid_pct = (bid_volume / total_volume * 100) if total_volume > 0 else 50
                
                print(f"   ✅ [BITGET] Delta: {delta:,.2f}, Bid%: {bid_pct:.1f}%")
                
                return {
                    'exchange': 'bitget',
                    'delta': delta,
                    'bid_volume': bid_volume,
                    'ask_volume': ask_volume,
                    'bid_pct': bid_pct
                }
        print(f"   ❌ [BITGET] HTTP {response.status_code}")
        return None
    except Exception as e:
        print(f"   ❌ [BITGET] Error: {type(e).__name__} - {str(e)}")
        return None

def get_bitget_trades():
    """Bitget trades - FIXED to use V2 API"""
    url = "https://api.bitget.com/api/v2/mix/market/fills"
    params = {
        "symbol": "BTCUSDT",
        "productType": "USDT-FUTURES",
        "limit": "500"
    }
    try:
        print(f"   🟢 [BITGET] Calling trades API (V2)...")
        response = requests.get(url, params=params, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            if data.get("code") == "00000":
                trades = data["data"]
                
                # V2 API format: side is "buy" or "sell"
                buy_volume = sum(float(t["size"]) for t in trades if t["side"] == "buy")
                sell_volume = sum(float(t["size"]) for t in trades if t["side"] == "sell")
                total = buy_volume + sell_volume
                
                buy_pct = (buy_volume / total * 100) if total > 0 else 50
                cvd = buy_volume - sell_volume
                
                print(f"   ✅ [BITGET] Buy%: {buy_pct:.1f}%, CVD: {cvd:,.2f}")
                
                return {
                    'exchange': 'bitget',
                    'buy_pct': buy_pct,
                    'cvd': cvd,
                    'buy_volume': buy_volume,
                    'sell_volume': sell_volume
                }
        print(f"   ❌ [BITGET] HTTP {response.status_code}")
        return None
    except Exception as e:
        print(f"   ❌ [BITGET] Error: {type(e).__name__} - {str(e)}")
        return None

# ============= MEXC API (FIXED - Array format) =============
def get_mexc_orderbook():
    """MEXC orderbook - FIXED parsing"""
    url = "https://contract.mexc.com/api/v1/contract/depth/BTC_USDT"
    params = {"limit": 500}
    try:
        print(f"   🟡 [MEXC] Calling orderbook API...")
        response = requests.get(url, params=params, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            if data.get("success"):
                book = data["data"]
                bids = book["bids"]  # Array format: [price, vol, ...]
                asks = book["asks"]
                
                # FIXED: Access by index, not dict key
                # Also convert units (MEXC uses contracts, divide by 1000 to normalize)
                bid_volume = sum(float(b[1]) for b in bids) / 1000
                ask_volume = sum(float(a[1]) for a in asks) / 1000
                total_volume = bid_volume + ask_volume
                
                delta = bid_volume - ask_volume
                bid_pct = (bid_volume / total_volume * 100) if total_volume > 0 else 50
                
                print(f"   ✅ [MEXC] Delta: {delta:,.2f}, Bid%: {bid_pct:.1f}%")
                
                return {
                    'exchange': 'mexc',
                    'delta': delta,
                    'bid_volume': bid_volume,
                    'ask_volume': ask_volume,
                    'bid_pct': bid_pct
                }
        print(f"   ❌ [MEXC] HTTP {response.status_code}")
        return None
    except Exception as e:
        print(f"   ❌ [MEXC] Error: {type(e).__name__} - {str(e)}")
        import traceback
        traceback.print_exc()
        return None

def get_mexc_trades():
    """MEXC trades - FIXED parsing"""
    url = "https://contract.mexc.com/api/v1/contract/deals/BTC_USDT"
    params = {"limit": 500}
    try:
        print(f"   🟡 [MEXC] Calling trades API...")
        response = requests.get(url, params=params, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            if data.get("success"):
                trades = data["data"]
                
                # MEXC uses "trade_type" field, not "side"
                # 1 = buy (open long), 2 = buy (close short), 3 = sell (close long), 4 = sell (open short)
                buy_volume = 0
                sell_volume = 0
                
                for t in trades:
                    vol = float(t.get("vol", 0))
                    trade_type = t.get("trade_type", 0)
                    
                    if trade_type in [1, 2]:  # Buy
                        buy_volume += vol
                    elif trade_type in [3, 4]:  # Sell
                        sell_volume += vol
                
                # Convert to comparable units (MEXC uses contracts)
                buy_volume = buy_volume / 1000
                sell_volume = sell_volume / 1000
                total = buy_volume + sell_volume
                
                buy_pct = (buy_volume / total * 100) if total > 0 else 50
                cvd = buy_volume - sell_volume
                
                print(f"   ✅ [MEXC] Buy%: {buy_pct:.1f}%, CVD: {cvd:,.2f}")
                
                return {
                    'exchange': 'mexc',
                    'buy_pct': buy_pct,
                    'cvd': cvd,
                    'buy_volume': buy_volume,
                    'sell_volume': sell_volume
                }
        print(f"   ❌ [MEXC] HTTP {response.status_code}")
        return None
    except Exception as e:
        print(f"   ❌ [MEXC] Error: {type(e).__name__} - {str(e)}")
        import traceback
        traceback.print_exc()
        return None

# ============= KUCOIN API =============
def get_kucoin_orderbook():
    """KuCoin orderbook"""
    url = "https://api-futures.kucoin.com/api/v1/level2/depth100"
    params = {"symbol": "XBTUSDTM"}
    try:
        print(f"   🔵 [KUCOIN] Calling orderbook API...")
        response = requests.get(url, params=params, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            if data.get("code") == "200000":
                book = data["data"]
                bids = book["bids"]
                asks = book["asks"]
                
                bid_volume = sum(float(b[1]) for b in bids)
                ask_volume = sum(float(a[1]) for a in asks)
                total_volume = bid_volume + ask_volume
                
                delta = bid_volume - ask_volume
                bid_pct = (bid_volume / total_volume * 100) if total_volume > 0 else 50
                
                print(f"   ✅ [KUCOIN] Delta: {delta:,.2f}, Bid%: {bid_pct:.1f}%")
                
                return {
                    'exchange': 'kucoin',
                    'delta': delta,
                    'bid_volume': bid_volume,
                    'ask_volume': ask_volume,
                    'bid_pct': bid_pct
                }
        print(f"   ❌ [KUCOIN] HTTP {response.status_code}")
        return None
    except Exception as e:
        print(f"   ❌ [KUCOIN] Error: {type(e).__name__}")
        return None

def get_kucoin_trades():
    """KuCoin trades"""
    url = "https://api-futures.kucoin.com/api/v1/trade/history"
    params = {"symbol": "XBTUSDTM"}
    try:
        print(f"   🔵 [KUCOIN] Calling trades API...")
        response = requests.get(url, params=params, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            if data.get("code") == "200000":
                trades = data["data"]
                
                buy_volume = sum(float(t["size"]) for t in trades if t["side"] == "buy")
                sell_volume = sum(float(t["size"]) for t in trades if t["side"] == "sell")
                total = buy_volume + sell_volume
                
                buy_pct = (buy_volume / total * 100) if total > 0 else 50
                cvd = buy_volume - sell_volume
                
                print(f"   ✅ [KUCOIN] Buy%: {buy_pct:.1f}%, CVD: {cvd:,.2f}")
                
                return {
                    'exchange': 'kucoin',
                    'buy_pct': buy_pct,
                    'cvd': cvd,
                    'buy_volume': buy_volume,
                    'sell_volume': sell_volume
                }
        print(f"   ❌ [KUCOIN] HTTP {response.status_code}")
        return None
    except Exception as e:
        print(f"   ❌ [KUCOIN] Error: {type(e).__name__}")
        return None

# ============= GATE.IO API (FIXED - Convert units) =============
def get_gateio_orderbook():
    """Gate.io orderbook - FIXED unit conversion"""
    url = "https://api.gateio.ws/api/v4/futures/usdt/order_book"
    params = {"contract": "BTC_USDT", "limit": 100}
    try:
        print(f"   🔷 [GATE.IO] Calling orderbook API...")
        response = requests.get(url, params=params, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            bids = data["bids"]
            asks = data["asks"]
            
            # Gate.io returns size in contracts (USD value)
            # Convert to approximate BTC by dividing by 1000 to make comparable
            bid_volume = sum(float(b["s"]) for b in bids) / 1000
            ask_volume = sum(float(a["s"]) for a in asks) / 1000
            
            total_volume = bid_volume + ask_volume
            
            delta = bid_volume - ask_volume
            bid_pct = (bid_volume / total_volume * 100) if total_volume > 0 else 50
            
            print(f"   ✅ [GATE.IO] Delta: {delta:,.2f}, Bid%: {bid_pct:.1f}%")
            
            return {
                'exchange': 'gateio',
                'delta': delta,
                'bid_volume': bid_volume,
                'ask_volume': ask_volume,
                'bid_pct': bid_pct
            }
        print(f"   ❌ [GATE.IO] HTTP {response.status_code}")
        return None
    except Exception as e:
        print(f"   ❌ [GATE.IO] Error: {type(e).__name__} - {str(e)}")
        import traceback
        traceback.print_exc()
        return None

def get_gateio_trades():
    """Gate.io trades - FIXED unit conversion"""
    url = "https://api.gateio.ws/api/v4/futures/usdt/trades"
    params = {"contract": "BTC_USDT", "limit": 500}
    try:
        print(f"   🔷 [GATE.IO] Calling trades API...")
        response = requests.get(url, params=params, timeout=10)
        
        if response.status_code == 200:
            trades = response.json()
            
            # Gate.io: positive size = buy, negative size = sell
            buy_volume = sum(float(t["size"]) for t in trades if float(t["size"]) > 0) / 1000
            sell_volume = sum(abs(float(t["size"])) for t in trades if float(t["size"]) < 0) / 1000
            
            total = buy_volume + sell_volume
            
            buy_pct = (buy_volume / total * 100) if total > 0 else 50
            cvd = buy_volume - sell_volume
            
            print(f"   ✅ [GATE.IO] Buy%: {buy_pct:.1f}%, CVD: {cvd:,.2f}")
            
            return {
                'exchange': 'gateio',
                'buy_pct': buy_pct,
                'cvd': cvd,
                'buy_volume': buy_volume,
                'sell_volume': sell_volume
            }
        print(f"   ❌ [GATE.IO] HTTP {response.status_code}")
        return None
    except Exception as e:
        print(f"   ❌ [GATE.IO] Error: {type(e).__name__} - {str(e)}")
        import traceback
        traceback.print_exc()
        return None

# ============= CURRENT PRICE =============
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
    
    # Try Bitget
    try:
        response = requests.get("https://api.bitget.com/api/mix/v1/market/ticker",
                              params={"symbol": "BTCUSDT_UMCBL"}, timeout=5)
        if response.status_code == 200:
            data = response.json()
            if data.get("code") == "00000":
                return float(data["data"]["last"])
    except:
        pass
    
    # Try MEXC
    try:
        response = requests.get("https://contract.mexc.com/api/v1/contract/ticker/BTC_USDT", timeout=5)
        if response.status_code == 200:
            data = response.json()
            if data.get("success"):
                return float(data["data"]["last"])
    except:
        pass
    
    return None

# Continue with scoring, monitoring, and all other features...

# ============= 5-EXCHANGE SCORING =============
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

def confirm_order_flow_five_exchange(direction, entry_price):
    """5-EXCHANGE ORDERFLOW CONFIRMATION"""
    print("=" * 80)
    print("🔍 5-EXCHANGE ORDERFLOW CHECK")
    print(f"Direction: {direction}, Entry: {entry_price}")
    print("=" * 80)
    
    try:
        # Fetch all 5 exchanges
        print("\n📡 Fetching from all 5 exchanges...")
        
        okx_book = get_okx_orderbook()
        okx_trades = get_okx_trades()
        
        bitget_book = get_bitget_orderbook()
        bitget_trades = get_bitget_trades()
        
        mexc_book = get_mexc_orderbook()
        mexc_trades = get_mexc_trades()
        
        kucoin_book = get_kucoin_orderbook()
        kucoin_trades = get_kucoin_trades()
        
        gateio_book = get_gateio_orderbook()
        gateio_trades = get_gateio_trades()
        
        # Score each exchange
        print("\n📊 Scoring exchanges...")
        
        scores = {}
        details = {}
        
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
        
        if bitget_book and bitget_trades:
            score, reasons = score_single_exchange(bitget_book, bitget_trades, direction)
            scores['bitget'] = score
            details['bitget'] = {
                'score': score,
                'reasons': reasons,
                'delta': bitget_book['delta'],
                'buy_pct': bitget_trades['buy_pct']
            }
            print(f"   🟢 BITGET: {score}/7")
        else:
            print(f"   ❌ BITGET: Failed")
        
        if mexc_book and mexc_trades:
            score, reasons = score_single_exchange(mexc_book, mexc_trades, direction)
            scores['mexc'] = score
            details['mexc'] = {
                'score': score,
                'reasons': reasons,
                'delta': mexc_book['delta'],
                'buy_pct': mexc_trades['buy_pct']
            }
            print(f"   🟡 MEXC: {score}/7")
        else:
            print(f"   ❌ MEXC: Failed")
        
        if kucoin_book and kucoin_trades:
            score, reasons = score_single_exchange(kucoin_book, kucoin_trades, direction)
            scores['kucoin'] = score
            details['kucoin'] = {
                'score': score,
                'reasons': reasons,
                'delta': kucoin_book['delta'],
                'buy_pct': kucoin_trades['buy_pct']
            }
            print(f"   🔵 KUCOIN: {score}/7")
        else:
            print(f"   ❌ KUCOIN: Failed")
        
        if gateio_book and gateio_trades:
            score, reasons = score_single_exchange(gateio_book, gateio_trades, direction)
            scores['gateio'] = score
            details['gateio'] = {
                'score': score,
                'reasons': reasons,
                'delta': gateio_book['delta'],
                'buy_pct': gateio_trades['buy_pct']
            }
            print(f"   🔷 GATE.IO: {score}/7")
        else:
            print(f"   ❌ GATE.IO: Failed")
        
        # Calculate combined metrics
        working_exchanges = len(scores)
        
        if working_exchanges == 0:
            print("\n❌ ALL EXCHANGES FAILED!")
            return False, "Unable to fetch orderflow from ANY exchange", {}, 0
        
        print(f"\n✅ Working exchanges: {working_exchanges}/5")
        
        avg_score = sum(scores.values()) / working_exchanges
        strong_count = sum(1 for s in scores.values() if s >= 5)
        
        # Determine confirmation level
        if working_exchanges >= 5:
            # All 5 working
            if strong_count == 5:
                confirmed = True
                strength = "ULTRA HIGH 🔥🔥🔥"
                confidence_desc = "ALL 5 EXCHANGES ALIGNED"
            elif strong_count >= 4:
                confirmed = True
                strength = "VERY HIGH 🔥🔥"
                confidence_desc = f"STRONG CONSENSUS ({strong_count}/5)"
            elif strong_count >= 3:
                confirmed = True
                strength = "HIGH ✅"
                confidence_desc = f"MAJORITY ALIGNED ({strong_count}/5)"
            else:
                confirmed = False
                strength = "DIVERGING ⚠️"
                confidence_desc = "EXCHANGES DISAGREE"
        
        elif working_exchanges == 4:
            if strong_count >= 4:
                confirmed = True
                strength = "VERY HIGH 🔥🔥"
                confidence_desc = "ALL 4 ALIGNED"
            elif strong_count >= 3:
                confirmed = True
                strength = "HIGH ✅"
                confidence_desc = f"MAJORITY ({strong_count}/4)"
            else:
                confirmed = False
                strength = "MIXED ⚠️"
                confidence_desc = "DIVERGING"
        
        elif working_exchanges == 3:
            if strong_count >= 3:
                confirmed = True
                strength = "HIGH ✅"
                confidence_desc = "ALL 3 ALIGNED"
            elif strong_count >= 2:
                confirmed = True
                strength = "MODERATE ✅"
                confidence_desc = f"MAJORITY ({strong_count}/3)"
            else:
                confirmed = False
                strength = "WEAK ⚠️"
                confidence_desc = "DIVERGING"
        
        elif working_exchanges == 2:
            if strong_count >= 2:
                confirmed = True
                strength = "MODERATE ✅"
                confidence_desc = "BOTH ALIGNED"
            else:
                confirmed = False
                strength = "WEAK ❌"
                confidence_desc = "DIVERGING"
        
        else:
            # Only 1 working
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
        
        exchange_emojis = {
            'okx': '🟠',
            'bitget': '🟢',
            'mexc': '🟡',
            'kucoin': '🔵',
            'gateio': '🔷'
        }
        
        for exchange_name in ['okx', 'bitget', 'mexc', 'kucoin', 'gateio']:
            if exchange_name in details:
                d = details[exchange_name]
                emoji = exchange_emojis[exchange_name]
                reason_parts.append(f"{emoji} <b>{exchange_name.upper()}: {d['score']}/7</b>")
                for reason_line in d['reasons']:
                    reason_parts.append(f"   {reason_line}")
                reason_parts.append("")
            else:
                emoji = exchange_emojis[exchange_name]
                reason_parts.append(f"{emoji} <b>{exchange_name.upper()}: ❌ FAILED</b>\n")
        
        reason_parts.append(f"<b>COMBINED: {avg_score:.1f}/7 ({strong_count}/{working_exchanges} strong)</b>")
        reason_parts.append(f"Strength: {strength}")
        
        reason = "\n".join(reason_parts)
        
        combined_data = {
            'avg_score': avg_score,
            'working_exchanges': working_exchanges,
            'strong_count': strong_count,
            'scores': scores,
            'details': details
        }
        
        print(f"\n📊 FINAL RESULT:")
        print(f"   Combined Score: {avg_score:.1f}/7")
        print(f"   Strong Signals: {strong_count}/{working_exchanges}")
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


# ============= MARKET SCANNER - PATTERN DETECTION =============
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
    """Detect if delta is strengthening or weakening"""
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
    """Send pattern alert"""
    pattern_type = pattern.get('type')
    
    if pattern_type in ['BEARISH_DIVERGENCE', 'BULLISH_DIVERGENCE']:
        emoji = "📉" if "BEARISH" in pattern_type else "📈"
        message = f"""
{emoji} <b>MARKET SCANNER ALERT</b>

<b>{pattern_type}</b>
Strength: {pattern['strength']:.1f}/10

Price: {pattern['price_trend']}
Delta: {pattern['delta_trend']}

Current Price: ${pattern['current_price']:,.0f}
Current Δ: {pattern['current_delta']:+,.0f}

⚠️ Reversal signal detected across 5 exchanges
        """
        send_telegram(message)
    
    elif pattern_type in ['SUPPLY_ABSORPTION', 'DEMAND_ABSORPTION']:
        emoji = "🛑" if "SUPPLY" in pattern_type else "💪"
        message = f"""
{emoji} <b>MARKET SCANNER ALERT</b>

<b>{pattern_type}</b>
Severity: {pattern['severity']:.1f}/10

Δ: {pattern['delta']:+,.0f} BTC
Price Change: {pattern['price_change_pct']:.2f}%

⚠️ Heavy orderflow with minimal price movement
        """
        send_telegram(message)
    
    elif pattern_type in ['BUYING_CLIMAX', 'SELLING_CLIMAX']:
        emoji = "🔥" if "BUYING" in pattern_type else "❄️"
        message = f"""
{emoji} <b>MARKET SCANNER ALERT</b>

<b>{pattern_type}</b>
Severity: {pattern['severity']:.1f}/10

Current Δ: {pattern['delta']:+,.0f} BTC
Average Δ: {pattern['avg_delta']:,.0f} BTC
Ratio: {(abs(pattern['delta'])/pattern['avg_delta']):.1f}x

Price Momentum: {pattern['price_momentum']:.2f}%

⚠️ Exhaustion signal - potential reversal
        """
        send_telegram(message)

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


# ============= ORDERFLOW MONITORING =============
def monitor_orderflow():
    """Monitor orderflow across 5 exchanges + market scanner"""
    global last_orderflow_check
    print("📊 5-exchange orderflow monitoring + Market Scanner started")
    
    while True:
        try:
            time.sleep(ORDERFLOW_INTERVAL)
            
            print("\n" + "="*80)
            print("📊 5-EXCHANGE ORDERFLOW + MARKET SCAN")
            print("="*80)
            
            current_price = get_current_price()
            if not current_price:
                print("❌ Failed to get price")
                continue
            
            # Fetch all exchanges
            okx_book = get_okx_orderbook()
            okx_trades = get_okx_trades()
            bitget_book = get_bitget_orderbook()
            bitget_trades = get_bitget_trades()
            mexc_book = get_mexc_orderbook()
            mexc_trades = get_mexc_trades()
            kucoin_book = get_kucoin_orderbook()
            kucoin_trades = get_kucoin_trades()
            gateio_book = get_gateio_orderbook()
            gateio_trades = get_gateio_trades()
            
            # Aggregate data
            deltas = []
            buy_pcts = []
            cvds = []
            
            if okx_book and okx_trades:
                deltas.append(okx_book['delta'])
                buy_pcts.append(okx_trades['buy_pct'])
                cvds.append(okx_trades['cvd'])
            
            if bitget_book and bitget_trades:
                deltas.append(bitget_book['delta'])
                buy_pcts.append(bitget_trades['buy_pct'])
                cvds.append(bitget_trades['cvd'])
            
            if mexc_book and mexc_trades:
                deltas.append(mexc_book['delta'])
                buy_pcts.append(mexc_trades['buy_pct'])
                cvds.append(mexc_trades['cvd'])
            
            if kucoin_book and kucoin_trades:
                deltas.append(kucoin_book['delta'])
                buy_pcts.append(kucoin_trades['buy_pct'])
                cvds.append(kucoin_trades['cvd'])
            
            if gateio_book and gateio_trades:
                deltas.append(gateio_book['delta'])
                buy_pcts.append(gateio_trades['buy_pct'])
                cvds.append(gateio_trades['cvd'])
            
            if not deltas:
                print("❌ No exchange data")
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
                if divergence and divergence['strength'] > 5:
                    print(f"   🚨 DIVERGENCE: {divergence['type']} (strength: {divergence['strength']:.1f}/10)")
                    patterns_detected.append(divergence)
                    if should_alert_pattern(divergence['type']):
                        send_pattern_alert(divergence)
            
            if len(orderflow_history) >= 2:
                absorption = detect_absorption(current_reading, orderflow_history[-2])
                if absorption and absorption['severity'] > 5:
                    print(f"   🚨 ABSORPTION: {absorption['type']} (severity: {absorption['severity']:.1f}/10)")
                    patterns_detected.append(absorption)
                    if should_alert_pattern(absorption['type']):
                        send_pattern_alert(absorption)
            
            if len(orderflow_history) >= 3:
                exhaustion = detect_exhaustion(current_reading, orderflow_history)
                if exhaustion and exhaustion['severity'] > 5:
                    print(f"   🚨 EXHAUSTION: {exhaustion['type']} (severity: {exhaustion['severity']:.1f}/10)")
                    patterns_detected.append(exhaustion)
                    if should_alert_pattern(exhaustion['type']):
                        send_pattern_alert(exhaustion)
            
            if not patterns_detected:
                print("   ✅ No significant patterns detected")
            
            print(f"✅ Working exchanges: {len(deltas)}/5")
            print(f"📊 Avg Delta: {avg_delta:,.2f}, Bias: {bias}")
        
        except Exception as e:
            print(f"❌ Orderflow monitor error: {e}")
            import traceback
            traceback.print_exc()

# ============= TRADE MONITORING =============
def monitor_trades():
    """Monitor active trades for weakness"""
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
                
                # Get current orderflow
                okx_book = get_okx_orderbook()
                bitget_book = get_bitget_orderbook()
                mexc_book = get_mexc_orderbook()
                kucoin_book = get_kucoin_orderbook()
                gateio_book = get_gateio_orderbook()
                
                deltas = []
                if okx_book:
                    deltas.append(okx_book['delta'])
                if bitget_book:
                    deltas.append(bitget_book['delta'])
                if mexc_book:
                    deltas.append(mexc_book['delta'])
                if kucoin_book:
                    deltas.append(kucoin_book['delta'])
                if gateio_book:
                    deltas.append(gateio_book['delta'])
                
                if not deltas:
                    continue
                
                current_delta = sum(deltas) / len(deltas)
                trades_to_remove = []
                
                for trade_id, info in active_trades.items():
                    direction = info['direction']
                    entry = info['entry']
                    target = info['target']
                    stop = info.get('stop', 0)
                    entry_delta = info['entry_delta']
                    alerts_sent = info['alerts_sent']
                    
                    # Check stop loss
                    if direction == "LONG" and current_price <= stop:
                        send_telegram(f"🛑 <b>STOP HIT - LONG</b>\nEntry: ${entry:,.0f}\nExit: ${current_price:,.0f}\nLoss: ${current_price - entry:,.0f}")
                        trades_to_remove.append(trade_id)
                        continue
                    
                    elif direction == "SHORT" and current_price >= stop:
                        send_telegram(f"🛑 <b>STOP HIT - SHORT</b>\nEntry: ${entry:,.0f}\nExit: ${current_price:,.0f}\nLoss: ${entry - current_price:,.0f}")
                        trades_to_remove.append(trade_id)
                        continue
                    
                    # Monitor for weakness
                    if direction == "LONG":
                        if entry_delta > 2000 and current_delta < -2000 and 'weakening' not in alerts_sent:
                            send_telegram(f"🚨 <b>LONG WEAKENING</b>\n\nEntry Δ: +{entry_delta:,.0f}\nCurrent Δ: {current_delta:,.0f}\n\n⚠️ CONSIDER EXIT\n\nPrice: ${current_price:,.0f}\nP&L: ${current_price - entry:+,.0f}")
                            alerts_sent.append('weakening')
                        
                        if current_price >= target:
                            send_telegram(f"✅ <b>TARGET HIT - LONG</b>\n\nProfit: ${target - entry:,.0f}\n🎉")
                            trades_to_remove.append(trade_id)
                    
                    elif direction == "SHORT":
                        if entry_delta < -2000 and current_delta > 2000 and 'weakening' not in alerts_sent:
                            send_telegram(f"🚨 <b>SHORT WEAKENING</b>\n\nEntry Δ: {entry_delta:,.0f}\nCurrent Δ: +{current_delta:,.0f}\n\n⚠️ CONSIDER EXIT\n\nPrice: ${current_price:,.0f}\nP&L: ${entry - current_price:+,.0f}")
                            alerts_sent.append('weakening')
                        
                        if current_price <= target:
                            send_telegram(f"✅ <b>TARGET HIT - SHORT</b>\n\nProfit: ${entry - target:,.0f}\n🎉")
                            trades_to_remove.append(trade_id)
                
                for tid in trades_to_remove:
                    del active_trades[tid]
        
        except Exception as e:
            print(f"❌ Trade monitor error: {e}")
            import traceback
            traceback.print_exc()


# ============= WEBHOOK HANDLER =============
@app.route('/webhook', methods=['POST'])
def webhook():
    """Webhook handler for TradingView alerts"""
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
            
            if "monthly" in poc_type:
                setup_type = "NAKED POC (Monthly)"
            elif "weekly" in poc_type:
                setup_type = "NAKED POC (Weekly)"
            else:
                setup_type = "NAKED POC"
            
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
        
        # 5-EXCHANGE ORDERFLOW CONFIRMATION
        confirmed, reason, of_data, score = confirm_order_flow_five_exchange(direction, entry)
        
        # Confidence
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
        
        # Timeframe & position
        timeframe, duration = classify_timeframe(setup_type, rr_ratio, of_data)
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

<b>CONFIDENCE: {confidence_stars} ({score}/7)</b>
Strength: {strength}

📍 <b>TRADE SETUP</b>
Entry: ${entry:,.0f}
Stop: ${stop:,.0f}
Target: ${target:,.0f}
R:R: {rr_ratio:.2f}

💰 Position: {position:.4f} BTC ({RISK_PER_TRADE*100:.0f}% risk)
⏱️ Timeframe: {timeframe} ({duration})

📊 Market Bias: {market_bias} {confluence}

<b>5-EXCHANGE ORDERFLOW:</b>
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

<b>CONFIDENCE: {confidence_stars} ({score}/7)</b>
Strength: {strength}

Entry: ${entry:,.0f}
Target: ${target:,.0f}
R:R: {rr_ratio:.2f}

<b>5-EXCHANGE ORDERFLOW:</b>
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
    <h1>🚀 Beast Mode V6 - 5-EXCHANGE EDITION</h1>
    <p>Active Trades: {len(active_trades)}</p>
    <p>History Size: {len(orderflow_history)}/{HISTORY_SIZE}</p>
    <h3>5-Exchange Coverage:</h3>
    <ul>
        <li>🟠 OKX: $20B daily</li>
        <li>🟢 Bitget: $17B daily</li>
        <li>🟡 MEXC: $12B daily</li>
        <li>🔵 KuCoin: $12B daily</li>
        <li>🔷 Gate.io: $10B daily</li>
    </ul>
    <p><b>Total Coverage: $71B daily (47% of global BTC futures volume!)</b></p>
    <h3>Features:</h3>
    <ul>
        <li>✅ 5-Exchange Cross-Validation</li>
        <li>✅ Divergence Detection</li>
        <li>✅ Absorption Patterns</li>
        <li>✅ Exhaustion Signals</li>
        <li>✅ Dynamic Stops & Targets</li>
        <li>✅ Trade Monitoring with Weakness Alerts</li>
        <li>✅ Market Scanner (15min cycles)</li>
    </ul>
    """, 200

if __name__ == '__main__':
    print("🚀 Beast Mode V6 - 5-EXCHANGE EDITION")
    print("="*80)
    print("Exchange Coverage:")
    print("  🟠 OKX: $20B daily")
    print("  🟢 Bitget: $17B daily")
    print("  🟡 MEXC: $12B daily")
    print("  🔵 KuCoin: $12B daily")
    print("  🔷 Gate.io: $10B daily")
    print("  ────────────────────")
    print("  💰 TOTAL: $71B daily")
    print("  📊 Coverage: 47% of global BTC futures volume")
    print("="*80)
    print("Features:")
    print("  ✅ 5-Exchange Cross-Validation")
    print("  ✅ Divergence Detection")
    print("  ✅ Absorption & Exhaustion Patterns")
    print("  ✅ Trade Monitoring with Weakness Alerts")
    print("  ✅ Market Scanner (every 15 min)")
    print("="*80)
    
    # Start monitoring threads
    orderflow_thread = threading.Thread(target=monitor_orderflow, daemon=True)
    orderflow_thread.start()
    
    trade_thread = threading.Thread(target=monitor_trades, daemon=True)
    trade_thread.start()
    
    # Send startup message
    send_telegram(f"""
🚀 <b>Beast Mode V6 - 5-EXCHANGE Started</b>

📊 <b>Multi-Exchange Coverage: $71B Daily</b>
🟠 OKX: $20B
🟢 Bitget: $17B
🟡 MEXC: $12B
🔵 KuCoin: $12B
🔷 Gate.io: $10B

<b>Coverage: 47% of global BTC futures volume!</b>

🔄 Monitoring: ON
✅ Filter: ACTIVE
🔥 Market Scanner: ACTIVE

💰 Account: ${ACCOUNT_SIZE:,.0f}
📊 Risk: {RISK_PER_TRADE*100:.0f}% per trade

<b>Features:</b>
• 5-Exchange Cross-Validation
• Divergence Detection (Chart Champions style)
• Absorption Patterns (smart money)
• Exhaustion Signals (climax patterns)
• Trade Monitoring with Weakness Alerts

<b>Scanning every {ORDERFLOW_INTERVAL/60:.0f} minutes!</b>
    """)
    
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)

