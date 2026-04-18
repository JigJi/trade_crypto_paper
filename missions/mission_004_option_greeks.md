# Mission #004: Option Greeks Deep Dive -- BTC IV/Skew/GEX Signal Analysis

**วันที่**: 2026-03-15
**ระดับ**: Hard (100 XP)
**สถานะ**: COMPLETED (ผลเป็นลบ แต่มีข้อมูลสำคัญ)

---

## สมมติฐาน

ข้อมูล BTC option Greeks 26,066 rows (6 เดือน, Binance EAPI) สามารถสร้าง signal ที่ทำนายทิศทาง BTC ได้
โดยรวม option data เป็น 4 สัญญาณ:
1. **ATM IV** (z-score) -- ระดับความกลัว/ความผันผวนของตลาด
2. **25-delta Skew** (IV put - IV call) -- fear premium ของ put options
3. **Put/Call OI Ratio** -- สัดส่วนการเปิดสถานะ put vs call
4. **Net GEX** (Gamma Exposure) -- ทิศทางการ hedge ของ market maker

---

## ข้อมูลที่สำรวจ

### market_data.option_greeks (Binance EAPI)
| รายการ | ค่า |
|--------|-----|
| จำนวน rows | **26,066** |
| ช่วงวันที่ | 2025-09-14 ถึง 2026-03-14 (177 วัน) |
| Resolution | **~627 นาที (~10.5 ชม.)** -- ไม่ใช่ 5 นาทีตามที่คาด! |
| Unique timestamps | 412 (เฉลี่ย ~2.3 ครั้ง/วัน) |
| Columns | symbol, ts, iv_solved, delta, gamma, theta, vega, rho |
| OI data | **ไม่มี!** (0 non-null) -- Binance EAPI ไม่ส่ง OI กลับมา |
| Side split | CALL 13,093 / PUT 12,973 (สมดุลดี) |

### market_data.options_data (Deribit, pre-computed)
| รายการ | ค่า |
|--------|-----|
| จำนวน rows | 516 |
| ช่วงวันที่ | 2026-03-09 ถึง 2026-03-14 (**5 วันเท่านั้น**) |
| มี skew, P/C ratio, GEX, max_pain | ครบ แต่สั้นเกินไปสำหรับ backtest |

---

## สิ่งที่ค้นพบ

### 1. Data Quality Issues (สำคัญมาก!)

**ปัญหาหลัก**: Binance EAPI ไม่ส่ง Open Interest (OI) กลับมา
- `volume` = None, `oi` = None ทุก row
- ทำให้ **คำนวณ Put/Call Ratio ไม่ได้**
- ทำให้ **คำนวณ GEX ไม่ได้** (ต้องใช้ OI ถ่วงน้ำหนัก gamma)
- จาก 4 สัญญาณที่วางแผนไว้ ใช้ได้จริงแค่ 2 (ATM IV + Skew)

**ปัญหารอง**: Data ห่างมาก
- ~10 ชม./data point (ไม่ใช่ 5 นาทีตามที่ daemon ตั้งเวลาไว้)
- สาเหตุ: Binance EAPI มี geo-blocking (HTTP 418 error) ทำให้เก็บข้อมูลได้ไม่สม่ำเสมอ
- เมื่อ resample เป็น 15 นาที ต้อง forward-fill ข้ามช่วง 10 ชม. → signal ค้างนาน

### 2. Aggregate Metrics

| Signal | Valid Count | Mean | Std | Min | Max |
|--------|------------|------|-----|-----|-----|
| ATM IV | 405 | 0.416 | 0.087 | 0.228 | 0.770 |
| 25d Skew | 405 | 0.030 | 0.058 | -0.135 | 0.317 |
| P/C Ratio | 0 | -- | -- | -- | -- |
| GEX Net | 0 | -- | -- | -- | -- |

### 3. Signal Density (z-score thresholds)
- ATM IV: 145 signals จาก 405 timestamps (35.8%)
- Skew: 112 signals จาก 405 timestamps (27.7%)
- P/C Ratio: 0 (ไม่มีข้อมูล)
- GEX: 0 (ไม่มีข้อมูล)

---

## ผลการ Backtest

**Baseline (v3 ไม่มี option factor)**: $1,180 (OOS Jan-Mar 2026, 6 coins)

