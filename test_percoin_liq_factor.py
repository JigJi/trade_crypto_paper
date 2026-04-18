"""
test_percoin_liq_factor.py — Per-Coin Liquidation Factor Test
=============================================================
Hypothesis: altcoin-specific liquidation cascades are a better signal
than BTC-only liquidation for that specific altcoin.

Architecture: per-coin score boost on top of BTC composite score.
Each coin gets its own modified score based on its own liq data.

Data: market_data.liquidation (tick-level, ~Sep 2025 - present)
"""

import os
import json
import numpy as np
import pandas as pd
import psycopg2
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# ── imports from existing codebase ──────────────────────────────────
from backtest_15m_btc_led_alts import (
    fetch_binance_15m,
    build_btc_features,
    compute_btc_composite_score,
    build_alt_technicals,
    run_backtest,
    calc_metrics,
    BKK_UTC_OFFSET,
    DB_PARAMS,
)
from test_v12_improvements import (
    V11_CONFIGS,
    generate_signal_short_bias,
    ALL_COINS,
)

# ── constants ───────────────────────────────────────────────────────
SHORT_OFFSET = 0.5
TEST_START = pd.Timestamp("2026-01-01")
MIN_OOS_EVENTS = 20            # skip coins with < 20 liquidation events in OOS period
WEIGHTS_TO_TEST = [0.5, 1.0, 1.5, 2.0]

# BTC per-coin liq is redundant (already in composite via score_tick_liq)
# Focus on altcoins only
ALT_COINS = [c for c in ALL_COINS if c != "BTC"]


# ═══════════════════════════════════════════════════════════════════
# [B] Load per-coin tick liquidation from DB
# ═══════════════════════════════════════════════════════════════════

def load_coin_tick_liq(conn, coin):
    """
    Load tick-level liquidation for a specific coin from DB.
    Returns 15min resampled DataFrame with features.
    """
    symbol = f"{coin}USDT"
    sql = f"""
        SELECT event_time, side, notional_usd
        FROM market_data.liquidation
        WHERE symbol = '{symbol}'
        ORDER BY event_time
    """
    df = pd.read_sql(sql, conn)

    if df.empty:
        print(f"  {coin}: NO liquidation data found")
        return pd.DataFrame()

    # TZ fix: event_time is stored with TZ (UTC+7) to convert to naive UTC
    df["event_time"] = pd.to_datetime(df["event_time"], utc=True).dt.tz_localize(None)
    df = df.sort_values("event_time").reset_index(drop=True)

    # Convert notional to float
    df["notional_usd"] = df["notional_usd"].astype(float)

    print(f"  {coin}: {len(df):,} events, "
          f"{df['event_time'].min().date()} to {df['event_time'].max().date()}")

    # ── Resample to 15min ──────────────────────────────────────────
    df["is_long_liq"] = (df["side"] == "SELL").astype(int)   # SELL = long liq
    df["is_short_liq"] = (df["side"] == "BUY").astype(int)   # BUY = short liq

    df = df.set_index("event_time")

    resampled = pd.DataFrame()
    resampled["cl_notional"] = df["notional_usd"].resample("15min").sum()
    resampled["cl_long_count"] = df["is_long_liq"].resample("15min").sum()
    resampled["cl_short_count"] = df["is_short_liq"].resample("15min").sum()
    resampled["cl_net_count"] = resampled["cl_short_count"] - resampled["cl_long_count"]
    resampled = resampled.fillna(0)

    # ── Rolling features ───────────────────────────────────────────
    # 4h = 16 bars, 24h = 96 bars
    resampled["cl_notional_ma"] = resampled["cl_notional"].rolling(16, min_periods=1).mean()
    resampled["cl_net_ma"] = resampled["cl_net_count"].rolling(16, min_periods=1).mean()
    resampled["cl_notional_ma_slow"] = resampled["cl_notional"].rolling(96, min_periods=1).mean()

    # Z-score (24h window)
    roll_mean = resampled["cl_notional"].rolling(96, min_periods=16).mean()
    roll_std = resampled["cl_notional"].rolling(96, min_periods=16).std().clip(lower=1e-8)
    resampled["cl_z"] = (resampled["cl_notional"] - roll_mean) / roll_std

    # Spike detection
    resampled["cl_spike"] = (
        resampled["cl_notional"] > resampled["cl_notional_ma_slow"] * 3
    ).astype(int)
    resampled["cl_spike_4h"] = resampled["cl_spike"].rolling(16, min_periods=1).sum()

    # Reset index to "ts" column for merge
    resampled = resampled.reset_index().rename(columns={"event_time": "ts"})

    # Stats
    n_spikes = resampled["cl_spike"].sum()
    days = (resampled["ts"].max() - resampled["ts"].min()).days or 1
    events_per_day = len(df) / days
    print(f"         {len(resampled):,} bars (15m), {n_spikes:.0f} spikes, "
          f"{events_per_day:.1f} events/day")

    return resampled


