"""
Paper Trader -- Main Entry Point
==================================
APScheduler daemon that runs every 15 minutes.
CLI: --once for single run (testing), default = daemon mode.
"""

import sys
import os
import argparse
import atexit
import logging
import signal
from datetime import datetime
from logging.handlers import RotatingFileHandler

import pandas as pd

# Add parent dir to path so we can run as: python paper_trading/paper_trader.py
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paper_trading.config import (
    COINS, COIN_CONFIGS, COMPOSITE_WEIGHTS, V3_EXTRA_WEIGHTS,
    V5_COMPOSITE_WEIGHTS, V5_EXTRA_WEIGHTS,
    INIT_EQUITY, LOG_DIR, STATE_DIR, WARMUP_BARS, BUDGET_PER_COIN,
    BINANCE_TESTNET_KEY, BINANCE_TESTNET_SECRET,
    EXTREME_CONF3_ENABLED, EXTREME_CONF3_MIN_FACTORS,
    HEALTH_ENABLED, HEALTH_BLOCK_ENABLED,
    V6_SIZE_MULT_DISP_01, V6_SIZE_MULT_DISP_03,
    V6_SIZE_MULT_CASCADE_5X, V6_SIZE_MULT_MAX,
    MIN_BARS_BEFORE_FLIP, LONG_ENABLED,
)
from paper_trading import state_db as db
from paper_trading.alt_filter import check_entry_filter
from paper_trading.sizing import rolling_sharpe_multiplier
from paper_trading.data_feed import (
    fetch_recent_ohlcv, load_btc_db_data_recent, check_data_staleness,
    is_data_too_stale,
)
from paper_trading.strategy import (
    build_btc_features, compute_btc_composite_score,
    compute_btc_composite_score_v6,
    build_alt_technicals, evaluate_signal,
    count_active_factors, compute_vol_regime,
)
from paper_trading.exchange import TestnetExchange
from paper_trading.position_manager import PositionManager
from paper_trading.daily_report import generate_daily_report
from paper_trading.coin_health import CoinHealthMonitor

logger = logging.getLogger("paper_trading")

# Binance OHLCV timestamps are UTC; display offset for BKK logs
BKK_OFFSET = pd.Timedelta("7h")

# PID lock file to prevent double daemon
PID_FILE = STATE_DIR / "paper_trader.pid"


def _acquire_pid_lock():
    """Acquire PID lock. Raises RuntimeError if another instance is running."""
    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text().strip())
            # Check if process is still alive (Windows-compatible)
            import psutil
            if psutil.pid_exists(old_pid):
                proc = psutil.Process(old_pid)
                if proc.is_running() and "python" in proc.name().lower():
                    raise RuntimeError(
                        f"Another paper_trader is already running (PID {old_pid}). "
                        f"Kill it first or delete {PID_FILE}"
                    )
        except ImportError:
            # psutil not available, try OS-level check
            try:
                os.kill(old_pid, 0)  # signal 0 = check if alive
                raise RuntimeError(
                    f"Another paper_trader may be running (PID {old_pid}). "
                    f"Kill it first or delete {PID_FILE}"
                )
            except OSError:
                pass  # Process dead, stale PID file
        except (ValueError, OSError):
            pass  # Stale PID file, safe to overwrite

    PID_FILE.write_text(str(os.getpid()))
    atexit.register(_release_pid_lock)
    logger.info(f"PID lock acquired: {os.getpid()}")


def _release_pid_lock():
    """Release PID lock on exit."""
    try:
        if PID_FILE.exists():
            stored_pid = int(PID_FILE.read_text().strip())
            if stored_pid == os.getpid():
                PID_FILE.unlink()
    except Exception:
        pass

# Module-level exchange client (initialized once)
_exchange: TestnetExchange | None = None
# Module-level health monitor (initialized once)
_health_monitor: CoinHealthMonitor | None = None


def get_health_monitor() -> CoinHealthMonitor:
    """Get or create the health monitor singleton."""
    global _health_monitor
    if _health_monitor is None:
        _health_monitor = CoinHealthMonitor()
    return _health_monitor


