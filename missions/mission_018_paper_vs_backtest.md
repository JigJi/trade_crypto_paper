# Mission 018: Paper Trading vs Backtest Reality Gap

**วันที่**: 2026-03-27
**ประเภท**: reality_check / system_validation
**ความยาก**: hard (7 experiments, 827 paper trades vs 443 backtest trades, 46 coins)
**สถานะ**: COMPLETED -- **CRITICAL FINDING** -- ค้นพบ root cause ของ WR gap 26.4pp

---

## แรงบันดาลใจ

Paper trading รัน 10 วัน (17-26 มี.ค.) มี 827 trades จาก 46 coins. WR ในกระดาษ = ~45% ในขณะที่ backtest WR = ~71%. **Gap 26pp นี้ใหญ่เกินกว่าจะเป็นแค่ slippage หรือ bad luck** -- ต้องหา root cause

---

## EXP1: Overall Comparison (Paper vs Backtest, same 10-day window)

| Metric | Paper | Backtest | Gap |
|--------|-------|----------|-----|
| Trades | 827 | 443 | Paper เทรดเยอะกว่า 86% |
| **Win Rate** | **44.9%** | **71.3%** | **-26.4pp** |
| Total PnL | $763.19 | N/A (% based) | Paper ยังกำไร |

**Key finding**: Paper เทรดเยอะกว่า backtest เกือบ 2 เท่า (827 vs 443) = ระบบ paper เข้าเทรดก้าวร้าวกว่า

---

## EXP2: Per-Model Version

| Model | Paper Trades | Paper WR | BT Trades | BT WR | Gap |
|-------|-------------|----------|-----------|-------|-----|
| v3 | 305 | 47.5% | 187 | 67.4% | -19.9pp |
| **v5** | **272** | **43.0%** | **164** | **76.2%** | **-33.2pp** |
| v6 | 250 | 43.6% | 92 | 70.7% | -27.1pp |

**Key finding**: v5 มี gap ใหญ่ที่สุด (-33.2pp) ทั้งๆ ที่ backtest สูงสุด (76.2%)

---

## EXP3: Direction Asymmetry

| Direction | Paper WR | BT WR | Gap |
|-----------|----------|-------|-----|
| **LONG** | **34.6%** (347t) | **67.7%** (167t) | **-33.1pp** |
| SHORT | 52.3% (480t) | 73.6% (276t) | -21.3pp |

**Key finding**:
- LONG มี gap ใหญ่กว่า SHORT ถึง 12pp (-33.1 vs -21.3)
- Paper มี SHORT bias ชัดเจน (480 vs 347) ซึ่งสอดคล้องกับ SHORT > LONG principle
- LONG 34.6% WR ใน paper = **ขาดทุนเกือบทุก 2 ใน 3 trades**

---

## EXP4: Exit Mechanism -- **ROOT CAUSE #1**

### Paper exits:
| Exit | Trades | % | WR | PnL |
|------|--------|---|----|-----|
| **SIGNAL_FLIP** | **526** | **63.6%** | **22.8%** | **-$897.51** |
| TP | 171 | 20.7% | 99.4% | +$1,305.16 |
| TIMEOUT | 49 | 5.9% | 75.5% | +$118.13 |
| SL/TP | 45 | 5.4% | 80.0% | +$565.45 |
| SL | 36 | 4.4% | 22.2% | -$328.04 |

### Backtest exits:
| Exit | Trades | % | WR |
|------|--------|---|----|
| TRAIL | 372 | 84.0% | 84.4% |
| SIGNAL_FLIP | 71 | 16.0% | 2.8% |

### วิเคราะห์:
**SIGNAL_FLIP คือ killer ตัวจริง!**
- Paper: 64% ของ trades ออกด้วย SIGNAL_FLIP, WR แค่ 22.8%, ขาดทุนรวม -$898
- Backtest: 84% ออกด้วย TRAIL (WR 84.4%), SIGNAL_FLIP แค่ 16%
- **ถ้าตัด SIGNAL_FLIP losses ออก**: Paper PnL จะเป็น $763 + $897 = **+$1,661** (WR 70%+)

**STRUCTURAL MISMATCH**:
- Backtest ใช้ trailing stop (0.5 ATR) เป็นหลัก = ออกเร็ว ล็อคกำไร
- Paper ใช้ signal flip = รอจนสัญญาณกลับ = ถือนานกว่า = โดน drawdown มากกว่า

---

## EXP5: Entry Score Distribution

| |score| | Trades | WR | Avg PnL |
|---------|--------|----|----|
| 0-3 | 234 | 38.9% | $0.53 |
| 3-5 | 519 | 48.0% | $1.10 |
| 5-8 | 74 | 41.9% | $0.91 |

### Score-Direction Alignment:

| Direction | Aligned | WR | PnL | Misaligned | WR | PnL |
|-----------|---------|----|----|------------|----|----|
| LONG | 296 | 36.5% | +$163 | 51 | 23.5% | -$55 |
| SHORT | 462 | 53.5% | +$665 | 18 | 22.2% | -$10 |

**Key finding**:
- 234 trades (28%) เข้าด้วย score ต่ำ (|s|<3) = noise zone
- Score median = -2.5 (SHORT bias) = สอดคล้องกับ SHORT >> LONG
- Misaligned trades (score ชี้ทางตรงข้าม) WR แค่ 22-24% = **ห้ามเทรดเด็ดขาด**

