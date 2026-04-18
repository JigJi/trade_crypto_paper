#!/usr/bin/env python3
"""
Mission #005: Exit Mechanism Deep Dive
========================================
Holding Period PnL Trajectory & Time Stop Analysis

สมมติฐาน: SL/TP เป็นตัวทำลายกำไรใน paper trading (WR 16% บน SL/TP exit)
วิเคราะห์ว่า PnL เปลี่ยนแปลงยังไงตาม holding period
แล้วทดสอบว่า time-based exit ดีกว่า fixed SL/TP หรือไม่

Experiments:
1. Baseline: v3 SL3.0/TP5.0 -- exit reason breakdown
2. PnL by holding period (bins: 1-4, 5-12, 13-24, 25-48, 49-96 bars)
3. Time stop tests: exit after N bars (8, 16, 24, 32, 48) instead of max 96
4. No-SL test: remove SL, keep TP + signal_flip + timeout
5. Tighter time stop + no SL: best of both?
6. Bar-by-bar PnL trajectory of wins vs losses (unrealized PnL at each bar)
"""

import sys
import json
import logging
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import backtest_15m_btc_led_alts as bt
from paper_trading.config import COMPOSITE_WEIGHTS, V3_EXTRA_WEIGHTS, COIN_CONFIGS
from paper_trading.strategy import score_ob_combined, score_basis_contrarian, score_tick_liq

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────────
OOS_START = "2026-01-01"
OOS_END = "2026-03-31"
V3_CORE_COINS = ["BTCUSDT", "XRPUSDT", "ADAUSDT", "DOTUSDT", "SUIUSDT", "FILUSDT"]


def load_btc_score():
    """Load BTC OHLCV + build v3 composite score."""
    log.info("Loading BTC data...")
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


def run_backtest_for_coins(btc_score_ts, coins, sl_mult, tp_mult, max_hold=96):
    """Run backtest across coins with given SL/TP/max_hold config."""
    all_trades = []
    for symbol in coins:
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
        trades = bt.run_backtest(
            alt_merged, signals,
            sl_atr_mult=sl_mult,
            tp_atr_mult=tp_mult,
            cooldown_bars=cfg.get("cooldown_bars", 4),
            max_hold_bars=max_hold)
        if len(trades) > 0:
            trades["coin"] = coin
            all_trades.append(trades)

    if all_trades:
        return pd.concat(all_trades, ignore_index=True)
    return pd.DataFrame()


def run_no_sl_backtest(btc_score_ts, coins, tp_mult, max_hold=96):
    """Run backtest with NO stop-loss (SL=999 ATR, effectively disabled)."""
    return run_backtest_for_coins(btc_score_ts, coins, sl_mult=999.0, tp_mult=tp_mult, max_hold=max_hold)


def analyze_exit_reasons(trades, label=""):
    """Break down PnL and WR by exit reason."""
    results = {}
    for reason in trades["exit_reason"].unique():
        subset = trades[trades["exit_reason"] == reason]
        n = len(subset)
        wins = (subset["pnl_net"] > 0).sum()
        wr = wins / n * 100 if n > 0 else 0
        pnl = subset["pnl_net"].sum()
        avg_pnl = subset["pnl_net"].mean()
        avg_bars = subset["holding_bars"].mean()
        results[reason] = {
            "count": int(n),
            "win_rate": round(wr, 1),
            "total_pnl": round(pnl, 2),
            "avg_pnl": round(avg_pnl, 2),
            "avg_bars_held": round(avg_bars, 1),
        }
    return results


def analyze_holding_period_bins(trades):
    """PnL and WR by holding period bins."""
    bins = [0, 4, 12, 24, 48, 96, 9999]
    labels = ["1-4bars", "5-12bars", "13-24bars", "25-48bars", "49-96bars", "96+bars"]
    trades = trades.copy()
    trades["hold_bin"] = pd.cut(trades["holding_bars"], bins=bins, labels=labels, right=True)

    results = {}
    for label in labels:
        subset = trades[trades["hold_bin"] == label]
        n = len(subset)
        if n == 0:
            continue
        wins = (subset["pnl_net"] > 0).sum()
        wr = wins / n * 100
        pnl = subset["pnl_net"].sum()
        avg_pnl = subset["pnl_net"].mean()
        results[label] = {
            "count": int(n),
            "win_rate": round(wr, 1),
            "total_pnl": round(pnl, 2),
            "avg_pnl": round(avg_pnl, 2),
        }
    return results


