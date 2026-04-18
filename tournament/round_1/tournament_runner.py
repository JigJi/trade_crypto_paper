"""
Tournament Round 1: Strategy Evolution
Tests multiple contender strategies against v3 baseline.
Runs on 6 core coins OOS Jan 2025 - Mar 2026.
"""
import sys, os, json, time
from pathlib import Path
from datetime import datetime

BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR))
os.chdir(str(BASE_DIR))

import pandas as pd
import numpy as np
from backtest_15m_btc_led_alts import (
    fetch_binance_15m, load_btc_db_data, build_btc_features,
    compute_btc_composite_score, build_alt_technicals,
    generate_btc_led_signal, run_backtest, calc_metrics,
    score_basis_contrarian, score_tick_liq, score_ob_combined,
    V3_EXTRA_WEIGHTS, COMPOSITE_WEIGHTS,
)

# ── Config ──────────────────────────────────────────────
OOS_START = "2025-01-01"
OOS_END   = "2026-03-18"
COINS = ["BTC", "XRP", "ADA", "DOT", "SUI", "FIL"]

V11_CONFIGS = {
    "BTC": {"threshold": 2.5, "alt_pa": False, "sl": 2.5, "tp": 4.0, "trail": 99, "trail_act": 99, "cd": 4},
    "XRP": {"threshold": 3.5, "alt_pa": False, "sl": 2.5, "tp": 4.0, "trail": 99, "trail_act": 99, "cd": 4},
    "ADA": {"threshold": 3.5, "alt_pa": False, "sl": 2.5, "tp": 4.0, "trail": 99, "trail_act": 99, "cd": 4},
    "DOT": {"threshold": 3.0, "alt_pa": False, "sl": 2.5, "tp": 4.0, "trail": 99, "trail_act": 99, "cd": 4},
    "SUI": {"threshold": 3.0, "alt_pa": False, "sl": 2.5, "tp": 4.0, "trail": 99, "trail_act": 99, "cd": 4},
    "FIL": {"threshold": 3.0, "alt_pa": False, "sl": 2.5, "tp": 4.0, "trail": 99, "trail_act": 99, "cd": 4},
}

RESULTS_DIR = Path(__file__).parent
RESULTS_FILE = RESULTS_DIR / "results.json"

# ── Data Loading (once) ─────────────────────────────────
print("=" * 70)
print("TOURNAMENT ROUND 1 -- Loading data...")
print("=" * 70)

t0 = time.time()
btc_ohlcv = fetch_binance_15m("BTCUSDT", years=3)
db_data = load_btc_db_data()
btc_df = build_btc_features(btc_ohlcv, db_data)
print(f"BTC features: {len(btc_df)} bars ({time.time()-t0:.1f}s)")

# Pre-load all alt data
alt_data = {}
for coin in COINS:
    sym = f"{coin}USDT"
    ohlcv = fetch_binance_15m(sym, years=3)
    alt_data[coin] = build_alt_technicals(ohlcv)
    print(f"  {coin}: {len(alt_data[coin])} bars")

print(f"Data loaded in {time.time()-t0:.1f}s\n")


