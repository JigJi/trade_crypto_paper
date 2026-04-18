# Mission 023: Factor Conflict Analysis — Factor ขัดแย้งกันทำนาย SIGNAL_FLIP ได้ไหม?

**วันที่**: 2026-04-01
**ประเภท**: factor_analysis / signal_flip
**ความยาก**: hard (7 experiments, 3,547 trades, factor decomposition + trajectory)
**สถานะ**: COMPLETED — **CRITICAL DISCOVERY** — ob_combined เป็นต้นเหตุหลักของ SIGNAL_FLIP (flip ก่อนใน 49% ของเคส)

---

## แรงบันดาลใจ

ต่อยอดจาก:
- **Mission 021**: entry features ไม่ทำนาย FLIP → ปัญหาอยู่ระหว่างเทรด
- **Mission 022**: FLIP เป็น sudden event ไม่ใช่ gradual decay

**คำถามใหม่**: ถ้าปัญหาอยู่ระหว่างเทรด ใช่ factor ตัวไหนที่ "กลับทิศก่อน" จนทำให้ composite score FLIP?

**สมมติฐาน**: เมื่อ factor 8 ตัวขัดแย้งกัน (บางตัวบอก LONG บางตัวบอก SHORT) composite score จะไม่เสถียร → FLIP

---

## EXP 1: Factor Activity Baseline (OOS 42,649 bars)

| Factor | Active% | Bull% | Bear% | Mean Score |
|--------|---------|-------|-------|------------|
| ob_combined | **21.3%** | 9.6% | 11.7% | -0.049 |
| etf_flows | **22.1%** | 9.5% | 12.6% | -0.032 |
| oi_divergence | 8.3% | 4.1% | 4.2% | -0.000 |
| tick_liq | 7.9% | 3.2% | 4.7% | -0.022 |
| liq_cascade | 5.9% | 2.5% | 3.4% | -0.019 |
| basis_contrarian | 4.6% | 2.3% | 2.3% | -0.000 |
| whale_alerts | 1.0% | 0.8% | 0.2% | +0.008 |
| funding_rate | **0.4%** | 0.4% | 0.0% | +0.008 |

**Key**: ob_combined และ etf_flows เป็น factor ที่ active ที่สุด (>20% ของเวลา). funding_rate แทบไม่เคย active (0.4%)!

---

## EXP 2: Factor Agreement — FLIP vs Non-FLIP at Entry

| Metric | FLIP (163) | Non-FLIP (3,384) | Diff |
|--------|-----------|-------------------|------|
| Agreement Ratio | 0.908 | 0.913 | **-0.005** |
| Active Factors | 1.68 | 2.02 | -0.34 |

**ผล**: Agreement ตอน entry แทบไม่ต่างกัน! ยืนยัน M021 ว่า entry features ไม่ทำนาย FLIP

FLIP trades เข้าตอนมี factor active น้อยกว่าเล็กน้อย (1.68 vs 2.02) — signal อ่อนกว่านิดหน่อย

---

## EXP 3: Factor Dominance at Entry

| Factor | FLIP abs mean | Non-FLIP abs mean | Ratio |
|--------|---------------|-------------------|-------|
| **ob_combined** | **1.767** | **1.709** | 1.03x |
| tick_liq | 0.405 | 0.567 | 0.71x |
| liq_cascade | 0.196 | 0.536 | **0.37x** |
| etf_flows | 0.258 | 0.355 | 0.73x |
| basis_contrarian | 0.226 | 0.261 | 0.86x |
| oi_divergence | 0.054 | 0.050 | 1.07x |
| funding_rate | 0.049 | 0.028 | 1.73x |
| whale_alerts | 0.009 | 0.043 | 0.22x |

**Key Finding**: 
- FLIP trades มี **liq_cascade contribution ต่ำกว่า 2.7x** → เข้าตอนไม่มี cascade!
- FLIP trades มี **ob_combined สูงสุด** → พึ่ง order book เป็นหลัก
- Non-FLIP trades มี factor หลายตัวช่วย (liq, tick, etf, whale)

---

## EXP 4: Agreement Quartile Performance — ผลกลับด้าน!

| Quartile | Trades | WR% | PnL | Avg PnL | FLIP% |
|----------|--------|-----|-----|---------|-------|
| **Low (0-50%)** | 260 | 76.2% | $26 | **$0.10** | **7.3%** |
| Med (50-67%) | 432 | **81.0%** | $3,135 | $7.26 | **2.8%** |
| High (67-80%) | 140 | **82.9%** | $822 | $5.87 | 4.3% |
| Very High (80-100%) | 2,715 | 76.6% | $7,771 | $2.86 | 4.6% |

**ผลที่น่าสนใจ**:
- Low agreement (conflict สูงสุด) = FLIP สูงสุด (7.3%) + PnL แทบเป็นศูนย์ ✓
- **Medium agreement ดีที่สุด!** WR 81%, FLIP ต่ำสุด 2.8%
- Very High agreement (factor เห็นตรงกัน) กลับไม่ใช่ดีสุด เพราะส่วนใหญ่มี factor active แค่ 1-2 ตัว

