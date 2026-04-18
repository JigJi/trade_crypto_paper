"""
Tournament Round 4: MARKET GUARD SYSTEM
========================================
Paper trading lost $482 (-8.9%) on Mar 23 — Trump-Iran geopolitical whipsaw
+ double daemon bug + v6 binary score amplification.

Key insight: the event was "calculable" — there were leading signals
(weekend thin liquidity, ATR spike, dual-side liquidation) but the system
had no "early warning" to pause trading when markets go abnormal.

Philosophy: The model works well for ~90% of normal markets. Don't try to
be smarter in chaos — just STOP TRADING when conditions are abnormal.

Goal: Build + backtest a Market Guard system that blocks entries during
abnormal conditions → test if it improves PnL / drawdown.

Guards:
  1. Weekend Mode — block/reduce entries on Sat/Sun
  2. ATR Spike — block when realized volatility exceeds threshold
  3. Double Liquidation — block when both-side liq spikes (chaos)
  4. Score Oscillation — block when BTC score flips too often
  5. Intraday Drawdown Breaker — stop trading after X% daily loss

Phases:
  Phase 1: 20 individual guard experiments
  Phase 2: 5-8 combined guard experiments
  Phase 3: cross-model validation (v3, v5, v6)
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
    calc_metrics,
    INIT_EQUITY, BUDGET_USDT, LEVERAGE, FEE, SLIP,
)
from signal_core import build_btc_features, compute_btc_composite_score_v6

# ── Config ──────────────────────────────────────────────
OOS_START = "2025-01-01"
OOS_END   = "2026-03-24"
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
    "BULL":  ("2024-10-01", "2025-01-20"),
    "BEAR":  ("2025-01-20", "2025-04-01"),
    "FLAT":  ("2025-04-01", "2025-07-01"),
    "MIXED": ("2025-07-01", "2025-10-01"),
    "RECENT": ("2025-10-01", "2026-03-24"),
}

RESULTS_DIR = Path(__file__).parent
RESULTS_FILE = RESULTS_DIR / "results.json"


# ══════════════════════════════════════════════════════════
# DATA LOADING
# ══════════════════════════════════════════════════════════

print("=" * 70)
print("TOURNAMENT ROUND 4: MARKET GUARD SYSTEM")
print("=" * 70)

t0 = time.time()
btc_ohlcv = fetch_binance_15m("BTCUSDT", years=3)
db_data = load_btc_db_data()
btc_df = build_btc_features(btc_ohlcv, db_data)
print(f"BTC features: {len(btc_df)} bars ({time.time()-t0:.1f}s)")

# v6 BTC score
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


# ══════════════════════════════════════════════════════════
# gen_signal_with_options() — copied from R3 (can't import: module-level code)
# ══════════════════════════════════════════════════════════

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


# ══════════════════════════════════════════════════════════
# FUNCTION 1: compute_guards() — Guard calculator
# ══════════════════════════════════════════════════════════

def compute_guards(btc_df, alt_df, btc_score_aligned, guard_config):
    """Compute guard mask: True = BLOCK entry at this bar.

    Args:
        btc_df: BTC features DataFrame (with liq columns)
        alt_df: merged alt DataFrame (with ts, atr, btc_score)
        btc_score_aligned: btc_score values aligned to alt_df index
        guard_config: dict with guard settings

    Returns:
        blocked: boolean Series aligned to alt_df index
        guard_details: dict with per-guard block counts
    """
    n = len(alt_df)
    blocked = pd.Series(False, index=alt_df.index)
    guard_details = {}

    # ── Guard 1: Weekend Mode ──
    if guard_config.get("weekend"):
        mode = guard_config["weekend"]
        is_weekend = alt_df["ts"].dt.dayofweek >= 5
        if mode == "block_all":
            weekend_blocked = is_weekend
        elif mode == "size_50":
            # For size reduction, we still track but don't fully block
            # (handled in backtest via size multiplier, here we just track)
            weekend_blocked = pd.Series(False, index=alt_df.index)
        else:
            weekend_blocked = pd.Series(False, index=alt_df.index)
        blocked |= weekend_blocked
        guard_details["weekend"] = int(weekend_blocked.sum())

    # ── Guard 2: ATR Spike ──
    if guard_config.get("atr_spike_mult"):
        mult = guard_config["atr_spike_mult"]
        atr = alt_df["atr"].copy()
        atr_ma = atr.rolling(96, min_periods=20).mean()
        atr_spike = atr > (atr_ma * mult)
        blocked |= atr_spike
        guard_details["atr_spike"] = int(atr_spike.sum())

    # ── Guard 3: Double Liquidation ──
    if guard_config.get("double_liq"):
        dliq_cfg = guard_config["double_liq"]
        dliq_mode = dliq_cfg.get("mode", "balance")

        # Merge liq data from btc_df to alt timeline
        liq_cols = ["ts", "liq_long_1h", "liq_short_1h", "liq_total", "liq_total_ma"]
        available_cols = [c for c in liq_cols if c in btc_df.columns]
        if len(available_cols) >= 3:  # ts + at least 2 liq cols
            liq_data = btc_df[available_cols].dropna(subset=[c for c in available_cols if c != "ts"])
            alt_with_liq = pd.merge_asof(
                alt_df[["ts"]].sort_values("ts"),
                liq_data.sort_values("ts"),
                on="ts", direction="backward", tolerance=pd.Timedelta("2h")
            )

            if dliq_mode == "balance":
                # Balance mode: both sides contribute roughly equally
                bal_thr = dliq_cfg.get("balance_thr", 0.3)
                total_mult = dliq_cfg.get("total_mult", 2.0)
                liq_l = alt_with_liq["liq_long_1h"].fillna(0)
                liq_s = alt_with_liq["liq_short_1h"].fillna(0)
                liq_t = alt_with_liq["liq_total"].fillna(0)
                liq_t_ma = alt_with_liq["liq_total_ma"].fillna(1)
                balance = liq_l.clip(lower=0).where(liq_s > 0, 0).combine(
                    liq_s.clip(lower=0), min) / (liq_t + 1e-8)
                dliq_blocked = (balance > bal_thr) & (liq_t > liq_t_ma * total_mult)
                # Reset index to match alt_df
                dliq_blocked = dliq_blocked.values
                dliq_blocked = pd.Series(dliq_blocked, index=alt_df.index)

            elif dliq_mode == "both_spike":
                # Both sides must independently spike above MA
                ma_mult = dliq_cfg.get("ma_mult", 2.0)
                liq_l = alt_with_liq["liq_long_1h"].fillna(0)
                liq_s = alt_with_liq["liq_short_1h"].fillna(0)
                liq_l_ma = liq_l.rolling(24, min_periods=5).mean()
                liq_s_ma = liq_s.rolling(24, min_periods=5).mean()
                dliq_blocked = (liq_l > liq_l_ma * ma_mult) & (liq_s > liq_s_ma * ma_mult)
                dliq_blocked = pd.Series(dliq_blocked.values, index=alt_df.index)
            else:
                dliq_blocked = pd.Series(False, index=alt_df.index)

            blocked |= dliq_blocked
            guard_details["double_liq"] = int(dliq_blocked.sum())
        else:
            guard_details["double_liq"] = 0

    # ── Guard 4: Score Oscillation Detector ──
    if guard_config.get("score_osc"):
        osc_cfg = guard_config["score_osc"]
        window = osc_cfg.get("window", 16)
        max_flips = osc_cfg.get("max_flips", 4)

        scores = btc_score_aligned.values if hasattr(btc_score_aligned, 'values') else btc_score_aligned
        signs = np.sign(scores)
        # Count sign changes in rolling window
        sign_changes = np.zeros(len(signs))
        for i in range(1, len(signs)):
            if signs[i] != signs[i-1] and signs[i] != 0 and signs[i-1] != 0:
                sign_changes[i] = 1
        # Rolling sum of sign changes
        sc_series = pd.Series(sign_changes)
        rolling_flips = sc_series.rolling(window, min_periods=1).sum()
        osc_blocked = rolling_flips >= max_flips
        osc_blocked = pd.Series(osc_blocked.values, index=alt_df.index)
        blocked |= osc_blocked
        guard_details["score_osc"] = int(osc_blocked.sum())

    return blocked, guard_details


# ══════════════════════════════════════════════════════════
# FUNCTION 2: run_backtest_guarded() — Backtest with guard
# ══════════════════════════════════════════════════════════

def run_backtest_guarded(df, signals, guard_mask, sl_atr_mult=25.0, tp_atr_mult=20.0,
                         max_hold_bars=96, cooldown_bars=4,
                         flip_mode="exit_only", min_bars_before_flip=0,
                         flip_cooldown_extra=4,
                         weekend_size_mult=None):
    """Modified backtest with guard mask that blocks entries.

    guard_mask: boolean array, True = block entry at this bar
    weekend_size_mult: if set, reduce position size on weekends instead of blocking
    """
    sig = signals.shift(1).fillna(0).astype(int).values
    guard = guard_mask.values if hasattr(guard_mask, 'values') else guard_mask
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
    guard_blocked_count = 0
    guard_blocked_would_pnl = 0.0  # Track PnL of blocked signals

    # For tracking what blocked signals would have done
    shadow_positions = []  # track hypothetical trades that were blocked

    for i in range(n):
        # ── Entry ──
        if position == 0 and sig[i] != 0 and (i - last_exit_i) > cooldown_bars:
            # Check guard
            if guard[i]:
                guard_blocked_count += 1
                # Track shadow: what would have happened if we entered
                shadow_positions.append({
                    "entry_i": i, "entry_sig": sig[i],
                    "entry_px": opens[i], "entry_atr": atrs[i],
                })
                continue

            raw_px = opens[i]
            cur_atr = atrs[i] if not np.isnan(atrs[i]) else 0
            if raw_px <= 0 or cur_atr <= 0 or cur_atr / raw_px < 0.0005:
                continue

            # Weekend size adjustment
            size_mult = 1.0
            if weekend_size_mult is not None:
                ts = pd.Timestamp(times[i])
                if ts.dayofweek >= 5:
                    size_mult = weekend_size_mult

            qty = (BUDGET_USDT * LEVERAGE * size_mult) / raw_px
            entry_px = raw_px * (1 + SLIP) if sig[i] == 1 else raw_px * (1 - SLIP)
            entry_atr = cur_atr
            fee_in = entry_px * qty * FEE
            position = sig[i]
            entry_i = i
            peak = entry_px
            trough = entry_px
            continue

        # ── Exit ──
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

            # SIGNAL_FLIP check (R3 champion settings)
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

    # Compute shadow PnL for blocked trades
    blocked_would_pnl = 0.0
    blocked_would_trades = 0
    blocked_would_wins = 0
    for sp in shadow_positions:
        si = sp["entry_i"]
        s_sig = sp["entry_sig"]
        s_px = sp["entry_px"]
        s_atr = sp["entry_atr"]
        if s_px <= 0 or np.isnan(s_atr) or s_atr <= 0:
            continue
        s_qty = (BUDGET_USDT * LEVERAGE) / s_px
        s_entry = s_px * (1 + SLIP) if s_sig == 1 else s_px * (1 - SLIP)
        s_sl = s_entry - sl_atr_mult * s_atr if s_sig == 1 else s_entry + sl_atr_mult * s_atr
        s_tp = s_entry + tp_atr_mult * s_atr if s_sig == 1 else s_entry - tp_atr_mult * s_atr

        # Simulate forward until exit
        for j in range(si + 1, min(si + max_hold_bars + 1, n)):
            h, l, c = highs[j], lows[j], closes[j]
            s_exit = s_reason = None
            if s_sig == 1:
                if l <= s_sl: s_exit = s_sl
                elif h >= s_tp: s_exit = s_tp
            else:
                if h >= s_sl: s_exit = s_sl
                elif l <= s_tp: s_exit = s_tp
            if s_exit is None and (j - si) >= max_hold_bars:
                s_exit = c
            if s_exit is not None:
                s_exit_f = s_exit * (1 - SLIP) if s_sig == 1 else s_exit * (1 + SLIP)
                s_fee = s_entry * s_qty * FEE + s_exit_f * s_qty * FEE
                s_pnl = (s_exit_f - s_entry) * s_qty * s_sig - s_fee
                blocked_would_pnl += s_pnl
                blocked_would_trades += 1
                if s_pnl > 0:
                    blocked_would_wins += 1
                break

    trades_df = pd.DataFrame(records)
    return trades_df, {
        "guard_blocked_count": guard_blocked_count,
        "blocked_would_pnl": round(blocked_would_pnl, 2),
        "blocked_would_trades": blocked_would_trades,
        "blocked_would_wr": round(100 * blocked_would_wins / max(blocked_would_trades, 1), 1),
        "guard_savings": round(-blocked_would_pnl, 2),  # positive = guard saved money
    }


# ══════════════════════════════════════════════════════════
# FUNCTION 3: run_backtest_guarded_dd() — With drawdown breaker
# ══════════════════════════════════════════════════════════

def run_backtest_guarded_dd(df, signals, guard_mask, dd_threshold=-0.03,
                            sl_atr_mult=25.0, tp_atr_mult=20.0,
                            max_hold_bars=96, cooldown_bars=4,
                            flip_mode="exit_only", min_bars_before_flip=0,
                            flip_cooldown_extra=4):
    """Backtest with guard mask + intraday drawdown circuit breaker.

    dd_threshold: e.g. -0.03 = stop trading after -3% daily drawdown
    """
    sig = signals.shift(1).fillna(0).astype(int).values
    guard = guard_mask.values if hasattr(guard_mask, 'values') else guard_mask
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
    guard_blocked_count = 0
    dd_blocked_count = 0

    # Daily drawdown tracking
    current_day = None
    day_start_equity = equity
    day_breaker_active = False

    for i in range(n):
        ts = pd.Timestamp(times[i])
        day = ts.date()

        # Reset daily tracker
        if day != current_day:
            current_day = day
            day_start_equity = equity
            day_breaker_active = False

        # Check intraday drawdown
        if day_start_equity > 0:
            intraday_dd = (equity - day_start_equity) / day_start_equity
            if intraday_dd <= dd_threshold:
                day_breaker_active = True

        # ── Entry ──
        if position == 0 and sig[i] != 0 and (i - last_exit_i) > cooldown_bars:
            if guard[i]:
                guard_blocked_count += 1
                continue
            if day_breaker_active:
                dd_blocked_count += 1
                continue

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

        # ── Exit ──
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

    trades_df = pd.DataFrame(records)
    return trades_df, {
        "guard_blocked_count": guard_blocked_count,
        "dd_blocked_count": dd_blocked_count,
        "total_blocked": guard_blocked_count + dd_blocked_count,
    }


# ══════════════════════════════════════════════════════════
# FUNCTION 4: run_contender_guard() — Experiment orchestrator
# ══════════════════════════════════════════════════════════

def compute_exit_breakdown(trades_df):
    """Compute per-exit-reason metrics."""
    breakdown = {}
    if trades_df.empty:
        return breakdown
    for reason in ["SL", "TP", "TIMEOUT", "SIGNAL_FLIP"]:
        sub = trades_df[trades_df["exit_reason"] == reason]
        if len(sub) > 0:
            wins = (sub["pnl_net"] > 0).sum()
            breakdown[reason] = {
                "count": len(sub),
                "pnl": round(sub["pnl_net"].sum(), 2),
                "wr": round(100 * wins / len(sub), 1),
                "avg_pnl": round(sub["pnl_net"].mean(), 2),
                "avg_bars": round(sub["holding_bars"].mean(), 1),
            }
        else:
            breakdown[reason] = {"count": 0, "pnl": 0, "wr": 0, "avg_pnl": 0, "avg_bars": 0}
    return breakdown


def run_contender_guard(name, guard_config, dd_threshold=None, description=""):
    """Run one guard experiment across all 6 coins.

    guard_config: dict passed to compute_guards()
    dd_threshold: if set, use drawdown breaker version
    """
    print(f"\n{'-'*60}")
    print(f"  {name}")
    if description:
        print(f"  {description}")
    guards_str = ", ".join(f"{k}={v}" for k, v in guard_config.items() if v)
    if dd_threshold:
        guards_str += f", dd_breaker={dd_threshold}"
    print(f"  guards: {guards_str}")
    print(f"{'-'*60}")

    t1 = time.time()

    all_trades = []
    coin_results = {}
    total_guard_stats = {
        "guard_blocked_count": 0,
        "blocked_would_pnl": 0.0,
        "blocked_would_trades": 0,
        "blocked_would_wins": 0,
        "dd_blocked_count": 0,
    }
    total_guard_details = {}

    for coin in COINS:
        cfg = V6_CONFIGS[coin]

        # Generate signals (R3 champion settings)
        sig, alt_merged = gen_signal_with_options(
            v6_score_ts, alt_data[coin],
            entry_threshold=cfg["threshold"],
            hysteresis_band=R3_HYSTERESIS,
            use_alt_pa=False,
            confirm_bars=1,
        )

        # Filter to OOS
        oos_mask = (alt_merged["ts"] >= pd.Timestamp(OOS_START))
        if OOS_END:
            oos_mask &= (alt_merged["ts"] <= pd.Timestamp(OOS_END))
        alt_oos = alt_merged[oos_mask].reset_index(drop=True)
        sig_oos = sig[oos_mask].reset_index(drop=True)

        if len(alt_oos) < 100:
            continue

        # Get btc_score aligned to alt timeline
        btc_score_aligned = alt_oos["btc_score"] if "btc_score" in alt_oos.columns else pd.Series(0, index=alt_oos.index)

        # Compute guard mask
        guard_mask, guard_details = compute_guards(btc_df, alt_oos, btc_score_aligned, guard_config)

        # Aggregate guard details
        for gname, gcount in guard_details.items():
            total_guard_details[gname] = total_guard_details.get(gname, 0) + gcount

        # Run backtest
        if dd_threshold is not None:
            trades, gstats = run_backtest_guarded_dd(
                alt_oos, sig_oos, guard_mask,
                dd_threshold=dd_threshold,
                sl_atr_mult=cfg["sl"], tp_atr_mult=cfg["tp"],
                max_hold_bars=96, cooldown_bars=cfg["cd"],
                flip_mode=R3_FLIP_MODE,
                min_bars_before_flip=0,
                flip_cooldown_extra=R3_CD_EXTRA,
            )
            total_guard_stats["dd_blocked_count"] += gstats.get("dd_blocked_count", 0)
            total_guard_stats["guard_blocked_count"] += gstats["guard_blocked_count"]
        else:
            weekend_size = None
            if guard_config.get("weekend") == "size_50":
                weekend_size = 0.5
            trades, gstats = run_backtest_guarded(
                alt_oos, sig_oos, guard_mask,
                sl_atr_mult=cfg["sl"], tp_atr_mult=cfg["tp"],
                max_hold_bars=96, cooldown_bars=cfg["cd"],
                flip_mode=R3_FLIP_MODE,
                min_bars_before_flip=0,
                flip_cooldown_extra=R3_CD_EXTRA,
                weekend_size_mult=weekend_size,
            )
            total_guard_stats["guard_blocked_count"] += gstats["guard_blocked_count"]
            total_guard_stats["blocked_would_pnl"] += gstats["blocked_would_pnl"]
            total_guard_stats["blocked_would_trades"] += gstats["blocked_would_trades"]
            total_guard_stats["blocked_would_wins"] += gstats.get("blocked_would_wins", 0)

        if len(trades) > 0:
            m = calc_metrics(trades, len(alt_oos))
            bd = compute_exit_breakdown(trades)
            coin_results[coin] = {**m, "exit_breakdown": bd}
            trades["coin"] = coin
            all_trades.append(trades)

    # ── Aggregate ──
    if all_trades:
        combined = pd.concat(all_trades, ignore_index=True)
        total_pnl = combined["pnl_net"].sum()
        total_trades = len(combined)
        total_wins = (combined["pnl_net"] > 0).sum()
        total_wr = 100 * total_wins / total_trades if total_trades > 0 else 0

        longs = combined[combined["dir"] == "L"]
        shorts = combined[combined["dir"] == "S"]
        long_pnl = longs["pnl_net"].sum() if len(longs) > 0 else 0
        short_pnl = shorts["pnl_net"].sum() if len(shorts) > 0 else 0

        equity = INIT_EQUITY + combined["pnl_net"].cumsum()
        max_dd = ((equity - equity.cummax()) / equity.cummax() * 100).min()
        ret_per_trade = combined["pnl_net"] / BUDGET_USDT
        sharpe = (ret_per_trade.mean() / ret_per_trade.std() * np.sqrt(total_trades)
                  if ret_per_trade.std() > 0 else 0)
        agg_breakdown = compute_exit_breakdown(combined)

        # Regime analysis
        regime_results = {}
        for rname, (rstart, rend) in REGIMES.items():
            rmask = ((pd.to_datetime(combined["entry_time"]) >= pd.Timestamp(rstart)) &
                     (pd.to_datetime(combined["entry_time"]) < pd.Timestamp(rend)))
            rsub = combined[rmask]
            if len(rsub) > 0:
                regime_results[rname] = {
                    "trades": len(rsub),
                    "pnl": round(rsub["pnl_net"].sum(), 2),
                    "wr": round(100 * (rsub["pnl_net"] > 0).sum() / len(rsub), 1),
                }
            else:
                regime_results[rname] = {"trades": 0, "pnl": 0, "wr": 0}
    else:
        total_pnl = total_trades = 0; total_wr = 0
        long_pnl = short_pnl = 0; max_dd = sharpe = 0
        agg_breakdown = {}; regime_results = {}

    elapsed = time.time() - t1

    # Guard stats
    gb = total_guard_stats["guard_blocked_count"]
    dd_b = total_guard_stats["dd_blocked_count"]
    bw_pnl = total_guard_stats["blocked_would_pnl"]
    bw_trades = total_guard_stats["blocked_would_trades"]
    bw_wins = total_guard_stats["blocked_would_wins"]
    pct_blocked = round(100 * gb / max(gb + total_trades, 1), 1)

    result = {
        "name": name, "description": description,
        "guard_config": {k: str(v) for k, v in guard_config.items()},
        "dd_threshold": dd_threshold,
        "total_pnl": round(total_pnl, 2),
        "total_trades": total_trades,
        "win_rate": round(total_wr, 1),
        "sharpe": round(sharpe, 2),
        "max_dd_pct": round(max_dd, 2),
        "long_pnl": round(long_pnl, 2),
        "short_pnl": round(short_pnl, 2),
        "exit_breakdown": agg_breakdown,
        "regime_results": regime_results,
        "coin_results": {c: {"pnl": round(m["net_pnl"], 2), "wr": round(m["win_rate"], 1),
                              "trades": m["total"], "sharpe": round(m["sharpe"], 2)}
                         for c, m in coin_results.items()},
        "guard_stats": {
            "signals_blocked": gb,
            "pct_blocked": pct_blocked,
            "blocked_would_pnl": round(bw_pnl, 2),
            "blocked_would_trades": bw_trades,
            "blocked_would_wr": round(100 * bw_wins / max(bw_trades, 1), 1),
            "guard_savings": round(-bw_pnl, 2),
            "dd_blocked": dd_b,
            "per_guard": total_guard_details,
        },
        "elapsed_s": round(elapsed, 1),
    }

    # Pretty print
    guard_info = f" | BLOCKED: {gb} ({pct_blocked}%)"
    if bw_trades > 0:
        guard_info += f" | saved ${-bw_pnl:+,.0f} ({bw_trades} trades, {100*bw_wins/max(bw_trades,1):.0f}% WR)"
    if dd_b > 0:
        guard_info += f" | DD_BREAK: {dd_b}"
    print(f"    {total_trades} trades | WR {total_wr:.1f}% | PnL ${total_pnl:+,.0f} | "
          f"Sharpe {sharpe:.2f} | DD {max_dd:.1f}%{guard_info}")

    return result


# ══════════════════════════════════════════════════════════
# HELPER: Summary printer
# ══════════════════════════════════════════════════════════

def print_batch_summary(batch_name, results, baseline_pnl):
    print(f"\n{'='*130}")
    print(f"{batch_name} SUMMARY")
    print(f"{'='*130}")
    print(f"{'Rank':<4} {'Name':<40} {'Trades':>6} {'WR%':>6} {'PnL':>10} {'Sharpe':>7} "
          f"{'DD%':>6} {'Blocked':>7} {'Blk%':>5} {'Savings':>9} {'Delta':>10}")
    print("-" * 130)
    for i, r in enumerate(sorted(results, key=lambda x: x["total_pnl"], reverse=True), 1):
        delta = r["total_pnl"] - baseline_pnl
        gs = r.get("guard_stats", {})
        blocked = gs.get("signals_blocked", 0)
        pct_blk = gs.get("pct_blocked", 0)
        savings = gs.get("guard_savings", 0)
        marker = " ***" if r["total_pnl"] == max(x["total_pnl"] for x in results) else ""
        print(f"{i:<4} {r['name']:<40} {r['total_trades']:>6} {r['win_rate']:>5.1f}% "
              f"${r['total_pnl']:>9,.0f} {r['sharpe']:>7.2f} {r['max_dd_pct']:>5.1f}% "
              f"{blocked:>7} {pct_blk:>4.1f}% ${savings:>+8,.0f} ${delta:>+9,.0f}{marker}")
    return sorted(results, key=lambda x: x["total_pnl"], reverse=True)


# ══════════════════════════════════════════════════════════
# PHASE 1: INDIVIDUAL GUARD EXPERIMENTS (20 total)
# ══════════════════════════════════════════════════════════

all_results = []

# ── Baseline: No guards (R3 champion) ──
print("\n" + "#" * 70)
print("# BASELINE: R3 Champion (no guards)")
print("#" * 70)

baseline = run_contender_guard("BASELINE_no_guard", {}, description="R3 champion, no guards")
all_results.append(baseline)
BASELINE_PNL = baseline["total_pnl"]
print(f"\n  BASELINE PnL: ${BASELINE_PNL:+,.0f}")

# ── Group A: Weekend Mode (2) ──
print("\n" + "#" * 70)
print("# GROUP A: Weekend Guard")
print("#" * 70)

group_a = [baseline]
for mode in ["block_all", "size_50"]:
    r = run_contender_guard(
        f"A_weekend_{mode}",
        {"weekend": mode},
        description=f"Weekend guard: {mode}",
    )
    group_a.append(r)
    all_results.append(r)

group_a_sorted = print_batch_summary("GROUP A: WEEKEND", group_a, BASELINE_PNL)
best_weekend = group_a_sorted[0]["guard_config"].get("weekend") if group_a_sorted[0]["name"] != "BASELINE_no_guard" else None

# ── Group B: ATR Spike (4) ──
print("\n" + "#" * 70)
print("# GROUP B: ATR Spike Guard")
print("#" * 70)

group_b = [baseline]
for mult in [1.5, 2.0, 2.5, 3.0]:
    r = run_contender_guard(
        f"B_atr_spike_{mult}x",
        {"atr_spike_mult": mult},
        description=f"ATR spike guard: {mult}x MA threshold",
    )
    group_b.append(r)
    all_results.append(r)

group_b_sorted = print_batch_summary("GROUP B: ATR SPIKE", group_b, BASELINE_PNL)
best_atr = group_b_sorted[0]["guard_config"].get("atr_spike_mult") if group_b_sorted[0]["name"] != "BASELINE_no_guard" else None

# ── Group C: Double Liquidation (4) ──
print("\n" + "#" * 70)
print("# GROUP C: Double Liquidation Guard")
print("#" * 70)

group_c = [baseline]
dliq_variants = [
    ("balance_30_2x", {"mode": "balance", "balance_thr": 0.3, "total_mult": 2.0}),
    ("balance_40_3x", {"mode": "balance", "balance_thr": 0.4, "total_mult": 3.0}),
    ("both_spike_2x", {"mode": "both_spike", "ma_mult": 2.0}),
    ("both_spike_3x", {"mode": "both_spike", "ma_mult": 3.0}),
]
for vname, vcfg in dliq_variants:
    r = run_contender_guard(
        f"C_dliq_{vname}",
        {"double_liq": vcfg},
        description=f"Double liquidation guard: {vname}",
    )
    group_c.append(r)
    all_results.append(r)

group_c_sorted = print_batch_summary("GROUP C: DOUBLE LIQUIDATION", group_c, BASELINE_PNL)
best_dliq = None
if group_c_sorted[0]["name"] != "BASELINE_no_guard":
    best_dliq_name = group_c_sorted[0]["name"].replace("C_dliq_", "")
    for vn, vc in dliq_variants:
        if vn == best_dliq_name:
            best_dliq = vc
            break

# ── Group D: Score Oscillation (5) ──
print("\n" + "#" * 70)
print("# GROUP D: Score Oscillation Guard")
print("#" * 70)

group_d = [baseline]
osc_variants = [
    ("8bars_3flips",  {"window": 8,  "max_flips": 3}),
    ("16bars_3flips", {"window": 16, "max_flips": 3}),
    ("16bars_4flips", {"window": 16, "max_flips": 4}),
    ("24bars_4flips", {"window": 24, "max_flips": 4}),
    ("24bars_5flips", {"window": 24, "max_flips": 5}),
]
for vname, vcfg in osc_variants:
    r = run_contender_guard(
        f"D_osc_{vname}",
        {"score_osc": vcfg},
        description=f"Score oscillation guard: {vname}",
    )
    group_d.append(r)
    all_results.append(r)

group_d_sorted = print_batch_summary("GROUP D: SCORE OSCILLATION", group_d, BASELINE_PNL)
best_osc = None
if group_d_sorted[0]["name"] != "BASELINE_no_guard":
    best_osc_name = group_d_sorted[0]["name"].replace("D_osc_", "")
    for vn, vc in osc_variants:
        if vn == best_osc_name:
            best_osc = vc
            break

# ── Group E: Drawdown Breaker (3) ──
print("\n" + "#" * 70)
print("# GROUP E: Intraday Drawdown Breaker")
print("#" * 70)

group_e = [baseline]
for dd_thr in [-0.02, -0.03, -0.05]:
    r = run_contender_guard(
        f"E_dd_breaker_{abs(dd_thr)*100:.0f}pct",
        {},  # no static guards, only drawdown breaker
        dd_threshold=dd_thr,
        description=f"Intraday drawdown breaker: {dd_thr*100:.0f}%",
    )
    group_e.append(r)
    all_results.append(r)

group_e_sorted = print_batch_summary("GROUP E: DRAWDOWN BREAKER", group_e, BASELINE_PNL)
best_dd = None
if group_e_sorted[0]["name"] != "BASELINE_no_guard":
    best_dd = float(group_e_sorted[0].get("dd_threshold") or 0)


# ══════════════════════════════════════════════════════════
# PHASE 1 SUMMARY
# ══════════════════════════════════════════════════════════

phase1_sorted = print_batch_summary("PHASE 1: ALL INDIVIDUAL GUARDS", all_results, BASELINE_PNL)
best_phase1 = phase1_sorted[0]

print(f"\n{'='*70}")
print(f"PHASE 1 WINNER: {best_phase1['name']}")
print(f"  PnL: ${best_phase1['total_pnl']:+,.0f} | WR: {best_phase1['win_rate']:.1f}% | "
      f"Sharpe: {best_phase1['sharpe']:.2f} | DD: {best_phase1['max_dd_pct']:.1f}%")
gs = best_phase1.get("guard_stats", {})
print(f"  Guards blocked: {gs.get('signals_blocked', 0)} ({gs.get('pct_blocked', 0):.1f}%) | "
      f"Savings: ${gs.get('guard_savings', 0):+,.0f}")
print(f"\n  Per-group winners:")
print(f"    Weekend: {best_weekend or 'BASELINE (no improvement)'}")
print(f"    ATR Spike: {best_atr or 'BASELINE (no improvement)'}")
print(f"    Double Liq: {best_dliq or 'BASELINE (no improvement)'}")
print(f"    Score Osc: {best_osc or 'BASELINE (no improvement)'}")
print(f"    DD Breaker: {best_dd or 'BASELINE (no improvement)'}")
print(f"{'='*70}")


# ══════════════════════════════════════════════════════════
# PHASE 2: COMBINED GUARDS (5-8 experiments)
# ══════════════════════════════════════════════════════════

print("\n" + "#" * 70)
print("# PHASE 2: COMBINED GUARDS")
print("#" * 70)

phase2_results = []

# Build combined configs from Phase 1 winners
# Only include guards that beat baseline
winning_guards = {}
if best_weekend:
    winning_guards["weekend"] = best_weekend
if best_atr:
    winning_guards["atr_spike_mult"] = float(best_atr)
if best_dliq:
    winning_guards["double_liq"] = best_dliq
if best_osc:
    winning_guards["score_osc"] = best_osc

print(f"\n  Winning guards from Phase 1: {list(winning_guards.keys()) or 'NONE'}")

if len(winning_guards) >= 2:
    # Combo 1: Best weekend + Best ATR
    if best_weekend and best_atr:
        cfg = {"weekend": best_weekend, "atr_spike_mult": float(best_atr)}
        r = run_contender_guard("P2_weekend+atr", cfg, description="Weekend + ATR spike")
        phase2_results.append(r)

    # Combo 2: Best weekend + Best double liq
    if best_weekend and best_dliq:
        cfg = {"weekend": best_weekend, "double_liq": best_dliq}
        r = run_contender_guard("P2_weekend+dliq", cfg, description="Weekend + Double liq")
        phase2_results.append(r)

    # Combo 3: Best weekend + Best score osc
    if best_weekend and best_osc:
        cfg = {"weekend": best_weekend, "score_osc": best_osc}
        r = run_contender_guard("P2_weekend+osc", cfg, description="Weekend + Score oscillation")
        phase2_results.append(r)

    # Combo 4: All winning static guards
    r = run_contender_guard("P2_all_winners", winning_guards, description="All Phase 1 winning guards combined")
    phase2_results.append(r)

    # Combo 5: All winners + drawdown breaker
    if best_dd:
        r = run_contender_guard("P2_all_winners+dd", winning_guards, dd_threshold=best_dd,
                                description="All winners + drawdown breaker")
        phase2_results.append(r)

elif len(winning_guards) == 1:
    # Only one winning guard — combine with drawdown breaker
    r = run_contender_guard("P2_winner_only", winning_guards, description="Single winning guard")
    phase2_results.append(r)

    if best_dd:
        r = run_contender_guard("P2_winner+dd", winning_guards, dd_threshold=best_dd,
                                description="Single winner + drawdown breaker")
        phase2_results.append(r)

else:
    # No guards beat baseline — try aggressive combos anyway
    print("  No individual guards beat baseline. Trying aggressive combos...")

    # Try weekend + tight ATR
    r = run_contender_guard("P2_weekend+atr1.5",
                            {"weekend": "block_all", "atr_spike_mult": 1.5},
                            description="Aggressive: block weekends + ATR 1.5x")
    phase2_results.append(r)

    # Try weekend + score osc + dd breaker
    r = run_contender_guard("P2_weekend+osc+dd",
                            {"weekend": "block_all", "score_osc": {"window": 16, "max_flips": 3}},
                            dd_threshold=-0.03,
                            description="Aggressive: weekend + osc + DD 3%")
    phase2_results.append(r)

    # Try all guards together
    r = run_contender_guard("P2_kitchen_sink",
                            {"weekend": "block_all", "atr_spike_mult": 2.0,
                             "double_liq": {"mode": "both_spike", "ma_mult": 2.0},
                             "score_osc": {"window": 16, "max_flips": 4}},
                            dd_threshold=-0.03,
                            description="Kitchen sink: all guards together")
    phase2_results.append(r)

if phase2_results:
    phase2_all = [baseline] + phase2_results
    phase2_sorted = print_batch_summary("PHASE 2: COMBINED GUARDS", phase2_all, BASELINE_PNL)
else:
    phase2_sorted = [baseline]
    print("  No Phase 2 experiments run.")


# ══════════════════════════════════════════════════════════
# PHASE 3: CROSS-MODEL VALIDATION (v3, v5, v6)
# ══════════════════════════════════════════════════════════

print("\n" + "#" * 70)
print("# PHASE 3: CROSS-MODEL VALIDATION")
print("#" * 70)

# Find overall best guard config
all_phase1_and_2 = all_results + phase2_results
overall_best = max(all_phase1_and_2, key=lambda x: x["total_pnl"])

if overall_best["name"] == "BASELINE_no_guard":
    print("\n  No guard configuration beat baseline. Skipping Phase 3.")
    print("  VERDICT: Guards do NOT help for this strategy/period.")
    phase3_results = []
else:
    print(f"\n  Best config: {overall_best['name']} (${overall_best['total_pnl']:+,.0f})")

    # Extract winning guard config for cross-model test
    # Rebuild the actual guard config from the winner
    winner_guard_config = {}
    winner_dd = overall_best.get("dd_threshold")

    # Parse guard_config back from the stored string representation
    gc = overall_best.get("guard_config", {})
    for k, v in gc.items():
        if v and v != 'None':
            try:
                winner_guard_config[k] = eval(v)  # safe here: our own data
            except Exception:
                winner_guard_config[k] = v

    print(f"  Guard config: {winner_guard_config}")
    if winner_dd:
        print(f"  DD threshold: {winner_dd}")

    # Test on v3 model (8-factor composite)
    from signal_core import compute_btc_composite_score

    phase3_results = []

    # V3 model test
    print("\n  [V3] Testing with 8-factor composite score...")
    v3_score = compute_btc_composite_score(btc_df)
    v3_score_ts = pd.Series(v3_score.values, index=btc_df["ts"].values)

    v3_trades = []
    for coin in COINS:
        cfg = V6_CONFIGS[coin]  # same SL/TP config
        sig, alt_m = gen_signal_with_options(
            v3_score_ts, alt_data[coin],
            entry_threshold=cfg["threshold"],
            hysteresis_band=R3_HYSTERESIS,
        )
        oos_mask = (alt_m["ts"] >= pd.Timestamp(OOS_START)) & (alt_m["ts"] <= pd.Timestamp(OOS_END))
        alt_oos = alt_m[oos_mask].reset_index(drop=True)
        sig_oos = sig[oos_mask].reset_index(drop=True)
        if len(alt_oos) < 100:
            continue
        btc_s = alt_oos["btc_score"] if "btc_score" in alt_oos.columns else pd.Series(0, index=alt_oos.index)
        gm, _ = compute_guards(btc_df, alt_oos, btc_s, winner_guard_config)
        if winner_dd:
            trades, _ = run_backtest_guarded_dd(alt_oos, sig_oos, gm, dd_threshold=winner_dd,
                                                 sl_atr_mult=cfg["sl"], tp_atr_mult=cfg["tp"],
                                                 cooldown_bars=cfg["cd"], flip_mode=R3_FLIP_MODE,
                                                 flip_cooldown_extra=R3_CD_EXTRA)
        else:
            trades, _ = run_backtest_guarded(alt_oos, sig_oos, gm,
                                              sl_atr_mult=cfg["sl"], tp_atr_mult=cfg["tp"],
                                              cooldown_bars=cfg["cd"], flip_mode=R3_FLIP_MODE,
                                              flip_cooldown_extra=R3_CD_EXTRA)
        if len(trades) > 0:
            trades["coin"] = coin
            v3_trades.append(trades)

    if v3_trades:
        v3_combined = pd.concat(v3_trades, ignore_index=True)
        v3_pnl = v3_combined["pnl_net"].sum()
        v3_n = len(v3_combined)
        v3_wr = 100 * (v3_combined["pnl_net"] > 0).sum() / v3_n
        print(f"    V3 + Guard: {v3_n} trades | WR {v3_wr:.1f}% | PnL ${v3_pnl:+,.0f}")
        phase3_results.append({"name": "V3_with_guard", "pnl": round(v3_pnl, 2),
                                "trades": v3_n, "wr": round(v3_wr, 1)})

    # V6 baseline vs guarded (already have these)
    phase3_results.append({"name": "V6_baseline", "pnl": BASELINE_PNL,
                            "trades": baseline["total_trades"], "wr": baseline["win_rate"]})
    phase3_results.append({"name": "V6_with_guard", "pnl": overall_best["total_pnl"],
                            "trades": overall_best["total_trades"], "wr": overall_best["win_rate"]})

    print(f"\n  Cross-model results:")
    for pr in phase3_results:
        print(f"    {pr['name']:<20} {pr['trades']:>5} trades | WR {pr['wr']:.1f}% | PnL ${pr['pnl']:>+9,.0f}")


# ══════════════════════════════════════════════════════════
# FINAL SUMMARY + SAVE RESULTS
# ══════════════════════════════════════════════════════════

print("\n" + "=" * 70)
print("TOURNAMENT ROUND 4: FINAL RESULTS")
print("=" * 70)

all_experiments = all_results + phase2_results
champion = max(all_experiments, key=lambda x: x["total_pnl"])

print(f"\n  CHAMPION: {champion['name']}")
print(f"  PnL: ${champion['total_pnl']:+,.0f} | WR: {champion['win_rate']:.1f}% | "
      f"Sharpe: {champion['sharpe']:.2f} | DD: {champion['max_dd_pct']:.1f}%")
print(f"  vs Baseline: ${champion['total_pnl'] - BASELINE_PNL:+,.0f} "
      f"({(champion['total_pnl'] - BASELINE_PNL) / max(abs(BASELINE_PNL), 1) * 100:+.1f}%)")

gs = champion.get("guard_stats", {})
print(f"\n  Guard Stats:")
print(f"    Signals blocked: {gs.get('signals_blocked', 0)} ({gs.get('pct_blocked', 0):.1f}%)")
print(f"    Blocked would-have PnL: ${gs.get('blocked_would_pnl', 0):+,.0f} "
      f"({gs.get('blocked_would_trades', 0)} trades, {gs.get('blocked_would_wr', 0):.0f}% WR)")
print(f"    Guard savings: ${gs.get('guard_savings', 0):+,.0f}")
print(f"    Per-guard breakdown: {gs.get('per_guard', {})}")

# Exit breakdown
bd = champion.get("exit_breakdown", {})
if bd:
    print(f"\n  Exit Breakdown:")
    for reason in ["SL", "TP", "TIMEOUT", "SIGNAL_FLIP"]:
        if reason in bd:
            e = bd[reason]
            print(f"    {reason:<15} {e['count']:>5} trades | ${e['pnl']:>+9,.0f} | "
                  f"WR {e['wr']:>5.1f}% | avg {e['avg_bars']:.0f} bars")

# Regime analysis
rr = champion.get("regime_results", {})
if rr:
    print(f"\n  Regime Analysis:")
    for rname, rdata in rr.items():
        print(f"    {rname:<10} {rdata['trades']:>4} trades | ${rdata['pnl']:>+8,.0f} | WR {rdata['wr']:.1f}%")

# Per-coin
print(f"\n  Per-Coin:")
for coin in COINS:
    if coin in champion.get("coin_results", {}):
        cr = champion["coin_results"][coin]
        print(f"    {coin:<5} {cr['trades']:>4} trades | ${cr['pnl']:>+8,.0f} | "
              f"WR {cr['wr']:.1f}% | Sharpe {cr['sharpe']:.2f}")

# Verdict
print(f"\n  {'='*60}")
if champion["name"] == "BASELINE_no_guard":
    print(f"  VERDICT: NO GUARD IMPROVES THE STRATEGY")
    print(f"  The R3 champion configuration is already robust.")
    verdict = "NO_IMPROVEMENT"
elif champion["total_pnl"] > BASELINE_PNL * 1.02:  # >2% improvement
    print(f"  VERDICT: GUARD SYSTEM RECOMMENDED FOR DEPLOYMENT")
    print(f"  Config: {champion.get('guard_config', {})}")
    if champion.get("dd_threshold"):
        print(f"  DD Breaker: {champion['dd_threshold']}")
    verdict = "DEPLOY"
else:
    print(f"  VERDICT: MARGINAL IMPROVEMENT — MONITOR BEFORE DEPLOY")
    verdict = "MONITOR"
print(f"  {'='*60}")

# Save results
final_results = {
    "tournament": "round_4_market_guard",
    "run_date": datetime.now().isoformat(),
    "baseline_pnl": BASELINE_PNL,
    "verdict": verdict,
    "champion": {
        "name": champion["name"],
        "guard_config": champion.get("guard_config", {}),
        "dd_threshold": champion.get("dd_threshold"),
        "total_pnl": champion["total_pnl"],
        "win_rate": champion["win_rate"],
        "sharpe": champion["sharpe"],
        "max_dd_pct": champion["max_dd_pct"],
        "guard_stats": champion.get("guard_stats", {}),
        "exit_breakdown": champion.get("exit_breakdown", {}),
        "regime_results": champion.get("regime_results", {}),
        "coin_results": champion.get("coin_results", {}),
    },
    "phase1_winners": {
        "weekend": best_weekend,
        "atr_spike": best_atr,
        "double_liq": str(best_dliq) if best_dliq else None,
        "score_osc": str(best_osc) if best_osc else None,
        "dd_breaker": best_dd,
    },
    "phase3_cross_model": phase3_results if 'phase3_results' in dir() else [],
    "all_experiments": [{
        "name": r["name"],
        "guard_config": r.get("guard_config", {}),
        "dd_threshold": r.get("dd_threshold"),
        "total_pnl": r["total_pnl"],
        "total_trades": r["total_trades"],
        "win_rate": r["win_rate"],
        "sharpe": r["sharpe"],
        "max_dd_pct": r["max_dd_pct"],
        "guard_stats": r.get("guard_stats", {}),
    } for r in all_experiments],
}

with open(RESULTS_FILE, "w") as f:
    json.dump(final_results, f, indent=2, default=str)

# Generate SUMMARY.md
summary_lines = [
    f"# Tournament Round 4: Market Guard System",
    f"",
    f"**Run date**: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
    f"**Verdict**: {verdict}",
    f"",
    f"## Baseline (R3 Champion)",
    f"- PnL: ${BASELINE_PNL:+,.0f}",
    f"- Trades: {baseline['total_trades']}",
    f"- WR: {baseline['win_rate']:.1f}%",
    f"",
    f"## Champion: {champion['name']}",
    f"- PnL: ${champion['total_pnl']:+,.0f} ({(champion['total_pnl'] - BASELINE_PNL) / max(abs(BASELINE_PNL), 1) * 100:+.1f}% vs baseline)",
    f"- Trades: {champion['total_trades']}",
    f"- WR: {champion['win_rate']:.1f}%",
    f"- Sharpe: {champion['sharpe']:.2f}",
    f"- Max DD: {champion['max_dd_pct']:.1f}%",
    f"",
    f"## Guard Stats",
    f"- Signals blocked: {gs.get('signals_blocked', 0)} ({gs.get('pct_blocked', 0):.1f}%)",
    f"- Blocked trades would-have PnL: ${gs.get('blocked_would_pnl', 0):+,.0f}",
    f"- Guard savings: ${gs.get('guard_savings', 0):+,.0f}",
    f"",
    f"## Phase 1 Winners (per-group)",
    f"| Group | Guard | Winner |",
    f"|-------|-------|--------|",
    f"| A | Weekend | {best_weekend or 'Baseline'} |",
    f"| B | ATR Spike | {best_atr or 'Baseline'} |",
    f"| C | Double Liq | {best_dliq or 'Baseline'} |",
    f"| D | Score Osc | {best_osc or 'Baseline'} |",
    f"| E | DD Breaker | {best_dd or 'Baseline'} |",
    f"",
    f"## All Experiments (sorted by PnL)",
    f"| Rank | Name | Trades | WR% | PnL | Blocked | Savings | Delta |",
    f"|------|------|--------|-----|-----|---------|---------|-------|",
]

for i, r in enumerate(sorted(all_experiments, key=lambda x: x["total_pnl"], reverse=True), 1):
    delta = r["total_pnl"] - BASELINE_PNL
    gs_r = r.get("guard_stats", {})
    summary_lines.append(
        f"| {i} | {r['name']} | {r['total_trades']} | {r['win_rate']:.1f}% | "
        f"${r['total_pnl']:+,.0f} | {gs_r.get('signals_blocked', 0)} | "
        f"${gs_r.get('guard_savings', 0):+,.0f} | ${delta:+,.0f} |"
    )

if phase3_results:
    summary_lines.extend([
        f"",
        f"## Phase 3: Cross-Model Validation",
        f"| Model | Trades | WR% | PnL |",
        f"|-------|--------|-----|-----|",
    ])
    for pr in phase3_results:
        summary_lines.append(f"| {pr['name']} | {pr['trades']} | {pr['wr']:.1f}% | ${pr['pnl']:+,.0f} |")

summary_md = "\n".join(summary_lines) + "\n"
with open(RESULTS_DIR / "SUMMARY.md", "w") as f:
    f.write(summary_md)

print(f"\n  Results saved to {RESULTS_FILE}")
print(f"  Summary saved to {RESULTS_DIR / 'SUMMARY.md'}")
print(f"  Total elapsed: {time.time()-t0:.0f}s")
print("=" * 70)
