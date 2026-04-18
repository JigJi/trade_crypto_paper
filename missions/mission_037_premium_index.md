# Mission 037: Premium Index as Real-Time Positioning Signal

**วันที่**: 2026-04-15
**ประเภท**: factor_research / derivatives / premium_index
**ความยาก**: hard (6 experiments, 58K premium rows, 12 factor configs x 6 coins, 85K+ trades)
**สถานะ**: COMPLETED -- **FAIL** -- Premium มี edge เดี่ยว แต่ทำลาย v3 ทุก config

---

## แรงบันดาลใจ

Mission 035 ค้นพบว่า funding_rate active แค่ **0.4%** ของเวลา แต่เมื่อ agree กับ ob_combined ให้ WR 93.3% (30 trades)
ปัญหาคือ FR เป็น 8h resolution (856 records) -- rare มาก

**premium_index** (mark - index spread) อัปเดตทุก 5 นาที มี **58,734 records** -- เป็น "live FR" ที่เห็นการ position ของ crowd แบบ real-time

**สมมติฐาน**: Premium contrarian signal (extreme premium → expect reversal) จะช่วย v3 เพราะ:
1. Resolution สูงกว่า FR 70x (5min vs 8h)
2. Premium คือ "สาเหตุ" ที่ทำให้ FR settle -- ควรเป็น leading indicator
3. Active 2-3% vs FR 0.2% → signal ไม่หายากเกินไป

---

## EXP1: Premium Distribution

| Metric | Value |
|--------|-------|
| Records | 58,734 |
| Mean | -0.000435 (-0.044%) |
| Std | 0.000123 |
| Min | -0.00175 (-0.175%) |
| Max | +0.00555 (+0.555%) |
| Skew | +9.39 (skew ขวามาก!) |
| Kurtosis | 244 (fat tails สุดขีด) |

**Premium เป็นลบเกือบตลอด** (mean = -0.044%) -- mark ต่ำกว่า index เป็นปกติ
มี extreme positive spikes (+0.5%) ที่หายากมาก → ทำให้ distribution เบ้ขวาอย่างรุนแรง

| Extreme | Count | % |
|---------|-------|---|
| z < -2 | 1,064 | 1.8% |
| z > 2 | 1,710 | 2.9% |

---

## EXP2: Forward Returns หลัง Premium Extremes

| Z-Score Bucket | n | Avg 1h (bps) | Avg 4h (bps) | Contrarian WR |
|---------------|---|-------------|-------------|--------------|
| **z < -3** | **55** | **+20.1** | **+94.7** | **54.5%** |
| -3 < z < -2 | 262 | -5.3 | -6.4 | 50.0% |
| -2 < z < -1 | 2,053 | +0.3 | +3.0 | 49.8% |
| -1 < z < 1 | 13,281 | -0.9 | -4.3 | -- |
| 1 < z < 2 | 1,939 | -1.1 | -6.4 | 50.2% |
| **2 < z < 3** | **364** | **-8.9** | **-20.9** | **55.5%** |
| **z > 3** | **95** | **-8.4** | **-19.4** | **55.8%** |

**ค้นพบ**:
- z < -3 (premium ต่ำสุด): contrarian LONG ได้ +95bps ที่ 4h (แต่ n=55 เท่านั้น)
- z > 2 (premium สูง): contrarian SHORT ได้ -21bps ที่ 4h, WR 55.5-55.8%
- **ทั้ง 2 ทิศ contrarian ได้ผลจริง** แต่ edge ไม่แข็งแรง (WR 54-56%)

---

## EXP3: Premium เป็น v3 Factor -- ผลลัพธ์

**Baseline**: 7,114 trades, WR 70.6%, PnL **$10,727**

| Weight | Threshold | Trades | WR | PnL | Delta |
|--------|-----------|--------|-----|-----|-------|
| 0.5 | 1.5 | 7,072 | 70.1% | $9,890 | **-$837** |
| 0.5 | 2.0 | 7,124 | 70.1% | $10,103 | **-$624** |
| 0.5 | 2.5 | 7,127 | 70.3% | $10,011 | **-$716** |
| 1.0 | 1.5 | 6,821 | 70.0% | $9,046 | **-$1,681** |
| 1.0 | 2.0 | 7,009 | 70.0% | $9,133 | **-$1,594** |
| 1.0 | 2.5 | 7,090 | 70.4% | $9,790 | **-$937** |
| 1.5 | 1.5 | 6,472 | 70.1% | $8,835 | **-$1,892** |
| 1.5 | 2.0 | 6,869 | 69.8% | $8,790 | **-$1,937** |
| 1.5 | 2.5 | 7,074 | 70.5% | $9,553 | **-$1,174** |
| 2.0 | 1.5 | 6,390 | 70.2% | $7,710 | **-$3,018** |
| 2.0 | 2.0 | 6,822 | 69.7% | $7,907 | **-$2,820** |
| 2.0 | 2.5 | 7,077 | 70.3% | $9,171 | **-$1,556** |

