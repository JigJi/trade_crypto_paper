"""
Top 100 Binance Altcoins -- BTC-Led v3 Screening Backtest
==========================================================
Tests the v3 BTC composite signal across top 100 USDT perpetual futures.
Uses fixed default params (no per-coin grid search), OOS period only.

Usage:
    python test_100_coins.py
    python test_100_coins.py --top 50          # top N coins
    python test_100_coins.py --skip-download    # use cached data only
"""

import os, sys, warnings, time as _time, argparse, json
from datetime import datetime

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

# Fix Windows console encoding for non-ASCII symbols
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Reuse core functions from backtest engine
from backtest_15m_btc_led_alts import (
    fetch_binance_15m,
    load_btc_db_data,
    build_btc_features,
    compute_btc_composite_score,
    build_alt_technicals,
    generate_btc_led_signal,
    run_backtest,
    calc_metrics,
    INIT_EQUITY,
    BUDGET_USDT,
    LEVERAGE,
    BKK_UTC_OFFSET,
)
from paper_trading.config import COMPOSITE_WEIGHTS

import requests

# ---- Constants ----
OOS_START = pd.Timestamp("2025-12-01")
OOS_END = pd.Timestamp("2026-03-12")

# Fixed default params for screening (v3 alt defaults)
DEFAULT_THRESHOLD = 3.0
DEFAULT_SL = 2.5
DEFAULT_TP = 4.0
DEFAULT_COOLDOWN = 4
DEFAULT_USE_PA = False

# Exclude stablecoins and leveraged tokens
EXCLUDE_PATTERNS = [
    "USDCUSDT", "BUSDUSDT", "TUSDUSDT", "USDPUSDT", "FDUSDUSDT",
    "DAIUSDT", "EURUSDT", "GBPUSDT",
]
EXCLUDE_SUFFIXES = ["DOWNUSDT", "UPUSDT", "BULLUSDT", "BEARUSDT"]


def get_top_usdt_perps(top_n=100):
    """Fetch top N USDT perpetual futures by 24h quote volume from Binance."""
    print(f"\n[1] Fetching top {top_n} USDT perpetual symbols...")

    # Get exchange info for PERPETUAL contracts
    info_url = "https://fapi.binance.com/fapi/v1/exchangeInfo"
    r = requests.get(info_url, timeout=15)
    r.raise_for_status()
    exchange_info = r.json()

    # Filter: PERPETUAL, USDT quote, TRADING status
    valid_symbols = set()
    for s in exchange_info["symbols"]:
        if (s.get("contractType") == "PERPETUAL"
                and s.get("quoteAsset") == "USDT"
                and s.get("status") == "TRADING"
                and s["symbol"] not in EXCLUDE_PATTERNS
                and not any(s["symbol"].endswith(suf) for suf in EXCLUDE_SUFFIXES)):
            valid_symbols.add(s["symbol"])

    print(f"  Found {len(valid_symbols)} valid USDT perpetuals")

    # Get 24h ticker for volume sorting
    ticker_url = "https://fapi.binance.com/fapi/v1/ticker/24hr"
    r = requests.get(ticker_url, timeout=15)
    r.raise_for_status()
    tickers = r.json()

    # Sort by quoteVolume descending
    ranked = []
    for t in tickers:
        sym = t["symbol"]
        if sym in valid_symbols:
            ranked.append({
                "symbol": sym,
                "quote_volume_24h": float(t["quoteVolume"]),
                "price": float(t["lastPrice"]),
            })

    ranked.sort(key=lambda x: x["quote_volume_24h"], reverse=True)
    top = ranked[:top_n]

    print(f"  Top {len(top)} by 24h volume:")
    print(f"    #1  {top[0]['symbol']:12s} vol=${top[0]['quote_volume_24h']/1e9:.1f}B")
    print(f"    #{len(top)}  {top[-1]['symbol']:12s} vol=${top[-1]['quote_volume_24h']/1e6:.0f}M")

    return top


