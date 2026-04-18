"""
Per-Coin Momentum Gate + ATR Sizing Backtest
=============================================
Problem: BTC signal determines direction for ALL coins, but not all coins
follow BTC. Paper trading shows 14/28 coins lost money despite correct signal.

Tests:
  Section A: Multi-month OOS backtest (Jan-Mar 2026, 6 original coins)
    1. baseline      -- current v3 (no filter, fixed sizing)
    2. pa_ema        -- existing PA filter (close > EMA9 > EMA21 + vol > 0.8)
    3. momentum_5bar -- 5-bar return confirms BTC direction
    4. momentum_rsi  -- RSI(14) > 50 for LONG, < 50 for SHORT
    5. atr_sizing    -- position size inversely proportional to ATR%
    6. mom5+atr      -- best momentum gate + ATR sizing combined

  Section B: Paper replay (03-17 16:46 to 03-18 09:46 UTC)
    Fetch OHLCV for all coins, compute indicators at entry bar,
    show which coins would have been filtered + hypothetical PnL.
"""

import os, sys, json, warnings, sqlite3
from datetime import datetime

import numpy as np
import pandas as pd
import pandas_ta as pta

warnings.filterwarnings("ignore")

# ---- Import from existing codebase ----
from backtest_15m_btc_led_alts import (
    fetch_binance_15m, load_btc_db_data, build_btc_features,
    compute_btc_composite_score, build_alt_technicals,
    run_backtest, calc_metrics,
    BKK_UTC_OFFSET, INIT_EQUITY, FEE, SLIP,
    COMPOSITE_WEIGHTS, V3_EXTRA_WEIGHTS,
)
import backtest_15m_btc_led_alts as bt

# ---- Configs ----
# Original 6 coins use grid-searched params (SL/TP updated to current production)
COIN_CONFIGS_6 = {
    "BTC": {"threshold": 2.5, "alt_pa": False, "sl": 10.0, "tp": 5.0, "trail": 99, "trail_act": 99, "cd": 4},
    "XRP": {"threshold": 3.5, "alt_pa": False, "sl": 10.0, "tp": 5.0, "trail": 99, "trail_act": 99, "cd": 4},
    "ADA": {"threshold": 3.5, "alt_pa": False, "sl": 10.0, "tp": 5.0, "trail": 99, "trail_act": 99, "cd": 4},
    "DOT": {"threshold": 3.0, "alt_pa": False, "sl": 10.0, "tp": 5.0, "trail": 99, "trail_act": 99, "cd": 8},
    "SUI": {"threshold": 3.0, "alt_pa": False, "sl": 10.0, "tp": 5.0, "trail": 99, "trail_act": 99, "cd": 4},
    "FIL": {"threshold": 3.0, "alt_pa": False, "sl": 10.0, "tp": 5.0, "trail": 99, "trail_act": 99, "cd": 4},
}

ALL_COINS = ["BTC", "XRP", "ADA", "DOT", "SUI", "FIL"]
TEST_START = pd.Timestamp("2026-01-01")

# All 31 paper trading coins
PAPER_COINS_V3 = [
    "BTC", "XRP", "ADA", "DOT", "SUI", "FIL",
    "RENDER", "BEAT", "PIXEL", "NEAR", "AXS", "SOL",
    "ETH", "1000BONK", "ARB", "ARIA", "BARD", "BANANAS31", "PIPPIN",
]
PAPER_COINS_V4 = [
    "OGN", "SAHARA", "ASTER", "LTC", "ZRO", "NAORIS",
    "1000PEPE", "JCT", "DEGO", "HYPE", "PENGU", "LINK",
]
PAPER_COINS = PAPER_COINS_V3 + PAPER_COINS_V4

# BTC score weights (from test_v12_improvements.py -- used in v1.1 backtest)
BTC_SCORE_WEIGHTS = {
    "w_oi_bull": 0.5, "w_oi_capit": 0.5, "w_oi_weak": 0.5, "w_oi_bear": 0.5,
    "w_taker_strong": 1.5, "w_taker_mild": 0.5, "w_ls_extreme": 1.5,
    "w_fr_neg": 2.0, "w_fr_pos": 2.0, "w_fg_fear": 2.0, "w_fg_mild_fear": 1.0,
    "w_fg_greed": 2.0, "w_fg_mild_greed": 1.0, "w_whale_bull": 1.5, "w_whale_bear": 1.5,
    "w_liq_bull": 2.0, "w_liq_bear": 2.0, "w_etf_bull": 1.5, "w_etf_bear": 1.5,
}


# ====================================================================
# Section A: Signal generation with momentum gates
# ====================================================================

