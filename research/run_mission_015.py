"""
Mission 015: Pre-Cascade BTC Fingerprint
=========================================
Hypothesis: BTC exhibits identifiable price action patterns 1-3 hours BEFORE
a liquidation cascade fires. If we can detect pre-cascade conditions, we can
improve entry timing or predict cascade quality.

Questions:
1. What does BTC price action look like N bars before cascade events?
2. Are there volume, range, or momentum patterns that predict cascades?
3. Can pre-cascade features predict post-cascade trade quality?
4. Does a pre-cascade "readiness" score improve v6 entry?

Methodology:
- Load BTC OHLCV + liquidation cascade events
- Extract features from N bars BEFORE each cascade
- Compare pre-cascade bars vs random (non-cascade) bars
- Test predictive power via backtest overlay
"""

import sys, json, logging, warnings
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta
from scipy import stats

warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import backtest_15m_btc_led_alts as bt
from signal_core import compute_btc_composite_score_v6, build_btc_features

# ===========================================================================
# CONFIG
# ===========================================================================
OOS_START = "2025-01-01"
OOS_END = "2026-03-31"
PRE_BARS = [1, 2, 4, 8, 12]  # bars before cascade to analyze (15m each)
CASCADE_MULT = 1.1  # v6 default
RESULTS = {}


def _compute_rsi(prices, period=14):
    """Compute RSI indicator."""
    delta = prices.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss.clip(lower=1e-10)
    return 100 - (100 / (1 + rs))

# ===========================================================================
# LOAD DATA
# ===========================================================================
log.info("=" * 70)
log.info("MISSION 015: Pre-Cascade BTC Fingerprint")
log.info("=" * 70)

log.info("\n[1] Loading BTC OHLCV + liquidation data...")
btc_ohlcv = bt.fetch_binance_15m("BTCUSDT", years=3)
if "date_time" in btc_ohlcv.columns:
    btc_ohlcv = btc_ohlcv.rename(columns={"date_time": "ts"})

db_data = bt.load_btc_db_data()
btc_df = build_btc_features(btc_ohlcv, db_data)

# Filter OOS
oos_mask = (btc_df["ts"] >= OOS_START) & (btc_df["ts"] <= OOS_END)
df = btc_df[oos_mask].copy().reset_index(drop=True)
log.info(f"  OOS period: {df['ts'].iloc[0]} to {df['ts'].iloc[-1]}")
log.info(f"  Total bars: {len(df)}")

# ===========================================================================
# IDENTIFY CASCADE EVENTS
# ===========================================================================
log.info("\n[2] Identifying cascade events...")

lt = df["liq_total"].fillna(0)
ln = df["liq_net"].fillna(0)
lt_ma = df.get("liq_total_ma", lt.rolling(24).mean()).fillna(1)

# Cascade: liq_total > lt_ma * cascade_mult
df["cascade"] = (lt > (lt_ma * CASCADE_MULT)) & (lt > 0)
df["cascade_magnitude"] = np.where(df["cascade"], lt / lt_ma.clip(lower=0.01), 0)

# Cascade direction: net liq direction
df["cascade_dir"] = np.where(
    df["cascade"] & (ln > 0), 1,   # net SHORT liq -> bullish
    np.where(df["cascade"] & (ln < 0), -1, 0)  # net LONG liq -> bearish
)

n_cascade = df["cascade"].sum()
n_bull = (df["cascade_dir"] == 1).sum()
n_bear = (df["cascade_dir"] == -1).sum()
log.info(f"  Cascade bars: {n_cascade} ({100*n_cascade/len(df):.1f}% of bars)")
log.info(f"  Bullish cascades: {n_bull}, Bearish: {n_bear}")

RESULTS["cascade_count"] = int(n_cascade)
RESULTS["cascade_pct"] = round(100 * n_cascade / len(df), 1)

# ===========================================================================
# COMPUTE PRE-CASCADE FEATURES
# ===========================================================================
log.info("\n[3] Computing pre-cascade features...")

