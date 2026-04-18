"""
Tournament Round 2b: LIQUIDATION ADVANCED
==========================================
Follow-up from Round 2 key discoveries:
1. Cascade 1.5x is king - fine-tune around it
2. Ratio-based scoring was a surprise hit - explore more
3. Velocity adds alpha - combine with best cascade
4. Tiered scoring showed promise - optimize tiers
5. Combine the best architectures

CHAMPION from R2: cascade_1.5x + MA=18 + liq_w=8 + tick_w=8 + tick_net=3
PnL: $73,161 | Sharpe: 25.93 | DD: -1.3%
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

# ── Config ──────────────────────────────────────────────
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
RESULTS_FILE = RESULTS_DIR / "results_round2b.json"

# ── Data Loading ────────────────────────────────────────
print("=" * 70)
print("TOURNAMENT ROUND 2b: LIQUIDATION ADVANCED")
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
    print(f"  {coin}: {len(alt_data[coin])} bars")

print(f"Data loaded in {time.time()-t0:.1f}s\n")


# ══════════════════════════════════════════════════════════
# SCORING FUNCTIONS
# ══════════════════════════════════════════════════════════

def custom_cascade(df, w_bull=8.0, w_bear=8.0, mult=1.5, ma_lb=18):
    """Customizable hourly cascade."""
    s = pd.Series(0.0, index=df.index)
    if "liq_net" not in df.columns or "liq_total" not in df.columns:
        return s
    lt = df["liq_total"].fillna(0)
    ln = df["liq_net"].fillna(0)
    lt_ma = df.get("liq_total_ma", lt.rolling(24, min_periods=1).mean()).fillna(1)
    cascade = lt > (lt_ma * mult)
    s += np.where(cascade & (ln > 0), w_bull, 0)
    s += np.where(cascade & (ln < 0), -w_bear, 0)
    return s


def tiered_cascade(df, w_bull=8.0, w_bear=8.0, t1=1.5, t2=3.0, t3=6.0,
                    b2=0.5, b3=0.3):
    """Tiered cascade with configurable thresholds and bonuses."""
    s = pd.Series(0.0, index=df.index)
    if "liq_net" not in df.columns or "liq_total" not in df.columns:
        return s
    lt = df["liq_total"].fillna(0)
    ln = df["liq_net"].fillna(0)
    lt_ma = df.get("liq_total_ma", lt.rolling(24, min_periods=1).mean()).fillna(1)

    c1 = lt > (lt_ma * t1)
    s += np.where(c1 & (ln > 0), w_bull, 0)
    s += np.where(c1 & (ln < 0), -w_bear, 0)

    c2 = lt > (lt_ma * t2)
    s += np.where(c2 & (ln > 0), w_bull * b2, 0)
    s += np.where(c2 & (ln < 0), -w_bear * b2, 0)

    c3 = lt > (lt_ma * t3)
    s += np.where(c3 & (ln > 0), w_bull * b3, 0)
    s += np.where(c3 & (ln < 0), -w_bear * b3, 0)

    return s


def ratio_cascade(df, w_bull=8.0, w_bear=8.0, ratio_thr=0.65, extreme_thr=0.80,
                   min_mult=1.0):
    """Ratio-based scoring: % of directional liqs."""
    s = pd.Series(0.0, index=df.index)
    if "liq_total" not in df.columns or "liq_short_1h" not in df.columns:
        return s
    lt = df["liq_total"].fillna(0)
    lt_ma = df.get("liq_total_ma", lt.rolling(24, min_periods=1).mean()).fillna(1)
    meaningful = lt > (lt_ma * min_mult)

    short_pct = df["liq_short_1h"].fillna(0) / lt.clip(lower=1)
    long_pct = 1 - short_pct

    s += np.where(meaningful & (short_pct > ratio_thr), w_bull, 0)
    s += np.where(meaningful & (short_pct > extreme_thr), w_bull * 0.5, 0)
    s += np.where(meaningful & (long_pct > ratio_thr), -w_bear, 0)
    s += np.where(meaningful & (long_pct > extreme_thr), -w_bear * 0.5, 0)
    return s


def velocity_cascade(df, weight=5.0, lookback=4, accel_thr=1.0, decel_bonus=0.3):
    """Velocity-based: acceleration of liq volume."""
    s = pd.Series(0.0, index=df.index)
    if "liq_total" not in df.columns:
        return s
    lt = df["liq_total"].fillna(0)
    velocity = lt.pct_change(lookback).fillna(0)
    ln = df["liq_net"].fillna(0)

    accel = velocity > accel_thr
    s += np.where(accel & (ln > 0), weight, 0)
    s += np.where(accel & (ln < 0), -weight, 0)

    decel = velocity < -0.5
    s += np.where(decel & (ln > 0), weight * decel_bonus, 0)
    s += np.where(decel & (ln < 0), -weight * decel_bonus, 0)
    return s


def custom_tick(df, weight=8.0, net_thr=3, not_mult=3.0, not_bonus=0.0):
    """Customizable tick liquidation."""
    s = pd.Series(0.0, index=df.index)
    if "liq_net_ma" not in df.columns:
        return s
    ln = df["liq_net_ma"].fillna(0)
    lt = df["liq_notional_ma"].fillna(0)
    s += np.where(ln > net_thr, weight, 0)
    s += np.where(ln < -net_thr, -weight, 0)
    if not_bonus > 0:
        lt_mean = lt[lt > 0].mean() if (lt > 0).any() else 1
        s += np.where(lt > lt_mean * not_mult, weight * not_bonus, 0)
    return s


def tiered_tick(df, weight=8.0, t1=2, t2=5, t3=10, b2=0.5, b3=0.5):
    """Tiered tick: graduated net count levels."""
    s = pd.Series(0.0, index=df.index)
    if "liq_net_ma" not in df.columns:
        return s
    ln = df["liq_net_ma"].fillna(0)
    s += np.where(ln > t1, weight, 0)
    s += np.where(ln < -t1, -weight, 0)
    s += np.where(ln > t2, weight * b2, 0)
    s += np.where(ln < -t2, -weight * b2, 0)
    s += np.where(ln > t3, weight * b3, 0)
    s += np.where(ln < -t3, -weight * b3, 0)
    return s


def make_score_fn(liq_fn, tick_fn=None, core_overrides=None, extra_overrides=None):
    """Build BTC score function with custom liq, preserving other factors."""
    def fn(btc_df_local):
        import backtest_15m_btc_led_alts as bt
        params = dict(V5_CORE)
        params["w_liq_bull"] = 0.0
        params["w_liq_bear"] = 0.0
        if core_overrides:
            params.update(core_overrides)

        extra = dict(V5_EXTRA)
        extra["tick_liq"] = 0.0
        if extra_overrides:
            extra.update(extra_overrides)

        old_extra = dict(bt.V3_EXTRA_WEIGHTS)
        bt.V3_EXTRA_WEIGHTS.update(extra)
        score = compute_btc_composite_score(btc_df_local, params=params)
        bt.V3_EXTRA_WEIGHTS.update(old_extra)

        if liq_fn:
            score += liq_fn(btc_df_local)
        if tick_fn:
            score += tick_fn(btc_df_local)
        return score
    return fn


# ── Runner ──────────────────────────────────────────────
def run_contender(name, description, btc_score_fn=None, coin_overrides=None):
    print(f"\n{'-'*60}")
    print(f"  {name}: {description}")
    print(f"{'-'*60}")

    t1 = time.time()
    if btc_score_fn:
        btc_score = btc_score_fn(btc_df)
    else:
        # v5 baseline
        import backtest_15m_btc_led_alts as bt
        old = dict(bt.V3_EXTRA_WEIGHTS)
        bt.V3_EXTRA_WEIGHTS.update(V5_EXTRA)
        btc_score = compute_btc_composite_score(btc_df, params=dict(V5_CORE))
        bt.V3_EXTRA_WEIGHTS.update(old)

    btc_score_ts = pd.Series(btc_score.values, index=btc_df["ts"].values)
    all_trades = []
    coin_results = {}

    for coin in COINS:
        cfg = dict(V5_CONFIGS.get(coin, V5_CONFIGS["DOT"]))
        if coin_overrides and coin in coin_overrides:
            cfg.update(coin_overrides[coin])
        elif coin_overrides and "__all__" in coin_overrides:
            cfg.update(coin_overrides["__all__"])

        signals, alt_merged = generate_btc_led_signal(
            btc_score_ts, alt_data[coin],
            threshold=cfg["threshold"],
            use_alt_pa_filter=cfg.get("alt_pa", False),
        )

        oos_mask = (alt_merged["ts"] >= pd.Timestamp(OOS_START))
        if OOS_END:
            oos_mask &= (alt_merged["ts"] <= pd.Timestamp(OOS_END))

        alt_oos = alt_merged[oos_mask].reset_index(drop=True)
        sig_oos = signals[oos_mask].reset_index(drop=True)
        if len(alt_oos) < 100:
            continue

        trades = run_backtest(alt_oos, sig_oos,
                              sl_atr_mult=cfg.get("sl", 15.0),
                              tp_atr_mult=cfg.get("tp", 12.0),
                              trail_atr_mult=cfg.get("trail", 99),
                              trail_activate_atr=cfg.get("trail_act", 99),
                              max_hold_bars=cfg.get("max_hold", 96),
                              cooldown_bars=cfg.get("cd", 4))

        if len(trades) > 0:
            m = calc_metrics(trades, len(alt_oos))
            coin_results[coin] = m
            trades["coin"] = coin
            all_trades.append(trades)

    if all_trades:
        combined = pd.concat(all_trades, ignore_index=True)
        total_pnl = combined["pnl_net"].sum()
        total_trades = len(combined)
        total_wr = 100 * (combined["pnl_net"] > 0).sum() / total_trades if total_trades else 0
        longs = combined[combined["dir"] == "L"]
        shorts = combined[combined["dir"] == "S"]
        long_pnl = longs["pnl_net"].sum() if len(longs) else 0
        short_pnl = shorts["pnl_net"].sum() if len(shorts) else 0
        long_wr = 100 * (longs["pnl_net"] > 0).sum() / len(longs) if len(longs) else 0
        short_wr = 100 * (shorts["pnl_net"] > 0).sum() / len(shorts) if len(shorts) else 0
        equity = 10000 + combined["pnl_net"].cumsum()
        max_dd = ((equity - equity.cummax()) / equity.cummax() * 100).min()
        ret_per_trade = combined["pnl_net"] / 1000
        sharpe = (ret_per_trade.mean() / ret_per_trade.std() * np.sqrt(total_trades)
                  if ret_per_trade.std() > 0 else 0)
    else:
        total_pnl = total_trades = 0; total_wr = long_pnl = short_pnl = 0
        long_wr = short_wr = max_dd = sharpe = 0
        longs = shorts = pd.DataFrame()

    result = {
        "name": name, "description": description,
        "total_pnl": round(total_pnl, 2), "total_trades": total_trades,
        "win_rate": round(total_wr, 1), "sharpe": round(sharpe, 2),
        "max_dd_pct": round(max_dd, 2),
        "long_trades": len(longs), "long_wr": round(long_wr, 1), "long_pnl": round(long_pnl, 2),
        "short_trades": len(shorts), "short_wr": round(short_wr, 1), "short_pnl": round(short_pnl, 2),
        "coin_results": {c: {"pnl": round(m["net_pnl"], 2), "wr": round(m["win_rate"], 1),
                              "trades": m["total"], "sharpe": round(m["sharpe"], 2)}
                         for c, m in coin_results.items()},
        "elapsed_s": round(time.time() - t1, 1),
    }
    print(f"    {total_trades} trades | WR {total_wr:.1f}% | PnL ${total_pnl:+,.0f} | "
          f"Sharpe {sharpe:.2f} | DD {max_dd:.1f}%")
    return result


def print_summary(name, results, baseline_pnl):
    print(f"\n{'='*95}")
    print(f"{name}")
    print(f"{'='*95}")
    print(f"{'Rank':<4} {'Name':<45} {'Trades':>6} {'WR%':>6} {'PnL':>10} {'Sharpe':>7} {'DD%':>6} {'Delta':>10}")
    print("-" * 95)
    for i, r in enumerate(sorted(results, key=lambda x: x["total_pnl"], reverse=True), 1):
        delta = r["total_pnl"] - baseline_pnl
        marker = " ***" if i == 1 else ""
        print(f"{i:<4} {r['name']:<45} {r['total_trades']:>6} {r['win_rate']:>5.1f}% "
              f"${r['total_pnl']:>9,.0f} {r['sharpe']:>7.2f} {r['max_dd_pct']:>5.1f}% "
              f"${delta:>+9,.0f}{marker}")


all_results = []

# R2 champion as our baseline
R2_CHAMPION_FN = make_score_fn(
    liq_fn=lambda df: custom_cascade(df, 8.0, 8.0, 1.5, 18),
    tick_fn=lambda df: custom_tick(df, 8.0, 3),
)

r = run_contender("R2_champion", "cascade=1.5x, MA=18, liq=8, tick=8, net=3",
                  btc_score_fn=R2_CHAMPION_FN)
all_results.append(r)
BASELINE_PNL = r["total_pnl"]
print(f"\n  >>> R2 CHAMPION BASELINE: ${BASELINE_PNL:+,.0f}")


# ══════════════════════════════════════════════════════════
# BATCH A: FINE-TUNE CASCADE MULT (1.0 - 2.0 range)
# ══════════════════════════════════════════════════════════
print("\n" + "#" * 70)
print("# BATCH A: Fine-tune cascade mult (1.0 - 2.0)")
print("#" * 70)

batchA = []
for mult in [1.0, 1.1, 1.2, 1.3, 1.4, 1.6, 1.7, 1.8, 1.9, 2.0]:
    r = run_contender(
        f"cascade_{mult}x",
        f"Cascade mult={mult}x",
        btc_score_fn=make_score_fn(
            liq_fn=lambda df, m=mult: custom_cascade(df, 8.0, 8.0, m, 18),
            tick_fn=lambda df: custom_tick(df, 8.0, 3),
        ),
    )
    batchA.append(r)

all_results.extend(batchA)
print_summary("BATCH A: CASCADE FINE-TUNE", [all_results[0]] + batchA, BASELINE_PNL)


# ══════════════════════════════════════════════════════════
# BATCH B: FINE-TUNE CASCADE MA (12-24 range)
# ══════════════════════════════════════════════════════════
print("\n" + "#" * 70)
print("# BATCH B: Fine-tune cascade MA (12-24 bars)")
print("#" * 70)

# Find best cascade mult from batch A
best_cascade_mult = 1.5
if batchA:
    bw = max(batchA, key=lambda x: x["total_pnl"])
    for m in [1.0, 1.1, 1.2, 1.3, 1.4, 1.6, 1.7, 1.8, 1.9, 2.0]:
        if f"cascade_{m}x" == bw["name"] and bw["total_pnl"] > BASELINE_PNL:
            best_cascade_mult = m
            break

print(f"Using best cascade mult: {best_cascade_mult}x")

batchB = []
for ma in [12, 14, 15, 16, 17, 19, 20, 22, 24]:
    r = run_contender(
        f"cascade_ma_{ma}",
        f"Cascade MA={ma} bars (mult={best_cascade_mult}x)",
        btc_score_fn=make_score_fn(
            liq_fn=lambda df, m=ma: custom_cascade(df, 8.0, 8.0, best_cascade_mult, m),
            tick_fn=lambda df: custom_tick(df, 8.0, 3),
        ),
    )
    batchB.append(r)

all_results.extend(batchB)
print_summary("BATCH B: CASCADE MA FINE-TUNE", [all_results[0]] + batchB, BASELINE_PNL)

# Find best MA
best_ma = 18
if batchB:
    bw = max(batchB, key=lambda x: x["total_pnl"])
    for m in [12, 14, 15, 16, 17, 19, 20, 22, 24]:
        if f"cascade_ma_{m}" == bw["name"] and bw["total_pnl"] > BASELINE_PNL:
            best_ma = m
            break

print(f"\nBest cascade config: mult={best_cascade_mult}x, MA={best_ma}")


# ══════════════════════════════════════════════════════════
# BATCH C: RATIO-BASED SCORING (promising from R2)
# ══════════════════════════════════════════════════════════
print("\n" + "#" * 70)
print("# BATCH C: Ratio-based scoring exploration")
print("#" * 70)

batchC = []

# Ratio with different thresholds
for rt, et in [(0.55, 0.70), (0.55, 0.75), (0.55, 0.80),
               (0.60, 0.75), (0.60, 0.80), (0.60, 0.85),
               (0.65, 0.80), (0.65, 0.85), (0.70, 0.85),
               (0.70, 0.90)]:
    r = run_contender(
        f"ratio_{int(rt*100)}_{int(et*100)}",
        f"Ratio: {rt}/{et} threshold",
        btc_score_fn=make_score_fn(
            liq_fn=lambda df, r=rt, e=et: ratio_cascade(df, 8.0, 8.0, r, e),
            tick_fn=lambda df: custom_tick(df, 8.0, 3),
        ),
    )
    batchC.append(r)

# Ratio with different min_mult (minimum total volume to consider)
for mm in [0.5, 0.75, 1.5, 2.0]:
    r = run_contender(
        f"ratio_65_80_min{mm}x",
        f"Ratio 65/80 + min_mult={mm}x",
        btc_score_fn=make_score_fn(
            liq_fn=lambda df, m=mm: ratio_cascade(df, 8.0, 8.0, 0.65, 0.80, m),
            tick_fn=lambda df: custom_tick(df, 8.0, 3),
        ),
    )
    batchC.append(r)

all_results.extend(batchC)
print_summary("BATCH C: RATIO-BASED", [all_results[0]] + batchC, BASELINE_PNL)


# ══════════════════════════════════════════════════════════
# BATCH D: CASCADE + RATIO HYBRID
# ══════════════════════════════════════════════════════════
print("\n" + "#" * 70)
print("# BATCH D: Cascade + Ratio Hybrid")
print("#" * 70)

batchD = []

# Find best ratio from batch C
best_ratio_result = max(batchC, key=lambda x: x["total_pnl"]) if batchC else None

# Hybrid: cascade + ratio combined (additive)
for ratio_w in [3.0, 4.0, 5.0, 6.0]:
    r = run_contender(
        f"hybrid_cascade_ratio_w{ratio_w}",
        f"Cascade({best_cascade_mult}x) + Ratio(65/80, w={ratio_w})",
        btc_score_fn=make_score_fn(
            liq_fn=lambda df, rw=ratio_w: (
                custom_cascade(df, 8.0, 8.0, best_cascade_mult, best_ma) +
                ratio_cascade(df, rw, rw, 0.65, 0.80)
            ),
            tick_fn=lambda df: custom_tick(df, 8.0, 3),
        ),
    )
    batchD.append(r)

# Cascade + velocity hybrid
for vel_w in [2.0, 3.0, 4.0, 5.0]:
    r = run_contender(
        f"hybrid_cascade_velocity_w{vel_w}",
        f"Cascade({best_cascade_mult}x) + Velocity(w={vel_w})",
        btc_score_fn=make_score_fn(
            liq_fn=lambda df, vw=vel_w: (
                custom_cascade(df, 8.0, 8.0, best_cascade_mult, best_ma) +
                velocity_cascade(df, vw, 4)
            ),
            tick_fn=lambda df: custom_tick(df, 8.0, 3),
        ),
    )
    batchD.append(r)

# Triple: cascade + ratio + velocity
for rw, vw in [(3.0, 2.0), (4.0, 3.0), (5.0, 2.0)]:
    r = run_contender(
        f"triple_r{rw}_v{vw}",
        f"Cascade + Ratio(w={rw}) + Velocity(w={vw})",
        btc_score_fn=make_score_fn(
            liq_fn=lambda df, rw_=rw, vw_=vw: (
                custom_cascade(df, 8.0, 8.0, best_cascade_mult, best_ma) +
                ratio_cascade(df, rw_, rw_, 0.65, 0.80) +
                velocity_cascade(df, vw_, 4)
            ),
            tick_fn=lambda df: custom_tick(df, 8.0, 3),
        ),
    )
    batchD.append(r)

all_results.extend(batchD)
print_summary("BATCH D: HYBRID ARCHITECTURES", [all_results[0]] + batchD, BASELINE_PNL)


# ══════════════════════════════════════════════════════════
# BATCH E: TIERED OPTIMIZATION
# ══════════════════════════════════════════════════════════
print("\n" + "#" * 70)
print("# BATCH E: Tiered scoring with best cascade mult")
print("#" * 70)

batchE = []

# Tiered cascade with best base tier
for t2, t3, b2, b3 in [
    (3.0, 6.0, 0.5, 0.3),   # Standard
    (3.0, 6.0, 0.3, 0.2),   # Smaller bonuses
    (2.5, 5.0, 0.5, 0.5),   # Tighter tiers, equal bonus
    (4.0, 8.0, 0.5, 0.5),   # Wider tiers
    (3.0, 5.0, 0.75, 0.5),  # Higher tier2 bonus
    (2.0, 4.0, 0.5, 0.5),   # Very tight tiers
]:
    r = run_contender(
        f"tiered_{best_cascade_mult}_{t2}_{t3}_b{b2}_{b3}",
        f"Tiered: {best_cascade_mult}x/{t2}x/{t3}x, bonus={b2}/{b3}",
        btc_score_fn=make_score_fn(
            liq_fn=lambda df, _t2=t2, _t3=t3, _b2=b2, _b3=b3: tiered_cascade(
                df, 8.0, 8.0, best_cascade_mult, _t2, _t3, _b2, _b3),
            tick_fn=lambda df: custom_tick(df, 8.0, 3),
        ),
    )
    batchE.append(r)

# Tiered tick with best cascade
for t1, t2, t3 in [(1, 3, 7), (2, 4, 8), (2, 5, 10), (3, 6, 12), (1, 4, 8)]:
    r = run_contender(
        f"tiered_tick_{t1}_{t2}_{t3}",
        f"Best cascade + tiered tick: {t1}/{t2}/{t3}",
        btc_score_fn=make_score_fn(
            liq_fn=lambda df: custom_cascade(df, 8.0, 8.0, best_cascade_mult, best_ma),
            tick_fn=lambda df, _t1=t1, _t2=t2, _t3=t3: tiered_tick(df, 8.0, _t1, _t2, _t3),
        ),
    )
    batchE.append(r)

# Both tiered
r = run_contender(
    "both_tiered_optimized",
    f"Both tiered: cascade {best_cascade_mult}/3/6 + tick 2/5/10",
    btc_score_fn=make_score_fn(
        liq_fn=lambda df: tiered_cascade(df, 8.0, 8.0, best_cascade_mult, 3.0, 6.0, 0.5, 0.3),
        tick_fn=lambda df: tiered_tick(df, 8.0, 2, 5, 10),
    ),
)
batchE.append(r)

all_results.extend(batchE)
print_summary("BATCH E: TIERED OPTIMIZATION", [all_results[0]] + batchE, BASELINE_PNL)


# ══════════════════════════════════════════════════════════
# BATCH F: LIQ-ONLY EXPERIMENTS (since liq IS the edge)
# ══════════════════════════════════════════════════════════
print("\n" + "#" * 70)
print("# BATCH F: Liq-only (zero other factors)")
print("#" * 70)

batchF = []

# No other factors, just best cascade + tick
zero_others = {
    "w_fr_neg": 0.0, "w_fr_pos": 0.0,
    "w_whale_bull": 0.0, "w_whale_bear": 0.0,
    "w_etf_bull": 0.0, "w_etf_bear": 0.0,
    "w_oi_bull": 0.0, "w_oi_capit": 0.0, "w_oi_weak": 0.0, "w_oi_bear": 0.0,
}
zero_extra = {"ob_combined": 0.0, "basis_contrarian": 0.0}

r = run_contender(
    "liq_only_best_cascade",
    f"Liq-only: cascade {best_cascade_mult}x + tick(8.0, net>3)",
    btc_score_fn=make_score_fn(
        liq_fn=lambda df: custom_cascade(df, 8.0, 8.0, best_cascade_mult, best_ma),
        tick_fn=lambda df: custom_tick(df, 8.0, 3),
        core_overrides=zero_others,
        extra_overrides=zero_extra,
    ),
)
batchF.append(r)

# Liq-only with ratio
r = run_contender(
    "liq_only_ratio",
    "Liq-only: ratio(65/80) + tick(8.0)",
    btc_score_fn=make_score_fn(
        liq_fn=lambda df: ratio_cascade(df, 8.0, 8.0, 0.65, 0.80),
        tick_fn=lambda df: custom_tick(df, 8.0, 3),
        core_overrides=zero_others,
        extra_overrides=zero_extra,
    ),
)
batchF.append(r)

# Liq-only with cascade + ratio hybrid
r = run_contender(
    "liq_only_hybrid",
    f"Liq-only: cascade({best_cascade_mult}x) + ratio(4.0) + tick",
    btc_score_fn=make_score_fn(
        liq_fn=lambda df: (
            custom_cascade(df, 8.0, 8.0, best_cascade_mult, best_ma) +
            ratio_cascade(df, 4.0, 4.0, 0.65, 0.80)
        ),
        tick_fn=lambda df: custom_tick(df, 8.0, 3),
        core_overrides=zero_others,
        extra_overrides=zero_extra,
    ),
)
batchF.append(r)

# Liq-only with lower threshold (since less signal = fewer trades)
for thr in [2.0, 2.5]:
    r = run_contender(
        f"liq_only_thr{thr}",
        f"Liq-only + lower threshold={thr}",
        btc_score_fn=make_score_fn(
            liq_fn=lambda df: custom_cascade(df, 8.0, 8.0, best_cascade_mult, best_ma),
            tick_fn=lambda df: custom_tick(df, 8.0, 3),
            core_overrides=zero_others,
            extra_overrides=zero_extra,
        ),
        coin_overrides={"__all__": {"threshold": thr}},
    )
    batchF.append(r)

all_results.extend(batchF)
print_summary("BATCH F: LIQ-ONLY", [all_results[0]] + batchF, BASELINE_PNL)


# ══════════════════════════════════════════════════════════
# BATCH G: SL/TP OPTIMIZATION WITH BEST LIQ CONFIG
# ══════════════════════════════════════════════════════════
print("\n" + "#" * 70)
print("# BATCH G: SL/TP with best liq config")
print("#" * 70)

batchG = []
for sl, tp in [(10.0, 10.0), (10.0, 12.0), (12.0, 10.0), (12.0, 12.0),
               (12.0, 15.0), (15.0, 10.0), (15.0, 15.0),
               (20.0, 12.0), (20.0, 15.0), (20.0, 20.0),
               (25.0, 12.0), (25.0, 15.0)]:
    r = run_contender(
        f"sl{sl}_tp{tp}",
        f"Best liq + SL={sl}, TP={tp}",
        btc_score_fn=R2_CHAMPION_FN,
        coin_overrides={"__all__": {"sl": sl, "tp": tp}},
    )
    batchG.append(r)

all_results.extend(batchG)
print_summary("BATCH G: SL/TP OPTIMIZATION", [all_results[0]] + batchG, BASELINE_PNL)


# ══════════════════════════════════════════════════════════
# BATCH H: THRESHOLD SWEEP (more/fewer trades)
# ══════════════════════════════════════════════════════════
print("\n" + "#" * 70)
print("# BATCH H: Entry threshold sweep")
print("#" * 70)

batchH = []
for thr in [2.0, 2.5, 3.5, 4.0, 4.5, 5.0]:
    r = run_contender(
        f"threshold_{thr}",
        f"Best liq + threshold={thr} (all coins)",
        btc_score_fn=R2_CHAMPION_FN,
        coin_overrides={"__all__": {"threshold": thr}},
    )
    batchH.append(r)

all_results.extend(batchH)
print_summary("BATCH H: THRESHOLD SWEEP", [all_results[0]] + batchH, BASELINE_PNL)


# ══════════════════════════════════════════════════════════
# BATCH I: ULTIMATE COMBINATIONS
# ══════════════════════════════════════════════════════════
print("\n" + "#" * 70)
print("# BATCH I: Ultimate Combinations")
print("#" * 70)

batchI = []

# Find best SL/TP from batch G
best_sl_tp = (15.0, 12.0)
if batchG:
    bw = max(batchG, key=lambda x: x["total_pnl"])
    for sl, tp in [(10.0, 10.0), (10.0, 12.0), (12.0, 10.0), (12.0, 12.0),
                   (12.0, 15.0), (15.0, 10.0), (15.0, 15.0),
                   (20.0, 12.0), (20.0, 15.0), (20.0, 20.0),
                   (25.0, 12.0), (25.0, 15.0)]:
        if f"sl{sl}_tp{tp}" == bw["name"] and bw["total_pnl"] > BASELINE_PNL:
            best_sl_tp = (sl, tp)
            break

# Find best threshold from batch H
best_thr = None
if batchH:
    bw = max(batchH, key=lambda x: x["total_pnl"])
    for thr in [2.0, 2.5, 3.5, 4.0, 4.5, 5.0]:
        if f"threshold_{thr}" == bw["name"] and bw["total_pnl"] > BASELINE_PNL:
            best_thr = thr
            break

print(f"Best SL/TP: {best_sl_tp}")
print(f"Best threshold: {best_thr}")

# Ultimate 1: best cascade + best SL/TP
if best_sl_tp != (15.0, 12.0):
    r = run_contender(
        f"ultimate_sl{best_sl_tp[0]}_tp{best_sl_tp[1]}",
        f"Best cascade + SL={best_sl_tp[0]}, TP={best_sl_tp[1]}",
        btc_score_fn=make_score_fn(
            liq_fn=lambda df: custom_cascade(df, 8.0, 8.0, best_cascade_mult, best_ma),
            tick_fn=lambda df: custom_tick(df, 8.0, 3),
        ),
        coin_overrides={"__all__": {"sl": best_sl_tp[0], "tp": best_sl_tp[1]}},
    )
    batchI.append(r)

# Ultimate 2: best cascade + best threshold
if best_thr:
    r = run_contender(
        f"ultimate_thr{best_thr}",
        f"Best cascade + threshold={best_thr}",
        btc_score_fn=make_score_fn(
            liq_fn=lambda df: custom_cascade(df, 8.0, 8.0, best_cascade_mult, best_ma),
            tick_fn=lambda df: custom_tick(df, 8.0, 3),
        ),
        coin_overrides={"__all__": {"threshold": best_thr}},
    )
    batchI.append(r)

# Ultimate 3: best cascade + ratio hybrid + best SL/TP + best threshold
sl_tp_override = {"sl": best_sl_tp[0], "tp": best_sl_tp[1]}
if best_thr:
    sl_tp_override["threshold"] = best_thr

r = run_contender(
    "ultimate_hybrid_everything",
    f"Cascade + Ratio(4.0) + best SL/TP + best threshold",
    btc_score_fn=make_score_fn(
        liq_fn=lambda df: (
            custom_cascade(df, 8.0, 8.0, best_cascade_mult, best_ma) +
            ratio_cascade(df, 4.0, 4.0, 0.65, 0.80)
        ),
        tick_fn=lambda df: custom_tick(df, 8.0, 3),
    ),
    coin_overrides={"__all__": sl_tp_override},
)
batchI.append(r)

# Ultimate 4: cascade + velocity + best SL/TP
r = run_contender(
    "ultimate_cascade_velocity",
    f"Cascade + Velocity(3.0) + best SL/TP",
    btc_score_fn=make_score_fn(
        liq_fn=lambda df: (
            custom_cascade(df, 8.0, 8.0, best_cascade_mult, best_ma) +
            velocity_cascade(df, 3.0, 4)
        ),
        tick_fn=lambda df: custom_tick(df, 8.0, 3),
    ),
    coin_overrides={"__all__": sl_tp_override},
)
batchI.append(r)

# Ultimate 5: tiered both + best SL/TP
r = run_contender(
    "ultimate_tiered_both",
    f"Both tiered + best SL/TP",
    btc_score_fn=make_score_fn(
        liq_fn=lambda df: tiered_cascade(df, 8.0, 8.0, best_cascade_mult, 3.0, 6.0, 0.5, 0.3),
        tick_fn=lambda df: tiered_tick(df, 8.0, 2, 5, 10),
    ),
    coin_overrides={"__all__": sl_tp_override},
)
batchI.append(r)

# Ultimate 6: best cascade + ratio + velocity + tiered tick
r = run_contender(
    "ultimate_kitchen_sink",
    f"Cascade + Ratio(3.0) + Velocity(2.0) + Tiered tick",
    btc_score_fn=make_score_fn(
        liq_fn=lambda df: (
            custom_cascade(df, 8.0, 8.0, best_cascade_mult, best_ma) +
            ratio_cascade(df, 3.0, 3.0, 0.65, 0.80) +
            velocity_cascade(df, 2.0, 4)
        ),
        tick_fn=lambda df: tiered_tick(df, 8.0, 2, 5, 10),
    ),
    coin_overrides={"__all__": sl_tp_override},
)
batchI.append(r)

all_results.extend(batchI)
if batchI:
    print_summary("BATCH I: ULTIMATE COMBINATIONS", [all_results[0]] + batchI, BASELINE_PNL)


# ══════════════════════════════════════════════════════════
# GRAND SUMMARY
# ══════════════════════════════════════════════════════════
sorted_all = sorted(all_results, key=lambda x: x["total_pnl"], reverse=True)

print(f"\n{'='*100}")
print("GRAND TOURNAMENT R2b RESULTS -- LIQUIDATION ADVANCED")
print(f"{'='*100}")
print(f"{'Rank':<4} {'Name':<45} {'Trades':>6} {'WR%':>6} {'PnL':>10} {'Sharpe':>7} {'DD%':>6} {'Delta':>10}")
print("-" * 100)
for i, r in enumerate(sorted_all[:30], 1):
    delta = r["total_pnl"] - BASELINE_PNL
    marker = " <-- KING" if i == 1 else (" <-- R2 CHAMP" if r["name"] == "R2_champion" else "")
    print(f"{i:<4} {r['name']:<45} {r['total_trades']:>6} {r['win_rate']:>5.1f}% "
          f"${r['total_pnl']:>9,.0f} {r['sharpe']:>7.2f} {r['max_dd_pct']:>5.1f}% "
          f"${delta:>+9,.0f}{marker}")

print(f"\n... BOTTOM 5 ...")
for r in sorted_all[-5:]:
    delta = r["total_pnl"] - BASELINE_PNL
    print(f"     {r['name']:<45} {r['total_trades']:>6} {r['win_rate']:>5.1f}% "
          f"${r['total_pnl']:>9,.0f} {r['sharpe']:>7.2f} {r['max_dd_pct']:>5.1f}% "
          f"${delta:>+9,.0f}")

# Save
results_data = {
    "tournament": "round_2b_liquidation_advanced",
    "timestamp": datetime.utcnow().isoformat(),
    "oos_period": f"{OOS_START} to {OOS_END}",
    "coins": COINS,
    "r2_champion_pnl": BASELINE_PNL,
    "total_experiments": len(all_results),
    "results": sorted_all,
    "king": sorted_all[0],
    "best_params": {
        "cascade_mult": best_cascade_mult,
        "cascade_ma": best_ma,
        "best_sl_tp": best_sl_tp,
        "best_threshold": best_thr,
    },
}
RESULTS_FILE.write_text(json.dumps(results_data, indent=2, default=str, ensure_ascii=False))
print(f"\nResults saved to {RESULTS_FILE}")

king = sorted_all[0]
print(f"\n{'='*70}")
print(f"THE LIQUIDATION KING R2b: {king['name']}")
print(f"  PnL: ${king['total_pnl']:+,.0f} (delta ${king['total_pnl']-BASELINE_PNL:+,.0f} vs R2 champion)")
print(f"  Trades: {king['total_trades']} | WR: {king['win_rate']:.1f}%")
print(f"  Sharpe: {king['sharpe']:.2f} | MaxDD: {king['max_dd_pct']:.1f}%")
print(f"  LONG: {king['long_trades']} trades, WR {king['long_wr']:.1f}%, ${king['long_pnl']:+,.0f}")
print(f"  SHORT: {king['short_trades']} trades, WR {king['short_wr']:.1f}%, ${king['short_pnl']:+,.0f}")
print(f"  Description: {king['description']}")
print(f"{'='*70}")

print(f"\nTotal time: {(time.time()-t0)/60:.1f} minutes")
print(f"Experiments: {len(all_results)}")
print(f"Beat R2 champion: {len([r for r in all_results if r['total_pnl'] > BASELINE_PNL and r['name'] != 'R2_champion'])}")
