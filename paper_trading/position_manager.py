"""
Position Manager -- Exchange Execution
========================================
Manages real positions on Binance Futures Testnet.
SL/TP are exchange-managed STOP_MARKET / TAKE_PROFIT_MARKET orders.
Timeout and signal-flip exits are market orders from this code.
"""

import json
import logging
from datetime import datetime

import numpy as np
import pandas as pd

from .config import LEVERAGE, MAX_HOLD_BARS, MIN_BARS_BEFORE_FLIP, FLIP_MODE, FLIP_COOLDOWN_EXTRA, FLIP_CONFIG, LONG_ENABLED
from .exchange import TestnetExchange
from . import state_db as db

logger = logging.getLogger(__name__)


class PositionManager:
    """Manages one coin's position on Binance Testnet."""

    def __init__(self, coin: str, config: dict, exchange: TestnetExchange,
                 budget_usdt: float = 1000.0):
        self.coin = coin
        self.config = config
        self.symbol = config["symbol"]
        self.exchange = exchange
        self.budget_usdt = budget_usdt
        self.sl_atr_mult = config["sl_atr_mult"]
        self.tp_atr_mult = config["tp_atr_mult"]
        self.cooldown_bars = config["cooldown_bars"]
        # Trailing stop config (matching backtest logic)
        self.trail_atr_mult = config.get("trail_atr_mult", 99)
        self.trail_activate_atr = config.get("trail_activate_atr", 99)
        self.trail_enabled = self.trail_atr_mult < 50  # 50+ = disabled
        # Per-model flip config (Tournament R3)
        self.model = config.get("model", "v3")
        fc = FLIP_CONFIG.get(self.model, {})
        self.flip_mode = fc.get("flip_mode", FLIP_MODE)
        self.min_bars_flip = fc.get("min_bars", MIN_BARS_BEFORE_FLIP)
        self.flip_cd_extra = fc.get("cd_extra", FLIP_COOLDOWN_EXTRA)

    def tick(self, candle: dict, signal: int, btc_score: float,
             current_equity: float) -> dict | None:
        """
        Process one bar for this coin.

        candle: dict with open, high, low, close, atr, ts
        signal: -1, 0, or 1
        current_equity: current account balance (from exchange)

        Returns trade record dict if a trade was closed this cycle, else None.
        """
        pos = self.exchange.get_position(self.symbol)
        has_position = pos is not None
        trade_result = None

        if has_position:
            # Sync: if exchange has position but state_meta is missing, populate it
            entry_time = db.get_meta(f"entry_time_{self.coin}")
            if not entry_time:
                self._sync_state_from_exchange(pos, btc_score, candle)

            # Force-close LONG positions when LONG_ENABLED=False
            # (catches orphan positions from stale algo orders or pre-disable opens)
            pos_amt = float(pos["positionAmt"])
            if pos_amt > 0 and not LONG_ENABLED:
                direction = 1
                entry_price = float(pos["entryPrice"])
                qty = abs(pos_amt)
                bars_held = self._get_bars_held()
                logger.warning(
                    f"[{self.coin}] FORCE CLOSE orphan LONG (LONG_ENABLED=False) | "
                    f"qty={qty} entry={entry_price:.4f}"
                )
                trade_result = self._close_position(
                    direction, entry_price, qty,
                    "LONG_DISABLED", btc_score, current_equity, bars_held
                )
            else:
                trade_result = self._manage_position(pos, candle, signal, btc_score, current_equity)
        else:
            # Check if position was closed by SL/TP since last cycle
            self._detect_sl_tp_close(btc_score, current_equity)

            if signal != 0 and self._cooldown_ok():
                if signal == 1 and not LONG_ENABLED:
                    logger.debug(f"[{self.coin}] PM: LONG blocked (LONG_ENABLED=False)")
                else:
                    self._open_position(candle, signal, btc_score)

        return trade_result

    def _manage_position(self, pos: dict, candle: dict, signal: int,
                         btc_score: float, current_equity: float) -> dict | None:
        """Manage an existing exchange position. Check SL/TP, timeout, signal flip."""
        pos_amt = float(pos["positionAmt"])
        direction = 1 if pos_amt > 0 else -1
        entry_price = float(pos["entryPrice"])
        qty = abs(pos_amt)

        # Calculate bars held from entry time stored in state_meta
        bars_held = self._get_bars_held()

        # Software SL/TP check (for positions where exchange algo orders failed)
        sl_result = self._check_software_sl_tp(candle, direction, entry_price, qty,
                                                btc_score, current_equity, bars_held)
        if sl_result:
            return sl_result

        # Trailing stop check (matching backtest: activate after profit >= trail_activate_atr * ATR)
        trail_result = self._check_trailing_stop(candle, direction, entry_price, qty,
                                                  btc_score, current_equity, bars_held)
        if trail_result:
            return trail_result

        # Timeout exit
        if bars_held >= MAX_HOLD_BARS:
            logger.info(f"[{self.coin}] TIMEOUT after {bars_held} bars, closing position")
            return self._close_position(direction, entry_price, qty,
                                        "TIMEOUT", btc_score, current_equity, bars_held)

        # Signal flip exit (Tournament R3: per-model flip config)
        if signal != 0 and signal != direction:
            if self.flip_mode == "disabled":
                logger.debug(f"[{self.coin}] SIGNAL_FLIP ignored (flip_mode=disabled)")
            elif bars_held < self.min_bars_flip:
                logger.debug(
                    f"[{self.coin}] SIGNAL_FLIP blocked: bars_held={bars_held} "
                    f"< min={self.min_bars_flip}"
                )
            else:
                logger.info(f"[{self.coin}] SIGNAL_FLIP dir={direction} -> signal={signal} "
                            f"(held {bars_held} bars, model={self.model}, mode={self.flip_mode})")
                trade = self._close_position(direction, entry_price, qty,
                                             "SIGNAL_FLIP", btc_score, current_equity, bars_held)
                if trade and self.flip_mode == "reverse":
                    if signal == 1 and not LONG_ENABLED:
                        logger.info(f"[{self.coin}] FLIP reverse blocked: LONG_ENABLED=False")
                    else:
                        self._open_position(candle, signal, btc_score)
                elif trade and self.flip_mode == "exit_only" and self.flip_cd_extra > 0:
                    db.set_meta(f"flip_cooldown_{self.coin}",
                                str(bars_held + self.flip_cd_extra))
                    logger.info(f"[{self.coin}] flip cooldown set: +{self.flip_cd_extra} bars")
                return trade

        # Update bars held counter
        self._increment_bars_held()
        return None

    def _check_software_sl_tp(self, candle: dict, direction: int,
                               entry_price: float, qty: float,
                               btc_score: float, current_equity: float,
                               bars_held: int) -> dict | None:
        """Software-managed SL/TP for positions without exchange algo orders.

        Checks current price against stored SL/TP levels. Only triggers
        if the SL or TP was NOT placed on the exchange (sl_exchange=0).
        """
        sl_ex_val = db.get_meta(f"sl_exchange_{self.coin}", "1")
        tp_ex_val = db.get_meta(f"tp_exchange_{self.coin}", "1")
        sl_on_exchange = sl_ex_val == "1"
        tp_on_exchange = tp_ex_val == "1"

        # If both flags are empty/missing, skip software check (synced positions)
        if (not sl_ex_val and not tp_ex_val) or (sl_on_exchange and tp_on_exchange):
            return None

        def _safe_float(val, default=0.0):
            try:
                return float(val) if val else default
            except (ValueError, TypeError):
                return default

        sl_price = _safe_float(db.get_meta(f"sl_price_{self.coin}", "0"))
        tp_price = _safe_float(db.get_meta(f"tp_price_{self.coin}", "0"))

        if sl_price <= 0 and tp_price <= 0:
            return None

        current_price = candle["close"]

        # Check SL hit (software)
        if not sl_on_exchange and sl_price > 0:
            if (direction == 1 and current_price <= sl_price) or \
               (direction == -1 and current_price >= sl_price):
                logger.info(
                    f"[{self.coin}] SOFTWARE SL HIT | price={current_price:.4f} "
                    f"sl={sl_price:.4f}"
                )
                return self._close_position(direction, entry_price, qty,
                                            "SL", btc_score, current_equity, bars_held)

        # Check TP hit (software)
        if not tp_on_exchange and tp_price > 0:
            if (direction == 1 and current_price >= tp_price) or \
               (direction == -1 and current_price <= tp_price):
                logger.info(
                    f"[{self.coin}] SOFTWARE TP HIT | price={current_price:.4f} "
                    f"tp={tp_price:.4f}"
                )
                return self._close_position(direction, entry_price, qty,
                                            "TP", btc_score, current_equity, bars_held)

        return None

    def _check_trailing_stop(self, candle: dict, direction: int,
                              entry_price: float, qty: float,
                              btc_score: float, current_equity: float,
                              bars_held: int) -> dict | None:
        """Trailing stop — matches backtest logic exactly.

        Tracks peak (LONG) or trough (SHORT) in state_db.
        Activates when unrealized profit >= trail_activate_atr * ATR.
        Once active, exits when price retraces trail_atr_mult * ATR from peak/trough.
        """
        if not self.trail_enabled:
            return None

        entry_atr = float(db.get_meta(f"entry_atr_{self.coin}", "0") or "0")
        if entry_atr <= 0:
            return None

        high = candle["high"]
        low = candle["low"]

        # Load or initialize peak/trough from state_db
        if direction == 1:  # LONG
            stored_peak = float(db.get_meta(f"trail_peak_{self.coin}", "0") or "0")
            peak = max(stored_peak, high) if stored_peak > 0 else high
            db.set_meta(f"trail_peak_{self.coin}", str(peak))

            # Check activation: profit from entry >= trail_activate_atr * ATR
            if (peak - entry_price) >= self.trail_activate_atr * entry_atr:
                trail_stop = peak - self.trail_atr_mult * entry_atr
                if low <= trail_stop:
                    logger.info(
                        f"[{self.coin}] TRAIL EXIT (LONG) | peak={peak:.4f} "
                        f"trail_stop={trail_stop:.4f} low={low:.4f} | "
                        f"entry={entry_price:.4f} ATR={entry_atr:.4f}"
                    )
                    return self._close_position(
                        direction, entry_price, qty,
                        "TRAIL", btc_score, current_equity, bars_held
                    )
        else:  # SHORT
            stored_trough = float(db.get_meta(f"trail_trough_{self.coin}", "0") or "0")
            trough = min(stored_trough, low) if stored_trough > 0 else low
            db.set_meta(f"trail_trough_{self.coin}", str(trough))

            # Check activation: profit from entry >= trail_activate_atr * ATR
            if (entry_price - trough) >= self.trail_activate_atr * entry_atr:
                trail_stop = trough + self.trail_atr_mult * entry_atr
                if high >= trail_stop:
                    logger.info(
                        f"[{self.coin}] TRAIL EXIT (SHORT) | trough={trough:.4f} "
                        f"trail_stop={trail_stop:.4f} high={high:.4f} | "
                        f"entry={entry_price:.4f} ATR={entry_atr:.4f}"
                    )
                    return self._close_position(
                        direction, entry_price, qty,
                        "TRAIL", btc_score, current_equity, bars_held
                    )

        return None

    def _close_position(self, direction: int, entry_price: float, qty: float,
                        reason: str, btc_score: float, current_equity: float,
                        bars_held: int) -> dict | None:
        """Close position via market order."""
        try:
            # Cancel SL/TP orders first
            self.exchange.cancel_all_orders(self.symbol)

            # Market close (reduceOnly to bypass min notional for small positions)
            close_side = "SELL" if direction == 1 else "BUY"
            order = self.exchange.place_market_order(self.symbol, close_side, qty,
                                                     reduce_only=True)

            # Get PnL from Binance (source of truth)
            order_id = order.get("orderId")
            pnl_gross, commission, api_exit_price = self._get_pnl_from_trades(order_id)
            exit_price = api_exit_price if api_exit_price > 0 else float(order.get("avgPrice", 0))

            # Sanity check: API pnl vs price-based calculation
            if exit_price > 0 and entry_price > 0 and qty > 0:
                calc_pnl = (exit_price - entry_price) * qty * direction
                if abs(pnl_gross - calc_pnl) > abs(calc_pnl) * 0.5 + 1.0:
                    logger.warning(
                        f"[{self.coin}] PnL sanity check FAILED: "
                        f"api={pnl_gross:+.4f} calc={calc_pnl:+.4f} — using calc"
                    )
                    pnl_gross = calc_pnl

            # Include entry commission in total fee
            entry_commission = float(db.get_meta(f"entry_commission_{self.coin}", "0") or "0")
            total_fee = commission + entry_commission
            pnl_net = pnl_gross - total_fee

            now_str = datetime.utcnow().isoformat()
            entry_time = db.get_meta(f"entry_time_{self.coin}", now_str)
            entry_btc_score = float(db.get_meta(f"entry_btc_score_{self.coin}", "0"))

            trade = {
                "coin": self.coin,
                "direction": direction,
                "entry_time": entry_time,
                "entry_price": entry_price,
                "exit_time": now_str,
                "exit_price": exit_price,
                "qty": qty,
                "pnl_gross": round(pnl_gross, 4),
                "pnl_net": round(pnl_net, 4),
                "fee_total": round(total_fee, 4),
                "exit_reason": reason,
                "btc_score_entry": entry_btc_score,
                "btc_score_exit": btc_score,
                "equity_after": round(current_equity + pnl_net, 2),
                "bars_held": bars_held,
            }

            db.insert_trade(trade)

            # Save exchange trade backup
            self._save_exchange_trade(order, reason)

            # Reset state
            db.set_meta(f"last_exit_bar_{self.coin}", db.get_meta("bar_count", "0"))
            db.set_meta(f"bars_held_{self.coin}", "0")
            self._clear_entry_meta()

            dir_str = "LONG" if direction == 1 else "SHORT"
            logger.info(
                f"[{self.coin}] CLOSE {dir_str} | {reason} | "
                f"PnL=${pnl_net:+.2f} | Entry={entry_price:.4f} Exit={exit_price:.4f} | "
                f"Bars={bars_held} | Fee=${commission:.4f}"
            )
            return trade

        except Exception as e:
            logger.error(f"[{self.coin}] Failed to close position: {e}", exc_info=True)
            return None

    # Max algo orders Binance testnet allows (account-wide).
    # Reserve 2 slots for the new SL + TP.
    MAX_ALGO_ORDERS = 10

    def _open_position(self, candle: dict, signal: int, btc_score: float):
        """Open a new position with SL/TP orders on the exchange.

        Pre-checks algo order capacity BEFORE placing market entry to avoid
        orphaned positions when SL/TP placement fails (-4045).
        """
        price = candle["close"]
        atr = candle.get("atr", 0)

        if price <= 0 or atr <= 0 or np.isnan(atr):
            logger.warning(f"[{self.coin}] Skip entry: invalid price={price} or ATR={atr}")
            return

        if atr / price < 0.0005:
            logger.warning(f"[{self.coin}] Skip entry: ATR too small ({atr/price:.6f})")
            return

        # Pre-check: ensure algo order capacity before placing market entry
        algo_count = self.exchange.get_open_algo_order_count()
        if algo_count >= 0 and algo_count + 2 > self.MAX_ALGO_ORDERS:
            logger.warning(
                f"[{self.coin}] Skip entry: algo order limit "
                f"({algo_count}/{self.MAX_ALGO_ORDERS}), need 2 slots"
            )
            return

        # Cancel any stale orders for this symbol before entry
        try:
            self.exchange.cancel_all_orders(self.symbol)
        except Exception:
            pass

        order = None
        qty = 0
        side = "BUY" if signal == 1 else "SELL"

        try:
            # 1. Set leverage
            self.exchange.set_leverage(self.symbol, int(LEVERAGE))

            # 2. Calculate qty (dynamic budget from equity / max_concurrent)
            notional = self.budget_usdt * LEVERAGE
            raw_qty = notional / price
            qty = self.exchange.round_qty(self.symbol, raw_qty)
            if qty <= 0:
                logger.warning(f"[{self.coin}] Skip: qty rounds to 0 (raw={raw_qty})")
                return

            # 3. Place market entry
            order = self.exchange.place_market_order(self.symbol, side, qty)

            # Get fill price (exchange.py now polls until filled)
            fill_price = float(order.get("avgPrice", 0))
            if fill_price == 0:
                fill_price = price  # absolute last resort

            # 4. Calculate SL/TP levels
            if signal == 1:
                sl_price = fill_price - self.sl_atr_mult * atr
                tp_price = fill_price + self.tp_atr_mult * atr
                sl_side = "SELL"
            else:
                sl_price = fill_price + self.sl_atr_mult * atr
                tp_price = fill_price - self.tp_atr_mult * atr
                sl_side = "BUY"

            # 5. Place SL order (with retry after cancel if -4045)
            sl_resp = self._place_algo_with_retry(
                "SL", self.exchange.place_stop_loss, sl_side, qty, sl_price
            )
            sl_placed = sl_resp is not None

            # 6. Place TP order
            tp_resp = self._place_algo_with_retry(
                "TP", self.exchange.place_take_profit, sl_side, qty, tp_price
            )
            tp_placed = tp_resp is not None

            # If SL/TP failed, manage them in software instead of rolling back
            if not sl_placed or not tp_placed:
                logger.warning(
                    f"[{self.coin}] SL/TP placement failed "
                    f"(SL={'OK' if sl_placed else 'FAIL'}, "
                    f"TP={'OK' if tp_placed else 'FAIL'}). "
                    f"Will manage SL/TP in software."
                )

            # 7. Save state AFTER market entry succeeds (even if SL/TP failed)
            now_str = datetime.utcnow().isoformat()
            db.set_meta(f"entry_time_{self.coin}", now_str)
            db.set_meta(f"entry_btc_score_{self.coin}", str(btc_score))
            db.set_meta(f"entry_atr_{self.coin}", str(atr))
            db.set_meta(f"entry_direction_{self.coin}", str(signal))
            db.set_meta(f"entry_price_{self.coin}", str(fill_price))
            db.set_meta(f"entry_qty_{self.coin}", str(qty))
            db.set_meta(f"sl_price_{self.coin}", str(sl_price))
            db.set_meta(f"tp_price_{self.coin}", str(tp_price))
            db.set_meta(f"bars_held_{self.coin}", "0")
            db.set_meta(f"sl_exchange_{self.coin}", "1" if sl_placed else "0")
            db.set_meta(f"tp_exchange_{self.coin}", "1" if tp_placed else "0")
            # Save algo order IDs for exit reason detection
            sl_algo_id = str(sl_resp.get("algoId", "")) if sl_resp else ""
            tp_algo_id = str(tp_resp.get("algoId", "")) if tp_resp else ""
            db.set_meta(f"sl_algo_id_{self.coin}", sl_algo_id)
            db.set_meta(f"tp_algo_id_{self.coin}", tp_algo_id)

            # Get entry commission from fills
            entry_commission = 0.0
            try:
                order_id = order.get("orderId")
                if order_id:
                    entry_fills = self.exchange.get_trades(self.symbol, limit=10)
                    matched = [t for t in entry_fills
                               if t.get("orderId") == order_id]
                    entry_commission = sum(
                        abs(float(t.get("commission", 0))) for t in matched
                    )
            except Exception as e:
                logger.debug(f"[{self.coin}] Failed to get entry commission: {e}")
            db.set_meta(f"entry_commission_{self.coin}", str(entry_commission))

            # Save exchange trade backup
            self._save_exchange_trade(order, "ENTRY")

            dir_str = "LONG" if signal == 1 else "SHORT"
            logger.info(
                f"[{self.coin}] OPEN {dir_str} | Price={fill_price:.4f} | "
                f"ATR={atr:.4f} | SL={sl_price:.4f} TP={tp_price:.4f} | "
                f"BTC Score={btc_score:.2f} | Qty={qty} | "
                f"EntryFee=${entry_commission:.4f}"
            )

        except Exception as e:
            logger.error(f"[{self.coin}] Failed to open position: {e}", exc_info=True)
            # Clean up: cancel any partial orders
            try:
                self.exchange.cancel_all_orders(self.symbol)
            except Exception:
                pass
            # Close orphaned position if market order succeeded but entry flow failed
            if order is not None:
                try:
                    close_side = "SELL" if side == "BUY" else "BUY"
                    self.exchange.place_market_order(self.symbol, close_side, qty,
                                                     reduce_only=True)
                    logger.info(f"[{self.coin}] Rolled back orphaned position (qty={qty})")
                except Exception as e2:
                    logger.error(f"[{self.coin}] CRITICAL: Failed to rollback orphaned position: {e2}")
            # Clear any partial state_meta (should not exist, but safety net)
            self._clear_entry_meta()
            db.set_meta(f"bars_held_{self.coin}", "0")

    def _place_algo_with_retry(self, label: str, place_fn, side: str,
                                qty: float, price: float) -> dict | None:
        """Try to place an algo order. On -4045, cancel stale orders and retry once.

        Returns order response dict (with algoId) on success, None on failure.
        """
        from binance.exceptions import BinanceAPIException
        try:
            return place_fn(self.symbol, side, qty, price)
        except BinanceAPIException as e:
            if e.code == -4045:
                logger.warning(f"[{self.coin}] {label} hit algo limit, retrying after cancel")
                try:
                    self.exchange.cancel_all_orders(self.symbol)
                    return place_fn(self.symbol, side, qty, price)
                except Exception:
                    pass
            else:
                logger.error(f"[{self.coin}] {label} placement error: {e}")
        except Exception as e:
            logger.error(f"[{self.coin}] {label} placement error: {e}")
        return None

    def _detect_sl_tp_close(self, btc_score: float, current_equity: float):
        """Check if a position was closed by SL/TP between cycles.

        When exchange executes SL/TP, position disappears. We detect this
        by checking if we had entry metadata but no position.
        """
        entry_time = db.get_meta(f"entry_time_{self.coin}")
        if not entry_time:
            return  # No previous entry -- nothing to detect

        # Retrieve saved entry data (handle empty strings from _clear_entry_meta)
        def _safe_parse(val, default, cast=float):
            try:
                return cast(val) if val else cast(default)
            except (ValueError, TypeError):
                return cast(default)

        direction = _safe_parse(db.get_meta(f"entry_direction_{self.coin}", "0"), "0", int)
        entry_price = _safe_parse(db.get_meta(f"entry_price_{self.coin}", "0"), "0")
        qty = _safe_parse(db.get_meta(f"entry_qty_{self.coin}", "0"), "0")
        bars_held = self._get_bars_held()

        # Guard: skip phantom trade if metadata is invalid (from failed _open_position)
        if entry_price <= 0 or qty <= 0 or direction == 0:
            logger.warning(
                f"[{self.coin}] Clearing invalid entry metadata "
                f"(price={entry_price}, qty={qty}, dir={direction})"
            )
            self._clear_entry_meta()
            db.set_meta(f"bars_held_{self.coin}", "0")
            return

        # Get PnL + exit price from Binance trades API (source of truth)
        exit_price = 0.0
        recent_pnl = 0.0
        recent_fee = 0.0
        try:
            trades = self.exchange.get_trades(self.symbol, limit=10)
            if trades:
                exit_side = "SELL" if direction == 1 else "BUY"
                entry_ts_ms = int(pd.Timestamp(entry_time).timestamp() * 1000)
                matching = [
                    t for t in trades
                    if t.get("side") == exit_side
                    and int(t.get("time", 0)) >= entry_ts_ms
                ]
                if matching:
                    # Group by orderId — use only the LATEST order (the SL/TP fill)
                    latest_order_id = max(
                        matching, key=lambda t: int(t.get("time", 0))
                    ).get("orderId")
                    fills = [t for t in matching
                             if t.get("orderId") == latest_order_id]
                    recent_pnl = sum(float(t.get("realizedPnl", 0)) for t in fills)
                    recent_fee = sum(abs(float(t.get("commission", 0))) for t in fills)
                    total_qty = sum(float(t.get("qty", 0)) for t in fills)
                    if total_qty > 0:
                        exit_price = sum(
                            float(t.get("price", 0)) * float(t.get("qty", 0))
                            for t in fills
                        ) / total_qty
                    else:
                        exit_price = float(fills[-1].get("price", 0))
                    logger.info(
                        f"[{self.coin}] SL/TP Binance PnL (trades API): "
                        f"pnl=${recent_pnl:+.4f} fee=${recent_fee:.4f} "
                        f"exit={exit_price:.4f} ({len(fills)} fills, "
                        f"orderId={latest_order_id})"
                    )
        except Exception as e:
            logger.debug(f"[{self.coin}] Failed to get exit trades: {e}")

        # Fallback: calculate PnL from prices if trades API didn't match
        if recent_pnl == 0.0 and exit_price == 0.0:
            try:
                income = self.exchange.get_income(
                    symbol=self.symbol, income_type="REALIZED_PNL", limit=5
                )
                if income:
                    # Use only the LATEST entry, not sum of multiple
                    recent_pnl = float(income[-1].get("income", 0))
                commission_income = self.exchange.get_income(
                    symbol=self.symbol, income_type="COMMISSION", limit=5
                )
                if commission_income:
                    # Use only the LATEST commission entry
                    recent_fee = abs(float(commission_income[-1].get("income", 0)))
            except Exception as e:
                logger.warning(
                    f"[{self.coin}] Both trades and income API failed: {e}. "
                    f"Recording pnl=0 (will be corrected by dashboard)."
                )
                recent_pnl = 0.0
                recent_fee = 0.0

        # Determine exit reason by checking which algo order is still open
        # If SL algoId is still open → TP was filled (and vice versa)
        exit_reason = "SL/TP"  # fallback
        sl_algo_id = db.get_meta(f"sl_algo_id_{self.coin}", "")
        tp_algo_id = db.get_meta(f"tp_algo_id_{self.coin}", "")
        try:
            if sl_algo_id or tp_algo_id:
                open_algos = self.exchange.get_open_algo_orders(self.symbol)
                open_ids = {str(o.get("algoId", "")) for o in open_algos}
                sl_open = sl_algo_id in open_ids if sl_algo_id else False
                tp_open = tp_algo_id in open_ids if tp_algo_id else False
                if sl_open and not tp_open:
                    exit_reason = "TP"   # TP filled, SL still open
                elif tp_open and not sl_open:
                    exit_reason = "SL"   # SL filled, TP still open
                elif not sl_open and not tp_open:
                    exit_reason = "SL/TP"  # both gone (edge case)
                logger.info(
                    f"[{self.coin}] Exit reason detection: "
                    f"SL({sl_algo_id})={'OPEN' if sl_open else 'FILLED'} "
                    f"TP({tp_algo_id})={'OPEN' if tp_open else 'FILLED'} "
                    f"→ {exit_reason}"
                )
            else:
                # No saved algoIds (old position), fall back to open orders check
                orders = self.exchange.get_open_orders(self.symbol)
                for o in orders:
                    otype = o.get("type", "")
                    if otype in ("STOP_MARKET", "STOP"):
                        exit_reason = "TP"
                    elif otype in ("TAKE_PROFIT_MARKET", "TAKE_PROFIT"):
                        exit_reason = "SL"
            # Cancel remaining orders
            self.exchange.cancel_all_orders(self.symbol)
        except Exception as e:
            logger.warning(f"[{self.coin}] Exit reason detection failed: {e}")

        entry_btc_score = float(db.get_meta(f"entry_btc_score_{self.coin}", "0"))

        # SL/TP slippage diagnostic
        sl_price = float(db.get_meta(f"sl_price_{self.coin}", "0"))
        tp_price = float(db.get_meta(f"tp_price_{self.coin}", "0"))
        if exit_price > 0 and exit_reason in ("SL", "TP"):
            expected = sl_price if exit_reason == "SL" else tp_price
            if expected > 0:
                slippage = abs(exit_price - expected)
                slip_bps = (slippage / expected) * 10000 if expected else 0
                logger.info(
                    f"[{self.coin}] SL/TP DIAG | {exit_reason} | "
                    f"expected={expected:.4f} fill={exit_price:.4f} "
                    f"slip={slippage:+.4f} ({slip_bps:.1f}bps)"
                )

        # Sanity check: if API pnl is wildly different from price-based calc,
        # use price-based calculation (API can accumulate from previous closes)
        if exit_price > 0 and entry_price > 0 and qty > 0:
            calc_pnl = (exit_price - entry_price) * qty * direction
            if abs(recent_pnl - calc_pnl) > abs(calc_pnl) * 0.5 + 1.0:
                logger.warning(
                    f"[{self.coin}] PnL sanity check FAILED: "
                    f"api={recent_pnl:+.4f} calc={calc_pnl:+.4f} "
                    f"diff={recent_pnl - calc_pnl:+.4f} — using calc"
                )
                recent_pnl = calc_pnl

        # Include entry commission in total fee
        entry_commission = float(db.get_meta(f"entry_commission_{self.coin}", "0") or "0")
        total_fee = recent_fee + entry_commission

        trade = {
            "coin": self.coin,
            "direction": direction,
            "entry_time": entry_time,
            "entry_price": entry_price,
            "exit_time": datetime.utcnow().isoformat(),
            "exit_price": exit_price,
            "qty": qty,
            "pnl_gross": round(recent_pnl, 4),
            "pnl_net": round(recent_pnl - total_fee, 4),
            "fee_total": round(total_fee, 4),
            "exit_reason": exit_reason,
            "btc_score_entry": entry_btc_score,
            "btc_score_exit": btc_score,
            "equity_after": round(current_equity, 2),
            "bars_held": bars_held,
        }

        db.insert_trade(trade)

        # Clear entry state
        db.set_meta(f"last_exit_bar_{self.coin}", db.get_meta("bar_count", "0"))
        db.set_meta(f"bars_held_{self.coin}", "0")
        self._clear_entry_meta()

        dir_str = "LONG" if direction == 1 else "SHORT"
        logger.info(
            f"[{self.coin}] DETECTED {exit_reason} ({dir_str}, between cycles) | "
            f"Entry={entry_price:.4f} Exit={exit_price:.4f} | "
            f"PnL=${recent_pnl:+.2f} | Fee=${recent_fee:.4f} | Bars={bars_held}"
        )

    def _cooldown_ok(self) -> bool:
        """Check if cooldown period has passed since last exit.
        Also checks flip_cooldown from exit_only SIGNAL_FLIP (Tournament R3).
        """
        bar_count = int(db.get_meta("bar_count", "0"))
        last_exit = int(db.get_meta(f"last_exit_bar_{self.coin}", "-999"))
        if (bar_count - last_exit) <= self.cooldown_bars:
            return False
        # Check flip cooldown (set by exit_only SIGNAL_FLIP)
        flip_cd = db.get_meta(f"flip_cooldown_{self.coin}", None)
        if flip_cd is not None:
            if bar_count <= int(flip_cd):
                return False
            # Cooldown expired, clean up
            db.set_meta(f"flip_cooldown_{self.coin}", None)
        return True

    def _get_bars_held(self) -> int:
        """Get bars held from state_meta counter."""
        return int(db.get_meta(f"bars_held_{self.coin}", "0"))

    def _increment_bars_held(self):
        """Increment the bars held counter."""
        current = self._get_bars_held()
        db.set_meta(f"bars_held_{self.coin}", str(current + 1))

    def _sync_state_from_exchange(self, pos: dict, btc_score: float, candle: dict = None):
        """Populate missing state_meta from live exchange position.

        Bug #7 fix 2026-04-18: also compute SL/TP software-fallback from
        current ATR so orphan-adopted positions get proper stop protection.
        Previously sl_price/tp_price=0 disabled software SL/TP entirely,
        leaving synced positions exposed to runaway losses if algo orders
        also failed.
        """
        pos_amt = float(pos["positionAmt"])
        direction = 1 if pos_amt > 0 else -1
        entry_price = float(pos["entryPrice"])
        qty = abs(pos_amt)
        atr = float(candle.get("atr", 0)) if candle else 0

        now_str = datetime.utcnow().isoformat()
        db.set_meta(f"entry_time_{self.coin}", now_str)
        db.set_meta(f"entry_btc_score_{self.coin}", str(btc_score))
        db.set_meta(f"entry_atr_{self.coin}", str(atr))
        db.set_meta(f"entry_direction_{self.coin}", str(direction))
        db.set_meta(f"entry_price_{self.coin}", str(entry_price))
        db.set_meta(f"entry_qty_{self.coin}", str(qty))

        # Software SL/TP fallback using current ATR
        if atr > 0:
            if direction == 1:
                sl_price = entry_price - self.sl_atr_mult * atr
                tp_price = entry_price + self.tp_atr_mult * atr
            else:
                sl_price = entry_price + self.sl_atr_mult * atr
                tp_price = entry_price - self.tp_atr_mult * atr
        else:
            sl_price = 0
            tp_price = 0

        db.set_meta(f"sl_price_{self.coin}", str(sl_price))
        db.set_meta(f"tp_price_{self.coin}", str(tp_price))
        db.set_meta(f"sl_exchange_{self.coin}", "0")   # no exchange algo for synced pos
        db.set_meta(f"tp_exchange_{self.coin}", "0")   # → software-only
        db.set_meta(f"bars_held_{self.coin}", "0")

        sl_tp_msg = (f"SL={sl_price:.4f} TP={tp_price:.4f}" if atr > 0
                     else "no ATR → timeout/signal-only exit")
        logger.warning(
            f"[{self.coin}] Synced state from exchange: "
            f"dir={direction} price={entry_price:.4f} qty={qty} {sl_tp_msg}"
        )

    def _clear_entry_meta(self):
        """Clear all entry metadata for this coin."""
        for key in ("entry_time", "entry_btc_score", "entry_atr",
                    "entry_direction", "entry_price", "entry_qty",
                    "entry_commission",
                    "sl_price", "tp_price", "sl_exchange", "tp_exchange",
                    "sl_algo_id", "tp_algo_id",
                    "trail_peak", "trail_trough"):
            db.set_meta(f"{key}_{self.coin}", "")

    def _get_pnl_from_trades(self, order_id) -> tuple:
        """Get (pnl, commission, exit_price) from Binance account trades.

        Matches trades by orderId for accuracy. Falls back to income API.
        Returns (pnl_gross, commission, exit_price).
        """
        try:
            trades = self.exchange.get_trades(self.symbol, limit=10)
            matching = [t for t in trades if t.get("orderId") == order_id]
            if matching:
                pnl = sum(float(t.get("realizedPnl", 0)) for t in matching)
                comm = sum(abs(float(t.get("commission", 0))) for t in matching)
                # Weighted average exit price
                total_qty = sum(float(t.get("qty", 0)) for t in matching)
                if total_qty > 0:
                    price = sum(
                        float(t.get("price", 0)) * float(t.get("qty", 0))
                        for t in matching
                    ) / total_qty
                else:
                    price = float(matching[-1].get("price", 0))
                logger.info(
                    f"[{self.coin}] Binance PnL (trades API): "
                    f"pnl=${pnl:+.4f} fee=${comm:.4f} exit={price:.4f} "
                    f"({len(matching)} fills)"
                )
                return pnl, comm, price
        except Exception as e:
            logger.debug(f"[{self.coin}] get_trades failed: {e}")

        # Fallback: income API (use only LATEST entry, not sum)
        try:
            income = self.exchange.get_income(
                symbol=self.symbol, income_type="REALIZED_PNL", limit=5
            )
            pnl = float(income[-1].get("income", 0)) if income else 0.0
            comm_income = self.exchange.get_income(
                symbol=self.symbol, income_type="COMMISSION", limit=5
            )
            comm = abs(float(comm_income[-1].get("income", 0))) if comm_income else 0.0
            logger.info(
                f"[{self.coin}] Binance PnL (income API fallback): "
                f"pnl=${pnl:+.4f} fee=${comm:.4f}"
            )
            return pnl, comm, 0.0
        except Exception as e:
            logger.warning(
                f"[{self.coin}] Both trades and income API failed: {e}. "
                f"Recording pnl=0."
            )
            return 0.0, 0.0, 0.0

    def _save_exchange_trade(self, order: dict, reason: str):
        """Save raw exchange order data as backup."""
        try:
            db.insert_exchange_trade({
                "coin": self.coin,
                "symbol": self.symbol,
                "order_id": str(order.get("orderId", "")),
                "side": order.get("side", ""),
                "type": reason,
                "qty": float(order.get("origQty", 0)),
                "price": float(order.get("avgPrice", 0) or order.get("price", 0)),
                "realized_pnl": None,
                "commission": None,
                "commission_asset": None,
                "ts": datetime.utcnow().isoformat(),
                "raw_json": json.dumps(order),
            })
        except Exception as e:
            logger.warning(f"[{self.coin}] Failed to save exchange trade: {e}")
