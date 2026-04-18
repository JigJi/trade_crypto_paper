# Mission 030: Post-Surgery Health Check -- 4 วันหลังผ่าตัดใหญ่

**วันที่**: 2026-04-08
**ประเภท**: system_audit / performance_monitoring / paper_trading
**ความยาก**: hard (6 experiments, 2,092 trades, 75K signal_log, pre vs post comparison)
**สถานะ**: COMPLETED -- **CRITICAL WARNING** -- ผ่าตัดหยุดเลือด แต่ระบบแทบไม่เทรด

---

## แรงบันดาลใจ

ระบบผ่านการผ่าตัดใหญ่ 4 ครั้งในช่วง 04-03 ถึง 04-04:
1. **04-03**: Alt filter (FR+CVD+Liq) deployed + Trail stop widened (0.5->1.5 ATR)
2. **04-04**: v6 ถอดทั้งหมด (13/15 เหรียญ negative)
3. **04-04**: LONG disabled (WR 31%, -$402)
4. **04-04**: เหรียญตัดจาก 46 -> 13 (เอาเฉพาะ profitable)

**คำถาม**: 4 วันหลังผ่าตัด ระบบดีขึ้นไหม? สุขภาพเป็นอย่างไร?

---

## EXP1: Pre vs Post Surgery Overview

| Period | Trades | PnL | WR% | PnL/day | PF |
|--------|--------|-----|-----|---------|-----|
| PRE ทั้งหมด (17 วัน) | 1,995 | +$348 | 42.0% | $20.5 | -- |
| PRE เหรียญที่เหลือ | 602 | +$904 | 48.2% | $53.2 | -- |
| PRE เหรียญที่ถอด | 1,386 | **-$556** | 39.3% | -- | -- |
| **POST (4 วัน)** | **97** | **+$3** | **42.3%** | **$0.8** | -- |
| POST SHORT | 77 | -$8 | 48.1% | -- | -- |
| POST LONG* | 20 | +$12 | 20.0% | -- | -- |

*\*20 LONG trades = เข้าก่อน LONG ถูก disable หรือ exit หลัง disable*

### Key Findings:
- **เหรียญที่ถอดออกคือตัวร้ายจริง**: -$556 จาก 1,386 trades (WR 39.3%)
- **แต่ post-surgery แทบไม่เทรด**: 97 trades ใน 4 วัน = 24 trades/day (เดิม ~117/day)
- **PnL/day ดิ่ง**: $53.2/day -> $0.8/day (เหรียญที่เหลือเดิมทำ $53/day!)
- **ปัญหาไม่ใช่ quality แต่เป็น volume** -- ตัดเทรดเยอะเกินไป

---

## EXP2: Daily PnL Trend

| Date | Trades | PnL | WR% | CumPnL | Note |
|------|--------|-----|-----|---------|------|
| 03-18 | 77 | **+$694** | 75.3% | $685 | Best day ever |
| 03-23 | 218 | **-$339** | 26.1% | $458 | Worst choppy day |
| 03-28 | 212 | **-$276** | 25.5% | $610 | 2nd worst day |
| 04-01 | 165 | -$171 | 35.8% | $429 | Pre-surgery decline |
| **04-04** | **40** | **-$13** | **27.5%** | **$335** | **Surgery day** |
| 04-05 | 10 | +$16 | 50.0% | $351 | Recovery |
| 04-06 | 35 | -$6 | 45.7% | $345 | Flat |
| 04-07 | 12 | +$7 | 75.0% | $352 | Low volume |

**Post-surgery: 2 win days, 2 lose days** -- ไม่ขาดทุนหนักอีก แต่ก็ไม่ทำกำไร

### Pattern ที่เห็น:
- **Volume หายไป 80%**: จาก ~117 trades/day เหลือ ~24 trades/day
- **ไม่มีวัน massacre อีก** (ไม่มี -$200+ days) -- ผ่าตัดหยุดเลือดได้
- **แต่ไม่มีวัน big win เช่นกัน** -- revenue หายไปด้วย

---

## EXP3: Alt Filter -- บล็อก 78% ของ entries!

| Metric | Value |
|--------|-------|
| SKIP_ALT_FILTER ทั้งหมด | 611 |
| Entries ที่เปิดได้ (post-filter) | 174 |
| **Block rate** | **77.8%** |

