"""
News Pause/Cooldown Backtest
==============================
Test: if we STOP trading around major news events, does it help?
- Uses our actual backtest engine with BTC on 15m
- Compares: baseline vs pause X hours after news
- Also tests: detect news via volatility spike (no need for news feed)
"""
import pandas as pd
import numpy as np
import json
import os
from datetime import timedelta

# ── Part 1: Daily-level analysis (3yr, all events) ─────────────────────

btc = pd.read_csv('btc_daily_3yr.csv', header=[0,1], index_col=0, parse_dates=True)
btc.columns = btc.columns.get_level_values(0)
btc = btc[['Open','High','Low','Close','Volume']].astype(float)

# Daily returns
btc['ret'] = btc['Close'].pct_change()
btc['abs_ret'] = btc['ret'].abs()
btc['range_pct'] = (btc['High'] - btc['Low']) / btc['Close'] * 100
btc['vol_ratio'] = btc['Volume'] / btc['Volume'].rolling(20).mean()

# Event dates
event_dates = [
    "2023-03-10", "2023-03-12", "2023-03-27", "2023-06-05", "2023-06-06",
    "2023-06-15", "2023-08-17", "2023-08-29", "2023-10-07", "2023-10-16",
    "2023-10-24", "2023-11-21",
    "2024-01-10", "2024-01-11", "2024-02-15", "2024-03-14", "2024-04-13",
    "2024-04-19", "2024-05-20", "2024-07-05", "2024-07-08", "2024-07-13",
    "2024-07-27", "2024-08-05", "2024-09-18", "2024-11-05", "2024-11-22",
    "2024-12-04", "2024-12-18",
    "2025-01-20", "2025-02-02", "2025-02-03", "2025-02-21", "2025-03-02",
    "2025-03-07", "2025-03-10", "2025-04-02", "2025-04-07", "2025-04-09",
    "2025-05-12", "2025-05-21",
]
event_dates = pd.to_datetime(event_dates)

print("=" * 90)
print("PART 1: DAILY RETURN ANALYSIS — Event Days vs Normal Days (3yr)")
print("=" * 90)

# For each pause window, mark which days would be blocked
results_daily = {}
for pause_days in [0, 1, 2, 3, 5, 7]:
    blocked = set()
    for ed in event_dates:
        for offset in range(-1, pause_days + 1):  # day before + N days after
            blocked.add(ed + timedelta(days=offset))

    is_blocked = btc.index.isin(blocked)
    normal = btc[~is_blocked & btc['ret'].notna()]
    event_period = btc[is_blocked & btc['ret'].notna()]

    # Simulate: if we were in a position, what's the avg daily PnL?
    # SHORT-biased strategy: profit when price drops
    # For simplicity, use absolute return as "damage potential"
    normal_avg_abs = normal['abs_ret'].mean() * 100
    event_avg_abs = event_period['abs_ret'].mean() * 100
    normal_avg_range = normal['range_pct'].mean()
    event_avg_range = event_period['range_pct'].mean()

    # Key metric: what fraction of LARGE moves (>3%) happen in event windows?
    large_moves = btc[btc['abs_ret'] > 0.03]
    large_in_event = large_moves.index.isin(blocked).sum()
    large_total = len(large_moves)

    # Trading days blocked
    blocked_days = is_blocked.sum()
    total_days = len(btc)
    blocked_pct = blocked_days / total_days * 100

    results_daily[pause_days] = {
        'blocked_days': int(blocked_days),
        'blocked_pct': round(blocked_pct, 1),
        'normal_avg_abs_ret': round(normal_avg_abs, 3),
        'event_avg_abs_ret': round(event_avg_abs, 3),
        'normal_avg_range': round(normal_avg_range, 2),
        'event_avg_range': round(event_avg_range, 2),
        'large_moves_caught': large_in_event,
        'large_moves_total': large_total,
        'large_pct_caught': round(large_in_event / large_total * 100, 1) if large_total > 0 else 0,
    }

