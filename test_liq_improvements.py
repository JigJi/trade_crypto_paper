"""
Liquidation Scoring Improvements: P2 (Magnitude) + P4 (Velocity)
================================================================
Tests whether proportional scoring and faster rolling windows
improve over the current binary liquidation scoring in v5 config.

8 experiments × 5 coins = 40 backtests.
"""
import sys, os, json, time, copy
from pathlib import Path
from datetime import datetime

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
os.chdir(str(BASE_DIR))

import pandas as pd, numpy as np
import backtest_15m_btc_led_alts as bt
from backtest_15m_btc_led_alts import (
    fetch_binance_15m, load_btc_db_data, build_btc_features,
    compute_btc_composite_score, build_alt_technicals,
    generate_btc_led_signal, run_backtest, calc_metrics,
    score_tick_liq, score_basis_contrarian, score_ob_combined,
    V3_EXTRA_WEIGHTS, COMPOSITE_WEIGHTS, resample_to_15m,
    BKK_UTC_OFFSET,
)

# ── V5 Config ──
V5_LIQ = 5.0
V5_OB = 2.0
V5_TICK = 3.0
V5_BASIS = 1.5
V5_SL = 15.0
V5_TP = 12.0
V5_CD = 4
V5_THRESHOLD = 3.0
V5_MAX_HOLD = 96

OOS_START, OOS_END = "2025-01-01", "2026-03-18"
SAMPLE_COINS = ["FARTCOIN", "AAVE", "DOGE", "SEI", "CRV"]

# ═══════════════════════════════════════════════════════════════════
# SCORING OVERRIDES
# ═══════════════════════════════════════════════════════════════════

def score_cascade_magnitude(df, weight=5.0):
    """P2: Proportional cascade scoring (0→weight instead of binary)."""
    s = pd.Series(0.0, index=df.index)
    if "liq_net" not in df.columns or "liq_total_ma" not in df.columns:
        return s
    lt = df["liq_total"].fillna(0)
    lt_ma = df["liq_total_ma"].fillna(1).clip(lower=1e-6)
    ln = df["liq_net"].fillna(0)
    ratio = (lt / (lt_ma * 3)).clip(upper=3.0)  # 0 to 3
    magnitude = ratio / 3.0  # normalize to 0-1
    # Only score when cascade detected (ratio >= 1 means lt >= 3*ma)
    cascade = ratio >= 1.0
    score_val = magnitude * weight
    s += np.where(cascade & (ln > 0), score_val, 0)
    s += np.where(cascade & (ln < 0), -score_val, 0)
    return s


def score_tick_liq_magnitude(df, weight=3.0):
    """P2: Linear magnitude tick liq scoring (smooth instead of binary)."""
    s = pd.Series(0.0, index=df.index)
    if "liq_net_ma" not in df.columns:
        return s
    ln = df["liq_net_ma"].fillna(0)
    # Linear scale: ±weight at ln=±5, capped
    s += np.clip(ln / 5.0, -1, 1) * weight
    # Keep notional spike component (also proportional)
    lt = df["liq_notional_ma"].fillna(0)
    lt_mean = lt[lt > 0].mean() if (lt > 0).any() else 1
    ratio = (lt / (lt_mean * 3).clip(min=1e-6)).clip(upper=2.0)
    s += np.where(lt > lt_mean * 3, ratio / 2.0 * weight * 0.5, 0)
    return s


def rebuild_tick_liq_features(db_data, window=16):
    """Rebuild tick liq features with custom rolling window."""
    if "tick_liq" not in db_data or len(db_data["tick_liq"]) == 0:
        return None
    tliq = db_data["tick_liq"].copy()
    tliq["is_sell"] = (tliq["side"] == "SELL").astype(float)
    tliq["is_buy"] = (tliq["side"] == "BUY").astype(float)
    agg = tliq.set_index("ts").resample("15min").agg({
        "notional_usd": "sum", "is_sell": "sum", "is_buy": "sum",
    }).fillna(0).reset_index()
    agg.columns = ["ts", "liq_notional", "liq_long_count", "liq_short_count"]
    agg["liq_net_count"] = agg["liq_short_count"] - agg["liq_long_count"]
    agg["liq_notional_ma"] = agg["liq_notional"].rolling(window).mean()
    agg["liq_net_ma"] = agg["liq_net_count"].rolling(window).mean()
    return agg


