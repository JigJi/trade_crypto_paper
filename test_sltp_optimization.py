"""
SL/TP Optimization Tests (Steps 1.3, 1.4, 1.5)
=================================================
Tests:
  1.3 SHORT-only mode vs baseline vs asymmetric threshold
  1.4 Breakeven stop at various activation levels
  1.5 Grid search SL/TP ratios with breakeven

All tests use v3 scoring (aligned with paper trading).
Results split by BULL/BEAR/FLAT regime.
"""

import sys, io, warnings, os, json
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
import pandas_ta as ta

from backtest_15m_btc_led_alts import (
    fetch_binance_15m, load_btc_db_data, build_btc_features,
    compute_btc_composite_score, build_alt_technicals,
    run_backtest, calc_metrics,
    BKK_UTC_OFFSET, INIT_EQUITY, BUDGET_USDT, LEVERAGE, FEE, SLIP,
)
from test_v12_improvements import V11_CONFIGS

TEST_START = pd.Timestamp("2026-01-01")
COINS = ["BTC", "XRP", "ADA", "DOT", "SUI", "FIL"]


def gen_signal(btc_score_ts, alt_df, threshold, use_alt_pa,
               direction_filter=None, long_threshold_offset=0.0):
    """Generate signal with optional direction filter and asymmetric thresholds.

    direction_filter: None=both, 'SHORT'=short only, 'LONG'=long only
    long_threshold_offset: extra threshold for LONG signals (asymmetric mode)
    """
    btc_score_df = btc_score_ts.reset_index()
    btc_score_df.columns = ["ts", "btc_score"]
    alt = alt_df.copy().sort_values("ts")
    alt = pd.merge_asof(alt, btc_score_df.sort_values("ts"),
                        on="ts", direction="backward", tolerance=pd.Timedelta("30min"))
    alt["btc_score"] = alt["btc_score"].fillna(0)

    signal = pd.Series(0, index=alt.index)
    long_thr = threshold + long_threshold_offset
    signal[alt["btc_score"] >= long_thr] = 1
    signal[alt["btc_score"] <= -threshold] = -1

    if use_alt_pa:
        alt_bull_pa = (alt["close"] > alt["ema9"]) & (alt["ema9"] > alt["ema21"])
        alt_bear_pa = (alt["close"] < alt["ema9"]) & (alt["ema9"] < alt["ema21"])
        alt_vol_ok = alt["vol_ratio"] > 0.8
        signal[(signal == 1) & ~(alt_bull_pa & alt_vol_ok)] = 0
        signal[(signal == -1) & ~(alt_bear_pa & alt_vol_ok)] = 0

    if direction_filter == "SHORT":
        signal[signal == 1] = 0
    elif direction_filter == "LONG":
        signal[signal == -1] = 0

    return signal, alt


def run_backtest_be(df, signals, sl_atr_mult=2.0, tp_atr_mult=3.0,
                    trail_atr_mult=0.5, trail_activate_atr=0.5,
                    max_hold_bars=96, cooldown_bars=4,
                    be_bars=None, be_activation_pct=None):
    """Extended run_backtest with breakeven stop support.

    be_bars: after N bars, check for breakeven stop activation
    be_activation_pct: minimum unrealized profit % to activate breakeven (e.g. 0.003 = 0.3%)
    """
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
    be_active = False  # breakeven stop activated
    last_exit_i = -cooldown_bars - 1
    use_be = be_bars is not None and be_activation_pct is not None

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
            be_active = False
            continue

        if position != 0:
            h, l, c, o = highs[i], lows[i], closes[i], opens[i]
            atr = entry_atr

            # Breakeven stop: after be_bars, if max favorable excursion > activation,
            # move SL to entry price
            if use_be and not be_active and (i - entry_i) >= be_bars:
                if position == 1:
                    max_favorable = (peak - entry_px) / entry_px
                else:
                    max_favorable = (entry_px - trough) / entry_px
                if max_favorable >= be_activation_pct:
                    be_active = True

            if position == 1:
                peak = max(peak, h)
                sl_level = entry_px if be_active else entry_px - sl_atr_mult * atr
                tp_level = entry_px + tp_atr_mult * atr
            else:
                trough = min(trough, l)
                sl_level = entry_px if be_active else entry_px + sl_atr_mult * atr
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
                if l <= sl_level:
                    exit_px = sl_level
                    exit_reason = "BE" if be_active else "SL"
                elif trl_active and trail_stop and l <= trail_stop:
                    exit_px, exit_reason = trail_stop, "TRAIL"
                elif h >= tp_level:
                    exit_px, exit_reason = tp_level, "TP"
            else:
                if h >= sl_level:
                    exit_px = sl_level
                    exit_reason = "BE" if be_active else "SL"
                elif trl_active and trail_stop and h >= trail_stop:
                    exit_px, exit_reason = trail_stop, "TRAIL"
                elif l <= tp_level:
                    exit_px, exit_reason = tp_level, "TP"

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


