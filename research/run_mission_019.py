"""
Mission 019: Adaptive Position Sizing — Cascade Quality x FR Regime Combo
==========================================================================
ต่อยอดจาก Mission 014 (cascade quality) + Mission 016 (FR regime):
- M014: displacement → WR 70→86%, avg PnL 2.5x (ใช้เป็น sizing ไม่ใช่ filter)
- M016: FR extreme neg → WR 79.5%, $5.65/trade (ใช้เป็น sizing ไม่ใช่ filter)
- ทั้งสอง mission แนะนำ SIZE UP แต่ยังไม่เคยทดสอบ sizing จริง

สมมติฐาน: Position sizing ตาม quality signals จะเพิ่ม total PnL โดยไม่ต้องลด trade count

Experiments:
  EXP1: Baseline (flat sizing) vs Displacement-based sizing
  EXP2: Baseline vs FR regime-based sizing
  EXP3: Combo sizing (displacement + FR)
  EXP4: Grid search optimal multipliers
  EXP5: Risk analysis — max drawdown, worst streak under sizing
  EXP6: Walk-forward stability (split OOS into 3 periods)
"""

import sys, os, json, logging
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta
from scipy import stats

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import backtest_15m_btc_led_alts as bt
from paper_trading.config import COIN_CONFIGS
import psycopg2

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


def build_quality_features(btc_df, trades_df):
    """Match trades to cascade quality + FR regime features at entry time."""
    # Build cascade features
    df = btc_df.copy()
    lt = df["liq_total"].fillna(0)
    lt_ma = lt.rolling(24).mean().fillna(1)
    df["cascade_mag"] = np.where(lt_ma > 0, lt / lt_ma, 0)
    df["cascade_displacement"] = df["ret"].abs() * 100  # in %

    # Build FR z-score
    fr = df["fr_8h"].ffill()
    fr_mean = fr.rolling(96, min_periods=24).mean()  # ~24h rolling
    fr_std = fr.rolling(96, min_periods=24).std().clip(lower=1e-8)
    df["fr_z"] = (fr - fr_mean) / fr_std

    # Also compute FR regime
    df["fr_regime"] = pd.cut(
        df["fr_z"].fillna(0),
        bins=[-np.inf, -1.5, -0.5, 0.5, 1.5, np.inf],
        labels=["extreme_neg", "neg", "neutral", "pos", "extreme_pos"]
    )

    # Select feature columns
    feature_cols = ["ts", "cascade_mag", "cascade_displacement", "fr_z", "fr_regime"]
    feat_df = df[feature_cols].sort_values("ts")

    # Match trades to features
    trades = trades_df.copy()
    trades["entry_time"] = pd.to_datetime(trades["entry_time"])
    trades = trades.sort_values("entry_time")

    merged = pd.merge_asof(
        trades, feat_df,
        left_on="entry_time", right_on="ts",
        direction="backward",
        tolerance=pd.Timedelta("30min"),
    )
    n_matched = merged["cascade_mag"].notna().sum()
    log.info(f"Matched {n_matched}/{len(merged)} trades to quality features")
    return merged


def apply_sizing(trades, disp_mult=None, fr_mult=None, cap=2.0):
    """Apply position sizing multipliers to trades. Returns new PnL column.

    disp_mult: dict of {threshold: multiplier} for displacement
    fr_mult: dict of {regime: multiplier} for FR regime
    cap: max combined multiplier
    """
    t = trades.copy()
    t["size_mult"] = 1.0

    # Displacement-based sizing
    if disp_mult:
        for threshold, mult in sorted(disp_mult.items()):
            mask = t["cascade_displacement"] >= threshold
            t.loc[mask, "size_mult"] = mult

    # FR regime-based sizing (additive bonus)
    if fr_mult:
        for regime, bonus in fr_mult.items():
            mask = t["fr_regime"] == regime
            t.loc[mask, "size_mult"] = t.loc[mask, "size_mult"] + bonus

    # Cap
    t["size_mult"] = t["size_mult"].clip(upper=cap)

    # Adjusted PnL = base_pnl * multiplier
    # This is valid because pnl = (exit_px - entry_px) * qty * dir - fees
    # and both qty and fees scale linearly with position size
    t["pnl_sized"] = t["pnl_net"] * t["size_mult"]
    return t


