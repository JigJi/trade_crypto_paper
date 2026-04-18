"""
Experiment: Grid Search Short Offset on v3
==========================================
Tests multiple short_offset values [0.3, 0.5, 0.7, 1.0] on v3 baseline
to find optimal short bias strength.

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
OFFSETS_TO_TEST = [0.3, 0.5, 0.7, 1.0]


def run_experiment(signal_fn, signal_kwargs, btc_score_ts, alt_cache):
    """Run backtest across all coins with given signal function. Uses pre-loaded alt data."""
    old_lev = bt.LEVERAGE
    bt.LEVERAGE = 2.0

    total_pnl = 0.0
    total_trades = 0
    results = {}

    for coin in ALL_COINS:
        cfg = V11_CONFIGS[coin]
        alt_df = alt_cache[coin]

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
    print("EXPERIMENT: Grid Search Short Offset on v3")
    print(f"Offsets: {OFFSETS_TO_TEST}")
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

    # ---- 2. Pre-load alt data (shared across all tests) ----
    print("\n[2] Pre-loading altcoin data...")
    alt_cache = {}
    for coin in ALL_COINS:
        symbol = f"{coin}USDT"
        alt_raw = fetch_binance_15m(symbol, years=3)
        alt_df = build_alt_technicals(alt_raw)
        alt_df = alt_df[(alt_df["ts"] >= OOS_START) & (alt_df["ts"] <= OOS_END)].copy()
        alt_cache[coin] = alt_df
        print(f"    {coin}: {len(alt_df):,} bars")

    # ---- 3. Baseline (no short bias) ----
    print("\n[3] Running v3 BASELINE (no short bias)...")
    base_pnl, base_trades, base_results = run_experiment(
        generate_signal_v11, {}, btc_score_ts, alt_cache
    )
    print(f"  BASELINE: ${base_pnl:,.0f} ({base_trades} trades)")

    # ---- 4. Grid search offsets ----
    grid_results = {}
    best_offset = None
    best_pnl = base_pnl

    for offset in OFFSETS_TO_TEST:
        print(f"\n[4.{OFFSETS_TO_TEST.index(offset)+1}] Testing short_offset = {offset}...")
        pnl, trades, coin_results = run_experiment(
            generate_signal_short_bias, {"short_offset": offset},
            btc_score_ts, alt_cache
        )
        delta = pnl - base_pnl
        pct = (delta / abs(base_pnl) * 100) if base_pnl != 0 else 0

        grid_results[str(offset)] = {
            "pnl": round(pnl, 2),
            "trades": trades,
            "delta": round(delta, 2),
            "delta_pct": round(pct, 1),
            "per_coin": coin_results,
        }

        print(f"  offset={offset}: ${pnl:,.0f} ({trades} tr) | delta: ${delta:+,.0f} ({pct:+.1f}%)")
        for coin, r in coin_results.items():
            print(f"    {coin}: ${r['pnl']:,.0f} (WR {r['wr']:.1f}%, L:{r['n_long']} S:{r['n_short']})")

        if pnl > best_pnl:
            best_pnl = pnl
            best_offset = offset

    # ---- 5. Summary ----
    print(f"\n{'='*70}")
    print(f"  GRID SEARCH RESULTS")
    print(f"  {'Offset':>8s} {'PnL':>10s} {'Trades':>7s} {'Delta':>10s} {'%':>8s}")
    print(f"  {'-'*8} {'-'*10} {'-'*7} {'-'*10} {'-'*8}")
    print(f"  {'none':>8s} ${base_pnl:>9,.0f} {base_trades:>7d} {'--':>10s} {'--':>8s}")
    for offset in OFFSETS_TO_TEST:
        r = grid_results[str(offset)]
        marker = " <-- BEST" if offset == best_offset else ""
        print(f"  {offset:>8.1f} ${r['pnl']:>9,.0f} {r['trades']:>7d} ${r['delta']:>+9,.0f} {r['delta_pct']:>+7.1f}%{marker}")

    if best_offset is not None:
        improvement = best_pnl - base_pnl
        print(f"\n  OPTIMAL: offset={best_offset} -> ${best_pnl:,.0f} (+${improvement:,.0f}, +{improvement/abs(base_pnl)*100:.1f}%)")
    else:
        print(f"\n  No offset improved over baseline. Short bias HURTS v3.")
    print(f"{'='*70}")

    # ---- 6. Save results ----
    exp_id = f"short_offset_grid_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    result = {
        "experiment_id": exp_id,
        "description": f"Grid search short_offset {OFFSETS_TO_TEST} on v3",
        "oos_period": f"{OOS_START} to {OOS_END}",
        "baseline": {"pnl": round(base_pnl, 2), "trades": base_trades, "per_coin": base_results},
        "grid_results": grid_results,
        "best_offset": best_offset,
        "best_pnl": round(best_pnl, 2),
        "verdict": f"OPTIMAL offset={best_offset}" if best_offset else "NO_IMPROVEMENT",
    }

    os.makedirs("experiments", exist_ok=True)
    out_path = f"experiments/{exp_id}.json"
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2, default=str)
    print(f"\n  Saved: {out_path}")

    return result


if __name__ == "__main__":
    main()
