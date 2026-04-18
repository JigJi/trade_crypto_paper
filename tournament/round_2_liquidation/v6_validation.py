"""
V6 VALIDATION: Period-Split + Walk-Forward + All-Coin Test
==========================================================
Goal: Verify v6 (liq-only) is robust across:
1. BULL / BEAR / FLAT regime splits
2. Walk-forward (rolling 3-month OOS windows)
3. All 46 coins (not just 6 tournament coins)
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

RESULTS_DIR = Path(__file__).parent
RESULTS_FILE = RESULTS_DIR / "results_v6_validation.json"

# ── Configs ──────────────────────────────────────────────

V5_CORE = {
    "w_liq_bull": 5.0, "w_liq_bear": 5.0,
    "w_fr_neg": 2.0, "w_fr_pos": 2.0,
    "w_whale_bull": 1.5, "w_whale_bear": 1.5,
    "w_etf_bull": 1.0, "w_etf_bear": 1.0,
    "w_oi_bull": 0.25, "w_oi_capit": 0.25, "w_oi_weak": 0.25, "w_oi_bear": 0.25,
}
V5_EXTRA = {"ob_combined": 2.0, "tick_liq": 3.0, "basis_contrarian": 1.5}

ZERO_OTHERS = {
    "w_fr_neg": 0.0, "w_fr_pos": 0.0,
    "w_whale_bull": 0.0, "w_whale_bear": 0.0,
    "w_etf_bull": 0.0, "w_etf_bear": 0.0,
    "w_oi_bull": 0.0, "w_oi_capit": 0.0, "w_oi_weak": 0.0, "w_oi_bear": 0.0,
}
ZERO_EXTRA = {"ob_combined": 0.0, "basis_contrarian": 0.0}

TOURNAMENT_COINS = ["BTC", "XRP", "ADA", "DOT", "SUI", "FIL"]

V3_COINS = ["BTC", "XRP", "ADA", "DOT", "SUI", "FIL", "RENDER", "NEAR", "AXS",
            "SOL", "ETH", "1000BONK", "ARB"]
V4_COINS = ["OGN", "LTC", "ZRO", "1000PEPE", "HYPE", "PENGU", "LINK"]
V5_COINS = ["FARTCOIN", "GALA", "AAVE", "AVAX", "UNI", "SEI", "DOGE", "ONDO",
            "1000SHIB", "BNB", "WIF", "CRV", "TAO"]
ALL_COINS = list(set(V3_COINS + V4_COINS + V5_COINS))

# Period definitions (BTC price regimes)
PERIODS = {
    "BULL_H1_2025":  ("2025-01-01", "2025-06-30"),   # BTC ~$40K -> $70K
    "BEAR_H2_2025":  ("2025-07-01", "2025-12-31"),   # BTC volatility + correction
    "Q1_2026":       ("2026-01-01", "2026-03-22"),    # Recent
}

# Walk-forward: 3-month rolling windows
WALK_FORWARD = {
    "WF_Q1_2025": ("2025-01-01", "2025-03-31"),
    "WF_Q2_2025": ("2025-04-01", "2025-06-30"),
    "WF_Q3_2025": ("2025-07-01", "2025-09-30"),
    "WF_Q4_2025": ("2025-10-01", "2025-12-31"),
    "WF_Q1_2026": ("2026-01-01", "2026-03-22"),
}


# ── Scoring functions (from R2c) ─────────────────────────

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


def velocity(df, w=5.0, lb=4):
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


def make_v5_score(btc_df_local):
    """v5 baseline score"""
    import backtest_15m_btc_led_alts as bt
    old = dict(bt.V3_EXTRA_WEIGHTS)
    bt.V3_EXTRA_WEIGHTS.update(V5_EXTRA)
    score = compute_btc_composite_score(btc_df_local, params=dict(V5_CORE))
    bt.V3_EXTRA_WEIGHTS.update(old)
    return score


def make_v6_score(btc_df_local):
    """v6 Conservative: liq-only cascade(1.1x) + tick(8,3)"""
    import backtest_15m_btc_led_alts as bt
    params = dict(V5_CORE)
    params["w_liq_bull"] = 0.0
    params["w_liq_bear"] = 0.0
    params.update(ZERO_OTHERS)
    extra = dict(V5_EXTRA)
    extra["tick_liq"] = 0.0
    extra.update(ZERO_EXTRA)
    old = dict(bt.V3_EXTRA_WEIGHTS)
    bt.V3_EXTRA_WEIGHTS.update(extra)
    score = compute_btc_composite_score(btc_df_local, params=params)
    bt.V3_EXTRA_WEIGHTS.update(old)
    score += cascade(btc_df_local, 8.0, 1.1)
    score += tick(btc_df_local, 8.0, 3)
    return score


def make_v6_aggressive_score(btc_df_local):
    """v6 Aggressive: liq-only cascade(1.1x) + tick(8,3) + velocity(5.0)"""
    score = make_v6_score(btc_df_local)
    score += velocity(btc_df_local, 5.0)
    return score


def make_v6_risk_score(btc_df_local):
    """v6 Risk-Optimal: same as v6 Conservative (max_hold handled in run)"""
    return make_v6_score(btc_df_local)


# ── Run function ─────────────────────────────────────────

def run(name, desc, score_fn, coins, btc_df, alt_data_cache, oos_start, oos_end,
        sl=15.0, tp=12.0, max_hold=96, cd=4, threshold_map=None):
    """Run backtest for given config/period/coins"""
    t1 = time.time()
    bs = score_fn(btc_df)
    bts = pd.Series(bs.values, index=btc_df["ts"].values)

    all_trades = []
    coin_results = {}

    for coin in coins:
        if coin not in alt_data_cache:
            continue

        thr = 3.0
        if threshold_map and coin in threshold_map:
            thr = threshold_map[coin]
        elif coin == "BTC":
            thr = 2.5
        elif coin in ("XRP", "ADA"):
            thr = 3.5

        sig, am = generate_btc_led_signal(bts, alt_data_cache[coin],
                                           threshold=thr, use_alt_pa_filter=False)
        mask = (am["ts"] >= pd.Timestamp(oos_start))
        if oos_end:
            mask &= (am["ts"] <= pd.Timestamp(oos_end))
        ao = am[mask].reset_index(drop=True)
        so = sig[mask].reset_index(drop=True)
        if len(ao) < 50:
            continue

        tr = run_backtest(ao, so, sl_atr_mult=sl, tp_atr_mult=tp,
                          trail_atr_mult=99, trail_activate_atr=99,
                          max_hold_bars=max_hold, cooldown_bars=cd)
        if len(tr) > 0:
            m = calc_metrics(tr, len(ao))
            coin_results[coin] = m
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
        "oos_period": f"{oos_start} to {oos_end}",
        "coins_tested": len([c for c in coins if c in alt_data_cache]),
        "total_pnl": round(pnl, 2), "total_trades": nt,
        "win_rate": round(wr, 1), "sharpe": round(sp, 2),
        "max_dd_pct": round(dd, 2),
        "long_trades": len(lo), "long_wr": round(100*(lo["pnl_net"]>0).sum()/max(len(lo),1), 1) if len(lo) else 0,
        "long_pnl": round(lo["pnl_net"].sum(), 2) if len(lo) else 0,
        "short_trades": len(sh), "short_wr": round(100*(sh["pnl_net"]>0).sum()/max(len(sh),1), 1) if len(sh) else 0,
        "short_pnl": round(sh["pnl_net"].sum(), 2) if len(sh) else 0,
        "coin_results": {k: {"pnl": round(v["net_pnl"],2), "wr": round(v["win_rate"],1),
                              "trades": v["total"], "sharpe": round(v["sharpe"],2)}
                         for k, v in coin_results.items()},
        "elapsed_s": round(time.time()-t1, 1),
    }
    return r


# ══════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════

print("=" * 80)
print("V6 VALIDATION: Period-Split + Walk-Forward + All-Coin")
print("=" * 80)

t0 = time.time()

# Load BTC data
btc_ohlcv = fetch_binance_15m("BTCUSDT", years=3)
db_data = load_btc_db_data()
btc_df = build_btc_features(btc_ohlcv, db_data)
print(f"BTC features: {len(btc_df)} bars ({time.time()-t0:.1f}s)")

# Load tournament coins
alt_data_6 = {}
for coin in TOURNAMENT_COINS:
    ohlcv = fetch_binance_15m(f"{coin}USDT", years=3)
    alt_data_6[coin] = build_alt_technicals(ohlcv)
print(f"Tournament coins loaded ({time.time()-t0:.1f}s)")

all_results = []

# ══════════════════════════════════════════════════════════
# SECTION 1: PERIOD SPLIT (6 tournament coins)
# ══════════════════════════════════════════════════════════
print("\n" + "=" * 80)
print("SECTION 1: PERIOD SPLIT ANALYSIS (6 coins)")
print("=" * 80)

configs = [
    ("v5", make_v5_score, 15.0, 12.0, 96),
    ("v6_conservative", make_v6_score, 25.0, 20.0, 96),
    ("v6_aggressive", make_v6_aggressive_score, 25.0, 20.0, 96),
    ("v6_risk_optimal", make_v6_risk_score, 25.0, 20.0, 48),
]

period_results = []

print(f"\n{'Config':<25} {'Period':<20} {'Tr':>5} {'WR%':>6} {'PnL':>10} {'Sh':>7} {'DD%':>6} {'L_WR':>5} {'S_WR':>5}")
print("-" * 95)

for cfg_name, score_fn, sl, tp, mh in configs:
    for period_name, (ps, pe) in PERIODS.items():
        r = run(f"{cfg_name}_{period_name}", f"{cfg_name} on {period_name}",
                score_fn, TOURNAMENT_COINS, btc_df, alt_data_6, ps, pe,
                sl=sl, tp=tp, max_hold=mh)
        r["config"] = cfg_name
        r["period"] = period_name
        period_results.append(r)
        print(f"  {cfg_name:<23} {period_name:<20} {r['total_trades']:>5} {r['win_rate']:>5.1f}% "
              f"${r['total_pnl']:>9,.0f} {r['sharpe']:>6.2f} {r['max_dd_pct']:>5.1f}% "
              f"{r['long_wr']:>4.0f}% {r['short_wr']:>4.0f}%")

    # Full period
    r = run(f"{cfg_name}_FULL", f"{cfg_name} full OOS",
            score_fn, TOURNAMENT_COINS, btc_df, alt_data_6,
            "2025-01-01", "2026-03-22", sl=sl, tp=tp, max_hold=mh)
    r["config"] = cfg_name
    r["period"] = "FULL"
    period_results.append(r)
    print(f"  {cfg_name:<23} {'FULL':<20} {r['total_trades']:>5} {r['win_rate']:>5.1f}% "
          f"${r['total_pnl']:>9,.0f} {r['sharpe']:>6.2f} {r['max_dd_pct']:>5.1f}% "
          f"{r['long_wr']:>4.0f}% {r['short_wr']:>4.0f}%")
    print()

all_results.extend(period_results)


# ══════════════════════════════════════════════════════════
# SECTION 2: WALK-FORWARD VALIDATION (6 coins)
# ══════════════════════════════════════════════════════════
print("\n" + "=" * 80)
print("SECTION 2: WALK-FORWARD (quarterly, 6 coins)")
print("=" * 80)

wf_results = []

for cfg_name, score_fn, sl, tp, mh in [
    ("v5", make_v5_score, 15.0, 12.0, 96),
    ("v6_conservative", make_v6_score, 25.0, 20.0, 96),
]:
    print(f"\n  {cfg_name}:")
    print(f"  {'Quarter':<15} {'Tr':>5} {'WR%':>6} {'PnL':>10} {'Sh':>7} {'$/trade':>8}")
    print("  " + "-" * 60)
    quarter_pnls = []
    for wf_name, (ws, we) in WALK_FORWARD.items():
        r = run(f"WF_{cfg_name}_{wf_name}", f"Walk-forward {wf_name}",
                score_fn, TOURNAMENT_COINS, btc_df, alt_data_6, ws, we,
                sl=sl, tp=tp, max_hold=mh)
        r["config"] = cfg_name
        r["wf_period"] = wf_name
        wf_results.append(r)
        avg_pnl = r["total_pnl"] / max(r["total_trades"], 1)
        quarter_pnls.append(r["total_pnl"])
        print(f"  {wf_name:<15} {r['total_trades']:>5} {r['win_rate']:>5.1f}% "
              f"${r['total_pnl']:>9,.0f} {r['sharpe']:>6.2f} ${avg_pnl:>7.2f}")

    # Consistency metrics
    positive_qs = sum(1 for p in quarter_pnls if p > 0)
    total_qs = len(quarter_pnls)
    min_q = min(quarter_pnls)
    max_q = max(quarter_pnls)
    print(f"  {'CONSISTENCY':<15} {positive_qs}/{total_qs} profitable quarters | "
          f"min=${min_q:,.0f} max=${max_q:,.0f} ratio={max_q/max(abs(min_q),1):.1f}x")

all_results.extend(wf_results)


# ══════════════════════════════════════════════════════════
# SECTION 3: ALL-COIN TEST (v6 conservative)
# ══════════════════════════════════════════════════════════
print("\n" + "=" * 80)
print("SECTION 3: ALL-COIN TEST")
print("=" * 80)

# Load all coin data
alt_data_all = dict(alt_data_6)  # Start with what we have
new_coins = [c for c in ALL_COINS if c not in alt_data_all]
print(f"Loading {len(new_coins)} additional coins...")
failed_coins = []
for coin in new_coins:
    try:
        ohlcv = fetch_binance_15m(f"{coin}USDT", years=3)
        if len(ohlcv) > 500:
            alt_data_all[coin] = build_alt_technicals(ohlcv)
        else:
            failed_coins.append(coin)
    except Exception as e:
        failed_coins.append(coin)
        print(f"  SKIP {coin}: {e}")

print(f"Loaded {len(alt_data_all)} coins total ({len(failed_coins)} failed: {failed_coins})")

# Run v5 and v6 on ALL coins
all_coin_results = []

for cfg_name, score_fn, sl, tp, mh in [
    ("v5", make_v5_score, 15.0, 12.0, 96),
    ("v6_conservative", make_v6_score, 25.0, 20.0, 96),
    ("v6_aggressive", make_v6_aggressive_score, 25.0, 20.0, 96),
]:
    r = run(f"allcoin_{cfg_name}", f"{cfg_name} on all {len(alt_data_all)} coins",
            score_fn, list(alt_data_all.keys()), btc_df, alt_data_all,
            "2025-01-01", "2026-03-22", sl=sl, tp=tp, max_hold=mh)
    r["config"] = cfg_name
    all_coin_results.append(r)

    # Count profitable coins
    profitable = sum(1 for v in r["coin_results"].values() if v["pnl"] > 0)
    total = len(r["coin_results"])
    print(f"  {cfg_name:<25} {r['total_trades']:>5} tr | WR {r['win_rate']:>5.1f}% | "
          f"${r['total_pnl']:>10,.0f} | Sh {r['sharpe']:>6.2f} | DD {r['max_dd_pct']:>5.1f}% | "
          f"{profitable}/{total} coins profitable")

# Per-coin comparison: v5 vs v6
if len(all_coin_results) >= 2:
    v5_coins = all_coin_results[0]["coin_results"]
    v6_coins = all_coin_results[1]["coin_results"]

    print(f"\n  {'Coin':<12} {'v5 PnL':>10} {'v6 PnL':>10} {'Delta':>10} {'v5 WR':>6} {'v6 WR':>6} {'v5 Tr':>5} {'v6 Tr':>5}")
    print("  " + "-" * 70)

    all_coin_names = sorted(set(list(v5_coins.keys()) + list(v6_coins.keys())))
    v6_better = 0
    v5_better = 0
    for coin in all_coin_names:
        v5c = v5_coins.get(coin, {"pnl": 0, "wr": 0, "trades": 0})
        v6c = v6_coins.get(coin, {"pnl": 0, "wr": 0, "trades": 0})
        delta = v6c["pnl"] - v5c["pnl"]
        marker = " +" if delta > 0 else " -" if delta < 0 else "  "
        if delta > 0: v6_better += 1
        elif delta < 0: v5_better += 1
        print(f"  {coin:<12} ${v5c['pnl']:>9,.0f} ${v6c['pnl']:>9,.0f} ${delta:>+9,.0f}{marker} "
              f"{v5c['wr']:>5.1f}% {v6c['wr']:>5.1f}% {v5c['trades']:>5} {v6c['trades']:>5}")

    print(f"\n  v6 better: {v6_better} coins | v5 better: {v5_better} coins")

all_results.extend(all_coin_results)


# ══════════════════════════════════════════════════════════
# SECTION 4: DIRECTION ANALYSIS (v6 vs v5)
# ══════════════════════════════════════════════════════════
print("\n" + "=" * 80)
print("SECTION 4: DIRECTION ANALYSIS")
print("=" * 80)

for cfg_name, score_fn, sl, tp, mh in [
    ("v5", make_v5_score, 15.0, 12.0, 96),
    ("v6_conservative", make_v6_score, 25.0, 20.0, 96),
]:
    r = run(f"dir_{cfg_name}", f"Direction analysis {cfg_name}",
            score_fn, TOURNAMENT_COINS, btc_df, alt_data_6,
            "2025-01-01", "2026-03-22", sl=sl, tp=tp, max_hold=mh)
    l_avg = r["long_pnl"] / max(r["long_trades"], 1)
    s_avg = r["short_pnl"] / max(r["short_trades"], 1)
    print(f"  {cfg_name}:")
    print(f"    LONG:  {r['long_trades']} trades, WR {r['long_wr']:.1f}%, PnL ${r['long_pnl']:+,.0f}, avg ${l_avg:+.2f}/trade")
    print(f"    SHORT: {r['short_trades']} trades, WR {r['short_wr']:.1f}%, PnL ${r['short_pnl']:+,.0f}, avg ${s_avg:+.2f}/trade")
    print(f"    TOTAL: {r['total_trades']} trades, WR {r['win_rate']:.1f}%, PnL ${r['total_pnl']:+,.0f}")
    print()


# ══════════════════════════════════════════════════════════
# GRAND SUMMARY
# ══════════════════════════════════════════════════════════
print("\n" + "=" * 80)
print("GRAND SUMMARY: V6 VALIDATION")
print("=" * 80)

# Period split summary
print("\nPERIOD SPLIT (6 coins):")
print(f"  {'Config':<25} {'BULL_H1':>10} {'BEAR_H2':>10} {'Q1_2026':>10} {'FULL':>10} {'All+?':>5}")
print("  " + "-" * 70)
for cfg_name, _, _, _, _ in configs:
    pnls = {}
    for r in period_results:
        if r["config"] == cfg_name:
            pnls[r["period"]] = r["total_pnl"]
    all_positive = all(v > 0 for k, v in pnls.items() if k != "FULL")
    print(f"  {cfg_name:<25} ${pnls.get('BULL_H1_2025',0):>9,.0f} ${pnls.get('BEAR_H2_2025',0):>9,.0f} "
          f"${pnls.get('Q1_2026',0):>9,.0f} ${pnls.get('FULL',0):>9,.0f}  {'YES' if all_positive else 'NO'}")

# Walk-forward summary
print("\nWALK-FORWARD (6 coins):")
for cfg_name in ["v5", "v6_conservative"]:
    qs = [r for r in wf_results if r["config"] == cfg_name]
    pnls = [r["total_pnl"] for r in qs]
    pos = sum(1 for p in pnls if p > 0)
    print(f"  {cfg_name}: {pos}/{len(pnls)} profitable quarters, "
          f"total=${sum(pnls):,.0f}, min=${min(pnls):,.0f}, max=${max(pnls):,.0f}")

# All-coin summary
print("\nALL-COIN TEST:")
for r in all_coin_results:
    profitable = sum(1 for v in r["coin_results"].values() if v["pnl"] > 0)
    total = len(r["coin_results"])
    print(f"  {r['config']}: ${r['total_pnl']:>10,.0f} | {profitable}/{total} coins | "
          f"WR {r['win_rate']:.1f}% | Sh {r['sharpe']:.2f}")

# Save all results
results_data = {
    "validation": "v6_period_split_walkforward_allcoin",
    "timestamp": datetime.utcnow().isoformat(),
    "period_split": period_results,
    "walk_forward": wf_results,
    "all_coin": all_coin_results,
    "all_results": all_results,
    "total_experiments": len(all_results),
}
RESULTS_FILE.write_text(json.dumps(results_data, indent=2, default=str, ensure_ascii=False))
print(f"\nResults saved to {RESULTS_FILE}")
print(f"Total time: {(time.time()-t0)/60:.1f}min | Experiments: {len(all_results)}")
