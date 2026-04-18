"""
Controlled comparison: v1.1 vs v2 enhancements
Same test period (Jan-Mar 2026), toggle enhancements one at a time.
"""
import os, sys, warnings
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

import backtest_15m_btc_led_alts as bt
from backtest_15m_btc_led_alts import (
    fetch_binance_15m, load_btc_db_data, build_btc_features,
    compute_btc_composite_score, build_alt_technicals,
    run_backtest, calc_metrics,
    BKK_UTC_OFFSET, INIT_EQUITY, FEE, SLIP,
)

# v1.1 configs (from paper_trading/config.py - current production)
V1_CONFIGS = {
    "BTC": {"threshold": 2.5, "alt_pa": False, "sl": 2.5, "tp": 4.0, "trail": 99, "trail_act": 99, "cd": 4},
    "XRP": {"threshold": 3.5, "alt_pa": False, "sl": 2.5, "tp": 4.0, "trail": 99, "trail_act": 99, "cd": 4},
    "ADA": {"threshold": 3.5, "alt_pa": False, "sl": 2.5, "tp": 4.0, "trail": 99, "trail_act": 99, "cd": 4},
    "DOT": {"threshold": 3.0, "alt_pa": False, "sl": 2.5, "tp": 4.0, "trail": 99, "trail_act": 99, "cd": 8},
    "SUI": {"threshold": 3.0, "alt_pa": False, "sl": 2.5, "tp": 4.0, "trail": 99, "trail_act": 99, "cd": 4},
    "FIL": {"threshold": 3.0, "alt_pa": False, "sl": 2.5, "tp": 4.0, "trail": 99, "trail_act": 99, "cd": 4},
}

# v2 configs (from grid search with Jun-Nov training)
V2_CONFIGS = {
    "BTC": {"threshold": 3.0, "alt_pa": False, "sl": 2.5, "tp": 4.0, "trail": 99, "trail_act": 99, "cd": 4},
    "XRP": {"threshold": 3.5, "alt_pa": False, "sl": 2.5, "tp": 4.0, "trail": 99, "trail_act": 99, "cd": 4},
    "ADA": {"threshold": 3.0, "alt_pa": False, "sl": 2.0, "tp": 4.0, "trail": 99, "trail_act": 99, "cd": 4},
    "DOT": {"threshold": 2.0, "alt_pa": True,  "sl": 1.5, "tp": 3.0, "trail": 0.5, "trail_act": 0.5, "cd": 4},
    "SUI": {"threshold": 2.0, "alt_pa": True,  "sl": 2.5, "tp": 4.0, "trail": 99, "trail_act": 99, "cd": 4},
    "FIL": {"threshold": 2.0, "alt_pa": False, "sl": 2.5, "tp": 4.0, "trail": 99, "trail_act": 99, "cd": 4},
}

COINS = ["BTC", "XRP", "ADA", "DOT", "SUI", "FIL"]
TEST_START = pd.Timestamp("2026-01-01")


def generate_signal_no_deadzone(btc_score_ts, alt_df, threshold, use_alt_pa):
    """Generate signal WITHOUT dead zone filter (v1.1 behavior)."""
    btc_score_df = btc_score_ts.reset_index()
    btc_score_df.columns = ["ts", "btc_score"]
    alt = alt_df.copy().sort_values("ts")
    alt = pd.merge_asof(alt, btc_score_df.sort_values("ts"),
                        on="ts", direction="backward", tolerance=pd.Timedelta("30min"))
    alt["btc_score"] = alt["btc_score"].fillna(0)
    signal = pd.Series(0, index=alt.index)
    bull_cond = alt["btc_score"] >= threshold
    bear_cond = alt["btc_score"] <= -threshold
    if use_alt_pa:
        alt_bull_pa = (alt["close"] > alt["ema9"]) & (alt["ema9"] > alt["ema21"])
        alt_bear_pa = (alt["close"] < alt["ema9"]) & (alt["ema9"] < alt["ema21"])
        alt_vol_ok = alt["vol_ratio"] > 0.8
        signal[bull_cond & alt_bull_pa & alt_vol_ok] = 1
        signal[bear_cond & alt_bear_pa & alt_vol_ok] = -1
    else:
        signal[bull_cond] = 1
        signal[bear_cond] = -1
    return signal, alt


def generate_signal_with_deadzone(btc_score_ts, alt_df, threshold, use_alt_pa):
    """Generate signal WITH dead zone filter (v2 behavior)."""
    signal, alt = generate_signal_no_deadzone(btc_score_ts, alt_df, threshold, use_alt_pa)
    hour = alt["ts"].dt.hour
    is_dead = (hour >= 23) | (hour < 6)
    signal[is_dead] = 0
    return signal, alt


def run_single(coin, btc_score_ts, alt_test, cfg, use_dead_zone, leverage):
    """Run one coin on test data with specific config."""
    if use_dead_zone:
        signals, alt_merged = generate_signal_with_deadzone(
            btc_score_ts, alt_test, cfg["threshold"], cfg["alt_pa"])
    else:
        signals, alt_merged = generate_signal_no_deadzone(
            btc_score_ts, alt_test, cfg["threshold"], cfg["alt_pa"])

    old_lev = bt.LEVERAGE
    bt.LEVERAGE = leverage
    trades = run_backtest(alt_merged, signals,
                          sl_atr_mult=cfg["sl"], tp_atr_mult=cfg["tp"],
                          trail_atr_mult=cfg["trail"], trail_activate_atr=cfg["trail_act"],
                          cooldown_bars=cfg["cd"])
    m = calc_metrics(trades, len(alt_test))
    bt.LEVERAGE = old_lev
    return m