def compute_metrics(pnl_series, label=""):
    """Compute trading metrics from a PnL series."""
    n = len(pnl_series)
    total = pnl_series.sum()
    wins = (pnl_series > 0).sum()
    wr = wins / n * 100 if n > 0 else 0
    avg = pnl_series.mean()

    # Cumulative PnL for drawdown
    cum = pnl_series.cumsum()
    peak = cum.cummax()
    dd = cum - peak
    max_dd = dd.min()

    # Sharpe (annualized, assuming ~35k 15-min bars per year)
    if pnl_series.std() > 0:
        sharpe = (pnl_series.mean() / pnl_series.std()) * np.sqrt(35040)
    else:
        sharpe = 0

    return {
        "label": label,
        "trades": n,
        "total_pnl": round(total, 2),
        "win_rate": round(wr, 1),
        "avg_pnl": round(avg, 2),
        "max_drawdown": round(max_dd, 2),
        "sharpe": round(sharpe, 2),
    }


def exp1_displacement_sizing(trades):
    """EXP1: Displacement-based position sizing."""
    log.info("\n=== EXP1: Displacement-Based Sizing ===")
    results = {}

    # Baseline
    results["baseline"] = compute_metrics(trades["pnl_net"], "Flat 1.0x")

    # Scheme A: conservative (from M014 recommendation)
    t_a = apply_sizing(trades, disp_mult={0.1: 1.2, 0.3: 1.5}, cap=2.0)
    results["disp_conservative"] = compute_metrics(t_a["pnl_sized"], "Disp 1.2x/1.5x")

    # Scheme B: aggressive
    t_b = apply_sizing(trades, disp_mult={0.1: 1.3, 0.3: 1.7, 1.0: 2.0}, cap=2.0)
    results["disp_aggressive"] = compute_metrics(t_b["pnl_sized"], "Disp 1.3x/1.7x/2.0x")

    # Scheme C: extreme-only boost
    t_c = apply_sizing(trades, disp_mult={0.5: 1.5, 1.0: 2.0}, cap=2.0)
    results["disp_extreme_only"] = compute_metrics(t_c["pnl_sized"], "Disp extreme 1.5x/2.0x")

    for k, v in results.items():
        log.info(f"  {v['label']:30s} | PnL ${v['total_pnl']:>10,.0f} | WR {v['win_rate']:.1f}% | "
                 f"Avg ${v['avg_pnl']:.2f} | DD ${v['max_drawdown']:.0f} | Sharpe {v['sharpe']:.2f}")

    return results


def exp2_fr_regime_sizing(trades):
    """EXP2: FR regime-based position sizing."""
    log.info("\n=== EXP2: FR Regime-Based Sizing ===")
    results = {}

    results["baseline"] = compute_metrics(trades["pnl_net"], "Flat 1.0x")

    # Scheme A: boost extreme_neg only (best regime from M016)
    t_a = apply_sizing(trades, fr_mult={"extreme_neg": 0.3}, cap=2.0)
    results["fr_neg_boost"] = compute_metrics(t_a["pnl_sized"], "FR ext_neg +0.3x")

    # Scheme B: boost extreme_neg, penalize extreme_pos
    t_b = apply_sizing(trades, fr_mult={"extreme_neg": 0.5, "extreme_pos": -0.3}, cap=2.0)
    results["fr_both"] = compute_metrics(t_b["pnl_sized"], "FR neg+0.5/pos-0.3")

    # Scheme C: gradient sizing across all regimes
    t_c = apply_sizing(trades,
                       fr_mult={"extreme_neg": 0.5, "neg": 0.2, "neutral": 0,
                                "pos": -0.1, "extreme_pos": -0.3},
                       cap=2.0)
    results["fr_gradient"] = compute_metrics(t_c["pnl_sized"], "FR gradient")

    for k, v in results.items():
        log.info(f"  {v['label']:30s} | PnL ${v['total_pnl']:>10,.0f} | WR {v['win_rate']:.1f}% | "
                 f"Avg ${v['avg_pnl']:.2f} | DD ${v['max_drawdown']:.0f} | Sharpe {v['sharpe']:.2f}")

    return results


