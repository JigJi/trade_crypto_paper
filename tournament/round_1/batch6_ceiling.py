"""
Batch 6: Find the absolute ceiling. Champion = liq=5.0, ob=3.0, SL=15, TP=10 ($45,635)
"""
import sys, os, json, time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR))
os.chdir(str(BASE_DIR))

import pandas as pd, numpy as np
from backtest_15m_btc_led_alts import (
    fetch_binance_15m, load_btc_db_data, build_btc_features,
    compute_btc_composite_score, build_alt_technicals,
    generate_btc_led_signal, run_backtest, calc_metrics,
    V3_EXTRA_WEIGHTS, COMPOSITE_WEIGHTS,
)

OOS_START, OOS_END = "2025-01-01", "2026-03-18"
COINS = ["BTC", "XRP", "ADA", "DOT", "SUI", "FIL"]
V11 = {"BTC": {"threshold": 2.5}, "XRP": {"threshold": 3.5}, "ADA": {"threshold": 3.5},
       "DOT": {"threshold": 3.0}, "SUI": {"threshold": 3.0}, "FIL": {"threshold": 3.0}}

print("Loading data...")
t0 = time.time()
btc_ohlcv = fetch_binance_15m("BTCUSDT", years=3)
db_data = load_btc_db_data()
btc_df = build_btc_features(btc_ohlcv, db_data)
alt_data = {c: build_alt_technicals(fetch_binance_15m(f"{c}USDT", years=3)) for c in COINS}
print(f"Loaded in {time.time()-t0:.1f}s\n")


def run(name, liq=5.0, ob=3.0, tick=2.0, basis=1.5, whale=1.5, fund=2.0,
        etf=1.0, oi=0.25, sl=15.0, tp=10.0, cd=4, max_hold=96, spike=None,
        thresholds=None):
    import backtest_15m_btc_led_alts as bt
    old = dict(bt.V3_EXTRA_WEIGHTS)
    bt.V3_EXTRA_WEIGHTS.update({"ob_combined": ob, "tick_liq": tick, "basis_contrarian": basis})
    params = dict(COMPOSITE_WEIGHTS)
    params.update({"w_liq_bull": liq, "w_liq_bear": liq, "w_whale_bull": whale, "w_whale_bear": whale,
                   "w_fr_neg": fund, "w_fr_pos": fund, "w_etf_bull": etf, "w_etf_bear": etf,
                   "w_oi_bull": oi, "w_oi_capit": oi, "w_oi_weak": oi, "w_oi_bear": oi})
    score = compute_btc_composite_score(btc_df, params=params)
    score_ts = pd.Series(score.values, index=btc_df["ts"].values)
    bt.V3_EXTRA_WEIGHTS.update(old)

    all_t = []
    for c in COINS:
        th = (thresholds or V11).get(c, V11.get(c, {"threshold": 3.0}))["threshold"] if isinstance((thresholds or V11).get(c), dict) else (thresholds or V11).get(c, 3.0)
        sig, am = generate_btc_led_signal(score_ts, alt_data[c], threshold=th, use_alt_pa_filter=False, spike_mode=spike)
        m = (am["ts"] >= pd.Timestamp(OOS_START)) & (am["ts"] <= pd.Timestamp(OOS_END))
        ao, so = am[m].reset_index(drop=True), sig[m].reset_index(drop=True)
        if len(ao) < 100: continue
        t = run_backtest(ao, so, sl_atr_mult=sl, tp_atr_mult=tp, trail_atr_mult=99, trail_activate_atr=99,
                         max_hold_bars=max_hold, cooldown_bars=cd)
        if len(t) > 0: t["coin"] = c; all_t.append(t)
    if not all_t: return None
    c = pd.concat(all_t, ignore_index=True)
    pnl = c["pnl_net"].sum(); n = len(c)
    wr = 100*(c["pnl_net"]>0).sum()/n
    L, S = c[c["dir"]=="L"], c[c["dir"]=="S"]
    eq = 10000+c["pnl_net"].cumsum(); dd = ((eq-eq.cummax())/eq.cummax()*100).min()
    ret = c["pnl_net"]/1000; sh = ret.mean()/ret.std()*np.sqrt(n) if ret.std()>0 else 0
    print(f"  {name:<45} {n:>5} | WR {wr:>5.1f}% | ${pnl:>+9,.0f} | S {sh:>5.2f} | DD {dd:>5.1f}%")
    return {"name": name, "pnl": round(pnl,2), "trades": n, "wr": round(wr,1),
            "sharpe": round(sh,2), "dd": round(dd,1),
            "long_n": len(L), "long_wr": round(100*(L["pnl_net"]>0).sum()/len(L) if len(L)>0 else 0,1),
            "long_pnl": round(L["pnl_net"].sum(),2),
            "short_n": len(S), "short_wr": round(100*(S["pnl_net"]>0).sum()/len(S) if len(S)>0 else 0,1),
            "short_pnl": round(S["pnl_net"].sum(),2)}

R = []
print("="*90)
print("BATCH 6: Finding the ceiling")
print("="*90)

# Champion baseline
r = run("CHAMPION_baseline", liq=5.0); R.append(r)
CHAMP = r["pnl"]

# ── Push liquidation higher ──
print("\n--- Liq ceiling ---")
for l in [5.5, 6.0, 7.0, 8.0, 10.0]:
    R.append(run(f"liq_{l}", liq=l))

