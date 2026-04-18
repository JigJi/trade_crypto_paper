"""
Mission 032: Cross-Coin Correlation & Diversification Analysis
================================================================
สมมติฐาน: เพราะทุกเหรียญใช้ BTC composite signal เดียวกัน
→ trades ทุกเหรียญเข้า/ออก พร้อมกัน → ไม่ได้ diversification จริง
→ มี 13 เหรียญแต่อาจเหมือนมีแค่ 2-3 "independent bets"

ถ้าจริง: ลด coins ได้โดยไม่เสีย edge, ลด risk stacking

6 Experiments:
  EXP1: Trade timing overlap — กี่เหรียญเปิด trade พร้อมกัน?
  EXP2: Per-coin return correlation matrix (15m return correlation)
  EXP3: Concurrent position PnL — win/lose พร้อมกันบ่อยแค่ไหน?
  EXP4: Effective N — จำนวน independent bets จริงๆ
  EXP5: Optimal subset — เหรียญไหนให้ diversification สูงสุด?
  EXP6: Risk stacking — worst concurrent drawdown
"""

import sys, json, logging, warnings
from pathlib import Path
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import backtest_15m_btc_led_alts as bt
from paper_trading.config import COMPOSITE_WEIGHTS, V3_EXTRA_WEIGHTS, COIN_CONFIGS
from signal_core import compute_btc_composite_score

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

results = {}

# ══════════════════════════════════════════════════════════════
# Load BTC data & compute score
# ══════════════════════════════════════════════════════════════
log.info("Loading BTC OHLCV + DB data...")
btc_ohlcv = bt.fetch_binance_15m("BTCUSDT", years=3)
if "date_time" in btc_ohlcv.columns:
    btc_ohlcv = btc_ohlcv.rename(columns={"date_time": "ts"})

db_data = bt.load_btc_db_data()
btc_df = bt.build_btc_features(btc_ohlcv, db_data)

btc_score = compute_btc_composite_score(btc_df, COMPOSITE_WEIGHTS, V3_EXTRA_WEIGHTS)
btc_score_ts = pd.Series(btc_score.values, index=btc_df["ts"].values, name="btc_score")

# ══════════════════════════════════════════════════════════════
# Run backtest for all 6 original coins
# ══════════════════════════════════════════════════════════════
log.info("Running backtest for 6 coins (OOS)...")
coins = ["BTCUSDT", "XRPUSDT", "ADAUSDT", "DOTUSDT", "SUIUSDT", "FILUSDT"]
oos_start, oos_end = "2025-01-01", "2026-03-31"

all_trades = []
coin_ohlcv = {}  # store for return correlation

for symbol in coins:
    coin = symbol.replace("USDT", "")
    ohlcv = bt.fetch_binance_15m(symbol, years=3)
    if "date_time" in ohlcv.columns:
        ohlcv = ohlcv.rename(columns={"date_time": "ts"})

    # Store OHLCV for correlation analysis
    oos_ohlcv = ohlcv[(ohlcv["ts"] >= oos_start) & (ohlcv["ts"] <= oos_end)].copy()
    oos_ohlcv["ret_15m"] = oos_ohlcv["close"].pct_change()
    coin_ohlcv[coin] = oos_ohlcv[["ts", "ret_15m", "close"]].copy()

    alt_df = bt.build_alt_technicals(ohlcv)
    oos_mask = (alt_df["ts"] >= oos_start) & (alt_df["ts"] <= oos_end)

    cfg = COIN_CONFIGS.get(coin, {})
    signals, alt_merged = bt.generate_btc_led_signal(
        btc_score_ts, alt_df[oos_mask],
        threshold=cfg.get("threshold", 3.0),
        use_alt_pa_filter=cfg.get("use_alt_pa_filter", False),
        hysteresis_band=cfg.get("hysteresis_band", 0.0))
    trades = bt.run_backtest(alt_merged, signals,
                             sl_atr_mult=cfg.get("sl_atr_mult", 2.5),
                             tp_atr_mult=cfg.get("tp_atr_mult", 4.0),
                             cooldown_bars=cfg.get("cooldown_bars", 4),
                             flip_cd_extra=cfg.get("flip_cd_extra", 0))
    if len(trades) > 0:
        trades["coin"] = coin
        all_trades.append(trades)

