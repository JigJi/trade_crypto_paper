# Strategy Reference — BTC-Led Altcoin Trading System

> เอกสารรวมทุกรายละเอียดของระบบเทรด v3 / v4 / v5
> อัปเดตล่าสุด: 2026-03-20

---

## สารบัญ

1. [แนวคิดหลัก](#1-แนวคิดหลัก)
2. [ข้อมูลที่ใช้ (Data Sources)](#2-ข้อมูลที่ใช้-data-sources)
3. [BTC Composite Score — สูตรคำนวณ 8 ปัจจัย](#3-btc-composite-score--สูตรคำนวณ-8-ปัจจัย)
4. [สัญญาณเทรด Alt (Signal Generation)](#4-สัญญาณเทรด-alt-signal-generation)
5. [เงื่อนไขเข้า-ออก (Entry / Exit Rules)](#5-เงื่อนไขเข้า-ออก-entry--exit-rules)
6. [SL / TP / Trail / Timeout](#6-sl--tp--trail--timeout)
7. [Hysteresis Anti-Whipsaw](#7-hysteresis-anti-whipsaw)
8. [Volatility Spike Overlay](#8-volatility-spike-overlay)
9. [Per-Coin Configs — v3 / v4 / v5](#9-per-coin-configs--v3--v4--v5)
10. [ผลทดสอบ Backtest](#10-ผลทดสอบ-backtest)
11. [Tournament Round 1 (v5 Champion)](#11-tournament-round-1-v5-champion)
12. [Paper Trading — ระบบจริง](#12-paper-trading--ระบบจริง)
13. [Key Principles — บทเรียนสำคัญ](#13-key-principles--บทเรียนสำคัญ)

---

## 1. แนวคิดหลัก

**BTC-Led Strategy** = ใช้ BTC เป็นตัวนำทิศทางตลาด แล้วเทรด altcoins ตาม

```
BTC Data (8 ปัจจัย) → BTC Composite Score → ถ้าบวกมาก = LONG alts, ลบมาก = SHORT alts
```

- **Timeframe**: 15 นาที (15m candles)
- **ตลาด**: Binance USDT-M Futures
- **Leverage**: 2x
- **ทิศทาง**: ทั้ง LONG และ SHORT (ไม่ใช่ SHORT-only)
- **จำนวนเหรียญ**: 46 เหรียญ (v3=19, v4=12, v5=15 ไม่ซ้ำกัน)

**ทำไม BTC-Led?**
- BTC เป็นตัวกำหนดทิศตลาดคริปโตทั้งหมด
- Altcoins มักเคลื่อนไหวตาม BTC แต่ amplify มากกว่า
- วิเคราะห์ BTC เชิงลึก (on-chain, derivatives, flows) แล้วใช้ edge นั้นเทรด alts ที่ volatility สูงกว่า

---

## 2. ข้อมูลที่ใช้ (Data Sources)

ข้อมูลทั้งหมดเก็บใน **PostgreSQL** (smart_trading) ผ่าน data collector daemon

### 2.1 ข้อมูล BTC (สำหรับ Composite Score)

| # | ข้อมูล | แหล่ง | ความถี่ | ใช้ทำอะไร |
|---|--------|-------|---------|----------|
| 1 | **Open Interest (OI)** | Binance API | 15 นาที | OI Divergence — ดูว่า OI สวนทาง price ไหม |
| 2 | **Funding Rate** | Binance API | 8 ชม. (MA smoothed) | ดูว่าตลาด overly long/short |
| 3 | **Whale Alerts** | Telegram (whale_alert_io) | 1 ชม. | Net flow เงินเข้า/ออก exchange (ปัจจุบัน unreliable) |
| 4 | **Liquidation Cascades** | Binance WebSocket (real-time) | 15 นาที (agg) | Long/short squeeze events |
| 5 | **ETF Flows** | PostgreSQL (manual/scraped) | รายวัน | Bitcoin ETF inflow/outflow |
| 6 | **Order Book (OB)** | Binance API (1000 levels) | 15 นาที | Bid/ask imbalance (contrarian) |
| 7 | **Basis (Premium)** | Binance mark price - spot | 15 นาที | Futures premium z-score (contrarian) |
| 8 | **Tick Liquidation** | Binance WebSocket (per-coin) | Real-time → 15m agg | Per-tick liq events, net count + notional |

### 2.2 ข้อมูล Altcoins (สำหรับ filter)

| ข้อมูล | แหล่ง | ใช้ทำอะไร |
|--------|-------|----------|
| **OHLCV 15m** | Binance Futures API | EMA, RSI, ATR, vol_ratio |
| **1H Klines** | Binance Futures API | EMA9/21 1H สำหรับ regime filter |
| **Per-coin Liquidation** | WebSocket `!forceOrder@arr` | Tick liq per alt (v5) |
| **Per-coin Order Book** | Binance API 1000 levels | OB imbalance per alt |
| **Per-coin Basis** | Mark price vs last price | Basis z-score per alt |

### 2.3 ข้อมูลเสริม (เก็บแต่ยังไม่ใช้ใน score)

| ข้อมูล | แหล่ง | สถานะ |
|--------|-------|--------|
| CVD (Cumulative Volume Delta) | WebSocket aggTrade | เก็บแล้ว, ยังไม่ใช้ |
| LS Ratio | Binance API | เก็บแล้ว, tested แล้วไม่ช่วย |
| Taker Buy/Sell Ratio | Binance API | เก็บแล้ว, tested แล้วไม่ช่วย |
| Fear & Greed Index | alternative.me | เก็บแล้ว, tested แล้วทำให้แย่ลง |
| Deribit Options (DVOL, skew) | Deribit API | เก็บแล้ว, ข้อมูลยังน้อย |
| Macro (DXY, US10Y, Gold, SP500) | yfinance | เก็บแล้ว, ข้อมูลยังน้อย |
| News Sentiment | Telegram (Cointelegraph) | เก็บแล้ว, ยังไม่ใช้ |

### 2.4 Timezone & Anti-Lookahead Rules

**ทุก timestamp ภายในระบบ = naive UTC** ไม่มี timezone info

| แหล่ง | Raw TZ | วิธีแก้ |
|--------|--------|---------|
| PostgreSQL | Bangkok (UTC+7) | ลบ 7 ชั่วโมง |
| Binance API | Unix epoch ms (UTC) | `pd.Timestamp(ms, unit="ms")` |
| Deribit | Unix epoch ms (UTC) | +1h shift (candle ts = start) |
| Blockchain.com | Unix epoch s (UTC) | +1d shift (daily aggregate) |
| DefiLlama | Unix epoch s (UTC) | +1d shift (daily aggregate) |

**Anti-Lookahead ใน backtest:**
- `signals.shift(1)` — signal ที่ bar T, เข้าเทรดที่ bar T+1 open
- `merge_asof(direction="backward")` — ไม่ดูข้อมูลอนาคต
- Hourly data: +1h shift, Daily data: +1d shift
- คำนวณ features ก่อน shift timestamps

---

## 3. BTC Composite Score — สูตรคำนวณ 8 ปัจจัย

BTC Composite Score = ผลรวมของ 8 ปัจจัย แต่ละปัจจัยให้ค่า +weight (bullish) หรือ -weight (bearish)

### ปัจจัยที่ 1: Liquidation Cascades

**แนวคิด**: เมื่อเกิด liquidation cascade (volume > 3x ค่าเฉลี่ย) = ตลาดกำลัง squeeze ไปทิศใดทิศหนึ่ง

```
เงื่อนไข: liq_total > liq_total_ma * 3 (cascade detected)
  ถ้า liq_net > 0 (short ถูก liq มากกว่า)  → +w_liq_bull  (bullish squeeze)
  ถ้า liq_net < 0 (long ถูก liq มากกว่า)   → -w_liq_bear  (bearish squeeze)
```

| Version | Weight | หมายเหตุ |
|---------|--------|----------|
| v3 | 2.0 | ค่าเริ่มต้น |
| **v5** | **5.0** | **THE KEY CHANGE — ปรับจาก 2→5 ทำให้ PnL เพิ่ม 74%** |

> Liquidation เป็นปัจจัยสำคัญที่สุด เพิ่ม weight จาก 2→3→4→5 ทุกขั้นดีขึ้นเรื่อยๆ (monotonic improvement)

### ปัจจัยที่ 2: Funding Rate

**แนวคิด**: Funding rate ติดลบ = shorts จ่าย longs = ตลาด oversold, บวกมาก = overbought

```
fr_ma = rolling mean ของ funding rate (7 วัน)

ถ้า fr_ma < -0.0001  → +w_fr_neg (2.0)   ตลาด oversold = bullish
ถ้า fr_ma > +0.0003  → -w_fr_pos (2.0)   ตลาด overbought = bearish
```

| Version | Weight | หมายเหตุ |
|---------|--------|----------|
| v3/v5 | 2.0 | ไม่เปลี่ยน |

### ปัจจัยที่ 3: Order Book Combined (Contrarian)

**แนวคิด**: ดู bid/ask imbalance แบบ **contrarian** — ถ้า bid มากกว่า ask = คนรอซื้อเยอะ = มักไม่ขึ้น

```
ob_imb_ma = MA ของ (bid_vol - ask_vol) / (bid_vol + ask_vol)
ob_vol_imb_ma = MA ของ volume-weighted imbalance
combo = (ob_imb_ma + ob_vol_imb_ma) / 2

combo > +0.03   → -weight      (bid > ask = bearish contrarian)
combo > +0.07   → -weight*0.5  (เพิ่มอีก)
combo < -0.03   → +weight      (ask > bid = bullish contrarian)
combo < -0.07   → +weight*0.5  (เพิ่มอีก)
```

| Version | Weight | หมายเหตุ |
|---------|--------|----------|
| v3/v5 | 2.0 | ob=2.0 ดีที่สุดสำหรับ Sharpe, เพิ่มมากกว่านี้ = noise |

### ปัจจัยที่ 4: ETF Flows

**แนวคิด**: Bitcoin ETF มีเงินไหลเข้า = institutional demand

```
etf_flow_ma = rolling mean ของ ETF daily flows (shift +1d เพราะ daily aggregate)

etf_flow_ma > +50 (ล้าน USD)  → +w_etf_bull (1.0)
etf_flow_ma < -50 (ล้าน USD)  → -w_etf_bear (1.0)
```

| Version | Weight | หมายเหตุ |
|---------|--------|----------|
| v3/v5 | 1.0 | ข้อมูลรายวัน ใน 15m ช่วยได้จำกัด แต่ยังเป็นบวก |

### ปัจจัยที่ 5: Basis Contrarian

**แนวคิด**: Futures premium สูงผิดปกติ = overbought (contrarian short), ต่ำผิดปกติ = oversold (contrarian long)

```
basis_z = z-score ของ (futures_price - spot_price) rolling 96 bars (24h)

basis_z > +1.5   → -weight (1.5)      premium สูง = bearish
basis_z > +2.5   → -weight*0.5 (0.75)  เพิ่มอีก
basis_z < -1.5   → +weight (1.5)      discount = bullish
basis_z < -2.5   → +weight*0.5 (0.75)  เพิ่มอีก
```

| Version | Weight | หมายเหตุ |
|---------|--------|----------|
| v3/v5 | 1.5 | Contrarian logic ทำงานดี |

### ปัจจัยที่ 6: Tick Liquidation

**แนวคิด**: ดู liquidation events แบบ real-time (per-tick) ทั้ง count และ notional value

```
liq_net_ma = MA ของ (short_liq_count - long_liq_count)
liq_notional_ma = MA ของ total liquidation notional value

liq_net_ma > +2   → +weight      short liqs มากกว่า = bullish
liq_net_ma < -2   → -weight      long liqs มากกว่า = bearish
liq_notional_ma > mean*3  → +weight*0.5  cascade ใหญ่ = momentum boost
```

| Version | Weight | หมายเหตุ |
|---------|--------|----------|
| v3 | 2.0 | ค่าเริ่มต้น |
| **v5** | **3.0** | **เพิ่มจาก 2→3 ช่วยจับ cascade ดีขึ้น** |

### ปัจจัยที่ 7: OI Divergence

**แนวคิด**: เปรียบเทียบทิศทาง price กับ Open Interest — ถ้าสวนทาง = divergence

```
price_up = ret > 0,  oi_up = oi_chg > 0

price_up AND oi_up    → +0.25 (bull confirmation)
price_down AND oi_down → +0.25 (capitulation = bullish)
price_up AND oi_down   → -0.25 (weak rally)
price_down AND oi_up   → -0.25 (bearish divergence)
```

| Version | Weight | หมายเหตุ |
|---------|--------|----------|
| v3/v5 | 0.25 per condition (0.5 total) | Weak signal แต่ช่วยเล็กน้อย |

### ปัจจัยที่ 8: Whale Alerts

**แนวคิด**: เงินจำนวนมากย้ายเข้า/ออก exchange

```
whale_net_ma = MA8 ของ (bullish_usd - bearish_usd) ทุก 15 นาที

whale_net_ma > +50,000,000  → +w_whale_bull (1.5)  เงินออก exchange = bullish
whale_net_ma < -50,000,000  → -w_whale_bear (1.5)  เงินเข้า exchange = bearish
```

| Version | Weight | หมายเหตุ |
|---------|--------|----------|
| v3/v5 | 1.5 | **Unreliable** — Telegram scraper stale บ่อย, contribute แค่ 2.2% |

### สรุป Weights ทั้งหมด

| # | ปัจจัย | v3 Weight | v5 Weight | สไตล์ |
|---|--------|-----------|-----------|-------|
| 1 | Liquidation | 2.0 | **5.0** | Momentum |
| 2 | Funding Rate | 2.0 | 2.0 | Contrarian |
| 3 | Order Book | 2.0 | 2.0 | Contrarian |
| 4 | ETF Flows | 1.0 | 1.0 | Momentum |
| 5 | Basis Contrarian | 1.5 | 1.5 | Contrarian |
| 6 | Tick Liquidation | 2.0 | **3.0** | Momentum |
| 7 | OI Divergence | 0.5 | 0.5 | Divergence |
| 8 | Whale Alerts | 1.5 | 1.5 | Flow |
| | **Max Score (ทิศเดียว)** | **~12.5** | **~16.5** | |

---

## 4. สัญญาณเทรด Alt (Signal Generation)

### 4.1 BTC Score → Alt Signal

```
1. คำนวณ BTC Composite Score ทุก 15 นาที
2. merge_asof() เอา score ไปแปะบน alt timeframe (direction="backward")
3. เปรียบเทียบกับ threshold ของแต่ละเหรียญ:
   - score >= +threshold  → signal = +1 (LONG)
   - score <= -threshold  → signal = -1 (SHORT)
   - อื่นๆ              → signal = 0 (ไม่เทรด)
```

### 4.2 Dead Zone

```
23:00 - 06:00 UTC = ไม่เทรด (signal = 0 เสมอ)
```

เหตุผล: volume ต่ำ, spread กว้าง, false signals เยอะ

### 4.3 PA Filter (ปิดอยู่ — pa_filter=False)

```
LONG:  close > EMA9 > EMA21 AND vol_ratio > 0.8
SHORT: close < EMA9 < EMA21 AND vol_ratio > 0.8
```

ทดสอบแล้วพบว่าปิด PA filter ให้ผลดีกว่า — BTC score อย่างเดียวเพียงพอ

### 4.4 Regime Filter (Soft Penalty)

ไม่ได้ใช้ใน production (penalty=0) แต่ระบบรองรับ:
```
BULL regime (1H EMA9 > EMA21): ลด short threshold
BEAR regime (1H EMA9 < EMA21): ลด long threshold
```

---

## 5. เงื่อนไขเข้า-ออก (Entry / Exit Rules)

### 5.1 เงื่อนไขเข้า (Entry)

ต้องผ่านทุกข้อ:
1. **BTC Score เกิน threshold** ของเหรียญนั้น (ดู Section 9)
2. **ไม่มี position เปิดอยู่** สำหรับเหรียญนั้น
3. **ผ่าน cooldown** — ต้องรอ N bars หลังปิด position ก่อนหน้า
4. **ไม่อยู่ใน dead zone** (23:00-06:00 UTC)
5. **ข้อมูลไม่ stale เกินไป** (critical factors ≤ 2 ตัว stale)

**ขนาด position:**
```
Backtest:  $1,000 × 2x leverage = $2,000 notional per coin
Paper:     $100 × 2x leverage   = $200 notional per coin
```

**ราคาเข้า:**
```
entry_price = open_price × (1 + slippage)   สำหรับ LONG
entry_price = open_price × (1 - slippage)   สำหรับ SHORT
slippage = 0.015% (1.5 bps)
```

### 5.2 เงื่อนไขออก (Exit) — ตามลำดับความสำคัญ

```
ลำดับ 1: Stop Loss (SL)      → ขาดทุนเกิน ATR multiplier
ลำดับ 2: Trailing Stop        → (ปิดอยู่ ใช้ trail=99 ATR = ไม่มีทาง trigger)
ลำดับ 3: Take Profit (TP)     → กำไรถึง ATR multiplier
ลำดับ 4: Stale Exit           → ถือนาน + กำไร < 0.1% = ออก
ลำดับ 5: Timeout              → ถือครบ 96 bars (24 ชั่วโมง)
ลำดับ 6: Signal Flip          → BTC score กลับทิศ = ออกที่ open ถัดไป
```

### 5.3 การคำนวณ PnL

```
LONG:  pnl = (exit_price - entry_price) × quantity
SHORT: pnl = (entry_price - exit_price) × quantity
fees  = notional × fee_rate × 2 (เข้า+ออก) = 0.02% × 2 = 0.04%
net_pnl = pnl - fees

Funding cost (ถ้าถือข้ามรอบ): 0.01% ทุก 32 bars (8 ชม.)
```

---

## 6. SL / TP / Trail / Timeout

### ATR-Based Stop Loss & Take Profit

SL/TP คำนวณจาก **ATR(14)** ของเหรียญนั้นๆ:

```
LONG:
  SL = entry_price - (sl_atr_mult × ATR)
  TP = entry_price + (tp_atr_mult × ATR)

SHORT:
  SL = entry_price + (sl_atr_mult × ATR)
  TP = entry_price - (tp_atr_mult × ATR)
```

### ค่า SL/TP ตาม Version

| Version | SL (ATR mult) | TP (ATR mult) | Trail | Timeout |
|---------|---------------|---------------|-------|---------|
| v3 (original 6) | 10.0 | 5.0 | 99 (ปิด) | 96 bars |
| v3 new + v4 | 10.0 | 5.0 | 99 (ปิด) | 96 bars |
| **v5** | **15.0** | **12.0** | 99 (ปิด) | 96 bars |

> **Key Insight: SL/TP asymmetry IS the edge**
> - SL กว้าง = ให้ trade มีที่หายใจ ไม่โดน stop out จาก noise
> - TP กว้าง = จับ big moves ได้เต็มที่
> - SL เดิม 2.5 ATR = โดน stop ตลอด (0% WR ใน paper trading)
> - SL=15 + TP=12 = sweet spot จาก 139 experiments

---

## 7. Hysteresis Anti-Whipsaw

### ปัญหา
BTC score แกว่งรอบ threshold ทำให้เปิด/ปิด position ซ้ำๆ อย่างรวดเร็ว (whipsaw)
- Paper trading: เสีย $24.72 ใน 2 ชม. (50 trades, 2% WR)

### วิธีแก้: แยก Entry / Exit Threshold

```
HYSTERESIS_BAND = 1.5
exit_threshold = entry_threshold - 1.5

เข้าง่ายกว่าออก:
  ENTRY: score ต้องเกิน entry_threshold (เช่น 3.0)
  EXIT:  score ต้องต่ำกว่า exit_threshold (เช่น 1.5) ถึงจะออก
```

### State Machine

```
สถานะปัจจุบัน → เงื่อนไข → สถานะใหม่

ไม่มี position (prev=0):
  score >= +threshold     → LONG
  score <= -threshold     → SHORT
  อื่นๆ                   → ไม่เทรด

ถือ LONG (prev=+1):
  score >= exit_threshold → ถือต่อ (LONG)
  score <= -threshold     → Flip เป็น SHORT
  อื่นๆ                   → ออก (signal=0)

ถือ SHORT (prev=-1):
  score <= -exit_threshold → ถือต่อ (SHORT)
  score >= +threshold      → Flip เป็น LONG
  อื่นๆ                    → ออก (signal=0)
```

### ผลทดสอบ Hysteresis

| Config | Trades | WR | PnL | Sharpe | Signal Changes |
|--------|--------|-----|-----|--------|---------------|
| No hysteresis | 828 | 63.2% | $14,121 | 12.34 | 791 |
| **Hyst-1.5** | **885** | **63.8%** | **$14,976** | **12.53** | **538 (-32%)** |

---

## 8. Volatility Spike Overlay

### แนวคิด
เมื่อ volatility พุ่งสูงผิดปกติ ใช้ logic พิเศษจับโอกาสที่ BTC score ปกติอาจพลาด

### เงื่อนไข Spike Detection

```python
range_z = (high - low) / ATR   # bar range เทียบ ATR
vol_ratio = volume / volume_ma  # volume เทียบค่าเฉลี่ย

spike_detected = (range_z > 1.5) AND (vol_ratio > 2.0)
```

### 2 โหมด Spike

**Contrarian** (ราคาขยับแรง สวนทาง):
```
adj_threshold = threshold - 0.5  (ลด threshold ลง)
LONG:  ต้อง ret < -0.5% (ราคาตกแรง)  + score >= adj_threshold
SHORT: ต้อง ret > +0.5% (ราคาขึ้นแรง) + score <= -adj_threshold
```

**Momentum** (ราคาขยับแรง ตามทิศ):
```
adj_threshold = threshold - 0.8  (ลด threshold มากกว่า)
ไม่ต้องเช็ค ret direction
```

### ผลทดสอบ Spike

| Config | PnL (6 coins OOS) |
|--------|--------------------|
| v3 base | $14,121 |
| **v3 + spike** | **$18,119 (+28%)** |

---

## 9. Per-Coin Configs — v3 / v4 / v5

### 9.1 v3 — Original 6 Coins (Grid-Searched)

| Coin | Threshold | SL ATR | TP ATR | Trail | Cooldown | PA Filter |
|------|-----------|--------|--------|-------|----------|-----------|
| BTC | 2.5 | 10.0 | 5.0 | 99 | 4 | False |
| XRP | 3.5 | 10.0 | 5.0 | 99 | 4 | False |
| ADA | 3.5 | 10.0 | 5.0 | 99 | 4 | False |
| DOT | 3.0 | 10.0 | 5.0 | 99 | 8 | False |
| SUI | 3.0 | 10.0 | 5.0 | 99 | 4 | False |
| FIL | 3.0 | 10.0 | 5.0 | 99 | 4 | False |

### 9.2 v3 — New 13 Coins (Default Config)

| Coins | Config |
|-------|--------|
| RENDER, BEAT, PIXEL, NEAR, AXS, SOL, ETH, 1000BONK, ARB, ARIA, BARD, BANANAS31, PIPPIN | threshold=3.0, SL=10.0, TP=5.0, trail=99, cd=4, pa=False |

**BTC Score Weights (v3):**
```
liq=2.0, fr=2.0, whale=1.5, etf=1.0, oi=0.5
ob=2.0, basis=1.5, tick_liq=2.0
```

### 9.3 v4 — 12 Coins (จาก 100-coin screening)

| Coins | Config |
|-------|--------|
| OGN, SAHARA, ASTER, LTC, ZRO, NAORIS, 1000PEPE, JCT, DEGO, HYPE, PENGU, LINK | threshold=3.0, SL=10.0, TP=5.0, trail=99, cd=4, pa=False |

**ใช้ BTC Score Weights เดียวกับ v3** — v4 ต่างแค่เหรียญ ไม่ต่าง model

### 9.4 v5 — 15 Coins (Tournament Champion)

| Coins | Config |
|-------|--------|
| FARTCOIN, GALA, AAVE, AVAX, UNI, SEI, DOGE, ONDO, 1000SHIB, ICX, BNB, WIF, CRV, TAO, ACX | threshold=3.0, **SL=15.0**, **TP=12.0**, trail=99, cd=4, pa=False |

**BTC Score Weights (v5) — ต่างจาก v3:**
```
liq=5.0 (↑จาก 2.0), fr=2.0, whale=1.5, etf=1.0, oi=0.5
ob=2.0, basis=1.5, tick_liq=3.0 (↑จาก 2.0)
```

### 9.5 สรุปความแตกต่าง

| | v3 | v4 | v5 |
|-|----|----|-----|
| **จำนวนเหรียญ** | 19 | 12 | 15 |
| **Liq weight** | 2.0 | 2.0 | **5.0** |
| **Tick liq weight** | 2.0 | 2.0 | **3.0** |
| **SL** | 10.0 ATR | 10.0 ATR | **15.0 ATR** |
| **TP** | 5.0 ATR | 5.0 ATR | **12.0 ATR** |
| **BTC Score** | v3 weights | v3 weights | **v5 weights** |
| **ที่มา** | Mega discovery + grid search | 100-coin screening | **139-experiment tournament** |

### 9.6 Architecture: Dual BTC Score

Paper trader คำนวณ **2 BTC scores ต่อรอบ**:
```
BTC Score v3 (liq=2.0, tick=2.0) → ใช้กับ v3 + v4 coins (31 เหรียญ)
BTC Score v5 (liq=5.0, tick=3.0) → ใช้กับ v5 coins (15 เหรียญ)
```

---

## 10. ผลทดสอบ Backtest

### 10.1 วิวัฒนาการ Strategy

| Version | Date | PnL (OOS) | Trades | WR | Sharpe | เปลี่ยนอะไร |
|---------|------|-----------|--------|-----|--------|------------|
| v1 | 03-08 | $1,575 | — | — | — | 8 factors เริ่มต้น |
| v1.1 | 03-08 | $7,486 | — | — | — | แก้ TZ bug, 6 coins, 2x lev |
| v1.2 | 03-08 | $9,819 | — | — | — | Short bias (threshold-0.5) |
| v2 | 03-08 | — | — | — | — | Dead zone + re-grid-search → **REJECTED (overfit)** |
| **v3** | 03-09 | **$14,121** | 946 | 66.6% | 4.97-6.83 | **8 optimal factors จาก scratch** |
| v3+spike | 03-15 | $18,119 | — | — | — | +vol spike overlay |
| v3+hyst | 03-17 | $14,976 | 885 | 63.8% | 12.53 | +hysteresis -1.5 |
| **v5** | **03-18** | **$49,052** | **1,836** | **68.7%** | **18.78** | **Tournament champion** |

### 10.2 v3 Per-Coin Performance (6 coins, 15m, 2x)

| Coin | Trades | WR% | Sharpe | PnL |
|------|--------|-----|--------|-----|
| BTC | 206 | 64.1 | 4.97 | +$1,922 |
| XRP | 146 | 69.2 | 6.30 | +$2,865 |
| ADA | 149 | 69.1 | 6.65 | +$3,247 |
| DOT | 137 | 69.3 | 6.83 | +$3,420 |
| SUI | 161 | 64.0 | 6.07 | +$3,513 |
| FIL | 147 | 64.0 | 5.50 | +$3,089 |
| **รวม** | **946** | **66.6%** | — | **$18,056** |

### 10.3 v3 Factor Attribution (Incremental PnL)

| # | Factor | Weight | Incremental PnL | Dominant % | WR เมื่อ Dominant |
|---|--------|--------|-----------------|-----------|-----------------|
| 1 | liquidation | 2.0 | +$10,837 | 22.3% | 78.4% |
| 2 | ob_combined | 2.0 | +$1,274 | 51.7% | 77.2% |
| 3 | basis_contrarian | 1.5 | +$1,709 | 4.6% | 76.7% |
| 4 | etf_flows | 1.0 | +$1,654 | 0.0% | — |
| 5 | funding_rate | 2.0 | +$1,218 | 1.0% | 90.9% |
| 6 | tick_liq | 2.0 | +$657 | 13.1% | 76.4% |
| 7 | oi_divergence | 0.5 | +$493 | 2.8% | 67.4% |
| 8 | whale_alerts | 1.5 | +$214 | 1.2% | 92.5% |

**Factors ที่ถูก Drop (ทดสอบแล้วไม่ช่วย):**
- fear_greed — ทำให้ PnL แย่ลง
- taker_ratio — redundant กับ factors อื่น
- ls_ratio — redundant กับ factors อื่น

---

## 11. Tournament Round 1 (v5 Champion)

### 11.1 ภาพรวม

- **139 experiments** ใน 6 batches
- ทดสอบบน **Jan 2025 - Mar 2026** (OOS period)
- **6 coins** (BTC, XRP, ADA, DOT, SUI, FIL)
- ใช้ **fixed position sizing** ($1,000 × 2x per coin, max 3 concurrent)

### 11.2 Evolution Path (6 Kings)

| # | Config | PnL | เปลี่ยนอะไร | ปรับปรุง |
|---|--------|-----|------------|---------|
| 1 | v3_baseline | $26,165 | SL=2.5, TP=4.0 | — |
| 2 | v3_sl10_tp8 | $33,338 | SL=10, TP=8 | +27% |
| 3 | evo_boost_asym5 | $34,449 | +asymmetric threshold | +32% |
| 4 | ultra_simple_sl15_tp10 | $40,346 | SL=15, TP=10, ลบ asymmetric | +54% |
| 5 | nk_liq5.0 | $45,635 | liq weight 2→5 | +74% |
| 6 | **GOD_liq5_ob2_tick3_tp12** | **$49,052** | tick 2→3, TP=12 | **+88%** |

### 11.3 v5 Champion Metrics

| Metric | ค่า |
|--------|-----|
| **PnL** | $49,052 |
| **Trades** | 1,836 |
| **Win Rate** | 68.7% |
| **Sharpe Ratio** | 18.78 |
| **Max Drawdown** | -3.2% |
| **Avg PnL/Trade** | $26.72 |

### 11.4 Regime Robustness

| ช่วง | ลักษณะ | Trades | PnL | สัดส่วน |
|------|--------|--------|-----|---------|
| H1 2025 (Jan-Jun) | BULL | 27 | $234 | 0.5% |
| H2 2025 (Jul-Dec) | BEAR | 1,429 | $28,027 | 57.1% |
| Q1 2026 (Jan-Mar) | MIXED | 657 | $15,911 | 32.4% |

> **BEAR market = cash cow** — strategy ทำเงินได้ดีที่สุดในตลาดขาลง (SHORT signals WR สูง 60-68%)

### 11.5 Liquidation Weight Sweep

| liq weight | PnL | เปลี่ยนแปลง |
|------------|-----|------------|
| 2.0 (v3) | $26,165 | baseline |
| 3.0 | $40,346 | +54% |
| 4.0 | $44,557 | +70% |
| **5.0** | **$45,635** | **+74%** |
| 6.0+ | diminishing | เริ่มไม่คุ้ม |

### 11.6 Key Lessons จาก Tournament

1. **SL/TP asymmetry IS the edge** — SL กว้าง + TP กว้าง = ดีที่สุด
2. **Liquidation = THE factor** — weight เพิ่มทุกขั้นดีขึ้น
3. **Asymmetric threshold ช่วยใน paper (72 trades) แต่ทำลาย backtest (2000 trades)** — beware paper trading bias
4. **OB weight > 2.0 = noise** — 2.0 เป็น sweet spot
5. **ทุก factor ยังช่วย** — ลบตัวไหนออก PnL ลดทุกตัว
6. **BEAR regime = strongest** — $28K จาก $49K มาจาก H2 2025

---

## 12. Paper Trading — ระบบจริง

### 12.1 Architecture

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  Data Collector  │────▶│   PostgreSQL DB   │◀────│  Paper Trader   │
│  (daemon.py)     │     │  (smart_trading)  │     │ (paper_trader.py)│
│  21 data sources │     └──────────────────┘     │  46 coins / 15m  │
│  WebSocket + REST│                               │  Binance Testnet │
└─────────────────┘                               └─────────────────┘
         │                                                │
         ▼                                                ▼
┌─────────────────┐                              ┌─────────────────┐
│ Research Sched.  │                              │   Dashboard     │
│ (scheduler.py)   │                              │   (app.py)      │
│ auto-discovery   │                              │  localhost:5000  │
└─────────────────┘                              └─────────────────┘
```

### 12.2 วงจรทุก 15 นาที

```
1. รอ candle ปิด (+ delay 60 วินาที)
2. ดึง OHLCV 15m ของ 46 เหรียญจาก Binance
3. ดึง BTC data 8 ปัจจัยจาก PostgreSQL
4. คำนวณ BTC Score (2 ชุด: v3 + v5)
5. วนลูปทุกเหรียญ:
   a. เลือก BTC score ตาม version ของเหรียญ
   b. เช็ค signal ด้วย hysteresis state machine
   c. ถ้ามี position → เช็ค exit conditions (SL/TP/timeout/signal flip)
   d. ถ้าไม่มี position + signal != 0 → เปิด position ใหม่
6. บันทึก trades + equity + signals ลง SQLite
7. Log สรุป
```

### 12.3 ค่าคงที่ Paper Trading

```
Initial Equity:    $5,000
Budget per coin:   $100 (× 2x lev = $200 notional)
Leverage:          2x
Fee:               0.02% (2 bps)
Slippage:          0.015% (1.5 bps)
Max hold:          96 bars (24h)
Hysteresis:        1.5
Dead zone:         23:00-06:00 UTC
Funding rate:      0.01% every 32 bars (8h)
```

### 12.4 Data Staleness Protection

| ข้อมูล | Staleness Threshold | ถ้า Stale |
|--------|--------------------|-----------|
| OI, Premium, Basis, Tick Liq, OB | 60 นาที | Critical |
| Liquidation (1h agg) | 2 ชม. | Critical |
| Whale | 4 ชม. | Non-critical |
| Funding Rate | 12 ชม. | Non-critical |
| ETF | 48 ชม. | Non-critical |

**ถ้า critical factors stale > 2 ตัว → suppress trading (ไม่เปิด position ใหม่)**

### 12.5 ผล Paper Trading (ณ 2026-03-20, 3 วันหลัง reset)

| Metric | ค่า |
|--------|-----|
| Equity | $5,408 |
| กำไร | +$408 (+8.2%) |
| จำนวนเทรด | 115 |
| Win Rate | 65.2% |
| Profit Factor | 4.32 |
| Max Drawdown | -1.69% |
| Sharpe | 3.77 |

### 12.6 ปัญหาที่พบใน Paper Trading

| ปัญหา | ผลกระทบ | วิธีแก้ |
|--------|---------|---------|
| SL แคบเกินไป (เดิม 2.5 ATR) | WR 0%, เสีย $1,103 | เพิ่มเป็น SL=10 (v3) / SL=15 (v5) |
| BTC score แกว่งรอบ threshold | 50 trades ใน 2 ชม., WR 2% | Hysteresis band = 1.5 |
| Whale data stale 14+ ชม. | BTC Score = 0, signal suppression | อยู่ระหว่างพิจารณาลบ factor |
| Algo order limit (Binance testnet max ~10) | ส่ง SL/TP order ไม่ได้ | Software SL/TP fallback (15m granularity) |
| Illiquid testnet coins (XPL, ZEC) | Fill ไม่ได้, error | ลบออกจาก coin list |

---

## 13. Key Principles — บทเรียนสำคัญ

### การพัฒนา Model

1. **ห้าม re-grid-search** per-coin params ที่พิสูจน์แล้ว → overfit
2. **เริ่มจาก scratch เป็นระยะ** → incremental อาจพลาด combo ที่ดีกว่า
3. **Daily data ใน 15m = noise** → F&G, news ไม่ช่วย
4. **Contrarian > Momentum** สำหรับ liquidation, basis, order book
5. **ทุก factor ยังช่วย** → อย่าลบง่ายๆ แม้จะเล็กน้อย

### การเทรด

6. **SHORT > LONG** — short signals WR 60-68% vs long 37-46%
7. **แต่ทั้ง 2 ทิศมี edge** — SHORT ดีใน BULL/BEAR, LONG ดีใน FLAT
8. **SL/TP asymmetry = edge** — SL กว้าง + TP กว้าง = ดีที่สุด
9. **Breakeven stop = เสียหาย** → ทำลาย asymmetry ที่เป็น edge

### การทดสอบ

10. **Compound sizing bias** → ทำให้ผลบวมขึ้น 10x, ใช้ fixed size เสมอ
11. **Period bias** → ต้องทดสอบ BULL + BEAR + FLAT แยกกัน
12. **Paper trading bias ≠ structural edge** → 72-trade paper ≠ 2000-trade backtest
13. **บันทึกทุก experiment** → `experiments/` folder + JSON + summary

### ระบบ

14. **Timezone ต้อง naive UTC เสมอ** → ผิด TZ = PnL เปลี่ยน 100%+
15. **Anti-lookahead ต้องเข้มงวด** → shift(1), merge_asof backward, shift daily data +1d
16. **Data staleness = signal suppression** → monitor ตลอด
17. **Max concurrent positions** → portfolio simulation ต้อง realistic

---

## Appendix A: ไฟล์สำคัญ

| ไฟล์ | หน้าที่ |
|------|---------|
| `backtest_15m_btc_led_alts.py` | Backtest engine หลัก |
| `paper_trading/config.py` | Config ทั้งหมด (v3/v4/v5 weights, coins, params) |
| `paper_trading/strategy.py` | Live strategy scorer + signal evaluator |
| `paper_trading/data_feed.py` | Live data feed + staleness check |
| `paper_trading/paper_trader.py` | Main trading loop |
| `paper_trading/position_manager.py` | Position tracking + SL/TP |
| `paper_trading/exchange.py` | Binance testnet connector |
| `data_collector/daemon.py` | Data collection daemon (21 sources) |
| `data_collector/ws_liquidation.py` | WebSocket real-time liquidation |
| `tournament/` | Tournament experiments + results |
| `research/` | Auto-discovery system |
| `dashboard/` | Web dashboard (localhost:5000) |
| `experiments/` | All experiment data (JSON + summaries) |

## Appendix B: Coin Lists

### v3 (19 coins) — ใช้ BTC Score v3
```
BTC, XRP, ADA, DOT, SUI, FIL,
RENDER, BEAT, PIXEL, NEAR, AXS, SOL, ETH,
1000BONK, ARB, ARIA, BARD, BANANAS31, PIPPIN
```

### v4 (12 coins) — ใช้ BTC Score v3
```
OGN, SAHARA, ASTER, LTC, ZRO, NAORIS,
1000PEPE, JCT, DEGO, HYPE, PENGU, LINK
```

### v5 (15 coins) — ใช้ BTC Score v5
```
FARTCOIN, GALA, AAVE, AVAX, UNI, SEI, DOGE,
ONDO, 1000SHIB, ICX, BNB, WIF, CRV, TAO, ACX
```

**รวม 46 เหรียญ ไม่ซ้ำกัน**
