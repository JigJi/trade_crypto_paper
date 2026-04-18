"""
Tournament Round 5: VOLATILITY REGIME TRADING
==============================================
Focus: Trade ONLY during high-volatility periods (strong price moves).

Two core strategies:
  1. MOMENTUM ("ตามรถ") — ride the wave when price moves strongly
  2. REBOUND ("สวนกลับ") — fade extreme moves expecting mean reversion

Philosophy: Most of the time, markets are quiet and noisy. The EDGE is
concentrated in high-volatility windows. Can we isolate those windows
and extract more profit with specialized entry logic?

Phases:
  Phase 1: Volatility Detection — which method best identifies "strong moves"?
    - ATR z-score spike
    - Volume spike
    - Price displacement from EMA
    - Return z-score (multi-bar)
    - Liquidation cascade
    - Combined detectors

  Phase 2: Momentum Strategies — ride the wave
    - Follow BTC direction during vol spike
    - Follow liquidation direction
    - Breakout: price breaks recent range
    - Trend continuation: EMA alignment + vol

  Phase 3: Rebound Strategies — fade extreme moves
    - RSI extreme reversal
    - Extreme displacement reversal
    - Liquidation flush contrarian
    - Exhaustion candle (vol + range + reversal)

  Phase 4: Best combined + cross-validation

Baseline: v6 + R3 champion ($83,768)
"""

import sys, os, json, time, traceback
from pathlib import Path
from datetime import datetime

BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR))
os.chdir(str(BASE_DIR))

import pandas as pd
import numpy as np
from backtest_15m_btc_led_alts import (
    fetch_binance_15m, load_btc_db_data, build_alt_technicals,
    run_backtest, calc_metrics,
    INIT_EQUITY, BUDGET_USDT, LEVERAGE, FEE, SLIP,
)
from signal_core import (
    build_btc_features, compute_btc_composite_score_v6,
    detect_spike, classify_spike_mode,
    DEFAULT_SPIKE_CONFIG,
)

# -- Config ----------------------------------------------
OOS_START = "2025-01-01"
OOS_END   = "2026-03-26"
COINS = ["BTC", "XRP", "ADA", "DOT", "SUI", "FIL"]

# v6 + R3 champion config (baseline)
V6_CONFIGS = {
    "BTC":  {"threshold": 2.5, "sl": 25.0, "tp": 20.0, "trail": 99, "trail_act": 99, "cd": 4},
    "XRP":  {"threshold": 3.5, "sl": 25.0, "tp": 20.0, "trail": 99, "trail_act": 99, "cd": 4},
    "ADA":  {"threshold": 3.5, "sl": 25.0, "tp": 20.0, "trail": 99, "trail_act": 99, "cd": 4},
    "DOT":  {"threshold": 3.0, "sl": 25.0, "tp": 20.0, "trail": 99, "trail_act": 99, "cd": 4},
    "SUI":  {"threshold": 3.0, "sl": 25.0, "tp": 20.0, "trail": 99, "trail_act": 99, "cd": 4},
    "FIL":  {"threshold": 3.0, "sl": 25.0, "tp": 20.0, "trail": 99, "trail_act": 99, "cd": 4},
}

# R3 champion settings
R3_FLIP_MODE = "exit_only"
R3_HYSTERESIS = 3.0
R3_CD_EXTRA = 4

REGIMES = {
    "BULL":   ("2024-10-01", "2025-01-20"),
    "BEAR":   ("2025-01-20", "2025-04-01"),
    "FLAT":   ("2025-04-01", "2025-07-01"),
    "MIXED":  ("2025-07-01", "2025-10-01"),
    "RECENT": ("2025-10-01", "2026-03-26"),
}

RESULTS_DIR = Path(__file__).parent
RESULTS_FILE = RESULTS_DIR / "results.json"

# Dead zone
DEAD_ZONE_START = 23
DEAD_ZONE_END = 6


# ==========================================================
# DATA LOADING
# ==========================================================

print("=" * 70)
print("TOURNAMENT ROUND 5: VOLATILITY REGIME TRADING")
print("=" * 70)

t0 = time.time()
btc_ohlcv = fetch_binance_15m("BTCUSDT", years=3)
db_data = load_btc_db_data()
btc_df = build_btc_features(btc_ohlcv, db_data)
print(f"BTC features: {len(btc_df)} bars ({time.time()-t0:.1f}s)")

# v6 BTC score (for baseline comparison)
v6_score = compute_btc_composite_score_v6(btc_df)
v6_score_ts = pd.Series(v6_score.values, index=btc_df["ts"].values)
print(f"V6 BTC score: range [{v6_score.min():.1f}, {v6_score.max():.1f}]")

# Load alt data
alt_data = {}
for coin in COINS:
    sym = f"{coin}USDT"
    ohlcv = fetch_binance_15m(sym, years=3)
    alt_data[coin] = build_alt_technicals(ohlcv)
    print(f"  {coin}: {len(alt_data[coin])} bars")

print(f"Data loaded in {time.time()-t0:.1f}s\n")


# ==========================================================
# BTC VOLATILITY FEATURES (enhanced for this tournament)
# ==========================================================

def build_btc_vol_features(btc_df):
    """Build enhanced volatility features on BTC for regime detection."""
    df = btc_df.copy()

    # Already have: range_z, vol_ratio, ema21_dist, rsi, atr

    # 1. Return z-score (multi-bar)
    df["ret_1"] = df["close"].pct_change(1)
    df["ret_4"] = df["close"].pct_change(4)   # 1 hour
    df["ret_8"] = df["close"].pct_change(8)   # 2 hours
    df["ret_16"] = df["close"].pct_change(16)  # 4 hours

    for col in ["ret_1", "ret_4", "ret_8", "ret_16"]:
        ma = df[col].rolling(96).mean()
        std = df[col].rolling(96).std().clip(lower=1e-8)
        df[f"{col}_z"] = (df[col] - ma) / std

    # 2. Body ratio (how much of the candle is body vs wick)
    df["body"] = abs(df["close"] - df["open"])
    df["range"] = df["high"] - df["low"]
    df["body_ratio"] = df["body"] / df["range"].clip(lower=1e-8)

    # 3. ATR z-score
    atr_ma = df["atr"].rolling(96).mean()
    atr_std = df["atr"].rolling(96).std().clip(lower=1e-8)
    df["atr_z"] = (df["atr"] - atr_ma) / atr_std

    # 4. Bollinger bandwidth (volatility expansion)
    bb_ma = df["close"].rolling(20).mean()
    bb_std = df["close"].rolling(20).std()
    df["bb_upper"] = bb_ma + 2 * bb_std
    df["bb_lower"] = bb_ma - 2 * bb_std
    df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / bb_ma.clip(lower=1e-8)
    bb_w_ma = df["bb_width"].rolling(96).mean()
    bb_w_std = df["bb_width"].rolling(96).std().clip(lower=1e-8)
    df["bb_width_z"] = (df["bb_width"] - bb_w_ma) / bb_w_std

    # 5. Consecutive direction bars
    df["is_bull_bar"] = (df["close"] > df["open"]).astype(int)
    df["is_bear_bar"] = (df["close"] < df["open"]).astype(int)
    # Rolling count of consecutive same-direction bars
    bull_streak = df["is_bull_bar"].copy()
    bear_streak = df["is_bear_bar"].copy()
    for i in range(1, len(df)):
        if bull_streak.iloc[i] == 1 and bull_streak.iloc[i-1] > 0:
            bull_streak.iloc[i] = bull_streak.iloc[i-1] + 1
        if bear_streak.iloc[i] == 1 and bear_streak.iloc[i-1] > 0:
            bear_streak.iloc[i] = bear_streak.iloc[i-1] + 1
    df["bull_streak"] = bull_streak
    df["bear_streak"] = bear_streak

    # 6. Distance from 4h high/low
    df["high_16"] = df["high"].rolling(16).max()
    df["low_16"] = df["low"].rolling(16).min()
    df["range_16"] = df["high_16"] - df["low_16"]
    df["pos_in_range_16"] = (df["close"] - df["low_16"]) / df["range_16"].clip(lower=1e-8)

    return df


