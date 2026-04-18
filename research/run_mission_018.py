"""
Mission 018: Paper Trading vs Backtest Reality Gap
===================================================
Paper WR ~45% vs Backtest WR ~75% -- gap 30pp! Why?

7 Experiments:
  EXP1: Overall stats comparison (paper vs backtest, same 10-day window)
  EXP2: Per-model version (v3 vs v5 vs v6)
  EXP3: Direction asymmetry (LONG vs SHORT)
  EXP4: Exit mechanism comparison
  EXP5: Entry score distribution & alignment
  EXP6: Per-coin reality gap ranking
  EXP7: Holding period analysis
"""

import sys, json, logging, sqlite3
from pathlib import Path
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import backtest_15m_btc_led_alts as bt
from signal_core import compute_btc_composite_score_v6
from paper_trading.config import (
    COMPOSITE_WEIGHTS, V3_EXTRA_WEIGHTS, COIN_CONFIGS,
    COINS_V3, COINS_V5, COINS_V6, COINS,
    FLIP_CONFIG,
)
from paper_trading.strategy import score_ob_combined, score_basis_contrarian, score_tick_liq

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

# V6 constants
V6_CASCADE_MULT = 1.1
V6_LIQ_W = 8.0
V6_TICK_W = 8.0
V6_TICK_THR = 3

# ── Load paper trades ──
def load_paper_trades():
    conn = sqlite3.connect(str(BASE_DIR / "paper_trading" / "state" / "paper_trades.db"))
    df = pd.read_sql("SELECT * FROM trades ORDER BY entry_time", conn)
    conn.close()
    df["entry_time"] = pd.to_datetime(df["entry_time"])
    df["exit_time"] = pd.to_datetime(df["exit_time"])
    df["model"] = df["coin"].apply(lambda c:
        "v6" if c in COINS_V6 else ("v5" if c in COINS_V5 else "v3"))
    df["dir_label"] = df["direction"].map({1: "LONG", -1: "SHORT"})
    df["win"] = df["pnl_net"] > 0
    return df


def build_btc_scores():
    """Build both v3 and v6 BTC scores."""
    btc_ohlcv = bt.fetch_binance_15m("BTCUSDT", years=3)
    if "date_time" in btc_ohlcv.columns:
        btc_ohlcv = btc_ohlcv.rename(columns={"date_time": "ts"})
    db_data = bt.load_btc_db_data()
    btc_df = bt.build_btc_features(btc_ohlcv, db_data)

    # v3 score
    v3_score = bt.compute_btc_composite_score(btc_df, COMPOSITE_WEIGHTS)
    v3_score = v3_score + score_ob_combined(btc_df, weight=V3_EXTRA_WEIGHTS["ob_combined"])
    v3_score = v3_score + score_basis_contrarian(btc_df, weight=V3_EXTRA_WEIGHTS["basis_contrarian"])
    v3_score = v3_score + score_tick_liq(btc_df, weight=V3_EXTRA_WEIGHTS["tick_liq"])
    v3_score_ts = pd.Series(v3_score.values, index=btc_df["ts"].values, name="btc_score")

    # v6 score
    v6_score = compute_btc_composite_score_v6(
        btc_df, cascade_mult=V6_CASCADE_MULT,
        liq_w=V6_LIQ_W, tick_w=V6_TICK_W, tick_net_thr=V6_TICK_THR)
    v6_score_ts = pd.Series(v6_score.values, index=btc_df["ts"].values, name="btc_score")

    return v3_score_ts, v6_score_ts, btc_df


