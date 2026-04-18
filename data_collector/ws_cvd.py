"""
WebSocket CVD (Cumulative Volume Delta) Collector
====================================================
Aggregates BTC spot + futures aggTrade into 15-min buckets.
Runs in a background thread with its own asyncio event loop.
Ported from get_cvd.py.
"""

import sys
import asyncio
import logging
import threading
import time as _time
from datetime import datetime, timezone

import orjson
import websockets
import psycopg2

from .config import (
    DB_PARAMS,
    WS_CVD_FUTURES, WS_CVD_SPOT,
    WS_CVD_ALT_FUTURES, CVD_ALT_SYMBOLS,
    CVD_BUCKET_SECONDS, CVD_FLUSH_INTERVAL_SEC,
)

logger = logging.getLogger("data_collector.ws_cvd")

# ---- DDL ----
DDL = """
CREATE SCHEMA IF NOT EXISTS market_data;
CREATE TABLE IF NOT EXISTS market_data.cvd (
    ts            TIMESTAMPTZ NOT NULL,
    source        TEXT NOT NULL,
    buy_vol       DECIMAL DEFAULT 0,
    sell_vol      DECIMAL DEFAULT 0,
    volume_delta  DECIMAL DEFAULT 0,
    trade_count   INTEGER DEFAULT 0,
    is_final      BOOLEAN DEFAULT FALSE,
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (ts, source)
);
"""


def _bucket_start(epoch_ms):
    """Round down to nearest bucket boundary (UTC)."""
    epoch_sec = epoch_ms / 1000.0
    floored = int(epoch_sec // CVD_BUCKET_SECONDS) * CVD_BUCKET_SECONDS
    return datetime.fromtimestamp(floored, tz=timezone.utc)


class _Bucket:
    __slots__ = ("ts", "buy_vol", "sell_vol", "trade_count")

    def __init__(self, ts):
        self.ts = ts
        self.buy_vol = 0.0
        self.sell_vol = 0.0
        self.trade_count = 0

    def add(self, qty, is_buyer_maker):
        if is_buyer_maker:
            self.sell_vol += qty
        else:
            self.buy_vol += qty
        self.trade_count += 1


def _upsert_bucket(ts, source, buy_vol, sell_vol, trade_count, is_final):
    """Upsert a single bucket row."""
    sql = """
    INSERT INTO market_data.cvd (ts, source, buy_vol, sell_vol, volume_delta, trade_count, is_final)
    VALUES (%s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (ts, source) DO UPDATE SET
        buy_vol      = EXCLUDED.buy_vol,
        sell_vol     = EXCLUDED.sell_vol,
        volume_delta = EXCLUDED.volume_delta,
        trade_count  = EXCLUDED.trade_count,
        is_final     = EXCLUDED.is_final
    """
    delta = buy_vol - sell_vol
    conn = None
    try:
        conn = psycopg2.connect(**DB_PARAMS)
        with conn, conn.cursor() as cur:
            cur.execute(sql, (ts, source, buy_vol, sell_vol, delta, trade_count, is_final))
        return True
    except Exception as e:
        logger.error(f"CVD upsert error: {e}")
        return False
    finally:
        if conn is not None:
            conn.close()


class CVDCollector:
    """Async WebSocket CVD collector (spot + futures) that runs in a daemon thread."""

    def __init__(self):
        self._running = False
        self._thread = None
        self._stats = {"futures": {"recv": 0, "flushed": 0}, "spot": {"recv": 0, "flushed": 0}}
        self._stats_lock = threading.Lock()
        self._buckets = {}  # key: (source, ts) -> _Bucket
        self._lock = None   # asyncio.Lock, created in event loop

    def start(self):
        if self._running:
            logger.warning("CVDCollector already running")
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="ws-cvd")
        self._thread.start()
        logger.info("WS CVD thread started (spot + futures)")

    def stop(self):
        self._running = False

    def _run_loop(self):
        if sys.platform.startswith("win"):
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            # Ensure table exists
            try:
                conn = psycopg2.connect(**DB_PARAMS)
                conn.autocommit = True
                with conn.cursor() as cur:
                    cur.execute(DDL)
                conn.close()
                logger.info("CVD table ensured")
            except Exception as e:
                logger.warning(f"CVD DDL: {e}")

            loop.run_until_complete(self._run())
        except Exception as e:
            logger.error(f"CVD loop exited: {e}")
        finally:
            loop.close()

    async def _run(self):
        self._lock = asyncio.Lock()
        await asyncio.gather(
            self._ws_producer(WS_CVD_FUTURES, "futures"),
            self._ws_producer(WS_CVD_SPOT, "spot"),
            self._flusher(),
        )

    async def _ws_producer(self, ws_url, source):
        while self._running:
            try:
                async with websockets.connect(
                    ws_url, ping_interval=20, ping_timeout=20, close_timeout=10
                ) as ws:
                    logger.info(f"CVD WS connected: {source} -> {ws_url}")
                    async for msg in ws:
                        if not self._running:
                            break
                        with self._stats_lock:
                            self._stats[source]["recv"] += 1
                        try:
                            data = orjson.loads(msg)
                        except Exception:
                            continue

                        trade_time = data.get("T")
                        qty = float(data.get("q", 0))
                        is_buyer_maker = data.get("m", False)
                        if not trade_time or qty <= 0:
                            continue

                        ts = _bucket_start(trade_time)
                        key = (source, ts)

                        async with self._lock:
                            if key not in self._buckets:
                                await self._finalize_old(source, ts)
                                self._buckets[key] = _Bucket(ts)
                            self._buckets[key].add(qty, is_buyer_maker)

            except Exception as e:
                if not self._running:
                    break
                logger.warning(f"CVD WS {source} reconnect: {e}")
                await asyncio.sleep(2)

    async def _finalize_old(self, source, current_ts):
        to_remove = []
        for key, bucket in self._buckets.items():
            if key[0] == source and bucket.ts < current_ts:
                _upsert_bucket(bucket.ts, source, bucket.buy_vol, bucket.sell_vol,
                               bucket.trade_count, is_final=True)
                with self._stats_lock:
                    self._stats[source]["flushed"] += 1
                to_remove.append(key)
        for key in to_remove:
            del self._buckets[key]

    async def _flusher(self):
        last_stats = _time.time()
        while self._running:
            await asyncio.sleep(CVD_FLUSH_INTERVAL_SEC)
            async with self._lock:
                for key, bucket in self._buckets.items():
                    source = key[0]
                    _upsert_bucket(bucket.ts, source, bucket.buy_vol, bucket.sell_vol,
                                   bucket.trade_count, is_final=False)
            if _time.time() - last_stats >= 60:
                with self._stats_lock:
                    stats = dict(self._stats)
                logger.info(f"CVD stats: {stats}")
                last_stats = _time.time()

    def get_stats(self) -> dict:
        with self._stats_lock:
            return dict(self._stats)


