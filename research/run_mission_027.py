"""
Mission 027: Momentum Burst Anatomy
=====================================
M025 found TRAIL winners 80% close in 1-4 bars (15-60 min).
Question: What fingerprint identifies 1-bar winners BEFORE entry?

EXP1: Score magnitude at entry by duration bucket
EXP2: Volatility context (ATR level)
EXP3: Score momentum (delta before entry)
EXP4: Time-of-day for 1-bar winners
EXP5: Clustering (do 1-bar wins come in bursts?)
EXP6: Factor activity at entry
EXP7: Confidence-based sizing simulation
"""

import sys, json, logging
import numpy as np, pandas as pd
from pathlib import Path
from datetime import datetime

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import backtest_15m_btc_led_alts as bt
from paper_trading.config import COIN_CONFIGS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

log.info("Loading BTC OHLCV + DB data...")
btc_ohlcv = bt.fetch_binance_15m("BTCUSDT", years=3)
if "date_time" in btc_ohlcv.columns:
    btc_ohlcv = btc_ohlcv.rename(columns={"date_time": "ts"})

db_data = bt.load_btc_db_data()
btc_df = bt.build_btc_features(btc_ohlcv, db_data)
btc_score = bt.compute_btc_composite_score(btc_df)
btc_score_ts = pd.Series(btc_score.values, index=btc_df["ts"].values, name="btc_score")
btc_df["btc_score"] = btc_score.values

coins = ["BTCUSDT", "XRPUSDT", "ADAUSDT", "DOTUSDT", "SUIUSDT", "FILUSDT"]
oos_start, oos_end = "2025-01-01", "2026-03-31"

log.info("Running backtest for 6 coins (OOS)...")
all_trades = []
all_alt_dfs = {}

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
        use_alt_pa_filter=cfg.get("use_alt_pa_filter", False))
    trades = bt.run_backtest(alt_merged, signals,
                             sl_atr_mult=cfg.get("sl_atr_mult", 2.5),
                             tp_atr_mult=cfg.get("tp_atr_mult", 4.0),
                             cooldown_bars=cfg.get("cooldown_bars", 4))
    if len(trades) > 0:
        trades["coin"] = coin
        all_trades.append(trades)
    all_alt_dfs[coin] = alt_merged

trades_df = pd.concat(all_trades, ignore_index=True)
trades_df["entry_time"] = pd.to_datetime(trades_df["entry_time"])
trades_df["exit_time"] = pd.to_datetime(trades_df["exit_time"])
log.info(f"Total trades: {len(trades_df)}")

# Enrich trades with BTC score at entry
btc_score_lookup = btc_df.set_index("ts")["btc_score"]

def get_btc_score_at(entry_time):
    idx = btc_score_lookup.index.searchsorted(entry_time, side="right") - 1
    if 0 <= idx < len(btc_score_lookup):
        return btc_score_lookup.iloc[idx]
    return np.nan

trades_df["btc_score_entry"] = trades_df["entry_time"].apply(get_btc_score_at)
trades_df["abs_score"] = trades_df["btc_score_entry"].abs()

btc_df_indexed = btc_df.set_index("ts")
btc_df_indexed["score_delta_1"] = btc_df_indexed["btc_score"].diff(1)
btc_df_indexed["score_delta_4"] = btc_df_indexed["btc_score"].diff(4)

def get_score_delta(entry_time, col):
    idx = btc_df_indexed.index.searchsorted(entry_time, side="right") - 1
    if 0 <= idx < len(btc_df_indexed):
        return btc_df_indexed[col].iloc[idx]
    return np.nan

trades_df["score_delta_1"] = trades_df["entry_time"].apply(
    lambda t: get_score_delta(t, "score_delta_1"))
trades_df["score_delta_4"] = trades_df["entry_time"].apply(
    lambda t: get_score_delta(t, "score_delta_4"))

def get_atr_context(row):
    coin = row["coin"]
    entry_time = row["entry_time"]
    if coin in all_alt_dfs:
        adf = all_alt_dfs[coin]
        idx = adf["ts"].searchsorted(entry_time, side="right") - 1
        if 0 <= idx < len(adf):
            atr = adf["atr"].iloc[idx]
            close = adf["close"].iloc[idx]
            return atr / close * 100 if close > 0 else np.nan
    return np.nan

trades_df["atr_pct"] = trades_df.apply(get_atr_context, axis=1)

