"""
Mission #003: BTC Composite Score -- Signal Strength vs Trade Outcome
=====================================================================
สมมติฐาน: เทรดที่เข้าตอน BTC composite score สูง (extreme) จะได้ผลดีกว่าเทรดที่เข้าตอน score ต่ำ (borderline)
ถ้าจริง -> สามารถใช้ confidence-based position sizing หรือ skip low-confidence signals ได้

Tests:
1. BTC score distribution at trade entries
2. Bucket trades by score magnitude -> compare WR, avg PnL, Sharpe
3. Long vs Short: does score magnitude predict differently?
4. Score threshold sweep: what's the optimal minimum?
5. Streak analysis: does score predict consecutive wins/losses?
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

# ---- Build BTC composite score ----
log.info("Loading BTC data...")
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

# ---- Run backtest and capture scores ----
coins = ["BTCUSDT", "XRPUSDT", "ADAUSDT", "DOTUSDT", "SUIUSDT", "FILUSDT"]
oos_start, oos_end = "2025-01-01", "2026-03-31"

all_trades = []
for symbol in coins:
    coin = symbol.replace("USDT", "")
    log.info(f"Backtesting {coin}...")
    ohlcv = bt.fetch_binance_15m(symbol, years=3)
    if "date_time" in ohlcv.columns:
        ohlcv = ohlcv.rename(columns={"date_time": "ts"})
    alt_df = bt.build_alt_technicals(ohlcv)
    oos_mask = (alt_df["ts"] >= oos_start) & (alt_df["ts"] <= oos_end)

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
        # Map BTC score at signal bar (1 bar before entry, since signals.shift(1))
        score_lookup = alt_merged.set_index("ts")["btc_score"]
        entry_scores = []
        for _, t in trades.iterrows():
            entry_ts = pd.Timestamp(t["entry_time"])
            # Signal was at entry_ts - 15min (shift(1) in backtest)
            signal_ts = entry_ts - pd.Timedelta("15min")
            # Find closest score
            idx = score_lookup.index.get_indexer([signal_ts], method="nearest")
            if idx[0] >= 0:
                entry_scores.append(score_lookup.iloc[idx[0]])
            else:
                entry_scores.append(np.nan)
        trades["btc_score_at_entry"] = entry_scores
        all_trades.append(trades)

trades_df = pd.concat(all_trades, ignore_index=True)
trades_df["is_win"] = trades_df["pnl_net"] > 0
trades_df["abs_score"] = trades_df["btc_score_at_entry"].abs()
trades_df["dir"] = trades_df["dir"].astype(str)

log.info(f"Total trades: {len(trades_df)}, with scores: {trades_df['btc_score_at_entry'].notna().sum()}")

# ---- Analysis ----
results = {}

# 1. Score distribution stats
score_stats = {
    "mean": float(trades_df["btc_score_at_entry"].mean()),
    "median": float(trades_df["btc_score_at_entry"].median()),
    "std": float(trades_df["btc_score_at_entry"].std()),
    "min": float(trades_df["btc_score_at_entry"].min()),
    "max": float(trades_df["btc_score_at_entry"].max()),
    "abs_mean": float(trades_df["abs_score"].mean()),
    "abs_median": float(trades_df["abs_score"].median()),
}
results["score_distribution"] = score_stats
log.info(f"\n=== Score Distribution ===")
log.info(f"Mean: {score_stats['mean']:.2f}, Median: {score_stats['median']:.2f}, Std: {score_stats['std']:.2f}")
log.info(f"Range: [{score_stats['min']:.2f}, {score_stats['max']:.2f}]")
log.info(f"|Score| Mean: {score_stats['abs_mean']:.2f}, Median: {score_stats['abs_median']:.2f}")

# 2. Bucket by absolute score (custom bins since scores are discrete)
# First, show score value counts
print("\n=== Score Value Counts (|score|) ===")
vc = trades_df["abs_score"].value_counts().sort_index()
for val, cnt in vc.items():
    pct = cnt / len(trades_df) * 100
    print(f"  |score|={val:.1f}: {cnt:>5} trades ({pct:>5.1f}%)")

# Use custom bins based on actual distribution
score_bins = [0, 3.0, 3.5, 4.5, 20]
score_labels = ["Q1_low", "Q2", "Q3", "Q4_high"]
trades_df["score_quartile"] = pd.cut(trades_df["abs_score"], bins=score_bins, labels=score_labels, include_lowest=True)

bucket_results = {}
print("\n=== Score Buckets (by |score| quartiles) ===")
print(f"{'Bucket':<12} {'Range':>20} {'Trades':>7} {'WR%':>7} {'AvgPnL':>10} {'TotalPnL':>10} {'Sharpe':>7}")
print("-" * 80)

for q in ["Q1_low", "Q2", "Q3", "Q4_high"]:
    subset = trades_df[trades_df["score_quartile"] == q]
    wr = subset["is_win"].mean() * 100
    avg_pnl = subset["pnl_net"].mean()
    total_pnl = subset["pnl_net"].sum()
    sharpe = subset["pnl_net"].mean() / subset["pnl_net"].std() * np.sqrt(252 * 4) if subset["pnl_net"].std() > 0 else 0
    score_range = f"[{subset['abs_score'].min():.1f}, {subset['abs_score'].max():.1f}]"

    bucket_results[q] = {
        "trades": int(len(subset)),
        "wr_pct": round(wr, 1),
        "avg_pnl": round(avg_pnl, 2),
        "total_pnl": round(total_pnl, 2),
        "sharpe": round(sharpe, 2),
        "score_range": score_range,
    }
    print(f"{q:<12} {score_range:>20} {len(subset):>7} {wr:>6.1f}% {avg_pnl:>10.2f} {total_pnl:>10.2f} {sharpe:>7.2f}")

results["score_buckets"] = bucket_results

# 3. Long vs Short by score magnitude
print("\n=== Long vs Short by Score Magnitude ===")
for direction in ["L", "S"]:
    dir_trades = trades_df[trades_df["dir"] == direction]
    if len(dir_trades) < 10:
        continue
    dir_trades = dir_trades.copy()
    median_abs = dir_trades["abs_score"].median()
    dir_trades["score_half"] = np.where(dir_trades["abs_score"] <= median_abs, "low", "high")

    dir_label = "LONG" if direction == "L" else "SHORT"
    dir_result = {}
    print(f"\n  {dir_label} trades ({len(dir_trades)}):")
    for half in ["low", "high"]:
        sub = dir_trades[dir_trades["score_half"] == half]
        wr = sub["is_win"].mean() * 100
        avg_pnl = sub["pnl_net"].mean()
        total = sub["pnl_net"].sum()
        dir_result[half] = {"trades": int(len(sub)), "wr_pct": round(wr, 1),
                            "avg_pnl": round(avg_pnl, 2), "total_pnl": round(total, 2)}
        print(f"    {half:>5}: {len(sub):>5} trades | WR {wr:5.1f}% | AvgPnL {avg_pnl:>8.2f} | Total {total:>10.2f}")
    results[f"long_short_{direction}"] = dir_result

# 4. Score threshold sweep
print("\n=== Threshold Sweep (minimum |score| to enter) ===")
print(f"{'MinScore':>10} {'Trades':>7} {'WR%':>7} {'AvgPnL':>10} {'TotalPnL':>10} {'Sharpe':>7}")
print("-" * 60)

sweep_results = {}
for min_score in np.arange(2.5, 8.5, 0.5):
    subset = trades_df[trades_df["abs_score"] >= min_score]
    if len(subset) < 20:
        break
    wr = subset["is_win"].mean() * 100
    avg_pnl = subset["pnl_net"].mean()
    total_pnl = subset["pnl_net"].sum()
    sharpe = subset["pnl_net"].mean() / subset["pnl_net"].std() * np.sqrt(252 * 4) if subset["pnl_net"].std() > 0 else 0

    sweep_results[str(min_score)] = {
        "trades": int(len(subset)),
        "wr_pct": round(wr, 1),
        "avg_pnl": round(avg_pnl, 2),
        "total_pnl": round(total_pnl, 2),
        "sharpe": round(sharpe, 2),
    }
    print(f"{min_score:>10.1f} {len(subset):>7} {wr:>6.1f}% {avg_pnl:>10.2f} {total_pnl:>10.2f} {sharpe:>7.2f}")

results["threshold_sweep"] = sweep_results

# 5. Per-coin analysis: does score magnitude matter differently per coin?
print("\n=== Per-Coin Score Analysis ===")
coin_results = {}
for coin in ["BTC", "XRP", "ADA", "DOT", "SUI", "FIL"]:
    coin_trades = trades_df[trades_df["coin"] == coin]
    if len(coin_trades) < 20:
        continue
    coin_trades = coin_trades.copy()
    median_score = coin_trades["abs_score"].median()
    low = coin_trades[coin_trades["abs_score"] <= median_score]
    high = coin_trades[coin_trades["abs_score"] > median_score]

    low_wr = low["is_win"].mean() * 100 if len(low) > 0 else 0
    high_wr = high["is_win"].mean() * 100 if len(high) > 0 else 0
    low_pnl = low["pnl_net"].mean() if len(low) > 0 else 0
    high_pnl = high["pnl_net"].mean() if len(high) > 0 else 0

    coin_results[coin] = {
        "median_score": round(median_score, 1),
        "low_half": {"trades": int(len(low)), "wr_pct": round(low_wr, 1), "avg_pnl": round(low_pnl, 2)},
        "high_half": {"trades": int(len(high)), "wr_pct": round(high_wr, 1), "avg_pnl": round(high_pnl, 2)},
        "wr_delta": round(high_wr - low_wr, 1),
    }
    marker = "YES" if high_wr > low_wr else "NO"
    print(f"  {coin:>4}: low WR {low_wr:5.1f}% (n={len(low)}) | high WR {high_wr:5.1f}% (n={len(high)}) | delta {high_wr-low_wr:+.1f}pp [{marker}]")

results["per_coin"] = coin_results

# 6. Win/Loss streak analysis -- does score predict streaks?
print("\n=== Win/Loss Streak Analysis ===")
# Sort by entry_time within each coin, then check consecutive outcomes
streak_results = {}
for coin in ["BTC", "XRP", "ADA", "DOT", "SUI", "FIL"]:
    ct = trades_df[trades_df["coin"] == coin].sort_values("entry_time").copy()
    if len(ct) < 20:
        continue
    # Compute streaks
    ct["streak_group"] = (ct["is_win"] != ct["is_win"].shift()).cumsum()
    streaks = ct.groupby("streak_group").agg(
        length=("is_win", "count"),
        is_win=("is_win", "first"),
        avg_score=("abs_score", "mean"),
    )
    win_streaks = streaks[streaks["is_win"]]
    loss_streaks = streaks[~streaks["is_win"]]

    # After a loss, what's the score of next trade and is it a win?
    ct["prev_win"] = ct["is_win"].shift(1)
    after_loss = ct[ct["prev_win"] == False]
    after_win = ct[ct["prev_win"] == True]

    streak_results[coin] = {
        "avg_win_streak": round(win_streaks["length"].mean(), 1),
        "max_win_streak": int(win_streaks["length"].max()),
        "avg_loss_streak": round(loss_streaks["length"].mean(), 1),
        "max_loss_streak": int(loss_streaks["length"].max()),
        "after_loss_wr": round(after_loss["is_win"].mean() * 100, 1) if len(after_loss) > 0 else 0,
        "after_win_wr": round(after_win["is_win"].mean() * 100, 1) if len(after_win) > 0 else 0,
        "after_loss_avg_score": round(after_loss["abs_score"].mean(), 1) if len(after_loss) > 0 else 0,
        "after_win_avg_score": round(after_win["abs_score"].mean(), 1) if len(after_win) > 0 else 0,
    }

# Aggregate streak results
all_after_loss = trades_df.copy().sort_values(["coin", "entry_time"])
all_after_loss["prev_win"] = all_after_loss.groupby("coin")["is_win"].shift(1)
after_loss_all = all_after_loss[all_after_loss["prev_win"] == False]
after_win_all = all_after_loss[all_after_loss["prev_win"] == True]

print(f"  After a LOSS: WR {after_loss_all['is_win'].mean()*100:.1f}% (n={len(after_loss_all)}), avg |score| {after_loss_all['abs_score'].mean():.1f}")
print(f"  After a WIN:  WR {after_win_all['is_win'].mean()*100:.1f}% (n={len(after_win_all)}), avg |score| {after_win_all['abs_score'].mean():.1f}")
streak_results["aggregate"] = {
    "after_loss_wr": round(after_loss_all["is_win"].mean() * 100, 1),
    "after_win_wr": round(after_win_all["is_win"].mean() * 100, 1),
    "after_loss_n": int(len(after_loss_all)),
    "after_win_n": int(len(after_win_all)),
}
results["streak_analysis"] = streak_results

# 7. Signed score analysis (positive = long signal, negative = short signal)
print("\n=== Signed Score Analysis ===")
print(f"{'Score Range':>15} {'Dir':>5} {'Trades':>7} {'WR%':>7} {'AvgPnL':>10} {'TotalPnL':>10}")
print("-" * 60)

signed_results = {}
bins = [(-20, -6), (-6, -4), (-4, -2.5), (2.5, 4), (4, 6), (6, 20)]
for lo, hi in bins:
    subset = trades_df[(trades_df["btc_score_at_entry"] >= lo) & (trades_df["btc_score_at_entry"] < hi)]
    if len(subset) < 10:
        continue
    direction = "SHORT" if lo < 0 else "LONG"
    wr = subset["is_win"].mean() * 100
    avg_pnl = subset["pnl_net"].mean()
    total_pnl = subset["pnl_net"].sum()
    label = f"[{lo}, {hi})"
    signed_results[label] = {
        "direction": direction,
        "trades": int(len(subset)),
        "wr_pct": round(wr, 1),
        "avg_pnl": round(avg_pnl, 2),
        "total_pnl": round(total_pnl, 2),
    }
    print(f"{label:>15} {direction:>5} {len(subset):>7} {wr:>6.1f}% {avg_pnl:>10.2f} {total_pnl:>10.2f}")

results["signed_score_bins"] = signed_results

# 8. Time-weighted score decay: do high-score signals last longer?
print("\n=== Score vs Hold Duration ===")
trades_df["holding_bars"] = pd.to_numeric(trades_df["holding_bars"], errors="coerce")
for q in ["Q1_low", "Q2", "Q3", "Q4_high"]:
    sub = trades_df[trades_df["score_quartile"] == q]
    avg_bars = sub["holding_bars"].mean()
    avg_bars_win = sub[sub["is_win"]]["holding_bars"].mean()
    avg_bars_loss = sub[~sub["is_win"]]["holding_bars"].mean()
    print(f"  {q:<12}: avg hold {avg_bars:.1f} bars (win: {avg_bars_win:.1f}, loss: {avg_bars_loss:.1f})")
    results["score_buckets"][q]["avg_holding_bars"] = round(avg_bars, 1)
    results["score_buckets"][q]["avg_bars_win"] = round(avg_bars_win, 1)
    results["score_buckets"][q]["avg_bars_loss"] = round(avg_bars_loss, 1)

# 9. Exit reason breakdown by score
print("\n=== Exit Reason by Score Quartile ===")
exit_by_score = {}
for q in ["Q1_low", "Q2", "Q3", "Q4_high"]:
    sub = trades_df[trades_df["score_quartile"] == q]
    reasons = sub["exit_reason"].value_counts(normalize=True) * 100
    exit_by_score[q] = {r: round(v, 1) for r, v in reasons.items()}
    reason_str = ", ".join([f"{r}: {v:.0f}%" for r, v in reasons.items()])
    print(f"  {q:<12}: {reason_str}")

results["exit_reason_by_score"] = exit_by_score

# ---- Summary verdict ----
q1_wr = bucket_results["Q1_low"]["wr_pct"]
q4_wr = bucket_results["Q4_high"]["wr_pct"]
wr_delta = q4_wr - q1_wr
q1_pnl = bucket_results["Q1_low"]["avg_pnl"]
q4_pnl = bucket_results["Q4_high"]["avg_pnl"]

if wr_delta > 3:
    verdict = "strong_signal"
    insight_en = f"Strong: Q4 WR {q4_wr}% vs Q1 {q1_wr}% (+{wr_delta:.1f}pp). High scores = better trades."
elif wr_delta > 0:
    verdict = "weak_signal"
    insight_en = f"Weak: Q4 WR {q4_wr}% vs Q1 {q1_wr}% (+{wr_delta:.1f}pp). Score magnitude matters slightly."
else:
    verdict = "no_signal"
    insight_en = f"No signal: Q4 WR {q4_wr}% vs Q1 {q1_wr}% ({wr_delta:+.1f}pp). Score magnitude doesn't predict outcome."

results["verdict"] = verdict
results["insight_en"] = insight_en

print(f"\n{'='*60}")
print(f"VERDICT: {verdict}")
print(f"INSIGHT: {insight_en}")
print(f"{'='*60}")

# ---- Save results ----
mission_data = {
    "mission_id": "mission_003_signal_strength",
    "date": datetime.utcnow().strftime("%Y-%m-%d"),
    "oos_period": f"{oos_start} to {oos_end}",
    "total_trades": int(len(trades_df)),
    "coins": [c.replace("USDT", "") for c in coins],
    "results": results,
}

# Save JSON
json_path = BASE_DIR / "missions" / "mission_003_signal_strength.json"
with open(json_path, "w", encoding="utf-8") as f:
    json.dump(mission_data, f, indent=2, ensure_ascii=False, default=str)
log.info(f"Saved {json_path}")

# Also save to experiments
exp_path = BASE_DIR / "experiments" / "mission_003_signal_strength.json"
with open(exp_path, "w", encoding="utf-8") as f:
    json.dump(mission_data, f, indent=2, ensure_ascii=False, default=str)

print(f"\nSaved: {json_path}")
print(f"Saved: {exp_path}")
print("Done! Now write the .md report and update missions.json.")
