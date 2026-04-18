"""
Phase 1 Factor Backtest: CVD, Macro (DXY/Gold/SP500/US10Y), BTC Dominance
==========================================================================
Test whether these new data sources add alpha on top of v3 baseline.

CVD: computed from Binance kline taker_buy_base_asset_volume
Macro: from yfinance historical daily data
BTC.D: from CoinGecko /coins/bitcoin/market_chart (limited -- daily resolution)

Methodology:
1. Compute v3 baseline score + PnL across 6 coins (OOS: Jan-Mar 2026)
2. For each new factor, test at weights [0.5, 1.0, 1.5, 2.0] as additive
3. Compare enhanced PnL vs baseline
4. Report: is it worth collecting?
"""

import os, sys, json, requests
import time as _time
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd
import yfinance as yf
import psycopg2

# ---- Import from existing backtest infrastructure ----
sys.path.insert(0, r"D:\0_product_dev\trade_crypto")
from backtest_15m_btc_led_alts import (
    fetch_binance_15m, load_btc_db_data as _load_btc_db_data_base,
    run_backtest, calc_metrics, BKK_UTC_OFFSET,
)
from test_v12_improvements import V11_CONFIGS, ALL_COINS, generate_signal_v11

# v3 score computation (from paper trading strategy -- has basis, tick_liq, ob)
from paper_trading.strategy import (
    compute_btc_composite_score, build_btc_features, build_alt_technicals,
    score_basis_contrarian, score_tick_liq, score_ob_combined,
)
from paper_trading.config import COMPOSITE_WEIGHTS, V3_EXTRA_WEIGHTS

# ---- Constants ----
OOS_START = pd.Timestamp("2026-01-01")
OOS_END   = pd.Timestamp("2026-03-10")
WEIGHTS_TO_TEST = [0.5, 1.0, 1.5, 2.0]

DB_PARAMS = {
    "dbname": "smart_trading", "user": "postgres",
    "password": "P@ssw0rd", "host": "localhost", "port": "5432",
}


def load_btc_db_data_v3():
    """Load base DB data + v3 factors (basis, tick_liq, ob)."""
    # Base factors
    data = _load_btc_db_data_base()

    # Add v3 factors
    conn = psycopg2.connect(**DB_PARAMS)

    print("  Loading basis (v3)...", end="", flush=True)
    data["basis"] = pd.read_sql(
        "SELECT ts, basis_rate FROM market_data.basis WHERE pair='BTCUSDT' ORDER BY ts",
        conn, parse_dates=["ts"])
    if not data["basis"].empty:
        data["basis"]["ts"] = data["basis"]["ts"].dt.tz_localize(None) - BKK_UTC_OFFSET
    print(f" {len(data['basis']):,}")

    print("  Loading tick_liq (v3)...", end="", flush=True)
    data["tick_liq"] = pd.read_sql(
        "SELECT event_time, side, notional_usd FROM market_data.liquidation "
        "WHERE symbol='BTCUSDT' ORDER BY event_time",
        conn, parse_dates=["event_time"])
    if not data["tick_liq"].empty:
        data["tick_liq"]["ts"] = data["tick_liq"]["event_time"].dt.tz_localize(None) - BKK_UTC_OFFSET
    print(f" {len(data['tick_liq']):,}")

    print("  Loading order book (v3)...", end="", flush=True)
    data["ob"] = pd.read_sql(
        "SELECT fetched_at, "
        "(meta->>'imbalance')::float as imbalance, "
        "(meta->>'bid_sum')::float as bid_sum, "
        "(meta->>'ask_sum')::float as ask_sum "
        "FROM market_data.order_book_raw ORDER BY fetched_at",
        conn, parse_dates=["fetched_at"])
    if not data["ob"].empty:
        data["ob"]["ts"] = data["ob"]["fetched_at"].dt.tz_localize(None) - BKK_UTC_OFFSET
    print(f" {len(data['ob']):,}")

    conn.close()
    return data


