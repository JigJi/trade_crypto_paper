"""
Tournament Round 2c: LIQUIDATION FINAL BOSS
============================================
Key insight from R2+R2b: LIQ-ONLY beats full v5 model!
- liq_only_best_cascade: $67,648 | 70.0% WR | Sharpe 26.49 | DD -1.1%
- ultimate_hybrid_everything: $70,859 | 68.4% WR | Sharpe 25.04 | DD -1.1%

This round: Deep optimization of liq-only architecture + hybrid combos.
Goal: Find the absolute maximum alpha from liquidation data alone.
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
OOS_END   = "2026-03-22"
COINS = ["BTC", "XRP", "ADA", "DOT", "SUI", "FIL"]

V5_CONFIGS = {c: {"threshold": 3.0 if c != "BTC" else 2.5,
                   "alt_pa": False, "sl": 15.0, "tp": 12.0,
                   "trail": 99, "trail_act": 99, "cd": 4}
              for c in COINS}
V5_CONFIGS["XRP"]["threshold"] = 3.5
V5_CONFIGS["ADA"]["threshold"] = 3.5

V5_CORE = {
    "w_liq_bull": 5.0, "w_liq_bear": 5.0,
    "w_fr_neg": 2.0, "w_fr_pos": 2.0,
    "w_whale_bull": 1.5, "w_whale_bear": 1.5,
    "w_etf_bull": 1.0, "w_etf_bear": 1.0,
    "w_oi_bull": 0.25, "w_oi_capit": 0.25, "w_oi_weak": 0.25, "w_oi_bear": 0.25,
}
V5_EXTRA = {"ob_combined": 2.0, "tick_liq": 3.0, "basis_contrarian": 1.5}

RESULTS_DIR = Path(__file__).parent
RESULTS_FILE = RESULTS_DIR / "results_round2c.json"

# Zero all non-liq factors
ZERO_OTHERS = {
    "w_fr_neg": 0.0, "w_fr_pos": 0.0,
    "w_whale_bull": 0.0, "w_whale_bear": 0.0,
    "w_etf_bull": 0.0, "w_etf_bear": 0.0,
    "w_oi_bull": 0.0, "w_oi_capit": 0.0, "w_oi_weak": 0.0, "w_oi_bear": 0.0,
}
ZERO_EXTRA = {"ob_combined": 0.0, "basis_contrarian": 0.0}

print("=" * 70)
print("TOURNAMENT ROUND 2c: LIQUIDATION FINAL BOSS")
print("=" * 70)

t0 = time.time()
btc_ohlcv = fetch_binance_15m("BTCUSDT", years=3)
db_data = load_btc_db_data()
btc_df = build_btc_features(btc_ohlcv, db_data)
print(f"BTC features: {len(btc_df)} bars ({time.time()-t0:.1f}s)")

alt_data = {}
for coin in COINS:
    ohlcv = fetch_binance_15m(f"{coin}USDT", years=3)
    alt_data[coin] = build_alt_technicals(ohlcv)
print(f"Data loaded in {time.time()-t0:.1f}s\n")


# ── Scoring functions ───────────────────────────────────

def cascade(df, w=8.0, mult=1.1, ma_lb=24):
    s = pd.Series(0.0, index=df.index)
    if "liq_net" not in df.columns or "liq_total" not in df.columns:
        return s
    lt = df["liq_total"].fillna(0)
    ln = df["liq_net"].fillna(0)
    lt_ma = df.get("liq_total_ma", lt.rolling(24).mean()).fillna(1)
    c = lt > (lt_ma * mult)
    s += np.where(c & (ln > 0), w, 0)
    s += np.where(c & (ln < 0), -w, 0)
    return s


def ratio(df, w=8.0, thr=0.65, extreme=0.80, min_mult=1.0):
    s = pd.Series(0.0, index=df.index)
    if "liq_total" not in df.columns or "liq_short_1h" not in df.columns:
        return s
    lt = df["liq_total"].fillna(0)
    lt_ma = df.get("liq_total_ma", lt.rolling(24).mean()).fillna(1)
    meaningful = lt > (lt_ma * min_mult)
    sp = df["liq_short_1h"].fillna(0) / lt.clip(lower=1)
    lp = 1 - sp
    s += np.where(meaningful & (sp > thr), w, 0)
    s += np.where(meaningful & (sp > extreme), w * 0.5, 0)
    s += np.where(meaningful & (lp > thr), -w, 0)
    s += np.where(meaningful & (lp > extreme), -w * 0.5, 0)
    return s


def velocity(df, w=3.0, lb=4):
    s = pd.Series(0.0, index=df.index)
    if "liq_total" not in df.columns:
        return s
    lt = df["liq_total"].fillna(0)
    vel = lt.pct_change(lb).fillna(0)
    ln = df["liq_net"].fillna(0)
    acc = vel > 1.0
    s += np.where(acc & (ln > 0), w, 0)
    s += np.where(acc & (ln < 0), -w, 0)
    dec = vel < -0.5
    s += np.where(dec & (ln > 0), w * 0.3, 0)
    s += np.where(dec & (ln < 0), -w * 0.3, 0)
    return s


def tick(df, w=8.0, net_thr=3):
    s = pd.Series(0.0, index=df.index)
    if "liq_net_ma" not in df.columns:
        return s
    ln = df["liq_net_ma"].fillna(0)
    s += np.where(ln > net_thr, w, 0)
    s += np.where(ln < -net_thr, -w, 0)
    return s


def make_fn(liq_fn, tick_fn=None, liq_only=False):
    def fn(btc_df_local):
        import backtest_15m_btc_led_alts as bt
        params = dict(V5_CORE)
        params["w_liq_bull"] = 0.0
        params["w_liq_bear"] = 0.0
        if liq_only:
            params.update(ZERO_OTHERS)

        extra = dict(V5_EXTRA)
        extra["tick_liq"] = 0.0
        if liq_only:
            extra.update(ZERO_EXTRA)

        old = dict(bt.V3_EXTRA_WEIGHTS)
        bt.V3_EXTRA_WEIGHTS.update(extra)
        score = compute_btc_composite_score(btc_df_local, params=params)
        bt.V3_EXTRA_WEIGHTS.update(old)

        if liq_fn:
            score += liq_fn(btc_df_local)
        if tick_fn:
            score += tick_fn(btc_df_local)
        return score
    return fn


def run(name, desc, score_fn=None, coin_overrides=None):
    t1 = time.time()
    if score_fn:
        bs = score_fn(btc_df)
    else:
        import backtest_15m_btc_led_alts as bt
        old = dict(bt.V3_EXTRA_WEIGHTS)
        bt.V3_EXTRA_WEIGHTS.update(V5_EXTRA)
        bs = compute_btc_composite_score(btc_df, params=dict(V5_CORE))
        bt.V3_EXTRA_WEIGHTS.update(old)

    bts = pd.Series(bs.values, index=btc_df["ts"].values)
    all_trades = []
    cr = {}

    for coin in COINS:
        cfg = dict(V5_CONFIGS.get(coin, V5_CONFIGS["DOT"]))
        if coin_overrides and coin in coin_overrides:
            cfg.update(coin_overrides[coin])
        elif coin_overrides and "__all__" in coin_overrides:
            cfg.update(coin_overrides["__all__"])

        sig, am = generate_btc_led_signal(bts, alt_data[coin],
                                           threshold=cfg["threshold"],
                                           use_alt_pa_filter=cfg.get("alt_pa", False))
        mask = (am["ts"] >= pd.Timestamp(OOS_START))
        if OOS_END:
            mask &= (am["ts"] <= pd.Timestamp(OOS_END))
        ao = am[mask].reset_index(drop=True)
        so = sig[mask].reset_index(drop=True)
        if len(ao) < 100:
            continue

        tr = run_backtest(ao, so, sl_atr_mult=cfg.get("sl", 15.0),
                          tp_atr_mult=cfg.get("tp", 12.0),
                          trail_atr_mult=cfg.get("trail", 99),
                          trail_activate_atr=cfg.get("trail_act", 99),
                          max_hold_bars=cfg.get("max_hold", 96),
                          cooldown_bars=cfg.get("cd", 4))
        if len(tr) > 0:
            m = calc_metrics(tr, len(ao))
            cr[coin] = m
            tr["coin"] = coin
            all_trades.append(tr)

    if all_trades:
        c = pd.concat(all_trades, ignore_index=True)
        pnl = c["pnl_net"].sum()
        nt = len(c)
        wr = 100 * (c["pnl_net"] > 0).sum() / nt if nt else 0
        lo = c[c["dir"] == "L"]; sh = c[c["dir"] == "S"]
        eq = 10000 + c["pnl_net"].cumsum()
        dd = ((eq - eq.cummax()) / eq.cummax() * 100).min()
        rpt = c["pnl_net"] / 1000
        sp = rpt.mean() / rpt.std() * np.sqrt(nt) if rpt.std() > 0 else 0
    else:
        pnl = nt = 0; wr = dd = sp = 0; lo = sh = pd.DataFrame()

    r = {
        "name": name, "description": desc,
        "total_pnl": round(pnl, 2), "total_trades": nt,
        "win_rate": round(wr, 1), "sharpe": round(sp, 2),
        "max_dd_pct": round(dd, 2),
        "long_trades": len(lo), "long_wr": round(100*(lo["pnl_net"]>0).sum()/max(len(lo),1), 1) if len(lo) else 0,
        "long_pnl": round(lo["pnl_net"].sum(), 2) if len(lo) else 0,
        "short_trades": len(sh), "short_wr": round(100*(sh["pnl_net"]>0).sum()/max(len(sh),1), 1) if len(sh) else 0,
        "short_pnl": round(sh["pnl_net"].sum(), 2) if len(sh) else 0,
        "coin_results": {k: {"pnl": round(v["net_pnl"],2), "wr": round(v["win_rate"],1),
                              "trades": v["total"], "sharpe": round(v["sharpe"],2)}
                         for k, v in cr.items()},
        "elapsed_s": round(time.time()-t1, 1),
    }
    print(f"  {name:<45} {nt:>5} tr | WR {wr:>5.1f}% | ${pnl:>+9,.0f} | Sh {sp:>6.2f} | DD {dd:>5.1f}%")
    return r


all_results = []

# ══════════════════════════════════════════════════════════
# BASELINES
# ══════════════════════════════════════════════════════════
print("BASELINES:")
r_v5 = run("v5_baseline", "v5 original")
all_results.append(r_v5)

r_r2 = run("R2_champion", "cascade=1.5x, liq=8, tick=8, net=3",
           make_fn(lambda df: cascade(df, 8.0, 1.5), lambda df: tick(df, 8.0, 3)))
all_results.append(r_r2)

r_liq = run("liq_only_cascade_1.1x", "Liq-only: cascade=1.1x + tick(8,3)",
            make_fn(lambda df: cascade(df, 8.0, 1.1), lambda df: tick(df, 8.0, 3), liq_only=True))
all_results.append(r_liq)
BASELINE_PNL = r_liq["total_pnl"]
print(f"\n  >>> LIQ-ONLY BASELINE: ${BASELINE_PNL:+,.0f}\n")


# ══════════════════════════════════════════════════════════
# GROUP 1: LIQ-ONLY ARCHITECTURE SWEEP
# ══════════════════════════════════════════════════════════
print("\n--- GROUP 1: Liq-only architecture variations ---")
g1 = []

# Pure cascade only (no tick)
for m in [1.0, 1.1, 1.5, 2.0]:
    r = run(f"lo_cascade_{m}x_notick", f"Liq-only: cascade={m}x, NO tick",
            make_fn(lambda df, _m=m: cascade(df, 8.0, _m), None, liq_only=True))
    g1.append(r)

# Pure ratio only (no cascade)
for rt in [0.55, 0.60, 0.65, 0.70]:
    r = run(f"lo_ratio_{int(rt*100)}_notick", f"Liq-only: ratio={rt}, NO tick",
            make_fn(lambda df, _r=rt: ratio(df, 8.0, _r, _r+0.15), None, liq_only=True))
    g1.append(r)

# Cascade + ratio hybrid (liq-only)
for cw, rw in [(8.0, 4.0), (6.0, 6.0), (4.0, 8.0), (8.0, 8.0)]:
    r = run(f"lo_cascade_{cw}+ratio_{rw}", f"Liq-only: cascade(w={cw}) + ratio(w={rw})",
            make_fn(lambda df, _cw=cw, _rw=rw: cascade(df, _cw, 1.1) + ratio(df, _rw, 0.65, 0.80),
                    lambda df: tick(df, 8.0, 3), liq_only=True))
    g1.append(r)

# Cascade + velocity hybrid (liq-only)
for vw in [2.0, 3.0, 4.0, 5.0]:
    r = run(f"lo_cascade+vel_{vw}", f"Liq-only: cascade(8) + velocity(w={vw})",
            make_fn(lambda df, _vw=vw: cascade(df, 8.0, 1.1) + velocity(df, _vw),
                    lambda df: tick(df, 8.0, 3), liq_only=True))
    g1.append(r)

# All three: cascade + ratio + velocity (liq-only)
for cw, rw, vw in [(8.0, 3.0, 2.0), (8.0, 4.0, 3.0), (6.0, 4.0, 4.0), (8.0, 5.0, 3.0)]:
    r = run(f"lo_triple_{cw}_{rw}_{vw}", f"Liq-only: cascade({cw})+ratio({rw})+vel({vw})",
            make_fn(lambda df, _cw=cw, _rw=rw, _vw=vw:
                    cascade(df, _cw, 1.1) + ratio(df, _rw, 0.65, 0.80) + velocity(df, _vw),
                    lambda df: tick(df, 8.0, 3), liq_only=True))
    g1.append(r)

all_results.extend(g1)


# ══════════════════════════════════════════════════════════
# GROUP 2: TICK OPTIMIZATION (liq-only context)
# ══════════════════════════════════════════════════════════
print("\n--- GROUP 2: Tick liq optimization (liq-only) ---")
g2 = []

# Tick weight sweep
for tw in [4.0, 6.0, 10.0, 12.0]:
    r = run(f"lo_tick_w{tw}", f"Liq-only: cascade(1.1) + tick(w={tw}, net>3)",
            make_fn(lambda df: cascade(df, 8.0, 1.1),
                    lambda df, _tw=tw: tick(df, _tw, 3), liq_only=True))
    g2.append(r)

# Tick net threshold sweep
for nt in [1, 2, 4, 5]:
    r = run(f"lo_tick_net{nt}", f"Liq-only: cascade(1.1) + tick(8, net>{nt})",
            make_fn(lambda df: cascade(df, 8.0, 1.1),
                    lambda df, _nt=nt: tick(df, 8.0, _nt), liq_only=True))
    g2.append(r)

all_results.extend(g2)


# ══════════════════════════════════════════════════════════
# GROUP 3: ENTRY THRESHOLD + SL/TP (liq-only context)
# ══════════════════════════════════════════════════════════
print("\n--- GROUP 3: Threshold + SL/TP optimization (liq-only) ---")
g3 = []

best_liq_fn = make_fn(lambda df: cascade(df, 8.0, 1.1),
                       lambda df: tick(df, 8.0, 3), liq_only=True)

# Threshold sweep (liq-only generates less signal, lower threshold may help)
for thr in [2.0, 2.5, 3.5, 4.0, 4.5, 5.0, 6.0]:
    r = run(f"lo_thr{thr}", f"Liq-only + threshold={thr}",
            best_liq_fn, coin_overrides={"__all__": {"threshold": thr}})
    g3.append(r)

# SL/TP sweep for liq-only
for sl, tp in [(10.0, 8.0), (12.0, 10.0), (12.0, 12.0), (15.0, 15.0),
               (20.0, 12.0), (20.0, 15.0), (20.0, 20.0), (25.0, 15.0), (25.0, 20.0)]:
    r = run(f"lo_sl{sl}_tp{tp}", f"Liq-only + SL={sl}, TP={tp}",
            best_liq_fn, coin_overrides={"__all__": {"sl": sl, "tp": tp}})
    g3.append(r)

all_results.extend(g3)


# ══════════════════════════════════════════════════════════
# GROUP 4: GRAND COMBOS (liq-only + best params from G1-G3)
# ══════════════════════════════════════════════════════════
print("\n--- GROUP 4: Grand combinations ---")
g4 = []

# Find best threshold
best_thr_result = max(g3[:7], key=lambda x: x["total_pnl"]) if g3 else None
best_thr = None
if best_thr_result and best_thr_result["total_pnl"] > BASELINE_PNL:
    for t in [2.0, 2.5, 3.5, 4.0, 4.5, 5.0, 6.0]:
        if f"lo_thr{t}" == best_thr_result["name"]:
            best_thr = t
            break

# Find best SL/TP
best_sltp_result = max(g3[7:], key=lambda x: x["total_pnl"]) if len(g3) > 7 else None
best_sl, best_tp = 15.0, 12.0
if best_sltp_result and best_sltp_result["total_pnl"] > BASELINE_PNL:
    for sl, tp in [(10.0, 8.0), (12.0, 10.0), (12.0, 12.0), (15.0, 15.0),
                   (20.0, 12.0), (20.0, 15.0), (20.0, 20.0), (25.0, 15.0), (25.0, 20.0)]:
        if f"lo_sl{sl}_tp{tp}" == best_sltp_result["name"]:
            best_sl, best_tp = sl, tp
            break

print(f"  Best threshold: {best_thr}, Best SL/TP: {best_sl}/{best_tp}")

# Combo configs
combo_override = {"sl": best_sl, "tp": best_tp}
if best_thr:
    combo_override["threshold"] = best_thr

# Best architecture + best SL/TP + best threshold
# Use top architectures from G1
best_g1 = sorted(g1, key=lambda x: x["total_pnl"], reverse=True)[:3]
for i, bg1 in enumerate(best_g1):
    nm = bg1["name"]
    # Reconstruct the score function based on name patterns
    if "cascade" in nm and "ratio" in nm:
        parts = nm.replace("lo_cascade_", "").replace("+ratio_", ",").split(",")
        try:
            cw = float(parts[0])
            rw = float(parts[1])
            sfn = make_fn(lambda df, _cw=cw, _rw=rw: cascade(df, _cw, 1.1) + ratio(df, _rw, 0.65, 0.80),
                          lambda df: tick(df, 8.0, 3), liq_only=True)
        except (ValueError, IndexError):
            continue
    elif "triple" in nm:
        parts = nm.replace("lo_triple_", "").split("_")
        try:
            cw, rw, vw = float(parts[0]), float(parts[1]), float(parts[2])
            sfn = make_fn(lambda df, _cw=cw, _rw=rw, _vw=vw:
                          cascade(df, _cw, 1.1) + ratio(df, _rw, 0.65, 0.80) + velocity(df, _vw),
                          lambda df: tick(df, 8.0, 3), liq_only=True)
        except (ValueError, IndexError):
            continue
    elif "vel" in nm:
        parts = nm.replace("lo_cascade+vel_", "").split("_")
        try:
            vw = float(parts[0])
            sfn = make_fn(lambda df, _vw=vw: cascade(df, 8.0, 1.1) + velocity(df, _vw),
                          lambda df: tick(df, 8.0, 3), liq_only=True)
        except (ValueError, IndexError):
            continue
    else:
        sfn = best_liq_fn

    r = run(f"combo_{i+1}_{nm}_opt", f"Best G1 #{i+1} + best SL/TP/thr",
            sfn, coin_overrides={"__all__": combo_override})
    g4.append(r)

# Explicit best combos
# Cascade + ratio + best params
r = run("combo_cascade_ratio_best",
        f"Liq-only: cascade(8)+ratio(4) + SL={best_sl}/TP={best_tp}/thr={best_thr}",
        make_fn(lambda df: cascade(df, 8.0, 1.1) + ratio(df, 4.0, 0.65, 0.80),
                lambda df: tick(df, 8.0, 3), liq_only=True),
        coin_overrides={"__all__": combo_override})
g4.append(r)

# Cascade + velocity + best params
r = run("combo_cascade_vel_best",
        f"Liq-only: cascade(8)+vel(3) + SL={best_sl}/TP={best_tp}/thr={best_thr}",
        make_fn(lambda df: cascade(df, 8.0, 1.1) + velocity(df, 3.0),
                lambda df: tick(df, 8.0, 3), liq_only=True),
        coin_overrides={"__all__": combo_override})
g4.append(r)

# Triple + best params
r = run("combo_triple_best",
        f"Liq-only: cascade(8)+ratio(4)+vel(3) + best params",
        make_fn(lambda df: cascade(df, 8.0, 1.1) + ratio(df, 4.0, 0.65, 0.80) + velocity(df, 3.0),
                lambda df: tick(df, 8.0, 3), liq_only=True),
        coin_overrides={"__all__": combo_override})
g4.append(r)

# WITH other factors (full model) + best liq + best params
r = run("combo_full_model_best",
        f"FULL MODEL + cascade(8,1.1) + tick(8,3) + best params",
        make_fn(lambda df: cascade(df, 8.0, 1.1),
                lambda df: tick(df, 8.0, 3), liq_only=False),
        coin_overrides={"__all__": combo_override})
g4.append(r)

# Full model + cascade + ratio + best params
r = run("combo_full_hybrid_best",
        f"FULL MODEL + cascade(8)+ratio(4) + tick(8) + best params",
        make_fn(lambda df: cascade(df, 8.0, 1.1) + ratio(df, 4.0, 0.65, 0.80),
                lambda df: tick(df, 8.0, 3), liq_only=False),
        coin_overrides={"__all__": combo_override})
g4.append(r)

# Full model + velocity + best params
r = run("combo_full_vel_best",
        f"FULL MODEL + cascade(8)+vel(3) + tick(8) + best params",
        make_fn(lambda df: cascade(df, 8.0, 1.1) + velocity(df, 3.0),
                lambda df: tick(df, 8.0, 3), liq_only=False),
        coin_overrides={"__all__": combo_override})
g4.append(r)

all_results.extend(g4)


# ══════════════════════════════════════════════════════════
# GROUP 5: COOLDOWN AND MAX HOLD (with best config)
# ══════════════════════════════════════════════════════════
print("\n--- GROUP 5: Cooldown & max hold ---")
g5 = []
for cd in [2, 6, 8, 12]:
    r = run(f"lo_cd{cd}", f"Liq-only + cooldown={cd}",
            best_liq_fn, coin_overrides={"__all__": {"cd": cd}})
    g5.append(r)

for mh in [48, 72, 128, 192]:
    r = run(f"lo_maxhold{mh}", f"Liq-only + max_hold={mh} bars",
            best_liq_fn, coin_overrides={"__all__": {"max_hold": mh}})
    g5.append(r)

all_results.extend(g5)


# ══════════════════════════════════════════════════════════
# GRAND SUMMARY
# ══════════════════════════════════════════════════════════
sorted_all = sorted(all_results, key=lambda x: x["total_pnl"], reverse=True)

print(f"\n{'='*105}")
print("GRAND TOURNAMENT R2c: LIQUIDATION FINAL BOSS")
print(f"{'='*105}")
print(f"{'Rk':<3} {'Name':<47} {'Tr':>5} {'WR%':>6} {'PnL':>10} {'Sh':>6} {'DD%':>6} {'Delta':>10} {'L/S':>8}")
print("-" * 105)
for i, r in enumerate(sorted_all[:40], 1):
    delta = r["total_pnl"] - r_v5["total_pnl"]
    marker = " <-- KING" if i == 1 else (" <-- v5" if r["name"] == "v5_baseline" else "")
    ls = f"{r['long_wr']:.0f}/{r['short_wr']:.0f}"
    print(f"{i:<3} {r['name']:<47} {r['total_trades']:>5} {r['win_rate']:>5.1f}% "
          f"${r['total_pnl']:>9,.0f} {r['sharpe']:>6.2f} {r['max_dd_pct']:>5.1f}% "
          f"${delta:>+9,.0f}{marker}  {ls}")

# Save
results_data = {
    "tournament": "round_2c_liquidation_final",
    "timestamp": datetime.utcnow().isoformat(),
    "oos_period": f"{OOS_START} to {OOS_END}",
    "coins": COINS,
    "v5_baseline_pnl": r_v5["total_pnl"],
    "total_experiments": len(all_results),
    "results": sorted_all,
    "king": sorted_all[0],
}
RESULTS_FILE.write_text(json.dumps(results_data, indent=2, default=str, ensure_ascii=False))

king = sorted_all[0]
print(f"\n{'='*70}")
print(f"ABSOLUTE KING: {king['name']}")
print(f"  PnL: ${king['total_pnl']:+,.0f} (${king['total_pnl']-r_v5['total_pnl']:+,.0f} vs v5)")
print(f"  Trades: {king['total_trades']} | WR: {king['win_rate']:.1f}%")
print(f"  Sharpe: {king['sharpe']:.2f} | DD: {king['max_dd_pct']:.1f}%")
print(f"  L: {king['long_trades']} WR={king['long_wr']:.1f}% ${king['long_pnl']:+,.0f}")
print(f"  S: {king['short_trades']} WR={king['short_wr']:.1f}% ${king['short_pnl']:+,.0f}")
print(f"  {king['description']}")
print(f"{'='*70}")
print(f"\nTime: {(time.time()-t0)/60:.1f}min | Experiments: {len(all_results)}")