def classify_regime(trades_df, btc_df):
    """Add BULL/BEAR/FLAT regime to trades based on weekly BTC return."""
    btc_weekly = btc_df.set_index("ts")["close"].resample("W").last().pct_change() * 100
    trades_df = trades_df.copy()
    trades_df["entry_time"] = pd.to_datetime(trades_df["entry_time"])
    trades_df["week"] = trades_df["entry_time"].dt.to_period("W").dt.start_time
    regimes = []
    for _, row in trades_df.iterrows():
        week_start = row["week"]
        diffs = abs(btc_weekly.index - week_start)
        if len(diffs) == 0:
            regimes.append("UNKNOWN")
            continue
        closest_idx = diffs.argmin()
        wk_ret = btc_weekly.iloc[closest_idx]
        if wk_ret > 2:
            regimes.append("BULL")
        elif wk_ret < -2:
            regimes.append("BEAR")
        else:
            regimes.append("FLAT")
    trades_df["regime"] = regimes
    return trades_df


def regime_metrics(trades_df):
    """Compute metrics per regime."""
    results = {}
    for regime in ["BULL", "BEAR", "FLAT", "ALL"]:
        sub = trades_df if regime == "ALL" else trades_df[trades_df["regime"] == regime]
        if len(sub) == 0:
            results[regime] = {"n": 0, "wr": 0, "pnl": 0, "sharpe": 0, "max_dd": 0,
                               "long_wr": 0, "short_wr": 0, "n_long": 0, "n_short": 0}
            continue
        n = len(sub)
        wins = (sub["pnl_net"] > 0).sum()
        wr = wins / n * 100
        pnl = sub["pnl_net"].sum()
        rets = sub["pnl_net"] / BUDGET_USDT
        sharpe = rets.mean() / rets.std() * np.sqrt(n) if len(rets) > 1 and rets.std() > 0 else 0
        eq = INIT_EQUITY + sub["pnl_net"].cumsum()
        eq_full = pd.concat([pd.Series([INIT_EQUITY]), eq]).reset_index(drop=True)
        dd = ((eq_full - eq_full.cummax()) / eq_full.cummax()).min() * 100
        longs = sub[sub["dir"] == "L"]
        shorts = sub[sub["dir"] == "S"]
        l_wr = (longs["pnl_net"] > 0).sum() / max(len(longs), 1) * 100
        s_wr = (shorts["pnl_net"] > 0).sum() / max(len(shorts), 1) * 100
        results[regime] = {
            "n": n, "wr": round(wr, 1), "pnl": round(pnl, 0),
            "sharpe": round(sharpe, 2), "max_dd": round(dd, 1),
            "long_wr": round(l_wr, 1), "short_wr": round(s_wr, 1),
            "n_long": len(longs), "n_short": len(shorts),
        }
    return results


