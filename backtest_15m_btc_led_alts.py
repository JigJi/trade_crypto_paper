"""
BTC-Led Altcoin 15m Backtest (DOGE, XRP, ADA, AVAX, LINK)
==========================================================
Uses BTC composite signal (8 factors) as market leader to trade altcoins.

Hypothesis: BTC is the market leader -- when BTC sentiment is bullish,
altcoins follow with higher beta (higher volatility).

Data flow:
  BTC DB Data (8 factors) -> BTC Composite Score -> Signal
                                                      |
  Altcoin OHLCV (Binance) -> Altcoin Technicals -> Entry Filter (optional)
                                                      |
                                                 Trade on Altcoin price
                                                 SL/TP based on Altcoin ATR

Walk-forward: Train Jun-Nov 2025, Test Dec 2025 - Mar 2026 (4-month OOS)

Enhancements v2:
  - Dead zone filter (23:00-06:00 UTC suppressed)
  - Regime filter (soft penalty based on BTC EMA50)
  - Leverage 2x + Half-Kelly dynamic sizing
  - Portfolio simulation (max 3 concurrent positions)
"""

import os, sys, warnings, time as _time
from datetime import datetime

import numpy as np
import pandas as pd
import pandas_ta as ta
import psycopg2
import requests
from dotenv import load_dotenv

warnings.filterwarnings("ignore")
load_dotenv()

INIT_EQUITY = 10_000.0
BUDGET_USDT = 1_000.0
LEVERAGE = 2.0
FEE_BPS = 2.0
SLIP_BPS = 1.5
FEE = FEE_BPS / 10_000
SLIP = SLIP_BPS / 10_000

MAX_CONCURRENT = 3
DEAD_ZONE_START = 23  # UTC hour
DEAD_ZONE_END = 6     # UTC hour

# DB timestamps are stored in Bangkok local time (UTC+7).
# Binance API returns UTC timestamps. We must align them.
BKK_UTC_OFFSET = pd.Timedelta("7h")

DB_PARAMS = {
    "dbname": os.getenv("PG_DB", "smart_trading"),
    "user": os.getenv("PG_USER", "postgres"),
    "password": os.getenv("PG_PASS", "P@ssw0rd"),
    "host": os.getenv("PG_HOST", "localhost"),
    "port": os.getenv("PG_PORT", "5432"),
}

ALT_COINS = ["BTC", "XRP", "ADA", "DOT", "SUI", "FIL"]

# ══════════════════════════════════════════════════════════════
# Signal Core — Single Source of Truth
# All signal functions imported from signal_core.py
# ══════════════════════════════════════════════════════════════
from signal_core import (
    resample_to_15m, build_btc_features, build_alt_technicals,
    score_basis_contrarian, score_tick_liq, score_ob_combined,
    compute_btc_composite_score,
    compute_raw_signal, is_dead_zone, check_pa_alignment,
    detect_spike, classify_spike_mode,
    DEFAULT_COMPOSITE_WEIGHTS, DEFAULT_EXTRA_WEIGHTS, DEFAULT_SPIKE_CONFIG,
    DEAD_ZONE_START, DEAD_ZONE_END,
)

# Backward compat aliases (40+ files import these names from here)
COMPOSITE_WEIGHTS = DEFAULT_COMPOSITE_WEIGHTS
V3_EXTRA_WEIGHTS = DEFAULT_EXTRA_WEIGHTS
SPIKE_CONFIG = DEFAULT_SPIKE_CONFIG


# ---- Fetch OHLCV from Binance Futures ----

def fetch_binance_15m(symbol, years=3):
    cache_file = f"data_cache/{symbol}_15m_{years}yr.parquet"
    if os.path.exists(cache_file):
        print(f"  Using cached {cache_file}")
        df = pd.read_parquet(cache_file)
        df["date_time"] = pd.to_datetime(df["date_time"])
        return df.sort_values("date_time").reset_index(drop=True)

    print(f"  Fetching {years}yr 15m {symbol} from Binance...")
    url = "https://fapi.binance.com/fapi/v1/klines"
    end_ts = int(datetime.now().timestamp() * 1000)
    start_ts = end_ts - years * 365 * 24 * 3600 * 1000

    all_data = []
    current = start_ts
    batch = 0
    while current < end_ts:
        params = {"symbol": symbol, "interval": "15m", "startTime": current, "limit": 1500}
        for attempt in range(5):
            try:
                r = requests.get(url, params=params, timeout=15)
                if r.status_code == 429:
                    print(f"    Rate limited, waiting 10s...")
                    _time.sleep(10)
                    continue
                r.raise_for_status()
                data = r.json()
                break
            except Exception as e:
                if attempt < 4:
                    _time.sleep(3)
                else:
                    print(f"    Failed: {e}")
                    data = []

        if not data:
            break

        for k in data:
            all_data.append({
                "date_time": pd.Timestamp(k[0], unit="ms"),
                "open": float(k[1]), "high": float(k[2]),
                "low": float(k[3]), "close": float(k[4]),
                "volume": float(k[5]),
            })

        current = data[-1][0] + 1
        batch += 1
        if batch % 20 == 0:
            print(f"    {len(all_data):,} candles fetched...", end="\r", flush=True)
            _time.sleep(0.5)

    df = pd.DataFrame(all_data).drop_duplicates("date_time").sort_values("date_time").reset_index(drop=True)
    os.makedirs("data_cache", exist_ok=True)
    df.to_parquet(cache_file, index=False)
    print(f"    {len(df):,} candles saved to {cache_file}")
    return df


def fetch_binance_funding(symbol):
    cache_file = f"data_cache/{symbol}_funding_hist.parquet"
    if os.path.exists(cache_file):
        df = pd.read_parquet(cache_file)
        print(f"  Using cached {cache_file} ({len(df):,} rows)")
        return df

    print(f"  Fetching funding rate for {symbol}...")
    url = "https://fapi.binance.com/fapi/v1/fundingRate"
    all_data = []
    start_ts = int(datetime(2020, 1, 1).timestamp() * 1000)
    end_ts = int(datetime.now().timestamp() * 1000)

    while start_ts < end_ts:
        params = {"symbol": symbol, "startTime": start_ts, "limit": 1000}
        for attempt in range(5):
            try:
                r = requests.get(url, params=params, timeout=15)
                if r.status_code == 429:
                    _time.sleep(5)
                    continue
                r.raise_for_status()
                data = r.json()
                break
            except Exception as e:
                if attempt < 4:
                    _time.sleep(2)
                else:
                    print(f"    Failed: {e}")
                    data = []

        if not data:
            break

        for d in data:
            all_data.append({
                "ts": pd.Timestamp(d["fundingTime"], unit="ms"),
                "fr_8h": float(d["fundingRate"]),
            })

        start_ts = data[-1]["fundingTime"] + 1
        if len(data) < 1000:
            break
        _time.sleep(0.3)

    df = pd.DataFrame(all_data).drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    os.makedirs("data_cache", exist_ok=True)
    df.to_parquet(cache_file, index=False)
    print(f"    Saved {len(df):,} rows ({df['ts'].iloc[0]} to {df['ts'].iloc[-1]})")
    return df


# ---- Load BTC DB data (all 8 factors) ----

