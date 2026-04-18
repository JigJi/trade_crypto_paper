# Mission 015: Pre-Cascade BTC Fingerprint -- BTC ทำอะไรก่อน Liquidation Cascade?

**วันที่**: 2026-03-24
**ประเภท**: factor_deep_dive / signal_quality
**ความยาก**: hard (7 experiments, 5,102 trades, 736 cascade episodes, v6 liq-only)
**สถานะ**: COMPLETED -- **SIGNIFICANT** -- ค้นพบ "Squeeze -> Pop -> Cascade" pattern

---

## แรงบันดาลใจ

Mission 014 วิเคราะห์ว่าอะไรแยก cascade ดีกับ cascade แย่ (OUTPUT side) แต่ยังไม่เคยดู INPUT side -- **BTC ทำอะไรก่อนเกิด cascade?**

v6 เป็น reactive system (เข้าเทรดหลัง cascade fires) ถ้าเราเข้าใจ "ลายนิ้วมือ" ของ BTC ก่อนเกิด cascade จะ:
1. เข้าใจว่า cascade เกิดจากอะไร (volatility compression? momentum buildup?)
2. แยก winning trades จาก losing trades ได้ดีขึ้น
3. อาจ pre-position ก่อน cascade (proactive vs reactive)

### จากงานวิจัยภายนอก
Amberdata analysis ของ October 2025 crash ($3.21B liquidated ใน 60 วินาที) แสดงว่า cascade เกิดจาก feedback loop: forced selling -> price drop -> more liquidations -> cascade. Binance Nov 2025 ก็เช่นกัน -- cascade มักเกิดหลัง price เคลื่อนที่ strongly ใน direction เดียวจน hit liquidation levels.

---

## Config (v6 liq-only)

| Parameter | Value |
|-----------|-------|
| cascade_mult | 1.1x MA |
| liq_w / tick_w | 8.0 / 8.0 |
| SL / TP | 25.0 / 20.0 ATR |
| OOS | Jan 2025 - Mar 2026 |
| Coins (EXP6) | BTC, XRP, ADA, DOT, SUI, FIL |

**Baseline**: 5,102 trades, WR 75.5%, PnL $21,671, avg $4.25/trade

---

## EXP1: Pre-Cascade vs Random Bar Comparison (736 cascade episodes)

### Features ที่ significant (p < 0.001):

| Feature | LB (bars) | Pre-Cascade | Random | Diff% | t-stat | p-val |
|---------|-----------|-------------|--------|-------|--------|-------|
| **range_pct** | 4 | 0.270% | 0.335% | **-19.4%** | -6.03 | 0.0000 |
| **range_pct** | 8 | 0.267% | 0.335% | **-20.4%** | -6.33 | 0.0000 |
| **body_pct** | 4 | 0.132% | 0.167% | **-21.2%** | -4.83 | 0.0000 |
| **body_pct** | 8 | 0.130% | 0.167% | **-22.3%** | -5.08 | 0.0000 |
| **bb_bandwidth** | 1 | 0.596% | 0.734% | **-18.8%** | -5.98 | 0.0000 |
| **bb_bandwidth** | 4 | 0.589% | 0.734% | **-19.7%** | -6.28 | 0.0000 |
| **bb_bandwidth** | 8 | 0.591% | 0.734% | **-19.4%** | -6.19 | 0.0000 |
| **range_z** | 1 | +0.167 | -0.057 | **+394%** | 6.16 | 0.0000 |
| **vol_ratio** | 1 | 1.115 | 1.015 | **+9.9%** | 4.27 | 0.0000 |

### Features ที่ไม่ significant:
- **Returns (ret_1, ret_4, ret_8)**: ไม่มี directional bias ก่อน cascade (p > 0.2 ทั้งหมด)
- **RSI**: ไม่ extreme ก่อน cascade (p > 0.05 ยกเว้น LB-8 marginally)

### INSIGHT: "Squeeze -> Pop -> Cascade" Pattern!

```
  4-8 bars before        1 bar before         CASCADE BAR
  (1-2 ชม.ก่อน)         (15 นาทีก่อน)

  QUIET PERIOD           INITIAL KICK-UP      FULL CASCADE
  - range ต่ำ 20%        - range_z สูง 394%   - liq spike fires
  - body ต่ำ 22%         - vol สูง 10%        - v6 signal triggers
  - BB ต่ำ 19%           - range expanding     - trade entry
  - vol ปกติ             - volume spikes
```

**BTC "หายใจเข้า" (compress) 1-2 ชม.ก่อน cascade แล้ว "ระเบิด" (expand) ใน 1-2 bars สุดท้าย**

