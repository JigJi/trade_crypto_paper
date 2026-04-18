"""
Analyze Entry Quality: TP exits vs SIGNAL_FLIP exits
=====================================================
Goal: Find patterns that distinguish good entries from bad entries.
If we can filter bad entries BEFORE entering, SIGNAL_FLIP disappears.
"""

import os, sys, warnings
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backtest_15m_btc_led_alts import (
    fetch_binance_15m, load_btc_db_data,
    generate_btc_led_signal, run_backtest,
    INIT_EQUITY, BUDGET_USDT, LEVERAGE,
)
from signal_core import (
    build_btc_features, build_alt_technicals,
    compute_btc_composite_score,
    DEFAULT_COMPOSITE_WEIGHTS,
)

BKK_UTC_OFFSET = pd.Timedelta("7h")
ALT_COINS = ["BTC", "XRP", "ADA", "DOT", "SUI", "FIL"]

# Use v6 config (champion) with realistic flip settings
V6_CONFIGS = {
    "BTC": {"threshold": 2.5, "sl": 25.0, "tp": 20.0, "cd": 4},
    "XRP": {"threshold": 3.5, "sl": 25.0, "tp": 20.0, "cd": 4},
    "ADA": {"threshold": 3.5, "sl": 25.0, "tp": 20.0, "cd": 4},
    "DOT": {"threshold": 3.0, "sl": 25.0, "tp": 20.0, "cd": 4},
    "SUI": {"threshold": 3.0, "sl": 25.0, "tp": 20.0, "cd": 4},
    "FIL": {"threshold": 3.0, "sl": 25.0, "tp": 20.0, "cd": 4},
}


def analyze_entries(coin, btc_score_ts, btc_df_trimmed, btc_period_start, btc_period_end, btc_regime_ts):
    """Collect entry-level features for each trade, tagged by exit reason."""
    cfg = V6_CONFIGS[coin]
    symbol = f"{coin}USDT"

    ohlcv = fetch_binance_15m(symbol, years=3)
    alt_df = build_alt_technicals(ohlcv)
    alt_df = alt_df[(alt_df["ts"] >= btc_period_start) & (alt_df["ts"] <= btc_period_end)].reset_index(drop=True)

    if len(alt_df) < 200:
        return None

    # Generate signals with v6 hysteresis
    signals, alt_merged = generate_btc_led_signal(
        btc_score_ts, alt_df, cfg["threshold"], False,
        btc_regime_ts=btc_regime_ts,
        hysteresis_band=3.0,
    )

    # Run backtest with realistic flip
    trades = run_backtest(
        alt_merged, signals,
        sl_atr_mult=cfg["sl"], tp_atr_mult=cfg["tp"],
        trail_atr_mult=99, trail_activate_atr=99,
        cooldown_bars=cfg["cd"],
        min_bars_before_flip=0, flip_cd_extra=4,
    )

    if trades.empty:
        return None

    # Enrich trades with entry-time features
    rows = []
    sig_shifted = signals.shift(1).fillna(0).astype(int)
    scores = alt_merged["btc_score"].values
    times = alt_merged["ts"].values
    opens = alt_merged["open"].values
    closes = alt_merged["close"].values
    highs = alt_merged["high"].values
    lows = alt_merged["low"].values
    atrs = alt_merged["atr"].values
    vols = alt_merged["volume"].values if "volume" in alt_merged.columns else np.zeros(len(alt_merged))

    for _, t in trades.iterrows():
        ei = int(t["entry_idx"])
        if ei < 10 or ei >= len(alt_merged):
            continue

        score_at_entry = scores[ei]
        score_prev = scores[ei - 1] if ei > 0 else 0
        score_2ago = scores[ei - 2] if ei > 1 else 0

        # Score features
        score_abs = abs(score_at_entry)
        score_velocity = score_at_entry - score_prev  # how fast score moved
        score_accel = (score_at_entry - score_prev) - (score_prev - score_2ago)
        score_streak = 0  # how many bars score has been same sign
        for j in range(ei, max(ei - 20, -1), -1):
            if np.sign(scores[j]) == np.sign(score_at_entry):
                score_streak += 1
            else:
                break

        # Price action features at entry
        entry_bar_range = (highs[ei] - lows[ei]) / closes[ei] if closes[ei] > 0 else 0
        entry_bar_body = abs(closes[ei] - opens[ei]) / closes[ei] if closes[ei] > 0 else 0
        prev_bar_range = (highs[ei-1] - lows[ei-1]) / closes[ei-1] if ei > 0 and closes[ei-1] > 0 else 0

        # ATR ratio (current bar range vs ATR)
        atr_val = atrs[ei] if not np.isnan(atrs[ei]) else 1
        range_vs_atr = (highs[ei] - lows[ei]) / atr_val if atr_val > 0 else 0

        # Time features
        entry_hour = pd.Timestamp(times[ei]).hour

        # Direction alignment: is price moving with our entry direction?
        direction = int(t["dir"] == "L") * 2 - 1  # 1 for L, -1 for S
        price_move_1bar = (closes[ei] - opens[ei]) / opens[ei] if opens[ei] > 0 else 0
        price_move_3bar = (closes[ei] - closes[max(0, ei-3)]) / closes[max(0, ei-3)] if closes[max(0, ei-3)] > 0 else 0
        alignment_1bar = price_move_1bar * direction  # positive = price moving our way
        alignment_3bar = price_move_3bar * direction

        # Volume features
        vol_now = vols[ei] if not np.isnan(vols[ei]) else 0
        vol_avg = np.nanmean(vols[max(0, ei-20):ei]) if ei > 0 else vol_now
        vol_ratio = vol_now / vol_avg if vol_avg > 0 else 1

        rows.append({
            "coin": coin,
            "exit_reason": t["exit_reason"],
            "pnl_net": t["pnl_net"],
            "holding_bars": t["holding_bars"],
            "direction": t["dir"],
            # Score features
            "score_at_entry": score_at_entry,
            "score_abs": score_abs,
            "score_velocity": score_velocity,
            "score_accel": score_accel,
            "score_streak": score_streak,
            # Price action
            "entry_bar_range": entry_bar_range,
            "entry_bar_body": entry_bar_body,
            "prev_bar_range": prev_bar_range,
            "range_vs_atr": range_vs_atr,
            # Alignment
            "alignment_1bar": alignment_1bar,
            "alignment_3bar": alignment_3bar,
            # Time
            "entry_hour": entry_hour,
            # Volume
            "vol_ratio": vol_ratio,
        })

    return pd.DataFrame(rows)