# ── Helper: Run a contender ─────────────────────────────
def run_contender(name, description, btc_score_fn=None, signal_modifier=None,
                  coin_overrides=None, extra_weights=None):
    """
    Run a contender strategy on all coins.

    btc_score_fn: callable(btc_df) -> pd.Series (custom BTC score)
    signal_modifier: callable(signals, alt_merged, coin) -> signals (post-process)
    coin_overrides: dict of {coin: {param: value}} overrides on V11_CONFIGS
    extra_weights: dict override for V3_EXTRA_WEIGHTS
    """
    print(f"\n{'='*60}")
    print(f"CONTENDER: {name}")
    print(f"  {description}")
    print(f"{'='*60}")

    t1 = time.time()

    # Compute BTC score
    if btc_score_fn:
        btc_score = btc_score_fn(btc_df)
    elif extra_weights:
        # Temporarily override V3_EXTRA_WEIGHTS
        import backtest_15m_btc_led_alts as bt
        old_extra = dict(bt.V3_EXTRA_WEIGHTS)
        bt.V3_EXTRA_WEIGHTS.update(extra_weights)
        btc_score = compute_btc_composite_score(btc_df)
        bt.V3_EXTRA_WEIGHTS.update(old_extra)
    else:
        btc_score = compute_btc_composite_score(btc_df)

    btc_score_ts = pd.Series(btc_score.values, index=btc_df["ts"].values)

    all_trades = []
    coin_results = {}

    for coin in COINS:
        cfg = dict(V11_CONFIGS.get(coin, V11_CONFIGS["DOT"]))
        if coin_overrides and coin in coin_overrides:
            cfg.update(coin_overrides[coin])
        elif coin_overrides and "__all__" in coin_overrides:
            cfg.update(coin_overrides["__all__"])

        alt_df = alt_data[coin]

        signals, alt_merged = generate_btc_led_signal(
            btc_score_ts, alt_df,
            threshold=cfg["threshold"],
            use_alt_pa_filter=cfg.get("alt_pa", False),
            spike_mode=cfg.get("spike_mode", None),
        )

        # Apply signal modifier if provided
        if signal_modifier:
            signals = signal_modifier(signals, alt_merged, coin, btc_score_ts)

        # OOS filter
        oos_mask = alt_merged["ts"] >= pd.Timestamp(OOS_START)
        if OOS_END:
            oos_mask &= alt_merged["ts"] <= pd.Timestamp(OOS_END)

        alt_oos = alt_merged[oos_mask].reset_index(drop=True)
        sig_oos = signals[oos_mask].reset_index(drop=True)

        if len(alt_oos) < 100:
            print(f"  {coin}: insufficient OOS data ({len(alt_oos)} bars)")
            continue

        trades = run_backtest(
            alt_oos, sig_oos,
            sl_atr_mult=cfg.get("sl", 2.5),
            tp_atr_mult=cfg.get("tp", 4.0),
            trail_atr_mult=cfg.get("trail", 99),
            trail_activate_atr=cfg.get("trail_act", 99),
            max_hold_bars=cfg.get("max_hold", 96),
            cooldown_bars=cfg.get("cd", 4),
        )

        if len(trades) > 0:
            m = calc_metrics(trades, len(alt_oos))
            coin_results[coin] = m
            trades["coin"] = coin
            all_trades.append(trades)
            print(f"  {coin}: {m['total']} trades, WR {m['win_rate']:.1f}%, "
                  f"PnL ${m['net_pnl']:+,.0f}, Sharpe {m['sharpe']:.2f}")
        else:
            print(f"  {coin}: 0 trades")

    # Aggregate
    if all_trades:
        combined = pd.concat(all_trades, ignore_index=True)
        total_pnl = combined["pnl_net"].sum()
        total_trades = len(combined)
        total_wins = (combined["pnl_net"] > 0).sum()
        total_wr = 100 * total_wins / total_trades if total_trades > 0 else 0

        # Direction breakdown
        longs = combined[combined["dir"] == "L"]
        shorts = combined[combined["dir"] == "S"]
        long_wr = 100 * (longs["pnl_net"] > 0).sum() / len(longs) if len(longs) > 0 else 0
        short_wr = 100 * (shorts["pnl_net"] > 0).sum() / len(shorts) if len(shorts) > 0 else 0
        long_pnl = longs["pnl_net"].sum() if len(longs) > 0 else 0
        short_pnl = shorts["pnl_net"].sum() if len(shorts) > 0 else 0

        # Max DD
        equity = 10000 + combined["pnl_net"].cumsum()
        peak = equity.cummax()
        dd = (equity - peak) / peak * 100
        max_dd = dd.min()

        # Sharpe (annualized from 15m bars)
        bars_per_year = 4 * 24 * 365
        ret_per_trade = combined["pnl_net"] / 1000  # budget per trade
        sharpe = (ret_per_trade.mean() / ret_per_trade.std() * np.sqrt(total_trades)
                  if ret_per_trade.std() > 0 else 0)
    else:
        total_pnl = 0; total_trades = 0; total_wr = 0
        long_wr = 0; short_wr = 0; long_pnl = 0; short_pnl = 0
        max_dd = 0; sharpe = 0

    elapsed = time.time() - t1

    result = {
        "name": name,
        "description": description,
        "total_pnl": round(total_pnl, 2),
        "total_trades": total_trades,
        "win_rate": round(total_wr, 1),
        "sharpe": round(sharpe, 2),
        "max_dd_pct": round(max_dd, 2),
        "long_trades": len(longs) if all_trades else 0,
        "long_wr": round(long_wr, 1),
        "long_pnl": round(long_pnl, 2),
        "short_trades": len(shorts) if all_trades else 0,
        "short_wr": round(short_wr, 1),
        "short_pnl": round(short_pnl, 2),
        "coin_results": {c: {"pnl": round(m["net_pnl"], 2), "wr": round(m["win_rate"], 1),
                              "trades": m["total"], "sharpe": round(m["sharpe"], 2)}
                         for c, m in coin_results.items()},
        "elapsed_s": round(elapsed, 1),
    }

    print(f"\n  TOTAL: {total_trades} trades | WR {total_wr:.1f}% | "
          f"PnL ${total_pnl:+,.0f} | Sharpe {sharpe:.2f} | DD {max_dd:.1f}%")
    print(f"  LONG: {result['long_trades']} trades, WR {long_wr:.1f}%, ${long_pnl:+,.0f}")
    print(f"  SHORT: {result['short_trades']} trades, WR {short_wr:.1f}%, ${short_pnl:+,.0f}")
    print(f"  ({elapsed:.1f}s)")

    return result


