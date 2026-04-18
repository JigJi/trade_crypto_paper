# Tournament Round 4: Market Guard System

**Run date**: 2026-03-24 14:54
**Verdict**: MONITOR

## Baseline (R3 Champion)
- PnL: $+83,768
- Trades: 2643
- WR: 71.7%

## Champion: P2_all_winners
- PnL: $+84,409 (+0.8% vs baseline)
- Trades: 2636
- WR: 72.0%
- Sharpe: 26.34
- Max DD: -1.3%

## Guard Stats
- Signals blocked: 243 (8.4%)
- Blocked trades would-have PnL: $+14,047
- Guard savings: $-14,047

## Phase 1 Winners (per-group)
| Group | Guard | Winner |
|-------|-------|--------|
| A | Weekend | Baseline |
| B | ATR Spike | 3.0 |
| C | Double Liq | {'mode': 'balance', 'balance_thr': 0.3, 'total_mult': 2.0} |
| D | Score Osc | {'window': 24, 'max_flips': 4} |
| E | DD Breaker | Baseline |

## All Experiments (sorted by PnL)
| Rank | Name | Trades | WR% | PnL | Blocked | Savings | Delta |
|------|------|--------|-----|-----|---------|---------|-------|
| 1 | P2_all_winners | 2636 | 72.0% | $+84,409 | 243 | $-14,047 | $+642 |
| 2 | B_atr_spike_3.0x | 2642 | 71.7% | $+84,154 | 53 | $-6,359 | $+387 |
| 3 | B_atr_spike_2.0x | 2637 | 71.6% | $+83,927 | 337 | $-53,914 | $+159 |
| 4 | C_dliq_balance_30_2x | 2643 | 71.7% | $+83,894 | 120 | $-4,038 | $+126 |
| 5 | D_osc_24bars_4flips | 2643 | 71.8% | $+83,848 | 14 | $-1,020 | $+80 |
| 6 | D_osc_16bars_3flips | 2637 | 71.9% | $+83,835 | 52 | $-1,564 | $+67 |
| 7 | B_atr_spike_2.5x | 2642 | 71.8% | $+83,772 | 106 | $-35,811 | $+5 |
| 8 | BASELINE_no_guard | 2643 | 71.7% | $+83,768 | 0 | $-0 | $+0 |
| 9 | D_osc_8bars_3flips | 2643 | 71.7% | $+83,768 | 0 | $-0 | $+0 |
| 10 | D_osc_16bars_4flips | 2643 | 71.7% | $+83,768 | 0 | $-0 | $+0 |
| 11 | D_osc_24bars_5flips | 2643 | 71.7% | $+83,768 | 0 | $-0 | $+0 |
| 12 | E_dd_breaker_3pct | 2643 | 71.7% | $+83,768 | 0 | $-0 | $+0 |
| 13 | E_dd_breaker_5pct | 2643 | 71.7% | $+83,768 | 0 | $-0 | $+0 |
| 14 | E_dd_breaker_2pct | 2641 | 71.7% | $+83,764 | 0 | $-0 | $-4 |
| 15 | C_dliq_balance_40_3x | 2643 | 71.7% | $+83,740 | 16 | $-2,621 | $-27 |
| 16 | C_dliq_both_spike_3x | 2643 | 71.8% | $+83,589 | 322 | $-8,486 | $-179 |
| 17 | C_dliq_both_spike_2x | 2640 | 72.2% | $+82,996 | 706 | $-26,992 | $-771 |
| 18 | B_atr_spike_1.5x | 2600 | 72.0% | $+82,400 | 1962 | $-90,283 | $-1,368 |
| 19 | A_weekend_size_50 | 2643 | 71.7% | $+73,596 | 0 | $-0 | $-10,172 |
| 20 | A_weekend_block_all | 1996 | 73.9% | $+69,565 | 24493 | $-430,565 | $-14,202 |

## Phase 3: Cross-Model Validation
| Model | Trades | WR% | PnL |
|-------|--------|-----|-----|
| V3_with_guard | 1573 | 68.5% | $+38,183 |
| V6_baseline | 2643 | 71.7% | $+83,768 |
| V6_with_guard | 2636 | 72.0% | $+84,409 |
