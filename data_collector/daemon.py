"""
Data Collector Daemon -- Main Entry Point
============================================
APScheduler daemon + WebSocket threads for unified data collection.
CLI: --once for single run, --status for health check, default = daemon mode.
"""

import sys
import os
import argparse
import logging
import signal
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler

import psycopg2

# Add parent dir to path so we can run as: python data_collector/daemon.py
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data_collector.config import DB_PARAMS, LOG_DIR
from data_collector.collectors import (
    collect_basis,
    collect_order_book,
    collect_open_interest,
    collect_premium_index,
    collect_funding_rate,
    collect_long_short_ratio,
    collect_taker_volume,
    collect_index_price_klines,
    collect_mark_price_klines,
    collect_macro_indicators,
    collect_btc_dominance,
    collect_deribit_options,
    collect_option_instruments,
    collect_option_greeks,
    collect_fear_greed,
    collect_etf_flows,
    ensure_order_book_schema,
    collect_funding_rate_alt,
    collect_open_interest_alt,
    collect_taker_ratio_alt,
    collect_ls_ratio_alt,
    collect_top_trader_ls_alt,
    collect_order_book_alt,
    collect_mark_klines_alt,
    collect_basis_alt,
)
from data_collector.aggregator import aggregate_1h_liq
from data_collector.whale import collect_whale_alerts, collect_news
from data_collector.ws_liquidation import LiquidationCollector
from data_collector.ws_cvd import CVDCollector, CVDAltCollector
from data_collector.health import HealthMonitor

logger = logging.getLogger("data_collector")

# Global state
health = HealthMonitor()
ws_liq_collector = None
ws_cvd_collector = None
ws_cvd_alt_collector = None


def setup_logging(debug: bool = False):
    """Setup console + rotating file logging."""
    logger.setLevel(logging.DEBUG if debug else logging.INFO)

    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG if debug else logging.INFO)
    ch.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"
    ))
    logger.addHandler(ch)

    LOG_DIR.mkdir(exist_ok=True)
    fh = RotatingFileHandler(
        LOG_DIR / "data_collector.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=30,
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    ))
    logger.addHandler(fh)

    logger.propagate = False


def get_conn():
    return psycopg2.connect(**DB_PARAMS)


def _run_collector(name: str, func, *args):
    """Run a collector function with error handling and health tracking."""
    try:
        result = func(*args)
        if result.get("status") in ("ok", "skipped"):
            health.record_success(name, result)
        else:
            health.record_failure(name, result.get("error", "unknown"))
    except Exception as e:
        logger.error(f"[{name}] Unexpected error: {e}", exc_info=True)
        health.record_failure(name, str(e))


def _job(name, func):
    """Generic job wrapper: create conn, run collector, close conn."""
    conn = get_conn()
    try:
        _run_collector(name, func, conn)
    finally:
        conn.close()


# ---- Scheduled job wrappers (each creates its own connection) ----

def job_basis():               _job("basis", collect_basis)
def job_order_book():          _job("order_book", collect_order_book)
def job_open_interest():       _job("open_interest", collect_open_interest)
def job_premium_index():       _job("premium_index", collect_premium_index)
def job_long_short_ratio():    _job("long_short_ratio", collect_long_short_ratio)
def job_taker_volume():        _job("taker_volume", collect_taker_volume)
def job_index_klines():        _job("index_klines", collect_index_price_klines)
def job_mark_klines():         _job("mark_klines", collect_mark_price_klines)
def job_funding_rate():        _job("funding_rate", collect_funding_rate)
def job_liq_aggregate():       _job("liq_1h_agg", aggregate_1h_liq)
def job_whale_alerts():        _job("whale_alerts", collect_whale_alerts)
def job_news():                _job("news", collect_news)
def job_btc_dominance():       _job("btc_dominance", collect_btc_dominance)
def job_deribit_options():     _job("deribit_options", collect_deribit_options)
def job_macro():               _job("macro", collect_macro_indicators)
def job_option_instruments():  _job("option_instruments", collect_option_instruments)
def job_option_greeks():       _job("option_greeks", collect_option_greeks)
def job_fear_greed():          _job("fear_greed", collect_fear_greed)
def job_etf_flows():           _job("etf_flows", collect_etf_flows)
def job_funding_rate_alt():    _job("funding_rate_alt", collect_funding_rate_alt)
def job_open_interest_alt():   _job("open_interest_alt", collect_open_interest_alt)
def job_taker_ratio_alt():     _job("taker_ratio_alt", collect_taker_ratio_alt)
def job_ls_ratio_alt():        _job("ls_ratio_alt", collect_ls_ratio_alt)
def job_top_trader_ls_alt():   _job("top_trader_ls_alt", collect_top_trader_ls_alt)
def job_order_book_alt():      _job("order_book_alt", collect_order_book_alt)
def job_mark_klines_alt():     _job("mark_klines_alt", collect_mark_klines_alt)
def job_basis_alt():           _job("basis_alt", collect_basis_alt)


