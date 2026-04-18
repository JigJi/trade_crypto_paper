"""
Experiment: BTC-Led Extended Tests
====================================
1. Test ETH + SOL with BTC-led strategy (grid search)
2. Dynamic SL/TP based on BTC score strength
3. DOGE revisit with relaxed params

Reuses core functions from backtest_15m_btc_led_alts.py
"""

import os, sys, warnings
from datetime import datetime

import numpy as np
import pandas as pd
import pandas_ta as ta

warnings.filterwarnings("ignore")

# Import core functions from existing backtest
from backtest_15m_btc_led_alts import (
    fetch_binance_15m, load_btc_db_data, build_btc_features,
    compute_btc_composite_score, build_alt_technicals,
    generate_btc_led_signal, calc_metrics,
    INIT_EQUITY, BUDGET_USDT, LEVERAGE, FEE, SLIP, FEE_BPS, SLIP_BPS
)

# ---- Dynamic SL/TP backtest engine ----

def run_backtest_dynamic_sltp(df, signals, btc_scores,
                               base_sl=2.0, base_tp=3.0,
                               sl_scale_factor=0.15, tp_scale_factor=0.2,
                               trail_atr_mult=0.5, trail_activate_atr=0.5,
                               max_hold_bars=96, cooldown_bars=4):
    """
    Like run_backtest but SL/TP multipliers scale with BTC score strength.

    When |btc_score| is higher (stronger signal), we:
    - Tighten SL slightly (more confident, accept tighter stop)
    - Widen TP (let winners run on strong signals)

    Formula:
      score_excess = |btc_score| - threshold  (always >= 0 at entry)
      sl_mult = base_sl - sl_scale_factor * score_excess  (tighter SL)
      tp_mult = base_tp + tp_scale_factor * score_excess  (wider TP)

    Clamped to reasonable ranges.
    """
    sig = signals.shift(1).fillna(0).astype(int).values
    atrs = df["atr"].values
    opens = df["open"].values
    highs = df["high"].values
    lows = df["low"].values
    closes = df["close"].values
    times = df["ts"].values

    # Get btc_score aligned to df
    scores = df["btc_score"].values if "btc_score" in df.columns else np.zeros(len(df))

    n = len(df)
    records = []
    equity = INIT_EQUITY
    position = 0
    entry_i = entry_px = entry_atr = qty = fee_in = 0
    peak = trough = 0.0
    trl_active = False
    last_exit_i = -cooldown_bars - 1
    sl_atr_mult = base_sl
    tp_atr_mult = base_tp

    for i in range(n):
        if position == 0 and sig[i] != 0 and (i - last_exit_i) > cooldown_bars:
            raw_px = opens[i]
            cur_atr = atrs[i] if not np.isnan(atrs[i]) else 0
            if raw_px <= 0 or cur_atr <= 0 or cur_atr / raw_px < 0.0005:
                continue

            # Dynamic SL/TP based on score strength
            score_abs = abs(scores[i]) if not np.isnan(scores[i]) else 0
            score_excess = max(0, score_abs - 2.0)  # excess above minimum threshold
            sl_atr_mult = max(1.0, base_sl - sl_scale_factor * score_excess)
            tp_atr_mult = min(6.0, base_tp + tp_scale_factor * score_excess)

            qty = (BUDGET_USDT * LEVERAGE) / raw_px
            entry_px = raw_px * (1 + SLIP) if sig[i] == 1 else raw_px * (1 - SLIP)
            entry_atr = cur_atr
            fee_in = entry_px * qty * FEE
            position = sig[i]
            entry_i = i
            peak = entry_px
            trough = entry_px
            trl_active = False
            continue

        if position != 0:
            h, l, c, o = highs[i], lows[i], closes[i], opens[i]
            atr = entry_atr
            if position == 1:
                peak = max(peak, h)
                sl_level = entry_px - sl_atr_mult * atr
                tp_level = entry_px + tp_atr_mult * atr
            else:
                trough = min(trough, l)
                sl_level = entry_px + sl_atr_mult * atr
                tp_level = entry_px - tp_atr_mult * atr

            trail_stop = None
            if trail_atr_mult < 50:
                if position == 1 and (peak - entry_px) >= trail_activate_atr * atr:
                    trl_active = True
                    trail_stop = peak - trail_atr_mult * atr
                elif position == -1 and (entry_px - trough) >= trail_activate_atr * atr:
                    trl_active = True
                    trail_stop = trough + trail_atr_mult * atr

            exit_px = exit_reason = None
            if position == 1:
                if l <= sl_level: exit_px, exit_reason = sl_level, "SL"
                elif trl_active and trail_stop and l <= trail_stop: exit_px, exit_reason = trail_stop, "TRAIL"
                elif h >= tp_level: exit_px, exit_reason = tp_level, "TP"
            else:
                if h >= sl_level: exit_px, exit_reason = sl_level, "SL"
                elif trl_active and trail_stop and h >= trail_stop: exit_px, exit_reason = trail_stop, "TRAIL"
                elif l <= tp_level: exit_px, exit_reason = tp_level, "TP"

            if exit_px is None and (i - entry_i) >= max_hold_bars:
                exit_px, exit_reason = c, "TIMEOUT"
            if exit_px is None and sig[i] != 0 and sig[i] != position:
                exit_px, exit_reason = o, "SIGNAL_FLIP"

            if exit_px is not None:
                exit_px_f = exit_px * (1 - SLIP) if position == 1 else exit_px * (1 + SLIP)
                fee_out = exit_px_f * qty * FEE
                pnl_gross = (exit_px_f - entry_px) * qty * position
                pnl_net = pnl_gross - fee_in - fee_out
                equity += pnl_net
                records.append({
                    "entry_idx": entry_i, "exit_idx": i,
                    "entry_time": times[entry_i], "exit_time": times[i],
                    "dir": "L" if position == 1 else "S",
                    "entry_price": entry_px, "exit_price": exit_px_f,
                    "qty": qty, "pnl_net": pnl_net,
                    "equity_after": equity, "exit_reason": exit_reason,
                    "holding_bars": i - entry_i,
                    "sl_mult_used": sl_atr_mult, "tp_mult_used": tp_atr_mult,
                })
                last_exit_i = i
                position = 0

    return pd.DataFrame(records)


