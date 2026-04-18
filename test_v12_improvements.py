"""
v1.2 Improvement Test Harness
=============================
Tests 5 targeted improvements against v1.1 baseline, one at a time,
then combined. All on same Jan-Mar 2026 test period.

Improvements:
  1. Drop DOT (negative OOS coin)
  2. Tighter max hold: 96 -> 40 bars
  3. Short bias: threshold_short = threshold - 0.5
  4. Hour filter: suppress 00:00-04:59 UTC entries
  5. Stale exit: force exit if flat after 30 bars
  6. Combined: stack all that helped
  7. Combined + 2x leverage

All results saved to experiments/ for future reference.
"""

import os, sys, json, warnings
from datetime import datetime

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

# ---- v1.1 configs (current paper trading production) ----
V11_CONFIGS = {
    "BTC": {"threshold": 2.5, "alt_pa": False, "sl": 2.5, "tp": 4.0, "trail": 99, "trail_act": 99, "cd": 4},
    "XRP": {"threshold": 3.5, "alt_pa": False, "sl": 2.5, "tp": 4.0, "trail": 99, "trail_act": 99, "cd": 4},
    "ADA": {"threshold": 3.5, "alt_pa": False, "sl": 2.5, "tp": 4.0, "trail": 99, "trail_act": 99, "cd": 4},
    "DOT": {"threshold": 3.0, "alt_pa": False, "sl": 2.5, "tp": 4.0, "trail": 99, "trail_act": 99, "cd": 8},
    "SUI": {"threshold": 3.0, "alt_pa": False, "sl": 2.5, "tp": 4.0, "trail": 99, "trail_act": 99, "cd": 4},
    "FIL": {"threshold": 3.0, "alt_pa": False, "sl": 2.5, "tp": 4.0, "trail": 99, "trail_act": 99, "cd": 4},
}

ALL_COINS = ["BTC", "XRP", "ADA", "DOT", "SUI", "FIL"]
TEST_START = pd.Timestamp("2026-01-01")

BTC_SCORE_WEIGHTS = {
    "w_oi_bull": 0.5, "w_oi_capit": 0.5, "w_oi_weak": 0.5, "w_oi_bear": 0.5,
    "w_taker_strong": 1.5, "w_taker_mild": 0.5, "w_ls_extreme": 1.5,
    "w_fr_neg": 2.0, "w_fr_pos": 2.0, "w_fg_fear": 2.0, "w_fg_mild_fear": 1.0,
    "w_fg_greed": 2.0, "w_fg_mild_greed": 1.0, "w_whale_bull": 1.5, "w_whale_bear": 1.5,
    "w_liq_bull": 2.0, "w_liq_bear": 2.0, "w_etf_bull": 1.5, "w_etf_bear": 1.5,
}


# ---- Signal generation variants ----

def generate_signal_v11(btc_score_ts, alt_df, threshold, use_alt_pa):
    """v1.1 signal: no dead zone, no regime filter."""
    btc_score_df = btc_score_ts.reset_index()
    btc_score_df.columns = ["ts", "btc_score"]
    alt = alt_df.copy().sort_values("ts")
    alt = pd.merge_asof(alt, btc_score_df.sort_values("ts"),
                        on="ts", direction="backward", tolerance=pd.Timedelta("30min"))
    alt["btc_score"] = alt["btc_score"].fillna(0)

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


def generate_signal_short_bias(btc_score_ts, alt_df, threshold, use_alt_pa, short_offset=0.5):
    """Short bias: lower threshold for SHORT signals by short_offset."""
    btc_score_df = btc_score_ts.reset_index()
    btc_score_df.columns = ["ts", "btc_score"]
    alt = alt_df.copy().sort_values("ts")
    alt = pd.merge_asof(alt, btc_score_df.sort_values("ts"),
                        on="ts", direction="backward", tolerance=pd.Timedelta("30min"))
    alt["btc_score"] = alt["btc_score"].fillna(0)

    signal = pd.Series(0, index=alt.index)
    # LONG: same threshold
    signal[alt["btc_score"] >= threshold] = 1
    # SHORT: easier to trigger (lower threshold)
    threshold_short = threshold - short_offset
    signal[alt["btc_score"] <= -threshold_short] = -1

    if use_alt_pa:
        alt_bull_pa = (alt["close"] > alt["ema9"]) & (alt["ema9"] > alt["ema21"])
        alt_bear_pa = (alt["close"] < alt["ema9"]) & (alt["ema9"] < alt["ema21"])
        alt_vol_ok = alt["vol_ratio"] > 0.8
        mask_long = (signal == 1) & ~(alt_bull_pa & alt_vol_ok)
        mask_short = (signal == -1) & ~(alt_bear_pa & alt_vol_ok)
        signal[mask_long] = 0
        signal[mask_short] = 0

    return signal, alt


