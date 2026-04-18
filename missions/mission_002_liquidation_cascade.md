# Mission #002: Liquidation Cascade Analysis

**วันที่**: 2026-03-14  
**ช่วง OOS**: 2025-01-01 ถึง 2026-03-31  
**ผลลัพธ์**: PASS -- continuation_pattern  

## สมมติฐาน
เมื่อเกิด Liquidation Cascade (การถูก liquidate จำนวนมากในทิศทางเดียวกันภายใน 15 นาที)
ราคา BTC จะกลับตัว (reversal) ภายใน 1-4 ชั่วโมง เนื่องจากแรงขาย/ซื้อเทียมหมดลง

## ข้อมูลที่ใช้
- Liquidation events: 61,354 รายการ (BTCUSDT)
- ช่วงข้อมูล: 2025-09-14 to 2026-03-14
- 15-min bars ที่มี liquidation (OOS): 4,317 bars

## นิยาม Cascade
| ระดับ | ขั้นต่ำ USD | ขั้นต่ำ Events | ขั้นต่ำ Side Ratio |
|-------|-----------|---------------|-------------------|
| moderate | $64,508 | 5 | 0.5 |
| strong | $153,980 | 10 | 0.6 |
| extreme | $242,586 | 15 | 0.7 |

Side Ratio > 0 = Short ถูก liq มากกว่า (ราคากำลังขึ้น)
Side Ratio < 0 = Long ถูก liq มากกว่า (ราคากำลังลง)

## ผลวิเคราะห์ Cascade แต่ละระดับ

### MODERATE
- Long Liq Cascades (ราคาลง): 391 ครั้ง
- Short Liq Cascades (ราคาขึ้น): 497 ครั้ง

**หลัง Long ถูก Liq (ราคาลง):**
- Return 15m ถัดไป: -0.0148%
- Return 1h ถัดไป: -0.0545% (median: -0.0283%)
- Return 4h ถัดไป: -0.0860%
- % ที่ bounce กลับ (1h): 34.8%
- สรุป: **continuation_down**

**หลัง Short ถูก Liq (ราคาขึ้น):**
- Return 15m ถัดไป: -0.0225%
- Return 1h ถัดไป: -0.0508% (median: -0.0136%)
- Return 4h ถัดไป: -0.1970%
- % ที่ร่วงลง (1h): 46.7%
- สรุป: **reversal_down**

### STRONG
- Long Liq Cascades (ราคาลง): 134 ครั้ง
- Short Liq Cascades (ราคาขึ้น): 196 ครั้ง

**หลัง Long ถูก Liq (ราคาลง):**
- Return 15m ถัดไป: -0.0162%
- Return 1h ถัดไป: -0.0108% (median: -0.0116%)
- Return 4h ถัดไป: +0.1181%
- % ที่ bounce กลับ (1h): 32.1%
- สรุป: **continuation_down**

**หลัง Short ถูก Liq (ราคาขึ้น):**
- Return 15m ถัดไป: -0.0169%
- Return 1h ถัดไป: -0.0228% (median: -0.0113%)
- Return 4h ถัดไป: -0.1693%
- % ที่ร่วงลง (1h): 45.4%
- สรุป: **reversal_down**

### EXTREME
- Long Liq Cascades (ราคาลง): 50 ครั้ง
- Short Liq Cascades (ราคาขึ้น): 104 ครั้ง

**หลัง Long ถูก Liq (ราคาลง):**
- Return 15m ถัดไป: -0.0232%
- Return 1h ถัดไป: -0.0488% (median: -0.1468%)
- Return 4h ถัดไป: +0.0677%
- % ที่ bounce กลับ (1h): 22.0%
- สรุป: **continuation_down**

**หลัง Short ถูก Liq (ราคาขึ้น):**
- Return 15m ถัดไป: -0.0083%
- Return 1h ถัดไป: -0.0255% (median: +0.0062%)
- Return 4h ถัดไป: -0.1135%
- % ที่ร่วงลง (1h): 41.3%
- สรุป: **reversal_down**

## ขนาด Cascade vs ผลลัพธ์

| ขนาด | จำนวน | Avg Ret 1h | Avg Ret 4h | Avg Side Ratio |
|------|-------|-----------|-----------|----------------|
| <$5K | 1207 | +0.0059% | -0.0264% | +0.092 |
| $5-20K | 974 | +0.0023% | -0.0120% | -0.006 |
| $20-50K | 812 | -0.0138% | -0.0548% | -0.007 |
| $50-100K | 588 | -0.0634% | -0.1719% | +0.023 |
| >$100K | 736 | -0.0382% | -0.1677% | +0.111 |

## Cascade ซ้อน (Consecutive)
- Cascade ครั้งแรก: 14 ครั้ง, avg ret 1h: +0.1748%
- Cascade ตาม (ภายใน 1h): 4303 ครั้ง, avg ret 1h: -0.0154%

## V3 Performance: Cascade vs Normal

เทรด BTC ทั้งหมด (OOS): 682

| ประเภท | จำนวนเทรด | Win Rate | Avg PnL | Total PnL |
|--------|----------|---------|---------|----------|
| ช่วง Cascade | 71 | 64.8% | $1.44 | $102.28 |
| ช่วงปกติ | 611 | 60.4% | $-0.48 | $-294.24 |

ส่วนต่าง WR: +4.4pp
-> ไม่พบความแตกต่างที่มีนัยสำคัญ

## สรุปและข้อเสนอแนะ

**พบ Continuation Pattern**: หลังเกิด liquidation cascade ราคามักไปต่อในทิศทางเดิม
- ข้อเสนอ: cascade ใช้เป็น momentum confirmation ไม่ใช่ contrarian signal

## อ้างอิง
- Amberdata: Forced liquidation dynamics in crypto derivatives
- BitMEX Research: Liquidation cascades and market microstructure
- Mission #001: Hour-of-Day Session Analysis (baseline context)
