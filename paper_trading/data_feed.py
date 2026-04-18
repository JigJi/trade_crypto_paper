"""
Live Data Feed
===============
Fetch recent OHLCV from Binance Futures + BTC factors from PostgreSQL.
No parquet caching -- always live data.
"""

import time
import logging
from collections import deque
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import requests
import psycopg2

from .config import DB_PARAMS, WARMUP_BARS

logger = logging.getLogger(__name__)

BINANCE_FAPI = "https://fapi.binance.com/fapi/v1/klines"

# DB timestamps are stored in Bangkok local time (UTC+7).
# Binance API returns UTC timestamps. We must align them.
BKK_UTC_OFFSET = pd.Timedelta("7h")


def _to_naive_utc(ts_series: pd.Series) -> pd.Series:
    """Safely convert timestamp series to naive UTC.
    Handles both tz-aware (TIMESTAMPTZ -> auto-converted by pandas) and
    tz-naive (TIMESTAMP, stored as BKK local time) columns."""
    if ts_series.empty:
        return ts_series
    if ts_series.dt.tz is not None:
        # tz-aware: pandas already parsed as UTC offset -> convert to UTC, strip tz
        return ts_series.dt.tz_convert("UTC").dt.tz_localize(None)
    else:
        # tz-naive: assumed BKK local time -> subtract 7h
        return ts_series - BKK_UTC_OFFSET


# ---- OHLCV buffer (in-memory rolling window per symbol) ----

_ohlcv_buffers: dict[str, deque] = {}


def _get_buffer(symbol: str) -> deque:
    if symbol not in _ohlcv_buffers:
        _ohlcv_buffers[symbol] = deque(maxlen=WARMUP_BARS + 50)
    return _ohlcv_buffers[symbol]


def fetch_recent_ohlcv(symbol: str, n_bars: int = WARMUP_BARS) -> pd.DataFrame:
    """
    Fetch latest N candles from Binance Futures.
    Uses in-memory buffer to avoid refetching everything each cycle.
    Returns DataFrame with columns: date_time, open, high, low, close, volume
    """
    buf = _get_buffer(symbol)

    # If buffer has data, only fetch new candles since last one
    if len(buf) > 0:
        last_ts = buf[-1]["date_time"]
        start_ms = int(last_ts.timestamp() * 1000) + 1
        limit = 10  # fetch a few recent candles to catch up
    else:
        # Cold start: fetch full warmup
        start_ms = None
        limit = n_bars

    params = {"symbol": symbol, "interval": "15m", "limit": limit}
    if start_ms:
        params["startTime"] = start_ms

    data = None
    for attempt in range(3):
        try:
            r = requests.get(BINANCE_FAPI, params=params, timeout=15)
            if r.status_code == 429:
                logger.warning("Binance rate limited, waiting 5s...")
                time.sleep(5)
                continue
            r.raise_for_status()
            data = r.json()
            break
        except Exception as e:
            logger.error(f"Binance fetch attempt {attempt+1}/3 failed: {e}")
            if attempt < 2:
                time.sleep(2)
            else:
                raise

    if data is None:
        raise RuntimeError(f"Failed to fetch {symbol} OHLCV after 3 attempts (rate limited)")

    existing_ts = {c["date_time"] for c in buf}
    new_count = 0
    for k in data:
        ts = pd.Timestamp(k[0], unit="ms")
        if ts not in existing_ts:
            buf.append({
                "date_time": ts,
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
            })
            new_count += 1

    if new_count > 0:
        logger.debug(f"{symbol}: +{new_count} new candles, buffer={len(buf)}")

    df = pd.DataFrame(list(buf))
    df = df.sort_values("date_time").reset_index(drop=True)

    # Drop the current incomplete candle (last one if market is open)
    # Keep only closed candles (Binance timestamps are UTC)
    now = pd.Timestamp(datetime.utcnow())
    current_candle_start = now.floor("15min")
    df = df[df["date_time"] < current_candle_start].reset_index(drop=True)

    return df.tail(n_bars).reset_index(drop=True)


