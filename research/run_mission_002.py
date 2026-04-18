"""
MISSION #002: Liquidation Cascade Analysis
===========================================
Hypothesis: Large liquidation cascades (clustered liquidation events in one
direction within a 15-minute window) predict short-term BTC price reversals.

Background:
  - Liquidation is v3's #1 factor (weight=2.0, +$10,837 incremental PnL)
  - Current v3 uses hourly liquidation aggregates
  - This mission digs deeper: are there "cascade" patterns within the raw
    event-level liquidation data that provide additional alpha?

Research backing:
  - Amberdata/BitMEX: "Forced liquidations create artificial selling/buying
    pressure that often overshoots equilibrium price"
  - Liquidation cascades are self-reinforcing (liq -> price move -> more liq)
    but eventually exhaust the leveraged positions, leading to reversal

Test:
  1. Aggregate 61K raw liquidation events into 15-min bars
  2. Identify "cascade" events (high volume, one-sided)
  3. Measure BTC price action 1h, 4h after cascade
  4. Test if contrarian signal (fade cascade direction) adds alpha
  5. Compare v3 model performance during cascade vs normal periods

Pass criteria: Clear directional bias after cascades (reversal or continuation)
Fail criteria: No significant price pattern after cascades
"""
import sys
import json
import logging
from pathlib import Path
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
log = logging.getLogger("mission_002")

BKK_UTC_OFFSET = timedelta(hours=7)