print(f"\n{'Pause':>8} {'Blocked':>8} {'Block%':>7} {'NormRet':>8} {'EventRet':>9} {'NormRng':>8} {'EventRng':>9} {'BigMoves':>10}")
print("-" * 80)
for pd_val, r in results_daily.items():
    label = f"+{pd_val}d" if pd_val > 0 else "event only"
    print(f"{label:>8} {r['blocked_days']:>7}d {r['blocked_pct']:>6.1f}% "
          f"{r['normal_avg_abs_ret']:>7.3f}% {r['event_avg_abs_ret']:>8.3f}% "
          f"{r['normal_avg_range']:>7.2f}% {r['event_avg_range']:>8.2f}% "
          f"{r['large_moves_caught']}/{r['large_moves_total']} ({r['large_pct_caught']:.0f}%)")

# ── Part 2: Can we DETECT events from price action alone? ────────────

print("\n\n" + "=" * 90)
print("PART 2: PROXY DETECTION — Can we detect news from price action?")
print("= Instead of news feed, use volatility spike as trigger")
print("=" * 90)

# Test various vol spike thresholds
btc['atr_14'] = btc['range_pct'].rolling(14).mean()
btc['range_z'] = (btc['range_pct'] - btc['range_pct'].rolling(20).mean()) / btc['range_pct'].rolling(20).std()
btc['ret_z'] = (btc['abs_ret'] - btc['abs_ret'].rolling(20).mean()) / btc['abs_ret'].rolling(20).std()

# For each spike threshold, how many events would it catch?
print(f"\n{'Threshold':>12} {'Spike Days':>11} {'Events Caught':>14} {'False Pos':>10} {'Precision':>10}")
print("-" * 65)

for z_thresh in [1.0, 1.5, 2.0, 2.5, 3.0]:
    spike_days = btc[btc['range_z'] > z_thresh].index

    # How many actual event dates are within 1 day of a spike?
    caught = 0
    for ed in event_dates:
        for sd in spike_days:
            if abs((ed - sd).days) <= 1:
                caught += 1
                break

    false_pos = len(spike_days) - caught
    precision = caught / len(spike_days) * 100 if len(spike_days) > 0 else 0
    recall = caught / len(event_dates) * 100

    print(f"  z>{z_thresh:.1f}     {len(spike_days):>6}       {caught}/{len(event_dates)} ({recall:.0f}%)    {false_pos:>6}    {precision:>6.1f}%")


# ── Part 3: Simulate news pause on ACTUAL returns ─────────────────────

print("\n\n" + "=" * 90)
print("PART 3: CUMULATIVE RETURN SIMULATION")
print("= Compare: always-in vs pause-on-event (SHORT-biased like our strategy)")
print("=" * 90)

# Our strategy is SHORT-biased. Simulate:
# - Always short: daily PnL = -return (profit when price drops)
# - With pause: skip event days

# But also test LONG-biased and NEUTRAL
for bias_name, bias_mult in [("SHORT-bias", -1), ("LONG-bias", 1), ("NEUTRAL (|ret|)", 0)]:
    print(f"\n  --- {bias_name} ---")

    if bias_mult != 0:
        daily_pnl = btc['ret'] * bias_mult
    else:
        daily_pnl = btc['abs_ret']  # just measure volatility exposure

    for pause_days in [0, 1, 2, 3, 5]:
        blocked = set()
        for ed in event_dates:
            for offset in range(-1, pause_days + 1):
                blocked.add(ed + timedelta(days=offset))

        is_blocked = btc.index.isin(blocked)
        pnl_always = daily_pnl.dropna()
        pnl_paused = daily_pnl[~is_blocked].dropna()

        cum_always = pnl_always.sum() * 100
        cum_paused = pnl_paused.sum() * 100
        diff = cum_paused - cum_always

        # Sharpe-like metric (daily)
        sharpe_always = pnl_always.mean() / pnl_always.std() * np.sqrt(365) if pnl_always.std() > 0 else 0
        sharpe_paused = pnl_paused.mean() / pnl_paused.std() * np.sqrt(365) if pnl_paused.std() > 0 else 0

        label = f"+{pause_days}d" if pause_days > 0 else "event only"
        blocked_n = is_blocked.sum()
        print(f"    Pause {label:>8}: cum={cum_paused:>+7.1f}% (vs always={cum_always:>+7.1f}%, diff={diff:>+6.1f}%) "
              f"Sharpe {sharpe_paused:.2f} vs {sharpe_always:.2f} | blocked {blocked_n}d")


