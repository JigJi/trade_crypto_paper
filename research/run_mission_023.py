"""
Mission 023: Factor Conflict Analysis — Do Conflicting Factors Predict SIGNAL_FLIP?
====================================================================================
สมมติฐาน: เมื่อ 8 factors ใน v3 ขัดแย้งกัน (บางตัวบอก LONG บางตัวบอก SHORT)
composite score จะไม่เสถียร → โอกาส SIGNAL_FLIP สูงขึ้น

ถ้าจริง: สามารถสร้าง "consensus filter" ที่ไม่เข้าเทรดเมื่อ factor ขัดแย้งกัน

ต่อยอดจาก:
- Mission 021: entry features ไม่ทำนาย FLIP → แต่ยังไม่ได้ดู factor-level disagreement
- Mission 022: FLIP เป็น sudden event → อาจเพราะ 1 factor dominant กลับทิศทันที

Experiments:
1. Baseline: decompose v3 score เป็น 8 factors แยกกัน
2. Factor agreement analysis: entry FLIP vs non-FLIP
3. Factor dominance: factor ตัวไหน dominant ที่สุดเมื่อเกิด FLIP
4. Factor conflict during trade: FLIP trades มี conflict เพิ่มขึ้นระหว่างเทรดหรือไม่
5. Consensus filter test: กรองเทรดที่ factor conflict สูงออกไป
6. Single-factor FLIP: factor ตัวเดียวที่ flip ก่อน full FLIP
"""

import sys, json, logging
from pathlib import Path
from datetime import datetime

import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import backtest_15m_btc_led_alts as bt
from paper_trading.config import COIN_CONFIGS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════
# Individual Factor Scorers (decompose v3 composite)
# ══════════════════════════════════════════════════════════════

def score_oi_divergence(df, weight=0.5):
    """OI divergence: bullish accumulation vs bearish capitulation."""
    s = pd.Series(0.0, index=df.index)
    if "oi_chg" not in df.columns:
        return s
    oi_chg = df["oi_chg"].fillna(0)
    ret = df["ret"].fillna(0)
    w = weight / 2  # sub-weight 0.25 each
    s += np.where((ret > 0.001) & (oi_chg > 0.002), w, 0)
    s += np.where((ret < -0.001) & (oi_chg < -0.002), w, 0)
    s += np.where((ret > 0.001) & (oi_chg < -0.002), -w, 0)
    s += np.where((ret < -0.001) & (oi_chg > 0.002), -w, 0)
    return s


def score_funding_rate(df, weight=2.0):
    """Funding rate: contrarian — negative FR = bullish, high FR = bearish."""
    s = pd.Series(0.0, index=df.index)
    if "fr_8h" in df.columns:
        fr = df["fr_8h"].fillna(0)
        s += np.where(fr < -0.0001, weight, 0)
        s += np.where(fr > 0.0003, -weight, 0)
    elif "last_funding_rate" in df.columns:
        fr = df["last_funding_rate"].fillna(0)
        s += np.where(fr < -0.00005, weight, 0)
        s += np.where(fr > 0.0002, -weight, 0)
    return s


def score_whale_alerts(df, weight=1.5):
    """Whale alerts: net bullish whale activity."""
    s = pd.Series(0.0, index=df.index)
    if "whale_net_ma" not in df.columns:
        return s
    wn = df["whale_net_ma"].fillna(0)
    s += np.where(wn > 50_000_000, weight, 0)
    s += np.where(wn < -50_000_000, -weight, 0)
    return s


def score_liquidation_cascade(df, weight=2.0):
    """Liquidation cascade: contrarian — liq cascade direction."""
    s = pd.Series(0.0, index=df.index)
    if "liq_net" not in df.columns or "liq_total_ma" not in df.columns:
        return s
    lt = df["liq_total"].fillna(0)
    lt_ma = df["liq_total_ma"].fillna(1)
    ln = df["liq_net"].fillna(0)
    cascade = lt > (lt_ma * 3)
    s += np.where(cascade & (ln > 0), weight, 0)
    s += np.where(cascade & (ln < 0), -weight, 0)
    return s


def score_etf_flows(df, weight=1.0):
    """ETF flows: positive flows = bullish."""
    s = pd.Series(0.0, index=df.index)
    if "etf_flow_ma" not in df.columns:
        return s
    etf = df["etf_flow_ma"].fillna(0)
    s += np.where(etf > 50, weight, 0)
    s += np.where(etf < -50, -weight, 0)
    return s


