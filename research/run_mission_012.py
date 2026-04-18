"""
Mission 012: BTC Volatility Regime Classification & Adaptive Trading
=====================================================================
สมมติฐาน:
1. BTC 15m volatility สามารถแบ่งเป็น regime ที่ชัดเจน (Low/Normal/High/Extreme)
2. v3/v5 performance ต่างกันอย่างมีนัยสำคัญในแต่ละ regime
3. สามารถปรับ position sizing ตาม vol regime เพื่อ risk-adjusted return ที่ดีขึ้น

7 Experiments:
1. Vol regime classification (realized vol percentile)
2. v3 trade performance by regime
3. Regime transition analysis (does change predict performance?)
4. Adaptive position sizing (increase/decrease by regime)
5. Vol-based entry filter (only trade in "sweet spot" vol)
6. SHORT vs LONG by regime (interaction with direction bias)
7. Regime duration & predictability (how long do regimes last?)
"""
import sys
import logging
import json
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import backtest_15m_btc_led_alts as bt
from paper_trading.config import (
    COMPOSITE_WEIGHTS, V3_EXTRA_WEIGHTS, COIN_CONFIGS,
    COINS_V3_ORIGINAL,
)
from paper_trading.strategy import score_ob_combined, score_basis_contrarian, score_tick_liq

logging.basicConfig(level=logging.WARNING)
log = logging.getLogger("mission_012")

# ---- Config ----
OOS_START = "2025-01-01"
OOS_END = "2026-03-18"
COINS = ["BTCUSDT", "XRPUSDT", "ADAUSDT", "DOTUSDT", "SUIUSDT", "FILUSDT"]
VOL_LOOKBACK = 96  # 96 bars = 24h rolling realized vol

results = {}

# ==============================================================
# Step 0: Load BTC data + compute v3 score + vol regimes
# ==============================================================
print("=" * 60)
print("Mission 012: BTC Volatility Regime Classification")
print("=" * 60)

print("\n[Step 0] Loading BTC data & computing v3 score...")
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

# Compute realized volatility (rolling 24h log returns std, annualized)
btc_df["log_ret"] = np.log(btc_df["close"] / btc_df["close"].shift(1))
btc_df["realized_vol"] = btc_df["log_ret"].rolling(VOL_LOOKBACK).std() * np.sqrt(4 * 24 * 365)  # annualized
btc_df["atr_pct"] = (btc_df["high"] - btc_df["low"]) / btc_df["close"] * 100  # % range per bar
btc_df["atr_pct_24h"] = btc_df["atr_pct"].rolling(VOL_LOOKBACK).mean()

# OOS filter
oos_mask = (btc_df["ts"] >= OOS_START) & (btc_df["ts"] <= OOS_END)
btc_oos = btc_df[oos_mask].copy()
btc_oos["btc_score"] = btc_score_ts.reindex(btc_oos["ts"].values).values

print(f"  BTC OOS: {len(btc_oos)} bars, {btc_oos['ts'].min()} to {btc_oos['ts'].max()}")

# ==============================================================
# Exp 1: Vol Regime Classification
# ==============================================================
print("\n[Exp 1] Volatility Regime Classification...")
rv = btc_oos["realized_vol"].dropna()
q25, q50, q75, q90 = rv.quantile([0.25, 0.5, 0.75, 0.9]).values

def classify_vol(v):
    if pd.isna(v):
        return "Unknown"
    if v <= q25:
        return "Low"
    elif v <= q75:
        return "Normal"
    elif v <= q90:
        return "High"
    else:
        return "Extreme"

btc_oos["vol_regime"] = btc_oos["realized_vol"].apply(classify_vol)

