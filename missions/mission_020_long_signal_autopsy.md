# Mission 020: LONG Signal Autopsy — เมื่อไหร่ LONG ทำงาน เมื่อไหร่ตาย?

**วันที่**: 2026-03-29
**ประเภท**: signal_analysis / direction_bias
**ความยาก**: hard (9 experiments, 10,677 trades, v6 champion)
**สถานะ**: COMPLETED — **CRITICAL INSIGHT** — ปัญหา LONG อยู่ที่ execution ไม่ใช่ signal!

---

## แรงบันดาลใจ

Mission 018 พบว่า Paper LONG WR = 34.6% (แย่มาก) vs SHORT = 52.3% (gap 18pp)
สมมติฐาน: มีบาง condition ที่ทำให้ LONG ตาย → หา condition นั้น → filter/size down

**ผลกลับตรงข้าม: ใน backtest LONG กับ SHORT แทบไม่ต่าง!**

---

## EXP 1: LONG vs SHORT Baseline (v6 champion, 6 coins)

| Direction | Trades | WR | Total PnL | Avg PnL |
|-----------|--------|-----|-----------|---------|
| **LONG** | 4,961 | **69.7%** | **$7,995** | $1.61 |
| **SHORT** | 5,716 | **70.4%** | **$10,649** | $1.86 |
| Gap | - | **0.7pp** | $2,654 | $0.25 |

**Key Finding**: Gap เพียง 0.7pp ใน backtest! Paper gap 18pp → ปัญหาอยู่ที่ execution 100%

---

## EXP 2: LONG ตามชั่วโมง

| ช่วง | Best Hour | WR | Worst Hour | WR |
|------|-----------|-----|------------|-----|
| LONG | 15:00 UTC | **79.5%** | 10:00 UTC | **57.6%** |

**Spread: 22pp** — ชั่วโมงมีผลต่อ LONG มาก

ชั่วโมงที่ดี (WR > 75%): 15, 20, 21
ชั่วโมงที่แย่ (WR < 65%): 7, 8, 10, 11, 12

**Pattern**: Asian session morning (7-12 UTC) = LONG toxic zone

---

## EXP 3: LONG ตาม BTC Trend

| Condition | Trades | WR | PnL |
|-----------|--------|-----|-----|
| BTC > EMA200 (uptrend) | 2,973 | 70.5% | $5,311 |
| BTC < EMA200 (downtrend) | 1,988 | 68.5% | $2,684 |

**Gap เพียง 2pp** — BTC trend ไม่ใช่ปัจจัยสำคัญของ LONG quality

---

## EXP 4: LONG ตาม Volatility Regime

| Vol Regime | LONG WR | LONG PnL | SHORT WR | SHORT PnL |
|------------|---------|----------|----------|-----------|
| Low (<0.3) | **66.3%** | $1,756 | 63.0% | $1,178 |
| Normal (0.3-0.5) | 69.9% | $2,681 | 68.7% | $2,914 |
| High (0.5-0.7) | **74.3%** | $3,126 | **76.3%** | $4,753 |
| Extreme (>0.7) | **74.7%** | $432 | **79.2%** | $1,804 |

**Surprise!** LONG ดีขึ้นเมื่อ vol สูง (74.7% vs 66.3%) — ตรงข้ามกับสมมติฐาน!
เหตุผล: vol สูง → ATR กว้าง → SL ห่างขึ้น → ไม่โดน stop out ง่าย

---

## EXP 5: LONG ตาม v3 Score Magnitude

| Score Range | LONG Trades | LONG WR |
|-------------|-------------|---------|
| 0-2 | 876 | 68.7% |
| 2-4 | 742 | 73.9% |
| 4-6 | 406 | 68.7% |
| 6-8 | 215 | **76.3%** |
| **8+** | **205** | **80.5%** |

**LONG score 8+ → WR 80.5%!** Score magnitude ยิ่งสูง LONG ยิ่งดี (+12pp จาก baseline)

---

## EXP 6: Factor ที่ทำให้ LONG ชนะ/แพ้

| Factor | Winner Mean | Loser Mean | Delta (W-L) |
|--------|------------|------------|-------------|
| **ob_combined** | 0.296 | 0.096 | **+0.200** |
| tick_liq | 0.264 | 0.176 | +0.088 |
| liq_cascade | 2.990 | 3.021 | -0.031 |
| basis | -0.010 | 0.001 | -0.011 |

**ob_combined เป็น factor ที่แยก LONG win/loss ได้ดีที่สุด** (delta 0.200)
LONG ที่ OB support สูง → ชนะ, OB อ่อน → แพ้

---

