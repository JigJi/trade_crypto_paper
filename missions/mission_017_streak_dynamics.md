# Mission 017: Win/Loss Streak Dynamics & Serial Correlation

**วันที่**: 2026-03-26
**ประเภท**: risk_management / statistical_analysis
**ความยาก**: hard (7 experiments, 5,293 trades, v6 liq-only)
**สถานะ**: COMPLETED -- **HIGHLY SIGNIFICANT** -- ค้นพบ positive serial correlation ที่ actionable

---

## แรงบันดาลใจ

16 missions ที่ผ่านมาวิเคราะห์ **คุณภาพของ signal** (cascade quality, pre-cascade fingerprint, funding regime) แต่ยังไม่เคยดู **sequence dynamics** -- เทรดที่เรียงกันตามเวลามีความสัมพันธ์กันหรือไม่?

ถ้ามี "hot hand" (ชนะแล้วชนะต่อ) หรือ "cold streak" (แพ้แล้วแพ้ต่อ) → สามารถใช้ streak history ปรับ position size ได้

**3 สมมติฐาน:**
1. **Hot hand**: positive autocorrelation → ควรเพิ่มขนาดหลังชนะ
2. **Mean reversion**: negative autocorrelation → ควรเพิ่มขนาดหลังแพ้
3. **Random**: ไม่มี correlation → streak ไม่มีข้อมูล ปรับขนาดตาม streak = ไม่มีประโยชน์

---

## Baseline (v6 liq-only, 6 coins)

| Metric | Value |
|--------|-------|
| Trades | 5,293 |
| Win Rate | 75.5% |
| Total PnL | $22,882 |
| Avg PnL/trade | $4.32 |
| OOS | Jan 2025 - Mar 2026 |

---

## EXP1: Basic Streak Statistics

| Metric | Win Streaks | Loss Streaks |
|--------|-------------|--------------|
| จำนวน streaks | 816 | 815 |
| **Max streak** | **62** | **11** |
| Avg streak | 4.9 | 1.59 |
| Median | 3 | 1 |
| Expected (random) | 4.08 | 1.32 |

### Win Streak Distribution (top entries):

| ความยาว | จำนวนครั้ง | สัดส่วน |
|----------|-----------|---------|
| 1 | 222 | 27.2% |
| 2 | 142 | 17.4% |
| 3 | 87 | 10.7% |
| 4-5 | 130 | 15.9% |
| 6-10 | 141 | 17.3% |
| 11-20 | 95 | 11.6% |
| 21+ | 21 | 2.6% |

### Loss Streak Distribution:

| ความยาว | จำนวนครั้ง |
|----------|-----------|
| 1 | 557 (68.3%) |
| 2 | 154 (18.9%) |
| 3 | 49 (6.0%) |
| 4-5 | 35 (4.3%) |
| 6+ | 20 (2.5%) |

### INSIGHT:
- Win streaks **ยาวกว่า expected** อย่างชัดเจน (avg 4.9 vs expected 4.08, +20%)
- Loss streaks ก็ยาวกว่า expected (avg 1.59 vs 1.32, +20%)
- **Max win streak 62!** ชนะติดกัน 62 เทรด (ในระดับ portfolio)
- **Loss streaks สั้น**: 68% ของ loss runs จบใน 1 เทรด, 87% จบใน 2 เทรด

---

## EXP2: Autocorrelation Test -- **HIGHLY SIGNIFICANT**

### Runs Test (Wald-Wolfowitz)

| Metric | Value |
|--------|-------|
| จำนวน runs | 1,631 |
| Expected (random) | 1,959 |
| **z-stat** | **-12.201** |
| **p-value** | **0.0000** |
| Interpretation | **Positive correlation** |

Runs น้อยกว่า expected 17% → เทรดมี **positive clustering** (ชนะรวมกลุ่ม, แพ้รวมกลุ่ม)

### Lag-1 Autocorrelation

| Test | AC | z-stat | p-value | Significant? |
|------|-----|--------|---------|-------------|
| **Outcome (W/L)** | **+0.1676** | **12.195** | **0.0000** | **YES** |
| **PnL (continuous)** | **+0.3426** | **24.926** | **0.0000** | **YES** |

### Multi-Lag Autocorrelation:

| Lag | AC |
|-----|------|
| 1 | +0.1676 |
| 2 | +0.1308 |
| 3 | +0.1078 |
| 4 | +0.0876 |
| 5 | +0.0643 |
| 6 | +0.0335 |
| 7 | +0.0263 |
| 8 | +0.0061 |

