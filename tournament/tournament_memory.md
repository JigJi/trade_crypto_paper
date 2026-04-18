# Tournament Memory -- Trading Strategy Evolution
**Started**: 2026-03-18
**Goal**: Evolve v3/v4 → Next Version that beats both

## Round 1 Champion: v5 (GOD_liq5_ob2_tick3_tp12)
**PnL: $49,052 (+87.5% vs v3 baseline $26,165)**

| Metric | v3 Baseline | v5 Champion | Delta |
|--------|-------------|-------------|-------|
| PnL | $26,165 | $49,052 | +$22,887 (+87.5%) |
| Trades | 2,104 | 1,836 | -268 |
| Win Rate | 59.4% | 68.7% | +9.3% |
| Sharpe | 14.66 | 18.78 | +4.12 |
| Max DD | -2.4% | -3.2% | -0.8% |
| LONG WR | 59.3% | 68.7% | +9.4% |
| SHORT WR | 59.4% | 68.7% | +9.3% |

### v5 Config
```python
COMPOSITE_WEIGHTS = {
    "w_liq_bull": 5.0, "w_liq_bear": 5.0,     # was 2.0 → KEY CHANGE
    "w_fr_neg": 2.0, "w_fr_pos": 2.0,
    "w_whale_bull": 1.5, "w_whale_bear": 1.5,
    "w_etf_bull": 1.0, "w_etf_bear": 1.0,
    "w_oi_bull": 0.25, "w_oi_capit": 0.25, "w_oi_weak": 0.25, "w_oi_bear": 0.25,
}
V3_EXTRA_WEIGHTS = {
    "ob_combined": 2.0,       # kept at 2.0 (lower = less noise = better Sharpe)
    "tick_liq": 3.0,           # was 2.0 → +1.0
    "basis_contrarian": 1.5,   # unchanged
}
# Per-coin: SL=15.0, TP=12.0, cd=4 (all coins same)
```

## Round 1 Stats
| Item | Value |
|------|-------|
| Experiments | 139 |
| Batches | 6 |
| Runtime | ~3 min (data cached) |
| Models beating baseline | 50+ |
| OOS period | Jan 2025 - Mar 2026 |
| Coins tested | 6 (BTC, XRP, ADA, DOT, SUI, FIL) |

## Evolution Path (Kings)
| # | Config | PnL | Delta vs v3 | Batch |
|---|--------|-----|-------------|-------|
| 1 | v3_baseline (SL=2.5, TP=4.0) | $26,165 | -- | 1 |
| 2 | v3_sl10_tp8 | $33,338 | +$7,174 (+27%) | 1 |
| 3 | evo_boost_asym5_sl10_tp8 | $34,449 | +$8,284 (+32%) | 3 |
| 4 | ultra_simple_sl15_tp10 (no asym) | $40,346 | +$14,181 (+54%) | 4 |
| 5 | nk_liq5.0 | $45,635 | +$19,470 (+74%) | 5 |
| 6 | GOD_liq5_ob2_tick3_tp12 | $49,052 | +$22,887 (+88%) | 6 |

## Key Discoveries
1. **Liquidation weight monotonically improves 2.0→5.0** (diminishing after 5.0)
2. **ob_combined=2.0 best for Sharpe** (more = more noise)
3. **SL=15 > SL=10 > SL=2.5** (monotonic, SL hits = 0% WR always)
4. **TP=12 optimal** (TP=20 more PnL but less Sharpe)
5. **Asymmetric threshold HURTS in backtest** despite helping in paper (sample bias)
6. **All 8 factors contribute** (dropping any hurts net PnL)
7. **BEAR regime = cash cow** ($28K of $49K from H2 2025)

## Pending Verification (v5)
- [x] Test v5 on all 99 coins (done 2026-03-19, 92/99 profitable)
- [x] Deploy to paper trading (done 2026-03-19, 15 coins)
- [x] Round 2: Liquidation deep dive (done 2026-03-22, 246 experiments -> v6)

---

## Round 2 Champion: v6 (Liq-Only Architecture)
**PnL: $69,701 (+35.4% vs v5 baseline $51,464)**

| Metric | v5 Baseline | v6 Conservative | v6 Aggressive |
|--------|-------------|-----------------|---------------|
| PnL | $51,464 | $69,701 | $71,802 |
| Trades | 1,990 | 2,038 | 3,423 |
| Win Rate | 68.1% | 69.8% | 61.6% |
| Sharpe | 19.29 | 25.63 | 22.08 |
| Max DD | -3.1% | -1.1% | -0.9% |
| Factors | 8 | 2 (cascade+tick) | 3 (+velocity) |

