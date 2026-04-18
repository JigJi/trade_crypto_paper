# Mission #009: Discovery: volume_profile_vwap
**วันที่**: 2026-03-18 | **XP**: 70 | **Difficulty**: Hard | **Status**: COMPLETED | **Category**: Discovery

---

## สมมติฐาน
Price deviation from VWAP predicts mean reversion on 15m timeframe

## หัวข้อวิจัย: volume_profile_vwap
**Search query**: `VWAP deviation crypto futures alpha strategy 2026`

---

## Phase 1: Web Research (5 แหล่ง)

### 1. Best Alpha Futures Strategy to Actually Get Consistent Payouts (2026)
**URL**: https://www.proptradingvibes.com/blog/best-alpha-futures-strategy

> The strategy I use to pass Alpha Futures evaluations and pull consistent payouts — VWAP setups, session timing, and position sizing tailored to Alpha.

### 2. VWAP Scalping Strategy for High Volatility Crypto Markets 2026
**URL**: https://www.livevolatile.com/blog/vwap-scalping-strategy-crypto-2026

> Meta Description: Master VWAP scalping in crypto&#x27;s volatile markets. Learn institutional-grade execution, optimal entry/exit rules, and risk management for consistent 1-3% daily gains. Target Keywords: VWAP scalping crypto, volume weighted average price strategy, crypto scalping 2026

### 3. VWAP Trading Strategy That Actually Works in 2026 | FibAlgo
**URL**: https://fibalgo.com/education/vwap-trading-strategy-institutional-benchmark

> VWAP Trading Strategy That Actually Works in 2026 Master VWAP trading with 3 high-probability setups, sector-specific adjustments, and the institutional benchmark framework professional traders use daily.

### 4. What you need to know about crypto vwap orders in 2026
**URL**: https://cow.fi/learn/what-you-need-to-know-about-crypto-vwap-orders-in-2026

> Conclusion A VWAP order is an execution strategy that aims to match the market&#x27;s volume‑weighted average price by slicing a large order into smaller trades across time. In crypto, it helps traders and institutions move size through fragmented markets with more predictable impact and a clear ben

### 5. Mastering VWAP in Crypto Trading - hyrotrader.com
**URL**: https://www.hyrotrader.com/blog/vwap-trading-strategy/

> Seasoned crypto traders incorporate VWAP into their strategies to fine-tune trade entries, exits, and overall market bias. Because VWAP represents an average price weighted by volume, it acts as an &quot;equilibrium&quot; level for the day. Many intraday crypto strategies evolve around the VWAP line

### สรุปจาก Web Research
1. **Best Alpha Futures Strategy to Actually Get Consistent Payouts (2026)**: The strategy I use to pass Alpha Futures evaluations and pull consistent payouts — VWAP setups, session timing, and posi
2. **VWAP Scalping Strategy for High Volatility Crypto Markets 2026**: Meta Description: Master VWAP scalping in crypto&#x27;s volatile markets. Learn institutional-grade execution, optimal e
3. **VWAP Trading Strategy That Actually Works in 2026 | FibAlgo**: VWAP Trading Strategy That Actually Works in 2026 Master VWAP trading with 3 high-probability setups, sector-specific ad

---

## Phase 2: Proxy Test (ทดสอบกับข้อมูลจริง)

**Method**: Computed rolling 96-bar (24h) VWAP, measured deviation vs next-bar return (mean reversion hypothesis)

**Data**: BTC 15m OHLCV (2250 candles, 30 days)

### ผลการทดสอบ

| Metric | ค่า |
|--------|-----|
| จำนวน Data Points | 2153 |
| Correlation กับ Forward Return | 0.0031 |
| Hit Rate รวม (%) | 51.4 |
| Bull Signal Count | 775 |
| Bull Hit Rate (%) | 52.0 |
| Bull Avg Return (bps) | 0.16 |
| Bear Signal Count | 803 |
| Bear Hit Rate (%) | 50.7 |
| Bear Avg Return (bps) | -0.56 |

### สรุป Proxy Test

VWAP deviation correlation = 0.0031. Buy-below-VWAP: HR 52.0% (775 signals, avg 0.16 bps). Sell-above-VWAP: HR 50.7% (803 signals, avg -0.56 bps). สัญญาณอ่อน ยังไม่พร้อมเป็น factor

> **ผลลัพธ์: ไม่มีนัยสำคัญ** -- Hit rate 51.4% ไม่ต่างจาก random (50%)

---

## Phase 3: Data Availability

| Field | ค่า |
|-------|-----|
| **ข้อมูลที่ต้องการ** | BTC 15m OHLCV |
| **แหล่งข้อมูล** | market_data.index_price_klines / Binance API |
| **สถานะ** | มีอยู่แล้ว |

---

## การประเมินรวม

**Verdict**: ไม่มีนัยสำคัญจากการทดสอบ

## ขั้นตอนถัดไป (Concrete)

1. ผลเบื้องต้นยังไม่ชัด (hit rate 51.4%)
2. ปรับ parameter (threshold, lookback window) แล้วทดสอบใหม่
3. เก็บข้อมูลเพิ่ม 2 สัปดาห์แล้ว retest
4. ทดสอบบน alt coins ด้วย (ไม่ใช่แค่ BTC)

## ความเกี่ยวข้องกับระบบ v3

- **หัวข้อ**: volume_profile_vwap
- **สมมติฐาน**: Price deviation from VWAP predicts mean reversion on 15m timeframe
- **ขั้นต่อไป**: สร้าง scorer function จากข้อมูลที่มี แล้ว backtest

## สรุป

ค้นพบ 5 แหล่งข้อมูลเกี่ยวกับ volume_profile_vwap
**Proxy Test**: VWAP deviation correlation = 0.0031. Buy-below-VWAP: HR 52.0% (775 signals, avg 0.16 bps). Sell-above-VWAP: HR 50.7% (803 signals, avg -0.56 bps). สัญ
**สถานะ**: ไม่มีนัยสำคัญจากการทดสอบ
**Next**: ผลเบื้องต้นยังไม่ชัด (hit rate 51.4%)

---

*Mission #009 completed | XP +70 | Level 11 Researcher | Streak: 7 days*
