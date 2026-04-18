"""
Volatility Spike Entry System -- Backtest Harness
===================================================
Tests two entry modes during high-volatility periods:
1. Momentum ("ตามรถ") -- follow the move when it has legs
2. Contrarian ("แหย่สวน") -- fade the overreaction when exhausted

Spike detection: range_z > 2.0 OR vol_ratio > 2.5 OR liq_cascade
Mode classification: contrarian when overextended, else momentum
Signal: threshold adjustment (contrarian -1.0, momentum -0.5)

Experiments:
  1. Baseline (v3, no spike logic)
  2. Spike-Contrarian only
  3. Spike-Momentum only
  4. Spike-Both (auto-classify) + normal trades
  5. Grid search spike thresholds
"""

import json
import os
import sys
import warnings
from datetime import datetime

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

# Import from backtest engine
from backtest_15m_btc_led_alts import (
    ALT_COINS,
    BUDGET_USDT,
    COMPOSITE_WEIGHTS,
    DEAD_ZONE_END,
    DEAD_ZONE_START,
    INIT_EQUITY,
    V3_EXTRA_WEIGHTS,
    build_alt_technicals,
    build_btc_features,
    calc_metrics,
    compute_btc_composite_score,
    fetch_binance_15m,
    generate_btc_led_signal,
    load_btc_db_data,
    run_backtest,
)

# Import regime classifier
from test_sltp_optimization import classify_regime


# ════════════════════════════════════════════════════════════════
# Volatility Spike Detection & Classification
# ════════════════════════════════════════════════════════════════

def compute_volatility_features(df):
    """
    Add spike detection features to a DataFrame that already has
    high, low, close, ema21, atr, rsi, vol_ratio columns.

    New columns:
      - intrabar_range: (H-L)/C
      - range_z: z-score of intrabar range over 96-bar rolling window
      - ema21_dist: distance from EMA21 in ATR units (signed)
    """
    df = df.copy()
    df["intrabar_range"] = (df["high"] - df["low"]) / df["close"].clip(lower=1e-8)
    range_ma = df["intrabar_range"].rolling(96).mean()
    range_std = df["intrabar_range"].rolling(96).std().clip(lower=1e-8)
    df["range_z"] = (df["intrabar_range"] - range_ma) / range_std
    atr_safe = df["atr"].clip(lower=1e-8)
    df["ema21_dist"] = (df["close"] - df["ema21"]) / atr_safe
    return df


def detect_spike(df, range_z_thr=2.0, vol_ratio_thr=2.5, liq_mult=3.0):
    """
    Boolean Series: True when volatility spike detected.
    Spike = any of:
      - range_z > threshold (intrabar range is extreme)
      - vol_ratio > threshold (volume is extreme)
      - liq_cascade (liq_total > liq_total_ma * mult)
    """
    spike = pd.Series(False, index=df.index)

    if "range_z" in df.columns:
        spike = spike | (df["range_z"] > range_z_thr)

    if "vol_ratio" in df.columns:
        spike = spike | (df["vol_ratio"] > vol_ratio_thr)

    if "liq_total" in df.columns and "liq_total_ma" in df.columns:
        liq_ok = df["liq_total"].notna() & df["liq_total_ma"].notna()
        spike = spike | (liq_ok & (df["liq_total"] > df["liq_total_ma"] * liq_mult))

    return spike


