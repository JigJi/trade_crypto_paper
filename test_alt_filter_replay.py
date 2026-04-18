"""
Alt Filter Replay Test
========================
Replay all paper trades through the alt filter and show:
1. How many trades would have been BLOCKED
2. PnL of blocked trades (ideally negative = filter catches losers)
3. PnL of kept trades (should be better than baseline)
4. Per-exit-reason breakdown (does it catch SIGNAL_FLIP?)

Uses actual historical alt data at actual trade entry times.
"""

import sqlite3
import psycopg2
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime

SQLITE_PATH = Path(__file__).parent / "paper_trading" / "state" / "paper_trades.db"
PG_PARAMS = dict(host="localhost", port=5432, dbname="smart_trading",
                 user="postgres", password="P@ssw0rd")


def load_trades():
    conn = sqlite3.connect(str(SQLITE_PATH))
    df = pd.read_sql_query("""
        SELECT id, coin, direction, entry_time, exit_time,
               entry_price, exit_price, pnl_net, exit_reason,
               btc_score_entry, bars_held
        FROM trades ORDER BY entry_time
    """, conn)
    conn.close()
    df["entry_time"] = pd.to_datetime(df["entry_time"])
    df["symbol"] = df["coin"] + "USDT"
    df["win"] = (df["pnl_net"] > 0).astype(int)
    df["dir_label"] = df["direction"].map({1: "LONG", -1: "SHORT"})
    return df


def load_fr_alt():
    """Load all funding_rate_alt data."""
    conn = psycopg2.connect(**PG_PARAMS)
    df = pd.read_sql_query("""
        SELECT symbol, ts, funding_rate
        FROM market_data.funding_rate_alt
        ORDER BY symbol, ts
    """, conn)
    conn.close()
    df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_localize(None)
    return df


def load_cvd_alt():
    """Load all cvd_alt data."""
    conn = psycopg2.connect(**PG_PARAMS)
    df = pd.read_sql_query("""
        SELECT symbol, ts, buy_vol, sell_vol, volume_delta
        FROM market_data.cvd_alt
        ORDER BY symbol, ts
    """, conn)
    conn.close()
    df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_localize(None)
    df["buy_vol"] = df["buy_vol"].astype(float)
    df["sell_vol"] = df["sell_vol"].astype(float)
    return df


def compute_zscore_at_time(data_series, timestamps, target_time, lookback, max_staleness_hours=2):
    """Get z-score of the value at target_time using lookback window."""
    # Find data up to target_time
    mask = timestamps <= target_time
    if mask.sum() < lookback:
        return None

    recent = data_series[mask].iloc[-lookback:]
    latest_ts = timestamps[mask].iloc[-1]

    # Check staleness
    if (target_time - latest_ts).total_seconds() > max_staleness_hours * 3600:
        return None

    mean = recent.mean()
    std = recent.std()
    if std < 1e-12:
        return 0.0
    return (recent.iloc[-1] - mean) / std


def compute_momentum_at_time(data_series, timestamps, target_time, lookback, max_staleness_hours=2):
    """Get momentum at target_time."""
    mask = timestamps <= target_time
    if mask.sum() < lookback + 1:
        return None

    latest_ts = timestamps[mask].iloc[-1]
    if (target_time - latest_ts).total_seconds() > max_staleness_hours * 3600:
        return None

    vals = data_series[mask]
    return float(vals.iloc[-1] - vals.iloc[-(lookback + 1)])


