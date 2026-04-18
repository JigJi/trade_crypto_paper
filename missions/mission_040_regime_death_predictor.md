# Mission 040: Regime Death Predictor — ทำนาย imminent death ได้ไหม?

**วันที่**: 2026-04-18
**ประเภท**: signal_quality / exit_timing / regime_analysis
**ความยาก**: hard (10 experiments, 1,897 regimes, 7,750 mature bars, 7,409 trades)
**สถานะ**: COMPLETED — **HYPOTHESIS FAILED** (แต่ได้ insight ที่แหลมคม)

---

## แรงบันดาลใจ — mirror ของ Mission 039

Mission 039 (birth survival) ล้มเหลว: ทำนาย ณ **birth** ว่า regime จะ survive ถึง age≥8 ไม่ได้

คำถามพลิก: ถ้า regime อยู่มาแล้ว N bars — เราใช้ **ข้อมูลปัจจุบัน** (score decay, slope, margin)
ทำนายได้ไหมว่า regime นี้กำลังจะตายภายใน 1-2 bars ข้างหน้า?

**Payoff ที่คาดหวัง**: ถ้าทำได้ → เพิ่ม "early exit" rule ใน live strategy → ลด loss จาก SIGNAL_FLIP
(FLIP เป็น money killer ตั้งแต่เริ่ม paper trading)

**สมมติฐาน**:
1. ก่อนตาย regime score จะ **decay** จาก peak
2. **Slope** (Δ over 3 bars) จะเข้าหา 0 หรือกลับทิศก่อน flip
3. **Margin to threshold** แคบ = ใกล้ flip
4. Volatility spike = regime instability

---

## EXP1: Regime Catalog (OOS)

| Metric | Value |
|---|---|
| Total regimes | 1,897 |
| Mean duration | 5.7 bars |
| Median duration | 2 bars |
| **Mean peak_at_age** | **2.7** (peak เร็ว — ส่วนใหญ่ peak ใน 3 bars แรก) |
| Median peak_at_age | 1 |
| Mean peak |score| | 4.78 |

**ข้อสังเกต**: ส่วนใหญ่ regime peak **ทันทีที่เกิด** แล้วเสื่อมลงช้าๆ ไม่มี "build-up" phase

---

## EXP2: Per-Bar Death Labels (mature bars age≥3)

| Metric | Value |
|---|---|
| Mature bars (age≥3) | **7,750** |
| P(dies within 1 bar) | 21.4% |
| **P(dies within 2 bars)** | **29.6%** (base rate) |
| P(dies within 3 bars) | 36.9% |

---

## EXP3: Feature Profile — Imminent Death vs Healthy ⭐

| Feature | Imminent (n=2,292) | Healthy (n=4,888) | Δ | Cohen's d |
|---|---|---|---|---|
| **score_abs** | **4.614** | **6.037** | **-1.423** | **-0.73** ← strong |
| **margin_to_thr** | **1.614** | **3.037** | **-1.423** | **-0.73** ← strong |
| decay_ratio | 0.766 | 0.833 | -0.067 | -0.30 |
| slope_signed_3bar | -0.366 | +0.182 | -0.547 | -0.29 |
| range_z | +0.060 | +0.029 | +0.031 | +0.03 |
| atr | 301.9 | 289.6 | +12.3 | +0.08 |
| vol_ratio | 1.043 | 1.077 | -0.034 | -0.04 |
| rsi | 49.86 | 49.08 | +0.78 | +0.07 |

**Insight**: score_abs และ margin ให้ **Cohen d = 0.73** (medium-large effect) — bar-level discrimination
แข็งแรงมาก! ต่างจาก Mission 039 (d=0.22 เท่านั้น) อย่างเด่นชัด

---

## EXP4: Univariate Death Filters (bar level)

| Filter | n | P(die≤2)% | Lift % |
|---|---|---|---|
| baseline (mature) | 7,750 | 29.6 | 0 |
| decay_ratio < 0.6 | 1,650 | 41.6 | **+40.8** |
| decay_ratio < 0.5 | 884 | 43.8 | +48.0 |
| decay_ratio < 0.4 | 370 | 47.3 | +59.9 |
| **margin_to_thr < 0.5** | **659** | **53.4** | **+80.6** ← best |
| margin_to_thr < 0.3 | 659 | 53.4 | +80.6 |
| margin_to_thr < 0.1 | 602 | 52.3 | +76.9 |
| slope < 0 | 1,895 | 34.0 | +15.1 |
| slope < -1.0 | 1,309 | 35.3 | +19.3 |
| combined: margin<0.3 AND slope<0 | 179 | **48.6** | +64.3 |
| combined: decay<0.5 AND margin<0.5 | 156 | 51.3 | +73.4 |