def get_exchange() -> TestnetExchange:
    """Get or create the testnet exchange client."""
    global _exchange
    if _exchange is None:
        if not BINANCE_TESTNET_KEY or not BINANCE_TESTNET_SECRET:
            raise RuntimeError(
                "BINANCE_TESTNET_KEY / BINANCE_TESTNET_SECRET not set in .env. "
                "Get keys from https://testnet.binancefuture.com"
            )
        _exchange = TestnetExchange(BINANCE_TESTNET_KEY, BINANCE_TESTNET_SECRET)
        _exchange.ensure_one_way_mode()
    return _exchange


def setup_logging(debug: bool = False):
    """Setup console + rotating file logging."""
    logger.setLevel(logging.DEBUG if debug else logging.INFO)

    # Console handler (INFO)
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"
    ))
    logger.addHandler(ch)

    # File handler (DEBUG, 30 day retention)
    LOG_DIR.mkdir(exist_ok=True)
    fh = RotatingFileHandler(
        LOG_DIR / "paper_trader.log",
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=30,
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    ))
    logger.addHandler(fh)


def run_cycle():
    """
    Main trading cycle. Called every 15 minutes.
    1. Fetch data (BTC OHLCV + DB factors + alt OHLCVs)
    2. Compute BTC composite score
    3. Evaluate signals per coin
    4. Manage positions (check exits, open new)
    5. Log everything
    """
    cycle_start = datetime.now()
    logger.info(f"{'='*50}")
    logger.info(f"CYCLE START: {cycle_start.strftime('%Y-%m-%d %H:%M:%S BKK')}")

    # Increment bar count
    bar_count = int(db.get_meta("bar_count", "0")) + 1
    db.set_meta("bar_count", str(bar_count))
    logger.info(f"Bar #{bar_count}")

    # ---- Step 1: Fetch BTC OHLCV ----
    try:
        btc_ohlcv = fetch_recent_ohlcv("BTCUSDT", n_bars=WARMUP_BARS)
        latest_bkk = btc_ohlcv['date_time'].iloc[-1] + BKK_OFFSET
        logger.info(f"BTC OHLCV: {len(btc_ohlcv)} bars, latest={latest_bkk}")
    except Exception as e:
        logger.error(f"Failed to fetch BTC OHLCV: {e}")
        return

    # ---- Step 2: Fetch BTC DB data ----
    suppress_signals = False
    try:
        btc_db = load_btc_db_data_recent(hours=48)
        stale = check_data_staleness(btc_db)
        if stale:
            logger.warning(f"Stale data sources: {stale}")
        too_stale, stale_reason = is_data_too_stale(stale)
        if too_stale:
            suppress_signals = True
            logger.warning(f"SIGNAL SUPPRESSED: {stale_reason}")
    except Exception as e:
        logger.error(f"Failed to load BTC DB data: {e}")
        btc_db = {}
        suppress_signals = True

    # ---- Step 3: Build BTC features + composite scores (v3 + v5) ----
    try:
        btc_df = build_btc_features(btc_ohlcv, btc_db)
        # v3/v4 score (liq=2.0, tick=2.0)
        btc_score_v3 = compute_btc_composite_score(btc_df, COMPOSITE_WEIGHTS, V3_EXTRA_WEIGHTS)
        # v5 score (liq=5.0, tick=3.0)
        btc_score_v5 = compute_btc_composite_score(btc_df, V5_COMPOSITE_WEIGHTS, V5_EXTRA_WEIGHTS)
        # v6 score (liq-only: cascade 1.1x + tick net>3)
        btc_score_v6 = compute_btc_composite_score_v6(btc_df)
        latest_v3 = float(btc_score_v3.iloc[-1])
        latest_v5 = float(btc_score_v5.iloc[-1])
        latest_v6 = float(btc_score_v6.iloc[-1])
        latest_btc_score = latest_v3  # primary score for logging/equity snapshot
        logger.info(f"BTC Score v3: {latest_v3:.2f} | v5: {latest_v5:.2f} | v6: {latest_v6:.2f}")
    except Exception as e:
        logger.error(f"Failed to compute BTC score: {e}")
        return

    # ---- Step 3b: Extreme_Conf3 filter (Mission 013) ----
    vol_regime = "Unknown"
    active_factors = 0
    extreme_conf3_block = False
    if EXTREME_CONF3_ENABLED:
        try:
            vol_regime = compute_vol_regime(btc_df)
            active_factors = count_active_factors(btc_df, COMPOSITE_WEIGHTS, V3_EXTRA_WEIGHTS)
            extreme_conf3_block = (
                vol_regime == "Extreme" and active_factors < EXTREME_CONF3_MIN_FACTORS
            )
            if extreme_conf3_block:
                logger.warning(
                    f"EXTREME_CONF3: BLOCKING new entries "
                    f"(vol={vol_regime}, factors={active_factors}/{EXTREME_CONF3_MIN_FACTORS})"
                )
            else:
                logger.info(f"Vol regime: {vol_regime} | Active factors: {active_factors}")
        except Exception as e:
            logger.error(f"Extreme_conf3 check failed: {e}")
            # On error, don't block -- fail open

    # ---- Step 3c: Coin Health Monitor ----
    health_monitor = get_health_monitor()
    if HEALTH_ENABLED:
        try:
            health_monitor.update_all(COINS)
            paused_coins = health_monitor.get_paused_coins()
            if paused_coins:
                logger.info(
                    f"Health: {len(paused_coins)} paused: {', '.join(paused_coins)}"
                )
            else:
                logger.info("Health: all coins healthy")
        except Exception as e:
            logger.error(f"Health monitor update failed: {e}", exc_info=True)

    # ---- Step 4: Process each coin ----
    exchange = get_exchange()
    current_equity = exchange.get_account_balance()
    open_positions = exchange.get_open_positions()
    num_open = len(open_positions)
    open_symbols = {p["symbol"] for p in open_positions}

    logger.info(
        f"Equity=${current_equity:,.2f} | Open={num_open}/{len(COINS)} | "
        f"Budget/coin=${BUDGET_PER_COIN}"
    )

    # ---- Ghost position cleanup: close positions for coins not in COINS ----
    valid_symbols = {COIN_CONFIGS[c]["symbol"] for c in COINS}
    for pos in open_positions:
        sym = pos["symbol"]
        if sym not in valid_symbols:
            ghost_coin = sym.replace("USDT", "")
            pos_amt = float(pos["positionAmt"])
            if abs(pos_amt) > 0:
                logger.warning(f"[GHOST] Closing orphaned position: {sym} amt={pos_amt}")
                try:
                    side = "SELL" if pos_amt > 0 else "BUY"
                    exchange.client.futures_create_order(
                        symbol=sym, side=side, type="MARKET",
                        quantity=abs(pos_amt), reduceOnly=True,
                    )
                    # Clear signal state if exists
                    db.set_meta(f"signal_state_{ghost_coin}", "0")
                    logger.info(f"[GHOST] Closed {sym} successfully")
                except Exception as e:
                    logger.error(f"[GHOST] Failed to close {sym}: {e}")

    # ---- Stale algo order cleanup: cancel algo orders for coins WITHOUT position ----
    # Prevents orphan LONGs from stale SL/TP orders firing after position closed
    for coin in COINS:
        config = COIN_CONFIGS[coin]
        sym = config["symbol"]
        if sym not in open_symbols:
            try:
                algo_orders = exchange.get_open_algo_orders(sym)
                if algo_orders:
                    logger.warning(
                        f"[{coin}] STALE ALGO CLEANUP: {len(algo_orders)} orphan algo orders "
                        f"(no position) — cancelling"
                    )
                    exchange.cancel_all_orders(sym)
            except Exception as e:
                logger.debug(f"[{coin}] algo cleanup check failed: {e}")

    # Refresh after ghost cleanup
    open_positions = exchange.get_open_positions()
    num_open = len(open_positions)
    open_symbols = {p["symbol"] for p in open_positions}

    prices = {"btc_price": float(btc_ohlcv["close"].iloc[-1])}

    for coin in COINS:
        config = COIN_CONFIGS[coin]
        symbol = config["symbol"]

        try:
            # Fetch alt OHLCV (BTC uses same data)
            if coin == "BTC":
                alt_ohlcv = btc_ohlcv.copy()
            else:
                alt_ohlcv = fetch_recent_ohlcv(symbol, n_bars=WARMUP_BARS)

            if len(alt_ohlcv) < 30:
                logger.warning(f"[{coin}] Insufficient data ({len(alt_ohlcv)} bars), skipping")
                continue

            # Store price for equity snapshot
            prices[f"{coin.lower()}_price"] = float(alt_ohlcv["close"].iloc[-1])

            # Build alt technicals
            alt_df = build_alt_technicals(alt_ohlcv)

            # Select BTC score based on model version
            model_ver = config.get("model", "v3")
            if model_ver == "v6":
                btc_score = btc_score_v6
            elif model_ver == "v5":
                btc_score = btc_score_v5
            else:
                btc_score = btc_score_v3

            # Evaluate signal (with hysteresis: pass previous signal state)
            prev_signal = int(db.get_meta(f"signal_state_{coin}", "0"))
            sig_result = evaluate_signal(
                btc_df, btc_score, alt_df, coin,
                threshold=config["threshold"],
                use_alt_pa_filter=config["use_alt_pa_filter"],
                prev_signal=prev_signal,
                model=config.get("model", "v3"),
            )

            signal = sig_result["signal"]
            # Persist signal state for next cycle (hysteresis memory)
            db.set_meta(f"signal_state_{coin}", str(signal))
            has_position = symbol in open_symbols

            # Suppress NEW entries if data too stale (still manage existing positions)
            if suppress_signals and not has_position:
                signal = 0
                sig_result["reason"] = "DATA_TOO_STALE"

            # Dynamic sizing by total net PnL (added 2026-04-15)
            # Scales budget by cumulative closed-trade PnL. ARIA-tier up to 2.5x,
            # heavy losers down to 0.4x. Falls back to base budget if <10 trades.
            budget, _tot_pnl, _n = rolling_sharpe_multiplier(coin, BUDGET_PER_COIN)
            if _n >= 10:
                logger.info(
                    f"[{coin}] sizing: total_pnl=${_tot_pnl:+.0f} n={_n} "
                    f"budget=${budget:.0f} (base=${BUDGET_PER_COIN:.0f})"
                )

            # V6: cascade quality position sizing (Mission 014) -- layered on top
            size_mult = 1.0
            if model_ver == "v6" and signal != 0:
                # displacement = |price change| of latest BTC bar
                displacement = abs(
                    btc_df["close"].iloc[-1] / btc_df["close"].iloc[-2] - 1
                )
                # cascade magnitude = liq_total / liq_total_ma
                lt = btc_df["liq_total"].iloc[-1] if "liq_total" in btc_df.columns else 0
                lt_ma = btc_df["liq_total_ma"].iloc[-1] if "liq_total_ma" in btc_df.columns else 1
                cascade_mag = lt / lt_ma if lt_ma > 0 else 0

                if displacement >= 0.003:
                    size_mult = V6_SIZE_MULT_DISP_03       # 1.5
                elif displacement >= 0.001:
                    size_mult = V6_SIZE_MULT_DISP_01       # 1.2
                if cascade_mag >= 5.0:
                    size_mult += V6_SIZE_MULT_CASCADE_5X   # +0.3
                size_mult = min(size_mult, V6_SIZE_MULT_MAX)  # cap 2.0
                budget = budget * size_mult  # layer v6 cascade mult on dynamic sizing

                logger.debug(
                    f"[{coin}] v6 sizing: disp={displacement:.4f} "
                    f"cascade={cascade_mag:.1f}x size_mult={size_mult:.1f} "
                    f"budget=${budget:.0f}"
                )

            pm = PositionManager(coin, config, exchange, budget)
            cooldown_ok = pm._cooldown_ok()

            # Health guard check (block only if HEALTH_BLOCK_ENABLED)
            health_paused = False
            health_reason = ""
            if HEALTH_ENABLED and HEALTH_BLOCK_ENABLED and not has_position:
                health_paused_ok, health_reason = health_monitor.should_trade(coin)
                health_paused = not health_paused_ok

            # Determine action
            model_tag = config.get("model", "v3")
            if has_position:
                action = "MANAGE_POSITION"
            elif suppress_signals:
                action = "SKIP_DATA_STALE"
            elif extreme_conf3_block and signal != 0 and model_tag != "v6":
                action = "SKIP_EXTREME_CONF3"
                signal = 0  # suppress entry (v6 is self-cleaning, doesn't need this)
            elif health_paused and signal != 0:
                action = "SKIP_HEALTH_PAUSED"
                # Don't zero signal -- shadow log keeps original signal
            elif signal == 1 and not LONG_ENABLED:
                action = "SKIP_LONG_DISABLED"
                signal = 0
            elif signal != 0 and cooldown_ok:
                # Alt data entry filter (FR + CVD + Liq)
                alt_check = check_entry_filter(coin, signal)
                if not alt_check["allow"]:
                    action = "SKIP_ALT_FILTER"
                    logger.info(f"[{coin}] {alt_check['reason']} | "
                                f"details={alt_check['details']}")
                    signal = 0
                else:
                    action = "OPEN_LONG" if signal == 1 else "OPEN_SHORT"
            elif signal != 0 and not cooldown_ok:
                action = "SKIP_COOLDOWN"
            elif signal == 0 and sig_result.get("pa_aligned") == 0:
                action = "SKIP_PA"
            else:
                action = "NO_SIGNAL"

            # Log signal (UTC to match position_manager timestamps)
            now_str = datetime.utcnow().isoformat()
            db.insert_signal_log({
                "ts": now_str,
                "coin": coin,
                "btc_score": sig_result["btc_score"],
                "signal": signal,
                "pa_aligned": sig_result.get("pa_aligned"),
                "has_position": int(has_position),
                "cooldown_ok": int(cooldown_ok),
                "action": action,
                "model": model_tag,
            })

            # Build candle dict for position manager
            latest = alt_df.iloc[-1]
            candle = {
                "ts": latest["ts"],
                "open": latest["open"],
                "high": latest["high"],
                "low": latest["low"],
                "close": latest["close"],
                "atr": latest["atr"],
            }

            # Tick position manager (suppress signal if health paused, but still manage existing)
            effective_signal = 0 if action == "SKIP_HEALTH_PAUSED" else signal
            trade_result = pm.tick(candle, effective_signal, latest_btc_score, current_equity)

            if trade_result:
                current_equity = exchange.get_account_balance()

            # Update open count after potential position changes
            if action in ("OPEN_LONG", "OPEN_SHORT"):
                num_open += 1
                open_symbols.add(symbol)

            # Health score in log (if available)
            h = health_monitor.get_health(coin) if HEALTH_ENABLED else None
            health_tag = f" HP={h.health_score:.0f}/{h.status}" if h else ""
            logger.info(
                f"[{coin}] Score={sig_result['btc_score']:.2f} Signal={signal} "
                f"PA={sig_result.get('pa_aligned')} Action={action}{health_tag}"
            )

        except Exception as e:
            logger.error(f"[{coin}] Error: {e}", exc_info=True)

    # ---- Step 5: Equity snapshot ----
    # Re-fetch balance after all trades processed
    current_equity = exchange.get_account_balance()
    open_positions = exchange.get_open_positions()
    open_coins = [p["symbol"].replace("USDT", "") for p in open_positions]
    db.insert_equity_snapshot(
        ts=datetime.utcnow().isoformat(),
        equity=current_equity,
        btc_price=prices.get("btc_price"),
        xrp_price=prices.get("xrp_price"),
        ada_price=prices.get("ada_price"),
        btc_score=latest_btc_score,
        open_positions=open_coins if open_coins else None,
    )

    db.set_meta("last_run_time", datetime.utcnow().isoformat())

    elapsed = (datetime.now() - cycle_start).total_seconds()
    logger.info(
        f"CYCLE DONE in {elapsed:.1f}s | Equity=${current_equity:,.2f} | "
        f"Open={len(open_positions)} | Score={latest_btc_score:.2f}"
    )


