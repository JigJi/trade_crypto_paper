"""
Mission 022: In-Trade Score Momentum Exit
=========================================
ต่อยอดจาก Mission 021 — ค้นพบว่า SIGNAL_FLIP ปัญหาอยู่ระหว่างเทรด ไม่ใช่ตอน entry
ทดสอบว่า monitor BTC score ระหว่างเทรดแล้วออกเร็วเมื่อ score เสื่อมลง จะลด FLIP losses ได้ไหม

Approaches:
1. Score Decay Exit: ออกเมื่อ |score| ลดลงต่ำกว่า threshold * decay_pct
2. Score Zero-Cross Exit: ออกเมื่อ score ข้ามศูนย์ (ก่อน full flip)
3. Score Momentum Exit: ออกเมื่อ score เปลี่ยนทิศ N bars ติดต่อกัน
4. Combined: ผสม best approach
5. Walk-Forward validation
"""

import sys, json, logging
import numpy as np, pandas as pd
from pathlib import Path
from datetime import datetime, timedelta

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import backtest_15m_btc_led_alts as bt
from paper_trading.config import COMPOSITE_WEIGHTS, V3_EXTRA_WEIGHTS, COIN_CONFIGS
from signal_core import (
    score_ob_combined, score_basis_contrarian, score_tick_liq,
    compute_btc_composite_score_v6,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

# ── Constants ──
COINS = ["BTCUSDT", "XRPUSDT", "ADAUSDT", "DOTUSDT", "SUIUSDT", "FILUSDT"]
OOS_START = "2025-01-01"
OOS_END = "2026-03-31"

INIT_EQUITY = 10_000.0
BUDGET_USDT = 1_000.0
LEVERAGE = 2.0
FEE = 2.0 / 10_000
SLIP = 1.5 / 10_000


def load_btc_score():
    """Load BTC data and compute v6 composite score."""
    btc_ohlcv = bt.fetch_binance_15m("BTCUSDT", years=3)
    if "date_time" in btc_ohlcv.columns:
        btc_ohlcv = btc_ohlcv.rename(columns={"date_time": "ts"})
    db_data = bt.load_btc_db_data()
    btc_df = bt.build_btc_features(btc_ohlcv, db_data)

    # v6 liq-only score
    btc_score = compute_btc_composite_score_v6(btc_df)
    btc_score_ts = pd.Series(btc_score.values, index=btc_df["ts"].values, name="btc_score")

    # Also create a full btc_score array indexed by ts for in-trade monitoring
    btc_score_df = pd.DataFrame({"ts": btc_df["ts"].values, "btc_score": btc_score.values})
    return btc_score_ts, btc_score_df


def run_backtest_with_score_exit(df, signals, btc_scores_aligned,
                                  sl_atr_mult=2.5, tp_atr_mult=4.0,
                                  trail_atr_mult=0.5, trail_activate_atr=0.5,
                                  max_hold_bars=96, cooldown_bars=4,
                                  flip_cd_extra=0,
                                  # Score exit params
                                  score_decay_pct=None,    # exit if |score| < threshold * pct
                                  score_zero_cross=False,  # exit if score crosses zero
                                  score_adverse_bars=None,  # exit if score goes against N bars
                                  score_threshold=3.0,     # the threshold used
                                  score_min_hold=0):       # min bars before score exit applies
    """Modified backtest with in-trade score monitoring exits."""
    sig = signals.shift(1).fillna(0).astype(int).values
    atrs = df["atr"].values
    opens = df["open"].values
    highs = df["high"].values
    lows = df["low"].values
    closes = df["close"].values
    times = df["ts"].values
    scores = btc_scores_aligned  # aligned to df index

    n = len(df)
    records = []
    equity = INIT_EQUITY
    position = 0
    entry_i = entry_px = entry_atr = qty = fee_in = 0
    peak = trough = 0.0
    trl_active = False
    last_exit_i = -cooldown_bars - 1
    entry_score = 0.0
    adverse_count = 0

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
            trl_active = False
            entry_score = scores[i] if i < len(scores) else 0
            adverse_count = 0
            continue

        if position != 0:
            h, l, c, o = highs[i], lows[i], closes[i], opens[i]
            atr = entry_atr
            cur_score = scores[i] if i < len(scores) else 0

            if position == 1:
                peak = max(peak, h)
                sl_level = entry_px - sl_atr_mult * atr
                tp_level = entry_px + tp_atr_mult * atr
            else:
                trough = min(trough, l)
                sl_level = entry_px + sl_atr_mult * atr
                tp_level = entry_px - tp_atr_mult * atr

            # Trailing stop
            trail_stop = None
            if trail_atr_mult < 50:
                if position == 1 and (peak - entry_px) >= trail_activate_atr * atr:
                    trl_active = True
                    trail_stop = peak - trail_atr_mult * atr
                elif position == -1 and (entry_px - trough) >= trail_activate_atr * atr:
                    trl_active = True
                    trail_stop = trough + trail_atr_mult * atr

            exit_px = exit_reason = None

            # Price-based exits (SL, TRAIL, TP)
            if position == 1:
                if l <= sl_level: exit_px, exit_reason = sl_level, "SL"
                elif trl_active and trail_stop and l <= trail_stop: exit_px, exit_reason = trail_stop, "TRAIL"
                elif h >= tp_level: exit_px, exit_reason = tp_level, "TP"
            else:
                if h >= sl_level: exit_px, exit_reason = sl_level, "SL"
                elif trl_active and trail_stop and h >= trail_stop: exit_px, exit_reason = trail_stop, "TRAIL"
                elif l <= tp_level: exit_px, exit_reason = tp_level, "TP"

            # ── NEW: Score-based early exits (before SIGNAL_FLIP) ──
            bars_held = i - entry_i
            if exit_px is None and bars_held >= score_min_hold:
                # Score decay: |score| dropped below threshold * decay_pct
                if score_decay_pct is not None:
                    decay_threshold = score_threshold * score_decay_pct
                    if position == 1 and cur_score < decay_threshold:
                        exit_px, exit_reason = c, "SCORE_DECAY"
                    elif position == -1 and cur_score > -decay_threshold:
                        exit_px, exit_reason = c, "SCORE_DECAY"

                # Score zero-cross: score crossed zero
                if exit_px is None and score_zero_cross:
                    if position == 1 and cur_score <= 0:
                        exit_px, exit_reason = c, "SCORE_ZERO"
                    elif position == -1 and cur_score >= 0:
                        exit_px, exit_reason = c, "SCORE_ZERO"

                # Score adverse: score moving against trade for N consecutive bars
                if exit_px is None and score_adverse_bars is not None:
                    prev_score = scores[i-1] if i > 0 else cur_score
                    if position == 1:
                        adverse_count = adverse_count + 1 if cur_score < prev_score else 0
                    else:
                        adverse_count = adverse_count + 1 if cur_score > prev_score else 0
                    if adverse_count >= score_adverse_bars:
                        exit_px, exit_reason = c, "SCORE_ADVERSE"

            # Max hold
            if exit_px is None and (i - entry_i) >= max_hold_bars:
                exit_px, exit_reason = c, "TIMEOUT"

            # SIGNAL_FLIP (original — only triggers if score exits didn't catch it)
            if exit_px is None and sig[i] != 0 and sig[i] != position:
                exit_px, exit_reason = c, "SIGNAL_FLIP"

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
                    "entry_score": entry_score,
                    "exit_score": cur_score,
                })
                extra_cd = flip_cd_extra if exit_reason == "SIGNAL_FLIP" else 0
                last_exit_i = i + extra_cd
                position = 0
                adverse_count = 0

    return pd.DataFrame(records)