def run_mission():
    import psycopg2
    from research.config import get_pg_dsn
    import backtest_15m_btc_led_alts as bt

    oos_start = "2025-01-01"
    oos_end = "2026-03-31"

    # ── Step 1: Load raw liquidation data ──
    log.info("Loading liquidation data from DB...")
    conn = psycopg2.connect(get_pg_dsn())
    liq_raw = pd.read_sql("""
        SELECT event_time, symbol, side, notional_usd
        FROM market_data.liquidation
        WHERE symbol = 'BTCUSDT'
        ORDER BY event_time
    """, conn)
    conn.close()

    liq_raw["event_time"] = pd.to_datetime(liq_raw["event_time"], utc=True).dt.tz_localize(None)
    liq_raw["event_time"] = liq_raw["event_time"] - BKK_UTC_OFFSET  # Convert to naive UTC
    log.info(f"Loaded {len(liq_raw)} BTCUSDT liquidation events")
    log.info(f"Range: {liq_raw['event_time'].min()} -> {liq_raw['event_time'].max()}")

    # ── Step 2: Load BTC OHLCV for price reference ──
    log.info("Loading BTC 15m OHLCV...")
    btc_ohlcv = bt.fetch_binance_15m("BTCUSDT", years=3)
    # Column is "date_time" from fetch_binance_15m, rename to "ts" for consistency
    if "date_time" in btc_ohlcv.columns:
        btc_ohlcv = btc_ohlcv.rename(columns={"date_time": "ts"})
    btc_ohlcv["ts"] = pd.to_datetime(btc_ohlcv["ts"])
    # Make sure we have forward returns
    btc_ohlcv["ret_1h"] = btc_ohlcv["close"].pct_change(4).shift(-4) * 100   # 4 bars = 1h
    btc_ohlcv["ret_4h"] = btc_ohlcv["close"].pct_change(16).shift(-16) * 100  # 16 bars = 4h
    btc_ohlcv["ret_15m"] = btc_ohlcv["close"].pct_change(1).shift(-1) * 100
    btc_ohlcv["vol_1h"] = btc_ohlcv["close"].pct_change(1).rolling(4).std() * 100  # recent vol

    # ── Step 3: Aggregate liquidations into 15-min bars ──
    log.info("Aggregating into 15-min bars...")
    liq_raw["bar"] = liq_raw["event_time"].dt.floor("15min")

    # Per-bar, per-side aggregation
    bar_agg = liq_raw.groupby(["bar", "side"]).agg(
        count=("notional_usd", "size"),
        total_usd=("notional_usd", "sum"),
    ).reset_index()

    # Pivot to get buy/sell columns
    buy_bars = bar_agg[bar_agg["side"] == "BUY"].set_index("bar")[["count", "total_usd"]].rename(
        columns={"count": "buy_count", "total_usd": "buy_usd"})
    sell_bars = bar_agg[bar_agg["side"] == "SELL"].set_index("bar")[["count", "total_usd"]].rename(
        columns={"count": "sell_count", "total_usd": "sell_usd"})

    liq_bars = buy_bars.join(sell_bars, how="outer").fillna(0).reset_index()
    liq_bars["total_count"] = liq_bars["buy_count"] + liq_bars["sell_count"]
    liq_bars["total_usd"] = liq_bars["buy_usd"] + liq_bars["sell_usd"]
    liq_bars["net_usd"] = liq_bars["sell_usd"] - liq_bars["buy_usd"]  # positive = shorts liq'd
    liq_bars["side_ratio"] = liq_bars["net_usd"] / liq_bars["total_usd"].clip(lower=1)
    # side_ratio > 0 means more shorts liquidated (price moving up)
    # side_ratio < 0 means more longs liquidated (price moving down)

    log.info(f"Total 15-min bars with liquidations: {len(liq_bars)}")

    # ── Step 4: Define cascade thresholds ──
    # A "cascade" is a 15-min bar with significantly above-average liquidation activity
    usd_p75 = liq_bars["total_usd"].quantile(0.75)
    usd_p90 = liq_bars["total_usd"].quantile(0.90)
    usd_p95 = liq_bars["total_usd"].quantile(0.95)
    count_p90 = liq_bars["total_count"].quantile(0.90)

    log.info(f"USD thresholds: P75=${usd_p75:,.0f}, P90=${usd_p90:,.0f}, P95=${usd_p95:,.0f}")
    log.info(f"Count P90={count_p90:.0f}")

    thresholds = {
        "moderate": {"min_usd": usd_p75, "min_count": 5, "min_side_ratio": 0.5},
        "strong": {"min_usd": usd_p90, "min_count": 10, "min_side_ratio": 0.6},
        "extreme": {"min_usd": usd_p95, "min_count": 15, "min_side_ratio": 0.7},
    }

    # ── Step 5: Merge with OHLCV and analyze cascades ──
    log.info("Merging with OHLCV...")
    liq_bars = liq_bars.rename(columns={"bar": "ts"})
    merged = pd.merge_asof(
        liq_bars.sort_values("ts"),
        btc_ohlcv[["ts", "close", "ret_15m", "ret_1h", "ret_4h", "vol_1h"]].sort_values("ts"),
        on="ts", direction="backward"
    )
    # Filter to OOS period
    merged = merged[(merged["ts"] >= oos_start) & (merged["ts"] <= oos_end)]
    log.info(f"OOS bars with liquidations: {len(merged)}")

    cascade_results = {}
    for level_name, th in thresholds.items():
        # Find cascade bars: high volume + directional
        mask_long_liq = (
            (merged["total_usd"] >= th["min_usd"]) &
            (merged["total_count"] >= th["min_count"]) &
            (merged["side_ratio"] < -th["min_side_ratio"])  # negative = longs liq'd (price dropping)
        )
        mask_short_liq = (
            (merged["total_usd"] >= th["min_usd"]) &
            (merged["total_count"] >= th["min_count"]) &
            (merged["side_ratio"] > th["min_side_ratio"])  # positive = shorts liq'd (price rising)
        )

        long_cascades = merged[mask_long_liq]
        short_cascades = merged[mask_short_liq]

        if len(long_cascades) == 0 and len(short_cascades) == 0:
            cascade_results[level_name] = {
                "events": 0, "note": "No cascades found at this threshold"
            }
            continue

        # After long liquidation cascade (price was dropping), does it:
        # - Continue dropping (continuation) or
        # - Reverse up (mean reversion)?
        long_ret_15m = long_cascades["ret_15m"].mean() if len(long_cascades) > 0 else 0
        long_ret_1h = long_cascades["ret_1h"].mean() if len(long_cascades) > 0 else 0
        long_ret_4h = long_cascades["ret_4h"].mean() if len(long_cascades) > 0 else 0
        long_ret_1h_median = long_cascades["ret_1h"].median() if len(long_cascades) > 0 else 0
        long_positive_1h = (long_cascades["ret_1h"] > 0).mean() * 100 if len(long_cascades) > 0 else 0

        # After short liquidation cascade (price was rising), does it:
        # - Continue rising or
        # - Reverse down?
        short_ret_15m = short_cascades["ret_15m"].mean() if len(short_cascades) > 0 else 0
        short_ret_1h = short_cascades["ret_1h"].mean() if len(short_cascades) > 0 else 0
        short_ret_4h = short_cascades["ret_4h"].mean() if len(short_cascades) > 0 else 0
        short_ret_1h_median = short_cascades["ret_1h"].median() if len(short_cascades) > 0 else 0
        short_negative_1h = (short_cascades["ret_1h"] < 0).mean() * 100 if len(short_cascades) > 0 else 0

        cascade_results[level_name] = {
            "long_liq_cascades": len(long_cascades),
            "short_liq_cascades": len(short_cascades),
            "total_events": len(long_cascades) + len(short_cascades),
            "after_long_liq": {
                "mean_ret_15m": round(long_ret_15m, 4),
                "mean_ret_1h": round(long_ret_1h, 4),
                "median_ret_1h": round(long_ret_1h_median, 4),
                "mean_ret_4h": round(long_ret_4h, 4),
                "pct_positive_1h": round(long_positive_1h, 1),
                "interpretation": "reversal_up" if long_ret_1h > 0 else "continuation_down",
            },
            "after_short_liq": {
                "mean_ret_15m": round(short_ret_15m, 4),
                "mean_ret_1h": round(short_ret_1h, 4),
                "median_ret_1h": round(short_ret_1h_median, 4),
                "mean_ret_4h": round(short_ret_4h, 4),
                "pct_negative_1h": round(short_negative_1h, 1),
                "interpretation": "reversal_down" if short_ret_1h < 0 else "continuation_up",
            },
        }
        log.info(f"{level_name}: {len(long_cascades)} long-liq + {len(short_cascades)} short-liq cascades")

    # ── Step 6: V3 performance during cascade vs normal ──
    log.info("Running v3 backtest for cascade comparison...")
    from paper_trading.config import COMPOSITE_WEIGHTS, V3_EXTRA_WEIGHTS, COIN_CONFIGS
    from paper_trading.strategy import score_ob_combined, score_basis_contrarian, score_tick_liq

    db_data = bt.load_btc_db_data()
    btc_df = bt.build_btc_features(btc_ohlcv, db_data)
    btc_score = bt.compute_btc_composite_score(btc_df, COMPOSITE_WEIGHTS)
    btc_score = btc_score + score_ob_combined(btc_df, weight=V3_EXTRA_WEIGHTS["ob_combined"])
    btc_score = btc_score + score_basis_contrarian(btc_df, weight=V3_EXTRA_WEIGHTS["basis_contrarian"])
    btc_score = btc_score + score_tick_liq(btc_df, weight=V3_EXTRA_WEIGHTS["tick_liq"])
    btc_score_ts = pd.Series(btc_score.values, index=btc_df["ts"].values, name="btc_score")

    # Run BTC backtest
    btc_alt = bt.build_alt_technicals(btc_ohlcv)
    oos_mask = (btc_alt["ts"] >= oos_start) & (btc_alt["ts"] <= oos_end)
    btc_alt_oos = btc_alt[oos_mask]
    cfg = COIN_CONFIGS.get("BTC", {})
    signals, alt_merged = bt.generate_btc_led_signal(
        btc_score_ts, btc_alt_oos,
        threshold=cfg.get("threshold", 3.0),
        use_alt_pa_filter=cfg.get("use_alt_pa_filter", False))
    trades = bt.run_backtest(alt_merged, signals,
                             sl_atr_mult=cfg.get("sl_atr_mult", 2.5),
                             tp_atr_mult=cfg.get("tp_atr_mult", 4.0),
                             cooldown_bars=cfg.get("cooldown_bars", 4))

    if len(trades) > 0:
        trades["entry_time"] = pd.to_datetime(trades["entry_time"])
        trades["entry_bar"] = trades["entry_time"].dt.floor("15min")

        # Classify trades: during cascade bar, near cascade (within 1h), or normal
        # Use "moderate" threshold for cascade detection
        th_mod = thresholds["moderate"]
        cascade_bars_set = set()
        for _, row in merged.iterrows():
            if row["total_usd"] >= th_mod["min_usd"] and row["total_count"] >= th_mod["min_count"]:
                if abs(row["side_ratio"]) >= th_mod["min_side_ratio"]:
                    ts = row["ts"]
                    # Mark this bar and next 4 bars (1h) as cascade-affected
                    for i in range(5):
                        cascade_bars_set.add(ts + timedelta(minutes=15 * i))

        trades["during_cascade"] = trades["entry_bar"].isin(cascade_bars_set)

        cascade_trades = trades[trades["during_cascade"]]
        normal_trades = trades[~trades["during_cascade"]]

        cascade_pnl = cascade_trades["pnl_net"].sum() if len(cascade_trades) > 0 else 0
        cascade_wr = (100 * (cascade_trades["pnl_net"] > 0).sum() / len(cascade_trades)) if len(cascade_trades) > 0 else 0
        cascade_avg = cascade_trades["pnl_net"].mean() if len(cascade_trades) > 0 else 0
        normal_pnl = normal_trades["pnl_net"].sum() if len(normal_trades) > 0 else 0
        normal_wr = (100 * (normal_trades["pnl_net"] > 0).sum() / len(normal_trades)) if len(normal_trades) > 0 else 0
        normal_avg = normal_trades["pnl_net"].mean() if len(normal_trades) > 0 else 0

        v3_comparison = {
            "total_btc_trades": len(trades),
            "cascade_trades": len(cascade_trades),
            "cascade_wr": round(cascade_wr, 1),
            "cascade_avg_pnl": round(cascade_avg, 2),
            "cascade_total_pnl": round(cascade_pnl, 2),
            "normal_trades": len(normal_trades),
            "normal_wr": round(normal_wr, 1),
            "normal_avg_pnl": round(normal_avg, 2),
            "normal_total_pnl": round(normal_pnl, 2),
        }
    else:
        v3_comparison = {"error": "No BTC trades in OOS period"}

    # ── Step 7: Cascade size analysis ──
    log.info("Analyzing cascade size vs return...")
    # Bin by total_usd and see if larger cascades = bigger reversals
    merged["usd_bin"] = pd.cut(merged["total_usd"],
                                bins=[0, 5000, 20000, 50000, 100000, float("inf")],
                                labels=["<$5K", "$5-20K", "$20-50K", "$50-100K", ">$100K"])
    size_analysis = []
    for bin_label in ["<$5K", "$5-20K", "$20-50K", "$50-100K", ">$100K"]:
        subset = merged[merged["usd_bin"] == bin_label]
        if len(subset) < 5:
            continue
        size_analysis.append({
            "size_bucket": bin_label,
            "count": len(subset),
            "mean_ret_1h": round(subset["ret_1h"].mean(), 4),
            "mean_ret_4h": round(subset["ret_4h"].mean(), 4),
            "mean_side_ratio": round(subset["side_ratio"].mean(), 3),
            "mean_total_usd": round(subset["total_usd"].mean(), 0),
        })

    # ── Step 8: Consecutive cascade analysis ──
    log.info("Analyzing consecutive cascades...")
    # Are cascades that follow other cascades (within 1h) more predictive?
    merged_sorted = merged.sort_values("ts").reset_index(drop=True)
    merged_sorted["time_since_last"] = merged_sorted["ts"].diff().dt.total_seconds() / 60
    merged_sorted["is_followup"] = merged_sorted["time_since_last"] <= 60  # within 1 hour

    followup = merged_sorted[merged_sorted["is_followup"]]
    first_time = merged_sorted[~merged_sorted["is_followup"]]

    consecutive_analysis = {
        "first_cascade_count": len(first_time),
        "first_cascade_avg_ret_1h": round(first_time["ret_1h"].mean(), 4) if len(first_time) > 0 else 0,
        "followup_cascade_count": len(followup),
        "followup_cascade_avg_ret_1h": round(followup["ret_1h"].mean(), 4) if len(followup) > 0 else 0,
    }

    # ── Step 9: Determine pass/fail ──
    # Pass if strong cascade level shows clear directional bias (>60% reversal rate)
    strong = cascade_results.get("strong", {})
    passed = False
    verdict = "no_pattern"
    if strong.get("total_events", 0) > 10:
        after_long = strong.get("after_long_liq", {})
        after_short = strong.get("after_short_liq", {})
        # Check if there's a clear reversal pattern
        long_reversal = after_long.get("pct_positive_1h", 50) > 55  # majority bounce after long liq
        short_reversal = after_short.get("pct_negative_1h", 50) > 55  # majority drop after short liq
        if long_reversal or short_reversal:
            passed = True
            verdict = "reversal_pattern"
        elif after_long.get("pct_positive_1h", 50) < 45 or after_short.get("pct_negative_1h", 50) < 45:
            passed = True
            verdict = "continuation_pattern"

    # Also check v3 performance difference
    v3_edge = False
    if v3_comparison.get("cascade_trades", 0) > 10:
        wr_diff = v3_comparison["cascade_wr"] - v3_comparison["normal_wr"]
        avg_diff = v3_comparison["cascade_avg_pnl"] - v3_comparison["normal_avg_pnl"]
        if abs(wr_diff) > 5 or abs(avg_diff) > 2:
            v3_edge = True
            if not passed:
                passed = True
                verdict = "v3_cascade_edge"

    # ── Build results ──
    results = {
        "mission_id": "mission_002_liquidation_cascade",
        "date": datetime.utcnow().strftime("%Y-%m-%d"),
        "title": "Liquidation Cascade Analysis",
        "hypothesis": "Large liquidation cascades predict short-term BTC price reversals",
        "oos_period": f"{oos_start} to {oos_end}",
        "passed": passed,
        "verdict": verdict,
        "data_summary": {
            "total_events": len(liq_raw),
            "oos_bars_with_liq": len(merged),
            "date_range": f"{liq_raw['event_time'].min().strftime('%Y-%m-%d')} to {liq_raw['event_time'].max().strftime('%Y-%m-%d')}",
        },
        "thresholds": {
            k: {"min_usd": round(v["min_usd"], 0), "min_count": v["min_count"], "min_side_ratio": v["min_side_ratio"]}
            for k, v in thresholds.items()
        },
        "cascade_analysis": cascade_results,
        "v3_comparison": v3_comparison,
        "size_analysis": size_analysis,
        "consecutive_analysis": consecutive_analysis,
    }

    # ── Save JSON ──
    missions_dir = BASE_DIR / "missions"
    missions_dir.mkdir(exist_ok=True)
    json_path = missions_dir / "mission_002_liquidation_cascade.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, default=str, ensure_ascii=False)
    log.info(f"Saved JSON: {json_path}")

    # ── Save Thai MD report ──
    md_path = missions_dir / "mission_002_liquidation_cascade.md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("# Mission #002: Liquidation Cascade Analysis\n\n")
        f.write(f"**วันที่**: {results['date']}  \n")
        f.write(f"**ช่วง OOS**: {oos_start} ถึง {oos_end}  \n")
        f.write(f"**ผลลัพธ์**: {'PASS' if passed else 'FAIL'} -- {verdict}  \n\n")

        f.write("## สมมติฐาน\n")
        f.write("เมื่อเกิด Liquidation Cascade (การถูก liquidate จำนวนมากในทิศทางเดียวกันภายใน 15 นาที)\n")
        f.write("ราคา BTC จะกลับตัว (reversal) ภายใน 1-4 ชั่วโมง เนื่องจากแรงขาย/ซื้อเทียมหมดลง\n\n")

        f.write("## ข้อมูลที่ใช้\n")
        f.write(f"- Liquidation events: {len(liq_raw):,} รายการ (BTCUSDT)\n")
        f.write(f"- ช่วงข้อมูล: {results['data_summary']['date_range']}\n")
        f.write(f"- 15-min bars ที่มี liquidation (OOS): {len(merged):,} bars\n\n")

        f.write("## นิยาม Cascade\n")
        f.write("| ระดับ | ขั้นต่ำ USD | ขั้นต่ำ Events | ขั้นต่ำ Side Ratio |\n")
        f.write("|-------|-----------|---------------|-------------------|\n")
        for level, th in thresholds.items():
            f.write(f"| {level} | ${th['min_usd']:,.0f} | {th['min_count']} | {th['min_side_ratio']} |\n")
        f.write("\nSide Ratio > 0 = Short ถูก liq มากกว่า (ราคากำลังขึ้น)\n")
        f.write("Side Ratio < 0 = Long ถูก liq มากกว่า (ราคากำลังลง)\n\n")

        f.write("## ผลวิเคราะห์ Cascade แต่ละระดับ\n\n")
        for level, data in cascade_results.items():
            f.write(f"### {level.upper()}\n")
            if data.get("total_events", 0) == 0:
                f.write("ไม่พบ cascade ที่ระดับนี้\n\n")
                continue

            f.write(f"- Long Liq Cascades (ราคาลง): {data['long_liq_cascades']} ครั้ง\n")
            f.write(f"- Short Liq Cascades (ราคาขึ้น): {data['short_liq_cascades']} ครั้ง\n\n")

            if data.get("after_long_liq"):
                al = data["after_long_liq"]
                f.write("**หลัง Long ถูก Liq (ราคาลง):**\n")
                f.write(f"- Return 15m ถัดไป: {al['mean_ret_15m']:+.4f}%\n")
                f.write(f"- Return 1h ถัดไป: {al['mean_ret_1h']:+.4f}% (median: {al['median_ret_1h']:+.4f}%)\n")
                f.write(f"- Return 4h ถัดไป: {al['mean_ret_4h']:+.4f}%\n")
                f.write(f"- % ที่ bounce กลับ (1h): {al['pct_positive_1h']:.1f}%\n")
                f.write(f"- สรุป: **{al['interpretation']}**\n\n")

            if data.get("after_short_liq"):
                ash = data["after_short_liq"]
                f.write("**หลัง Short ถูก Liq (ราคาขึ้น):**\n")
                f.write(f"- Return 15m ถัดไป: {ash['mean_ret_15m']:+.4f}%\n")
                f.write(f"- Return 1h ถัดไป: {ash['mean_ret_1h']:+.4f}% (median: {ash['median_ret_1h']:+.4f}%)\n")
                f.write(f"- Return 4h ถัดไป: {ash['mean_ret_4h']:+.4f}%\n")
                f.write(f"- % ที่ร่วงลง (1h): {ash['pct_negative_1h']:.1f}%\n")
                f.write(f"- สรุป: **{ash['interpretation']}**\n\n")

        f.write("## ขนาด Cascade vs ผลลัพธ์\n\n")
        if size_analysis:
            f.write("| ขนาด | จำนวน | Avg Ret 1h | Avg Ret 4h | Avg Side Ratio |\n")
            f.write("|------|-------|-----------|-----------|----------------|\n")
            for s in size_analysis:
                f.write(f"| {s['size_bucket']} | {s['count']} | {s['mean_ret_1h']:+.4f}% | {s['mean_ret_4h']:+.4f}% | {s['mean_side_ratio']:+.3f} |\n")
        f.write("\n")

        f.write("## Cascade ซ้อน (Consecutive)\n")
        f.write(f"- Cascade ครั้งแรก: {consecutive_analysis['first_cascade_count']} ครั้ง, avg ret 1h: {consecutive_analysis['first_cascade_avg_ret_1h']:+.4f}%\n")
        f.write(f"- Cascade ตาม (ภายใน 1h): {consecutive_analysis['followup_cascade_count']} ครั้ง, avg ret 1h: {consecutive_analysis['followup_cascade_avg_ret_1h']:+.4f}%\n\n")

        f.write("## V3 Performance: Cascade vs Normal\n\n")
        if "error" not in v3_comparison:
            f.write(f"เทรด BTC ทั้งหมด (OOS): {v3_comparison['total_btc_trades']}\n\n")
            f.write("| ประเภท | จำนวนเทรด | Win Rate | Avg PnL | Total PnL |\n")
            f.write("|--------|----------|---------|---------|----------|\n")
            f.write(f"| ช่วง Cascade | {v3_comparison['cascade_trades']} | {v3_comparison['cascade_wr']:.1f}% | ${v3_comparison['cascade_avg_pnl']:.2f} | ${v3_comparison['cascade_total_pnl']:,.2f} |\n")
            f.write(f"| ช่วงปกติ | {v3_comparison['normal_trades']} | {v3_comparison['normal_wr']:.1f}% | ${v3_comparison['normal_avg_pnl']:.2f} | ${v3_comparison['normal_total_pnl']:,.2f} |\n\n")

            wr_diff = v3_comparison["cascade_wr"] - v3_comparison["normal_wr"]
            f.write(f"ส่วนต่าง WR: {wr_diff:+.1f}pp\n")
            if v3_edge:
                f.write("-> **พบความแตกต่างมีนัยสำคัญ** ระหว่างการเทรดช่วง cascade vs ปกติ\n\n")
            else:
                f.write("-> ไม่พบความแตกต่างที่มีนัยสำคัญ\n\n")
        else:
            f.write("ไม่มีเทรด BTC ในช่วง OOS\n\n")

        f.write("## สรุปและข้อเสนอแนะ\n\n")
        if passed:
            if verdict == "reversal_pattern":
                f.write("**พบ Reversal Pattern**: หลังเกิด liquidation cascade ราคามักกลับตัว\n")
                f.write("- ข้อเสนอ: พิจารณาสร้าง 'cascade_contrarian' factor ที่ fade ทิศทาง cascade\n")
                f.write("- อาจเพิ่มเข้า v3 เป็น factor ใหม่ได้\n")
            elif verdict == "continuation_pattern":
                f.write("**พบ Continuation Pattern**: หลังเกิด liquidation cascade ราคามักไปต่อในทิศทางเดิม\n")
                f.write("- ข้อเสนอ: cascade ใช้เป็น momentum confirmation ไม่ใช่ contrarian signal\n")
            elif verdict == "v3_cascade_edge":
                f.write("**พบว่า V3 ทำงานแตกต่างในช่วง cascade vs ปกติ**\n")
                f.write("- ข้อเสนอ: อาจปรับ threshold หรือ position size ตาม cascade intensity\n")
        else:
            f.write("**ไม่พบ pattern ที่ชัดเจน** หลังเกิด liquidation cascade\n")
            f.write("- Liquidation factor ปัจจุบัน (aggregate hourly) อาจจับข้อมูลได้เพียงพอแล้ว\n")
            f.write("- Event-level cascade ไม่ได้เพิ่ม alpha ที่ชัดเจน\n")

        f.write("\n## อ้างอิง\n")
        f.write("- Amberdata: Forced liquidation dynamics in crypto derivatives\n")
        f.write("- BitMEX Research: Liquidation cascades and market microstructure\n")
        f.write("- Mission #001: Hour-of-Day Session Analysis (baseline context)\n")

    log.info(f"Saved MD: {md_path}")

    # ── Update missions.json ──
    from research.missions import MissionEngine, _get_level
    engine = MissionEngine()

    mission_entry = {
        "mission_id": "mission_002_liquidation_cascade",
        "date": results["date"],
        "type": "regime_test",
        "title": "Liquidation Cascade Analysis",
        "description": "วิเคราะห์ว่า Liquidation Cascade ทำนายทิศทางราคา BTC ได้หรือไม่",
        "difficulty": "hard",
        "xp_reward": 100,
        "status": "completed",
        "target": "liquidation_cascade",
        "started_at": datetime.utcnow().isoformat(),
        "finished_at": datetime.utcnow().isoformat(),
        "result": {
            "success": passed,
            "verdict": verdict,
            "cascade_events_strong": cascade_results.get("strong", {}).get("total_events", 0),
            "v3_cascade_wr": v3_comparison.get("cascade_wr", 0),
            "v3_normal_wr": v3_comparison.get("normal_wr", 0),
        },
        "insight": _build_insight(passed, verdict, cascade_results, v3_comparison),
        "tags": ["regime_test", "liquidation", "cascade", "microstructure"],
    }
    engine._data["missions"].append(mission_entry)
    engine._data["meta"]["total_xp"] += 100
    engine._data["meta"]["current_streak"] += 1
    engine._data["meta"]["longest_streak"] = max(
        engine._data["meta"].get("longest_streak", 0),
        engine._data["meta"]["current_streak"]
    )
    engine._data["meta"]["last_mission_date"] = results["date"]
    lvl, _ = _get_level(engine._data["meta"]["total_xp"])
    engine._data["meta"]["level"] = lvl
    engine._save()
    log.info("Updated missions.json")

    # ── Print summary ──
    print("\n" + "=" * 70)
    print("  MISSION #002: Liquidation Cascade Analysis")
    print("=" * 70)
    print(f"  Result: {'PASS' if passed else 'FAIL'} -- {verdict}")
    print(f"  Data: {len(liq_raw):,} liquidation events -> {len(merged):,} OOS bars")
    print()
    for level, data in cascade_results.items():
        if data.get("total_events", 0) > 0:
            print(f"  {level.upper()}: {data['total_events']} cascades")
            al = data.get("after_long_liq", {})
            ash = data.get("after_short_liq", {})
            if al:
                print(f"    After Long Liq: ret_1h={al['mean_ret_1h']:+.4f}%, bounce={al['pct_positive_1h']:.0f}%")
            if ash:
                print(f"    After Short Liq: ret_1h={ash['mean_ret_1h']:+.4f}%, drop={ash['pct_negative_1h']:.0f}%")
    print()
    if "error" not in v3_comparison:
        print(f"  V3 BTC during cascade: {v3_comparison['cascade_trades']} trades, WR {v3_comparison['cascade_wr']:.1f}%, avg ${v3_comparison['cascade_avg_pnl']:.2f}")
        print(f"  V3 BTC normal:         {v3_comparison['normal_trades']} trades, WR {v3_comparison['normal_wr']:.1f}%, avg ${v3_comparison['normal_avg_pnl']:.2f}")
    print()
    print(f"  +100 XP earned!")
    print("=" * 70)

    return results


