"""Check latest timestamps and row counts for all v3 model tables."""

import os
from dotenv import load_dotenv
import psycopg2
from tabulate import tabulate

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

conn = psycopg2.connect(
    dbname=os.getenv("PG_DB"),
    user=os.getenv("PG_USER"),
    password=os.getenv("PG_PASS"),
    host=os.getenv("PG_HOST"),
    port=os.getenv("PG_PORT"),
)

queries = [
    # --- v3 model tables ---
    ("public.liquidation", "created_at", "coin='BTC'", "v3"),
    ("public.funding_rate", "date", "symbol='BTCUSDT'", "v3"),
    ("market_data.order_book_raw", "fetched_at", None, "v3"),
    ("public.etf_btc", "date", None, "v3"),
    ("market_data.basis", "ts", "pair='BTCUSDT'", "v3"),
    ("market_data.liquidation", "event_time", "symbol='BTCUSDT'", "v3"),
    ("market_data.open_interest", "ts", "symbol='BTCUSDT'", "v3"),
    ("public.whale_alert", "alert_time", "symbol='BTC'", "v3"),
    # --- dropped from v3, still in paper trading ---
    ("market_data.taker_volume", "ts", "symbol='BTCUSDT'", "dropped"),
    ("market_data.long_short_ratio", "ts", "symbol='BTCUSDT'", "dropped"),
    ("public.fear_greed", "created_at", None, "dropped"),
    ("market_data.premium_index", "ts", "symbol='BTCUSDT'", "dropped"),
]

results = []
cur = conn.cursor()

for table, ts_col, where, group in queries:
    where_clause = f"WHERE {where}" if where else ""
    sql = f"SELECT MAX({ts_col}), COUNT(*) FROM {table} {where_clause}"
    try:
        cur.execute(sql)
        latest, count = cur.fetchone()
        results.append((group, table, ts_col, where or "—", str(latest), f"{count:,}"))
    except Exception as e:
        conn.rollback()
        results.append((group, table, ts_col, where or "—", f"ERROR: {e}", "—"))

cur.close()
conn.close()

headers = ["Group", "Table", "Column", "Filter", "Latest Timestamp", "Total Rows"]
print(tabulate(results, headers=headers, tablefmt="grid"))
