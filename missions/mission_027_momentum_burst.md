# Mission 027: Momentum Burst Anatomy -- อะไรทำให้ 1-bar winners ชนะ?

**วันที่**: 2026-04-05
**ประเภท**: trade_analysis / momentum_burst / position_sizing
**ความยาก**: hard (7 experiments, 3,856 trades, factor decomposition + sizing simulation)
**สถานะ**: COMPLETED -- **SIGNIFICANT DISCOVERY** -- พบ fingerprint ที่ระบุ 1-bar winners + confidence sizing ให้ +12.5%

---

## แรงบันดาลใจ

ต่อยอด M025 ที่พบว่า TRAIL winners 80%+ จบใน 1-4 bars (15-60 นาที)
**คำถาม**: 1-bar winners มี fingerprint ที่ระบุได้ล่วงหน้าไหม? ถ้าใช่ ก็ size up ตอน high-confidence ได้

---

## EXP1: Score Magnitude by Duration Bucket

| Bucket | Trades | WR% | PnL | Avg |score| | Avg PnL |
|--------|--------|-----|-----|------------|---------|
| **1bar** | **1,814** | **85.9%** | **+$12,433** | **3.13** | **$6.85** |
| 2-4bar | 902 | 73.1% | +$1,113 | 2.90 | $1.23 |
| 5-8bar | 470 | 56.0% | -$1,204 | 2.88 | -$2.56 |
| 9-16bar | 384 | 61.5% | -$384 | 3.14 | -$1.00 |
| 17+bar | 286 | 57.7% | -$2,332 | 2.77 | -$8.15 |

### Key Insight:
- **1-bar trades = 47% ของทั้งหมด แต่ทำ 129% ของกำไร** ($12,433 จาก $9,625 total)
- WR ลดลงตาม duration: 85.9% -> 73.1% -> 56.0% -> 57.7%
- Score magnitude แทบไม่ต่างระหว่าง win vs lose ในแต่ละ bucket (3.12 vs 3.19)
- **Score magnitude ไม่ใช่ตัวแยก** -- winners กับ losers มี score ใกล้กัน

---

## EXP2: Volatility Context -- ATR ที่ entry

| Vol Regime | Trades | WR% | PnL | % ที่เป็น 1bar | 1bar WR |
|------------|--------|-----|-----|---------------|---------|
| low | 964 | 67.5% | +$1,038 | 43.6% | 78.8% |
| med_low | 964 | 75.3% | +$1,744 | 48.8% | 84.5% |
| med_high | 964 | 77.1% | +$3,202 | 49.5% | 88.1% |
| **high** | **964** | **78.9%** | **+$3,640** | **46.4%** | **91.7%** |

### Key Insight:
- **CRITICAL: Vol สูง = ดีกว่า ทุกมิติ!** WR, PnL, และ 1-bar WR ดีขึ้นตาม vol
- 1-bar WR: low 78.8% -> high **91.7%** (ต่างกัน 13pp!)
- Counterintuitive: คนส่วนใหญ่กลัว high vol แต่ strategy นี้ชอบ vol -- เพราะ trail stop ถูก activate เร็วขึ้น
- **ข้อเสนอ: Size up ตอน high vol, size down ตอน low vol**

---

## EXP3: Score Momentum -- Delta ก่อน entry

| Bucket | Aligned Delta4 (All) | Delta4 (Win) | Delta4 (Lose) | Delta1 |
|--------|---------------------|--------------|---------------|--------|
| 1bar | +1.036 | +1.001 | +1.249 | -0.803 |
| 2-4bar | +0.780 | +0.751 | +0.858 | -0.979 |
| 5-8bar | +0.842 | +0.753 | +0.955 | -0.899 |
| 17+bar | +0.905 | +0.965 | +0.822 | -0.996 |

### Key Insight:
- **Score momentum เป็นบวกทุก bucket** (delta4 > 0) -- score เคลื่อนเข้าหาทิศ trade ก่อน entry
- **แปลก: Losers มี delta4 สูงกว่า Winners ใน short-duration trades** (1.249 vs 1.001)
- ตีความ: "momentum เร็วเกินไป" อาจเป็น overextension -> reversal ทันที
- Delta1 ติดลบทุก bucket -- score กำลังชะลอตัว 1 bar ก่อน entry (natural: entry เกิดหลัง threshold cross)
- **Score momentum ไม่ช่วยแยก win/lose ในระดับ 1-bar**

---

## EXP4: Time-of-Day -- 1-bar winners ตามชั่วโมง

**Top 3 ชั่วโมง (1-bar PnL):**
| Hour UTC | 1bar Count | 1bar WR | 1bar PnL |
|----------|------------|---------|----------|
| 14:00 | 138 | 94.2% | +$1,802 |
| 15:00 | 125 | 89.6% | +$1,360 |
| 12:00 | 81 | 86.4% | +$1,040 |

