"""
Tournament Round 1, Batch 4: Evolve from the King
King = evo_boost_asym5_sl10_tp8: Boost Tier1 (liq=3, ob=3) + Asym L5 + SL=10 + TP=8
$34,449 | 1451 trades | 69.9% WR | Sharpe 13.97
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
    V3_EXTRA_WEIGHTS, COMPOSITE_WEIGHTS,
)

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

# ── Load data ──────────────────────────────────────────
print("Loading data...")
t0 = time.time()
btc_ohlcv = fetch_binance_15m("BTCUSDT", years=3)
db_data = load_btc_db_data()
btc_df = build_btc_features(btc_ohlcv, db_data)

alt_data = {}
for coin in COINS:
    alt_data[coin] = build_alt_technicals(fetch_binance_15m(f"{coin}USDT", years=3))
print(f"Data loaded in {time.time()-t0:.1f}s\n")


def run_experiment(name, desc, core_overrides=None, extra_overrides=None,
                   signal_mod=None, coin_overrides=None):
    """Run single experiment and return result dict."""
    import backtest_15m_btc_led_alts as bt
    old_extra = dict(bt.V3_EXTRA_WEIGHTS)

    params = dict(COMPOSITE_WEIGHTS)
    if core_overrides:
        params.update(core_overrides)
    if extra_overrides:
        bt.V3_EXTRA_WEIGHTS.update(extra_overrides)

    btc_score = compute_btc_composite_score(btc_df, params=params)
    btc_score_ts = pd.Series(btc_score.values, index=btc_df["ts"].values)
    bt.V3_EXTRA_WEIGHTS.update(old_extra)

    all_trades = []
    for coin in COINS:
        cfg = dict(V11_CONFIGS.get(coin, V11_CONFIGS["DOT"]))
        if coin_overrides:
            ov = coin_overrides.get(coin, coin_overrides.get("__all__", {}))
            cfg.update(ov)

        signals, alt_merged = generate_btc_led_signal(
            btc_score_ts, alt_data[coin],
            threshold=cfg["threshold"],
            use_alt_pa_filter=cfg.get("alt_pa", False),
            spike_mode=cfg.get("spike_mode"),
        )

        if signal_mod:
            signals = signal_mod(signals, alt_merged, coin, btc_score_ts)

        oos_mask = (alt_merged["ts"] >= pd.Timestamp(OOS_START)) & (alt_merged["ts"] <= pd.Timestamp(OOS_END))
        alt_oos = alt_merged[oos_mask].reset_index(drop=True)
        sig_oos = signals[oos_mask].reset_index(drop=True)
        if len(alt_oos) < 100:
            continue

        trades = run_backtest(alt_oos, sig_oos,
                              sl_atr_mult=cfg.get("sl", 10.0),
                              tp_atr_mult=cfg.get("tp", 8.0),
                              trail_atr_mult=cfg.get("trail", 99),
                              trail_activate_atr=cfg.get("trail_act", 99),
                              max_hold_bars=cfg.get("max_hold", 96),
                              cooldown_bars=cfg.get("cd", 4))
        if len(trades) > 0:
            trades["coin"] = coin
            all_trades.append(trades)

    if not all_trades:
        return None

    combined = pd.concat(all_trades, ignore_index=True)
    pnl = combined["pnl_net"].sum()
    n = len(combined)
    wr = 100 * (combined["pnl_net"] > 0).sum() / n
    longs = combined[combined["dir"] == "L"]
    shorts = combined[combined["dir"] == "S"]
    long_wr = 100 * (longs["pnl_net"] > 0).sum() / len(longs) if len(longs) > 0 else 0
    short_wr = 100 * (shorts["pnl_net"] > 0).sum() / len(shorts) if len(shorts) > 0 else 0
    equity = 10000 + combined["pnl_net"].cumsum()
    max_dd = ((equity - equity.cummax()) / equity.cummax() * 100).min()
    ret = combined["pnl_net"] / 1000
    sharpe = ret.mean() / ret.std() * np.sqrt(n) if ret.std() > 0 else 0

    r = {"name": name, "desc": desc, "pnl": round(pnl, 2), "trades": n,
         "wr": round(wr, 1), "sharpe": round(sharpe, 2), "dd": round(max_dd, 1),
         "long_n": len(longs), "long_wr": round(long_wr, 1), "long_pnl": round(longs["pnl_net"].sum(), 2),
         "short_n": len(shorts), "short_wr": round(short_wr, 1), "short_pnl": round(shorts["pnl_net"].sum(), 2)}

    print(f"  {name:<35} {n:>5} trades | WR {wr:>5.1f}% | ${pnl:>+9,.0f} | S {sharpe:>5.2f} | DD {max_dd:>5.1f}%")
    return r


def asym_threshold(long_thr):
    def mod(signals, alt_merged, coin, btc_score_ts):
        sig = signals.copy()
        if "btc_score" in alt_merged.columns:
            scores = alt_merged["btc_score"].values
        else:
            scores = btc_score_ts.reindex(alt_merged["ts"]).ffill().values
        for i in range(len(sig)):
            if sig.iloc[i] == 1 and (i >= len(scores) or abs(scores[i]) < long_thr):
                sig.iloc[i] = 0
        return sig
    return mod


# King config: core_overrides={w_liq_bull:3, w_liq_bear:3}, extra_overrides={ob_combined:3}
# + asym L5 + sl=10 + tp=8
KING_CORE = {"w_liq_bull": 3.0, "w_liq_bear": 3.0}
KING_EXTRA = {"ob_combined": 3.0}
KING_COINS = {"__all__": {"sl": 10.0, "tp": 8.0}}
KING_SIG = asym_threshold(5.0)

print("=" * 80)
print("BATCH 4: Evolving from the King ($34,449)")
print("=" * 80)

results = []

# Re-verify King
r = run_experiment("KING_baseline",
    "Boost Tier1 + Asym L5 + SL=10 + TP=8",
    core_overrides=KING_CORE, extra_overrides=KING_EXTRA,
    signal_mod=KING_SIG, coin_overrides=KING_COINS)
results.append(r)
KING_PNL = r["pnl"]

# ── Weight variations on King ──
print("\n--- Weight variations ---")

# 1. Boost liq=4.0
r = run_experiment("king_liq4",
    "liq=4.0 (up from 3.0)",
    core_overrides={"w_liq_bull": 4.0, "w_liq_bear": 4.0},
    extra_overrides=KING_EXTRA, signal_mod=KING_SIG, coin_overrides=KING_COINS)
results.append(r)

# 2. Boost ob=4.0
r = run_experiment("king_ob4",
    "ob=4.0 (up from 3.0)",
    core_overrides=KING_CORE,
    extra_overrides={"ob_combined": 4.0},
    signal_mod=KING_SIG, coin_overrides=KING_COINS)
results.append(r)

# 3. Both 4.0
r = run_experiment("king_liq4_ob4",
    "liq=4, ob=4",
    core_overrides={"w_liq_bull": 4.0, "w_liq_bear": 4.0},
    extra_overrides={"ob_combined": 4.0},
    signal_mod=KING_SIG, coin_overrides=KING_COINS)
results.append(r)

# 4. Boost tick_liq to 3.0
r = run_experiment("king_tick3",
    "King + tick_liq=3.0",
    core_overrides=KING_CORE,
    extra_overrides={"ob_combined": 3.0, "tick_liq": 3.0},
    signal_mod=KING_SIG, coin_overrides=KING_COINS)
results.append(r)

# 5. Reduce whale to 0.5
r = run_experiment("king_whale05",
    "King + whale=0.5",
    core_overrides={**KING_CORE, "w_whale_bull": 0.5, "w_whale_bear": 0.5},
    extra_overrides=KING_EXTRA, signal_mod=KING_SIG, coin_overrides=KING_COINS)
results.append(r)

# 6. Reduce oi to 0.0
r = run_experiment("king_no_oi",
    "King + oi=0",
    core_overrides={**KING_CORE, "w_oi_bull": 0, "w_oi_capit": 0, "w_oi_weak": 0, "w_oi_bear": 0},
    extra_overrides=KING_EXTRA, signal_mod=KING_SIG, coin_overrides=KING_COINS)
results.append(r)

# 7. Drop etf
r = run_experiment("king_no_etf",
    "King + etf=0",
    core_overrides={**KING_CORE, "w_etf_bull": 0, "w_etf_bear": 0},
    extra_overrides=KING_EXTRA, signal_mod=KING_SIG, coin_overrides=KING_COINS)
results.append(r)

# 8. No funding
r = run_experiment("king_no_funding",
    "King + funding=0",
    core_overrides={**KING_CORE, "w_fr_neg": 0, "w_fr_pos": 0},
    extra_overrides=KING_EXTRA, signal_mod=KING_SIG, coin_overrides=KING_COINS)
results.append(r)

# 9. Basis to 2.0
r = run_experiment("king_basis2",
    "King + basis=2.0",
    core_overrides=KING_CORE,
    extra_overrides={"ob_combined": 3.0, "basis_contrarian": 2.0},
    signal_mod=KING_SIG, coin_overrides=KING_COINS)
results.append(r)

# 10. Mega boost: liq=4, ob=4, tick=3, basis=2
r = run_experiment("king_mega_boost",
    "liq=4, ob=4, tick=3, basis=2",
    core_overrides={"w_liq_bull": 4.0, "w_liq_bear": 4.0},
    extra_overrides={"ob_combined": 4.0, "tick_liq": 3.0, "basis_contrarian": 2.0},
    signal_mod=KING_SIG, coin_overrides=KING_COINS)
results.append(r)

# ── SL/TP variations ──
print("\n--- SL/TP variations ---")

# 11. SL=15, TP=8
r = run_experiment("king_sl15",
    "King + SL=15",
    core_overrides=KING_CORE, extra_overrides=KING_EXTRA,
    signal_mod=KING_SIG, coin_overrides={"__all__": {"sl": 15.0, "tp": 8.0}})
results.append(r)

# 12. SL=99 (no SL), TP=8
r = run_experiment("king_nosl",
    "King + no SL",
    core_overrides=KING_CORE, extra_overrides=KING_EXTRA,
    signal_mod=KING_SIG, coin_overrides={"__all__": {"sl": 99.0, "tp": 8.0}})
results.append(r)

# 13. TP=10
r = run_experiment("king_tp10",
    "King + TP=10",
    core_overrides=KING_CORE, extra_overrides=KING_EXTRA,
    signal_mod=KING_SIG, coin_overrides={"__all__": {"sl": 10.0, "tp": 10.0}})
results.append(r)

# 14. TP=12
r = run_experiment("king_tp12",
    "King + TP=12",
    core_overrides=KING_CORE, extra_overrides=KING_EXTRA,
    signal_mod=KING_SIG, coin_overrides={"__all__": {"sl": 10.0, "tp": 12.0}})
results.append(r)

# 15. SL=15, TP=10
r = run_experiment("king_sl15_tp10",
    "King + SL=15 + TP=10",
    core_overrides=KING_CORE, extra_overrides=KING_EXTRA,
    signal_mod=KING_SIG, coin_overrides={"__all__": {"sl": 15.0, "tp": 10.0}})
results.append(r)

# 16. SL=99, TP=10
r = run_experiment("king_nosl_tp10",
    "King + no SL + TP=10",
    core_overrides=KING_CORE, extra_overrides=KING_EXTRA,
    signal_mod=KING_SIG, coin_overrides={"__all__": {"sl": 99.0, "tp": 10.0}})
results.append(r)

# ── Threshold variations ──
print("\n--- Threshold variations ---")

# 17. Asym L4 (let more LONGs in)
r = run_experiment("king_asymL4",
    "King + Asym L4",
    core_overrides=KING_CORE, extra_overrides=KING_EXTRA,
    signal_mod=asym_threshold(4.0), coin_overrides=KING_COINS)
results.append(r)

# 18. Asym L4 + lower thresholds
r = run_experiment("king_asymL4_lowT",
    "Asym L4 + threshold -0.5 all",
    core_overrides=KING_CORE, extra_overrides=KING_EXTRA,
    signal_mod=asym_threshold(4.0),
    coin_overrides={"BTC": {"sl": 10, "tp": 8, "threshold": 2.0},
                    "XRP": {"sl": 10, "tp": 8, "threshold": 3.0},
                    "ADA": {"sl": 10, "tp": 8, "threshold": 3.0},
                    "DOT": {"sl": 10, "tp": 8, "threshold": 2.5},
                    "SUI": {"sl": 10, "tp": 8, "threshold": 2.5},
                    "FIL": {"sl": 10, "tp": 8, "threshold": 2.5}})
results.append(r)

# 19. No asym (let all LONGs in)
r = run_experiment("king_no_asym",
    "King without asymmetric threshold",
    core_overrides=KING_CORE, extra_overrides=KING_EXTRA,
    coin_overrides=KING_COINS)
results.append(r)

# 20. Spike momentum
r = run_experiment("king_spike",
    "King + spike momentum",
    core_overrides=KING_CORE, extra_overrides=KING_EXTRA,
    signal_mod=KING_SIG,
    coin_overrides={"__all__": {"sl": 10.0, "tp": 8.0, "spike_mode": "momentum"}})
results.append(r)

# ── Ultimate combos ──
print("\n--- Ultimate combos ---")

# 21. Mega boost + no SL + TP=10
r = run_experiment("ultra_mega_nosl_tp10",
    "Mega weights + no SL + TP=10",
    core_overrides={"w_liq_bull": 4.0, "w_liq_bear": 4.0},
    extra_overrides={"ob_combined": 4.0, "tick_liq": 3.0, "basis_contrarian": 2.0},
    signal_mod=KING_SIG,
    coin_overrides={"__all__": {"sl": 99.0, "tp": 10.0}})
results.append(r)

# 22. Best weight combo + SL15 + TP10
r = run_experiment("ultra_boost_sl15_tp10",
    "Mega weights + SL=15 + TP=10",
    core_overrides={"w_liq_bull": 4.0, "w_liq_bear": 4.0},
    extra_overrides={"ob_combined": 4.0, "tick_liq": 3.0, "basis_contrarian": 2.0},
    signal_mod=KING_SIG,
    coin_overrides={"__all__": {"sl": 15.0, "tp": 10.0}})
results.append(r)

# 23. King no asym + SL=15 + TP=10 (simpler, let all LONGs)
r = run_experiment("ultra_simple_sl15_tp10",
    "Boost Tier1 + SL=15 + TP=10 (no asym)",
    core_overrides=KING_CORE, extra_overrides=KING_EXTRA,
    coin_overrides={"__all__": {"sl": 15.0, "tp": 10.0}})
results.append(r)

# 24. Boost + lower threshold + SL10 + TP8
r = run_experiment("ultra_boost_lowT",
    "Boost Tier1 + low threshold + SL=10 + TP=8",
    core_overrides=KING_CORE, extra_overrides=KING_EXTRA,
    coin_overrides={"BTC": {"sl": 10, "tp": 8, "threshold": 2.0},
                    "XRP": {"sl": 10, "tp": 8, "threshold": 3.0},
                    "ADA": {"sl": 10, "tp": 8, "threshold": 3.0},
                    "DOT": {"sl": 10, "tp": 8, "threshold": 2.5},
                    "SUI": {"sl": 10, "tp": 8, "threshold": 2.5},
                    "FIL": {"sl": 10, "tp": 8, "threshold": 2.5}})
results.append(r)

# 25. Liq=3, ob=3, no weak factors, SL=10, TP=8
r = run_experiment("ultra_clean",
    "Tier1 boost + drop funding+whale+etf + SL10 + TP8",
    core_overrides={**KING_CORE, "w_fr_neg": 0, "w_fr_pos": 0,
                    "w_whale_bull": 0, "w_whale_bear": 0,
                    "w_etf_bull": 0, "w_etf_bear": 0},
    extra_overrides=KING_EXTRA, signal_mod=KING_SIG, coin_overrides=KING_COINS)
results.append(r)


# ══════════════════════════════════════════════════════════
# Summary
# ══════════════════════════════════════════════════════════
results = [r for r in results if r is not None]
results.sort(key=lambda x: x["pnl"], reverse=True)

print("\n" + "=" * 100)
print("BATCH 4 FINAL RANKING")
print("=" * 100)
print(f"{'Rank':<5} {'Name':<35} {'Trades':>6} {'WR%':>6} {'PnL':>10} {'Sharpe':>7} {'DD%':>6} {'vs King':>10}")
print("-" * 100)
for i, r in enumerate(results, 1):
    delta = r["pnl"] - KING_PNL
    marker = " <-- NEW KING!" if i == 1 and delta > 0 else (" <-- KING" if r["name"] == "KING_baseline" else "")
    print(f"{i:<5} {r['name']:<35} {r['trades']:>6} {r['wr']:>5.1f}% "
          f"${r['pnl']:>9,.0f} {r['sharpe']:>7.2f} {r['dd']:>5.1f}% "
          f"${delta:>+9,.0f}{marker}")

# Save
outfile = RESULTS_DIR / "batch4_results.json"
outfile.write_text(json.dumps({"batch": 4, "king_pnl": KING_PNL, "results": results},
                               indent=2, default=str, ensure_ascii=False))
print(f"\nSaved to {outfile}")

# Show the new king details
best = results[0]
print(f"\n{'='*60}")
if best["pnl"] > KING_PNL:
    print(f"NEW KING: {best['name']}")
else:
    print(f"KING DEFENDED: {results[0]['name']}")
print(f"  PnL: ${best['pnl']:+,.0f} (vs old king ${KING_PNL:+,.0f}, delta ${best['pnl']-KING_PNL:+,.0f})")
print(f"  Trades: {best['trades']} | WR: {best['wr']:.1f}% | Sharpe: {best['sharpe']:.2f}")
print(f"  LONG: {best['long_n']} ({best['long_wr']:.1f}%) ${best['long_pnl']:+,.0f}")
print(f"  SHORT: {best['short_n']} ({best['short_wr']:.1f}%) ${best['short_pnl']:+,.0f}")
print(f"  {best['desc']}")
print(f"{'='*60}")
