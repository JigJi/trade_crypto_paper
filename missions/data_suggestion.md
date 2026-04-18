# Suggestion: ข้อมูลที่ควรเพิ่มเข้า smart_trading DB

วิเคราะห์จากโครงสร้าง DB ปัจจุบัน + factor ที่ใช้ใน v3 + งานวิจัย

## สถานะ DB ปัจจุบัน (2026-03-14)

### ข้อมูลที่มีอยู่แล้ว (Active)
| ตาราง | จำนวน | ช่วงข้อมูล | ใช้ใน v3 |
|-------|--------|------------|----------|
| market_data.liquidation | 61,495 | 6 เดือน | YES (w=2.0) |
| market_data.order_book_raw | หลายล้าน | 6 เดือน (partitioned) | YES (ob_combined w=2.0) |
| market_data.basis | - | - | YES (basis_contrarian w=1.5) |
| market_data.premium_index | 49,965 | - | YES (ใช้ใน BTC features) |
| market_data.open_interest | 49,401 | - | YES (oi_divergence w=0.5) |
| public.funding_rate | 762 | 9 เดือน | YES (w=2.0) |
| public.whale_alert | 9,210 | 9 เดือน | YES (w=1.5) |
| public.etf_btc | 168 | 9 เดือน | YES (etf_flows w=1.0) |
| market_data.cvd | 927 | 5 วัน | tested_positive (bench) |
| market_data.options_data | 479 | 5 วัน | untested (dvol, skew, gex) |
| market_data.option_greeks | 25,986 | 6 เดือน | ยังไม่ใช้ |
| market_data.macro_indicators | 22 | 8 วัน | tested_positive (bench) |
| market_data.market_global | 463 | 5 วัน | tested_negative (btc.d) |
| public.fear_greed | 259 | 8 เดือน | tested_negative (dropped) |
| public.news_crypto | 4,626 | 9 เดือน | tested_negative |

### ข้อมูลเก่าจากระบบก่อน (Historical, ไม่ active)
| ตาราง | จำนวน | หมายเหตุ |
|-------|--------|----------|
| market_data.trade_log | 58,060 | trading decisions ระบบเก่า (Aug-Nov 2025) |
| market_data.trades | 351 | ผลเทรดจริงระบบเก่า |
| public.factor_agent | 7,907 | AI agent scoring (Jul-Sep 2025) |
| public.reflect_agent | 1,620 | AI reflection agent (Jul-Sep 2025) |
| public.backtest_log | 207,751 | backtest ระบบเก่า |
| public.decision_log | 64 | daily decisions ระบบเก่า |

---

## แนะนำข้อมูลใหม่ที่ควรเก็บ

### Priority 1: มีโอกาสเป็น alpha สูง

#### 1. Altcoin Funding Rates (แต่ละเหรียญ)
- **ตอนนี้**: เก็บแค่ BTC funding rate
- **ควรเพิ่ม**: funding rate ของ XRP, ADA, DOT, SUI, FIL + เหรียญ top 20
- **ทำไม**: funding rate ของ altcoin แต่ละตัวบอกได้ว่า positioning ของแต่ละเหรียญเป็นอย่างไร ไม่จำเป็นต้องดูจาก BTC เสมอ
- **แหล่งข้อมูล**: Binance API `GET /fapi/v1/fundingRate` (ฟรี)
- **ความถี่**: ทุก 8 ชม. (ตาม funding period)
- **ตาราง**: `market_data.funding_rate_alt` (symbol, ts, funding_rate, mark_price)

#### 2. Altcoin Open Interest
- **ตอนนี้**: เก็บแค่ BTC OI
- **ควรเพิ่ม**: OI ของแต่ละ altcoin ที่เทรด
- **ทำไม**: OI divergence เป็น factor ที่ใช้ใน v3 แต่ดูจาก BTC อย่างเดียว ถ้าดู per-coin OI จะเพิ่มความแม่นของสัญญาณ
- **แหล่งข้อมูล**: Binance API `GET /fapi/v1/openInterest` (ฟรี)
- **ความถี่**: ทุก 15 นาที (ตาม timeframe)
- **ตาราง**: `market_data.open_interest_alt` (symbol, ts, oi_contracts, oi_usdt)

#### 3. Aggregated Liquidation Stats (รายชั่วโมง)
- **ตอนนี้**: เก็บ raw liquidation events (61K rows, event-level)
- **ควรเพิ่ม**: pre-compute สรุปรายชั่วโมง + 15 นาที
- **ทำไม**: ตอนนี้ backtest ต้อง aggregate on-the-fly ซึ่งช้า ถ้า pre-compute ไว้จะเร็วขึ้นมากและสามารถทำ cross-coin liquidation analysis ได้
- **ตาราง**: `market_data.liquidation_agg` (ts, symbol, interval, long_liq_usd, short_liq_usd, count)
- **หมายเหตุ**: สร้างจาก cron job ที่ aggregate จาก liquidation table

### Priority 2: มีศักยภาพ ต้องทดสอบ

#### 4. Realized Volatility (Pre-computed)
- **ทำไม**: Mission #001 พบว่าช่วงเวลาที่ volatility สูง (16:00 UTC) โมเดลทำงานแย่ ถ้ามี realized vol เป็น feature จะช่วย filter ได้
- **แหล่งข้อมูล**: คำนวณจาก OHLCV ที่มีอยู่แล้ว
- **ความถี่**: ทุก 15 นาที (rolling 1h, 4h, 24h)
- **ตาราง**: `market_data.realized_vol` (ts, symbol, vol_1h, vol_4h, vol_24h)

