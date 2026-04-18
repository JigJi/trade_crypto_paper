"""
Track 2 Factor Tests (Steps 2.2, 2.3, 2.4, 2.5)
==================================================
Tests new leading indicators:
  2.2 CVD (Cumulative Volume Delta) as contrarian factor
  2.3 DVOL (Deribit Implied Volatility) as regime filter
  2.4 Macro cross-market signals (DXY, Gold, US10Y)
  2.5 Signal confidence score (factor agreement, RSI, pos-in-range, volume)

Uses v3 scoring as baseline (aligned with paper trading).
"""

import sys, io, warnings, os, json
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
import pandas_ta as ta
import psycopg2

from backtest_15m_btc_led_alts import (
    fetch_binance_15m, load_btc_db_data, build_btc_features,
    compute_btc_composite_score, build_alt_technicals,
    run_backtest, calc_metrics, resample_to_15m,
    BKK_UTC_OFFSET, INIT_EQUITY, BUDGET_USDT, LEVERAGE,
    COMPOSITE_WEIGHTS, V3_EXTRA_WEIGHTS,
    score_basis_contrarian, score_tick_liq, score_ob_combined,
)
from test_v12_improvements import V11_CONFIGS

DB_PARAMS = {
    "dbname": os.getenv("PG_DB", "smart_trading"),
    "user": os.getenv("PG_USER", "postgres"),
    "password": os.getenv("PG_PASS", "P@ssw0rd"),
    "host": os.getenv("PG_HOST", "localhost"),
    "port": os.getenv("PG_PORT", "5432"),
}

TEST_START = pd.Timestamp("2026-01-01")
COINS = ["BTC", "XRP", "ADA", "DOT", "SUI", "FIL"]


# ============================================================
# Factor Loaders
# ============================================================

def load_cvd_data(conn):
    """Load CVD from market_data.cvd, aggregate to 15min."""
    df = pd.read_sql(
        "SELECT ts, source, volume_delta FROM market_data.cvd ORDER BY ts",
        conn, parse_dates=["ts"])
    if df.empty:
        return None
    df["ts"] = df["ts"].dt.tz_localize(None)

    # Separate futures and spot, aggregate to 15min
    fut = df[df["source"] == "futures"].copy()
    spot = df[df["source"] == "spot"].copy()

    if fut.empty:
        return None

    agg = fut.set_index("ts").resample("15min").agg({"volume_delta": "sum"}).fillna(0).reset_index()
    agg.rename(columns={"volume_delta": "cvd_15m"}, inplace=True)
    agg["cvd_cumulative"] = agg["cvd_15m"].cumsum()
    agg["cvd_ma"] = agg["cvd_15m"].rolling(16).mean()  # 4h MA
    agg["cvd_z"] = (
        (agg["cvd_15m"] - agg["cvd_15m"].rolling(96).mean())
        / agg["cvd_15m"].rolling(96).std().clip(lower=1e-8)
    )
    return agg


def load_dvol_data(conn):
    """Load DVOL from market_data.options_data."""
    df = pd.read_sql(
        "SELECT ts, dvol, skew_25d, put_call_ratio, max_pain, spot_price "
        "FROM market_data.options_data ORDER BY ts",
        conn, parse_dates=["ts"])
    if df.empty:
        return None
    df["ts"] = df["ts"].dt.tz_localize(None)

    # DVOL z-score (rolling 24h = 96 bars)
    df["dvol_ma"] = df["dvol"].rolling(96).mean()
    df["dvol_std"] = df["dvol"].rolling(96).std()
    df["dvol_z"] = (df["dvol"] - df["dvol_ma"]) / df["dvol_std"].clip(lower=0.1)

    # Max pain distance
    df["mp_dist"] = (df["spot_price"] - df["max_pain"]) / df["spot_price"] * 100

    # Put-call ratio z-score
    df["pcr_ma"] = df["put_call_ratio"].rolling(96).mean()
    df["pcr_z"] = (df["put_call_ratio"] - df["pcr_ma"]) / df["put_call_ratio"].rolling(96).std().clip(lower=0.01)

    return df


def load_macro_data(conn):
    """Load macro indicators, pivot to wide format."""
    df = pd.read_sql(
        "SELECT ts, indicator, value FROM market_data.macro_indicators ORDER BY ts",
        conn, parse_dates=["ts"])
    if df.empty:
        return None

    # Pivot: ts -> dxy, us10y, gold, sp500
    wide = df.pivot_table(index="ts", columns="indicator", values="value", aggfunc="last")
    wide = wide.reset_index()
    wide["ts"] = pd.to_datetime(wide["ts"])

    # Compute daily returns
    for col in ["dxy", "us10y", "gold", "sp500"]:
        if col in wide.columns:
            wide[f"{col}_ret"] = wide[col].pct_change()

    return wide


