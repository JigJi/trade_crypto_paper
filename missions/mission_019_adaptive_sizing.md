# Mission 019: Adaptive Position Sizing — Cascade Quality x FR Regime Combo

**วันที่**: 2026-03-28
**ประเภท**: position_sizing / risk_management
**ความยาก**: hard (6 experiments, 5,102 trades, 80 grid combos, walk-forward 3 periods)
**สถานะ**: COMPLETED — **SIGNIFICANT & STABLE** — Adaptive sizing เพิ่ม PnL +23% แบบ walk-forward consistent

---

## แรงบันดาลใจ

ต่อยอดจาก 2 missions สำคัญ:
- **Mission 014** (Cascade Quality): พบว่า displacement ≥0.1% ให้ WR 80%+ (vs 70.5% baseline), avg PnL scales 6.2x — แนะนำใช้เป็น SIZING signal
- **Mission 016** (FR Regime): พบว่า extreme negative FR ให้ WR 79.5%, avg $5.65/trade — แนะนำ SIZE UP ไม่ใช่ FILTER

**ทั้งสอง mission แนะนำเหมือนกัน: ใช้ quality signals ปรับขนาด position ไม่ใช่ตัด trades ออก — แต่ยังไม่เคยทดสอบจริง!**

สอดคล้องกับ Principle #13: "Position sizing is the correct risk lever, not timeframe or guards"

---

## Baseline (v6 liq-only, flat sizing)

| Metric | Value |
|--------|-------|
| Trades | 5,102 |
| Win Rate | 75.6% |
| Total PnL | $21,655 |
| Avg PnL/trade | $4.24 |
| Max Drawdown | -$508 |
| Sharpe | 57.35 |

### Feature Distribution:

| FR Regime | Trades | % |
|-----------|--------|---|
| neutral | 1,524 | 29.9% |
| pos | 1,334 | 26.1% |
| neg | 1,215 | 23.8% |
| extreme_neg | 549 | 10.8% |
| extreme_pos | 480 | 9.4% |

Displacement mean: 0.157%

---

## EXP1: Displacement-Based Position Sizing

| Scheme | Size Rules | PnL | Delta | DD | Sharpe |
|--------|-----------|-----|-------|-----|--------|
| **Baseline** | **Flat 1.0x** | **$21,655** | — | **-$508** | **57.35** |
| Conservative | disp≥0.1%→1.2x, ≥0.3%→1.5x | $26,025 | **+$4,370 (+20.2%)** | -$762 | 53.36 |
| Aggressive | disp≥0.1%→1.3x, ≥0.3%→1.7x, ≥1.0%→2.0x | $28,205 | +$6,550 (+30.2%) | -$863 | 51.83 |
| Extreme-only | disp≥0.5%→1.5x, ≥1.0%→2.0x | $23,254 | +$1,599 (+7.4%) | -$762 | 51.47 |

### INSIGHT:
**Displacement sizing ทำงานชัดเจน!** Conservative scheme เพิ่ม PnL +$4,370 (+20.2%) โดย drawdown เพิ่มแค่ $254 (50%). Aggressive ได้เยอะกว่า (+30.2%) แต่ risk/reward ratio แย่ลง.

**Extreme-only ได้น้อย** เพราะ trades ที่ disp≥0.5% มีแค่ ~237 trades (4.6%) — ไม่พอ leverage

---

## EXP2: FR Regime-Based Position Sizing

| Scheme | Size Rules | PnL | Delta | DD | Sharpe |
|--------|-----------|-----|-------|-----|--------|
| **Baseline** | **Flat 1.0x** | **$21,655** | — | **-$508** | **57.35** |
| ext_neg boost | FR_ext_neg +0.3x | $22,285 | +$630 (+2.9%) | -$508 | 57.31 |
| Both extremes | ext_neg +0.5x, ext_pos -0.3x | $22,178 | +$523 (+2.4%) | -$508 | 56.78 |
| Gradient | ext_neg +0.5, neg +0.2, pos -0.1, ext_pos -0.3 | $22,759 | +$1,104 (+5.1%) | -$508 | 56.81 |

### INSIGHT:
**FR sizing ให้ผลน้อยกว่า displacement มาก** — max delta แค่ +$1,104 (+5.1%) vs displacement +$4,370 (+20.2%)

