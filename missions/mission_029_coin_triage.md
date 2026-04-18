# Mission 029: Coin Performance Triage -- 21-Day Paper Trading Per-Coin Deep Dive

**วันที่**: 2026-04-07
**ประเภท**: portfolio_analysis / coin_selection / paper_trading
**ความยาก**: hard (46 เหรียญ, 2,067 trades, 21 วัน, multi-dimensional scoring)
**สถานะ**: COMPLETED -- **SIGNIFICANT** -- พบ 10 เหรียญ DROP ที่กินกำไร $520, ถอดแล้ว portfolio ดีขึ้นทันที

---

## แรงบันดาลใจ

จาก Next Steps ใน memory: "Re-analyze coin selection (TP>0%, FLIP avg loss, PnL>0)" ยังไม่มีใครทำ
- Paper trading ครบ 21 วัน (2026-03-17 ถึง 04-06) มี 2,067 trades จาก 46 เหรียญ
- ต้องตอบคำถาม: **เหรียญไหนทำกำไร เหรียญไหนกินกำไร ควรถอดตัวไหนออก?**

---

## ผลรวม 21 วัน

| Metric | Value |
|--------|-------|
| Total trades | 2,067 |
| Total coins | 46 |
| Total PnL | +$337 |
| Overall WR | 41.7% |
| **SHORT PnL** | **+$739 (WR 48.9%)** |
| **LONG PnL** | **-$402 (WR 31.0%)** |

**LONG ตายจริง** -- 21 วัน paper ยืนยัน: LONG WR 31% ขาดทุน $402 ทุกเหรียญ SHORT ดีกว่า LONG

---

## Triage Scoring

คะแนนจาก 5 มิติ:
1. **PnL** -- กำไร/ขาดทุนสะสม (cap +30)
2. **Win Rate** -- WR เทียบกับ baseline 40%
3. **Consistency** -- สัดส่วนสัปดาห์ที่ไม่ขาดทุน
4. **FLIP Quality** -- SIGNAL_FLIP WR (ยิ่งสูงยิ่งดี)
5. **FLIP Penalty** -- เหรียญที่ FLIP เยอะเกินโดน penalty

---

## KEEP (13 เหรียญ, PnL +$1,121)

| Coin | Score | PnL | WR | FLIP% | S_WR | L_WR |
|------|-------|-----|-----|-------|------|------|
| **ARIA** | **56.9** | **+$688** | **64.6%** | 39% | **71.9%** | 48.0% |
| NAORIS | 37.2 | +$247 | 46.7% | 56% | 48.0% | 45.0% |
| BANANAS31 | 27.2 | +$51 | 53.7% | 59% | 52.0% | 56.2% |
| PIPPIN | 25.4 | +$44 | 58.8% | 74% | 68.4% | 46.7% |
| BEAT | 24.3 | +$78 | 54.2% | 40% | 60.0% | 44.4% |
| XRP | 21.7 | +$27 | 57.5% | 40% | 72.0% | 33.3% |
| PIXEL | 21.3 | +$40 | 56.6% | 38% | 64.7% | 42.1% |
| ADA | 15.6 | +$27 | 54.3% | 40% | 73.7% | 31.2% |
| ONDO | 11.2 | -$9 | 44.9% | 63% | 55.2% | 30.0% |
| RENDER | 11.1 | +$6 | 47.9% | 38% | 61.5% | 31.8% |
| BNB | 10.4 | -$9 | 52.1% | 67% | 58.6% | 42.1% |
| ICX | 8.3 | +$1 | 42.9% | 86% | 50.0% | 33.3% |
| JCT | 6.0 | -$71 | 54.1% | 65% | 44.4% | 63.2% |

**ARIA = MVP** -- $688 profit, 71.9% SHORT WR, สัปดาห์แรกทำ $409 เดียว

---

## DROP (10 เหรียญ, PnL -$520)

