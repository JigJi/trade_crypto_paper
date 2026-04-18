"""
Signal Core — Single Source of Truth
======================================
Canonical signal computation shared by backtest and paper trading.

All signal-related functions live HERE. Both backtest_15m_btc_led_alts.py
and paper_trading/strategy.py import from this module.

Rule: NEVER copy signal logic. If you need to change scoring, thresholds,
or features — change it HERE and both systems update automatically.

Created: 2026-03-23 (extracted from backtest + strategy.py)
"""

import numpy as np
import pandas as pd
import pandas_ta as ta


# ══════════════════════════════════════════════════════════════
# Default Constants
# ══════════════════════════════════════════════════════════════

DEFAULT_COMPOSITE_WEIGHTS = {
    # OI divergence (weight 0.5 -> sub-weights 0.25 each)
    "w_oi_bull": 0.25, "w_oi_capit": 0.25, "w_oi_weak": 0.25, "w_oi_bear": 0.25,
    # Funding rate (weight 2.0)
    "w_fr_neg": 2.0, "w_fr_pos": 2.0,
    # Whale alerts (weight 1.5)
    "w_whale_bull": 1.5, "w_whale_bear": 1.5,
    # Liquidation cascades (weight 2.0)
    "w_liq_bull": 2.0, "w_liq_bear": 2.0,
    # ETF flows (weight 1.0)
    "w_etf_bull": 1.0, "w_etf_bear": 1.0,
}

DEFAULT_EXTRA_WEIGHTS = {
    "ob_combined": 2.0,
    "basis_contrarian": 1.5,
    "tick_liq": 2.0,
}

DEFAULT_V6_CASCADE_MULT = 1.1
DEFAULT_V6_LIQ_WEIGHT = 8.0
DEFAULT_V6_TICK_WEIGHT = 8.0
DEFAULT_V6_TICK_NET_THRESHOLD = 3

DEFAULT_SPIKE_CONFIG = {
    "range_z_thr": 1.5,
    "vol_ratio_thr": 2.0,
    "liq_mult": 3.0,
    "liq_mult_extreme": 5.0,
    "displacement_thr": 2.0,
    "rsi_high": 75,
    "rsi_low": 25,
    "contrarian_reduction": 0.5,
    "momentum_reduction": 0.8,
}

# Dead zone hours (UTC)
DEAD_ZONE_START = 23
DEAD_ZONE_END = 6


# ══════════════════════════════════════════════════════════════
# Resampling
# ══════════════════════════════════════════════════════════════

def resample_to_15m(df, ts_col, value_cols, agg="last"):
    """Resample irregular time series to 15-minute intervals."""
    d = df.set_index(ts_col).sort_index()
    if agg == "last":
        return d[value_cols].resample("15min").last().dropna(how="all").reset_index()
    elif agg == "sum":
        return d[value_cols].resample("15min").sum().reset_index()
    return d[value_cols].resample("15min").last().dropna(how="all").reset_index()


# ══════════════════════════════════════════════════════════════
# Feature Building
# ══════════════════════════════════════════════════════════════

