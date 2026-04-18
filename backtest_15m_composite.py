"""
BTC 15m Composite Strategy Backtest
====================================
Uses on-chain + derivatives data from smart_trading database:
  1. Open Interest (OI) divergence
  2. Taker Buy/Sell ratio
  3. Long/Short ratio (contrarian)
  4. Funding rate extremes
  5. Fear & Greed Index
  6. Whale alert net flow
  7. Liquidation cascades
  8. ETF BTC flows

Scoring: each factor contributes to composite score.
Entry when score crosses threshold with price confirmation.
"""

import os, sys, warnings, time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pandas_ta as ta
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

warnings.filterwarnings("ignore")
load_dotenv()

# ---- Config ----
SYMBOL = "BTCUSDT"
TF_MINUTES = 15
INIT_EQUITY = 10_000.0
BUDGET_USDT = 1_000.0
LEVERAGE = 1.0

# Fees: use MAKER rate (limit orders) for realistic live trading
FEE_BPS = 2.0        # 0.02% maker fee
SLIP_BPS = 1.5        # 0.015% slippage (less with limit orders)
FEE = FEE_BPS / 10_000
SLIP = SLIP_BPS / 10_000

DB_PARAMS = {
    "dbname": os.getenv("PG_DB", "smart_trading"),
    "user": os.getenv("PG_USER", "postgres"),
    "password": os.getenv("PG_PASS", "P@ssw0rd"),
    "host": os.getenv("PG_HOST", "localhost"),
    "port": os.getenv("PG_PORT", "5432"),
}


# ---- Data Loading ----

def load_ohlcv_15m() -> pd.DataFrame:
    """Load cached 15m OHLCV data."""
    cache_file = "data_cache/BTCUSDT_15m_365d.parquet"
    if not os.path.exists(cache_file):
        print("ERROR: Run backtest_tf_compare.py first to cache 15m data")
        sys.exit(1)
    df = pd.read_parquet(cache_file)
    df["date_time"] = pd.to_datetime(df["date_time"])
    return df.sort_values("date_time").reset_index(drop=True)


def load_db_data() -> dict:
    """Load all market data from database."""
    conn = psycopg2.connect(**DB_PARAMS)

    data = {}

    # 1. Open Interest
    print("  Loading OI...", end="", flush=True)
    sql = """
        SELECT ts, oi_usdt, mark_price
        FROM market_data.open_interest
        WHERE symbol = 'BTCUSDT'
        ORDER BY ts
    """
    data["oi"] = pd.read_sql(sql, conn, parse_dates=["ts"])
    data["oi"]["ts"] = data["oi"]["ts"].dt.tz_localize(None)
    print(f" {len(data['oi']):,} rows")

    # 2. Taker Volume
    print("  Loading taker volume...", end="", flush=True)
    sql = """
        SELECT ts, buy_vol, sell_vol, buy_sell_ratio
        FROM market_data.taker_volume
        WHERE symbol = 'BTCUSDT'
        ORDER BY ts
    """
    data["taker"] = pd.read_sql(sql, conn, parse_dates=["ts"])
    print(f" {len(data['taker']):,} rows")

    # 3. Long/Short Ratio
    print("  Loading long/short ratio...", end="", flush=True)
    sql = """
        SELECT ts, gl_ac_ratio, top_ac_ratio, top_po_ratio
        FROM market_data.long_short_ratio
        WHERE symbol = 'BTCUSDT'
        ORDER BY ts
    """
    data["ls_ratio"] = pd.read_sql(sql, conn, parse_dates=["ts"])
    print(f" {len(data['ls_ratio']):,} rows")

    # 4. Premium / Funding Rate
    print("  Loading premium index...", end="", flush=True)
    sql = """
        SELECT ts, last_funding_rate, premium
        FROM market_data.premium_index
        WHERE symbol = 'BTCUSDT'
        ORDER BY ts
    """
    data["premium"] = pd.read_sql(sql, conn, parse_dates=["ts"])
    data["premium"]["ts"] = data["premium"]["ts"].dt.tz_localize(None)
    data["premium"]["last_funding_rate"] = data["premium"]["last_funding_rate"].astype(float)
    data["premium"]["premium"] = data["premium"]["premium"].astype(float)
    print(f" {len(data['premium']):,} rows")

    # 5. Fear & Greed
    print("  Loading fear & greed...", end="", flush=True)
    sql = """
        SELECT created_at as ts, score, description
        FROM public.fear_greed
        ORDER BY created_at
    """
    data["fg"] = pd.read_sql(sql, conn, parse_dates=["ts"])
    data["fg"]["ts"] = data["fg"]["ts"].dt.tz_localize(None)
    print(f" {len(data['fg']):,} rows")

    # 6. Whale Alerts (BTC)
    print("  Loading whale alerts...", end="", flush=True)
    sql = """
        SELECT alert_time as ts, fire_level, usd_value, sentiment, direction
        FROM public.whale_alert
        WHERE symbol = 'BTC'
        ORDER BY alert_time
    """
    data["whale"] = pd.read_sql(sql, conn, parse_dates=["ts"])
    data["whale"]["ts"] = data["whale"]["ts"].dt.tz_localize(None)
    data["whale"]["usd_value"] = data["whale"]["usd_value"].astype(float)
    print(f" {len(data['whale']):,} rows")

    # 7. Liquidation (BTC hourly -- shift +1h: hourly data available at end of hour)
    print("  Loading liquidations...", end="", flush=True)
    sql = """
        SELECT created_at as ts, liq_long_1h, liq_short_1h
        FROM public.liquidation
        WHERE coin = 'BTC'
        ORDER BY created_at
    """
    data["liq"] = pd.read_sql(sql, conn, parse_dates=["ts"])
    data["liq"]["ts"] = data["liq"]["ts"].dt.tz_localize(None)
    data["liq"]["ts"] = data["liq"]["ts"] + pd.Timedelta("1h")  # fix look-ahead bias
    print(f" {len(data['liq']):,} rows")

    # 8. ETF Flows (shift +1 day: daily flow only available after US market close ~21:00 UTC)
    print("  Loading ETF flows...", end="", flush=True)
    sql = """
        SELECT date as ts, total
        FROM public.etf_btc
        ORDER BY date
    """
    data["etf"] = pd.read_sql(sql, conn, parse_dates=["ts"])
    data["etf"]["ts"] = data["etf"]["ts"] + pd.Timedelta("1d")  # fix look-ahead bias
    print(f" {len(data['etf']):,} rows")

    # 9. Funding Rate (8h)
    print("  Loading funding rate...", end="", flush=True)
    sql = """
        SELECT date as ts, funding_rate
        FROM public.funding_rate
        WHERE symbol = 'BTCUSDT'
        ORDER BY date
    """
    data["funding"] = pd.read_sql(sql, conn, parse_dates=["ts"])
    data["funding"]["ts"] = data["funding"]["ts"].dt.tz_localize(None)
    print(f" {len(data['funding']):,} rows")

    conn.close()
    return data