trades_df["is_winner"] = trades_df["pnl_net"] > 0
trades_df["is_1bar"] = trades_df["holding_bars"] == 1
trades_df["dur_bucket"] = pd.cut(trades_df["holding_bars"],
                                  bins=[0, 1, 4, 8, 16, 200],
                                  labels=["1bar", "2-4bar", "5-8bar", "9-16bar", "17+bar"])
trades_df["hour_utc"] = trades_df["entry_time"].dt.hour

# ============================================================
# EXP1: Score Magnitude by duration bucket
# ============================================================
log.info("=== EXP1: Score Magnitude ===")
exp1_results = {}
for bucket in ["1bar", "2-4bar", "5-8bar", "9-16bar", "17+bar"]:
    mask = trades_df["dur_bucket"] == bucket
    subset = trades_df[mask]
    winners = subset[subset["is_winner"]]
    losers = subset[~subset["is_winner"]]
    exp1_results[bucket] = {
        "trades": int(len(subset)),
        "wr": round(float(len(winners) / len(subset) * 100), 1) if len(subset) > 0 else 0,
        "pnl": round(float(subset["pnl_net"].sum()), 2),
        "avg_abs_score": round(float(subset["abs_score"].mean()), 2),
        "win_avg_score": round(float(winners["abs_score"].mean()), 2) if len(winners) > 0 else 0,
        "lose_avg_score": round(float(losers["abs_score"].mean()), 2) if len(losers) > 0 else 0,
        "avg_pnl": round(float(subset["pnl_net"].mean()), 2) if len(subset) > 0 else 0,
    }
    log.info(f"  {bucket}: {exp1_results[bucket]}")

# ============================================================
# EXP2: Volatility Context
# ============================================================
log.info("=== EXP2: Volatility Context ===")
atr_valid = trades_df.dropna(subset=["atr_pct"]).copy()
atr_q = atr_valid["atr_pct"].quantile([0.25, 0.5, 0.75])
atr_valid["vol_regime"] = pd.cut(atr_valid["atr_pct"],
                                  bins=[0, float(atr_q[0.25]), float(atr_q[0.5]),
                                        float(atr_q[0.75]), 100],
                                  labels=["low", "med_low", "med_high", "high"])
exp2_results = {}
for regime in ["low", "med_low", "med_high", "high"]:
    mask = atr_valid["vol_regime"] == regime
    subset = atr_valid[mask]
    one_bar = subset[subset["is_1bar"]]
    exp2_results[regime] = {
        "trades": int(len(subset)),
        "wr": round(float(subset["is_winner"].mean() * 100), 1) if len(subset) > 0 else 0,
        "pnl": round(float(subset["pnl_net"].sum()), 2),
        "pct_1bar": round(float(len(one_bar) / len(subset) * 100), 1) if len(subset) > 0 else 0,
        "1bar_wr": round(float(one_bar["is_winner"].mean() * 100), 1) if len(one_bar) > 0 else 0,
    }
    log.info(f"  {regime}: {exp2_results[regime]}")

# ============================================================
# EXP3: Score Momentum
# ============================================================
log.info("=== EXP3: Score Momentum ===")
trades_df["aligned_delta_1"] = np.where(
    trades_df["dir"] == "L", trades_df["score_delta_1"], -trades_df["score_delta_1"])
trades_df["aligned_delta_4"] = np.where(
    trades_df["dir"] == "L", trades_df["score_delta_4"], -trades_df["score_delta_4"])

exp3_results = {}
for bucket in ["1bar", "2-4bar", "5-8bar", "9-16bar", "17+bar"]:
    mask = trades_df["dur_bucket"] == bucket
    subset = trades_df[mask].dropna(subset=["aligned_delta_4"])
    winners = subset[subset["is_winner"]]
    losers = subset[~subset["is_winner"]]
    exp3_results[bucket] = {
        "trades": int(len(subset)),
        "avg_delta4_all": round(float(subset["aligned_delta_4"].mean()), 3) if len(subset) > 0 else 0,
        "avg_delta4_win": round(float(winners["aligned_delta_4"].mean()), 3) if len(winners) > 0 else 0,
        "avg_delta4_lose": round(float(losers["aligned_delta_4"].mean()), 3) if len(losers) > 0 else 0,
        "avg_delta1_all": round(float(subset["aligned_delta_1"].mean()), 3) if len(subset) > 0 else 0,
    }
    log.info(f"  {bucket}: {exp3_results[bucket]}")