### Alt filter blocks per day:
| Date | Blocked | Opened | Block% |
|------|---------|--------|--------|
| 04-03 | 309 | ~40 | ~89% |
| 04-04 | 71 | ~40 | ~64% |
| 04-05 | 34 | ~10 | ~77% |
| 04-06 | 95 | ~35 | ~73% |
| 04-07 | 102 | ~12 | ~89% |

### Top blocked coins:
| Coin | Blocks | Comment |
|------|--------|---------|
| AAVE | 70 | v5 star -- ถูกบล็อกเยอะสุด! |
| ETH | 51 | Major coin ถูกบล็อกเยอะ |
| BTC | 41 | แม้แต่ BTC ก็โดน |
| SUI | 34 | |
| AXS | 30 | |

### CRITICAL: Alt filter อาจเข้มเกินไป
- **Block rate 78%** = บล็อก 3 ใน 4 signals ที่ควรเปิด
- **AAVE ถูกบล็อกมากสุด** (70 ครั้ง) ทั้งที่มีแค่ 6 trades post-surgery
- ควรทบทวน threshold ของ alt filter -- อาจปรับให้ผ่อนลง

---

## EXP4: LONG Disabled -- ประหยัดน้อยกว่าคาด

| Metric | Value |
|--------|-------|
| LONG entries blocked | 258 |
| Pre-surgery LONG avg PnL (surviving coins) | -$0.08/trade |
| **Estimated savings** | **~$20** |

### Insight:
- LONG ที่เหรียญ surviving ไม่ได้ขาดทุนหนัก (avg -$0.08) -- เกือบ breakeven
- Savings จริงน้อย (~$20) เพราะ LONG ที่หนักๆ อยู่ในเหรียญที่ถอดไปแล้ว
- **LONG disabled ไม่ใช่ปัจจัยหลัก** ที่ทำให้ volume หาย

---

## EXP5: Per-Coin Health Post-Surgery (13 เหรียญ)

| Coin | Model | Trades | PnL | WR% | AvgPnL | Status |
|------|-------|--------|-----|-----|--------|--------|
| **PIXEL** | v3 | 8 | **+$32.2** | **62.5%** | $4.03 | STAR |
| **BEAT** | v3 | 7 | **+$17.9** | **71.4%** | $2.55 | STAR |
| ETH | v3 | 5 | +$2.3 | 40.0% | $0.46 | OK |
| SUI | v3 | 3 | +$1.5 | 66.7% | $0.49 | OK (low vol) |
| RENDER | v3 | 8 | +$1.2 | 50.0% | $0.15 | FLAT |
| 1000BONK | v3 | 6 | +$1.1 | 50.0% | $0.18 | FLAT |
| ADA | v3 | 4 | +$0.4 | 75.0% | $0.09 | OK (low vol) |
| XRP | v3 | 4 | -$0.2 | 50.0% | -$0.04 | FLAT |
| BTC | v3 | 9 | -$1.5 | 22.2% | -$0.17 | WEAK |
| SOL | v3 | 7 | -$2.4 | 28.6% | -$0.34 | WEAK |
| AAVE | v5 | 6 | -$2.6 | 16.7% | -$0.44 | WEAK |
| AXS | v3 | 7 | -$7.6 | 14.3% | -$1.09 | DANGER |
| **ARIA** | **v5** | **11** | **-$32.6** | **54.5%** | **-$2.96** | **COLLAPSED** |

### Shocking: ARIA collapsed!
- **Pre-surgery star (+$722) -> Post-surgery worst (-$32.6)**
- 11 trades, WR 54.5% แต่ avg loss ใหญ่กว่า avg win มาก
- v5 model ทั้ง 2 เหรียญ (ARIA, AAVE) กำลังแย่ -- อาจเป็น regime change

### สรุป:
- **7 profitable** (+$56.5) vs **6 unprofitable** (-$46.9) = net +$9.6
- **PIXEL + BEAT = hero ใหม่** ($50 จาก 15 trades)
- **BTC WR 22.2%** น่ากังวล -- flagship coin กำลังพัง

---

## EXP6: Exit Reason Evolution

