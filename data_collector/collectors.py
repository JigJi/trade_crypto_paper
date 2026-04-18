"""
REST API Collectors
====================
All REST-based data collectors for v3 strategy factors.
Each function takes a psycopg2 connection and returns a result dict.
"""

import json
import time
import logging
from datetime import datetime, timezone
from decimal import Decimal

import requests
import psycopg2
from psycopg2.extras import execute_values

from .config import (
    BINANCE_KEY, BINANCE_SECRET,
    OI_SYMBOLS, OB_SYMBOL, OB_LEVELS, OB_SOURCE,
    LS_SYMBOL, TAKER_SYMBOL, KLINE_SYMBOLS,
    MACRO_TICKERS, COINGECKO_GLOBAL_URL, DERIBIT_BASE,
    MAX_RETRIES, RETRY_BACKOFF_SEC,
    ALT_SYMBOLS,
)

logger = logging.getLogger("data_collector.collectors")


# ============================================================
# Helpers
# ============================================================

def _retry(func, *args, max_retries=MAX_RETRIES, backoff=RETRY_BACKOFF_SEC, **kwargs):
    """Retry a function with exponential backoff."""
    for attempt in range(1, max_retries + 1):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            if attempt >= max_retries:
                raise
            wait = backoff * attempt
            logger.warning(f"Attempt {attempt}/{max_retries} failed: {e}. Retrying in {wait}s...")
            time.sleep(wait)


def _now_utc():
    return datetime.now(timezone.utc)


def _to_float(s):
    try:
        return float(s)
    except Exception:
        return None


# ============================================================
# 1. Basis (from get_basis.py)
# ============================================================

BINANCE_BASIS_URL = "https://fapi.binance.com/futures/data/basis"


def collect_basis(conn) -> dict:
    """Fetch basis from Binance REST, upsert to market_data.basis."""
    t0 = time.time()
    try:
        raw = _retry(
            _fetch_basis,
            pair="BTCUSDT",
            contract_type="PERPETUAL",
            period="5m",
            limit=1,
        )
        rows = _normalize_basis_rows(raw)

        upsert_sql = """
        INSERT INTO market_data.basis (
            pair, contract_type, ts,
            index_price, futures_price,
            basis, basis_rate, annualized_basis_rate
        ) VALUES %s
        ON CONFLICT (pair, contract_type, ts)
        DO UPDATE SET
            index_price = EXCLUDED.index_price,
            futures_price = EXCLUDED.futures_price,
            basis = EXCLUDED.basis,
            basis_rate = EXCLUDED.basis_rate,
            annualized_basis_rate = EXCLUDED.annualized_basis_rate
        """
        with conn.cursor() as cur:
            execute_values(cur, upsert_sql, rows, template="(%s,%s,%s,%s,%s,%s,%s,%s)")
        conn.commit()

        elapsed = int((time.time() - t0) * 1000)
        logger.info(f"[basis] OK: {len(rows)} rows in {elapsed}ms")
        return {"status": "ok", "rows": len(rows), "elapsed_ms": elapsed}

    except Exception as e:
        conn.rollback()
        elapsed = int((time.time() - t0) * 1000)
        logger.error(f"[basis] ERROR: {e}")
        return {"status": "error", "error": str(e), "elapsed_ms": elapsed}


def _fetch_basis(pair, contract_type, period, limit):
    params = {
        "pair": pair,
        "contractType": contract_type,
        "period": period,
        "limit": limit,
    }
    resp = requests.get(BINANCE_BASIS_URL, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        raise ValueError(f"Unexpected response type: {type(data)}")
    return data


def _normalize_basis_rows(raw):
    rows = []
    for r in raw:
        ts = datetime.fromtimestamp(int(r["timestamp"]) / 1000.0, tz=timezone.utc)
        ann = r.get("annualizedBasisRate")
        ann = None if (ann is None or str(ann).strip() == "" or str(ann).lower() == "null") else Decimal(str(ann))
        rows.append((
            str(r["pair"]),
            str(r["contractType"]),
            ts,
            Decimal(str(r["indexPrice"])),
            Decimal(str(r["futuresPrice"])),
            Decimal(str(r["basis"])),
            Decimal(str(r["basisRate"])),
            ann,
        ))
    return rows


# ============================================================
# 2. Order Book (from get_order_book.py)
# ============================================================

def ensure_order_book_schema(conn):
    """Create order_book_raw table + partitions if not exist. Called on daemon init."""
    ddl = """
    CREATE SCHEMA IF NOT EXISTS market_data;

    CREATE TABLE IF NOT EXISTS market_data.order_book_raw (
        id BIGINT GENERATED ALWAYS AS IDENTITY,
        symbol TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT 'um_futures',
        fetched_at TIMESTAMPTZ NOT NULL,
        last_update_id BIGINT,
        levels INT NOT NULL,
        bids JSONB NOT NULL,
        asks JSONB NOT NULL,
        meta JSONB,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (symbol, source, fetched_at, id)
    ) PARTITION BY RANGE (fetched_at);

    CREATE UNIQUE INDEX IF NOT EXISTS uq_obraw_symbol_source_time_luid
    ON market_data.order_book_raw (symbol, source, fetched_at, last_update_id);

    CREATE INDEX IF NOT EXISTS idx_obraw_symbol_time
    ON market_data.order_book_raw (symbol, fetched_at DESC);
    """

    fn_partition = """
    CREATE OR REPLACE FUNCTION market_data.ensure_obraw_month_partition(p_date date)
    RETURNS void LANGUAGE plpgsql AS $$
    DECLARE
        start_date date := date_trunc('month', p_date)::date;
        end_date   date := (date_trunc('month', p_date) + interval '1 month')::date;
        part_name  text := format('order_book_raw_%s', to_char(start_date, 'YYYY_MM'));
        full_name  text := format('market_data.%I', part_name);
    BEGIN
        IF NOT EXISTS (
            SELECT 1
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'market_data' AND c.relname = part_name
        ) THEN
            EXECUTE format($f$
                CREATE TABLE %s PARTITION OF market_data.order_book_raw
                FOR VALUES FROM (%L) TO (%L)
            $f$, full_name, start_date::timestamptz, end_date::timestamptz);
        END IF;
    END$$;
    """

    ensure_partitions = """
    SELECT market_data.ensure_obraw_month_partition((NOW())::date);
    SELECT market_data.ensure_obraw_month_partition((NOW() + interval '1 month')::date);
    """

    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(ddl)
        cur.execute(fn_partition)
        cur.execute(ensure_partitions)
    conn.autocommit = False
    logger.info("[order_book] Schema + partitions ensured")


def collect_order_book(conn) -> dict:
    """Fetch order book depth from Binance, compute meta, insert to market_data.order_book_raw."""
    t0 = time.time()
    try:
        from binance.client import Client
        client = Client(api_key=BINANCE_KEY, api_secret=BINANCE_SECRET)

        depth = _retry(_fetch_depth, client, OB_SYMBOL, OB_LEVELS, OB_SOURCE)
        latency_ms = int((time.time() - t0) * 1000)

        bids = depth.get("bids", [])[:OB_LEVELS]
        asks = depth.get("asks", [])[:OB_LEVELS]
        last_update_id = depth.get("lastUpdateId")

        fetched_at = _now_utc()
        meta = _quick_meta(bids, asks)
        meta["latency_ms"] = latency_ms

        sql = """
        INSERT INTO market_data.order_book_raw
            (symbol, source, fetched_at, last_update_id, levels, bids, asks, meta)
        VALUES %s
        ON CONFLICT (symbol, source, fetched_at, last_update_id) DO NOTHING;
        """
        vals = [(
            OB_SYMBOL, OB_SOURCE, fetched_at, last_update_id,
            int(meta["levels"]), json.dumps(bids), json.dumps(asks), json.dumps(meta),
        )]
        with conn.cursor() as cur:
            execute_values(cur, sql, vals)
        conn.commit()

        elapsed = int((time.time() - t0) * 1000)
        logger.info(f"[order_book] OK: spread={meta.get('spread')} imb={meta.get('imbalance', 0):.4f} in {elapsed}ms")
        return {"status": "ok", "rows": 1, "elapsed_ms": elapsed}

    except Exception as e:
        conn.rollback()
        elapsed = int((time.time() - t0) * 1000)
        logger.error(f"[order_book] ERROR: {e}")
        return {"status": "error", "error": str(e), "elapsed_ms": elapsed}


def _fetch_depth(client, symbol, top_n, source):
    if source == "um_futures":
        return client.futures_order_book(symbol=symbol, limit=top_n)
    elif source == "cm_futures":
        return client.futures_coin_order_book(symbol=symbol, limit=top_n)
    elif source == "spot":
        return client.get_order_book(symbol=symbol, limit=top_n)
    else:
        raise ValueError(f"Unknown source: {source}")


def _quick_meta(bids, asks):
    best_bid = _to_float(bids[0][0]) if bids else None
    best_ask = _to_float(asks[0][0]) if asks else None
    spread = (best_ask - best_bid) if (best_bid is not None and best_ask is not None) else None
    bid_qtys = [_to_float(x[1]) or 0.0 for x in bids]
    ask_qtys = [_to_float(x[1]) or 0.0 for x in asks]
    bid_sum = sum(bid_qtys)
    ask_sum = sum(ask_qtys)
    denom = bid_sum + ask_sum
    imbalance = ((bid_sum - ask_sum) / denom) if denom > 0 else 0.0

    max_bid_wall = max(bid_qtys) if bid_qtys else 0.0
    max_ask_wall = max(ask_qtys) if ask_qtys else 0.0
    wall_ratio = (max_bid_wall / max_ask_wall) if max_ask_wall > 0 else 0.0

    n_bids = len(bid_qtys)
    n_asks = len(ask_qtys)
    depth_5 = max(1, min(n_bids, n_asks, 50))
    depth_25 = max(1, min(n_bids, n_asks, 250))
    bid_5 = sum(bid_qtys[:depth_5])
    ask_5 = sum(ask_qtys[:depth_5])
    bid_25 = sum(bid_qtys[:depth_25])
    ask_25 = sum(ask_qtys[:depth_25])
    imb_5 = ((bid_5 - ask_5) / (bid_5 + ask_5)) if (bid_5 + ask_5) > 0 else 0.0
    imb_25 = ((bid_25 - ask_25) / (bid_25 + ask_25)) if (bid_25 + ask_25) > 0 else 0.0

    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread": spread,
        "bid_sum": bid_sum,
        "ask_sum": ask_sum,
        "imbalance": imbalance,
        "imbalance_top50": imb_5,
        "imbalance_top250": imb_25,
        "max_bid_wall": max_bid_wall,
        "max_ask_wall": max_ask_wall,
        "wall_ratio": wall_ratio,
        "levels": len(bids) if bids else 0,
    }


