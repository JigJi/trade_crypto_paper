"""
Historical Backfill CLI
========================
Backfill per-coin data from Binance REST APIs.
Usage:
    python data_collector/backfill.py --source funding_rate_alt --days 30 [--symbols BTCUSDT,ETHUSDT]

Supported sources:
    funding_rate_alt, open_interest_alt, basis_alt, taker_ratio_alt,
    ls_ratio_alt, top_trader_ls_alt, mark_klines_alt
"""

import sys
import os
import argparse
import time
import logging
from datetime import datetime, timezone, timedelta

import requests
import psycopg2

# Add parent dir to path so we can run as: python data_collector/backfill.py
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data_collector.config import DB_PARAMS, ALT_SYMBOLS

logger = logging.getLogger("backfill")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

BASE_URL = "https://fapi.binance.com"
FUTURES_DATA_URL = "https://fapi.binance.com/futures/data"


def _ms(dt: datetime) -> int:
    """Convert datetime to epoch ms."""
    return int(dt.timestamp() * 1000)


def _ensure_tables(conn):
    """Ensure all target tables exist."""
    from data_collector.collectors import ensure_alt_tables, ensure_alt_tables_v2, ensure_basis_alt_table
    ensure_alt_tables(conn)
    ensure_alt_tables_v2(conn)
    ensure_basis_alt_table(conn)


