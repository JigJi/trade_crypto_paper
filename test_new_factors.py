"""
Test unused DB tables as new BTC composite factors.
Factors tested:
  1. Basis (futures premium/discount)
  2. Tick-level liquidations (market_data.liquidation)
  3. News sentiment (public.news_crypto)
  4. Displacement candles (public.displacement_candles)
  5. Fair Value Gaps (public.fvg)
  6. Liquidity sweeps (public.liquidity_sweeps)

Method: add each as factor to existing 8-factor composite score,
test on overlapping period, compare vs baseline.
"""

import os, json, warnings
from datetime import datetime

import numpy as np
import pandas as pd
import psycopg2
from dotenv import load_dotenv

warnings.filterwarnings("ignore")
load_dotenv()

import backtest_15m_btc_led_alts as bt
from backtest_15m_btc_led_alts import (
    fetch_binance_15m, load_btc_db_data, build_btc_features,
    compute_btc_composite_score, build_alt_technicals,
    run_backtest, calc_metrics, BKK_UTC_OFFSET,
)
from test_v12_improvements import V11_CONFIGS, BTC_SCORE_WEIGHTS, generate_signal_v11, ALL_COINS

DB_PARAMS = {
    "dbname": os.getenv("PG_DB", "smart_trading"),
    "user": os.getenv("PG_USER", "postgres"),
    "password": os.getenv("PG_PASS", "P@ssw0rd"),
    "host": os.getenv("PG_HOST", "localhost"),
    "port": os.getenv("PG_PORT", "5432"),
}


# ================================================================
# Factor loaders — each returns a 15m DataFrame with ts + features
# ================================================================

def load_basis_factor(conn):
    """Futures basis rate as sentiment indicator."""
    print("  Loading basis...")
    df = pd.read_sql(
        "SELECT ts, basis_rate, annualized_basis_rate FROM market_data.basis "
        "WHERE pair='BTCUSDT' ORDER BY ts", conn, parse_dates=["ts"])
    if df.empty:
        return None
    df["ts"] = df["ts"].dt.tz_localize(None) - BKK_UTC_OFFSET

    # Resample to 15min
    r = df.set_index("ts").resample("15min").last().dropna(how="all").reset_index()
    r["basis_ma"] = r["basis_rate"].rolling(12).mean()  # 3hr MA
    r["basis_z"] = (r["basis_rate"] - r["basis_rate"].rolling(96).mean()) / r["basis_rate"].rolling(96).std().clip(lower=1e-8)
    print(f"    {len(r):,} 15m bars, basis_rate mean={r['basis_rate'].mean():.6f}")
    return r


def load_tick_liquidation_factor(conn):
    """Tick-level liquidation aggregated to 15min."""
    print("  Loading tick liquidations...")
    df = pd.read_sql(
        "SELECT event_time, symbol, side, notional_usd FROM market_data.liquidation "
        "WHERE symbol='BTCUSDT' ORDER BY event_time", conn, parse_dates=["event_time"])
    if df.empty:
        return None
    df["ts"] = df["event_time"].dt.tz_localize(None) - BKK_UTC_OFFSET

    # Aggregate to 15min bins
    df["is_sell"] = (df["side"] == "SELL").astype(float)  # SELL = long liquidated
    df["is_buy"] = (df["side"] == "BUY").astype(float)   # BUY = short liquidated

    agg = df.set_index("ts").resample("15min").agg({
        "notional_usd": "sum",
        "is_sell": "sum",
        "is_buy": "sum",
    }).fillna(0).reset_index()

    agg.columns = ["ts", "liq_notional", "liq_long_count", "liq_short_count"]
    agg["liq_net_count"] = agg["liq_short_count"] - agg["liq_long_count"]  # positive = short squeeze
    agg["liq_total_count"] = agg["liq_long_count"] + agg["liq_short_count"]
    agg["liq_notional_ma"] = agg["liq_notional"].rolling(16).mean()  # 4hr MA
    agg["liq_net_ma"] = agg["liq_net_count"].rolling(16).mean()
    print(f"    {len(agg):,} 15m bars, avg notional={agg['liq_notional'].mean():.0f}")
    return agg


