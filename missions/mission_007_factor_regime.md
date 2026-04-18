# Mission #007: Factor Regime Decomposition -- Factor ไหนทำงานใน Regime ไหน?
**วันที่**: 2026-03-18 | **XP**: 100 | **Difficulty**: Hard | **Status**: COMPLETED

---

## สมมติฐาน
v3 ใช้ 8 factors ด้วย weight คงที่ทุก regime ตลาด
แต่ factors แต่ละตัวอาจทำงานต่างกันใน BULL/BEAR/FLAT
ถ้ารู้ว่า factor ไหนดีใน regime ไหน อาจปรับ weight ตาม regime เพื่อเพิ่ม PnL ได้

## ผลลัพธ์: ค้นพบ factor hierarchy ชัดเจน แต่ regime-adaptive weighting ไม่คุ้มค่า

---

## OOS Period: Jan 2025 - Mar 2026 (15 เดือน, SL=10 ATR)

### Regime Distribution
| Regime | Bars | % | Trades | WR% | PnL | PnL/Trade |
|--------|------|---|--------|-----|-----|-----------|
| BULL | 12,384 | 29.9% | 897 | 75.5% | $2,678 | $2.99 |
| **BEAR** | **19,488** | **47.1%** | **1,697** | **79.5%** | **$6,479** | **$3.82** |
| FLAT | 9,505 | 23.0% | 653 | 73.2% | $1,639 | $2.51 |
| ALL | 41,377 | 100% | 3,235 | 77.1% | $10,755 | $3.32 |

**BEAR = Cash Cow**: 47% ของ bars แต่ 60% ของ PnL ทั้งหมด, WR สูงสุด 79.5%

---

## EXP 2: Factor Ablation by Regime (core finding)

Delta PnL = ถอด factor ออกแล้ว PnL ลดลงเท่าไหร่ (ค่า+ = factor ช่วย)

| # | Factor | ALL | BULL | BEAR | FLAT | Verdict |
|---|--------|-----|------|------|------|---------|
| 1 | **liquidation** | **+$4,311** | +$964 | **+$2,633** | +$634 | UNIVERSAL |
| 2 | **ob_combined** | **+$3,741** | +$850 | **+$2,880** | +$70 | UNIVERSAL |
| 3 | **tick_liq** | **+$1,569** | +$297 | +$820 | +$441 | UNIVERSAL |
| 4 | basis_contrarian | +$1,070 | +$222 | +$1,024 | **-$96** | TRENDING |
| 5 | oi_divergence | +$903 | +$265 | +$152 | -$0 | TRENDING |
| 6 | etf_flows | +$722 | **-$43** | +$513 | +$310 | BEAR-SPEC |
| 7 | whale_alerts | +$183 | +$176 | +$307 | **-$78** | TRENDING |
| 8 | funding_rate | +$165 | +$0 | +$88 | +$77 | BEAR-SPEC |

### การค้นพบหลัก

#### 1. "Holy Trinity" -- 3 Universal Factors ที่ทำงานทุก Regime
- **liquidation** (+$4,311): ทำงานทุก regime, BEAR ดีสุด (+$2,633)
- **ob_combined** (+$3,741): order book imbalance, BEAR ดีมาก (+$2,880)
- **tick_liq** (+$1,569): สม่ำเสมอทุก regime (BULL $297, BEAR $820, FLAT $441)

รวมกัน = $9,621 (89% ของ total factor contribution $10,755)

#### 2. "Trending Duo" -- ทำงานใน BULL+BEAR แต่ FLAT อ่อน
- **basis_contrarian** (+$1,070): BEAR dominate (+$1,024) แต่ **เจ็บใน FLAT (-$96)**
- **oi_divergence** (+$903): BULL ช่วยได้ (+$265) แต่ FLAT = ศูนย์

#### 3. "Specialists" -- Factor เฉพาะ Regime
- **etf_flows** (+$722): **เจ็บใน BULL (-$43!)** แต่ BEAR+FLAT ดี
- **funding_rate** (+$165): zero contribution ใน BULL, เล็กน้อยใน BEAR/FLAT
- **whale_alerts** (+$183): เจ็บใน FLAT (-$78)

#### 4. BEAR Dominance ชัดเจน
- ทุก factor ให้ delta สูงสุดใน BEAR regime
- ob_combined ใน BEAR เพียง regime เดียว = $2,880 (27% ของ total PnL!)
- BEAR = regime ที่ contrarian factors ทำงานดีที่สุด (leveraged shorts get liquidated)

---

## EXP 3: Factor Firing Frequency

