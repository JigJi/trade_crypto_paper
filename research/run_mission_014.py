"""
Mission 014: Liquidation Cascade Quality Anatomy
=================================================
v6 proved liquidation is THE only factor needed (binary signal).
But not all cascades are equal. This mission dissects CASCADE CHARACTERISTICS
to find what separates profitable from unprofitable cascade-triggered trades.

Experiments:
  EXP1: Cascade Magnitude Buckets (liq_total / liq_total_ma)
  EXP2: Side Dominance (|liq_net| / liq_total ratio)
  EXP3: Cascade Freshness (bars since previous cascade)
  EXP4: Price Displacement during cascade bar
  EXP5: Multi-bar vs Single-bar cascades
  EXP6: Combined Quality Score (composite of best features)
"""

import sys, os, json, logging
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import backtest_15m_btc_led_alts as bt
from paper_trading.config import COIN_CONFIGS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

# ---- Config ----
OOS_START = "2025-01-01"
OOS_END = "2026-03-31"
COINS = ["BTCUSDT", "XRPUSDT", "ADAUSDT", "DOTUSDT", "SUIUSDT", "FILUSDT"]
V6_CASCADE_MULT = 1.1
V6_LIQ_W = 8.0
V6_TICK_W = 8.0
V6_TICK_NET_THR = 3
V6_SL = 25.0
V6_TP = 20.0


def load_btc_data():
    """Load BTC OHLCV + DB data + build features (v6 score)."""
    log.info("Loading BTC OHLCV...")
    btc_ohlcv = bt.fetch_binance_15m("BTCUSDT", years=3)
    if "date_time" in btc_ohlcv.columns:
        btc_ohlcv = btc_ohlcv.rename(columns={"date_time": "ts"})

    log.info("Loading DB data...")
    db_data = bt.load_btc_db_data()

    log.info("Building BTC features...")
    btc_df = bt.build_btc_features(btc_ohlcv, db_data)

    log.info("Computing v6 score...")
    btc_score = bt.compute_btc_composite_score_v6(
        btc_df,
        cascade_mult=V6_CASCADE_MULT,
        liq_w=V6_LIQ_W,
        tick_w=V6_TICK_W,
        tick_net_thr=V6_TICK_NET_THR,
    )
    btc_score_ts = pd.Series(btc_score.values, index=btc_df["ts"].values, name="btc_score")

    return btc_df, btc_score_ts


def run_v6_backtest(btc_score_ts):
    """Run v6 backtest across all coins, return trades DataFrame."""
    all_trades = []
    for symbol in COINS:
        coin = symbol.replace("USDT", "")
        ohlcv = bt.fetch_binance_15m(symbol, years=3)
        if "date_time" in ohlcv.columns:
            ohlcv = ohlcv.rename(columns={"date_time": "ts"})
        alt_df = bt.build_alt_technicals(ohlcv)
        oos_mask = (alt_df["ts"] >= OOS_START) & (alt_df["ts"] <= OOS_END)

        cfg = COIN_CONFIGS.get(coin, {})
        threshold = cfg.get("threshold", 3.0)
        signals, alt_merged = bt.generate_btc_led_signal(
            btc_score_ts, alt_df[oos_mask],
            threshold=threshold,
            use_alt_pa_filter=cfg.get("use_alt_pa_filter", False),
        )
        trades = bt.run_backtest(
            alt_merged, signals,
            sl_atr_mult=V6_SL,
            tp_atr_mult=V6_TP,
            cooldown_bars=cfg.get("cooldown_bars", 4),
        )
        if len(trades) > 0:
            trades["coin"] = coin
            all_trades.append(trades)

    trades_df = pd.concat(all_trades, ignore_index=True)
    log.info(f"Total trades: {len(trades_df)}, PnL: ${trades_df['pnl_net'].sum():.0f}")
    return trades_df