def compute_unrealized_pnl_trajectory(btc_score_ts, coins):
    """
    For each trade, compute bar-by-bar unrealized PnL up to 96 bars.
    Returns aggregated trajectory for wins and losses separately.
    """
    log.info("Computing bar-by-bar unrealized PnL trajectory...")

    # We need access to raw OHLCV data for each trade
    # First, run baseline to get trades with entry_idx/exit_idx
    all_trajectories_win = []
    all_trajectories_loss = []

    for symbol in coins:
        coin = symbol.replace("USDT", "")
        ohlcv = bt.fetch_binance_15m(symbol, years=3)
        if "date_time" in ohlcv.columns:
            ohlcv = ohlcv.rename(columns={"date_time": "ts"})
        alt_df = bt.build_alt_technicals(ohlcv)
        oos_mask = (alt_df["ts"] >= OOS_START) & (alt_df["ts"] <= OOS_END)
        alt_oos = alt_df[oos_mask].reset_index(drop=True)

        cfg = COIN_CONFIGS.get(coin, {})
        signals, alt_merged = bt.generate_btc_led_signal(
            btc_score_ts, alt_oos,
            threshold=cfg.get("threshold", 3.0),
            use_alt_pa_filter=cfg.get("use_alt_pa_filter", False))
        trades = bt.run_backtest(
            alt_merged, signals,
            sl_atr_mult=3.0, tp_atr_mult=5.0,
            cooldown_bars=cfg.get("cooldown_bars", 4),
            max_hold_bars=96)

        if trades.empty:
            continue

        closes = alt_merged["close"].values
        opens = alt_merged["open"].values

        for _, trade in trades.iterrows():
            entry_i = int(trade["entry_idx"])
            entry_px = trade["entry_price"]
            direction = 1 if trade["dir"] == "L" else -1
            is_win = trade["pnl_net"] > 0
            notional = entry_px * trade["qty"]

            # Compute unrealized PnL at each bar (up to 96 bars from entry)
            trajectory = []
            max_bars = min(96, len(closes) - entry_i - 1)
            for b in range(1, max_bars + 1):
                idx = entry_i + b
                if idx >= len(closes):
                    break
                mid_price = closes[idx]
                unrealized_pct = (mid_price - entry_px) / entry_px * direction * 100
                trajectory.append(unrealized_pct)

            if len(trajectory) > 0:
                # Pad to 96 with NaN
                padded = trajectory + [np.nan] * (96 - len(trajectory))
                if is_win:
                    all_trajectories_win.append(padded[:96])
                else:
                    all_trajectories_loss.append(padded[:96])

    # Aggregate: mean unrealized PnL at each bar
    win_traj = np.nanmean(all_trajectories_win, axis=0) if all_trajectories_win else np.zeros(96)
    loss_traj = np.nanmean(all_trajectories_loss, axis=0) if all_trajectories_loss else np.zeros(96)
    all_traj = np.nanmean(all_trajectories_win + all_trajectories_loss, axis=0) if (all_trajectories_win or all_trajectories_loss) else np.zeros(96)

    # Find peak bars
    win_peak_bar = int(np.nanargmax(win_traj)) + 1
    loss_worst_bar = int(np.nanargmin(loss_traj)) + 1
    all_peak_bar = int(np.nanargmax(all_traj)) + 1

    return {
        "win_trajectory": [round(x, 4) if not np.isnan(x) else None for x in win_traj],
        "loss_trajectory": [round(x, 4) if not np.isnan(x) else None for x in loss_traj],
        "all_trajectory": [round(x, 4) if not np.isnan(x) else None for x in all_traj],
        "win_peak_bar": win_peak_bar,
        "win_peak_pct": round(float(np.nanmax(win_traj)), 4),
        "loss_worst_bar": loss_worst_bar,
        "loss_worst_pct": round(float(np.nanmin(loss_traj)), 4),
        "all_peak_bar": all_peak_bar,
        "all_peak_pct": round(float(np.nanmax(all_traj)), 4),
        "n_wins": len(all_trajectories_win),
        "n_losses": len(all_trajectories_loss),
    }