def build_btc_features(ohlcv, db_data):
    """Build BTC features with 8 factors for composite scoring.
    Input ohlcv must have columns: date_time, open, high, low, close, volume"""
    df = ohlcv.copy().rename(columns={"date_time": "ts"})

    # BTC technicals (needed for composite signal PA filter)
    df["ema9"] = df["close"].ewm(span=9, adjust=False).mean()
    df["ema21"] = df["close"].ewm(span=21, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
    df["rsi"] = ta.rsi(df["close"], length=14)
    df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=14)
    df["vol_ma"] = df["volume"].rolling(20).mean()
    df["vol_ratio"] = df["volume"] / df["vol_ma"]
    df["ret"] = df["close"].pct_change()

    # 1H indicators
    df_1h = df.set_index("ts")[["open", "high", "low", "close"]].resample("1h").agg({
        "open": "first", "high": "max", "low": "min", "close": "last"
    }).dropna()
    df_1h["ema9_1h"] = df_1h["close"].ewm(span=9, adjust=False).mean()
    df_1h["ema21_1h"] = df_1h["close"].ewm(span=21, adjust=False).mean()
    df = pd.merge_asof(df.sort_values("ts"),
                       df_1h[["ema9_1h", "ema21_1h"]].reset_index().sort_values("ts"),
                       on="ts", direction="backward", tolerance=pd.Timedelta("2h"))

    # DB features
    if "oi" in db_data and len(db_data["oi"]) > 0:
        oi = resample_to_15m(db_data["oi"], "ts", ["oi_usdt"])
        oi["oi_chg"] = oi["oi_usdt"].pct_change()
        df = pd.merge_asof(df.sort_values("ts"), oi.sort_values("ts"),
                           on="ts", direction="backward", tolerance=pd.Timedelta("30min"))

    if "premium" in db_data and len(db_data["premium"]) > 0:
        prem = resample_to_15m(db_data["premium"], "ts", ["last_funding_rate", "premium"])
        df = pd.merge_asof(df.sort_values("ts"), prem.sort_values("ts"),
                           on="ts", direction="backward", tolerance=pd.Timedelta("30min"))

    if "whale" in db_data and len(db_data["whale"]) > 0:
        whale = db_data["whale"].copy()
        whale["bull_val"] = np.where(whale["sentiment"] == "bullish", whale["usd_value"], 0)
        whale["bear_val"] = np.where(whale["sentiment"] == "bearish", whale["usd_value"], 0)
        whale_agg = whale.set_index("ts").resample("15min").agg({"bull_val": "sum", "bear_val": "sum"}).reset_index()
        whale_agg["whale_net"] = whale_agg["bull_val"] - whale_agg["bear_val"]
        whale_agg["whale_net_ma"] = whale_agg["whale_net"].rolling(8).mean()
        df = pd.merge_asof(df.sort_values("ts"), whale_agg.sort_values("ts"),
                           on="ts", direction="backward", tolerance=pd.Timedelta("30min"))

    if "liq" in db_data and len(db_data["liq"]) > 0:
        liq = db_data["liq"].copy()
        liq["liq_net"] = liq["liq_short_1h"] - liq["liq_long_1h"]
        liq["liq_total"] = liq["liq_long_1h"] + liq["liq_short_1h"]
        liq["liq_total_ma"] = liq["liq_total"].rolling(24).mean()
        df = pd.merge_asof(df.sort_values("ts"), liq.sort_values("ts"),
                           on="ts", direction="backward", tolerance=pd.Timedelta("2h"))

    if "etf" in db_data and len(db_data["etf"]) > 0:
        etf = db_data["etf"].copy()
        etf["etf_flow_ma"] = etf["etf_flow"].rolling(5).mean()
        df = pd.merge_asof(df.sort_values("ts"), etf.sort_values("ts"),
                           on="ts", direction="backward", tolerance=pd.Timedelta("3d"))

    if "funding" in db_data and len(db_data["funding"]) > 0:
        fr = db_data["funding"].copy()
        fr["fr_ma"] = fr["fr_8h"].rolling(3).mean()
        df = pd.merge_asof(df.sort_values("ts"), fr.sort_values("ts"),
                           on="ts", direction="backward", tolerance=pd.Timedelta("12h"))

    # ---- v3 new factors ----

    # Basis rate (futures premium z-score)
    if "basis" in db_data and len(db_data["basis"]) > 0:
        basis = resample_to_15m(db_data["basis"], "ts", ["basis_rate"])
        basis["basis_z"] = (
            (basis["basis_rate"] - basis["basis_rate"].rolling(96).mean())
            / basis["basis_rate"].rolling(96).std().clip(lower=1e-8)
        )
        df = pd.merge_asof(df.sort_values("ts"), basis[["ts", "basis_z"]].sort_values("ts"),
                           on="ts", direction="backward", tolerance=pd.Timedelta("30min"))

    # Tick-level liquidations (aggregated to 15min)
    if "tick_liq" in db_data and len(db_data["tick_liq"]) > 0:
        tliq = db_data["tick_liq"].copy()
        tliq["is_sell"] = (tliq["side"] == "SELL").astype(float)
        tliq["is_buy"] = (tliq["side"] == "BUY").astype(float)
        agg = tliq.set_index("ts").resample("15min").agg({
            "notional_usd": "sum", "is_sell": "sum", "is_buy": "sum",
        }).fillna(0).reset_index()
        agg.columns = ["ts", "liq_notional", "liq_long_count", "liq_short_count"]
        agg["liq_net_count"] = agg["liq_short_count"] - agg["liq_long_count"]
        agg["liq_notional_ma"] = agg["liq_notional"].rolling(16).mean()
        agg["liq_net_ma"] = agg["liq_net_count"].rolling(16).mean()
        df = pd.merge_asof(df.sort_values("ts"),
                           agg[["ts", "liq_net_ma", "liq_notional_ma"]].sort_values("ts"),
                           on="ts", direction="backward", tolerance=pd.Timedelta("30min"))

    # Order book imbalance
    if "ob" in db_data and len(db_data["ob"]) > 0:
        ob = db_data["ob"].copy()
        r = ob.set_index("ts").resample("15min").agg({
            "imbalance": "mean", "bid_sum": "mean", "ask_sum": "mean",
        }).dropna(how="all").reset_index()
        r["ob_imb_ma"] = r["imbalance"].rolling(12).mean()
        r["ob_vol_imb"] = (r["bid_sum"] - r["ask_sum"]) / (r["bid_sum"] + r["ask_sum"]).clip(lower=1e-8)
        r["ob_vol_imb_ma"] = r["ob_vol_imb"].rolling(12).mean()
        df = pd.merge_asof(df.sort_values("ts"),
                           r[["ts", "ob_imb_ma", "ob_vol_imb_ma"]].sort_values("ts"),
                           on="ts", direction="backward", tolerance=pd.Timedelta("30min"))

    # ---- Volatility spike features ----
    df["intrabar_range"] = (df["high"] - df["low"]) / df["close"].clip(lower=1e-8)
    _range_ma = df["intrabar_range"].rolling(96).mean()
    _range_std = df["intrabar_range"].rolling(96).std().clip(lower=1e-8)
    df["range_z"] = (df["intrabar_range"] - _range_ma) / _range_std
    df["ema21_dist"] = (df["close"] - df["ema21"]) / df["atr"].clip(lower=1e-8)

    return df.sort_values("ts").reset_index(drop=True)