def run_reconcile_pnl():
    """Reconcile trades with pnl=0 against Binance API (source of truth).

    Finds trades where pnl_gross=0 (API failed at close time),
    looks up the actual PnL from Binance account trades, and patches the record.
    """
    try:
        zero_trades = db.get_zero_pnl_trades()
        if not zero_trades:
            return

        exchange = get_exchange()
        patched = 0

        for trade in zero_trades:
            trade_id = trade["id"]
            coin = trade["coin"]
            symbol = COIN_CONFIGS.get(coin, {}).get("symbol")
            if not symbol:
                continue

            order_id = trade.get("order_id")
            direction = trade["direction"]

            try:
                # Try matching by order_id first
                binance_trades = exchange.get_trades(symbol, limit=20)
                if order_id:
                    matching = [
                        t for t in binance_trades
                        if t.get("orderId") == int(order_id)
                    ]
                else:
                    # Fallback: match by side + time proximity
                    exit_side = "SELL" if direction == 1 else "BUY"
                    matching = [
                        t for t in binance_trades
                        if t.get("side") == exit_side
                    ]

                if not matching:
                    continue

                pnl = sum(float(t.get("realizedPnl", 0)) for t in matching)
                fee = sum(abs(float(t.get("commission", 0))) for t in matching)
                total_qty = sum(float(t.get("qty", 0)) for t in matching)
                if total_qty > 0:
                    exit_price = sum(
                        float(t.get("price", 0)) * float(t.get("qty", 0))
                        for t in matching
                    ) / total_qty
                else:
                    exit_price = 0.0

                if pnl == 0 and fee == 0:
                    continue  # Still no data from Binance

                db.update_trade_pnl(trade_id, pnl, pnl - fee, fee, exit_price)
                patched += 1
                logger.info(
                    f"[RECONCILE] {coin} trade#{trade_id}: "
                    f"pnl=${pnl:+.4f} fee=${fee:.4f} exit={exit_price:.4f}"
                )
            except Exception as e:
                logger.debug(f"[RECONCILE] {coin} trade#{trade_id} failed: {e}")

        if patched:
            logger.info(f"[RECONCILE] Patched {patched}/{len(zero_trades)} trades")

    except Exception as e:
        logger.error(f"Reconcile PnL failed: {e}", exc_info=True)


