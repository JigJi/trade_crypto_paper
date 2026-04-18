"""
Realistic Portfolio Backtest (Fixed Size, No Compound)
======================================================
Addresses key backtest-to-live gaps:
  1. Portfolio-level shared equity across all coins
  2. FIXED position size ($1,000 * leverage) -- no snowball/compound
  3. Max concurrent positions enforced across all coins (max 3)
  4. Funding rate costs (~0.01% every 8 hours = 32 bars of 15m)
  5. Higher slippage for altcoins (BTC 2bps, alts 4bps)
  6. Margin check before opening (need >= notional/leverage free margin)
  7. Proper equity curve with drawdown
  8. Stop trading if equity < $1,000

Tests: v3 baseline vs v3+short_bias(0.5/1.0) on BULL, BEAR, FULL periods.
"""

import os, sys, json, warnings
from datetime import datetime
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

sys.path.insert(0, r"D:\0_product_dev\trade_crypto")
from backtest_15m_btc_led_alts import (
    fetch_binance_15m, build_alt_technicals, calc_metrics,
    BKK_UTC_OFFSET,
)
from test_v12_improvements import (
    V11_CONFIGS, ALL_COINS, generate_signal_v11, generate_signal_short_bias,
)
from paper_trading.strategy import (
    compute_btc_composite_score, build_btc_features,
)
from test_phase1_factors import load_btc_db_data_v3

# ---- Realistic constants ----
INIT_EQUITY     = 10_000.0
BUDGET_USDT     = 1_000.0  # FIXED per-trade budget (no compound)
LEVERAGE        = 2.0
MAX_CONCURRENT  = 3        # max open positions at any time
FEE_BPS         = 4.0      # taker fee (Binance default tier, round-trip ~8bps)
SLIP_BTC_BPS    = 2.0      # BTC slippage
SLIP_ALT_BPS    = 4.0      # altcoin slippage (less liquid)
FUNDING_RATE    = 0.0001   # 0.01% per 8h (Binance default)
FUNDING_BARS    = 32       # 8h / 15m = 32 bars between funding
MIN_EQUITY      = 1_000.0  # stop trading if equity below this


@dataclass
class Position:
    coin: str
    direction: int       # 1=long, -1=short
    entry_price: float
    entry_atr: float
    qty: float
    notional: float
    entry_bar: int
    entry_time: object
    sl_level: float
    tp_level: float
    trail_mult: float
    trail_act_mult: float
    peak: float = 0.0
    trough: float = 999999.0
    trail_active: bool = False
    funding_paid: float = 0.0
    last_funding_bar: int = 0