def test_filter_thresholds(trades, fr_data, cvd_data, fr_z_thr, fr_mom_thr, cvd_z_thr):
    """Test a specific set of filter thresholds.

    Returns dict with stats.
    """
    blocked_mask = pd.Series(False, index=trades.index)
    block_reasons = []
    fr_z_values = []
    fr_mom_values = []
    cvd_z_values = []

    for idx, trade in trades.iterrows():
        sym = trade["symbol"]
        entry_t = trade["entry_time"]
        direction = trade["direction"]
        reasons = []

        # FR filter
        fr_sym = fr_data[fr_data["symbol"] == sym]
        if len(fr_sym) >= 4:
            fr_z = compute_zscore_at_time(
                fr_sym["funding_rate"], fr_sym["ts"], entry_t, lookback=8, max_staleness_hours=4)
            fr_mom = compute_momentum_at_time(
                fr_sym["funding_rate"], fr_sym["ts"], entry_t, lookback=4, max_staleness_hours=4)

            if fr_z is not None:
                fr_z_values.append(fr_z)
            if fr_mom is not None:
                fr_mom_values.append(fr_mom)

            if fr_z is not None and fr_z > fr_z_thr:
                reasons.append("FR_Z8")
            if fr_mom is not None and fr_mom > fr_mom_thr:
                reasons.append("FR_MOM4")

        # CVD filter
        cvd_sym = cvd_data[cvd_data["symbol"] == sym]
        if len(cvd_sym) >= 4:
            bv_z = compute_zscore_at_time(
                cvd_sym["buy_vol"], cvd_sym["ts"], entry_t, lookback=4, max_staleness_hours=1)

            if bv_z is not None:
                cvd_z_values.append(bv_z)

            if direction == 1 and bv_z is not None and bv_z < cvd_z_thr:
                reasons.append("CVD_BUY_LOW")

            # Also check sell vol for SHORT
            if direction == -1:
                sv_z = compute_zscore_at_time(
                    cvd_sym["sell_vol"], cvd_sym["ts"], entry_t, lookback=4, max_staleness_hours=1)
                if sv_z is not None and sv_z < cvd_z_thr:
                    reasons.append("CVD_SELL_LOW")

        if reasons:
            blocked_mask.iloc[idx] = True
            block_reasons.append(", ".join(reasons))

    kept = trades[~blocked_mask]
    blocked = trades[blocked_mask]

    return {
        "total": len(trades),
        "blocked": len(blocked),
        "blocked_pct": len(blocked) / len(trades) * 100 if len(trades) > 0 else 0,
        "kept": len(kept),
        # Baseline
        "baseline_pnl": trades["pnl_net"].sum(),
        "baseline_wr": trades["win"].mean() * 100,
        "baseline_avg": trades["pnl_net"].mean(),
        # Kept
        "kept_pnl": kept["pnl_net"].sum(),
        "kept_wr": kept["win"].mean() * 100 if len(kept) > 0 else 0,
        "kept_avg": kept["pnl_net"].mean() if len(kept) > 0 else 0,
        # Blocked
        "blocked_pnl": blocked["pnl_net"].sum(),
        "blocked_wr": blocked["win"].mean() * 100 if len(blocked) > 0 else 0,
        "blocked_avg": blocked["pnl_net"].mean() if len(blocked) > 0 else 0,
        # PnL improvement
        "pnl_delta": kept["pnl_net"].sum() - trades["pnl_net"].sum(),
        # By exit reason
        "blocked_by_exit": blocked.groupby("exit_reason").agg(
            n=("pnl_net", "count"),
            pnl=("pnl_net", "sum"),
            avg=("pnl_net", "mean"),
        ).to_dict("index") if len(blocked) > 0 else {},
        "blocked_by_dir": blocked.groupby("dir_label").agg(
            n=("pnl_net", "count"),
            pnl=("pnl_net", "sum"),
            wr=("win", "mean"),
        ).to_dict("index") if len(blocked) > 0 else {},
    }


