"""
Whale Alert + News Collector (Optional)
=========================================
Scrapes Telegram whale_alert_io + Cointelegraph channels via Telethon.
Gracefully skips if telethon not installed or session missing.
"""

import re
import logging
import asyncio
from datetime import datetime, timezone, timedelta

import psycopg2

from .config import DB_PARAMS, TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_SESSION_PATH

logger = logging.getLogger("data_collector.whale")


def collect_whale_alerts(conn) -> dict:
    """
    Fetch recent whale alerts from Telegram.
    Runs async internally. Returns result dict.
    Gracefully skips if telethon unavailable.
    """
    try:
        from telethon import TelegramClient
    except ImportError:
        logger.info("[whale] telethon not installed, skipping")
        return {"status": "skipped", "reason": "telethon not installed"}

    try:
        import sys
        if sys.platform.startswith("win"):
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                _fetch_and_insert_whale_alerts(conn)
            )
        finally:
            loop.close()
        return result

    except Exception as e:
        logger.error(f"[whale] ERROR: {e}")
        return {"status": "error", "error": str(e)}


async def _fetch_and_insert_whale_alerts(conn) -> dict:
    """Async: connect to Telegram, fetch messages, parse, insert."""
    from telethon import TelegramClient

    client = TelegramClient(
        TELEGRAM_SESSION_PATH,
        int(TELEGRAM_API_ID),
        TELEGRAM_API_HASH,
    )

    try:
        await client.start()
        messages = await client.get_messages("whale_alert_io", limit=20)
    except Exception as e:
        logger.error(f"[whale] Telegram connect/fetch failed: {e}")
        return {"status": "error", "error": str(e)}
    finally:
        await client.disconnect()

    if not messages:
        return {"status": "ok", "rows": 0}

    # Parse messages
    parsed = []
    for msg in messages:
        if not msg.message:
            continue
        text = msg.message.strip()
        fire_level = text.count('\U0001f6a8')  # siren emoji
        amount_match = re.search(r'([\d,\.]+)\s+#([A-Z0-9]+)', text)
        usd_match = re.search(r'\(([\d,\.]+)\s*USD\)', text)

        amount = float(amount_match.group(1).replace(',', '')) if amount_match else 0
        symbol = amount_match.group(2) if amount_match else "unknown"
        usd_value = float(usd_match.group(1).replace(',', '')) if usd_match else 0

        from_match = re.search(r'transferred from (.*?) to', text)
        to_match = re.search(r'to\s+#?([A-Za-z0-9_-]+)', text)

        from_addr = from_match.group(1).replace('#', '').replace('wallet', '').strip() if from_match else "unknown"
        to_addr = to_match.group(1).replace('#', '').replace('wallet', '').strip() if to_match else "unknown"

        sentiment = "neutral"
        direction = "unknown"
        whale_type = "transfer" if "transfer" in text else "other"

        if "to Binance" in text or "to Coinbase" in text or to_addr in ["Binance", "Coinbase"]:
            sentiment = "bearish"
            direction = "inflow"
        elif "from Binance" in text or "from Coinbase" in text or from_addr in ["Binance", "Coinbase"]:
            sentiment = "bullish"
            direction = "outflow"

        # Use UTC time from Telegram message
        alert_time = msg.date.astimezone(timezone.utc)

        parsed.append((
            alert_time, fire_level, symbol, amount, usd_value,
            from_addr, to_addr, sentiment, direction, whale_type, text, datetime.utcnow(),
        ))

    # Insert to DB
    if not parsed:
        return {"status": "ok", "rows": 0}

    sql = """
    INSERT INTO whale_alert
        (alert_time, fire_level, symbol, amount, usd_value, "from", "to",
         sentiment, direction, whale_type, raw_data, created_at)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT ON CONSTRAINT unique_alert_key DO NOTHING;
    """
    inserted = 0
    with conn.cursor() as cur:
        for row in parsed:
            try:
                cur.execute("SAVEPOINT sp_whale")
                cur.execute(sql, row)
                cur.execute("RELEASE SAVEPOINT sp_whale")
                inserted += 1
            except Exception as e:
                cur.execute("ROLLBACK TO SAVEPOINT sp_whale")
                logger.debug(f"[whale] skip duplicate or error: {e}")

    conn.commit()
    logger.info(f"[whale] OK: {inserted}/{len(parsed)} alerts inserted")
    return {"status": "ok", "rows": inserted}


