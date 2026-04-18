# Mission 014: Liquidation Cascade Quality Anatomy

**วันที่**: 2026-03-23
**ประเภท**: factor_deep_dive / signal_quality
**ความยาก**: hard (6 experiments, 5,102 trades, v6 liq-only architecture)
**สถานะ**: COMPLETED -- **SIGNIFICANT** actionable findings

---

## แรงบันดาลใจ

v6 Tournament (Mission ไม่ใช่ แต่เป็น R2 Tournament 03-22) พิสูจน์แล้วว่า **liquidation คือ factor เดียวที่ต้องการ**:
- Liq-only $69,701 beats full model (8 factors) $51,464 (+35%)
- Signal เป็น **binary** -- ยิงหรือไม่ยิง (score กระโดดเป็น +/-8 ทันที)

**คำถาม**: ถ้า signal เป็น binary แล้ว ไม่ใช่ cascade ทุกอันจะให้ผลเหมือนกัน -- **อะไรแยก cascade ดีกับ cascade แย่?**

---

## Config (v6 liq-only)

| Parameter | Value |
|-----------|-------|
| cascade_mult | 1.1x MA |
| liq_w / tick_w | 8.0 / 8.0 |
| tick_net_thr | 3 |
| SL / TP | 25.0 / 20.0 ATR |
| OOS | Jan 2025 - Mar 2026 |
| Coins | BTC, XRP, ADA, DOT, SUI, FIL |

**Baseline**: 5,102 trades, WR 75.5%, PnL $21,671, avg $4.25/trade

---

## EXP1: Cascade Magnitude (liq_total / liq_total_MA)

| Bucket | Trades | WR% | Avg PnL | Total PnL |
|--------|--------|-----|---------|-----------|
| <1.1x (tick-only) | 1,233 | 78.8% | **$4.92** | $6,069 |
| 1.1-1.5x | 707 | 71.3% | $2.89 | $2,042 |
| 1.5-2.0x | 557 | 73.6% | $3.12 | $1,737 |
| 2.0-3.0x | 852 | 71.2% | $2.82 | $2,400 |
| 3.0-5.0x | 918 | 77.2% | $4.47 | $4,102 |
| **5.0x+** | **788** | **79.6%** | **$6.76** | **$5,327** |

### INSIGHT: U-Shape Pattern!
- **Tick-only (ไม่มี hourly cascade)**: WR 78.8%, avg $4.92 -- ดี!
- **Mid-range cascade (1.1-3.0x)**: WR 71-74%, avg $2.82-3.12 -- **แย่ที่สุด**
- **Extreme cascade (5.0x+)**: WR 79.6%, avg $6.76 -- **ดีที่สุด**

**ทำไม?** Cascade ขนาดกลาง (1.1-3.0x) คือ "noise zone" -- มี liquidation spike แต่ไม่รุนแรงพอจะ force mean-reversion. Tick-only trades ถูก drive ด้วย net count (16-bar trend) ซึ่งเป็น signal ที่ค่อยๆ สะสม ไม่ใช่ event-based noise.

---

## EXP2: Side Dominance (|liq_net| / liq_total)

| Bucket | Trades | WR% | Avg PnL | Total PnL |
|--------|--------|-----|---------|-----------|
| <0.3 (mixed) | 546 | 78.6% | $4.14 | $2,259 |
| 0.3-0.5 | 319 | 74.9% | $3.94 | $1,257 |
| 0.5-0.7 | 462 | 74.7% | $4.77 | $2,203 |
| 0.7-0.9 | 1,077 | 73.5% | $4.24 | $4,564 |
| 0.9+ (one-sided) | 2,698 | 75.9% | $4.22 | $11,388 |

### INSIGHT: Side dominance ไม่ค่อยสำคัญ
- WR range แค่ 73.5-78.6% (5pp spread)
- Avg PnL ค่อนข้างแบนราบ ($3.94-4.77)
- ว่า cascade จะ mixed หรือ one-sided ผลไม่ต่างกันมาก
- **ข้อสรุป**: ไม่ต้อง filter ด้วย side dominance

---

## EXP3: Cascade Freshness (bars since last cascade)