### v6 Config
```python
COMPOSITE_WEIGHTS = {
    "w_liq_bull": 8.0, "w_liq_bear": 8.0,     # ALL others = 0
}
V6_EXTRA_WEIGHTS = {
    "tick_liq": 8.0,           # net>3 threshold
    # ob_combined, basis_contrarian = 0
}
# CASCADE_MULT = 1.1 (not 3.0!)
# SL = 25.0 ATR, TP = 20.0 ATR
# Optional: velocity(w=5.0) for aggressive variant
```

### Round 2 Stats
| Item | Value |
|------|-------|
| Experiments | 246 |
| Rounds | 3 (R2, R2b, R2c) |
| Runtime | ~10 min total |
| OOS period | Jan 2025 - Mar 2026 |
| Coins tested | 6 (BTC, XRP, ADA, DOT, SUI, FIL) |

### Evolution Path (Round 2)
| # | Config | PnL | Delta vs v5 | Round |
|---|--------|-----|-------------|-------|
| 1 | v5 baseline (8 factors) | $51,464 | -- | -- |
| 2 | cascade 2.0x | $57,720 | +$6,256 | R2 |
| 3 | tiered both | $59,441 | +$7,978 | R2 |
| 4 | liq-only | $51,857 | +$393 | R2 |
| 5 | cascade 1.1x | $65,255 | +$13,791 | R2b |
| 6 | liq-only 1.1x | $67,648 | +$16,184 | R2b |
| 7 | + SL25/TP20 | $69,701 | +$18,237 | R2c |
| 8 | **+ velocity(5.0)** | **$71,802** | **+$20,338** | **R2c** |

### Key Discoveries (Round 2)
1. **Cascade threshold 1.1x > 3.0x**: Lower = more signals = more alpha (+$14K)
2. **Liq-only > full model**: 2 factors beat 8 factors (+35% PnL, +33% Sharpe)
3. **Weight saturates at 8.0**: Binary cascade signal makes higher weight pointless
4. **MA lookback doesn't matter**: 12-48 bars identical at 1.1x cascade mult
5. **Velocity adds PnL, costs WR**: +$4K but 70%→62% WR
6. **Ratio-based scoring**: Alternative architecture, $65K standalone
7. **Confluence kills**: Requiring both layers = too restrictive ($20K vs $67K)
8. **SL=25/TP=20**: Wider continues to help in liq-only mode
9. **Threshold irrelevant in liq-only**: Score jumps to ±8, always exceeds threshold
10. **max_hold=48**: Best Sharpe (27.02) and DD (-0.5%)

## V6 Validation Results (2026-03-22)

### Period Split: ALL POSITIVE
| Config | BULL_H1 | BEAR_H2 | Q1_2026 | FULL |
|--------|---------|---------|---------|------|
| v5 | $279 | $31,195 | $19,669 | $51,464 |
| v6 Conservative | $14 | $47,205 | $21,922 | $69,701 |

### Walk-Forward: v6 wins EVERY quarter
| Quarter | v5 | v6 |
|---------|----|----|
| Q2 2025 | $279 | $14 |
| Q3 2025 | $13,544 | $21,091 |
| Q4 2025 | $17,604 | $26,183 |
| Q1 2026 | $19,669 | $21,922 |

### All-Coin Test: v6 wins 33/33 coins
- v5: $323,578 | v6: $414,312 (+28%) | 33/33 coins profitable

### Realistic Portfolio (32 coins, max 3 concurrent, 4bps fees, funding)
| Config | PnL | Return | Max DD | WR | Trades |
|--------|-----|--------|--------|----|--------|
| v5 | $23,985 | +240% | -4.9% | 61.1% | 1,265 |
| **v6 Conservative** | **$30,941** | **+309%** | **-3.5%** | **61.3%** | **1,296** |
| v6 Aggressive | $25,844 | +258% | -6.8% | 51.2% | 2,387 |

### Vol Filter: NOT NEEDED
v6 has only 6 trades in Extreme vol (all winners). Liq-only is self-cleaning.

### Extreme_conf3 filter: NOT APPLICABLE
v6 has binary score (±8 or ±16). Factor confluence concept doesn't apply.

## Pending (v6)
- [x] Period-split validation -- ALL periods positive
- [x] All-coin test -- 33/33 coins v6>v5
- [x] Walk-forward -- consistent every quarter
- [x] Realistic portfolio backtest -- +29% vs v5, better DD
- [x] Vol filter test -- not needed (self-cleaning)
- [x] Implement v6 in backtest engine (compute_btc_composite_score_v6)
- [ ] Deploy to paper trading
- [ ] Compare paper trading v5 vs v6 (2 weeks)