btc_vol = build_btc_vol_features(btc_df)
print(f"BTC vol features built: {len(btc_vol)} bars, "
      f"{len([c for c in btc_vol.columns if c not in btc_df.columns])} new columns")


# ==========================================================
# SIGNAL GENERATION WITH HYSTERESIS (from R3/R4)
# ==========================================================

def gen_signal_with_options(btc_score_ts, alt_df, entry_threshold,
                            hysteresis_band=0.0, use_alt_pa=False,
                            confirm_bars=1):
    """Generate signal with hysteresis band + confirmation bars."""
    exit_threshold = entry_threshold - hysteresis_band

    btc_score_df = btc_score_ts.reset_index()
    btc_score_df.columns = ["ts", "btc_score"]
    alt = alt_df.copy().sort_values("ts")
    alt = pd.merge_asof(alt, btc_score_df.sort_values("ts"),
                        on="ts", direction="backward", tolerance=pd.Timedelta("30min"))
    alt["btc_score"] = alt["btc_score"].fillna(0)

    scores = alt["btc_score"].values
    n = len(scores)
    sig = np.zeros(n, dtype=int)
    state = 0
    consec_bull = 0
    consec_bear = 0

    for i in range(n):
        s = scores[i]
        if s >= entry_threshold:
            consec_bull += 1
        else:
            consec_bull = 0
        if s <= -entry_threshold:
            consec_bear += 1
        else:
            consec_bear = 0

        if state == 0:
            if consec_bull >= confirm_bars:
                state = 1
            elif consec_bear >= confirm_bars:
                state = -1
        elif state == 1:
            if consec_bear >= confirm_bars:
                state = -1
            elif s < exit_threshold:
                state = 0
        else:
            if consec_bull >= confirm_bars:
                state = 1
            elif s > -exit_threshold:
                state = 0
        sig[i] = state

    signal = pd.Series(sig, index=alt.index)

    if use_alt_pa:
        alt_bull_pa = (alt["close"] > alt["ema9"]) & (alt["ema9"] > alt["ema21"])
        alt_bear_pa = (alt["close"] < alt["ema9"]) & (alt["ema9"] < alt["ema21"])
        alt_vol_ok = alt["vol_ratio"] > 0.8
        signal[(signal == 1) & ~(alt_bull_pa & alt_vol_ok)] = 0
        signal[(signal == -1) & ~(alt_bear_pa & alt_vol_ok)] = 0

    return signal, alt


# ==========================================================
# BACKTEST WITH FLIP MODE (from R3/R4)
# ==========================================================

def run_backtest_flip(df, signals, sl_atr_mult=25.0, tp_atr_mult=20.0,
                      max_hold_bars=96, cooldown_bars=4,
                      flip_mode="exit_only", min_bars_before_flip=0,
                      flip_cooldown_extra=4):
    """Backtest with signal flip handling (R3 champion)."""
    sig = signals.shift(1).fillna(0).astype(int).values
    atrs = df["atr"].values
    opens = df["open"].values
    highs = df["high"].values
    lows = df["low"].values
    closes = df["close"].values
    times = df["ts"].values

    n = len(df)
    records = []
    equity = INIT_EQUITY
    position = 0
    entry_i = entry_px = entry_atr = qty = fee_in = 0
    peak = trough = 0.0
    last_exit_i = -cooldown_bars - 1
    max_notional = BUDGET_USDT * LEVERAGE * 2

    for i in range(n):
        if position == 0 and sig[i] != 0 and (i - last_exit_i) > cooldown_bars:
            raw_px = opens[i]
            cur_atr = atrs[i] if not np.isnan(atrs[i]) else 0
            if raw_px <= 0 or cur_atr <= 0 or cur_atr / raw_px < 0.0005:
                continue
            qty = (BUDGET_USDT * LEVERAGE) / raw_px
            entry_px = raw_px * (1 + SLIP) if sig[i] == 1 else raw_px * (1 - SLIP)
            entry_atr = cur_atr
            fee_in = entry_px * qty * FEE
            position = sig[i]
            entry_i = i
            peak = entry_px
            trough = entry_px
            continue

        if position != 0:
            h, l, c, o = highs[i], lows[i], closes[i], opens[i]
            atr = entry_atr
            if position == 1:
                peak = max(peak, h)
                sl_level = entry_px - sl_atr_mult * atr
                tp_level = entry_px + tp_atr_mult * atr
            else:
                trough = min(trough, l)
                sl_level = entry_px + sl_atr_mult * atr
                tp_level = entry_px - tp_atr_mult * atr

            exit_px = exit_reason = None
            if position == 1:
                if l <= sl_level: exit_px, exit_reason = sl_level, "SL"
                elif h >= tp_level: exit_px, exit_reason = tp_level, "TP"
            else:
                if h >= sl_level: exit_px, exit_reason = sl_level, "SL"
                elif l <= tp_level: exit_px, exit_reason = tp_level, "TP"

            if exit_px is None and (i - entry_i) >= max_hold_bars:
                exit_px, exit_reason = c, "TIMEOUT"

            if exit_px is None and flip_mode != "disabled":
                if sig[i] != 0 and sig[i] != position:
                    bars_held = i - entry_i
                    if bars_held >= min_bars_before_flip:
                        exit_px, exit_reason = o, "SIGNAL_FLIP"

            if exit_px is not None:
                exit_px_f = exit_px * (1 - SLIP) if position == 1 else exit_px * (1 + SLIP)
                fee_out = exit_px_f * qty * FEE
                pnl_gross = (exit_px_f - entry_px) * qty * position
                pnl_net = pnl_gross - fee_in - fee_out
                equity += pnl_net
                records.append({
                    "entry_idx": entry_i, "exit_idx": i,
                    "entry_time": times[entry_i], "exit_time": times[i],
                    "dir": "L" if position == 1 else "S",
                    "entry_price": entry_px, "exit_price": exit_px_f,
                    "qty": qty, "pnl_net": pnl_net,
                    "equity_after": equity, "exit_reason": exit_reason,
                    "holding_bars": i - entry_i,
                })
                if exit_reason == "SIGNAL_FLIP" and flip_mode == "exit_only":
                    last_exit_i = i + flip_cooldown_extra
                else:
                    last_exit_i = i
                position = 0

    return pd.DataFrame(records)