def score_basis_contrarian(df, weight=1.5):
    """Basis contrarian: high basis = bearish."""
    s = pd.Series(0.0, index=df.index)
    if "basis_z" not in df.columns:
        return s
    bz = df["basis_z"].fillna(0)
    s += np.where(bz > 1.5, -weight, 0)
    s += np.where(bz > 2.5, -weight * 0.5, 0)
    s += np.where(bz < -1.5, weight, 0)
    s += np.where(bz < -2.5, weight * 0.5, 0)
    return s


def score_tick_liq(df, weight=2.0):
    """Tick liquidation: net short liqs = bullish."""
    s = pd.Series(0.0, index=df.index)
    if "liq_net_ma" not in df.columns:
        return s
    ln = df["liq_net_ma"].fillna(0)
    lt = df["liq_notional_ma"].fillna(0)
    s += np.where(ln > 2, weight, 0)
    s += np.where(ln < -2, -weight, 0)
    lt_em = lt.where(lt > 0).expanding().mean().fillna(1)
    s += np.where(lt > lt_em * 3, weight * 0.5, 0)
    return s


def score_ob_combined(df, weight=2.0):
    """Order book combined: contrarian on imbalance."""
    s = pd.Series(0.0, index=df.index)
    if "ob_imb_ma" not in df.columns:
        return s
    combo = (df["ob_imb_ma"].fillna(0) + df["ob_vol_imb_ma"].fillna(0)) / 2
    s += np.where(combo > 0.03, -weight, 0)
    s += np.where(combo > 0.07, -weight * 0.5, 0)
    s += np.where(combo < -0.03, weight, 0)
    s += np.where(combo < -0.07, weight * 0.5, 0)
    return s


FACTOR_SCORERS = {
    "oi_divergence": score_oi_divergence,
    "funding_rate": score_funding_rate,
    "whale_alerts": score_whale_alerts,
    "liq_cascade": score_liquidation_cascade,
    "etf_flows": score_etf_flows,
    "basis_contrarian": score_basis_contrarian,
    "tick_liq": score_tick_liq,
    "ob_combined": score_ob_combined,
}


def compute_factor_decomposition(btc_df):
    """Compute each factor individually and return DataFrame with ts + 8 factor columns."""
    result = btc_df[["ts"]].copy()
    for name, scorer in FACTOR_SCORERS.items():
        result[name] = scorer(btc_df).values
    result["composite"] = result[list(FACTOR_SCORERS.keys())].sum(axis=1)
    return result


def compute_factor_metrics(factor_row):
    """For a single bar, compute agreement/conflict metrics across factors."""
    factors = {k: v for k, v in factor_row.items() if k in FACTOR_SCORERS}
    active = {k: v for k, v in factors.items() if v != 0}
    n_active = len(active)
    if n_active == 0:
        return {"n_active": 0, "n_bullish": 0, "n_bearish": 0,
                "agreement_ratio": 1.0, "dominant_factor": None, "max_contribution": 0}

    n_bull = sum(1 for v in active.values() if v > 0)
    n_bear = sum(1 for v in active.values() if v < 0)
    majority = max(n_bull, n_bear)
    agreement = majority / n_active if n_active > 0 else 1.0
    dominant = max(active, key=lambda k: abs(active[k]))
    return {
        "n_active": n_active,
        "n_bullish": n_bull,
        "n_bearish": n_bear,
        "agreement_ratio": agreement,
        "dominant_factor": dominant,
        "max_contribution": abs(active[dominant]),
    }


