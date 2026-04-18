# Mission 032: Cross-Coin Correlation & Diversification Analysis

**วันที่**: 2026-04-10
**ประเภท**: portfolio_analysis / diversification / risk_management
**ความยาก**: hard (6 experiments, 3,856 trades, eigenvalue analysis, greedy optimization)
**สถานะ**: COMPLETED -- **CRITICAL FINDING** -- 6 เหรียญ ≈ 1.6 independent bets เท่านั้น

---

## แรงบันดาลใจ

Strategy ใช้ BTC composite signal เดียวกันทุกเหรียญ → ทุกเหรียญเข้า/ออก พร้อมกัน
**คำถาม**: มี 6 เหรียญ (หรือ 13 ใน production) แต่จริงๆ ได้ diversification เท่าไหร่?
ถ้า correlation สูงมาก = สุมความเสี่ยงโดยไม่ได้ benefit

ต่อยอด: Mission 006 (concentration risk), Mission 029 (coin triage)

---

## EXP1: Trade Timing Overlap

| Metric | Value |
|--------|-------|
| Total bars (OOS) | 42,649 |
| Bars in market | 8,265 (19.4%) |
| Avg concurrent positions | 0.50 |
| Max concurrent | **6 (ทุกเหรียญพร้อมกัน!)** |
| Multi-coin entry bars | **923/1,386 (66.6%)** |
| Avg coins per entry bar | **2.78** |

### Concurrent Position Distribution:
| # Coins Open | % of Time |
|-------------|-----------|
| 0 | 80.6% |
| 1 | 6.7% |
| 2 | 4.1% |
| 3 | 3.0% |
| 4 | 3.1% |
| 5 | 1.2% |
| 6 | 1.2% |

### Key Finding:
- **66.6% ของ entries เปิดหลายเหรียญพร้อมกัน** — เพราะ BTC signal เดียวกัน
- เฉลี่ย 2.78 เหรียญต่อ entry bar → เมื่อ entry ก็ entry ทั้งกลุ่ม
- 80.6% ของเวลาไม่มี position เลย → เมื่อเปิดก็เปิดเต็มที่

---

## EXP2: Per-Coin 15m Return Correlation

### Correlation Matrix:
| | BTC | XRP | ADA | DOT | SUI | FIL |
|---|-----|-----|-----|-----|-----|-----|
| BTC | 1.000 | 0.743 | 0.736 | 0.676 | 0.694 | 0.630 |
| XRP | | 1.000 | 0.838 | 0.774 | 0.763 | 0.663 |
| ADA | | | 1.000 | 0.848 | 0.803 | 0.713 |
| DOT | | | | 1.000 | 0.788 | 0.731 |
| SUI | | | | | 1.000 | 0.713 |
| FIL | | | | | | 1.000 |

| Metric | Value |
|--------|-------|
| **Avg pairwise correlation** | **0.741** |
| Min correlation | 0.630 (BTC-FIL) |
| Max correlation | 0.848 (ADA-DOT) |

### Most Correlated:
1. **ADA-DOT**: 0.848 — เกือบเหมือนกัน!
2. **XRP-ADA**: 0.838
3. **ADA-SUI**: 0.803

### Least Correlated:
1. **BTC-FIL**: 0.630 — ดีที่สุดแต่ก็สูง
2. **XRP-FIL**: 0.663
3. **BTC-DOT**: 0.676

### Insight:
- **ทุกคู่ correlation > 0.63** — ไม่มีเหรียญไหนที่อิสระจากกันจริงๆ
- ADA/DOT/XRP เป็นกลุ่มเดียวกัน (corr 0.83-0.85) — มีตัวเดียวพอ
- FIL แยกออกมามากสุด (avg corr ต่ำสุด) — ดีสำหรับ diversification
- BTC เองก็ correlated กับ alts น้อยสุด (0.63-0.74) — เป็น "different bet" มากกว่า alts

---

## EXP3: Win/Loss Synchronization

| Metric | Value |
|--------|-------|
| Multi-coin entries | 923 |
| **All same outcome** | **52.3%** |
| All win | 47.3% |
| All lose | 5.0% |
| Avg win fraction | 0.751 |

### Key Finding:
- **52.3% ของเวลา ทุกเหรียญ win/lose พร้อมกัน** — synchronization สูง
- **47.3% all win vs 5.0% all lose** — เมื่อ BTC signal ถูก มันถูกทุกเหรียญ
- Avg win fraction 0.751 → เมื่อ signal ถูก 75% ของเหรียญ win
- **ข่าวดี**: มีเพียง 5% ที่ทุกเหรียญแพ้พร้อมกัน (tail risk ต่ำกว่าคาด)

---

## EXP4: Effective N (Independent Bets) ⭐

| Metric | Value |
|--------|-------|
| Actual coins | 6 |
| **Effective N** | **1.59** |
| **Diversification ratio** | **26.6%** |
| PC1 variance | **78.5%** |
| Top 2 PCs variance | 85.2% |

### Eigenvalues:
| PC | Eigenvalue | % Variance |
|----|-----------|-----------|
| PC1 | 4.710 | 78.5% |
| PC2 | 0.399 | 6.6% |
| PC3 | 0.339 | 5.7% |
| PC4 | 0.246 | 4.1% |
| PC5 | 0.185 | 3.1% |
| PC6 | 0.121 | 2.0% |