# ==========================================================
# VOLATILITY REGIME SIGNAL GENERATORS
# ==========================================================

def detect_vol_regime(btc_vol, method, params):
    """Detect high-volatility bars on BTC using specified method.

    Returns: boolean Series aligned to btc_vol index (True = high vol)
    """
    if method == "range_z":
        # Intrabar range z-score
        thr = params.get("threshold", 1.5)
        return btc_vol["range_z"] > thr

    elif method == "vol_spike":
        # Volume spike
        thr = params.get("threshold", 2.0)
        return btc_vol["vol_ratio"] > thr

    elif method == "displacement":
        # Price displacement from EMA21 in ATR units
        thr = params.get("threshold", 1.5)
        return btc_vol["ema21_dist"].abs() > thr

    elif method == "ret_z":
        # Multi-bar return z-score
        lookback = params.get("lookback", 4)
        thr = params.get("threshold", 2.0)
        col = f"ret_{lookback}_z"
        if col in btc_vol.columns:
            return btc_vol[col].abs() > thr
        return pd.Series(False, index=btc_vol.index)

    elif method == "atr_z":
        # ATR z-score (sustained vol expansion)
        thr = params.get("threshold", 1.5)
        return btc_vol["atr_z"] > thr

    elif method == "liq_cascade":
        # Liquidation cascade
        mult = params.get("mult", 2.0)
        if "liq_total" in btc_vol.columns and "liq_total_ma" in btc_vol.columns:
            lt = btc_vol["liq_total"].fillna(0)
            lt_ma = btc_vol["liq_total_ma"].fillna(1)
            return lt > (lt_ma * mult)
        return pd.Series(False, index=btc_vol.index)

    elif method == "bb_expansion":
        # Bollinger band width expansion
        thr = params.get("threshold", 1.5)
        return btc_vol["bb_width_z"] > thr

    elif method == "combined_any2":
        # At least 2 of: range_z, vol_spike, displacement, ret_z
        signals = []
        signals.append(btc_vol["range_z"] > params.get("range_z_thr", 1.5))
        signals.append(btc_vol["vol_ratio"] > params.get("vol_thr", 2.0))
        signals.append(btc_vol["ema21_dist"].abs() > params.get("disp_thr", 1.5))
        if "ret_4_z" in btc_vol.columns:
            signals.append(btc_vol["ret_4_z"].abs() > params.get("ret_z_thr", 2.0))
        combined = sum(s.astype(int) for s in signals)
        return combined >= params.get("min_count", 2)

    elif method == "combined_any3":
        # At least 3 of: range_z, vol_spike, displacement, ret_z, liq
        signals = []
        signals.append(btc_vol["range_z"] > params.get("range_z_thr", 1.5))
        signals.append(btc_vol["vol_ratio"] > params.get("vol_thr", 2.0))
        signals.append(btc_vol["ema21_dist"].abs() > params.get("disp_thr", 1.5))
        if "ret_4_z" in btc_vol.columns:
            signals.append(btc_vol["ret_4_z"].abs() > params.get("ret_z_thr", 2.0))
        if "liq_total" in btc_vol.columns and "liq_total_ma" in btc_vol.columns:
            lt = btc_vol["liq_total"].fillna(0)
            lt_ma = btc_vol["liq_total_ma"].fillna(1)
            signals.append(lt > (lt_ma * params.get("liq_mult", 2.0)))
        combined = sum(s.astype(int) for s in signals)
        return combined >= params.get("min_count", 3)

    return pd.Series(False, index=btc_vol.index)


def gen_momentum_signal(btc_vol, vol_mask, alt_df, config):
    """Generate MOMENTUM signal: follow BTC direction during vol spike.

    Strategy: When volatility spike detected, enter in direction of recent move.
    """
    method = config.get("direction_method", "ret")
    lookback = config.get("lookback", 4)
    min_ret = config.get("min_ret", 0.003)  # minimum return to confirm direction
    cooldown = config.get("cooldown", 4)
    use_ema_align = config.get("use_ema_align", False)
    use_streak = config.get("use_streak", False)
    min_streak = config.get("min_streak", 3)

    # Align vol_mask to alt timestamps
    vol_mask_ts = pd.Series(vol_mask.values, index=btc_vol["ts"].values)
    vol_df = vol_mask_ts.reset_index()
    vol_df.columns = ["ts", "is_vol"]

    # Also align BTC features we need
    btc_feats = btc_vol[["ts", "ret_1", "ret_4", "ret_8", "ret_16",
                          "ema9", "ema21", "close", "rsi",
                          "bull_streak", "bear_streak"]].copy()

    alt = alt_df.copy().sort_values("ts")
    alt = pd.merge_asof(alt, vol_df.sort_values("ts"),
                        on="ts", direction="backward", tolerance=pd.Timedelta("30min"))
    alt = pd.merge_asof(alt, btc_feats.sort_values("ts"),
                        on="ts", direction="backward", tolerance=pd.Timedelta("30min"),
                        suffixes=("", "_btc"))
    alt["is_vol"] = alt["is_vol"].fillna(False)

    # Determine BTC direction
    if method == "ret":
        ret_col = f"ret_{lookback}"
        if ret_col not in alt.columns:
            ret_col = "ret_4"
        direction = np.where(alt[ret_col] > min_ret, 1,
                   np.where(alt[ret_col] < -min_ret, -1, 0))
    elif method == "ema":
        direction = np.where(alt["close_btc"] > alt["ema9_btc"], 1,
                   np.where(alt["close_btc"] < alt["ema9_btc"], -1, 0))
    elif method == "streak":
        direction = np.where(alt["bull_streak"] >= min_streak, 1,
                   np.where(alt["bear_streak"] >= min_streak, -1, 0))
    else:
        direction = np.zeros(len(alt))

    # Additional filter: EMA alignment
    if use_ema_align:
        ema_bull = (alt["close_btc"] > alt["ema9_btc"]) & (alt["ema9_btc"] > alt["ema21_btc"])
        ema_bear = (alt["close_btc"] < alt["ema9_btc"]) & (alt["ema9_btc"] < alt["ema21_btc"])
        direction = np.where((direction == 1) & ~ema_bull, 0, direction)
        direction = np.where((direction == -1) & ~ema_bear, 0, direction)

    # Dead zone filter
    hour = alt["ts"].dt.hour
    is_dead = (hour >= DEAD_ZONE_START) | (hour < DEAD_ZONE_END)

    # Final signal: vol spike + direction confirmed + not dead zone
    signal = pd.Series(0, index=alt.index)
    is_vol = alt["is_vol"].values
    for i in range(len(alt)):
        if is_vol[i] and direction[i] != 0 and not is_dead.iloc[i]:
            signal.iloc[i] = int(direction[i])

    return signal, alt


