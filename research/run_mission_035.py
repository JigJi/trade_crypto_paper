"""
Mission 035: Factor Interaction Effects — Synergy & Conflict Between Factor Pairs
==================================================================================
Mission 034 tested factors independently. This mission tests PAIRS:
- Do certain factor pairs amplify each other (synergy)?
- Do some pairs cancel each other (conflict)?
- Does a "confluence filter" (trade only when N+ factors agree) improve WR?

Approach:
1. Compute individual factor contributions per bar (not combined score)
2. For each pair of top 6 factors, classify bars into 4 quadrants:
   - Both bullish, Both bearish, A bull + B bear, A bear + B bull
3. Analyze trade performance in each quadrant
4. Test confluence filter: only trade when 3+, 4+, 5+ factors agree on direction
"""

import sys, json, logging
import numpy as np, pandas as pd
from pathlib import Path
from datetime import datetime
from itertools import combinations

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import backtest_15m_btc_led_alts as bt
from signal_core import (
    DEFAULT_COMPOSITE_WEIGHTS, DEFAULT_EXTRA_WEIGHTS,
    compute_btc_composite_score, score_ob_combined,
    score_basis_contrarian, score_tick_liq,
)
from paper_trading.config import COIN_CONFIGS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

COINS = ["BTCUSDT", "XRPUSDT", "ADAUSDT", "DOTUSDT", "SUIUSDT", "FILUSDT"]
OOS_START, OOS_END = "2025-01-01", "2026-03-31"


def compute_individual_factor_scores(btc_df):
    """Compute each factor's contribution separately, returning a DataFrame."""
    factors = {}

    # 1. OI Divergence
    if "oi_chg" in btc_df.columns:
        oi_chg = btc_df["oi_chg"].fillna(0)
        ret = btc_df["ret"].fillna(0)
        s = pd.Series(0.0, index=btc_df.index)
        s += np.where((ret > 0.001) & (oi_chg > 0.002), 0.25, 0)
        s += np.where((ret < -0.001) & (oi_chg < -0.002), 0.25, 0)
        s += np.where((ret > 0.001) & (oi_chg < -0.002), -0.25, 0)
        s += np.where((ret < -0.001) & (oi_chg > 0.002), -0.25, 0)
        factors["oi_divergence"] = s

    # 2. Funding Rate
    if "fr_8h" in btc_df.columns:
        fr = btc_df["fr_8h"].fillna(0)
        s = pd.Series(0.0, index=btc_df.index)
        s += np.where(fr < -0.0001, 2.0, 0)
        s += np.where(fr > 0.0003, -2.0, 0)
        factors["funding_rate"] = s

    # 3. Whale Alerts
    if "whale_net_ma" in btc_df.columns:
        wn_ma = btc_df["whale_net_ma"].fillna(0)
        s = pd.Series(0.0, index=btc_df.index)
        s += np.where(wn_ma > 50_000_000, 1.5, 0)
        s += np.where(wn_ma < -50_000_000, -1.5, 0)
        factors["whale_alerts"] = s

    # 4. Liquidation
    if "liq_net" in btc_df.columns and "liq_total_ma" in btc_df.columns:
        lt = btc_df["liq_total"].fillna(0)
        lt_ma = btc_df["liq_total_ma"].fillna(1)
        ln = btc_df["liq_net"].fillna(0)
        cascade = lt > (lt_ma * 3)
        s = pd.Series(0.0, index=btc_df.index)
        s += np.where(cascade & (ln > 0), 2.0, 0)
        s += np.where(cascade & (ln < 0), -2.0, 0)
        factors["liquidation"] = s

    # 5. ETF Flows
    if "etf_flow_ma" in btc_df.columns:
        etf_ma = btc_df["etf_flow_ma"].fillna(0)
        s = pd.Series(0.0, index=btc_df.index)
        s += np.where(etf_ma > 50, 1.0, 0)
        s += np.where(etf_ma < -50, -1.0, 0)
        factors["etf_flows"] = s

    # 6. Basis Contrarian
    s = score_basis_contrarian(btc_df, weight=1.5)
    factors["basis_contrarian"] = s

    # 7. Tick Liq
    s = score_tick_liq(btc_df, weight=2.0)
    factors["tick_liq"] = s

    # 8. OB Combined
    s = score_ob_combined(btc_df, weight=2.0)
    factors["ob_combined"] = s

    return pd.DataFrame(factors, index=btc_df.index)


