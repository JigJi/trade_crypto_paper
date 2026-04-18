"""
Mission 016: Funding Regime × Cascade Quality
==============================================
สมมติฐาน: เมื่อ BTC funding rate อยู่ในสภาวะ extreme (positive หรือ negative มาก)
แสดงว่า market มี overcrowded positions -> liquidation cascade ที่เกิดขึ้นในช่วงนั้น
ควรจะ "แท้" กว่าและให้ผลดีกว่า

Experiments:
  EXP1: Funding rate distribution ณ จุดเปิดเทรด (winning vs losing trades)
  EXP2: Funding regime buckets: extreme_neg / neg / neutral / pos / extreme_pos
  EXP3: Funding "alignment" -- cascade direction สอดคล้องกับ funding overcrowding?
  EXP4: Funding as trade filter for v6 (extreme regime only)
  EXP5: Funding rate momentum (ΔFR) -- funding กำลังเพิ่มหรือลด ณ จุดเทรด?
"""

import sys, json, logging
import numpy as np, pandas as pd
from pathlib import Path
from datetime import datetime, timedelta
from scipy import stats

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import backtest_15m_btc_led_alts as bt
from signal_core import compute_btc_composite_score_v6

logging.basicConfig(level=logging.WARNING)

# ──────────────────────────────────────────────
# V6 Config
# ──────────────────────────────────────────────
V6_CASCADE_MULT = 1.1
V6_LIQ_W = 8.0
V6_TICK_W = 8.0
V6_TICK_THR = 3
V6_SL = 25.0
V6_TP = 20.0
V6_COOLDOWN = 4
V6_THRESHOLD = 3.0

COINS = ["BTCUSDT", "XRPUSDT", "ADAUSDT", "DOTUSDT", "SUIUSDT", "FILUSDT"]
OOS_START = "2025-01-01"
OOS_END = "2026-03-25"

print("=" * 60)
print("Mission 016: Funding Regime × Cascade Quality")
print("=" * 60)

# ──────────────────────────────────────────────
# 1. Load BTC data + v6 score + funding rate
# ──────────────────────────────────────────────
print("\n[1] Loading BTC data...")
btc_ohlcv = bt.fetch_binance_15m("BTCUSDT", years=3)
if "date_time" in btc_ohlcv.columns:
    btc_ohlcv = btc_ohlcv.rename(columns={"date_time": "ts"})

db_data = bt.load_btc_db_data()
btc_df = bt.build_btc_features(btc_ohlcv, db_data)

# v6 score
btc_score = compute_btc_composite_score_v6(
    btc_df, cascade_mult=V6_CASCADE_MULT,
    liq_w=V6_LIQ_W, tick_w=V6_TICK_W, tick_net_thr=V6_TICK_THR)
btc_score_ts = pd.Series(btc_score.values, index=btc_df["ts"].values, name="btc_score")

# Funding rate -- already merged by build_btc_features() into btc_df
# Columns available: fr_8h, fr_ma, last_funding_rate
btc_df["fr_8h"] = btc_df["fr_8h"].fillna(0)
# Funding rate z-score (rolling 7 days ≈ 672 bars of 15m)
btc_df["fr_zscore"] = (
    (btc_df["fr_8h"] - btc_df["fr_8h"].rolling(672).mean()) /
    btc_df["fr_8h"].rolling(672).std().clip(lower=1e-8)
)
# Funding rate change (Δ between current and previous funding period, ~8h = 32 bars)
btc_df["fr_change"] = btc_df["fr_8h"].diff(32)

print(f"  Funding rate range: [{btc_df['fr_8h'].min():.6f}, {btc_df['fr_8h'].max():.6f}]")
print(f"  FR z-score range: [{btc_df['fr_zscore'].min():.2f}, {btc_df['fr_zscore'].max():.2f}]")

# Build funding lookup series indexed by ts
fr_lookup = btc_df.set_index("ts")[["fr_8h", "fr_zscore", "fr_change"]].copy()

# ──────────────────────────────────────────────
# 2. Run v6 backtest on all coins
# ──────────────────────────────────────────────
print("\n[2] Running v6 backtest on 6 coins...")
all_trades = []
for symbol in COINS:
    coin = symbol.replace("USDT", "")
    ohlcv = bt.fetch_binance_15m(symbol, years=3)
    if "date_time" in ohlcv.columns:
        ohlcv = ohlcv.rename(columns={"date_time": "ts"})
    alt_df = bt.build_alt_technicals(ohlcv)
    oos = alt_df[(alt_df["ts"] >= OOS_START) & (alt_df["ts"] <= OOS_END)].copy()

    signals, alt_merged = bt.generate_btc_led_signal(
        btc_score_ts, oos,
        threshold=V6_THRESHOLD, use_alt_pa_filter=False)
    trades = bt.run_backtest(
        alt_merged, signals,
        sl_atr_mult=V6_SL, tp_atr_mult=V6_TP,
        cooldown_bars=V6_COOLDOWN, max_hold_bars=96)
    if len(trades) > 0:
        trades["coin"] = coin
        all_trades.append(trades)
    print(f"  {coin}: {len(trades)} trades, PnL ${trades['pnl_net'].sum():.0f}")

