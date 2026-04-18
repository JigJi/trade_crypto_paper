"""
Mission 021: Chop Detection & SIGNAL_FLIP Prevention
=====================================================
สมมติฐาน: SIGNAL_FLIP เกิดเมื่อ BTC score สั่นไปมาใกล้ threshold ("chop")
ถ้าตรวจจับ chop ได้ real-time จะลด FLIP losses ได้

Experiments:
  EXP1: SIGNAL_FLIP baseline stats (frequency, PnL, bars_held)
  EXP2: Score trajectory before FLIP — rolling score std dev
  EXP3: Define "Chop Index" = rolling std of BTC score
  EXP4: SIGNAL_FLIP rate by Chop Index quartile
  EXP5: Score velocity at entry — does deceleration predict FLIP?
  EXP6: Chop-based filters (skip/reduce size when chop high)
  EXP7: Walk-forward stability of best chop filter
"""

import sys, json, logging
import numpy as np, pandas as pd
from pathlib import Path
from datetime import datetime

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import backtest_15m_btc_led_alts as bt
from paper_trading.config import COMPOSITE_WEIGHTS, V3_EXTRA_WEIGHTS, COIN_CONFIGS
from paper_trading.strategy import score_ob_combined, score_basis_contrarian, score_tick_liq

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────
COINS = ["BTCUSDT", "XRPUSDT", "ADAUSDT", "DOTUSDT", "SUIUSDT", "FILUSDT"]
OOS_START = "2025-01-01"
OOS_END = "2026-03-31"
RESULTS = {}
STARTED = datetime.utcnow()


def build_btc_score():
    """Build BTC composite score (v3 config)."""
    log.info("Building BTC composite score...")
    btc_ohlcv = bt.fetch_binance_15m("BTCUSDT", years=3)
    if "date_time" in btc_ohlcv.columns:
        btc_ohlcv = btc_ohlcv.rename(columns={"date_time": "ts"})
    db_data = bt.load_btc_db_data()
    btc_df = bt.build_btc_features(btc_ohlcv, db_data)

    btc_score = bt.compute_btc_composite_score(btc_df, COMPOSITE_WEIGHTS)
    btc_score = btc_score + score_ob_combined(btc_df, weight=V3_EXTRA_WEIGHTS["ob_combined"])
    btc_score = btc_score + score_basis_contrarian(btc_df, weight=V3_EXTRA_WEIGHTS["basis_contrarian"])
    btc_score = btc_score + score_tick_liq(btc_df, weight=V3_EXTRA_WEIGHTS["tick_liq"])

    btc_score_ts = pd.Series(btc_score.values, index=btc_df["ts"].values, name="btc_score")
    return btc_score_ts, btc_df


def run_backtest_all(btc_score_ts, chop_series=None, chop_filter_fn=None, label="baseline"):
    """Run backtest across all coins, optionally with chop filter."""
    all_trades = []
    for symbol in COINS:
        coin = symbol.replace("USDT", "")
        ohlcv = bt.fetch_binance_15m(symbol, years=3)
        if "date_time" in ohlcv.columns:
            ohlcv = ohlcv.rename(columns={"date_time": "ts"})
        alt_df = bt.build_alt_technicals(ohlcv)
        oos_mask = (alt_df["ts"] >= OOS_START) & (alt_df["ts"] <= OOS_END)

        cfg = COIN_CONFIGS.get(coin, {})
        signals, alt_merged = bt.generate_btc_led_signal(
            btc_score_ts, alt_df[oos_mask],
            threshold=cfg.get("threshold", 3.0),
            use_alt_pa_filter=cfg.get("use_alt_pa_filter", False))

        # Apply chop filter if given
        if chop_filter_fn is not None and chop_series is not None:
            signals = chop_filter_fn(signals, alt_merged, chop_series)

        trades = bt.run_backtest(
            alt_merged, signals,
            sl_atr_mult=cfg.get("sl_atr_mult", 2.5),
            tp_atr_mult=cfg.get("tp_atr_mult", 4.0),
            cooldown_bars=cfg.get("cooldown_bars", 4))
        if len(trades) > 0:
            trades["coin"] = coin
            all_trades.append(trades)

    if not all_trades:
        return pd.DataFrame()
    return pd.concat(all_trades, ignore_index=True)