def generate_signal_baseline(btc_score_ts, alt_df, threshold, use_alt_pa):
    """v3 baseline: BTC score threshold, no per-coin filter."""
    btc_score_df = btc_score_ts.reset_index()
    btc_score_df.columns = ["ts", "btc_score"]
    alt = alt_df.copy().sort_values("ts")
    alt = pd.merge_asof(alt, btc_score_df.sort_values("ts"),
                        on="ts", direction="backward", tolerance=pd.Timedelta("30min"))
    alt["btc_score"] = alt["btc_score"].fillna(0)

    signal = pd.Series(0, index=alt.index)
    signal[alt["btc_score"] >= threshold] = 1
    signal[alt["btc_score"] <= -threshold] = -1

    return signal, alt


def generate_signal_pa_ema(btc_score_ts, alt_df, threshold, use_alt_pa):
    """PA EMA filter: close > EMA9 > EMA21 + vol > 0.8 for LONG (reversed for SHORT)."""
    signal, alt = generate_signal_baseline(btc_score_ts, alt_df, threshold, use_alt_pa)

    alt_bull = (alt["close"] > alt["ema9"]) & (alt["ema9"] > alt["ema21"])
    alt_bear = (alt["close"] < alt["ema9"]) & (alt["ema9"] < alt["ema21"])
    vol_ok = alt["vol_ratio"] > 0.8

    signal[(signal == 1) & ~(alt_bull & vol_ok)] = 0
    signal[(signal == -1) & ~(alt_bear & vol_ok)] = 0

    return signal, alt


def generate_signal_momentum_5bar(btc_score_ts, alt_df, threshold, use_alt_pa):
    """5-bar momentum gate: coin's 5-bar return must confirm BTC direction."""
    signal, alt = generate_signal_baseline(btc_score_ts, alt_df, threshold, use_alt_pa)

    mom5 = alt["close"].pct_change(5)
    signal[(signal == 1) & ~(mom5 > 0)] = 0
    signal[(signal == -1) & ~(mom5 < 0)] = 0

    return signal, alt


def generate_signal_momentum_rsi(btc_score_ts, alt_df, threshold, use_alt_pa):
    """RSI(14) momentum gate: RSI > 50 for LONG, < 50 for SHORT."""
    signal, alt = generate_signal_baseline(btc_score_ts, alt_df, threshold, use_alt_pa)

    rsi = pta.rsi(alt["close"], length=14)
    signal[(signal == 1) & ~(rsi > 50)] = 0
    signal[(signal == -1) & ~(rsi < 50)] = 0

    return signal, alt


# ====================================================================
# Modified run_backtest with ATR-based sizing
# ====================================================================

