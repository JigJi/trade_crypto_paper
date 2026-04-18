"""
v4 Factor Backtest: v3 + Free Historical Data Sources
======================================================
Test whether free data sources that can be backfilled historically
(no need to collect in advance) add alpha on top of v3 baseline.

New factors:
  1. DVOL (Deribit Volatility Index) -- hourly, contrarian + momentum
  2. Stablecoin Supply (USDT+USDC from DefiLlama) -- daily, supply growth
  3. Hashrate (Blockchain.com) -- daily, miner health
  4. Active Addresses (Blockchain.com) -- daily, on-chain activity
  5. DEX/CEX Volume Ratio (DefiLlama) -- daily, retail mania contrarian

Phase 1 winners (already tested):
  - cvd_contrarian (w=1.5): +$667
  - macro_risk_off (w=0.5): +$785

Methodology:
  A. Individual factor test: each factor × weights [0.5, 1.0, 1.5, 2.0]
  B. Stepwise v4 build: combine best factors greedily
  C. Comparison report: v3 vs v4
"""

import os, sys, json, requests
import time as _time
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd

# ---- Import from existing backtest infrastructure ----
sys.path.insert(0, r"D:\0_product_dev\trade_crypto")
from backtest_15m_btc_led_alts import (
    fetch_binance_15m, load_btc_db_data as _load_btc_db_data_base,
    run_backtest, calc_metrics, BKK_UTC_OFFSET,
)
from test_v12_improvements import V11_CONFIGS, ALL_COINS, generate_signal_v11

from paper_trading.strategy import (
    compute_btc_composite_score, build_btc_features, build_alt_technicals,
    score_basis_contrarian, score_tick_liq, score_ob_combined,
)
from paper_trading.config import COMPOSITE_WEIGHTS, V3_EXTRA_WEIGHTS

# Phase 1 winners
from test_phase1_factors import (
    load_btc_db_data_v3, load_cvd_factor, score_cvd_contrarian,
    load_macro_factor, engineer_macro_features, score_macro_risk_off,
)

# ---- Constants ----
OOS_START = pd.Timestamp("2026-01-01")
OOS_END   = pd.Timestamp("2026-03-10")
WEIGHTS_TO_TEST = [0.5, 1.0, 1.5, 2.0]
CACHE_DIR = "data_cache"


# =====================================================================
# 1) DVOL LOADER -- from Deribit public API (hourly)
# =====================================================================
def load_dvol_historical():
    """Fetch BTC DVOL index hourly data from Deribit, cache to parquet."""
    cache_file = f"{CACHE_DIR}/dvol_historical.parquet"
    if os.path.exists(cache_file):
        print("  [DVOL] Using cached data")
        df = pd.read_parquet(cache_file)
        df["ts"] = pd.to_datetime(df["ts"])
        return df

    print("  [DVOL] Fetching from Deribit API (hourly, ~14 months)...")
    os.makedirs(CACHE_DIR, exist_ok=True)

    url = "https://www.deribit.com/api/v2/public/get_volatility_index_data"
    start_dt = datetime(2025, 1, 1, tzinfo=timezone.utc)
    end_dt = datetime.now(timezone.utc)
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    all_data = []
    current_start = start_ms
    batch = 0

    while current_start < end_ms:
        # Deribit returns max ~1000 candles per call = ~41 days at 1h
        current_end = min(current_start + 41 * 24 * 3600 * 1000, end_ms)

        for attempt in range(5):
            try:
                r = requests.get(url, params={
                    "currency": "BTC",
                    "resolution": 3600,
                    "start_timestamp": current_start,
                    "end_timestamp": current_end,
                }, timeout=30)
                r.raise_for_status()
                resp = r.json()
                break
            except Exception as e:
                if attempt < 4:
                    _time.sleep(3)
                else:
                    print(f"    WARNING: failed batch at {current_start}: {e}")
                    resp = {"result": {"data": []}}

        data = resp.get("result", {}).get("data", [])
        if not data:
            current_start = current_end + 1
            continue

        for row in data:
            # [timestamp, open, high, low, close]
            # Unix epoch ms -> naive UTC (same as Binance convention)
            all_data.append({
                "ts": pd.Timestamp(row[0], unit="ms"),
                "dvol_open": row[1],
                "dvol_high": row[2],
                "dvol_low": row[3],
                "dvol": row[4],  # close = current DVOL level
            })

        current_start = data[-1][0] + 1
        batch += 1
        if batch % 5 == 0:
            print(f"    {len(all_data):,} candles...", end="\r", flush=True)

        # Stop if we got very few candles (near end of data)
        if len(data) < 10:
            break

        _time.sleep(0.5)  # rate limit

    if not all_data:
        print("  [DVOL] No data fetched!")
        return None

    df = pd.DataFrame(all_data)

    # Compute features BEFORE shifting (rolling ops work on original timeline)
    df["dvol_ma24"] = df["dvol"].rolling(24).mean()  # 24h MA
    roll_mean = df["dvol"].rolling(168).mean()  # 7d rolling mean
    roll_std = df["dvol"].rolling(168).std().clip(lower=0.1)
    df["dvol_z"] = (df["dvol"] - roll_mean) / roll_std
    df["dvol_chg24"] = df["dvol"].diff(24)  # 24h absolute change

    # ANTI-LOOKAHEAD: Deribit candle ts = candle START. Close value is at ts+1h.
    # Shift forward 1h so ts represents when the data becomes available.
    df["ts"] = df["ts"] + pd.Timedelta("1h")

    df.to_parquet(cache_file, index=False)
    print(f"  [DVOL] Fetched {len(df):,} hourly candles, range {df['ts'].min()} to {df['ts'].max()}")
    return df


