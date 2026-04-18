# Mission 010: Factor Contribution Attribution
## วิเคราะห์ว่า Factor ไหนผลักดัน Winning vs Losing Trades

**วันที่**: 2026-03-19
**ระดับ**: Hard (100 XP)
**ผลลัพธ์**: SIGNIFICANT -- ค้นพบ pattern สำคัญหลายอย่าง

---

## สมมติฐาน
Individual factor contributions ที่ entry point ของ winning trades จะแตกต่างจาก losing trades อย่างมีนัย -- บาง factor อาจเป็น "noise" ที่ทำให้ signal ผิดพลาด

## ข้อมูลและวิธีการ
- **Backtest**: 6 core coins (BTC, XRP, ADA, DOT, SUI, FIL), OOS Jan 2025 - Mar 2026
- **v3 config**: ใช้ per-coin threshold, SL 2.5/TP 4.0 (original v3)
- **แยกคะแนน**: 8 factors ถูกคำนวณแยกกัน แล้ว map กลับไปหา entry time ของแต่ละเทรด
- **3,235 trades**: Winners 2,495 (77.1%), Losers 740 (22.9%)

---

## ผลการทดลอง

### EXP1: ค่าเฉลี่ย Factor Score (Winner vs Loser)

| Factor | Winner Mean | Loser Mean | Delta (W-L) | สรุป |
|--------|------------|------------|-------------|------|
| tick_liq | -0.150 | -0.219 | **+0.069** | Winner มี tick_liq score สูงกว่า |
| ob_combined | -0.231 | -0.268 | +0.037 | Winner มี OB score สูงกว่า |
| funding_rate | 0.033 | 0.011 | +0.022 | Winner มี funding active มากกว่า |
| liquidation | -0.095 | -0.116 | +0.021 | Winner มี liq score สูงกว่าเล็กน้อย |
| etf_flows | -0.088 | -0.022 | **-0.066** | Loser มี ETF score ดีกว่า (!!) |
| **TOTAL** | **-0.530** | **-0.597** | **+0.067** | W-L separation น้อยมาก |

**Key insight**: Delta ระหว่าง W กับ L น้อยมาก (0.067 จาก total range ~14) -- score magnitude ไม่ใช่ตัวชี้ขาด W/L

### EXP3: Dominant Factor Analysis (Factor ไหนดัง)

| Factor | จำนวนที่ Dominant | % | WR% | Total PnL |
|--------|-------------------|---|-----|-----------|
| **ob_combined** | 1,674 | **51.7%** | 77.2% | **$4,304** |
| **liquidation** | 722 | **22.3%** | **78.4%** | **$3,632** |
| tick_liq | 423 | 13.1% | 76.4% | $1,728 |
| basis_contrarian | 150 | 4.6% | 76.7% | $701 |
| oi_divergence | 92 | 2.8% | **67.4%** | **-$285** |
| whale_alerts | 40 | 1.2% | **92.5%** | $310 |
| funding_rate | 33 | 1.0% | **90.9%** | $382 |
| etf_flows | 0 | 0.0% | -- | $0 |

**CRITICAL findings:**
1. **ob_combined** เป็น "workhorse" -- fire บ่อยสุด (51.7%) แต่ WR ปานกลาง
2. **liquidation** เป็น "alpha generator" -- 78.4% WR เมื่อ dominant, สร้าง PnL $3,632
3. **oi_divergence** เป็น **"weak link"** -- เมื่อ dominant WR ตกเหลือ 67.4% และ PnL ติดลบ -$285!
4. **funding_rate + whale_alerts** มี WR สูงสุด (90%+) แต่ fire น้อยมาก
5. **etf_flows** ไม่เคย dominant -- เป็น supporting factor เท่านั้น

### EXP7: จำนวน Active Factors ที่ Entry

| # Factors Active | Trades | WR% | Avg PnL | Total PnL |
|------------------|--------|-----|---------|-----------|
| 0 | 66 | 74.2% | $5.50 | $363 |
| 1 | 929 | 76.0% | $1.32 | $1,226 |
| 2 | 1,393 | 75.7% | $2.88 | $4,013 |
| **3** | **635** | **80.8%** | **$5.90** | **$3,748** |
| **4** | **180** | **80.6%** | **$6.92** | **$1,246** |
| **5** | **32** | **87.5%** | **$4.99** | **$160** |