regime_stats = {}
for regime in ["Low", "Normal", "High", "Extreme"]:
    mask = btc_oos["vol_regime"] == regime
    subset = btc_oos[mask]
    if len(subset) == 0:
        continue
    regime_stats[regime] = {
        "bars": int(mask.sum()),
        "pct_of_time": round(mask.sum() / len(btc_oos) * 100, 1),
        "mean_rv": round(subset["realized_vol"].mean(), 1),
        "mean_atr_pct": round(subset["atr_pct"].mean(), 3),
        "mean_score": round(subset["btc_score"].mean(), 2),
        "score_std": round(subset["btc_score"].std(), 2),
        "pct_score_above_3": round((subset["btc_score"].abs() >= 3).sum() / len(subset) * 100, 1),
    }
    print(f"  {regime}: {regime_stats[regime]['bars']} bars ({regime_stats[regime]['pct_of_time']}%), "
          f"RV={regime_stats[regime]['mean_rv']}%, "
          f"Avg|Score|={round(subset['btc_score'].abs().mean(), 2)}, "
          f"Signal rate={regime_stats[regime]['pct_score_above_3']}%")

results["exp1_vol_regimes"] = {
    "q25_rv": round(q25, 1), "q50_rv": round(q50, 1),
    "q75_rv": round(q75, 1), "q90_rv": round(q90, 1),
    "regime_stats": regime_stats,
}

# ==============================================================
# Exp 2: v3 Trade Performance by Regime
# ==============================================================
print("\n[Exp 2] v3 Trade Performance by Regime...")
all_trades = []
for symbol in COINS:
    coin = symbol.replace("USDT", "")
    ohlcv = bt.fetch_binance_15m(symbol, years=3)
    if "date_time" in ohlcv.columns:
        ohlcv = ohlcv.rename(columns={"date_time": "ts"})
    alt_df = bt.build_alt_technicals(ohlcv)
    oos_m = (alt_df["ts"] >= OOS_START) & (alt_df["ts"] <= OOS_END)
    cfg = COIN_CONFIGS.get(coin, {})
    signals, alt_merged = bt.generate_btc_led_signal(
        btc_score_ts, alt_df[oos_m],
        threshold=cfg.get("threshold", 3.0),
        use_alt_pa_filter=cfg.get("use_alt_pa_filter", False))
    trades = bt.run_backtest(alt_merged, signals,
                             sl_atr_mult=cfg.get("sl_atr_mult", 2.5),
                             tp_atr_mult=cfg.get("tp_atr_mult", 4.0),
                             cooldown_bars=cfg.get("cooldown_bars", 4))
    if len(trades) > 0:
        trades["coin"] = coin
        all_trades.append(trades)

trades_df = pd.concat(all_trades, ignore_index=True)
print(f"  Total trades: {len(trades_df)}")

# Map each trade entry to a vol regime
vol_regime_map = pd.Series(btc_oos["vol_regime"].values, index=btc_oos["ts"].values)
trades_df["entry_rv"] = trades_df["entry_time"].map(
    lambda t: btc_oos.set_index("ts")["realized_vol"].asof(t) if t >= btc_oos["ts"].min() else np.nan
)
trades_df["vol_regime"] = trades_df["entry_rv"].apply(classify_vol)

perf_by_regime = {}
for regime in ["Low", "Normal", "High", "Extreme", "Unknown"]:
    mask = trades_df["vol_regime"] == regime
    subset = trades_df[mask]
    if len(subset) == 0:
        continue
    wins = (subset["pnl_net"] > 0).sum()
    losses = (subset["pnl_net"] <= 0).sum()
    perf_by_regime[regime] = {
        "trades": int(len(subset)),
        "wins": int(wins),
        "wr": round(wins / len(subset) * 100, 1),
        "total_pnl": round(subset["pnl_net"].sum(), 1),
        "avg_pnl": round(subset["pnl_net"].mean(), 2),
        "avg_bars": round(subset["holding_bars"].mean(), 1),
        "pnl_per_bar": round(subset["pnl_net"].sum() / max(subset["holding_bars"].sum(), 1), 3),
    }
    print(f"  {regime}: {perf_by_regime[regime]['trades']} trades, "
          f"WR {perf_by_regime[regime]['wr']}%, "
          f"PnL ${perf_by_regime[regime]['total_pnl']}, "
          f"Avg ${perf_by_regime[regime]['avg_pnl']}/trade")