# ============================================================
# 3. Open Interest (from get_open_interest.py)
# ============================================================

def collect_open_interest(conn) -> dict:
    """Fetch OI from Binance for OI_SYMBOLS, upsert to market_data.open_interest.
    FIX: Uses UTC timestamps instead of Bangkok timezone."""
    t0 = time.time()
    total_rows = 0
    try:
        from binance.um_futures import UMFutures
        client = UMFutures(key=BINANCE_KEY, secret=BINANCE_SECRET)

        for symbol in OI_SYMBOLS:
            mark_price = float(client.mark_price(symbol=symbol)["markPrice"])
            oi = client.open_interest(symbol=symbol)

            # FIX: Use UTC instead of Bangkok timezone
            oi_time = datetime.fromtimestamp(oi["time"] / 1000, tz=timezone.utc)
            oi_val = float(oi["openInterest"])
            oi_usdt = oi_val * mark_price

            sql = """
            INSERT INTO market_data.open_interest
            (symbol, ts, interval, mark_price, oi_val, oi_usdt, anomaly, created_at)
            VALUES (%s, %s, '5m', %s, %s, %s, %s, NOW())
            ON CONFLICT (symbol, ts) DO UPDATE
            SET mark_price = EXCLUDED.mark_price,
                oi_val     = EXCLUDED.oi_val,
                oi_usdt    = EXCLUDED.oi_usdt,
                anomaly    = EXCLUDED.anomaly,
                recv_ts    = NOW();
            """
            with conn.cursor() as cur:
                cur.execute(sql, (symbol, oi_time, mark_price, oi_val, oi_usdt, False))
            total_rows += 1

        conn.commit()
        elapsed = int((time.time() - t0) * 1000)
        logger.info(f"[open_interest] OK: {total_rows} symbols in {elapsed}ms")
        return {"status": "ok", "rows": total_rows, "elapsed_ms": elapsed}

    except Exception as e:
        conn.rollback()
        elapsed = int((time.time() - t0) * 1000)
        logger.error(f"[open_interest] ERROR: {e}")
        return {"status": "error", "error": str(e), "elapsed_ms": elapsed}


# ============================================================
# 4. Premium Index (from get_premium_index.py)
# ============================================================

BINANCE_PREMIUM_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"


def collect_premium_index(conn) -> dict:
    """Fetch premium index + funding from Binance REST, insert to market_data.premium_index."""
    t0 = time.time()
    try:
        row = _retry(_fetch_premium_index, "BTCUSDT")

        sql = """
        INSERT INTO market_data.premium_index
        (symbol, ts, mark_price, index_price, last_funding_rate, est_settle_price, next_funding_time)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (symbol, ts) DO NOTHING
        """
        with conn.cursor() as cur:
            cur.execute(sql, (
                row["symbol"], row["ts"], row["mark_price"], row["index_price"],
                row["last_funding_rate"], row["est_settle_price"], row["next_funding_time"],
            ))
        conn.commit()

        elapsed = int((time.time() - t0) * 1000)
        logger.info(f"[premium_index] OK: mark={row['mark_price']:.2f} fr={row['last_funding_rate']:.6f} in {elapsed}ms")
        return {"status": "ok", "rows": 1, "elapsed_ms": elapsed}

    except Exception as e:
        conn.rollback()
        elapsed = int((time.time() - t0) * 1000)
        logger.error(f"[premium_index] ERROR: {e}")
        return {"status": "error", "error": str(e), "elapsed_ms": elapsed}


def _fetch_premium_index(symbol):
    resp = requests.get(f"{BINANCE_PREMIUM_URL}?symbol={symbol}", timeout=10)
    resp.raise_for_status()
    data = resp.json()
    return {
        "symbol": symbol,
        "ts": datetime.fromtimestamp(data["time"] / 1000, tz=timezone.utc),
        "mark_price": float(data["markPrice"]),
        "index_price": float(data["indexPrice"]),
        "last_funding_rate": float(data.get("lastFundingRate", 0.0)),
        "est_settle_price": float(data.get("estimatedSettlePrice", 0.0)),
        "next_funding_time": (
            datetime.fromtimestamp(data["nextFundingTime"] / 1000, tz=timezone.utc)
            if data.get("nextFundingTime") else None
        ),
    }


# ============================================================
# 5. Funding Rate (from funding_rate.py)
# ============================================================

def collect_funding_rate(conn) -> dict:
    """Fetch latest funding rate from Binance, insert to public.funding_rate.
    FIX: Uses .env credentials, UTC timestamps."""
    t0 = time.time()
    try:
        from binance.um_futures import UMFutures
        client = UMFutures(key=BINANCE_KEY, secret=BINANCE_SECRET)

        latest = _retry(lambda: client.funding_rate(symbol="BTCUSDT", limit=1)[0])

        # FIX: Use UTC instead of Bangkok timezone
        funding_time = datetime.fromtimestamp(latest["fundingTime"] / 1000, tz=timezone.utc)
        funding_rate = float(latest["fundingRate"])
        mark_price = float(latest["markPrice"])

        sql = """
        INSERT INTO funding_rate (date, symbol, funding_rate, mark_price, created_at)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT ON CONSTRAINT unique_funding_key DO NOTHING;
        """
        with conn.cursor() as cur:
            cur.execute(sql, (funding_time, "BTCUSDT", funding_rate, mark_price, datetime.utcnow()))
        conn.commit()

        elapsed = int((time.time() - t0) * 1000)
        logger.info(f"[funding_rate] OK: rate={funding_rate:.6f} at {funding_time} in {elapsed}ms")
        return {"status": "ok", "rows": 1, "elapsed_ms": elapsed}

    except Exception as e:
        conn.rollback()
        elapsed = int((time.time() - t0) * 1000)
        logger.error(f"[funding_rate] ERROR: {e}")
        return {"status": "error", "error": str(e), "elapsed_ms": elapsed}


# ============================================================
# 6. Long/Short Ratio (from get_long_short_ratio.py)
# ============================================================

def collect_long_short_ratio(conn) -> dict:
    """Fetch global/top account/position long-short ratios from Binance.
    FIX: Uses UTC timestamps instead of Bangkok."""
    t0 = time.time()
    try:
        urls = {
            "gl": "https://fapi.binance.com/futures/data/globalLongShortAccountRatio",
            "ac": "https://fapi.binance.com/futures/data/topLongShortAccountRatio",
            "po": "https://fapi.binance.com/futures/data/topLongShortPositionRatio",
        }
        params = {"symbol": LS_SYMBOL, "period": "5m", "limit": 1}

        def _fetch_ls(u):
            resp = requests.get(u, params=params, timeout=10)
            resp.raise_for_status()
            return resp

        results = {}
        for key, url in urls.items():
            resp = _retry(_fetch_ls, url)
            data = resp.json()
            if data:
                results[key] = data[-1]

        if not results:
            return {"status": "ok", "rows": 0, "elapsed_ms": int((time.time() - t0) * 1000)}

        gl = results.get("gl", {})
        ac = results.get("ac", {})
        po = results.get("po", {})

        # Use UTC timestamp
        ts_ms = int(gl.get("timestamp", ac.get("timestamp", po.get("timestamp", 0))))
        ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)

        sql = """
        INSERT INTO market_data.long_short_ratio
        (symbol, ts,
         gl_ac_long, gl_ac_short, gl_ac_ratio,
         top_ac_long, top_ac_short, top_ac_ratio,
         top_po_long, top_po_short, top_po_ratio)
        VALUES %s
        ON CONFLICT (ts, symbol) DO NOTHING;
        """
        row = [(
            LS_SYMBOL, ts,
            float(gl.get("longAccount", 0)), float(gl.get("shortAccount", 0)), float(gl.get("longShortRatio", 0)),
            float(ac.get("longAccount", 0)), float(ac.get("shortAccount", 0)), float(ac.get("longShortRatio", 0)),
            float(po.get("longAccount", 0)), float(po.get("shortAccount", 0)), float(po.get("longShortRatio", 0)),
        )]
        with conn.cursor() as cur:
            execute_values(cur, sql, row)
        conn.commit()

        elapsed = int((time.time() - t0) * 1000)
        logger.info(f"[long_short_ratio] OK: gl_ratio={float(gl.get('longShortRatio', 0)):.4f} in {elapsed}ms")
        return {"status": "ok", "rows": 1, "elapsed_ms": elapsed}

    except Exception as e:
        conn.rollback()
        elapsed = int((time.time() - t0) * 1000)
        logger.error(f"[long_short_ratio] ERROR: {e}")
        return {"status": "error", "error": str(e), "elapsed_ms": elapsed}


