"""
V6 Realistic Portfolio Backtest
================================
Tests v5 vs v6 with realistic conditions:
- Shared equity across all coins
- Fixed $1K per trade (no compound)
- Max 3 concurrent positions
- Higher fees (4bps) and slippage (alts 4bps)
- Funding costs (0.01% per 8h)
- Margin checks

This is the REAL test -- per-coin backtests inflate results.
"""
import sys, os, json, time, warnings
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass

BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR))
os.chdir(str(BASE_DIR))
warnings.filterwarnings("ignore")

import pandas as pd
import numpy as np
from backtest_15m_btc_led_alts import (
    fetch_binance_15m, load_btc_db_data, build_btc_features,
    compute_btc_composite_score, compute_btc_composite_score_v6,
    build_alt_technicals, generate_btc_led_signal,
    V3_EXTRA_WEIGHTS,
)

# Import the portfolio backtest engine
from test_exp_realistic_portfolio import (
    run_portfolio_backtest, compute_metrics,
    INIT_EQUITY, BUDGET_USDT, LEVERAGE, MAX_CONCURRENT,
    FEE_BPS, SLIP_BTC_BPS, SLIP_ALT_BPS, FUNDING_RATE,
)

RESULTS_DIR = Path(__file__).parent
RESULTS_FILE = RESULTS_DIR / "results_v6_portfolio.json"

# Coins
TOURNAMENT_COINS = ["BTC", "XRP", "ADA", "DOT", "SUI", "FIL"]
V3_COINS_EXTRA = ["RENDER", "NEAR", "AXS", "SOL", "ETH", "1000BONK", "ARB"]
V4_COINS = ["OGN", "LTC", "ZRO", "1000PEPE", "HYPE", "PENGU", "LINK"]
V5_COINS_EXTRA = ["GALA", "AAVE", "AVAX", "UNI", "SEI", "DOGE", "ONDO",
                   "1000SHIB", "BNB", "WIF", "CRV", "TAO"]
# For portfolio test, use all available coins
ALL_PORTFOLIO_COINS = TOURNAMENT_COINS + V3_COINS_EXTRA + V4_COINS + V5_COINS_EXTRA

# Override ALL_COINS in the imported module so portfolio engine uses our coin list
import test_exp_realistic_portfolio as portfolio_mod

# Configs per coin
def make_configs(sl=15.0, tp=12.0, cd=4):
    cfgs = {}
    for coin in ALL_PORTFOLIO_COINS:
        thr = 3.0
        if coin == "BTC": thr = 2.5
        elif coin in ("XRP", "ADA"): thr = 3.5
        cfgs[coin] = {
            "threshold": thr, "sl": sl, "tp": tp,
            "cd": cd, "trail": 99, "trail_act": 99,
        }
    return cfgs

V5_CONFIGS = make_configs(sl=15.0, tp=12.0)
V6_CONFIGS = make_configs(sl=25.0, tp=20.0)

# Periods
PERIODS = {
    "BEAR_H2": (pd.Timestamp("2025-07-01"), pd.Timestamp("2025-12-31")),
    "Q1_2026": (pd.Timestamp("2026-01-01"), pd.Timestamp("2026-03-22")),
    "FULL":    (pd.Timestamp("2025-01-01"), pd.Timestamp("2026-03-22")),
}