def exp1_baseline(trades_df):
    """EXP1: SIGNAL_FLIP baseline stats."""
    log.info("=== EXP1: SIGNAL_FLIP Baseline Stats ===")
    total = len(trades_df)
    flip = trades_df[trades_df["exit_reason"] == "SIGNAL_FLIP"]
    nonflip = trades_df[trades_df["exit_reason"] != "SIGNAL_FLIP"]

    result = {
        "total_trades": total,
        "flip_count": len(flip),
        "flip_pct": round(100 * len(flip) / total, 1) if total > 0 else 0,
        "flip_wr": round(100 * (flip["pnl_net"] > 0).mean(), 1) if len(flip) > 0 else 0,
        "flip_pnl": round(flip["pnl_net"].sum(), 2) if len(flip) > 0 else 0,
        "flip_avg_pnl": round(flip["pnl_net"].mean(), 2) if len(flip) > 0 else 0,
        "flip_avg_bars": round(flip["holding_bars"].mean(), 1) if len(flip) > 0 else 0,
        "nonflip_wr": round(100 * (nonflip["pnl_net"] > 0).mean(), 1) if len(nonflip) > 0 else 0,
        "nonflip_pnl": round(nonflip["pnl_net"].sum(), 2) if len(nonflip) > 0 else 0,
        "nonflip_avg_pnl": round(nonflip["pnl_net"].mean(), 2) if len(nonflip) > 0 else 0,
        "total_pnl": round(trades_df["pnl_net"].sum(), 2),
    }

    # By direction
    for d, dname in [("L", "LONG"), ("S", "SHORT")]:
        fd = flip[flip["dir"] == d]
        result[f"flip_{dname}_count"] = len(fd)
        result[f"flip_{dname}_wr"] = round(100 * (fd["pnl_net"] > 0).mean(), 1) if len(fd) > 0 else 0
        result[f"flip_{dname}_pnl"] = round(fd["pnl_net"].sum(), 2) if len(fd) > 0 else 0

    # By bars_held buckets
    if len(flip) > 0:
        flip = flip.copy()
        flip["bars_bucket"] = pd.cut(flip["holding_bars"], bins=[0, 4, 8, 16, 32, 96],
                                      labels=["1-4", "5-8", "9-16", "17-32", "33-96"])
        bucket_stats = []
        for b in ["1-4", "5-8", "9-16", "17-32", "33-96"]:
            fb = flip[flip["bars_bucket"] == b]
            if len(fb) > 0:
                bucket_stats.append({
                    "bucket": b,
                    "count": len(fb),
                    "wr": round(100 * (fb["pnl_net"] > 0).mean(), 1),
                    "avg_pnl": round(fb["pnl_net"].mean(), 2),
                    "total_pnl": round(fb["pnl_net"].sum(), 2)
                })
        result["bars_held_buckets"] = bucket_stats

    log.info(f"  FLIP: {result['flip_count']} trades ({result['flip_pct']}%), "
             f"WR {result['flip_wr']}%, PnL ${result['flip_pnl']}")
    log.info(f"  Non-FLIP: WR {result['nonflip_wr']}%, PnL ${result['nonflip_pnl']}")
    return result


