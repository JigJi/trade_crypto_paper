# Mission 024: ob_combined Surgery — ลด/เอา Order Book ออกเพื่อแก้ SIGNAL_FLIP

**วันที่**: 2026-04-01
**ประเภท**: factor_surgery / signal_flip
**ความยาก**: hard (6 experiments, 6 coins OOS)
**สถานะ**: COMPLETED

---

## แรงบันดาลใจ

ต่อยอดจาก:
- **Mission 023**: ob_combined flip ก่อนใน 49% ของ SIGNAL_FLIP trades
- **Mission 021**: Chop ตอน entry ไม่ทำนาย FLIP → ปัญหาอยู่ระหว่างเทรด
- **Mission 022**: FLIP เป็น sudden event จาก factor instability

**คำถาม**: ถ้า ob_combined เป็นตัวการหลักของ FLIP — ลดหรือเอาออกจะช่วยได้ไหม?

**สมมติฐาน**: ลด ob_combined weight → score เสถียรขึ้น → FLIP น้อยลง → PnL สูงขึ้น

---

## Experiments

| # | Scenario | ob_weight | Threshold | MA | Description |
|---|----------|-----------|-----------|-----|------------|
| 1 | Baseline | 2.0 | 0.03/0.07 | 12 | ค่าปัจจุบัน |
| 2 | ob=1.0 | 1.0 | 0.03/0.07 | 12 | ลดครึ่ง |
| 3 | ob=0.5 | 0.5 | 0.03/0.07 | 12 | ลด 75% |
| 4 | ob=0.0 | 0.0 | - | - | เอาออกเลย |
| 5 | Wider thr | 2.0 | 0.05/0.10 | 12 | threshold สูงขึ้น (filter noise) |
| 6 | Smooth MA24 | 2.0 | 0.03/0.07 | 24 | MA ยาวขึ้น (smooth) |

---

## ผลลัพธ์

| Experiment                | Trades |   WR% |      PnL |     ΔPNL |  FLIP% |  FLIP PnL |  Non-FLIP PnL |
|---------------------------|--------|-------|----------|----------|--------|-----------|---------------|
| EXP1_baseline_ob2.0       |   3547 | 77.4% | $  11753 | +      0 |   4.6% | $   -4498 | $       16252 |
| EXP2_ob1.0                |   2440 | 78.5% | $  11121 |    -632 |   2.5% | $   -1413 | $       12534 |
| EXP3_ob0.5                |   1894 | 80.0% | $   9402 |   -2351 |   1.9% | $    -902 | $       10304 |
| EXP4_ob0.0_removed        |   1608 | 79.4% | $   7961 |   -3792 |   1.6% | $    -490 | $        8451 |
| EXP5_ob2.0_wider_thr      |   2729 | 78.5% | $  11064 |    -689 |   3.5% | $   -2481 | $       13544 |
| EXP6_ob2.0_smooth_MA24    |   3494 | 77.9% | $  11790 | +     37 |   3.8% | $   -3887 | $       15677 |

### Direction Breakdown

| Experiment                | L Trades |  L WR% |    L PnL | S Trades |  S WR% |    S PnL |
|---------------------------|----------|--------|----------|----------|--------|----------|
| EXP1_baseline_ob2.0       |     1518 |  77.5% | $   5396 |     2029 |  77.3% | $   6358 |
| EXP2_ob1.0                |     1026 |  79.0% | $   4443 |     1414 |  78.1% | $   6678 |
| EXP3_ob0.5                |      789 |  80.6% | $   3598 |     1105 |  79.6% | $   5804 |
| EXP4_ob0.0_removed        |      694 |  79.0% | $   2883 |      914 |  79.6% | $   5078 |
| EXP5_ob2.0_wider_thr      |     1175 |  79.3% | $   4876 |     1554 |  77.8% | $   6188 |
| EXP6_ob2.0_smooth_MA24    |     1497 |  77.2% | $   5226 |     1997 |  78.5% | $   6564 |

---

## วิเคราะห์เชิงลึก

### Key Finding: ob_combined = Trade Generator + FLIP Generator

