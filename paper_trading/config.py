"""
Paper Trading Configuration
============================
Constants, coin configs (best params from grid search), composite weights.

v3 = cleaned coins (proven/promising in paper trading)
v4 = new coins from 100-coin screening (to validate in paper trading)
v3 and v4 coin sets do NOT overlap.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ---- Paths ----
BASE_DIR = Path(__file__).parent
STATE_DIR = BASE_DIR / "state"
LOG_DIR = BASE_DIR / "logs"
STATE_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

SQLITE_PATH = STATE_DIR / "paper_trades.db"

# ---- Binance Futures Testnet ----
BINANCE_TESTNET_KEY = os.getenv("BINANCE_TESTNET_KEY", "")
BINANCE_TESTNET_SECRET = os.getenv("BINANCE_TESTNET_SECRET", "")

# ---- Trading constants ----
INIT_EQUITY = 5_000.0  # Testnet wallet balance
# Epoch after which trades count (reset on DB wipe to ignore old Binance history)
PAPER_TRADING_START_MS = 1773760000000  # 2026-03-17 15:06 UTC (reset #2)
V6_DEPLOY_MS = 1774245600000  # 2026-03-23 06:00 UTC (v6 liq-only deployment + coin reshuffles)
LEVERAGE = 2.0  # v3: 2x leverage (matching backtest)
BUDGET_PER_COIN = 100.0  # $100 per coin -> $200 notional w/ 2x lev
FEE_BPS = 2.0
SLIP_BPS = 1.5
FEE = FEE_BPS / 10_000
SLIP = SLIP_BPS / 10_000

# ---- Data ----
WARMUP_BARS = 100
EVAL_DELAY_SEC = 60  # wait 60s after candle close before evaluating
MAX_HOLD_BARS = 96   # same as backtest default
HYSTERESIS_BAND = 1.5  # default for v3/v5 (continuous score, original setting)
MIN_BARS_BEFORE_FLIP = 4  # default for v3/v5 (original setting)
FLIP_MODE = "reverse"     # default for v3/v5 (original behavior)
FLIP_COOLDOWN_EXTRA = 0   # default for v3/v5

# ---- LONG disabled (2026-04-04) ----
# LONG WR 31.7% over 18 days, -$21 PnL. Strong bull BTC score → worst WR (29.4%).
# Signal is structurally broken for LONG. SHORT-only until LONG signal is redesigned.
LONG_ENABLED = False

# Per-model flip config (Tournament R3 validated: v6 only benefits from champion settings)
# v3: FLIP barely profitable (54% WR), champion makes FLIP PnL negative → keep original
# v5: champion neutral (-0.5%), hyst_2.0 best but marginal → keep original
# v6: champion +12.1%, hyst=3.0 critical for binary score → use champion
# SURGERY 2026-04-10: reverse → exit_only
# SIGNAL_FLIP "reverse" lost -$1,429 over 730 trades (WR 23.6%)
# "exit_only" still cuts losses but stops bleeding by NOT opening opposite
# cd_extra=4 prevents re-entry for 4 bars (1h) after flip exit
FLIP_CONFIG = {
    "v3": {"hysteresis_band": 1.5, "flip_mode": "exit_only", "min_bars": 4, "cd_extra": 4},
    "v5": {"hysteresis_band": 1.5, "flip_mode": "exit_only", "min_bars": 4, "cd_extra": 4},
}

# ---- Funding rate cost (Binance perps, ~0.01% per 8h) ----
FUNDING_RATE = 0.0001      # 0.01% per funding period
FUNDING_BARS = 32          # every 8h = 32 x 15min bars

# ---- Database ----
DB_PARAMS = {
    "dbname": os.getenv("PG_DB", "smart_trading"),
    "user": os.getenv("PG_USER", "postgres"),
    "password": os.getenv("PG_PASS", "P@ssw0rd"),
    "host": os.getenv("PG_HOST", "localhost"),
    "port": os.getenv("PG_PORT", "5432"),
}

# ══════════════════════════════════════════════════════════════
# v3 COINS -- SHRUNK 2026-04-15: kept only top-5 positive PnL
# Rationale: 10/13 coins HP<30 PAUSED, ARIA carries 92% of total PnL.
# Removing marginal/negative coins reduces drag and concentrates risk
# on coins the system actually trades profitably.
# Daily report 2026-04-14 per-coin PnL used as ground truth.
# ══════════════════════════════════════════════════════════════
COINS_V3 = [
    "ARIA",       # +$626.33  WR 66%  — carries the port (custom SL/TP below)
    "BEAT",       # +$52.98   WR 50%
    "PIXEL",      # +$32.94   WR 53%
    "ADA",        # +$26.37   WR 57%
    "XRP",        # +$22.45   WR 55%
]  # 5 coins (shrunk from 13)

# Removed 2026-04-15 (marginal / negative PnL):
# RENDER +$2.40, SUI +$2.07, ETH +$2.99, SOL -$2.86, BTC -$3.66,
# AAVE -$4.00, 1000BONK -$4.02, AXS -$9.17

# ══════════════════════════════════════════════════════════════
# v5 COINS -- SURGERY 2026-04-10: emptied
# ARIA moved to v3 (custom config) — v5 SL=15 ATR caused -$42 single losses
# AAVE moved to v3 — barely profitable on v5 (+$2.49 in 24 days)
# ══════════════════════════════════════════════════════════════
COINS_V5 = []  # disabled — ARIA + AAVE moved to v3

# ══════════════════════════════════════════════════════════════
# v6 REMOVED entirely (2026-04-04) -- Liq-only model failed in paper
# 13/15 coins negative, -$389 in 8 days, systematic failure in sideways market
# v4 coins also absorbed here were all negative
# ══════════════════════════════════════════════════════════════
COINS_V4 = []  # DISABLED — kept for backward compat with imports
COINS_V6 = []  # DISABLED — kept for backward compat with imports

# All coins that were ever traded (for dashboard to fetch historical trades/income)
COINS_REMOVED = [
    # ex-v3 (negative PnL, removed 2026-04-04)
    "DOT", "FIL", "NEAR", "ARB",
    # ex-v3 (marginal/negative, removed 2026-04-15 in shrink)
    "BTC", "SUI", "RENDER", "AXS", "SOL", "ETH", "1000BONK", "AAVE",
    # ex-v5 (negative PnL)
    "FARTCOIN", "GALA", "AVAX", "UNI", "SEI", "DOGE", "ONDO",
    "1000SHIB", "BNB", "WIF", "CRV", "TAO", "ACX",
    # ex-v6 (all removed)
    "OGN", "SAHARA", "ASTER", "LTC", "ZRO", "NAORIS", "1000PEPE",
    "JCT", "DEGO", "HYPE", "PENGU", "LINK", "BARD", "BANANAS31", "PIPPIN",
]
# All coins = v3 + v5 (13 total, down from 46)
COINS = COINS_V3 + COINS_V5
COINS_ALL_EVER = COINS + COINS_REMOVED  # for historical lookups

# Default params for v3 coins
# Updated 2026-03-16: SL 3.0→10.0 (mission #005: SL=0% WR in both backtest+paper)
_DEFAULT_CONFIG = {
    "use_alt_pa_filter": False,
    "sl_atr_mult": 10.0,
    "tp_atr_mult": 5.0,
    "trail_atr_mult": 1.5,         # trailing stop: 1.5 ATR from peak/trough (was 0.5, too tight in live)
    "trail_activate_atr": 1.0,     # activate after 1.0 ATR profit (was 0.5)
    "cooldown_bars": 4,
    "threshold": 3.0,
}

# Default params for v5 coins (tournament champion config)
_V5_DEFAULT_CONFIG = {
    "use_alt_pa_filter": False,
    "sl_atr_mult": 15.0,
    "tp_atr_mult": 12.0,
    "trail_atr_mult": 1.5,         # trailing stop: 1.5 ATR from peak/trough (was 0.5, too tight in live)
    "trail_activate_atr": 1.0,     # activate after 1.0 ATR profit (was 0.5)
    "cooldown_bars": 4,
    "threshold": 3.0,
}

# v6 config kept for backward compat (research scripts import it)
_V6_DEFAULT_CONFIG = {
    "use_alt_pa_filter": False,
    "sl_atr_mult": 25.0,
    "tp_atr_mult": 20.0,
    "trail_atr_mult": 1.5,
    "trail_activate_atr": 1.0,
    "cooldown_bars": 4,
    "threshold": 3.0,
}

# ---- Per-coin configs (best params from grid search OOS results) ----
COIN_CONFIGS = {
    # === Grid-searched hardcoded configs (kept for coins still in COINS_V3) ===
    "XRP": {
        "symbol": "XRPUSDT",
        "model": "v3",
        "threshold": 3.5,
        "use_alt_pa_filter": False,
        "sl_atr_mult": 10.0,
        "tp_atr_mult": 5.0,
        "trail_atr_mult": 1.5,
        "trail_activate_atr": 1.0,
        "cooldown_bars": 4,
    },
    "ADA": {
        "symbol": "ADAUSDT",
        "model": "v3",
        "threshold": 3.5,
        "use_alt_pa_filter": False,
        "sl_atr_mult": 10.0,
        "tp_atr_mult": 5.0,
        "trail_atr_mult": 1.5,
        "trail_activate_atr": 1.0,
        "cooldown_bars": 4,
    },
    # SURGERY 2026-04-10: ARIA custom — was v5(SL=15,TP=12), now v3 with tighter SL
    # Reason: single ARIA losses hit -$42 (Apr 9), -$55 (Mar 30) due to wide SL.
    # Keep TP wider than default v3 since ARIA makes big moves when right.
    "ARIA": {
        "symbol": "ARIAUSDT",
        "model": "v3",
        "threshold": 3.0,
        "use_alt_pa_filter": False,
        "sl_atr_mult": 8.0,         # tighter than v3 default (10) — cap max loss
        "tp_atr_mult": 8.0,         # wider than v3 default (5) — let winners run
        "trail_atr_mult": 1.5,
        "trail_activate_atr": 1.0,
        "cooldown_bars": 4,
    },
}

# === Generate configs for remaining v3 coins (not in COIN_CONFIGS yet) ===
for _coin in COINS_V3:
    if _coin not in COIN_CONFIGS:
        COIN_CONFIGS[_coin] = {
            "symbol": f"{_coin}USDT",
            "model": "v3",
            **_DEFAULT_CONFIG,
        }

# === Generate configs for v5 coins ===
for _coin in COINS_V5:
    COIN_CONFIGS[_coin] = {
        "symbol": f"{_coin}USDT",
        "model": "v5",
        **_V5_DEFAULT_CONFIG,
    }

# ---- BTC composite score weights ----
# v3 optimal weights (mega discovery 2026-03-09)
# Removed: taker_ratio, ls_ratio, fear_greed (hurt or redundant)
# Added: ob_combined, basis_contrarian, tick_liq
COMPOSITE_WEIGHTS = {
    # OI divergence (weight 0.5)
    "w_oi_bull": 0.25, "w_oi_capit": 0.25, "w_oi_weak": 0.25, "w_oi_bear": 0.25,
    # Funding rate (weight 2.0)
    "w_fr_neg": 2.0, "w_fr_pos": 2.0,
    # Whale alerts (weight 1.5)
    "w_whale_bull": 1.5, "w_whale_bear": 1.5,
    # Liquidation cascades (weight 2.0)
    "w_liq_bull": 2.0, "w_liq_bear": 2.0,
    # ETF flows (weight 1.0)
    "w_etf_bull": 1.0, "w_etf_bear": 1.0,
}

# New v3 factors with standalone weights (passed directly to score functions)
V3_EXTRA_WEIGHTS = {
    "ob_combined": 2.0,
    "basis_contrarian": 1.5,
    "tick_liq": 2.0,
}

# ---- v5 BTC composite score weights (Tournament Round 1 champion) ----
# Key changes: liq 2.0→5.0, tick_liq 2.0→3.0, ob kept at 2.0
V5_COMPOSITE_WEIGHTS = {
    "w_oi_bull": 0.25, "w_oi_capit": 0.25, "w_oi_weak": 0.25, "w_oi_bear": 0.25,
    "w_fr_neg": 2.0, "w_fr_pos": 2.0,
    "w_whale_bull": 1.5, "w_whale_bear": 1.5,
    "w_liq_bull": 5.0, "w_liq_bear": 5.0,   # was 2.0 → KEY CHANGE
    "w_etf_bull": 1.0, "w_etf_bear": 1.0,
}

V5_EXTRA_WEIGHTS = {
    "ob_combined": 2.0,        # kept at 2.0 (lower = less noise)
    "basis_contrarian": 1.5,   # unchanged
    "tick_liq": 3.0,           # was 2.0 → +1.0
}

# ---- v6 Liq-Only Architecture (Tournament Round 2, 2026-03-22) ----
# Key change: DROP all non-liquidation factors, cascade threshold 3.0x → 1.1x
# Per-coin backtest: $69,701 (+35% vs v5) | Realistic portfolio: $30,941 (+29% vs v5)
# Validated: all periods positive, 33/33 coins v6>v5, walk-forward stable
V6_CASCADE_MULT = 1.1          # was 3.0 → KEY CHANGE (+$14K PnL alone)
V6_LIQ_WEIGHT = 8.0            # saturates at 8.0 (binary signal)
V6_TICK_WEIGHT = 8.0            # net > 3 threshold
V6_TICK_NET_THRESHOLD = 3       # higher = fewer but better quality signals
V6_SL = 25.0                    # wider SL for full mean-reversion
V6_TP = 20.0                    # wider TP
# Note: v6 does NOT need extreme_conf3 filter (self-cleaning architecture)

# V6 Cascade Quality Sizing (Mission 014)
# Higher displacement / cascade magnitude → larger position size
V6_SIZE_MULT_DEFAULT = 1.0     # base multiplier
V6_SIZE_MULT_DISP_01 = 1.2    # displacement >= 0.1%
V6_SIZE_MULT_DISP_03 = 1.5    # displacement >= 0.3%
V6_SIZE_MULT_CASCADE_5X = 0.3  # bonus for cascade >= 5x MA
V6_SIZE_MULT_MAX = 2.0         # cap

# ---- Extreme Confluence Filter (Mission 013, validated 2026-03-22) ----
# Skip NEW entries when vol regime = Extreme AND active_factors < 3
# Backtest: PnL +$15,814 (+13.7%), Calmar +5-107%, across all 46 coins
EXTREME_CONF3_ENABLED = True
EXTREME_CONF3_MIN_FACTORS = 3   # require >= 3 factors active in Extreme vol
VOL_REGIME_LOOKBACK = 96        # 24h rolling window for realized vol (96 x 15m bars)
# Fixed vol regime thresholds (from 15-month OOS quantiles in backtest)
# More robust than dynamic quantiles with only 100 warmup bars
VOL_REGIME_THRESHOLDS = {
    "q25": 0.2835,   # below = Low
    "q75": 0.5045,   # below = Normal
    "q90": 0.6644,   # below = High, above = Extreme
}

# ---- Coin Health Monitor (2026-03-22) ----
# Monitor-only mode: scores all coins but NEVER blocks paper trading
# Health data used for real-trade coin selection (not paper trading filtering)
HEALTH_ENABLED = True             # compute + log health scores
HEALTH_BLOCK_ENABLED = False      # False = monitor-only, True = auto-pause (for real trading)
HEALTH_PAUSE_THRESHOLD = 30      # score < 30 = PAUSED status
HEALTH_RESUME_THRESHOLD = 50     # score >= 50 = unpause (hysteresis)
HEALTH_MIN_TRADES = 5            # need at least 5 trades before scoring (COLD_START)
HEALTH_SHORT_WINDOW = 10         # rolling window for recent metrics (last N trades)
HEALTH_MIN_PAUSE_HOURS = 24      # minimum pause duration before unpause allowed

# ---- Volatility Spike Config (grid-searched 2026-03-15) ----
SPIKE_ENABLED = True  # Set False to disable spike overlay
SPIKE_CONFIG = {
    "range_z_thr": 1.5,        # range z-score threshold for spike detection
    "vol_ratio_thr": 2.0,      # volume ratio threshold for spike detection
    "liq_mult": 3.0,           # liq_total > liq_total_ma * this = spike
    "liq_mult_extreme": 5.0,   # extreme liq = contrarian mode
    "displacement_thr": 2.0,   # ATR distance from EMA21 for contrarian
    "rsi_high": 75,            # RSI above = contrarian
    "rsi_low": 25,             # RSI below = contrarian
    "contrarian_reduction": 0.5,  # threshold reduction for contrarian signals
    "momentum_reduction": 0.8,   # threshold reduction for momentum signals
}
