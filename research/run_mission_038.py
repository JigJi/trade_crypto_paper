"""
Mission 038: Signal Age at Entry -- Does Fresh Signal Beat Stale Signal?

สมมติฐาน: สัญญาณที่เพิ่ง fire (signal age ต่ำ) ให้ trade quality ดีกว่า
สัญญาณที่ active มานาน (signal age สูง) → ถ้าจริง เราควรมี max_signal_age filter

ต่อยอดจาก: Mission 011 (alpha decay), Mission 031 (score velocity), Mission 005 (exit mechanism)
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

COINS = ["BTCUSDT", "XRPUSDT", "ADAUSDT", "DOTUSDT", "SUIUSDT", "FILUSDT"]
OOS_START, OOS_END = "2025-01-01", "2026-03-31"

results = {}

# ── Step 1: Build BTC composite score ──
log.info("Loading BTC data and building composite score...")
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

# ── Step 2: Compute signal age for each bar ──
log.info("Computing signal age per bar...")

# Build signal series (same logic as generate_btc_led_signal but we need raw signal per bar)
score_df = btc_score_ts.reset_index()
score_df.columns = ["ts", "btc_score"]
score_df = score_df.sort_values("ts").reset_index(drop=True)

# Classify each bar: +1 (LONG zone), -1 (SHORT zone), 0 (neutral)
threshold = 3.0  # default threshold used by most coins
signal_raw = np.where(score_df["btc_score"] >= threshold, 1,
             np.where(score_df["btc_score"] <= -threshold, -1, 0))
score_df["raw_signal"] = signal_raw

# Compute signal age: how many consecutive bars the current signal has been active
signal_age = np.zeros(len(score_df), dtype=int)
for i in range(1, len(score_df)):
    if signal_raw[i] != 0 and signal_raw[i] == signal_raw[i-1]:
        signal_age[i] = signal_age[i-1] + 1
    elif signal_raw[i] != 0:
        signal_age[i] = 1  # new signal regime starts
    else:
        signal_age[i] = 0  # no signal
score_df["signal_age"] = signal_age

# Stats about signal regimes
oos_mask = (score_df["ts"] >= OOS_START) & (score_df["ts"] <= OOS_END)
oos_scores = score_df[oos_mask]
active_bars = oos_scores[oos_scores["raw_signal"] != 0]
log.info(f"OOS bars: {len(oos_scores)}, Active signal bars: {len(active_bars)} ({100*len(active_bars)/len(oos_scores):.1f}%)")

# Signal regime duration distribution
regime_lengths = []
current_len = 0
current_sig = 0
for _, row in oos_scores.iterrows():
    if row["raw_signal"] != 0 and row["raw_signal"] == current_sig:
        current_len += 1
    else:
        if current_len > 0:
            regime_lengths.append(current_len)
        current_len = 1 if row["raw_signal"] != 0 else 0
        current_sig = row["raw_signal"]
if current_len > 0:
    regime_lengths.append(current_len)

regime_arr = np.array(regime_lengths)
results["regime_duration"] = {
    "count": len(regime_arr),
    "mean": float(np.mean(regime_arr)),
    "median": float(np.median(regime_arr)),
    "p25": float(np.percentile(regime_arr, 25)),
    "p75": float(np.percentile(regime_arr, 75)),
    "p90": float(np.percentile(regime_arr, 90)),
    "max": int(np.max(regime_arr)),
}
log.info(f"Signal regimes: {len(regime_arr)}, Mean duration: {np.mean(regime_arr):.1f} bars, Median: {np.median(regime_arr):.0f}")

# ── Step 3: Run backtest and tag each trade with signal age ──
log.info("Running backtests and tagging trades with signal age...")

# Build signal age lookup: ts -> signal_age
age_lookup = pd.Series(score_df["signal_age"].values, index=score_df["ts"].values)

all_trades = []
for symbol in COINS:
    coin = symbol.replace("USDT", "")
    ohlcv = bt.fetch_binance_15m(symbol, years=3)
    if "date_time" in ohlcv.columns:
        ohlcv = ohlcv.rename(columns={"date_time": "ts"})
    alt_df = bt.build_alt_technicals(ohlcv)
    oos = (alt_df["ts"] >= OOS_START) & (alt_df["ts"] <= OOS_END)

    cfg = COIN_CONFIGS.get(coin, {})
    signals, alt_merged = bt.generate_btc_led_signal(
        btc_score_ts, alt_df[oos],
        threshold=cfg.get("threshold", 3.0),
        use_alt_pa_filter=cfg.get("use_alt_pa_filter", False))
    trades = bt.run_backtest(alt_merged, signals,
                             sl_atr_mult=cfg.get("sl_atr_mult", 2.5),
                             tp_atr_mult=cfg.get("tp_atr_mult", 4.0),
                             cooldown_bars=cfg.get("cooldown_bars", 4))
    if len(trades) > 0:
        trades["coin"] = coin
        # Tag each trade with signal age at entry
        entry_ages = []
        for _, t in trades.iterrows():
            et = t["entry_time"]
            # Find nearest signal age from lookup
            idx = age_lookup.index.searchsorted(pd.Timestamp(et), side="right") - 1
            if 0 <= idx < len(age_lookup):
                entry_ages.append(int(age_lookup.iloc[idx]))
            else:
                entry_ages.append(0)
        trades["signal_age"] = entry_ages
        all_trades.append(trades)

trades_df = pd.concat(all_trades, ignore_index=True)
log.info(f"Total trades: {len(trades_df)}")

# ── EXP1: Signal Age Distribution at Entry ──
log.info("=== EXP1: Signal Age Distribution at Entry ===")
age_stats = trades_df["signal_age"].describe()
log.info(f"Signal age at entry: mean={age_stats['mean']:.1f}, median={age_stats['50%']:.0f}, max={age_stats['max']:.0f}")

results["exp1_age_distribution"] = {
    "mean": float(age_stats["mean"]),
    "median": float(age_stats["50%"]),
    "p25": float(age_stats["25%"]),
    "p75": float(age_stats["75%"]),
    "max": float(age_stats["max"]),
    "std": float(age_stats["std"]),
}

# ── EXP2: Trade Quality by Signal Age Bucket ──
log.info("=== EXP2: Trade Quality by Signal Age Bucket ===")
bins = [0, 1, 4, 8, 16, 32, 999]
labels = ["1 (fresh)", "2-4", "5-8", "9-16", "17-32", "33+"]
trades_df["age_bucket"] = pd.cut(trades_df["signal_age"], bins=bins, labels=labels, right=True)

bucket_results = {}
for bucket in labels:
    subset = trades_df[trades_df["age_bucket"] == bucket]
    if len(subset) < 10:
        continue
    n = len(subset)
    wr = (subset["pnl_net"] > 0).mean() * 100
    avg_pnl = subset["pnl_net"].mean()
    total_pnl = subset["pnl_net"].sum()

    winners = subset[subset["pnl_net"] > 0]["pnl_net"]
    losers = subset[subset["pnl_net"] <= 0]["pnl_net"]
    avg_win = winners.mean() if len(winners) > 0 else 0
    avg_loss = losers.mean() if len(losers) > 0 else 0

    bucket_results[bucket] = {
        "n": int(n),
        "wr": round(wr, 1),
        "avg_pnl": round(avg_pnl, 2),
        "total_pnl": round(total_pnl, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
    }
    log.info(f"  {bucket:>12s}: n={n:4d}, WR={wr:5.1f}%, avg={avg_pnl:+.2f}, total={total_pnl:+.0f}")

results["exp2_age_buckets"] = bucket_results

# ── EXP3: Signal Age by Direction (SHORT vs LONG) ──
log.info("=== EXP3: Signal Age x Direction ===")
dir_age_results = {}
for direction in ["S", "L"]:
    dir_label = "SHORT" if direction == "S" else "LONG"
    dir_trades = trades_df[trades_df["dir"] == direction]
    for bucket in labels:
        subset = dir_trades[dir_trades["age_bucket"] == bucket]
        if len(subset) < 10:
            continue
        n = len(subset)
        wr = (subset["pnl_net"] > 0).mean() * 100
        avg_pnl = subset["pnl_net"].mean()
        key = f"{dir_label}_{bucket}"
        dir_age_results[key] = {"n": int(n), "wr": round(wr, 1), "avg_pnl": round(avg_pnl, 2)}
        log.info(f"  {dir_label} {bucket:>12s}: n={n:4d}, WR={wr:5.1f}%, avg={avg_pnl:+.2f}")

results["exp3_direction_x_age"] = dir_age_results

# ── EXP4: Signal Age by Exit Reason ──
log.info("=== EXP4: Exit Reason by Signal Age ===")
exit_age_results = {}
for bucket in labels:
    subset = trades_df[trades_df["age_bucket"] == bucket]
    if len(subset) < 10:
        continue
    exit_dist = subset["exit_reason"].value_counts(normalize=True) * 100
    exit_age_results[bucket] = {reason: round(pct, 1) for reason, pct in exit_dist.items()}
    log.info(f"  {bucket:>12s}: {dict(exit_dist.round(1))}")

results["exp4_exit_by_age"] = exit_age_results

# ── EXP5: Optimal Max Signal Age Filter ──
log.info("=== EXP5: Max Signal Age Filter Optimization ===")
baseline_pnl = trades_df["pnl_net"].sum()
baseline_n = len(trades_df)
baseline_wr = (trades_df["pnl_net"] > 0).mean() * 100

filter_results = {}
for max_age in [1, 2, 4, 6, 8, 12, 16, 24, 32, 48, 64, 999]:
    filtered = trades_df[trades_df["signal_age"] <= max_age]
    if len(filtered) < 50:
        continue
    n = len(filtered)
    wr = (filtered["pnl_net"] > 0).mean() * 100
    total = filtered["pnl_net"].sum()
    avg = filtered["pnl_net"].mean()
    pnl_per_trade_vs_baseline = avg - (baseline_pnl / baseline_n)

    filter_results[str(max_age)] = {
        "n": int(n),
        "wr": round(wr, 1),
        "total_pnl": round(total, 2),
        "avg_pnl": round(avg, 2),
        "delta_vs_baseline": round(total - baseline_pnl, 2),
        "pct_trades_kept": round(100 * n / baseline_n, 1),
    }
    marker = " <<<" if total > baseline_pnl else ""
    log.info(f"  max_age={max_age:3d}: n={n:4d} ({100*n/baseline_n:4.1f}%), WR={wr:5.1f}%, PnL={total:+.0f} (Δ={total-baseline_pnl:+.0f}){marker}")

results["exp5_max_age_filter"] = filter_results

# ── EXP6: Signal Age vs Bars Held (holding duration) ──
log.info("=== EXP6: Signal Age vs Holding Duration ===")
trades_df["bars_held_int"] = trades_df["holding_bars"].astype(int)
corr_age_bars = trades_df[["signal_age", "bars_held_int"]].corr().iloc[0, 1]
corr_age_pnl = trades_df[["signal_age", "pnl_net"]].corr().iloc[0, 1]
log.info(f"Correlation signal_age vs bars_held: {corr_age_bars:.3f}")
log.info(f"Correlation signal_age vs pnl_net: {corr_age_pnl:.3f}")

results["exp6_correlations"] = {
    "signal_age_vs_bars_held": round(corr_age_bars, 4),
    "signal_age_vs_pnl": round(corr_age_pnl, 4),
}

# ── EXP7: First Entry Only (age=1) vs All ──
log.info("=== EXP7: First Entry Only (age=1) Performance ===")
fresh = trades_df[trades_df["signal_age"] == 1]
stale = trades_df[trades_df["signal_age"] > 8]

for label, subset in [("Fresh (age=1)", fresh), ("Stale (age>8)", stale), ("All", trades_df)]:
    if len(subset) < 10:
        continue
    n = len(subset)
    wr = (subset["pnl_net"] > 0).mean() * 100
    total = subset["pnl_net"].sum()
    avg = subset["pnl_net"].mean()
    log.info(f"  {label:>20s}: n={n:4d}, WR={wr:5.1f}%, PnL={total:+.0f}, avg={avg:+.2f}")

results["exp7_fresh_vs_stale"] = {
    "fresh_n": int(len(fresh)),
    "fresh_wr": round((fresh["pnl_net"] > 0).mean() * 100, 1) if len(fresh) > 0 else 0,
    "fresh_total_pnl": round(fresh["pnl_net"].sum(), 2) if len(fresh) > 0 else 0,
    "fresh_avg_pnl": round(fresh["pnl_net"].mean(), 2) if len(fresh) > 0 else 0,
    "stale_n": int(len(stale)),
    "stale_wr": round((stale["pnl_net"] > 0).mean() * 100, 1) if len(stale) > 0 else 0,
    "stale_total_pnl": round(stale["pnl_net"].sum(), 2) if len(stale) > 0 else 0,
    "stale_avg_pnl": round(stale["pnl_net"].mean(), 2) if len(stale) > 0 else 0,
    "baseline_n": baseline_n,
    "baseline_wr": round(baseline_wr, 1),
    "baseline_total_pnl": round(baseline_pnl, 2),
}

# ── Save Results ──
log.info("Saving results...")
mission_data = {
    "mission_id": "mission_038_signal_age",
    "date": "2026-04-16",
    "type": "signal_quality",
    "title": "Signal Age at Entry -- Does Fresh Signal Beat Stale Signal?",
    "hypothesis": "สัญญาณที่เพิ่ง fire (signal age ต่ำ) ดีกว่าสัญญาณที่ active มานาน",
    "coins": [c.replace("USDT", "") for c in COINS],
    "oos_period": f"{OOS_START} to {OOS_END}",
    "total_trades": baseline_n,
    "baseline_pnl": round(baseline_pnl, 2),
    "baseline_wr": round(baseline_wr, 1),
    "results": results,
}

out_json = BASE_DIR / "missions" / "mission_038_signal_age.json"
with open(out_json, "w", encoding="utf-8") as f:
    json.dump(mission_data, f, indent=2, ensure_ascii=False, default=str)
log.info(f"Saved: {out_json}")

# ── Print Summary ──
print("\n" + "="*70)
print("  MISSION 038: Signal Age at Entry")
print("="*70)
print(f"  Total trades: {baseline_n}")
print(f"  Baseline PnL: ${baseline_pnl:,.0f} | WR: {baseline_wr:.1f}%")
print(f"  Signal regime mean duration: {results['regime_duration']['mean']:.1f} bars")
print(f"  Signal age at entry: mean={results['exp1_age_distribution']['mean']:.1f}, median={results['exp1_age_distribution']['median']:.0f}")
print()
print("  Age Bucket Performance:")
for bucket, data in results["exp2_age_buckets"].items():
    marker = " *" if data["wr"] > baseline_wr + 2 else ""
    print(f"    {bucket:>12s}: n={data['n']:4d}, WR={data['wr']:5.1f}%, PnL=${data['total_pnl']:+,.0f}{marker}")
print()
print(f"  Corr(signal_age, pnl): {results['exp6_correlations']['signal_age_vs_pnl']:.4f}")
print()
if "exp7_fresh_vs_stale" in results:
    r = results["exp7_fresh_vs_stale"]
    print(f"  Fresh (age=1): n={r['fresh_n']}, WR={r['fresh_wr']:.1f}%, PnL=${r['fresh_total_pnl']:+,.0f}")
    print(f"  Stale (age>8): n={r['stale_n']}, WR={r['stale_wr']:.1f}%, PnL=${r['stale_total_pnl']:+,.0f}")
print("="*70)
