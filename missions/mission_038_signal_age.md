# Mission 038: Signal Age at Entry -- สัญญาณสดแพ้สัญญาณเก่า!

**วันที่**: 2026-04-16
**ประเภท**: signal_quality / entry_timing / regime_stability
**ความยาก**: hard (7 experiments, 7,409 trades, 6 coins, signal age tracking)
**สถานะ**: COMPLETED -- **HYPOTHESIS FAILED** -- สัญญาณเก่ากลับดีกว่าสัญญาณสด!

---

## แรงบันดาลใจ

ต่อยอดจาก Mission 011 (alpha decay), Mission 031 (score velocity), Mission 005 (exit mechanism)

**สมมติฐาน**: สัญญาณที่เพิ่ง fire (signal age = 1) ควรดีกว่าสัญญาณที่ active มานานหลาย bars เพราะ:
1. สัญญาณสดยังไม่ถูก "price in"
2. สัญญาณเก่าอาจใกล้ reversal
3. Alpha ควรเสื่อมตามเวลา (decay)

**ผลจริง**: ตรงกันข้าม 100%!

---

## EXP1: Signal Regime Duration Distribution (OOS)

| Metric | Value |
|--------|-------|
| OOS bars | 42,649 |
| Active signal bars | 10,773 (25.3%) |
| Signal regimes | 1,897 |
| Mean duration | **5.7 bars** (~1.4 ชั่วโมง) |
| Median duration | **2 bars** (30 นาที) |
| P75 | 6 bars |
| P90 | 14 bars |
| Max | 71 bars (~18 ชั่วโมง) |

**ข้อสังเกต**: สัญญาณส่วนใหญ่สั้นมาก (median 2 bars) — กว่าครึ่งของ regime จบภายใน 30 นาที

---

## EXP2: Trade Quality ตาม Signal Age Bucket ⭐

| Age Bucket | Trades | WR% | Avg PnL | Total PnL | หมายเหตุ |
|---|---|---|---|---|---|
| **1 (สด)** | **217** | **47.9%** | **-$1.28** | **-$279** | **ขาดทุน!** |
| 2-4 | 2,733 | 67.9% | +$0.65 | +$1,771 | เริ่มกำไร |
| 5-8 | 984 | 72.3% | +$1.73 | +$1,707 | ดีขึ้นชัด |
| 9-16 | 1,089 | 71.0% | +$1.48 | +$1,613 | คงที่ |
| 17-32 | 558 | **76.0%** | **+$2.70** | +$1,509 | ดีมาก |
| 33+ | 184 | **77.2%** | +$2.10 | +$386 | ดีที่สุดแต่ n น้อย |

**Pattern ชัดเจน**: WR เพิ่มขึ้นตาม signal age! จาก 47.9% → 77.2% (+29.3pp)

Baseline ทั้งหมด: 7,409 trades, WR 68.7%, PnL $7,666

---

## EXP3: ทำไม Age=1 แย่? → SIGNAL_FLIP!

| Age Bucket | TRAIL% | SIGNAL_FLIP% | SL% |
|---|---|---|---|
| **1 (สด)** | 50.7% | **47.5%** | 1.8% |
| 2-4 | 82.9% | 6.0% | 11.1% |
| 5-8 | 85.5% | 5.3% | 9.2% |
| 9-16 | 84.9% | 5.9% | 9.1% |
| 17-32 | 90.7% | 4.1% | 5.0% |
| 33+ | 91.8% | 3.8% | 4.3% |

**คำตอบ**: เกือบครึ่งของ age=1 trades ออกด้วย SIGNAL_FLIP (47.5%) — สัญญาณเพิ่งปรากฏแล้วก็หายไปทันที
สัญญาณเก่า (33+) มี SIGNAL_FLIP แค่ 3.8% — regime ที่อยู่นานมีความมั่นคงสูง

---

## EXP4: Signal Age x Direction

### SHORT
| Age | n | WR% | Avg PnL |
|---|---|---|---|
| 1 | 109 | 57.8% | +$0.07 |
| 2-4 | 1,427 | 70.1% | +$1.25 |
| 5-8 | 541 | 73.8% | +$1.74 |
| 9-16 | 624 | 67.5% | +$0.50 |
| 17-32 | 292 | **76.7%** | **+$4.10** |
| 33+ | 116 | **81.0%** | **+$3.76** |

### LONG
| Age | n | WR% | Avg PnL |
|---|---|---|---|
| 1 | 108 | **38.0%** | **-$2.65** |
| 2-4 | 1,306 | 65.6% | -$0.01 |
| 5-8 | 443 | 70.4% | +$1.73 |
| 9-16 | 465 | **75.7%** | **+$2.80** |
| 17-32 | 266 | 75.2% | +$1.17 |
| 33+ | 68 | 70.6% | -$0.74 |

