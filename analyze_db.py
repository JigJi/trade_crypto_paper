import os
import psycopg2
import pandas as pd
import numpy as np
from dotenv import load_dotenv
from datetime import datetime, timedelta

load_dotenv()

def get_db_connection():
    return psycopg2.connect(
        dbname=os.getenv('PG_DB'),
        user=os.getenv('PG_USER'),
        password=os.getenv('PG_PASS'),
        host=os.getenv('PG_HOST'),
        port=os.getenv('PG_PORT')
    )

def run_analysis():
    conn = get_db_connection()
    
    # Define aggregation queries for the last year
    queries = {
        'etf_btc': "SELECT date::date as day, total as etf_flow FROM etf_btc WHERE date >= NOW() - INTERVAL '1 year'",
        'fear_greed': "SELECT created_at::date as day, AVG(score) as fg_score FROM fear_greed WHERE created_at >= NOW() - INTERVAL '1 year' GROUP BY 1",
        'funding_rate': "SELECT date::date as day, AVG(funding_rate) as avg_funding FROM funding_rate WHERE symbol = 'BTCUSDT' AND date >= NOW() - INTERVAL '1 year' GROUP BY 1",
        'whale_alert': "SELECT alert_time::date as day, COUNT(*) as whale_count, SUM(usd_value) as whale_usd FROM whale_alert WHERE symbol = 'BTC' AND alert_time >= NOW() - INTERVAL '1 year' GROUP BY 1",
        'displacement': "SELECT candle_time::date as day, COUNT(*) as disp_count FROM displacement_candles WHERE symbol = 'BTCUSDT' AND candle_time >= NOW() - INTERVAL '1 year' GROUP BY 1",
        'fvg': "SELECT fvg_time::date as day, COUNT(*) as fvg_count FROM fvg WHERE symbol = 'BTCUSDT' AND fvg_time >= NOW() - INTERVAL '1 year' GROUP BY 1",
        'liquidity': "SELECT liq_time::date as day, COUNT(*) as liq_sweep_count FROM liquidity_sweeps WHERE symbol = 'BTCUSDT' AND liq_time >= NOW() - INTERVAL '1 year' GROUP BY 1",
        'liquidations': "SELECT created_at::date as day, SUM(liq_long_1h) as liq_long, SUM(liq_short_1h) as liq_short FROM liquidation WHERE created_at >= NOW() - INTERVAL '1 year' GROUP BY 1"
    }

    dfs = {}
    for name, query in queries.items():
        try:
            dfs[name] = pd.read_sql(query, conn)
            dfs[name]['day'] = pd.to_datetime(dfs[name]['day'])
        except Exception as e:
            print(f"Error loading {name}: {e}")

    conn.close()

    # Merge all into a master dataframe
    master_df = pd.DataFrame({'day': pd.date_range(start=datetime.now() - timedelta(days=365), end=datetime.now(), freq='D').normalize()})
    for name, df in dfs.items():
        if not df.empty:
            master_df = master_df.merge(df, on='day', how='left')
    
    master_df = master_df.fillna(0)
    
    # Correlation Analysis
    corr_matrix = master_df.corr(numeric_only=True)
    
    # Lead-Lag Analysis: Does X(t-1) predict Displacement(t)?
    lags = {}
    if 'disp_count' in master_df.columns:
        for col in master_df.columns:
            if col not in ['day', 'disp_count']:
                lags[col] = master_df[col].shift(1).corr(master_df['disp_count'])

    # Generate Report
    report = f"""# BTC Trading Analysis Report
Generated on: {datetime.now().strftime('%Y-%m-%d')}

## 1. Overview
Analysis of database relationships over the last 12 months for BTCUSDT.

## 2. Key Correlations
Direct correlations between metrics (same-day):
{corr_matrix['disp_count'].sort_values(ascending=False).to_markdown() if 'disp_count' in corr_matrix else "N/A"}

## 3. Leading Indicators (T-1 Predictors)
Correlation of previous day's metric with today's Displacement Candles:
| Metric | Lagged Correlation |
| :--- | :--- |
"""
    for metric, val in sorted(lags.items(), key=lambda x: abs(x[1]), reverse=True):
        report += f"| {metric} | {val:.4f} |\n"

    report += """
## 4. Patterns & Insights
- **Whale Activity:** Check if `whale_usd` spikes precede high volatility (displacement).
- **Sentiment:** Relationship between `fg_score` (Fear & Greed) and FVG occurrences.
- **ETF Flows:** Impact of institutional `etf_flow` on market structure (Zones/OBs).

## 5. Potential Trading Signals
1. **Divergence Signal:** High Whale Inflow + Negative ETF Flow.
2. **Expansion Signal:** Elevated Funding Rates + Liquidity Sweeps.
3. **Reversal Signal:** Extreme Fear/Greed + Zone touch.

*Note: This report is for informational purposes only.*
"""
    
    with open('trading_analysis.md', 'w') as f:
        f.write(report)
    print("Report generated: trading_analysis.md")

if __name__ == "__main__":
    run_analysis()