# ============================================================
# 7. Taker Volume (from get_taker_volume.py)
# ============================================================

TAKER_URL = "https://fapi.binance.com/futures/data/takerlongshortRatio"


def collect_taker_volume(conn) -> dict:
    """Fetch taker buy/sell volume ratio from Binance.
    FIX: Uses UTC timestamps instead of Bangkok."""
    t0 = time.time()
    try:
        def _fetch_taker():
            resp = requests.get(TAKER_URL, params={
                "symbol": TAKER_SYMBOL, "period": "5m", "limit": 1
            }, timeout=10)
            resp.raise_for_status()
            return resp

        resp = _retry(_fetch_taker)
        data = resp.json()

        if not data:
            return {"status": "ok", "rows": 0, "elapsed_ms": int((time.time() - t0) * 1000)}

        d = data[-1]
        # FIX: Use UTC instead of Bangkok timezone
        ts = datetime.fromtimestamp(d["timestamp"] / 1000, tz=timezone.utc)

        sql = """
        INSERT INTO market_data.taker_volume (symbol, ts, buy_vol, sell_vol, buy_sell_ratio)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (symbol, ts) DO UPDATE
        SET buy_vol = EXCLUDED.buy_vol,
            sell_vol = EXCLUDED.sell_vol,
            buy_sell_ratio = EXCLUDED.buy_sell_ratio;
        """
        with conn.cursor() as cur:
            cur.execute(sql, (TAKER_SYMBOL, ts, float(d["buyVol"]), float(d["sellVol"]), float(d["buySellRatio"])))
        conn.commit()

        elapsed = int((time.time() - t0) * 1000)
        logger.info(f"[taker_volume] OK: ratio={float(d['buySellRatio']):.4f} in {elapsed}ms")
        return {"status": "ok", "rows": 1, "elapsed_ms": elapsed}

    except Exception as e:
        conn.rollback()
        elapsed = int((time.time() - t0) * 1000)
        logger.error(f"[taker_volume] ERROR: {e}")
        return {"status": "error", "error": str(e), "elapsed_ms": elapsed}


# ============================================================
# 8. Index Price Klines (from get_index_price_klines.py)
# ============================================================

def collect_index_price_klines(conn) -> dict:
    """Fetch index price klines (5m) from Binance."""
    t0 = time.time()
    total = 0
    try:
        for symbol in KLINE_SYMBOLS:
            resp = _retry(lambda s=symbol: requests.get(
                "https://fapi.binance.com/fapi/v1/indexPriceKlines",
                params={"symbol": s, "pair": s, "interval": "5m", "limit": 3},
                timeout=10,
            ))
            resp.raise_for_status()
            data = resp.json()

            sql = """
            INSERT INTO market_data.index_price_klines
            (symbol, interval, open_time, close_time, open, high, low, close)
            VALUES %s
            ON CONFLICT (symbol, interval, open_time) DO NOTHING
            """
            rows = [(
                symbol, "5m",
                datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc),
                datetime.fromtimestamp(k[6] / 1000, tz=timezone.utc),
                float(k[1]), float(k[2]), float(k[3]), float(k[4]),
            ) for k in data]

            with conn.cursor() as cur:
                execute_values(cur, sql, rows)
            total += len(rows)

        conn.commit()
        elapsed = int((time.time() - t0) * 1000)
        logger.info(f"[index_klines] OK: {total} rows in {elapsed}ms")
        return {"status": "ok", "rows": total, "elapsed_ms": elapsed}

    except Exception as e:
        conn.rollback()
        elapsed = int((time.time() - t0) * 1000)
        logger.error(f"[index_klines] ERROR: {e}")
        return {"status": "error", "error": str(e), "elapsed_ms": elapsed}


# ============================================================
# 9. Mark Price Klines (from get_mark_price_klines.py)
# ============================================================

def collect_mark_price_klines(conn) -> dict:
    """Fetch mark price klines (5m) from Binance."""
    t0 = time.time()
    total = 0
    try:
        for symbol in KLINE_SYMBOLS:
            resp = _retry(lambda s=symbol: requests.get(
                "https://fapi.binance.com/fapi/v1/markPriceKlines",
                params={"symbol": s, "interval": "5m", "limit": 3},
                timeout=10,
            ))
            resp.raise_for_status()
            data = resp.json()

            sql = """
            INSERT INTO market_data.mark_price_klines
            (symbol, interval, open_time, close_time, open, high, low, close)
            VALUES %s
            ON CONFLICT (symbol, interval, open_time) DO NOTHING
            """
            rows = [(
                symbol, "5m",
                datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc),
                datetime.fromtimestamp(k[6] / 1000, tz=timezone.utc),
                float(k[1]), float(k[2]), float(k[3]), float(k[4]),
            ) for k in data]

            with conn.cursor() as cur:
                execute_values(cur, sql, rows)
            total += len(rows)

        conn.commit()
        elapsed = int((time.time() - t0) * 1000)
        logger.info(f"[mark_klines] OK: {total} rows in {elapsed}ms")
        return {"status": "ok", "rows": total, "elapsed_ms": elapsed}

    except Exception as e:
        conn.rollback()
        elapsed = int((time.time() - t0) * 1000)
        logger.error(f"[mark_klines] ERROR: {e}")
        return {"status": "error", "error": str(e), "elapsed_ms": elapsed}


# ============================================================
# 10. Macro Indicators (from get_macro.py)
# ============================================================

def collect_macro_indicators(conn) -> dict:
    """Fetch DXY, US10Y, Gold, S&P500 via yfinance. Daily data."""
    t0 = time.time()
    try:
        import yfinance as yf
    except ImportError:
        return {"status": "skipped", "reason": "yfinance not installed"}

    inserted = 0
    try:
        for ticker, indicator in MACRO_TICKERS.items():
            try:
                df = yf.download(ticker, period="5d", interval="1d", progress=False)
                if df.empty:
                    logger.warning(f"[macro] {ticker} ({indicator}): no data")
                    continue
                # Flatten MultiIndex columns
                if hasattr(df.columns, 'levels') and len(df.columns.levels) > 1:
                    df.columns = df.columns.get_level_values(0)
                close_val = float(df.iloc[-1]["Close"])
                ts_date = df.index[-1].date()
                sql = """
                INSERT INTO market_data.macro_indicators (ts, indicator, value)
                VALUES (%s, %s, %s)
                ON CONFLICT (ts, indicator) DO UPDATE SET value = EXCLUDED.value
                """
                with conn.cursor() as cur:
                    cur.execute(sql, (ts_date, indicator, close_val))
                inserted += 1
            except Exception as e:
                logger.warning(f"[macro] {ticker}: {e}")

        conn.commit()
        elapsed = int((time.time() - t0) * 1000)
        logger.info(f"[macro] OK: {inserted}/{len(MACRO_TICKERS)} indicators in {elapsed}ms")
        return {"status": "ok", "rows": inserted, "elapsed_ms": elapsed}

    except Exception as e:
        conn.rollback()
        elapsed = int((time.time() - t0) * 1000)
        logger.error(f"[macro] ERROR: {e}")
        return {"status": "error", "error": str(e), "elapsed_ms": elapsed}


# ============================================================
# 11. BTC Dominance / Global Market (from get_btc_dominance.py)
# ============================================================