def load_btc_db_data():
    conn = psycopg2.connect(**DB_PARAMS)
    data = {}

    print("  Loading BTC OI...", end="", flush=True)
    data["oi"] = pd.read_sql(
        "SELECT ts, oi_usdt FROM market_data.open_interest WHERE symbol='BTCUSDT' ORDER BY ts",
        conn, parse_dates=["ts"])
    if not data["oi"].empty:
        data["oi"]["ts"] = data["oi"]["ts"].dt.tz_localize(None) - BKK_UTC_OFFSET
    print(f" {len(data['oi']):,}")

    # v3: taker_ratio and ls_ratio REMOVED (redundant/noise)

    print("  Loading BTC premium index...", end="", flush=True)
    data["premium"] = pd.read_sql(
        "SELECT ts, last_funding_rate, premium FROM market_data.premium_index WHERE symbol='BTCUSDT' ORDER BY ts",
        conn, parse_dates=["ts"])
    if not data["premium"].empty:
        data["premium"]["ts"] = data["premium"]["ts"].dt.tz_localize(None) - BKK_UTC_OFFSET
        data["premium"]["last_funding_rate"] = data["premium"]["last_funding_rate"].astype(float)
        data["premium"]["premium"] = data["premium"]["premium"].astype(float)
    print(f" {len(data['premium']):,}")

    # v3: fear_greed REMOVED (daily data = noise in 15m)

    print("  Loading whale alerts...", end="", flush=True)
    data["whale"] = pd.read_sql(
        "SELECT alert_time as ts, usd_value, sentiment FROM public.whale_alert WHERE symbol='BTC' ORDER BY alert_time",
        conn, parse_dates=["ts"])
    if not data["whale"].empty:
        data["whale"]["ts"] = data["whale"]["ts"].dt.tz_localize(None) - BKK_UTC_OFFSET
        data["whale"]["usd_value"] = data["whale"]["usd_value"].astype(float)
    print(f" {len(data['whale']):,}")

    print("  Loading liquidations...", end="", flush=True)
    data["liq"] = pd.read_sql(
        "SELECT created_at as ts, liq_long_1h, liq_short_1h FROM public.liquidation WHERE coin='BTC' ORDER BY created_at",
        conn, parse_dates=["ts"])
    if not data["liq"].empty:
        data["liq"]["ts"] = data["liq"]["ts"].dt.tz_localize(None) - BKK_UTC_OFFSET
        data["liq"]["ts"] = data["liq"]["ts"] + pd.Timedelta("1h")
    print(f" {len(data['liq']):,}")

    print("  Loading ETF flows...", end="", flush=True)
    data["etf"] = pd.read_sql(
        "SELECT date as ts, total FROM public.etf_btc ORDER BY date",
        conn, parse_dates=["ts"])
    if not data["etf"].empty:
        data["etf"]["ts"] = data["etf"]["ts"] - BKK_UTC_OFFSET + pd.Timedelta("1d")
        data["etf"] = data["etf"].rename(columns={"total": "etf_flow"})
    print(f" {len(data['etf']):,}")

    print("  Loading BTC funding rate from DB...", end="", flush=True)
    data["funding"] = pd.read_sql(
        "SELECT date as ts, funding_rate FROM public.funding_rate WHERE symbol='BTCUSDT' ORDER BY date",
        conn, parse_dates=["ts"])
    if not data["funding"].empty:
        data["funding"]["ts"] = data["funding"]["ts"].dt.tz_localize(None) - BKK_UTC_OFFSET
        data["funding"] = data["funding"].rename(columns={"funding_rate": "fr_8h"})
    print(f" {len(data['funding']):,}")

    # v3 new factors: basis, tick_liq, order book
    print("  Loading basis rate...", end="", flush=True)
    data["basis"] = pd.read_sql(
        "SELECT ts, basis_rate FROM market_data.basis WHERE pair='BTCUSDT' ORDER BY ts",
        conn, parse_dates=["ts"])
    if not data["basis"].empty:
        data["basis"]["ts"] = data["basis"]["ts"].dt.tz_localize(None) - BKK_UTC_OFFSET
    print(f" {len(data['basis']):,}")

    print("  Loading tick liquidations...", end="", flush=True)
    data["tick_liq"] = pd.read_sql(
        "SELECT event_time as ts, side, notional_usd FROM market_data.liquidation "
        "WHERE symbol='BTCUSDT' ORDER BY event_time",
        conn, parse_dates=["ts"])
    if not data["tick_liq"].empty:
        data["tick_liq"]["ts"] = data["tick_liq"]["ts"].dt.tz_localize(None) - BKK_UTC_OFFSET
    print(f" {len(data['tick_liq']):,}")

    print("  Loading order book...", end="", flush=True)
    data["ob"] = pd.read_sql(
        "SELECT fetched_at as ts, "
        "(meta->>'imbalance')::float as imbalance, "
        "(meta->>'bid_sum')::float as bid_sum, "
        "(meta->>'ask_sum')::float as ask_sum "
        "FROM market_data.order_book_raw ORDER BY fetched_at",
        conn, parse_dates=["ts"])
    if not data["ob"].empty:
        data["ob"]["ts"] = data["ob"]["ts"].dt.tz_localize(None) - BKK_UTC_OFFSET
    print(f" {len(data['ob']):,}")

    conn.close()
    return data



# ---- Generate BTC-led signal for altcoin ----

