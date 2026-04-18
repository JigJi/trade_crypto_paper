"""
Test Entry Filter: Skip entry when prev bar is too volatile
============================================================
Hypothesis: entries after volatile bars tend to be wrong-direction (flip).
Filter: skip entry if prev_bar_range > X * ATR
"""

import os, sys, warnings
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backtest_15m_btc_led_alts import (
    fetch_binance_15m, load_btc_db_data,
    generate_btc_led_signal, run_backtest, calc_metrics,
    INIT_EQUITY,
)
from signal_core import (
    build_btc_features, build_alt_technicals,
    compute_btc_composite_score,
    DEFAULT_COMPOSITE_WEIGHTS,
)

BKK_UTC_OFFSET = pd.Timedelta("7h")
ALT_COINS = ["BTC", "XRP", "ADA", "DOT", "SUI", "FIL"]

V6_CONFIGS = {
    "BTC": {"threshold": 2.5, "sl": 25.0, "tp": 20.0, "cd": 4},
    "XRP": {"threshold": 3.5, "sl": 25.0, "tp": 20.0, "cd": 4},
    "ADA": {"threshold": 3.5, "sl": 25.0, "tp": 20.0, "cd": 4},
    "DOT": {"threshold": 3.0, "sl": 25.0, "tp": 20.0, "cd": 4},
    "SUI": {"threshold": 3.0, "sl": 25.0, "tp": 20.0, "cd": 4},
    "FIL": {"threshold": 3.0, "sl": 25.0, "tp": 20.0, "cd": 4},
}


def apply_vol_filter(signals, alt_merged, max_range_atr):
    """Zero out signals where previous bar range > max_range_atr * ATR."""
    filtered = signals.copy()
    highs = alt_merged["high"].values
    lows = alt_merged["low"].values
    atrs = alt_merged["atr"].values

    for i in range(1, len(filtered)):
        if filtered.iloc[i] == 0:
            continue
        prev_range = highs[i-1] - lows[i-1]
        atr = atrs[i-1] if not np.isnan(atrs[i-1]) else 1
        if atr > 0 and prev_range / atr > max_range_atr:
            filtered.iloc[i] = 0

    return filtered


def apply_streak_filter(signals, alt_merged, min_streak):
    """Zero out signals where score hasn't been same sign for min_streak bars."""
    filtered = signals.copy()
    scores = alt_merged["btc_score"].values

    for i in range(1, len(filtered)):
        if filtered.iloc[i] == 0:
            continue
        sign = np.sign(scores[i])
        streak = 0
        for j in range(i, max(i - 30, -1), -1):
            if np.sign(scores[j]) == sign:
                streak += 1
            else:
                break
        if streak < min_streak:
            filtered.iloc[i] = 0

    return filtered


def apply_combined_filter(signals, alt_merged, max_range_atr, min_streak):
    """Apply both filters."""
    filtered = signals.copy()
    highs = alt_merged["high"].values
    lows = alt_merged["low"].values
    atrs = alt_merged["atr"].values
    scores = alt_merged["btc_score"].values

    for i in range(1, len(filtered)):
        if filtered.iloc[i] == 0:
            continue

        # Vol filter
        prev_range = highs[i-1] - lows[i-1]
        atr = atrs[i-1] if not np.isnan(atrs[i-1]) else 1
        if atr > 0 and prev_range / atr > max_range_atr:
            filtered.iloc[i] = 0
            continue

        # Streak filter
        sign = np.sign(scores[i])
        streak = 0
        for j in range(i, max(i - 30, -1), -1):
            if np.sign(scores[j]) == sign:
                streak += 1
            else:
                break
        if streak < min_streak:
            filtered.iloc[i] = 0

    return filtered