def classify_spike_mode(df, liq_mult_extreme=5.0, displacement_thr=2.0,
                        rsi_high=75, rsi_low=25):
    """
    Classify spike bars as 'contrarian' or 'momentum'.

    Contrarian when ANY of:
      - Extreme liquidation cascade (liq_total > liq_total_ma * 5)
      - Price displaced > 2 ATR from EMA21
      - RSI extreme (> 75 or < 25)

    Momentum when spike but NOT contrarian.
    Returns Series of strings: 'contrarian' or 'momentum'.
    """
    is_contrarian = pd.Series(False, index=df.index)

    # Extreme liquidation
    if "liq_total" in df.columns and "liq_total_ma" in df.columns:
        liq_ok = df["liq_total"].notna() & df["liq_total_ma"].notna()
        is_contrarian = is_contrarian | (liq_ok & (df["liq_total"] > df["liq_total_ma"] * liq_mult_extreme))

    # Extreme displacement from EMA21
    if "ema21_dist" in df.columns:
        is_contrarian = is_contrarian | (df["ema21_dist"].abs() > displacement_thr)

    # Extreme RSI
    if "rsi" in df.columns:
        rsi = df["rsi"].fillna(50)
        is_contrarian = is_contrarian | (rsi > rsi_high) | (rsi < rsi_low)

    mode = pd.Series("momentum", index=df.index)
    mode[is_contrarian] = "contrarian"
    return mode


def generate_spike_signal(btc_score, alt_df, threshold, spike, mode,
                          contrarian_reduction=1.0, momentum_reduction=0.5):
    """
    Generate modified signals during volatility spikes.

    - Contrarian: signal OPPOSITE to move, threshold reduced by contrarian_reduction
      (only if composite score supports contrarian direction)
    - Momentum: signal SAME as move, threshold reduced by momentum_reduction
      (score must already point in move direction)

    Returns signal Series (1=LONG, -1=SHORT, 0=NO_SIGNAL).
    """
    signal = pd.Series(0, index=alt_df.index)

    # Precompute
    score_vals = btc_score.values if len(btc_score) == len(alt_df) else np.zeros(len(alt_df))
    ret_vals = alt_df["ret"].fillna(0).values if "ret" in alt_df.columns else np.zeros(len(alt_df))
    spike_vals = spike.values
    mode_vals = mode.values

    # Dead zone filter
    if "ts" in alt_df.columns:
        hour = alt_df["ts"].dt.hour
        is_dead = (hour >= DEAD_ZONE_START) | (hour < DEAD_ZONE_END)
    else:
        is_dead = pd.Series(False, index=alt_df.index)

    for i in range(len(alt_df)):
        if not spike_vals[i] or is_dead.iloc[i]:
            continue

        score = score_vals[i]
        ret = ret_vals[i]

        if mode_vals[i] == "contrarian":
            adj_thr = max(threshold - contrarian_reduction, 0.5)
            # Fade the move -- determine move direction from return
            if ret < -0.005:
                # Price dropped hard -> contrarian = go LONG
                if score >= adj_thr:
                    signal.iloc[i] = 1
            elif ret > 0.005:
                # Price spiked up hard -> contrarian = go SHORT
                if score <= -adj_thr:
                    signal.iloc[i] = -1
        else:
            # Momentum -- follow the move
            adj_thr = max(threshold - momentum_reduction, 1.0)
            if score >= adj_thr:
                signal.iloc[i] = 1
            elif score <= -adj_thr:
                signal.iloc[i] = -1

    return signal


def generate_combined_signal(btc_score_series, alt_df, threshold, use_alt_pa_filter,
                             spike, mode,
                             contrarian_reduction=1.0, momentum_reduction=0.5,
                             btc_regime_ts=None, regime_penalty=0.0):
    """
    Generate combined signal: normal v3 signal + spike overlay.
    Spike signals fill in gaps where normal signal is 0.
    """
    # Normal v3 signal
    normal_signal, alt_merged = generate_btc_led_signal(
        btc_score_series, alt_df, threshold, use_alt_pa_filter,
        btc_regime_ts=btc_regime_ts, regime_penalty=regime_penalty)

    # Align spike/mode to merged alt
    spike_aligned = spike.reindex(alt_merged.index, fill_value=False)
    mode_aligned = mode.reindex(alt_merged.index, fill_value="momentum")

    # BTC score from merged data
    btc_score_vals = alt_merged["btc_score"] if "btc_score" in alt_merged.columns else pd.Series(0.0, index=alt_merged.index)

    # Spike signal
    spike_signal = generate_spike_signal(
        btc_score_vals, alt_merged, threshold, spike_aligned, mode_aligned,
        contrarian_reduction, momentum_reduction)

    # Combine: normal takes priority, spike fills in gaps
    combined = normal_signal.copy()
    spike_only = (normal_signal == 0) & (spike_signal != 0)
    combined[spike_only] = spike_signal[spike_only]

    return combined, alt_merged


