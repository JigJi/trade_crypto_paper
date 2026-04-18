"""
Mission 036: Liquidation Asymmetry -- Long vs Short Liquidation Predictive Power
================================================================================
สมมติฐาน: Long liquidation events (SELL side) กับ Short liquidation events (BUY side)
อาจมีพลังทำนายราคาที่ต่างกัน ระบบปัจจุบันใช้ net = short - long (symmetric)
แต่ถ้าฝั่งใดฝั่งหนึ่งแม่นกว่า เราควร weight ต่างกัน

Tests:
1. Asymmetry in volume: long vs short liq notional distribution
2. Cascade frequency: which side cascades more often?
3. Predictive power: after each type of cascade, BTC moves how?
4. Impact on v3 trades: trades during long-cascade vs short-cascade WR
5. Size-bucketed analysis: small vs large liqs by side
"""

import sys, json, logging
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import backtest_15m_btc_led_alts as bt
from paper_trading.config import COMPOSITE_WEIGHTS, V3_EXTRA_WEIGHTS, COIN_CONFIGS
from signal_core import score_ob_combined, score_basis_contrarian, score_tick_liq
import psycopg2
from research.config import get_pg_dsn

BKK_UTC_OFFSET = timedelta(hours=7)

print("=" * 70)
print("Mission 036: Liquidation Asymmetry Analysis")
print("=" * 70)

# ---- Step 1: Load raw tick-level liquidation data ----
print("\n[1] Loading raw tick liquidation data...")
conn = psycopg2.connect(get_pg_dsn())
tick_liq = pd.read_sql(
    "SELECT event_time as ts, side, notional_usd::float as notional_usd "
    "FROM market_data.liquidation WHERE symbol='BTCUSDT' ORDER BY event_time",
    conn, parse_dates=["ts"])
conn.close()

tick_liq["ts"] = tick_liq["ts"].dt.tz_localize(None) - BKK_UTC_OFFSET
print(f"  Total BTC liq events: {len(tick_liq):,}")
print(f"  Date range: {tick_liq['ts'].min()} to {tick_liq['ts'].max()}")

long_liqs = tick_liq[tick_liq["side"] == "SELL"]  # SELL = long positions liquidated
short_liqs = tick_liq[tick_liq["side"] == "BUY"]   # BUY = short positions liquidated
print(f"  Long liqs (SELL): {len(long_liqs):,} ({len(long_liqs)/len(tick_liq)*100:.1f}%)")
print(f"  Short liqs (BUY): {len(short_liqs):,} ({len(short_liqs)/len(tick_liq)*100:.1f}%)")

# ---- Step 2: Asymmetry in notional distribution ----
print("\n[2] Notional distribution by side...")
for label, subset in [("Long (SELL)", long_liqs), ("Short (BUY)", short_liqs)]:
    vals = subset["notional_usd"]
    print(f"  {label}:")
    print(f"    mean=${vals.mean():,.0f}  median=${vals.median():,.0f}  "
          f"std=${vals.std():,.0f}  max=${vals.max():,.0f}")
    print(f"    p75=${vals.quantile(0.75):,.0f}  p90=${vals.quantile(0.90):,.0f}  "
          f"p99=${vals.quantile(0.99):,.0f}")

# ---- Step 3: Resample to 15min and analyze cascades by side ----
print("\n[3] Resampling to 15min bars...")
tick_liq["is_sell"] = (tick_liq["side"] == "SELL").astype(float)
tick_liq["is_buy"] = (tick_liq["side"] == "BUY").astype(float)
tick_liq["sell_notional"] = tick_liq["notional_usd"] * tick_liq["is_sell"]
tick_liq["buy_notional"] = tick_liq["notional_usd"] * tick_liq["is_buy"]

agg = tick_liq.set_index("ts").resample("15min").agg({
    "notional_usd": "sum",
    "is_sell": "sum",
    "is_buy": "sum",
    "sell_notional": "sum",
    "buy_notional": "sum",
}).fillna(0).reset_index()
agg.columns = ["ts", "total_notional", "long_liq_count", "short_liq_count",
               "long_liq_notional", "short_liq_notional"]

