"""
V6 + Extreme Vol Filter Test
=============================
Mission 013 found extreme_conf3 filter helps v5 (skip Extreme vol + <3 factors).
But v6 is liq-only (2 factors max), so the "3+ factors" rule won't work.
Need to test alternative vol filters for v6.
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
    compute_btc_composite_score_v6, compute_btc_composite_score,
    build_alt_technicals, generate_btc_led_signal,
    run_backtest, calc_metrics, V3_EXTRA_WEIGHTS, COMPOSITE_WEIGHTS,
)

RESULTS_DIR = Path(__file__).parent
RESULTS_FILE = RESULTS_DIR / "results_v6_extreme_vol.json"

OOS_START = "2025-01-01"
OOS_END = "2026-03-22"
COINS = ["BTC", "XRP", "ADA", "DOT", "SUI", "FIL"]

print("=" * 80)
print("V6 + EXTREME VOL FILTER TEST")
print("=" * 80)

t0 = time.time()
btc_ohlcv = fetch_binance_15m("BTCUSDT", years=3)
db_data = load_btc_db_data()
btc_df = build_btc_features(btc_ohlcv, db_data)

alt_data = {}
for coin in COINS:
    ohlcv = fetch_binance_15m(f"{coin}USDT", years=3)
    alt_data[coin] = build_alt_technicals(ohlcv)
print(f"Data loaded in {time.time()-t0:.1f}s")

# Compute volatility regime on BTC OHLCV
btc_close = btc_ohlcv.set_index("date_time")["close"]
btc_ret = btc_close.pct_change().fillna(0)
btc_rv = btc_ret.rolling(96).std() * 100  # 96 bars = 24h rolling realized vol
# Fixed thresholds from Mission 012
VOL_LOW = 0.2835
VOL_NORMAL = 0.5045
VOL_HIGH = 0.6644

def get_vol_regime(rv_val):
    if pd.isna(rv_val): return "normal"
    if rv_val < VOL_LOW: return "low"
    if rv_val < VOL_NORMAL: return "normal"
    if rv_val < VOL_HIGH: return "high"
    return "extreme"

vol_regime = btc_rv.apply(get_vol_regime)
vol_regime_aligned = vol_regime.reindex(btc_df["ts"].values, method="ffill").fillna("normal")


def run_with_vol_filter(name, desc, score_fn, vol_filter_fn=None,
                        sl=25.0, tp=20.0, max_hold=96):
    """Run backtest, optionally filtering signals based on vol regime."""
    t1 = time.time()
    bs = score_fn(btc_df)
    bts = pd.Series(bs.values, index=btc_df["ts"].values)

    all_trades = []
    cr = {}
    skipped_bars = 0
    total_signal_bars = 0

    for coin in COINS:
        thr = 3.0
        if coin == "BTC": thr = 2.5
        elif coin in ("XRP", "ADA"): thr = 3.5

        sig, am = generate_btc_led_signal(bts, alt_data[coin],
                                           threshold=thr, use_alt_pa_filter=False)
        mask = (am["ts"] >= pd.Timestamp(OOS_START))
        mask &= (am["ts"] <= pd.Timestamp(OOS_END))
        ao = am[mask].reset_index(drop=True)
        so = sig[mask].reset_index(drop=True)
        if len(ao) < 100:
            continue

        # Apply vol filter
        if vol_filter_fn is not None:
            regimes = vol_regime_aligned.reindex(ao["ts"].values, method="ffill").fillna("normal")
            scores = bts.reindex(ao["ts"].values, method="ffill").fillna(0)
            for i in range(len(so)):
                total_signal_bars += 1
                if so.iloc[i] != 0 and vol_filter_fn(regimes.iloc[i], scores.iloc[i], so.iloc[i]):
                    so.iloc[i] = 0
                    skipped_bars += 1

        tr = run_backtest(ao, so, sl_atr_mult=sl, tp_atr_mult=tp,
                          trail_atr_mult=99, trail_activate_atr=99,
                          max_hold_bars=max_hold, cooldown_bars=4)
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
        eq = 10000 + c["pnl_net"].cumsum()
        dd = ((eq - eq.cummax()) / eq.cummax() * 100).min()
        rpt = c["pnl_net"] / 1000
        sp = rpt.mean() / rpt.std() * np.sqrt(nt) if rpt.std() > 0 else 0
    else:
        pnl = nt = 0; wr = dd = sp = 0

    r = {
        "name": name, "description": desc,
        "total_pnl": round(pnl, 2), "total_trades": nt,
        "win_rate": round(wr, 1), "sharpe": round(sp, 2),
        "max_dd_pct": round(dd, 2),
        "skipped_bars": skipped_bars,
        "elapsed_s": round(time.time()-t1, 1),
    }
    return r


# ── Score functions ──────────────────────────────────────

def v6_conservative(df):
    return compute_btc_composite_score_v6(df)

def v6_aggressive(df):
    return compute_btc_composite_score_v6(df, velocity_w=5.0)

# Count "active layers" for v6
# Layer 1: cascade fires (score has ±8 from cascade)
# Layer 2: tick fires (score has ±8 from tick)
# So total can be 0, 8, -8, 16, -16
# "1 factor" = only cascade OR only tick
# "2 factors" = both cascade AND tick

all_results = []

# ══════════════════════════════════════════════════════════
# SECTION 1: BASELINE + VOL REGIME STATS
# ══════════════════════════════════════════════════════════
print("\n--- BASELINES ---")
r_base = run_with_vol_filter("v6_baseline", "v6 conservative no filter", v6_conservative)
all_results.append(r_base)
print(f"  v6 baseline:     {r_base['total_trades']:>5} tr | WR {r_base['win_rate']:>5.1f}% | "
      f"${r_base['total_pnl']:>9,.0f} | Sh {r_base['sharpe']:>6.2f} | DD {r_base['max_dd_pct']:>5.1f}%")

r_agg = run_with_vol_filter("v6_agg_baseline", "v6 aggressive no filter", v6_aggressive)
all_results.append(r_agg)
print(f"  v6 aggressive:   {r_agg['total_trades']:>5} tr | WR {r_agg['win_rate']:>5.1f}% | "
      f"${r_agg['total_pnl']:>9,.0f} | Sh {r_agg['sharpe']:>6.2f} | DD {r_agg['max_dd_pct']:>5.1f}%")


# ══════════════════════════════════════════════════════════
# SECTION 2: VOL FILTERS FOR V6
# ══════════════════════════════════════════════════════════
print("\n--- VOL FILTERS (v6 conservative) ---")
print(f"  {'Filter':<40} {'Tr':>5} {'WR%':>6} {'PnL':>10} {'Sh':>7} {'DD%':>6} {'Skip':>6} {'Delta':>10}")
print("  " + "-" * 100)

filters = [
    # Simple: skip all extreme
    ("skip_extreme",
     "Skip ALL signals during Extreme vol",
     lambda reg, score, sig: reg == "extreme"),

    # Skip extreme + low vol
    ("skip_extreme+low",
     "Skip Extreme + Low vol signals",
     lambda reg, score, sig: reg in ("extreme", "low")),

    # Skip extreme weak signals (|score| < 16 = only 1 layer)
    ("skip_extreme_1layer",
     "Skip Extreme when only 1 factor active (|score|<=8)",
     lambda reg, score, sig: reg == "extreme" and abs(score) <= 8),

    # Skip extreme strong signals too (|score| >= 16 = both layers)
    ("skip_extreme_2layer",
     "Skip Extreme even with 2 factors (all extreme)",
     lambda reg, score, sig: reg == "extreme"),

    # Skip extreme + high when weak
    ("skip_extreme+high_1layer",
     "Skip Extreme always + High when 1-layer",
     lambda reg, score, sig: reg == "extreme" or (reg == "high" and abs(score) <= 8)),

    # Only trade in Normal regime
    ("normal_only",
     "Only trade in Normal vol regime",
     lambda reg, score, sig: reg != "normal"),

    # Skip extreme LONG only (SHORT works better in extreme)
    ("skip_extreme_long",
     "Skip Extreme for LONG signals only",
     lambda reg, score, sig: reg == "extreme" and sig > 0),

    # Skip extreme SHORT only
    ("skip_extreme_short",
     "Skip Extreme for SHORT signals only",
     lambda reg, score, sig: reg == "extreme" and sig < 0),

    # Skip HIGH + EXTREME
    ("skip_high+extreme",
     "Skip High + Extreme",
     lambda reg, score, sig: reg in ("high", "extreme")),
]

for fname, fdesc, ffn in filters:
    r = run_with_vol_filter(f"v6_{fname}", fdesc, v6_conservative, ffn)
    all_results.append(r)
    delta = r["total_pnl"] - r_base["total_pnl"]
    print(f"  {fname:<40} {r['total_trades']:>5} {r['win_rate']:>5.1f}% "
          f"${r['total_pnl']:>9,.0f} {r['sharpe']:>6.2f} {r['max_dd_pct']:>5.1f}% "
          f"{r['skipped_bars']:>6} ${delta:>+9,.0f}")


# ══════════════════════════════════════════════════════════
# SECTION 3: VOL FILTERS FOR V6 AGGRESSIVE
# ══════════════════════════════════════════════════════════
print("\n--- VOL FILTERS (v6 aggressive) ---")
print(f"  {'Filter':<40} {'Tr':>5} {'WR%':>6} {'PnL':>10} {'Sh':>7} {'DD%':>6} {'Skip':>6} {'Delta':>10}")
print("  " + "-" * 100)

# Key filters for aggressive
agg_filters = [
    ("skip_extreme",
     lambda reg, score, sig: reg == "extreme"),
    ("skip_extreme_1layer",
     lambda reg, score, sig: reg == "extreme" and abs(score) <= 8),
    ("skip_extreme_long",
     lambda reg, score, sig: reg == "extreme" and sig > 0),
    ("skip_extreme+high_1layer",
     lambda reg, score, sig: reg == "extreme" or (reg == "high" and abs(score) <= 8)),
]

for fname, ffn in agg_filters:
    r = run_with_vol_filter(f"v6agg_{fname}", f"Aggressive + {fname}", v6_aggressive, ffn)
    all_results.append(r)
    delta = r["total_pnl"] - r_agg["total_pnl"]
    print(f"  {fname:<40} {r['total_trades']:>5} {r['win_rate']:>5.1f}% "
          f"${r['total_pnl']:>9,.0f} {r['sharpe']:>6.2f} {r['max_dd_pct']:>5.1f}% "
          f"{r['skipped_bars']:>6} ${delta:>+9,.0f}")


# ══════════════════════════════════════════════════════════
# SECTION 4: PER-REGIME BREAKDOWN
# ══════════════════════════════════════════════════════════
print("\n--- PER-REGIME BREAKDOWN (v6 conservative) ---")

for regime in ["low", "normal", "high", "extreme"]:
    # Only trade in this regime
    r = run_with_vol_filter(f"v6_only_{regime}", f"Only trade in {regime}",
                            v6_conservative,
                            lambda reg, score, sig, _r=regime: reg != _r)
    all_results.append(r)
    print(f"  {regime:<10}: {r['total_trades']:>5} tr | WR {r['win_rate']:>5.1f}% | "
          f"${r['total_pnl']:>9,.0f} | Sh {r['sharpe']:>6.2f} | DD {r['max_dd_pct']:>5.1f}%")

# ══════════════════════════════════════════════════════════
# SUMMARY
# ══════════════════════════════════════════════════════════
print("\n" + "=" * 80)
print("SUMMARY: V6 + VOL FILTER")
print("=" * 80)

sorted_all = sorted(all_results, key=lambda x: x["total_pnl"], reverse=True)
for i, r in enumerate(sorted_all[:15], 1):
    delta = r["total_pnl"] - r_base["total_pnl"]
    marker = " <-- BASE" if r["name"] == "v6_baseline" else ""
    print(f"  {i:>2}. {r['name']:<40} {r['total_trades']:>5} tr | WR {r['win_rate']:>5.1f}% | "
          f"${r['total_pnl']:>9,.0f} | Sh {r['sharpe']:>6.2f} | DD {r['max_dd_pct']:>5.1f}% | "
          f"${delta:>+8,.0f}{marker}")

# Best vol filter
best_filter = [r for r in sorted_all if "skip" in r["name"] or "only" in r["name"]]
if best_filter:
    bf = best_filter[0]
    print(f"\n  BEST FILTER: {bf['name']}")
    print(f"  PnL: ${bf['total_pnl']:+,.0f} (${bf['total_pnl']-r_base['total_pnl']:+,.0f} vs baseline)")
    print(f"  Sharpe: {bf['sharpe']:.2f} | DD: {bf['max_dd_pct']:.1f}%")

# Save
results_data = {
    "test": "v6_extreme_vol_filter",
    "timestamp": datetime.utcnow().isoformat(),
    "total_experiments": len(all_results),
    "results": sorted_all,
    "baseline": r_base,
}
RESULTS_FILE.write_text(json.dumps(results_data, indent=2, default=str, ensure_ascii=False))
print(f"\nResults saved to {RESULTS_FILE}")
print(f"Time: {(time.time()-t0)/60:.1f}min | Experiments: {len(all_results)}")
