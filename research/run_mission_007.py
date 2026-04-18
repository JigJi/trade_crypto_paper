"""
Mission #007: Factor Regime Decomposition
==========================================
Hypothesis: v3 uses 8 factors with FIXED weights across all market regimes.
But different factors may contribute differently in BULL/BEAR/FLAT.
If we identify regime-specific strengths, adaptive weighting could outperform.

Experiments:
  EXP 1: Define market regimes (BULL/BEAR/FLAT) from BTC price
  EXP 2: Ablation -- remove each factor in each regime, measure delta PnL
  EXP 3: Factor firing frequency by regime -- which factors actually fire?
  EXP 4: Rolling factor stability -- does each factor's contribution drift over time?
  EXP 5: Regime-adaptive weighting -- use regime-specific optimal weights
"""

import sys, json, logging, warnings
from pathlib import Path
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.WARNING)

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import backtest_15m_btc_led_alts as bt
from paper_trading.config import COMPOSITE_WEIGHTS, V3_EXTRA_WEIGHTS, COIN_CONFIGS

# ---- Config ----
COINS = ["BTCUSDT", "XRPUSDT", "ADAUSDT", "DOTUSDT", "SUIUSDT", "FILUSDT"]
OOS_START = "2025-01-01"
OOS_END = "2026-03-31"

# Factor definitions for ablation
# Each factor maps to the weight keys it uses
FACTOR_MAP = {
    "oi_divergence": {
        "composite": {"w_oi_bull": 0.25, "w_oi_capit": 0.25, "w_oi_weak": 0.25, "w_oi_bear": 0.25},
        "extra": {},
    },
    "funding_rate": {
        "composite": {"w_fr_neg": 2.0, "w_fr_pos": 2.0},
        "extra": {},
    },
    "whale_alerts": {
        "composite": {"w_whale_bull": 1.5, "w_whale_bear": 1.5},
        "extra": {},
    },
    "liquidation": {
        "composite": {"w_liq_bull": 2.0, "w_liq_bear": 2.0},
        "extra": {},
    },
    "etf_flows": {
        "composite": {"w_etf_bull": 1.0, "w_etf_bear": 1.0},
        "extra": {},
    },
    "basis_contrarian": {
        "composite": {},
        "extra": {"basis_contrarian": 1.5},
    },
    "tick_liq": {
        "composite": {},
        "extra": {"tick_liq": 2.0},
    },
    "ob_combined": {
        "composite": {},
        "extra": {"ob_combined": 2.0},
    },
}


def build_btc_score_custom(btc_df, exclude_factor=None, custom_weights=None):
    """Build BTC composite score, optionally excluding a factor or using custom weights."""
    cw = dict(COMPOSITE_WEIGHTS)
    ew = dict(V3_EXTRA_WEIGHTS)

    if custom_weights:
        for k, v in custom_weights.get("composite", {}).items():
            cw[k] = v
        for k, v in custom_weights.get("extra", {}).items():
            ew[k] = v

    if exclude_factor and exclude_factor in FACTOR_MAP:
        fm = FACTOR_MAP[exclude_factor]
        for k in fm["composite"]:
            cw[k] = 0.0
        for k in fm["extra"]:
            ew[k] = 0.0

    score = bt.compute_btc_composite_score(btc_df, params=cw)
    # Recompute extra factors with potentially zeroed weights
    # compute_btc_composite_score already calls score_basis/tick_liq/ob_combined
    # with V3_EXTRA_WEIGHTS, so we need to subtract the originals and add custom
    if exclude_factor in ("basis_contrarian", "tick_liq", "ob_combined"):
        # The function already added these with V3_EXTRA_WEIGHTS
        # We need to subtract them and add with our weights
        if exclude_factor == "basis_contrarian":
            score = score - bt.score_basis_contrarian(btc_df, weight=V3_EXTRA_WEIGHTS["basis_contrarian"])
            score = score + bt.score_basis_contrarian(btc_df, weight=ew.get("basis_contrarian", 0))
        elif exclude_factor == "tick_liq":
            score = score - bt.score_tick_liq(btc_df, weight=V3_EXTRA_WEIGHTS["tick_liq"])
            score = score + bt.score_tick_liq(btc_df, weight=ew.get("tick_liq", 0))
        elif exclude_factor == "ob_combined":
            score = score - bt.score_ob_combined(btc_df, weight=V3_EXTRA_WEIGHTS["ob_combined"])
            score = score + bt.score_ob_combined(btc_df, weight=ew.get("ob_combined", 0))

    return pd.Series(score.values, index=btc_df["ts"].values, name="btc_score")