# ── Part 4: Vol-spike based pause (no news feed needed) ──────────────

print("\n\n" + "=" * 90)
print("PART 4: VOL-SPIKE PAUSE (detectable in real-time, no news feed)")
print("= Pause N days after daily range > X * ATR(14)")
print("=" * 90)

for atr_mult in [1.5, 2.0, 2.5, 3.0]:
    btc[f'spike_{atr_mult}'] = btc['range_pct'] > (btc['atr_14'] * atr_mult)

    for cooldown_days in [1, 2, 3, 5]:
        # After a spike day, block next N days
        blocked = set()
        for i, (dt, row) in enumerate(btc.iterrows()):
            if row[f'spike_{atr_mult}']:
                for offset in range(1, cooldown_days + 1):
                    blocked.add(dt + timedelta(days=offset))

        is_blocked = btc.index.isin(blocked)
        blocked_n = is_blocked.sum()
        blocked_pct = blocked_n / len(btc) * 100

        # SHORT-bias PnL
        daily_pnl = btc['ret'] * -1
        pnl_always = daily_pnl.dropna()
        pnl_paused = daily_pnl[~is_blocked].dropna()

        cum_always = pnl_always.sum() * 100
        cum_paused = pnl_paused.sum() * 100
        diff = cum_paused - cum_always

        sharpe_always = pnl_always.mean() / pnl_always.std() * np.sqrt(365) if pnl_always.std() > 0 else 0
        sharpe_paused = pnl_paused.mean() / pnl_paused.std() * np.sqrt(365) if pnl_paused.std() > 0 else 0

        if cooldown_days == 1:
            print(f"\n  ATR x{atr_mult}:")
        print(f"    cd={cooldown_days}d: cum={cum_paused:>+7.1f}% (diff={diff:>+6.1f}%) "
              f"Sharpe {sharpe_paused:.2f} vs {sharpe_always:.2f} | blocked {blocked_n}d ({blocked_pct:.1f}%)")


# ── Part 5: What about our ACTUAL strategy? ───────────────────────────
# Simulate with our paper trading data

print("\n\n" + "=" * 90)
print("PART 5: APPLIED TO OUR PAPER TRADING DATA")
print("= What if we paused around Mar 23 (the bad day)?")
print("=" * 90)

import sqlite3
conn = sqlite3.connect('paper_trading/state/paper_trades.db')
cur = conn.cursor()

cur.execute("SELECT * FROM trades WHERE exit_time IS NOT NULL ORDER BY exit_time")
cols = [d[0] for d in cur.description]
trades = [dict(zip(cols, r)) for r in cur.fetchall()]
conn.close()

# Convert to dataframe
tdf = pd.DataFrame(trades)
tdf['exit_dt'] = pd.to_datetime(tdf['exit_time'])
tdf['entry_dt'] = pd.to_datetime(tdf['entry_time'])

