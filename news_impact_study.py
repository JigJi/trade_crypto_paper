"""
BTC News Impact Analysis (2023-2026)
=====================================
Analyzes how major news events affected BTC price.
Categories: WAR/GEOPOLITICAL, REGULATION, FED/MACRO, ETF, EXCHANGE_COLLAPSE,
            WHALE/INSTITUTIONAL, TRUMP/POLITICS, HALVING, HACK/EXPLOIT, OTHER
"""
import pandas as pd
import numpy as np
import json
import os

# === Load BTC daily data ===
btc = pd.read_csv('btc_daily_3yr.csv', header=[0,1], index_col=0, parse_dates=True)
btc.columns = btc.columns.get_level_values(0)
btc = btc[['Open','High','Low','Close','Volume']].astype(float)
btc.index.name = 'date'

# === Events list ===
events = [
    # 2023
    {"date": "2023-03-10", "event": "SVB Bank Collapse", "category": "EXCHANGE_COLLAPSE", "expected": "BEARISH"},
    {"date": "2023-03-12", "event": "Fed BTFP backstop announced", "category": "FED/MACRO", "expected": "BULLISH"},
    {"date": "2023-03-27", "event": "CFTC sues Binance & CZ", "category": "REGULATION", "expected": "BEARISH"},
    {"date": "2023-06-05", "event": "SEC sues Binance", "category": "REGULATION", "expected": "BEARISH"},
    {"date": "2023-06-06", "event": "SEC sues Coinbase", "category": "REGULATION", "expected": "BEARISH"},
    {"date": "2023-06-15", "event": "BlackRock files spot BTC ETF", "category": "ETF", "expected": "BULLISH"},
    {"date": "2023-08-17", "event": "SpaceX BTC writedown, market dump 7%", "category": "WHALE/INSTITUTIONAL", "expected": "BEARISH"},
    {"date": "2023-08-29", "event": "Grayscale wins vs SEC on ETF", "category": "ETF", "expected": "BULLISH"},
    {"date": "2023-10-07", "event": "Hamas attack on Israel", "category": "WAR/GEOPOLITICAL", "expected": "BEARISH"},
    {"date": "2023-10-16", "event": "Fake BlackRock ETF approval news", "category": "ETF", "expected": "UNCERTAIN"},
    {"date": "2023-10-24", "event": "BTC surges on ETF momentum >$35K", "category": "ETF", "expected": "BULLISH"},
    {"date": "2023-11-21", "event": "CZ pleads guilty, Binance $4.3B fine", "category": "REGULATION", "expected": "BEARISH"},

    # 2024
    {"date": "2024-01-10", "event": "SEC approves 11 spot BTC ETFs", "category": "ETF", "expected": "BULLISH"},
    {"date": "2024-01-11", "event": "Sell-the-news dump post ETF launch", "category": "ETF", "expected": "BEARISH"},
    {"date": "2024-02-15", "event": "BTC breaks $52K on ETF inflows", "category": "ETF", "expected": "BULLISH"},
    {"date": "2024-03-14", "event": "BTC ATH $73.8K on ETF inflows", "category": "ETF", "expected": "BULLISH"},
    {"date": "2024-04-13", "event": "Iran attacks Israel, BTC -8%", "category": "WAR/GEOPOLITICAL", "expected": "BEARISH"},
    {"date": "2024-04-19", "event": "Bitcoin 4th Halving", "category": "HALVING", "expected": "BULLISH"},
    {"date": "2024-05-20", "event": "SEC approves spot ETH ETF filings", "category": "ETF", "expected": "BULLISH"},
    {"date": "2024-07-05", "event": "Mt.Gox + German govt BTC selling fears", "category": "WHALE/INSTITUTIONAL", "expected": "BEARISH"},
    {"date": "2024-07-08", "event": "German govt sells ~50K BTC ($3B+)", "category": "WHALE/INSTITUTIONAL", "expected": "BEARISH"},
    {"date": "2024-07-13", "event": "Trump assassination attempt", "category": "TRUMP/POLITICS", "expected": "BULLISH"},
    {"date": "2024-07-27", "event": "Trump Bitcoin 2024 Nashville speech", "category": "TRUMP/POLITICS", "expected": "BULLISH"},
    {"date": "2024-08-05", "event": "Japan carry trade unwind, BTC -15%", "category": "FED/MACRO", "expected": "BEARISH"},
    {"date": "2024-09-18", "event": "Fed cuts 50bps (first since 2020)", "category": "FED/MACRO", "expected": "BULLISH"},
    {"date": "2024-11-05", "event": "Trump wins presidential election", "category": "TRUMP/POLITICS", "expected": "BULLISH"},
    {"date": "2024-11-22", "event": "Gensler resigns SEC, BTC near $99K", "category": "REGULATION", "expected": "BULLISH"},
    {"date": "2024-12-04", "event": "BTC breaks $100K first time", "category": "OTHER", "expected": "BULLISH"},
    {"date": "2024-12-18", "event": "Fed cuts 25bps but hawkish dot plot", "category": "FED/MACRO", "expected": "BEARISH"},

    # 2025
    {"date": "2025-01-20", "event": "Trump inauguration, crypto EOs signed", "category": "TRUMP/POLITICS", "expected": "BULLISH"},
    {"date": "2025-02-02", "event": "Trump tariffs on Canada/Mexico/China", "category": "WAR/GEOPOLITICAL", "expected": "BEARISH"},
    {"date": "2025-02-03", "event": "BTC drops 8% to $91K on tariff fears", "category": "WAR/GEOPOLITICAL", "expected": "BEARISH"},
    {"date": "2025-02-21", "event": "Bybit hacked $1.4B (largest crypto hack)", "category": "HACK/EXPLOIT", "expected": "BEARISH"},
    {"date": "2025-03-02", "event": "Trump announces US BTC Strategic Reserve", "category": "TRUMP/POLITICS", "expected": "BULLISH"},
    {"date": "2025-03-07", "event": "BTC Reserve EO signed (seized BTC only)", "category": "TRUMP/POLITICS", "expected": "UNCERTAIN"},
    {"date": "2025-03-10", "event": "BTC drops to $80K, reserve disappointment", "category": "TRUMP/POLITICS", "expected": "BEARISH"},
    {"date": "2025-04-02", "event": "Trump Liberation Day tariffs (sweeping)", "category": "WAR/GEOPOLITICAL", "expected": "BEARISH"},
    {"date": "2025-04-07", "event": "Global crash, BTC $75K, China retaliates", "category": "WAR/GEOPOLITICAL", "expected": "BEARISH"},
    {"date": "2025-04-09", "event": "Trump pauses tariffs 90 days, BTC +10%", "category": "WAR/GEOPOLITICAL", "expected": "BULLISH"},
    {"date": "2025-05-12", "event": "US-China 90-day tariff truce", "category": "WAR/GEOPOLITICAL", "expected": "BULLISH"},
    {"date": "2025-05-21", "event": "BTC new ATH ~$110K+", "category": "OTHER", "expected": "BULLISH"},
]