# Price action features for every bar
df["ret_1"] = df["close"].pct_change(1)
df["ret_4"] = df["close"].pct_change(4)   # 1h return
df["ret_8"] = df["close"].pct_change(8)   # 2h return
df["ret_12"] = df["close"].pct_change(12)  # 3h return
df["range_pct"] = (df["high"] - df["low"]) / df["close"]
df["body_pct"] = abs(df["close"] - df["open"]) / df["close"]
df["upper_wick"] = (df["high"] - df[["close", "open"]].max(axis=1)) / df["close"]
df["lower_wick"] = (df[["close", "open"]].min(axis=1) - df["low"]) / df["close"]

# Volatility features
df["range_ma8"] = df["range_pct"].rolling(8).mean()
df["range_ratio"] = df["range_pct"] / df["range_ma8"].clip(lower=1e-8)
df["range_std8"] = df["range_pct"].rolling(8).std()
df["range_z"] = (df["range_pct"] - df["range_ma8"]) / df["range_std8"].clip(lower=1e-8)

# Momentum features
df["rsi_14"] = _compute_rsi(df["close"], 14)
df["mom_4"] = df["close"] - df["close"].shift(4)
df["mom_8"] = df["close"] - df["close"].shift(8)

# Volume features (if available)
if "volume" in df.columns:
    df["vol_ma8"] = df["volume"].rolling(8).mean()
    df["vol_ratio"] = df["volume"] / df["vol_ma8"].clip(lower=1)
    has_volume = True
else:
    df["vol_ratio"] = 1.0
    has_volume = False
    log.info("  (no volume column, skipping volume features)")

# Bollinger Bandwidth (volatility compression/expansion)
df["bb_ma20"] = df["close"].rolling(20).mean()
df["bb_std20"] = df["close"].rolling(20).std()
df["bb_bandwidth"] = (2 * df["bb_std20"]) / df["bb_ma20"].clip(lower=1)

# OI and funding if available
has_oi = "oi_value" in df.columns
has_funding = "funding_rate" in df.columns

log.info(f"  Features computed. has_volume={has_volume}, has_oi={has_oi}, has_funding={has_funding}")

# ===========================================================================
# EXP1: PRE-CASCADE vs RANDOM BAR COMPARISON
# ===========================================================================
log.info("\n[4] EXP1: Pre-cascade vs random bar comparison...")

# Get cascade event indices (first bar of each cascade episode)
cascade_indices = []
in_cascade = False
for i in range(len(df)):
    if df["cascade"].iloc[i] and not in_cascade:
        cascade_indices.append(i)
        in_cascade = True
    elif not df["cascade"].iloc[i]:
        in_cascade = False

log.info(f"  Unique cascade episodes: {len(cascade_indices)}")

# Features to compare
features = ["ret_1", "ret_4", "ret_8", "range_pct", "body_pct",
            "range_z", "range_ratio", "bb_bandwidth", "rsi_14"]
if has_volume:
    features.append("vol_ratio")

# For each lookback, compare pre-cascade bar features vs random
exp1_results = {}
for lb in PRE_BARS:
    pre_cascade_feats = {}
    random_feats = {}

    for feat in features:
        pre_vals = []
        for idx in cascade_indices:
            pre_idx = idx - lb
            if pre_idx >= 0 and not np.isnan(df[feat].iloc[pre_idx]):
                pre_vals.append(df[feat].iloc[pre_idx])

        # Random bars (non-cascade, not within 12 bars of cascade)
        cascade_set = set()
        for idx in cascade_indices:
            for j in range(max(0, idx-12), min(len(df), idx+4)):
                cascade_set.add(j)

        random_vals = df.loc[~df.index.isin(cascade_set), feat].dropna().values

        if len(pre_vals) > 10 and len(random_vals) > 10:
            pre_mean = np.mean(pre_vals)
            rand_mean = np.mean(random_vals)
            t_stat, p_val = stats.ttest_ind(pre_vals, random_vals)

            pre_cascade_feats[feat] = {
                "pre_mean": round(float(pre_mean), 6),
                "random_mean": round(float(rand_mean), 6),
                "diff_pct": round(100 * (pre_mean - rand_mean) / max(abs(rand_mean), 1e-8), 1),
                "t_stat": round(float(t_stat), 2),
                "p_val": round(float(p_val), 4),
                "n_pre": len(pre_vals),
                "n_random": len(random_vals),
            }

    exp1_results[f"lb_{lb}"] = pre_cascade_feats

