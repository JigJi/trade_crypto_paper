"""Analyze top 5 winners vs bottom 5 losers in paper trading."""
import sqlite3
from collections import defaultdict
from datetime import datetime

conn = sqlite3.connect('paper_trading/state/paper_trades.db')
conn.row_factory = sqlite3.Row

since = '2026-03-17T15:06:00'
rows = conn.execute('SELECT * FROM trades WHERE entry_time >= ? ORDER BY exit_time', (since,)).fetchall()

coin_data = defaultdict(list)
for r in rows:
    coin_data[r['coin']].append(dict(r))

coin_summary = []
for coin, trades in coin_data.items():
    pnl = sum(t['pnl_net'] for t in trades)
    coin_summary.append((coin, pnl, trades))

coin_summary.sort(key=lambda x: x[1], reverse=True)
top5 = coin_summary[:5]
bot5 = coin_summary[-5:]

def analyze_coin(coin, trades):
    n = len(trades)
    wins = sum(1 for t in trades if t['pnl_net'] > 0)
    pnl = sum(t['pnl_net'] for t in trades)

    long_t = [t for t in trades if t['direction'] == 1]
    short_t = [t for t in trades if t['direction'] == -1]
    long_pnl = sum(t['pnl_net'] for t in long_t)
    short_pnl = sum(t['pnl_net'] for t in short_t)
    long_wr = sum(1 for t in long_t if t['pnl_net'] > 0) / len(long_t) * 100 if long_t else 0
    short_wr = sum(1 for t in short_t if t['pnl_net'] > 0) / len(short_t) * 100 if short_t else 0

    exits = defaultdict(lambda: {'n': 0, 'pnl': 0.0})
    for t in trades:
        exits[t['exit_reason']]['n'] += 1
        exits[t['exit_reason']]['pnl'] += t['pnl_net']

    avg_bars = sum(t['bars_held'] for t in trades) / n
    scores = [t['btc_score_entry'] for t in trades if t['btc_score_entry'] is not None]
    avg_score = sum(scores) / len(scores) if scores else 0
    abs_scores = [abs(s) for s in scores]
    avg_abs_score = sum(abs_scores) / len(abs_scores) if abs_scores else 0

    best = max(trades, key=lambda t: t['pnl_net'])
    worst = min(trades, key=lambda t: t['pnl_net'])

    # TP rate
    tp_trades = [t for t in trades if 'TP' in (t['exit_reason'] or '') and t['exit_reason'] != 'SL']
    tp_rate = len(tp_trades) / n * 100 if n else 0
    tp_pnl = sum(t['pnl_net'] for t in tp_trades)

    # SIGNAL_FLIP stats
    flip_trades = [t for t in trades if t['exit_reason'] == 'SIGNAL_FLIP']
    flip_rate = len(flip_trades) / n * 100 if n else 0
    flip_pnl = sum(t['pnl_net'] for t in flip_trades)

    # SL stats
    sl_trades = [t for t in trades if t['exit_reason'] == 'SL']
    sl_pnl = sum(t['pnl_net'] for t in sl_trades)

    print(f'  Trades: {n} | Wins: {wins} | WR: {wins/n*100:.1f}% | PnL: ${pnl:+.2f}')
    print(f'  LONG:  {len(long_t)}t, WR {long_wr:.0f}%, PnL ${long_pnl:+.2f}')
    print(f'  SHORT: {len(short_t)}t, WR {short_wr:.0f}%, PnL ${short_pnl:+.2f}')
    print(f'  Avg bars: {avg_bars:.1f} | Avg |score|: {avg_abs_score:.1f} | Avg score: {avg_score:+.1f}')
    print(f'  TP rate: {tp_rate:.0f}% ({len(tp_trades)}t, ${tp_pnl:+.1f}) | FLIP rate: {flip_rate:.0f}% ({len(flip_trades)}t, ${flip_pnl:+.1f}) | SL: {len(sl_trades)}t (${sl_pnl:+.1f})')
    print(f'  Best:  ${best["pnl_net"]:+.2f} ({"L" if best["direction"]==1 else "S"}, {best["exit_reason"]}, {best["bars_held"]}bars)')
    print(f'  Worst: ${worst["pnl_net"]:+.2f} ({"L" if worst["direction"]==1 else "S"}, {worst["exit_reason"]}, {worst["bars_held"]}bars)')

    print(f'  --- Trade Log ---')
    for t in sorted(trades, key=lambda x: x['entry_time']):
        d = 'L' if t['direction'] == 1 else 'S'
        print(f'    {t["entry_time"][:16]} {d} {t["bars_held"]:>3}bars score={t["btc_score_entry"]:>+5.1f} pnl=${t["pnl_net"]:>+8.2f} exit={t["exit_reason"]}')


