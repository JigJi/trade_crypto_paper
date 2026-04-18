"""
Backtest Rerun with Realistic SIGNAL_FLIP Logic
================================================
Fixes lookahead bias: backtest now matches paper trading flip behavior.

Changes from original:
1. SIGNAL_FLIP exit at CLOSE (not OPEN) — no future-peeking
2. Hysteresis band applied to signal generation (v3/v5=1.5, v6=3.0)
3. min_bars_before_flip enforced (v3/v5=4, v6=0)
4. flip_cd_extra cooldown after flip (v6=4 bars)
5. Portfolio sim now includes SIGNAL_FLIP exits

Runs v3, v5, v6 configs side by side and compares OLD vs NEW results.
"""

import os, sys, warnings, time as _time
from datetime import datetime

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

# Ensure project root is in path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backtest_15m_btc_led_alts import (
    fetch_binance_15m, load_btc_db_data,
    generate_btc_led_signal, run_backtest, calc_metrics,
    run_portfolio_simulation, compute_half_kelly,
    INIT_EQUITY, BUDGET_USDT, LEVERAGE, FEE, SLIP,
)
from signal_core import (
    build_btc_features, build_alt_technicals,
    compute_btc_composite_score, compute_btc_composite_score_v6,
    DEFAULT_COMPOSITE_WEIGHTS, DEFAULT_EXTRA_WEIGHTS,
)

BKK_UTC_OFFSET = pd.Timedelta("7h")

# -- Paper trading flip configs (source of truth) --
FLIP_CONFIG = {
    "v3": {"hysteresis_band": 1.5, "min_bars_flip": 4, "flip_cd_extra": 0},
    "v5": {"hysteresis_band": 1.5, "min_bars_flip": 4, "flip_cd_extra": 0},
    "v6": {"hysteresis_band": 3.0, "min_bars_flip": 0, "flip_cd_extra": 4},
}

# -- Per-coin configs (from memory/v3_model_config.md) --
V3_CONFIGS = {
    "BTC": {"threshold": 2.5, "alt_pa": False, "sl": 2.5, "tp": 4.0, "trail": 99, "trail_act": 99, "cd": 4},
    "XRP": {"threshold": 3.5, "alt_pa": False, "sl": 2.5, "tp": 4.0, "trail": 99, "trail_act": 99, "cd": 4},
    "ADA": {"threshold": 3.5, "alt_pa": False, "sl": 2.5, "tp": 4.0, "trail": 99, "trail_act": 99, "cd": 4},
    "DOT": {"threshold": 3.0, "alt_pa": False, "sl": 2.5, "tp": 4.0, "trail": 99, "trail_act": 99, "cd": 8},
    "SUI": {"threshold": 3.0, "alt_pa": False, "sl": 2.5, "tp": 4.0, "trail": 99, "trail_act": 99, "cd": 4},
    "FIL": {"threshold": 3.0, "alt_pa": False, "sl": 2.5, "tp": 4.0, "trail": 99, "trail_act": 99, "cd": 4},
}

V5_CONFIGS = {
    "BTC": {"threshold": 2.5, "alt_pa": False, "sl": 15.0, "tp": 12.0, "trail": 99, "trail_act": 99, "cd": 4},
    "XRP": {"threshold": 3.5, "alt_pa": False, "sl": 15.0, "tp": 12.0, "trail": 99, "trail_act": 99, "cd": 4},
    "ADA": {"threshold": 3.5, "alt_pa": False, "sl": 15.0, "tp": 12.0, "trail": 99, "trail_act": 99, "cd": 4},
    "DOT": {"threshold": 3.0, "alt_pa": False, "sl": 15.0, "tp": 12.0, "trail": 99, "trail_act": 99, "cd": 4},
    "SUI": {"threshold": 3.0, "alt_pa": False, "sl": 15.0, "tp": 12.0, "trail": 99, "trail_act": 99, "cd": 4},
    "FIL": {"threshold": 3.0, "alt_pa": False, "sl": 15.0, "tp": 12.0, "trail": 99, "trail_act": 99, "cd": 4},
}

