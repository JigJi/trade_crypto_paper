"""
Hysteresis Anti-Whipsaw Backtest
================================
Paper trading lost $24.72 in 2h (50 trades, 2% WR) on 2026-03-16 due to
BTC score oscillating around threshold every 15 min. Root cause: entry and
exit use the SAME threshold -- no hysteresis band.

This script tests separate entry/exit thresholds:
  ENTER when score >= T,  EXIT when score < T_exit  (T_exit < T)

Test Matrix (6 experiments):
  1. Baseline      -- current behavior (exit_thr = entry_thr)
  2. Hyst-1.0      -- exit_thr = T - 1.0
  3. Hyst-1.5      -- exit_thr = T - 1.5
  4. Hyst-2.0      -- exit_thr = T - 2.0
  5. Exit-at-zero  -- exit_thr = 0.0
  6. Hyst-50%      -- exit_thr = T * 0.5

All tests use v3 scoring (aligned with paper trading).
OOS period: 2026-01-01 onwards.
6 v3 core coins: BTC, XRP, ADA, DOT, SUI, FIL.
"""

import sys, io, warnings, os, json
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd

from backtest_15m_btc_led_alts import (
    fetch_binance_15m, load_btc_db_data, build_btc_features,
    compute_btc_composite_score, build_alt_technicals,
    run_backtest, calc_metrics,
    BKK_UTC_OFFSET, INIT_EQUITY, BUDGET_USDT, LEVERAGE, FEE, SLIP,
)
from test_v12_improvements import V11_CONFIGS

TEST_START = pd.Timestamp("2026-01-01")
COINS = ["BTC", "XRP", "ADA", "DOT", "SUI", "FIL"]


# ============================================================
# Stateful hysteresis signal generator
# ============================================================

def gen_signal_hysteresis(btc_score_ts, alt_df, entry_threshold, exit_threshold,
                          use_alt_pa):
    """Generate signal with hysteresis band between entry and exit thresholds.

    State machine:
      state=0:  ENTER LONG  if score >= +entry_threshold
                ENTER SHORT if score <= -entry_threshold
      state=1:  STAY LONG   if score >= +exit_threshold
                FLIP SHORT  if score <= -entry_threshold
                EXIT to 0   otherwise (score < exit_thr and > -entry_thr)
      state=-1: STAY SHORT  if score <= -exit_threshold
                FLIP LONG   if score >= +entry_threshold
                EXIT to 0   otherwise (score > -exit_thr and < entry_thr)

    Returns (signal, alt) -- signal is pd.Series of 0/1/-1.
    """
    btc_score_df = btc_score_ts.reset_index()
    btc_score_df.columns = ["ts", "btc_score"]
    alt = alt_df.copy().sort_values("ts")
    alt = pd.merge_asof(alt, btc_score_df.sort_values("ts"),
                        on="ts", direction="backward", tolerance=pd.Timedelta("30min"))
    alt["btc_score"] = alt["btc_score"].fillna(0)

    scores = alt["btc_score"].values
    n = len(scores)
    sig = np.zeros(n, dtype=int)
    state = 0

    for i in range(n):
        s = scores[i]
        if state == 0:
            if s >= entry_threshold:
                state = 1
            elif s <= -entry_threshold:
                state = -1
        elif state == 1:
            if s <= -entry_threshold:
                state = -1          # direct flip to SHORT
            elif s < exit_threshold:
                state = 0           # exit to neutral
        else:  # state == -1
            if s >= entry_threshold:
                state = 1           # direct flip to LONG
            elif s > -exit_threshold:
                state = 0           # exit to neutral

        sig[i] = state

    signal = pd.Series(sig, index=alt.index)

    if use_alt_pa:
        alt_bull_pa = (alt["close"] > alt["ema9"]) & (alt["ema9"] > alt["ema21"])
        alt_bear_pa = (alt["close"] < alt["ema9"]) & (alt["ema9"] < alt["ema21"])
        alt_vol_ok = alt["vol_ratio"] > 0.8
        signal[(signal == 1) & ~(alt_bull_pa & alt_vol_ok)] = 0
        signal[(signal == -1) & ~(alt_bear_pa & alt_vol_ok)] = 0

    return signal, alt


