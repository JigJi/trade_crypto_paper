"""
Experiment: Short Bias Validation on BULL Period
=================================================
Tests short_bias on bullish market (Jun-Dec 2025, BTC $60k -> $108k)
to validate whether short bias is a structural edge or period artifact.

If short bias HURTS during bull market, it's just a bear market artifact.
If it HELPS or stays neutral, it's a real structural edge.
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

# BULL period: Jun-Dec 2025 (BTC $60k -> $108k)
BULL_START = pd.Timestamp("2025-06-01")
BULL_END   = pd.Timestamp("2025-12-31")

# BEAR period: Jan-Mar 2026 (BTC $108k -> $80k)
BEAR_START = pd.Timestamp("2026-01-01")
BEAR_END   = pd.Timestamp("2026-03-10")

OFFSETS = [0.0, 0.5, 1.0]  # 0.0 = no short bias (baseline)


def run_period(btc_score_ts, period_start, period_end, signal_fn, signal_kwargs):
    """Run backtest across all coins for a specific period."""
    old_lev = bt.LEVERAGE
    bt.LEVERAGE = 2.0

    total_pnl = 0.0
    total_trades = 0
    total_long = 0
    total_short = 0
    results = {}

    for coin in ALL_COINS:
        cfg = V11_CONFIGS[coin]
        symbol = f"{coin}USDT"
        alt_raw = fetch_binance_15m(symbol, years=3)
        alt_df = build_alt_technicals(alt_raw)
        alt_df = alt_df[(alt_df["ts"] >= period_start) & (alt_df["ts"] <= period_end)].copy()

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
                "pnl": round(pnl, 2), "trades": m["total"],
                "wr": round(m["win_rate"], 1), "sharpe": round(m["sharpe"], 2),
                "wr_long": round(m.get("wr_long", 0), 1),
                "wr_short": round(m.get("wr_short", 0), 1),
                "n_long": m.get("n_long", 0), "n_short": m.get("n_short", 0),
            }
            total_long += m.get("n_long", 0)
            total_short += m.get("n_short", 0)
        else:
            pnl = 0
            results[coin] = {"pnl": 0, "trades": 0, "wr": 0, "sharpe": 0,
                             "n_long": 0, "n_short": 0}

        total_pnl += pnl
        total_trades += results[coin]["trades"]

    bt.LEVERAGE = old_lev
    return total_pnl, total_trades, total_long, total_short, results


def main():
    print("=" * 70)
    print("VALIDATION: Short Bias on BULL vs BEAR Periods")
    print(f"BULL: {BULL_START} to {BULL_END} (BTC $60k -> $108k)")
    print(f"BEAR: {BEAR_START} to {BEAR_END} (BTC $108k -> $80k)")
    print(f"Offsets: {OFFSETS}")
    print("=" * 70)

    # ---- 1. Load data ----
    print("\n[1] Loading BTC data + v3 factors...")
    btc_raw = fetch_binance_15m("BTCUSDT", years=3)
    db_data = load_btc_db_data_v3()
    btc_df = build_btc_features(btc_raw, db_data)
    btc_df = btc_df[btc_df["ts"] >= pd.Timestamp("2025-01-01")].copy().reset_index(drop=True)

    btc_score = compute_btc_composite_score(btc_df)
    btc_score_ts = btc_score.copy()
    btc_score_ts.index = btc_df["ts"]

    # ---- 2. Run all combinations ----
    all_results = {}

    for period_name, p_start, p_end in [("BULL", BULL_START, BULL_END),
                                          ("BEAR", BEAR_START, BEAR_END)]:
        print(f"\n{'='*50}")
        print(f"  PERIOD: {period_name} ({p_start.date()} to {p_end.date()})")
        print(f"{'='*50}")

        period_results = {}

        for offset in OFFSETS:
            if offset == 0.0:
                label = "no_bias"
                fn = generate_signal_v11
                kwargs = {}
            else:
                label = f"offset_{offset}"
                fn = generate_signal_short_bias
                kwargs = {"short_offset": offset}

            print(f"\n  [{period_name}] offset={offset}...")
            pnl, trades, n_long, n_short, coin_results = run_period(
                btc_score_ts, p_start, p_end, fn, kwargs
            )

            period_results[str(offset)] = {
                "pnl": round(pnl, 2), "trades": trades,
                "n_long": n_long, "n_short": n_short,
                "per_coin": coin_results,
            }

            print(f"    PnL: ${pnl:,.0f} ({trades} trades, L:{n_long} S:{n_short})")
            for coin, r in coin_results.items():
                print(f"      {coin}: ${r['pnl']:,.0f} (WR {r['wr']:.1f}%, L:{r['n_long']} S:{r['n_short']})")

        all_results[period_name] = period_results

    # ---- 3. Summary comparison ----
    print(f"\n{'='*70}")
    print(f"  BULL vs BEAR COMPARISON")
    print(f"{'='*70}")
    print(f"\n  {'Offset':>8s} | {'BULL PnL':>10s} {'Trades':>7s} {'L/S':>8s} | {'BEAR PnL':>10s} {'Trades':>7s} {'L/S':>8s}")
    print(f"  {'-'*8}-+-{'-'*10}-{'-'*7}-{'-'*8}-+-{'-'*10}-{'-'*7}-{'-'*8}")

    for offset in OFFSETS:
        k = str(offset)
        bull = all_results["BULL"][k]
        bear = all_results["BEAR"][k]
        bull_ls = f"{bull['n_long']}/{bull['n_short']}"
        bear_ls = f"{bear['n_long']}/{bear['n_short']}"
        print(f"  {offset:>8.1f} | ${bull['pnl']:>9,.0f} {bull['trades']:>7d} {bull_ls:>8s} | ${bear['pnl']:>9,.0f} {bear['trades']:>7d} {bear_ls:>8s}")

    # Delta analysis
    print(f"\n  DELTA from baseline (offset=0.0):")
    base_bull = all_results["BULL"]["0.0"]["pnl"]
    base_bear = all_results["BEAR"]["0.0"]["pnl"]
    for offset in [0.5, 1.0]:
        k = str(offset)
        d_bull = all_results["BULL"][k]["pnl"] - base_bull
        d_bear = all_results["BEAR"][k]["pnl"] - base_bear
        pct_bull = (d_bull / abs(base_bull) * 100) if base_bull != 0 else 0
        pct_bear = (d_bear / abs(base_bear) * 100) if base_bear != 0 else 0
        print(f"    offset={offset}: BULL ${d_bull:+,.0f} ({pct_bull:+.1f}%) | BEAR ${d_bear:+,.0f} ({pct_bear:+.1f}%)")

    # Verdict
    d_bull_05 = all_results["BULL"]["0.5"]["pnl"] - base_bull
    d_bear_05 = all_results["BEAR"]["0.5"]["pnl"] - base_bear
    if d_bull_05 > 0 and d_bear_05 > 0:
        verdict = "STRUCTURAL EDGE -- short bias helps in BOTH bull and bear markets"
    elif d_bull_05 < -200:
        verdict = "PERIOD ARTIFACT -- short bias HURTS in bull market, only works in bear"
    elif abs(d_bull_05) < 200:
        verdict = "MIXED -- short bias neutral in bull, helps in bear. Mild structural edge."
    else:
        verdict = "UNCLEAR -- needs more analysis"

    print(f"\n  VERDICT: {verdict}")
    print(f"{'='*70}")

    # ---- 4. Save ----
    exp_id = f"short_bias_bull_bear_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    result = {
        "experiment_id": exp_id,
        "description": "Short bias validation: BULL (Jun-Dec 2025) vs BEAR (Jan-Mar 2026)",
        "bull_period": f"{BULL_START} to {BULL_END}",
        "bear_period": f"{BEAR_START} to {BEAR_END}",
        "results": all_results,
        "baseline_bull": base_bull,
        "baseline_bear": base_bear,
        "verdict": verdict,
    }

    os.makedirs("experiments", exist_ok=True)
    out_path = f"experiments/{exp_id}.json"
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2, default=str)
    print(f"\n  Saved: {out_path}")

    return result


if __name__ == "__main__":
    main()