def run_experiment(btc_score_ts, btc_df, alt_data, config_name,
                   direction_filter=None, long_threshold_offset=0.0,
                   sl_mult=None, tp_mult=None,
                   be_bars=None, be_activation_pct=None):
    """Run a full experiment across all coins."""
    all_trades = []
    for coin in COINS:
        cfg = V11_CONFIGS[coin]
        alt_df = alt_data[coin]

        signals, alt_merged = gen_signal(
            btc_score_ts, alt_df, cfg["threshold"], cfg["alt_pa"],
            direction_filter=direction_filter,
            long_threshold_offset=long_threshold_offset
        )

        oos_mask = alt_merged["ts"] >= TEST_START
        df_oos = alt_merged[oos_mask].reset_index(drop=True)
        sig_oos = signals[oos_mask].reset_index(drop=True)

        sl = sl_mult if sl_mult is not None else cfg["sl"]
        tp = tp_mult if tp_mult is not None else cfg["tp"]

        if be_bars is not None:
            trades = run_backtest_be(
                df_oos, sig_oos,
                sl_atr_mult=sl, tp_atr_mult=tp,
                trail_atr_mult=cfg["trail"], trail_activate_atr=cfg["trail_act"],
                cooldown_bars=cfg["cd"],
                be_bars=be_bars, be_activation_pct=be_activation_pct
            )
        else:
            trades = run_backtest(
                df_oos, sig_oos,
                sl_atr_mult=sl, tp_atr_mult=tp,
                trail_atr_mult=cfg["trail"], trail_activate_atr=cfg["trail_act"],
                cooldown_bars=cfg["cd"]
            )

        if not trades.empty:
            trades["coin"] = coin
            all_trades.append(trades)

    if not all_trades:
        return None, {}

    at = pd.concat(all_trades, ignore_index=True)
    at = classify_regime(at, btc_df)
    metrics = regime_metrics(at)
    return at, metrics


def print_metrics(name, metrics):
    print(f"\n  {name}:")
    for regime in ["ALL", "BULL", "BEAR", "FLAT"]:
        m = metrics.get(regime, {})
        if m.get("n", 0) == 0:
            continue
        print(f"    {regime:4s}: {m['n']:3d} trades, WR {m['wr']:.1f}%, "
              f"PnL ${m['pnl']:>8,.0f}, Sharpe {m['sharpe']:.2f}, DD {m['max_dd']:.1f}% | "
              f"L {m['n_long']}({m['long_wr']:.0f}%) S {m['n_short']}({m['short_wr']:.0f}%)")


# ============================================================
# MAIN
# ============================================================

print("=" * 70)
print("SL/TP OPTIMIZATION (Steps 1.3, 1.4, 1.5)")
print("=" * 70)

# Load data
print("\n=== LOADING DATA ===")
btc_ohlcv = fetch_binance_15m("BTCUSDT")
db_data = load_btc_db_data()
btc_df = build_btc_features(btc_ohlcv, db_data)
btc_score = compute_btc_composite_score(btc_df)
btc_score_ts = pd.Series(btc_score.values, index=btc_df["ts"].values)

# Preload alt data
alt_data = {}
for coin in COINS:
    symbol = f"{coin}USDT"
    ohlcv = fetch_binance_15m(symbol) if coin != "BTC" else btc_ohlcv
    alt_data[coin] = build_alt_technicals(ohlcv)
print(f"  Loaded {len(alt_data)} coins")


# ============================================================
# Step 1.3: SHORT-only vs baseline vs asymmetric threshold
# ============================================================
print("\n" + "=" * 70)
print("STEP 1.3: DIRECTION FILTER TESTS")
print("=" * 70)

experiments_13 = {}

# 1. Baseline (both directions)
at, m = run_experiment(btc_score_ts, btc_df, alt_data, "baseline")
experiments_13["baseline"] = m
print_metrics("Baseline (LONG+SHORT)", m)