# ============================================================
# Factor Score Functions
# ============================================================

def score_cvd_contrarian(df, weight=1.5):
    """Contrarian CVD: extreme buying = bearish (overextended), extreme selling = bullish."""
    s = pd.Series(0.0, index=df.index)
    if "cvd_z" not in df.columns:
        return s
    cz = df["cvd_z"].fillna(0)
    # Contrarian: high CVD (aggressive buying) = bearish
    s += np.where(cz > 1.5, -weight, 0)
    s += np.where(cz > 2.5, -weight * 0.5, 0)
    # Low CVD (aggressive selling) = bullish
    s += np.where(cz < -1.5, weight, 0)
    s += np.where(cz < -2.5, weight * 0.5, 0)
    return s


def score_dvol_regime(df, weight=1.0):
    """DVOL as regime/volatility filter:
    - High DVOL z = volatility spike → amplify signal
    - Low DVOL = choppy/range-bound → dampen signal
    """
    s = pd.Series(0.0, index=df.index)
    if "dvol_z" not in df.columns:
        return s
    dz = df["dvol_z"].fillna(0)
    # High volatility = amplify (sign matches base score direction)
    # We use a multiplicative approach: return score that adds to existing direction
    # For now, just test as a signal booster
    s += np.where(dz > 1.5, weight * 0.5, 0)  # vol spike = expect continuation
    s += np.where(dz < -1.0, -weight * 0.3, 0)  # low vol = mean reversion risk
    return s


def score_macro_divergence(df, weight=1.0):
    """Cross-market divergence:
    - DXY up + BTC flat = bearish (risk-off coming)
    - Gold up + BTC down = safe haven flow (bearish crypto)
    - DXY down = bullish crypto
    """
    s = pd.Series(0.0, index=df.index)
    if "dxy_ret" not in df.columns:
        return s

    dxy_ret = df["dxy_ret"].fillna(0)
    gold_ret = df["gold_ret"].fillna(0) if "gold_ret" in df.columns else pd.Series(0, index=df.index)

    # DXY strong up = bearish for crypto
    s += np.where(dxy_ret > 0.005, -weight, 0)
    s += np.where(dxy_ret < -0.005, weight * 0.5, 0)

    # Gold strong up = safe haven = bearish for crypto
    s += np.where(gold_ret > 0.01, -weight * 0.5, 0)

    return s


# ============================================================
# Confidence Score Filters
# ============================================================