def generate_btc_led_signal(btc_score_series, alt_df, threshold, use_alt_pa_filter,
                            btc_regime_ts=None, regime_penalty=0.0,
                            spike_mode=None, hysteresis_band=0.0):
    """
    Generate entry signals based on BTC composite score, optionally filtered
    by altcoin price action alignment, dead zone, and regime filter.

    btc_score_series: pd.Series indexed by ts with BTC composite scores
    alt_df: altcoin DataFrame with ts, ema9, ema21, vol_ratio columns
    btc_regime_ts: pd.Series indexed by ts, True=bullish (close>ema50)
    regime_penalty: extra threshold for counter-regime trades (0=disabled)
    spike_mode: None (no spike), "momentum", "contrarian", or "both"
                When set, adds volatility spike overlay signals.
    """
    # Align BTC score to altcoin timestamps
    btc_score_df = btc_score_series.reset_index()
    btc_score_df.columns = ["ts", "btc_score"]
    alt = alt_df.copy().sort_values("ts")
    alt = pd.merge_asof(alt, btc_score_df.sort_values("ts"),
                        on="ts", direction="backward", tolerance=pd.Timedelta("30min"))
    alt["btc_score"] = alt["btc_score"].fillna(0)

    # Regime filter: align BTC regime to altcoin timestamps
    if btc_regime_ts is not None and regime_penalty > 0:
        regime_df = btc_regime_ts.reset_index()
        regime_df.columns = ["ts", "btc_bullish"]
        alt = pd.merge_asof(alt, regime_df.sort_values("ts"),
                            on="ts", direction="backward", tolerance=pd.Timedelta("30min"))
        alt["btc_bullish"] = alt["btc_bullish"].fillna(False)

        # Soft regime: penalize counter-regime entries
        long_thr = np.where(alt["btc_bullish"], threshold, threshold + regime_penalty)
        short_thr = np.where(alt["btc_bullish"], threshold + regime_penalty, threshold)
    else:
        long_thr = threshold
        short_thr = threshold

    signal = pd.Series(0, index=alt.index)

    bull_cond = alt["btc_score"] >= long_thr
    bear_cond = alt["btc_score"] <= -short_thr

    if use_alt_pa_filter:
        alt_bull_pa = (alt["close"] > alt["ema9"]) & (alt["ema9"] > alt["ema21"])
        alt_bear_pa = (alt["close"] < alt["ema9"]) & (alt["ema9"] < alt["ema21"])
        alt_vol_ok = alt["vol_ratio"] > 0.8

        signal[bull_cond & alt_bull_pa & alt_vol_ok] = 1
        signal[bear_cond & alt_bear_pa & alt_vol_ok] = -1
    else:
        signal[bull_cond] = 1
        signal[bear_cond] = -1

    # Dead zone filter: suppress signals during 23:00-06:00 UTC
    hour = alt["ts"].dt.hour
    is_dead = (hour >= DEAD_ZONE_START) | (hour < DEAD_ZONE_END)
    signal[is_dead] = 0

    # ── Volatility spike overlay ──
    if spike_mode is not None:
        cfg = SPIKE_CONFIG
        # Compute spike features on alt if not already present
        if "range_z" not in alt.columns:
            alt["intrabar_range"] = (alt["high"] - alt["low"]) / alt["close"].clip(lower=1e-8)
            _rma = alt["intrabar_range"].rolling(96).mean()
            _rstd = alt["intrabar_range"].rolling(96).std().clip(lower=1e-8)
            alt["range_z"] = (alt["intrabar_range"] - _rma) / _rstd
        if "ema21_dist" not in alt.columns and "ema21" in alt.columns and "atr" in alt.columns:
            alt["ema21_dist"] = (alt["close"] - alt["ema21"]) / alt["atr"].clip(lower=1e-8)

        spike = detect_spike(alt)
        mode = classify_spike_mode(alt)

        # Only apply spike signals where normal signal is 0
        no_signal = signal == 0
        spike_active = spike & no_signal & ~is_dead

        for i in alt.index:
            if not spike_active.iloc[i] if isinstance(spike_active.index[0], int) else not spike_active.loc[i]:
                continue

            score = alt["btc_score"].iloc[i] if isinstance(alt.index[0], int) else alt.loc[i, "btc_score"]
            bar_mode = mode.iloc[i] if isinstance(mode.index[0], int) else mode.loc[i]

            if spike_mode == "momentum" and bar_mode != "momentum":
                continue
            if spike_mode == "contrarian" and bar_mode != "contrarian":
                continue

            if bar_mode == "contrarian":
                adj_thr = max(threshold - cfg["contrarian_reduction"], 0.5)
                ret = alt["ret"].iloc[i] if "ret" in alt.columns else 0
                if ret < -0.005 and score >= adj_thr:
                    signal.iloc[i] = 1
                elif ret > 0.005 and score <= -adj_thr:
                    signal.iloc[i] = -1
            else:  # momentum
                adj_thr = max(threshold - cfg["momentum_reduction"], 1.0)
                if score >= adj_thr:
                    signal.iloc[i] = 1
                elif score <= -adj_thr:
                    signal.iloc[i] = -1

    # ── Hysteresis: re-compute signals bar-by-bar with state ──
    if hysteresis_band > 0:
        scores = alt["btc_score"].values
        raw_signals = signal.values.copy()
        prev = 0
        for i in range(len(raw_signals)):
            if is_dead.iloc[i]:
                raw_signals[i] = 0
                prev = 0
                continue
            raw_signals[i] = compute_raw_signal(float(scores[i]), threshold, prev, hysteresis_band)
            prev = int(raw_signals[i])
        signal = pd.Series(raw_signals, index=signal.index)

    return signal, alt


# ---- Backtest engine (runs on altcoin OHLCV with altcoin ATR) ----

def compute_half_kelly(trades):
    """Compute half-Kelly fraction from trade results. Returns (kelly, half_kelly, win_rate, avg_rr)."""
    if trades.empty or len(trades) < 10:
        return 0, 0, 0, 0
    wins = trades[trades["pnl_net"] > 0]
    losses = trades[trades["pnl_net"] < 0]
    if len(wins) == 0 or len(losses) == 0:
        return 0, 0, 0, 0
    W = len(wins) / len(trades)
    R = wins["pnl_net"].mean() / abs(losses["pnl_net"].mean())
    K = W - (1 - W) / R
    half_K = max(K / 2, 0.01)  # floor at 1%
    return round(K, 4), round(half_K, 4), round(W, 4), round(R, 4)


def run_backtest(df, signals, sl_atr_mult=2.0, tp_atr_mult=3.0,
                 trail_atr_mult=0.5, trail_activate_atr=0.5,
                 max_hold_bars=96, cooldown_bars=4,
                 dynamic_sizing=False, risk_pct=0.02,
                 stale_exit_bars=None,
                 min_bars_before_flip=0, flip_cd_extra=0):
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
    max_notional = BUDGET_USDT * LEVERAGE * 2  # cap for dynamic sizing

    for i in range(n):
        if position == 0 and sig[i] != 0 and (i - last_exit_i) > cooldown_bars:
            raw_px = opens[i]
            cur_atr = atrs[i] if not np.isnan(atrs[i]) else 0
            if raw_px <= 0 or cur_atr <= 0 or cur_atr / raw_px < 0.0005:
                continue
            if dynamic_sizing:
                risk_amount = risk_pct * equity
                sl_distance = sl_atr_mult * cur_atr
                qty = risk_amount / sl_distance
                notional = qty * raw_px
                if notional > max_notional:
                    qty = max_notional / raw_px
            else:
                qty = (BUDGET_USDT * LEVERAGE) / raw_px
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
                if unrealized_pct < 0.001:  # < 0.1%
                    exit_px, exit_reason = c, "STALE"

            if exit_px is None and (i - entry_i) >= max_hold_bars:
                exit_px, exit_reason = c, "TIMEOUT"
            if exit_px is None and sig[i] != 0 and sig[i] != position:
                bars_held = i - entry_i
                if bars_held >= min_bars_before_flip:
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
                })
                extra_cd = flip_cd_extra if exit_reason == "SIGNAL_FLIP" else 0
                last_exit_i = i + extra_cd
                position = 0

    return pd.DataFrame(records)


