"""
Alt Data Entry Filter — Funding Rate + CVD + Liquidation
==========================================================
Uses per-coin alt data to filter bad entries.
Based on paper trade correlation analysis (2026-04-03, 1,858 trades):
  - Funding rate z8: corr -0.20 with PnL → block when extreme
  - CVD buy volume z4: corr +0.10 with PnL → block LONG when weak
  - Liq ratio z4: corr +0.08 (+0.15 LONG) → block LONG when longs getting liquidated

Ground truth = real paper trades, NOT backtest.
"""

import logging
import time
from datetime import datetime, timedelta

import numpy as np
import psycopg2

from .config import DB_PARAMS

logger = logging.getLogger(__name__)

# Cache to avoid hammering PostgreSQL every 15min for every coin
_cache = {}
_cache_ts = {}
CACHE_TTL = 300  # 5 minutes


def _get_conn():
    return psycopg2.connect(**DB_PARAMS)


def _query_recent(table, symbol, col, hours=6):
    """Fetch recent rows for a symbol from a market_data table."""
    cache_key = f"{table}_{symbol}_{col}"
    now = time.time()
    if cache_key in _cache and (now - _cache_ts.get(cache_key, 0)) < CACHE_TTL:
        return _cache[cache_key]

    # SURGERY 2026-04-10: try/finally to prevent connection leak
    # Previous code leaked conn on exception → "too many clients already" after 17h
    conn = None
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(f"""
            SELECT ts, {col} FROM market_data.{table}
            WHERE symbol = %s AND ts >= NOW() - INTERVAL '{hours} hours'
            ORDER BY ts
        """, (symbol,))
        rows = cur.fetchall()

        if not rows:
            return None

        values = [float(r[1]) for r in rows if r[1] is not None]
        _cache[cache_key] = values
        _cache_ts[cache_key] = now
        return values

    except Exception as e:
        logger.warning(f"[alt_filter] DB error {table}/{symbol}: {e}")
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _query_liq_ratio(symbol, hours=4):
    """Compute liq_ratio (short_liq / total_liq) in 15-min buckets from tick data."""
    cache_key = f"liq_ratio_{symbol}"
    now = time.time()
    if cache_key in _cache and (now - _cache_ts.get(cache_key, 0)) < CACHE_TTL:
        return _cache[cache_key]

    # SURGERY 2026-04-10: try/finally to prevent connection leak
    conn = None
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT
                date_trunc('minute', event_time) -
                    (EXTRACT(MINUTE FROM event_time)::int %% 15) * INTERVAL '1 minute' AS bucket,
                SUM(CASE WHEN side = 'BUY' THEN notional_usd ELSE 0 END) AS short_liq,
                SUM(notional_usd) AS total_liq
            FROM market_data.liquidation
            WHERE symbol = %s AND event_time >= NOW() - INTERVAL '%s hours'
            GROUP BY bucket
            ORDER BY bucket
        """, (symbol, hours))
        rows = cur.fetchall()

        if not rows:
            return None

        ratios = []
        for _, short_liq, total_liq in rows:
            if total_liq and float(total_liq) > 0:
                ratios.append(float(short_liq) / float(total_liq))
        _cache[cache_key] = ratios
        _cache_ts[cache_key] = now
        return ratios if len(ratios) >= 2 else None

    except Exception as e:
        logger.warning(f"[alt_filter] Liq query error {symbol}: {e}")
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _zscore(values, lookback):
    """Compute z-score of last value over lookback window."""
    if not values or len(values) < lookback:
        return None
    window = values[-lookback:]
    mean = np.mean(window)
    std = np.std(window)
    if std < 1e-12:
        return 0.0
    return (values[-1] - mean) / std


def _momentum(values, lookback):
    """Compute momentum: last value minus value lookback periods ago."""
    if not values or len(values) < lookback + 1:
        return None
    return values[-1] - values[-(lookback + 1)]


def check_entry_filter(coin: str, signal: int) -> dict:
    """
    Check if per-coin alt data supports this entry.

    Args:
        coin: e.g. "BTC"
        signal: 1 (LONG) or -1 (SHORT)

    Returns:
        dict with:
            "allow": bool - True = entry OK, False = block
            "reason": str - why blocked
            "details": dict - factor values for logging
    """
    symbol = coin + "USDT"
    details = {}
    block_reasons = []

    # ── 1. Funding Rate Filter ──────────────────────────────────
    # Analysis: funding_rate_z8 corr -0.20, removing top 30% → +$402
    # Threshold: z-score > 1.0 ≈ top ~16% (conservative)
    fr_values = _query_recent("funding_rate_alt", symbol, "funding_rate", hours=24)
    if fr_values and len(fr_values) >= 4:
        # z-score over 8 periods (8 hours for hourly data)
        fr_z8 = _zscore(fr_values, min(8, len(fr_values)))
        # momentum over 4 periods
        fr_mom4 = _momentum(fr_values, min(4, len(fr_values) - 1))

        details["fr_z8"] = round(fr_z8, 3) if fr_z8 is not None else None
        details["fr_mom4"] = round(fr_mom4, 6) if fr_mom4 is not None else None

        if fr_z8 is not None and fr_z8 > 1.0:
            block_reasons.append(f"FR_Z8_HIGH({fr_z8:+.2f})")
        if fr_mom4 is not None and fr_mom4 > 0.0003:
            block_reasons.append(f"FR_MOM4_HIGH({fr_mom4:+.6f})")

    # ── 2. CVD Filter (15 coins only) ──────────────────────────
    # Analysis: buy_vol_z4 corr +0.10 overall, +0.15 for LONG
    # Low buy volume = bad for LONG entries
    buy_values = _query_recent("cvd_alt", symbol, "buy_vol", hours=4)
    if buy_values and len(buy_values) >= 4:
        bv_z4 = _zscore(buy_values, min(4, len(buy_values)))
        details["bv_z4"] = round(bv_z4, 3) if bv_z4 is not None else None

        # Block LONG when buy volume is abnormally low (z < -1.0)
        if signal == 1 and bv_z4 is not None and bv_z4 < -1.0:
            block_reasons.append(f"CVD_BUY_LOW({bv_z4:+.2f})")

        # SURGERY 2026-04-10: CVD_SELL_LOW DISABLED
        # Was blocking 162+ SHORT entries / week. SHORT is the profitable
        # direction (WR 50.1%, +$356). Blocking it killed weekly PnL.
        # Keep sv_z4 in details for monitoring but do NOT block.
        sell_values = _query_recent("cvd_alt", symbol, "sell_vol", hours=4)
        if sell_values and len(sell_values) >= 4:
            sv_z4 = _zscore(sell_values, min(4, len(sell_values)))
            details["sv_z4"] = round(sv_z4, 3) if sv_z4 is not None else None

    # ── 3. Liquidation Ratio Filter ─────────────────────────────
    # Analysis: liq_ratio_z4 corr +0.08 overall, +0.15 for LONG
    # liq_ratio = short_liq / total_liq → high = shorts squeezed (bullish)
    # Low ratio = longs getting liquidated = bearish → bad for LONG
    liq_data = _query_liq_ratio(symbol, hours=4)
    if liq_data and len(liq_data) >= 4:
        lr_z4 = _zscore(liq_data, min(4, len(liq_data)))
        details["lr_z4"] = round(lr_z4, 3) if lr_z4 is not None else None

        # Block LONG when liq_ratio z-score is very low (longs being wiped out)
        if signal == 1 and lr_z4 is not None and lr_z4 < -1.0:
            block_reasons.append(f"LIQ_RATIO_LOW({lr_z4:+.2f})")

    # ── Decision ────────────────────────────────────────────────
    if block_reasons:
        reason = "ALT_FILTER: " + ", ".join(block_reasons)
        logger.info(f"[{coin}] BLOCKED by alt filter: {reason} | details={details}")
        return {"allow": False, "reason": reason, "details": details}

    return {"allow": True, "reason": "ALT_FILTER_OK", "details": details}