# =====================================================================
# 1) CVD LOADER -- from Binance klines (taker_buy_base_asset_volume)
# =====================================================================
def fetch_btc_klines_with_taker_vol(years=1):
    """Fetch BTCUSDT 15m klines including taker buy volume (column [9])."""
    cache_file = "data_cache/BTCUSDT_15m_taker_vol.parquet"
    if os.path.exists(cache_file):
        print("  [CVD] Using cached taker vol data")
        df = pd.read_parquet(cache_file)
        df["ts"] = pd.to_datetime(df["ts"])
        return df

    print("  [CVD] Fetching BTCUSDT 15m klines with taker buy volume...")
    url = "https://fapi.binance.com/fapi/v1/klines"
    end_ts = int(datetime.now().timestamp() * 1000)
    start_ts = end_ts - years * 365 * 24 * 3600 * 1000

    all_data = []
    current = start_ts
    batch = 0
    while current < end_ts:
        params = {"symbol": "BTCUSDT", "interval": "15m", "startTime": current, "limit": 1500}
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
                "ts": pd.Timestamp(k[0], unit="ms"),
                "volume": float(k[5]),
                "taker_buy_vol": float(k[9]),
                "trades": int(k[8]),
            })

        current = data[-1][0] + 1
        batch += 1
        if batch % 20 == 0:
            print(f"    {len(all_data):,} candles...", end="\r", flush=True)

    df = pd.DataFrame(all_data)
    os.makedirs("data_cache", exist_ok=True)
    df.to_parquet(cache_file, index=False)
    print(f"  [CVD] Fetched {len(df):,} candles, cached to {cache_file}")
    return df


def load_cvd_factor():
    """Load CVD from Binance klines and compute features."""
    df = fetch_btc_klines_with_taker_vol(years=1)

    # CVD = taker_buy - taker_sell = 2*taker_buy - total
    df["volume_delta"] = 2 * df["taker_buy_vol"] - df["volume"]
    df["cvd_cumul"] = df["volume_delta"].cumsum()

    # Rolling features
    df["cvd_ma4"] = df["volume_delta"].rolling(4).mean()    # 1h MA
    df["cvd_ma16"] = df["volume_delta"].rolling(16).mean()  # 4h MA
    df["cvd_ma96"] = df["volume_delta"].rolling(96).mean()  # 24h MA

    # Z-score of volume delta (relative to 24h)
    std = df["volume_delta"].rolling(96).std().clip(lower=1e-8)
    df["cvd_z"] = (df["volume_delta"] - df["cvd_ma96"]) / std

    # CVD cumulative trend (slope over 4h)
    df["cvd_slope"] = df["cvd_cumul"].diff(16) / 16

    print(f"  [CVD] {len(df):,} bars, range {df['ts'].min()} to {df['ts'].max()}")
    return df


# =====================================================================
# 2) CVD SCORE FUNCTIONS
# =====================================================================
def score_cvd_momentum(btc_df, weight=1.5):
    """CVD momentum: positive delta = buying pressure = bullish."""
    s = pd.Series(0.0, index=btc_df.index)
    if "cvd_z" not in btc_df.columns:
        return s
    z = btc_df["cvd_z"].fillna(0)
    # Strong buy pressure
    s += np.where(z > 1.5, weight, 0)
    s += np.where(z > 2.5, weight * 0.5, 0)
    # Strong sell pressure
    s += np.where(z < -1.5, -weight, 0)
    s += np.where(z < -2.5, -weight * 0.5, 0)
    return s


def score_cvd_contrarian(btc_df, weight=1.5):
    """CVD contrarian: extreme buying = bearish reversal signal."""
    s = pd.Series(0.0, index=btc_df.index)
    if "cvd_z" not in btc_df.columns:
        return s
    z = btc_df["cvd_z"].fillna(0)
    # Extreme buying = overbought = bearish
    s += np.where(z > 2.0, -weight, 0)
    s += np.where(z > 3.0, -weight * 0.5, 0)
    # Extreme selling = oversold = bullish
    s += np.where(z < -2.0, weight, 0)
    s += np.where(z < -3.0, weight * 0.5, 0)
    return s


def score_cvd_divergence(btc_df, weight=1.5):
    """CVD-price divergence: price up but CVD down = bearish, vice versa."""
    s = pd.Series(0.0, index=btc_df.index)
    if "cvd_ma16" not in btc_df.columns or "ret" not in btc_df.columns:
        return s
    cvd = btc_df["cvd_ma16"].fillna(0)
    ret = btc_df["ret"].fillna(0)
    # Price rising but CVD falling = bearish divergence
    s += np.where((ret > 0.001) & (cvd < 0), -weight, 0)
    # Price falling but CVD rising = bullish divergence
    s += np.where((ret < -0.001) & (cvd > 0), weight, 0)
    return s