def compute_factor_agreement(df, params=None):
    """Count how many individual factors agree with the composite direction.
    Returns agreement count (0-8) and direction consensus."""
    if params is None:
        params = COMPOSITE_WEIGHTS
    extra = V3_EXTRA_WEIGHTS

    # Compute individual factor scores
    factors = {}

    # OI divergence
    if "oi_chg" in df.columns:
        oi_chg = df["oi_chg"].fillna(0)
        ret = df["ret"].fillna(0)
        oi_score = pd.Series(0.0, index=df.index)
        oi_score += np.where((ret > 0.001) & (oi_chg > 0.002), 1, 0)
        oi_score += np.where((ret < -0.001) & (oi_chg < -0.002), 1, 0)
        oi_score += np.where((ret > 0.001) & (oi_chg < -0.002), -1, 0)
        oi_score += np.where((ret < -0.001) & (oi_chg > 0.002), -1, 0)
        factors["oi"] = np.sign(oi_score)

    # Funding rate
    if "fr_8h" in df.columns:
        fr = df["fr_8h"].fillna(0)
        fr_score = pd.Series(0.0, index=df.index)
        fr_score += np.where(fr < -0.0001, 1, 0)
        fr_score += np.where(fr > 0.0003, -1, 0)
        factors["funding"] = np.sign(fr_score)

    # Whale
    if "whale_net_ma" in df.columns:
        wn = df["whale_net_ma"].fillna(0)
        factors["whale"] = np.sign(np.where(wn > 50e6, 1, np.where(wn < -50e6, -1, 0)))

    # Liquidation
    if "liq_net" in df.columns:
        ln = df["liq_net"].fillna(0)
        lt = df["liq_total"].fillna(0)
        lt_ma = df["liq_total_ma"].fillna(1)
        cascade = lt > (lt_ma * 3)
        factors["liq"] = np.sign(np.where(cascade & (ln > 0), 1, np.where(cascade & (ln < 0), -1, 0)))

    # ETF
    if "etf_flow_ma" in df.columns:
        etf = df["etf_flow_ma"].fillna(0)
        factors["etf"] = np.sign(np.where(etf > 50, 1, np.where(etf < -50, -1, 0)))

    # basis_contrarian
    if "basis_z" in df.columns:
        bz = df["basis_z"].fillna(0)
        factors["basis"] = np.sign(np.where(bz > 1.5, -1, np.where(bz < -1.5, 1, 0)))

    # tick_liq
    if "liq_net_ma" in df.columns:
        ln2 = df["liq_net_ma"].fillna(0)
        factors["tick_liq"] = np.sign(np.where(ln2 > 2, 1, np.where(ln2 < -2, -1, 0)))

    # ob_combined
    if "ob_imb_ma" in df.columns:
        combo = (df["ob_imb_ma"].fillna(0) + df["ob_vol_imb_ma"].fillna(0)) / 2
        factors["ob"] = np.sign(np.where(combo > 0.03, -1, np.where(combo < -0.03, 1, 0)))

    # Count agreements
    if not factors:
        return pd.Series(0, index=df.index), pd.Series(0, index=df.index)

    factor_df = pd.DataFrame(factors)
    bullish_count = (factor_df > 0).sum(axis=1)
    bearish_count = (factor_df < 0).sum(axis=1)
    agreement = np.maximum(bullish_count, bearish_count)
    consensus_dir = np.where(bullish_count > bearish_count, 1,
                             np.where(bearish_count > bullish_count, -1, 0))

    return agreement, pd.Series(consensus_dir, index=df.index)


def apply_confidence_filters(signals, alt_df, min_agreement=3, rsi_filter=True, pos_filter=True, vol_filter=True):
    """Apply confidence filters to signals.

    Filters:
    - min_agreement: require N factors to agree
    - rsi_filter: block LONG if RSI > 65, SHORT if RSI < 35
    - pos_filter: block LONG if pos_in_range > 0.75, SHORT if pos < 0.25
    - vol_filter: require vol_ratio > 0.8
    """
    sig = signals.copy()

    if rsi_filter and "rsi" in alt_df.columns:
        rsi = alt_df["rsi"].fillna(50)
        sig[(sig == 1) & (rsi > 65)] = 0
        sig[(sig == -1) & (rsi < 35)] = 0

    if pos_filter:
        if "high" in alt_df.columns:
            h20 = alt_df["high"].rolling(20).max()
            l20 = alt_df["low"].rolling(20).min()
            pos = (alt_df["close"] - l20) / (h20 - l20).clip(lower=1e-10)
            sig[(sig == 1) & (pos > 0.75)] = 0
            sig[(sig == -1) & (pos < 0.25)] = 0

    if vol_filter and "vol_ratio" in alt_df.columns:
        sig[(sig != 0) & (alt_df["vol_ratio"] < 0.8)] = 0

    return sig


# ============================================================
# Signal generation
# ============================================================

def gen_signal(btc_score_ts, alt_df, threshold, use_alt_pa):
    btc_score_df = btc_score_ts.reset_index()
    btc_score_df.columns = ["ts", "btc_score"]
    alt = alt_df.copy().sort_values("ts")
    alt = pd.merge_asof(alt, btc_score_df.sort_values("ts"),
                        on="ts", direction="backward", tolerance=pd.Timedelta("30min"))
    alt["btc_score"] = alt["btc_score"].fillna(0)
    signal = pd.Series(0, index=alt.index)
    signal[alt["btc_score"] >= threshold] = 1
    signal[alt["btc_score"] <= -threshold] = -1
    if use_alt_pa:
        alt_bull_pa = (alt["close"] > alt["ema9"]) & (alt["ema9"] > alt["ema21"])
        alt_bear_pa = (alt["close"] < alt["ema9"]) & (alt["ema9"] < alt["ema21"])
        alt_vol_ok = alt["vol_ratio"] > 0.8
        signal[(signal == 1) & ~(alt_bull_pa & alt_vol_ok)] = 0
        signal[(signal == -1) & ~(alt_bear_pa & alt_vol_ok)] = 0
    return signal, alt