def exp2_score_trajectory(trades_df, btc_score_ts, btc_df):
    """EXP2: Score trajectory before SIGNAL_FLIP vs before other exits."""
    log.info("=== EXP2: Score Trajectory Before FLIP ===")

    # Build time-indexed score for lookup
    ts_vals = btc_df["ts"].values
    score_vals = btc_score_ts.values

    # For each trade, measure score std dev over the bars_held window
    flip_trades = trades_df[trades_df["exit_reason"] == "SIGNAL_FLIP"].copy()
    other_trades = trades_df[trades_df["exit_reason"] != "SIGNAL_FLIP"].copy()

    def get_entry_score_stats(trade_row):
        """Get score std & range during trade lifetime."""
        entry_t = pd.Timestamp(trade_row["entry_time"])
        exit_t = pd.Timestamp(trade_row["exit_time"])
        mask = (ts_vals >= entry_t.to_datetime64()) & (ts_vals <= exit_t.to_datetime64())
        scores = score_vals[mask]
        if len(scores) < 2:
            return pd.Series({"score_std": np.nan, "score_range": np.nan,
                            "score_crossings": np.nan})
        # Count zero-crossings (how many times score changes sign)
        signs = np.sign(scores)
        crossings = np.sum(np.diff(signs) != 0)
        return pd.Series({
            "score_std": np.std(scores),
            "score_range": np.max(scores) - np.min(scores),
            "score_crossings": crossings
        })

    log.info("  Computing score stats for FLIP trades...")
    flip_stats = flip_trades.apply(get_entry_score_stats, axis=1)
    log.info("  Computing score stats for non-FLIP trades...")
    other_stats = other_trades.apply(get_entry_score_stats, axis=1)

    result = {
        "flip_score_std_mean": round(flip_stats["score_std"].mean(), 3),
        "flip_score_std_median": round(flip_stats["score_std"].median(), 3),
        "flip_score_range_mean": round(flip_stats["score_range"].mean(), 3),
        "flip_score_crossings_mean": round(flip_stats["score_crossings"].mean(), 2),
        "other_score_std_mean": round(other_stats["score_std"].mean(), 3),
        "other_score_std_median": round(other_stats["score_std"].median(), 3),
        "other_score_range_mean": round(other_stats["score_range"].mean(), 3),
        "other_score_crossings_mean": round(other_stats["score_crossings"].mean(), 2),
    }

    log.info(f"  FLIP trades: score_std={result['flip_score_std_mean']:.3f}, "
             f"crossings={result['flip_score_crossings_mean']:.1f}")
    log.info(f"  Other trades: score_std={result['other_score_std_mean']:.3f}, "
             f"crossings={result['other_score_crossings_mean']:.1f}")
    return result


def exp3_chop_index(btc_score_ts, btc_df):
    """EXP3: Define Chop Index = rolling std of BTC score."""
    log.info("=== EXP3: Chop Index Definition ===")

    score_series = pd.Series(btc_score_ts.values, index=btc_df["ts"].values)

    # Test multiple windows
    windows = [4, 8, 12, 16, 24]
    chop_indices = {}
    for w in windows:
        chop = score_series.rolling(w, min_periods=w).std()
        chop_indices[f"chop_{w}"] = chop

    # Also compute score velocity (rate of change)
    score_velocity = score_series.diff(4)  # 4-bar change
    score_accel = score_velocity.diff(4)   # acceleration

    # Create chop DataFrame
    chop_df = pd.DataFrame(chop_indices, index=btc_df["ts"].values)
    chop_df["score_velocity"] = score_velocity.values
    chop_df["score_accel"] = score_accel.values
    chop_df["score"] = score_series.values

    # Correlation between chop windows
    oos_mask = (chop_df.index >= OOS_START) & (chop_df.index <= OOS_END)
    oos_chop = chop_df[oos_mask].dropna()

    result = {
        "windows_tested": windows,
        "chop_stats": {}
    }
    for w in windows:
        col = f"chop_{w}"
        vals = oos_chop[col]
        result["chop_stats"][col] = {
            "mean": round(vals.mean(), 3),
            "median": round(vals.median(), 3),
            "p25": round(vals.quantile(0.25), 3),
            "p75": round(vals.quantile(0.75), 3),
            "p90": round(vals.quantile(0.90), 3),
        }
        log.info(f"  {col}: mean={vals.mean():.3f}, median={vals.median():.3f}, "
                 f"p90={vals.quantile(0.90):.3f}")

    return chop_df, result