def gen_signal_baseline(btc_score_ts, alt_df, threshold, use_alt_pa):
    """Non-hysteresis baseline (same as gen_signal from test_sltp_optimization)."""
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
        signal[(signal == 1) & ~(alt_bull_pa & alt_vol_ok)] = 0
        signal[(signal == -1) & ~(alt_bear_pa & alt_vol_ok)] = 0

    return signal, alt


# ============================================================
# Metrics helpers
# ============================================================

def whipsaw_metrics(trades_df):
    """Compute whipsaw-specific metrics from trades DataFrame."""
    if trades_df.empty:
        return {"whipsaw_count": 0, "avg_hold_bars": 0, "avg_hold_min": 0,
                "exit_breakdown": {}}

    whipsaw_count = int((trades_df["holding_bars"] <= 2).sum())
    avg_hold = trades_df["holding_bars"].mean()

    # Exit reason breakdown
    breakdown = {}
    for reason in ["SL", "TP", "TRAIL", "SIGNAL_FLIP", "TIMEOUT"]:
        sub = trades_df[trades_df["exit_reason"] == reason]
        breakdown[reason] = {
            "count": len(sub),
            "pnl": round(sub["pnl_net"].sum(), 2) if len(sub) > 0 else 0,
            "wr": round((sub["pnl_net"] > 0).mean() * 100, 1) if len(sub) > 0 else 0,
        }

    return {
        "whipsaw_count": whipsaw_count,
        "avg_hold_bars": round(avg_hold, 1),
        "avg_hold_min": round(avg_hold * 15, 0),
        "exit_breakdown": breakdown,
    }


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


# ============================================================
# Experiment runner
# ============================================================

def run_experiment(btc_score_ts, btc_df, alt_data, config_name,
                   entry_exit_fn, fn_kwargs=None):
    """Run a full experiment across all coins.

    entry_exit_fn: callable(btc_score_ts, alt_df, ...) -> (signal, alt)
    fn_kwargs: extra kwargs per coin (entry_threshold, exit_threshold, etc.)
    """
    all_trades = []
    for coin in COINS:
        cfg = V11_CONFIGS[coin]
        alt_df = alt_data[coin]

        # Build kwargs for signal function
        kwargs = fn_kwargs(cfg) if callable(fn_kwargs) else (fn_kwargs or {})
        signals, alt_merged = entry_exit_fn(btc_score_ts, alt_df, **kwargs)

        oos_mask = alt_merged["ts"] >= TEST_START
        df_oos = alt_merged[oos_mask].reset_index(drop=True)
        sig_oos = signals[oos_mask].reset_index(drop=True)

        trades = run_backtest(
            df_oos, sig_oos,
            sl_atr_mult=cfg["sl"], tp_atr_mult=cfg["tp"],
            trail_atr_mult=cfg["trail"], trail_activate_atr=cfg["trail_act"],
            cooldown_bars=cfg["cd"],
        )

        if not trades.empty:
            trades["coin"] = coin
            all_trades.append(trades)

    if not all_trades:
        return None, {}, {}

    at = pd.concat(all_trades, ignore_index=True)
    at = classify_regime(at, btc_df)
    rm = regime_metrics(at)
    wm = whipsaw_metrics(at)
    return at, rm, wm


# ============================================================
# Display helpers
# ============================================================

def print_header(title):
    print(f"\n{'=' * 80}")
    print(f"  {title}")
    print(f"{'=' * 80}")


