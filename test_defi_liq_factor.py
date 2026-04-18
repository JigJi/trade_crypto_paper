"""
Test DeFi Liquidation (Aave V3) as a new BTC composite factor.
==============================================================
Hypothesis: DeFi liquidations are "forced execution" just like CEX liquidations.
They may be a leading indicator before CEX cascade effects propagate.

Data: Aave V3 Ethereum via public RPC eth_getLogs (no API key needed)
"""

import os, json, warnings, time
from datetime import datetime

import numpy as np
import pandas as pd
import requests
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

# ================================================================
# Constants
# ================================================================

SHORT_OFFSET = 0.5
TEST_START = pd.Timestamp("2026-01-01")

# Public Ethereum RPCs (no key needed, fallback chain)
ETH_RPCS = [
    "https://ethereum-rpc.publicnode.com",
    "https://rpc.ankr.com/eth",
    "https://eth.llamarpc.com",
]

# Aave V3 Pool contract on Ethereum mainnet
AAVE_V3_POOL = "0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2"

# keccak256("LiquidationCall(address,address,address,uint256,uint256,address,bool)")
LIQUIDATION_CALL_TOPIC = "0xe413a321e8681d831f4dbccbca790d2952b56f977908e45be37335533e005286"

CACHE_FILE = "data_cache/aave_v3_liquidations.parquet"

# Post-merge constants for block↔timestamp conversion (exact 12s blocks)
MERGE_BLOCK = 15_537_394
MERGE_TIMESTAMP = 1663224179  # Sep 15, 2022 06:42:59 UTC

# Token address → (symbol, decimals)
TOKEN_MAP = {
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": ("WETH", 18),
    "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599": ("WBTC", 8),
    "0x7f39c581f595b53c5cb19bd0b3f8da6c935e2ca0": ("wstETH", 18),
    "0xae78736cd615f374d3085123a210448e74fc6393": ("rETH", 18),
    "0xbe9895146f7af43049ca1c1ae358b0541ea49704": ("cbETH", 18),
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": ("USDC", 6),
    "0xdac17f958d2ee523a2206206994597c13d831ec7": ("USDT", 6),
    "0x6b175474e89094c44da98b954eedeac495271d0f": ("DAI", 18),
    "0x514910771af9ca656af840dff83e8264ecf986ca": ("LINK", 18),
    "0x7fc66500c84a76ad7e9c93437bfc5ac33e2ddae9": ("AAVE", 18),
    "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984": ("UNI", 18),
    "0xd533a949740bb3306d119cc777fa900ba034cd52": ("CRV", 18),
    "0xc011a73ee8576fb46f5e1c5751ca3b9fe0af2a6f": ("SNX", 18),
    "0x5f98805a4e8be255a32880fdec7f6728c6568ba0": ("LUSD", 18),
    "0x83f20f44975d03b1b09e64809b757c47f942beea": ("sDAI", 18),
    "0x9f8f72aa9304c8b593d555f12ef6589cc3a579a2": ("MKR", 18),
    "0xba100000625a3754423978a60c9317c58a424e3d": ("BAL", 18),
}

# Crypto collateral symbols that indicate a directional position
CRYPTO_COLLATERAL = {"WETH", "WBTC", "stETH", "wstETH", "cbETH", "rETH",
                     "AAVE", "LINK", "UNI", "CRV", "SNX", "MKR", "BAL"}

BLOCKS_PER_CHUNK = 50_000  # ~7 days at 12s/block


# ================================================================
# [B] Fetch Aave V3 Liquidations via public RPC
# ================================================================

def _rpc_call(method, params):
    """Call Ethereum JSON-RPC with fallback across multiple RPCs."""
    payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
    for rpc_url in ETH_RPCS:
        for attempt in range(2):
            try:
                resp = requests.post(rpc_url, json=payload, timeout=30)
                resp.raise_for_status()
                data = resp.json()
                if "error" in data:
                    err_msg = data["error"].get("message", "")
                    # Block range too large — caller should reduce chunk size
                    if "exceed" in err_msg.lower() or "too large" in err_msg.lower() or "range" in err_msg.lower():
                        return {"error": "range_too_large"}
                    continue
                return data
            except Exception:
                time.sleep(0.5)
    return {"error": "all_rpcs_failed"}


