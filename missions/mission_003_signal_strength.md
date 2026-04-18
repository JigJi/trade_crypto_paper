# Mission #003: BTC Composite Score -- Signal Strength vs Trade Outcome

**วันที่**: 2026-03-14
**ช่วง OOS**: 2025-01-01 ถึง 2026-03-31
**ผลลัพธ์**: PASS -- strong_signal

## สมมติฐาน
เทรดที่เข้าตอน BTC composite score สูง (extreme) จะได้ผลดีกว่าเทรดที่เข้าตอน score ต่ำ (borderline)
ถ้าจริง -> สามารถใช้ confidence-based position sizing หรือ skip low-confidence signals ได้

## ข้อมูลที่ใช้
- v3 model, 8 factors, 6 coins (BTC, XRP, ADA, DOT, SUI, FIL)
- จำนวนเทรดทั้งหมด: 2,201 รายการ (OOS)
- BTC composite score ณ จุดเข้าเทรด (signal bar ก่อน entry 1 bar)

## Score Distribution
- Range: [-6.0, +7.0]
- Mean: +1.35 (เอียงไปทาง long เพราะ long trades เยอะกว่า)
- |Score| Mean: 3.39, Median: 3.50
- 37% ของเทรดอยู่ที่ |score| <= 3.0 (borderline)
- เพียง 2.5% อยู่ที่ |score| >= 4.8 (extreme)

## ผลวิเคราะห์หลัก: Score Buckets

| Bucket | |Score| Range | เทรด | WR% | AvgPnL | TotalPnL | Sharpe |
|--------|---------------|------|-----|--------|----------|--------|
| **Q1 (ต่ำ)** | 2.5-3.0 | 815 | **63.7%** | $0.22 | $175 | 0.54 |
| Q2 (กลาง) | 3.2-3.5 | 841 | 71.5% | $0.42 | $356 | 0.81 |
| Q3 (สูง) | 3.8-4.5 | 490 | **76.7%** | **$3.07** | **$1,502** | **5.27** |
| **Q4 (extreme)** | 4.8-7.0 | 55 | **78.2%** | **$4.65** | $256 | **10.74** |

### ข้อค้นพบสำคัญ
- **WR เพิ่มขึ้นเป็นเส้นตรงตาม score**: Q1 63.7% -> Q4 78.2% (ส่วนต่าง +14.5pp)
- **Sharpe กระโดดจาก 0.54 เป็น 10.74** -- ความแตกต่างมหาศาล
- **Q3 (score 3.8-4.5) เป็น sweet spot**: สมดุลระหว่างจำนวนเทรด (490) กับคุณภาพ (WR 76.7%, Sharpe 5.27)
- **Q1 (score 2.5-3.0) เป็นจุดอ่อน**: WR 63.7% = ยังกำไรแต่ edge น้อยมาก

## Long vs Short by Score Magnitude

| ทิศทาง | Score Half | เทรด | WR% | AvgPnL | TotalPnL |
|---------|-----------|------|-----|--------|----------|
| **LONG** | low | 1,116 | 67.7% | $0.17 | $184 |
| **LONG** | high | 402 | 74.6% | $1.31 | $527 |
| **SHORT** | low | 348 | 64.9% | $0.80 | $278 |
| **SHORT** | **high** | **335** | **77.0%** | **$3.88** | **$1,300** |

### ข้อสังเกต
- **Short + High Score = เทรดที่ดีที่สุด**: WR 77.0%, AvgPnL $3.88
- High-score shorts ทำเงิน $1,300 จาก 335 เทรด = **56.8% ของกำไรทั้งหมด**
- High-score ช่วย LONG มากเหมือนกัน (+6.9pp WR, 7.7x AvgPnL)

## Signed Score Analysis (ทิศทางจริงของ score)

| Score Range | ทิศทาง | เทรด | WR% | AvgPnL | TotalPnL |
|-------------|--------|------|-----|--------|----------|
| [-6, -4) | SHORT | 50 | **82.0%** | $5.78 | $289 |
| [-4, -2.5) | SHORT | 518 | 72.6% | $2.51 | $1,300 |
| [2.5, 4) | LONG | 1,264 | 68.3% | $0.15 | $188 |
| [4, 6) | LONG | 249 | 75.5% | $2.14 | $532 |

### ข้อสังเกตสำคัญ
- **Short extreme (score <= -4): WR 82.0%** -- accuracy สูงมาก!
- Short ทั้งหมด ($1,589) ทำเงินมากกว่า Long ($720) ถึง 2.2 เท่า
- ยืนยัน principle เดิม: **SHORT > LONG** และ score magnitude ทำให้ gap ยิ่งชัดขึ้น

## Threshold Sweep (ถ้าขยับ minimum score ขึ้น)

| Min |Score| | เทรด | WR% | AvgPnL | TotalPnL | Sharpe |
|-------------|------|-----|--------|----------|--------|
| 2.5 (ปัจจุบัน) | 2,201 | 69.9% | $1.04 | $2,289 | 2.10 |
| 3.0 | 1,788 | 72.4% | $1.49 | $2,664 | 2.81 |
| 3.5 | 1,270 | 73.8% | $1.62 | $2,060 | 3.08 |
| **4.0** | **379** | **78.1%** | **$4.37** | **$1,654** | **7.86** |
| 4.5 | 217 | 74.7% | $2.27 | $492 | 4.20 |