def build_alt_technicals(ohlcv):
    """Build altcoin technical indicators for PA filter and ATR-based SL/TP."""
    df = ohlcv.copy().rename(columns={"date_time": "ts"})
    df["ema9"] = df["close"].ewm(span=9, adjust=False).mean()
    df["ema21"] = df["close"].ewm(span=21, adjust=False).mean()
    df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=14)
    df["vol_ma"] = df["volume"].rolling(20).mean()
    df["vol_ratio"] = df["volume"] / df["vol_ma"]
    return df


# ══════════════════════════════════════════════════════════════
# Score Helper Functions (v3)
# ══════════════════════════════════════════════════════════════

def score_basis_contrarian(df, weight=1.5):
    """Contrarian basis score: high basis = bearish, low basis = bullish."""
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
    """Tick liquidation score: net short liqs = bullish, net long liqs = bearish."""
    s = pd.Series(0.0, index=df.index)
    if "liq_net_ma" not in df.columns:
        return s
    ln = df["liq_net_ma"].fillna(0)
    lt = df["liq_notional_ma"].fillna(0)
    s += np.where(ln > 2, weight, 0)
    s += np.where(ln < -2, -weight, 0)
    lt_expanding_mean = lt.where(lt > 0).expanding().mean().fillna(1)
    s += np.where(lt > lt_expanding_mean * 3, weight * 0.5, 0)
    return s