def print_result(name, rm, wm):
    m = rm.get("ALL", {})
    if m.get("n", 0) == 0:
        print(f"  {name}: NO TRADES")
        return

    # Core metrics
    print(f"\n  {name}:")
    print(f"    Trades: {m['n']}, WR: {m['wr']:.1f}%, PnL: ${m['pnl']:>,.0f}, "
          f"Sharpe: {m['sharpe']:.2f}, DD: {m['max_dd']:.1f}%")
    print(f"    L: {m['n_long']}({m['long_wr']:.0f}%) S: {m['n_short']}({m['short_wr']:.0f}%)")

    # Whipsaw metrics
    print(f"    Whipsaw (<= 2 bars): {wm['whipsaw_count']}, "
          f"Avg hold: {wm['avg_hold_bars']:.1f} bars ({wm['avg_hold_min']:.0f} min)")

    # Exit breakdown
    bd = wm.get("exit_breakdown", {})
    parts = []
    for reason in ["SL", "TP", "SIGNAL_FLIP", "TIMEOUT", "TRAIL"]:
        d = bd.get(reason, {})
        if d.get("count", 0) > 0:
            parts.append(f"{reason}: {d['count']}(${d['pnl']:+,.0f}, {d['wr']:.0f}%WR)")
    if parts:
        print(f"    Exits: {' | '.join(parts)}")

    # Regime breakdown
    for regime in ["BULL", "BEAR", "FLAT"]:
        r = rm.get(regime, {})
        if r.get("n", 0) > 0:
            print(f"    {regime:4s}: {r['n']:3d} trades, WR {r['wr']:.1f}%, PnL ${r['pnl']:>,.0f}")


# ============================================================
# MAIN
# ============================================================

print("=" * 80)
print("  HYSTERESIS ANTI-WHIPSAW BACKTEST")
print("  Testing separate entry/exit thresholds to reduce signal churn")
print("=" * 80)

# ---- Load data ----
print("\n=== LOADING DATA ===")
btc_ohlcv = fetch_binance_15m("BTCUSDT")
db_data = load_btc_db_data()
btc_df = build_btc_features(btc_ohlcv, db_data)
btc_score = compute_btc_composite_score(btc_df)
btc_score_ts = pd.Series(btc_score.values, index=btc_df["ts"].values)

alt_data = {}
for coin in COINS:
    symbol = f"{coin}USDT"
    ohlcv = fetch_binance_15m(symbol) if coin != "BTC" else btc_ohlcv
    alt_data[coin] = build_alt_technicals(ohlcv)
print(f"  Loaded {len(alt_data)} coins, OOS from {TEST_START}")


# ---- Define experiments ----
EXPERIMENTS = {
    "1_baseline": {
        "desc": "Baseline (no hysteresis, exit_thr = entry_thr)",
        "fn": gen_signal_baseline,
        "kwargs": lambda cfg: {
            "threshold": cfg["threshold"],
            "use_alt_pa": cfg["alt_pa"],
        },
    },
    "2_hyst_1.0": {
        "desc": "Hysteresis -1.0 (exit_thr = T - 1.0)",
        "fn": gen_signal_hysteresis,
        "kwargs": lambda cfg: {
            "entry_threshold": cfg["threshold"],
            "exit_threshold": max(cfg["threshold"] - 1.0, 0.0),
            "use_alt_pa": cfg["alt_pa"],
        },
    },
    "3_hyst_1.5": {
        "desc": "Hysteresis -1.5 (exit_thr = T - 1.5)",
        "fn": gen_signal_hysteresis,
        "kwargs": lambda cfg: {
            "entry_threshold": cfg["threshold"],
            "exit_threshold": max(cfg["threshold"] - 1.5, 0.0),
            "use_alt_pa": cfg["alt_pa"],
        },
    },
    "4_hyst_2.0": {
        "desc": "Hysteresis -2.0 (exit_thr = T - 2.0)",
        "fn": gen_signal_hysteresis,
        "kwargs": lambda cfg: {
            "entry_threshold": cfg["threshold"],
            "exit_threshold": max(cfg["threshold"] - 2.0, 0.0),
            "use_alt_pa": cfg["alt_pa"],
        },
    },
    "5_exit_at_zero": {
        "desc": "Exit at zero (exit_thr = 0.0, only exit when score crosses zero)",
        "fn": gen_signal_hysteresis,
        "kwargs": lambda cfg: {
            "entry_threshold": cfg["threshold"],
            "exit_threshold": 0.0,
            "use_alt_pa": cfg["alt_pa"],
        },
    },
    "6_hyst_50pct": {
        "desc": "Hysteresis 50% (exit_thr = T * 0.5)",
        "fn": gen_signal_hysteresis,
        "kwargs": lambda cfg: {
            "entry_threshold": cfg["threshold"],
            "exit_threshold": cfg["threshold"] * 0.5,
            "use_alt_pa": cfg["alt_pa"],
        },
    },
}


# ---- Run experiments ----
print_header("RUNNING 6 HYSTERESIS EXPERIMENTS")

