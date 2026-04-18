"""
Mission 040: Regime Death Predictor
-----------------------------------
Mirror ของ Mission 039 — แทนที่จะทำนาย "survival ตั้งแต่ birth"
เรามาทำนาย "death ที่จะเกิดขึ้นใน 1-3 bars ข้างหน้า" สำหรับ regime ที่ active อยู่แล้ว

แรงบันดาลใจ:
- Mission 038: SIGNAL_FLIP เป็น exit_reason ที่แย่ที่สุดในทุก age bucket
- Paper trading ยังเสียเงินกับ SIGNAL_FLIP (แม้หลัง surgery 04-10)
- Mission 039: predictors ที่ birth ล้มเหลวเพราะ regime duration = random
- คำถามใหม่: ถ้าใช้ข้อมูล "ปัจจุบัน" (score decay, slope, margin) ของ regime
  ที่ aged แล้ว เราจะทำนาย imminent death ได้แม่นพอไหม?

สมมติฐาน:
1. ก่อนตาย regime score จะ "decay" จาก peak (peak แล้วอ่อนลง)
2. Slope ของ score (Δ over last 3 bars) จะเข้าหา 0 ก่อน flip
3. |score| ใกล้ threshold มากกว่า = imminent death
4. High volatility spike = regime instability → พร้อม flip

Payoff ถ้าสำเร็จ:
- เพิ่ม "early_exit" rule ใน live strategy:
  ถ้ากำลัง hold trade อยู่ และตรวจพบ imminent death → exit ที่ close bar ปัจจุบัน
  (ไม่ต้องรอ SIGNAL_FLIP ที่ bar ถัดไป)
- ลด loss จาก SIGNAL_FLIP exits

Anti-lookahead:
- Features ใช้เฉพาะข้อมูลถึง bar t (ไม่ใช่ future bars)
- Label (will_die_within_K) ใช้ความจริง OOS = validation truth
- Filter/Rule apply เฉพาะ features ที่ available ที่ bar t
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
THRESHOLD = 3.0
MATURE_AGE = 3  # minimum age to consider a regime "mature" (has decay signal)

results = {}

# ── Load BTC features + score ──
log.info("Loading BTC data...")
btc_ohlcv = bt.fetch_binance_15m("BTCUSDT", years=3)
if "date_time" in btc_ohlcv.columns:
    btc_ohlcv = btc_ohlcv.rename(columns={"date_time": "ts"})
db_data = bt.load_btc_db_data()
btc_df = bt.build_btc_features(btc_ohlcv, db_data)

core = bt.compute_btc_composite_score(btc_df, COMPOSITE_WEIGHTS)
s_ob = score_ob_combined(btc_df, weight=V3_EXTRA_WEIGHTS["ob_combined"])
s_basis = score_basis_contrarian(btc_df, weight=V3_EXTRA_WEIGHTS["basis_contrarian"])
s_tick = score_tick_liq(btc_df, weight=V3_EXTRA_WEIGHTS["tick_liq"])
btc_score_full = core + s_ob + s_basis + s_tick

score_df = pd.DataFrame({
    "ts": btc_df["ts"].values,
    "btc_score": btc_score_full.values,
    "range_z": btc_df.get("range_z", pd.Series(0, index=btc_df.index)).fillna(0).values,
    "atr": btc_df["atr"].fillna(0).values,
    "rsi": btc_df["rsi"].fillna(50).values,
    "vol_ratio": btc_df["vol_ratio"].fillna(1).values,
    "liq_net": btc_df.get("liq_net", pd.Series(0, index=btc_df.index)).fillna(0).values,
}).sort_values("ts").reset_index(drop=True)

# Signal regime numbering
raw = np.where(score_df["btc_score"] >= THRESHOLD, 1,
      np.where(score_df["btc_score"] <= -THRESHOLD, -1, 0))
score_df["raw_signal"] = raw

age = np.zeros(len(score_df), dtype=int)
for i in range(1, len(score_df)):
    if raw[i] != 0 and raw[i] == raw[i-1]:
        age[i] = age[i-1] + 1
    elif raw[i] != 0:
        age[i] = 1
    else:
        age[i] = 0
score_df["signal_age"] = age

# regime_id: ++1 every regime transition (signal goes from !=dir to dir or 0->dir)
regime_id = np.zeros(len(score_df), dtype=int)
cur_id = 0
for i in range(len(score_df)):
    if age[i] == 1:
        cur_id += 1
    regime_id[i] = cur_id if age[i] > 0 else 0
score_df["regime_id"] = regime_id

# ── OOS slicing ──
oos_mask = (score_df["ts"] >= OOS_START) & (score_df["ts"] <= OOS_END)
oos_df = score_df[oos_mask].reset_index(drop=True)
log.info(f"OOS bars: {len(oos_df)}, active signal bars: {(oos_df['signal_age']>0).sum()}")

# ── EXP1: Build regime catalog (per-regime stats) ──
log.info("=== EXP1: Regime catalog with death timing ===")
active = oos_df[oos_df["signal_age"] > 0].copy()
regime_groups = active.groupby("regime_id")

regime_rows = []
for rid, g in regime_groups:
    if rid == 0:
        continue
    dur = len(g)
    dir_v = int(g["raw_signal"].iloc[0])
    score_abs = g["btc_score"].abs().values
    peak = float(score_abs.max())
    peak_idx = int(score_abs.argmax())  # position within regime (0-based)
    regime_rows.append({
        "regime_id": int(rid),
        "direction": dir_v,
        "duration": dur,
        "peak_score_abs": peak,
        "peak_at_age": peak_idx + 1,
        "end_score_abs": float(score_abs[-1]),
    })
regimes = pd.DataFrame(regime_rows)
log.info(f"Total regimes OOS: {len(regimes)}")
log.info(f"Mean duration: {regimes['duration'].mean():.1f} bars, "
         f"median {regimes['duration'].median():.0f}")
log.info(f"Peak_at_age distribution: "
         f"median={regimes['peak_at_age'].median():.0f}, "
         f"mean={regimes['peak_at_age'].mean():.1f}")

results["exp1_regime_catalog"] = {
    "total_regimes": int(len(regimes)),
    "mean_duration": round(float(regimes["duration"].mean()), 2),
    "median_duration": int(regimes["duration"].median()),
    "mean_peak_at_age": round(float(regimes["peak_at_age"].mean()), 2),
    "median_peak_at_age": int(regimes["peak_at_age"].median()),
    "mean_peak_score_abs": round(float(regimes["peak_score_abs"].mean()), 3),
}

# ── EXP2: For each active bar, compute bars_until_death + features ──
log.info("=== EXP2: Per-bar decay features ===")

# Build features at each bar: bars_until_death, score_abs, score_peak_so_far, decay_ratio, slope
bar_features = []
for rid, g in regime_groups:
    if rid == 0:
        continue
    g = g.reset_index(drop=True)
    dur = len(g)
    dir_v = int(g["raw_signal"].iloc[0])
    sc = g["btc_score"].values
    sc_abs = np.abs(sc)
    # Cumulative max of sc_abs
    peak_cum = np.maximum.accumulate(sc_abs)
    for i in range(len(g)):
        age_i = i + 1
        bars_until_death = dur - age_i  # 0 = dies next bar (current is last bar of regime)
        # score slope: Δscore over last 3 bars (in absolute-toward-regime-direction)
        # Use signed direction: if dir=+1, slope = sc[i] - sc[i-3]; else -sc[i] + sc[i-3]
        # Actually we want "is the signal strengthening in its direction?"
        if i >= 3:
            slope_signed = dir_v * (sc[i] - sc[i-3])  # positive = strengthening
        else:
            slope_signed = 0.0
        # score_margin = how far from flip threshold (sc crosses opposite threshold to flip
        # but we also care about crossing 0). Use distance to opposite threshold:
        # For dir=+1 regime: dies when sc drops below THRESHOLD (hysteresis-free version)
        # distance = sc - (-THRESHOLD) = sc + THRESHOLD; but more relevant = sc_abs - THRESHOLD
        margin = sc_abs[i] - THRESHOLD
        # decay ratio: current / peak_so_far (1.0 = at peak, 0.5 = half peak)
        decay = sc_abs[i] / max(peak_cum[i], 1e-6)
        bar_features.append({
            "regime_id": int(rid),
            "direction": dir_v,
            "age": age_i,
            "duration": dur,
            "bars_until_death": bars_until_death,
            "score": float(sc[i]),
            "score_abs": float(sc_abs[i]),
            "peak_so_far": float(peak_cum[i]),
            "decay_ratio": float(decay),
            "slope_signed_3bar": float(slope_signed),
            "margin_to_thr": float(margin),
            "range_z": float(g["range_z"].iloc[i]),
            "atr": float(g["atr"].iloc[i]),
            "vol_ratio": float(g["vol_ratio"].iloc[i]),
            "rsi": float(g["rsi"].iloc[i]),
            "ts": g["ts"].iloc[i],
        })
bars = pd.DataFrame(bar_features)
log.info(f"Total active bars: {len(bars)}")

# Restrict to "mature" bars (age >= MATURE_AGE) — where we expect decay signal
mature = bars[bars["age"] >= MATURE_AGE].copy()
log.info(f"Mature bars (age>={MATURE_AGE}): {len(mature)}")

# Label: will die within K bars?
mature["dies_within_1"] = (mature["bars_until_death"] <= 1).astype(int)
mature["dies_within_2"] = (mature["bars_until_death"] <= 2).astype(int)
mature["dies_within_3"] = (mature["bars_until_death"] <= 3).astype(int)

# Base rates
for k in [1, 2, 3]:
    rate = mature[f"dies_within_{k}"].mean() * 100
    log.info(f"  Base rate P(dies within {k}): {rate:.1f}%")

results["exp2_mature_death_base_rates"] = {
    "mature_bars": int(len(mature)),
    "p_dies_within_1_pct": round(float(mature["dies_within_1"].mean() * 100), 2),
    "p_dies_within_2_pct": round(float(mature["dies_within_2"].mean() * 100), 2),
    "p_dies_within_3_pct": round(float(mature["dies_within_3"].mean() * 100), 2),
}

# ── EXP3: Feature discriminator — "imminent death" (dies within 2) vs "healthy" (>=4 bars left) ──
log.info("=== EXP3: Feature profile — imminent death vs healthy ===")
imminent = mature[mature["bars_until_death"] <= 2]
healthy = mature[mature["bars_until_death"] >= 4]
log.info(f"  Imminent death bars (dies<=2): {len(imminent)}")
log.info(f"  Healthy bars (>=4 bars left): {len(healthy)}")

feature_cols = [
    "score_abs", "decay_ratio", "slope_signed_3bar", "margin_to_thr",
    "range_z", "atr", "vol_ratio", "rsi",
]
profile = {}
for col in feature_cols:
    i_mean = float(imminent[col].mean())
    h_mean = float(healthy[col].mean())
    i_std = float(imminent[col].std()) if len(imminent) > 1 else 0
    h_std = float(healthy[col].std()) if len(healthy) > 1 else 0
    pooled_std = np.sqrt((i_std**2 + h_std**2) / 2) if (i_std + h_std) > 0 else 1
    cohen_d = (i_mean - h_mean) / max(pooled_std, 1e-8)
    profile[col] = {
        "imminent_mean": round(i_mean, 4),
        "healthy_mean": round(h_mean, 4),
        "delta": round(i_mean - h_mean, 4),
        "cohen_d": round(cohen_d, 3),
    }
    mark = " <<" if abs(cohen_d) > 0.2 else ""
    log.info(f"  {col:>20s}: imm={i_mean:+.3f} healthy={h_mean:+.3f} "
             f"Δ={i_mean-h_mean:+.3f} d={cohen_d:+.2f}{mark}")

results["exp3_imminent_vs_healthy_profile"] = profile

# ── EXP4: Univariate filters — P(dies within 2 | filter) vs base rate ──
log.info("=== EXP4: Univariate death filters (lift over base rate) ===")
base_rate = float(mature["dies_within_2"].mean())
filter_specs = {
    "decay_ratio < 0.6 (score < 60% of peak)": mature[mature["decay_ratio"] < 0.6],
    "decay_ratio < 0.5": mature[mature["decay_ratio"] < 0.5],
    "decay_ratio < 0.4": mature[mature["decay_ratio"] < 0.4],
    "margin_to_thr < 0.5 (close to flip)": mature[mature["margin_to_thr"] < 0.5],
    "margin_to_thr < 0.3": mature[mature["margin_to_thr"] < 0.3],
    "margin_to_thr < 0.1": mature[mature["margin_to_thr"] < 0.1],
    "slope<0 (decelerating)": mature[mature["slope_signed_3bar"] < 0],
    "slope<-0.5 (strong decel)": mature[mature["slope_signed_3bar"] < -0.5],
    "slope<-1.0": mature[mature["slope_signed_3bar"] < -1.0],
    "range_z>=1.0 (vol spike)": mature[mature["range_z"] >= 1.0],
    "range_z>=2.0 (huge spike)": mature[mature["range_z"] >= 2.0],
    "combined: decay<0.5 AND margin<0.5":
        mature[(mature["decay_ratio"] < 0.5) & (mature["margin_to_thr"] < 0.5)],
    "combined: decay<0.6 AND slope<0":
        mature[(mature["decay_ratio"] < 0.6) & (mature["slope_signed_3bar"] < 0)],
    "combined: decay<0.5 AND slope<-0.5":
        mature[(mature["decay_ratio"] < 0.5) & (mature["slope_signed_3bar"] < -0.5)],
    "combined: margin<0.3 AND slope<0":
        mature[(mature["margin_to_thr"] < 0.3) & (mature["slope_signed_3bar"] < 0)],
}
filt_results = {}
for name, sub in filter_specs.items():
    if len(sub) < 20:
        continue
    p_dies = float(sub["dies_within_2"].mean())
    lift = (p_dies - base_rate) / max(base_rate, 1e-6) * 100
    filt_results[name] = {
        "n": int(len(sub)),
        "p_dies_within_2": round(p_dies * 100, 2),
        "lift_pct": round(lift, 1),
        "lift_pp": round((p_dies - base_rate) * 100, 2),
    }
    mark = " <<" if lift > 30 else ""
    log.info(f"  {name:>50s}: n={len(sub):5d}, P(die<=2)={p_dies*100:5.1f}%, "
             f"lift=+{lift:5.1f}%{mark}")

results["exp4_univariate_death_filters"] = {
    "base_rate_p_dies_within_2": round(base_rate * 100, 2),
    "filters": filt_results,
}

# ── Step 5: Run backtests, tag trades with death_proximity + test early exit rule ──
log.info("=== Running backtests per coin... ===")

# Build lookup: ts -> bars_until_death of that bar's regime
# Only populated for active bars; -1 for non-active
bars_sorted = bars.sort_values("ts").reset_index(drop=True)
bars_sorted["ts"] = pd.to_datetime(bars_sorted["ts"])
bars_idx = bars_sorted.set_index("ts")
# Use numeric array for searchsorted to avoid type-mismatch
ts_ns = bars_idx.index.values.astype("datetime64[ns]").astype(np.int64)

all_trades = []
for symbol in COINS:
    coin = symbol.replace("USDT", "")
    ohlcv = bt.fetch_binance_15m(symbol, years=3)
    if "date_time" in ohlcv.columns:
        ohlcv = ohlcv.rename(columns={"date_time": "ts"})
    alt_df = bt.build_alt_technicals(ohlcv)
    oos_alt = (alt_df["ts"] >= OOS_START) & (alt_df["ts"] <= OOS_END)

    cfg = COIN_CONFIGS.get(coin, {})
    btc_score_ts = pd.Series(score_df["btc_score"].values, index=score_df["ts"].values)
    signals, alt_merged = bt.generate_btc_led_signal(
        btc_score_ts, alt_df[oos_alt],
        threshold=cfg.get("threshold", 3.0),
        use_alt_pa_filter=cfg.get("use_alt_pa_filter", False))
    trades = bt.run_backtest(alt_merged, signals,
                             sl_atr_mult=cfg.get("sl_atr_mult", 2.5),
                             tp_atr_mult=cfg.get("tp_atr_mult", 4.0),
                             cooldown_bars=cfg.get("cooldown_bars", 4))
    if len(trades) == 0:
        continue
    trades["coin"] = coin

    # Tag each trade entry with features at entry time
    entry_ts = pd.to_datetime(trades["entry_time"].values)
    entry_age = []
    entry_decay = []
    entry_slope = []
    entry_margin = []
    entry_bars_until_death = []
    entry_regime_dur = []
    entry_ns = entry_ts.astype("datetime64[ns]").astype(np.int64)
    for et_ns in entry_ns:
        # Signal bar = bar STRICTLY before entry_time (entry executes at next bar open)
        pos = int(np.searchsorted(ts_ns, et_ns, side="left") - 1)
        if pos >= 0:
            row = bars_idx.iloc[pos]
            entry_age.append(int(row["age"]))
            entry_decay.append(float(row["decay_ratio"]))
            entry_slope.append(float(row["slope_signed_3bar"]))
            entry_margin.append(float(row["margin_to_thr"]))
            entry_bars_until_death.append(int(row["bars_until_death"]))
            entry_regime_dur.append(int(row["duration"]))
        else:
            entry_age.append(0)
            entry_decay.append(np.nan)
            entry_slope.append(np.nan)
            entry_margin.append(np.nan)
            entry_bars_until_death.append(-1)
            entry_regime_dur.append(0)
    trades["entry_age"] = entry_age
    trades["entry_decay_ratio"] = entry_decay
    trades["entry_slope_3bar"] = entry_slope
    trades["entry_margin"] = entry_margin
    trades["bars_until_regime_death"] = entry_bars_until_death
    trades["regime_duration"] = entry_regime_dur
    all_trades.append(trades)

trades_df = pd.concat(all_trades, ignore_index=True)
baseline_pnl = float(trades_df["pnl_net"].sum())
baseline_wr = float((trades_df["pnl_net"] > 0).mean() * 100)
log.info(f"Total trades: {len(trades_df)}, baseline PnL=${baseline_pnl:.0f}, "
         f"WR={baseline_wr:.1f}%")

# ── EXP5: Trade PnL by bars_until_regime_death at entry ──
log.info("=== EXP5: Trade PnL by bars_until_regime_death at entry ==="  )
buckets = [
    ("imminent (dies within 2)", trades_df["bars_until_regime_death"] <= 2),
    ("short (3-5 bars left)", (trades_df["bars_until_regime_death"] >= 3) & (trades_df["bars_until_regime_death"] <= 5)),
    ("healthy (6-15 bars)", (trades_df["bars_until_regime_death"] >= 6) & (trades_df["bars_until_regime_death"] <= 15)),
    ("long (16+ bars)", trades_df["bars_until_regime_death"] >= 16),
]
bucket_stats = {}
for name, mask in buckets:
    sub = trades_df[mask]
    if len(sub) < 5:
        continue
    bucket_stats[name] = {
        "n": int(len(sub)),
        "wr": round(float((sub["pnl_net"] > 0).mean() * 100), 1),
        "pnl": round(float(sub["pnl_net"].sum()), 2),
        "avg": round(float(sub["pnl_net"].mean()), 3),
    }
    flip_rate = (sub["exit_reason"] == "SIGNAL_FLIP").mean() * 100
    bucket_stats[name]["signal_flip_pct"] = round(float(flip_rate), 1)
    log.info(f"  {name:>30s}: n={len(sub):4d}, WR={bucket_stats[name]['wr']:.1f}%, "
             f"PnL=${bucket_stats[name]['pnl']:+,.0f}, FLIP%={flip_rate:.1f}")
results["exp5_trade_pnl_by_remaining_life"] = bucket_stats

# ── EXP6: "Don't enter" filter — skip trades opened in regimes that die within K ──
log.info("=== EXP6: Skip entries in dying regimes (hindsight ceiling) ===")
skip_strategies = {
    "baseline": trades_df,
    "skip if bars_until_death<=0 (dies NOW)":
        trades_df[trades_df["bars_until_regime_death"] > 0],
    "skip if bars_until_death<=1":
        trades_df[trades_df["bars_until_regime_death"] > 1],
    "skip if bars_until_death<=2":
        trades_df[trades_df["bars_until_regime_death"] > 2],
    "skip if bars_until_death<=3":
        trades_df[trades_df["bars_until_regime_death"] > 3],
    "skip if bars_until_death<=5":
        trades_df[trades_df["bars_until_regime_death"] > 5],
}
skip_results = {}
for name, sub in skip_strategies.items():
    s = {
        "n": int(len(sub)),
        "wr": round(float((sub["pnl_net"] > 0).mean() * 100), 1),
        "pnl": round(float(sub["pnl_net"].sum()), 2),
        "delta_vs_baseline": round(float(sub["pnl_net"].sum()) - baseline_pnl, 2),
    }
    skip_results[name] = s
    log.info(f"  {name:>40s}: n={s['n']:4d}, WR={s['wr']:.1f}%, "
             f"PnL={s['pnl']:+,.0f} (Δ={s['delta_vs_baseline']:+,.0f})")
results["exp6_hindsight_ceiling_skip_dying"] = skip_results

# ── EXP7: Realistic (non-hindsight) filter at entry based on decay_ratio + slope ──
log.info("=== EXP7: Realistic entry filters (features available at entry) ===")
realistic_filters = {
    "skip entry_decay<0.5 (faded to <50% peak)": trades_df[~(trades_df["entry_decay_ratio"] < 0.5)],
    "skip entry_decay<0.6": trades_df[~(trades_df["entry_decay_ratio"] < 0.6)],
    "skip entry_slope<0": trades_df[~(trades_df["entry_slope_3bar"] < 0)],
    "skip entry_slope<-1.0": trades_df[~(trades_df["entry_slope_3bar"] < -1.0)],
    "skip entry_margin<0.3": trades_df[~(trades_df["entry_margin"] < 0.3)],
    "skip decay<0.5 AND slope<0":
        trades_df[~((trades_df["entry_decay_ratio"] < 0.5) & (trades_df["entry_slope_3bar"] < 0))],
    "skip decay<0.6 AND margin<0.5":
        trades_df[~((trades_df["entry_decay_ratio"] < 0.6) & (trades_df["entry_margin"] < 0.5))],
    "skip margin<0.3 AND slope<0":
        trades_df[~((trades_df["entry_margin"] < 0.3) & (trades_df["entry_slope_3bar"] < 0))],
    "skip margin<0.5 AND slope<-1.0":
        trades_df[~((trades_df["entry_margin"] < 0.5) & (trades_df["entry_slope_3bar"] < -1.0))],
}
realistic_results = {}
for name, sub in realistic_filters.items():
    n_skip = len(trades_df) - len(sub)
    s = {
        "n_kept": int(len(sub)),
        "n_skipped": int(n_skip),
        "wr": round(float((sub["pnl_net"] > 0).mean() * 100), 1),
        "pnl": round(float(sub["pnl_net"].sum()), 2),
        "delta_vs_baseline": round(float(sub["pnl_net"].sum()) - baseline_pnl, 2),
    }
    realistic_results[name] = s
    mark = " <<" if s["delta_vs_baseline"] > 0 else ""
    log.info(f"  {name:>55s}: keep={s['n_kept']:4d} skip={n_skip:3d}, "
             f"WR={s['wr']:.1f}%, PnL={s['pnl']:+,.0f} "
             f"(Δ={s['delta_vs_baseline']:+,.0f}){mark}")
results["exp7_realistic_entry_filters"] = realistic_results

# ── EXP8: PnL of skipped trades (what are we throwing away?) ──
log.info("=== EXP8: PnL of SKIPPED subsets (what we throw away) ===")
skipped_analysis = {}
skip_configs = {
    "decay<0.5 subset": trades_df[trades_df["entry_decay_ratio"] < 0.5],
    "decay<0.6 subset": trades_df[trades_df["entry_decay_ratio"] < 0.6],
    "slope<0 subset": trades_df[trades_df["entry_slope_3bar"] < 0],
    "slope<-1 subset": trades_df[trades_df["entry_slope_3bar"] < -1.0],
    "margin<0.3 subset": trades_df[trades_df["entry_margin"] < 0.3],
    "margin<0.3 AND slope<0": trades_df[
        (trades_df["entry_margin"] < 0.3) & (trades_df["entry_slope_3bar"] < 0)],
    "decay<0.5 AND slope<0": trades_df[
        (trades_df["entry_decay_ratio"] < 0.5) & (trades_df["entry_slope_3bar"] < 0)],
}
for name, sub in skip_configs.items():
    if len(sub) < 5:
        continue
    skipped_analysis[name] = {
        "n": int(len(sub)),
        "wr": round(float((sub["pnl_net"] > 0).mean() * 100), 1),
        "pnl": round(float(sub["pnl_net"].sum()), 2),
        "avg": round(float(sub["pnl_net"].mean()), 3),
    }
    log.info(f"  {name:>35s}: n={len(sub):4d}, WR={skipped_analysis[name]['wr']:.1f}%, "
             f"PnL=${skipped_analysis[name]['pnl']:+,.0f}, "
             f"avg=${skipped_analysis[name]['avg']:+,.2f}")
results["exp8_skipped_subset_analysis"] = skipped_analysis

# ── EXP9: SIGNAL_FLIP-specific analysis ──
log.info("=== EXP9: SIGNAL_FLIP trades — can entry features predict them? ===")
flip_trades = trades_df[trades_df["exit_reason"] == "SIGNAL_FLIP"]
non_flip = trades_df[trades_df["exit_reason"] != "SIGNAL_FLIP"]
log.info(f"  FLIP trades: {len(flip_trades)}, Non-FLIP: {len(non_flip)}")
log.info(f"  FLIP PnL: ${flip_trades['pnl_net'].sum():.0f}, "
         f"Non-FLIP PnL: ${non_flip['pnl_net'].sum():.0f}")
flip_profile = {}
for col in ["entry_age", "entry_decay_ratio", "entry_slope_3bar", "entry_margin",
            "regime_duration", "bars_until_regime_death"]:
    if col not in trades_df.columns:
        continue
    flip_mean = float(flip_trades[col].mean())
    nonflip_mean = float(non_flip[col].mean())
    flip_profile[col] = {
        "flip_mean": round(flip_mean, 4),
        "nonflip_mean": round(nonflip_mean, 4),
        "delta": round(flip_mean - nonflip_mean, 4),
    }
    log.info(f"  {col:>28s}: FLIP={flip_mean:+.3f}, NON-FLIP={nonflip_mean:+.3f}, "
             f"Δ={flip_mean-nonflip_mean:+.3f}")
results["exp9_flip_vs_nonflip_profile"] = {
    "flip_count": int(len(flip_trades)),
    "flip_pnl": round(float(flip_trades["pnl_net"].sum()), 2),
    "nonflip_count": int(len(non_flip)),
    "nonflip_pnl": round(float(non_flip["pnl_net"].sum()), 2),
    "profile": flip_profile,
}

# ── EXP10: Interaction with existing Mission 038 rule (skip age=1) ──
log.info("=== EXP10: Combine death filter with Mission 038 (skip age=1) ===")
# Mission 038 rule: skip age=1 trades
m038 = trades_df[trades_df["entry_age"] != 1]
m038_pnl = float(m038["pnl_net"].sum())
log.info(f"  Mission 038 (skip age=1): n={len(m038)}, PnL=${m038_pnl:+,.0f} "
         f"(Δ={m038_pnl-baseline_pnl:+,.0f})")

combos = {
    "M038 + skip margin<0.3 AND slope<0":
        m038[~((m038["entry_margin"] < 0.3) & (m038["entry_slope_3bar"] < 0))],
    "M038 + skip decay<0.5 AND slope<0":
        m038[~((m038["entry_decay_ratio"] < 0.5) & (m038["entry_slope_3bar"] < 0))],
    "M038 + skip bars_until_death<=2 (hindsight)":
        m038[m038["bars_until_regime_death"] > 2],
}
combo_results = {}
for name, sub in combos.items():
    s = {
        "n": int(len(sub)),
        "wr": round(float((sub["pnl_net"] > 0).mean() * 100), 1),
        "pnl": round(float(sub["pnl_net"].sum()), 2),
        "delta_vs_baseline": round(float(sub["pnl_net"].sum()) - baseline_pnl, 2),
        "delta_vs_m038": round(float(sub["pnl_net"].sum()) - m038_pnl, 2),
    }
    combo_results[name] = s
    log.info(f"  {name:>50s}: n={s['n']:4d}, PnL={s['pnl']:+,.0f} "
             f"(Δ_base={s['delta_vs_baseline']:+,.0f}, Δ_m038={s['delta_vs_m038']:+,.0f})")
results["exp10_combined_with_m038"] = {
    "m038_pnl": round(m038_pnl, 2),
    "m038_delta_vs_baseline": round(m038_pnl - baseline_pnl, 2),
    "combos": combo_results,
}

# ── Save ──
mission_data = {
    "mission_id": "mission_040_regime_death_predictor",
    "date": datetime.utcnow().strftime("%Y-%m-%d"),
    "type": "signal_quality / exit_timing / regime_analysis",
    "title": "Regime Death Predictor — ทำนายสัญญาณใกล้ตายได้ไหม?",
    "hypothesis": "ใช้ decay_ratio + slope + margin ที่ mature regime ทำนาย death ภายใน 2 bars",
    "coins": [c.replace("USDT", "") for c in COINS],
    "oos_period": f"{OOS_START} to {OOS_END}",
    "total_trades": int(len(trades_df)),
    "baseline_pnl": round(baseline_pnl, 2),
    "baseline_wr": round(baseline_wr, 1),
    "threshold": THRESHOLD,
    "mature_age": MATURE_AGE,
    "results": results,
}

out_json = BASE_DIR / "missions" / "mission_040_regime_death_predictor.json"
with open(out_json, "w", encoding="utf-8") as f:
    json.dump(mission_data, f, indent=2, ensure_ascii=False, default=str)
log.info(f"Saved: {out_json}")

# ── Console summary ──
print("\n" + "=" * 75)
print("  MISSION 040: Regime Death Predictor")
print("=" * 75)
print(f"  Total trades: {len(trades_df)} | Baseline PnL: ${baseline_pnl:,.0f} | WR: {baseline_wr:.1f}%")
print(f"  Mature bars (age>={MATURE_AGE}): {results['exp2_mature_death_base_rates']['mature_bars']}")
print(f"  Base rate P(dies within 2): {results['exp2_mature_death_base_rates']['p_dies_within_2_pct']}%")
print()
print("  Top discriminators (|Cohen's d|):")
disc = sorted(results["exp3_imminent_vs_healthy_profile"].items(),
              key=lambda x: abs(x[1]["cohen_d"]), reverse=True)[:5]
for name, p in disc:
    print(f"    {name:>20s}: imm={p['imminent_mean']:+.3f} vs healthy={p['healthy_mean']:+.3f} "
          f"(d={p['cohen_d']:+.2f})")
print()
print("  Best univariate filter (P(dies within 2)):")
best_u = sorted(results["exp4_univariate_death_filters"]["filters"].items(),
                key=lambda x: x[1]["lift_pct"], reverse=True)[:3]
for name, s in best_u:
    print(f"    {name:>50s}: n={s['n']:5d}, P={s['p_dies_within_2']:.1f}%, "
          f"lift=+{s['lift_pct']:.1f}%")
print()
print("  Realistic entry filter results:")
for name, s in sorted(results["exp7_realistic_entry_filters"].items(),
                       key=lambda x: x[1]["delta_vs_baseline"], reverse=True):
    print(f"    {name:>55s}: PnL={s['pnl']:+,.0f} "
          f"(Δ={s['delta_vs_baseline']:+,.0f}, skip={s['n_skipped']})")
print("=" * 75)
