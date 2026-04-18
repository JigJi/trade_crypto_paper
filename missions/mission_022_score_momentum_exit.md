# Mission 022: In-Trade Score Momentum Exit — Score เสื่อมก่อน FLIP จริงหรือ?

**วันที่**: 2026-03-31
**ประเภท**: exit_mechanism / signal_analysis
**ความยาก**: hard (7 experiments, 5,130 trades, 15 parameter combinations, walk-forward)
**สถานะ**: COMPLETED — **CRITICAL FAIL** — Score-based exits ทำลาย PnL ทุกรูปแบบ แต่ค้นพบว่า FLIP เป็น sudden event ไม่ใช่ gradual decay

---

## แรงบันดาลใจ

ต่อยอดจาก Mission 021 ที่ค้นพบว่า:
- SIGNAL_FLIP trades มี score volatility สูง 2.4x
- Zero crossings สูง 6.3x
- Entry-time features ไม่ทำนาย FLIP

**สมมติฐาน**: ถ้า monitor BTC score ระหว่างเทรดแล้วออกเร็วเมื่อ score เสื่อมลง (ก่อน FLIP trigger) จะลด FLIP losses ได้

**ผลกลับตรงข้าม: Score ไม่เสื่อมก่อน FLIP! FLIP เป็น sudden event — score กระโดดจากฝั่งหนึ่งไปอีกฝั่งทันที**

---

## EXP 1: Baseline (v6 liq-only, 6 coins, OOS 15m)

| Metric | Value |
|--------|-------|
| Trades | 5,130 |
| Win Rate | 75.4% |
| Total PnL | $20,716 |
| SIGNAL_FLIP trades | 380 (7.4%) |
| FLIP PnL | **-$6,516** |
| FLIP WR | ~5% |

---

## EXP 2: Score Zero-Cross Exit — **ทำลายล้าง!**

ออกเมื่อ BTC score ข้ามศูนย์ (ก่อน full FLIP):

| Metric | Baseline | Zero-Cross | Delta |
|--------|----------|------------|-------|
| Trades | 5,130 | 5,717 | +587 (+11%) |
| PnL | $20,716 | **$1,482** | **-$19,234** |
| WR | 75.4% | 57.6% | -17.8pp |
| FLIPs | 380 | 0 | -380 |

**ทำไมเลวร้ายขนาดนี้?**
- Zero-cross ตัด FLIP ได้หมด (380→0) แต่ก็ตัดเทรดดีๆ ออกไปด้วย
- สร้าง 587 เทรดเพิ่ม (exit แล้ว re-enter) → churning
- PnL ลดลง $19,234 (-93%!)

---

## EXP 3: Score Decay Exit — **ทุกค่าเหมือน Zero-Cross**

ออกเมื่อ |score| ลดลงต่ำกว่า threshold × decay_pct:

| decay_pct | Threshold | PnL | Delta |
|-----------|-----------|-----|-------|
| 0.1 | 0.3 | $1,482 | -$19,234 |
| 0.2 | 0.6 | $1,482 | -$19,234 |
| 0.3 | 0.9 | $1,482 | -$19,234 |
| 0.5 | 1.5 | $1,482 | -$19,234 |
| 0.7 | 2.1 | $1,482 | -$19,234 |

**ทุกค่าให้ผลเหมือนกันเป๊ะ** → เพราะ score ลดลงต่ำกว่า threshold ทุกค่าในจังหวะเดียวกัน → exit timing เหมือนกันหมด

---

## EXP 4: Score Adverse Bars Exit

ออกเมื่อ score เคลื่อนสวนทิศทางเทรด N bars ติดต่อกัน:

| Adverse Bars | Trades | PnL | Delta | FLIPs |
|-------------|--------|-----|-------|-------|
| 2 | 5,140 | $20,196 | **-$520** | 350 |
| **3** | **5,132** | **$20,863** | **+$147** | 377 |
| 4 | 5,130 | $20,716 | $0 | 380 |
| 6 | 5,130 | $20,716 | $0 | 380 |
| 8 | 5,130 | $20,716 | $0 | 380 |

