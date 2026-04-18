"""
Mission 017: Win/Loss Streak Dynamics & Serial Correlation
==========================================================
สมมติฐาน: ถ้าเทรดมี serial correlation (hot hand หรือ mean reversion)
ควรปรับ position size ตาม streak history ได้

Experiments:
  EXP1: Basic streak statistics -- distribution ของ win/loss streaks
  EXP2: Autocorrelation test -- serial correlation ในผลเทรด
  EXP3: Post-streak performance -- ผลเทรดหลัง winning/losing streak
  EXP4: Streak by coin -- streaks cluster ภายในเหรียญเดียว หรือ cross-portfolio?
  EXP5: Time-gap analysis -- ระยะห่างระหว่างเทรดส่งผลต่อผลลัพธ์?
  EXP6: Drawdown anatomy -- consecutive loss runs, recovery time, max drawdown depth
  EXP7: Streak-based sizing simulation -- ลอง size up/down ตาม streak จะเป็นยังไง?
"""

import sys, json, logging
import numpy as np, pandas as pd
from pathlib import Path
from datetime import datetime, timedelta
from scipy import stats

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import backtest_15m_btc_led_alts as bt
from signal_core import compute_btc_composite_score_v6

logging.basicConfig(level=logging.WARNING)

# ──────────────────────────────────────────────
# V6 Config
# ──────────────────────────────────────────────
V6_CASCADE_MULT = 1.1
V6_LIQ_W = 8.0
V6_TICK_W = 8.0
V6_TICK_THR = 3
V6_SL = 25.0
V6_TP = 20.0
V6_COOLDOWN = 4
V6_THRESHOLD = 3.0

COINS = ["BTCUSDT", "XRPUSDT", "ADAUSDT", "DOTUSDT", "SUIUSDT", "FILUSDT"]
OOS_START = "2025-01-01"
OOS_END = "2026-03-26"

print("=" * 60)
print("Mission 017: Win/Loss Streak Dynamics & Serial Correlation")
print("=" * 60)

# ──────────────────────────────────────────────
# 1. Load BTC data + v6 score
# ──────────────────────────────────────────────
print("\n[1] Loading BTC data...")
btc_ohlcv = bt.fetch_binance_15m("BTCUSDT", years=3)
if "date_time" in btc_ohlcv.columns:
    btc_ohlcv = btc_ohlcv.rename(columns={"date_time": "ts"})

db_data = bt.load_btc_db_data()
btc_df = bt.build_btc_features(btc_ohlcv, db_data)

btc_score = compute_btc_composite_score_v6(
    btc_df, cascade_mult=V6_CASCADE_MULT,
    liq_w=V6_LIQ_W, tick_w=V6_TICK_W, tick_net_thr=V6_TICK_THR)
btc_score_ts = pd.Series(btc_score.values, index=btc_df["ts"].values, name="btc_score")

# ──────────────────────────────────────────────
# 2. Run v6 backtest on all coins
# ──────────────────────────────────────────────
print("\n[2] Running v6 backtest on 6 coins...")
all_trades = []
for symbol in COINS:
    coin = symbol.replace("USDT", "")
    ohlcv = bt.fetch_binance_15m(symbol, years=3)
    if "date_time" in ohlcv.columns:
        ohlcv = ohlcv.rename(columns={"date_time": "ts"})
    alt_df = bt.build_alt_technicals(ohlcv)
    oos = alt_df[(alt_df["ts"] >= OOS_START) & (alt_df["ts"] <= OOS_END)].copy()

    signals, alt_merged = bt.generate_btc_led_signal(
        btc_score_ts, oos,
        threshold=V6_THRESHOLD, use_alt_pa_filter=False)
    trades = bt.run_backtest(
        alt_merged, signals,
        sl_atr_mult=V6_SL, tp_atr_mult=V6_TP,
        cooldown_bars=V6_COOLDOWN, max_hold_bars=96)
    if len(trades) > 0:
        trades["coin"] = coin
        all_trades.append(trades)
    print(f"  {coin}: {len(trades)} trades, PnL ${trades['pnl_net'].sum():.0f}")

trades_df = pd.concat(all_trades, ignore_index=True)
trades_df["entry_time"] = pd.to_datetime(trades_df["entry_time"])
trades_df["exit_time"] = pd.to_datetime(trades_df["exit_time"])
trades_df["win"] = (trades_df["pnl_net"] > 0).astype(int)