def run_backtest_coins(coins, score_ts, period_start, period_end, model_name):
    """Run backtest for a list of coins."""
    all_trades = []
    for coin in coins:
        symbol = coin + "USDT"
        cfg = COIN_CONFIGS.get(coin, {})
        try:
            ohlcv = bt.fetch_binance_15m(symbol, years=3)
        except Exception:
            continue
        if "date_time" in ohlcv.columns:
            ohlcv = ohlcv.rename(columns={"date_time": "ts"})
        alt_df = bt.build_alt_technicals(ohlcv)
        oos = alt_df[(alt_df["ts"] >= period_start) & (alt_df["ts"] <= period_end)]
        if len(oos) < 10:
            continue

        signals, alt_merged = bt.generate_btc_led_signal(
            score_ts, oos,
            threshold=cfg.get("threshold", 3.0),
            use_alt_pa_filter=cfg.get("use_alt_pa_filter", False))
        trades = bt.run_backtest(
            alt_merged, signals,
            sl_atr_mult=cfg.get("sl_atr_mult", 10.0),
            tp_atr_mult=cfg.get("tp_atr_mult", 5.0),
            cooldown_bars=cfg.get("cooldown_bars", 4),
            max_hold_bars=96)
        if len(trades) > 0:
            trades["coin"] = coin
            trades["model"] = model_name
            all_trades.append(trades)

    if all_trades:
        return pd.concat(all_trades, ignore_index=True)
    return pd.DataFrame()


def stats(df, pnl_col="pnl_net"):
    """Quick stats dict."""
    if len(df) == 0:
        return {"trades": 0}
    wins = df[pnl_col] > 0
    return {
        "trades": len(df),
        "wins": int(wins.sum()),
        "wr": round(wins.mean() * 100, 1),
        "total_pnl": round(float(df[pnl_col].sum()), 2),
        "avg_pnl": round(float(df[pnl_col].mean()), 4),
        "avg_bars": round(float(df["bars_held"].mean()), 1) if "bars_held" in df.columns else None,
    }


