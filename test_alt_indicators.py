"""
Alt-Specific OHLCV Indicator Test
==================================
Test whether alt-specific technical indicators (RSI, Bollinger, z-score,
volume spike, momentum, EMA trend) improve trade quality when added to
the BTC composite score before threshold comparison.

Key difference from Phase 1 / v4 tests:
  - Those tests add BTC-level factors → same addon for all coins
  - This test adds per-coin indicators → addon differs per coin's OHLCV

Formula: effective_score = btc_score + alt_indicator_bonus * weight
         → compare effective_score against threshold

Methodology:
  Phase A: Individual test (6 indicators × 3 weights × 6 coins)
  Phase B: Stepwise build (sort by best delta, add one at a time)

No new data needed -- uses Binance OHLCV already fetched by backtest infra.
"""

import os, sys, json, warnings
from datetime import datetime

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

sys.path.insert(0, r"D:\0_product_dev\trade_crypto")
from backtest_15m_btc_led_alts import (
    fetch_binance_15m, build_alt_technicals, run_backtest, calc_metrics,
    BKK_UTC_OFFSET, LEVERAGE,
)
import backtest_15m_btc_led_alts as bt
from test_v12_improvements import V11_CONFIGS, ALL_COINS, generate_signal_v11
from test_phase1_factors import load_btc_db_data_v3

from paper_trading.strategy import (
    compute_btc_composite_score, build_btc_features,
)

# ---- Constants ----
OOS_START = pd.Timestamp("2026-01-01")
OOS_END   = pd.Timestamp("2026-03-10")
WEIGHTS_TO_TEST = [0.25, 0.5, 1.0]


# =====================================================================
# 1) ALT INDICATOR SCORE FUNCTIONS
# =====================================================================
# Each function takes alt_df (with ts, open, high, low, close, volume)
# and returns a pd.Series of bonus scores aligned to alt_df.index.

def score_rsi_contrarian(alt_df, weight=1.0):
    """RSI contrarian: oversold (< 25) → +bonus, overbought (> 75) → -bonus."""
    s = pd.Series(0.0, index=alt_df.index)
    rsi = alt_df["close"].rolling(14).apply(
        lambda x: _rsi_calc(x), raw=True
    )
    s += np.where(rsi < 25, weight, 0)
    s += np.where(rsi < 15, weight * 0.5, 0)
    s += np.where(rsi > 75, -weight, 0)
    s += np.where(rsi > 85, -weight * 0.5, 0)
    return s


def _rsi_calc(prices):
    """Compute RSI from a window of prices."""
    deltas = np.diff(prices)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = gains.mean()
    avg_loss = losses.mean()
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def score_bollinger_contrarian(alt_df, weight=1.0):
    """Bollinger %B contrarian: %B < 0 → +bonus, %B > 1 → -bonus."""
    s = pd.Series(0.0, index=alt_df.index)
    sma = alt_df["close"].rolling(20).mean()
    std = alt_df["close"].rolling(20).std().clip(lower=1e-8)
    upper = sma + 2 * std
    lower = sma - 2 * std
    pct_b = (alt_df["close"] - lower) / (upper - lower)

    s += np.where(pct_b < 0, weight, 0)
    s += np.where(pct_b < -0.2, weight * 0.5, 0)
    s += np.where(pct_b > 1, -weight, 0)
    s += np.where(pct_b > 1.2, -weight * 0.5, 0)
    return s


def score_mean_reversion(alt_df, weight=1.0):
    """Mean reversion (z-score): price far below SMA50 → +bonus, far above → -bonus."""
    s = pd.Series(0.0, index=alt_df.index)
    sma50 = alt_df["close"].rolling(50).mean()
    std50 = alt_df["close"].rolling(50).std().clip(lower=1e-8)
    z = (alt_df["close"] - sma50) / std50

    s += np.where(z < -2.0, weight, 0)
    s += np.where(z < -3.0, weight * 0.5, 0)
    s += np.where(z > 2.0, -weight, 0)
    s += np.where(z > 3.0, -weight * 0.5, 0)
    return s


def score_volume_spike(alt_df, weight=1.0):
    """Volume spike confirmation: volume > 2x MA → +bonus (confirms any signal)."""
    s = pd.Series(0.0, index=alt_df.index)
    vol_ma = alt_df["volume"].rolling(20).mean()
    ratio = alt_df["volume"] / vol_ma.clip(lower=1e-8)

    s += np.where(ratio > 2.0, weight * 0.5, 0)
    s += np.where(ratio > 3.0, weight * 0.5, 0)
    return s