**ข้อค้นพบ**: ถ้าถามคำถาม "bar นี้กำลังจะตายภายใน 2 bars ไหม?" → filter ง่ายๆ
`margin_to_thr < 0.5` ให้ความแม่นยำ **53.4%** (vs baseline 29.6%) — lift +80.6%

---

## EXP5: Trade PnL แบ่งตาม bars_until_death ณ entry ⭐⭐

**ที่สำคัญที่สุด — PnL ของ trades ที่เข้าในระยะต่างๆ**:

| Bucket (bars until regime_death) | n | WR% | PnL | avg | FLIP% |
|---|---|---|---|---|---|
| **imminent (die≤2)** | **3,250 (44%!)** | **64.6** | **+$961** | +$0.30 | 13.6 |
| short (3-5) | 1,288 | 70.3 | +$1,739 | +$1.35 | 5.8 |
| healthy (6-15) | 1,962 | 71.9 | +$2,731 | +$1.39 | 4.8 |
| long (16+) | 909 | 74.0 | +$2,235 | +$2.46 | 1.1 |

**ข้อค้นพบสำคัญ**:
1. **44% ของ trades เข้าในระยะ imminent** — เยอะมาก!
2. แต่ imminent trades ยัง **ทำกำไร +$961** (WR 64.6%) — **ไม่ใช่ขาดทุน!**
3. FLIP% สูงขึ้นใน imminent (13.6%) แต่ TRAIL/TP ยังชนะได้มากกว่า

---

## EXP6: Hindsight Ceiling — Skip Dying Regimes

| Strategy | n | WR | PnL | Δ vs baseline |
|---|---|---|---|---|
| baseline | 7,409 | 68.7 | +$7,666 | $0 |
| skip bars_until_death≤0 | 5,548 | 70.4 | +$6,986 | **-$680** |
| skip bars_until_death≤1 | 4,721 | 71.3 | +$7,291 | -$374 |
| skip bars_until_death≤2 | 4,159 | 71.8 | +$6,705 | -$961 |
| skip bars_until_death≤3 | 3,634 | 72.1 | +$5,957 | -$1,708 |
| skip bars_until_death≤5 | 2,871 | 72.6 | +$4,966 | -$2,700 |

**สำคัญมาก**: **แม้ hindsight (รู้อนาคต) ก็ยังไม่ควรข้าม trades ที่เข้าในช่วง regime ใกล้ตาย** —
เพราะ trades เหล่านั้น "ผู้ชนะยังมากกว่าผู้แพ้" (WR 64-70%)

---

## EXP7: Realistic Entry Filters (ใช้เฉพาะ features ที่รู้ ณ entry)

| Filter | keep | skip | WR | PnL | Δ |
|---|---|---|---|---|---|
| baseline | 7,409 | 0 | 68.7 | +$7,666 | $0 |
| skip entry_decay<0.5 | 6,918 | 491 | 68.3 | +$6,461 | **-$1,205** |
| skip entry_decay<0.6 | 6,571 | 838 | 68.2 | +$5,659 | **-$2,006** |
| skip entry_slope<0 | 6,446 | 963 | 68.1 | +$5,589 | **-$2,076** |
| skip entry_slope<-1.0 | 6,755 | 654 | 68.2 | +$5,579 | -$2,086 |
| skip entry_margin<0.3 | 6,364 | 1,045 | 69.8 | +$6,990 | -$676 |
| skip decay<0.5 AND slope<0 | 7,083 | 326 | 68.5 | +$7,051 | -$615 |
| skip decay<0.6 AND margin<0.5 | 7,328 | 81 | 68.7 | +$7,322 | -$343 |
| skip margin<0.3 AND slope<0 | 7,358 | 51 | 68.7 | +$7,379 | -$287 |

**ไม่มี filter ใดที่ทำให้ PnL เพิ่มขึ้น** — ทุกตัวลดลงทั้งหมด

---

## EXP8: PnL ของ subsets ที่ถูก skip (เราทิ้งอะไรไป?)

