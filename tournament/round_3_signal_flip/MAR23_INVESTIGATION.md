# Investigation: March 23, 2026 -- "Black Sunday"
## Paper Trading Lost $482 (-8.9%) in One Day

---

## Executive Summary

**3 root causes ซ้อนกัน** ทำให้ paper trading เสียเงิน $482 ในวันเดียว:

1. **Double Daemon Bug** -- daemon 2 ตัวรันพร้อมกัน ตั้งแต่ ~04:45 UTC, เห็น BTC คนละราคา ($1K ต่างกัน), คำนวณ score คนละค่า
2. **Trump-Iran Geopolitical Whipsaw** -- BTC ร่วง $68K จาก ultimatum ขู่อิหร่าน แล้ว pump $71.4K จากข่าวเลื่อนโจมตี 5 วัน ($400M+ liquidations ทั้ง long+short)
3. **v6 Binary Score Amplification** -- score กระโดด -4→+4 ข้ามทิศ ทำให้ SIGNAL_FLIP ยิง 168 trades (จาก 178 ทั้งหมด) WR 22.6%

---

## Timeline

### BTC Price & Score (from equity_curve)

| Time (UTC) | BTC Price | Score | Event |
|------------|-----------|-------|-------|
| 00:01 | $68,697 | +2.0 | Day opens, many positions held from yesterday |
| 02:01 | $68,423 | 0.0 | Score drops to zero |
| 03:16 | $68,247 | -2.0 | Score turns negative |
| 04:31 | $67,698 | -0.2 | BTC hits session low area |
| **04:46** | **$67,709 / $68,262** | **-2.0 / +2.0** | **DOUBLE DAEMON STARTS -- two instances, $553 price gap** |
| 05:16 | $67,624 / $68,676 | -2.0 / +2.0 | Gap widens to $1,052 |
| 05:47 | $68,254 | -4.0 | Daemon #2 sees deep negative |
| 06:31 | $68,267 / $68,645 | -4.0 / 0.0 | Complete contradiction in scores |
| 07:01 | $67,821 / $68,026 | -4.2 / -3.8 | Both daemons at least agree on direction |
| **11:19** | **$68,604** | **+4.2** | **TRUMP POSTPONES IRAN STRIKES -- BTC pumps** |
| **11:31** | **$70,964** | **+3.8** | **+$2,359 in 12 minutes -- shorts destroyed** |
| 11:19 | -- | -- | **EQUITY DROPS $331 IN ONE TICK** (biggest single loss) |
| 12:01 | $70,535 | +4.2 | Daemon merge? Only single entries again |
| 14:01 | $71,207 | +4.2 | BTC hits session high |
| 16:01 | $70,581 | -1.8 | Score turns negative again |
| 17:32 | $70,599 | -5.0 | Deep negative score |
| 17:47 | $70,547 | -6.5 | Score hits day's extreme |
| 20:04 | $70,910 | +2.2 | Score recovers to positive |
| 23:46 | $70,658 | -4.0 | Day ends negative |

**BTC Day: $68,697 -> $70,658 (+2.9%)** | **Low $67,551 -> High $71,613** (range $4,063, ~5.9%)

### Score Sign Transitions: **17 times** in 24 hours

---

## Root Cause #1: Double Daemon (04:45 - ~11:30 UTC)

**Evidence**: Equity curve table shows **duplicate entries at same timestamp** with different BTC prices and scores:

```
04:46  BTC=67,709  score=-2.0   (Daemon A)
04:46  BTC=68,262  score=+2.0   (Daemon B)  -- $553 gap!

05:01  BTC=67,819  score=-2.0   (Daemon A)
05:01  BTC=68,267  score=+2.0   (Daemon B)

05:16  BTC=67,624  score=-2.0   (Daemon A)
05:16  BTC=68,676  score=+2.0   (Daemon B)  -- $1,052 gap!

06:31  BTC=68,267  score=-4.0   (Daemon A)
06:31  BTC=68,645  score=+0.0   (Daemon B)
```

**Impact**: ทั้งสอง daemon trade ซ้อนกัน:
- Daemon A เห็น score = -2.0 -> SHORT
- Daemon B เห็น score = +2.0 -> LONG
- ผลลัพธ์: ทุก bar มี SIGNAL_FLIP เพราะ position direction ขัดกับอีก daemon

**Why different prices?** Daemon A ใช้ cached/stale OHLCV data ขณะที่ Daemon B fetch ใหม่ (different candle boundary timing)

---

## Root Cause #2: Trump-Iran Geopolitical Whipsaw

### External Events (verified from internet)

| Date | Event | BTC Impact |
|------|-------|------------|
| Mar 22 (Sat) | Trump: 48h ultimatum to Iran, threatens to "obliterate" power plants | BTC drops to $68,000 (panic selling) |
| Mar 23 (Sun) AM | CME gap near $70K, BTC tests $67,500 lows | Short sellers pile in |
| **Mar 23 ~11:00 UTC** | **Trump announces postponing strikes 5 days after "productive talks"** | **BTC surges +$3K in 15 min** |
| Mar 23 PM | Relief rally continues to $71,613 | Short squeeze ($100M+ shorts liquidated) |
| Mar 23 evening | Profit taking, BTC settles $70,500-$70,700 | Score turns negative again |

### Market Impact
- **$400M+ liquidations** in 24 hours (both directions)
- **$300M long liquidations** during the drop to $67.5K
- **$100M+ short liquidations** during the $68K -> $71.4K surge
- Fear & Greed Index: **10** (Extreme Fear -- 46 days straight)