**ค้นพบสำคัญ**:
- LONG age=1 เป็นเทรดที่แย่ที่สุดในระบบ: WR 38%, avg -$2.65
- SHORT age=33+ ดีที่สุด: WR 81%, avg +$3.76
- LONG มี "sweet spot" ที่ age 9-16 (WR 75.7%)
- SHORT ดีขึ้นเรื่อยๆ ตาม age (monotonic)

---

## EXP5: Max Signal Age Filter → ไม่ควรตัด!

การจำกัด max_age ทำให้ PnL **ลดลงทุกค่า**:

| Max Age | Trades Kept | WR% | PnL | ΔPnL |
|---|---|---|---|---|
| 1 | 25.1% | 63.5% | +$680 | -$6,986 |
| 4 | 62.0% | 66.2% | +$2,450 | -$5,215 |
| 8 | 75.3% | 67.2% | +$4,158 | -$3,508 |
| 16 | 90.0% | 67.8% | +$5,770 | -$1,895 |
| 24 | 94.6% | 68.4% | +$7,378 | -$287 |
| All | 100% | 68.7% | +$7,666 | 0 |

**ข้อสรุป**: ห้ามตัด max signal age — เทรดที่ age สูงเป็น "ของดี" ไม่ใช่ "ของเก่า"

---

## EXP6: Correlation Analysis

| Pair | Correlation |
|---|---|
| signal_age vs holding_bars | 0.013 (แทบไม่มี) |
| signal_age vs pnl_net | 0.034 (บวกเล็กน้อย) |

Signal age ไม่สัมพันธ์กับระยะเวลาถือ — มันเป็น factor อิสระ

---

## EXP7: Fresh vs Stale Summary

| กลุ่ม | n | WR% | Total PnL | Avg PnL |
|---|---|---|---|---|
| **Fresh (age=1)** | **217** | **47.9%** | **-$279** | **-$1.28** |
| **Stale (age>8)** | **1,831** | **73.1%** | **+$3,508** | **+$1.92** |
| All | 7,409 | 68.7% | +$7,666 | +$1.03 |

---

## ทำไมถึงเป็นแบบนี้? (สมมติฐานเชิงโครงสร้าง)

1. **Survival Bias ของ Regime**: สัญญาณที่อยู่นาน = BTC composite score มั่นคงจริงๆ (factors หลายตัว agree อย่างต่อเนื่อง) ในขณะที่ age=1 อาจเป็นแค่ noise ชั่วคราว

2. **Signal Confirmation**: เมื่อ score ข้าม threshold แล้วอยู่นาน = ตลาดยืนยันทิศทาง เทียบกับ age=1 ที่ยังไม่มีการยืนยัน

3. **SIGNAL_FLIP = Noise Detection**: age=1 ที่จบด้วย FLIP (47.5%) คือสัญญาณที่ "ลองข้าม threshold แล้วถอย" — เป็น noise ไม่ใช่ signal

4. **Momentum > Mean Reversion ในบริบทนี้**: ตรงข้ามกับ factor signals ที่เป็น contrarian, ตัว regime เองเป็น momentum — ยิ่งอยู่นานยิ่งน่าจะต่อ

---

## ข้อเสนอ Actionable

### 1. Minimum Signal Age Filter (แนะนำ)
**Skip entry ถ้า signal_age < 2** (ไม่เข้าเทรดตอน signal เพิ่ง fire bar แรก)
- ตัดออก: 217 trades, PnL -$279
- **PnL ปรับปรุง: +$279** (ตัดขาดทุนออก)
- เทียบเท่า cooldown_bars แต่สำหรับ signal onset

### 2. Confidence Boost สำหรับ Stale Signals
ใช้ signal_age เป็น confidence multiplier สำหรับ position sizing:
- age 1: skip หรือ 0.5x size
- age 2-8: 1.0x size (ปกติ)
- age 9+: 1.2-1.5x size (มั่นใจสูง)

### 3. อย่าลด max signal age
เทรดที่ signal age สูงเป็นเทรดที่ดีที่สุด — ห้ามตัดออก

---

## สิ่งที่เรียนรู้

1. **สมมติฐาน "สดกว่าดีกว่า" ผิด** — ในระบบ BTC-led, regime stability > freshness
2. **SIGNAL_FLIP ที่ age=1 = noise filter ธรรมชาติ** — ระบบกรอง noise ออกเองถ้ารอ 1 bar
3. **SHORT ได้ประโยชน์จาก age มากกว่า LONG** — WR เพิ่มจาก 57.8% → 81.0% (+23.2pp)
4. **LONG มี sweet spot ที่ age 9-16** แต่ age สูงมาก (33+) กลับแย่ลง
5. **signal_age เป็น factor อิสระ** — ไม่ซ้ำกับ holding_bars หรือ score magnitude