def run_backtest_atr_sizing(df, signals, sl_atr_mult=2.0, tp_atr_mult=3.0,
                            trail_atr_mult=0.5, trail_activate_atr=0.5,
                            max_hold_bars=96, cooldown_bars=4,
                            stale_exit_bars=None, median_atr_pct=0.015):
    """
    Same as run_backtest but with ATR-proportional position sizing.
    Volatile coins get smaller positions, quiet coins get larger.
    size_mult = median_atr_pct / atr_pct, capped at [0.5, 2.0].
    """
    sig = signals.shift(1).fillna(0).astype(int).values
    atrs = df["atr"].values
    opens = df["open"].values
    highs = df["high"].values
    lows = df["low"].values
    closes = df["close"].values
    times = df["ts"].values

    n = len(df)
    records = []
    equity = INIT_EQUITY
    position = 0
    entry_i = entry_px = entry_atr = qty = fee_in = 0
    peak = trough = 0.0
    trl_active = False
    last_exit_i = -cooldown_bars - 1

    for i in range(n):
        if position == 0 and sig[i] != 0 and (i - last_exit_i) > cooldown_bars:
            raw_px = opens[i]
            cur_atr = atrs[i] if not np.isnan(atrs[i]) else 0
            if raw_px <= 0 or cur_atr <= 0 or cur_atr / raw_px < 0.0005:
                continue

            # ATR-proportional sizing
            atr_pct = cur_atr / raw_px
            size_mult = np.clip(median_atr_pct / atr_pct, 0.5, 2.0)
            qty = (bt.BUDGET_USDT * bt.LEVERAGE * size_mult) / raw_px

            entry_px = raw_px * (1 + SLIP) if sig[i] == 1 else raw_px * (1 - SLIP)
            entry_atr = cur_atr
            fee_in = entry_px * qty * FEE
            position = sig[i]
            entry_i = i
            peak = entry_px
            trough = entry_px
            trl_active = False
            continue

        if position != 0:
            h, l, c, o = highs[i], lows[i], closes[i], opens[i]
            atr = entry_atr
            if position == 1:
                peak = max(peak, h)
                sl_level = entry_px - sl_atr_mult * atr
                tp_level = entry_px + tp_atr_mult * atr
            else:
                trough = min(trough, l)
                sl_level = entry_px + sl_atr_mult * atr
                tp_level = entry_px - tp_atr_mult * atr

            trail_stop = None
            if trail_atr_mult < 50:
                if position == 1 and (peak - entry_px) >= trail_activate_atr * atr:
                    trl_active = True
                    trail_stop = peak - trail_atr_mult * atr
                elif position == -1 and (entry_px - trough) >= trail_activate_atr * atr:
                    trl_active = True
                    trail_stop = trough + trail_atr_mult * atr

            exit_px = exit_reason = None
            if position == 1:
                if l <= sl_level: exit_px, exit_reason = sl_level, "SL"
                elif trl_active and trail_stop and l <= trail_stop: exit_px, exit_reason = trail_stop, "TRAIL"
                elif h >= tp_level: exit_px, exit_reason = tp_level, "TP"
            else:
                if h >= sl_level: exit_px, exit_reason = sl_level, "SL"
                elif trl_active and trail_stop and h >= trail_stop: exit_px, exit_reason = trail_stop, "TRAIL"
                elif l <= tp_level: exit_px, exit_reason = tp_level, "TP"

            if exit_px is None and stale_exit_bars is not None and (i - entry_i) >= stale_exit_bars:
                unrealized_pct = abs((c - entry_px) / entry_px)
                if unrealized_pct < 0.001:
                    exit_px, exit_reason = c, "STALE"

            if exit_px is None and (i - entry_i) >= max_hold_bars:
                exit_px, exit_reason = c, "TIMEOUT"
            if exit_px is None and sig[i] != 0 and sig[i] != position:
                exit_px, exit_reason = o, "SIGNAL_FLIP"

            if exit_px is not None:
                exit_px_f = exit_px * (1 - SLIP) if position == 1 else exit_px * (1 + SLIP)
                fee_out = exit_px_f * qty * FEE
                pnl_gross = (exit_px_f - entry_px) * qty * position
                pnl_net = pnl_gross - fee_in - fee_out
                equity += pnl_net
                records.append({
                    "entry_idx": entry_i, "exit_idx": i,
                    "entry_time": times[entry_i], "exit_time": times[i],
                    "dir": "L" if position == 1 else "S",
                    "entry_price": entry_px, "exit_price": exit_px_f,
                    "qty": qty, "pnl_net": pnl_net,
                    "equity_after": equity, "exit_reason": exit_reason,
                    "holding_bars": i - entry_i,
                })
                last_exit_i = i
                position = 0

    return pd.DataFrame(records)


# ====================================================================
# Run one coin scenario
# ====================================================================

def run_coin(coin, btc_score_ts, alt_test, cfg, signal_fn, use_atr_sizing=False,
             median_atr_pct=0.015):
    """Run backtest for one coin with a given signal function and optional ATR sizing."""
    signals, alt_merged = signal_fn(btc_score_ts, alt_test, cfg["threshold"], cfg["alt_pa"])

    if use_atr_sizing:
        trades = run_backtest_atr_sizing(
            alt_merged, signals,
            sl_atr_mult=cfg["sl"], tp_atr_mult=cfg["tp"],
            trail_atr_mult=cfg["trail"], trail_activate_atr=cfg["trail_act"],
            cooldown_bars=cfg["cd"], max_hold_bars=96,
            median_atr_pct=median_atr_pct,
        )
    else:
        trades = run_backtest(
            alt_merged, signals,
            sl_atr_mult=cfg["sl"], tp_atr_mult=cfg["tp"],
            trail_atr_mult=cfg["trail"], trail_activate_atr=cfg["trail_act"],
            cooldown_bars=cfg["cd"], max_hold_bars=96,
        )

    m = calc_metrics(trades, len(alt_test))
    return trades, m


# ====================================================================
# Compute median ATR% across all coins for ATR sizing reference
# ====================================================================

def compute_median_atr_pct(alt_data_dict):
    """Compute the median ATR% across all coins and all bars."""
    all_atr_pcts = []
    for coin, df in alt_data_dict.items():
        atr_pct = (df["atr"] / df["close"]).dropna()
        all_atr_pcts.extend(atr_pct.tolist())
    return np.median(all_atr_pcts) if all_atr_pcts else 0.015


# ====================================================================
# Section B: Paper Replay
# ====================================================================

def load_paper_trades(db_path="paper_trading/state/paper_trades.db"):
    """Load paper trades from SQLite."""
    conn = sqlite3.connect(db_path)
    df = pd.read_sql("SELECT * FROM trades ORDER BY entry_time", conn)
    conn.close()
    if df.empty:
        return df
    df["entry_time"] = pd.to_datetime(df["entry_time"])
    df["exit_time"] = pd.to_datetime(df["exit_time"])
    return df