results = {}
for exp_id, exp in EXPERIMENTS.items():
    print(f"\n  Running: {exp['desc']} ...")
    at, rm, wm = run_experiment(
        btc_score_ts, btc_df, alt_data, exp_id,
        entry_exit_fn=exp["fn"],
        fn_kwargs=exp["kwargs"],
    )
    results[exp_id] = {"trades": at, "regime_metrics": rm, "whipsaw_metrics": wm,
                       "desc": exp["desc"]}
    print_result(exp["desc"], rm, wm)


# ---- Summary comparison table ----
print_header("COMPARISON SUMMARY")

print(f"\n  {'Config':<30s} {'Trades':>6s} {'WR':>6s} {'PnL':>10s} {'Sharpe':>7s} "
      f"{'Whipsaw':>8s} {'AvgHold':>8s} {'SL':>5s} {'TP':>5s} {'FLIP':>5s} {'TO':>5s}")
print(f"  {'-' * 105}")

for exp_id, r in results.items():
    rm = r["regime_metrics"]
    wm = r["whipsaw_metrics"]
    m = rm.get("ALL", {})
    bd = wm.get("exit_breakdown", {})
    if m.get("n", 0) == 0:
        continue

    desc = r["desc"][:30]
    sl_n = bd.get("SL", {}).get("count", 0)
    tp_n = bd.get("TP", {}).get("count", 0)
    flip_n = bd.get("SIGNAL_FLIP", {}).get("count", 0)
    to_n = bd.get("TIMEOUT", {}).get("count", 0)

    print(f"  {desc:<30s} {m['n']:>6d} {m['wr']:>5.1f}% ${m['pnl']:>8,.0f} {m['sharpe']:>7.2f} "
          f"{wm['whipsaw_count']:>8d} {wm['avg_hold_bars']:>6.1f}b "
          f"{sl_n:>5d} {tp_n:>5d} {flip_n:>5d} {to_n:>5d}")


# ---- Per-coin breakdown for best hysteresis config ----
print_header("PER-COIN BREAKDOWN (Best Hysteresis vs Baseline)")

# Find best hysteresis by PnL (excluding baseline)
hyst_results = {k: v for k, v in results.items() if k != "1_baseline"}
best_hyst_id = max(hyst_results, key=lambda k: hyst_results[k]["regime_metrics"].get("ALL", {}).get("pnl", 0))
best_hyst = results[best_hyst_id]
baseline = results["1_baseline"]

print(f"\n  Best hysteresis: {best_hyst['desc']}")
print(f"\n  {'Coin':<6s} | {'--- Baseline ---':^30s} | {'--- Best Hysteresis ---':^30s} | {'Delta':>8s}")
print(f"  {'':6s} | {'Trades':>6s} {'WR':>6s} {'PnL':>8s} {'Hold':>6s} | {'Trades':>6s} {'WR':>6s} {'PnL':>8s} {'Hold':>6s} |")
print(f"  {'-' * 85}")

for coin in COINS:
    # Baseline coin stats
    bt = baseline["trades"]
    bt_coin = bt[bt["coin"] == coin] if bt is not None else pd.DataFrame()
    # Best hysteresis coin stats
    ht = best_hyst["trades"]
    ht_coin = ht[ht["coin"] == coin] if ht is not None else pd.DataFrame()

    def coin_stats(df):
        if df.empty:
            return 0, 0.0, 0.0, 0.0
        n = len(df)
        wr = (df["pnl_net"] > 0).sum() / n * 100
        pnl = df["pnl_net"].sum()
        hold = df["holding_bars"].mean()
        return n, wr, pnl, hold

    bn, bwr, bpnl, bhold = coin_stats(bt_coin)
    hn, hwr, hpnl, hhold = coin_stats(ht_coin)
    delta = hpnl - bpnl

    print(f"  {coin:<6s} | {bn:>6d} {bwr:>5.1f}% ${bpnl:>7,.0f} {bhold:>5.1f}b | "
          f"{hn:>6d} {hwr:>5.1f}% ${hpnl:>7,.0f} {hhold:>5.1f}b | ${delta:>+7,.0f}")


# ---- Signal state analysis: how often does hysteresis "save" a trade? ----
print_header("SIGNAL PERSISTENCE ANALYSIS")

