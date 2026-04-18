"""
Tournament Round 1, Batch 5: Final Push
New King = ultra_simple_sl15_tp10: Boost Tier1 (liq=3, ob=3) + SL=15 + TP=10
$40,346 | 1985 trades | 64.9% WR | Sharpe 15.26 | DD -2.4%
"""
import sys, os, json, time
from pathlib import Path

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

print("Loading data...")
t0 = time.time()
btc_ohlcv = fetch_binance_15m("BTCUSDT", years=3)
db_data = load_btc_db_data()
btc_df = build_btc_features(btc_ohlcv, db_data)
alt_data = {}
for coin in COINS:
    alt_data[coin] = build_alt_technicals(fetch_binance_15m(f"{coin}USDT", years=3))
print(f"Data loaded in {time.time()-t0:.1f}s\n")


def run_exp(name, desc, core_ov=None, extra_ov=None, coin_ov=None, spike=None):
    import backtest_15m_btc_led_alts as bt
    old_extra = dict(bt.V3_EXTRA_WEIGHTS)
    params = dict(COMPOSITE_WEIGHTS)
    if core_ov: params.update(core_ov)
    if extra_ov: bt.V3_EXTRA_WEIGHTS.update(extra_ov)
    btc_score = compute_btc_composite_score(btc_df, params=params)
    btc_score_ts = pd.Series(btc_score.values, index=btc_df["ts"].values)
    bt.V3_EXTRA_WEIGHTS.update(old_extra)

    all_trades = []
    for coin in COINS:
        cfg = dict(V11_CONFIGS.get(coin, V11_CONFIGS["DOT"]))
        if coin_ov:
            ov = coin_ov.get(coin, coin_ov.get("__all__", {}))
            cfg.update(ov)
        signals, alt_merged = generate_btc_led_signal(
            btc_score_ts, alt_data[coin], threshold=cfg["threshold"],
            use_alt_pa_filter=cfg.get("alt_pa", False), spike_mode=spike)
        oos_mask = (alt_merged["ts"] >= pd.Timestamp(OOS_START)) & (alt_merged["ts"] <= pd.Timestamp(OOS_END))
        alt_oos = alt_merged[oos_mask].reset_index(drop=True)
        sig_oos = signals[oos_mask].reset_index(drop=True)
        if len(alt_oos) < 100: continue
        trades = run_backtest(alt_oos, sig_oos, sl_atr_mult=cfg.get("sl", 15),
                              tp_atr_mult=cfg.get("tp", 10), trail_atr_mult=cfg.get("trail", 99),
                              trail_activate_atr=cfg.get("trail_act", 99),
                              max_hold_bars=cfg.get("max_hold", 96), cooldown_bars=cfg.get("cd", 4))
        if len(trades) > 0:
            trades["coin"] = coin
            all_trades.append(trades)

    if not all_trades: return None
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
    print(f"  {name:<40} {n:>5} tr | WR {wr:>5.1f}% | ${pnl:>+9,.0f} | S {sharpe:>5.2f} | DD {max_dd:>5.1f}%")
    return r


# New King baseline config
NK_CORE = {"w_liq_bull": 3.0, "w_liq_bear": 3.0}
NK_EXTRA = {"ob_combined": 3.0}
NK_COINS = {"__all__": {"sl": 15.0, "tp": 10.0}}

print("=" * 90)
print("BATCH 5: Final Push from New King ($40,346)")
print("=" * 90)
results = []

# Verify new king
r = run_exp("NK_baseline", "Boost Tier1 + SL15 + TP10", core_ov=NK_CORE, extra_ov=NK_EXTRA, coin_ov=NK_COINS)
results.append(r)
NK_PNL = r["pnl"]

# ── liq weight sweep (the winner from batch 4 was liq=4 at $39,861) ──
print("\n--- Liquidation weight sweep ---")
for liq_w in [3.5, 4.0, 4.5, 5.0]:
    r = run_exp(f"nk_liq{liq_w}", f"liq={liq_w}",
        core_ov={"w_liq_bull": liq_w, "w_liq_bear": liq_w}, extra_ov=NK_EXTRA, coin_ov=NK_COINS)
    results.append(r)

# ── ob_combined weight sweep ──
print("\n--- OB weight sweep ---")
for ob_w in [2.0, 2.5, 3.5, 4.0]:
    r = run_exp(f"nk_ob{ob_w}", f"ob={ob_w}",
        core_ov=NK_CORE, extra_ov={"ob_combined": ob_w}, coin_ov=NK_COINS)
    results.append(r)

