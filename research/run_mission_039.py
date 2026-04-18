"""
Mission 039: Regime Birth Survival Predictor
---------------------------------------------
ต่อยอดจาก Mission 038 ที่พบว่า age=1 trades ขาดทุน (WR 47.9%, -$279)
ขณะที่ age>=8 เป็น sweet spot (WR 73.1%, +$3,508)

คำถาม: ที่จังหวะสัญญาณเพิ่งเกิด (signal_age=1) — มี pattern ของ factors
หรือสภาพตลาดที่สามารถทำนายได้ไหมว่า regime นี้จะอยู่ยาว (survivor) หรือ
ตายเร็ว (short-lived)?

ถ้าเจอ discriminator ที่ชัด -> สามารถ "กู้" age=1 trades บางตัวให้ทำกำไรได้
แทนที่จะ skip ทั้งหมด

สมมติฐาน:
1. Regime ที่ born พร้อม score magnitude สูง -> survive นานกว่า
2. Liquidation cascade + tick_liq ที่ birth = confirmation ทิศทาง
3. High volatility at birth = regime มีโอกาส survive นาน
4. Basis contrarian extreme = stronger regime birth
"""
import sys, json, logging
import numpy as np, pandas as pd
from pathlib import Path
from datetime import datetime

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
SURVIVOR_THRESHOLD = 8  # bars — จาก Mission 038 "sweet spot" age>=8

results = {}

# ── Step 1: Build BTC composite + factor fields ──
log.info("Loading BTC data and building features...")
btc_ohlcv = bt.fetch_binance_15m("BTCUSDT", years=3)
if "date_time" in btc_ohlcv.columns:
    btc_ohlcv = btc_ohlcv.rename(columns={"date_time": "ts"})
db_data = bt.load_btc_db_data()
btc_df = bt.build_btc_features(btc_ohlcv, db_data)

# Core composite score
btc_score = bt.compute_btc_composite_score(btc_df, COMPOSITE_WEIGHTS)
# v3 extra factor scorers (keep individual series to profile birth)
s_ob = score_ob_combined(btc_df, weight=V3_EXTRA_WEIGHTS["ob_combined"])
s_basis = score_basis_contrarian(btc_df, weight=V3_EXTRA_WEIGHTS["basis_contrarian"])
s_tick = score_tick_liq(btc_df, weight=V3_EXTRA_WEIGHTS["tick_liq"])
btc_score_full = btc_score + s_ob + s_basis + s_tick

score_df = pd.DataFrame({
    "ts": btc_df["ts"].values,
    "btc_score": btc_score_full.values,
    "score_core": btc_score.values,
    "score_ob": s_ob.values,
    "score_basis": s_basis.values,
    "score_tick": s_tick.values,
    # Raw BTC state
    "range_z": btc_df.get("range_z", pd.Series(0, index=btc_df.index)).fillna(0).values,
    "atr": btc_df["atr"].fillna(0).values,
    "rsi": btc_df["rsi"].fillna(50).values,
    "ema21_dist": btc_df.get("ema21_dist", pd.Series(0, index=btc_df.index)).fillna(0).values,
    "vol_ratio": btc_df["vol_ratio"].fillna(1).values,
    # Raw factor inputs for deeper profiling
    "fr_8h": btc_df.get("fr_8h", pd.Series(np.nan, index=btc_df.index)).values,
    "liq_net": btc_df.get("liq_net", pd.Series(0, index=btc_df.index)).fillna(0).values,
    "liq_total": btc_df.get("liq_total", pd.Series(0, index=btc_df.index)).fillna(0).values,
    "liq_total_ma": btc_df.get("liq_total_ma", pd.Series(1, index=btc_df.index)).fillna(1).values,
    "whale_net_ma": btc_df.get("whale_net_ma", pd.Series(0, index=btc_df.index)).fillna(0).values,
    "basis_z": btc_df.get("basis_z", pd.Series(0, index=btc_df.index)).fillna(0).values,
    "liq_net_ma": btc_df.get("liq_net_ma", pd.Series(0, index=btc_df.index)).fillna(0).values,
    "ob_imb_ma": btc_df.get("ob_imb_ma", pd.Series(0, index=btc_df.index)).fillna(0).values,
}).sort_values("ts").reset_index(drop=True)