def exp3_combo_sizing(trades):
    """EXP3: Combined displacement + FR sizing."""
    log.info("\n=== EXP3: Combo Sizing (Displacement + FR) ===")
    results = {}

    results["baseline"] = compute_metrics(trades["pnl_net"], "Flat 1.0x")

    # Combo A: conservative disp + FR neg boost
    t_a = apply_sizing(trades,
                       disp_mult={0.1: 1.2, 0.3: 1.5},
                       fr_mult={"extreme_neg": 0.3},
                       cap=2.0)
    results["combo_conservative"] = compute_metrics(t_a["pnl_sized"], "Combo conservative")

    # Combo B: aggressive
    t_b = apply_sizing(trades,
                       disp_mult={0.1: 1.3, 0.3: 1.7, 1.0: 2.0},
                       fr_mult={"extreme_neg": 0.5, "extreme_pos": -0.3},
                       cap=2.0)
    results["combo_aggressive"] = compute_metrics(t_b["pnl_sized"], "Combo aggressive")

    # Combo C: full gradient
    t_c = apply_sizing(trades,
                       disp_mult={0.1: 1.2, 0.3: 1.5, 1.0: 2.0},
                       fr_mult={"extreme_neg": 0.5, "neg": 0.2, "neutral": 0,
                                "pos": -0.1, "extreme_pos": -0.3},
                       cap=2.0)
    results["combo_full"] = compute_metrics(t_c["pnl_sized"], "Combo full gradient")

    for k, v in results.items():
        log.info(f"  {v['label']:30s} | PnL ${v['total_pnl']:>10,.0f} | WR {v['win_rate']:.1f}% | "
                 f"Avg ${v['avg_pnl']:.2f} | DD ${v['max_drawdown']:.0f} | Sharpe {v['sharpe']:.2f}")

    return results


def exp4_grid_search(trades):
    """EXP4: Grid search displacement threshold and FR boost."""
    log.info("\n=== EXP4: Grid Search Optimal Multipliers ===")

    best_pnl = -np.inf
    best_combo = None
    results_grid = []

    disp_thresholds = [0.05, 0.1, 0.15, 0.2, 0.3]
    disp_mults = [1.1, 1.2, 1.3, 1.5]
    fr_boosts = [0, 0.2, 0.3, 0.5]

    for dt in disp_thresholds:
        for dm in disp_mults:
            for fb in fr_boosts:
                t = apply_sizing(
                    trades,
                    disp_mult={dt: dm},
                    fr_mult={"extreme_neg": fb} if fb > 0 else None,
                    cap=2.0
                )
                total = t["pnl_sized"].sum()
                avg = t["pnl_sized"].mean()
                cum = t["pnl_sized"].cumsum()
                max_dd = (cum - cum.cummax()).min()

                entry = {
                    "disp_thr": dt,
                    "disp_mult": dm,
                    "fr_boost": fb,
                    "total_pnl": round(total, 2),
                    "avg_pnl": round(avg, 2),
                    "max_dd": round(max_dd, 2),
                    "pnl_dd_ratio": round(total / abs(max_dd), 2) if max_dd < 0 else 999,
                }
                results_grid.append(entry)

                if total > best_pnl:
                    best_pnl = total
                    best_combo = entry

    log.info(f"  Grid search: {len(results_grid)} combos tested")
    log.info(f"  Best: disp_thr={best_combo['disp_thr']}, disp_mult={best_combo['disp_mult']}, "
             f"fr_boost={best_combo['fr_boost']} -> PnL ${best_combo['total_pnl']:,.0f}, "
             f"DD ${best_combo['max_dd']:,.0f}")

    # Top 5
    sorted_grid = sorted(results_grid, key=lambda x: x["total_pnl"], reverse=True)
    top5 = sorted_grid[:5]
    for i, g in enumerate(top5):
        log.info(f"  #{i+1}: disp>{g['disp_thr']}={g['disp_mult']}x, FR_neg+{g['fr_boost']}x "
                 f"-> ${g['total_pnl']:,.0f} (DD ${g['max_dd']:,.0f}, ratio {g['pnl_dd_ratio']:.1f})")

    return {"grid_size": len(results_grid), "best": best_combo, "top5": top5}