def calc_metrics(trades, total_bars):
    if trades.empty:
        return {"total": 0, "win_rate": 0, "pf": 0, "net_pnl": 0, "max_dd": 0,
                "sharpe": 0, "rr": 0, "avg_pnl": 0, "avg_win": 0, "avg_loss": 0,
                "n_long": 0, "n_short": 0, "wr_long": 0, "wr_short": 0, "exposure": 0}
    n = len(trades)
    wins = trades[trades["pnl_net"] > 0]
    losses = trades[trades["pnl_net"] < 0]
    wr = len(wins) / n * 100
    sw = wins["pnl_net"].sum() if len(wins) else 0
    sl = abs(losses["pnl_net"].sum()) if len(losses) else 0
    pf = sw / sl if sl > 0 else float("inf") if sw > 0 else 0
    net = trades["pnl_net"].sum()
    eq = INIT_EQUITY + trades["pnl_net"].cumsum()
    eq_full = pd.concat([pd.Series([INIT_EQUITY]), eq]).reset_index(drop=True)
    dd = ((eq_full - eq_full.cummax()) / eq_full.cummax()).min() * 100
    rets = trades["pnl_net"] / BUDGET_USDT
    sharpe = rets.mean() / rets.std() * np.sqrt(n) if len(rets) > 1 and rets.std() > 0 else 0
    aw = wins["pnl_net"].mean() if len(wins) else 0
    al = abs(losses["pnl_net"].mean()) if len(losses) else 0
    rr = aw / al if al > 0 else 0
    exp = trades["holding_bars"].sum() / total_bars * 100 if total_bars > 0 else 0
    longs = trades[trades["dir"] == "L"]
    shorts = trades[trades["dir"] == "S"]
    return {
        "total": n, "win_rate": round(wr, 2), "pf": round(pf, 3),
        "net_pnl": round(net, 2), "max_dd": round(dd, 2),
        "sharpe": round(sharpe, 3), "rr": round(rr, 3),
        "avg_pnl": round(trades["pnl_net"].mean(), 2),
        "avg_win": round(aw, 2), "avg_loss": round(al, 2),
        "n_long": len(longs), "n_short": len(shorts),
        "wr_long": round(len(longs[longs["pnl_net"] > 0]) / max(len(longs), 1) * 100, 1),
        "wr_short": round(len(shorts[shorts["pnl_net"] > 0]) / max(len(shorts), 1) * 100, 1),
        "exposure": round(exp, 2),
    }


# ---- Grid search per altcoin ----

def grid_search_alt(btc_score_ts, alt_df_train, total_bars, btc_regime_ts=None):
    """Grid search: BTC threshold x regime_penalty x altcoin SL/TP/trail/cooldown x PA filter."""
    thresholds = [2.0, 2.5, 3.0, 3.5, 4.0]
    regime_penalties = [0.0, 1.0, 2.0]
    sl_mults = [1.5, 2.0, 2.5]
    tp_mults = [3.0, 4.0]
    trail_mults = [0.5, 0.8, 1.0, 1.5, 99]
    cooldowns = [4, 8]
    alt_pa_filters = [True, False]

    total = (len(thresholds) * len(regime_penalties) * len(sl_mults) * len(tp_mults)
             * len(trail_mults) * len(cooldowns) * len(alt_pa_filters))
    print(f"\n  Grid search: {total} combinations...")

    best = None
    best_m = None
    results = []
    count = 0

    for thr in thresholds:
        for rp in regime_penalties:
            for use_pa in alt_pa_filters:
                signals, alt_merged = generate_btc_led_signal(
                    btc_score_ts, alt_df_train, thr, use_pa,
                    btc_regime_ts=btc_regime_ts, regime_penalty=rp)
                n_sig = (signals != 0).sum()
                if n_sig < 10:
                    count += len(sl_mults) * len(tp_mults) * len(trail_mults) * len(cooldowns)
                    continue

                for sl_m in sl_mults:
                    for tp_m in tp_mults:
                        if tp_m < sl_m * 1.5:
                            count += len(trail_mults) * len(cooldowns)
                            continue
                        for tr_m in trail_mults:
                            for cd in cooldowns:
                                ta_val = 0.5 if tr_m <= 0.5 else 0.8 if tr_m <= 1.0 else 1.5 if tr_m < 50 else 99
                                trades = run_backtest(alt_merged, signals,
                                                      sl_atr_mult=sl_m, tp_atr_mult=tp_m,
                                                      trail_atr_mult=tr_m, trail_activate_atr=ta_val,
                                                      cooldown_bars=cd)
                                m = calc_metrics(trades, total_bars)
                                count += 1
                                if m["total"] >= 10:
                                    r = {"threshold": thr, "regime_penalty": rp,
                                         "alt_pa": use_pa,
                                         "sl": sl_m, "tp": tp_m,
                                         "trail": tr_m, "trail_act": ta_val, "cd": cd, **m}
                                    results.append(r)
                                    if best is None or m["net_pnl"] > best_m["net_pnl"]:
                                        best = r
                                        best_m = m
                                if count % 200 == 0:
                                    print(f"    {count}/{total}...", end="\r", flush=True)

    print(f"    {count}/{total} done!          ")
    return results, best


# ---- Run one altcoin ----

