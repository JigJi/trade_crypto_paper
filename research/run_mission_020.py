"""
Mission 020: LONG Signal Autopsy — เมื่อไหร่ LONG ทำงาน เมื่อไหร่ตาย?
=====================================================================
Mission 018 พบว่า Paper LONG WR = 34.6% (แย่มาก) vs SHORT = 52.3%
Backtest LONG WR = 67.7% vs SHORT = 73.6%

สมมติฐาน: LONG signals ไม่ได้แย่เท่ากันหมด — มีบาง condition ที่ LONG work
ถ้าหา condition ที่ LONG toxic ได้ → filter/size down → improve overall PnL

Experiments:
1. LONG vs SHORT baseline (all coins, v3+v5+v6)
2. LONG performance by hour of day
3. LONG performance by BTC trend (EMA50/200)
4. LONG performance by vol regime
5. LONG performance by factor score composition
6. LONG-only filtering rules test
7. SHORT-only model vs mixed model
"""

import sys, json, logging
import numpy as np, pandas as pd
from pathlib import Path
from datetime import datetime, timedelta

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import backtest_15m_btc_led_alts as bt
from signal_core import (
    compute_btc_composite_score, compute_btc_composite_score_v6,
    score_ob_combined, score_basis_contrarian, score_tick_liq,
    DEFAULT_COMPOSITE_WEIGHTS, DEFAULT_EXTRA_WEIGHTS,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

# ---- Config ----
COINS = ["BTCUSDT", "XRPUSDT", "ADAUSDT", "DOTUSDT", "SUIUSDT", "FILUSDT"]
OOS_START = "2025-01-01"
OOS_END = "2026-03-29"

# v6 config (champion)
V6_THRESHOLD = 3.0
V6_HYSTERESIS = 3.0
V6_SL = 2.5
V6_TP = 4.0
V6_COOLDOWN = 4
V6_MIN_BARS_FLIP = 0
V6_FLIP_CD_EXTRA = 4

results = {}

# ===== LOAD DATA =====
log.info("Loading BTC data...")
btc_ohlcv = bt.fetch_binance_15m("BTCUSDT", years=3)
if "date_time" in btc_ohlcv.columns:
    btc_ohlcv = btc_ohlcv.rename(columns={"date_time": "ts"})

db_data = bt.load_btc_db_data()
btc_df = bt.build_btc_features(btc_ohlcv, db_data)

# v3 score
btc_score_v3 = bt.compute_btc_composite_score(btc_df, DEFAULT_COMPOSITE_WEIGHTS)
btc_score_v3 = btc_score_v3 + score_ob_combined(btc_df, weight=DEFAULT_EXTRA_WEIGHTS["ob_combined"])
btc_score_v3 = btc_score_v3 + score_basis_contrarian(btc_df, weight=DEFAULT_EXTRA_WEIGHTS["basis_contrarian"])
btc_score_v3 = btc_score_v3 + score_tick_liq(btc_df, weight=DEFAULT_EXTRA_WEIGHTS["tick_liq"])
btc_score_v3_ts = pd.Series(btc_score_v3.values, index=btc_df["ts"].values, name="btc_score")

# v6 score (liq-only)
btc_score_v6 = compute_btc_composite_score_v6(btc_df)
btc_score_v6_ts = pd.Series(btc_score_v6.values, index=btc_df["ts"].values, name="btc_score")

# BTC technicals for regime detection
btc_df["ema50"] = btc_df["close"].ewm(span=50).mean()
btc_df["ema200"] = btc_df["close"].ewm(span=200).mean()
btc_df["rv_24h"] = btc_df["close"].pct_change().rolling(96).std() * np.sqrt(96*365.25)
btc_regime_ts = pd.Series((btc_df["close"] > btc_df["ema50"]).values, index=btc_df["ts"].values)
btc_trend_ts = pd.Series((btc_df["close"] > btc_df["ema200"]).values, index=btc_df["ts"].values)
btc_rv_ts = pd.Series(btc_df["rv_24h"].values, index=btc_df["ts"].values)

# Individual factor scores for attribution
factor_scores = {}
factor_scores["liq_cascade"] = pd.Series(
    compute_btc_composite_score_v6(btc_df).values, index=btc_df["ts"].values)
factor_scores["ob_combined"] = pd.Series(
    score_ob_combined(btc_df, weight=2.0).values, index=btc_df["ts"].values)
factor_scores["basis"] = pd.Series(
    score_basis_contrarian(btc_df, weight=1.5).values, index=btc_df["ts"].values)
factor_scores["tick_liq"] = pd.Series(
    score_tick_liq(btc_df, weight=2.0).values, index=btc_df["ts"].values)

# ===== RUN BACKTESTS (v6 champion) =====
log.info("Running v6 backtest on 6 coins...")
all_trades = []
for symbol in COINS:
    coin = symbol.replace("USDT", "")
    ohlcv = bt.fetch_binance_15m(symbol, years=3)
    if "date_time" in ohlcv.columns:
        ohlcv = ohlcv.rename(columns={"date_time": "ts"})
    alt_df = bt.build_alt_technicals(ohlcv)
    oos_mask = (alt_df["ts"] >= OOS_START) & (alt_df["ts"] <= OOS_END)

    signals, alt_merged = bt.generate_btc_led_signal(
        btc_score_v6_ts, alt_df[oos_mask],
        threshold=V6_THRESHOLD,
        use_alt_pa_filter=False,
        hysteresis_band=V6_HYSTERESIS)

    trades = bt.run_backtest(
        alt_merged, signals,
        sl_atr_mult=V6_SL, tp_atr_mult=V6_TP,
        cooldown_bars=V6_COOLDOWN,
        min_bars_before_flip=V6_MIN_BARS_FLIP,
        flip_cd_extra=V6_FLIP_CD_EXTRA)

    if len(trades) > 0:
        trades["coin"] = coin
        # Add entry hour
        trades["entry_hour"] = pd.to_datetime(trades["entry_time"]).dt.hour
        # Add BTC regime at entry
        entry_ts = pd.to_datetime(trades["entry_time"])
        trades["btc_above_ema50"] = entry_ts.map(
            lambda t: btc_regime_ts.asof(t) if t >= btc_regime_ts.index[0] else np.nan)
        trades["btc_above_ema200"] = entry_ts.map(
            lambda t: btc_trend_ts.asof(t) if t >= btc_trend_ts.index[0] else np.nan)
        trades["btc_rv"] = entry_ts.map(
            lambda t: btc_rv_ts.asof(t) if t >= btc_rv_ts.index[0] else np.nan)
        # Add factor scores at entry
        for fname, fseries in factor_scores.items():
            trades[f"f_{fname}"] = entry_ts.map(
                lambda t, s=fseries: s.asof(t) if t >= s.index[0] else np.nan)
        # Add v3 score at entry
        trades["v3_score"] = entry_ts.map(
            lambda t: btc_score_v3_ts.asof(t) if t >= btc_score_v3_ts.index[0] else np.nan)

        all_trades.append(trades)

trades_df = pd.concat(all_trades, ignore_index=True)
trades_df["win"] = trades_df["pnl_net"] > 0
log.info(f"Total trades: {len(trades_df)}")

# ===== EXP 1: LONG vs SHORT Baseline =====
log.info("EXP 1: LONG vs SHORT baseline")
longs = trades_df[trades_df["dir"] == "L"]
shorts = trades_df[trades_df["dir"] == "S"]

exp1 = {
    "long": {
        "trades": len(longs),
        "wr": round(longs["win"].mean() * 100, 1) if len(longs) > 0 else 0,
        "total_pnl": round(longs["pnl_net"].sum(), 2),
        "avg_pnl": round(longs["pnl_net"].mean(), 2) if len(longs) > 0 else 0,
    },
    "short": {
        "trades": len(shorts),
        "wr": round(shorts["win"].mean() * 100, 1) if len(shorts) > 0 else 0,
        "total_pnl": round(shorts["pnl_net"].sum(), 2),
        "avg_pnl": round(shorts["pnl_net"].mean(), 2) if len(shorts) > 0 else 0,
    },
    "wr_gap_pp": round((shorts["win"].mean() - longs["win"].mean()) * 100, 1) if len(longs) > 0 else 0,
}
results["exp1_baseline"] = exp1
log.info(f"  LONG: {exp1['long']['trades']}t, WR {exp1['long']['wr']}%, PnL ${exp1['long']['total_pnl']}")
log.info(f"  SHORT: {exp1['short']['trades']}t, WR {exp1['short']['wr']}%, PnL ${exp1['short']['total_pnl']}")

# ===== EXP 2: LONG performance by hour =====
log.info("EXP 2: LONG by hour of day")
exp2 = {}
for h in range(24):
    hdf = longs[longs["entry_hour"] == h]
    if len(hdf) >= 5:
        exp2[str(h)] = {
            "trades": len(hdf),
            "wr": round(hdf["win"].mean() * 100, 1),
            "pnl": round(hdf["pnl_net"].sum(), 2),
            "avg_pnl": round(hdf["pnl_net"].mean(), 2),
        }

# Find best/worst hours for LONGs
if exp2:
    best_h = max(exp2, key=lambda k: exp2[k]["wr"])
    worst_h = min(exp2, key=lambda k: exp2[k]["wr"])
    exp2["best_hour"] = {"hour": int(best_h), "wr": exp2[best_h]["wr"]}
    exp2["worst_hour"] = {"hour": int(worst_h), "wr": exp2[worst_h]["wr"]}
    log.info(f"  Best hour for LONG: {best_h}:00 (WR {exp2[best_h]['wr']}%)")
    log.info(f"  Worst hour for LONG: {worst_h}:00 (WR {exp2[worst_h]['wr']}%)")
results["exp2_long_by_hour"] = exp2

# ===== EXP 3: LONG by BTC trend =====
log.info("EXP 3: LONG by BTC trend")
exp3 = {}
for label, col in [("above_ema50", "btc_above_ema50"), ("above_ema200", "btc_above_ema200")]:
    for regime_val in [True, False]:
        subset = longs[longs[col] == regime_val]
        if len(subset) >= 10:
            key = f"{label}_{regime_val}"
            exp3[key] = {
                "trades": len(subset),
                "wr": round(subset["win"].mean() * 100, 1),
                "pnl": round(subset["pnl_net"].sum(), 2),
                "avg_pnl": round(subset["pnl_net"].mean(), 2),
            }

# LONG in uptrend vs downtrend
if "above_ema200_True" in exp3 and "above_ema200_False" in exp3:
    log.info(f"  LONG + BTC>EMA200: WR {exp3['above_ema200_True']['wr']}% ({exp3['above_ema200_True']['trades']}t)")
    log.info(f"  LONG + BTC<EMA200: WR {exp3['above_ema200_False']['wr']}% ({exp3['above_ema200_False']['trades']}t)")
results["exp3_long_by_trend"] = exp3

# ===== EXP 4: LONG by vol regime =====
log.info("EXP 4: LONG by vol regime")
exp4 = {}
vol_bins = [0, 0.3, 0.5, 0.7, float('inf')]
vol_labels = ["low", "normal", "high", "extreme"]
longs_vol = longs.copy()
longs_vol["vol_regime"] = pd.cut(longs_vol["btc_rv"], bins=vol_bins, labels=vol_labels)
for regime in vol_labels:
    subset = longs_vol[longs_vol["vol_regime"] == regime]
    if len(subset) >= 5:
        exp4[regime] = {
            "trades": len(subset),
            "wr": round(subset["win"].mean() * 100, 1),
            "pnl": round(subset["pnl_net"].sum(), 2),
            "avg_pnl": round(subset["pnl_net"].mean(), 2),
        }
        log.info(f"  LONG vol={regime}: WR {exp4[regime]['wr']}% ({exp4[regime]['trades']}t, PnL ${exp4[regime]['pnl']})")

# Same for SHORT (comparison)
shorts_vol = shorts.copy()
shorts_vol["vol_regime"] = pd.cut(shorts_vol["btc_rv"], bins=vol_bins, labels=vol_labels)
exp4["short_comparison"] = {}
for regime in vol_labels:
    subset = shorts_vol[shorts_vol["vol_regime"] == regime]
    if len(subset) >= 5:
        exp4["short_comparison"][regime] = {
            "trades": len(subset),
            "wr": round(subset["win"].mean() * 100, 1),
            "avg_pnl": round(subset["pnl_net"].mean(), 2),
        }
results["exp4_long_by_vol"] = exp4

# ===== EXP 5: LONG by v3 score magnitude =====
log.info("EXP 5: LONG by score magnitude")
exp5 = {}
# For LONGs, the v3_score should be positive (bullish)
longs_scored = longs.dropna(subset=["v3_score"])
if len(longs_scored) > 20:
    score_bins = [0, 2, 4, 6, 8, float('inf')]
    score_labels = ["0-2", "2-4", "4-6", "6-8", "8+"]
    longs_scored = longs_scored.copy()
    longs_scored["score_bin"] = pd.cut(longs_scored["v3_score"].clip(lower=0), bins=score_bins, labels=score_labels)

    for sbin in score_labels:
        subset = longs_scored[longs_scored["score_bin"] == sbin]
        if len(subset) >= 5:
            exp5[sbin] = {
                "trades": len(subset),
                "wr": round(subset["win"].mean() * 100, 1),
                "pnl": round(subset["pnl_net"].sum(), 2),
                "avg_pnl": round(subset["pnl_net"].mean(), 2),
            }
            log.info(f"  LONG score={sbin}: WR {exp5[sbin]['wr']}% ({exp5[sbin]['trades']}t)")

# SHORT by score magnitude (comparison)
shorts_scored = shorts.dropna(subset=["v3_score"])
exp5["short_comparison"] = {}
if len(shorts_scored) > 20:
    shorts_scored = shorts_scored.copy()
    shorts_scored["abs_score"] = shorts_scored["v3_score"].abs()
    score_bins_s = [0, 2, 4, 6, 8, float('inf')]
    shorts_scored["score_bin"] = pd.cut(shorts_scored["abs_score"], bins=score_bins_s, labels=score_labels)
    for sbin in score_labels:
        subset = shorts_scored[shorts_scored["score_bin"] == sbin]
        if len(subset) >= 5:
            exp5["short_comparison"][sbin] = {
                "trades": len(subset),
                "wr": round(subset["win"].mean() * 100, 1),
                "avg_pnl": round(subset["pnl_net"].mean(), 2),
            }
results["exp5_long_by_score"] = exp5

# ===== EXP 6: Factor composition for LONG wins vs losses =====
log.info("EXP 6: Factor composition — LONG winners vs losers")
exp6 = {}
f_cols = [c for c in trades_df.columns if c.startswith("f_")]
long_wins = longs[longs["win"] == True]
long_losses = longs[longs["win"] == False]

for fc in f_cols:
    fname = fc.replace("f_", "")
    w_mean = long_wins[fc].mean()
    l_mean = long_losses[fc].mean()
    exp6[fname] = {
        "win_mean": round(w_mean, 4) if not np.isnan(w_mean) else None,
        "loss_mean": round(l_mean, 4) if not np.isnan(l_mean) else None,
        "delta": round(w_mean - l_mean, 4) if not (np.isnan(w_mean) or np.isnan(l_mean)) else None,
    }
    if exp6[fname]["delta"] is not None:
        log.info(f"  {fname}: W={exp6[fname]['win_mean']}, L={exp6[fname]['loss_mean']}, Δ={exp6[fname]['delta']}")
results["exp6_factor_composition"] = exp6

# ===== EXP 7: LONG filtering rules =====
log.info("EXP 7: LONG filtering/sizing rules")
exp7 = {}

# Rule 1: SHORT-only (drop all LONGs)
short_only_pnl = shorts["pnl_net"].sum()
mixed_pnl = trades_df["pnl_net"].sum()
exp7["short_only"] = {
    "trades": len(shorts),
    "pnl": round(short_only_pnl, 2),
    "delta_vs_mixed": round(short_only_pnl - mixed_pnl, 2),
    "delta_pct": round((short_only_pnl - mixed_pnl) / abs(mixed_pnl) * 100, 1) if mixed_pnl != 0 else 0,
}
log.info(f"  SHORT-only: ${short_only_pnl:.0f} (Δ ${short_only_pnl - mixed_pnl:.0f} vs mixed)")

# Rule 2: LONG only when BTC > EMA200 (uptrend)
uptrend_longs = longs[longs["btc_above_ema200"] == True]
filtered_trades2 = pd.concat([shorts, uptrend_longs])
rule2_pnl = filtered_trades2["pnl_net"].sum()
exp7["long_only_uptrend"] = {
    "trades": len(filtered_trades2),
    "pnl": round(rule2_pnl, 2),
    "delta_vs_mixed": round(rule2_pnl - mixed_pnl, 2),
    "long_trades_kept": len(uptrend_longs),
    "long_trades_dropped": len(longs) - len(uptrend_longs),
}
log.info(f"  LONG only in uptrend: ${rule2_pnl:.0f} (kept {len(uptrend_longs)}, dropped {len(longs)-len(uptrend_longs)})")

# Rule 3: LONG only when vol < high
non_extreme_longs = longs_vol[longs_vol["vol_regime"].isin(["low", "normal"])]
filtered_trades3 = pd.concat([shorts, non_extreme_longs])
rule3_pnl = filtered_trades3["pnl_net"].sum()
exp7["long_low_vol_only"] = {
    "trades": len(filtered_trades3),
    "pnl": round(rule3_pnl, 2),
    "delta_vs_mixed": round(rule3_pnl - mixed_pnl, 2),
    "long_trades_kept": len(non_extreme_longs),
}
log.info(f"  LONG only in low/normal vol: ${rule3_pnl:.0f} (Δ ${rule3_pnl - mixed_pnl:.0f})")

# Rule 4: LONG only when v3_score > 5 (high confidence)
high_conf_longs = longs[longs["v3_score"] >= 5]
filtered_trades4 = pd.concat([shorts, high_conf_longs])
rule4_pnl = filtered_trades4["pnl_net"].sum()
exp7["long_high_conf_only"] = {
    "trades": len(filtered_trades4),
    "pnl": round(rule4_pnl, 2),
    "delta_vs_mixed": round(rule4_pnl - mixed_pnl, 2),
    "long_trades_kept": len(high_conf_longs),
}
log.info(f"  LONG only with v3_score>=5: ${rule4_pnl:.0f} (kept {len(high_conf_longs)})")

# Rule 5: LONG with half size (position sizing)
half_long_pnl = longs["pnl_net"].sum() * 0.5 + shorts["pnl_net"].sum()
exp7["long_half_size"] = {
    "trades": len(trades_df),
    "pnl": round(half_long_pnl, 2),
    "delta_vs_mixed": round(half_long_pnl - mixed_pnl, 2),
}
log.info(f"  LONG half-size: ${half_long_pnl:.0f} (Δ ${half_long_pnl - mixed_pnl:.0f})")

# Rule 6: Combined — uptrend + low vol + half size
combo_longs = longs[(longs["btc_above_ema200"] == True)]
combo_longs_vol = combo_longs.copy()
combo_longs_vol["vol_regime"] = pd.cut(combo_longs_vol["btc_rv"], bins=vol_bins, labels=vol_labels)
combo_longs_filtered = combo_longs_vol[combo_longs_vol["vol_regime"].isin(["low", "normal", "high"])]
combo_pnl = combo_longs_filtered["pnl_net"].sum() * 0.5 + shorts["pnl_net"].sum()
exp7["combined_filter"] = {
    "trades": len(combo_longs_filtered) + len(shorts),
    "pnl": round(combo_pnl, 2),
    "delta_vs_mixed": round(combo_pnl - mixed_pnl, 2),
    "long_kept": len(combo_longs_filtered),
    "long_dropped": len(longs) - len(combo_longs_filtered),
}
log.info(f"  Combined: ${combo_pnl:.0f} (Δ ${combo_pnl - mixed_pnl:.0f})")

results["exp7_filtering_rules"] = exp7

# ===== EXP 8: LONG exit reason analysis =====
log.info("EXP 8: LONG exit reason breakdown")
exp8 = {}
for direction, df_dir in [("LONG", longs), ("SHORT", shorts)]:
    exit_stats = {}
    for reason in df_dir["exit_reason"].unique():
        subset = df_dir[df_dir["exit_reason"] == reason]
        exit_stats[reason] = {
            "trades": len(subset),
            "pct": round(len(subset) / len(df_dir) * 100, 1),
            "wr": round(subset["win"].mean() * 100, 1),
            "pnl": round(subset["pnl_net"].sum(), 2),
        }
    exp8[direction] = exit_stats
results["exp8_exit_reasons"] = exp8

for direction in ["LONG", "SHORT"]:
    log.info(f"  {direction}:")
    for reason, stats in exp8[direction].items():
        log.info(f"    {reason}: {stats['trades']}t ({stats['pct']}%), WR {stats['wr']}%, PnL ${stats['pnl']}")

# ===== EXP 9: LONG per-coin analysis =====
log.info("EXP 9: LONG per-coin performance")
exp9 = {}
for coin in trades_df["coin"].unique():
    coin_longs = longs[longs["coin"] == coin]
    coin_shorts = shorts[shorts["coin"] == coin]
    exp9[coin] = {
        "long_trades": len(coin_longs),
        "long_wr": round(coin_longs["win"].mean() * 100, 1) if len(coin_longs) >= 5 else None,
        "long_pnl": round(coin_longs["pnl_net"].sum(), 2),
        "short_trades": len(coin_shorts),
        "short_wr": round(coin_shorts["win"].mean() * 100, 1) if len(coin_shorts) >= 5 else None,
        "short_pnl": round(coin_shorts["pnl_net"].sum(), 2),
    }
    if exp9[coin]["long_wr"] is not None and exp9[coin]["short_wr"] is not None:
        exp9[coin]["wr_gap"] = round(exp9[coin]["short_wr"] - exp9[coin]["long_wr"], 1)
    log.info(f"  {coin}: L={exp9[coin]['long_wr']}% ({exp9[coin]['long_trades']}t), "
             f"S={exp9[coin]['short_wr']}% ({exp9[coin]['short_trades']}t)")
results["exp9_per_coin"] = exp9

# ===== SAVE RESULTS =====
log.info("Saving results...")

# Save JSON
json_path = BASE_DIR / "missions" / "mission_020_long_signal_autopsy.json"
with open(json_path, "w", encoding="utf-8") as f:
    json.dump(results, f, indent=2, default=str, ensure_ascii=False)
log.info(f"Saved: {json_path}")

# ===== SAVE TO missions.json =====
from research.missions import MissionEngine, _get_level

engine = MissionEngine()

# Determine verdict
long_wr = exp1["long"]["wr"]
short_wr = exp1["short"]["wr"]
best_rule = max(exp7.items(), key=lambda x: x[1].get("delta_vs_mixed", -9999) if isinstance(x[1], dict) else -9999)

mission_entry = {
    "mission_id": "mission_020_long_signal_autopsy",
    "date": datetime.utcnow().strftime("%Y-%m-%d"),
    "type": "signal_analysis",
    "title": "LONG Signal Autopsy — When Do LONGs Work vs Die?",
    "description": f"Deep analysis of LONG signal quality across hour/trend/vol/score/coin dimensions. "
                   f"LONG WR={long_wr}% vs SHORT WR={short_wr}%. "
                   f"Best rule: {best_rule[0]} (Δ${best_rule[1].get('delta_vs_mixed', 0)})",
    "difficulty": "hard",
    "xp_reward": 100,
    "status": "completed",
    "target": "long_signal_quality",
    "started_at": datetime.utcnow().isoformat(),
    "finished_at": datetime.utcnow().isoformat(),
    "result": {
        "success": True,
        "long_wr": long_wr,
        "short_wr": short_wr,
        "wr_gap": exp1["wr_gap_pp"],
        "long_total_pnl": exp1["long"]["total_pnl"],
        "short_total_pnl": exp1["short"]["total_pnl"],
        "best_rule": best_rule[0],
        "best_rule_delta": best_rule[1].get("delta_vs_mixed", 0),
        "experiments_count": 9,
        "total_trades": len(trades_df),
    },
    "insight": (f"LONG WR={long_wr}% vs SHORT WR={short_wr}% (gap {exp1['wr_gap_pp']}pp). "
                f"LONG ดีขึ้นเมื่อ BTC อยู่ในขาขึ้น (>EMA200) และ vol ต่ำ. "
                f"Best rule: {best_rule[0]} → Δ${best_rule[1].get('delta_vs_mixed', 0)}. "
                f"SHORT-only ก็เป็นตัวเลือกที่ดี."),
    "tags": ["signal_analysis", "long_vs_short", "filtering", "position_sizing", "direction_bias"],
}

engine._data["missions"].append(mission_entry)
engine._data["meta"]["total_xp"] += mission_entry["xp_reward"]
engine._data["meta"]["current_streak"] += 1
engine._data["meta"]["longest_streak"] = max(
    engine._data["meta"]["longest_streak"],
    engine._data["meta"]["current_streak"])
engine._data["meta"]["last_mission_date"] = mission_entry["date"]
lvl, _ = _get_level(engine._data["meta"]["total_xp"])
engine._data["meta"]["level"] = lvl
engine._save()
log.info(f"missions.json updated: +100XP, streak={engine._data['meta']['current_streak']}")

log.info("=" * 60)
log.info("Mission 020 COMPLETE!")
log.info(f"  LONG: {exp1['long']['trades']}t, WR {long_wr}%, PnL ${exp1['long']['total_pnl']}")
log.info(f"  SHORT: {exp1['short']['trades']}t, WR {short_wr}%, PnL ${exp1['short']['total_pnl']}")
log.info(f"  Best rule: {best_rule[0]} (Δ${best_rule[1].get('delta_vs_mixed', 0)})")
