"""
Mission #006: Cross-Coin Entry Synchronization & Concentration Risk
===================================================================
Hypothesis: Since all coins use the same BTC composite signal, entries are
highly synchronized. When a signal fires, multiple coins enter simultaneously.
If that signal is wrong, ALL positions lose at once -- creating hidden
concentration risk that the per-coin backtest doesn't capture.

Experiments:
1. Entry clustering: how many coins enter within the same 15m bar?
2. Cluster outcome correlation: do simultaneous entries win/lose together?
3. Mass loss events: when 3+ coins lose in the same cluster, what's the damage?
4. Cap entries per bar: limit N entries per signal, pick best
5. Effective diversification: actual portfolio Sharpe vs sum-of-coins Sharpe
6. Paper trading cluster analysis

Author: Research Agent | Date: 2026-03-17
"""

import sys, logging, json, warnings
from pathlib import Path
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import backtest_15m_btc_led_alts as bt
from paper_trading.config import COMPOSITE_WEIGHTS, V3_EXTRA_WEIGHTS, COIN_CONFIGS
from paper_trading.strategy import score_ob_combined, score_basis_contrarian, score_tick_liq

# ── Config ──
COINS = ["BTCUSDT", "XRPUSDT", "ADAUSDT", "DOTUSDT", "SUIUSDT", "FILUSDT"]
OOS_START = "2026-01-01"
OOS_END = "2026-03-31"


def build_btc_score():
    """Build BTC composite score (v3, identical to paper trading)."""
    log.info("Building BTC composite score...")
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


def run_all_coin_backtests(btc_score_ts):
    """Run backtest for each coin, return list of trade DataFrames with entry/exit times."""
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
            sl_atr_mult=cfg.get("sl_atr_mult", 3.0),
            tp_atr_mult=cfg.get("tp_atr_mult", 5.0),
            cooldown_bars=cfg.get("cooldown_bars", 4))

        if len(trades) > 0:
            trades["coin"] = coin
            trades["symbol"] = symbol
            all_trades.append(trades)
            log.info(f"  {coin}: {len(trades)} trades, WR={((trades['pnl_net']>0).mean()*100):.1f}%, PnL=${trades['pnl_net'].sum():.0f}")

    trades_df = pd.concat(all_trades, ignore_index=True)
    log.info(f"Total: {len(trades_df)} trades, PnL=${trades_df['pnl_net'].sum():.0f}")
    return trades_df


def exp1_entry_clustering(trades_df):
    """EXP 1: Measure how many coins enter within the same 15m bar."""
    log.info("\n=== EXP 1: Entry Clustering ===")

    # Round entry_time to 15m bins to identify same-bar entries
    trades_df["entry_bar"] = pd.to_datetime(trades_df["entry_time"]).dt.floor("15min")

    # Count coins per entry bar
    cluster_counts = trades_df.groupby("entry_bar").size()
    cluster_dist = cluster_counts.value_counts().sort_index()

    log.info("Entry cluster size distribution:")
    total_clusters = len(cluster_counts)
    for size, count in cluster_dist.items():
        pct = count / total_clusters * 100
        log.info(f"  {size} coins in bar: {count} events ({pct:.1f}%)")

    # Analyze PnL by cluster size
    trades_df["cluster_size"] = trades_df["entry_bar"].map(cluster_counts)
    cluster_pnl = trades_df.groupby("cluster_size").agg(
        trades=("pnl_net", "count"),
        wr=("pnl_net", lambda x: (x > 0).mean() * 100),
        total_pnl=("pnl_net", "sum"),
        avg_pnl=("pnl_net", "mean"),
    ).round(2)
    log.info("\nPnL by cluster size:")
    log.info(cluster_pnl.to_string())

    # Key metrics
    single_entry = trades_df[trades_df["cluster_size"] == 1]
    multi_entry = trades_df[trades_df["cluster_size"] > 1]
    max_cluster = cluster_counts.max()
    avg_cluster = cluster_counts.mean()
    multi_pct = (cluster_counts > 1).sum() / total_clusters * 100

    log.info(f"\nMax cluster size: {max_cluster}")
    log.info(f"Avg cluster size: {avg_cluster:.2f}")
    log.info(f"Multi-coin entries: {multi_pct:.1f}% of all entry bars")
    if len(single_entry) > 0 and len(multi_entry) > 0:
        log.info(f"Single-entry WR: {(single_entry['pnl_net']>0).mean()*100:.1f}%")
        log.info(f"Multi-entry WR:  {(multi_entry['pnl_net']>0).mean()*100:.1f}%")

    return {
        "cluster_dist": {int(k): int(v) for k, v in cluster_dist.items()},
        "max_cluster": int(max_cluster),
        "avg_cluster": round(float(avg_cluster), 2),
        "multi_entry_pct": round(float(multi_pct), 1),
        "single_wr": round(float((single_entry['pnl_net'] > 0).mean() * 100), 1) if len(single_entry) > 0 else None,
        "multi_wr": round(float((multi_entry['pnl_net'] > 0).mean() * 100), 1) if len(multi_entry) > 0 else None,
        "cluster_pnl": {int(k): {"trades": int(v["trades"]), "wr": v["wr"], "pnl": round(v["total_pnl"], 0)}
                        for k, v in cluster_pnl.iterrows()},
    }