# 2. SHORT-only
at, m = run_experiment(btc_score_ts, btc_df, alt_data, "short_only",
                       direction_filter="SHORT")
experiments_13["short_only"] = m
print_metrics("SHORT-only", m)

# 3. Asymmetric threshold (+1.0 for LONG)
at, m = run_experiment(btc_score_ts, btc_df, alt_data, "asym_1.0",
                       long_threshold_offset=1.0)
experiments_13["asym_1.0"] = m
print_metrics("Asymmetric (LONG +1.0)", m)

# 4. Asymmetric threshold (+1.5 for LONG)
at, m = run_experiment(btc_score_ts, btc_df, alt_data, "asym_1.5",
                       long_threshold_offset=1.5)
experiments_13["asym_1.5"] = m
print_metrics("Asymmetric (LONG +1.5)", m)

# 5. Asymmetric threshold (+2.0 for LONG)
at, m = run_experiment(btc_score_ts, btc_df, alt_data, "asym_2.0",
                       long_threshold_offset=2.0)
experiments_13["asym_2.0"] = m
print_metrics("Asymmetric (LONG +2.0)", m)


# ============================================================
# Step 1.4: Breakeven stop tests
# ============================================================
print("\n" + "=" * 70)
print("STEP 1.4: BREAKEVEN STOP TESTS")
print("=" * 70)

experiments_14 = {}

for be_bars in [4, 8, 12]:
    for be_pct in [0.003, 0.005, 0.010]:
        name = f"BE_{be_bars}bars_{be_pct*100:.1f}pct"
        at, m = run_experiment(btc_score_ts, btc_df, alt_data, name,
                               be_bars=be_bars, be_activation_pct=be_pct)
        experiments_14[name] = m
        all_m = m.get("ALL", {})
        if all_m.get("n", 0) > 0:
            # Count BE exits
            n_be = len(at[at["exit_reason"] == "BE"]) if at is not None else 0
            n_sl = len(at[at["exit_reason"] == "SL"]) if at is not None else 0
            be_pnl = at[at["exit_reason"] == "BE"]["pnl_net"].sum() if at is not None and n_be > 0 else 0
            sl_pnl = at[at["exit_reason"] == "SL"]["pnl_net"].sum() if at is not None and n_sl > 0 else 0
            print(f"  {name}: {all_m['n']} trades, WR {all_m['wr']:.1f}%, "
                  f"PnL ${all_m['pnl']:,.0f}, Sharpe {all_m['sharpe']:.2f} | "
                  f"BE exits: {n_be} (${be_pnl:,.0f}), SL: {n_sl} (${sl_pnl:,.0f})")


# ============================================================
# Step 1.5: Grid search SL/TP ratios
# ============================================================
print("\n" + "=" * 70)
print("STEP 1.5: SL/TP GRID SEARCH")
print("=" * 70)

experiments_15 = []

# Find best breakeven config from step 1.4
best_be_name = max(experiments_14, key=lambda k: experiments_14[k].get("ALL", {}).get("pnl", 0))
best_be_parts = best_be_name.split("_")
best_be_bars = int(best_be_parts[1].replace("bars", ""))
best_be_pct = float(best_be_parts[2].replace("pct", "")) / 100
print(f"\n  Best breakeven from 1.4: {best_be_name}")

sl_range = [1.5, 2.0, 2.5, 3.0, 3.5]
tp_range = [2.0, 3.0, 4.0, 5.0, 6.0]

