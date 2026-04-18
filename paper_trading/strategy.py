"""
Strategy: BTC Composite Score + Signal Generation
===================================================
Wraps signal_core functions for live use.
Key difference from backtest: NO .shift(1) -- signal evaluates on closed candle,
entry at current market price.
"""

import logging

import numpy as np
import pandas as pd

from signal_core import (
    resample_to_15m,
    build_btc_features,
    build_alt_technicals,
    score_basis_contrarian,
    score_tick_liq,
    score_ob_combined,
    compute_btc_composite_score,
    compute_raw_signal,
    is_dead_zone,
    check_pa_alignment,
    detect_spike_bar,
    classify_spike_bar,
    DEFAULT_COMPOSITE_WEIGHTS,
    DEFAULT_EXTRA_WEIGHTS,
    DEFAULT_SPIKE_CONFIG,
)

from .config import (
    COMPOSITE_WEIGHTS, SPIKE_CONFIG, SPIKE_ENABLED, V3_EXTRA_WEIGHTS,
    HYSTERESIS_BAND, VOL_REGIME_LOOKBACK, VOL_REGIME_THRESHOLDS,
    FLIP_CONFIG,
)

logger = logging.getLogger(__name__)


def evaluate_signal(btc_df: pd.DataFrame, btc_score: pd.Series,
                    alt_df: pd.DataFrame, coin: str,
                    threshold: float, use_alt_pa_filter: bool,
                    prev_signal: int = 0, model: str = "v3") -> dict:
    """
    Evaluate signal for a single coin at the current bar.
    Returns dict with signal info for logging and action.

    Unlike backtest, NO .shift(1) -- we evaluate on the latest closed candle
    and entry would be at current market price.

    Includes volatility spike overlay when SPIKE_ENABLED=True.

    Hysteresis: when prev_signal != 0, uses lower exit threshold to keep
    signal active longer (reduces whipsaw from score oscillating around threshold).
    """
    if alt_df.empty or btc_df.empty:
        return {"signal": 0, "btc_score": 0.0, "pa_aligned": None, "reason": "NO_DATA"}

    # Get latest BTC score
    latest_score = float(btc_score.iloc[-1])

    # Get latest alt bar and BTC bar
    alt_latest = alt_df.iloc[-1]
    btc_latest = btc_df.iloc[-1]

    # Dead zone filter
    bar_hour = alt_latest["ts"].hour
    if is_dead_zone(bar_hour):
        return {
            "signal": 0,
            "btc_score": latest_score,
            "pa_aligned": None,
            "reason": f"DEAD_ZONE ({bar_hour:02d}:00 UTC)",
        }

    # Core signal logic with hysteresis (per-model config from Tournament R3)
    hyst_band = FLIP_CONFIG.get(model, {}).get("hysteresis_band", HYSTERESIS_BAND)
    raw_signal = compute_raw_signal(latest_score, threshold, prev_signal, hyst_band)

    # If no normal signal, check spike overlay
    if raw_signal == 0 and SPIKE_ENABLED:
        cfg = SPIKE_CONFIG
        if detect_spike_bar(btc_latest, cfg):
            spike_mode = classify_spike_bar(btc_latest, cfg)

            if spike_mode == "contrarian":
                adj_thr = max(threshold - cfg["contrarian_reduction"], 0.5)
                ret = btc_latest.get("ret", 0)
                if pd.notna(ret) and ret < -0.005 and latest_score >= adj_thr:
                    raw_signal = 1
                    logger.info(f"[{coin}] SPIKE-CONTRARIAN LONG: ret={ret:.4f}, "
                                f"score={latest_score:.2f} >= {adj_thr:.1f}")
                elif pd.notna(ret) and ret > 0.005 and latest_score <= -adj_thr:
                    raw_signal = -1
                    logger.info(f"[{coin}] SPIKE-CONTRARIAN SHORT: ret={ret:.4f}, "
                                f"score={latest_score:.2f} <= -{adj_thr:.1f}")
            else:  # momentum
                adj_thr = max(threshold - cfg["momentum_reduction"], 1.0)
                if latest_score >= adj_thr:
                    raw_signal = 1
                    logger.info(f"[{coin}] SPIKE-MOMENTUM LONG: "
                                f"score={latest_score:.2f} >= {adj_thr:.1f}")
                elif latest_score <= -adj_thr:
                    raw_signal = -1
                    logger.info(f"[{coin}] SPIKE-MOMENTUM SHORT: "
                                f"score={latest_score:.2f} <= -{adj_thr:.1f}")

    if raw_signal == 0:
        return {
            "signal": 0,
            "btc_score": latest_score,
            "pa_aligned": None,
            "reason": f"BELOW_THRESHOLD ({latest_score:.2f} vs ±{threshold})",
        }

    # PA filter (if enabled) — uses shared logic
    pa_aligned, should_suppress = check_pa_alignment(alt_latest, raw_signal, use_alt_pa_filter)

    if should_suppress:
        return {
            "signal": 0,
            "btc_score": latest_score,
            "pa_aligned": 0,
            "reason": f"PA_NOT_ALIGNED (signal={raw_signal})",
        }

    return {
        "signal": raw_signal,
        "btc_score": latest_score,
        "pa_aligned": pa_aligned if pa_aligned is not None else 1,
        "reason": "SIGNAL_GENERATED",
    }


