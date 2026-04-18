"""
V5 Coin Screening: Test ALL 99 coins with v5 tournament champion config.
Config: liq=5.0, ob=2.0, tick_liq=3.0, basis=1.5, SL=15, TP=12
Goal: Select best coins for v5 paper trading (no overlap with v3/v4).
"""
import sys, os, json, time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
os.chdir(str(BASE_DIR))

import pandas as pd, numpy as np
from backtest_15m_btc_led_alts import (
    fetch_binance_15m, load_btc_db_data, build_btc_features,
    compute_btc_composite_score, build_alt_technicals,
    generate_btc_led_signal, run_backtest, calc_metrics,
    V3_EXTRA_WEIGHTS, COMPOSITE_WEIGHTS,
)

# ── V5 Config ──
V5_LIQ = 5.0
V5_OB = 2.0
V5_TICK = 3.0
V5_BASIS = 1.5
V5_SL = 15.0
V5_TP = 12.0
V5_CD = 4
V5_THRESHOLD = 3.0  # default for non-original coins
V5_MAX_HOLD = 96

# OOS period (same as tournament)
OOS_START, OOS_END = "2025-01-01", "2026-03-18"

# All 99 coins from 100-coin screening
ALL_COINS = [
    "PIPPIN", "BERA", "BEAT", "BLUAI", "ENSO", "ZEC", "XPL", "ARIA",
    "BANANAS31", "NAORIS", "PIXEL", "VIRTUAL", "PORTAL", "LYN", "RENDER",
    "BARD", "AXS", "NEAR", "BULLA", "SIREN", "ARB", "SAHARA", "DEGO",
    "ICP", "1000BONK", "SOL", "JCT", "ADA", "OGN", "OPN", "ZRO", "XAI",
    "ASTER", "DOT", "PUMP", "FIL", "1000PEPE", "RIVER", "ETH", "PENGU",
    "HYPE", "ENA", "MANTRA", "TAO", "LTC", "APT", "LA", "FARTCOIN",
    "ICX", "LINK", "ZEREBRO", "HBAR", "WIF", "VVV", "DOGE", "BTC", "WLD",
    "GALA", "AVAX", "AAVE", "ONDO", "XRP", "SUI", "POWER", "ETC", "RONIN",
    "CRV", "FLOW", "1000SHIB", "BNB", "FET", "XMR", "MMT", "SIGN", "ARC",
    "OP", "TRUMP", "HUMA", "TON", "UNI", "SEI", "XLM", "COMP", "BCH",
    "SOLV", "TRIA", "PAXG", "TRX", "AVNT", "WLFI", "ACX", "ROBO",
    "JELLYJELLY", "COLLECT", "NIGHT", "PLAY", "H", "KITE", "RESOLV",
]

# Coins already in v3/v4 (DO NOT select for v5)
V3_COINS = {"BTC", "XRP", "ADA", "DOT", "SUI", "FIL", "RENDER", "BEAT",
            "PIXEL", "NEAR", "AXS", "SOL", "ETH", "1000BONK", "ARB",
            "ARIA", "BARD", "BANANAS31", "PIPPIN"}
V4_COINS = {"OGN", "SAHARA", "ASTER", "LTC", "ZRO", "NAORIS", "1000PEPE",
            "JCT", "DEGO", "HYPE", "PENGU", "LINK"}
EXISTING = V3_COINS | V4_COINS

# Coins known to fail on testnet
TESTNET_BAD = {"LYN", "XAI", "PUMP", "ENSO", "BLUAI", "ICP",
               "ZEC", "XPL", "VIRTUAL", "BERA", "PORTAL"}

print("=" * 90)
print("V5 COIN SCREENING -- Tournament Champion Config")
print(f"Config: liq={V5_LIQ}, ob={V5_OB}, tick={V5_TICK}, SL={V5_SL}, TP={V5_TP}")
print(f"OOS: {OOS_START} to {OOS_END}")
print(f"Coins to test: {len(ALL_COINS)}")
print("=" * 90)

