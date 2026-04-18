"""
Binance Futures Testnet Exchange Client
========================================
Wraps python-binance for testnet order execution.
All orders go to https://testnet.binancefuture.com
"""

import logging
import math
import time
from typing import Optional

from binance.client import Client
from binance.exceptions import BinanceAPIException

logger = logging.getLogger(__name__)


class TestnetExchange:
    """Thin wrapper around python-binance for futures testnet."""

    def __init__(self, api_key: str, api_secret: str):
        self.client = Client(api_key, api_secret, testnet=True,
                              requests_params={"timeout": 15})
        self.client.REQUEST_TIMEOUT = 15
        # Allow 10s clock skew to survive after system restart / NTP drift
        self.client.timestamp_offset = 0
        self._sync_time()
        self._symbol_info_cache: dict = {}
        logger.info("TestnetExchange initialized (testnet mode)")

    def _sync_time(self):
        """Sync local clock offset with Binance server time."""
        try:
            server_time = self.client.get_server_time()
            self.client.timestamp_offset = server_time["serverTime"] - int(time.time() * 1000)
            logger.info(f"Time sync: offset={self.client.timestamp_offset}ms")
        except Exception as e:
            logger.warning(f"Time sync failed: {e}, using offset=0")
            self.client.timestamp_offset = 0

    def _synced_timestamp(self) -> int:
        """Return current timestamp adjusted by server offset."""
        return int(time.time() * 1000) + self.client.timestamp_offset

    # ── Symbol info & precision ──────────────────────────────────

    def get_symbol_info(self, symbol: str) -> dict:
        """Get symbol filters (precision, lot size, tick size) with caching."""
        if symbol in self._symbol_info_cache:
            return self._symbol_info_cache[symbol]

        info = self.client.futures_exchange_info()
        for s in info["symbols"]:
            if s["symbol"] == symbol:
                result = {
                    "quantityPrecision": s["quantityPrecision"],
                    "pricePrecision": s["pricePrecision"],
                    "filters": {f["filterType"]: f for f in s["filters"]},
                }
                self._symbol_info_cache[symbol] = result
                return result
        raise ValueError(f"Symbol {symbol} not found on testnet")

    def round_qty(self, symbol: str, qty: float) -> float:
        """Round quantity to exchange-allowed precision."""
        info = self.get_symbol_info(symbol)
        step = float(info["filters"]["LOT_SIZE"]["stepSize"])
        precision = info["quantityPrecision"]
        # Floor to step size
        qty = math.floor(qty / step) * step
        return round(qty, precision)

    def round_price(self, symbol: str, price: float) -> float:
        """Round price to exchange-allowed tick size."""
        info = self.get_symbol_info(symbol)
        tick = float(info["filters"]["PRICE_FILTER"]["tickSize"])
        precision = info["pricePrecision"]
        price = round(round(price / tick) * tick, precision)
        return price

    # ── Account ──────────────────────────────────────────────────

    def get_account_balance(self) -> float:
        """Get USDT balance from futures account."""
        balances = self.client.futures_account_balance()
        for b in balances:
            if b["asset"] == "USDT":
                return float(b["balance"])
        return 0.0

    def get_available_balance(self) -> float:
        """Get available (withdrawable) USDT balance."""
        balances = self.client.futures_account_balance()
        for b in balances:
            if b["asset"] == "USDT":
                return float(b["availableBalance"])
        return 0.0

    # ── Positions ────────────────────────────────────────────────

    def get_open_positions(self) -> list[dict]:
        """Get all positions with non-zero quantity."""
        positions = self.client.futures_position_information()
        return [
            p for p in positions
            if float(p.get("positionAmt", 0)) != 0
        ]

    def get_position(self, symbol: str) -> Optional[dict]:
        """Get position for a specific symbol. Returns None if no position."""
        try:
            positions = self.client.futures_position_information(symbol=symbol)
            for p in positions:
                if abs(float(p.get("positionAmt", 0))) > 0:
                    return p
        except BinanceAPIException as e:
            logger.error(f"get_position({symbol}) error: {e}")
        return None

    # ── Orders ───────────────────────────────────────────────────

    def place_market_order(self, symbol: str, side: str, qty: float,
                           reduce_only: bool = False) -> dict:
        """Place a market order (entry or exit).

        Args:
            symbol: e.g. "BTCUSDT"
            side: "BUY" or "SELL"
            qty: quantity (will be rounded to exchange precision)
            reduce_only: if True, order can only reduce position (bypasses
                         min notional check for closing small positions)

        Returns order dict with avgPrice populated (polled until FILLED).
        """
        qty = self.round_qty(symbol, qty)
        if qty <= 0:
            raise ValueError(f"Quantity too small after rounding: {qty}")

        params = dict(
            symbol=symbol,
            side=side,
            type="MARKET",
            quantity=qty,
        )
        if reduce_only:
            params["reduceOnly"] = "true"

        order = self.client.futures_create_order(**params)

        # Binance testnet returns status=NEW with avgPrice=0 for market orders.
        # Poll until filled to get the actual fill price.
        order_id = order["orderId"]
        avg_price = float(order.get("avgPrice", 0))
        if avg_price == 0 or order.get("status") != "FILLED":
            avg_price = self._poll_fill_price(symbol, order_id)
            order["avgPrice"] = str(avg_price)
            order["status"] = "FILLED"

        logger.info(
            f"MARKET {side} {symbol} qty={qty} | orderId={order_id} "
            f"avgPrice={avg_price}"
        )
        return order

    def _poll_fill_price(self, symbol: str, order_id: int,
                         max_retries: int = 10, delay: float = 0.5) -> float:
        """Poll order status until filled, return avgPrice.

        Falls back to recent account trades if order query fails.
        """
        for i in range(max_retries):
            try:
                result = self.client.futures_get_order(
                    symbol=symbol, orderId=order_id
                )
                avg = float(result.get("avgPrice", 0))
                if avg > 0 and result.get("status") == "FILLED":
                    return avg
            except BinanceAPIException as e:
                logger.debug(f"poll_fill_price attempt {i+1}: {e}")
            time.sleep(delay)

        # Fallback: get price from recent account trades
        logger.warning(
            f"Order {order_id} not filled after {max_retries} retries, "
            f"falling back to recent trades"
        )
        try:
            trades = self.client.futures_account_trades(
                symbol=symbol, limit=5
            )
            if trades:
                # Find trades matching this order
                matching = [t for t in trades if t.get("orderId") == order_id]
                if matching:
                    total_qty = sum(float(t["qty"]) for t in matching)
                    total_cost = sum(
                        float(t["price"]) * float(t["qty"]) for t in matching
                    )
                    if total_qty > 0:
                        return total_cost / total_qty
                # Last resort: latest trade price
                return float(trades[-1]["price"])
        except Exception as e:
            logger.error(f"Fallback trade lookup failed: {e}")

        raise RuntimeError(
            f"Cannot determine fill price for order {order_id} on {symbol}"
        )

    def _algo_order(self, symbol: str, side: str, order_type: str,
                    trigger_price: float, qty: float) -> dict:
        """Place a conditional order via Algo Order API (/fapi/v1/algoOrder).

        Binance migrated STOP_MARKET/TAKE_PROFIT_MARKET to Algo Service (Dec 2025).
        Uses MARK_PRICE (Binance default) for realistic paper trading.
        """
        import time
        import hmac
        import hashlib
        from urllib.parse import urlencode
        import requests

        qty = self.round_qty(symbol, qty)
        trigger_price = self.round_price(symbol, trigger_price)
        timestamp = self._synced_timestamp()

        # Order matters: build params as list of tuples (preserve insertion order)
        params = [
            ("algoType", "CONDITIONAL"),
            ("symbol", symbol),
            ("side", side),
            ("type", order_type),
            ("triggerPrice", str(trigger_price)),
            ("quantity", str(qty)),
            ("workingType", "MARK_PRICE"),
            ("reduceOnly", "true"),
            ("timestamp", str(timestamp)),
            ("recvWindow", "10000"),
        ]

        # Sign: HMAC-SHA256 of the query string
        query_string = urlencode(params)
        signature = hmac.new(
            self.client.API_SECRET.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        base_url = self.client.FUTURES_TESTNET_URL
        url = f"{base_url}/v1/algoOrder?{query_string}&signature={signature}"
        headers = {"X-MBX-APIKEY": self.client.API_KEY}

        resp = requests.post(url, headers=headers, timeout=10)
        data = resp.json()

        if resp.status_code != 200 or data.get("code"):
            raise BinanceAPIException(resp, resp.status_code, resp.text)

        logger.info(
            f"ALGO {order_type} {side} {symbol} qty={qty} trigger={trigger_price} | "
            f"algoId={data.get('algoId', data.get('orderId', ''))}"
        )
        return data

    def place_stop_loss(self, symbol: str, side: str, qty: float,
                        stop_price: float) -> dict:
        """Place a STOP_MARKET order for stop-loss via Algo Order API."""
        return self._algo_order(symbol, side, "STOP_MARKET", stop_price, qty)

    def place_take_profit(self, symbol: str, side: str, qty: float,
                          stop_price: float) -> dict:
        """Place a TAKE_PROFIT_MARKET order via Algo Order API."""
        return self._algo_order(symbol, side, "TAKE_PROFIT_MARKET", stop_price, qty)

    def cancel_all_orders(self, symbol: str) -> None:
        """Cancel all open orders for a symbol (regular + algo)."""
        try:
            self.client.futures_cancel_all_open_orders(symbol=symbol)
        except BinanceAPIException as e:
            if e.code != -2011:
                logger.debug(f"cancel regular orders: {e}")
        # Also cancel algo orders
        algo_cancel_ok = True
        try:
            self._cancel_algo_orders(symbol)
        except Exception as e:
            algo_cancel_ok = False
            logger.warning(f"cancel algo orders FAILED for {symbol}: {e}")
        # Verify no algo orders remain
        if algo_cancel_ok:
            remaining = self.get_open_algo_orders(symbol)
            if remaining:
                logger.warning(
                    f"{symbol}: {len(remaining)} algo orders still open after cancel! "
                    f"Retrying..."
                )
                try:
                    self._cancel_algo_orders(symbol)
                except Exception as e:
                    logger.warning(f"{symbol}: algo cancel retry failed: {e}")
        logger.info(f"Cancelled all orders for {symbol}")

    def _cancel_algo_orders(self, symbol: str) -> None:
        """Cancel open algo orders for a symbol."""
        import time
        import hmac
        import hashlib
        from urllib.parse import urlencode
        import requests

        base_url = self.client.FUTURES_TESTNET_URL
        headers = {"X-MBX-APIKEY": self.client.API_KEY}

        # Get open algo orders
        params = [("symbol", symbol), ("timestamp", str(self._synced_timestamp())),
                  ("recvWindow", "10000")]
        qs = urlencode(params)
        sig = hmac.new(
            self.client.API_SECRET.encode("utf-8"), qs.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        resp = requests.get(
            f"{base_url}/v1/algoOrder/openOrders?{qs}&signature={sig}",
            headers=headers, timeout=10,
        )
        if resp.status_code != 200:
            return
        data = resp.json()
        orders = data.get("orders", [])

        for o in orders:
            algo_id = o.get("algoId")
            if not algo_id:
                continue
            p = [("algoId", str(algo_id)), ("timestamp", str(self._synced_timestamp())),
                 ("recvWindow", "10000")]
            q = urlencode(p)
            s = hmac.new(
                self.client.API_SECRET.encode("utf-8"), q.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            requests.delete(
                f"{base_url}/v1/algoOrder?{q}&signature={s}",
                headers=headers, timeout=10,
            )

    def get_open_orders(self, symbol: str) -> list[dict]:
        """Get all open orders for a symbol (regular orders)."""
        return self.client.futures_get_open_orders(symbol=symbol)

    def get_open_algo_orders(self, symbol: str) -> list[dict]:
        """Get open algo orders for a specific symbol.

        Returns list of algo order dicts with algoId, type, side, etc.
        """
        import time as _time
        import hmac
        import hashlib
        from urllib.parse import urlencode
        import requests as _requests

        base_url = self.client.FUTURES_TESTNET_URL
        headers = {"X-MBX-APIKEY": self.client.API_KEY}

        params = [("symbol", symbol), ("timestamp", str(self._synced_timestamp())),
                  ("recvWindow", "10000")]
        qs = urlencode(params)
        sig = hmac.new(
            self.client.API_SECRET.encode("utf-8"), qs.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        try:
            resp = _requests.get(
                f"{base_url}/v1/algoOrder/openOrders?{qs}&signature={sig}",
                headers=headers, timeout=10,
            )
            if resp.status_code == 200:
                return resp.json().get("orders", [])
        except Exception as e:
            logger.debug(f"get_open_algo_orders({symbol}) failed: {e}")
        return []

    def list_all_open_algo_symbols(self) -> set[str]:
        """Return set of symbols that currently have open algo orders.

        Uses the algoOrder/openOrders endpoint without symbol filter → returns
        every open algo order on the account. Caller uses this to detect
        stale orders on removed/non-active symbols.
        """
        import hmac
        import hashlib
        from urllib.parse import urlencode
        import requests

        base_url = self.client.FUTURES_TESTNET_URL
        headers = {"X-MBX-APIKEY": self.client.API_KEY}
        params = [("timestamp", str(self._synced_timestamp())),
                  ("recvWindow", "10000")]
        qs = urlencode(params)
        sig = hmac.new(
            self.client.API_SECRET.encode("utf-8"), qs.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        try:
            resp = requests.get(
                f"{base_url}/v1/algoOrder/openOrders?{qs}&signature={sig}",
                headers=headers, timeout=10,
            )
            if resp.status_code == 200:
                orders = resp.json().get("orders", [])
                return {o["symbol"] for o in orders if o.get("symbol")}
        except Exception as e:
            logger.debug(f"list_all_open_algo_symbols failed: {e}")
        return set()

    def get_open_algo_order_count(self) -> int:
        """Get total number of open algo orders across all symbols.

        Binance testnet has a global limit on algo orders (~10).
        Returns count, or -1 if API call fails.
        """
        import time
        import hmac
        import hashlib
        from urllib.parse import urlencode
        import requests

        base_url = self.client.FUTURES_TESTNET_URL
        headers = {"X-MBX-APIKEY": self.client.API_KEY}

        params = [("timestamp", str(self._synced_timestamp())),
                  ("recvWindow", "10000")]
        qs = urlencode(params)
        sig = hmac.new(
            self.client.API_SECRET.encode("utf-8"), qs.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        try:
            resp = requests.get(
                f"{base_url}/v1/algoOrder/openOrders?{qs}&signature={sig}",
                headers=headers, timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                orders = data.get("orders", [])
                return len(orders)
        except Exception as e:
            logger.debug(f"get_open_algo_order_count failed: {e}")
        return -1

    # ── Leverage & Position Mode ─────────────────────────────────

    def set_leverage(self, symbol: str, leverage: int) -> None:
        """Set leverage for a symbol."""
        try:
            self.client.futures_change_leverage(
                symbol=symbol, leverage=leverage
            )
        except BinanceAPIException as e:
            # -4028 = leverage not changed (already set)
            if e.code != -4028:
                logger.warning(f"set_leverage({symbol}, {leverage}): {e}")

    def ensure_one_way_mode(self) -> None:
        """Ensure position mode is One-Way (not Hedge)."""
        try:
            mode = self.client.futures_get_position_mode()
            if mode.get("dualSidePosition"):
                self.client.futures_change_position_mode(dualSidePosition=False)
                logger.info("Switched to One-Way position mode")
        except BinanceAPIException as e:
            # -4059 = already in one-way mode
            if e.code != -4059:
                logger.warning(f"ensure_one_way_mode: {e}")

    # ── Trade History ────────────────────────────────────────────

    def get_trades(self, symbol: str, limit: int = 50) -> list[dict]:
        """Get recent account trades for a symbol."""
        return self.client.futures_account_trades(symbol=symbol, limit=limit)

    def get_all_trades(self, symbol: str, start_time: int = None) -> list[dict]:
        """Get ALL account trades for a symbol using pagination (fromId).

        Fetches without startTime first (gets recent), then paginates
        backwards if needed, or forward from fromId.
        Binance returns max 1000 per call.
        """
        all_trades = []
        # First call: no startTime (gets the most recent trades)
        params = {"symbol": symbol, "limit": 1000}
        while True:
            batch = self.client.futures_account_trades(**params)
            if not batch:
                break
            all_trades.extend(batch)
            if len(batch) < 1000:
                break  # got everything
            last_id = batch[-1]["id"]
            params = {"symbol": symbol, "limit": 1000, "fromId": last_id + 1}

        # Filter by start_time if provided
        if start_time and all_trades:
            all_trades = [t for t in all_trades if int(t.get("time", 0)) >= start_time]

        return all_trades

    def get_income(self, symbol: str = None, income_type: str = None,
                   limit: int = 100) -> list[dict]:
        """Get income history (realized PnL, funding, commission, etc.)."""
        params = {"limit": limit}
        if symbol:
            params["symbol"] = symbol
        if income_type:
            params["incomeType"] = income_type
        return self.client.futures_income_history(**params)

    def get_all_income(self, start_time: int = None) -> list[dict]:
        """Get ALL income history using pagination (startTime)."""
        all_income = []
        params = {"limit": 1000}
        if start_time:
            params["startTime"] = start_time
        while True:
            batch = self.client.futures_income_history(**params)
            if not batch:
                break
            all_income.extend(batch)
            if len(batch) < 1000:
                break
            last_time = int(batch[-1]["time"])
            params = {"limit": 1000, "startTime": last_time + 1}
        return all_income
