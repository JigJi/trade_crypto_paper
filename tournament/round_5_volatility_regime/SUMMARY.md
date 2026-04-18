# Tournament Round 5: Volatility Regime Trading - SUMMARY

**Date:** 2026-03-26
**OOS Period:** 2025-01-01 to 2026-03-26 (42,649 bars)
**Baseline:** v6 + R3 champion = **$83,768** (2,643 trades, 71.7% WR)

## Hypothesis

Focus ONLY on high-volatility periods (strong price moves) with two strategies:
1. **Momentum ("ride the wave")** - when price moves strongly, follow the direction
2. **Rebound ("fade extreme")** - when price moves too far, bet on mean reversion

## Phase 1: Volatility Detection Analysis

17 detection methods tested. Key findings on frequency vs selectivity:

| Method | Vol Bars | % of Time | Avg Window |
|--------|----------|-----------|------------|
| range_z > 1.5 | 3,657 | 8.6% | 1.6 bars |
| vol_spike > 2x | 4,168 | 9.8% | 1.6 bars |
| displacement > 1.5 | 9,674 | 22.7% | 3.3 bars |
| ret_z 4bar > 2.0 | 2,781 | 6.5% | 2.0 bars |
| ATR z > 1.5 | 6,128 | 14.4% | 11.3 bars |
| liq_cascade 2x | 3,608 | 8.5% | 6.4 bars |
| combined any2 | 4,716 | 11.1% | 1.9 bars |

**Insight:** Displacement and ATR-z are most persistent (long windows), while range_z and vol_spike are transient spikes. Liq cascade windows are moderately persistent.

## Phase 2: Momentum Strategies (10 experiments)

**ALL MOMENTUM STRATEGIES LOST MONEY OR BROKE EVEN.**

| Experiment | PnL | Trades | WR% | Note |
|-----------|-----|--------|-----|------|
| M1_range_z_ret4 | -$3,198 | 2,620 | 40.2% | Base momentum |
| M2_vol_spike_ret4 | -$3,666 | 2,747 | 37.6% | Volume spike |
| M3_combined2_ret4 | -$8,083 | 2,897 | 38.2% | Combined detection |
| M4_range_z_ema_align | -$873 | 2,038 | 42.3% | + EMA confirmation |
| M5_liq_cascade_follow | -$7,766 | 1,427 | 35.6% | Follow liq direction |
| M6_streak3_vol | -$11,722 | 2,226 | 38.6% | 3-bar streak |
| **M7_range_z_wide_sltp** | **+$49** | 2,576 | 40.4% | Wide SL25/TP20 |
| M8_ret_z4_2.0 | -$6,249 | 2,407 | 40.0% | Return z-score |
| M9_range_z_tight_sltp | -$1,977 | 2,315 | 41.1% | Tight SL8/TP6 |
| **M10_range_z_ret8** | **+$2,139** | 2,167 | 41.3% | 2h lookback |

**Key finding:** Momentum on 15m is a LOSING strategy. By the time vol spike is detected and entry triggered, the move is already priced in. WR ~37-42% = worse than random.

## Phase 3: Rebound Strategies (10 experiments)

**Rebound shows slight edge (56-57% WR) but low absolute PnL.**

| Experiment | PnL | Trades | WR% | Note |
|-----------|-----|--------|-----|------|
| R1_disp_rebound_2.0 | -$262 | 1,810 | 56.9% | Displacement contrarian |
| R2_disp_rebound_1.5 | -$3,207 | 2,116 | 57.0% | Looser threshold |
| **R3_rsi_extreme_75_25** | **+$5,348** | 1,059 | 56.0% | **Best rebound** |
| R4_rsi_extreme_80_20 | +$1,931 | 438 | 53.6% | Stricter RSI |
| R5_liq_flush_5x | **-$33,030** | 772 | 25.3% | CATASTROPHIC |
| R6_liq_flush_3x | **-$49,061** | 1,148 | 23.0% | CATASTROPHIC |
| R7_exhaustion_disp1.5_rsi70 | +$102 | 1,443 | 53.7% | Combined |
| R8_range_extreme_90_10 | -$5,669 | 2,084 | 56.7% | Range position |
| R9_disp_rebound_wide_sl | -$953 | 1,693 | 57.1% | Wide SL |
| R10_combined2_exhaustion | -$1,915 | 1,684 | 52.8% | Combined any2 |