## EXP 7: กฎกรอง LONG — ทุกกฎลด PnL!

| กฎ | Trades | PnL | Δ vs Mixed |
|----|--------|-----|------------|
| **Mixed (baseline)** | **10,677** | **$18,644** | **—** |
| SHORT-only | 5,716 | $10,649 | **-$7,995** |
| LONG เฉพาะ uptrend | 8,689 | $15,960 | -$2,684 |
| LONG เฉพาะ low/normal vol | 9,436 | $15,086 | -$3,558 |
| LONG เฉพาะ score≥5 | 6,463 | $13,433 | -$5,211 |
| LONG half-size | 10,677 | $14,647 | -$3,997 |
| Combined filter | - | $13,315 | -$5,328 |

**ผลสำคัญมาก: ทุกกฎกรอง LONG ทำให้ PnL ลดลง!**
- SHORT-only เสีย $7,995 (ลดลง 43%)
- ไม่มี filter rule ไหนที่ช่วย

**ใน backtest, LONG เป็นกำไร $7,995 → ไม่ควรตัดออก**

---

## EXP 8: Exit Reason (LONG vs SHORT)

| Exit Reason | LONG % | LONG WR | SHORT % | SHORT WR |
|-------------|--------|---------|---------|----------|
| TRAIL | 82.6% | 83.9% | 83.7% | 84.0% |
| SL | 12.7% | 0.0% | 11.7% | 0.0% |
| SIGNAL_FLIP | 4.7% | 9.1% | 4.5% | 2.7% |

Exit pattern เกือบเหมือนกันทั้ง LONG และ SHORT — ยืนยันว่า signal quality ไม่ต่าง

---

## EXP 9: LONG Per-Coin

| Coin | LONG WR | SHORT WR | Gap |
|------|---------|----------|-----|
| BTC | 60.6% | 61.3% | 0.7pp |
| XRP | 68.5% | 70.1% | 1.6pp |
| ADA | 71.2% | 73.1% | 1.9pp |
| DOT | 72.1% | 72.4% | 0.3pp |
| SUI | 72.3% | 72.3% | **0.0pp** |
| FIL | 73.2% | 73.0% | **-0.2pp** |

SUI/FIL: LONG = SHORT (ไม่มี bias)! BTC แย่สุดทั้งสองทาง

---

## สรุปการค้นพบ

### 1. ปัญหา LONG อยู่ที่ Paper Execution ไม่ใช่ Signal
- Backtest: LONG 69.7% vs SHORT 70.4% (gap 0.7pp)
- Paper: LONG 34.6% vs SHORT 52.3% (gap 18pp)
- **Gap 17.3pp เกิดจาก execution ล้วนๆ**

### 2. สาเหตุที่เป็นไปได้ของ Paper LONG ที่แย่
- **Market microstructure**: ซื้อ (LONG) ต้องจ่าย spread + slippage มากกว่าขาย (SHORT) ในตลาดที่มี downtrend bias
- **Paper เทรดเยอะกว่า backtest 2x** (Mission 018) → LONGs ที่เพิ่มมาอาจเป็น noise
- **Timing**: Paper enter ที่ open ของ candle ถัดไป vs backtest signal shift 1 bar

### 3. ไม่ควรตัด LONG ออกจาก backtest
ทุก filtering rule ลด PnL (worst case -$5,328) → LONG เป็นกำไรสุทธิ $7,995

### 4. ถ้าจะปรับปรุง LONG
- **Score-based sizing**: LONG score 8+ → WR 80.5% → size up
- **OB confirmation**: LONG ที่ ob_combined สูง → ชนะบ่อยกว่า
- **Hour filter**: เลี่ยง LONG ช่วง 7-12 UTC (Asian morning) อาจช่วยใน paper

### 5. ข้อเสนอ
- **ไม่ตัด LONG ออก** — profitable ใน backtest
- **ตรวจสอบ paper entry logic** — หาว่าทำไม paper เทรดเยอะกว่า backtest 2x
- **LONG half-size ใน paper** อาจเป็นทางสายกลาง (ลด exposure แต่ไม่ตัดทิ้ง)
- **Monitor per-hour LONG WR ใน paper** ต่ออีก 2 สัปดาห์

---

## Actionable Next Steps
1. **Debug paper entry**: เปรียบเทียบ entry condition ของ paper vs backtest อย่างละเอียด
2. **LONG score-based sizing**: size up เมื่อ v3_score ≥ 8 (WR 80.5%)
3. **OB confirmation**: ลอง require ob_combined > 0 สำหรับ LONG ใน paper
4. **Asian session LONG reduction**: ลด LONG size 50% ช่วง 7-12 UTC