def gen_rebound_signal(btc_vol, vol_mask, alt_df, config):
    """Generate REBOUND signal: fade extreme moves during vol spike.

    Strategy: When price has moved too far too fast, bet on mean reversion.
    The key is detecting EXHAUSTION — the move is overextended.
    """
    method = config.get("method", "displacement")
    cooldown = config.get("cooldown", 4)

    # Align vol_mask to alt timestamps
    vol_mask_ts = pd.Series(vol_mask.values, index=btc_vol["ts"].values)
    vol_df = vol_mask_ts.reset_index()
    vol_df.columns = ["ts", "is_vol"]

    btc_feats = btc_vol[["ts", "ret_1", "ret_4", "ret_8",
                          "ema21_dist", "rsi", "close",
                          "range_z", "vol_ratio",
                          "pos_in_range_16", "body_ratio"]].copy()

    alt = alt_df.copy().sort_values("ts")
    alt = pd.merge_asof(alt, vol_df.sort_values("ts"),
                        on="ts", direction="backward", tolerance=pd.Timedelta("30min"))
    alt = pd.merge_asof(alt, btc_feats.sort_values("ts"),
                        on="ts", direction="backward", tolerance=pd.Timedelta("30min"),
                        suffixes=("", "_btc"))
    alt["is_vol"] = alt["is_vol"].fillna(False)

    # Dead zone filter
    hour = alt["ts"].dt.hour
    is_dead = (hour >= DEAD_ZONE_START) | (hour < DEAD_ZONE_END)

    signal = pd.Series(0, index=alt.index)
    is_vol = alt["is_vol"].values

    if method == "displacement":
        # Extreme displacement from EMA21 -> contrarian
        disp_thr = config.get("disp_thr", 2.0)
        dist = alt["ema21_dist_btc"] if "ema21_dist_btc" in alt.columns else alt.get("ema21_dist", pd.Series(0, index=alt.index))
        dist = dist.fillna(0)
        for i in range(len(alt)):
            if is_vol[i] and not is_dead.iloc[i]:
                if dist.iloc[i] > disp_thr:
                    signal.iloc[i] = -1  # overbought -> short
                elif dist.iloc[i] < -disp_thr:
                    signal.iloc[i] = 1   # oversold -> long

    elif method == "rsi_extreme":
        # RSI extreme -> contrarian
        rsi_high = config.get("rsi_high", 75)
        rsi_low = config.get("rsi_low", 25)
        rsi = alt["rsi_btc"] if "rsi_btc" in alt.columns else alt.get("rsi", pd.Series(50, index=alt.index))
        rsi = rsi.fillna(50)
        for i in range(len(alt)):
            if is_vol[i] and not is_dead.iloc[i]:
                if rsi.iloc[i] > rsi_high:
                    signal.iloc[i] = -1
                elif rsi.iloc[i] < rsi_low:
                    signal.iloc[i] = 1

    elif method == "liq_flush":
        # Extreme liquidation flush -> contrarian (liquidated traders = exhaustion)
        if "liq_total" in btc_vol.columns and "liq_net" in btc_vol.columns:
            liq_feats = btc_vol[["ts", "liq_total", "liq_total_ma", "liq_net"]].copy()
            alt = pd.merge_asof(alt, liq_feats.sort_values("ts"),
                                on="ts", direction="backward", tolerance=pd.Timedelta("2h"),
                                suffixes=("", "_liq"))
            liq_mult = config.get("liq_mult", 5.0)
            lt = alt["liq_total"].fillna(0) if "liq_total" in alt.columns else pd.Series(0, index=alt.index)
            lt_ma = alt["liq_total_ma"].fillna(1) if "liq_total_ma" in alt.columns else pd.Series(1, index=alt.index)
            ln = alt["liq_net"].fillna(0) if "liq_net" in alt.columns else pd.Series(0, index=alt.index)
            for i in range(len(alt)):
                if is_vol[i] and not is_dead.iloc[i]:
                    if lt.iloc[i] > lt_ma.iloc[i] * liq_mult:
                        if ln.iloc[i] > 0:
                            signal.iloc[i] = -1  # long liq flush -> price already pumped -> short
                        elif ln.iloc[i] < 0:
                            signal.iloc[i] = 1   # short liq flush -> price already dumped -> long

    elif method == "exhaustion":
        # Combined exhaustion: vol spike + extreme displacement + reversal candle
        disp_thr = config.get("disp_thr", 1.5)
        dist = alt["ema21_dist_btc"] if "ema21_dist_btc" in alt.columns else alt.get("ema21_dist", pd.Series(0, index=alt.index))
        dist = dist.fillna(0)
        rsi = alt["rsi_btc"] if "rsi_btc" in alt.columns else alt.get("rsi", pd.Series(50, index=alt.index))
        rsi = rsi.fillna(50)
        rsi_high = config.get("rsi_high", 70)
        rsi_low = config.get("rsi_low", 30)

        for i in range(len(alt)):
            if is_vol[i] and not is_dead.iloc[i]:
                # Need displacement + RSI confirmation
                if dist.iloc[i] > disp_thr and rsi.iloc[i] > rsi_high:
                    signal.iloc[i] = -1
                elif dist.iloc[i] < -disp_thr and rsi.iloc[i] < rsi_low:
                    signal.iloc[i] = 1

    elif method == "range_extreme":
        # Price at extreme of 4h range -> contrarian
        pos_high = config.get("pos_high", 0.9)
        pos_low = config.get("pos_low", 0.1)
        pos = alt["pos_in_range_16"] if "pos_in_range_16" in alt.columns else pd.Series(0.5, index=alt.index)
        pos = pos.fillna(0.5)
        for i in range(len(alt)):
            if is_vol[i] and not is_dead.iloc[i]:
                if pos.iloc[i] > pos_high:
                    signal.iloc[i] = -1
                elif pos.iloc[i] < pos_low:
                    signal.iloc[i] = 1

    return signal, alt


# ==========================================================
# EXPERIMENT RUNNER
# ==========================================================