# Moving averages
for col in ["total_notional", "long_liq_notional", "short_liq_notional"]:
    agg[f"{col}_ma"] = agg[col].rolling(96, min_periods=16).mean()  # 96 bars = 24h

# Cascade detection per side (3x MA threshold)
agg["long_cascade"] = agg["long_liq_notional"] > (agg["long_liq_notional_ma"] * 3)
agg["short_cascade"] = agg["short_liq_notional"] > (agg["short_liq_notional_ma"] * 3)
agg["total_cascade"] = agg["total_notional"] > (agg["total_notional_ma"] * 3)

print(f"  15min bars: {len(agg):,}")
print(f"  Long cascades (3x MA): {agg['long_cascade'].sum()}")
print(f"  Short cascades (3x MA): {agg['short_cascade'].sum()}")
print(f"  Total cascades: {agg['total_cascade'].sum()}")
print(f"  Both cascades simultaneously: {(agg['long_cascade'] & agg['short_cascade']).sum()}")

# ---- Step 4: Load BTC OHLCV and compute forward returns ----
print("\n[4] Loading BTC OHLCV...")
btc_ohlcv = bt.fetch_binance_15m("BTCUSDT", years=3)
if "date_time" in btc_ohlcv.columns:
    btc_ohlcv = btc_ohlcv.rename(columns={"date_time": "ts"})

# Forward returns (1h, 4h, 24h)
btc_ohlcv["ret_1h"] = btc_ohlcv["close"].pct_change(4).shift(-4)   # 4 bars = 1h
btc_ohlcv["ret_4h"] = btc_ohlcv["close"].pct_change(16).shift(-16)  # 16 bars = 4h

# Merge cascade flags with BTC price
merged = pd.merge_asof(
    btc_ohlcv[["ts", "close", "ret_1h", "ret_4h"]].sort_values("ts"),
    agg[["ts", "long_cascade", "short_cascade", "total_cascade",
         "long_liq_notional", "short_liq_notional", "total_notional",
         "long_liq_count", "short_liq_count"]].sort_values("ts"),
    on="ts", direction="backward", tolerance=pd.Timedelta("15min"))

# ---- Step 5: Predictive power analysis ----
print("\n[5] Predictive power after cascades...")
print("\n  === Forward Returns After Cascade Events ===")

results = {}
for cascade_type, cascade_col in [("Long Cascade", "long_cascade"),
                                    ("Short Cascade", "short_cascade"),
                                    ("Total Cascade", "total_cascade"),
                                    ("No Cascade", None)]:
    if cascade_col:
        mask = merged[cascade_col] == True
    else:
        mask = (merged["total_cascade"] == False)

    subset = merged[mask].dropna(subset=["ret_1h", "ret_4h"])
    if len(subset) < 10:
        print(f"  {cascade_type}: insufficient data ({len(subset)} bars)")
        continue

    r1h = subset["ret_1h"]
    r4h = subset["ret_4h"]

    # Contrarian logic: after long cascade (longs liquidated), price should bounce (buy)
    # After short cascade (shorts liquidated), price should drop (sell)
    if cascade_type == "Long Cascade":
        # Contrarian = expect price UP after long liquidation
        win_1h = (r1h > 0).mean() * 100
        win_4h = (r4h > 0).mean() * 100
    elif cascade_type == "Short Cascade":
        # Contrarian = expect price DOWN after short liquidation
        win_1h = (r1h < 0).mean() * 100
        win_4h = (r4h < 0).mean() * 100
    else:
        win_1h = 0
        win_4h = 0

    results[cascade_type] = {
        "count": int(len(subset)),
        "ret_1h_mean": float(r1h.mean() * 100),
        "ret_1h_median": float(r1h.median() * 100),
        "ret_4h_mean": float(r4h.mean() * 100),
        "ret_4h_median": float(r4h.median() * 100),
        "contrarian_wr_1h": float(win_1h) if cascade_type in ("Long Cascade", "Short Cascade") else None,
        "contrarian_wr_4h": float(win_4h) if cascade_type in ("Long Cascade", "Short Cascade") else None,
    }

    print(f"\n  {cascade_type} (n={len(subset):,}):")
    print(f"    1h fwd: mean={r1h.mean()*100:+.4f}%  median={r1h.median()*100:+.4f}%")
    print(f"    4h fwd: mean={r4h.mean()*100:+.4f}%  median={r4h.median()*100:+.4f}%")
    if cascade_type in ("Long Cascade", "Short Cascade"):
        print(f"    Contrarian WR: 1h={win_1h:.1f}%  4h={win_4h:.1f}%")