V6_CONFIGS = {
    "BTC": {"threshold": 2.5, "alt_pa": False, "sl": 25.0, "tp": 20.0, "trail": 99, "trail_act": 99, "cd": 4},
    "XRP": {"threshold": 3.5, "alt_pa": False, "sl": 25.0, "tp": 20.0, "trail": 99, "trail_act": 99, "cd": 4},
    "ADA": {"threshold": 3.5, "alt_pa": False, "sl": 25.0, "tp": 20.0, "trail": 99, "trail_act": 99, "cd": 4},
    "DOT": {"threshold": 3.0, "alt_pa": False, "sl": 25.0, "tp": 20.0, "trail": 99, "trail_act": 99, "cd": 4},
    "SUI": {"threshold": 3.0, "alt_pa": False, "sl": 25.0, "tp": 20.0, "trail": 99, "trail_act": 99, "cd": 4},
    "FIL": {"threshold": 3.0, "alt_pa": False, "sl": 25.0, "tp": 20.0, "trail": 99, "trail_act": 99, "cd": 4},
}

ALT_COINS = ["BTC", "XRP", "ADA", "DOT", "SUI", "FIL"]


def run_single_coin(coin, btc_score_ts, btc_period_start, btc_period_end,
                    version, coin_cfg, flip_cfg, btc_regime_ts=None):
    """Run backtest for one coin with specific version config."""
    symbol = f"{coin}USDT"
    print(f"  {coin} ({version})...", end=" ", flush=True)

    ohlcv = fetch_binance_15m(symbol, years=3)
    alt_df = build_alt_technicals(ohlcv)
    alt_df = alt_df[(alt_df["ts"] >= btc_period_start) & (alt_df["ts"] <= btc_period_end)].reset_index(drop=True)

    if len(alt_df) < 200:
        print("SKIP (insufficient data)")
        return None

    # OOS split
    split_date = pd.Timestamp("2025-12-01")
    alt_test = alt_df[alt_df["ts"] >= split_date].reset_index(drop=True)

    if len(alt_test) < 100:
        print("SKIP (insufficient OOS data)")
        return None

    # Generate signals WITH hysteresis (matching paper trading)
    signals_hyst, alt_merged = generate_btc_led_signal(
        btc_score_ts, alt_df, coin_cfg["threshold"], coin_cfg["alt_pa"],
        btc_regime_ts=btc_regime_ts,
        hysteresis_band=flip_cfg["hysteresis_band"],
    )

    # Generate signals WITHOUT hysteresis (old behavior for comparison)
    signals_old, _ = generate_btc_led_signal(
        btc_score_ts, alt_df, coin_cfg["threshold"], coin_cfg["alt_pa"],
        btc_regime_ts=btc_regime_ts,
        hysteresis_band=0.0,
    )

    # Run NEW backtest (realistic flip)
    trades_new = run_backtest(
        alt_merged, signals_hyst,
        sl_atr_mult=coin_cfg["sl"], tp_atr_mult=coin_cfg["tp"],
        trail_atr_mult=coin_cfg["trail"], trail_activate_atr=coin_cfg["trail_act"],
        cooldown_bars=coin_cfg["cd"],
        min_bars_before_flip=flip_cfg["min_bars_flip"],
        flip_cd_extra=flip_cfg["flip_cd_extra"],
    )

    # Run OLD backtest (original behavior — no hysteresis, no flip protection)
    # Note: SIGNAL_FLIP exit price is now 'c' in all cases (code changed),
    # but old had no min_bars_flip and no hysteresis
    trades_old = run_backtest(
        alt_merged, signals_old,
        sl_atr_mult=coin_cfg["sl"], tp_atr_mult=coin_cfg["tp"],
        trail_atr_mult=coin_cfg["trail"], trail_activate_atr=coin_cfg["trail_act"],
        cooldown_bars=coin_cfg["cd"],
        min_bars_before_flip=0,
        flip_cd_extra=0,
    )

    # Split OOS
    m_new_full = calc_metrics(trades_new, len(alt_df))
    m_old_full = calc_metrics(trades_old, len(alt_df))

    if not trades_new.empty:
        trades_new_oos = trades_new[pd.to_datetime(trades_new["entry_time"]) >= split_date]
    else:
        trades_new_oos = trades_new
    if not trades_old.empty:
        trades_old_oos = trades_old[pd.to_datetime(trades_old["entry_time"]) >= split_date]
    else:
        trades_old_oos = trades_old

    m_new_oos = calc_metrics(trades_new_oos, len(alt_test))
    m_old_oos = calc_metrics(trades_old_oos, len(alt_test))

    # Exit analysis
    def exit_breakdown(trades):
        if trades.empty:
            return {}
        result = {}
        for reason, grp in trades.groupby("exit_reason"):
            result[reason] = {
                "count": len(grp),
                "pct": len(grp) / len(trades) * 100,
                "wr": (grp["pnl_net"] > 0).mean() * 100,
                "total_pnl": grp["pnl_net"].sum(),
                "avg_pnl": grp["pnl_net"].mean(),
            }
        return result

    print(f"NEW: {m_new_oos['total']}T WR={m_new_oos['win_rate']:.1f}% PnL=${m_new_oos['net_pnl']:+,.2f} | "
          f"OLD: {m_old_oos['total']}T WR={m_old_oos['win_rate']:.1f}% PnL=${m_old_oos['net_pnl']:+,.2f}")

    return {
        "coin": coin,
        "version": version,
        "config": coin_cfg,
        "flip_cfg": flip_cfg,
        "new_full": m_new_full,
        "new_oos": m_new_oos,
        "old_full": m_old_full,
        "old_oos": m_old_oos,
        "new_exits": exit_breakdown(trades_new),
        "old_exits": exit_breakdown(trades_old),
        "trades_new": trades_new,
    }