- adverse_3 ดีสุดแต่ +$147 เท่านั้น (+0.7%) = noise
- adverse_2 aggressive เกินไป (-$520)
- adverse ≥4 ไม่มีผลเลย (FLIP เกิดเร็วเกินกว่า 4 bars จะจับได้)

---

## EXP 5: Combined Approaches — **ทุก combo FAIL**

| Approach | PnL | Delta |
|----------|-----|-------|
| ZeroCross + Decay | $1,482 | -$19,234 |
| ZeroCross + Adverse | $1,482 | -$19,234 |
| Decay + Adverse | $1,482 | -$19,234 |
| All Three | $1,482 | -$19,234 |

ทุก combo ที่มี zero-cross หรือ decay → ถูก dominate โดย exit ที่เร็วที่สุด → ทำลายหมด

---

## EXP 6: Score Trajectory Analysis — **การค้นพบที่สำคัญที่สุด!**

### Score ตอน Entry vs Exit ตาม Exit Type:

| Exit Type | N | Entry |score| | Exit |score| | Decay Ratio | Avg Bars |
|-----------|---|---------------|--------------|-------------|----------|
| **TRAIL** | 4,721 | **8.10** | **6.06** | **0.75** | - |
| **SIGNAL_FLIP** | 380 | **7.64** | **7.79** | **1.02** | - |
| **SL** | 17 | 6.12 | 3.29 | 0.54 | - |

### CRITICAL INSIGHT:

**SIGNAL_FLIP trades มี score decay ratio = 1.02!**
- หมายความว่า **score ไม่ลดลงเลยก่อน FLIP** — กลับเพิ่มขึ้นเล็กน้อยด้วยซ้ำ!
- TRAIL trades: score ลดลงจาก 8.10 → 6.06 (decay 0.75) = ค่อยๆ อ่อนลงตามเวลา
- **FLIP = sudden jump event** — score อยู่ที่ 7.64 แล้วกระโดดข้ามฝั่งทันที

**นี่คือเหตุผลที่ score monitoring ไม่ช่วย: ไม่มี warning sign ก่อน FLIP!**

### EXP 6b: TRAIL trades ก็ข้ามศูนย์!

| Exit Type | % ที่ score ข้ามศูนย์ | จำนวน |
|-----------|----------------------|-------|
| TRAIL | **34.8%** | 1,642/4,721 |
| SIGNAL_FLIP | 100% | 380/380 |
| รวมทุก trade | 39.8% | - |

**35% ของ TRAIL trades (เทรดดีๆ!) มี score ข้ามศูนย์ระหว่างเทรด** → ถ้าใช้ zero-cross exit จะตัดเทรดดีๆ 1,642 ตัวออก!

### EXP 6c: Delayed Zero-Cross (min holding before exit)

| Min Hold | Trades | PnL | Delta | FLIPs |
|----------|--------|-----|-------|-------|
| 4 bars (1h) | 5,651 | $2,149 | -$18,567 | 65 |
| 8 bars (2h) | 5,458 | $4,403 | -$16,313 | 200 |
| 12 bars (3h) | 5,337 | $6,874 | -$13,842 | 261 |
| 16 bars (4h) | 5,277 | $10,415 | -$10,301 | 309 |
| 24 bars (6h) | 5,210 | $17,831 | -$2,885 | 347 |

**Pattern ชัดเจน**: ยิ่ง delay zero-cross exit ยิ่งใกล้ baseline → ยืนยันว่า early exit = destructive
แม้ delay 24 bars (6 ชม.) ยังขาดทุน $2,885 vs baseline

---

## EXP 7: Walk-Forward (3 periods)

Best approach: adverse_3 (+$147)

| Period | Baseline | Best (adverse_3) | Delta |
|--------|----------|-------------------|-------|
| 2025-01 to 2025-05 | $0 | $0 | $0 |
| 2025-06 to 2025-10 | $7,923 | $7,923 | $0 |
| 2025-11 to 2026-03 | $12,794 | $12,940 | **+$147** |

