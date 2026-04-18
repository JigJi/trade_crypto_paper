"""REST API routes for the dashboard."""
import json
import sqlite3
import logging
import time as _time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from flask import Blueprint, jsonify, request

import sys
BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from research.config import PAPER_TRADES_DB, EXPERIMENTS_DIR, EXPERIMENT_REGISTRY_PATH
from research.factor_registry import FactorRegistry
from research.model_registry import ModelRegistry
from research.leaderboard import build_leaderboard
from research.missions import MissionEngine
from paper_trading.exchange import TestnetExchange
from paper_trading.config import (
    BINANCE_TESTNET_KEY, BINANCE_TESTNET_SECRET, COIN_CONFIGS, COINS_V3, COINS_V5, COINS_V6,
    PAPER_TRADING_START_MS, V6_DEPLOY_MS, LEVERAGE,
    COINS_ALL_EVER, COINS_REMOVED,
)

log = logging.getLogger(__name__)
api_bp = Blueprint("api", __name__)

# ── Singletons (lazy init) ───────────────────────────────────
_factor_reg = None
_model_reg = None
_exchange = None
_mission_engine = None


def _ex():
    """Lazy-init testnet exchange client for dashboard."""
    global _exchange
    if _exchange is None and BINANCE_TESTNET_KEY and BINANCE_TESTNET_SECRET:
        try:
            _exchange = TestnetExchange(BINANCE_TESTNET_KEY, BINANCE_TESTNET_SECRET)
        except Exception as e:
            log.warning(f"Failed to init exchange client: {e}")
    return _exchange


def _fr():
    global _factor_reg
    if _factor_reg is None:
        _factor_reg = FactorRegistry()
    return _factor_reg


def _mr():
    global _model_reg
    if _model_reg is None:
        _model_reg = ModelRegistry()
    return _model_reg


def _me():
    global _mission_engine
    if _mission_engine is None:
        _mission_engine = MissionEngine()
    return _mission_engine


def _paper_db():
    """Get paper trading SQLite connection."""
    if not PAPER_TRADES_DB.exists():
        return None
    conn = sqlite3.connect(str(PAPER_TRADES_DB))
    conn.row_factory = sqlite3.Row
    return conn


# ── Binance caches ──────────────────────────────────────────
_trade_cache = {"data": None, "ts": 0}
_funding_cache = {"data": None, "ts": 0}
_TRADE_CACHE_TTL = 60  # seconds (paginated fetch is slower)


_INIT_EQUITY = 5000


def _fetch_funding_by_coin():
    """Fetch all FUNDING_FEE + COMMISSION from Binance income history, grouped by coin.

    Returns dict: {coin: total_funding_and_commission}
    Cached for 60s.
    """
    now = _time.time()
    if _funding_cache["data"] is not None and now - _funding_cache["ts"] < _TRADE_CACHE_TTL:
        return _funding_cache["data"]

    ex = _ex()
    if not ex:
        return {}

    try:
        all_income = ex.get_all_income(start_time=PAPER_TRADING_START_MS)
        by_coin = defaultdict(float)
        for r in all_income:
            if r.get("incomeType") in ("FUNDING_FEE", "COMMISSION"):
                sym = r.get("symbol", "")
                coin = sym.replace("USDT", "") if sym else "_OTHER"
                by_coin[coin] += float(r.get("income", 0))
        _funding_cache.update(data=dict(by_coin), ts=now)
        return dict(by_coin)
    except Exception as e:
        log.error(f"_fetch_funding_by_coin error: {e}")
        return _funding_cache.get("data") or {}