def exp2_outcome_correlation(trades_df):
    """EXP 2: Do coins in the same cluster win/lose together?"""
    log.info("\n=== EXP 2: Outcome Correlation ===")

    multi_bars = trades_df[trades_df["cluster_size"] > 1]["entry_bar"].unique()
    all_wins = 0
    all_losses = 0
    mixed = 0
    total_clusters = 0

    cluster_outcomes = []
    for bar in multi_bars:
        cluster = trades_df[trades_df["entry_bar"] == bar]
        wins = (cluster["pnl_net"] > 0).sum()
        losses = (cluster["pnl_net"] <= 0).sum()
        n = len(cluster)
        total_clusters += 1

        # All same outcome?
        if wins == n:
            all_wins += 1
        elif losses == n:
            all_losses += 1
        else:
            mixed += 1

        # Win rate within cluster
        cluster_wr = wins / n
        cluster_pnl = cluster["pnl_net"].sum()
        cluster_outcomes.append({
            "bar": str(bar),
            "size": n,
            "wins": wins,
            "losses": losses,
            "wr": cluster_wr,
            "total_pnl": cluster_pnl,
        })

    if total_clusters == 0:
        log.info("No multi-coin clusters found")
        return {}

    log.info(f"Multi-coin clusters: {total_clusters}")
    log.info(f"  All win:  {all_wins} ({all_wins/total_clusters*100:.1f}%)")
    log.info(f"  All loss: {all_losses} ({all_losses/total_clusters*100:.1f}%)")
    log.info(f"  Mixed:    {mixed} ({mixed/total_clusters*100:.1f}%)")

    # Cross-coin PnL correlation
    # Pivot: rows=entry_bar, cols=coin, values=pnl_net
    multi_trades = trades_df[trades_df["cluster_size"] > 1].copy()
    pivot = multi_trades.pivot_table(index="entry_bar", columns="coin", values="pnl_net")
    if pivot.shape[1] >= 2:
        corr_matrix = pivot.corr()
        # Average off-diagonal correlation
        mask = np.triu(np.ones(corr_matrix.shape, dtype=bool), k=1)
        avg_corr = corr_matrix.values[mask].mean()
        log.info(f"\nAvg cross-coin PnL correlation (simultaneous entries): {avg_corr:.3f}")
        log.info("Correlation matrix:")
        log.info(corr_matrix.round(3).to_string())
    else:
        avg_corr = None

    return {
        "total_multi_clusters": total_clusters,
        "all_win_pct": round(all_wins / total_clusters * 100, 1),
        "all_loss_pct": round(all_losses / total_clusters * 100, 1),
        "mixed_pct": round(mixed / total_clusters * 100, 1),
        "avg_pnl_correlation": round(float(avg_corr), 3) if avg_corr is not None else None,
    }