# =====================================================================
# 3) MACRO LOADER -- from yfinance
# =====================================================================
def load_macro_factor():
    """Load DXY, US10Y, Gold, SP500 historical data from yfinance."""
    cache_file = "data_cache/macro_historical.parquet"
    if os.path.exists(cache_file):
        print("  [MACRO] Using cached macro data")
        df = pd.read_parquet(cache_file)
        df["ts"] = pd.to_datetime(df["ts"])
        return df

    tickers = {
        "DX-Y.NYB": "dxy",
        "^TNX": "us10y",
        "GC=F": "gold",
        "^GSPC": "sp500",
    }

    frames = []
    for ticker, name in tickers.items():
        print(f"  [MACRO] Fetching {name} ({ticker})...")
        data = yf.download(ticker, period="2y", interval="1d", progress=False)
        if data.empty:
            print(f"    WARNING: no data for {ticker}")
            continue
        if hasattr(data.columns, "levels") and len(data.columns.levels) > 1:
            data.columns = data.columns.get_level_values(0)
        s = data[["Close"]].rename(columns={"Close": name})
        frames.append(s)

    if not frames:
        return None

    merged = frames[0]
    for f in frames[1:]:
        merged = merged.join(f, how="outer")

    merged = merged.ffill().reset_index()
    merged = merged.rename(columns={"Date": "ts"})
    merged["ts"] = pd.to_datetime(merged["ts"]).dt.tz_localize(None)

    os.makedirs("data_cache", exist_ok=True)
    merged.to_parquet(cache_file, index=False)
    print(f"  [MACRO] {len(merged)} daily bars, range {merged['ts'].min()} to {merged['ts'].max()}")
    return merged


def engineer_macro_features(macro_df):
    """Compute daily changes and rolling features for macro indicators."""
    df = macro_df.copy()

    for col in ["dxy", "us10y", "gold", "sp500"]:
        if col in df.columns:
            df[f"{col}_chg"] = df[col].pct_change()
            df[f"{col}_chg5d"] = df[col].pct_change(5)  # 5-day change

    # DXY strength index: DXY up + US10Y up = very risk-off
    if "dxy_chg" in df.columns and "us10y_chg" in df.columns:
        df["risk_off"] = df["dxy_chg"].fillna(0) + df["us10y_chg"].fillna(0)

    return df


# =====================================================================
# 4) MACRO SCORE FUNCTIONS
# =====================================================================
def score_macro_dxy(btc_df, weight=1.5):
    """DXY score: dollar strength = bearish for crypto."""
    s = pd.Series(0.0, index=btc_df.index)
    if "dxy_chg5d" not in btc_df.columns:
        return s
    chg = btc_df["dxy_chg5d"].fillna(0)
    # DXY rising strongly = risk-off = bearish
    s += np.where(chg > 0.005, -weight, 0)
    s += np.where(chg > 0.01, -weight * 0.5, 0)
    # DXY falling = risk-on = bullish
    s += np.where(chg < -0.005, weight, 0)
    s += np.where(chg < -0.01, weight * 0.5, 0)
    return s


def score_macro_sp500(btc_df, weight=1.5):
    """SP500 score: equity strength = risk-on = bullish for crypto."""
    s = pd.Series(0.0, index=btc_df.index)
    if "sp500_chg5d" not in btc_df.columns:
        return s
    chg = btc_df["sp500_chg5d"].fillna(0)
    # SP500 rising = risk-on = bullish
    s += np.where(chg > 0.01, weight, 0)
    s += np.where(chg > 0.02, weight * 0.5, 0)
    # SP500 falling = risk-off = bearish
    s += np.where(chg < -0.01, -weight, 0)
    s += np.where(chg < -0.02, -weight * 0.5, 0)
    return s


def score_macro_risk_off(btc_df, weight=1.5):
    """Combined risk-off: DXY up + US10Y up + SP500 down = bearish."""
    s = pd.Series(0.0, index=btc_df.index)
    if "risk_off" not in btc_df.columns:
        return s
    ro = btc_df["risk_off"].fillna(0)
    # Strong risk-off signal
    s += np.where(ro > 0.01, -weight, 0)
    # Strong risk-on signal
    s += np.where(ro < -0.01, weight, 0)
    return s


def score_macro_gold(btc_df, weight=1.0):
    """Gold score: gold rising = mixed, but extreme moves = risk signal."""
    s = pd.Series(0.0, index=btc_df.index)
    if "gold_chg5d" not in btc_df.columns:
        return s
    chg = btc_df["gold_chg5d"].fillna(0)
    # Gold surging = fear/risk-off = mild bearish for crypto
    s += np.where(chg > 0.02, -weight * 0.5, 0)
    # Gold dumping = risk-on = mild bullish
    s += np.where(chg < -0.02, weight * 0.5, 0)
    return s


