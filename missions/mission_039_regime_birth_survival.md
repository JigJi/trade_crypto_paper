# Mission 039: Regime Birth Survival Predictor — ทำนายได้ไหมว่าสัญญาณเกิดใหม่จะอยู่ยาว?

**วันที่**: 2026-04-17
**ประเภท**: signal_quality / regime_analysis
**ความยาก**: hard (10 experiments, 1,897 regime births, 7,409 trades, 17 features)
**สถานะ**: COMPLETED — **HYPOTHESIS FAILED** (แต่ได้ insight ที่สำคัญ)

---

## แรงบันดาลใจ — ต่อยอดจาก Mission 038

Mission 038 ค้นพบว่า:
- **age=1 (สัญญาณสด)** = WR 47.9%, PnL **-$279** (ขาดทุน)
- **age≥8 (สัญญาณแก่)** = WR 73.1%, PnL **+$3,508** (กำไรจัด)
- Sweet spot อยู่ที่ age 2-8

**คำถามต่อยอด**: ที่จังหวะ regime เพิ่งเกิด (age=1) — เราสามารถดู **สภาพตลาด + factor composition**
ได้เลยไหมว่าสัญญาณนี้จะกลายเป็น "ผู้รอด" (survive ถึง age≥8) หรือ "ตายเร็ว" (<8 bars)?

ถ้าใช่ เราจะ **กู้** trades บางตัวที่ age=1 ให้ทำกำไรได้ แทนที่จะ skip ทิ้งทั้งหมดตาม Mission 038

**สมมติฐาน**:
1. Regime birth ที่ score_abs สูง → survive นานกว่า
2. Liquidation cascade ตอน birth = confirmation → survive
3. High volatility (range_z สูง) ตอน birth → regime มั่นคงกว่า
4. Basis contrarian extreme ตอน birth = edge ชัดกว่า

---

## EXP1: Regime Birth Catalog

| Metric | Value |
|--------|-------|
| Total regime births (OOS) | 1,897 |
| **Survivors (duration ≥ 8 bars)** | **454 (23.9%)** |
| Short-lived (< 8 bars) | 1,443 (76.1%) |
| Duration mean | 5.7 bars |
| Duration median | 2 bars |
| Duration p75 | 6 bars |
| Duration p90 | 14 bars |
| Duration max | 71 bars |

**ข้อสังเกต**: มีเพียง 1 ใน 4 ของ regime births ที่ survive ถึง sweet spot — ถ้าทำนายได้แม่นจะเป็น gold

---

## EXP2: Profile "Survivor vs Short-Lived" (17 features)

| Feature | Survivor mean | Short-lived mean | Δ | Cohen's d |
|---|---|---|---|---|
| **score_abs** | **4.466** | **4.184** | **+0.282** | **+0.22** ← best |
| liq_net_ma | -0.555 | -0.073 | -0.482 | -0.14 |
| score_tick | -0.084 | +0.008 | -0.091 | -0.11 |
| basis_z | -0.010 | +0.098 | -0.108 | -0.08 |
| score_basis | -0.007 | -0.060 | +0.053 | +0.06 |
| liq_net | -1.08M | -3.05M | +1.97M | +0.05 |
| atr | 305.9 | 297.9 | +7.98 | +0.05 |
| liq_total_ratio | 1.178 | 1.236 | -0.059 | -0.02 |
| range_z | +0.018 | +0.000 | +0.019 | +0.02 |
| อื่นๆ | ... | ... | ... | <0.05 |

**ผลการวิเคราะห์**:
- **ไม่มี discriminator ที่แข็งแรงเลย** — Cohen's d สูงสุดแค่ **0.22** (weak effect size)
- คู่มือการวิจัย: d<0.2 = negligible, 0.2-0.5 = small, 0.5-0.8 = medium, >0.8 = large
- **แทบทุก feature มี effect size อยู่ใน "negligible" range**
- แม้แต่ score_abs (discriminator ดีที่สุด) ก็ต่างกันแค่ 0.3 points — ระดับที่ซ้อนทับกันมาก

---

## EXP3: LONG vs SHORT Birth

| Direction | n | Survivors | Survival rate | Dur mean |
|---|---|---|---|---|
| LONG | 857 | 199 | 23.2% | 5.6 |
| SHORT | 1,040 | 255 | 24.5% | 5.7 |

**ไม่มีความต่างนัยยะ** — ทั้ง LONG และ SHORT births มี survival rate ใกล้เคียงกัน ~24%

---

## EXP4: Score Magnitude ที่ Birth vs Survival Rate

| \|score\| bucket | n | Survival % | Dur mean |
|---|---|---|---|
| 3.0-3.5 | 536 | **12.3%** | 3.5 |
| 3.5-4.0 | 102 | 18.6% | 4.2 |
| 4.0-5.0 | 819 | 29.4% | 6.8 |
| 5.0-6.0 | 226 | 27.9% | 6.0 |
| 6.0-8.0 | 159 | 31.4% | 7.7 |
| 8.0+ | 55 | 27.3% | 6.4 |