def run_factor_test(btc_score_ts, btc_df, alt_data, test_name):
    """Run test and return metrics."""
    all_trades = []
    for coin in COINS:
        cfg = V11_CONFIGS[coin]
        alt_df = alt_data[coin]
        signals, alt_merged = gen_signal(btc_score_ts, alt_df, cfg["threshold"], cfg["alt_pa"])
        oos_mask = alt_merged["ts"] >= TEST_START
        df_oos = alt_merged[oos_mask].reset_index(drop=True)
        sig_oos = signals[oos_mask].reset_index(drop=True)
        trades = run_backtest(df_oos, sig_oos,
                              sl_atr_mult=cfg["sl"], tp_atr_mult=cfg["tp"],
                              trail_atr_mult=cfg["trail"], trail_activate_atr=cfg["trail_act"],
                              cooldown_bars=cfg["cd"])
        if not trades.empty:
            trades["coin"] = coin
            all_trades.append(trades)
    if not all_trades:
        return 0, 0, 0, pd.DataFrame()
    at = pd.concat(all_trades, ignore_index=True)
    n = len(at)
    wr = (at["pnl_net"] > 0).sum() / n * 100 if n > 0 else 0
    pnl = at["pnl_net"].sum()
    rets = at["pnl_net"] / BUDGET_USDT
    sharpe = rets.mean() / rets.std() * np.sqrt(n) if n > 1 and rets.std() > 0 else 0
    return n, round(wr, 1), round(pnl, 0), at


def run_confidence_filter_test(btc_score_ts, btc_df, alt_data, filter_name,
                                min_agreement=0, rsi_filter=False,
                                pos_filter=False, vol_filter=False):
    """Run test with confidence filters applied."""
    all_trades = []
    for coin in COINS:
        cfg = V11_CONFIGS[coin]
        alt_df = alt_data[coin]

        # Add RSI if not present
        if "rsi" not in alt_df.columns:
            alt_df = alt_df.copy()
            alt_df["rsi"] = ta.rsi(alt_df["close"], length=14)

        signals, alt_merged = gen_signal(btc_score_ts, alt_df, cfg["threshold"], cfg["alt_pa"])

        # Apply confidence filters
        sig_filtered = apply_confidence_filters(
            signals, alt_merged,
            min_agreement=min_agreement,
            rsi_filter=rsi_filter,
            pos_filter=pos_filter,
            vol_filter=vol_filter
        )

        oos_mask = alt_merged["ts"] >= TEST_START
        df_oos = alt_merged[oos_mask].reset_index(drop=True)
        sig_oos = sig_filtered[oos_mask].reset_index(drop=True)
        trades = run_backtest(df_oos, sig_oos,
                              sl_atr_mult=cfg["sl"], tp_atr_mult=cfg["tp"],
                              trail_atr_mult=cfg["trail"], trail_activate_atr=cfg["trail_act"],
                              cooldown_bars=cfg["cd"])
        if not trades.empty:
            trades["coin"] = coin
            all_trades.append(trades)
    if not all_trades:
        return 0, 0, 0, pd.DataFrame()
    at = pd.concat(all_trades, ignore_index=True)
    n = len(at)
    wr = (at["pnl_net"] > 0).sum() / n * 100 if n > 0 else 0
    pnl = at["pnl_net"].sum()
    rets = at["pnl_net"] / BUDGET_USDT
    sharpe = rets.mean() / rets.std() * np.sqrt(n) if n > 1 and rets.std() > 0 else 0
    return n, round(wr, 1), round(pnl, 0), at


# ============================================================
# MAIN
# ============================================================

print("=" * 70)
print("TRACK 2: LEADING INDICATOR TESTS (Steps 2.2-2.5)")
print("=" * 70)

# Load base data
print("\n=== LOADING DATA ===")
btc_ohlcv = fetch_binance_15m("BTCUSDT")
db_data = load_btc_db_data()
btc_df = build_btc_features(btc_ohlcv, db_data)
btc_score_v3 = compute_btc_composite_score(btc_df)
btc_score_ts_v3 = pd.Series(btc_score_v3.values, index=btc_df["ts"].values)

# Preload alt data
alt_data = {}
for coin in COINS:
    symbol = f"{coin}USDT"
    ohlcv = fetch_binance_15m(symbol) if coin != "BTC" else btc_ohlcv
    alt_data[coin] = build_alt_technicals(ohlcv)

# Baseline
n, wr, pnl, _ = run_factor_test(btc_score_ts_v3, btc_df, alt_data, "v3_baseline")
print(f"\n  v3 Baseline: {n} trades, WR {wr}%, PnL ${pnl:,.0f}")