edf = pd.DataFrame(events)
edf['date'] = pd.to_datetime(edf['date'])

# === Compute price changes around each event ===
results = []
for _, ev in edf.iterrows():
    d = ev['date']
    mask = btc.index >= d
    if not mask.any():
        continue
    idx = btc.index[mask][0]
    loc = btc.index.get_loc(idx)

    if loc < 1 or loc >= len(btc) - 3:
        continue

    p_before = btc.iloc[loc-1]['Close']
    p_event = btc.iloc[loc]['Close']
    p_1d = btc.iloc[min(loc+1, len(btc)-1)]['Close']
    p_3d = btc.iloc[min(loc+3, len(btc)-1)]['Close']
    p_7d = btc.iloc[min(loc+7, len(btc)-1)]['Close']

    window_3d = btc.iloc[loc:min(loc+4, len(btc))]
    window_7d = btc.iloc[loc:min(loc+8, len(btc))]
    max_high_3d = window_3d['High'].max()
    min_low_3d = window_3d['Low'].min()

    chg_0d = (p_event / p_before - 1) * 100
    chg_1d = (p_1d / p_before - 1) * 100
    chg_3d = (p_3d / p_before - 1) * 100
    chg_7d = (p_7d / p_before - 1) * 100
    max_up = (max_high_3d / p_before - 1) * 100
    max_down = (min_low_3d / p_before - 1) * 100
    vol_3d = window_3d['Volume'].mean()

    if ev['expected'] == 'BULLISH':
        correct = chg_3d > 0
    elif ev['expected'] == 'BEARISH':
        correct = chg_3d < 0
    else:
        correct = None

    results.append({
        'date': d.strftime('%Y-%m-%d'),
        'event': ev['event'],
        'category': ev['category'],
        'expected': ev['expected'],
        'chg_0d': round(chg_0d, 2),
        'chg_1d': round(chg_1d, 2),
        'chg_3d': round(chg_3d, 2),
        'chg_7d': round(chg_7d, 2),
        'max_up_3d': round(max_up, 2),
        'max_down_3d': round(max_down, 2),
        'correct': correct,
        'vol_3d': vol_3d,
        'btc_price': round(p_event, 0),
    })