**Pattern**: score<3.5 มี survival ต่ำชัด (12%) แต่เมื่อ score ≥ 4 แล้ว survival **plateau ที่ ~29%** ไม่เพิ่มขึ้นอีก
→ score ≥ 4 ใช้เป็น threshold ได้ แต่การเพิ่มเป็น 5, 6, 8 **ไม่ช่วยเพิ่ม survival**

---

## EXP5: Simple Discriminator Filters at Birth

| Filter | n | Survival % | Lift (pp) |
|---|---|---|---|
| baseline (all births) | 1,897 | 23.9% | — |
| score_abs ≥ 3.5 | 1,361 | 28.5% | +4.6 |
| **score_abs ≥ 4.0** | **1,259** | **29.3%** | **+5.4** |
| score_abs ≥ 5.0 | 440 | 29.1% | +5.2 |
| high range_z (≥0.5) | 418 | 23.2% | -0.7 ← no help |
| cascade at birth | 307 | 25.1% | +1.2 |
| SHORT only | 1,040 | 24.5% | +0.6 |
| SHORT + score_abs ≥ 4.0 | 712 | 29.2% | +5.3 |
| SHORT + score_abs ≥ 4.0 + range_z ≥ 0.5 | 162 | **32.1%** | +8.2 |
| LONG + score_abs ≥ 4.0 | 547 | 29.4% | +5.5 |

**ข้อสรุป**: ลิฟท์เต็มที่แค่ +8.2pp (จาก 23.9% → 32.1%) และใช้ trades แค่ 162 ตัว
— **ไม่ใช่ edge ที่แข็งแรงพอจะเป็น filter หลัก**

---

## EXP6: Fresh Trades IN Survivor vs Short-Lived Regimes ⚠️ (พลิก!)

| กลุ่ม | n | WR | PnL | Avg |
|---|---|---|---|---|
| Fresh all (age=1) | 217 | 47.9% | -$279 | -$1.28 |
| **Fresh IN survivor regime** | **20** | **35.0%** | **-$58** | **-$2.91** |
| Fresh IN short-lived regime | 197 | 49.2% | -$221 | -$1.12 |

**ค้นพบพลิกคาด**: trades age=1 ที่อยู่ใน regime ที่จะ survive **กลับทำผลงานแย่กว่า** trades age=1 ใน regime ที่จะตายเร็ว!

**สาเหตุ (สมมติฐาน)**:
- Fresh trade ใน survivor regime = เข้าเร็วไปก่อน momentum build → โดน drawdown ก่อน TP
- Fresh trade ใน short-lived regime = TP hit เร็วก่อน signal จะ flip (SL/TP asymmetry)
- SHORT TP ถึงไว → exit แบบกำไรเบา ก่อน regime ตาย

น่าสนใจ: **ผู้รอด** ไม่ได้แปลว่า **ผู้ชนะ** ที่ entry point

---

## EXP7: Birth Score Magnitude Filter บน age=1

| Filter | n | WR | PnL |
|---|---|---|---|
| birth\|score\| ≥ 3.0 (all) | 217 | 47.9% | -$279 |
| birth\|score\| ≥ 3.5 | 137 | 49.6% | -$173 |
| birth\|score\| ≥ 4.0 | 113 | 48.7% | -$128 |
| birth\|score\| ≥ 5.0 | 33 | 51.5% | -$42 |
| **birth\|score\| ≥ 6.0** | **15** | **60.0%** | **+$4** |

score filter ช่วยลดขาดทุนได้เรื่อยๆ แต่ต้องถึง **|score|≥6** ถึงจะกำไร — และ **n=15 เล็กเกินไป**

---

## EXP8: Birth Range_Z Filter บน age=1

| Filter | n | WR | PnL |
|---|---|---|---|
| range_z ≥ -0.5 | 98 | 50.0% | -$156 |
| range_z ≥ 0.0 | 65 | 46.2% | -$201 |
| range_z ≥ 0.5 | 55 | 40.0% | -$220 |
| range_z ≥ 1.0 | 26 | 34.6% | -$152 |
| range_z ≥ 2.0 | 11 | **9.1%** | -$123 |

**ผลตรงข้ามความคาดหวัง**: volatility สูงตอน birth = trades **แย่ลง** ไม่ใช่ดีขึ้น
— เหมือน "noise spike" ช่วง high vol จะทำให้สัญญาณเพิ่งจุดติดก็ดับไปแบบรวดเร็ว

---

## EXP9: Combined Filters บน age=1

| Filter | n | WR | PnL |
|---|---|---|---|
| **SHORT + birth_score ≥ 4.0** | **53** | **60.4%** | **+$18** |
| SHORT + birth_score ≥ 5.0 | 13 | 69.2% | +$26 |
| LONG + birth_score ≥ 4.0 | 60 | 38.3% | -$147 |
| LONG + birth_score ≥ 5.0 | 20 | 40.0% | -$68 |
| hindsight: regime_survivor==True | 20 | 35.0% | -$58 |