# Print results
log.info(f"\n  {'Feature':>15} | {'LB':>3} | {'Pre-Casc':>10} | {'Random':>10} | {'Diff%':>7} | {'t-stat':>7} | {'p-val':>6}")
log.info(f"  {'-'*15}-+-{'-'*3}-+-{'-'*10}-+-{'-'*10}-+-{'-'*7}-+-{'-'*7}-+-{'-'*6}")

for feat in features:
    for lb in [1, 4, 8]:
        key = f"lb_{lb}"
        if key in exp1_results and feat in exp1_results[key]:
            r = exp1_results[key][feat]
            sig = "***" if r["p_val"] < 0.001 else "**" if r["p_val"] < 0.01 else "*" if r["p_val"] < 0.05 else ""
            log.info(f"  {feat:>15} | {lb:>3} | {r['pre_mean']:>10.6f} | {r['random_mean']:>10.6f} | {r['diff_pct']:>6.1f}% | {r['t_stat']:>7.2f} | {r['p_val']:>5.4f} {sig}")

RESULTS["exp1_pre_vs_random"] = exp1_results

# ===========================================================================
# EXP2: PRE-CASCADE FINGERPRINT COMPOSITE SCORE
# ===========================================================================
log.info("\n[5] EXP2: Building pre-cascade fingerprint score...")

# Based on EXP1 results, build a composite "cascade readiness" score
# Score each bar on how much it looks like a "pre-cascade" bar

# Feature directions (based on hypothesis and web research):
# - Range compression (low range_z) -> building pressure -> cascade imminent
# - Price trending strongly (high abs ret_4/8) -> approaching liquidation levels
# - Volume declining (low vol_ratio) -> calm before storm
# - Bandwidth narrowing (low bb_bandwidth) -> squeeze -> breakout/cascade
# - RSI extreme -> positions overextended -> liquidation imminent

def compute_readiness_score(row):
    """Score how 'ready' a bar is for an upcoming cascade."""
    score = 0.0

    # 1. Range compression (narrow range = building pressure)
    if not np.isnan(row.get("range_z", np.nan)):
        if row["range_z"] < -0.5:  # below-average range
            score += 1.0
        elif row["range_z"] < 0:
            score += 0.5

    # 2. Strong directional move in past 1h (approaching liquidation prices)
    if not np.isnan(row.get("ret_4", np.nan)):
        if abs(row["ret_4"]) > 0.005:  # >0.5% move in 1h
            score += 1.5
        elif abs(row["ret_4"]) > 0.003:
            score += 0.75

    # 3. Bandwidth squeeze (low BB bandwidth)
    if not np.isnan(row.get("bb_bandwidth", np.nan)):
        if row["bb_bandwidth"] < 0.02:  # tight bands
            score += 1.0
        elif row["bb_bandwidth"] < 0.03:
            score += 0.5

    # 4. RSI extreme (overextended positions)
    if not np.isnan(row.get("rsi_14", np.nan)):
        if row["rsi_14"] > 70 or row["rsi_14"] < 30:
            score += 1.5
        elif row["rsi_14"] > 65 or row["rsi_14"] < 35:
            score += 0.5

    # 5. Volume declining (calm before storm)
    if has_volume and not np.isnan(row.get("vol_ratio", np.nan)):
        if row["vol_ratio"] < 0.7:
            score += 1.0
        elif row["vol_ratio"] < 0.9:
            score += 0.5

    return score

df["readiness_score"] = df.apply(compute_readiness_score, axis=1)

# Analyze: does readiness_score N bars before cascade predict cascade quality?
log.info("\n  Readiness score distribution:")
log.info(f"  Overall mean: {df['readiness_score'].mean():.2f}")