def load_news_factor(conn):
    """News sentiment aggregated to rolling windows."""
    print("  Loading news sentiment...")
    df = pd.read_sql(
        "SELECT news_time as ts, sentiment FROM public.news_crypto ORDER BY news_time",
        conn, parse_dates=["ts"])
    if df.empty:
        return None
    df["ts"] = df["ts"].dt.tz_localize(None) - BKK_UTC_OFFSET

    df["bull"] = (df["sentiment"] == "bullish").astype(float)
    df["bear"] = (df["sentiment"] == "bearish").astype(float)

    # Resample: count sentiments per 15min, then rolling 24h
    r = df.set_index("ts").resample("15min").agg({"bull": "sum", "bear": "sum"}).fillna(0).reset_index()
    r["news_bull_24h"] = r["bull"].rolling(96).sum()   # 24h rolling
    r["news_bear_24h"] = r["bear"].rolling(96).sum()
    r["news_net_24h"] = r["news_bull_24h"] - r["news_bear_24h"]
    r["news_ratio_24h"] = r["news_bull_24h"] / r["news_bear_24h"].clip(lower=0.1)
    print(f"    {len(r):,} 15m bars, total bull={df['bull'].sum():.0f}, bear={df['bear'].sum():.0f}")
    return r


def load_displacement_factor(conn):
    """Displacement candles (large body, 4h timeframe)."""
    print("  Loading displacement candles...")
    df = pd.read_sql(
        "SELECT candle_time as ts, direction, body_ratio FROM public.displacement_candles "
        "WHERE symbol='BTCUSDT' ORDER BY candle_time", conn, parse_dates=["ts"])
    if df.empty:
        return None

    # Create directional signal: Bull=+1, Bear=-1, weighted by body_ratio
    df["disp_signal"] = np.where(df["direction"] == "Bull", df["body_ratio"], -df["body_ratio"])

    # Resample to 15min (forward-fill displacement events for ~4h)
    r = df.set_index("ts")[["disp_signal"]].resample("15min").last().reset_index()
    # Rolling: sum of recent displacement signals (last 8h = 32 bars)
    r["disp_signal"] = r["disp_signal"].fillna(0)
    r["disp_ma"] = r["disp_signal"].rolling(32).sum()
    r = r.dropna().reset_index(drop=True)
    print(f"    {len(r):,} 15m bars, {len(df)} displacement events")
    return r


def load_fvg_factor(conn):
    """Fair Value Gaps — count of unfilled bull vs bear FVGs."""
    print("  Loading FVGs...")
    df = pd.read_sql(
        "SELECT fvg_time as ts, fvg_type, status, size FROM public.fvg "
        "WHERE symbol='BTCUSDT' ORDER BY fvg_time", conn, parse_dates=["ts"])
    if df.empty:
        return None

    # Create event series: Bull FVG = +1, Bear FVG = -1
    df["fvg_dir"] = np.where(df["fvg_type"] == "Bull", 1, -1)
    df["is_fresh"] = (df["status"] == "fresh").astype(float)

    # Resample: net FVG direction per 15min window
    r = df.set_index("ts")[["fvg_dir"]].resample("15min").sum().fillna(0).reset_index()
    r["fvg_net_24h"] = r["fvg_dir"].rolling(96).sum()  # net over 24h
    r = r.dropna().reset_index(drop=True)
    print(f"    {len(r):,} 15m bars, {len(df)} FVG events")
    return r


def load_sweep_factor(conn):
    """Liquidity sweeps — SweepHigh (bearish) vs SweepLow (bullish)."""
    print("  Loading liquidity sweeps...")
    df = pd.read_sql(
        "SELECT liq_time as ts, liq_type, count FROM public.liquidity_sweeps "
        "WHERE symbol='BTCUSDT' ORDER BY liq_time", conn, parse_dates=["ts"])
    if df.empty:
        return None

    # SweepHigh = bearish reversal, SweepLow = bullish reversal
    df["sweep_dir"] = np.where(df["liq_type"] == "SweepLow", 1, -1)
    df["sweep_signal"] = df["sweep_dir"] * df["count"]

    r = df.set_index("ts")[["sweep_signal"]].resample("15min").sum().fillna(0).reset_index()
    r["sweep_ma"] = r["sweep_signal"].rolling(32).sum()  # 8h rolling
    r = r.dropna().reset_index(drop=True)
    print(f"    {len(r):,} 15m bars, {len(df)} sweep events")
    return r


