"""
Test extreme_conf3 filter on ALL coins (v3/v4/v5)
===================================================
Validates Mission 013 finding: skip Extreme vol trades when < 3 factors active.
Original test was on 6 coins only. This tests all 46.

Groups:
  v3 (19 coins) - uses v3 BTC score weights
  v4 (12 coins) - uses v3 BTC score weights (same model)
  v5 (15 coins) - uses v5 BTC score weights (liq=5, tick=3)
"""

import sys, json, time
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import backtest_15m_btc_led_alts as bt
from paper_trading.config import (
    COMPOSITE_WEIGHTS, V3_EXTRA_WEIGHTS,
    V5_COMPOSITE_WEIGHTS, V5_EXTRA_WEIGHTS,
    COIN_CONFIGS, COINS_V3, COINS_V4, COINS_V5,
)
from paper_trading.strategy import score_ob_combined, score_basis_contrarian, score_tick_liq

# ---- Config ----
OOS_START = "2025-01-01"
OOS_END = "2026-03-31"
VOL_LOOKBACK = 96  # 24h rolling

started_at = datetime.utcnow()

# ══════════════════════════════════════════════════════════════
# Step 0: Load BTC data & compute factor scores
# ══════════════════════════════════════════════════════════════
print("=" * 70)
print("Extreme_Conf3 Filter Test: ALL COINS (v3/v4/v5)")
print("=" * 70)

print("\n[0] Loading BTC data...")
btc_ohlcv = bt.fetch_binance_15m("BTCUSDT", years=3)
if "date_time" in btc_ohlcv.columns:
    btc_ohlcv = btc_ohlcv.rename(columns={"date_time": "ts"})

db_data = bt.load_btc_db_data()
btc_df = bt.build_btc_features(btc_ohlcv, db_data)

# ---- Factor decomposition (same as mission 013) ----
def compute_individual_factor_scores(df, params, extra):
    scores = {}
    # 1. OI divergence
    s_oi = pd.Series(0.0, index=df.index)
    if "oi_chg" in df.columns:
        oi_chg = df["oi_chg"].fillna(0)
        ret = df["ret"].fillna(0)
        s_oi += np.where((ret > 0.001) & (oi_chg > 0.002), params.get("w_oi_bull", 0.25), 0)
        s_oi += np.where((ret < -0.001) & (oi_chg < -0.002), params.get("w_oi_capit", 0.25), 0)
        s_oi += np.where((ret > 0.001) & (oi_chg < -0.002), -params.get("w_oi_weak", 0.25), 0)
        s_oi += np.where((ret < -0.001) & (oi_chg > 0.002), -params.get("w_oi_bear", 0.25), 0)
    scores["oi_divergence"] = s_oi
    # 2. Funding rate
    s_fr = pd.Series(0.0, index=df.index)
    if "fr_8h" in df.columns:
        fr = df["fr_8h"].fillna(0)
        s_fr += np.where(fr < -0.0001, params.get("w_fr_neg", 2.0), 0)
        s_fr += np.where(fr > 0.0003, -params.get("w_fr_pos", 2.0), 0)
    elif "last_funding_rate" in df.columns:
        fr = df["last_funding_rate"].fillna(0)
        s_fr += np.where(fr < -0.00005, params.get("w_fr_neg", 2.0), 0)
        s_fr += np.where(fr > 0.0002, -params.get("w_fr_pos", 2.0), 0)
    scores["funding_rate"] = s_fr
    # 3. Whale alerts
    s_wh = pd.Series(0.0, index=df.index)
    if "whale_net_ma" in df.columns:
        wn_ma = df["whale_net_ma"].fillna(0)
        s_wh += np.where(wn_ma > 50_000_000, params.get("w_whale_bull", 1.5), 0)
        s_wh += np.where(wn_ma < -50_000_000, -params.get("w_whale_bear", 1.5), 0)
    scores["whale_alerts"] = s_wh
    # 4. Liquidation cascades
    s_liq = pd.Series(0.0, index=df.index)
    if "liq_net" in df.columns and "liq_total_ma" in df.columns:
        lt = df["liq_total"].fillna(0)
        lt_ma = df["liq_total_ma"].fillna(1)
        ln = df["liq_net"].fillna(0)
        cascade = lt > (lt_ma * 3)
        s_liq += np.where(cascade & (ln > 0), params.get("w_liq_bull", 2.0), 0)
        s_liq += np.where(cascade & (ln < 0), -params.get("w_liq_bear", 2.0), 0)
    scores["liquidation"] = s_liq
    # 5. ETF flows
    s_etf = pd.Series(0.0, index=df.index)
    if "etf_flow_ma" in df.columns:
        etf_ma = df["etf_flow_ma"].fillna(0)
        s_etf += np.where(etf_ma > 50, params.get("w_etf_bull", 1.0), 0)
        s_etf += np.where(etf_ma < -50, -params.get("w_etf_bear", 1.0), 0)
    scores["etf_flows"] = s_etf
    # 6-8. Extra factors
    scores["basis_contrarian"] = score_basis_contrarian(df, weight=extra.get("basis_contrarian", 1.5))
    scores["tick_liq"] = score_tick_liq(df, weight=extra.get("tick_liq", 2.0))
    scores["ob_combined"] = score_ob_combined(df, weight=extra.get("ob_combined", 2.0))
    return scores