def score_ob_combined(df, weight=2.0):
    """Combined order book score: contrarian on imbalance + volume imbalance."""
    s = pd.Series(0.0, index=df.index)
    if "ob_imb_ma" not in df.columns:
        return s
    combo = (df["ob_imb_ma"].fillna(0) + df["ob_vol_imb_ma"].fillna(0)) / 2
    s += np.where(combo > 0.03, -weight, 0)
    s += np.where(combo > 0.07, -weight * 0.5, 0)
    s += np.where(combo < -0.03, weight, 0)
    s += np.where(combo < -0.07, weight * 0.5, 0)
    return s


# ══════════════════════════════════════════════════════════════
# Composite Score Functions
# ══════════════════════════════════════════════════════════════

def compute_btc_composite_score(df, params=None, extra=None):
    """
    Compute BTC composite score from 8 v3 factors.
    Kept: OI divergence, funding rate, whale alerts, liquidation cascades, ETF flows
    New: basis_contrarian, tick_liq, ob_combined
    Removed: taker_ratio, ls_ratio, fear_greed
    """
    if params is None:
        params = DEFAULT_COMPOSITE_WEIGHTS
    if extra is None:
        extra = DEFAULT_EXTRA_WEIGHTS
    score = pd.Series(0.0, index=df.index)

    # OI divergence (weight 0.5 -> sub-weights 0.25 each)
    if "oi_chg" in df.columns:
        oi_chg = df["oi_chg"].fillna(0)
        ret = df["ret"].fillna(0)
        score += np.where((ret > 0.001) & (oi_chg > 0.002), params.get("w_oi_bull", 0.25), 0)
        score += np.where((ret < -0.001) & (oi_chg < -0.002), params.get("w_oi_capit", 0.25), 0)
        score += np.where((ret > 0.001) & (oi_chg < -0.002), -params.get("w_oi_weak", 0.25), 0)
        score += np.where((ret < -0.001) & (oi_chg > 0.002), -params.get("w_oi_bear", 0.25), 0)

    # Funding rate (weight 2.0)
    if "fr_8h" in df.columns:
        fr = df["fr_8h"].fillna(0)
        score += np.where(fr < -0.0001, params.get("w_fr_neg", 2.0), 0)
        score += np.where(fr > 0.0003, -params.get("w_fr_pos", 2.0), 0)
    elif "last_funding_rate" in df.columns:
        fr = df["last_funding_rate"].fillna(0)
        score += np.where(fr < -0.00005, params.get("w_fr_neg", 2.0), 0)
        score += np.where(fr > 0.0002, -params.get("w_fr_pos", 2.0), 0)

    # Whale alerts (weight 1.5)
    if "whale_net_ma" in df.columns:
        wn_ma = df["whale_net_ma"].fillna(0)
        score += np.where(wn_ma > 50_000_000, params.get("w_whale_bull", 1.5), 0)
        score += np.where(wn_ma < -50_000_000, -params.get("w_whale_bear", 1.5), 0)

    # Liquidation cascades (weight 2.0)
    if "liq_net" in df.columns and "liq_total_ma" in df.columns:
        lt = df["liq_total"].fillna(0)
        lt_ma = df["liq_total_ma"].fillna(1)
        ln = df["liq_net"].fillna(0)
        cascade = lt > (lt_ma * 3)
        score += np.where(cascade & (ln > 0), params.get("w_liq_bull", 2.0), 0)
        score += np.where(cascade & (ln < 0), -params.get("w_liq_bear", 2.0), 0)

    # ETF flows (weight 1.0)
    if "etf_flow_ma" in df.columns:
        etf_ma = df["etf_flow_ma"].fillna(0)
        score += np.where(etf_ma > 50, params.get("w_etf_bull", 1.0), 0)
        score += np.where(etf_ma < -50, -params.get("w_etf_bear", 1.0), 0)

    # v3 new factors
    score += score_basis_contrarian(df, weight=extra.get("basis_contrarian", 1.5))
    score += score_tick_liq(df, weight=extra.get("tick_liq", 2.0))
    score += score_ob_combined(df, weight=extra.get("ob_combined", 2.0))

    return score