def _paginated_fetch(url, params, start_time, end_time, limit, time_key="startTime",
                     page_sleep=0.2, symbol_label=""):
    """Fetch paginated data moving forward from start_time to end_time."""
    all_data = []
    current = start_time
    while current < end_time:
        p = {**params, time_key: _ms(current), "endTime": _ms(end_time), "limit": limit}
        resp = requests.get(url, params=p, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if not data:
            break
        all_data.extend(data)

        # Move start forward based on last record's timestamp
        last_ts = None
        if isinstance(data[-1], dict):
            # Find a timestamp field
            for key in ["fundingTime", "timestamp", "time"]:
                if key in data[-1]:
                    last_ts = data[-1][key]
                    break
        elif isinstance(data[-1], list):
            # Kline format: [open_time, ...]
            last_ts = data[-1][0]

        if last_ts is None:
            break
        current = datetime.fromtimestamp(last_ts / 1000, tz=timezone.utc) + timedelta(milliseconds=1)
        if len(data) < limit:
            break
        time.sleep(page_sleep)

    return all_data


# ============================================================
# Backfill functions per source
# ============================================================

def backfill_funding_rate_alt(conn, symbols, start, end):
    """Backfill funding rate history. API: /fapi/v1/fundingRate (limit=1000)."""
    url = f"{BASE_URL}/fapi/v1/fundingRate"
    sql = """
    INSERT INTO market_data.funding_rate_alt (symbol, ts, funding_rate, mark_price, created_at)
    VALUES (%s, %s, %s, %s, NOW())
    ON CONFLICT ON CONSTRAINT unique_funding_alt_key DO NOTHING;
    """
    total = 0
    for symbol in symbols:
        data = _paginated_fetch(
            url, {"symbol": symbol}, start, end, limit=1000,
            time_key="startTime", symbol_label=symbol,
        )
        rows = 0
        for rec in data:
            ts = datetime.fromtimestamp(rec["fundingTime"] / 1000, tz=timezone.utc)
            fr = float(rec["fundingRate"])
            mp = float(rec.get("markPrice", 0))
            with conn.cursor() as cur:
                cur.execute(sql, (symbol, ts, fr, mp))
            rows += 1
        conn.commit()
        total += rows
        logger.info(f"  {symbol}: {rows} funding rate records")
        time.sleep(0.5)
    return total


def backfill_open_interest_alt(conn, symbols, start, end):
    """Backfill OI history. API: /futures/data/openInterestHist (limit=500, period=15m)."""
    url = f"{FUTURES_DATA_URL}/openInterestHist"
    sql = """
    INSERT INTO market_data.open_interest_alt (symbol, ts, oi_val, oi_usdt, mark_price, created_at)
    VALUES (%s, %s, %s, %s, 0, NOW())
    ON CONFLICT ON CONSTRAINT unique_oi_alt_key DO NOTHING;
    """
    total = 0
    for symbol in symbols:
        data = _paginated_fetch(
            url, {"symbol": symbol, "period": "15m"}, start, end, limit=500,
            time_key="startTime", symbol_label=symbol,
        )
        rows = 0
        for rec in data:
            ts = datetime.fromtimestamp(rec["timestamp"] / 1000, tz=timezone.utc)
            oi_val = float(rec.get("sumOpenInterest", 0))
            oi_usdt = float(rec.get("sumOpenInterestValue", 0))
            with conn.cursor() as cur:
                cur.execute(sql, (symbol, ts, oi_val, oi_usdt))
            rows += 1
        conn.commit()
        total += rows
        logger.info(f"  {symbol}: {rows} OI records")
        time.sleep(0.5)
    return total


def backfill_basis_alt(conn, symbols, start, end):
    """Backfill basis (mark-index spread) via premiumIndexKlines. API: /fapi/v1/premiumIndexKlines (limit=1500, 15m)."""
    url = f"{BASE_URL}/fapi/v1/premiumIndexKlines"
    sql = """
    INSERT INTO market_data.basis_alt (symbol, ts, mark_price, index_price, basis_rate, last_funding_rate, created_at)
    VALUES (%s, %s, %s, %s, %s, 0, NOW())
    ON CONFLICT ON CONSTRAINT unique_basis_alt_key DO NOTHING;
    """
    total = 0
    for symbol in symbols:
        data = _paginated_fetch(
            url, {"symbol": symbol, "interval": "15m"}, start, end, limit=1500,
            time_key="startTime", symbol_label=symbol,
        )
        rows = 0
        for kline in data:
            # premiumIndexKlines: [open_time, open, high, low, close, ...]
            # The "close" value is the premium index at candle close
            ts = datetime.fromtimestamp(int(kline[0]) / 1000, tz=timezone.utc)
            premium_close = float(kline[4])  # premium index close
            # premium index = (mark - index) / index, so basis_rate = premium_close
            # mark_price and index_price not available from klines, store premium directly
            with conn.cursor() as cur:
                cur.execute(sql, (symbol, ts, 0, 0, premium_close))
            rows += 1
        conn.commit()
        total += rows
        logger.info(f"  {symbol}: {rows} basis records")
        time.sleep(0.5)
    return total


def backfill_taker_ratio_alt(conn, symbols, start, end):
    """Backfill taker buy/sell ratio. API: /futures/data/takerlongshortRatio (limit=500, period=15m)."""
    url = f"{FUTURES_DATA_URL}/takerlongshortRatio"
    sql = """
    INSERT INTO market_data.taker_ratio_alt (symbol, ts, buy_sell_ratio, buy_vol, sell_vol, created_at)
    VALUES (%s, %s, %s, %s, %s, NOW())
    ON CONFLICT ON CONSTRAINT unique_taker_ratio_alt_key DO NOTHING;
    """
    total = 0
    for symbol in symbols:
        data = _paginated_fetch(
            url, {"symbol": symbol, "period": "15m"}, start, end, limit=500,
            time_key="startTime", symbol_label=symbol,
        )
        rows = 0
        for rec in data:
            ts = datetime.fromtimestamp(rec["timestamp"] / 1000, tz=timezone.utc)
            ratio = float(rec.get("buySellRatio", 0))
            buy_vol = float(rec.get("buyVol", 0))
            sell_vol = float(rec.get("sellVol", 0))
            with conn.cursor() as cur:
                cur.execute(sql, (symbol, ts, ratio, buy_vol, sell_vol))
            rows += 1
        conn.commit()
        total += rows
        logger.info(f"  {symbol}: {rows} taker ratio records")
        time.sleep(0.5)
    return total


def backfill_ls_ratio_alt(conn, symbols, start, end):
    """Backfill global long/short account ratio. API: /futures/data/globalLongShortAccountRatio (limit=500, period=15m)."""
    url = f"{FUTURES_DATA_URL}/globalLongShortAccountRatio"
    sql = """
    INSERT INTO market_data.ls_ratio_alt (symbol, ts, long_short_ratio, long_account, short_account, created_at)
    VALUES (%s, %s, %s, %s, %s, NOW())
    ON CONFLICT ON CONSTRAINT unique_ls_ratio_alt_key DO NOTHING;
    """
    total = 0
    for symbol in symbols:
        data = _paginated_fetch(
            url, {"symbol": symbol, "period": "15m"}, start, end, limit=500,
            time_key="startTime", symbol_label=symbol,
        )
        rows = 0
        for rec in data:
            ts = datetime.fromtimestamp(rec["timestamp"] / 1000, tz=timezone.utc)
            ratio = float(rec.get("longShortRatio", 0))
            long_acc = float(rec.get("longAccount", 0))
            short_acc = float(rec.get("shortAccount", 0))
            with conn.cursor() as cur:
                cur.execute(sql, (symbol, ts, ratio, long_acc, short_acc))
            rows += 1
        conn.commit()
        total += rows
        logger.info(f"  {symbol}: {rows} LS ratio records")
        time.sleep(0.5)
    return total


def backfill_top_trader_ls_alt(conn, symbols, start, end):
    """Backfill top trader LS ratio. API: /futures/data/topLongShortAccountRatio (limit=500, period=15m)."""
    url_account = f"{FUTURES_DATA_URL}/topLongShortAccountRatio"
    url_position = f"{FUTURES_DATA_URL}/topLongShortPositionRatio"
    sql = """
    INSERT INTO market_data.top_trader_ls_alt
    (symbol, ts, account_ls_ratio, account_long, account_short,
     position_ls_ratio, position_long, position_short, created_at)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
    ON CONFLICT ON CONSTRAINT unique_top_trader_ls_alt_key DO NOTHING;
    """
    total = 0
    for symbol in symbols:
        # Fetch account ratios
        acc_data = _paginated_fetch(
            url_account, {"symbol": symbol, "period": "15m"}, start, end, limit=500,
            time_key="startTime", symbol_label=symbol,
        )
        # Fetch position ratios
        pos_data = _paginated_fetch(
            url_position, {"symbol": symbol, "period": "15m"}, start, end, limit=500,
            time_key="startTime", symbol_label=symbol,
        )
        # Index position data by timestamp
        pos_map = {}
        for rec in pos_data:
            pos_map[rec["timestamp"]] = rec

        rows = 0
        for rec in acc_data:
            ts = datetime.fromtimestamp(rec["timestamp"] / 1000, tz=timezone.utc)
            pos = pos_map.get(rec["timestamp"], {})
            with conn.cursor() as cur:
                cur.execute(sql, (
                    symbol, ts,
                    float(rec.get("longShortRatio", 0)),
                    float(rec.get("longAccount", 0)),
                    float(rec.get("shortAccount", 0)),
                    float(pos.get("longShortRatio", 0)),
                    float(pos.get("longAccount", 0)),
                    float(pos.get("shortAccount", 0)),
                ))
            rows += 1
        conn.commit()
        total += rows
        logger.info(f"  {symbol}: {rows} top trader LS records")
        time.sleep(0.5)
    return total


def backfill_mark_klines_alt(conn, symbols, start, end):
    """Backfill mark price klines. API: /fapi/v1/markPriceKlines (limit=1500, interval=15m)."""
    url = f"{BASE_URL}/fapi/v1/markPriceKlines"
    sql = """
    INSERT INTO market_data.mark_klines_alt (symbol, ts, open, high, low, close, created_at)
    VALUES (%s, %s, %s, %s, %s, %s, NOW())
    ON CONFLICT ON CONSTRAINT unique_mark_klines_alt_key DO NOTHING;
    """
    total = 0
    for symbol in symbols:
        data = _paginated_fetch(
            url, {"symbol": symbol, "interval": "15m"}, start, end, limit=1500,
            time_key="startTime", symbol_label=symbol,
        )
        rows = 0
        for kline in data:
            ts = datetime.fromtimestamp(int(kline[0]) / 1000, tz=timezone.utc)
            with conn.cursor() as cur:
                cur.execute(sql, (
                    symbol, ts,
                    float(kline[1]), float(kline[2]),
                    float(kline[3]), float(kline[4]),
                ))
            rows += 1
        conn.commit()
        total += rows
        logger.info(f"  {symbol}: {rows} mark kline records")
        time.sleep(0.5)
    return total


# ============================================================
# Source registry
# ============================================================

SOURCES = {
    "funding_rate_alt": backfill_funding_rate_alt,
    "open_interest_alt": backfill_open_interest_alt,
    "basis_alt": backfill_basis_alt,
    "taker_ratio_alt": backfill_taker_ratio_alt,
    "ls_ratio_alt": backfill_ls_ratio_alt,
    "top_trader_ls_alt": backfill_top_trader_ls_alt,
    "mark_klines_alt": backfill_mark_klines_alt,
}


def main():
    parser = argparse.ArgumentParser(
        description="Backfill historical per-coin data from Binance REST APIs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"Available sources: {', '.join(SOURCES.keys())}",
    )
    parser.add_argument("--source", required=True, choices=list(SOURCES.keys()),
                        help="Data source to backfill")
    parser.add_argument("--days", type=int, default=30,
                        help="Number of days to backfill (default: 30)")
    parser.add_argument("--symbols", type=str, default=None,
                        help="Comma-separated symbols (default: all ALT_SYMBOLS)")
    args = parser.parse_args()

    symbols = args.symbols.split(",") if args.symbols else ALT_SYMBOLS
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days)

    logger.info(f"Backfill: source={args.source}, days={args.days}, symbols={len(symbols)}")
    logger.info(f"  Range: {start.strftime('%Y-%m-%d %H:%M')} → {end.strftime('%Y-%m-%d %H:%M')} UTC")

    conn = psycopg2.connect(**DB_PARAMS)
    try:
        _ensure_tables(conn)
        func = SOURCES[args.source]
        total = func(conn, symbols, start, end)
        logger.info(f"Backfill complete: {total} total records inserted/skipped")
    except KeyboardInterrupt:
        logger.info("Backfill interrupted by user")
    except Exception as e:
        logger.error(f"Backfill failed: {e}", exc_info=True)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