# ---- Build v3 and v5 BTC scores ----
print("  Computing v3 BTC score + factors...")
v3_factor_scores = compute_individual_factor_scores(btc_df, COMPOSITE_WEIGHTS, V3_EXTRA_WEIGHTS)
v3_btc_score = bt.compute_btc_composite_score(btc_df, COMPOSITE_WEIGHTS)
v3_btc_score = v3_btc_score + score_ob_combined(btc_df, weight=V3_EXTRA_WEIGHTS["ob_combined"])
v3_btc_score = v3_btc_score + score_basis_contrarian(btc_df, weight=V3_EXTRA_WEIGHTS["basis_contrarian"])
v3_btc_score = v3_btc_score + score_tick_liq(btc_df, weight=V3_EXTRA_WEIGHTS["tick_liq"])
v3_score_ts = pd.Series(v3_btc_score.values, index=btc_df["ts"].values, name="btc_score")

print("  Computing v5 BTC score + factors...")
v5_factor_scores = compute_individual_factor_scores(btc_df, V5_COMPOSITE_WEIGHTS, V5_EXTRA_WEIGHTS)
v5_btc_score = bt.compute_btc_composite_score(btc_df, V5_COMPOSITE_WEIGHTS)
v5_btc_score = v5_btc_score + score_ob_combined(btc_df, weight=V5_EXTRA_WEIGHTS["ob_combined"])
v5_btc_score = v5_btc_score + score_basis_contrarian(btc_df, weight=V5_EXTRA_WEIGHTS["basis_contrarian"])
v5_btc_score = v5_btc_score + score_tick_liq(btc_df, weight=V5_EXTRA_WEIGHTS["tick_liq"])
v5_score_ts = pd.Series(v5_btc_score.values, index=btc_df["ts"].values, name="btc_score")

# ---- Factor DataFrames (for active count) ----
FACTOR_NAMES = list(v3_factor_scores.keys())

# Active factor count: conditions are same for v3/v5 (only weights differ, abs>0 is same)
factor_df = pd.DataFrame({k: v.values for k, v in v3_factor_scores.items()},
                          index=btc_df["ts"].values)
active_count_series = (factor_df[FACTOR_NAMES].abs() > 0).sum(axis=1)

# ---- Vol regime classification ----
btc_df["log_ret"] = np.log(btc_df["close"] / btc_df["close"].shift(1))
btc_df["realized_vol"] = btc_df["log_ret"].rolling(VOL_LOOKBACK).std() * np.sqrt(4 * 24 * 365)

oos_mask = (btc_df["ts"] >= OOS_START) & (btc_df["ts"] <= OOS_END)
btc_oos = btc_df[oos_mask].copy()
rv = btc_oos["realized_vol"].dropna()
q25, q75, q90 = rv.quantile([0.25, 0.75, 0.9]).values

def classify_vol(v):
    if pd.isna(v): return "Unknown"
    if v <= q25: return "Low"
    elif v <= q75: return "Normal"
    elif v <= q90: return "High"
    else: return "Extreme"

btc_oos["vol_regime"] = btc_oos["realized_vol"].apply(classify_vol)
vol_regime_lookup = pd.Series(btc_oos["vol_regime"].values, index=btc_oos["ts"].values)

