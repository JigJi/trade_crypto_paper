# Mission 033: News Volume Spike as Volatility / Trade Quality Signal

**วันที่**: 2026-04-11
**ประเภท**: factor_test / sentiment / volatility
**ความยาก**: hard (6 experiments, 7,114 trades, 4,906 news items)
**สถานะ**: COMPLETED -- **HYPOTHESIS MOSTLY FAILED** -- news volume ไม่ใช่ vol signal แต่พบ SHORT edge amplification

---

## แรงบันดาลใจ

instruction.md ระบุว่า news_crypto 4.6K rows ถูกทดสอบ sentiment แล้ว (negative) แต่ **"volume spike ยังไม่ลอง"**

**สมมติฐาน**: ข่าวเยอะ (volume สูง) = ตลาดมีความสนใจ = volatility เพิ่ม = ควรปรับ SL/TP หรือหลีกเลี่ยง entry

ต่อยอดจาก: Factor registry (news_directional, news_contrarian = tested_negative)

---

## EXP1: News Volume Distribution

| Metric | Value |
|--------|-------|
| News in OOS | **4,906** |
| Mean per 15m bar | **1.02** |
| P90 per 15m | 1 |
| Max per 15m | **2** |
| Mean per 1h | ~4 |
| Busiest hour (UTC) | **22:00** |
| Quietest hour (UTC) | **01:00** |

### Key Finding:
- **ข้อมูลข่าว sparse มาก** -- ส่วนใหญ่ 15m bar มีข่าว 0-1 ชิ้น
- Max 2 ต่อ 15m bar = ไม่มี "spike" ที่แท้จริงในระดับ 15m
- ชั่วโมง 22:00 UTC (05:00 BKK) ข่าวเยอะสุด = ช่วง US ปิดตลาด
- ชั่วโมง 01:00 UTC ข่าวน้อยสุด = ช่วง Asia เช้า

---

## EXP2: News Volume -> BTC Volatility Correlation

| Metric | Correlation |
|--------|-------------|
| news_15m vs vol_4bar | **-0.0538** |
| news_1h vs vol_4bar | **-0.0538** |
| news_4h vs vol_4bar | **-0.0692** |
| news_1h vs range_pct | (similar) |
| news_1h -> forward vol | **-0.0552** |
| news_4h -> forward vol | **-0.0667** |

### Vol by news bucket:
| Bucket | Mean Range % | Count |
|--------|-------------|-------|
| 0_none | baseline | majority |
| 1_low | similar | -- |
| 2_mid | similar | -- |
| 3_high | similar | very few |

### CRITICAL FINDING:
- **Correlation ทุกตัวใกล้ศูนย์** (ช่วง -0.05 ถึง -0.07)
- **ข่าว ไม่ ทำนาย volatility** ทั้ง concurrent และ forward-looking
- Negative sign เล็กน้อย = ข่าวเยอะอาจหมายถึง vol ต่ำลงนิดหน่อย (counter-intuitive)
- **สมมติฐาน "ข่าวเยอะ = vol เพิ่ม" ผิด**

---

## EXP3: News Volume -> v3 Trade WR / PnL

| News 1h Quartile | Trades | WR% | Total PnL | Avg PnL |
|-------------------|--------|-----|-----------|---------|
| **Q1_quiet (0 news)** | **6,476** | **70.4%** | **$9,958** | $1.54 |
| Q2 (1-2 news) | 594 | 72.0% | $674 | $1.13 |
| Q3 (3+ news) | 38 | 73.7% | $75 | $1.98 |
| Q4_busy (5+ news) | 6 | 66.7% | $20 | $3.28 |

| Correlation | Value |
|-------------|-------|
| news_1h vs WR | near zero |
| news_1h vs PnL | near zero |

### Key Finding:
- **91% ของ trades เกิดในช่วงไม่มีข่าว (Q1)** -- news volume ไม่มีผลเพราะส่วนใหญ่ไม่มีข่าว
- Q3/Q4 มีตัวอย่างน้อยเกินไป (38/6 trades) ใช้สรุปไม่ได้
- **News volume ไม่สามารถเป็น trade filter ที่ meaningful ได้** เพราะ data ไม่พอ

---

## EXP4: News Volume Spike as Entry Filter

| Filter | Trades | Trades% | PnL | Delta PnL | WR% |
|--------|--------|---------|-----|-----------|-----|
| Baseline (all) | 7,114 | 100% | $10,727 | -- | 70.6% |
| max_news<=2 | 7,070 | 99.4% | $10,632 | -$95 | 70.6% |
| max_news<=3 | 7,108 | 99.9% | $10,707 | -$20 | 70.6% |
| min_news>=3 | 44 | 0.6% | $95 | -$10,632 | 72.7% |