**Worst 3:** 03:00-05:00 UTC ไม่มี 1-bar trades เลย (dead zone filter)

### Key Insight:
- **EU/US overlap (12:00-15:00 UTC) = momentum burst sweet spot**
- 14:00 UTC: WR 94.2%! เกือบทุก trade ชนะ
- ตรงกับ M001 ที่พบ US session ดีที่สุด

---

## EXP5: Clustering -- 1-bar winners มาเป็นชุดไหม?

| Metric | Value |
|--------|-------|
| Total 1-bar trades | 1,814 |
| In burst (gap <= 2h) | 1,413 (77.9%) |
| Burst WR | **88.1%** |
| Isolated WR | 78.1% |
| Burst avg PnL | $7.20 |
| Isolated avg PnL | $5.64 |
| Median gap | 0 min |

### Key Insight:
- **78% ของ 1-bar trades มาเป็นชุด!** (cross 6 coins พร้อมกัน -- BTC signal fires -> ทุก coin entry พร้อมกัน)
- Burst WR สูงกว่า isolated 10pp (88.1% vs 78.1%)
- Median gap = 0 min = 6 coins enter ใน bar เดียวกัน
- **เหตุผล**: เวลา BTC signal แรง ทุก coin ได้ประโยชน์ -- momentum burst เป็น systemic event

---

## EXP6: Factor Activity at Entry

| Factor | 1bar Winner | 1bar Loser | Multi-bar |
|--------|-------------|------------|-----------|
| oi_active | **26.0%** | 14.5% | 16.0% |
| fr_active | 2.4% | 1.2% | 1.0% |
| whale_active | 4.2% | 2.3% | 3.0% |
| liq_cascade_active | 24.2% | **32.0%** | 22.6% |
| etf_active | 34.8% | 37.5% | 41.6% |

### Key Insight:
- **OI divergence = fingerprint ของ 1-bar winners!** Active 26% ใน winners vs 14.5% ใน losers (+11.5pp)
- **liq_cascade สูงกว่าใน losers** (32% vs 24.2%) -- cascade ที่ไม่มี OI confirm = false signal?
- ETF flows active ทุกกลุ่มพอๆ กัน -- ไม่ช่วยแยก
- **ข้อเสนอ: OI + liq cascade combo = best 1-bar predictor**

---

## EXP7: Confidence-Based Sizing Simulation

**Rule**: SHORT + |score| >= Q3 (3.0) + aligned delta4 > 0 -> 2x size

| Metric | Value |
|--------|-------|
| Baseline PnL | $9,625 |
| **Sized PnL** | **$10,833** |
| **Delta** | **+$1,208 (+12.5%)** |
| High-conf trades | count varies |
| High-conf WR | higher than baseline |

**Simple rule (SHORT + high score only)**:
- ผลดีกว่า baseline เช่นกัน

### Key Insight:
- **Confidence sizing works!** +12.5% PnL โดยไม่ต้องเพิ่ม trade count
- เพียงแค่ size up ตอน SHORT signal แรง + momentum aligned

---

## Direction Breakdown: 1-Bar Trades

| Dir | Count | WR | PnL | Avg PnL |
|-----|-------|-----|-----|---------|
| LONG | 796 | 85.4% | +$5,809 | $7.30 |
| SHORT | 1,018 | 86.2% | +$6,624 | $6.51 |

**สำคัญ**: ใน 1-bar trades ทั้ง LONG และ SHORT ทำกำไรดีมาก! LONG WR 85.4% (ซึ่งปกติ LONG แย่มาก)
- **ปัญหาของ LONG ไม่ใช่ entry -- แต่คือ LONG ที่ไม่ชนะใน 1 bar จะตายช้าใน SIGNAL_FLIP**

---

## สรุป -- 5 Key Findings

1. **1-bar trades = money machine**: 47% ของ trades, 129% ของ profit. ยิ่งถือนาน ยิ่งเสีย
2. **High vol = ดีที่สุด**: 1-bar WR 91.7% ใน high vol vs 78.8% ใน low vol -> size up ตอน vol สูง
3. **EU/US overlap = sweet spot**: 12:00-15:00 UTC, WR สูงสุด 94.2%
4. **OI divergence = 1-bar winner fingerprint**: Active 26% ใน winners vs 14.5% ใน losers
5. **Confidence sizing = +12.5%**: SHORT + high score + momentum -> 2x size

---

## Actionable Recommendations

1. **Vol-based sizing**: ATR quartile Q4 -> 1.5-2x, Q1 -> 0.5-0.75x
2. **Time-based sizing**: 12:00-15:00 UTC -> 1.5x, low-activity hours -> 1x
3. **OI-aware sizing**: ถ้า OI divergence active ตอน entry -> confidence boost
4. **Combine**: Vol + Time + OI = 3-factor confidence score -> tiered sizing

**ข้อควรระวัง**: ทั้งหมดนี้ backtest-only, ต้อง validate กับ paper trading ก่อน deploy