for use_be in [False, True]:
    for sl in sl_range:
        for tp in tp_range:
            if tp < sl * 1.2:  # skip unreasonable combos
                continue

            be_b = best_be_bars if use_be else None
            be_p = best_be_pct if use_be else None

            at, m = run_experiment(
                btc_score_ts, btc_df, alt_data,
                f"SL{sl}_TP{tp}_BE{use_be}",
                sl_mult=sl, tp_mult=tp,
                be_bars=be_b, be_activation_pct=be_p
            )
            all_m = m.get("ALL", {})
            if all_m.get("n", 0) > 0:
                row = {
                    "sl": sl, "tp": tp, "breakeven": use_be,
                    **{f"{regime}_{k}": v for regime, rm in m.items() for k, v in rm.items()},
                }
                experiments_15.append(row)

# Sort by ALL_pnl and show top 10
experiments_15.sort(key=lambda x: x.get("ALL_pnl", 0), reverse=True)

print(f"\n  Top 10 SL/TP configs (by ALL PnL):")
print(f"  {'SL':>4s} {'TP':>4s} {'BE':>5s} | {'ALL PnL':>10s} {'Sharpe':>7s} {'WR':>6s} | "
      f"{'BULL PnL':>10s} {'BEAR PnL':>10s} {'FLAT PnL':>10s}")
print(f"  {'-'*80}")

for r in experiments_15[:10]:
    print(f"  {r['sl']:>4.1f} {r['tp']:>4.1f} {str(r['breakeven']):>5s} | "
          f"${r.get('ALL_pnl', 0):>9,.0f} {r.get('ALL_sharpe', 0):>7.2f} {r.get('ALL_wr', 0):>5.1f}% | "
          f"${r.get('BULL_pnl', 0):>9,.0f} ${r.get('BEAR_pnl', 0):>9,.0f} ${r.get('FLAT_pnl', 0):>9,.0f}")

# Also show best SHORT-only + best SL/TP
print(f"\n\n  Top 5 SHORT-only SL/TP configs:")
so_experiments = []
for sl in sl_range:
    for tp in tp_range:
        if tp < sl * 1.2:
            continue
        at, m = run_experiment(
            btc_score_ts, btc_df, alt_data,
            f"SO_SL{sl}_TP{tp}",
            direction_filter="SHORT",
            sl_mult=sl, tp_mult=tp
        )
        all_m = m.get("ALL", {})
        if all_m.get("n", 0) > 0:
            row = {
                "sl": sl, "tp": tp,
                **{f"{regime}_{k}": v for regime, rm in m.items() for k, v in rm.items()},
            }
            so_experiments.append(row)

so_experiments.sort(key=lambda x: x.get("ALL_pnl", 0), reverse=True)

print(f"  {'SL':>4s} {'TP':>4s} | {'ALL PnL':>10s} {'Sharpe':>7s} {'WR':>6s} | "
      f"{'BULL PnL':>10s} {'BEAR PnL':>10s} {'FLAT PnL':>10s}")
print(f"  {'-'*75}")
for r in so_experiments[:5]:
    print(f"  {r['sl']:>4.1f} {r['tp']:>4.1f} | "
          f"${r.get('ALL_pnl', 0):>9,.0f} {r.get('ALL_sharpe', 0):>7.2f} {r.get('ALL_wr', 0):>5.1f}% | "
          f"${r.get('BULL_pnl', 0):>9,.0f} ${r.get('BEAR_pnl', 0):>9,.0f} ${r.get('FLAT_pnl', 0):>9,.0f}")


# ============================================================
# Save all results
# ============================================================
os.makedirs("experiments", exist_ok=True)

results_all = {
    "timestamp": datetime.now().isoformat(),
    "test_period": "2026-01-01 to present (OOS)",
    "scoring": "v3 (aligned with paper trading)",
    "step_1_3_direction_filter": experiments_13,
    "step_1_4_breakeven_stop": experiments_14,
    "step_1_5_sltp_grid_top10": experiments_15[:10],
    "step_1_5_short_only_top5": so_experiments[:5],
}

with open("experiments/sltp_optimization_results.json", "w") as f:
    json.dump(results_all, f, indent=2, default=str)

print("\n\nSaved to experiments/sltp_optimization_results.json")
print("\nDONE!")