def build_btc_score_once():
    """Build BTC composite score (v3 classic 5 factors) -- run once, reuse for all coins."""
    print(f"\n[2] Building BTC composite score (v3, 5 classic factors)...")

    btc_ohlcv = fetch_binance_15m("BTCUSDT", years=1)
    btc_db = load_btc_db_data()
    btc_df = build_btc_features(btc_ohlcv, btc_db)
    btc_score = compute_btc_composite_score(btc_df, COMPOSITE_WEIGHTS)
    btc_score_ts = pd.Series(btc_score.values, index=btc_df["ts"])

    # BTC regime for reference
    btc_regime_ts = pd.Series((btc_df["close"] > btc_df["ema50"]).values, index=btc_df["ts"])

    # OOS BTC buy & hold
    btc_oos = btc_df[(btc_df["ts"] >= OOS_START) & (btc_df["ts"] <= OOS_END)]
    if len(btc_oos) > 0:
        btc_bh = (btc_oos["close"].iloc[-1] / btc_oos["close"].iloc[0] - 1) * 100
        print(f"  BTC OOS B&H: {btc_bh:+.1f}% ({OOS_START.date()} to {OOS_END.date()})")

    # Score distribution
    oos_score = btc_score_ts[(btc_score_ts.index >= OOS_START) & (btc_score_ts.index <= OOS_END)]
    bull_bars = (oos_score >= DEFAULT_THRESHOLD).sum()
    bear_bars = (oos_score <= -DEFAULT_THRESHOLD).sum()
    total_bars = len(oos_score)
    print(f"  Score dist (OOS): {bull_bars} bull ({bull_bars/total_bars*100:.1f}%), "
          f"{bear_bars} bear ({bear_bars/total_bars*100:.1f}%), "
          f"{total_bars - bull_bars - bear_bars} neutral ({(total_bars - bull_bars - bear_bars)/total_bars*100:.1f}%)")

    return btc_score_ts, btc_regime_ts, btc_df


def backtest_one_coin(symbol, btc_score_ts, skip_download=False):
    """Run OOS-only backtest for one coin with fixed params. Returns metrics dict or None."""
    try:
        # Check cache first if skip_download
        cache_file = f"data_cache/{symbol}_15m_1yr.parquet"
        if skip_download and not os.path.exists(cache_file):
            return None

        ohlcv = fetch_binance_15m(symbol, years=1)
        if ohlcv is None or len(ohlcv) < 500:
            return None

        alt_df = build_alt_technicals(ohlcv)

        # OOS only
        alt_oos = alt_df[(alt_df["ts"] >= OOS_START) & (alt_df["ts"] <= OOS_END)].reset_index(drop=True)
        if len(alt_oos) < 200:
            return None

        # Buy & hold
        bh_pct = (alt_oos["close"].iloc[-1] / alt_oos["close"].iloc[0] - 1) * 100

        # Generate signal with fixed params
        signals, alt_merged = generate_btc_led_signal(
            btc_score_ts, alt_oos, DEFAULT_THRESHOLD, DEFAULT_USE_PA)

        # Run backtest
        trades = run_backtest(
            alt_merged, signals,
            sl_atr_mult=DEFAULT_SL,
            tp_atr_mult=DEFAULT_TP,
            cooldown_bars=DEFAULT_COOLDOWN,
        )

        metrics = calc_metrics(trades, len(alt_oos))
        metrics["symbol"] = symbol
        metrics["coin"] = symbol.replace("USDT", "")
        metrics["bh_pct"] = round(bh_pct, 2)
        metrics["oos_bars"] = len(alt_oos)
        metrics["start_price"] = round(alt_oos["close"].iloc[0], 6)
        metrics["end_price"] = round(alt_oos["close"].iloc[-1], 6)

        # Strategy return %
        strat_ret_pct = metrics["net_pnl"] / INIT_EQUITY * 100
        metrics["strat_ret_pct"] = round(strat_ret_pct, 2)

        # Alpha vs buy & hold (long-only comparison)
        metrics["alpha_vs_bh"] = round(strat_ret_pct - bh_pct, 2)

        return metrics

    except Exception as e:
        print(f"  ERROR {symbol}: {e}")
        return None


