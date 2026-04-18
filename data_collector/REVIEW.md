# รีวิว Data Collector -- ทั้ง 21 ตัว

**วันที่รีวิว:** 2026-03-11
**ไฟล์ที่ตรวจ:** `collectors.py`, `ws_liquidation.py`, `ws_cvd.py`, `aggregator.py`, `whale.py`, `health.py`, `daemon.py`, `config.py`

---

## สรุปภาพรวม

| ระดับ | จำนวน | รายละเอียด |
|-------|--------|------------|
| OK (ไม่มีปัญหา) | 12 ตัว | ทำงานถูกต้อง ไม่ต้องแก้ |
| WARNING (ควรปรับ) | 7 ตัว | ทำงานได้แต่มีจุดที่ควรปรับปรุง |
| ERROR (ต้องแก้) | 2 ตัว | มี bug ที่ต้องแก้ |

---

## ตัวที่ต้องแก้ (ERROR)

### 1. `fear_greed` -- ไม่มี dedup, insert ซ้ำทุกชั่วโมง

**ไฟล์:** `collectors.py` บรรทัด 1130-1152
**ปัญหา:** SQL INSERT ไม่มี `ON CONFLICT` หรือ dedup ใดๆ

```python
sql = """INSERT INTO fear_greed (created_at, score, description) VALUES (%s, %s, %s)"""
```

Fear & Greed Index อัพเดทวันละครั้ง แต่ collector รันทุกชั่วโมง (`:15`) ทำให้ได้ข้อมูลซ้ำ ~24 แถว/วัน ซึ่งจะสะสมเป็นข้อมูลซ้ำจำนวนมากในระยะยาว

**วิธีแก้:** เพิ่ม dedup โดยเช็คว่าวันนี้มีข้อมูลแล้วหรือยัง
```python
# ตรวจสอบว่าวันนี้มีข้อมูลแล้วหรือไม่
cur.execute("SELECT 1 FROM fear_greed WHERE created_at::date = CURRENT_DATE LIMIT 1")
if cur.fetchone():
    return {"status": "ok", "rows": 0, "note": "already have today's data"}
```

### 2. `cvd` (WebSocket) -- สร้าง DB connection ใหม่ทุกครั้งที่ upsert

**ไฟล์:** `ws_cvd.py` บรรทัด 69-91
**ปัญหา:** ฟังก์ชัน `_upsert_bucket()` สร้าง `psycopg2.connect()` ใหม่ทุกครั้งที่เรียก

```python
def _upsert_bucket(ts, source, buy_vol, sell_vol, trade_count, is_final):
    conn = psycopg2.connect(**DB_PARAMS)  # <-- สร้าง connection ใหม่ทุกครั้ง!
    ...
    conn.close()
```

ทุก 60 วินาที flusher จะ upsert ทุก bucket ที่ active (ปกติ 2 buckets: spot + futures) = สร้าง connection ใหม่ 2 ครั้ง/นาที
เมื่อ bucket เปลี่ยน (ทุก 15 นาที) จะ finalize bucket เก่า = สร้าง connection เพิ่มอีก

**ผลกระทบ:** สิ้นเปลือง connection (ไม่ถึง crash แต่ไม่ efficient)
**วิธีแก้:** เก็บ connection ไว้ใน class attribute, reconnect เมื่อ connection หลุด หรือใช้ connection pool

---

## ตัวที่ควรปรับ (WARNING)

### 3. `long_short_ratio` -- `raise_for_status()` อยู่นอก retry

**ไฟล์:** `collectors.py` บรรทัด 464-465
**ปัญหา:**

```python
resp = _retry(lambda u=url: requests.get(u, params=params, timeout=10))
resp.raise_for_status()  # <-- อยู่นอก _retry!
```

ถ้า API ตอบ 429 (rate limit) หรือ 500 (server error) จะ `raise_for_status()` ทันทีโดยไม่ retry
ซึ่ง `_retry()` จะ retry แค่กรณี connection error / timeout เท่านั้น

**วิธีแก้:** ย้าย `raise_for_status()` เข้าไปใน lambda ที่อยู่ใน `_retry`
```python
def _fetch_ls(u):
    resp = requests.get(u, params=params, timeout=10)
    resp.raise_for_status()
    return resp
resp = _retry(_fetch_ls, url)
```

### 4. `taker_volume` -- ปัญหาเดียวกัน raise_for_status() นอก retry