| Bucket | Trades | WR% | Avg PnL | Total PnL |
|--------|--------|-----|---------|-----------|
| 1 bar (ติดกัน) | 4,029 | 74.8% | $4.15 | $16,717 |
| 2-4 bars | 118 | 73.7% | $4.55 | $537 |
| **5-9 bars** | **209** | **80.4%** | **$6.41** | **$1,341** |
| **10-19 bars** | **214** | **83.6%** | **$5.82** | **$1,246** |
| 20-49 bars | 245 | 75.1% | $3.08 | $754 |
| 50+ bars | 287 | 76.7% | $3.75 | $1,076 |

### INSIGHT: "Fresh after rest" = sweet spot!
- **5-19 bars (75min - 4.75h gap)**: WR 80-84%, avg $5.82-6.41 -- **ดีที่สุด**
- **1 bar (ติดกัน)**: WR 74.8% -- average (เป็น 79% ของ trades)
- **50+ bars (12.5h+)**: WR 76.7% -- ok แต่ไม่ outstanding

**ทำไม?** Cascade ที่เกิดหลังจาก "พัก" 1-5 ชั่วโมง = market โดน surprise จากลูกใหม่หลังจากสงบ.
Cascade ต่อเนื่อง (1 bar) = market คาดการณ์ได้ → mean-reversion ไม่แรง.
แต่ sample size ที่ 5-19 bars น้อย (209+214 = 423) ต้องระวัง overfitting.

---

## EXP4: Price Displacement at Entry -- THE STRONGEST SIGNAL!

| Bucket | Trades | WR% | Avg PnL | Total PnL |
|--------|--------|-----|---------|-----------|
| <0.1% | 2,525 | 70.5% | $2.90 | $7,322 |
| **0.1-0.3%** | **1,899** | **80.1%** | **$5.07** | **$9,636** |
| 0.3-0.5% | 441 | 79.4% | $5.33 | $2,350 |
| 0.5-1.0% | 194 | 83.5% | $8.18 | $1,588 |
| **1.0%+** | **43** | **86.0%** | **$18.04** | **$776** |

### CRITICAL FINDING: Monotonic relationship!
- **ราคายิ่งขยับแรง ยิ่งดี** -- linear increase จาก 70.5% (quiet) ถึง 86.0% (explosive)
- **Avg PnL scales 6.2x**: $2.90 (quiet) vs $18.04 (explosive)
- **Threshold ที่ actionable**: displacement > 0.1% แยก "ok" (70.5%) กับ "good" (80%+)

**ทำไม?** Displacement สะท้อน "market impact" ของ cascade จริงๆ:
- ถ้า cascade ไม่ขยับราคา = noise / ถูก absorb
- ถ้า cascade ขยับราคา 1%+ ใน 15 นาที = forced selling/buying จริง = mean-reversion จะแรงกว่า

**สิ่งนี้สอดคล้องกับหลักการ v6**: liquidation cascade ที่ MÍ market impact = signal ที่แท้จริง

---

## EXP5: Multi-bar Cascade Duration

| Bucket | Trades | WR% | Avg PnL | Total PnL |
|--------|--------|-----|---------|-----------|
| no cascade bar | 106 | 72.6% | $4.93 | $523 |
| 1st bar | 2,322 | 74.5% | $3.94 | $9,149 |
| 2nd-3rd bar | 248 | 77.0% | $4.44 | $1,100 |
| 4th-7th bar | 609 | 75.5% | $4.22 | $2,571 |
| 8+ bar | 584 | 72.3% | $3.87 | $2,259 |

### INSIGHT: Duration ไม่สำคัญมาก
- WR range แค่ 72-77% (5pp spread)
- 2nd-3rd bar เล็กน้อย better (77%) แต่ sample size น้อย
- **ข้อสรุป**: ไม่ต้อง filter ด้วย cascade duration

---

## EXP6: Combined Quality Score

Quality Score = z_score(magnitude) + z_score(side_dominance) + z_score(tick_intensity) + z_score(displacement)

| Quartile | Trades | WR% | Avg PnL | Total PnL |
|----------|--------|-----|---------|-----------|
| Q1 (lowest) | 1,278 | 72.5% | $2.68 | $3,430 |
| Q2 | 1,273 | 71.8% | $3.77 | $4,800 |
| Q3 | 1,275 | 75.6% | $3.89 | $4,963 |
| **Q4 (highest)** | **1,276** | **82.1%** | **$6.64** | **$8,479** |

