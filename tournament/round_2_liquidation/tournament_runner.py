"""
Tournament Round 2: LIQUIDATION DEEP DIVE
==========================================
Exhaustive optimization of the liquidation factor — the flagship signal.
Tests every parameter dimension: cascade threshold, MA lookback, tiered scoring,
asymmetric weights, tick-level params, scoring architecture, and interactions.

Based on v5 champion config as baseline.
"""
import sys, os, json, time, traceback
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
OOS_END   = "2026-03-22"
COINS = ["BTC", "XRP", "ADA", "DOT", "SUI", "FIL"]

# v5 champion config (baseline for this tournament)
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
RESULTS_FILE = RESULTS_DIR / "results.json"

# ── Data Loading (once) ─────────────────────────────────
print("=" * 70)
print("TOURNAMENT ROUND 2: LIQUIDATION DEEP DIVE")
print("=" * 70)

t0 = time.time()
btc_ohlcv = fetch_binance_15m("BTCUSDT", years=3)
db_data = load_btc_db_data()
btc_df = build_btc_features(btc_ohlcv, db_data)
print(f"BTC features: {len(btc_df)} bars ({time.time()-t0:.1f}s)")

alt_data = {}
for coin in COINS:
    sym = f"{coin}USDT"
    ohlcv = fetch_binance_15m(sym, years=3)
    alt_data[coin] = build_alt_technicals(ohlcv)
    print(f"  {coin}: {len(alt_data[coin])} bars")

print(f"Data loaded in {time.time()-t0:.1f}s\n")


# ══════════════════════════════════════════════════════════
# CUSTOM LIQUIDATION SCORING FUNCTIONS
# ══════════════════════════════════════════════════════════

def custom_liq_cascade_score(df, weight_bull=5.0, weight_bear=5.0,
                              cascade_mult=3.0, ma_lookback=24):
    """Hourly liquidation cascade with configurable parameters."""
    s = pd.Series(0.0, index=df.index)
    if "liq_net" not in df.columns or "liq_total" not in df.columns:
        return s

    lt = df["liq_total"].fillna(0)
    ln = df["liq_net"].fillna(0)

    # Recompute MA with custom lookback (use raw liq_total, not pre-computed MA)
    if ma_lookback != 24 and "liq_long_1h" in df.columns:
        lt_raw = df["liq_long_1h"].fillna(0) + df["liq_short_1h"].fillna(0)
        lt_ma = lt_raw.rolling(ma_lookback, min_periods=1).mean()
    else:
        lt_ma = df.get("liq_total_ma", lt.rolling(ma_lookback, min_periods=1).mean()).fillna(1)

    cascade = lt > (lt_ma * cascade_mult)
    s += np.where(cascade & (ln > 0), weight_bull, 0)
    s += np.where(cascade & (ln < 0), -weight_bear, 0)
    return s


def tiered_liq_cascade_score(df, weight_bull=5.0, weight_bear=5.0,
                              tier1_mult=2.0, tier2_mult=4.0, tier3_mult=7.0,
                              tier2_bonus=0.5, tier3_bonus=0.5,
                              ma_lookback=24):
    """Tiered cascade: graduated scoring at multiple thresholds."""
    s = pd.Series(0.0, index=df.index)
    if "liq_net" not in df.columns or "liq_total" not in df.columns:
        return s

    lt = df["liq_total"].fillna(0)
    ln = df["liq_net"].fillna(0)
    lt_ma = df.get("liq_total_ma", lt.rolling(ma_lookback, min_periods=1).mean()).fillna(1)

    # Tier 1: base cascade
    t1 = lt > (lt_ma * tier1_mult)
    s += np.where(t1 & (ln > 0), weight_bull, 0)
    s += np.where(t1 & (ln < 0), -weight_bear, 0)

    # Tier 2: strong cascade bonus
    t2 = lt > (lt_ma * tier2_mult)
    s += np.where(t2 & (ln > 0), weight_bull * tier2_bonus, 0)
    s += np.where(t2 & (ln < 0), -weight_bear * tier2_bonus, 0)

    # Tier 3: extreme cascade bonus
    t3 = lt > (lt_ma * tier3_mult)
    s += np.where(t3 & (ln > 0), weight_bull * tier3_bonus, 0)
    s += np.where(t3 & (ln < 0), -weight_bear * tier3_bonus, 0)

    return s


def custom_tick_liq_score(df, weight=3.0, net_threshold=2, notional_mult=3.0,
                           ma_lookback=16, notional_bonus_pct=0.5):
    """Tick liquidation score with configurable parameters."""
    s = pd.Series(0.0, index=df.index)
    if "liq_net_ma" not in df.columns:
        return s

    # If we need custom MA lookback, recompute from raw columns
    if ma_lookback != 16 and "liq_net_count" in df.columns:
        ln = df["liq_net_count"].rolling(ma_lookback, min_periods=1).mean().fillna(0)
        lt = df["liq_notional"].rolling(ma_lookback, min_periods=1).mean().fillna(0) if "liq_notional" in df.columns else df["liq_notional_ma"].fillna(0)
    else:
        ln = df["liq_net_ma"].fillna(0)
        lt = df["liq_notional_ma"].fillna(0)

    # Direction signal
    s += np.where(ln > net_threshold, weight, 0)
    s += np.where(ln < -net_threshold, -weight, 0)

    # Magnitude bonus
    lt_mean = lt[lt > 0].mean() if (lt > 0).any() else 1
    s += np.where(lt > lt_mean * notional_mult, weight * notional_bonus_pct, 0)

    return s


def tiered_tick_liq_score(df, weight=3.0, tier1_net=2, tier2_net=5, tier3_net=10,
                           tier2_bonus=0.5, tier3_bonus=0.5,
                           notional_mult=3.0, notional_bonus_pct=0.5):
    """Tiered tick liquidation: graduated by net count severity."""
    s = pd.Series(0.0, index=df.index)
    if "liq_net_ma" not in df.columns:
        return s

    ln = df["liq_net_ma"].fillna(0)
    lt = df["liq_notional_ma"].fillna(0)

    # Tier 1: base signal
    s += np.where(ln > tier1_net, weight, 0)
    s += np.where(ln < -tier1_net, -weight, 0)

    # Tier 2: strong
    s += np.where(ln > tier2_net, weight * tier2_bonus, 0)
    s += np.where(ln < -tier2_net, -weight * tier2_bonus, 0)

    # Tier 3: extreme
    s += np.where(ln > tier3_net, weight * tier3_bonus, 0)
    s += np.where(ln < -tier3_net, -weight * tier3_bonus, 0)

    # Notional magnitude bonus
    lt_mean = lt[lt > 0].mean() if (lt > 0).any() else 1
    s += np.where(lt > lt_mean * notional_mult, weight * notional_bonus_pct, 0)

    return s