### Key Finding:
- **Filter แทบไม่ตัดอะไรออก** เพราะ 99%+ ของ trades อยู่ในช่วงข่าวน้อย
- ตัด max_news<=2 สูญเสียแค่ 44 trades (-$95) ไม่มีผลกระทบ
- **News volume ไม่สามารถเป็น entry filter ได้** -- ไม่มี signal ที่จะ filter

---

## EXP5: Sentiment Clustering During Volume Spikes

| Metric | Value |
|--------|-------|
| High-vol news hours (P90) | sample |
| Avg bull ratio (high vol) | **0.270** |
| Avg bear ratio (high vol) | **0.045** |
| Avg bull ratio (low vol) | (similar) |
| Corr sentiment -> BTC return (high vol) | **0.0312** |
| Very bullish hours (>50% bull) | counted |
| Very bearish hours (>30% bear) | counted |

### Key Finding:
- **Sentiment ไม่ทำนายทิศทาง BTC** แม้ในช่วงข่าวเยอะ (corr 0.03)
- ข่าวส่วนใหญ่ neutral (69%) -- bullish 25%, bearish 5%
- **ไม่มี contrarian signal จาก sentiment clustering**

---

## EXP6: Adaptive SL/TP by News Volume (INTERESTING FINDING!)

| Period | Trades | WR% | PnL | Avg PnL |
|--------|--------|-----|-----|---------|
| **Quiet** (<=median news) | 6,476 | 70.4% | **$9,958** | $1.54 |
| **Busy** (>median news) | 638 | 72.1% | **$769** | $1.20 |

### Direction Breakdown -- THE DISCOVERY:
| Direction | Period | Trades | WR% | PnL |
|-----------|--------|--------|-----|-----|
| **SHORT** | Quiet | 3,474 | 71.7% | **+$5,666** |
| **SHORT** | **Busy** | **378** | **75.9%** | **+$1,160** |
| LONG | Quiet | 3,002 | 68.9% | +$4,292 |
| LONG | **Busy** | **260** | **66.5%** | **-$391** |

### SHORT > LONG Gap:
| Period | SHORT WR | LONG WR | Gap |
|--------|----------|---------|-----|
| Quiet | 71.7% | 68.9% | **2.8pp** |
| **Busy** | **75.9%** | **66.5%** | **9.4pp** |

### KEY INSIGHT:
- **ช่วงข่าวเยอะ SHORT edge amplified 3.4 เท่า** (2.8pp -> 9.4pp)
- SHORT ระหว่าง busy: WR 75.9% (+$1,160) vs LONG: WR 66.5% (-$391)
- **ข่าวเยอะ = ตลาด attention สูง = LONG อ่อนลง แต่ SHORT แข็งขึ้น**
- สอดคล้องกับ principle "SHORT > LONG" -- effect ขยายในช่วงข่าวเยอะ

---

## สรุปผลทั้งหมด

### Hypothesis Result: MOSTLY FAILED แต่มี 1 finding ที่น่าสนใจ

1. **News volume ไม่ทำนาย volatility** (corr ~-0.05) -- FAILED
2. **News volume ไม่ช่วย filter entry** (99% trades ในช่วง quiet) -- FAILED
3. **Sentiment clustering ไม่มี signal** (corr 0.03) -- FAILED
4. **ข่าวเยอะ amplify SHORT > LONG edge 3.4x** -- DISCOVERED!

### ทำไม news volume ใช้ไม่ได้:
1. **Data resolution ไม่เหมาะกับ 15m** -- ข่าว 1 ชิ้นต่อ 15m bar
2. **ข่าวไม่ใช่ high-frequency data** -- ไม่มี "spike" จริงๆ ในระดับ intraday
3. **ข่าว neutral 69%** -- ส่วนใหญ่ไม่มี directional content
4. **ข่าวตาม event ไม่ใช่นำ event** -- ข่าวมาหลังตลาดขยับแล้ว

### สิ่งที่น่าต่อยอด:
- **SHORT > LONG gap ขยายช่วงข่าวเยอะ** -- ถ้ามี news volume ที่แม่นกว่า (เช่น Twitter/X volume) อาจใช้เป็น LONG filter ได้
- **ข่าว hourly pattern**: busiest 22:00 UTC = ตรงกับช่วงที่เคยพบว่า vol สูง (Mission 001)

---

## Tags
`factor_test`, `news_volume`, `sentiment`, `volatility`, `negative_result`, `short_edge_amplification`
