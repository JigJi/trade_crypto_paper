# Mission 016: Funding Regime x Cascade Quality -- FR ช่วยคัดกรอง Cascade ได้ไหม?

**วันที่**: 2026-03-25
**ประเภท**: cross_factor_analysis
**ความยาก**: hard (5 experiments, 5,293 trades, v6 liq-only)
**สถานะ**: COMPLETED -- **SIGNIFICANT แต่ไม่แนะนำให้ deploy**

---

## แรงบันดาลใจ

v6 เป็น liq-only architecture (ตัด 6 factors ออก รวมทั้ง funding_rate) แต่ funding rate ยังเป็นข้อมูลที่บอกความ "overcrowded" ของตลาดได้ดี:
- FR สูง (positive) = longs จ่าย shorts = longs overcrowded = เสี่ยงถูก liquidate ลง
- FR ต่ำ (negative) = shorts จ่าย longs = shorts overcrowded = เสี่ยงถูก squeeze ขึ้น

**สมมติฐาน**: ถ้า cascade เกิดตอน FR extreme (overcrowded) cascade นั้นควร "แท้" กว่า --> ผลเทรดดีกว่า

---

## Baseline (v6)

| Metric | Value |
|--------|-------|
| Trades | 5,293 |
| Win Rate | 75.5% |
| Total PnL | $22,882 |
| Avg PnL/trade | $4.32 |
| Coins | BTC, XRP, ADA, DOT, SUI, FIL |
| OOS | Jan 2025 - Mar 2026 |

---

## EXP1: FR ของเทรดชนะ vs เทรดแพ้

| Metric | Winners | Losers | t-stat | p-value | Sig? |
|--------|---------|--------|--------|---------|------|
| FR raw | 0.0000380 | 0.0000410 | -2.16 | 0.031 | * |
| **FR z-score** | **0.002** | **0.156** | **-3.91** | **0.0001** | **\*\*\*** |
| FR change | -0.000001 | 0.000001 | -1.31 | 0.190 | n.s. |

### INSIGHT:
**FR z-score ต่างกันอย่างมีนัยสำคัญ!** เทรดที่ชนะมี FR z-score ใกล้ 0 (funding ปกติ) ส่วนเทรดที่แพ้มี FR z-score สูงกว่า (funding เอียงไปทาง positive)

นี่ตรงกันข้ามกับสมมติฐาน -- **extreme funding ไม่ได้ช่วย แต่กลับทำร้ายเล็กน้อย**

---

## EXP2: Performance ตาม Funding Regime

| Regime | Trades | WR | PnL | Avg/Trade |
|--------|--------|------|------|-----------|
| **extreme_neg** (z<-1.5) | **396** | **79.5%** | **$2,239** | **$5.65** |
| neg (z -1.5 to -0.5) | 1,162 | 75.0% | $4,580 | $3.94 |
| neutral (z -0.5 to 0.5) | 1,829 | 76.9% | $8,822 | $4.82 |
| pos (z 0.5 to 1.5) | 1,606 | 74.0% | $6,079 | $3.79 |
| **extreme_pos** (z>1.5) | **300** | **71.7%** | **$1,162** | **$3.87** |

### INSIGHT:
**ไม่สมมาตร!** Extreme negative funding (shorts overcrowded) ให้ WR สูงสุด 79.5% แต่ extreme positive (longs overcrowded) ให้ WR ต่ำสุด 71.7%

ทำไม? เพราะ **SHORT > LONG** เป็น structural edge ของ strategy อยู่แล้ว -- เมื่อ shorts overcrowded (FR negative) cascade มักเป็น short squeeze (-> LONG signal) ซึ่งเป็น scenario ที่ strategy ทำได้ดีน้อยกว่า SHORT... **แต่** ความ overcrowded ของ shorts ทำให้ squeeze รุนแรงกว่า จึงชดเชย

---

## EXP3: Funding Alignment (FR direction match ทิศเทรด)

| Group | Trades | WR | Avg PnL |
|-------|--------|----|---------|
| Aligned (FR>0 & SHORT, FR<0 & LONG) | 2,947 | 75.9% | $4.40 |
| Unaligned | 2,346 | 75.0% | $4.23 |
| **t-test p-value** | | | **0.6414 (n.s.)** |

