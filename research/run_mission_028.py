"""
Mission 028: Compound Confidence Score — รวม findings จาก M014, M016, M027 เป็น unified sizing framework

Signals to combine:
1. Vol regime (ATR quartile) — M027: high vol 1-bar WR 91.7%
2. Time of day — M027: 12-15 UTC WR 94.2%
3. OI divergence active — M027: +11.5pp WR for winners
4. FR z-score regime — M016: extreme neg WR 79.5%
5. Price displacement (cascade quality) — M014: monotonic, >0.1% = WR 80%+
6. Direction bias — M027: SHORT > LONG structural edge

Each signal adds confidence points -> tiered sizing
"""

import sys, json, logging
import numpy as np, pandas as pd
from pathlib import Path
from datetime import datetime, timedelta

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import backtest_15m_btc_led_alts as bt
from paper_trading.config import COMPOSITE_WEIGHTS, V3_EXTRA_WEIGHTS, COIN_CONFIGS
from paper_trading.strategy import score_ob_combined, score_basis_contrarian, score_tick_liq

logging.basicConfig(level=logging.WARNING)

# ── Config ──────────────────────────────────────────────────
COINS = ["BTCUSDT", "XRPUSDT", "ADAUSDT", "DOTUSDT", "SUIUSDT", "FILUSDT"]
OOS_START, OOS_END = "2025-01-01", "2026-03-31"


def load_btc_score():
    """Load BTC OHLCV, build features, compute v3 composite score."""
    btc_ohlcv = bt.fetch_binance_15m("BTCUSDT", years=3)
    if "date_time" in btc_ohlcv.columns:
        btc_ohlcv = btc_ohlcv.rename(columns={"date_time": "ts"})
    db_data = bt.load_btc_db_data()
    btc_df = bt.build_btc_features(btc_ohlcv, db_data)

    btc_score = bt.compute_btc_composite_score(btc_df, COMPOSITE_WEIGHTS)
    btc_score = btc_score + score_ob_combined(btc_df, weight=V3_EXTRA_WEIGHTS["ob_combined"])
    btc_score = btc_score + score_basis_contrarian(btc_df, weight=V3_EXTRA_WEIGHTS["basis_contrarian"])
    btc_score = btc_score + score_tick_liq(btc_df, weight=V3_EXTRA_WEIGHTS["tick_liq"])
    btc_score_ts = pd.Series(btc_score.values, index=btc_df["ts"].values, name="btc_score")
    return btc_df, btc_score_ts


def run_baseline_backtest(btc_df, btc_score_ts):
    """Run v3 backtest across all coins, returning trades + context features."""
    all_trades = []
    for symbol in COINS:
        coin = symbol.replace("USDT", "")
        ohlcv = bt.fetch_binance_15m(symbol, years=3)
        if "date_time" in ohlcv.columns:
            ohlcv = ohlcv.rename(columns={"date_time": "ts"})
        alt_df = bt.build_alt_technicals(ohlcv)
        oos_mask = (alt_df["ts"] >= OOS_START) & (alt_df["ts"] <= OOS_END)

        cfg = COIN_CONFIGS.get(coin, {})
        signals, alt_merged = bt.generate_btc_led_signal(
            btc_score_ts, alt_df[oos_mask],
            threshold=cfg.get("threshold", 3.0),
            use_alt_pa_filter=cfg.get("use_alt_pa_filter", False))
        trades = bt.run_backtest(alt_merged, signals,
                                 sl_atr_mult=cfg.get("sl_atr_mult", 2.5),
                                 tp_atr_mult=cfg.get("tp_atr_mult", 4.0),
                                 cooldown_bars=cfg.get("cooldown_bars", 4))
        if len(trades) > 0:
            trades["coin"] = coin
            # Attach entry-time context features from alt_merged
            entry_atr = alt_merged.set_index(alt_merged.index)["atr"]
            trades["entry_atr"] = trades["entry_idx"].map(
                lambda idx: alt_merged.loc[idx, "atr"] if idx in alt_merged.index else np.nan)
            trades["entry_btc_score"] = trades["entry_idx"].map(
                lambda idx: alt_merged.loc[idx, "btc_score"] if idx in alt_merged.index else np.nan)
            all_trades.append(trades)

    trades_df = pd.concat(all_trades, ignore_index=True)
    return trades_df


