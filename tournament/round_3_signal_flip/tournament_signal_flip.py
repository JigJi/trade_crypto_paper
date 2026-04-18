"""
Tournament Round 3: SIGNAL_FLIP MITIGATION
============================================
Paper trading lost $385 in one day (Mar 23) because BTC score flipped 7-8 times,
causing 188 SIGNAL_FLIP trades (WR 21.3%, -$440) while TP exits were great (99.2% WR, +$906).

Critical finding: run_portfolio_simulation() has NO SIGNAL_FLIP exit at all,
yet portfolio backtest still profits $30,941 → proves disable SIGNAL_FLIP = viable.

Goal: Find optimal SIGNAL_FLIP policy that survives all market regimes.

Experiments:
  Phase 1: Per-coin (21 experiments) — flip_mode, min_bars, hysteresis, confirm, cooldown
  Phase 2: Portfolio (4 experiments) — best settings + max_concurrent sweep
  Phase 3: Combined champion (3-5 experiments) — merge winners + regime validation
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
    fetch_binance_15m, load_btc_db_data, build_alt_technicals,
    generate_btc_led_signal, calc_metrics,
    INIT_EQUITY, BUDGET_USDT, LEVERAGE, FEE, SLIP, MAX_CONCURRENT,
)
from signal_core import build_btc_features, compute_btc_composite_score_v6

# ── Config ──────────────────────────────────────────────
OOS_START = "2025-01-01"
OOS_END   = "2026-03-24"
COINS = ["BTC", "XRP", "ADA", "DOT", "SUI", "FIL"]

# v6 champion config (baseline for this tournament)
V6_CONFIGS = {
    "BTC":  {"threshold": 2.5, "sl": 25.0, "tp": 20.0, "trail": 99, "trail_act": 99, "cd": 4},
    "XRP":  {"threshold": 3.5, "sl": 25.0, "tp": 20.0, "trail": 99, "trail_act": 99, "cd": 4},
    "ADA":  {"threshold": 3.5, "sl": 25.0, "tp": 20.0, "trail": 99, "trail_act": 99, "cd": 4},
    "DOT":  {"threshold": 3.0, "sl": 25.0, "tp": 20.0, "trail": 99, "trail_act": 99, "cd": 4},
    "SUI":  {"threshold": 3.0, "sl": 25.0, "tp": 20.0, "trail": 99, "trail_act": 99, "cd": 4},
    "FIL":  {"threshold": 3.0, "sl": 25.0, "tp": 20.0, "trail": 99, "trail_act": 99, "cd": 4},
}

# Regime periods for validation
REGIMES = {
    "BULL":  ("2024-10-01", "2025-01-20"),
    "BEAR":  ("2025-01-20", "2025-04-01"),
    "FLAT":  ("2025-04-01", "2025-07-01"),
    "MIXED": ("2025-07-01", "2025-10-01"),
    "RECENT": ("2025-10-01", "2026-03-24"),
}

RESULTS_DIR = Path(__file__).parent
RESULTS_FILE = RESULTS_DIR / "results.json"


# ══════════════════════════════════════════════════════════
# DATA LOADING (once)
# ══════════════════════════════════════════════════════════

print("=" * 70)
print("TOURNAMENT ROUND 3: SIGNAL_FLIP MITIGATION")
print("=" * 70)

t0 = time.time()
btc_ohlcv = fetch_binance_15m("BTCUSDT", years=3)
db_data = load_btc_db_data()
btc_df = build_btc_features(btc_ohlcv, db_data)
print(f"BTC features: {len(btc_df)} bars ({time.time()-t0:.1f}s)")

# Compute v6 BTC score
v6_score = compute_btc_composite_score_v6(btc_df)
v6_score_ts = pd.Series(v6_score.values, index=btc_df["ts"].values)
print(f"V6 BTC score computed: range [{v6_score.min():.1f}, {v6_score.max():.1f}]")

alt_data = {}
for coin in COINS:
    sym = f"{coin}USDT"
    ohlcv = fetch_binance_15m(sym, years=3)
    alt_data[coin] = build_alt_technicals(ohlcv)
    print(f"  {coin}: {len(alt_data[coin])} bars")

print(f"Data loaded in {time.time()-t0:.1f}s\n")


# ══════════════════════════════════════════════════════════
# FUNCTION 1: gen_signal_with_options()
# ══════════════════════════════════════════════════════════

def gen_signal_with_options(btc_score_ts, alt_df, entry_threshold,
                            hysteresis_band=0.0, use_alt_pa=False,
                            confirm_bars=1):
    """Generate signal with hysteresis band + confirmation bars.

    State machine:
      state=0:  ENTER LONG  if score >= +entry_threshold for confirm_bars consecutive bars
                ENTER SHORT if score <= -entry_threshold for confirm_bars consecutive bars
      state=1:  STAY LONG   if score >= +exit_threshold
                FLIP SHORT  if score <= -entry_threshold (for confirm_bars bars)
                EXIT to 0   otherwise
      state=-1: STAY SHORT  if score <= -exit_threshold
                FLIP LONG   if score >= +entry_threshold (for confirm_bars bars)
                EXIT to 0   otherwise

    hysteresis_band=0.0: exit_threshold = entry_threshold (no hysteresis)
    confirm_bars=1: instant (default). >1 = require N consecutive bars above threshold
    """
    exit_threshold = entry_threshold - hysteresis_band

    btc_score_df = btc_score_ts.reset_index()
    btc_score_df.columns = ["ts", "btc_score"]
    alt = alt_df.copy().sort_values("ts")
    alt = pd.merge_asof(alt, btc_score_df.sort_values("ts"),
                        on="ts", direction="backward", tolerance=pd.Timedelta("30min"))
    alt["btc_score"] = alt["btc_score"].fillna(0)

    scores = alt["btc_score"].values
    n = len(scores)
    sig = np.zeros(n, dtype=int)
    state = 0
    consec_bull = 0  # consecutive bars with score >= entry_threshold
    consec_bear = 0  # consecutive bars with score <= -entry_threshold

    for i in range(n):
        s = scores[i]

        # Track consecutive confirmation bars
        if s >= entry_threshold:
            consec_bull += 1
        else:
            consec_bull = 0

        if s <= -entry_threshold:
            consec_bear += 1
        else:
            consec_bear = 0

        if state == 0:
            if consec_bull >= confirm_bars:
                state = 1
            elif consec_bear >= confirm_bars:
                state = -1
        elif state == 1:
            if consec_bear >= confirm_bars:
                state = -1          # direct flip to SHORT
            elif s < exit_threshold:
                state = 0           # exit to neutral
        else:  # state == -1
            if consec_bull >= confirm_bars:
                state = 1           # direct flip to LONG
            elif s > -exit_threshold:
                state = 0           # exit to neutral

        sig[i] = state

    signal = pd.Series(sig, index=alt.index)

    if use_alt_pa:
        alt_bull_pa = (alt["close"] > alt["ema9"]) & (alt["ema9"] > alt["ema21"])
        alt_bear_pa = (alt["close"] < alt["ema9"]) & (alt["ema9"] < alt["ema21"])
        alt_vol_ok = alt["vol_ratio"] > 0.8
        signal[(signal == 1) & ~(alt_bull_pa & alt_vol_ok)] = 0
        signal[(signal == -1) & ~(alt_bear_pa & alt_vol_ok)] = 0

    return signal, alt


# ══════════════════════════════════════════════════════════
# FUNCTION 2: run_backtest_flip()
# ══════════════════════════════════════════════════════════

def run_backtest_flip(df, signals, sl_atr_mult=25.0, tp_atr_mult=20.0,
                      max_hold_bars=96, cooldown_bars=4,
                      flip_mode="reverse",
                      min_bars_before_flip=0,
                      flip_cooldown_extra=0):
    """Modified backtest engine with configurable SIGNAL_FLIP behavior.

    flip_mode:
      "reverse"   - exit + immediately re-enter opposite direction (original behavior)
      "exit_only" - exit position but do NOT re-enter; apply flip_cooldown_extra
      "disabled"  - skip SIGNAL_FLIP check entirely (hold until SL/TP/TIMEOUT)

    min_bars_before_flip: minimum bars held before SIGNAL_FLIP is allowed
    flip_cooldown_extra: extra cooldown bars after a flip exit (exit_only mode)
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
    last_exit_i = -cooldown_bars - 1
    max_notional = BUDGET_USDT * LEVERAGE * 2

    for i in range(n):
        if position == 0 and sig[i] != 0 and (i - last_exit_i) > cooldown_bars:
            raw_px = opens[i]
            cur_atr = atrs[i] if not np.isnan(atrs[i]) else 0
            if raw_px <= 0 or cur_atr <= 0 or cur_atr / raw_px < 0.0005:
                continue
            qty = (BUDGET_USDT * LEVERAGE) / raw_px
            entry_px = raw_px * (1 + SLIP) if sig[i] == 1 else raw_px * (1 - SLIP)
            entry_atr = cur_atr
            fee_in = entry_px * qty * FEE
            position = sig[i]
            entry_i = i
            peak = entry_px
            trough = entry_px
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

            # Trail disabled for v6 (99/99)
            exit_px = exit_reason = None
            if position == 1:
                if l <= sl_level: exit_px, exit_reason = sl_level, "SL"
                elif h >= tp_level: exit_px, exit_reason = tp_level, "TP"
            else:
                if h >= sl_level: exit_px, exit_reason = sl_level, "SL"
                elif l <= tp_level: exit_px, exit_reason = tp_level, "TP"

            if exit_px is None and (i - entry_i) >= max_hold_bars:
                exit_px, exit_reason = c, "TIMEOUT"

            # SIGNAL_FLIP check (configurable)
            if exit_px is None and flip_mode != "disabled":
                if sig[i] != 0 and sig[i] != position:
                    bars_held = i - entry_i
                    if bars_held >= min_bars_before_flip:
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

                # Handle cooldown based on flip mode
                if exit_reason == "SIGNAL_FLIP" and flip_mode == "exit_only":
                    last_exit_i = i + flip_cooldown_extra
                else:
                    last_exit_i = i

                position = 0

    return pd.DataFrame(records)