def main():
    print("=" * 70)
    print("CONTROLLED COMPARISON: v1.1 vs v2")
    print("Test period: Jan 1 - Mar 8, 2026 (SAME for all scenarios)")
    print("=" * 70)

    print("\nLoading BTC data...")
    btc_ohlcv = fetch_binance_15m("BTCUSDT", years=3)
    btc_db = load_btc_db_data()
    btc_df = build_btc_features(btc_ohlcv, btc_db)

    w = {
        "w_oi_bull": 0.5, "w_oi_capit": 0.5, "w_oi_weak": 0.5, "w_oi_bear": 0.5,
        "w_taker_strong": 1.5, "w_taker_mild": 0.5, "w_ls_extreme": 1.5,
        "w_fr_neg": 2.0, "w_fr_pos": 2.0, "w_fg_fear": 2.0, "w_fg_mild_fear": 1.0,
        "w_fg_greed": 2.0, "w_fg_mild_greed": 1.0, "w_whale_bull": 1.5, "w_whale_bear": 1.5,
        "w_liq_bull": 2.0, "w_liq_bear": 2.0, "w_etf_bull": 1.5, "w_etf_bear": 1.5,
    }
    btc_score = compute_btc_composite_score(btc_df, w)

    btc_period_start = pd.Timestamp("2025-06-01")
    mask = btc_df["ts"] >= btc_period_start
    btc_df_trimmed = btc_df[mask].reset_index(drop=True)
    btc_score_trimmed = btc_score[mask].reset_index(drop=True)
    btc_score_ts = pd.Series(btc_score_trimmed.values, index=btc_df_trimmed["ts"].values)
    btc_period_end = btc_df_trimmed["ts"].iloc[-1]

    # 4 scenarios - toggle one thing at a time
    scenarios = [
        ("A: v1.1 baseline (1x, no DZ)", V1_CONFIGS, False, 1.0),
        ("B: v1.1 + dead zone (1x)",     V1_CONFIGS, True,  1.0),
        ("C: v2 new params + DZ (1x)",   V2_CONFIGS, True,  1.0),
        ("D: v2 new params + DZ (2x)",   V2_CONFIGS, True,  2.0),
    ]

    all_results = {}
    for scenario_name, configs, use_dz, lev in scenarios:
        print(f"\n--- {scenario_name} ---")
        coin_results = {}
        total_pnl = 0
        for coin in COINS:
            symbol = f"{coin}USDT"
            ohlcv = fetch_binance_15m(symbol, years=3)
            alt_df = build_alt_technicals(ohlcv)
            alt_test = alt_df[(alt_df["ts"] >= TEST_START) & (alt_df["ts"] <= btc_period_end)].reset_index(drop=True)
            if len(alt_test) < 100:
                print(f"  {coin}: insufficient data")
                continue
            m = run_single(coin, btc_score_ts, alt_test, configs[coin], use_dz, lev)
            coin_results[coin] = m
            total_pnl += m["net_pnl"]
            print(f"  {coin:5s}: {m['total']:3d} trades, WR={m['win_rate']:5.1f}%, PnL=${m['net_pnl']:>+9,.2f}")
        coin_results["TOTAL"] = total_pnl
        all_results[scenario_name] = coin_results
        print(f"  TOTAL: ${total_pnl:>+10,.2f}")

    # ============================================================
    # Final summary
    # ============================================================
    baseline = all_results["A: v1.1 baseline (1x, no DZ)"]["TOTAL"]

    print(f"\n{'='*70}")
    print("PER-COIN COMPARISON (Jan-Mar 2026)")
    print(f"{'='*70}")
    header = f"{'Coin':<6s}"
    for name, _, _, _ in scenarios:
        short = name.split(":")[0]
        header += f" | {short:>12s}"
    print(header)
    print("-" * 65)
    for coin in COINS:
        row = f"{coin:<6s}"
        for name, _, _, _ in scenarios:
            pnl = all_results[name].get(coin, {})
            if isinstance(pnl, dict):
                row += f" | ${pnl['net_pnl']:>+9,.2f}"
            else:
                row += f" | {'N/A':>12s}"
        print(row)
    row = f"{'TOTAL':<6s}"
    for name, _, _, _ in scenarios:
        row += f" | ${all_results[name]['TOTAL']:>+9,.2f}"
    print(row)

    print(f"\n{'='*70}")
    print("ENHANCEMENT IMPACT BREAKDOWN")
    print(f"{'='*70}")
    a = all_results["A: v1.1 baseline (1x, no DZ)"]["TOTAL"]
    b = all_results["B: v1.1 + dead zone (1x)"]["TOTAL"]
    c = all_results["C: v2 new params + DZ (1x)"]["TOTAL"]
    d = all_results["D: v2 new params + DZ (2x)"]["TOTAL"]

    abs_a = abs(a) if a != 0 else 1
    print(f"  Baseline (v1.1, 1x, no DZ):      ${a:>+10,.2f}")
    print(f"  + Dead zone filter:               ${b-a:>+10,.2f}  ({(b-a)/abs_a*100:>+6.1f}%)")
    print(f"  + New grid-searched params:        ${c-b:>+10,.2f}  ({(c-b)/abs_a*100:>+6.1f}%)")
    print(f"  + 2x leverage:                     ${d-c:>+10,.2f}  ({(d-c)/abs_a*100:>+6.1f}%)")
    print(f"  -----------------------------------------------")
    print(f"  Total (v2 full):                  ${d:>+10,.2f}  ({(d-a)/abs_a*100:>+6.1f}%)")
    print(f"\n  Verdict: v2 {'BETTER' if d > a else 'WORSE'} than v1.1 by ${abs(d-a):,.2f}")


if __name__ == "__main__":
    main()