trades_df = pd.concat(all_trades, ignore_index=True)
trades_df["entry_time"] = pd.to_datetime(trades_df["entry_time"])
trades_df["exit_time"] = pd.to_datetime(trades_df["exit_time"])
log.info(f"Total trades: {len(trades_df)}")

# ══════════════════════════════════════════════════════════════
# EXP1: Trade Timing Overlap
# ══════════════════════════════════════════════════════════════
log.info("=" * 60)
log.info("EXP1: Trade Timing Overlap")

# For each 15m bar, count how many coins have an open position
all_ts = sorted(btc_df[(btc_df["ts"] >= oos_start) & (btc_df["ts"] <= oos_end)]["ts"].unique())

concurrent_counts = []
for ts in all_ts:
    n_open = 0
    for coin in ["BTC", "XRP", "ADA", "DOT", "SUI", "FIL"]:
        coin_trades = trades_df[trades_df["coin"] == coin]
        in_trade = ((coin_trades["entry_time"] <= ts) & (coin_trades["exit_time"] > ts)).any()
        if in_trade:
            n_open += 1
    concurrent_counts.append(n_open)

concurrent_s = pd.Series(concurrent_counts, index=all_ts)

# Stats
total_bars = len(concurrent_s)
bars_with_trades = (concurrent_s > 0).sum()
pct_in_market = bars_with_trades / total_bars * 100

# Distribution of concurrent positions
conc_dist = concurrent_s.value_counts().sort_index()
conc_dist_pct = (conc_dist / total_bars * 100).round(1)

# Same-bar entries (multiple coins enter on same bar)
entry_bar_counts = trades_df.groupby("entry_time")["coin"].nunique()
multi_entry_bars = (entry_bar_counts > 1).sum()
pct_multi_entry = multi_entry_bars / len(entry_bar_counts) * 100
avg_coins_per_entry = entry_bar_counts.mean()

results["exp1"] = {
    "total_bars": int(total_bars),
    "bars_with_trades": int(bars_with_trades),
    "pct_in_market": round(pct_in_market, 1),
    "concurrent_distribution": {str(k): int(v) for k, v in conc_dist.items()},
    "concurrent_distribution_pct": {str(k): float(v) for k, v in conc_dist_pct.items()},
    "avg_concurrent": round(concurrent_s.mean(), 2),
    "max_concurrent": int(concurrent_s.max()),
    "multi_entry_bars": int(multi_entry_bars),
    "total_entry_bars": int(len(entry_bar_counts)),
    "pct_multi_entry": round(pct_multi_entry, 1),
    "avg_coins_per_entry_bar": round(avg_coins_per_entry, 2),
}

log.info(f"  Bars in market: {bars_with_trades}/{total_bars} ({pct_in_market:.1f}%)")
log.info(f"  Avg concurrent: {concurrent_s.mean():.2f}, Max: {concurrent_s.max()}")
log.info(f"  Multi-coin entry bars: {multi_entry_bars}/{len(entry_bar_counts)} ({pct_multi_entry:.1f}%)")
log.info(f"  Avg coins per entry bar: {avg_coins_per_entry:.2f}")
for k, v in conc_dist_pct.items():
    log.info(f"    {k} coins open: {v}%")

# ══════════════════════════════════════════════════════════════
# EXP2: Per-Coin 15m Return Correlation Matrix
# ══════════════════════════════════════════════════════════════
log.info("=" * 60)
log.info("EXP2: Per-Coin 15m Return Correlation")

# Build return matrix
ret_dfs = []
for coin, df in coin_ohlcv.items():
    s = df.set_index("ts")["ret_15m"].rename(coin)
    ret_dfs.append(s)

ret_matrix = pd.concat(ret_dfs, axis=1).dropna()
corr_matrix = ret_matrix.corr()