# ══════════════════════════════════════════════════════════
# FUNCTION 3: run_portfolio_flip()
# ══════════════════════════════════════════════════════════

def run_portfolio_flip(all_results, max_concurrent=MAX_CONCURRENT,
                       flip_mode="disabled",
                       min_bars_before_flip=0,
                       flip_cooldown_extra=0):
    """Portfolio sim with optional SIGNAL_FLIP exit handling.

    Clone of run_portfolio_simulation() with added SIGNAL_FLIP check.
    """
    coins_data = [r for r in all_results if r is not None and "alt_df_full" in r]
    if not coins_data:
        return None

    # Build unified timeline
    all_bars = []
    for cd in coins_data:
        adf = cd["alt_df_full"]
        sig = cd["signals_full"].shift(1).fillna(0).astype(int)
        cfg = cd["config"]
        for idx in range(len(adf)):
            all_bars.append({
                "ts": adf["ts"].iloc[idx],
                "coin": cd["coin"],
                "coin_idx": idx,
                "signal": int(sig.iloc[idx]),
                "btc_score": abs(adf["btc_score"].iloc[idx]) if "btc_score" in adf.columns else 0,
                "open": adf["open"].iloc[idx],
                "high": adf["high"].iloc[idx],
                "low": adf["low"].iloc[idx],
                "close": adf["close"].iloc[idx],
                "atr": adf["atr"].iloc[idx],
                "half_kelly": cd["half_kelly"],
                "sl": cfg["sl"],
                "tp": cfg["tp"],
                "cd": cfg["cd"],
            })

    bars_df = pd.DataFrame(all_bars).sort_values(["ts", "coin"]).reset_index(drop=True)

    equity = INIT_EQUITY
    positions = {}
    last_exit_ts = {}
    flip_cooldown_ts = {}  # coin -> extra cooldown until this ts
    records = []
    max_notional = BUDGET_USDT * LEVERAGE * 2

    for ts, group in bars_df.groupby("ts"):
        # Process exits
        coins_to_exit = []
        for coin, pos in list(positions.items()):
            coin_bars = group[group["coin"] == coin]
            if coin_bars.empty:
                continue
            bar = coin_bars.iloc[0]
            h, l, c = bar["high"], bar["low"], bar["close"]
            atr = pos["entry_atr"]
            direction = pos["direction"]

            if direction == 1:
                pos["peak"] = max(pos["peak"], h)
                sl_level = pos["entry_px"] - pos["sl"] * atr
                tp_level = pos["entry_px"] + pos["tp"] * atr
            else:
                pos["trough"] = min(pos["trough"], l)
                sl_level = pos["entry_px"] + pos["sl"] * atr
                tp_level = pos["entry_px"] - pos["tp"] * atr

            exit_px = exit_reason = None
            if direction == 1:
                if l <= sl_level: exit_px, exit_reason = sl_level, "SL"
                elif h >= tp_level: exit_px, exit_reason = tp_level, "TP"
            else:
                if h >= sl_level: exit_px, exit_reason = sl_level, "SL"
                elif l <= tp_level: exit_px, exit_reason = tp_level, "TP"

            bars_held = (pd.Timestamp(ts) - pd.Timestamp(pos["entry_ts"])) / pd.Timedelta("15min")
            if exit_px is None and bars_held >= 96:
                exit_px, exit_reason = c, "TIMEOUT"

            # SIGNAL_FLIP check
            if exit_px is None and flip_mode != "disabled":
                sig_now = int(bar["signal"])
                if sig_now != 0 and sig_now != direction:
                    if bars_held >= min_bars_before_flip:
                        exit_px = bar["open"]
                        exit_reason = "SIGNAL_FLIP"

            if exit_px is not None:
                exit_px_f = exit_px * (1 - SLIP) if direction == 1 else exit_px * (1 + SLIP)
                fee_out = exit_px_f * pos["qty"] * FEE
                pnl_gross = (exit_px_f - pos["entry_px"]) * pos["qty"] * direction
                pnl_net = pnl_gross - pos["fee_in"] - fee_out
                equity += pnl_net
                records.append({
                    "coin": coin, "entry_time": pos["entry_ts"], "exit_time": ts,
                    "dir": "L" if direction == 1 else "S",
                    "entry_price": pos["entry_px"], "exit_price": exit_px_f,
                    "qty": pos["qty"], "pnl_net": pnl_net,
                    "equity_after": equity, "exit_reason": exit_reason,
                    "holding_bars": int(bars_held),
                })
                coins_to_exit.append(coin)
                last_exit_ts[coin] = ts
                if exit_reason == "SIGNAL_FLIP" and flip_mode == "exit_only":
                    extra_td = pd.Timedelta(f"{flip_cooldown_extra * 15}min")
                    flip_cooldown_ts[coin] = pd.Timestamp(ts) + extra_td

        for coin in coins_to_exit:
            del positions[coin]

        # Process entries
        candidates = []
        for _, bar in group.iterrows():
            coin = bar["coin"]
            if coin in positions:
                continue
            if bar["signal"] == 0:
                continue
            # Check cooldown
            if coin in last_exit_ts:
                bars_since = (pd.Timestamp(ts) - pd.Timestamp(last_exit_ts[coin])) / pd.Timedelta("15min")
                if bars_since <= bar["cd"]:
                    continue
            # Check flip cooldown
            if coin in flip_cooldown_ts:
                if pd.Timestamp(ts) <= flip_cooldown_ts[coin]:
                    continue
            raw_px = bar["open"]
            cur_atr = bar["atr"]
            if raw_px <= 0 or np.isnan(cur_atr) or cur_atr <= 0 or cur_atr / raw_px < 0.0005:
                continue
            candidates.append(bar)

        candidates.sort(key=lambda x: x["btc_score"], reverse=True)

        slots_available = max_concurrent - len(positions)
        for bar in candidates[:slots_available]:
            coin = bar["coin"]
            raw_px = bar["open"]
            cur_atr = bar["atr"]
            sig_dir = bar["signal"]
            hk = bar["half_kelly"]

            risk_amount = hk * equity
            sl_distance = bar["sl"] * cur_atr
            qty = risk_amount / sl_distance
            notional = qty * raw_px
            if notional > max_notional:
                qty = max_notional / raw_px

            entry_px = raw_px * (1 + SLIP) if sig_dir == 1 else raw_px * (1 - SLIP)
            fee_in = entry_px * qty * FEE

            positions[coin] = {
                "direction": sig_dir,
                "entry_px": entry_px,
                "entry_atr": cur_atr,
                "entry_ts": ts,
                "qty": qty,
                "fee_in": fee_in,
                "peak": entry_px,
                "trough": entry_px,
                "sl": bar["sl"],
                "tp": bar["tp"],
            }

    trades_df = pd.DataFrame(records) if records else pd.DataFrame()
    total_bars = len(bars_df["ts"].unique())
    m_full = calc_metrics(trades_df, total_bars)

    return {
        "trades": trades_df,
        "metrics_full": m_full,
        "max_concurrent": max_concurrent,
        "n_coins": len(coins_data),
    }