def main():
    results = {}
    paper = load_paper_trades()
    paper_start = paper["entry_time"].min().strftime("%Y-%m-%d")
    paper_end = paper["exit_time"].max().strftime("%Y-%m-%d")
    log.info(f"Paper period: {paper_start} to {paper_end} ({len(paper)} trades, {paper['coin'].nunique()} coins)")

    # Build BTC scores
    log.info("Building BTC scores (v3 + v6)...")
    v3_score_ts, v6_score_ts, btc_df = build_btc_scores()

    # Run backtests per model group
    log.info("Running backtests for all 46 coins...")
    bt_v3 = run_backtest_coins(COINS_V3, v3_score_ts, paper_start, paper_end, "v3")
    bt_v5 = run_backtest_coins(COINS_V5, v3_score_ts, paper_start, paper_end, "v5")
    bt_v6 = run_backtest_coins(COINS_V6, v6_score_ts, paper_start, paper_end, "v6")
    bt_all = pd.concat([bt_v3, bt_v5, bt_v6], ignore_index=True)
    log.info(f"Backtest total: {len(bt_all)} trades")

    # Determine PnL column in backtest
    bt_pnl = "pnl_net" if "pnl_net" in bt_all.columns else "pnl_pct"

    # ═══════════════════════════════════════════════
    # EXP1: Overall comparison
    # ═══════════════════════════════════════════════
    print("\n" + "=" * 60)
    print("EXP1: Overall Paper vs Backtest (same 10-day window)")
    print("=" * 60)

    p_stats = stats(paper, "pnl_net")
    b_stats = stats(bt_all, bt_pnl)
    wr_gap = round(b_stats.get("wr", 0) - p_stats.get("wr", 0), 1)

    results["exp1_overall"] = {
        "paper": p_stats, "backtest": b_stats,
        "wr_gap_pp": wr_gap,
        "period": f"{paper_start} to {paper_end}",
        "days": (paper["exit_time"].max() - paper["entry_time"].min()).days,
    }
    print(f"  Paper:    {p_stats['trades']}t, WR {p_stats['wr']}%, PnL ${p_stats['total_pnl']}")
    print(f"  Backtest: {b_stats['trades']}t, WR {b_stats.get('wr', 'N/A')}%")
    print(f"  >>> WR Gap: {wr_gap}pp")

    # ═══════════════════════════════════════════════
    # EXP2: Per-model version
    # ═══════════════════════════════════════════════
    print("\n" + "=" * 60)
    print("EXP2: Per-Model Version (v3 / v5 / v6)")
    print("=" * 60)

    results["exp2_per_model"] = {}
    for model, bt_sub_df in [("v3", bt_v3), ("v5", bt_v5), ("v6", bt_v6)]:
        p_sub = paper[paper["model"] == model]
        p_s = stats(p_sub, "pnl_net")
        b_s = stats(bt_sub_df, bt_pnl)
        gap = round(b_s.get("wr", 0) - p_s.get("wr", 0), 1) if p_s["trades"] > 0 else None

        results["exp2_per_model"][model] = {
            "paper": p_s, "backtest": b_s, "wr_gap_pp": gap,
        }
        print(f"  {model}: Paper {p_s['trades']}t WR {p_s.get('wr','?')}% | "
              f"BT {b_s['trades']}t WR {b_s.get('wr','?')}% | Gap {gap}pp")

    # ═══════════════════════════════════════════════
    # EXP3: Direction asymmetry
    # ═══════════════════════════════════════════════
    print("\n" + "=" * 60)
    print("EXP3: Direction Asymmetry (LONG vs SHORT)")
    print("=" * 60)

    results["exp3_direction"] = {}
    for d_label, p_val, bt_val in [("LONG", 1, "L"), ("SHORT", -1, "S")]:
        p_sub = paper[paper["direction"] == p_val]
        b_sub = bt_all[bt_all["dir"] == bt_val] if "dir" in bt_all.columns else pd.DataFrame()
        p_s = stats(p_sub, "pnl_net")
        b_s = stats(b_sub, bt_pnl)
        gap = round(b_s.get("wr", 0) - p_s.get("wr", 0), 1) if p_s["trades"] > 0 else None

        results["exp3_direction"][d_label] = {
            "paper": p_s, "backtest": b_s, "wr_gap_pp": gap,
        }
        print(f"  {d_label}: Paper WR {p_s.get('wr','?')}% ({p_s['trades']}t) | "
              f"BT WR {b_s.get('wr','?')}% ({b_s['trades']}t) | Gap {gap}pp")

    # ═══════════════════════════════════════════════
    # EXP4: Exit mechanism
    # ═══════════════════════════════════════════════
    print("\n" + "=" * 60)
    print("EXP4: Exit Mechanism Comparison")
    print("=" * 60)

    paper_exits = {}
    for reason, grp in paper.groupby("exit_reason"):
        paper_exits[reason] = {
            "trades": len(grp), "pct": round(len(grp)/len(paper)*100, 1),
            "wr": round((grp["pnl_net"]>0).mean()*100, 1),
            "total_pnl": round(float(grp["pnl_net"].sum()), 2),
        }

    bt_exits = {}
    if len(bt_all) > 0 and "exit_reason" in bt_all.columns:
        for reason, grp in bt_all.groupby("exit_reason"):
            bt_exits[reason] = {
                "trades": len(grp), "pct": round(len(grp)/len(bt_all)*100, 1),
                "wr": round((grp[bt_pnl]>0).mean()*100, 1),
            }

    results["exp4_exit"] = {"paper": paper_exits, "backtest": bt_exits}
    print("  Paper exit reasons:")
    for r, d in sorted(paper_exits.items(), key=lambda x: -x[1]["trades"]):
        print(f"    {r}: {d['trades']}t ({d['pct']}%), WR {d['wr']}%, PnL ${d['total_pnl']}")
    print("  Backtest exit reasons:")
    for r, d in sorted(bt_exits.items(), key=lambda x: -x[1]["trades"]):
        print(f"    {r}: {d['trades']}t ({d['pct']}%), WR {d['wr']}%")

    # ═══════════════════════════════════════════════
    # EXP5: Entry score distribution
    # ═══════════════════════════════════════════════
    print("\n" + "=" * 60)
    print("EXP5: Entry Score Distribution")
    print("=" * 60)

    results["exp5_scores"] = {}
    if "btc_score_entry" in paper.columns:
        scores = paper["btc_score_entry"].dropna()
        score_dist = {
            "mean": round(float(scores.mean()), 2),
            "median": round(float(scores.median()), 2),
            "std": round(float(scores.std()), 2),
            "min": round(float(scores.min()), 2),
            "max": round(float(scores.max()), 2),
            "pct_positive": round(float((scores > 0).mean() * 100), 1),
            "pct_negative": round(float((scores < 0).mean() * 100), 1),
        }

        # WR by score magnitude
        paper["abs_score"] = paper["btc_score_entry"].abs()
        wr_by_mag = {}
        for lo, hi in [(0, 3), (3, 5), (5, 8), (8, 20)]:
            mask = (paper["abs_score"] >= lo) & (paper["abs_score"] < hi)
            sub = paper[mask]
            if len(sub) > 0:
                wr_by_mag[f"{lo}-{hi}"] = {
                    "trades": len(sub),
                    "wr": round(float((sub["pnl_net"]>0).mean()*100), 1),
                    "avg_pnl": round(float(sub["pnl_net"].mean()), 2),
                }

        # WR by direction + score sign alignment
        alignment = {}
        for d_val, d_label in [(1, "LONG"), (-1, "SHORT")]:
            sub = paper[paper["direction"] == d_val].copy()
            if len(sub) == 0:
                continue
            # Aligned = score sign matches direction
            aligned = sub[
                ((sub["btc_score_entry"] > 0) & (d_val == 1)) |
                ((sub["btc_score_entry"] < 0) & (d_val == -1))
            ]
            misaligned = sub.drop(aligned.index)
            alignment[d_label] = {
                "aligned_n": len(aligned),
                "aligned_wr": round(float((aligned["pnl_net"]>0).mean()*100), 1) if len(aligned) > 0 else None,
                "aligned_pnl": round(float(aligned["pnl_net"].sum()), 2) if len(aligned) > 0 else 0,
                "misaligned_n": len(misaligned),
                "misaligned_wr": round(float((misaligned["pnl_net"]>0).mean()*100), 1) if len(misaligned) > 0 else None,
                "misaligned_pnl": round(float(misaligned["pnl_net"].sum()), 2) if len(misaligned) > 0 else 0,
            }

        results["exp5_scores"] = {
            "distribution": score_dist,
            "wr_by_magnitude": wr_by_mag,
            "alignment": alignment,
        }
        print(f"  Score: mean={score_dist['mean']}, median={score_dist['median']}")
        print(f"  +score: {score_dist['pct_positive']}%, -score: {score_dist['pct_negative']}%")
        for k, v in wr_by_mag.items():
            print(f"  |score| {k}: {v['trades']}t, WR {v['wr']}%, avg ${v['avg_pnl']}")
        for d_label, a in alignment.items():
            print(f"  {d_label} aligned: {a['aligned_n']}t WR {a['aligned_wr']}% (${a['aligned_pnl']}) | "
                  f"misaligned: {a['misaligned_n']}t WR {a['misaligned_wr']}% (${a['misaligned_pnl']})")

    # ═══════════════════════════════════════════════
    # EXP6: Per-coin gap
    # ═══════════════════════════════════════════════
    print("\n" + "=" * 60)
    print("EXP6: Per-Coin Reality Gap")
    print("=" * 60)

    coin_gaps = []
    for coin in sorted(paper["coin"].unique()):
        p_sub = paper[paper["coin"] == coin]
        b_sub = bt_all[bt_all["coin"] == coin] if len(bt_all) > 0 else pd.DataFrame()
        p_wr = float((p_sub["pnl_net"] > 0).mean() * 100)
        b_wr = float((b_sub[bt_pnl] > 0).mean() * 100) if len(b_sub) > 0 else None
        coin_gaps.append({
            "coin": coin,
            "model": p_sub["model"].iloc[0],
            "p_trades": len(p_sub), "p_wr": round(p_wr, 1),
            "p_pnl": round(float(p_sub["pnl_net"].sum()), 2),
            "b_trades": len(b_sub),
            "b_wr": round(b_wr, 1) if b_wr is not None else None,
            "gap": round(b_wr - p_wr, 1) if b_wr is not None else None,
        })

    coin_gaps.sort(key=lambda x: x.get("gap") or 0, reverse=True)
    results["exp6_per_coin"] = {"top5_gap": coin_gaps[:5], "bottom5_gap": coin_gaps[-5:],
                                 "all": coin_gaps}
    print("  Biggest gap (backtest >> paper):")
    for c in coin_gaps[:5]:
        print(f"    {c['coin']} ({c['model']}): Paper WR {c['p_wr']}% vs BT {c['b_wr']}% -> gap {c['gap']}pp")
    print("  Paper outperforms backtest:")
    for c in coin_gaps[-5:]:
        print(f"    {c['coin']} ({c['model']}): Paper WR {c['p_wr']}% vs BT {c['b_wr']}% -> gap {c['gap']}pp")

    # ═══════════════════════════════════════════════
    # EXP7: Holding period
    # ═══════════════════════════════════════════════
    print("\n" + "=" * 60)
    print("EXP7: Holding Period Analysis")
    print("=" * 60)

    p_wins = paper[paper["pnl_net"] > 0]
    p_losses = paper[paper["pnl_net"] <= 0]

    hold_stats = {
        "avg_bars_win": round(float(p_wins["bars_held"].mean()), 1) if len(p_wins) > 0 else None,
        "avg_bars_loss": round(float(p_losses["bars_held"].mean()), 1) if len(p_losses) > 0 else None,
        "median_bars_all": round(float(paper["bars_held"].median()), 1),
        "max_bars": int(paper["bars_held"].max()),
        "pct_timeout": round(float((paper["exit_reason"] == "TIMEOUT").mean() * 100), 1),
    }

    # Holding bins
    hold_bins = {}
    for lo, hi in [(1, 4), (4, 12), (12, 24), (24, 48), (48, 96), (96, 999)]:
        mask = (paper["bars_held"] >= lo) & (paper["bars_held"] < hi)
        sub = paper[mask]
        if len(sub) > 0:
            hold_bins[f"{lo}-{hi}"] = {
                "trades": len(sub),
                "wr": round(float((sub["pnl_net"]>0).mean()*100), 1),
                "avg_pnl": round(float(sub["pnl_net"].mean()), 2),
                "total_pnl": round(float(sub["pnl_net"].sum()), 2),
            }

    results["exp7_holding"] = {"stats": hold_stats, "bins": hold_bins}
    print(f"  Avg bars: win={hold_stats['avg_bars_win']}, loss={hold_stats['avg_bars_loss']}")
    print(f"  Timeout: {hold_stats['pct_timeout']}%")
    for k, v in hold_bins.items():
        print(f"    bars {k}: {v['trades']}t, WR {v['wr']}%, avg ${v['avg_pnl']}, total ${v['total_pnl']}")

    # ═══════════════════════════════════════════════
    # DIAGNOSTIC: Why the gap?
    # ═══════════════════════════════════════════════
    print("\n" + "=" * 60)
    print("DIAGNOSTIC: Gap Root Causes")
    print("=" * 60)

    diag = []

    # 1. LONG performance drag
    p_long = paper[paper["direction"] == 1]
    if len(p_long) > 0 and (p_long["pnl_net"] > 0).mean() < 0.40:
        long_drag = round(float(p_long["pnl_net"].sum()), 2)
        diag.append(f"LONG trades WR {(p_long['pnl_net']>0).mean()*100:.1f}% = drag. Total ${long_drag}")

    # 2. SIGNAL_FLIP exit analysis
    sf = paper[paper["exit_reason"] == "SIGNAL_FLIP"]
    if len(sf) > 0:
        sf_wr = (sf["pnl_net"] > 0).mean() * 100
        sf_pnl = sf["pnl_net"].sum()
        if sf_wr < 40:
            diag.append(f"SIGNAL_FLIP: {len(sf)} trades ({len(sf)/len(paper)*100:.0f}%), "
                       f"WR {sf_wr:.1f}%, PnL ${sf_pnl:.2f}")

    # 3. Low-score entries
    if "btc_score_entry" in paper.columns:
        low_score = paper[paper["btc_score_entry"].abs() < 3]
        if len(low_score) > 0:
            ls_wr = (low_score["pnl_net"] > 0).mean() * 100
            diag.append(f"Low-score (|s|<3): {len(low_score)} trades, WR {ls_wr:.1f}%")

    # 4. Backtest doesn't have SIGNAL_FLIP mechanism
    diag.append("STRUCTURAL: Backtest uses SL/TP/trail exit only. Paper has SIGNAL_FLIP "
                f"({len(sf)}/{len(paper)}={len(sf)/len(paper)*100:.0f}% of trades)")

    results["diagnostic"] = diag
    for d in diag:
        print(f"  >> {d}")

    # ═══════════════════════════════════════════════
    # SAVE
    # ═══════════════════════════════════════════════
    out_path = BASE_DIR / "experiments" / "mission_018_paper_vs_backtest.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False, default=str)
    log.info(f"\nSaved: {out_path}")

    return results


if __name__ == "__main__":
    main()