def build_cascade_features(btc_df):
    """Build cascade characteristic features aligned to btc_df timestamps."""
    df = btc_df.copy()

    # Cascade trigger (v6 style)
    lt = df["liq_total"].fillna(0)
    lt_ma = df.get("liq_total_ma", lt.rolling(24).mean()).fillna(1)
    ln = df["liq_net"].fillna(0)

    df["cascade_triggered"] = lt > (lt_ma * V6_CASCADE_MULT)

    # 1. Cascade magnitude: how much above MA
    df["cascade_mag"] = np.where(lt_ma > 0, lt / lt_ma, 0)

    # 2. Side dominance: how one-sided is the cascade
    df["side_dominance"] = np.where(lt > 0, ln.abs() / lt.clip(lower=1), 0)

    # 3. Cascade freshness: bars since last cascade
    cascade_idx = df.index[df["cascade_triggered"]]
    df["bars_since_cascade"] = np.nan
    last_cascade_i = -999
    for i in range(len(df)):
        if df["cascade_triggered"].iloc[i]:
            df.iloc[i, df.columns.get_loc("bars_since_cascade")] = i - last_cascade_i
            last_cascade_i = i
        else:
            df.iloc[i, df.columns.get_loc("bars_since_cascade")] = i - last_cascade_i

    # 4. Price displacement during cascade bar (abs return)
    df["cascade_displacement"] = df["ret"].abs() * 100  # in %

    # 5. Multi-bar cascade: consecutive cascade bars
    df["multi_bar_count"] = 0
    count = 0
    for i in range(len(df)):
        if df["cascade_triggered"].iloc[i]:
            count += 1
            df.iloc[i, df.columns.get_loc("multi_bar_count")] = count
        else:
            count = 0

    # 6. Tick liq intensity
    if "liq_net_ma" in df.columns:
        df["tick_intensity"] = df["liq_net_ma"].abs().fillna(0)
    else:
        df["tick_intensity"] = 0

    # 7. Volume context: total liq in absolute terms
    df["liq_volume_usd"] = lt

    return df


def match_trades_to_cascade(trades_df, cascade_df):
    """For each trade, find the cascade characteristics at entry time."""
    # Build cascade feature series indexed by ts
    cascade_ts = cascade_df[["ts", "cascade_mag", "side_dominance",
                              "bars_since_cascade", "cascade_displacement",
                              "multi_bar_count", "tick_intensity",
                              "liq_volume_usd", "cascade_triggered",
                              "liq_net"]].copy()
    cascade_ts = cascade_ts.sort_values("ts")

    # Match each trade's entry_time to nearest cascade bar
    trades = trades_df.copy()
    trades["entry_time"] = pd.to_datetime(trades["entry_time"])
    trades = trades.sort_values("entry_time")

    merged = pd.merge_asof(
        trades, cascade_ts,
        left_on="entry_time", right_on="ts",
        direction="backward",
        tolerance=pd.Timedelta("30min"),
    )

    # Drop trades without cascade data
    n_before = len(merged)
    merged = merged.dropna(subset=["cascade_mag"])
    n_after = len(merged)
    log.info(f"Matched {n_after}/{n_before} trades to cascade features")

    return merged


def exp1_cascade_magnitude(trades):
    """EXP1: Group trades by cascade magnitude buckets."""
    log.info("\n=== EXP1: Cascade Magnitude Buckets ===")

    # Define magnitude buckets
    bins = [0, 1.1, 1.5, 2.0, 3.0, 5.0, 100]
    labels = ["<1.1x (no cascade)", "1.1-1.5x", "1.5-2.0x", "2.0-3.0x", "3.0-5.0x", "5.0x+"]
    trades["mag_bucket"] = pd.cut(trades["cascade_mag"], bins=bins, labels=labels, right=False)

    results = []
    for bucket in labels:
        mask = trades["mag_bucket"] == bucket
        subset = trades[mask]
        if len(subset) == 0:
            continue
        wr = (subset["pnl_net"] > 0).mean() * 100
        pnl = subset["pnl_net"].sum()
        avg_pnl = subset["pnl_net"].mean()
        n = len(subset)
        results.append({
            "bucket": bucket, "trades": n, "wr_pct": round(wr, 1),
            "total_pnl": round(pnl, 2), "avg_pnl": round(avg_pnl, 2),
        })
        log.info(f"  {bucket}: {n} trades, WR {wr:.1f}%, PnL ${pnl:.0f}, avg ${avg_pnl:.2f}")

    return results