def run_experiment(name, description, signal_fn, signal_args,
                   sl=25.0, tp=20.0, max_hold=96, cooldown=4,
                   flip_mode="exit_only", flip_cd_extra=4,
                   per_coin_config=None):
    """Run one experiment across all coins, return results dict."""
    print(f"\n{'-'*60}")
    print(f"  {name}")
    print(f"  {description}")
    print(f"{'-'*60}")

    t_start = time.time()
    all_trades = []
    coin_results = {}

    for coin in COINS:
        cfg = (per_coin_config or {}).get(coin, {})
        c_sl = cfg.get("sl", sl)
        c_tp = cfg.get("tp", tp)
        c_cd = cfg.get("cd", cooldown)

        try:
            signal, merged_df = signal_fn(coin=coin, **signal_args)

            # Filter OOS period
            mask = (merged_df["ts"] >= OOS_START) & (merged_df["ts"] <= OOS_END)
            df_oos = merged_df[mask].reset_index(drop=True)
            sig_oos = signal[mask].reset_index(drop=True)

            if sig_oos.abs().sum() == 0:
                print(f"    {coin}: 0 signals")
                coin_results[coin] = {"pnl": 0, "trades": 0, "wr": 0, "sharpe": 0}
                continue

            trades = run_backtest_flip(df_oos, sig_oos,
                                       sl_atr_mult=c_sl, tp_atr_mult=c_tp,
                                       max_hold_bars=max_hold, cooldown_bars=c_cd,
                                       flip_mode=flip_mode,
                                       flip_cooldown_extra=flip_cd_extra)

            m = calc_metrics(trades, len(df_oos))
            coin_results[coin] = {
                "pnl": m["net_pnl"], "trades": m["total"],
                "wr": m["win_rate"], "sharpe": m["sharpe"],
                "n_long": m["n_long"], "n_short": m["n_short"],
                "wr_long": m["wr_long"], "wr_short": m["wr_short"],
            }

            # Exit reason breakdown
            if not trades.empty:
                exit_breakdown = {}
                for reason in trades["exit_reason"].unique():
                    subset = trades[trades["exit_reason"] == reason]
                    exit_breakdown[reason] = {
                        "count": len(subset),
                        "pnl": round(subset["pnl_net"].sum(), 2),
                        "wr": round(len(subset[subset["pnl_net"] > 0]) / max(len(subset), 1) * 100, 1),
                    }
                coin_results[coin]["exit_breakdown"] = exit_breakdown

            all_trades.append(trades)
            print(f"    {coin}: {m['total']} trades, WR {m['win_rate']:.1f}%, "
                  f"PnL ${m['net_pnl']:,.0f}, Sharpe {m['sharpe']:.1f}")

        except Exception as e:
            print(f"    {coin}: ERROR — {e}")
            traceback.print_exc()
            coin_results[coin] = {"pnl": 0, "trades": 0, "wr": 0, "sharpe": 0, "error": str(e)}

    # Aggregate
    total_pnl = sum(cr["pnl"] for cr in coin_results.values())
    total_trades = sum(cr["trades"] for cr in coin_results.values())
    total_wins = sum(
        cr["trades"] * cr["wr"] / 100 for cr in coin_results.values()
        if cr["trades"] > 0
    )
    total_wr = (total_wins / total_trades * 100) if total_trades > 0 else 0

    elapsed = time.time() - t_start
    print(f"\n  > TOTAL: ${total_pnl:,.0f} | {total_trades} trades | "
          f"WR {total_wr:.1f}% | {elapsed:.1f}s")

    return {
        "name": name,
        "description": description,
        "total_pnl": round(total_pnl, 2),
        "total_trades": total_trades,
        "win_rate": round(total_wr, 1),
        "coin_results": coin_results,
        "elapsed_s": round(elapsed, 1),
    }


# ==========================================================
# WRAPPER: signal generator per coin
# ==========================================================

def make_baseline_signal(coin, **kwargs):
    """Baseline: v6 + R3 champion (hysteresis + exit_only flip)."""
    cfg = V6_CONFIGS[coin]
    signal, alt = gen_signal_with_options(
        v6_score_ts, alt_data[coin],
        entry_threshold=cfg["threshold"],
        hysteresis_band=R3_HYSTERESIS,
    )
    return signal, alt


def make_momentum_signal(coin, vol_method="range_z", vol_params=None,
                         mom_config=None, **kwargs):
    """Momentum: vol detection + follow direction."""
    vol_params = vol_params or {}
    mom_config = mom_config or {}
    vol_mask = detect_vol_regime(btc_vol, vol_method, vol_params)
    signal, alt = gen_momentum_signal(btc_vol, vol_mask, alt_data[coin], mom_config)
    return signal, alt


def make_rebound_signal(coin, vol_method="range_z", vol_params=None,
                        reb_config=None, **kwargs):
    """Rebound: vol detection + contrarian."""
    vol_params = vol_params or {}
    reb_config = reb_config or {}
    vol_mask = detect_vol_regime(btc_vol, vol_method, reb_config)
    signal, alt = gen_rebound_signal(btc_vol, vol_mask, alt_data[coin], reb_config)
    return signal, alt


def make_hybrid_signal(coin, vol_method="combined_any2", vol_params=None,
                       mom_config=None, reb_config=None,
                       blend_mode="priority_rebound", **kwargs):
    """Hybrid: use momentum normally, switch to rebound on extreme.

    blend_mode:
      - priority_rebound: if rebound signal exists, use it; else use momentum
      - priority_momentum: if momentum signal exists, use it; else use rebound
      - rebound_only_extreme: momentum for moderate vol, rebound for extreme
    """
    vol_params = vol_params or {}
    mom_config = mom_config or {}
    reb_config = reb_config or {}

    # Moderate vol -> momentum
    vol_mask_mod = detect_vol_regime(btc_vol, vol_method, vol_params)
    sig_mom, alt_mom = gen_momentum_signal(btc_vol, vol_mask_mod, alt_data[coin], mom_config)

    # Extreme vol -> rebound
    reb_vol_params = reb_config.copy()
    vol_mask_ext = detect_vol_regime(btc_vol, vol_method, reb_vol_params)
    sig_reb, alt_reb = gen_rebound_signal(btc_vol, vol_mask_ext, alt_data[coin], reb_config)

    # Blend
    signal = sig_mom.copy()
    if blend_mode == "priority_rebound":
        reb_active = sig_reb != 0
        signal[reb_active] = sig_reb[reb_active]
    elif blend_mode == "priority_momentum":
        # Keep momentum, fill gaps with rebound
        mom_inactive = sig_mom == 0
        signal[mom_inactive] = sig_reb[mom_inactive]
    elif blend_mode == "rebound_only_extreme":
        # Already done: vol_mask_mod for momentum, vol_mask_ext for rebound
        pass

    return signal, alt_mom


