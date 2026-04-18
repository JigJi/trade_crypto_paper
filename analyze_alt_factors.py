"""
Alt Data Factor Analysis — Paper Trades × Alt Data (No Backtest)
================================================================
Join actual paper trades with per-coin alt data collected over ~17 days.
Find which alt factors correlate with trade outcome (win/loss, PnL).

Ground truth = paper trades (real execution, real slippage).
"""

import sqlite3
import psycopg2
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime, timedelta
import json
import warnings
warnings.filterwarnings("ignore")

# ── Config ──────────────────────────────────────────────────────
SQLITE_PATH = Path(__file__).parent / "paper_trading" / "state" / "paper_trades.db"
PG_PARAMS = dict(host="localhost", port=5432, dbname="smart_trading",
                 user="postgres", password="P@ssw0rd")

# Alt data tables and their key numeric columns
ALT_TABLES = {
    "basis_alt":        {"cols": ["basis_rate", "last_funding_rate"], "freq": "5min"},
    "open_interest_alt": {"cols": ["oi_usdt"], "freq": "5min"},
    "order_book_alt":   {"cols": ["imbalance", "spread_bps"], "freq": "5min"},
    "funding_rate_alt": {"cols": ["funding_rate"], "freq": "1h"},
    "taker_ratio_alt":  {"cols": ["buy_sell_ratio"], "freq": "15min"},
    "ls_ratio_alt":     {"cols": ["long_short_ratio"], "freq": "15min"},
    "cvd_alt":          {"cols": ["volume_delta", "buy_vol", "sell_vol"], "freq": "15min",
                         "extra_derived": True},
    "mark_klines_alt":  {"cols": ["open", "high", "low", "close"], "freq": "15min",
                         "extra_derived": True},
}

OUTPUT_DIR = Path(__file__).parent / "experiments"
OUTPUT_DIR.mkdir(exist_ok=True)


def load_paper_trades():
    """Load all paper trades from SQLite."""
    conn = sqlite3.connect(str(SQLITE_PATH))
    df = pd.read_sql_query("""
        SELECT id, coin, direction, entry_time, exit_time,
               entry_price, exit_price, qty,
               pnl_net, fee_total, exit_reason,
               btc_score_entry, bars_held
        FROM trades
        ORDER BY entry_time
    """, conn)
    conn.close()
    df["entry_time"] = pd.to_datetime(df["entry_time"])
    df["exit_time"] = pd.to_datetime(df["exit_time"])
    df["symbol"] = df["coin"] + "USDT"
    df["win"] = (df["pnl_net"] > 0).astype(int)
    df["dir_label"] = df["direction"].map({1: "LONG", -1: "SHORT"})
    return df


def load_alt_data(table, cols):
    """Load alt data from PostgreSQL, return DataFrame."""
    col_str = ", ".join(cols)
    query = f"""
        SELECT symbol, ts, {col_str}
        FROM market_data.{table}
        ORDER BY symbol, ts
    """
    conn = psycopg2.connect(**PG_PARAMS)
    df = pd.read_sql_query(query, conn)
    conn.close()
    df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_localize(None)

    # Derived features for specific tables
    if table == "cvd_alt":
        total_vol = df["buy_vol"].astype(float) + df["sell_vol"].astype(float)
        df["buy_pressure"] = df["buy_vol"].astype(float) / total_vol.replace(0, np.nan)
        df["volume_delta"] = df["volume_delta"].astype(float)
        cols.append("buy_pressure")
    elif table == "mark_klines_alt":
        df["mark_range"] = (df["high"] - df["low"]) / df["close"].replace(0, np.nan)
        df["mark_ret"] = df.groupby("symbol")["close"].pct_change()
        cols.extend(["mark_range", "mark_ret"])

    return df


def get_alt_at_entry(trades_df, alt_df, cols, lookback_minutes=15):
    """For each trade, find the closest alt data row BEFORE entry_time.

    Uses merge_asof: for each trade, find latest alt row where ts <= entry_time.
    """
    results = []

    symbols = trades_df["symbol"].unique()
    for sym in symbols:
        t = trades_df[trades_df["symbol"] == sym].copy().sort_values("entry_time")
        a = alt_df[alt_df["symbol"] == sym].copy().sort_values("ts")

        if a.empty or t.empty:
            continue

        merged = pd.merge_asof(
            t, a,
            left_on="entry_time", right_on="ts",
            by="symbol",
            direction="backward",
            tolerance=pd.Timedelta(minutes=lookback_minutes * 2),
        )
        results.append(merged)

    if not results:
        return pd.DataFrame()
    return pd.concat(results, ignore_index=True)