---

## EXP 5: Factor Conflict During Trade — **การค้นพบสำคัญที่สุด!**

| Metric | FLIP Trades | Non-FLIP Trades | Ratio |
|--------|-------------|-----------------|-------|
| Agreement Start | 0.908 | 0.893 | ~เท่ากัน |
| Agreement End | **0.869** | **0.922** | ลดลง vs เพิ่มขึ้น |
| Agreement Min | ต่ำกว่า | สูงกว่า | - |
| Agreement Std | สูงกว่า | ต่ำกว่า | - |
| **Factor Flips/Trade** | **1.66** | **0.34** | **4.9x!** |
| Bars Held | ~16 bars | ~varied | - |

### สรุป Trajectory:
- **FLIP trades**: เริ่มเหมือนกัน แต่ agreement **ลดลง** ระหว่างเทรด + factor เปลี่ยนทิศ 1.66 ครั้ง
- **Non-FLIP trades**: agreement **เพิ่มขึ้น** ระหว่างเทรด + factor เปลี่ยนทิศแค่ 0.34 ครั้ง
- Factor instability ระหว่างเทรดสูง **4.9 เท่า** ของเทรดปกติ — ยืนยัน M021!

---

## EXP 6: Consensus Filter Test — **FAIL!**

| Min Agreement | Trades | WR% | PnL | Delta |
|---------------|--------|-----|-----|-------|
| Baseline (all) | 3,547 | 77.4% | $11,753 | - |
| >= 0.5 | 3,547 | 77.4% | $11,753 | $0 |
| >= 0.6 | 3,287 | 77.5% | $11,728 | **-$26** |
| >= 0.7 | 2,855 | 77.0% | $8,593 | **-$3,160** |
| >= 0.8 | 2,733 | 76.8% | $7,879 | **-$3,875** |
| = 1.0 | 2,715 | 76.6% | $7,771 | **-$3,982** |

**Consensus filter ที่ entry ไม่ช่วย** — ตัดเทรดดีออกมากกว่า FLIP

---

## EXP 7: Factor ตัวไหน FLIP ก่อน? — **ORDER BOOK!**

| Factor | Times Flipped First | % of FLIP Trades |
|--------|--------------------:|----------------:|
| **ob_combined** | **80** | **49.1%** |
| tick_liq | 35 | 21.5% |
| oi_divergence | 25 | 15.3% |
| liq_cascade | 9 | 5.5% |
| basis_contrarian | 3 | 1.8% |

### **ob_combined = Root Cause ของ SIGNAL_FLIP!**

Order book เปลี่ยนทิศก่อน factor อื่นใน **เกือบครึ่ง** ของ FLIP trades ทั้งหมด! ตามด้วย tick_liq (21.5%)

**เหตุผล**: Order book เป็น factor ที่:
1. Active มากที่สุด (21.3% ของเวลา)
2. Weight สูง (2.0)
3. ใช้ MA 12 bars (3 ชม.) ซึ่งสั้นมาก — สั่นไปมาง่าย
4. Order book data มี noise สูง — imbalance เปลี่ยนทิศเร็ว

---

## สรุป & Action Items

### การค้นพบหลัก:
1. **Entry agreement ไม่ทำนาย FLIP** (diff แค่ 0.005) — ยืนยัน M021
2. **In-trade factor flips = 4.9x สำหรับ FLIP trades** — root cause = factor instability
3. **ob_combined flip ก่อน 49%** → order book = ตัวการหลัก
4. **FLIP trades พึ่ง ob_combined เป็นหลัก** แต่ non-FLIP trades มี liq_cascade ช่วย
5. **Consensus filter ที่ entry ไม่ช่วย** — ปัญหาอยู่ระหว่างเทรด ไม่ใช่ตอนเข้า

### ข้อเสนอสำหรับ Mission ถัดไป:
1. **ลด ob_combined weight** จาก 2.0 → 1.0 แล้วทดสอบ — ลด FLIP จาก OB
2. **เพิ่ม OB MA window** จาก 12 → 24 bars (6 ชม.) — ทำให้ OB เสถียรขึ้น
3. **Freeze OB direction ระหว่างเทรด** — ไม่ให้ OB flip ระหว่างเทรด
4. **v6 (liq-only) อาจดีกว่า** เพราะไม่มี OB เลย → ไม่มีปัญหานี้!
5. ทดสอบ **"minimum factor support"** — ต้องมี factor active >= 2 ตัวถึงเข้าเทรด

### ทำไม v6 (liq-only) อาจเป็นคำตอบ?
v6 ใช้แค่ liquidation + tick_liq ซึ่งเป็น factor ที่เสถียรกว่า OB มาก. FLIP ที่เกิดจาก cascade flip จะเกิดเฉพาะตอนมี cascade event จริงๆ ไม่ใช่ noise จาก order book

---

**XP**: +100 | **Streak**: 21 | **Level**: 20