---

## EXP2: Readiness Score (Hypothesis Test)

สมมติฐาน: ถ้าสร้าง composite score จาก pre-cascade conditions (RSI extreme, strong momentum, low range, BB squeeze) จะทำนาย cascade ได้

**ผลลัพธ์: สมมติฐานผิด!**

| Lookback | Pre-Cascade | Random | t-stat | p-val |
|----------|-------------|--------|--------|-------|
| LB-1 | **2.43** | 2.71 | -6.89 | 0.0000 |
| LB-4 | 2.66 | 2.71 | -1.33 | 0.1848 |
| LB-8 | 2.64 | 2.71 | -1.81 | 0.0705 |

Pre-cascade bars มี readiness **ต่ำกว่า** random! เพราะ readiness score ให้คะแนน "extreme conditions" แต่ cascade เกิดหลัง **calm period** ไม่ใช่ extreme period.

---

## EXP3: Readiness vs Trade Outcome (BTC, 893 trades)

แม้ readiness จะทำนาย cascade ไม่ได้ แต่มันสัมพันธ์กับ **คุณภาพเทรด**:

| Readiness | Trades | WR% | Avg PnL | Total PnL |
|-----------|--------|-----|---------|-----------|
| 0-1 | 115 | 53.9% | $1.29 | $148 |
| 1-2 | 174 | 63.2% | $1.67 | $290 |
| **2-3** | **356** | **60.4%** | **$1.51** | **$538** |
| **3-5** | **236** | **73.3%** | **$2.55** | **$602** |
| 5+ | 12 | 75.0% | $3.59 | $43 |

**Correlation = 0.0695** (เล็กแต่ positive)

CONTEXT (volatility level, momentum) ตอนเข้าเทรดส่งผลต่อ outcome แม้ไม่ได้ทำนาย cascade timing

---

## EXP4: Price Trajectory ก่อน/หลัง Cascade

| Offset | Bull Cascade | Bear Cascade |
|--------|-------------|-------------|
| -12 bars (3h before) | -0.041% | +0.078% |
| -8 bars (2h before) | -0.057% | +0.083% |
| -4 bars (1h before) | -0.030% | +0.042% |
| -1 bar (15min before) | -0.021% | +0.011% |
| **0 (CASCADE)** | **0.000%** | **0.000%** |
| +4 bars (1h after) | **+0.049%** | **-0.020%** |

### INSIGHT: Mean-Reversion Trajectory!
- **Bullish cascade** ถูกนำด้วย price DECLINE 3 ชม. (avg -0.06%) -> cascade fires -> price recovers (+0.05%)
- **Bearish cascade** ถูกนำด้วย price INCREASE 3 ชม. (avg +0.08%) -> cascade fires -> price drops (-0.02%)

**สิ่งนี้ validates v6 contrarian logic**: ราคาเคลื่อนไปทาง A -> liquidate คนที่อยู่ทาง B -> cascade fires -> ราคากลับทาง B (mean revert)

---

## EXP5: Volatility Compression Confirmed (p < 0.001)

| Metric | BB Bandwidth | Diff vs Random |
|--------|-------------|----------------|
| 4-bar avg BEFORE cascade | 0.00592 | **-19.3%** |
| AT cascade bar | 0.00599 | -18.4% |
| Random bars | 0.00734 | baseline |

**t = -6.14, p = 0.0000**

Vol compression ยืนยันทางสถิติ: Bollinger Bands แคบลง 19% ก่อน cascade เกิด. แต่ ณ cascade bar ยังไม่ expand -- expansion เกิดหลัง cascade เริ่ม

---

## EXP6: Readiness Filter Backtest (6 coins)

| Filter | Trades | %Kept | WR% | PnL | Delta |
|--------|--------|-------|-----|-----|-------|
| None (baseline) | 5,102 | 100% | 75.5% | $21,671 | -- |
| Readiness >= 1.5 | 4,406 | 86% | 76.3% | $19,253 | **-$2,418** |
| Readiness >= 2.0 | 3,757 | 74% | 76.3% | $16,371 | **-$5,300** |
| Readiness >= 2.5 | 3,227 | 63% | 76.2% | $13,835 | **-$7,836** |
| Readiness >= 3.0 | 2,323 | 46% | 78.0% | $11,032 | **-$10,639** |

### INSIGHT: Filtering HURTS PnL (อีกครั้ง!)
เหมือน lesson #23 -- confidence filter boost WR แต่ลด opportunity. เทรดที่ถูก filter ออกยังมี positive expectancy (+$1.77/trade avg).