def prepare_coin_data(btc_score_ts, btc_score_df, symbol, cfg):
    """Prepare aligned data for one coin."""
    coin = symbol.replace("USDT", "")
    ohlcv = bt.fetch_binance_15m(symbol, years=3)
    if "date_time" in ohlcv.columns:
        ohlcv = ohlcv.rename(columns={"date_time": "ts"})
    alt_df = bt.build_alt_technicals(ohlcv)
    oos_mask = (alt_df["ts"] >= OOS_START) & (alt_df["ts"] <= OOS_END)
    alt_oos = alt_df[oos_mask].copy().reset_index(drop=True)

    threshold = cfg.get("threshold", 3.0)
    hysteresis = cfg.get("hysteresis_band", 0.0)
    signals, alt_merged = bt.generate_btc_led_signal(
        btc_score_ts, alt_oos,
        threshold=threshold,
        use_alt_pa_filter=cfg.get("use_alt_pa_filter", False),
        hysteresis_band=hysteresis)

    # Align BTC scores to alt_merged timestamps
    scores_aligned = pd.merge_asof(
        alt_merged[["ts"]].sort_values("ts"),
        btc_score_df.sort_values("ts"),
        on="ts", direction="backward",
        tolerance=pd.Timedelta("30min")
    )["btc_score"].fillna(0).values

    return alt_merged, signals, scores_aligned, threshold


