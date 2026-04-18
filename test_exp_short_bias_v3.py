"""
Experiment: Short Bias on v3 Baseline
=====================================
Tests generate_signal_short_bias (offset=0.5) on v3 composite score.
Compares v3 baseline (no short bias) vs v3 + short_bias.
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

import backtest_15m_btc_led_alts as bt

OOS_START = pd.Timestamp("2026-01-01")
OOS_END   = pd.Timestamp("2026-03-10")
SHORT_OFFSET = 0.5


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
    print("EXPERIMENT: Short Bias on v3 Baseline")
    print(f"OOS: {OOS_START} to {OOS_END} | 6 coins | 2x leverage")
    print("=" * 70)

    # ---- 1. Load data & compute v3 score ----
    print("\n[1] Loading BTC data + v3 factors...")
    btc_raw = fetch_binance_15m("BTCUSDT", years=3)
    db_data = load_btc_db_data_v3()
    btc_df = build_btc_features(btc_raw, db_data)
    btc_df = btc_df[btc_df["ts"] >= pd.Timestamp("2025-06-01")].copy().reset_index(drop=True)

    btc_score = compute_btc_composite_score(btc_df)
    btc_score_ts = btc_score.copy()
    btc_score_ts.index = btc_df["ts"]

    # ---- 2. V3 Baseline (no short bias) ----
    print("\n[2] Running v3 BASELINE (no short bias)...")
    base_pnl, base_trades, base_results = run_experiment(
        generate_signal_v11, {}, btc_score_ts, "v3_baseline"
    )
    print(f"\n  V3 BASELINE: ${base_pnl:,.0f} ({base_trades} trades)")
    for coin, r in base_results.items():
        print(f"    {coin}: ${r['pnl']:,.0f} ({r['trades']} tr, WR {r['wr']:.1f}%)")

    # ---- 3. V3 + Short Bias (offset=0.5) ----
    print(f"\n[3] Running v3 + SHORT BIAS (offset={SHORT_OFFSET})...")
    sb_pnl, sb_trades, sb_results = run_experiment(
        generate_signal_short_bias, {"short_offset": SHORT_OFFSET},
        btc_score_ts, "v3_short_bias"
    )
    print(f"\n  V3 + SHORT BIAS: ${sb_pnl:,.0f} ({sb_trades} trades)")
    for coin, r in sb_results.items():
        print(f"    {coin}: ${r['pnl']:,.0f} ({r['trades']} tr, WR {r['wr']:.1f}%, L:{r['n_long']} S:{r['n_short']})")

    # ---- 4. Comparison ----
    delta = sb_pnl - base_pnl
    pct = (delta / abs(base_pnl) * 100) if base_pnl != 0 else 0
    print(f"\n{'='*70}")
    print(f"  RESULT: Short bias {'HELPS' if delta > 0 else 'HURTS'}")
    print(f"  Baseline:    ${base_pnl:,.0f} ({base_trades} trades)")
    print(f"  Short Bias:  ${sb_pnl:,.0f} ({sb_trades} trades)")
    print(f"  Delta:       ${delta:+,.0f} ({pct:+.1f}%)")
    print(f"  Extra trades: {sb_trades - base_trades}")
    print(f"{'='*70}")

    # ---- 5. Save results ----
    exp_id = f"short_bias_v3_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    result = {
        "experiment_id": exp_id,
        "description": "Short bias (offset=0.5) on v3 baseline",
        "oos_period": f"{OOS_START} to {OOS_END}",
        "baseline": {"pnl": round(base_pnl, 2), "trades": base_trades, "per_coin": base_results},
        "short_bias": {"pnl": round(sb_pnl, 2), "trades": sb_trades, "per_coin": sb_results,
                       "short_offset": SHORT_OFFSET},
        "delta": round(delta, 2),
        "delta_pct": round(pct, 1),
        "verdict": "KEEP" if delta > 100 else "SKIP",
    }

    os.makedirs("experiments", exist_ok=True)
    out_path = f"experiments/{exp_id}.json"
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2, default=str)
    print(f"\n  Saved: {out_path}")

    return result


if __name__ == "__main__":
    main()