trades_df = pd.concat(all_trades, ignore_index=True)
trades_df["entry_time"] = pd.to_datetime(trades_df["entry_time"])
trades_df["exit_time"] = pd.to_datetime(trades_df["exit_time"])
trades_df["win"] = trades_df["pnl_net"] > 0

# Merge funding rate at entry time
trades_df = pd.merge_asof(
    trades_df.sort_values("entry_time"),
    fr_lookup.reset_index().rename(columns={"ts": "entry_time"}).sort_values("entry_time"),
    on="entry_time", direction="backward")

baseline_pnl = trades_df["pnl_net"].sum()
baseline_wr = trades_df["win"].mean() * 100
print(f"\n  BASELINE: {len(trades_df)} trades, WR {baseline_wr:.1f}%, PnL ${baseline_pnl:.0f}")

# ──────────────────────────────────────────────
# EXP1: Winning vs Losing trades -- funding rate distribution
# ──────────────────────────────────────────────
print("\n" + "=" * 60)
print("EXP1: Funding Rate at Entry -- Winners vs Losers")
print("=" * 60)

winners = trades_df[trades_df["win"]]
losers = trades_df[~trades_df["win"]]

exp1 = {}
for label, col in [("fr_raw", "fr_8h"), ("fr_zscore", "fr_zscore"), ("fr_change", "fr_change")]:
    w_vals = winners[col].dropna()
    l_vals = losers[col].dropna()
    t_stat, p_val = stats.ttest_ind(w_vals, l_vals, equal_var=False)
    exp1[label] = {
        "win_mean": round(float(w_vals.mean()), 8),
        "win_std": round(float(w_vals.std()), 8),
        "lose_mean": round(float(l_vals.mean()), 8),
        "lose_std": round(float(l_vals.std()), 8),
        "t_stat": round(float(t_stat), 3),
        "p_value": round(float(p_val), 4),
        "significant": bool(p_val < 0.05),
    }
    sig_mark = "***" if p_val < 0.001 else "**" if p_val < 0.01 else "*" if p_val < 0.05 else ""
    print(f"  {label}: Win={w_vals.mean():.6f} vs Lose={l_vals.mean():.6f}  t={t_stat:.2f} p={p_val:.4f} {sig_mark}")

# ──────────────────────────────────────────────
# EXP2: Funding regime buckets
# ──────────────────────────────────────────────
print("\n" + "=" * 60)
print("EXP2: Performance by Funding Regime Bucket")
print("=" * 60)

# Define regimes based on z-score
def classify_fr_regime(z):
    if z <= -1.5:
        return "extreme_neg"
    elif z <= -0.5:
        return "neg"
    elif z <= 0.5:
        return "neutral"
    elif z <= 1.5:
        return "pos"
    else:
        return "extreme_pos"

trades_df["fr_regime"] = trades_df["fr_zscore"].apply(classify_fr_regime)

exp2 = {}
regime_order = ["extreme_neg", "neg", "neutral", "pos", "extreme_pos"]
for regime in regime_order:
    subset = trades_df[trades_df["fr_regime"] == regime]
    if len(subset) < 5:
        exp2[regime] = {"trades": len(subset), "note": "too few trades"}
        continue
    wr = subset["win"].mean() * 100
    pnl = subset["pnl_net"].sum()
    avg_pnl = subset["pnl_net"].mean()
    # Direction breakdown
    shorts = subset[subset["dir"] == "S"]
    longs = subset[subset["dir"] == "L"]
    exp2[regime] = {
        "trades": int(len(subset)),
        "wr": round(wr, 1),
        "pnl": round(pnl, 0),
        "avg_pnl": round(float(avg_pnl), 2),
        "n_short": int(len(shorts)),
        "n_long": int(len(longs)),
        "short_wr": round(shorts["win"].mean() * 100, 1) if len(shorts) > 0 else 0,
        "long_wr": round(longs["win"].mean() * 100, 1) if len(longs) > 0 else 0,
    }
    print(f"  {regime:>12s}: {len(subset):4d} trades, WR {wr:5.1f}%, PnL ${pnl:+8.0f}, avg ${avg_pnl:+.2f}/trade")

