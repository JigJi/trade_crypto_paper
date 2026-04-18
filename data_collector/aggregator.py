"""
Liquidation Aggregator
========================
Derive 1h liquidation from tick-level data.
Replaces Coinglass Selenium scraper (scrape_liquidation.py).
Now aggregates per-coin (BTC + all ALT_SYMBOLS).
"""

import time
import logging
from datetime import datetime, timezone

import psycopg2

from .config import ALT_SYMBOLS

logger = logging.getLogger("data_collector.aggregator")


def _ensure_liq_pk(conn):
    """Migrate liquidation PK from (created_at) to (created_at, coin) if needed."""
    with conn.cursor() as cur:
        # Check if coin is part of PK
        cur.execute("""
        SELECT a.attname
        FROM pg_index i
        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
        WHERE i.indrelid = 'public.liquidation'::regclass AND i.indisprimary
        ORDER BY array_position(i.indkey, a.attnum)
        """)
        pk_cols = [r[0] for r in cur.fetchall()]
        if "coin" not in pk_cols:
            logger.info("[liq_1h_agg] Migrating PK to (created_at, coin)...")
            # Make coin NOT NULL first (fill any NULLs)
            cur.execute("UPDATE liquidation SET coin = 'BTC' WHERE coin IS NULL")
            cur.execute("ALTER TABLE liquidation ALTER COLUMN coin SET NOT NULL")
            cur.execute("ALTER TABLE liquidation DROP CONSTRAINT liquidation_pkey")
            cur.execute("ALTER TABLE liquidation ADD PRIMARY KEY (created_at, coin)")
            conn.commit()
            logger.info("[liq_1h_agg] PK migrated to (created_at, coin)")


def aggregate_1h_liq(conn) -> dict:
    """
    Query market_data.liquidation for last 1h of tick data.
    Aggregate per-coin into liq_long_1h (SELL side = longs liquidated)
    and liq_short_1h (BUY side = shorts liquidated).
    Insert into public.liquidation with per-coin rows.
    """
    t0 = time.time()
    try:
        _ensure_liq_pk(conn)

        # Get all unique symbols in tick data from last hour
        sql_read = """
        SELECT symbol, side, SUM(notional_usd) as total_notional
        FROM market_data.liquidation
        WHERE event_time >= NOW() - INTERVAL '1 hour'
        GROUP BY symbol, side
        """
        with conn.cursor() as cur:
            cur.execute(sql_read)
            rows = cur.fetchall()

        if not rows:
            elapsed = int((time.time() - t0) * 1000)
            logger.info("[liq_1h_agg] No tick data in last hour, skipping insert")
            return {"status": "ok", "rows": 0, "elapsed_ms": elapsed, "note": "no_data"}

        # Build per-symbol aggregates: {symbol: {liq_long, liq_short}}
        agg = {}
        for symbol, side, total in rows:
            if symbol not in agg:
                agg[symbol] = {"liq_long": 0.0, "liq_short": 0.0}
            if side == "SELL":
                agg[symbol]["liq_long"] = float(total) if total else 0.0
            elif side == "BUY":
                agg[symbol]["liq_short"] = float(total) if total else 0.0

        # Insert per-coin rows
        now_utc = datetime.now(timezone.utc)
        sql_insert = """
        INSERT INTO liquidation (created_at, coin, liq_long_1h, liq_short_1h)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (created_at, coin) DO UPDATE
        SET liq_long_1h = EXCLUDED.liq_long_1h,
            liq_short_1h = EXCLUDED.liq_short_1h
        """
        total_inserted = 0
        with conn.cursor() as cur:
            for symbol, vals in agg.items():
                coin = symbol.replace("USDT", "")
                cur.execute(sql_insert, (now_utc, coin, vals["liq_long"], vals["liq_short"]))
                total_inserted += 1
        conn.commit()

        elapsed = int((time.time() - t0) * 1000)
        # Log BTC specifically for compatibility
        btc = agg.get("BTCUSDT", {"liq_long": 0, "liq_short": 0})
        logger.info(
            f"[liq_1h_agg] OK: {total_inserted} coins, "
            f"BTC long_liq=${btc['liq_long']:,.0f} short_liq=${btc['liq_short']:,.0f} in {elapsed}ms"
        )
        return {"status": "ok", "rows": total_inserted, "elapsed_ms": elapsed,
                "liq_long": btc["liq_long"], "liq_short": btc["liq_short"]}

    except Exception as e:
        conn.rollback()
        elapsed = int((time.time() - t0) * 1000)
        logger.error(f"[liq_1h_agg] ERROR: {e}")
        return {"status": "error", "error": str(e), "elapsed_ms": elapsed}