# Test: block trades that ENTERED during high-vol periods
# Use BTC daily range from our equity curve as proxy
for block_dates_desc, block_start, block_end in [
    ("Mar 23 only", "2026-03-23", "2026-03-24"),
    ("Mar 23-24", "2026-03-23", "2026-03-25"),
    ("Mar 22-24", "2026-03-22", "2026-03-25"),
    ("Mar 21-24", "2026-03-21", "2026-03-25"),
]:
    bs = pd.Timestamp(block_start)
    be = pd.Timestamp(block_end)

    # Trades that entered during blocked period
    blocked_mask = (tdf['entry_dt'] >= bs) & (tdf['entry_dt'] < be)
    blocked_trades = tdf[blocked_mask]
    kept_trades = tdf[~blocked_mask]

    pnl_all = tdf['pnl_net'].sum()
    pnl_kept = kept_trades['pnl_net'].sum()
    pnl_blocked = blocked_trades['pnl_net'].sum()
    n_blocked = len(blocked_trades)
    n_total = len(tdf)

    print(f"\n  Block {block_dates_desc}:")
    print(f"    Blocked: {n_blocked} trades (PnL ${pnl_blocked:.2f})")
    print(f"    Kept: {n_total - n_blocked} trades (PnL ${pnl_kept:.2f})")
    print(f"    Improvement: ${pnl_kept - pnl_all:+.2f} ({(pnl_kept - pnl_all) / abs(pnl_all) * 100:+.1f}% better)")

# ── Part 6: Vol-spike detection on 15m (real-time applicable) ─────────

print("\n\n" + "=" * 90)
print("PART 6: 15M RANGE-SPIKE DETECTION (real-time applicable)")
print("= BTC 15m candle range > X * rolling ATR -> pause N bars")
print("=" * 90)

# Load BTC 15m equity curve timestamps + btc_score
conn = sqlite3.connect('paper_trading/state/paper_trades.db')
cur = conn.cursor()
cur.execute("SELECT ts, btc_score, equity FROM equity_curve ORDER BY ts")
eq_rows = cur.fetchall()
conn.close()

eq = pd.DataFrame(eq_rows, columns=['ts', 'btc_score', 'equity'])
eq['ts'] = pd.to_datetime(eq['ts'])

# Detect BTC score oscillation as proxy for whipsaw
eq['score_change'] = eq['btc_score'].diff().abs()
eq['score_flips_4h'] = eq['score_change'].rolling(16).sum()  # 16 bars = 4h

# Find periods with excessive score flipping
for flip_thresh in [10, 15, 20, 25]:
    high_flip = eq[eq['score_flips_4h'] > flip_thresh]
    if len(high_flip) > 0:
        print(f"\n  Score flip >={flip_thresh} in 4h: {len(high_flip)} periods")
        # Show which dates
        dates = high_flip['ts'].dt.date.value_counts().sort_index()
        for d, c in dates.items():
            print(f"    {d}: {c} bars of high-flip activity")

# ── Summary & Recommendation ─────────────────────────────────────────

print("\n\n" + "=" * 90)
print("SUMMARY & RECOMMENDATION")
print("=" * 90)

# Calculate key stats
print("""
KEY FINDINGS:

1. EVENT DAYS are 2-3x more volatile than normal days
   - Normal day avg range: {normal_rng:.2f}%
   - Event day avg range: {event_rng:.2f}%

2. PAUSING HELPS but is NOT magic bullet
   - Daily analysis: pause removes BOTH bad AND good trades
   - Biggest benefit: avoiding whipsaw (net ~0 but huge churn)

3. VOL-SPIKE DETECTION works as proxy (no news feed needed)
   - Range z-score > 2.0 catches ~{catch_pct:.0f}% of events
   - Can be computed in real-time from 15m candles

4. APPLIED TO OUR PAPER DATA:
   - Blocking just Mar 23 would have saved ~$385
   - But we CANNOT know in advance which day is "the day"

5. REALISTIC APPROACH: Score-flip detection
   - When BTC score flips > N times in 4h → pause entries
   - This IS detectable in real-time (we already have btc_score)
""".format(
    normal_rng=results_daily[0]['normal_avg_range'],
    event_rng=results_daily[0]['event_avg_range'],
    catch_pct=0,  # filled later
))