def count_active_factors(df: pd.DataFrame, params: dict = None, extra: dict = None) -> int:
    """
    Count how many of the 8 BTC factors are non-zero at the latest bar.
    Used by extreme_conf3 filter (Mission 013).
    """
    if params is None:
        params = COMPOSITE_WEIGHTS
    if extra is None:
        extra = V3_EXTRA_WEIGHTS

    if df.empty:
        return 0

    latest = df.iloc[[-1]]  # keep as DataFrame for score functions
    count = 0

    # 1. OI divergence
    if "oi_chg" in latest.columns:
        oi_chg = latest["oi_chg"].fillna(0).iloc[0]
        ret = latest["ret"].fillna(0).iloc[0]
        if ((ret > 0.001 and oi_chg > 0.002) or (ret < -0.001 and oi_chg < -0.002) or
                (ret > 0.001 and oi_chg < -0.002) or (ret < -0.001 and oi_chg > 0.002)):
            count += 1

    # 2. Funding rate
    if "fr_8h" in latest.columns:
        fr = latest["fr_8h"].fillna(0).iloc[0]
        if fr < -0.0001 or fr > 0.0003:
            count += 1
    elif "last_funding_rate" in latest.columns:
        fr = latest["last_funding_rate"].fillna(0).iloc[0]
        if fr < -0.00005 or fr > 0.0002:
            count += 1

    # 3. Whale alerts
    if "whale_net_ma" in latest.columns:
        wn = latest["whale_net_ma"].fillna(0).iloc[0]
        if abs(wn) > 50_000_000:
            count += 1

    # 4. Liquidation cascades
    if "liq_net" in latest.columns and "liq_total_ma" in latest.columns:
        lt = latest["liq_total"].fillna(0).iloc[0]
        lt_ma = latest["liq_total_ma"].fillna(1).iloc[0]
        if lt > (lt_ma * 3):
            count += 1

    # 5. ETF flows
    if "etf_flow_ma" in latest.columns:
        etf = latest["etf_flow_ma"].fillna(0).iloc[0]
        if abs(etf) > 50:
            count += 1

    # 6. Basis contrarian
    if score_basis_contrarian(latest, weight=extra.get("basis_contrarian", 1.5)).iloc[0] != 0:
        count += 1

    # 7. Tick liquidation
    if score_tick_liq(latest, weight=extra.get("tick_liq", 2.0)).iloc[0] != 0:
        count += 1

    # 8. Order book combined
    if score_ob_combined(latest, weight=extra.get("ob_combined", 2.0)).iloc[0] != 0:
        count += 1

    return count


def compute_vol_regime(df: pd.DataFrame) -> str:
    """
    Classify current BTC volatility regime based on 24h realized vol.
    Uses fixed thresholds from 15-month OOS backtest (robust).
    Returns: 'Low', 'Normal', 'High', 'Extreme', or 'Unknown'.
    """
    if df.empty or "close" not in df.columns:
        return "Unknown"

    lookback = VOL_REGIME_LOOKBACK
    if len(df) < lookback + 2:
        return "Unknown"

    log_ret = np.log(df["close"] / df["close"].shift(1))
    rv = log_ret.rolling(lookback).std() * np.sqrt(4 * 24 * 365)

    current_rv = rv.iloc[-1]
    if pd.isna(current_rv):
        return "Unknown"

    q25 = VOL_REGIME_THRESHOLDS["q25"]
    q75 = VOL_REGIME_THRESHOLDS["q75"]
    q90 = VOL_REGIME_THRESHOLDS["q90"]

    if current_rv <= q25:
        return "Low"
    elif current_rv <= q75:
        return "Normal"
    elif current_rv <= q90:
        return "High"
    else:
        return "Extreme"