def compute_rolling_features(alt_df, cols, windows=[12, 48, 96]):
    """Add rolling z-score and momentum features for each col.

    windows in number of rows (e.g., 12 rows @ 5min = 1h).
    """
    result = alt_df.copy()
    for sym in result["symbol"].unique():
        mask = result["symbol"] == sym
        for col in cols:
            series = result.loc[mask, col]
            for w in windows:
                roll_mean = series.rolling(w, min_periods=w // 2).mean()
                roll_std = series.rolling(w, min_periods=w // 2).std()
                zscore = (series - roll_mean) / roll_std.replace(0, np.nan)
                result.loc[mask, f"{col}_z{w}"] = zscore
                # Momentum: current vs w-periods ago
                result.loc[mask, f"{col}_mom{w}"] = series - series.shift(w)
    return result


def analyze_factor_correlation(merged_df, factor_cols, min_trades=30):
    """Compute correlation between factor values and trade outcomes."""
    results = []
    for col in factor_cols:
        valid = merged_df.dropna(subset=[col, "pnl_net"])
        if len(valid) < min_trades:
            continue

        corr_pnl = valid[col].corr(valid["pnl_net"])
        corr_win = valid[col].corr(valid["win"])

        # Quintile analysis
        try:
            valid["q"] = pd.qcut(valid[col], 5, labels=False, duplicates="drop")
            q_stats = valid.groupby("q").agg(
                trades=("pnl_net", "count"),
                wr=("win", "mean"),
                avg_pnl=("pnl_net", "mean"),
                total_pnl=("pnl_net", "sum"),
            ).to_dict("index")
        except Exception:
            q_stats = {}

        # Direction split
        for d_label in ["SHORT", "LONG"]:
            d_valid = valid[valid["dir_label"] == d_label]
            if len(d_valid) >= min_trades:
                results.append({
                    "factor": col,
                    "direction": d_label,
                    "n_trades": len(d_valid),
                    "corr_pnl": d_valid[col].corr(d_valid["pnl_net"]),
                    "corr_win": d_valid[col].corr(d_valid["win"]),
                    "avg_pnl": d_valid["pnl_net"].mean(),
                    "wr": d_valid["win"].mean(),
                })

        results.append({
            "factor": col,
            "direction": "ALL",
            "n_trades": len(valid),
            "corr_pnl": corr_pnl,
            "corr_win": corr_win,
            "avg_pnl": valid["pnl_net"].mean(),
            "wr": valid["win"].mean(),
            "quintiles": q_stats,
        })

    return pd.DataFrame(results)


def analyze_filter_potential(merged_df, factor_col, direction=None, pcts=[10, 20, 30]):
    """Test if filtering by extreme factor values improves PnL.

    For each percentile threshold, check:
    - Remove bottom X% trades  to does WR/PnL improve?
    - Remove top X% trades  to does WR/PnL improve?
    """
    df = merged_df.dropna(subset=[factor_col, "pnl_net"]).copy()
    if direction:
        df = df[df["dir_label"] == direction]

    if len(df) < 50:
        return None

    baseline_wr = df["win"].mean()
    baseline_pnl = df["pnl_net"].sum()
    baseline_avg = df["pnl_net"].mean()

    results = []
    for pct in pcts:
        lo = df[factor_col].quantile(pct / 100)
        hi = df[factor_col].quantile(1 - pct / 100)

        # Remove bottom pct%
        kept_hi = df[df[factor_col] >= lo]
        # Remove top pct%
        kept_lo = df[df[factor_col] <= hi]

        results.append({
            "factor": factor_col,
            "filter": f"remove_bottom_{pct}pct",
            "kept_trades": len(kept_hi),
            "wr": kept_hi["win"].mean(),
            "wr_delta": kept_hi["win"].mean() - baseline_wr,
            "total_pnl": kept_hi["pnl_net"].sum(),
            "pnl_delta": kept_hi["pnl_net"].sum() - baseline_pnl,
            "avg_pnl": kept_hi["pnl_net"].mean(),
            "avg_pnl_delta": kept_hi["pnl_net"].mean() - baseline_avg,
        })
        results.append({
            "factor": factor_col,
            "filter": f"remove_top_{pct}pct",
            "kept_trades": len(kept_lo),
            "wr": kept_lo["win"].mean(),
            "wr_delta": kept_lo["win"].mean() - baseline_wr,
            "total_pnl": kept_lo["pnl_net"].sum(),
            "pnl_delta": kept_lo["pnl_net"].sum() - baseline_pnl,
            "avg_pnl": kept_lo["pnl_net"].mean(),
            "avg_pnl_delta": kept_lo["pnl_net"].mean() - baseline_avg,
        })

    return pd.DataFrame(results)


def per_coin_analysis(merged_df, factor_cols, min_trades=15):
    """Check if factor effectiveness varies by coin."""
    results = []
    for coin in merged_df["coin"].unique():
        coin_df = merged_df[merged_df["coin"] == coin]
        if len(coin_df) < min_trades:
            continue
        for col in factor_cols:
            valid = coin_df.dropna(subset=[col])
            if len(valid) < min_trades:
                continue
            corr = valid[col].corr(valid["pnl_net"])
            results.append({
                "coin": coin,
                "factor": col,
                "n": len(valid),
                "corr_pnl": corr,
                "wr": valid["win"].mean(),
                "avg_pnl": valid["pnl_net"].mean(),
            })
    return pd.DataFrame(results)


def print_section(title):
    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"{'='*70}")


def main():
    print("=" * 70)
    print("  ALT DATA FACTOR ANALYSIS — Paper Trades × Alt Data")
    print("  Ground truth = real trades, NO backtest")
    print("=" * 70)

    # ── 1. Load paper trades ────────────────────────────────────
    print("\n[1/5] Loading paper trades...")
    trades = load_paper_trades()
    print(f"  Total trades: {len(trades)}")
    print(f"  Date range: {trades['entry_time'].min()} to {trades['entry_time'].max()}")
    print(f"  Overall WR: {trades['win'].mean():.1%}")
    print(f"  Total PnL: ${trades['pnl_net'].sum():.2f}")
    print(f"  Avg PnL: ${trades['pnl_net'].mean():.2f}")

    # ── 2. Load & enrich alt data ───────────────────────────────
    print("\n[2/5] Loading alt data from PostgreSQL...")
    all_corr_results = []
    all_filter_results = []
    all_percoin_results = []
    full_json = {}

    for table, info in ALT_TABLES.items():
        cols = info["cols"]
        print(f"\n  Processing {table} ({', '.join(cols)})...")

        try:
            alt_df = load_alt_data(table, cols)
        except Exception as e:
            print(f"    ERROR loading {table}: {e}")
            continue

        print(f"    Rows: {len(alt_df):,}")

        if alt_df.empty:
            print(f"    SKIP - no data")
            continue

        # Rolling features
        if info["freq"] == "5min":
            windows = [12, 48, 96]     # 1h, 4h, 8h
        elif info["freq"] == "15min":
            windows = [4, 16, 32]      # 1h, 4h, 8h
        else:
            windows = [4, 8, 24]       # 4h, 8h, 24h

        alt_enriched = compute_rolling_features(alt_df, cols, windows)
        all_cols = cols.copy()
        for c in cols:
            for w in windows:
                all_cols.extend([f"{c}_z{w}", f"{c}_mom{w}"])

        # ── 3. Join with trades ─────────────────────────────────
        lookback = 30 if info["freq"] == "1h" else 15
        merged = get_alt_at_entry(trades, alt_enriched, all_cols, lookback_minutes=lookback)

        if merged.empty:
            print(f"    SKIP - no matches after join")
            continue

        matched = merged.dropna(subset=cols).shape[0]
        print(f"    Matched trades: {matched}/{len(trades)}")

        # ── 4. Correlation analysis ─────────────────────────────
        corr_df = analyze_factor_correlation(merged, all_cols, min_trades=30)
        if not corr_df.empty:
            all_corr_results.append(corr_df)

        # ── 5. Filter potential ─────────────────────────────────
        for col in all_cols:
            filt = analyze_filter_potential(merged, col)
            if filt is not None:
                all_filter_results.append(filt)

        # Per-coin
        pc = per_coin_analysis(merged, cols)
        if not pc.empty:
            all_percoin_results.append(pc)

    # ── Print Results ───────────────────────────────────────────
    print_section("CORRELATION RESULTS (sorted by |corr_pnl|)")

    if all_corr_results:
        corr_all = pd.concat(all_corr_results, ignore_index=True)
        corr_all_dir = corr_all[corr_all["direction"] == "ALL"].copy()
        corr_all_dir["abs_corr"] = corr_all_dir["corr_pnl"].abs()
        corr_all_dir = corr_all_dir.sort_values("abs_corr", ascending=False)

        print(f"\n{'Factor':<35} {'N':>5} {'Corr PnL':>10} {'Corr WR':>10} {'WR':>7}")
        print("-" * 70)
        for _, r in corr_all_dir.head(30).iterrows():
            star = " ***" if abs(r["corr_pnl"]) > 0.05 else " *" if abs(r["corr_pnl"]) > 0.03 else ""
            print(f"{r['factor']:<35} {r['n_trades']:>5} {r['corr_pnl']:>+10.4f} "
                  f"{r['corr_win']:>+10.4f} {r['wr']:>6.1%}{star}")

        # Direction split for top factors
        print_section("TOP FACTORS BY DIRECTION")
        top_factors = corr_all_dir.head(10)["factor"].tolist()
        dir_results = corr_all[
            (corr_all["factor"].isin(top_factors)) &
            (corr_all["direction"] != "ALL")
        ].sort_values(["factor", "direction"])

        print(f"\n{'Factor':<35} {'Dir':>6} {'N':>5} {'Corr PnL':>10} {'WR':>7}")
        print("-" * 70)
        for _, r in dir_results.iterrows():
            print(f"{r['factor']:<35} {r['direction']:>6} {r['n_trades']:>5} "
                  f"{r['corr_pnl']:>+10.4f} {r['wr']:>6.1%}")
    else:
        print("  No correlation results generated.")

    # Filter potential
    print_section("BEST FILTERS (largest PnL improvement)")

    if all_filter_results:
        filt_all = pd.concat(all_filter_results, ignore_index=True)
        filt_best = filt_all.sort_values("avg_pnl_delta", ascending=False).head(20)

        print(f"\n{'Factor':<35} {'Filter':<25} {'Kept':>5} {'WR':>7} "
              f"{'dWR':>7} {'dAvg PnL':>10} {'dPNL':>10}")
        print("-" * 105)
        for _, r in filt_best.iterrows():
            print(f"{r['factor']:<35} {r['filter']:<25} {r['kept_trades']:>5} "
                  f"{r['wr']:>6.1%} {r['wr_delta']:>+6.1%} "
                  f"${r['avg_pnl_delta']:>+9.2f} ${r['pnl_delta']:>+9.2f}")
    else:
        print("  No filter results generated.")

    # Per-coin standouts
    print_section("PER-COIN FACTOR STANDOUTS (|corr| > 0.15)")

    if all_percoin_results:
        pc_all = pd.concat(all_percoin_results, ignore_index=True)
        pc_standout = pc_all[pc_all["corr_pnl"].abs() > 0.15].sort_values(
            "corr_pnl", key=abs, ascending=False)

        if not pc_standout.empty:
            print(f"\n{'Coin':<12} {'Factor':<25} {'N':>5} {'Corr':>8} {'WR':>7} {'Avg PnL':>10}")
            print("-" * 70)
            for _, r in pc_standout.head(30).iterrows():
                print(f"{r['coin']:<12} {r['factor']:<25} {r['n']:>5} "
                      f"{r['corr_pnl']:>+8.3f} {r['wr']:>6.1%} ${r['avg_pnl']:>+9.2f}")
        else:
            print("  No standout per-coin correlations found (all |corr| < 0.15)")
    else:
        print("  No per-coin results generated.")

    # ── Save full results to JSON ───────────────────────────────
    output = {
        "generated_at": datetime.utcnow().isoformat(),
        "trades_analyzed": len(trades),
        "date_range": f"{trades['entry_time'].min()}  to {trades['entry_time'].max()}",
        "overall_wr": float(trades["win"].mean()),
        "overall_pnl": float(trades["pnl_net"].sum()),
    }

    if all_corr_results:
        corr_all = pd.concat(all_corr_results, ignore_index=True)
        # Remove quintiles dict for JSON serialization
        corr_export = corr_all.drop(columns=["quintiles"], errors="ignore")
        output["correlations"] = corr_export.to_dict("records")

    if all_filter_results:
        filt_all = pd.concat(all_filter_results, ignore_index=True)
        output["filters"] = filt_all.sort_values("avg_pnl_delta", ascending=False).head(50).to_dict("records")

    if all_percoin_results:
        pc_all = pd.concat(all_percoin_results, ignore_index=True)
        output["per_coin"] = pc_all.to_dict("records")

    out_path = OUTPUT_DIR / "alt_factor_analysis.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2, default=str)

    print(f"\n\nFull results saved to: {out_path}")
    print("Done!")


if __name__ == "__main__":
    main()
