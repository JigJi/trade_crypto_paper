# BTC 15m Composite Strategy -- Final Backtest Report

**Generated:** 2026-03-08 13:46
**Symbol:** BTCUSDT (Binance Futures Perpetual)
**Timeframe:** 15m
**Equity:** $10,000 | Position: $1,000 | Leverage: 1.0x
**Fees:** Maker 2.0 bps + Slippage 1.5 bps (round-trip: 0.070%)

**Data Period:** 2025-09-15 00:00 to 2026-03-07 22:45
**Total Candles:** 16,700
**DB Features Available:** oi_usdt (94%), buy_sell_ratio (96%), gl_ac_ratio (96%), fg_score (98%), whale_net (100%), liq_net (93%), etf_flow (98%), fr_8h (98%), last_funding_rate (96%)

## Benchmark
- Buy & Hold: **-41.58%** ($115,061 -> $67,220)
- Buy & Hold Max Drawdown: **-52.22%**

## Best Strategy Configuration
| Parameter | Value |
|-----------|-------|
| Weight Set | sentiment |
| Signal Threshold | 3.5 |
| SL (ATR mult) | 1.5 |
| TP (ATR mult) | 3.0 |
| Trail (ATR mult) | 0.5 |
| Cooldown (bars) | 4 |

## Performance
| Metric | Value |
|--------|-------|
| Total Trades | 116 |
| Win Rate | 69.83% |
| Profit Factor | 1.532 |
| Net PnL | $+115.23 (+1.15%) |
| Final Equity | $10,115.23 |
| Max Drawdown | -0.39% |
| Sharpe Ratio | 1.708 |
| Risk/Reward | 0.662 |
| Avg PnL/Trade | $0.99 |
| Avg Win | $4.10 |
| Avg Loss | -$6.19 |
| Exposure | 2.36% |
| Long Trades | 87 (WR: 64.4%) |
| Short Trades | 29 (WR: 86.2%) |
| vs Buy & Hold | +42.73pp |

## Exit Analysis
| Exit Reason | Count | % | Avg PnL | WR | Total PnL |
|-------------|-------|---|---------|-----|-----------|
| SL | 35 | 30.2% | $-6.19 | 0% | $-216.69 |
| TRAIL | 81 | 69.8% | $+4.10 | 100% | $+331.92 |

## Top 10 Parameter Combinations
| # | Weights | Thr | SL | TP | Trail | CD | Trades | WR% | PF | Net PnL | DD% |
|---|---------|-----|----|----|-------|----|--------|-----|-----|---------|-----|
| 1 | sentiment | 3.5 | 1.5 | 3.0 | 0.5 | 4 | 67 | 71.6% | 1.76 | $+76.28 | -0.4% |
| 2 | sentiment | 3.5 | 1.5 | 4.0 | 0.5 | 4 | 67 | 71.6% | 1.76 | $+76.28 | -0.4% |
| 3 | sentiment | 3.0 | 2.0 | 3.0 | 0.5 | 4 | 132 | 68.9% | 1.31 | $+72.97 | -0.5% |
| 4 | sentiment | 3.0 | 2.0 | 4.0 | 0.5 | 4 | 132 | 68.9% | 1.31 | $+72.97 | -0.5% |
| 5 | sentiment | 3.0 | 2.0 | 3.0 | 0.5 | 8 | 108 | 70.4% | 1.38 | $+72.08 | -0.6% |
| 6 | sentiment | 3.0 | 2.0 | 4.0 | 0.5 | 8 | 108 | 70.4% | 1.38 | $+72.08 | -0.6% |
| 7 | sentiment | 3.5 | 1.0 | 2.0 | 0.5 | 4 | 69 | 62.3% | 1.70 | $+68.03 | -0.2% |
| 8 | sentiment | 3.5 | 1.0 | 3.0 | 0.5 | 4 | 69 | 62.3% | 1.70 | $+68.03 | -0.2% |
| 9 | sentiment | 3.5 | 1.0 | 4.0 | 0.5 | 4 | 69 | 62.3% | 1.70 | $+68.03 | -0.2% |
| 10 | sentiment | 3.5 | 2.0 | 3.0 | 0.5 | 4 | 67 | 74.6% | 1.60 | $+67.09 | -0.6% |

## Data Sources Used
| Source | Table | Usage |
|--------|-------|-------|
| Open Interest | market_data.open_interest | OI divergence with price |
| Taker Volume | market_data.taker_volume | Buy/sell pressure ratio |
| Long/Short Ratio | market_data.long_short_ratio | Positioning imbalance |
| Funding Rate | public.funding_rate | Leverage cost extremes |
| Fear & Greed | public.fear_greed | Sentiment extreme |
| Whale Alerts | public.whale_alert | Large transfers |
| Liquidation | public.liquidation | Cascade reversals |
| ETF Flows | public.etf_btc | Institutional flow |

## Walk-Forward Validation (Bias Check)

Grid search was run on TRAIN set only. Best parameters were then tested on unseen TEST set.

**Split Date:** 2026-02-01

| Metric | TRAIN (in-sample) | TEST (out-of-sample) | FULL |
|--------|-------------------|----------------------|------|
| Period | Sep 2025 - Jan 2026 | Feb 2026 - Mar 2026 | Sep 2025 - Mar 2026 |
| Bars | 13,344 | 3,356 | 16,700 |
| Trades | 67 | 49 | 116 |
| Win Rate | 71.6% | 67.3% | 69.8% |
| Profit Factor | 1.761 | 1.334 | 1.532 |
| Net PnL | $+76.28 | $+38.95 | $+115.23 |
| Max Drawdown | -0.39% | -0.38% | -0.39% |
| Sharpe | 1.834 | 0.728 | 1.708 |
| R:R | 0.697 | 0.647 | 0.662 |
| Buy & Hold | -31.60% | -15.24% | -41.58% |

**Out-of-Sample Result:** PROFITABLE -- $+38.95
The strategy generalizes to unseen data, suggesting the edge is real (not overfit).

## Bias Fixes Applied
1. **ETF Flow:** Shifted timestamps +1 day (data only available after US market close)
2. **Liquidation:** Shifted timestamps +1 hour (hourly data available at end of hour)
3. **Walk-Forward:** Grid search on train set only, validated on unseen test set