results["exp2_perf_by_regime"] = perf_by_regime

# ==============================================================
# Exp 3: Regime Transitions -- Performance around regime changes
# ==============================================================
print("\n[Exp 3] Regime Transitions...")
btc_oos["regime_changed"] = btc_oos["vol_regime"] != btc_oos["vol_regime"].shift(1)
transitions = btc_oos[btc_oos["regime_changed"]].copy()
n_transitions = len(transitions)

# Classify transition direction: escalation vs de-escalation
regime_order = {"Low": 0, "Normal": 1, "High": 2, "Extreme": 3}
btc_oos["regime_num"] = btc_oos["vol_regime"].map(regime_order)
btc_oos["regime_delta"] = btc_oos["regime_num"] - btc_oos["regime_num"].shift(1)

# Performance AFTER transition (next 4-16 bars)
transition_perf = {"escalation": [], "deescalation": [], "same": []}
for idx in transitions.index:
    delta = btc_oos.loc[idx, "regime_delta"]
    if pd.isna(delta):
        continue
    # Find trades that entered within 4h after this transition
    trans_time = btc_oos.loc[idx, "ts"]
    window_trades = trades_df[
        (trades_df["entry_time"] >= trans_time) &
        (trades_df["entry_time"] < trans_time + pd.Timedelta(hours=4))
    ]
    if len(window_trades) == 0:
        continue
    direction = "escalation" if delta > 0 else ("deescalation" if delta < 0 else "same")
    for _, t in window_trades.iterrows():
        transition_perf[direction].append(t["pnl_net"])

trans_results = {}
for direction, pnls in transition_perf.items():
    if len(pnls) == 0:
        continue
    pnls_arr = np.array(pnls)
    trans_results[direction] = {
        "trades": len(pnls),
        "wr": round((pnls_arr > 0).sum() / len(pnls) * 100, 1),
        "avg_pnl": round(pnls_arr.mean(), 2),
        "total_pnl": round(pnls_arr.sum(), 1),
    }
    print(f"  {direction}: {trans_results[direction]['trades']} trades, "
          f"WR {trans_results[direction]['wr']}%, "
          f"Avg ${trans_results[direction]['avg_pnl']}")

results["exp3_transitions"] = {
    "total_transitions": n_transitions,
    "avg_transitions_per_day": round(n_transitions / max((btc_oos["ts"].max() - btc_oos["ts"].min()).days, 1), 1),
    "performance_after": trans_results,
}

# ==============================================================
# Exp 4: Adaptive Position Sizing by Vol Regime
# ==============================================================
print("\n[Exp 4] Adaptive Position Sizing...")
# Baseline: $1000 per trade (fixed)
# Adaptive: Low=$1500, Normal=$1000, High=$750, Extreme=$500
sizing_configs = {
    "fixed_1000": {"Low": 1.0, "Normal": 1.0, "High": 1.0, "Extreme": 1.0},
    "vol_inverse": {"Low": 1.5, "Normal": 1.0, "High": 0.75, "Extreme": 0.5},
    "vol_follow": {"Low": 0.5, "Normal": 1.0, "High": 1.5, "Extreme": 2.0},
    "extreme_avoid": {"Low": 1.0, "Normal": 1.0, "High": 1.0, "Extreme": 0.0},
    "sweet_spot": {"Low": 0.5, "Normal": 1.5, "High": 1.0, "Extreme": 0.5},
}

