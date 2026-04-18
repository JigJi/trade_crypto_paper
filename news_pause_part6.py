import sqlite3, pandas as pd, numpy as np

conn = sqlite3.connect('paper_trading/state/paper_trades.db')
cur = conn.cursor()
cur.execute('SELECT ts, btc_score, equity FROM equity_curve ORDER BY ts')
eq_rows = cur.fetchall()
conn.close()

eq = pd.DataFrame(eq_rows, columns=['ts', 'btc_score', 'equity'])
eq['ts'] = pd.to_datetime(eq['ts'])
eq['score_change'] = eq['btc_score'].diff().abs()
eq['score_flips_4h'] = eq['score_change'].rolling(16).sum()

print('PART 6: BTC SCORE FLIP DETECTION (real-time, no news feed)')
print('=' * 70)
for flip_thresh in [10, 15, 20, 25]:
    high_flip = eq[eq['score_flips_4h'] > flip_thresh]
    if len(high_flip) > 0:
        print(f'\n  Score flip >={flip_thresh} in 4h: {len(high_flip)} periods')
        dates = high_flip['ts'].dt.date.value_counts().sort_index()
        for d, c in dates.items():
            print(f'    {d}: {c} bars of high-flip activity')

# Load trades
conn = sqlite3.connect('paper_trading/state/paper_trades.db')
cur = conn.cursor()
cur.execute('SELECT * FROM trades WHERE exit_time IS NOT NULL')
cols = [d[0] for d in cur.description]
trades = [dict(zip(cols, r)) for r in cur.fetchall()]
conn.close()

tdf = pd.DataFrame(trades)
tdf['entry_dt'] = pd.to_datetime(tdf['entry_time'])

print('\n\nAPPLIED: Block entries when score_flips_4h > threshold')
print('=' * 70)

for flip_thresh in [10, 15, 20]:
    high_flip_times = eq[eq['score_flips_4h'] > flip_thresh]['ts']
    if len(high_flip_times) == 0:
        print(f'\n  Threshold >= {flip_thresh}: No periods detected')
        continue

    # Block entries within 1h of any high-flip period
    blocked_mask = pd.Series(False, index=tdf.index)
    for ft in high_flip_times:
        mask = (tdf['entry_dt'] >= ft - pd.Timedelta(hours=1)) & (tdf['entry_dt'] <= ft + pd.Timedelta(hours=1))
        blocked_mask = blocked_mask | mask

    blocked = tdf[blocked_mask]
    kept = tdf[~blocked_mask]

    total_pnl = tdf['pnl_net'].sum()
    print(f'\n  Threshold >= {flip_thresh}:')
    print(f'    Blocked: {len(blocked)} trades, PnL ${blocked["pnl_net"].sum():.2f}')
    print(f'    Kept: {len(kept)} trades, PnL ${kept["pnl_net"].sum():.2f}')
    print(f'    Improvement: ${kept["pnl_net"].sum() - total_pnl:+.2f}')
    if len(blocked) > 0:
        blocked_wr = (blocked['pnl_net'] > 0).mean() * 100
        kept_wr = (kept['pnl_net'] > 0).mean() * 100
        print(f'    Blocked WR: {blocked_wr:.1f}%')
        print(f'    Kept WR: {kept_wr:.1f}%')

# Also: score oscillation analysis
print('\n\nSCORE OSCILLATION TIMELINE (Mar 22-25)')
print('=' * 70)
mar22 = eq[(eq['ts'] >= '2026-03-22') & (eq['ts'] < '2026-03-26')]
for _, row in mar22.iterrows():
    flip4h = row['score_flips_4h'] if pd.notna(row['score_flips_4h']) else 0
    bar = '#' * int(flip4h / 2)
    print(f'  {row["ts"]} score={row["btc_score"]:>+5.1f} flips4h={flip4h:>5.1f} {bar}')