def _fetch_binance_trades():
    """Fetch closed trades directly from Binance Testnet (cached 30s).

    Uses futures_income_history to find active symbols, then
    futures_account_trades per symbol. Groups fills by orderId.
    pnl_net = Binance realizedPnl (no manual calc).
    Returns list sorted newest-first.
    """
    now = _time.time()
    if _trade_cache["data"] is not None and now - _trade_cache["ts"] < _TRADE_CACHE_TTL:
        return _trade_cache["data"]

    ex = _ex()
    if not ex:
        return []

    try:
        # Step 1: get all symbols ever traded (active + removed)
        symbols = set(f"{c}USDT" for c in COINS_ALL_EVER)

        # Step 2: get ALL trades per symbol (paginated, no limit)
        all_fills = []
        for sym in symbols:
            try:
                fills = ex.get_all_trades(sym, start_time=PAPER_TRADING_START_MS)
                all_fills.extend(fills)
            except Exception as e:
                log.warning(f"get_all_trades({sym}) failed: {e}")

        # Step 3: group fills by orderId
        orders = defaultdict(list)
        for f in all_fills:
            orders[f["orderId"]].append(f)

        # Step 4: closing orders have non-zero realizedPnl
        trades = []
        for order_id, fills in orders.items():
            total_rpnl = sum(float(f.get("realizedPnl", 0)) for f in fills)
            if abs(total_rpnl) < 0.0001:
                continue  # entry order – skip

            total_commission = sum(float(f.get("commission", 0)) for f in fills)
            total_qty = sum(float(f.get("qty", 0)) for f in fills)

            first = fills[0]
            side = first.get("side", "SELL")
            # SELL closes LONG, BUY closes SHORT
            direction = 1 if side == "SELL" else -1
            trade_time = max(int(f.get("time", 0)) for f in fills)

            # Weighted average exit price
            exit_notional = sum(float(f.get("price", 0)) * float(f.get("qty", 0)) for f in fills)
            exit_price = exit_notional / total_qty if total_qty > 0 else 0

            # Derive entry price: pnl = (exit - entry) * qty * direction
            entry_price = exit_price - direction * (total_rpnl / total_qty) if total_qty > 0 else 0

            # Margin & profit % (net of fees)
            notional = entry_price * total_qty
            margin = notional / LEVERAGE if LEVERAGE > 0 else notional
            net_after_fee = total_rpnl - total_commission
            profit_pct = (net_after_fee / margin * 100) if margin > 0 else 0

            coin_name = first["symbol"].replace("USDT", "")
            # Model will be enriched from signal_log later; use current config as fallback
            coin_model = COIN_CONFIGS.get(coin_name, {}).get("model", "")
            if not coin_model:
                coin_model = "v6" if coin_name in COINS_V6 else "v5" if coin_name in COINS_V5 else "v3"
            trades.append({
                "coin": coin_name,
                "symbol": first["symbol"],
                "direction": direction,
                "qty": round(total_qty, 6),
                "entry_price": round(entry_price, 6),
                "exit_price": round(exit_price, 6),
                "pnl_net": round(total_rpnl, 4),  # Binance realizedPnl as-is
                "fee": round(total_commission, 4),
                "margin": round(margin, 2),
                "leverage": LEVERAGE,
                "profit_pct": round(profit_pct, 2),
                "exit_time": datetime.utcfromtimestamp(trade_time / 1000).isoformat(),
                "time_ms": trade_time,
                "model": coin_model,
            })

        trades.sort(key=lambda x: x["time_ms"], reverse=True)

        # Filter out trades before the current paper trading epoch (post-reset)
        trades = [t for t in trades if t["time_ms"] >= PAPER_TRADING_START_MS]

        # Enrich with exit_reason + actual model from SQLite
        conn = _paper_db()
        if conn:
            try:
                # --- exit_reason from trades table ---
                reason_rows = conn.execute(
                    "SELECT coin, exit_time, exit_reason FROM trades"
                ).fetchall()
                from collections import defaultdict as _dd
                coin_reasons = _dd(list)
                for r in reason_rows:
                    if r["exit_time"]:
                        coin_reasons[r["coin"]].append((r["exit_time"], r["exit_reason"]))
                for t in trades:
                    t["exit_reason"] = ""
                    candidates = coin_reasons.get(t["coin"], [])
                    if not candidates or not t.get("exit_time"):
                        continue
                    bt = t["exit_time"][:19]
                    best_reason = ""
                    best_diff = 300
                    for et, reason in candidates:
                        try:
                            diff = abs((datetime.fromisoformat(bt) - datetime.fromisoformat(et[:19])).total_seconds())
                            if diff < best_diff:
                                best_diff = diff
                                best_reason = reason
                        except Exception:
                            continue
                    t["exit_reason"] = best_reason

                # --- actual model from signal_log (source of truth) ---
                sig_rows = conn.execute(
                    "SELECT coin, ts, model FROM signal_log "
                    "WHERE action LIKE 'OPEN%' ORDER BY ts"
                ).fetchall()
                coin_signals = _dd(list)
                for s in sig_rows:
                    coin_signals[s["coin"]].append((s["ts"], s["model"]))
                for t in trades:
                    sigs = coin_signals.get(t["coin"], [])
                    if not sigs or not t.get("exit_time"):
                        continue
                    # Find latest OPEN signal before the trade exit time
                    et = t["exit_time"][:19]
                    best_model = None
                    for sig_ts, sig_model in reversed(sigs):
                        if sig_ts[:19] <= et:
                            best_model = sig_model
                            break
                    if best_model:
                        t["model"] = best_model
            except Exception:
                pass
            finally:
                conn.close()

        _trade_cache.update(data=trades, ts=now)
        return trades

    except Exception as e:
        log.error(f"_fetch_binance_trades error: {e}")
        return _trade_cache.get("data") or []


