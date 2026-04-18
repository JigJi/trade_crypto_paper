"""
Mission 034: Factor Weight Robustness & Sensitivity Analysis
=============================================================
Test how sensitive the v3 strategy is to weight changes.
For each of the 8 production factors, perturb its weight with multipliers
[0, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0] and measure PnL impact.

Goal: Determine if weights are fragile (overfit) or robust.
"""

import sys, json, logging
import numpy as np, pandas as pd
from pathlib import Path
from datetime import datetime
from copy import deepcopy

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
MULTIPLIERS = [0.0, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0]

FACTOR_GROUPS = {
    "oi_divergence": {
        "type": "composite",
        "keys": ["w_oi_bull", "w_oi_capit", "w_oi_weak", "w_oi_bear"],
        "prod_weight": 0.5,
    },
    "funding_rate": {
        "type": "composite",
        "keys": ["w_fr_neg", "w_fr_pos"],
        "prod_weight": 2.0,
    },
    "whale_alerts": {
        "type": "composite",
        "keys": ["w_whale_bull", "w_whale_bear"],
        "prod_weight": 1.5,
    },
    "liquidation": {
        "type": "composite",
        "keys": ["w_liq_bull", "w_liq_bear"],
        "prod_weight": 2.0,
    },
    "etf_flows": {
        "type": "composite",
        "keys": ["w_etf_bull", "w_etf_bear"],
        "prod_weight": 1.0,
    },
    "ob_combined": {
        "type": "extra",
        "key": "ob_combined",
        "prod_weight": 2.0,
    },
    "basis_contrarian": {
        "type": "extra",
        "key": "basis_contrarian",
        "prod_weight": 1.5,
    },
    "tick_liq": {
        "type": "extra",
        "key": "tick_liq",
        "prod_weight": 2.0,
    },
}


def compute_score_with_weights(btc_df, comp_weights, extra_weights):
    score = compute_btc_composite_score(btc_df, comp_weights)
    score = score + score_ob_combined(btc_df, weight=extra_weights["ob_combined"])
    score = score + score_basis_contrarian(btc_df, weight=extra_weights["basis_contrarian"])
    score = score + score_tick_liq(btc_df, weight=extra_weights["tick_liq"])
    return pd.Series(score.values, index=btc_df["ts"].values, name="btc_score")


def run_backtest_portfolio(btc_score_ts, coin_ohlcvs):
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


def perturb_weights(factor_name, multiplier):
    comp_w = deepcopy(DEFAULT_COMPOSITE_WEIGHTS)
    extra_w = deepcopy(DEFAULT_EXTRA_WEIGHTS)
    fg = FACTOR_GROUPS[factor_name]

    if fg["type"] == "composite":
        base_vals = {k: DEFAULT_COMPOSITE_WEIGHTS[k] for k in fg["keys"]}
        for k in fg["keys"]:
            comp_w[k] = base_vals[k] * multiplier
    else:
        extra_w[fg["key"]] = DEFAULT_EXTRA_WEIGHTS[fg["key"]] * multiplier

    return comp_w, extra_w


def analyze_trades(trades_df):
    if trades_df is None or len(trades_df) == 0:
        return {"n_trades": 0, "pnl": 0, "wr": 0, "avg_pnl": 0, "sharpe": 0}
    n = len(trades_df)
    pnl = trades_df["pnl_net"].sum()
    wr = (trades_df["pnl_net"] > 0).mean() * 100
    avg = trades_df["pnl_net"].mean()
    std = trades_df["pnl_net"].std()
    sharpe = (avg / std * np.sqrt(252 * 4)) if std > 0 else 0
    return {"n_trades": n, "pnl": round(pnl, 2), "wr": round(wr, 1),
            "avg_pnl": round(avg, 4), "sharpe": round(sharpe, 2)}