# Average pairwise correlation
n_coins = len(corr_matrix)
upper_tri = corr_matrix.where(np.triu(np.ones(corr_matrix.shape), k=1).astype(bool))
avg_corr = upper_tri.stack().mean()
min_corr = upper_tri.stack().min()
max_corr = upper_tri.stack().max()

# Most and least correlated pairs
pair_corrs = []
for i in range(n_coins):
    for j in range(i+1, n_coins):
        c1, c2 = corr_matrix.columns[i], corr_matrix.columns[j]
        pair_corrs.append((c1, c2, corr_matrix.iloc[i, j]))

pair_corrs.sort(key=lambda x: x[2], reverse=True)

results["exp2"] = {
    "correlation_matrix": {c: {c2: round(v, 3) for c2, v in row.items()} for c, row in corr_matrix.iterrows()},
    "avg_pairwise_corr": round(avg_corr, 3),
    "min_corr": round(min_corr, 3),
    "max_corr": round(max_corr, 3),
    "most_correlated": [{"pair": f"{p[0]}-{p[1]}", "corr": round(p[2], 3)} for p in pair_corrs[:3]],
    "least_correlated": [{"pair": f"{p[0]}-{p[1]}", "corr": round(p[2], 3)} for p in pair_corrs[-3:]],
}

log.info(f"  Avg pairwise correlation: {avg_corr:.3f}")
log.info(f"  Range: {min_corr:.3f} to {max_corr:.3f}")
log.info("  Most correlated:")
for p in pair_corrs[:3]:
    log.info(f"    {p[0]}-{p[1]}: {p[2]:.3f}")
log.info("  Least correlated:")
for p in pair_corrs[-3:]:
    log.info(f"    {p[0]}-{p[1]}: {p[2]:.3f}")

# ══════════════════════════════════════════════════════════════
# EXP3: Concurrent Trade Win/Loss Synchronization
# ══════════════════════════════════════════════════════════════
log.info("=" * 60)
log.info("EXP3: Win/Loss Synchronization")

# For trades that overlap in time, do they win/lose together?
trades_df["win"] = trades_df["pnl_net"] > 0

# Group trades by entry_time (same BTC signal = same entry bar)
sync_results = []
for entry_t, group in trades_df.groupby("entry_time"):
    if len(group) < 2:
        continue
    n_win = group["win"].sum()
    n_total = len(group)
    n_lose = n_total - n_win
    # All same outcome?
    all_win = n_win == n_total
    all_lose = n_lose == n_total
    sync_results.append({
        "entry_time": str(entry_t),
        "n_coins": n_total,
        "n_win": int(n_win),
        "n_lose": int(n_lose),
        "all_same": all_win or all_lose,
        "all_win": all_win,
        "all_lose": all_lose,
    })

sync_df = pd.DataFrame(sync_results)
if len(sync_df) > 0:
    pct_all_same = sync_df["all_same"].mean() * 100
    pct_all_win = sync_df["all_win"].mean() * 100
    pct_all_lose = sync_df["all_lose"].mean() * 100
    avg_win_frac = (sync_df["n_win"] / sync_df["n_coins"]).mean()
else:
    pct_all_same = pct_all_win = pct_all_lose = avg_win_frac = 0

results["exp3"] = {
    "multi_coin_entries": len(sync_df),
    "pct_all_same_outcome": round(pct_all_same, 1),
    "pct_all_win": round(pct_all_win, 1),
    "pct_all_lose": round(pct_all_lose, 1),
    "avg_win_fraction": round(avg_win_frac, 3),
}

log.info(f"  Multi-coin entries: {len(sync_df)}")
log.info(f"  All same outcome: {pct_all_same:.1f}%")
log.info(f"  All win: {pct_all_win:.1f}%, All lose: {pct_all_lose:.1f}%")
log.info(f"  Avg win fraction per entry: {avg_win_frac:.3f}")

# ══════════════════════════════════════════════════════════════
# EXP4: Effective N (eigenvalue-based diversification)
# ══════════════════════════════════════════════════════════════
log.info("=" * 60)
log.info("EXP4: Effective Number of Independent Bets")

