"""
Liquidation Alt Factor Analysis
=================================
Test per-coin tick liquidation data as entry filter.
Aggregate to 15-min buckets, compute features, correlate with paper trades.
"""

import sqlite3
import psycopg2
import pandas as pd
import numpy as np
from pathlib import Path

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


def load_liq_alt():
    """Load tick-level liquidation data and aggregate to 15-min buckets per coin."""
    conn = psycopg2.connect(**PG_PARAMS)
    df = pd.read_sql_query("""
        SELECT symbol, event_time as ts, side, notional_usd
        FROM market_data.liquidation
        WHERE event_time >= '2026-03-15'
        ORDER BY event_time
    """, conn)
    conn.close()

    df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_localize(None)
    df["is_long_liq"] = (df["side"] == "SELL").astype(float)  # SELL = long liquidated
    df["is_short_liq"] = (df["side"] == "BUY").astype(float)  # BUY = short liquidated
    df["long_usd"] = df["notional_usd"] * df["is_long_liq"]
    df["short_usd"] = df["notional_usd"] * df["is_short_liq"]

    # Aggregate to 15-min buckets per symbol
    agg = df.set_index("ts").groupby("symbol").resample("15min").agg({
        "notional_usd": "sum",
        "long_usd": "sum",
        "short_usd": "sum",
        "is_long_liq": "sum",
        "is_short_liq": "sum",
    }).reset_index()

    agg.columns = ["symbol", "ts", "liq_total_usd", "liq_long_usd", "liq_short_usd",
                    "liq_long_count", "liq_short_count"]

    # Derived features
    agg["liq_net_usd"] = agg["liq_short_usd"] - agg["liq_long_usd"]  # positive = short squeezed
    agg["liq_total_count"] = agg["liq_long_count"] + agg["liq_short_count"]
    total = agg["liq_total_usd"].replace(0, np.nan)
    agg["liq_ratio"] = agg["liq_short_usd"] / total  # >0.5 = more shorts liquidated = bullish

    # Rolling features per symbol
    for sym in agg["symbol"].unique():
        mask = agg["symbol"] == sym
        for col in ["liq_total_usd", "liq_net_usd", "liq_ratio"]:
            series = agg.loc[mask, col]
            for w in [4, 16, 32]:  # 1h, 4h, 8h
                roll_mean = series.rolling(w, min_periods=w // 2).mean()
                roll_std = series.rolling(w, min_periods=w // 2).std()
                agg.loc[mask, f"{col}_z{w}"] = (series - roll_mean) / roll_std.replace(0, np.nan)
                agg.loc[mask, f"{col}_ma{w}"] = roll_mean

    return agg


def main():
    print("=" * 60)
    print("  LIQUIDATION ALT FACTOR ANALYSIS")
    print("=" * 60)

    trades = load_trades()
    print(f"Paper trades: {len(trades)}")

    liq = load_liq_alt()
    print(f"Liq 15-min buckets: {len(liq):,}")
    print(f"Symbols: {liq['symbol'].nunique()}")

    # Merge with trades
    factor_cols = [
        "liq_total_usd", "liq_net_usd", "liq_ratio",
        "liq_total_usd_z4", "liq_total_usd_z16", "liq_total_usd_z32",
        "liq_net_usd_z4", "liq_net_usd_z16", "liq_net_usd_z32",
        "liq_ratio_z4", "liq_ratio_z16", "liq_ratio_z32",
        "liq_total_usd_ma4", "liq_total_usd_ma16",
        "liq_net_usd_ma4", "liq_net_usd_ma16",
    ]

    results = []
    for sym in trades["symbol"].unique():
        t = trades[trades["symbol"] == sym].copy().sort_values("entry_time")
        l = liq[liq["symbol"] == sym].copy().sort_values("ts")
        if l.empty or t.empty:
            continue
        merged = pd.merge_asof(t, l, left_on="entry_time", right_on="ts",
                               by="symbol", direction="backward",
                               tolerance=pd.Timedelta(minutes=30))
        results.append(merged)

    if not results:
        print("No matches!")
        return

    merged = pd.concat(results, ignore_index=True)
    matched = merged.dropna(subset=["liq_total_usd"]).shape[0]
    print(f"Matched trades: {matched}/{len(trades)}")

    # Correlation analysis
    print(f"\n{'Factor':<25} {'N':>5} {'Corr PnL':>10} {'Corr WR':>10}")
    print("-" * 55)

    corr_results = []
    for col in factor_cols:
        valid = merged.dropna(subset=[col, "pnl_net"])
        if len(valid) < 50:
            continue
        corr_pnl = valid[col].corr(valid["pnl_net"])
        corr_win = valid[col].corr(valid["win"])
        corr_results.append((col, len(valid), corr_pnl, corr_win))

    corr_results.sort(key=lambda x: abs(x[2]), reverse=True)
    for col, n, cp, cw in corr_results:
        star = " ***" if abs(cp) > 0.05 else " *" if abs(cp) > 0.03 else ""
        print(f"{col:<25} {n:>5} {cp:>+10.4f} {cw:>+10.4f}{star}")

    # Direction split for top factors
    print(f"\n{'Factor':<25} {'Dir':>6} {'N':>5} {'Corr PnL':>10} {'WR':>7}")
    print("-" * 60)
    top_factors = [r[0] for r in corr_results[:6]]
    for col in top_factors:
        for d in ["LONG", "SHORT"]:
            valid = merged[(merged["dir_label"] == d)].dropna(subset=[col, "pnl_net"])
            if len(valid) < 30:
                continue
            cp = valid[col].corr(valid["pnl_net"])
            wr = valid["win"].mean()
            print(f"{col:<25} {d:>6} {len(valid):>5} {cp:>+10.4f} {wr:>6.1%}")

    # Per-coin standouts
    print(f"\nPer-coin standouts (|corr| > 0.15):")
    print(f"{'Coin':<12} {'Factor':<25} {'N':>5} {'Corr':>8} {'WR':>7}")
    print("-" * 60)
    for coin in merged["coin"].unique():
        cdf = merged[merged["coin"] == coin]
        if len(cdf) < 15:
            continue
        for col in ["liq_total_usd", "liq_net_usd", "liq_ratio",
                     "liq_total_usd_z16", "liq_net_usd_z16", "liq_ratio_z16"]:
            valid = cdf.dropna(subset=[col])
            if len(valid) < 15:
                continue
            cp = valid[col].corr(valid["pnl_net"])
            if abs(cp) > 0.15:
                print(f"{coin:<12} {col:<25} {len(valid):>5} {cp:>+8.3f} "
                      f"{valid['win'].mean():>6.1%}")

    # Filter test: block when liq spike (top factor)
    if corr_results:
        best_col = corr_results[0][0]
        print(f"\n{'='*60}")
        print(f"  FILTER TEST: {best_col}")
        print(f"{'='*60}")

        valid = merged.dropna(subset=[best_col, "pnl_net"])
        baseline_pnl = valid["pnl_net"].sum()
        baseline_wr = valid["win"].mean()

        for pct in [10, 20, 30]:
            hi = valid[best_col].quantile(1 - pct/100)
            lo = valid[best_col].quantile(pct/100)

            kept_hi = valid[valid[best_col] <= hi]
            kept_lo = valid[valid[best_col] >= lo]

            print(f"\n  Remove top {pct}%: kept {len(kept_hi)}, "
                  f"WR {kept_hi['win'].mean():.1%} (d{kept_hi['win'].mean()-baseline_wr:+.1%}), "
                  f"PnL ${kept_hi['pnl_net'].sum():+.2f} "
                  f"(d${kept_hi['pnl_net'].sum()-baseline_pnl:+.2f})")
            print(f"  Remove bot {pct}%: kept {len(kept_lo)}, "
                  f"WR {kept_lo['win'].mean():.1%} (d{kept_lo['win'].mean()-baseline_wr:+.1%}), "
                  f"PnL ${kept_lo['pnl_net'].sum():+.2f} "
                  f"(d${kept_lo['pnl_net'].sum()-baseline_pnl:+.2f})")

    print("\nDone!")


if __name__ == "__main__":
    main()