# Sort chronologically (portfolio-level view)
trades_df = trades_df.sort_values("entry_time").reset_index(drop=True)

baseline_pnl = trades_df["pnl_net"].sum()
baseline_wr = trades_df["win"].mean() * 100
n_trades = len(trades_df)
print(f"\n  BASELINE: {n_trades} trades, WR {baseline_wr:.1f}%, PnL ${baseline_pnl:.0f}")

# ──────────────────────────────────────────────
# Helper: compute streaks
# ──────────────────────────────────────────────
def compute_streaks(outcomes):
    """Given binary outcomes (1=win, 0=loss), return list of (type, length)."""
    streaks = []
    if len(outcomes) == 0:
        return streaks
    current_type = outcomes[0]
    current_len = 1
    for o in outcomes[1:]:
        if o == current_type:
            current_len += 1
        else:
            streaks.append((current_type, current_len))
            current_type = o
            current_len = 1
    streaks.append((current_type, current_len))
    return streaks

def running_streak(outcomes):
    """For each trade, compute the current streak length (+N for wins, -N for losses)."""
    result = []
    streak = 0
    for o in outcomes:
        if o == 1:
            streak = streak + 1 if streak > 0 else 1
        else:
            streak = streak - 1 if streak < 0 else -1
        result.append(streak)
    return result

# ══════════════════════════════════════════════
# EXP1: Basic Streak Statistics
# ══════════════════════════════════════════════
print("\n" + "=" * 60)
print("EXP1: Basic Streak Statistics")
print("=" * 60)

outcomes = trades_df["win"].values
streaks = compute_streaks(outcomes)
win_streaks = [s[1] for s in streaks if s[0] == 1]
loss_streaks = [s[1] for s in streaks if s[0] == 0]

exp1 = {
    "total_streaks": len(streaks),
    "win_streaks": len(win_streaks),
    "loss_streaks": len(loss_streaks),
    "max_win_streak": int(max(win_streaks)),
    "max_loss_streak": int(max(loss_streaks)),
    "avg_win_streak": round(np.mean(win_streaks), 2),
    "avg_loss_streak": round(np.mean(loss_streaks), 2),
    "median_win_streak": int(np.median(win_streaks)),
    "median_loss_streak": int(np.median(loss_streaks)),
}

# Expected streak lengths under independence (geometric distribution)
p_win = trades_df["win"].mean()
expected_win_streak = 1.0 / (1.0 - p_win)
expected_loss_streak = 1.0 / p_win
exp1["expected_win_streak_random"] = round(expected_win_streak, 2)
exp1["expected_loss_streak_random"] = round(expected_loss_streak, 2)

# Distribution of streak lengths
for label, data in [("win", win_streaks), ("loss", loss_streaks)]:
    dist = {}
    for s in data:
        dist[s] = dist.get(s, 0) + 1
    exp1[f"{label}_streak_dist"] = {int(k): v for k, v in sorted(dist.items())}

print(f"  Win streaks:  max={exp1['max_win_streak']}, avg={exp1['avg_win_streak']}, "
      f"expected(random)={exp1['expected_win_streak_random']}")
print(f"  Loss streaks: max={exp1['max_loss_streak']}, avg={exp1['avg_loss_streak']}, "
      f"expected(random)={exp1['expected_loss_streak_random']}")
print(f"  Win streak distribution: {exp1['win_streak_dist']}")
print(f"  Loss streak distribution: {exp1['loss_streak_dist']}")

# ══════════════════════════════════════════════
# EXP2: Autocorrelation Test (Serial Correlation)
# ══════════════════════════════════════════════
print("\n" + "=" * 60)
print("EXP2: Autocorrelation Test")
print("=" * 60)

# Test 1: Runs test (Wald-Wolfowitz)
# Under independence, number of runs follows normal distribution
n_wins = int(outcomes.sum())
n_losses = n_trades - n_wins
n_runs = len(streaks)
expected_runs = 1 + (2 * n_wins * n_losses) / n_trades
var_runs = (2 * n_wins * n_losses * (2 * n_wins * n_losses - n_trades)) / (n_trades**2 * (n_trades - 1))
z_runs = (n_runs - expected_runs) / np.sqrt(var_runs) if var_runs > 0 else 0
p_runs = 2 * (1 - stats.norm.cdf(abs(z_runs)))

