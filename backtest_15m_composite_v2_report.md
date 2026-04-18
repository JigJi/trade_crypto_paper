# BTC 15m Composite Strategy V2 -- Backtest Report

**Generated:** 2026-03-08 14:02
**Symbol:** BTCUSDT (Binance Futures Perpetual)
**Timeframe:** 15m | **Equity:** $10,000 | **Position:** $1,000 | **Leverage:** 1.0x
**Fees:** Maker 2.0 bps + Slippage 1.5 bps (round-trip: 0.070%)
**Period:** 2025-09-15 00:00:00 to 2026-03-07 22:45:00 (16,700 bars)

---

## V2 Improvements over V1
1. **1H Trend Filter** -- only long in 1H uptrend (EMA9>EMA21), short in 1H downtrend
2. **Time-of-Day Filter** -- only trade during London/NY session (13:00-22:00 UTC)
3. **Dynamic Position Sizing** -- scale position 1.5x-2.0x for stronger signals
4. **Wider Trailing Stop Grid** -- tested trail 0.5, 0.8, 1.0, 1.5, 2.0 ATR to improve R:R
5. **Bias Fixes** -- ETF flow +1d, liquidation +1h timestamp shift
6. **Walk-Forward Validation** -- grid search on train only, validated on unseen test set

---

## Best Configuration
| Parameter | Value |
|-----------|-------|
| Weight Set | sentiment |
| Filters | none |
| Signal Threshold | 3.0 |
| SL (ATR mult) | 2.0 |
| TP (ATR mult) | 3.0 |
| Trail (ATR mult) | 0.5 |
| Trail Activate | 0.5 ATR |
| Cooldown | 4 bars |

---

## Walk-Forward Validation

**Split Date:** 2026-02-01

| Metric | TRAIN (in-sample) | TEST (out-of-sample) | FULL |
|--------|-------------------|----------------------|------|
| Period | Sep 2025 - Jan 2026 | Feb - Mar 2026 | Sep 2025 - Mar 2026 |
| Bars | 13,344 | 3,356 | 16,700 |
| Trades | 134 | 68 | 202 |
| Win Rate | 63.4% | 72.1% | 66.3% |
| Profit Factor | 1.516 | 2.230 | 1.760 |
| Net PnL | $+94.44 | $+117.36 | $+211.80 |
| Max Drawdown | -0.66% | -0.16% | -0.66% |
| Sharpe | 1.690 | 2.027 | 2.631 |
| R:R | 0.874 | 0.865 | 0.893 |
| Avg Win | $3.27 | $4.34 | $3.66 |
| Avg Loss | -$3.74 | -$5.02 | -$4.10 |
| Long (WR) | 69 (62.3%) | 64 (70.3%) | 133 (66.2%) |
| Short (WR) | 65 (64.6%) | 4 (100.0%) | 69 (66.7%) |
| Buy & Hold | -31.60% | -15.24% | -41.58% |

**Out-of-Sample: PROFITABLE** -- $+117.36

---

## Dynamic Position Sizing (Full Period)
| Metric | Fixed $1,000 | Dynamic (1x/1.5x/2x) |
|--------|-------------|----------------------|
| Net PnL | $+211.80 | $+212.26 |
| PF | 1.760 | 1.738 |
| Max DD | -0.66% | -0.71% |
| Avg PnL | $1.05 | $1.05 |

---

## Exit Analysis (Full Period)
| Exit Reason | Count | % | Avg PnL | WR | Total PnL |
|-------------|-------|---|---------|-----|-----------|
| SL | 39 | 19.3% | $-6.94 | 0% | $-270.75 |
| TRAIL | 163 | 80.7% | $+2.96 | 82% | $+482.55 |

---

## Top 10 Grid Results (Train Set)
| # | Filters | Thr | SL | TP | Trail | CD | Trades | WR% | PF | PnL | DD% | R:R |
|---|---------|-----|----|----|-------|----|--------|-----|-----|-----|-----|-----|
| 1 | none | 3.0 | 2.0 | 3.0 | 0.5 | 4 | 134 | 63.4% | 1.52 | $+94.44 | -0.7% | 0.87 |
| 2 | none | 3.0 | 2.0 | 4.0 | 0.5 | 4 | 134 | 63.4% | 1.52 | $+94.44 | -0.7% | 0.87 |
| 3 | none | 3.0 | 2.0 | 3.0 | 0.5 | 8 | 110 | 65.5% | 1.63 | $+88.41 | -0.5% | 0.86 |
| 4 | none | 3.0 | 2.0 | 4.0 | 0.5 | 8 | 110 | 65.5% | 1.63 | $+88.41 | -0.5% | 0.86 |
| 5 | none | 3.5 | 1.5 | 3.0 | 0.5 | 4 | 69 | 68.1% | 1.99 | $+85.85 | -0.4% | 0.93 |
| 6 | none | 3.5 | 1.5 | 4.0 | 0.5 | 4 | 69 | 68.1% | 1.99 | $+85.85 | -0.4% | 0.93 |
| 7 | none | 3.0 | 1.5 | 3.0 | 0.5 | 4 | 134 | 59.7% | 1.43 | $+77.58 | -0.4% | 0.96 |
| 8 | none | 3.0 | 1.5 | 4.0 | 0.5 | 4 | 134 | 59.7% | 1.43 | $+77.58 | -0.4% | 0.96 |
| 9 | none | 3.5 | 2.0 | 3.0 | 0.5 | 4 | 69 | 71.0% | 1.81 | $+77.49 | -0.5% | 0.74 |
| 10 | none | 3.5 | 2.0 | 4.0 | 0.5 | 4 | 69 | 71.0% | 1.81 | $+77.49 | -0.5% | 0.74 |

---

## V1 vs V2 Comparison
| Metric | V1 Baseline | V2 Improved |
|--------|------------|-------------|
| Filters | none | none |
| Train PnL | $+76.28 | $+94.44 |
| Test PnL (OOS) | $+38.95 | $+117.36 |
| Full PnL | $+115.23 | $+211.80 |
| Full PF | 1.532 | 1.760 |
| Full R:R | 0.662 | 0.893 |
| Full Max DD | -0.39% | -0.66% |