ข้อมูลแสดง **trade-off ที่ชัดเจน**:

| เอา OB ออก | ผลดี | ผลเสีย |
|------------|------|--------|
| FLIP ลดจาก 163 → 26 เทรด (-84%) | FLIP PnL ดีขึ้น $4,008 | เสียเทรดดีๆ 1,939 ตัว |
| WR เพิ่มจาก 77.4% → 79.4% | | Non-FLIP PnL ลดจาก $16,252 → $8,451 (-48%) |
| | | **Net PnL ลด $3,792 (-32%)** |

### การค้นพบสำคัญ: ob_combined สร้าง signals ที่ดี มากกว่า signals ที่เสีย

- **ob=2.0**: สร้าง 1,939 เทรดเพิ่ม (vs ob=0) → กำไรสุทธิ +$3,792 จากเทรดเหล่านี้
- **FLIP cost จาก OB**: ~$4,008 (FLIP ที่หายไปเมื่อเอา OB ออก)
- **Edge จาก OB**: $3,792 net = OB ให้ edge $7,800 แต่โดน FLIP กิน $4,008 → net +$3,792

### EXP6: Smooth MA24 = ดีที่สุด (แต่ marginal)

- PnL $11,790 (Δ+$37) — แทบเท่า baseline
- FLIP ลดจาก 4.6% → 3.8% (-18%)
- FLIP PnL ดีขึ้น $611 (จาก -$4,498 → -$3,887)
- Non-FLIP PnL ลดเล็กน้อย $575
- **Net effect เกือบเป็นศูนย์** — smooth ไม่ช่วยอะไรมาก

### EXP5: Wider threshold = second best

- PnL $11,064 (Δ-$689)
- FLIP ลดจาก 4.6% → 3.5% (-24%)
- เสียเทรดดี 818 ตัว → PnL ลดมากกว่า FLIP ที่หลีกเลี่ยงได้

### Pattern ที่ชัดเจน

```
ob weight ↓ → Trades ↓↓ → FLIP ↓ → WR ↑ → แต่ Total PnL ↓↓
```

ob_combined ไม่ใช่แค่ noise — มันเป็น **primary trade generator** ของ v3!
เทรด 55% มาจาก OB signal (3547-1608 = 1939 เทรดจาก OB)

---

## สรุป & บทเรียน

### ผลหลัก: **สมมติฐานถูกครึ่งเดียว**

✅ ลด OB ลด FLIP ได้จริง (-84% เมื่อเอาออก)
❌ แต่ PnL ลดลงแทนที่จะเพิ่ม — OB สร้างเทรดดีมากกว่าเทรดเสีย

### ทำไม M023 บอกว่า OB = root cause แต่เอาออกกลับแย่?

1. OB flip ก่อน 49% ของ FLIP trades — **แต่ FLIP trades มีแค่ 4.6%**
2. อีก 95.4% ของเทรดที่ OB สร้างเป็นเทรดดี
3. M023 ดูแค่ FLIP trades → bias: เห็นแต่ปัญหา ไม่เห็นประโยชน์

### Action Items

1. **อย่าลด ob_combined weight** — มันเป็น core trade generator
2. **EXP6 (smooth MA24)** อาจทดสอบเพิ่มใน paper — gain น้อยมากแต่ไม่มี downside
3. **ทางออกจริงของ FLIP**: ไม่ใช่ลด factor แต่คือ **exit management**
   - In-trade score stability check (ถ้า agreement drops → exit เร็ว)
   - Time-based exit สำหรับเทรดที่ score สั่น
4. **Lesson สำคัญ**: Factor ที่เป็นตัวการของ FLIP ≠ Factor ที่ควรเอาออก
   เพราะ factor เดียวกันสร้างทั้งเทรดดีและเทรดเสีย

### Next Mission Ideas

- **Dynamic exit based on OB stability**: ถ้า OB flip ระหว่างเทรด → exit ก่อน (ไม่ต้องรอ composite FLIP)
- **OB-only vs OB-absent subportfolio**: แยกเทรดที่ OB active vs ไม่ active แล้วใช้ exit rules ต่างกัน