def score_momentum_roc(alt_df, weight=1.0):
    """Momentum (ROC): 12-bar rate of change > 1% → +bonus, < -1% → -bonus."""
    s = pd.Series(0.0, index=alt_df.index)
    roc = alt_df["close"].pct_change(12)

    s += np.where(roc > 0.01, weight, 0)
    s += np.where(roc > 0.02, weight * 0.5, 0)
    s += np.where(roc < -0.01, -weight, 0)
    s += np.where(roc < -0.02, -weight * 0.5, 0)
    return s


def score_ema_trend(alt_df, weight=1.0):
    """EMA trend: close > EMA50 → +bonus, close < EMA50 → -bonus."""
    s = pd.Series(0.0, index=alt_df.index)
    ema50 = alt_df["close"].ewm(span=50, adjust=False).mean()

    s += np.where(alt_df["close"] > ema50, weight * 0.5, 0)
    s += np.where(alt_df["close"] < ema50, -weight * 0.5, 0)
    return s


# =====================================================================
# 2) MODIFIED SIGNAL GENERATION (with alt-specific bonus)
# =====================================================================

def generate_signal_with_alt(btc_score_ts, alt_df, threshold, use_alt_pa,
                             alt_score_fn=None, alt_weight=0.5):
    """
    Like generate_signal_v11 but adds alt_score bonus to btc_score before
    threshold comparison.

    effective_score = btc_score + alt_score_fn(alt_df) * alt_weight
    """
    btc_score_df = btc_score_ts.reset_index()
    btc_score_df.columns = ["ts", "btc_score"]
    alt = alt_df.copy().sort_values("ts")
    alt = pd.merge_asof(alt, btc_score_df.sort_values("ts"),
                        on="ts", direction="backward",
                        tolerance=pd.Timedelta("30min"))
    alt["btc_score"] = alt["btc_score"].fillna(0)

    # Add alt-specific bonus
    if alt_score_fn is not None:
        alt_bonus = alt_score_fn(alt, weight=alt_weight)
        alt["btc_score"] = alt["btc_score"] + alt_bonus

    signal = pd.Series(0, index=alt.index)
    signal[alt["btc_score"] >= threshold] = 1
    signal[alt["btc_score"] <= -threshold] = -1

    if use_alt_pa:
        alt_bull_pa = (alt["close"] > alt["ema9"]) & (alt["ema9"] > alt["ema21"])
        alt_bear_pa = (alt["close"] < alt["ema9"]) & (alt["ema9"] < alt["ema21"])
        alt_vol_ok = alt["vol_ratio"] > 0.8
        mask_long = (signal == 1) & ~(alt_bull_pa & alt_vol_ok)
        mask_short = (signal == -1) & ~(alt_bear_pa & alt_vol_ok)
        signal[mask_long] = 0
        signal[mask_short] = 0

    return signal, alt


# =====================================================================
# 3) BACKTEST RUNNERS
# =====================================================================

def run_v3_baseline(btc_score_ts, alt_cache):
    """Run v3 baseline across all 6 coins, return total OOS PnL + per-coin."""
    total_pnl = 0.0
    results = {}
    old_lev = bt.LEVERAGE
    bt.LEVERAGE = 2.0

    for coin in ALL_COINS:
        cfg = V11_CONFIGS[coin]
        alt_df = alt_cache[coin]

        signal, alt_merged = generate_signal_v11(
            btc_score_ts, alt_df, cfg["threshold"], cfg["alt_pa"]
        )

        trades = run_backtest(
            alt_merged, signal,
            sl_atr_mult=cfg["sl"], tp_atr_mult=cfg["tp"],
            trail_atr_mult=cfg["trail"], trail_activate_atr=cfg["trail_act"],
            max_hold_bars=96, cooldown_bars=cfg["cd"],
        )

        pnl = trades["pnl_net"].sum() if not trades.empty else 0.0
        n_trades = len(trades)
        total_pnl += pnl
        results[coin] = {"pnl": round(pnl, 2), "trades": n_trades}

    bt.LEVERAGE = old_lev
    return total_pnl, results