# ══════════════════════════════════════════════════════════
# FUNCTION 4: run_contender() — Experiment orchestrator
# ══════════════════════════════════════════════════════════

def compute_exit_breakdown(trades_df):
    """Compute per-exit-reason metrics."""
    breakdown = {}
    if trades_df.empty:
        return breakdown
    for reason in ["SL", "TP", "TIMEOUT", "SIGNAL_FLIP"]:
        sub = trades_df[trades_df["exit_reason"] == reason]
        if len(sub) > 0:
            wins = (sub["pnl_net"] > 0).sum()
            breakdown[reason] = {
                "count": len(sub),
                "pnl": round(sub["pnl_net"].sum(), 2),
                "wr": round(100 * wins / len(sub), 1),
                "avg_pnl": round(sub["pnl_net"].mean(), 2),
                "avg_bars": round(sub["holding_bars"].mean(), 1),
            }
        else:
            breakdown[reason] = {"count": 0, "pnl": 0, "wr": 0, "avg_pnl": 0, "avg_bars": 0}
    return breakdown


def run_contender(name, flip_mode="reverse", min_bars_before_flip=0,
                  flip_cooldown_extra=0, hysteresis_band=0.0,
                  confirm_bars=1, description=""):
    """Run one experiment across all 6 coins with given parameters."""
    print(f"\n{'-'*60}")
    print(f"  {name}")
    if description:
        print(f"  {description}")
    print(f"  flip={flip_mode} min_bars={min_bars_before_flip} hyst={hysteresis_band} "
          f"confirm={confirm_bars} cd_extra={flip_cooldown_extra}")
    print(f"{'-'*60}")

    t1 = time.time()

    all_trades = []
    coin_results = {}

    for coin in COINS:
        cfg = V6_CONFIGS[coin]

        # Generate signals with options
        sig, alt_merged = gen_signal_with_options(
            v6_score_ts, alt_data[coin],
            entry_threshold=cfg["threshold"],
            hysteresis_band=hysteresis_band,
            use_alt_pa=False,
            confirm_bars=confirm_bars,
        )

        # Filter to OOS period
        oos_mask = (alt_merged["ts"] >= pd.Timestamp(OOS_START))
        if OOS_END:
            oos_mask &= (alt_merged["ts"] <= pd.Timestamp(OOS_END))

        alt_oos = alt_merged[oos_mask].reset_index(drop=True)
        sig_oos = sig[oos_mask].reset_index(drop=True)

        if len(alt_oos) < 100:
            continue

        trades = run_backtest_flip(
            alt_oos, sig_oos,
            sl_atr_mult=cfg["sl"],
            tp_atr_mult=cfg["tp"],
            max_hold_bars=96,
            cooldown_bars=cfg["cd"],
            flip_mode=flip_mode,
            min_bars_before_flip=min_bars_before_flip,
            flip_cooldown_extra=flip_cooldown_extra,
        )

        if len(trades) > 0:
            m = calc_metrics(trades, len(alt_oos))
            bd = compute_exit_breakdown(trades)
            coin_results[coin] = {**m, "exit_breakdown": bd}
            trades["coin"] = coin
            all_trades.append(trades)

    # Aggregate
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

        equity = INIT_EQUITY + combined["pnl_net"].cumsum()
        max_dd = ((equity - equity.cummax()) / equity.cummax() * 100).min()
        ret_per_trade = combined["pnl_net"] / BUDGET_USDT
        sharpe = (ret_per_trade.mean() / ret_per_trade.std() * np.sqrt(total_trades)
                  if ret_per_trade.std() > 0 else 0)

        agg_breakdown = compute_exit_breakdown(combined)

        # Regime analysis
        regime_results = {}
        for rname, (rstart, rend) in REGIMES.items():
            rmask = ((pd.to_datetime(combined["entry_time"]) >= pd.Timestamp(rstart)) &
                     (pd.to_datetime(combined["entry_time"]) < pd.Timestamp(rend)))
            rsub = combined[rmask]
            if len(rsub) > 0:
                regime_results[rname] = {
                    "trades": len(rsub),
                    "pnl": round(rsub["pnl_net"].sum(), 2),
                    "wr": round(100 * (rsub["pnl_net"] > 0).sum() / len(rsub), 1),
                }
            else:
                regime_results[rname] = {"trades": 0, "pnl": 0, "wr": 0}
    else:
        total_pnl = total_trades = 0; total_wr = 0
        long_pnl = short_pnl = 0
        max_dd = sharpe = 0
        agg_breakdown = {}
        regime_results = {}

    elapsed = time.time() - t1

    result = {
        "name": name, "description": description,
        "params": {
            "flip_mode": flip_mode,
            "min_bars_before_flip": min_bars_before_flip,
            "flip_cooldown_extra": flip_cooldown_extra,
            "hysteresis_band": hysteresis_band,
            "confirm_bars": confirm_bars,
        },
        "total_pnl": round(total_pnl, 2),
        "total_trades": total_trades,
        "win_rate": round(total_wr, 1),
        "sharpe": round(sharpe, 2),
        "max_dd_pct": round(max_dd, 2),
        "long_pnl": round(long_pnl, 2),
        "short_pnl": round(short_pnl, 2),
        "exit_breakdown": agg_breakdown,
        "regime_results": regime_results,
        "coin_results": {c: {"pnl": round(m["net_pnl"], 2), "wr": round(m["win_rate"], 1),
                              "trades": m["total"], "sharpe": round(m["sharpe"], 2),
                              "exit_breakdown": m.get("exit_breakdown", {})}
                         for c, m in coin_results.items()},
        "elapsed_s": round(elapsed, 1),
    }

    # Pretty print
    flip_info = ""
    if "SIGNAL_FLIP" in agg_breakdown:
        sf = agg_breakdown["SIGNAL_FLIP"]
        flip_info = f" | FLIP: {sf['count']} trades, ${sf['pnl']:+,.0f}, {sf['wr']:.0f}% WR"
    print(f"    {total_trades} trades | WR {total_wr:.1f}% | PnL ${total_pnl:+,.0f} | "
          f"Sharpe {sharpe:.2f} | DD {max_dd:.1f}%{flip_info}")

    return result