def resample_to_15m(df: pd.DataFrame, ts_col: str, value_cols: list, agg: str = "last") -> pd.DataFrame:
    """Resample 5m data to 15m by taking last value in each window."""
    d = df.set_index(ts_col).sort_index()
    if agg == "last":
        return d[value_cols].resample("15min").last().dropna(how="all").reset_index()
    elif agg == "sum":
        return d[value_cols].resample("15min").sum().reset_index()
    elif agg == "mean":
        return d[value_cols].resample("15min").mean().dropna(how="all").reset_index()
    return d[value_cols].resample("15min").last().dropna(how="all").reset_index()


def build_features(ohlcv: pd.DataFrame, db_data: dict) -> pd.DataFrame:
    """Merge OHLCV with all database features, aligned to 15m bars."""
    df = ohlcv.copy()
    df = df.rename(columns={"date_time": "ts"})

    # --- Technical indicators ---
    df["ema9"] = df["close"].ewm(span=9, adjust=False).mean()
    df["ema21"] = df["close"].ewm(span=21, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
    df["rsi"] = ta.rsi(df["close"], length=14)
    df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=14)
    df["vol_ma"] = df["volume"].rolling(20).mean()
    df["vol_ratio"] = df["volume"] / df["vol_ma"]
    df["ret"] = df["close"].pct_change()

    # --- 1. Open Interest ---
    if "oi" in db_data and len(db_data["oi"]) > 0:
        oi = resample_to_15m(db_data["oi"], "ts", ["oi_usdt"])
        oi["oi_chg"] = oi["oi_usdt"].pct_change()
        oi["oi_chg_ma"] = oi["oi_chg"].rolling(12).mean()  # 3h moving avg
        df = pd.merge_asof(df.sort_values("ts"), oi.sort_values("ts"),
                           on="ts", direction="backward", tolerance=pd.Timedelta("30min"))

    # --- 2. Taker Volume ---
    if "taker" in db_data and len(db_data["taker"]) > 0:
        taker = resample_to_15m(db_data["taker"], "ts", ["buy_vol", "sell_vol", "buy_sell_ratio"])
        taker["net_taker"] = taker["buy_vol"] - taker["sell_vol"]
        taker["net_taker_ma"] = taker["net_taker"].rolling(12).mean()
        taker["bsr_ma"] = taker["buy_sell_ratio"].rolling(12).mean()
        df = pd.merge_asof(df.sort_values("ts"), taker.sort_values("ts"),
                           on="ts", direction="backward", tolerance=pd.Timedelta("30min"))

    # --- 3. Long/Short Ratio ---
    if "ls_ratio" in db_data and len(db_data["ls_ratio"]) > 0:
        ls = resample_to_15m(db_data["ls_ratio"], "ts", ["gl_ac_ratio", "top_po_ratio"])
        ls["ls_chg"] = ls["gl_ac_ratio"].pct_change()
        df = pd.merge_asof(df.sort_values("ts"), ls.sort_values("ts"),
                           on="ts", direction="backward", tolerance=pd.Timedelta("30min"))

    # --- 4. Premium / Funding ---
    if "premium" in db_data and len(db_data["premium"]) > 0:
        prem = resample_to_15m(db_data["premium"], "ts", ["last_funding_rate", "premium"])
        df = pd.merge_asof(df.sort_values("ts"), prem.sort_values("ts"),
                           on="ts", direction="backward", tolerance=pd.Timedelta("30min"))

    # --- 5. Fear & Greed (daily -> forward fill) ---
    if "fg" in db_data and len(db_data["fg"]) > 0:
        fg = db_data["fg"][["ts", "score"]].copy()
        fg = fg.rename(columns={"score": "fg_score"})
        df = pd.merge_asof(df.sort_values("ts"), fg.sort_values("ts"),
                           on="ts", direction="backward", tolerance=pd.Timedelta("2d"))

    # --- 6. Whale Alerts (aggregate per 15m window) ---
    if "whale" in db_data and len(db_data["whale"]) > 0:
        whale = db_data["whale"].copy()
        whale["bull_val"] = np.where(whale["sentiment"] == "bullish", whale["usd_value"], 0)
        whale["bear_val"] = np.where(whale["sentiment"] == "bearish", whale["usd_value"], 0)
        whale_agg = whale.set_index("ts").resample("15min").agg({
            "bull_val": "sum", "bear_val": "sum"
        }).reset_index()
        whale_agg["whale_net"] = whale_agg["bull_val"] - whale_agg["bear_val"]
        whale_agg["whale_net_ma"] = whale_agg["whale_net"].rolling(8).mean()  # 2h rolling
        df = pd.merge_asof(df.sort_values("ts"), whale_agg.sort_values("ts"),
                           on="ts", direction="backward", tolerance=pd.Timedelta("30min"))

    # --- 7. Liquidation (hourly -> forward fill) ---
    if "liq" in db_data and len(db_data["liq"]) > 0:
        liq = db_data["liq"][["ts", "liq_long_1h", "liq_short_1h"]].copy()
        liq["liq_net"] = liq["liq_short_1h"] - liq["liq_long_1h"]  # positive = more short liq = bullish
        liq["liq_total"] = liq["liq_long_1h"] + liq["liq_short_1h"]
        liq["liq_total_ma"] = liq["liq_total"].rolling(24).mean()  # 24h avg
        df = pd.merge_asof(df.sort_values("ts"), liq.sort_values("ts"),
                           on="ts", direction="backward", tolerance=pd.Timedelta("2h"))

    # --- 8. ETF Flows (daily) ---
    if "etf" in db_data and len(db_data["etf"]) > 0:
        etf = db_data["etf"][["ts", "total"]].copy()
        etf = etf.rename(columns={"total": "etf_flow"})
        etf["etf_flow_ma"] = etf["etf_flow"].rolling(5).mean()  # 5-day MA
        df = pd.merge_asof(df.sort_values("ts"), etf.sort_values("ts"),
                           on="ts", direction="backward", tolerance=pd.Timedelta("3d"))

    # --- 9. Funding Rate (8h) ---
    if "funding" in db_data and len(db_data["funding"]) > 0:
        fr = db_data["funding"][["ts", "funding_rate"]].copy()
        fr = fr.rename(columns={"funding_rate": "fr_8h"})
        fr["fr_ma"] = fr["fr_8h"].rolling(3).mean()  # 24h avg (3x8h)
        df = pd.merge_asof(df.sort_values("ts"), fr.sort_values("ts"),
                           on="ts", direction="backward", tolerance=pd.Timedelta("12h"))

    return df.sort_values("ts").reset_index(drop=True)


