import os
import time
import requests
from datetime import datetime, timezone, timedelta
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

# ============= NEW: ALERT MEMORY SYSTEM =============
alert_memory = deque(maxlen=50)  # Track last 50 alerts

def add_to_memory(alert_type, data):
    """Track alerts for context"""
    alert_memory.append({
        'type': alert_type,
        'timestamp': datetime.now(timezone.utc),
        'data': data
    })

def get_recent_alerts(alert_type=None, hours=3):
    """Get recent alerts, optionally filtered by type"""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    alerts = [a for a in alert_memory if a['timestamp'] > cutoff]
    
    if alert_type:
        alerts = [a for a in alerts if a['type'] == alert_type]
    
    return alerts

def check_recent_divergence():
    """Check if we recently called a divergence"""
    recent_div = get_recent_alerts('divergence', hours=3)
    if recent_div:
        latest = recent_div[-1]
        return latest['data']
    return None

def check_recent_climax():
    """Check if we recently detected a climax"""
    recent_climax = get_recent_alerts('climax', hours=2)
    if recent_climax:
        latest = recent_climax[-1]
        return latest['data']
    return None

# ============= TRADE TRACKING =============
active_trades = {}
trade_lock = threading.Lock()

last_orderflow_check = {
    'bias': 'NEUTRAL',
    'delta': 0,
    'cvd': 0,
    'timestamp': None,
    'price': 0
}

# ============= MARKET SCANNER DATA =============
orderflow_history = deque(maxlen=HISTORY_SIZE)
last_pattern_alerts = {}

# ============= FLASK APP =============
app = Flask(__name__)

# ============= HELPER: FORMAT DELTA EXPLANATION =============
def format_delta_explanation(delta, is_orderbook=False):
    """Add explanation for what delta means"""
    if delta > 0:
        pressure_type = "BUYING" if is_orderbook else "BUYING"
        symbol = "+"
        interpretation = "More buying than selling"
    else:
        pressure_type = "SELLING" if is_orderbook else "SELLING"
        symbol = ""
        interpretation = "More selling than buying"
    
    return f"{symbol}{delta:,.0f} BTC ({pressure_type} pressure)\n  ↳ {interpretation}"

def format_price_context(price_change_pct, delta):
    """Explain what price movement vs delta means"""
    abs_change = abs(price_change_pct)
    
    if abs_change < 0.1:
        movement = "Barely moved"
        if abs(delta) > 5000:
            context = "Heavy volume but minimal price impact"
            implication = "→ ABSORPTION happening"
        else:
            context = "Low volume, low movement"
            implication = "→ Consolidation"
    elif abs_change < 0.5:
        movement = "Small movement"
        context = "Normal price action"
        implication = ""
    else:
        movement = "Significant movement"
        context = "Strong directional move"
        implication = ""
    
    return f"""Price Change: {price_change_pct:.2f}% ({movement})
  ↳ {context}{implication if implication else ''}"""

def get_absorption_meaning(delta, price_change, price_level="current"):
    """Explain what absorption means at different price levels"""
    if delta > 0:
        # Positive delta = buying
        if abs(price_change) < 0.15:
            if price_level == "resistance":
                return """⚠️ WHAT THIS MEANS:
Heavy buying but price can't break through resistance
Sellers distributing into demand at this level
Buyers likely exhausting = BEARISH signal"""
            else:
                return """⚠️ WHAT THIS MEANS:
Heavy buying but price barely moving up
Strong support forming at this level
Sellers will exhaust = BULLISH continuation likely"""
    else:
        # Negative delta = selling
        if abs(price_change) < 0.15:
            if price_level == "support":
                return """⚠️ WHAT THIS MEANS:
Heavy selling but price holding support
Buyers absorbing all the selling pressure
Sellers will exhaust = BULLISH reversal likely"""
            else:
                return """⚠️ WHAT THIS MEANS:
Heavy selling with minimal price drop
Buyers stepping in to support price
Strong hands accumulating = BULLISH signal"""
    
    return ""

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
                bids = book["bids"]
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
                bids = book["bids"]
                asks = book["asks"]
                
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
        print(f"   ❌ [MEXC] Error: {type(e).__name__}")
        return None