def main():
    print("=" * 80)
    print("V6 REALISTIC PORTFOLIO BACKTEST")
    print(f"  Init equity: ${INIT_EQUITY:,.0f} | Budget: ${BUDGET_USDT:,.0f}/trade")
    print(f"  Leverage: {LEVERAGE}x | Max concurrent: {MAX_CONCURRENT}")
    print(f"  Fees: {FEE_BPS}bps | Slip alts: {SLIP_ALT_BPS}bps | Funding: {FUNDING_RATE*100:.3f}%/8h")
    print("=" * 80)

    t0 = time.time()

    # ── Load BTC data ──
    print("\n[1] Loading BTC data + factors...")
    btc_ohlcv = fetch_binance_15m("BTCUSDT", years=3)
    db_data = load_btc_db_data()
    btc_df = build_btc_features(btc_ohlcv, db_data)

    # Compute v5 score
    V5_EXTRA = {"ob_combined": 2.0, "tick_liq": 3.0, "basis_contrarian": 1.5}
    V5_CORE = {
        "w_liq_bull": 5.0, "w_liq_bear": 5.0,
        "w_fr_neg": 2.0, "w_fr_pos": 2.0,
        "w_whale_bull": 1.5, "w_whale_bear": 1.5,
        "w_etf_bull": 1.0, "w_etf_bear": 1.0,
        "w_oi_bull": 0.25, "w_oi_capit": 0.25, "w_oi_weak": 0.25, "w_oi_bear": 0.25,
    }
    import backtest_15m_btc_led_alts as bt
    old_extra = dict(bt.V3_EXTRA_WEIGHTS)
    bt.V3_EXTRA_WEIGHTS.update(V5_EXTRA)
    v5_score = compute_btc_composite_score(btc_df, params=dict(V5_CORE))
    bt.V3_EXTRA_WEIGHTS.update(old_extra)
    v5_score_ts = pd.Series(v5_score.values, index=btc_df["ts"].values)

    # Compute v6 scores
    v6_score = compute_btc_composite_score_v6(btc_df)
    v6_score_ts = pd.Series(v6_score.values, index=btc_df["ts"].values)

    v6_agg_score = compute_btc_composite_score_v6(btc_df, velocity_w=5.0)
    v6_agg_score_ts = pd.Series(v6_agg_score.values, index=btc_df["ts"].values)

    print(f"  BTC scores computed ({time.time()-t0:.1f}s)")

    # ── Load alt data ──
    print("\n[2] Loading altcoin data...")
    all_alt_data = {}
    loaded_coins = []
    for coin in ALL_PORTFOLIO_COINS:
        try:
            ohlcv = fetch_binance_15m(f"{coin}USDT", years=3)
            if len(ohlcv) > 500:
                all_alt_data[coin] = build_alt_technicals(ohlcv)
                loaded_coins.append(coin)
        except Exception as e:
            print(f"  SKIP {coin}: {e}")
    print(f"  Loaded {len(loaded_coins)} coins ({time.time()-t0:.1f}s)")

    # Update the module's ALL_COINS to match what we loaded
    portfolio_mod.ALL_COINS = loaded_coins

    # ── Generate signals ──
    def gen_signals(score_ts, configs, coins):
        signals = {}
        data = {}
        for coin in coins:
            if coin not in all_alt_data:
                continue
            cfg = configs[coin]
            sig, am = generate_btc_led_signal(
                score_ts, all_alt_data[coin],
                threshold=cfg["threshold"],
                use_alt_pa_filter=False
            )
            signals[coin] = sig
            data[coin] = am
        return signals, data

    # ── Run scenarios ──
    scenarios = [
        ("v5_8factors", v5_score_ts, V5_CONFIGS),
        ("v6_conservative", v6_score_ts, V6_CONFIGS),
        ("v6_aggressive", v6_agg_score_ts, V6_CONFIGS),
    ]

    all_results = {}

    for scenario_name, score_ts, configs in scenarios:
        print(f"\n{'='*60}")
        print(f"  SCENARIO: {scenario_name}")
        print(f"{'='*60}")

        sigs, sdata = gen_signals(score_ts, configs, loaded_coins)

        for period_name, (p_start, p_end) in PERIODS.items():
            print(f"\n  [{period_name}] {p_start.date()} to {p_end.date()}...")

            trades_df, eq_curve, final_eq = run_portfolio_backtest(
                sigs, sdata, configs, p_start, p_end
            )

            metrics = compute_metrics(trades_df, eq_curve)
            key = f"{scenario_name}_{period_name}"
            all_results[key] = metrics

            # Per-coin
            if not trades_df.empty:
                coin_breakdown = {}
                for coin in loaded_coins:
                    ct = trades_df[trades_df["coin"] == coin]
                    if len(ct) > 0:
                        coin_breakdown[coin] = {
                            "pnl": round(ct["pnl_net"].sum(), 2),
                            "trades": len(ct),
                            "wr": round((ct["pnl_net"] > 0).mean() * 100, 1),
                        }
                all_results[key]["per_coin"] = coin_breakdown

            print(f"    Trades: {metrics['total']} | PnL: ${metrics['pnl']:,.0f} | Return: {metrics['return_pct']:+.1f}%")
            print(f"    Final equity: ${metrics['final_equity']:,.0f} | Max DD: {metrics['max_dd_pct']:.1f}%")
            print(f"    WR: {metrics['win_rate']:.1f}% | Fees: ${metrics['total_fees']:,.0f} | Funding: ${metrics['total_funding']:,.0f}")
            print(f"    Long: {metrics['n_long']} (${metrics['pnl_long']:+,.0f}, WR {metrics['wr_long']:.1f}%)")
            print(f"    Short: {metrics['n_short']} (${metrics['pnl_short']:+,.0f}, WR {metrics['wr_short']:.1f}%)")

    # ── Grand comparison ──
    print(f"\n{'='*80}")
    print("GRAND COMPARISON: REALISTIC PORTFOLIO BACKTEST")
    print(f"{'='*80}")
    print(f"\n  {'Scenario':<25} {'Period':>8} {'PnL':>10} {'Ret%':>7} {'DD%':>6} {'Tr':>5} {'WR':>5} {'Fees':>8} {'Fund':>8}")
    print("  " + "-" * 95)

    for key in sorted(all_results.keys()):
        r = all_results[key]
        parts = key.rsplit("_", 1)
        scenario = "_".join(key.split("_")[:-1])
        period = key.split("_")[-1]
        print(f"  {scenario:<25} {period:>8} ${r['pnl']:>9,.0f} {r['return_pct']:>+6.1f}% "
              f"{r['max_dd_pct']:>5.1f}% {r['total']:>5} {r['win_rate']:>4.1f}% "
              f"${r['total_fees']:>7,.0f} ${r['total_funding']:>7,.0f}")

    # v5 vs v6 delta
    print(f"\n  v5 vs v6 FULL period delta:")
    v5_full = all_results.get("v5_8factors_FULL", {})
    v6_full = all_results.get("v6_conservative_FULL", {})
    v6a_full = all_results.get("v6_aggressive_FULL", {})
    if v5_full and v6_full:
        print(f"    v5:  ${v5_full.get('pnl',0):>9,.0f} (return {v5_full.get('return_pct',0):+.1f}%)")
        print(f"    v6c: ${v6_full.get('pnl',0):>9,.0f} (return {v6_full.get('return_pct',0):+.1f}%) "
              f"[${v6_full.get('pnl',0)-v5_full.get('pnl',0):+,.0f}]")
        if v6a_full:
            print(f"    v6a: ${v6a_full.get('pnl',0):>9,.0f} (return {v6a_full.get('return_pct',0):+.1f}%) "
                  f"[${v6a_full.get('pnl',0)-v5_full.get('pnl',0):+,.0f}]")

    # Save
    results_data = {
        "test": "v6_realistic_portfolio",
        "timestamp": datetime.utcnow().isoformat(),
        "params": {
            "init_equity": INIT_EQUITY,
            "leverage": LEVERAGE,
            "max_concurrent": MAX_CONCURRENT,
            "budget_per_trade": BUDGET_USDT,
            "fee_bps": FEE_BPS,
            "coins": len(loaded_coins),
        },
        "results": all_results,
    }
    RESULTS_FILE.write_text(json.dumps(results_data, indent=2, default=str, ensure_ascii=False))

    exp_file = BASE_DIR / "experiments" / "v6_portfolio_20260322.json"
    exp_file.write_text(json.dumps(results_data, indent=2, default=str, ensure_ascii=False))

    print(f"\nResults saved to {RESULTS_FILE}")
    print(f"Time: {(time.time()-t0)/60:.1f}min")


if __name__ == "__main__":
    main()
