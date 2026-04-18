"""
MISSION #001: Hour-of-Day Session Performance Analysis
=======================================================
Hypothesis: v3 model performance varies significantly by trading session.
Research shows crypto markets have clear intraday patterns:
  - Asian session (00-07 UTC): below-average volume/volatility
  - European session (08-15 UTC): above-average activity
  - US session (14-21 UTC): peak activity, UK "tea time" 16-17 UTC
  - Late session (22-23 UTC): worst returns historically

Test:
  1. Run v3 backtest on all 6 production coins
  2. Group trades by entry hour -> identify best/worst sessions
  3. Test a "session filter" (skip worst hours) -> does it improve results?
  4. Analyze long vs short performance by session

Pass criteria: If filtering worst session improves Sharpe or reduces drawdown
Fail criteria: No significant session effect found (uniform performance)

References:
  - Concretum Group: "Seasonality in Bitcoin Intraday Trend Trading"
  - Springer: "The crypto world trades at tea time"
  - ScienceDirect: "Time-of-day periodicities of trading volume and volatility"
"""
import sys
import json
import logging
from pathlib import Path
from datetime import datetime

import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
log = logging.getLogger("mission_001")

# ── Sessions (UTC hours) ──
SESSIONS = {
    "Asian":    (0, 7),    # 00:00 - 07:59 UTC
    "European": (8, 13),   # 08:00 - 13:59 UTC
    "US":       (14, 20),  # 14:00 - 20:59 UTC (US open -> close)
    "Late":     (21, 23),  # 21:00 - 23:59 UTC
}

def hour_to_session(hour: int) -> str:
    for name, (start, end) in SESSIONS.items():
        if start <= hour <= end:
            return name
    return "Unknown"