def exp4_flip_rate_by_chop(trades_df, chop_df):
    """EXP4: SIGNAL_FLIP rate by Chop Index quartile."""
    log.info("=== EXP4: FLIP Rate by Chop Quartile ===")

    # Use chop_8 (2 hours lookback) as primary chop index
    chop_col = "chop_8"

    # For each trade, get chop value at entry
    trades = trades_df.copy()
    entry_times = pd.to_datetime(trades["entry_time"])

    # Merge chop at entry
    chop_at_entry = []
    chop_ts = chop_df.index
    chop_vals = chop_df[chop_col].values
    for et in entry_times:
        idx = np.searchsorted(chop_ts, et, side="right") - 1
        if 0 <= idx < len(chop_vals):
            chop_at_entry.append(chop_vals[idx])
        else:
            chop_at_entry.append(np.nan)
    trades["chop_at_entry"] = chop_at_entry
    trades = trades.dropna(subset=["chop_at_entry"])

    # Quartiles
    trades["chop_q"] = pd.qcut(trades["chop_at_entry"], 4, labels=["Q1_low", "Q2", "Q3", "Q4_high"])

    result = {"chop_column": chop_col, "quartiles": []}
    for q in ["Q1_low", "Q2", "Q3", "Q4_high"]:
        qt = trades[trades["chop_q"] == q]
        flips = qt[qt["exit_reason"] == "SIGNAL_FLIP"]
        stats = {
            "quartile": q,
            "trades": len(qt),
            "flip_count": len(flips),
            "flip_rate": round(100 * len(flips) / len(qt), 1) if len(qt) > 0 else 0,
            "flip_wr": round(100 * (flips["pnl_net"] > 0).mean(), 1) if len(flips) > 0 else 0,
            "total_wr": round(100 * (qt["pnl_net"] > 0).mean(), 1),
            "total_pnl": round(qt["pnl_net"].sum(), 2),
            "avg_pnl": round(qt["pnl_net"].mean(), 3),
            "chop_range": f"{qt['chop_at_entry'].min():.3f}-{qt['chop_at_entry'].max():.3f}",
        }
        result["quartiles"].append(stats)
        log.info(f"  {q}: {stats['trades']} trades, FLIP rate {stats['flip_rate']}%, "
                 f"WR {stats['total_wr']}%, PnL ${stats['total_pnl']}")

    # Also test multiple chop windows
    result["by_window"] = {}
    for w in [4, 8, 12, 16, 24]:
        col = f"chop_{w}"
        chop_at = []
        for et in entry_times:
            idx = np.searchsorted(chop_ts, et, side="right") - 1
            if 0 <= idx < len(chop_df):
                chop_at.append(chop_df[col].iloc[idx])
            else:
                chop_at.append(np.nan)
        tmp = trades_df.copy()
        tmp["chop"] = chop_at
        tmp = tmp.dropna(subset=["chop"])
        tmp["chop_q"] = pd.qcut(tmp["chop"], 4, labels=["Q1", "Q2", "Q3", "Q4"])
        q4 = tmp[tmp["chop_q"] == "Q4"]
        q1 = tmp[tmp["chop_q"] == "Q1"]
        result["by_window"][col] = {
            "q4_flip_rate": round(100 * (q4["exit_reason"] == "SIGNAL_FLIP").mean(), 1),
            "q1_flip_rate": round(100 * (q1["exit_reason"] == "SIGNAL_FLIP").mean(), 1),
            "q4_wr": round(100 * (q4["pnl_net"] > 0).mean(), 1),
            "q1_wr": round(100 * (q1["pnl_net"] > 0).mean(), 1),
            "q4_pnl": round(q4["pnl_net"].sum(), 2),
            "q1_pnl": round(q1["pnl_net"].sum(), 2),
        }

    return trades, result


