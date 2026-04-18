"""
Tournament R3 Validation: Test champion settings on ALL model versions
======================================================================
ทดสอบว่า hyst=3.0 + exit_only + cd_extra=4 ช่วย v3/v5 ด้วยหรือไม่
เปรียบเทียบ baseline (reverse, hyst=0) vs champion สำหรับทุก model
"""

import sys, os, time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR))
os.chdir(str(BASE_DIR))

import pandas as pd
import numpy as np
import json
from backtest_15m_btc_led_alts import (
    fetch_binance_15m, load_btc_db_data, build_alt_technicals,
    calc_metrics, INIT_EQUITY, BUDGET_USDT, LEVERAGE, FEE, SLIP,
)
from signal_core import (
    build_btc_features, compute_btc_composite_score_v6,
    DEFAULT_COMPOSITE_WEIGHTS, DEFAULT_EXTRA_WEIGHTS,
)

# Import tournament functions from round 3
from tournament.round_3_signal_flip.tournament_signal_flip import (
    gen_signal_with_options, run_backtest_flip, compute_exit_breakdown,
)

# ── Config ──────────────────────────────────────────────
OOS_START = "2025-01-01"
OOS_END   = "2026-03-24"
COINS = ["BTC", "XRP", "ADA", "DOT", "SUI", "FIL"]

# v3 configs (original per-coin)
V3_CONFIGS = {
    "BTC":  {"threshold": 2.5, "sl": 10.0, "tp": 5.0, "cd": 4},
    "XRP":  {"threshold": 3.5, "sl": 10.0, "tp": 5.0, "cd": 4},
    "ADA":  {"threshold": 3.5, "sl": 10.0, "tp": 5.0, "cd": 4},
    "DOT":  {"threshold": 3.0, "sl": 10.0, "tp": 5.0, "cd": 8},
    "SUI":  {"threshold": 3.0, "sl": 10.0, "tp": 5.0, "cd": 4},
    "FIL":  {"threshold": 3.0, "sl": 10.0, "tp": 5.0, "cd": 4},
}

# v5 configs
V5_CONFIGS = {
    "BTC":  {"threshold": 2.5, "sl": 15.0, "tp": 12.0, "cd": 4},
    "XRP":  {"threshold": 3.5, "sl": 15.0, "tp": 12.0, "cd": 4},
    "ADA":  {"threshold": 3.5, "sl": 15.0, "tp": 12.0, "cd": 4},
    "DOT":  {"threshold": 3.0, "sl": 15.0, "tp": 12.0, "cd": 4},
    "SUI":  {"threshold": 3.0, "sl": 15.0, "tp": 12.0, "cd": 4},
    "FIL":  {"threshold": 3.0, "sl": 15.0, "tp": 12.0, "cd": 4},
}

# v6 configs
V6_CONFIGS = {
    "BTC":  {"threshold": 2.5, "sl": 25.0, "tp": 20.0, "cd": 4},
    "XRP":  {"threshold": 3.5, "sl": 25.0, "tp": 20.0, "cd": 4},
    "ADA":  {"threshold": 3.5, "sl": 25.0, "tp": 20.0, "cd": 4},
    "DOT":  {"threshold": 3.0, "sl": 25.0, "tp": 20.0, "cd": 4},
    "SUI":  {"threshold": 3.0, "sl": 25.0, "tp": 20.0, "cd": 4},
    "FIL":  {"threshold": 3.0, "sl": 25.0, "tp": 20.0, "cd": 4},
}

# v5 weights
V5_COMPOSITE_WEIGHTS = {
    "w_oi_bull": 0.25, "w_oi_capit": 0.25, "w_oi_weak": 0.25, "w_oi_bear": 0.25,
    "w_fr_neg": 2.0, "w_fr_pos": 2.0,
    "w_whale_bull": 1.5, "w_whale_bear": 1.5,
    "w_liq_bull": 5.0, "w_liq_bear": 5.0,
    "w_etf_bull": 1.0, "w_etf_bear": 1.0,
}
V5_EXTRA_WEIGHTS = {"ob_combined": 2.0, "basis_contrarian": 1.5, "tick_liq": 3.0}