def generate_signal_hour_filter(btc_score_ts, alt_df, threshold, use_alt_pa,
                                suppress_start=0, suppress_end=5):
    """Suppress entries during specified UTC hours (default 00:00-04:59)."""
    signal, alt = generate_signal_v11(btc_score_ts, alt_df, threshold, use_alt_pa)
    hour = alt["ts"].dt.hour
    is_suppressed = (hour >= suppress_start) & (hour < suppress_end)
    signal[is_suppressed] = 0
    return signal, alt


# ---- Run one scenario for one coin ----

def run_coin_scenario(coin, btc_score_ts, alt_test, cfg, scenario_opts):
    """
    Run a single coin with scenario-specific options.

    scenario_opts keys:
      - signal_fn: callable(btc_score_ts, alt_df, threshold, use_alt_pa) -> signal, alt
      - max_hold_bars: int (default 96)
      - leverage: float (default 1.0)
      - stale_exit_bars: int or None (default None)
    """
    signal_fn = scenario_opts.get("signal_fn", generate_signal_v11)
    max_hold = scenario_opts.get("max_hold_bars", 96)
    leverage = scenario_opts.get("leverage", 1.0)
    stale_exit = scenario_opts.get("stale_exit_bars", None)

    # Generate signals
    signal_kwargs = scenario_opts.get("signal_kwargs", {})
    signals, alt_merged = signal_fn(btc_score_ts, alt_test, cfg["threshold"], cfg["alt_pa"],
                                     **signal_kwargs)

    # Set leverage
    old_lev = bt.LEVERAGE
    bt.LEVERAGE = leverage

    trades = run_backtest(alt_merged, signals,
                          sl_atr_mult=cfg["sl"], tp_atr_mult=cfg["tp"],
                          trail_atr_mult=cfg["trail"], trail_activate_atr=cfg["trail_act"],
                          cooldown_bars=cfg["cd"], max_hold_bars=max_hold,
                          stale_exit_bars=stale_exit)

    bt.LEVERAGE = old_lev

    m = calc_metrics(trades, len(alt_test))
    return trades, m


# ---- Experiment persistence ----

def save_experiment(exp_id, description, scenario_results, params_desc):
    """Save experiment results to experiments/ directory."""
    os.makedirs("experiments/trades", exist_ok=True)
    os.makedirs("experiments/analysis", exist_ok=True)

    # Build summary rows
    summary_rows = []
    for scenario_name, coin_data in scenario_results.items():
        for coin, data in coin_data.items():
            if coin == "TOTAL":
                continue
            m = data["metrics"]
            summary_rows.append({
                "experiment_id": exp_id,
                "scenario": scenario_name,
                "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "coin": coin,
                "oos_trades": m["total"],
                "oos_wr": m["win_rate"],
                "oos_pf": m["pf"],
                "oos_pnl": m["net_pnl"],
                "oos_sharpe": m["sharpe"],
                "oos_dd": m["max_dd"],
                "oos_rr": m["rr"],
                "n_long": m["n_long"],
                "n_short": m["n_short"],
                "wr_long": m["wr_long"],
                "wr_short": m["wr_short"],
            })

    summary_df = pd.DataFrame(summary_rows)

    # Append to summaries.csv
    summary_path = "experiments/summaries.csv"
    if os.path.exists(summary_path):
        existing = pd.read_csv(summary_path)
        summary_df = pd.concat([existing, summary_df], ignore_index=True)
    summary_df.to_csv(summary_path, index=False)

    # Save trade-level parquet per scenario
    for scenario_name, coin_data in scenario_results.items():
        all_trades = []
        for coin, data in coin_data.items():
            if coin == "TOTAL":
                continue
            trades = data["trades"]
            if not trades.empty:
                t = trades.copy()
                t["experiment_id"] = exp_id
                t["scenario"] = scenario_name
                t["coin"] = coin
                if "entry_time" in t.columns:
                    t["hour_utc"] = pd.to_datetime(t["entry_time"]).dt.hour
                all_trades.append(t)
        if all_trades:
            combined = pd.concat(all_trades, ignore_index=True)
            safe_name = scenario_name.replace(" ", "_").replace("+", "plus")
            combined.to_parquet(f"experiments/trades/{exp_id}_{safe_name}.parquet", index=False)

    # Save per-scenario analysis CSVs
    for scenario_name, coin_data in coin_data_items(scenario_results):
        save_analysis(exp_id, scenario_name, coin_data)

    # Update registry
    registry_path = "experiments/registry.json"
    if os.path.exists(registry_path):
        with open(registry_path, "r") as f:
            registry = json.load(f)
    else:
        registry = []

    # Build scenario summary for registry
    scenario_summary = {}
    for scenario_name, coin_data in scenario_results.items():
        total_pnl = sum(d["metrics"]["net_pnl"] for c, d in coin_data.items() if c != "TOTAL")
        n_coins = sum(1 for c in coin_data if c != "TOTAL")
        scenario_summary[scenario_name] = {
            "total_pnl": round(total_pnl, 2),
            "n_coins": n_coins,
        }

    registry.append({
        "experiment_id": exp_id,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "description": description,
        "params": params_desc,
        "scenarios": scenario_summary,
    })

    with open(registry_path, "w") as f:
        json.dump(registry, f, indent=2)

    print(f"\n  Experiment '{exp_id}' saved to experiments/")