def compute_btc_composite_score_v6(df, cascade_mult=DEFAULT_V6_CASCADE_MULT,
                                    liq_w=DEFAULT_V6_LIQ_WEIGHT,
                                    tick_w=DEFAULT_V6_TICK_WEIGHT,
                                    tick_net_thr=DEFAULT_V6_TICK_NET_THRESHOLD,
                                    velocity_w=0.0, velocity_lb=4):
    """
    V6 Liq-Only composite score: cascade + tick + optional velocity.
    Tournament R2 champion: $69,701 conservative, $71,802 aggressive.
    """
    score = pd.Series(0.0, index=df.index)

    # Liquidation cascade (1.1x MA threshold)
    if "liq_net" in df.columns and "liq_total" in df.columns:
        lt = df["liq_total"].fillna(0)
        ln = df["liq_net"].fillna(0)
        lt_ma = df.get("liq_total_ma", lt.rolling(24).mean()).fillna(1)
        c = lt > (lt_ma * cascade_mult)
        score += np.where(c & (ln > 0), liq_w, 0)
        score += np.where(c & (ln < 0), -liq_w, 0)

    # Tick liquidation (net > threshold)
    if "liq_net_ma" in df.columns:
        ln_tick = df["liq_net_ma"].fillna(0)
        score += np.where(ln_tick > tick_net_thr, tick_w, 0)
        score += np.where(ln_tick < -tick_net_thr, -tick_w, 0)

    # Optional: velocity (acceleration of liq volume)
    if velocity_w > 0 and "liq_total" in df.columns:
        lt = df["liq_total"].fillna(0)
        vel = lt.pct_change(velocity_lb).fillna(0)
        ln = df["liq_net"].fillna(0)
        acc = vel > 1.0
        score += np.where(acc & (ln > 0), velocity_w, 0)
        score += np.where(acc & (ln < 0), -velocity_w, 0)
        dec = vel < -0.5
        score += np.where(dec & (ln > 0), velocity_w * 0.3, 0)
        score += np.where(dec & (ln < 0), -velocity_w * 0.3, 0)

    return score


# ══════════════════════════════════════════════════════════════
# Raw Signal Logic
# ══════════════════════════════════════════════════════════════

def compute_raw_signal(score, threshold, prev_signal=0, hysteresis_band=0.0):
    """
    Core signal logic — SAME code for backtest (hysteresis=0) and paper (hysteresis=1.5).

    Args:
        score: float, current BTC composite score
        threshold: float, entry threshold (e.g. 3.0)
        prev_signal: int, previous signal state (0, 1, -1)
        hysteresis_band: float, exit threshold reduction (0=backtest, 1.5=paper)

    Returns:
        int: signal (1=LONG, -1=SHORT, 0=NEUTRAL)
    """
    exit_threshold = max(threshold - hysteresis_band, 0.0)

    if prev_signal == 0:
        # Not in signal -- use entry threshold
        if score >= threshold:
            return 1
        elif score <= -threshold:
            return -1
        return 0
    elif prev_signal == 1:
        # Was LONG -- use exit threshold to stay, entry threshold to flip
        if score >= exit_threshold:
            return 1
        elif score <= -threshold:
            return -1
        return 0
    else:  # prev_signal == -1
        # Was SHORT -- use exit threshold to stay, entry threshold to flip
        if score <= -exit_threshold:
            return -1
        elif score >= threshold:
            return 1
        return 0


def is_dead_zone(hour):
    """Check if hour (UTC) is in dead zone (23:00-06:00)."""
    return hour >= DEAD_ZONE_START or hour < DEAD_ZONE_END


def check_pa_alignment(alt_latest, raw_signal, use_alt_pa_filter):
    """
    Check price action alignment for a single bar.
    Returns (pa_aligned, should_suppress).
    pa_aligned: int or None
    should_suppress: bool (True means signal should be zeroed)
    """
    if not use_alt_pa_filter:
        return None, False

    if raw_signal == 1:
        pa_ok = (alt_latest["close"] > alt_latest["ema9"] and
                 alt_latest["ema9"] > alt_latest["ema21"])
    else:
        pa_ok = (alt_latest["close"] < alt_latest["ema9"] and
                 alt_latest["ema9"] < alt_latest["ema21"])

    vol_ok = True
    vr = alt_latest.get("vol_ratio")
    if vr is not None and not pd.isna(vr):
        vol_ok = vr > 0.8

    pa_aligned = int(pa_ok and vol_ok)
    return pa_aligned, not pa_aligned