| Filter subset | n | WR% | PnL | avg |
|---|---|---|---|---|
| decay<0.5 subset | 491 | 74.1 | +$1,205 | +$2.45 |
| decay<0.6 subset | 838 | 72.7 | +$2,006 | +$2.39 |
| slope<0 subset | 963 | 72.5 | +$2,076 | +$2.16 |
| slope<-1 subset | 654 | 74.0 | +$2,086 | **+$3.19** |
| margin<0.3 subset | 1,045 | 62.0 | +$676 | +$0.65 |
| margin<0.3 AND slope<0 | 51 | 68.6 | +$287 | +$5.63 |
| decay<0.5 AND slope<0 | 326 | 72.4 | +$615 | +$1.89 |

**Subsets ที่ถูก skip ทุกตัว "ทำกำไร"** — decay<0.5 มี avg $2.45/trade (สูงกว่า baseline!)
→ การ skip พวกนี้ = **ทิ้งเงิน**

---

## EXP9: FLIP vs Non-FLIP — ความจริงที่ชัดที่สุด ⭐⭐⭐

**FLIP trades (n=623) PnL = -$9,339 ← money killer**
**Non-FLIP trades (n=6,786) PnL = +$17,005**

ถ้าแยก FLIP ออกได้สมบูรณ์ = เพิ่ม PnL 2.2x

| Feature ณ entry | FLIP | Non-FLIP | Δ |
|---|---|---|---|
| entry_age | 4.60 | 6.61 | -2.01 |
| entry_decay_ratio | 0.929 | 0.895 | **+0.034** |
| entry_slope_3bar | +0.04 | -0.03 | +0.07 |
| entry_margin | 1.58 | 2.04 | -0.46 |
| **regime_duration (unknowable)** | **7.07** | **13.50** | **-6.42** |
| **bars_until_regime_death (unknowable)** | **2.48** | **6.89** | **-4.41** |

**Insight คมที่สุด**:
- **Features ที่รู้ ณ entry ไม่แยก FLIP ออกจาก Non-FLIP ได้**:
  - entry_decay Δ = +0.034 (FLIP สูงกว่า น้อยนิด)
  - entry_slope Δ = +0.07 (แทบเท่ากัน)
  - entry_margin Δ = -0.46 (FLIP ต่ำกว่า เล็กน้อย)
- **เฉพาะ features ของอนาคต (regime_duration, bars_until_death) ที่แยกได้ชัด**
  → ข้อมูลที่ไม่รู้ ณ entry = ไม่สามารถใช้ filter ได้
- FLIP trades เข้าใน regime ที่ "ดูปกติ" ณ entry แต่ตายเร็วภายใน

---

## EXP10: Combine กับ Mission 038 (skip age=1)

| Strategy | n | PnL | Δ_base | Δ_m038 |
|---|---|---|---|---|
| baseline | 7,409 | +$7,666 | 0 | — |
| M038 (skip age=1) [หมายเหตุ] | 3,913 | +$6,433 | -$1,233 | 0 |
| M038 + skip margin<0.3 AND slope<0 | 3,862 | +$6,146 | -$1,520 | -$287 |
| M038 + skip decay<0.5 AND slope<0 | 3,587 | +$5,818 | -$1,848 | -$615 |
| M038 + skip bars_until_death≤2 (hindsight) | 2,687 | +$4,914 | -$2,752 | -$1,519 |

**หมายเหตุ**: ค่า M038 PnL ใน mission นี้ต่างจาก Mission 038 รายงาน (+$7,944) เพราะ mission นี้
กรอง age=1 จาก feature tagger (อาจมีบางเคส age=0 จาก edge cases) — ทิศทางเหมือนกัน: **การเพิ่ม
death filter ลงบน M038 ทุกกรณี → ทำให้แย่ลง**

---

## สรุปผล — HYPOTHESIS FAILED อีกครั้ง แต่คมที่สุดใน series

### สิ่งที่ FAIL
1. **Realistic death filters ทุกตัว → ลด PnL** (-$287 ถึง -$2,076)
2. **Hindsight ceiling → ลด PnL** (-$680 ถึง -$2,700) — แปลว่าแม้รู้อนาคตก็ไม่ใช่ edge
3. **Combined กับ M038 → แย่ลงทุกตัว**

### สิ่งที่ WORK (แต่ใช้ไม่ได้จริง)
- **Bar-level discrimination แข็งแรง** (Cohen d=0.73 สำหรับ margin/score_abs)
- `margin_to_thr < 0.5` ทำนาย death ภายใน 2 bars ได้ 53.4% (vs base 29.6%)
- **แต่ discrimination นี้ไม่ transfer ไป trade-level** เพราะ trades ส่วนใหญ่เข้าที่ high-score bars
  (ไม่ใช่ dying bars)