sizing_results = {}
for name, multipliers in sizing_configs.items():
    trades_copy = trades_df.copy()
    trades_copy["size_mult"] = trades_copy["vol_regime"].map(multipliers).fillna(1.0)
    trades_copy["adj_pnl"] = trades_copy["pnl_net"] * trades_copy["size_mult"]
    filtered = trades_copy[trades_copy["size_mult"] > 0]

    total_pnl = filtered["adj_pnl"].sum()
    n_trades = len(filtered)
    wr = (filtered["pnl_net"] > 0).sum() / max(n_trades, 1) * 100

    # Risk-adjusted: PnL / max drawdown proxy
    cumsum = filtered["adj_pnl"].cumsum()
    running_max = cumsum.cummax()
    drawdown = cumsum - running_max
    max_dd = drawdown.min()

    sizing_results[name] = {
        "trades": n_trades,
        "total_pnl": round(total_pnl, 1),
        "wr": round(wr, 1),
        "max_dd": round(max_dd, 1),
        "calmar": round(total_pnl / max(-max_dd, 1), 2),
        "multipliers": multipliers,
    }
    print(f"  {name}: {n_trades} trades, PnL ${total_pnl:.0f}, "
          f"MaxDD ${max_dd:.0f}, Calmar {sizing_results[name]['calmar']}")

results["exp4_adaptive_sizing"] = sizing_results

# ==============================================================
# Exp 5: Vol-Based Entry Filter (Sweet Spot)
# ==============================================================
print("\n[Exp 5] Vol-Based Entry Filter...")
# Test: only enter when vol is in a certain percentile range
filter_configs = [
    ("all", 0, 100),
    ("p10_90", 10, 90),
    ("p20_80", 20, 80),
    ("p25_75", 25, 75),
    ("above_p25", 25, 100),
    ("below_p75", 0, 75),
    ("p25_90", 25, 90),
]

all_rv = btc_oos["realized_vol"].dropna()
filter_results = {}
for name, lo, hi in filter_configs:
    lo_val = all_rv.quantile(lo / 100) if lo > 0 else 0
    hi_val = all_rv.quantile(hi / 100) if hi < 100 else 9999

    mask = (trades_df["entry_rv"] >= lo_val) & (trades_df["entry_rv"] <= hi_val)
    subset = trades_df[mask]

    if len(subset) == 0:
        filter_results[name] = {"trades": 0}
        continue

    wins = (subset["pnl_net"] > 0).sum()
    filter_results[name] = {
        "trades": int(len(subset)),
        "wr": round(wins / len(subset) * 100, 1),
        "total_pnl": round(subset["pnl_net"].sum(), 1),
        "avg_pnl": round(subset["pnl_net"].mean(), 2),
        "rv_range": f"{lo_val:.1f}-{hi_val:.1f}",
    }
    pct_change = (subset["pnl_net"].sum() / max(trades_df["pnl_net"].sum(), 1) - 1) * 100
    print(f"  {name} (p{lo}-p{hi}): {filter_results[name]['trades']} trades, "
          f"WR {filter_results[name]['wr']}%, "
          f"PnL ${filter_results[name]['total_pnl']}")

results["exp5_vol_filter"] = filter_results

# ==============================================================
# Exp 6: SHORT vs LONG by Regime
# ==============================================================
print("\n[Exp 6] SHORT vs LONG by Regime...")
dir_regime = {}
for regime in ["Low", "Normal", "High", "Extreme"]:
    regime_trades = trades_df[trades_df["vol_regime"] == regime]
    if len(regime_trades) == 0:
        continue

    dir_data = {}
    for direction in ["L", "S"]:
        d_trades = regime_trades[regime_trades["dir"] == direction]
        if len(d_trades) == 0:
            dir_data[direction] = {"trades": 0, "wr": 0, "pnl": 0}
            continue
        wins = (d_trades["pnl_net"] > 0).sum()
        dir_data[direction] = {
            "trades": int(len(d_trades)),
            "wr": round(wins / len(d_trades) * 100, 1),
            "pnl": round(d_trades["pnl_net"].sum(), 1),
            "avg_pnl": round(d_trades["pnl_net"].mean(), 2),
        }

    dir_regime[regime] = dir_data
    l = dir_data.get("L", {"trades": 0, "wr": 0, "pnl": 0})
    s = dir_data.get("S", {"trades": 0, "wr": 0, "pnl": 0})
    print(f"  {regime}: LONG {l['trades']}t WR {l['wr']}% ${l['pnl']} | "
          f"SHORT {s['trades']}t WR {s['wr']}% ${s['pnl']}")