# ══════════════════════════════════════════════════════════════
# TRADING ENDPOINTS
# ══════════════════════════════════════════════════════════════

@api_bp.route("/trading/positions")
def trading_positions():
    """Open positions from Binance Testnet."""
    ex = _ex()
    if not ex:
        return jsonify({"positions": [], "error": "exchange not configured"})
    try:
        raw_positions = ex.get_open_positions()
        positions = []
        for p in raw_positions:
            pos_amt = float(p.get("positionAmt", 0))
            positions.append({
                "symbol": p["symbol"],
                "coin": p["symbol"].replace("USDT", ""),
                "direction": 1 if pos_amt > 0 else -1,
                "qty": abs(pos_amt),
                "entry_price": float(p.get("entryPrice", 0)),
                "mark_price": float(p.get("markPrice", 0)),
                "unrealized_pnl": float(p.get("unRealizedProfit", 0)),
                "leverage": p.get("leverage", ""),
                "margin_type": p.get("marginType", ""),
            })
        return jsonify({"positions": positions})
    except Exception as e:
        log.error(f"trading_positions error: {e}")
        return jsonify({"positions": [], "error": str(e)})


@api_bp.route("/trading/trades")
def trading_trades():
    """Recent closed trades -- pulled directly from Binance Testnet."""
    limit = request.args.get("limit", 50, type=int)
    trades = _fetch_binance_trades()
    return jsonify({"trades": trades[:limit]})