# Score at pre-cascade bars vs random
for lb in [1, 2, 4, 8]:
    pre_scores = []
    for idx in cascade_indices:
        pre_idx = idx - lb
        if pre_idx >= 0:
            pre_scores.append(df["readiness_score"].iloc[pre_idx])

    cascade_set = set()
    for idx in cascade_indices:
        for j in range(max(0, idx-12), min(len(df), idx+4)):
            cascade_set.add(j)
    rand_scores = df.loc[~df.index.isin(cascade_set), "readiness_score"].values

    t, p = stats.ttest_ind(pre_scores, rand_scores) if len(pre_scores) > 10 else (0, 1)
    log.info(f"  LB-{lb}: pre-cascade={np.mean(pre_scores):.2f}, random={np.mean(rand_scores):.2f}, t={t:.2f}, p={p:.4f}")

RESULTS["exp2_readiness"] = {
    "overall_mean": round(float(df["readiness_score"].mean()), 2),
    "cascade_episodes": len(cascade_indices),
}

# ===========================================================================
# EXP3: PRE-CASCADE READINESS vs TRADE OUTCOME
# ===========================================================================
log.info("\n[6] EXP3: Does pre-cascade readiness predict trade quality?")

# Run v6 backtest on BTC and check if readiness score at entry predicts PnL
from paper_trading.config import COIN_CONFIGS

# Compute v6 score
v6_score = compute_btc_composite_score_v6(df)
v6_score_ts = pd.Series(v6_score.values, index=df["ts"].values, name="btc_score")

# Backtest BTC
btc_alt_ohlcv = bt.fetch_binance_15m("BTCUSDT", years=3)
if "date_time" in btc_alt_ohlcv.columns:
    btc_alt_ohlcv = btc_alt_ohlcv.rename(columns={"date_time": "ts"})
alt_df = bt.build_alt_technicals(btc_alt_ohlcv)
oos_mask_alt = (alt_df["ts"] >= OOS_START) & (alt_df["ts"] <= OOS_END)

cfg = COIN_CONFIGS.get("BTC", {})
signals, alt_merged = bt.generate_btc_led_signal(
    v6_score_ts, alt_df[oos_mask_alt],
    threshold=cfg.get("threshold", 3.0),
    use_alt_pa_filter=cfg.get("use_alt_pa_filter", False))
trades = bt.run_backtest(
    alt_merged, signals,
    sl_atr_mult=25.0,  # v6 params
    tp_atr_mult=20.0,
    cooldown_bars=cfg.get("cooldown_bars", 4))

if len(trades) > 0:
    log.info(f"  BTC trades: {len(trades)}, WR={100*trades['pnl_net'].gt(0).mean():.1f}%, PnL=${trades['pnl_net'].sum():.0f}")

    # Match readiness score to each trade entry
    readiness_ts = pd.Series(df["readiness_score"].values, index=df["ts"].values)

    trade_readiness = []
    for _, t in trades.iterrows():
        entry_ts = t["entry_time"]
        # Find readiness score 1 bar before entry
        mask = df["ts"] <= entry_ts
        if mask.any():
            last_idx = mask.values.nonzero()[0][-1]
            if last_idx > 0:
                r_score = df["readiness_score"].iloc[last_idx - 1]
                trade_readiness.append({
                    "pnl": t["pnl_net"],
                    "readiness": r_score,
                    "exit_reason": t["exit_reason"],
                    "dir": t["dir"],
                    "bars_held": t.get("holding_bars", t.get("bars_held", 0)),
                })

    tr_df = pd.DataFrame(trade_readiness)

    if len(tr_df) > 20:
        # Bucket by readiness score
        tr_df["r_bucket"] = pd.cut(tr_df["readiness"], bins=[-0.1, 1, 2, 3, 5, 10],
                                    labels=["0-1", "1-2", "2-3", "3-5", "5+"])

        log.info(f"\n  {'Readiness':>10} | {'Trades':>6} | {'WR%':>5} | {'Avg PnL':>8} | {'Total':>8}")
        log.info(f"  {'-'*10}-+-{'-'*6}-+-{'-'*5}-+-{'-'*8}-+-{'-'*8}")

        exp3_buckets = {}
        for bucket in ["0-1", "1-2", "2-3", "3-5", "5+"]:
            sub = tr_df[tr_df["r_bucket"] == bucket]
            if len(sub) > 0:
                wr = 100 * sub["pnl"].gt(0).mean()
                avg_pnl = sub["pnl"].mean()
                total_pnl = sub["pnl"].sum()
                log.info(f"  {bucket:>10} | {len(sub):>6} | {wr:>5.1f} | {avg_pnl:>8.2f} | {total_pnl:>8.0f}")
                exp3_buckets[bucket] = {
                    "trades": int(len(sub)),
                    "wr": round(float(wr), 1),
                    "avg_pnl": round(float(avg_pnl), 2),
                    "total_pnl": round(float(total_pnl), 0),
                }

        RESULTS["exp3_readiness_vs_outcome"] = exp3_buckets

        # Correlation
        corr = tr_df["readiness"].corr(tr_df["pnl"])
        log.info(f"\n  Correlation(readiness, pnl) = {corr:.4f}")
        RESULTS["exp3_correlation"] = round(float(corr), 4)