def print_result(label, r):
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    print(f"  Baseline:  {r['total']} trades, WR {r['baseline_wr']:.1f}%, "
          f"PnL ${r['baseline_pnl']:+.2f}, avg ${r['baseline_avg']:+.2f}")
    print(f"  Blocked:   {r['blocked']} trades ({r['blocked_pct']:.1f}%), "
          f"WR {r['blocked_wr']:.1f}%, PnL ${r['blocked_pnl']:+.2f}, "
          f"avg ${r['blocked_avg']:+.2f}")
    print(f"  Kept:      {r['kept']} trades, WR {r['kept_wr']:.1f}%, "
          f"PnL ${r['kept_pnl']:+.2f}, avg ${r['kept_avg']:+.2f}")
    print(f"  PnL delta: ${r['pnl_delta']:+.2f} "
          f"({'BETTER' if r['pnl_delta'] > 0 else 'WORSE'})")

    if r["blocked_by_exit"]:
        print(f"\n  Blocked by exit reason:")
        for reason, stats in sorted(r["blocked_by_exit"].items(), key=lambda x: x[1]["pnl"]):
            print(f"    {reason:<15} {stats['n']:>4} trades, "
                  f"PnL ${stats['pnl']:+.2f}, avg ${stats['avg']:+.2f}")

    if r["blocked_by_dir"]:
        print(f"\n  Blocked by direction:")
        for d, stats in r["blocked_by_dir"].items():
            print(f"    {d:<8} {stats['n']:>4} trades, "
                  f"PnL ${stats['pnl']:+.2f}, WR {stats['wr']*100:.1f}%")


def main():
    print("=" * 60)
    print("  ALT FILTER REPLAY TEST")
    print("  Replay paper trades through FR + CVD filter")
    print("=" * 60)

    print("\nLoading data...")
    trades = load_trades()
    print(f"  Paper trades: {len(trades)}")

    fr_data = load_fr_alt()
    print(f"  FR alt rows: {len(fr_data)}")

    cvd_data = load_cvd_alt()
    print(f"  CVD alt rows: {len(cvd_data)}")

    # Count matchable trades
    fr_symbols = set(fr_data["symbol"].unique())
    cvd_symbols = set(cvd_data["symbol"].unique())
    fr_matchable = trades[trades["symbol"].isin(fr_symbols)]
    cvd_matchable = trades[trades["symbol"].isin(cvd_symbols)]
    print(f"  Trades with FR data: {len(fr_matchable)}/{len(trades)}")
    print(f"  Trades with CVD data: {len(cvd_matchable)}/{len(trades)}")

    # ── Test multiple threshold combinations ────────────────────
    print("\n" + "=" * 60)
    print("  THRESHOLD GRID SEARCH")
    print("=" * 60)

    configs = [
        # (label, fr_z_thr, fr_mom_thr, cvd_z_thr)
        ("Conservative (z>1.5, mom>0.0005, cvd<-1.5)", 1.5, 0.0005, -1.5),
        ("Moderate     (z>1.0, mom>0.0003, cvd<-1.0)", 1.0, 0.0003, -1.0),
        ("Aggressive   (z>0.8, mom>0.0002, cvd<-0.8)", 0.8, 0.0002, -0.8),
        ("FR-only Moderate (z>1.0, mom>0.0003, no CVD)", 1.0, 0.0003, -99),
        ("CVD-only Moderate (no FR, cvd<-1.0)",          99, 99, -1.0),
    ]

    results = []
    for label, fr_z, fr_mom, cvd_z in configs:
        r = test_filter_thresholds(trades, fr_data, cvd_data, fr_z, fr_mom, cvd_z)
        r["label"] = label
        results.append(r)
        print_result(label, r)

    # ── Summary comparison table ────────────────────────────────
    print("\n" + "=" * 60)
    print("  SUMMARY COMPARISON")
    print("=" * 60)
    print(f"\n{'Config':<50} {'Block%':>6} {'dWR':>6} {'dAvgPnL':>10} {'dTotalPnL':>10}")
    print("-" * 85)
    for r in results:
        wr_delta = r["kept_wr"] - r["baseline_wr"]
        print(f"{r['label']:<50} {r['blocked_pct']:>5.1f}% {wr_delta:>+5.1f}% "
              f"${r['kept_avg'] - r['baseline_avg']:>+9.2f} "
              f"${r['pnl_delta']:>+9.2f}")

    # ── Per-coin impact for best config ─────────────────────────
    best = min(results, key=lambda x: -x["pnl_delta"])
    print(f"\n\nBest config: {best['label']}")
    print(f"Would have saved: ${-best['blocked_pnl']:+.2f} by blocking "
          f"{best['blocked']} bad trades")

    print("\nDone!")


if __name__ == "__main__":
    main()