@api_bp.route("/trading/equity")
def trading_equity():
    """Equity curve data."""
    limit = request.args.get("limit", 500, type=int)
    conn = _paper_db()
    if not conn:
        return jsonify({"equity": []})
    try:
        rows = conn.execute(
            "SELECT * FROM equity_curve ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        equity = [dict(r) for r in reversed(rows)]
        return jsonify({"equity": equity})
    finally:
        conn.close()


def _current_model(coin):
    """Get current model assignment for a coin."""
    m = COIN_CONFIGS.get(coin, {}).get("model", "?")
    if not m or m == "?":
        m = "v6" if coin in COINS_V6 else "v5" if coin in COINS_V5 else "v3"
    return m


def _get_sqlite_trades():
    """Get all trades from SQLite with model from signal_log.

    SQLite has the complete trade history (paper_trader records every trade).
    Model is resolved from signal_log OPEN entries (actual model at trade time).
    """
    conn = _paper_db()
    if not conn:
        return []
    try:
        # All trades
        trade_rows = conn.execute(
            "SELECT coin, direction, entry_time, exit_time, entry_price, exit_price, "
            "qty, pnl_net, fee_total, exit_reason, bars_held "
            "FROM trades ORDER BY exit_time DESC"
        ).fetchall()

        # Signal log for model attribution
        sig_rows = conn.execute(
            "SELECT coin, ts, model FROM signal_log "
            "WHERE action LIKE 'OPEN%' ORDER BY ts"
        ).fetchall()
        coin_signals = defaultdict(list)
        for s in sig_rows:
            coin_signals[s["coin"]].append((s["ts"], s["model"]))

        trades = []
        for r in trade_rows:
            d = dict(r)
            # Resolve model from signal_log
            sigs = coin_signals.get(d["coin"], [])
            model = "?"
            et = (d.get("exit_time") or "")[:19]
            for sig_ts, sig_model in reversed(sigs):
                if sig_ts[:19] <= et:
                    model = sig_model
                    break
            d["model"] = model
            d["pnl_net"] = d.get("pnl_net") or 0.0
            trades.append(d)
        return trades
    finally:
        conn.close()


@api_bp.route("/trading/stats")
def trading_stats():
    """Per-coin summary + overall stats -- from Binance (paginated, complete)."""
    trades = _fetch_binance_trades()

    # Live exchange balance (source of truth for total PnL)
    exchange_balance = None
    ex = _ex()
    if ex:
        try:
            exchange_balance = ex.get_account_balance()
        except Exception:
            pass

    # Overall
    total_trades = len(trades)
    wins = sum(1 for t in trades if t["pnl_net"] > 0)
    realized_pnl = sum(t["pnl_net"] for t in trades)
    total_pnl = round(exchange_balance - _INIT_EQUITY, 2) if exchange_balance else round(realized_pnl, 2)
    win_rate = round(100.0 * wins / total_trades, 1) if total_trades > 0 else 0

    # ALL income from Binance (REALIZED_PNL + COMMISSION + FUNDING_FEE)
    ex2 = _ex()
    _active_coins = set(COINS_V3 + COINS_V5)
    _removed_set = set(COINS_REMOVED)
    model_income = defaultdict(float)  # keyed by: v3, v5, old
    coin_income = defaultdict(float)   # keyed by coin (active coins only)
    if ex2:
        try:
            all_income = ex2.get_all_income(start_time=PAPER_TRADING_START_MS)
            for r in all_income:
                sym = r.get("symbol", "")
                coin = sym.replace("USDT", "") if sym else ""
                if not coin:
                    continue
                inc = float(r.get("income", 0))
                # Removed/old coins → all into "old"
                if coin in _removed_set:
                    model_income["old"] += inc
                elif coin in _active_coins:
                    cur_m = _current_model(coin)
                    model_income[cur_m] += inc
                    coin_income[coin] += inc
                else:
                    model_income["old"] += inc  # unknown coins → old
        except Exception as e:
            log.error(f"get_all_income failed: {e}")

    # Trade counts — active coins tracked individually, rest → _old
    coin_trades = defaultdict(lambda: {"trades": 0, "wins": 0})
    for t in trades:
        c = t["coin"]
        if c in _active_coins:
            coin_trades[c]["trades"] += 1
            if t["pnl_net"] > 0:
                coin_trades[c]["wins"] += 1
        else:
            coin_trades["_old"]["trades"] += 1
            if t["pnl_net"] > 0:
                coin_trades["_old"]["wins"] += 1

    # Per coin: active coins only
    per_coin = []
    for coin in _active_coins:
        pnl = coin_income.get(coin, 0.0)
        ct = coin_trades.get(coin, {}).get("trades", 0)
        cw = coin_trades.get(coin, {}).get("wins", 0)
        wr = round(100.0 * cw / ct, 1) if ct > 0 else 0
        cur_m = _current_model(coin)
        per_coin.append({
            "coin": coin,
            "model": cur_m,
            "trades": ct,
            "wins": cw,
            "win_rate": wr,
            "total_pnl": round(pnl, 2),
            "avg_pnl": round(pnl / ct, 2) if ct > 0 else 0,
        })
    per_coin.sort(key=lambda x: x["total_pnl"], reverse=True)

    # Model stats — v3, v5, old (no v6/removed)
    model_trades_agg = defaultdict(lambda: {"trades": 0, "wins": 0})
    for coin in _active_coins:
        cur_m = _current_model(coin)
        model_trades_agg[cur_m]["trades"] += coin_trades.get(coin, {}).get("trades", 0)
        model_trades_agg[cur_m]["wins"] += coin_trades.get(coin, {}).get("wins", 0)
    # Old = removed coins trade counts
    _old_td = coin_trades.get("_old", {"trades": 0, "wins": 0})

    def _fmt_agg(pnl, td):
        return {
            "trades": td["trades"],
            "wins": td["wins"],
            "pnl": round(pnl, 2),
            "win_rate": round(100.0 * td["wins"] / td["trades"], 1) if td["trades"] > 0 else 0,
        }

    _zt = {"trades": 0, "wins": 0}
    model_stats = {
        "v3": _fmt_agg(model_income.get("v3", 0), model_trades_agg.get("v3", _zt)),
        "v5": _fmt_agg(model_income.get("v5", 0), model_trades_agg.get("v5", _zt)),
        "old": _fmt_agg(model_income.get("old", 0), _old_td),
    }

    # Latest equity + BTC score (still from SQLite -- used for gauge)
    latest = {}
    meta = {}
    conn = _paper_db()
    if conn:
        try:
            row = conn.execute(
                "SELECT * FROM equity_curve ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            if row:
                latest = dict(row)
            meta_rows = conn.execute("SELECT key, value FROM state_meta").fetchall()
            meta = {r["key"]: r["value"] for r in meta_rows}
        finally:
            conn.close()

    return jsonify({
        "stats": {
            "total_trades": total_trades,
            "wins": wins,
            "total_pnl": total_pnl,
            "realized_pnl": round(realized_pnl, 2),
            "win_rate": win_rate,
            "model": (_mr().get_champion() or {}).get("version", "?"),
        },
        "model_stats": model_stats,
        "coin_list": {"v3": COINS_V3, "v5": COINS_V5},
        "per_coin": per_coin,
        "latest": latest,
        "meta": meta,
        "exchange_balance": exchange_balance,
    })


@api_bp.route("/trading/health")
def trading_health():
    """Per-coin health status from coin_health table."""
    conn = _paper_db()
    if not conn:
        return jsonify({"health": [], "error": "no database"})
    try:
        rows = conn.execute(
            "SELECT * FROM coin_health ORDER BY health_score ASC"
        ).fetchall()
        health = []
        for r in rows:
            d = dict(r)
            if d.get("metrics"):
                import json as _json
                d["metrics"] = _json.loads(d["metrics"])
            health.append(d)

        summary = {
            "total": len(health),
            "healthy": len([h for h in health if h["status"] == "HEALTHY"]),
            "warning": len([h for h in health if h["status"] == "WARNING"]),
            "paused": len([h for h in health if h["status"] == "PAUSED"]),
            "cold_start": len([h for h in health if h["status"] == "COLD_START"]),
        }
        return jsonify({"health": health, "summary": summary})
    except Exception as e:
        return jsonify({"health": [], "error": str(e)})
    finally:
        conn.close()


@api_bp.route("/trading/signals")
def trading_signals():
    """Recent signal log."""
    limit = request.args.get("limit", 100, type=int)
    conn = _paper_db()
    if not conn:
        return jsonify({"signals": []})
    try:
        rows = conn.execute(
            "SELECT * FROM signal_log ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        return jsonify({"signals": [dict(r) for r in rows]})
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════
# RESEARCH ENDPOINTS
# ══════════════════════════════════════════════════════════════

@api_bp.route("/research/factors")
def research_factors():
    """Factor registry."""
    fr = _fr()
    factors = fr.get_all()
    summary = {
        "total": len(factors),
        "production": len([f for f in factors if f["status"] == "production"]),
        "tested_positive": len([f for f in factors if f["status"] == "tested_positive"]),
        "tested_negative": len([f for f in factors if f["status"] == "tested_negative"]),
        "untested": len([f for f in factors if f["status"] == "untested"]),
    }
    return jsonify({"factors": factors, "summary": summary})


@api_bp.route("/research/experiments")
def research_experiments():
    """Experiment history."""
    limit = request.args.get("limit", 50, type=int)
    if EXPERIMENT_REGISTRY_PATH.exists():
        with open(EXPERIMENT_REGISTRY_PATH) as fh:
            experiments = json.load(fh)
        return jsonify({"experiments": experiments[-limit:]})
    return jsonify({"experiments": []})


@api_bp.route("/research/leaderboard")
def research_leaderboard():
    """Model rankings."""
    lb = build_leaderboard(_mr())
    champion = _mr().get_champion()
    return jsonify({
        "leaderboard": lb,
        "champion": champion,
    })


@api_bp.route("/research/models")
def research_models():
    """All model versions with full detail."""
    return jsonify({"models": _mr().get_all()})


@api_bp.route("/research/missions")
def research_missions():
    """Daily missions data."""
    me = _me()
    return jsonify({
        "missions": me.get_recent(50),
        "meta": me.get_meta(),
        "today": me.get_today_missions(),  # list of 0-1 missions
    })


# ══════════════════════════════════════════════════════════════
# DATA HEALTH ENDPOINTS
# ══════════════════════════════════════════════════════════════

@api_bp.route("/data/health")
def data_health():
    """Data collector staleness check."""
    try:
        from research.config import get_pg_conn
        conn = get_pg_conn()
        cur = conn.cursor()

        tables = [
            ("basis", "market_data.basis", "ts"),
            ("order_book", "market_data.order_book_raw", "fetched_at"),
            ("liquidation", "market_data.liquidation", "event_time"),
            ("premium", "market_data.premium_index", "ts"),
            ("funding", "market_data.funding_rate_alt", "ts"),
            ("open_interest", "market_data.open_interest", "ts"),
            ("cvd", "market_data.cvd", "ts"),
        ]

        health = []
        now = datetime.utcnow()
        for name, table, ts_col in tables:
            try:
                cur.execute(f"SELECT MAX({ts_col}) as latest, COUNT(*) as total FROM {table}")
                row = cur.fetchone()
                latest = row[0]
                total = row[1]
                if latest:
                    # DB stores in BKK timezone, subtract 7h
                    if hasattr(latest, 'tzinfo') and latest.tzinfo:
                        latest = latest.replace(tzinfo=None)
                    latest_utc = latest - timedelta(hours=7)
                    staleness_h = (now - latest_utc).total_seconds() / 3600
                    status = "live" if staleness_h < 1 else ("stale" if staleness_h < 24 else "down")
                else:
                    staleness_h = None
                    status = "down"

                health.append({
                    "name": name,
                    "table": table,
                    "status": status,
                    "latest_utc": latest_utc.isoformat() if latest else None,
                    "staleness_hours": round(staleness_h, 1) if staleness_h is not None else None,
                    "total_rows": total,
                })
            except Exception as e:
                conn.rollback()
                health.append({
                    "name": name,
                    "table": table,
                    "status": "error",
                    "error": str(e),
                })

        conn.close()
        return jsonify({"health": health})
    except Exception as e:
        return jsonify({"health": [], "error": str(e)})


@api_bp.route("/data/stats")
def data_stats():
    """Row counts and freshness per table."""
    return data_health()  # same info


# ══════════════════════════════════════════════════════════════
# SNAPSHOT (for WebSocket push)
# ══════════════════════════════════════════════════════════════

def _get_snapshot():
    """Build full dashboard snapshot for WebSocket push."""
    snapshot = {"ts": datetime.utcnow().isoformat()}

    # Exchange positions
    ex = _ex()
    if ex:
        try:
            raw_pos = ex.get_open_positions()
            snapshot["positions"] = [
                {
                    "symbol": p["symbol"],
                    "coin": p["symbol"].replace("USDT", ""),
                    "qty": abs(float(p.get("positionAmt", 0))),
                    "unrealized_pnl": float(p.get("unRealizedProfit", 0)),
                    "entry_price": float(p.get("entryPrice", 0)),
                }
                for p in raw_pos
            ]
            snapshot["exchange_balance"] = ex.get_account_balance()
        except Exception as e:
            log.warning(f"snapshot exchange error: {e}")
            snapshot["positions"] = []

    # Trades + income from Binance
    binance_trades = _fetch_binance_trades()
    snapshot["recent_trades"] = binance_trades[:50]

    # Income split: active coins → v3/v5, removed coins → old
    snap_model_income = defaultdict(float)
    snap_coin_income = defaultdict(float)
    snap_ex2 = _ex()
    _snap_active = set(COINS_V3 + COINS_V5)
    _snap_removed = set(COINS_REMOVED)
    if snap_ex2:
        try:
            all_income = snap_ex2.get_all_income(start_time=PAPER_TRADING_START_MS)
            for r in all_income:
                sym = r.get("symbol", "")
                coin = sym.replace("USDT", "") if sym else ""
                if not coin:
                    continue
                inc = float(r.get("income", 0))
                if coin in _snap_removed or coin not in _snap_active:
                    snap_model_income["old"] += inc
                else:
                    cur_m = _current_model(coin)
                    snap_model_income[cur_m] += inc
                    snap_coin_income[coin] += inc
        except Exception:
            pass

    # Trade counts — active vs removed
    coin_trades = defaultdict(lambda: {"trades": 0, "wins": 0})
    for t in binance_trades:
        c = t["coin"]
        if c in _snap_removed or c not in _snap_active:
            coin_trades["_old"]["trades"] += 1
            if t["pnl_net"] > 0:
                coin_trades["_old"]["wins"] += 1
        else:
            coin_trades[c]["trades"] += 1
            if t["pnl_net"] > 0:
                coin_trades[c]["wins"] += 1

    snapshot["coin_stats"] = []
    for coin in sorted(_snap_active, key=lambda c: snap_coin_income.get(c, 0), reverse=True):
        pnl = snap_coin_income.get(coin, 0.0)
        ct = coin_trades.get(coin, {}).get("trades", 0)
        cw = coin_trades.get(coin, {}).get("wins", 0)
        wr = round(100.0 * cw / ct, 1) if ct > 0 else 0
        snapshot["coin_stats"].append({
            "coin": coin, "model": _current_model(coin),
            "trades": ct, "wins": cw,
            "win_rate": wr, "total_pnl": round(pnl, 2),
            "avg_pnl": round(pnl / ct, 2) if ct > 0 else 0,
        })

    # Model stats — v3, v5, old
    model_trades_agg = defaultdict(lambda: {"trades": 0, "wins": 0})
    for coin in _snap_active:
        cur_m = _current_model(coin)
        model_trades_agg[cur_m]["trades"] += coin_trades.get(coin, {}).get("trades", 0)
        model_trades_agg[cur_m]["wins"] += coin_trades.get(coin, {}).get("wins", 0)
    _old_td = coin_trades.get("_old", {"trades": 0, "wins": 0})

    def _snap_fmt(pnl, td):
        return {
            "trades": td["trades"], "wins": td["wins"],
            "pnl": round(pnl, 2),
            "win_rate": round(100.0 * td["wins"] / td["trades"], 1) if td["trades"] > 0 else 0,
        }

    _zt = {"trades": 0, "wins": 0}
    snapshot["model_stats"] = {
        "v3": _snap_fmt(snap_model_income.get("v3", 0), model_trades_agg.get("v3", _zt)),
        "v5": _snap_fmt(snap_model_income.get("v5", 0), model_trades_agg.get("v5", _zt)),
        "old": _snap_fmt(snap_model_income.get("old", 0), _old_td),
    }

    # Equity curve (still from SQLite -- for BTC score gauge)
    conn = _paper_db()
    if conn:
        try:
            eq = conn.execute(
                "SELECT * FROM equity_curve ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            snapshot["latest_equity"] = dict(eq) if eq else {}
        finally:
            conn.close()

    # Coin list
    snapshot["coin_list"] = {"v3": COINS_V3, "v5": COINS_V5}

    # Research data
    snapshot["champion"] = _mr().get_champion()
    snapshot["factor_summary"] = {
        "production": len(_fr().get_production_factors()),
        "tested_positive": len(_fr().get_by_status("tested_positive")),
        "tested_negative": len(_fr().get_by_status("tested_negative")),
        "untested": len(_fr().get_untested()),
    }

    return snapshot
