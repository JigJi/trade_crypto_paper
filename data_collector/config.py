"""
Data Collector Configuration
==============================
DB params, Binance keys, schedules, staleness thresholds.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root
load_dotenv(Path(__file__).parent.parent / ".env")

# ---- Paths ----
BASE_DIR = Path(__file__).parent
LOG_DIR = BASE_DIR / "logs"
STATE_DIR = BASE_DIR / "state"
LOG_DIR.mkdir(exist_ok=True)
STATE_DIR.mkdir(exist_ok=True)

# ---- Database ----
DB_PARAMS = {
    "dbname": os.getenv("PG_DB", "smart_trading"),
    "user": os.getenv("PG_USER", "postgres"),
    "password": os.getenv("PG_PASS", "P@ssw0rd"),
    "host": os.getenv("PG_HOST", "localhost"),
    "port": os.getenv("PG_PORT", "5432"),
}

# ---- Binance ----
BINANCE_KEY = os.getenv("BINANCE_KEY")
BINANCE_SECRET = os.getenv("BINANCE_SECRET")

# ---- Per-coin symbols (v3 + v4 + v5, 46 coins) ----
ALT_SYMBOLS = [
    # v3 original (6)
    "BTCUSDT", "XRPUSDT", "ADAUSDT", "DOTUSDT", "SUIUSDT", "FILUSDT",
    # v3 new (13)
    "RENDERUSDT", "BEATUSDT", "PIXELUSDT", "NEARUSDT", "AXSUSDT", "SOLUSDT",
    "ETHUSDT", "1000BONKUSDT", "ARBUSDT", "ARIAUSDT", "BARDUSDT", "BANANAS31USDT", "PIPPINUSDT",
    # v4 (12) -- removed LYNUSDT, XAIUSDT, PUMPUSDT (testnet-bad)
    "OGNUSDT", "SAHARAUSDT", "ASTERUSDT", "LTCUSDT", "ZROUSDT",
    "NAORISUSDT", "1000PEPEUSDT", "JCTUSDT", "DEGOUSDT",
    "HYPEUSDT", "PENGUUSDT", "LINKUSDT",
    # v5 (15)
    "FARTCOINUSDT", "GALAUSDT", "AAVEUSDT", "AVAXUSDT", "UNIUSDT",
    "SEIUSDT", "DOGEUSDT", "ONDOUSDT", "1000SHIBUSDT", "ICXUSDT",
    "BNBUSDT", "WIFUSDT", "CRVUSDT", "TAOUSDT", "ACXUSDT",
]

# ---- Symbols ----
OI_SYMBOLS = ["BTCUSDT"]
OB_SYMBOL = "BTCUSDT"
OB_LEVELS = 1000
OB_SOURCE = "um_futures"

# Long/Short ratio symbols (from original get_long_short_ratio.py)
LS_SYMBOL = "BTCUSDT"

# Taker volume
TAKER_SYMBOL = "BTCUSDT"

# Klines
KLINE_SYMBOLS = ["BTCUSDT"]

# ---- WebSocket ----
WS_LIQ_URL = "wss://fstream.binance.com/ws/!forceOrder@arr"
WS_LIQ_SYMBOLS = ALT_SYMBOLS  # collect liq for all trading coins
WS_BUFFER_MAX = 2000
WS_FLUSH_INTERVAL_SEC = 5

# CVD WebSocket (BTC only)
WS_CVD_FUTURES = "wss://fstream.binance.com/ws/btcusdt@aggTrade"
WS_CVD_SPOT = "wss://stream.binance.com:9443/ws/btcusdt@aggTrade"
CVD_BUCKET_SECONDS = 900   # 15 minutes
CVD_FLUSH_INTERVAL_SEC = 60

# CVD Alt WebSocket (per-coin, top liquid coins)
CVD_ALT_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT",
    "ADAUSDT", "AVAXUSDT", "BNBUSDT", "SUIUSDT", "LINKUSDT",
    "DOTUSDT", "LTCUSDT", "UNIUSDT", "AAVEUSDT", "SEIUSDT",
]
_cvd_alt_streams = "/".join(f"{s.lower()}@aggTrade" for s in CVD_ALT_SYMBOLS)
WS_CVD_ALT_FUTURES = f"wss://fstream.binance.com/stream?streams={_cvd_alt_streams}"

# ---- Telegram (whale alerts + news, optional) ----
TELEGRAM_API_ID = os.getenv("TELEGRAM_API_ID", "29674353")
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "85f09278e682542fa3c5b210705d2efc")
TELEGRAM_SESSION_PATH = str(STATE_DIR / "whale_session")

# ---- Deribit ----
DERIBIT_BASE = "https://www.deribit.com/api/v2/public"

# ---- CoinGecko ----
COINGECKO_GLOBAL_URL = "https://api.coingecko.com/api/v3/global"

# ---- Macro (yfinance tickers) ----
MACRO_TICKERS = {
    "DX-Y.NYB": "dxy",
    "^TNX":     "us10y",
    "GC=F":     "gold",
    "^GSPC":    "sp500",
}

# ---- Staleness thresholds (minutes) ----
STALENESS_THRESHOLDS = {
    # v3 core
    "basis": 60,
    "order_book": 60,
    "open_interest": 60,
    "premium_index": 60,
    "funding_rate": 720,    # 12h
    "tick_liq": 60,
    "liq_1h_agg": 120,     # 2h
    "whale_alerts": 240,    # 4h
    # Additional collectors
    "cvd": 60,
    "long_short_ratio": 60,
    "taker_volume": 60,
    "index_klines": 60,
    "mark_klines": 60,
    "macro": 1440,          # 24h (daily data)
    "btc_dominance": 60,
    "deribit_options": 60,
    "option_instruments": 1440,  # daily
    "option_greeks": 60,
    "fear_greed": 1440,     # daily
    "news": 240,            # 4h
    "etf_flows": 1440,      # daily (only on trading days)
    # Per-coin alt collectors (v2)
    "taker_ratio_alt": 60,
    "ls_ratio_alt": 60,
    "top_trader_ls_alt": 60,
    "order_book_alt": 60,
    "mark_klines_alt": 60,
    "basis_alt": 60,
    "cvd_alt": 60,
}

# ---- Collector retry config ----
MAX_RETRIES = 3
RETRY_BACKOFF_SEC = 1.5