# =====================================================================
# 2) STABLECOIN SUPPLY LOADER -- from DefiLlama
# =====================================================================
def load_stablecoin_supply():
    """Fetch USDT + USDC daily supply from DefiLlama stablecoins API."""
    cache_file = f"{CACHE_DIR}/stablecoin_supply.parquet"
    if os.path.exists(cache_file):
        print("  [STABLE] Using cached data")
        df = pd.read_parquet(cache_file)
        df["ts"] = pd.to_datetime(df["ts"])
        return df

    print("  [STABLE] Fetching from DefiLlama stablecoins API...")
    os.makedirs(CACHE_DIR, exist_ok=True)

    stables = {
        "1": "usdt",   # Tether
        "2": "usdc",   # USD Coin
    }

    frames = {}
    for sid, name in stables.items():
        url = f"https://stablecoins.llama.fi/stablecoin/{sid}"
        for attempt in range(3):
            try:
                r = requests.get(url, timeout=30)
                r.raise_for_status()
                data = r.json()
                break
            except Exception as e:
                if attempt < 2:
                    _time.sleep(3)
                else:
                    print(f"    WARNING: failed to fetch {name}: {e}")
                    data = None

        if not data:
            continue

        # Extract totalCirculating over time
        tokens = data.get("tokens", [])
        rows = []
        for entry in tokens:
            ts = entry.get("date")
            circ = entry.get("circulating", {})
            # peggedUSD is the main metric
            pegged = circ.get("peggedUSD", 0)
            if ts and pegged:
                rows.append({
                    "ts": pd.Timestamp(int(ts), unit="s"),
                    name: pegged,
                })

        if rows:
            frames[name] = pd.DataFrame(rows)
            print(f"    {name}: {len(rows)} daily points")
        _time.sleep(1)

    if not frames:
        print("  [STABLE] No data fetched!")
        return None

    # Merge USDT + USDC
    if "usdt" in frames and "usdc" in frames:
        df = pd.merge(frames["usdt"], frames["usdc"], on="ts", how="outer")
    elif "usdt" in frames:
        df = frames["usdt"]
    else:
        df = frames["usdc"]

    df = df.sort_values("ts").ffill()
    df["stable_total"] = df.get("usdt", 0) + df.get("usdc", 0)
    df["stable_chg7d"] = df["stable_total"].pct_change(7)

    # ANTI-LOOKAHEAD: daily snapshot may not be final until end of day.
    # Shift +1d so ts represents when the data is reliably available.
    df["ts"] = df["ts"] + pd.Timedelta("1d")

    df.to_parquet(cache_file, index=False)
    print(f"  [STABLE] {len(df)} daily points, range {df['ts'].min()} to {df['ts'].max()}")
    return df


