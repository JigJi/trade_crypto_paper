"""
Fetch additional data sources for enhanced composite strategy:
  1. Binance Historical Funding Rate (3+ years)
  2. Deribit BTC Options (DVOL IV + put/call ratio)
  3. DXY Dollar Index (Yahoo Finance)
"""

import os, time, json
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import requests

os.makedirs("data_cache", exist_ok=True)


# ============================================================
# 1. BINANCE HISTORICAL FUNDING RATE
# ============================================================

def fetch_binance_funding_rate():
    """Fetch all historical funding rate for BTCUSDT from Binance Futures API."""
    cache_file = "data_cache/binance_funding_rate_hist.parquet"
    if os.path.exists(cache_file):
        df = pd.read_parquet(cache_file)
        print(f"  [Funding] Cached: {len(df):,} rows ({df['ts'].iloc[0]} to {df['ts'].iloc[-1]})")
        return df

    print("  [Funding] Fetching from Binance API...")
    url = "https://fapi.binance.com/fapi/v1/fundingRate"
    all_data = []

    # Start from 2020-01-01
    start_ts = int(datetime(2020, 1, 1).timestamp() * 1000)
    end_ts = int(datetime.now().timestamp() * 1000)

    while start_ts < end_ts:
        params = {"symbol": "BTCUSDT", "startTime": start_ts, "limit": 1000}
        for attempt in range(5):
            try:
                r = requests.get(url, params=params, timeout=15)
                if r.status_code == 429:
                    time.sleep(5)
                    continue
                r.raise_for_status()
                data = r.json()
                break
            except Exception as e:
                if attempt < 4:
                    time.sleep(2)
                else:
                    print(f"    Failed: {e}")
                    data = []

        if not data:
            break

        for d in data:
            all_data.append({
                "ts": pd.Timestamp(d["fundingTime"], unit="ms"),
                "funding_rate": float(d["fundingRate"]),
                "mark_price": float(d.get("markPrice", 0) or 0),
            })

        start_ts = data[-1]["fundingTime"] + 1
        if len(data) < 1000:
            break
        time.sleep(0.3)

    df = pd.DataFrame(all_data).drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    df.to_parquet(cache_file, index=False)
    print(f"    Saved {len(df):,} rows ({df['ts'].iloc[0]} to {df['ts'].iloc[-1]})")
    return df


# ============================================================
# 2. DERIBIT BTC OPTIONS (DVOL + Put/Call)
# ============================================================

def fetch_deribit_dvol():
    """Fetch BTC DVOL (implied volatility index) from Deribit public API."""
    cache_file = "data_cache/deribit_dvol.parquet"
    if os.path.exists(cache_file):
        df = pd.read_parquet(cache_file)
        print(f"  [DVOL] Cached: {len(df):,} rows ({df['ts'].iloc[0]} to {df['ts'].iloc[-1]})")
        return df

    print("  [DVOL] Fetching from Deribit API...")
    url = "https://www.deribit.com/api/v2/public/get_volatility_index_data"
    all_data = []

    # Fetch in 30-day chunks (max resolution=3600 = 1h)
    end_ts = int(datetime.now().timestamp() * 1000)
    start_ts = int(datetime(2021, 1, 1).timestamp() * 1000)
    chunk = 30 * 24 * 3600 * 1000  # 30 days in ms

    current = start_ts
    while current < end_ts:
        chunk_end = min(current + chunk, end_ts)
        params = {
            "currency": "BTC",
            "start_timestamp": current,
            "end_timestamp": chunk_end,
            "resolution": 3600  # 1 hour
        }
        for attempt in range(5):
            try:
                r = requests.get(url, params=params, timeout=15)
                r.raise_for_status()
                result = r.json()
                if "result" in result and "data" in result["result"]:
                    data = result["result"]["data"]
                    for row in data:
                        all_data.append({
                            "ts": pd.Timestamp(row[0], unit="ms"),
                            "dvol_open": row[1],
                            "dvol_high": row[2],
                            "dvol_low": row[3],
                            "dvol_close": row[4],
                        })
                break
            except Exception as e:
                if attempt < 4:
                    time.sleep(2)
                else:
                    print(f"    Failed chunk: {e}")

        current = chunk_end
        time.sleep(0.2)
        if len(all_data) % 5000 < 100:
            print(f"    {len(all_data):,} rows...", end="\r", flush=True)

    if not all_data:
        print("    WARNING: No DVOL data fetched")
        return pd.DataFrame()

    df = pd.DataFrame(all_data).drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    df.to_parquet(cache_file, index=False)
    print(f"    Saved {len(df):,} rows ({df['ts'].iloc[0]} to {df['ts'].iloc[-1]})")
    return df