# ═══════════════════════════════════════════════════════════════════
# [C] Score functions — 3 variants
# ═══════════════════════════════════════════════════════════════════

def score_percoin_liq_contrarian(df, weight=1.0):
    """
    Contrarian: cascade + net short liq = bullish (shorts getting rekt to price up).
    cascade + net long liq = bearish (longs getting rekt to price down).
    Same logic as BTC score_tick_liq but with per-coin data.
    """
    s = pd.Series(0.0, index=df.index)
    if "cl_net_ma" not in df.columns:
        return s

    net = df["cl_net_ma"].fillna(0)
    notional = df["cl_notional_ma"].fillna(0)

    # Net direction: short liqs > long liqs = bullish
    s += np.where(net > 0.5, weight, 0)
    s += np.where(net < -0.5, -weight, 0)

    # Big cascade spike = extra signal
    notional_mean = notional[notional > 0].mean() if (notional > 0).any() else 1
    s += np.where(notional > notional_mean * 3, weight * 0.5, 0)

    return s


def score_percoin_liq_momentum(df, weight=1.0):
    """
    Momentum: any cascade = bearish (more selling/liquidation coming).
    Spike = panic to avoid entry or go short.
    """
    s = pd.Series(0.0, index=df.index)
    if "cl_spike_4h" not in df.columns:
        return s

    spike_4h = df["cl_spike_4h"].fillna(0)
    net = df["cl_net_ma"].fillna(0)

    # Active cascade = bearish (momentum of liquidation)
    s += np.where(spike_4h >= 2, -weight, 0)
    s += np.where(spike_4h >= 4, -weight * 0.5, 0)  # extra bearish

    # Net long liqs during cascade = extra bearish
    s += np.where((spike_4h >= 1) & (net < -0.5), -weight * 0.5, 0)

    return s


def score_percoin_liq_zscore(df, weight=1.0):
    """
    Z-score based: extreme liq activity = contrarian signal.
    z > 2 = unusual activity to contrarian bullish.
    z < -1 = quiet period to slight bullish (calm = accumulation).
    """
    s = pd.Series(0.0, index=df.index)
    if "cl_z" not in df.columns:
        return s

    z = df["cl_z"].fillna(0)
    net = df["cl_net_ma"].fillna(0)

    # Extreme activity: contrarian
    s += np.where((z > 2) & (net > 0), weight, 0)       # shorts rekt hard to bullish
    s += np.where((z > 2) & (net < 0), -weight, 0)      # longs rekt hard to bearish
    s += np.where(z > 3, weight * 0.5, 0)               # extreme = extra contrarian bullish

    # Quiet period = slight bullish (calm before pump?)
    s += np.where(z < -0.5, weight * 0.3, 0)

    return s


SCORE_VARIANTS = {
    "contrarian": score_percoin_liq_contrarian,
    "momentum": score_percoin_liq_momentum,
    "zscore": score_percoin_liq_zscore,
}


# ═══════════════════════════════════════════════════════════════════
# [D] Data quality analysis
# ═══════════════════════════════════════════════════════════════════