def _timestamp_to_block(ts_unix):
    """Convert Unix timestamp to approximate block number (post-merge)."""
    return max(MERGE_BLOCK, MERGE_BLOCK + (ts_unix - MERGE_TIMESTAMP) // 12)


def _block_to_timestamp(block_num):
    """Convert block number to approximate Unix timestamp (post-merge)."""
    return MERGE_TIMESTAMP + (block_num - MERGE_BLOCK) * 12


def _get_latest_block():
    """Get latest Ethereum block number."""
    data = _rpc_call("eth_blockNumber", [])
    if "result" in data:
        return int(data["result"], 16)
    return _timestamp_to_block(int(pd.Timestamp.utcnow().timestamp()))


def _parse_event(log, price_cache):
    """Parse a single LiquidationCall event log into a dict."""
    topics = log.get("topics", [])
    if len(topics) < 4:
        return None

    coll_addr = "0x" + topics[1][-40:]  # last 20 bytes

    # Data: debtToCover(32) + liquidatedCollateralAmount(32) + liquidator(32) + receiveAToken(32)
    data_hex = log.get("data", "0x")[2:]  # strip 0x
    if len(data_hex) < 128:
        return None

    coll_amount_raw = int(data_hex[64:128], 16)  # liquidatedCollateralAmount

    # Look up token info
    coll_info = TOKEN_MAP.get(coll_addr.lower(), ("UNKNOWN", 18))
    coll_symbol, coll_decimals = coll_info
    coll_amount = coll_amount_raw / (10 ** coll_decimals)

    # Timestamp from block number (post-merge: exact 12s)
    block_num = int(log.get("blockNumber", "0x0"), 16)
    ts_unix = _block_to_timestamp(block_num)
    timestamp = pd.Timestamp(ts_unix, unit="s")

    # Estimate USD
    coll_usd = _estimate_usd(coll_symbol, coll_amount, timestamp, price_cache)

    return {
        "timestamp": timestamp,
        "collateral_symbol": coll_symbol,
        "collateral_usd": coll_usd,
        "collateral_amount": coll_amount,
        "is_crypto_collateral": coll_symbol in CRYPTO_COLLATERAL,
    }


def _estimate_usd(symbol, amount, timestamp, price_cache):
    """Estimate USD value using Binance price cache."""
    if symbol in ("USDC", "USDT", "DAI", "LUSD", "sDAI"):
        return amount
    if symbol in ("WETH", "wstETH", "cbETH", "rETH", "stETH"):
        if price_cache is not None and "ETH" in price_cache:
            price = price_cache["ETH"].asof(timestamp)
            if not pd.isna(price) and price > 0:
                return amount * price
        return amount * 3000  # fallback
    if symbol == "WBTC":
        if price_cache is not None and "BTC" in price_cache:
            price = price_cache["BTC"].asof(timestamp)
            if not pd.isna(price) and price > 0:
                return amount * price
        return amount * 80000  # fallback
    # Other tokens: rough estimate or skip
    return 0


def _build_price_cache():
    """Build ETH + BTC price Series indexed by timestamp for USD estimation."""
    cache = {}
    for sym in ["ETH", "BTC"]:
        try:
            ohlcv = fetch_binance_15m(f"{sym}USDT", years=3)
            s = ohlcv.set_index("date_time")["close"].sort_index()
            # Resample to 1h to keep it lightweight
            s = s.resample("1h").last().ffill()
            cache[sym] = s
        except Exception as e:
            print(f"  Warning: could not load {sym} price cache: {e}")
    return cache


def fetch_aave_v3_liquidations(start_date="2024-01-01", force_refresh=False):
    """
    Fetch LiquidationCall events from Aave V3 Pool via public Ethereum RPC.
    No API key needed. Uses eth_getLogs with block range chunking.
    Caches result as parquet.
    """
    if not force_refresh and os.path.exists(CACHE_FILE):
        print(f"  Using cached {CACHE_FILE}")
        df = pd.read_parquet(CACHE_FILE)
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        print(f"    {len(df):,} events, {df['timestamp'].min()} to {df['timestamp'].max()}")
        return df

    print(f"  Building price cache for USD estimation...")
    price_cache = _build_price_cache()

    start_ts = int(pd.Timestamp(start_date).timestamp())
    start_block = _timestamp_to_block(start_ts)
    end_block = _get_latest_block()
    total_blocks = end_block - start_block
    total_chunks = total_blocks // BLOCKS_PER_CHUNK + 1

    print(f"  Block range: {start_block:,} to {end_block:,} ({total_blocks:,} blocks, ~{total_chunks} chunks)")

    all_events = []
    current_block = start_block
    chunk_num = 0
    chunk_size = BLOCKS_PER_CHUNK

    while current_block < end_block:
        chunk_end = min(current_block + chunk_size - 1, end_block)
        chunk_num += 1

        data = _rpc_call("eth_getLogs", [{
            "address": AAVE_V3_POOL,
            "topics": [LIQUIDATION_CALL_TOPIC],
            "fromBlock": hex(current_block),
            "toBlock": hex(chunk_end),
        }])

        if isinstance(data, dict) and data.get("error") == "range_too_large":
            # Reduce chunk size and retry
            chunk_size = max(chunk_size // 2, 2_000)
            print(f"    Range too large, reducing to {chunk_size:,} blocks/chunk")
            continue

        if isinstance(data, dict) and data.get("error") == "all_rpcs_failed":
            print(f"    All RPCs failed at block {current_block:,}, skipping chunk")
            current_block = chunk_end + 1
            time.sleep(2)
            continue

        logs = data.get("result", [])
        if isinstance(logs, list):
            for log in logs:
                ev = _parse_event(log, price_cache)
                if ev:
                    all_events.append(ev)

        if chunk_num % 10 == 0:
            pct = (current_block - start_block) / total_blocks * 100
            print(f"    Chunk {chunk_num}: {pct:.0f}% done, {len(all_events):,} events")

        current_block = chunk_end + 1
        time.sleep(0.3)  # be nice to public RPCs

    if not all_events:
        print("  No liquidation events found!")
        return None

    df = pd.DataFrame(all_events)
    df = df.sort_values("timestamp").reset_index(drop=True)

    # Save cache
    os.makedirs("data_cache", exist_ok=True)
    df.to_parquet(CACHE_FILE, index=False)
    print(f"  Cached {len(df):,} events to {CACHE_FILE}")
    print(f"    Range: {df['timestamp'].min()} to {df['timestamp'].max()}")
    return df


# ================================================================
# [C] Build DeFi Liquidation Features
# ================================================================

def build_defi_liq_features(raw_df, shift_hours=1):
    """
    Build 15m features from raw Aave V3 liquidation events.
    Anti-lookahead: shift +1h (block confirmation + indexing delay).
    """
    df = raw_df.copy()

    # Anti-lookahead shift: events are available shift_hours after on-chain
    df["ts"] = df["timestamp"] + pd.Timedelta(hours=shift_hours)

    # Filter to crypto collateral only (bearish signal)
    crypto_df = df[df["is_crypto_collateral"]].copy()

    # Resample all events to 15min bins
    df_all = df.set_index("ts").resample("15min").agg(
        defi_liq_total_usd=("collateral_usd", "sum"),
        defi_liq_count=("collateral_usd", "count"),
    ).fillna(0).reset_index()

    # Resample crypto collateral only
    if len(crypto_df) > 0:
        df_crypto = crypto_df.set_index("ts").resample("15min").agg(
            defi_liq_crypto_usd=("collateral_usd", "sum"),
            defi_liq_crypto_count=("collateral_usd", "count"),
        ).fillna(0).reset_index()
    else:
        df_crypto = pd.DataFrame({"ts": df_all["ts"],
                                  "defi_liq_crypto_usd": 0.0,
                                  "defi_liq_crypto_count": 0})

    # Merge
    feat = pd.merge(df_all, df_crypto, on="ts", how="left").fillna(0)

    # Rolling features (backward-looking)
    feat["defi_liq_usd_ma"] = feat["defi_liq_total_usd"].rolling(16, min_periods=1).mean()       # 4h MA
    feat["defi_liq_usd_ma_slow"] = feat["defi_liq_total_usd"].rolling(96, min_periods=1).mean()   # 24h MA
    feat["defi_liq_crypto_ma"] = feat["defi_liq_crypto_usd"].rolling(16, min_periods=1).mean()     # 4h MA

    # Z-score (24h window)
    roll_mean = feat["defi_liq_total_usd"].rolling(96, min_periods=16).mean()
    roll_std = feat["defi_liq_total_usd"].rolling(96, min_periods=16).std()
    feat["defi_liq_z"] = ((feat["defi_liq_total_usd"] - roll_mean) / roll_std.clip(lower=1e-8)).fillna(0)

    # Spike detection: total > slow MA * 3
    feat["defi_liq_spike"] = (feat["defi_liq_total_usd"] > feat["defi_liq_usd_ma_slow"] * 3).astype(int)

    # Sustained spike: count of spikes in last 4h
    feat["defi_liq_spike_4h"] = feat["defi_liq_spike"].rolling(16, min_periods=1).sum()

    # Crypto ratio (what fraction of liquidations are crypto collateral)
    total_safe = feat["defi_liq_total_usd"].clip(lower=1)
    feat["defi_liq_crypto_ratio"] = feat["defi_liq_crypto_usd"] / total_safe

    feat = feat.dropna().reset_index(drop=True)
    print(f"  Built {len(feat):,} 15m feature bars")
    print(f"    Range: {feat['ts'].min()} to {feat['ts'].max()}")
    print(f"    Spikes: {feat['defi_liq_spike'].sum():,.0f} bars ({feat['defi_liq_spike'].mean()*100:.1f}%)")
    return feat


# ================================================================
# [D] Score Functions (3 variants)
# ================================================================

def score_defi_liq_contrarian(btc_df, weight=1.5):
    """
    Contrarian: DeFi liq spike = capitulation = bullish (same logic as CEX liq).
    Crypto collateral being liquidated = longs are being washed out = buy signal.
    """
    s = pd.Series(0.0, index=btc_df.index)
    if "defi_liq_z" not in btc_df.columns:
        return s

    z = btc_df["defi_liq_z"].fillna(0)
    spike = btc_df["defi_liq_spike"].fillna(0)
    crypto_ratio = btc_df["defi_liq_crypto_ratio"].fillna(0)

    # Single spike with crypto = capitulation = bullish
    s += np.where((spike > 0) & (crypto_ratio > 0.3), weight, 0)
    # Z-score extreme = strong capitulation
    s += np.where(z > 2.5, weight * 0.5, 0)
    # Quiet period (very low z) with crypto = accumulation zone = mildly bullish
    s += np.where((z < -1.0) & (crypto_ratio > 0.5), weight * 0.3, 0)
    return s


def score_defi_liq_momentum(btc_df, weight=1.5):
    """
    Momentum: DeFi liq spike = cascade ongoing = bearish.
    More liquidations coming = more forced selling ahead.
    """
    s = pd.Series(0.0, index=btc_df.index)
    if "defi_liq_z" not in btc_df.columns:
        return s

    z = btc_df["defi_liq_z"].fillna(0)
    spike = btc_df["defi_liq_spike"].fillna(0)
    crypto_ratio = btc_df["defi_liq_crypto_ratio"].fillna(0)

    # Spike with crypto collateral = cascade = bearish
    s += np.where((spike > 0) & (crypto_ratio > 0.3), -weight, 0)
    # Z-score extreme = heavy cascade
    s += np.where(z > 2.5, -weight * 0.5, 0)
    # Quiet period = no stress = mildly bullish
    s += np.where(z < -1.0, weight * 0.3, 0)
    return s


def score_defi_liq_combined(btc_df, weight=1.5):
    """
    Combined: single spike = contrarian (capitulation), sustained 3+ = momentum (cascade).
    Best of both worlds.
    """
    s = pd.Series(0.0, index=btc_df.index)
    if "defi_liq_z" not in btc_df.columns:
        return s

    z = btc_df["defi_liq_z"].fillna(0)
    spike = btc_df["defi_liq_spike"].fillna(0)
    spike_4h = btc_df["defi_liq_spike_4h"].fillna(0)
    crypto_ratio = btc_df["defi_liq_crypto_ratio"].fillna(0)

    # Single spike = capitulation = bullish
    single_spike = (spike > 0) & (spike_4h <= 2) & (crypto_ratio > 0.3)
    s += np.where(single_spike, weight, 0)

    # Sustained spikes (3+) = cascade = bearish
    sustained = (spike_4h >= 3) & (crypto_ratio > 0.3)
    s += np.where(sustained, -weight, 0)

    # Extreme z-score + sustained = strong bearish
    s += np.where((z > 3.0) & (spike_4h >= 3), -weight * 0.5, 0)

    # Quiet zone = accumulation
    s += np.where(z < -1.0, weight * 0.3, 0)
    return s


# ================================================================
# [E] Data Quality Analysis
# ================================================================

def analyze_defi_liq(defi_df, btc_df):
    """Analyze DeFi liquidation data quality and relevance."""
    print(f"\n{'='*65}")
    print("  DeFi LIQUIDATION DATA QUALITY ANALYSIS")
    print(f"{'='*65}")

    raw = defi_df
    print(f"\n  Raw events: {len(raw):,}")
    print(f"  Date range: {raw['timestamp'].min()} to {raw['timestamp'].max()}")
    days = (raw['timestamp'].max() - raw['timestamp'].min()).days
    print(f"  Coverage: {days} days")
    print(f"  Events/day avg: {len(raw)/max(days,1):.1f}")

    # Collateral breakdown
    print(f"\n  Top collateral symbols:")
    top = raw.groupby("collateral_symbol").agg(
        count=("collateral_usd", "count"),
        total_usd=("collateral_usd", "sum"),
    ).sort_values("total_usd", ascending=False).head(10)
    for sym, row in top.iterrows():
        is_crypto = "crypto" if sym in CRYPTO_COLLATERAL else "stable"
        print(f"    {sym:10s} {row['count']:6,.0f} events  ${row['total_usd']:>12,.0f}  [{is_crypto}]")

    crypto_pct = raw[raw["is_crypto_collateral"]]["collateral_usd"].sum() / max(raw["collateral_usd"].sum(), 1) * 100
    print(f"\n  Crypto collateral: {crypto_pct:.1f}% of total USD")

    # Distribution
    daily = raw.set_index("timestamp").resample("1D")["collateral_usd"].sum()
    print(f"\n  Daily USD distribution:")
    print(f"    Mean:   ${daily.mean():>12,.0f}")
    print(f"    Median: ${daily.median():>12,.0f}")
    print(f"    Max:    ${daily.max():>12,.0f}")
    print(f"    Std:    ${daily.std():>12,.0f}")

    # Correlation with BTC forward returns
    print(f"\n  Correlation with BTC forward returns:")
    if btc_df is not None and len(btc_df) > 100:
        btc = btc_df[["ts", "close"]].copy()
        btc["ret_1h"] = btc["close"].pct_change(4).shift(-4)    # 1h forward
        btc["ret_4h"] = btc["close"].pct_change(16).shift(-16)  # 4h forward
        btc["ret_24h"] = btc["close"].pct_change(96).shift(-96) # 24h forward

        # Merge defi features
        feat = build_defi_liq_features(raw, shift_hours=0)  # no shift for correlation analysis
        merged = pd.merge_asof(
            btc.sort_values("ts"),
            feat[["ts", "defi_liq_total_usd", "defi_liq_z", "defi_liq_crypto_usd"]].sort_values("ts"),
            on="ts", direction="backward", tolerance=pd.Timedelta("2h"))

        for col in ["defi_liq_total_usd", "defi_liq_z", "defi_liq_crypto_usd"]:
            if col in merged.columns:
                for ret_col in ["ret_1h", "ret_4h", "ret_24h"]:
                    corr = merged[col].corr(merged[ret_col])
                    print(f"    {col:25s} vs {ret_col:8s}: {corr:+.4f}")

    print(f"{'='*65}")


# ================================================================
# [F] Run Scenario (from test_orderbook_factor.py pattern)
# ================================================================

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


# ================================================================
# [G] Main
# ================================================================

def main():
    print("=" * 65)
    print(" DeFi LIQUIDATION (Aave V3) FACTOR TEST")
    print(" Hypothesis: DeFi liqs lead CEX cascades")
    print(" Data source: Public RPC eth_getLogs (no key needed)")
    print("=" * 65)

    # ----- Step 1: Fetch Aave V3 data -----
    print("\n[1] Fetching Aave V3 liquidation data...")
    raw_df = fetch_aave_v3_liquidations(start_date="2024-01-01")
    if raw_df is None:
        print("  ABORTED: No data available.")
        return

    # ----- Step 2: Build features + analyze quality -----
    print("\n[2] Building features...")
    defi_feat = build_defi_liq_features(raw_df, shift_hours=1)

    # ----- Step 3: Load BTC baseline -----
    print("\n[3] Loading BTC OHLCV + DB factors...")
    btc_ohlcv = fetch_binance_15m("BTCUSDT", years=3)
    btc_db = load_btc_db_data()
    btc_df = build_btc_features(btc_ohlcv, btc_db)
    btc_score_base = compute_btc_composite_score(btc_df)

    # ----- Step 2b: Analyze data quality (needs btc_df) -----
    analyze_defi_liq(raw_df, btc_df)

    # OOS filter
    mask = btc_df["ts"] >= pd.Timestamp("2025-06-01")
    btc_df_oos = btc_df[mask].reset_index(drop=True)
    btc_score_oos = btc_score_base[mask].reset_index(drop=True)

    # ----- Step 4: Merge DeFi features into BTC -----
    print("\n[4] Merging DeFi features into BTC data...")
    defi_merge_cols = [c for c in defi_feat.columns if c != "ts"]
    btc_merged = pd.merge_asof(
        btc_df_oos.sort_values("ts"),
        defi_feat[["ts"] + defi_merge_cols].sort_values("ts"),
        on="ts", direction="backward", tolerance=pd.Timedelta("2h"))

    n_filled = btc_merged["defi_liq_total_usd"].notna().sum()
    coverage = n_filled / len(btc_merged) * 100
    print(f"  Coverage: {n_filled:,}/{len(btc_merged):,} bars ({coverage:.1f}%)")

    if coverage < 10:
        print(f"  WARNING: Very low coverage ({coverage:.1f}%)! Results may be unreliable.")

    # ----- Step 5: Load altcoin data -----
    TEST_END = btc_df_oos["ts"].iloc[-1]
    print(f"\n[5] Loading altcoin data (OOS: {TEST_START.date()} to {TEST_END.date()})...")
    alt_data = {}
    for coin in ALL_COINS:
        ohlcv = fetch_binance_15m(f"{coin}USDT", years=3)
        alt_df = build_alt_technicals(ohlcv)
        alt_test = alt_df[(alt_df["ts"] >= TEST_START) & (alt_df["ts"] <= TEST_END)].reset_index(drop=True)
        if len(alt_test) >= 100:
            alt_data[coin] = alt_test
            print(f"  {coin}: {len(alt_test):,} bars")

    # ----- Step 6: Run baseline -----
    print(f"\n{'='*65}")
    print("[6] BACKTEST COMPARISON")
    print(f"{'='*65}")

    score_ts_base = pd.Series(btc_score_oos.values, index=btc_merged["ts"].values)
    res_base = run_scenario("BASELINE: v3 composite (8 factors)", score_ts_base, alt_data)

    # ----- Step 7: Test 3 variants x 4 weights = 12 experiments -----
    defi_tests = [
        ("defi_liq_contrarian", score_defi_liq_contrarian),
        ("defi_liq_momentum", score_defi_liq_momentum),
        ("defi_liq_combined", score_defi_liq_combined),
    ]

    best_overall = {"label": "BASELINE", "pnl": res_base["total_pnl"]}
    all_results = {"BASELINE": res_base}

    for name, score_fn in defi_tests:
        for w in [0.5, 1.0, 1.5, 2.0]:
            score_test = btc_score_oos + score_fn(btc_merged, weight=w)
            score_ts = pd.Series(score_test.values, index=btc_merged["ts"].values)
            label = f"{name}(w={w})"
            res = run_scenario(f"+ {label}", score_ts, alt_data)
            all_results[label] = res
            if res["total_pnl"] > best_overall["pnl"]:
                best_overall = {"label": label, "pnl": res["total_pnl"]}

    # ----- Step 8: Summary -----
    print(f"\n{'='*65}")
    print("[7] SUMMARY")
    print(f"{'='*65}")
    print(f"  {'Scenario':<35s} {'PnL':>11s} {'Delta':>11s} {'#Tr':>5s} {'WR%':>6s}")
    print(f"  {'-'*35} {'-'*11} {'-'*11} {'-'*5} {'-'*6}")

    base_pnl = res_base["total_pnl"]
    print(f"  {'BASELINE (v3 8F)':<35s} ${base_pnl:>+9,.2f} {'':>11s} "
          f"{res_base['n_trades']:>5d} {res_base['wr']:>5.1f}%")

    for key in sorted(all_results.keys()):
        if key == "BASELINE":
            continue
        r = all_results[key]
        d = r["total_pnl"] - base_pnl
        marker = " <-- BEST" if key == best_overall.get("label") else ""
        print(f"  {key:<35s} ${r['total_pnl']:>+9,.2f} ${d:>+9,.2f} "
              f"{r['n_trades']:>5d} {r['wr']:>5.1f}%{marker}")

    # Per-coin breakdown if best > baseline
    if best_overall["label"] != "BASELINE":
        best_res = all_results[best_overall["label"]]
        delta = best_res["total_pnl"] - base_pnl
        pct = delta / abs(base_pnl) * 100

        print(f"\n{'='*65}")
        print(f"[8] BEST: {best_overall['label']} (+${delta:,.0f}, {pct:+.1f}%)")
        print(f"{'='*65}")
        print(f"  {'Coin':5s} {'PnL_Base':>10s} {'PnL_Best':>10s} {'Delta':>10s}")
        print(f"  {'-'*5} {'-'*10} {'-'*10} {'-'*10}")
        for coin in ALL_COINS:
            if coin not in res_base["coins"]:
                continue
            pA = res_base["coins"][coin]["net_pnl"]
            pB = best_res["coins"][coin]["net_pnl"]
            d = pB - pA
            m = " +" if d > 0 else " x" if d < -50 else ""
            print(f"  {coin:5s} ${pA:>+9,.2f} ${pB:>+9,.2f} ${d:>+9,.2f}{m}")
        print(f"  {'-'*5} {'-'*10} {'-'*10} {'-'*10}")
        print(f"  TOTAL ${base_pnl:>+9,.2f} ${best_res['total_pnl']:>+9,.2f} ${delta:>+9,.2f}")

        # Long/Short breakdown
        print(f"\n  Long/Short:")
        for lbl, res in [("Base", res_base), ("Best", best_res)]:
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
    if best_overall["label"] != "BASELINE":
        d = best_overall["pnl"] - base_pnl
        if d > 500:
            verdict = "DeFi Liquidation HELPS SIGNIFICANTLY"
            action = "Add to production + start collecting"
        elif d > 200:
            verdict = "DeFi Liquidation shows PROMISE"
            action = "Start collecting data, test more"
        else:
            verdict = "DeFi Liquidation marginal improvement"
            action = "Not worth collecting yet"
        print(f"  VERDICT: {verdict} (best={best_overall['label']}, +${d:,.0f})")
        print(f"  ACTION:  {action}")
    else:
        print(f"  VERDICT: DeFi Liquidation does NOT improve over v3 baseline")
        print(f"  ACTION:  Skip -- not worth collecting")
    print(f"{'='*65}")

    # ----- Step 9: Save to experiments/registry.json -----
    exp_id = f"defi_liq_factor_{datetime.now().strftime('%Y%m%d_%H%M')}"
    registry_path = "experiments/registry.json"
    os.makedirs("experiments", exist_ok=True)

    if os.path.exists(registry_path):
        with open(registry_path, "r") as f:
            registry = json.load(f)
    else:
        registry = []

    summary = {
        "baseline_pnl": round(base_pnl, 2),
        "data_coverage_pct": round(coverage, 1),
        "data_events": len(raw_df),
        "data_range": f"{raw_df['timestamp'].min().date()} to {raw_df['timestamp'].max().date()}",
    }
    for key, r in all_results.items():
        if key == "BASELINE":
            continue
        summary[key] = {
            "pnl": round(r["total_pnl"], 2),
            "delta": round(r["total_pnl"] - base_pnl, 2),
            "trades": r["n_trades"],
            "wr": round(r["wr"], 1),
        }

    registry.append({
        "experiment_id": exp_id,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "description": "Test Aave V3 DeFi liquidation as BTC composite factor (3 variants x 4 weights)",
        "params": {
            "test_period": f"{TEST_START.date()} to {TEST_END.date()}",
            "shift_hours": 1,
            "variants": ["contrarian", "momentum", "combined"],
            "weights": [0.5, 1.0, 1.5, 2.0],
            "data_source": "public_rpc_eth_getLogs",
        },
        "results": summary,
        "best": best_overall["label"],
    })
    with open(registry_path, "w") as f:
        json.dump(registry, f, indent=2)
    print(f"\n  Saved as '{exp_id}' to {registry_path}")


if __name__ == "__main__":
    main()
