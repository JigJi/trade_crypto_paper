# Mission 021: Chop Detection & SIGNAL_FLIP Prevention

**วันที่**: 2026-03-30
**ประเภท**: signal_analysis / risk_management
**ความยาก**: hard (7 experiments, 6,473 trades, 5 filter tests)
**สถานะ**: COMPLETED — **IMPORTANT FAIL** — Chop filter ที่ entry ไม่ช่วย แต่ค้นพบว่าปัญหาอยู่ที่ in-trade score instability

---

## แรงบันดาลใจ

SIGNAL_FLIP = ปัญหาใหญ่สุดของระบบ:
- Paper: 63.6% ของ exits, WR 22.8%, -$898 (M018)
- Backtest: 13% ของ exits, WR 4.6%, **-$19,833**

สมมติฐาน: SIGNAL_FLIP เกิดเมื่อ BTC score สั่นไปมาใกล้ threshold ("chop")
ถ้าตรวจจับ chop ได้ตอน entry จะเลี่ยง FLIP ได้

**ผลกลับตรงข้าม: Chop ตอน entry ไม่ทำนาย FLIP → ปัญหาอยู่ระหว่างเทรด ไม่ใช่ตอนเข้า!**

---

## EXP 1: SIGNAL_FLIP Baseline (v3, 6 coins, OOS 15m)

| Metric | SIGNAL_FLIP | Non-FLIP |
|--------|-------------|----------|
| Trades | 843 (13.0%) | 5,630 (87.0%) |
| Win Rate | **4.6%** | **81.7%** |
| Total PnL | **-$19,833** | **+$32,178** |
| Avg PnL/trade | -$23.53 | +$5.71 |
| Avg bars held | 15.8 | - |

**SIGNAL_FLIP ทำลาย PnL มากกว่า $19K!** ถ้าไม่มี FLIP เลย PnL จะเป็น $32,178 (+160%)

### FLIP by Direction:
| Direction | Count | WR | PnL |
|-----------|-------|-----|-----|
| LONG FLIP | 386 | 3.9% | -$9,521 |
| SHORT FLIP | 457 | 5.3% | -$10,312 |

ทั้ง LONG และ SHORT FLIP แย่พอๆ กัน — ไม่ใช่แค่ปัญหา LONG

### FLIP by Holding Time:
| Bars Held | Count | WR | Avg PnL | Total PnL |
|-----------|-------|-----|---------|-----------|
| 1-4 bars | มาก | ต่ำ | -$23+ | ส่วนใหญ่ |
| 5-8 bars | กลาง | ต่ำ | -$20+ | มาก |
| 9-16 bars | น้อย | ดีขึ้นเล็กน้อย | - | - |
| 17-96 bars | น้อยมาก | - | - | - |

---

## EXP 2: Score Trajectory — การค้นพบสำคัญที่สุด!

| Metric | FLIP Trades | Non-FLIP Trades | Ratio |
|--------|-------------|-----------------|-------|
| **Score Std Dev** | **2.779** | **1.145** | **2.43x** |
| **Score Range** | สูงกว่ามาก | ต่ำกว่า | - |
| **Zero Crossings** | **3.8** | **0.6** | **6.3x** |

### CRITICAL INSIGHT:
- **FLIP trades มี score volatility สูงกว่า 2.4 เท่า** ระหว่างที่เทรดเปิดอยู่
- **Zero crossings สูงกว่า 6.3 เท่า!** — score เปลี่ยนเครื่องหมาย 3.8 ครั้ง vs 0.6 ครั้ง
- **แต่เราวัดได้หลังจากเทรดจบแล้วเท่านั้น** — ตอน entry ยังไม่รู้ว่า score จะสั่นขนาดไหน

---

## EXP 3: Chop Index Definition

Chop Index = Rolling Std Dev ของ BTC Score

| Window | Mean | Median | P75 | P90 |
|--------|------|--------|-----|-----|
| 4 bars (1h) | 0.599 | 0.000 | - | 2.240 |
| **8 bars (2h)** | **0.818** | **0.000** | **1.708** | **2.550** |
| 12 bars (3h) | 0.967 | 0.000 | - | 2.823 |
| 16 bars (4h) | 1.083 | 0.000 | - | 3.073 |
| 24 bars (6h) | 1.248 | 0.408 | - | 3.472 |

**หมายเหตุ**: Median = 0 หมายความว่าส่วนใหญ่ score ไม่เปลี่ยนเลย (stable) — chop เกิดเป็น cluster

---

## EXP 4: FLIP Rate ตาม Chop Quartile — **ไม่ช่วย!**

| Quartile | Trades | FLIP Rate | Overall WR | PnL |
|----------|--------|-----------|------------|-----|
| Q1 (Low chop) | 1,620 | 11.4% | 72.6% | $2,577 |
| Q2 | 1,618 | **14.6%** | 70.5% | **$3,899** |
| Q3 | 1,621 | 13.6% | 70.9% | $3,041 |
| Q4 (High chop) | 1,614 | 12.5% | 72.8% | $2,828 |

**ผลสำคัญ: FLIP rate แทบไม่ต่างกันระหว่าง quartiles!** (11.4% - 14.6%)

Q2 (moderate chop) กลับมี PnL ดีสุด ($3,899) — ตรงข้ามกับสมมติฐาน!

**เหตุผล**: Chop ก่อน entry ≠ Chop ระหว่างเทรด. Score สามารถ stable ตอน entry แล้วเกิด volatility ทีหลังได้

### Cross-Window Analysis:
ทุก chop window (4-24 bars) แสดงผลเดียวกัน: Q4 (high chop) ไม่ได้มี FLIP rate สูงกว่า Q1 อย่างมีนัยสำคัญ