def make_vol_enhanced_baseline(coin, vol_method="range_z", vol_params=None,
                                enhancement="boost", boost_mode="momentum",
                                mom_config=None, reb_config=None, **kwargs):
    """Enhanced baseline: v6 baseline + extra vol-regime signals.

    enhancement:
      - boost: add vol signals ON TOP of baseline (fill gaps)
      - replace: use vol signals INSTEAD of baseline during vol periods
      - overlay: baseline always runs, vol signals add entries in gaps
    """
    vol_params = vol_params or {}
    mom_config = mom_config or {}
    reb_config = reb_config or {}

    # Baseline signal
    cfg = V6_CONFIGS[coin]
    sig_base, alt_base = gen_signal_with_options(
        v6_score_ts, alt_data[coin],
        entry_threshold=cfg["threshold"],
        hysteresis_band=R3_HYSTERESIS,
    )

    # Vol regime signal
    vol_mask = detect_vol_regime(btc_vol, vol_method, vol_params)
    if boost_mode == "momentum":
        sig_vol, _ = gen_momentum_signal(btc_vol, vol_mask, alt_data[coin], mom_config)
    else:
        sig_vol, _ = gen_rebound_signal(btc_vol, vol_mask, alt_data[coin], reb_config)

    # Blend
    if enhancement == "boost":
        # Add vol signals where baseline is neutral
        base_inactive = sig_base == 0
        signal = sig_base.copy()
        signal[base_inactive] = sig_vol[base_inactive]
    elif enhancement == "replace":
        # Align vol_mask to alt timestamps
        vol_mask_ts = pd.Series(vol_mask.values, index=btc_vol["ts"].values)
        vol_df = vol_mask_ts.reset_index()
        vol_df.columns = ["ts", "is_vol"]
        alt_base_v = pd.merge_asof(alt_base[["ts"]].sort_values("ts"),
                                    vol_df.sort_values("ts"),
                                    on="ts", direction="backward",
                                    tolerance=pd.Timedelta("30min"))
        is_vol_aligned = alt_base_v["is_vol"].fillna(False).values
        signal = sig_base.copy()
        for i in range(len(signal)):
            if is_vol_aligned[i]:
                signal.iloc[i] = sig_vol.iloc[i] if sig_vol.iloc[i] != 0 else sig_base.iloc[i]
    else:  # overlay
        signal = sig_base.copy()
        base_inactive = sig_base == 0
        signal[base_inactive] = sig_vol[base_inactive]

    return signal, alt_base


# ==========================================================
# PHASE 1: VOLATILITY DETECTION ANALYSIS
# ==========================================================

print("\n" + "=" * 70)
print("PHASE 1: VOLATILITY DETECTION — which method finds strong moves?")
print("=" * 70)

# Analyze each detection method: how many bars, frequency, overlap
phase1_analysis = {}
detection_methods = {
    "range_z_1.5":    ("range_z",     {"threshold": 1.5}),
    "range_z_2.0":    ("range_z",     {"threshold": 2.0}),
    "vol_spike_2.0":  ("vol_spike",   {"threshold": 2.0}),
    "vol_spike_3.0":  ("vol_spike",   {"threshold": 3.0}),
    "disp_1.5":       ("displacement", {"threshold": 1.5}),
    "disp_2.0":       ("displacement", {"threshold": 2.0}),
    "disp_2.5":       ("displacement", {"threshold": 2.5}),
    "ret_z_4bar_2.0": ("ret_z",       {"lookback": 4, "threshold": 2.0}),
    "ret_z_4bar_2.5": ("ret_z",       {"lookback": 4, "threshold": 2.5}),
    "ret_z_8bar_2.0": ("ret_z",       {"lookback": 8, "threshold": 2.0}),
    "atr_z_1.5":      ("atr_z",       {"threshold": 1.5}),
    "atr_z_2.0":      ("atr_z",       {"threshold": 2.0}),
    "liq_cascade_2x": ("liq_cascade", {"mult": 2.0}),
    "liq_cascade_3x": ("liq_cascade", {"mult": 3.0}),
    "bb_exp_1.5":     ("bb_expansion", {"threshold": 1.5}),
    "combined_any2":  ("combined_any2", {"range_z_thr": 1.5, "vol_thr": 2.0, "disp_thr": 1.5, "ret_z_thr": 2.0}),
    "combined_any3":  ("combined_any3", {"range_z_thr": 1.5, "vol_thr": 2.0, "disp_thr": 1.5, "ret_z_thr": 2.0, "liq_mult": 2.0}),
}

# Filter to OOS period
btc_oos_mask = (btc_vol["ts"] >= OOS_START) & (btc_vol["ts"] <= OOS_END)
btc_oos = btc_vol[btc_oos_mask]
total_bars_oos = len(btc_oos)

print(f"\nOOS period: {OOS_START} to {OOS_END} ({total_bars_oos:,} bars)")
print(f"\n{'Method':<25} {'Vol Bars':>10} {'Pct':>8} {'Avg Duration':>14}")
print("-" * 60)

for method_name, (method, params) in detection_methods.items():
    mask = detect_vol_regime(btc_vol, method, params)
    mask_oos = mask[btc_oos_mask]
    n_vol = mask_oos.sum()
    pct = n_vol / total_bars_oos * 100

    # Average duration of vol windows
    changes = mask_oos.astype(int).diff().fillna(0)
    starts = (changes == 1).sum()
    avg_dur = n_vol / max(starts, 1)

    phase1_analysis[method_name] = {
        "vol_bars": int(n_vol),
        "pct": round(pct, 2),
        "windows": int(starts),
        "avg_duration": round(avg_dur, 1),
    }
    print(f"  {method_name:<23} {n_vol:>10,} {pct:>7.1f}% {avg_dur:>13.1f}")

print(f"\nTotal OOS bars: {total_bars_oos:,}")


# ==========================================================
# PHASE 2: MOMENTUM EXPERIMENTS
# ==========================================================

print("\n\n" + "=" * 70)
print("PHASE 2: MOMENTUM STRATEGIES — ride the wave")
print("=" * 70)

all_results = []

# Baseline first
baseline_result = run_experiment(
    "BASELINE_v6_R3",
    "v6 + R3 champion (hysteresis=3.0, exit_only, cd_extra=4)",
    make_baseline_signal, {},
    per_coin_config=V6_CONFIGS,
)
all_results.append(baseline_result)

# -- Momentum experiments --