def exp5_score_velocity(trades_df, chop_df):
    """EXP5: Score velocity at entry — does deceleration predict FLIP?"""
    log.info("=== EXP5: Score Velocity at Entry ===")

    trades = trades_df.copy()
    entry_times = pd.to_datetime(trades["entry_time"])
    chop_ts = chop_df.index

    vel_at_entry = []
    accel_at_entry = []
    for et in entry_times:
        idx = np.searchsorted(chop_ts, et, side="right") - 1
        if 0 <= idx < len(chop_df):
            vel_at_entry.append(chop_df["score_velocity"].iloc[idx])
            accel_at_entry.append(chop_df["score_accel"].iloc[idx])
        else:
            vel_at_entry.append(np.nan)
            accel_at_entry.append(np.nan)

    trades["velocity"] = vel_at_entry
    trades["accel"] = accel_at_entry
    trades = trades.dropna(subset=["velocity", "accel"])

    flip = trades[trades["exit_reason"] == "SIGNAL_FLIP"]
    nonflip = trades[trades["exit_reason"] != "SIGNAL_FLIP"]

    # Check if velocity direction matches trade direction
    trades["vel_aligned"] = ((trades["dir"] == "L") & (trades["velocity"] > 0)) | \
                            ((trades["dir"] == "S") & (trades["velocity"] < 0))
    aligned = trades[trades["vel_aligned"]]
    misaligned = trades[~trades["vel_aligned"]]

    result = {
        "flip_velocity_mean": round(flip["velocity"].mean(), 4),
        "flip_velocity_abs_mean": round(flip["velocity"].abs().mean(), 4),
        "nonflip_velocity_mean": round(nonflip["velocity"].mean(), 4),
        "nonflip_velocity_abs_mean": round(nonflip["velocity"].abs().mean(), 4),
        "flip_accel_abs_mean": round(flip["accel"].abs().mean(), 4),
        "nonflip_accel_abs_mean": round(nonflip["accel"].abs().mean(), 4),
        "aligned_trades": len(aligned),
        "aligned_wr": round(100 * (aligned["pnl_net"] > 0).mean(), 1),
        "aligned_flip_rate": round(100 * (aligned["exit_reason"] == "SIGNAL_FLIP").mean(), 1),
        "misaligned_trades": len(misaligned),
        "misaligned_wr": round(100 * (misaligned["pnl_net"] > 0).mean(), 1),
        "misaligned_flip_rate": round(100 * (misaligned["exit_reason"] == "SIGNAL_FLIP").mean(), 1),
    }

    log.info(f"  Aligned: {result['aligned_trades']} trades, WR {result['aligned_wr']}%, "
             f"FLIP rate {result['aligned_flip_rate']}%")
    log.info(f"  Misaligned: {result['misaligned_trades']} trades, WR {result['misaligned_wr']}%, "
             f"FLIP rate {result['misaligned_flip_rate']}%")
    return result


def exp6_chop_filters(btc_score_ts, chop_df):
    """EXP6: Test chop-based filters/sizing."""
    log.info("=== EXP6: Chop-Based Filters ===")

    chop_col = "chop_8"
    oos_chop = chop_df[(chop_df.index >= OOS_START) & (chop_df.index <= OOS_END)]
    chop_p75 = oos_chop[chop_col].quantile(0.75)
    chop_p90 = oos_chop[chop_col].quantile(0.90)
    chop_median = oos_chop[chop_col].quantile(0.50)

    log.info(f"  Chop thresholds: median={chop_median:.3f}, p75={chop_p75:.3f}, p90={chop_p90:.3f}")

    # Also test velocity filter
    vel_p25 = oos_chop["score_velocity"].abs().quantile(0.25)

    filters = {
        "baseline": None,
        "skip_chop_p75": lambda sig, alt, chop: _apply_chop_skip(sig, alt, chop, chop_col, chop_p75),
        "skip_chop_p90": lambda sig, alt, chop: _apply_chop_skip(sig, alt, chop, chop_col, chop_p90),
        "halfsize_chop_p75": lambda sig, alt, chop: sig,  # post-hoc sizing
        "skip_low_velocity": lambda sig, alt, chop: _apply_vel_skip(sig, alt, chop, vel_p25),
    }

    results = {}
    for name, fn in filters.items():
        log.info(f"  Testing filter: {name}")
        trades = run_backtest_all(btc_score_ts, chop_df, fn if fn else None, label=name)
        if trades.empty:
            results[name] = {"trades": 0, "pnl": 0}
            continue

        # For halfsize filter, apply post-hoc
        if name == "halfsize_chop_p75":
            trades = _apply_halfsize_posthoc(trades, chop_df, chop_col, chop_p75)

        flip = trades[trades["exit_reason"] == "SIGNAL_FLIP"]
        results[name] = {
            "trades": len(trades),
            "total_pnl": round(trades["pnl_net"].sum(), 2),
            "wr": round(100 * (trades["pnl_net"] > 0).mean(), 1),
            "flip_count": len(flip),
            "flip_pct": round(100 * len(flip) / len(trades), 1) if len(trades) > 0 else 0,
            "flip_pnl": round(flip["pnl_net"].sum(), 2) if len(flip) > 0 else 0,
            "avg_pnl": round(trades["pnl_net"].mean(), 3),
        }
        log.info(f"    -> {results[name]['trades']} trades, PnL ${results[name]['total_pnl']}, "
                 f"WR {results[name]['wr']}%, FLIPs {results[name]['flip_count']}")

    return results, {"chop_p75": round(chop_p75, 4), "chop_p90": round(chop_p90, 4),
                     "chop_median": round(chop_median, 4), "vel_p25": round(vel_p25, 4)}