# ============================================================
# EXP4: Time-of-Day
# ============================================================
log.info("=== EXP4: Time-of-Day ===")
exp4_results = {}
one_bar_trades = trades_df[trades_df["is_1bar"]]
for h in range(24):
    s1 = one_bar_trades[one_bar_trades["hour_utc"] == h]
    all_h = trades_df[trades_df["hour_utc"] == h]
    exp4_results[str(h)] = {
        "1bar_count": int(len(s1)),
        "1bar_wr": round(float(s1["is_winner"].mean() * 100), 1) if len(s1) > 0 else 0,
        "1bar_pnl": round(float(s1["pnl_net"].sum()), 2),
        "total_at_hour": int(len(all_h)),
        "pct_1bar": round(float(len(s1) / len(all_h) * 100), 1) if len(all_h) > 0 else 0,
    }
sorted_hours = sorted(exp4_results.items(), key=lambda x: x[1]["1bar_pnl"], reverse=True)
best3 = [(h, d["1bar_pnl"], d["1bar_wr"]) for h, d in sorted_hours[:3]]
worst3 = [(h, d["1bar_pnl"], d["1bar_wr"]) for h, d in sorted_hours[-3:]]
log.info(f"  Best: {best3}")
log.info(f"  Worst: {worst3}")

# ============================================================
# EXP5: Clustering
# ============================================================
log.info("=== EXP5: Clustering ===")
obs = one_bar_trades.sort_values("entry_time").copy()
obs["time_gap_min"] = obs["entry_time"].diff().dt.total_seconds() / 60
burst_mask = obs["time_gap_min"] <= 120
non_burst = obs[~burst_mask | obs["time_gap_min"].isna()]
exp5_results = {
    "total_1bar": int(len(obs)),
    "in_burst": int(burst_mask.sum()),
    "pct_in_burst": round(float(burst_mask.sum() / len(obs) * 100), 1) if len(obs) > 0 else 0,
    "burst_wr": round(float(obs[burst_mask]["is_winner"].mean() * 100), 1) if burst_mask.sum() > 0 else 0,
    "isolated_wr": round(float(non_burst["is_winner"].mean() * 100), 1) if len(non_burst) > 0 else 0,
    "burst_avg_pnl": round(float(obs[burst_mask]["pnl_net"].mean()), 2) if burst_mask.sum() > 0 else 0,
    "isolated_avg_pnl": round(float(non_burst["pnl_net"].mean()), 2) if len(non_burst) > 0 else 0,
    "median_gap_min": round(float(obs["time_gap_min"].median()), 1) if len(obs) > 1 else 0,
}
log.info(f"  {exp5_results}")

# ============================================================
# EXP6: Factor Activity at Entry
# ============================================================
log.info("=== EXP6: Factor Activity ===")

def decompose_factors_at(entry_time):
    idx = btc_df_indexed.index.searchsorted(entry_time, side="right") - 1
    if idx < 0 or idx >= len(btc_df_indexed):
        return {}
    row = btc_df_indexed.iloc[idx]
    factors = {}
    if "oi_chg" in row.index:
        factors["oi_active"] = (abs(row.get("ret", 0) or 0) > 0.001 and
                                 abs(row.get("oi_chg", 0) or 0) > 0.002)
    if "fr_8h" in row.index:
        fr = row.get("fr_8h", 0) or 0
        factors["fr_active"] = fr < -0.0001 or fr > 0.0003
    if "whale_net_ma" in row.index:
        factors["whale_active"] = abs(row.get("whale_net_ma", 0) or 0) > 50_000_000
    if "liq_total" in row.index and "liq_total_ma" in row.index:
        lt = row.get("liq_total", 0) or 0
        lt_ma = row.get("liq_total_ma", 1) or 1
        factors["liq_cascade_active"] = lt > (lt_ma * 3)
    if "etf_flow_ma" in row.index:
        factors["etf_active"] = abs(row.get("etf_flow_ma", 0) or 0) > 50
    return factors

log.info("  Decomposing factors...")
s1w = trades_df[(trades_df["is_1bar"]) & (trades_df["is_winner"])].head(500)
s1l = trades_df[(trades_df["is_1bar"]) & (~trades_df["is_winner"])].head(500)
sm = trades_df[~trades_df["is_1bar"]].head(500)

def factor_activity_rate(sample):
    results = [decompose_factors_at(row["entry_time"]) for _, row in sample.iterrows()]
    if not results:
        return {}
    df_f = pd.DataFrame(results)
    return {col: round(float(df_f[col].mean() * 100), 1) for col in df_f.columns}

exp6_results = {
    "1bar_winner_factors": factor_activity_rate(s1w),
    "1bar_loser_factors": factor_activity_rate(s1l),
    "multi_bar_factors": factor_activity_rate(sm),
}
for k, v in exp6_results.items():
    log.info(f"  {k}: {v}")

