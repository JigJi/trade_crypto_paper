# Mission 034: Factor Weight Robustness & Sensitivity Analysis

**วันที่**: 2026-04-12
**ประเภท**: factor_research / robustness / weight_optimization
**ความยาก**: hard (56 backtests, 8 factors x 7 multipliers, 7,114 trades baseline)
**สถานะ**: COMPLETED — **ROBUST + ACTIONABLE** — weights ไม่ fragile, แต่พบว่า liquidation under-weighted มาก

---

## แรงบันดาลใจ

Mega Discovery (Phase 1-4) ค้นพบ 8 factors และ optimal weights แต่ **ไม่เคยทดสอบว่า weights เหล่านั้น robust แค่ไหน**
- เปลี่ยน weight นิดเดียว PnL พังไหม? (= overfit)
- ถอด factor ออก 1 ตัว เสียหายเท่าไหร่?
- มี factor ไหนที่ "ยิ่งเพิ่มยิ่งดี"?

ต่อยอด: Mega Discovery, Mission 010 (factor attribution), Factor Registry

---

## วิธีการทดลอง

สำหรับ 8 production factors แต่ละตัว:
- ทดสอบ 7 multipliers: **0.0** (ถอดออก), **0.5**, **0.75**, **1.0** (baseline), **1.25**, **1.5**, **2.0**
- Backtest 6 coins (BTC, XRP, ADA, DOT, SUI, FIL) ช่วง OOS (Jan 2025 - Mar 2026)
- วัด PnL, WR, trade count, Sharpe
- **รวม 56 backtests** (8 x 7)

---

## Baseline

| Metric | Value |
|--------|-------|
| Trades | 7,114 |
| PnL | **$10,727** |
| WR | 70.6% |

---

## ผลการทดลอง — Factor-by-Factor

### 1. OI Divergence (weight 0.5) — NEARLY IRRELEVANT

| Multiplier | Weight | PnL | Delta |
|------------|--------|-----|-------|
| 0.0 | 0 | $10,421 | -$306 |
| 0.5-1.5 | 0.25-0.75 | $10,727 | $0 |
| 2.0 | 1.0 | $10,634 | -$93 |

- **Removal impact: -$306 (2.9%)** — แทบไม่มีผล
- Weight 0.25-0.75 ให้ผลเหมือนกันทุกประการ
- **Verdict: IRRELEVANT** — อาจถอดออกได้โดยไม่กระทบ

---

### 2. Funding Rate (weight 2.0) — MODERATE, SLIGHTLY OVERWEIGHTED

| Multiplier | Weight | PnL | Delta |
|------------|--------|-----|-------|
| 0.0 | 0 | $10,309 | -$418 |
| 0.5 | 1.0 | $10,626 | -$101 |
| 1.0 | 2.0 | $10,727 | $0 |
| **1.5** | **3.0** | **$10,742** | **+$15** |
| 2.0 | 4.0 | $10,639 | -$88 |

- **Removal impact: -$418 (3.9%)**
- Best at 1.5x (weight 3.0) แต่ improvement แค่ $15
- **Verdict: STABLE** — weight ปัจจุบันดีพอ

---

### 3. Whale Alerts (weight 1.5) — AT OPTIMUM, FRAGILE UPWARD

| Multiplier | Weight | PnL | Delta |
|------------|--------|-----|-------|
| 0.0 | 0 | $10,643 | -$84 |
| 0.5 | 0.75 | $10,726 | -$1 |
| **1.0** | **1.5** | **$10,727** | **$0** |
| 1.25 | 1.875 | $10,488 | -$239 |
| 2.0 | 3.0 | $9,975 | **-$752** |

- **Removal impact: -$84 (0.8%)** — แทบไม่มีผล!
- **แต่เพิ่ม weight = พังหนัก** — 2x ทำให้เสีย $752
- **Verdict: FRAGILE UPWARD** — อย่าเพิ่ม, อาจลดได้

---

### 4. ⭐ Liquidation (weight 2.0) — KING FACTOR, UNDER-WEIGHTED!

| Multiplier | Weight | PnL | Delta |
|------------|--------|-----|-------|
| 0.0 | 0 | $9,171 | **-$1,556** |
| 0.5 | 1.0 | $9,572 | -$1,155 |
| 0.75 | 1.5 | $10,387 | -$340 |
| 1.0 | 2.0 | $10,727 | $0 |
| 1.25 | 2.5 | $11,350 | **+$623** |
| 1.5 | 3.0 | $12,415 | **+$1,688** |
| **2.0** | **4.0** | **$13,191** | **+$2,464** |