rdf = pd.DataFrame(results)

# === PRINT RESULTS ===
print("=" * 130)
print("BTC NEWS IMPACT ANALYSIS (2023-2026) -- 41 Major Events")
print("=" * 130)
print(f"\n{'Date':>11} {'BTC$':>8} {'0D%':>6} {'1D%':>6} {'3D%':>7} {'7D%':>7} {'MaxUp':>6} {'MaxDn':>7} {'OK':>3} {'Category':>18}  Event")
print("-" * 130)
for _, r in rdf.iterrows():
    ok = 'Y' if r['correct'] == True else ('N' if r['correct'] == False else '?')
    print(f"{r['date']:>11} {r['btc_price']:>7.0f} {r['chg_0d']:>+5.1f}% {r['chg_1d']:>+5.1f}% {r['chg_3d']:>+6.1f}% {r['chg_7d']:>+6.1f}% {r['max_up_3d']:>+5.1f}% {r['max_down_3d']:>+6.1f}% {ok:>3} {r['category']:>18}  {r['event']}")

# === CATEGORY SUMMARY ===
print("\n\n" + "=" * 90)
print("IMPACT BY CATEGORY (sorted by avg absolute 3D move)")
print("=" * 90)
cats = rdf.groupby('category').agg(
    count=('chg_3d', 'count'),
    avg_0d=('chg_0d', 'mean'),
    avg_3d=('chg_3d', 'mean'),
    avg_7d=('chg_7d', 'mean'),
    avg_abs_3d=('chg_3d', lambda x: x.abs().mean()),
    median_abs_3d=('chg_3d', lambda x: x.abs().median()),
    correct_pct=('correct', lambda x: x.dropna().mean() * 100 if x.dropna().any() else float('nan')),
).round(2)

print(f"\n{'Category':>20} {'N':>3} {'Avg0D':>7} {'Avg3D':>7} {'Avg7D':>7} {'|Avg3D|':>7} {'|Med3D|':>7} {'Predict':>8}")
print("-" * 78)
for cat, row in cats.sort_values('avg_abs_3d', ascending=False).iterrows():
    cr = f"{row['correct_pct']:.0f}%" if pd.notna(row['correct_pct']) else "N/A"
    print(f"{cat:>20} {row['count']:>3.0f} {row['avg_0d']:>+6.2f}% {row['avg_3d']:>+6.2f}% {row['avg_7d']:>+6.2f}% {row['avg_abs_3d']:>6.2f}% {row['median_abs_3d']:>6.2f}% {cr:>8}")

# === DIRECTION ANALYSIS (BULLISH vs BEARISH events) ===
print("\n\n" + "=" * 90)
print("BULLISH vs BEARISH NEWS -- Does Direction Match?")
print("=" * 90)
for exp in ['BULLISH', 'BEARISH']:
    subset = rdf[rdf['expected'] == exp]
    correct = subset['correct'].sum()
    total = len(subset)
    avg_3d = subset['chg_3d'].mean()
    avg_7d = subset['chg_7d'].mean()
    print(f"\n  {exp} events ({total} total):")
    print(f"    Prediction accuracy: {correct:.0f}/{total} = {correct/total*100:.1f}%")
    print(f"    Avg 3D move: {avg_3d:+.2f}%")
    print(f"    Avg 7D move: {avg_7d:+.2f}%")
    print(f"    Individual results:")
    for _, r in subset.iterrows():
        ok = 'Y' if r['correct'] else 'N'
        print(f"      [{ok}] {r['date']} {r['chg_3d']:>+6.1f}% (7D:{r['chg_7d']:>+6.1f}%) {r['event']}")