def write_results_md(results, top_symbols, btc_score_ts, btc_df):
    """Write results markdown file in Thai."""
    # Sort by net_pnl descending
    results.sort(key=lambda x: x["net_pnl"], reverse=True)

    # Stats
    profitable = [r for r in results if r["net_pnl"] > 0]
    losing = [r for r in results if r["net_pnl"] <= 0]
    total_pnl = sum(r["net_pnl"] for r in results)
    avg_pnl = total_pnl / len(results) if results else 0
    avg_wr = np.mean([r["win_rate"] for r in results if r["total"] > 0])
    avg_sharpe = np.mean([r["sharpe"] for r in results if r["total"] > 0])

    # Current 6 coins
    current_coins = {"BTC", "XRP", "ADA", "DOT", "SUI", "FIL"}
    current_results = [r for r in results if r["coin"] in current_coins]
    other_results = [r for r in results if r["coin"] not in current_coins]

    # BTC OOS B&H
    btc_oos = btc_df[(btc_df["ts"] >= OOS_START) & (btc_df["ts"] <= OOS_END)]
    btc_bh = (btc_oos["close"].iloc[-1] / btc_oos["close"].iloc[0] - 1) * 100 if len(btc_oos) > 0 else 0

    lines = []
    lines.append("# ผลทดสอบ v3 Model บน Top 100 Binance Altcoins")
    lines.append("")
    lines.append(f"**วันที่ทดสอบ:** {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"**OOS Period:** {OOS_START.date()} ถึง {OOS_END.date()} (~3.5 เดือน)")
    lines.append(f"**BTC B&H (OOS):** {btc_bh:+.1f}%")
    lines.append(f"**Model:** v3 BTC Composite (5 classic factors)")
    lines.append(f"**Params:** threshold={DEFAULT_THRESHOLD}, SL={DEFAULT_SL}ATR, TP={DEFAULT_TP}ATR, cooldown={DEFAULT_COOLDOWN}")
    lines.append(f"**Sizing:** ${BUDGET_USDT:.0f} x {LEVERAGE:.0f}x = ${BUDGET_USDT*LEVERAGE:.0f} notional/trade")
    lines.append("")

    lines.append("## สรุปภาพรวม")
    lines.append("")
    lines.append(f"| รายการ | ค่า |")
    lines.append(f"|--------|-----|")
    lines.append(f"| เหรียญที่ทดสอบได้ | {len(results)} จาก {len(top_symbols)} |")
    lines.append(f"| กำไร (เหรียญ) | {len(profitable)} ({len(profitable)/len(results)*100:.0f}%) |")
    lines.append(f"| ขาดทุน (เหรียญ) | {len(losing)} ({len(losing)/len(results)*100:.0f}%) |")
    lines.append(f"| PnL รวม | ${total_pnl:+,.0f} |")
    lines.append(f"| PnL เฉลี่ย/เหรียญ | ${avg_pnl:+,.0f} |")
    lines.append(f"| Win Rate เฉลี่ย | {avg_wr:.1f}% |")
    lines.append(f"| Sharpe เฉลี่ย | {avg_sharpe:.2f} |")
    lines.append("")

    # Current 6 coins section
    lines.append("## เปรียบเทียบ 6 เหรียญปัจจุบัน vs ตลาด")
    lines.append("")
    if current_results:
        cur_pnl = sum(r["net_pnl"] for r in current_results)
        cur_avg_wr = np.mean([r["win_rate"] for r in current_results if r["total"] > 0])
        # Find ranks
        for cr in current_results:
            cr["rank"] = next(i+1 for i, r in enumerate(results) if r["symbol"] == cr["symbol"])

        lines.append(f"| เหรียญ | Rank | Trades | WR% | Sharpe | PnL | B&H% | Alpha |")
        lines.append(f"|--------|------|--------|-----|--------|-----|------|-------|")
        for r in sorted(current_results, key=lambda x: x["net_pnl"], reverse=True):
            lines.append(f"| {r['coin']} | #{r['rank']}/{len(results)} | {r['total']} | "
                         f"{r['win_rate']:.1f} | {r['sharpe']:.2f} | "
                         f"${r['net_pnl']:+,.0f} | {r['bh_pct']:+.1f}% | {r['alpha_vs_bh']:+.1f}% |")
        lines.append(f"| **รวม 6 เหรียญ** | | | {cur_avg_wr:.1f} | | **${cur_pnl:+,.0f}** | | |")
        lines.append("")

    # Top 20
    lines.append("## Top 20 เหรียญที่ทำกำไรสูงสุด")
    lines.append("")
    lines.append(f"| # | เหรียญ | Trades | WR% | Sharpe | PnL | MaxDD% | B&H% | Alpha | หมายเหตุ |")
    lines.append(f"|---|--------|--------|-----|--------|-----|--------|------|-------|----------|")
    for i, r in enumerate(results[:20]):
        note = "**ปัจจุบัน**" if r["coin"] in current_coins else ""
        lines.append(f"| {i+1} | {r['coin']} | {r['total']} | {r['win_rate']:.1f} | "
                     f"{r['sharpe']:.2f} | ${r['net_pnl']:+,.0f} | {r['max_dd']:.1f}% | "
                     f"{r['bh_pct']:+.1f}% | {r['alpha_vs_bh']:+.1f}% | {note} |")
    lines.append("")

    # Bottom 10
    lines.append("## Bottom 10 เหรียญที่ขาดทุนมากสุด")
    lines.append("")
    lines.append(f"| # | เหรียญ | Trades | WR% | Sharpe | PnL | MaxDD% | B&H% |")
    lines.append(f"|---|--------|--------|-----|--------|-----|--------|------|")
    for i, r in enumerate(reversed(results[-10:])):
        rank = len(results) - i
        lines.append(f"| {rank} | {r['coin']} | {r['total']} | {r['win_rate']:.1f} | "
                     f"{r['sharpe']:.2f} | ${r['net_pnl']:+,.0f} | {r['max_dd']:.1f}% | "
                     f"{r['bh_pct']:+.1f}% |")
    lines.append("")

    # Full table
    lines.append("## ตารางเต็ม -- ทุกเหรียญ (เรียงตาม PnL)")
    lines.append("")
    lines.append(f"| # | เหรียญ | Trades | Long | Short | WR% | WR_L% | WR_S% | Sharpe | PF | PnL | MaxDD% | B&H% | Alpha |")
    lines.append(f"|---|--------|--------|------|-------|-----|-------|-------|--------|-----|-----|--------|------|-------|")
    for i, r in enumerate(results):
        lines.append(f"| {i+1} | {r['coin']} | {r['total']} | {r['n_long']} | {r['n_short']} | "
                     f"{r['win_rate']:.1f} | {r['wr_long']:.0f} | {r['wr_short']:.0f} | "
                     f"{r['sharpe']:.2f} | {r['pf']:.2f} | ${r['net_pnl']:+,.0f} | "
                     f"{r['max_dd']:.1f}% | {r['bh_pct']:+.1f}% | {r['alpha_vs_bh']:+.1f}% |")
    lines.append("")

    # Short vs Long analysis
    all_trades_total = sum(r["total"] for r in results)
    all_longs = sum(r["n_long"] for r in results)
    all_shorts = sum(r["n_short"] for r in results)
    avg_wr_long = np.mean([r["wr_long"] for r in results if r["n_long"] > 0])
    avg_wr_short = np.mean([r["wr_short"] for r in results if r["n_short"] > 0])

    lines.append("## วิเคราะห์ Long vs Short")
    lines.append("")
    lines.append(f"| | Long | Short |")
    lines.append(f"|--|------|-------|")
    lines.append(f"| จำนวน | {all_longs} ({all_longs/max(all_trades_total,1)*100:.0f}%) | {all_shorts} ({all_shorts/max(all_trades_total,1)*100:.0f}%) |")
    lines.append(f"| WR เฉลี่ย | {avg_wr_long:.1f}% | {avg_wr_short:.1f}% |")
    lines.append("")

    # Insights
    lines.append("## ข้อสังเกตและข้อสรุป")
    lines.append("")

    # Find coins with >70% WR
    high_wr = [r for r in results if r["win_rate"] > 70 and r["total"] >= 20]
    if high_wr:
        lines.append(f"### เหรียญที่มี WR > 70% (>20 trades)")
        lines.append(f"- " + ", ".join(f"{r['coin']} ({r['win_rate']:.1f}%)" for r in high_wr))
        lines.append("")

    # Find coins with Sharpe > 5
    high_sharpe = [r for r in results if r["sharpe"] > 5 and r["total"] >= 20]
    if high_sharpe:
        lines.append(f"### เหรียญที่มี Sharpe > 5 (>20 trades)")
        lines.append(f"- " + ", ".join(f"{r['coin']} ({r['sharpe']:.2f})" for r in high_sharpe))
        lines.append("")

    # Coins that beat current 6 avg
    if current_results:
        cur_avg_pnl = sum(r["net_pnl"] for r in current_results) / len(current_results)
        better = [r for r in other_results if r["net_pnl"] > cur_avg_pnl and r["total"] >= 20]
        if better:
            lines.append(f"### เหรียญใหม่ที่ดีกว่า avg ของ 6 เหรียญปัจจุบัน (>${cur_avg_pnl:,.0f})")
            for r in better[:15]:
                lines.append(f"- **{r['coin']}**: ${r['net_pnl']:+,.0f} | WR {r['win_rate']:.1f}% | Sharpe {r['sharpe']:.2f}")
            lines.append("")

    # Write file
    output_path = "backtest_100coins_results.md"
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"\n  Results written to {output_path}")

    return output_path