### INSIGHT:
**Alignment ไม่ significant.** ทิศของ funding rate ไม่ช่วยทำนายว่า cascade จะทำกำไรหรือไม่ เหตุผล: v6 cascade signal เป็น reactive (ตอบสนองต่อ liquidation ที่เกิดขึ้นแล้ว) ดังนั้น funding regime ก่อนหน้าไม่ค่อยสำคัญ

---

## EXP4: Funding-Based Trade Filters

| Filter | Trades | WR | PnL | Avg/Trade | vs Baseline |
|--------|--------|------|------|-----------|-------------|
| Baseline (no filter) | 5,293 | 75.5% | $22,882 | $4.32 | -- |
| \|FR_z\| > 0.5 | 3,464 | 74.7% | $14,060 | $4.06 | -$8,822 |
| \|FR_z\| > 1.0 | 1,746 | 73.6% | $7,008 | $4.01 | -$15,874 |
| \|FR_z\| > 1.5 | 696 | 76.1% | $3,401 | $4.89 | -$19,481 |
| **\|FR_z\| > 2.0** | **261** | **73.6%** | **$1,399** | **$5.36** | **-$21,483** |
| Aligned + \|z\|>0.5 | 1,815 | 74.7% | $7,446 | $4.10 | -$15,436 |
| Exclude neutral | 3,464 | 74.7% | $14,060 | $4.06 | -$8,822 |

### INSIGHT:
**Filter ทำ avg PnL/trade ดีขึ้นเล็กน้อย ($5.36 vs $4.32) แต่ลด total PnL อย่างรุนแรง!**

ปัญหาเดียวกับ lessons_learned #11: "WR and PnL can trade off -- confidence filters boost WR but reduce opportunity"

---

## EXP5: Funding Rate Momentum (Delta FR)

| Group | Trades | WR | Avg PnL |
|-------|--------|----|---------|
| FR rising | 2,290 | 74.7% | $4.71 |
| FR falling | 3,003 | 76.1% | $4.03 |
| Momentum aligned | 2,331 | 75.1% | $4.47 |
| Momentum unaligned | 2,962 | 75.8% | $4.21 |
| **Double aligned** (level + momentum) | **1,493** | **76.0%** | **$4.95** |

### INSIGHT:
Double alignment (FR direction + momentum ตรงกับทิศเทรด) ให้ avg ดีสุด $4.95 แต่ก็ไม่มากพอ (+$0.63 vs baseline) และเสียโอกาสจำนวนมาก

---

## สรุปและข้อเสนอ

### สิ่งที่ค้นพบ:
1. **FR z-score significant (p=0.0001)** -- winning trades มี FR ใกล้ 0 มากกว่า losing trades
2. **Extreme negative FR = best regime** -- WR 79.5%, avg $5.65/trade
3. **Extreme positive FR = worst regime** -- WR 71.7%, avg $3.87/trade
4. **FR alignment ไม่ significant** -- ทิศ FR ไม่ทำนายผลเทรด
5. **ทุก filter ลด total PnL** -- เพิ่ม quality ต่อเทรดแต่เสียจำนวนเทรด

### คำตัดสิน: **ไม่แนะนำให้ deploy**

ถึงจะ statistically significant แต่:
- avg PnL/trade เพิ่มแค่ ~$1 (+23%) ในขณะที่เสียเทรด 95% (5293 -> 261)
- Total PnL ลด $21K (-94%)
- ตรงกับ lessons_learned #14: "Simpler model = better model"
- ตรงกับ lessons_learned #17: "Market guards don't help in backtest"

### สิ่งที่ actionable:
- **Extreme negative FR regime เป็น "sweet spot"** -- ถ้าจะใช้ก็ใช้แค่เป็น confidence indicator (size up เวลา FR extreme neg) ไม่ใช่ filter
- **FR direction as sizing signal** อาจคุ้มกว่า filter: เพิ่ม size 1.2x เมื่อ FR < -1.5 z-score
- **ต่อยอดกับ cascade quality sizing (Mission 014)**: FR extreme neg + high cascade quality = double confidence -> max size

---

## Gamification

| Metric | Value |
|--------|-------|
| XP | +100 (hard) |
| Total XP | 1,465 |
| Streak | 14 วัน |
| Level | 4 (Professor) |