def coin_data_items(scenario_results):
    """Yield (scenario_name, coin_data) pairs."""
    for scenario_name, coin_data in scenario_results.items():
        yield scenario_name, coin_data


def save_analysis(exp_id, scenario_name, coin_data):
    """Save hourly PnL, direction, and exit reason analysis CSVs."""
    safe_name = scenario_name.replace(" ", "_").replace("+", "plus")

    # Hourly PnL breakdown
    hourly_rows = []
    dir_rows = []
    exit_rows = []

    for coin, data in coin_data.items():
        if coin == "TOTAL":
            continue
        trades = data["trades"]
        if trades.empty:
            continue

        t = trades.copy()
        t["hour_utc"] = pd.to_datetime(t["entry_time"]).dt.hour

        # Hourly
        for hour in range(24):
            h_trades = t[t["hour_utc"] == hour]
            if len(h_trades) > 0:
                hourly_rows.append({
                    "coin": coin, "hour_utc": hour,
                    "n_trades": len(h_trades),
                    "pnl": round(h_trades["pnl_net"].sum(), 2),
                    "wr": round((h_trades["pnl_net"] > 0).mean() * 100, 1),
                })

        # Direction breakdown
        for direction in ["L", "S"]:
            d_trades = t[t["dir"] == direction]
            if len(d_trades) > 0:
                dir_rows.append({
                    "coin": coin, "direction": direction,
                    "n_trades": len(d_trades),
                    "pnl": round(d_trades["pnl_net"].sum(), 2),
                    "wr": round((d_trades["pnl_net"] > 0).mean() * 100, 1),
                    "avg_pnl": round(d_trades["pnl_net"].mean(), 2),
                })

        # Exit reason distribution
        for reason, grp in t.groupby("exit_reason"):
            exit_rows.append({
                "coin": coin, "exit_reason": reason,
                "n_trades": len(grp),
                "pnl": round(grp["pnl_net"].sum(), 2),
                "wr": round((grp["pnl_net"] > 0).mean() * 100, 1),
                "avg_holding": round(grp["holding_bars"].mean(), 1),
            })

    if hourly_rows:
        pd.DataFrame(hourly_rows).to_csv(
            f"experiments/analysis/{exp_id}_{safe_name}_hourly.csv", index=False)
    if dir_rows:
        pd.DataFrame(dir_rows).to_csv(
            f"experiments/analysis/{exp_id}_{safe_name}_direction.csv", index=False)
    if exit_rows:
        pd.DataFrame(exit_rows).to_csv(
            f"experiments/analysis/{exp_id}_{safe_name}_exits.csv", index=False)


# ---- Main ----