**ทุก config ทำให้ v3 แย่ลง!** ดีที่สุดคือ w=0.5, th=2.0 ที่ -$624 (ยังแย่อยู่ดี)

**Pattern ชัดเจน**: ยิ่ง weight สูง → ยิ่งทำลาย, ยิ่ง threshold ต่ำ → ยิ่งทำลาย

---

## EXP4: Activation Comparison

| Signal | Active Bars | % of Total | Ratio |
|--------|-------------|-----------|-------|
| Premium (z>=2) | 11,501 | 10.9% | 72x |
| FR 8h | 161 | 0.2% | 1x |

Premium fires **72 เท่า** ของ FR -- นี่คือปัญหา! Signal ที่ fire บ่อยเกินไปกลายเป็น noise

---

## EXP5: Premium Extremes by Hour

ชั่วโมงที่มี extreme premium มากที่สุด: **09 UTC (1.6%), 07-08 UTC (1.1%)**
-- ตรงกับช่วง Asia morning ที่ market liquidity ต่ำ

---

## EXP6: Agreement กับ FR 8h

| Metric | Value |
|--------|-------|
| Both active | 135 bars |
| Agreement | **100%** |
| Premium-only signals | 11,366 |

เมื่อทั้งคู่ active พร้อมกัน **agree 100%** -- premium เป็น superset ของ FR
แต่ premium มี 11,366 signals ที่ FR ไม่ได้ fire → ส่วนเกินนี้คือ **noise ที่ทำลาย v3**

---

## ทำไม Premium ถึง Fail เป็น Factor?

1. **Fire บ่อยเกินไป (10.9%)**: FR ที่ work คือ event ที่หายาก (0.2%) -- ความหายากคือสิ่งที่ทำให้มีคุณค่า
2. **Premium skew รุนแรง**: mean = -0.044%, skew = +9.4 → z-score ไม่เหมาะกับ distribution นี้
3. **Conflict กับ factors อื่น**: Premium extreme มักเกิดตอน market chaos (liquidation cascade, OB extreme) -- ซ้อนทับกับ signal ที่มีอยู่แล้ว
4. **ไม่ใช่ "event"**: FR 8h settle ทุก 8 ชั่วโมง = discrete event, premium เปลี่ยนตลอดเวลา = state-based → เหมือน ATM IV ที่ fail (mission 004)

**บทเรียน**: Signal ที่ rare + event-based (FR, liquidation) ดีกว่า signal ที่ continuous + frequent (premium, IV) สำหรับระบบ 15m contrarian

---

## Insight หลัก

Premium index (mark-index spread) มี contrarian edge เป็นของตัวเอง (z<-3: +95bps at 4h, z>3: contrarian WR 55.8%) แต่เมื่อรวมเข้า v3 **ทำให้แย่ลง -$624 ถึง -$3,018 ทุก config** เพราะ fire บ่อยเกินไป (10.9% vs FR 0.2%) และเป็น state-based signal ที่ซ้อนทับกับ factors ที่มีอยู่ สิ่งที่ทำให้ FR ดีคือ **ความหายาก** ไม่ใช่ข้อมูลเอง

---

## สิ่งที่ยังน่าทดลอง

- Premium เป็น **filter** (ไม่ใช่ factor) -- block trade เมื่อ premium ขัดแย้งกับ signal?
- Premium **rate of change** แทน level -- premium กำลังพุ่ง/ร่วง = momentum signal?
- Premium extreme duration (อยู่นานกี่บาร์ก่อน revert?) -- timing exit?
- ใช้ premium เป็น percentile แทน z-score (เพราะ distribution เบ้มาก)

---

## Tags
`factor_research`, `premium_index`, `derivatives`, `funding_rate`, `contrarian`, `fail`, `signal_frequency`