# Test 2: Lag-1 autocorrelation of outcomes
outcomes_centered = outcomes - outcomes.mean()
ac_lag1 = np.corrcoef(outcomes_centered[:-1], outcomes_centered[1:])[0, 1]
# SE under null: 1/sqrt(n)
se_ac = 1.0 / np.sqrt(n_trades)
z_ac = ac_lag1 / se_ac
p_ac = 2 * (1 - stats.norm.cdf(abs(z_ac)))

# Test 3: Lag-1 autocorrelation of PnL (continuous)
pnl_vals = trades_df["pnl_net"].values
pnl_centered = pnl_vals - pnl_vals.mean()
ac_pnl_lag1 = np.corrcoef(pnl_centered[:-1], pnl_centered[1:])[0, 1]
z_pnl = ac_pnl_lag1 / se_ac
p_pnl = 2 * (1 - stats.norm.cdf(abs(z_pnl)))

# Test 4: Multi-lag autocorrelation (lags 1-10)
ac_lags = {}
for lag in range(1, 11):
    if len(outcomes) > lag:
        ac = np.corrcoef(outcomes_centered[:-lag], outcomes_centered[lag:])[0, 1]
        ac_lags[lag] = round(ac, 4)

exp2 = {
    "runs_test": {
        "n_runs": n_runs,
        "expected_runs": round(expected_runs, 1),
        "z_stat": round(z_runs, 3),
        "p_value": round(p_runs, 4),
        "interpretation": "positive_correlation" if z_runs < -1.96 else "negative_correlation" if z_runs > 1.96 else "independent"
    },
    "lag1_outcome_autocorr": {
        "autocorrelation": round(ac_lag1, 4),
        "z_stat": round(z_ac, 3),
        "p_value": round(p_ac, 4),
        "significant": p_ac < 0.05
    },
    "lag1_pnl_autocorr": {
        "autocorrelation": round(ac_pnl_lag1, 4),
        "z_stat": round(z_pnl, 3),
        "p_value": round(p_pnl, 4),
        "significant": p_pnl < 0.05
    },
    "multi_lag_autocorr": ac_lags
}

print(f"  Runs test: n_runs={n_runs}, expected={expected_runs:.1f}, z={z_runs:.3f}, p={p_runs:.4f}")
print(f"    -> {exp2['runs_test']['interpretation']}")
print(f"  Lag-1 outcome AC: {ac_lag1:.4f}, z={z_ac:.3f}, p={p_ac:.4f}")
print(f"  Lag-1 PnL AC:     {ac_pnl_lag1:.4f}, z={z_pnl:.3f}, p={p_pnl:.4f}")
print(f"  Multi-lag AC: {ac_lags}")

# ══════════════════════════════════════════════
# EXP3: Post-Streak Performance
# ══════════════════════════════════════════════
print("\n" + "=" * 60)
print("EXP3: Post-Streak Performance")
print("=" * 60)

# Compute running streak for each trade
trades_df["running_streak"] = running_streak(outcomes)
# Previous streak at entry = running_streak of previous trade
trades_df["prev_streak"] = trades_df["running_streak"].shift(1).fillna(0).astype(int)

exp3 = {}

# Bucket by previous streak
streak_buckets = {
    "after_3+_wins": trades_df[trades_df["prev_streak"] >= 3],
    "after_2_wins": trades_df[trades_df["prev_streak"] == 2],
    "after_1_win": trades_df[trades_df["prev_streak"] == 1],
    "after_1_loss": trades_df[trades_df["prev_streak"] == -1],
    "after_2_losses": trades_df[trades_df["prev_streak"] == -2],
    "after_3+_losses": trades_df[trades_df["prev_streak"] <= -3],
}

print(f"\n  {'Bucket':<20s} {'Trades':>7s} {'WR%':>7s} {'Avg PnL':>9s} {'Total PnL':>11s}")
print("  " + "-" * 56)

