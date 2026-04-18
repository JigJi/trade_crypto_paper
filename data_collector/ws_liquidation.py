"""
WebSocket Tick Liquidation Collector
======================================
Runs in a background thread with its own asyncio event loop.
Ported from get_liquidation.py with daemon-friendly design.
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
from psycopg2.extras import execute_values

from .config import (
    DB_PARAMS,
    WS_LIQ_URL, WS_LIQ_SYMBOLS,
    WS_BUFFER_MAX, WS_FLUSH_INTERVAL_SEC,
)

logger = logging.getLogger("data_collector.ws_liq")


def _to_utc(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


def _f(x):
    try:
        return float(x)
    except Exception:
        return 0.0


def _calc_notional(o: dict) -> float:
    ap, l, p, q = _f(o.get("ap")), _f(o.get("l")), _f(o.get("p")), _f(o.get("q"))
    if ap > 0 and l > 0:
        return ap * l
    if p > 0 and l > 0:
        return p * l
    if ap > 0 and q > 0:
        return ap * q
    return 0.0


class LiquidationCollector:
    """Async WebSocket liquidation collector that runs in a daemon thread."""

    def __init__(self):
        self._symbols_set = set(WS_LIQ_SYMBOLS)
        self._filter_enabled = len(self._symbols_set) > 0
        self._stats = {"recv": 0, "parsed": 0, "filtered": 0, "inserted": 0}
        self._stats_lock = threading.Lock()
        self._running = False
        self._thread = None

    def start(self):
        """Start the collector in a daemon thread."""
        if self._running:
            logger.warning("LiquidationCollector already running")
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="ws-liq")
        self._thread.start()
        logger.info(f"WS liquidation thread started (symbols={WS_LIQ_SYMBOLS})")

    def stop(self):
        self._running = False

    def _run_loop(self):
        # Windows: set selector event loop policy
        if sys.platform.startswith("win"):
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._run())
        except Exception as e:
            logger.error(f"WS loop exited: {e}")
        finally:
            loop.close()

    async def _run(self):
        q = asyncio.Queue(maxsize=50000)
        buf = []

        async def producer():
            while self._running:
                try:
                    async with websockets.connect(
                        WS_LIQ_URL, ping_interval=20, ping_timeout=20, close_timeout=10
                    ) as ws:
                        logger.info(f"WS connected: {WS_LIQ_URL}")
                        async for msg in ws:
                            if not self._running:
                                break
                            with self._stats_lock:
                                self._stats["recv"] += 1
                            try:
                                data = orjson.loads(msg)
                            except Exception:
                                continue
                            items = data if isinstance(data, list) else [data]
                            for it in items:
                                try:
                                    q.put_nowait(it)
                                except asyncio.QueueFull:
                                    pass
                except Exception as e:
                    if not self._running:
                        break
                    logger.warning(f"WS reconnect due to: {e}")
                    await asyncio.sleep(2)

        async def consumer():
            while self._running:
                try:
                    it = await asyncio.wait_for(q.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue

                o = it.get("o", {})
                if not o:
                    continue

                sym = (o.get("s") or "").upper()
                if self._filter_enabled and sym not in self._symbols_set:
                    continue

                with self._stats_lock:
                    self._stats["filtered"] += 1

                side = o.get("S")
                evtms = it.get("E") or o.get("T")
                if not sym or not side or not evtms:
                    continue

                evt = _to_utc(int(evtms))
                note = _calc_notional(o)

                with self._stats_lock:
                    self._stats["parsed"] += 1

                row = (
                    evt, sym, side,
                    o.get("o"), o.get("f"),
                    o.get("p"), o.get("ap"),
                    o.get("q"), o.get("l"), o.get("z"),
                    note,
                    orjson.dumps(it).decode("utf-8"),
                )
                buf.append(row)

                if len(buf) >= WS_BUFFER_MAX:
                    n = self._insert_raw(buf)
                    with self._stats_lock:
                        self._stats["inserted"] += n
                    buf.clear()

        async def flusher():
            last_stats_ts = _time.time()
            while self._running:
                await asyncio.sleep(WS_FLUSH_INTERVAL_SEC)
                if buf:
                    n = self._insert_raw(buf)
                    with self._stats_lock:
                        self._stats["inserted"] += n
                    buf.clear()
                # Log stats periodically
                if _time.time() - last_stats_ts >= 60:
                    with self._stats_lock:
                        stats = dict(self._stats)
                    logger.info(f"WS stats: {stats}")
                    last_stats_ts = _time.time()

        await asyncio.gather(producer(), consumer(), flusher())

    def _insert_raw(self, rows) -> int:
        if not rows:
            return 0
        sql = """
        INSERT INTO market_data.liquidation
        (event_time, symbol, side, order_type, time_in_force, price, avg_price,
         qty, last_fill_qty, filled_accum, notional_usd, raw_json)
        VALUES %s
        """
        conn = None
        try:
            conn = psycopg2.connect(**DB_PARAMS)
            with conn, conn.cursor() as cur:
                execute_values(cur, sql, rows, page_size=1000)
            return len(rows)
        except Exception as e:
            logger.error(f"DB insert_raw error ({len(rows)} rows): {e}")
            return 0
        finally:
            if conn is not None:
                conn.close()

    def get_stats(self) -> dict:
        """Return recv/parsed/inserted counts for health monitoring."""
        with self._stats_lock:
            return dict(self._stats)