else:
    log.info("  No BTC trades in OOS period")
    RESULTS["exp3_readiness_vs_outcome"] = {}

# ===========================================================================
# EXP4: PRICE TRAJECTORY BEFORE CASCADE (AGGREGATE)
# ===========================================================================
log.info("\n[7] EXP4: Average BTC price trajectory before cascades...")

# For each cascade, compute price return path from -12 bars to +4 bars
trajectories_bull = []
trajectories_bear = []

for idx in cascade_indices:
    if idx < 12 or idx + 4 >= len(df):
        continue

    cascade_price = df["close"].iloc[idx]
    path = []
    for offset in range(-12, 5):
        ret = (df["close"].iloc[idx + offset] - cascade_price) / cascade_price * 100
        path.append(ret)

    if df["cascade_dir"].iloc[idx] == 1:
        trajectories_bull.append(path)
    elif df["cascade_dir"].iloc[idx] == -1:
        trajectories_bear.append(path)

log.info(f"  Bullish cascades with full path: {len(trajectories_bull)}")
log.info(f"  Bearish cascades with full path: {len(trajectories_bear)}")

if len(trajectories_bull) > 10:
    bull_avg = np.mean(trajectories_bull, axis=0)
    bear_avg = np.mean(trajectories_bear, axis=0) if len(trajectories_bear) > 10 else [0]*17

    offsets = list(range(-12, 5))
    log.info(f"\n  {'Offset':>7} | {'Bull(%price)':>12} | {'Bear(%price)':>12}")
    log.info(f"  {'-'*7}-+-{'-'*12}-+-{'-'*12}")
    for i, off in enumerate(offsets):
        marker = " <-- CASCADE" if off == 0 else ""
        b = bull_avg[i]
        br = bear_avg[i] if len(trajectories_bear) > 10 else 0
        log.info(f"  {off:>+7} | {b:>+11.3f}% | {br:>+11.3f}%{marker}")

    RESULTS["exp4_trajectory"] = {
        "bull_path": [round(float(x), 4) for x in bull_avg],
        "bear_path": [round(float(x), 4) for x in bear_avg] if len(trajectories_bear) > 10 else [],
        "offsets": offsets,
        "n_bull": len(trajectories_bull),
        "n_bear": len(trajectories_bear),
    }

# ===========================================================================
# EXP5: VOLATILITY COMPRESSION BEFORE CASCADE
# ===========================================================================
log.info("\n[8] EXP5: Volatility compression -> cascade?")

# Hypothesis: cascades are preceded by volatility compression (squeeze)
# Test: Is BB bandwidth lower than average in the 4 bars before cascade?

compression_before = []
expansion_before = []
random_bandwidth = df.loc[~df.index.isin(set().union(*[range(max(0,i-12), min(len(df),i+4)) for i in cascade_indices])), "bb_bandwidth"].dropna()

for idx in cascade_indices:
    if idx < 4:
        continue
    # Average bandwidth 4 bars before cascade
    pre_bw = df["bb_bandwidth"].iloc[idx-4:idx].mean()
    # Bandwidth at cascade bar
    casc_bw = df["bb_bandwidth"].iloc[idx]

    if not np.isnan(pre_bw) and not np.isnan(casc_bw):
        compression_before.append(pre_bw)
        expansion_before.append(casc_bw)