def main():
    print("=" * 70)
    print("ENTRY QUALITY ANALYSIS: TP vs SIGNAL_FLIP")
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

    # Analyze all coins
    print("\n[2/2] Analyzing entries per coin...")
    all_dfs = []
    for coin in ALT_COINS:
        print(f"  {coin}...", end=" ", flush=True)
        df = analyze_entries(coin, btc_score_ts, btc_df_trimmed, btc_period_start, btc_period_end, btc_regime_ts)
        if df is not None:
            all_dfs.append(df)
            tp = df[df["exit_reason"] == "TP"]
            flip = df[df["exit_reason"] == "SIGNAL_FLIP"]
            print(f"TP={len(tp)} FLIP={len(flip)} SL={len(df[df['exit_reason']=='SL'])} TIMEOUT={len(df[df['exit_reason']=='TIMEOUT'])}")
        else:
            print("SKIP")

    if not all_dfs:
        print("No data!")
        return

    all_trades = pd.concat(all_dfs, ignore_index=True)

    tp_trades = all_trades[all_trades["exit_reason"] == "TP"]
    flip_trades = all_trades[all_trades["exit_reason"] == "SIGNAL_FLIP"]

    print(f"\nTotal: {len(all_trades)} trades | TP: {len(tp_trades)} | FLIP: {len(flip_trades)}")

    # ── Compare features ──
    features = [
        "score_abs", "score_velocity", "score_accel", "score_streak",
        "entry_bar_range", "entry_bar_body", "prev_bar_range", "range_vs_atr",
        "alignment_1bar", "alignment_3bar",
        "entry_hour", "vol_ratio", "holding_bars",
    ]

    print(f"\n{'='*70}")
    print("FEATURE COMPARISON: TP vs SIGNAL_FLIP (mean values)")
    print(f"{'='*70}")
    print(f"{'Feature':<22s} {'TP':>12s} {'FLIP':>12s} {'Diff':>12s} {'Signal':>8s}")
    print("-" * 70)

    significant = []
    for f in features:
        tp_mean = tp_trades[f].mean()
        flip_mean = flip_trades[f].mean()
        diff = tp_mean - flip_mean
        # Effect size (Cohen's d)
        pooled_std = np.sqrt((tp_trades[f].std()**2 + flip_trades[f].std()**2) / 2)
        d = diff / pooled_std if pooled_std > 0 else 0
        sig = "***" if abs(d) > 0.5 else "**" if abs(d) > 0.3 else "*" if abs(d) > 0.2 else ""
        print(f"{f:<22s} {tp_mean:>12.4f} {flip_mean:>12.4f} {diff:>+12.4f} {sig:>8s}")
        if abs(d) > 0.2:
            significant.append((f, d, tp_mean, flip_mean))

    # ── Direction breakdown ──
    print(f"\n{'='*70}")
    print("DIRECTION BREAKDOWN")
    print(f"{'='*70}")
    for d in ["L", "S"]:
        tp_d = tp_trades[tp_trades["direction"] == d]
        flip_d = flip_trades[flip_trades["direction"] == d]
        total_d = all_trades[all_trades["direction"] == d]
        flip_rate = len(flip_d) / len(total_d) * 100 if len(total_d) > 0 else 0
        print(f"  {d}: {len(total_d)} trades | TP={len(tp_d)} FLIP={len(flip_d)} | Flip rate={flip_rate:.1f}%")

    # ── Score magnitude buckets ──
    print(f"\n{'='*70}")
    print("SCORE MAGNITUDE vs FLIP RATE")
    print(f"{'='*70}")
    print(f"{'Score range':<20s} {'Trades':>8s} {'TP':>6s} {'FLIP':>6s} {'FlipRate':>10s} {'AvgPnL':>10s}")
    print("-" * 70)
    bins = [(0, 3), (3, 5), (5, 8), (8, 12), (12, 20), (20, 100)]
    for lo, hi in bins:
        subset = all_trades[(all_trades["score_abs"] >= lo) & (all_trades["score_abs"] < hi)]
        if len(subset) == 0:
            continue
        tp_n = len(subset[subset["exit_reason"] == "TP"])
        flip_n = len(subset[subset["exit_reason"] == "SIGNAL_FLIP"])
        flip_rate = flip_n / len(subset) * 100
        avg_pnl = subset["pnl_net"].mean()
        print(f"  |score| {lo}-{hi:<10d} {len(subset):>8d} {tp_n:>6d} {flip_n:>6d} {flip_rate:>9.1f}% ${avg_pnl:>+9.2f}")

    # ── Score velocity buckets ──
    print(f"\n{'='*70}")
    print("SCORE VELOCITY (how fast score changed) vs FLIP RATE")
    print(f"{'='*70}")
    print(f"{'Velocity':<20s} {'Trades':>8s} {'TP':>6s} {'FLIP':>6s} {'FlipRate':>10s} {'AvgPnL':>10s}")
    print("-" * 70)
    vel_bins = [(-100, -3), (-3, -1), (-1, 0), (0, 1), (1, 3), (3, 100)]
    for lo, hi in vel_bins:
        subset = all_trades[(all_trades["score_velocity"] >= lo) & (all_trades["score_velocity"] < hi)]
        if len(subset) == 0:
            continue
        tp_n = len(subset[subset["exit_reason"] == "TP"])
        flip_n = len(subset[subset["exit_reason"] == "SIGNAL_FLIP"])
        flip_rate = flip_n / len(subset) * 100
        avg_pnl = subset["pnl_net"].mean()
        label = f"  vel {lo:+d} to {hi:+d}"
        print(f"{label:<20s} {len(subset):>8d} {tp_n:>6d} {flip_n:>6d} {flip_rate:>9.1f}% ${avg_pnl:>+9.2f}")

    # ── Score streak ──
    print(f"\n{'='*70}")
    print("SCORE STREAK (bars same sign) vs FLIP RATE")
    print(f"{'='*70}")
    print(f"{'Streak':<20s} {'Trades':>8s} {'TP':>6s} {'FLIP':>6s} {'FlipRate':>10s} {'AvgPnL':>10s}")
    print("-" * 70)
    for lo, hi in [(1, 2), (2, 4), (4, 8), (8, 15), (15, 100)]:
        subset = all_trades[(all_trades["score_streak"] >= lo) & (all_trades["score_streak"] < hi)]
        if len(subset) == 0:
            continue
        tp_n = len(subset[subset["exit_reason"] == "TP"])
        flip_n = len(subset[subset["exit_reason"] == "SIGNAL_FLIP"])
        flip_rate = flip_n / len(subset) * 100
        avg_pnl = subset["pnl_net"].mean()
        print(f"  streak {lo}-{hi:<10d} {len(subset):>8d} {tp_n:>6d} {flip_n:>6d} {flip_rate:>9.1f}% ${avg_pnl:>+9.2f}")

    # ── Alignment ──
    print(f"\n{'='*70}")
    print("PRICE ALIGNMENT AT ENTRY (positive = price moving our way)")
    print(f"{'='*70}")
    print(f"{'Alignment 3bar':<20s} {'Trades':>8s} {'TP':>6s} {'FLIP':>6s} {'FlipRate':>10s} {'AvgPnL':>10s}")
    print("-" * 70)
    align_bins = [(-1, -0.01), (-0.01, -0.003), (-0.003, 0), (0, 0.003), (0.003, 0.01), (0.01, 1)]
    for lo, hi in align_bins:
        subset = all_trades[(all_trades["alignment_3bar"] >= lo) & (all_trades["alignment_3bar"] < hi)]
        if len(subset) == 0:
            continue
        tp_n = len(subset[subset["exit_reason"] == "TP"])
        flip_n = len(subset[subset["exit_reason"] == "SIGNAL_FLIP"])
        flip_rate = flip_n / len(subset) * 100
        avg_pnl = subset["pnl_net"].mean()
        print(f"  align {lo:+.3f}~{hi:+.3f} {len(subset):>8d} {tp_n:>6d} {flip_n:>6d} {flip_rate:>9.1f}% ${avg_pnl:>+9.2f}")

    # ── Hour of day ──
    print(f"\n{'='*70}")
    print("ENTRY HOUR vs FLIP RATE")
    print(f"{'='*70}")
    print(f"{'Hour':<10s} {'Trades':>8s} {'TP':>6s} {'FLIP':>6s} {'FlipRate':>10s} {'AvgPnL':>10s}")
    print("-" * 70)
    for h in range(0, 24, 2):
        subset = all_trades[(all_trades["entry_hour"] >= h) & (all_trades["entry_hour"] < h + 2)]
        if len(subset) < 5:
            continue
        tp_n = len(subset[subset["exit_reason"] == "TP"])
        flip_n = len(subset[subset["exit_reason"] == "SIGNAL_FLIP"])
        flip_rate = flip_n / len(subset) * 100
        avg_pnl = subset["pnl_net"].mean()
        print(f"  {h:02d}-{h+2:02d}    {len(subset):>8d} {tp_n:>6d} {flip_n:>6d} {flip_rate:>9.1f}% ${avg_pnl:>+9.2f}")

    # ── Holding bars distribution ──
    print(f"\n{'='*70}")
    print("HOLDING BARS (how fast the flip happens)")
    print(f"{'='*70}")
    print(f"  TP avg holding:   {tp_trades['holding_bars'].mean():.1f} bars ({tp_trades['holding_bars'].mean()*15:.0f} min)")
    print(f"  FLIP avg holding: {flip_trades['holding_bars'].mean():.1f} bars ({flip_trades['holding_bars'].mean()*15:.0f} min)")
    print(f"\n  FLIP holding distribution:")
    for lo, hi in [(0, 4), (4, 8), (8, 16), (16, 32), (32, 96), (96, 999)]:
        n = len(flip_trades[(flip_trades["holding_bars"] >= lo) & (flip_trades["holding_bars"] < hi)])
        if n > 0:
            pct = n / len(flip_trades) * 100
            print(f"    {lo:3d}-{hi:3d} bars: {n:4d} ({pct:.1f}%)")

    # ── Key findings ──
    print(f"\n{'='*70}")
    print("KEY FINDINGS")
    print(f"{'='*70}")
    if significant:
        print("\nSignificant features (Cohen's d > 0.2):")
        for f, d, tp_m, flip_m in sorted(significant, key=lambda x: abs(x[1]), reverse=True):
            direction = "TP higher" if d > 0 else "FLIP higher"
            print(f"  {f}: d={d:+.3f} ({direction}) | TP={tp_m:.4f} FLIP={flip_m:.4f}")

    # Potential filter: score_abs threshold
    print("\n\nPOTENTIAL FILTER: Score magnitude")
    for min_score in [3, 5, 8, 10, 12]:
        filtered = all_trades[all_trades["score_abs"] >= min_score]
        if len(filtered) < 10:
            continue
        tp_n = len(filtered[filtered["exit_reason"] == "TP"])
        flip_n = len(filtered[filtered["exit_reason"] == "SIGNAL_FLIP"])
        flip_rate = flip_n / len(filtered) * 100
        removed = len(all_trades) - len(filtered)
        pnl = filtered["pnl_net"].sum()
        orig_pnl = all_trades["pnl_net"].sum()
        print(f"  |score| >= {min_score:2d}: {len(filtered):4d} trades (removed {removed}), "
              f"flip_rate={flip_rate:.1f}%, PnL=${pnl:+,.2f} (was ${orig_pnl:+,.2f})")

    print("\nDone!")


if __name__ == "__main__":
    main()