def run_daily_report_job():
    """Generate daily report (scheduled at 00:05 BKK)."""
    try:
        path = generate_daily_report()
        logger.info(f"Daily report generated: {path}")
    except Exception as e:
        logger.error(f"Daily report failed: {e}", exc_info=True)


def run_daemon():
    """Run as APScheduler daemon."""
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger

    scheduler = BlockingScheduler()

    # Trading cycle: every 15 min at :01, :16, :31, :46
    scheduler.add_job(
        run_cycle,
        CronTrigger(minute="1,16,31,46"),
        id="trading_cycle",
        name="Trading Cycle (15m)",
        misfire_grace_time=120,
    )

    # Reconcile PnL: every hour at :30 (patch trades where API failed at close)
    scheduler.add_job(
        run_reconcile_pnl,
        CronTrigger(minute=30),
        id="reconcile_pnl",
        name="Reconcile PnL (hourly)",
        misfire_grace_time=300,
    )

    # Daily report at 00:05 local time (BKK)
    scheduler.add_job(
        run_daily_report_job,
        CronTrigger(hour=0, minute=5),
        id="daily_report",
        name="Daily Report",
        misfire_grace_time=300,
    )

    # Graceful shutdown
    def shutdown(signum, frame):
        logger.info("Shutdown signal received, stopping scheduler...")
        scheduler.shutdown(wait=False)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    logger.info("=" * 60)
    logger.info("Paper Trading Daemon Started (Binance Testnet)")
    logger.info(f"Coins: {', '.join(COINS)}")
    logger.info(f"Schedule: every 15min at :01,:16,:31,:46")
    try:
        exchange = get_exchange()
        logger.info(f"Testnet Balance: ${exchange.get_account_balance():,.2f}")
    except Exception as e:
        logger.error(f"Failed to connect to testnet: {e}")
        return
    logger.info("=" * 60)

    # Run initial cycle immediately
    logger.info("Running initial cycle...")
    run_cycle()

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Daemon stopped.")


def main():
    parser = argparse.ArgumentParser(description="Paper Trading System -- BTC-Led Strategy")
    parser.add_argument("--once", action="store_true", help="Run single cycle and exit")
    parser.add_argument("--report", action="store_true", help="Generate daily report and exit")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    setup_logging(debug=args.debug)

    # Init DB
    db.init_db()
    db.set_meta("init_equity", str(INIT_EQUITY))
    logger.info(f"SQLite DB initialized at {db.SQLITE_PATH}")

    if args.report:
        path = generate_daily_report()
        print(f"Report saved: {path}")
    elif args.once:
        logger.info("Single cycle mode (--once)")
        run_cycle()
        logger.info("Done.")
    else:
        # Acquire PID lock before daemon mode (prevent double daemon)
        _acquire_pid_lock()
        run_daemon()


if __name__ == "__main__":
    main()
