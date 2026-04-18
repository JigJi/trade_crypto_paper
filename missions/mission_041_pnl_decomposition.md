# Mission 041: PnL Decomposition of 2,250 Archived Trades

**Date:** 2026-04-18
**Source:** `paper_trades_archive_20260418.db` (entire paper trading history 2026-03-17 → 2026-04-17)
**Total:** 2,250 trades, Net PnL +$160.34, PF 1.037, WR 41.6%

---

## The single biggest finding

**SIGNAL_FLIP "reverse" mode was responsible for -$2,165 of losses (100% of the equity drawdown and then some). Surgery 04-10 switched to "exit_only" and the bleeding stopped immediately.**

```
                              SIGNAL_FLIP PnL       Other exits PnL     Net PnL
Pre-surgery  (24 days, 2,146)     -$2,165            +$2,306          +$141
Post-surgery (8 days, 104)          +$2.31             +$17               +$19
```

## Exit reason breakdown (all-time)

| Exit Reason  | Trades | WR    | Total PnL | Avg    |
|--------------|--------|-------|-----------|--------|
| TP           | 251    | 99.2% | +$1,726   | +$6.88 |
| SL/TP        | 84     | 72.6% | +$826     | +$9.84 |
| TIMEOUT      | 68     | 75.0% | +$179     | +$2.63 |
| TRAIL        | 539    | 48.2% | +$150     | +$0.28 |
| LONG_DISABLED| 17     | 35.3% | -$2       | -$0.11 |
| SL           | 57     | 14.0% | -$556     | -$9.75 |
| **SIGNAL_FLIP** | **1234** | **24.3%** | **-$2,163** | **-$1.75** |

**If SIGNAL_FLIP never fired:** PnL would have been +$2,323 instead of +$160.

---

## Pre-surgery vs post-surgery

| Metric              | Pre (03-17 to 04-10) | Post (04-10 to 04-17) |
|---------------------|----------------------|-----------------------|
| Days                | 24                   | 8                     |
| Trades              | 2,146                | 104                   |
| WR                  | 41.5%                | 42.3%                 |
| PF                  | 1.033                | **1.229**             |
| PnL                 | +$141                | +$19                  |
| SIGNAL_FLIP PnL     | **-$2,165**          | **+$2.31**            |
| SL losses           | -$548                | -$7.82                |
| TP + SL/TP wins     | +$2,514              | +$38                  |

Key observations:
- Trade rate fell 85% post-surgery (LONG disabled + coin shrink + health pause)
- PF improved from 1.03 → 1.23
- SIGNAL_FLIP fix was structural — it's not bleeding anymore
- Post-surgery sample is too small (8 days, 104 trades) for strong conclusions but direction is positive

---

## Other findings

### Direction asymmetry (confirmed memory)
- **SHORT**: 1,386 trades, WR 48.1%, **+$570**
- **LONG**: 864 trades, WR 31.1%, **-$410**
- → LONG disable on 04-04 was correct action. Need redesign before re-enabling.

### Hour-of-day patterns (all-time, entry UTC)
Worst hours:
- **Hour 7 UTC (14:00 BKK, EU open):** -$360 (73% of loss = SIGNAL_FLIP)
- Hour 10 UTC: -$180
- Hour 13 UTC: -$108

Best hours: 9 UTC (+$190), 20 UTC (+$154), 22 UTC (+$148)

**Note:** Hour 7 UTC losses are mostly SIGNAL_FLIP → now fixed. May not repeat post-surgery.

### Per-coin (current 5 + removed)
Current COINS_V3 (post-surgery):
- ARIA: 100% WR, +$56 (9 trades) — star continues
- Others: mixed, small sample, need more data

---

## Recommendations

### DO (evidence-based):
1. **KEEP surgery 04-10 fix** (flip_mode="exit_only" + cd_extra=4) — proved the thesis
2. **Restart daemon with fresh $5k + 0 flip-reverse history** — current code is clean
3. **Hold config static for 30 days** — need clean data from post-surgery regime
4. **Re-evaluate all "shrink" decisions** (LONG disable, coin removals, health pauses) after Mission 050 with clean post-surgery data

### DON'T (not supported by evidence):
- ❌ New feature additions (more filters, more gates, new factors)
- ❌ Re-enable LONG without redesigning entry logic for it
- ❌ Restore v5 or v6 (both underperformed)
- ❌ Expand coin universe before 30-day baseline is established

### Open questions for next research:
- M042: Post-surgery WR by coin (once n≥30/coin)
- M043: Does hour-7 UTC bleed persist in post-surgery regime?
- M044: What's the distribution of "near-miss" TPs that got SIGNAL_FLIPped?
- M045: Position sizing optimization per coin (Kelly-ish based on WR × avg win/loss)

---

## Decision for user

The $475 equity loss was **tuition for the SIGNAL_FLIP fix**. The fix is in.
Post-surgery data shows edge may exist but sample is too small (8 days, 104 trades).

**Recommended action:** Restart daemon as-is, observe 30 days without config changes, then run Mission 042-045.