def rebuild_dual_tick_liq_features(db_data, fast_window=4, slow_window=16):
    """P4-Dual: Build both fast and slow tick liq features."""
    if "tick_liq" not in db_data or len(db_data["tick_liq"]) == 0:
        return None
    tliq = db_data["tick_liq"].copy()
    tliq["is_sell"] = (tliq["side"] == "SELL").astype(float)
    tliq["is_buy"] = (tliq["side"] == "BUY").astype(float)
    agg = tliq.set_index("ts").resample("15min").agg({
        "notional_usd": "sum", "is_sell": "sum", "is_buy": "sum",
    }).fillna(0).reset_index()
    agg.columns = ["ts", "liq_notional", "liq_long_count", "liq_short_count"]
    agg["liq_net_count"] = agg["liq_short_count"] - agg["liq_long_count"]
    # Slow (original)
    agg["liq_notional_ma"] = agg["liq_notional"].rolling(slow_window).mean()
    agg["liq_net_ma"] = agg["liq_net_count"].rolling(slow_window).mean()
    # Fast
    agg["liq_notional_ma_fast"] = agg["liq_notional"].rolling(fast_window).mean()
    agg["liq_net_ma_fast"] = agg["liq_net_count"].rolling(fast_window).mean()
    return agg


def score_tick_liq_dual(df, weight=3.0):
    """P4-Dual: Signal only when BOTH fast and slow windows agree."""
    s = pd.Series(0.0, index=df.index)
    if "liq_net_ma" not in df.columns or "liq_net_ma_fast" not in df.columns:
        return s
    ln_slow = df["liq_net_ma"].fillna(0)
    ln_fast = df["liq_net_ma_fast"].fillna(0)
    lt = df["liq_notional_ma"].fillna(0)
    # Both windows must agree on direction AND exceed threshold
    both_bull = (ln_slow > 2) & (ln_fast > 1)
    both_bear = (ln_slow < -2) & (ln_fast < -1)
    s += np.where(both_bull, weight, 0)
    s += np.where(both_bear, -weight, 0)
    # Notional spike (same as original)
    lt_mean = lt[lt > 0].mean() if (lt > 0).any() else 1
    s += np.where(lt > lt_mean * 3, weight * 0.5, 0)
    return s


# ═══════════════════════════════════════════════════════════════════
# CUSTOM COMPOSITE SCORE BUILDER
# ═══════════════════════════════════════════════════════════════════

def compute_custom_btc_score(df, params, extra,
                             cascade_fn=None, tick_liq_fn=None):
    """
    Recompute BTC composite score with optional override functions
    for cascade and tick_liq scoring.
    """
    score = pd.Series(0.0, index=df.index)

    # OI divergence
    if "oi_chg" in df.columns:
        oi_chg = df["oi_chg"].fillna(0)
        ret = df["ret"].fillna(0)
        score += np.where((ret > 0.001) & (oi_chg > 0.002), params.get("w_oi_bull", 0.25), 0)
        score += np.where((ret < -0.001) & (oi_chg < -0.002), params.get("w_oi_capit", 0.25), 0)
        score += np.where((ret > 0.001) & (oi_chg < -0.002), -params.get("w_oi_weak", 0.25), 0)
        score += np.where((ret < -0.001) & (oi_chg > 0.002), -params.get("w_oi_bear", 0.25), 0)

    # Funding rate
    if "fr_8h" in df.columns:
        fr = df["fr_8h"].fillna(0)
        score += np.where(fr < -0.0001, params.get("w_fr_neg", 2.0), 0)
        score += np.where(fr > 0.0003, -params.get("w_fr_pos", 2.0), 0)
    elif "last_funding_rate" in df.columns:
        fr = df["last_funding_rate"].fillna(0)
        score += np.where(fr < -0.00005, params.get("w_fr_neg", 2.0), 0)
        score += np.where(fr > 0.0002, -params.get("w_fr_pos", 2.0), 0)

    # Whale alerts
    if "whale_net_ma" in df.columns:
        wn_ma = df["whale_net_ma"].fillna(0)
        score += np.where(wn_ma > 50_000_000, params.get("w_whale_bull", 1.5), 0)
        score += np.where(wn_ma < -50_000_000, -params.get("w_whale_bear", 1.5), 0)

    # Liquidation cascades — use override or original
    if cascade_fn is not None:
        score += cascade_fn(df, weight=params.get("w_liq_bull", V5_LIQ))
    else:
        if "liq_net" in df.columns and "liq_total_ma" in df.columns:
            lt = df["liq_total"].fillna(0)
            lt_ma = df["liq_total_ma"].fillna(1)
            ln = df["liq_net"].fillna(0)
            cascade = lt > (lt_ma * 3)
            score += np.where(cascade & (ln > 0), params.get("w_liq_bull", V5_LIQ), 0)
            score += np.where(cascade & (ln < 0), -params.get("w_liq_bear", V5_LIQ), 0)

    # ETF flows
    if "etf_flow_ma" in df.columns:
        etf_ma = df["etf_flow_ma"].fillna(0)
        score += np.where(etf_ma > 50, params.get("w_etf_bull", 1.0), 0)
        score += np.where(etf_ma < -50, -params.get("w_etf_bear", 1.0), 0)

    # v3 new factors
    score += score_basis_contrarian(df, weight=extra.get("basis_contrarian", V5_BASIS))
    score += score_ob_combined(df, weight=extra.get("ob_combined", V5_OB))

    # Tick liq — use override or original
    if tick_liq_fn is not None:
        score += tick_liq_fn(df, weight=extra.get("tick_liq", V5_TICK))
    else:
        score += score_tick_liq(df, weight=extra.get("tick_liq", V5_TICK))

    return score