### CRITICAL INSIGHT:
- **6 เหรียญ = 1.59 independent bets** — เอา 27% ของ theoretical diversification เท่านั้น
- **PC1 (= BTC movement) อธิบาย 78.5%** ของ variance ทั้งหมด
- เหรียญ 2-6 ให้ข้อมูลเพิ่มแค่ 21.5% — ส่วนใหญ่ซ้ำกับ BTC
- **การมีหลายเหรียญ ≈ เพิ่ม position size** ไม่ใช่เพิ่ม diversification

---

## EXP5: Optimal Coin Subset (Greedy Forward Selection)

| Step | +Coin | Sharpe | Total PnL |
|------|-------|--------|-----------|
| 1 | **ADA** | **7.56** | $2,249 |
| 2 | +SUI | 7.36 | $4,963 |
| 3 | +BTC | 7.20 | $6,145 |
| 4 | +XRP | 7.10 | $7,796 |
| 5 | +DOT | 6.33 | $8,837 |
| 6 | +FIL | 5.55 | $9,625 |

### Key Finding:
- **Peak Sharpe = ADA alone (7.56)** — ทุกเหรียญที่เพิ่มทำให้ Sharpe ลดลง!
- **แต่ PnL เพิ่มขึ้นเรื่อยๆ** ($2,249 → $9,625) — tradeoff: Sharpe vs absolute PnL
- DOT/FIL ทำให้ Sharpe ลดลงมาก (7.10 → 5.55) — เพิ่ม noise
- **Optimal สำหรับ risk-adjusted**: ADA + SUI + BTC (3 เหรียญ, Sharpe 7.20, PnL $6,145)
- **ADA dominates** — highest Sharpe solo, 1st pick always

### Tradeoff Decision:
- ถ้าต้องการ **risk-adjusted**: 3 เหรียญพอ (ADA, SUI, BTC)
- ถ้าต้องการ **absolute PnL**: 4 เหรียญ (+ XRP) ยังคุ้ม (Sharpe drop เล็กน้อย)
- DOT/FIL = **noise adders** — ลด Sharpe โดยไม่เพิ่มคุณภาพ

---

## EXP6: Risk Stacking — Concurrent Drawdown

| Metric | Value |
|--------|-------|
| Max drawdown | **-$780.7** (2025-11-08) |
| Worst day | **-$383.0** (2025-11-06) |
| Worst 5-day rolling | -$626.8 |
| All-red days | 7/210 (3.3%) |
| Max losing streak | 4 days (-$414.3) |

### Worst Day Breakdown (2025-11-06):
| Coin | PnL |
|------|-----|
| FIL | **-$228.3** |
| SUI | **-$181.5** |
| DOT | -$11.3 |
| BTC | +$7.1 |
| ADA | +$9.9 |
| XRP | +$21.2 |

### Key Finding:
- **FIL + SUI ทำลายวัน worst ทั้งหมด** (-$410 จาก 2 เหรียญ)
- BTC, ADA, XRP กำไรในวันนั้น → **ไม่ได้ worst พร้อมกันทุกตัว**
- All-red days เพียง 3.3% → tail risk ไม่ catastrophic
- **แต่ max drawdown -$780 = 7.8% ของ equity $10K** → ยอมรับได้

---

## สรุปผลทั้งหมด

### Finding หลัก:
1. **6 เหรียญ ≈ 1.6 independent bets** — diversification แทบไม่มี
2. **Avg correlation 0.741** — สูงมาก, ทุกเหรียญขยับเหมือนกัน
3. **66.6% entries เปิดหลายเหรียญพร้อมกัน** — BTC signal ทำให้ synchronized
4. **ADA alone = Sharpe สูงสุด** — เพิ่มเหรียญ = เพิ่ม PnL แต่ลด quality
5. **3 เหรียญ = sweet spot** (ADA, SUI, BTC) — Sharpe 7.20, PnL $6,145
6. **DOT/FIL = noise** — ลด Sharpe จาก 7.10 เป็น 5.55

### ข้อเสนอเชิงปฏิบัติ:
1. **ลดเหรียญเป็น 3-4 ตัว** (ADA, SUI, BTC, optionally XRP)
2. **เพิ่ม position size ต่อเหรียญ** แทนการเพิ่มจำนวนเหรียญ — ได้ PnL เท่ากันแต่ง่ายกว่า
3. **หา uncorrelated signal** — ไม่ใช่เหรียญใหม่ แต่ signal ใหม่ (e.g., alt-specific factors) ถึงจะได้ diversification จริง
4. **Monitor ADA-DOT/XRP-ADA** — ถ้า correlation > 0.85 ไม่ควรเทรดทั้งคู่

### Structural Insight:
> **การมีหลายเหรียญในระบบ BTC-led ≠ diversification**
> มันคือ **การเพิ่ม exposure ไปยัง BTC signal เดียวกัน**
> True diversification ต้องมาจาก signal ที่ต่างกัน ไม่ใช่ asset ที่ต่างกัน

---

## Tags
`portfolio_analysis`, `diversification`, `correlation`, `eigenvalue`, `coin_selection`, `risk_management`