# ══════════════════════════════════════════════════════════
# HELPER: Summary printer
# ══════════════════════════════════════════════════════════

def print_batch_summary(batch_name, results, baseline_pnl):
    print(f"\n{'='*110}")
    print(f"{batch_name} SUMMARY")
    print(f"{'='*110}")
    print(f"{'Rank':<4} {'Name':<35} {'Trades':>6} {'WR%':>6} {'PnL':>10} {'Sharpe':>7} "
          f"{'DD%':>6} {'FlipN':>6} {'FlipPnL':>9} {'Delta':>10}")
    print("-" * 110)
    for i, r in enumerate(sorted(results, key=lambda x: x["total_pnl"], reverse=True), 1):
        delta = r["total_pnl"] - baseline_pnl
        sf = r.get("exit_breakdown", {}).get("SIGNAL_FLIP", {})
        flip_n = sf.get("count", 0)
        flip_pnl = sf.get("pnl", 0)
        marker = " ***" if r["total_pnl"] == max(x["total_pnl"] for x in results) else ""
        print(f"{i:<4} {r['name']:<35} {r['total_trades']:>6} {r['win_rate']:>5.1f}% "
              f"${r['total_pnl']:>9,.0f} {r['sharpe']:>7.2f} {r['max_dd_pct']:>5.1f}% "
              f"{flip_n:>6} ${flip_pnl:>+8,.0f} ${delta:>+9,.0f}{marker}")
    return sorted(results, key=lambda x: x["total_pnl"], reverse=True)