# ============================================================
# Per-Coin CVD via Combined WebSocket Stream
# ============================================================

DDL_ALT = """
CREATE SCHEMA IF NOT EXISTS market_data;
CREATE TABLE IF NOT EXISTS market_data.cvd_alt (
    ts            TIMESTAMPTZ NOT NULL,
    symbol        TEXT NOT NULL,
    buy_vol       DECIMAL DEFAULT 0,
    sell_vol      DECIMAL DEFAULT 0,
    volume_delta  DECIMAL DEFAULT 0,
    trade_count   INTEGER DEFAULT 0,
    is_final      BOOLEAN DEFAULT FALSE,
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (ts, symbol)
);
CREATE INDEX IF NOT EXISTS idx_cvd_alt_symbol_ts ON market_data.cvd_alt (symbol, ts);
"""


def _upsert_bucket_alt(ts, symbol, buy_vol, sell_vol, trade_count, is_final):
    """Upsert a single per-coin CVD bucket row."""
    sql = """
    INSERT INTO market_data.cvd_alt (ts, symbol, buy_vol, sell_vol, volume_delta, trade_count, is_final)
    VALUES (%s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (ts, symbol) DO UPDATE SET
        buy_vol      = EXCLUDED.buy_vol,
        sell_vol     = EXCLUDED.sell_vol,
        volume_delta = EXCLUDED.volume_delta,
        trade_count  = EXCLUDED.trade_count,
        is_final     = EXCLUDED.is_final
    """
    delta = buy_vol - sell_vol
    conn = None
    try:
        conn = psycopg2.connect(**DB_PARAMS)
        with conn, conn.cursor() as cur:
            cur.execute(sql, (ts, symbol, buy_vol, sell_vol, delta, trade_count, is_final))
        return True
    except Exception as e:
        logger.error(f"CVD alt upsert error: {e}")
        return False
    finally:
        if conn is not None:
            conn.close()