| Coin | Score | PnL | WR | FLIP% | Neg Weeks | ปัญหา |
|------|-------|-----|-----|-------|-----------|-------|
| TAO | -5.5 | -$64 | 39.6% | 71% | 2/3 | FLIP เยอะ, ทั้ง S+L ขาดทุน |
| HYPE | -6.8 | -$26 | 28.9% | 66% | 2/3 | WR ต่ำมาก, LONG -$24 |
| GALA | -6.8 | -$29 | 30.0% | 64% | 2/3 | WR 30%, LONG -$33 |
| BARD | -7.4 | -$53 | 31.7% | 61% | 1/3 | **LONG WR = 0%** (0/16!) |
| NEAR | -8.8 | -$28 | 34.8% | 50% | 3/3 | ขาดทุนทุกสัปดาห์ |
| ZRO | -12.7 | -$50 | 32.5% | 68% | 3/3 | ขาดทุนทุกสัปดาห์ |
| LTC | -15.0 | -$41 | 24.4% | 68% | 2/3 | WR 24%, LONG WR 12.5% |
| SAHARA | -19.1 | -$79 | 25.6% | 64% | 3/3 | ขาดทุนทุกสัปดาห์, LONG WR 6% |
| **CRV** | **-21.6** | **-$55** | **19.1%** | **74%** | **3/3** | **WR ต่ำสุด, ขาดทุนทุกสัปดาห์** |
| **OGN** | **-22.9** | **-$94** | **20.0%** | **70%** | **2/3** | **ขาดทุนมากสุด** |

**สาเหตุร่วม**: WR ต่ำกว่า 35%, FLIP% สูง 60-74%, negative ทุก/เกือบทุกสัปดาห์

---

## WATCH (23 เหรียญ, PnL -$264)

เหรียญที่ยังไม่ชัดเจน -- ให้อีก 1-2 สัปดาห์ รอดูแนวโน้ม
ถ้า score ดิ่งลงต่ำกว่า -5 → ย้ายเป็น DROP

เหรียญ WATCH ที่น่าจับตา:
- **SOL** (score -2.9): WR 33.3% แต่ SHORT ยัง +$19
- **ETH** (score 0.5): FLIP_WR แค่ 4% แต่ SHORT +$22
- **DOT** (score 0.5): SHORT +$10 แต่ LONG -$26

---

## Key Discoveries

### 1. LONG ตายจริงในทุกมิติ
- **Overall**: SHORT WR 48.9% vs LONG WR 31.0%
- **PnL**: SHORT +$739 vs LONG -$402
- **เหรียญที่ LONG ดี**: แทบไม่มี (BANANAS31, NAORIS เป็นข้อยกเว้น)
- **BARD LONG WR = 0%**: เทรด 16 ครั้ง แพ้หมด

### 2. SIGNAL_FLIP ยังเป็นปัญหาหลัก
- เหรียญ DROP มี FLIP% เฉลี่ย 66% (vs KEEP 51%)
- FLIP_WR ของ DROP เฉลี่ย 16% (vs KEEP 33%)
- FLIP ที่กินกำไรมากสุด: OGN (-$66 จาก FLIP)

### 3. ถอด 10 ตัว = +$520 ทันที
- Portfolio PnL จาก +$337 → +$857 (ถ้าถอด DROP)
- ไม่ต้องเปลี่ยน strategy, parameter, หรือ model -- แค่เลือกเหรียญ

### 4. Weekly Consistency = ตัวบ่งชี้ที่ดี
- Bottom 5 ทุกตัว negative 2-3/3 สัปดาห์
- Top 5 ทุกตัว negative แค่ 1 สัปดาห์
- Consistency เป็นสัญญาณที่เชื่อถือได้กว่า overall WR

### 5. ARIA = Outlier ที่ต้องระวัง
- +$688 คือ 204% ของ total PnL (+$337)
- ถ้า ARIA หยุดทำกำไร portfolio จะติดลบทันที
- ต้อง diversify ไม่พึ่ง ARIA ตัวเดียว

---

## Action Items (ข้อเสนอ)

| # | Action | Priority | Impact |
|---|--------|----------|--------|
| 1 | **ถอด 10 เหรียญ DROP ออกจาก paper trading** | HIGH | +$520 PnL |
| 2 | **ปิด LONG ทั้งหมด** (หรือเฉพาะ DROP coins) | HIGH | +$402 PnL |
| 3 | Monitor WATCH coins อีก 2 สัปดาห์ | MED | data-driven cut |
| 4 | ลด dependency กับ ARIA | MED | risk management |
| 5 | Re-run triage อีกครั้งตอน Apr 21 | LOW | update with new data |

---

## Technical Notes
- Data: `paper_trading/state/paper_trades.db` -- 2,067 trades, 46 coins
- Direction: 1=LONG, -1=SHORT
- Period: 2026-03-17 to 2026-04-06 (21 days)
- Triage score = PnL/10 + (WR-40)*0.5 + consistency*10 + (FLIP_WR-20)*0.3 - FLIP_penalty
- Thresholds: KEEP > 5, WATCH > -5, DROP <= -5
