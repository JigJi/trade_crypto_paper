"""
Experiment: Short Bias on v4 (v3 + stable_supply)
==================================================
Tests generate_signal_short_bias (offset=0.5) on v4 composite score.
v4 = v3 + stable_supply(w=1.0)

OOS: Jan-Mar 2026 | 6 coins | 2x leverage

Run by subagent in parallel.
"""

import os, sys, json, warnings
from datetime import datetime

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

sys.path.insert(0, r"D:\0_product_dev\trade_crypto")
from backtest_15m_btc_led_alts import (
    fetch_binance_15m, run_backtest, calc_metrics, BKK_UTC_OFFSET,
)
from test_v12_improvements import (
    V11_CONFIGS, ALL_COINS, generate_signal_v11, generate_signal_short_bias,
)
from paper_trading.strategy import (
    compute_btc_composite_score, build_btc_features, build_alt_technicals,
)
from test_phase1_factors import load_btc_db_data_v3
from test_v4_factors import load_stablecoin_supply, score_stable_supply

import backtest_15m_btc_led_alts as bt

OOS_START = pd.Timestamp("2026-01-01")
OOS_END   = pd.Timestamp("2026-03-10")
SHORT_OFFSET = 0.5
STABLE_WEIGHT = 1.0


def run_experiment(signal_fn, signal_kwargs, btc_score_ts, label):
    """Run backtest across all coins with given signal function."""
    old_lev = bt.LEVERAGE
    bt.LEVERAGE = 2.0

    total_pnl = 0.0
    total_trades = 0
    results = {}

    for coin in ALL_COINS:
        cfg = V11_CONFIGS[coin]
        symbol = f"{coin}USDT"
        alt_raw = fetch_binance_15m(symbol, years=3)
        alt_df = build_alt_technicals(alt_raw)
        alt_df = alt_df[(alt_df["ts"] >= OOS_START) & (alt_df["ts"] <= OOS_END)].copy()

        if alt_df.empty:
            continue

        signal, alt_merged = signal_fn(
            btc_score_ts, alt_df, cfg["threshold"], cfg["alt_pa"],
            **signal_kwargs
        )
        trades = run_backtest(
            alt_merged, signal,
            sl_atr_mult=cfg["sl"], tp_atr_mult=cfg["tp"],
            trail_atr_mult=cfg["trail"], trail_activate_atr=cfg["trail_act"],
            max_hold_bars=96, cooldown_bars=cfg["cd"],
        )

        if not trades.empty:
            m = calc_metrics(trades, len(alt_df))
            pnl = m["net_pnl"]
            results[coin] = {
                "pnl": round(pnl, 2),
                "trades": m["total"],
                "wr": round(m["win_rate"], 1),
                "sharpe": round(m["sharpe"], 2),
                "wr_long": round(m.get("wr_long", 0), 1),
                "wr_short": round(m.get("wr_short", 0), 1),
                "n_long": m.get("n_long", 0),
                "n_short": m.get("n_short", 0),
            }
        else:
            pnl = 0
            results[coin] = {"pnl": 0, "trades": 0, "wr": 0, "sharpe": 0}

        total_pnl += pnl
        total_trades += results[coin]["trades"]

    bt.LEVERAGE = old_lev
    return total_pnl, total_trades, results