# ── liq=5 + TP sweep ──
print("\n--- liq=5 + TP sweep ---")
for tp in [12, 15, 20, 25]:
    R.append(run(f"liq5_tp{tp}", liq=5.0, tp=tp))

# ── liq=5 + ob sweep ──
print("\n--- liq=5 + ob sweep ---")
for ob in [1.5, 2.0, 2.5, 4.0, 5.0]:
    R.append(run(f"liq5_ob{ob}", liq=5.0, ob=ob))

# ── liq=5 + other factor tweaks ──
print("\n--- Factor tweaks ---")
R.append(run("liq5_tick3", liq=5.0, tick=3.0))
R.append(run("liq5_tick4", liq=5.0, tick=4.0))
R.append(run("liq5_basis0", liq=5.0, basis=0.0))
R.append(run("liq5_basis2", liq=5.0, basis=2.0))
R.append(run("liq5_whale0", liq=5.0, whale=0.0))
R.append(run("liq5_fund0", liq=5.0, fund=0.0))
R.append(run("liq5_etf0", liq=5.0, etf=0.0))
R.append(run("liq5_oi0", liq=5.0, oi=0.0))

# ── SL/TP grid on liq=5 ──
print("\n--- SL/TP grid ---")
for sl, tp in [(12,10), (12,12), (15,12), (15,15), (20,12), (20,15), (99,10), (99,15)]:
    R.append(run(f"liq5_sl{sl}_tp{tp}", liq=5.0, sl=sl, tp=tp))

# ── Multi-factor boost combos ──
print("\n--- Multi-boost ---")
R.append(run("liq5_ob2_tick3", liq=5.0, ob=2.0, tick=3.0))
R.append(run("liq6_ob2", liq=6.0, ob=2.0))
R.append(run("liq6_ob2_tick3", liq=6.0, ob=2.0, tick=3.0))
R.append(run("liq7_ob2", liq=7.0, ob=2.0))
R.append(run("liq5_ob2_tp12", liq=5.0, ob=2.0, tp=12))
R.append(run("liq5_ob2_tp15", liq=5.0, ob=2.0, tp=15))
R.append(run("liq6_ob2_tp12", liq=6.0, ob=2.0, tp=12))
R.append(run("liq5_ob2_nofund", liq=5.0, ob=2.0, fund=0.0))
R.append(run("liq5_ob2_nowhale", liq=5.0, ob=2.0, whale=0.0))

# ── Threshold variations ──
print("\n--- Threshold variations ---")
LOW_T = {"BTC": {"threshold": 2.0}, "XRP": {"threshold": 3.0}, "ADA": {"threshold": 3.0},
         "DOT": {"threshold": 2.5}, "SUI": {"threshold": 2.5}, "FIL": {"threshold": 2.5}}
R.append(run("liq5_ob2_lowT", liq=5.0, ob=2.0, thresholds=LOW_T))
R.append(run("liq6_ob2_lowT", liq=6.0, ob=2.0, thresholds=LOW_T))

# ── Best of everything ──
print("\n--- Best combos ---")
R.append(run("GOD_liq5_ob2_tp12_sl15", liq=5.0, ob=2.0, tp=12, sl=15))
R.append(run("GOD_liq6_ob2_tp12_sl15", liq=6.0, ob=2.0, tp=12, sl=15))
R.append(run("GOD_liq5_ob2_tick3_tp12", liq=5.0, ob=2.0, tick=3.0, tp=12))
R.append(run("GOD_liq6_ob2_tick3_tp12", liq=6.0, ob=2.0, tick=3.0, tp=12))
R.append(run("GOD_liq7_ob2_tp12", liq=7.0, ob=2.0, tp=12))
R.append(run("GOD_liq8_ob2_tp12", liq=8.0, ob=2.0, tp=12))

# ── Summary ──
R = [r for r in R if r]
R.sort(key=lambda x: x["pnl"], reverse=True)

print("\n" + "="*105)
print(f"{'Rank':<5} {'Name':<45} {'Trades':>6} {'WR%':>6} {'PnL':>10} {'Sharpe':>7} {'DD%':>6} {'vs Champ':>10}")
print("-"*105)
for i, r in enumerate(R[:20], 1):
    d = r["pnl"] - CHAMP
    print(f"{i:<5} {r['name']:<45} {r['trades']:>6} {r['wr']:>5.1f}% ${r['pnl']:>9,.0f} {r['sharpe']:>7.2f} {r['dd']:>5.1f}% ${d:>+9,.0f}")
print(f"... ({len(R)} total)")

# Save
Path(__file__).parent.joinpath("batch6_results.json").write_text(
    json.dumps({"results": R}, indent=2, default=str, ensure_ascii=False))

best = R[0]
print(f"\n{'='*70}")
print(f"ABSOLUTE CHAMPION: {best['name']}")
print(f"  PnL: ${best['pnl']:+,.0f} (+{(best['pnl']-26165)/26165*100:.1f}% vs v3 baseline $26,165)")
print(f"  Trades: {best['trades']} | WR: {best['wr']:.1f}% | Sharpe: {best['sharpe']:.2f} | DD: {best['dd']:.1f}%")
print(f"  LONG: {best['long_n']} ({best['long_wr']:.1f}%) ${best['long_pnl']:+,.0f}")
print(f"  SHORT: {best['short_n']} ({best['short_wr']:.1f}%) ${best['short_pnl']:+,.0f}")
print(f"{'='*70}")
print(f"\nTotal: {time.time()-t0:.1f}s")