for bucket_name, bucket_df in streak_buckets.items():
    if len(bucket_df) > 0:
        wr = bucket_df["win"].mean() * 100
        avg_pnl = bucket_df["pnl_net"].mean()
        total_pnl = bucket_df["pnl_net"].sum()
        exp3[bucket_name] = {
            "trades": len(bucket_df),
            "wr": round(wr, 1),
            "avg_pnl": round(avg_pnl, 2),
            "total_pnl": round(total_pnl, 0)
        }
        print(f"  {bucket_name:<20s} {len(bucket_df):>7d} {wr:>6.1f}% ${avg_pnl:>8.2f} ${total_pnl:>10.0f}")

# Statistical test: after 3+ wins vs after 3+ losses
if len(streak_buckets["after_3+_wins"]) > 10 and len(streak_buckets["after_3+_losses"]) > 10:
    t_stat, p_val = stats.ttest_ind(
        streak_buckets["after_3+_wins"]["pnl_net"],
        streak_buckets["after_3+_losses"]["pnl_net"],
        equal_var=False)
    exp3["win_vs_loss_streak_test"] = {
        "t_stat": round(t_stat, 3),
        "p_value": round(p_val, 4),
        "significant": p_val < 0.05
    }
    print(f"\n  After 3+ wins vs 3+ losses: t={t_stat:.3f}, p={p_val:.4f}")

# ══════════════════════════════════════════════
# EXP4: Streak by Coin -- Within-Coin vs Cross-Portfolio
# ══════════════════════════════════════════════
print("\n" + "=" * 60)
print("EXP4: Streak by Coin (per-coin autocorrelation)")
print("=" * 60)

exp4 = {}
for coin in trades_df["coin"].unique():
    coin_trades = trades_df[trades_df["coin"] == coin].sort_values("entry_time")
    coin_outcomes = coin_trades["win"].values
    n_coin = len(coin_outcomes)
    if n_coin < 20:
        continue

    # Per-coin runs test
    coin_streaks = compute_streaks(coin_outcomes)
    n_w = int(coin_outcomes.sum())
    n_l = n_coin - n_w
    n_r = len(coin_streaks)
    er = 1 + (2 * n_w * n_l) / n_coin
    vr = (2 * n_w * n_l * (2 * n_w * n_l - n_coin)) / (n_coin**2 * (n_coin - 1)) if n_coin > 1 else 1
    z_r = (n_r - er) / np.sqrt(vr) if vr > 0 else 0
    p_r = 2 * (1 - stats.norm.cdf(abs(z_r)))

    # Per-coin lag-1 AC
    oc = coin_outcomes - coin_outcomes.mean()
    ac1 = np.corrcoef(oc[:-1], oc[1:])[0, 1] if len(oc) > 1 else 0

    # Per-coin max streaks
    cs = compute_streaks(coin_outcomes)
    max_ws = max([s[1] for s in cs if s[0] == 1], default=0)
    max_ls = max([s[1] for s in cs if s[0] == 0], default=0)

    exp4[coin] = {
        "trades": n_coin,
        "wr": round(coin_outcomes.mean() * 100, 1),
        "max_win_streak": int(max_ws),
        "max_loss_streak": int(max_ls),
        "runs_z": round(z_r, 3),
        "runs_p": round(p_r, 4),
        "lag1_ac": round(ac1, 4),
    }
    sig_marker = "*" if p_r < 0.05 else ""
    print(f"  {coin}: trades={n_coin}, WR={coin_outcomes.mean()*100:.1f}%, "
          f"max_win={max_ws}, max_loss={max_ls}, "
          f"runs_z={z_r:.3f}, p={p_r:.4f}{sig_marker}, AC1={ac1:.4f}")

# ══════════════════════════════════════════════
# EXP5: Time-Gap Analysis (ระยะห่างระหว่างเทรด)
# ══════════════════════════════════════════════
print("\n" + "=" * 60)
print("EXP5: Time-Gap Analysis")
print("=" * 60)

# Need to recompute time gap on sorted trades
trades_df["time_gap_min"] = trades_df["entry_time"].diff().dt.total_seconds() / 60
trades_df["time_gap_min"] = trades_df["time_gap_min"].fillna(0)

# Bucket by time gap
gap_buckets = [
    ("< 1h", 0, 60),
    ("1-4h", 60, 240),
    ("4-12h", 240, 720),
    ("12-24h", 720, 1440),
    ("> 24h", 1440, 1e9),
]

exp5 = {}
print(f"\n  {'Gap Bucket':<12s} {'Trades':>7s} {'WR%':>7s} {'Avg PnL':>9s}")
print("  " + "-" * 37)