# ---- Composite Signal Generation ----

def generate_composite_signal(df: pd.DataFrame, params: dict) -> pd.Series:
    """
    Score-based composite signal from all available factors.
    Each factor contributes to a composite score.
    Long when score >= threshold, Short when score <= -threshold.
    """
    n = len(df)
    score = pd.Series(0.0, index=df.index)

    # --- Factor 1: OI Divergence ---
    if "oi_chg" in df.columns:
        oi_chg = df["oi_chg"].fillna(0)
        price_ret = df["ret"].fillna(0)

        # Healthy trend: OI up + price up = bullish continuation
        # Unhealthy: OI down + price up = weak rally (shorts closing)
        # Capitulation: OI down + price down = bottom forming
        # Accumulation: OI up + price down = new shorts entering

        # Price up + OI up = strong bullish (+1)
        score += np.where((price_ret > 0.001) & (oi_chg > 0.002), params.get("w_oi_bull", 1.0), 0)
        # Price down + OI down = capitulation, potential bounce (+0.5)
        score += np.where((price_ret < -0.001) & (oi_chg < -0.002), params.get("w_oi_capit", 0.5), 0)
        # Price up + OI down = weak rally (-0.5)
        score += np.where((price_ret > 0.001) & (oi_chg < -0.002), -params.get("w_oi_weak", 0.5), 0)
        # Price down + OI up = new shorts (-1)
        score += np.where((price_ret < -0.001) & (oi_chg > 0.002), -params.get("w_oi_bear", 1.0), 0)

    # --- Factor 2: Taker Buy/Sell Ratio ---
    if "buy_sell_ratio" in df.columns:
        bsr = df["buy_sell_ratio"].fillna(1.0)
        # Strong buying pressure
        score += np.where(bsr > 1.5, params.get("w_taker_strong", 2.0), 0)
        score += np.where((bsr > 1.2) & (bsr <= 1.5), params.get("w_taker_mild", 1.0), 0)
        # Strong selling pressure
        score += np.where(bsr < 0.7, -params.get("w_taker_strong", 2.0), 0)
        score += np.where((bsr >= 0.7) & (bsr < 0.85), -params.get("w_taker_mild", 1.0), 0)

    # --- Factor 3: Long/Short Ratio (contrarian) ---
    if "gl_ac_ratio" in df.columns:
        ls = df["gl_ac_ratio"].fillna(1.0)
        # Too many longs = contrarian short
        score += np.where(ls > 2.5, -params.get("w_ls_extreme", 1.0), 0)
        # Too many shorts = contrarian long
        score += np.where(ls < 0.6, params.get("w_ls_extreme", 1.0), 0)

    # --- Factor 4: Funding Rate ---
    if "fr_8h" in df.columns:
        fr = df["fr_8h"].fillna(0)
        # Very negative funding = shorts paying, bullish signal
        score += np.where(fr < -0.0001, params.get("w_fr_neg", 1.5), 0)
        # Very positive funding = longs overleveraged, bearish
        score += np.where(fr > 0.0003, -params.get("w_fr_pos", 1.5), 0)
    elif "last_funding_rate" in df.columns:
        fr = df["last_funding_rate"].fillna(0)
        score += np.where(fr < -0.00005, params.get("w_fr_neg", 1.5), 0)
        score += np.where(fr > 0.0002, -params.get("w_fr_pos", 1.5), 0)

    # --- Factor 5: Fear & Greed ---
    if "fg_score" in df.columns:
        fg = df["fg_score"].fillna(50)
        # Extreme fear = buy opportunity
        score += np.where(fg < 15, params.get("w_fg_fear", 1.5), 0)
        score += np.where((fg >= 15) & (fg < 25), params.get("w_fg_mild_fear", 0.5), 0)
        # Extreme greed = sell opportunity
        score += np.where(fg > 80, -params.get("w_fg_greed", 1.5), 0)
        score += np.where((fg > 65) & (fg <= 80), -params.get("w_fg_mild_greed", 0.5), 0)

    # --- Factor 6: Whale Net Flow ---
    if "whale_net" in df.columns:
        wn = df["whale_net"].fillna(0)
        wn_ma = df["whale_net_ma"].fillna(0) if "whale_net_ma" in df.columns else wn
        # Bullish whale activity (outflow > inflow)
        score += np.where(wn_ma > 50_000_000, params.get("w_whale_bull", 1.0), 0)
        # Bearish whale activity (inflow > outflow)
        score += np.where(wn_ma < -50_000_000, -params.get("w_whale_bear", 1.0), 0)

    # --- Factor 7: Liquidation ---
    if "liq_net" in df.columns and "liq_total_ma" in df.columns:
        lt = df["liq_total"].fillna(0)
        lt_ma = df["liq_total_ma"].fillna(1)
        ln = df["liq_net"].fillna(0)

        # Large liquidation cascade (> 3x average) = reversal opportunity
        cascade = lt > (lt_ma * 3)
        # Net short liq (shorts getting rekt) = bullish reversal
        score += np.where(cascade & (ln > 0), params.get("w_liq_bull", 1.5), 0)
        # Net long liq (longs getting rekt) = bearish reversal
        score += np.where(cascade & (ln < 0), -params.get("w_liq_bear", 1.5), 0)

    # --- Factor 8: ETF Flows ---
    if "etf_flow" in df.columns:
        etf = df["etf_flow"].fillna(0)
        etf_ma = df["etf_flow_ma"].fillna(0) if "etf_flow_ma" in df.columns else etf
        # Positive ETF inflow
        score += np.where(etf_ma > 50, params.get("w_etf_bull", 1.0), 0)
        # Negative ETF outflow
        score += np.where(etf_ma < -50, -params.get("w_etf_bear", 1.0), 0)

    # --- Price Confirmation Filter ---
    # Only take signals when price action confirms
    bull_pa = (df["close"] > df["ema9"]) & (df["ema9"] > df["ema21"])
    bear_pa = (df["close"] < df["ema9"]) & (df["ema9"] < df["ema21"])

    # Minimum volume
    vol_ok = df["vol_ratio"] > 0.8

    # --- Generate signals ---
    threshold = params.get("threshold", 3.0)

    signal = pd.Series(0, index=df.index)
    signal[(score >= threshold) & bull_pa & vol_ok] = 1
    signal[(score <= -threshold) & bear_pa & vol_ok] = -1

    return signal, score


