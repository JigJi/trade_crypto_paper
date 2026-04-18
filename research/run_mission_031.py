"""
Mission 031: BTC Score Velocity & Flip Prediction
===================================================
สมมติฐาน: เมื่อ BTC composite score เปลี่ยนเร็ว (velocity สูง)
→ signal ไม่มั่นคง → SIGNAL_FLIP probability สูงขึ้น
→ ควรหลีกเลี่ยง entry ในช่วง high velocity

ต่อยอด: Mission 003 (score magnitude), 021 (chop detection), 023 (factor conflict)
มุมใหม่: วัด rate of change ของ composite score เอง ไม่ใช่ price

6 Experiments:
  EXP1: Score velocity distribution & basic stats
  EXP2: Velocity at entry → SIGNAL_FLIP correlation
  EXP3: Velocity quartile → trade outcome
  EXP4: Score acceleration (2nd derivative) analysis
  EXP5: Velocity filter backtest (skip high velocity entries)
  EXP6: Combined velocity + magnitude filter
"""

import sys, json, logging, warnings
from pathlib import Path
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import backtest_15m_btc_led_alts as bt
from paper_trading.config import COMPOSITE_WEIGHTS, V3_EXTRA_WEIGHTS, COIN_CONFIGS
from signal_core import (
    score_ob_combined, score_basis_contrarian, score_tick_liq,
    compute_btc_composite_score,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════
# Load data
# ══════════════════════════════════════════════════════════════
log.info("Loading BTC OHLCV + DB data...")
btc_ohlcv = bt.fetch_binance_15m("BTCUSDT", years=3)
if "date_time" in btc_ohlcv.columns:
    btc_ohlcv = btc_ohlcv.rename(columns={"date_time": "ts"})

db_data = bt.load_btc_db_data()
btc_df = bt.build_btc_features(btc_ohlcv, db_data)

btc_score = compute_btc_composite_score(btc_df, COMPOSITE_WEIGHTS, V3_EXTRA_WEIGHTS)
btc_score_ts = pd.Series(btc_score.values, index=btc_df["ts"].values, name="btc_score")

# ══════════════════════════════════════════════════════════════
# Compute velocity & acceleration
# ══════════════════════════════════════════════════════════════
log.info("Computing score velocity & acceleration...")
score_s = pd.Series(btc_score.values, index=btc_df["ts"].values)

# Velocity = change per bar (Δscore / Δbar)
velocity_1 = score_s.diff(1)       # 1-bar change (15 min)
velocity_4 = score_s.diff(4)       # 4-bar change (1 hour)
velocity_8 = score_s.diff(8)       # 8-bar change (2 hours)

# Rolling velocity (smoothed)
velocity_4_smooth = score_s.diff(1).rolling(4).mean()

# Acceleration = change of velocity
accel_1 = velocity_1.diff(1)

# Absolute velocity (magnitude of change)
abs_vel_4 = velocity_4.abs()

# Create velocity DataFrame
vel_df = pd.DataFrame({
    "ts": btc_df["ts"].values,
    "score": score_s.values,
    "vel_1": velocity_1.values,
    "vel_4": velocity_4.values,
    "vel_8": velocity_8.values,
    "vel_4_smooth": velocity_4_smooth.values,
    "accel": accel_1.values,
    "abs_vel_4": abs_vel_4.values,
})

# ══════════════════════════════════════════════════════════════
# Run backtest & annotate trades with velocity
# ══════════════════════════════════════════════════════════════
log.info("Running backtest for 6 coins (OOS)...")
coins = ["BTCUSDT", "XRPUSDT", "ADAUSDT", "DOTUSDT", "SUIUSDT", "FILUSDT"]
oos_start, oos_end = "2025-01-01", "2026-03-31"

all_trades = []
for symbol in coins:
    coin = symbol.replace("USDT", "")
    ohlcv = bt.fetch_binance_15m(symbol, years=3)
    if "date_time" in ohlcv.columns:
        ohlcv = ohlcv.rename(columns={"date_time": "ts"})
    alt_df = bt.build_alt_technicals(ohlcv)
    oos_mask = (alt_df["ts"] >= oos_start) & (alt_df["ts"] <= oos_end)

    cfg = COIN_CONFIGS.get(coin, {})
    signals, alt_merged = bt.generate_btc_led_signal(
        btc_score_ts, alt_df[oos_mask],
        threshold=cfg.get("threshold", 3.0),
        use_alt_pa_filter=cfg.get("use_alt_pa_filter", False),
        hysteresis_band=cfg.get("hysteresis_band", 0.0))
    trades = bt.run_backtest(alt_merged, signals,
                             sl_atr_mult=cfg.get("sl_atr_mult", 2.5),
                             tp_atr_mult=cfg.get("tp_atr_mult", 4.0),
                             cooldown_bars=cfg.get("cooldown_bars", 4),
                             flip_cd_extra=cfg.get("flip_cd_extra", 0))
    if len(trades) > 0:
        trades["coin"] = coin
        all_trades.append(trades)

trades_df = pd.concat(all_trades, ignore_index=True)
trades_df["entry_time"] = pd.to_datetime(trades_df["entry_time"])
log.info(f"Total trades: {len(trades_df)}")

# Annotate trades with velocity at entry
vel_lookup = vel_df.set_index("ts")
def get_vel_at_entry(entry_time):
    """Find velocity closest to entry time (backward)."""
    mask = vel_lookup.index <= entry_time
    if mask.any():
        row = vel_lookup.loc[mask].iloc[-1]
        return row
    return pd.Series({c: np.nan for c in vel_lookup.columns})

log.info("Annotating trades with velocity at entry...")
vel_at_entry = []
for _, trade in trades_df.iterrows():
    v = get_vel_at_entry(trade["entry_time"])
    vel_at_entry.append(v)
vel_entry_df = pd.DataFrame(vel_at_entry)
for col in ["score", "vel_1", "vel_4", "vel_8", "vel_4_smooth", "accel", "abs_vel_4"]:
    trades_df[f"entry_{col}"] = vel_entry_df[col].values

results = {}

# ══════════════════════════════════════════════════════════════
# EXP1: Score velocity distribution
# ══════════════════════════════════════════════════════════════
log.info("=== EXP1: Score Velocity Distribution ===")
oos_vel = vel_df[(vel_df["ts"] >= oos_start) & (vel_df["ts"] <= oos_end)].copy()
exp1 = {
    "total_bars": len(oos_vel),
    "vel_1_mean": round(float(oos_vel["vel_1"].mean()), 4),
    "vel_1_std": round(float(oos_vel["vel_1"].std()), 4),
    "vel_4_mean": round(float(oos_vel["vel_4"].mean()), 4),
    "vel_4_std": round(float(oos_vel["vel_4"].std()), 4),
    "abs_vel_4_median": round(float(oos_vel["abs_vel_4"].median()), 4),
    "abs_vel_4_p75": round(float(oos_vel["abs_vel_4"].quantile(0.75)), 4),
    "abs_vel_4_p90": round(float(oos_vel["abs_vel_4"].quantile(0.90)), 4),
    "abs_vel_4_p95": round(float(oos_vel["abs_vel_4"].quantile(0.95)), 4),
    "score_mean": round(float(oos_vel["score"].mean()), 4),
    "score_std": round(float(oos_vel["score"].std()), 4),
    "pct_score_positive": round(float((oos_vel["score"] > 0).mean() * 100), 1),
    "pct_score_negative": round(float((oos_vel["score"] < 0).mean() * 100), 1),
    "pct_score_zero": round(float((oos_vel["score"] == 0).mean() * 100), 1),
}
results["exp1_velocity_distribution"] = exp1
log.info(f"  vel_4 std: {exp1['vel_4_std']}, abs_vel_4 median: {exp1['abs_vel_4_median']}")
log.info(f"  Score polarity: +{exp1['pct_score_positive']}% / -{exp1['pct_score_negative']}% / 0:{exp1['pct_score_zero']}%")

# ══════════════════════════════════════════════════════════════
# EXP2: Velocity at entry → SIGNAL_FLIP correlation
# ══════════════════════════════════════════════════════════════
log.info("=== EXP2: Velocity → SIGNAL_FLIP Correlation ===")
trades_df["is_flip"] = (trades_df["exit_reason"] == "SIGNAL_FLIP").astype(int)
trades_df["is_win"] = (trades_df["pnl_net"] > 0).astype(int)

# Direction-aware velocity: positive vel for matching direction, negative for opposing
trades_df["dir_vel_4"] = np.where(
    trades_df["dir"] == "L",
    trades_df["entry_vel_4"],
    -trades_df["entry_vel_4"]
)

flip_trades = trades_df[trades_df["is_flip"] == 1]
non_flip = trades_df[trades_df["is_flip"] == 0]

exp2 = {
    "flip_pct": round(float(trades_df["is_flip"].mean() * 100), 1),
    "flip_abs_vel4_mean": round(float(flip_trades["entry_abs_vel_4"].mean()), 4),
    "nonflip_abs_vel4_mean": round(float(non_flip["entry_abs_vel_4"].mean()), 4),
    "flip_vel4_mean": round(float(flip_trades["entry_vel_4"].mean()), 4),
    "nonflip_vel4_mean": round(float(non_flip["entry_vel_4"].mean()), 4),
    "flip_dir_vel4_mean": round(float(flip_trades["dir_vel_4"].mean()), 4) if len(flip_trades) else 0,
    "nonflip_dir_vel4_mean": round(float(non_flip["dir_vel_4"].mean()), 4) if len(non_flip) else 0,
    "flip_accel_mean": round(float(flip_trades["entry_accel"].mean()), 4),
    "nonflip_accel_mean": round(float(non_flip["entry_accel"].mean()), 4),
    "correlation_abs_vel4_vs_flip": round(float(
        trades_df[["entry_abs_vel_4", "is_flip"]].dropna().corr().iloc[0, 1]), 4),
    "correlation_abs_vel4_vs_win": round(float(
        trades_df[["entry_abs_vel_4", "is_win"]].dropna().corr().iloc[0, 1]), 4),
    "correlation_dir_vel4_vs_flip": round(float(
        trades_df[["dir_vel_4", "is_flip"]].dropna().corr().iloc[0, 1]), 4),
}
results["exp2_velocity_flip_correlation"] = exp2
log.info(f"  FLIP trades: {exp2['flip_pct']}%")
log.info(f"  Abs vel4 mean: FLIP={exp2['flip_abs_vel4_mean']} vs NON-FLIP={exp2['nonflip_abs_vel4_mean']}")
log.info(f"  Dir vel4 mean: FLIP={exp2['flip_dir_vel4_mean']} vs NON-FLIP={exp2['nonflip_dir_vel4_mean']}")
log.info(f"  Corr abs_vel4 vs flip: {exp2['correlation_abs_vel4_vs_flip']}")

# ══════════════════════════════════════════════════════════════
# EXP3: Velocity quartile → trade outcome
# ══════════════════════════════════════════════════════════════
log.info("=== EXP3: Velocity Quartile → Trade Outcome ===")
trades_clean = trades_df.dropna(subset=["entry_abs_vel_4"]).copy()
trades_clean["vel_quartile"] = pd.qcut(trades_clean["entry_abs_vel_4"], 4, labels=["Q1_low", "Q2", "Q3", "Q4_high"])

exp3_quartiles = {}
for q in ["Q1_low", "Q2", "Q3", "Q4_high"]:
    qt = trades_clean[trades_clean["vel_quartile"] == q]
    exp3_quartiles[q] = {
        "n_trades": int(len(qt)),
        "pnl": round(float(qt["pnl_net"].sum()), 2),
        "avg_pnl": round(float(qt["pnl_net"].mean()), 4),
        "win_rate": round(float(qt["is_win"].mean() * 100), 1),
        "flip_pct": round(float(qt["is_flip"].mean() * 100), 1),
        "avg_bars_held": round(float(qt["holding_bars"].mean()), 1),
    }
    log.info(f"  {q}: {exp3_quartiles[q]['n_trades']} trades, "
             f"WR={exp3_quartiles[q]['win_rate']}%, "
             f"PnL=${exp3_quartiles[q]['pnl']}, "
             f"FLIP={exp3_quartiles[q]['flip_pct']}%")

# Also by direction-aware velocity
trades_clean["dir_vel_quartile"] = pd.qcut(trades_clean["dir_vel_4"], 4,
                                            labels=["Q1_weakening", "Q2", "Q3", "Q4_strengthening"],
                                            duplicates="drop")

exp3_dir = {}
for q in ["Q1_weakening", "Q2", "Q3", "Q4_strengthening"]:
    qt = trades_clean[trades_clean["dir_vel_quartile"] == q]
    if len(qt) == 0:
        continue
    exp3_dir[q] = {
        "n_trades": int(len(qt)),
        "pnl": round(float(qt["pnl_net"].sum()), 2),
        "win_rate": round(float(qt["is_win"].mean() * 100), 1),
        "flip_pct": round(float(qt["is_flip"].mean() * 100), 1),
    }
    log.info(f"  DirVel {q}: WR={exp3_dir[q]['win_rate']}%, FLIP={exp3_dir[q]['flip_pct']}%")

results["exp3_velocity_quartiles"] = {"abs_velocity": exp3_quartiles, "dir_velocity": exp3_dir}

# ══════════════════════════════════════════════════════════════
# EXP4: Acceleration analysis
# ══════════════════════════════════════════════════════════════
log.info("=== EXP4: Score Acceleration Analysis ===")
trades_clean["accel_sign"] = np.sign(trades_clean["entry_accel"])
# Acceleration direction: is score accelerating toward or away from signal?
# For LONG: positive accel = accelerating upward (good)
# For SHORT: negative accel = accelerating downward (good)
trades_clean["dir_accel"] = np.where(
    trades_clean["dir"] == "L",
    trades_clean["entry_accel"],
    -trades_clean["entry_accel"]
)
trades_clean["favorable_accel"] = (trades_clean["dir_accel"] > 0).astype(int)

fav = trades_clean[trades_clean["favorable_accel"] == 1]
unfav = trades_clean[trades_clean["favorable_accel"] == 0]

exp4 = {
    "favorable_accel_pct": round(float(trades_clean["favorable_accel"].mean() * 100), 1),
    "favorable_wr": round(float(fav["is_win"].mean() * 100), 1) if len(fav) else 0,
    "unfavorable_wr": round(float(unfav["is_win"].mean() * 100), 1) if len(unfav) else 0,
    "favorable_pnl": round(float(fav["pnl_net"].sum()), 2) if len(fav) else 0,
    "unfavorable_pnl": round(float(unfav["pnl_net"].sum()), 2) if len(unfav) else 0,
    "favorable_flip_pct": round(float(fav["is_flip"].mean() * 100), 1) if len(fav) else 0,
    "unfavorable_flip_pct": round(float(unfav["is_flip"].mean() * 100), 1) if len(unfav) else 0,
    "favorable_avg_pnl": round(float(fav["pnl_net"].mean()), 4) if len(fav) else 0,
    "unfavorable_avg_pnl": round(float(unfav["pnl_net"].mean()), 4) if len(unfav) else 0,
}
results["exp4_acceleration"] = exp4
log.info(f"  Favorable accel: {exp4['favorable_accel_pct']}% of trades")
log.info(f"  WR: favorable={exp4['favorable_wr']}% vs unfavorable={exp4['unfavorable_wr']}%")
log.info(f"  PnL: favorable=${exp4['favorable_pnl']} vs unfavorable=${exp4['unfavorable_pnl']}")
log.info(f"  FLIP: favorable={exp4['favorable_flip_pct']}% vs unfavorable={exp4['unfavorable_flip_pct']}%")

# ══════════════════════════════════════════════════════════════
# EXP5: Velocity filter backtest
# ══════════════════════════════════════════════════════════════
log.info("=== EXP5: Velocity Filter Backtest ===")

# Test: skip entries when abs_vel_4 > threshold
# Use percentiles as thresholds
vel_thresholds = {
    "no_filter": 999,
    "p90": float(oos_vel["abs_vel_4"].quantile(0.90)),
    "p75": float(oos_vel["abs_vel_4"].quantile(0.75)),
    "p50": float(oos_vel["abs_vel_4"].quantile(0.50)),
}

exp5 = {}
for name, vel_thr in vel_thresholds.items():
    filtered_trades = trades_clean[trades_clean["entry_abs_vel_4"] <= vel_thr]
    n = len(filtered_trades)
    if n == 0:
        continue
    pnl = float(filtered_trades["pnl_net"].sum())
    wr = float(filtered_trades["is_win"].mean() * 100)
    flip = float(filtered_trades["is_flip"].mean() * 100)
    avg = float(filtered_trades["pnl_net"].mean())

    exp5[name] = {
        "vel_threshold": round(vel_thr, 4),
        "n_trades": n,
        "pnl": round(pnl, 2),
        "avg_pnl": round(avg, 4),
        "win_rate": round(wr, 1),
        "flip_pct": round(flip, 1),
        "trades_kept_pct": round(n / len(trades_clean) * 100, 1),
    }
    log.info(f"  {name} (vel<={vel_thr:.3f}): {n} trades ({exp5[name]['trades_kept_pct']}%), "
             f"WR={wr:.1f}%, PnL=${pnl:.0f}, FLIP={flip:.1f}%")

results["exp5_velocity_filter"] = exp5

# ══════════════════════════════════════════════════════════════
# EXP6: Combined velocity + magnitude filter
# ══════════════════════════════════════════════════════════════
log.info("=== EXP6: Combined Velocity + Magnitude Filter ===")

# Combine: only trade when abs_vel_4 is low AND score magnitude is high
# From Mission 003: score magnitude > 3 is good (threshold already at 3)
# New: add velocity constraint

combos = [
    ("baseline", 999, 0),
    ("vel_p75", float(oos_vel["abs_vel_4"].quantile(0.75)), 0),
    ("vel_p75_score4", float(oos_vel["abs_vel_4"].quantile(0.75)), 4),
    ("vel_p90_score4", float(oos_vel["abs_vel_4"].quantile(0.90)), 4),
    ("vel_p50_fav_accel", float(oos_vel["abs_vel_4"].quantile(0.50)), 0),
]

exp6 = {}
for name, vel_thr, score_thr in combos:
    mask = trades_clean["entry_abs_vel_4"] <= vel_thr
    if score_thr > 0:
        mask = mask & (trades_clean["entry_score"].abs() >= score_thr)
    if name == "vel_p50_fav_accel":
        mask = mask & (trades_clean["favorable_accel"] == 1)

    ft = trades_clean[mask]
    n = len(ft)
    if n == 0:
        continue
    pnl = float(ft["pnl_net"].sum())
    wr = float(ft["is_win"].mean() * 100)
    flip = float(ft["is_flip"].mean() * 100)
    avg = float(ft["pnl_net"].mean())

    # Per-direction
    shorts = ft[ft["dir"] == "S"]
    longs = ft[ft["dir"] == "L"]
    s_wr = float(shorts["is_win"].mean() * 100) if len(shorts) > 0 else 0
    l_wr = float(longs["is_win"].mean() * 100) if len(longs) > 0 else 0

    exp6[name] = {
        "vel_threshold": round(vel_thr, 4),
        "score_threshold": score_thr,
        "n_trades": n,
        "pnl": round(pnl, 2),
        "avg_pnl": round(avg, 4),
        "win_rate": round(wr, 1),
        "flip_pct": round(flip, 1),
        "wr_short": round(s_wr, 1),
        "wr_long": round(l_wr, 1),
        "trades_kept_pct": round(n / len(trades_clean) * 100, 1),
    }
    log.info(f"  {name}: {n} trades ({exp6[name]['trades_kept_pct']}%), "
             f"WR={wr:.1f}%, PnL=${pnl:.0f}, FLIP={flip:.1f}%")

results["exp6_combined_filter"] = exp6

# ══════════════════════════════════════════════════════════════
# Summary
# ══════════════════════════════════════════════════════════════
baseline_pnl = exp5.get("no_filter", {}).get("pnl", 0)
best_filter = max(exp5.items(), key=lambda x: x[1]["pnl"] if x[0] != "no_filter" else -9999)
best_combo = max(exp6.items(), key=lambda x: x[1]["avg_pnl"] if x[0] != "baseline" else -9999)

summary = {
    "mission": "mission_031_score_velocity",
    "date": datetime.utcnow().strftime("%Y-%m-%d"),
    "baseline_pnl": baseline_pnl,
    "baseline_trades": exp5.get("no_filter", {}).get("n_trades", 0),
    "best_vel_filter": best_filter[0],
    "best_vel_filter_pnl": best_filter[1]["pnl"],
    "best_vel_filter_delta": round(best_filter[1]["pnl"] - baseline_pnl, 2),
    "best_combo": best_combo[0],
    "best_combo_pnl": best_combo[1]["pnl"],
    "best_combo_avg_pnl": best_combo[1]["avg_pnl"],
    "vel4_flip_corr": exp2["correlation_abs_vel4_vs_flip"],
    "vel4_win_corr": exp2["correlation_abs_vel4_vs_win"],
    "favorable_accel_wr_delta": round(exp4["favorable_wr"] - exp4["unfavorable_wr"], 1),
}
results["summary"] = summary

# ══════════════════════════════════════════════════════════════
# Save
# ══════════════════════════════════════════════════════════════
out_dir = BASE_DIR / "missions"
out_dir.mkdir(exist_ok=True)

# JSON
json_path = out_dir / "mission_031_score_velocity.json"
with open(json_path, "w", encoding="utf-8") as f:
    json.dump(results, f, indent=2, ensure_ascii=False, default=str)
log.info(f"Saved JSON: {json_path}")

# Also save to experiments
exp_path = BASE_DIR / "experiments" / "mission_031_score_velocity.json"
with open(exp_path, "w", encoding="utf-8") as f:
    json.dump(results, f, indent=2, ensure_ascii=False, default=str)

log.info("=== Mission 031 Complete ===")
log.info(f"Baseline PnL: ${baseline_pnl:.0f}")
log.info(f"Best filter: {best_filter[0]} -> ${best_filter[1]['pnl']:.0f} (delta: ${summary['best_vel_filter_delta']:.0f})")
log.info(f"Best combo: {best_combo[0]} -> avg ${best_combo[1]['avg_pnl']:.4f}/trade")
log.info(f"Velocity-FLIP correlation: {exp2['correlation_abs_vel4_vs_flip']}")
log.info(f"Favorable accel WR delta: {summary['favorable_accel_wr_delta']}pp")

print("\n" + "="*60)
print(f"Mission 031: BTC Score Velocity & Flip Prediction")
print(f"="*60)
print(f"Baseline: {exp5.get('no_filter',{}).get('n_trades',0)} trades, ${baseline_pnl:.0f}")
print(f"Best velocity filter: {best_filter[0]} -> ${best_filter[1]['pnl']:.0f}")
print(f"Velocity-FLIP corr: {exp2['correlation_abs_vel4_vs_flip']}")
print(f"Dir velocity-FLIP corr: {exp2['correlation_dir_vel4_vs_flip']}")
print(f"Favorable accel: WR {exp4['favorable_wr']}% vs {exp4['unfavorable_wr']}% (delta: {summary['favorable_accel_wr_delta']}pp)")
