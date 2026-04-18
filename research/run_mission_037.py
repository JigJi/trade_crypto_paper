"""
Mission 037: Premium Index as Real-Time Positioning Signal
==========================================================
สมมติฐาน: premium_index (mark vs index spread) อัปเดตทุก 5 นาที (58K records)
เป็น real-time positioning signal ที่ดีกว่า funding_rate 8h (856 records, active 0.4%)

Premium > 0 → longs overcrowded → contrarian SHORT
Premium < 0 → shorts overcrowded → contrarian LONG

Experiments:
1. Premium distribution & extreme events
2. Forward return after premium extremes
3. Premium z-score as v3 factor (backtest)
4. Compare with existing funding_rate factor
5. Interaction with top factors (liq, ob, etf)
"""

import sys, json, logging
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import psycopg2
from research.config import get_pg_dsn
import backtest_15m_btc_led_alts as bt
from paper_trading.config import COMPOSITE_WEIGHTS, V3_EXTRA_WEIGHTS, COIN_CONFIGS
from paper_trading.strategy import score_ob_combined, score_basis_contrarian, score_tick_liq

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

BKK_UTC_OFFSET = timedelta(hours=7)

results = {}

# ── Load Premium Index ──────────────────────────────────────────
log.info("Loading premium_index from DB...")
conn = psycopg2.connect(get_pg_dsn())
pi_raw = pd.read_sql(
    "SELECT ts, premium, last_funding_rate, mark_price, index_price "
    "FROM market_data.premium_index ORDER BY ts",
    conn,
)
conn.close()

pi_raw["ts"] = pd.to_datetime(pi_raw["ts"], utc=True).dt.tz_localize(None)
pi_raw["ts"] = pi_raw["ts"] - BKK_UTC_OFFSET
pi_raw = pi_raw.sort_values("ts").reset_index(drop=True)

log.info(f"Premium index: {len(pi_raw)} rows, {pi_raw['ts'].min()} → {pi_raw['ts'].max()}")

# ── EXP1: Distribution & Extreme Events ────────────────────────
log.info("EXP1: Premium distribution...")

premium = pi_raw["premium"].dropna()
results["exp1_distribution"] = {
    "count": int(len(premium)),
    "mean": float(premium.mean()),
    "std": float(premium.std()),
    "min": float(premium.min()),
    "max": float(premium.max()),
    "pct_1": float(premium.quantile(0.01)),
    "pct_5": float(premium.quantile(0.05)),
    "pct_95": float(premium.quantile(0.95)),
    "pct_99": float(premium.quantile(0.99)),
    "skew": float(premium.skew()),
    "kurtosis": float(premium.kurtosis()),
}

# z-score
pi_raw["prem_zscore"] = (premium - premium.mean()) / premium.std()

extreme_neg = (pi_raw["prem_zscore"] < -2).sum()
extreme_pos = (pi_raw["prem_zscore"] > 2).sum()
results["exp1_extremes"] = {
    "z_below_neg2": int(extreme_neg),
    "z_above_pos2": int(extreme_pos),
    "pct_extreme_neg": round(extreme_neg / len(pi_raw) * 100, 2),
    "pct_extreme_pos": round(extreme_pos / len(pi_raw) * 100, 2),
}
log.info(f"  Extremes: z<-2: {extreme_neg} ({extreme_neg/len(pi_raw)*100:.1f}%), z>2: {extreme_pos} ({extreme_pos/len(pi_raw)*100:.1f}%)")

# ── Resample to 15m ─────────────────────────────────────────────
log.info("Resampling premium to 15m...")
pi_raw = pi_raw.set_index("ts")
pi_15m = pi_raw["premium"].resample("15min").mean().dropna()
pi_fr_15m = pi_raw["last_funding_rate"].resample("15min").last().dropna()
pi_15m = pi_15m.reset_index()
pi_15m.columns = ["ts", "premium_15m"]
pi_fr = pi_fr_15m.reset_index()
pi_fr.columns = ["ts", "last_fr_15m"]
pi_15m = pi_15m.merge(pi_fr, on="ts", how="left")

log.info(f"  15m bars: {len(pi_15m)}")

# ── EXP2: Forward Returns after Premium Extremes ───────────────
log.info("EXP2: Forward returns after premium extremes...")