# ================================================================
# Score addons — each takes btc_df (merged) and returns additive score
# ================================================================

def score_basis(btc_df, weight=1.5):
    s = pd.Series(0.0, index=btc_df.index)
    if "basis_z" not in btc_df.columns:
        return s
    bz = btc_df["basis_z"].fillna(0)
    # Contrarian: high basis (overheated) = bearish, low basis = bullish
    s += np.where(bz > 1.5, -weight, 0)
    s += np.where(bz > 2.5, -weight * 0.5, 0)
    s += np.where(bz < -1.5, weight, 0)
    s += np.where(bz < -2.5, weight * 0.5, 0)
    return s


def score_basis_momentum(btc_df, weight=1.5):
    s = pd.Series(0.0, index=btc_df.index)
    if "basis_ma" not in btc_df.columns:
        return s
    bm = btc_df["basis_ma"].fillna(0)
    # Momentum: high basis = bullish demand, low = bearish
    s += np.where(bm > 0.0003, weight, 0)
    s += np.where(bm > 0.0006, weight * 0.5, 0)
    s += np.where(bm < -0.0001, -weight, 0)
    s += np.where(bm < -0.0003, -weight * 0.5, 0)
    return s


def score_tick_liq(btc_df, weight=1.5):
    s = pd.Series(0.0, index=btc_df.index)
    if "liq_net_ma" not in btc_df.columns:
        return s
    ln = btc_df["liq_net_ma"].fillna(0)
    lt = btc_df["liq_notional_ma"].fillna(0)
    # Net positive = more short liquidations = short squeeze = bullish
    s += np.where(ln > 2, weight, 0)
    s += np.where(ln < -2, -weight, 0)
    # High total liquidation = cascade/volatility
    lt_mean = lt[lt > 0].mean() if (lt > 0).any() else 1
    s += np.where(lt > lt_mean * 3, weight * 0.5, 0)  # high vol environment
    return s


def score_news(btc_df, weight=1.5):
    s = pd.Series(0.0, index=btc_df.index)
    if "news_net_24h" not in btc_df.columns:
        return s
    nn = btc_df["news_net_24h"].fillna(0)
    # Directional: more bullish news = bullish signal
    s += np.where(nn > 5, weight, 0)
    s += np.where(nn > 10, weight * 0.5, 0)
    s += np.where(nn < -3, -weight, 0)
    s += np.where(nn < -5, -weight * 0.5, 0)
    return s


def score_news_contrarian(btc_df, weight=1.5):
    s = pd.Series(0.0, index=btc_df.index)
    if "news_net_24h" not in btc_df.columns:
        return s
    nn = btc_df["news_net_24h"].fillna(0)
    # Contrarian: too bullish = bearish, too bearish = bullish
    s += np.where(nn > 10, -weight, 0)
    s += np.where(nn < -3, weight, 0)
    return s


def score_displacement(btc_df, weight=1.0):
    s = pd.Series(0.0, index=btc_df.index)
    if "disp_ma" not in btc_df.columns:
        return s
    dm = btc_df["disp_ma"].fillna(0)
    # Momentum: displacement direction = continuation
    s += np.where(dm > 3, weight, 0)
    s += np.where(dm < -3, -weight, 0)
    return s


def score_fvg(btc_df, weight=1.0):
    s = pd.Series(0.0, index=btc_df.index)
    if "fvg_net_24h" not in btc_df.columns:
        return s
    fn = btc_df["fvg_net_24h"].fillna(0)
    # Net Bull FVGs = bullish momentum
    s += np.where(fn > 2, weight, 0)
    s += np.where(fn < -2, -weight, 0)
    return s


def score_sweep(btc_df, weight=1.0):
    s = pd.Series(0.0, index=btc_df.index)
    if "sweep_ma" not in btc_df.columns:
        return s
    sm = btc_df["sweep_ma"].fillna(0)
    # SweepLow = bullish reversal signal
    s += np.where(sm > 1, weight, 0)
    s += np.where(sm < -1, -weight, 0)
    return s


# ================================================================
# Main
# ================================================================

