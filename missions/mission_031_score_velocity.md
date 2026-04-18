# Mission 031: BTC Score Velocity & Flip Prediction

**วันที่**: 2026-04-09
**ประเภท**: factor_research / signal_quality / flip_prediction
**ความยาก**: hard (6 experiments, 3,856 trades, velocity/acceleration analysis)
**สถานะ**: COMPLETED -- **HYPOTHESIS FAILED** -- velocity ไม่ทำนาย SIGNAL_FLIP

---

## แรงบันดาลใจ

ต่อยอดจาก Mission 003 (score magnitude ทำนาย quality), Mission 021 (chop detection), Mission 023 (factor conflict)

**สมมติฐาน**: เมื่อ BTC composite score เปลี่ยนเร็ว (velocity สูง) → signal ไม่มั่นคง → SIGNAL_FLIP probability สูงขึ้น → ควรหลีกเลี่ยง entry ในช่วง high velocity

**ทำไมถึงน่าลอง**: SIGNAL_FLIP คือปัญหาหลักของ paper trading (-$1,345 จาก 12 วัน) ถ้า velocity ทำนาย FLIP ได้ จะเป็น filter ที่ดี

---

## EXP1: Score Velocity Distribution (OOS Period)

| Metric | Value |
|--------|-------|
| Total bars (OOS) | 43,152 |
| Score = 0 | **54.9%** |
| Score > 0 (LONG) | 20.3% |
| Score < 0 (SHORT) | 24.8% |
| vel_4 std | 1.23 |
| abs_vel_4 median | 0.0 |
| abs_vel_4 P75 | 1.0 |
| abs_vel_4 P90 | 2.0 |

### Key Finding:
- **Score = 0 มากกว่าครึ่ง (55%)** -- ระบบไม่มี signal ส่วนใหญ่ของเวลา
- Score เป็น SHORT (24.8%) มากกว่า LONG (20.3%) -- สอดคล้องกับ SHORT > LONG edge
- Velocity median = 0 เพราะ score มักไม่เปลี่ยน (sticky)

---

## EXP2: Velocity → SIGNAL_FLIP Correlation

| Metric | FLIP Trades | Non-FLIP Trades |
|--------|-------------|-----------------|
| Abs vel4 mean | 1.47 | 1.73 |
| Dir vel4 mean | 0.64 | 0.97 |
| Accel mean | similar | similar |

| Correlation | Value |
|-------------|-------|
| abs_vel4 vs FLIP | **-0.032** (ใกล้ศูนย์) |
| abs_vel4 vs WIN | **-0.011** (ใกล้ศูนย์) |
| dir_vel4 vs FLIP | **-0.029** (ใกล้ศูนย์) |

### CRITICAL FINDING:
- **Correlation ใกล้ศูนย์ทุกตัว** -- velocity ไม่มีความสัมพันธ์กับ FLIP เลย!
- FLIP trades มี velocity **ต่ำกว่า** non-FLIP เล็กน้อย (1.47 vs 1.73)
- สมมติฐาน "high velocity → more FLIP" ผิด อาจจะตรงข้ามด้วยซ้ำ

---

## EXP3: Velocity Quartile → Trade Outcome

### Absolute Velocity Quartiles:

| Quartile | Trades | WR% | PnL | FLIP% |
|----------|--------|-----|-----|-------|
| Q1 (low vel) | 983 | 75.4% | $1,261 | 3.2% |
| Q2 | 1,030 | **76.5%** | **$3,334** | 5.0% |
| Q3 | 957 | 69.7% | $1,832 | 3.2% |
| Q4 (high vel) | 886 | **77.3%** | **$3,199** | **2.0%** |

### Direction-Aware Velocity:

| Quartile | WR% | FLIP% |
|----------|-----|-------|
| Q1 (weakening) | 75.6% | 3.6% |
| Q2 | 72.5% | 5.7% |
| Q3 | 73.6% | 2.8% |
| Q4 (strengthening) | 75.7% | 2.3% |

### SHOCKING:
- **Q4 (high velocity) มี WR สูงสุด (77.3%) และ FLIP ต่ำสุด (2.0%)**!
- **สมมติฐานผิดทิศทาง** -- high velocity = signal conviction สูง ไม่ใช่ noise
- Q2 ทำกำไรมากสุด ($3,334) -- ไม่ใช่ Q1 ที่ velocity ต่ำ
- **ไม่มี monotonic relationship** ระหว่าง velocity กับ performance

---

## EXP4: Score Acceleration Analysis

| Metric | Favorable Accel | Unfavorable Accel |
|--------|-----------------|-------------------|
| % of trades | 9.2% | 90.8% |
| WR | 74.0% | 74.8% |
| PnL | $536 | $9,089 |
| FLIP% | 2.5% | 3.5% |