# ══════════════════════════════════════════════════════════
# PHASE 1: PER-COIN EXPERIMENTS (21 experiments)
# ══════════════════════════════════════════════════════════

all_results = []

# ---- Group A: Flip Mode (3) ----
print("\n" + "#" * 70)
print("# GROUP A: Flip Mode (reverse vs exit_only vs disabled)")
print("#" * 70)

group_a = []
for mode in ["reverse", "exit_only", "disabled"]:
    r = run_contender(
        f"A_{mode}",
        flip_mode=mode,
        description=f"flip_mode={mode}, all other params default",
    )
    group_a.append(r)

all_results.extend(group_a)
BASELINE_PNL = group_a[0]["total_pnl"]  # reverse = baseline
group_a_sorted = print_batch_summary("GROUP A: FLIP MODE", group_a, BASELINE_PNL)
best_flip_mode = group_a_sorted[0]["params"]["flip_mode"]
print(f"\n  >>> Best flip mode: {best_flip_mode}")


# ---- Group B: Min Bars Before Flip (6) ----
print("\n" + "#" * 70)
print("# GROUP B: Min Bars Before Flip")
print("#" * 70)

group_b = []
for bars in [0, 4, 8, 12, 16, 24]:
    r = run_contender(
        f"B_min_bars_{bars}",
        flip_mode="reverse",
        min_bars_before_flip=bars,
        description=f"min_bars_before_flip={bars}",
    )
    group_b.append(r)

all_results.extend(group_b)
group_b_sorted = print_batch_summary("GROUP B: MIN BARS BEFORE FLIP", group_b, BASELINE_PNL)
best_min_bars = group_b_sorted[0]["params"]["min_bars_before_flip"]
print(f"\n  >>> Best min_bars: {best_min_bars}")


# ---- Group C: Hysteresis Band (5) ----
print("\n" + "#" * 70)
print("# GROUP C: Hysteresis Band")
print("#" * 70)

group_c = []
for band in [0.0, 1.5, 2.0, 2.5, 3.0]:
    r = run_contender(
        f"C_hyst_{band}",
        flip_mode="reverse",
        hysteresis_band=band,
        description=f"hysteresis_band={band}",
    )
    group_c.append(r)

all_results.extend(group_c)
group_c_sorted = print_batch_summary("GROUP C: HYSTERESIS BAND", group_c, BASELINE_PNL)
best_hyst = group_c_sorted[0]["params"]["hysteresis_band"]
print(f"\n  >>> Best hysteresis: {best_hyst}")