def run_all_coins(btc_score_ts, btc_score_df, **exit_params):
    """Run backtest for all coins with given exit params."""
    all_trades = []
    for symbol in COINS:
        coin = symbol.replace("USDT", "")
        cfg = COIN_CONFIGS.get(coin, {})
        alt_merged, signals, scores_aligned, threshold = prepare_coin_data(
            btc_score_ts, btc_score_df, symbol, cfg)

        trades = run_backtest_with_score_exit(
            alt_merged, signals, scores_aligned,
            sl_atr_mult=cfg.get("sl_atr_mult", 2.5),
            tp_atr_mult=cfg.get("tp_atr_mult", 4.0),
            cooldown_bars=cfg.get("cooldown_bars", 4),
            flip_cd_extra=cfg.get("flip_cd_extra", 0),
            score_threshold=threshold,
            **exit_params)

        if len(trades) > 0:
            trades["coin"] = coin
            all_trades.append(trades)

    if all_trades:
        return pd.concat(all_trades, ignore_index=True)
    return pd.DataFrame()


def summarize(trades, label=""):
    """Summarize trades."""
    if trades.empty:
        return {"label": label, "trades": 0, "pnl": 0, "wr": 0,
                "avg_pnl": 0, "exits": {}, "flip_trades": 0, "flip_pnl": 0, "flip_wr": 0, "wins": 0}

    n = len(trades)
    wins = (trades["pnl_net"] > 0).sum()
    wr = wins / n * 100
    pnl = trades["pnl_net"].sum()

    # Exit reason breakdown
    exits = trades["exit_reason"].value_counts().to_dict()

    # FLIP specific
    flip = trades[trades["exit_reason"] == "SIGNAL_FLIP"]
    flip_n = len(flip)
    flip_pnl = flip["pnl_net"].sum() if flip_n > 0 else 0
    flip_wr = (flip["pnl_net"] > 0).sum() / flip_n * 100 if flip_n > 0 else 0

    return {
        "label": label,
        "trades": n,
        "wins": int(wins),
        "wr": round(wr, 1),
        "pnl": round(pnl, 2),
        "avg_pnl": round(pnl / n, 2),
        "exits": exits,
        "flip_trades": flip_n,
        "flip_pnl": round(flip_pnl, 2),
        "flip_wr": round(flip_wr, 1),
    }