# ── Signal Modifiers ────────────────────────────────────
def asymmetric_threshold(long_thr, short_thr):
    """Post-process signals: higher threshold for LONGs."""
    def modifier(signals, alt_merged, coin, btc_score_ts):
        sig = signals.copy()
        # Get BTC score aligned with alt data
        if "btc_score" in alt_merged.columns:
            scores = alt_merged["btc_score"].values
        else:
            # Reconstruct from btc_score_ts
            scores = btc_score_ts.reindex(alt_merged["ts"]).ffill().values

        for i in range(len(sig)):
            if sig.iloc[i] == 1:  # LONG
                score_val = scores[i] if i < len(scores) else 0
                if abs(score_val) < long_thr:
                    sig.iloc[i] = 0
        return sig
    return modifier


def hour_filter(bad_hours):
    """Suppress entries during bad hours (UTC)."""
    def modifier(signals, alt_merged, coin, btc_score_ts):
        sig = signals.copy()
        hours = alt_merged["ts"].dt.hour.values
        for i in range(len(sig)):
            if sig.iloc[i] != 0 and hours[i] in bad_hours:
                sig.iloc[i] = 0
        return sig
    return modifier


def short_only():
    """Only allow SHORT signals."""
    def modifier(signals, alt_merged, coin, btc_score_ts):
        sig = signals.copy()
        sig[sig > 0] = 0
        return sig
    return modifier


def combine_modifiers(*modifiers):
    """Chain multiple signal modifiers."""
    def modifier(signals, alt_merged, coin, btc_score_ts):
        sig = signals
        for m in modifiers:
            sig = m(sig, alt_merged, coin, btc_score_ts)
        return sig
    return modifier


# ── Custom BTC Score Functions ──────────────────────────
def make_boosted_score_fn(extra_overrides):
    """Create a BTC score function with modified V3_EXTRA_WEIGHTS."""
    def fn(btc_df_local):
        import backtest_15m_btc_led_alts as bt
        old = dict(bt.V3_EXTRA_WEIGHTS)
        bt.V3_EXTRA_WEIGHTS.update(extra_overrides)
        score = compute_btc_composite_score(btc_df_local)
        bt.V3_EXTRA_WEIGHTS.update(old)
        return score
    return fn


def make_dropped_factor_score_fn(drop_factor):
    """Create a BTC score function with one factor dropped."""
    def fn(btc_df_local):
        import backtest_15m_btc_led_alts as bt
        old = dict(bt.V3_EXTRA_WEIGHTS)
        if drop_factor in bt.V3_EXTRA_WEIGHTS:
            bt.V3_EXTRA_WEIGHTS[drop_factor] = 0.0

        # For core factors, need to set weights to 0
        drop_map = {
            "funding_rate": {"w_fr_neg": 0.0, "w_fr_pos": 0.0},
            "whale_alerts": {"w_whale_bull": 0.0, "w_whale_bear": 0.0},
            "etf_flows": {"w_etf_bull": 0.0, "w_etf_bear": 0.0},
            "oi_divergence": {"w_oi_bull": 0.0, "w_oi_capit": 0.0, "w_oi_weak": 0.0, "w_oi_bear": 0.0},
            "liquidation": {"w_liq_bull": 0.0, "w_liq_bear": 0.0},
        }
        params = dict(COMPOSITE_WEIGHTS)
        if drop_factor in drop_map:
            params.update(drop_map[drop_factor])

        score = compute_btc_composite_score(btc_df_local, params=params)
        bt.V3_EXTRA_WEIGHTS.update(old)
        return score
    return fn