btc_ohlcv = bt.fetch_binance_15m("BTCUSDT", years=3)
if "date_time" in btc_ohlcv.columns:
    btc_ohlcv = btc_ohlcv.rename(columns={"date_time": "ts"})

# Merge premium with BTC price
merged = pd.merge_asof(
    btc_ohlcv.sort_values("ts"),
    pi_15m.sort_values("ts"),
    on="ts",
    direction="backward",
)

# Forward returns (1h = 4 bars, 4h = 16 bars)
for n, label in [(4, "1h"), (16, "4h")]:
    merged[f"fwd_{label}"] = merged["close"].shift(-n) / merged["close"] - 1

# Premium z-score on 15m
prem = merged["premium_15m"].dropna()
prem_mean = prem.mean()
prem_std = prem.std()
merged["prem_z"] = (merged["premium_15m"] - prem_mean) / prem_std

# Analyze by z-score buckets
z_buckets = [
    ("z < -3", merged["prem_z"] < -3),
    ("-3 < z < -2", (merged["prem_z"] >= -3) & (merged["prem_z"] < -2)),
    ("-2 < z < -1", (merged["prem_z"] >= -2) & (merged["prem_z"] < -1)),
    ("-1 < z < 1 (neutral)", (merged["prem_z"] >= -1) & (merged["prem_z"] <= 1)),
    ("1 < z < 2", (merged["prem_z"] > 1) & (merged["prem_z"] <= 2)),
    ("2 < z < 3", (merged["prem_z"] > 2) & (merged["prem_z"] <= 3)),
    ("z > 3", merged["prem_z"] > 3),
]

fwd_results = []
for label, mask in z_buckets:
    sub = merged[mask].dropna(subset=["fwd_1h", "fwd_4h"])
    if len(sub) > 0:
        fwd_results.append({
            "bucket": label,
            "n": int(len(sub)),
            "avg_fwd_1h_bps": round(sub["fwd_1h"].mean() * 10000, 2),
            "avg_fwd_4h_bps": round(sub["fwd_4h"].mean() * 10000, 2),
            "contrarian_1h_wr": round(
                ((sub["prem_z"] < 0) & (sub["fwd_1h"] > 0) |
                 (sub["prem_z"] > 0) & (sub["fwd_1h"] < 0)).mean() * 100, 1
            ) if label != "-1 < z < 1 (neutral)" else None,
        })

results["exp2_forward_returns"] = fwd_results
for r in fwd_results:
    log.info(f"  {r['bucket']}: n={r['n']}, 1h={r['avg_fwd_1h_bps']}bps, 4h={r['avg_fwd_4h_bps']}bps, cWR={r.get('contrarian_1h_wr')}%")

# ── EXP3: Premium as v3 Factor ──────────────────────────────────
log.info("EXP3: Premium z-score as v3 factor...")

# Build BTC features + v3 score (baseline)
db_data = bt.load_btc_db_data()
btc_df = bt.build_btc_features(btc_ohlcv, db_data)

btc_score = bt.compute_btc_composite_score(btc_df, COMPOSITE_WEIGHTS)
btc_score = btc_score + score_ob_combined(btc_df, weight=V3_EXTRA_WEIGHTS["ob_combined"])
btc_score = btc_score + score_basis_contrarian(btc_df, weight=V3_EXTRA_WEIGHTS["basis_contrarian"])
btc_score = btc_score + score_tick_liq(btc_df, weight=V3_EXTRA_WEIGHTS["tick_liq"])

btc_score_base = pd.Series(btc_score.values, index=btc_df["ts"].values, name="btc_score")

# Add premium to btc_df for scoring
btc_df_m = btc_df.merge(pi_15m[["ts", "premium_15m"]], on="ts", how="left")
prem_col = btc_df_m["premium_15m"].fillna(0)
prem_z = (prem_col - prem_col.mean()) / prem_col.std()
prem_z = prem_z.fillna(0)

def score_premium_contrarian(prem_z_series, weight=1.0, threshold=2.0):
    """Contrarian: extreme negative premium → bullish, extreme positive → bearish"""
    score = np.zeros(len(prem_z_series))
    score[prem_z_series < -threshold] = weight   # shorts overcrowded → long
    score[prem_z_series > threshold] = -weight    # longs overcrowded → short
    return score