### ATM IV (contrarian: high IV → bullish)
| Weight | PnL | Delta vs Baseline | Trades | WR% |
|--------|-----|-------------------|--------|-----|
| 0.5 | $631 | **-$549** | 1,118 | 72.8% |
| 1.0 | $107 | **-$1,073** | 1,182 | 72.1% |
| 1.5 | -$454 | **-$1,634** | 1,219 | 71.0% |
| 2.0 | -$466 | **-$1,646** | 1,346 | 70.4% |

**ผลตัดสิน**: NEGATIVE -- ยิ่ง weight สูง ยิ่งเสียมาก. เพิ่มจำนวน trade (noise) ลด WR

### 25-delta Skew (contrarian: high put premium → bullish)
| Weight | PnL | Delta vs Baseline | Trades | WR% |
|--------|-----|-------------------|--------|-----|
| 0.5 | $807 | **-$373** | 1,131 | 73.0% |
| 1.0 | $658 | **-$522** | 1,184 | 73.1% |
| 1.5 | $157 | **-$1,023** | 1,182 | 72.4% |
| 2.0 | -$189 | **-$1,369** | 1,216 | 71.0% |

**ผลตัดสิน**: NEGATIVE -- ดีกว่า ATM IV เล็กน้อย แต่ก็ยังเสียทุก weight

---

## บทวิเคราะห์

### ทำไม Option Greeks ไม่ทำงาน?

1. **Resolution ต่ำเกินไป**: Data ~10 ชม./จุด vs trading 15 นาที = signal ค้างนานเกินไป
   - เหมือน Fear & Greed (daily) ที่ fail บน 15m -- **ปัญหาเดิม!**
   - Lesson #4 จาก lessons_learned.md: "Daily data + 15m trading = noise"

2. **Contrarian IV ≠ Contrarian ที่ดี**:
   - Contrarian liquidation/funding ทำงานเพราะเป็น event-based (เกิดแล้วตอบสนอง)
   - Contrarian IV เป็น state-based (IV สูงอยู่นาน ไม่รู้ว่าจะกลับเมื่อไร)
   - State-based signal ต้องการ timing ที่ดี ซึ่ง 10 ชม. resolution ให้ไม่ได้

3. **ไม่มี OI → ขาด signal ที่สำคัญที่สุด**:
   - Put/Call Ratio (ต้อง OI) คือ signal ที่ research ชี้ว่าแข็งแกร่งที่สุด
   - GEX (ต้อง OI) คือ signal ที่บอก gamma hedging flow
   - ทั้งสองตัวไม่สามารถทดสอบได้เลย

4. **Forward-fill ข้ามช่วงยาว**:
   - Resample 15 นาทีจากข้อมูล 10 ชม. = forward-fill ~40 bars
   - Signal เดิมซ้ำ 40 bars ติด → ไม่มี granularity

### Deribit Data มีแววมากกว่า

ข้อมูล Deribit (options_data) มี:
- Resolution 15 นาที (ตรงกับ trading timeframe!)
- Pre-computed skew_25d, put_call_ratio, gex_net, max_pain
- **แต่มีแค่ 516 rows (5 วัน)** -- ยังทดสอบไม่ได้

---

## ข้อเสนอ

1. **ปิด Binance EAPI collection** -- data ห่างเกินไป (10h resolution) และไม่มี OI
2. **เก็บ Deribit data ต่อ** -- resolution 15m + มี P/C ratio + GEX + skew ครบ
3. **รอ Deribit data ครบ 3 เดือน** (ประมาณ Jun 2026) แล้วทดสอบใหม่
4. **อัปเดต factor registry**:
   - skew_25d → tested_negative (Binance) / untested (Deribit, pending data)
   - put_call_ratio → untestable (no OI from Binance) / untested (Deribit)
   - gamma_exposure → untestable (no OI from Binance) / untested (Deribit)
   - max_pain → untestable / untested (Deribit)

---

## Key Insight

**Option Greeks จาก Binance EAPI ไม่มีประโยชน์สำหรับ 15m trading**:
1. Resolution ต่ำ (~10h) = daily-equivalent noise
2. ไม่มี Open Interest = ขาด signal ที่สำคัญที่สุด (P/C ratio, GEX)
3. ตรงกับ lesson เดิม: "daily data + 15m trading = noise"
4. **Deribit data มีแวว** (15m resolution, มี OI) แต่ต้องรอสะสมข้อมูลเพิ่ม

**ค่าของ mission นี้**: ป้องกัน effort ในอนาคตจากการพยายามใช้ Binance EAPI option data
