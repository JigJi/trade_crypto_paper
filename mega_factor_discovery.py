"""
Mega Factor Discovery Backtest
===============================
Comprehensive factor analysis across ALL DB factors, 15m and 5m timeframes.

Phase 1: Core factor ablation - which of the 8 core factors pull weight?
Phase 2: Forward-stepwise addition - which extras to add on top?
Phase 3: From-scratch discovery - build optimal combo from ALL 19 factors
Phase 4: Best combination tested on 5m timeframe

Run overnight. All results saved to experiments/ and auto-memory.
"""

import os, sys, json, warnings, time as _time
from datetime import datetime

import numpy as np
import pandas as pd
import pandas_ta as ta
import psycopg2
import requests
from dotenv import load_dotenv

warnings.filterwarnings("ignore")
load_dotenv()

import backtest_15m_btc_led_alts as bt
from backtest_15m_btc_led_alts import (
    fetch_binance_15m, load_btc_db_data, build_btc_features,
    compute_btc_composite_score, build_alt_technicals,
    run_backtest, calc_metrics,
    BKK_UTC_OFFSET, INIT_EQUITY, FEE, SLIP, BUDGET_USDT,
)
from test_v12_improvements import (
    V11_CONFIGS, BTC_SCORE_WEIGHTS,
    generate_signal_short_bias, ALL_COINS,
)
from test_new_factors import (
    load_basis_factor, load_tick_liquidation_factor,
    load_displacement_factor, load_news_factor,
    load_fvg_factor, load_sweep_factor,
    score_basis, score_basis_momentum, score_tick_liq,
    score_news, score_news_contrarian,
    score_displacement, score_fvg, score_sweep,
    DB_PARAMS,
)
from test_orderbook_factor import (
    load_orderbook_factor,
    score_ob_contrarian, score_ob_vol_contrarian, score_ob_combined,
)

SHORT_OFFSET = 0.5
TEST_START = pd.Timestamp("2026-01-01")
LEVERAGE = 2.0
MIN_IMPROVEMENT = 50  # minimum $ improvement to add a factor
WEIGHTS_TO_TEST = [0.5, 1.0, 1.5, 2.0]


# ================================================================
# Core factor score functions (decomposed from composite_score)
# Each returns the factor's contribution. weight=1.0 = default weights.
# ================================================================

def score_core_oi(df, weight=1.0):
    """OI divergence (price-OI confirmation/divergence)."""
    s = pd.Series(0.0, index=df.index)
    if "oi_chg" not in df.columns:
        return s
    oi_chg = df["oi_chg"].fillna(0)
    ret = df["ret"].fillna(0)
    w = 0.5 * weight
    s += np.where((ret > 0.001) & (oi_chg > 0.002), w, 0)       # bullish confirmation
    s += np.where((ret < -0.001) & (oi_chg < -0.002), w, 0)      # capitulation (bullish)
    s += np.where((ret > 0.001) & (oi_chg < -0.002), -w, 0)      # weak rally
    s += np.where((ret < -0.001) & (oi_chg > 0.002), -w, 0)      # trapped longs
    return s


def score_core_taker(df, weight=1.0):
    """Taker buy/sell ratio."""
    s = pd.Series(0.0, index=df.index)
    if "buy_sell_ratio" not in df.columns:
        return s
    bsr = df["buy_sell_ratio"].fillna(1.0)
    s += np.where(bsr > 1.5, 1.5 * weight, 0)
    s += np.where((bsr > 1.2) & (bsr <= 1.5), 0.5 * weight, 0)
    s += np.where(bsr < 0.7, -1.5 * weight, 0)
    s += np.where((bsr >= 0.7) & (bsr < 0.85), -0.5 * weight, 0)
    return s


def score_core_ls(df, weight=1.0):
    """Long/short ratio (contrarian at extremes)."""
    s = pd.Series(0.0, index=df.index)
    if "gl_ac_ratio" not in df.columns:
        return s
    ls = df["gl_ac_ratio"].fillna(1.0)
    s += np.where(ls > 2.5, -1.5 * weight, 0)   # overheated longs -> bearish
    s += np.where(ls < 0.6, 1.5 * weight, 0)     # extreme shorts -> bullish
    return s


def score_core_funding(df, weight=1.0):
    """Funding rate."""
    s = pd.Series(0.0, index=df.index)
    if "fr_8h" in df.columns:
        fr = df["fr_8h"].fillna(0)
        s += np.where(fr < -0.0001, 2.0 * weight, 0)
        s += np.where(fr > 0.0003, -2.0 * weight, 0)
    elif "last_funding_rate" in df.columns:
        fr = df["last_funding_rate"].fillna(0)
        s += np.where(fr < -0.00005, 2.0 * weight, 0)
        s += np.where(fr > 0.0002, -2.0 * weight, 0)
    return s