# Compare signal series: count transitions (state changes) per experiment
for exp_id, exp in EXPERIMENTS.items():
    # Re-generate signals for BTC to count transitions
    cfg = V11_CONFIGS["BTC"]
    kwargs = exp["kwargs"](cfg)
    sig, _ = exp["fn"](btc_score_ts, alt_data["BTC"], **kwargs)

    oos_mask = alt_data["BTC"]["ts"] >= TEST_START
    sig_oos = sig[oos_mask].reset_index(drop=True)
    transitions = (sig_oos.diff().fillna(0) != 0).sum()
    active_bars = (sig_oos != 0).sum()
    total_bars = len(sig_oos)

    print(f"  {results[exp_id]['desc'][:35]:<35s}: "
          f"{transitions:>4d} transitions, {active_bars:>5d}/{total_bars} active bars "
          f"({active_bars/total_bars*100:.1f}%)")


# ---- Save results ----
print_header("SAVING RESULTS")

os.makedirs("experiments", exist_ok=True)

save_data = {
    "timestamp": datetime.now().isoformat(),
    "test": "hysteresis_anti_whipsaw",
    "test_period": f"{TEST_START} to present (OOS)",
    "scoring": "v3 (aligned with paper trading)",
    "coins": COINS,
    "per_coin_thresholds": {c: V11_CONFIGS[c]["threshold"] for c in COINS},
    "experiments": {},
}

for exp_id, r in results.items():
    rm = r["regime_metrics"]
    wm = r["whipsaw_metrics"]
    # Remove non-serializable exit_breakdown nested dicts (already have counts)
    wm_save = {k: v for k, v in wm.items()}
    save_data["experiments"][exp_id] = {
        "description": r["desc"],
        "regime_metrics": rm,
        "whipsaw_metrics": wm_save,
    }

with open("experiments/hysteresis_results.json", "w") as f:
    json.dump(save_data, f, indent=2, default=str)

print(f"  Saved to experiments/hysteresis_results.json")

# ---- Recommendation ----
print_header("RECOMMENDATION")

bl_all = baseline["regime_metrics"].get("ALL", {})
bh_all = best_hyst["regime_metrics"].get("ALL", {})
bh_wm = best_hyst["whipsaw_metrics"]
bl_wm = baseline["whipsaw_metrics"]

print(f"""
  Baseline:       {bl_all.get('n',0)} trades, {bl_all.get('wr',0):.1f}% WR, ${bl_all.get('pnl',0):,.0f} PnL, {bl_wm.get('whipsaw_count',0)} whipsaws
  Best Hysteresis: {bh_all.get('n',0)} trades, {bh_all.get('wr',0):.1f}% WR, ${bh_all.get('pnl',0):,.0f} PnL, {bh_wm.get('whipsaw_count',0)} whipsaws
  Config:          {best_hyst['desc']}

  Delta: {bh_all.get('n',0) - bl_all.get('n',0):+d} trades, {bh_all.get('wr',0) - bl_all.get('wr',0):+.1f}% WR, ${bh_all.get('pnl',0) - bl_all.get('pnl',0):+,.0f} PnL, {bh_wm.get('whipsaw_count',0) - bl_wm.get('whipsaw_count',0):+d} whipsaws
""")

if bh_all.get('pnl', 0) >= bl_all.get('pnl', 0) and bh_wm.get('whipsaw_count', 0) < bl_wm.get('whipsaw_count', 0):
    print("  >>> HYSTERESIS HELPS: Higher PnL AND fewer whipsaws. Deploy to paper trading.")
elif bh_wm.get('whipsaw_count', 0) < bl_wm.get('whipsaw_count', 0):
    pnl_diff = bh_all.get('pnl', 0) - bl_all.get('pnl', 0)
    whip_diff = bl_wm.get('whipsaw_count', 0) - bh_wm.get('whipsaw_count', 0)
    print(f"  >>> TRADEOFF: {whip_diff} fewer whipsaws but ${abs(pnl_diff):,.0f} less PnL. Evaluate cost-benefit.")
else:
    print("  >>> NO BENEFIT: Hysteresis doesn't reduce whipsaws in backtest. Investigate other causes.")

print("\nDONE!")