results["exp6_direction_by_regime"] = dir_regime

# ==============================================================
# Exp 7: Regime Duration & Persistence
# ==============================================================
print("\n[Exp 7] Regime Duration & Persistence...")
# How long does each regime last?
btc_oos["regime_group"] = (btc_oos["vol_regime"] != btc_oos["vol_regime"].shift(1)).cumsum()
regime_durations = btc_oos.groupby(["regime_group", "vol_regime"]).size().reset_index(name="bars")

duration_stats = {}
for regime in ["Low", "Normal", "High", "Extreme"]:
    regime_dur = regime_durations[regime_durations["vol_regime"] == regime]["bars"]
    if len(regime_dur) == 0:
        continue
    duration_stats[regime] = {
        "episodes": int(len(regime_dur)),
        "avg_duration_bars": round(regime_dur.mean(), 1),
        "avg_duration_hours": round(regime_dur.mean() * 0.25, 1),
        "median_bars": round(regime_dur.median(), 1),
        "max_bars": int(regime_dur.max()),
        "max_hours": round(regime_dur.max() * 0.25, 1),
        "min_bars": int(regime_dur.min()),
    }
    print(f"  {regime}: {duration_stats[regime]['episodes']} episodes, "
          f"avg {duration_stats[regime]['avg_duration_hours']}h, "
          f"max {duration_stats[regime]['max_hours']}h")

# Regime persistence (autocorrelation of regime)
regime_autocorr = {}
for lag in [1, 4, 16, 96]:
    corr = btc_oos["regime_num"].autocorr(lag)
    regime_autocorr[f"lag_{lag}"] = round(corr, 4) if not pd.isna(corr) else None

print(f"  Autocorrelation: lag1={regime_autocorr['lag_1']}, "
      f"lag4={regime_autocorr['lag_4']}, "
      f"lag16={regime_autocorr['lag_16']}, "
      f"lag96={regime_autocorr['lag_96']}")

results["exp7_duration"] = {
    "duration_stats": duration_stats,
    "autocorrelation": regime_autocorr,
}

# ==============================================================
# Summary & Conclusions
# ==============================================================
print("\n" + "=" * 60)
print("SUMMARY")
print("=" * 60)

# Find best and worst regimes
regime_pnls = {r: v.get("total_pnl", 0) for r, v in perf_by_regime.items() if r != "Unknown"}
best_regime = max(regime_pnls, key=regime_pnls.get) if regime_pnls else "N/A"
worst_regime = min(regime_pnls, key=regime_pnls.get) if regime_pnls else "N/A"

# Best sizing
best_sizing = max(sizing_results, key=lambda k: sizing_results[k].get("calmar", 0))
best_calmar = sizing_results[best_sizing]["calmar"]
baseline_calmar = sizing_results["fixed_1000"]["calmar"]

# Direction-regime interaction
best_dir_regime = None
best_dir_regime_wr = 0
for regime, dirs in dir_regime.items():
    for d, stats in dirs.items():
        if stats["trades"] >= 10 and stats["wr"] > best_dir_regime_wr:
            best_dir_regime_wr = stats["wr"]
            best_dir_regime = f"{d}+{regime}"

print(f"\n  Best regime for PnL: {best_regime} (${regime_pnls.get(best_regime, 0)})")
print(f"  Worst regime for PnL: {worst_regime} (${regime_pnls.get(worst_regime, 0)})")
print(f"  Best sizing strategy: {best_sizing} (Calmar {best_calmar} vs baseline {baseline_calmar})")
print(f"  Best dir+regime combo: {best_dir_regime} (WR {best_dir_regime_wr}%)")
print(f"  Regime autocorr lag96: {regime_autocorr['lag_96']} (high = predictable)")
print(f"  Vol regime thresholds: Low<{q25:.1f}%, Normal<{q75:.1f}%, High<{q90:.1f}%, Extreme>{q90:.1f}%")