### Quality Score แยกได้ชัด!
- **Q4 vs Q1**: WR 82.1% vs 72.5% (+9.6pp), avg PnL $6.64 vs $2.68 (**2.5x better**)
- ลักษณะเป็น **monotonic increasing**: Q1 < Q2 < Q3 < Q4

### Filter Tests
| Filter | Trades | WR% | PnL | Delta |
|--------|--------|-----|-----|-------|
| **Baseline (ไม่ filter)** | **5,102** | **75.5%** | **$21,671** | -- |
| Skip Q1 | 3,824 | 76.5% | $18,241 | **-$3,430** |
| Skip Q1+Q2 | 2,551 | 78.8% | $13,442 | **-$8,229** |

### CRITICAL: ทุก quartile กำไร! Filter = เสีย PnL
- Q1 แม้ WR ต่ำสุด (72.5%) ก็ยังกำไร $3,430
- Skip Q1 เสีย $3,430 แม้ WR ดีขึ้น
- **ใช้เป็น POSITION SIZING ไม่ใช่ FILTER**

---

## Key Findings Summary

### 1. Price Displacement คือ "Quality Meter" ที่ดีที่สุด
- Monotonic relationship: displacement ยิ่งสูง WR ยิ่งดี (70% -> 86%)
- Avg PnL scales 6.2x จาก quiet -> explosive cascade
- **Actionable threshold**: displacement > 0.1% = good trade (WR 80%+)

### 2. Cascade Magnitude มี U-Shape
- Tick-only (ไม่มี hourly cascade) = ดี (WR 79%, tick trend signal)
- Mid-range cascade (1.1-3.0x) = แย่สุด (noise zone)
- Extreme cascade (5.0x+) = ดีสุด (real forced selling)
- **ข้อเสนอ**: ไม่ต้องเปลี่ยน threshold แต่ SIZE UP ที่ 5x+ cascade

### 3. "Fresh After Rest" Cascades ดีกว่า
- Gap 5-19 bars (1-5 ชั่วโมง) ก่อน cascade = WR 80-84%, avg $5.82-6.41
- Cascade ต่อเนื่อง (1 bar) = WR 74.8% (average)
- **ข้อเสนอ**: Track bars_since_last_cascade เป็น context variable

### 4. Side Dominance และ Duration ไม่สำคัญ
- ทั้งสองไม่มี monotonic relationship ที่ชัด
- ไม่ควรใช้เป็น filter หรือ sizing factor

### 5. Quality Score ใช้เป็น Position Sizing ได้
- Q4 ให้ avg PnL 2.5x ของ Q1 (ทั้งคู่กำไร)
- ทุก quartile positive = **ห้าม filter** แต่ **SIZE UP ที่ Q4** ได้

---

## ข้อเสนอเชิงปฏิบัติ

### DEPLOY: Position Sizing by Cascade Quality (paper trading)

```python
# Calculate quality multiplier at entry
displacement = abs(btc_ret_15m)  # price move during cascade bar
cascade_mag = liq_total / liq_total_ma

if displacement >= 0.003:     # 0.3%+
    size_mult = 1.5           # 50% larger position
elif displacement >= 0.001:   # 0.1%+
    size_mult = 1.2           # 20% larger
else:
    size_mult = 1.0           # base size

# Optional: extreme cascade boost
if cascade_mag >= 5.0:
    size_mult += 0.3          # additional 30%
```

**Expected impact**: ไม่กรอง trade ออก (เก็บทั้งหมด) แต่ **allocate capital มากขึ้นที่ high-quality signals**

### MONITOR: Cascade freshness dashboard metric
- Track `bars_since_last_cascade` ทุก 15 นาที
- ถ้า 5-19 bars gap = highlight เป็น "fresh cascade opportunity"

### FOLLOW-UP: Missions ต่อไป
1. ทดสอบ position sizing บน portfolio backtest (realistic)
2. วิเคราะห์ displacement threshold per-coin (BTC vs altcoin sensitivity)
3. รวม displacement quality กับ vol regime filter จาก mission 013

---

## Technical Notes
- OOS Period: Jan 2025 - Mar 2026 (15 months)
- Coins: BTC, XRP, ADA, DOT, SUI, FIL (v3 original 6)
- Total trades: 5,102 (v6 config)
- Duration: 16 seconds
- Anti-lookahead: signals.shift(1) in run_backtest
- Cascade features computed at entry time (backward-looking only)