# Test scenarios
SCENARIOS = [
    {"name": "baseline",  "flip_mode": "reverse",   "hyst": 0.0, "cd_extra": 0, "confirm": 1},
    {"name": "champion",  "flip_mode": "exit_only", "hyst": 3.0, "cd_extra": 4, "confirm": 1},
    {"name": "hyst_1.5",  "flip_mode": "reverse",   "hyst": 1.5, "cd_extra": 0, "confirm": 1},
    {"name": "hyst_2.0",  "flip_mode": "reverse",   "hyst": 2.0, "cd_extra": 0, "confirm": 1},
    {"name": "disabled",  "flip_mode": "disabled",  "hyst": 0.0, "cd_extra": 0, "confirm": 1},
]


# ══════════════════════════════════════════════════════════
# DATA LOADING
# ══════════════════════════════════════════════════════════

print("=" * 80)
print("VALIDATION: Champion Settings on ALL Model Versions (v3, v5, v6)")
print("=" * 80)

t0 = time.time()
btc_ohlcv = fetch_binance_15m("BTCUSDT", years=3)
db_data = load_btc_db_data()
btc_df = build_btc_features(btc_ohlcv, db_data)
print(f"BTC features: {len(btc_df)} bars ({time.time()-t0:.1f}s)")

# Compute scores for each model
import backtest_15m_btc_led_alts as bt

# v3 score
v3_score = compute_btc_composite_score_v6.__wrapped__(btc_df) if hasattr(compute_btc_composite_score_v6, '__wrapped__') else None
old_extra = dict(bt.V3_EXTRA_WEIGHTS)
v3_score = bt.compute_btc_composite_score(btc_df)
v3_score_ts = pd.Series(v3_score.values, index=btc_df["ts"].values)
print(f"V3 score: range [{v3_score.min():.1f}, {v3_score.max():.1f}]")

# v5 score
bt.V3_EXTRA_WEIGHTS.update(V5_EXTRA_WEIGHTS)
v5_score = bt.compute_btc_composite_score(btc_df, params=V5_COMPOSITE_WEIGHTS)
bt.V3_EXTRA_WEIGHTS.update(old_extra)
v5_score_ts = pd.Series(v5_score.values, index=btc_df["ts"].values)
print(f"V5 score: range [{v5_score.min():.1f}, {v5_score.max():.1f}]")

# v6 score
v6_score = compute_btc_composite_score_v6(btc_df)
v6_score_ts = pd.Series(v6_score.values, index=btc_df["ts"].values)
print(f"V6 score: range [{v6_score.min():.1f}, {v6_score.max():.1f}]")

# Alt data
alt_data = {}
for coin in COINS:
    ohlcv = fetch_binance_15m(f"{coin}USDT", years=3)
    alt_data[coin] = build_alt_technicals(ohlcv)
print(f"Data loaded in {time.time()-t0:.1f}s\n")


# ══════════════════════════════════════════════════════════
# RUN TESTS
# ══════════════════════════════════════════════════════════

def run_model_test(model_name, score_ts, configs, scenario):
    """Run one model + one scenario across all coins."""
    all_trades = []
    for coin in COINS:
        cfg = configs[coin]
        sig, alt_merged = gen_signal_with_options(
            score_ts, alt_data[coin],
            entry_threshold=cfg["threshold"],
            hysteresis_band=scenario["hyst"],
            confirm_bars=scenario["confirm"],
        )
        oos_mask = (alt_merged["ts"] >= pd.Timestamp(OOS_START))
        if OOS_END:
            oos_mask &= (alt_merged["ts"] <= pd.Timestamp(OOS_END))
        alt_oos = alt_merged[oos_mask].reset_index(drop=True)
        sig_oos = sig[oos_mask].reset_index(drop=True)
        if len(alt_oos) < 100:
            continue

        trades = run_backtest_flip(
            alt_oos, sig_oos,
            sl_atr_mult=cfg["sl"], tp_atr_mult=cfg["tp"],
            max_hold_bars=96, cooldown_bars=cfg["cd"],
            flip_mode=scenario["flip_mode"],
            min_bars_before_flip=0,
            flip_cooldown_extra=scenario["cd_extra"],
        )
        if len(trades) > 0:
            trades["coin"] = coin
            all_trades.append(trades)

    if not all_trades:
        return {"pnl": 0, "trades": 0, "wr": 0, "sharpe": 0, "dd": 0,
                "flip_n": 0, "flip_pnl": 0, "flip_wr": 0}

    combined = pd.concat(all_trades, ignore_index=True)
    total_pnl = combined["pnl_net"].sum()
    total_trades = len(combined)
    total_wr = 100 * (combined["pnl_net"] > 0).sum() / total_trades
    equity = INIT_EQUITY + combined["pnl_net"].cumsum()
    max_dd = ((equity - equity.cummax()) / equity.cummax() * 100).min()
    ret = combined["pnl_net"] / BUDGET_USDT
    sharpe = ret.mean() / ret.std() * np.sqrt(total_trades) if ret.std() > 0 else 0

    bd = compute_exit_breakdown(combined)
    sf = bd.get("SIGNAL_FLIP", {"count": 0, "pnl": 0, "wr": 0})

    return {
        "pnl": round(total_pnl, 0),
        "trades": total_trades,
        "wr": round(total_wr, 1),
        "sharpe": round(sharpe, 2),
        "dd": round(max_dd, 1),
        "flip_n": sf["count"],
        "flip_pnl": round(sf["pnl"], 0),
        "flip_wr": round(sf["wr"], 1),
    }