def exp2_side_dominance(trades):
    """EXP2: Group by how one-sided the cascade is."""
    log.info("\n=== EXP2: Side Dominance ===")

    bins = [0, 0.3, 0.5, 0.7, 0.9, 1.01]
    labels = ["<0.3 (mixed)", "0.3-0.5", "0.5-0.7", "0.7-0.9", "0.9+ (one-sided)"]
    trades["dom_bucket"] = pd.cut(trades["side_dominance"], bins=bins, labels=labels, right=False)

    results = []
    for bucket in labels:
        mask = trades["dom_bucket"] == bucket
        subset = trades[mask]
        if len(subset) == 0:
            continue
        wr = (subset["pnl_net"] > 0).mean() * 100
        pnl = subset["pnl_net"].sum()
        avg_pnl = subset["pnl_net"].mean()
        n = len(subset)
        results.append({
            "bucket": bucket, "trades": n, "wr_pct": round(wr, 1),
            "total_pnl": round(pnl, 2), "avg_pnl": round(avg_pnl, 2),
        })
        log.info(f"  {bucket}: {n} trades, WR {wr:.1f}%, PnL ${pnl:.0f}, avg ${avg_pnl:.2f}")

    return results


def exp3_cascade_freshness(trades):
    """EXP3: Does time since last cascade affect trade quality?"""
    log.info("\n=== EXP3: Cascade Freshness (bars since last cascade) ===")

    bins = [0, 2, 5, 10, 20, 50, 10000]
    labels = ["1 bar (immediate)", "2-4 bars", "5-9 bars", "10-19 bars", "20-49 bars", "50+ bars"]
    trades["fresh_bucket"] = pd.cut(trades["bars_since_cascade"], bins=bins, labels=labels, right=False)

    results = []
    for bucket in labels:
        mask = trades["fresh_bucket"] == bucket
        subset = trades[mask]
        if len(subset) == 0:
            continue
        wr = (subset["pnl_net"] > 0).mean() * 100
        pnl = subset["pnl_net"].sum()
        avg_pnl = subset["pnl_net"].mean()
        n = len(subset)
        results.append({
            "bucket": bucket, "trades": n, "wr_pct": round(wr, 1),
            "total_pnl": round(pnl, 2), "avg_pnl": round(avg_pnl, 2),
        })
        log.info(f"  {bucket}: {n} trades, WR {wr:.1f}%, PnL ${pnl:.0f}, avg ${avg_pnl:.2f}")

    return results


def exp4_price_displacement(trades):
    """EXP4: Price displacement during cascade bar."""
    log.info("\n=== EXP4: Price Displacement at Entry ===")

    bins = [0, 0.1, 0.3, 0.5, 1.0, 5.0]
    labels = ["<0.1%", "0.1-0.3%", "0.3-0.5%", "0.5-1.0%", "1.0%+"]
    trades["disp_bucket"] = pd.cut(trades["cascade_displacement"], bins=bins, labels=labels, right=False)

    results = []
    for bucket in labels:
        mask = trades["disp_bucket"] == bucket
        subset = trades[mask]
        if len(subset) == 0:
            continue
        wr = (subset["pnl_net"] > 0).mean() * 100
        pnl = subset["pnl_net"].sum()
        avg_pnl = subset["pnl_net"].mean()
        n = len(subset)
        results.append({
            "bucket": bucket, "trades": n, "wr_pct": round(wr, 1),
            "total_pnl": round(pnl, 2), "avg_pnl": round(avg_pnl, 2),
        })
        log.info(f"  {bucket}: {n} trades, WR {wr:.1f}%, PnL ${pnl:.0f}, avg ${avg_pnl:.2f}")

    return results


def exp5_multibar_cascade(trades):
    """EXP5: Single-bar vs multi-bar cascade."""
    log.info("\n=== EXP5: Multi-bar Cascade Duration ===")

    bins = [0, 1, 2, 4, 8, 100]
    labels = ["no cascade bar", "1st bar", "2nd-3rd bar", "4th-7th bar", "8+ bar"]
    trades["mb_bucket"] = pd.cut(trades["multi_bar_count"], bins=bins, labels=labels, right=True)

    results = []
    for bucket in labels:
        mask = trades["mb_bucket"] == bucket
        subset = trades[mask]
        if len(subset) == 0:
            continue
        wr = (subset["pnl_net"] > 0).mean() * 100
        pnl = subset["pnl_net"].sum()
        avg_pnl = subset["pnl_net"].mean()
        n = len(subset)
        results.append({
            "bucket": bucket, "trades": n, "wr_pct": round(wr, 1),
            "total_pnl": round(pnl, 2), "avg_pnl": round(avg_pnl, 2),
        })
        log.info(f"  {bucket}: {n} trades, WR {wr:.1f}%, PnL ${pnl:.0f}, avg ${avg_pnl:.2f}")

    return results