**CRITICAL:** Liquidation flush contrarian (R5, R6) is ANTI-EDGE. When cascading liq happens, the move CONTINUES - it does NOT reverse. This destroys the "rebound after flush" theory.

**RSI extreme (R3)** is the only consistently profitable rebound: +$5,348 with 1,059 trades at 56% WR.

## Phase 4: Hybrid (Baseline + Vol Regime)

| Experiment | PnL | vs Baseline | Trades | WR% |
|-----------|-----|-------------|--------|-----|
| **H2_baseline_reb_boost** | **$85,029** | **+$1,261 (+1.5%)** | 3,500 | 69.6% |
| BASELINE_v6_R3 | $83,768 | --- | 2,643 | 71.7% |
| H1_baseline_mom_boost | $76,297 | -$7,471 | 3,929 | 59.9% |
| H4_baseline_reb_replace | $75,078 | -$8,690 | 4,134 | 69.4% |
| H3_baseline_mom_replace | $65,506 | -$18,262 | 4,434 | 55.2% |

**H2 (baseline + displacement rebound boost)** is the only strategy that beats baseline, but by only +1.5% - marginal improvement similar to R4's market guard (+0.8%).

## Key Conclusions

### 1. Momentum ("ride the wave") DOES NOT WORK on 15m
- WR consistently 37-42% across ALL detection methods
- By the time a vol spike is detected, the move is done
- Even with EMA alignment, streak confirmation, or multiple detectors
- **Root cause:** 15m is too slow for momentum. By next candle, the move is priced in.

### 2. Rebound has SLIGHT edge but can't match baseline
- RSI extreme is the best standalone rebound: 56% WR, +$5.3K
- Displacement-based rebound also has ~57% WR but loses on PnL
- The edge exists but is too small relative to fees/slippage

### 3. Liquidation flush contrarian is ANTI-EDGE (Lesson #103)
- Fading liq cascades: 23-25% WR, -$33K to -$49K
- When cascading liquidation happens, the move ACCELERATES
- This confirms: v6's approach (follow liq direction) is correct

### 4. Adding signals to baseline DILUTES quality
- H1 (baseline + momentum gaps): -$7,471 vs baseline
- H3 (replace during vol): -$18,262 vs baseline
- **Lesson:** The baseline's signal quality is extremely high (71.7% WR). Adding lower-quality signals hurts more than it helps.

### 5. H2 rebound boost is marginal
- +$1,261 is not deployment-worthy (noise range)
- Adds 857 trades at lower WR (69.6% vs 71.7%)
- Similar to R4 market guard: small improvement, not worth complexity

## New Lessons for lessons_learned.md

- **#103:** Liquidation flush contrarian is ANTI-EDGE. When cascading liquidation happens, the move accelerates - it does NOT reverse. v6's approach (follow liq direction) is correct.
- **#104:** 15m momentum (chase price moves) does NOT work. By the time vol spike is detected and entry triggered, the move is already priced in. WR 37-42%.
- **#105:** Adding lower-quality signals to fill baseline "gaps" DILUTES overall quality. The baseline's 71.7% WR is extremely high - protect it.
- **#106:** RSI extreme rebound (vol spike + RSI>75/<25) has slight standalone edge (56% WR, +$5.3K) but can't match composite signal approach.

## Verdict

**NO DEPLOYMENT CHANGE.** The v6 + R3 champion remains the best strategy.

The volatility regime approach confirmed what we already suspected:
- The composite signal (v6 liq-only) already captures vol regimes implicitly
- Standalone vol-regime trading can't match the information density of 8 on-chain/market factors
- The strategy IS the volatility regime filter - it only trades when liq cascades happen (which ARE the vol events)
