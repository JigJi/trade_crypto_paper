"""Coin Selection Ranking for Live Trading.

Ranks all 46 coins by composite score based on paper trading metrics.
Produces Tier A/B/C recommendations.
"""
import sqlite3
from collections import defaultdict
from datetime import datetime

conn = sqlite3.connect('paper_trading/state/paper_trades.db')
conn.row_factory = sqlite3.Row

since = '2026-03-17T15:06:00'
rows = conn.execute('SELECT * FROM trades WHERE entry_time >= ? ORDER BY exit_time', (since,)).fetchall()

# Version mapping
v3 = ['BTC','XRP','ADA','DOT','SUI','FIL','RENDER','BEAT','PIXEL','NEAR','AXS','SOL','ETH','1000BONK','ARB','ARIA','BARD','BANANAS31','PIPPIN']
v4 = ['OGN','SAHARA','ASTER','LTC','ZRO','NAORIS','1000PEPE','JCT','DEGO','HYPE','PENGU','LINK']
v5 = ['FARTCOIN','GALA','AAVE','AVAX','UNI','SEI','DOGE','ONDO','1000SHIB','ICX','BNB','WIF','CRV','TAO','ACX']

def get_ver(coin):
    if coin in v3: return 'v3'
    if coin in v4: return 'v4'
    return 'v5'

# Group by coin
coin_data = defaultdict(list)
for r in rows:
    coin_data[r['coin']].append(dict(r))

# Calculate metrics per coin
coin_metrics = []
for coin, trades in coin_data.items():
    n = len(trades)
    if n == 0:
        continue

    pnl = sum(t['pnl_net'] for t in trades)
    wins = sum(1 for t in trades if t['pnl_net'] > 0)
    wr = wins / n * 100

    # TP rate
    tp_trades = [t for t in trades if 'TP' in (t['exit_reason'] or '') and t['exit_reason'] != 'SL']
    tp_rate = len(tp_trades) / n * 100

    # FLIP rate and FLIP WR
    flip_trades = [t for t in trades if t['exit_reason'] == 'SIGNAL_FLIP']
    flip_rate = len(flip_trades) / n * 100
    flip_wr = sum(1 for t in flip_trades if t['pnl_net'] > 0) / len(flip_trades) * 100 if flip_trades else 50
    flip_pnl = sum(t['pnl_net'] for t in flip_trades)

    # SL rate
    sl_trades = [t for t in trades if t['exit_reason'] == 'SL']
    sl_rate = len(sl_trades) / n * 100

    # Avg BTC score magnitude
    scores = [abs(t['btc_score_entry']) for t in trades if t['btc_score_entry'] is not None]
    avg_abs_score = sum(scores) / len(scores) if scores else 0

    # Avg bars held
    avg_bars = sum(t['bars_held'] for t in trades) / n

    # PnL excluding best trade (robustness)
    best_pnl = max(t['pnl_net'] for t in trades)
    pnl_ex_best = pnl - best_pnl

    # Consistency: days with trades that are profitable
    days = defaultdict(float)
    for t in trades:
        day = t['entry_time'][:10]
        days[day] += t['pnl_net']
    days_traded = len(days)
    days_profitable = sum(1 for d, p in days.items() if p > 0)
    consistency = days_profitable / days_traded * 100 if days_traded > 0 else 0

    # PnL per trade
    pnl_per_trade = pnl / n

    # Direction analysis
    long_t = [t for t in trades if t['direction'] == 1]
    short_t = [t for t in trades if t['direction'] == -1]
    long_wr = sum(1 for t in long_t if t['pnl_net'] > 0) / len(long_t) * 100 if long_t else 0
    short_wr = sum(1 for t in short_t if t['pnl_net'] > 0) / len(short_t) * 100 if short_t else 0

    coin_metrics.append({
        'coin': coin,
        'ver': get_ver(coin),
        'n': n,
        'pnl': pnl,
        'wr': wr,
        'tp_rate': tp_rate,
        'flip_rate': flip_rate,
        'flip_wr': flip_wr,
        'flip_pnl': flip_pnl,
        'sl_rate': sl_rate,
        'avg_abs_score': avg_abs_score,
        'avg_bars': avg_bars,
        'pnl_ex_best': pnl_ex_best,
        'consistency': consistency,
        'pnl_per_trade': pnl_per_trade,
        'days_traded': days_traded,
        'long_wr': long_wr,
        'short_wr': short_wr,
    })

# === Composite Score ===
# Weights: TP rate (25%), low FLIP rate (20%), Consistency (20%), Robustness (20%), WR (15%)
# Normalize each metric to 0-100 scale

def normalize(values, higher_better=True):
    mn, mx = min(values), max(values)
    if mx == mn:
        return [50.0] * len(values)
    if higher_better:
        return [(v - mn) / (mx - mn) * 100 for v in values]
    else:
        return [(mx - v) / (mx - mn) * 100 for v in values]

tp_rates = [m['tp_rate'] for m in coin_metrics]
flip_rates = [m['flip_rate'] for m in coin_metrics]
consistencies = [m['consistency'] for m in coin_metrics]
pnl_ex_bests = [m['pnl_ex_best'] for m in coin_metrics]
wrs = [m['wr'] for m in coin_metrics]

