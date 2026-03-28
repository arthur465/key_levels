"""
BTC Liquidity System — Central Configuration
All tuneable parameters live here. Secrets come from .env
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ── Market ────────────────────────────────────────────────────────────────────
SYMBOL           = "BTC/USDT:USDT"          # Unified CCXT perp symbol
EXCHANGES        = ["okx", "kucoin", "gateio"]
PRIMARY_EXCHANGE = "okx"

# ── Timeframes ────────────────────────────────────────────────────────────────
TF_ENTRY   = "15m"   # Day-trade entries
TF_BIAS_1H = "1h"    # Intermediate HTF bias
TF_BIAS_4H = "4h"    # Primary HTF bias
TF_DAILY   = "1d"    # For PDH/PDL
TF_WEEKLY  = "1w"    # For PWH/PWL

# ── Fibonacci OTE ─────────────────────────────────────────────────────────────
FIB_OTE         = 0.71    # Primary entry zone
FIB_OTE_OPT     = 0.72    # Optional confirmation level
FIB_STOP_BUFFER = 0.003   # 0.3% SL buffer beyond swing point
MIN_RR          = 1.5     # Minimum acceptable R:R
MAX_RR          = 3.0     # Cap R:R at 3:1

# ── Level Sweep Tolerance ─────────────────────────────────────────────────────
SWEEP_TOLERANCE_PCT = 0.0025   # Price within 0.25% of level = sweep candidate

# ── Risk Management ───────────────────────────────────────────────────────────
INITIAL_CAPITAL = 30_000
RISK_PER_TRADE  = 0.01          # 1% per trade

# ── Chop Filter (ADX) ─────────────────────────────────────────────────────────
ADX_PERIOD    = 14
ADX_THRESHOLD = 22              # < 22 = ranging, suppress signals

# ── Session Windows (UTC hours, inclusive start, exclusive end) ───────────────
ACTIVE_SESSIONS = {
    "london":   (7,  12),
    "new_york": (12, 21),
}
TRADE_ALL_SESSIONS = False       # False = London + NY only

# ── CVD Filter ────────────────────────────────────────────────────────────────
USE_CVD      = True
CVD_LOOKBACK = 20                # Candles used for CVD slope

# ── Structure / Fractal ───────────────────────────────────────────────────────
FRACTAL_N          = 2           # Bars each side for pivot confirmation
STRUCTURE_LOOKBACK = 60          # Candles used for HTF bias detection
BOS_LOOKBACK       = 40          # Candles scanned after sweep for BOS

# ── Scheduler ─────────────────────────────────────────────────────────────────
POLL_INTERVAL_SECONDS = 60       # Scan frequency
DAILY_REPORT_TIME_UTC = "08:00"  # When to send the daily P&L report

# ── Telegram ─────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

# ── Exchange API Keys (optional — needed only for private endpoints) ───────────
OKX_API_KEY  = os.getenv("OKX_API_KEY", "")
OKX_SECRET   = os.getenv("OKX_SECRET", "")
OKX_PASSWORD = os.getenv("OKX_PASSWORD", "")