def _apply_chop_skip(signals, alt_merged, chop_df, chop_col, threshold):
    """Zero out signals when chop is above threshold."""
    sig = signals.copy()
    ts_vals = alt_merged["ts"].values
    chop_ts = chop_df.index
    chop_vals = chop_df[chop_col].values

    for i in range(len(sig)):
        t = ts_vals[i]
        idx = np.searchsorted(chop_ts, t, side="right") - 1
        if 0 <= idx < len(chop_vals) and not np.isnan(chop_vals[idx]):
            if chop_vals[idx] > threshold:
                sig.iloc[i] = 0
    return sig


def _apply_vel_skip(signals, alt_merged, chop_df, vel_threshold):
    """Zero out signals when abs(velocity) is below threshold (low momentum)."""
    sig = signals.copy()
    ts_vals = alt_merged["ts"].values
    chop_ts = chop_df.index
    vel_vals = chop_df["score_velocity"].values

    for i in range(len(sig)):
        t = ts_vals[i]
        idx = np.searchsorted(chop_ts, t, side="right") - 1
        if 0 <= idx < len(vel_vals) and not np.isnan(vel_vals[idx]):
            if abs(vel_vals[idx]) < vel_threshold:
                sig.iloc[i] = 0
    return sig


def _apply_halfsize_posthoc(trades, chop_df, chop_col, threshold):
    """Post-hoc: halve PnL for trades entered during high chop."""
    trades = trades.copy()
    chop_ts = chop_df.index
    chop_vals = chop_df[chop_col].values

    for i in range(len(trades)):
        et = pd.Timestamp(trades.iloc[i]["entry_time"])
        idx = np.searchsorted(chop_ts, et, side="right") - 1
        if 0 <= idx < len(chop_vals) and not np.isnan(chop_vals[idx]):
            if chop_vals[idx] > threshold:
                trades.iloc[i, trades.columns.get_loc("pnl_net")] *= 0.5
    return trades