**ไฟล์:** `collectors.py` บรรทัด 523-528
**ปัญหา:** เหมือน `long_short_ratio` ข้างบน -- `resp.raise_for_status()` อยู่นอก `_retry()`

### 5. `deribit_options` -- ไม่มี graceful skip เมื่อไม่มี scipy

**ไฟล์:** `collectors.py` บรรทัด 754-758
**ปัญหา:**

```python
def collect_deribit_options(conn) -> dict:
    t0 = time.time()
    try:
        from math import log, sqrt, exp
        from scipy.stats import norm    # <-- ถ้าไม่มี scipy จะ ImportError
```

ถ้าไม่มี `scipy` จะ `ImportError` → ถูกจับโดย outer `try/except` → return `{"status": "error"}`
ไม่ crash daemon แต่จะ log เป็น ERROR ทุก 15 นาที (สร้าง noise ใน log)

**วิธีแก้:** เพิ่ม explicit ImportError handling เหมือน `collect_macro_indicators()`:
```python
try:
    from scipy.stats import norm
except ImportError:
    return {"status": "skipped", "reason": "scipy not installed"}
```

### 6. `option_greeks` -- ปัญหาเดียวกัน + sequential API call ช้า

**ไฟล์:** `collectors.py` บรรทัด 986-992
**ปัญหา 1:** ไม่มี graceful skip สำหรับ `scipy.optimize.brentq` (เหมือน deribit_options)

**ปัญหา 2:** ทำ API call sequential สำหรับทุก instrument (สูงสุด 200 ตัว)
- แต่ละ instrument ทำ 1-2 HTTP requests (mark price + fallback ticker)
- 200 instruments x 1-2 requests x ~100ms timeout = **20-40 วินาที** ต่อรอบ
- จำกัดไว้ 200 ตัวแล้ว (จาก original ที่ไม่จำกัด → timeout 152 วินาที)

**วิธีแก้:**
- เพิ่ม explicit ImportError handling
- ปรับปรุง: ใช้ batch API call ถ้า Binance EAPI รองรับ (ยังไม่มี batch endpoint)
- สถานะปัจจุบัน: ใช้งานได้แต่ช้า

### 7. `etf_flows` -- time.sleep(5) blocking + weekday check ใช้ local time

**ไฟล์:** `collectors.py` บรรทัด 1159-1235
**ปัญหา 1:** `time.sleep(5)` บรรทัด 1181 -- block thread ของ APScheduler 5 วินาทีเต็ม

**ปัญหา 2:** Weekday check ใช้ `dt.date.today().weekday()` ซึ่งเป็น local system time (Bangkok)
ถ้าเป็นวันจันทร์เช้า (Bangkok) แต่ยังเป็นวันอาทิตย์ (UTC) จะ skip ทั้งที่ไม่ควร skip

**ปัญหา 3:** HTML parsing ผูกกับโครงสร้าง HTML ของ farside.co.uk
- ถ้าเว็บเปลี่ยน layout จะพัง
- ต้อง maintain แบบ manual

**วิธีแก้:**
- ใช้ `datetime.now(timezone.utc).weekday()` แทน `dt.date.today().weekday()`
- ปัญหา sleep + Selenium เป็นข้อจำกัดของ data source (ไม่มี API)

### 8. `option_instruments` -- Binance EAPI 418 error (known issue)

**ไฟล์:** `collectors.py` บรรทัด 933-979
**ปัญหา:** Binance EAPI ตอบ 418 "I'm a Teapot" ซึ่งอาจเป็น geo-restriction หรือ rate limit
- จะ error ทุกวันตอน 00:30 UTC
- ไม่มีทางแก้จากฝั่ง code (ต้องใช้ VPN หรือรอ Binance แก้)

**สถานะ:** Known issue, ไม่กระทบ v3 strategy

### 9. `config.py` -- Telegram credentials เป็น hardcoded default

**ไฟล์:** `config.py` บรรทัด 62-63
**ปัญหา:**
```python
TELEGRAM_API_ID = os.getenv("TELEGRAM_API_ID", "29674353")
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "85f09278e682542fa3c5b210705d2efc")
```

API credentials ไม่ควรมี default value ใน source code (ต่อให้เป็น free API ของ Telegram)
ควรอยู่ใน `.env` เท่านั้น

**วิธีแก้:** ย้ายไป `.env` แล้วเปลี่ยน default เป็น empty string
```python
TELEGRAM_API_ID = os.getenv("TELEGRAM_API_ID", "")
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "")
```

---

## ตัวที่ปกติ (OK)