# =====================================================================
# 3) HASHRATE LOADER -- from Blockchain.com
# =====================================================================
def load_hashrate():
    """Fetch BTC hashrate from Blockchain.com charts API."""
    cache_file = f"{CACHE_DIR}/hashrate_historical.parquet"
    if os.path.exists(cache_file):
        print("  [HASH] Using cached data")
        df = pd.read_parquet(cache_file)
        df["ts"] = pd.to_datetime(df["ts"])
        return df

    print("  [HASH] Fetching from Blockchain.com API (2 years)...")
    os.makedirs(CACHE_DIR, exist_ok=True)

    url = "https://api.blockchain.info/charts/hash-rate"
    params = {"timespan": "2years", "format": "json", "cors": "true"}

    for attempt in range(3):
        try:
            r = requests.get(url, params=params, timeout=30)
            r.raise_for_status()
            data = r.json()
            break
        except Exception as e:
            if attempt < 2:
                _time.sleep(3)
            else:
                print(f"  [HASH] Failed to fetch: {e}")
                return None

    values = data.get("values", [])
    if not values:
        print("  [HASH] No data!")
        return None

    rows = [{"ts": pd.Timestamp(v["x"], unit="s"), "hashrate": v["y"]} for v in values]
    df = pd.DataFrame(rows)

    # Features (compute BEFORE shifting)
    df["hashrate_chg7d"] = df["hashrate"].pct_change(7)
    df["hashrate_chg30d"] = df["hashrate"].pct_change(30)

    # ANTI-LOOKAHEAD: hashrate is a daily aggregate (avg hash rate over full day).
    # Not known until end of day. Shift +1d so ts = when data is available.
    df["ts"] = df["ts"] + pd.Timedelta("1d")

    df.to_parquet(cache_file, index=False)
    print(f"  [HASH] {len(df)} daily points, range {df['ts'].min()} to {df['ts'].max()}")
    return df


# =====================================================================
# 4) ACTIVE ADDRESSES LOADER -- from Blockchain.com
# =====================================================================
def load_active_addresses():
    """Fetch BTC unique active addresses from Blockchain.com charts API."""
    cache_file = f"{CACHE_DIR}/active_addresses.parquet"
    if os.path.exists(cache_file):
        print("  [ADDR] Using cached data")
        df = pd.read_parquet(cache_file)
        df["ts"] = pd.to_datetime(df["ts"])
        return df

    print("  [ADDR] Fetching from Blockchain.com API (2 years)...")
    os.makedirs(CACHE_DIR, exist_ok=True)

    url = "https://api.blockchain.info/charts/n-unique-addresses"
    params = {"timespan": "2years", "format": "json", "cors": "true"}

    for attempt in range(3):
        try:
            r = requests.get(url, params=params, timeout=30)
            r.raise_for_status()
            data = r.json()
            break
        except Exception as e:
            if attempt < 2:
                _time.sleep(3)
            else:
                print(f"  [ADDR] Failed to fetch: {e}")
                return None

    values = data.get("values", [])
    if not values:
        print("  [ADDR] No data!")
        return None

    rows = [{"ts": pd.Timestamp(v["x"], unit="s"), "active_addr": v["y"]} for v in values]
    df = pd.DataFrame(rows)

    # Features (compute BEFORE shifting)
    df["active_addr_chg7d"] = df["active_addr"].pct_change(7)
    roll_mean = df["active_addr"].rolling(30).mean()
    roll_std = df["active_addr"].rolling(30).std().clip(lower=1)
    df["active_addr_z"] = (df["active_addr"] - roll_mean) / roll_std

    # ANTI-LOOKAHEAD: daily aggregate count. Not known until end of day.
    # Shift +1d so ts = when data is available.
    df["ts"] = df["ts"] + pd.Timedelta("1d")

    df.to_parquet(cache_file, index=False)
    print(f"  [ADDR] {len(df)} daily points, range {df['ts'].min()} to {df['ts'].max()}")
    return df