### INSIGHT: **POSITIVE SERIAL CORRELATION CONFIRMED!**

**Hot hand hypothesis = TRUE** ในระดับ portfolio:
- ทั้ง 3 tests unanimous: runs test, outcome AC, PnL AC ทั้งหมด highly significant
- **PnL autocorrelation สูงกว่า outcome** (0.34 vs 0.17) → ขนาดกำไร/ขาดทุนก็ cluster ไม่ใช่แค่ W/L
- Autocorrelation ค่อยๆ สลายตัว (decay) จาก lag-1 ถึง lag-8 → "memory" ยาว ~8 เทรด (~2 ชม.)

**ทำไม?** เพราะ v6 ใช้ BTC composite signal ที่ shared กับทุกเหรียญ → เมื่อ BTC signal ถูก (cascade เกิดจริง) เหรียญทั้ง 6 ก็ชนะพร้อมกัน ในทางกลับกันเมื่อ signal ผิด ก็แพ้พร้อมกัน

---

## EXP3: Post-Streak Performance -- **ACTIONABLE**

| Bucket | Trades | WR% | Avg PnL | Total PnL |
|--------|--------|-----|---------|-----------|
| **หลัง 3+ wins** | **2,586** | **82.5%** | **$6.89** | **$17,825** |
| หลัง 2 wins | 593 | 76.2% | $4.77 | $2,826 |
| หลัง 1 win | 816 | 72.8% | $2.95 | $2,411 |
| หลัง 1 loss | 815 | 68.3% | $0.96 | $780 |
| หลัง 2 losses | 258 | 59.7% | $0.14 | $35 |
| **หลัง 3+ losses** | **224** | **46.4%** | **-$4.45** | **-$996** |

### INSIGHT: **36 percentage points WR spread!**

- **หลัง 3+ wins: WR 82.5%** → confident zone, ทุกเทรดมี positive expectancy สูง
- **หลัง 3+ losses: WR 46.4%** → danger zone, negative expectancy!
- t-test: t=9.760, **p=0.0000** → ต่างกันอย่าง overwhelming

**คำอธิบาย**: ไม่ใช่ "hot hand" ในแบบจิตวิทยา แต่เป็น **regime effect**:
- Win streak = BTC signal ถูก = market มี clean cascade → เหรียญทั้งหมดได้ประโยชน์
- Loss streak = BTC signal ผิด = market choppy/noisy → signal ทุกตัวแพ้

**นี่คือ information ที่ actionable มาก**: เมื่อแพ้ 3+ ครั้งติด ตลาดกำลังอยู่ใน "noise regime" → ควรลดขนาด

---

## EXP4: Per-Coin Autocorrelation

| Coin | Trades | WR% | Max Win | Max Loss | Runs z | Runs p | Lag-1 AC |
|------|--------|-----|---------|----------|--------|--------|----------|
| FIL | 883 | 81.2% | 27 | 4 | -0.396 | 0.692 | 0.013 |
| SUI | 872 | 78.8% | 25 | 5 | -0.659 | 0.510 | 0.020 |
| **DOT** | **894** | **77.9%** | **27** | **7** | **-3.427** | **0.001*** | **0.113** |
| ADA | 880 | 77.0% | 22 | 4 | -0.407 | 0.684 | 0.012 |
| XRP | 871 | 74.5% | 21 | 5 | -0.342 | 0.732 | 0.010 |
| BTC | 893 | 63.7% | 14 | 7 | -1.006 | 0.314 | 0.032 |

### INSIGHT: Serial correlation เป็น **cross-coin phenomenon**

- Per-coin: เกือบทุกเหรียญ **ไม่มี** significant autocorrelation (p > 0.3)
- ยกเว้น **DOT** (p=0.001) ที่มี mild clustering
- **Portfolio level**: AC = 0.17 (highly significant)

**สรุป**: Correlation เกิดจาก **BTC signal ที่ shared** ไม่ใช่จากเหรียญแต่ละตัว → เมื่อ BTC cascade เกิดขึ้น เหรียญ 6 ตัวเข้าเทรดพร้อมกัน (ภายใน 1-2 bars) → ชนะ/แพ้เป็นกลุ่ม

---

## EXP5: Time-Gap Analysis

| Gap | Trades | WR% | Avg PnL |
|-----|--------|-----|---------|
| < 1h | 658 | 78.0% | $5.00 |
| 1-4h | 622 | 75.9% | $5.17 |
| 4-12h | 209 | 79.9% | $3.69 |
| 12-24h | 122 | 62.3% | $1.17 |
| > 24h | 35 | 57.1% | $1.86 |

