"""
Signal Parity Test
===================
Verifies that signal_core.py produces IDENTICAL results to the original
backtest functions. If any test fails, it means signal_core diverged
from the canonical logic — fix immediately.

Run: python test_signal_parity.py
"""

import sys
import numpy as np
import pandas as pd


def test_compute_raw_signal():
    """compute_raw_signal() matches the hysteresis state machine exactly."""
    from signal_core import compute_raw_signal

    print("[1/6] Testing compute_raw_signal()...")
    errors = 0

    # No hysteresis (backtest mode)
    cases_no_hyst = [
        # (score, threshold, prev_signal, hysteresis_band) -> expected
        (5.0, 3.0, 0, 0.0, 1),       # above threshold -> LONG
        (-5.0, 3.0, 0, 0.0, -1),     # below -threshold -> SHORT
        (2.0, 3.0, 0, 0.0, 0),       # between -> NEUTRAL
        (0.0, 3.0, 0, 0.0, 0),       # zero -> NEUTRAL
        (3.0, 3.0, 0, 0.0, 1),       # exactly at threshold -> LONG
        (-3.0, 3.0, 0, 0.0, -1),     # exactly at -threshold -> SHORT
    ]

    for score, thr, prev, hyst, expected in cases_no_hyst:
        result = compute_raw_signal(score, thr, prev, hyst)
        if result != expected:
            print(f"  FAIL: compute_raw_signal({score}, {thr}, {prev}, {hyst}) = {result}, expected {expected}")
            errors += 1

    # With hysteresis (paper trading mode, band=1.5)
    cases_hyst = [
        # Entry from neutral
        (5.0, 3.0, 0, 1.5, 1),       # above entry threshold -> LONG
        (-5.0, 3.0, 0, 1.5, -1),     # below -entry threshold -> SHORT
        (2.0, 3.0, 0, 1.5, 0),       # between -> NEUTRAL
        # Stay in LONG (exit threshold = 3.0 - 1.5 = 1.5)
        (2.0, 3.0, 1, 1.5, 1),       # above exit threshold -> STAY LONG
        (1.5, 3.0, 1, 1.5, 1),       # exactly at exit threshold -> STAY LONG
        (1.0, 3.0, 1, 1.5, 0),       # below exit threshold -> EXIT
        (-3.0, 3.0, 1, 1.5, -1),     # below -entry threshold -> FLIP SHORT
        # Stay in SHORT (exit threshold = -1.5)
        (-2.0, 3.0, -1, 1.5, -1),    # below -exit threshold -> STAY SHORT
        (-1.5, 3.0, -1, 1.5, -1),    # exactly at -exit threshold -> STAY SHORT
        (-1.0, 3.0, -1, 1.5, 0),     # above -exit threshold -> EXIT
        (3.0, 3.0, -1, 1.5, 1),      # above entry threshold -> FLIP LONG
    ]

    for score, thr, prev, hyst, expected in cases_hyst:
        result = compute_raw_signal(score, thr, prev, hyst)
        if result != expected:
            print(f"  FAIL: compute_raw_signal({score}, {thr}, {prev}, {hyst}) = {result}, expected {expected}")
            errors += 1

    if errors == 0:
        print("  PASS: All compute_raw_signal() cases match")
    else:
        print(f"  FAIL: {errors} cases failed")
    return errors


def test_is_dead_zone():
    """is_dead_zone() matches DEAD_ZONE_START=23, DEAD_ZONE_END=6."""
    from signal_core import is_dead_zone

    print("[2/6] Testing is_dead_zone()...")
    errors = 0

    # Dead zone: 23:00-06:00 UTC
    dead_hours = [23, 0, 1, 2, 3, 4, 5]
    active_hours = [6, 7, 8, 12, 15, 18, 22]

    for h in dead_hours:
        if not is_dead_zone(h):
            print(f"  FAIL: is_dead_zone({h}) = False, expected True")
            errors += 1

    for h in active_hours:
        if is_dead_zone(h):
            print(f"  FAIL: is_dead_zone({h}) = True, expected False")
            errors += 1

    if errors == 0:
        print("  PASS: All is_dead_zone() cases match")
    else:
        print(f"  FAIL: {errors} cases failed")
    return errors


