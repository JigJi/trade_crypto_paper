"""
SQLite Persistence for Paper Trading
======================================
Tables: trades, exchange_trades, equity_curve, signal_log, state_meta
Note: positions are now managed by Binance Testnet, not SQLite.
"""

import sqlite3
import json
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .config import SQLITE_PATH


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(SQLITE_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def _conn():
    """Context manager that guarantees connection is closed even on exception."""
    conn = get_conn()
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    """Create all tables if they don't exist."""
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            coin TEXT NOT NULL,
            direction INTEGER NOT NULL,
            entry_time TEXT NOT NULL,
            entry_price REAL NOT NULL,
            exit_time TEXT NOT NULL,
            exit_price REAL NOT NULL,
            qty REAL NOT NULL,
            pnl_gross REAL NOT NULL,
            pnl_net REAL NOT NULL,
            fee_total REAL NOT NULL,
            exit_reason TEXT NOT NULL,
            btc_score_entry REAL,
            btc_score_exit REAL,
            equity_after REAL NOT NULL,
            bars_held INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS exchange_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            coin TEXT NOT NULL,
            symbol TEXT NOT NULL,
            order_id TEXT NOT NULL,
            side TEXT NOT NULL,
            type TEXT NOT NULL,
            qty REAL NOT NULL,
            price REAL NOT NULL,
            realized_pnl REAL,
            commission REAL,
            commission_asset TEXT,
            ts TEXT NOT NULL,
            raw_json TEXT            -- full exchange response
        );

        CREATE TABLE IF NOT EXISTS equity_curve (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            equity REAL NOT NULL,
            btc_price REAL,
            xrp_price REAL,
            ada_price REAL,
            btc_score REAL,
            open_positions TEXT    -- JSON list of open position coins
        );

        CREATE TABLE IF NOT EXISTS signal_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            coin TEXT NOT NULL,
            btc_score REAL NOT NULL,
            signal INTEGER NOT NULL,       -- -1, 0, 1
            pa_aligned INTEGER,            -- 0/1 or NULL
            has_position INTEGER NOT NULL,  -- 0/1
            cooldown_ok INTEGER NOT NULL,   -- 0/1
            action TEXT NOT NULL,           -- OPEN_LONG, OPEN_SHORT, HOLD, SKIP_COOLDOWN, SKIP_PA, etc.
            model TEXT                      -- v3, v5, or v6
        );

        CREATE TABLE IF NOT EXISTS state_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS coin_health (
            coin TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            health_score REAL NOT NULL,
            metrics TEXT,
            diagnosis TEXT,
            paused_at TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS coin_health_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            coin TEXT NOT NULL,
            health_score REAL NOT NULL,
            status TEXT NOT NULL,
            metrics TEXT
        );
    """)
    conn.commit()

    # Migrations: add columns to existing tables
    _migrate_add_column(conn, "signal_log", "model", "TEXT")
    conn.close()


def _migrate_add_column(conn, table: str, column: str, col_type: str):
    """Add column to existing table if it doesn't exist (SQLite migration)."""
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # Column already exists


# ---- Trade records ----

def insert_trade(trade: dict):
    with _conn() as conn:
        conn.execute("""
            INSERT INTO trades (coin, direction, entry_time, entry_price, exit_time,
                exit_price, qty, pnl_gross, pnl_net, fee_total, exit_reason,
                btc_score_entry, btc_score_exit, equity_after, bars_held)
            VALUES (:coin, :direction, :entry_time, :entry_price, :exit_time,
                :exit_price, :qty, :pnl_gross, :pnl_net, :fee_total, :exit_reason,
                :btc_score_entry, :btc_score_exit, :equity_after, :bars_held)
        """, trade)
        conn.commit()


def get_zero_pnl_trades() -> list[dict]:
    """Get trades where pnl_gross = 0 (need reconciliation from Binance)."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT t.*, et.order_id FROM trades t "
            "LEFT JOIN exchange_trades et ON t.coin = et.coin "
            "AND et.type IN ('TIMEOUT', 'SIGNAL_FLIP') "
            "AND et.ts BETWEEN t.exit_time AND datetime(t.exit_time, '+5 minutes') "
            "WHERE t.pnl_gross = 0 AND t.pnl_net = 0 "
            "ORDER BY t.exit_time"
        ).fetchall()
        return [dict(r) for r in rows]


def update_trade_pnl(trade_id: int, pnl_gross: float, pnl_net: float,
                     fee_total: float, exit_price: float = None):
    """Update PnL for a trade (used by reconciliation)."""
    with _conn() as conn:
        if exit_price and exit_price > 0:
            conn.execute(
                "UPDATE trades SET pnl_gross=?, pnl_net=?, fee_total=?, exit_price=? "
                "WHERE id=?",
                (round(pnl_gross, 4), round(pnl_net, 4), round(fee_total, 4),
                 round(exit_price, 6), trade_id)
            )
        else:
            conn.execute(
                "UPDATE trades SET pnl_gross=?, pnl_net=?, fee_total=? WHERE id=?",
                (round(pnl_gross, 4), round(pnl_net, 4), round(fee_total, 4), trade_id)
            )
        conn.commit()


def get_trades(since: str = None) -> list[dict]:
    with _conn() as conn:
        if since:
            rows = conn.execute(
                "SELECT * FROM trades WHERE exit_time >= ? ORDER BY exit_time", (since,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM trades ORDER BY exit_time").fetchall()
        return [dict(r) for r in rows]


# ---- Exchange trade backup ----

def insert_exchange_trade(trade: dict):
    """Save raw exchange trade data as backup."""
    with _conn() as conn:
        conn.execute("""
            INSERT INTO exchange_trades (coin, symbol, order_id, side, type,
                qty, price, realized_pnl, commission, commission_asset, ts, raw_json)
            VALUES (:coin, :symbol, :order_id, :side, :type,
                :qty, :price, :realized_pnl, :commission, :commission_asset, :ts, :raw_json)
        """, trade)
        conn.commit()


# ---- Equity curve ----

def insert_equity_snapshot(ts: str, equity: float, btc_price: float = None,
                           xrp_price: float = None, ada_price: float = None,
                           btc_score: float = None, open_positions: list = None):
    with _conn() as conn:
        conn.execute("""
            INSERT INTO equity_curve (ts, equity, btc_price, xrp_price, ada_price,
                btc_score, open_positions)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (ts, equity, btc_price, xrp_price, ada_price, btc_score,
              json.dumps(open_positions) if open_positions else None))
        conn.commit()