if len(compression_before) > 10:
    pre_mean = np.mean(compression_before)
    rand_mean = random_bandwidth.mean()
    casc_mean = np.mean(expansion_before)
    t, p = stats.ttest_ind(compression_before, random_bandwidth.values)

    log.info(f"  BB Bandwidth 4-bar avg BEFORE cascade: {pre_mean:.5f}")
    log.info(f"  BB Bandwidth AT cascade bar:           {casc_mean:.5f}")
    log.info(f"  BB Bandwidth random bars:              {rand_mean:.5f}")
    log.info(f"  Compression before cascade? pre < random: {pre_mean < rand_mean} (t={t:.2f}, p={p:.4f})")
    log.info(f"  Expansion at cascade? casc > random:  {casc_mean > rand_mean}")

    RESULTS["exp5_vol_compression"] = {
        "pre_cascade_bw": round(float(pre_mean), 6),
        "at_cascade_bw": round(float(casc_mean), 6),
        "random_bw": round(float(rand_mean), 6),
        "compression_confirmed": bool(pre_mean < rand_mean),
        "expansion_at_cascade": bool(casc_mean > rand_mean),
        "t_stat": round(float(t), 2),
        "p_val": round(float(p), 4),
    }

# ===========================================================================
# EXP6: MULTI-COIN BACKTEST WITH READINESS FILTER
# ===========================================================================
log.info("\n[9] EXP6: v6 backtest with pre-cascade readiness filter...")

coins = ["BTCUSDT", "XRPUSDT", "ADAUSDT", "DOTUSDT", "SUIUSDT", "FILUSDT"]

# Compute readiness-adjusted entry: only enter when readiness > threshold
readiness_ts_series = pd.Series(df["readiness_score"].values, index=df["ts"].values)

# Baseline: normal v6
baseline_trades = []
for symbol in coins:
    coin = symbol.replace("USDT", "")
    ohlcv = bt.fetch_binance_15m(symbol, years=3)
    if "date_time" in ohlcv.columns:
        ohlcv = ohlcv.rename(columns={"date_time": "ts"})
    alt_df_c = bt.build_alt_technicals(ohlcv)
    oos_m = (alt_df_c["ts"] >= OOS_START) & (alt_df_c["ts"] <= OOS_END)

    cfg = COIN_CONFIGS.get(coin, {})
    signals, merged = bt.generate_btc_led_signal(
        v6_score_ts, alt_df_c[oos_m],
        threshold=cfg.get("threshold", 3.0),
        use_alt_pa_filter=cfg.get("use_alt_pa_filter", False))
    t = bt.run_backtest(
        merged, signals,
        sl_atr_mult=25.0, tp_atr_mult=20.0,
        cooldown_bars=cfg.get("cooldown_bars", 4))
    if len(t) > 0:
        t["coin"] = coin
        baseline_trades.append(t)

if baseline_trades:
    bl_df = pd.concat(baseline_trades, ignore_index=True)
    bl_pnl = bl_df["pnl_net"].sum()
    bl_wr = 100 * bl_df["pnl_net"].gt(0).mean()
    bl_n = len(bl_df)
    log.info(f"\n  BASELINE (v6, 6 coins): {bl_n} trades, WR={bl_wr:.1f}%, PnL=${bl_pnl:.0f}")

    # Now test: skip entries where readiness at entry bar < threshold
    for r_thresh in [1.0, 1.5, 2.0, 2.5, 3.0]:
        # Match readiness to trade entry times
        filtered_pnl = 0
        filtered_n = 0
        filtered_wins = 0

        for _, t in bl_df.iterrows():
            entry_ts = t["entry_time"]
            # Find nearest readiness score
            mask = df["ts"] <= entry_ts
            if mask.any():
                last_idx = mask.values.nonzero()[0][-1]
                r_score = df["readiness_score"].iloc[max(0, last_idx - 1)]

                if r_score >= r_thresh:
                    filtered_pnl += t["pnl_net"]
                    filtered_n += 1
                    if t["pnl_net"] > 0:
                        filtered_wins += 1

        f_wr = 100 * filtered_wins / max(filtered_n, 1)
        delta = filtered_pnl - bl_pnl
        log.info(f"  Readiness >= {r_thresh}: {filtered_n} trades ({100*filtered_n/bl_n:.0f}%), WR={f_wr:.1f}%, PnL=${filtered_pnl:.0f} (delta=${delta:+.0f})")

    RESULTS["exp6_baseline"] = {
        "trades": bl_n, "wr": round(bl_wr, 1), "pnl": round(bl_pnl, 0)
    }