# ── Step 2: Identify signal regimes with threshold 3.0 ──
threshold = 3.0
raw = np.where(score_df["btc_score"] >= threshold, 1,
      np.where(score_df["btc_score"] <= -threshold, -1, 0))
score_df["raw_signal"] = raw

# Compute signal_age
age = np.zeros(len(score_df), dtype=int)
for i in range(1, len(score_df)):
    if raw[i] != 0 and raw[i] == raw[i-1]:
        age[i] = age[i-1] + 1
    elif raw[i] != 0:
        age[i] = 1
    else:
        age[i] = 0
score_df["signal_age"] = age

# Restrict to OOS
oos_mask = (score_df["ts"] >= OOS_START) & (score_df["ts"] <= OOS_END)
oos_df = score_df[oos_mask].reset_index(drop=True)

# ── Step 3: Build Regime Birth Catalog ──
log.info("Building regime birth catalog...")
births_idx = np.where(oos_df["signal_age"].values == 1)[0]

birth_records = []
for idx in births_idx:
    direction = int(oos_df["raw_signal"].iloc[idx])
    # Find regime duration: count consecutive bars with same signal
    duration = 1
    j = idx + 1
    while j < len(oos_df) and oos_df["raw_signal"].iloc[j] == direction:
        duration += 1
        j += 1

    row = oos_df.iloc[idx]
    record = {
        "ts": row["ts"],
        "direction": direction,
        "duration": duration,
        "survivor": duration >= SURVIVOR_THRESHOLD,
        "score_birth": float(row["btc_score"]),
        "score_abs": float(abs(row["btc_score"])),
        "score_core": float(row["score_core"]),
        "score_ob": float(row["score_ob"]),
        "score_basis": float(row["score_basis"]),
        "score_tick": float(row["score_tick"]),
        "range_z": float(row["range_z"]),
        "atr": float(row["atr"]),
        "rsi": float(row["rsi"]),
        "ema21_dist": float(row["ema21_dist"]),
        "vol_ratio": float(row["vol_ratio"]),
        "fr_8h": float(row["fr_8h"]) if not pd.isna(row["fr_8h"]) else 0.0,
        "liq_net": float(row["liq_net"]),
        "liq_total_ratio": float(row["liq_total"] / max(row["liq_total_ma"], 1)),
        "whale_net_ma": float(row["whale_net_ma"]),
        "basis_z": float(row["basis_z"]),
        "liq_net_ma": float(row["liq_net_ma"]),
        "ob_imb_ma": float(row["ob_imb_ma"]),
        "hour": pd.Timestamp(row["ts"]).hour,
    }
    birth_records.append(record)

births = pd.DataFrame(birth_records)
log.info(f"Total regime births: {len(births)}")
log.info(f"Survivors (>={SURVIVOR_THRESHOLD} bars): {births['survivor'].sum()} "
         f"({100*births['survivor'].mean():.1f}%)")
log.info(f"Short-lived (<{SURVIVOR_THRESHOLD} bars): {(~births['survivor']).sum()} "
         f"({100*(~births['survivor']).mean():.1f}%)")

results["exp1_birth_catalog"] = {
    "total_births": int(len(births)),
    "survivors": int(births["survivor"].sum()),
    "short_lived": int((~births["survivor"]).sum()),
    "survivor_rate_pct": round(100 * births["survivor"].mean(), 1),
    "survivor_threshold_bars": SURVIVOR_THRESHOLD,
    "duration_mean": round(float(births["duration"].mean()), 2),
    "duration_median": int(births["duration"].median()),
    "duration_p75": int(np.percentile(births["duration"], 75)),
    "duration_p90": int(np.percentile(births["duration"], 90)),
    "duration_max": int(births["duration"].max()),
}

# ── EXP2: Feature Profile — Survivor vs Short-Lived ──
log.info("=== EXP2: Survivor vs Short-Lived Profile ===")
feature_cols = [
    "score_abs", "score_core", "score_ob", "score_basis", "score_tick",
    "range_z", "atr", "rsi", "ema21_dist", "vol_ratio",
    "fr_8h", "liq_net", "liq_total_ratio", "whale_net_ma",
    "basis_z", "liq_net_ma", "ob_imb_ma",
]
surv = births[births["survivor"]]
short = births[~births["survivor"]]