def run_backtest_portfolio(btc_score_ts, coin_ohlcvs):
    """Standard portfolio backtest, returns trades DataFrame."""
    all_trades = []
    for symbol, ohlcv in coin_ohlcvs.items():
        coin = symbol.replace("USDT", "")
        alt_df = bt.build_alt_technicals(ohlcv)
        oos_mask = (alt_df["ts"] >= OOS_START) & (alt_df["ts"] <= OOS_END)
        cfg = COIN_CONFIGS.get(coin, {})
        signals, alt_merged = bt.generate_btc_led_signal(
            btc_score_ts, alt_df[oos_mask],
            threshold=cfg.get("threshold", 3.0),
            use_alt_pa_filter=cfg.get("use_alt_pa_filter", False),
        )
        trades = bt.run_backtest(
            alt_merged, signals,
            sl_atr_mult=cfg.get("sl_atr_mult", 2.5),
            tp_atr_mult=cfg.get("tp_atr_mult", 4.0),
            cooldown_bars=cfg.get("cooldown_bars", 4),
        )
        if len(trades) > 0:
            trades["coin"] = coin
            all_trades.append(trades)
    if all_trades:
        return pd.concat(all_trades, ignore_index=True)
    return pd.DataFrame()


def analyze_trades(trades_df):
    if trades_df is None or len(trades_df) == 0:
        return {"n_trades": 0, "pnl": 0, "wr": 0, "avg_pnl": 0}
    n = len(trades_df)
    pnl = trades_df["pnl_net"].sum()
    wr = (trades_df["pnl_net"] > 0).mean() * 100
    avg = trades_df["pnl_net"].mean()
    return {"n_trades": n, "pnl": round(pnl, 2), "wr": round(wr, 1), "avg_pnl": round(avg, 4)}