def load_btc_db_data_recent(hours: int = 48, liq_hours: int = 720) -> dict:
    """
    Load last N hours of BTC factors from PostgreSQL.
    Returns dict of DataFrames matching backtest load_btc_db_data() format.

    liq_hours: hours for liq + tick_liq data (default 720 = 30 days).
    Longer window needed so rolling(24).mean() / expanding().mean() have
    proper baseline — fixes cascade threshold bug where 48h window gave
    inflated liq_total_ma.
    """
    # DB timestamps are BKK local time, so use local time for SQL cutoff
    cutoff = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    liq_cutoff = (datetime.now() - timedelta(hours=liq_hours)).strftime("%Y-%m-%d %H:%M:%S")
    data = {}

    try:
        conn = psycopg2.connect(**DB_PARAMS)
    except Exception as e:
        logger.error(f"PostgreSQL connection failed: {e}")
        return data

    try:
        # OI
        data["oi"] = pd.read_sql(
            f"SELECT ts, oi_usdt FROM market_data.open_interest "
            f"WHERE symbol='BTCUSDT' AND ts >= '{cutoff}' ORDER BY ts",
            conn, parse_dates=["ts"])
        if not data["oi"].empty:
            data["oi"]["ts"] = _to_naive_utc(data["oi"]["ts"])

        # Premium index
        data["premium"] = pd.read_sql(
            f"SELECT ts, last_funding_rate, premium FROM market_data.premium_index "
            f"WHERE symbol='BTCUSDT' AND ts >= '{cutoff}' ORDER BY ts",
            conn, parse_dates=["ts"])
        if not data["premium"].empty:
            data["premium"]["ts"] = _to_naive_utc(data["premium"]["ts"])
            data["premium"]["last_funding_rate"] = data["premium"]["last_funding_rate"].astype(float)
            data["premium"]["premium"] = data["premium"]["premium"].astype(float)

        # Whale alerts
        data["whale"] = pd.read_sql(
            f"SELECT alert_time as ts, usd_value, sentiment FROM public.whale_alert "
            f"WHERE symbol='BTC' AND alert_time >= '{cutoff}' ORDER BY alert_time",
            conn, parse_dates=["ts"])
        if not data["whale"].empty:
            data["whale"]["ts"] = _to_naive_utc(data["whale"]["ts"])
            data["whale"]["usd_value"] = data["whale"]["usd_value"].astype(float)

        # Liquidations (30d window for rolling(24).mean baseline)
        data["liq"] = pd.read_sql(
            f"SELECT created_at as ts, liq_long_1h, liq_short_1h FROM public.liquidation "
            f"WHERE coin='BTC' AND created_at >= '{liq_cutoff}' ORDER BY created_at",
            conn, parse_dates=["ts"])
        if not data["liq"].empty:
            data["liq"]["ts"] = _to_naive_utc(data["liq"]["ts"])
            data["liq"]["ts"] = data["liq"]["ts"] + pd.Timedelta("1h")

        # ETF flows (last 30 days for MA)
        etf_cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        data["etf"] = pd.read_sql(
            f"SELECT date as ts, total FROM public.etf_btc "
            f"WHERE date >= '{etf_cutoff}' ORDER BY date",
            conn, parse_dates=["ts"])
        if not data["etf"].empty:
            data["etf"]["ts"] = _to_naive_utc(data["etf"]["ts"]) + pd.Timedelta("1d")
            data["etf"] = data["etf"].rename(columns={"total": "etf_flow"})

        # Funding rate
        fr_cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        data["funding"] = pd.read_sql(
            f"SELECT date as ts, funding_rate FROM public.funding_rate "
            f"WHERE symbol='BTCUSDT' AND date >= '{fr_cutoff}' ORDER BY date",
            conn, parse_dates=["ts"])
        if not data["funding"].empty:
            data["funding"]["ts"] = _to_naive_utc(data["funding"]["ts"])
            data["funding"] = data["funding"].rename(columns={"funding_rate": "fr_8h"})

        # ---- v3 new factors ----

        # Basis rate (futures premium)
        data["basis"] = pd.read_sql(
            f"SELECT ts, basis_rate FROM market_data.basis "
            f"WHERE pair='BTCUSDT' AND ts >= '{cutoff}' ORDER BY ts",
            conn, parse_dates=["ts"])
        if not data["basis"].empty:
            data["basis"]["ts"] = _to_naive_utc(data["basis"]["ts"])

        # Tick-level liquidations (30d window for rolling(16).mean + expanding().mean)
        data["tick_liq"] = pd.read_sql(
            f"SELECT event_time, side, notional_usd FROM market_data.liquidation "
            f"WHERE symbol='BTCUSDT' AND event_time >= '{liq_cutoff}' ORDER BY event_time",
            conn, parse_dates=["event_time"])
        if not data["tick_liq"].empty:
            data["tick_liq"]["ts"] = _to_naive_utc(data["tick_liq"]["event_time"])

        # Order book (JSONB meta fields)
        data["ob"] = pd.read_sql(
            f"SELECT fetched_at, "
            f"(meta->>'imbalance')::float as imbalance, "
            f"(meta->>'bid_sum')::float as bid_sum, "
            f"(meta->>'ask_sum')::float as ask_sum "
            f"FROM market_data.order_book_raw "
            f"WHERE fetched_at >= '{cutoff}' ORDER BY fetched_at",
            conn, parse_dates=["fetched_at"])
        if not data["ob"].empty:
            data["ob"]["ts"] = _to_naive_utc(data["ob"]["fetched_at"])

        logger.info("BTC DB data loaded: " + ", ".join(
            f"{k}={len(v)}" for k, v in data.items()
        ))

    except Exception as e:
        logger.error(f"Error loading BTC DB data: {e}")
    finally:
        conn.close()

    return data