# ══════════════════════════════════════════════════════════════
# Volatility Spike Detection (single-bar, for paper trading)
# ══════════════════════════════════════════════════════════════

def detect_spike_bar(btc_latest, spike_config=None):
    """Check if the latest BTC bar is a volatility spike. Single-bar version."""
    cfg = spike_config or DEFAULT_SPIKE_CONFIG
    if btc_latest.get("range_z", 0) > cfg["range_z_thr"]:
        return True
    if btc_latest.get("vol_ratio", 0) > cfg["vol_ratio_thr"]:
        return True
    lt = btc_latest.get("liq_total", 0)
    lt_ma = btc_latest.get("liq_total_ma", 1)
    if pd.notna(lt) and pd.notna(lt_ma) and lt_ma > 0 and lt > lt_ma * cfg["liq_mult"]:
        return True
    return False


def classify_spike_bar(btc_latest, spike_config=None):
    """Classify spike bar as 'contrarian' or 'momentum'. Single-bar version."""
    cfg = spike_config or DEFAULT_SPIKE_CONFIG
    # Extreme liquidation
    lt = btc_latest.get("liq_total", 0)
    lt_ma = btc_latest.get("liq_total_ma", 1)
    if pd.notna(lt) and pd.notna(lt_ma) and lt_ma > 0 and lt > lt_ma * cfg["liq_mult_extreme"]:
        return "contrarian"
    # Extreme displacement
    if abs(btc_latest.get("ema21_dist", 0)) > cfg["displacement_thr"]:
        return "contrarian"
    # Extreme RSI
    rsi = btc_latest.get("rsi", 50)
    if pd.notna(rsi) and (rsi > cfg["rsi_high"] or rsi < cfg["rsi_low"]):
        return "contrarian"
    return "momentum"


# ══════════════════════════════════════════════════════════════
# Vectorized Spike Detection (for backtest)
# ══════════════════════════════════════════════════════════════

def detect_spike(df, spike_config=None):
    """Boolean Series: True when volatility spike (range_z OR vol_ratio OR liq_cascade)."""
    cfg = spike_config or DEFAULT_SPIKE_CONFIG
    spike = pd.Series(False, index=df.index)
    if "range_z" in df.columns:
        spike = spike | (df["range_z"] > cfg["range_z_thr"])
    if "vol_ratio" in df.columns:
        spike = spike | (df["vol_ratio"] > cfg["vol_ratio_thr"])
    if "liq_total" in df.columns and "liq_total_ma" in df.columns:
        liq_ok = df["liq_total"].notna() & df["liq_total_ma"].notna()
        spike = spike | (liq_ok & (df["liq_total"] > df["liq_total_ma"] * cfg["liq_mult"]))
    return spike


def classify_spike_mode(df, spike_config=None):
    """Classify spike bars: 'contrarian' (overextended) or 'momentum'. Vectorized."""
    cfg = spike_config or DEFAULT_SPIKE_CONFIG
    is_contrarian = pd.Series(False, index=df.index)
    if "liq_total" in df.columns and "liq_total_ma" in df.columns:
        liq_ok = df["liq_total"].notna() & df["liq_total_ma"].notna()
        is_contrarian = is_contrarian | (liq_ok & (df["liq_total"] > df["liq_total_ma"] * cfg["liq_mult_extreme"]))
    if "ema21_dist" in df.columns:
        is_contrarian = is_contrarian | (df["ema21_dist"].abs() > cfg["displacement_thr"])
    if "rsi" in df.columns:
        rsi = df["rsi"].fillna(50)
        is_contrarian = is_contrarian | (rsi > cfg["rsi_high"]) | (rsi < cfg["rsi_low"])
    mode = pd.Series("momentum", index=df.index)
    mode[is_contrarian] = "contrarian"
    return mode