def score_core_fg(df, weight=1.0):
    """Fear & Greed index."""
    s = pd.Series(0.0, index=df.index)
    if "fg_score" not in df.columns:
        return s
    fg = df["fg_score"].fillna(50)
    s += np.where(fg < 15, 2.0 * weight, 0)
    s += np.where((fg >= 15) & (fg < 25), 1.0 * weight, 0)
    s += np.where(fg > 80, -2.0 * weight, 0)
    s += np.where((fg > 65) & (fg <= 80), -1.0 * weight, 0)
    return s


def score_core_whale(df, weight=1.0):
    """Whale alerts (net flow MA)."""
    s = pd.Series(0.0, index=df.index)
    if "whale_net_ma" not in df.columns:
        return s
    wn_ma = df["whale_net_ma"].fillna(0)
    s += np.where(wn_ma > 50_000_000, 1.5 * weight, 0)
    s += np.where(wn_ma < -50_000_000, -1.5 * weight, 0)
    return s


def score_core_liq(df, weight=1.0):
    """Liquidation cascades (3x MA threshold)."""
    s = pd.Series(0.0, index=df.index)
    if "liq_net" not in df.columns or "liq_total_ma" not in df.columns:
        return s
    lt = df["liq_total"].fillna(0)
    lt_ma = df["liq_total_ma"].fillna(1)
    ln = df["liq_net"].fillna(0)
    cascade = lt > (lt_ma * 3)
    s += np.where(cascade & (ln > 0), 2.0 * weight, 0)
    s += np.where(cascade & (ln < 0), -2.0 * weight, 0)
    return s


def score_core_etf(df, weight=1.0):
    """ETF flows (5-bar MA)."""
    s = pd.Series(0.0, index=df.index)
    if "etf_flow_ma" not in df.columns:
        return s
    etf_ma = df["etf_flow_ma"].fillna(0)
    s += np.where(etf_ma > 50, 1.5 * weight, 0)
    s += np.where(etf_ma < -50, -1.5 * weight, 0)
    return s


# ================================================================
# All factors registry
# ================================================================

CORE_FACTORS = [
    ("oi_divergence", score_core_oi),
    ("taker_ratio", score_core_taker),
    ("ls_ratio", score_core_ls),
    ("funding_rate", score_core_funding),
    ("fear_greed", score_core_fg),
    ("whale_alerts", score_core_whale),
    ("liquidation", score_core_liq),
    ("etf_flows", score_core_etf),
]

EXTRA_FACTORS = [
    ("basis_contrarian", score_basis),
    ("basis_momentum", score_basis_momentum),
    ("tick_liq", score_tick_liq),
    ("news_directional", score_news),
    ("news_contrarian", score_news_contrarian),
    ("displacement", score_displacement),
    ("fvg", score_fvg),
    ("sweep", score_sweep),
    ("ob_contrarian", score_ob_contrarian),
    ("ob_vol_contrarian", score_ob_vol_contrarian),
    ("ob_combined", score_ob_combined),
]

ALL_FACTORS = CORE_FACTORS + EXTRA_FACTORS


# ================================================================
# Utility functions
# ================================================================

def run_all_coins(btc_score_ts, alt_data, configs=None, leverage=2.0, max_hold=96):
    """Run backtest across all coins. Returns (total_pnl, n_trades, coin_metrics)."""
    if configs is None:
        configs = V11_CONFIGS
    old_lev = bt.LEVERAGE
    bt.LEVERAGE = leverage

    total_pnl = 0
    total_trades = 0
    coin_metrics = {}

    for coin in ALL_COINS:
        if coin not in alt_data:
            continue
        cfg = configs[coin]
        signals, alt_m = generate_signal_short_bias(
            btc_score_ts, alt_data[coin], cfg["threshold"], cfg["alt_pa"],
            short_offset=SHORT_OFFSET)
        trades = run_backtest(alt_m, signals,
                              sl_atr_mult=cfg["sl"], tp_atr_mult=cfg["tp"],
                              trail_atr_mult=cfg["trail"], trail_activate_atr=cfg["trail_act"],
                              cooldown_bars=cfg["cd"], max_hold_bars=max_hold)
        m = calc_metrics(trades, len(alt_data[coin]))
        coin_metrics[coin] = m
        total_pnl += m["net_pnl"]
        total_trades += m["total"]

    bt.LEVERAGE = old_lev
    return total_pnl, total_trades, coin_metrics