def run_portfolio_backtest(all_signals, all_data, configs, period_start, period_end):
    """
    Run portfolio-level backtest with shared equity across all coins.

    all_signals: dict[coin] -> pd.Series of signals (1, -1, 0)
    all_data: dict[coin] -> pd.DataFrame with ts, open, high, low, close, atr
    configs: dict[coin] -> cfg dict
    """
    equity = INIT_EQUITY
    positions = {}  # coin -> Position
    trades = []
    equity_curve = [INIT_EQUITY]

    # Build unified timeline from all coins
    all_times = set()
    for coin, df in all_data.items():
        mask = (df["ts"] >= period_start) & (df["ts"] <= period_end)
        for t in df.loc[mask, "ts"]:
            all_times.add(t)
    timeline = sorted(all_times)

    if not timeline:
        return pd.DataFrame(), equity_curve, equity

    # Build bar lookups per coin
    coin_bars = {}
    coin_sigs = {}
    for coin in ALL_COINS:
        if coin not in all_data:
            continue
        df = all_data[coin].copy()
        df = df[(df["ts"] >= period_start) & (df["ts"] <= period_end)].copy()
        df = df.set_index("ts").sort_index()
        coin_bars[coin] = df

        sig = all_signals[coin].copy()
        # Apply shift(1) for anti-lookahead
        sig = sig.shift(1).fillna(0).astype(int)
        sig.index = all_data[coin].set_index("ts").sort_index().index
        sig = sig[(sig.index >= period_start) & (sig.index <= period_end)]
        coin_sigs[coin] = sig

    cooldown_tracker = {coin: -999 for coin in ALL_COINS}
    bar_idx = 0

    for ts in timeline:
        bar_idx += 1

        # ---- 1. Process EXITS for all open positions ----
        coins_to_close = []
        for coin, pos in positions.items():
            if coin not in coin_bars or ts not in coin_bars[coin].index:
                continue
            bar = coin_bars[coin].loc[ts]
            h, l, c, o = bar["high"], bar["low"], bar["close"], bar["open"]

            # Funding cost every 32 bars (8h)
            bars_held = bar_idx - pos.entry_bar
            if bars_held > 0 and bars_held % FUNDING_BARS == 0:
                funding_cost = pos.notional * FUNDING_RATE
                pos.funding_paid += funding_cost

            # Update peak/trough
            if pos.direction == 1:
                pos.peak = max(pos.peak, h)
            else:
                pos.trough = min(pos.trough, l)

            # Check exit conditions
            exit_px = exit_reason = None
            atr = pos.entry_atr

            # Trailing stop
            trail_stop = None
            if pos.trail_mult < 50:
                if pos.direction == 1 and (pos.peak - pos.entry_price) >= pos.trail_act_mult * atr:
                    pos.trail_active = True
                    trail_stop = pos.peak - pos.trail_mult * atr
                elif pos.direction == -1 and (pos.entry_price - pos.trough) >= pos.trail_act_mult * atr:
                    pos.trail_active = True
                    trail_stop = pos.trough + pos.trail_mult * atr

            if pos.direction == 1:
                if l <= pos.sl_level: exit_px, exit_reason = pos.sl_level, "SL"
                elif pos.trail_active and trail_stop and l <= trail_stop: exit_px, exit_reason = trail_stop, "TRAIL"
                elif h >= pos.tp_level: exit_px, exit_reason = pos.tp_level, "TP"
            else:
                if h >= pos.sl_level: exit_px, exit_reason = pos.sl_level, "SL"
                elif pos.trail_active and trail_stop and h >= trail_stop: exit_px, exit_reason = trail_stop, "TRAIL"
                elif l <= pos.tp_level: exit_px, exit_reason = pos.tp_level, "TP"

            # Timeout
            if exit_px is None and bars_held >= 96:
                exit_px, exit_reason = c, "TIMEOUT"

            # Signal flip
            if exit_px is None and coin in coin_sigs and ts in coin_sigs[coin].index:
                new_sig = coin_sigs[coin].loc[ts]
                if new_sig != 0 and new_sig != pos.direction:
                    exit_px, exit_reason = o, "SIGNAL_FLIP"

            if exit_px is not None:
                slip_bps = SLIP_BTC_BPS if coin == "BTC" else SLIP_ALT_BPS
                slip = slip_bps / 10_000
                fee = FEE_BPS / 10_000

                exit_px_f = exit_px * (1 - slip) if pos.direction == 1 else exit_px * (1 + slip)
                fee_in = pos.entry_price * pos.qty * fee
                fee_out = exit_px_f * pos.qty * fee
                pnl_gross = (exit_px_f - pos.entry_price) * pos.qty * pos.direction
                pnl_net = pnl_gross - fee_in - fee_out - pos.funding_paid

                equity += pnl_net
                cooldown_tracker[coin] = bar_idx

                trades.append({
                    "coin": coin,
                    "entry_time": pos.entry_time,
                    "exit_time": ts,
                    "dir": "L" if pos.direction == 1 else "S",
                    "entry_price": pos.entry_price,
                    "exit_price": exit_px_f,
                    "qty": pos.qty,
                    "notional": pos.notional,
                    "pnl_gross": round(pnl_gross, 2),
                    "fees": round(fee_in + fee_out, 2),
                    "funding": round(pos.funding_paid, 2),
                    "pnl_net": round(pnl_net, 2),
                    "exit_reason": exit_reason,
                    "holding_bars": bars_held,
                    "equity_after": round(equity, 2),
                })
                coins_to_close.append(coin)

        for coin in coins_to_close:
            del positions[coin]

        # ---- 2. Process ENTRIES (if under max concurrent and equity sufficient) ----
        if equity < MIN_EQUITY:
            equity_curve.append(equity)
            continue

        if len(positions) >= MAX_CONCURRENT:
            equity_curve.append(equity)
            continue

        # Priority: check all coins for signals, pick by signal strength (score)
        candidates = []
        for coin in ALL_COINS:
            if coin in positions:
                continue
            if coin not in coin_sigs or ts not in coin_sigs[coin].index:
                continue
            if coin not in coin_bars or ts not in coin_bars[coin].index:
                continue

            sig = coin_sigs[coin].loc[ts]
            if sig == 0:
                continue

            cfg = configs[coin]
            cd = cfg["cd"]
            if (bar_idx - cooldown_tracker.get(coin, -999)) <= cd:
                continue

            candidates.append((coin, sig))

        # Open positions for candidates (up to max_concurrent)
        for coin, sig in candidates:
            if len(positions) >= MAX_CONCURRENT:
                break

            bar = coin_bars[coin].loc[ts]
            raw_px = bar["open"]
            cur_atr = bar["atr"]

            if raw_px <= 0 or np.isnan(cur_atr) or cur_atr <= 0:
                continue

            cfg = configs[coin]

            # FIXED position sizing -- no compound/snowball
            notional = BUDGET_USDT * LEVERAGE
            qty = notional / raw_px

            # Margin check: need notional/leverage free margin
            margin_required = notional / LEVERAGE
            used_margin = sum(p.notional / LEVERAGE for p in positions.values())
            free_margin = equity - used_margin

            if margin_required > free_margin:
                continue

            # Apply slippage
            slip_bps = SLIP_BTC_BPS if coin == "BTC" else SLIP_ALT_BPS
            slip = slip_bps / 10_000
            entry_px = raw_px * (1 + slip) if sig == 1 else raw_px * (1 - slip)

            # SL/TP levels
            if sig == 1:
                sl_level = entry_px - cfg["sl"] * cur_atr
                tp_level = entry_px + cfg["tp"] * cur_atr
            else:
                sl_level = entry_px + cfg["sl"] * cur_atr
                tp_level = entry_px - cfg["tp"] * cur_atr

            positions[coin] = Position(
                coin=coin, direction=sig,
                entry_price=entry_px, entry_atr=cur_atr,
                qty=qty, notional=notional,
                entry_bar=bar_idx, entry_time=ts,
                sl_level=sl_level, tp_level=tp_level,
                trail_mult=cfg["trail"], trail_act_mult=cfg["trail_act"],
                peak=entry_px, trough=entry_px,
                last_funding_bar=bar_idx,
            )

        equity_curve.append(equity)

    return pd.DataFrame(trades), equity_curve, equity


