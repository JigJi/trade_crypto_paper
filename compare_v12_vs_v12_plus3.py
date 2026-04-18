"""
Compare v1.2 (8 factors + short bias) vs v1.2 + 3 new factors
==============================================================
A: v1.2 baseline  -- 8 factors, short_bias (threshold_short = threshold - 0.5)
B: v1.2 + 3 new   -- + basis_momentum(w=1.0) + displacement(w=1.5) + tick_liq(w=2.0)

Same OOS period: Jan 1 - Mar 8, 2026
Same coins: BTC, XRP, ADA, DOT, SUI, FIL
"""

import os, json, warnings
from datetime import datetime

import numpy as np
import pandas as pd
import psycopg2
from dotenv import load_dotenv

warnings.filterwarnings("ignore")
load_dotenv()

from backtest_15m_btc_led_alts import (
    fetch_binance_15m, load_btc_db_data, build_btc_features,
    compute_btc_composite_score, build_alt_technicals,
    run_backtest, calc_metrics, BKK_UTC_OFFSET,
)
from test_v12_improvements import (
    V11_CONFIGS, BTC_SCORE_WEIGHTS, generate_signal_short_bias, ALL_COINS,
)
from test_new_factors import (
    load_basis_factor, load_tick_liquidation_factor, load_displacement_factor,
    score_basis_momentum, score_tick_liq, score_displacement, DB_PARAMS,
)

SHORT_OFFSET = 0.5  # v1.2 short bias
TEST_START = pd.Timestamp("2026-01-01")

# Best weights from individual tests
NEW_FACTOR_WEIGHTS = {
    "basis_momentum": 1.0,
    "tick_liq": 2.0,
    "displacement": 1.5,
}


def run_scenario(label, btc_score_ts, alt_data):
    """Run all 6 coins, return per-coin metrics + total."""
    print(f"\n{'-'*60}")
    print(f"  {label}")
    print(f"{'-'*60}")
    print(f"  {'Coin':5s} {'#Tr':>4s} {'WR%':>6s} {'PF':>7s} {'Sharpe':>7s} {'PnL':>11s} {'MaxDD%':>7s}")
    print(f"  {'-'*5} {'-'*4} {'-'*6} {'-'*7} {'-'*7} {'-'*11} {'-'*7}")

    total_pnl = 0
    all_trades = []
    coin_metrics = {}

    for coin in ALL_COINS:
        if coin not in alt_data:
            continue
        cfg = V11_CONFIGS[coin]
        signals, alt_m = generate_signal_short_bias(
            btc_score_ts, alt_data[coin], cfg["threshold"], cfg["alt_pa"],
            short_offset=SHORT_OFFSET)
        trades = run_backtest(
            alt_m, signals,
            sl_atr_mult=cfg["sl"], tp_atr_mult=cfg["tp"],
            trail_atr_mult=cfg["trail"], trail_activate_atr=cfg["trail_act"],
            cooldown_bars=cfg["cd"])
        m = calc_metrics(trades, len(alt_data[coin]))
        coin_metrics[coin] = m
        total_pnl += m["net_pnl"]

        if not trades.empty:
            trades["coin"] = coin
            all_trades.append(trades)

        print(f"  {coin:5s} {m['total']:4d} {m['win_rate']:6.1f} {m['pf']:7.3f} "
              f"{m['sharpe']:7.3f} ${m['net_pnl']:>+10,.2f} {m['max_dd']:>6.2f}%")

    print(f"  {'-'*5} {'-'*4} {'-'*6} {'-'*7} {'-'*7} {'-'*11} {'-'*7}")
    print(f"  {'TOTAL':5s} {'':4s} {'':6s} {'':7s} {'':7s} ${total_pnl:>+10,.2f}")

    # Aggregate stats
    all_tr = pd.concat(all_trades) if all_trades else pd.DataFrame()
    n_trades = len(all_tr)
    wr = (all_tr["pnl_net"] > 0).mean() * 100 if n_trades > 0 else 0

    return {"total_pnl": total_pnl, "coins": coin_metrics,
            "n_trades": n_trades, "wr": wr, "trades_df": all_tr}