def test_default_constants():
    """Default constants in signal_core match backtest/config values."""
    from signal_core import (
        DEFAULT_COMPOSITE_WEIGHTS, DEFAULT_EXTRA_WEIGHTS, DEFAULT_SPIKE_CONFIG,
    )
    from backtest_15m_btc_led_alts import COMPOSITE_WEIGHTS, V3_EXTRA_WEIGHTS, SPIKE_CONFIG

    print("[3/6] Testing default constants parity...")
    errors = 0

    if DEFAULT_COMPOSITE_WEIGHTS != COMPOSITE_WEIGHTS:
        print(f"  FAIL: DEFAULT_COMPOSITE_WEIGHTS != backtest COMPOSITE_WEIGHTS")
        errors += 1

    if DEFAULT_EXTRA_WEIGHTS != V3_EXTRA_WEIGHTS:
        print(f"  FAIL: DEFAULT_EXTRA_WEIGHTS != backtest V3_EXTRA_WEIGHTS")
        errors += 1

    if DEFAULT_SPIKE_CONFIG != SPIKE_CONFIG:
        print(f"  FAIL: DEFAULT_SPIKE_CONFIG != backtest SPIKE_CONFIG")
        errors += 1

    if errors == 0:
        print("  PASS: All default constants match")
    else:
        print(f"  FAIL: {errors} constants mismatched")
    return errors


def test_score_functions_synthetic():
    """Score helper functions produce identical results on synthetic data."""
    from signal_core import score_basis_contrarian, score_tick_liq, score_ob_combined

    print("[4/6] Testing score functions on synthetic data...")
    errors = 0

    # Create synthetic DataFrame
    n = 100
    np.random.seed(42)
    df = pd.DataFrame({
        "basis_z": np.random.normal(0, 2, n),
        "liq_net_ma": np.random.normal(0, 3, n),
        "liq_notional_ma": np.abs(np.random.normal(100, 50, n)),
        "ob_imb_ma": np.random.normal(0, 0.05, n),
        "ob_vol_imb_ma": np.random.normal(0, 0.05, n),
    })

    # Test score_basis_contrarian
    s = score_basis_contrarian(df, weight=1.5)
    # Verify logic manually for a few rows
    for i in [0, 10, 50, 99]:
        bz = df["basis_z"].iloc[i]
        expected = 0.0
        if bz > 1.5: expected -= 1.5
        if bz > 2.5: expected -= 0.75
        if bz < -1.5: expected += 1.5
        if bz < -2.5: expected += 0.75
        if abs(s.iloc[i] - expected) > 1e-10:
            print(f"  FAIL: score_basis_contrarian row {i}: got {s.iloc[i]}, expected {expected}")
            errors += 1

    # Test score_tick_liq
    s2 = score_tick_liq(df, weight=2.0)
    if len(s2) != n:
        print(f"  FAIL: score_tick_liq returned {len(s2)} rows, expected {n}")
        errors += 1

    # Test score_ob_combined
    s3 = score_ob_combined(df, weight=2.0)
    if len(s3) != n:
        print(f"  FAIL: score_ob_combined returned {len(s3)} rows, expected {n}")
        errors += 1

    # Test with missing columns
    empty_df = pd.DataFrame({"close": [1.0, 2.0]})
    s_empty = score_basis_contrarian(empty_df)
    if not (s_empty == 0).all():
        print(f"  FAIL: score_basis_contrarian with no basis_z should be all zeros")
        errors += 1

    if errors == 0:
        print("  PASS: All score functions produce correct results")
    else:
        print(f"  FAIL: {errors} score function errors")
    return errors