print('=' * 70)
print('TOP 5 WINNERS')
print('=' * 70)
for coin, pnl, trades in top5:
    v3 = ['BTC','XRP','ADA','DOT','SUI','FIL','RENDER','BEAT','PIXEL','NEAR','AXS','SOL','ETH','1000BONK','ARB','ARIA','BARD','BANANAS31','PIPPIN']
    v4 = ['OGN','SAHARA','ASTER','LTC','ZRO','NAORIS','1000PEPE','JCT','DEGO','HYPE','PENGU','LINK']
    ver = 'v3' if coin in v3 else 'v4' if coin in v4 else 'v5'
    print(f'\n--- {coin} [{ver}] (PnL: ${pnl:+.2f}) ---')
    analyze_coin(coin, trades)

print(f'\n{"=" * 70}')
print('BOTTOM 5 LOSERS')
print('=' * 70)
for coin, pnl, trades in bot5:
    ver = 'v3' if coin in v3 else 'v4' if coin in v4 else 'v5'
    print(f'\n--- {coin} [{ver}] (PnL: ${pnl:+.2f}) ---')
    analyze_coin(coin, trades)

# === Cross-comparison ===
print(f'\n{"=" * 70}')
print('CROSS COMPARISON: TOP 5 vs BOTTOM 5')
print('=' * 70)

def group_stats(group):
    all_t = []
    for _, _, trades in group:
        all_t.extend(trades)
    n = len(all_t)
    wins = sum(1 for t in all_t if t['pnl_net'] > 0)
    pnl = sum(t['pnl_net'] for t in all_t)
    avg_bars = sum(t['bars_held'] for t in all_t) / n
    abs_scores = [abs(t['btc_score_entry']) for t in all_t if t['btc_score_entry'] is not None]
    avg_abs_score = sum(abs_scores) / len(abs_scores) if abs_scores else 0
    tp = sum(1 for t in all_t if 'TP' in (t['exit_reason'] or '') and t['exit_reason'] != 'SL')
    sl = sum(1 for t in all_t if t['exit_reason'] == 'SL')
    flip = sum(1 for t in all_t if t['exit_reason'] == 'SIGNAL_FLIP')
    long_t = [t for t in all_t if t['direction'] == 1]
    short_t = [t for t in all_t if t['direction'] == -1]
    long_pnl = sum(t['pnl_net'] for t in long_t)
    short_pnl = sum(t['pnl_net'] for t in short_t)
    long_wr = sum(1 for t in long_t if t['pnl_net'] > 0) / len(long_t) * 100 if long_t else 0
    short_wr = sum(1 for t in short_t if t['pnl_net'] > 0) / len(short_t) * 100 if short_t else 0
    flip_pnl = sum(t['pnl_net'] for t in all_t if t['exit_reason'] == 'SIGNAL_FLIP')
    tp_pnl = sum(t['pnl_net'] for t in all_t if 'TP' in (t['exit_reason'] or ''))
    return {
        'n': n, 'wr': wins/n*100, 'pnl': pnl, 'avg_bars': avg_bars,
        'avg_abs_score': avg_abs_score,
        'tp': tp, 'tp_pct': tp/n*100, 'tp_pnl': tp_pnl,
        'sl': sl, 'sl_pct': sl/n*100,
        'flip': flip, 'flip_pct': flip/n*100, 'flip_pnl': flip_pnl,
        'long_n': len(long_t), 'long_wr': long_wr, 'long_pnl': long_pnl,
        'short_n': len(short_t), 'short_wr': short_wr, 'short_pnl': short_pnl,
    }

top_s = group_stats(top5)
bot_s = group_stats(bot5)

metrics = [
    ('Trades', 'n', 'd'),
    ('WR%', 'wr', '.1f'),
    ('PnL', 'pnl', '+.2f'),
    ('Avg bars', 'avg_bars', '.1f'),
    ('Avg |score|', 'avg_abs_score', '.1f'),
    ('TP rate%', 'tp_pct', '.0f'),
    ('TP PnL', 'tp_pnl', '+.1f'),
    ('FLIP rate%', 'flip_pct', '.0f'),
    ('FLIP PnL', 'flip_pnl', '+.1f'),
    ('SL rate%', 'sl_pct', '.0f'),
    ('LONG WR%', 'long_wr', '.0f'),
    ('LONG PnL', 'long_pnl', '+.2f'),
    ('SHORT WR%', 'short_wr', '.0f'),
    ('SHORT PnL', 'short_pnl', '+.2f'),
]

print(f'\n  {"Metric":<15} {"TOP 5":>12} {"BOTTOM 5":>12} {"Delta":>12}')
print(f'  {"-"*55}')
for label, key, fmt in metrics:
    tv = top_s[key]
    bv = bot_s[key]
    delta = tv - bv
    print(f'  {label:<15} {tv:>12{fmt}} {bv:>12{fmt}} {delta:>+12{fmt}}')

conn.close()