**กฎ: ใช้ quality metrics สำหรับ SIZING ไม่ใช่ FILTERING** (สอดคล้องกับ Mission 014 lesson #84)

---

## EXP7: Winner vs Loser Pre-Cascade Features

| Feature | Winners | Losers | t-stat | p-val | Significant? |
|---------|---------|--------|--------|-------|-------------|
| **readiness** | 2.67 | 2.54 | 3.90 | 0.0001 | YES |
| **bb_bandwidth** | 0.0066 | 0.0055 | 6.71 | **0.0000** | YES |
| **rsi_14** | 48.68 | 50.36 | -3.30 | 0.0010 | YES |
| range_z | 0.034 | 0.004 | 0.95 | 0.3405 | no |
| ret_4 | -0.0002 | -0.0002 | -0.20 | 0.8434 | no |

### INSIGHT: BB Bandwidth แยก Winner/Loser ได้ดีที่สุด!

**Winning trades** เกิดเมื่อ BB bandwidth สูง (0.0066 vs 0.0055, +20%)

ทำไม? เพราะ:
- **BW สูง = volatility ปกติ/สูง** -> cascade เป็น mean-reversion event (ราคาจะกลับ) -> WIN
- **BW ต่ำ = squeeze zone** -> cascade อาจเป็น **breakout ของ squeeze** ไม่ใช่ mean-reversion -> LOSE

Cascade ใน **active volatility** context = mean-reversion (ดี)
Cascade ใน **squeeze** context = อาจเป็นจุดเริ่มต้น trend ใหม่ (อันตราย)

---

## สรุป: 5 Insights หลัก

### 1. "Squeeze -> Pop -> Cascade" เป็น pattern จริง (p < 0.001)
BTC compresses volatility 19% (BB bandwidth) เป็นเวลา 1-3 ชม. ก่อน cascade จากนั้น range + volume เพิ่มขึ้นใน bar สุดท้ายก่อน cascade fires. **เหมือนสปริงถูกกด -- แล้วดีดกลับ.**

### 2. Price trajectory ยืนยัน contrarian logic
Bullish cascade ถูกนำด้วย decline, bearish ถูกนำด้วย incline. Price moves ทิศตรงข้ามกับ cascade direction ก่อนเกิด. นี่คือ **mean-reversion pattern** ที่ v6 ทำกำไร.

### 3. Cascade TIMING ไม่สามารถทำนายได้จาก price action
Returns ก่อน cascade ไม่มี statistical significance. RSI ปกติ. **ไม่มีทางรู้ว่า cascade จะเกิดเมื่อไหร่** จาก price action alone. v6 ที่ reactive (เข้าหลัง cascade fires) คือแนวทางที่ถูกแล้ว.

### 4. Filtering by pre-conditions HURTS (lesson confirmed)
Readiness filter boost WR 75.5% -> 78.0% แต่ลด PnL $21,671 -> $11,032 (-49%). เทรดที่ถูก skip ยังกำไรเฉลี่ย +$1.77. **อย่า filter ออก -- ปรับขนาดแทน.**

### 5. BB Bandwidth = Quality Signal สำหรับ Position Sizing (ACTIONABLE)
Winners มี BB bandwidth สูงกว่า 20% (p=0.0000). ใช้เป็น quality metric:
- **BW > 0.006**: cascade ใน active vol = confident mean-reversion -> **ขนาดใหญ่ (1.5x)**
- **BW 0.004-0.006**: ปกติ -> **ขนาดปกติ (1.0x)**
- **BW < 0.004**: cascade ใน squeeze = อาจเป็น breakout -> **ขนาดเล็ก (0.7x)**

---

## Action Items

1. **BB Bandwidth position sizing** (ต่อยอดจาก v6 cascade quality sizing ที่ deploy แล้ว):
   - เพิ่ม bb_bandwidth check ใน size multiplier logic
   - ทดสอบด้วย portfolio backtest ก่อน deploy

2. **ไม่ทำ pre-cascade prediction**: data ชัดเจนว่า cascade timing predict ไม่ได้ จาก price action. ไม่ต้องเสียเวลากับ approach นี้อีก.

3. **Future mission**: ทดสอบว่า Funding Rate extreme ทำนาย cascade timing ได้หรือไม่ (ต่างจาก price action เพราะเป็น structural data ไม่ใช่ price data)

---

## Tags
`pre_cascade`, `volatility_compression`, `bb_bandwidth`, `squeeze_pattern`, `position_sizing`, `v6_liq_only`, `mean_reversion`