def exp5_risk_analysis(trades):
    """EXP5: Risk comparison — max drawdown, worst streak, tail risk."""
    log.info("\n=== EXP5: Risk Analysis ===")

    # Best combo from grid + baseline
    configs = {
        "baseline": {"disp_mult": None, "fr_mult": None},
        "disp_only": {"disp_mult": {0.1: 1.2, 0.3: 1.5}, "fr_mult": None},
        "fr_only": {"disp_mult": None, "fr_mult": {"extreme_neg": 0.3}},
        "combo": {
            "disp_mult": {0.1: 1.2, 0.3: 1.5},
            "fr_mult": {"extreme_neg": 0.3}
        },
    }

    results = {}
    for name, cfg in configs.items():
        t = apply_sizing(trades, disp_mult=cfg["disp_mult"], fr_mult=cfg["fr_mult"], cap=2.0)
        pnl = t["pnl_sized"] if name != "baseline" else t["pnl_net"]

        cum = pnl.cumsum()
        peak = cum.cummax()
        dd = cum - peak
        max_dd = dd.min()

        # Worst consecutive loss streak
        is_loss = (pnl < 0).astype(int)
        streak = 0
        max_loss_streak = 0
        worst_streak_pnl = 0
        current_streak_pnl = 0
        for v, p in zip(is_loss, pnl):
            if v:
                streak += 1
                current_streak_pnl += p
                if streak > max_loss_streak:
                    max_loss_streak = streak
                    worst_streak_pnl = current_streak_pnl
            else:
                streak = 0
                current_streak_pnl = 0

        # Tail risk: worst 1% and 5% trades
        p1 = pnl.quantile(0.01)
        p5 = pnl.quantile(0.05)

        results[name] = {
            "label": name,
            "total_pnl": round(pnl.sum(), 2),
            "max_dd": round(max_dd, 2),
            "max_loss_streak": max_loss_streak,
            "worst_streak_pnl": round(worst_streak_pnl, 2),
            "tail_1pct": round(p1, 2),
            "tail_5pct": round(p5, 2),
            "pnl_dd_ratio": round(pnl.sum() / abs(max_dd), 2) if max_dd < 0 else 999,
        }

        log.info(f"  {name:15s} | PnL ${results[name]['total_pnl']:>10,.0f} | "
                 f"DD ${max_dd:>8,.0f} | Ratio {results[name]['pnl_dd_ratio']:.1f} | "
                 f"MaxLossStreak {max_loss_streak} | Tail1% ${p1:.2f}")

    return results


def exp6_walk_forward(trades):
    """EXP6: Walk-forward stability — split OOS into 3 periods."""
    log.info("\n=== EXP6: Walk-Forward Stability (3 periods) ===")

    trades_sorted = trades.sort_values("entry_time").copy()
    n = len(trades_sorted)
    split_size = n // 3

    periods = [
        ("P1_early", trades_sorted.iloc[:split_size]),
        ("P2_mid", trades_sorted.iloc[split_size:2*split_size]),
        ("P3_late", trades_sorted.iloc[2*split_size:]),
    ]

    results = {}
    for period_name, period_trades in periods:
        log.info(f"\n  --- {period_name} ({len(period_trades)} trades, "
                 f"{period_trades['entry_time'].min().strftime('%Y-%m-%d')} to "
                 f"{period_trades['entry_time'].max().strftime('%Y-%m-%d')}) ---")

        # Baseline
        base = compute_metrics(period_trades["pnl_net"], f"{period_name} baseline")

        # Combo sizing
        t = apply_sizing(period_trades,
                         disp_mult={0.1: 1.2, 0.3: 1.5},
                         fr_mult={"extreme_neg": 0.3},
                         cap=2.0)
        sized = compute_metrics(t["pnl_sized"], f"{period_name} combo")

        delta_pnl = sized["total_pnl"] - base["total_pnl"]
        delta_pct = delta_pnl / abs(base["total_pnl"]) * 100 if base["total_pnl"] != 0 else 0

        results[period_name] = {
            "baseline": base,
            "combo": sized,
            "delta_pnl": round(delta_pnl, 2),
            "delta_pct": round(delta_pct, 1),
        }

        log.info(f"    Baseline: PnL ${base['total_pnl']:,.0f} | WR {base['win_rate']:.1f}%")
        log.info(f"    Combo:    PnL ${sized['total_pnl']:,.0f} | WR {sized['win_rate']:.1f}%")
        log.info(f"    Delta:    ${delta_pnl:+,.0f} ({delta_pct:+.1f}%)")

    # Check consistency: combo should beat baseline in all 3 periods
    all_positive = all(r["delta_pnl"] > 0 for r in results.values())
    log.info(f"\n  Walk-forward consistency: {'ALL POSITIVE' if all_positive else 'MIXED'}")

    return {"periods": results, "all_positive": all_positive}