# ---- Backtest Engine ----

def run_backtest(df, signals, sl_atr_mult=1.5, tp_atr_mult=3.0,
                 trail_atr_mult=0.8, trail_activate_atr=1.0,
                 max_hold_bars=96, cooldown_bars=4):
    """ATR-based backtest engine with trailing stop."""

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
                tp_level = entry_px + tp_atr_mult * atr if tp_atr_mult > 0 else None
            else:
                trough = min(trough, l)
                sl_level = entry_px + sl_atr_mult * atr
                tp_level = entry_px - tp_atr_mult * atr if tp_atr_mult > 0 else None

            # Trailing
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
                if l <= sl_level:
                    exit_px, exit_reason = sl_level, "SL"
                elif trl_active and trail_stop and l <= trail_stop:
                    exit_px, exit_reason = trail_stop, "TRAIL"
                elif tp_level and h >= tp_level:
                    exit_px, exit_reason = tp_level, "TP"
            else:
                if h >= sl_level:
                    exit_px, exit_reason = sl_level, "SL"
                elif trl_active and trail_stop and h >= trail_stop:
                    exit_px, exit_reason = trail_stop, "TRAIL"
                elif tp_level and l <= tp_level:
                    exit_px, exit_reason = tp_level, "TP"

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
                    "qty": qty, "fee_in": fee_in, "fee_out": fee_out,
                    "pnl_gross": pnl_gross, "pnl_net": pnl_net,
                    "equity_after": equity, "exit_reason": exit_reason,
                    "holding_bars": i - entry_i,
                })
                last_exit_i = i
                position = 0

    return pd.DataFrame(records)


# ---- Metrics ----

def calc_metrics(trades, total_bars):
    if trades.empty:
        return {"total": 0, "win_rate": 0, "pf": 0, "net_pnl": 0, "max_dd": 0,
                "sharpe": 0, "rr": 0, "avg_pnl": 0, "exposure": 0}

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
    pk = eq_full.cummax()
    dd = ((eq_full - pk) / pk).min() * 100

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
        "net_pnl": round(net, 2), "net_pct": round(net / INIT_EQUITY * 100, 2),
        "final_eq": round(INIT_EQUITY + net, 2),
        "max_dd": round(dd, 2), "sharpe": round(sharpe, 3), "rr": round(rr, 3),
        "avg_pnl": round(trades["pnl_net"].mean(), 2),
        "avg_win": round(aw, 2), "avg_loss": round(al, 2),
        "exposure": round(exp, 2),
        "n_long": len(longs), "n_short": len(shorts),
        "wr_long": round(len(longs[longs["pnl_net"] > 0]) / max(len(longs), 1) * 100, 1),
        "wr_short": round(len(shorts[shorts["pnl_net"] > 0]) / max(len(shorts), 1) * 100, 1),
    }


# ---- Grid Search ----