profile = {}
for col in feature_cols:
    s_mean = float(surv[col].mean())
    l_mean = float(short[col].mean())
    s_median = float(surv[col].median())
    l_median = float(short[col].median())
    s_std = float(surv[col].std()) if len(surv) > 1 else 0
    l_std = float(short[col].std()) if len(short) > 1 else 0
    # Cohen's d (effect size)
    pooled_std = np.sqrt((s_std**2 + l_std**2) / 2) if (s_std + l_std) > 0 else 1
    cohen_d = (s_mean - l_mean) / max(pooled_std, 1e-8)
    profile[col] = {
        "surv_mean": round(s_mean, 4),
        "short_mean": round(l_mean, 4),
        "surv_median": round(s_median, 4),
        "short_median": round(l_median, 4),
        "delta_mean": round(s_mean - l_mean, 4),
        "cohen_d": round(cohen_d, 3),
    }
    mark = " <<" if abs(cohen_d) > 0.2 else ""
    log.info(f"  {col:>18s}: surv={s_mean:+.3f} short={l_mean:+.3f} "
             f"Δ={s_mean-l_mean:+.3f} d={cohen_d:+.2f}{mark}")

results["exp2_survivor_profile"] = profile

# ── EXP3: Direction-split profile (LONG vs SHORT births) ──
log.info("=== EXP3: LONG vs SHORT Birth Profile ===")
dir_profile = {}
for dir_label, dir_val in [("LONG", 1), ("SHORT", -1)]:
    dir_births = births[births["direction"] == dir_val]
    if len(dir_births) == 0:
        continue
    surv_d = dir_births[dir_births["survivor"]]
    short_d = dir_births[~dir_births["survivor"]]
    dir_profile[dir_label] = {
        "total": int(len(dir_births)),
        "survivors": int(len(surv_d)),
        "short_lived": int(len(short_d)),
        "surv_rate_pct": round(100 * dir_births["survivor"].mean(), 1),
        "duration_mean": round(float(dir_births["duration"].mean()), 2),
    }
    log.info(f"  {dir_label}: n={len(dir_births)}, survivors={len(surv_d)} "
             f"({100*dir_births['survivor'].mean():.1f}%), dur_mean={dir_births['duration'].mean():.1f}")

results["exp3_direction_profile"] = dir_profile

# ── EXP4: Score Magnitude Buckets vs Survival Rate ──
log.info("=== EXP4: Score Magnitude at Birth vs Survival ===")
# Buckets of |score| at birth
score_bins = [3.0, 3.5, 4.0, 5.0, 6.0, 8.0, 20.0]
score_labels = ["3.0-3.5", "3.5-4.0", "4.0-5.0", "5.0-6.0", "6.0-8.0", "8.0+"]
births["score_bucket"] = pd.cut(births["score_abs"], bins=score_bins, labels=score_labels, right=False)

score_bucket_stats = {}
for label in score_labels:
    sub = births[births["score_bucket"] == label]
    if len(sub) < 5:
        continue
    n = len(sub)
    surv_rate = sub["survivor"].mean() * 100
    dur_mean = sub["duration"].mean()
    dur_median = sub["duration"].median()
    score_bucket_stats[label] = {
        "n": int(n),
        "surv_rate_pct": round(surv_rate, 1),
        "dur_mean": round(float(dur_mean), 2),
        "dur_median": int(dur_median),
    }
    log.info(f"  |score|={label}: n={n}, surv={surv_rate:.1f}%, dur_mean={dur_mean:.1f}")

results["exp4_score_magnitude"] = score_bucket_stats

# ── EXP5: Simple Discriminator Filters at Birth ──
log.info("=== EXP5: Simple Discriminator Filter Experiments ===")
filters = {
    "baseline (all births)": births,
    "score_abs>=3.5": births[births["score_abs"] >= 3.5],
    "score_abs>=4.0": births[births["score_abs"] >= 4.0],
    "score_abs>=5.0": births[births["score_abs"] >= 5.0],
    "high range_z (>=0.5)": births[births["range_z"] >= 0.5],
    "cascade at birth (liq_total_ratio>=2)": births[births["liq_total_ratio"] >= 2],
    "SHORT only": births[births["direction"] == -1],
    "SHORT + score_abs>=4.0": births[(births["direction"] == -1) & (births["score_abs"] >= 4.0)],
    "SHORT + score_abs>=4.0 + range_z>=0.5":
        births[(births["direction"] == -1) & (births["score_abs"] >= 4.0) & (births["range_z"] >= 0.5)],
    "LONG only": births[births["direction"] == 1],
    "LONG + score_abs>=4.0": births[(births["direction"] == 1) & (births["score_abs"] >= 4.0)],
}