---

## EXP6: Per-Coin Reality Gap (Top/Bottom)

### Biggest gap (BT >> Paper):
| Coin | Model | Paper WR | BT WR | Gap |
|------|-------|----------|-------|-----|
| ZRO | v6 | 31.2% | 87.5% | -56.2pp |
| CRV | v5 | 31.2% | 85.7% | -54.5pp |
| ASTER | v6 | 37.5% | 88.9% | -51.4pp |
| GALA | v5 | 37.5% | 88.9% | -51.4pp |
| JCT | v6 | 52.9% | 100.0% | -47.1pp |

### Paper > Backtest:
| Coin | Model | Paper WR | BT WR | Gap |
|------|-------|----------|-------|-----|
| BANANAS31 | v6 | 83.3% | 16.7% | +66.7pp |
| NAORIS | v6 | 52.6% | 50.0% | +2.6pp |
| BNB | v5 | 58.8% | 58.3% | +0.5pp |

**Key finding**: Gap กระจายไม่สม่ำเสมอ -- บางเหรียญ gap 50pp+ (STRUCTURAL issue) บางเหรียญใกล้เคียง

---

## EXP7: Holding Period

| Bars Held | Trades | WR | Avg PnL | Total PnL |
|-----------|--------|----|----|-----------|
| 1-4 | 46 | 23.9% | $2.30 | +$106 |
| 4-12 | 129 | 31.8% | $0.41 | +$53 |
| 12-24 | 164 | 47.0% | -$0.08 | -$14 |
| 24-48 | 205 | 43.9% | $1.50 | +$307 |
| 48-96 | 220 | 48.2% | -$0.17 | -$37 |
| **96+** | **50** | **76.0%** | **$2.51** | **+$126** |

**Key finding**:
- เทรดสั้น (1-12 bars) WR ต่ำมาก (24-32%) แต่ avg PnL ยังบวก (winners ชนะเยอะ)
- เทรด 96+ bars WR สูงสุด 76% = เทรดที่ถือยาวชนะ
- แต่ TIMEOUT (96 bars) แค่ 5.9% = ส่วนใหญ่ถูกบังคับออกก่อนถึง timeout

---

## Root Cause Analysis

### #1: SIGNAL_FLIP เป็น toxic exit (63.6% ของ trades, WR 22.8%)
- Backtest ใช้ trailing stop เป็นหลัก (84% of exits)
- Paper ใช้ signal flip เป็นหลัก (64% of exits)
- **นี่คือ structural mismatch ที่ใหญ่ที่สุด**
- SIGNAL_FLIP ออกเร็วเกินไปก่อนที่ trade จะมีเวลา work out

### #2: LONG trades broken (WR 34.6%, gap -33pp)
- SHORT ดีกว่าทั้ง paper และ backtest (สอดคล้องกับ lessons learned)
- LONG WR 34.6% = ทุก 3 trades แพ้ 2 ครั้ง
- ช่วง 10 วันนี้อาจเป็น bearish bias ทำให้ LONG แย่ลง

### #3: Paper เทรดมากเกินไป (827 vs 443)
- Paper เข้าเทรด ~86% มากกว่า backtest
- แปลว่า paper เข้า signal ที่ backtest ไม่เข้า (อาจเพราะ exit mechanism ต่างกัน)

### #4: Misaligned trades (score ชี้ทางตรงข้าม) WR 22-24%
- 69 trades ที่ score ชี้ตรงข้ามกับ direction = ขาดทุนเกือบทุกตัว
- ไม่ควรเกิดขึ้นเลยถ้า logic ถูกต้อง

---

## ข้อเสนอแนะ (Actionable)

1. **ตรวจสอบ SIGNAL_FLIP logic ใน paper trader** -- ทำไมมัน trigger 64% ของ trades?
   - Backtest: TRAIL exits 84% (quick profit-taking)
   - Paper: ไม่มี TRAIL = ถือจนกว่า score จะ flip = โดน drawdown
   - **ACTION**: เพิ่ม trailing stop mechanism ใน paper trading เหมือน backtest

2. **พิจารณาปิด LONG trades** -- WR 34.6% = structural loss
   - หรืออย่างน้อยเพิ่ม threshold สำหรับ LONG (เช่น +5 แทน +3)

3. **Block misaligned trades** -- ถ้า score < 0 ห้ามเทรด LONG, ถ้า score > 0 ห้ามเทรด SHORT

4. **ตรวจสอบว่า paper trader เข้าเทรดที่ score เท่าไหร่** -- 234 trades ที่ |score| < 3 = noise

---

## สรุป

**SIGNAL_FLIP คือ root cause หลักของ WR gap 26pp**. Paper trader ไม่มี trailing stop ทำให้ 64% ของ trades ออกด้วย signal flip (WR 22.8%) แทนที่จะออกด้วย trail (WR 84.4% ใน backtest). ถ้า paper trader ใช้ exit mechanism เดียวกับ backtest (trailing stop) WR น่าจะใกล้เคียง 70%+. นอกจากนี้ LONG trades ยังแย่มาก (WR 34.6%) ควรพิจารณาลด exposure หรือเพิ่ม threshold.