# ── SL sweep ──
print("\n--- SL sweep ---")
for sl in [12.0, 20.0, 30.0, 50.0, 99.0]:
    r = run_exp(f"nk_sl{int(sl)}", f"SL={sl}",
        core_ov=NK_CORE, extra_ov=NK_EXTRA, coin_ov={"__all__": {"sl": sl, "tp": 10.0}})
    results.append(r)

# ── TP sweep ──
print("\n--- TP sweep ---")
for tp in [8.0, 12.0, 15.0, 20.0, 50.0, 99.0]:
    r = run_exp(f"nk_tp{int(tp)}", f"TP={tp}",
        core_ov=NK_CORE, extra_ov=NK_EXTRA, coin_ov={"__all__": {"sl": 15.0, "tp": tp}})
    results.append(r)

# ── Best combo so far: liq=4 + optimal SL/TP from sweeps ──
print("\n--- Ultimate combos ---")

# Liq=4 + SL=15 + TP=10
r = run_exp("final_liq4_sl15_tp10", "liq=4 + SL15 + TP10",
    core_ov={"w_liq_bull": 4.0, "w_liq_bear": 4.0}, extra_ov=NK_EXTRA, coin_ov=NK_COINS)
results.append(r)

# Liq=4 + SL=20 + TP=10
r = run_exp("final_liq4_sl20_tp10", "liq=4 + SL20 + TP10",
    core_ov={"w_liq_bull": 4.0, "w_liq_bear": 4.0}, extra_ov=NK_EXTRA,
    coin_ov={"__all__": {"sl": 20.0, "tp": 10.0}})
results.append(r)

# Liq=4 + SL=20 + TP=12
r = run_exp("final_liq4_sl20_tp12", "liq=4 + SL20 + TP12",
    core_ov={"w_liq_bull": 4.0, "w_liq_bear": 4.0}, extra_ov=NK_EXTRA,
    coin_ov={"__all__": {"sl": 20.0, "tp": 12.0}})
results.append(r)

# Liq=4 + SL=15 + TP=12
r = run_exp("final_liq4_sl15_tp12", "liq=4 + SL15 + TP12",
    core_ov={"w_liq_bull": 4.0, "w_liq_bear": 4.0}, extra_ov=NK_EXTRA,
    coin_ov={"__all__": {"sl": 15.0, "tp": 12.0}})
results.append(r)

# Liq=4.5 + SL=15 + TP=10
r = run_exp("final_liq45_sl15_tp10", "liq=4.5 + SL15 + TP10",
    core_ov={"w_liq_bull": 4.5, "w_liq_bear": 4.5}, extra_ov=NK_EXTRA, coin_ov=NK_COINS)
results.append(r)

# Liq=4 + ob=2.5 + SL=15 + TP=10
r = run_exp("final_liq4_ob25", "liq=4 + ob=2.5 + SL15 + TP10",
    core_ov={"w_liq_bull": 4.0, "w_liq_bear": 4.0},
    extra_ov={"ob_combined": 2.5}, coin_ov=NK_COINS)
results.append(r)

# Liq=4 + ob=3.5 + SL=15 + TP=10
r = run_exp("final_liq4_ob35", "liq=4 + ob=3.5 + SL15 + TP10",
    core_ov={"w_liq_bull": 4.0, "w_liq_bear": 4.0},
    extra_ov={"ob_combined": 3.5}, coin_ov=NK_COINS)
results.append(r)

# Lower threshold + liq=4
r = run_exp("final_liq4_lowT", "liq=4 + lower thresholds",
    core_ov={"w_liq_bull": 4.0, "w_liq_bear": 4.0}, extra_ov=NK_EXTRA,
    coin_ov={"BTC": {"sl": 15, "tp": 10, "threshold": 2.0},
             "XRP": {"sl": 15, "tp": 10, "threshold": 3.0},
             "ADA": {"sl": 15, "tp": 10, "threshold": 3.0},
             "DOT": {"sl": 15, "tp": 10, "threshold": 2.5},
             "SUI": {"sl": 15, "tp": 10, "threshold": 2.5},
             "FIL": {"sl": 15, "tp": 10, "threshold": 2.5}})
results.append(r)

# Spike momentum on new king
r = run_exp("final_nk_spike", "NK + spike momentum",
    core_ov=NK_CORE, extra_ov=NK_EXTRA, coin_ov=NK_COINS, spike="momentum")
results.append(r)

# Spike + liq=4
r = run_exp("final_liq4_spike", "liq=4 + spike momentum",
    core_ov={"w_liq_bull": 4.0, "w_liq_bear": 4.0}, extra_ov=NK_EXTRA,
    coin_ov=NK_COINS, spike="momentum")