def main():
    print("=" * 60)
    print(" COMPARE: v1.2 (8F) vs v1.2 + 3 new factors")
    print("=" * 60)

    # -- Load BTC base --
    print("\n[1] Loading BTC OHLCV + 8 DB factors...")
    btc_ohlcv = fetch_binance_15m("BTCUSDT", years=3)
    btc_db = load_btc_db_data()
    btc_df = build_btc_features(btc_ohlcv, btc_db)
    btc_score_base = compute_btc_composite_score(btc_df, BTC_SCORE_WEIGHTS)

    # Trim to Jun 2025+
    mask = btc_df["ts"] >= pd.Timestamp("2025-06-01")
    btc_df_full = btc_df[mask].reset_index(drop=True)
    btc_score_full = btc_score_base[mask].reset_index(drop=True)

    # -- Load 3 new factors --
    print("\n[2] Loading 3 new factors...")
    conn = psycopg2.connect(**DB_PARAMS)
    basis_df = load_basis_factor(conn)
    tick_liq_df = load_tick_liquidation_factor(conn)
    disp_df = load_displacement_factor(conn)
    conn.close()

    # Merge new factors into btc_df
    btc_merged = btc_df_full.copy()
    for name, fdf in [("basis", basis_df), ("tick_liq", tick_liq_df), ("displacement", disp_df)]:
        if fdf is None:
            print(f"  WARNING: {name} has no data!")
            continue
        merge_cols = [c for c in fdf.columns if c != "ts"]
        btc_merged = pd.merge_asof(
            btc_merged.sort_values("ts"),
            fdf[["ts"] + merge_cols].sort_values("ts"),
            on="ts", direction="backward", tolerance=pd.Timedelta("30min"))
        n_filled = btc_merged[merge_cols[0]].notna().sum()
        print(f"  {name}: {n_filled:,}/{len(btc_merged):,} bars ({n_filled/len(btc_merged)*100:.1f}%)")

    # Enhanced score = base + 3 new factor addons
    enhanced_score = btc_score_full.copy()
    enhanced_score = enhanced_score + score_basis_momentum(btc_merged, weight=NEW_FACTOR_WEIGHTS["basis_momentum"])
    enhanced_score = enhanced_score + score_tick_liq(btc_merged, weight=NEW_FACTOR_WEIGHTS["tick_liq"])
    enhanced_score = enhanced_score + score_displacement(btc_merged, weight=NEW_FACTOR_WEIGHTS["displacement"])

    # Build ts-indexed score series
    score_ts_A = pd.Series(btc_score_full.values, index=btc_df_full["ts"].values)
    score_ts_B = pd.Series(enhanced_score.values, index=btc_merged["ts"].values)

    # -- Load alt data --
    TEST_END = btc_df_full["ts"].iloc[-1]
    print(f"\n[3] Loading altcoin data (OOS: {TEST_START.date()} to {TEST_END.date()})...")
    alt_data = {}
    for coin in ALL_COINS:
        ohlcv = fetch_binance_15m(f"{coin}USDT", years=3)
        alt_df = build_alt_technicals(ohlcv)
        alt_test = alt_df[(alt_df["ts"] >= TEST_START) & (alt_df["ts"] <= TEST_END)].reset_index(drop=True)
        if len(alt_test) >= 100:
            alt_data[coin] = alt_test
            print(f"  {coin}: {len(alt_test):,} bars")

    # -- Run both scenarios --
    print(f"\n{'='*60}")
    print("[4] BACKTEST COMPARISON (OOS: Jan-Mar 2026)")
    print(f"{'='*60}")

    res_A = run_scenario("A: v1.2 -- 8 factors + short bias", score_ts_A, alt_data)
    res_B = run_scenario("B: v1.2 + basis_mom(1.0) + tick_liq(2.0) + displacement(1.5)", score_ts_B, alt_data)

    # -- Delta analysis --
    print(f"\n{'='*60}")
    print("[5] DELTA ANALYSIS (B minus A)")
    print(f"{'='*60}")
    print(f"  {'Coin':5s} {'PnL_A':>10s} {'PnL_B':>10s} {'Delta':>10s} {'Trades_A':>8s} {'Trades_B':>8s}")
    print(f"  {'-'*5} {'-'*10} {'-'*10} {'-'*10} {'-'*8} {'-'*8}")

    for coin in ALL_COINS:
        if coin not in res_A["coins"]:
            continue
        mA = res_A["coins"][coin]
        mB = res_B["coins"][coin]
        d = mB["net_pnl"] - mA["net_pnl"]
        marker = " +" if d > 0 else " x" if d < -50 else ""
        print(f"  {coin:5s} ${mA['net_pnl']:>+9,.2f} ${mB['net_pnl']:>+9,.2f} ${d:>+9,.2f} "
              f"{mA['total']:>8d} {mB['total']:>8d}{marker}")

    delta_total = res_B["total_pnl"] - res_A["total_pnl"]
    pct_improvement = delta_total / abs(res_A["total_pnl"]) * 100 if res_A["total_pnl"] != 0 else 0
    print(f"  {'-'*5} {'-'*10} {'-'*10} {'-'*10}")
    print(f"  {'TOTAL':5s} ${res_A['total_pnl']:>+9,.2f} ${res_B['total_pnl']:>+9,.2f} ${delta_total:>+9,.2f} ({pct_improvement:+.1f}%)")

    # -- Win rate / trade count comparison --
    print(f"\n  Portfolio trades: A={res_A['n_trades']}, B={res_B['n_trades']} (delta: {res_B['n_trades']-res_A['n_trades']:+d})")
    print(f"  Portfolio WR:     A={res_A['wr']:.1f}%, B={res_B['wr']:.1f}%")

    # -- Long vs Short breakdown --
    print(f"\n{'='*60}")
    print("[6] LONG vs SHORT BREAKDOWN")
    print(f"{'='*60}")

    for label, res in [("A (v1.2)", res_A), ("B (v1.2+3)", res_B)]:
        tr = res["trades_df"]
        if tr.empty:
            continue
        longs = tr[tr["dir"] == "L"]
        shorts = tr[tr["dir"] == "S"]
        l_pnl = longs["pnl_net"].sum() if not longs.empty else 0
        s_pnl = shorts["pnl_net"].sum() if not shorts.empty else 0
        l_wr = (longs["pnl_net"] > 0).mean() * 100 if not longs.empty else 0
        s_wr = (shorts["pnl_net"] > 0).mean() * 100 if not shorts.empty else 0
        print(f"  {label}:")
        print(f"    LONG:  {len(longs):4d} trades, WR={l_wr:.1f}%, PnL=${l_pnl:>+10,.2f}")
        print(f"    SHORT: {len(shorts):4d} trades, WR={s_wr:.1f}%, PnL=${s_pnl:>+10,.2f}")

    # -- Verdict --
    print(f"\n{'='*60}")
    if delta_total > 100:
        verdict = f"B WINS (+${delta_total:,.0f}, {pct_improvement:+.1f}%) -> recommend adding 3 new factors"
    elif delta_total < -100:
        verdict = f"A WINS -> 3 new factors HURT performance (${delta_total:,.0f})"
    else:
        verdict = f"NEUTRAL (delta ${delta_total:,.0f}) -> not enough improvement to justify complexity"
    print(f"  VERDICT: {verdict}")
    print(f"{'='*60}")

    # -- Save --
    exp_id = f"v12_vs_v12plus3_{datetime.now().strftime('%Y%m%d_%H%M')}"
    registry_path = "experiments/registry.json"
    if os.path.exists(registry_path):
        with open(registry_path, "r") as f:
            registry = json.load(f)
    else:
        registry = []

    registry.append({
        "experiment_id": exp_id,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "description": "v1.2 (8F+short_bias) vs v1.2 + 3 new factors (basis_mom, tick_liq, displacement)",
        "params": {
            "test_period": f"{TEST_START.date()} to {TEST_END.date()}",
            "new_factors": NEW_FACTOR_WEIGHTS,
            "short_offset": SHORT_OFFSET,
        },
        "results": {
            "A_v12_pnl": round(res_A["total_pnl"], 2),
            "B_v12plus3_pnl": round(res_B["total_pnl"], 2),
            "delta": round(delta_total, 2),
            "pct_improvement": round(pct_improvement, 1),
            "A_coins": {c: {"pnl": round(m["net_pnl"], 2), "trades": m["total"], "wr": round(m["win_rate"], 1)}
                        for c, m in res_A["coins"].items()},
            "B_coins": {c: {"pnl": round(m["net_pnl"], 2), "trades": m["total"], "wr": round(m["win_rate"], 1)}
                        for c, m in res_B["coins"].items()},
        },
    })

    with open(registry_path, "w") as f:
        json.dump(registry, f, indent=2)
    print(f"\n  Saved to experiments/registry.json as '{exp_id}'")


if __name__ == "__main__":
    main()