Consistent = True (ไม่ negative ในช่วงไหน) แต่ improvement อยู่ใน noise

---

## สรุปการค้นพบ

### 1. SIGNAL_FLIP เป็น Sudden Jump ไม่ใช่ Gradual Decay
- Score decay ratio ของ FLIP = **1.02** (score ไม่ลดเลย!)
- TRAIL trades มี decay ratio 0.75 (ค่อยๆ ลด)
- **FLIP เกิดจาก BTC score กระโดดข้ามฝั่งทันที ไม่มี warning**

### 2. Score-Based Exits ทำลาย PnL ทุกรูปแบบ
- Zero-cross: **-$19,234** (-93% ของ PnL!)
- Score decay: เหมือน zero-cross ทุกค่า
- Adverse bars: +$147 (+0.7%) = noise
- Combined: ถูก dominate โดย exit ที่เร็วที่สุด

### 3. Score ข้ามศูนย์เป็นเรื่องปกติของเทรดดี
- **35% ของ TRAIL trades** มี score ข้ามศูนย์ระหว่างเทรด
- ตัด score zero-cross = ตัดเทรดดี 1,642 ตัว

### 4. Price-Based Exit (TRAIL) เหนือกว่า Signal-Based Exit
- TRAIL จับ 92% ของ exits ด้วย WR 84%
- TRAIL ทำงานบน price momentum ไม่ใช่ signal score
- **Price ≠ Signal** — price สามารถทำกำไรแม้ score อ่อนลง

### 5. Hierarchy of Exit Effectiveness:
1. **TRAIL (price-based)** → ดีที่สุด, จับ profit runs
2. **TP (price-based)** → ดีมาก, target hit
3. **SL (price-based)** → จำเป็น, ป้องกัน catastrophic loss
4. **TIMEOUT** → neutral
5. **Score-based exits** → **DESTRUCTIVE** ❌
6. **SIGNAL_FLIP** → worst, but unpredictable

---

## ข้อเสนอ

### 1. **ห้ามเพิ่ม score-based exit** ในระบบ — proven destructive
### 2. **Trailing stop ใน paper_trading.py เป็นสิ่งเดียวที่จะแก้ FLIP problem**
- Backtest: 84% TRAIL, WR 84.4% (M018)
- Paper: 0% TRAIL, 64% FLIP, WR 22.8% (M018)
- Gap = trail implementation, ไม่ใช่ signal quality
### 3. **FLIP เป็น irreducible cost ที่เหลืออยู่** หลัง trail ทำงาน
- Backtest FLIP: 380 trades (-$6,516) = 7.4% ของ trades
- นี่คือ cost of doing business — ลดได้ด้วย hysteresis (v6) แต่ไม่ eliminate ได้
### 4. **Adaptive sizing (M019) เป็นทางออกที่ดีกว่า** — size up เมื่อมั่นใจ ไม่ใช่ exit เมื่อไม่มั่นใจ

---

## Actionable Next Steps

1. **IMPLEMENT TRAILING STOP IN PAPER TRADER** — เร่งด่วนที่สุด (3 missions ยืนยันแล้ว: M018, M021, M022)
2. **ไม่ต้อง** ทดสอบ score-based exit อีก — ปิดสาย research นี้
3. **Accept FLIP as cost** — focus on sizing (M019) ไม่ใช่ elimination
4. ต่อ research ด้าน sizing, coin selection, หรือ new data sources แทน

---

## Technical Notes
- OOS: Jan 2025 - Mar 2026 (15 months)
- Coins: BTC, XRP, ADA, DOT, SUI, FIL (v6 original 6)
- v6 config: liq-only, hysteresis=3.0, cd_extra=4
- Score exits tested: zero-cross, decay (5 levels), adverse (5 levels), delayed (5 levels), combos (4)
- Anti-lookahead: signals.shift(1), merge_asof direction="backward"
- Duration: ~15 seconds

---

## Gamification

| Metric | Value |
|--------|-------|
| XP | +100 (hard) |
| Total XP | 2,065 |
| Streak | 20 วัน |