def job_health_check():
    """Log health status + WS stats."""
    global ws_liq_collector, ws_cvd_collector, ws_cvd_alt_collector
    if ws_liq_collector:
        stats = ws_liq_collector.get_stats()
        health.record_success("tick_liq", {
            "rows": stats.get("inserted", 0), "elapsed_ms": 0, "ws_stats": stats,
        })
    if ws_cvd_collector:
        stats = ws_cvd_collector.get_stats()
        health.record_success("cvd", {
            "rows": stats.get("futures", {}).get("flushed", 0), "elapsed_ms": 0, "ws_stats": stats,
        })
    if ws_cvd_alt_collector:
        stats = ws_cvd_alt_collector.get_stats()
        health.record_success("cvd_alt", {
            "rows": stats.get("flushed", 0), "elapsed_ms": 0, "ws_stats": stats,
        })
    health.log_status()


def init_db():
    """Ensure DB schemas and partitions exist."""
    logger.info("Initializing DB schemas...")
    conn = get_conn()
    try:
        ensure_order_book_schema(conn)
    except Exception as e:
        logger.warning(f"Order book schema init: {e} (may already exist)")
    finally:
        conn.close()


def run_all_once():
    """Run all collectors once (for testing / --once mode)."""
    logger.info("Running all REST collectors once...")

    # 5-minute collectors
    for name, func in [
        ("basis", collect_basis),
        ("order_book", collect_order_book),
        ("open_interest", collect_open_interest),
        ("premium_index", collect_premium_index),
        ("long_short_ratio", collect_long_short_ratio),
        ("taker_volume", collect_taker_volume),
        ("index_klines", collect_index_price_klines),
        ("mark_klines", collect_mark_price_klines),
        ("btc_dominance", collect_btc_dominance),
        ("order_book_alt", collect_order_book_alt),
        ("basis_alt", collect_basis_alt),
    ]:
        conn = get_conn()
        try:
            _run_collector(name, func, conn)
        finally:
            conn.close()

    # 15-minute collectors
    for name, func in [
        ("deribit_options", collect_deribit_options),
        ("taker_ratio_alt", collect_taker_ratio_alt),
        ("ls_ratio_alt", collect_ls_ratio_alt),
        ("top_trader_ls_alt", collect_top_trader_ls_alt),
        ("mark_klines_alt", collect_mark_klines_alt),
    ]:
        conn = get_conn()
        try:
            _run_collector(name, func, conn)
        finally:
            conn.close()

    # Hourly collectors
    for name, func in [
        ("funding_rate", collect_funding_rate),
        ("funding_rate_alt", collect_funding_rate_alt),
        ("open_interest_alt", collect_open_interest_alt),
        ("liq_1h_agg", aggregate_1h_liq),
        ("whale_alerts", collect_whale_alerts),
        ("news", collect_news),
        ("macro", collect_macro_indicators),
        ("fear_greed", collect_fear_greed),
    ]:
        conn = get_conn()
        try:
            _run_collector(name, func, conn)
        finally:
            conn.close()

    # Daily collectors
    for name, func in [
        ("option_instruments", collect_option_instruments),
        ("option_greeks", collect_option_greeks),
        ("etf_flows", collect_etf_flows),
    ]:
        conn = get_conn()
        try:
            _run_collector(name, func, conn)
        finally:
            conn.close()

    health.log_status()