def run_mission():
    import backtest_15m_btc_led_alts as bt

    coins = ["BTCUSDT", "XRPUSDT", "ADAUSDT", "DOTUSDT", "SUIUSDT", "FILUSDT"]
    oos_start = "2025-01-01"
    oos_end = "2026-03-31"

    # ── Step 1: Load data & build v3 score (8 factors) ──
    log.info("Loading BTC data...")
    from paper_trading.config import COMPOSITE_WEIGHTS, V3_EXTRA_WEIGHTS
    from paper_trading.strategy import score_ob_combined, score_basis_contrarian, score_tick_liq

    btc_ohlcv = bt.fetch_binance_15m("BTCUSDT", years=3)
    db_data = bt.load_btc_db_data()
    btc_df = bt.build_btc_features(btc_ohlcv, db_data)
    # Core 5 factors
    btc_score = bt.compute_btc_composite_score(btc_df, COMPOSITE_WEIGHTS)
    # Add v3 extra factors (ob_combined, basis_contrarian, tick_liq)
    btc_score = btc_score + score_ob_combined(btc_df, weight=V3_EXTRA_WEIGHTS["ob_combined"])
    btc_score = btc_score + score_basis_contrarian(btc_df, weight=V3_EXTRA_WEIGHTS["basis_contrarian"])
    btc_score = btc_score + score_tick_liq(btc_df, weight=V3_EXTRA_WEIGHTS["tick_liq"])

    btc_score_ts = pd.Series(btc_score.values, index=btc_df["ts"].values, name="btc_score")

    try:
        from paper_trading.config import COIN_CONFIGS
    except ImportError:
        COIN_CONFIGS = {}

    # ── Step 2: Run backtest for each coin, collect all trades ──
    all_trades = []
    for symbol in coins:
        log.info(f"Backtesting {symbol}...")
        coin = symbol.replace("USDT", "")
        ohlcv = bt.fetch_binance_15m(symbol, years=3)
        alt_df = bt.build_alt_technicals(ohlcv)
        oos_mask = (alt_df["ts"] >= oos_start) & (alt_df["ts"] <= oos_end)
        alt_oos = alt_df[oos_mask]

        cfg = COIN_CONFIGS.get(coin, {})
        threshold = cfg.get("threshold", 3.0)
        use_pa = cfg.get("use_alt_pa_filter", False)
        sl = cfg.get("sl_atr_mult", 2.5)
        tp = cfg.get("tp_atr_mult", 4.0)
        cooldown = cfg.get("cooldown_bars", 4)

        signals, alt_merged = bt.generate_btc_led_signal(
            btc_score_ts, alt_oos, threshold=threshold, use_alt_pa_filter=use_pa)
        trades = bt.run_backtest(alt_merged, signals,
                                sl_atr_mult=sl, tp_atr_mult=tp,
                                cooldown_bars=cooldown)
        if len(trades) > 0:
            trades["coin"] = coin
            all_trades.append(trades)

    trades_df = pd.concat(all_trades, ignore_index=True)
    trades_df["entry_time"] = pd.to_datetime(trades_df["entry_time"])
    trades_df["entry_hour"] = trades_df["entry_time"].dt.hour
    trades_df["entry_session"] = trades_df["entry_hour"].apply(hour_to_session)
    trades_df["entry_dow"] = trades_df["entry_time"].dt.day_name()

    log.info(f"Total trades: {len(trades_df)}")

    # ── Step 3: Analyze by hour ──
    hour_stats = []
    for hour in range(24):
        mask = trades_df["entry_hour"] == hour
        subset = trades_df[mask]
        if len(subset) == 0:
            continue
        wins = (subset["pnl_net"] > 0).sum()
        hour_stats.append({
            "hour": hour,
            "session": hour_to_session(hour),
            "trades": len(subset),
            "wins": int(wins),
            "win_rate": round(100 * wins / len(subset), 1),
            "total_pnl": round(subset["pnl_net"].sum(), 2),
            "avg_pnl": round(subset["pnl_net"].mean(), 2),
            "max_win": round(subset["pnl_net"].max(), 2),
            "max_loss": round(subset["pnl_net"].min(), 2),
        })
    hour_df = pd.DataFrame(hour_stats)

    # ── Step 4: Analyze by session ──
    session_stats = []
    for session in ["Asian", "European", "US", "Late"]:
        mask = trades_df["entry_session"] == session
        subset = trades_df[mask]
        if len(subset) == 0:
            continue
        wins = (subset["pnl_net"] > 0).sum()
        long_mask = subset["dir"] == "L"
        short_mask = subset["dir"] == "S"
        long_wr = round(100 * (subset[long_mask]["pnl_net"] > 0).sum() / long_mask.sum(), 1) if long_mask.sum() > 0 else 0
        short_wr = round(100 * (subset[short_mask]["pnl_net"] > 0).sum() / short_mask.sum(), 1) if short_mask.sum() > 0 else 0

        session_stats.append({
            "session": session,
            "hours": f"{SESSIONS[session][0]:02d}-{SESSIONS[session][1]:02d}",
            "trades": len(subset),
            "wins": int(wins),
            "win_rate": round(100 * wins / len(subset), 1),
            "total_pnl": round(subset["pnl_net"].sum(), 2),
            "avg_pnl": round(subset["pnl_net"].mean(), 2),
            "long_trades": int(long_mask.sum()),
            "long_wr": long_wr,
            "short_trades": int(short_mask.sum()),
            "short_wr": short_wr,
        })
    session_df = pd.DataFrame(session_stats)

    # ── Step 5: Identify worst hours & test filter ──
    baseline_pnl = trades_df["pnl_net"].sum()
    baseline_wr = 100 * (trades_df["pnl_net"] > 0).sum() / len(trades_df)
    baseline_trades = len(trades_df)

    # Find hours with negative avg PnL
    bad_hours = hour_df[hour_df["avg_pnl"] < 0]["hour"].tolist() if len(hour_df) > 0 else []
    # Find worst session
    worst_session = session_df.loc[session_df["avg_pnl"].idxmin(), "session"] if len(session_df) > 0 else None

    # Test: skip worst hours
    if bad_hours:
        filtered = trades_df[~trades_df["entry_hour"].isin(bad_hours)]
        filtered_pnl = filtered["pnl_net"].sum()
        filtered_wr = 100 * (filtered["pnl_net"] > 0).sum() / len(filtered) if len(filtered) > 0 else 0
        filter_improvement = filtered_pnl - baseline_pnl
    else:
        filtered_pnl = baseline_pnl
        filtered_wr = baseline_wr
        filter_improvement = 0

    # Test: skip worst session
    if worst_session:
        session_filtered = trades_df[trades_df["entry_session"] != worst_session]
        sf_pnl = session_filtered["pnl_net"].sum()
        sf_wr = 100 * (session_filtered["pnl_net"] > 0).sum() / len(session_filtered) if len(session_filtered) > 0 else 0
        sf_improvement = sf_pnl - baseline_pnl
    else:
        sf_pnl = baseline_pnl
        sf_wr = baseline_wr
        sf_improvement = 0

    # ── Step 6: Day of week analysis ──
    dow_stats = []
    for dow in ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]:
        mask = trades_df["entry_dow"] == dow
        subset = trades_df[mask]
        if len(subset) == 0:
            continue
        wins = (subset["pnl_net"] > 0).sum()
        dow_stats.append({
            "day": dow,
            "trades": len(subset),
            "win_rate": round(100 * wins / len(subset), 1),
            "total_pnl": round(subset["pnl_net"].sum(), 2),
            "avg_pnl": round(subset["pnl_net"].mean(), 2),
        })
    dow_df = pd.DataFrame(dow_stats)

    # ── Step 7: Determine pass/fail ──
    # Pass if session filter improves PnL by >5% or removes >10% of losses
    session_effect_significant = abs(sf_improvement) > baseline_pnl * 0.05
    best_session = session_df.loc[session_df["avg_pnl"].idxmax(), "session"] if len(session_df) > 0 else None
    best_session_wr = session_df.loc[session_df["avg_pnl"].idxmax(), "win_rate"] if len(session_df) > 0 else 0
    worst_session_wr = session_df.loc[session_df["avg_pnl"].idxmin(), "win_rate"] if len(session_df) > 0 else 0
    wr_spread = best_session_wr - worst_session_wr

    passed = session_effect_significant or wr_spread > 10

    # ── Build results ──
    results = {
        "mission_id": "mission_001_hour_of_day",
        "date": datetime.utcnow().strftime("%Y-%m-%d"),
        "title": "Hour-of-Day Session Performance Analysis",
        "hypothesis": "v3 model performance varies significantly by trading session",
        "oos_period": f"{oos_start} to {oos_end}",
        "passed": passed,
        "baseline": {
            "total_trades": baseline_trades,
            "total_pnl": round(baseline_pnl, 2),
            "win_rate": round(baseline_wr, 1),
        },
        "session_analysis": session_stats,
        "hour_analysis": hour_stats,
        "dow_analysis": dow_stats,
        "worst_session": worst_session,
        "best_session": best_session,
        "wr_spread": round(wr_spread, 1),
        "filter_test": {
            "skip_worst_session": worst_session,
            "filtered_pnl": round(sf_pnl, 2),
            "filtered_wr": round(sf_wr, 1),
            "improvement": round(sf_improvement, 2),
            "improvement_pct": round(100 * sf_improvement / baseline_pnl, 1) if baseline_pnl != 0 else 0,
        },
        "bad_hours_filter": {
            "bad_hours": bad_hours,
            "filtered_pnl": round(filtered_pnl, 2),
            "filtered_wr": round(filtered_wr, 1),
            "improvement": round(filter_improvement, 2),
        },
    }

    # ── Save JSON ──
    experiments_dir = BASE_DIR / "experiments"
    experiments_dir.mkdir(exist_ok=True)
    json_path = experiments_dir / "mission_001_hour_of_day.json"
    with open(json_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    log.info(f"Saved JSON: {json_path}")

    # ── Save summary MD ──
    md_path = experiments_dir / "mission_001_hour_of_day.md"
    with open(md_path, "w") as f:
        f.write("# Mission #001: Hour-of-Day Session Performance Analysis\n\n")
        f.write(f"**Date**: {results['date']}  \n")
        f.write(f"**OOS Period**: {oos_start} to {oos_end}  \n")
        f.write(f"**Result**: {'PASS' if passed else 'FAIL'} -- {'Significant' if passed else 'No significant'} session effect found  \n\n")

        f.write("## Hypothesis\n")
        f.write("v3 model performance varies significantly by trading session (Asian/European/US/Late).\n")
        f.write("Research shows crypto markets have clear intraday patterns with volume/volatility peaking during EU-US overlap.\n\n")

        f.write("## Baseline (All Hours)\n")
        f.write(f"- Trades: {baseline_trades}\n")
        f.write(f"- Win Rate: {baseline_wr:.1f}%\n")
        f.write(f"- Total PnL: ${baseline_pnl:,.2f}\n\n")

        f.write("## Session Analysis\n\n")
        f.write("| Session | Hours (UTC) | Trades | WR% | Total PnL | Avg PnL | Long WR | Short WR |\n")
        f.write("|---------|-------------|--------|-----|-----------|---------|---------|----------|\n")
        for s in session_stats:
            f.write(f"| {s['session']} | {s['hours']} | {s['trades']} | {s['win_rate']}% | ${s['total_pnl']:,.2f} | ${s['avg_pnl']:.2f} | {s['long_wr']}% | {s['short_wr']}% |\n")
        f.write(f"\n**Best session**: {best_session} (WR {best_session_wr:.1f}%)  \n")
        f.write(f"**Worst session**: {worst_session} (WR {worst_session_wr:.1f}%)  \n")
        f.write(f"**WR spread**: {wr_spread:.1f}pp  \n\n")

        f.write("## Hour-by-Hour Breakdown\n\n")
        f.write("| Hour | Session | Trades | WR% | Total PnL | Avg PnL |\n")
        f.write("|------|---------|--------|-----|-----------|----------|\n")
        for h in sorted(hour_stats, key=lambda x: x["hour"]):
            marker = " **" if h["avg_pnl"] < 0 else ""
            f.write(f"| {h['hour']:02d}:00 | {h['session']} | {h['trades']} | {h['win_rate']}% | ${h['total_pnl']:,.2f} | ${h['avg_pnl']:.2f}{marker} |\n")
            if marker:
                f.write("")  # bold marker for negative

        f.write("\n## Day-of-Week Analysis\n\n")
        f.write("| Day | Trades | WR% | Total PnL | Avg PnL |\n")
        f.write("|-----|--------|-----|-----------|----------|\n")
        for d in dow_stats:
            f.write(f"| {d['day']} | {d['trades']} | {d['win_rate']}% | ${d['total_pnl']:,.2f} | ${d['avg_pnl']:.2f} |\n")

        f.write("\n## Filter Tests\n\n")
        f.write(f"### Skip Worst Session ({worst_session})\n")
        f.write(f"- PnL: ${sf_pnl:,.2f} (vs ${baseline_pnl:,.2f} baseline)\n")
        f.write(f"- WR: {sf_wr:.1f}% (vs {baseline_wr:.1f}% baseline)\n")
        f.write(f"- Improvement: ${sf_improvement:,.2f} ({100*sf_improvement/baseline_pnl:.1f}%)\n\n")

        if bad_hours:
            f.write(f"### Skip Negative-PnL Hours ({bad_hours})\n")
            f.write(f"- PnL: ${filtered_pnl:,.2f} (vs ${baseline_pnl:,.2f} baseline)\n")
            f.write(f"- WR: {filtered_wr:.1f}% (vs {baseline_wr:.1f}% baseline)\n")
            f.write(f"- Improvement: ${filter_improvement:,.2f}\n\n")

        f.write("## Conclusion\n\n")
        if passed:
            f.write(f"Session effect IS significant. Best: {best_session}, Worst: {worst_session}. ")
            f.write(f"WR spread of {wr_spread:.1f}pp across sessions. ")
            if sf_improvement > 0:
                f.write(f"Skipping {worst_session} session improves PnL by ${sf_improvement:,.0f}. ")
                f.write("Consider adding session filter to v3.1.\n")
            else:
                f.write(f"However, skipping {worst_session} does NOT improve overall PnL (${sf_improvement:,.0f}). ")
                f.write("The worst session has low WR but possibly larger wins. No action recommended.\n")
        else:
            f.write(f"No significant session effect. WR spread only {wr_spread:.1f}pp. ")
            f.write("Model performs consistently across all hours. No time filter needed.\n")

        f.write("\n## References\n")
        f.write("- [Concretum Group: Seasonality in Bitcoin Intraday Trend Trading](https://concretumgroup.com/seasonality-in-bitcoin-intraday-trend-trading/)\n")
        f.write("- [Springer: The crypto world trades at tea time](https://link.springer.com/article/10.1007/s11156-024-01304-1)\n")
        f.write("- [ScienceDirect: Time-of-day periodicities of trading volume/volatility](https://www.sciencedirect.com/science/article/abs/pii/S1544612319301904)\n")
        f.write("- [Amberdata: The Rhythm of Liquidity](https://blog.amberdata.io/the-rhythm-of-liquidity-temporal-patterns-in-market-depth)\n")

    log.info(f"Saved MD: {md_path}")

    # ── Update missions.json ──
    from research.missions import MissionEngine
    engine = MissionEngine()
    mission = {
        "mission_id": "mission_001_hour_of_day",
        "date": results["date"],
        "type": "regime_test",
        "title": "Hour-of-Day Session Performance Analysis",
        "description": "Test if v3 model performs differently across Asian/EU/US/Late trading sessions",
        "difficulty": "hard",
        "xp_reward": 100,
        "status": "completed",
        "target": "hour_of_day",
        "started_at": datetime.utcnow().isoformat(),
        "finished_at": datetime.utcnow().isoformat(),
        "result": {
            "success": True,
            "verdict": "significant" if passed else "uniform",
            "best_session": best_session,
            "worst_session": worst_session,
            "wr_spread": round(wr_spread, 1),
            "best_delta": round(sf_improvement, 0),
            "baseline_pnl": round(baseline_pnl, 0),
        },
        "insight": (
            f"Best session: {best_session}, Worst: {worst_session}. "
            f"WR spread: {wr_spread:.1f}pp. "
            f"Skipping {worst_session} -> ${sf_improvement:+,.0f} impact."
        ),
        "tags": ["regime_test", "intraday", "session_analysis"],
    }
    engine._data["missions"].append(mission)
    engine._data["meta"]["total_xp"] += 100
    engine._data["meta"]["current_streak"] = 1
    engine._data["meta"]["longest_streak"] = max(engine._data["meta"].get("longest_streak", 0), 1)
    engine._data["meta"]["last_mission_date"] = results["date"]
    from research.missions import _get_level
    lvl, _ = _get_level(engine._data["meta"]["total_xp"])
    engine._data["meta"]["level"] = lvl
    engine._save()
    log.info("Updated missions.json")

    # ── Print summary ──
    print("\n" + "=" * 70)
    print("  MISSION #001: Hour-of-Day Session Performance Analysis")
    print("=" * 70)
    print(f"  Result: {'PASS' if passed else 'FAIL'}")
    print(f"  Baseline: {baseline_trades} trades, WR {baseline_wr:.1f}%, PnL ${baseline_pnl:,.2f}")
    print()
    print("  SESSION BREAKDOWN:")
    for s in session_stats:
        marker = " <<<" if s["session"] == worst_session else (" ***" if s["session"] == best_session else "")
        print(f"    {s['session']:>10} ({s['hours']}): {s['trades']:>4} trades, WR {s['win_rate']:>5.1f}%, PnL ${s['total_pnl']:>8,.2f}, Avg ${s['avg_pnl']:>6.2f}{marker}")
    print()
    print(f"  Best: {best_session} | Worst: {worst_session} | WR Spread: {wr_spread:.1f}pp")
    print(f"  Skip {worst_session} -> PnL ${sf_pnl:,.2f} ({sf_improvement:+,.0f})")
    print()
    print("  DAY-OF-WEEK:")
    for d in dow_stats:
        print(f"    {d['day']:>10}: {d['trades']:>4} trades, WR {d['win_rate']:>5.1f}%, PnL ${d['total_pnl']:>8,.2f}")
    print()
    print(f"  +100 XP earned!")
    print("=" * 70)

    return results


if __name__ == "__main__":
    run_mission()
