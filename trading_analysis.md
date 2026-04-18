# BTC Trading Analysis Report
Generated on: 2026-02-26

## 1. Overview
Analysis of database relationships over the last 12 months for BTCUSDT.

## 2. Key Correlations
Direct correlations between metrics (same-day):
|                 |   disp_count |
|:----------------|-------------:|
| disp_count      |    1         |
| fvg_count       |    0.797418  |
| fg_score        |    0.463972  |
| whale_count     |    0.402532  |
| avg_funding     |    0.374927  |
| liq_long        |    0.367507  |
| liq_short       |    0.3281    |
| liq_sweep_count |    0.311971  |
| whale_usd       |    0.234383  |
| etf_flow        |   -0.0465638 |

## 3. Leading Indicators (T-1 Predictors)
Correlation of previous day's metric with today's Displacement Candles:
| Metric | Lagged Correlation |
| :--- | :--- |
| fvg_count | 0.6049 |
| fg_score | 0.4698 |
| avg_funding | 0.3833 |
| liq_sweep_count | 0.3573 |
| whale_count | 0.3254 |
| liq_short | 0.2103 |
| whale_usd | 0.1888 |
| liq_long | 0.1866 |
| etf_flow | -0.0536 |

## 4. Patterns & Insights
- **Whale Activity:** Check if `whale_usd` spikes precede high volatility (displacement).
- **Sentiment:** Relationship between `fg_score` (Fear & Greed) and FVG occurrences.
- **ETF Flows:** Impact of institutional `etf_flow` on market structure (Zones/OBs).

## 5. Potential Trading Signals
1. **Divergence Signal:** High Whale Inflow + Negative ETF Flow.
2. **Expansion Signal:** Elevated Funding Rates + Liquidity Sweeps.
3. **Reversal Signal:** Extreme Fear/Greed + Zone touch.

*Note: This report is for informational purposes only.*