def save_results(all_results, trades_df):
    """Save mission results to JSON + missions.json."""
    mission_dir = BASE_DIR / "missions"
    research_dir = BASE_DIR / "research"

    # Save raw JSON
    json_path = mission_dir / "mission_019_adaptive_sizing.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, indent=2, ensure_ascii=False, default=str)
    log.info(f"Saved: {json_path}")

    # Update missions.json
    from research.missions import MissionEngine, _get_level

    engine = MissionEngine()

    # Determine best combo PnL lift
    base_pnl = all_results["exp1"]["baseline"]["total_pnl"]
    combo_pnl = all_results["exp3"]["combo_conservative"]["total_pnl"]
    delta = combo_pnl - base_pnl
    delta_pct = delta / abs(base_pnl) * 100 if base_pnl != 0 else 0

    wf = all_results["exp6"]
    wf_consistent = wf["all_positive"]

    mission_entry = {
        "mission_id": "mission_019_adaptive_sizing",
        "date": datetime.utcnow().strftime("%Y-%m-%d"),
        "type": "position_sizing / risk_management",
        "title": "Adaptive Position Sizing — Cascade Quality x FR Regime Combo",
        "description": "ทดสอบ position sizing แบบ adaptive: เพิ่มขนาด position ตาม cascade displacement + FR extreme neg regime แทนการ filter trades ออก",
        "difficulty": "hard",
        "xp_reward": 100,
        "status": "completed",
        "target": "position_sizing",
        "started_at": all_results.get("started_at", datetime.utcnow().isoformat()),
        "finished_at": datetime.utcnow().isoformat(),
        "result": {
            "baseline_pnl": base_pnl,
            "combo_pnl": combo_pnl,
            "delta_pnl": round(delta, 2),
            "delta_pct": round(delta_pct, 1),
            "walk_forward_consistent": wf_consistent,
            "best_grid": all_results["exp4"]["best"],
            "trades": len(trades_df),
        },
        "insight": (
            f"Adaptive sizing (disp>=0.1%→1.2x, disp>=0.3%→1.5x, FR_ext_neg+0.3x) "
            f"{'เพิ่ม' if delta > 0 else 'ลด'} PnL ${abs(delta):,.0f} ({delta_pct:+.1f}%) "
            f"จาก ${base_pnl:,.0f} → ${combo_pnl:,.0f}. "
            f"Walk-forward: {'ทุก period positive = stable' if wf_consistent else 'ไม่ consistent ทุก period'}."
        ),
        "tags": ["position_sizing", "cascade_quality", "funding_rate", "risk_management", "follow_up_m014_m016"],
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
    log.info(f"Updated missions.json: +100 XP, streak {engine._data['meta']['current_streak']}")

    return mission_entry


def main():
    started_at = datetime.utcnow().isoformat()
    log.info("=" * 60)
    log.info("Mission 019: Adaptive Position Sizing")
    log.info("=" * 60)

    # Load data
    btc_df, btc_score_ts = load_btc_data()

    # Run baseline backtest
    trades_df = run_v6_backtest(btc_score_ts)

    # Build quality features for each trade
    trades = build_quality_features(btc_df, trades_df)

    # Sanity check: distribution of features
    log.info(f"\nFeature coverage:")
    log.info(f"  cascade_displacement: {trades['cascade_displacement'].notna().sum()}/{len(trades)} "
             f"(mean={trades['cascade_displacement'].mean():.4f}%)")
    log.info(f"  fr_z: {trades['fr_z'].notna().sum()}/{len(trades)} "
             f"(mean={trades['fr_z'].mean():.3f})")
    log.info(f"  fr_regime distribution:")
    if "fr_regime" in trades.columns:
        for regime, count in trades["fr_regime"].value_counts().items():
            pct = count / len(trades) * 100
            log.info(f"    {regime}: {count} ({pct:.1f}%)")

    # Run experiments
    r1 = exp1_displacement_sizing(trades)
    r2 = exp2_fr_regime_sizing(trades)
    r3 = exp3_combo_sizing(trades)
    r4 = exp4_grid_search(trades)
    r5 = exp5_risk_analysis(trades)
    r6 = exp6_walk_forward(trades)

    # Collect all results
    all_results = {
        "started_at": started_at,
        "exp1": r1,
        "exp2": r2,
        "exp3": r3,
        "exp4": r4,
        "exp5": r5,
        "exp6": r6,
        "config": {
            "oos_start": OOS_START,
            "oos_end": OOS_END,
            "coins": [c.replace("USDT", "") for c in COINS],
            "v6_sl": V6_SL,
            "v6_tp": V6_TP,
            "v6_cascade_mult": V6_CASCADE_MULT,
        }
    }

    # Save
    mission_entry = save_results(all_results, trades_df)

    log.info("\n" + "=" * 60)
    log.info("Mission 019 COMPLETE")
    log.info(f"Insight: {mission_entry['insight']}")
    log.info("=" * 60)

    return all_results


if __name__ == "__main__":
    main()