def exp6_quality_score(trades, all_results):
    """EXP6: Build composite quality score from best features, test as filter."""
    log.info("\n=== EXP6: Combined Quality Score & Filter Test ===")

    # Normalize features to z-scores for combination
    features = ["cascade_mag", "side_dominance", "tick_intensity", "cascade_displacement"]
    for f in features:
        col = trades[f].fillna(0)
        mean, std = col.mean(), col.std()
        if std > 0:
            trades[f"z_{f}"] = (col - mean) / std
        else:
            trades[f"z_{f}"] = 0

    # Quality score = sum of z-scores (higher = more intense cascade)
    trades["quality_score"] = (
        trades["z_cascade_mag"] +
        trades["z_side_dominance"] +
        trades["z_tick_intensity"] +
        trades["z_cascade_displacement"]
    )

    # Bucket by quality score
    quantiles = trades["quality_score"].quantile([0.25, 0.5, 0.75]).values
    bins = [-100, quantiles[0], quantiles[1], quantiles[2], 100]
    labels = ["Q1 (lowest)", "Q2", "Q3", "Q4 (highest)"]
    trades["quality_bucket"] = pd.cut(trades["quality_score"], bins=bins, labels=labels)

    quality_results = []
    for bucket in labels:
        mask = trades["quality_bucket"] == bucket
        subset = trades[mask]
        if len(subset) == 0:
            continue
        wr = (subset["pnl_net"] > 0).mean() * 100
        pnl = subset["pnl_net"].sum()
        avg_pnl = subset["pnl_net"].mean()
        n = len(subset)
        quality_results.append({
            "bucket": bucket, "trades": n, "wr_pct": round(wr, 1),
            "total_pnl": round(pnl, 2), "avg_pnl": round(avg_pnl, 2),
        })
        log.info(f"  {bucket}: {n} trades, WR {wr:.1f}%, PnL ${pnl:.0f}, avg ${avg_pnl:.2f}")

    # Filter tests: skip lowest quality quartile
    baseline_pnl = trades["pnl_net"].sum()
    baseline_trades = len(trades)
    baseline_wr = (trades["pnl_net"] > 0).mean() * 100

    filter_results = []
    for skip_below in ["Q1 (lowest)", "Q2"]:
        filtered = trades[trades["quality_bucket"] != skip_below]
        if skip_below == "Q2":
            filtered = trades[trades["quality_bucket"].isin(["Q3", "Q4 (highest)"])]

        f_pnl = filtered["pnl_net"].sum()
        f_trades = len(filtered)
        f_wr = (filtered["pnl_net"] > 0).mean() * 100 if len(filtered) > 0 else 0
        delta = f_pnl - baseline_pnl

        filter_results.append({
            "filter": f"skip {skip_below}",
            "trades": f_trades,
            "wr_pct": round(f_wr, 1),
            "total_pnl": round(f_pnl, 2),
            "delta_pnl": round(delta, 2),
            "trades_removed_pct": round((1 - f_trades/baseline_trades) * 100, 1),
        })
        log.info(f"  Filter '{skip_below}': {f_trades} trades ({(1-f_trades/baseline_trades)*100:.1f}% removed), "
                 f"WR {f_wr:.1f}%, PnL ${f_pnl:.0f} (delta ${delta:.0f})")

    # Direction breakdown
    dir_results = {}
    for d, label in [(1, "LONG"), (-1, "SHORT")]:
        dir_col = "dir"
        if dir_col in trades.columns:
            for bucket in labels:
                mask = (trades["quality_bucket"] == bucket)
                # Determine direction from 'dir' column (L=1, S=-1)
                if "dir" in trades.columns:
                    dir_mask = trades["dir"].map({"L": 1, "S": -1, 1: 1, -1: -1}).fillna(0) == d
                else:
                    continue
                subset = trades[mask & dir_mask]
                if len(subset) == 0:
                    continue
                key = f"{label}_{bucket}"
                dir_results[key] = {
                    "trades": len(subset),
                    "wr_pct": round((subset["pnl_net"] > 0).mean() * 100, 1),
                    "avg_pnl": round(subset["pnl_net"].mean(), 2),
                }

    return {
        "quality_buckets": quality_results,
        "filter_tests": filter_results,
        "direction_x_quality": dir_results,
        "baseline": {
            "trades": baseline_trades,
            "wr_pct": round(baseline_wr, 1),
            "total_pnl": round(baseline_pnl, 2),
        },
    }