# ===========================================================================
# EXP7: WHAT'S DIFFERENT ABOUT HIGH-QUALITY CASCADE BARS?
# ===========================================================================
log.info("\n[10] EXP7: Pre-cascade features of winning vs losing trades...")

if baseline_trades:
    # Separate winners and losers
    winners = bl_df[bl_df["pnl_net"] > 0]
    losers = bl_df[bl_df["pnl_net"] <= 0]

    win_readiness = []
    lose_readiness = []
    win_features = {f: [] for f in ["range_z", "ret_4", "bb_bandwidth", "rsi_14"]}
    lose_features = {f: [] for f in ["range_z", "ret_4", "bb_bandwidth", "rsi_14"]}

    for _, t in winners.iterrows():
        mask = df["ts"] <= t["entry_time"]
        if mask.any():
            idx = mask.values.nonzero()[0][-1]
            if idx > 0:
                win_readiness.append(df["readiness_score"].iloc[idx-1])
                for f in win_features:
                    if f in df.columns:
                        win_features[f].append(df[f].iloc[idx-1])

    for _, t in losers.iterrows():
        mask = df["ts"] <= t["entry_time"]
        if mask.any():
            idx = mask.values.nonzero()[0][-1]
            if idx > 0:
                lose_readiness.append(df["readiness_score"].iloc[idx-1])
                for f in lose_features:
                    if f in df.columns:
                        lose_features[f].append(df[f].iloc[idx-1])

    if len(win_readiness) > 10 and len(lose_readiness) > 10:
        log.info(f"\n  {'Feature':>15} | {'Winners':>10} | {'Losers':>10} | {'t-stat':>7} | {'p-val':>6}")
        log.info(f"  {'-'*15}-+-{'-'*10}-+-{'-'*10}-+-{'-'*7}-+-{'-'*6}")

        # Readiness
        t, p = stats.ttest_ind(win_readiness, lose_readiness)
        log.info(f"  {'readiness':>15} | {np.mean(win_readiness):>10.2f} | {np.mean(lose_readiness):>10.2f} | {t:>7.2f} | {p:>6.4f}")

        exp7_results = {
            "readiness_winners": round(float(np.mean(win_readiness)), 2),
            "readiness_losers": round(float(np.mean(lose_readiness)), 2),
            "readiness_t": round(float(t), 2),
            "readiness_p": round(float(p), 4),
        }

        for f in ["range_z", "ret_4", "bb_bandwidth", "rsi_14"]:
            if len(win_features[f]) > 10 and len(lose_features[f]) > 10:
                wf = [x for x in win_features[f] if not np.isnan(x)]
                lf = [x for x in lose_features[f] if not np.isnan(x)]
                if len(wf) > 10 and len(lf) > 10:
                    t, p = stats.ttest_ind(wf, lf)
                    log.info(f"  {f:>15} | {np.mean(wf):>10.4f} | {np.mean(lf):>10.4f} | {t:>7.2f} | {p:>6.4f}")
                    exp7_results[f"{f}_winners"] = round(float(np.mean(wf)), 4)
                    exp7_results[f"{f}_losers"] = round(float(np.mean(lf)), 4)
                    exp7_results[f"{f}_p"] = round(float(p), 4)

        RESULTS["exp7_winners_vs_losers"] = exp7_results

# ===========================================================================
# SAVE RESULTS
# ===========================================================================
log.info("\n" + "=" * 70)
log.info("SAVING RESULTS...")

# Save JSON
output_path = BASE_DIR / "missions" / "mission_015_pre_cascade_fingerprint.json"
with open(output_path, "w", encoding="utf-8") as f:
    json.dump(RESULTS, f, indent=2, ensure_ascii=False, default=str)
log.info(f"  JSON: {output_path}")

log.info("\nMission 015 complete!")