def check_data_staleness(db_data: dict) -> dict[str, str]:
    """
    Check how stale each data source is.
    Returns dict of source -> staleness message (only for stale ones).
    """
    now = pd.Timestamp(datetime.utcnow())  # DB timestamps are now UTC-aligned
    stale = {}

    thresholds = {
        "oi": pd.Timedelta("60min"),
        "premium": pd.Timedelta("60min"),
        # SURGERY 2026-04-10: whale 4h → 48h
        # data_feed queries `whale_alert WHERE symbol='BTC'` but most whale
        # alerts on Telegram are USDT/USDC/ETH, not BTC. BTC-specific whale
        # alerts are naturally sparse (1-3 per day). 4h threshold = false alarm.
        "whale": pd.Timedelta("48h"),
        "liq": pd.Timedelta("2h"),
        "etf": pd.Timedelta("48h"),
        "funding": pd.Timedelta("12h"),
        # v3 new factors
        "basis": pd.Timedelta("60min"),
        "tick_liq": pd.Timedelta("60min"),
        "ob": pd.Timedelta("60min"),
    }

    for source, threshold in thresholds.items():
        if source not in db_data or db_data[source].empty:
            stale[source] = "NO DATA"
            continue
        latest = db_data[source]["ts"].max()
        if pd.isna(latest):
            stale[source] = "NO VALID TS"
            continue
        age = now - latest
        if age > threshold:
            stale[source] = f"stale by {age} (threshold {threshold})"

    return stale


# Critical factors for v3 composite score -- if too many are stale, signal is unreliable
CRITICAL_FACTORS = {"oi", "liq", "basis", "tick_liq", "ob"}
MAX_CRITICAL_STALE = 2  # suppress signals if > 2 critical factors are stale


def is_data_too_stale(stale_report: dict) -> tuple[bool, str]:
    """
    Check if data staleness is severe enough to suppress trading.
    Returns (should_suppress, reason).
    """
    if not stale_report:
        return False, ""

    stale_critical = [s for s in stale_report if s in CRITICAL_FACTORS]
    n = len(stale_critical)

    if n > MAX_CRITICAL_STALE:
        reason = f"{n}/{len(CRITICAL_FACTORS)} critical factors stale: {', '.join(stale_critical)}"
        return True, reason

    return False, ""


def clear_buffers():
    """Clear all OHLCV buffers (used on restart for clean state)."""
    _ohlcv_buffers.clear()
    logger.info("OHLCV buffers cleared")