results["summary"] = {
    "best_regime": best_regime,
    "best_regime_pnl": regime_pnls.get(best_regime, 0),
    "worst_regime": worst_regime,
    "worst_regime_pnl": regime_pnls.get(worst_regime, 0),
    "best_sizing": best_sizing,
    "best_sizing_calmar": best_calmar,
    "baseline_calmar": baseline_calmar,
    "calmar_improvement_pct": round((best_calmar / max(baseline_calmar, 0.01) - 1) * 100, 1),
    "best_dir_regime": best_dir_regime,
    "best_dir_regime_wr": best_dir_regime_wr,
    "regime_predictability": regime_autocorr.get("lag_96"),
    "vol_thresholds": {"low": round(q25, 1), "normal": round(q75, 1), "high": round(q90, 1)},
    "total_trades_analyzed": len(trades_df),
    "experiments_run": 7,
}

# ==============================================================
# Save Results
# ==============================================================
print("\n[Saving results...]")

# JSON
json_path = BASE_DIR / "missions" / "mission_012_vol_regime.json"
with open(json_path, "w", encoding="utf-8") as f:
    json.dump(results, f, indent=2, ensure_ascii=False, default=str)
print(f"  Saved: {json_path}")

# MD Report
md_path = BASE_DIR / "missions" / "mission_012_vol_regime.md"
with open(md_path, "w", encoding="utf-8") as f:
    f.write("# Mission 012: BTC Volatility Regime Classification & Adaptive Trading\n\n")
    f.write(f"**Date**: {datetime.utcnow().strftime('%Y-%m-%d')}\n")
    f.write(f"**Type**: regime_analysis\n")
    f.write(f"**Difficulty**: hard (7 experiments, {len(trades_df)} trades)\n\n")

    f.write("## สมมติฐาน\n")
    f.write("1. BTC 15m realized volatility สามารถแบ่งเป็น regime ที่ชัดเจน\n")
    f.write("2. v3 performance ต่างกันอย่างมีนัยสำคัญในแต่ละ regime\n")
    f.write("3. Adaptive position sizing ช่วยเพิ่ม risk-adjusted return\n\n")

    f.write("## Exp 1: Vol Regime Classification\n")
    f.write(f"Rolling 24h realized vol (annualized), OOS {OOS_START} to {OOS_END}\n\n")
    f.write(f"| Regime | Bars | % Time | Mean RV | Signal Rate (|score|>=3) |\n")
    f.write(f"|--------|------|--------|---------|------------------------|\n")
    for r in ["Low", "Normal", "High", "Extreme"]:
        s = regime_stats.get(r, {})
        if not s:
            continue
        f.write(f"| {r} | {s['bars']} | {s['pct_of_time']}% | {s['mean_rv']}% | {s['pct_score_above_3']}% |\n")
    f.write(f"\nThresholds: Low<{q25:.1f}%, Normal<{q75:.1f}%, High<{q90:.1f}%, Extreme>{q90:.1f}%\n\n")

    f.write("## Exp 2: Trade Performance by Regime\n")
    f.write(f"| Regime | Trades | WR | Total PnL | Avg PnL | PnL/bar |\n")
    f.write(f"|--------|--------|-----|-----------|---------|--------|\n")
    for r in ["Low", "Normal", "High", "Extreme"]:
        p = perf_by_regime.get(r, {})
        if not p:
            continue
        f.write(f"| {r} | {p['trades']} | {p['wr']}% | ${p['total_pnl']} | ${p['avg_pnl']} | ${p['pnl_per_bar']} |\n")

    f.write("\n## Exp 3: Regime Transitions\n")
    f.write(f"Total transitions: {n_transitions} ({results['exp3_transitions']['avg_transitions_per_day']}/day)\n\n")
    f.write(f"| Direction | Trades | WR | Avg PnL |\n")
    f.write(f"|-----------|--------|-----|--------|\n")
    for d, s in trans_results.items():
        f.write(f"| {d} | {s['trades']} | {s['wr']}% | ${s['avg_pnl']} |\n")

    f.write("\n## Exp 4: Adaptive Position Sizing\n")
    f.write(f"| Strategy | Trades | PnL | MaxDD | Calmar |\n")
    f.write(f"|----------|--------|-----|-------|--------|\n")
    for name in ["fixed_1000", "vol_inverse", "vol_follow", "extreme_avoid", "sweet_spot"]:
        s = sizing_results[name]
        f.write(f"| {name} | {s['trades']} | ${s['total_pnl']} | ${s['max_dd']} | {s['calmar']} |\n")

    f.write("\n## Exp 5: Vol-Based Entry Filter\n")
    f.write(f"| Filter | Trades | WR | PnL |\n")
    f.write(f"|--------|--------|-----|-----|\n")
    for name, lo, hi in filter_configs:
        fr = filter_results.get(name, {})
        if fr.get("trades", 0) == 0:
            continue
        f.write(f"| p{lo}-p{hi} | {fr['trades']} | {fr['wr']}% | ${fr['total_pnl']} |\n")

    f.write("\n## Exp 6: Direction x Regime\n")
    f.write(f"| Regime | LONG Trades | LONG WR | LONG PnL | SHORT Trades | SHORT WR | SHORT PnL |\n")
    f.write(f"|--------|-------------|---------|----------|--------------|----------|----------|\n")
    for r in ["Low", "Normal", "High", "Extreme"]:
        d = dir_regime.get(r, {})
        l = d.get("L", {"trades": 0, "wr": 0, "pnl": 0})
        s = d.get("S", {"trades": 0, "wr": 0, "pnl": 0})
        f.write(f"| {r} | {l['trades']} | {l['wr']}% | ${l['pnl']} | {s['trades']} | {s['wr']}% | ${s['pnl']} |\n")

    f.write("\n## Exp 7: Regime Duration & Persistence\n")
    f.write(f"| Regime | Episodes | Avg Duration | Max Duration |\n")
    f.write(f"|--------|----------|-------------|-------------|\n")
    for r in ["Low", "Normal", "High", "Extreme"]:
        ds = duration_stats.get(r, {})
        if not ds:
            continue
        f.write(f"| {r} | {ds['episodes']} | {ds['avg_duration_hours']}h | {ds['max_hours']}h |\n")
    f.write(f"\nAutocorrelation: lag1={regime_autocorr['lag_1']}, lag96={regime_autocorr['lag_96']}\n")

    f.write(f"\n## สรุปผล\n\n")
    s = results["summary"]
    f.write(f"- **Regime ที่ดีที่สุด**: {s['best_regime']} (PnL ${s['best_regime_pnl']})\n")
    f.write(f"- **Regime ที่แย่ที่สุด**: {s['worst_regime']} (PnL ${s['worst_regime_pnl']})\n")
    f.write(f"- **Sizing ที่ดีที่สุด**: {s['best_sizing']} (Calmar {s['best_sizing_calmar']} vs baseline {s['baseline_calmar']})\n")
    f.write(f"- **Direction+Regime ที่ดีที่สุด**: {s['best_dir_regime']} (WR {s['best_dir_regime_wr']}%)\n")
    f.write(f"- **Regime predictability**: autocorr lag96 = {s['regime_predictability']} (สูง = ทำนายได้)\n")
    f.write(f"- **วิเคราะห์ทั้งหมด**: {s['total_trades_analyzed']} trades, {s['experiments_run']} experiments\n")

print(f"  Saved: {md_path}")
print("\nMission 012 complete!")
print(json.dumps(results["summary"], indent=2, ensure_ascii=False, default=str))