# ---- Step 6: Size-bucketed analysis ----
print("\n[6] Size-bucketed liquidation analysis...")

# Bucket individual liq events by notional size
size_buckets = {
    "Small (<$10K)": (0, 10_000),
    "Medium ($10K-$100K)": (10_000, 100_000),
    "Large ($100K-$1M)": (100_000, 1_000_000),
    "Whale (>$1M)": (1_000_000, float("inf")),
}

size_results = {}
for bucket_name, (lo, hi) in size_buckets.items():
    for side_label, side_val in [("Long", "SELL"), ("Short", "BUY")]:
        mask = (tick_liq["notional_usd"] >= lo) & (tick_liq["notional_usd"] < hi) & (tick_liq["side"] == side_val)
        count = mask.sum()
        total = tick_liq.loc[mask, "notional_usd"].sum()
        key = f"{side_label}_{bucket_name}"
        size_results[key] = {"count": int(count), "total_usd": float(total)}
        print(f"  {side_label} {bucket_name}: {count:,} events, ${total:,.0f}")

# ---- Step 7: Exclusive cascade analysis ----
print("\n[7] Exclusive cascade analysis (one side only)...")
merged["long_cascade"] = merged["long_cascade"].fillna(False).astype(bool)
merged["short_cascade"] = merged["short_cascade"].fillna(False).astype(bool)
long_only_cascade = merged["long_cascade"] & ~merged["short_cascade"]
short_only_cascade = merged["short_cascade"] & ~merged["long_cascade"]
both_cascade = merged["long_cascade"] & merged["short_cascade"]

exclusive_results = {}
for label, mask in [("Long-Only Cascade", long_only_cascade),
                     ("Short-Only Cascade", short_only_cascade),
                     ("Both-Side Cascade", both_cascade)]:
    subset = merged[mask].dropna(subset=["ret_1h", "ret_4h"])
    if len(subset) < 5:
        print(f"  {label}: insufficient data ({len(subset)})")
        continue

    r1h = subset["ret_1h"]
    r4h = subset["ret_4h"]

    if "Long" in label:
        wr1 = (r1h > 0).mean() * 100
        wr4 = (r4h > 0).mean() * 100
        direction = "expect UP (contrarian)"
    elif "Short" in label:
        wr1 = (r1h < 0).mean() * 100
        wr4 = (r4h < 0).mean() * 100
        direction = "expect DOWN (contrarian)"
    else:
        wr1 = 0
        wr4 = 0
        direction = "mixed"

    exclusive_results[label] = {
        "count": int(len(subset)),
        "ret_1h_mean": float(r1h.mean() * 100),
        "ret_4h_mean": float(r4h.mean() * 100),
        "contrarian_wr_1h": float(wr1) if "Both" not in label else None,
        "contrarian_wr_4h": float(wr4) if "Both" not in label else None,
    }

    print(f"\n  {label} (n={len(subset)}) -- {direction}:")
    print(f"    1h fwd: mean={r1h.mean()*100:+.4f}%  median={r1h.median()*100:+.4f}%")
    print(f"    4h fwd: mean={r4h.mean()*100:+.4f}%  median={r4h.median()*100:+.4f}%")
    if "Both" not in label:
        print(f"    Contrarian WR: 1h={wr1:.1f}%  4h={wr4:.1f}%")

# ---- Step 8: Ratio analysis (long/short liq ratio as signal) ----
print("\n[8] Long/Short liquidation ratio as signal...")
agg["ls_liq_ratio"] = (agg["long_liq_notional"] + 1) / (agg["short_liq_notional"] + 1)
agg["ls_liq_ratio_ma"] = agg["ls_liq_ratio"].rolling(16).mean()