# =====================================================================
# 5) DEX VOLUME LOADER -- from DefiLlama
# =====================================================================
def load_dex_volume():
    """Fetch daily DEX volume from DefiLlama overview/dexs API."""
    cache_file = f"{CACHE_DIR}/dex_cex_ratio.parquet"
    if os.path.exists(cache_file):
        print("  [DEX] Using cached data")
        df = pd.read_parquet(cache_file)
        df["ts"] = pd.to_datetime(df["ts"])
        return df

    print("  [DEX] Fetching from DefiLlama DEX overview API...")
    os.makedirs(CACHE_DIR, exist_ok=True)

    url = "https://api.llama.fi/overview/dexs"
    params = {"excludeTotalDataChart": "false"}

    for attempt in range(3):
        try:
            r = requests.get(url, params=params, timeout=60)
            r.raise_for_status()
            data = r.json()
            break
        except Exception as e:
            if attempt < 2:
                _time.sleep(5)
            else:
                print(f"  [DEX] Failed to fetch: {e}")
                return None

    chart = data.get("totalDataChart", [])
    if not chart:
        print("  [DEX] No chart data!")
        return None

    rows = [{"ts": pd.Timestamp(int(entry[0]), unit="s"), "dex_vol_24h": entry[1]}
            for entry in chart if len(entry) >= 2]
    df = pd.DataFrame(rows)

    # Compute rolling features (BEFORE shifting)
    df["dex_vol_ma7d"] = df["dex_vol_24h"].rolling(7).mean()
    df["dex_vol_z"] = (
        (df["dex_vol_24h"] - df["dex_vol_ma7d"])
        / df["dex_vol_24h"].rolling(30).std().clip(lower=1)
    )

    # ANTI-LOOKAHEAD: daily DEX volume = full-day aggregate.
    # Not known until end of day. Shift +1d so ts = when data is available.
    df["ts"] = df["ts"] + pd.Timedelta("1d")

    df.to_parquet(cache_file, index=False)
    print(f"  [DEX] {len(df)} daily points, range {df['ts'].min()} to {df['ts'].max()}")
    return df


# =====================================================================
# SCORE FUNCTIONS
# =====================================================================

def score_dvol_level(btc_df, weight=1.5):
    """DVOL level contrarian: very high DVOL = fear peak = bullish reversal.
    Very low DVOL = complacent = bearish."""
    s = pd.Series(0.0, index=btc_df.index)
    if "dvol" not in btc_df.columns:
        return s
    dvol = btc_df["dvol"].fillna(0)
    # Very high vol (>70) = fear exhaustion = bullish
    s += np.where(dvol > 70, weight, 0)
    s += np.where(dvol > 85, weight * 0.5, 0)
    # Very low vol (<40) = complacency = bearish
    s += np.where(dvol < 40, -weight, 0)
    s += np.where(dvol < 30, -weight * 0.5, 0)
    return s


def score_dvol_change(btc_df, weight=1.5):
    """DVOL change momentum: DVOL rising fast = uncertainty = bearish.
    DVOL falling = calming = bullish."""
    s = pd.Series(0.0, index=btc_df.index)
    if "dvol_chg24" not in btc_df.columns:
        return s
    chg = btc_df["dvol_chg24"].fillna(0)
    # DVOL rising fast = volatility spike = bearish
    s += np.where(chg > 5, -weight, 0)
    s += np.where(chg > 10, -weight * 0.5, 0)
    # DVOL falling fast = calming = bullish
    s += np.where(chg < -5, weight, 0)
    s += np.where(chg < -10, weight * 0.5, 0)
    return s


def score_stable_supply(btc_df, weight=1.0):
    """Stablecoin supply: growth = new capital inflow = bullish.
    Shrinking = outflow = bearish."""
    s = pd.Series(0.0, index=btc_df.index)
    if "stable_chg7d" not in btc_df.columns:
        return s
    chg = btc_df["stable_chg7d"].fillna(0)
    # Supply growing >1% in 7d = fresh capital = bullish
    s += np.where(chg > 0.01, weight, 0)
    s += np.where(chg > 0.02, weight * 0.5, 0)
    # Supply shrinking = outflow = bearish
    s += np.where(chg < -0.005, -weight, 0)
    s += np.where(chg < -0.015, -weight * 0.5, 0)
    return s


def score_hashrate(btc_df, weight=1.0):
    """Hashrate: dropping = miner stress = short-term bearish.
    Rising steadily = healthy = neutral/bullish."""
    s = pd.Series(0.0, index=btc_df.index)
    if "hashrate_chg7d" not in btc_df.columns:
        return s
    chg7 = btc_df["hashrate_chg7d"].fillna(0)
    # Hashrate dropping >5% in 7d = miner capitulation = bearish
    s += np.where(chg7 < -0.05, -weight, 0)
    s += np.where(chg7 < -0.10, -weight * 0.5, 0)
    # Hashrate rising >5% in 7d = healthy network = mild bullish
    s += np.where(chg7 > 0.05, weight * 0.5, 0)
    return s


def score_active_addr(btc_df, weight=1.0):
    """Active addresses: rising activity = bullish adoption.
    Falling activity = bearish."""
    s = pd.Series(0.0, index=btc_df.index)
    if "active_addr_z" not in btc_df.columns:
        return s
    z = btc_df["active_addr_z"].fillna(0)
    # High activity (z > 1.5) = strong adoption = bullish
    s += np.where(z > 1.5, weight, 0)
    s += np.where(z > 2.5, weight * 0.5, 0)
    # Low activity (z < -1.5) = waning interest = bearish
    s += np.where(z < -1.5, -weight, 0)
    s += np.where(z < -2.5, -weight * 0.5, 0)
    return s