def run_alt(coin_name, btc_score_ts, btc_period_start, btc_period_end, btc_regime_ts=None):
    """Run BTC-led backtest for one altcoin."""
    symbol = f"{coin_name}USDT"
    print(f"\n{'='*60}")
    print(f"  {coin_name} ({symbol}) -- BTC-Led Backtest v2")
    print(f"{'='*60}")

    # 1. Fetch altcoin OHLCV
    print(f"\n[1/4] Loading OHLCV for {symbol}...")
    ohlcv = fetch_binance_15m(symbol, years=3)
    print(f"  OHLCV: {len(ohlcv):,} candles ({ohlcv['date_time'].iloc[0]} to {ohlcv['date_time'].iloc[-1]})")

    # 2. Build altcoin technicals
    print(f"\n[2/4] Building altcoin technicals...")
    alt_df = build_alt_technicals(ohlcv)

    # Trim to backtest period (aligned with BTC data)
    alt_df = alt_df[(alt_df["ts"] >= btc_period_start) & (alt_df["ts"] <= btc_period_end)].reset_index(drop=True)
    if len(alt_df) < 200:
        print(f"  ERROR: Insufficient altcoin data ({len(alt_df)} rows)")
        return None
    print(f"  Dataset: {len(alt_df):,} rows ({alt_df['ts'].iloc[0]} to {alt_df['ts'].iloc[-1]})")

    # Walk-forward split: Train Jun-Nov 2025, Test Dec 2025 - Mar 2026
    split_date = pd.Timestamp("2025-12-01")
    alt_train = alt_df[alt_df["ts"] < split_date].reset_index(drop=True)
    alt_test = alt_df[alt_df["ts"] >= split_date].reset_index(drop=True)

    if len(alt_train) < 100 or len(alt_test) < 100:
        print(f"  ERROR: Insufficient data for walk-forward (train={len(alt_train)}, test={len(alt_test)})")
        return None

    print(f"  Train: {len(alt_train):,} bars ({alt_train['ts'].iloc[0]} to {alt_train['ts'].iloc[-1]})")
    print(f"  Test:  {len(alt_test):,} bars ({alt_test['ts'].iloc[0]} to {alt_test['ts'].iloc[-1]})")

    bh_train = (alt_train["close"].iloc[-1] / alt_train["close"].iloc[0] - 1) * 100
    bh_test = (alt_test["close"].iloc[-1] / alt_test["close"].iloc[0] - 1) * 100
    bh_full = (alt_df["close"].iloc[-1] / alt_df["close"].iloc[0] - 1) * 100
    print(f"  B&H: Train {bh_train:+.2f}% | Test {bh_test:+.2f}% | Full {bh_full:+.2f}%")

    # 3. Grid search on train (fixed sizing, 2x leverage)
    print(f"\n[3/4] Grid search on TRAIN ({coin_name})...")
    all_results, best = grid_search_alt(btc_score_ts, alt_train, len(alt_train),
                                        btc_regime_ts=btc_regime_ts)

    if best is None:
        print(f"  ERROR: No valid results for {coin_name}")
        return None

    print(f"\n  Best: thr={best['threshold']} rp={best['regime_penalty']} alt_pa={best['alt_pa']} "
          f"SL={best['sl']} TP={best['tp']} trail={best['trail']} cd={best['cd']}")
    print(f"  -> {best['total']} trades, WR={best['win_rate']:.1f}%, PnL=${best['net_pnl']:+,.2f}, PF={best['pf']:.3f}")

    # 4. Compute half-Kelly from training trades
    print(f"\n[4/4] Computing half-Kelly + validation ({coin_name})...")
    signals_train, alt_train_merged = generate_btc_led_signal(
        btc_score_ts, alt_train, best["threshold"], best["alt_pa"],
        btc_regime_ts=btc_regime_ts, regime_penalty=best["regime_penalty"])
    train_trades = run_backtest(alt_train_merged, signals_train,
                                sl_atr_mult=best["sl"], tp_atr_mult=best["tp"],
                                trail_atr_mult=best["trail"], trail_activate_atr=best["trail_act"],
                                cooldown_bars=best["cd"])
    kelly, half_kelly, kelly_wr, kelly_rr = compute_half_kelly(train_trades)
    print(f"  Kelly: K={kelly:.4f}, K/2={half_kelly:.4f} (WR={kelly_wr:.2%}, R:R={kelly_rr:.2f})")

    # Validate on train/test/full with both fixed and dynamic sizing
    results = {}
    results_dyn = {}
    for label, d in [("train", alt_train), ("test", alt_test), ("full", alt_df)]:
        signals, alt_merged = generate_btc_led_signal(
            btc_score_ts, d, best["threshold"], best["alt_pa"],
            btc_regime_ts=btc_regime_ts, regime_penalty=best["regime_penalty"])

        # Fixed sizing
        trades = run_backtest(alt_merged, signals,
                              sl_atr_mult=best["sl"], tp_atr_mult=best["tp"],
                              trail_atr_mult=best["trail"], trail_activate_atr=best["trail_act"],
                              cooldown_bars=best["cd"])
        m = calc_metrics(trades, len(d))
        results[label] = {"trades": trades, "metrics": m}

        # Dynamic sizing (half-Kelly)
        trades_dyn = run_backtest(alt_merged, signals,
                                  sl_atr_mult=best["sl"], tp_atr_mult=best["tp"],
                                  trail_atr_mult=best["trail"], trail_activate_atr=best["trail_act"],
                                  cooldown_bars=best["cd"],
                                  dynamic_sizing=True, risk_pct=half_kelly)
        m_dyn = calc_metrics(trades_dyn, len(d))
        results_dyn[label] = {"trades": trades_dyn, "metrics": m_dyn}

        print(f"  {label.upper():5s}: Fixed ${m['net_pnl']:+,.2f} | Dynamic ${m_dyn['net_pnl']:+,.2f} "
              f"({m['total']} trades, WR={m['win_rate']:.1f}%)")

    # Store alt_df_full with signals for portfolio simulation
    signals_full, alt_df_full = generate_btc_led_signal(
        btc_score_ts, alt_df, best["threshold"], best["alt_pa"],
        btc_regime_ts=btc_regime_ts, regime_penalty=best["regime_penalty"])

    # Save trades
    os.makedirs("backtest_details", exist_ok=True)
    if not results["full"]["trades"].empty:
        results["full"]["trades"].to_csv(f"backtest_details/trades_15m_btcled_{coin_name.lower()}.csv", index=False)

    return {
        "coin": coin_name,
        "symbol": symbol,
        "config": best,
        "train": results["train"]["metrics"],
        "test": results["test"]["metrics"],
        "full": results["full"]["metrics"],
        "train_dyn": results_dyn["train"]["metrics"],
        "test_dyn": results_dyn["test"]["metrics"],
        "full_dyn": results_dyn["full"]["metrics"],
        "kelly": kelly,
        "half_kelly": half_kelly,
        "kelly_wr": kelly_wr,
        "kelly_rr": kelly_rr,
        "bh_train": bh_train,
        "bh_test": bh_test,
        "bh_full": bh_full,
        "n_bars_train": len(alt_train),
        "n_bars_test": len(alt_test),
        "n_bars_full": len(alt_df),
        "grid_results": all_results,
        "trades_full": results["full"]["trades"],
        # For portfolio simulation
        "alt_df_full": alt_df_full,
        "signals_full": signals_full,
    }


# ---- Portfolio Simulation (max concurrent positions) ----

