import pandas as pd
import os

files = [
    'trades_15m_breakout.csv',
    'trades_15m_fvg_structure.csv',
    'trades_15m_rsi_extreme.csv',
    'trades_5m_fvg_structure.csv',
    'trades_15m_ema_trend_follow.csv',
]

for f in files:
    fp = os.path.join('backtest_details', f)
    if not os.path.exists(fp):
        continue
    df = pd.read_csv(fp)
    print(f'=== {f} ({len(df)} trades) ===')

    print('Exit reasons:')
    for reason, grp in df.groupby('exit_reason'):
        cnt = len(grp)
        pct = cnt / len(df) * 100
        avg_pnl = grp['pnl_net'].mean()
        win_rate = (grp['pnl_net'] > 0).mean() * 100
        total_pnl = grp['pnl_net'].sum()
        print(f'  {reason:15s}: {cnt:4d} ({pct:5.1f}%) | Avg PnL: ${avg_pnl:+8.2f} | WR: {win_rate:.1f}% | Total: ${total_pnl:+,.2f}')

    for d, label in [('L', 'Long'), ('S', 'Short')]:
        sub = df[df['dir'] == d]
        if len(sub) > 0:
            avg = sub['pnl_net'].mean()
            wr = (sub['pnl_net'] > 0).mean() * 100
            total = sub['pnl_net'].sum()
            print(f'  {label:15s}: {len(sub):4d} trades | Avg: ${avg:+8.2f} | WR: {wr:.1f}% | Total: ${total:+,.2f}')

    print(f'  Total PnL: ${df["pnl_net"].sum():+,.2f}')
    print()