### 10. `basis`
**ไฟล์:** `collectors.py` บรรทัด 60-137
**สถานะ:** OK -- ใช้ Decimal precision, retry, ON CONFLICT upsert ครบ

### 11. `order_book`
**ไฟล์:** `collectors.py` บรรทัด 144-300
**สถานะ:** OK -- partition management ดี, meta computation (imbalance, wall_ratio) ถูกต้อง
**หมายเหตุ:** ใช้ `binance.client.Client` (python-binance) ในขณะที่ตัวอื่นใช้ `binance.um_futures.UMFutures` (binance-connector) -- ไม่เป็นปัญหา แต่ inconsistent

### 12. `open_interest`
**ไฟล์:** `collectors.py` บรรทัด 307-349
**สถานะ:** OK -- แก้ Bangkok→UTC แล้ว ✓

### 13. `premium_index`
**ไฟล์:** `collectors.py` บรรทัด 352-404
**สถานะ:** OK -- clean, มี ON CONFLICT DO NOTHING

### 14. `funding_rate`
**ไฟล์:** `collectors.py` บรรทัด 411-443
**สถานะ:** OK -- แก้ Bangkok→UTC + .env credentials แล้ว ✓

### 15. `index_price_klines`
**ไฟล์:** `collectors.py` บรรทัด 565-605
**สถานะ:** OK
**หมายเหตุ:** API params มีทั้ง `symbol` และ `pair` (endpoint ใช้แค่ `pair`) -- ไม่เป็นปัญหา, Binance ignore params ที่ไม่รู้จัก

### 16. `mark_price_klines`
**ไฟล์:** `collectors.py` บรรทัด 612-652
**สถานะ:** OK -- เหมือน index_price_klines

### 17. `macro_indicators`
**ไฟล์:** `collectors.py` บรรทัด 659-700
**สถานะ:** OK -- graceful skip ถ้าไม่มี yfinance ✓
**หมายเหตุ:** ข้อมูล daily จาก yfinance ไม่ได้ shift +1d ใน collector (เป็นหน้าที่ของ backtest/strategy ที่ใช้ข้อมูล)

### 18. `btc_dominance`
**ไฟล์:** `collectors.py` บรรทัด 707-747
**สถานะ:** OK -- 15-min bucket dedup ดี

### 19. `tick_liq` (WebSocket)
**ไฟล์:** `ws_liquidation.py`
**สถานะ:** OK -- Producer-Consumer-Flusher pattern ดี
- Queue maxsize=50000 ป้องกัน memory leak
- Buffer flush ทุก 5 วินาที หรือเมื่อ buffer เต็ม 500 rows
- Reconnect อัตโนมัติเมื่อ WS หลุด

### 20. `liq_1h_agg` (Aggregator)
**ไฟล์:** `aggregator.py`
**สถานะ:** OK
**หมายเหตุ:** ใช้ `NOW()` ใน SQL ซึ่งเป็น PostgreSQL server time
- PostgreSQL ทำ comparison TIMESTAMPTZ ใน UTC เสมอ ไม่ว่า server timezone จะเป็นอะไร → ✓ ถูกต้อง
- ทดแทน Coinglass Selenium scraper ได้สำเร็จ (ประหยัดเวลา + ลด dependency)

### 21. `whale_alerts`
**ไฟล์:** `whale.py` บรรทัด 20-135
**สถานะ:** OK -- graceful skip ถ้าไม่มี telethon ✓
- สร้าง Telethon client ใหม่ทุกรอบ (ไม่ efficient แต่ stable สำหรับ hourly)
- Regex parsing สำหรับ whale message ทำงานดี

### 22. `news`
**ไฟล์:** `whale.py` บรรทัด 142-244
**สถานะ:** OK -- pattern เดียวกับ whale_alerts
- Simple keyword sentiment (bullish/bearish/neutral) เพียงพอสำหรับ v3

---

## ปัญหา Cross-cutting

### 1. ข้อมูลเก่า vs ใหม่ Timezone ไม่ตรง

| Collector | ข้อมูลเก่า | ข้อมูลใหม่ |
|-----------|-----------|-----------|
| long_short_ratio | Bangkok (UTC+7) | UTC |
| taker_volume | Bangkok (UTC+7) | UTC |
| open_interest | Bangkok (UTC+7) | UTC |
| funding_rate | Bangkok (UTC+7) | UTC |