def main():
    print("=" * 70)
    print("BACKTEST RERUN — Realistic SIGNAL_FLIP (matches paper trading)")
    print("=" * 70)
    print("\nFixes applied:")
    print("  1. SIGNAL_FLIP exit at CLOSE (was: OPEN = lookahead)")
    print("  2. Hysteresis band (v3/v5=1.5, v6=3.0)")
    print("  3. min_bars_before_flip (v3/v5=4, v6=0)")
    print("  4. flip_cd_extra (v6=4 bars)")
    print()

    # -- Phase 1: Build BTC composite score --
    print("[1/3] Building BTC composite score...")
    btc_ohlcv = fetch_binance_15m("BTCUSDT", years=3)
    btc_db = load_btc_db_data()
    btc_df = build_btc_features(btc_ohlcv, btc_db)

    w = DEFAULT_COMPOSITE_WEIGHTS
    btc_score = compute_btc_composite_score(btc_df, w)

    btc_period_start = pd.Timestamp("2025-06-01")
    mask = btc_df["ts"] >= btc_period_start
    btc_df_trimmed = btc_df[mask].reset_index(drop=True)
    btc_score_trimmed = btc_score[mask].reset_index(drop=True)
    btc_period_end = btc_df_trimmed["ts"].iloc[-1]

    btc_score_ts = pd.Series(btc_score_trimmed.values, index=btc_df_trimmed["ts"].values)
    btc_regime = btc_df_trimmed["close"] > btc_df_trimmed["ema50"]
    btc_regime_ts = pd.Series(btc_regime.values, index=btc_df_trimmed["ts"].values)
    print(f"  BTC score: {btc_period_start} to {btc_period_end}")

    # -- Phase 2: Run all versions --
    print(f"\n[2/3] Running backtests for v3/v5/v6...")

    version_configs = {
        "v3": V3_CONFIGS,
        "v5": V5_CONFIGS,
        "v6": V6_CONFIGS,
    }

    all_results = {}
    for ver in ["v3", "v5", "v6"]:
        print(f"\n{'-'*50}")
        print(f"  VERSION: {ver} (hysteresis={FLIP_CONFIG[ver]['hysteresis_band']}, "
              f"min_bars_flip={FLIP_CONFIG[ver]['min_bars_flip']}, "
              f"flip_cd_extra={FLIP_CONFIG[ver]['flip_cd_extra']})")
        print(f"{'-'*50}")

        results = []
        for coin in ALT_COINS:
            try:
                r = run_single_coin(
                    coin, btc_score_ts, btc_period_start, btc_period_end,
                    ver, version_configs[ver][coin], FLIP_CONFIG[ver],
                    btc_regime_ts=btc_regime_ts,
                )
                results.append(r)
            except Exception as e:
                print(f"  {coin} ERROR: {e}")
                results.append(None)

        all_results[ver] = [r for r in results if r is not None]

    # -- Phase 3: Generate report --
    print(f"\n[3/3] Generating comparison report...")

    md = []
    md.append("# Backtest Rerun — Realistic SIGNAL_FLIP")
    md.append(f"\n**Date:** {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    md.append(f"**Purpose:** Fix lookahead bias in SIGNAL_FLIP exit")
    md.append(f"\n## Fixes Applied")
    md.append("| Fix | Old (biased) | New (realistic) |")
    md.append("|-----|-------------|-----------------|")
    md.append("| SIGNAL_FLIP exit price | Open of next bar (lookahead) | Close of current bar |")
    md.append("| Hysteresis (v3/v5) | 0 | 1.5 |")
    md.append("| Hysteresis (v6) | 0 | 3.0 |")
    md.append("| min_bars_before_flip (v3/v5) | 0 | 4 |")
    md.append("| flip_cd_extra (v6) | 0 | 4 bars |")

    for ver in ["v3", "v5", "v6"]:
        results = all_results.get(ver, [])
        if not results:
            continue

        md.append(f"\n## {ver.upper()} Results (OOS: Dec 2025 — Mar 2026)")
        md.append("| Coin | OLD Trades | OLD WR | OLD PnL | NEW Trades | NEW WR | NEW PnL | Delta |")
        md.append("|------|-----------|--------|---------|-----------|--------|---------|-------|")

        total_old = total_new = 0
        for r in results:
            o = r["old_oos"]
            n = r["new_oos"]
            delta = n["net_pnl"] - o["net_pnl"]
            total_old += o["net_pnl"]
            total_new += n["net_pnl"]
            md.append(f"| {r['coin']} | {o['total']} | {o['win_rate']:.1f}% | ${o['net_pnl']:+,.2f} | "
                      f"{n['total']} | {n['win_rate']:.1f}% | ${n['net_pnl']:+,.2f} | ${delta:+,.2f} |")

        md.append(f"| **TOTAL** | | | **${total_old:+,.2f}** | | | **${total_new:+,.2f}** | **${total_new - total_old:+,.2f}** |")

        # Exit breakdown for NEW
        md.append(f"\n### {ver.upper()} Exit Breakdown (NEW, Full Period)")
        md.append("| Coin | SL | TP | SIGNAL_FLIP | TIMEOUT | TRAIL |")
        md.append("|------|----|----|-------------|---------|-------|")
        for r in results:
            exits = r["new_exits"]
            def _fmt(reason):
                e = exits.get(reason, {})
                if not e:
                    return "—"
                return f"{e['count']} ({e['wr']:.0f}%WR, ${e['total_pnl']:+,.0f})"
            md.append(f"| {r['coin']} | {_fmt('SL')} | {_fmt('TP')} | {_fmt('SIGNAL_FLIP')} | {_fmt('TIMEOUT')} | {_fmt('TRAIL')} |")

    # Summary
    md.append("\n## Summary")
    for ver in ["v3", "v5", "v6"]:
        results = all_results.get(ver, [])
        if not results:
            continue
        old_total = sum(r["old_oos"]["net_pnl"] for r in results)
        new_total = sum(r["new_oos"]["net_pnl"] for r in results)
        pct_change = ((new_total - old_total) / abs(old_total) * 100) if old_total != 0 else 0
        md.append(f"- **{ver}**: OLD ${old_total:+,.2f} -> NEW ${new_total:+,.2f} ({pct_change:+.1f}%)")

    report_path = "backtest_rerun_realistic_report.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md))
    print(f"\n  Report: {report_path}")

    # Print summary table
    print(f"\n{'='*70}")
    print("SUMMARY — OLD vs NEW (OOS)")
    print(f"{'='*70}")
    for ver in ["v3", "v5", "v6"]:
        results = all_results.get(ver, [])
        if not results:
            continue
        old_total = sum(r["old_oos"]["net_pnl"] for r in results)
        new_total = sum(r["new_oos"]["net_pnl"] for r in results)
        print(f"\n  {ver}:")
        for r in results:
            o = r["old_oos"]
            n = r["new_oos"]
            print(f"    {r['coin']:5s}: OLD ${o['net_pnl']:+8,.2f} (WR {o['win_rate']:5.1f}%) -> "
                  f"NEW ${n['net_pnl']:+8,.2f} (WR {n['win_rate']:5.1f}%)")
        print(f"    {'TOTAL':5s}: OLD ${old_total:+8,.2f} -> NEW ${new_total:+8,.2f}")

    print(f"\n{'='*70}")
    print("Done! Check report for full details.")


if __name__ == "__main__":
    main()