- **Removal impact: -$1,556 (14.5%)** — factor ที่สำคัญที่สุด
- **PnL เพิ่มขึ้นทุก multiplier!** Monotonically increasing!
- ที่ 2x (weight 4.0): **+$2,464 (+23%)**, WR 71.2%
- ที่ 1.5x (weight 3.0): **+$1,688 (+15.7%)**, WR 71.2%
- **Verdict: MASSIVELY UNDER-WEIGHTED** — ควรเพิ่มจาก 2.0 เป็น 3.0-4.0

---

### 5. ETF Flows (weight 1.0) — AT OPTIMUM, IMPORTANT

| Multiplier | Weight | PnL | Delta |
|------------|--------|-----|-------|
| 0.0 | 0 | $9,717 | **-$1,010** |
| 0.5 | 0.5 | $10,003 | -$724 |
| **1.0** | **1.0** | **$10,727** | **$0** |
| 1.25 | 1.25 | $10,363 | -$364 |
| 2.0 | 2.0 | $10,350 | -$377 |

- **Removal impact: -$1,010 (9.4%)**
- Current weight 1.0 = optimal
- เพิ่มก็เสีย ลดก็เสีย
- **Verdict: PERFECTLY WEIGHTED**

---

### 6. OB Combined (weight 2.0) — AT OPTIMUM, IMPORTANT

| Multiplier | Weight | PnL | Delta |
|------------|--------|-----|-------|
| 0.0 | 0 | $9,376 | **-$1,351** |
| 0.5 | 1.0 | $10,399 | -$328 |
| **1.0** | **2.0** | **$10,727** | **$0** |
| 1.25 | 2.5 | $10,162 | -$565 |
| 2.0 | 4.0 | $10,086 | -$641 |

- **Removal impact: -$1,351 (12.6%)**
- Trade count drops to 5,369 at 0x (vs 7,114 baseline) — OB generates many entry signals
- **Verdict: PERFECTLY WEIGHTED**

---

### 7. Basis Contrarian (weight 1.5) — AT OPTIMUM, MODERATE

| Multiplier | Weight | PnL | Delta |
|------------|--------|-----|-------|
| 0.0 | 0 | $10,195 | -$532 |
| 0.75 | 1.125 | $10,703 | -$25 |
| **1.0** | **1.5** | **$10,727** | **$0** |
| 1.25 | 1.875 | $10,315 | -$413 |
| 2.0 | 3.0 | $9,921 | -$806 |

- **Removal impact: -$532 (5.0%)**
- Very sharp peak at current weight — 0.75x nearly same, 1.25x drops $413
- **Verdict: AT OPTIMUM, slightly fragile**

---

### 8. Tick Liq (weight 2.0) — SLIGHTLY UNDER-WEIGHTED

| Multiplier | Weight | PnL | Delta |
|------------|--------|-----|-------|
| 0.0 | 0 | $9,371 | **-$1,356** |
| 0.75 | 1.5 | $10,491 | -$236 |
| 1.0 | 2.0 | $10,727 | $0 |
| 1.25 | 2.5 | $10,844 | +$117 |
| **2.0** | **4.0** | **$10,994** | **+$267** |

- **Removal impact: -$1,356 (12.6%)**
- เพิ่ม weight ดีขึ้นเล็กน้อย ($267 at 2x)
- **Verdict: SLIGHTLY UNDER-WEIGHTED** — อาจเพิ่มเป็น 2.5-3.0

---

## Sensitivity Rankings

### By Removal Impact (สำคัญที่สุด → น้อยที่สุด):

| Rank | Factor | Removal $ | Removal % | Status |
|------|--------|-----------|-----------|--------|
| **1** | **liquidation** | **-$1,556** | **14.5%** | ⬆️ UNDER-WEIGHTED |
| 2 | tick_liq | -$1,356 | 12.6% | ⬆️ Slightly under |
| 3 | ob_combined | -$1,351 | 12.6% | ✅ At optimum |
| 4 | etf_flows | -$1,010 | 9.4% | ✅ At optimum |
| 5 | basis_contrarian | -$532 | 5.0% | ✅ At optimum |
| 6 | funding_rate | -$418 | 3.9% | ✅ Stable |
| 7 | oi_divergence | -$306 | 2.9% | ⚠️ Nearly irrelevant |
| 8 | whale_alerts | -$84 | 0.8% | ⚠️ Very weak |