# === TOP MOVERS ===
print("\n\n" + "=" * 90)
print("TOP 10 BIGGEST 3-DAY MOVES")
print("=" * 90)
top = rdf.reindex(rdf['chg_3d'].abs().sort_values(ascending=False).index).head(10)
for _, r in top.iterrows():
    print(f"  {r['date']} {r['chg_3d']:>+6.1f}% (7D:{r['chg_7d']:>+6.1f}%) [{r['category']}] {r['event']}")

# === KEY INSIGHT: Whipsaw events ===
print("\n\n" + "=" * 90)
print("WHIPSAW EVENTS (large intraday range but small net move)")
print("= Events where max_up - max_down > 5% but |3D change| < 3%")
print("=" * 90)
rdf['range_3d'] = rdf['max_up_3d'] - rdf['max_down_3d']
whipsaw = rdf[(rdf['range_3d'] > 5) & (rdf['chg_3d'].abs() < 3)]
if len(whipsaw) > 0:
    for _, r in whipsaw.iterrows():
        print(f"  {r['date']} net={r['chg_3d']:>+5.1f}% range={r['range_3d']:.1f}% [{r['category']}] {r['event']}")
else:
    print("  None found with these thresholds")

# Try relaxed threshold
whipsaw2 = rdf[(rdf['range_3d'] > 4) & (rdf['chg_3d'].abs() < 4)]
if len(whipsaw2) > len(whipsaw):
    print("\n  Relaxed (range>4%, |net|<4%):")
    for _, r in whipsaw2.iterrows():
        print(f"  {r['date']} net={r['chg_3d']:>+5.1f}% range={r['range_3d']:.1f}% [{r['category']}] {r['event']}")

# === PERSISTENCE: Do moves continue or reverse? ===
print("\n\n" + "=" * 90)
print("MOVE PERSISTENCE: 3D vs 7D (does the move continue?)")
print("=" * 90)
rdf['same_dir_7d'] = np.sign(rdf['chg_3d']) == np.sign(rdf['chg_7d'])
rdf['amplified_7d'] = rdf['chg_7d'].abs() > rdf['chg_3d'].abs()
for cat in ['WAR/GEOPOLITICAL', 'TRUMP/POLITICS', 'FED/MACRO', 'ETF', 'REGULATION']:
    subset = rdf[rdf['category'] == cat]
    if len(subset) == 0:
        continue
    same = subset['same_dir_7d'].mean() * 100
    amp = subset[subset['same_dir_7d']]['amplified_7d'].mean() * 100 if subset['same_dir_7d'].any() else 0
    print(f"  {cat:>20}: {same:.0f}% same direction at 7D | {amp:.0f}% amplified")

# === TRADING STRATEGY IMPLICATIONS ===
print("\n\n" + "=" * 90)
print("STRATEGY IMPLICATIONS FOR OUR MODEL")
print("=" * 90)

# How many events cause >5% moves (would destroy any model)
big = rdf[rdf['chg_3d'].abs() > 5]
print(f"\n  Events causing >5% 3D move: {len(big)}/{len(rdf)} ({len(big)/len(rdf)*100:.0f}%)")
print(f"  Events causing >10% 3D move: {len(rdf[rdf['chg_3d'].abs() > 10])}/{len(rdf)}")

# Most dangerous categories
print(f"\n  Most volatile categories (avg |3D| > 5%):")
dangerous = cats[cats['avg_abs_3d'] > 5]
for cat, row in dangerous.iterrows():
    print(f"    {cat}: avg |{row['avg_abs_3d']:.1f}%| per event")

# Average event frequency
dates = pd.to_datetime(rdf['date'])
gaps = dates.diff().dropna().dt.days
print(f"\n  Event frequency: avg every {gaps.mean():.0f} days (median {gaps.median():.0f} days)")
print(f"  Longest gap: {gaps.max():.0f} days")
print(f"  Shortest gap: {gaps.min():.0f} days")

# Save full results
os.makedirs('experiments', exist_ok=True)
rdf.to_csv('experiments/news_impact_analysis.csv', index=False)
with open('experiments/news_impact_analysis.json', 'w') as f:
    json.dump({
        'events': results,
        'summary': {
            'total_events': len(rdf),
            'prediction_accuracy': float(rdf['correct'].dropna().mean() * 100),
            'avg_abs_3d_move': float(rdf['chg_3d'].abs().mean()),
            'biggest_categories': cats.sort_values('avg_abs_3d', ascending=False).head(3).index.tolist(),
        }
    }, f, indent=2, default=str)

print("\nSaved to experiments/news_impact_analysis.csv + .json")