def grid_search(df, total_bars):
    """Search over key parameters to find profitable combinations."""

    # Signal parameters
    thresholds = [2.0, 2.5, 3.0, 3.5, 4.0]

    # TP/SL parameters
    sl_mults = [1.0, 1.5, 2.0]
    tp_mults = [2.0, 3.0, 4.0]
    trail_mults = [0.5, 0.8, 99]  # 99 = no trail
    trail_activates = [0.8, 1.5]
    cooldowns = [4, 8]

    best = None
    best_metrics = None
    results = []

    # Factor weight sets
    weight_sets = [
        {"name": "balanced", "w_oi_bull": 1.0, "w_oi_capit": 0.5, "w_oi_weak": 0.5, "w_oi_bear": 1.0,
         "w_taker_strong": 2.0, "w_taker_mild": 1.0, "w_ls_extreme": 1.0,
         "w_fr_neg": 1.5, "w_fr_pos": 1.5, "w_fg_fear": 1.5, "w_fg_mild_fear": 0.5,
         "w_fg_greed": 1.5, "w_fg_mild_greed": 0.5, "w_whale_bull": 1.0, "w_whale_bear": 1.0,
         "w_liq_bull": 1.5, "w_liq_bear": 1.5, "w_etf_bull": 1.0, "w_etf_bear": 1.0},

        {"name": "taker_heavy", "w_oi_bull": 0.5, "w_oi_capit": 0.3, "w_oi_weak": 0.3, "w_oi_bear": 0.5,
         "w_taker_strong": 3.0, "w_taker_mild": 1.5, "w_ls_extreme": 0.5,
         "w_fr_neg": 1.0, "w_fr_pos": 1.0, "w_fg_fear": 1.0, "w_fg_mild_fear": 0.3,
         "w_fg_greed": 1.0, "w_fg_mild_greed": 0.3, "w_whale_bull": 0.5, "w_whale_bear": 0.5,
         "w_liq_bull": 1.0, "w_liq_bear": 1.0, "w_etf_bull": 0.5, "w_etf_bear": 0.5},

        {"name": "sentiment", "w_oi_bull": 0.5, "w_oi_capit": 0.5, "w_oi_weak": 0.5, "w_oi_bear": 0.5,
         "w_taker_strong": 1.5, "w_taker_mild": 0.5, "w_ls_extreme": 1.5,
         "w_fr_neg": 2.0, "w_fr_pos": 2.0, "w_fg_fear": 2.0, "w_fg_mild_fear": 1.0,
         "w_fg_greed": 2.0, "w_fg_mild_greed": 1.0, "w_whale_bull": 1.5, "w_whale_bear": 1.5,
         "w_liq_bull": 2.0, "w_liq_bear": 2.0, "w_etf_bull": 1.5, "w_etf_bear": 1.5},
    ]

    total_combos = len(weight_sets) * len(thresholds) * len(sl_mults) * len(tp_mults) * len(trail_mults) * len(cooldowns)
    print(f"\n  Grid search: {total_combos} combinations...")

    count = 0
    for ws in weight_sets:
        for thr in thresholds:
            params = {**ws, "threshold": thr}
            signals, scores = generate_composite_signal(df, params)
            n_sig = (signals != 0).sum()
            if n_sig < 10:
                count += len(sl_mults) * len(tp_mults) * len(trail_mults) * len(cooldowns)
                continue

            for sl_m in sl_mults:
                for tp_m in tp_mults:
                    if tp_m < sl_m * 1.5:  # minimum R:R 1.5:1
                        count += len(trail_mults) * len(cooldowns)
                        continue
                    for tr_m in trail_mults:
                        for cd in cooldowns:
                            trades = run_backtest(df, signals,
                                                  sl_atr_mult=sl_m, tp_atr_mult=tp_m,
                                                  trail_atr_mult=tr_m,
                                                  trail_activate_atr=trail_activates[0] if tr_m < 50 else 99,
                                                  max_hold_bars=96, cooldown_bars=cd)
                            m = calc_metrics(trades, total_bars)
                            count += 1

                            if m["total"] >= 10:
                                r = {
                                    "weights": ws["name"], "threshold": thr,
                                    "sl": sl_m, "tp": tp_m, "trail": tr_m, "cd": cd,
                                    **m
                                }
                                results.append(r)

                                if best is None or m["net_pnl"] > best_metrics["net_pnl"]:
                                    best = r
                                    best_metrics = m

                            if count % 100 == 0:
                                print(f"    {count}/{total_combos} tested...", end="\r", flush=True)

    print(f"    {count}/{total_combos} done!                    ")
    return results, best


# ---- Report ----