def make_custom_weight_score_fn(core_overrides=None, extra_overrides=None):
    """Create a BTC score function with fully custom weights."""
    def fn(btc_df_local):
        import backtest_15m_btc_led_alts as bt
        old_extra = dict(bt.V3_EXTRA_WEIGHTS)

        params = dict(COMPOSITE_WEIGHTS)
        if core_overrides:
            params.update(core_overrides)
        if extra_overrides:
            bt.V3_EXTRA_WEIGHTS.update(extra_overrides)

        score = compute_btc_composite_score(btc_df_local, params=params)
        bt.V3_EXTRA_WEIGHTS.update(old_extra)
        return score
    return fn


# ══════════════════════════════════════════════════════════
# BATCH 1: Single-variable experiments
# ══════════════════════════════════════════════════════════
all_results = []

# 0. BASELINE: v3 with original SL 2.5, TP 4.0
print("\n" + "#" * 70)
print("# BATCH 1: Single-Variable Experiments")
print("#" * 70)

r = run_contender(
    "v3_baseline",
    "v3 original (SL=2.5, TP=4.0, per-coin thresholds)",
)
all_results.append(r)
BASELINE_PNL = r["total_pnl"]

# 1. v3 with deployed config (SL=10, TP=5)
r = run_contender(
    "v3_deployed",
    "v3 deployed config (SL=10, TP=5)",
    coin_overrides={"__all__": {"sl": 10.0, "tp": 5.0}},
)
all_results.append(r)

# 2. Asymmetric threshold: LONG=5.0, SHORT=original
r = run_contender(
    "v3_asym_L5",
    "Asymmetric threshold: LONG needs score>=5.0 (SHORT unchanged)",
    signal_modifier=asymmetric_threshold(5.0, 3.0),
)
all_results.append(r)

# 3. Asymmetric threshold: LONG=6.0
r = run_contender(
    "v3_asym_L6",
    "Asymmetric threshold: LONG needs score>=6.0 (SHORT unchanged)",
    signal_modifier=asymmetric_threshold(6.0, 3.0),
)
all_results.append(r)

# 4. SHORT only
r = run_contender(
    "v3_short_only",
    "SHORT trades only (no LONGs)",
    signal_modifier=short_only(),
)
all_results.append(r)

# 5. Hour filter (skip worst hours)
r = run_contender(
    "v3_hour_filter",
    "Skip entries at hours 12,13,14,19,20 UTC",
    signal_modifier=hour_filter({12, 13, 14, 19, 20}),
)
all_results.append(r)

# 6. SL=99 (effectively no SL)
r = run_contender(
    "v3_no_sl",
    "No stop loss (SL=99 ATR)",
    coin_overrides={"__all__": {"sl": 99.0}},
)
all_results.append(r)

# 7. Wider TP (8.0 ATR)
r = run_contender(
    "v3_tp8",
    "Wider take profit (TP=8.0 ATR)",
    coin_overrides={"__all__": {"tp": 8.0}},
)
all_results.append(r)

# 8. SL=10, TP=8 (wide both)
r = run_contender(
    "v3_sl10_tp8",
    "Wide SL=10 + wide TP=8",
    coin_overrides={"__all__": {"sl": 10.0, "tp": 8.0}},
)
all_results.append(r)

# 9. Drop funding_rate (weakest factor)
r = run_contender(
    "v3_no_funding",
    "Drop funding_rate factor (weakest per Mission #007)",
    btc_score_fn=make_dropped_factor_score_fn("funding_rate"),
)
all_results.append(r)