def exp3_mass_loss_events(trades_df):
    """EXP 3: Analyze mass loss events (3+ coins lose in same cluster)."""
    log.info("\n=== EXP 3: Mass Loss Events ===")

    multi_bars = trades_df[trades_df["cluster_size"] > 1]["entry_bar"].unique()
    mass_losses = []

    for bar in multi_bars:
        cluster = trades_df[trades_df["entry_bar"] == bar]
        losses = cluster[cluster["pnl_net"] <= 0]
        if len(losses) >= 3:
            mass_losses.append({
                "bar": str(bar),
                "total_coins": len(cluster),
                "losing_coins": len(losses),
                "total_loss": float(losses["pnl_net"].sum()),
                "coins": list(losses["coin"].values),
                "directions": list(cluster["dir"].values),
            })

    log.info(f"Mass loss events (3+ coins losing): {len(mass_losses)}")
    total_mass_loss = sum(e["total_loss"] for e in mass_losses)
    log.info(f"Total mass loss: ${total_mass_loss:.0f}")

    if mass_losses:
        worst_5 = sorted(mass_losses, key=lambda x: x["total_loss"])[:5]
        log.info("Worst 5 mass loss events:")
        for e in worst_5:
            log.info(f"  {e['bar']}: {e['losing_coins']}/{e['total_coins']} coins lost, ${e['total_loss']:.0f}")

    # What fraction of total losses come from mass events?
    total_loss_all = trades_df[trades_df["pnl_net"] <= 0]["pnl_net"].sum()
    mass_loss_pct = (total_mass_loss / total_loss_all * 100) if total_loss_all < 0 else 0

    log.info(f"Mass losses as % of all losses: {mass_loss_pct:.1f}%")

    return {
        "mass_loss_events": len(mass_losses),
        "total_mass_loss": round(total_mass_loss, 0),
        "total_all_losses": round(float(total_loss_all), 0),
        "mass_loss_pct": round(float(mass_loss_pct), 1),
        "worst_event": mass_losses[0] if mass_losses else None,
    }


def exp4_cap_entries_per_bar(trades_df, btc_score_ts):
    """EXP 4: What if we cap N entries per signal bar? Pick by coin priority."""
    log.info("\n=== EXP 4: Cap Entries Per Signal Bar ===")

    results = {}
    baseline_pnl = trades_df["pnl_net"].sum()
    baseline_wr = (trades_df["pnl_net"] > 0).mean() * 100
    baseline_trades = len(trades_df)

    # Historical coin performance ranking (from v3 model registry)
    coin_priority = ["DOT", "SUI", "FIL", "ADA", "XRP", "BTC"]  # by backtest PnL descending

    for cap in [1, 2, 3, 4, 5]:
        # For each entry bar with >cap coins, keep only top-priority coins
        filtered = []
        for bar, group in trades_df.groupby("entry_bar"):
            if len(group) <= cap:
                filtered.append(group)
            else:
                # Sort by priority (lower index = higher priority)
                group = group.copy()
                group["priority"] = group["coin"].map(
                    {c: i for i, c in enumerate(coin_priority)}).fillna(99)
                group = group.sort_values("priority").head(cap)
                filtered.append(group)

        cap_df = pd.concat(filtered, ignore_index=True)
        cap_pnl = cap_df["pnl_net"].sum()
        cap_wr = (cap_df["pnl_net"] > 0).mean() * 100
        cap_trades = len(cap_df)
        delta = cap_pnl - baseline_pnl

        log.info(f"  Cap={cap}: {cap_trades} trades ({cap_trades/baseline_trades*100:.0f}%), WR={cap_wr:.1f}%, PnL=${cap_pnl:.0f} (delta=${delta:+.0f})")
        results[cap] = {
            "trades": cap_trades,
            "wr": round(cap_wr, 1),
            "pnl": round(cap_pnl, 0),
            "delta": round(delta, 0),
        }

    log.info(f"  Baseline (no cap): {baseline_trades} trades, WR={baseline_wr:.1f}%, PnL=${baseline_pnl:.0f}")
    results["baseline"] = {
        "trades": baseline_trades,
        "wr": round(baseline_wr, 1),
        "pnl": round(baseline_pnl, 0),
    }
    return results