# ── Load BTC data once ──
print("\nLoading BTC data...")
t0 = time.time()
btc_ohlcv = fetch_binance_15m("BTCUSDT", years=3)
db_data = load_btc_db_data()
btc_df = build_btc_features(btc_ohlcv, db_data)

# ── Compute v5 BTC score once ──
import backtest_15m_btc_led_alts as bt
old_extra = dict(bt.V3_EXTRA_WEIGHTS)
bt.V3_EXTRA_WEIGHTS.update({"ob_combined": V5_OB, "tick_liq": V5_TICK, "basis_contrarian": V5_BASIS})

params = dict(COMPOSITE_WEIGHTS)
params.update({
    "w_liq_bull": V5_LIQ, "w_liq_bear": V5_LIQ,
})
score = compute_btc_composite_score(btc_df, params=params)
score_ts = pd.Series(score.values, index=btc_df["ts"].values)

bt.V3_EXTRA_WEIGHTS.update(old_extra)  # restore
print(f"BTC data loaded in {time.time()-t0:.1f}s\n")

# ── Screen each coin ──
results = []
failed = []

for i, coin in enumerate(ALL_COINS, 1):
    symbol = f"{coin}USDT"
    tag = ""
    if coin in V3_COINS: tag = " [v3]"
    elif coin in V4_COINS: tag = " [v4]"
    elif coin in TESTNET_BAD: tag = " [bad-testnet]"

    try:
        alt_ohlcv = fetch_binance_15m(symbol, years=3)
        alt_df = build_alt_technicals(alt_ohlcv)

        sig, am = generate_btc_led_signal(
            score_ts, alt_df, threshold=V5_THRESHOLD,
            use_alt_pa_filter=False, spike_mode=None
        )
        mask = (am["ts"] >= pd.Timestamp(OOS_START)) & (am["ts"] <= pd.Timestamp(OOS_END))
        ao = am[mask].reset_index(drop=True)
        so = sig[mask].reset_index(drop=True)

        if len(ao) < 50:
            print(f"  [{i:>3}/{len(ALL_COINS)}] {coin:<12} SKIP (only {len(ao)} bars){tag}")
            failed.append({"coin": coin, "reason": f"too few bars ({len(ao)})"})
            continue

        trades = run_backtest(
            ao, so,
            sl_atr_mult=V5_SL, tp_atr_mult=V5_TP,
            trail_atr_mult=99, trail_activate_atr=99,
            max_hold_bars=V5_MAX_HOLD, cooldown_bars=V5_CD,
        )

        if len(trades) < 5:
            print(f"  [{i:>3}/{len(ALL_COINS)}] {coin:<12} SKIP (only {len(trades)} trades){tag}")
            failed.append({"coin": coin, "reason": f"too few trades ({len(trades)})"})
            continue

        pnl = trades["pnl_net"].sum()
        n = len(trades)
        wr = 100 * (trades["pnl_net"] > 0).sum() / n
        L = trades[trades["dir"] == "L"]
        S = trades[trades["dir"] == "S"]
        eq = 10000 + trades["pnl_net"].cumsum()
        dd = ((eq - eq.cummax()) / eq.cummax() * 100).min()
        ret = trades["pnl_net"] / 1000
        sh = ret.mean() / ret.std() * np.sqrt(n) if ret.std() > 0 else 0

        r = {
            "coin": coin,
            "pnl": round(pnl, 2),
            "trades": n,
            "wr": round(wr, 1),
            "sharpe": round(sh, 2),
            "dd": round(dd, 1),
            "long_n": len(L),
            "long_wr": round(100 * (L["pnl_net"] > 0).sum() / len(L) if len(L) > 0 else 0, 1),
            "long_pnl": round(L["pnl_net"].sum(), 2),
            "short_n": len(S),
            "short_wr": round(100 * (S["pnl_net"] > 0).sum() / len(S) if len(S) > 0 else 0, 1),
            "short_pnl": round(S["pnl_net"].sum(), 2),
            "in_v3": coin in V3_COINS,
            "in_v4": coin in V4_COINS,
            "testnet_bad": coin in TESTNET_BAD,
        }
        results.append(r)

        status = "OK" if pnl > 0 else "NEG"
        print(f"  [{i:>3}/{len(ALL_COINS)}] {coin:<12} {n:>4} trades | WR {wr:>5.1f}% | ${pnl:>+9,.0f} | S {sh:>5.2f} | DD {dd:>5.1f}% [{status}]{tag}")

    except Exception as e:
        print(f"  [{i:>3}/{len(ALL_COINS)}] {coin:<12} FAILED: {e}{tag}")
        failed.append({"coin": coin, "reason": str(e)})