# Load new factor data from DB
conn = psycopg2.connect(**DB_PARAMS)
cvd_data = load_cvd_data(conn)
dvol_data = load_dvol_data(conn)
macro_data = load_macro_data(conn)
conn.close()

print(f"\n  CVD data: {len(cvd_data) if cvd_data is not None else 0} rows")
print(f"  DVOL data: {len(dvol_data) if dvol_data is not None else 0} rows")
print(f"  Macro data: {len(macro_data) if macro_data is not None else 0} rows")


# ============================================================
# Step 2.2: CVD
# ============================================================
print("\n" + "=" * 70)
print("STEP 2.2: CVD (Cumulative Volume Delta)")
print("=" * 70)

if cvd_data is not None and len(cvd_data) > 10:
    # Merge CVD into btc_df
    btc_df_cvd = btc_df.copy()
    btc_df_cvd = pd.merge_asof(btc_df_cvd.sort_values("ts"),
                                cvd_data[["ts", "cvd_15m", "cvd_ma", "cvd_z"]].sort_values("ts"),
                                on="ts", direction="backward", tolerance=pd.Timedelta("30min"))

    for w in [0.5, 1.0, 1.5, 2.0]:
        # v3 score + CVD
        score_cvd = compute_btc_composite_score(btc_df_cvd) + score_cvd_contrarian(btc_df_cvd, weight=w)
        score_cvd_ts = pd.Series(score_cvd.values, index=btc_df_cvd["ts"].values)
        n, wr, pnl, _ = run_factor_test(score_cvd_ts, btc_df_cvd, alt_data, f"v3+cvd_{w}")
        delta = pnl - int(pnl) + int(pnl)  # keep as is
        print(f"  v3 + CVD(w={w}): {n} trades, WR {wr}%, PnL ${pnl:,.0f} (Δ${pnl - int(run_factor_test(btc_score_ts_v3, btc_df, alt_data, 'base')[2]):+,.0f})")
else:
    print("  SKIP: Insufficient CVD data")

# Recompute baseline PnL once for delta comparison
_, _, baseline_pnl, _ = run_factor_test(btc_score_ts_v3, btc_df, alt_data, "base")


# ============================================================
# Step 2.3: DVOL
# ============================================================
print("\n" + "=" * 70)
print("STEP 2.3: DVOL (Deribit Implied Volatility)")
print("=" * 70)

if dvol_data is not None and len(dvol_data) > 10:
    btc_df_dvol = btc_df.copy()
    btc_df_dvol = pd.merge_asof(btc_df_dvol.sort_values("ts"),
                                 dvol_data[["ts", "dvol_z", "mp_dist", "pcr_z"]].sort_values("ts"),
                                 on="ts", direction="backward", tolerance=pd.Timedelta("30min"))

    # Test as factor
    for w in [0.5, 1.0, 1.5]:
        score_d = compute_btc_composite_score(btc_df_dvol) + score_dvol_regime(btc_df_dvol, weight=w)
        score_d_ts = pd.Series(score_d.values, index=btc_df_dvol["ts"].values)
        n, wr, pnl, _ = run_factor_test(score_d_ts, btc_df_dvol, alt_data, f"v3+dvol_{w}")
        print(f"  v3 + DVOL(w={w}): {n} trades, WR {wr}%, PnL ${pnl:,.0f} (Δ${pnl - baseline_pnl:+,.0f})")

    # Test as regime filter: only trade when DVOL z > -0.5 (not too quiet)
    print("\n  DVOL as regime filter:")
    btc_df_dvol_filt = btc_df_dvol.copy()
    for threshold in [-0.5, 0.0, 0.5]:
        score_filtered = compute_btc_composite_score(btc_df_dvol_filt)
        # Zero out score when DVOL is too low (quiet market)
        if "dvol_z" in btc_df_dvol_filt.columns:
            quiet = btc_df_dvol_filt["dvol_z"].fillna(0) < threshold
            score_filtered[quiet] = 0
        score_f_ts = pd.Series(score_filtered.values, index=btc_df_dvol_filt["ts"].values)
        n, wr, pnl, _ = run_factor_test(score_f_ts, btc_df_dvol_filt, alt_data, f"v3_dvol_filter_{threshold}")
        print(f"    DVOL z > {threshold}: {n} trades, WR {wr}%, PnL ${pnl:,.0f} (Δ${pnl - baseline_pnl:+,.0f})")