def compute_metrics(trades_df, equity_curve):
    """Compute realistic portfolio metrics."""
    if trades_df.empty:
        return {"total": 0, "pnl": 0, "return_pct": 0, "max_dd_pct": 0}

    total_pnl = trades_df["pnl_net"].sum()
    total_fees = trades_df["fees"].sum()
    total_funding = trades_df["funding"].sum()
    n = len(trades_df)
    wins = trades_df[trades_df["pnl_net"] > 0]
    losses = trades_df[trades_df["pnl_net"] < 0]
    wr = len(wins) / n * 100 if n > 0 else 0

    # Equity curve metrics
    eq = pd.Series(equity_curve)
    running_max = eq.cummax()
    drawdown = (eq - running_max) / running_max
    max_dd = drawdown.min() * 100

    # Return
    final_equity = equity_curve[-1] if equity_curve else INIT_EQUITY
    return_pct = (final_equity - INIT_EQUITY) / INIT_EQUITY * 100

    # Per-direction
    longs = trades_df[trades_df["dir"] == "L"]
    shorts = trades_df[trades_df["dir"] == "S"]

    return {
        "total": n,
        "pnl": round(total_pnl, 2),
        "return_pct": round(return_pct, 1),
        "final_equity": round(final_equity, 2),
        "max_dd_pct": round(max_dd, 1),
        "win_rate": round(wr, 1),
        "total_fees": round(total_fees, 2),
        "total_funding": round(total_funding, 2),
        "avg_pnl": round(trades_df["pnl_net"].mean(), 2),
        "n_long": len(longs), "n_short": len(shorts),
        "pnl_long": round(longs["pnl_net"].sum(), 2) if len(longs) > 0 else 0,
        "pnl_short": round(shorts["pnl_net"].sum(), 2) if len(shorts) > 0 else 0,
        "wr_long": round(len(longs[longs["pnl_net"] > 0]) / max(len(longs), 1) * 100, 1),
        "wr_short": round(len(shorts[shorts["pnl_net"] > 0]) / max(len(shorts), 1) * 100, 1),
    }