def compute_coin_indicators(ohlcv_df, bar_idx=-2):
    """
    Compute momentum indicators at a specific bar (default: -2 for anti-lookahead,
    i.e. the bar BEFORE the entry bar).

    Returns dict with indicator values.
    """
    df = ohlcv_df.copy()
    if len(df) < 30:
        return None

    close = df["close"].values
    high = df["high"].values
    low = df["low"].values

    # Use the bar at bar_idx
    idx = bar_idx if bar_idx >= 0 else len(df) + bar_idx
    if idx < 21:
        return None

    # 5-bar momentum
    mom5 = (close[idx] - close[idx - 5]) / close[idx - 5] if close[idx - 5] > 0 else 0

    # EMA9, EMA21 at bar
    ema9 = pd.Series(close).ewm(span=9, adjust=False).mean().iloc[idx]
    ema21 = pd.Series(close).ewm(span=21, adjust=False).mean().iloc[idx]
    ema_aligned_long = close[idx] > ema9 and ema9 > ema21
    ema_aligned_short = close[idx] < ema9 and ema9 < ema21

    # Vol ratio
    vol = df["volume"].values
    vol_ma20 = pd.Series(vol).rolling(20).mean().iloc[idx]
    vol_ratio = vol[idx] / vol_ma20 if vol_ma20 > 0 else 0

    # RSI(14)
    rsi_series = pta.rsi(pd.Series(close), length=14)
    rsi_val = rsi_series.iloc[idx] if not pd.isna(rsi_series.iloc[idx]) else 50

    # ATR and ATR%
    atr_series = pta.atr(pd.Series(high), pd.Series(low), pd.Series(close), length=14)
    atr_val = atr_series.iloc[idx] if not pd.isna(atr_series.iloc[idx]) else 0
    atr_pct = atr_val / close[idx] if close[idx] > 0 else 0

    return {
        "close": close[idx],
        "mom5": mom5,
        "mom5_long": mom5 > 0,
        "mom5_short": mom5 < 0,
        "ema9": ema9,
        "ema21": ema21,
        "ema_aligned_long": ema_aligned_long,
        "ema_aligned_short": ema_aligned_short,
        "vol_ratio": vol_ratio,
        "vol_ok": vol_ratio > 0.8,
        "rsi": rsi_val,
        "rsi_long": rsi_val > 50,
        "rsi_short": rsi_val < 50,
        "atr": atr_val,
        "atr_pct": atr_pct,
    }


# ====================================================================
# Save experiment
# ====================================================================

def save_experiment(results, paper_results, median_atr_pct):
    """Save experiment to experiments/ directory."""
    os.makedirs("experiments", exist_ok=True)
    exp_id = f"coin_filter_{datetime.now().strftime('%Y%m%d_%H%M')}"

    # Build summary
    summary = {
        "experiment_id": exp_id,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "description": "Per-coin momentum gate + ATR sizing backtest",
        "median_atr_pct": round(median_atr_pct, 5),
        "section_a": {},
        "section_b": paper_results if paper_results else {},
    }

    for scenario_name, coin_data in results.items():
        total_pnl = sum(d["metrics"]["net_pnl"] for c, d in coin_data.items() if c != "TOTAL")
        total_trades = sum(d["metrics"]["total"] for c, d in coin_data.items() if c != "TOTAL")
        avg_wr = np.mean([d["metrics"]["win_rate"] for c, d in coin_data.items()
                          if c != "TOTAL" and d["metrics"]["total"] > 0])
        summary["section_a"][scenario_name] = {
            "total_pnl": round(total_pnl, 2),
            "total_trades": total_trades,
            "avg_wr": round(avg_wr, 2) if not np.isnan(avg_wr) else 0,
            "per_coin": {
                c: {"pnl": round(d["metrics"]["net_pnl"], 2),
                    "trades": d["metrics"]["total"],
                    "wr": d["metrics"]["win_rate"],
                    "sharpe": d["metrics"]["sharpe"]}
                for c, d in coin_data.items() if c != "TOTAL"
            },
        }

    with open(f"experiments/{exp_id}.json", "w") as f:
        json.dump(summary, f, indent=2, default=str)

    print(f"\n  Experiment saved: experiments/{exp_id}.json")
    return exp_id


# ====================================================================
# Main
# ====================================================================