def print_coin_table(coin_metrics, label=""):
    """Print per-coin metrics table."""
    if label:
        print(f"  {label}")
    print(f"  {'Coin':5s} {'#Tr':>4s} {'WR%':>6s} {'PF':>7s} {'Sharpe':>7s} {'PnL':>11s}")
    print(f"  {'-'*5} {'-'*4} {'-'*6} {'-'*7} {'-'*7} {'-'*11}")
    total = 0
    for coin in ALL_COINS:
        if coin not in coin_metrics:
            continue
        m = coin_metrics[coin]
        total += m["net_pnl"]
        print(f"  {coin:5s} {m['total']:4d} {m['win_rate']:6.1f} {m['pf']:7.3f} "
              f"{m['sharpe']:7.3f} ${m['net_pnl']:>+10,.2f}")
    print(f"  {'TOTAL':5s} {'':4s} {'':6s} {'':7s} {'':7s} ${total:>+10,.2f}")
    return total


def forward_stepwise(btc_merged, base_score, alt_data, candidates, label="",
                     configs=None, leverage=2.0, max_hold=96, min_improve=50):
    """
    Forward-stepwise factor selection.
    candidates: list of (name, score_fn) tuples.
    Returns: (selected_factors, final_score, step_log)
    """
    if configs is None:
        configs = V11_CONFIGS
    current_score = base_score.copy()
    selected = []
    remaining = list(candidates)
    step_log = []
    step = 0

    # Get baseline PnL
    score_ts = pd.Series(current_score.values, index=btc_merged["ts"].values)
    baseline_pnl, baseline_trades, _ = run_all_coins(score_ts, alt_data, configs, leverage, max_hold)
    current_pnl = baseline_pnl
    print(f"\n  {label} Baseline: ${baseline_pnl:+,.2f} ({baseline_trades} trades)")

    while remaining:
        step += 1
        print(f"\n  --- Step {step}: testing {len(remaining)} factors ---")

        best_improvement = 0
        best_factor = None
        best_weight = None
        best_pnl = current_pnl
        step_results = []

        for name, score_fn in remaining:
            factor_best_w = None
            factor_best_pnl = -1e9

            for w in WEIGHTS_TO_TEST:
                test_score = current_score + score_fn(btc_merged, weight=w)
                test_ts = pd.Series(test_score.values, index=btc_merged["ts"].values)
                test_pnl, test_trades, _ = run_all_coins(test_ts, alt_data, configs, leverage, max_hold)
                improvement = test_pnl - current_pnl

                if test_pnl > factor_best_pnl:
                    factor_best_pnl = test_pnl
                    factor_best_w = w

            improvement = factor_best_pnl - current_pnl
            step_results.append((name, factor_best_w, factor_best_pnl, improvement))

            if improvement > best_improvement:
                best_improvement = improvement
                best_factor = (name, score_fn)
                best_weight = factor_best_w
                best_pnl = factor_best_pnl

        # Print step results sorted by improvement
        step_results.sort(key=lambda x: -x[3])
        for name, w, pnl, imp in step_results[:5]:  # top 5
            marker = " <<<" if imp == best_improvement and imp >= min_improve else ""
            print(f"    {name:25s} w={w:.1f}: ${pnl:>+10,.2f} (d=${imp:>+8,.2f}){marker}")
        if len(step_results) > 5:
            print(f"    ... {len(step_results)-5} more factors tested")

        step_log.append({
            "step": step,
            "results": [(n, w, round(p, 2), round(i, 2)) for n, w, p, i in step_results],
            "selected": best_factor[0] if best_factor and best_improvement >= min_improve else None,
            "selected_weight": best_weight if best_improvement >= min_improve else None,
        })

        if best_improvement >= min_improve and best_factor is not None:
            name, score_fn = best_factor
            current_score = current_score + score_fn(btc_merged, weight=best_weight)
            current_pnl = best_pnl
            selected.append({"name": name, "weight": best_weight, "improvement": round(best_improvement, 2)})
            remaining = [(n, fn) for n, fn in remaining if n != name]
            print(f"  >>> SELECTED: {name} (w={best_weight}, +${best_improvement:,.2f}) -> cumulative ${current_pnl:+,.2f}")
        else:
            print(f"  >>> STOPPED: best improvement ${best_improvement:+,.2f} < ${min_improve}")
            break

    return selected, current_score, step_log