def compute_individual_factor_scores(btc_df):
    """Compute each factor's individual contribution to the composite score."""
    factor_scores = {}
    for fname in FACTOR_MAP:
        # Score with all factors
        full_score = build_btc_score_custom(btc_df)
        # Score without this factor
        ablated_score = build_btc_score_custom(btc_df, exclude_factor=fname)
        # Difference = this factor's contribution
        diff = full_score.values - ablated_score.values
        factor_scores[fname] = pd.Series(diff, index=btc_df["ts"].values, name=fname)
    return factor_scores


def define_regimes(btc_ohlcv):
    """Define BULL/BEAR/FLAT regimes from BTC daily SMA20 slope."""
    df = btc_ohlcv.copy()
    if "date_time" in df.columns:
        df = df.rename(columns={"date_time": "ts"})
    # Resample to daily
    df = df.set_index("ts")
    daily = df["close"].resample("1D").last().dropna()
    sma20 = daily.rolling(20).mean()
    slope = sma20.pct_change(5)  # 5-day slope of SMA20

    regime = pd.Series("FLAT", index=daily.index)
    regime[slope > 0.005] = "BULL"
    regime[slope < -0.005] = "BEAR"

    # Forward-fill to 15m bars
    regime_15m = regime.resample("15min").ffill()
    return regime_15m


def run_backtest_for_regime(btc_score_ts, btc_ohlcv, regime_15m, target_regime=None):
    """Run backtest, optionally filtered to a specific regime."""
    all_trades = []
    for symbol in COINS:
        coin = symbol.replace("USDT", "")
        ohlcv = bt.fetch_binance_15m(symbol, years=3)
        if "date_time" in ohlcv.columns:
            ohlcv = ohlcv.rename(columns={"date_time": "ts"})
        alt_df = bt.build_alt_technicals(ohlcv)
        oos_mask = (alt_df["ts"] >= OOS_START) & (alt_df["ts"] <= OOS_END)
        alt_oos = alt_df[oos_mask].copy()

        cfg = COIN_CONFIGS.get(coin, {})
        signals, alt_merged = bt.generate_btc_led_signal(
            btc_score_ts, alt_oos,
            threshold=cfg.get("threshold", 3.0),
            use_alt_pa_filter=cfg.get("use_alt_pa_filter", False))

        # If regime filter, zero out signals outside target regime
        if target_regime is not None:
            ts_vals = alt_merged["ts"].values
            regime_aligned = regime_15m.reindex(pd.DatetimeIndex(ts_vals), method="ffill")
            regime_mask = regime_aligned.values == target_regime
            signals = signals.copy()
            signals[~regime_mask] = 0

        trades = bt.run_backtest(alt_merged, signals,
                                 sl_atr_mult=cfg.get("sl_atr_mult", 10.0),
                                 tp_atr_mult=cfg.get("tp_atr_mult", 5.0),
                                 cooldown_bars=cfg.get("cooldown_bars", 4))
        if len(trades) > 0:
            trades["coin"] = coin
            all_trades.append(trades)

    if not all_trades:
        return pd.DataFrame()
    return pd.concat(all_trades, ignore_index=True)