ข้อมูลเก่าที่อยู่ใน DB แล้วยังเป็น Bangkok timezone
- **ไม่กระทบ paper trading** (ใช้ข้อมูลล่าสุดเท่านั้น)
- **กระทบ backtest** ที่ดึงข้อมูลย้อนหลัง -- ต้อง handle ใน data_feed.py (ซึ่งมี BKK_UTC_OFFSET fix อยู่แล้ว)
- ข้อมูลจะ "jump" 7 ชั่วโมง ณ จุดที่เปลี่ยน collector

### 2. Binance Library ไม่ consistent

- `order_book` ใช้ `binance.client.Client` (python-binance)
- `open_interest`, `funding_rate` ใช้ `binance.um_futures.UMFutures` (binance-connector)
- `basis`, `premium_index`, `long_short_ratio`, `taker_volume`, `klines` ใช้ raw `requests.get`

ไม่เป็นปัญหาในการทำงาน แต่ maintenance อาจยากขึ้นถ้า library version เปลี่ยน

### 3. BS Greeks มี 2 implementations ที่ใช้ค่า r ต่างกัน

| ตัว | Risk-free rate (r) | อยู่ที่ |
|-----|-------------------|--------|
| `deribit_options` | r = 0 (ไม่ใช้) | collectors.py:770-777 |
| `option_greeks` | R = 0.02 (2%) | collectors.py:993-1029 |

ค่า Greeks ที่ได้จะต่างกันเล็กน้อย (โดยเฉพาะ delta และ rho)
- deribit_options ใช้สำหรับ GEX/skew → ค่า approximate ก็พอ
- option_greeks ใช้สำหรับ per-instrument Greeks → ต้องแม่นกว่า
- **ไม่ใช่ bug** แต่ควรรู้ว่าค่าจะไม่ตรงกัน

---

## สรุปสิ่งที่ต้องทำ (จัดลำดับความสำคัญ)

### ควรทำทันที (Priority 1)

| # | ปัญหา | Collector | ระดับ |
|---|--------|-----------|-------|
| 1 | เพิ่ม dedup สำหรับ fear_greed (ป้องกัน insert ซ้ำ ~24 แถว/วัน) | fear_greed | FIX |
| 2 | ย้าย `raise_for_status()` เข้าไปใน `_retry()` | long_short_ratio, taker_volume | FIX |

### ควรทำเร็วๆ นี้ (Priority 2)

| # | ปัญหา | Collector | ระดับ |
|---|--------|-----------|-------|
| 3 | เพิ่ม graceful skip สำหรับ scipy ImportError | deribit_options, option_greeks | IMPROVE |
| 4 | ปรับ CVD ให้ reuse connection แทนสร้างใหม่ทุก upsert | cvd | IMPROVE |
| 5 | ย้าย Telegram credentials ออกจาก hardcoded default | config.py | SECURITY |
| 6 | ใช้ UTC weekday check แทน local time | etf_flows | FIX |

### ไม่เร่ง (Priority 3)

| # | ปัญหา | Collector | ระดับ |
|---|--------|-----------|-------|
| 7 | option_instruments Binance EAPI 418 error | option_instruments | KNOWN ISSUE |
| 8 | etf_flows ใช้ Selenium + time.sleep(5) blocking | etf_flows | LIMITATION |
| 9 | Timezone discontinuity ข้อมูลเก่า vs ใหม่ | ls_ratio, taker, OI, funding | NOTE |
| 10 | Binance library inconsistency | order_book vs อื่นๆ | NOTE |

---

## สถิติ Collector

| กลุ่ม | จำนวน | รอบ | สถานะ |
|-------|--------|-----|-------|
| 5-min REST | 8 ตัว | ทุก 5 นาที | ทำงานปกติ |
| 15-min REST | 2 ตัว | ทุก 15 นาที | ทำงานปกติ |
| Hourly REST | 6 ตัว | ทุกชั่วโมง | ทำงานปกติ (fear_greed มี dedup issue) |
| Daily REST | 2 ตัว | วันละครั้ง 00:30 UTC | option_instruments 418 error |
| 5-min + Daily | 1 ตัว | ทุก 5 นาที | ทำงานได้แต่ช้า (~20-40s) |
| WebSocket | 2 ตัว | ต่อเนื่อง | tick_liq ดี, cvd มี connection issue |
| Telegram | 2 ตัว | ทุกชั่วโมง | ทำงานปกติ |
| Aggregator | 1 ตัว | ทุกชั่วโมง | ทำงานปกติ |

**รวม: 21/24 ตัวทำงานปกติ** (option_instruments fail เพราะ Binance, fear_greed insert ซ้ำ, cvd ไม่ efficient)
