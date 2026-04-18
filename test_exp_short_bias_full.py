"""
Experiment: Short Bias on FULL Period + Monthly Regime Breakdown
================================================================
Tests short_bias on entire available period (Jun 2025 - Mar 2026)
with monthly PnL breakdown to see regime-specific behavior.

This complements the bull/bear split test by showing month-by-month impact.
"""

import os, sys, json, warnings
from datetime import datetime

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

sys.path.insert(0, r"D:\0_product_dev\trade_crypto")
from backtest_15m_btc_led_alts import (
    fetch_binance_15m, run_backtest, calc_metrics, BKK_UTC_OFFSET,
)
from test_v12_improvements import (
    V11_CONFIGS, ALL_COINS, generate_signal_v11, generate_signal_short_bias,
)
from paper_trading.strategy import (
    compute_btc_composite_score, build_btc_features, build_alt_technicals,
)
from test_phase1_factors import load_btc_db_data_v3

import backtest_15m_btc_led_alts as bt

FULL_START = pd.Timestamp("2025-06-01")
FULL_END   = pd.Timestamp("2026-03-10")
OFFSETS = [0.0, 0.5, 1.0]


def run_with_trades(btc_score_ts, period_start, period_end, signal_fn, signal_kwargs):
    """Run backtest and return all trades with timestamps for monthly breakdown."""
    old_lev = bt.LEVERAGE
    bt.LEVERAGE = 2.0

    all_trades = []
    coin_results = {}

    for coin in ALL_COINS:
        cfg = V11_CONFIGS[coin]
        symbol = f"{coin}USDT"
        alt_raw = fetch_binance_15m(symbol, years=3)
        alt_df = build_alt_technicals(alt_raw)
        alt_df = alt_df[(alt_df["ts"] >= period_start) & (alt_df["ts"] <= period_end)].copy()

        if alt_df.empty:
            continue

        signal, alt_merged = signal_fn(
            btc_score_ts, alt_df, cfg["threshold"], cfg["alt_pa"],
            **signal_kwargs
        )
        trades = run_backtest(
            alt_merged, signal,
            sl_atr_mult=cfg["sl"], tp_atr_mult=cfg["tp"],
            trail_atr_mult=cfg["trail"], trail_activate_atr=cfg["trail_act"],
            max_hold_bars=96, cooldown_bars=cfg["cd"],
        )

        if not trades.empty:
            trades_copy = trades.copy()
            trades_copy["coin"] = coin
            all_trades.append(trades_copy)
            m = calc_metrics(trades, len(alt_df))
            coin_results[coin] = {
                "pnl": round(m["net_pnl"], 2), "trades": m["total"],
                "wr": round(m["win_rate"], 1), "sharpe": round(m["sharpe"], 2),
                "n_long": m.get("n_long", 0), "n_short": m.get("n_short", 0),
            }
        else:
            coin_results[coin] = {"pnl": 0, "trades": 0, "wr": 0, "sharpe": 0,
                                  "n_long": 0, "n_short": 0}

    bt.LEVERAGE = old_lev

    if all_trades:
        combined = pd.concat(all_trades, ignore_index=True)
    else:
        combined = pd.DataFrame()

    return combined, coin_results


def monthly_breakdown(trades_df):
    """Break down trades by month."""
    if trades_df.empty:
        return {}

    trades_df = trades_df.copy()
    trades_df["month"] = pd.to_datetime(trades_df["entry_time"]).dt.to_period("M")

    monthly = {}
    for month, group in trades_df.groupby("month"):
        n_long = (group["dir"] == "L").sum()
        n_short = (group["dir"] == "S").sum()
        pnl_long = group.loc[group["dir"] == "L", "pnl_net"].sum()
        pnl_short = group.loc[group["dir"] == "S", "pnl_net"].sum()
        wr = (group["pnl_net"] > 0).mean() * 100

        monthly[str(month)] = {
            "pnl": round(group["pnl_net"].sum(), 2),
            "trades": len(group),
            "wr": round(wr, 1),
            "n_long": int(n_long), "n_short": int(n_short),
            "pnl_long": round(pnl_long, 2), "pnl_short": round(pnl_short, 2),
        }

    return monthly