def liq_ratio_score(df, weight_bull=5.0, weight_bear=5.0,
                     ratio_threshold=0.65, extreme_threshold=0.80):
    """Score based on liq ratio (short_pct) instead of net difference."""
    s = pd.Series(0.0, index=df.index)
    if "liq_total" not in df.columns or "liq_short_1h" not in df.columns:
        return s

    lt = df["liq_total"].fillna(0)
    lt_ma = df.get("liq_total_ma", lt.rolling(24, min_periods=1).mean()).fillna(1)

    # Only score when total is meaningful (>1x MA)
    meaningful = lt > lt_ma

    short_pct = df["liq_short_1h"].fillna(0) / lt.clip(lower=1)
    long_pct = 1 - short_pct

    # Shorts dominate = bullish
    s += np.where(meaningful & (short_pct > ratio_threshold), weight_bull, 0)
    s += np.where(meaningful & (short_pct > extreme_threshold), weight_bull * 0.5, 0)

    # Longs dominate = bearish
    s += np.where(meaningful & (long_pct > ratio_threshold), -weight_bear, 0)
    s += np.where(meaningful & (long_pct > extreme_threshold), -weight_bear * 0.5, 0)

    return s


def liq_velocity_score(df, weight=3.0, lookback=4):
    """Score based on rate of change of liquidation volume (acceleration)."""
    s = pd.Series(0.0, index=df.index)
    if "liq_total" not in df.columns:
        return s

    lt = df["liq_total"].fillna(0)
    # Velocity = pct change over lookback
    velocity = lt.pct_change(lookback).fillna(0)

    ln = df["liq_net"].fillna(0)

    # Accelerating liquidation + directional signal
    accel = velocity > 1.0  # >100% increase over lookback
    s += np.where(accel & (ln > 0), weight, 0)
    s += np.where(accel & (ln < 0), -weight, 0)

    # Decelerating = weak signal
    decel = velocity < -0.5  # >50% decrease
    s += np.where(decel & (ln > 0), weight * 0.3, 0)
    s += np.where(decel & (ln < 0), -weight * 0.3, 0)

    return s


def confluence_liq_score(df, weight=5.0, cascade_mult=3.0, net_threshold=2):
    """Only score when BOTH hourly cascade AND tick-level agree."""
    s = pd.Series(0.0, index=df.index)

    # Check hourly cascade
    has_cascade = False
    if "liq_total" in df.columns and "liq_total_ma" in df.columns:
        lt = df["liq_total"].fillna(0)
        lt_ma = df["liq_total_ma"].fillna(1)
        ln_hourly = df["liq_net"].fillna(0)
        cascade = lt > (lt_ma * cascade_mult)
        has_cascade = True

    # Check tick level
    has_tick = False
    if "liq_net_ma" in df.columns:
        ln_tick = df["liq_net_ma"].fillna(0)
        has_tick = True

    if has_cascade and has_tick:
        # Both agree bullish
        both_bull = cascade & (ln_hourly > 0) & (ln_tick > net_threshold)
        s += np.where(both_bull, weight, 0)

        # Both agree bearish
        both_bear = cascade & (ln_hourly < 0) & (ln_tick < -net_threshold)
        s += np.where(both_bear, -weight, 0)
    elif has_cascade:
        # Fallback to cascade only (half weight)
        s += np.where(cascade & (ln_hourly > 0), weight * 0.5, 0)
        s += np.where(cascade & (ln_hourly < 0), -weight * 0.5, 0)

    return s


def make_custom_liq_score_fn(liq_fn, tick_fn, core_overrides=None, extra_overrides=None):
    """Create a BTC score function with custom liquidation scoring.
    Replaces the built-in liq cascade and tick_liq with custom functions."""
    def fn(btc_df_local):
        import backtest_15m_btc_led_alts as bt

        params = dict(V5_CORE)
        if core_overrides:
            params.update(core_overrides)

        extra = dict(V5_EXTRA)
        if extra_overrides:
            extra.update(extra_overrides)

        # Zero out built-in liquidation scoring
        params["w_liq_bull"] = 0.0
        params["w_liq_bear"] = 0.0

        old_extra = dict(bt.V3_EXTRA_WEIGHTS)
        bt.V3_EXTRA_WEIGHTS.update(extra)
        bt.V3_EXTRA_WEIGHTS["tick_liq"] = 0.0  # disable built-in tick_liq

        # Compute base score WITHOUT liquidation
        score = compute_btc_composite_score(btc_df_local, params=params)

        bt.V3_EXTRA_WEIGHTS.update(old_extra)

        # Add custom liquidation scoring
        if liq_fn:
            score += liq_fn(btc_df_local)
        if tick_fn:
            score += tick_fn(btc_df_local)

        return score

    return fn


def make_weight_only_score_fn(core_overrides=None, extra_overrides=None):
    """Create a BTC score function with only weight changes (no custom liq)."""
    def fn(btc_df_local):
        import backtest_15m_btc_led_alts as bt
        old_extra = dict(bt.V3_EXTRA_WEIGHTS)

        params = dict(V5_CORE)
        if core_overrides:
            params.update(core_overrides)

        extra = dict(V5_EXTRA)
        if extra_overrides:
            extra.update(extra_overrides)

        bt.V3_EXTRA_WEIGHTS.update(extra)
        score = compute_btc_composite_score(btc_df_local, params=params)
        bt.V3_EXTRA_WEIGHTS.update(old_extra)
        return score

    return fn