for label, lo, hi in gap_buckets:
    mask = (trades_df["time_gap_min"] > lo) & (trades_df["time_gap_min"] <= hi)
    bucket = trades_df[mask]
    if len(bucket) > 5:
        wr = bucket["win"].mean() * 100
        avg_pnl = bucket["pnl_net"].mean()
        exp5[label] = {"trades": len(bucket), "wr": round(wr, 1), "avg_pnl": round(avg_pnl, 2)}
        print(f"  {label:<12s} {len(bucket):>7d} {wr:>6.1f}% ${avg_pnl:>8.2f}")

# Correlation between gap and outcome
valid_gaps = trades_df[trades_df["time_gap_min"] > 0]
if len(valid_gaps) > 10:
    corr_gap_win = np.corrcoef(valid_gaps["time_gap_min"], valid_gaps["win"])[0, 1]
    corr_gap_pnl = np.corrcoef(valid_gaps["time_gap_min"], valid_gaps["pnl_net"])[0, 1]
    exp5["gap_win_corr"] = round(corr_gap_win, 4)
    exp5["gap_pnl_corr"] = round(corr_gap_pnl, 4)
    print(f"\n  Gap-Win correlation: {corr_gap_win:.4f}")
    print(f"  Gap-PnL correlation: {corr_gap_pnl:.4f}")

# ══════════════════════════════════════════════
# EXP6: Drawdown Anatomy
# ══════════════════════════════════════════════
print("\n" + "=" * 60)
print("EXP6: Drawdown Anatomy (consecutive loss analysis)")
print("=" * 60)

# Compute cumulative PnL
trades_df["cum_pnl"] = trades_df["pnl_net"].cumsum()
trades_df["cum_max"] = trades_df["cum_pnl"].cummax()
trades_df["drawdown"] = trades_df["cum_pnl"] - trades_df["cum_max"]

# Find drawdown episodes (periods where we're below peak)
in_dd = False
dd_episodes = []
dd_start_idx = 0
dd_start_pnl = 0

for i, row in trades_df.iterrows():
    if row["drawdown"] < 0 and not in_dd:
        in_dd = True
        dd_start_idx = i
        dd_start_pnl = trades_df.loc[i - 1, "cum_pnl"] if i > 0 else 0
    elif row["drawdown"] >= 0 and in_dd:
        in_dd = False
        dd_depth = trades_df.loc[dd_start_idx:i-1, "drawdown"].min()
        dd_len = i - dd_start_idx
        dd_trades = trades_df.loc[dd_start_idx:i-1]
        dd_losses = (dd_trades["win"] == 0).sum()
        recovery_trade = i  # trade that recovered
        dd_episodes.append({
            "start_idx": int(dd_start_idx),
            "end_idx": int(i),
            "depth": round(dd_depth, 2),
            "n_trades": int(dd_len),
            "n_losses": int(dd_losses),
            "start_time": str(trades_df.loc[dd_start_idx, "entry_time"]),
            "recovery_time": str(row["entry_time"]),
        })

# If still in drawdown at end
if in_dd:
    dd_depth = trades_df.loc[dd_start_idx:, "drawdown"].min()
    dd_len = len(trades_df) - dd_start_idx
    dd_episodes.append({
        "start_idx": int(dd_start_idx),
        "end_idx": int(len(trades_df) - 1),
        "depth": round(dd_depth, 2),
        "n_trades": int(dd_len),
        "n_losses": int((trades_df.loc[dd_start_idx:, "win"] == 0).sum()),
        "start_time": str(trades_df.loc[dd_start_idx, "entry_time"]),
        "recovery_time": "ongoing",
    })

# Top 10 deepest drawdowns
dd_episodes_sorted = sorted(dd_episodes, key=lambda x: x["depth"])
top10_dd = dd_episodes_sorted[:10]

# Loss run analysis
loss_runs = [s[1] for s in streaks if s[0] == 0]
loss_run_impact = []
idx = 0
for stype, slen in streaks:
    if stype == 0:
        run_trades = trades_df.iloc[idx:idx+slen]
        total_loss = run_trades["pnl_net"].sum()
        loss_run_impact.append({"length": slen, "total_loss": round(total_loss, 2)})
    idx += slen