def run_with_alt_indicator(btc_score_ts, alt_cache, score_fn, alt_weight):
    """Run backtest with alt indicator bonus for each coin. Returns total PnL + per-coin."""
    total_pnl = 0.0
    results = {}
    old_lev = bt.LEVERAGE
    bt.LEVERAGE = 2.0

    for coin in ALL_COINS:
        cfg = V11_CONFIGS[coin]
        alt_df = alt_cache[coin]

        signal, alt_merged = generate_signal_with_alt(
            btc_score_ts, alt_df, cfg["threshold"], cfg["alt_pa"],
            alt_score_fn=score_fn, alt_weight=alt_weight,
        )

        trades = run_backtest(
            alt_merged, signal,
            sl_atr_mult=cfg["sl"], tp_atr_mult=cfg["tp"],
            trail_atr_mult=cfg["trail"], trail_activate_atr=cfg["trail_act"],
            max_hold_bars=96, cooldown_bars=cfg["cd"],
        )

        pnl = trades["pnl_net"].sum() if not trades.empty else 0.0
        n_trades = len(trades)
        total_pnl += pnl
        results[coin] = {"pnl": round(pnl, 2), "trades": n_trades}

    bt.LEVERAGE = old_lev
    return total_pnl, results


def run_with_multiple_alt_indicators(btc_score_ts, alt_cache, indicator_list):
    """
    Run backtest with multiple alt indicators stacked.
    indicator_list: list of (name, score_fn, weight) tuples.
    """
    total_pnl = 0.0
    results = {}
    old_lev = bt.LEVERAGE
    bt.LEVERAGE = 2.0

    for coin in ALL_COINS:
        cfg = V11_CONFIGS[coin]
        alt_df = alt_cache[coin]

        # Build signal with stacked indicators
        btc_score_df = btc_score_ts.reset_index()
        btc_score_df.columns = ["ts", "btc_score"]
        alt = alt_df.copy().sort_values("ts")
        alt = pd.merge_asof(alt, btc_score_df.sort_values("ts"),
                            on="ts", direction="backward",
                            tolerance=pd.Timedelta("30min"))
        alt["btc_score"] = alt["btc_score"].fillna(0)

        for _name, fn, w in indicator_list:
            alt_bonus = fn(alt, weight=w)
            alt["btc_score"] = alt["btc_score"] + alt_bonus

        signal = pd.Series(0, index=alt.index)
        signal[alt["btc_score"] >= cfg["threshold"]] = 1
        signal[alt["btc_score"] <= -cfg["threshold"]] = -1

        if cfg["alt_pa"]:
            alt_bull_pa = (alt["close"] > alt["ema9"]) & (alt["ema9"] > alt["ema21"])
            alt_bear_pa = (alt["close"] < alt["ema9"]) & (alt["ema9"] < alt["ema21"])
            alt_vol_ok = alt["vol_ratio"] > 0.8
            mask_long = (signal == 1) & ~(alt_bull_pa & alt_vol_ok)
            mask_short = (signal == -1) & ~(alt_bear_pa & alt_vol_ok)
            signal[mask_long] = 0
            signal[mask_short] = 0

        trades = run_backtest(
            alt, signal,
            sl_atr_mult=cfg["sl"], tp_atr_mult=cfg["tp"],
            trail_atr_mult=cfg["trail"], trail_activate_atr=cfg["trail_act"],
            max_hold_bars=96, cooldown_bars=cfg["cd"],
        )

        pnl = trades["pnl_net"].sum() if not trades.empty else 0.0
        n_trades = len(trades)
        total_pnl += pnl
        results[coin] = {"pnl": round(pnl, 2), "trades": n_trades}

    bt.LEVERAGE = old_lev
    return total_pnl, results


# =====================================================================
# 4) MAIN
# =====================================================================