# =====================================================================
# 5) BTC.D LOADER -- from CoinGecko market_chart (daily resolution)
# =====================================================================
def load_btcd_factor():
    """
    Load BTC market cap history from CoinGecko.
    We can't get dominance directly, but we can use BTC market cap trend
    as a proxy for dominance changes.
    """
    cache_file = "data_cache/btc_mcap_historical.parquet"
    if os.path.exists(cache_file):
        print("  [BTC.D] Using cached market cap data")
        df = pd.read_parquet(cache_file)
        df["ts"] = pd.to_datetime(df["ts"])
        return df

    print("  [BTC.D] Fetching BTC market cap from CoinGecko (365d)...")
    url = "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart"
    params = {"vs_currency": "usd", "days": 365, "interval": "daily"}

    try:
        r = requests.get(url, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"  [BTC.D] Failed to fetch: {e}")
        return None

    # Also fetch total market cap
    print("  [BTC.D] Fetching total crypto market chart...")
    _time.sleep(5)  # CoinGecko rate limit
    try:
        url2 = "https://api.coingecko.com/api/v3/global"
        # Can't get historical total, so we'll use BTC mcap trend only
    except:
        pass

    mcaps = data.get("market_caps", [])
    rows = []
    for ts_ms, mcap in mcaps:
        rows.append({
            "ts": pd.Timestamp(ts_ms, unit="ms").tz_localize(None),
            "btc_mcap": mcap,
        })

    df = pd.DataFrame(rows)
    if df.empty:
        return None

    # Compute features from BTC market cap alone
    df["btc_mcap_chg"] = df["btc_mcap"].pct_change()
    df["btc_mcap_chg5d"] = df["btc_mcap"].pct_change(5)
    df["btc_mcap_ma7d"] = df["btc_mcap"].rolling(7).mean()
    df["btc_mcap_z"] = (
        (df["btc_mcap"] - df["btc_mcap_ma7d"])
        / df["btc_mcap"].rolling(30).std().clip(lower=1e-8)
    )

    os.makedirs("data_cache", exist_ok=True)
    df.to_parquet(cache_file, index=False)
    print(f"  [BTC.D] {len(df)} daily bars")
    return df


def score_btc_mcap_trend(btc_df, weight=1.0):
    """BTC market cap trend: rising mcap = bullish momentum."""
    s = pd.Series(0.0, index=btc_df.index)
    if "btc_mcap_chg5d" not in btc_df.columns:
        return s
    chg = btc_df["btc_mcap_chg5d"].fillna(0)
    s += np.where(chg > 0.05, weight, 0)
    s += np.where(chg < -0.05, -weight, 0)
    return s


# =====================================================================
# 6) MAIN BACKTEST
# =====================================================================
def run_v3_baseline(btc_df, btc_score):
    """Run v3 baseline across all 6 coins, return total OOS PnL."""
    total_pnl = 0.0
    results = {}

    for coin in ALL_COINS:
        cfg = V11_CONFIGS[coin]
        symbol = f"{coin}USDT"
        alt_raw = fetch_binance_15m(symbol, years=3)
        alt_df = build_alt_technicals(alt_raw)
        alt_df = alt_df[(alt_df["ts"] >= OOS_START) & (alt_df["ts"] <= OOS_END)].copy()

        if alt_df.empty:
            continue

        signal, alt_merged = generate_signal_v11(
            btc_score, alt_df, cfg["threshold"], cfg["alt_pa"]
        )

        trades = run_backtest(
            alt_merged, signal,
            sl_atr_mult=cfg["sl"], tp_atr_mult=cfg["tp"],
            trail_atr_mult=cfg["trail"], trail_activate_atr=cfg["trail_act"],
            max_hold_bars=96, cooldown_bars=cfg["cd"],
        )

        if not trades.empty:
            pnl = trades["pnl_net"].sum()
            n_trades = len(trades)
        else:
            pnl = 0.0
            n_trades = 0

        total_pnl += pnl
        results[coin] = {"pnl": pnl, "trades": n_trades}

    return total_pnl, results