# Method: eigenvalue decomposition of correlation matrix
# Effective N = (sum of eigenvalues)^2 / sum(eigenvalue^2)
# This gives the "effective number of independent factors"
eigenvalues = np.linalg.eigvalsh(corr_matrix.values)
eigenvalues = np.maximum(eigenvalues, 0)  # numerical stability

effective_n = (eigenvalues.sum() ** 2) / (eigenvalues ** 2).sum()

# Also compute: variance explained by top eigenvector (PC1)
eigenvalues_sorted = sorted(eigenvalues, reverse=True)
total_var = sum(eigenvalues_sorted)
pc1_var_pct = eigenvalues_sorted[0] / total_var * 100
pc2_var_pct = eigenvalues_sorted[1] / total_var * 100 if len(eigenvalues_sorted) > 1 else 0
top2_var_pct = pc1_var_pct + pc2_var_pct

# Diversification ratio: effective_n / actual_n
div_ratio = effective_n / n_coins

results["exp4"] = {
    "n_coins": n_coins,
    "effective_n": round(effective_n, 2),
    "diversification_ratio": round(div_ratio, 3),
    "pc1_variance_pct": round(pc1_var_pct, 1),
    "pc2_variance_pct": round(pc2_var_pct, 1),
    "top2_variance_pct": round(top2_var_pct, 1),
    "eigenvalues": [round(e, 3) for e in eigenvalues_sorted],
}

log.info(f"  Actual coins: {n_coins}")
log.info(f"  Effective N: {effective_n:.2f}")
log.info(f"  Diversification ratio: {div_ratio:.3f} ({div_ratio*100:.0f}% of theoretical)")
log.info(f"  PC1 explains: {pc1_var_pct:.1f}% of variance")
log.info(f"  Top 2 PCs explain: {top2_var_pct:.1f}%")

# ══════════════════════════════════════════════════════════════
# EXP5: Optimal Subset — greedy forward selection
# ══════════════════════════════════════════════════════════════
log.info("=" * 60)
log.info("EXP5: Optimal Coin Subset (greedy forward selection)")

# For each subset size, pick coins that maximize portfolio Sharpe
# using the actual per-coin trade PnL timeseries

# Build daily PnL per coin
trades_df["entry_date"] = trades_df["entry_time"].dt.date
daily_pnl = trades_df.groupby(["coin", "entry_date"])["pnl_net"].sum().unstack(level=0, fill_value=0)

# Forward greedy: start empty, add coin that maximizes Sharpe
all_coin_names = list(daily_pnl.columns)
selected = []
remaining = list(all_coin_names)
selection_order = []

for step in range(len(all_coin_names)):
    best_sharpe = -999
    best_coin = None
    for c in remaining:
        trial = selected + [c]
        port_pnl = daily_pnl[trial].sum(axis=1)
        if port_pnl.std() == 0:
            sharpe = 0
        else:
            sharpe = port_pnl.mean() / port_pnl.std() * np.sqrt(252)
        if sharpe > best_sharpe:
            best_sharpe = sharpe
            best_coin = c
    selected.append(best_coin)
    remaining.remove(best_coin)
    port_pnl = daily_pnl[selected].sum(axis=1)
    total_pnl = port_pnl.sum()
    selection_order.append({
        "step": step + 1,
        "added": best_coin,
        "sharpe": round(best_sharpe, 2),
        "total_pnl": round(total_pnl, 1),
        "coins": list(selected),
    })
    log.info(f"  Step {step+1}: +{best_coin} → Sharpe={best_sharpe:.2f}, PnL=${total_pnl:.0f}")

# Find peak Sharpe
peak_step = max(selection_order, key=lambda x: x["sharpe"])

results["exp5"] = {
    "selection_order": selection_order,
    "peak_sharpe_at": peak_step["step"],
    "peak_sharpe": peak_step["sharpe"],
    "peak_coins": peak_step["coins"],
    "full_portfolio_sharpe": selection_order[-1]["sharpe"],
}

log.info(f"  Peak Sharpe at {peak_step['step']} coins: {peak_step['sharpe']:.2f}")
log.info(f"  Best subset: {peak_step['coins']}")