def main():
    print("=" * 72)
    print("PER-COIN MOMENTUM GATE + ATR SIZING BACKTEST")
    print("=" * 72)

    # ================================================================
    # Phase 1: Load shared data
    # ================================================================
    print("\n[Phase 1] Loading BTC data...")
    btc_ohlcv = fetch_binance_15m("BTCUSDT", years=3)
    btc_db = load_btc_db_data()
    btc_df = build_btc_features(btc_ohlcv, btc_db)
    btc_score = compute_btc_composite_score(btc_df, COMPOSITE_WEIGHTS)

    btc_period_start = pd.Timestamp("2025-06-01")
    mask = btc_df["ts"] >= btc_period_start
    btc_df_trimmed = btc_df[mask].reset_index(drop=True)
    btc_score_trimmed = btc_score[mask].reset_index(drop=True)
    btc_score_ts = pd.Series(btc_score_trimmed.values, index=btc_df_trimmed["ts"].values)
    btc_period_end = btc_df_trimmed["ts"].iloc[-1]
    print(f"  BTC score: {btc_period_start} to {btc_period_end}")

    # Preload altcoin test data (original 6)
    print("\n[Phase 1] Loading altcoin data (6 original)...")
    alt_test_data = {}
    for coin in ALL_COINS:
        symbol = f"{coin}USDT"
        ohlcv = fetch_binance_15m(symbol, years=3)
        alt_df = build_alt_technicals(ohlcv)
        alt_test = alt_df[(alt_df["ts"] >= TEST_START) & (alt_df["ts"] <= btc_period_end)].reset_index(drop=True)
        if len(alt_test) >= 100:
            alt_test_data[coin] = alt_test
            print(f"  {coin}: {len(alt_test):,} test bars")
        else:
            print(f"  {coin}: insufficient data ({len(alt_test)} bars), skipped")

    # Compute median ATR% for sizing reference
    median_atr_pct = compute_median_atr_pct(alt_test_data)
    print(f"\n  Median ATR%: {median_atr_pct:.5f} ({median_atr_pct*100:.3f}%)")

    # ================================================================
    # Phase 2: Run 6 scenarios (Section A)
    # ================================================================
    print(f"\n{'='*72}")
    print("[Phase 2] SECTION A: Multi-month OOS (Jan-Mar 2026, 6 coins)")
    print(f"{'='*72}")
    print(f"  Config: SL=10 ATR, TP=5 ATR, 2x leverage, per-coin thresholds")

    # Save original leverage
    old_lev = bt.LEVERAGE
    bt.LEVERAGE = 2.0

    scenarios = {
        "baseline": {"signal_fn": generate_signal_baseline, "atr_sizing": False,
                      "desc": "v3 no filter, fixed sizing"},
        "pa_ema": {"signal_fn": generate_signal_pa_ema, "atr_sizing": False,
                    "desc": "PA filter: close>EMA9>EMA21 + vol>0.8"},
        "momentum_5bar": {"signal_fn": generate_signal_momentum_5bar, "atr_sizing": False,
                           "desc": "5-bar return confirms direction"},
        "momentum_rsi": {"signal_fn": generate_signal_momentum_rsi, "atr_sizing": False,
                          "desc": "RSI(14) > 50 for LONG, < 50 for SHORT"},
        "atr_sizing": {"signal_fn": generate_signal_baseline, "atr_sizing": True,
                        "desc": "Fixed signal, ATR-proportional sizing"},
        "mom5_atr": {"signal_fn": generate_signal_momentum_5bar, "atr_sizing": True,
                      "desc": "5-bar momentum gate + ATR sizing"},
    }

    all_results = {}

    for scenario_name, scenario_def in scenarios.items():
        signal_fn = scenario_def["signal_fn"]
        use_atr = scenario_def["atr_sizing"]
        print(f"\n--- {scenario_name}: {scenario_def['desc']} ---")

        coin_results = {}
        total_pnl = 0
        total_trades = 0

        for coin in ALL_COINS:
            if coin not in alt_test_data:
                continue
            cfg = COIN_CONFIGS_6[coin]
            trades, m = run_coin(coin, btc_score_ts, alt_test_data[coin], cfg,
                                  signal_fn, use_atr_sizing=use_atr,
                                  median_atr_pct=median_atr_pct)
            coin_results[coin] = {"trades": trades, "metrics": m}
            total_pnl += m["net_pnl"]
            total_trades += m["total"]
            print(f"  {coin:5s}: {m['total']:3d} trades, WR={m['win_rate']:5.1f}%, "
                  f"PnL=${m['net_pnl']:>+9,.2f}, Sharpe={m['sharpe']:.2f}")

        coin_results["TOTAL"] = total_pnl
        all_results[scenario_name] = coin_results
        print(f"  {'TOTAL':5s}: {total_trades:3d} trades, PnL=${total_pnl:>+9,.2f}")

    bt.LEVERAGE = old_lev

    # ================================================================
    # Phase 3: Summary comparison table (Section A)
    # ================================================================
    print(f"\n{'='*72}")
    print("SECTION A: COMPARISON TABLE")
    print(f"{'='*72}")

    scenario_names = list(all_results.keys())
    # Header
    header = f"{'Coin':<6s}"
    for name in scenario_names:
        header += f" | {name:>14s}"
    print(header)
    print("-" * (6 + 17 * len(scenario_names)))

    # Per-coin rows
    for coin in ALL_COINS:
        row = f"{coin:<6s}"
        for name in scenario_names:
            data = all_results[name].get(coin)
            if data and isinstance(data, dict) and "metrics" in data:
                row += f" | ${data['metrics']['net_pnl']:>+10,.2f}"
            else:
                row += f" |       {'N/A':>7s}"
        print(row)

    # Totals
    row = f"{'TOTAL':<6s}"
    for name in scenario_names:
        total = all_results[name].get("TOTAL", 0)
        row += f" | ${total:>+10,.2f}"
    print(row)

    # Trades
    print()
    row = f"{'Trd#':<6s}"
    for name in scenario_names:
        n = sum(d["metrics"]["total"] for c, d in all_results[name].items()
                if c != "TOTAL" and isinstance(d, dict))
        row += f" | {n:>14d}"
    print(row)

    # WR
    row = f"{'WR%':<6s}"
    for name in scenario_names:
        wrs = [d["metrics"]["win_rate"] for c, d in all_results[name].items()
               if c != "TOTAL" and isinstance(d, dict) and d["metrics"]["total"] > 0]
        avg_wr = np.mean(wrs) if wrs else 0
        row += f" | {avg_wr:>13.1f}%"
    print(row)

    # Deltas vs baseline
    baseline_pnl = all_results["baseline"]["TOTAL"]
    print(f"\n--- Deltas vs baseline (${baseline_pnl:+,.2f}) ---")
    for name in scenario_names:
        if name == "baseline":
            continue
        delta = all_results[name]["TOTAL"] - baseline_pnl
        pct = delta / abs(baseline_pnl) * 100 if baseline_pnl != 0 else 0
        print(f"  {name:<16s}: ${delta:>+10,.2f} ({pct:>+6.1f}%)")

    # Per-coin detail for momentum_5bar
    print(f"\n--- Per-coin: baseline vs momentum_5bar ---")
    print(f"{'Coin':<6s} | {'BL trades':>9s} {'BL WR':>6s} {'BL PnL':>10s} | "
          f"{'M5 trades':>9s} {'M5 WR':>6s} {'M5 PnL':>10s} | {'Delta':>8s}")
    print("-" * 80)
    for coin in ALL_COINS:
        bl = all_results["baseline"].get(coin, {})
        m5 = all_results["momentum_5bar"].get(coin, {})
        if isinstance(bl, dict) and "metrics" in bl and isinstance(m5, dict) and "metrics" in m5:
            bm = bl["metrics"]
            mm = m5["metrics"]
            delta = mm["net_pnl"] - bm["net_pnl"]
            print(f"{coin:<6s} | {bm['total']:>9d} {bm['win_rate']:>5.1f}% ${bm['net_pnl']:>+9,.2f} | "
                  f"{mm['total']:>9d} {mm['win_rate']:>5.1f}% ${mm['net_pnl']:>+9,.2f} | ${delta:>+7,.2f}")

    # ================================================================
    # Phase 4: Paper Replay (Section B)
    # ================================================================
    print(f"\n{'='*72}")
    print("[Phase 3] SECTION B: Paper Trading Replay (03-17 16:46 to 03-18 ~09:46 UTC)")
    print(f"{'='*72}")

    paper_trades = load_paper_trades()
    if paper_trades.empty:
        print("  No paper trades found. Skipping Section B.")
        paper_replay_data = None
    else:
        print(f"  Loaded {len(paper_trades)} paper trades")

        # We want the entry bar (~16:46 UTC on 03-17)
        # Fetch enough OHLCV data for indicators (need ~30 bars before entry)
        fetch_start = pd.Timestamp("2026-03-17 08:00:00")  # ~8h before entry
        fetch_end = pd.Timestamp("2026-03-18 12:00:00")

        replay_rows = []
        coins_fetched = 0

        for coin in PAPER_COINS:
            symbol = f"{coin}USDT"
            # Check if we have paper trade data for this coin
            coin_trades = paper_trades[paper_trades["coin"] == coin]
            if coin_trades.empty:
                continue

            # Use the FIRST entry for this coin
            first_trade = coin_trades.iloc[0]
            direction = first_trade["direction"]  # 1 or -1
            paper_pnl = coin_trades["pnl_net"].sum()

            # Fetch OHLCV from cache or API
            try:
                ohlcv = fetch_binance_15m(symbol, years=3)
                ohlcv["date_time"] = pd.to_datetime(ohlcv["date_time"])
                mask = (ohlcv["date_time"] >= fetch_start) & (ohlcv["date_time"] <= fetch_end)
                ohlcv_window = ohlcv[mask].reset_index(drop=True)
            except Exception as e:
                print(f"  {coin}: failed to fetch ({e})")
                continue

            if len(ohlcv_window) < 30:
                print(f"  {coin}: insufficient bars ({len(ohlcv_window)})")
                continue

            # Find the bar closest to entry time
            entry_time = pd.Timestamp(first_trade["entry_time"])
            # Compute indicators at the bar BEFORE entry (anti-lookahead)
            time_diffs = (ohlcv_window["date_time"] - entry_time).abs()
            entry_bar_idx = time_diffs.idxmin()

            # Use bar before entry for indicator computation
            if entry_bar_idx >= 1:
                indicator_idx = entry_bar_idx - 1
            else:
                indicator_idx = 0

            indicators = compute_coin_indicators(ohlcv_window, bar_idx=indicator_idx)
            if indicators is None:
                print(f"  {coin}: not enough history for indicators")
                continue

            coins_fetched += 1

            # Determine if each gate would PASS
            dir_label = "LONG" if direction == 1 else "SHORT"

            if direction == 1:
                pass_ema = indicators["ema_aligned_long"] and indicators["vol_ok"]
                pass_mom5 = indicators["mom5_long"]
                pass_rsi = indicators["rsi_long"]
            else:
                pass_ema = indicators["ema_aligned_short"] and indicators["vol_ok"]
                pass_mom5 = indicators["mom5_short"]
                pass_rsi = indicators["rsi_short"]

            replay_rows.append({
                "coin": coin,
                "direction": dir_label,
                "entry_time": str(entry_time)[:19],
                "paper_pnl": round(paper_pnl, 2),
                "n_trades": len(coin_trades),
                "mom5": round(indicators["mom5"] * 100, 2),
                "rsi": round(indicators["rsi"], 1),
                "ema_aligned": "LONG" if indicators["ema_aligned_long"] else ("SHORT" if indicators["ema_aligned_short"] else "FLAT"),
                "vol_ratio": round(indicators["vol_ratio"], 2),
                "atr_pct": round(indicators["atr_pct"] * 100, 3),
                "pass_ema": pass_ema,
                "pass_mom5": pass_mom5,
                "pass_rsi": pass_rsi,
            })

        # Build replay table
        if replay_rows:
            replay_df = pd.DataFrame(replay_rows).sort_values("paper_pnl")

            print(f"\n  Paper Replay: {len(replay_df)} coins analyzed")
            print(f"  Entry around: 03-17 ~16:46 UTC (BTC score = +3.0)")

            # Full table
            print(f"\n  {'Coin':<12s} {'Dir':>5s} {'PnL':>8s} {'Mom5%':>7s} {'RSI':>6s} "
                  f"{'EMA':>6s} {'VR':>5s} {'ATR%':>6s} | "
                  f"{'EMA':>4s} {'M5':>4s} {'RSI':>4s}")
            print("  " + "-" * 90)

            for _, r in replay_df.iterrows():
                ema_pass = "OK" if r["pass_ema"] else "CUT"
                m5_pass = "OK" if r["pass_mom5"] else "CUT"
                rsi_pass = "OK" if r["pass_rsi"] else "CUT"
                print(f"  {r['coin']:<12s} {r['direction']:>5s} ${r['paper_pnl']:>+7.2f} "
                      f"{r['mom5']:>+6.2f}% {r['rsi']:>5.1f} "
                      f"{r['ema_aligned']:>6s} {r['vol_ratio']:>5.2f} {r['atr_pct']:>5.3f}% | "
                      f"{ema_pass:>4s} {m5_pass:>4s} {rsi_pass:>4s}")

            # Summary: hypothetical PnL with each filter
            total_paper_pnl = replay_df["paper_pnl"].sum()
            pnl_with_ema = replay_df[replay_df["pass_ema"]]["paper_pnl"].sum()
            pnl_with_mom5 = replay_df[replay_df["pass_mom5"]]["paper_pnl"].sum()
            pnl_with_rsi = replay_df[replay_df["pass_rsi"]]["paper_pnl"].sum()
            pnl_with_mom5_rsi = replay_df[replay_df["pass_mom5"] & replay_df["pass_rsi"]]["paper_pnl"].sum()

            n_pass_ema = replay_df["pass_ema"].sum()
            n_pass_mom5 = replay_df["pass_mom5"].sum()
            n_pass_rsi = replay_df["pass_rsi"].sum()
            n_pass_both = (replay_df["pass_mom5"] & replay_df["pass_rsi"]).sum()

            # ATR sizing simulation
            if median_atr_pct > 0:
                atr_sized_pnl = 0
                for _, r in replay_df.iterrows():
                    atr_pct = r["atr_pct"] / 100  # convert back from %
                    if atr_pct > 0:
                        size_mult = np.clip(median_atr_pct / atr_pct, 0.5, 2.0)
                    else:
                        size_mult = 1.0
                    atr_sized_pnl += r["paper_pnl"] * size_mult
            else:
                atr_sized_pnl = total_paper_pnl

            # Mom5 + ATR sizing
            mom5_atr_pnl = 0
            for _, r in replay_df.iterrows():
                if r["pass_mom5"]:
                    atr_pct = r["atr_pct"] / 100
                    if atr_pct > 0:
                        size_mult = np.clip(median_atr_pct / atr_pct, 0.5, 2.0)
                    else:
                        size_mult = 1.0
                    mom5_atr_pnl += r["paper_pnl"] * size_mult

            print(f"\n  {'='*60}")
            print(f"  HYPOTHETICAL PnL COMPARISON (Paper Replay)")
            print(f"  {'='*60}")
            print(f"  {'Filter':<20s} {'Coins':>6s} {'PnL':>10s} {'Delta':>10s}")
            print(f"  {'-'*50}")
            print(f"  {'No filter':<20s} {len(replay_df):>6d} ${total_paper_pnl:>+9.2f} {'---':>10s}")
            print(f"  {'PA EMA':<20s} {int(n_pass_ema):>6d} ${pnl_with_ema:>+9.2f} ${pnl_with_ema-total_paper_pnl:>+9.2f}")
            print(f"  {'Momentum 5-bar':<20s} {int(n_pass_mom5):>6d} ${pnl_with_mom5:>+9.2f} ${pnl_with_mom5-total_paper_pnl:>+9.2f}")
            print(f"  {'RSI > 50':<20s} {int(n_pass_rsi):>6d} ${pnl_with_rsi:>+9.2f} ${pnl_with_rsi-total_paper_pnl:>+9.2f}")
            print(f"  {'Mom5 + RSI':<20s} {int(n_pass_both):>6d} ${pnl_with_mom5_rsi:>+9.2f} ${pnl_with_mom5_rsi-total_paper_pnl:>+9.2f}")
            print(f"  {'ATR sizing':<20s} {len(replay_df):>6d} ${atr_sized_pnl:>+9.2f} ${atr_sized_pnl-total_paper_pnl:>+9.2f}")
            print(f"  {'Mom5 + ATR sizing':<20s} {int(n_pass_mom5):>6d} ${mom5_atr_pnl:>+9.2f} ${mom5_atr_pnl-total_paper_pnl:>+9.2f}")

            # Which coins would have been saved?
            losers = replay_df[replay_df["paper_pnl"] < -1.0]
            if len(losers) > 0:
                print(f"\n  LOSERS (PnL < -$1) that would have been CUT by Mom5:")
                for _, r in losers.iterrows():
                    if not r["pass_mom5"]:
                        print(f"    {r['coin']:<12s} PnL=${r['paper_pnl']:>+7.2f} "
                              f"mom5={r['mom5']:>+5.2f}% -> SAVED ${abs(r['paper_pnl']):.2f}")

            paper_replay_data = {
                "n_coins": len(replay_df),
                "total_pnl": round(total_paper_pnl, 2),
                "pnl_ema": round(pnl_with_ema, 2),
                "pnl_mom5": round(pnl_with_mom5, 2),
                "pnl_rsi": round(pnl_with_rsi, 2),
                "pnl_atr_sizing": round(atr_sized_pnl, 2),
                "pnl_mom5_atr": round(mom5_atr_pnl, 2),
                "coins_cut_by_mom5": int(len(replay_df) - n_pass_mom5),
                "per_coin": replay_rows,
            }
        else:
            print("  No paper replay data generated.")
            paper_replay_data = None

    # ================================================================
    # Phase 5: Save experiment + Verdict
    # ================================================================
    exp_id = save_experiment(all_results, paper_replay_data, median_atr_pct)

    print(f"\n{'='*72}")
    print("VERDICT")
    print(f"{'='*72}")
    baseline_pnl = all_results["baseline"]["TOTAL"]
    best_name = max(all_results.keys(), key=lambda k: all_results[k]["TOTAL"])
    best_pnl = all_results[best_name]["TOTAL"]
    delta = best_pnl - baseline_pnl

    print(f"  Baseline (no filter):  ${baseline_pnl:>+10,.2f}")
    print(f"  Best scenario:         {best_name} -> ${best_pnl:>+10,.2f} (delta: ${delta:>+,.2f})")

    for name in ["momentum_5bar", "momentum_rsi", "pa_ema"]:
        n_trades_bl = sum(d["metrics"]["total"] for c, d in all_results["baseline"].items()
                          if c != "TOTAL" and isinstance(d, dict))
        n_trades = sum(d["metrics"]["total"] for c, d in all_results[name].items()
                       if c != "TOTAL" and isinstance(d, dict))
        pct_cut = (1 - n_trades / n_trades_bl) * 100 if n_trades_bl > 0 else 0
        print(f"  {name:<16s}: {pct_cut:>5.1f}% trades filtered out")

    print(f"\n  Experiment data: experiments/{exp_id}.json")
    print("Done!")


if __name__ == "__main__":
    main()