def main():
    log.info("=== Mission 035: Factor Interaction Effects ===")

    # ---- Phase 1: Load data ----
    log.info("Loading BTC OHLCV + DB data...")
    btc_ohlcv = bt.fetch_binance_15m("BTCUSDT", years=3)
    if "date_time" in btc_ohlcv.columns:
        btc_ohlcv = btc_ohlcv.rename(columns={"date_time": "ts"})
    db_data = bt.load_btc_db_data()
    btc_df = bt.build_btc_features(btc_ohlcv, db_data)

    log.info("Loading altcoin OHLCV...")
    coin_ohlcvs = {}
    for symbol in COINS:
        ohlcv = bt.fetch_binance_15m(symbol, years=3)
        if "date_time" in ohlcv.columns:
            ohlcv = ohlcv.rename(columns={"date_time": "ts"})
        coin_ohlcvs[symbol] = ohlcv

    # ---- Compute individual factor scores ----
    log.info("Computing individual factor scores...")
    factor_df = compute_individual_factor_scores(btc_df)
    # Add ts for merging
    factor_df["ts"] = btc_df["ts"].values

    # OOS filter
    oos_mask = (factor_df["ts"] >= OOS_START) & (factor_df["ts"] <= OOS_END)
    factor_oos = factor_df[oos_mask].copy()

    # Classify each factor as bullish (+1), bearish (-1), or neutral (0)
    factor_dir = pd.DataFrame(index=factor_oos.index)
    for col in factor_oos.columns:
        if col == "ts":
            continue
        factor_dir[col] = np.sign(factor_oos[col])

    log.info(f"Factor scores computed: {factor_oos.shape[0]} bars, {len(factor_dir.columns)} factors")

    # Factor activation rates
    activation = {}
    for col in factor_dir.columns:
        n_active = (factor_dir[col] != 0).sum()
        pct = n_active / len(factor_dir) * 100
        activation[col] = {"active_bars": int(n_active), "pct": round(pct, 1)}
        log.info(f"  {col}: {n_active} active bars ({pct:.1f}%)")

    # ---- Phase 2: Baseline backtest ----
    log.info("Running baseline backtest...")
    baseline_score = compute_btc_composite_score(btc_df, DEFAULT_COMPOSITE_WEIGHTS, DEFAULT_EXTRA_WEIGHTS)
    baseline_score_ts = pd.Series(baseline_score.values, index=btc_df["ts"].values, name="btc_score")
    baseline_trades = run_backtest_portfolio(baseline_score_ts, coin_ohlcvs)
    baseline_stats = analyze_trades(baseline_trades)
    log.info(f"Baseline: {baseline_stats['n_trades']} trades, PnL ${baseline_stats['pnl']}, WR {baseline_stats['wr']}%")

    # Map entry_time to factor state at entry
    if len(baseline_trades) > 0:
        # Merge factor directions at entry time
        factor_ts = factor_oos.set_index("ts")
        factor_dir_ts = factor_dir.copy()
        factor_dir_ts["ts"] = factor_oos["ts"].values
        factor_dir_ts = factor_dir_ts.set_index("ts")

        # For each trade, find closest factor bar <= entry_time
        trade_factors = []
        for _, trade in baseline_trades.iterrows():
            et = trade["entry_time"]
            mask = factor_dir_ts.index <= et
            if mask.any():
                row = factor_dir_ts.loc[mask].iloc[-1]
                trade_factors.append(row)
            else:
                trade_factors.append(pd.Series(0, index=factor_dir_ts.columns))

        trade_factor_df = pd.DataFrame(trade_factors).reset_index(drop=True)
        trades_with_factors = pd.concat([baseline_trades.reset_index(drop=True), trade_factor_df], axis=1)
    else:
        log.error("No baseline trades!")
        return

    # ---- Phase 3: Pair interaction analysis ----
    # Focus on top 6 factors (skip oi_divergence and whale_alerts per Mission 034)
    TOP_FACTORS = ["liquidation", "tick_liq", "ob_combined", "etf_flows", "basis_contrarian", "funding_rate"]
    # Filter to factors that exist
    TOP_FACTORS = [f for f in TOP_FACTORS if f in trades_with_factors.columns]
    log.info(f"\nAnalyzing pairs from: {TOP_FACTORS}")

    pair_results = {}
    for fa, fb in combinations(TOP_FACTORS, 2):
        log.info(f"\n--- Pair: {fa} x {fb} ---")
        pair_key = f"{fa}_x_{fb}"

        # 4 quadrants
        quadrants = {
            "both_bull": (trades_with_factors[fa] > 0) & (trades_with_factors[fb] > 0),
            "both_bear": (trades_with_factors[fa] < 0) & (trades_with_factors[fb] < 0),
            "A_bull_B_bear": (trades_with_factors[fa] > 0) & (trades_with_factors[fb] < 0),
            "A_bear_B_bull": (trades_with_factors[fa] < 0) & (trades_with_factors[fb] > 0),
            "both_neutral_or_mixed": ~(
                ((trades_with_factors[fa] > 0) & (trades_with_factors[fb] > 0)) |
                ((trades_with_factors[fa] < 0) & (trades_with_factors[fb] < 0)) |
                ((trades_with_factors[fa] > 0) & (trades_with_factors[fb] < 0)) |
                ((trades_with_factors[fa] < 0) & (trades_with_factors[fb] > 0))
            ),
        }

        # Also: "agree" = both same sign (both bull or both bear), "disagree" = opposite signs
        agree_mask = ((trades_with_factors[fa] > 0) & (trades_with_factors[fb] > 0)) | \
                     ((trades_with_factors[fa] < 0) & (trades_with_factors[fb] < 0))
        disagree_mask = ((trades_with_factors[fa] > 0) & (trades_with_factors[fb] < 0)) | \
                        ((trades_with_factors[fa] < 0) & (trades_with_factors[fb] > 0))

        quad_stats = {}
        for qname, qmask in quadrants.items():
            subset = trades_with_factors[qmask]
            quad_stats[qname] = analyze_trades(subset)

        agree_stats = analyze_trades(trades_with_factors[agree_mask])
        disagree_stats = analyze_trades(trades_with_factors[disagree_mask])

        # Synergy metric: agree WR - baseline WR
        synergy = agree_stats["wr"] - baseline_stats["wr"] if agree_stats["n_trades"] >= 20 else None
        conflict = disagree_stats["wr"] - baseline_stats["wr"] if disagree_stats["n_trades"] >= 20 else None

        pair_results[pair_key] = {
            "factor_a": fa,
            "factor_b": fb,
            "quadrants": quad_stats,
            "agree": agree_stats,
            "disagree": disagree_stats,
            "synergy_wr_delta": round(synergy, 1) if synergy is not None else None,
            "conflict_wr_delta": round(conflict, 1) if conflict is not None else None,
        }

        log.info(f"  Agree: {agree_stats['n_trades']} trades, WR {agree_stats['wr']}%, PnL ${agree_stats['pnl']}")
        log.info(f"  Disagree: {disagree_stats['n_trades']} trades, WR {disagree_stats['wr']}%, PnL ${disagree_stats['pnl']}")
        if synergy is not None:
            log.info(f"  Synergy delta: {synergy:+.1f}% WR")
        if conflict is not None:
            log.info(f"  Conflict delta: {conflict:+.1f}% WR")

    # ---- Phase 4: Confluence filter ----
    log.info("\n=== Confluence Filter Test ===")
    # Count how many factors agree on direction at each trade entry
    # "agree on bullish" = factor > 0, "agree on bearish" = factor < 0
    bull_count = pd.Series(0, index=trades_with_factors.index)
    bear_count = pd.Series(0, index=trades_with_factors.index)
    for f in TOP_FACTORS:
        if f in trades_with_factors.columns:
            bull_count += (trades_with_factors[f] > 0).astype(int)
            bear_count += (trades_with_factors[f] < 0).astype(int)

    trades_with_factors["bull_count"] = bull_count
    trades_with_factors["bear_count"] = bear_count
    trades_with_factors["max_agree"] = trades_with_factors[["bull_count", "bear_count"]].max(axis=1)
    trades_with_factors["dominant_dir"] = np.where(
        trades_with_factors["bull_count"] > trades_with_factors["bear_count"], "bull",
        np.where(trades_with_factors["bear_count"] > trades_with_factors["bull_count"], "bear", "neutral")
    )

    confluence_results = {}
    for min_agree in range(1, len(TOP_FACTORS) + 1):
        mask = trades_with_factors["max_agree"] >= min_agree
        subset = trades_with_factors[mask]
        stats = analyze_trades(subset)
        confluence_results[f"min_{min_agree}_agree"] = stats
        log.info(f"  {min_agree}+ factors agree: {stats['n_trades']} trades, WR {stats['wr']}%, PnL ${stats['pnl']}")

    # ---- Phase 5: Direction-aligned confluence ----
    # Only count trades where the TRADE direction matches the factor consensus
    log.info("\n=== Direction-Aligned Confluence ===")
    dir_aligned_results = {}
    for min_agree in range(1, len(TOP_FACTORS) + 1):
        # SHORT trades where bear_count >= min_agree
        short_mask = (trades_with_factors.get("dir", "") == "S") & (trades_with_factors["bear_count"] >= min_agree)
        # LONG trades where bull_count >= min_agree (even though LONG is weak)
        long_mask = (trades_with_factors.get("dir", "") == "L") & (trades_with_factors["bull_count"] >= min_agree)
        aligned_mask = short_mask | long_mask

        if aligned_mask.any():
            subset = trades_with_factors[aligned_mask]
            stats = analyze_trades(subset)
            dir_aligned_results[f"aligned_{min_agree}"] = stats
            log.info(f"  Aligned {min_agree}+: {stats['n_trades']} trades, WR {stats['wr']}%, PnL ${stats['pnl']}")

    # ---- Save results ----
    results = {
        "mission_id": "mission_035_factor_interaction",
        "date": datetime.utcnow().strftime("%Y-%m-%d"),
        "baseline": baseline_stats,
        "activation_rates": activation,
        "pair_interactions": pair_results,
        "confluence_filter": confluence_results,
        "direction_aligned": dir_aligned_results,
        "top_factors_used": TOP_FACTORS,
    }

    out_json = BASE_DIR / "missions" / "mission_035_factor_interaction.json"
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False, default=str)
    log.info(f"\nSaved: {out_json}")

    # ---- Find top synergy/conflict pairs ----
    synergy_ranking = []
    for pk, pv in pair_results.items():
        if pv["synergy_wr_delta"] is not None:
            synergy_ranking.append((pk, pv["synergy_wr_delta"], pv["agree"]["n_trades"],
                                    pv["conflict_wr_delta"], pv["disagree"]["n_trades"]))

    synergy_ranking.sort(key=lambda x: x[1] if x[1] is not None else 0, reverse=True)

    log.info("\n=== SYNERGY RANKING (agree WR delta vs baseline) ===")
    for pk, syn, n_agree, conf, n_dis in synergy_ranking:
        log.info(f"  {pk}: synergy={syn:+.1f}% ({n_agree} trades), conflict={conf:+.1f}% ({n_dis} trades)" if conf else
                 f"  {pk}: synergy={syn:+.1f}% ({n_agree} trades), conflict=N/A")

    return results


if __name__ == "__main__":
    results = main()