momentum_experiments = [
    # M1: range_z detection + return direction
    {
        "name": "M1_range_z_ret4",
        "desc": "Momentum: range_z>1.5 + 1h return direction (>0.3%)",
        "vol_method": "range_z", "vol_params": {"threshold": 1.5},
        "mom_config": {"direction_method": "ret", "lookback": 4, "min_ret": 0.003},
        "sl": 15.0, "tp": 12.0,
    },
    # M2: vol spike + return direction
    {
        "name": "M2_vol_spike_ret4",
        "desc": "Momentum: vol>2x + 1h return direction",
        "vol_method": "vol_spike", "vol_params": {"threshold": 2.0},
        "mom_config": {"direction_method": "ret", "lookback": 4, "min_ret": 0.003},
        "sl": 15.0, "tp": 12.0,
    },
    # M3: combined_any2 + return direction
    {
        "name": "M3_combined2_ret4",
        "desc": "Momentum: 2-of-4 detectors + 1h return direction",
        "vol_method": "combined_any2",
        "vol_params": {"range_z_thr": 1.5, "vol_thr": 2.0, "disp_thr": 1.5, "ret_z_thr": 2.0},
        "mom_config": {"direction_method": "ret", "lookback": 4, "min_ret": 0.003},
        "sl": 15.0, "tp": 12.0,
    },
    # M4: range_z + EMA alignment
    {
        "name": "M4_range_z_ema_align",
        "desc": "Momentum: range_z>1.5 + EMA9>EMA21 alignment",
        "vol_method": "range_z", "vol_params": {"threshold": 1.5},
        "mom_config": {"direction_method": "ret", "lookback": 4, "min_ret": 0.003,
                       "use_ema_align": True},
        "sl": 15.0, "tp": 12.0,
    },
    # M5: liq cascade + follow liquidation direction
    {
        "name": "M5_liq_cascade_follow",
        "desc": "Momentum: liq cascade 2x + follow liq direction",
        "vol_method": "liq_cascade", "vol_params": {"mult": 2.0},
        "mom_config": {"direction_method": "ret", "lookback": 4, "min_ret": 0.002},
        "sl": 20.0, "tp": 15.0,
    },
    # M6: streak-based (3+ consecutive bars)
    {
        "name": "M6_streak3_vol",
        "desc": "Momentum: vol>2x + 3-bar streak confirmation",
        "vol_method": "vol_spike", "vol_params": {"threshold": 2.0},
        "mom_config": {"direction_method": "streak", "min_streak": 3},
        "sl": 15.0, "tp": 12.0,
    },
    # M7: wider SL/TP for momentum
    {
        "name": "M7_range_z_wide_sltp",
        "desc": "Momentum: range_z>1.5 + ret direction + wide SL25/TP20",
        "vol_method": "range_z", "vol_params": {"threshold": 1.5},
        "mom_config": {"direction_method": "ret", "lookback": 4, "min_ret": 0.003},
        "sl": 25.0, "tp": 20.0,
    },
    # M8: ret_z 4-bar detection
    {
        "name": "M8_ret_z4_2.0",
        "desc": "Momentum: ret z-score(4bar)>2.0 + same direction",
        "vol_method": "ret_z", "vol_params": {"lookback": 4, "threshold": 2.0},
        "mom_config": {"direction_method": "ret", "lookback": 4, "min_ret": 0.003},
        "sl": 15.0, "tp": 12.0,
    },
    # M9: tight SL for quick scalp
    {
        "name": "M9_range_z_tight_sltp",
        "desc": "Momentum: range_z>2.0 + ret direction + tight SL8/TP6",
        "vol_method": "range_z", "vol_params": {"threshold": 2.0},
        "mom_config": {"direction_method": "ret", "lookback": 4, "min_ret": 0.005},
        "sl": 8.0, "tp": 6.0, "max_hold": 48,
    },
    # M10: 8-bar (2h) return direction
    {
        "name": "M10_range_z_ret8",
        "desc": "Momentum: range_z>1.5 + 2h return direction (>0.5%)",
        "vol_method": "range_z", "vol_params": {"threshold": 1.5},
        "mom_config": {"direction_method": "ret", "lookback": 8, "min_ret": 0.005},
        "sl": 15.0, "tp": 12.0,
    },
]

for exp in momentum_experiments:
    result = run_experiment(
        exp["name"], exp["desc"],
        make_momentum_signal,
        {
            "vol_method": exp["vol_method"],
            "vol_params": exp["vol_params"],
            "mom_config": exp["mom_config"],
        },
        sl=exp.get("sl", 15.0),
        tp=exp.get("tp", 12.0),
        max_hold=exp.get("max_hold", 96),
    )
    all_results.append(result)


# ==========================================================
# PHASE 3: REBOUND EXPERIMENTS
# ==========================================================

print("\n\n" + "=" * 70)
print("PHASE 3: REBOUND STRATEGIES — fade extreme moves")
print("=" * 70)

rebound_experiments = [
    # R1: Displacement rebound with range_z detection
    {
        "name": "R1_disp_rebound_2.0",
        "desc": "Rebound: range_z>1.5 + displacement>2.0 ATR -> contrarian",
        "vol_method": "range_z", "vol_params": {"threshold": 1.5},
        "reb_config": {"method": "displacement", "disp_thr": 2.0, "threshold": 1.5},
        "sl": 10.0, "tp": 8.0,
    },
    # R2: Displacement rebound with tighter threshold
    {
        "name": "R2_disp_rebound_1.5",
        "desc": "Rebound: range_z>1.5 + displacement>1.5 ATR -> contrarian",
        "vol_method": "range_z", "vol_params": {"threshold": 1.5},
        "reb_config": {"method": "displacement", "disp_thr": 1.5, "threshold": 1.5},
        "sl": 10.0, "tp": 8.0,
    },
    # R3: RSI extreme rebound
    {
        "name": "R3_rsi_extreme_75_25",
        "desc": "Rebound: vol spike + RSI>75 short / RSI<25 long",
        "vol_method": "vol_spike", "vol_params": {"threshold": 2.0},
        "reb_config": {"method": "rsi_extreme", "rsi_high": 75, "rsi_low": 25, "threshold": 2.0},
        "sl": 10.0, "tp": 8.0,
    },
    # R4: RSI extreme wider threshold
    {
        "name": "R4_rsi_extreme_80_20",
        "desc": "Rebound: vol spike + RSI>80 short / RSI<20 long (stricter)",
        "vol_method": "vol_spike", "vol_params": {"threshold": 2.0},
        "reb_config": {"method": "rsi_extreme", "rsi_high": 80, "rsi_low": 20, "threshold": 2.0},
        "sl": 10.0, "tp": 8.0,
    },
    # R5: Liquidation flush contrarian
    {
        "name": "R5_liq_flush_5x",
        "desc": "Rebound: liq cascade >5x MA -> contrarian",
        "vol_method": "liq_cascade", "vol_params": {"mult": 3.0},
        "reb_config": {"method": "liq_flush", "liq_mult": 5.0, "mult": 3.0},
        "sl": 15.0, "tp": 12.0,
    },
    # R6: Liquidation flush less extreme
    {
        "name": "R6_liq_flush_3x",
        "desc": "Rebound: liq cascade >3x MA -> contrarian",
        "vol_method": "liq_cascade", "vol_params": {"mult": 2.0},
        "reb_config": {"method": "liq_flush", "liq_mult": 3.0, "mult": 2.0},
        "sl": 15.0, "tp": 12.0,
    },
    # R7: Exhaustion candle (displacement + RSI combined)
    {
        "name": "R7_exhaustion_disp1.5_rsi70",
        "desc": "Rebound: displacement>1.5 + RSI>70/<30 -> contrarian",
        "vol_method": "range_z", "vol_params": {"threshold": 1.5},
        "reb_config": {"method": "exhaustion", "disp_thr": 1.5, "rsi_high": 70, "rsi_low": 30, "threshold": 1.5},
        "sl": 10.0, "tp": 8.0,
    },
    # R8: Range extreme rebound
    {
        "name": "R8_range_extreme_90_10",
        "desc": "Rebound: vol spike + price at 90%/10% of 4h range -> contrarian",
        "vol_method": "vol_spike", "vol_params": {"threshold": 2.0},
        "reb_config": {"method": "range_extreme", "pos_high": 0.9, "pos_low": 0.1, "threshold": 2.0},
        "sl": 10.0, "tp": 8.0,
    },
    # R9: Displacement with wider SL
    {
        "name": "R9_disp_rebound_wide_sl",
        "desc": "Rebound: displacement>2.0 + wide SL20/TP15",
        "vol_method": "range_z", "vol_params": {"threshold": 1.5},
        "reb_config": {"method": "displacement", "disp_thr": 2.0, "threshold": 1.5},
        "sl": 20.0, "tp": 15.0,
    },
    # R10: Combined any2 + exhaustion
    {
        "name": "R10_combined2_exhaustion",
        "desc": "Rebound: 2-of-4 detectors + exhaustion (disp+RSI)",
        "vol_method": "combined_any2",
        "vol_params": {"range_z_thr": 1.5, "vol_thr": 2.0, "disp_thr": 1.5, "ret_z_thr": 2.0},
        "reb_config": {"method": "exhaustion", "disp_thr": 1.5, "rsi_high": 70, "rsi_low": 30,
                       "range_z_thr": 1.5, "vol_thr": 2.0, "disp_thr": 1.5, "ret_z_thr": 2.0},
        "sl": 12.0, "tp": 10.0,
    },
]