def fetch_deribit_options_summary():
    """Fetch current BTC options open interest by type for put/call ratio.
    Note: Deribit doesn't have a clean historical PCR endpoint.
    We'll compute it from instruments snapshot + use DVOL as primary signal."""
    # For historical PCR, we'd need to build it from daily snapshots
    # For now, use DVOL as the options signal (IV is more actionable than PCR)
    print("  [Options PCR] Note: Historical PCR not directly available from Deribit API.")
    print("    Using DVOL (implied volatility) as primary options signal instead.")
    print("    DVOL captures the same fear/greed dynamic as PCR.")
    return None


# ============================================================
# 3. DXY DOLLAR INDEX (Yahoo Finance)
# ============================================================

def fetch_dxy():
    """Fetch DXY from Yahoo Finance."""
    cache_file = "data_cache/dxy_daily.parquet"
    if os.path.exists(cache_file):
        df = pd.read_parquet(cache_file)
        print(f"  [DXY] Cached: {len(df):,} rows ({df['ts'].iloc[0]} to {df['ts'].iloc[-1]})")
        return df

    print("  [DXY] Fetching from Yahoo Finance...")
    # Use Yahoo Finance v8 API
    symbol = "DX-Y.NYB"
    period1 = int(datetime(2020, 1, 1).timestamp())
    period2 = int(datetime.now().timestamp())

    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {
        "period1": period1,
        "period2": period2,
        "interval": "1d",
    }
    headers = {"User-Agent": "Mozilla/5.0"}

    for attempt in range(3):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=15)
            r.raise_for_status()
            data = r.json()
            break
        except Exception as e:
            if attempt < 2:
                time.sleep(3)
            else:
                print(f"    Yahoo Finance failed: {e}")
                # Fallback: try yfinance
                return fetch_dxy_yfinance()

    try:
        chart = data["chart"]["result"][0]
        timestamps = chart["timestamp"]
        quotes = chart["indicators"]["quote"][0]

        rows = []
        for i, ts in enumerate(timestamps):
            c = quotes["close"][i]
            if c is not None:
                rows.append({
                    "ts": pd.Timestamp(ts, unit="s").normalize(),
                    "dxy_open": quotes["open"][i],
                    "dxy_high": quotes["high"][i],
                    "dxy_low": quotes["low"][i],
                    "dxy_close": c,
                })

        df = pd.DataFrame(rows).drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
        df.to_parquet(cache_file, index=False)
        print(f"    Saved {len(df):,} rows ({df['ts'].iloc[0]} to {df['ts'].iloc[-1]})")
        return df
    except Exception as e:
        print(f"    Parse failed: {e}")
        return fetch_dxy_yfinance()


def fetch_dxy_yfinance():
    """Fallback: use yfinance library."""
    cache_file = "data_cache/dxy_daily.parquet"
    try:
        import yfinance as yf
        print("    Trying yfinance fallback...")
        ticker = yf.Ticker("DX-Y.NYB")
        df = ticker.history(period="5y")
        if df.empty:
            print("    WARNING: No DXY data from yfinance")
            return pd.DataFrame()
        df = df.reset_index()
        df = df.rename(columns={"Date": "ts", "Open": "dxy_open", "High": "dxy_high",
                                "Low": "dxy_low", "Close": "dxy_close"})
        df["ts"] = pd.to_datetime(df["ts"]).dt.tz_localize(None)
        df = df[["ts", "dxy_open", "dxy_high", "dxy_low", "dxy_close"]]
        df.to_parquet(cache_file, index=False)
        print(f"    Saved {len(df):,} rows ({df['ts'].iloc[0]} to {df['ts'].iloc[-1]})")
        return df
    except ImportError:
        print("    yfinance not installed. Installing...")
        import subprocess
        subprocess.check_call(["pip", "install", "yfinance", "-q"])
        return fetch_dxy_yfinance()
    except Exception as e:
        print(f"    yfinance failed: {e}")
        return pd.DataFrame()


# ============================================================
# MAIN
# ============================================================

def main():
    print("=" * 60)
    print("Fetching Additional Data Sources")
    print("=" * 60)

    print("\n[1/3] Binance Historical Funding Rate...")
    funding = fetch_binance_funding_rate()

    print("\n[2/3] Deribit Options (DVOL)...")
    dvol = fetch_deribit_dvol()
    fetch_deribit_options_summary()

    print("\n[3/3] DXY Dollar Index...")
    dxy = fetch_dxy()

    # Summary
    print("\n" + "=" * 60)
    print("DATA SUMMARY")
    print("=" * 60)

    datasets = [
        ("Funding Rate", funding, "funding_rate"),
        ("DVOL (IV)", dvol, "dvol_close"),
        ("DXY", dxy, "dxy_close"),
    ]

    for name, df, col in datasets:
        if df is not None and not df.empty:
            print(f"  {name:20s}: {len(df):>6,} rows | {str(df['ts'].iloc[0])[:10]} to {str(df['ts'].iloc[-1])[:10]} | {col}")
        else:
            print(f"  {name:20s}: NO DATA")

    print("\nDone! All data cached in data_cache/")


if __name__ == "__main__":
    main()