# ============================================================
# News from Cointelegraph (from scrape_telegram.py cointele())
# ============================================================

def collect_news(conn) -> dict:
    """
    Fetch recent news from Cointelegraph Telegram channel.
    Gracefully skips if telethon unavailable.
    """
    try:
        from telethon import TelegramClient
    except ImportError:
        logger.info("[news] telethon not installed, skipping")
        return {"status": "skipped", "reason": "telethon not installed"}

    try:
        import sys
        if sys.platform.startswith("win"):
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(_fetch_and_insert_news(conn))
        finally:
            loop.close()
        return result

    except Exception as e:
        logger.error(f"[news] ERROR: {e}")
        return {"status": "error", "error": str(e)}


async def _fetch_and_insert_news(conn) -> dict:
    """Async: connect to Telegram, fetch Cointelegraph messages, parse, insert."""
    from telethon import TelegramClient

    client = TelegramClient(
        TELEGRAM_SESSION_PATH,
        int(TELEGRAM_API_ID),
        TELEGRAM_API_HASH,
    )

    try:
        await client.start()
        messages = await client.get_messages("Cointelegraph", limit=20)
    except Exception as e:
        logger.error(f"[news] Telegram connect/fetch failed: {e}")
        return {"status": "error", "error": str(e)}
    finally:
        await client.disconnect()

    if not messages:
        return {"status": "ok", "rows": 0}

    parsed = []
    for msg in messages:
        if not msg.message:
            continue
        text = msg.message.strip()
        if text == "" or "Catch up on the news" in text:
            continue

        text_clean = re.sub(r'Read more:.*', '', text).strip()
        text_clean = re.sub(r'News \|.*', '', text_clean).strip()

        match = re.match(r'^(?:[^\w\s]*\s*)?([A-Z][A-Z\s]{1,20}):', text_clean)
        if not match:
            continue

        prefix = match.group(1).strip()
        content_match = re.match(r'^[^\w\s]*\s*[A-Z][A-Z\s]{1,20}:\s*(.*)', text_clean)
        content = content_match.group(1).strip() if content_match else text_clean.strip()

        # Simple sentiment detection
        raw_senti = text_clean.lower()
        lst_bull = ["buy", "added", "inflow", "pumped", "accumulate", "holding", "support", "bullish", "etf", "record"]
        lst_bear = ["hack", "dump", "scam", "exploit", "short", "bearish", "plunge", "fell", "loss", "risk-off", "lawsuit"]
        senti = 'neutral'
        if any(x in raw_senti for x in lst_bull):
            senti = 'bullish'
        elif any(x in raw_senti for x in lst_bear):
            senti = 'bearish'

        news_time = msg.date.astimezone(timezone.utc)
        parsed.append((news_time, prefix, content, senti, datetime.utcnow()))

    if not parsed:
        return {"status": "ok", "rows": 0}

    sql = """
    INSERT INTO news_crypto (news_time, prefix, content, sentiment, created_at)
    VALUES (%s, %s, %s, %s, %s)
    ON CONFLICT ON CONSTRAINT unique_key DO NOTHING;
    """
    inserted = 0
    with conn.cursor() as cur:
        for row in parsed:
            try:
                cur.execute("SAVEPOINT sp_news")
                cur.execute(sql, row)
                cur.execute("RELEASE SAVEPOINT sp_news")
                inserted += 1
            except Exception as e:
                cur.execute("ROLLBACK TO SAVEPOINT sp_news")
                logger.debug(f"[news] skip: {e}")

    conn.commit()
    logger.info(f"[news] OK: {inserted}/{len(parsed)} news inserted")
    return {"status": "ok", "rows": inserted}