for exp in rebound_experiments:
    result = run_experiment(
        exp["name"], exp["desc"],
        make_rebound_signal,
        {
            "vol_method": exp["vol_method"],
            "vol_params": exp["vol_params"],
            "reb_config": exp["reb_config"],
        },
        sl=exp.get("sl", 10.0),
        tp=exp.get("tp", 8.0),
        max_hold=exp.get("max_hold", 96),
    )
    all_results.append(result)


# ==========================================================
# PHASE 4: HYBRID & ENHANCED BASELINE
# ==========================================================

print("\n\n" + "=" * 70)
print("PHASE 4: HYBRID & ENHANCED BASELINE")
print("=" * 70)

# Find best momentum and rebound from phases 2 & 3
mom_results = [r for r in all_results if r["name"].startswith("M")]
reb_results = [r for r in all_results if r["name"].startswith("R")]

best_mom = max(mom_results, key=lambda x: x["total_pnl"]) if mom_results else None
best_reb = max(reb_results, key=lambda x: x["total_pnl"]) if reb_results else None

print(f"\nBest Momentum: {best_mom['name'] if best_mom else 'N/A'} "
      f"(${best_mom['total_pnl']:,.0f})" if best_mom else "")
print(f"Best Rebound:  {best_reb['name'] if best_reb else 'N/A'} "
      f"(${best_reb['total_pnl']:,.0f})" if best_reb else "")

# H1: Baseline + momentum boost (fill gaps with momentum signals)
hybrid_experiments = [
    {
        "name": "H1_baseline_mom_boost",
        "desc": "Baseline v6 + momentum signals fill gaps (range_z+ret4)",
        "vol_method": "range_z", "vol_params": {"threshold": 1.5},
        "enhancement": "boost", "boost_mode": "momentum",
        "mom_config": {"direction_method": "ret", "lookback": 4, "min_ret": 0.003},
    },
    {
        "name": "H2_baseline_reb_boost",
        "desc": "Baseline v6 + rebound signals fill gaps (displacement>2.0)",
        "vol_method": "range_z", "vol_params": {"threshold": 1.5},
        "enhancement": "boost", "boost_mode": "rebound",
        "reb_config": {"method": "displacement", "disp_thr": 2.0, "threshold": 1.5},
    },
    {
        "name": "H3_baseline_mom_replace",
        "desc": "Replace baseline with momentum during vol spikes",
        "vol_method": "range_z", "vol_params": {"threshold": 1.5},
        "enhancement": "replace", "boost_mode": "momentum",
        "mom_config": {"direction_method": "ret", "lookback": 4, "min_ret": 0.003},
    },
    {
        "name": "H4_baseline_reb_replace",
        "desc": "Replace baseline with rebound during vol spikes",
        "vol_method": "range_z", "vol_params": {"threshold": 1.5},
        "enhancement": "replace", "boost_mode": "rebound",
        "reb_config": {"method": "displacement", "disp_thr": 2.0, "threshold": 1.5},
    },
]

for exp in hybrid_experiments:
    result = run_experiment(
        exp["name"], exp["desc"],
        make_vol_enhanced_baseline,
        {
            "vol_method": exp["vol_method"],
            "vol_params": exp["vol_params"],
            "enhancement": exp["enhancement"],
            "boost_mode": exp["boost_mode"],
            "mom_config": exp.get("mom_config", {}),
            "reb_config": exp.get("reb_config", {}),
        },
        sl=25.0, tp=20.0,  # Use baseline SL/TP
        per_coin_config=V6_CONFIGS,
    )
    all_results.append(result)


# ==========================================================
# FINAL RESULTS & LEADERBOARD
# ==========================================================

print("\n\n" + "=" * 70)
print("FINAL LEADERBOARD")
print("=" * 70)

# Sort by PnL
sorted_results = sorted(all_results, key=lambda x: x["total_pnl"], reverse=True)

baseline_pnl = baseline_result["total_pnl"]
print(f"\nBaseline (v6+R3): ${baseline_pnl:,.0f}")
print(f"\n{'Rank':<5} {'Name':<30} {'PnL':>12} {'Δ vs Base':>12} {'Trades':>8} {'WR%':>7}")
print("-" * 78)

for rank, r in enumerate(sorted_results, 1):
    delta = r["total_pnl"] - baseline_pnl
    delta_str = f"+${delta:,.0f}" if delta >= 0 else f"-${abs(delta):,.0f}"
    marker = " *" if r["total_pnl"] > baseline_pnl else ""
    print(f"  {rank:<3} {r['name']:<30} ${r['total_pnl']:>10,.0f} {delta_str:>12} "
          f"{r['total_trades']:>8} {r['win_rate']:>6.1f}%{marker}")


# ==========================================================
# SAVE RESULTS
# ==========================================================

results_data = {
    "tournament": "round_5_volatility_regime",
    "timestamp": datetime.now().isoformat(),
    "oos_period": f"{OOS_START} to {OOS_END}",
    "baseline_pnl": baseline_pnl,
    "phase1_analysis": phase1_analysis,
    "results": sorted_results,
    "champion": sorted_results[0]["name"] if sorted_results else None,
    "champion_pnl": sorted_results[0]["total_pnl"] if sorted_results else 0,
}

# Convert numpy types for JSON serialization
def convert_numpy(obj):
    if isinstance(obj, (np.integer,)):
        return int(obj)
    elif isinstance(obj, (np.floating,)):
        return float(obj)
    elif isinstance(obj, np.ndarray):
        return obj.tolist()
    elif isinstance(obj, pd.Timestamp):
        return str(obj)
    elif isinstance(obj, np.bool_):
        return bool(obj)
    return obj

def deep_convert(obj):
    if isinstance(obj, dict):
        return {k: deep_convert(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [deep_convert(v) for v in obj]
    return convert_numpy(obj)

results_data = deep_convert(results_data)

with open(RESULTS_FILE, "w") as f:
    json.dump(results_data, f, indent=2, default=str)
print(f"\nResults saved to {RESULTS_FILE}")

total_elapsed = time.time() - t0
print(f"\nTotal time: {total_elapsed:.0f}s ({total_elapsed/60:.1f}min)")
print("=" * 70)
print("TOURNAMENT ROUND 5 COMPLETE")
print("=" * 70)