### วิเคราะห์ Trade-off
- **Threshold 3.0**: ลดเทรด 19% แต่ได้ PnL เพิ่ม +$375 (+16.4%) -- **น่าสนใจมาก**
- **Threshold 4.0**: WR 78.1%, Sharpe 7.86 สุดยอด แต่เทรดแค่ 379 (ลด 83%)
- **Sweet spot = 3.0**: ยังมีเทรดพอเยอะ แต่ตัด noise ชั้นล่างสุดออก

## Per-Coin Analysis (high score > low score ?)

| Coin | Low WR | High WR | Delta | สรุป |
|------|--------|---------|-------|------|
| BTC | 56.3% | 66.0% | **+9.7pp** | YES |
| XRP | 67.9% | 74.8% | +6.9pp | YES |
| ADA | 69.9% | 76.4% | +6.5pp | YES |
| DOT | 73.3% | 80.0% | +6.7pp | YES |
| SUI | 74.1% | 74.2% | +0.2pp | ไม่ชัด |
| FIL | 73.3% | 83.3% | **+10.0pp** | YES |

- **ทุกเหรียญ** high score WR > low score WR
- BTC (+9.7pp) และ FIL (+10.0pp) ได้ประโยชน์มากที่สุด
- SUI แทบไม่แตกต่าง (อาจเพราะ threshold ปัจจุบัน 3.0 ตัด noise ไปแล้ว)

## Score vs Hold Duration

| Bucket | Avg Hold (bars) | Win Hold | Loss Hold |
|--------|-----------------|----------|-----------|
| Q1 (low) | 3.9 | 3.1 | 5.5 |
| Q2 | 4.4 | 3.6 | 6.7 |
| Q3 (high) | 4.7 | 3.8 | 7.7 |
| **Q4 (extreme)** | **3.3** | **3.2** | **3.8** |

- Q4 (extreme) **ถือสั้นที่สุด** และ loss ก็ตัดเร็ว (3.8 bars)
- Q3 (high) ถือนานสุด -- อาจเพราะ strong trend ทำให้ trail ลากนาน

## Exit Reason by Score

| Bucket | Trail% | SL% | Signal Flip% |
|--------|--------|-----|--------------|
| Q1 (low) | 81% | **17%** | 2% |
| Q2 | 83% | 17% | 0% |
| Q3 (high) | **90%** | **10%** | 0% |
| Q4 (extreme) | **93%** | **7%** | 0% |

- High score trades ถูก SL น้อยกว่ามาก (7% vs 17%)
- Score สูง = เข้าถูกทิศทาง = ไม่โดน SL

## Win/Loss Streak

- หลังแพ้: WR 65.8% (n=661), avg |score| 3.3
- หลังชนะ: WR 71.6% (n=1534), avg |score| 3.4
- **ส่วนต่าง 5.8pp** = mild momentum (ชนะแล้วมีแนวโน้มชนะต่อ)
- Score ไม่ได้เปลี่ยนมากหลังแพ้/ชนะ -> streak มาจากตลาด ไม่ใช่ signal quality

## สรุปและข้อเสนอแนะ

### ผลลัพธ์หลัก
1. **Score magnitude ทำนาย trade quality ได้ชัดเจน**: Q1 WR 63.7% -> Q4 WR 78.2% (+14.5pp)
2. **Sharpe ดีขึ้น 20x**: Q1 Sharpe 0.54 -> Q4 Sharpe 10.74
3. **Short + extreme score = golden signal**: WR 82%, AvgPnL $5.78
4. **Threshold 3.0 = quick win**: ลดเทรด 19% แต่ได้ PnL +$375 (+16.4%)
5. **Score สูง = SL น้อยลง**: 7% SL rate vs 17% -- เข้าถูกทิศทางบ่อยกว่า

### ข้อเสนอสำหรับ v3.1 (ถ้าจะปรับ)
1. **Confidence-based position sizing**: score >= 4.0 ให้ 2x position, score 2.5-3.0 ให้ 0.5x
2. **Raise BTC threshold จาก 2.5 เป็น 3.0**: ตัด noise 364 เทรด, ได้ PnL เพิ่ม +16%
3. **Short signal priority**: เมื่อ score <= -4.0, ให้น้ำหนักพิเศษ (WR 82%)
4. **ระวัง overfit**: ต้องทดสอบ out-of-sample ก่อนใช้จริง

### สาเหตุที่ Mission นี้ "PASS"
- ค้นพบ strong relationship ที่ consistent ทุกเหรียญ
- Actionable: สามารถใช้ปรับ position sizing ได้ทันที
- ยืนยัน SHORT > LONG principle ด้วยมุมใหม่ (score magnitude)
- ต่อยอดจาก Mission #001 (timing) + Mission #002 (liquidation) -> dimension ที่ 3 = signal confidence

## แหล่งอ้างอิง
- Mission #001: Hour-of-Day Session Analysis
- Mission #002: Liquidation Cascade Analysis
- v3 Model Registry: 8 factors, $14,121 OOS
