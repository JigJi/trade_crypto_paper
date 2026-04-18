# Mission 012: BTC Volatility Regime Classification & Adaptive Trading

**Date**: 2026-03-20
**Type**: regime_analysis
**Difficulty**: hard (7 experiments, 6018 trades)

## สมมติฐาน
1. BTC 15m realized volatility สามารถแบ่งเป็น regime ที่ชัดเจน
2. v3 performance ต่างกันอย่างมีนัยสำคัญในแต่ละ regime
3. Adaptive position sizing ช่วยเพิ่ม risk-adjusted return

## Exp 1: Vol Regime Classification
Rolling 24h realized vol (annualized), OOS 2025-01-01 to 2026-03-18

| Regime | Bars | % Time | Mean RV | Signal Rate (|score|>=3) |
|--------|------|--------|---------|------------------------|
| Low | 10352 | 25.0% | 0.2% | 23.0% |
| Normal | 20702 | 50.0% | 0.4% | 21.8% |
| High | 6211 | 15.0% | 0.6% | 30.3% |
| Extreme | 4141 | 10.0% | 0.9% | 25.7% |

Thresholds: Low<0.3%, Normal<0.5%, High<0.7%, Extreme>0.7%

## Exp 2: Trade Performance by Regime
| Regime | Trades | WR | Total PnL | Avg PnL | PnL/bar |
|--------|--------|-----|-----------|---------|--------|
| Low | 1406 | 67.3% | $1958.8 | $1.39 | $0.273 |
| Normal | 2770 | 72.9% | $6648.8 | $2.4 | $0.402 |
| High | 1236 | 72.6% | $3669.9 | $2.97 | $0.507 |
| Extreme | 606 | 71.8% | $-239.7 | $-0.4 | $-0.053 |

## Exp 3: Regime Transitions
Total transitions: 569 (1.3/day)

| Direction | Trades | WR | Avg PnL |
|-----------|--------|-----|--------|
| escalation | 736 | 69.0% | $0.61 |
| deescalation | 687 | 69.3% | $2.21 |

## Exp 4: Adaptive Position Sizing
| Strategy | Trades | PnL | MaxDD | Calmar |
|----------|--------|-----|-------|--------|
| fixed_1000 | 6018 | $12037.9 | $-811.3 | 14.84 |
| vol_inverse | 6018 | $12219.7 | $-651.3 | 18.76 |
| vol_follow | 6018 | $12653.7 | $-1331.1 | 9.51 |
| extreme_avoid | 5412 | $12277.6 | $-497.9 | 24.66 |
| sweet_spot | 6018 | $14502.7 | $-786.1 | 18.45 |

## Exp 5: Vol-Based Entry Filter
| Filter | Trades | WR | PnL |
|--------|--------|-----|-----|
| p0-p100 | 6018 | 71.4% | $12037.9 |
| p10-p90 | 4823 | 72.2% | $11699.8 |
| p20-p80 | 3380 | 72.6% | $9265.4 |
| p25-p75 | 2770 | 72.9% | $6648.8 |
| p25-p100 | 4612 | 72.7% | $10079.0 |
| p0-p75 | 4176 | 71.0% | $8607.6 |
| p25-p90 | 4006 | 72.8% | $10318.7 |

## Exp 6: Direction x Regime
| Regime | LONG Trades | LONG WR | LONG PnL | SHORT Trades | SHORT WR | SHORT PnL |
|--------|-------------|---------|----------|--------------|----------|----------|
| Low | 594 | 65.8% | $1016.5 | 812 | 68.3% | $942.4 |
| Normal | 1318 | 70.3% | $1653.9 | 1452 | 75.3% | $4994.9 |
| High | 486 | 72.2% | $2128.5 | 750 | 72.8% | $1541.4 |
| Extreme | 295 | 70.8% | $-252.5 | 311 | 72.7% | $12.8 |

## Exp 7: Regime Duration & Persistence
| Regime | Episodes | Avg Duration | Max Duration |
|--------|----------|-------------|-------------|
| Low | 136 | 19.0h | 130.2h |
| Normal | 233 | 22.2h | 157.5h |
| High | 148 | 10.5h | 52.0h |
| Extreme | 52 | 19.9h | 109.2h |

Autocorrelation: lag1=0.9913, lag96=0.5204

## สรุปผล

- **Regime ที่ดีที่สุด**: Normal (PnL $6648.8)
- **Regime ที่แย่ที่สุด**: Extreme (PnL $-239.7)
- **Sizing ที่ดีที่สุด**: extreme_avoid (Calmar 24.66 vs baseline 14.84)
- **Direction+Regime ที่ดีที่สุด**: S+Normal (WR 75.3%)
- **Regime predictability**: autocorr lag96 = 0.5204 (สูง = ทำนายได้)
- **วิเคราะห์ทั้งหมด**: 6018 trades, 7 experiments