# ---- Standard backtest (copied for convenience) ----

def run_backtest(df, signals, sl_atr_mult=2.0, tp_atr_mult=3.0,
                 trail_atr_mult=0.5, trail_activate_atr=0.5,
                 max_hold_bars=96, cooldown_bars=4):
    sig = signals.shift(1).fillna(0).astype(int).values
    atrs = df["atr"].values
    opens = df["open"].values
    highs = df["high"].values
    lows = df["low"].values
    closes = df["close"].values
    times = df["ts"].values

    n = len(df)
    records = []
    equity = INIT_EQUITY
    position = 0
    entry_i = entry_px = entry_atr = qty = fee_in = 0
    peak = trough = 0.0
    trl_active = False
    last_exit_i = -cooldown_bars - 1

    for i in range(n):
        if position == 0 and sig[i] != 0 and (i - last_exit_i) > cooldown_bars:
            raw_px = opens[i]
            cur_atr = atrs[i] if not np.isnan(atrs[i]) else 0
            if raw_px <= 0 or cur_atr <= 0 or cur_atr / raw_px < 0.0005:
                continue
            qty = (BUDGET_USDT * LEVERAGE) / raw_px
            entry_px = raw_px * (1 + SLIP) if sig[i] == 1 else raw_px * (1 - SLIP)
            entry_atr = cur_atr
            fee_in = entry_px * qty * FEE
            position = sig[i]
            entry_i = i
            peak = entry_px
            trough = entry_px
            trl_active = False
            continue

        if position != 0:
            h, l, c, o = highs[i], lows[i], closes[i], opens[i]
            atr = entry_atr
            if position == 1:
                peak = max(peak, h)
                sl_level = entry_px - sl_atr_mult * atr
                tp_level = entry_px + tp_atr_mult * atr
            else:
                trough = min(trough, l)
                sl_level = entry_px + sl_atr_mult * atr
                tp_level = entry_px - tp_atr_mult * atr

            trail_stop = None
            if trail_atr_mult < 50:
                if position == 1 and (peak - entry_px) >= trail_activate_atr * atr:
                    trl_active = True
                    trail_stop = peak - trail_atr_mult * atr
                elif position == -1 and (entry_px - trough) >= trail_activate_atr * atr:
                    trl_active = True
                    trail_stop = trough + trail_atr_mult * atr

            exit_px = exit_reason = None
            if position == 1:
                if l <= sl_level: exit_px, exit_reason = sl_level, "SL"
                elif trl_active and trail_stop and l <= trail_stop: exit_px, exit_reason = trail_stop, "TRAIL"
                elif h >= tp_level: exit_px, exit_reason = tp_level, "TP"
            else:
                if h >= sl_level: exit_px, exit_reason = sl_level, "SL"
                elif trl_active and trail_stop and h >= trail_stop: exit_px, exit_reason = trail_stop, "TRAIL"
                elif l <= tp_level: exit_px, exit_reason = tp_level, "TP"

            if exit_px is None and (i - entry_i) >= max_hold_bars:
                exit_px, exit_reason = c, "TIMEOUT"
            if exit_px is None and sig[i] != 0 and sig[i] != position:
                exit_px, exit_reason = o, "SIGNAL_FLIP"

            if exit_px is not None:
                exit_px_f = exit_px * (1 - SLIP) if position == 1 else exit_px * (1 + SLIP)
                fee_out = exit_px_f * qty * FEE
                pnl_gross = (exit_px_f - entry_px) * qty * position
                pnl_net = pnl_gross - fee_in - fee_out
                equity += pnl_net
                records.append({
                    "entry_idx": entry_i, "exit_idx": i,
                    "entry_time": times[entry_i], "exit_time": times[i],
                    "dir": "L" if position == 1 else "S",
                    "entry_price": entry_px, "exit_price": exit_px_f,
                    "qty": qty, "pnl_net": pnl_net,
                    "equity_after": equity, "exit_reason": exit_reason,
                    "holding_bars": i - entry_i,
                })
                last_exit_i = i
                position = 0

    return pd.DataFrame(records)