def main():
    print("=" * 70)
    print("REALISTIC PORTFOLIO BACKTEST (Fixed Size, No Compound)")
    print(f"  Init equity: ${INIT_EQUITY:,.0f} | Budget: ${BUDGET_USDT:,.0f}/trade")
    print(f"  Leverage: {LEVERAGE}x | Notional: ${BUDGET_USDT*LEVERAGE:,.0f}/trade")
    print(f"  Max concurrent: {MAX_CONCURRENT} (max exposure ${MAX_CONCURRENT*BUDGET_USDT*LEVERAGE:,.0f})")
    print(f"  Fees: {FEE_BPS} bps | Slip BTC: {SLIP_BTC_BPS} bps | Slip alts: {SLIP_ALT_BPS} bps")
    print(f"  Funding: {FUNDING_RATE*100:.3f}% per 8h")
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

    # ---- 2. Load alt data ----
    print("\n[2] Loading altcoin data...")
    all_data = {}
    for coin in ALL_COINS:
        symbol = f"{coin}USDT"
        alt_raw = fetch_binance_15m(symbol, years=3)
        alt_df = build_alt_technicals(alt_raw)
        all_data[coin] = alt_df
        print(f"    {coin}: {len(alt_df):,} bars")

    # ---- 3. Define test periods ----
    periods = {
        "BULL": (pd.Timestamp("2025-06-01"), pd.Timestamp("2025-12-31")),
        "BEAR": (pd.Timestamp("2026-01-01"), pd.Timestamp("2026-03-10")),
        "FULL": (pd.Timestamp("2025-06-01"), pd.Timestamp("2026-03-10")),
    }

    # ---- 4. Test scenarios ----
    scenarios = {
        "v3_baseline": {
            "signal_fn": generate_signal_v11,
            "signal_kwargs": {},
        },
        "v3_short_bias_05": {
            "signal_fn": generate_signal_short_bias,
            "signal_kwargs": {"short_offset": 0.5},
        },
        "v3_short_bias_10": {
            "signal_fn": generate_signal_short_bias,
            "signal_kwargs": {"short_offset": 1.0},
        },
    }

    all_results = {}

    for scenario_name, scenario_def in scenarios.items():
        print(f"\n{'='*50}")
        print(f"  SCENARIO: {scenario_name}")
        print(f"{'='*50}")

        # Generate signals for all coins
        all_signals = {}
        for coin in ALL_COINS:
            cfg = V11_CONFIGS[coin]
            sig, _ = scenario_def["signal_fn"](
                btc_score_ts, all_data[coin],
                cfg["threshold"], cfg["alt_pa"],
                **scenario_def["signal_kwargs"]
            )
            # Align signal index with data
            all_signals[coin] = sig

        for period_name, (p_start, p_end) in periods.items():
            print(f"\n  [{period_name}] {p_start.date()} to {p_end.date()}...")

            trades_df, eq_curve, final_eq = run_portfolio_backtest(
                all_signals, all_data, V11_CONFIGS, p_start, p_end
            )

            metrics = compute_metrics(trades_df, eq_curve)

            key = f"{scenario_name}_{period_name}"
            all_results[key] = metrics

            # Per-coin breakdown
            coin_breakdown = {}
            if not trades_df.empty:
                for coin in ALL_COINS:
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
            if coin_breakdown:
                for coin, cb in coin_breakdown.items():
                    print(f"      {coin}: ${cb['pnl']:+,.0f} ({cb['trades']} tr, WR {cb['wr']:.1f}%)")

    # ---- 5. Comparison table ----
    print(f"\n{'='*70}")
    print(f"  REALISTIC vs ORIGINAL COMPARISON")
    print(f"{'='*70}")

    # Compare old vs realistic for each scenario/period
    print(f"\n  {'Scenario':<22s} {'Period':>5s} {'PnL':>10s} {'Return%':>9s} {'MaxDD%':>8s} {'Trades':>7s} {'Fees':>8s} {'Funding':>9s}")
    print(f"  {'-'*22} {'-'*5} {'-'*10} {'-'*9} {'-'*8} {'-'*7} {'-'*8} {'-'*9}")
    for key in sorted(all_results.keys()):
        r = all_results[key]
        parts = key.rsplit("_", 1)
        scenario = parts[0]
        period = parts[1]
        print(f"  {scenario:<22s} {period:>5s} ${r['pnl']:>9,.0f} {r['return_pct']:>+8.1f}% {r['max_dd_pct']:>7.1f}% {r['total']:>7d} ${r['total_fees']:>7,.0f} ${r['total_funding']:>8,.0f}")

    print(f"\n  NOTE: Original backtest used fixed $1k/trade, no funding, 1.5bps slip,")
    print(f"  no max concurrent enforcement. Realistic numbers will be LOWER.")
    print(f"{'='*70}")

    # ---- 6. Save ----
    exp_id = f"realistic_portfolio_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    result = {
        "experiment_id": exp_id,
        "description": "Realistic portfolio backtest: shared equity, compound sizing, funding, higher fees",
        "params": {
            "init_equity": INIT_EQUITY,
            "leverage": LEVERAGE,
            "max_concurrent": MAX_CONCURRENT,
            "budget_per_trade": BUDGET_USDT,
            "fee_bps": FEE_BPS,
            "slip_btc_bps": SLIP_BTC_BPS,
            "slip_alt_bps": SLIP_ALT_BPS,
            "funding_rate_8h": FUNDING_RATE,
        },
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
