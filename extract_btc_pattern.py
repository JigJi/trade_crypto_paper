import os
import psycopg2
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone
from binance.client import Client
import time

# Load environment variables
load_dotenv()

def get_db_connection():
    """Establish a connection to the PostgreSQL database."""
    try:
        conn = psycopg2.connect(
            dbname=os.getenv('PG_DB'),
            user=os.getenv('PG_USER'),
            password=os.getenv('PG_PASS'),
            host=os.getenv('PG_HOST'),
            port=os.getenv('PG_PORT')
        )
        return conn
    except Exception as e:
        print(f"Error connecting to database: {e}")
        return None

def get_binance_client():
    """Initialize the Binance API client."""
    api_key = os.getenv('BINANCE_KEY')
    api_secret = os.getenv('BINANCE_SECRET')
    return Client(api_key, api_secret)

def analyze_btc_pattern():
    """
    Extract BTCUSDT data where 'fvg' and 'liquidity_sweeps' occurred 
    in the same 5-minute window in the last year, then compare with Binance.
    """
    conn = get_db_connection()
    if not conn:
        return

    binance = get_binance_client()

    try:
        with conn.cursor() as cur:
            # Query to get the exact time and close price for each match
            query = """
            WITH fvg_windows AS (
                SELECT DISTINCT floor(extract(epoch from fvg_time) / 300) as window_id
                FROM fvg
                WHERE symbol = 'BTCUSDT' AND fvg_time >= NOW() - INTERVAL '1 year'
            ),
            liq_windows AS (
                SELECT 
                    floor(extract(epoch from liq_time) / 300) as window_id,
                    liq_time,
                    close
                FROM liquidity_sweeps
                WHERE symbol = 'BTCUSDT' AND liq_time >= NOW() - INTERVAL '1 year'
            )
            SELECT l.liq_time, l.close
            FROM liq_windows l
            JOIN fvg_windows f ON f.window_id = l.window_id
            ORDER BY l.liq_time DESC;
            """
            
            print("Fetching pattern occurrences from database...")
            cur.execute(query)
            matches = cur.fetchall()
            total_matches = len(matches)

            print(f"Found {total_matches} occurrences. Starting Binance data integrity check...")
            print("-" * 75)
            print(f"{'Time (UTC)':<20} | {'DB Close':<12} | {'Binance Close':<15} | {'Diff'}")
            print("-" * 75)

            discrepancies = 0
            for liq_time, db_close in matches:
                # Ensure liq_time is UTC-aware for accurate API requests
                if liq_time.tzinfo is None:
                    liq_time = liq_time.replace(tzinfo=timezone.utc)
                
                # Create a small window (e.g., 5 minutes before/after)
                start_ts = int((liq_time - timedelta(minutes=5)).timestamp() * 1000)
                end_ts = int((liq_time + timedelta(minutes=5)).timestamp() * 1000)
                
                try:
                    # Use integer timestamps (ms) for more reliable fetching
                    klines = binance.get_historical_klines("BTCUSDT", Client.KLINE_INTERVAL_1MINUTE, start_ts, end_ts)
                    
                    binance_close = None
                    target_ts_ms = int(liq_time.timestamp() * 1000)
                    
                    for k in klines:
                        # Match the exact minute open time
                        if k[0] == target_ts_ms:
                            binance_close = float(k[4])
                            break
                    
                    if binance_close is not None:
                        diff = abs(float(db_close) - binance_close)
                        print(f"{liq_time.strftime('%Y-%m-%d %H:%M:%S'):<20} | {float(db_close):<12.2f} | {binance_close:<15.2f} | {diff:.2f}")
                        if diff > 1.0:
                            discrepancies += 1
                    else:
                        print(f"{liq_time.strftime('%Y-%m-%d %H:%M:%S'):<20} | {float(db_close):<12.2f} | {'Not Found':<15} | N/A")
                
                except Exception as e:
                    print(f"Error fetching from Binance for {liq_time}: {e}")
                
                time.sleep(0.05) # Minor throttle

            print("-" * 75)
            print(f"Summary:")
            print(f"Total Patterns Analyzed: {total_matches}")
            print(f"Discrepancies found (>1.0): {discrepancies}")
            print("-" * 75)

    except psycopg2.Error as e:
        print(f"Database error: {e}")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    analyze_btc_pattern()