| Factor | ALL | BULL | BEAR | FLAT | Bull% | Bear% |
|--------|-----|------|------|------|-------|-------|
| etf_flows | 22.7% | 24.4% | 17.5% | **31.4%** | 9.7% | 13.0% |
| ob_combined | 20.4% | 16.8% | **25.0%** | 15.5% | 9.2% | 11.2% |
| oi_divergence | 7.8% | 3.5% | **12.0%** | 4.8% | 3.9% | 3.9% |
| tick_liq | 6.1% | **8.5%** | 5.0% | 5.1% | 2.0% | 4.1% |
| liquidation | 5.9% | 4.9% | 6.3% | 6.6% | 2.5% | 3.5% |
| basis_contrarian | 4.5% | 3.8% | **5.6%** | 3.1% | 2.2% | 2.2% |
| whale_alerts | 1.0% | 1.3% | 0.9% | 0.8% | 0.8% | 0.2% |
| **funding_rate** | **0.3%** | **0.0%** | 0.5% | 0.3% | 0.3% | 0.0% |

**Key insight**: funding_rate fires แค่ 0.3% ของ bars! ให้ delta เพียง $165
- ใน BULL ไม่เคย fire เลย (0.0%)
- ob_combined fires 25% ใน BEAR (สูงสุด) -- สอดคล้องกับ delta ที่สูงใน BEAR
- etf_flows fires บ่อยที่สุด (22.7%) แต่ delta/fire ต่ำ

---

## EXP 4: Rolling Factor Stability (Quarterly Windows)

| Factor | Q1'25 | Q2'25 | Q3'25 | Q4'25 | Q1'26 | Sign Consistency | CV |
|--------|-------|-------|-------|-------|-------|------------------|-----|
| oi_divergence | +$0 | +$0 | +$0 | +$816 | +$87 | 60% | 1.77 |
| funding_rate | +$0 | +$0 | +$0 | +$77 | +$88 | 60% | 1.23 |
| whale_alerts | +$0 | +$0 | -$45 | +$111 | +$92 | 60% | 1.20 |
| liquidation | +$0 | +$0 | +$838 | +$2,389 | +$1,090 | 40% | 1.02 |
| etf_flows | +$0 | +$0 | +$552 | -$311 | +$481 | 60% | 1.21 |
| basis_contrarian | +$0 | +$0 | -$55 | -$762 | +$1,782 | 80% | 1.63 |
| tick_liq | +$0 | +$0 | +$146 | +$826 | +$597 | 40% | 1.07 |
| ob_combined | +$0 | +$0 | +$251 | +$751 | +$2,708 | 40% | 1.38 |

**หมายเหตุ**: Q1-Q2 2025 = $0 delta เพราะ data sources ยังไม่มี (DB collection เริ่มที่หลัง)
- **basis_contrarian**: HURT in Q3-Q4 2025 but +$1,782 in Q1 2026 -- regime-dependent!
- **etf_flows**: hurt -$311 in Q4 2025 -- bull regime effect?
- **liquidation, tick_liq, ob_combined**: consistent ตั้งแต่ Q3 2025 เป็นต้นมา

---

## สรุปและข้อเสนอ

### 1. v3 Factor Weights ยังคง ROBUST
- ไม่มี factor ไหนเป็น "ตัวถ่วง" อย่างรุนแรงใน regime ใด
- ส่วนต่างที่ FLAT hurt (-$78 whale, -$96 basis) เล็กน้อยเทียบกับ total PnL
- Regime-adaptive weighting จะ save ได้แค่ ~$174 ใน FLAT แต่เพิ่มความซับซ้อน + overfit risk

### 2. Factor Hierarchy ชัดเจน
- **Tier 1 (UNIVERSAL)**: liquidation, ob_combined, tick_liq -- 89% ของ contribution
- **Tier 2 (TRENDING)**: basis_contrarian, oi_divergence -- ดีใน trending, อ่อนใน flat
- **Tier 3 (WEAK)**: etf_flows, whale_alerts, funding_rate -- marginal contribution

### 3. funding_rate = weakest factor
- fires 0.3% of bars, $165 total delta
- zero contribution ใน BULL
- **candidate for removal or weight reduction?** (ต้องทดสอบก่อน -- อาจมี interaction effects)

### 4. BEAR = ที่มาของ alpha
- Strategy มี inherent bear-market edge (contrarian factors ทำงานดีเมื่อ leveraged longs ถูก squeeze)
- ไม่ใช่ bug -- เป็น structural feature ของ contrarian strategy

### 5. No action needed for now
- v3 weights ยังดี ไม่ต้องเปลี่ยน
- แต่ถ้าจะทำ v5 ในอนาคต: focus on Tier 1 factors, ลอง reduce funding_rate weight
- ข้อมูลนี้มีประโยชน์สำหรับ risk management -- รู้ว่า FLAT periods จะมี edge น้อยลง

---

## Methodology
- **Regime definition**: BTC daily SMA20, 5-day slope. BULL (>+0.5%), BEAR (<-0.5%), FLAT (in between)
- **Ablation**: remove one factor at a time, measure delta PnL per regime
- **OOS**: Jan 2025 - Mar 2026, 6 coins, SL=10 ATR, per-coin configs from v3
- **Rolling stability**: 5 quarterly windows, measure sign consistency + CV
- **Experiments**: 4 (baseline, ablation, firing freq, stability)