# Test multiple weights and thresholds
coins = ["BTCUSDT", "XRPUSDT", "ADAUSDT", "DOTUSDT", "SUIUSDT", "FILUSDT"]
oos_start, oos_end = "2025-01-01", "2026-03-31"

# Baseline first
log.info("  Running baseline...")
base_trades = []
for symbol in coins:
    coin = symbol.replace("USDT", "")
    ohlcv = bt.fetch_binance_15m(symbol, years=3)
    if "date_time" in ohlcv.columns:
        ohlcv = ohlcv.rename(columns={"date_time": "ts"})
    alt_df = bt.build_alt_technicals(ohlcv)
    oos_mask = (alt_df["ts"] >= oos_start) & (alt_df["ts"] <= oos_end)
    cfg = COIN_CONFIGS.get(coin, {})
    signals, alt_merged = bt.generate_btc_led_signal(
        btc_score_base, alt_df[oos_mask],
        threshold=cfg.get("threshold", 3.0),
        use_alt_pa_filter=cfg.get("use_alt_pa_filter", False),
    )
    trades = bt.run_backtest(
        alt_merged, signals,
        sl_atr_mult=cfg.get("sl_atr_mult", 2.5),
        tp_atr_mult=cfg.get("tp_atr_mult", 4.0),
        cooldown_bars=cfg.get("cooldown_bars", 4),
    )
    if len(trades) > 0:
        trades["coin"] = coin
        base_trades.append(trades)

base_df = pd.concat(base_trades, ignore_index=True)
base_pnl = base_df["pnl_net"].sum()
base_wr = (base_df["pnl_net"] > 0).mean() * 100
base_n = len(base_df)
log.info(f"  Baseline: {base_n} trades, WR {base_wr:.1f}%, PnL ${base_pnl:.0f}")

results["exp3_baseline"] = {
    "trades": base_n,
    "wr": round(base_wr, 1),
    "pnl": round(base_pnl, 2),
}

# Test premium factor at different weights/thresholds
test_configs = [
    (0.5, 1.5), (0.5, 2.0), (0.5, 2.5),
    (1.0, 1.5), (1.0, 2.0), (1.0, 2.5),
    (1.5, 1.5), (1.5, 2.0), (1.5, 2.5),
    (2.0, 1.5), (2.0, 2.0), (2.0, 2.5),
]

factor_results = []
for weight, threshold in test_configs:
    prem_score = score_premium_contrarian(prem_z.values, weight=weight, threshold=threshold)
    btc_score_new = btc_score_base.copy()
    btc_score_new = btc_score_new + pd.Series(prem_score, index=btc_df["ts"].values)
    btc_score_ts = pd.Series(btc_score_new.values, index=btc_df["ts"].values, name="btc_score")

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
        )
        trades = bt.run_backtest(
            alt_merged, signals,
            sl_atr_mult=cfg.get("sl_atr_mult", 2.5),
            tp_atr_mult=cfg.get("tp_atr_mult", 4.0),
            cooldown_bars=cfg.get("cooldown_bars", 4),
        )
        if len(trades) > 0:
            trades["coin"] = coin
            all_trades.append(trades)

    if all_trades:
        df = pd.concat(all_trades, ignore_index=True)
        pnl = df["pnl_net"].sum()
        wr = (df["pnl_net"] > 0).mean() * 100
        delta = pnl - base_pnl
        n = len(df)
    else:
        pnl, wr, delta, n = 0, 0, -base_pnl, 0

    factor_results.append({
        "weight": weight,
        "threshold": threshold,
        "trades": n,
        "wr": round(wr, 1),
        "pnl": round(pnl, 2),
        "delta_pnl": round(delta, 2),
    })
    status = "+" if delta > 0 else ""
    log.info(f"  w={weight}, th={threshold}: {n} trades, WR {wr:.1f}%, PnL ${pnl:.0f} (Δ{status}${delta:.0f})")

results["exp3_factor_tests"] = factor_results

# Best config
best = max(factor_results, key=lambda x: x["delta_pnl"])
results["exp3_best"] = best
log.info(f"  BEST: w={best['weight']}, th={best['threshold']}, Δ${best['delta_pnl']:.0f}")

# ── EXP4: Compare Premium vs FR 8h ─────────────────────────────
log.info("EXP4: Premium signal activation vs FR signal activation...")