def run_daemon():
    """Run as APScheduler daemon with WebSocket threads."""
    global ws_liq_collector, ws_cvd_collector, ws_cvd_alt_collector

    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger

    scheduler = BlockingScheduler()

    # ---- 5-minute REST collectors ----
    for name, func in [
        ("basis", job_basis),
        ("order_book", job_order_book),
        ("open_interest", job_open_interest),
        ("premium_index", job_premium_index),
        ("long_short_ratio", job_long_short_ratio),
        ("taker_volume", job_taker_volume),
        ("index_klines", job_index_klines),
        ("mark_klines", job_mark_klines),
    ]:
        scheduler.add_job(
            func, CronTrigger(minute="*/5"),
            id=name, name=name, max_instances=1, misfire_grace_time=120,
        )

    # ---- 15-minute collectors ----
    for name, func in [
        ("btc_dominance", job_btc_dominance),
        ("deribit_options", job_deribit_options),
        ("taker_ratio_alt", job_taker_ratio_alt),
        ("ls_ratio_alt", job_ls_ratio_alt),
        ("top_trader_ls_alt", job_top_trader_ls_alt),
        ("mark_klines_alt", job_mark_klines_alt),
    ]:
        scheduler.add_job(
            func, CronTrigger(minute="*/15"),
            id=name, name=name, max_instances=1, misfire_grace_time=120,
        )

    # ---- Hourly collectors ----
    for name, func, minute in [
        ("funding_rate", job_funding_rate, "0"),
        ("funding_rate_alt", job_funding_rate_alt, "1"),   # per-coin FR
        ("liq_1h_agg", job_liq_aggregate, "5"),
        ("whale_alerts", job_whale_alerts, "10"),
        ("news", job_news, "10"),
        ("macro", job_macro, "15"),
        ("fear_greed", job_fear_greed, "15"),
    ]:
        scheduler.add_job(
            func, CronTrigger(minute=minute),
            id=name, name=name, max_instances=1, misfire_grace_time=300,
        )

    # ---- Per-coin OI + OB + basis: every 5 min ----
    for name, func in [
        ("open_interest_alt", job_open_interest_alt),
        ("order_book_alt", job_order_book_alt),
        ("basis_alt", job_basis_alt),
    ]:
        scheduler.add_job(
            func, CronTrigger(minute="*/5"),
            id=name, name=name, max_instances=1, misfire_grace_time=120,
        )

    # ---- Daily collectors (run at 00:30 UTC) ----
    for name, func in [
        ("option_instruments", job_option_instruments),
        ("etf_flows", job_etf_flows),
    ]:
        scheduler.add_job(
            func, CronTrigger(hour="0", minute="30"),
            id=name, name=name, max_instances=1, misfire_grace_time=600,
        )

    # Option greeks: every 5 min (depends on instruments being synced)
    scheduler.add_job(
        job_option_greeks, CronTrigger(minute="*/5"),
        id="option_greeks", name="option_greeks", max_instances=1, misfire_grace_time=120,
    )

    # ---- Health check every 5 min ----
    scheduler.add_job(
        job_health_check, CronTrigger(minute="*/5"),
        id="health_check", name="health_check", max_instances=1, misfire_grace_time=60,
    )

    # ---- Start WebSocket collectors ----
    ws_liq_collector = LiquidationCollector()
    ws_liq_collector.start()

    ws_cvd_collector = CVDCollector()
    ws_cvd_collector.start()

    ws_cvd_alt_collector = CVDAltCollector()
    ws_cvd_alt_collector.start()

    # ---- Graceful shutdown ----
    def shutdown(signum, frame):
        logger.info("Shutdown signal received...")
        if ws_liq_collector:
            ws_liq_collector.stop()
        if ws_cvd_collector:
            ws_cvd_collector.stop()
        if ws_cvd_alt_collector:
            ws_cvd_alt_collector.stop()
        scheduler.shutdown(wait=False)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    logger.info("=" * 60)
    logger.info("Data Collector Daemon Started")
    logger.info("  5min:  basis, order_book, open_interest, premium_index,")
    logger.info("         long_short_ratio, taker_volume, index/mark_klines, option_greeks")
    logger.info("         open_interest_alt, order_book_alt")
    logger.info("  15min: btc_dominance, deribit_options,")
    logger.info("         taker_ratio_alt, ls_ratio_alt, top_trader_ls_alt, mark_klines_alt")
    logger.info("  1h:    funding_rate(:00), funding_rate_alt(:01), liq_1h_agg(:05),")
    logger.info("         whale+news(:10), macro+f&g(:15)")
    logger.info("  Daily: option_instruments, etf_flows (00:30 UTC)")
    logger.info("  WS:    tick_liquidation, CVD, CVD_alt (continuous)")
    logger.info("  Health: */5min")
    logger.info("=" * 60)

    # Run initial collection
    logger.info("Running initial collection cycle...")
    run_all_once()

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        if ws_liq_collector:
            ws_liq_collector.stop()
        if ws_cvd_collector:
            ws_cvd_collector.stop()
        if ws_cvd_alt_collector:
            ws_cvd_alt_collector.stop()
        logger.info("Daemon stopped.")