# 10. Boost liquidation (3.0) + ob_combined (3.0)
r = run_contender(
    "v3_boost_tier1",
    "Boost Tier 1: liquidation=3.0, ob_combined=3.0",
    btc_score_fn=make_custom_weight_score_fn(
        core_overrides={"w_liq_bull": 3.0, "w_liq_bear": 3.0},
        extra_overrides={"ob_combined": 3.0},
    ),
)
all_results.append(r)

# 11. Drop weak factors: funding + whale + etf
r = run_contender(
    "v3_tier1_only",
    "Tier 1 only: liquidation + ob_combined + tick_liq (drop funding, whale, etf, oi, basis)",
    btc_score_fn=make_custom_weight_score_fn(
        core_overrides={
            "w_fr_neg": 0.0, "w_fr_pos": 0.0,
            "w_whale_bull": 0.0, "w_whale_bear": 0.0,
            "w_etf_bull": 0.0, "w_etf_bear": 0.0,
            "w_oi_bull": 0.0, "w_oi_capit": 0.0, "w_oi_weak": 0.0, "w_oi_bear": 0.0,
        },
        extra_overrides={"basis_contrarian": 0.0},
    ),
)
all_results.append(r)

# 12. Cooldown=8 for all coins
r = run_contender(
    "v3_cd8",
    "Longer cooldown (8 bars for all coins)",
    coin_overrides={"__all__": {"cd": 8}},
)
all_results.append(r)

# 13. Lower thresholds (more trades)
r = run_contender(
    "v3_low_threshold",
    "Lower thresholds (all coins -0.5)",
    coin_overrides={
        "BTC": {"threshold": 2.0}, "XRP": {"threshold": 3.0},
        "ADA": {"threshold": 3.0}, "DOT": {"threshold": 2.5},
        "SUI": {"threshold": 2.5}, "FIL": {"threshold": 2.5},
    },
)
all_results.append(r)

# 14. Spike mode: momentum
r = run_contender(
    "v3_momentum_spike",
    "Vol spike overlay: momentum mode",
    coin_overrides={"__all__": {"spike_mode": "momentum"}},
)
all_results.append(r)


# ══════════════════════════════════════════════════════════
# BATCH 1 Summary
# ══════════════════════════════════════════════════════════
print("\n" + "=" * 80)
print("BATCH 1 RESULTS SUMMARY")
print("=" * 80)
print(f"{'Name':<25} {'Trades':>6} {'WR%':>6} {'PnL':>10} {'Sharpe':>7} {'DD%':>6} {'Delta':>10}")
print("-" * 80)
for r in sorted(all_results, key=lambda x: x["total_pnl"], reverse=True):
    delta = r["total_pnl"] - BASELINE_PNL
    marker = " ***" if delta > 0 else ""
    print(f"{r['name']:<25} {r['total_trades']:>6} {r['win_rate']:>5.1f}% "
          f"${r['total_pnl']:>9,.0f} {r['sharpe']:>7.2f} {r['max_dd_pct']:>5.1f}% "
          f"${delta:>+9,.0f}{marker}")

# Find batch 1 winners
winners_b1 = [r for r in all_results if r["total_pnl"] > BASELINE_PNL and r["name"] != "v3_baseline"]
winners_b1.sort(key=lambda x: x["total_pnl"], reverse=True)

print(f"\nBATCH 1 WINNERS ({len(winners_b1)} beat baseline):")
for w in winners_b1[:5]:
    print(f"  {w['name']}: ${w['total_pnl']:+,.0f} (delta ${w['total_pnl']-BASELINE_PNL:+,.0f})")


# ══════════════════════════════════════════════════════════
# BATCH 2: Combinations of winners
# ══════════════════════════════════════════════════════════
print("\n" + "#" * 70)
print("# BATCH 2: Combination Experiments")
print("#" * 70)

batch2_results = []

# Combo 1: Asym L5 + SL10 + TP5
r = run_contender(
    "combo_asym5_sl10_tp5",
    "Asymmetric L5 + SL=10 + TP=5",
    signal_modifier=asymmetric_threshold(5.0, 3.0),
    coin_overrides={"__all__": {"sl": 10.0, "tp": 5.0}},
)
batch2_results.append(r)

# Combo 2: Asym L6 + SL10 + TP5
r = run_contender(
    "combo_asym6_sl10_tp5",
    "Asymmetric L6 + SL=10 + TP=5",
    signal_modifier=asymmetric_threshold(6.0, 3.0),
    coin_overrides={"__all__": {"sl": 10.0, "tp": 5.0}},
)
batch2_results.append(r)