def grid_search_coin(coin_name, btc_score_ts, alt_df, split_date):
    """Grid search for one coin, returns train/test results."""
    alt_train = alt_df[alt_df["ts"] < split_date].reset_index(drop=True)
    alt_test = alt_df[alt_df["ts"] >= split_date].reset_index(drop=True)

    if len(alt_train) < 100 or len(alt_test) < 100:
        print(f"  {coin_name}: Insufficient data (train={len(alt_train)}, test={len(alt_test)})")
        return None

    print(f"  {coin_name}: Train={len(alt_train):,} Test={len(alt_test):,}")

    thresholds = [2.0, 2.5, 3.0, 3.5, 4.0]
    sl_mults = [1.5, 2.0, 2.5]
    tp_mults = [3.0, 4.0]
    trail_mults = [0.5, 0.8, 1.0, 99]
    cooldowns = [4, 8]
    alt_pa_filters = [True, False]

    best = None
    best_pnl = -999999
    count = 0

    for thr in thresholds:
        for use_pa in alt_pa_filters:
            signals, alt_merged = generate_btc_led_signal(btc_score_ts, alt_train, thr, use_pa)
            n_sig = (signals != 0).sum()
            if n_sig < 5:
                continue

            for sl_m in sl_mults:
                for tp_m in tp_mults:
                    if tp_m < sl_m * 1.5:
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

    if best is None:
        print(f"  {coin_name}: No valid results from {count} combos")
        return None

    print(f"  {coin_name}: Best train -> thr={best['threshold']} pa={best['alt_pa']} "
          f"SL={best['sl']} TP={best['tp']} tr={best['trail']} cd={best['cd']}")
    print(f"    Train: {best['total']} trades, WR={best['win_rate']:.1f}%, PnL=${best['net_pnl']:+,.2f}")

    # Validate on test
    signals_test, alt_merged_test = generate_btc_led_signal(
        btc_score_ts, alt_test, best["threshold"], best["alt_pa"])
    trades_test = run_backtest(alt_merged_test, signals_test,
                               sl_atr_mult=best["sl"], tp_atr_mult=best["tp"],
                               trail_atr_mult=best["trail"], trail_activate_atr=best["trail_act"],
                               cooldown_bars=best["cd"])
    m_test = calc_metrics(trades_test, len(alt_test))

    print(f"    OOS:   {m_test['total']} trades, WR={m_test['win_rate']:.1f}%, "
          f"PnL=${m_test['net_pnl']:+,.2f}, Sharpe={m_test['sharpe']:.3f}")

    return {
        "coin": coin_name, "config": best,
        "train": best, "test": m_test,
        "trades_test": trades_test,
    }