def analyze_coin_liq(coin, liq_df, alt_df):
    """Analyze per-coin liq data quality and correlation with returns."""
    if liq_df.empty:
        return {"oos_events": 0, "oos_bars_active": 0, "events_per_day": 0,
                "n_spikes": 0, "corr_1h": 0, "corr_4h": 0}

    # OOS period
    oos_start = TEST_START
    oos_end = alt_df["ts"].max()
    oos_liq = liq_df[(liq_df["ts"] >= oos_start) & (liq_df["ts"] <= oos_end)]

    # Count bars with actual events (cl_notional > 0)
    oos_bars_active = int((oos_liq["cl_notional"] > 0).sum())

    # Total events in OOS (approximate from active bars)
    oos_events = oos_bars_active

    # Events per day (overall)
    days = max((liq_df["ts"].max() - liq_df["ts"].min()).days, 1)
    total_active_bars = int(liq_df["cl_notional"].gt(0).sum())
    events_per_day = total_active_bars / days

    # Spikes in OOS
    n_spikes = int(oos_liq["cl_spike"].sum()) if "cl_spike" in oos_liq.columns else 0

    # Correlation with forward returns
    corr_1h = 0
    corr_4h = 0
    if oos_bars_active > 10 and len(alt_df) > 10:
        merged = pd.merge_asof(
            alt_df.sort_values("ts"),
            oos_liq[["ts", "cl_notional", "cl_net_count"]].sort_values("ts"),
            on="ts", direction="backward", tolerance=pd.Timedelta("30min")
        )
        merged["fwd_ret_1h"] = merged["close"].pct_change(4).shift(-4)
        merged["fwd_ret_4h"] = merged["close"].pct_change(16).shift(-16)

        # Only correlate on bars that actually have liq events
        valid = merged[merged["cl_notional"].fillna(0) > 0].dropna(
            subset=["fwd_ret_1h"])
        if len(valid) > 10:
            corr_1h = valid["cl_notional"].corr(valid["fwd_ret_1h"])
            corr_4h = valid["cl_notional"].corr(valid["fwd_ret_4h"])

    return {
        "oos_events": oos_events,
        "oos_bars_active": oos_bars_active,
        "events_per_day": round(events_per_day, 2),
        "n_spikes": n_spikes,
        "corr_1h": round(corr_1h, 4) if not np.isnan(corr_1h) else 0,
        "corr_4h": round(corr_4h, 4) if not np.isnan(corr_4h) else 0,
    }


# ═══════════════════════════════════════════════════════════════════
# [E] Run scenario (per existing pattern)
# ═══════════════════════════════════════════════════════════════════

def run_scenario(label, per_coin_scores, alt_data):
    """
    Run backtest for all coins using per-coin modified scores.
    per_coin_scores: dict {coin: pd.Series(score, index=ts)}
    """
    print(f"\n{'-'*65}")
    print(f"  {label}")
    print(f"{'-'*65}")
    print(f"  {'Coin':5s} {'#Tr':>4s} {'WR%':>6s} {'PF':>7s} {'Sharpe':>7s} "
          f"{'PnL':>11s} {'MaxDD':>7s}")
    print(f"  {'-'*5} {'-'*4} {'-'*6} {'-'*7} {'-'*7} {'-'*11} {'-'*7}")

    total_pnl = 0
    all_trades = []
    coin_metrics = {}

    for coin in ALL_COINS:
        if coin not in alt_data:
            continue
        if coin not in per_coin_scores:
            continue

        cfg = V11_CONFIGS[coin]
        score_ts = per_coin_scores[coin]

        signals, alt_m = generate_signal_short_bias(
            score_ts, alt_data[coin], cfg["threshold"], cfg["alt_pa"],
            short_offset=SHORT_OFFSET)
        trades = run_backtest(
            alt_m, signals,
            sl_atr_mult=cfg["sl"], tp_atr_mult=cfg["tp"],
            trail_atr_mult=cfg["trail"], trail_activate_atr=cfg["trail_act"],
            cooldown_bars=cfg["cd"])
        m = calc_metrics(trades, len(alt_data[coin]))
        coin_metrics[coin] = m
        total_pnl += m["net_pnl"]

        if not trades.empty:
            trades["coin"] = coin
            all_trades.append(trades)

        print(f"  {coin:5s} {m['total']:4d} {m['win_rate']:6.1f} {m['pf']:7.3f} "
              f"{m['sharpe']:7.3f} ${m['net_pnl']:>+10,.2f} {m['max_dd']:>6.2f}%")

    print(f"  {'-'*5} {'-'*4} {'-'*6} {'-'*7} {'-'*7} {'-'*11} {'-'*7}")
    print(f"  {'TOTAL':5s} {'':4s} {'':6s} {'':7s} {'':7s} ${total_pnl:>+10,.2f}")

    all_tr = pd.concat(all_trades) if all_trades else pd.DataFrame()
    n_trades = len(all_tr)
    wr = (all_tr["pnl_net"] > 0).mean() * 100 if n_trades > 0 else 0

    return {"total_pnl": total_pnl, "coins": coin_metrics,
            "n_trades": n_trades, "wr": wr, "trades_df": all_tr}