def main():
    log.info("=" * 60)
    log.info("Mission #005: Exit Mechanism Deep Dive")
    log.info("=" * 60)
    start_time = datetime.utcnow()

    btc_score_ts, btc_df = load_btc_score()
    results = {}

    # ── Experiment 1: Baseline exit reason breakdown ────────────
    log.info("\n[EXP 1] Baseline SL3.0/TP5.0 exit reason breakdown...")
    baseline = run_backtest_for_coins(btc_score_ts, V3_CORE_COINS, sl_mult=3.0, tp_mult=5.0, max_hold=96)
    baseline_metrics = {
        "total_trades": len(baseline),
        "win_rate": round((baseline["pnl_net"] > 0).sum() / len(baseline) * 100, 1) if len(baseline) > 0 else 0,
        "total_pnl": round(baseline["pnl_net"].sum(), 2) if len(baseline) > 0 else 0,
        "avg_bars_held": round(baseline["holding_bars"].mean(), 1) if len(baseline) > 0 else 0,
    }
    exit_breakdown = analyze_exit_reasons(baseline, "baseline")
    holding_bins = analyze_holding_period_bins(baseline)
    results["exp1_baseline"] = {
        "config": "SL3.0/TP5.0/MAX96",
        "metrics": baseline_metrics,
        "exit_breakdown": exit_breakdown,
        "holding_period_bins": holding_bins,
    }
    log.info(f"  Baseline: {baseline_metrics['total_trades']} trades, WR {baseline_metrics['win_rate']}%, PnL ${baseline_metrics['total_pnl']}")
    for reason, data in exit_breakdown.items():
        log.info(f"    {reason}: {data['count']} trades, WR {data['win_rate']}%, PnL ${data['total_pnl']}, avg {data['avg_bars_held']} bars")

    # ── Experiment 2: Time stop tests ──────────────────────────
    log.info("\n[EXP 2] Time stop tests (max_hold_bars = 8, 16, 24, 32, 48)...")
    time_stop_results = {}
    for max_hold in [8, 16, 24, 32, 48]:
        trades = run_backtest_for_coins(btc_score_ts, V3_CORE_COINS, sl_mult=3.0, tp_mult=5.0, max_hold=max_hold)
        n = len(trades)
        wr = round((trades["pnl_net"] > 0).sum() / n * 100, 1) if n > 0 else 0
        pnl = round(trades["pnl_net"].sum(), 2) if n > 0 else 0
        exit_br = analyze_exit_reasons(trades)
        time_stop_results[f"max{max_hold}"] = {
            "max_hold_bars": max_hold,
            "trades": n,
            "win_rate": wr,
            "total_pnl": pnl,
            "delta_vs_baseline": round(pnl - baseline_metrics["total_pnl"], 2),
            "exit_breakdown": exit_br,
        }
        log.info(f"  max_hold={max_hold}: {n} trades, WR {wr}%, PnL ${pnl} (delta ${round(pnl - baseline_metrics['total_pnl'], 2)})")
    results["exp2_time_stops"] = time_stop_results

    # ── Experiment 3: No-SL tests ──────────────────────────────
    log.info("\n[EXP 3] No-SL tests (SL disabled, TP + signal_flip + timeout)...")
    no_sl_results = {}
    for max_hold in [24, 32, 48, 96]:
        trades = run_no_sl_backtest(btc_score_ts, V3_CORE_COINS, tp_mult=5.0, max_hold=max_hold)
        n = len(trades)
        wr = round((trades["pnl_net"] > 0).sum() / n * 100, 1) if n > 0 else 0
        pnl = round(trades["pnl_net"].sum(), 2) if n > 0 else 0
        exit_br = analyze_exit_reasons(trades)
        no_sl_results[f"noSL_max{max_hold}"] = {
            "max_hold_bars": max_hold,
            "trades": n,
            "win_rate": wr,
            "total_pnl": pnl,
            "delta_vs_baseline": round(pnl - baseline_metrics["total_pnl"], 2),
            "exit_breakdown": exit_br,
        }
        log.info(f"  no-SL max_hold={max_hold}: {n} trades, WR {wr}%, PnL ${pnl} (delta ${round(pnl - baseline_metrics['total_pnl'], 2)})")
    results["exp3_no_sl"] = no_sl_results

    # ── Experiment 4: Wide SL tests ────────────────────────────
    log.info("\n[EXP 4] Wide SL tests (SL 5.0/6.0/8.0/10.0 with TP 5.0)...")
    wide_sl_results = {}
    for sl in [5.0, 6.0, 8.0, 10.0]:
        trades = run_backtest_for_coins(btc_score_ts, V3_CORE_COINS, sl_mult=sl, tp_mult=5.0, max_hold=96)
        n = len(trades)
        wr = round((trades["pnl_net"] > 0).sum() / n * 100, 1) if n > 0 else 0
        pnl = round(trades["pnl_net"].sum(), 2) if n > 0 else 0
        exit_br = analyze_exit_reasons(trades)
        wide_sl_results[f"SL{sl}"] = {
            "sl_mult": sl,
            "trades": n,
            "win_rate": wr,
            "total_pnl": pnl,
            "delta_vs_baseline": round(pnl - baseline_metrics["total_pnl"], 2),
            "exit_breakdown": exit_br,
        }
        log.info(f"  SL={sl}: {n} trades, WR {wr}%, PnL ${pnl} (delta ${round(pnl - baseline_metrics['total_pnl'], 2)})")
    results["exp4_wide_sl"] = wide_sl_results

    # ── Experiment 5: TP variation with wide SL ────────────────
    log.info("\n[EXP 5] TP variation with wide SL (SL=8.0)...")
    tp_var_results = {}
    for tp in [3.0, 4.0, 5.0, 6.0, 8.0, 10.0]:
        trades = run_backtest_for_coins(btc_score_ts, V3_CORE_COINS, sl_mult=8.0, tp_mult=tp, max_hold=96)
        n = len(trades)
        wr = round((trades["pnl_net"] > 0).sum() / n * 100, 1) if n > 0 else 0
        pnl = round(trades["pnl_net"].sum(), 2) if n > 0 else 0
        tp_var_results[f"TP{tp}"] = {
            "tp_mult": tp,
            "trades": n,
            "win_rate": wr,
            "total_pnl": pnl,
            "delta_vs_baseline": round(pnl - baseline_metrics["total_pnl"], 2),
        }
        log.info(f"  SL=8.0/TP={tp}: {n} trades, WR {wr}%, PnL ${pnl}")
    results["exp5_tp_variation"] = tp_var_results

    # ── Experiment 6: Bar-by-bar PnL trajectory ────────────────
    log.info("\n[EXP 6] Bar-by-bar unrealized PnL trajectory...")
    trajectory = compute_unrealized_pnl_trajectory(btc_score_ts, V3_CORE_COINS)
    results["exp6_pnl_trajectory"] = {
        "win_peak_bar": trajectory["win_peak_bar"],
        "win_peak_pct": trajectory["win_peak_pct"],
        "loss_worst_bar": trajectory["loss_worst_bar"],
        "loss_worst_pct": trajectory["loss_worst_pct"],
        "all_peak_bar": trajectory["all_peak_bar"],
        "all_peak_pct": trajectory["all_peak_pct"],
        "n_wins": trajectory["n_wins"],
        "n_losses": trajectory["n_losses"],
        # Store sampled trajectory (every 4 bars) for readability
        "win_traj_sampled": [trajectory["win_trajectory"][i] for i in range(0, 96, 4)],
        "loss_traj_sampled": [trajectory["loss_trajectory"][i] for i in range(0, 96, 4)],
        "all_traj_sampled": [trajectory["all_trajectory"][i] for i in range(0, 96, 4)],
    }
    log.info(f"  Win peak at bar {trajectory['win_peak_bar']} ({trajectory['win_peak_pct']:.2f}%)")
    log.info(f"  Loss worst at bar {trajectory['loss_worst_bar']} ({trajectory['loss_worst_pct']:.2f}%)")
    log.info(f"  All trades peak at bar {trajectory['all_peak_bar']} ({trajectory['all_peak_pct']:.2f}%)")

    # ── Experiment 7: Direction split exit analysis ─────────────
    log.info("\n[EXP 7] Exit analysis split by direction (LONG vs SHORT)...")
    dir_results = {}
    for direction in ["L", "S"]:
        dir_label = "LONG" if direction == "L" else "SHORT"
        subset = baseline[baseline["dir"] == direction]
        if len(subset) == 0:
            continue
        n = len(subset)
        wr = round((subset["pnl_net"] > 0).sum() / n * 100, 1)
        pnl = round(subset["pnl_net"].sum(), 2)
        exit_br = analyze_exit_reasons(subset)
        holding = analyze_holding_period_bins(subset)
        dir_results[dir_label] = {
            "trades": n,
            "win_rate": wr,
            "total_pnl": pnl,
            "avg_bars_held": round(subset["holding_bars"].mean(), 1),
            "exit_breakdown": exit_br,
            "holding_bins": holding,
        }
        log.info(f"  {dir_label}: {n} trades, WR {wr}%, PnL ${pnl}")
        for reason, data in exit_br.items():
            log.info(f"    {reason}: {data['count']} ({data['win_rate']}% WR), PnL ${data['total_pnl']}")
    results["exp7_direction_split"] = dir_results

    # ── Summary ─────────────────────────────────────────────────
    finish_time = datetime.utcnow()

    # Find best configuration
    all_configs = []
    all_configs.append(("Baseline SL3.0/TP5.0", baseline_metrics["total_pnl"], baseline_metrics["win_rate"]))
    for k, v in time_stop_results.items():
        all_configs.append((f"Time stop {k}", v["total_pnl"], v["win_rate"]))
    for k, v in no_sl_results.items():
        all_configs.append((f"No-SL {k}", v["total_pnl"], v["win_rate"]))
    for k, v in wide_sl_results.items():
        all_configs.append((f"Wide {k}", v["total_pnl"], v["win_rate"]))

    all_configs.sort(key=lambda x: x[1], reverse=True)
    best = all_configs[0]
    worst = all_configs[-1]

    summary = {
        "mission_id": "mission_005_exit_mechanism",
        "date": datetime.utcnow().strftime("%Y-%m-%d"),
        "best_config": {"name": best[0], "pnl": best[1], "wr": best[2]},
        "worst_config": {"name": worst[0], "pnl": worst[1], "wr": worst[2]},
        "baseline_pnl": baseline_metrics["total_pnl"],
        "all_configs_ranked": [{"name": c[0], "pnl": c[1], "wr": c[2]} for c in all_configs[:10]],
        "trajectory_insight": {
            "win_peak_bar": trajectory["win_peak_bar"],
            "loss_worst_bar": trajectory["loss_worst_bar"],
            "all_peak_bar": trajectory["all_peak_bar"],
        },
    }
    results["summary"] = summary

    log.info("\n" + "=" * 60)
    log.info("MISSION #005 SUMMARY")
    log.info("=" * 60)
    log.info(f"Best config: {best[0]} -> PnL ${best[1]}, WR {best[2]}%")
    log.info(f"Worst config: {worst[0]} -> PnL ${worst[1]}, WR {worst[2]}%")
    log.info(f"Baseline: ${baseline_metrics['total_pnl']}")
    log.info(f"Win PnL peaks at bar {trajectory['win_peak_bar']}")
    log.info(f"Loss PnL worst at bar {trajectory['loss_worst_bar']}")
    log.info("\nTop 5 configs:")
    for c in all_configs[:5]:
        log.info(f"  {c[0]}: ${c[1]}, WR {c[2]}%")

    # ── Save results ────────────────────────────────────────────
    out_dir = BASE_DIR / "missions"
    out_dir.mkdir(exist_ok=True)

    json_path = out_dir / "mission_005_exit_mechanism.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, default=str, ensure_ascii=False)
    log.info(f"\nSaved JSON: {json_path}")

    # Also save to experiments/
    exp_dir = BASE_DIR / "experiments"
    exp_dir.mkdir(exist_ok=True)
    exp_path = exp_dir / "exit_mechanism_analysis.json"
    with open(exp_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, default=str, ensure_ascii=False)
    log.info(f"Saved experiments: {exp_path}")

    return results


if __name__ == "__main__":
    results = main()