# Count how often premium signal fires vs FR 8h
prem_active = (np.abs(prem_z) >= 2.0).sum()
fr_col = btc_df["fr_8h"] if "fr_8h" in btc_df.columns else btc_df.get("last_funding_rate")
if fr_col is not None:
    fr_active = ((fr_col < -0.0001) | (fr_col > 0.0003)).sum()
else:
    fr_active = 0
total_bars = len(btc_df)

results["exp4_activation"] = {
    "premium_active_bars": int(prem_active),
    "premium_active_pct": round(prem_active / total_bars * 100, 2),
    "fr_8h_active_bars": int(fr_active),
    "fr_8h_active_pct": round(fr_active / total_bars * 100, 2),
    "premium_vs_fr_ratio": round(prem_active / max(fr_active, 1), 1),
}
log.info(f"  Premium active: {prem_active} bars ({prem_active/total_bars*100:.1f}%)")
log.info(f"  FR 8h active: {fr_active} bars ({fr_active/total_bars*100:.1f}%)")

# ── EXP5: Premium + Time-of-Day interaction ────────────────────
log.info("EXP5: Premium extremes by hour of day...")

merged["hour"] = merged["ts"].dt.hour
extreme_mask = np.abs(merged["prem_z"]) >= 2.0
hourly_extremes = merged[extreme_mask].groupby("hour").size()
hourly_total = merged.groupby("hour").size()
hourly_pct = (hourly_extremes / hourly_total * 100).fillna(0)

results["exp5_hourly_extremes"] = {
    int(h): round(p, 2) for h, p in hourly_pct.items()
}
top_hours = hourly_pct.nlargest(3)
log.info(f"  Top extreme hours: {dict(top_hours.round(1))}")

# ── EXP6: Agreement with existing FR ────────────────────────────
log.info("EXP6: Premium vs FR signal agreement...")

# When both premium and FR fire, do they agree?
if fr_col is not None and "premium_15m" in btc_df_m.columns:
    fr_signal = np.where(fr_col < -0.0001, 1, np.where(fr_col > 0.0003, -1, 0))
    prem_signal = np.where(prem_z < -2, 1, np.where(prem_z > 2, -1, 0))

    both_active = (fr_signal != 0) & (prem_signal != 0)
    if both_active.sum() > 0:
        agree = (fr_signal[both_active] == prem_signal[both_active]).sum()
        results["exp6_agreement"] = {
            "both_active": int(both_active.sum()),
            "agree": int(agree),
            "agree_pct": round(agree / both_active.sum() * 100, 1),
        }
        log.info(f"  Both active: {both_active.sum()}, agree: {agree} ({agree/both_active.sum()*100:.0f}%)")
    else:
        results["exp6_agreement"] = {"both_active": 0, "note": "never both active simultaneously"}
        log.info("  Never both active simultaneously")

    # Premium-only fires (FR silent)
    prem_only = (prem_signal != 0) & (fr_signal == 0)
    results["exp6_premium_only"] = {
        "count": int(prem_only.sum()),
        "pct_of_premium_signals": round(prem_only.sum() / max((prem_signal != 0).sum(), 1) * 100, 1),
    }
    log.info(f"  Premium-only signals: {prem_only.sum()}")

# ── Summary ─────────────────────────────────────────────────────
log.info("\n=== MISSION 037 SUMMARY ===")
log.info(f"Premium data: {results['exp1_distribution']['count']} rows")
log.info(f"Forward returns: see exp2")
log.info(f"Best factor config: w={best['weight']}, th={best['threshold']}, Δ${best['delta_pnl']}")
log.info(f"Baseline PnL: ${base_pnl:.0f}, Best PnL: ${best['pnl']:.0f}")

# ── Save Results ────────────────────────────────────────────────
mission_data = {
    "mission_id": "mission_037_premium_index",
    "date": datetime.utcnow().strftime("%Y-%m-%d"),
    "experiments": results,
}

out_json = BASE_DIR / "missions" / "mission_037_premium_index.json"
with open(out_json, "w", encoding="utf-8") as f:
    json.dump(mission_data, f, indent=2, ensure_ascii=False, default=str)

log.info(f"Saved: {out_json}")
print("\n✅ Mission 037 complete!")
print(json.dumps(results, indent=2, ensure_ascii=False, default=str))