def show_status():
    """Show current data freshness from DB."""
    conn = get_conn()
    try:
        # Build queries dynamically, skip tables that don't exist
        queries = [
            ("basis", "SELECT MAX(ts) FROM market_data.basis"),
            ("ob", "SELECT MAX(fetched_at) FROM market_data.order_book_raw"),
            ("oi", "SELECT MAX(ts) FROM market_data.open_interest WHERE symbol='BTCUSDT'"),
            ("premium", "SELECT MAX(ts) FROM market_data.premium_index"),
            ("funding", "SELECT MAX(date) FROM funding_rate"),
            ("tick_liq", "SELECT MAX(event_time) FROM market_data.liquidation"),
            ("liq_1h", "SELECT MAX(created_at) FROM liquidation"),
            ("whale", "SELECT MAX(alert_time) FROM whale_alert"),
            ("ls_ratio", "SELECT MAX(ts) FROM market_data.long_short_ratio"),
            ("taker", "SELECT MAX(ts) FROM market_data.taker_volume"),
            ("idx_kline", "SELECT MAX(open_time) FROM market_data.index_price_klines"),
            ("mrk_kline", "SELECT MAX(open_time) FROM market_data.mark_price_klines"),
            ("cvd", "SELECT MAX(ts) FROM market_data.cvd"),
            ("macro", "SELECT MAX(ts)::timestamptz FROM market_data.macro_indicators"),
            ("btc_dom", "SELECT MAX(ts) FROM market_data.market_global"),
            ("deribit", "SELECT MAX(ts) FROM market_data.options_data"),
            ("opt_inst", "SELECT MAX(expiry) FROM market_data.option_instruments WHERE expiry > NOW()"),
            ("opt_greek", "SELECT MAX(ts) FROM market_data.option_greeks"),
            ("f_greed", "SELECT MAX(created_at) FROM fear_greed"),
            ("etf", "SELECT MAX(created_at) FROM etf_btc"),
            ("news", "SELECT MAX(news_time) FROM news_crypto"),
            # Per-coin alt collectors v2
            ("taker_alt", "SELECT MAX(ts) FROM market_data.taker_ratio_alt"),
            ("ls_alt", "SELECT MAX(ts) FROM market_data.ls_ratio_alt"),
            ("tt_ls_alt", "SELECT MAX(ts) FROM market_data.top_trader_ls_alt"),
            ("ob_alt", "SELECT MAX(ts) FROM market_data.order_book_alt"),
            ("mk_kl_alt", "SELECT MAX(ts) FROM market_data.mark_klines_alt"),
            ("basis_alt", "SELECT MAX(ts) FROM market_data.basis_alt"),
            ("cvd_alt", "SELECT MAX(ts) FROM market_data.cvd_alt"),
        ]

        now = datetime.utcnow()
        print(f"\n{'='*60}")
        print(f"DATA FRESHNESS (as of {now.strftime('%Y-%m-%d %H:%M:%S')} UTC)")
        print(f"{'='*60}")

        for src, sql in queries:
            try:
                with conn.cursor() as cur:
                    cur.execute(sql)
                    latest = cur.fetchone()[0]
                if latest is None:
                    age_str = "NO DATA"
                else:
                    if hasattr(latest, 'tzinfo') and latest.tzinfo is not None:
                        from datetime import timezone as tz
                        latest = latest.astimezone(tz.utc).replace(tzinfo=None)
                    age = now - latest
                    hours = age.total_seconds() / 3600
                    if hours < 1:
                        age_str = f"{int(age.total_seconds() / 60)}min ago"
                    elif hours < 24:
                        age_str = f"{hours:.1f}h ago"
                    else:
                        age_str = f"{hours / 24:.1f}d ago"
                    age_str = f"{latest.strftime('%Y-%m-%d %H:%M')} ({age_str})"
            except Exception as e:
                age_str = f"ERROR: {e}"
            print(f"  {src:12s} {age_str}")

        # Per-coin coverage: how many symbols have data in last 24h per table
        print(f"\n{'='*60}")
        print(f"PER-COIN COVERAGE (distinct symbols with data in last 24h)")
        print(f"{'='*60}")
        coverage_queries = [
            ("funding_alt", "SELECT COUNT(DISTINCT symbol) FROM market_data.funding_rate_alt WHERE ts > NOW() - INTERVAL '24 hours'"),
            ("oi_alt", "SELECT COUNT(DISTINCT symbol) FROM market_data.open_interest_alt WHERE ts > NOW() - INTERVAL '24 hours'"),
            ("taker_alt", "SELECT COUNT(DISTINCT symbol) FROM market_data.taker_ratio_alt WHERE ts > NOW() - INTERVAL '24 hours'"),
            ("ls_alt", "SELECT COUNT(DISTINCT symbol) FROM market_data.ls_ratio_alt WHERE ts > NOW() - INTERVAL '24 hours'"),
            ("tt_ls_alt", "SELECT COUNT(DISTINCT symbol) FROM market_data.top_trader_ls_alt WHERE ts > NOW() - INTERVAL '24 hours'"),
            ("ob_alt", "SELECT COUNT(DISTINCT symbol) FROM market_data.order_book_alt WHERE ts > NOW() - INTERVAL '24 hours'"),
            ("mk_kl_alt", "SELECT COUNT(DISTINCT symbol) FROM market_data.mark_klines_alt WHERE ts > NOW() - INTERVAL '24 hours'"),
            ("basis_alt", "SELECT COUNT(DISTINCT symbol) FROM market_data.basis_alt WHERE ts > NOW() - INTERVAL '24 hours'"),
            ("cvd_alt", "SELECT COUNT(DISTINCT symbol) FROM market_data.cvd_alt WHERE ts > NOW() - INTERVAL '24 hours'"),
            ("tick_liq", "SELECT COUNT(DISTINCT symbol) FROM market_data.liquidation WHERE event_time > NOW() - INTERVAL '24 hours'"),
        ]
        for src, sql in coverage_queries:
            try:
                with conn.cursor() as cur:
                    cur.execute(sql)
                    count = cur.fetchone()[0] or 0
                print(f"  {src:12s} {count} symbols")
            except Exception as e:
                print(f"  {src:12s} ERROR: {e}")

        print(f"{'='*60}\n")

    except Exception as e:
        print(f"Error: {e}")
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Data Collector Daemon -- Unified data collection")
    parser.add_argument("--once", action="store_true", help="Run all collectors once and exit")
    parser.add_argument("--status", action="store_true", help="Show data freshness and exit")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    setup_logging(debug=args.debug)

    if args.status:
        show_status()
        return

    init_db()

    if args.once:
        logger.info("Single run mode (--once)")
        run_all_once()
        logger.info("Done.")
    else:
        run_daemon()


if __name__ == "__main__":
    main()