# ── Runner ──────────────────────────────────────────────
def run_contender(name, description, btc_score_fn=None, coin_overrides=None):
    """Run a contender on all coins with v5 baseline config."""
    print(f"\n{'-'*60}")
    print(f"  {name}")
    print(f"  {description}")
    print(f"{'-'*60}")

    t1 = time.time()

    if btc_score_fn:
        btc_score = btc_score_fn(btc_df)
    else:
        btc_score = make_weight_only_score_fn()(btc_df)

    btc_score_ts = pd.Series(btc_score.values, index=btc_df["ts"].values)

    all_trades = []
    coin_results = {}

    for coin in COINS:
        cfg = dict(V5_CONFIGS.get(coin, V5_CONFIGS["DOT"]))
        if coin_overrides and coin in coin_overrides:
            cfg.update(coin_overrides[coin])
        elif coin_overrides and "__all__" in coin_overrides:
            cfg.update(coin_overrides["__all__"])

        alt_df = alt_data[coin]
        signals, alt_merged = generate_btc_led_signal(
            btc_score_ts, alt_df,
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

        trades = run_backtest(
            alt_oos, sig_oos,
            sl_atr_mult=cfg.get("sl", 15.0),
            tp_atr_mult=cfg.get("tp", 12.0),
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

    if all_trades:
        combined = pd.concat(all_trades, ignore_index=True)
        total_pnl = combined["pnl_net"].sum()
        total_trades = len(combined)
        total_wins = (combined["pnl_net"] > 0).sum()
        total_wr = 100 * total_wins / total_trades if total_trades > 0 else 0
        longs = combined[combined["dir"] == "L"]
        shorts = combined[combined["dir"] == "S"]
        long_pnl = longs["pnl_net"].sum() if len(longs) > 0 else 0
        short_pnl = shorts["pnl_net"].sum() if len(shorts) > 0 else 0
        long_wr = 100 * (longs["pnl_net"] > 0).sum() / len(longs) if len(longs) > 0 else 0
        short_wr = 100 * (shorts["pnl_net"] > 0).sum() / len(shorts) if len(shorts) > 0 else 0
        equity = 10000 + combined["pnl_net"].cumsum()
        max_dd = ((equity - equity.cummax()) / equity.cummax() * 100).min()
        ret_per_trade = combined["pnl_net"] / 1000
        sharpe = (ret_per_trade.mean() / ret_per_trade.std() * np.sqrt(total_trades)
                  if ret_per_trade.std() > 0 else 0)
    else:
        total_pnl = total_trades = 0; total_wr = 0
        long_pnl = short_pnl = long_wr = short_wr = 0
        max_dd = sharpe = 0
        longs = shorts = pd.DataFrame()

    elapsed = time.time() - t1

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
        "elapsed_s": round(elapsed, 1),
    }

    print(f"    {total_trades} trades | WR {total_wr:.1f}% | PnL ${total_pnl:+,.0f} | "
          f"Sharpe {sharpe:.2f} | DD {max_dd:.1f}%")

    return result


def print_batch_summary(batch_name, results, baseline_pnl):
    print(f"\n{'='*90}")
    print(f"{batch_name} SUMMARY")
    print(f"{'='*90}")
    print(f"{'Rank':<4} {'Name':<40} {'Trades':>6} {'WR%':>6} {'PnL':>10} {'Sharpe':>7} {'DD%':>6} {'Delta':>10}")
    print("-" * 90)
    for i, r in enumerate(sorted(results, key=lambda x: x["total_pnl"], reverse=True), 1):
        delta = r["total_pnl"] - baseline_pnl
        marker = " ***" if r["total_pnl"] == max(x["total_pnl"] for x in results) else ""
        print(f"{i:<4} {r['name']:<40} {r['total_trades']:>6} {r['win_rate']:>5.1f}% "
              f"${r['total_pnl']:>9,.0f} {r['sharpe']:>7.2f} {r['max_dd_pct']:>5.1f}% "
              f"${delta:>+9,.0f}{marker}")
    return sorted(results, key=lambda x: x["total_pnl"], reverse=True)


all_results = []

# ══════════════════════════════════════════════════════════
# BATCH 0: v5 BASELINE
# ══════════════════════════════════════════════════════════
print("\n" + "#" * 70)
print("# BATCH 0: v5 BASELINE (liq=5.0, tick=3.0, SL=15, TP=12)")
print("#" * 70)

r = run_contender("v5_baseline", "v5 champion (liq=5, tick=3, SL=15, TP=12)")
all_results.append(r)
BASELINE_PNL = r["total_pnl"]
print(f"\n  >>> BASELINE PnL: ${BASELINE_PNL:+,.0f}")


# ══════════════════════════════════════════════════════════
# BATCH 1: CASCADE THRESHOLD SWEEP
# ══════════════════════════════════════════════════════════
print("\n" + "#" * 70)
print("# BATCH 1: Cascade Threshold (currently 3x MA)")
print("#" * 70)

batch1 = []
for mult in [1.5, 2.0, 2.5, 4.0, 5.0, 7.0]:
    r = run_contender(
        f"cascade_mult_{mult}x",
        f"Cascade threshold={mult}x MA (vs 3x baseline)",
        btc_score_fn=make_custom_liq_score_fn(
            liq_fn=lambda df, m=mult: custom_liq_cascade_score(df, 5.0, 5.0, m, 24),
            tick_fn=lambda df: custom_tick_liq_score(df, 3.0),
        ),
    )
    batch1.append(r)

all_results.extend(batch1)
batch1_sorted = print_batch_summary("BATCH 1: CASCADE THRESHOLD", [all_results[0]] + batch1, BASELINE_PNL)


# ══════════════════════════════════════════════════════════
# BATCH 2: MA LOOKBACK SWEEP
# ══════════════════════════════════════════════════════════
print("\n" + "#" * 70)
print("# BATCH 2: Cascade MA Lookback (currently 24 bars = 24h)")
print("#" * 70)

batch2 = []
for lb in [6, 12, 18, 36, 48, 72]:
    r = run_contender(
        f"cascade_ma_{lb}bars",
        f"Cascade MA lookback={lb} bars (vs 24 baseline)",
        btc_score_fn=make_custom_liq_score_fn(
            liq_fn=lambda df, l=lb: custom_liq_cascade_score(df, 5.0, 5.0, 3.0, l),
            tick_fn=lambda df: custom_tick_liq_score(df, 3.0),
        ),
    )
    batch2.append(r)

all_results.extend(batch2)
batch2_sorted = print_batch_summary("BATCH 2: MA LOOKBACK", [all_results[0]] + batch2, BASELINE_PNL)


# ══════════════════════════════════════════════════════════
# BATCH 3: HOURLY LIQ WEIGHT SWEEP (beyond v5's 5.0)
# ══════════════════════════════════════════════════════════
print("\n" + "#" * 70)
print("# BATCH 3: Hourly Liq Weight (currently 5.0)")
print("#" * 70)

batch3 = []
for w in [3.0, 4.0, 6.0, 7.0, 8.0, 10.0, 15.0]:
    r = run_contender(
        f"liq_weight_{w}",
        f"Hourly liq weight={w} (vs 5.0 baseline)",
        btc_score_fn=make_weight_only_score_fn(
            core_overrides={"w_liq_bull": w, "w_liq_bear": w},
        ),
    )
    batch3.append(r)

all_results.extend(batch3)
batch3_sorted = print_batch_summary("BATCH 3: HOURLY LIQ WEIGHT", [all_results[0]] + batch3, BASELINE_PNL)


# ══════════════════════════════════════════════════════════
# BATCH 4: TICK LIQ WEIGHT SWEEP
# ══════════════════════════════════════════════════════════
print("\n" + "#" * 70)
print("# BATCH 4: Tick Liq Weight (currently 3.0)")
print("#" * 70)

batch4 = []
for tw in [1.0, 2.0, 4.0, 5.0, 6.0, 8.0]:
    r = run_contender(
        f"tick_weight_{tw}",
        f"Tick liq weight={tw} (vs 3.0 baseline)",
        btc_score_fn=make_weight_only_score_fn(
            extra_overrides={"tick_liq": tw},
        ),
    )
    batch4.append(r)

all_results.extend(batch4)
batch4_sorted = print_batch_summary("BATCH 4: TICK LIQ WEIGHT", [all_results[0]] + batch4, BASELINE_PNL)


# ══════════════════════════════════════════════════════════
# BATCH 5: TICK NET COUNT THRESHOLD
# ══════════════════════════════════════════════════════════
print("\n" + "#" * 70)
print("# BATCH 5: Tick Net Count Threshold (currently ±2)")
print("#" * 70)

batch5 = []
for thr in [1, 3, 4, 5, 0.5]:
    r = run_contender(
        f"tick_net_thr_{thr}",
        f"Tick net count threshold=±{thr} (vs ±2 baseline)",
        btc_score_fn=make_custom_liq_score_fn(
            liq_fn=lambda df: custom_liq_cascade_score(df, 5.0, 5.0, 3.0, 24),
            tick_fn=lambda df, t=thr: custom_tick_liq_score(df, 3.0, net_threshold=t),
        ),
    )
    batch5.append(r)

all_results.extend(batch5)
batch5_sorted = print_batch_summary("BATCH 5: TICK NET THRESHOLD", [all_results[0]] + batch5, BASELINE_PNL)


# ══════════════════════════════════════════════════════════
# BATCH 6: TICK MA LOOKBACK
# ══════════════════════════════════════════════════════════
print("\n" + "#" * 70)
print("# BATCH 6: Tick MA Lookback (currently 16 bars = 4h)")
print("#" * 70)

batch6 = []
for tlb in [4, 8, 12, 24, 32, 48]:
    r = run_contender(
        f"tick_ma_{tlb}bars",
        f"Tick MA lookback={tlb} bars (vs 16 baseline)",
        btc_score_fn=make_custom_liq_score_fn(
            liq_fn=lambda df: custom_liq_cascade_score(df, 5.0, 5.0, 3.0, 24),
            tick_fn=lambda df, l=tlb: custom_tick_liq_score(df, 3.0, ma_lookback=l),
        ),
    )
    batch6.append(r)

all_results.extend(batch6)
batch6_sorted = print_batch_summary("BATCH 6: TICK MA LOOKBACK", [all_results[0]] + batch6, BASELINE_PNL)


# ══════════════════════════════════════════════════════════
# BATCH 7: ASYMMETRIC BULL/BEAR WEIGHTS
# ══════════════════════════════════════════════════════════
print("\n" + "#" * 70)
print("# BATCH 7: Asymmetric Bull/Bear Liq Weights")
print("#" * 70)

batch7 = []
for bull, bear in [(3.0, 7.0), (4.0, 6.0), (6.0, 4.0), (7.0, 3.0),
                    (3.0, 8.0), (2.0, 8.0), (5.0, 8.0), (8.0, 5.0)]:
    r = run_contender(
        f"asym_b{bull}_s{bear}",
        f"Asymmetric: bull={bull}, bear={bear} (vs symmetric 5.0)",
        btc_score_fn=make_weight_only_score_fn(
            core_overrides={"w_liq_bull": bull, "w_liq_bear": bear},
        ),
    )
    batch7.append(r)

all_results.extend(batch7)
batch7_sorted = print_batch_summary("BATCH 7: ASYMMETRIC WEIGHTS", [all_results[0]] + batch7, BASELINE_PNL)


# ══════════════════════════════════════════════════════════
# BATCH 8: TIERED CASCADE SCORING
# ══════════════════════════════════════════════════════════
print("\n" + "#" * 70)
print("# BATCH 8: Tiered Cascade Scoring (graduated levels)")
print("#" * 70)

batch8 = []

# Tiered: 2x/4x/7x with 50%/50% bonus
r = run_contender(
    "tiered_2_4_7_50_50",
    "Tiered cascade: 2x/4x/7x MA, +50%/+50% bonus",
    btc_score_fn=make_custom_liq_score_fn(
        liq_fn=lambda df: tiered_liq_cascade_score(df, 5.0, 5.0, 2.0, 4.0, 7.0, 0.5, 0.5),
        tick_fn=lambda df: custom_tick_liq_score(df, 3.0),
    ),
)
batch8.append(r)

# Tiered: 2x/5x/10x with 50%/100% bonus
r = run_contender(
    "tiered_2_5_10_50_100",
    "Tiered cascade: 2x/5x/10x MA, +50%/+100% bonus",
    btc_score_fn=make_custom_liq_score_fn(
        liq_fn=lambda df: tiered_liq_cascade_score(df, 5.0, 5.0, 2.0, 5.0, 10.0, 0.5, 1.0),
        tick_fn=lambda df: custom_tick_liq_score(df, 3.0),
    ),
)
batch8.append(r)

# Tiered: 1.5x/3x/6x with 30%/30% bonus (more triggers, smaller bonuses)
r = run_contender(
    "tiered_1.5_3_6_30_30",
    "Tiered cascade: 1.5x/3x/6x MA, +30%/+30% bonus",
    btc_score_fn=make_custom_liq_score_fn(
        liq_fn=lambda df: tiered_liq_cascade_score(df, 5.0, 5.0, 1.5, 3.0, 6.0, 0.3, 0.3),
        tick_fn=lambda df: custom_tick_liq_score(df, 3.0),
    ),
)
batch8.append(r)

# Tiered: 3x/6x/10x with 50%/50% (conservative, higher bars)
r = run_contender(
    "tiered_3_6_10_50_50",
    "Tiered cascade: 3x/6x/10x MA, +50%/+50% bonus",
    btc_score_fn=make_custom_liq_score_fn(
        liq_fn=lambda df: tiered_liq_cascade_score(df, 5.0, 5.0, 3.0, 6.0, 10.0, 0.5, 0.5),
        tick_fn=lambda df: custom_tick_liq_score(df, 3.0),
    ),
)
batch8.append(r)

# Tiered tick: 2/5/10 net count with 50%/50% bonus
r = run_contender(
    "tiered_tick_2_5_10",
    "Tiered tick: net>2/5/10, +50%/+50% bonus",
    btc_score_fn=make_custom_liq_score_fn(
        liq_fn=lambda df: custom_liq_cascade_score(df, 5.0, 5.0, 3.0, 24),
        tick_fn=lambda df: tiered_tick_liq_score(df, 3.0, 2, 5, 10, 0.5, 0.5),
    ),
)
batch8.append(r)

# Both tiered
r = run_contender(
    "both_tiered",
    "Both layers tiered: cascade 2x/4x/7x + tick 2/5/10",
    btc_score_fn=make_custom_liq_score_fn(
        liq_fn=lambda df: tiered_liq_cascade_score(df, 5.0, 5.0, 2.0, 4.0, 7.0, 0.5, 0.5),
        tick_fn=lambda df: tiered_tick_liq_score(df, 3.0, 2, 5, 10, 0.5, 0.5),
    ),
)
batch8.append(r)

all_results.extend(batch8)
batch8_sorted = print_batch_summary("BATCH 8: TIERED SCORING", [all_results[0]] + batch8, BASELINE_PNL)


# ══════════════════════════════════════════════════════════
# BATCH 9: ALTERNATIVE SCORING ARCHITECTURES
# ══════════════════════════════════════════════════════════
print("\n" + "#" * 70)
print("# BATCH 9: Alternative Scoring Architectures")
print("#" * 70)

batch9 = []

# Ratio-based instead of net difference
r = run_contender(
    "ratio_65_80",
    "Ratio-based: 65%/80% threshold (instead of net)",
    btc_score_fn=make_custom_liq_score_fn(
        liq_fn=lambda df: liq_ratio_score(df, 5.0, 5.0, 0.65, 0.80),
        tick_fn=lambda df: custom_tick_liq_score(df, 3.0),
    ),
)
batch9.append(r)

# Ratio-based with lower thresholds
r = run_contender(
    "ratio_55_70",
    "Ratio-based: 55%/70% threshold (more triggers)",
    btc_score_fn=make_custom_liq_score_fn(
        liq_fn=lambda df: liq_ratio_score(df, 5.0, 5.0, 0.55, 0.70),
        tick_fn=lambda df: custom_tick_liq_score(df, 3.0),
    ),
)
batch9.append(r)

# Velocity-based
r = run_contender(
    "velocity_lb4",
    "Velocity-based: acceleration over 4 bars",
    btc_score_fn=make_custom_liq_score_fn(
        liq_fn=lambda df: liq_velocity_score(df, 5.0, 4),
        tick_fn=lambda df: custom_tick_liq_score(df, 3.0),
    ),
)
batch9.append(r)

# Velocity with longer lookback
r = run_contender(
    "velocity_lb8",
    "Velocity-based: acceleration over 8 bars",
    btc_score_fn=make_custom_liq_score_fn(
        liq_fn=lambda df: liq_velocity_score(df, 5.0, 8),
        tick_fn=lambda df: custom_tick_liq_score(df, 3.0),
    ),
)
batch9.append(r)

# Confluence: require both layers
r = run_contender(
    "confluence_3x_2net",
    "Confluence: require BOTH cascade(3x) AND tick(net>2)",
    btc_score_fn=make_custom_liq_score_fn(
        liq_fn=lambda df: confluence_liq_score(df, 7.0, 3.0, 2),
        tick_fn=None,  # Embedded in confluence function
    ),
)
batch9.append(r)

# Confluence with lower thresholds
r = run_contender(
    "confluence_2x_1net",
    "Confluence: require BOTH cascade(2x) AND tick(net>1)",
    btc_score_fn=make_custom_liq_score_fn(
        liq_fn=lambda df: confluence_liq_score(df, 7.0, 2.0, 1),
        tick_fn=None,
    ),
)
batch9.append(r)

# Cascade + velocity combination
r = run_contender(
    "cascade_plus_velocity",
    "Cascade(5.0) + Velocity(3.0) instead of tick_liq",
    btc_score_fn=make_custom_liq_score_fn(
        liq_fn=lambda df: custom_liq_cascade_score(df, 5.0, 5.0, 3.0, 24) + liq_velocity_score(df, 3.0, 4),
        tick_fn=None,
    ),
)
batch9.append(r)

all_results.extend(batch9)
batch9_sorted = print_batch_summary("BATCH 9: ALTERNATIVE ARCHITECTURES", [all_results[0]] + batch9, BASELINE_PNL)


# ══════════════════════════════════════════════════════════
# BATCH 10: LIQUIDATION DOMINANCE (reduce other factors)
# ══════════════════════════════════════════════════════════
print("\n" + "#" * 70)
print("# BATCH 10: Liquidation Dominance Tests")
print("#" * 70)

batch10 = []

# Liq-only (zero all other factors)
r = run_contender(
    "liq_only",
    "Liquidation ONLY (zero all other factors)",
    btc_score_fn=make_weight_only_score_fn(
        core_overrides={
            "w_fr_neg": 0.0, "w_fr_pos": 0.0,
            "w_whale_bull": 0.0, "w_whale_bear": 0.0,
            "w_etf_bull": 0.0, "w_etf_bear": 0.0,
            "w_oi_bull": 0.0, "w_oi_capit": 0.0, "w_oi_weak": 0.0, "w_oi_bear": 0.0,
        },
        extra_overrides={"ob_combined": 0.0, "basis_contrarian": 0.0},
    ),
)
batch10.append(r)

# Liq-dominated (liq=10, others=0.5)
r = run_contender(
    "liq_dominated",
    "Liq-dominated: liq=10.0, others=0.5",
    btc_score_fn=make_weight_only_score_fn(
        core_overrides={
            "w_liq_bull": 10.0, "w_liq_bear": 10.0,
            "w_fr_neg": 0.5, "w_fr_pos": 0.5,
            "w_whale_bull": 0.5, "w_whale_bear": 0.5,
            "w_etf_bull": 0.5, "w_etf_bear": 0.5,
            "w_oi_bull": 0.1, "w_oi_capit": 0.1, "w_oi_weak": 0.1, "w_oi_bear": 0.1,
        },
        extra_overrides={"ob_combined": 0.5, "basis_contrarian": 0.5, "tick_liq": 5.0},
    ),
)
batch10.append(r)

# Liq + tick_liq only
r = run_contender(
    "liq_tick_only",
    "Only hourly liq(5.0) + tick_liq(3.0), drop everything else",
    btc_score_fn=make_weight_only_score_fn(
        core_overrides={
            "w_fr_neg": 0.0, "w_fr_pos": 0.0,
            "w_whale_bull": 0.0, "w_whale_bear": 0.0,
            "w_etf_bull": 0.0, "w_etf_bear": 0.0,
            "w_oi_bull": 0.0, "w_oi_capit": 0.0, "w_oi_weak": 0.0, "w_oi_bear": 0.0,
        },
        extra_overrides={"ob_combined": 0.0, "basis_contrarian": 0.0},
    ),
)
batch10.append(r)

# Super liq: both layers cranked up
r = run_contender(
    "super_liq",
    "Super liq: hourly=8.0, tick=6.0 (other factors at v5)",
    btc_score_fn=make_weight_only_score_fn(
        core_overrides={"w_liq_bull": 8.0, "w_liq_bear": 8.0},
        extra_overrides={"tick_liq": 6.0},
    ),
)
batch10.append(r)

all_results.extend(batch10)
batch10_sorted = print_batch_summary("BATCH 10: LIQ DOMINANCE", [all_results[0]] + batch10, BASELINE_PNL)


# ══════════════════════════════════════════════════════════
# BATCH 11: NOTIONAL MULTIPLIER SWEEP
# ══════════════════════════════════════════════════════════
print("\n" + "#" * 70)
print("# BATCH 11: Tick Notional Multiplier (currently 3x mean)")
print("#" * 70)

batch11 = []
for nm in [1.5, 2.0, 4.0, 5.0, 7.0]:
    r = run_contender(
        f"tick_notional_{nm}x",
        f"Tick notional multiplier={nm}x (vs 3x baseline)",
        btc_score_fn=make_custom_liq_score_fn(
            liq_fn=lambda df: custom_liq_cascade_score(df, 5.0, 5.0, 3.0, 24),
            tick_fn=lambda df, n=nm: custom_tick_liq_score(df, 3.0, notional_mult=n),
        ),
    )
    batch11.append(r)

all_results.extend(batch11)
batch11_sorted = print_batch_summary("BATCH 11: NOTIONAL MULTIPLIER", [all_results[0]] + batch11, BASELINE_PNL)


# ══════════════════════════════════════════════════════════
# BATCH 12: NOTIONAL BONUS % SWEEP
# ══════════════════════════════════════════════════════════
print("\n" + "#" * 70)
print("# BATCH 12: Tick Notional Bonus % (currently 0.5 = 50%)")
print("#" * 70)

batch12 = []
for bp in [0.0, 0.25, 0.75, 1.0, 1.5]:
    r = run_contender(
        f"tick_bonus_{int(bp*100)}pct",
        f"Tick notional bonus={bp*100:.0f}% (vs 50% baseline)",
        btc_score_fn=make_custom_liq_score_fn(
            liq_fn=lambda df: custom_liq_cascade_score(df, 5.0, 5.0, 3.0, 24),
            tick_fn=lambda df, b=bp: custom_tick_liq_score(df, 3.0, notional_bonus_pct=b),
        ),
    )
    batch12.append(r)

all_results.extend(batch12)
batch12_sorted = print_batch_summary("BATCH 12: NOTIONAL BONUS %", [all_results[0]] + batch12, BASELINE_PNL)


# ══════════════════════════════════════════════════════════
# IDENTIFY BATCH WINNERS → COMBINE IN BATCH 13
# ══════════════════════════════════════════════════════════
print("\n" + "#" * 70)
print("# IDENTIFYING BATCH WINNERS FOR COMBINATION")
print("#" * 70)

# Collect best from each parameter dimension
batch_winners = {}
for bname, bresults in [
    ("cascade_mult", batch1), ("cascade_ma", batch2),
    ("liq_weight", batch3), ("tick_weight", batch4),
    ("tick_net_thr", batch5), ("tick_ma", batch6),
    ("asym", batch7), ("tiered", batch8),
    ("architecture", batch9), ("dominance", batch10),
    ("notional_mult", batch11), ("notional_bonus", batch12),
]:
    if bresults:
        winner = max(bresults, key=lambda x: x["total_pnl"])
        if winner["total_pnl"] > BASELINE_PNL:
            batch_winners[bname] = winner
            print(f"  {bname}: {winner['name']} (${winner['total_pnl']:+,.0f}, delta ${winner['total_pnl']-BASELINE_PNL:+,.0f})")
        else:
            print(f"  {bname}: no winner beat baseline")

print(f"\n  {len(batch_winners)} dimensions beat baseline")


# ══════════════════════════════════════════════════════════
# BATCH 13: GRAND COMBINATIONS
# ══════════════════════════════════════════════════════════
print("\n" + "#" * 70)
print("# BATCH 13: Grand Combinations from Batch Winners")
print("#" * 70)

batch13 = []

# Let's systematically extract the best parameters found
# and combine the best cascade params with best tick params

# Best cascade mult (from batch1)
best_cascade_mult = 3.0  # default
if batch1:
    bw = max(batch1, key=lambda x: x["total_pnl"])
    # Extract mult from name
    for mult in [1.5, 2.0, 2.5, 4.0, 5.0, 7.0]:
        if f"cascade_mult_{mult}x" == bw["name"]:
            best_cascade_mult = mult
            break
    if bw["total_pnl"] <= BASELINE_PNL:
        best_cascade_mult = 3.0  # baseline is still best

# Best cascade MA (from batch2)
best_cascade_ma = 24  # default
if batch2:
    bw = max(batch2, key=lambda x: x["total_pnl"])
    for lb in [6, 12, 18, 36, 48, 72]:
        if f"cascade_ma_{lb}bars" == bw["name"]:
            best_cascade_ma = lb
            break
    if bw["total_pnl"] <= BASELINE_PNL:
        best_cascade_ma = 24

# Best hourly weight (from batch3)
best_liq_weight = 5.0
if batch3:
    bw = max(batch3, key=lambda x: x["total_pnl"])
    for w in [3.0, 4.0, 6.0, 7.0, 8.0, 10.0, 15.0]:
        if f"liq_weight_{w}" == bw["name"]:
            best_liq_weight = w
            break
    if bw["total_pnl"] <= BASELINE_PNL:
        best_liq_weight = 5.0

# Best tick weight (from batch4)
best_tick_weight = 3.0
if batch4:
    bw = max(batch4, key=lambda x: x["total_pnl"])
    for tw in [1.0, 2.0, 4.0, 5.0, 6.0, 8.0]:
        if f"tick_weight_{tw}" == bw["name"]:
            best_tick_weight = tw
            break
    if bw["total_pnl"] <= BASELINE_PNL:
        best_tick_weight = 3.0

# Best tick net threshold (from batch5)
best_tick_net = 2
if batch5:
    bw = max(batch5, key=lambda x: x["total_pnl"])
    for thr in [1, 3, 4, 5, 0.5]:
        if f"tick_net_thr_{thr}" == bw["name"]:
            best_tick_net = thr
            break
    if bw["total_pnl"] <= BASELINE_PNL:
        best_tick_net = 2

# Best tick MA (from batch6)
best_tick_ma = 16
if batch6:
    bw = max(batch6, key=lambda x: x["total_pnl"])
    for tlb in [4, 8, 12, 24, 32, 48]:
        if f"tick_ma_{tlb}bars" == bw["name"]:
            best_tick_ma = tlb
            break
    if bw["total_pnl"] <= BASELINE_PNL:
        best_tick_ma = 16

print(f"\nBest params found:")
print(f"  Cascade mult: {best_cascade_mult}x")
print(f"  Cascade MA: {best_cascade_ma} bars")
print(f"  Hourly weight: {best_liq_weight}")
print(f"  Tick weight: {best_tick_weight}")
print(f"  Tick net threshold: ±{best_tick_net}")
print(f"  Tick MA: {best_tick_ma} bars")

# Combo 1: Best cascade params + baseline tick
r = run_contender(
    "combo_best_cascade",
    f"Best cascade (mult={best_cascade_mult}x, MA={best_cascade_ma}) + baseline tick",
    btc_score_fn=make_custom_liq_score_fn(
        liq_fn=lambda df: custom_liq_cascade_score(df, best_liq_weight, best_liq_weight,
                                                    best_cascade_mult, best_cascade_ma),
        tick_fn=lambda df: custom_tick_liq_score(df, 3.0),
    ),
)
batch13.append(r)

# Combo 2: Baseline cascade + best tick params
r = run_contender(
    "combo_best_tick",
    f"Baseline cascade + best tick (w={best_tick_weight}, net=±{best_tick_net}, MA={best_tick_ma})",
    btc_score_fn=make_custom_liq_score_fn(
        liq_fn=lambda df: custom_liq_cascade_score(df, 5.0, 5.0, 3.0, 24),
        tick_fn=lambda df: custom_tick_liq_score(df, best_tick_weight, best_tick_net,
                                                  ma_lookback=best_tick_ma),
    ),
)
batch13.append(r)

# Combo 3: BOTH best cascade + best tick
r = run_contender(
    "combo_best_both",
    f"Best cascade + best tick ALL params combined",
    btc_score_fn=make_custom_liq_score_fn(
        liq_fn=lambda df: custom_liq_cascade_score(df, best_liq_weight, best_liq_weight,
                                                    best_cascade_mult, best_cascade_ma),
        tick_fn=lambda df: custom_tick_liq_score(df, best_tick_weight, best_tick_net,
                                                  ma_lookback=best_tick_ma),
    ),
)
batch13.append(r)

# Combo 4: Best both + best hourly weight
r = run_contender(
    "combo_best_all_w_weight",
    f"Best all + hourly weight={best_liq_weight}",
    btc_score_fn=make_custom_liq_score_fn(
        liq_fn=lambda df: custom_liq_cascade_score(df, best_liq_weight, best_liq_weight,
                                                    best_cascade_mult, best_cascade_ma),
        tick_fn=lambda df: custom_tick_liq_score(df, best_tick_weight, best_tick_net,
                                                  ma_lookback=best_tick_ma),
    ),
)
batch13.append(r)

# Combo 5: Best both + tiered
r = run_contender(
    "combo_tiered_best_tick",
    f"Tiered cascade(2x/4x/7x) + best tick params",
    btc_score_fn=make_custom_liq_score_fn(
        liq_fn=lambda df: tiered_liq_cascade_score(df, best_liq_weight, best_liq_weight,
                                                    2.0, 4.0, 7.0, 0.5, 0.5),
        tick_fn=lambda df: custom_tick_liq_score(df, best_tick_weight, best_tick_net,
                                                  ma_lookback=best_tick_ma),
    ),
)
batch13.append(r)

# Combo 6: Best both + higher liq weight
for w_try in [7.0, 8.0, 10.0]:
    r = run_contender(
        f"combo_best_liq_w{w_try}",
        f"Best params + liq weight={w_try}",
        btc_score_fn=make_custom_liq_score_fn(
            liq_fn=lambda df, w=w_try: custom_liq_cascade_score(df, w, w,
                                                        best_cascade_mult, best_cascade_ma),
            tick_fn=lambda df: custom_tick_liq_score(df, best_tick_weight, best_tick_net,
                                                      ma_lookback=best_tick_ma),
        ),
    )
    batch13.append(r)

# Combo 7: Best both + asymmetric if it helped
if "asym" in batch_winners:
    # Extract best asym params
    bw = batch_winners["asym"]
    # Try best asym + best other params
    r = run_contender(
        "combo_best_asym_combined",
        f"Best asymmetric + best cascade/tick params",
        btc_score_fn=make_custom_liq_score_fn(
            liq_fn=lambda df: custom_liq_cascade_score(df, 3.0, 7.0,
                                                        best_cascade_mult, best_cascade_ma),
            tick_fn=lambda df: custom_tick_liq_score(df, best_tick_weight, best_tick_net,
                                                      ma_lookback=best_tick_ma),
        ),
    )
    batch13.append(r)

all_results.extend(batch13)
batch13_sorted = print_batch_summary("BATCH 13: GRAND COMBINATIONS", [all_results[0]] + batch13, BASELINE_PNL)


# ══════════════════════════════════════════════════════════
# BATCH 14: FINE-TUNING THE CHAMPION
# ══════════════════════════════════════════════════════════
print("\n" + "#" * 70)
print("# BATCH 14: Fine-Tuning the Champion")
print("#" * 70)

# Find the current best
current_best = max(all_results, key=lambda x: x["total_pnl"])
print(f"Current champion: {current_best['name']} at ${current_best['total_pnl']:+,.0f}")

batch14 = []

# Fine-tune cascade mult ±0.5 around best
if best_cascade_mult != 3.0:
    for delta in [-0.5, +0.5]:
        cm = best_cascade_mult + delta
        if cm > 0:
            r = run_contender(
                f"finetune_cascade_{cm}x",
                f"Fine-tune cascade mult={cm}x (±0.5 from best {best_cascade_mult}x)",
                btc_score_fn=make_custom_liq_score_fn(
                    liq_fn=lambda df, m=cm: custom_liq_cascade_score(df, best_liq_weight, best_liq_weight,
                                                                      m, best_cascade_ma),
                    tick_fn=lambda df: custom_tick_liq_score(df, best_tick_weight, best_tick_net,
                                                              ma_lookback=best_tick_ma),
                ),
            )
            batch14.append(r)

# Fine-tune hourly weight ±0.5
for delta in [-1.0, -0.5, +0.5, +1.0]:
    w = best_liq_weight + delta
    if w > 0:
        r = run_contender(
            f"finetune_liq_w{w}",
            f"Fine-tune liq weight={w} (±from best {best_liq_weight})",
            btc_score_fn=make_custom_liq_score_fn(
                liq_fn=lambda df, ww=w: custom_liq_cascade_score(df, ww, ww,
                                                                   best_cascade_mult, best_cascade_ma),
                tick_fn=lambda df: custom_tick_liq_score(df, best_tick_weight, best_tick_net,
                                                          ma_lookback=best_tick_ma),
            ),
        )
        batch14.append(r)

# Fine-tune tick weight ±0.5
for delta in [-1.0, -0.5, +0.5, +1.0]:
    tw = best_tick_weight + delta
    if tw > 0:
        r = run_contender(
            f"finetune_tick_w{tw}",
            f"Fine-tune tick weight={tw} (±from best {best_tick_weight})",
            btc_score_fn=make_custom_liq_score_fn(
                liq_fn=lambda df: custom_liq_cascade_score(df, best_liq_weight, best_liq_weight,
                                                            best_cascade_mult, best_cascade_ma),
                tick_fn=lambda df, t=tw: custom_tick_liq_score(df, t, best_tick_net,
                                                                ma_lookback=best_tick_ma),
            ),
        )
        batch14.append(r)

# Test with different SL/TP on champion config
for sl, tp in [(12.0, 10.0), (12.0, 12.0), (15.0, 15.0), (20.0, 12.0), (20.0, 15.0)]:
    r = run_contender(
        f"finetune_sl{sl}_tp{tp}",
        f"Champion config + SL={sl}, TP={tp}",
        btc_score_fn=make_custom_liq_score_fn(
            liq_fn=lambda df: custom_liq_cascade_score(df, best_liq_weight, best_liq_weight,
                                                        best_cascade_mult, best_cascade_ma),
            tick_fn=lambda df: custom_tick_liq_score(df, best_tick_weight, best_tick_net,
                                                      ma_lookback=best_tick_ma),
        ),
        coin_overrides={"__all__": {"sl": sl, "tp": tp}},
    )
    batch14.append(r)

all_results.extend(batch14)
batch14_sorted = print_batch_summary("BATCH 14: FINE-TUNING", [all_results[0]] + batch14, BASELINE_PNL)


# ══════════════════════════════════════════════════════════
# GRAND SUMMARY
# ══════════════════════════════════════════════════════════
print("\n" + "=" * 100)
print("GRAND TOURNAMENT RESULTS -- LIQUIDATION DEEP DIVE")
print("=" * 100)
print(f"{'Rank':<4} {'Name':<42} {'Trades':>6} {'WR%':>6} {'PnL':>10} {'Sharpe':>7} {'DD%':>6} {'Delta':>10}")
print("-" * 100)

sorted_all = sorted(all_results, key=lambda x: x["total_pnl"], reverse=True)
for i, r in enumerate(sorted_all[:30], 1):  # Top 30
    delta = r["total_pnl"] - BASELINE_PNL
    marker = " <-- KING" if i == 1 else (" <-- BASELINE" if r["name"] == "v5_baseline" else "")
    print(f"{i:<4} {r['name']:<42} {r['total_trades']:>6} {r['win_rate']:>5.1f}% "
          f"${r['total_pnl']:>9,.0f} {r['sharpe']:>7.2f} {r['max_dd_pct']:>5.1f}% "
          f"${delta:>+9,.0f}{marker}")

# Bottom 5
print("\n... BOTTOM 5 ...")
for r in sorted_all[-5:]:
    delta = r["total_pnl"] - BASELINE_PNL
    print(f"     {r['name']:<42} {r['total_trades']:>6} {r['win_rate']:>5.1f}% "
          f"${r['total_pnl']:>9,.0f} {r['sharpe']:>7.2f} {r['max_dd_pct']:>5.1f}% "
          f"${delta:>+9,.0f}")

# Save results
results_data = {
    "tournament": "round_2_liquidation",
    "timestamp": datetime.utcnow().isoformat(),
    "oos_period": f"{OOS_START} to {OOS_END}",
    "coins": COINS,
    "baseline_pnl": BASELINE_PNL,
    "total_experiments": len(all_results),
    "results": sorted_all,
    "king": sorted_all[0],
    "best_params": {
        "cascade_mult": best_cascade_mult,
        "cascade_ma": best_cascade_ma,
        "liq_weight": best_liq_weight,
        "tick_weight": best_tick_weight,
        "tick_net_threshold": best_tick_net,
        "tick_ma_lookback": best_tick_ma,
    },
}
RESULTS_FILE.write_text(json.dumps(results_data, indent=2, default=str, ensure_ascii=False))
print(f"\nResults saved to {RESULTS_FILE}")

# THE KING
king = sorted_all[0]
print("\n" + "=" * 70)
print(f"THE LIQUIDATION KING: {king['name']}")
print(f"  PnL: ${king['total_pnl']:+,.0f} (delta ${king['total_pnl']-BASELINE_PNL:+,.0f} vs v5 baseline)")
print(f"  Trades: {king['total_trades']} | WR: {king['win_rate']:.1f}%")
print(f"  Sharpe: {king['sharpe']:.2f} | MaxDD: {king['max_dd_pct']:.1f}%")
print(f"  LONG: {king['long_trades']} trades, WR {king['long_wr']:.1f}%, ${king['long_pnl']:+,.0f}")
print(f"  SHORT: {king['short_trades']} trades, WR {king['short_wr']:.1f}%, ${king['short_pnl']:+,.0f}")
print(f"  Description: {king['description']}")
print("=" * 70)

total_time = time.time() - t0
print(f"\nTotal tournament time: {total_time/60:.1f} minutes")
print(f"Experiments run: {len(all_results)}")
print(f"Experiments beating baseline: {len([r for r in all_results if r['total_pnl'] > BASELINE_PNL and r['name'] != 'v5_baseline'])}")