def test_factor_addon(btc_df, base_score_ts, factor_name, score_fn, weights_to_test):
    """Test a score function as additive to v3 baseline at various weights."""
    results = {}

    for w in weights_to_test:
        addon = score_fn(btc_df, weight=w)
        # addon has integer index, base_score_ts has timestamp index
        # Create enhanced score with timestamp index
        enhanced_vals = base_score_ts.values + addon.values
        enhanced_score = pd.Series(enhanced_vals, index=base_score_ts.index)

        total_pnl = 0.0
        total_trades = 0

        for coin in ALL_COINS:
            cfg = V11_CONFIGS[coin]
            symbol = f"{coin}USDT"
            alt_raw = fetch_binance_15m(symbol, years=3)
            alt_df = build_alt_technicals(alt_raw)
            alt_df = alt_df[(alt_df["ts"] >= OOS_START) & (alt_df["ts"] <= OOS_END)].copy()

            if alt_df.empty:
                continue

            signal, alt_merged = generate_signal_v11(
                enhanced_score, alt_df, cfg["threshold"], cfg["alt_pa"]
            )

            trades = run_backtest(
                alt_merged, signal,
                sl_atr_mult=cfg["sl"], tp_atr_mult=cfg["tp"],
                trail_atr_mult=cfg["trail"], trail_activate_atr=cfg["trail_act"],
                max_hold_bars=96, cooldown_bars=cfg["cd"],
            )

            if not trades.empty:
                total_pnl += trades["pnl_net"].sum()
                total_trades += len(trades)

        results[w] = {"pnl": total_pnl, "trades": total_trades}

    return results