# ═══════════════════════════════════════════════════════════════════
# [F] Main
# ═══════════════════════════════════════════════════════════════════

def load_btc_db_data():
    """Load BTC DB data (same as existing pattern)."""
    from backtest_15m_btc_led_alts import load_btc_db_data as _load
    return _load()


def main():
    print("=" * 70)
    print("  PER-COIN LIQUIDATION FACTOR TEST")
    print("  Hypothesis: altcoin-specific liq cascades improve per-coin signal")
    print("=" * 70)

    # ── 1. Load BTC baseline ───────────────────────────────────────
    print("\n[1/7] Loading BTC data + computing v3 composite score...")
    btc_ohlcv = fetch_binance_15m("BTCUSDT", years=3)
    btc_db = load_btc_db_data()
    btc_df = build_btc_features(btc_ohlcv, btc_db)
    btc_score = compute_btc_composite_score(btc_df)

    # OOS filter
    btc_period_start = pd.Timestamp("2025-06-01")
    mask = btc_df["ts"] >= btc_period_start
    btc_df_oos = btc_df[mask].reset_index(drop=True)
    btc_score_oos = btc_score[mask].reset_index(drop=True)
    btc_score_ts = pd.Series(btc_score_oos.values, index=btc_df_oos["ts"].values)
    btc_period_end = btc_df_oos["ts"].iloc[-1]
    print(f"  BTC OOS: {btc_period_start.date()} to {btc_period_end.date()}, "
          f"{len(btc_df_oos):,} bars")

    # ── 2. Load per-coin liquidation from DB ───────────────────────
    print("\n[2/7] Loading per-coin tick liquidation from DB...")
    conn = psycopg2.connect(**DB_PARAMS)

    per_coin_liq = {}
    for coin in ALL_COINS:
        liq_df = load_coin_tick_liq(conn, coin)
        if not liq_df.empty:
            per_coin_liq[coin] = liq_df

    conn.close()
    print(f"\n  Loaded liq data for {len(per_coin_liq)} coins: "
          f"{list(per_coin_liq.keys())}")

    # ── 3. Load altcoin OHLCV ──────────────────────────────────────
    print("\n[3/7] Loading altcoin OHLCV...")
    alt_data = {}
    for coin in ALL_COINS:
        symbol = f"{coin}USDT"
        ohlcv = fetch_binance_15m(symbol, years=3)
        alt_df = build_alt_technicals(ohlcv)
        alt_test = alt_df[
            (alt_df["ts"] >= TEST_START) & (alt_df["ts"] <= btc_period_end)
        ].reset_index(drop=True)
        if len(alt_test) >= 100:
            alt_data[coin] = alt_test
            print(f"  {coin}: {len(alt_test):,} bars")
        else:
            print(f"  {coin}: SKIPPED (only {len(alt_test)} bars)")

    # ── 4. Data quality analysis ───────────────────────────────────
    print("\n[4/7] Data quality analysis...")
    print(f"\n  {'Coin':5s} {'OOSEvt':>7s} {'Evt/Day':>8s} {'Spikes':>7s} "
          f"{'Corr1h':>7s} {'Corr4h':>7s} {'Status':>8s}")
    print(f"  {'-'*5} {'-'*7} {'-'*8} {'-'*7} {'-'*7} {'-'*7} {'-'*8}")

    quality = {}
    eligible_coins = []

    for coin in ALL_COINS:
        if coin not in per_coin_liq or coin not in alt_data:
            print(f"  {coin:5s} {'N/A':>7s} {'N/A':>8s} {'N/A':>7s} "
                  f"{'N/A':>7s} {'N/A':>7s} {'NO DATA':>8s}")
            continue

        q = analyze_coin_liq(coin, per_coin_liq[coin], alt_data[coin])
        quality[coin] = q

        status = "OK" if q["oos_events"] >= MIN_OOS_EVENTS else "SPARSE"
        if q["oos_events"] >= MIN_OOS_EVENTS:
            eligible_coins.append(coin)

        print(f"  {coin:5s} {q['oos_events']:>7d} {q['events_per_day']:>8.2f} "
              f"{q['n_spikes']:>7d} {q['corr_1h']:>+7.4f} {q['corr_4h']:>+7.4f} "
              f"{status:>8s}")

    print(f"\n  Eligible coins (>={MIN_OOS_EVENTS} OOS events): {eligible_coins}")

    if not eligible_coins:
        print("\n  *** No eligible coins! Aborting. ***")
        return

    # ── 5. BASELINE: v3 composite (same score for all coins) ──────
    print("\n[5/7] Running BASELINE (v3 composite, no per-coin liq)...")
    baseline_scores = {coin: btc_score_ts for coin in ALL_COINS}
    baseline = run_scenario("BASELINE: v3 composite only", baseline_scores, alt_data)
    base_pnl = baseline["total_pnl"]

    # ── 6. Test variants × weights ─────────────────────────────────
    print(f"\n[6/7] Testing per-coin liq variants...")
    print(f"  Variants: {list(SCORE_VARIANTS.keys())}")
    print(f"  Weights: {WEIGHTS_TO_TEST}")
    print(f"  Eligible coins: {eligible_coins}")

    all_results = {"BASELINE": baseline}
    best_overall = {"label": "BASELINE", "pnl": base_pnl, "delta": 0}

    for variant_name, score_fn in SCORE_VARIANTS.items():
        for weight in WEIGHTS_TO_TEST:
            label = f"{variant_name}_w{weight}"

            # Build per-coin modified scores
            per_coin_scores = {}
            for coin in ALL_COINS:
                if coin in eligible_coins and coin in per_coin_liq:
                    # Merge per-coin liq features into a temp df aligned to BTC timeline
                    coin_liq = per_coin_liq[coin][["ts"] + [
                        c for c in per_coin_liq[coin].columns
                        if c.startswith("cl_")
                    ]].copy()

                    # Create merged df for scoring
                    btc_ts_df = btc_df_oos[["ts"]].copy()
                    merged = pd.merge_asof(
                        btc_ts_df.sort_values("ts"),
                        coin_liq.sort_values("ts"),
                        on="ts", direction="backward",
                        tolerance=pd.Timedelta("30min")
                    )

                    # Compute per-coin liq score boost
                    coin_liq_score = score_fn(merged, weight=weight)

                    # Modified score = base BTC score + per-coin liq boost
                    modified_score = btc_score_oos + coin_liq_score
                    score_ts = pd.Series(
                        modified_score.values, index=btc_df_oos["ts"].values)
                    per_coin_scores[coin] = score_ts
                else:
                    # No per-coin liq data to use base BTC score unchanged
                    per_coin_scores[coin] = btc_score_ts

            result = run_scenario(label, per_coin_scores, alt_data)
            all_results[label] = result

            delta = result["total_pnl"] - base_pnl
            if result["total_pnl"] > best_overall["pnl"]:
                best_overall = {
                    "label": label, "pnl": result["total_pnl"], "delta": delta
                }

    # ── 7. Summary table ───────────────────────────────────────────
    print("\n" + "=" * 70)
    print("  SUMMARY: Per-Coin Liquidation Factor Results")
    print("=" * 70)
    print(f"\n  {'Scenario':25s} {'#Tr':>5s} {'WR%':>6s} {'PnL':>11s} "
          f"{'Delta':>11s} {'vs Base':>8s}")
    print(f"  {'-'*25} {'-'*5} {'-'*6} {'-'*11} {'-'*11} {'-'*8}")

    for label, r in all_results.items():
        delta = r["total_pnl"] - base_pnl
        pct = delta / abs(base_pnl) * 100 if base_pnl != 0 else 0
        marker = " ***" if label == best_overall["label"] and label != "BASELINE" else ""
        print(f"  {label:25s} {r['n_trades']:5d} {r['wr']:5.1f}% "
              f"${r['total_pnl']:>+10,.2f} ${delta:>+10,.2f} {pct:>+7.1f}%{marker}")

    # ── Per-coin delta breakdown ───────────────────────────────────
    if best_overall["label"] != "BASELINE":
        best_r = all_results[best_overall["label"]]
        print(f"\n  Per-coin delta (best: {best_overall['label']}):")
        print(f"  {'Coin':5s} {'Base PnL':>11s} {'Best PnL':>11s} {'Delta':>11s}")
        print(f"  {'-'*5} {'-'*11} {'-'*11} {'-'*11}")
        for coin in ALL_COINS:
            base_m = baseline["coins"].get(coin, {})
            best_m = best_r["coins"].get(coin, {})
            bp = base_m.get("net_pnl", 0)
            bestp = best_m.get("net_pnl", 0)
            d = bestp - bp
            eligible_mark = " *" if coin in eligible_coins else ""
            print(f"  {coin:5s} ${bp:>+10,.2f} ${bestp:>+10,.2f} "
                  f"${d:>+10,.2f}{eligible_mark}")

    # ── 8. Verdict ─────────────────────────────────────────────────
    print("\n" + "=" * 70)
    delta = best_overall["delta"]
    best_wr = all_results[best_overall["label"]]["wr"]
    base_wr = baseline["wr"]

    if delta > 500 and best_wr >= base_wr - 1:
        verdict = "GREAT — add per-coin liq to production"
        verdict_code = "great"
    elif delta > 200:
        verdict = "MODERATE — keep collecting, do per-coin grid search"
        verdict_code = "moderate"
    else:
        verdict = "NEUTRAL — BTC liq alone is sufficient"
        verdict_code = "neutral"

    print(f"  VERDICT: {verdict}")
    print(f"  Best: {best_overall['label']} (${best_overall['pnl']:+,.2f}, "
          f"delta ${delta:+,.2f})")
    print(f"  WR: {best_wr:.1f}% (baseline {base_wr:.1f}%)")
    print("=" * 70)

    # ── 9. Save to experiments/registry.json ───────────────────────
    exp_id = f"percoin_liq_factor_{datetime.now().strftime('%Y%m%d_%H%M')}"
    registry_path = "experiments/registry.json"
    os.makedirs("experiments", exist_ok=True)

    if os.path.exists(registry_path):
        with open(registry_path, "r") as f:
            registry = json.load(f)
    else:
        registry = []

    summary = {
        "baseline_pnl": round(base_pnl, 2),
        "eligible_coins": eligible_coins,
        "data_quality": quality,
    }
    for key, r in all_results.items():
        if key == "BASELINE":
            continue
        summary[key] = {
            "pnl": round(r["total_pnl"], 2),
            "delta": round(r["total_pnl"] - base_pnl, 2),
            "trades": r["n_trades"],
            "wr": round(r["wr"], 1),
        }

    registry.append({
        "experiment_id": exp_id,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "description": "Per-coin liquidation factor test: "
                       "altcoin-specific liq as per-coin score boost "
                       "(3 variants x 4 weights)",
        "params": {
            "test_period": f"{TEST_START.date()} to OOS end",
            "variants": list(SCORE_VARIANTS.keys()),
            "weights": WEIGHTS_TO_TEST,
            "eligible_coins": eligible_coins,
            "min_oos_events": MIN_OOS_EVENTS,
            "data_source": "market_data.liquidation (tick-level)",
        },
        "results": summary,
        "best": best_overall["label"],
        "verdict": verdict_code,
    })

    with open(registry_path, "w") as f:
        json.dump(registry, f, indent=2)
    print(f"\n  Saved to {registry_path} as '{exp_id}'")


if __name__ == "__main__":
    main()