n_tp = normalize(tp_rates, higher_better=True)
n_flip = normalize(flip_rates, higher_better=False)  # lower = better
n_cons = normalize(consistencies, higher_better=True)
n_robust = normalize(pnl_ex_bests, higher_better=True)
n_wr = normalize(wrs, higher_better=True)

for i, m in enumerate(coin_metrics):
    m['score'] = (
        n_tp[i] * 0.25 +
        n_flip[i] * 0.20 +
        n_cons[i] * 0.20 +
        n_robust[i] * 0.20 +
        n_wr[i] * 0.15
    )

# Sort by composite score
coin_metrics.sort(key=lambda x: x['score'], reverse=True)

# === Print Results ===
print('=' * 100)
print('COIN SELECTION RANKING (Paper Trading Since 2026-03-17)')
print('=' * 100)
print(f'{"#":>3} {"Coin":<12} {"Ver":<4} {"Trades":>6} {"PnL":>9} {"WR%":>6} {"TP%":>5} {"FLIP%":>6} {"FlipWR":>6} {"Cons%":>6} {"PnL-Best":>9} {"Score":>7}')
print('-' * 100)

for rank, m in enumerate(coin_metrics, 1):
    tier = 'A' if rank <= 15 else 'B' if rank <= 30 else 'C'
    marker = '  '
    if rank == 16:
        print('-' * 100 + '  << Tier B below >>')
    if rank == 31:
        print('-' * 100 + '  << Tier C below >>')

    print(f'{rank:>3} {m["coin"]:<12} {m["ver"]:<4} {m["n"]:>6} ${m["pnl"]:>+8.2f} {m["wr"]:>5.1f} {m["tp_rate"]:>5.0f} {m["flip_rate"]:>5.0f} {m["flip_wr"]:>6.0f} {m["consistency"]:>5.0f} ${m["pnl_ex_best"]:>+8.2f} {m["score"]:>7.1f}')

# === Tier Summary ===
print(f'\n{"=" * 100}')
print('TIER SUMMARY')
print('=' * 100)

for tier_name, start, end in [('A (Top 15)', 0, 15), ('B (Mid)', 15, 30), ('C (Bottom)', 30, len(coin_metrics))]:
    tier_coins = coin_metrics[start:end]
    if not tier_coins:
        continue
    t_pnl = sum(m['pnl'] for m in tier_coins)
    t_n = sum(m['n'] for m in tier_coins)
    t_wins = sum(m['n'] * m['wr'] / 100 for m in tier_coins)
    t_wr = t_wins / t_n * 100 if t_n else 0
    t_tp = sum(m['tp_rate'] * m['n'] for m in tier_coins) / t_n if t_n else 0
    t_flip = sum(m['flip_rate'] * m['n'] for m in tier_coins) / t_n if t_n else 0
    coins_str = ', '.join(m['coin'] for m in tier_coins)

    print(f'\nTier {tier_name}: {len(tier_coins)} coins')
    print(f'  PnL: ${t_pnl:+.2f} | Trades: {t_n} | WR: {t_wr:.1f}% | TP rate: {t_tp:.0f}% | FLIP rate: {t_flip:.0f}%')
    print(f'  Coins: {coins_str}')

# === What-if: Tier A only ===
print(f'\n{"=" * 100}')
print('WHAT-IF: TRADING TIER A ONLY')
print('=' * 100)
tier_a = coin_metrics[:15]
tier_a_coins = set(m['coin'] for m in tier_a)

all_trades = [dict(r) for r in rows]
tier_a_trades = [t for t in all_trades if t['coin'] in tier_a_coins]
all_n = len(all_trades)
a_n = len(tier_a_trades)

all_pnl = sum(t['pnl_net'] for t in all_trades)
a_pnl = sum(t['pnl_net'] for t in tier_a_trades)
all_wr = sum(1 for t in all_trades if t['pnl_net'] > 0) / all_n * 100
a_wr = sum(1 for t in tier_a_trades if t['pnl_net'] > 0) / a_n * 100 if a_n else 0

print(f'  All 46 coins: {all_n} trades, PnL ${all_pnl:+.2f}, WR {all_wr:.1f}%')
print(f'  Tier A only:  {a_n} trades, PnL ${a_pnl:+.2f}, WR {a_wr:.1f}%')
print(f'  Difference:   {a_n - all_n} trades, PnL ${a_pnl - all_pnl:+.2f}, WR {a_wr - all_wr:+.1f}%')

removed_pnl = all_pnl - a_pnl
removed_n = all_n - a_n
print(f'\n  Removed coins contributed: {removed_n} trades, PnL ${removed_pnl:+.2f}')
if removed_n > 0:
    removed_pnl_per_trade = removed_pnl / removed_n
    print(f'  Removed coins PnL/trade: ${removed_pnl_per_trade:+.4f}')

# === Version breakdown in Tier A ===
print(f'\n  Version mix in Tier A:')
for ver in ['v3', 'v4', 'v5']:
    ver_coins = [m for m in tier_a if m['ver'] == ver]
    if ver_coins:
        print(f'    {ver}: {len(ver_coins)} coins ({", ".join(m["coin"] for m in ver_coins)})')

conn.close()