def run_dynamic_sltp_test(coin_name, btc_score_ts, alt_df, split_date, fixed_config):
    """Test dynamic SL/TP vs fixed on same coin config."""
    alt_test = alt_df[alt_df["ts"] >= split_date].reset_index(drop=True)
    if len(alt_test) < 100:
        return None

    thr = fixed_config["threshold"]
    use_pa = fixed_config["alt_pa"]
    signals, alt_merged = generate_btc_led_signal(btc_score_ts, alt_test, thr, use_pa)

    # Fixed (baseline)
    trades_fixed = run_backtest(alt_merged, signals,
                                sl_atr_mult=fixed_config["sl"],
                                tp_atr_mult=fixed_config["tp"],
                                trail_atr_mult=fixed_config["trail"],
                                trail_activate_atr=fixed_config.get("trail_act", 99),
                                cooldown_bars=fixed_config["cd"])
    m_fixed = calc_metrics(trades_fixed, len(alt_test))

    # Dynamic variants
    dynamic_results = []
    for sl_scale in [0.1, 0.15, 0.2, 0.25]:
        for tp_scale in [0.15, 0.2, 0.3, 0.4]:
            trades_dyn = run_backtest_dynamic_sltp(
                alt_merged, signals, btc_scores=None,
                base_sl=fixed_config["sl"], base_tp=fixed_config["tp"],
                sl_scale_factor=sl_scale, tp_scale_factor=tp_scale,
                trail_atr_mult=fixed_config["trail"],
                trail_activate_atr=fixed_config.get("trail_act", 99),
                cooldown_bars=fixed_config["cd"])
            m_dyn = calc_metrics(trades_dyn, len(alt_test))
            dynamic_results.append({
                "sl_scale": sl_scale, "tp_scale": tp_scale, **m_dyn
            })

    best_dyn = max(dynamic_results, key=lambda x: x["net_pnl"]) if dynamic_results else None

    return {
        "coin": coin_name,
        "fixed": m_fixed,
        "best_dynamic": best_dyn,
        "all_dynamic": dynamic_results,
    }