print(f"  Vol quantiles: Low<{q25:.4f}, Normal<{q75:.4f}, High<{q90:.4f}, Extreme>{q90:.4f}")

# ══════════════════════════════════════════════════════════════
# Step 1: Run backtest on ALL coins
# ══════════════════════════════════════════════════════════════
print("\n[1] Running backtest on all coins...")

version_map = {}
for c in COINS_V3: version_map[c] = "v3"
for c in COINS_V4: version_map[c] = "v4"
for c in COINS_V5: version_map[c] = "v5"

all_trades = []
coin_stats = []
skipped = []

all_coins = COINS_V3 + COINS_V4 + COINS_V5
for i, coin in enumerate(all_coins):
    ver = version_map[coin]
    cfg = COIN_CONFIGS.get(coin, {})
    symbol = cfg.get("symbol", f"{coin}USDT")

    # Pick correct BTC score for this coin's model
    btc_score_ts = v5_score_ts if ver == "v5" else v3_score_ts

    try:
        ohlcv = bt.fetch_binance_15m(symbol, years=3)
    except Exception as e:
        skipped.append((coin, ver, str(e)))
        continue

    if "date_time" in ohlcv.columns:
        ohlcv = ohlcv.rename(columns={"date_time": "ts"})

    alt_df = bt.build_alt_technicals(ohlcv)
    oos_m = (alt_df["ts"] >= OOS_START) & (alt_df["ts"] <= OOS_END)
    if oos_m.sum() < 100:
        skipped.append((coin, ver, f"only {oos_m.sum()} OOS bars"))
        continue

    signals, alt_merged = bt.generate_btc_led_signal(
        btc_score_ts, alt_df[oos_m],
        threshold=cfg.get("threshold", 3.0),
        use_alt_pa_filter=cfg.get("use_alt_pa_filter", False))

    trades = bt.run_backtest(
        alt_merged, signals,
        sl_atr_mult=cfg.get("sl_atr_mult", 10.0),
        tp_atr_mult=cfg.get("tp_atr_mult", 5.0),
        cooldown_bars=cfg.get("cooldown_bars", 4))

    if len(trades) > 0:
        trades["coin"] = coin
        trades["version"] = ver
        all_trades.append(trades)
        coin_stats.append({"coin": coin, "ver": ver, "trades": len(trades),
                          "pnl": round(trades["pnl_net"].sum(), 1)})

    prog = f"[{i+1}/{len(all_coins)}]"
    status = f"{len(trades)} trades" if len(trades) > 0 else "0 trades"
    print(f"  {prog} {coin:<12} ({ver}) {status}")

if skipped:
    print(f"\n  Skipped {len(skipped)} coins: {[s[0] for s in skipped]}")

trades_df = pd.concat(all_trades, ignore_index=True)
print(f"\n  Total: {len(trades_df)} trades from {len(coin_stats)} coins")

# ══════════════════════════════════════════════════════════════
# Step 2: Map factor count + vol regime to each trade
# ══════════════════════════════════════════════════════════════
print("\n[2] Mapping factor count + vol regime to trades...")

# Vectorized lookup using merge_asof (much faster than iterrows)
trade_times = pd.DataFrame({"entry_time": trades_df["entry_time"]})
trade_times = trade_times.sort_values("entry_time").reset_index()

# Active factor count lookup
af_df = pd.DataFrame({"ts": factor_df.index, "active_factors": active_count_series.values})
af_df = af_df.sort_values("ts")
merged_af = pd.merge_asof(
    trade_times.rename(columns={"entry_time": "ts"}),
    af_df, on="ts", direction="backward"
)

# Vol regime lookup
vr_df = pd.DataFrame({"ts": vol_regime_lookup.index, "vol_regime": vol_regime_lookup.values})
vr_df = vr_df.sort_values("ts")
merged_vr = pd.merge_asof(
    trade_times.rename(columns={"entry_time": "ts"}),
    vr_df, on="ts", direction="backward"
)

# Map back to original index
idx_to_af = dict(zip(merged_af["index"], merged_af["active_factors"].fillna(0).astype(int)))
idx_to_vr = dict(zip(merged_vr["index"], merged_vr["vol_regime"].fillna("Unknown")))

trades_df["active_factors"] = trades_df.index.map(idx_to_af)
trades_df["vol_regime"] = trades_df.index.map(idx_to_vr)