def collect_btc_dominance(conn) -> dict:
    """Fetch BTC dominance + global market data from CoinGecko."""
    t0 = time.time()
    try:
        resp = _retry(lambda: requests.get(COINGECKO_GLOBAL_URL, timeout=15))
        resp.raise_for_status()
        data = resp.json().get("data", {})

        btc_dom = data.get("market_cap_percentage", {}).get("btc")
        eth_dom = data.get("market_cap_percentage", {}).get("eth")
        total_mcap = data.get("total_market_cap", {}).get("usd")
        total_vol = data.get("total_volume", {}).get("usd")

        # Round timestamp to 15-min bucket for dedup
        now_utc = datetime.now(timezone.utc)
        epoch = int(now_utc.timestamp())
        floored = (epoch // 900) * 900
        ts = datetime.fromtimestamp(floored, tz=timezone.utc)

        sql = """
        INSERT INTO market_data.market_global (ts, btc_dominance, eth_dominance, total_market_cap, total_volume_24h)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (ts) DO UPDATE SET
            btc_dominance    = EXCLUDED.btc_dominance,
            eth_dominance    = EXCLUDED.eth_dominance,
            total_market_cap = EXCLUDED.total_market_cap,
            total_volume_24h = EXCLUDED.total_volume_24h
        """
        with conn.cursor() as cur:
            cur.execute(sql, (ts, btc_dom, eth_dom, total_mcap, total_vol))
        conn.commit()

        elapsed = int((time.time() - t0) * 1000)
        logger.info(f"[btc_dominance] OK: BTC.D={btc_dom:.2f}% in {elapsed}ms")
        return {"status": "ok", "rows": 1, "elapsed_ms": elapsed}

    except Exception as e:
        conn.rollback()
        elapsed = int((time.time() - t0) * 1000)
        logger.error(f"[btc_dominance] ERROR: {e}")
        return {"status": "error", "error": str(e), "elapsed_ms": elapsed}


# ============================================================
# 12. Deribit Options (from get_deribit_options.py)
# ============================================================

def collect_deribit_options(conn) -> dict:
    """Fetch DVOL, P/C ratio, Max Pain, 25d Skew, GEX from Deribit."""
    t0 = time.time()
    try:
        from math import log, sqrt, exp
        from scipy.stats import norm

        def _api_get(endpoint, params=None):
            url = f"{DERIBIT_BASE}/{endpoint}"
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if "result" not in data:
                raise ValueError(f"No 'result' in response")
            return data["result"]

        def _bs_greeks(S, K, T, sigma):
            if T <= 0 or sigma <= 0:
                return {"delta_call": 0.0, "delta_put": 0.0, "gamma": 0.0}
            d1 = (log(S / K) + (sigma ** 2 / 2) * T) / (sigma * sqrt(T))
            delta_call = norm.cdf(d1)
            delta_put = delta_call - 1.0
            gamma = norm.pdf(d1) / (S * sigma * sqrt(T))
            return {"delta_call": delta_call, "delta_put": delta_put, "gamma": gamma}

        MONTH_MAP = {
            "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
            "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
        }

        def _parse_instrument(name):
            parts = name.split("-")
            if len(parts) != 4:
                return None
            _, expiry_str, strike_str, opt_type = parts
            if opt_type not in ("C", "P"):
                return None
            try:
                day = int(expiry_str[:2])
                month = MONTH_MAP[expiry_str[2:5]]
                year = 2000 + int(expiry_str[5:])
                expiry_dt = datetime(year, month, day, 8, 0, tzinfo=timezone.utc)
                strike = float(strike_str)
            except (ValueError, KeyError):
                return None
            return {"expiry": expiry_dt, "expiry_str": expiry_str, "strike": strike, "type": opt_type}

        # Fetch DVOL
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        dvol_result = _retry(lambda: _api_get("get_volatility_index_data", {
            "currency": "BTC", "resolution": 3600,
            "start_timestamp": now_ms - 2 * 3600 * 1000,
            "end_timestamp": now_ms,
        }))
        candles = dvol_result.get("data", [])
        dvol = candles[-1][4] if candles else None

        # Fetch options chain
        chain = _retry(lambda: _api_get("get_book_summary_by_currency", {
            "currency": "BTC", "kind": "option",
        }))
        options = []
        for item in chain:
            parsed = _parse_instrument(item.get("instrument_name", ""))
            if parsed is None:
                continue
            oi = item.get("open_interest", 0) or 0
            iv = item.get("mark_iv", 0) or 0
            if oi <= 0:
                continue
            options.append({**parsed, "oi": oi, "iv": iv / 100.0})

        # Fetch spot price
        spot_result = _retry(lambda: _api_get("get_index_price", {"index_name": "btc_usd"}))
        spot = spot_result.get("index_price")

        if not options or spot is None:
            return {"status": "error", "error": "missing options or spot", "elapsed_ms": int((time.time() - t0) * 1000)}

        # Compute metrics
        now = datetime.now(timezone.utc)
        call_oi = sum(o["oi"] for o in options if o["type"] == "C")
        put_oi = sum(o["oi"] for o in options if o["type"] == "P")
        pc_ratio = put_oi / call_oi if call_oi > 0 else None

        # Nearest expiry
        future_expiries = sorted(set(o["expiry"] for o in options if o["expiry"] > now))
        max_pain = None
        max_pain_expiry = None
        skew_25d = None
        gex = 0.0

        if future_expiries:
            nearest_exp = future_expiries[0]
            max_pain_expiry = nearest_exp.strftime("%d%b%y").upper()
            nearest_opts = [o for o in options if o["expiry"] == nearest_exp]

            # Max Pain
            strikes = sorted(set(o["strike"] for o in nearest_opts))
            if strikes:
                min_loss = float("inf")
                for candidate in strikes:
                    loss = sum(
                        max(0, candidate - o["strike"]) * o["oi"] if o["type"] == "C"
                        else max(0, o["strike"] - candidate) * o["oi"]
                        for o in nearest_opts
                    )
                    if loss < min_loss:
                        min_loss = loss
                        max_pain = candidate

            # 25-Delta Skew
            T = (nearest_exp - now).total_seconds() / (365.25 * 86400)
            if T > 0:
                best_call = {"diff": float("inf"), "iv": None}
                best_put = {"diff": float("inf"), "iv": None}
                for o in nearest_opts:
                    if o["iv"] <= 0:
                        continue
                    greeks = _bs_greeks(spot, o["strike"], T, o["iv"])
                    if o["type"] == "C":
                        diff = abs(greeks["delta_call"] - 0.25)
                        if diff < best_call["diff"]:
                            best_call = {"diff": diff, "iv": o["iv"]}
                    else:
                        diff = abs(greeks["delta_put"] - (-0.25))
                        if diff < best_put["diff"]:
                            best_put = {"diff": diff, "iv": o["iv"]}
                if best_put["iv"] is not None and best_call["iv"] is not None:
                    skew_25d = (best_put["iv"] - best_call["iv"]) * 100

        # GEX
        for o in options:
            T_opt = (o["expiry"] - now).total_seconds() / (365.25 * 86400)
            if T_opt <= 0 or o["iv"] <= 0:
                continue
            gamma = _bs_greeks(spot, o["strike"], T_opt, o["iv"])["gamma"]
            contract_gex = gamma * o["oi"] * spot * spot / 100
            gex += contract_gex if o["type"] == "C" else -contract_gex

        # Upsert
        epoch = int(now.timestamp())
        ts = datetime.fromtimestamp((epoch // 900) * 900, tz=timezone.utc)

        sql = """
        INSERT INTO market_data.options_data
            (ts, dvol, skew_25d, put_call_ratio, max_pain, max_pain_expiry,
             total_oi_calls, total_oi_puts, gex_net, spot_price)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (ts) DO UPDATE SET
            dvol=EXCLUDED.dvol, skew_25d=EXCLUDED.skew_25d,
            put_call_ratio=EXCLUDED.put_call_ratio, max_pain=EXCLUDED.max_pain,
            max_pain_expiry=EXCLUDED.max_pain_expiry,
            total_oi_calls=EXCLUDED.total_oi_calls, total_oi_puts=EXCLUDED.total_oi_puts,
            gex_net=EXCLUDED.gex_net, spot_price=EXCLUDED.spot_price
        """
        with conn.cursor() as cur:
            cur.execute(sql, (ts, dvol, skew_25d, pc_ratio, max_pain, max_pain_expiry,
                              call_oi, put_oi, gex, spot))
        conn.commit()

        elapsed = int((time.time() - t0) * 1000)
        logger.info(f"[deribit_options] OK: DVOL={dvol} P/C={pc_ratio:.3f} MaxPain=${max_pain:,.0f} in {elapsed}ms")
        return {"status": "ok", "rows": 1, "elapsed_ms": elapsed}

    except Exception as e:
        conn.rollback()
        elapsed = int((time.time() - t0) * 1000)
        logger.error(f"[deribit_options] ERROR: {e}")
        return {"status": "error", "error": str(e), "elapsed_ms": elapsed}


# ============================================================
# 13. Option Instruments Master (from get_option_main.py)
# ============================================================

EAPI_BASE = "https://eapi.binance.com"


def collect_option_instruments(conn) -> dict:
    """Sync BTC options instrument master from Binance EAPI. Daily."""
    t0 = time.time()
    try:
        resp = _retry(lambda: requests.get(
            f"{EAPI_BASE}/eapi/v1/exchangeInfo", timeout=20,
            headers={"User-Agent": "Mozilla/5.0"},
        ))
        resp.raise_for_status()
        items = resp.json().get("optionSymbols", [])

        rows = []
        for d in items:
            if not isinstance(d, dict):
                continue
            und = d.get("underlying", "")
            if not und.startswith("BTC"):
                continue
            sym = d.get("symbol")
            if not sym:
                continue
            side = d.get("side", "").upper()
            strike = float(d.get("strikePrice", 0) or 0)
            exp = datetime.utcfromtimestamp(int(d.get("expiryDate", 0)) / 1000).replace(tzinfo=timezone.utc)
            rows.append((sym, und, side, strike, exp))

        if rows:
            sql = """
            INSERT INTO market_data.option_instruments (symbol, underlying, side, strike, expiry)
            VALUES %s
            ON CONFLICT (symbol) DO UPDATE SET
                underlying=EXCLUDED.underlying, side=EXCLUDED.side,
                strike=EXCLUDED.strike, expiry=EXCLUDED.expiry
            """
            with conn.cursor() as cur:
                execute_values(cur, sql, rows)
            conn.commit()

        elapsed = int((time.time() - t0) * 1000)
        logger.info(f"[option_instruments] OK: {len(rows)} instruments in {elapsed}ms")
        return {"status": "ok", "rows": len(rows), "elapsed_ms": elapsed}

    except Exception as e:
        conn.rollback()
        elapsed = int((time.time() - t0) * 1000)
        logger.error(f"[option_instruments] ERROR: {e}")
        return {"status": "error", "error": str(e), "elapsed_ms": elapsed}


# ============================================================
# 14. Option Quotes & Greeks (from get_option_detail.py)
# ============================================================

def collect_option_greeks(conn) -> dict:
    """Fetch option quotes from Binance EAPI, compute BS Greeks, insert."""
    t0 = time.time()
    try:
        from math import log, sqrt, exp, erf, pi
        from scipy.optimize import brentq

        R = 0.02  # risk-free rate

        def _norm_cdf(x):
            return 0.5 * (1.0 + erf(x / sqrt(2.0)))

        def _norm_pdf(x):
            return exp(-0.5 * x * x) / sqrt(2.0 * pi)

        def _bs_price(S, K, T, r, sig, cp):
            if T <= 0 or sig <= 0:
                return max(cp * (S - K), 0.0)
            d1 = (log(S / K) + (r + 0.5 * sig * sig) * T) / (sig * sqrt(T))
            d2 = d1 - sig * sqrt(T)
            if cp > 0:
                return S * _norm_cdf(d1) - K * exp(-r * T) * _norm_cdf(d2)
            else:
                return K * exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)

        def _iv_solve(target, S, K, T, r, cp):
            intrinsic = max(cp * (S - K), 0.0)
            if target <= intrinsic + 1e-12:
                target = intrinsic + 1e-6
            return brentq(lambda s: _bs_price(S, K, T, r, s, cp) - target, 1e-4, 5.0)

        def _bs_greeks(S, K, T, r, sig, cp):
            d1 = (log(S / K) + (r + 0.5 * sig * sig) * T) / (sig * sqrt(T))
            d2 = d1 - sig * sqrt(T)
            nd1 = _norm_pdf(d1)
            delta = _norm_cdf(d1) if cp > 0 else (_norm_cdf(d1) - 1)
            gamma = nd1 / (S * sig * sqrt(T))
            if cp > 0:
                theta = -S * nd1 * sig / (2 * sqrt(T)) - r * K * exp(-r * T) * _norm_cdf(d2)
            else:
                theta = -S * nd1 * sig / (2 * sqrt(T)) + r * K * exp(-r * T) * _norm_cdf(-d2)
            vega = S * nd1 * sqrt(T)
            rho = K * T * exp(-r * T) * (_norm_cdf(d2) if cp > 0 else -_norm_cdf(-d2))
            return delta, gamma, theta, vega, rho

        # Get instruments from DB
        with conn.cursor() as cur:
            cur.execute("""
                SELECT symbol, underlying, side, strike, expiry
                FROM market_data.option_instruments
                WHERE expiry > NOW() ORDER BY expiry
            """)
            instruments = cur.fetchall()

        if not instruments:
            return {"status": "ok", "rows": 0, "elapsed_ms": int((time.time() - t0) * 1000),
                    "note": "no instruments"}

        # Limit to first 200 instruments to avoid timeout
        instruments = instruments[:200]

        ts = datetime.utcnow().replace(tzinfo=timezone.utc)
        und = instruments[0][1]

        # Fetch underlying mark price
        mark_resp = requests.get(f"https://fapi.binance.com/fapi/v1/premiumIndex",
                                 params={"symbol": und}, timeout=10)
        mark_data = mark_resp.json()
        S = float(mark_data.get("markPrice", mark_data.get("indexPrice", 0.0)) or 0.0)
        if S <= 0:
            return {"status": "error", "error": "could not get underlying price",
                    "elapsed_ms": int((time.time() - t0) * 1000)}

        quotes, greeks = [], []
        for sym, underlying, side, K, expiry in instruments:
            try:
                # Fetch mark price for this option
                r = requests.get(f"{EAPI_BASE}/eapi/v1/mark", params={"symbol": sym}, timeout=10)
                mark = None
                try:
                    j = r.json()
                    d = j[0] if isinstance(j, list) and j else (j if isinstance(j, dict) else None)
                    if d and d.get("markPrice"):
                        mark = float(d["markPrice"])
                except Exception:
                    pass

                if not mark:
                    # Try ticker
                    r2 = requests.get(f"{EAPI_BASE}/eapi/v1/ticker", params={"symbol": sym}, timeout=10)
                    try:
                        j2 = r2.json()
                        d = j2[0] if isinstance(j2, list) and j2 else (j2 if isinstance(j2, dict) else None)
                        if d:
                            bid = float(d.get("bidPrice", 0) or 0)
                            ask = float(d.get("askPrice", 0) or 0)
                            if bid > 0 and ask > 0:
                                mark = (bid + ask) / 2.0
                    except Exception:
                        pass

                if not mark:
                    continue

                cp = +1 if side == "CALL" else -1
                T = max((expiry - ts).total_seconds() / 31557600.0, 1 / 36500.0)
                iv = _iv_solve(mark, S, float(K), T, R, cp)
                dlt, gma, tht, vga, rho = _bs_greeks(S, float(K), T, R, iv, cp)
                quotes.append((sym, ts, mark, None, None, None, None, None, S))
                greeks.append((sym, ts, iv, dlt, gma, tht, vga, rho))

            except Exception:
                continue

        with conn.cursor() as cur:
            if quotes:
                execute_values(cur, """
                    INSERT INTO market_data.option_quotes
                    (symbol, ts, mark_price, bid_price, ask_price, last_price, volume, oi, underlying)
                    VALUES %s ON CONFLICT DO NOTHING
                """, quotes)
            if greeks:
                execute_values(cur, """
                    INSERT INTO market_data.option_greeks
                    (symbol, ts, iv_solved, delta, gamma, theta, vega, rho)
                    VALUES %s ON CONFLICT DO NOTHING
                """, greeks)
        conn.commit()

        elapsed = int((time.time() - t0) * 1000)
        logger.info(f"[option_greeks] OK: {len(quotes)} quotes, {len(greeks)} greeks in {elapsed}ms")
        return {"status": "ok", "rows": len(greeks), "elapsed_ms": elapsed}

    except Exception as e:
        conn.rollback()
        elapsed = int((time.time() - t0) * 1000)
        logger.error(f"[option_greeks] ERROR: {e}")
        return {"status": "error", "error": str(e), "elapsed_ms": elapsed}


# ============================================================
# 15. Fear & Greed Index (from scrape_etf_btc.py REST part)
# ============================================================

def collect_fear_greed(conn) -> dict:
    """Fetch Fear & Greed index from alternative.me API."""
    t0 = time.time()
    try:
        # Dedup: skip if already have today's data
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM fear_greed WHERE created_at::date = CURRENT_DATE LIMIT 1")
            if cur.fetchone():
                elapsed = int((time.time() - t0) * 1000)
                logger.info(f"[fear_greed] Skipped: already have today's data")
                return {"status": "ok", "rows": 0, "elapsed_ms": elapsed, "note": "dedup"}

        resp = _retry(lambda: requests.get("https://api.alternative.me/fng/", timeout=10))
        resp.raise_for_status()
        data = resp.json()
        latest = data["data"][0]

        sql = """INSERT INTO fear_greed (created_at, score, description) VALUES (%s, %s, %s)"""
        with conn.cursor() as cur:
            cur.execute(sql, (datetime.utcnow(), int(latest["value"]), latest["value_classification"]))
        conn.commit()

        elapsed = int((time.time() - t0) * 1000)
        logger.info(f"[fear_greed] OK: score={latest['value']} ({latest['value_classification']}) in {elapsed}ms")
        return {"status": "ok", "rows": 1, "elapsed_ms": elapsed}

    except Exception as e:
        conn.rollback()
        elapsed = int((time.time() - t0) * 1000)
        logger.error(f"[fear_greed] ERROR: {e}")
        return {"status": "error", "error": str(e), "elapsed_ms": elapsed}


# ============================================================
# 16. ETF Flows (from scrape_etf_btc.py Selenium part)
# ============================================================

def collect_etf_flows(conn) -> dict:
    """Scrape BTC ETF flows from farside.co.uk. Requires Selenium + Chrome.
    Gracefully skips if selenium not installed or weekday check fails."""
    t0 = time.time()
    import datetime as dt
    weekday = dt.date.today().weekday()
    if weekday not in (1, 2, 3, 4, 5):  # Tue-Sat (for previous day's data)
        return {"status": "skipped", "reason": f"weekday={weekday}, ETF data only on trading days"}

    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from bs4 import BeautifulSoup
    except ImportError:
        return {"status": "skipped", "reason": "selenium/bs4 not installed"}

    try:
        options = Options()
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
        driver = webdriver.Chrome(options=options)
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        })
        driver.get("https://farside.co.uk/btc/")
        import time as _t
        _t.sleep(8)  # longer wait for Cloudflare challenge

        html = driver.page_source
        driver.quit()

        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table", class_="etf")
        if not table:
            return {"status": "error", "error": "ETF table not found on page"}

        data_rows = []
        for row in table.find_all("tr"):
            cols = row.find_all("td")
            if not cols:
                continue
            row_data = []
            for col in cols:
                text = col.get_text(strip=True)
                text = text.replace("(", "-").replace(")", "").replace(",", "")
                row_data.append(text)
            data_rows.append(row_data)

        columns = ["Date", "IBIT", "FBTC", "BITB", "ARKB", "BTCO", "EZBC", "BRRR", "HODL", "BTCW", "GBTC", "BTC", "Total"]
        if not data_rows:
            return {"status": "error", "error": "no data rows found"}

        # Get latest valid row
        valid_rows = [r for r in data_rows if len(r) >= len(columns) and r[0] not in ('Total', 'Average', 'Maximum', 'Minimum')]
        if not valid_rows:
            return {"status": "error", "error": "no valid data rows"}
        latest = valid_rows[-1]

        def sf(val):
            try:
                return float(val)
            except (ValueError, TypeError):
                return 0.0

        sql = """INSERT INTO etf_btc (date, ibit, fbtc, bitb, arkb, btco, ezbc, brrr, hodl, btcw, gbtc, btc, total, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT ON CONSTRAINT unique_etf_key DO NOTHING;"""
        with conn.cursor() as cur:
            cur.execute(sql, (latest[0], sf(latest[1]), sf(latest[2]), sf(latest[3]), sf(latest[4]),
                              sf(latest[5]), sf(latest[6]), sf(latest[7]), sf(latest[8]), sf(latest[9]),
                              sf(latest[10]), sf(latest[11]), sf(latest[12]), datetime.utcnow()))
        conn.commit()

        elapsed = int((time.time() - t0) * 1000)
        logger.info(f"[etf_flows] OK: date={latest[0]} total={latest[-1]} in {elapsed}ms")
        return {"status": "ok", "rows": 1, "elapsed_ms": elapsed}

    except Exception as e:
        conn.rollback()
        elapsed = int((time.time() - t0) * 1000)
        logger.error(f"[etf_flows] ERROR: {e}")
        return {"status": "error", "error": str(e), "elapsed_ms": elapsed}