# ──────────────────────────────────────────────
# EXP3: Funding "alignment" with trade direction
# ──────────────────────────────────────────────
print("\n" + "=" * 60)
print("EXP3: Funding Alignment -- Does FR predict cascade direction?")
print("=" * 60)

# Concept: If FR > 0 (longs pay shorts = longs overcrowded),
# a SHORT cascade should be more genuine (longs getting liquidated)
# Aligned = (FR>0 & SHORT) or (FR<0 & LONG)
trades_df["fr_aligned"] = (
    ((trades_df["fr_8h"] > 0) & (trades_df["dir"] == "S")) |
    ((trades_df["fr_8h"] < 0) & (trades_df["dir"] == "L"))
)

aligned = trades_df[trades_df["fr_aligned"]]
unaligned = trades_df[~trades_df["fr_aligned"]]

exp3 = {}
for label, subset in [("aligned", aligned), ("unaligned", unaligned)]:
    if len(subset) < 5:
        exp3[label] = {"trades": len(subset), "note": "too few"}
        continue
    wr = subset["win"].mean() * 100
    pnl = subset["pnl_net"].sum()
    avg = subset["pnl_net"].mean()
    exp3[label] = {
        "trades": int(len(subset)),
        "wr": round(wr, 1),
        "pnl": round(pnl, 0),
        "avg_pnl": round(float(avg), 2),
    }
    print(f"  {label:>10s}: {len(subset)} trades, WR {wr:.1f}%, PnL ${pnl:+.0f}, avg ${avg:+.2f}")

# Statistical test
if len(aligned) >= 10 and len(unaligned) >= 10:
    t, p = stats.ttest_ind(aligned["pnl_net"], unaligned["pnl_net"], equal_var=False)
    exp3["t_stat"] = round(float(t), 3)
    exp3["p_value"] = round(float(p), 4)
    print(f"  t={t:.3f}, p={p:.4f} {'***' if p<0.001 else '**' if p<0.01 else '*' if p<0.05 else 'n.s.'}")

# Also test with |FR| > median for "strong" alignment
fr_abs_median = trades_df["fr_8h"].abs().median()
strong_aligned = trades_df[trades_df["fr_aligned"] & (trades_df["fr_8h"].abs() > fr_abs_median)]
weak_trades = trades_df[~trades_df["fr_aligned"] | (trades_df["fr_8h"].abs() <= fr_abs_median)]
exp3["strong_aligned"] = {
    "trades": int(len(strong_aligned)),
    "wr": round(strong_aligned["win"].mean() * 100, 1) if len(strong_aligned) > 0 else 0,
    "pnl": round(strong_aligned["pnl_net"].sum(), 0),
    "avg_pnl": round(float(strong_aligned["pnl_net"].mean()), 2) if len(strong_aligned) > 0 else 0,
}
print(f"  Strong aligned (|FR| > median): {len(strong_aligned)} trades, WR {strong_aligned['win'].mean()*100:.1f}%, avg ${strong_aligned['pnl_net'].mean():+.2f}")

# ──────────────────────────────────────────────
# EXP4: Funding as trade filter for v6
# ──────────────────────────────────────────────
print("\n" + "=" * 60)
print("EXP4: Funding-Based Trade Filters")
print("=" * 60)

exp4 = {}

# Filter 1: Only trade when FR is extreme (|z| > 1.0)
for z_thr in [0.5, 1.0, 1.5, 2.0]:
    filtered = trades_df[trades_df["fr_zscore"].abs() > z_thr]
    if len(filtered) < 10:
        exp4[f"extreme_z{z_thr}"] = {"trades": len(filtered), "note": "too few"}
        continue
    wr = filtered["win"].mean() * 100
    pnl = filtered["pnl_net"].sum()
    avg = filtered["pnl_net"].mean()
    exp4[f"extreme_z{z_thr}"] = {
        "trades": int(len(filtered)),
        "wr": round(wr, 1),
        "pnl": round(pnl, 0),
        "avg_pnl": round(float(avg), 2),
        "pnl_vs_baseline": round(pnl - baseline_pnl, 0),
    }
    print(f"  |FR_z| > {z_thr}: {len(filtered):4d} trades, WR {wr:.1f}%, PnL ${pnl:+.0f} ({pnl-baseline_pnl:+.0f} vs base), avg ${avg:+.2f}")