# Merge ratio with BTC returns
ratio_merged = pd.merge_asof(
    btc_ohlcv[["ts", "ret_1h", "ret_4h"]].sort_values("ts"),
    agg[["ts", "ls_liq_ratio_ma"]].sort_values("ts"),
    on="ts", direction="backward", tolerance=pd.Timedelta("15min"))

ratio_merged = ratio_merged.dropna(subset=["ls_liq_ratio_ma", "ret_4h"])

# Quintile analysis
ratio_merged["ratio_q"] = pd.qcut(ratio_merged["ls_liq_ratio_ma"], 5, labels=False, duplicates="drop")
print("\n  Long/Short Ratio Quintiles -> 4h Forward Return:")
ratio_results = {}
for q in sorted(ratio_merged["ratio_q"].unique()):
    subset = ratio_merged[ratio_merged["ratio_q"] == q]
    r = subset["ret_4h"]
    print(f"    Q{int(q)+1} (ratio={subset['ls_liq_ratio_ma'].median():.2f}): "
          f"n={len(subset):,}  mean_ret={r.mean()*100:+.4f}%  WR_short={(r < 0).mean()*100:.1f}%  "
          f"WR_long={(r > 0).mean()*100:.1f}%")
    ratio_results[f"Q{int(q)+1}"] = {
        "count": int(len(subset)),
        "median_ratio": float(subset["ls_liq_ratio_ma"].median()),
        "mean_ret_4h": float(r.mean() * 100),
        "wr_short": float((r < 0).mean() * 100),
        "wr_long": float((r > 0).mean() * 100),
    }

# ---- Step 9: Impact on v3 backtest trades ----
print("\n[9] Impact on v3 backtest trades...")
db_data = bt.load_btc_db_data()
btc_df = bt.build_btc_features(btc_ohlcv, db_data)

btc_score = bt.compute_btc_composite_score(btc_df, COMPOSITE_WEIGHTS)
btc_score = btc_score + score_ob_combined(btc_df, weight=V3_EXTRA_WEIGHTS["ob_combined"])
btc_score = btc_score + score_basis_contrarian(btc_df, weight=V3_EXTRA_WEIGHTS["basis_contrarian"])
btc_score = btc_score + score_tick_liq(btc_df, weight=V3_EXTRA_WEIGHTS["tick_liq"])
btc_score_ts = pd.Series(btc_score.values, index=btc_df["ts"].values, name="btc_score")

# Run backtest on BTC
oos_start, oos_end = "2025-01-01", "2026-03-31"
alt_df = bt.build_alt_technicals(btc_ohlcv)
oos_mask = (alt_df["ts"] >= oos_start) & (alt_df["ts"] <= oos_end)
cfg = COIN_CONFIGS.get("BTC", {})
signals, alt_merged = bt.generate_btc_led_signal(
    btc_score_ts, alt_df[oos_mask],
    threshold=cfg.get("threshold", 3.0),
    use_alt_pa_filter=cfg.get("use_alt_pa_filter", False))
trades = bt.run_backtest(alt_merged, signals,
                         sl_atr_mult=cfg.get("sl_atr_mult", 2.5),
                         tp_atr_mult=cfg.get("tp_atr_mult", 4.0),
                         cooldown_bars=cfg.get("cooldown_bars", 4))