def main():
    print("=" * 70)
    print("v1.2 IMPROVEMENT TEST HARNESS")
    print("Test period: Jan 1 - Mar 8, 2026 (same for all scenarios)")
    print("Base: v1.1 params (proven, no re-grid-search)")
    print("=" * 70)

    # ---- Phase 1: Load shared data ----
    print("\n[Phase 1] Loading BTC data...")
    btc_ohlcv = fetch_binance_15m("BTCUSDT", years=3)
    btc_db = load_btc_db_data()
    btc_df = build_btc_features(btc_ohlcv, btc_db)
    btc_score = compute_btc_composite_score(btc_df, BTC_SCORE_WEIGHTS)

    btc_period_start = pd.Timestamp("2025-06-01")
    mask = btc_df["ts"] >= btc_period_start
    btc_df_trimmed = btc_df[mask].reset_index(drop=True)
    btc_score_trimmed = btc_score[mask].reset_index(drop=True)
    btc_score_ts = pd.Series(btc_score_trimmed.values, index=btc_df_trimmed["ts"].values)
    btc_period_end = btc_df_trimmed["ts"].iloc[-1]
    print(f"  BTC score: {btc_period_start} to {btc_period_end}")

    # Preload altcoin test data
    print("\n[Phase 1] Loading altcoin data...")
    alt_test_data = {}
    for coin in ALL_COINS:
        symbol = f"{coin}USDT"
        ohlcv = fetch_binance_15m(symbol, years=3)
        alt_df = build_alt_technicals(ohlcv)
        alt_test = alt_df[(alt_df["ts"] >= TEST_START) & (alt_df["ts"] <= btc_period_end)].reset_index(drop=True)
        if len(alt_test) >= 100:
            alt_test_data[coin] = alt_test
            print(f"  {coin}: {len(alt_test):,} test bars")
        else:
            print(f"  {coin}: insufficient data ({len(alt_test)} bars), skipped")

    # ---- Phase 2: Define scenarios ----
    scenarios = {
        "baseline": {
            "desc": "v1.1 as-is (6 coins, max_hold=96, no filters)",
            "coins": ALL_COINS,
            "opts": {"signal_fn": generate_signal_v11},
        },
        "drop_dot": {
            "desc": "5 coins (remove DOT, known negative OOS)",
            "coins": [c for c in ALL_COINS if c != "DOT"],
            "opts": {"signal_fn": generate_signal_v11},
        },
        "tighter_hold": {
            "desc": "max_hold=40 bars (10 hours) instead of 96",
            "coins": ALL_COINS,
            "opts": {"signal_fn": generate_signal_v11, "max_hold_bars": 40},
        },
        "short_bias": {
            "desc": "threshold_short = threshold - 0.5 (easier SHORT entry)",
            "coins": ALL_COINS,
            "opts": {"signal_fn": generate_signal_short_bias, "signal_kwargs": {"short_offset": 0.5}},
        },
        "hour_filter": {
            "desc": "suppress entries 00:00-04:59 UTC",
            "coins": ALL_COINS,
            "opts": {"signal_fn": generate_signal_hour_filter, "signal_kwargs": {"suppress_start": 0, "suppress_end": 5}},
        },
        "stale_exit": {
            "desc": "exit if flat (< 0.1%) after 30 bars",
            "coins": ALL_COINS,
            "opts": {"signal_fn": generate_signal_v11, "stale_exit_bars": 30},
        },
    }

    # ---- Phase 3: Run all scenarios ----
    print(f"\n{'='*70}")
    print("[Phase 2] Running 6 independent scenarios...")
    print(f"{'='*70}")

    all_scenario_results = {}

    for scenario_name, scenario_def in scenarios.items():
        coins = scenario_def["coins"]
        opts = scenario_def["opts"]
        print(f"\n--- {scenario_name}: {scenario_def['desc']} ---")

        coin_results = {}
        total_pnl = 0

        for coin in coins:
            if coin not in alt_test_data:
                continue
            cfg = V11_CONFIGS[coin]
            trades, m = run_coin_scenario(coin, btc_score_ts, alt_test_data[coin], cfg, opts)
            coin_results[coin] = {"trades": trades, "metrics": m}
            total_pnl += m["net_pnl"]
            print(f"  {coin:5s}: {m['total']:3d} trades, WR={m['win_rate']:5.1f}%, "
                  f"PnL=${m['net_pnl']:>+9,.2f}, PF={m['pf']:.3f}, Sharpe={m['sharpe']:.3f}")

        coin_results["TOTAL"] = total_pnl
        all_scenario_results[scenario_name] = coin_results
        print(f"  TOTAL: ${total_pnl:>+10,.2f}")

    # ---- Phase 4: Build combined scenario ----
    print(f"\n{'='*70}")
    print("[Phase 3] Building COMBINED scenario (stack improvements that helped)...")
    print(f"{'='*70}")

    baseline_pnl = all_scenario_results["baseline"]["TOTAL"]
    print(f"\n  Baseline PnL: ${baseline_pnl:+,.2f}")
    print()

    # Determine which improvements helped
    improvements = {}
    for name in ["drop_dot", "tighter_hold", "short_bias", "hour_filter", "stale_exit"]:
        delta = all_scenario_results[name]["TOTAL"] - baseline_pnl
        helped = delta > 0
        improvements[name] = {"delta": delta, "helped": helped}
        status = "HELPED" if helped else "HURT"
        print(f"  {name:<15s}: ${delta:>+9,.2f} ({status})")

    # Stack all that helped
    helpful = [name for name, info in improvements.items() if info["helped"]]
    print(f"\n  Stacking: {', '.join(helpful) if helpful else 'NONE (all hurt!)'}")

    # Build combined options
    combined_coins = ALL_COINS
    combined_opts = {"signal_fn": generate_signal_v11}

    # Determine the right signal function based on what helped
    use_short_bias = "short_bias" in helpful
    use_hour_filter = "hour_filter" in helpful

    if use_short_bias and use_hour_filter:
        # Need a combined signal function
        def generate_signal_combined(btc_score_ts, alt_df, threshold, use_alt_pa,
                                     short_offset=0.5, suppress_start=0, suppress_end=5):
            signal, alt = generate_signal_short_bias(btc_score_ts, alt_df, threshold, use_alt_pa,
                                                      short_offset=short_offset)
            hour = alt["ts"].dt.hour
            is_suppressed = (hour >= suppress_start) & (hour < suppress_end)
            signal[is_suppressed] = 0
            return signal, alt
        combined_opts["signal_fn"] = generate_signal_combined
        combined_opts["signal_kwargs"] = {"short_offset": 0.5, "suppress_start": 0, "suppress_end": 5}
    elif use_short_bias:
        combined_opts["signal_fn"] = generate_signal_short_bias
        combined_opts["signal_kwargs"] = {"short_offset": 0.5}
    elif use_hour_filter:
        combined_opts["signal_fn"] = generate_signal_hour_filter
        combined_opts["signal_kwargs"] = {"suppress_start": 0, "suppress_end": 5}

    if "drop_dot" in helpful:
        combined_coins = [c for c in ALL_COINS if c != "DOT"]
    if "tighter_hold" in helpful:
        combined_opts["max_hold_bars"] = 40
    if "stale_exit" in helpful:
        combined_opts["stale_exit_bars"] = 30

    # Run combined (1x)
    print(f"\n--- combined: stack all positive improvements (1x) ---")
    coin_results_combined = {}
    total_pnl = 0
    for coin in combined_coins:
        if coin not in alt_test_data:
            continue
        cfg = V11_CONFIGS[coin]
        trades, m = run_coin_scenario(coin, btc_score_ts, alt_test_data[coin], cfg, combined_opts)
        coin_results_combined[coin] = {"trades": trades, "metrics": m}
        total_pnl += m["net_pnl"]
        print(f"  {coin:5s}: {m['total']:3d} trades, WR={m['win_rate']:5.1f}%, "
              f"PnL=${m['net_pnl']:>+9,.2f}, PF={m['pf']:.3f}, Sharpe={m['sharpe']:.3f}")
    coin_results_combined["TOTAL"] = total_pnl
    all_scenario_results["combined"] = coin_results_combined
    print(f"  TOTAL: ${total_pnl:>+10,.2f}")

    # Run combined (2x leverage)
    print(f"\n--- combined_2x: same improvements + 2x leverage ---")
    combined_opts_2x = {**combined_opts, "leverage": 2.0}
    coin_results_2x = {}
    total_pnl = 0
    for coin in combined_coins:
        if coin not in alt_test_data:
            continue
        cfg = V11_CONFIGS[coin]
        trades, m = run_coin_scenario(coin, btc_score_ts, alt_test_data[coin], cfg, combined_opts_2x)
        coin_results_2x[coin] = {"trades": trades, "metrics": m}
        total_pnl += m["net_pnl"]
        print(f"  {coin:5s}: {m['total']:3d} trades, WR={m['win_rate']:5.1f}%, "
              f"PnL=${m['net_pnl']:>+9,.2f}, PF={m['pf']:.3f}, Sharpe={m['sharpe']:.3f}")
    coin_results_2x["TOTAL"] = total_pnl
    all_scenario_results["combined_2x"] = coin_results_2x
    print(f"  TOTAL: ${total_pnl:>+10,.2f}")

    # ---- Phase 5: Save everything ----
    print(f"\n{'='*70}")
    print("[Phase 4] Saving experiment data...")
    print(f"{'='*70}")

    exp_id = f"v12_test_{datetime.now().strftime('%Y%m%d_%H%M')}"
    params_desc = {
        "base_configs": "v1.1 paper trading params",
        "test_period": "2026-01-01 to present",
        "improvements_tested": list(scenarios.keys()) + ["combined", "combined_2x"],
        "improvements_that_helped": helpful,
        "combined_includes": helpful,
    }

    save_experiment(exp_id, "v1.2 improvement test: 5 targeted filters + combined",
                    all_scenario_results, params_desc)

    # ---- Phase 6: Final report ----
    print(f"\n{'='*70}")
    print("FINAL COMPARISON TABLE")
    print(f"{'='*70}")

    scenario_names = list(all_scenario_results.keys())
    header = f"{'Coin':<6s}"
    for name in scenario_names:
        header += f" | {name:>14s}"
    print(header)
    print("-" * (6 + 17 * len(scenario_names)))

    for coin in ALL_COINS:
        row = f"{coin:<6s}"
        for name in scenario_names:
            data = all_scenario_results[name].get(coin)
            if data and coin != "TOTAL" and isinstance(data, dict) and "metrics" in data:
                row += f" | ${data['metrics']['net_pnl']:>+10,.2f}"
            else:
                row += f" |       {'N/A':>7s}"
        print(row)

    row = f"{'TOTAL':<6s}"
    for name in scenario_names:
        total = all_scenario_results[name].get("TOTAL", 0)
        row += f" | ${total:>+10,.2f}"
    print(row)

    # Deltas vs baseline
    print(f"\n{'='*70}")
    print("IMPROVEMENT DELTAS vs BASELINE")
    print(f"{'='*70}")
    for name in scenario_names:
        if name == "baseline":
            continue
        delta = all_scenario_results[name]["TOTAL"] - baseline_pnl
        pct = delta / abs(baseline_pnl) * 100 if baseline_pnl != 0 else 0
        marker = ">>>" if name in ["combined", "combined_2x"] else "   "
        print(f"{marker} {name:<15s}: ${delta:>+10,.2f} ({pct:>+6.1f}% vs baseline)")

    # Direction analysis for baseline
    print(f"\n{'='*70}")
    print("DIRECTION ANALYSIS (baseline)")
    print(f"{'='*70}")
    for coin in ALL_COINS:
        data = all_scenario_results["baseline"].get(coin)
        if data and isinstance(data, dict) and "metrics" in data:
            m = data["metrics"]
            print(f"  {coin:5s}: L={m['n_long']:3d} (WR {m['wr_long']:.1f}%) | "
                  f"S={m['n_short']:3d} (WR {m['wr_short']:.1f}%)")

    # Exit reason analysis for baseline
    print(f"\n{'='*70}")
    print("EXIT REASON ANALYSIS (baseline)")
    print(f"{'='*70}")
    for coin in ALL_COINS:
        data = all_scenario_results["baseline"].get(coin)
        if data and isinstance(data, dict) and "trades" in data and not data["trades"].empty:
            t = data["trades"]
            reasons = []
            for reason, grp in t.groupby("exit_reason"):
                reasons.append(f"{reason}:{len(grp)}(${grp['pnl_net'].sum():+.0f})")
            print(f"  {coin:5s}: {', '.join(reasons)}")

    # Verdict
    print(f"\n{'='*70}")
    print("VERDICT")
    print(f"{'='*70}")
    combined_delta = all_scenario_results["combined"]["TOTAL"] - baseline_pnl
    combined_2x_total = all_scenario_results["combined_2x"]["TOTAL"]
    print(f"  Baseline (v1.1):     ${baseline_pnl:>+10,.2f}")
    print(f"  Combined (1x):       ${all_scenario_results['combined']['TOTAL']:>+10,.2f} "
          f"(delta: ${combined_delta:>+,.2f})")
    print(f"  Combined (2x):       ${combined_2x_total:>+10,.2f}")

    if combined_delta > 0:
        print(f"\n  >>> v1.2 BEATS v1.1 by ${combined_delta:,.2f} ({combined_delta/abs(baseline_pnl)*100:+.1f}%)")
        print(f"  >>> Improvements applied: {', '.join(helpful)}")
        print(f"  >>> Production candidate: combined_2x (${combined_2x_total:+,.2f})")
    else:
        print(f"\n  >>> v1.2 does NOT beat v1.1. Keep v1.1 params as-is.")

    print(f"\nExperiment data saved to experiments/")
    print("Done!")


if __name__ == "__main__":
    main()