def main():
    log.info("=== Mission 034: Factor Weight Sensitivity ===")

    # Phase 1: Load data
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

    # Phase 2: Baseline
    log.info("Running baseline (all weights at 1.0x)...")
    baseline_score = compute_score_with_weights(btc_df, DEFAULT_COMPOSITE_WEIGHTS, DEFAULT_EXTRA_WEIGHTS)
    baseline_trades = run_backtest_portfolio(baseline_score, coin_ohlcvs)
    baseline_stats = analyze_trades(baseline_trades)
    log.info(f"Baseline: {baseline_stats['n_trades']} trades, PnL ${baseline_stats['pnl']}, WR {baseline_stats['wr']}%")

    # Phase 3: Perturbation tests
    results = {"baseline": baseline_stats, "perturbations": {}}

    for factor_name, fg in FACTOR_GROUPS.items():
        log.info(f"\n--- Testing {factor_name} (prod weight={fg['prod_weight']}) ---")
        factor_results = []
        for mult in MULTIPLIERS:
            comp_w, extra_w = perturb_weights(factor_name, mult)
            score = compute_score_with_weights(btc_df, comp_w, extra_w)
            trades = run_backtest_portfolio(score, coin_ohlcvs)
            stats = analyze_trades(trades)
            stats["multiplier"] = mult
            stats["effective_weight"] = round(fg["prod_weight"] * mult, 2)
            stats["delta_pnl"] = round(stats["pnl"] - baseline_stats["pnl"], 2)
            stats["delta_pnl_pct"] = round(stats["delta_pnl"] / max(abs(baseline_stats["pnl"]), 1) * 100, 1)
            factor_results.append(stats)
            log.info(f"  {factor_name} x{mult}: PnL ${stats['pnl']} (delta ${stats['delta_pnl']}), "
                     f"WR {stats['wr']}%, trades={stats['n_trades']}")
        results["perturbations"][factor_name] = factor_results

    # Phase 4: Sensitivity metrics
    log.info("\n=== Computing Sensitivity Metrics ===")
    sensitivity = {}
    for factor_name, factor_results in results["perturbations"].items():
        pnls = [r["pnl"] for r in factor_results]
        mults = [r["multiplier"] for r in factor_results]

        pnl_range = max(pnls) - min(pnls)
        zero_pnl = next(r["pnl"] for r in factor_results if r["multiplier"] == 0.0)
        removal_impact = baseline_stats["pnl"] - zero_pnl
        best_mult = mults[np.argmax(pnls)]
        best_pnl = max(pnls)

        small_perts = [r for r in factor_results if r["multiplier"] in [0.75, 1.0, 1.25]]
        local_range = max(r["pnl"] for r in small_perts) - min(r["pnl"] for r in small_perts)

        sensitivity[factor_name] = {
            "removal_impact": round(removal_impact, 2),
            "removal_impact_pct": round(removal_impact / max(abs(baseline_stats["pnl"]), 1) * 100, 1),
            "pnl_range": round(pnl_range, 2),
            "local_sensitivity": round(local_range, 2),
            "best_multiplier": best_mult,
            "best_pnl": round(best_pnl, 2),
            "current_is_best": best_mult == 1.0,
            "prod_weight": FACTOR_GROUPS[factor_name]["prod_weight"],
        }
        log.info(f"{factor_name}: removal=${removal_impact:.0f}, range=${pnl_range:.0f}, "
                 f"best_mult={best_mult}, local_sensitivity=${local_range:.0f}")

    results["sensitivity"] = sensitivity

    # Phase 5: Robustness score
    total_pnl = baseline_stats["pnl"]
    avg_removal_pct = np.mean([abs(s["removal_impact_pct"]) for s in sensitivity.values()])
    avg_local_sensitivity = np.mean([s["local_sensitivity"] for s in sensitivity.values()])
    factors_at_optimum = sum(1 for s in sensitivity.values() if s["current_is_best"])

    results["robustness_summary"] = {
        "baseline_pnl": total_pnl,
        "avg_removal_impact_pct": round(avg_removal_pct, 1),
        "avg_local_sensitivity": round(avg_local_sensitivity, 2),
        "factors_at_optimum": f"{factors_at_optimum}/8",
        "verdict": "ROBUST" if avg_removal_pct < 15 and factors_at_optimum >= 4 else
                   "MODERATE" if avg_removal_pct < 30 else "FRAGILE",
    }

    log.info(f"\n=== ROBUSTNESS: {results['robustness_summary']['verdict']} ===")
    log.info(f"Avg removal impact: {avg_removal_pct:.1f}%")
    log.info(f"Factors at optimum: {factors_at_optimum}/8")

    # Save results
    out_json = BASE_DIR / "missions" / "mission_034_factor_weight_sensitivity.json"
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False, default=str)
    log.info(f"Saved: {out_json}")

    return results


if __name__ == "__main__":
    main()