# ============================================================
# EXP7: Confidence-Based Sizing
# ============================================================
log.info("=== EXP7: Confidence-Based Sizing ===")
score_q3 = trades_df["abs_score"].quantile(0.75)
trades_df["high_conf"] = (
    (trades_df["abs_score"] >= score_q3) &
    (trades_df["dir"] == "S") &
    (trades_df["aligned_delta_4"] > 0)
)
trades_df["sized_pnl"] = np.where(
    trades_df["high_conf"], trades_df["pnl_net"] * 2, trades_df["pnl_net"])

baseline_pnl = trades_df["pnl_net"].sum()
sized_pnl = trades_df["sized_pnl"].sum()
hc = trades_df[trades_df["high_conf"]]
lc = trades_df[~trades_df["high_conf"]]

exp7_results = {
    "baseline_pnl": round(float(baseline_pnl), 2),
    "sized_pnl": round(float(sized_pnl), 2),
    "delta_pnl": round(float(sized_pnl - baseline_pnl), 2),
    "delta_pct": round(float((sized_pnl - baseline_pnl) / abs(baseline_pnl) * 100), 1)
                 if baseline_pnl != 0 else 0,
    "high_conf_count": int(len(hc)),
    "high_conf_wr": round(float(hc["is_winner"].mean() * 100), 1) if len(hc) > 0 else 0,
    "high_conf_avg_pnl": round(float(hc["pnl_net"].mean()), 2) if len(hc) > 0 else 0,
    "low_conf_count": int(len(lc)),
    "low_conf_wr": round(float(lc["is_winner"].mean() * 100), 1) if len(lc) > 0 else 0,
    "low_conf_avg_pnl": round(float(lc["pnl_net"].mean()), 2) if len(lc) > 0 else 0,
    "score_q3": round(float(score_q3), 2),
}
sc = trades_df[(trades_df["abs_score"] >= score_q3) & (trades_df["dir"] == "S")]
exp7_results["simple_short_high_score"] = {
    "count": int(len(sc)),
    "wr": round(float(sc["is_winner"].mean() * 100), 1) if len(sc) > 0 else 0,
    "avg_pnl": round(float(sc["pnl_net"].mean()), 2) if len(sc) > 0 else 0,
    "total_pnl": round(float(sc["pnl_net"].sum()), 2),
}
log.info(f"  Baseline: ${baseline_pnl:,.0f}, Sized: ${sized_pnl:,.0f}")
log.info(f"  Delta: ${sized_pnl - baseline_pnl:,.0f} ({exp7_results['delta_pct']}%)")

# Direction breakdown for 1-bar
dir_results = {}
for d in ["L", "S"]:
    mask = (trades_df["is_1bar"]) & (trades_df["dir"] == d)
    subset = trades_df[mask]
    dir_results[d] = {
        "count": int(len(subset)),
        "wr": round(float(subset["is_winner"].mean() * 100), 1) if len(subset) > 0 else 0,
        "pnl": round(float(subset["pnl_net"].sum()), 2),
        "avg_pnl": round(float(subset["pnl_net"].mean()), 2) if len(subset) > 0 else 0,
    }
log.info(f"  1-bar L: {dir_results['L']}, S: {dir_results['S']}")

# SAVE
results = {
    "mission_id": "mission_027_momentum_burst",
    "date": datetime.utcnow().strftime("%Y-%m-%d"),
    "total_trades": int(len(trades_df)),
    "total_1bar": int(len(one_bar_trades)),
    "pct_1bar": round(float(len(one_bar_trades) / len(trades_df) * 100), 1),
    "exp1_score_magnitude": exp1_results,
    "exp2_volatility_context": exp2_results,
    "exp3_score_momentum": exp3_results,
    "exp4_time_of_day": exp4_results,
    "exp5_clustering": exp5_results,
    "exp6_factor_activity": exp6_results,
    "exp7_confidence_sizing": exp7_results,
    "direction_1bar": dir_results,
}

json_path = BASE_DIR / "missions" / "mission_027_momentum_burst.json"
with open(json_path, "w", encoding="utf-8") as f:
    json.dump(results, f, indent=2, ensure_ascii=False, default=str)
log.info(f"Saved: {json_path}")

exp_path = BASE_DIR / "experiments" / "mission_027_momentum_burst.json"
with open(exp_path, "w", encoding="utf-8") as f:
    json.dump(results, f, indent=2, ensure_ascii=False, default=str)

log.info("=== Mission 027 Complete ===")