class CVDAltCollector:
    """Per-coin futures CVD collector via combined aggTrade stream. Runs in a daemon thread."""

    def __init__(self):
        self._running = False
        self._thread = None
        self._stats = {"recv": 0, "flushed": 0, "symbols": 0}
        self._stats_lock = threading.Lock()
        self._buckets = {}  # key: (symbol, ts) -> _Bucket
        self._lock = None   # asyncio.Lock, created in event loop

    def start(self):
        if self._running:
            logger.warning("CVDAltCollector already running")
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="ws-cvd-alt")
        self._thread.start()
        logger.info(f"WS CVD Alt thread started ({len(CVD_ALT_SYMBOLS)} symbols)")

    def stop(self):
        self._running = False

    def _run_loop(self):
        if sys.platform.startswith("win"):
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            # Ensure table exists
            try:
                conn = psycopg2.connect(**DB_PARAMS)
                conn.autocommit = True
                with conn.cursor() as cur:
                    cur.execute(DDL_ALT)
                conn.close()
                logger.info("CVD alt table ensured")
            except Exception as e:
                logger.warning(f"CVD alt DDL: {e}")

            loop.run_until_complete(self._run())
        except Exception as e:
            logger.error(f"CVD alt loop exited: {e}")
        finally:
            loop.close()

    async def _run(self):
        self._lock = asyncio.Lock()
        await asyncio.gather(
            self._ws_producer(),
            self._flusher(),
        )

    async def _ws_producer(self):
        while self._running:
            try:
                async with websockets.connect(
                    WS_CVD_ALT_FUTURES, ping_interval=20, ping_timeout=20, close_timeout=10
                ) as ws:
                    logger.info(f"CVD Alt WS connected: {len(CVD_ALT_SYMBOLS)} streams")
                    async for msg in ws:
                        if not self._running:
                            break
                        with self._stats_lock:
                            self._stats["recv"] += 1
                        try:
                            envelope = orjson.loads(msg)
                        except Exception:
                            continue

                        # Combined stream format: {"stream": "btcusdt@aggTrade", "data": {...}}
                        stream_name = envelope.get("stream", "")
                        data = envelope.get("data")
                        if not data:
                            continue

                        # Extract symbol from stream name (e.g., "btcusdt@aggTrade" -> "BTCUSDT")
                        symbol = stream_name.split("@")[0].upper() if "@" in stream_name else None
                        if not symbol:
                            continue

                        trade_time = data.get("T")
                        qty = float(data.get("q", 0))
                        is_buyer_maker = data.get("m", False)
                        if not trade_time or qty <= 0:
                            continue

                        ts = _bucket_start(trade_time)
                        key = (symbol, ts)

                        async with self._lock:
                            if key not in self._buckets:
                                await self._finalize_old(symbol, ts)
                                self._buckets[key] = _Bucket(ts)
                            self._buckets[key].add(qty, is_buyer_maker)

            except Exception as e:
                if not self._running:
                    break
                logger.warning(f"CVD Alt WS reconnect: {e}")
                await asyncio.sleep(2)

    async def _finalize_old(self, symbol, current_ts):
        to_remove = []
        for key, bucket in self._buckets.items():
            if key[0] == symbol and bucket.ts < current_ts:
                _upsert_bucket_alt(bucket.ts, symbol, bucket.buy_vol, bucket.sell_vol,
                                   bucket.trade_count, is_final=True)
                with self._stats_lock:
                    self._stats["flushed"] += 1
                to_remove.append(key)
        for key in to_remove:
            del self._buckets[key]

    async def _flusher(self):
        last_stats = _time.time()
        while self._running:
            await asyncio.sleep(CVD_FLUSH_INTERVAL_SEC)
            seen_symbols = set()
            async with self._lock:
                for key, bucket in self._buckets.items():
                    symbol = key[0]
                    seen_symbols.add(symbol)
                    _upsert_bucket_alt(bucket.ts, symbol, bucket.buy_vol, bucket.sell_vol,
                                       bucket.trade_count, is_final=False)
            if _time.time() - last_stats >= 60:
                with self._stats_lock:
                    self._stats["symbols"] = len(seen_symbols)
                    stats = dict(self._stats)
                logger.info(f"CVD Alt stats: {stats}")
                last_stats = _time.time()

    def get_stats(self) -> dict:
        with self._stats_lock:
            return dict(self._stats)