# Group by loss run length
loss_run_by_len = {}
for lr in loss_run_impact:
    l = lr["length"]
    if l not in loss_run_by_len:
        loss_run_by_len[l] = {"count": 0, "total_loss": 0}
    loss_run_by_len[l]["count"] += 1
    loss_run_by_len[l]["total_loss"] += lr["total_loss"]

exp6 = {
    "total_dd_episodes": len(dd_episodes),
    "max_dd_depth": round(min(d["depth"] for d in dd_episodes), 2) if dd_episodes else 0,
    "max_dd_trades": max(d["n_trades"] for d in dd_episodes) if dd_episodes else 0,
    "avg_dd_trades": round(np.mean([d["n_trades"] for d in dd_episodes]), 1) if dd_episodes else 0,
    "top10_drawdowns": top10_dd[:5],  # save top 5 for report
    "loss_run_by_length": {int(k): v for k, v in sorted(loss_run_by_len.items())},
}

print(f"  Total DD episodes: {exp6['total_dd_episodes']}")
print(f"  Max DD depth: ${exp6['max_dd_depth']:.0f}")
print(f"  Max DD trades: {exp6['max_dd_trades']}")
print(f"  Avg DD trades: {exp6['avg_dd_trades']}")
print(f"\n  Loss run impact by length:")
print(f"  {'Length':>6s} {'Count':>6s} {'Total Loss':>12s} {'Avg Loss':>10s}")
print("  " + "-" * 36)
for l in sorted(loss_run_by_len.keys()):
    d = loss_run_by_len[l]
    avg_l = d["total_loss"] / d["count"]
    print(f"  {l:>6d} {d['count']:>6d} ${d['total_loss']:>11.0f} ${avg_l:>9.0f}")

# ══════════════════════════════════════════════
# EXP7: Streak-Based Sizing Simulation
# ══════════════════════════════════════════════
print("\n" + "=" * 60)
print("EXP7: Streak-Based Sizing Simulation")
print("=" * 60)

# Simulate different sizing strategies based on streak
base_pnl_per_trade = trades_df["pnl_net"].values
prev_streaks = trades_df["prev_streak"].values

strategies = {
    "baseline_1x": lambda streak: 1.0,
    "martingale_after_loss": lambda streak: 1.5 if streak <= -2 else 1.0,
    "anti_martingale_hot_hand": lambda streak: 1.5 if streak >= 3 else 1.0,
    "reduce_after_3_losses": lambda streak: 0.5 if streak <= -3 else 1.0,
    "streak_proportional": lambda streak: min(2.0, max(0.5, 1.0 + streak * 0.1)),
    "conservative_dd": lambda streak: 0.7 if streak <= -2 else (1.2 if streak >= 3 else 1.0),
    "fr_neg_inspired": lambda streak: 1.3 if streak >= 2 else (0.8 if streak <= -3 else 1.0),
}

exp7 = {}
print(f"\n  {'Strategy':<30s} {'Total PnL':>11s} {'vs Base':>10s} {'Max DD':>10s} {'Sharpe*':>8s}")
print("  " + "-" * 72)

for strat_name, size_fn in strategies.items():
    sizes = np.array([size_fn(int(s)) for s in prev_streaks])
    adjusted_pnl = base_pnl_per_trade * sizes
    cum = np.cumsum(adjusted_pnl)
    max_dd = np.min(cum - np.maximum.accumulate(cum))
    total = cum[-1]
    sharpe = (adjusted_pnl.mean() / adjusted_pnl.std()) * np.sqrt(252 * 4) if adjusted_pnl.std() > 0 else 0

    delta = total - baseline_pnl
    exp7[strat_name] = {
        "total_pnl": round(total, 0),
        "delta_vs_baseline": round(delta, 0),
        "max_dd": round(max_dd, 0),
        "sharpe": round(sharpe, 2),
    }
    marker = " <--" if strat_name == "baseline_1x" else ""
    print(f"  {strat_name:<30s} ${total:>10.0f} ${delta:>+9.0f} ${max_dd:>9.0f} {sharpe:>7.2f}{marker}")

# ══════════════════════════════════════════════
# SAVE RESULTS
# ══════════════════════════════════════════════
print("\n" + "=" * 60)
print("SAVING RESULTS")
print("=" * 60)