def run_factor_test(factor_name, btc_df_merged, base_score, score_fn, btc_score_ts_base,
                    alt_data, test_start, test_end, weights_to_test):
    """Test a single factor at different weights."""
    results = {}
    for w in weights_to_test:
        addon = score_fn(btc_df_merged, weight=w)
        enhanced = base_score + addon
        score_ts = pd.Series(enhanced.values, index=btc_df_merged["ts"].values)

        total_pnl = 0
        coin_results = {}
        for coin in ALL_COINS:
            if coin not in alt_data:
                continue
            cfg = V11_CONFIGS[coin]
            signals, alt_m = generate_signal_v11(score_ts, alt_data[coin], cfg["threshold"], cfg["alt_pa"])
            trades = run_backtest(alt_m, signals,
                                  sl_atr_mult=cfg["sl"], tp_atr_mult=cfg["tp"],
                                  trail_atr_mult=cfg["trail"], trail_activate_atr=cfg["trail_act"],
                                  cooldown_bars=cfg["cd"])
            m = calc_metrics(trades, len(alt_data[coin]))
            coin_results[coin] = m
            total_pnl += m["net_pnl"]

        results[w] = {"total_pnl": total_pnl, "coins": coin_results}
    return results


def check_correlation(btc_df, feature_col, label):
    """Check correlation of feature with forward BTC returns."""
    df = btc_df[["ts", "close", feature_col]].dropna().copy()
    if len(df) < 200:
        print(f"    {label}: insufficient data ({len(df)} rows)")
        return
    df["fwd_1h"] = df["close"].shift(-4) / df["close"] - 1
    df["fwd_4h"] = df["close"].shift(-16) / df["close"] - 1
    valid = df.dropna()
    c1 = valid[feature_col].corr(valid["fwd_1h"])
    c4 = valid[feature_col].corr(valid["fwd_4h"])
    print(f"    {label:30s}: corr(1h)={c1:+.4f}, corr(4h)={c4:+.4f}  (n={len(valid):,})")