if len(trades) > 0:
    # Tag each trade with cascade state at entry
    trades_ts = pd.merge_asof(
        trades[["entry_time", "dir", "pnl_net", "exit_reason"]].rename(columns={"entry_time": "ts"}).sort_values("ts"),
        agg[["ts", "long_cascade", "short_cascade", "long_liq_notional",
             "short_liq_notional", "ls_liq_ratio"]].sort_values("ts"),
        on="ts", direction="backward", tolerance=pd.Timedelta("15min"))

    trade_cascade_results = {}
    for cascade_label, mask in [("During Long Cascade", trades_ts["long_cascade"] == True),
                                  ("During Short Cascade", trades_ts["short_cascade"] == True),
                                  ("No Cascade", (trades_ts["long_cascade"] == False) & (trades_ts["short_cascade"] == False))]:
        subset = trades_ts[mask]
        if len(subset) < 5:
            continue
        wr = (subset["pnl_net"] > 0).mean() * 100
        avg_pnl = subset["pnl_net"].mean()
        total_pnl = subset["pnl_net"].sum()

        trade_cascade_results[cascade_label] = {
            "trades": int(len(subset)),
            "wr": float(wr),
            "avg_pnl": float(avg_pnl),
            "total_pnl": float(total_pnl),
        }

        # Short vs Long trades in each cascade state
        for d in ["S", "L"]:
            d_subset = subset[subset["dir"] == d]
            if len(d_subset) >= 3:
                d_wr = (d_subset["pnl_net"] > 0).mean() * 100
                trade_cascade_results[f"{cascade_label}_{d}"] = {
                    "trades": int(len(d_subset)),
                    "wr": float(d_wr),
                    "avg_pnl": float(d_subset["pnl_net"].mean()),
                }

        print(f"\n  {cascade_label} (n={len(subset)}):")
        print(f"    WR: {wr:.1f}%  avg_pnl: ${avg_pnl:.2f}  total: ${total_pnl:.2f}")
        for d, d_label in [("S", "SHORT"), ("L", "LONG")]:
            d_subset = subset[subset["dir"] == d]
            if len(d_subset) >= 3:
                print(f"    {d_label}: n={len(d_subset)} WR={( d_subset['pnl_net']>0).mean()*100:.1f}%"
                      f"  avg=${d_subset['pnl_net'].mean():.2f}")

    # ---- Step 10: Ratio-based trade filtering ----
    print("\n[10] L/S Ratio-based trade filtering...")
    try:
        trades_ts["ratio_q"] = pd.qcut(trades_ts["ls_liq_ratio"].fillna(1),
                                        3, labels=False, duplicates="drop")
        ratio_labels = {0: "low_ratio", 1: "mid_ratio", 2: "high_ratio"}
        trades_ts["ratio_q"] = trades_ts["ratio_q"].map(ratio_labels).fillna("mid_ratio")
    except Exception:
        trades_ts["ratio_q"] = "mid_ratio"
    filter_results = {}
    for rq in ["low_ratio", "mid_ratio", "high_ratio"]:
        subset = trades_ts[trades_ts["ratio_q"] == rq]
        if len(subset) < 5:
            continue
        wr = (subset["pnl_net"] > 0).mean() * 100
        total = subset["pnl_net"].sum()
        filter_results[rq] = {
            "trades": int(len(subset)),
            "wr": float(wr),
            "total_pnl": float(total),
        }
        print(f"  {rq}: n={len(subset)} WR={wr:.1f}% total=${total:.2f}")

        for d, dl in [("S", "SHORT"), ("L", "LONG")]:
            ds = subset[subset["dir"] == d]
            if len(ds) >= 3:
                print(f"    {dl}: n={len(ds)} WR={(ds['pnl_net']>0).mean()*100:.1f}%"
                      f" total=${ds['pnl_net'].sum():.2f}")

# ---- Step 11: Temporal pattern (hour of day) ----
print("\n[11] Liquidation asymmetry by hour of day...")
tick_liq["hour"] = tick_liq["ts"].dt.hour
hourly_asym = tick_liq.groupby(["hour", "side"]).agg(
    count=("notional_usd", "count"),
    total_notional=("notional_usd", "sum"),
    avg_notional=("notional_usd", "mean"),
).reset_index()

# Compute ratio per hour
hour_ratios = {}
for h in range(24):
    long_n = hourly_asym[(hourly_asym["hour"] == h) & (hourly_asym["side"] == "SELL")]["total_notional"].sum()
    short_n = hourly_asym[(hourly_asym["hour"] == h) & (hourly_asym["side"] == "BUY")]["total_notional"].sum()
    ratio = (long_n + 1) / (short_n + 1)
    hour_ratios[h] = {"long_notional": float(long_n), "short_notional": float(short_n), "ratio": float(ratio)}

