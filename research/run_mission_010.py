"""
Mission 010: Factor Contribution Attribution
=============================================
Which factors drive winning vs losing trades?
Decompose the BTC composite score into 8 individual factor scores
and analyze their profiles at trade entry for winners vs losers.

Experiments:
  EXP1: Mean factor scores (winners vs losers, per factor)
  EXP2: Factor agreement -- how often does each factor align with trade direction?
  EXP3: Dominant factor analysis -- which factor contributes most to entry?
  EXP4: Factor pair interaction -- do certain combos predict W/L?
  EXP5: Score composition -- % of total score from each factor (W vs L)
  EXP6: v5 comparison -- does v5 weighting change the attribution picture?
"""

import sys, json, logging
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta
from itertools import combinations

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import backtest_15m_btc_led_alts as bt
from paper_trading.config import COMPOSITE_WEIGHTS, V3_EXTRA_WEIGHTS, COIN_CONFIGS

# ---- Factor decomposition ----

def compute_individual_factor_scores(df, params=None, extra=None):
    """
    Compute each factor's contribution separately.
    Returns dict of {factor_name: pd.Series}.
    """
    if params is None:
        params = COMPOSITE_WEIGHTS
    if extra is None:
        extra = V3_EXTRA_WEIGHTS

    scores = {}

    # 1. OI divergence
    s_oi = pd.Series(0.0, index=df.index)
    if "oi_chg" in df.columns:
        oi_chg = df["oi_chg"].fillna(0)
        ret = df["ret"].fillna(0)
        s_oi += np.where((ret > 0.001) & (oi_chg > 0.002), params.get("w_oi_bull", 0.25), 0)
        s_oi += np.where((ret < -0.001) & (oi_chg < -0.002), params.get("w_oi_capit", 0.25), 0)
        s_oi += np.where((ret > 0.001) & (oi_chg < -0.002), -params.get("w_oi_weak", 0.25), 0)
        s_oi += np.where((ret < -0.001) & (oi_chg > 0.002), -params.get("w_oi_bear", 0.25), 0)
    scores["oi_divergence"] = s_oi

    # 2. Funding rate
    s_fr = pd.Series(0.0, index=df.index)
    if "fr_8h" in df.columns:
        fr = df["fr_8h"].fillna(0)
        s_fr += np.where(fr < -0.0001, params.get("w_fr_neg", 2.0), 0)
        s_fr += np.where(fr > 0.0003, -params.get("w_fr_pos", 2.0), 0)
    elif "last_funding_rate" in df.columns:
        fr = df["last_funding_rate"].fillna(0)
        s_fr += np.where(fr < -0.00005, params.get("w_fr_neg", 2.0), 0)
        s_fr += np.where(fr > 0.0002, -params.get("w_fr_pos", 2.0), 0)
    scores["funding_rate"] = s_fr

    # 3. Whale alerts
    s_wh = pd.Series(0.0, index=df.index)
    if "whale_net_ma" in df.columns:
        wn_ma = df["whale_net_ma"].fillna(0)
        s_wh += np.where(wn_ma > 50_000_000, params.get("w_whale_bull", 1.5), 0)
        s_wh += np.where(wn_ma < -50_000_000, -params.get("w_whale_bear", 1.5), 0)
    scores["whale_alerts"] = s_wh

    # 4. Liquidation cascades
    s_liq = pd.Series(0.0, index=df.index)
    if "liq_net" in df.columns and "liq_total_ma" in df.columns:
        lt = df["liq_total"].fillna(0)
        lt_ma = df["liq_total_ma"].fillna(1)
        ln = df["liq_net"].fillna(0)
        cascade = lt > (lt_ma * 3)
        s_liq += np.where(cascade & (ln > 0), params.get("w_liq_bull", 2.0), 0)
        s_liq += np.where(cascade & (ln < 0), -params.get("w_liq_bear", 2.0), 0)
    scores["liquidation"] = s_liq

    # 5. ETF flows
    s_etf = pd.Series(0.0, index=df.index)
    if "etf_flow_ma" in df.columns:
        etf_ma = df["etf_flow_ma"].fillna(0)
        s_etf += np.where(etf_ma > 50, params.get("w_etf_bull", 1.0), 0)
        s_etf += np.where(etf_ma < -50, -params.get("w_etf_bear", 1.0), 0)
    scores["etf_flows"] = s_etf

    # 6. Basis contrarian
    scores["basis_contrarian"] = bt.score_basis_contrarian(df, weight=extra.get("basis_contrarian", 1.5))

    # 7. Tick liquidation
    scores["tick_liq"] = bt.score_tick_liq(df, weight=extra.get("tick_liq", 2.0))

    # 8. Order book combined
    scores["ob_combined"] = bt.score_ob_combined(df, weight=extra.get("ob_combined", 2.0))

    return scores