### Finding:
- **Favorable acceleration (score accelerating toward signal direction) เกิดแค่ 9.2%**
- WR แทบไม่ต่างกัน (74.0% vs 74.8%) -- acceleration ไม่ช่วยทำนาย
- FLIP ต่างกัน 1pp -- ไม่ significant

---

## EXP5: Velocity Filter Backtest

| Filter | Trades | Kept% | WR% | PnL | FLIP% | Avg PnL |
|--------|--------|-------|-----|-----|-------|---------|
| **No filter** | **3,856** | **100%** | **74.7%** | **$9,625** | **3.4%** | **$2.50** |
| vel < P90 | 2,818 | 73.1% | 73.8% | $5,552 | 3.9% | $1.97 |
| vel < P75 | 1,556 | 40.4% | 74.1% | $2,624 | 4.2% | $1.69 |
| vel < P50 | 643 | 16.7% | 76.2% | $1,063 | 1.9% | $1.65 |

### VERDICT: ทุก filter ทำให้ PnL ลดลง!
- Avg PnL/trade ลดลงจาก $2.50 เมื่อ filter เข้มขึ้น
- **High velocity trades มี avg PnL สูงกว่า** -- filter ออกแล้วเสียหาย
- P50 filter ลด FLIP จาก 3.4% → 1.9% แต่สูญเสีย PnL $8,562 (89%!)

---

## EXP6: Combined Velocity + Score Magnitude

| Combo | Trades | Kept% | WR% | PnL | Avg PnL |
|-------|--------|-------|-----|-----|---------|
| Baseline | 3,856 | 100% | 74.7% | $9,625 | $2.50 |
| vel<P75 + |score|>=4 | 393 | 10.2% | 76.1% | $1,132 | **$2.88** |
| vel<P90 + |score|>=4 | 688 | 17.8% | 74.9% | $1,897 | $2.76 |
| vel<P50 + fav accel | 43 | 1.1% | 72.1% | -$38 | -$0.89 |

### Finding:
- Best avg PnL คือ vel<P75 + |score|>=4 ($2.88/trade) แต่สูงกว่า baseline แค่ $0.38
- **ไม่คุ้มที่จะตัด 90% ของ trades เพื่อได้เพิ่ม $0.38/trade**
- Combined filter ไม่มี synergy -- velocity ไม่ add value เหนือ magnitude

---

## สรุปรวม -- ทำไมสมมติฐานผิด?

### 1. Score Velocity ≠ Instability
ที่คิดว่า "score เปลี่ยนเร็ว = ไม่มั่นคง" ผิด เพราะ:
- High velocity หมายถึง **factors หลายตัว agree พร้อมกัน** → conviction สูง
- เมื่อ liquidation + funding + OB + basis ยิง signal พร้อมกัน → score กระโดด → velocity สูง → แต่ signal จริงมาก

### 2. SIGNAL_FLIP ใน backtest แค่ 3.4%
- Paper trading FLIP สูง (~45%) เพราะ conditions ต่างกัน (no hysteresis, more coins, real-time)
- ใน backtest FLIP rare เกินไปจะมองเห็น pattern จาก velocity

### 3. Score เป็น Discrete ไม่ใช่ Continuous
- Score = 0 ถึง 55% ของเวลา → velocity เป็น 0 บ่อย
- เมื่อ score เปลี่ยน มักกระโดดเป็นขั้น (เพราะ factor weight เป็น integer-like)
- Velocity analysis เหมาะกับ continuous signal มากกว่า

### 4. สิ่งที่ค้นพบแทน (unexpected insights)
- **Score polarity**: SHORT territory (24.8%) > LONG territory (20.3%) -- confirms SHORT edge
- **55% of time = no signal** -- ระบบ selective มาก ซึ่งดี
- **High velocity = high conviction** -- ตรงข้ามกับสมมติฐาน

---

## Actionable Items

1. **ไม่ควรเพิ่ม velocity filter** -- จะทำให้เสีย edge
2. **Score magnitude (|score|) ยังเป็น metric ที่ดีที่สุด** สำหรับ quality (จาก Mission 003)
3. **SIGNAL_FLIP problem ต้องแก้ที่ paper trading layer** ไม่ใช่ backtest analysis
   - Paper FLIP 45% vs Backtest FLIP 3.4% → gap มาจาก implementation ไม่ใช่ signal
4. **Score = 0 (55% of time)** → อาจศึกษา "what happens when score just turned non-zero" (score onset)

---

## Lesson Learned
- **Velocity ≠ Instability** สำหรับ discrete composite score
- High velocity = multiple factors aligning = STRONG signal
- FLIP ใน backtest (3.4%) vs paper (45%) ต่างกันมาก → ต้องวิเคราะห์แยก
- Hypothesis fail = valuable data point: ไม่ต้องเสียเวลากับ velocity filter อีก