def exp5_effective_diversification(trades_df):
    """EXP 5: Calculate effective portfolio diversification metrics."""
    log.info("\n=== EXP 5: Effective Diversification ===")

    # Build daily PnL per coin
    trades_df["entry_date"] = pd.to_datetime(trades_df["entry_time"]).dt.date
    daily_pnl = trades_df.pivot_table(index="entry_date", columns="coin", values="pnl_net", aggfunc="sum").fillna(0)

    # Daily return correlation
    if daily_pnl.shape[1] >= 2:
        daily_corr = daily_pnl.corr()
        mask = np.triu(np.ones(daily_corr.shape, dtype=bool), k=1)
        avg_daily_corr = daily_corr.values[mask].mean()
        log.info(f"Avg daily PnL correlation: {avg_daily_corr:.3f}")
        log.info("Daily PnL correlation matrix:")
        log.info(daily_corr.round(3).to_string())
    else:
        avg_daily_corr = None

    # Per-coin Sharpe vs portfolio Sharpe
    coin_sharpes = {}
    for coin in daily_pnl.columns:
        s = daily_pnl[coin]
        if s.std() > 0:
            sharpe = s.mean() / s.std() * np.sqrt(252)
            coin_sharpes[coin] = round(sharpe, 2)
    log.info(f"\nPer-coin annualized Sharpe: {coin_sharpes}")

    portfolio_daily = daily_pnl.sum(axis=1)
    if portfolio_daily.std() > 0:
        port_sharpe = portfolio_daily.mean() / portfolio_daily.std() * np.sqrt(252)
    else:
        port_sharpe = 0
    log.info(f"Portfolio Sharpe: {port_sharpe:.2f}")

    # Diversification ratio = sum of individual vol / portfolio vol
    individual_vols = daily_pnl.std()
    portfolio_vol = portfolio_daily.std()
    if portfolio_vol > 0:
        div_ratio = individual_vols.sum() / portfolio_vol
    else:
        div_ratio = 1.0
    log.info(f"Diversification ratio: {div_ratio:.2f} (>1 = some diversification, higher = better)")

    # Effective N (diversification equivalent)
    # If perfectly correlated, eff_n = 1. If uncorrelated, eff_n = N
    n_coins = len(COINS)
    if avg_daily_corr is not None:
        eff_n = n_coins / (1 + (n_coins - 1) * max(avg_daily_corr, 0))
        log.info(f"Effective N: {eff_n:.2f} out of {n_coins} coins (1=fully correlated, {n_coins}=uncorrelated)")
    else:
        eff_n = None

    # Max drawdown of portfolio
    cumsum = portfolio_daily.cumsum()
    running_max = cumsum.cummax()
    dd = cumsum - running_max
    max_dd = dd.min()
    log.info(f"Portfolio max drawdown: ${max_dd:.0f}")

    # Worst day
    worst_day = portfolio_daily.idxmin()
    worst_day_pnl = portfolio_daily.min()
    log.info(f"Worst day: {worst_day} (${worst_day_pnl:.0f})")

    return {
        "avg_daily_corr": round(float(avg_daily_corr), 3) if avg_daily_corr is not None else None,
        "coin_sharpes": coin_sharpes,
        "portfolio_sharpe": round(float(port_sharpe), 2),
        "diversification_ratio": round(float(div_ratio), 2),
        "effective_n": round(float(eff_n), 2) if eff_n is not None else None,
        "max_drawdown": round(float(max_dd), 0),
        "worst_day": str(worst_day),
        "worst_day_pnl": round(float(worst_day_pnl), 0),
    }


def exp6_paper_trading_clusters():
    """EXP 6: Analyze clustering in actual paper trading."""
    log.info("\n=== EXP 6: Paper Trading Cluster Analysis ===")
    import sqlite3

    db_path = BASE_DIR / "paper_trading" / "state" / "paper_trades.db"
    if not db_path.exists():
        log.info("No paper trading DB found")
        return {}

    conn = sqlite3.connect(str(db_path))
    paper = pd.read_sql("SELECT * FROM trades ORDER BY entry_time", conn)
    conn.close()

    if len(paper) == 0:
        log.info("No paper trades")
        return {}

    paper["entry_bar"] = pd.to_datetime(paper["entry_time"]).dt.floor("15min")
    cluster_counts = paper.groupby("entry_bar").size()
    cluster_dist = cluster_counts.value_counts().sort_index()

    log.info(f"Paper trading: {len(paper)} trades across {len(cluster_counts)} entry bars")
    for size, count in cluster_dist.items():
        log.info(f"  {size} coins in bar: {count} events ({count/len(cluster_counts)*100:.1f}%)")

    # Paper cluster PnL
    paper["cluster_size"] = paper["entry_bar"].map(cluster_counts)
    multi = paper[paper["cluster_size"] > 1]
    single = paper[paper["cluster_size"] == 1]

    paper_multi_wr = (multi["pnl_net"] > 0).mean() * 100 if len(multi) > 0 else None
    paper_single_wr = (single["pnl_net"] > 0).mean() * 100 if len(single) > 0 else None

    log.info(f"Paper single-entry WR: {paper_single_wr:.1f}%" if paper_single_wr else "No single entries")
    log.info(f"Paper multi-entry WR:  {paper_multi_wr:.1f}%" if paper_multi_wr else "No multi entries")

    # Same-bar all-loss clusters in paper
    paper_mass_loss = 0
    for bar, group in multi.groupby("entry_bar"):
        if (group["pnl_net"] <= 0).all() and len(group) >= 2:
            paper_mass_loss += 1

    log.info(f"Paper: all-loss clusters (2+ coins): {paper_mass_loss}")

    return {
        "paper_trades": len(paper),
        "paper_cluster_dist": {int(k): int(v) for k, v in cluster_dist.items()},
        "paper_single_wr": round(float(paper_single_wr), 1) if paper_single_wr is not None else None,
        "paper_multi_wr": round(float(paper_multi_wr), 1) if paper_multi_wr is not None else None,
        "paper_mass_loss_clusters": paper_mass_loss,
    }