filter_results = {}
for name, sub in filters.items():
    if len(sub) < 5:
        continue
    surv_rate = sub["survivor"].mean() * 100
    dur_mean = float(sub["duration"].mean())
    filter_results[name] = {
        "n": int(len(sub)),
        "surv_rate_pct": round(surv_rate, 1),
        "dur_mean": round(dur_mean, 2),
        "lift_pp": round(surv_rate - 100 * births["survivor"].mean(), 1),
    }
    mark = " <<" if surv_rate > 100 * births["survivor"].mean() + 5 else ""
    log.info(f"  {name:>45s}: n={len(sub):4d}, surv={surv_rate:5.1f}%, dur={dur_mean:.1f}{mark}")

results["exp5_discriminator_filters"] = filter_results

# ── Step 4: Run backtests and tag trades with birth_survivor flag ──
log.info("Running backtests + tagging trades by regime birth survival...")

# Build regime lookup: for every signal bar, associate with its regime's duration
# Regime start index per signal bar
regime_start_idx = np.full(len(oos_df), -1, dtype=int)
for i in range(len(oos_df)):
    if oos_df["signal_age"].iloc[i] == 0:
        regime_start_idx[i] = -1
    else:
        regime_start_idx[i] = i - (int(oos_df["signal_age"].iloc[i]) - 1)

oos_df["regime_start_idx"] = regime_start_idx
# Tag each signal bar with its regime's total duration + birth features
regime_duration_lookup = {}
for b in birth_records:
    regime_duration_lookup[b["ts"]] = {
        "duration": b["duration"],
        "survivor": b["survivor"],
        "score_birth": b["score_birth"],
        "score_abs_birth": b["score_abs"],
        "range_z_birth": b["range_z"],
    }

# Build bar-level lookup via regime_start_ts
oos_df["regime_start_ts"] = oos_df.apply(
    lambda r: oos_df["ts"].iloc[int(r["regime_start_idx"])] if r["regime_start_idx"] >= 0 else None,
    axis=1
)

# Build series of signal_age by ts for later trade tagging
age_lookup = pd.Series(score_df["signal_age"].values, index=score_df["ts"].values)
start_ts_map = oos_df.set_index("ts")["regime_start_ts"].to_dict()

all_trades = []
for symbol in COINS:
    coin = symbol.replace("USDT", "")
    ohlcv = bt.fetch_binance_15m(symbol, years=3)
    if "date_time" in ohlcv.columns:
        ohlcv = ohlcv.rename(columns={"date_time": "ts"})
    alt_df = bt.build_alt_technicals(ohlcv)
    oos = (alt_df["ts"] >= OOS_START) & (alt_df["ts"] <= OOS_END)

    cfg = COIN_CONFIGS.get(coin, {})
    btc_score_ts = pd.Series(score_df["btc_score"].values, index=score_df["ts"].values)
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
        entry_ages = []
        birth_scores = []
        birth_rangez = []
        regime_durations = []
        regime_survivors = []
        for _, t in trades.iterrows():
            et = pd.Timestamp(t["entry_time"])
            idx = age_lookup.index.searchsorted(et, side="right") - 1
            if 0 <= idx < len(age_lookup):
                entry_ages.append(int(age_lookup.iloc[idx]))
                tst = age_lookup.index[idx]
                start_ts = start_ts_map.get(tst)
                if start_ts is not None and start_ts in regime_duration_lookup:
                    rd = regime_duration_lookup[start_ts]
                    birth_scores.append(rd["score_abs_birth"])
                    birth_rangez.append(rd["range_z_birth"])
                    regime_durations.append(rd["duration"])
                    regime_survivors.append(rd["survivor"])
                else:
                    birth_scores.append(np.nan)
                    birth_rangez.append(np.nan)
                    regime_durations.append(np.nan)
                    regime_survivors.append(False)
            else:
                entry_ages.append(0)
                birth_scores.append(np.nan)
                birth_rangez.append(np.nan)
                regime_durations.append(np.nan)
                regime_survivors.append(False)
        trades["signal_age"] = entry_ages
        trades["birth_score_abs"] = birth_scores
        trades["birth_range_z"] = birth_rangez
        trades["regime_duration"] = regime_durations
        trades["regime_survivor"] = regime_survivors
        all_trades.append(trades)