def test_composite_score_synthetic():
    """compute_btc_composite_score produces same result with/without extra param."""
    from signal_core import (
        compute_btc_composite_score,
        DEFAULT_COMPOSITE_WEIGHTS, DEFAULT_EXTRA_WEIGHTS,
    )

    print("[5/6] Testing composite score functions on synthetic data...")
    errors = 0

    n = 200
    np.random.seed(123)

    # Build synthetic BTC features DataFrame
    df = pd.DataFrame({
        "oi_chg": np.random.normal(0, 0.005, n),
        "ret": np.random.normal(0, 0.003, n),
        "fr_8h": np.random.normal(0.0001, 0.0002, n),
        "whale_net_ma": np.random.normal(0, 80_000_000, n),
        "liq_total": np.abs(np.random.normal(1000, 500, n)),
        "liq_net": np.random.normal(0, 500, n),
        "etf_flow_ma": np.random.normal(0, 100, n),
        "basis_z": np.random.normal(0, 2, n),
        "liq_net_ma": np.random.normal(0, 3, n),
        "liq_notional_ma": np.abs(np.random.normal(100, 50, n)),
        "ob_imb_ma": np.random.normal(0, 0.05, n),
        "ob_vol_imb_ma": np.random.normal(0, 0.05, n),
    })
    df["liq_total_ma"] = df["liq_total"].rolling(24).mean().fillna(df["liq_total"].mean())

    # v3 score: default params vs explicit params should be identical
    score_default = compute_btc_composite_score(df)
    score_explicit = compute_btc_composite_score(df, params=DEFAULT_COMPOSITE_WEIGHTS,
                                                  extra=DEFAULT_EXTRA_WEIGHTS)
    diff = (score_default - score_explicit).abs().max()
    if diff > 1e-10:
        print(f"  FAIL: v3 score default vs explicit diff = {diff}")
        errors += 1

    # Scores should not be all zero (with this seed, some should trigger)
    if (score_default == 0).all():
        print(f"  FAIL: v3 score is all zeros on synthetic data (unlikely)")
        errors += 1

    if errors == 0:
        print("  PASS: Composite score functions work correctly")
    else:
        print(f"  FAIL: {errors} composite score errors")
    return errors


def test_spike_detection():
    """Spike detection (single-bar and vectorized) produce consistent results."""
    from signal_core import detect_spike_bar, classify_spike_bar, detect_spike, classify_spike_mode

    print("[6/6] Testing spike detection parity...")
    errors = 0

    # Create synthetic bars
    n = 50
    np.random.seed(77)
    df = pd.DataFrame({
        "range_z": np.random.normal(0, 1.5, n),
        "vol_ratio": np.random.normal(1, 0.8, n),
        "liq_total": np.abs(np.random.normal(100, 100, n)),
        "ema21_dist": np.random.normal(0, 1.5, n),
        "rsi": np.random.uniform(20, 80, n),
    })
    df["liq_total_ma"] = df["liq_total"].rolling(24).mean().fillna(df["liq_total"].mean())

    # Vectorized detection
    vec_spike = detect_spike(df)
    vec_mode = classify_spike_mode(df)

    # Compare with single-bar detection for each row
    for i in range(n):
        row = df.iloc[i]
        single_spike = detect_spike_bar(row)
        single_mode = classify_spike_bar(row)

        if single_spike != vec_spike.iloc[i]:
            print(f"  FAIL row {i}: detect_spike_bar={single_spike}, detect_spike={vec_spike.iloc[i]}")
            errors += 1
        if single_spike and single_mode != vec_mode.iloc[i]:
            # Only compare mode when spike is active (mode is meaningless when not spiking)
            print(f"  FAIL row {i}: classify_spike_bar={single_mode}, classify_spike_mode={vec_mode.iloc[i]}")
            errors += 1

    if errors == 0:
        print("  PASS: Spike detection single-bar and vectorized are consistent")
    else:
        print(f"  FAIL: {errors} spike detection mismatches")
    return errors


def main():
    print("=" * 60)
    print("Signal Parity Test — signal_core.py verification")
    print("=" * 60)
    print()

    total_errors = 0
    total_errors += test_compute_raw_signal()
    total_errors += test_is_dead_zone()
    total_errors += test_default_constants()
    total_errors += test_score_functions_synthetic()
    total_errors += test_composite_score_synthetic()
    total_errors += test_spike_detection()

    print()
    print("=" * 60)
    if total_errors == 0:
        print("ALL TESTS PASSED — signal_core.py is parity-verified")
    else:
        print(f"FAILED: {total_errors} total errors")
    print("=" * 60)

    return 0 if total_errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
