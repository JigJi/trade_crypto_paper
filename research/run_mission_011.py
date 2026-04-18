"""
Mission 011: Signal Alpha Decay & Entry Timing
================================================
สมมติฐาน: BTC composite score alpha จะ decay ไปตามเวลา
เราเข้า T+1 (shift(1)) แต่ถ้าช้าไป T+2, T+3 จะเสีย alpha เท่าไหร่?
และ signal strength สูง decay ช้ากว่า signal อ่อนหรือไม่?

Experiments:
1. Entry delay sweep: shift(1) through shift(8) -- PnL/WR impact
2. Signal autocorrelation: score[T] vs score[T+k]
3. Signal persistence: how many bars does |score| > threshold last?
4. Decay by signal strength: strong signals (|s|>=5) vs weak (|s|~3)
5. Forward returns by score bucket: avg return 1,2,4,8,16 bars ahead
6. Paper trading signal age analysis (how stale are signals in practice?)

ผลลัพธ์จะบอกเราว่า:
- ระบบ sensitive กับ execution timing มากแค่ไหน
- ถ้า data lag 15-30 นาที alpha ยังเหลืออยู่ไหม
- strong signal ให้เวลาเข้ามากกว่า weak signal ไหม
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

# ─── Config ───
COINS = ["BTCUSDT", "XRPUSDT", "ADAUSDT", "DOTUSDT", "SUIUSDT", "FILUSDT"]
OOS_START = "2025-01-01"
OOS_END = "2026-03-31"
MAX_SHIFTS = 8  # test shift(1) through shift(8)


def build_btc_score():
    """Build full BTC composite score (v3)."""
    log.info("Loading BTC data...")
    btc_ohlcv = bt.fetch_binance_15m("BTCUSDT", years=3)
    if "date_time" in btc_ohlcv.columns:
        btc_ohlcv = btc_ohlcv.rename(columns={"date_time": "ts"})

    db_data = bt.load_btc_db_data()
    btc_df = bt.build_btc_features(btc_ohlcv, db_data)

    from paper_trading.strategy import score_ob_combined, score_basis_contrarian, score_tick_liq

    btc_score = bt.compute_btc_composite_score(btc_df, COMPOSITE_WEIGHTS)
    btc_score = btc_score + score_ob_combined(btc_df, weight=V3_EXTRA_WEIGHTS["ob_combined"])
    btc_score = btc_score + score_basis_contrarian(btc_df, weight=V3_EXTRA_WEIGHTS["basis_contrarian"])
    btc_score = btc_score + score_tick_liq(btc_df, weight=V3_EXTRA_WEIGHTS["tick_liq"])

    btc_score_ts = pd.Series(btc_score.values, index=btc_df["ts"].values, name="btc_score")
    return btc_score_ts, btc_df


def exp1_entry_delay_sweep(btc_score_ts):
    """Test shift(1) through shift(8) - how much PnL drops with delayed entry."""
    log.info("=== EXP1: Entry Delay Sweep ===")
    results = {}

    for shift_n in range(1, MAX_SHIFTS + 1):
        total_pnl = 0
        total_trades = 0
        total_wins = 0

        for symbol in COINS:
            coin = symbol.replace("USDT", "")
            ohlcv = bt.fetch_binance_15m(symbol, years=3)
            if "date_time" in ohlcv.columns:
                ohlcv = ohlcv.rename(columns={"date_time": "ts"})
            alt_df = bt.build_alt_technicals(ohlcv)
            oos_mask = (alt_df["ts"] >= OOS_START) & (alt_df["ts"] <= OOS_END)

            cfg = COIN_CONFIGS.get(coin, {})
            signals, alt_merged = bt.generate_btc_led_signal(
                btc_score_ts, alt_df[oos_mask],
                threshold=cfg.get("threshold", 3.0),
                use_alt_pa_filter=cfg.get("use_alt_pa_filter", False))

            # Custom backtest with variable shift
            sig = signals.shift(shift_n).fillna(0).astype(int).values
            atrs = alt_merged["atr"].values
            opens = alt_merged["open"].values
            highs = alt_merged["high"].values
            lows = alt_merged["low"].values
            closes = alt_merged["close"].values
            times = alt_merged["ts"].values
            n = len(alt_merged)

            sl_mult = cfg.get("sl_atr_mult", 2.5)
            tp_mult = cfg.get("tp_atr_mult", 4.0)
            cooldown = cfg.get("cooldown_bars", 4)
            trail_mult = 0.5
            trail_activate = 0.5
            max_hold = 96
            position = 0
            entry_i = entry_px = entry_atr = qty = fee_in = 0
            peak = trough = 0.0
            trl_active = False
            last_exit_i = -cooldown - 1
            coin_pnl = 0
            coin_trades = 0
            coin_wins = 0

            for i in range(n):
                if position == 0 and sig[i] != 0 and (i - last_exit_i) > cooldown:
                    raw_px = opens[i]
                    cur_atr = atrs[i] if not np.isnan(atrs[i]) else 0
                    if raw_px <= 0 or cur_atr <= 0 or cur_atr / raw_px < 0.0005:
                        continue
                    qty = (bt.BUDGET_USDT * bt.LEVERAGE) / raw_px
                    entry_px = raw_px * (1 + bt.SLIP) if sig[i] == 1 else raw_px * (1 - bt.SLIP)
                    entry_atr = cur_atr
                    fee_in = entry_px * qty * bt.FEE
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
                        sl_level = entry_px - sl_mult * atr
                        tp_level = entry_px + tp_mult * atr
                    else:
                        trough = min(trough, l)
                        sl_level = entry_px + sl_mult * atr
                        tp_level = entry_px - tp_mult * atr

                    trail_stop = None
                    if position == 1 and (peak - entry_px) >= trail_activate * atr:
                        trl_active = True
                        trail_stop = peak - trail_mult * atr
                    elif position == -1 and (entry_px - trough) >= trail_activate * atr:
                        trl_active = True
                        trail_stop = trough + trail_mult * atr

                    exit_px = exit_reason = None
                    if position == 1:
                        if l <= sl_level: exit_px = sl_level
                        elif trl_active and trail_stop and l <= trail_stop: exit_px = trail_stop
                        elif h >= tp_level: exit_px = tp_level
                    else:
                        if h >= sl_level: exit_px = sl_level
                        elif trl_active and trail_stop and h >= trail_stop: exit_px = trail_stop
                        elif l <= tp_level: exit_px = tp_level

                    if exit_px is None and (i - entry_i) >= max_hold:
                        exit_px = c
                    if exit_px is None and sig[i] != 0 and sig[i] != position:
                        exit_px = o

                    if exit_px is not None:
                        exit_px_f = exit_px * (1 - bt.SLIP) if position == 1 else exit_px * (1 + bt.SLIP)
                        fee_out = exit_px_f * qty * bt.FEE
                        pnl = (exit_px_f - entry_px) * qty * position - fee_in - fee_out
                        coin_pnl += pnl
                        coin_trades += 1
                        if pnl > 0:
                            coin_wins += 1
                        last_exit_i = i
                        position = 0

            total_pnl += coin_pnl
            total_trades += coin_trades
            total_wins += coin_wins

        wr = (total_wins / total_trades * 100) if total_trades > 0 else 0
        results[f"shift_{shift_n}"] = {
            "shift": shift_n,
            "delay_minutes": shift_n * 15,
            "total_pnl": round(total_pnl, 2),
            "trades": total_trades,
            "wr_pct": round(wr, 1),
            "avg_pnl": round(total_pnl / total_trades, 2) if total_trades > 0 else 0,
        }
        log.info(f"  shift({shift_n}): {total_trades} trades, WR {wr:.1f}%, PnL ${total_pnl:.0f}")

    return results


def exp2_signal_autocorrelation(btc_score_ts):
    """Measure score autocorrelation at lags 1-16 bars."""
    log.info("=== EXP2: Signal Autocorrelation ===")
    scores = btc_score_ts.values
    # Filter to OOS period
    idx = btc_score_ts.index
    mask = (idx >= np.datetime64(OOS_START)) & (idx <= np.datetime64(OOS_END))
    scores_oos = scores[mask]

    results = {}
    for lag in [1, 2, 3, 4, 5, 8, 12, 16, 24, 32]:
        if lag >= len(scores_oos):
            break
        s1 = scores_oos[:-lag]
        s2 = scores_oos[lag:]
        valid = ~(np.isnan(s1) | np.isnan(s2))
        if valid.sum() < 100:
            continue
        corr = np.corrcoef(s1[valid], s2[valid])[0, 1]
        results[f"lag_{lag}"] = {
            "lag_bars": lag,
            "lag_minutes": lag * 15,
            "autocorrelation": round(float(corr), 4),
            "n_obs": int(valid.sum()),
        }
        log.info(f"  lag {lag} ({lag*15}min): autocorr = {corr:.4f}")

    return results


def exp3_signal_persistence(btc_score_ts):
    """How many consecutive bars does |score| > threshold persist?"""
    log.info("=== EXP3: Signal Persistence ===")
    idx = btc_score_ts.index
    mask = (idx >= np.datetime64(OOS_START)) & (idx <= np.datetime64(OOS_END))
    scores_oos = btc_score_ts.values[mask]

    thresholds = [2.0, 3.0, 4.0, 5.0]
    results = {}

    for thr in thresholds:
        # Count streak lengths of |score| >= thr
        active = np.abs(scores_oos) >= thr
        streaks = []
        current_streak = 0
        for a in active:
            if a:
                current_streak += 1
            else:
                if current_streak > 0:
                    streaks.append(current_streak)
                current_streak = 0
        if current_streak > 0:
            streaks.append(current_streak)

        if len(streaks) == 0:
            results[f"thr_{thr}"] = {"threshold": thr, "n_streaks": 0}
            continue

        streaks_arr = np.array(streaks)
        results[f"thr_{thr}"] = {
            "threshold": thr,
            "n_streaks": len(streaks),
            "mean_bars": round(float(streaks_arr.mean()), 1),
            "median_bars": round(float(np.median(streaks_arr)), 1),
            "max_bars": int(streaks_arr.max()),
            "pct_1bar": round(float((streaks_arr == 1).mean() * 100), 1),
            "pct_3plus": round(float((streaks_arr >= 3).mean() * 100), 1),
            "pct_8plus": round(float((streaks_arr >= 8).mean() * 100), 1),
            "mean_duration_min": round(float(streaks_arr.mean() * 15), 0),
        }
        log.info(f"  thr {thr}: {len(streaks)} streaks, mean {streaks_arr.mean():.1f} bars ({streaks_arr.mean()*15:.0f}min), {(streaks_arr==1).mean()*100:.0f}% are 1-bar flickers")

    return results


def exp4_decay_by_strength(btc_score_ts, btc_df):
    """Compare alpha decay curves for strong vs weak signals."""
    log.info("=== EXP4: Decay by Signal Strength ===")
    idx = btc_score_ts.index
    mask = (idx >= np.datetime64(OOS_START)) & (idx <= np.datetime64(OOS_END))
    scores_oos = btc_score_ts.values[mask]

    # Get BTC returns for OOS period
    btc_oos = btc_df[btc_df["ts"].isin(btc_score_ts.index[mask])].copy()
    if len(btc_oos) == 0:
        # Alternative: align by position
        btc_oos = btc_df[(btc_df["ts"] >= OOS_START) & (btc_df["ts"] <= OOS_END)].copy()

    btc_oos = btc_oos.sort_values("ts").reset_index(drop=True)
    returns = btc_oos["close"].pct_change().values

    # Align scores with returns (use same length)
    min_len = min(len(scores_oos), len(returns))
    scores_oos = scores_oos[:min_len]
    returns = returns[:min_len]

    # Define signal buckets
    buckets = {
        "weak_long": (scores_oos >= 2.0) & (scores_oos < 4.0),
        "strong_long": (scores_oos >= 4.0) & (scores_oos < 6.0),
        "extreme_long": scores_oos >= 6.0,
        "weak_short": (scores_oos <= -2.0) & (scores_oos > -4.0),
        "strong_short": (scores_oos <= -4.0) & (scores_oos > -6.0),
        "extreme_short": scores_oos <= -6.0,
    }

    horizons = [1, 2, 4, 8, 16, 32]
    results = {}

    for name, bucket_mask in buckets.items():
        n_signals = int(bucket_mask.sum())
        if n_signals < 10:
            results[name] = {"n_signals": n_signals, "insufficient_data": True}
            continue

        decay_curve = {}
        for h in horizons:
            # Forward return h bars ahead
            fwd_ret = pd.Series(returns).shift(-h).values[:min_len]
            valid = bucket_mask & ~np.isnan(fwd_ret)
            if valid.sum() < 5:
                continue

            # For long signals, we want positive return; for short, negative
            is_short = "short" in name
            directional_ret = -fwd_ret[valid] if is_short else fwd_ret[valid]

            avg_ret_bps = float(directional_ret.mean() * 10000)
            hit_rate = float((directional_ret > 0).mean() * 100)

            decay_curve[f"h{h}"] = {
                "horizon_bars": h,
                "horizon_min": h * 15,
                "avg_return_bps": round(avg_ret_bps, 2),
                "hit_rate_pct": round(hit_rate, 1),
                "n_obs": int(valid.sum()),
            }

        results[name] = {
            "n_signals": n_signals,
            "decay_curve": decay_curve,
        }

        h1 = decay_curve.get("h1", {}).get("avg_return_bps", 0)
        h4 = decay_curve.get("h4", {}).get("avg_return_bps", 0)
        h16 = decay_curve.get("h16", {}).get("avg_return_bps", 0)
        log.info(f"  {name}: {n_signals} signals, h1={h1:.1f}bps, h4={h4:.1f}bps, h16={h16:.1f}bps")

    return results


def exp5_forward_returns_by_bucket(btc_score_ts, btc_df):
    """Average forward returns at different horizons by absolute score magnitude."""
    log.info("=== EXP5: Forward Returns by Score Bucket ===")
    idx = btc_score_ts.index
    mask = (idx >= np.datetime64(OOS_START)) & (idx <= np.datetime64(OOS_END))
    scores_oos = btc_score_ts.values[mask]

    btc_oos = btc_df[(btc_df["ts"] >= OOS_START) & (btc_df["ts"] <= OOS_END)].copy()
    btc_oos = btc_oos.sort_values("ts").reset_index(drop=True)
    returns = btc_oos["close"].pct_change().values

    min_len = min(len(scores_oos), len(returns))
    scores_oos = scores_oos[:min_len]
    returns = returns[:min_len]

    abs_scores = np.abs(scores_oos)
    # Buckets by absolute magnitude
    score_buckets = [
        ("0-1", (abs_scores >= 0) & (abs_scores < 1)),
        ("1-2", (abs_scores >= 1) & (abs_scores < 2)),
        ("2-3", (abs_scores >= 2) & (abs_scores < 3)),
        ("3-4", (abs_scores >= 3) & (abs_scores < 4)),
        ("4-5", (abs_scores >= 4) & (abs_scores < 5)),
        ("5+", abs_scores >= 5),
    ]

    horizons = [1, 2, 4, 8, 16]
    results = {}

    for bucket_name, bucket_mask in score_buckets:
        n = int(bucket_mask.sum())
        if n < 20:
            results[bucket_name] = {"n": n, "insufficient": True}
            continue

        row = {"n": n}
        for h in horizons:
            # Directional forward return (sign-aligned with score)
            signs = np.sign(scores_oos[bucket_mask])
            fwd_cum = np.zeros(int(bucket_mask.sum()))
            for j, idx_i in enumerate(np.where(bucket_mask)[0]):
                if idx_i + h < min_len:
                    # Cumulative return from idx_i+1 to idx_i+h
                    fwd_cum[j] = (btc_oos["close"].iloc[idx_i + h] / btc_oos["close"].iloc[idx_i] - 1) * signs[j]
                else:
                    fwd_cum[j] = np.nan

            valid = ~np.isnan(fwd_cum)
            if valid.sum() > 0:
                row[f"h{h}_ret_bps"] = round(float(np.nanmean(fwd_cum) * 10000), 2)
                row[f"h{h}_hr_pct"] = round(float((fwd_cum[valid] > 0).mean() * 100), 1)

        results[bucket_name] = row
        log.info(f"  |score| {bucket_name}: n={n}, h1={row.get('h1_ret_bps', '?')}bps, h4={row.get('h4_ret_bps', '?')}bps")

    return results


def exp6_paper_signal_age():
    """Analyze how stale signals are in paper trading."""
    log.info("=== EXP6: Paper Trading Signal Age ===")
    import sqlite3
    db_path = BASE_DIR / "paper_trading" / "state" / "paper_trades.db"
    if not db_path.exists():
        log.warning("No paper_trades.db found")
        return {"error": "no_db"}

    conn = sqlite3.connect(str(db_path))
    try:
        trades = pd.read_sql("SELECT * FROM trades ORDER BY entry_time", conn)
    except Exception as e:
        log.warning(f"Error reading trades: {e}")
        return {"error": str(e)}
    finally:
        conn.close()

    if len(trades) == 0:
        return {"error": "no_trades"}

    # Parse times
    trades["entry_time"] = pd.to_datetime(trades["entry_time"])
    if "exit_time" in trades.columns:
        trades["exit_time"] = pd.to_datetime(trades["exit_time"])

    # Signal age = time from candle close (15m aligned) to actual entry
    # Candle close is at :00, :15, :30, :45. Entry should be shortly after.
    trades["entry_minute"] = trades["entry_time"].dt.minute
    trades["entry_second"] = trades["entry_time"].dt.second

    # Minutes into the 15m bar (how late after candle close)
    trades["delay_minutes"] = trades["entry_minute"] % 15 + trades["entry_second"] / 60

    n_trades = len(trades)
    avg_delay = float(trades["delay_minutes"].mean())
    max_delay = float(trades["delay_minutes"].max())
    pct_within_2min = float((trades["delay_minutes"] <= 2).mean() * 100)
    pct_within_5min = float((trades["delay_minutes"] <= 5).mean() * 100)

    # Analyze by exit reason
    by_exit = {}
    if "exit_reason" in trades.columns:
        for reason in trades["exit_reason"].dropna().unique():
            subset = trades[trades["exit_reason"] == reason]
            by_exit[reason] = {
                "trades": len(subset),
                "avg_delay_min": round(float(subset["delay_minutes"].mean()), 1),
            }

    results = {
        "n_trades": n_trades,
        "avg_entry_delay_min": round(avg_delay, 1),
        "max_entry_delay_min": round(max_delay, 1),
        "pct_within_2min": round(pct_within_2min, 1),
        "pct_within_5min": round(pct_within_5min, 1),
        "delay_distribution": {
            "0-1min": int((trades["delay_minutes"] <= 1).sum()),
            "1-2min": int(((trades["delay_minutes"] > 1) & (trades["delay_minutes"] <= 2)).sum()),
            "2-5min": int(((trades["delay_minutes"] > 2) & (trades["delay_minutes"] <= 5)).sum()),
            "5-10min": int(((trades["delay_minutes"] > 5) & (trades["delay_minutes"] <= 10)).sum()),
            "10-15min": int((trades["delay_minutes"] > 10).sum()),
        },
        "by_exit_reason": by_exit,
    }
    log.info(f"  {n_trades} trades, avg delay {avg_delay:.1f}min, {pct_within_2min:.0f}% within 2min")
    return results


def exp7_optimal_hold_by_signal(btc_score_ts):
    """Test if strong signals should be held longer (wider TP/more patience)."""
    log.info("=== EXP7: Optimal Hold Period by Signal Strength ===")
    results = {}

    # Test two configs: aggressive (shift=1, default SL/TP) vs patient (shift=1, wider hold)
    configs = [
        {"name": "baseline", "max_hold": 96, "trail_mult": 0.5, "trail_activate": 0.5},
        {"name": "patient_trail", "max_hold": 96, "trail_mult": 1.0, "trail_activate": 1.0},
        {"name": "very_patient", "max_hold": 192, "trail_mult": 1.5, "trail_activate": 1.5},
    ]

    for cfg_test in configs:
        total_pnl = 0
        total_trades = 0
        total_wins = 0
        pnl_by_strength = {"weak": 0, "strong": 0, "extreme": 0}
        trades_by_strength = {"weak": 0, "strong": 0, "extreme": 0}

        for symbol in COINS:
            coin = symbol.replace("USDT", "")
            ohlcv = bt.fetch_binance_15m(symbol, years=3)
            if "date_time" in ohlcv.columns:
                ohlcv = ohlcv.rename(columns={"date_time": "ts"})
            alt_df = bt.build_alt_technicals(ohlcv)
            oos_mask = (alt_df["ts"] >= OOS_START) & (alt_df["ts"] <= OOS_END)

            coin_cfg = COIN_CONFIGS.get(coin, {})
            signals, alt_merged = bt.generate_btc_led_signal(
                btc_score_ts, alt_df[oos_mask],
                threshold=coin_cfg.get("threshold", 3.0),
                use_alt_pa_filter=coin_cfg.get("use_alt_pa_filter", False))

            trades = bt.run_backtest(
                alt_merged, signals,
                sl_atr_mult=coin_cfg.get("sl_atr_mult", 2.5),
                tp_atr_mult=coin_cfg.get("tp_atr_mult", 4.0),
                trail_atr_mult=cfg_test["trail_mult"],
                trail_activate_atr=cfg_test["trail_activate"],
                max_hold_bars=cfg_test["max_hold"],
                cooldown_bars=coin_cfg.get("cooldown_bars", 4))

            if len(trades) > 0:
                # Map entry to BTC score at entry time
                for _, t in trades.iterrows():
                    entry_ts = t["entry_time"]
                    # Find closest score
                    score_idx = btc_score_ts.index.searchsorted(entry_ts)
                    if score_idx > 0:
                        score_idx -= 1  # signal was shift(1), so score is 1 bar before
                    entry_score = abs(float(btc_score_ts.iloc[min(score_idx, len(btc_score_ts)-1)]))

                    if entry_score < 4:
                        bucket = "weak"
                    elif entry_score < 6:
                        bucket = "strong"
                    else:
                        bucket = "extreme"

                    pnl_by_strength[bucket] += t["pnl_net"]
                    trades_by_strength[bucket] += 1

                total_pnl += trades["pnl_net"].sum()
                total_trades += len(trades)
                total_wins += (trades["pnl_net"] > 0).sum()

        wr = (total_wins / total_trades * 100) if total_trades > 0 else 0
        results[cfg_test["name"]] = {
            "total_pnl": round(total_pnl, 2),
            "trades": total_trades,
            "wr_pct": round(wr, 1),
            "by_strength": {
                k: {
                    "trades": trades_by_strength[k],
                    "pnl": round(pnl_by_strength[k], 2),
                    "avg_pnl": round(pnl_by_strength[k] / max(trades_by_strength[k], 1), 2)
                }
                for k in ["weak", "strong", "extreme"]
            },
            "config": cfg_test,
        }
        log.info(f"  {cfg_test['name']}: {total_trades} trades, WR {wr:.1f}%, PnL ${total_pnl:.0f}")

    return results


def main():
    started_at = datetime.utcnow()
    log.info("=" * 60)
    log.info("Mission 011: Signal Alpha Decay & Entry Timing")
    log.info("=" * 60)

    # Build BTC score once
    btc_score_ts, btc_df = build_btc_score()

    # Run all experiments
    r1 = exp1_entry_delay_sweep(btc_score_ts)
    r2 = exp2_signal_autocorrelation(btc_score_ts)
    r3 = exp3_signal_persistence(btc_score_ts)
    r4 = exp4_decay_by_strength(btc_score_ts, btc_df)
    r5 = exp5_forward_returns_by_bucket(btc_score_ts, btc_df)
    r6 = exp6_paper_signal_age()
    r7 = exp7_optimal_hold_by_signal(btc_score_ts)

    finished_at = datetime.utcnow()

    # Compile results
    all_results = {
        "mission_id": "mission_011_signal_alpha_decay",
        "date": datetime.utcnow().strftime("%Y-%m-%d"),
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "exp1_entry_delay": r1,
        "exp2_autocorrelation": r2,
        "exp3_persistence": r3,
        "exp4_decay_by_strength": r4,
        "exp5_forward_returns": r5,
        "exp6_paper_signal_age": r6,
        "exp7_optimal_hold": r7,
    }

    # Save JSON
    out_json = BASE_DIR / "missions" / "mission_011_signal_alpha_decay.json"
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(all_results, f, indent=2, ensure_ascii=False, default=str)
    log.info(f"Saved: {out_json}")

    return all_results


if __name__ == "__main__":
    results = main()

    # Print summary
    print("\n" + "=" * 60)
    print("SUMMARY: Signal Alpha Decay")
    print("=" * 60)

    if "exp1_entry_delay" in results:
        print("\nEntry Delay Impact:")
        for k, v in results["exp1_entry_delay"].items():
            print(f"  {k}: ${v['total_pnl']:,.0f} ({v['trades']} trades, WR {v['wr_pct']}%)")

    if "exp3_persistence" in results:
        print("\nSignal Persistence (mean bars at threshold):")
        for k, v in results["exp3_persistence"].items():
            if "mean_bars" in v:
                print(f"  {k}: {v['mean_bars']} bars ({v['mean_duration_min']}min), {v['pct_1bar']}% flickers")