# ════════════════════════════════════════════════════════════════
# Experiment Runner
# ════════════════════════════════════════════════════════════════

def run_experiment(name, btc_df, btc_score_ts, btc_regime_ts, coins, oos_start,
                   signal_fn, sl=3.0, tp=5.0, cooldown=4):
    """
    Run one experiment across all coins in OOS period.
    signal_fn(btc_score_ts, alt_df, coin_config) -> (signals, alt_merged)
    """
    print(f"\n{'='*60}")
    print(f"  Experiment: {name}")
    print(f"{'='*60}")

    all_trades = []
    per_coin = {}

    for coin in coins:
        symbol = f"{coin}USDT"
        ohlcv = fetch_binance_15m(symbol, years=3)
        alt_df = build_alt_technicals(ohlcv)
        alt_df = alt_df[alt_df["ts"] >= oos_start].reset_index(drop=True)

        if len(alt_df) < 100:
            print(f"  {coin}: insufficient data ({len(alt_df)} bars)")
            continue

        # Add volatility features to alt
        alt_df = compute_volatility_features(alt_df)

        signals, alt_merged = signal_fn(btc_score_ts, alt_df, coin)

        trades = run_backtest(alt_merged, signals,
                              sl_atr_mult=sl, tp_atr_mult=tp,
                              trail_atr_mult=99, trail_activate_atr=99,
                              cooldown_bars=cooldown)

        m = calc_metrics(trades, len(alt_merged))
        per_coin[coin] = m

        if not trades.empty:
            trades["coin"] = coin
            all_trades.append(trades)

        print(f"  {coin:6s}: {m['total']:3d} trades, WR={m['win_rate']:5.1f}%, "
              f"PnL=${m['net_pnl']:+,.0f}, Sharpe={m['sharpe']:.2f}")

    if all_trades:
        all_df = pd.concat(all_trades, ignore_index=True)
    else:
        all_df = pd.DataFrame()

    total_m = calc_metrics(all_df, 1) if not all_df.empty else calc_metrics(pd.DataFrame(), 1)
    total_m["per_coin"] = per_coin

    print(f"\n  TOTAL: {total_m['total']} trades, WR={total_m['win_rate']:.1f}%, "
          f"PnL=${total_m['net_pnl']:+,.0f}, Sharpe={total_m['sharpe']:.2f}")

    return total_m, all_df