def main():
    print("=" * 70)
    print("VALIDATION: Short Bias -- Full Period + Monthly Breakdown")
    print(f"Period: {FULL_START} to {FULL_END}")
    print(f"Offsets: {OFFSETS}")
    print("=" * 70)

    # ---- 1. Load data ----
    print("\n[1] Loading BTC data + v3 factors...")
    btc_raw = fetch_binance_15m("BTCUSDT", years=3)
    db_data = load_btc_db_data_v3()
    btc_df = build_btc_features(btc_raw, db_data)
    btc_df = btc_df[btc_df["ts"] >= pd.Timestamp("2025-01-01")].copy().reset_index(drop=True)

    btc_score = compute_btc_composite_score(btc_df)
    btc_score_ts = btc_score.copy()
    btc_score_ts.index = btc_df["ts"]

    # ---- 2. Run each offset ----
    all_results = {}

    for offset in OFFSETS:
        if offset == 0.0:
            label = "baseline"
            fn = generate_signal_v11
            kwargs = {}
        else:
            label = f"short_bias_{offset}"
            fn = generate_signal_short_bias
            kwargs = {"short_offset": offset}

        print(f"\n[2] Running {label}...")
        trades_df, coin_results = run_with_trades(
            btc_score_ts, FULL_START, FULL_END, fn, kwargs
        )

        total_pnl = sum(r["pnl"] for r in coin_results.values())
        total_trades = sum(r["trades"] for r in coin_results.values())
        total_long = sum(r["n_long"] for r in coin_results.values())
        total_short = sum(r["n_short"] for r in coin_results.values())

        monthly = monthly_breakdown(trades_df)

        all_results[str(offset)] = {
            "pnl": round(total_pnl, 2),
            "trades": total_trades,
            "n_long": total_long,
            "n_short": total_short,
            "per_coin": coin_results,
            "monthly": monthly,
        }

        print(f"  Total: ${total_pnl:,.0f} ({total_trades} trades, L:{total_long} S:{total_short})")
        print(f"\n  Monthly breakdown:")
        print(f"  {'Month':>8s} {'PnL':>10s} {'Trades':>7s} {'WR':>6s} {'PnL_L':>10s} {'PnL_S':>10s} {'L':>4s} {'S':>4s}")
        print(f"  {'-'*8} {'-'*10} {'-'*7} {'-'*6} {'-'*10} {'-'*10} {'-'*4} {'-'*4}")
        for month in sorted(monthly.keys()):
            m = monthly[month]
            print(f"  {month:>8s} ${m['pnl']:>9,.0f} {m['trades']:>7d} {m['wr']:>5.1f}% ${m['pnl_long']:>9,.0f} ${m['pnl_short']:>9,.0f} {m['n_long']:>4d} {m['n_short']:>4d}")

    # ---- 3. Summary ----
    print(f"\n{'='*70}")
    print(f"  FULL PERIOD SUMMARY")
    print(f"{'='*70}")

    base_pnl = all_results["0.0"]["pnl"]
    print(f"\n  {'Offset':>8s} {'Total PnL':>11s} {'Trades':>7s} {'Delta':>10s} {'%':>8s}")
    print(f"  {'-'*8} {'-'*11} {'-'*7} {'-'*10} {'-'*8}")
    for offset in OFFSETS:
        k = str(offset)
        r = all_results[k]
        delta = r["pnl"] - base_pnl
        pct = (delta / abs(base_pnl) * 100) if base_pnl != 0 else 0
        print(f"  {offset:>8.1f} ${r['pnl']:>10,.0f} {r['trades']:>7d} ${delta:>+9,.0f} {pct:>+7.1f}%")

    # Monthly delta comparison
    print(f"\n  MONTHLY DELTA (offset=0.5 vs baseline):")
    print(f"  {'Month':>8s} {'Base PnL':>10s} {'SB PnL':>10s} {'Delta':>10s} {'Regime':>8s}")
    print(f"  {'-'*8} {'-'*10} {'-'*10} {'-'*10} {'-'*8}")
    base_monthly = all_results["0.0"]["monthly"]
    sb_monthly = all_results["0.5"]["monthly"]
    for month in sorted(set(list(base_monthly.keys()) + list(sb_monthly.keys()))):
        bp = base_monthly.get(month, {}).get("pnl", 0)
        sp = sb_monthly.get(month, {}).get("pnl", 0)
        delta = sp - bp
        # Simple regime classification based on PnL direction of baseline
        regime = "BULL" if bp > 0 else "BEAR" if bp < -200 else "FLAT"
        marker = " ***" if delta < -200 else ""
        print(f"  {month:>8s} ${bp:>9,.0f} ${sp:>9,.0f} ${delta:>+9,.0f} {regime:>8s}{marker}")

    print(f"{'='*70}")

    # ---- 4. Save ----
    exp_id = f"short_bias_full_monthly_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    result = {
        "experiment_id": exp_id,
        "description": "Short bias full period (Jun 2025 - Mar 2026) with monthly breakdown",
        "period": f"{FULL_START} to {FULL_END}",
        "results": all_results,
    }

    os.makedirs("experiments", exist_ok=True)
    out_path = f"experiments/{exp_id}.json"
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2, default=str)
    print(f"\n  Saved: {out_path}")

    return result


if __name__ == "__main__":
    main()