def get_equity_curve(since: str = None) -> list[dict]:
    with _conn() as conn:
        if since:
            rows = conn.execute(
                "SELECT * FROM equity_curve WHERE ts >= ? ORDER BY ts", (since,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM equity_curve ORDER BY ts").fetchall()
        return [dict(r) for r in rows]


# ---- Signal log ----

def insert_signal_log(entry: dict):
    with _conn() as conn:
        conn.execute("""
            INSERT INTO signal_log (ts, coin, btc_score, signal, pa_aligned,
                has_position, cooldown_ok, action, model)
            VALUES (:ts, :coin, :btc_score, :signal, :pa_aligned,
                :has_position, :cooldown_ok, :action, :model)
        """, entry)
        conn.commit()


# ---- State meta (key-value) ----

def get_meta(key: str, default: str = None) -> str | None:
    with _conn() as conn:
        row = conn.execute("SELECT value FROM state_meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_meta(key: str, value: str):
    with _conn() as conn:
        if value is None:
            conn.execute("DELETE FROM state_meta WHERE key=?", (key,))
        else:
            conn.execute(
                "INSERT INTO state_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value)
            )
        conn.commit()


def get_current_equity() -> float:
    """Get current equity from latest trade or initial equity."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT equity_after FROM trades ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row:
            return row["equity_after"]
    return float(get_meta("init_equity", str(10_000.0)))


# ---- Coin Health ----

def upsert_coin_health(coin: str, status: str, health_score: float,
                       metrics: dict = None, diagnosis: str = None,
                       paused_at: str = None):
    """Insert or update current health state for a coin."""
    with _conn() as conn:
        now_str = datetime.utcnow().isoformat()
        conn.execute("""
            INSERT INTO coin_health (coin, status, health_score, metrics, diagnosis,
                paused_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(coin) DO UPDATE SET
                status=excluded.status,
                health_score=excluded.health_score,
                metrics=excluded.metrics,
                diagnosis=excluded.diagnosis,
                paused_at=CASE
                    WHEN excluded.status='PAUSED' AND coin_health.status!='PAUSED'
                    THEN excluded.paused_at
                    WHEN excluded.status='PAUSED' AND coin_health.status='PAUSED'
                    THEN coin_health.paused_at
                    ELSE NULL
                END,
                updated_at=excluded.updated_at
        """, (coin, status, round(health_score, 2),
              json.dumps(metrics) if metrics else None,
              diagnosis, paused_at or now_str, now_str))
        conn.commit()


def get_coin_health(coin: str) -> dict | None:
    """Get current health state for a coin."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM coin_health WHERE coin=?", (coin,)
        ).fetchone()
        if row:
            d = dict(row)
            if d.get("metrics"):
                d["metrics"] = json.loads(d["metrics"])
            return d
        return None


def get_all_coin_health() -> list[dict]:
    """Get health state for all coins."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM coin_health ORDER BY health_score ASC"
        ).fetchall()
        results = []
        for row in rows:
            d = dict(row)
            if d.get("metrics"):
                d["metrics"] = json.loads(d["metrics"])
            results.append(d)
        return results


def insert_health_history(coin: str, health_score: float, status: str,
                          metrics: dict = None):
    """Append health snapshot for trend tracking."""
    with _conn() as conn:
        conn.execute("""
            INSERT INTO coin_health_history (ts, coin, health_score, status, metrics)
            VALUES (?, ?, ?, ?, ?)
        """, (datetime.utcnow().isoformat(), coin, round(health_score, 2), status,
              json.dumps(metrics) if metrics else None))
        conn.commit()


def get_trades_for_coin(coin: str, limit: int = None) -> list[dict]:
    """Get closed trades for a specific coin (newest first)."""
    with _conn() as conn:
        sql = "SELECT * FROM trades WHERE coin=? ORDER BY id DESC"
        if limit:
            sql += f" LIMIT {limit}"
        rows = conn.execute(sql, (coin,)).fetchall()
        return [dict(r) for r in rows]