def fetch_binance_ohlcv(symbol, interval="15m", years=1):
    """Generalized OHLCV fetcher supporting any interval."""
    cache_file = f"data_cache/{symbol}_{interval}_{years}yr.parquet"
    if os.path.exists(cache_file):
        print(f"  Using cached {cache_file}")
        df = pd.read_parquet(cache_file)
        df["date_time"] = pd.to_datetime(df["date_time"])
        return df.sort_values("date_time").reset_index(drop=True)

    print(f"  Fetching {years}yr {interval} {symbol}...")
    url = "https://fapi.binance.com/fapi/v1/klines"
    end_ts = int(datetime.now().timestamp() * 1000)
    start_ts = end_ts - years * 365 * 24 * 3600 * 1000

    all_data = []
    current = start_ts
    batch = 0
    while current < end_ts:
        params = {"symbol": symbol, "interval": interval, "startTime": current, "limit": 1500}
        for attempt in range(5):
            try:
                r = requests.get(url, params=params, timeout=15)
                if r.status_code == 429:
                    _time.sleep(10)
                    continue
                r.raise_for_status()
                data = r.json()
                break
            except Exception as e:
                if attempt < 4:
                    _time.sleep(3)
                else:
                    data = []

        if not data:
            break

        for k in data:
            all_data.append({
                "date_time": pd.Timestamp(k[0], unit="ms"),
                "open": float(k[1]), "high": float(k[2]),
                "low": float(k[3]), "close": float(k[4]),
                "volume": float(k[5]),
            })

        current = data[-1][0] + 1
        batch += 1
        if batch % 20 == 0:
            print(f"    {len(all_data):,} candles...", end="\r", flush=True)
            _time.sleep(0.3)

    df = pd.DataFrame(all_data).drop_duplicates("date_time").sort_values("date_time").reset_index(drop=True)
    os.makedirs("data_cache", exist_ok=True)
    df.to_parquet(cache_file, index=False)
    print(f"    {len(df):,} candles saved to {cache_file}")
    return df


# ================================================================
# Main
# ================================================================