def exp7_walkforward(btc_score_ts, chop_df, best_filter_name, best_filter_fn, best_thresholds):
    """EXP7: Walk-forward stability of best chop filter."""
    log.info("=== EXP7: Walk-Forward Stability ===")

    periods = [
        ("P1_early", "2025-01-01", "2025-06-30"),
        ("P2_mid", "2025-07-01", "2025-12-31"),
        ("P3_late", "2026-01-01", "2026-03-31"),
    ]

    results = []
    for pname, pstart, pend in periods:
        log.info(f"  Period: {pname} ({pstart} to {pend})")

        # Baseline
        all_base = []
        all_filtered = []
        for symbol in COINS:
            coin = symbol.replace("USDT", "")
            ohlcv = bt.fetch_binance_15m(symbol, years=3)
            if "date_time" in ohlcv.columns:
                ohlcv = ohlcv.rename(columns={"date_time": "ts"})
            alt_df = bt.build_alt_technicals(ohlcv)
            mask = (alt_df["ts"] >= pstart) & (alt_df["ts"] <= pend)

            cfg = COIN_CONFIGS.get(coin, {})
            signals, alt_merged = bt.generate_btc_led_signal(
                btc_score_ts, alt_df[mask],
                threshold=cfg.get("threshold", 3.0),
                use_alt_pa_filter=cfg.get("use_alt_pa_filter", False))

            # Baseline
            base_trades = bt.run_backtest(
                alt_merged, signals,
                sl_atr_mult=cfg.get("sl_atr_mult", 2.5),
                tp_atr_mult=cfg.get("tp_atr_mult", 4.0),
                cooldown_bars=cfg.get("cooldown_bars", 4))
            if len(base_trades) > 0:
                base_trades["coin"] = coin
                all_base.append(base_trades)

            # Filtered
            filtered_signals = best_filter_fn(signals, alt_merged, chop_df) if best_filter_fn else signals
            filt_trades = bt.run_backtest(
                alt_merged, filtered_signals,
                sl_atr_mult=cfg.get("sl_atr_mult", 2.5),
                tp_atr_mult=cfg.get("tp_atr_mult", 4.0),
                cooldown_bars=cfg.get("cooldown_bars", 4))
            if len(filt_trades) > 0:
                filt_trades["coin"] = coin
                all_filtered.append(filt_trades)

        base_df = pd.concat(all_base, ignore_index=True) if all_base else pd.DataFrame()
        filt_df = pd.concat(all_filtered, ignore_index=True) if all_filtered else pd.DataFrame()

        base_pnl = base_df["pnl_net"].sum() if len(base_df) > 0 else 0
        filt_pnl = filt_df["pnl_net"].sum() if len(filt_df) > 0 else 0
        base_flips = len(base_df[base_df["exit_reason"] == "SIGNAL_FLIP"]) if len(base_df) > 0 else 0
        filt_flips = len(filt_df[filt_df["exit_reason"] == "SIGNAL_FLIP"]) if len(filt_df) > 0 else 0

        period_result = {
            "period": pname,
            "dates": f"{pstart} to {pend}",
            "baseline_trades": len(base_df),
            "baseline_pnl": round(base_pnl, 2),
            "filtered_trades": len(filt_df),
            "filtered_pnl": round(filt_pnl, 2),
            "delta_pnl": round(filt_pnl - base_pnl, 2),
            "delta_pct": round(100 * (filt_pnl - base_pnl) / base_pnl, 1) if base_pnl != 0 else 0,
            "baseline_flips": base_flips,
            "filtered_flips": filt_flips,
            "flips_avoided": base_flips - filt_flips,
        }
        results.append(period_result)
        log.info(f"    Baseline: ${base_pnl:.0f} ({base_flips} flips), "
                 f"Filtered: ${filt_pnl:.0f} ({filt_flips} flips), "
                 f"Delta: ${filt_pnl - base_pnl:.0f} ({period_result['delta_pct']:.1f}%)")

    return results