# ---- Group D: Signal Confirmation (3) ----
print("\n" + "#" * 70)
print("# GROUP D: Signal Confirmation Bars")
print("#" * 70)

group_d = []
for bars in [1, 2, 3]:
    r = run_contender(
        f"D_confirm_{bars}",
        flip_mode="reverse",
        confirm_bars=bars,
        description=f"confirm_bars={bars}",
    )
    group_d.append(r)

all_results.extend(group_d)
group_d_sorted = print_batch_summary("GROUP D: SIGNAL CONFIRMATION", group_d, BASELINE_PNL)
best_confirm = group_d_sorted[0]["params"]["confirm_bars"]
print(f"\n  >>> Best confirm_bars: {best_confirm}")


# ---- Group E: Flip Cooldown Extra (4) ----
print("\n" + "#" * 70)
print("# GROUP E: Flip Cooldown Extra (exit_only mode)")
print("#" * 70)

group_e = []
for cd in [0, 4, 8, 16]:
    r = run_contender(
        f"E_cd_extra_{cd}",
        flip_mode="exit_only",
        flip_cooldown_extra=cd,
        description=f"exit_only + flip_cooldown_extra={cd}",
    )
    group_e.append(r)

all_results.extend(group_e)
group_e_sorted = print_batch_summary("GROUP E: FLIP COOLDOWN EXTRA", group_e, BASELINE_PNL)
best_cd_extra = group_e_sorted[0]["params"]["flip_cooldown_extra"]
print(f"\n  >>> Best flip_cooldown_extra: {best_cd_extra}")


# ══════════════════════════════════════════════════════════
# PHASE 1 SUMMARY
# ══════════════════════════════════════════════════════════

phase1_sorted = print_batch_summary("PHASE 1: ALL PER-COIN EXPERIMENTS", all_results, BASELINE_PNL)
best_phase1 = phase1_sorted[0]

print(f"\n{'='*70}")
print(f"PHASE 1 WINNER: {best_phase1['name']}")
print(f"  PnL: ${best_phase1['total_pnl']:+,.0f} | WR: {best_phase1['win_rate']:.1f}% | "
      f"Sharpe: {best_phase1['sharpe']:.2f}")
print(f"  Params: {best_phase1['params']}")
print(f"{'='*70}")


# ══════════════════════════════════════════════════════════
# PHASE 2: PORTFOLIO LEVEL (4 experiments)
# ══════════════════════════════════════════════════════════

print("\n" + "#" * 70)
print("# PHASE 2: PORTFOLIO SIMULATION")
print("#" * 70)

# First, prepare data for portfolio sim (need alt_df_full + signals_full per coin)
def prepare_portfolio_data(flip_mode, min_bars_flip, cd_extra, hyst_band, conf_bars):
    """Prepare per-coin results for portfolio simulation."""
    results_for_port = []
    for coin in COINS:
        cfg = V6_CONFIGS[coin]
        sig, alt_merged = gen_signal_with_options(
            v6_score_ts, alt_data[coin],
            entry_threshold=cfg["threshold"],
            hysteresis_band=hyst_band,
            use_alt_pa=False,
            confirm_bars=conf_bars,
        )

        oos_mask = (alt_merged["ts"] >= pd.Timestamp(OOS_START))
        if OOS_END:
            oos_mask &= (alt_merged["ts"] <= pd.Timestamp(OOS_END))

        alt_oos = alt_merged[oos_mask].reset_index(drop=True)
        sig_oos = sig[oos_mask].reset_index(drop=True)

        if len(alt_oos) < 100:
            continue

        # Run per-coin backtest to get half_kelly
        trades = run_backtest_flip(
            alt_oos, sig_oos,
            sl_atr_mult=cfg["sl"], tp_atr_mult=cfg["tp"],
            max_hold_bars=96, cooldown_bars=cfg["cd"],
            flip_mode=flip_mode,
            min_bars_before_flip=min_bars_flip,
            flip_cooldown_extra=cd_extra,
        )

        if len(trades) > 0:
            m = calc_metrics(trades, len(alt_oos))
            wr = m["win_rate"] / 100
            rr = m["rr"]
            kelly = (wr * rr - (1 - wr)) / rr if rr > 0 and wr > 0 else 0.01
            half_kelly = max(kelly / 2, 0.005)
        else:
            half_kelly = 0.01

        results_for_port.append({
            "coin": coin,
            "config": cfg,
            "alt_df_full": alt_oos,
            "signals_full": sig_oos,
            "half_kelly": half_kelly,
        })

    return results_for_port


# P1: No flip (current portfolio sim behavior), max_concurrent=3
print("\n  [P1] No flip, max_concurrent=3...")
p1_data = prepare_portfolio_data("disabled", 0, 0, 0.0, 1)
p1_result = run_portfolio_flip(p1_data, max_concurrent=3, flip_mode="disabled")
p1_pnl = p1_result["metrics_full"]["net_pnl"] if p1_result else 0
p1_trades = p1_result["metrics_full"]["total"] if p1_result else 0
print(f"    P1 (no flip, mc=3): {p1_trades} trades, ${p1_pnl:+,.0f}")

# Get best Phase 1 params
bp = best_phase1["params"]