def run_all_experiments():
    """Run all 5 experiments and save results."""
    print("\n" + "=" * 70)
    print("  VOLATILITY SPIKE ENTRY SYSTEM -- BACKTEST")
    print("=" * 70)

    # ── Load data ──
    print("\n[1] Loading BTC data...")
    btc_ohlcv = fetch_binance_15m("BTCUSDT", years=3)
    db_data = load_btc_db_data()
    btc_df = build_btc_features(btc_ohlcv, db_data)
    btc_score = compute_btc_composite_score(btc_df)
    btc_score_ts = btc_score.copy()
    btc_score_ts.index = btc_df["ts"]

    # Regime
    btc_regime_ts = (btc_df["close"] > btc_df["ema50"])
    btc_regime_ts.index = btc_df["ts"]

    # Add volatility features to BTC
    btc_df = compute_volatility_features(btc_df)

    # Spike detection on BTC (used for spike-aware signals)
    btc_spike = detect_spike(btc_df)
    btc_mode = classify_spike_mode(btc_df)

    oos_start = pd.Timestamp("2025-12-01")
    coins = ALT_COINS  # 6 original coins

    # ── Spike frequency check ──
    oos_btc = btc_df[btc_df["ts"] >= oos_start]
    oos_spike = btc_spike[btc_df["ts"] >= oos_start]
    oos_mode = btc_mode[btc_df["ts"] >= oos_start]
    n_total = len(oos_btc)
    n_spike = oos_spike.sum()
    n_contrarian = (oos_mode[oos_spike] == "contrarian").sum()
    n_momentum = (oos_mode[oos_spike] == "momentum").sum()
    pct_spike = n_spike / n_total * 100 if n_total > 0 else 0

    print(f"\n[2] Spike Frequency Check (OOS period):")
    print(f"  Total bars: {n_total:,}")
    print(f"  Spike bars: {n_spike:,} ({pct_spike:.1f}%)")
    print(f"  Contrarian: {n_contrarian:,} ({n_contrarian/max(n_spike,1)*100:.1f}% of spikes)")
    print(f"  Momentum:   {n_momentum:,} ({n_momentum/max(n_spike,1)*100:.1f}% of spikes)")

    results = {}

    # ── Experiment 1: Baseline ──
    def baseline_signal(score_ts, alt_df, coin):
        from paper_trading.config import COIN_CONFIGS
        cfg = COIN_CONFIGS.get(coin, {"threshold": 3.0, "use_alt_pa_filter": False})
        thr = cfg.get("threshold", 3.0)
        pa = cfg.get("use_alt_pa_filter", False)
        return generate_btc_led_signal(score_ts, alt_df, thr, pa)

    m1, trades1 = run_experiment("1. Baseline (v3, no spike)",
                                  btc_df, btc_score_ts, btc_regime_ts, coins, oos_start,
                                  baseline_signal)
    results["baseline"] = m1

    # ── Experiment 2: Spike-Contrarian Only ──
    def contrarian_only_signal(score_ts, alt_df, coin):
        from paper_trading.config import COIN_CONFIGS
        cfg = COIN_CONFIGS.get(coin, {"threshold": 3.0})
        thr = cfg.get("threshold", 3.0)
        # Detect spikes on alt data
        spike = detect_spike(alt_df)
        mode = pd.Series("contrarian", index=alt_df.index)  # force all to contrarian
        # BTC score aligned to alt
        score_df = score_ts.reset_index()
        score_df.columns = ["ts", "btc_score"]
        alt = alt_df.copy().sort_values("ts")
        alt = pd.merge_asof(alt, score_df.sort_values("ts"),
                            on="ts", direction="backward", tolerance=pd.Timedelta("30min"))
        alt["btc_score"] = alt["btc_score"].fillna(0)
        score_aligned = alt["btc_score"]
        sig = generate_spike_signal(score_aligned, alt, thr, spike, mode,
                                    contrarian_reduction=1.0, momentum_reduction=0.5)
        return sig, alt

    m2, trades2 = run_experiment("2. Spike-Contrarian Only",
                                  btc_df, btc_score_ts, btc_regime_ts, coins, oos_start,
                                  contrarian_only_signal)
    results["contrarian_only"] = m2

    # ── Experiment 3: Spike-Momentum Only ──
    def momentum_only_signal(score_ts, alt_df, coin):
        from paper_trading.config import COIN_CONFIGS
        cfg = COIN_CONFIGS.get(coin, {"threshold": 3.0})
        thr = cfg.get("threshold", 3.0)
        spike = detect_spike(alt_df)
        mode = pd.Series("momentum", index=alt_df.index)  # force all to momentum
        score_df = score_ts.reset_index()
        score_df.columns = ["ts", "btc_score"]
        alt = alt_df.copy().sort_values("ts")
        alt = pd.merge_asof(alt, score_df.sort_values("ts"),
                            on="ts", direction="backward", tolerance=pd.Timedelta("30min"))
        alt["btc_score"] = alt["btc_score"].fillna(0)
        score_aligned = alt["btc_score"]
        sig = generate_spike_signal(score_aligned, alt, thr, spike, mode,
                                    contrarian_reduction=1.0, momentum_reduction=0.5)
        return sig, alt

    m3, trades3 = run_experiment("3. Spike-Momentum Only",
                                  btc_df, btc_score_ts, btc_regime_ts, coins, oos_start,
                                  momentum_only_signal)
    results["momentum_only"] = m3

    # ── Experiment 4: Spike-Both + Normal ──
    def combined_signal(score_ts, alt_df, coin):
        from paper_trading.config import COIN_CONFIGS
        cfg = COIN_CONFIGS.get(coin, {"threshold": 3.0, "use_alt_pa_filter": False})
        thr = cfg.get("threshold", 3.0)
        pa = cfg.get("use_alt_pa_filter", False)
        spike = detect_spike(alt_df)
        mode = classify_spike_mode(alt_df)
        return generate_combined_signal(score_ts, alt_df, thr, pa, spike, mode,
                                        contrarian_reduction=1.0, momentum_reduction=0.5)

    m4, trades4 = run_experiment("4. Spike-Both + Normal",
                                  btc_df, btc_score_ts, btc_regime_ts, coins, oos_start,
                                  combined_signal)
    results["combined"] = m4

    # ── Experiment 5: Grid Search Spike Thresholds ──
    print(f"\n{'='*60}")
    print(f"  Experiment 5: Grid Search Spike Parameters")
    print(f"{'='*60}")

    best_config = None
    best_pnl = -float("inf")
    grid_results = []

    range_z_options = [1.5, 2.0, 2.5, 3.0]
    vol_ratio_options = [2.0, 2.5, 3.0]
    contrarian_red_options = [0.5, 1.0, 1.5]
    momentum_red_options = [0.3, 0.5, 0.8]
    displacement_options = [1.5, 2.0, 2.5]

    total_combos = (len(range_z_options) * len(vol_ratio_options) *
                    len(contrarian_red_options) * len(momentum_red_options) *
                    len(displacement_options))
    print(f"  Grid: {total_combos} combinations")

    combo_count = 0
    for rz in range_z_options:
        for vr in vol_ratio_options:
            for cr in contrarian_red_options:
                for mr in momentum_red_options:
                    for disp in displacement_options:
                        combo_count += 1
                        all_trades_grid = []

                        for coin in coins:
                            symbol = f"{coin}USDT"
                            ohlcv = fetch_binance_15m(symbol, years=3)
                            alt_df = build_alt_technicals(ohlcv)
                            alt_df = alt_df[alt_df["ts"] >= oos_start].reset_index(drop=True)
                            if len(alt_df) < 100:
                                continue
                            alt_df = compute_volatility_features(alt_df)

                            from paper_trading.config import COIN_CONFIGS
                            cfg = COIN_CONFIGS.get(coin, {"threshold": 3.0, "use_alt_pa_filter": False})
                            thr = cfg.get("threshold", 3.0)
                            pa = cfg.get("use_alt_pa_filter", False)

                            spike = detect_spike(alt_df, range_z_thr=rz, vol_ratio_thr=vr)
                            mode = classify_spike_mode(alt_df, displacement_thr=disp)

                            sig, alt_merged = generate_combined_signal(
                                btc_score_ts, alt_df, thr, pa, spike, mode,
                                contrarian_reduction=cr, momentum_reduction=mr)

                            trades = run_backtest(alt_merged, sig,
                                                  sl_atr_mult=3.0, tp_atr_mult=5.0,
                                                  trail_atr_mult=99, trail_activate_atr=99,
                                                  cooldown_bars=4)
                            if not trades.empty:
                                trades["coin"] = coin
                                all_trades_grid.append(trades)

                        if all_trades_grid:
                            all_df_grid = pd.concat(all_trades_grid, ignore_index=True)
                            m = calc_metrics(all_df_grid, 1)
                        else:
                            m = calc_metrics(pd.DataFrame(), 1)

                        config = {
                            "range_z": rz, "vol_ratio": vr,
                            "contrarian_reduction": cr, "momentum_reduction": mr,
                            "displacement": disp,
                        }
                        grid_results.append({**config, **m})

                        if m["net_pnl"] > best_pnl:
                            best_pnl = m["net_pnl"]
                            best_config = config

                        if combo_count % 20 == 0:
                            print(f"    {combo_count}/{total_combos}... best PnL=${best_pnl:+,.0f}", end="\r", flush=True)

    print(f"    {combo_count}/{total_combos} done!                            ")
    print(f"\n  Best config: {best_config}")
    print(f"  Best PnL: ${best_pnl:+,.0f}")

    # Run best config to get full results
    best_m_exp5 = None
    for gr in grid_results:
        if (gr["range_z"] == best_config["range_z"] and
            gr["vol_ratio"] == best_config["vol_ratio"] and
            gr["contrarian_reduction"] == best_config["contrarian_reduction"] and
            gr["momentum_reduction"] == best_config["momentum_reduction"] and
            gr["displacement"] == best_config["displacement"]):
            best_m_exp5 = gr
            break

    results["optimized"] = best_m_exp5
    results["optimized_config"] = best_config
    results["grid_top10"] = sorted(grid_results, key=lambda x: x["net_pnl"], reverse=True)[:10]

    # ── Regime Analysis ──
    print(f"\n{'='*60}")
    print(f"  Regime Analysis (Experiment 4: Combined)")
    print(f"{'='*60}")

    if not trades4.empty:
        trades4_regime = classify_regime(trades4, btc_df)
        for regime in ["BULL", "BEAR", "FLAT"]:
            rt = trades4_regime[trades4_regime["regime"] == regime]
            if not rt.empty:
                rm = calc_metrics(rt, 1)
                print(f"  {regime:5s}: {rm['total']:3d} trades, WR={rm['win_rate']:5.1f}%, "
                      f"PnL=${rm['net_pnl']:+,.0f}")
                results[f"combined_regime_{regime.lower()}"] = rm
            else:
                print(f"  {regime:5s}: no trades")

    # ── Summary ──
    print(f"\n{'='*70}")
    print(f"  SUMMARY")
    print(f"{'='*70}")
    for exp_name, key in [("1. Baseline", "baseline"),
                           ("2. Contrarian Only", "contrarian_only"),
                           ("3. Momentum Only", "momentum_only"),
                           ("4. Combined", "combined"),
                           ("5. Optimized", "optimized")]:
        m = results.get(key, {})
        if m:
            print(f"  {exp_name:25s}: {m.get('total', 0):4d} trades, "
                  f"WR={m.get('win_rate', 0):5.1f}%, "
                  f"PnL=${m.get('net_pnl', 0):+,.0f}, "
                  f"Sharpe={m.get('sharpe', 0):.2f}")

    # ── Save results ──
    os.makedirs("experiments", exist_ok=True)

    # Clean results for JSON (remove non-serializable)
    save_results = {}
    for k, v in results.items():
        if isinstance(v, dict):
            clean = {}
            for kk, vv in v.items():
                if isinstance(vv, (int, float, str, bool, type(None))):
                    clean[kk] = vv
                elif isinstance(vv, dict):
                    # per_coin dict
                    clean[kk] = {ck: cv for ck, cv in vv.items()
                                 if isinstance(cv, (int, float, str, bool, type(None)))}
            save_results[k] = clean
        elif isinstance(v, list):
            save_results[k] = v

    save_results["timestamp"] = datetime.now().isoformat()
    save_results["spike_frequency"] = {
        "total_bars": int(n_total),
        "spike_bars": int(n_spike),
        "spike_pct": round(pct_spike, 1),
        "contrarian_bars": int(n_contrarian),
        "momentum_bars": int(n_momentum),
    }

    with open("experiments/volatility_spike_results.json", "w") as f:
        json.dump(save_results, f, indent=2, default=str)
    print(f"\n  Results saved to experiments/volatility_spike_results.json")

    return results


if __name__ == "__main__":
    results = run_all_experiments()