**Correlations**: Gap-Win = -0.095, Gap-PnL = -0.079

### INSIGHT:
- **เทรดที่เกิดใกล้กัน (< 4h) ดีกว่า** เทรดที่ห่างกัน
- Gap > 12h → WR ตก 13pp
- สอดคล้องกับ EXP2/EXP3: เทรดที่เกิดใกล้กัน = same cascade event = same regime = correlated

---

## EXP6: Drawdown Anatomy

| Metric | Value |
|--------|-------|
| Total DD episodes | 458 |
| **Max DD depth** | **-$504** |
| Max DD trades | 163 |
| Avg DD trades | 5.9 |

### Loss Run Impact:

| ความยาว | จำนวนครั้ง | Total Loss | Avg Loss/Run |
|----------|-----------|------------|-------------|
| 1 | 557 | -$2,400 | -$4 |
| 2 | 154 | -$1,682 | -$11 |
| 3 | 49 | -$916 | -$19 |
| 4 | 25 | -$903 | -$36 |
| 5 | 10 | -$731 | -$73 |
| 6 | 13 | -$1,085 | -$83 |
| 7 | 4 | -$455 | -$114 |
| 9 | 2 | -$106 | -$53 |
| 11 | 1 | -$77 | -$77 |

### INSIGHT:
- **Max DD เพียง $504** จาก PnL $22,882 = drawdown ratio 2.2% → ดีมาก
- Loss runs 1-2 เทรดรวมกัน = **-$4,082** (47% ของ total losses) → loss ส่วนใหญ่มาจาก isolated losses ไม่ใช่ deep streaks
- Loss runs 5+ = **-$2,454** (28% ของ total losses) → significant chunk แต่ไม่ใช่ส่วนใหญ่

---

## EXP7: Streak-Based Sizing Simulation -- **PROMISING**

| Strategy | Total PnL | vs Base | Max DD | Sharpe |
|----------|-----------|---------|--------|--------|
| **baseline_1x** | **$22,882** | **--** | **-$504** | **9.97** |
| martingale_after_loss | $22,402 | -$481 | -$504 | 9.20 |
| **anti_martingale (hot hand)** | **$31,795** | **+$8,912** | **-$636** | **10.69** |
| reduce_after_3_losses | $23,380 | +$498 | -$504 | 10.48 |
| **streak_proportional** | **$35,945** | **+$13,063** | **-$740** | **11.03** |
| conservative_dd | $26,735 | +$3,853 | -$557 | 10.70 |
| fr_neg_inspired | $29,277 | +$6,395 | -$583 | 10.75 |

### Strategy Descriptions:
- **martingale_after_loss**: 1.5x หลังแพ้ 2+ ครั้งติด → **แย่ลง** (loss streak = noise regime)
- **anti_martingale (hot hand)**: 1.5x หลังชนะ 3+ ครั้งติด → **+39% PnL, Sharpe ดีขึ้น**
- **streak_proportional**: size = 1.0 + streak × 0.1 (cap 0.5-2.0) → **+57% PnL, best Sharpe 11.03**
- **conservative_dd**: 0.7x หลังแพ้ 2+, 1.2x หลังชนะ 3+ → **+17% PnL, balanced**
- **reduce_after_3_losses**: 0.5x หลังแพ้ 3+ → +$498 only (loss regime สั้นเกิน)

### INSIGHT:
- **Martingale (เพิ่มขนาดหลังแพ้) = WRONG** → loss streak = signal กำลังผิด → ไม่ควรเพิ่ม
- **Anti-martingale (เพิ่มขนาดหลังชนะ) = RIGHT** → win streak = regime ดี → ควรเพิ่ม
- **streak_proportional ดีที่สุด** (+57% PnL, +11% Sharpe) แต่ Max DD เพิ่ม 47% (-$504 → -$740)
- **conservative_dd น่าสนใจที่สุดสำหรับ production**: +17% PnL, +7% Sharpe, DD เพิ่มแค่ 11%

### CAVEAT:
- นี่คือ **in-sample sizing optimization** ต้องระวัง overfit
- Streak-proportional ดีเพราะ "ride the wave" แต่จะเจ็บมากถ้า wave breaks
- ต้องทดสอบ out-of-sample (เช่น train on 2025, test on 2026) ก่อน deploy

---

## สรุป: 6 Insights หลัก