# ══════════════════════════════════════════════════════════════
# EXP6: Risk Stacking — worst concurrent drawdown
# ══════════════════════════════════════════════════════════════
log.info("=" * 60)
log.info("EXP6: Risk Stacking — Concurrent Drawdown Analysis")

# Build portfolio equity curve (all coins)
port_daily = daily_pnl.sum(axis=1).sort_index()
port_cum = port_daily.cumsum()

# Max drawdown
running_max = port_cum.cummax()
drawdown = port_cum - running_max
max_dd = drawdown.min()
max_dd_date = drawdown.idxmin()

# Worst N-day drawdowns
rolling_5d = port_daily.rolling(5).sum()
worst_5d = rolling_5d.min()
worst_5d_date = rolling_5d.idxmin()

# Per-coin contribution to worst day
worst_day = port_daily.idxmin()
worst_day_pnl = port_daily.min()
worst_day_breakdown = daily_pnl.loc[worst_day].to_dict() if worst_day in daily_pnl.index else {}

# Count "all-red" days (every coin negative)
daily_coin_sign = daily_pnl.apply(lambda x: x > 0)
all_red_days = (~daily_coin_sign.any(axis=1)).sum()  # no coin positive
total_trading_days = len(daily_pnl)

# Concurrent losing streaks
port_daily_win = port_daily > 0
streak_changes = port_daily_win.ne(port_daily_win.shift())
streaks = streak_changes.cumsum()
losing_streaks = port_daily[~port_daily_win].groupby(streaks[~port_daily_win]).agg(["count", "sum"])
if len(losing_streaks) > 0:
    max_losing_streak = int(losing_streaks["count"].max())
    worst_streak_pnl = float(losing_streaks["sum"].min())
else:
    max_losing_streak = 0
    worst_streak_pnl = 0

results["exp6"] = {
    "max_drawdown": round(float(max_dd), 1),
    "max_dd_date": str(max_dd_date),
    "worst_day_pnl": round(float(worst_day_pnl), 1),
    "worst_day_date": str(worst_day),
    "worst_day_breakdown": {k: round(v, 1) for k, v in worst_day_breakdown.items()},
    "worst_5d_rolling": round(float(worst_5d), 1) if not pd.isna(worst_5d) else 0,
    "worst_5d_date": str(worst_5d_date),
    "all_red_days": int(all_red_days),
    "total_trading_days": int(total_trading_days),
    "pct_all_red": round(all_red_days / max(total_trading_days, 1) * 100, 1),
    "max_losing_streak_days": max_losing_streak,
    "worst_streak_pnl": round(worst_streak_pnl, 1),
}

log.info(f"  Max drawdown: ${max_dd:.1f} (at {max_dd_date})")
log.info(f"  Worst day: ${worst_day_pnl:.1f} ({worst_day})")
log.info(f"  Worst 5-day: ${worst_5d:.1f}")
log.info(f"  All-red days: {all_red_days}/{total_trading_days} ({all_red_days/max(total_trading_days,1)*100:.1f}%)")
log.info(f"  Max losing streak: {max_losing_streak} days (${worst_streak_pnl:.1f})")
if worst_day_breakdown:
    log.info(f"  Worst day breakdown:")
    for c, p in sorted(worst_day_breakdown.items(), key=lambda x: x[1]):
        log.info(f"    {c}: ${p:.1f}")

# ══════════════════════════════════════════════════════════════
# SAVE RESULTS
# ══════════════════════════════════════════════════════════════
log.info("=" * 60)
log.info("Saving results...")

output = {
    "mission_id": "mission_032_cross_coin_correlation",
    "date": datetime.utcnow().strftime("%Y-%m-%d"),
    "experiments": results,
}

json_path = BASE_DIR / "missions" / "mission_032_cross_coin_correlation.json"
with open(json_path, "w", encoding="utf-8") as f:
    json.dump(output, f, indent=2, ensure_ascii=False, default=str)

log.info(f"Saved to {json_path}")
log.info("Mission 032 COMPLETE!")