def enrich_with_btc_features(trades_df, btc_df):
    """Attach BTC-level features at each trade's entry_time."""
    btc_indexed = btc_df.set_index("ts")

    # Create lookup by merging on entry_time
    entry_times = pd.to_datetime(trades_df["entry_time"])
    trades_df["entry_hour"] = entry_times.dt.hour

    # Map BTC features by entry_time (nearest 15m bar)
    features_to_grab = ["atr", "fr_8h", "fr_ma", "oi_chg", "liq_total", "liq_total_ma", "ret"]
    for feat in features_to_grab:
        if feat in btc_indexed.columns:
            feat_series = btc_indexed[feat]
            trades_df[f"btc_{feat}"] = entry_times.map(
                lambda t: feat_series.get(t, np.nan) if t in feat_series.index else np.nan)

    return trades_df


def compute_confidence_signals(trades_df):
    """Compute individual confidence signals at each trade entry."""

    # 1. Vol regime (ATR quartile)
    atr_vals = trades_df["btc_atr"].dropna()
    q25, q50, q75 = atr_vals.quantile([0.25, 0.5, 0.75])
    trades_df["vol_quartile"] = pd.cut(trades_df["btc_atr"],
                                        bins=[-np.inf, q25, q50, q75, np.inf],
                                        labels=["Q1_low", "Q2_medlow", "Q3_medhigh", "Q4_high"])
    trades_df["sig_high_vol"] = (trades_df["btc_atr"] >= q75).astype(int)

    # 2. Time of day — EU/US overlap sweet spot (12-15 UTC)
    trades_df["sig_sweet_hour"] = trades_df["entry_hour"].between(12, 15).astype(int)

    # 3. OI divergence active (|oi_chg| > threshold)
    oi_chg = trades_df["btc_oi_chg"].abs()
    oi_thresh = oi_chg.quantile(0.75)
    trades_df["sig_oi_active"] = (oi_chg >= oi_thresh).astype(int)

    # 4. FR z-score regime
    fr = trades_df["btc_fr_8h"]
    fr_mean = fr.mean()
    fr_std = fr.std()
    trades_df["fr_z"] = (fr - fr_mean) / fr_std
    trades_df["sig_fr_extreme_neg"] = (trades_df["fr_z"] < -1.5).astype(int)
    trades_df["sig_fr_extreme_pos"] = (trades_df["fr_z"] > 1.5).astype(int)

    # 5. Price displacement (cascade quality proxy)
    disp = trades_df["btc_ret"].abs()
    trades_df["sig_displacement_high"] = (disp >= 0.001).astype(int)  # 0.1%+
    trades_df["sig_displacement_extreme"] = (disp >= 0.003).astype(int)  # 0.3%+

    # 6. Direction bias
    trades_df["sig_is_short"] = (trades_df["dir"] == "S").astype(int)

    # 7. Score magnitude
    score_abs = trades_df["entry_btc_score"].abs()
    score_q75 = score_abs.quantile(0.75)
    trades_df["sig_high_score"] = (score_abs >= score_q75).astype(int)

    return trades_df


def compute_compound_score(trades_df):
    """Combine signals into compound confidence score."""
    # Weight each signal by its empirical WR lift from past missions
    signal_weights = {
        "sig_high_vol":            2,   # M027: +13pp WR
        "sig_sweet_hour":          2,   # M027: +8pp WR
        "sig_oi_active":           1,   # M027: +11.5pp WR
        "sig_fr_extreme_neg":      1,   # M016: +4pp WR
        "sig_displacement_high":   2,   # M014: +10pp WR (monotonic)
        "sig_displacement_extreme":1,   # M014: bonus for 0.3%+
        "sig_is_short":            1,   # M027: SHORT > LONG
        "sig_high_score":          1,   # M027: magnitude matters
    }
    # Penalty for bad conditions
    penalty_weights = {
        "sig_fr_extreme_pos":     -2,   # M016: worst regime
    }

    trades_df["confidence_score"] = 0
    for sig, w in signal_weights.items():
        trades_df["confidence_score"] += trades_df[sig] * w
    for sig, w in penalty_weights.items():
        trades_df["confidence_score"] += trades_df[sig] * w

    return trades_df


def apply_tiered_sizing(trades_df, tiers):
    """Apply sizing multipliers based on confidence score tiers.
    tiers: dict of {min_score: multiplier}
    """
    sorted_tiers = sorted(tiers.items(), reverse=True)
    multipliers = []
    for _, row in trades_df.iterrows():
        score = row["confidence_score"]
        mult = 1.0
        for min_score, m in sorted_tiers:
            if score >= min_score:
                mult = m
                break
        multipliers.append(mult)
    trades_df["size_mult"] = multipliers
    trades_df["pnl_sized"] = trades_df["pnl_net"] * trades_df["size_mult"]
    return trades_df