results = {
    "mission": "017_streak_dynamics",
    "date": datetime.utcnow().strftime("%Y-%m-%d"),
    "baseline": {
        "trades": n_trades,
        "wr": round(baseline_wr, 1),
        "pnl": round(baseline_pnl, 0),
        "avg_pnl": round(trades_df["pnl_net"].mean(), 2),
    },
    "exp1_streak_stats": exp1,
    "exp2_autocorrelation": exp2,
    "exp3_post_streak": exp3,
    "exp4_per_coin": exp4,
    "exp5_time_gap": exp5,
    "exp6_drawdown": exp6,
    "exp7_sizing": exp7,
}

# Save JSON
json_path = BASE_DIR / "missions" / "mission_017_streak_dynamics.json"
with open(json_path, "w", encoding="utf-8") as f:
    json.dump(results, f, indent=2, ensure_ascii=False, default=str)
print(f"  Saved: {json_path}")

# ──────────────────────────────────────────────
# Update missions.json
# ──────────────────────────────────────────────
from research.missions import MissionEngine, _get_level

engine = MissionEngine()

# Determine verdict
any_significant = (exp2["runs_test"]["p_value"] < 0.05 or
                   exp2["lag1_outcome_autocorr"]["significant"] or
                   exp2["lag1_pnl_autocorr"]["significant"])
verdict = "significant_serial_correlation" if any_significant else "independent_random"

# Best sizing strategy
best_strat = max(exp7.items(), key=lambda x: x[1]["total_pnl"])

mission_entry = {
    "mission_id": "mission_017_streak_dynamics",
    "date": datetime.utcnow().strftime("%Y-%m-%d"),
    "type": "risk_management",
    "title": "Win/Loss Streak Dynamics & Serial Correlation",
    "description": "Analyze serial correlation in trades, streak patterns, drawdown anatomy, "
                   "and test streak-based position sizing strategies",
    "difficulty": "hard",
    "xp_reward": 100,
    "status": "completed",
    "target": "streak_serial_correlation",
    "started_at": datetime.utcnow().isoformat(),
    "finished_at": datetime.utcnow().isoformat(),
    "result": {
        "success": True,
        "verdict": verdict,
        "runs_test_z": exp2["runs_test"]["z_stat"],
        "runs_test_p": exp2["runs_test"]["p_value"],
        "lag1_ac_outcome": exp2["lag1_outcome_autocorr"]["autocorrelation"],
        "lag1_ac_pnl": exp2["lag1_pnl_autocorr"]["autocorrelation"],
        "max_win_streak": exp1["max_win_streak"],
        "max_loss_streak": exp1["max_loss_streak"],
        "max_dd_depth": exp6["max_dd_depth"],
        "best_sizing_strategy": best_strat[0],
        "best_sizing_pnl": best_strat[1]["total_pnl"],
        "best_sizing_delta": best_strat[1]["delta_vs_baseline"],
    },
    "insight": (f"Runs test z={exp2['runs_test']['z_stat']:.3f} (p={exp2['runs_test']['p_value']:.4f}), "
                f"lag-1 AC={exp2['lag1_outcome_autocorr']['autocorrelation']:.4f}. "
                f"Max win streak={exp1['max_win_streak']}, max loss streak={exp1['max_loss_streak']}. "
                f"Max DD=${exp6['max_dd_depth']:.0f}. "
                f"Best sizing: {best_strat[0]} (${best_strat[1]['delta_vs_baseline']:+.0f} vs baseline)."),
    "tags": ["risk_management", "serial_correlation", "streak", "position_sizing", "drawdown",
             "autocorrelation", "runs_test"],
}

engine._data["missions"].append(mission_entry)
engine._data["meta"]["total_xp"] += mission_entry["xp_reward"]
engine._data["meta"]["current_streak"] += 1
engine._data["meta"]["longest_streak"] = max(
    engine._data["meta"]["longest_streak"],
    engine._data["meta"]["current_streak"])
engine._data["meta"]["last_mission_date"] = mission_entry["date"]
lvl, _ = _get_level(engine._data["meta"]["total_xp"])
engine._data["meta"]["level"] = lvl
engine._save()
print(f"  Updated missions.json: +100 XP, total={engine._data['meta']['total_xp']}")

print("\n" + "=" * 60)
print("Mission 017 COMPLETE!")
print("=" * 60)