def main():
    print("=" * 60)
    print("EXPERIMENT: BTC-Led Extended Tests")
    print("=" * 60)

    # ---- Build BTC composite score (reuse cached data) ----
    print("\n[PHASE 1] Building BTC Composite Score...")
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

    print(f"  BTC Score: {btc_period_start} to {btc_period_end}")
    print(f"  Range: [{btc_score_trimmed.min():.1f}, {btc_score_trimmed.max():.1f}]")

    split_date = pd.Timestamp("2026-01-01")

    # ================================================================
    # EXPERIMENT 1: ETH + SOL with BTC-led strategy
    # ================================================================
    print(f"\n{'='*60}")
    print("EXPERIMENT 1: ETH + SOL (New Coins)")
    print(f"{'='*60}")

    new_coins = ["ETH", "SOL"]
    new_results = {}

    for coin in new_coins:
        symbol = f"{coin}USDT"
        print(f"\n--- {coin} ({symbol}) ---")
        ohlcv = fetch_binance_15m(symbol, years=3)
        alt_df = build_alt_technicals(ohlcv)
        alt_df = alt_df[(alt_df["ts"] >= btc_period_start) & (alt_df["ts"] <= btc_period_end)]
        alt_df = alt_df.reset_index(drop=True)

        result = grid_search_coin(coin, btc_score_ts, alt_df, split_date)
        new_results[coin] = result

    # ================================================================
    # EXPERIMENT 2: Dynamic SL/TP on profitable coins
    # ================================================================
    print(f"\n{'='*60}")
    print("EXPERIMENT 2: Dynamic SL/TP (Score-Based)")
    print(f"{'='*60}")

    # Best configs from original backtest
    fixed_configs = {
        "BTC": {"threshold": 3.0, "alt_pa": True, "sl": 2.5, "tp": 4.0, "trail": 0.5, "trail_act": 0.5, "cd": 4},
        "XRP": {"threshold": 2.5, "alt_pa": True, "sl": 1.5, "tp": 3.0, "trail": 99, "trail_act": 99, "cd": 4},
        "ADA": {"threshold": 3.5, "alt_pa": True, "sl": 1.5, "tp": 4.0, "trail": 99, "trail_act": 99, "cd": 4},
    }

    dynamic_results = {}
    for coin in ["BTC", "XRP", "ADA"]:
        symbol = f"{coin}USDT"
        print(f"\n--- {coin} Dynamic SL/TP ---")

        if coin == "BTC":
            ohlcv = btc_ohlcv
        else:
            ohlcv = fetch_binance_15m(symbol, years=3)

        alt_df = build_alt_technicals(ohlcv)
        alt_df = alt_df[(alt_df["ts"] >= btc_period_start) & (alt_df["ts"] <= btc_period_end)]
        alt_df = alt_df.reset_index(drop=True)

        result = run_dynamic_sltp_test(coin, btc_score_ts, alt_df, split_date, fixed_configs[coin])
        dynamic_results[coin] = result

        if result:
            f = result["fixed"]
            d = result["best_dynamic"]
            print(f"  Fixed:   {f['total']} trades, PnL=${f['net_pnl']:+,.2f}, Sharpe={f['sharpe']:.3f}")
            if d:
                print(f"  Dynamic: {d['total']} trades, PnL=${d['net_pnl']:+,.2f}, Sharpe={d.get('sharpe', 0):.3f}")
                print(f"           sl_scale={d['sl_scale']}, tp_scale={d['tp_scale']}")
                delta = d['net_pnl'] - f['net_pnl']
                print(f"  Delta:   ${delta:+,.2f} ({'BETTER' if delta > 0 else 'WORSE'})")

    # ================================================================
    # EXPERIMENT 3: DOGE revisit
    # ================================================================
    print(f"\n{'='*60}")
    print("EXPERIMENT 3: DOGE Revisit")
    print(f"{'='*60}")

    ohlcv = fetch_binance_15m("DOGEUSDT", years=3)
    alt_df = build_alt_technicals(ohlcv)
    alt_df = alt_df[(alt_df["ts"] >= btc_period_start) & (alt_df["ts"] <= btc_period_end)]
    alt_df = alt_df.reset_index(drop=True)

    doge_result = grid_search_coin("DOGE", btc_score_ts, alt_df, split_date)

    # ================================================================
    # SUMMARY REPORT
    # ================================================================
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")

    print("\n--- Experiment 1: New Coins OOS ---")
    for coin in new_coins:
        r = new_results.get(coin)
        if r and r["test"]:
            t = r["test"]
            c = r["config"]
            print(f"  {coin:5s}: {t['total']} trades, WR={t['win_rate']:.1f}%, "
                  f"PnL=${t['net_pnl']:+,.2f}, Sharpe={t['sharpe']:.3f}, PF={t['pf']:.3f} "
                  f"[thr={c['threshold']} pa={c['alt_pa']} SL={c['sl']} TP={c['tp']} tr={c['trail']}]")
        else:
            print(f"  {coin:5s}: NO VALID RESULT")

    print("\n--- Experiment 2: Dynamic vs Fixed SL/TP (OOS) ---")
    for coin in ["BTC", "XRP", "ADA"]:
        r = dynamic_results.get(coin)
        if r:
            f = r["fixed"]
            d = r["best_dynamic"]
            print(f"  {coin:5s}: Fixed=${f['net_pnl']:+,.2f} | "
                  f"Dynamic=${d['net_pnl']:+,.2f} (sl_s={d['sl_scale']}, tp_s={d['tp_scale']}) | "
                  f"Delta=${d['net_pnl']-f['net_pnl']:+,.2f}")

    print("\n--- Experiment 3: DOGE Revisit ---")
    if doge_result and doge_result["test"]:
        t = doge_result["test"]
        c = doge_result["config"]
        print(f"  DOGE:  {t['total']} trades, WR={t['win_rate']:.1f}%, "
              f"PnL=${t['net_pnl']:+,.2f}, Sharpe={t['sharpe']:.3f} "
              f"[thr={c['threshold']} pa={c['alt_pa']} SL={c['sl']} TP={c['tp']} tr={c['trail']}]")
    else:
        print(f"  DOGE:  NO VALID RESULT")

    print("\n--- Portfolio Candidates ---")
    all_candidates = []
    for coin in ["BTC", "XRP", "ADA"]:
        all_candidates.append({"coin": coin, "pnl": fixed_configs[coin], "source": "original"})

    for coin in new_coins:
        r = new_results.get(coin)
        if r and r["test"] and r["test"]["net_pnl"] > 0:
            all_candidates.append({"coin": coin, "pnl_oos": r["test"]["net_pnl"],
                                    "sharpe": r["test"]["sharpe"], "source": "new"})

    if doge_result and doge_result["test"] and doge_result["test"]["net_pnl"] > 0:
        all_candidates.append({"coin": "DOGE", "pnl_oos": doge_result["test"]["net_pnl"],
                                "sharpe": doge_result["test"]["sharpe"], "source": "revisit"})

    print("\nDone!")


if __name__ == "__main__":
    main()