trades_df = pd.concat(all_trades, ignore_index=True)
log.info(f"Total trades: {len(trades_df)}")
baseline_pnl = float(trades_df["pnl_net"].sum())
baseline_wr = float((trades_df["pnl_net"] > 0).mean() * 100)
log.info(f"Baseline: n={len(trades_df)}, PnL=${baseline_pnl:.0f}, WR={baseline_wr:.1f}%")

# ── EXP6: Compare fresh (age=1) trades, survivor regime vs short-lived regime ──
log.info("=== EXP6: Fresh Trades in Survivor vs Short-Lived Regimes ===")
fresh = trades_df[trades_df["signal_age"] == 1]
fresh_surv = fresh[fresh["regime_survivor"] == True]
fresh_short = fresh[fresh["regime_survivor"] == False]

def summary(df):
    if len(df) == 0:
        return {"n": 0, "wr": 0, "pnl": 0, "avg": 0}
    return {
        "n": int(len(df)),
        "wr": round((df["pnl_net"] > 0).mean() * 100, 1),
        "pnl": round(float(df["pnl_net"].sum()), 2),
        "avg": round(float(df["pnl_net"].mean()), 2),
    }

results["exp6_fresh_by_survival"] = {
    "fresh_all": summary(fresh),
    "fresh_in_survivor_regime": summary(fresh_surv),
    "fresh_in_short_lived_regime": summary(fresh_short),
}
log.info(f"  Fresh ALL:               {summary(fresh)}")
log.info(f"  Fresh (survivor regime): {summary(fresh_surv)}")
log.info(f"  Fresh (short-lived):     {summary(fresh_short)}")

# ── EXP7: Can birth_score_abs rescue age=1 trades? ──
log.info("=== EXP7: Birth Score Magnitude Filter on age=1 Trades ===")
fresh_filters = {}
for thr in [3.0, 3.5, 4.0, 5.0, 6.0]:
    sub = fresh[fresh["birth_score_abs"] >= thr]
    fresh_filters[f"birth_score>={thr}"] = summary(sub)
    log.info(f"  birth|score|>={thr}: {summary(sub)}")
results["exp7_birth_score_filter_on_fresh"] = fresh_filters

# ── EXP8: Can birth_range_z rescue age=1? ──
log.info("=== EXP8: Birth Range_Z Filter on age=1 Trades ===")
rangez_filters = {}
for thr in [-0.5, 0.0, 0.5, 1.0, 2.0]:
    sub = fresh[fresh["birth_range_z"] >= thr]
    rangez_filters[f"birth_range_z>={thr}"] = summary(sub)
    log.info(f"  birth range_z>={thr}: {summary(sub)}")
results["exp8_birth_range_z_filter_on_fresh"] = rangez_filters

# ── EXP9: Combined filter on age=1 trades ──
log.info("=== EXP9: Combined Filters on age=1 ===")
combos = {
    "SHORT + birth_score>=4.0":
        fresh[(fresh["dir"] == "S") & (fresh["birth_score_abs"] >= 4.0)],
    "SHORT + birth_score>=5.0":
        fresh[(fresh["dir"] == "S") & (fresh["birth_score_abs"] >= 5.0)],
    "LONG + birth_score>=4.0":
        fresh[(fresh["dir"] == "L") & (fresh["birth_score_abs"] >= 4.0)],
    "LONG + birth_score>=5.0":
        fresh[(fresh["dir"] == "L") & (fresh["birth_score_abs"] >= 5.0)],
    "regime_survivor==True (hindsight)":
        fresh[fresh["regime_survivor"] == True],
}
combo_results = {}
for name, sub in combos.items():
    combo_results[name] = summary(sub)
    log.info(f"  {name:>45s}: {summary(sub)}")
results["exp9_combined_fresh_filters"] = combo_results

