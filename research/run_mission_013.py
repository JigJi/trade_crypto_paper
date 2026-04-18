"""
Mission 013: Confluence x Vol Regime Adaptive Filter
=====================================================
Builds on Mission 010 (factor confluence 3+ = WR 80%+)
and Mission 012 (Extreme vol = -$240, Normal = $6,649).

Key hypothesis: Require HIGHER factor confluence in bad vol regimes.
Normal vol: trade everything. Extreme vol: only trade 3+ factor confluence.

Experiments:
  EXP1: Confluence x Regime performance matrix
  EXP2: Adaptive confluence filter (min_factors varies by regime)
  EXP3: Regime-aware confidence sizing (confluence + vol combined)
  EXP4: Confluence trend as regime shift warning
  EXP5: Optimal min_confluence per regime (grid search)
  EXP6: Direction x Confluence x Regime (3-way interaction)
"""

import sys, json, logging
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta

logging.basicConfig(level=logging.WARNING)
log = logging.getLogger("mission_013")

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import backtest_15m_btc_led_alts as bt
from paper_trading.config import COMPOSITE_WEIGHTS, V3_EXTRA_WEIGHTS, COIN_CONFIGS
from paper_trading.strategy import score_ob_combined, score_basis_contrarian, score_tick_liq

# ---- Config ----
OOS_START = "2025-01-01"
OOS_END = "2026-03-31"
COINS = ["BTCUSDT", "XRPUSDT", "ADAUSDT", "DOTUSDT", "SUIUSDT", "FILUSDT"]
VOL_LOOKBACK = 96  # 24h rolling

results = {}
started_at = datetime.utcnow()

# ---- Factor decomposition (from mission 010) ----
def compute_individual_factor_scores(df, params=None, extra=None):
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

    # 6-8. Extra factors
    scores["basis_contrarian"] = bt.score_basis_contrarian(df, weight=extra.get("basis_contrarian", 1.5))
    scores["tick_liq"] = bt.score_tick_liq(df, weight=extra.get("tick_liq", 2.0))
    scores["ob_combined"] = bt.score_ob_combined(df, weight=extra.get("ob_combined", 2.0))

    return scores

# ==============================================================
# Step 0: Load data
# ==============================================================
print("=" * 60)
print("Mission 013: Confluence x Vol Regime Adaptive Filter")
print("=" * 60)

print("\n[Step 0] Loading BTC data...")
btc_ohlcv = bt.fetch_binance_15m("BTCUSDT", years=3)
if "date_time" in btc_ohlcv.columns:
    btc_ohlcv = btc_ohlcv.rename(columns={"date_time": "ts"})

db_data = bt.load_btc_db_data()
btc_df = bt.build_btc_features(btc_ohlcv, db_data)

# Compute individual factor scores
factor_scores = compute_individual_factor_scores(btc_df)
FACTOR_NAMES = list(factor_scores.keys())

# Compute total composite score (v3)
btc_score = bt.compute_btc_composite_score(btc_df, COMPOSITE_WEIGHTS)
btc_score = btc_score + score_ob_combined(btc_df, weight=V3_EXTRA_WEIGHTS["ob_combined"])
btc_score = btc_score + score_basis_contrarian(btc_df, weight=V3_EXTRA_WEIGHTS["basis_contrarian"])
btc_score = btc_score + score_tick_liq(btc_df, weight=V3_EXTRA_WEIGHTS["tick_liq"])
btc_score_ts = pd.Series(btc_score.values, index=btc_df["ts"].values, name="btc_score")

# Build factor DataFrame aligned to timestamps
factor_df = pd.DataFrame({k: v.values for k, v in factor_scores.items()},
                          index=btc_df["ts"].values)
factor_df["total"] = btc_score.values

# Active factor count per bar (how many factors are non-zero)
factor_cols_list = [f for f in FACTOR_NAMES]
active_count_series = pd.Series(
    (factor_df[factor_cols_list].abs() > 0).sum(axis=1).values,
    index=factor_df.index,
    name="active_factors"
)

# Compute vol regimes (from mission 012)
btc_df["log_ret"] = np.log(btc_df["close"] / btc_df["close"].shift(1))
btc_df["realized_vol"] = btc_df["log_ret"].rolling(VOL_LOOKBACK).std() * np.sqrt(4 * 24 * 365)