def exp7_signal_direction_consistency(trades_df):
    """EXP 7: When coins enter together, are they all same direction?"""
    log.info("\n=== EXP 7: Signal Direction Consistency ===")

    multi_bars = trades_df[trades_df["cluster_size"] > 1]["entry_bar"].unique()
    same_dir = 0
    mixed_dir = 0
    mixed_dir_detail = []

    for bar in multi_bars:
        cluster = trades_df[trades_df["entry_bar"] == bar]
        dirs = cluster["dir"].unique()
        if len(dirs) == 1:
            same_dir += 1
        else:
            mixed_dir += 1
            mixed_dir_detail.append({
                "bar": str(bar),
                "long_coins": list(cluster[cluster["dir"] == "L"]["coin"].values),
                "short_coins": list(cluster[cluster["dir"] == "S"]["coin"].values),
            })

    total = same_dir + mixed_dir
    if total == 0:
        return {}

    log.info(f"Multi-coin clusters: {total}")
    log.info(f"  Same direction: {same_dir} ({same_dir/total*100:.1f}%)")
    log.info(f"  Mixed direction: {mixed_dir} ({mixed_dir/total*100:.1f}%)")

    return {
        "same_direction_pct": round(same_dir / total * 100, 1),
        "mixed_direction_pct": round(mixed_dir / total * 100, 1),
        "mixed_examples": mixed_dir_detail[:5],
    }


def main():
    log.info("=" * 60)
    log.info("Mission #006: Cross-Coin Entry Synchronization")
    log.info("=" * 60)
    started = datetime.utcnow()

    # Build BTC score
    btc_score_ts, btc_df = build_btc_score()

    # Run all backtests
    trades_df = run_all_coin_backtests(btc_score_ts)

    # Run experiments
    r1 = exp1_entry_clustering(trades_df)
    r2 = exp2_outcome_correlation(trades_df)
    r3 = exp3_mass_loss_events(trades_df)
    r4 = exp4_cap_entries_per_bar(trades_df, btc_score_ts)
    r5 = exp5_effective_diversification(trades_df)
    r6 = exp6_paper_trading_clusters()
    r7 = exp7_signal_direction_consistency(trades_df)

    finished = datetime.utcnow()

    # ── Compile results ──
    results = {
        "mission_id": "mission_006_concentration_risk",
        "date": "2026-03-17",
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "total_trades": len(trades_df),
        "total_pnl": round(float(trades_df["pnl_net"].sum()), 0),
        "exp1_clustering": r1,
        "exp2_correlation": r2,
        "exp3_mass_loss": r3,
        "exp4_cap_entries": r4,
        "exp5_diversification": r5,
        "exp6_paper": r6,
        "exp7_direction": r7,
    }

    # Save experiment JSON
    out_dir = BASE_DIR / "experiments"
    out_dir.mkdir(exist_ok=True)
    json_path = out_dir / "mission_006_concentration_risk.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False, default=str)
    log.info(f"\nSaved: {json_path}")

    # Save mission report (separate step in main calling code)
    return results


if __name__ == "__main__":
    results = main()
    print("\n=== DONE ===")
    print(f"Total trades: {results['total_trades']}")
    print(f"Total PnL: ${results['total_pnl']}")