def main():
    print("=" * 70)
    print("MISSION #007: Factor Regime Decomposition")
    print("=" * 70)

    # ---- Load BTC data ----
    print("\n[1/5] Loading BTC data...")
    btc_ohlcv = bt.fetch_binance_15m("BTCUSDT", years=3)
    if "date_time" in btc_ohlcv.columns:
        btc_ohlcv = btc_ohlcv.rename(columns={"date_time": "ts"})
    db_data = bt.load_btc_db_data()
    btc_df = bt.build_btc_features(btc_ohlcv, db_data)

    # ---- Define regimes ----
    regime_15m = define_regimes(btc_ohlcv)
    print("\nRegime distribution (OOS period):")
    oos_regime = regime_15m[(regime_15m.index >= OOS_START) & (regime_15m.index <= OOS_END)]
    regime_counts = oos_regime.value_counts()
    total_bars = len(oos_regime)
    for r in ["BULL", "BEAR", "FLAT"]:
        cnt = regime_counts.get(r, 0)
        print(f"  {r}: {cnt} bars ({cnt/total_bars*100:.1f}%)")

    results = {}

    # ======================================================================
    # EXP 1: BASELINE -- Full v3 score per regime
    # ======================================================================
    print("\n[2/5] EXP 1: Baseline per regime...")
    baseline_score = build_btc_score_custom(btc_df)
    baseline_trades = {}
    exp1 = {}

    for regime in ["BULL", "BEAR", "FLAT", None]:
        label = regime if regime else "ALL"
        trades = run_backtest_for_regime(baseline_score, btc_ohlcv, regime_15m, target_regime=regime)
        if len(trades) > 0:
            pnl = trades["pnl_net"].sum()
            wr = (trades["pnl_net"] > 0).mean() * 100
            n = len(trades)
            avg_pnl = trades["pnl_net"].mean()
        else:
            pnl, wr, n, avg_pnl = 0, 0, 0, 0

        exp1[label] = {"trades": n, "wr": round(wr, 1), "pnl": round(pnl, 2), "avg_pnl": round(avg_pnl, 2)}
        if regime is None:
            baseline_trades["ALL"] = trades
        else:
            baseline_trades[regime] = trades
        print(f"  {label}: {n} trades, WR {wr:.1f}%, PnL ${pnl:,.0f}")

    results["exp1_baseline_by_regime"] = exp1

    # ======================================================================
    # EXP 2: ABLATION -- Remove each factor, measure delta PnL by regime
    # ======================================================================
    print("\n[3/5] EXP 2: Factor ablation by regime...")
    exp2 = {}
    baseline_all_pnl = exp1["ALL"]["pnl"]

    for fname in FACTOR_MAP:
        ablated_score = build_btc_score_custom(btc_df, exclude_factor=fname)
        exp2[fname] = {}

        for regime in ["BULL", "BEAR", "FLAT", None]:
            label = regime if regime else "ALL"
            trades = run_backtest_for_regime(ablated_score, btc_ohlcv, regime_15m, target_regime=regime)
            if len(trades) > 0:
                pnl = trades["pnl_net"].sum()
                wr = (trades["pnl_net"] > 0).mean() * 100
                n = len(trades)
            else:
                pnl, wr, n = 0, 0, 0

            baseline_pnl = exp1[label]["pnl"]
            delta = baseline_pnl - pnl  # positive = factor helps

            exp2[fname][label] = {
                "trades": n,
                "wr": round(wr, 1),
                "pnl": round(pnl, 2),
                "delta": round(delta, 2),
            }

        all_delta = exp2[fname]["ALL"]["delta"]
        bull_delta = exp2[fname]["BULL"]["delta"]
        bear_delta = exp2[fname]["BEAR"]["delta"]
        flat_delta = exp2[fname]["FLAT"]["delta"]
        print(f"  {fname:20s} | ALL: {all_delta:+8.0f} | BULL: {bull_delta:+8.0f} | BEAR: {bear_delta:+8.0f} | FLAT: {flat_delta:+8.0f}")

    results["exp2_ablation_by_regime"] = exp2

    # ======================================================================
    # EXP 3: Factor firing frequency by regime
    # ======================================================================
    print("\n[4/5] EXP 3: Factor firing frequency by regime...")
    factor_scores = compute_individual_factor_scores(btc_df)
    exp3 = {}

    oos_mask_btc = (btc_df["ts"] >= OOS_START) & (btc_df["ts"] <= OOS_END)
    btc_ts_oos = btc_df.loc[oos_mask_btc, "ts"].values

    for fname, fscore in factor_scores.items():
        oos_fs = fscore[fscore.index.isin(pd.DatetimeIndex(btc_ts_oos))]
        if len(oos_fs) == 0:
            exp3[fname] = {"BULL": 0, "BEAR": 0, "FLAT": 0, "ALL": 0}
            continue

        regimes = regime_15m.reindex(oos_fs.index, method="ffill")
        firing = oos_fs.abs() > 0  # factor is non-zero = firing

        exp3[fname] = {}
        for regime in ["BULL", "BEAR", "FLAT"]:
            mask = regimes == regime
            if mask.sum() > 0:
                pct = firing[mask].mean() * 100
            else:
                pct = 0
            exp3[fname][regime] = round(pct, 1)
        exp3[fname]["ALL"] = round(firing.mean() * 100, 1)

        # Also add average score magnitude when firing
        bullish = oos_fs > 0
        bearish = oos_fs < 0
        exp3[fname]["bullish_pct"] = round(bullish.mean() * 100, 1)
        exp3[fname]["bearish_pct"] = round(bearish.mean() * 100, 1)

    print(f"  {'Factor':20s} | {'ALL':>5s} | {'BULL':>5s} | {'BEAR':>5s} | {'FLAT':>5s} | {'Bull%':>5s} | {'Bear%':>5s}")
    print(f"  {'-'*20} | {'-'*5} | {'-'*5} | {'-'*5} | {'-'*5} | {'-'*5} | {'-'*5}")
    for fname in FACTOR_MAP:
        f = exp3[fname]
        print(f"  {fname:20s} | {f['ALL']:5.1f} | {f['BULL']:5.1f} | {f['BEAR']:5.1f} | {f['FLAT']:5.1f} | {f.get('bullish_pct',0):5.1f} | {f.get('bearish_pct',0):5.1f}")

    results["exp3_firing_frequency"] = exp3

    # ======================================================================
    # EXP 4: Rolling factor stability (3-month windows)
    # ======================================================================
    print("\n[5/5] EXP 4: Rolling factor stability...")
    exp4 = {}

    # Define 3-month rolling windows within OOS
    windows = [
        ("2025-01-01", "2025-03-31", "Q1_2025"),
        ("2025-04-01", "2025-06-30", "Q2_2025"),
        ("2025-07-01", "2025-09-30", "Q3_2025"),
        ("2025-10-01", "2025-12-31", "Q4_2025"),
        ("2026-01-01", "2026-03-18", "Q1_2026"),
    ]

    for fname in FACTOR_MAP:
        exp4[fname] = {}
        for w_start, w_end, w_label in windows:
            # Compute baseline for this window
            base_trades_w = []
            abl_trades_w = []
            ablated_score = build_btc_score_custom(btc_df, exclude_factor=fname)

            for symbol in COINS:
                coin = symbol.replace("USDT", "")
                ohlcv = bt.fetch_binance_15m(symbol, years=3)
                if "date_time" in ohlcv.columns:
                    ohlcv = ohlcv.rename(columns={"date_time": "ts"})
                alt_df = bt.build_alt_technicals(ohlcv)
                w_mask = (alt_df["ts"] >= w_start) & (alt_df["ts"] <= w_end)
                alt_w = alt_df[w_mask].copy()
                if len(alt_w) < 10:
                    continue

                cfg = COIN_CONFIGS.get(coin, {})

                # Baseline
                sig_b, merged_b = bt.generate_btc_led_signal(
                    baseline_score, alt_w,
                    threshold=cfg.get("threshold", 3.0),
                    use_alt_pa_filter=cfg.get("use_alt_pa_filter", False))
                trades_b = bt.run_backtest(merged_b, sig_b,
                    sl_atr_mult=cfg.get("sl_atr_mult", 10.0),
                    tp_atr_mult=cfg.get("tp_atr_mult", 5.0),
                    cooldown_bars=cfg.get("cooldown_bars", 4))
                if len(trades_b) > 0:
                    trades_b["coin"] = coin
                    base_trades_w.append(trades_b)

                # Ablated
                sig_a, merged_a = bt.generate_btc_led_signal(
                    ablated_score, alt_w,
                    threshold=cfg.get("threshold", 3.0),
                    use_alt_pa_filter=cfg.get("use_alt_pa_filter", False))
                trades_a = bt.run_backtest(merged_a, sig_a,
                    sl_atr_mult=cfg.get("sl_atr_mult", 10.0),
                    tp_atr_mult=cfg.get("tp_atr_mult", 5.0),
                    cooldown_bars=cfg.get("cooldown_bars", 4))
                if len(trades_a) > 0:
                    trades_a["coin"] = coin
                    abl_trades_w.append(trades_a)

            base_pnl = pd.concat(base_trades_w)["pnl_net"].sum() if base_trades_w else 0
            abl_pnl = pd.concat(abl_trades_w)["pnl_net"].sum() if abl_trades_w else 0
            delta = base_pnl - abl_pnl

            exp4[fname][w_label] = {
                "base_pnl": round(base_pnl, 2),
                "ablated_pnl": round(abl_pnl, 2),
                "delta": round(delta, 2),
            }

        # Print summary
        deltas = [exp4[fname][w[2]]["delta"] for w in windows]
        stability = np.std(deltas) / (np.mean(np.abs(deltas)) + 1e-6)
        exp4[fname]["stability_cv"] = round(stability, 2)
        signs = [1 if d > 0 else -1 for d in deltas]
        exp4[fname]["sign_consistency"] = sum(1 for s in signs if s == signs[0]) / len(signs)
        delta_str = " | ".join([f"{d:+7.0f}" for d in deltas])
        print(f"  {fname:20s} | {delta_str} | CV={stability:.2f}")

    results["exp4_rolling_stability"] = exp4

    # ======================================================================
    # Summary & Save
    # ======================================================================
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)

    # Find regime-dependent factors
    print("\n--- Factor Regime Heatmap (delta PnL = how much removing this factor hurts) ---")
    print(f"  {'Factor':20s} | {'ALL':>8s} | {'BULL':>8s} | {'BEAR':>8s} | {'FLAT':>8s} | Verdict")
    print(f"  {'-'*20} | {'-'*8} | {'-'*8} | {'-'*8} | {'-'*8} | {'-'*20}")

    verdicts = {}
    for fname in FACTOR_MAP:
        d = exp2[fname]
        all_d = d["ALL"]["delta"]
        bull_d = d["BULL"]["delta"]
        bear_d = d["BEAR"]["delta"]
        flat_d = d["FLAT"]["delta"]

        # Classify factor behavior
        if bull_d > 0 and bear_d > 0 and flat_d > 0:
            verdict = "UNIVERSAL (all regimes)"
        elif bull_d > 0 and bear_d > 0 and flat_d <= 0:
            verdict = "TRENDING (bull+bear)"
        elif bull_d <= 0 and bear_d > 0:
            verdict = "BEAR-SPECIALIST"
        elif bull_d > 0 and bear_d <= 0:
            verdict = "BULL-SPECIALIST"
        elif flat_d > 0 and (bull_d <= 0 or bear_d <= 0):
            verdict = "FLAT-SPECIALIST"
        else:
            verdict = "MIXED/WEAK"

        verdicts[fname] = verdict
        print(f"  {fname:20s} | {all_d:+8.0f} | {bull_d:+8.0f} | {bear_d:+8.0f} | {flat_d:+8.0f} | {verdict}")

    # Rolling stability summary
    print("\n--- Factor Stability Over Time ---")
    print(f"  {'Factor':20s} | {'Sign Consistency':>16s} | {'CV':>5s} | Verdict")
    for fname in FACTOR_MAP:
        sc = exp4[fname]["sign_consistency"]
        cv = exp4[fname]["stability_cv"]
        if sc >= 0.8 and cv < 1.5:
            sv = "STABLE"
        elif sc >= 0.6:
            sv = "MODERATE"
        else:
            sv = "UNSTABLE"
        print(f"  {fname:20s} | {sc*100:>15.0f}% | {cv:5.2f} | {sv}")

    # Build final results
    results["verdicts"] = verdicts
    results["summary"] = {
        "baseline_all_pnl": exp1["ALL"]["pnl"],
        "baseline_all_trades": exp1["ALL"]["trades"],
        "baseline_all_wr": exp1["ALL"]["wr"],
        "regime_distribution": {r: regime_counts.get(r, 0) for r in ["BULL", "BEAR", "FLAT"]},
        "total_oos_bars": total_bars,
        "experiments_run": 4,
    }

    # Save results
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    json_path = BASE_DIR / "experiments" / f"mission_007_factor_regime_{ts}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False, default=str)
    print(f"\nResults saved: {json_path}")

    return results


if __name__ == "__main__":
    results = main()
