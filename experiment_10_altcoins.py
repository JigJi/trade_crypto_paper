"""
Experiment: BTC-Led Strategy on 10 Additional Altcoins
=======================================================
6-month backtest: Train Sep-Dec 2025, Test Jan-Mar 2026 (OOS)
"""

import os, sys, warnings, time as _time
from datetime import datetime

import numpy as np
import pandas as pd
import pandas_ta as ta

warnings.filterwarnings("ignore")

from backtest_15m_btc_led_alts import (
    fetch_binance_15m, load_btc_db_data, build_btc_features,
    compute_btc_composite_score, build_alt_technicals,
    generate_btc_led_signal, run_backtest, calc_metrics,
    INIT_EQUITY, BUDGET_USDT, LEVERAGE, FEE, SLIP, FEE_BPS, SLIP_BPS
)

# 10 new altcoins to test
ALT_COINS = ["DOT", "NEAR", "FIL", "APT", "ARB", "OP", "ATOM", "SUI", "INJ", "PEPE"]


def grid_search_coin(coin_name, btc_score_ts, alt_df, split_date):
    """Grid search on train, validate on test."""
    alt_train = alt_df[alt_df["ts"] < split_date].reset_index(drop=True)
    alt_test = alt_df[alt_df["ts"] >= split_date].reset_index(drop=True)

    if len(alt_train) < 100 or len(alt_test) < 100:
        print(f"  {coin_name}: Insufficient data (train={len(alt_train)}, test={len(alt_test)})")
        return None

    print(f"  Train={len(alt_train):,} bars | Test={len(alt_test):,} bars")
    bh_train = (alt_train["close"].iloc[-1] / alt_train["close"].iloc[0] - 1) * 100
    bh_test = (alt_test["close"].iloc[-1] / alt_test["close"].iloc[0] - 1) * 100
    print(f"  B&H: Train {bh_train:+.1f}% | Test {bh_test:+.1f}%")

    thresholds = [2.0, 2.5, 3.0, 3.5, 4.0]
    sl_mults = [1.5, 2.0, 2.5]
    tp_mults = [3.0, 4.0]
    trail_mults = [0.5, 0.8, 1.0, 99]
    cooldowns = [4, 8]
    alt_pa_filters = [True, False]

    best = None
    best_pnl = -999999
    count = 0
    total = len(thresholds) * len(sl_mults) * len(tp_mults) * len(trail_mults) * len(cooldowns) * len(alt_pa_filters)

    for thr in thresholds:
        for use_pa in alt_pa_filters:
            signals, alt_merged = generate_btc_led_signal(btc_score_ts, alt_train, thr, use_pa)
            n_sig = (signals != 0).sum()
            if n_sig < 5:
                count += len(sl_mults) * len(tp_mults) * len(trail_mults) * len(cooldowns)
                continue

            for sl_m in sl_mults:
                for tp_m in tp_mults:
                    if tp_m < sl_m * 1.5:
                        count += len(trail_mults) * len(cooldowns)
                        continue
                    for tr_m in trail_mults:
                        for cd in cooldowns:
                            ta_val = 0.5 if tr_m <= 0.5 else 0.8 if tr_m <= 1.0 else 1.5 if tr_m < 50 else 99
                            trades = run_backtest(alt_merged, signals,
                                                  sl_atr_mult=sl_m, tp_atr_mult=tp_m,
                                                  trail_atr_mult=tr_m, trail_activate_atr=ta_val,
                                                  cooldown_bars=cd)
                            m = calc_metrics(trades, len(alt_train))
                            count += 1
                            if m["total"] >= 5 and m["net_pnl"] > best_pnl:
                                best_pnl = m["net_pnl"]
                                best = {"threshold": thr, "alt_pa": use_pa,
                                        "sl": sl_m, "tp": tp_m,
                                        "trail": tr_m, "trail_act": ta_val, "cd": cd, **m}

    print(f"  Grid: {count}/{total} combos searched")

    if best is None:
        print(f"  => NO VALID RESULT")
        return None

    print(f"  Best train: thr={best['threshold']} pa={best['alt_pa']} "
          f"SL={best['sl']} TP={best['tp']} tr={best['trail']} cd={best['cd']}")
    print(f"    Train: {best['total']} trades, WR={best['win_rate']:.1f}%, "
          f"PF={best['pf']:.3f}, PnL=${best['net_pnl']:+,.2f}")

    # Validate on OOS
    signals_test, alt_merged_test = generate_btc_led_signal(
        btc_score_ts, alt_test, best["threshold"], best["alt_pa"])
    trades_test = run_backtest(alt_merged_test, signals_test,
                               sl_atr_mult=best["sl"], tp_atr_mult=best["tp"],
                               trail_atr_mult=best["trail"], trail_activate_atr=best["trail_act"],
                               cooldown_bars=best["cd"])
    m_test = calc_metrics(trades_test, len(alt_test))

    status = "PROFITABLE" if m_test["net_pnl"] > 0 else "NOT PROFITABLE"
    print(f"    OOS:   {m_test['total']} trades, WR={m_test['win_rate']:.1f}%, "
          f"PF={m_test['pf']:.3f}, PnL=${m_test['net_pnl']:+,.2f}, "
          f"Sharpe={m_test['sharpe']:.3f} => {status}")

    # Full period too
    signals_full, alt_merged_full = generate_btc_led_signal(
        btc_score_ts, alt_df, best["threshold"], best["alt_pa"])
    trades_full = run_backtest(alt_merged_full, signals_full,
                               sl_atr_mult=best["sl"], tp_atr_mult=best["tp"],
                               trail_atr_mult=best["trail"], trail_activate_atr=best["trail_act"],
                               cooldown_bars=best["cd"])
    m_full = calc_metrics(trades_full, len(alt_df))

    return {
        "coin": coin_name,
        "config": best,
        "train": {k: v for k, v in best.items() if k in [
            "total", "win_rate", "pf", "net_pnl", "sharpe", "max_dd", "rr",
            "avg_pnl", "avg_win", "avg_loss", "n_long", "n_short", "exposure"]},
        "test": m_test,
        "full": m_full,
        "bh_train": bh_train,
        "bh_test": bh_test,
        "trades_test": trades_test,
        "trades_full": trades_full,
    }