def score_dex_ratio(btc_df, weight=1.0):
    """DEX volume contrarian: DEX volume spiking = retail mania = potential top = bearish.
    Low DEX activity = accumulation = neutral."""
    s = pd.Series(0.0, index=btc_df.index)
    if "dex_vol_z" not in btc_df.columns:
        return s
    z = btc_df["dex_vol_z"].fillna(0)
    # DEX volume spiking (z > 2) = retail mania = bearish
    s += np.where(z > 2.0, -weight, 0)
    s += np.where(z > 3.0, -weight * 0.5, 0)
    # DEX volume very low (z < -1.5) = accumulation = mild bullish
    s += np.where(z < -1.5, weight * 0.5, 0)
    return s


# =====================================================================
# BACKTEST INFRASTRUCTURE (reuse from test_phase1_factors)
# =====================================================================

def run_v3_baseline(btc_df, btc_score):
    """Run v3 baseline across all 6 coins, return total OOS PnL + per-coin."""
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
            n = len(trades)
            wr = (trades["pnl_net"] > 0).mean() * 100
        else:
            pnl, n, wr = 0.0, 0, 0.0

        total_pnl += pnl
        total_trades += n
        results[coin] = {"pnl": pnl, "trades": n, "wr": wr}

    return total_pnl, total_trades, results


def test_factor_addon(btc_df, base_score_ts, factor_name, score_fn, weights_to_test):
    """Test a score function as additive to v3 baseline at various weights."""
    results = {}

    for w in weights_to_test:
        addon = score_fn(btc_df, weight=w)
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


def stepwise_v4_build(btc_df, base_score_ts, baseline_pnl, candidate_factors):
    """
    Greedily add factors one at a time, keeping those that improve PnL.

    candidate_factors: list of (name, score_fn, best_weight, best_delta) tuples,
                       sorted by delta descending.
    Returns: final factor list, final PnL, build log.
    """
    print("\n  Stepwise build log:")
    print(f"  {'Step':<4} {'Factor':<22} {'Weight':>6} {'PnL':>10} {'Delta':>10} {'Verdict':>8}")
    print("  " + "-" * 68)

    current_score = base_score_ts.copy()
    current_pnl = baseline_pnl
    selected = []
    build_log = []

    for i, (name, score_fn, weight, _delta) in enumerate(candidate_factors, 1):
        addon = score_fn(btc_df, weight=weight)
        test_score_vals = current_score.values + addon.values
        test_score = pd.Series(test_score_vals, index=current_score.index)

        # Evaluate
        total_pnl = 0.0
        for coin in ALL_COINS:
            cfg = V11_CONFIGS[coin]
            symbol = f"{coin}USDT"
            alt_raw = fetch_binance_15m(symbol, years=3)
            alt_df = build_alt_technicals(alt_raw)
            alt_df = alt_df[(alt_df["ts"] >= OOS_START) & (alt_df["ts"] <= OOS_END)].copy()
            if alt_df.empty:
                continue

            signal, alt_merged = generate_signal_v11(
                test_score, alt_df, cfg["threshold"], cfg["alt_pa"]
            )
            trades = run_backtest(
                alt_merged, signal,
                sl_atr_mult=cfg["sl"], tp_atr_mult=cfg["tp"],
                trail_atr_mult=cfg["trail"], trail_activate_atr=cfg["trail_act"],
                max_hold_bars=96, cooldown_bars=cfg["cd"],
            )
            if not trades.empty:
                total_pnl += trades["pnl_net"].sum()

        delta = total_pnl - current_pnl
        keep = delta > 0
        verdict = "KEEP" if keep else "SKIP"

        print(f"  {i:<4} {name:<22} {weight:>6.1f} ${total_pnl:>9,.0f} {delta:>+9,.0f} {verdict:>8}")

        build_log.append({
            "step": i, "factor": name, "weight": weight,
            "pnl": total_pnl, "delta": delta, "kept": keep,
        })

        if keep:
            current_score = test_score
            current_pnl = total_pnl
            selected.append((name, weight))

    return selected, current_pnl, build_log