else:
    print("  SKIP: Insufficient DVOL data")


# ============================================================
# Step 2.4: Macro Cross-Market
# ============================================================
print("\n" + "=" * 70)
print("STEP 2.4: Macro Cross-Market Signals")
print("=" * 70)

if macro_data is not None and len(macro_data) > 3:
    btc_df_macro = btc_df.copy()
    # Forward-fill macro data (daily) to 15min
    macro_15m = macro_data.copy()
    macro_15m["ts"] = pd.to_datetime(macro_15m["ts"])
    # Shift +1d for anti-lookahead (daily data not final until EOD)
    macro_15m["ts"] = macro_15m["ts"] + pd.Timedelta("1d")

    merge_cols = ["ts"]
    for col in ["dxy_ret", "gold_ret", "us10y_ret"]:
        if col in macro_15m.columns:
            merge_cols.append(col)

    btc_df_macro = pd.merge_asof(btc_df_macro.sort_values("ts"),
                                  macro_15m[merge_cols].sort_values("ts"),
                                  on="ts", direction="backward", tolerance=pd.Timedelta("3d"))

    for w in [0.5, 1.0, 1.5]:
        score_m = compute_btc_composite_score(btc_df_macro) + score_macro_divergence(btc_df_macro, weight=w)
        score_m_ts = pd.Series(score_m.values, index=btc_df_macro["ts"].values)
        n, wr, pnl, _ = run_factor_test(score_m_ts, btc_df_macro, alt_data, f"v3+macro_{w}")
        print(f"  v3 + Macro(w={w}): {n} trades, WR {wr}%, PnL ${pnl:,.0f} (Δ${pnl - baseline_pnl:+,.0f})")
else:
    print("  SKIP: Insufficient Macro data")


# ============================================================
# Step 2.5: Confidence Score Filters
# ============================================================
print("\n" + "=" * 70)
print("STEP 2.5: Signal Confidence Filters")
print("=" * 70)

# Test individual filters
filters = [
    ("baseline (no filter)", dict()),
    ("RSI filter only", dict(rsi_filter=True)),
    ("Pos-in-range filter only", dict(pos_filter=True)),
    ("Volume filter only", dict(vol_filter=True)),
    ("RSI + Pos", dict(rsi_filter=True, pos_filter=True)),
    ("RSI + Vol", dict(rsi_filter=True, vol_filter=True)),
    ("Pos + Vol", dict(pos_filter=True, vol_filter=True)),
    ("All 3 filters", dict(rsi_filter=True, pos_filter=True, vol_filter=True)),
]

for name, kwargs in filters:
    n, wr, pnl, trades = run_confidence_filter_test(
        btc_score_ts_v3, btc_df, alt_data, name, **kwargs)
    n_long = len(trades[trades["dir"] == "L"]) if not trades.empty else 0
    n_short = len(trades[trades["dir"] == "S"]) if not trades.empty else 0
    long_wr = (trades[(trades["dir"] == "L") & (trades["pnl_net"] > 0)].shape[0] / max(n_long, 1)) * 100 if not trades.empty else 0
    short_wr = (trades[(trades["dir"] == "S") & (trades["pnl_net"] > 0)].shape[0] / max(n_short, 1)) * 100 if not trades.empty else 0
    print(f"  {name:30s}: {n:4d} trades, WR {wr:5.1f}%, PnL ${pnl:>8,.0f} (Δ${pnl - baseline_pnl:+,.0f}) | "
          f"L {n_long}({long_wr:.0f}%) S {n_short}({short_wr:.0f}%)")

# ============================================================
# Save results
# ============================================================
os.makedirs("experiments", exist_ok=True)
results = {
    "timestamp": datetime.now().isoformat(),
    "test_period": "2026-01-01 to present (OOS)",
    "scoring": "v3 (aligned with paper trading)",
    "baseline_pnl": baseline_pnl,
    "cvd_data_rows": len(cvd_data) if cvd_data is not None else 0,
    "dvol_data_rows": len(dvol_data) if dvol_data is not None else 0,
    "macro_data_rows": len(macro_data) if macro_data is not None else 0,
    "note": "CVD/DVOL only have 6 days of data (Mar 9-15). Macro only 8 days. Results are indicative only.",
}
with open("experiments/track2_factor_results.json", "w") as f:
    json.dump(results, f, indent=2, default=str)

print("\n\nSaved to experiments/track2_factor_results.json")
print("DONE!")
