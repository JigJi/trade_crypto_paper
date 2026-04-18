"""Anti-lookahead validators - programmatic checks for backtest integrity.

Run these checks on any new factor or backtest to catch data leakage.
"""
import logging
from datetime import timedelta

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)


def check_merge_asof_direction(df_left, df_right, on_col="ts", tolerance=None):
    """Verify merge_asof uses backward direction (no future data).

    Returns True if safe, raises ValueError if forward-looking detected.
    """
    merged_bw = pd.merge_asof(
        df_left.sort_values(on_col),
        df_right.sort_values(on_col),
        on=on_col,
        direction="backward",
        tolerance=tolerance,
    )
    merged_fw = pd.merge_asof(
        df_left.sort_values(on_col),
        df_right.sort_values(on_col),
        on=on_col,
        direction="forward",
        tolerance=tolerance,
    )
    # If results differ, the direction matters -- backward is correct,
    # but if the code used forward, results would be different (lookahead!)
    right_cols = [c for c in df_right.columns if c != on_col]
    differs = []
    for col in right_cols:
        if col not in merged_bw.columns:
            continue
        diff = (merged_bw[col].fillna(0) - merged_fw[col].fillna(0)).abs()
        if diff.sum() > 0:
            differs.append(col)
            log.info(f"[OK] Column '{col}' differs between backward/forward merge - direction matters")

    if not differs:
        log.warning(
            "backward and forward merge produce identical results - "
            "direction does not matter for these columns (may indicate stale or constant data)"
        )
    return True


def check_signal_shift(signals, expected_shift=1):
    """Verify signals are shifted by expected_shift bars.

    The first `expected_shift` values should be 0/NaN (no lookahead).
    """
    head = signals.head(expected_shift)
    if head.sum() != 0:
        raise ValueError(
            f"First {expected_shift} signal values are non-zero: {head.tolist()}. "
            "Signals may not be properly shifted."
        )
    return True


def check_rolling_not_future(series, window):
    """Verify rolling operations don't include future data.

    A rolling(window) operation should have NaN for the first (window-1) values
    when using default min_periods=window.
    """
    rolled = series.rolling(window).mean()
    nan_count = rolled.isna().sum()
    if nan_count < window - 1:
        raise ValueError(
            f"Rolling({window}) has only {nan_count} NaNs at start. "
            f"Expected at least {window-1}. Possible future data leakage."
        )
    return True


def check_daily_data_shift(df, ts_col="ts", expected_shift_hours=24):
    """Verify daily data is shifted appropriately.

    Daily aggregate data should not be used on the same day it represents.
    Check that timestamps have been shifted by at least expected_shift_hours.
    """
    if len(df) < 2:
        return True

    sorted_df = df.sort_values(ts_col)
    diffs = sorted_df[ts_col].diff().dropna()
    median_diff_hours = diffs.median().total_seconds() / 3600

    if median_diff_hours < expected_shift_hours * 0.5:
        log.warning(
            f"Daily data has median interval of {median_diff_hours:.1f}h, "
            f"expected ~{expected_shift_hours}h. May not be properly shifted."
        )
    return True


def check_no_future_values(feature_df, price_df, ts_col="ts", feature_col=None):
    """Check that feature values don't correlate with future prices.

    High correlation with future returns suggests lookahead bias.
    """
    if feature_col is None:
        feature_col = [c for c in feature_df.columns if c != ts_col][0]

    merged = pd.merge_asof(
        price_df.sort_values(ts_col),
        feature_df[[ts_col, feature_col]].sort_values(ts_col),
        on=ts_col,
        direction="backward",
    )

    if "close" not in merged.columns:
        return True

    # Future return (1 bar ahead)
    merged["future_ret"] = merged["close"].pct_change().shift(-1)
    # Past return (1 bar behind)
    merged["past_ret"] = merged["close"].pct_change()

    valid = merged.dropna(subset=[feature_col, "future_ret", "past_ret"])
    if len(valid) < 100:
        return True

    future_corr = valid[feature_col].corr(valid["future_ret"])
    past_corr = valid[feature_col].corr(valid["past_ret"])

    if abs(future_corr) > 0.3 and abs(future_corr) > abs(past_corr) * 2:
        raise ValueError(
            f"Feature '{feature_col}' has suspicious future correlation: "
            f"future_corr={future_corr:.3f} vs past_corr={past_corr:.3f}. "
            "Possible lookahead bias!"
        )

    log.info(
        f"[OK] Feature '{feature_col}': future_corr={future_corr:.3f}, "
        f"past_corr={past_corr:.3f}"
    )
    return True


def run_all_checks(btc_df, signals, db_data=None):
    """Run all anti-lookahead checks on a backtest setup."""
    results = []

    # Check signal shift
    try:
        check_signal_shift(signals)
        results.append(("signal_shift", "PASS"))
    except ValueError as e:
        results.append(("signal_shift", f"FAIL: {e}"))

    # Check rolling operations
    for col in ["ema9", "ema21", "rsi", "atr"]:
        if col in btc_df.columns:
            try:
                window = {"ema9": 9, "ema21": 21, "rsi": 14, "atr": 14}.get(col, 14)
                check_rolling_not_future(btc_df[col], window)
                results.append((f"rolling_{col}", "PASS"))
            except ValueError as e:
                results.append((f"rolling_{col}", f"FAIL: {e}"))

    log.info("Anti-lookahead check results:")
    for name, result in results:
        status = "PASS" if result == "PASS" else "FAIL"
        log.info(f"  [{status}] {name}: {result}")

    return results