# P2: Best Phase 1 settings, max_concurrent=3
print(f"\n  [P2] Best Phase 1 ({bp}), max_concurrent=3...")
p2_data = prepare_portfolio_data(
    bp["flip_mode"], bp["min_bars_before_flip"],
    bp["flip_cooldown_extra"], bp["hysteresis_band"], bp["confirm_bars"]
)
p2_result = run_portfolio_flip(
    p2_data, max_concurrent=3,
    flip_mode=bp["flip_mode"],
    min_bars_before_flip=bp["min_bars_before_flip"],
    flip_cooldown_extra=bp["flip_cooldown_extra"],
)
p2_pnl = p2_result["metrics_full"]["net_pnl"] if p2_result else 0
p2_trades = p2_result["metrics_full"]["total"] if p2_result else 0
print(f"    P2 (best P1, mc=3): {p2_trades} trades, ${p2_pnl:+,.0f}")

# P3: Best Phase 1 settings, max_concurrent=5
print(f"\n  [P3] Best Phase 1, max_concurrent=5...")
p3_result = run_portfolio_flip(
    p2_data, max_concurrent=5,
    flip_mode=bp["flip_mode"],
    min_bars_before_flip=bp["min_bars_before_flip"],
    flip_cooldown_extra=bp["flip_cooldown_extra"],
)
p3_pnl = p3_result["metrics_full"]["net_pnl"] if p3_result else 0
p3_trades = p3_result["metrics_full"]["total"] if p3_result else 0
print(f"    P3 (best P1, mc=5): {p3_trades} trades, ${p3_pnl:+,.0f}")

# P4: Best Phase 1 settings, max_concurrent=10
print(f"\n  [P4] Best Phase 1, max_concurrent=10...")
p4_result = run_portfolio_flip(
    p2_data, max_concurrent=10,
    flip_mode=bp["flip_mode"],
    min_bars_before_flip=bp["min_bars_before_flip"],
    flip_cooldown_extra=bp["flip_cooldown_extra"],
)
p4_pnl = p4_result["metrics_full"]["net_pnl"] if p4_result else 0
p4_trades = p4_result["metrics_full"]["total"] if p4_result else 0
print(f"    P4 (best P1, mc=10): {p4_trades} trades, ${p4_pnl:+,.0f}")

portfolio_results = [
    {"name": "P1_no_flip_mc3", "pnl": p1_pnl, "trades": p1_trades,
     "metrics": p1_result["metrics_full"] if p1_result else {}},
    {"name": "P2_best_mc3", "pnl": p2_pnl, "trades": p2_trades,
     "metrics": p2_result["metrics_full"] if p2_result else {}},
    {"name": "P3_best_mc5", "pnl": p3_pnl, "trades": p3_trades,
     "metrics": p3_result["metrics_full"] if p3_result else {}},
    {"name": "P4_best_mc10", "pnl": p4_pnl, "trades": p4_trades,
     "metrics": p4_result["metrics_full"] if p4_result else {}},
]

print(f"\n{'='*70}")
print("PHASE 2: PORTFOLIO SUMMARY")
print(f"{'='*70}")
for pr in sorted(portfolio_results, key=lambda x: x["pnl"], reverse=True):
    m = pr["metrics"]
    wr = m.get("win_rate", 0)
    dd = m.get("max_dd", 0)
    print(f"  {pr['name']:<25} {pr['trades']:>5} trades | WR {wr:>5.1f}% | "
          f"PnL ${pr['pnl']:>+9,.0f} | DD {dd:.1f}%")

best_portfolio = max(portfolio_results, key=lambda x: x["pnl"])
print(f"\n  >>> Best portfolio: {best_portfolio['name']} (${best_portfolio['pnl']:+,.0f})")


# ══════════════════════════════════════════════════════════
# PHASE 3: COMBINED CHAMPION
# ══════════════════════════════════════════════════════════

print("\n" + "#" * 70)
print("# PHASE 3: COMBINED CHAMPION")
print("#" * 70)

# Combine winners from each group
phase3_results = []

# C1: Best from each group combined
c1_params = {
    "flip_mode": best_flip_mode,
    "min_bars_before_flip": best_min_bars,
    "flip_cooldown_extra": best_cd_extra if best_flip_mode == "exit_only" else 0,
    "hysteresis_band": best_hyst,
    "confirm_bars": best_confirm,
}
r = run_contender("C1_combined_winners", **c1_params,
                  description="Combined best from each Phase 1 group")
phase3_results.append(r)

# C2: Disabled flip + best hysteresis + best confirmation
r = run_contender("C2_disabled_hyst_confirm",
                  flip_mode="disabled",
                  hysteresis_band=best_hyst,
                  confirm_bars=best_confirm,
                  description="No flip + best hyst + best confirm")
phase3_results.append(r)

# C3: Exit-only + best min_bars + best hysteresis
r = run_contender("C3_exit_minbars_hyst",
                  flip_mode="exit_only",
                  min_bars_before_flip=best_min_bars,
                  hysteresis_band=best_hyst,
                  flip_cooldown_extra=best_cd_extra,
                  description="Exit-only + best min_bars + best hyst + best cd_extra")
phase3_results.append(r)

# C4: Disabled flip + strong hysteresis (3.0) + confirm 2
r = run_contender("C4_conservative",
                  flip_mode="disabled",
                  hysteresis_band=3.0,
                  confirm_bars=2,
                  description="Conservative: no flip, max hyst, 2-bar confirm")
phase3_results.append(r)

# C5: Exit-only + high min_bars (16) + hysteresis 2.0
r = run_contender("C5_exit_aggressive_guard",
                  flip_mode="exit_only",
                  min_bars_before_flip=16,
                  hysteresis_band=2.0,
                  flip_cooldown_extra=8,
                  description="Exit-only with aggressive guards")
