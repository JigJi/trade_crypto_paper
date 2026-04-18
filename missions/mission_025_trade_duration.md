# Mission 025: Trade Duration & Time Stop — เทรดนานแค่ไหนถึงควรยอมแพ้?

**วันที่**: 2026-04-03
**ประเภท**: exit_mechanism / duration_analysis
**ความยาก**: hard (6 experiments, 6,776 trades, 8 time stop tests, bar-by-bar PnL tracking)
**สถานะ**: COMPLETED — **IMPORTANT FAIL** — Time stop ทุกรูปแบบทำลาย PnL แต่ค้นพบ duration fingerprint ที่มีค่า

---

## แรงบันดาลใจ

ต่อยอดจาก M021-024 ที่พบว่า SIGNAL_FLIP เป็น money destroyer แต่ entry filter / score-based exit / factor surgery ไม่ช่วย

**คำถามใหม่**: ถ้าดูจากมุม "เวลา" — เทรดที่ถืออยู่นานเกินไปมีพฤติกรรมยังไง? ถ้า trade underwater หลัง N bars ควรตัดขาดทุนไหม?

**สมมติฐาน**: เทรดที่ไม่ถึง TP ภายใน N bars มีแนวโน้มจะจบด้วย SIGNAL_FLIP → time stop หรือ conditional exit จะช่วย

**ผล: สมมติฐานผิดทั้งหมด — strategy ต้อง "ให้เวลา" กับเทรด ตัดเร็วทำให้เสีย winner มากกว่า**

---

## EXP1: Duration Distribution by Exit Reason

| Exit Reason | Count | % | Bars Mean | Bars Median | WR | PnL |
|-------------|-------|---|-----------|-------------|-----|-----|
| **TRAIL** | **5,854** | **86.4%** | **4.3** | **1** | **82.6%** | **+$38,775** |
| SIGNAL_FLIP | 874 | 12.9% | 15.8 | 9 | 4.8% | -$20,793 |
| SL | 41 | 0.6% | 18.1 | 9 | 0.0% | -$4,524 |
| TIMEOUT | 7 | 0.1% | 96.0 | 96 | 0.0% | -$317 |

### Key Insight:
- **TRAIL ชนะเร็ว**: median 1 bar (15 นาที!) — 80%+ ของเทรดที่ชนะจบเร็วมาก
- **SIGNAL_FLIP ตายช้า**: median 9 bars (2.25 ชม.) — ถือนานแล้วตายทีหลัง
- **SL แทบไม่เกิด**: 41/6,776 = 0.6% — SL ไม่ใช่ตัวหยุดขาดทุนจริง, FLIP คือตัวหยุด
- **TIMEOUT หายาก**: 7 เทรดเท่านั้นถือถึง 96 bars (24 ชม.)

---

## EXP2: PnL vs Holding Duration — **CRITICAL PATTERN!**

| Bars | Trades | WR | PnL | TP% | FLIP% | SL% | TRAIL% |
|------|--------|-----|-----|-----|-------|-----|--------|
| **1-4** | **4,794** | **79.8%** | **+$28,371** | 0% | 6% | 0% | **94%** |
| 5-8 | 729 | 59.1% | -$1,279 | 0% | 21% | 1% | 78% |
| 9-16 | 663 | 50.5% | -$5,453 | 0% | 32% | 2% | 65% |
| 17-24 | 248 | 62.5% | -$551 | 0% | 24% | 0% | 76% |
| 25-48 | 234 | 47.0% | -$3,560 | 0% | 42% | 2% | 56% |
| **49-72** | **75** | **18.7%** | **-$3,120** | 0% | **73%** | 5% | 21% |
| 73-96 | 33 | 24.2% | -$1,267 | 0% | 52% | 3% | 24% |

### การค้นพบที่สำคัญที่สุด:

**"Speed is the Edge"** — เทรดที่ชนะเร็ว!

- **70% ของเทรดจบภายใน 4 bars (1 ชม.)** — แทบทั้งหมดเป็น TRAIL winners
- **หลัง 4 bars PnL ติดลบทุก bucket** ยกเว้น 17-24
- **ยิ่งถือนาน FLIP% ยิ่งสูง**: 6% → 21% → 32% → 42% → 73%
- **เทรด 49-72 bars**: WR แค่ 18.7%, FLIP 73% — แทบเป็นเทรดที่ตายแน่

**Metaphor**: Strategy เปรียบเหมือนนักวิ่ง sprint — ชนะเร็วที่สุดภายใน 1 ชม. ถ้ายังไม่ชนะหลังจากนั้น โอกาสยิ่งลดลงเรื่อยๆ