MODELS = [
    ("v3", v3_score_ts, V3_CONFIGS),
    ("v5", v5_score_ts, V5_CONFIGS),
    ("v6", v6_score_ts, V6_CONFIGS),
]

all_results = {}

for model_name, score_ts, configs in MODELS:
    print(f"\n{'#'*80}")
    print(f"# MODEL: {model_name}")
    print(f"{'#'*80}")

    model_results = []
    for sc in SCENARIOS:
        t1 = time.time()
        r = run_model_test(model_name, score_ts, configs, sc)
        elapsed = time.time() - t1
        r["name"] = sc["name"]
        r["elapsed"] = round(elapsed, 1)
        model_results.append(r)
        print(f"  {sc['name']:<12} {r['trades']:>5} trades | WR {r['wr']:>5.1f}% | "
              f"PnL ${r['pnl']:>+9,.0f} | Sharpe {r['sharpe']:>6.2f} | DD {r['dd']:>5.1f}% | "
              f"FLIP: {r['flip_n']:>5} ${r['flip_pnl']:>+8,.0f} ({r['flip_wr']:.0f}% WR)")

    all_results[model_name] = model_results


# ══════════════════════════════════════════════════════════
# COMPARISON TABLE
# ══════════════════════════════════════════════════════════

print(f"\n{'='*100}")
print("CROSS-MODEL COMPARISON: baseline vs champion")
print(f"{'='*100}")
print(f"\n{'Model':<6} {'Scenario':<12} {'Trades':>6} {'WR%':>6} {'PnL':>10} {'Sharpe':>7} "
      f"{'DD%':>6} {'FlipN':>6} {'FlipPnL':>9} {'Delta PnL':>10}")
print("-" * 100)

for model_name in ["v3", "v5", "v6"]:
    results = all_results[model_name]
    baseline_pnl = results[0]["pnl"]  # first is baseline
    for r in results:
        delta = r["pnl"] - baseline_pnl
        marker = " ***" if r["name"] == "champion" else ""
        print(f"{model_name:<6} {r['name']:<12} {r['trades']:>6} {r['wr']:>5.1f}% "
              f"${r['pnl']:>9,.0f} {r['sharpe']:>7.2f} {r['dd']:>5.1f}% "
              f"{r['flip_n']:>6} ${r['flip_pnl']:>+8,.0f} ${delta:>+9,.0f}{marker}")
    print()

# Summary verdict
print(f"{'='*100}")
print("VERDICT")
print(f"{'='*100}")
for model_name in ["v3", "v5", "v6"]:
    results = all_results[model_name]
    baseline = results[0]
    champion = results[1]
    delta = champion["pnl"] - baseline["pnl"]
    pct = delta / max(abs(baseline["pnl"]), 1) * 100
    better = "BETTER" if delta > 0 else "WORSE"
    print(f"  {model_name}: champion vs baseline = ${delta:+,.0f} ({pct:+.1f}%) => {better}")
    # Find best scenario for this model
    best = max(results, key=lambda x: x["pnl"])
    if best["name"] != "champion":
        print(f"         Best for {model_name} = {best['name']} (${best['pnl']:+,.0f})")

# Save results
results_file = Path(__file__).parent / "validation_results.json"
with open(results_file, "w") as f:
    json.dump(all_results, f, indent=2, default=str)
print(f"\nResults saved to {results_file}")
print(f"Total elapsed: {time.time()-t0:.0f}s")