# ═══════════════════════════════════════════════════════════════════
# EXPERIMENT RUNNER
# ═══════════════════════════════════════════════════════════════════

def run_experiment(exp_name, btc_df, score_ts, coins, alt_cache):
    """Run a single experiment across all coins, return results dict."""
    results = []
    total_pnl = 0
    total_trades = 0
    total_wins = 0

    for coin in coins:
        symbol = f"{coin}USDT"
        if coin not in alt_cache:
            continue
        alt_df = alt_cache[coin]

        sig, am = generate_btc_led_signal(
            score_ts, alt_df, threshold=V5_THRESHOLD,
            use_alt_pa_filter=False, spike_mode=None
        )
        mask = (am["ts"] >= pd.Timestamp(OOS_START)) & (am["ts"] <= pd.Timestamp(OOS_END))
        ao = am[mask].reset_index(drop=True)
        so = sig[mask].reset_index(drop=True)

        if len(ao) < 50:
            results.append({"coin": coin, "trades": 0, "wr": 0, "pnl": 0, "sharpe": 0, "error": "too few bars"})
            continue

        trades = run_backtest(
            ao, so,
            sl_atr_mult=V5_SL, tp_atr_mult=V5_TP,
            trail_atr_mult=99, trail_activate_atr=99,
            max_hold_bars=V5_MAX_HOLD, cooldown_bars=V5_CD,
        )

        if len(trades) < 3:
            results.append({"coin": coin, "trades": len(trades), "wr": 0, "pnl": 0, "sharpe": 0, "error": "too few trades"})
            continue

        n = len(trades)
        pnl = trades["pnl_net"].sum()
        wr = 100 * (trades["pnl_net"] > 0).sum() / n
        ret = trades["pnl_net"] / 1000
        sh = ret.mean() / ret.std() * np.sqrt(n) if ret.std() > 0 else 0

        results.append({
            "coin": coin, "trades": n,
            "wr": round(wr, 1), "pnl": round(pnl, 2),
            "sharpe": round(sh, 2),
        })
        total_pnl += pnl
        total_trades += n
        total_wins += (trades["pnl_net"] > 0).sum()

    total_wr = round(100 * total_wins / total_trades, 1) if total_trades > 0 else 0

    return {
        "name": exp_name,
        "coins": results,
        "total_trades": total_trades,
        "total_wr": total_wr,
        "total_pnl": round(total_pnl, 2),
    }