### 1. Serial Correlation มีจริง (p=0.0000, z=-12.2)
เทรดที่เรียงตามเวลามี positive autocorrelation -- ชนะแล้วมีแนวโน้มชนะต่อ, แพ้แล้วมีแนวโน้มแพ้ต่อ. Lag-1 AC = 0.17 (outcome) และ 0.34 (PnL). Memory ~8 เทรด.

### 2. Correlation เป็น Cross-Coin Phenomenon (ไม่ใช่ Per-Coin)
Per-coin autocorrelation แทบไม่มี (5/6 coins p > 0.3). Correlation เกิดจาก **BTC signal ที่ shared** → cascade event ทำให้เหรียญทั้ง 6 ชนะ/แพ้พร้อมกัน.

### 3. Post-Streak WR Spread = 36pp (82.5% vs 46.4%)
หลัง 3+ wins: WR 82.5%, avg $6.89. หลัง 3+ losses: WR 46.4%, avg -$4.45. **36 percentage points!** นี่คือ regime information ที่แรงมาก.

### 4. Anti-Martingale > Martingale
เพิ่มขนาดหลังชนะ (+39% PnL) ถูกต้อง. เพิ่มขนาดหลังแพ้ (-2% PnL) ผิด. สอดคล้องกับ logic: win streak = signal ถูก = regime ดี.

### 5. Drawdown ตื้นมาก (Max -$504, ratio 2.2%)
Loss runs ส่วนใหญ่จบใน 1-2 เทรด. Max loss streak 11 เกิดแค่ 1 ครั้ง. Strategy มี structural resilience สูง.

### 6. Time Proximity = Quality Signal
เทรดที่เกิดภายใน 4 ชม. (WR 77%) ดีกว่าเทรดที่ห่าง > 12 ชม. (WR 61%). Trade clustering = same cascade event = high quality.

---

## คำตัดสิน: SIGNIFICANT & ACTIONABLE (ด้วยความระวัง)

### ทำได้ทันที (low risk):
- **Reduce size หลังแพ้ 3+ ครั้งติด** (0.7x) -- เพียง +2% PnL แต่ลด risk ในช่วง noise regime
- นี่คือ **risk management** ไม่ใช่ alpha seeking → safe to implement

### ต้องทดสอบเพิ่ม (medium risk):
- **Anti-martingale sizing** (1.2-1.5x หลังชนะ 3+ ติด) -- promising (+39% PnL) แต่ต้อง OOS validation
- **Conservative_dd** (combined: reduce after loss + increase after win) -- +17% PnL, balanced risk

### ไม่ควรทำ (high risk):
- **Streak-proportional** (continuous sizing) -- +57% PnL แต่เสี่ยง overfit สูง, DD เพิ่ม 47%
- **Martingale** (เพิ่มหลังแพ้) -- data ชัดเจนว่าผิด

---

## Action Items

1. **Implement loss streak detector ใน paper trading** -- ถ้าแพ้ 3+ ติดใน portfolio level → log warning + reduce next size 0.7x
2. **Design OOS test for anti-martingale** -- train sizing rules on 2025H1, validate on 2025H2-2026
3. **Monitor per-coin vs portfolio correlation** ใน paper trading -- ยืนยันว่า cross-coin clustering เกิดจริงใน live
4. **Future mission**: ทดสอบ "regime-based sizing" -- ไม่ได้ใช้ streak โดยตรง แต่ใช้ indicator ของ regime (เช่น recent BTC volatility, cascade frequency) มาปรับขนาด

---

## ความสัมพันธ์กับ Missions เก่า

| Mission | Connection |
|---------|-----------|
| #003 (Signal Strength) | Signal magnitude → streak probability? High score = longer win streaks? |
| #006 (Concentration Risk) | Cross-coin clustering อธิบาย concentration risk ที่พบ |
| #012 (Vol Regime) | Vol regime อาจเป็น underlying cause ของ streak clustering |
| #014 (Cascade Quality) | High quality cascade → longer win streak? |
| #015 (Pre-Cascade Fingerprint) | BB bandwidth (winner predictor) อาจ correlate กับ streak position |

---

## Tags
`risk_management`, `serial_correlation`, `streak`, `position_sizing`, `drawdown`, `autocorrelation`, `runs_test`, `hot_hand`, `anti_martingale`, `regime_effect`

---

## Gamification

| Metric | Value |
|--------|-------|
| XP | +100 (hard) |
| Total XP | 1,565 |
| Streak | 15 วัน |
| Level | 4 (Professor) |