def main():
    print("=" * 70)
    print("Phase 1 Factor Backtest: CVD / Macro / BTC.D")
    print("=" * 70)

    # ---- Load BTC data and compute v3 baseline ----
    print("\n[1] Loading BTC data + v3 baseline (with v3 factors)...")
    btc_raw = fetch_binance_15m("BTCUSDT", years=3)

    db_data = load_btc_db_data_v3()

    btc_df = build_btc_features(btc_raw, db_data)
    btc_df = btc_df[btc_df["ts"] >= pd.Timestamp("2025-06-01")].copy().reset_index(drop=True)

    # v3 composite score
    btc_score = compute_btc_composite_score(btc_df)
    btc_score_ts = btc_score.copy()
    btc_score_ts.index = btc_df["ts"]

    print("\n[2] Running v3 baseline (OOS: Jan-Mar 2026)...")
    baseline_pnl, baseline_results = run_v3_baseline(btc_df, btc_score_ts)
    print(f"\n  V3 BASELINE: ${baseline_pnl:,.0f}")
    for coin, r in baseline_results.items():
        print(f"    {coin}: ${r['pnl']:,.0f} ({r['trades']} trades)")

    # ---- Load new factors ----
    print("\n[3] Loading new factor data...")

    # CVD
    cvd_df = load_cvd_factor()
    if cvd_df is not None:
        btc_df = pd.merge_asof(
            btc_df.sort_values("ts"),
            cvd_df[["ts", "volume_delta", "cvd_z", "cvd_ma4", "cvd_ma16",
                     "cvd_ma96", "cvd_slope"]].sort_values("ts"),
            on="ts", direction="backward", tolerance=pd.Timedelta("30min")
        )

    # Macro
    macro_df = load_macro_factor()
    if macro_df is not None:
        macro_df = engineer_macro_features(macro_df)
        macro_cols = [c for c in macro_df.columns if c != "ts"]
        btc_df = pd.merge_asof(
            btc_df.sort_values("ts"),
            macro_df[["ts"] + macro_cols].sort_values("ts"),
            on="ts", direction="backward", tolerance=pd.Timedelta("3d")
        )

    # BTC.D (market cap proxy)
    btcd_df = load_btcd_factor()
    if btcd_df is not None:
        btcd_cols = ["ts", "btc_mcap_chg5d", "btc_mcap_z"]
        btc_df = pd.merge_asof(
            btc_df.sort_values("ts"),
            btcd_df[btcd_cols].sort_values("ts"),
            on="ts", direction="backward", tolerance=pd.Timedelta("3d")
        )

    # Recompute score with merged data (same as before, just more columns available)
    btc_score = compute_btc_composite_score(btc_df)
    btc_score_ts = btc_score.copy()
    btc_score_ts.index = btc_df["ts"]

    # ---- Test each new factor ----
    print("\n[4] Testing new factors (additive to v3 baseline)...")
    print("=" * 70)

    all_tests = []

    # -- CVD variants --
    cvd_tests = [
        ("cvd_momentum", score_cvd_momentum),
        ("cvd_contrarian", score_cvd_contrarian),
        ("cvd_divergence", score_cvd_divergence),
    ]

    for name, fn in cvd_tests:
        print(f"\n  Testing: {name}")
        results = test_factor_addon(btc_df, btc_score_ts, name, fn, WEIGHTS_TO_TEST)
        for w, r in sorted(results.items()):
            delta = r["pnl"] - baseline_pnl
            marker = " ***" if delta > 200 else ""
            print(f"    w={w:.1f}: ${r['pnl']:,.0f} (delta={delta:+,.0f}, {r['trades']} trades){marker}")
            all_tests.append({
                "factor": name, "weight": w,
                "pnl": r["pnl"], "delta": delta, "trades": r["trades"]
            })

    # -- Macro variants --
    macro_tests = [
        ("macro_dxy", score_macro_dxy),
        ("macro_sp500", score_macro_sp500),
        ("macro_risk_off", score_macro_risk_off),
        ("macro_gold", score_macro_gold),
    ]

    for name, fn in macro_tests:
        print(f"\n  Testing: {name}")
        results = test_factor_addon(btc_df, btc_score_ts, name, fn, WEIGHTS_TO_TEST)
        for w, r in sorted(results.items()):
            delta = r["pnl"] - baseline_pnl
            marker = " ***" if delta > 200 else ""
            print(f"    w={w:.1f}: ${r['pnl']:,.0f} (delta={delta:+,.0f}, {r['trades']} trades){marker}")
            all_tests.append({
                "factor": name, "weight": w,
                "pnl": r["pnl"], "delta": delta, "trades": r["trades"]
            })

    # -- BTC.D --
    if btcd_df is not None:
        btcd_tests = [("btc_mcap_trend", score_btc_mcap_trend)]
        for name, fn in btcd_tests:
            print(f"\n  Testing: {name}")
            results = test_factor_addon(btc_df, btc_score_ts, name, fn, WEIGHTS_TO_TEST)
            for w, r in sorted(results.items()):
                delta = r["pnl"] - baseline_pnl
                marker = " ***" if delta > 200 else ""
                print(f"    w={w:.1f}: ${r['pnl']:,.0f} (delta={delta:+,.0f}, {r['trades']} trades){marker}")
                all_tests.append({
                    "factor": name, "weight": w,
                    "pnl": r["pnl"], "delta": delta, "trades": r["trades"]
                })

    # ---- Summary ----
    print("\n" + "=" * 70)
    print("SUMMARY: Phase 1 Factor Results")
    print("=" * 70)
    print(f"\nV3 Baseline: ${baseline_pnl:,.0f}")
    print(f"\nBest result per factor:")

    factor_best = {}
    for t in all_tests:
        fname = t["factor"]
        if fname not in factor_best or t["delta"] > factor_best[fname]["delta"]:
            factor_best[fname] = t

    print(f"\n{'Factor':<22} {'Best W':>6} {'PnL':>10} {'Delta':>10} {'Trades':>7} {'Verdict':>12}")
    print("-" * 70)

    for fname, best in sorted(factor_best.items(), key=lambda x: -x[1]["delta"]):
        verdict = "COLLECT" if best["delta"] > 200 else "MARGINAL" if best["delta"] > 0 else "SKIP"
        print(f"{fname:<22} {best['weight']:>6.1f} ${best['pnl']:>9,.0f} {best['delta']:>+9,.0f} "
              f"{best['trades']:>7} {verdict:>12}")

    print(f"\n{'='*70}")
    print("Recommendation:")
    worth_collecting = [f for f, b in factor_best.items() if b["delta"] > 200]
    marginal = [f for f, b in factor_best.items() if 0 < b["delta"] <= 200]
    skip = [f for f, b in factor_best.items() if b["delta"] <= 0]

    if worth_collecting:
        print(f"  COLLECT: {', '.join(worth_collecting)}")
    if marginal:
        print(f"  MARGINAL (monitor): {', '.join(marginal)}")
    if skip:
        print(f"  SKIP (not worth): {', '.join(skip)}")

    # Save results
    os.makedirs("experiments", exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = {
        "timestamp": ts,
        "baseline_pnl": baseline_pnl,
        "baseline_results": baseline_results,
        "factor_tests": all_tests,
        "factor_best": {k: v for k, v in factor_best.items()},
    }
    with open(f"experiments/phase1_factors_{ts}.json", "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"\nResults saved to experiments/phase1_factors_{ts}.json")


if __name__ == "__main__":
    main()