---

## EXP3: Conditional Recovery — ถ้า Underwater แล้วจะ Recover ได้ไหม?

| After N bars | Underwater Count | Recovery Rate | Above Water Count | Win Rate |
|--------------|-----------------|---------------|-------------------|----------|
| 2 bars (30m) | 2,556 | **54.1%** | 1,040 | 80.2% |
| 4 bars (1h) | 1,868 | **50.7%** | 430 | 72.8% |
| 8 bars (2h) | 1,241 | **47.3%** | 146 | 75.3% |
| 12 bars (3h) | 854 | **47.3%** | 85 | 74.1% |
| 16 bars (4h) | 587 | **46.5%** | 55 | 81.8% |
| 24 bars (6h) | 343 | **37.0%** | 21 | 71.4% |
| 32 bars (8h) | 235 | **27.2%** | 8 | 100.0% |
| 48 bars (12h) | 117 | **19.7%** | 3 | 66.7% |

### Key Insights:

1. **Recovery rate ลดลงช้ากว่าคาด!** หลัง 12 bars (3 ชม.) ยัง 47% — เกือบ coin flip
2. **จุดเปลี่ยน: 24 bars (6 ชม.)** — recovery ลดเหลือ 37%, แสดงว่าเทรดเริ่ม "ตาย" จริงๆ
3. **32+ bars**: recovery 27% → ส่วนใหญ่ไม่กลับมา
4. **Above-water trades**: WR 72-82% คงที่ — ถ้ากำลังจะชนะ มันจะชนะจริง

**ทำไม recovery ยังสูงแม้ underwater?** — เพราะ BTC score สามารถกลับทิศได้ (ยืนยัน M022: FLIP เป็น sudden event) ดังนั้นเทรดที่ underwater อาจยังมี score ถูกทิศอยู่ แค่ราคา alt ยังไม่ตาม

---

## EXP4: Time Stop Grid Search — **ทุกตัว FAIL!**

| max_hold (bars) | Hours | Trades | WR | PnL | ΔPNL | FLIPs | TIMEOUTs |
|-----------------|-------|--------|-----|-----|------|-------|----------|
| **8** | 2h | 7,817 | 63.0% | $2,883 | **-$10,259** | 460 | 1,490 |
| 12 | 3h | 7,511 | 66.0% | $4,335 | -$8,807 | 605 | 962 |
| 16 | 4h | 7,296 | 67.7% | $6,497 | -$6,645 | 687 | 665 |
| 24 | 6h | 7,111 | 70.0% | $10,264 | -$2,877 | 741 | 380 |
| 32 | 8h | 7,030 | 70.9% | $11,148 | -$1,993 | 768 | 255 |
| 48 | 12h | 6,894 | 71.6% | $12,511 | -$631 | 817 | 117 |
| 64 | 16h | 6,808 | 71.8% | $12,786 | -$355 | 856 | 47 |
| **96** | **24h** | **6,776** | **72.0%** | **$13,142** | **$0** | **874** | **7** |

### Analysis:

**ยิ่ง time stop เข้ม ยิ่งเสีย PnL!** ทุกค่าที่ต่ำกว่า 96 ทำให้ PnL ลดลง

ทำไม?
1. **TIMEOUT สร้าง re-entry churning**: ออกแล้วเข้าใหม่ → เสีย fee + slippage
2. **ตัด slow winners**: เทรดที่ underwater ตอน timeout บาง trade จะ recover ได้ (47% recovery!)
3. **FLIPs ไม่ลดลง**: Time stop ไม่ได้ลด FLIP — แค่สร้าง TIMEOUT แทน
4. **Monotonic relationship**: ยิ่ง max_hold สูง ยิ่ง PnL สูง — ไม่มี sweet spot ตรงกลาง

---

## EXP5: Conditional Time Stop — Exit เฉพาะ Underwater — **ก็ FAIL!**

| Checkpoint | Trades Cut | Adjusted PnL | ΔPNL | Δ% |
|------------|-----------|-------------|------|-----|
| 8 bars (2h) | 1,241 | $4,142 | **-$9,000** | -68.5% |
| 12 bars (3h) | 854 | $5,537 | -$7,604 | -57.9% |
| 16 bars (4h) | 587 | $7,479 | -$5,663 | -43.1% |
| 24 bars (6h) | 343 | $11,677 | -$1,465 | -11.1% |
| 32 bars (8h) | 235 | $12,081 | -$1,061 | -8.1% |