def main():
    started_at = datetime.utcnow()
    results = {}

    # ── Load data ──
    log.info("Loading BTC data...")
    btc_ohlcv = bt.fetch_binance_15m("BTCUSDT", years=3)
    if "date_time" in btc_ohlcv.columns:
        btc_ohlcv = btc_ohlcv.rename(columns={"date_time": "ts"})
    db_data = bt.load_btc_db_data()
    btc_df = bt.build_btc_features(btc_ohlcv, db_data)

    # ── Compute factor decomposition ──
    log.info("Computing factor decomposition...")
    factors_df = compute_factor_decomposition(btc_df)

    # ── Compute v3 composite score ──
    from signal_core import compute_btc_composite_score, DEFAULT_COMPOSITE_WEIGHTS, DEFAULT_EXTRA_WEIGHTS
    btc_score = compute_btc_composite_score(btc_df, DEFAULT_COMPOSITE_WEIGHTS, DEFAULT_EXTRA_WEIGHTS)
    btc_score_ts = pd.Series(btc_score.values, index=btc_df["ts"].values, name="btc_score")

    # Verify decomposition matches composite
    diff = (factors_df["composite"] - btc_score.values).abs()
    log.info(f"Decomposition verify: max diff = {diff.max():.6f}, mean = {diff.mean():.6f}")
    results["decomposition_max_diff"] = float(diff.max())

    # ── EXP 1: Factor Activity Baseline ──
    log.info("=" * 60)
    log.info("EXP 1: Factor Activity Baseline")
    oos_mask = (btc_df["ts"] >= "2025-01-01") & (btc_df["ts"] <= "2026-03-31")
    factors_oos = factors_df[oos_mask.values].copy()
    log.info(f"OOS bars: {len(factors_oos)}")

    factor_stats = {}
    for name in FACTOR_SCORERS:
        vals = factors_oos[name]
        active_pct = (vals != 0).mean() * 100
        pos_pct = (vals > 0).mean() * 100
        neg_pct = (vals < 0).mean() * 100
        factor_stats[name] = {
            "active_pct": round(active_pct, 1),
            "positive_pct": round(pos_pct, 1),
            "negative_pct": round(neg_pct, 1),
            "mean": round(vals.mean(), 4),
            "std": round(vals.std(), 4),
            "max": round(vals.max(), 4),
            "min": round(vals.min(), 4),
        }
        log.info(f"  {name}: active {active_pct:.1f}%, bull {pos_pct:.1f}%, bear {neg_pct:.1f}%, mean={vals.mean():.4f}")
    results["exp1_factor_stats"] = factor_stats

    # ── EXP 2: Run backtest and tag FLIP trades ──
    log.info("=" * 60)
    log.info("EXP 2: Backtest with Factor Decomposition at Entry")
    coins = ["BTCUSDT", "XRPUSDT", "ADAUSDT", "DOTUSDT", "SUIUSDT", "FILUSDT"]
    oos_start, oos_end = "2025-01-01", "2026-03-31"

    all_trades = []
    for symbol in coins:
        coin = symbol.replace("USDT", "")
        ohlcv = bt.fetch_binance_15m(symbol, years=3)
        if "date_time" in ohlcv.columns:
            ohlcv = ohlcv.rename(columns={"date_time": "ts"})
        alt_df = bt.build_alt_technicals(ohlcv)
        oos_m = (alt_df["ts"] >= oos_start) & (alt_df["ts"] <= oos_end)

        cfg = COIN_CONFIGS.get(coin, {})
        signals, alt_merged = bt.generate_btc_led_signal(
            btc_score_ts, alt_df[oos_m],
            threshold=cfg.get("threshold", 3.0),
            use_alt_pa_filter=cfg.get("use_alt_pa_filter", False))
        trades = bt.run_backtest(
            alt_merged, signals,
            sl_atr_mult=cfg.get("sl_atr_mult", 2.5),
            tp_atr_mult=cfg.get("tp_atr_mult", 4.0),
            cooldown_bars=cfg.get("cooldown_bars", 4))
        if len(trades) > 0:
            trades["coin"] = coin
            all_trades.append(trades)

    trades_df = pd.concat(all_trades, ignore_index=True)
    trades_df["is_flip"] = trades_df["exit_reason"] == "SIGNAL_FLIP"
    log.info(f"Total trades: {len(trades_df)}, FLIP: {trades_df['is_flip'].sum()}")

    # Attach factor values at entry time for each trade
    factors_ts = factors_df.set_index("ts")
    factor_names = list(FACTOR_SCORERS.keys())

    entry_factor_data = []
    for _, trade in trades_df.iterrows():
        entry_t = pd.Timestamp(trade["entry_time"])
        # Find nearest factor bar (backward)
        idx = factors_ts.index.get_indexer([entry_t], method="ffill")[0]
        if idx >= 0:
            row = factors_ts.iloc[idx]
            d = {f"entry_{k}": row[k] for k in factor_names}
            d["entry_composite"] = row["composite"]
            entry_factor_data.append(d)
        else:
            entry_factor_data.append({f"entry_{k}": 0 for k in factor_names})

    entry_factors = pd.DataFrame(entry_factor_data)
    trades_df = pd.concat([trades_df.reset_index(drop=True), entry_factors], axis=1)

    # Compute agreement metrics at entry
    def get_agreement(row):
        active = {}
        for k in factor_names:
            v = row.get(f"entry_{k}", 0)
            if v != 0:
                active[k] = v
        n = len(active)
        if n == 0:
            return 1.0, 0, 0, 0, None, 0
        n_bull = sum(1 for v in active.values() if v > 0)
        n_bear = sum(1 for v in active.values() if v < 0)
        majority = max(n_bull, n_bear)
        agreement = majority / n
        dominant = max(active, key=lambda x: abs(active[x])) if active else None
        max_c = abs(active[dominant]) if dominant else 0
        return agreement, n, n_bull, n_bear, dominant, max_c

    agreement_data = trades_df.apply(get_agreement, axis=1)
    trades_df["agreement_ratio"] = [a[0] for a in agreement_data]
    trades_df["n_active"] = [a[1] for a in agreement_data]
    trades_df["n_bullish"] = [a[2] for a in agreement_data]
    trades_df["n_bearish"] = [a[3] for a in agreement_data]
    trades_df["dominant_factor"] = [a[4] for a in agreement_data]
    trades_df["max_contribution"] = [a[5] for a in agreement_data]

    # ── EXP 2 Results: FLIP vs Non-FLIP agreement ──
    flip_trades = trades_df[trades_df["is_flip"]]
    non_flip_trades = trades_df[~trades_df["is_flip"]]

    exp2 = {
        "total_trades": len(trades_df),
        "flip_trades": len(flip_trades),
        "non_flip_trades": len(non_flip_trades),
        "flip_pnl": round(flip_trades["pnl_net"].sum(), 2),
        "non_flip_pnl": round(non_flip_trades["pnl_net"].sum(), 2),
        "flip_agreement": {
            "mean": round(flip_trades["agreement_ratio"].mean(), 4) if len(flip_trades) > 0 else None,
            "median": round(flip_trades["agreement_ratio"].median(), 4) if len(flip_trades) > 0 else None,
            "n_active_mean": round(flip_trades["n_active"].mean(), 2) if len(flip_trades) > 0 else None,
        },
        "non_flip_agreement": {
            "mean": round(non_flip_trades["agreement_ratio"].mean(), 4),
            "median": round(non_flip_trades["agreement_ratio"].median(), 4),
            "n_active_mean": round(non_flip_trades["n_active"].mean(), 2),
        },
    }
    log.info(f"FLIP agreement: mean={exp2['flip_agreement']['mean']}, "
             f"n_active={exp2['flip_agreement']['n_active_mean']}")
    log.info(f"Non-FLIP agreement: mean={exp2['non_flip_agreement']['mean']}, "
             f"n_active={exp2['non_flip_agreement']['n_active_mean']}")
    results["exp2_flip_vs_nonflip"] = exp2

    # ── EXP 3: Factor Dominance at FLIP ──
    log.info("=" * 60)
    log.info("EXP 3: Factor Dominance at FLIP")

    # Which factor is dominant at entry for FLIP vs non-FLIP?
    flip_dominant = flip_trades["dominant_factor"].value_counts().to_dict() if len(flip_trades) > 0 else {}
    non_flip_dominant = non_flip_trades["dominant_factor"].value_counts().to_dict()

    # Factor contribution mean at entry for FLIP vs non-FLIP
    factor_at_entry = {}
    for k in factor_names:
        col = f"entry_{k}"
        flip_mean = flip_trades[col].mean() if len(flip_trades) > 0 else 0
        nf_mean = non_flip_trades[col].mean()
        flip_abs_mean = flip_trades[col].abs().mean() if len(flip_trades) > 0 else 0
        nf_abs_mean = non_flip_trades[col].abs().mean()
        factor_at_entry[k] = {
            "flip_mean": round(float(flip_mean), 4),
            "nonflip_mean": round(float(nf_mean), 4),
            "flip_abs_mean": round(float(flip_abs_mean), 4),
            "nonflip_abs_mean": round(float(nf_abs_mean), 4),
        }
        log.info(f"  {k}: FLIP abs={flip_abs_mean:.4f}, non-FLIP abs={nf_abs_mean:.4f}")

    results["exp3_factor_dominance"] = {
        "flip_dominant_counts": {str(k): int(v) for k, v in flip_dominant.items()},
        "nonflip_dominant_counts": {str(k): int(v) for k, v in non_flip_dominant.items()},
        "factor_at_entry": factor_at_entry,
    }

    # ── EXP 4: Agreement by Quartile ──
    log.info("=" * 60)
    log.info("EXP 4: Agreement Ratio Quartile Performance")

    # Bin trades by agreement ratio
    try:
        trades_df["agreement_q"] = pd.qcut(trades_df["agreement_ratio"], q=4, labels=["Q1_low", "Q2", "Q3", "Q4_high"], duplicates="drop")
    except ValueError:
        # If too few unique values for qcut, use cut instead
        trades_df["agreement_q"] = pd.cut(trades_df["agreement_ratio"],
                                           bins=[0, 0.5, 0.67, 0.8, 1.01],
                                           labels=["low_0_50", "med_50_67", "high_67_80", "very_high_80_100"])

    quartile_stats = {}
    for q in trades_df["agreement_q"].dropna().unique():
        q_trades = trades_df[trades_df["agreement_q"] == q]
        wins = q_trades["pnl_net"] > 0
        q_name = str(q)
        quartile_stats[q_name] = {
            "count": len(q_trades),
            "wr": round(wins.mean() * 100, 1),
            "pnl": round(q_trades["pnl_net"].sum(), 2),
            "avg_pnl": round(q_trades["pnl_net"].mean(), 4),
            "flip_pct": round(q_trades["is_flip"].mean() * 100, 1),
            "avg_agreement": round(q_trades["agreement_ratio"].mean(), 3),
        }
        log.info(f"  {q_name}: {len(q_trades)} trades, WR {quartile_stats[q_name]['wr']}%, "
                 f"PnL ${quartile_stats[q_name]['pnl']}, FLIP {quartile_stats[q_name]['flip_pct']}%")
    results["exp4_agreement_quartiles"] = quartile_stats

    # ── EXP 5: Factor Conflict During Trade (trajectory) ──
    log.info("=" * 60)
    log.info("EXP 5: Factor Conflict During Trade")

    # Sample up to 500 FLIP and 500 non-FLIP trades for trajectory analysis
    n_sample = min(500, len(flip_trades), len(non_flip_trades))
    if n_sample > 0:
        flip_sample = flip_trades.sample(n=min(n_sample, len(flip_trades)), random_state=42)
        nf_sample = non_flip_trades.sample(n=min(n_sample, len(non_flip_trades)), random_state=42)

        def analyze_trajectory(trade_row):
            """Compute factor agreement trajectory during a trade."""
            entry_t = pd.Timestamp(trade_row["entry_time"])
            exit_t = pd.Timestamp(trade_row["exit_time"])
            mask = (factors_df["ts"] >= entry_t) & (factors_df["ts"] <= exit_t)
            traj = factors_df[mask]
            if len(traj) < 2:
                return None

            agreements = []
            n_flipping_factors = 0
            prev_signs = None
            for _, row in traj.iterrows():
                active = {k: row[k] for k in factor_names if row[k] != 0}
                n = len(active)
                if n == 0:
                    agreements.append(1.0)
                    continue
                n_bull = sum(1 for v in active.values() if v > 0)
                n_bear = sum(1 for v in active.values() if v < 0)
                agreements.append(max(n_bull, n_bear) / n)

                # Count factors that change sign
                cur_signs = {k: np.sign(row[k]) for k in factor_names}
                if prev_signs is not None:
                    for k in factor_names:
                        if prev_signs[k] != 0 and cur_signs[k] != 0 and prev_signs[k] != cur_signs[k]:
                            n_flipping_factors += 1
                prev_signs = cur_signs

            return {
                "bars": len(traj),
                "agreement_start": agreements[0] if agreements else None,
                "agreement_end": agreements[-1] if agreements else None,
                "agreement_min": min(agreements) if agreements else None,
                "agreement_std": float(np.std(agreements)) if len(agreements) > 1 else 0,
                "n_factor_flips": n_flipping_factors,
            }

        flip_trajectories = flip_sample.apply(analyze_trajectory, axis=1).dropna()
        nf_trajectories = nf_sample.apply(analyze_trajectory, axis=1).dropna()

        flip_traj_df = pd.DataFrame(flip_trajectories.tolist())
        nf_traj_df = pd.DataFrame(nf_trajectories.tolist())

        exp5 = {
            "flip_trajectory": {
                "count": len(flip_traj_df),
                "agreement_start_mean": round(flip_traj_df["agreement_start"].mean(), 4) if len(flip_traj_df) > 0 else None,
                "agreement_end_mean": round(flip_traj_df["agreement_end"].mean(), 4) if len(flip_traj_df) > 0 else None,
                "agreement_min_mean": round(flip_traj_df["agreement_min"].mean(), 4) if len(flip_traj_df) > 0 else None,
                "agreement_std_mean": round(flip_traj_df["agreement_std"].mean(), 4) if len(flip_traj_df) > 0 else None,
                "factor_flips_mean": round(flip_traj_df["n_factor_flips"].mean(), 2) if len(flip_traj_df) > 0 else None,
                "bars_mean": round(flip_traj_df["bars"].mean(), 1) if len(flip_traj_df) > 0 else None,
            },
            "nonflip_trajectory": {
                "count": len(nf_traj_df),
                "agreement_start_mean": round(nf_traj_df["agreement_start"].mean(), 4) if len(nf_traj_df) > 0 else None,
                "agreement_end_mean": round(nf_traj_df["agreement_end"].mean(), 4) if len(nf_traj_df) > 0 else None,
                "agreement_min_mean": round(nf_traj_df["agreement_min"].mean(), 4) if len(nf_traj_df) > 0 else None,
                "agreement_std_mean": round(nf_traj_df["agreement_std"].mean(), 4) if len(nf_traj_df) > 0 else None,
                "factor_flips_mean": round(nf_traj_df["n_factor_flips"].mean(), 2) if len(nf_traj_df) > 0 else None,
                "bars_mean": round(nf_traj_df["bars"].mean(), 1) if len(nf_traj_df) > 0 else None,
            },
        }
        log.info(f"FLIP traj: agreement_start={exp5['flip_trajectory']['agreement_start_mean']}, "
                 f"end={exp5['flip_trajectory']['agreement_end_mean']}, "
                 f"factor_flips={exp5['flip_trajectory']['factor_flips_mean']}")
        log.info(f"Non-FLIP traj: agreement_start={exp5['nonflip_trajectory']['agreement_start_mean']}, "
                 f"end={exp5['nonflip_trajectory']['agreement_end_mean']}, "
                 f"factor_flips={exp5['nonflip_trajectory']['factor_flips_mean']}")
    else:
        exp5 = {"note": "insufficient FLIP trades for trajectory analysis"}

    results["exp5_trajectory"] = exp5

    # ── EXP 6: Consensus Filter Test ──
    log.info("=" * 60)
    log.info("EXP 6: Consensus Filter — Skip Low-Agreement Entries")

    baseline_pnl = trades_df["pnl_net"].sum()
    baseline_wr = (trades_df["pnl_net"] > 0).mean() * 100
    baseline_trades = len(trades_df)
    baseline_flip_pnl = flip_trades["pnl_net"].sum()

    filter_results = {}
    for min_agreement in [0.5, 0.6, 0.7, 0.75, 0.8, 1.0]:
        filtered = trades_df[trades_df["agreement_ratio"] >= min_agreement]
        if len(filtered) == 0:
            continue
        f_wr = (filtered["pnl_net"] > 0).mean() * 100
        f_pnl = filtered["pnl_net"].sum()
        f_flips = filtered[filtered["is_flip"]]["pnl_net"].sum()
        f_n_flips = filtered["is_flip"].sum()
        key = f"min_{min_agreement}"
        filter_results[key] = {
            "trades": len(filtered),
            "trades_pct": round(len(filtered) / baseline_trades * 100, 1),
            "wr": round(f_wr, 1),
            "pnl": round(f_pnl, 2),
            "pnl_delta": round(f_pnl - baseline_pnl, 2),
            "flip_count": int(f_n_flips),
            "flip_pnl": round(f_flips, 2),
        }
        log.info(f"  min_agreement={min_agreement}: {len(filtered)} trades ({filter_results[key]['trades_pct']}%), "
                 f"WR {f_wr:.1f}%, PnL ${f_pnl:.0f} (delta ${f_pnl - baseline_pnl:.0f}), "
                 f"FLIPs {f_n_flips}")

    results["exp6_consensus_filter"] = {
        "baseline": {
            "trades": baseline_trades,
            "wr": round(baseline_wr, 1),
            "pnl": round(baseline_pnl, 2),
            "flip_pnl": round(baseline_flip_pnl, 2),
        },
        "filters": filter_results,
    }

    # ── EXP 7: Single-Factor Flip Analysis ──
    log.info("=" * 60)
    log.info("EXP 7: Which Factor Flips First Before SIGNAL_FLIP?")

    if len(flip_trades) > 0:
        first_flipper_counts = {}
        for _, trade in flip_trades.iterrows():
            entry_t = pd.Timestamp(trade["entry_time"])
            exit_t = pd.Timestamp(trade["exit_time"])
            direction = 1 if trade["dir"] == "L" else -1

            mask = (factors_df["ts"] >= entry_t) & (factors_df["ts"] <= exit_t)
            traj = factors_df[mask]
            if len(traj) < 2:
                continue

            # Entry factor signs (aligned with trade direction)
            entry_row = traj.iloc[0]
            for k in factor_names:
                entry_val = entry_row[k]
                if entry_val == 0:
                    continue
                # Check if this factor flips against trade direction during trade
                for j in range(1, len(traj)):
                    cur_val = traj.iloc[j][k]
                    if direction == 1 and cur_val < 0 and entry_val > 0:
                        first_flipper_counts[k] = first_flipper_counts.get(k, 0) + 1
                        break
                    elif direction == -1 and cur_val > 0 and entry_val < 0:
                        first_flipper_counts[k] = first_flipper_counts.get(k, 0) + 1
                        break

        # Sort by frequency
        sorted_flippers = sorted(first_flipper_counts.items(), key=lambda x: -x[1])
        log.info("Factor that flips first (against trade direction):")
        for k, v in sorted_flippers:
            pct = v / len(flip_trades) * 100
            log.info(f"  {k}: {v} times ({pct:.1f}%)")

        results["exp7_first_flipper"] = {k: int(v) for k, v in sorted_flippers}
    else:
        results["exp7_first_flipper"] = {}

    # ══════════════════════════════════════════════════════════════
    # Save Results
    # ══════════════════════════════════════════════════════════════
    finished_at = datetime.utcnow()

    # JSON
    json_path = BASE_DIR / "missions" / "mission_023_factor_conflict.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False, default=str)
    log.info(f"Saved JSON: {json_path}")

    # Update missions.json
    from research.missions import MissionEngine, _get_level

    engine = MissionEngine()
    mission_entry = {
        "mission_id": "mission_023_factor_conflict",
        "date": datetime.utcnow().strftime("%Y-%m-%d"),
        "type": "factor_analysis",
        "title": "Factor Conflict Analysis — Do Conflicting Factors Predict SIGNAL_FLIP?",
        "description": "วิเคราะห์ว่า factor 8 ตัวใน v3 ขัดแย้งกันตอน entry ทำให้เกิด SIGNAL_FLIP หรือไม่ ต่อยอดจาก M021 (entry features ไม่ช่วย) และ M022 (FLIP = sudden event)",
        "difficulty": "hard",
        "xp_reward": 100,
        "status": "completed",
        "target": "factor_conflict_flip",
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "result": results,
        "insight": "TBD — will be updated after analysis",
        "tags": ["factor_analysis", "signal_flip", "factor_interaction", "consensus", "decomposition"],
    }
    engine._data["missions"].append(mission_entry)
    engine._data["meta"]["total_xp"] += 100
    engine._data["meta"]["current_streak"] += 1
    engine._data["meta"]["longest_streak"] = max(
        engine._data["meta"]["longest_streak"],
        engine._data["meta"]["current_streak"])
    engine._data["meta"]["last_mission_date"] = mission_entry["date"]
    lvl, _ = _get_level(engine._data["meta"]["total_xp"])
    engine._data["meta"]["level"] = lvl
    engine._save()
    log.info(f"Updated missions.json: XP +100, streak {engine._data['meta']['current_streak']}")

    log.info("=" * 60)
    log.info("Mission 023 COMPLETE")
    return results


if __name__ == "__main__":
    results = main()