ทำไม? เพราะ extreme FR regime มีแค่ ~10% ของ trades (549 trades) จึง impact น้อย. **FR ดีกว่าเป็น "bonus" ไม่ใช่ main sizing driver**

แต่ข้อดี: **drawdown ไม่เพิ่มเลย!** (-$508 เท่าเดิม) — FR sizing ปลอดภัยมาก

---

## EXP3: Combo Sizing (Displacement + FR)

| Scheme | PnL | Delta | Delta% | DD | Sharpe |
|--------|-----|-------|--------|-----|--------|
| **Baseline** | **$21,655** | — | — | **-$508** | **57.35** |
| **Combo Conservative** | **$26,655** | **+$5,000** | **+23.1%** | **-$762** | **53.51** |
| Combo Aggressive | $28,658 | +$7,003 | +32.3% | -$863 | 51.91 |
| Combo Full Gradient | $27,432 | +$5,777 | +26.7% | -$762 | 52.94 |

### INSIGHT:
**Combo Conservative คือ sweet spot!** — disp≥0.1%→1.2x, disp≥0.3%→1.5x, FR_ext_neg+0.3x

- PnL เพิ่ม +$5,000 (+23.1%)
- Drawdown เพิ่มแค่ $254 (50%) — ยอมรับได้
- Sharpe ลดเล็กน้อย (57.35→53.51) เพราะ variance เพิ่มตาม position size
- **FR contributes +$630 incremental** บน displacement base (+$4,370)

Aggressive ได้ +$7,003 (+32.3%) แต่ DD เพิ่ม 70% — risk/reward ไม่คุ้ม

---

## EXP4: Grid Search Optimal Multipliers (80 combos)

| # | disp_thr | disp_mult | FR_neg boost | PnL | DD | PnL/DD |
|---|----------|-----------|-------------|-----|-----|--------|
| 1 | 0.05% | 1.5x | +0.5x | $31,814 | -$762 | 41.8 |
| 2 | 0.05% | 1.5x | +0.3x | $31,394 | -$762 | 41.2 |
| 3 | 0.05% | 1.5x | +0.2x | $31,184 | -$762 | 40.9 |
| 4 | 0.05% | 1.5x | 0 | $30,765 | -$762 | 40.4 |
| 5 | 0.10% | 1.5x | +0.5x | $29,971 | -$762 | 39.3 |

### INSIGHT:
**Grid best = disp≥0.05% → 1.5x + FR_neg +0.5x → $31,814 (+47%!)**

แต่ disp_thr=0.05% หมายความว่า **เกือบทุก trade ได้ 1.5x** (mean displacement = 0.157%) — เท่ากับแค่เพิ่ม position size ทั้งหมด. **ไม่ใช่ adaptive sizing จริง = overfit!**

ถ้าเลือก threshold ที่ "meaningful" (≥0.1% ซึ่งแยก WR 70→80% จาก M014) → top5[5] = $29,971

**คำตัดสิน: ใช้ Conservative scheme (disp≥0.1%→1.2x, ≥0.3%→1.5x) ปลอดภัยกว่า grid-optimized**

---

## EXP5: Risk Analysis

| Strategy | PnL | Max DD | PnL/DD | MaxLossStreak | Tail 1% | Tail 5% |
|----------|------|--------|--------|---------------|---------|---------|
| **Baseline** | **$21,655** | **-$508** | **42.6** | **11** | **-$38.67** | **-$12.91** |
| Disp only | $26,025 | -$762 | 34.2 | 11 | -$44.05 | -$15.64 |
| FR only | $22,285 | -$508 | 43.9 | 11 | -$39.41 | -$13.01 |
| **Combo** | **$26,655** | **-$762** | **35.0** | **11** | **-$44.81** | **-$15.69** |

### INSIGHT:
- **Loss streak ไม่เปลี่ยน (11)** — sizing ไม่ทำให้แพ้ติดต่อกันนานขึ้น (ถูกต้อง เพราะ sizing ไม่เปลี่ยน entry/exit)
- **Tail risk เพิ่ม ~16%** (Tail 1%: -$38.67 → -$44.81) — proportional กับ sizing increase
- **PnL/DD ratio ลดลง** (42.6 → 35.0) — acceptable trade-off เพราะ absolute PnL เพิ่ม $5K
- **FR only มี risk-adjusted return ดีสุด** (PnL/DD = 43.9) — เพิ่ม PnL โดย DD ไม่เพิ่มเลย