**ค้นพบเล็กๆ**: SHORT age=1 + birth_score≥4 = เป็น "ผู้กู้" เดียวที่ทำกำไร (+$18)
แต่ LONG age=1 ไม่ช่วย — แม้กรองด้วย birth_score แรงแค่ไหนก็ขาดทุน

---

## EXP10: Portfolio Strategy Comparison

| Strategy | n | WR% | PnL | Δ vs baseline |
|---|---|---|---|---|
| baseline (ทั้งหมด) | 7,409 | 68.7 | $7,666 | $0 |
| **drop age=1 only (Mission 038)** | 7,192 | 69.1 | **$7,944** | **+$279** |
| drop age=1 except SHORT+birth≥4 | 7,245 | 69.2 | **$7,963** | **+$297** ← best |
| drop age=1 except birth_score≥6 | 7,207 | 69.3 | $7,949 | +$283 |
| drop age=1 except regime_survivor (hindsight) | 7,212 | 69.2 | $7,886 | +$221 |
| aggressive: drop age≤1 (age=0+age=1) | 5,548 | 70.4 | $6,986 | -$680 ← over-skip |

**ข้อค้นพบระดับ portfolio**:
1. **Best rule**: drop age=1 + คืนกลับ "SHORT+birth_score≥4" → +$297 เทียบกับ Mission 038 (+$279) = เพิ่มเพียง **+$18**
2. **Oracle ceiling** (ถ้ามี hindsight ว่า regime ไหนจะ survive) = **+$221 แย่กว่า** Mission 038 rule!
   - **พิสูจน์ว่า regime survival ≠ trade profitability** ที่ entry point
3. ไม่ควรลบ age=0 trades (จะเสีย -$680)

---

## สรุปผล — HYPOTHESIS FAILED แต่ได้ Insight ที่สำคัญ

### สิ่งที่ FAIL
1. **ไม่มี birth condition ที่เป็น discriminator ที่แท้จริง** — Cohen's d สูงสุดแค่ 0.22
2. **Score magnitude ที่ birth** ช่วยแยก short-lived (<3.5) ออกได้ แต่ไม่แยก survivor ได้
3. **Range_z สูงที่ birth ไม่ได้ช่วย** — กลับแย่ลงด้วยซ้ำ
4. **Liquidation cascade ที่ birth ไม่ได้ช่วย** — cascade_at_birth filter ได้ 25% survival (vs 24% baseline)
5. **Hindsight oracle แย่กว่า Mission 038 rule** — รู้ว่า regime จะ survive ก็ไม่ช่วยให้ age=1 ทำกำไร

### Insight ที่สำคัญ (counter-intuitive)
1. **Survivor regime ≠ winning trades at entry**: age=1 trades ใน survivor regime **ขาดทุนหนักกว่า** (WR 35% vs 49.2%)
   → Mission 038 rule ที่ skip age=1 ทิ้งหมดเป็นทางที่ดีที่สุดจริงๆ
2. **SHORT age=1 + birth_score≥4** เป็นซอกเล็กๆ ที่ทำกำไรได้ (+$18, WR 60.4%) แต่ lift ต่อ portfolio ไม่คุ้มความซับซ้อน
3. **BTC regime duration มีธรรมชาติเป็น random walk** — ไม่มีข้อมูล ณ birth ที่ทำนายได้แม่นพอ

### Implication
- Mission 038 recommendation "skip age=1" **ยังเป็น optimal solution** (+$279 / 3.6% portfolio lift)
- Regime birth condition **ไม่ใช่ predictive edge** → อย่าเสียเวลาพัฒนา filter เพิ่มจากตรงนี้
- ให้หา edge จากที่อื่นแทน (เช่น: regime death prediction, confluence timing, alt-specific birth)

---

## Next Missions (ต่อยอด)

1. **Regime Death Predictor** — ตรงข้ามกับ mission นี้: ทำนายว่า regime ที่ aged 30+ จะ die เมื่อไหร่ เพื่อ
exit ก่อนเกิด SIGNAL_FLIP
2. **Fresh SHORT Micro-Edge** — SHORT age=1 + birth_score≥4 (n=53, +$18). เจาะลึกว่ามี refinement เพิ่มได้ไหม (เช่น: per-coin, hour filter)?
3. **Alt-specific Regime** — ไม่ใช้ BTC global regime — สร้าง regime birth per alt (อาจมีสัญญาณต่างจาก BTC)
4. **Partial Entry Strategy** — แทนที่จะ binary skip/take age=1, ใช้ 0.3x size สำหรับ age=1 + scale up เมื่อ regime confirm

---

## Technical Notes

- **OOS**: 2025-01-01 ถึง 2026-03-31 (15 เดือน)
- **Coins**: BTC, XRP, ADA, DOT, SUI, FIL
- **Threshold**: 3.0 (default)
- **Survivor definition**: regime duration ≥ 8 bars (จาก Mission 038 sweet spot)
- **Baseline**: 7,409 trades, PnL $7,666, WR 68.7%

**Script**: `research/run_mission_039.py`
**Raw data**: `missions/mission_039_regime_birth_survival.json`