phase3_results.append(r)

phase3_sorted = print_batch_summary("PHASE 3: COMBINED CHAMPION", phase3_results, BASELINE_PNL)

# Portfolio sim for Phase 3 winner
best_p3 = phase3_sorted[0]
bp3 = best_p3["params"]
print(f"\n  Running portfolio sim for Phase 3 winner: {best_p3['name']}...")
p3_winner_data = prepare_portfolio_data(
    bp3["flip_mode"], bp3["min_bars_before_flip"],
    bp3["flip_cooldown_extra"], bp3["hysteresis_band"], bp3["confirm_bars"]
)
p3_winner_port = run_portfolio_flip(
    p3_winner_data, max_concurrent=3,
    flip_mode=bp3["flip_mode"],
    min_bars_before_flip=bp3["min_bars_before_flip"],
    flip_cooldown_extra=bp3["flip_cooldown_extra"],
)
if p3_winner_port:
    pm = p3_winner_port["metrics_full"]
    print(f"    Portfolio: {pm['total']} trades | WR {pm['win_rate']:.1f}% | "
          f"PnL ${pm['net_pnl']:+,.0f} | DD {pm['max_dd']:.1f}%")


# ══════════════════════════════════════════════════════════
# FINAL SUMMARY + SAVE RESULTS
# ══════════════════════════════════════════════════════════

print("\n" + "=" * 70)
print("TOURNAMENT ROUND 3: FINAL RESULTS")
print("=" * 70)

# Overall champion (per-coin)
all_phase1_and_3 = all_results + phase3_results
overall_sorted = sorted(all_phase1_and_3, key=lambda x: x["total_pnl"], reverse=True)
champion = overall_sorted[0]

print(f"\n  CHAMPION (per-coin): {champion['name']}")
print(f"  PnL: ${champion['total_pnl']:+,.0f} | WR: {champion['win_rate']:.1f}% | "
      f"Sharpe: {champion['sharpe']:.2f} | DD: {champion['max_dd_pct']:.1f}%")
print(f"  Params: {champion['params']}")

# Exit breakdown
bd = champion.get("exit_breakdown", {})
print(f"\n  Exit Breakdown:")
for reason in ["SL", "TP", "TIMEOUT", "SIGNAL_FLIP"]:
    if reason in bd:
        e = bd[reason]
        print(f"    {reason:<15} {e['count']:>5} trades | ${e['pnl']:>+9,.0f} | "
              f"WR {e['wr']:>5.1f}% | avg {e['avg_bars']:.0f} bars")

# Regime analysis
print(f"\n  Regime Analysis:")
for rname, rdata in champion.get("regime_results", {}).items():
    print(f"    {rname:<10} {rdata['trades']:>4} trades | ${rdata['pnl']:>+8,.0f} | WR {rdata['wr']:.1f}%")

# Per-coin
print(f"\n  Per-Coin:")
for coin in COINS:
    if coin in champion.get("coin_results", {}):
        cr = champion["coin_results"][coin]
        print(f"    {coin:<5} {cr['trades']:>4} trades | ${cr['pnl']:>+8,.0f} | "
              f"WR {cr['wr']:.1f}% | Sharpe {cr['sharpe']:.2f}")

# Baseline comparison
print(f"\n  vs Baseline (reverse flip): ${champion['total_pnl'] - BASELINE_PNL:+,.0f} "
      f"({(champion['total_pnl'] - BASELINE_PNL) / max(abs(BASELINE_PNL), 1) * 100:+.1f}%)")

# Portfolio results
if p3_winner_port:
    print(f"\n  Champion Portfolio (mc=3): ${pm['net_pnl']:+,.0f} | "
          f"WR {pm['win_rate']:.1f}% | DD {pm['max_dd']:.1f}%")

# Save results
final_results = {
    "tournament": "round_3_signal_flip",
    "run_date": datetime.now().isoformat(),
    "baseline_pnl": BASELINE_PNL,
    "champion": {
        "name": champion["name"],
        "params": champion["params"],
        "total_pnl": champion["total_pnl"],
        "win_rate": champion["win_rate"],
        "sharpe": champion["sharpe"],
        "max_dd_pct": champion["max_dd_pct"],
        "exit_breakdown": champion.get("exit_breakdown", {}),
        "regime_results": champion.get("regime_results", {}),
        "coin_results": champion.get("coin_results", {}),
    },
    "portfolio_results": portfolio_results,
    "phase3_portfolio": {
        "name": best_p3["name"],
        "metrics": p3_winner_port["metrics_full"] if p3_winner_port else {},
    },
    "all_experiments": [{
        "name": r["name"],
        "params": r["params"],
        "total_pnl": r["total_pnl"],
        "total_trades": r["total_trades"],
        "win_rate": r["win_rate"],
        "sharpe": r["sharpe"],
        "max_dd_pct": r["max_dd_pct"],
        "exit_breakdown": r.get("exit_breakdown", {}),
    } for r in all_phase1_and_3],
    "group_winners": {
        "A_flip_mode": best_flip_mode,
        "B_min_bars": best_min_bars,
        "C_hysteresis": best_hyst,
        "D_confirm": best_confirm,
        "E_cd_extra": best_cd_extra,
    },
}

with open(RESULTS_FILE, "w") as f:
    json.dump(final_results, f, indent=2, default=str)

print(f"\n  Results saved to {RESULTS_FILE}")
print(f"  Total elapsed: {time.time()-t0:.0f}s")
print("=" * 70)