#### 5. Options Term Structure
- **ตอนนี้**: เก็บ dvol (single number), skew_25d, max_pain
- **ควรเพิ่ม**: IV curve across multiple expirations (7d, 30d, 90d)
- **ทำไม**: Term structure inversion (short-term IV > long-term IV) บ่งบอก panic/fear ที่แม่นกว่า dvol เดี่ยวๆ
- **แหล่งข้อมูล**: Deribit API (ฟรี)
- **ตาราง**: `market_data.options_term_structure` (ts, expiry_days, iv, delta_25_call_iv, delta_25_put_iv)

#### 6. Taker Buy/Sell Volume (Per-Coin)
- **ตอนนี้**: CVD เก็บแค่ BTC
- **ควรเพิ่ม**: taker volume ratio ของ altcoin แต่ละตัว
- **ทำไม**: ดู aggression ของ buyer/seller ในแต่ละเหรียญแยกจาก BTC
- **แหล่งข้อมูล**: Binance klines มี `taker_buy_base_vol` อยู่แล้ว (ฟรี)
- **ความถี่**: ทุก 15 นาที
- **ตาราง**: `market_data.taker_volume_alt` (ts, symbol, taker_buy_vol, taker_sell_vol, ratio)

### Priority 3: Nice-to-have / ระยะยาว

#### 7. Exchange Flow (On-Chain)
- **อะไร**: BTC/ETH inflow-outflow จาก exchange wallets
- **ทำไม**: Large inflows to exchange = ขาย, Large outflows = สะสม
- **แหล่งข้อมูล**: CryptoQuant API (มีค่าใช้จ่าย), Glassnode (มีค่าใช้จ่าย), หรือ blockchain.com (ฟรีบางส่วน)
- **ข้อจำกัด**: ส่วนใหญ่เป็น paid API

#### 8. Social Sentiment Score
- **อะไร**: sentiment จาก Twitter/X, Reddit, Telegram
- **ทำไม**: sentiment ขั้นกว่า news_crypto (ที่ test แล้วไม่ work) อาจจะ work ถ้ามี real-time stream
- **แหล่งข้อมูล**: LunarCrush API (มี free tier)
- **ข้อจำกัด**: news_directional/contrarian ทดสอบแล้วไม่ work บน 15m อาจจะไม่คุ้ม

#### 9. Cross-Exchange Basis
- **อะไร**: ส่วนต่างราคาระหว่าง exchanges (Binance vs Bybit vs OKX)
- **ทำไม**: cross-exchange basis บอก flow ของเงินระหว่าง exchange ซึ่งนำหน้าราคาได้
- **แหล่งข้อมูล**: ต้อง query จากหลาย exchange
- **ข้อจำกัด**: เพิ่มความซับซ้อนของ infrastructure

---

## ข้อมูลที่มีอยู่แต่ยังไม่ได้ใช้ประโยชน์

### 1. market_data.option_greeks (25,986 rows, 6 เดือน)
- มี IV, delta, gamma, theta, vega ของ BTC options
- **เสนอ**: สร้าง factor ใหม่จาก gamma exposure (GEX) -- market_data.options_data มี gex_net แล้วแต่แค่ 5 วัน
- **Mission idea**: คำนวณ GEX จาก option_greeks ย้อนหลัง 6 เดือน แล้ว backtest เป็น factor

### 2. public.news_crypto (4,626 rows, 9 เดือน)
- ทดสอบแล้วว่า news sentiment ไม่ work บน 15m
- **แต่**: ยังไม่ได้ทดสอบ "news volume spike" (จำนวนข่าวต่อชั่วโมงเพิ่มขึ้นผิดปกติ = volatility กำลังมา)
- **Mission idea**: ทดสอบ news_volume_spike เป็น volatility regime filter

### 3. market_data.trade_log + trades (ระบบเก่า, 58K decisions, 351 trades)
- ข้อมูลการตัดสินใจของระบบเก่า
- **ใช้ประโยชน์**: เปรียบเทียบ v3 signals กับ decisions ของระบบเก่า ดูว่า overlap กี่ %

### 4. public.factor_agent + reflect_agent (9,527 AI decisions)
- AI agents ที่เคยให้ score/confidence ในแต่ละเหรียญ
- **ใช้ประโยชน์**: วิเคราะห์ว่า AI agent เคยถูกบ่อยแค่ไหน ใช้เป็น baseline เปรียบเทียบกับ v3

---

## ลำดับความสำคัญสรุป

| # | ข้อมูล | ความยาก | ค่าใช้จ่าย | ศักยภาพ Alpha |
|---|--------|---------|------------|---------------|
| 1 | Altcoin Funding Rates | ง่าย | ฟรี | สูง |
| 2 | Altcoin Open Interest | ง่าย | ฟรี | สูง |
| 3 | Liquidation Aggregation | ง่าย (compute) | ฟรี | ปานกลาง (speed) |
| 4 | Realized Volatility | ง่าย (compute) | ฟรี | ปานกลาง |
| 5 | Options Term Structure | ปานกลาง | ฟรี (Deribit) | ปานกลาง |
| 6 | Per-Coin Taker Volume | ง่าย | ฟรี | ปานกลาง |
| 7 | Exchange Flow | ยาก | มีค่าใช้จ่าย | สูง |
| 8 | Social Sentiment | ปานกลาง | Free tier | ต่ำ (15m) |
| 9 | Cross-Exchange Basis | ยาก | ฟรี | ปานกลาง |

**แนะนำเริ่มจาก #1 + #2** (Altcoin Funding + OI) เพราะง่าย ฟรี และมีโอกาสเป็น alpha สูงสุด
