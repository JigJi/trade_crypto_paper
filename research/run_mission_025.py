"""
Mission 025: Trade Duration & Time Stop Analysis
==================================================
เทรดที่ถือนานเกินไป มีพฤติกรรมยังไง? ควรมี time stop ไหม?

สมมติฐาน: เทรดที่ไม่ถึง TP ภายใน N bars มีแนวโน้มจะแพ้
ถ้าออกก่อนตอน underwater (losing) จะลด SIGNAL_FLIP losses

Experiments:
  EXP1: Duration distribution by exit reason (v6, 6 coins)
  EXP2: PnL curve vs holding duration
  EXP3: Conditional recovery — ถ้า underwater หลัง N bars, recover ได้ไหม?
  EXP4: Time stop grid search (max_hold_bars = 8..96)
  EXP5: Conditional time stop — ออกเฉพาะเมื่อ underwater หลัง N bars
  EXP6: Walk-forward validation
"""

import sys
import json
import logging
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import backtest_15m_btc_led_alts as bt
from paper_trading.config import COMPOSITE_WEIGHTS, V3_EXTRA_WEIGHTS, COIN_CONFIGS
from paper_trading.strategy import score_ob_combined, score_basis_contrarian, score_tick_liq

logging.basicConfig(level=logging.WARNING)

# Fix Windows console encoding for Thai
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# ── Config ─────────────────────────────────────────────────────
COINS = ["BTCUSDT", "XRPUSDT", "ADAUSDT", "DOTUSDT", "SUIUSDT", "FILUSDT"]
OOS_START = "2025-01-01"
OOS_END = "2026-03-31"


def build_btc_score():
    """Build v3 BTC composite score (standard pattern)."""
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


def run_all_coins(btc_score_ts, max_hold_bars=96, cooldown_bars=4):
    """Run v3 backtest for all 6 coins, return combined trades."""
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
        trades = bt.run_backtest(
            alt_merged, signals,
            sl_atr_mult=cfg.get("sl_atr_mult", 2.5),
            tp_atr_mult=cfg.get("tp_atr_mult", 4.0),
            cooldown_bars=cooldown_bars,
            max_hold_bars=max_hold_bars)
        if len(trades) > 0:
            trades["coin"] = coin
            all_trades.append(trades)
    if not all_trades:
        return pd.DataFrame()
    return pd.concat(all_trades, ignore_index=True)


def run_all_coins_with_bar_pnl(btc_score_ts, btc_df):
    """Run backtest AND capture bar-by-bar unrealized PnL for each trade."""
    # First get trades
    trades = run_all_coins(btc_score_ts)
    if trades.empty:
        return trades, {}

    # Now for each trade, compute bar-by-bar unrealized PnL
    # Cache alt data
    alt_data = {}
    for symbol in COINS:
        ohlcv = bt.fetch_binance_15m(symbol, years=3)
        if "date_time" in ohlcv.columns:
            ohlcv = ohlcv.rename(columns={"date_time": "ts"})
        alt_df = bt.build_alt_technicals(ohlcv)
        alt_data[symbol.replace("USDT", "")] = alt_df

    bar_pnls = {}  # trade_idx -> list of unrealized pnl at each bar
    for idx, row in trades.iterrows():
        coin = row["coin"]
        df = alt_data[coin]
        entry_px = row["entry_price"]
        direction = 1 if row["dir"] == "L" else -1
        qty = row["qty"]

        # Find bar indices
        entry_mask = df["ts"] == row["entry_time"]
        exit_mask = df["ts"] == row["exit_time"]

        if not entry_mask.any() or not exit_mask.any():
            continue

        entry_iloc = df.index[entry_mask][0]
        exit_iloc = df.index[exit_mask][0]

        unrealized = []
        for i in range(entry_iloc, exit_iloc + 1):
            close_px = df.loc[i, "close"]
            pnl = (close_px - entry_px) * qty * direction
            unrealized.append(pnl)
        bar_pnls[idx] = unrealized

    return trades, bar_pnls