# ── EXP10: Does minimum signal_age filter interact with birth score? ──
log.info("=== EXP10: Portfolio PnL under alternative age/birth filters ===")
strategy_results = {}
strategies = {
    "baseline (all trades)": trades_df,
    "drop age=1 only (Mission 038 rule)": trades_df[trades_df["signal_age"] != 1],
    "drop age=1 except SHORT+birth>=4": trades_df[
        (trades_df["signal_age"] != 1)
        | ((trades_df["signal_age"] == 1) & (trades_df["dir"] == "S") & (trades_df["birth_score_abs"] >= 4))
    ],
    "drop age=1 except birth_score>=6": trades_df[
        (trades_df["signal_age"] != 1)
        | ((trades_df["signal_age"] == 1) & (trades_df["birth_score_abs"] >= 6))
    ],
    "drop age=1 except regime_survivor (hindsight ceiling)": trades_df[
        (trades_df["signal_age"] != 1)
        | ((trades_df["signal_age"] == 1) & (trades_df["regime_survivor"] == True))
    ],
    "aggressive: skip age<=1 (drops both age=0 and age=1)":
        trades_df[trades_df["signal_age"] >= 2],
}
for name, sub in strategies.items():
    s = summary(sub)
    delta = s["pnl"] - baseline_pnl
    s["delta_vs_baseline"] = round(delta, 2)
    strategy_results[name] = s
    log.info(f"  {name:>55s}: n={s['n']:4d}, WR={s['wr']:.1f}%, "
             f"PnL={s['pnl']:+,.0f} (Δ={delta:+,.0f})")
results["exp10_portfolio_strategies"] = strategy_results

# ── Save Mission Result ──
log.info("Saving results...")
mission_data = {
    "mission_id": "mission_039_regime_birth_survival",
    "date": datetime.utcnow().strftime("%Y-%m-%d"),
    "type": "signal_quality / regime_analysis",
    "title": "Regime Birth Survival Predictor — กู้ age=1 trades ได้ไหม?",
    "hypothesis": "สภาพ factors/volatility ตอน regime birth ทำนายได้ว่าจะ survive >=8 bars",
    "coins": [c.replace("USDT", "") for c in COINS],
    "oos_period": f"{OOS_START} to {OOS_END}",
    "total_trades": int(len(trades_df)),
    "baseline_pnl": round(baseline_pnl, 2),
    "baseline_wr": round(baseline_wr, 1),
    "survivor_threshold_bars": SURVIVOR_THRESHOLD,
    "results": results,
}

out_json = BASE_DIR / "missions" / "mission_039_regime_birth_survival.json"
with open(out_json, "w", encoding="utf-8") as f:
    json.dump(mission_data, f, indent=2, ensure_ascii=False, default=str)
log.info(f"Saved: {out_json}")

# ── Console Summary ──
print("\n" + "=" * 70)
print("  MISSION 039: Regime Birth Survival Predictor")
print("=" * 70)
print(f"  Total trades: {len(trades_df)} | Baseline PnL: ${baseline_pnl:,.0f} | WR: {baseline_wr:.1f}%")
print(f"  Regime births: {results['exp1_birth_catalog']['total_births']} "
      f"| Survivors: {results['exp1_birth_catalog']['survivors']} "
      f"({results['exp1_birth_catalog']['survivor_rate_pct']}%)")
print()
print("  Top discriminators (|Cohen's d|):")
disc = sorted(results["exp2_survivor_profile"].items(),
              key=lambda x: abs(x[1]["cohen_d"]), reverse=True)[:5]
for name, p in disc:
    print(f"    {name:>18s}: surv={p['surv_mean']:+.3f} vs short={p['short_mean']:+.3f} "
          f"(d={p['cohen_d']:+.2f})")
print()
print("  Fresh (age=1) trade performance by regime survival:")
for name, s in results["exp6_fresh_by_survival"].items():
    print(f"    {name:>30s}: n={s['n']:4d}, WR={s['wr']:.1f}%, PnL=${s['pnl']:+,.0f}")
print()
print("  Portfolio strategy comparison:")
for name, s in results["exp10_portfolio_strategies"].items():
    print(f"    {name:>55s}: PnL=${s['pnl']:+,.0f} (delta=${s['delta_vs_baseline']:+,.0f})")
print("=" * 70)