def main():
    print("=" * 70)
    print("EXPERIMENT: Short Bias on v4 (v3 + stable_supply)")
    print(f"OOS: {OOS_START} to {OOS_END} | 6 coins | 2x leverage")
    print("=" * 70)

    # ---- 1. Load data & compute v3 score ----
    print("\n[1] Loading BTC data + v3 factors...")
    btc_raw = fetch_binance_15m("BTCUSDT", years=3)
    db_data = load_btc_db_data_v3()
    btc_df = build_btc_features(btc_raw, db_data)
    btc_df = btc_df[btc_df["ts"] >= pd.Timestamp("2025-06-01")].copy().reset_index(drop=True)

    # ---- 2. Merge stable_supply to build v4 score ----
    print("\n[2] Loading stablecoin supply and merging...")
    stable_df = load_stablecoin_supply()
    if stable_df is not None:
        stable_cols = ["ts", "stable_total", "stable_chg7d"]
        existing = [c for c in stable_cols if c in stable_df.columns]
        btc_df = pd.merge_asof(
            btc_df.sort_values("ts"),
            stable_df[existing].sort_values("ts"),
            on="ts", direction="backward", tolerance=pd.Timedelta("3d")
        )
        print(f"  Stablecoin data merged: {btc_df['stable_chg7d'].notna().sum():,} / {len(btc_df):,} bars")

    # Compute v3 base score
    v3_score = compute_btc_composite_score(btc_df)
    # Add stable_supply to get v4 score
    v4_score = v3_score + score_stable_supply(btc_df, weight=STABLE_WEIGHT)

    v4_score_ts = v4_score.copy()
    v4_score_ts.index = btc_df["ts"]

    # Also prepare v3 score for comparison
    v3_score_ts = v3_score.copy()
    v3_score_ts.index = btc_df["ts"]

    # ---- 3. V4 Baseline (no short bias) ----
    print("\n[3] Running v4 BASELINE (no short bias)...")
    base_pnl, base_trades, base_results = run_experiment(
        generate_signal_v11, {}, v4_score_ts, "v4_baseline"
    )
    print(f"\n  V4 BASELINE: ${base_pnl:,.0f} ({base_trades} trades)")
    for coin, r in base_results.items():
        print(f"    {coin}: ${r['pnl']:,.0f} ({r['trades']} tr, WR {r['wr']:.1f}%)")

    # ---- 4. V4 + Short Bias ----
    print(f"\n[4] Running v4 + SHORT BIAS (offset={SHORT_OFFSET})...")
    sb_pnl, sb_trades, sb_results = run_experiment(
        generate_signal_short_bias, {"short_offset": SHORT_OFFSET},
        v4_score_ts, "v4_short_bias"
    )
    print(f"\n  V4 + SHORT BIAS: ${sb_pnl:,.0f} ({sb_trades} trades)")
    for coin, r in sb_results.items():
        print(f"    {coin}: ${r['pnl']:,.0f} ({r['trades']} tr, WR {r['wr']:.1f}%, L:{r['n_long']} S:{r['n_short']})")

    # ---- 5. V3 baseline for reference ----
    print("\n[5] Running v3 BASELINE for reference...")
    v3_pnl, v3_trades, v3_results = run_experiment(
        generate_signal_v11, {}, v3_score_ts, "v3_baseline"
    )
    print(f"\n  V3 BASELINE (ref): ${v3_pnl:,.0f} ({v3_trades} trades)")

    # ---- 6. Comparison ----
    delta_vs_v4 = sb_pnl - base_pnl
    delta_vs_v3 = sb_pnl - v3_pnl
    pct_vs_v4 = (delta_vs_v4 / abs(base_pnl) * 100) if base_pnl != 0 else 0
    pct_vs_v3 = (delta_vs_v3 / abs(v3_pnl) * 100) if v3_pnl != 0 else 0

    print(f"\n{'='*70}")
    print(f"  RESULTS SUMMARY")
    print(f"  V3 baseline:       ${v3_pnl:,.0f} ({v3_trades} trades)")
    print(f"  V4 baseline:       ${base_pnl:,.0f} ({base_trades} trades)")
    print(f"  V4 + short_bias:   ${sb_pnl:,.0f} ({sb_trades} trades)")
    print(f"  Delta vs V4:       ${delta_vs_v4:+,.0f} ({pct_vs_v4:+.1f}%)")
    print(f"  Delta vs V3:       ${delta_vs_v3:+,.0f} ({pct_vs_v3:+.1f}%)")
    print(f"  Short bias {'HELPS' if delta_vs_v4 > 0 else 'HURTS'} v4")
    print(f"{'='*70}")

    # ---- 7. Save results ----
    exp_id = f"short_bias_v4_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    result = {
        "experiment_id": exp_id,
        "description": "Short bias (offset=0.5) on v4 (v3 + stable_supply w=1.0)",
        "oos_period": f"{OOS_START} to {OOS_END}",
        "v3_baseline": {"pnl": round(v3_pnl, 2), "trades": v3_trades, "per_coin": v3_results},
        "v4_baseline": {"pnl": round(base_pnl, 2), "trades": base_trades, "per_coin": base_results},
        "v4_short_bias": {"pnl": round(sb_pnl, 2), "trades": sb_trades, "per_coin": sb_results,
                          "short_offset": SHORT_OFFSET, "stable_weight": STABLE_WEIGHT},
        "delta_vs_v4": round(delta_vs_v4, 2),
        "delta_vs_v3": round(delta_vs_v3, 2),
        "verdict": "KEEP" if delta_vs_v4 > 100 else "SKIP",
    }

    os.makedirs("experiments", exist_ok=True)
    out_path = f"experiments/{exp_id}.json"
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2, default=str)
    print(f"\n  Saved: {out_path}")

    return result


if __name__ == "__main__":
    main()