**CRITICAL finding**: ยิ่งหลาย factor เห็นด้วยพร้อมกัน WR ยิ่งสูง!
- 0-2 factors: WR 74-76% (ปกติ)
- **3+ factors: WR 80%+ ("confluence zone")**
- 5 factors: WR 87.5% (แต่มีแค่ 32 trades)

### EXP4: Factor Pair Interactions (best combos)

| Factor Pair | Count (same sign) | WR% | Avg PnL |
|-------------|-------------------|-----|---------|
| **funding_rate + ob_combined** | 29 | **93.1%** | $4.50 |
| **funding_rate + tick_liq** | 25 | **92.0%** | **$12.58** |
| oi_divergence + basis_contrarian | 73 | 87.7% | $6.28 |
| oi_divergence + etf_flows | 113 | 85.8% | $9.90 |
| oi_divergence + ob_combined | 278 | 82.4% | $7.62 |

**Best pair for PnL**: `funding_rate + tick_liq` = 92% WR, $12.58/trade
**Best pair for volume**: `etf_flows + ob_combined` = 80.3% WR, 503 trades

### EXP8: Factor Attribution แยกตาม Direction

**LONG** (1,345 trades, 77.0% WR):
- `liquidation` สำคัญสุด (delta W-L = +0.089) -- liq cascade ที่ชัดเจน = winning LONG
- `ob_combined` delta +0.076 -- order book bullish alignment ช่วย LONG

**SHORT** (1,890 trades, 77.2% WR):
- `etf_flows` สำคัญสุด (delta W-L = -0.091) -- ETF outflow ชัด = winning SHORT
- `tick_liq` delta +0.074 -- tick liq bearish = winning SHORT

**Different factors drive different directions!**

### EXP6: v3 vs v5 Score Separation

| | W Mean | L Mean | Delta (W-L) |
|---|--------|--------|-------------|
| v3 | -0.530 | -0.597 | 0.067 |
| **v5** | **-0.748** | **-0.880** | **0.132** |

**v5 doubles W-L separation** (0.132 vs 0.067) -- ลดน้ำหนัก noise factors + เพิ่ม liq weight ทำให้แยก W/L ได้ดีขึ้น 2x

---

## สรุปและข้อเสนอแนะ

### ค้นพบหลัก
1. **Score magnitude แยก W/L ได้น้อยมาก** (delta 0.067) -- ไม่ใช่ "score สูง = ชนะ" เสมอ
2. **Factor confluence เป็น predictor ที่แท้จริง**: 3+ factors active = WR 80%+ vs 1 factor = 76%
3. **oi_divergence เป็น weak link**: เมื่อ dominant (2.8% ของเทรด) ให้ WR แค่ 67.4% และขาดทุน -$285
4. **ob_combined เป็น workhorse แต่ไม่ใช่ star**: 51.7% dominant, WR average
5. **liquidation เป็น king**: WR 78.4% เมื่อ dominant, มี confluence สูง
6. **funding_rate + tick_liq = dream team**: 92% WR เมื่อ agree (แต่ rare)
7. **v5 weighting ถูกต้อง**: amplify liq = amplify best predictor, doubles W-L separation
8. **LONG vs SHORT ใช้ factor ต่างกัน**: LONG ชนะด้วย liq cascade, SHORT ชนะด้วย ETF outflow

### ข้อเสนอ (Actionable)
1. **Confidence filter ด้วย active factor count**: ถ้า >= 3 factors active, เพิ่ม position size หรือลด threshold
2. **พิจารณาลด oi_divergence weight**: จาก 0.5 เหลือ 0.25 หรือตัดออก -- เป็น factor เดียวที่ PnL negative เมื่อ dominant
3. **Direction-specific factor weighting**: LONG trades ควร weight liq + ob สูง, SHORT ควร weight etf + tick_liq สูง
4. **v5 validation ยืนยัน**: การเพิ่ม liq weight สอดคล้องกับ attribution analysis ทุกประการ

---

## Experiments: 8 | Duration: ~57s | Status: COMPLETED