def run_with_filter(coin, btc_score_ts, btc_period_start, btc_period_end, btc_regime_ts,
                    filter_fn=None, filter_label="none"):
    """Run backtest with optional entry filter."""
    cfg = V6_CONFIGS[coin]
    symbol = f"{coin}USDT"

    ohlcv = fetch_binance_15m(symbol, years=3)
    alt_df = build_alt_technicals(ohlcv)
    alt_df = alt_df[(alt_df["ts"] >= btc_period_start) & (alt_df["ts"] <= btc_period_end)].reset_index(drop=True)

    if len(alt_df) < 200:
        return None

    split_date = pd.Timestamp("2025-12-01")
    alt_test = alt_df[alt_df["ts"] >= split_date].reset_index(drop=True)

    signals, alt_merged = generate_btc_led_signal(
        btc_score_ts, alt_df, cfg["threshold"], False,
        btc_regime_ts=btc_regime_ts, hysteresis_band=3.0,
    )

    # Apply filter
    if filter_fn is not None:
        signals = filter_fn(signals, alt_merged)

    trades = run_backtest(
        alt_merged, signals,
        sl_atr_mult=cfg["sl"], tp_atr_mult=cfg["tp"],
        trail_atr_mult=99, trail_activate_atr=99,
        cooldown_bars=cfg["cd"],
        min_bars_before_flip=0, flip_cd_extra=4,
    )

    m_full = calc_metrics(trades, len(alt_df))

    if not trades.empty:
        trades_oos = trades[pd.to_datetime(trades["entry_time"]) >= split_date]
    else:
        trades_oos = trades

    m_oos = calc_metrics(trades_oos, len(alt_test))

    # Exit breakdown
    exits = {}
    if not trades.empty:
        for reason, grp in trades.groupby("exit_reason"):
            exits[reason] = {
                "count": len(grp), "wr": (grp["pnl_net"] > 0).mean() * 100,
                "pnl": grp["pnl_net"].sum(),
            }

    return {
        "coin": coin, "filter": filter_label,
        "full": m_full, "oos": m_oos, "exits": exits,
        "n_signals": (signals != 0).sum(),
    }