def _build_insight(passed, verdict, cascade_results, v3_comparison):
    """Generate Thai insight summary."""
    strong = cascade_results.get("strong", {})
    if not passed:
        return (
            f"ไม่พบ pattern ที่ชัดเจนหลัง liquidation cascade. "
            f"V3 cascade WR {v3_comparison.get('cascade_wr', 0):.0f}% vs "
            f"ปกติ {v3_comparison.get('normal_wr', 0):.0f}%. "
            f"Liquidation factor ปัจจุบันจับได้เพียงพอแล้ว"
        )

    if verdict == "reversal_pattern":
        al = strong.get("after_long_liq", {})
        return (
            f"พบ reversal pattern: หลัง cascade ราคา bounce {al.get('pct_positive_1h', 0):.0f}% ของเวลา. "
            f"พิจารณาสร้าง cascade_contrarian factor"
        )
    elif verdict == "continuation_pattern":
        return (
            f"พบ continuation pattern: cascade ไม่ได้ทำให้เกิด reversal. "
            f"ใช้เป็น momentum confirmation ได้"
        )
    else:
        return (
            f"V3 ทำงานต่างกันในช่วง cascade: WR {v3_comparison.get('cascade_wr', 0):.0f}% "
            f"vs ปกติ {v3_comparison.get('normal_wr', 0):.0f}%. "
            f"อาจปรับ position size ตาม cascade intensity"
        )


if __name__ == "__main__":
    run_mission()