def main():
    print("=" * 70)
    print("Alt-Specific OHLCV Indicator Test")
    print("=" * 70)
    print("Formula: effective_score = btc_score + indicator_bonus(alt_ohlcv) * weight")
    print(f"OOS period: {OOS_START} to {OOS_END}")
    print(f"Weights to test: {WEIGHTS_TO_TEST}")

    # ---- 1) Load BTC data + v3 score ----
    print("\n[1] Loading BTC data + computing v3 composite score...")
    btc_raw = fetch_binance_15m("BTCUSDT", years=3)
    db_data = load_btc_db_data_v3()
    btc_df = build_btc_features(btc_raw, db_data)
    btc_df = btc_df[btc_df["ts"] >= pd.Timestamp("2025-06-01")].copy().reset_index(drop=True)

    btc_score = compute_btc_composite_score(btc_df)
    btc_score_ts = btc_score.copy()
    btc_score_ts.index = btc_df["ts"]

    # ---- 2) Pre-load altcoin data ----
    print("\n[2] Pre-loading altcoin OHLCV data...")
    alt_cache = {}
    for coin in ALL_COINS:
        symbol = f"{coin}USDT"
        alt_raw = fetch_binance_15m(symbol, years=3)
        alt_df = build_alt_technicals(alt_raw)
        alt_df = alt_df[(alt_df["ts"] >= OOS_START) & (alt_df["ts"] <= OOS_END)].copy()
        alt_df = alt_df.reset_index(drop=True)
        alt_cache[coin] = alt_df
        print(f"  {coin}: {len(alt_df):,} bars")

    # ---- 3) Baseline ----
    print("\n[3] Running v3 baseline (2x leverage)...")
    baseline_pnl, baseline_results = run_v3_baseline(btc_score_ts, alt_cache)
    print(f"\n  V3 BASELINE: ${baseline_pnl:,.0f}")
    for coin, r in baseline_results.items():
        print(f"    {coin}: ${r['pnl']:,.0f} ({r['trades']} trades)")

    # ---- 4) Phase A: Individual indicator tests ----
    print(f"\n{'='*70}")
    print("[4] PHASE A: Individual Indicator Tests")
    print(f"{'='*70}")

    INDICATORS = [
        ("rsi_contrarian",      score_rsi_contrarian),
        ("bollinger_contrarian", score_bollinger_contrarian),
        ("mean_reversion",      score_mean_reversion),
        ("volume_spike",        score_volume_spike),
        ("momentum_roc",        score_momentum_roc),
        ("ema_trend",           score_ema_trend),
    ]

    all_tests = []

    for ind_name, score_fn in INDICATORS:
        print(f"\n  --- {ind_name} ---")
        for w in WEIGHTS_TO_TEST:
            pnl, per_coin = run_with_alt_indicator(btc_score_ts, alt_cache, score_fn, w)
            delta = pnl - baseline_pnl
            marker = " ***" if delta > 200 else ""
            print(f"    w={w:.2f}: ${pnl:,.0f} (delta={delta:+,.0f}, "
                  f"trades={sum(r['trades'] for r in per_coin.values())}){marker}")

            all_tests.append({
                "indicator": ind_name,
                "weight": w,
                "pnl": round(pnl, 2),
                "delta": round(delta, 2),
                "per_coin": per_coin,
            })

    # ---- 5) Summary table ----
    print(f"\n{'='*70}")
    print("[5] PHASE A SUMMARY")
    print(f"{'='*70}")
    print(f"\nV3 Baseline: ${baseline_pnl:,.0f}")
    print(f"\n{'Indicator':<24} {'Best W':>6} {'PnL':>10} {'Delta':>10} {'Verdict':>12}")
    print("-" * 65)

    # Find best weight per indicator
    indicator_best = {}
    for t in all_tests:
        name = t["indicator"]
        if name not in indicator_best or t["delta"] > indicator_best[name]["delta"]:
            indicator_best[name] = t

    for name, best in sorted(indicator_best.items(), key=lambda x: -x[1]["delta"]):
        verdict = "KEEP" if best["delta"] > 200 else "MARGINAL" if best["delta"] > 0 else "SKIP"
        print(f"  {name:<22} {best['weight']:>6.2f} ${best['pnl']:>9,.0f} "
              f"{best['delta']:>+9,.0f} {verdict:>12}")

    # ---- Per-coin breakdown for best configs ----
    print(f"\n{'='*70}")
    print("Per-Coin Breakdown (best weight per indicator)")
    print(f"{'='*70}")

    header = f"  {'Indicator':<22}"
    for coin in ALL_COINS:
        header += f" {coin:>8}"
    print(header)
    print("  " + "-" * (22 + 9 * len(ALL_COINS)))

    # Baseline row
    row = f"  {'[baseline]':<22}"
    for coin in ALL_COINS:
        row += f" ${baseline_results[coin]['pnl']:>6,.0f}"
    print(row)

    for name, best in sorted(indicator_best.items(), key=lambda x: -x[1]["delta"]):
        row = f"  {name:<22}"
        for coin in ALL_COINS:
            coin_pnl = best["per_coin"][coin]["pnl"]
            base_pnl = baseline_results[coin]["pnl"]
            d = coin_pnl - base_pnl
            sign = "+" if d >= 0 else ""
            row += f" {sign}{d:>6,.0f}"
        print(row)

    # ---- 6) Phase B: Stepwise build ----
    print(f"\n{'='*70}")
    print("[6] PHASE B: Stepwise Build")
    print(f"{'='*70}")

    # Sort candidates by delta descending, only keep positive
    candidates = sorted(
        [(name, best) for name, best in indicator_best.items() if best["delta"] > 0],
        key=lambda x: -x[1]["delta"]
    )

    if not candidates:
        print("\n  No indicators improved PnL. Stepwise build skipped.")
        stepwise_result = None
    else:
        print(f"\n  Candidates (sorted by individual delta):")
        for name, best in candidates:
            print(f"    {name}: delta={best['delta']:+,.0f} (w={best['weight']:.2f})")

        selected = []
        current_pnl = baseline_pnl

        for name, best in candidates:
            score_fn = dict(INDICATORS)[name]
            w = best["weight"]

            trial = selected + [(name, score_fn, w)]
            trial_pnl, trial_per_coin = run_with_multiple_alt_indicators(
                btc_score_ts, alt_cache, trial
            )
            delta_from_current = trial_pnl - current_pnl

            if delta_from_current > 0:
                selected.append((name, score_fn, w))
                current_pnl = trial_pnl
                print(f"\n  + {name} (w={w:.2f}): ${trial_pnl:,.0f} "
                      f"(+${delta_from_current:,.0f}) -> ADDED")
                for coin in ALL_COINS:
                    print(f"      {coin}: ${trial_per_coin[coin]['pnl']:,.0f} "
                          f"({trial_per_coin[coin]['trades']} trades)")
            else:
                print(f"\n  x {name} (w={w:.2f}): ${trial_pnl:,.0f} "
                      f"({delta_from_current:+,.0f}) -> SKIP (hurts when combined)")

        stepwise_result = {
            "selected": [(name, w) for name, _, w in selected],
            "final_pnl": round(current_pnl, 2),
            "delta_vs_baseline": round(current_pnl - baseline_pnl, 2),
        }

    # ---- 7) Final summary ----
    print(f"\n{'='*70}")
    print("FINAL RESULTS")
    print(f"{'='*70}")
    print(f"\n  V3 Baseline:  ${baseline_pnl:,.0f}")

    if stepwise_result and stepwise_result["selected"]:
        print(f"  Stepwise:     ${stepwise_result['final_pnl']:,.0f} "
              f"(+${stepwise_result['delta_vs_baseline']:,.0f}, "
              f"+{stepwise_result['delta_vs_baseline']/abs(baseline_pnl)*100:.1f}%)")
        print(f"  Selected indicators:")
        for name, w in stepwise_result["selected"]:
            print(f"    - {name} (w={w:.2f})")
    else:
        print(f"  Stepwise:     No improvement found")

    print(f"\n  Phase A individual results:")
    for name, best in sorted(indicator_best.items(), key=lambda x: -x[1]["delta"]):
        pct = best["delta"] / abs(baseline_pnl) * 100 if baseline_pnl != 0 else 0
        print(f"    {name:<22}: {best['delta']:+,.0f} ({pct:+.1f}%) at w={best['weight']:.2f}")

    # ---- 8) Save experiment ----
    os.makedirs("experiments", exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    output = {
        "timestamp": ts,
        "description": "Alt-specific OHLCV indicator test on v3 baseline",
        "oos_period": f"{OOS_START} to {OOS_END}",
        "baseline_pnl": round(baseline_pnl, 2),
        "baseline_per_coin": baseline_results,
        "phase_a_tests": all_tests,
        "phase_a_best": {k: {"indicator": k, "weight": v["weight"],
                              "pnl": v["pnl"], "delta": v["delta"],
                              "per_coin": v["per_coin"]}
                         for k, v in indicator_best.items()},
        "phase_b_stepwise": stepwise_result,
    }
    outfile = f"experiments/alt_indicator_test_{ts}.json"
    with open(outfile, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\n  Results saved to {outfile}")

    print(f"\n{'='*70}")
    print("Done!")
    print(f"{'='*70}")


if __name__ == "__main__":
    main()