def run_portfolio_simulation(all_results, max_concurrent=MAX_CONCURRENT):
    """
    Replay all coins' signals chronologically, bar-by-bar.
    Max `max_concurrent` positions across the portfolio.
    When >max candidates, prioritize by abs(btc_score).
    Uses dynamic sizing with each coin's half-Kelly.
    """
    coins_data = [r for r in all_results if r is not None and "alt_df_full" in r]
    if not coins_data:
        return None

    # Build unified timeline of all bars across coins
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
                "trail": cfg["trail"],
                "trail_act": cfg["trail_act"],
                "cd": cfg["cd"],
                "min_bars_flip": cfg.get("min_bars_flip", 0),
                "flip_cd_extra": cfg.get("flip_cd_extra", 0),
            })

    bars_df = pd.DataFrame(all_bars).sort_values(["ts", "coin"]).reset_index(drop=True)

    # Group by timestamp for bar-by-bar processing
    equity = INIT_EQUITY
    positions = {}  # coin -> position dict
    last_exit_ts = {}  # coin -> last exit ts
    records = []
    max_notional = BUDGET_USDT * LEVERAGE * 2

    for ts, group in bars_df.groupby("ts"):
        # Process exits first for existing positions
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

            trail_stop = None
            if pos["trail"] < 50:
                if direction == 1 and (pos["peak"] - pos["entry_px"]) >= pos["trail_act"] * atr:
                    pos["trl_active"] = True
                    trail_stop = pos["peak"] - pos["trail"] * atr
                elif direction == -1 and (pos["entry_px"] - pos["trough"]) >= pos["trail_act"] * atr:
                    pos["trl_active"] = True
                    trail_stop = pos["trough"] + pos["trail"] * atr

            exit_px = exit_reason = None
            if direction == 1:
                if l <= sl_level: exit_px, exit_reason = sl_level, "SL"
                elif pos["trl_active"] and trail_stop and l <= trail_stop: exit_px, exit_reason = trail_stop, "TRAIL"
                elif h >= tp_level: exit_px, exit_reason = tp_level, "TP"
            else:
                if h >= sl_level: exit_px, exit_reason = sl_level, "SL"
                elif pos["trl_active"] and trail_stop and h >= trail_stop: exit_px, exit_reason = trail_stop, "TRAIL"
                elif l <= tp_level: exit_px, exit_reason = tp_level, "TP"

            bars_held = (pd.Timestamp(ts) - pd.Timestamp(pos["entry_ts"])) / pd.Timedelta("15min")
            if exit_px is None and bars_held >= 96:
                exit_px, exit_reason = c, "TIMEOUT"

            # SIGNAL_FLIP: check if signal has flipped (matches paper trading logic)
            if exit_px is None and bar["signal"] != 0 and bar["signal"] != direction:
                min_bf = pos.get("min_bars_flip", 0)
                if bars_held >= min_bf:
                    exit_px, exit_reason = c, "SIGNAL_FLIP"

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
                # Extra cooldown after SIGNAL_FLIP (matches paper trading)
                extra_cd = pos.get("flip_cd_extra", 0) if exit_reason == "SIGNAL_FLIP" else 0
                last_exit_ts[coin] = pd.Timestamp(ts) + pd.Timedelta(minutes=15 * extra_cd)

        for coin in coins_to_exit:
            del positions[coin]

        # Process entries: collect candidates, prioritize by btc_score
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
            raw_px = bar["open"]
            cur_atr = bar["atr"]
            if raw_px <= 0 or np.isnan(cur_atr) or cur_atr <= 0 or cur_atr / raw_px < 0.0005:
                continue
            candidates.append(bar)

        # Sort by abs(btc_score) descending
        candidates.sort(key=lambda x: x["btc_score"], reverse=True)

        # Open positions up to max_concurrent
        slots_available = max_concurrent - len(positions)
        for bar in candidates[:slots_available]:
            coin = bar["coin"]
            raw_px = bar["open"]
            cur_atr = bar["atr"]
            sig_dir = bar["signal"]
            hk = bar["half_kelly"]

            # Dynamic sizing with half-Kelly
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
                "trl_active": False,
                "sl": bar["sl"],
                "tp": bar["tp"],
                "trail": bar["trail"],
                "trail_act": bar["trail_act"],
                "min_bars_flip": bar.get("min_bars_flip", 0),
                "flip_cd_extra": bar.get("flip_cd_extra", 0),
            }

    trades_df = pd.DataFrame(records) if records else pd.DataFrame()

    # Split into train/test for metrics
    split_date = pd.Timestamp("2025-12-01")
    total_bars = len(bars_df["ts"].unique())
    m_full = calc_metrics(trades_df, total_bars)

    if not trades_df.empty:
        trades_test = trades_df[pd.to_datetime(trades_df["entry_time"]) >= split_date]
        trades_train = trades_df[pd.to_datetime(trades_df["entry_time"]) < split_date]
    else:
        trades_test = trades_df
        trades_train = trades_df

    m_test = calc_metrics(trades_test, total_bars // 2)
    m_train = calc_metrics(trades_train, total_bars // 2)

    return {
        "trades": trades_df,
        "metrics_full": m_full,
        "metrics_train": m_train,
        "metrics_test": m_test,
        "max_concurrent": max_concurrent,
        "n_coins": len(coins_data),
    }


# ---- Report ----

def generate_report(all_results, portfolio_result=None):
    """Generate comparison report across all altcoins."""
    md = []
    md.append("# BTC-Led Altcoin 15m Backtest Report")
    md.append(f"\n**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    md.append(f"**Strategy:** BTC composite signal (8 factors) -> Trade altcoin")
    md.append(f"**Hypothesis:** BTC sentiment leads altcoins (higher beta)")
    md.append(f"**Train:** Jun-Nov 2025 | **Test:** Dec 2025 - Mar 2026 (4-month OOS)")
    md.append(f"**Fees:** Maker {FEE_BPS} bps + Slippage {SLIP_BPS} bps (RT: {(FEE+SLIP)*2*100:.3f}%)")
    md.append(f"**Position:** ${BUDGET_USDT:,.0f} x{LEVERAGE} leverage per trade")
    md.append(f"**Enhancements:** Dead zone (23-06 UTC), Regime filter, 2x Leverage, Half-Kelly sizing, Portfolio sim (max {MAX_CONCURRENT})")

    coins = [r for r in all_results if r is not None]
    if not coins:
        md.append("\n**ERROR: No coin results.**")
        with open("backtest_15m_btc_led_alts_report.md", "w", encoding="utf-8") as f:
            f.write("\n".join(md))
        return

    # Cross-coin comparison
    md.append("\n## Cross-Coin Comparison (OOS = Test Period)")
    headers = ["Metric"] + [c["coin"] for c in coins]
    md.append("\n| " + " | ".join(headers) + " |")
    md.append("|" + "|".join(["--------"] * len(headers)) + "|")

    rows = [
        ("Config", [f"thr={c['config']['threshold']} rp={c['config']['regime_penalty']} pa={c['config']['alt_pa']} SL={c['config']['sl']} TP={c['config']['tp']} tr={c['config']['trail']} cd={c['config']['cd']}" for c in coins]),
        ("**OOS Trades**", [str(c["test"]["total"]) for c in coins]),
        ("**OOS Win Rate**", [f"{c['test']['win_rate']:.1f}%" for c in coins]),
        ("**OOS PF**", [f"{c['test']['pf']:.3f}" for c in coins]),
        ("**OOS PnL**", [f"${c['test']['net_pnl']:+,.2f}" for c in coins]),
        ("**OOS Sharpe**", [f"{c['test']['sharpe']:.3f}" for c in coins]),
        ("OOS Max DD", [f"{c['test']['max_dd']:.2f}%" for c in coins]),
        ("OOS R:R", [f"{c['test']['rr']:.3f}" for c in coins]),
        ("OOS Avg Win", [f"${c['test']['avg_win']:.2f}" for c in coins]),
        ("OOS Avg Loss", [f"-${c['test']['avg_loss']:.2f}" for c in coins]),
        ("---", ["---"] * len(coins)),
        ("Train Trades", [str(c["train"]["total"]) for c in coins]),
        ("Train WR", [f"{c['train']['win_rate']:.1f}%" for c in coins]),
        ("Train PF", [f"{c['train']['pf']:.3f}" for c in coins]),
        ("Train PnL", [f"${c['train']['net_pnl']:+,.2f}" for c in coins]),
        ("Train Sharpe", [f"{c['train']['sharpe']:.3f}" for c in coins]),
        ("---", ["---"] * len(coins)),
        ("Full Trades", [str(c["full"]["total"]) for c in coins]),
        ("Full WR", [f"{c['full']['win_rate']:.1f}%" for c in coins]),
        ("Full PF", [f"{c['full']['pf']:.3f}" for c in coins]),
        ("Full PnL", [f"${c['full']['net_pnl']:+,.2f}" for c in coins]),
        ("Full Sharpe", [f"{c['full']['sharpe']:.3f}" for c in coins]),
        ("Full Max DD", [f"{c['full']['max_dd']:.2f}%" for c in coins]),
        ("---", ["---"] * len(coins)),
        ("B&H Train", [f"{c['bh_train']:+.2f}%" for c in coins]),
        ("B&H Test", [f"{c['bh_test']:+.2f}%" for c in coins]),
        ("B&H Full", [f"{c['bh_full']:+.2f}%" for c in coins]),
    ]

    for label, vals in rows:
        md.append(f"| {label} | " + " | ".join(vals) + " |")

    # Data flow diagram
    md.append("\n## Data Flow")
    md.append("```")
    md.append("BTC DB Data (8 factors: OI, Taker, L/S, Funding, F&G, Whale, Liq, ETF)")
    md.append("  -> BTC Composite Score (per 15m bar)")
    md.append("       -> Score >= threshold? -> Entry signal")
    md.append("                                    |")
    md.append("Altcoin OHLCV (Binance Futures)     |")
    md.append("  -> EMA9, EMA21, Vol ratio         |")
    md.append("       -> [Optional] PA alignment --+-> Trade on Altcoin price")
    md.append("       -> ATR for SL/TP                 (altcoin volatility)")
    md.append("```")

    # Per-coin monthly breakdown
    for c in coins:
        trades = c.get("trades_full")
        if trades is not None and not trades.empty:
            md.append(f"\n## {c['coin']} -- Monthly Breakdown")
            md.append("| Month | Trades | WR | PnL | Cum PnL |")
            md.append("|-------|--------|-----|-----|---------|")
            trades_copy = trades.copy()
            trades_copy["month"] = pd.to_datetime(trades_copy["entry_time"]).dt.to_period("M")
            cum = 0
            for month, grp in trades_copy.groupby("month"):
                pnl = grp["pnl_net"].sum()
                cum += pnl
                wr_m = (grp["pnl_net"] > 0).mean() * 100
                md.append(f"| {month} | {len(grp)} | {wr_m:.0f}% | ${pnl:+,.2f} | ${cum:+,.2f} |")

    # Per-coin exit analysis
    for c in coins:
        trades = c.get("trades_full")
        if trades is not None and not trades.empty:
            md.append(f"\n## {c['coin']} -- Exit Analysis")
            md.append("| Exit | Count | % | Avg PnL | WR | Total |")
            md.append("|------|-------|---|---------|-----|-------|")
            for reason, grp in trades.groupby("exit_reason"):
                cnt = len(grp)
                md.append(f"| {reason} | {cnt} | {cnt/len(trades)*100:.1f}% | ${grp['pnl_net'].mean():+.2f} | {(grp['pnl_net']>0).mean()*100:.0f}% | ${grp['pnl_net'].sum():+,.2f} |")

    # Half-Kelly parameters
    md.append("\n## Half-Kelly Parameters")
    md.append("| Coin | Kelly (K) | Half-Kelly (K/2) | Win Rate | Avg R:R |")
    md.append("|------|-----------|------------------|----------|---------|")
    for c in coins:
        md.append(f"| {c['coin']} | {c.get('kelly', 0):.4f} | {c.get('half_kelly', 0):.4f} | "
                  f"{c.get('kelly_wr', 0):.2%} | {c.get('kelly_rr', 0):.2f} |")

    # Dynamic sizing comparison
    md.append("\n## Dynamic Sizing vs Fixed (OOS)")
    md.append("| Coin | Fixed PnL | Dynamic PnL | Fixed DD | Dynamic DD |")
    md.append("|------|-----------|-------------|----------|------------|")
    for c in coins:
        td = c.get("test_dyn", {})
        tf = c.get("test", {})
        md.append(f"| {c['coin']} | ${tf.get('net_pnl', 0):+,.2f} | ${td.get('net_pnl', 0):+,.2f} | "
                  f"{tf.get('max_dd', 0):.2f}% | {td.get('max_dd', 0):.2f}% |")

    # Portfolio simulation
    if portfolio_result is not None:
        pm = portfolio_result
        md.append(f"\n## Portfolio Simulation (Max {pm['max_concurrent']} Concurrent)")
        md.append(f"- **Coins:** {pm['n_coins']}")
        md.append(f"- **Sizing:** Dynamic (Half-Kelly per coin)")
        mf = pm["metrics_full"]
        mt = pm["metrics_test"]
        mtr = pm["metrics_train"]
        md.append(f"\n| Period | Trades | WR | PF | PnL | Max DD | Sharpe |")
        md.append(f"|--------|--------|-----|-----|-----|--------|--------|")
        md.append(f"| Train | {mtr['total']} | {mtr['win_rate']:.1f}% | {mtr['pf']:.3f} | "
                  f"${mtr['net_pnl']:+,.2f} | {mtr['max_dd']:.2f}% | {mtr['sharpe']:.3f} |")
        md.append(f"| **OOS** | **{mt['total']}** | **{mt['win_rate']:.1f}%** | **{mt['pf']:.3f}** | "
                  f"**${mt['net_pnl']:+,.2f}** | **{mt['max_dd']:.2f}%** | **{mt['sharpe']:.3f}** |")
        md.append(f"| Full | {mf['total']} | {mf['win_rate']:.1f}% | {mf['pf']:.3f} | "
                  f"${mf['net_pnl']:+,.2f} | {mf['max_dd']:.2f}% | {mf['sharpe']:.3f} |")

        # Per-coin breakdown in portfolio
        if not pm["trades"].empty:
            md.append(f"\n### Portfolio -- Per-Coin Breakdown")
            md.append("| Coin | Trades | WR | PnL | Avg PnL |")
            md.append("|------|--------|-----|-----|---------|")
            for coin, grp in pm["trades"].groupby("coin"):
                wr_c = (grp["pnl_net"] > 0).mean() * 100
                md.append(f"| {coin} | {len(grp)} | {wr_c:.0f}% | ${grp['pnl_net'].sum():+,.2f} | ${grp['pnl_net'].mean():+.2f} |")

        # Sum of individual vs portfolio
        sum_individual = sum(c["test"].get("net_pnl", 0) for c in coins)
        md.append(f"\n- Sum of individual OOS PnL (no limit): ${sum_individual:+,.2f}")
        md.append(f"- Portfolio OOS PnL (max {pm['max_concurrent']}): ${mt['net_pnl']:+,.2f}")

    # Top 5 grid per coin
    for c in coins:
        grid = c.get("grid_results", [])
        if grid:
            md.append(f"\n## {c['coin']} -- Top 5 Grid (Train)")
            md.append("| # | Thr | RP | PA | SL | TP | Trail | CD | Trades | WR% | PF | PnL | DD% |")
            md.append("|---|-----|----|----|----|----|-------|----|--------|-----|-----|-----|-----|")
            top = sorted(grid, key=lambda x: x["net_pnl"], reverse=True)[:5]
            for i, r in enumerate(top, 1):
                md.append(f"| {i} | {r['threshold']} | {r['regime_penalty']} | {r['alt_pa']} | {r['sl']} | {r['tp']} | {r['trail']} | {r['cd']} | {r['total']} | {r['win_rate']:.1f}% | {r['pf']:.2f} | ${r['net_pnl']:+,.2f} | {r['max_dd']:.1f}% |")

    # Verdict
    md.append("\n## Verdict")
    profitable = []
    not_profitable = []
    for c in coins:
        oos_ok = c["test"]["net_pnl"] > 0
        status = "PROFITABLE" if oos_ok else "NOT PROFITABLE"
        md.append(f"- **{c['coin']}** OOS: **{status}** -- ${c['test']['net_pnl']:+,.2f} "
                  f"(PF {c['test']['pf']:.3f}, Sharpe {c['test']['sharpe']:.3f})")
        if oos_ok:
            profitable.append(c["coin"])
        else:
            not_profitable.append(c["coin"])

    md.append(f"\n**Best BTC-led alts:** {', '.join(profitable) if profitable else 'None'}")
    if profitable:
        best_coin = max([c for c in coins if c["coin"] in profitable],
                        key=lambda x: x["test"]["net_pnl"])
        md.append(f"**Top performer:** {best_coin['coin']} (OOS PnL ${best_coin['test']['net_pnl']:+,.2f}, "
                  f"Sharpe {best_coin['test']['sharpe']:.3f})")

    report_path = "backtest_15m_btc_led_alts_report.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md))
    print(f"\n  Report saved: {report_path}")


# ---- Main ----

def main():
    print("=" * 60)
    print("BTC-Led Altcoin 15m Backtest v2 (Enhanced)")
    print(f"Coins: {', '.join(ALT_COINS)}")
    print("Signal: BTC composite (8 factors) -> Trade altcoin")
    print(f"Enhancements: Dead zone, Regime filter, {LEVERAGE}x Leverage, Half-Kelly, Portfolio sim")
    print("=" * 60)

    # Step 1: Build BTC composite score (shared across all alts)
    print("\n" + "=" * 60)
    print("PHASE 1: Building BTC Composite Score")
    print("=" * 60)

    print("\n[1/3] Loading BTC OHLCV (3yr warmup)...")
    btc_ohlcv = fetch_binance_15m("BTCUSDT", years=3)
    print(f"  BTC OHLCV: {len(btc_ohlcv):,} candles")

    print("\n[2/3] Loading BTC DB data (8 factors)...")
    btc_db = load_btc_db_data()

    print("\n[3/3] Building BTC features + composite score...")
    btc_df = build_btc_features(btc_ohlcv, btc_db)

    # Composite score weights (fixed -- same as BTC strategy baseline)
    w = {
        "w_oi_bull": 0.5, "w_oi_capit": 0.5, "w_oi_weak": 0.5, "w_oi_bear": 0.5,
        "w_taker_strong": 1.5, "w_taker_mild": 0.5, "w_ls_extreme": 1.5,
        "w_fr_neg": 2.0, "w_fr_pos": 2.0, "w_fg_fear": 2.0, "w_fg_mild_fear": 1.0,
        "w_fg_greed": 2.0, "w_fg_mild_greed": 1.0, "w_whale_bull": 1.5, "w_whale_bear": 1.5,
        "w_liq_bull": 2.0, "w_liq_bear": 2.0, "w_etf_bull": 1.5, "w_etf_bear": 1.5,
    }
    btc_score = compute_btc_composite_score(btc_df, w)

    # Trim to Jun 2025+ (9-month backtest period)
    btc_period_start = pd.Timestamp("2025-06-01")
    mask = btc_df["ts"] >= btc_period_start
    btc_df_trimmed = btc_df[mask].reset_index(drop=True)
    btc_score_trimmed = btc_score[mask].reset_index(drop=True)

    btc_period_end = btc_df_trimmed["ts"].iloc[-1]
    print(f"  BTC score period: {btc_period_start} to {btc_period_end}")
    print(f"  Score range: [{btc_score_trimmed.min():.1f}, {btc_score_trimmed.max():.1f}], "
          f"mean={btc_score_trimmed.mean():.2f}, std={btc_score_trimmed.std():.2f}")
    print(f"  Score >2: {(btc_score_trimmed >= 2).sum():,} bars | <-2: {(btc_score_trimmed <= -2).sum():,} bars")

    # Create score time series for alignment
    btc_score_ts = pd.Series(btc_score_trimmed.values, index=btc_df_trimmed["ts"].values)

    # Compute BTC regime: bullish = close > EMA50
    btc_regime = btc_df_trimmed["close"] > btc_df_trimmed["ema50"]
    btc_regime_ts = pd.Series(btc_regime.values, index=btc_df_trimmed["ts"].values)
    bull_pct = btc_regime.mean() * 100
    print(f"  BTC regime: {bull_pct:.1f}% bullish, {100-bull_pct:.1f}% bearish")

    # Coverage check
    for col in ["oi_usdt", "buy_sell_ratio", "gl_ac_ratio", "fg_score",
                "whale_net", "liq_net", "etf_flow", "fr_8h"]:
        if col in btc_df_trimmed.columns:
            pct = btc_df_trimmed[col].notna().mean() * 100
            print(f"    BTC {col}: {pct:.1f}%")

    # Step 2: Run each altcoin
    print("\n" + "=" * 60)
    print("PHASE 2: Running Altcoin Backtests (with enhancements)")
    print("=" * 60)

    all_results = []
    for coin in ALT_COINS:
        try:
            result = run_alt(coin, btc_score_ts, btc_period_start, btc_period_end,
                             btc_regime_ts=btc_regime_ts)
            all_results.append(result)
        except Exception as e:
            print(f"\n  ERROR running {coin}: {e}")
            import traceback
            traceback.print_exc()
            all_results.append(None)

    # Step 3: Portfolio simulation
    print(f"\n{'='*60}")
    print(f"PHASE 3: Portfolio Simulation (max {MAX_CONCURRENT} concurrent)")
    print(f"{'='*60}")
    portfolio_result = run_portfolio_simulation(all_results, max_concurrent=MAX_CONCURRENT)
    if portfolio_result is not None:
        pm = portfolio_result
        print(f"  Portfolio Full:  {pm['metrics_full']['total']} trades, "
              f"PnL=${pm['metrics_full']['net_pnl']:+,.2f}, DD={pm['metrics_full']['max_dd']:.2f}%")
        print(f"  Portfolio OOS:   {pm['metrics_test']['total']} trades, "
              f"PnL=${pm['metrics_test']['net_pnl']:+,.2f}, DD={pm['metrics_test']['max_dd']:.2f}%")

    # Step 4: Generate report
    print(f"\n{'='*60}")
    print("PHASE 4: Generating Report")
    print(f"{'='*60}")
    generate_report(all_results, portfolio_result=portfolio_result)

    # Save grid results
    os.makedirs("backtest_details", exist_ok=True)
    for r in all_results:
        if r is not None and r.get("grid_results"):
            pd.DataFrame(r["grid_results"]).to_csv(
                f"backtest_details/grid_btcled_{r['coin'].lower()}.csv", index=False)

    # Summary
    print(f"\n{'='*60}")
    print("SUMMARY -- BTC-Led Altcoin Backtest v2")
    print(f"{'='*60}")
    for r in all_results:
        if r is not None:
            oos = r["test"]
            cfg = r["config"]
            print(f"  {r['coin']:5s}: OOS {oos['total']} trades, WR={oos['win_rate']:.1f}%, "
                  f"PF={oos['pf']:.3f}, PnL=${oos['net_pnl']:+,.2f}, Sharpe={oos['sharpe']:.3f} "
                  f"[thr={cfg['threshold']} rp={cfg['regime_penalty']} pa={cfg['alt_pa']}] "
                  f"K/2={r.get('half_kelly', 0):.4f}")
        else:
            print(f"  ???  : FAILED")

    if portfolio_result:
        mt = portfolio_result["metrics_test"]
        print(f"\n  PORTFOLIO (max {MAX_CONCURRENT}): OOS {mt['total']} trades, "
              f"WR={mt['win_rate']:.1f}%, PnL=${mt['net_pnl']:+,.2f}, "
              f"Sharpe={mt['sharpe']:.3f}, DD={mt['max_dd']:.2f}%")

    print("\nDone!")


if __name__ == "__main__":
    main()