# ══════════════════════════════════════════════════════════════
# Step 3: Analyze baseline vs extreme_conf3 per version
# ══════════════════════════════════════════════════════════════
print("\n[3] Results: Baseline vs Extreme_Conf3")
print("=" * 70)

def compute_metrics(df_trades):
    if len(df_trades) == 0:
        return {"trades": 0, "wr": 0, "pnl": 0, "max_dd": 0, "calmar": 0, "avg_pnl": 0}
    pnl = df_trades["pnl_net"].sum()
    wr = (df_trades["pnl_net"] > 0).mean() * 100
    cumsum = df_trades["pnl_net"].cumsum()
    max_dd = (cumsum - cumsum.cummax()).min()
    calmar = round(pnl / max(-max_dd, 1), 2) if max_dd < 0 else round(pnl, 2)
    avg_pnl = df_trades["pnl_net"].mean()
    return {
        "trades": len(df_trades),
        "wr": round(wr, 1),
        "pnl": round(pnl, 1),
        "max_dd": round(max_dd, 1),
        "calmar": calmar,
        "avg_pnl": round(avg_pnl, 2),
    }

def apply_extreme_conf3(df_trades):
    """Keep trades unless Extreme vol + active_factors < 3."""
    mask = ~((df_trades["vol_regime"] == "Extreme") & (df_trades["active_factors"] < 3))
    return df_trades[mask]

results = {}
versions = ["v3", "v4", "v5", "ALL"]

for ver in versions:
    if ver == "ALL":
        subset = trades_df
    else:
        subset = trades_df[trades_df["version"] == ver]

    if len(subset) == 0:
        print(f"\n  {ver}: no trades")
        continue

    baseline = compute_metrics(subset)
    filtered = apply_extreme_conf3(subset)
    filtered_m = compute_metrics(filtered)

    # Extreme regime breakdown
    extreme_low = subset[(subset["vol_regime"] == "Extreme") & (subset["active_factors"] < 3)]
    extreme_high = subset[(subset["vol_regime"] == "Extreme") & (subset["active_factors"] >= 3)]

    dropped_trades = baseline["trades"] - filtered_m["trades"]
    dropped_pct = dropped_trades / baseline["trades"] * 100 if baseline["trades"] > 0 else 0
    delta_pnl = filtered_m["pnl"] - baseline["pnl"]
    delta_calmar_pct = ((filtered_m["calmar"] - baseline["calmar"]) / baseline["calmar"] * 100
                        if baseline["calmar"] > 0 else 0)

    print(f"\n{'-' * 70}")
    print(f"  {ver.upper()} ({len(subset)} trades, {len(subset['coin'].unique())} coins)")
    print(f"{'-' * 70}")
    print(f"  {'':25} {'Trades':>7} {'WR%':>7} {'PnL':>10} {'MaxDD':>9} {'Calmar':>8} {'Avg':>8}")
    print(f"  {'Baseline':<25} {baseline['trades']:>7} {baseline['wr']:>7.1f} {baseline['pnl']:>10.1f} {baseline['max_dd']:>9.1f} {baseline['calmar']:>8.2f} {baseline['avg_pnl']:>8.2f}")
    print(f"  {'+ extreme_conf3':<25} {filtered_m['trades']:>7} {filtered_m['wr']:>7.1f} {filtered_m['pnl']:>10.1f} {filtered_m['max_dd']:>9.1f} {filtered_m['calmar']:>8.2f} {filtered_m['avg_pnl']:>8.2f}")
    print(f"  {'':25} {'-' * 56}")
    print(f"  {'Delta':<25} {-dropped_trades:>+7} {filtered_m['wr']-baseline['wr']:>+7.1f} {delta_pnl:>+10.1f} {filtered_m['max_dd']-baseline['max_dd']:>+9.1f} {delta_calmar_pct:>+7.0f}%")

    if len(extreme_low) > 0:
        el_m = compute_metrics(extreme_low)
        print(f"\n  Extreme + <3 factors (DROPPED):")
        print(f"    {el_m['trades']} trades, WR {el_m['wr']}%, PnL ${el_m['pnl']}, avg ${el_m['avg_pnl']}/trade")

    if len(extreme_high) > 0:
        eh_m = compute_metrics(extreme_high)
        print(f"  Extreme + 3+ factors (KEPT):")
        print(f"    {eh_m['trades']} trades, WR {eh_m['wr']}%, PnL ${eh_m['pnl']}, avg ${eh_m['avg_pnl']}/trade")

    results[ver] = {
        "n_coins": len(subset["coin"].unique()),
        "baseline": baseline,
        "extreme_conf3": filtered_m,
        "delta_pnl": round(delta_pnl, 1),
        "delta_calmar_pct": round(delta_calmar_pct, 1),
        "dropped_trades": dropped_trades,
        "dropped_pct": round(dropped_pct, 1),
        "extreme_low_conf": compute_metrics(extreme_low) if len(extreme_low) > 0 else None,
        "extreme_high_conf": compute_metrics(extreme_high) if len(extreme_high) > 0 else None,
    }