### Why Our System Got Hurt
**Two-way liquidation cascade** = exactly what v6 detects, but in BOTH directions:
1. Long liquidations triggered score -4 to -6 -> SHORT signals
2. Short liquidations triggered score +2 to +4 -> LONG signals
3. Both happened within same day -> score oscillated 17 times between pos/neg

---

## Root Cause #3: v6 Binary Score Amplification

v6 uses **liquidation-only scoring** (cascade 1.1x threshold):
- When liq cascade triggers: score jumps by +/-8 (or +/-16 for extreme)
- When it stops: score drops back to 0-2 range immediately
- This creates **binary behavior**: ON (+8) vs OFF (0) -- no gradual transition

With hysteresis band = 1.5 (old setting), the score easily jumped OVER the band:
- Entry threshold: 2.5, Exit threshold: 1.0
- Score goes from -4 -> +4: crosses BOTH entry AND exit threshold in one bar
- Result: forced entry in opposite direction = SIGNAL_FLIP

---

## Trade Statistics (Mar 23)

| Metric | Value |
|--------|-------|
| Total trades | 178 |
| SIGNAL_FLIP trades | 168 (94.4%) |
| SIGNAL_FLIP WR | 22.6% |
| SIGNAL_FLIP PnL | -$335.83 |
| TP trades | 4 (WR 75%, +$130.78) |
| SL trades | 4 (WR 75%, +$1.37) |
| SL/TP trades | 2 (WR 50%, -$13.34) |
| **Net PnL** | **-$482.11** |
| Equity: start -> end | $5,425 -> $4,943 |

### Worst Coins (all from SIGNAL_FLIP)
| Coin | Trades | Flips | PnL |
|------|--------|-------|-----|
| OGN | 6 | 6 | -$27.38 |
| ASTER | 5 | 5 | -$26.33 |
| PENGU | 6 | 6 | -$26.12 |
| FARTCOIN | 3 | 2 | -$23.30 |
| HYPE | 5 | 5 | -$21.25 |

### Best Coins (survived or profited)
| Coin | Trades | Flips | PnL |
|------|--------|-------|-----|
| NAORIS | 6 | 5 | +$93.84 (TP hit!) |
| BANANAS31 | 6 | 6 | +$71.84 |
| ARIA | 4 | 4 | +$40.77 |

### Worst Hours
| Hour | Trades | Flips | PnL | Event |
|------|--------|-------|-----|-------|
| 07:00 | 23 | 23 | -$191.98 | Double daemon peak conflict |
| 12:00 | 27 | 27 | -$38.65 | Post-pump oscillation |
| 06:00 | 12 | 11 | -$37.02 | Double daemon warm-up |
| 11:00 | 35 | 33 | -$28.98 | Trump announcement shock |

### Single Worst Moment
**11:19 UTC**: Equity dropped **-$331.32** in one 15-min bar when BTC pumped $68K -> $71K.
Multiple SHORT positions (opened on score -4 to -6) got destroyed by $3K upward move.

---

## Double Daemon Root Cause Analysis

### Why Two Daemons?
Likely: daemon restarted without killing the old process (PID lock bug fixed in previous session, but perhaps not deployed yet on Mar 23)

### Price Discrepancy Mechanism
- Daemon A: used partially cached 15m candle data (fetched at different phase of candle)
- Daemon B: fetched fresh data at exact same time but from different candle boundary
- Result: $500-$1,000 BTC price difference -> completely different score calculations

### Compounding Effect
Each daemon independently:
1. Evaluates signal (different score -> different direction)
2. Opens/closes positions (conflicting orders on same coins)
3. Records to same DB (interleaved equity curve entries)

---

## Lessons Learned

### 1. Double Daemon Prevention
PID lock was already implemented but likely not deployed. **Verify daemon singleton before every restart.**

### 2. v6 Score is Too Binary for SIGNAL_FLIP
Backtest: SIGNAL_FLIP is profitable because entry/exit prices are deterministic (15m bar)
Paper trading: SIGNAL_FLIP is destructive because:
- Execution delay (seconds, not instant)
- Slippage on volatile moves
- Score can flip within execution window

### 3. Geopolitical Events = Two-Way Liquidation = Deadly for Flip
Any event that causes BOTH long AND short liquidations will make v6 score oscillate.
Examples: war announcements, central bank surprises, stablecoin depegs.

### 4. The $331 Drop Was NOT from SIGNAL_FLIP
The single biggest loss was from **holding SHORT positions when BTC pumped $3K in 15 min**.
This is a SL issue (SL=25 ATR was too wide for this sudden move), not a flip issue.

---

## Actions Taken (same day)

1. **Per-model FLIP_CONFIG deployed**: v6 gets exit_only + hyst=3.0 + cd_extra=4
2. **v3/v5 keep original settings**: reverse + hyst=1.5 (they weren't as badly affected)
3. **Tournament R3 validated**: champion settings give +12.1% on v6 backtest

## Recommendations (for future sessions)

1. **Add daemon health check**: if two equity_curve entries at same timestamp -> alert
2. **Consider max loss circuit breaker**: if equity drops > 5% intraday -> pause all trading
3. **Geopolitical event detector**: if ATR spikes > 3x normal -> reduce position size or pause
4. **Test v6 with hyst=5.0 or 6.0**: score range is -16 to +16, band of 3.0 might still be too small