### Analysis:

**แม้ตัดเฉพาะเทรดที่ขาดทุนก็ยังเสีย PnL!**

ทำไม? — ย้อนกลับไปดู EXP3: recovery rate 47% ที่ 8-16 bars หมายความว่า **เกือบครึ่ง** ของเทรดที่เราจะตัดจะ recover ได้ ถ้าให้เวลามัน

**ถ้า recovery = 50% และ avg win ≈ avg loss** → cutting = zero-sum
**แต่** avg win ($5.71) > avg loss ($-4.50 approx) สำหรับ recovered trades → cutting = negative EV!

---

## EXP6: Walk-Forward — SKIP

ไม่มี conditional time stop ที่ช่วย PnL → ไม่ต้อง walk-forward

---

## สรุป & บทเรียน

### การค้นพบหลัก:

1. **"Speed is the Edge"** — 70% ของเทรดจบใน 1 ชม., TRAIL median = 1 bar (15 นาที). Strategy คือ sprint runner.

2. **FLIP สัมพันธ์กับ duration โดยตรง**: FLIP% เพิ่มจาก 6% (1-4 bars) → 73% (49-72 bars). เทรดที่ยาว = เทรดที่ FLIP.

3. **แต่ time stop ไม่ช่วย**: เพราะ underwater trades ยัง recover ได้ ~47% (coin flip). ตัดเร็วเกินไปจะเสีย winner.

4. **จุดเปลี่ยนคือ 24+ bars (6 ชม.)**: recovery ลดเหลือ 37%. ถ้าจะมี time stop ต้อง > 24 bars แต่ผล PnL ก็ยังติดลบ.

5. **Monotonic = ไม่มี sweet spot**: ยิ่ง max_hold_bars มาก ยิ่ง PnL ดี. ไม่มีจุดที่ลดลงแล้วดีขึ้น.

### ทำไม Time Stop ไม่ work สำหรับ strategy นี้?

Strategy นี้เป็น **mean-reversion** ที่อาศัย BTC cascade → ราคากลับทิศ. บาง cascade ใช้เวลานานกว่าจะ manifest ในราคา alt:
- **Fast cascade** (1-4 bars): ราคา snap back เร็ว → TRAIL จับได้
- **Slow cascade** (5-24 bars): ราคาค่อยๆ กลับ → ต้องให้เวลา
- **Dead cascade** (24+ bars): ราคาไม่กลับ → รอ SIGNAL_FLIP ฆ่า

ปัญหาคือ slow cascade กับ dead cascade **แยกไม่ออก** ตอนที่ยังอยู่ในเทรด

### เปรียบเทียบกับ missions ก่อนหน้า:

| Approach | Mission | Result | Why |
|----------|---------|--------|-----|
| Entry chop filter | M021 | FAIL | Chop ตอน entry ≠ chop ระหว่างเทรด |
| Score-based exit | M022 | FAIL | FLIP เป็น sudden event |
| Factor surgery | M024 | FAIL | OB สร้างเทรดดี > เทรดเสีย |
| **Time stop** | **M025** | **FAIL** | **Underwater recovery ~47%, ตัดเสีย winners** |

**Pattern**: ทุก exit optimization FAIL → SIGNAL_FLIP เป็นปัญหาโครงสร้างที่ไม่สามารถแก้ด้วย exit rule

### Action Items:

1. **อย่าเพิ่ม time stop** — max_hold_bars=96 เป็นค่าที่ดีอยู่แล้ว
2. **อย่าตัด underwater trades** — recovery rate ยังสูงพอที่จะ justify การถือต่อ
3. **SIGNAL_FLIP คำตอบสุดท้าย**: ถ้า exit ทุกรูปแบบไม่ช่วย → คำตอบอาจอยู่ที่ **position sizing** (M019: +23%) หรือ **ยอมรับว่า FLIP = structural cost** ของ strategy
4. **v6 (hyst=3.0, exit_only)** อาจเป็นคำตอบที่ดีที่สุดแล้ว — ลด FLIP โดยไม่ reverse position

### Principle ใหม่:
> **"Speed is the Edge"** — เทรดของ strategy นี้ชนะหรือแพ้ภายใน 1-4 bars. ถ้ายังไม่ชนะหลัง 4 bars, โอกาสจะค่อยๆ ลดลง แต่ recovery rate ยังสูงพอที่ไม่ควรตัดเร็ว.

---

**XP**: +100 | **Level**: 22 | **Type**: IMPORTANT FAIL