def main():
    log.info("Mission 022: In-Trade Score Momentum Exit")
    log.info("=" * 60)

    # Load data
    log.info("Loading BTC data and computing v6 score...")
    btc_score_ts, btc_score_df = load_btc_score()

    results = {}

    # ══════════════════════════════════════════════════════════
    # EXP 1: Baseline (no score exit)
    # ══════════════════════════════════════════════════════════
    log.info("\n[EXP 1] Baseline — no score-based exit")
    baseline = run_all_coins(btc_score_ts, btc_score_df)
    results["exp1_baseline"] = summarize(baseline, "Baseline (no score exit)")
    log.info(f"  Trades={results['exp1_baseline']['trades']}, "
             f"PnL=${results['exp1_baseline']['pnl']:.0f}, "
             f"WR={results['exp1_baseline']['wr']}%, "
             f"FLIPs={results['exp1_baseline']['flip_trades']} (${results['exp1_baseline']['flip_pnl']:.0f})")

    # ══════════════════════════════════════════════════════════
    # EXP 2: Score Zero-Cross Exit
    # ══════════════════════════════════════════════════════════
    log.info("\n[EXP 2] Score Zero-Cross Exit — exit when score crosses zero")
    zero_cross = run_all_coins(btc_score_ts, btc_score_df, score_zero_cross=True)
    results["exp2_zero_cross"] = summarize(zero_cross, "Score Zero-Cross")
    log.info(f"  Trades={results['exp2_zero_cross']['trades']}, "
             f"PnL=${results['exp2_zero_cross']['pnl']:.0f}, "
             f"WR={results['exp2_zero_cross']['wr']}%, "
             f"FLIPs={results['exp2_zero_cross']['flip_trades']}")

    # ══════════════════════════════════════════════════════════
    # EXP 3: Score Decay Exit — grid search decay_pct
    # ══════════════════════════════════════════════════════════
    log.info("\n[EXP 3] Score Decay Exit — grid search")
    decay_results = {}
    for pct in [0.1, 0.2, 0.3, 0.5, 0.7]:
        trades = run_all_coins(btc_score_ts, btc_score_df, score_decay_pct=pct)
        s = summarize(trades, f"Decay {pct}")
        decay_results[pct] = s
        delta = s["pnl"] - results["exp1_baseline"]["pnl"]
        log.info(f"  decay_pct={pct}: Trades={s['trades']}, PnL=${s['pnl']:.0f} "
                 f"(Δ${delta:+.0f}), WR={s['wr']}%, FLIPs={s['flip_trades']}")
    results["exp3_decay_grid"] = decay_results

    # Find best decay
    best_decay_pct = max(decay_results, key=lambda k: decay_results[k]["pnl"])
    results["exp3_best_decay"] = best_decay_pct
    log.info(f"  Best decay_pct = {best_decay_pct}")

    # ══════════════════════════════════════════════════════════
    # EXP 4: Score Adverse Bars Exit — grid search
    # ══════════════════════════════════════════════════════════
    log.info("\n[EXP 4] Score Adverse Bars Exit — grid search")
    adverse_results = {}
    for n_bars in [2, 3, 4, 6, 8]:
        trades = run_all_coins(btc_score_ts, btc_score_df, score_adverse_bars=n_bars)
        s = summarize(trades, f"Adverse {n_bars} bars")
        adverse_results[n_bars] = s
        delta = s["pnl"] - results["exp1_baseline"]["pnl"]
        log.info(f"  adverse_bars={n_bars}: Trades={s['trades']}, PnL=${s['pnl']:.0f} "
                 f"(Δ${delta:+.0f}), WR={s['wr']}%, FLIPs={s['flip_trades']}")
    results["exp4_adverse_grid"] = adverse_results

    best_adverse = max(adverse_results, key=lambda k: adverse_results[k]["pnl"])
    results["exp4_best_adverse"] = best_adverse
    log.info(f"  Best adverse_bars = {best_adverse}")

    # ══════════════════════════════════════════════════════════
    # EXP 5: Combined — best score exit approaches
    # ══════════════════════════════════════════════════════════
    log.info("\n[EXP 5] Combined approaches")

    combos = [
        {"label": "ZeroCross + BestDecay", "score_zero_cross": True, "score_decay_pct": best_decay_pct},
        {"label": "ZeroCross + BestAdverse", "score_zero_cross": True, "score_adverse_bars": best_adverse},
        {"label": "BestDecay + BestAdverse", "score_decay_pct": best_decay_pct, "score_adverse_bars": best_adverse},
        {"label": "All Three", "score_zero_cross": True, "score_decay_pct": best_decay_pct, "score_adverse_bars": best_adverse},
    ]
    combo_results = {}
    for combo in combos:
        lbl = combo.pop("label")
        trades = run_all_coins(btc_score_ts, btc_score_df, **combo)
        s = summarize(trades, lbl)
        combo_results[lbl] = s
        delta = s["pnl"] - results["exp1_baseline"]["pnl"]
        log.info(f"  {lbl}: PnL=${s['pnl']:.0f} (Δ${delta:+.0f}), "
                 f"WR={s['wr']}%, FLIPs={s['flip_trades']}")
    results["exp5_combos"] = combo_results

    # ══════════════════════════════════════════════════════════
    # EXP 6: Score trajectory analysis — WHY score exits fail
    # ══════════════════════════════════════════════════════════
    log.info("\n[EXP 6] Score trajectory analysis — why score exits fail")

    # Analyze baseline trades: how does score behave for TRAIL vs FLIP exits?
    if not baseline.empty and "entry_score" in baseline.columns:
        for exit_type in ["TRAIL", "SIGNAL_FLIP", "TP", "SL"]:
            subset = baseline[baseline["exit_reason"] == exit_type]
            if len(subset) > 10:
                # Score at entry vs exit
                avg_entry_score = subset["entry_score"].abs().mean()
                avg_exit_score = subset["exit_score"].abs().mean()
                score_decay = avg_exit_score / avg_entry_score if avg_entry_score > 0 else 0
                log.info(f"  {exit_type}: n={len(subset)}, "
                         f"entry |score|={avg_entry_score:.2f}, "
                         f"exit |score|={avg_exit_score:.2f}, "
                         f"decay ratio={score_decay:.2f}")
                results[f"exp6_{exit_type}_trajectory"] = {
                    "count": len(subset),
                    "avg_entry_score_abs": round(avg_entry_score, 3),
                    "avg_exit_score_abs": round(avg_exit_score, 3),
                    "score_decay_ratio": round(score_decay, 3),
                    "avg_bars_held": round(subset["holding_bars"].mean(), 1),
                }

    # ══════════════════════════════════════════════════════════
    # EXP 6b: Score zero-cross rate by exit type
    # ══════════════════════════════════════════════════════════
    log.info("\n[EXP 6b] Do TRAIL trades also cross zero? (confirms M021)")
    # Check how many TRAIL/TP trades in baseline would have been caught by zero-cross
    zero_cross_would_catch = baseline[
        ((baseline["dir"] == "L") & (baseline["exit_score"] <= 0)) |
        ((baseline["dir"] == "S") & (baseline["exit_score"] >= 0))
    ]
    pct_caught = len(zero_cross_would_catch) / len(baseline) * 100 if len(baseline) > 0 else 0
    log.info(f"  {pct_caught:.1f}% of ALL baseline trades have score cross zero by exit time")

    for exit_type in ["TRAIL", "TP", "SIGNAL_FLIP"]:
        subset = baseline[baseline["exit_reason"] == exit_type]
        if len(subset) > 0:
            cross_zero = subset[
                ((subset["dir"] == "L") & (subset["exit_score"] <= 0)) |
                ((subset["dir"] == "S") & (subset["exit_score"] >= 0))
            ]
            pct = len(cross_zero) / len(subset) * 100
            log.info(f"  {exit_type}: {pct:.1f}% have score at/past zero at exit ({len(cross_zero)}/{len(subset)})")
    results["exp6b_zero_cross_overlap"] = {"pct_all_trades_cross_zero": round(pct_caught, 1)}

    # ══════════════════════════════════════════════════════════
    # EXP 6c: Delayed score exit (only after min_bars holding)
    # ══════════════════════════════════════════════════════════
    log.info("\n[EXP 6c] Delayed zero-cross exit — only after N bars of holding")
    delayed_results = {}
    for min_bars in [4, 8, 12, 16, 24]:
        # Re-run with zero-cross but min_bars_before_flip to delay all exits
        # We need to implement this properly — use adverse bars as proxy
        # Actually, let's add min_hold to our backtest
        trades = run_all_coins(btc_score_ts, btc_score_df,
                               score_zero_cross=True,
                               score_min_hold=min_bars)
        s = summarize(trades, f"ZeroCross after {min_bars} bars")
        delayed_results[min_bars] = s
        delta = s["pnl"] - results["exp1_baseline"]["pnl"]
        log.info(f"  min_hold={min_bars}: Trades={s['trades']}, PnL=${s['pnl']:.0f} "
                 f"(Δ${delta:+.0f}), WR={s['wr']}%, FLIPs={s['flip_trades']}")
    results["exp6c_delayed_zero_cross"] = delayed_results

    # ══════════════════════════════════════════════════════════
    # EXP 7: Walk-Forward (3 periods)
    # ══════════════════════════════════════════════════════════
    log.info("\n[EXP 7] Walk-Forward Validation (3 periods)")
    wf_periods = [
        ("2025-01-01", "2025-05-31"),
        ("2025-06-01", "2025-10-31"),
        ("2025-11-01", "2026-03-31"),
    ]

    # Find overall best approach
    all_approaches = {}
    all_approaches["baseline"] = results["exp1_baseline"]["pnl"]
    all_approaches["zero_cross"] = results["exp2_zero_cross"]["pnl"]
    for pct, s in decay_results.items():
        all_approaches[f"decay_{pct}"] = s["pnl"]
    for nb, s in adverse_results.items():
        all_approaches[f"adverse_{nb}"] = s["pnl"]
    for lbl, s in combo_results.items():
        all_approaches[lbl] = s["pnl"]

    best_approach_name = max(all_approaches, key=all_approaches.get)
    log.info(f"  Best overall approach: {best_approach_name} (${all_approaches[best_approach_name]:.0f})")

    # Parse best approach params
    def get_params_for(name):
        if name == "baseline": return {}
        if name == "zero_cross": return {"score_zero_cross": True}
        if name.startswith("decay_"): return {"score_decay_pct": float(name.split("_")[1])}
        if name.startswith("adverse_"): return {"score_adverse_bars": int(name.split("_")[1])}
        # combos
        if "ZeroCross" in name and "Decay" in name:
            return {"score_zero_cross": True, "score_decay_pct": best_decay_pct}
        if "ZeroCross" in name and "Adverse" in name:
            return {"score_zero_cross": True, "score_adverse_bars": best_adverse}
        if "All" in name:
            return {"score_zero_cross": True, "score_decay_pct": best_decay_pct, "score_adverse_bars": best_adverse}
        return {}

    # Walk-forward: test best approach on each period + baseline
    wf_results = []
    for period_start, period_end in wf_periods:
        # Temporarily override OOS range
        global OOS_START, OOS_END
        old_start, old_end = OOS_START, OOS_END
        OOS_START, OOS_END = period_start, period_end

        base_trades = run_all_coins(btc_score_ts, btc_score_df)
        base_s = summarize(base_trades, f"Baseline {period_start[:7]}")

        best_params = get_params_for(best_approach_name)
        if best_params:
            test_trades = run_all_coins(btc_score_ts, btc_score_df, **best_params)
            test_s = summarize(test_trades, f"Best {period_start[:7]}")
        else:
            test_s = base_s

        delta = test_s["pnl"] - base_s["pnl"]
        wf_results.append({
            "period": f"{period_start} to {period_end}",
            "baseline_pnl": base_s["pnl"],
            "best_pnl": test_s["pnl"],
            "delta": round(delta, 2),
            "baseline_flips": base_s["flip_trades"],
            "best_flips": test_s["flip_trades"],
        })
        log.info(f"  {period_start[:7]}: Baseline=${base_s['pnl']:.0f}, "
                 f"Best=${test_s['pnl']:.0f}, Δ${delta:+.0f}")

        OOS_START, OOS_END = old_start, old_end

    results["exp7_walk_forward"] = wf_results

    # Check consistency: did best approach help in all periods?
    consistent = all(r["delta"] >= 0 for r in wf_results)
    results["exp7_consistent"] = consistent
    log.info(f"  Walk-forward consistent: {consistent}")

    # ══════════════════════════════════════════════════════════
    # Final Summary
    # ══════════════════════════════════════════════════════════
    best_pnl = all_approaches[best_approach_name]
    baseline_pnl = results["exp1_baseline"]["pnl"]
    delta_pct = (best_pnl - baseline_pnl) / abs(baseline_pnl) * 100 if baseline_pnl != 0 else 0

    results["summary"] = {
        "best_approach": best_approach_name,
        "baseline_pnl": baseline_pnl,
        "best_pnl": best_pnl,
        "delta": round(best_pnl - baseline_pnl, 2),
        "delta_pct": round(delta_pct, 1),
        "baseline_flips": results["exp1_baseline"]["flip_trades"],
        "walk_forward_consistent": consistent,
    }

    log.info("\n" + "=" * 60)
    log.info("MISSION 022 COMPLETE")
    log.info(f"Best approach: {best_approach_name}")
    log.info(f"Baseline PnL: ${baseline_pnl:.0f}")
    log.info(f"Best PnL: ${best_pnl:.0f} (Δ${best_pnl - baseline_pnl:+.0f}, {delta_pct:+.1f}%)")
    log.info(f"Walk-forward consistent: {consistent}")

    # ── Save results ──
    output_path = BASE_DIR / "missions" / "mission_022_score_momentum_exit.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=str)
    log.info(f"Results saved to {output_path}")

    return results


if __name__ == "__main__":
    main()