def main():
    print("=" * 70)
    print("Mission 025: Trade Duration & Time Stop Analysis")
    print("=" * 70)

    # ── Build BTC score ──
    print("\n[1/6] Building BTC composite score...")
    btc_score_ts, btc_df = build_btc_score()

    # ══════════════════════════════════════════════════════════════
    # EXP1: Duration Distribution by Exit Reason
    # ══════════════════════════════════════════════════════════════
    print("\n[2/6] EXP1: Duration distribution by exit reason...")
    trades = run_all_coins(btc_score_ts)
    print(f"  Total trades: {len(trades)}")

    results = {"exp1": {}, "exp2": {}, "exp3": {}, "exp4": {}, "exp5": {}, "exp6": {}}

    # Duration stats by exit reason
    for reason in trades["exit_reason"].unique():
        subset = trades[trades["exit_reason"] == reason]
        results["exp1"][reason] = {
            "count": int(len(subset)),
            "pct": round(len(subset) / len(trades) * 100, 1),
            "bars_mean": round(float(subset["holding_bars"].mean()), 1),
            "bars_median": round(float(subset["holding_bars"].median()), 1),
            "bars_p25": round(float(subset["holding_bars"].quantile(0.25)), 1),
            "bars_p75": round(float(subset["holding_bars"].quantile(0.75)), 1),
            "bars_max": int(subset["holding_bars"].max()),
            "wr": round(float((subset["pnl_net"] > 0).mean() * 100), 1),
            "total_pnl": round(float(subset["pnl_net"].sum()), 2),
            "avg_pnl": round(float(subset["pnl_net"].mean()), 2),
        }
        print(f"  {reason:15s}: n={len(subset):4d} ({results['exp1'][reason]['pct']:5.1f}%), "
              f"bars={results['exp1'][reason]['bars_mean']:5.1f} (med={results['exp1'][reason]['bars_median']:.0f}), "
              f"WR={results['exp1'][reason]['wr']:.1f}%, PnL=${results['exp1'][reason]['total_pnl']:,.0f}")

    # ══════════════════════════════════════════════════════════════
    # EXP2: PnL curve vs holding duration (buckets)
    # ══════════════════════════════════════════════════════════════
    print("\n[3/6] EXP2: PnL vs holding duration buckets...")
    buckets = [(1, 4), (5, 8), (9, 16), (17, 24), (25, 48), (49, 72), (73, 96)]
    results["exp2"] = {}
    for lo, hi in buckets:
        label = f"{lo}-{hi}"
        subset = trades[(trades["holding_bars"] >= lo) & (trades["holding_bars"] <= hi)]
        if len(subset) == 0:
            continue
        results["exp2"][label] = {
            "count": int(len(subset)),
            "wr": round(float((subset["pnl_net"] > 0).mean() * 100), 1),
            "total_pnl": round(float(subset["pnl_net"].sum()), 2),
            "avg_pnl": round(float(subset["pnl_net"].mean()), 2),
            "pct_flip": round(float((subset["exit_reason"] == "SIGNAL_FLIP").mean() * 100), 1),
            "pct_tp": round(float((subset["exit_reason"] == "TP").mean() * 100), 1),
            "pct_sl": round(float((subset["exit_reason"] == "SL").mean() * 100), 1),
            "pct_trail": round(float((subset["exit_reason"] == "TRAIL").mean() * 100), 1),
        }
        print(f"  Bars {label:>5s}: n={len(subset):4d}, WR={results['exp2'][label]['wr']:.1f}%, "
              f"PnL=${results['exp2'][label]['total_pnl']:,.0f}, "
              f"TP={results['exp2'][label]['pct_tp']:.0f}% FLIP={results['exp2'][label]['pct_flip']:.0f}% "
              f"SL={results['exp2'][label]['pct_sl']:.0f}% TRAIL={results['exp2'][label]['pct_trail']:.0f}%")

    # ══════════════════════════════════════════════════════════════
    # EXP3: Conditional recovery — ถ้า underwater หลัง N bars?
    # ══════════════════════════════════════════════════════════════
    print("\n[4/6] EXP3: Bar-by-bar unrealized PnL analysis...")
    trades_with_pnl, bar_pnls = run_all_coins_with_bar_pnl(btc_score_ts, btc_df)

    # For each checkpoint (after N bars), check if trade was underwater and final outcome
    checkpoints = [2, 4, 8, 12, 16, 24, 32, 48]
    results["exp3"] = {}

    for cp in checkpoints:
        underwater_win = 0
        underwater_lose = 0
        above_water_win = 0
        above_water_lose = 0

        for idx, row in trades_with_pnl.iterrows():
            if idx not in bar_pnls:
                continue
            pnl_series = bar_pnls[idx]
            if len(pnl_series) <= cp:
                continue  # Trade ended before checkpoint

            unrealized_at_cp = pnl_series[cp]
            final_pnl = row["pnl_net"]

            if unrealized_at_cp < 0:  # underwater
                if final_pnl > 0:
                    underwater_win += 1
                else:
                    underwater_lose += 1
            else:  # above water
                if final_pnl > 0:
                    above_water_win += 1
                else:
                    above_water_lose += 1

        uw_total = underwater_win + underwater_lose
        aw_total = above_water_win + above_water_lose
        results["exp3"][str(cp)] = {
            "checkpoint_bars": cp,
            "checkpoint_hours": round(cp * 0.25, 1),
            "underwater_total": uw_total,
            "underwater_recovery_rate": round(underwater_win / uw_total * 100, 1) if uw_total > 0 else 0,
            "above_water_total": aw_total,
            "above_water_win_rate": round(above_water_win / aw_total * 100, 1) if aw_total > 0 else 0,
        }
        uw_rr = results["exp3"][str(cp)]["underwater_recovery_rate"]
        aw_wr = results["exp3"][str(cp)]["above_water_win_rate"]
        print(f"  After {cp:2d} bars ({cp*15:4d}min): "
              f"Underwater n={uw_total:4d} recovery={uw_rr:.1f}% | "
              f"Above water n={aw_total:4d} WR={aw_wr:.1f}%")

    # ══════════════════════════════════════════════════════════════
    # EXP4: Time Stop Grid Search
    # ══════════════════════════════════════════════════════════════
    print("\n[5/6] EXP4: Time stop grid search (max_hold_bars)...")
    baseline_pnl = float(trades["pnl_net"].sum())
    baseline_trades = len(trades)
    time_stops = [8, 12, 16, 24, 32, 48, 64, 96]
    results["exp4"] = {}

    for mhb in time_stops:
        ts_trades = run_all_coins(btc_score_ts, max_hold_bars=mhb)
        if ts_trades.empty:
            continue
        pnl = float(ts_trades["pnl_net"].sum())
        wr = float((ts_trades["pnl_net"] > 0).mean() * 100)
        n = len(ts_trades)
        flip_count = int((ts_trades["exit_reason"] == "SIGNAL_FLIP").sum())
        timeout_count = int((ts_trades["exit_reason"] == "TIMEOUT").sum())
        results["exp4"][str(mhb)] = {
            "max_hold_bars": mhb,
            "max_hold_hours": mhb * 0.25,
            "trades": n,
            "wr": round(wr, 1),
            "pnl": round(pnl, 2),
            "delta_pnl": round(pnl - baseline_pnl, 2),
            "delta_pct": round((pnl - baseline_pnl) / abs(baseline_pnl) * 100, 1) if baseline_pnl != 0 else 0,
            "flip_count": flip_count,
            "timeout_count": timeout_count,
        }
        print(f"  max_hold={mhb:3d} ({mhb*0.25:5.1f}h): n={n:4d}, WR={wr:.1f}%, "
              f"PnL=${pnl:,.0f} (Δ${pnl-baseline_pnl:+,.0f}), "
              f"FLIPs={flip_count}, TIMEOUTs={timeout_count}")

    # ══════════════════════════════════════════════════════════════
    # EXP5: Conditional Time Stop — ออกเฉพาะเมื่อ underwater
    # ══════════════════════════════════════════════════════════════
    print("\n[6/6] EXP5: Conditional time stop (exit if underwater after N bars)...")
    # We simulate this by looking at trades and their bar-by-bar PnL
    # For each (checkpoint, threshold), compute PnL if we had exited underwater trades
    cond_checkpoints = [8, 12, 16, 24, 32]
    results["exp5"] = {}

    for cp in cond_checkpoints:
        # Calculate: trades that survived past cp bars AND were underwater at cp
        # -> exit them at their unrealized PnL at cp instead of actual exit
        adjusted_pnl = 0.0
        trades_cut = 0
        trades_kept = 0
        cut_saved = 0.0  # PnL saved by cutting (negative = we avoided this loss)

        for idx, row in trades_with_pnl.iterrows():
            if idx not in bar_pnls:
                adjusted_pnl += row["pnl_net"]
                trades_kept += 1
                continue

            pnl_series = bar_pnls[idx]

            if len(pnl_series) <= cp:
                # Trade ended before checkpoint — keep as-is
                adjusted_pnl += row["pnl_net"]
                trades_kept += 1
            elif pnl_series[cp] < 0:
                # Underwater at checkpoint — exit at unrealized PnL (rough approximation)
                # Account for fees on exit
                exit_fee = abs(pnl_series[cp]) * 0.001  # rough fee estimate
                adjusted_pnl += pnl_series[cp] - exit_fee
                trades_cut += 1
                cut_saved += row["pnl_net"] - pnl_series[cp]  # how much we avoided
            else:
                # Above water — keep
                adjusted_pnl += row["pnl_net"]
                trades_kept += 1

        results["exp5"][str(cp)] = {
            "checkpoint_bars": cp,
            "checkpoint_hours": cp * 0.25,
            "trades_cut": trades_cut,
            "trades_kept": trades_kept,
            "adjusted_pnl": round(adjusted_pnl, 2),
            "baseline_pnl": round(baseline_pnl, 2),
            "delta_pnl": round(adjusted_pnl - baseline_pnl, 2),
            "delta_pct": round((adjusted_pnl - baseline_pnl) / abs(baseline_pnl) * 100, 1) if baseline_pnl != 0 else 0,
            "avg_saved_per_cut": round(cut_saved / trades_cut, 2) if trades_cut > 0 else 0,
        }
        delta = adjusted_pnl - baseline_pnl
        print(f"  Exit underwater after {cp:2d} bars: cut={trades_cut:4d}, kept={trades_kept:4d}, "
              f"PnL=${adjusted_pnl:,.0f} (Δ${delta:+,.0f}, {results['exp5'][str(cp)]['delta_pct']:+.1f}%)")

    # ══════════════════════════════════════════════════════════════
    # EXP6: Walk-Forward (best conditional time stop)
    # ══════════════════════════════════════════════════════════════
    print("\n[BONUS] EXP6: Walk-forward for best time stop...")
    # Find best conditional checkpoint from EXP5
    best_cp = None
    best_delta = -999999
    for cp_str, data in results["exp5"].items():
        if data["delta_pnl"] > best_delta:
            best_delta = data["delta_pnl"]
            best_cp = int(cp_str)

    if best_cp and best_delta > 0:
        print(f"  Best conditional checkpoint: {best_cp} bars (Δ${best_delta:+,.0f})")
        # Walk-forward: split OOS into 3 periods
        wf_periods = [
            ("P1_early", "2025-01-01", "2025-06-30"),
            ("P2_mid", "2025-07-01", "2025-12-31"),
            ("P3_late", "2026-01-01", "2026-03-31"),
        ]
        results["exp6"]["best_checkpoint"] = best_cp
        results["exp6"]["periods"] = {}

        for label, start, end in wf_periods:
            period_trades = trades_with_pnl[
                (trades_with_pnl["entry_time"] >= start) &
                (trades_with_pnl["entry_time"] <= end)
            ]
            if period_trades.empty:
                continue

            baseline_p = float(period_trades["pnl_net"].sum())
            adjusted_p = 0.0
            cuts = 0

            for idx, row in period_trades.iterrows():
                if idx not in bar_pnls:
                    adjusted_p += row["pnl_net"]
                    continue
                pnl_series = bar_pnls[idx]
                if len(pnl_series) > best_cp and pnl_series[best_cp] < 0:
                    adjusted_p += pnl_series[best_cp]
                    cuts += 1
                else:
                    adjusted_p += row["pnl_net"]

            delta_p = adjusted_p - baseline_p
            results["exp6"]["periods"][label] = {
                "start": start, "end": end,
                "trades": int(len(period_trades)),
                "baseline_pnl": round(baseline_p, 2),
                "adjusted_pnl": round(adjusted_p, 2),
                "delta": round(delta_p, 2),
                "delta_pct": round(delta_p / abs(baseline_p) * 100, 1) if baseline_p != 0 else 0,
                "trades_cut": cuts,
            }
            print(f"  {label}: baseline=${baseline_p:,.0f} → adjusted=${adjusted_p:,.0f} "
                  f"(Δ${delta_p:+,.0f}, {results['exp6']['periods'][label]['delta_pct']:+.1f}%)")
    else:
        print(f"  No conditional time stop improved PnL. Skip walk-forward.")
        results["exp6"]["verdict"] = "no_improvement"

    # ══════════════════════════════════════════════════════════════
    # Save Results
    # ══════════════════════════════════════════════════════════════
    mission_dir = BASE_DIR / "missions"
    mission_dir.mkdir(exist_ok=True)

    # JSON
    json_path = mission_dir / "mission_025_trade_duration.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False, default=str)
    print(f"\nResults saved to {json_path}")

    # Summary stats for missions.json
    total_pnl = float(trades["pnl_net"].sum())
    total_trades = len(trades)
    flip_pct = float((trades["exit_reason"] == "SIGNAL_FLIP").mean() * 100)

    # Find best time stop result
    best_ts_key = max(results["exp4"].keys(), key=lambda k: results["exp4"][k]["pnl"]) if results["exp4"] else "96"
    best_ts = results["exp4"].get(best_ts_key, {})

    # Find best conditional stop
    best_cond_key = max(results["exp5"].keys(), key=lambda k: results["exp5"][k]["delta_pnl"]) if results["exp5"] else None
    best_cond = results["exp5"].get(best_cond_key, {}) if best_cond_key else {}

    print("\n" + "=" * 70)
    print("MISSION 025 COMPLETE")
    print(f"Baseline: {total_trades} trades, PnL=${total_pnl:,.0f}, FLIP={flip_pct:.1f}%")
    if best_ts:
        print(f"Best time stop: max_hold={best_ts.get('max_hold_bars', '?')} bars → "
              f"PnL=${best_ts.get('pnl', 0):,.0f} (Δ${best_ts.get('delta_pnl', 0):+,.0f})")
    if best_cond:
        print(f"Best conditional stop: {best_cond.get('checkpoint_bars', '?')} bars underwater → "
              f"PnL=${best_cond.get('adjusted_pnl', 0):,.0f} (Δ${best_cond.get('delta_pnl', 0):+,.0f})")
    print("=" * 70)

    return results


if __name__ == "__main__":
    results = main()