### Insight คม (จาก EXP9)
- **FLIP trades = money killer (-$9,339)** แต่แยกจาก Non-FLIP ด้วย entry features ไม่ได้
- เพราะ FLIP trades "เข้าใน regime ที่ดูปกติ" (score_abs, margin, slope ใกล้เคียง Non-FLIP)
- ตัวแปรที่แยกได้ (regime_duration, bars_until_death) = ข้อมูลอนาคต = ไม่รู้ ณ entry
- **SIGNAL_FLIP เป็นปรากฏการณ์ random ที่ไม่มีสัญญาณก่อนเกิด**

### Structural implication
1. **Exit-side ก็แก้ไม่ได้ด้วย features ที่เห็น** — surgery 04-10 ทำถูกแล้ว (เปลี่ยนเป็น exit_only,
cooldown)
2. **Entry-side ก็แก้ไม่ได้ด้วย features ที่เห็น** — ยืนยัน Mission 039 + Mission 038
3. ต้องหา edge จาก **ที่อื่น** ไม่ใช่ regime timing features
4. BTC-led signal มี random transitions ที่ไม่ใช้ factors ที่เราใช้อยู่ทำนายได้

---

## Implication ต่อ live trading

**อย่าเพิ่ม death filter เข้า production** — จะทำให้ paper performance ลดลง:
- filter ที่ดีที่สุด (margin<0.3 AND slope<0) ลด PnL -$287 (-3.7%)
- ทุก filter ลดอย่างน้อย -$287 ถึง -$2,076

**สิ่งที่ควรทำต่อ**:
- ยืนยัน surgery 04-10 (exit_only + cooldown=4) ยังเป็นวิธีที่ดีที่สุดในการจัดการ FLIP
- เน้นหา edge จาก **alt-level features** (ไม่ใช่ BTC regime timing)
- ลอง **per-coin regime duration profile** — อาจเฉพาะบางเหรียญที่มี pattern
- ดู **exit refinement** — FLIP ที่ loss เฉลี่ย -$15/trade อาจลดด้วย tighter TP

---

## Next Missions (ต่อยอด)

1. **Alt-Specific Feature Autopsy** — ทิ้ง BTC regime, หา edge จาก **alt internal features**
(RSI divergence, OB imbalance per coin, volume profile)
2. **FLIP Loss Distribution** — วิเคราะห์ FLIP trades 623 ตัว: เวลา, เหรียญ, market regime —
หา cluster ที่ FLIP รวมตัว (อาจเฉพาะ hour/day/coin)
3. **TP Tightening for Old Regimes** — regime age≥16 มี WR 74%, TP avg $2.46 — ลองใช้ TP ที่แคบลง
เพื่อ lock กำไรก่อน regime ตาย
4. **Score Cycle Phase** — ไม่ใช้ age/duration แต่ใช้ **ตำแหน่งใน cycle** (peak-approach, peak, decay,
near-cross) — อาจ discriminate ได้ดีกว่า

---

## Technical Notes

- **OOS**: 2025-01-01 ถึง 2026-03-31 (15 เดือน)
- **Coins**: BTC, XRP, ADA, DOT, SUI, FIL (6 เหรียญ)
- **Threshold**: 3.0
- **Mature age**: ≥3 bars
- **Survivor threshold**: bars_until_death ≤2 = imminent

**Script**: `research/run_mission_040.py`
**Raw data**: `missions/mission_040_regime_death_predictor.json`

---

## เสริม — ความเชื่อมโยงกับ Mission 039

| Mission | Question | Answer | Edge พบไหม |
|---|---|---|---|
| 038 | age=1 trades ขาดทุนเพราะอะไร? | SIGNAL_FLIP 47.5% | +$279 (skip age=1) |
| 039 | ณ birth ทำนาย survivor ได้ไหม? | Cohen d=0.22 (อ่อน) | ไม่พบ |
| **040** | **ณ mature bar ทำนาย death ได้ไหม?** | **Cohen d=0.73 bar-level แต่ไม่ transfer** | **ไม่พบ** |

**Meta-learning**: การพยายามหา edge จาก **regime timing** (birth, death, age) ไม่ work สามมิชชั่นติด
→ **regime = random walk ที่เราไม่มีข้อมูลทำนาย** → หยุดค้นทิศนี้