def get_mexc_trades():
    """MEXC trades - FIXED for single-letter field names"""
    url = "https://contract.mexc.com/api/v1/contract/deals/BTC_USDT"
    params = {"limit": 1000}
    try:
        print(f"   🟡 [MEXC] Calling trades API...")
        response = requests.get(url, params=params, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            if data.get("success"):
                trades = data.get("data", [])
                
                if not trades:
                    print(f"   ❌ [MEXC] Trades array is empty")
                    return None
                
                buy_volume = 0
                sell_volume = 0
                parsed_count = 0
                
                for t in trades:
                    try:
                        # MEXC uses single-letter keys:
                        # 'v' = volume
                        # 'T' = type/side (1=buy, 2=sell based on their API)
                        
                        vol = float(t.get('v', 0))
                        side = t.get('T', 0)
                        
                        if vol == 0:
                            continue
                        
                        # T=1 is buy, T=2 is sell (standard MEXC format)
                        if side == 1:
                            buy_volume += vol
                            parsed_count += 1
                        elif side == 2:
                            sell_volume += vol
                            parsed_count += 1
                        
                    except (KeyError, ValueError, TypeError) as e:
                        continue
                
                print(f"   🟡 [MEXC] Parsed {parsed_count}/{len(trades)} trades")
                
                # Normalize (MEXC uses contracts, divide by 1000)
                buy_volume = buy_volume / 1000
                sell_volume = sell_volume / 1000
                total = buy_volume + sell_volume
                
                if total == 0:
                    print(f"   ❌ [MEXC] Total volume is 0 after parsing")
                    return None
                
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

# ============= GATE.IO API =============
def get_gateio_orderbook():
    """Gate.io orderbook"""
    url = "https://api.gateio.ws/api/v4/futures/usdt/order_book"
    params = {"contract": "BTC_USDT", "limit": 100}
    try:
        print(f"   🔷 [GATE.IO] Calling orderbook API...")
        response = requests.get(url, params=params, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            bids = data["bids"]
            asks = data["asks"]
            
            bid_volume = sum(float(b["s"]) for b in bids)
            ask_volume = sum(float(a["s"]) for a in asks)
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
        print(f"   ❌ [GATE.IO] Error: {type(e).__name__}")
        return None

def get_gateio_trades():
    """Gate.io trades"""
    url = "https://api.gateio.ws/api/v4/futures/usdt/trades"
    params = {"contract": "BTC_USDT", "limit": 1000}
    try:
        print(f"   🔷 [GATE.IO] Calling trades API...")
        response = requests.get(url, params=params, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            
            buy_volume = sum(abs(float(t["size"])) for t in data if float(t["size"]) > 0)
            sell_volume = sum(abs(float(t["size"])) for t in data if float(t["size"]) < 0)
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
        print(f"   ❌ [GATE.IO] Error: {type(e).__name__}")
        return None

# ============= AGGREGATED ORDERFLOW =============
def get_aggregated_orderflow():
    """Aggregate order flow from all 5 exchanges with robust error handling"""
    print("\n📊 AGGREGATING 5-EXCHANGE ORDERFLOW")
    print("="*80)
    
    exchanges_data = []
    
    # OKX
    try:
        okx_book = get_okx_orderbook()
        okx_trades = get_okx_trades()
        if okx_book and okx_trades:
            exchanges_data.append({
                'name': 'OKX',
                'emoji': '🟠',
                'orderbook': okx_book,
                'trades': okx_trades
            })
    except Exception as e:
        print(f"   ❌ [OKX] Aggregation error: {e}")
    
    # Bitget
    try:
        bitget_book = get_bitget_orderbook()
        bitget_trades = get_bitget_trades()
        if bitget_book and bitget_trades:
            exchanges_data.append({
                'name': 'BITGET',
                'emoji': '🟢',
                'orderbook': bitget_book,
                'trades': bitget_trades
            })
    except Exception as e:
        print(f"   ❌ [BITGET] Aggregation error: {e}")
    
    # MEXC
    try:
        mexc_book = get_mexc_orderbook()
        mexc_trades = get_mexc_trades()
        if mexc_book and mexc_trades:
            exchanges_data.append({
                'name': 'MEXC',
                'emoji': '🟡',
                'orderbook': mexc_book,
                'trades': mexc_trades
            })
    except Exception as e:
        print(f"   ❌ [MEXC] Aggregation error: {e}")
    
    # KuCoin
    try:
        kucoin_book = get_kucoin_orderbook()
        kucoin_trades = get_kucoin_trades()
        if kucoin_book and kucoin_trades:
            exchanges_data.append({
                'name': 'KUCOIN',
                'emoji': '🔵',
                'orderbook': kucoin_book,
                'trades': kucoin_trades
            })
    except Exception as e:
        print(f"   ❌ [KUCOIN] Aggregation error: {e}")
    
    # Gate.io
    try:
        gateio_book = get_gateio_orderbook()
        gateio_trades = get_gateio_trades()
        if gateio_book and gateio_trades:
            exchanges_data.append({
                'name': 'GATEIO',
                'emoji': '🔷',
                'orderbook': gateio_book,
                'trades': gateio_trades
            })
    except Exception as e:
        print(f"   ❌ [GATEIO] Aggregation error: {e}")
    
    if not exchanges_data:
        print("❌ No exchange data available")
        return None
    
    # Aggregate with error handling
    try:
        total_delta = sum(ex['orderbook']['delta'] for ex in exchanges_data)
        total_cvd = sum(ex['trades']['cvd'] for ex in exchanges_data)
        avg_bid_pct = sum(ex['orderbook']['bid_pct'] for ex in exchanges_data) / len(exchanges_data)
        avg_buy_pct = sum(ex['trades']['buy_pct'] for ex in exchanges_data) / len(exchanges_data)
    except Exception as e:
        print(f"❌ Aggregation calculation error: {e}")
        return None
    
    # Determine bias
    if avg_bid_pct >= 55 and avg_buy_pct >= 55:
        bias = "🟢 BULLISH"
    elif avg_bid_pct <= 45 and avg_buy_pct <= 45:
        bias = "🔴 BEARISH"
    else:
        bias = "⚪ NEUTRAL"
    
    print(f"\n📊 AGGREGATED RESULTS:")
    print(f"   Bias: {bias}")
    print(f"   Delta: {total_delta:,.0f} BTC")
    print(f"   CVD: {total_cvd:,.0f} BTC")
    print(f"   Avg Bid%: {avg_bid_pct:.1f}%")
    print(f"   Avg Buy%: {avg_buy_pct:.1f}%")
    print(f"   Exchanges: {len(exchanges_data)}/5")
    print("="*80)
    
    return {
        'bias': bias,
        'delta': total_delta,
        'cvd': total_cvd,
        'avg_bid_pct': avg_bid_pct,
        'avg_buy_pct': avg_buy_pct,
        'exchanges_count': len(exchanges_data),
        'exchanges_data': exchanges_data
    }

# ============= TELEGRAM =============
def send_telegram(message):
    """Send message to Telegram"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Telegram not configured")
        return False
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    
    try:
        response = requests.post(url, data=data, timeout=10)
        if response.status_code == 200:
            print("✅ Telegram message sent")
            return True
        else:
            print(f"❌ Telegram error: {response.status_code}")
            return False
    except Exception as e:
        print(f"❌ Telegram error: {e}")
        return False

# ============= PRICE FETCHING =============
def get_current_price():
    """Get current BTC price from OKX"""
    try:
        url = "https://www.okx.com/api/v5/market/ticker"
        params = {"instId": "BTC-USDT-SWAP"}
        response = requests.get(url, params=params, timeout=5)
        
        if response.status_code == 200:
            data = response.json()
            if data.get("code") == "0":
                price = float(data["data"][0]["last"])
                return price
    except:
        pass
    
    return None

# ============= ENHANCED: PATTERN DETECTION WITH INTERPRETATIONS =============
def detect_market_patterns(current_of):
    """
    Detect market patterns with FULL EXPLANATIONS
    - Divergences
    - Absorption
    - Exhaustion/Climax
    """
    if not current_of:
        return
    
    current_price = get_current_price()
    if not current_price:
        return
    
    # Store in history
    orderflow_history.append({
        'timestamp': datetime.now(timezone.utc),
        'price': current_price,
        'delta': current_of['delta'],
        'cvd': current_of['cvd'],
        'avg_bid_pct': current_of['avg_bid_pct'],
        'avg_buy_pct': current_of['avg_buy_pct']
    })
    
    if len(orderflow_history) < 5:
        return
    
    # Check for cooldown
    now = time.time()
    
    # ============= DIVERGENCE DETECTION =============
    if len(orderflow_history) >= DIVERGENCE_LOOKBACK:
        recent = list(orderflow_history)[-DIVERGENCE_LOOKBACK:]
        
        prices = [h['price'] for h in recent]
        deltas = [h['delta'] for h in recent]
        
        # Bearish divergence: price making higher highs, delta weakening
        if prices[-1] > max(prices[:-1]) and deltas[-1] < max(deltas[:-1]):
            delta_decline_pct = ((deltas[-1] - max(deltas[:-1])) / abs(max(deltas[:-1]))) * 100 if max(deltas[:-1]) != 0 else 0
            
            if abs(delta_decline_pct) > 30:  # At least 30% weaker
                cooldown_key = 'bearish_divergence'
                last_alert_time = last_pattern_alerts.get(cooldown_key, 0)
                
                if now - last_alert_time > PATTERN_ALERT_COOLDOWN:
                    strength = min(10.0, abs(delta_decline_pct) / 10)
                    
                    # Store in memory
                    add_to_memory('divergence', {
                        'type': 'bearish',
                        'price': current_price,
                        'delta': current_of['delta'],
                        'strength': strength,
                        'direction': 'down'
                    })
                    
                    # ENHANCED ALERT WITH FULL EXPLANATION
                    message = f"""
📉 <b>MARKET SCANNER ALERT</b>

<b>BEARISH_DIVERGENCE</b>
Strength: {strength:.1f}/10

<b>📊 WHAT'S HAPPENING:</b>
Price: HIGHER_HIGHS
  ↳ Price making new highs at ${current_price:,.0f}

Delta: WEAKENING
  ↳ Current Δ: {format_delta_explanation(current_of['delta'])}
  ↳ Declining {abs(delta_decline_pct):.1f}% vs. recent peak
  ↳ Buyers losing strength at higher prices

<b>⚠️ WHAT THIS MEANS:</b>
Price is pushing to new highs BUT buying volume is declining
This is a classic <b>TOP FORMATION</b> pattern
Buyers are exhausting while sellers prepare to step in
= High probability of <b>REVERSAL DOWN</b>

<b>🎯 WATCH FOR:</b>
• Price failing to make new highs
• Increased selling pressure
• Reversal candle forming
• Break below recent support

<b>⚡ BIAS: BEARISH</b>
Potential reversal from ${current_price:,.0f}

<b>🗣️ IN PLAIN ENGLISH:</b>
Price is at new highs but buyers are getting weaker.
This usually means sellers are about to take over.
Watch for price to reverse and drop.
                    """
                    
                    send_telegram(message)
                    last_pattern_alerts[cooldown_key] = now
        
        # Bullish divergence: price making lower lows, delta strengthening
        elif prices[-1] < min(prices[:-1]) and deltas[-1] > min(deltas[:-1]):
            delta_improve_pct = ((deltas[-1] - min(deltas[:-1])) / abs(min(deltas[:-1]))) * 100 if min(deltas[:-1]) != 0 else 0
            
            if abs(delta_improve_pct) > 30:
                cooldown_key = 'bullish_divergence'
                last_alert_time = last_pattern_alerts.get(cooldown_key, 0)
                
                if now - last_alert_time > PATTERN_ALERT_COOLDOWN:
                    strength = min(10.0, abs(delta_improve_pct) / 10)
                    
                    # Store in memory
                    add_to_memory('divergence', {
                        'type': 'bullish',
                        'price': current_price,
                        'delta': current_of['delta'],
                        'strength': strength,
                        'direction': 'up'
                    })
                    
                    # ENHANCED ALERT
                    message = f"""
📈 <b>MARKET SCANNER ALERT</b>

<b>BULLISH_DIVERGENCE</b>
Strength: {strength:.1f}/10

<b>📊 WHAT'S HAPPENING:</b>
Price: LOWER_LOWS
  ↳ Price making new lows at ${current_price:,.0f}

Delta: STRENGTHENING
  ↳ Current Δ: {format_delta_explanation(current_of['delta'])}
  ↳ Improving {abs(delta_improve_pct):.1f}% vs. recent trough
  ↳ Buyers stepping in at lower prices

<b>⚠️ WHAT THIS MEANS:</b>
Price is dropping to new lows BUT buying is increasing
This is a classic <b>BOTTOM FORMATION</b> pattern
Sellers are exhausting while buyers accumulate
= High probability of <b>REVERSAL UP</b>

<b>🎯 WATCH FOR:</b>
• Price stabilizing at current level
• Volume declining (sellers done)
• Reversal candle forming  
• Break above recent resistance

<b>⚡ BIAS: BULLISH</b>
Potential reversal from ${current_price:,.0f}

<b>🗣️ IN PLAIN ENGLISH:</b>
Price is at new lows but buyers are getting stronger.
This usually means sellers are exhausted.
Watch for price to reverse and rally.
                    """
                    
                    send_telegram(message)
                    last_pattern_alerts[cooldown_key] = now
    
    # ============= ABSORPTION PATTERNS =============
    if len(orderflow_history) >= 3:
        recent = list(orderflow_history)[-3:]
        current_delta = recent[-1]['delta']
        avg_delta = sum(abs(h['delta']) for h in recent[:-1]) / 2
        
        # Calculate price change
        price_change = ((recent[-1]['price'] - recent[-2]['price']) / recent[-2]['price']) * 100
        
        # Demand absorption: Heavy selling with minimal price drop
        if current_delta < -3000 and abs(price_change) < 0.15:
            severity = min(10.0, abs(current_delta) / 2000)
            
            cooldown_key = 'demand_absorption'
            last_alert_time = last_pattern_alerts.get(cooldown_key, 0)
            
            if now - last_alert_time > 1800:  # 30 min cooldown
                # Store in memory
                add_to_memory('absorption', {
                    'type': 'demand',
                    'price': current_price,
                    'delta': current_delta,
                    'severity': severity
                })
                
                # Check if this follows a recent divergence
                recent_div = check_recent_divergence()
                context_note = ""
                if recent_div and recent_div.get('type') == 'bearish':
                    context_note = f"""
<b>📌 CONTEXT:</b>
This follows the bearish divergence at ${recent_div.get('price', 0):,.0f}
Price dumped as predicted, now finding support
Buyers absorbing the selling = reversal zone!
"""
                
                message = f"""
🔴 <b>MARKET SCANNER ALERT</b>

<b>DEMAND_ABSORPTION</b>
Severity: {severity:.1f}/10

<b>📊 ORDER FLOW:</b>
Δ: {format_delta_explanation(current_delta)}
  ↳ HEAVY selling pressure
  
Avg Δ: {avg_delta:,.0f} BTC (Recent average)
  ↳ Current is {abs(current_delta/avg_delta):.1f}x heavier

<b>📈 PRICE ACTION:</b>
{format_price_context(price_change, current_delta)}

{get_absorption_meaning(current_delta, price_change, "support")}

<b>🎯 WATCH FOR:</b>
• Support holding at ${current_price:,.0f}
• Volume declining (sellers exhausting)
• Reversal candle forming
• Bounce to resistance

<b>⚡ BIAS: Cautiously BULLISH</b>
Heavy selling absorbed = potential bounce

{context_note}

<b>🗣️ IN PLAIN ENGLISH:</b>
Sellers dumped {abs(current_delta):,.0f} BTC but price barely dropped.
Someone is buying ALL that selling = strong support.
Watch for price to bounce when sellers run out.

<b>⚠️ Heavy orderflow with minimal price movement</b>
                """
                
                send_telegram(message)
                last_pattern_alerts[cooldown_key] = now
        
        # Supply absorption: Heavy buying with minimal price rise  
        elif current_delta > 3000 and abs(price_change) < 0.15:
            severity = min(10.0, abs(current_delta) / 2000)
            
            cooldown_key = 'supply_absorption'
            last_alert_time = last_pattern_alerts.get(cooldown_key, 0)
            
            if now - last_alert_time > 1800:
                # Store in memory
                add_to_memory('absorption', {
                    'type': 'supply',
                    'price': current_price,
                    'delta': current_delta,
                    'severity': severity
                })
                
                # Check context
                recent_div = check_recent_divergence()
                context_note = ""
                if recent_div and recent_div.get('type') == 'bullish':
                    context_note = f"""
<b>📌 CONTEXT:</b>
This follows the bullish divergence at ${recent_div.get('price', 0):,.0f}
Price rallied as predicted, now at resistance
Heavy buying but can't break through = potential top!
"""
                
                message = f"""
🟢 <b>MARKET SCANNER ALERT</b>

<b>SUPPLY_ABSORPTION</b>
Severity: {severity:.1f}/10

<b>📊 ORDER FLOW:</b>
Δ: {format_delta_explanation(current_delta)}
  ↳ HEAVY buying pressure

Avg Δ: {avg_delta:,.0f} BTC (Recent average)
  ↳ Current is {abs(current_delta/avg_delta):.1f}x heavier

<b>📈 PRICE ACTION:</b>
{format_price_context(price_change, current_delta)}

{get_absorption_meaning(current_delta, price_change, "resistance")}

<b>🎯 WATCH FOR:</b>
• Resistance holding (can't break ${current_price:,.0f})
• Volume declining (buyers exhausting)
• Reversal candle down
• Drop back to support

<b>⚡ BIAS: Cautiously BEARISH</b>
Heavy buying absorbed = potential rejection

{context_note}

<b>🗣️ IN PLAIN ENGLISH:</b>
Buyers pushed {abs(current_delta):,.0f} BTC but price barely moved up.
Sellers are absorbing at this level = strong resistance.
Watch for price to reverse down when buyers exhaust.

<b>⚠️ Heavy orderflow with minimal price movement</b>
                """
                
                send_telegram(message)
                last_pattern_alerts[cooldown_key] = now
    
    # ============= CLIMAX PATTERNS =============
    if len(orderflow_history) >= 5:
        recent = list(orderflow_history)[-5:]
        current_delta = recent[-1]['delta']
        avg_delta = sum(h['delta'] for h in recent[:-1]) / 4
        
        price_change_1min = ((recent[-1]['price'] - recent[-2]['price']) / recent[-2]['price']) * 100
        
        ratio = abs(current_delta / avg_delta) if avg_delta != 0 else 1
        
        # Selling climax: Extreme selling with minimal price drop
        if current_delta < -5000 and ratio > 2.0 and abs(price_change_1min) < 0.2:
            severity = min(10.0, ratio * 2)
            
            cooldown_key = 'selling_climax'
            last_alert_time = last_pattern_alerts.get(cooldown_key, 0)
            
            if now - last_alert_time > 1800:
                # Store in memory
                add_to_memory('climax', {
                    'type': 'selling',
                    'price': current_price,
                    'delta': current_delta,
                    'severity': severity,
                    'ratio': ratio
                })
                
                message = f"""
💥 <b>MARKET SCANNER ALERT</b>

<b>SELLING_CLIMAX</b>
Severity: {severity:.1f}/10

<b>📊 ORDER FLOW:</b>
Current Δ: {format_delta_explanation(current_delta)}
  
Average Δ: {avg_delta:,.0f} BTC (Recent normal)

Ratio: {ratio:.1f}x above normal
  ↳ Selling is <b>MUCH heavier</b> than usual
  ↳ This is <b>EXTREME</b> selling pressure

<b>📈 PRICE ACTION:</b>
Price Momentum: {price_change_1min:.2f}% (Minimal downside)
  ↳ Despite heavy selling, price barely dropped
  ↳ Buyers are ABSORBING the dump

<b>⚠️ WHAT THIS MEANS:</b>
Sellers threw EVERYTHING at the market
But buyers absorbed it all = price held firm
This is <b>SELLER EXHAUSTION</b>
= High probability of <b>BULLISH reversal</b>

<b>🎯 WATCH FOR:</b>
• Price stabilizing at ${current_price:,.0f}
• Volume declining (sellers done)
• Reversal candle forming
• Bounce in next 15-60 minutes

<b>⚡ BIAS: Strongly BULLISH</b>
Climax selling absorbed = reversal imminent

<b>🗣️ IN PLAIN ENGLISH:</b>
Sellers dumped {abs(current_delta):,.0f} BTC ({ratio:.1f}x normal).
But price held strong = buyers absorbed it all.
Sellers are likely exhausted now.
Watch for bounce.

<b>⚠️ Exhaustion signal - potential reversal</b>
                """
                
                send_telegram(message)
                last_pattern_alerts[cooldown_key] = now
        
        # Buying climax: Extreme buying with minimal price rise
        elif current_delta > 5000 and ratio > 2.0 and abs(price_change_1min) < 0.2:
            severity = min(10.0, ratio * 2)
            
            cooldown_key = 'buying_climax'
            last_alert_time = last_pattern_alerts.get(cooldown_key, 0)
            
            if now - last_alert_time > 1800:
                # Store in memory
                add_to_memory('climax', {
                    'type': 'buying',
                    'price': current_price,
                    'delta': current_delta,
                    'severity': severity,
                    'ratio': ratio
                })
                
                message = f"""
💥 <b>MARKET SCANNER ALERT</b>

<b>BUYING_CLIMAX</b>
Severity: {severity:.1f}/10

<b>📊 ORDER FLOW:</b>
Current Δ: {format_delta_explanation(current_delta)}

Average Δ: {avg_delta:,.0f} BTC (Recent normal)

Ratio: {ratio:.1f}x above normal
  ↳ Buying is <b>MUCH heavier</b> than usual
  ↳ This is <b>EXTREME</b> buying pressure

<b>📈 PRICE ACTION:</b>
Price Momentum: {price_change_1min:.2f}% (Minimal upside)
  ↳ Despite heavy buying, price can't rise
  ↳ Sellers are ABSORBING at this level

<b>⚠️ WHAT THIS MEANS:</b>
Buyers pushed HARD but hit resistance
Sellers are distributing into the demand
This is <b>BUYER EXHAUSTION</b>
= High probability of <b>BEARISH reversal</b>

<b>🎯 WATCH FOR:</b>
• Resistance holding at ${current_price:,.0f}
• Volume declining (buyers exhausted)
• Reversal candle down
• Drop in next 15-60 minutes

<b>⚡ BIAS: Strongly BEARISH</b>
Climax buying absorbed = reversal imminent

<b>🗣️ IN PLAIN ENGLISH:</b>
Buyers pushed {abs(current_delta):,.0f} BTC ({ratio:.1f}x normal).
But price can't break through = sellers absorbing.
Buyers are likely exhausted now.
Watch for reversal down.

<b>⚠️ Exhaustion signal - potential reversal</b>
                """
                
                send_telegram(message)
                last_pattern_alerts[cooldown_key] = now

# ============= ENHANCED: CONTEXT-AWARE CONFIRMATION =============
def confirm_order_flow_five_exchange(direction, entry):
    """
    5-EXCHANGE orderflow confirmation with CONTEXT AWARENESS
    
    Now checks:
    - Recent divergences
    - Recent climaxes
    - Delta interpretation based on context
    - Dynamic confidence thresholds
    """
    print(f"\n🔍 5-EXCHANGE CONFIRMATION FOR {direction} @ ${entry:,.0f}")
    
    of = get_aggregated_orderflow()
    if not of:
        return False, "Unable to fetch order flow data", {}, 0
    
    exchanges = of.get('exchanges_data', [])
    if len(exchanges) < 3:
        return False, f"Only {len(exchanges)}/5 exchanges available", of, 0
    
    # ============= CONTEXT CHECKS =============
    recent_div = check_recent_divergence()
    recent_climax = check_recent_climax()
    
    # Check if our prediction is playing out
    prediction_confirmed = False
    context_boost = 0
    context_note = ""
    
    if recent_div:
        div_type = recent_div.get('type')
        div_price = recent_div.get('price', 0)
        price_moved = abs(entry - div_price)
        
        # Check if price moved as predicted
        if div_type == 'bearish' and entry < div_price:
            # Price dumped as predicted, now trying to long the bottom
            prediction_confirmed = True
            context_boost = 2  # Add 2 to confidence
            context_note = f"✅ Following bearish divergence at ${div_price:,.0f} - prediction confirmed!"
            print(f"   ✅ CONTEXT: Recent bearish divergence confirmed, trying to catch reversal")
        
        elif div_type == 'bullish' and entry > div_price:
            # Price rallied as predicted, now trying to short the top
            prediction_confirmed = True
            context_boost = 2
            context_note = f"✅ Following bullish divergence at ${div_price:,.0f} - prediction confirmed!"
            print(f"   ✅ CONTEXT: Recent bullish divergence confirmed, trying to catch reversal")
    
    if recent_climax and not prediction_confirmed:
        climax_type = recent_climax.get('type')
        climax_price = recent_climax.get('price', 0)
        
        if climax_type == 'selling' and direction == "LONG":
            prediction_confirmed = True
            context_boost = 2
            context_note = f"✅ Following selling climax at ${climax_price:,.0f} - looking for bounce!"
            print(f"   ✅ CONTEXT: Recent selling climax, now trying to long the reversal")
        
        elif climax_type == 'buying' and direction == "SHORT":
            prediction_confirmed = True
            context_boost = 2
            context_note = f"✅ Following buying climax at ${climax_price:,.0f} - looking for rejection!"
            print(f"   ✅ CONTEXT: Recent buying climax, now trying to short the reversal")
    
    # ============= ANALYZE EACH EXCHANGE WITH CONTEXT =============
    scores = []
    reasons = []
    
    for ex in exchanges:
        name = ex['name']
        emoji = ex['emoji']
        orderbook = ex['orderbook']
        trades = ex['trades']
        
        delta = orderbook['delta']
        bid_pct = orderbook['bid_pct']
        buy_pct = trades['buy_pct']
        
        score = 0
        
        # ============= CONTEXT-AWARE DELTA INTERPRETATION =============
        # After a dump, negative delta is BULLISH (absorption)
        # At resistance, negative delta is BEARISH (distribution)
        
        if direction == "LONG":
            # Interpret delta in context
            if prediction_confirmed and delta < 0:
                # Negative delta after dump = absorption = BULLISH!
                if abs(delta) > 1000:
                    score += 1
                    reasons.append(f"   {emoji} Negative Δ but in reversal zone (absorption)")
            elif delta > 500:
                score += 1
        
        elif direction == "SHORT":
            if prediction_confirmed and delta > 0:
                # Positive delta after rally = absorption = BEARISH!
                if abs(delta) > 1000:
                    score += 1
                    reasons.append(f"   {emoji} Positive Δ but in reversal zone (absorption)")
            elif delta < -500:
                score += 1
        
        # Bid/Ask analysis
        if direction == "LONG" and bid_pct > 52:
            score += 1
        elif direction == "SHORT" and bid_pct < 48:
            score += 1
        
        # Buy/Sell pressure
        if direction == "LONG" and buy_pct > 52:
            score += 1
        elif direction == "SHORT" and buy_pct < 48:
            score += 1
        
        scores.append(score)
        
        # Format reason
        delta_sign = "+" if delta > 0 else ""
        status = "✅" if score >= 2 else "❌"
        
        buy_emoji = "✅" if (direction == "LONG" and buy_pct > 50) or (direction == "SHORT" and buy_pct < 50) else "❌"
        
        reasons.append(
            f"{emoji} <b>{name}: {score}/7</b>\n"
            f"   {status} Negative Δ: {delta:,.0f}\n"
            f"   {buy_emoji} Strong buys: {buy_pct:.1f}%"
        )
    
    # ============= DYNAMIC CONFIDENCE THRESHOLD =============
    total_score = sum(scores) + context_boost
    strong_count = sum(1 for s in scores if s >= 2)
    
    # Adjust threshold based on context
    if prediction_confirmed:
        # Lower threshold when we predicted this move
        required_score = 2  # Much lower!
        required_strong = 0  # Don't need strong exchanges
        print(f"   📊 Using LOWERED threshold (prediction confirmed)")
    else:
        # Normal threshold
        required_score = 5
        required_strong = 2
        print(f"   📊 Using NORMAL threshold")
    
    confirmed = total_score >= required_score or strong_count >= required_strong
    
    # Build reason message
    reason_text = f"""
{"<b>🎯 CONTEXT:</b>" if context_note else ""}
{context_note}

{chr(10).join(reasons)}

<b>COMBINED: {total_score}/{7 + context_boost} ({strong_count}/5 strong)</b>
Strength: {"CONFIRMING ✅" if confirmed else "DIVERGING ⚠️"}
"""
    
    if prediction_confirmed:
        reason_text += f"\n<b>💡 Threshold lowered due to confirmed prediction</b>"
    
    print(f"   📊 Total Score: {total_score}/{7 + context_boost}")
    print(f"   📊 Strong Exchanges: {strong_count}/5")
    print(f"   {'✅ CONFIRMED' if confirmed else '❌ REJECTED'}")
    
    return confirmed, reason_text, of, total_score

# ============= POSITION SIZING =============
def calculate_position_size(entry, stop):
    """Calculate position size based on risk"""
    risk_amount = ACCOUNT_SIZE * RISK_PER_TRADE
    stop_distance = abs(entry - stop)
    stop_pct = stop_distance / entry
    
    position = risk_amount / stop_distance
    
    return position

# ============= TIMEFRAME CLASSIFICATION =============
def classify_timeframe(setup_type, rr_ratio, of_data):
    """Classify trade timeframe"""
    if "SINGLE PRINT" in setup_type:
        return "SWING", "3-7 days"
    elif rr_ratio > 5:
        return "POSITIONAL", "5-14 days"
    elif rr_ratio > 3:
        return "SWING", "2-5 days"
    else:
        return "INTRADAY", "4-24 hours"

# ============= STOPS & TARGETS =============
def calculate_stops_targets(setup_type, direction, entry, webhook_data):
    """Calculate stops and targets"""
    if "VA RE-ENTRY" in setup_type:
        val = webhook_data.get('val', entry * 0.97)
        vah = webhook_data.get('vah', entry * 1.03)
        
        if direction == "LONG":
            stop = val * 0.997
            target = vah
        else:
            stop = vah * 1.003
            target = val
    
    elif "NAKED POC" in setup_type:
        if direction == "LONG":
            stop = entry * 0.995
            target = entry * 1.015
        else:
            stop = entry * 1.005
            target = entry * 0.985
    
    elif "SINGLE PRINT" in setup_type:
        level_high = webhook_data.get('level_high', entry * 1.01)
        level_low = webhook_data.get('level_low', entry * 0.99)
        
        if direction == "LONG":
            stop = level_low * 0.997
            target = level_high
        else:
            stop = level_high * 1.003
            target = level_low
    
    else:
        if direction == "LONG":
            stop = entry * 0.995
            target = entry * 1.015
        else:
            stop = entry * 1.005
            target = entry * 0.985
    
    rr_ratio = abs((target - entry) / (entry - stop)) if (entry - stop) != 0 else 1
    
    return stop, target, rr_ratio

# ============= TRADE MONITORING =============
def monitor_trades():
    """Monitor active trades for weakness"""
    print("🔄 Trade monitoring thread started")
    
    while True:
        try:
            time.sleep(TRADE_CHECK_INTERVAL)
            
            if not active_trades:
                continue
            
            print(f"\n📊 Checking {len(active_trades)} active trades...")
            
            current_of = get_aggregated_orderflow()
            if not current_of:
                continue
            
            current_price = get_current_price()
            if not current_price:
                continue
            
            with trade_lock:
                for trade_id, trade in list(active_trades.items()):
                    direction = trade['direction']
                    entry = trade['entry']
                    stop = trade['stop']
                    target = trade['target']
                    entry_delta = trade.get('entry_delta', 0)
                    
                    current_delta = current_of['delta']
                    delta_change = current_delta - entry_delta
                    delta_change_pct = (delta_change / abs(entry_delta) * 100) if entry_delta != 0 else 0
                    
                    # Check for weakness
                    weakness_detected = False
                    weakness_reason = ""
                    
                    if direction == "LONG":
                        if delta_change_pct < -50:
                            weakness_detected = True
                            weakness_reason = f"Delta weakened {abs(delta_change_pct):.0f}% since entry"
                    
                    elif direction == "SHORT":
                        if delta_change_pct > 50:
                            weakness_detected = True
                            weakness_reason = f"Delta strengthened {abs(delta_change_pct):.0f}% against position"
                    
                    if weakness_detected:
                        if 'weakness' not in trade['alerts_sent']:
                            message = f"""
⚠️ <b>TRADE WEAKNESS DETECTED</b>

<b>{trade['setup_type']} - {direction}</b>

Entry: ${entry:,.0f}
Current: ${current_price:,.0f}
Target: ${target:,.0f}

<b>⚠️ Order Flow Weakness:</b>
{weakness_reason}

Entry Δ: {entry_delta:,.0f}
Current Δ: {current_delta:,.0f}
Change: {delta_change_pct:+.1f}%

<b>Consider tightening stop or exiting</b>
                            """
                            
                            send_telegram(message)
                            trade['alerts_sent'].append('weakness')
                            trade['last_update'] = datetime.now(timezone.utc)
        
        except Exception as e:
            print(f"❌ Trade monitoring error: {e}")

# ============= ORDERFLOW MONITORING =============
def monitor_orderflow():
    """Monitor orderflow every 15 minutes"""
    print("🔄 Orderflow monitoring thread started")
    
    while True:
        try:
            current_of = get_aggregated_orderflow()
            
            if current_of:
                global last_orderflow_check
                current_price = get_current_price()
                
                last_orderflow_check = {
                    'bias': current_of['bias'],
                    'delta': current_of['delta'],
                    'cvd': current_of['cvd'],
                    'timestamp': datetime.now(timezone.utc),
                    'price': current_price or 0
                }
                
                detect_market_patterns(current_of)
            
            time.sleep(ORDERFLOW_INTERVAL)
        
        except Exception as e:
            print(f"❌ Orderflow monitoring error: {e}")
            time.sleep(ORDERFLOW_INTERVAL)

# ============= WEBHOOK ENDPOINT =============
@app.route('/webhook', methods=['POST'])
def webhook():
    """Webhook endpoint for TradingView alerts"""
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({"status": "error", "message": "No data"}), 400
        
        print(f"\n📨 Webhook received: {json.dumps(data, indent=2)}")
        
        alert_type = data.get('alert_type', '').lower()
        webhook_data = data.copy()
        
        # Determine setup type and entry
        if alert_type == "va_reentry":
            val = float(data.get('val', 0))
            vah = float(data.get('vah', 0))
            setup_type = "VA RE-ENTRY"
            
            current_price = get_current_price()
            if current_price and current_price < val:
                direction = "LONG"
                entry = val
            else:
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
        
        # 5-EXCHANGE ORDERFLOW CONFIRMATION (with context awareness!)
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
    <h1>🚀 Beast Mode V7 - ENHANCED EDITION</h1>
    <p>Active Trades: {len(active_trades)}</p>
    <p>History Size: {len(orderflow_history)}/{HISTORY_SIZE}</p>
    <p>Alert Memory: {len(alert_memory)}/50</p>
    
    <h3>NEW FEATURES:</h3>
    <ul>
        <li>✅ Alert Memory System (tracks last 50 alerts)</li>
        <li>✅ Context-Aware Confidence (dynamic thresholds)</li>
        <li>✅ Delta Interpretation (context-based meaning)</li>
        <li>✅ Enhanced Alert Formatting (full explanations)</li>
        <li>✅ Prediction Tracking (validates divergences)</li>
    </ul>
    
    <h3>5-Exchange Coverage:</h3>
    <ul>
        <li>🟠 OKX: $20B daily</li>
        <li>🟢 Bitget: $17B daily</li>
        <li>🟡 MEXC: $12B daily</li>
        <li>🔵 KuCoin: $12B daily</li>
        <li>🔷 Gate.io: $10B daily</li>
    </ul>
    <p><b>Total Coverage: $71B daily (47% of global BTC futures volume!)</b></p>
    """, 200

if __name__ == '__main__':
    print("🚀 Beast Mode V7 - ENHANCED EDITION")
    print("="*80)
    print("NEW FEATURES:")
    print("  ✅ Alert Memory System")
    print("  ✅ Context-Aware Confidence")
    print("  ✅ Delta Interpretation")
    print("  ✅ Enhanced Formatting")
    print("  ✅ Prediction Tracking")
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
    
    # Start monitoring threads
    orderflow_thread = threading.Thread(target=monitor_orderflow, daemon=True)
    orderflow_thread.start()
    
    trade_thread = threading.Thread(target=monitor_trades, daemon=True)
    trade_thread.start()
    
    # Send startup message
    send_telegram(f"""
🚀 <b>Beast Mode V7 - ENHANCED Started</b>

<b>🆕 NEW FEATURES:</b>
✅ Alert Memory System (last 50 alerts)
✅ Context-Aware Confidence
✅ Smart Delta Interpretation
✅ Full Alert Explanations
✅ Prediction Tracking

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

<b>Scanning every {ORDERFLOW_INTERVAL/60:.0f} minutes!</b>
    """)
    
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