# Combo 3: Asym L5 + SL10 + TP8
r = run_contender(
    "combo_asym5_sl10_tp8",
    "Asymmetric L5 + SL=10 + TP=8",
    signal_modifier=asymmetric_threshold(5.0, 3.0),
    coin_overrides={"__all__": {"sl": 10.0, "tp": 8.0}},
)
batch2_results.append(r)

# Combo 4: Asym L5 + no SL
r = run_contender(
    "combo_asym5_nosl",
    "Asymmetric L5 + No SL (99 ATR)",
    signal_modifier=asymmetric_threshold(5.0, 3.0),
    coin_overrides={"__all__": {"sl": 99.0}},
)
batch2_results.append(r)

# Combo 5: Asym L5 + hour filter + SL10
r = run_contender(
    "combo_asym5_hour_sl10",
    "Asymmetric L5 + Hour filter + SL=10",
    signal_modifier=combine_modifiers(
        asymmetric_threshold(5.0, 3.0),
        hour_filter({12, 13, 14, 19, 20}),
    ),
    coin_overrides={"__all__": {"sl": 10.0, "tp": 5.0}},
)
batch2_results.append(r)

# Combo 6: Short only + SL10 + TP8
r = run_contender(
    "combo_short_sl10_tp8",
    "SHORT only + SL=10 + TP=8",
    signal_modifier=short_only(),
    coin_overrides={"__all__": {"sl": 10.0, "tp": 8.0}},
)
batch2_results.append(r)

# Combo 7: Boost tier1 + asym L5 + SL10
r = run_contender(
    "combo_boost_asym5_sl10",
    "Boost Tier1 + Asym L5 + SL=10",
    btc_score_fn=make_custom_weight_score_fn(
        core_overrides={"w_liq_bull": 3.0, "w_liq_bear": 3.0},
        extra_overrides={"ob_combined": 3.0},
    ),
    signal_modifier=asymmetric_threshold(5.0, 3.0),
    coin_overrides={"__all__": {"sl": 10.0, "tp": 5.0}},
)
batch2_results.append(r)

# Combo 8: No funding + asym L5 + SL10 + TP8
r = run_contender(
    "combo_nofund_asym5_sl10_tp8",
    "Drop funding + Asym L5 + SL=10 + TP=8",
    btc_score_fn=make_dropped_factor_score_fn("funding_rate"),
    signal_modifier=asymmetric_threshold(5.0, 3.0),
    coin_overrides={"__all__": {"sl": 10.0, "tp": 8.0}},
)
batch2_results.append(r)

# Combo 9: Asym L5 + spike momentum + SL10 + TP5
r = run_contender(
    "combo_asym5_spike_sl10",
    "Asym L5 + Spike momentum + SL=10 + TP=5",
    signal_modifier=asymmetric_threshold(5.0, 3.0),
    coin_overrides={"__all__": {"sl": 10.0, "tp": 5.0, "spike_mode": "momentum"}},
)
batch2_results.append(r)

# Combo 10: Low threshold + short only + SL10
r = run_contender(
    "combo_low_short_sl10",
    "Low threshold (-0.5) + SHORT only + SL=10",
    signal_modifier=short_only(),
    coin_overrides={
        "BTC": {"threshold": 2.0, "sl": 10.0}, "XRP": {"threshold": 3.0, "sl": 10.0},
        "ADA": {"threshold": 3.0, "sl": 10.0}, "DOT": {"threshold": 2.5, "sl": 10.0},
        "SUI": {"threshold": 2.5, "sl": 10.0}, "FIL": {"threshold": 2.5, "sl": 10.0},
    },
)
batch2_results.append(r)

all_results.extend(batch2_results)