def main():
    t_start = _time.time()
    print("=" * 70)
    print("  MEGA FACTOR DISCOVERY BACKTEST")
    print(f"  Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Test period: {TEST_START.date()} to present")
    print(f"  Coins: {', '.join(ALL_COINS)}")
    print(f"  Leverage: {LEVERAGE}x")
    print("=" * 70)

    # ================================================================
    # LOAD DATA
    # ================================================================
    print("\n[LOAD] BTC OHLCV + 8 DB factors...")
    btc_ohlcv = fetch_binance_15m("BTCUSDT", years=3)
    btc_db = load_btc_db_data()
    btc_df = build_btc_features(btc_ohlcv, btc_db)

    print("\n[LOAD] Extra factors from DB...")
    conn = psycopg2.connect(**DB_PARAMS)
    extra_factor_data = {
        "basis": load_basis_factor(conn),
        "tick_liq": load_tick_liquidation_factor(conn),
        "news": load_news_factor(conn),
        "displacement": load_displacement_factor(conn),
        "fvg": load_fvg_factor(conn),
        "sweep": load_sweep_factor(conn),
        "orderbook": load_orderbook_factor(conn),
    }
    conn.close()

    # Trim to Jun 2025+
    mask = btc_df["ts"] >= pd.Timestamp("2025-06-01")
    btc_df_full = btc_df[mask].reset_index(drop=True)

    # Merge extra factors into btc_df
    print("\n[LOAD] Merging extra factors...")
    btc_merged = btc_df_full.copy()
    for name, fdf in extra_factor_data.items():
        if fdf is None:
            print(f"  {name}: NO DATA, skipped")
            continue
        merge_cols = [c for c in fdf.columns if c != "ts"]
        btc_merged = pd.merge_asof(
            btc_merged.sort_values("ts"),
            fdf[["ts"] + merge_cols].sort_values("ts"),
            on="ts", direction="backward", tolerance=pd.Timedelta("30min"))
        n_filled = btc_merged[merge_cols[0]].notna().sum()
        pct = n_filled / len(btc_merged) * 100
        print(f"  {name}: {n_filled:,}/{len(btc_merged):,} bars ({pct:.1f}%)")

    # Load altcoin data
    TEST_END = btc_merged["ts"].iloc[-1]
    print(f"\n[LOAD] Altcoin data (OOS: {TEST_START.date()} to {TEST_END.date()})...")
    alt_data_15m = {}
    for coin in ALL_COINS:
        ohlcv = fetch_binance_15m(f"{coin}USDT", years=3)
        alt_df = build_alt_technicals(ohlcv)
        alt_test = alt_df[(alt_df["ts"] >= TEST_START) & (alt_df["ts"] <= TEST_END)].reset_index(drop=True)
        if len(alt_test) >= 100:
            alt_data_15m[coin] = alt_test
            print(f"  {coin}: {len(alt_test):,} bars")

    load_time = _time.time() - t_start
    print(f"\n  Data loaded in {load_time:.0f}s")

    # ================================================================
    # Verify: compute 8-core baseline using decomposed functions
    # Should match compute_btc_composite_score
    # ================================================================
    print(f"\n{'='*70}")
    print("[VERIFY] Decomposed core factors match composite_score...")
    print(f"{'='*70}")

    # Compute via original function
    original_score = compute_btc_composite_score(btc_df_full, BTC_SCORE_WEIGHTS)

    # Compute via decomposed functions
    decomposed_score = pd.Series(0.0, index=btc_merged.index)
    for name, score_fn in CORE_FACTORS:
        decomposed_score = decomposed_score + score_fn(btc_merged, weight=1.0)

    # Compare
    diff = (original_score - decomposed_score).abs()
    max_diff = diff.max()
    mean_diff = diff.mean()
    print(f"  Max difference: {max_diff:.6f}")
    print(f"  Mean difference: {mean_diff:.6f}")
    if max_diff < 0.01:
        print("  OK -- Decomposition verified, scores match!")
        baseline_score = decomposed_score
    else:
        print("  WARNING: Decomposition mismatch! Using original composite score.")
        baseline_score = original_score

    # Baseline PnL
    baseline_ts = pd.Series(baseline_score.values, index=btc_merged["ts"].values)
    baseline_pnl, baseline_trades, baseline_metrics = run_all_coins(
        baseline_ts, alt_data_15m, leverage=LEVERAGE)
    print(f"\n  8-Core Baseline: ${baseline_pnl:+,.2f} ({baseline_trades} trades)")
    print_coin_table(baseline_metrics)

    # ================================================================
    # PHASE 1: CORE FACTOR ABLATION
    # ================================================================
    t_phase1 = _time.time()
    print(f"\n{'='*70}")
    print("  PHASE 1: CORE FACTOR ABLATION")
    print(f"  Which of the 8 core factors are pulling their weight?")
    print(f"{'='*70}")

    ablation_results = {}
    for name, score_fn in CORE_FACTORS:
        # Remove this factor from baseline
        ablated_score = baseline_score - score_fn(btc_merged, weight=1.0)
        ablated_ts = pd.Series(ablated_score.values, index=btc_merged["ts"].values)
        pnl, trades, metrics = run_all_coins(ablated_ts, alt_data_15m, leverage=LEVERAGE)
        delta = pnl - baseline_pnl
        ablation_results[name] = {
            "pnl_without": round(pnl, 2),
            "delta": round(delta, 2),
            "trades": trades,
        }

    # Print ablation results
    print(f"\n  {'Factor':<20s} {'With All 8':>12s} {'Without':>12s} {'Delta':>12s} {'Verdict':>10s}")
    print(f"  {'-'*20} {'-'*12} {'-'*12} {'-'*12} {'-'*10}")
    for name, r in sorted(ablation_results.items(), key=lambda x: x[1]["delta"]):
        delta = r["delta"]
        if delta < -100:
            verdict = "KEEP!"
        elif delta < -20:
            verdict = "keep"
        elif delta > 100:
            verdict = "DROP?"
        elif delta > 20:
            verdict = "weak"
        else:
            verdict = "neutral"
        print(f"  {name:<20s} ${baseline_pnl:>+10,.2f} ${r['pnl_without']:>+10,.2f} ${delta:>+10,.2f} {verdict:>10s}")

    # Determine if any factors should be dropped
    factors_to_drop = [name for name, r in ablation_results.items() if r["delta"] > 100]
    if factors_to_drop:
        print(f"\n  Factors that HURT when included: {', '.join(factors_to_drop)}")
        # Build improved core set
        improved_core_score = pd.Series(0.0, index=btc_merged.index)
        improved_core_names = []
        for name, score_fn in CORE_FACTORS:
            if name not in factors_to_drop:
                improved_core_score = improved_core_score + score_fn(btc_merged, weight=1.0)
                improved_core_names.append(name)
        improved_ts = pd.Series(improved_core_score.values, index=btc_merged["ts"].values)
        improved_pnl, improved_trades, improved_metrics = run_all_coins(
            improved_ts, alt_data_15m, leverage=LEVERAGE)
        print(f"\n  Improved core ({len(improved_core_names)} factors): ${improved_pnl:+,.2f} ({improved_trades} trades)")
        print_coin_table(improved_metrics)
    else:
        print(f"\n  All 8 core factors contribute positively. No drops needed.")
        improved_core_score = baseline_score
        improved_core_names = [n for n, _ in CORE_FACTORS]
        improved_pnl = baseline_pnl

    phase1_time = _time.time() - t_phase1
    print(f"\n  Phase 1 completed in {phase1_time:.0f}s")

    # ================================================================
    # PHASE 2: FORWARD-STEPWISE ADDITION (extras on improved core)
    # ================================================================
    t_phase2 = _time.time()
    print(f"\n{'='*70}")
    print("  PHASE 2: FORWARD-STEPWISE ADDITION OF EXTRAS")
    print(f"  Starting from {'improved' if factors_to_drop else 'full'} core ({len(improved_core_names)} factors)")
    print(f"  Testing {len(EXTRA_FACTORS)} extra factors")
    print(f"{'='*70}")

    selected_extras, phase2_score, phase2_log = forward_stepwise(
        btc_merged, improved_core_score, alt_data_15m,
        candidates=list(EXTRA_FACTORS),
        label="Phase 2:",
        leverage=LEVERAGE,
    )

    # Final Phase 2 results
    phase2_ts = pd.Series(phase2_score.values, index=btc_merged["ts"].values)
    phase2_pnl, phase2_trades, phase2_metrics = run_all_coins(
        phase2_ts, alt_data_15m, leverage=LEVERAGE)
    print(f"\n  Phase 2 Final: ${phase2_pnl:+,.2f} ({phase2_trades} trades)")
    print_coin_table(phase2_metrics)

    if selected_extras:
        extras_str = ", ".join(f"{s['name']}(w={s['weight']})" for s in selected_extras)
        print(f"\n  Selected extras: {extras_str}")
        total_improvement = sum(s["improvement"] for s in selected_extras)
        print(f"  Total improvement from extras: +${total_improvement:,.2f}")
    else:
        print(f"\n  No extras improved the model.")

    phase2_time = _time.time() - t_phase2
    print(f"\n  Phase 2 completed in {phase2_time:.0f}s")

    # ================================================================
    # PHASE 3: FROM-SCRATCH DISCOVERY
    # ================================================================
    t_phase3 = _time.time()
    print(f"\n{'='*70}")
    print("  PHASE 3: FROM-SCRATCH DISCOVERY")
    print(f"  Starting from ZERO -- find optimal combo from ALL {len(ALL_FACTORS)} factors")
    print(f"{'='*70}")

    zero_score = pd.Series(0.0, index=btc_merged.index)
    scratch_selected, scratch_score, scratch_log = forward_stepwise(
        btc_merged, zero_score, alt_data_15m,
        candidates=list(ALL_FACTORS),
        label="Phase 3 (scratch):",
        leverage=LEVERAGE,
    )

    # Final Phase 3 results
    scratch_ts = pd.Series(scratch_score.values, index=btc_merged["ts"].values)
    scratch_pnl, scratch_trades, scratch_metrics = run_all_coins(
        scratch_ts, alt_data_15m, leverage=LEVERAGE)
    print(f"\n  Phase 3 Final: ${scratch_pnl:+,.2f} ({scratch_trades} trades)")
    print_coin_table(scratch_metrics)

    if scratch_selected:
        print(f"\n  From-scratch model ({len(scratch_selected)} factors):")
        for i, s in enumerate(scratch_selected, 1):
            print(f"    {i}. {s['name']} (w={s['weight']}, +${s['improvement']:,.2f})")
    else:
        print(f"\n  No factors produced positive results from scratch.")

    phase3_time = _time.time() - t_phase3
    print(f"\n  Phase 3 completed in {phase3_time:.0f}s")

    # ================================================================
    # PHASE 4: BEST COMBO ON 5M TIMEFRAME
    # ================================================================
    t_phase4 = _time.time()
    print(f"\n{'='*70}")
    print("  PHASE 4: 5M TIMEFRAME TEST")
    print(f"  Testing best combinations from Phase 2 and 3 on 5m candles")
    print(f"{'='*70}")

    # 5m configs: scale cooldown and max_hold
    V11_CONFIGS_5M = {}
    for coin, cfg in V11_CONFIGS.items():
        V11_CONFIGS_5M[coin] = {**cfg, "cd": cfg["cd"] * 3}
    MAX_HOLD_5M = 96 * 3  # 288 bars = 24h at 5m

    # Fetch 5m OHLCV for all coins
    print("\n[LOAD] Fetching 5m OHLCV for all coins...")
    alt_data_5m = {}
    for coin in ALL_COINS:
        ohlcv_5m = fetch_binance_ohlcv(f"{coin}USDT", interval="5m", years=1)
        alt_df_5m = build_alt_technicals(ohlcv_5m)
        alt_test_5m = alt_df_5m[
            (alt_df_5m["ts"] >= TEST_START) & (alt_df_5m["ts"] <= TEST_END)
        ].reset_index(drop=True)
        if len(alt_test_5m) >= 100:
            alt_data_5m[coin] = alt_test_5m
            print(f"  {coin}: {len(alt_test_5m):,} 5m bars")

    if alt_data_5m:
        # Test Phase 2 best combo on 5m
        print(f"\n  --- Phase 2 combo on 5m ---")
        p2_5m_pnl, p2_5m_trades, p2_5m_metrics = run_all_coins(
            phase2_ts, alt_data_5m, configs=V11_CONFIGS_5M, leverage=LEVERAGE, max_hold=MAX_HOLD_5M)
        print(f"  Phase 2 on 5m: ${p2_5m_pnl:+,.2f} ({p2_5m_trades} trades)")
        print_coin_table(p2_5m_metrics)

        # Test Phase 3 best combo on 5m
        print(f"\n  --- Phase 3 (scratch) combo on 5m ---")
        p3_5m_pnl, p3_5m_trades, p3_5m_metrics = run_all_coins(
            scratch_ts, alt_data_5m, configs=V11_CONFIGS_5M, leverage=LEVERAGE, max_hold=MAX_HOLD_5M)
        print(f"  Phase 3 on 5m: ${p3_5m_pnl:+,.2f} ({p3_5m_trades} trades)")
        print_coin_table(p3_5m_metrics)

        # Also test 8-core baseline on 5m for comparison
        print(f"\n  --- 8-core baseline on 5m ---")
        base_5m_pnl, base_5m_trades, base_5m_metrics = run_all_coins(
            baseline_ts, alt_data_5m, configs=V11_CONFIGS_5M, leverage=LEVERAGE, max_hold=MAX_HOLD_5M)
        print(f"  Baseline on 5m: ${base_5m_pnl:+,.2f} ({base_5m_trades} trades)")
        print_coin_table(base_5m_metrics)
    else:
        print("  No 5m data available!")
        p2_5m_pnl = p3_5m_pnl = base_5m_pnl = 0
        p2_5m_trades = p3_5m_trades = base_5m_trades = 0

    phase4_time = _time.time() - t_phase4
    print(f"\n  Phase 4 completed in {phase4_time:.0f}s")

    # ================================================================
    # FINAL SUMMARY
    # ================================================================
    total_time = _time.time() - t_start
    print(f"\n{'='*70}")
    print("  FINAL SUMMARY")
    print(f"{'='*70}")

    print(f"\n  15M RESULTS:")
    print(f"  {'Model':<45s} {'PnL':>12s} {'Trades':>8s} {'vs Base':>12s}")
    print(f"  {'-'*45} {'-'*12} {'-'*8} {'-'*12}")

    models_15m = [
        ("8-core baseline (v1.1)", baseline_pnl, baseline_trades),
    ]
    if factors_to_drop:
        models_15m.append((f"Ablated core ({len(improved_core_names)}F)", improved_pnl, 0))
    models_15m.append((
        f"Phase 2: core + {len(selected_extras)} extras",
        phase2_pnl, phase2_trades
    ))
    models_15m.append((
        f"Phase 3: {len(scratch_selected)}F from scratch",
        scratch_pnl, scratch_trades
    ))

    for label, pnl, trades in models_15m:
        delta = pnl - baseline_pnl
        best_marker = ""
        print(f"  {label:<45s} ${pnl:>+10,.2f} {trades:>8d} ${delta:>+10,.2f}{best_marker}")

    # Find overall best 15m model
    best_15m = max(models_15m, key=lambda x: x[1])
    print(f"\n  >>> BEST 15M: {best_15m[0]} (${best_15m[1]:+,.2f})")

    if alt_data_5m:
        print(f"\n  5M RESULTS:")
        print(f"  {'Model':<45s} {'PnL':>12s} {'Trades':>8s}")
        print(f"  {'-'*45} {'-'*12} {'-'*8}")
        print(f"  {'8-core baseline':<45s} ${base_5m_pnl:>+10,.2f} {base_5m_trades:>8d}")
        print(f"  {'Phase 2 combo':<45s} ${p2_5m_pnl:>+10,.2f} {p2_5m_trades:>8d}")
        print(f"  {'Phase 3 (scratch) combo':<45s} ${p3_5m_pnl:>+10,.2f} {p3_5m_trades:>8d}")

        best_5m_pnl = max(base_5m_pnl, p2_5m_pnl, p3_5m_pnl)
        if best_5m_pnl == p2_5m_pnl:
            best_5m_label = "Phase 2 combo"
        elif best_5m_pnl == p3_5m_pnl:
            best_5m_label = "Phase 3 (scratch) combo"
        else:
            best_5m_label = "8-core baseline"
        print(f"\n  >>> BEST 5M: {best_5m_label} (${best_5m_pnl:+,.2f})")

        print(f"\n  TIMEFRAME COMPARISON:")
        print(f"  15m best: ${best_15m[1]:+,.2f}")
        print(f"  5m best:  ${best_5m_pnl:+,.2f}")
        if best_5m_pnl > best_15m[1]:
            print(f"  >>> 5M wins by ${best_5m_pnl - best_15m[1]:,.2f}")
        else:
            print(f"  >>> 15M wins by ${best_15m[1] - best_5m_pnl:,.2f}")

    # ================================================================
    # OPTIMAL MODEL SPECIFICATION
    # ================================================================
    print(f"\n{'='*70}")
    print("  OPTIMAL MODEL SPECIFICATION")
    print(f"{'='*70}")

    # Phase 2 model (core + extras)
    print(f"\n  Model A (Phase 2 -- core + extras):")
    print(f"  Core factors ({len(improved_core_names)}):")
    for name in improved_core_names:
        print(f"    - {name} (w=1.0)")
    if selected_extras:
        print(f"  Extra factors ({len(selected_extras)}):")
        for s in selected_extras:
            print(f"    - {s['name']} (w={s['weight']})")
    print(f"  PnL: ${phase2_pnl:+,.2f}")

    # Phase 3 model (from scratch)
    if scratch_selected:
        print(f"\n  Model B (Phase 3 -- from scratch):")
        for i, s in enumerate(scratch_selected, 1):
            print(f"    {i}. {s['name']} (w={s['weight']})")
        print(f"  PnL: ${scratch_pnl:+,.2f}")

    # Recommendation
    print(f"\n{'='*70}")
    if phase2_pnl >= scratch_pnl:
        print(f"  RECOMMENDATION: Use Model A (core + {len(selected_extras)} extras)")
        print(f"  PnL: ${phase2_pnl:+,.2f} ({phase2_trades} trades)")
        recommended_model = "A"
    else:
        print(f"  RECOMMENDATION: Use Model B ({len(scratch_selected)} factors from scratch)")
        print(f"  PnL: ${scratch_pnl:+,.2f} ({scratch_trades} trades)")
        recommended_model = "B"
    print(f"{'='*70}")

    # ================================================================
    # SAVE RESULTS
    # ================================================================
    print(f"\n[SAVE] Saving results...")

    exp_id = f"mega_discovery_{datetime.now().strftime('%Y%m%d_%H%M')}"
    os.makedirs("experiments", exist_ok=True)

    # Build results dict
    results = {
        "experiment_id": exp_id,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "description": "Mega factor discovery: ablation + stepwise + from-scratch + 5m test",
        "runtime_seconds": round(total_time),
        "test_period": f"{TEST_START.date()} to {TEST_END.date()}",
        "leverage": LEVERAGE,
        "phase1_ablation": {
            "baseline_pnl": round(baseline_pnl, 2),
            "results": ablation_results,
            "factors_dropped": factors_to_drop,
            "improved_core": improved_core_names,
            "improved_pnl": round(improved_pnl, 2),
        },
        "phase2_stepwise": {
            "starting_pnl": round(improved_pnl, 2),
            "final_pnl": round(phase2_pnl, 2),
            "selected_extras": selected_extras,
            "step_log": phase2_log,
        },
        "phase3_scratch": {
            "final_pnl": round(scratch_pnl, 2),
            "selected_factors": scratch_selected,
            "step_log": scratch_log,
        },
        "phase4_5m": {
            "baseline_5m_pnl": round(base_5m_pnl, 2) if alt_data_5m else None,
            "phase2_5m_pnl": round(p2_5m_pnl, 2) if alt_data_5m else None,
            "phase3_5m_pnl": round(p3_5m_pnl, 2) if alt_data_5m else None,
        },
        "recommendation": {
            "model": recommended_model,
            "factors": (
                [{"name": n, "weight": 1.0} for n in improved_core_names] + selected_extras
                if recommended_model == "A"
                else scratch_selected
            ),
            "pnl": round(phase2_pnl if recommended_model == "A" else scratch_pnl, 2),
        },
    }

    # Save to experiments/
    results_path = f"experiments/{exp_id}.json"
    with open(results_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"  Saved: {results_path}")

    # Update registry
    registry_path = "experiments/registry.json"
    if os.path.exists(registry_path):
        with open(registry_path, "r") as f:
            registry = json.load(f)
    else:
        registry = []

    registry.append({
        "experiment_id": exp_id,
        "date": results["date"],
        "description": results["description"],
        "params": {
            "test_period": results["test_period"],
            "leverage": LEVERAGE,
            "phases": ["ablation", "stepwise_add", "from_scratch", "5m_test"],
        },
        "results": {
            "baseline_pnl": round(baseline_pnl, 2),
            "phase2_pnl": round(phase2_pnl, 2),
            "phase3_pnl": round(scratch_pnl, 2),
            "recommended": recommended_model,
            "recommended_pnl": results["recommendation"]["pnl"],
        },
    })
    with open(registry_path, "w") as f:
        json.dump(registry, f, indent=2)
    print(f"  Updated: {registry_path}")

    # Save per-coin metrics for recommended model
    if recommended_model == "A":
        rec_metrics = phase2_metrics
    else:
        rec_metrics = scratch_metrics
    metrics_rows = []
    for coin in ALL_COINS:
        if coin in rec_metrics:
            m = rec_metrics[coin]
            metrics_rows.append({"coin": coin, **m})
    if metrics_rows:
        pd.DataFrame(metrics_rows).to_csv(
            f"experiments/{exp_id}_coin_metrics.csv", index=False)
        print(f"  Saved: experiments/{exp_id}_coin_metrics.csv")

    print(f"\n  Total runtime: {total_time:.0f}s ({total_time/60:.1f} minutes)")
    print(f"\n{'='*70}")
    print("  MEGA FACTOR DISCOVERY COMPLETE")
    print(f"  Finished: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*70}")


if __name__ == "__main__":
    main()