oos_mask = (btc_df["ts"] >= OOS_START) & (btc_df["ts"] <= OOS_END)
btc_oos = btc_df[oos_mask].copy()

rv = btc_oos["realized_vol"].dropna()
q25, q75, q90 = rv.quantile([0.25, 0.75, 0.9]).values

def classify_vol(v):
    if pd.isna(v):
        return "Unknown"
    if v <= q25:
        return "Low"
    elif v <= q75:
        return "Normal"
    elif v <= q90:
        return "High"
    else:
        return "Extreme"

btc_oos["vol_regime"] = btc_oos["realized_vol"].apply(classify_vol)
vol_regime_lookup = pd.Series(btc_oos["vol_regime"].values, index=btc_oos["ts"].values)
rv_lookup = pd.Series(btc_oos["realized_vol"].values, index=btc_oos["ts"].values)

print(f"  BTC OOS: {len(btc_oos)} bars ({OOS_START} to {OOS_END})")
print(f"  Vol thresholds: Low<{q25:.1f}%, Normal<{q75:.1f}%, High<{q90:.1f}%")

# Run backtest on 6 core coins
print("\n[Step 0b] Running backtest on 6 coins...")
all_trades = []
for symbol in COINS:
    coin = symbol.replace("USDT", "")
    ohlcv = bt.fetch_binance_15m(symbol, years=3)
    if "date_time" in ohlcv.columns:
        ohlcv = ohlcv.rename(columns={"date_time": "ts"})
    alt_df = bt.build_alt_technicals(ohlcv)
    oos_m = (alt_df["ts"] >= OOS_START) & (alt_df["ts"] <= OOS_END)
    cfg = COIN_CONFIGS.get(coin, {})
    signals, alt_merged = bt.generate_btc_led_signal(
        btc_score_ts, alt_df[oos_m],
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

# Map factor data and vol regime to each trade
entry_active_counts = []
entry_vol_regimes = []
entry_rvs = []
for _, t in trades_df.iterrows():
    et = t["entry_time"]
    # Active factor count at entry
    mask = factor_df.index <= et
    if mask.any():
        row = factor_df.loc[mask].iloc[-1]
        count = int((row[factor_cols_list].abs() > 0).sum())
    else:
        count = 0
    entry_active_counts.append(count)
    # Vol regime at entry
    rv_mask = rv_lookup.index <= et
    if rv_mask.any():
        entry_rvs.append(rv_lookup[rv_mask].iloc[-1])
    else:
        entry_rvs.append(np.nan)
    vr_mask = vol_regime_lookup.index <= et
    if vr_mask.any():
        entry_vol_regimes.append(vol_regime_lookup[vr_mask].iloc[-1])
    else:
        entry_vol_regimes.append("Unknown")

trades_df["active_factors"] = entry_active_counts
trades_df["vol_regime"] = entry_vol_regimes
trades_df["entry_rv"] = entry_rvs

baseline_pnl = trades_df["pnl_net"].sum()
baseline_trades = len(trades_df)
baseline_wr = (trades_df["pnl_net"] > 0).mean() * 100

print(f"  Total: {baseline_trades} trades, WR {baseline_wr:.1f}%, PnL ${baseline_pnl:.0f}")

# ==============================================================
# EXP1: Confluence x Regime Performance Matrix
# ==============================================================
print("\n[Exp 1] Confluence x Regime Performance Matrix...")
REGIMES = ["Low", "Normal", "High", "Extreme"]
CONFLUENCE_BUCKETS = [(0, 1, "0-1"), (2, 2, "2"), (3, 3, "3"), (4, 8, "4+")]

exp1 = {}
print(f"{'Regime':<10} {'Conf':>6} {'Trades':>7} {'WR%':>6} {'AvgPnL':>8} {'TotalPnL':>10}")
print("-" * 55)

for regime in REGIMES:
    regime_data = {}
    for lo, hi, label in CONFLUENCE_BUCKETS:
        mask = (
            (trades_df["vol_regime"] == regime) &
            (trades_df["active_factors"] >= lo) &
            (trades_df["active_factors"] <= hi)
        )
        subset = trades_df[mask]
        if len(subset) < 5:
            regime_data[label] = {"trades": int(len(subset)), "wr": None, "avg_pnl": None, "total_pnl": None}
            continue
        wr = (subset["pnl_net"] > 0).mean() * 100
        avg_pnl = subset["pnl_net"].mean()
        total_pnl = subset["pnl_net"].sum()
        regime_data[label] = {
            "trades": int(len(subset)),
            "wr": round(wr, 1),
            "avg_pnl": round(avg_pnl, 2),
            "total_pnl": round(total_pnl, 1),
        }
        print(f"{regime:<10} {label:>6} {len(subset):>7} {wr:>6.1f} {avg_pnl:>8.2f} {total_pnl:>10.1f}")
    exp1[regime] = regime_data

results["exp1_confluence_x_regime"] = exp1

# ==============================================================
# EXP2: Adaptive Confluence Filter
# ==============================================================
print("\n[Exp 2] Adaptive Confluence Filter...")
# Test: require minimum active_factors per regime
filter_configs = {
    "baseline": {"Low": 0, "Normal": 0, "High": 0, "Extreme": 0},
    "extreme_conf3": {"Low": 0, "Normal": 0, "High": 0, "Extreme": 3},
    "high_extreme_conf2": {"Low": 0, "Normal": 0, "High": 2, "Extreme": 2},
    "high_extreme_conf3": {"Low": 0, "Normal": 0, "High": 3, "Extreme": 3},
    "progressive": {"Low": 0, "Normal": 1, "High": 2, "Extreme": 3},
    "aggressive": {"Low": 1, "Normal": 2, "High": 3, "Extreme": 4},
    "conf2_everywhere": {"Low": 2, "Normal": 2, "High": 2, "Extreme": 2},
    "conf3_everywhere": {"Low": 3, "Normal": 3, "High": 3, "Extreme": 3},
}

exp2 = {}
print(f"{'Config':<25} {'Trades':>7} {'WR%':>6} {'PnL':>10} {'MaxDD':>8} {'Calmar':>8} {'vs Base':>8}")
print("-" * 80)

for name, min_conf_by_regime in filter_configs.items():
    mask = pd.Series(True, index=trades_df.index)
    for regime, min_conf in min_conf_by_regime.items():
        regime_mask = trades_df["vol_regime"] == regime
        conf_mask = trades_df["active_factors"] >= min_conf
        # Keep trade if either: not in this regime, or meets confluence requirement
        mask = mask & (~regime_mask | conf_mask)

    filtered = trades_df[mask]
    if len(filtered) == 0:
        exp2[name] = {"trades": 0}
        continue

    total_pnl = filtered["pnl_net"].sum()
    wr = (filtered["pnl_net"] > 0).mean() * 100
    cumsum = filtered["pnl_net"].cumsum()
    max_dd = (cumsum - cumsum.cummax()).min()
    calmar = round(total_pnl / max(-max_dd, 1), 2)
    delta_pnl = total_pnl - baseline_pnl

    exp2[name] = {
        "trades": int(len(filtered)),
        "wr": round(wr, 1),
        "total_pnl": round(total_pnl, 1),
        "max_dd": round(max_dd, 1),
        "calmar": calmar,
        "delta_pnl": round(delta_pnl, 1),
        "min_conf": min_conf_by_regime,
    }
    print(f"{name:<25} {len(filtered):>7} {wr:>6.1f} {total_pnl:>10.1f} {max_dd:>8.1f} {calmar:>8.2f} {delta_pnl:>+8.1f}")

results["exp2_adaptive_filter"] = exp2

# ==============================================================
# EXP3: Regime-Aware Confidence Sizing
# ==============================================================
print("\n[Exp 3] Regime-Aware Confidence Sizing...")
# Combine vol regime multiplier with confluence multiplier
sizing_configs = {
    "baseline_fixed": lambda r, c: 1.0,
    "vol_only": lambda r, c: {"Low": 1.0, "Normal": 1.0, "High": 0.75, "Extreme": 0.0}.get(r, 1.0),
    "conf_only": lambda r, c: 0.5 if c <= 1 else (1.0 if c == 2 else 1.5),
    "vol_x_conf": lambda r, c: (
        {"Low": 1.0, "Normal": 1.0, "High": 0.75, "Extreme": 0.5}.get(r, 1.0) *
        (0.5 if c <= 1 else (1.0 if c == 2 else 1.5))
    ),
    "vol_x_conf_skip_extreme": lambda r, c: (
        0.0 if (r == "Extreme" and c < 3) else
        {"Low": 1.0, "Normal": 1.0, "High": 0.75, "Extreme": 0.5}.get(r, 1.0) *
        (0.5 if c <= 1 else (1.0 if c == 2 else 1.5))
    ),
    "smart_filter": lambda r, c: (
        0.0 if (r == "Extreme" and c < 3) else
        0.0 if (r == "High" and c < 2) else
        0.5 if c <= 1 else (1.0 if c == 2 else 1.5)
    ),
}

exp3 = {}
print(f"{'Config':<30} {'Trades':>7} {'PnL':>10} {'MaxDD':>8} {'Calmar':>8} {'vs Base':>8}")
print("-" * 78)

for name, size_fn in sizing_configs.items():
    mults = [size_fn(r, c) for r, c in zip(trades_df["vol_regime"], trades_df["active_factors"])]
    trades_df[f"sz_{name}"] = mults
    adj_pnl = trades_df["pnl_net"] * trades_df[f"sz_{name}"]
    active = adj_pnl[trades_df[f"sz_{name}"] > 0]

    if len(active) == 0:
        exp3[name] = {"trades": 0}
        continue

    total_pnl = adj_pnl.sum()
    n_trades = int((trades_df[f"sz_{name}"] > 0).sum())
    wr = (trades_df.loc[trades_df[f"sz_{name}"] > 0, "pnl_net"] > 0).mean() * 100
    cumsum = adj_pnl.cumsum()
    max_dd = (cumsum - cumsum.cummax()).min()
    calmar = round(total_pnl / max(-max_dd, 1), 2)
    delta = total_pnl - baseline_pnl

    exp3[name] = {
        "trades": n_trades,
        "wr": round(wr, 1),
        "total_pnl": round(total_pnl, 1),
        "max_dd": round(max_dd, 1),
        "calmar": calmar,
        "delta_pnl": round(delta, 1),
    }
    print(f"{name:<30} {n_trades:>7} {total_pnl:>10.1f} {max_dd:>8.1f} {calmar:>8.2f} {delta:>+8.1f}")

results["exp3_confidence_sizing"] = exp3

# ==============================================================
# EXP4: Confluence Trend as Warning Signal
# ==============================================================
print("\n[Exp 4] Confluence Trend as Regime Warning...")
# Does declining average confluence precede poor performance?
# Compute rolling average active_factors over last 4h (16 bars)
btc_oos_af = btc_oos.copy()
af_at_bar = []
for _, row in btc_oos_af.iterrows():
    ts = row["ts"]
    mask = factor_df.index <= ts
    if mask.any():
        frow = factor_df.loc[mask].iloc[-1]
        af_at_bar.append(int((frow[factor_cols_list].abs() > 0).sum()))
    else:
        af_at_bar.append(0)
btc_oos_af["active_factors"] = af_at_bar
btc_oos_af["af_ma16"] = btc_oos_af["active_factors"].rolling(16, min_periods=1).mean()
btc_oos_af["af_trend"] = btc_oos_af["af_ma16"] - btc_oos_af["af_ma16"].shift(16)

# Map confluence trend to trades
trade_af_trends = []
for _, t in trades_df.iterrows():
    et = t["entry_time"]
    mask = btc_oos_af["ts"] <= et
    if mask.any():
        trade_af_trends.append(btc_oos_af.loc[mask.values, "af_trend"].iloc[-1])
    else:
        trade_af_trends.append(0)
trades_df["af_trend"] = trade_af_trends

# Bucket: rising, stable, falling confluence trend
exp4 = {}
trend_buckets = [
    ("falling", -999, -0.3),
    ("stable", -0.3, 0.3),
    ("rising", 0.3, 999),
]
print(f"{'Trend':<12} {'Trades':>7} {'WR%':>6} {'AvgPnL':>8} {'TotalPnL':>10}")
print("-" * 50)

for label, lo, hi in trend_buckets:
    mask = (trades_df["af_trend"] > lo) & (trades_df["af_trend"] <= hi)
    subset = trades_df[mask]
    if len(subset) < 5:
        exp4[label] = {"trades": int(len(subset))}
        continue
    wr = (subset["pnl_net"] > 0).mean() * 100
    avg_pnl = subset["pnl_net"].mean()
    total_pnl = subset["pnl_net"].sum()
    exp4[label] = {
        "trades": int(len(subset)),
        "wr": round(wr, 1),
        "avg_pnl": round(avg_pnl, 2),
        "total_pnl": round(total_pnl, 1),
    }
    print(f"{label:<12} {len(subset):>7} {wr:>6.1f} {avg_pnl:>8.2f} {total_pnl:>10.1f}")

results["exp4_confluence_trend"] = exp4

# ==============================================================
# EXP5: Grid Search - Optimal Min Confluence per Regime
# ==============================================================
print("\n[Exp 5] Grid Search: Optimal Min Confluence per Regime...")
# Try all combinations of min_conf [0,1,2,3,4] for each regime
# But that's 5^4 = 625 combos. Simplify: Low always 0, vary Normal/High/Extreme
best_calmar = 0
best_config = None
best_result = None

exp5_all = []
for n_conf in range(0, 4):
    for h_conf in range(0, 5):
        for e_conf in range(0, 5):
            min_conf = {"Low": 0, "Normal": n_conf, "High": h_conf, "Extreme": e_conf}
            mask = pd.Series(True, index=trades_df.index)
            for regime, mc in min_conf.items():
                rm = trades_df["vol_regime"] == regime
                cm = trades_df["active_factors"] >= mc
                mask = mask & (~rm | cm)

            filtered = trades_df[mask]
            if len(filtered) < 100:
                continue

            total_pnl = filtered["pnl_net"].sum()
            wr = (filtered["pnl_net"] > 0).mean() * 100
            cumsum = filtered["pnl_net"].cumsum()
            max_dd = (cumsum - cumsum.cummax()).min()
            calmar = total_pnl / max(-max_dd, 1) if max_dd < 0 else total_pnl

            entry = {
                "normal": n_conf, "high": h_conf, "extreme": e_conf,
                "trades": int(len(filtered)),
                "wr": round(wr, 1),
                "pnl": round(total_pnl, 1),
                "max_dd": round(max_dd, 1),
                "calmar": round(calmar, 2),
            }
            exp5_all.append(entry)

            if calmar > best_calmar:
                best_calmar = calmar
                best_config = min_conf.copy()
                best_result = entry.copy()

# Top 10 by Calmar
exp5_sorted = sorted(exp5_all, key=lambda x: x["calmar"], reverse=True)[:10]
print(f"\nTop 10 configs by Calmar:")
print(f"{'N':>3} {'H':>3} {'E':>3} {'Trades':>7} {'WR%':>6} {'PnL':>10} {'MaxDD':>8} {'Calmar':>8}")
print("-" * 55)
for e in exp5_sorted:
    print(f"{e['normal']:>3} {e['high']:>3} {e['extreme']:>3} {e['trades']:>7} {e['wr']:>6.1f} {e['pnl']:>10.1f} {e['max_dd']:>8.1f} {e['calmar']:>8.2f}")

if best_config:
    print(f"\nBest: Normal>={best_config['Normal']}, High>={best_config['High']}, Extreme>={best_config['Extreme']}")
    print(f"  => {best_result['trades']} trades, WR {best_result['wr']}%, PnL ${best_result['pnl']}, Calmar {best_result['calmar']}")

results["exp5_grid_search"] = {
    "total_configs_tested": len(exp5_all),
    "best_config": best_config,
    "best_result": best_result,
    "top10": exp5_sorted,
}

# ==============================================================
# EXP6: Direction x Confluence x Regime (3-way)
# ==============================================================
print("\n[Exp 6] Direction x Confluence x Regime...")
exp6 = {}
print(f"{'Dir':<4} {'Regime':<10} {'Conf':>5} {'Trades':>7} {'WR%':>6} {'AvgPnL':>8} {'TotalPnL':>10}")
print("-" * 60)

for direction in ["L", "S"]:
    dir_data = {}
    for regime in REGIMES:
        regime_data = {}
        for lo, hi, label in [(0, 2, "0-2"), (3, 8, "3+")]:
            mask = (
                (trades_df["dir"] == direction) &
                (trades_df["vol_regime"] == regime) &
                (trades_df["active_factors"] >= lo) &
                (trades_df["active_factors"] <= hi)
            )
            subset = trades_df[mask]
            if len(subset) < 5:
                regime_data[label] = {"trades": int(len(subset)), "wr": None}
                continue
            wr = (subset["pnl_net"] > 0).mean() * 100
            avg_pnl = subset["pnl_net"].mean()
            total_pnl = subset["pnl_net"].sum()
            regime_data[label] = {
                "trades": int(len(subset)),
                "wr": round(wr, 1),
                "avg_pnl": round(avg_pnl, 2),
                "total_pnl": round(total_pnl, 1),
            }
            print(f"{direction:<4} {regime:<10} {label:>5} {len(subset):>7} {wr:>6.1f} {avg_pnl:>8.2f} {total_pnl:>10.1f}")
        dir_data[regime] = regime_data
    exp6[direction] = dir_data

results["exp6_3way_interaction"] = exp6

# ==============================================================
# Summary
# ==============================================================
finished_at = datetime.utcnow()

# Find best adaptive filter
best_filter = max(
    [(k, v) for k, v in exp2.items() if v.get("trades", 0) > 0],
    key=lambda x: x[1].get("calmar", 0),
    default=(None, {})
)

# Find best sizing
best_sizing = max(
    [(k, v) for k, v in exp3.items() if v.get("trades", 0) > 0],
    key=lambda x: x[1].get("calmar", 0),
    default=(None, {})
)

print("\n" + "=" * 60)
print("SUMMARY")
print("=" * 60)
print(f"\n  Baseline: {baseline_trades} trades, WR {baseline_wr:.1f}%, PnL ${baseline_pnl:.0f}")
print(f"  Best adaptive filter: {best_filter[0]} (Calmar {best_filter[1].get('calmar', 0)}, PnL ${best_filter[1].get('total_pnl', 0)})")
print(f"  Best confidence sizing: {best_sizing[0]} (Calmar {best_sizing[1].get('calmar', 0)}, PnL ${best_sizing[1].get('total_pnl', 0)})")
if best_config:
    print(f"  Grid search best: N>={best_config['Normal']} H>={best_config['High']} E>={best_config['Extreme']} (Calmar {best_result['calmar']})")
print(f"  Duration: {(finished_at - started_at).total_seconds():.1f}s")

results["summary"] = {
    "baseline_trades": baseline_trades,
    "baseline_wr": round(baseline_wr, 1),
    "baseline_pnl": round(baseline_pnl, 1),
    "best_adaptive_filter": best_filter[0],
    "best_filter_calmar": best_filter[1].get("calmar", 0),
    "best_filter_pnl": best_filter[1].get("total_pnl", 0),
    "best_filter_delta_pnl": best_filter[1].get("delta_pnl", 0),
    "best_sizing": best_sizing[0],
    "best_sizing_calmar": best_sizing[1].get("calmar", 0),
    "best_sizing_pnl": best_sizing[1].get("total_pnl", 0),
    "grid_search_best_config": best_config,
    "grid_search_best_calmar": best_result["calmar"] if best_result else 0,
    "experiments": 6,
    "total_configs_tested": len(exp5_all),
    "duration_sec": round((finished_at - started_at).total_seconds(), 1),
}

results["meta"] = {
    "started_at": started_at.isoformat(),
    "finished_at": finished_at.isoformat(),
    "oos_period": f"{OOS_START} to {OOS_END}",
    "coins": [c.replace("USDT", "") for c in COINS],
}

# ==============================================================
# Save Results
# ==============================================================
print("\n[Saving results...]")

json_path = BASE_DIR / "missions" / "mission_013_confluence_x_regime.json"
with open(json_path, "w", encoding="utf-8") as f:
    json.dump(results, f, indent=2, ensure_ascii=False, default=str)
print(f"  Saved: {json_path}")

print("\nMission 013 complete!")