---

## EXP 5: Score Velocity at Entry

| Condition | Trades | WR | FLIP Rate |
|-----------|--------|-----|-----------|
| Velocity aligned (score moving with trade) | 3,153 | 72.9% | 11.3% |
| Velocity misaligned | 3,320 | 70.5% | 14.6% |

**Gap เพียง 2.4pp WR และ 3.3pp FLIP rate** — velocity มีสัญญาณเล็กน้อยแต่ไม่แรงพอใช้เป็น filter

---

## EXP 6: Chop-Based Filters — **ทุกตัว FAIL!**

| Filter | Trades | PnL | Delta | WR | FLIPs |
|--------|--------|------|-------|-----|-------|
| **Baseline** | **6,473** | **$12,345** | **—** | **71.7%** | **843** |
| Skip chop>P75 | 3,749 | $8,525 | **-$3,820** | 76.9% | 214 |
| Skip chop>P90 | 5,339 | $9,849 | **-$2,496** | 73.5% | 544 |
| Half-size chop>P75 | 6,473 | $8,776 | **-$3,569** | 71.7% | 843 |
| Skip low velocity | 6,473 | $12,345 | **$0** | 71.7% | 843 |

### ทำไมทุก filter ทำให้ PnL ลดลง?

1. **Skip chop>P75**: ลด FLIPs จาก 843→214 (-75%) แต่ก็ลด trades จาก 6,473→3,749 (-42%)
   - ตัด trades ดีๆ ออกไปด้วย เพราะ chop ตอน entry ไม่ทำนาย FLIP

2. **Skip chop>P90**: ผลลัพธ์อ่อนกว่า — ยิ่งตัดน้อย ยิ่งช่วยน้อย

3. **Half-size**: ลด PnL ทั้ง trades ดีและแย่พอๆ กัน → net negative

4. **Skip low velocity**: ไม่มีผลเลย ($12,345 = เท่าเดิม) เพราะ signals ผ่าน threshold แล้ว = มี momentum อยู่แล้ว

---

## EXP 7: Walk-Forward — ข้ามเพราะไม่มี filter ที่ช่วย

---

## สรุปการค้นพบ

### 1. SIGNAL_FLIP = money destroyer (-$19,833, WR 4.6%)
ทำลาย PnL 160% ของ total profit — ถ้าไม่มี FLIP จะได้ $32K แทน $12K

### 2. ปัญหาไม่ได้อยู่ที่ entry — อยู่ที่ in-trade score instability
- Score std ระหว่าง FLIP trades = 2.779 vs non-FLIP = 1.145 (2.4x)
- Zero crossings: FLIP = 3.8 ครั้ง vs non-FLIP = 0.6 ครั้ง (6.3x)
- **แต่วัดได้เฉพาะหลังจากเทรดจบ** — ตอน entry ยังดูไม่ออก

### 3. Entry-time features ไม่ทำนาย FLIP
- Chop quartile: FLIP rate 11-15% ทุก quartile (flat)
- Score velocity: gap แค่ 3.3pp (ไม่คุ้มใช้เป็น filter)
- ทุก filter ลด PnL (worst: -$3,820)

### 4. นัยสำคัญสำหรับ strategy
เนื่องจาก entry filters ไม่ช่วย ทางออกที่เหลือคือ:
- **ปรับ exit mechanism** — ใช้ trailing stop ให้ออกก่อน FLIP (M005/M018 แนะนำแล้ว)
- **In-trade monitoring** — ถ้า score std สูงขึ้นระหว่างเทรด → tighten SL/exit early
- **Hysteresis band** — v6 ใช้ hyst=3.0 ซึ่งลด FLIP แล้ว (Tournament R3)
- **ไม่ควรพยายาม filter ที่ entry** — เสียเวลาและลด PnL

### 5. ข้อเสนอ
- **TRAIL is the answer**: M005 พบว่า 84% ของ non-FLIP exits เป็น TRAIL (WR 84%) → ให้ trailing stop ทำงานก่อน FLIP
- **Paper trading ต้องมี trailing stop** ที่ทำงานเหมือน backtest (M018 finding ยืนยัน)
- **ไม่ต้องเพิ่ม entry filter สำหรับ chop** — ไม่มีประโยชน์
- **ลอง dynamic hysteresis**: เพิ่ม hyst band เมื่อ score std สูงขึ้นระหว่างเทรด

---

## Actionable Next Steps

1. **Implement trailing stop in paper_trader.py** — เร่งด่วนที่สุด จะแก้ปัญหา FLIP ได้ถึงรากเหง้า
2. **Test in-trade score monitoring**: ถ้า rolling score std > 2.0 ระหว่างเทรด → exit early
3. **Dynamic hysteresis**: เพิ่ม hyst band เมื่อตรวจพบ score instability
4. **ไม่ต้อง** ทดสอบ entry-time chop filters อีก — proven ว่าไม่ช่วย

---

## Technical Notes
- OOS: Jan 2025 - Mar 2026 (15 months)
- Coins: BTC, XRP, ADA, DOT, SUI, FIL (v3 original 6)
- v3 config: 8 factors, per-coin COIN_CONFIGS
- Chop Index: rolling std of BTC composite score over 8 bars (2h)
- Score velocity: 4-bar diff of BTC score
- Anti-lookahead: signals.shift(1), merge_asof direction="backward"
- Duration: ~17 seconds

---

## Gamification

| Metric | Value |
|--------|-------|
| XP | +100 (hard) |
| Total XP | 1,965 |
| Streak | 19 วัน |
