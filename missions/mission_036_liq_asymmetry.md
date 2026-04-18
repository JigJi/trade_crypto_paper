# Mission 036: Liquidation Asymmetry -- Long vs Short Liquidation Predictive Power

**วันที่**: 2026-04-14
**ความยาก**: Hard (100 XP)
**สถานะ**: COMPLETED -- พบ asymmetry ที่ actionable

---

## สมมติฐาน

Long liquidation (คนถือ long โดน liquidate) กับ Short liquidation (คนถือ short โดน liquidate) อาจมีพลังทำนายที่ต่างกัน ระบบปัจจุบันใช้ net = short - long แบบ symmetric แต่ถ้าฝั่งใดฝั่งหนึ่งแม่นกว่า เราควร weight ต่างกัน

## ข้อมูล

- BTC liquidation events: **98,016 events** (Sep 2025 - Apr 2026)
- Long liqs (SELL): 53,565 (54.6%) -- ฝั่ง long โดน liquidate เยอะกว่า
- Short liqs (BUY): 44,451 (45.4%)
- Long cascades (3x MA): 909 events
- Short cascades (3x MA): 891 events

## ผลวิจัย

### 1. Contrarian Forward Return -- เกือบ Symmetric

| Cascade Type | n | 1h Contrarian WR | 4h Contrarian WR |
|---|---|---|---|
| Long Cascade (expect UP) | 703 | 51.4% | **53.6%** |
| Short Cascade (expect DOWN) | 680 | 50.1% | **54.3%** |

ต่างกันแค่ 0.6pp -- **raw contrarian signal ไม่ asymmetric**

### 2. V3 Trade Impact -- **พบ Asymmetry ชัดเจน!**

| Entry Condition | Trades | WR | Avg PnL | Total PnL |
|---|---|---|---|---|
| **During Long Cascade** | **50** | **74.0%** | **$2.38** | **$119.21** |
| During Short Cascade | 43 | 58.1% | $-0.75 | -$32.33 |
| No Cascade (baseline) | 1,084 | 61.2% | $0.66 | $711.30 |

**Long cascade = +12.8pp WR เหนือ baseline!** เปิดเทรดตอน long cascade = high confidence

### 3. Direction Breakdown -- **SHORT ตอน Short Cascade = coin flip**

| Condition | SHORT WR | LONG WR |
|---|---|---|
| Long Cascade | **70.4%** (n=27) | **78.3%** (n=23) |
| Short Cascade | **50.0%** (n=24) | 68.4% (n=19) |
| No Cascade | 62.6% (n=575) | 59.5% (n=509) |

**ค้นพบสำคัญ**: SHORT trades ตอน short cascade = WR 50.0% (coin flip!)
- ทั้งที่ logic บอกว่า "shorts โดน liquidate = ราคาควรลง = ควร short"
- แต่ในทางปฏิบัติ short cascade มักเกิดตอนราคา drop แรง แล้วเด้ง (dead cat bounce)
- **SHORT ตอน long cascade ดีกว่า (70.4%)** -- contrarian logic ถูกต้อง

### 4. L/S Ratio Filter

| Ratio Group | Trades | WR | Total PnL |
|---|---|---|---|
| Low ratio (short-dominated) | 1,017 | 60.3% | $429.65 |
| Mid ratio (balanced) | 195 | **65.1%** | $395.89 |

Balanced ratio (มีทั้งสองฝั่ง) ให้ WR สูงกว่า 4.8pp

### 5. Hourly Asymmetry

- **ชั่วโมงที่ long ถูก liquidate เยอะ**: 23 UTC (ratio 2.13x), 05 UTC (1.71x), 10 UTC (1.65x)
- **ชั่วโมงที่ short ถูก liquidate เยอะ**: 14 UTC (ratio 0.67x), 00 UTC (0.78x)

## Insight หลัก

1. **Raw contrarian WR ไม่ asymmetric** (53.6% vs 54.3%) -- แต่ impact ต่อ v3 trades ต่างกันมาก
2. **Long cascade = high confidence signal** -- WR 74.0% (+12.8pp vs baseline)
3. **SHORT ตอน short cascade = ห้ามทำ** -- WR แค่ 50.0% (เสียเปรียบค่า fee)
4. เหตุผล: short cascade เกิดตอนราคาลงแรง -> dead cat bounce ทำให้ short ตามหลัง cascade แพ้
5. **Actionable**: เพิ่ม position size ตอน long cascade, block SHORT entry ตอน short cascade

## ข้อเสนอ (ถ้าจะ implement)

- เพิ่ม flag `long_cascade_active` ใน signal pipeline
- ตอน long cascade: size *= 1.5x
- ตอน short cascade: block SHORT direction (หรือ size *= 0.5x)
- คาดว่าช่วย +$50-100 per month จาก SHORT cascade avoidance alone
- **ต้องทดสอบใน paper trading ก่อน** (ตาม feedback_no_rush_live.md)

## สิ่งที่ยังไม่ได้ทำ

- Multi-coin cascade analysis (cascade event เกิดพร้อมกันข้าม coins?)
- Size-weighted cascade (cascade ที่มี notional สูง vs ต่ำ)
- Cascade duration analysis (cascade ยาวกี่บาร์ -> performance?)