# ══════════════════════════════════════════════════════════════
# Step 4: Per-coin breakdown (which coins benefit most/least)
# ══════════════════════════════════════════════════════════════
print(f"\n{'=' * 70}")
print("[4] Per-Coin Delta (extreme_conf3 vs baseline)")
print(f"{'=' * 70}")

coin_deltas = []
for coin in trades_df["coin"].unique():
    ct = trades_df[trades_df["coin"] == coin]
    base_pnl = ct["pnl_net"].sum()
    filt = apply_extreme_conf3(ct)
    filt_pnl = filt["pnl_net"].sum()
    delta = filt_pnl - base_pnl
    dropped = len(ct) - len(filt)
    coin_deltas.append({
        "coin": coin,
        "ver": version_map[coin],
        "base_trades": len(ct),
        "base_pnl": round(base_pnl, 1),
        "filt_pnl": round(filt_pnl, 1),
        "delta_pnl": round(delta, 1),
        "dropped": dropped,
    })

coin_deltas.sort(key=lambda x: x["delta_pnl"], reverse=True)

print(f"  {'Coin':<12} {'Ver':>4} {'Trades':>7} {'Base PnL':>10} {'Filt PnL':>10} {'Delta':>8} {'Dropped':>8}")
print(f"  {'-' * 65}")
for cd in coin_deltas:
    marker = " ***" if cd["delta_pnl"] < -50 else ""
    print(f"  {cd['coin']:<12} {cd['ver']:>4} {cd['base_trades']:>7} {cd['base_pnl']:>10.1f} {cd['filt_pnl']:>10.1f} {cd['delta_pnl']:>+8.1f} {cd['dropped']:>8}{marker}")

# Count coins that benefit vs hurt
benefit = sum(1 for cd in coin_deltas if cd["delta_pnl"] >= 0)
hurt = sum(1 for cd in coin_deltas if cd["delta_pnl"] < 0)
print(f"\n  Benefit: {benefit} coins | Hurt: {hurt} coins")

# ══════════════════════════════════════════════════════════════
# Summary & Save
# ══════════════════════════════════════════════════════════════
finished_at = datetime.utcnow()
duration = (finished_at - started_at).total_seconds()

print(f"\n{'=' * 70}")
print("FINAL VERDICT")
print(f"{'=' * 70}")

for ver in ["v3", "v4", "v5", "ALL"]:
    if ver not in results:
        continue
    r = results[ver]
    emoji = "OK" if r["delta_pnl"] >= 0 else "SKIP"
    print(f"  {ver:>4}: PnL {r['delta_pnl']:>+8.1f} | Calmar {r['delta_calmar_pct']:>+6.1f}% | Dropped {r['dropped_pct']:.1f}% trades | [{emoji}]")

print(f"\n  Duration: {duration:.1f}s")

# Save
output = {
    "test": "extreme_conf3_all_coins",
    "date": started_at.isoformat(),
    "oos_period": f"{OOS_START} to {OOS_END}",
    "results_by_version": results,
    "per_coin": coin_deltas,
    "skipped": [{"coin": s[0], "ver": s[1], "reason": s[2]} for s in skipped],
    "duration_sec": round(duration, 1),
}

out_path = BASE_DIR / "experiments" / "extreme_conf3_all_coins.json"
out_path.parent.mkdir(exist_ok=True)
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(output, f, indent=2, ensure_ascii=False, default=str)
print(f"  Saved: {out_path}")