def main():
    log.info("=== Mission 010: Factor Contribution Attribution ===")
    started_at = datetime.utcnow()

    # ---- Load BTC data and compute scores ----
    log.info("Loading BTC OHLCV...")
    btc_ohlcv = bt.fetch_binance_15m("BTCUSDT", years=3)
    if "date_time" in btc_ohlcv.columns:
        btc_ohlcv = btc_ohlcv.rename(columns={"date_time": "ts"})

    log.info("Loading DB data and building features...")
    db_data = bt.load_btc_db_data()
    btc_df = bt.build_btc_features(btc_ohlcv, db_data)

    # Compute individual factor scores (v3 weights)
    log.info("Computing individual factor scores (v3)...")
    factor_scores_v3 = compute_individual_factor_scores(btc_df)

    # Also compute total composite score
    btc_score = bt.compute_btc_composite_score(btc_df, COMPOSITE_WEIGHTS)
    btc_score_ts = pd.Series(btc_score.values, index=btc_df["ts"].values, name="btc_score")

    # Build factor scores DataFrame aligned to timestamps
    factor_df = pd.DataFrame({k: v.values for k, v in factor_scores_v3.items()},
                              index=btc_df["ts"].values)
    factor_df["total"] = btc_score.values

    FACTOR_NAMES = list(factor_scores_v3.keys())
    log.info(f"Factors: {FACTOR_NAMES}")

    # ---- v5 weights for comparison ----
    V5_WEIGHTS = {
        "w_oi_bull": 0.25, "w_oi_capit": 0.25, "w_oi_weak": 0.25, "w_oi_bear": 0.25,
        "w_fr_neg": 2.0, "w_fr_pos": 2.0,
        "w_whale_bull": 1.5, "w_whale_bear": 1.5,
        "w_liq_bull": 5.0, "w_liq_bear": 5.0,
        "w_etf_bull": 1.0, "w_etf_bear": 1.0,
    }
    V5_EXTRA = {
        "ob_combined": 2.0,
        "basis_contrarian": 1.5,
        "tick_liq": 3.0,
    }
    factor_scores_v5 = compute_individual_factor_scores(btc_df, V5_WEIGHTS, V5_EXTRA)
    factor_df_v5 = pd.DataFrame({k: v.values for k, v in factor_scores_v5.items()},
                                 index=btc_df["ts"].values)
    btc_score_v5_arr = sum(s.values for s in factor_scores_v5.values())
    factor_df_v5["total"] = btc_score_v5_arr

    # ---- Run backtest on 6 core coins ----
    coins = ["BTCUSDT", "XRPUSDT", "ADAUSDT", "DOTUSDT", "SUIUSDT", "FILUSDT"]
    oos_start, oos_end = "2025-01-01", "2026-03-31"

    log.info("Running backtest on 6 core coins (v3 params)...")
    all_trades = []
    for symbol in coins:
        coin = symbol.replace("USDT", "")
        ohlcv = bt.fetch_binance_15m(symbol, years=3)
        if "date_time" in ohlcv.columns:
            ohlcv = ohlcv.rename(columns={"date_time": "ts"})
        alt_df = bt.build_alt_technicals(ohlcv)
        oos_mask = (alt_df["ts"] >= oos_start) & (alt_df["ts"] <= oos_end)

        cfg = COIN_CONFIGS.get(coin, {})
        signals, alt_merged = bt.generate_btc_led_signal(
            btc_score_ts, alt_df[oos_mask],
            threshold=cfg.get("threshold", 3.0),
            use_alt_pa_filter=cfg.get("use_alt_pa_filter", False))
        trades = bt.run_backtest(alt_merged, signals,
                                  sl_atr_mult=cfg.get("sl_atr_mult", 2.5),
                                  tp_atr_mult=cfg.get("tp_atr_mult", 4.0),
                                  cooldown_bars=cfg.get("cooldown_bars", 4))
        if len(trades) > 0:
            trades["coin"] = coin
            all_trades.append(trades)

    trades_df = pd.concat(all_trades, ignore_index=True)
    log.info(f"Total trades: {len(trades_df)}, WR: {(trades_df['pnl_net']>0).mean()*100:.1f}%")

    # ---- Map factor scores to each trade's entry time ----
    log.info("Mapping factor scores to trade entries...")
    for fname in FACTOR_NAMES + ["total"]:
        vals = []
        for _, t in trades_df.iterrows():
            et = t["entry_time"]
            # Find closest factor score at or before entry time
            mask = factor_df.index <= et
            if mask.any():
                vals.append(factor_df.loc[mask, fname].iloc[-1])
            else:
                vals.append(0.0)
        trades_df[f"f_{fname}"] = vals

    # Also map v5 factor scores
    for fname in FACTOR_NAMES + ["total"]:
        vals = []
        for _, t in trades_df.iterrows():
            et = t["entry_time"]
            mask = factor_df_v5.index <= et
            if mask.any():
                vals.append(factor_df_v5.loc[mask, fname].iloc[-1])
            else:
                vals.append(0.0)
        trades_df[f"v5_{fname}"] = vals

    # Split into winners and losers
    winners = trades_df[trades_df["pnl_net"] > 0].copy()
    losers = trades_df[trades_df["pnl_net"] <= 0].copy()
    log.info(f"Winners: {len(winners)}, Losers: {len(losers)}")

    results = {}

    # ========== EXP1: Mean factor scores (winners vs losers) ==========
    log.info("EXP1: Mean factor scores per W/L...")
    exp1 = {}
    for fname in FACTOR_NAMES:
        col = f"f_{fname}"
        w_mean = float(winners[col].mean())
        l_mean = float(losers[col].mean())
        all_mean = float(trades_df[col].mean())
        # Signed contribution: positive = aligned with trade direction
        exp1[fname] = {
            "winner_mean": round(w_mean, 4),
            "loser_mean": round(l_mean, 4),
            "all_mean": round(all_mean, 4),
            "delta_w_minus_l": round(w_mean - l_mean, 4),
            "winner_abs_mean": round(float(winners[col].abs().mean()), 4),
            "loser_abs_mean": round(float(losers[col].abs().mean()), 4),
        }
    # Total score
    exp1["total_score"] = {
        "winner_mean": round(float(winners["f_total"].mean()), 4),
        "loser_mean": round(float(losers["f_total"].mean()), 4),
        "delta": round(float(winners["f_total"].mean() - losers["f_total"].mean()), 4),
    }
    results["exp1_mean_scores"] = exp1

    # ========== EXP2: Factor agreement with trade direction ==========
    log.info("EXP2: Factor agreement with trade direction...")
    exp2 = {}
    for fname in FACTOR_NAMES:
        col = f"f_{fname}"
        # Agreement = factor sign matches trade direction (L=+1, S=-1)
        trade_dir = trades_df["dir"].map({"L": 1, "S": -1}).fillna(0)
        factor_sign = np.sign(trades_df[col])
        agrees = (factor_sign == trade_dir) & (factor_sign != 0)

        w_dir = winners["dir"].map({"L": 1, "S": -1}).fillna(0)
        w_sign = np.sign(winners[col])
        w_agrees = (w_sign == w_dir) & (w_sign != 0)

        l_dir = losers["dir"].map({"L": 1, "S": -1}).fillna(0)
        l_sign = np.sign(losers[col])
        l_agrees = (l_sign == l_dir) & (l_sign != 0)

        # Also count "active" (non-zero)
        active_pct = float((trades_df[col] != 0).mean() * 100)
        w_active_pct = float((winners[col] != 0).mean() * 100)
        l_active_pct = float((losers[col] != 0).mean() * 100)

        exp2[fname] = {
            "overall_agreement_pct": round(float(agrees.mean() * 100), 1),
            "winner_agreement_pct": round(float(w_agrees.mean() * 100), 1),
            "loser_agreement_pct": round(float(l_agrees.mean() * 100), 1),
            "delta_agreement_pp": round(float(w_agrees.mean() - l_agrees.mean()) * 100, 1),
            "active_pct": round(active_pct, 1),
            "winner_active_pct": round(w_active_pct, 1),
            "loser_active_pct": round(l_active_pct, 1),
        }
    results["exp2_agreement"] = exp2

    # ========== EXP3: Dominant factor analysis ==========
    log.info("EXP3: Dominant factor analysis...")
    exp3 = {}
    # For each trade, which factor has the largest absolute contribution?
    factor_cols = [f"f_{f}" for f in FACTOR_NAMES]
    abs_factors = trades_df[factor_cols].abs()
    dominant_idx = abs_factors.idxmax(axis=1)
    dominant_name = dominant_idx.str.replace("f_", "", regex=False)

    for fname in FACTOR_NAMES:
        mask = dominant_name == fname
        w_mask = mask & (trades_df["pnl_net"] > 0)
        l_mask = mask & (trades_df["pnl_net"] <= 0)

        exp3[fname] = {
            "dominant_count": int(mask.sum()),
            "dominant_pct": round(float(mask.mean() * 100), 1),
            "dominant_wr": round(float(w_mask.sum() / mask.sum() * 100), 1) if mask.sum() > 0 else 0,
            "dominant_avg_pnl": round(float(trades_df.loc[mask, "pnl_net"].mean()), 2) if mask.sum() > 0 else 0,
            "dominant_total_pnl": round(float(trades_df.loc[mask, "pnl_net"].sum()), 2) if mask.sum() > 0 else 0,
        }
    results["exp3_dominant"] = exp3

    # ========== EXP4: Factor pair interaction ==========
    log.info("EXP4: Factor pair interactions...")
    exp4 = {}
    for f1, f2 in combinations(FACTOR_NAMES, 2):
        c1, c2 = f"f_{f1}", f"f_{f2}"
        # Both active and same sign
        both_same = (np.sign(trades_df[c1]) == np.sign(trades_df[c2])) & (trades_df[c1] != 0) & (trades_df[c2] != 0)
        # Both active and opposite sign
        both_opp = (np.sign(trades_df[c1]) == -np.sign(trades_df[c2])) & (trades_df[c1] != 0) & (trades_df[c2] != 0)

        if both_same.sum() >= 10:
            same_wr = float((trades_df.loc[both_same, "pnl_net"] > 0).mean() * 100)
            same_pnl = float(trades_df.loc[both_same, "pnl_net"].mean())
        else:
            same_wr, same_pnl = None, None

        if both_opp.sum() >= 10:
            opp_wr = float((trades_df.loc[both_opp, "pnl_net"] > 0).mean() * 100)
            opp_pnl = float(trades_df.loc[both_opp, "pnl_net"].mean())
        else:
            opp_wr, opp_pnl = None, None

        pair_key = f"{f1}+{f2}"
        exp4[pair_key] = {
            "same_sign_count": int(both_same.sum()),
            "same_sign_wr": round(same_wr, 1) if same_wr is not None else None,
            "same_sign_avg_pnl": round(same_pnl, 2) if same_pnl is not None else None,
            "opp_sign_count": int(both_opp.sum()),
            "opp_sign_wr": round(opp_wr, 1) if opp_wr is not None else None,
            "opp_sign_avg_pnl": round(opp_pnl, 2) if opp_pnl is not None else None,
        }
    results["exp4_interactions"] = exp4

    # ========== EXP5: Score composition (% of total from each factor) ==========
    log.info("EXP5: Score composition...")
    exp5 = {}
    # Signed contribution ratio: factor_score / total_score (when total != 0)
    valid = trades_df["f_total"].abs() > 0.1
    for fname in FACTOR_NAMES:
        col = f"f_{fname}"
        if valid.sum() > 0:
            ratio = (trades_df.loc[valid, col] / trades_df.loc[valid, "f_total"])
            w_valid = valid & (trades_df["pnl_net"] > 0)
            l_valid = valid & (trades_df["pnl_net"] <= 0)
            exp5[fname] = {
                "avg_contribution_pct": round(float(ratio.mean() * 100), 1),
                "winner_contribution_pct": round(float((trades_df.loc[w_valid, col] / trades_df.loc[w_valid, "f_total"]).mean() * 100), 1) if w_valid.sum() > 0 else 0,
                "loser_contribution_pct": round(float((trades_df.loc[l_valid, col] / trades_df.loc[l_valid, "f_total"]).mean() * 100), 1) if l_valid.sum() > 0 else 0,
                "abs_contribution_pct": round(float((trades_df.loc[valid, col].abs() / trades_df.loc[valid, "f_total"].abs()).mean() * 100), 1),
            }
        else:
            exp5[fname] = {"avg_contribution_pct": 0, "winner_contribution_pct": 0, "loser_contribution_pct": 0}
    results["exp5_composition"] = exp5

    # ========== EXP6: v5 comparison ==========
    log.info("EXP6: v5 factor attribution comparison...")
    exp6 = {}
    for fname in FACTOR_NAMES:
        v3_col = f"f_{fname}"
        v5_col = f"v5_{fname}"
        exp6[fname] = {
            "v3_winner_mean": round(float(winners[v3_col].mean()), 4),
            "v5_winner_mean": round(float(winners[v5_col].mean()), 4),
            "v3_loser_mean": round(float(losers[v3_col].mean()), 4),
            "v5_loser_mean": round(float(losers[v5_col].mean()), 4),
            "v3_delta_wl": round(float(winners[v3_col].mean() - losers[v3_col].mean()), 4),
            "v5_delta_wl": round(float(winners[v5_col].mean() - losers[v5_col].mean()), 4),
        }
    exp6["total_score"] = {
        "v3_winner_mean": round(float(winners["f_total"].mean()), 4),
        "v5_winner_mean": round(float(winners["v5_total"].mean()), 4),
        "v3_loser_mean": round(float(losers["f_total"].mean()), 4),
        "v5_loser_mean": round(float(losers["v5_total"].mean()), 4),
        "v3_delta": round(float(winners["f_total"].mean() - losers["f_total"].mean()), 4),
        "v5_delta": round(float(winners["v5_total"].mean() - losers["v5_total"].mean()), 4),
    }
    results["exp6_v5_comparison"] = exp6

    # ========== EXP7: Factor count analysis ==========
    log.info("EXP7: Active factor count at entry...")
    exp7 = {}
    active_count = (trades_df[factor_cols].abs() > 0).sum(axis=1)
    trades_df["active_factors"] = active_count

    for n in sorted(active_count.unique()):
        mask = active_count == n
        if mask.sum() >= 5:
            wr = float((trades_df.loc[mask, "pnl_net"] > 0).mean() * 100)
            avg_pnl = float(trades_df.loc[mask, "pnl_net"].mean())
            exp7[str(int(n))] = {
                "trades": int(mask.sum()),
                "wr": round(wr, 1),
                "avg_pnl": round(avg_pnl, 2),
                "total_pnl": round(float(trades_df.loc[mask, "pnl_net"].sum()), 2),
            }
    results["exp7_active_count"] = exp7

    # ========== EXP8: Factor by direction (LONG vs SHORT) ==========
    log.info("EXP8: Factor attribution by direction...")
    exp8 = {}
    for direction in ["L", "S"]:
        dir_df = trades_df[trades_df["dir"] == direction]
        dir_w = dir_df[dir_df["pnl_net"] > 0]
        dir_l = dir_df[dir_df["pnl_net"] <= 0]
        dir_result = {}
        for fname in FACTOR_NAMES:
            col = f"f_{fname}"
            dir_result[fname] = {
                "winner_mean": round(float(dir_w[col].mean()), 4) if len(dir_w) > 0 else 0,
                "loser_mean": round(float(dir_l[col].mean()), 4) if len(dir_l) > 0 else 0,
                "delta": round(float(dir_w[col].mean() - dir_l[col].mean()), 4) if len(dir_w) > 0 and len(dir_l) > 0 else 0,
            }
        exp8[direction] = {
            "trades": len(dir_df),
            "wr": round(float((dir_df["pnl_net"] > 0).mean() * 100), 1),
            "factors": dir_result,
        }
    results["exp8_direction"] = exp8

    # ========== Summary statistics ==========
    results["summary"] = {
        "total_trades": len(trades_df),
        "winners": len(winners),
        "losers": len(losers),
        "wr_pct": round(float(len(winners) / len(trades_df) * 100), 1),
        "total_pnl": round(float(trades_df["pnl_net"].sum()), 2),
        "avg_pnl": round(float(trades_df["pnl_net"].mean()), 2),
        "coins": [c.replace("USDT", "") for c in coins],
        "oos_period": f"{oos_start} to {oos_end}",
    }

    finished_at = datetime.utcnow()
    results["meta"] = {
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_sec": round((finished_at - started_at).total_seconds(), 1),
    }

    # ---- Print summary ----
    print("\n" + "="*70)
    print("MISSION 010: Factor Contribution Attribution -- RESULTS")
    print("="*70)

    print(f"\nTotal: {len(trades_df)} trades, WR {results['summary']['wr_pct']}%, PnL ${results['summary']['total_pnl']}")
    print(f"Winners: {len(winners)}, Losers: {len(losers)}")

    print("\n--- EXP1: Mean Factor Scores (W vs L) ---")
    print(f"{'Factor':<20} {'W Mean':>8} {'L Mean':>8} {'Delta':>8} {'Signal?':>8}")
    for fname in FACTOR_NAMES:
        e = exp1[fname]
        signal = "***" if abs(e["delta_w_minus_l"]) > 0.1 else ""
        print(f"{fname:<20} {e['winner_mean']:>8.3f} {e['loser_mean']:>8.3f} {e['delta_w_minus_l']:>8.3f} {signal:>8}")
    e = exp1["total_score"]
    print(f"{'TOTAL SCORE':<20} {e['winner_mean']:>8.3f} {e['loser_mean']:>8.3f} {e['delta']:>8.3f}")

    print("\n--- EXP2: Factor Agreement with Trade Direction ---")
    print(f"{'Factor':<20} {'W Agree%':>10} {'L Agree%':>10} {'Delta pp':>10} {'Active%':>10}")
    for fname in FACTOR_NAMES:
        e = exp2[fname]
        print(f"{fname:<20} {e['winner_agreement_pct']:>10.1f} {e['loser_agreement_pct']:>10.1f} {e['delta_agreement_pp']:>10.1f} {e['active_pct']:>10.1f}")

    print("\n--- EXP3: Dominant Factor WR ---")
    print(f"{'Factor':<20} {'Count':>8} {'%':>8} {'WR%':>8} {'Avg PnL':>10} {'Total PnL':>12}")
    for fname in FACTOR_NAMES:
        e = exp3[fname]
        print(f"{fname:<20} {e['dominant_count']:>8} {e['dominant_pct']:>8.1f} {e['dominant_wr']:>8.1f} {e['dominant_avg_pnl']:>10.2f} {e['dominant_total_pnl']:>12.2f}")

    print("\n--- EXP7: Active Factor Count at Entry ---")
    print(f"{'#Factors':>10} {'Trades':>8} {'WR%':>8} {'Avg PnL':>10} {'Total PnL':>12}")
    for n, e in sorted(exp7.items(), key=lambda x: int(x[0])):
        print(f"{n:>10} {e['trades']:>8} {e['wr']:>8.1f} {e['avg_pnl']:>10.2f} {e['total_pnl']:>12.2f}")

    print("\n--- EXP8: Top Factor Deltas by Direction ---")
    for direction in ["L", "S"]:
        d = exp8[direction]
        print(f"\n{direction} trades: {d['trades']}, WR: {d['wr']}%")
        sorted_factors = sorted(d["factors"].items(), key=lambda x: abs(x[1]["delta"]), reverse=True)
        for fname, f_data in sorted_factors[:5]:
            print(f"  {fname:<20} W={f_data['winner_mean']:>7.3f} L={f_data['loser_mean']:>7.3f} delta={f_data['delta']:>7.3f}")

    # Find top interaction pairs
    print("\n--- EXP4: Top Factor Pair Interactions ---")
    good_pairs = [(k, v) for k, v in exp4.items()
                  if v["same_sign_wr"] is not None and v["same_sign_count"] >= 20]
    good_pairs.sort(key=lambda x: x[1]["same_sign_wr"] or 0, reverse=True)
    print(f"{'Pair':<35} {'Count':>6} {'WR%':>6} {'Avg PnL':>8}")
    for pair, v in good_pairs[:10]:
        print(f"{pair:<35} {v['same_sign_count']:>6} {v['same_sign_wr']:>6.1f} {v['same_sign_avg_pnl']:>8.2f}")

    print("\n--- EXP6: v3 vs v5 Score Separation ---")
    e = exp6["total_score"]
    print(f"v3: W={e['v3_winner_mean']:.3f} L={e['v3_loser_mean']:.3f} delta={e['v3_delta']:.3f}")
    print(f"v5: W={e['v5_winner_mean']:.3f} L={e['v5_loser_mean']:.3f} delta={e['v5_delta']:.3f}")
    print(f"v5 improves W-L separation by {abs(e['v5_delta']) - abs(e['v3_delta']):.3f}")

    # ---- Save results ----
    out_json = BASE_DIR / "missions" / "mission_010_factor_attribution.json"
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False, default=str)
    log.info(f"Saved: {out_json}")

    return results


if __name__ == "__main__":
    results = main()