# ══════════════════════════════════════════════════════════
# BATCH 2 Summary
# ══════════════════════════════════════════════════════════
print("\n" + "=" * 80)
print("BATCH 2 RESULTS SUMMARY")
print("=" * 80)
print(f"{'Name':<30} {'Trades':>6} {'WR%':>6} {'PnL':>10} {'Sharpe':>7} {'DD%':>6} {'Delta':>10}")
print("-" * 80)
for r in sorted(batch2_results, key=lambda x: x["total_pnl"], reverse=True):
    delta = r["total_pnl"] - BASELINE_PNL
    print(f"{r['name']:<30} {r['total_trades']:>6} {r['win_rate']:>5.1f}% "
          f"${r['total_pnl']:>9,.0f} {r['sharpe']:>7.2f} {r['max_dd_pct']:>5.1f}% "
          f"${delta:>+9,.0f}")


# ══════════════════════════════════════════════════════════
# BATCH 3: Evolve from best combo
# ══════════════════════════════════════════════════════════
print("\n" + "#" * 70)
print("# BATCH 3: Evolution from Best Combo")
print("#" * 70)

# Find best from batch 1+2
best_so_far = max(all_results, key=lambda x: x["total_pnl"])
print(f"Current leader: {best_so_far['name']} at ${best_so_far['total_pnl']:+,.0f}")

batch3_results = []

# Evo 1: Best combo + cooldown 8
r = run_contender(
    "evo_best_cd8",
    f"Best combo + cooldown 8",
    signal_modifier=asymmetric_threshold(5.0, 3.0),
    coin_overrides={"__all__": {"sl": 10.0, "tp": 8.0, "cd": 8}},
)
batch3_results.append(r)

# Evo 2: Best combo + max_hold 48 (faster exit)
r = run_contender(
    "evo_best_hold48",
    f"Best combo + max_hold=48 bars",
    signal_modifier=asymmetric_threshold(5.0, 3.0),
    coin_overrides={"__all__": {"sl": 10.0, "tp": 8.0, "max_hold": 48}},
)
batch3_results.append(r)

# Evo 3: Best combo + trail_atr 1.0 (activate trailing stop)
r = run_contender(
    "evo_best_trail1",
    f"Best combo + trailing stop (trail=1.0, activate=1.0)",
    signal_modifier=asymmetric_threshold(5.0, 3.0),
    coin_overrides={"__all__": {"sl": 10.0, "tp": 8.0, "trail": 1.0, "trail_act": 1.0}},
)
batch3_results.append(r)

# Evo 4: Best combo + trail_atr 0.5 (tighter trailing)
r = run_contender(
    "evo_best_trail05",
    f"Best combo + trailing stop (trail=0.5, activate=0.5)",
    signal_modifier=asymmetric_threshold(5.0, 3.0),
    coin_overrides={"__all__": {"sl": 10.0, "tp": 8.0, "trail": 0.5, "trail_act": 0.5}},
)
batch3_results.append(r)

# Evo 5: Asym L4.5 (slightly lower than L5)
r = run_contender(
    "evo_asym_L45_sl10_tp8",
    "Asymmetric L4.5 + SL=10 + TP=8",
    signal_modifier=asymmetric_threshold(4.5, 3.0),
    coin_overrides={"__all__": {"sl": 10.0, "tp": 8.0}},
)
batch3_results.append(r)

# Evo 6: Asym L5 + SL=10 + TP=6 (between 5 and 8)
r = run_contender(
    "evo_asym5_sl10_tp6",
    "Asymmetric L5 + SL=10 + TP=6",
    signal_modifier=asymmetric_threshold(5.0, 3.0),
    coin_overrides={"__all__": {"sl": 10.0, "tp": 6.0}},
)
batch3_results.append(r)

# Evo 7: Asym L5 + SL=10 + TP=10 (very wide TP)
r = run_contender(
    "evo_asym5_sl10_tp10",
    "Asymmetric L5 + SL=10 + TP=10",
    signal_modifier=asymmetric_threshold(5.0, 3.0),
    coin_overrides={"__all__": {"sl": 10.0, "tp": 10.0}},
)
batch3_results.append(r)

# Evo 8: Boost Tier1 + Asym L5 + SL=10 + TP=8
r = run_contender(
    "evo_boost_asym5_sl10_tp8",
    "Boost Tier1 + Asym L5 + SL=10 + TP=8",
    btc_score_fn=make_custom_weight_score_fn(
        core_overrides={"w_liq_bull": 3.0, "w_liq_bear": 3.0},
        extra_overrides={"ob_combined": 3.0},
    ),
    signal_modifier=asymmetric_threshold(5.0, 3.0),
    coin_overrides={"__all__": {"sl": 10.0, "tp": 8.0}},
)
batch3_results.append(r)