### By Local Sensitivity (±25% around current):

| Factor | Local Range | Interpretation |
|--------|------------|----------------|
| liquidation | $963 | High (but upward = good!) |
| ob_combined | $565 | Moderate (peaked) |
| basis_contrarian | $413 | Moderate (peaked) |
| etf_flows | $385 | Moderate (peaked) |
| tick_liq | $353 | Moderate (upward trend) |
| whale_alerts | $239 | Low (fragile upward) |
| funding_rate | $115 | Low (stable) |
| oi_divergence | $0 | **Zero!** (insensitive) |

---

## Robustness Assessment

| Metric | Value |
|--------|-------|
| Avg removal impact | **7.7%** |
| Factors at optimum | **4/8** |
| Factors under-weighted | **2/8** (liquidation, tick_liq) |
| Factors over-weighted | **0/8** |
| Factors irrelevant | **2/8** (oi_divergence, whale_alerts) |
| **Overall Verdict** | **ROBUST** |

---

## การค้นพบสำคัญ

### 1. ⭐ Liquidation คือ King Factor — ยิ่งเพิ่มยิ่งดี
- เป็น factor เดียวที่ PnL เพิ่มขึ้น monotonically กับ weight
- ที่ weight 4.0 (2x current): **+$2,464 (+23%)** พร้อม WR 71.2%
- นี่สอดคล้องกับ mega discovery ที่ liquidation มี best_delta_pnl $10,837 สูงสุดในทุก factor
- **ข้อควรระวัง**: ต้อง validate กับ paper trading ก่อน — backtest อาจ overestimate

### 2. OI Divergence แทบไม่มีผล
- ถอดออกเสีย $306 (2.9%) เท่านั้น
- Weight 0.25-0.75 ให้ผลเหมือนกันทุกประการ
- **อาจถอดออกเพื่อลดความซับซ้อน** (simpler model = better, lesson #2)

### 3. Whale Alerts อ่อนแอมาก
- Removal impact แค่ $84 (0.8%)
- แต่เพิ่ม weight ทำให้ PnL ลดลงหนัก (-$752 at 2x)
- **ควรลดหรือถอดออก** — มี risk/reward ที่ไม่ดี

### 4. ETF, OB, Basis — Perfectly Weighted
- 3 factors นี้อยู่ที่ optimal แล้ว
- เปลี่ยนทิศทางใดก็ทำให้ PnL ลด
- **ไม่ต้องแตะต้อง**

### 5. Model ไม่ Fragile
- Average removal impact แค่ 7.7% — ถอด factor ใดก็ไม่พัง
- 4/8 factors อยู่ที่ optimum
- ไม่มี factor ที่ over-weighted
- **ระบบ robust พอสำหรับ production**

---

## Potential v3.1 Weights

จากผลการวิจัย ถ้าจะปรับ weights:

| Factor | Current | Proposed | Change | Expected Impact |
|--------|---------|----------|--------|----------------|
| liquidation | 2.0 | **3.0** | +1.0 | **+$1,688** |
| tick_liq | 2.0 | **2.5** | +0.5 | +$117 |
| ob_combined | 2.0 | 2.0 | 0 | — |
| etf_flows | 1.0 | 1.0 | 0 | — |
| basis_contrarian | 1.5 | 1.5 | 0 | — |
| funding_rate | 2.0 | 2.0 | 0 | — |
| whale_alerts | 1.5 | **0.75** | -0.75 | ~-$1 |
| oi_divergence | 0.5 | **0.25** | -0.25 | ~$0 |

**Expected combined impact: ~+$1,800 (+17%)**

⚠️ **ข้อควรระวัง**: นี่คือ backtest estimate — ต้อง validate ใน paper trading ก่อนใช้ live

---

## สรุป

**Weights ของ v3 ส่วนใหญ่ robust** — ระบบไม่ fragile, ไม่ overfit. 4/8 factors อยู่ที่ optimum แล้ว.

แต่มีโอกาสปรับปรุง: **liquidation under-weighted อย่างมาก** เพิ่ม weight จาก 2.0 เป็น 3.0 อาจเพิ่ม PnL +$1,688 (+15.7%). OI divergence และ whale alerts แทบไม่มีค่า อาจลดหรือถอดเพื่อทำ model ง่ายขึ้น.

---

## Tags
`factor_research`, `weight_sensitivity`, `robustness`, `liquidation`, `optimization`, `v3_weights`