def main():
    start_time = datetime.utcnow()
    log.info("=" * 60)
    log.info("Mission 014: Liquidation Cascade Quality Anatomy")
    log.info("=" * 60)

    # Step 1: Load data
    btc_df, btc_score_ts = load_btc_data()

    # Step 2: Run v6 backtest
    log.info("\nRunning v6 backtest...")
    trades_df = run_v6_backtest(btc_score_ts)

    # Step 3: Build cascade features
    log.info("\nBuilding cascade features...")
    cascade_df = build_cascade_features(btc_df)

    # Step 4: Match trades to cascade characteristics
    matched_trades = match_trades_to_cascade(trades_df, cascade_df)

    # Step 5: Run experiments
    results = {}
    results["exp1_magnitude"] = exp1_cascade_magnitude(matched_trades)
    results["exp2_side_dominance"] = exp2_side_dominance(matched_trades)
    results["exp3_freshness"] = exp3_cascade_freshness(matched_trades)
    results["exp4_displacement"] = exp4_price_displacement(matched_trades)
    results["exp5_multibar"] = exp5_multibar_cascade(matched_trades)
    results["exp6_quality_score"] = exp6_quality_score(matched_trades, results)

    # Summary stats
    total_trades = len(matched_trades)
    total_pnl = matched_trades["pnl_net"].sum()
    overall_wr = (matched_trades["pnl_net"] > 0).mean() * 100

    # Direction breakdown
    if "dir" in matched_trades.columns:
        long_mask = matched_trades["dir"].isin(["L", 1])
        short_mask = matched_trades["dir"].isin(["S", -1])
        results["direction_summary"] = {
            "LONG": {
                "trades": int(long_mask.sum()),
                "wr_pct": round((matched_trades.loc[long_mask, "pnl_net"] > 0).mean() * 100, 1) if long_mask.any() else 0,
                "total_pnl": round(matched_trades.loc[long_mask, "pnl_net"].sum(), 2),
            },
            "SHORT": {
                "trades": int(short_mask.sum()),
                "wr_pct": round((matched_trades.loc[short_mask, "pnl_net"] > 0).mean() * 100, 1) if short_mask.any() else 0,
                "total_pnl": round(matched_trades.loc[short_mask, "pnl_net"].sum(), 2),
            },
        }

    results["summary"] = {
        "total_trades": total_trades,
        "total_pnl": round(total_pnl, 2),
        "overall_wr": round(overall_wr, 1),
        "oos_period": f"{OOS_START} to {OOS_END}",
        "coins": [c.replace("USDT", "") for c in COINS],
        "v6_config": {
            "cascade_mult": V6_CASCADE_MULT,
            "liq_w": V6_LIQ_W,
            "tick_w": V6_TICK_W,
            "sl": V6_SL, "tp": V6_TP,
        },
    }

    end_time = datetime.utcnow()
    results["duration_sec"] = round((end_time - start_time).total_seconds(), 1)

    # ---- Save results ----
    mission_dir = BASE_DIR / "missions"
    mission_dir.mkdir(exist_ok=True)

    # JSON
    json_path = mission_dir / "mission_014_cascade_quality.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False, default=str)
    log.info(f"\nSaved JSON: {json_path}")

    log.info(f"\nDone! {total_trades} trades analyzed, PnL ${total_pnl:.0f}, "
             f"WR {overall_wr:.1f}%, duration {results['duration_sec']:.0f}s")

    return results


if __name__ == "__main__":
    results = main()