# Print top asymmetric hours
sorted_hours = sorted(hour_ratios.items(), key=lambda x: x[1]["ratio"])
print("  Most Short-Dominated Hours (low ratio = more shorts liquidated):")
for h, v in sorted_hours[:3]:
    print(f"    Hour {h:02d} UTC: ratio={v['ratio']:.2f}  "
          f"long=${v['long_notional']/1e6:.1f}M  short=${v['short_notional']/1e6:.1f}M")
print("  Most Long-Dominated Hours (high ratio = more longs liquidated):")
for h, v in sorted_hours[-3:]:
    print(f"    Hour {h:02d} UTC: ratio={v['ratio']:.2f}  "
          f"long=${v['long_notional']/1e6:.1f}M  short=${v['short_notional']/1e6:.1f}M")

# ---- Compile Results ----
print("\n" + "=" * 70)
print("MISSION 036 COMPLETE")
print("=" * 70)

mission_result = {
    "data_summary": {
        "total_events": int(len(tick_liq)),
        "long_liqs": int(len(long_liqs)),
        "short_liqs": int(len(short_liqs)),
        "long_pct": float(len(long_liqs) / len(tick_liq) * 100),
        "date_range": f"{tick_liq['ts'].min()} to {tick_liq['ts'].max()}",
    },
    "cascade_counts": {
        "long_cascades": int(agg["long_cascade"].sum()),
        "short_cascades": int(agg["short_cascade"].sum()),
        "total_cascades": int(agg["total_cascade"].sum()),
        "both_simultaneous": int((agg["long_cascade"] & agg["short_cascade"]).sum()),
    },
    "forward_returns": results,
    "exclusive_cascades": exclusive_results,
    "ratio_quintiles": ratio_results,
    "trade_impact": trade_cascade_results if len(trades) > 0 else {},
    "trade_filter": filter_results if len(trades) > 0 else {},
    "size_buckets": size_results,
    "hourly_asymmetry": hour_ratios,
}

# Save JSON
out_json = BASE_DIR / "missions" / "mission_036_liq_asymmetry.json"
with open(out_json, "w", encoding="utf-8") as f:
    json.dump(mission_result, f, indent=2, ensure_ascii=False, default=str)
print(f"\nSaved: {out_json}")

# ---- Generate key insights ----
print("\n=== KEY INSIGHTS ===")

# Check if there's meaningful asymmetry
if "Long Cascade" in results and "Short Cascade" in results:
    lc = results["Long Cascade"]
    sc = results["Short Cascade"]
    print(f"\n1. Long Cascade contrarian WR: 1h={lc['contrarian_wr_1h']:.1f}%  4h={lc['contrarian_wr_4h']:.1f}%")
    print(f"   Short Cascade contrarian WR: 1h={sc['contrarian_wr_1h']:.1f}%  4h={sc['contrarian_wr_4h']:.1f}%")
    diff_4h = lc["contrarian_wr_4h"] - sc["contrarian_wr_4h"]
    if abs(diff_4h) > 3:
        winner = "Long Cascade" if diff_4h > 0 else "Short Cascade"
        print(f"   >>> ASYMMETRY DETECTED: {winner} contrarian signal is {abs(diff_4h):.1f}pp stronger at 4h")
    else:
        print(f"   >>> Difference small ({diff_4h:+.1f}pp) -- roughly symmetric")

if ratio_results:
    q1 = ratio_results.get("Q1", {})
    q5 = ratio_results.get("Q5", {})
    if q1 and q5:
        print(f"\n2. L/S Ratio Signal:")
        print(f"   Q1 (short-dominated): 4h ret={q1['mean_ret_4h']:+.4f}%")
        print(f"   Q5 (long-dominated):  4h ret={q5['mean_ret_4h']:+.4f}%")

if trade_cascade_results:
    print(f"\n3. V3 Trade Impact:")
    for k, v in trade_cascade_results.items():
        if "_" not in k or k.startswith("During") or k.startswith("No"):
            if "trades" in v:
                print(f"   {k}: n={v['trades']} WR={v['wr']:.1f}%")

print("\nDone!")