# Filter 2: Only trade when aligned AND extreme
for z_thr in [0.5, 1.0]:
    filtered = trades_df[trades_df["fr_aligned"] & (trades_df["fr_zscore"].abs() > z_thr)]
    if len(filtered) < 10:
        exp4[f"aligned_extreme_z{z_thr}"] = {"trades": len(filtered), "note": "too few"}
        continue
    wr = filtered["win"].mean() * 100
    pnl = filtered["pnl_net"].sum()
    avg = filtered["pnl_net"].mean()
    exp4[f"aligned_extreme_z{z_thr}"] = {
        "trades": int(len(filtered)),
        "wr": round(wr, 1),
        "pnl": round(pnl, 0),
        "avg_pnl": round(float(avg), 2),
    }
    print(f"  Aligned+|z|>{z_thr}: {len(filtered):4d} trades, WR {wr:.1f}%, PnL ${pnl:+.0f}, avg ${avg:+.2f}")

# Filter 3: Exclude neutral funding regime
filtered = trades_df[trades_df["fr_regime"] != "neutral"]
if len(filtered) >= 10:
    wr = filtered["win"].mean() * 100
    pnl = filtered["pnl_net"].sum()
    avg = filtered["pnl_net"].mean()
    exp4["exclude_neutral"] = {
        "trades": int(len(filtered)),
        "wr": round(wr, 1),
        "pnl": round(pnl, 0),
        "avg_pnl": round(float(avg), 2),
    }
    print(f"  Exclude neutral: {len(filtered):4d} trades, WR {wr:.1f}%, PnL ${pnl:+.0f}, avg ${avg:+.2f}")

# ──────────────────────────────────────────────
# EXP5: Funding rate momentum (ΔFR)
# ──────────────────────────────────────────────
print("\n" + "=" * 60)
print("EXP5: Funding Rate Momentum (Delta FR)")
print("=" * 60)

# Rising FR = more longs being added -> more vulnerable to SHORT cascade
# Falling FR = more shorts being added -> more vulnerable to LONG cascade
trades_df["fr_rising"] = trades_df["fr_change"] > 0
trades_df["fr_momentum_aligned"] = (
    ((trades_df["fr_change"] > 0) & (trades_df["dir"] == "S")) |  # Rising FR -> short = aligned
    ((trades_df["fr_change"] < 0) & (trades_df["dir"] == "L"))     # Falling FR -> long = aligned
)

exp5 = {}
for label, mask in [
    ("fr_rising", trades_df["fr_rising"]),
    ("fr_falling", ~trades_df["fr_rising"]),
    ("momentum_aligned", trades_df["fr_momentum_aligned"]),
    ("momentum_unaligned", ~trades_df["fr_momentum_aligned"]),
]:
    subset = trades_df[mask].dropna(subset=["fr_change"])
    if len(subset) < 10:
        exp5[label] = {"trades": len(subset), "note": "too few"}
        continue
    wr = subset["win"].mean() * 100
    pnl = subset["pnl_net"].sum()
    avg = subset["pnl_net"].mean()
    exp5[label] = {
        "trades": int(len(subset)),
        "wr": round(wr, 1),
        "pnl": round(pnl, 0),
        "avg_pnl": round(float(avg), 2),
    }
    print(f"  {label:>22s}: {len(subset):4d} trades, WR {wr:.1f}%, PnL ${pnl:+.0f}, avg ${avg:+.2f}")

# Combined: momentum + level alignment
combo = trades_df[trades_df["fr_aligned"] & trades_df["fr_momentum_aligned"]].dropna(subset=["fr_change"])
if len(combo) >= 10:
    wr = combo["win"].mean() * 100
    pnl = combo["pnl_net"].sum()
    avg = combo["pnl_net"].mean()
    exp5["double_aligned"] = {
        "trades": int(len(combo)),
        "wr": round(wr, 1),
        "pnl": round(pnl, 0),
        "avg_pnl": round(float(avg), 2),
    }
    print(f"  {'double_aligned':>22s}: {len(combo):4d} trades, WR {wr:.1f}%, PnL ${pnl:+.0f}, avg ${avg:+.2f}")

# ──────────────────────────────────────────────
# Summary
# ──────────────────────────────────────────────
print("\n" + "=" * 60)
print("SUMMARY")
print("=" * 60)

# Find best filter
best_filter = None
best_avg = baseline_pnl / len(trades_df)
for k, v in {**exp4, **exp5}.items():
    if isinstance(v, dict) and "avg_pnl" in v and v.get("trades", 0) >= 30:
        if v["avg_pnl"] > best_avg:
            best_avg = v["avg_pnl"]
            best_filter = k