def main():
    log.info("=" * 60)
    log.info("Mission 021: Chop Detection & SIGNAL_FLIP Prevention")
    log.info("=" * 60)

    # Build BTC score
    btc_score_ts, btc_df = build_btc_score()

    # Run baseline backtest
    log.info("\nRunning baseline backtest...")
    trades_df = run_backtest_all(btc_score_ts)
    log.info(f"Baseline: {len(trades_df)} trades, PnL ${trades_df['pnl_net'].sum():.2f}")

    # EXP1: Baseline stats
    RESULTS["exp1_baseline"] = exp1_baseline(trades_df)

    # EXP2: Score trajectory
    RESULTS["exp2_score_trajectory"] = exp2_score_trajectory(trades_df, btc_score_ts, btc_df)

    # EXP3: Chop Index
    chop_df, chop_stats = exp3_chop_index(btc_score_ts, btc_df)
    RESULTS["exp3_chop_index"] = chop_stats

    # EXP4: FLIP rate by chop quartile
    trades_with_chop, flip_by_chop = exp4_flip_rate_by_chop(trades_df, chop_df)
    RESULTS["exp4_flip_by_chop"] = flip_by_chop

    # EXP5: Score velocity
    RESULTS["exp5_velocity"] = exp5_score_velocity(trades_df, chop_df)

    # EXP6: Chop filters
    filter_results, thresholds = exp6_chop_filters(btc_score_ts, chop_df)
    RESULTS["exp6_filters"] = filter_results
    RESULTS["exp6_thresholds"] = thresholds

    # EXP7: Walk-forward with best filter
    # Determine best filter (highest PnL improvement)
    baseline_pnl = filter_results["baseline"]["total_pnl"]
    best_name = "baseline"
    best_delta = 0
    for name, res in filter_results.items():
        if name == "baseline":
            continue
        delta = res["total_pnl"] - baseline_pnl
        if delta > best_delta:
            best_delta = delta
            best_name = name

    chop_col = "chop_8"
    chop_p75 = thresholds["chop_p75"]
    chop_p90 = thresholds["chop_p90"]
    vel_p25 = thresholds["vel_p25"]

    # Build filter function for walk-forward
    if best_name == "skip_chop_p75":
        best_fn = lambda sig, alt, chop: _apply_chop_skip(sig, alt, chop, chop_col, chop_p75)
    elif best_name == "skip_chop_p90":
        best_fn = lambda sig, alt, chop: _apply_chop_skip(sig, alt, chop, chop_col, chop_p90)
    elif best_name == "skip_low_velocity":
        best_fn = lambda sig, alt, chop: _apply_vel_skip(sig, alt, chop, vel_p25)
    else:
        best_fn = None

    if best_fn:
        log.info(f"\nBest filter: {best_name} (delta +${best_delta:.0f})")
        RESULTS["exp7_walkforward"] = exp7_walkforward(btc_score_ts, chop_df, best_name, best_fn, thresholds)
    else:
        log.info("\nNo filter improved PnL — skip walk-forward")
        RESULTS["exp7_walkforward"] = {"verdict": "no_filter_improved"}

    RESULTS["best_filter"] = best_name
    RESULTS["best_delta"] = round(best_delta, 2)

    # Save results
    finished = datetime.utcnow()
    RESULTS["meta"] = {
        "mission": "021_chop_detection",
        "started": STARTED.isoformat(),
        "finished": finished.isoformat(),
        "duration_sec": round((finished - STARTED).total_seconds(), 1),
        "coins": [c.replace("USDT", "") for c in COINS],
        "oos": f"{OOS_START} to {OOS_END}",
    }

    # Save JSON
    json_path = BASE_DIR / "missions" / "mission_021_chop_detection.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(RESULTS, f, indent=2, ensure_ascii=False, default=str)
    log.info(f"\nSaved: {json_path}")

    # Print summary
    log.info("\n" + "=" * 60)
    log.info("SUMMARY")
    log.info("=" * 60)
    e1 = RESULTS["exp1_baseline"]
    log.info(f"SIGNAL_FLIP: {e1['flip_count']}/{e1['total_trades']} trades "
             f"({e1['flip_pct']}%), WR {e1['flip_wr']}%, PnL ${e1['flip_pnl']}")

    e4 = RESULTS["exp4_flip_by_chop"]
    log.info(f"\nFLIP rate by chop quartile:")
    for q in e4["quartiles"]:
        log.info(f"  {q['quartile']}: FLIP rate {q['flip_rate']}%, WR {q['total_wr']}%, PnL ${q['total_pnl']}")

    log.info(f"\nBest filter: {best_name}, delta: +${best_delta:.0f}")
    if "exp7_walkforward" in RESULTS and isinstance(RESULTS["exp7_walkforward"], list):
        all_positive = all(p["delta_pnl"] > 0 for p in RESULTS["exp7_walkforward"])
        log.info(f"Walk-forward: {'ALL POSITIVE' if all_positive else 'MIXED'}")
        for p in RESULTS["exp7_walkforward"]:
            log.info(f"  {p['period']}: delta ${p['delta_pnl']} ({p['delta_pct']}%)")

    return RESULTS


if __name__ == "__main__":
    results = main()