def main():
    print("=" * 70)
    print("ENTRY FILTER TEST: Reduce wrong-direction entries")
    print("=" * 70)

    # Build BTC score
    print("\n[1/2] Building BTC score...")
    btc_ohlcv = fetch_binance_15m("BTCUSDT", years=3)
    btc_db = load_btc_db_data()
    btc_df = build_btc_features(btc_ohlcv, btc_db)
    btc_score = compute_btc_composite_score(btc_df, DEFAULT_COMPOSITE_WEIGHTS)

    btc_period_start = pd.Timestamp("2025-06-01")
    mask = btc_df["ts"] >= btc_period_start
    btc_df_trimmed = btc_df[mask].reset_index(drop=True)
    btc_score_trimmed = btc_score[mask].reset_index(drop=True)
    btc_period_end = btc_df_trimmed["ts"].iloc[-1]
    btc_score_ts = pd.Series(btc_score_trimmed.values, index=btc_df_trimmed["ts"].values)
    btc_regime = btc_df_trimmed["close"] > btc_df_trimmed["ema50"]
    btc_regime_ts = pd.Series(btc_regime.values, index=btc_df_trimmed["ts"].values)

    # Define filters to test
    filters = [
        ("BASELINE (no filter)", None),
        ("vol_filter 1.2x ATR", lambda s, a: apply_vol_filter(s, a, 1.2)),
        ("vol_filter 1.5x ATR", lambda s, a: apply_vol_filter(s, a, 1.5)),
        ("vol_filter 2.0x ATR", lambda s, a: apply_vol_filter(s, a, 2.0)),
        ("streak >= 2", lambda s, a: apply_streak_filter(s, a, 2)),
        ("streak >= 3", lambda s, a: apply_streak_filter(s, a, 3)),
        ("streak >= 4", lambda s, a: apply_streak_filter(s, a, 4)),
        ("combo: vol<1.5 + streak>=2", lambda s, a: apply_combined_filter(s, a, 1.5, 2)),
        ("combo: vol<1.5 + streak>=3", lambda s, a: apply_combined_filter(s, a, 1.5, 3)),
        ("combo: vol<2.0 + streak>=2", lambda s, a: apply_combined_filter(s, a, 2.0, 2)),
        ("combo: vol<2.0 + streak>=3", lambda s, a: apply_combined_filter(s, a, 2.0, 3)),
    ]

    print(f"\n[2/2] Testing {len(filters)} filter variants x {len(ALT_COINS)} coins...")

    # Run all
    results_by_filter = {}
    for label, fn in filters:
        print(f"\n  --- {label} ---")
        results = []
        for coin in ALT_COINS:
            r = run_with_filter(coin, btc_score_ts, btc_period_start, btc_period_end,
                                btc_regime_ts, filter_fn=fn, filter_label=label)
            if r:
                results.append(r)
        results_by_filter[label] = results

    # Print comparison
    print(f"\n{'='*90}")
    print("RESULTS COMPARISON (OOS: Dec 2025 - Mar 2026)")
    print(f"{'='*90}")
    print(f"{'Filter':<35s} {'Trades':>7s} {'WR':>7s} {'PnL':>12s} {'FLIP#':>7s} {'FLIP%':>7s} {'FlipWR':>7s} {'TP#':>5s}")
    print("-" * 90)

    for label, results in results_by_filter.items():
        if not results:
            continue
        total_trades = sum(r["oos"]["total"] for r in results)
        total_pnl = sum(r["oos"]["net_pnl"] for r in results)
        # Weighted WR
        total_wins = sum(r["oos"]["total"] * r["oos"]["win_rate"] / 100 for r in results)
        avg_wr = total_wins / total_trades * 100 if total_trades > 0 else 0

        # Aggregate exits
        total_flip = sum(r["exits"].get("SIGNAL_FLIP", {}).get("count", 0) for r in results)
        total_tp = sum(r["exits"].get("TP", {}).get("count", 0) for r in results)
        total_all = sum(sum(e["count"] for e in r["exits"].values()) for r in results)
        flip_pct = total_flip / total_all * 100 if total_all > 0 else 0

        flip_wins = 0
        flip_total = 0
        for r in results:
            fe = r["exits"].get("SIGNAL_FLIP", {})
            if fe:
                flip_total += fe["count"]
                flip_wins += fe["count"] * fe["wr"] / 100

        flip_wr = flip_wins / flip_total * 100 if flip_total > 0 else 0

        print(f"{label:<35s} {total_trades:>7d} {avg_wr:>6.1f}% ${total_pnl:>+10,.2f} "
              f"{total_flip:>7d} {flip_pct:>6.1f}% {flip_wr:>6.1f}% {total_tp:>5d}")

    # Per-coin detail for best filter
    print(f"\n{'='*90}")
    print("PER-COIN DETAIL: BASELINE vs BEST FILTERS")
    print(f"{'='*90}")

    baseline = results_by_filter.get("BASELINE (no filter)", [])
    for label in ["vol_filter 1.5x ATR", "streak >= 3", "combo: vol<1.5 + streak>=3"]:
        filtered = results_by_filter.get(label, [])
        if not filtered:
            continue

        print(f"\n  --- {label} vs BASELINE ---")
        print(f"  {'Coin':<6s} {'BL_T':>5s} {'BL_WR':>7s} {'BL_PnL':>10s} {'F_T':>5s} {'F_WR':>7s} {'F_PnL':>10s} {'Delta':>10s}")
        print("  " + "-" * 60)

        for i, coin in enumerate(ALT_COINS):
            if i >= len(baseline) or i >= len(filtered):
                continue
            bl = baseline[i]["oos"]
            fl = filtered[i]["oos"]
            delta = fl["net_pnl"] - bl["net_pnl"]
            print(f"  {coin:<6s} {bl['total']:>5d} {bl['win_rate']:>6.1f}% ${bl['net_pnl']:>+8,.2f} "
                  f"{fl['total']:>5d} {fl['win_rate']:>6.1f}% ${fl['net_pnl']:>+8,.2f} ${delta:>+8,.2f}")

    print(f"\n{'='*90}")
    print("Done!")


if __name__ == "__main__":
    main()
