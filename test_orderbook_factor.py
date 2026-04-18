"""
Test order_book_raw as new BTC composite factor.
Uses imbalance and depth volume imbalance (contrarian signals).
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

SHORT_OFFSET = 0.5
TEST_START = pd.Timestamp("2026-01-01")

# Best weights from previous test
PREV_FACTOR_WEIGHTS = {
    "basis_momentum": 1.0,
    "tick_liq": 2.0,
    "displacement": 1.5,
}


def load_orderbook_factor(conn):
    """Extract order book imbalance features from meta JSONB."""
    print("  Loading order_book_raw meta...")
    df = pd.read_sql(
        "SELECT fetched_at, "
        "(meta->>'imbalance')::float as imbalance, "
        "(meta->>'bid_sum')::float as bid_sum, "
        "(meta->>'ask_sum')::float as ask_sum "
        "FROM market_data.order_book_raw "
        "ORDER BY fetched_at",
        conn, parse_dates=["fetched_at"])
    if df.empty:
        return None

    df["ts"] = df["fetched_at"].dt.tz_localize(None) - BKK_UTC_OFFSET

    # Resample to 15min
    r = df.set_index("ts").resample("15min").agg({
        "imbalance": "mean",
        "bid_sum": "mean",
        "ask_sum": "mean",
    }).dropna(how="all").reset_index()

    # Features
    r["ob_imb_ma"] = r["imbalance"].rolling(12).mean()       # 3h MA
    r["ob_imb_ma_fast"] = r["imbalance"].rolling(4).mean()    # 1h MA
    r["ob_vol_imb"] = (r["bid_sum"] - r["ask_sum"]) / (r["bid_sum"] + r["ask_sum"])
    r["ob_vol_imb_ma"] = r["ob_vol_imb"].rolling(12).mean()   # 3h MA
    r["ob_depth_ratio"] = r["bid_sum"] / r["ask_sum"].clip(lower=1)
    r["ob_depth_ratio_ma"] = r["ob_depth_ratio"].rolling(12).mean()

    r = r.dropna().reset_index(drop=True)
    print(f"    {len(r):,} 15m bars, imbalance mean={r['imbalance'].mean():.4f}")
    return r


# ================================================================
# Score functions for order book
# ================================================================

def score_ob_contrarian(btc_df, weight=1.5):
    """Contrarian: high bid imbalance = bearish (distribution), low = bullish."""
    s = pd.Series(0.0, index=btc_df.index)
    if "ob_imb_ma" not in btc_df.columns:
        return s
    imb = btc_df["ob_imb_ma"].fillna(0)
    # Contrarian: positive imbalance (more bids) -> price drops
    s += np.where(imb > 0.05, -weight, 0)
    s += np.where(imb > 0.10, -weight * 0.5, 0)
    s += np.where(imb < -0.05, weight, 0)
    s += np.where(imb < -0.10, weight * 0.5, 0)
    return s


def score_ob_vol_contrarian(btc_df, weight=1.5):
    """Contrarian using depth volume imbalance."""
    s = pd.Series(0.0, index=btc_df.index)
    if "ob_vol_imb_ma" not in btc_df.columns:
        return s
    vi = btc_df["ob_vol_imb_ma"].fillna(0)
    s += np.where(vi > 0.03, -weight, 0)
    s += np.where(vi > 0.08, -weight * 0.5, 0)
    s += np.where(vi < -0.03, weight, 0)
    s += np.where(vi < -0.08, weight * 0.5, 0)
    return s


def score_ob_combined(btc_df, weight=1.5):
    """Combined: average of imbalance + volume imbalance (contrarian)."""
    s = pd.Series(0.0, index=btc_df.index)
    if "ob_imb_ma" not in btc_df.columns:
        return s
    imb = btc_df["ob_imb_ma"].fillna(0)
    vi = btc_df["ob_vol_imb_ma"].fillna(0)
    combo = (imb + vi) / 2
    s += np.where(combo > 0.03, -weight, 0)
    s += np.where(combo > 0.07, -weight * 0.5, 0)
    s += np.where(combo < -0.03, weight, 0)
    s += np.where(combo < -0.07, weight * 0.5, 0)
    return s


def run_scenario(label, btc_score_ts, alt_data):
    """Run all 6 coins, return per-coin metrics + total."""
    print(f"\n{'-'*65}")
    print(f"  {label}")
    print(f"{'-'*65}")
    print(f"  {'Coin':5s} {'#Tr':>4s} {'WR%':>6s} {'PF':>7s} {'Sharpe':>7s} {'PnL':>11s} {'MaxDD':>7s}")
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

    all_tr = pd.concat(all_trades) if all_trades else pd.DataFrame()
    n_trades = len(all_tr)
    wr = (all_tr["pnl_net"] > 0).mean() * 100 if n_trades > 0 else 0

    return {"total_pnl": total_pnl, "coins": coin_metrics,
            "n_trades": n_trades, "wr": wr, "trades_df": all_tr}


def main():
    print("=" * 65)
    print(" ORDER BOOK FACTOR TEST")
    print(" A: v1.2 + 3 new factors (current best)")
    print(" B/C/D: + order book variants")
    print("=" * 65)

    # Load BTC base
    print("\n[1] Loading BTC OHLCV + 8 DB factors...")
    btc_ohlcv = fetch_binance_15m("BTCUSDT", years=3)
    btc_db = load_btc_db_data()
    btc_df = build_btc_features(btc_ohlcv, btc_db)
    btc_score_base = compute_btc_composite_score(btc_df, BTC_SCORE_WEIGHTS)

    mask = btc_df["ts"] >= pd.Timestamp("2025-06-01")
    btc_df_full = btc_df[mask].reset_index(drop=True)
    btc_score_full = btc_score_base[mask].reset_index(drop=True)

    # Load all factors (3 prev + orderbook)
    print("\n[2] Loading factors...")
    conn = psycopg2.connect(**DB_PARAMS)
    basis_df = load_basis_factor(conn)
    tick_liq_df = load_tick_liquidation_factor(conn)
    disp_df = load_displacement_factor(conn)
    ob_df = load_orderbook_factor(conn)
    conn.close()

    # Merge all into btc_df
    btc_merged = btc_df_full.copy()
    for name, fdf in [("basis", basis_df), ("tick_liq", tick_liq_df),
                       ("displacement", disp_df), ("orderbook", ob_df)]:
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

    # Build score A: v1.2 + 3 prev factors (baseline)
    score_A = btc_score_full.copy()
    score_A = score_A + score_basis_momentum(btc_merged, weight=PREV_FACTOR_WEIGHTS["basis_momentum"])
    score_A = score_A + score_tick_liq(btc_merged, weight=PREV_FACTOR_WEIGHTS["tick_liq"])
    score_A = score_A + score_displacement(btc_merged, weight=PREV_FACTOR_WEIGHTS["displacement"])

    # Load alt data
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

    # Run scenarios
    print(f"\n{'='*65}")
    print("[4] BACKTEST COMPARISON")
    print(f"{'='*65}")

    score_ts_A = pd.Series(score_A.values, index=btc_merged["ts"].values)
    res_A = run_scenario("A: v1.2 + 3F (basis_mom + tick_liq + displacement)", score_ts_A, alt_data)

    # Test OB variants at multiple weights
    ob_tests = [
        ("ob_contrarian", score_ob_contrarian),
        ("ob_vol_contrarian", score_ob_vol_contrarian),
        ("ob_combined", score_ob_combined),
    ]

    best_overall = {"label": "A", "pnl": res_A["total_pnl"]}
    all_results = {"A": res_A}

    for ob_name, ob_fn in ob_tests:
        for w in [0.5, 1.0, 1.5, 2.0]:
            score_test = score_A + ob_fn(btc_merged, weight=w)
            score_ts = pd.Series(score_test.values, index=btc_merged["ts"].values)
            label = f"{ob_name}(w={w})"
            res = run_scenario(f"+ {label}", score_ts, alt_data)
            all_results[label] = res
            if res["total_pnl"] > best_overall["pnl"]:
                best_overall = {"label": label, "pnl": res["total_pnl"],
                                "fn": ob_fn, "weight": w}

    # Summary
    print(f"\n{'='*65}")
    print("[5] SUMMARY")
    print(f"{'='*65}")
    print(f"  {'Scenario':<35s} {'PnL':>11s} {'Delta':>11s} {'#Tr':>5s} {'WR%':>6s}")
    print(f"  {'-'*35} {'-'*11} {'-'*11} {'-'*5} {'-'*6}")

    base_pnl = res_A["total_pnl"]
    print(f"  {'A: v1.2 + 3F (baseline)':<35s} ${base_pnl:>+9,.2f} {'':>11s} {res_A['n_trades']:>5d} {res_A['wr']:>5.1f}%")

    for key in sorted(all_results.keys()):
        if key == "A":
            continue
        r = all_results[key]
        d = r["total_pnl"] - base_pnl
        marker = " <-- BEST" if key == best_overall.get("label") else ""
        print(f"  {key:<35s} ${r['total_pnl']:>+9,.2f} ${d:>+9,.2f} {r['n_trades']:>5d} {r['wr']:>5.1f}%{marker}")

    # If best is better than A, show per-coin breakdown
    if best_overall["label"] != "A":
        best_res = all_results[best_overall["label"]]
        delta = best_res["total_pnl"] - base_pnl
        pct = delta / abs(base_pnl) * 100

        print(f"\n{'='*65}")
        print(f"[6] BEST: {best_overall['label']} (+${delta:,.0f}, {pct:+.1f}%)")
        print(f"{'='*65}")
        print(f"  {'Coin':5s} {'PnL_A':>10s} {'PnL_Best':>10s} {'Delta':>10s}")
        print(f"  {'-'*5} {'-'*10} {'-'*10} {'-'*10}")
        for coin in ALL_COINS:
            if coin not in res_A["coins"]:
                continue
            pA = res_A["coins"][coin]["net_pnl"]
            pB = best_res["coins"][coin]["net_pnl"]
            d = pB - pA
            m = " +" if d > 0 else " x" if d < -50 else ""
            print(f"  {coin:5s} ${pA:>+9,.2f} ${pB:>+9,.2f} ${d:>+9,.2f}{m}")
        print(f"  {'-'*5} {'-'*10} {'-'*10} {'-'*10}")
        print(f"  TOTAL ${base_pnl:>+9,.2f} ${best_res['total_pnl']:>+9,.2f} ${delta:>+9,.2f}")

        # Long/Short breakdown
        print(f"\n  Long/Short:")
        for lbl, res in [("A", res_A), ("Best", best_res)]:
            tr = res["trades_df"]
            if tr.empty:
                continue
            longs = tr[tr["dir"] == "L"]
            shorts = tr[tr["dir"] == "S"]
            l_pnl = longs["pnl_net"].sum() if not longs.empty else 0
            s_pnl = shorts["pnl_net"].sum() if not shorts.empty else 0
            l_wr = (longs["pnl_net"] > 0).mean() * 100 if not longs.empty else 0
            s_wr = (shorts["pnl_net"] > 0).mean() * 100 if not shorts.empty else 0
            print(f"    {lbl}: LONG {len(longs)} tr WR={l_wr:.1f}% PnL=${l_pnl:>+,.0f} | "
                  f"SHORT {len(shorts)} tr WR={s_wr:.1f}% PnL=${s_pnl:>+,.0f}")

    # Verdict
    print(f"\n{'='*65}")
    if best_overall["label"] != "A":
        d = best_overall["pnl"] - base_pnl
        print(f"  VERDICT: Order book HELPS! Best={best_overall['label']}, +${d:,.0f}")
        print(f"  -> 12-factor model (8 orig + basis_mom + tick_liq + disp + OB)")
    else:
        print(f"  VERDICT: Order book does NOT improve over current 3F combination")
    print(f"{'='*65}")

    # Save
    exp_id = f"orderbook_factor_{datetime.now().strftime('%Y%m%d_%H%M')}"
    registry_path = "experiments/registry.json"
    if os.path.exists(registry_path):
        with open(registry_path, "r") as f:
            registry = json.load(f)
    else:
        registry = []

    summary = {"A_baseline_pnl": round(base_pnl, 2)}
    for key, r in all_results.items():
        if key == "A":
            continue
        summary[key] = {"pnl": round(r["total_pnl"], 2),
                        "delta": round(r["total_pnl"] - base_pnl, 2),
                        "trades": r["n_trades"], "wr": round(r["wr"], 1)}

    registry.append({
        "experiment_id": exp_id,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "description": "Test order_book_raw (imbalance/depth) as factor on top of v1.2+3F",
        "params": {"test_period": f"{TEST_START.date()} to {TEST_END.date()}"},
        "results": summary,
    })
    with open(registry_path, "w") as f:
        json.dump(registry, f, indent=2)
    print(f"\n  Saved as '{exp_id}'")


if __name__ == "__main__":
    main()