def replace_tick_liq_in_btc_df(btc_df_base, new_tick_agg):
    """Replace tick liq columns in btc_df with new aggregation."""
    df = btc_df_base.copy()
    # Drop old tick liq columns if present
    for col in ["liq_net_ma", "liq_notional_ma", "liq_net_ma_fast", "liq_notional_ma_fast"]:
        if col in df.columns:
            df = df.drop(columns=[col])
    # Merge new
    merge_cols = ["ts"] + [c for c in new_tick_agg.columns if c != "ts" and c not in
                  ["liq_notional", "liq_long_count", "liq_short_count", "liq_net_count"]]
    df = pd.merge_asof(df.sort_values("ts"),
                       new_tick_agg[merge_cols].sort_values("ts"),
                       on="ts", direction="backward", tolerance=pd.Timedelta("30min"))
    return df.sort_values("ts").reset_index(drop=True)


# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 70, flush=True)
    print("  LIQUIDATION SCORING IMPROVEMENTS: P2 (Magnitude) + P4 (Velocity)")
    print(f"  Coins: {', '.join(SAMPLE_COINS)}")
    print(f"  OOS: {OOS_START} to {OOS_END}")
    print(f"  V5 config: liq={V5_LIQ}, ob={V5_OB}, tick={V5_TICK}, SL={V5_SL}, TP={V5_TP}")
    print("=" * 70, flush=True)

    t0 = time.time()

    # ── Load BTC data once ──
    print("\n[1/4] Loading BTC data...")
    btc_ohlcv = fetch_binance_15m("BTCUSDT", years=3)
    db_data = load_btc_db_data()

    # ── Pre-load altcoin data ──
    print("\n[2/4] Loading altcoin data...")
    alt_cache = {}
    for coin in SAMPLE_COINS:
        symbol = f"{coin}USDT"
        try:
            alt_ohlcv = fetch_binance_15m(symbol, years=3)
            alt_cache[coin] = build_alt_technicals(alt_ohlcv)
            print(f"  {coin}: {len(alt_cache[coin]):,} bars")
        except Exception as e:
            print(f"  {coin}: FAILED ({e})")

    # ── V5 params ──
    v5_params = dict(COMPOSITE_WEIGHTS)
    v5_params.update({"w_liq_bull": V5_LIQ, "w_liq_bear": V5_LIQ})
    v5_extra = {"ob_combined": V5_OB, "tick_liq": V5_TICK, "basis_contrarian": V5_BASIS}

    # ── Pre-build BTC features variants ──
    print("\n[3/4] Building BTC feature variants...")

    # Base BTC features (16-bar window = original)
    btc_df_16 = build_btc_features(btc_ohlcv, db_data)

    # 8-bar window variant
    tick_agg_8 = rebuild_tick_liq_features(db_data, window=8)
    btc_df_8 = replace_tick_liq_in_btc_df(btc_df_16, tick_agg_8) if tick_agg_8 is not None else btc_df_16
    print("  8-bar tick liq features built")

    # 4-bar window variant
    tick_agg_4 = rebuild_tick_liq_features(db_data, window=4)
    btc_df_4 = replace_tick_liq_in_btc_df(btc_df_16, tick_agg_4) if tick_agg_4 is not None else btc_df_16
    print("  4-bar tick liq features built")

    # Dual window variant (4-bar fast + 16-bar slow)
    tick_agg_dual = rebuild_dual_tick_liq_features(db_data, fast_window=4, slow_window=16)
    btc_df_dual = replace_tick_liq_in_btc_df(btc_df_16, tick_agg_dual) if tick_agg_dual is not None else btc_df_16
    print("  Dual (4+16) tick liq features built")

    # ═══════════════════════════════════════════════════════════════
    # RUN 8 EXPERIMENTS
    # ═══════════════════════════════════════════════════════════════

    print("\n[4/4] Running 8 experiments...\n")
    all_results = []

    # Helper to build score_ts from btc_df and scoring functions
    def make_score_ts(btc_df, cascade_fn=None, tick_liq_fn=None):
        score = compute_custom_btc_score(btc_df, v5_params, v5_extra,
                                         cascade_fn=cascade_fn, tick_liq_fn=tick_liq_fn)
        return pd.Series(score.values, index=btc_df["ts"].values)

    # ── EXP 1: Baseline (v5 as-is) ──
    print("-" * 70)
    print("EXP 1: Baseline (v5 config as-is, binary scoring, 16-bar window)")
    score_ts_1 = make_score_ts(btc_df_16)
    r1 = run_experiment("Baseline", btc_df_16, score_ts_1, SAMPLE_COINS, alt_cache)
    for c in r1["coins"]:
        print(f"  {c['coin']:<12} {c['trades']:>4} trades | WR {c['wr']:>5.1f}% | ${c['pnl']:>+10,.2f} | Sharpe {c.get('sharpe',0):>6.2f}")
    print(f"  {'TOTAL':<12} {r1['total_trades']:>4} trades | WR {r1['total_wr']:>5.1f}% | ${r1['total_pnl']:>+10,.2f}")
    all_results.append(r1)

    # ── EXP 2: P2 Magnitude cascade only ──
    print("\n" + "─" * 70)
    print("EXP 2: P2 Magnitude cascade (proportional 0→weight)")
    score_ts_2 = make_score_ts(btc_df_16, cascade_fn=score_cascade_magnitude)
    r2 = run_experiment("P2: Mag cascade", btc_df_16, score_ts_2, SAMPLE_COINS, alt_cache)
    for c in r2["coins"]:
        print(f"  {c['coin']:<12} {c['trades']:>4} trades | WR {c['wr']:>5.1f}% | ${c['pnl']:>+10,.2f} | Sharpe {c.get('sharpe',0):>6.2f}")
    print(f"  {'TOTAL':<12} {r2['total_trades']:>4} trades | WR {r2['total_wr']:>5.1f}% | ${r2['total_pnl']:>+10,.2f}")
    all_results.append(r2)

    # ── EXP 3: P2 Magnitude tick_liq only ──
    print("\n" + "─" * 70)
    print("EXP 3: P2 Magnitude tick_liq (linear scale)")
    score_ts_3 = make_score_ts(btc_df_16, tick_liq_fn=score_tick_liq_magnitude)
    r3 = run_experiment("P2: Mag tick_liq", btc_df_16, score_ts_3, SAMPLE_COINS, alt_cache)
    for c in r3["coins"]:
        print(f"  {c['coin']:<12} {c['trades']:>4} trades | WR {c['wr']:>5.1f}% | ${c['pnl']:>+10,.2f} | Sharpe {c.get('sharpe',0):>6.2f}")
    print(f"  {'TOTAL':<12} {r3['total_trades']:>4} trades | WR {r3['total_wr']:>5.1f}% | ${r3['total_pnl']:>+10,.2f}")
    all_results.append(r3)

    # ── EXP 4: P2 Both magnitude ──
    print("\n" + "─" * 70)
    print("EXP 4: P2 Both magnitude (cascade + tick_liq)")
    score_ts_4 = make_score_ts(btc_df_16, cascade_fn=score_cascade_magnitude,
                               tick_liq_fn=score_tick_liq_magnitude)
    r4 = run_experiment("P2: Both mag", btc_df_16, score_ts_4, SAMPLE_COINS, alt_cache)
    for c in r4["coins"]:
        print(f"  {c['coin']:<12} {c['trades']:>4} trades | WR {c['wr']:>5.1f}% | ${c['pnl']:>+10,.2f} | Sharpe {c.get('sharpe',0):>6.2f}")
    print(f"  {'TOTAL':<12} {r4['total_trades']:>4} trades | WR {r4['total_wr']:>5.1f}% | ${r4['total_pnl']:>+10,.2f}")
    all_results.append(r4)

    # ── EXP 5: P4 8-bar (2h) window ──
    print("\n" + "─" * 70)
    print("EXP 5: P4 8-bar window (2h instead of 4h)")
    score_ts_5 = make_score_ts(btc_df_8)
    r5 = run_experiment("P4: 8-bar (2h)", btc_df_8, score_ts_5, SAMPLE_COINS, alt_cache)
    for c in r5["coins"]:
        print(f"  {c['coin']:<12} {c['trades']:>4} trades | WR {c['wr']:>5.1f}% | ${c['pnl']:>+10,.2f} | Sharpe {c.get('sharpe',0):>6.2f}")
    print(f"  {'TOTAL':<12} {r5['total_trades']:>4} trades | WR {r5['total_wr']:>5.1f}% | ${r5['total_pnl']:>+10,.2f}")
    all_results.append(r5)

    # ── EXP 6: P4 4-bar (1h) window ──
    print("\n" + "─" * 70)
    print("EXP 6: P4 4-bar window (1h instead of 4h)")
    score_ts_6 = make_score_ts(btc_df_4)
    r6 = run_experiment("P4: 4-bar (1h)", btc_df_4, score_ts_6, SAMPLE_COINS, alt_cache)
    for c in r6["coins"]:
        print(f"  {c['coin']:<12} {c['trades']:>4} trades | WR {c['wr']:>5.1f}% | ${c['pnl']:>+10,.2f} | Sharpe {c.get('sharpe',0):>6.2f}")
    print(f"  {'TOTAL':<12} {r6['total_trades']:>4} trades | WR {r6['total_wr']:>5.1f}% | ${r6['total_pnl']:>+10,.2f}")
    all_results.append(r6)

    # ── EXP 7: P4 Dual window (4+16 bar) ──
    print("\n" + "─" * 70)
    print("EXP 7: P4 Dual window (fast 4-bar + slow 16-bar, both must agree)")
    score_ts_7 = make_score_ts(btc_df_dual, tick_liq_fn=score_tick_liq_dual)
    r7 = run_experiment("P4: Dual (4+16)", btc_df_dual, score_ts_7, SAMPLE_COINS, alt_cache)
    for c in r7["coins"]:
        print(f"  {c['coin']:<12} {c['trades']:>4} trades | WR {c['wr']:>5.1f}% | ${c['pnl']:>+10,.2f} | Sharpe {c.get('sharpe',0):>6.2f}")
    print(f"  {'TOTAL':<12} {r7['total_trades']:>4} trades | WR {r7['total_wr']:>5.1f}% | ${r7['total_pnl']:>+10,.2f}")
    all_results.append(r7)

    # ── EXP 8: Best P2 + Best P4 combined ──
    # Determine best P2 and best P4 from results so far
    p2_exps = [r2, r3, r4]
    best_p2 = max(p2_exps, key=lambda x: x["total_pnl"])
    p4_exps = [r5, r6, r7]
    best_p4 = max(p4_exps, key=lambda x: x["total_pnl"])

    # Determine which functions to combine
    best_p2_cascade_fn = None
    best_p2_tick_fn = None
    if best_p2["name"] in ("P2: Mag cascade", "P2: Both mag"):
        best_p2_cascade_fn = score_cascade_magnitude
    if best_p2["name"] in ("P2: Mag tick_liq", "P2: Both mag"):
        best_p2_tick_fn = score_tick_liq_magnitude

    # Determine which btc_df to use for P4
    best_p4_btc_df = btc_df_16
    if best_p4["name"] == "P4: 8-bar (2h)":
        best_p4_btc_df = btc_df_8
    elif best_p4["name"] == "P4: 4-bar (1h)":
        best_p4_btc_df = btc_df_4
    elif best_p4["name"] == "P4: Dual (4+16)":
        best_p4_btc_df = btc_df_dual
        # If best P4 is dual AND best P2 tick is magnitude, use dual tick fn
        if best_p2_tick_fn is not None:
            best_p2_tick_fn = score_tick_liq_magnitude  # magnitude on dual features

    # For dual window, if P2 tick_liq wasn't best, use the dual scoring fn
    combo_tick_fn = best_p2_tick_fn
    if best_p4["name"] == "P4: Dual (4+16)" and combo_tick_fn is None:
        combo_tick_fn = score_tick_liq_dual

    print("\n" + "─" * 70)
    print(f"EXP 8: Best combo = [{best_p2['name']}] + [{best_p4['name']}]")
    score_ts_8 = make_score_ts(best_p4_btc_df, cascade_fn=best_p2_cascade_fn,
                               tick_liq_fn=combo_tick_fn)
    r8 = run_experiment(f"Best: {best_p2['name']} + {best_p4['name']}",
                        best_p4_btc_df, score_ts_8, SAMPLE_COINS, alt_cache)
    for c in r8["coins"]:
        print(f"  {c['coin']:<12} {c['trades']:>4} trades | WR {c['wr']:>5.1f}% | ${c['pnl']:>+10,.2f} | Sharpe {c.get('sharpe',0):>6.2f}")
    print(f"  {'TOTAL':<12} {r8['total_trades']:>4} trades | WR {r8['total_wr']:>5.1f}% | ${r8['total_pnl']:>+10,.2f}")
    all_results.append(r8)

    # ═══════════════════════════════════════════════════════════════
    # COMPARISON TABLE
    # ═══════════════════════════════════════════════════════════════

    baseline_pnl = r1["total_pnl"]

    print("\n" + "=" * 90)
    print(f"{'EXP':>3} | {'Name':<30} | {'Trades':>6} | {'WR%':>6} | {'PnL':>11} | {'vs Baseline':>18}")
    print("-" * 90)
    for i, r in enumerate(all_results, 1):
        diff = r["total_pnl"] - baseline_pnl
        pct = 100 * diff / abs(baseline_pnl) if baseline_pnl != 0 else 0
        vs = f"${diff:>+,.0f} ({pct:>+.1f}%)" if i > 1 else "--"
        print(f"  {i} | {r['name']:<30} | {r['total_trades']:>6} | {r['total_wr']:>5.1f}% | ${r['total_pnl']:>+10,.2f} | {vs:>18}")
    print("=" * 90)

    # ═══════════════════════════════════════════════════════════════
    # CONCLUSION
    # ═══════════════════════════════════════════════════════════════

    best_p2_final = max([r2, r3, r4], key=lambda x: x["total_pnl"])
    best_p4_final = max([r5, r6, r7], key=lambda x: x["total_pnl"])
    best_overall = max(all_results[1:], key=lambda x: x["total_pnl"])  # exclude baseline

    def fmt_vs(r):
        d = r["total_pnl"] - baseline_pnl
        p = 100 * d / abs(baseline_pnl) if baseline_pnl != 0 else 0
        return f"${d:>+,.0f} ({p:>+.1f}%)"

    print(f"\n{'='*70}")
    print("CONCLUSION")
    print(f"  Best P2 (Magnitude): {best_p2_final['name']} {fmt_vs(best_p2_final)}")
    print(f"  Best P4 (Velocity):  {best_p4_final['name']} {fmt_vs(best_p4_final)}")
    print(f"  Best overall:        {best_overall['name']} {fmt_vs(best_overall)}")
    beat = best_overall["total_pnl"] > baseline_pnl
    print(f"\n  Verdict: {'IMPROVEMENT FOUND' if beat else 'BASELINE WINS'}")
    if beat:
        print(f"  >> {best_overall['name']} beats baseline by {fmt_vs(best_overall)}")
    else:
        print(f"  >> Binary scoring + 16-bar window remains optimal")
    print(f"{'='*70}")

    # ═══════════════════════════════════════════════════════════════
    # SAVE RESULTS
    # ═══════════════════════════════════════════════════════════════

    ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    output = {
        "experiment": "liq_scoring_improvements_p2_p4",
        "timestamp": ts_str,
        "config": {
            "v5_liq": V5_LIQ, "v5_ob": V5_OB, "v5_tick": V5_TICK,
            "v5_sl": V5_SL, "v5_tp": V5_TP, "v5_threshold": V5_THRESHOLD,
            "oos": f"{OOS_START} to {OOS_END}",
            "coins": SAMPLE_COINS,
        },
        "results": [],
        "conclusion": {
            "best_p2": best_p2_final["name"],
            "best_p2_pnl": best_p2_final["total_pnl"],
            "best_p4": best_p4_final["name"],
            "best_p4_pnl": best_p4_final["total_pnl"],
            "best_overall": best_overall["name"],
            "best_overall_pnl": best_overall["total_pnl"],
            "baseline_pnl": baseline_pnl,
            "improvement": beat,
        },
        "elapsed_seconds": round(time.time() - t0, 1),
    }

    for i, r in enumerate(all_results, 1):
        diff = r["total_pnl"] - baseline_pnl
        pct = 100 * diff / abs(baseline_pnl) if baseline_pnl != 0 else 0
        output["results"].append({
            "exp": i,
            "name": r["name"],
            "total_trades": r["total_trades"],
            "total_wr": r["total_wr"],
            "total_pnl": r["total_pnl"],
            "vs_baseline_pnl": round(diff, 2),
            "vs_baseline_pct": round(pct, 1),
            "coins": r["coins"],
        })

    date_str = datetime.now().strftime("%Y%m%d")
    outpath = Path("experiments") / f"liq_improvements_{date_str}.json"
    outpath.parent.mkdir(exist_ok=True)
    outpath.write_text(json.dumps(output, indent=2, default=str, ensure_ascii=False))
    print(f"\nResults saved to {outpath}")
    print(f"Total time: {time.time()-t0:.1f}s")