def main():
    parser = argparse.ArgumentParser(description="BTC-Led v3 Screening -- Top 100 Altcoins")
    parser.add_argument("--top", type=int, default=100, help="Number of top coins to test")
    parser.add_argument("--skip-download", action="store_true", help="Only use cached data")
    args = parser.parse_args()

    t0 = _time.time()
    print("=" * 70)
    print("  BTC-Led v3 Strategy -- Top 100 Altcoin Screening Backtest")
    print(f"  OOS: {OOS_START.date()} to {OOS_END.date()}")
    print(f"  Params: thr={DEFAULT_THRESHOLD} SL={DEFAULT_SL} TP={DEFAULT_TP} cd={DEFAULT_COOLDOWN}")
    print("=" * 70)

    # Step 1: Get top symbols
    top_symbols = get_top_usdt_perps(args.top)

    # Step 2: Build BTC composite score once
    btc_score_ts, btc_regime_ts, btc_df = build_btc_score_once()

    # Step 3: Loop through all coins
    print(f"\n[3] Running OOS backtest on {len(top_symbols)} coins...")
    print(f"    (Fixed params: thr={DEFAULT_THRESHOLD}, SL={DEFAULT_SL}, TP={DEFAULT_TP}, cd={DEFAULT_COOLDOWN})")

    results = []
    failed = []

    for idx, sym_info in enumerate(top_symbols):
        symbol = sym_info["symbol"]
        pct = (idx + 1) / len(top_symbols) * 100
        print(f"\n  [{idx+1}/{len(top_symbols)}] {symbol} ({pct:.0f}%)...", end="", flush=True)

        metrics = backtest_one_coin(symbol, btc_score_ts, skip_download=args.skip_download)

        if metrics is None:
            failed.append(symbol)
            print(f" SKIP (insufficient data)")
            continue

        results.append(metrics)
        pnl = metrics["net_pnl"]
        wr = metrics["win_rate"]
        trades = metrics["total"]
        print(f" {trades} trades, WR={wr:.1f}%, PnL=${pnl:+,.0f}")

    elapsed = _time.time() - t0
    print(f"\n{'='*70}")
    print(f"  DONE: {len(results)} coins tested, {len(failed)} failed/skipped")
    print(f"  Time: {elapsed/60:.1f} min")

    if not results:
        print("  No results to write!")
        return

    # Step 4: Write results
    print(f"\n[4] Writing results...")
    output_path = write_results_md(results, top_symbols, btc_score_ts, btc_df)

    # Also save raw JSON for further analysis
    json_path = "experiments/100coins_screening.json"
    os.makedirs("experiments", exist_ok=True)
    with open(json_path, "w") as f:
        # Convert numpy types to Python native
        clean = []
        for r in results:
            clean.append({k: (float(v) if isinstance(v, (np.floating, np.integer)) else v)
                          for k, v in r.items()})
        json.dump(clean, f, indent=2, default=str)
    print(f"  Raw data saved to {json_path}")

    # Print quick summary
    results.sort(key=lambda x: x["net_pnl"], reverse=True)
    profitable = [r for r in results if r["net_pnl"] > 0]
    total_pnl = sum(r["net_pnl"] for r in results)

    print(f"\n{'='*70}")
    print(f"  SUMMARY")
    print(f"  Profitable: {len(profitable)}/{len(results)} ({len(profitable)/len(results)*100:.0f}%)")
    print(f"  Total PnL: ${total_pnl:+,.0f}")
    print(f"  Avg PnL/coin: ${total_pnl/len(results):+,.0f}")
    print(f"\n  Top 5:")
    for r in results[:5]:
        print(f"    {r['coin']:8s} ${r['net_pnl']:+8,.0f}  WR={r['win_rate']:.1f}%  Sharpe={r['sharpe']:.2f}")
    print(f"\n  Bottom 5:")
    for r in results[-5:]:
        print(f"    {r['coin']:8s} ${r['net_pnl']:+8,.0f}  WR={r['win_rate']:.1f}%  Sharpe={r['sharpe']:.2f}")
    print(f"{'='*70}")


if __name__ == "__main__":
    main()