def main():
    print("=" * 70)
    print("NEW FACTOR TEST — 6 unused DB tables")
    print("=" * 70)

    # Load BTC base data
    print("\n[1] Loading BTC base data...")
    btc_ohlcv = fetch_binance_15m("BTCUSDT", years=3)
    btc_db = load_btc_db_data()
    btc_df = build_btc_features(btc_ohlcv, btc_db)
    btc_score_base = compute_btc_composite_score(btc_df, BTC_SCORE_WEIGHTS)

    # Trim to Jun 2025+
    mask = btc_df["ts"] >= pd.Timestamp("2025-06-01")
    btc_df_full = btc_df[mask].reset_index(drop=True)
    btc_score_full = btc_score_base[mask].reset_index(drop=True)
    btc_score_ts = pd.Series(btc_score_full.values, index=btc_df_full["ts"].values)

    # Load new factors
    print("\n[2] Loading new factors from DB...")
    conn = psycopg2.connect(**DB_PARAMS)
    factors = {}
    factors["basis"] = load_basis_factor(conn)
    factors["tick_liq"] = load_tick_liquidation_factor(conn)
    factors["news"] = load_news_factor(conn)
    factors["displacement"] = load_displacement_factor(conn)
    factors["fvg"] = load_fvg_factor(conn)
    factors["sweep"] = load_sweep_factor(conn)
    conn.close()

    # Merge all factors into btc_df
    print("\n[3] Merging factors into BTC DataFrame...")
    btc_merged = btc_df_full.copy()
    for name, fdf in factors.items():
        if fdf is None:
            print(f"    {name}: no data, skipped")
            continue
        merge_cols = [c for c in fdf.columns if c != "ts"]
        btc_merged = pd.merge_asof(
            btc_merged.sort_values("ts"),
            fdf[["ts"] + merge_cols].sort_values("ts"),
            on="ts", direction="backward", tolerance=pd.Timedelta("30min"))
        n_filled = btc_merged[merge_cols[0]].notna().sum()
        print(f"    {name}: merged, {n_filled:,}/{len(btc_merged):,} bars have data ({n_filled/len(btc_merged)*100:.1f}%)")

    # Correlation analysis
    print(f"\n{'='*70}")
    print("[4] CORRELATION WITH FORWARD BTC RETURNS")
    print(f"{'='*70}")

    corr_features = [
        ("basis_rate", "Basis rate (raw)"),
        ("basis_ma", "Basis rate (3h MA)"),
        ("basis_z", "Basis z-score"),
        ("liq_net_ma", "Liq net count (4h MA)"),
        ("liq_notional_ma", "Liq notional (4h MA)"),
        ("news_net_24h", "News net 24h"),
        ("news_ratio_24h", "News ratio 24h"),
        ("disp_ma", "Displacement MA"),
        ("fvg_net_24h", "FVG net 24h"),
        ("sweep_ma", "Sweep MA"),
    ]
    for col, label in corr_features:
        if col in btc_merged.columns:
            check_correlation(btc_merged, col, label)

    # Determine test period (where most factors have data)
    # basis and tick_liq start Sep 2025, news starts Jun 2025
    # Use Jan-Mar 2026 as OOS (same as v1.2 test)
    TEST_START = pd.Timestamp("2026-01-01")
    TEST_END = btc_merged["ts"].iloc[-1]

    # Preload alt test data
    print(f"\n[5] Loading altcoin data (test: {TEST_START.date()} to {TEST_END.date()})...")
    alt_data = {}
    for coin in ALL_COINS:
        ohlcv = fetch_binance_15m(f"{coin}USDT", years=3)
        alt_df = build_alt_technicals(ohlcv)
        alt_test = alt_df[(alt_df["ts"] >= TEST_START) & (alt_df["ts"] <= TEST_END)].reset_index(drop=True)
        if len(alt_test) >= 100:
            alt_data[coin] = alt_test

    # Baseline
    print(f"\n{'='*70}")
    print("[6] BACKTEST: baseline vs each new factor")
    print(f"{'='*70}")

    print("\n--- baseline (8 factors, no additions) ---")
    baseline_total = 0
    for coin in ALL_COINS:
        if coin not in alt_data:
            continue
        cfg = V11_CONFIGS[coin]
        signals, alt_m = generate_signal_v11(btc_score_ts, alt_data[coin], cfg["threshold"], cfg["alt_pa"])
        trades = run_backtest(alt_m, signals,
                              sl_atr_mult=cfg["sl"], tp_atr_mult=cfg["tp"],
                              trail_atr_mult=cfg["trail"], trail_activate_atr=cfg["trail_act"],
                              cooldown_bars=cfg["cd"])
        m = calc_metrics(trades, len(alt_data[coin]))
        baseline_total += m["net_pnl"]
        print(f"  {coin:5s}: {m['total']:3d} trades, WR={m['win_rate']:5.1f}%, PnL=${m['net_pnl']:>+9,.2f}")
    print(f"  TOTAL: ${baseline_total:>+10,.2f}")

    # Test each factor
    factor_tests = [
        ("basis_contrarian", score_basis, [0.5, 1.0, 1.5, 2.0]),
        ("basis_momentum", score_basis_momentum, [0.5, 1.0, 1.5, 2.0]),
        ("tick_liq", score_tick_liq, [0.5, 1.0, 1.5, 2.0]),
        ("news_directional", score_news, [0.5, 1.0, 1.5]),
        ("news_contrarian", score_news_contrarian, [0.5, 1.0, 1.5]),
        ("displacement", score_displacement, [0.5, 1.0, 1.5]),
        ("fvg", score_fvg, [0.5, 1.0, 1.5]),
        ("sweep", score_sweep, [0.5, 1.0, 1.5]),
    ]

    all_results = {"baseline": baseline_total}

    for factor_name, score_fn, weights in factor_tests:
        print(f"\n--- {factor_name} ---")
        results = run_factor_test(
            factor_name, btc_merged, btc_score_full, score_fn,
            btc_score_ts, alt_data, TEST_START, TEST_END, weights)

        best_w = max(results, key=lambda w: results[w]["total_pnl"])
        best_pnl = results[best_w]["total_pnl"]
        all_results[factor_name] = {"best_weight": best_w, "best_pnl": best_pnl, "all": results}

        for w in weights:
            r = results[w]
            delta = r["total_pnl"] - baseline_total
            marker = " ***" if r["total_pnl"] == best_pnl else ""
            print(f"  w={w:.1f}: ${r['total_pnl']:>+10,.2f} (delta: ${delta:>+9,.2f}){marker}")

    # ============================================================
    # Summary
    # ============================================================
    print(f"\n{'='*70}")
    print("SUMMARY — NEW FACTOR TEST RESULTS")
    print(f"{'='*70}")
    print(f"  {'Factor':<25s} {'Best w':>7s} {'PnL':>12s} {'Delta':>12s} {'Verdict':>10s}")
    print(f"  {'-'*25} {'-'*7} {'-'*12} {'-'*12} {'-'*10}")
    print(f"  {'baseline':<25s} {'':>7s} ${baseline_total:>+10,.2f} {'':>12s} {'':>10s}")

    helped = []
    for name in [ft[0] for ft in factor_tests]:
        r = all_results[name]
        delta = r["best_pnl"] - baseline_total
        verdict = "HELPED" if delta > 50 else "NEUTRAL" if delta > -50 else "HURT"
        print(f"  {name:<25s} {r['best_weight']:>7.1f} ${r['best_pnl']:>+10,.2f} ${delta:>+10,.2f} {verdict:>10s}")
        if delta > 50:
            helped.append((name, r["best_weight"], delta))

    if helped:
        print(f"\n  Factors that helped: {', '.join(h[0] for h in helped)}")

        # Test combined
        print(f"\n{'='*70}")
        print("COMBINED — stacking all helpful factors")
        print(f"{'='*70}")

        combined_score = btc_score_full.copy()
        combo_desc = []
        for name, best_w, delta in helped:
            score_fn = dict((ft[0], ft[1]) for ft in factor_tests)[name]
            combined_score = combined_score + score_fn(btc_merged, weight=best_w)
            combo_desc.append(f"{name}(w={best_w})")

        combined_ts = pd.Series(combined_score.values, index=btc_merged["ts"].values)

        print(f"  Combined: {' + '.join(combo_desc)}")
        total_pnl = 0
        for coin in ALL_COINS:
            if coin not in alt_data:
                continue
            cfg = V11_CONFIGS[coin]
            signals, alt_m = generate_signal_v11(combined_ts, alt_data[coin], cfg["threshold"], cfg["alt_pa"])
            trades = run_backtest(alt_m, signals,
                                  sl_atr_mult=cfg["sl"], tp_atr_mult=cfg["tp"],
                                  trail_atr_mult=cfg["trail"], trail_activate_atr=cfg["trail_act"],
                                  cooldown_bars=cfg["cd"])
            m = calc_metrics(trades, len(alt_data[coin]))
            total_pnl += m["net_pnl"]
            print(f"  {coin:5s}: {m['total']:3d} trades, WR={m['win_rate']:5.1f}%, "
                  f"PnL=${m['net_pnl']:>+9,.2f}, PF={m['pf']:.3f}, Sharpe={m['sharpe']:.3f}")
        print(f"  TOTAL: ${total_pnl:>+10,.2f} (baseline: ${baseline_total:>+,.2f}, delta: ${total_pnl - baseline_total:>+,.2f})")
    else:
        print("\n  No factors helped. All new data sources don't improve the model.")

    # Save results to experiments
    print(f"\n{'='*70}")
    print("Saving results...")
    print(f"{'='*70}")

    exp_id = f"new_factors_{datetime.now().strftime('%Y%m%d_%H%M')}"
    registry_path = "experiments/registry.json"
    if os.path.exists(registry_path):
        with open(registry_path, "r") as f:
            registry = json.load(f)
    else:
        registry = []

    summary = {"baseline": baseline_total}
    for name in [ft[0] for ft in factor_tests]:
        r = all_results[name]
        summary[name] = {"best_weight": r["best_weight"], "best_pnl": round(r["best_pnl"], 2),
                          "delta": round(r["best_pnl"] - baseline_total, 2)}

    registry.append({
        "experiment_id": exp_id,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "description": "Test 6 unused DB tables as new BTC composite factors",
        "params": {"test_period": f"{TEST_START.date()} to {TEST_END.date()}",
                    "factors_tested": [ft[0] for ft in factor_tests]},
        "scenarios": summary,
    })

    with open(registry_path, "w") as f:
        json.dump(registry, f, indent=2)

    print(f"  Saved to experiments/registry.json as '{exp_id}'")
    print("\nDone!")


if __name__ == "__main__":
    main()