# ============================================================
# 17. Per-Coin Funding Rate (all v3+v4 coins)
# ============================================================

def ensure_alt_tables(conn):
    """Create per-coin funding rate and OI tables if they don't exist."""
    with conn.cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS market_data.funding_rate_alt (
            symbol VARCHAR(30) NOT NULL,
            ts TIMESTAMPTZ NOT NULL,
            funding_rate DOUBLE PRECISION,
            mark_price DOUBLE PRECISION,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            CONSTRAINT unique_funding_alt_key UNIQUE (symbol, ts)
        );
        CREATE INDEX IF NOT EXISTS idx_funding_alt_symbol_ts
            ON market_data.funding_rate_alt (symbol, ts);
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS market_data.open_interest_alt (
            symbol VARCHAR(30) NOT NULL,
            ts TIMESTAMPTZ NOT NULL,
            oi_val DOUBLE PRECISION,
            oi_usdt DOUBLE PRECISION,
            mark_price DOUBLE PRECISION,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            CONSTRAINT unique_oi_alt_key UNIQUE (symbol, ts)
        );
        CREATE INDEX IF NOT EXISTS idx_oi_alt_symbol_ts
            ON market_data.open_interest_alt (symbol, ts);
        """)
    conn.commit()
    logger.info("[alt_tables] Schema ensured")


def collect_funding_rate_alt(conn) -> dict:
    """Fetch latest funding rate for all v3+v4 coins."""
    t0 = time.time()
    total_rows = 0
    errors = []
    try:
        ensure_alt_tables(conn)

        url = "https://fapi.binance.com/fapi/v1/fundingRate"
        sql = """
        INSERT INTO market_data.funding_rate_alt (symbol, ts, funding_rate, mark_price, created_at)
        VALUES (%s, %s, %s, %s, NOW())
        ON CONFLICT ON CONSTRAINT unique_funding_alt_key DO NOTHING;
        """

        for symbol in ALT_SYMBOLS:
            try:
                resp = _retry(lambda s=symbol: requests.get(
                    url, params={"symbol": s, "limit": 1}, timeout=10))
                data = resp.json()
                if not data:
                    continue
                latest = data[0]
                funding_time = datetime.fromtimestamp(latest["fundingTime"] / 1000, tz=timezone.utc)
                funding_rate = float(latest["fundingRate"])
                mark_price = float(latest.get("markPrice", 0))

                with conn.cursor() as cur:
                    cur.execute(sql, (symbol, funding_time, funding_rate, mark_price))
                total_rows += 1
            except Exception as e:
                errors.append(f"{symbol}: {e}")
                continue

            time.sleep(0.05)  # rate limit

        conn.commit()
        elapsed = int((time.time() - t0) * 1000)
        logger.info(f"[funding_rate_alt] OK: {total_rows}/{len(ALT_SYMBOLS)} symbols in {elapsed}ms"
                     + (f", errors: {len(errors)}" if errors else ""))
        return {"status": "ok", "rows": total_rows, "elapsed_ms": elapsed, "errors": errors[:5]}

    except Exception as e:
        conn.rollback()
        elapsed = int((time.time() - t0) * 1000)
        logger.error(f"[funding_rate_alt] ERROR: {e}")
        return {"status": "error", "error": str(e), "elapsed_ms": elapsed}


# ============================================================
# 18. Per-Coin Open Interest (all v3+v4 coins)
# ============================================================

def collect_open_interest_alt(conn) -> dict:
    """Fetch OI for all v3+v4 coins from Binance."""
    t0 = time.time()
    total_rows = 0
    errors = []
    try:
        ensure_alt_tables(conn)

        from binance.um_futures import UMFutures
        client = UMFutures(key=BINANCE_KEY, secret=BINANCE_SECRET)

        sql = """
        INSERT INTO market_data.open_interest_alt
        (symbol, ts, oi_val, oi_usdt, mark_price, created_at)
        VALUES (%s, %s, %s, %s, %s, NOW())
        ON CONFLICT ON CONSTRAINT unique_oi_alt_key DO UPDATE
        SET oi_val = EXCLUDED.oi_val,
            oi_usdt = EXCLUDED.oi_usdt,
            mark_price = EXCLUDED.mark_price;
        """

        for symbol in ALT_SYMBOLS:
            try:
                mark_data = _retry(lambda s=symbol: client.mark_price(symbol=s))
                mark_price = float(mark_data["markPrice"])
                oi_data = _retry(lambda s=symbol: client.open_interest(symbol=s))

                oi_time = datetime.fromtimestamp(oi_data["time"] / 1000, tz=timezone.utc)
                oi_val = float(oi_data["openInterest"])
                oi_usdt = oi_val * mark_price

                with conn.cursor() as cur:
                    cur.execute(sql, (symbol, oi_time, oi_val, oi_usdt, mark_price))
                total_rows += 1
            except Exception as e:
                errors.append(f"{symbol}: {e}")
                continue

            time.sleep(0.05)  # rate limit

        conn.commit()
        elapsed = int((time.time() - t0) * 1000)
        logger.info(f"[open_interest_alt] OK: {total_rows}/{len(ALT_SYMBOLS)} symbols in {elapsed}ms"
                     + (f", errors: {len(errors)}" if errors else ""))
        return {"status": "ok", "rows": total_rows, "elapsed_ms": elapsed, "errors": errors[:5]}

    except Exception as e:
        conn.rollback()
        elapsed = int((time.time() - t0) * 1000)
        logger.error(f"[open_interest_alt] ERROR: {e}")
        return {"status": "error", "error": str(e), "elapsed_ms": elapsed}


# ============================================================
# 19. Per-Coin Alt Tables v2 (taker, LS, top-trader, OB, mark)
# ============================================================

def ensure_alt_tables_v2(conn):
    """Create 5 new per-coin data tables if they don't exist."""
    with conn.cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS market_data.taker_ratio_alt (
            symbol VARCHAR(30) NOT NULL,
            ts TIMESTAMPTZ NOT NULL,
            buy_sell_ratio DOUBLE PRECISION,
            buy_vol DOUBLE PRECISION,
            sell_vol DOUBLE PRECISION,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            CONSTRAINT unique_taker_ratio_alt_key UNIQUE (symbol, ts)
        );
        CREATE INDEX IF NOT EXISTS idx_taker_ratio_alt_symbol_ts
            ON market_data.taker_ratio_alt (symbol, ts);

        CREATE TABLE IF NOT EXISTS market_data.ls_ratio_alt (
            symbol VARCHAR(30) NOT NULL,
            ts TIMESTAMPTZ NOT NULL,
            long_short_ratio DOUBLE PRECISION,
            long_account DOUBLE PRECISION,
            short_account DOUBLE PRECISION,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            CONSTRAINT unique_ls_ratio_alt_key UNIQUE (symbol, ts)
        );
        CREATE INDEX IF NOT EXISTS idx_ls_ratio_alt_symbol_ts
            ON market_data.ls_ratio_alt (symbol, ts);

        CREATE TABLE IF NOT EXISTS market_data.top_trader_ls_alt (
            symbol VARCHAR(30) NOT NULL,
            ts TIMESTAMPTZ NOT NULL,
            account_ls_ratio DOUBLE PRECISION,
            account_long DOUBLE PRECISION,
            account_short DOUBLE PRECISION,
            position_ls_ratio DOUBLE PRECISION,
            position_long DOUBLE PRECISION,
            position_short DOUBLE PRECISION,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            CONSTRAINT unique_top_trader_ls_alt_key UNIQUE (symbol, ts)
        );
        CREATE INDEX IF NOT EXISTS idx_top_trader_ls_alt_symbol_ts
            ON market_data.top_trader_ls_alt (symbol, ts);

        CREATE TABLE IF NOT EXISTS market_data.order_book_alt (
            symbol VARCHAR(30) NOT NULL,
            ts TIMESTAMPTZ NOT NULL,
            bid_sum DOUBLE PRECISION,
            ask_sum DOUBLE PRECISION,
            imbalance DOUBLE PRECISION,
            spread_bps DOUBLE PRECISION,
            best_bid DOUBLE PRECISION,
            best_ask DOUBLE PRECISION,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            CONSTRAINT unique_order_book_alt_key UNIQUE (symbol, ts)
        );
        CREATE INDEX IF NOT EXISTS idx_order_book_alt_symbol_ts
            ON market_data.order_book_alt (symbol, ts);

        CREATE TABLE IF NOT EXISTS market_data.mark_klines_alt (
            symbol VARCHAR(30) NOT NULL,
            ts TIMESTAMPTZ NOT NULL,
            open DOUBLE PRECISION,
            high DOUBLE PRECISION,
            low DOUBLE PRECISION,
            close DOUBLE PRECISION,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            CONSTRAINT unique_mark_klines_alt_key UNIQUE (symbol, ts)
        );
        CREATE INDEX IF NOT EXISTS idx_mark_klines_alt_symbol_ts
            ON market_data.mark_klines_alt (symbol, ts);
        """)
    conn.commit()
    logger.info("[alt_tables_v2] Schema ensured (5 new tables)")


# ============================================================
# 20. Per-Coin Taker Buy/Sell Ratio
# ============================================================

def collect_taker_ratio_alt(conn) -> dict:
    """Fetch taker long/short ratio for all v3+v4 coins."""
    t0 = time.time()
    total_rows = 0
    errors = []
    try:
        ensure_alt_tables_v2(conn)

        url = "https://fapi.binance.com/futures/data/takerlongshortRatio"
        sql = """
        INSERT INTO market_data.taker_ratio_alt
        (symbol, ts, buy_sell_ratio, buy_vol, sell_vol, created_at)
        VALUES (%s, %s, %s, %s, %s, NOW())
        ON CONFLICT ON CONSTRAINT unique_taker_ratio_alt_key DO NOTHING;
        """

        for symbol in ALT_SYMBOLS:
            try:
                resp = _retry(lambda s=symbol: requests.get(
                    url, params={"symbol": s, "period": "15m", "limit": 1}, timeout=10))
                data = resp.json()
                if not data:
                    continue
                row = data[0]
                ts = datetime.fromtimestamp(int(row["timestamp"]) / 1000, tz=timezone.utc)
                with conn.cursor() as cur:
                    cur.execute(sql, (
                        symbol, ts,
                        _to_float(row.get("buySellRatio")),
                        _to_float(row.get("buyVol")),
                        _to_float(row.get("sellVol")),
                    ))
                total_rows += 1
            except Exception as e:
                errors.append(f"{symbol}: {e}")
                continue
            time.sleep(0.05)

        conn.commit()
        elapsed = int((time.time() - t0) * 1000)
        logger.info(f"[taker_ratio_alt] OK: {total_rows}/{len(ALT_SYMBOLS)} symbols in {elapsed}ms"
                     + (f", errors: {len(errors)}" if errors else ""))
        return {"status": "ok", "rows": total_rows, "elapsed_ms": elapsed, "errors": errors[:5]}

    except Exception as e:
        conn.rollback()
        elapsed = int((time.time() - t0) * 1000)
        logger.error(f"[taker_ratio_alt] ERROR: {e}")
        return {"status": "error", "error": str(e), "elapsed_ms": elapsed}


# ============================================================
# 21. Per-Coin Long/Short Account Ratio (global)
# ============================================================

def collect_ls_ratio_alt(conn) -> dict:
    """Fetch global long/short account ratio for all v3+v4 coins."""
    t0 = time.time()
    total_rows = 0
    errors = []
    try:
        ensure_alt_tables_v2(conn)

        url = "https://fapi.binance.com/futures/data/globalLongShortAccountRatio"
        sql = """
        INSERT INTO market_data.ls_ratio_alt
        (symbol, ts, long_short_ratio, long_account, short_account, created_at)
        VALUES (%s, %s, %s, %s, %s, NOW())
        ON CONFLICT ON CONSTRAINT unique_ls_ratio_alt_key DO NOTHING;
        """

        for symbol in ALT_SYMBOLS:
            try:
                resp = _retry(lambda s=symbol: requests.get(
                    url, params={"symbol": s, "period": "15m", "limit": 1}, timeout=10))
                data = resp.json()
                if not data:
                    continue
                row = data[0]
                ts = datetime.fromtimestamp(int(row["timestamp"]) / 1000, tz=timezone.utc)
                with conn.cursor() as cur:
                    cur.execute(sql, (
                        symbol, ts,
                        _to_float(row.get("longShortRatio")),
                        _to_float(row.get("longAccount")),
                        _to_float(row.get("shortAccount")),
                    ))
                total_rows += 1
            except Exception as e:
                errors.append(f"{symbol}: {e}")
                continue
            time.sleep(0.05)

        conn.commit()
        elapsed = int((time.time() - t0) * 1000)
        logger.info(f"[ls_ratio_alt] OK: {total_rows}/{len(ALT_SYMBOLS)} symbols in {elapsed}ms"
                     + (f", errors: {len(errors)}" if errors else ""))
        return {"status": "ok", "rows": total_rows, "elapsed_ms": elapsed, "errors": errors[:5]}

    except Exception as e:
        conn.rollback()
        elapsed = int((time.time() - t0) * 1000)
        logger.error(f"[ls_ratio_alt] ERROR: {e}")
        return {"status": "error", "error": str(e), "elapsed_ms": elapsed}


# ============================================================
# 22. Per-Coin Top Trader Long/Short (Account + Position)
# ============================================================

def collect_top_trader_ls_alt(conn) -> dict:
    """Fetch top trader L/S ratio (both account + position) for all v3+v4 coins."""
    t0 = time.time()
    total_rows = 0
    errors = []
    try:
        ensure_alt_tables_v2(conn)

        url_account = "https://fapi.binance.com/futures/data/topLongShortAccountRatio"
        url_position = "https://fapi.binance.com/futures/data/topLongShortPositionRatio"
        sql = """
        INSERT INTO market_data.top_trader_ls_alt
        (symbol, ts, account_ls_ratio, account_long, account_short,
         position_ls_ratio, position_long, position_short, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT ON CONSTRAINT unique_top_trader_ls_alt_key DO UPDATE SET
            account_ls_ratio = EXCLUDED.account_ls_ratio,
            account_long = EXCLUDED.account_long,
            account_short = EXCLUDED.account_short,
            position_ls_ratio = EXCLUDED.position_ls_ratio,
            position_long = EXCLUDED.position_long,
            position_short = EXCLUDED.position_short;
        """

        for symbol in ALT_SYMBOLS:
            try:
                # Account ratio
                resp_acc = _retry(lambda s=symbol: requests.get(
                    url_account, params={"symbol": s, "period": "15m", "limit": 1}, timeout=10))
                acc_data = resp_acc.json()

                # Position ratio
                resp_pos = _retry(lambda s=symbol: requests.get(
                    url_position, params={"symbol": s, "period": "15m", "limit": 1}, timeout=10))
                pos_data = resp_pos.json()

                if not acc_data and not pos_data:
                    continue

                # Use account data timestamp as primary
                acc = acc_data[0] if acc_data else {}
                pos = pos_data[0] if pos_data else {}
                ts_raw = acc.get("timestamp") or pos.get("timestamp")
                if not ts_raw:
                    continue
                ts = datetime.fromtimestamp(int(ts_raw) / 1000, tz=timezone.utc)

                with conn.cursor() as cur:
                    cur.execute(sql, (
                        symbol, ts,
                        _to_float(acc.get("longShortRatio")),
                        _to_float(acc.get("longAccount")),
                        _to_float(acc.get("shortAccount")),
                        _to_float(pos.get("longShortRatio")),
                        _to_float(pos.get("longAccount")),
                        _to_float(pos.get("shortAccount")),
                    ))
                total_rows += 1
            except Exception as e:
                errors.append(f"{symbol}: {e}")
                continue
            time.sleep(0.05)

        conn.commit()
        elapsed = int((time.time() - t0) * 1000)
        logger.info(f"[top_trader_ls_alt] OK: {total_rows}/{len(ALT_SYMBOLS)} symbols in {elapsed}ms"
                     + (f", errors: {len(errors)}" if errors else ""))
        return {"status": "ok", "rows": total_rows, "elapsed_ms": elapsed, "errors": errors[:5]}

    except Exception as e:
        conn.rollback()
        elapsed = int((time.time() - t0) * 1000)
        logger.error(f"[top_trader_ls_alt] ERROR: {e}")
        return {"status": "error", "error": str(e), "elapsed_ms": elapsed}


# ============================================================
# 23. Per-Coin Order Book Snapshot (top 20 levels)
# ============================================================

def collect_order_book_alt(conn) -> dict:
    """Fetch order book depth for all v3+v4 coins, compute imbalance/spread."""
    t0 = time.time()
    total_rows = 0
    errors = []
    try:
        ensure_alt_tables_v2(conn)

        url = "https://fapi.binance.com/fapi/v1/depth"
        sql = """
        INSERT INTO market_data.order_book_alt
        (symbol, ts, bid_sum, ask_sum, imbalance, spread_bps, best_bid, best_ask, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT ON CONSTRAINT unique_order_book_alt_key DO UPDATE SET
            bid_sum = EXCLUDED.bid_sum,
            ask_sum = EXCLUDED.ask_sum,
            imbalance = EXCLUDED.imbalance,
            spread_bps = EXCLUDED.spread_bps,
            best_bid = EXCLUDED.best_bid,
            best_ask = EXCLUDED.best_ask;
        """
        now_ts = _now_utc()

        for symbol in ALT_SYMBOLS:
            try:
                resp = _retry(lambda s=symbol: requests.get(
                    url, params={"symbol": s, "limit": 20}, timeout=10))
                data = resp.json()

                bids = data.get("bids", [])
                asks = data.get("asks", [])
                if not bids or not asks:
                    continue

                # Compute aggregates
                bid_sum = sum(float(b[0]) * float(b[1]) for b in bids)  # price * qty = notional
                ask_sum = sum(float(a[0]) * float(a[1]) for a in asks)
                total = bid_sum + ask_sum
                imbalance = (bid_sum - ask_sum) / total if total > 0 else 0.0

                best_bid = float(bids[0][0])
                best_ask = float(asks[0][0])
                mid = (best_bid + best_ask) / 2
                spread_bps = ((best_ask - best_bid) / mid) * 10000 if mid > 0 else 0.0

                with conn.cursor() as cur:
                    cur.execute(sql, (
                        symbol, now_ts, bid_sum, ask_sum,
                        imbalance, spread_bps, best_bid, best_ask,
                    ))
                total_rows += 1
            except Exception as e:
                errors.append(f"{symbol}: {e}")
                continue
            time.sleep(0.05)

        conn.commit()
        elapsed = int((time.time() - t0) * 1000)
        logger.info(f"[order_book_alt] OK: {total_rows}/{len(ALT_SYMBOLS)} symbols in {elapsed}ms"
                     + (f", errors: {len(errors)}" if errors else ""))
        return {"status": "ok", "rows": total_rows, "elapsed_ms": elapsed, "errors": errors[:5]}

    except Exception as e:
        conn.rollback()
        elapsed = int((time.time() - t0) * 1000)
        logger.error(f"[order_book_alt] ERROR: {e}")
        return {"status": "error", "error": str(e), "elapsed_ms": elapsed}


# ============================================================
# 24. Per-Coin Mark Price Klines (premium/discount)
# ============================================================

def collect_mark_klines_alt(conn) -> dict:
    """Fetch mark price klines for all v3+v4 coins (15m interval)."""
    t0 = time.time()
    total_rows = 0
    errors = []
    try:
        ensure_alt_tables_v2(conn)

        url = "https://fapi.binance.com/fapi/v1/markPriceKlines"
        sql = """
        INSERT INTO market_data.mark_klines_alt
        (symbol, ts, open, high, low, close, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT ON CONSTRAINT unique_mark_klines_alt_key DO NOTHING;
        """

        for symbol in ALT_SYMBOLS:
            try:
                resp = _retry(lambda s=symbol: requests.get(
                    url, params={"symbol": s, "interval": "15m", "limit": 1}, timeout=10))
                data = resp.json()
                if not data:
                    continue
                kline = data[0]
                # kline: [open_time, open, high, low, close, ...]
                ts = datetime.fromtimestamp(int(kline[0]) / 1000, tz=timezone.utc)
                with conn.cursor() as cur:
                    cur.execute(sql, (
                        symbol, ts,
                        _to_float(kline[1]),
                        _to_float(kline[2]),
                        _to_float(kline[3]),
                        _to_float(kline[4]),
                    ))
                total_rows += 1
            except Exception as e:
                errors.append(f"{symbol}: {e}")
                continue
            time.sleep(0.05)

        conn.commit()
        elapsed = int((time.time() - t0) * 1000)
        logger.info(f"[mark_klines_alt] OK: {total_rows}/{len(ALT_SYMBOLS)} symbols in {elapsed}ms"
                     + (f", errors: {len(errors)}" if errors else ""))
        return {"status": "ok", "rows": total_rows, "elapsed_ms": elapsed, "errors": errors[:5]}

    except Exception as e:
        conn.rollback()
        elapsed = int((time.time() - t0) * 1000)
        logger.error(f"[mark_klines_alt] ERROR: {e}")
        return {"status": "error", "error": str(e), "elapsed_ms": elapsed}


# ============================================================
# 25. Per-Coin Basis (mark-index spread) via premiumIndex
# ============================================================

def ensure_basis_alt_table(conn):
    """Create basis_alt table if it doesn't exist."""
    with conn.cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS market_data.basis_alt (
            symbol VARCHAR(30) NOT NULL,
            ts TIMESTAMPTZ NOT NULL,
            mark_price DOUBLE PRECISION,
            index_price DOUBLE PRECISION,
            basis_rate DOUBLE PRECISION,
            last_funding_rate DOUBLE PRECISION,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            CONSTRAINT unique_basis_alt_key UNIQUE (symbol, ts)
        );
        CREATE INDEX IF NOT EXISTS idx_basis_alt_symbol_ts
            ON market_data.basis_alt (symbol, ts);
        """)
    conn.commit()
    logger.info("[basis_alt] Schema ensured")


def collect_basis_alt(conn) -> dict:
    """Fetch premium index (mark vs index price) for all coins, compute basis_rate."""
    t0 = time.time()
    total_rows = 0
    errors = []
    try:
        ensure_basis_alt_table(conn)

        url = "https://fapi.binance.com/fapi/v1/premiumIndex"
        sql = """
        INSERT INTO market_data.basis_alt
        (symbol, ts, mark_price, index_price, basis_rate, last_funding_rate, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT ON CONSTRAINT unique_basis_alt_key DO UPDATE SET
            mark_price = EXCLUDED.mark_price,
            index_price = EXCLUDED.index_price,
            basis_rate = EXCLUDED.basis_rate,
            last_funding_rate = EXCLUDED.last_funding_rate;
        """

        for symbol in ALT_SYMBOLS:
            try:
                resp = _retry(lambda s=symbol: requests.get(
                    url, params={"symbol": s}, timeout=10))
                data = resp.json()
                if not data or "markPrice" not in data:
                    continue

                mark_price = float(data["markPrice"])
                index_price = float(data["indexPrice"])
                last_fr = float(data.get("lastFundingRate", 0))
                basis_rate = (mark_price - index_price) / index_price if index_price > 0 else 0.0
                ts = datetime.fromtimestamp(int(data["time"]) / 1000, tz=timezone.utc)

                with conn.cursor() as cur:
                    cur.execute(sql, (
                        symbol, ts, mark_price, index_price, basis_rate, last_fr,
                    ))
                total_rows += 1
            except Exception as e:
                errors.append(f"{symbol}: {e}")
                continue
            time.sleep(0.05)

        conn.commit()
        elapsed = int((time.time() - t0) * 1000)
        logger.info(f"[basis_alt] OK: {total_rows}/{len(ALT_SYMBOLS)} symbols in {elapsed}ms"
                     + (f", errors: {len(errors)}" if errors else ""))
        return {"status": "ok", "rows": total_rows, "elapsed_ms": elapsed, "errors": errors[:5]}

    except Exception as e:
        conn.rollback()
        elapsed = int((time.time() - t0) * 1000)
        logger.error(f"[basis_alt] ERROR: {e}")
        return {"status": "error", "error": str(e), "elapsed_ms": elapsed}