# ── Sort and display ──
results.sort(key=lambda x: x["pnl"], reverse=True)

print("\n" + "=" * 120)
print(f"{'Rank':<5} {'Coin':<12} {'Trades':>6} {'WR%':>6} {'PnL':>10} {'Sharpe':>7} {'DD%':>6} {'L_WR':>6} {'S_WR':>6} {'Status':<15}")
print("-" * 120)
for i, r in enumerate(results, 1):
    status = ""
    if r["in_v3"]: status = "v3"
    elif r["in_v4"]: status = "v4"
    elif r["testnet_bad"]: status = "bad-testnet"
    elif r["pnl"] > 0 and r["sharpe"] > 0: status = "** CANDIDATE **"
    else: status = "negative"
    print(f"{i:<5} {r['coin']:<12} {r['trades']:>6} {r['wr']:>5.1f}% ${r['pnl']:>9,.0f} {r['sharpe']:>7.2f} {r['dd']:>5.1f}% {r['long_wr']:>5.1f}% {r['short_wr']:>5.1f}% {status}")

# ── V5 candidates (not in v3/v4, not testnet-bad, profitable) ──
candidates = [r for r in results
              if not r["in_v3"] and not r["in_v4"] and not r["testnet_bad"]
              and r["pnl"] > 0 and r["sharpe"] > 0]

print(f"\n{'='*80}")
print(f"V5 CANDIDATES (profitable, Sharpe>0, not in v3/v4, not testnet-bad): {len(candidates)}")
print(f"{'='*80}")
for i, r in enumerate(candidates, 1):
    print(f"  {i:>2}. {r['coin']:<12} ${r['pnl']:>+9,.0f} | WR {r['wr']:>5.1f}% | Sharpe {r['sharpe']:>5.2f} | {r['trades']} trades | DD {r['dd']:.1f}%")

# ── Also show how existing v3/v4 coins do with v5 config ──
existing_v5 = [r for r in results if r["in_v3"] or r["in_v4"]]
existing_v5.sort(key=lambda x: x["pnl"], reverse=True)
print(f"\n{'='*80}")
print(f"EXISTING v3/v4 COINS with v5 CONFIG (for reference only):")
print(f"{'='*80}")
for r in existing_v5:
    tag = "v3" if r["in_v3"] else "v4"
    print(f"  [{tag}] {r['coin']:<12} ${r['pnl']:>+9,.0f} | WR {r['wr']:>5.1f}% | Sharpe {r['sharpe']:>5.2f}")

# ── Save ──
output = {
    "config": {"liq": V5_LIQ, "ob": V5_OB, "tick": V5_TICK, "sl": V5_SL, "tp": V5_TP,
               "threshold": V5_THRESHOLD, "oos": f"{OOS_START} to {OOS_END}"},
    "results": results,
    "candidates": candidates,
    "failed": failed,
    "total_coins": len(ALL_COINS),
    "total_tested": len(results),
    "total_profitable": sum(1 for r in results if r["pnl"] > 0),
    "total_candidates": len(candidates),
}
Path(__file__).parent.joinpath("v5_screening_results.json").write_text(
    json.dumps(output, indent=2, default=str, ensure_ascii=False))

print(f"\n{'='*80}")
print(f"SUMMARY")
print(f"  Tested: {len(results)}/{len(ALL_COINS)} coins")
print(f"  Profitable: {sum(1 for r in results if r['pnl'] > 0)}")
print(f"  V5 candidates: {len(candidates)}")
print(f"  Failed: {len(failed)}")
print(f"  Total time: {time.time()-t0:.1f}s")
print(f"  Results saved to tournament/v5_screening_results.json")
print(f"{'='*80}")