def run_v4_final(btc_df, base_score_ts, selected_factors):
    """Run final v4 model with all selected factors, return per-coin breakdown."""
    # Build enhanced score
    enhanced_score = base_score_ts.copy()
    factor_fns = {
        "dvol_level": score_dvol_level,
        "dvol_change": score_dvol_change,
        "stable_supply": score_stable_supply,
        "hashrate": score_hashrate,
        "active_addr": score_active_addr,
        "dex_ratio": score_dex_ratio,
        "cvd_contrarian": score_cvd_contrarian,
        "macro_risk_off": score_macro_risk_off,
    }

    for name, weight in selected_factors:
        fn = factor_fns[name]
        addon = fn(btc_df, weight=weight)
        enhanced_vals = enhanced_score.values + addon.values
        enhanced_score = pd.Series(enhanced_vals, index=enhanced_score.index)

    # Run per-coin
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
            pnl = trades["pnl_net"].sum()
            n = len(trades)
            wr = (trades["pnl_net"] > 0).mean() * 100
        else:
            pnl, n, wr = 0.0, 0, 0.0

        total_pnl += pnl
        total_trades += n
        results[coin] = {"pnl": pnl, "trades": n, "wr": wr}

    return total_pnl, total_trades, results


# =====================================================================
# MAIN
# =====================================================================
def main():
    print("=" * 70)
    print("v4 Factor Backtest: v3 + Free Historical Data Sources")
    print("OOS: Jan-Mar 2026 | 6 coins | 2x leverage")
    print("=" * 70)

    # ---- 1. Load BTC data and compute v3 baseline score ----
    print("\n[1] Loading BTC data + v3 factors...")
    btc_raw = fetch_binance_15m("BTCUSDT", years=3)
    db_data = load_btc_db_data_v3()
    btc_df = build_btc_features(btc_raw, db_data)
    btc_df = btc_df[btc_df["ts"] >= pd.Timestamp("2025-06-01")].copy().reset_index(drop=True)

    btc_score = compute_btc_composite_score(btc_df)
    btc_score_ts = btc_score.copy()
    btc_score_ts.index = btc_df["ts"]

    # ---- 2. Run v3 baseline ----
    print("\n[2] Running v3 baseline...")
    baseline_pnl, baseline_trades, baseline_results = run_v3_baseline(btc_df, btc_score_ts)
    print(f"\n  V3 BASELINE: ${baseline_pnl:,.0f} ({baseline_trades} trades)")
    for coin, r in baseline_results.items():
        print(f"    {coin}: ${r['pnl']:,.0f} ({r['trades']} trades, WR {r['wr']:.1f}%)")

    # ---- 3. Load new factor data ----
    print("\n[3] Loading new factor data (backfill from APIs)...")

    # DVOL (hourly)
    dvol_df = load_dvol_historical()
    if dvol_df is not None:
        dvol_cols = ["ts", "dvol", "dvol_ma24", "dvol_z", "dvol_chg24"]
        btc_df = pd.merge_asof(
            btc_df.sort_values("ts"),
            dvol_df[dvol_cols].sort_values("ts"),
            on="ts", direction="backward", tolerance=pd.Timedelta("2h")
        )
        print(f"    DVOL merged: {btc_df['dvol'].notna().sum():,} / {len(btc_df):,} bars have DVOL")
        dvol_vals = btc_df["dvol"].dropna()
        if len(dvol_vals) > 0:
            print(f"    DVOL range: {dvol_vals.min():.1f} - {dvol_vals.max():.1f} (mean {dvol_vals.mean():.1f})")

    # Stablecoin supply (daily)
    stable_df = load_stablecoin_supply()
    if stable_df is not None:
        stable_cols = ["ts", "stable_total", "stable_chg7d"]
        existing = [c for c in stable_cols if c in stable_df.columns]
        btc_df = pd.merge_asof(
            btc_df.sort_values("ts"),
            stable_df[existing].sort_values("ts"),
            on="ts", direction="backward", tolerance=pd.Timedelta("3d")
        )
        if "stable_total" in btc_df.columns:
            vals = btc_df["stable_total"].dropna()
            if len(vals) > 0:
                print(f"    Stablecoin supply: ${vals.iloc[-1]/1e9:.1f}B (latest)")

    # Hashrate (daily)
    hash_df = load_hashrate()
    if hash_df is not None:
        hash_cols = ["ts", "hashrate", "hashrate_chg7d", "hashrate_chg30d"]
        btc_df = pd.merge_asof(
            btc_df.sort_values("ts"),
            hash_df[hash_cols].sort_values("ts"),
            on="ts", direction="backward", tolerance=pd.Timedelta("3d")
        )
        if "hashrate" in btc_df.columns:
            vals = btc_df["hashrate"].dropna()
            if len(vals) > 0:
                print(f"    Hashrate: {vals.iloc[-1]:.0f} TH/s (latest)")

    # Active addresses (daily)
    addr_df = load_active_addresses()
    if addr_df is not None:
        addr_cols = ["ts", "active_addr", "active_addr_chg7d", "active_addr_z"]
        btc_df = pd.merge_asof(
            btc_df.sort_values("ts"),
            addr_df[addr_cols].sort_values("ts"),
            on="ts", direction="backward", tolerance=pd.Timedelta("3d")
        )
        if "active_addr" in btc_df.columns:
            vals = btc_df["active_addr"].dropna()
            if len(vals) > 0:
                print(f"    Active addresses: {vals.iloc[-1]:,.0f} (latest)")

    # DEX volume (daily)
    dex_df = load_dex_volume()
    if dex_df is not None:
        dex_cols = ["ts", "dex_vol_24h", "dex_vol_ma7d", "dex_vol_z"]
        btc_df = pd.merge_asof(
            btc_df.sort_values("ts"),
            dex_df[dex_cols].sort_values("ts"),
            on="ts", direction="backward", tolerance=pd.Timedelta("3d")
        )
        if "dex_vol_24h" in btc_df.columns:
            vals = btc_df["dex_vol_24h"].dropna()
            if len(vals) > 0:
                print(f"    DEX volume: ${vals.iloc[-1]/1e9:.1f}B (latest)")

    # Phase 1 winners: CVD + Macro
    print("\n  Loading Phase 1 factors (CVD, Macro)...")
    cvd_df = load_cvd_factor()
    if cvd_df is not None:
        btc_df = pd.merge_asof(
            btc_df.sort_values("ts"),
            cvd_df[["ts", "volume_delta", "cvd_z", "cvd_ma4", "cvd_ma16",
                     "cvd_ma96", "cvd_slope"]].sort_values("ts"),
            on="ts", direction="backward", tolerance=pd.Timedelta("30min")
        )

    macro_df = load_macro_factor()
    if macro_df is not None:
        macro_df = engineer_macro_features(macro_df)
        macro_cols = [c for c in macro_df.columns if c != "ts"]
        btc_df = pd.merge_asof(
            btc_df.sort_values("ts"),
            macro_df[["ts"] + macro_cols].sort_values("ts"),
            on="ts", direction="backward", tolerance=pd.Timedelta("3d")
        )

    # Recompute v3 score (same — just more columns now available for addon scoring)
    btc_score = compute_btc_composite_score(btc_df)
    btc_score_ts = btc_score.copy()
    btc_score_ts.index = btc_df["ts"]

    # ---- Phase A: Individual factor tests ----
    print("\n" + "=" * 70)
    print("[Phase A] Individual Factor Tests (additive to v3 baseline)")
    print("=" * 70)

    new_factor_tests = [
        ("dvol_level", score_dvol_level),
        ("dvol_change", score_dvol_change),
        ("stable_supply", score_stable_supply),
        ("hashrate", score_hashrate),
        ("active_addr", score_active_addr),
        ("dex_ratio", score_dex_ratio),
        # Phase 1 winners
        ("cvd_contrarian", score_cvd_contrarian),
        ("macro_risk_off", score_macro_risk_off),
    ]

    all_tests = []

    for name, fn in new_factor_tests:
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

    # Find best weight per factor
    factor_best = {}
    for t in all_tests:
        fname = t["factor"]
        if fname not in factor_best or t["delta"] > factor_best[fname]["delta"]:
            factor_best[fname] = t

    print(f"\n{'='*70}")
    print("Phase A Summary: Best Weight Per Factor")
    print(f"{'='*70}")
    print(f"\n{'Factor':<22} {'Best W':>6} {'PnL':>10} {'Delta':>10} {'Trades':>7}")
    print("-" * 60)

    for fname, best in sorted(factor_best.items(), key=lambda x: -x[1]["delta"]):
        print(f"{fname:<22} {best['weight']:>6.1f} ${best['pnl']:>9,.0f} {best['delta']:>+9,.0f} {best['trades']:>7}")

    # ---- Phase B: Stepwise v4 build ----
    print(f"\n{'='*70}")
    print("[Phase B] Stepwise v4 Build")
    print(f"{'='*70}")
    print(f"  Starting from v3 baseline: ${baseline_pnl:,.0f}")

    # Collect all positive factors sorted by delta
    factor_fn_map = {name: fn for name, fn in new_factor_tests}
    candidates = []
    for fname, best in factor_best.items():
        if best["delta"] > 0:
            candidates.append((fname, factor_fn_map[fname], best["weight"], best["delta"]))

    candidates.sort(key=lambda x: -x[3])  # sort by delta descending

    if candidates:
        print(f"\n  Positive candidates ({len(candidates)}):")
        for name, _, w, d in candidates:
            print(f"    {name} (w={w:.1f}, delta=+${d:,.0f})")

        selected, v4_pnl, build_log = stepwise_v4_build(
            btc_df, btc_score_ts, baseline_pnl, candidates
        )
    else:
        print("\n  No positive factors found! v4 = v3.")
        selected = []
        v4_pnl = baseline_pnl
        build_log = []

    # ---- Phase C: Comparison Report ----
    print(f"\n{'='*70}")
    print("[Phase C] v3 vs v4 Comparison Report")
    print(f"{'='*70}")

    if selected:
        print(f"\n  v4 factors added to v3:")
        for name, weight in selected:
            print(f"    + {name} (w={weight:.1f})")

        # Run final v4 with per-coin breakdown
        v4_total_pnl, v4_total_trades, v4_results = run_v4_final(
            btc_df, btc_score_ts, selected
        )
    else:
        v4_total_pnl = baseline_pnl
        v4_total_trades = baseline_trades
        v4_results = baseline_results

    delta = v4_total_pnl - baseline_pnl
    delta_pct = delta / abs(baseline_pnl) * 100 if baseline_pnl != 0 else 0

    print(f"\n  {'Metric':<14} {'v3 Baseline':>12} {'v4 Enhanced':>12} {'Delta':>12}")
    print("  " + "-" * 54)
    print(f"  {'Total PnL':<14} ${baseline_pnl:>10,.0f} ${v4_total_pnl:>10,.0f} {delta:>+10,.0f}")
    print(f"  {'Trades':<14} {baseline_trades:>11} {v4_total_trades:>11} {v4_total_trades - baseline_trades:>+11}")

    print(f"\n  Per-Coin Comparison:")
    print(f"  {'Coin':<6} {'v3 PnL':>10} {'v4 PnL':>10} {'Delta':>10} {'v3 WR':>7} {'v4 WR':>7}")
    print("  " + "-" * 56)

    for coin in ALL_COINS:
        v3r = baseline_results.get(coin, {"pnl": 0, "wr": 0})
        v4r = v4_results.get(coin, {"pnl": 0, "wr": 0})
        d = v4r["pnl"] - v3r["pnl"]
        print(f"  {coin:<6} ${v3r['pnl']:>9,.0f} ${v4r['pnl']:>9,.0f} {d:>+9,.0f} "
              f"{v3r['wr']:>6.1f}% {v4r['wr']:>6.1f}%")

    # ---- Verdict ----
    print(f"\n{'='*70}")
    print("VERDICT")
    print(f"{'='*70}")

    if delta > 0:
        print(f"\n  >>> v4 BEATS v3 by ${delta:,.0f} ({delta_pct:+.1f}%)")
        print(f"  >>> Selected factors: {', '.join(f'{n}(w={w})' for n, w in selected)}")
        print(f"  >>> Consider integrating into production")
    else:
        print(f"\n  >>> v4 does NOT beat v3. Free data sources don't add meaningful alpha.")
        print(f"  >>> Stick with v3 model.")

    # ---- Save results ----
    os.makedirs("experiments", exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = {
        "timestamp": ts,
        "oos_period": f"{OOS_START} to {OOS_END}",
        "v3_baseline": {
            "pnl": baseline_pnl, "trades": baseline_trades,
            "per_coin": baseline_results,
        },
        "phase_a_individual_tests": all_tests,
        "phase_a_best_per_factor": {k: v for k, v in factor_best.items()},
        "phase_b_stepwise_log": build_log,
        "phase_b_selected_factors": [{"name": n, "weight": w} for n, w in selected],
        "v4_final": {
            "pnl": v4_total_pnl, "trades": v4_total_trades,
            "per_coin": v4_results,
            "delta_vs_v3": delta, "delta_pct": delta_pct,
        },
    }

    outfile = f"experiments/v4_factor_test_{ts}.json"
    with open(outfile, "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"\nResults saved to {outfile}")


if __name__ == "__main__":
    main()