def analyze_results(trades_df, label=""):
    """Compute summary stats."""
    total_pnl = trades_df["pnl_net"].sum()
    sized_pnl = trades_df["pnl_sized"].sum()
    total_trades = len(trades_df)
    wr = (trades_df["pnl_net"] > 0).mean() * 100

    return {
        "label": label,
        "trades": total_trades,
        "wr_pct": round(wr, 1),
        "baseline_pnl": round(total_pnl, 0),
        "sized_pnl": round(sized_pnl, 0),
        "delta_pnl": round(sized_pnl - total_pnl, 0),
        "delta_pct": round((sized_pnl - total_pnl) / abs(total_pnl) * 100, 1) if total_pnl != 0 else 0,
    }


def main():
    print("=" * 60)
    print("Mission 028: Compound Confidence Score")
    print("=" * 60)

    # ── Load data ─────────────────────────────────────────
    print("\n[1/6] Loading BTC score...")
    btc_df, btc_score_ts = load_btc_score()

    print("[2/6] Running baseline backtest (6 coins)...")
    trades_df = run_baseline_backtest(btc_df, btc_score_ts)
    print(f"  Total trades: {len(trades_df)}")
    print(f"  Baseline PnL: ${trades_df['pnl_net'].sum():.0f}")

    print("[3/6] Enriching with BTC features...")
    trades_df = enrich_with_btc_features(trades_df, btc_df)

    print("[4/6] Computing confidence signals...")
    trades_df = compute_confidence_signals(trades_df)
    trades_df = compute_compound_score(trades_df)

    # ── EXP1: Individual signal analysis ──────────────────
    print("\n" + "=" * 60)
    print("EXP1: Individual Signal Performance")
    print("=" * 60)

    signals_to_test = [
        "sig_high_vol", "sig_sweet_hour", "sig_oi_active",
        "sig_fr_extreme_neg", "sig_displacement_high",
        "sig_displacement_extreme", "sig_is_short", "sig_high_score",
    ]
    exp1_results = {}
    for sig in signals_to_test:
        mask = trades_df[sig] == 1
        if mask.sum() < 10:
            continue
        active = trades_df[mask]
        inactive = trades_df[~mask]
        active_wr = (active["pnl_net"] > 0).mean() * 100
        inactive_wr = (inactive["pnl_net"] > 0).mean() * 100
        active_avg = active["pnl_net"].mean()
        inactive_avg = inactive["pnl_net"].mean()
        exp1_results[sig] = {
            "active_count": int(mask.sum()),
            "active_pct": round(mask.mean() * 100, 1),
            "active_wr": round(active_wr, 1),
            "inactive_wr": round(inactive_wr, 1),
            "wr_lift": round(active_wr - inactive_wr, 1),
            "active_avg_pnl": round(active_avg, 2),
            "inactive_avg_pnl": round(inactive_avg, 2),
        }
        print(f"  {sig:30s} | active={mask.sum():5d} ({mask.mean()*100:4.1f}%) "
              f"| WR {active_wr:5.1f}% vs {inactive_wr:5.1f}% (D{active_wr-inactive_wr:+.1f}pp) "
              f"| avg ${active_avg:+.2f} vs ${inactive_avg:+.2f}")

    # ── EXP2: Compound score distribution ─────────────────
    print("\n" + "=" * 60)
    print("EXP2: Compound Score Distribution")
    print("=" * 60)

    score_range = trades_df["confidence_score"].agg(["min", "max", "mean", "median", "std"])
    print(f"  Score range: {score_range['min']:.0f} to {score_range['max']:.0f}")
    print(f"  Mean: {score_range['mean']:.1f}, Median: {score_range['median']:.0f}, Std: {score_range['std']:.1f}")

    exp2_results = {}
    for score_val in sorted(trades_df["confidence_score"].unique()):
        mask = trades_df["confidence_score"] == score_val
        if mask.sum() < 5:
            continue
        grp = trades_df[mask]
        wr = (grp["pnl_net"] > 0).mean() * 100
        avg_pnl = grp["pnl_net"].mean()
        total_pnl = grp["pnl_net"].sum()
        exp2_results[int(score_val)] = {
            "count": int(mask.sum()),
            "wr": round(wr, 1),
            "avg_pnl": round(avg_pnl, 2),
            "total_pnl": round(total_pnl, 0),
        }
        bar = "#" * int(wr / 5)
        print(f"  Score {score_val:3.0f} | n={mask.sum():5d} | WR {wr:5.1f}% {bar} | avg ${avg_pnl:+.2f} | total ${total_pnl:+.0f}")

    # ── EXP3: Bucket analysis (low/med/high confidence) ───
    print("\n" + "=" * 60)
    print("EXP3: Confidence Buckets")
    print("=" * 60)

    # Define confidence tiers based on distribution
    p33 = trades_df["confidence_score"].quantile(0.33)
    p67 = trades_df["confidence_score"].quantile(0.67)
    trades_df["conf_bucket"] = pd.cut(trades_df["confidence_score"],
                                       bins=[-np.inf, p33, p67, np.inf],
                                       labels=["low", "medium", "high"])
    exp3_results = {}
    for bucket in ["low", "medium", "high"]:
        mask = trades_df["conf_bucket"] == bucket
        grp = trades_df[mask]
        wr = (grp["pnl_net"] > 0).mean() * 100
        avg_pnl = grp["pnl_net"].mean()
        total_pnl = grp["pnl_net"].sum()
        exp3_results[bucket] = {
            "count": int(mask.sum()),
            "wr": round(wr, 1),
            "avg_pnl": round(avg_pnl, 2),
            "total_pnl": round(total_pnl, 0),
        }
        print(f"  {bucket:8s} | n={mask.sum():5d} | WR {wr:5.1f}% | avg ${avg_pnl:+.2f} | total ${total_pnl:+.0f}")

    # ── EXP4: Tiered sizing strategies ────────────────────
    print("\n" + "=" * 60)
    print("EXP4: Tiered Sizing Strategies")
    print("=" * 60)

    sizing_strategies = {
        "conservative": {7: 1.5, 4: 1.2, 0: 1.0, -99: 0.8},
        "aggressive":   {7: 2.0, 4: 1.5, 2: 1.0, 0: 0.75, -99: 0.5},
        "simple_short":    {3: 1.5, 0: 1.0},   # just SHORT + some signal
        "vol_time_only":   {4: 1.5, 2: 1.0, 0: 0.75},  # vol + time signals only
        "top_vs_bottom":   {5: 2.0, 0: 1.0, -99: 0.5},  # max contrast
    }

    exp4_results = {}
    baseline_pnl = trades_df["pnl_net"].sum()
    print(f"  Baseline PnL: ${baseline_pnl:.0f}")
    print()

    for name, tiers in sizing_strategies.items():
        trades_df = apply_tiered_sizing(trades_df, tiers)
        result = analyze_results(trades_df, name)
        exp4_results[name] = result
        avg_mult = trades_df["size_mult"].mean()
        print(f"  {name:20s} | sized PnL: ${result['sized_pnl']:>8.0f} | "
              f"D${result['delta_pnl']:>+7.0f} ({result['delta_pct']:>+5.1f}%) | "
              f"avg mult: {avg_mult:.2f}x")

    # ── EXP5: Direction-specific sizing ───────────────────
    print("\n" + "=" * 60)
    print("EXP5: Direction-Specific Analysis")
    print("=" * 60)

    for direction in ["L", "S"]:
        dir_mask = trades_df["dir"] == direction
        dir_trades = trades_df[dir_mask]
        dir_label = "LONG" if direction == "L" else "SHORT"

        # Confidence buckets per direction
        for bucket in ["low", "medium", "high"]:
            b_mask = dir_trades["conf_bucket"] == bucket
            grp = dir_trades[b_mask]
            if len(grp) == 0:
                continue
            wr = (grp["pnl_net"] > 0).mean() * 100
            avg_pnl = grp["pnl_net"].mean()
            print(f"  {dir_label:5s} | {bucket:8s} | n={len(grp):5d} | WR {wr:5.1f}% | avg ${avg_pnl:+.2f}")

    # ── EXP6: Walk-forward stability ──────────────────────
    print("\n" + "=" * 60)
    print("EXP6: Walk-Forward Stability (quarterly)")
    print("=" * 60)

    trades_df["entry_quarter"] = pd.to_datetime(trades_df["entry_time"]).dt.to_period("Q")
    exp6_results = {}
    for q in sorted(trades_df["entry_quarter"].unique()):
        q_mask = trades_df["entry_quarter"] == q
        q_trades = trades_df[q_mask]
        if len(q_trades) < 20:
            continue

        # Apply aggressive sizing for stability test
        q_trades = apply_tiered_sizing(q_trades.copy(), sizing_strategies["aggressive"])
        base_pnl = q_trades["pnl_net"].sum()
        sized_pnl = q_trades["pnl_sized"].sum()
        delta = sized_pnl - base_pnl
        exp6_results[str(q)] = {
            "trades": len(q_trades),
            "base_pnl": round(base_pnl, 0),
            "sized_pnl": round(sized_pnl, 0),
            "delta": round(delta, 0),
            "delta_pct": round(delta / abs(base_pnl) * 100, 1) if base_pnl != 0 else 0,
        }
        sign = "+" if delta >= 0 else ""
        print(f"  {str(q):8s} | n={len(q_trades):5d} | base ${base_pnl:>7.0f} | "
              f"sized ${sized_pnl:>7.0f} | D${sign}{delta:.0f} ({sign}{delta/abs(base_pnl)*100:.1f}%)" if base_pnl != 0 else f"  {str(q):8s} | base=0")

    # ── EXP7: Signal correlation ──────────────────────────
    print("\n" + "=" * 60)
    print("EXP7: Signal Correlation Matrix")
    print("=" * 60)

    sig_cols = [s for s in signals_to_test if s in trades_df.columns]
    corr = trades_df[sig_cols].corr()
    exp7_results = {}
    print("  Correlations > 0.15:")
    for i in range(len(sig_cols)):
        for j in range(i + 1, len(sig_cols)):
            r = corr.iloc[i, j]
            if abs(r) > 0.15:
                pair = f"{sig_cols[i]} x {sig_cols[j]}"
                exp7_results[pair] = round(r, 3)
                print(f"    {pair}: {r:.3f}")

    if not exp7_results:
        print("  All pairwise correlations < 0.15 — signals are independent!")
        exp7_results = {"note": "all_independent"}

    # ── Save results ──────────────────────────────────────
    print("\n[5/6] Saving results...")

    mission_data = {
        "mission_id": "mission_028_compound_confidence",
        "date": "2026-04-06",
        "baseline_trades": len(trades_df),
        "baseline_pnl": round(baseline_pnl, 0),
        "exp1_individual_signals": exp1_results,
        "exp2_score_distribution": exp2_results,
        "exp3_confidence_buckets": exp3_results,
        "exp4_sizing_strategies": exp4_results,
        "exp6_walkforward": exp6_results,
        "exp7_correlations": exp7_results,
    }

    json_path = BASE_DIR / "missions" / "mission_028_compound_confidence.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(mission_data, f, ensure_ascii=False, indent=2, default=str)
    print(f"  Saved: {json_path}")

    # ── Summary ───────────────────────────────────────────
    print("\n" + "=" * 60)
    print("MISSION 028 SUMMARY")
    print("=" * 60)

    best_strategy = max(exp4_results.items(), key=lambda x: x[1]["delta_pnl"])
    worst_strategy = min(exp4_results.items(), key=lambda x: x[1]["delta_pnl"])

    print(f"  Baseline: {len(trades_df)} trades, ${baseline_pnl:.0f}")
    print(f"  Best sizing: {best_strategy[0]} -> D${best_strategy[1]['delta_pnl']:+.0f} ({best_strategy[1]['delta_pct']:+.1f}%)")
    print(f"  Worst sizing: {worst_strategy[0]} -> D${worst_strategy[1]['delta_pnl']:+.0f} ({worst_strategy[1]['delta_pct']:+.1f}%)")

    # Check monotonicity of confidence score
    scores_sorted = sorted(exp2_results.items(), key=lambda x: x[0])
    wrs = [v["wr"] for _, v in scores_sorted]
    monotonic = all(wrs[i] <= wrs[i+1] for i in range(len(wrs)-1))
    print(f"  Score-WR monotonic: {'YES' if monotonic else 'NO -- non-monotonic'}")
    if len(wrs) >= 2:
        print(f"  WR range: {min(wrs):.1f}% (lowest score) to {max(wrs):.1f}% (highest score)")

    # Walk-forward consistency
    wf_positive = sum(1 for v in exp6_results.values() if v["delta"] > 0)
    wf_total = len(exp6_results)
    print(f"  Walk-forward: {wf_positive}/{wf_total} quarters positive delta")

    return mission_data


if __name__ == "__main__":
    results = main()