| Exit Reason | PRE (surviving) | POST | Direction |
|-------------|-----------------|------|-----------|
| SIGNAL_FLIP | 46.0% ($-535) | 30.9% ($-41) | IMPROVED |
| TP | 20.4% ($+727) | 10.3% ($+55) | Less TP |
| **TRAIL** | **17.4%** ($+80) | **55.7%** ($-6) | **DOMINANT but losing** |
| SL | 4.7% ($-249) | 1.0% ($0) | Almost gone |

### Key Changes:
- **TRAIL dominates post-surgery (55.7%)** -- wider trail (1.5 ATR) = more TRAIL exits
- **แต่ TRAIL WR ลดจาก 55.2% เป็น 42.6%** -- trail ตอนนี้ lose มากกว่า win
- SIGNAL_FLIP ลดจาก 46% -> 31% (ดีขึ้น)
- SL แทบหายไป (1%) -- SL 10 ATR กว้างพอ
- TP ลดลงเยอะ (20.4% -> 10.3%) -- trades น้อยลง = TP trigger น้อยลง

---

## Diagnosis: ทำไม PnL ถึงดิ่ง?

### Root Causes (เรียงตามผลกระทบ):

| # | Cause | Impact | Evidence |
|---|-------|--------|----------|
| 1 | **Alt filter เข้มเกินไป** | -80% volume | Block rate 78%, AAVE โดนบล็อก 70 ครั้ง |
| 2 | **เหรียญน้อยเกินไป** | Diversification ต่ำ | 46 -> 13 เหรียญ, บางวันเทรดแค่ 10 ครั้ง |
| 3 | **ARIA regime change** | -$32.6 | จาก +$722 กลายเป็นตัวร้าย |
| 4 | **TRAIL WR ตก** | -$6.4 (was +$80) | 1.5 ATR trail อาจกว้างเกินใน low vol |
| 5 | **ตลาด sideways** | Low opportunity | 04-04 ถึง 04-07 = low vol period |

### Factor #1 สำคัญที่สุด: Alt Filter
- Alt filter block 78% ของ entries ที่ระบบจะเปิด
- ก่อนผ่าตัด surviving coins ทำ ~35 trades/day, หลังผ่าตัดเหลือ ~24/day
- alt filter อาจตัด trades ที่ดีออกไปด้วย (ไม่ใช่แค่ trades แย่ๆ)

---

## Recommendations

### Urgent (ทำทันที)
1. **Review alt filter threshold** -- block rate 78% สูงเกินไป. ลอง:
   - ปรับ threshold ให้ผ่อนลง (block ~40-50% แทน 78%)
   - หรือใช้ alt filter เฉพาะ LONG (ถ้า enable LONG กลับ)
   - เก็บ log ว่า blocked trades จะเป็น win/lose ถ้าเปิดจริง (shadow tracking)

2. **Monitor ARIA closely** -- ถ้า -$50 ให้ถอดออกจาก v5

3. **Monitor BTC** -- WR 22.2% ใน 4 วันน่ากังวล แต่ sample เล็ก (9 trades)

### Short-term (1 สัปดาห์)
4. **Shadow-track blocked trades** -- สร้าง "phantom trades" จาก SKIP_ALT_FILTER เพื่อวัดว่า filter ช่วยจริงไหม
5. **Check TRAIL width** -- 1.5 ATR trail WR 42.6% ควร A/B test กับ 1.0 ATR

### Medium-term (ก่อน live 04-25)
6. **เพิ่มเหรียญกลับ** ถ้า alt filter ผ่อนลง -- 13 เหรียญอาจน้อยเกินไป
7. **v5 model review** -- ARIA + AAVE ทั้งคู่แย่ post-surgery

---

## สรุป

**ผ่าตัดหยุดเลือดได้ แต่หยุดหายใจด้วย.** ระบบไม่ขาดทุนหนักอีก (ไม่มี -$200+ days) แต่ก็แทบไม่ทำกำไร เพราะเทรดน้อยเกินไป. Alt filter block 78% เป็นตัวการหลัก ต้อง review ก่อนที่จะตัดสินว่าผ่าตัดสำเร็จหรือล้มเหลว.

ตอนนี้ระบบอยู่ใน "ICU mode" -- ปลอดภัยแต่ไม่ productive. ต้องค่อยๆ ฟื้นฟูกลับ.