---

## EXP6: Walk-Forward Stability (3 periods)

| Period | Dates | Trades | Baseline PnL | Combo PnL | Delta | Delta% |
|--------|-------|--------|-------------|-----------|-------|--------|
| P1 Early | Jun-Sep 2025 | 1,700 | $4,556 | $5,116 | +$561 | **+12.3%** |
| P2 Mid | Sep-Dec 2025 | 1,700 | $8,980 | $11,514 | +$2,535 | **+28.2%** |
| P3 Late | Dec 2025-Mar 2026 | 1,702 | $8,120 | $10,024 | +$1,905 | **+23.5%** |

### CRITICAL: ALL 3 PERIODS POSITIVE!

- **Walk-forward consistent** — combo sizing ชนะ baseline ในทุก period
- Improvement range: +12.3% ถึง +28.2% — ไม่มี period ที่ sizing ทำร้าย
- P1 (bear period, WR 72.5%) ได้น้อยสุด (+12.3%) — คาดได้ เพราะ displacement events น้อยกว่าใน bear market
- P2-P3 (bull periods, WR 77%) ได้เยอะกว่า (+23-28%) — cascade quality signals มี impact มากกว่าใน trending market

**ข้อสรุป: Adaptive sizing ไม่ใช่ overfit — มัน stable across time periods**

---

## สรุปและข้อเสนอ

### สิ่งที่ค้นพบ:
1. **Displacement-based sizing = main driver** (+$4,370, +20.2%) — cascade ที่ขยับราคาแรง = signal ดี = เพิ่มขนาดคุ้ม
2. **FR regime sizing = safe bonus** (+$630, +2.9%) — drawdown ไม่เพิ่มเลย แต่ impact น้อย
3. **Combo conservative = optimal** (+$5,000, +23.1%) — ได้ synergy เล็กน้อย
4. **Walk-forward ALL POSITIVE** — ทุก 3 periods combo ชนะ baseline
5. **Grid search shows danger** — ถ้า optimize aggressively (disp≥0.05%→1.5x) ได้ +47% แต่เป็น overfit

### คำตัดสิน: **แนะนำ deploy Combo Conservative บน paper trading**

```python
# Recommended sizing rules for paper_trader.py
if cascade_displacement >= 0.003:     # 0.3%+ displacement
    size_mult = 1.5
elif cascade_displacement >= 0.001:   # 0.1%+ displacement
    size_mult = 1.2
else:
    size_mult = 1.0

# FR bonus (optional, low impact but safe)
if fr_z_score < -1.5:                # extreme negative FR
    size_mult += 0.3

size_mult = min(size_mult, 2.0)      # cap at 2x
```

**Expected impact ใน paper trading:**
- PnL เพิ่มขึ้น ~20-25% (conservative estimate)
- Drawdown เพิ่ม ~50% (proportional) — ยอมรับได้
- ไม่มี trade ถูกตัดออก — **ทุก trade ยังถูกเทรด แค่ size ต่างกัน**

### Follow-up missions:
1. **Deploy sizing rules ใน paper_trader.py** — ทดสอบจริง 1 เดือน
2. **Per-coin displacement sensitivity** — BTC vs altcoin อาจต้องใช้ threshold ต่างกัน
3. **Dynamic sizing + streak dynamics (M017)** — ลดขนาดหลังแพ้ติดกัน 5+ ครั้ง

---

## Technical Notes
- OOS: Jan 2025 - Mar 2026 (15 months)
- Coins: BTC, XRP, ADA, DOT, SUI, FIL (v3 original 6)
- v6 config: cascade_mult=1.1, liq_w=8.0, tick_w=8.0, SL=25, TP=20
- FR z-score: rolling 96-bar (24h) mean/std
- Post-hoc sizing: pnl_sized = pnl_net * size_mult (valid because fees and slippage scale linearly)
- Duration: ~5 seconds
- Anti-lookahead: signals.shift(1), merge_asof direction="backward"

---

## Gamification

| Metric | Value |
|--------|-------|
| XP | +100 (hard) |
| Total XP | 1,765 |
| Streak | 17 วัน |