print(f"  Baseline: {len(trades_df)} trades, avg ${baseline_pnl/len(trades_df):.2f}/trade")
if best_filter:
    bv = ({**exp4, **exp5})[best_filter]
    print(f"  Best filter: {best_filter} -> {bv['trades']} trades, avg ${bv['avg_pnl']:.2f}/trade (+${bv['avg_pnl'] - baseline_pnl/len(trades_df):.2f})")
else:
    print(f"  No filter beats baseline with sufficient trades (>=30)")

# ──────────────────────────────────────────────
# Save results
# ──────────────────────────────────────────────
results = {
    "mission_id": "mission_016_funding_regime_cascade",
    "date": "2026-03-25",
    "baseline": {
        "trades": int(len(trades_df)),
        "wr": round(baseline_wr, 1),
        "pnl": round(baseline_pnl, 0),
        "avg_pnl": round(baseline_pnl / len(trades_df), 2),
    },
    "exp1_winner_vs_loser": exp1,
    "exp2_regime_buckets": exp2,
    "exp3_alignment": exp3,
    "exp4_filters": exp4,
    "exp5_momentum": exp5,
    "best_filter": best_filter,
    "verdict": "TBD",  # will be set after analysis
}

# Determine verdict
any_significant = any(
    v.get("significant", False) for v in exp1.values() if isinstance(v, dict)
)
alignment_p = exp3.get("p_value", 1.0)
has_filter_edge = best_filter is not None

if any_significant or alignment_p < 0.05 or has_filter_edge:
    results["verdict"] = "significant"
else:
    results["verdict"] = "not_significant"

# Save JSON
out_json = BASE_DIR / "missions" / "mission_016_funding_regime_cascade.json"
with open(out_json, "w", encoding="utf-8") as f:
    json.dump(results, f, indent=2, ensure_ascii=False, default=str)
print(f"\n  Saved: {out_json}")

# ──────────────────────────────────────────────
# Save missions.json
# ──────────────────────────────────────────────
from research.missions import MissionEngine, _get_level

insight_parts = []
if any_significant:
    sig_factors = [k for k, v in exp1.items() if isinstance(v, dict) and v.get("significant")]
    insight_parts.append(f"Funding rate มี signal ({', '.join(sig_factors)})")
if alignment_p < 0.05:
    insight_parts.append(f"FR alignment significant (p={alignment_p:.4f})")
if has_filter_edge:
    bv = ({**exp4, **exp5})[best_filter]
    insight_parts.append(f"Best filter: {best_filter} -> avg ${bv['avg_pnl']:.2f} vs baseline ${baseline_pnl/len(trades_df):.2f}")
if not insight_parts:
    insight_parts.append("Funding rate ไม่ช่วยคัดกรอง cascade ใน v6 -- cascade quality ไม่ขึ้นกับ funding regime")

engine = MissionEngine()
mission_entry = {
    "mission_id": "mission_016_funding_regime_cascade",
    "date": "2026-03-25",
    "type": "cross_factor_analysis",
    "title": "Funding Regime × Cascade Quality -- Does FR Predict Cascade Outcomes?",
    "description": "ทดสอบว่า funding rate extreme ช่วยคัดกรองคุณภาพ cascade ใน v6 ได้หรือไม่ (5 experiments)",
    "difficulty": "hard",
    "xp_reward": 100,
    "status": "completed",
    "target": "funding_regime_cascade",
    "started_at": datetime.utcnow().isoformat(),
    "finished_at": datetime.utcnow().isoformat(),
    "result": {
        "success": True,
        "verdict": results["verdict"],
        "baseline_trades": int(len(trades_df)),
        "baseline_wr": round(baseline_wr, 1),
        "baseline_pnl": round(baseline_pnl, 0),
        "best_filter": best_filter,
        "any_significant": any_significant,
        "alignment_p": alignment_p if alignment_p < 1.0 else None,
    },
    "insight": " | ".join(insight_parts),
    "tags": ["cross_factor", "funding_rate", "cascade_quality", "v6", "filter"],
}
engine._data["missions"].append(mission_entry)
engine._data["meta"]["total_xp"] += 100
engine._data["meta"]["current_streak"] += 1
engine._data["meta"]["longest_streak"] = max(
    engine._data["meta"]["longest_streak"],
    engine._data["meta"]["current_streak"])
engine._data["meta"]["last_mission_date"] = "2026-03-25"
lvl, _ = _get_level(engine._data["meta"]["total_xp"])
engine._data["meta"]["level"] = lvl
engine._save()
print(f"  missions.json updated: XP={engine._data['meta']['total_xp']}, streak={engine._data['meta']['current_streak']}")

print(f"\n  VERDICT: {results['verdict'].upper()}")
print(f"  Insight: {' | '.join(insight_parts)}")
print("\nDone!")