# Evo 9: Drop funding + Boost Tier1 + Asym L5 + SL10 + TP8
r = run_contender(
    "evo_nofund_boost_asym5",
    "No funding + Boost Tier1 + Asym L5 + SL=10 + TP=8",
    btc_score_fn=make_custom_weight_score_fn(
        core_overrides={
            "w_liq_bull": 3.0, "w_liq_bear": 3.0,
            "w_fr_neg": 0.0, "w_fr_pos": 0.0,
        },
        extra_overrides={"ob_combined": 3.0},
    ),
    signal_modifier=asymmetric_threshold(5.0, 3.0),
    coin_overrides={"__all__": {"sl": 10.0, "tp": 8.0}},
)
batch3_results.append(r)

# Evo 10: Best combo + hour filter
r = run_contender(
    "evo_best_hour_filter",
    "Asym L5 + SL10 + TP8 + Hour filter",
    signal_modifier=combine_modifiers(
        asymmetric_threshold(5.0, 3.0),
        hour_filter({12, 13, 14, 19, 20}),
    ),
    coin_overrides={"__all__": {"sl": 10.0, "tp": 8.0}},
)
batch3_results.append(r)

all_results.extend(batch3_results)


# ══════════════════════════════════════════════════════════
# GRAND SUMMARY
# ══════════════════════════════════════════════════════════
print("\n" + "=" * 90)
print("GRAND TOURNAMENT RESULTS -- ALL CONTENDERS")
print("=" * 90)
print(f"{'Rank':<5} {'Name':<30} {'Trades':>6} {'WR%':>6} {'PnL':>10} {'Sharpe':>7} {'DD%':>6} {'Delta':>10}")
print("-" * 90)

sorted_all = sorted(all_results, key=lambda x: x["total_pnl"], reverse=True)
for i, r in enumerate(sorted_all, 1):
    delta = r["total_pnl"] - BASELINE_PNL
    marker = " <-- KING" if i == 1 else (" <-- BASELINE" if r["name"] == "v3_baseline" else "")
    print(f"{i:<5} {r['name']:<30} {r['total_trades']:>6} {r['win_rate']:>5.1f}% "
          f"${r['total_pnl']:>9,.0f} {r['sharpe']:>7.2f} {r['max_dd_pct']:>5.1f}% "
          f"${delta:>+9,.0f}{marker}")

# Save results
results_data = {
    "tournament": "round_1",
    "timestamp": datetime.utcnow().isoformat(),
    "oos_period": f"{OOS_START} to {OOS_END}",
    "coins": COINS,
    "baseline_pnl": BASELINE_PNL,
    "total_contenders": len(all_results),
    "results": sorted_all,
    "king": sorted_all[0],
}
RESULTS_FILE.write_text(json.dumps(results_data, indent=2, default=str, ensure_ascii=False))
print(f"\nResults saved to {RESULTS_FILE}")

# ══════════════════════════════════════════════════════════
# THE KING
# ══════════════════════════════════════════════════════════
king = sorted_all[0]
print("\n" + "=" * 70)
print(f"THE NEW KING: {king['name']}")
print(f"  PnL: ${king['total_pnl']:+,.0f} (delta ${king['total_pnl']-BASELINE_PNL:+,.0f} vs v3)")
print(f"  Trades: {king['total_trades']} | WR: {king['win_rate']:.1f}%")
print(f"  Sharpe: {king['sharpe']:.2f} | MaxDD: {king['max_dd_pct']:.1f}%")
print(f"  LONG: {king['long_trades']} trades, WR {king['long_wr']:.1f}%, ${king['long_pnl']:+,.0f}")
print(f"  SHORT: {king['short_trades']} trades, WR {king['short_wr']:.1f}%, ${king['short_pnl']:+,.0f}")
print(f"  Description: {king['description']}")
print("=" * 70)

total_time = time.time() - t0
print(f"\nTotal tournament time: {total_time/60:.1f} minutes")
print(f"Contenders tested: {len(all_results)}")
print(f"Contenders beating baseline: {len([r for r in all_results if r['total_pnl'] > BASELINE_PNL and r['name'] != 'v3_baseline'])}")