def main():
    print("=" * 70)
    print("BTC-Led Strategy: 10 Altcoin Scan")
    print(f"Coins: {', '.join(ALT_COINS)}")
    print(f"Period: Sep 2025 - Mar 2026 (6 months)")
    print(f"Train: Sep-Dec 2025 | Test: Jan-Mar 2026 (OOS)")
    print("=" * 70)

    # Build BTC composite score
    print("\n[PHASE 1] BTC Composite Score...")
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

    btc_period_start = pd.Timestamp("2025-09-01")
    mask = btc_df["ts"] >= btc_period_start
    btc_df_trimmed = btc_df[mask].reset_index(drop=True)
    btc_score_trimmed = btc_score[mask].reset_index(drop=True)
    btc_period_end = btc_df_trimmed["ts"].iloc[-1]
    btc_score_ts = pd.Series(btc_score_trimmed.values, index=btc_df_trimmed["ts"].values)

    print(f"  Period: {btc_period_start} to {btc_period_end}")
    print(f"  Score range: [{btc_score_trimmed.min():.1f}, {btc_score_trimmed.max():.1f}]")

    split_date = pd.Timestamp("2026-01-01")

    # Run each coin
    print(f"\n[PHASE 2] Scanning {len(ALT_COINS)} Altcoins...")
    all_results = []

    for i, coin in enumerate(ALT_COINS, 1):
        symbol = f"{coin}USDT"
        print(f"\n{'-'*70}")
        print(f"[{i}/{len(ALT_COINS)}] {coin} ({symbol})")
        print(f"{'-'*70}")

        try:
            ohlcv = fetch_binance_15m(symbol, years=3)
            print(f"  OHLCV: {len(ohlcv):,} candles ({ohlcv['date_time'].iloc[0]} to {ohlcv['date_time'].iloc[-1]})")

            alt_df = build_alt_technicals(ohlcv)
            alt_df = alt_df[(alt_df["ts"] >= btc_period_start) & (alt_df["ts"] <= btc_period_end)]
            alt_df = alt_df.reset_index(drop=True)

            if len(alt_df) < 200:
                print(f"  SKIP: Only {len(alt_df)} bars in period")
                all_results.append(None)
                continue

            result = grid_search_coin(coin, btc_score_ts, alt_df, split_date)
            all_results.append(result)

        except Exception as e:
            print(f"  ERROR: {e}")
            import traceback
            traceback.print_exc()
            all_results.append(None)

    # ================================================================
    # FINAL REPORT
    # ================================================================
    print(f"\n{'='*70}")
    print("FINAL REPORT: 10 Altcoin BTC-Led Scan")
    print(f"{'='*70}")

    # Sort by OOS PnL
    valid = [(r["coin"], r) for r in all_results if r is not None]
    valid.sort(key=lambda x: x[1]["test"]["net_pnl"], reverse=True)

    print(f"\n{'Coin':<6} {'OOS PnL':>10} {'Sharpe':>8} {'PF':>7} {'WR':>7} {'Trades':>7} "
          f"{'Train PnL':>10} {'B&H Test':>9} {'Config'}")
    print("-" * 110)

    profitable = []
    not_profitable = []

    for coin, r in valid:
        t = r["test"]
        tr = r["train"]
        c = r["config"]
        status = "+" if t["net_pnl"] > 0 else "-"

        print(f"  {status} {coin:<4} ${t['net_pnl']:>+8,.2f}  {t['sharpe']:>7.3f} "
              f"{t['pf']:>6.3f} {t['win_rate']:>5.1f}% {t['total']:>6}  "
              f"${tr['net_pnl']:>+8,.2f}  {r['bh_test']:>+7.1f}%  "
              f"thr={c['threshold']} pa={c['alt_pa']} SL={c['sl']} TP={c['tp']} tr={c['trail']} cd={c['cd']}")

        if t["net_pnl"] > 0:
            profitable.append((coin, r))
        else:
            not_profitable.append((coin, r))

    failed = [ALT_COINS[i] for i, r in enumerate(all_results) if r is None]
    if failed:
        for coin in failed:
            print(f"  - {coin:<4}   FAILED / NO VALID RESULT")

    # Summary
    print(f"\n{'='*70}")
    print(f"PROFITABLE OOS ({len(profitable)}/{len(valid)}):")
    for coin, r in profitable:
        t = r["test"]
        print(f"  {coin}: ${t['net_pnl']:+,.2f} (Sharpe {t['sharpe']:.3f}, PF {t['pf']:.3f})")

    print(f"\nNOT PROFITABLE OOS ({len(not_profitable)}/{len(valid)}):")
    for coin, r in not_profitable:
        t = r["test"]
        print(f"  {coin}: ${t['net_pnl']:+,.2f} (Sharpe {t['sharpe']:.3f})")

    # Combined with existing portfolio
    print(f"\n{'='*70}")
    print("COMBINED LEADERBOARD (All Coins Tested)")
    print(f"{'='*70}")

    existing = [
        ("BTC",  +68.42,  0.994, "ORIGINAL"),
        ("XRP",  +311.74, 1.731, "ORIGINAL"),
        ("ADA",  +244.02, 1.432, "ORIGINAL"),
        ("SOL",  +213.80, 1.078, "PREV EXP"),
        ("ETH",  -3.26,  -0.027, "PREV EXP"),
        ("DOGE", -1.69,  -0.006, "PREV EXP"),
        ("AVAX", -126.14, 0.0,   "ORIGINAL"),
        ("LINK", -136.67, 0.0,   "ORIGINAL"),
    ]

    for coin, r in valid:
        existing.append((coin, r["test"]["net_pnl"], r["test"]["sharpe"], "NEW"))

    existing.sort(key=lambda x: x[1], reverse=True)

    print(f"\n{'Rank':<5} {'Coin':<6} {'OOS PnL':>10} {'Sharpe':>8} {'Source':<10} {'Status'}")
    print("-" * 55)
    for i, (coin, pnl, sharpe, src) in enumerate(existing, 1):
        status = "PASS" if pnl > 0 else "FAIL"
        marker = " <<<" if src == "NEW" and pnl > 0 else ""
        print(f"  {i:<4} {coin:<5} ${pnl:>+8,.2f}  {sharpe:>7.3f}  {src:<10} {status}{marker}")

    n_pass = sum(1 for _, pnl, _, _ in existing if pnl > 0)
    print(f"\nTotal: {n_pass}/{len(existing)} coins profitable OOS")
    print("\nDone!")


if __name__ == "__main__":
    main()