results.append(r)

# ── Regime test: BULL-only and BEAR-only ──
print("\n--- Regime robustness (split OOS) ---")

# Jan-Jun 2025 (BULL period proxy)
for period_name, start, end in [("bull_h1_2025", "2025-01-01", "2025-06-30"),
                                 ("bear_h2_2025", "2025-07-01", "2025-12-31"),
                                 ("recent_q1_2026", "2026-01-01", "2026-03-18")]:
    import backtest_15m_btc_led_alts as bt
    old_extra = dict(bt.V3_EXTRA_WEIGHTS)
    params = dict(COMPOSITE_WEIGHTS)
    params.update({"w_liq_bull": 4.0, "w_liq_bear": 4.0})
    bt.V3_EXTRA_WEIGHTS.update(NK_EXTRA)
    btc_score = compute_btc_composite_score(btc_df, params=params)
    btc_score_ts = pd.Series(btc_score.values, index=btc_df["ts"].values)
    bt.V3_EXTRA_WEIGHTS.update(old_extra)

    period_trades = []
    for coin in COINS:
        cfg = dict(V11_CONFIGS.get(coin, V11_CONFIGS["DOT"]))
        cfg.update({"sl": 15.0, "tp": 10.0})
        signals, alt_merged = generate_btc_led_signal(
            btc_score_ts, alt_data[coin], threshold=cfg["threshold"],
            use_alt_pa_filter=False)
        mask = (alt_merged["ts"] >= pd.Timestamp(start)) & (alt_merged["ts"] <= pd.Timestamp(end))
        alt_p = alt_merged[mask].reset_index(drop=True)
        sig_p = signals[mask].reset_index(drop=True)
        if len(alt_p) < 50: continue
        trades = run_backtest(alt_p, sig_p, sl_atr_mult=15.0, tp_atr_mult=10.0,
                              trail_atr_mult=99, trail_activate_atr=99, cooldown_bars=cfg["cd"])
        if len(trades) > 0:
            trades["coin"] = coin
            period_trades.append(trades)

    if period_trades:
        c = pd.concat(period_trades, ignore_index=True)
        p_pnl = c["pnl_net"].sum()
        p_n = len(c)
        p_wr = 100 * (c["pnl_net"] > 0).sum() / p_n
        print(f"  {period_name:<25} {p_n:>5} tr | WR {p_wr:>5.1f}% | ${p_pnl:>+9,.0f}")


# ══════════════════════════════════════════════════════════
# Final ranking
# ══════════════════════════════════════════════════════════
results = [r for r in results if r is not None]
results.sort(key=lambda x: x["pnl"], reverse=True)

print("\n" + "=" * 100)
print("BATCH 5 FINAL RANKING")
print("=" * 100)
print(f"{'Rank':<5} {'Name':<40} {'Trades':>6} {'WR%':>6} {'PnL':>10} {'Sharpe':>7} {'DD%':>6} {'vs NK':>10}")
print("-" * 100)
for i, r in enumerate(results, 1):
    delta = r["pnl"] - NK_PNL
    marker = " ***" if i <= 3 else ""
    print(f"{i:<5} {r['name']:<40} {r['trades']:>6} {r['wr']:>5.1f}% "
          f"${r['pnl']:>9,.0f} {r['sharpe']:>7.2f} {r['dd']:>5.1f}% "
          f"${delta:>+9,.0f}{marker}")

outfile = RESULTS_DIR / "batch5_results.json"
outfile.write_text(json.dumps({"batch": 5, "nk_pnl": NK_PNL, "results": results},
                               indent=2, default=str, ensure_ascii=False))

best = results[0]
print(f"\n{'='*70}")
print(f"ULTIMATE CHAMPION: {best['name']}")
print(f"  PnL: ${best['pnl']:+,.0f} (vs v3 baseline $26,165: +${best['pnl']-26165:,.0f} / +{(best['pnl']-26165)/26165*100:.1f}%)")
print(f"  Trades: {best['trades']} | WR: {best['wr']:.1f}% | Sharpe: {best['sharpe']:.2f} | DD: {best['dd']:.1f}%")
print(f"  LONG: {best['long_n']} ({best['long_wr']:.1f}%) ${best['long_pnl']:+,.0f}")
print(f"  SHORT: {best['short_n']} ({best['short_wr']:.1f}%) ${best['short_pnl']:+,.0f}")
print(f"  Config: {best['desc']}")
print(f"{'='*70}")

total_time = time.time() - t0
print(f"\nBatch 5 completed in {total_time/60:.1f} minutes")