def generate_report(df, best_result, all_results, trades, metrics, buy_hold):
    md = []
    md.append("# BTC 15m Composite Strategy -- Final Backtest Report")
    md.append(f"\n**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    md.append(f"**Symbol:** {SYMBOL} (Binance Futures Perpetual)")
    md.append(f"**Timeframe:** 15m")
    md.append(f"**Equity:** ${INIT_EQUITY:,.0f} | Position: ${BUDGET_USDT:,.0f} | Leverage: {LEVERAGE}x")
    md.append(f"**Fees:** Maker {FEE_BPS} bps + Slippage {SLIP_BPS} bps (round-trip: {(FEE+SLIP)*2*100:.3f}%)")

    # Data coverage
    ts_min = df["ts"].min()
    ts_max = df["ts"].max()
    md.append(f"\n**Data Period:** {str(ts_min)[:16]} to {str(ts_max)[:16]}")
    md.append(f"**Total Candles:** {len(df):,}")

    avail = []
    for col in ["oi_usdt", "buy_sell_ratio", "gl_ac_ratio", "fg_score", "whale_net", "liq_net", "etf_flow", "fr_8h", "last_funding_rate"]:
        if col in df.columns:
            pct = df[col].notna().mean() * 100
            if pct > 0:
                avail.append(f"{col} ({pct:.0f}%)")
    md.append(f"**DB Features Available:** {', '.join(avail)}")

    # Benchmark
    md.append("\n## Benchmark")
    md.append(f"- Buy & Hold: **{buy_hold['return_pct']:+.2f}%** (${buy_hold['start_price']:,.0f} -> ${buy_hold['end_price']:,.0f})")
    md.append(f"- Buy & Hold Max Drawdown: **{buy_hold['max_drawdown_pct']:.2f}%**")

    # Best result
    md.append("\n## Best Strategy Configuration")
    md.append(f"| Parameter | Value |")
    md.append(f"|-----------|-------|")
    md.append(f"| Weight Set | {best_result['weights']} |")
    md.append(f"| Signal Threshold | {best_result['threshold']} |")
    md.append(f"| SL (ATR mult) | {best_result['sl']} |")
    md.append(f"| TP (ATR mult) | {best_result['tp']} |")
    md.append(f"| Trail (ATR mult) | {best_result['trail']} |")
    md.append(f"| Cooldown (bars) | {best_result['cd']} |")

    # Performance
    md.append("\n## Performance")
    md.append(f"| Metric | Value |")
    md.append(f"|--------|-------|")
    md.append(f"| Total Trades | {metrics['total']} |")
    md.append(f"| Win Rate | {metrics['win_rate']:.2f}% |")
    md.append(f"| Profit Factor | {metrics['pf']:.3f} |")
    md.append(f"| Net PnL | ${metrics['net_pnl']:+,.2f} ({metrics['net_pct']:+.2f}%) |")
    md.append(f"| Final Equity | ${metrics['final_eq']:,.2f} |")
    md.append(f"| Max Drawdown | {metrics['max_dd']:.2f}% |")
    md.append(f"| Sharpe Ratio | {metrics['sharpe']:.3f} |")
    md.append(f"| Risk/Reward | {metrics['rr']:.3f} |")
    md.append(f"| Avg PnL/Trade | ${metrics['avg_pnl']:.2f} |")
    md.append(f"| Avg Win | ${metrics['avg_win']:.2f} |")
    md.append(f"| Avg Loss | -${metrics['avg_loss']:.2f} |")
    md.append(f"| Exposure | {metrics['exposure']:.2f}% |")
    md.append(f"| Long Trades | {metrics['n_long']} (WR: {metrics['wr_long']:.1f}%) |")
    md.append(f"| Short Trades | {metrics['n_short']} (WR: {metrics['wr_short']:.1f}%) |")
    md.append(f"| vs Buy & Hold | {metrics['net_pct'] - buy_hold['return_pct']:+.2f}pp |")

    # Exit analysis
    if not trades.empty:
        md.append("\n## Exit Analysis")
        md.append("| Exit Reason | Count | % | Avg PnL | WR | Total PnL |")
        md.append("|-------------|-------|---|---------|-----|-----------|")
        for reason, grp in trades.groupby("exit_reason"):
            cnt = len(grp)
            pct = cnt / len(trades) * 100
            avg = grp["pnl_net"].mean()
            wr = (grp["pnl_net"] > 0).mean() * 100
            total = grp["pnl_net"].sum()
            md.append(f"| {reason} | {cnt} | {pct:.1f}% | ${avg:+.2f} | {wr:.0f}% | ${total:+,.2f} |")

    # Top 10 grid results
    md.append("\n## Top 10 Parameter Combinations")
    md.append("| # | Weights | Thr | SL | TP | Trail | CD | Trades | WR% | PF | Net PnL | DD% |")
    md.append("|---|---------|-----|----|----|-------|----|--------|-----|-----|---------|-----|")
    sorted_results = sorted(all_results, key=lambda x: x["net_pnl"], reverse=True)[:10]
    for i, r in enumerate(sorted_results, 1):
        md.append(f"| {i} | {r['weights']} | {r['threshold']} | {r['sl']} | {r['tp']} | {r['trail']} | {r['cd']} | {r['total']} | {r['win_rate']:.1f}% | {r['pf']:.2f} | ${r['net_pnl']:+,.2f} | {r['max_dd']:.1f}% |")

    # Notes
    md.append("\n## Data Sources Used")
    md.append("| Source | Table | Usage |")
    md.append("|--------|-------|-------|")
    md.append("| Open Interest | market_data.open_interest | OI divergence with price |")
    md.append("| Taker Volume | market_data.taker_volume | Buy/sell pressure ratio |")
    md.append("| Long/Short Ratio | market_data.long_short_ratio | Positioning imbalance |")
    md.append("| Funding Rate | public.funding_rate | Leverage cost extremes |")
    md.append("| Fear & Greed | public.fear_greed | Sentiment extreme |")
    md.append("| Whale Alerts | public.whale_alert | Large transfers |")
    md.append("| Liquidation | public.liquidation | Cascade reversals |")
    md.append("| ETF Flows | public.etf_btc | Institutional flow |")

    return "\n".join(md)


# ---- Main ----

def main():
    print("=" * 60)
    print("BTC 15m Composite Strategy Backtest")
    print("=" * 60)

    # 1. Load data
    print("\n[1/4] Loading data...")
    ohlcv = load_ohlcv_15m()
    print(f"  OHLCV: {len(ohlcv):,} candles ({ohlcv['date_time'].iloc[0]} to {ohlcv['date_time'].iloc[-1]})")

    db_data = load_db_data()

    # 2. Build features
    print("\n[2/4] Building features...")
    df = build_features(ohlcv, db_data)

    # Trim to period where DB data is available (Sep 2025+)
    db_start = pd.Timestamp("2025-09-15")
    df = df[df["ts"] >= db_start].reset_index(drop=True)
    print(f"  Final dataset: {len(df):,} rows ({df['ts'].iloc[0]} to {df['ts'].iloc[-1]})")

    # Feature coverage
    for col in ["oi_usdt", "buy_sell_ratio", "gl_ac_ratio", "fg_score", "whale_net", "liq_net", "etf_flow", "fr_8h"]:
        if col in df.columns:
            pct = df[col].notna().mean() * 100
            print(f"    {col}: {pct:.1f}% coverage")

    # ---- Walk-Forward: Train / Test Split ----
    # Train: Sep 2025 - Jan 2026 (~4.5 months)
    # Test:  Feb 2026 - Mar 2026 (~1.5 months, unseen)
    split_date = pd.Timestamp("2026-02-01")
    df_train = df[df["ts"] < split_date].reset_index(drop=True)
    df_test = df[df["ts"] >= split_date].reset_index(drop=True)
    print(f"\n  Train set: {len(df_train):,} bars ({df_train['ts'].iloc[0]} to {df_train['ts'].iloc[-1]})")
    print(f"  Test set:  {len(df_test):,} bars ({df_test['ts'].iloc[0]} to {df_test['ts'].iloc[-1]})")

    # Buy & Hold benchmarks
    def calc_buy_hold(d, label=""):
        bh = {
            "start_price": d["close"].iloc[0],
            "end_price": d["close"].iloc[-1],
            "return_pct": (d["close"].iloc[-1] / d["close"].iloc[0] - 1) * 100,
            "max_drawdown_pct": ((d["close"] / d["close"].cummax() - 1).min()) * 100,
        }
        print(f"  Buy & Hold {label}: {bh['return_pct']:+.2f}% (${bh['start_price']:,.0f} -> ${bh['end_price']:,.0f})")
        return bh

    buy_hold_full = calc_buy_hold(df, "Full")
    buy_hold_train = calc_buy_hold(df_train, "Train")
    buy_hold_test = calc_buy_hold(df_test, "Test")

    # 3. Grid search on TRAIN set only
    print("\n[3/5] Running grid search on TRAIN set...")
    all_results, best = grid_search(df_train, len(df_train))

    if best is None:
        print("ERROR: No valid results found.")
        sys.exit(1)

    print(f"\n  Best (train): {best['weights']} thr={best['threshold']} SL={best['sl']} TP={best['tp']} trail={best['trail']} cd={best['cd']}")
    print(f"  -> {best['total']} trades, WR={best['win_rate']:.1f}%, PnL=${best['net_pnl']:+,.2f}, PF={best['pf']:.3f}")

    # 4. Validate best config on TEST set (out-of-sample)
    print("\n[4/5] Validating on TEST set (out-of-sample)...")

    # Reconstruct weight params from best
    weight_sets = {
        "balanced": {"w_oi_bull": 1.0, "w_oi_capit": 0.5, "w_oi_weak": 0.5, "w_oi_bear": 1.0,
                     "w_taker_strong": 2.0, "w_taker_mild": 1.0, "w_ls_extreme": 1.0,
                     "w_fr_neg": 1.5, "w_fr_pos": 1.5, "w_fg_fear": 1.5, "w_fg_mild_fear": 0.5,
                     "w_fg_greed": 1.5, "w_fg_mild_greed": 0.5, "w_whale_bull": 1.0, "w_whale_bear": 1.0,
                     "w_liq_bull": 1.5, "w_liq_bear": 1.5, "w_etf_bull": 1.0, "w_etf_bear": 1.0},
        "taker_heavy": {"w_oi_bull": 0.5, "w_oi_capit": 0.3, "w_oi_weak": 0.3, "w_oi_bear": 0.5,
                        "w_taker_strong": 3.0, "w_taker_mild": 1.5, "w_ls_extreme": 0.5,
                        "w_fr_neg": 1.0, "w_fr_pos": 1.0, "w_fg_fear": 1.0, "w_fg_mild_fear": 0.3,
                        "w_fg_greed": 1.0, "w_fg_mild_greed": 0.3, "w_whale_bull": 0.5, "w_whale_bear": 0.5,
                        "w_liq_bull": 1.0, "w_liq_bear": 1.0, "w_etf_bull": 0.5, "w_etf_bear": 0.5},
        "sentiment": {"w_oi_bull": 0.5, "w_oi_capit": 0.5, "w_oi_weak": 0.5, "w_oi_bear": 0.5,
                      "w_taker_strong": 1.5, "w_taker_mild": 0.5, "w_ls_extreme": 1.5,
                      "w_fr_neg": 2.0, "w_fr_pos": 2.0, "w_fg_fear": 2.0, "w_fg_mild_fear": 1.0,
                      "w_fg_greed": 2.0, "w_fg_mild_greed": 1.0, "w_whale_bull": 1.5, "w_whale_bear": 1.5,
                      "w_liq_bull": 2.0, "w_liq_bear": 2.0, "w_etf_bull": 1.5, "w_etf_bear": 1.5},
    }
    sig_params = {**weight_sets[best["weights"]], "threshold": best["threshold"]}

    # Run on train
    signals_train, _ = generate_composite_signal(df_train, sig_params)
    trades_train = run_backtest(df_train, signals_train,
                                sl_atr_mult=best["sl"], tp_atr_mult=best["tp"],
                                trail_atr_mult=best["trail"],
                                trail_activate_atr=0.8 if best["trail"] < 50 else 99,
                                cooldown_bars=best["cd"])
    metrics_train = calc_metrics(trades_train, len(df_train))

    # Run on test (out-of-sample)
    signals_test, _ = generate_composite_signal(df_test, sig_params)
    trades_test = run_backtest(df_test, signals_test,
                               sl_atr_mult=best["sl"], tp_atr_mult=best["tp"],
                               trail_atr_mult=best["trail"],
                               trail_activate_atr=0.8 if best["trail"] < 50 else 99,
                               cooldown_bars=best["cd"])
    metrics_test = calc_metrics(trades_test, len(df_test))

    # Run on full period
    signals_full, scores_full = generate_composite_signal(df, sig_params)
    trades_full = run_backtest(df, signals_full,
                                sl_atr_mult=best["sl"], tp_atr_mult=best["tp"],
                                trail_atr_mult=best["trail"],
                                trail_activate_atr=0.8 if best["trail"] < 50 else 99,
                                cooldown_bars=best["cd"])
    metrics_full = calc_metrics(trades_full, len(df))

    print(f"\n  --- TRAIN (in-sample) ---")
    print(f"  Trades: {metrics_train['total']}, WR: {metrics_train['win_rate']:.1f}%, PF: {metrics_train['pf']:.3f}, PnL: ${metrics_train['net_pnl']:+,.2f}")
    print(f"\n  --- TEST (out-of-sample) ---")
    print(f"  Trades: {metrics_test['total']}, WR: {metrics_test['win_rate']:.1f}%, PF: {metrics_test['pf']:.3f}, PnL: ${metrics_test['net_pnl']:+,.2f}")
    print(f"\n  --- FULL PERIOD ---")
    print(f"  Trades: {metrics_full['total']}, WR: {metrics_full['win_rate']:.1f}%, PF: {metrics_full['pf']:.3f}, PnL: ${metrics_full['net_pnl']:+,.2f}")

    # 5. Generate report
    print("\n[5/5] Generating final report...")
    report = generate_report(df, best, all_results, trades_full, metrics_full, buy_hold_full)

    # Append walk-forward section to report
    wf_section = []
    wf_section.append("\n\n## Walk-Forward Validation (Bias Check)")
    wf_section.append("\nGrid search was run on TRAIN set only. Best parameters were then tested on unseen TEST set.")
    wf_section.append(f"\n**Split Date:** {split_date.strftime('%Y-%m-%d')}")
    wf_section.append(f"\n| Metric | TRAIN (in-sample) | TEST (out-of-sample) | FULL |")
    wf_section.append(f"|--------|-------------------|----------------------|------|")
    wf_section.append(f"| Period | Sep 2025 - Jan 2026 | Feb 2026 - Mar 2026 | Sep 2025 - Mar 2026 |")
    wf_section.append(f"| Bars | {len(df_train):,} | {len(df_test):,} | {len(df):,} |")
    wf_section.append(f"| Trades | {metrics_train['total']} | {metrics_test['total']} | {metrics_full['total']} |")
    wf_section.append(f"| Win Rate | {metrics_train['win_rate']:.1f}% | {metrics_test['win_rate']:.1f}% | {metrics_full['win_rate']:.1f}% |")
    wf_section.append(f"| Profit Factor | {metrics_train['pf']:.3f} | {metrics_test['pf']:.3f} | {metrics_full['pf']:.3f} |")
    wf_section.append(f"| Net PnL | ${metrics_train['net_pnl']:+,.2f} | ${metrics_test['net_pnl']:+,.2f} | ${metrics_full['net_pnl']:+,.2f} |")
    wf_section.append(f"| Max Drawdown | {metrics_train['max_dd']:.2f}% | {metrics_test['max_dd']:.2f}% | {metrics_full['max_dd']:.2f}% |")
    wf_section.append(f"| Sharpe | {metrics_train['sharpe']:.3f} | {metrics_test['sharpe']:.3f} | {metrics_full['sharpe']:.3f} |")
    wf_section.append(f"| R:R | {metrics_train['rr']:.3f} | {metrics_test['rr']:.3f} | {metrics_full['rr']:.3f} |")
    wf_section.append(f"| Buy & Hold | {buy_hold_train['return_pct']:+.2f}% | {buy_hold_test['return_pct']:+.2f}% | {buy_hold_full['return_pct']:+.2f}% |")

    oos_profitable = metrics_test['net_pnl'] > 0
    wf_section.append(f"\n**Out-of-Sample Result:** {'PROFITABLE' if oos_profitable else 'NOT PROFITABLE'} -- ${metrics_test['net_pnl']:+,.2f}")
    if oos_profitable:
        wf_section.append("The strategy generalizes to unseen data, suggesting the edge is real (not overfit).")
    else:
        wf_section.append("The strategy does NOT generalize. The in-sample result was likely overfit.")

    wf_section.append("\n## Bias Fixes Applied")
    wf_section.append("1. **ETF Flow:** Shifted timestamps +1 day (data only available after US market close)")
    wf_section.append("2. **Liquidation:** Shifted timestamps +1 hour (hourly data available at end of hour)")
    wf_section.append("3. **Walk-Forward:** Grid search on train set only, validated on unseen test set")

    report += "\n".join(wf_section)

    report_path = "backtest_15m_composite_report.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\n  Report: {report_path}")

    if not trades_full.empty:
        trades_full.to_csv("backtest_details/trades_15m_composite_best.csv", index=False)
        print("  Trades: backtest_details/trades_15m_composite_best.csv")

    if not trades_test.empty:
        trades_test.to_csv("backtest_details/trades_15m_composite_test.csv", index=False)
        print("  Test trades: backtest_details/trades_15m_composite_test.csv")

    # Save all results
    pd.DataFrame(all_results).to_csv("backtest_details/grid_search_results.csv", index=False)
    print("  Grid results: backtest_details/grid_search_results.csv")

    print("\nDone!")


if __name__ == "__main__":
    main()
