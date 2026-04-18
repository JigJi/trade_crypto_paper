"""
BTC 15m Composite Strategy Backtest -- V2 (Improved)
=====================================================
Improvements over V1:
  1. 1H trend filter (higher TF confluence)
  2. Time-of-day filter (London/NY session)
  3. Dynamic position sizing (score-based)
  4. Wider trailing stop grid
  5. Walk-forward validation (train/test split)
  6. Bias fixes (ETF +1d, liquidation +1h)
"""

import os, sys, warnings
from datetime import datetime

import numpy as np
import pandas as pd
import pandas_ta as ta
import psycopg2
from dotenv import load_dotenv

warnings.filterwarnings("ignore")
load_dotenv()

# ---- Config ----
SYMBOL = "BTCUSDT"
TF_MINUTES = 15
INIT_EQUITY = 10_000.0
BUDGET_USDT = 1_000.0
LEVERAGE = 1.0

FEE_BPS = 2.0
SLIP_BPS = 1.5
FEE = FEE_BPS / 10_000
SLIP = SLIP_BPS / 10_000

DB_PARAMS = {
    "dbname": os.getenv("PG_DB", "smart_trading"),
    "user": os.getenv("PG_USER", "postgres"),
    "password": os.getenv("PG_PASS", "P@ssw0rd"),
    "host": os.getenv("PG_HOST", "localhost"),
    "port": os.getenv("PG_PORT", "5432"),
}


# ---- Data Loading (same as V1 with bias fixes) ----

def load_ohlcv_15m() -> pd.DataFrame:
    cache_file = "data_cache/BTCUSDT_15m_365d.parquet"
    if not os.path.exists(cache_file):
        print("ERROR: Run backtest_tf_compare.py first to cache 15m data")
        sys.exit(1)
    df = pd.read_parquet(cache_file)
    df["date_time"] = pd.to_datetime(df["date_time"])
    return df.sort_values("date_time").reset_index(drop=True)


def load_db_data() -> dict:
    conn = psycopg2.connect(**DB_PARAMS)
    data = {}

    print("  Loading OI...", end="", flush=True)
    data["oi"] = pd.read_sql("SELECT ts, oi_usdt, mark_price FROM market_data.open_interest WHERE symbol='BTCUSDT' ORDER BY ts", conn, parse_dates=["ts"])
    data["oi"]["ts"] = data["oi"]["ts"].dt.tz_localize(None)
    print(f" {len(data['oi']):,}")

    print("  Loading taker volume...", end="", flush=True)
    data["taker"] = pd.read_sql("SELECT ts, buy_vol, sell_vol, buy_sell_ratio FROM market_data.taker_volume WHERE symbol='BTCUSDT' ORDER BY ts", conn, parse_dates=["ts"])
    print(f" {len(data['taker']):,}")

    print("  Loading long/short ratio...", end="", flush=True)
    data["ls_ratio"] = pd.read_sql("SELECT ts, gl_ac_ratio, top_ac_ratio, top_po_ratio FROM market_data.long_short_ratio WHERE symbol='BTCUSDT' ORDER BY ts", conn, parse_dates=["ts"])
    print(f" {len(data['ls_ratio']):,}")

    print("  Loading premium index...", end="", flush=True)
    data["premium"] = pd.read_sql("SELECT ts, last_funding_rate, premium FROM market_data.premium_index WHERE symbol='BTCUSDT' ORDER BY ts", conn, parse_dates=["ts"])
    data["premium"]["ts"] = data["premium"]["ts"].dt.tz_localize(None)
    data["premium"]["last_funding_rate"] = data["premium"]["last_funding_rate"].astype(float)
    data["premium"]["premium"] = data["premium"]["premium"].astype(float)
    print(f" {len(data['premium']):,}")

    print("  Loading fear & greed...", end="", flush=True)
    data["fg"] = pd.read_sql("SELECT created_at as ts, score, description FROM public.fear_greed ORDER BY created_at", conn, parse_dates=["ts"])
    data["fg"]["ts"] = data["fg"]["ts"].dt.tz_localize(None)
    print(f" {len(data['fg']):,}")

    print("  Loading whale alerts...", end="", flush=True)
    data["whale"] = pd.read_sql("SELECT alert_time as ts, fire_level, usd_value, sentiment, direction FROM public.whale_alert WHERE symbol='BTC' ORDER BY alert_time", conn, parse_dates=["ts"])
    data["whale"]["ts"] = data["whale"]["ts"].dt.tz_localize(None)
    data["whale"]["usd_value"] = data["whale"]["usd_value"].astype(float)
    print(f" {len(data['whale']):,}")

    print("  Loading liquidations...", end="", flush=True)
    data["liq"] = pd.read_sql("SELECT created_at as ts, liq_long_1h, liq_short_1h FROM public.liquidation WHERE coin='BTC' ORDER BY created_at", conn, parse_dates=["ts"])
    data["liq"]["ts"] = data["liq"]["ts"].dt.tz_localize(None)
    data["liq"]["ts"] = data["liq"]["ts"] + pd.Timedelta("1h")  # bias fix
    print(f" {len(data['liq']):,}")

    print("  Loading ETF flows...", end="", flush=True)
    data["etf"] = pd.read_sql("SELECT date as ts, total FROM public.etf_btc ORDER BY date", conn, parse_dates=["ts"])
    data["etf"]["ts"] = data["etf"]["ts"] + pd.Timedelta("1d")  # bias fix
    print(f" {len(data['etf']):,}")

    print("  Loading funding rate...", end="", flush=True)
    data["funding"] = pd.read_sql("SELECT date as ts, funding_rate FROM public.funding_rate WHERE symbol='BTCUSDT' ORDER BY date", conn, parse_dates=["ts"])
    data["funding"]["ts"] = data["funding"]["ts"].dt.tz_localize(None)
    print(f" {len(data['funding']):,}")

    conn.close()
    return data


def resample_to_15m(df, ts_col, value_cols, agg="last"):
    d = df.set_index(ts_col).sort_index()
    if agg == "last":
        return d[value_cols].resample("15min").last().dropna(how="all").reset_index()
    elif agg == "sum":
        return d[value_cols].resample("15min").sum().reset_index()
    elif agg == "mean":
        return d[value_cols].resample("15min").mean().dropna(how="all").reset_index()
    return d[value_cols].resample("15min").last().dropna(how="all").reset_index()


def build_features(ohlcv, db_data):
    df = ohlcv.copy()
    df = df.rename(columns={"date_time": "ts"})

    # --- 15m Technical indicators ---
    df["ema9"] = df["close"].ewm(span=9, adjust=False).mean()
    df["ema21"] = df["close"].ewm(span=21, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
    df["rsi"] = ta.rsi(df["close"], length=14)
    df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=14)
    df["vol_ma"] = df["volume"].rolling(20).mean()
    df["vol_ratio"] = df["volume"] / df["vol_ma"]
    df["ret"] = df["close"].pct_change()

    # --- [NEW] 1H trend indicators from 15m data ---
    df_1h = df.set_index("ts")[["open", "high", "low", "close", "volume"]].resample("1h").agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
    }).dropna()
    df_1h["ema9_1h"] = df_1h["close"].ewm(span=9, adjust=False).mean()
    df_1h["ema21_1h"] = df_1h["close"].ewm(span=21, adjust=False).mean()
    df_1h["ema50_1h"] = df_1h["close"].ewm(span=50, adjust=False).mean()
    df_1h_feat = df_1h[["ema9_1h", "ema21_1h", "ema50_1h"]].reset_index()
    df_1h_feat = df_1h_feat.rename(columns={"ts": "ts"})
    df = pd.merge_asof(df.sort_values("ts"), df_1h_feat.sort_values("ts"),
                       on="ts", direction="backward", tolerance=pd.Timedelta("2h"))

    # --- [NEW] Extract hour for time-of-day filter ---
    df["hour"] = df["ts"].dt.hour

    # --- DB features (same as V1) ---
    if "oi" in db_data and len(db_data["oi"]) > 0:
        oi = resample_to_15m(db_data["oi"], "ts", ["oi_usdt"])
        oi["oi_chg"] = oi["oi_usdt"].pct_change()
        oi["oi_chg_ma"] = oi["oi_chg"].rolling(12).mean()
        df = pd.merge_asof(df.sort_values("ts"), oi.sort_values("ts"),
                           on="ts", direction="backward", tolerance=pd.Timedelta("30min"))

    if "taker" in db_data and len(db_data["taker"]) > 0:
        taker = resample_to_15m(db_data["taker"], "ts", ["buy_vol", "sell_vol", "buy_sell_ratio"])
        taker["net_taker"] = taker["buy_vol"] - taker["sell_vol"]
        taker["net_taker_ma"] = taker["net_taker"].rolling(12).mean()
        taker["bsr_ma"] = taker["buy_sell_ratio"].rolling(12).mean()
        df = pd.merge_asof(df.sort_values("ts"), taker.sort_values("ts"),
                           on="ts", direction="backward", tolerance=pd.Timedelta("30min"))

    if "ls_ratio" in db_data and len(db_data["ls_ratio"]) > 0:
        ls = resample_to_15m(db_data["ls_ratio"], "ts", ["gl_ac_ratio", "top_po_ratio"])
        ls["ls_chg"] = ls["gl_ac_ratio"].pct_change()
        df = pd.merge_asof(df.sort_values("ts"), ls.sort_values("ts"),
                           on="ts", direction="backward", tolerance=pd.Timedelta("30min"))

    if "premium" in db_data and len(db_data["premium"]) > 0:
        prem = resample_to_15m(db_data["premium"], "ts", ["last_funding_rate", "premium"])
        df = pd.merge_asof(df.sort_values("ts"), prem.sort_values("ts"),
                           on="ts", direction="backward", tolerance=pd.Timedelta("30min"))

    if "fg" in db_data and len(db_data["fg"]) > 0:
        fg = db_data["fg"][["ts", "score"]].copy()
        fg = fg.rename(columns={"score": "fg_score"})
        df = pd.merge_asof(df.sort_values("ts"), fg.sort_values("ts"),
                           on="ts", direction="backward", tolerance=pd.Timedelta("2d"))

    if "whale" in db_data and len(db_data["whale"]) > 0:
        whale = db_data["whale"].copy()
        whale["bull_val"] = np.where(whale["sentiment"] == "bullish", whale["usd_value"], 0)
        whale["bear_val"] = np.where(whale["sentiment"] == "bearish", whale["usd_value"], 0)
        whale_agg = whale.set_index("ts").resample("15min").agg({
            "bull_val": "sum", "bear_val": "sum"
        }).reset_index()
        whale_agg["whale_net"] = whale_agg["bull_val"] - whale_agg["bear_val"]
        whale_agg["whale_net_ma"] = whale_agg["whale_net"].rolling(8).mean()
        df = pd.merge_asof(df.sort_values("ts"), whale_agg.sort_values("ts"),
                           on="ts", direction="backward", tolerance=pd.Timedelta("30min"))

    if "liq" in db_data and len(db_data["liq"]) > 0:
        liq = db_data["liq"][["ts", "liq_long_1h", "liq_short_1h"]].copy()
        liq["liq_net"] = liq["liq_short_1h"] - liq["liq_long_1h"]
        liq["liq_total"] = liq["liq_long_1h"] + liq["liq_short_1h"]
        liq["liq_total_ma"] = liq["liq_total"].rolling(24).mean()
        df = pd.merge_asof(df.sort_values("ts"), liq.sort_values("ts"),
                           on="ts", direction="backward", tolerance=pd.Timedelta("2h"))

    if "etf" in db_data and len(db_data["etf"]) > 0:
        etf = db_data["etf"][["ts", "total"]].copy()
        etf = etf.rename(columns={"total": "etf_flow"})
        etf["etf_flow_ma"] = etf["etf_flow"].rolling(5).mean()
        df = pd.merge_asof(df.sort_values("ts"), etf.sort_values("ts"),
                           on="ts", direction="backward", tolerance=pd.Timedelta("3d"))

    if "funding" in db_data and len(db_data["funding"]) > 0:
        fr = db_data["funding"][["ts", "funding_rate"]].copy()
        fr = fr.rename(columns={"funding_rate": "fr_8h"})
        fr["fr_ma"] = fr["fr_8h"].rolling(3).mean()
        df = pd.merge_asof(df.sort_values("ts"), fr.sort_values("ts"),
                           on="ts", direction="backward", tolerance=pd.Timedelta("12h"))

    return df.sort_values("ts").reset_index(drop=True)


# ---- Composite Signal Generation (V2) ----

def generate_composite_signal(df, params):
    score = pd.Series(0.0, index=df.index)

    # Factor 1: OI Divergence
    if "oi_chg" in df.columns:
        oi_chg = df["oi_chg"].fillna(0)
        price_ret = df["ret"].fillna(0)
        score += np.where((price_ret > 0.001) & (oi_chg > 0.002), params.get("w_oi_bull", 1.0), 0)
        score += np.where((price_ret < -0.001) & (oi_chg < -0.002), params.get("w_oi_capit", 0.5), 0)
        score += np.where((price_ret > 0.001) & (oi_chg < -0.002), -params.get("w_oi_weak", 0.5), 0)
        score += np.where((price_ret < -0.001) & (oi_chg > 0.002), -params.get("w_oi_bear", 1.0), 0)

    # Factor 2: Taker Buy/Sell Ratio
    if "buy_sell_ratio" in df.columns:
        bsr = df["buy_sell_ratio"].fillna(1.0)
        score += np.where(bsr > 1.5, params.get("w_taker_strong", 2.0), 0)
        score += np.where((bsr > 1.2) & (bsr <= 1.5), params.get("w_taker_mild", 1.0), 0)
        score += np.where(bsr < 0.7, -params.get("w_taker_strong", 2.0), 0)
        score += np.where((bsr >= 0.7) & (bsr < 0.85), -params.get("w_taker_mild", 1.0), 0)

    # Factor 3: Long/Short Ratio (contrarian)
    if "gl_ac_ratio" in df.columns:
        ls = df["gl_ac_ratio"].fillna(1.0)
        score += np.where(ls > 2.5, -params.get("w_ls_extreme", 1.0), 0)
        score += np.where(ls < 0.6, params.get("w_ls_extreme", 1.0), 0)

    # Factor 4: Funding Rate
    if "fr_8h" in df.columns:
        fr = df["fr_8h"].fillna(0)
        score += np.where(fr < -0.0001, params.get("w_fr_neg", 1.5), 0)
        score += np.where(fr > 0.0003, -params.get("w_fr_pos", 1.5), 0)
    elif "last_funding_rate" in df.columns:
        fr = df["last_funding_rate"].fillna(0)
        score += np.where(fr < -0.00005, params.get("w_fr_neg", 1.5), 0)
        score += np.where(fr > 0.0002, -params.get("w_fr_pos", 1.5), 0)

    # Factor 5: Fear & Greed
    if "fg_score" in df.columns:
        fg = df["fg_score"].fillna(50)
        score += np.where(fg < 15, params.get("w_fg_fear", 1.5), 0)
        score += np.where((fg >= 15) & (fg < 25), params.get("w_fg_mild_fear", 0.5), 0)
        score += np.where(fg > 80, -params.get("w_fg_greed", 1.5), 0)
        score += np.where((fg > 65) & (fg <= 80), -params.get("w_fg_mild_greed", 0.5), 0)

    # Factor 6: Whale Net Flow
    if "whale_net" in df.columns:
        wn_ma = df["whale_net_ma"].fillna(0) if "whale_net_ma" in df.columns else df["whale_net"].fillna(0)
        score += np.where(wn_ma > 50_000_000, params.get("w_whale_bull", 1.0), 0)
        score += np.where(wn_ma < -50_000_000, -params.get("w_whale_bear", 1.0), 0)

    # Factor 7: Liquidation
    if "liq_net" in df.columns and "liq_total_ma" in df.columns:
        lt = df["liq_total"].fillna(0)
        lt_ma = df["liq_total_ma"].fillna(1)
        ln = df["liq_net"].fillna(0)
        cascade = lt > (lt_ma * 3)
        score += np.where(cascade & (ln > 0), params.get("w_liq_bull", 1.5), 0)
        score += np.where(cascade & (ln < 0), -params.get("w_liq_bear", 1.5), 0)

    # Factor 8: ETF Flows
    if "etf_flow" in df.columns:
        etf_ma = df["etf_flow_ma"].fillna(0) if "etf_flow_ma" in df.columns else df["etf_flow"].fillna(0)
        score += np.where(etf_ma > 50, params.get("w_etf_bull", 1.0), 0)
        score += np.where(etf_ma < -50, -params.get("w_etf_bear", 1.0), 0)

    # --- Price Confirmation ---
    bull_pa = (df["close"] > df["ema9"]) & (df["ema9"] > df["ema21"])
    bear_pa = (df["close"] < df["ema9"]) & (df["ema9"] < df["ema21"])
    vol_ok = df["vol_ratio"] > 0.8

    # --- [NEW] 1H Trend Filter ---
    use_1h_filter = params.get("use_1h_filter", False)
    if use_1h_filter and "ema9_1h" in df.columns and "ema21_1h" in df.columns:
        bull_1h = df["ema9_1h"] > df["ema21_1h"]
        bear_1h = df["ema9_1h"] < df["ema21_1h"]
    else:
        bull_1h = pd.Series(True, index=df.index)
        bear_1h = pd.Series(True, index=df.index)

    # --- [NEW] Time-of-Day Filter ---
    use_time_filter = params.get("use_time_filter", False)
    if use_time_filter and "hour" in df.columns:
        # London PM + NY: 13:00-22:00 UTC (widened from 14-22 to catch more)
        time_ok = (df["hour"] >= 13) & (df["hour"] <= 22)
    else:
        time_ok = pd.Series(True, index=df.index)

    # --- Generate signals ---
    threshold = params.get("threshold", 3.0)
    signal = pd.Series(0, index=df.index)
    signal[(score >= threshold) & bull_pa & vol_ok & bull_1h & time_ok] = 1
    signal[(score <= -threshold) & bear_pa & vol_ok & bear_1h & time_ok] = -1

    return signal, score


# ---- Backtest Engine V2 (with dynamic position sizing) ----

def run_backtest(df, signals, scores=None, sl_atr_mult=1.5, tp_atr_mult=3.0,
                 trail_atr_mult=0.8, trail_activate_atr=1.0,
                 max_hold_bars=96, cooldown_bars=4, use_dynamic_size=False):

    sig = signals.shift(1).fillna(0).astype(int).values
    score_vals = scores.shift(1).fillna(0).values if scores is not None else None
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

            # [NEW] Dynamic position sizing based on score magnitude
            budget = BUDGET_USDT
            if use_dynamic_size and score_vals is not None:
                abs_score = abs(score_vals[i])
                threshold = 3.0  # base threshold
                if abs_score >= threshold + 4:
                    budget = BUDGET_USDT * 2.0
                elif abs_score >= threshold + 2:
                    budget = BUDGET_USDT * 1.5
                # else: 1.0x (default)

            qty = (budget * LEVERAGE) / raw_px
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
                "sharpe": 0, "rr": 0, "avg_pnl": 0, "exposure": 0,
                "net_pct": 0, "final_eq": INIT_EQUITY, "avg_win": 0, "avg_loss": 0,
                "n_long": 0, "n_short": 0, "wr_long": 0, "wr_short": 0}

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


# ---- Grid Search V2 ----

def grid_search(df, total_bars):
    thresholds = [2.5, 3.0, 3.5, 4.0]
    sl_mults = [1.0, 1.5, 2.0]
    tp_mults = [3.0, 4.0]
    # [NEW] Expanded trail values: wider trails to improve R:R
    trail_mults = [0.5, 0.8, 1.0, 1.5, 2.0, 99]  # 99 = no trail
    trail_activates = [0.5, 0.8, 1.5]
    cooldowns = [4, 8]

    # [NEW] Filter combinations: 1H filter on/off, time filter on/off
    filter_combos = [
        {"use_1h_filter": False, "use_time_filter": False, "label": "none"},
        {"use_1h_filter": True,  "use_time_filter": False, "label": "1h"},
        {"use_1h_filter": False, "use_time_filter": True,  "label": "time"},
        {"use_1h_filter": True,  "use_time_filter": True,  "label": "1h+time"},
    ]

    # Only use sentiment weights (proven best in V1)
    weight_set = {
        "name": "sentiment",
        "w_oi_bull": 0.5, "w_oi_capit": 0.5, "w_oi_weak": 0.5, "w_oi_bear": 0.5,
        "w_taker_strong": 1.5, "w_taker_mild": 0.5, "w_ls_extreme": 1.5,
        "w_fr_neg": 2.0, "w_fr_pos": 2.0, "w_fg_fear": 2.0, "w_fg_mild_fear": 1.0,
        "w_fg_greed": 2.0, "w_fg_mild_greed": 1.0, "w_whale_bull": 1.5, "w_whale_bear": 1.5,
        "w_liq_bull": 2.0, "w_liq_bear": 2.0, "w_etf_bull": 1.5, "w_etf_bear": 1.5,
    }

    total_combos = (len(filter_combos) * len(thresholds) * len(sl_mults) *
                    len(tp_mults) * len(trail_mults) * len(cooldowns))
    print(f"\n  Grid search: {total_combos} combinations...")

    best = None
    best_metrics = None
    results = []
    count = 0

    for fc in filter_combos:
        for thr in thresholds:
            params = {**weight_set, "threshold": thr,
                      "use_1h_filter": fc["use_1h_filter"],
                      "use_time_filter": fc["use_time_filter"]}
            signals, scores = generate_composite_signal(df, params)
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
                            # Pick trail_activate based on trail value
                            if tr_m < 50:
                                ta_val = 0.5 if tr_m <= 0.5 else 0.8 if tr_m <= 1.0 else 1.5
                            else:
                                ta_val = 99

                            trades = run_backtest(df, signals, scores,
                                                  sl_atr_mult=sl_m, tp_atr_mult=tp_m,
                                                  trail_atr_mult=tr_m,
                                                  trail_activate_atr=ta_val,
                                                  cooldown_bars=cd,
                                                  use_dynamic_size=False)
                            m = calc_metrics(trades, total_bars)
                            count += 1

                            if m["total"] >= 10:
                                r = {
                                    "filters": fc["label"], "threshold": thr,
                                    "sl": sl_m, "tp": tp_m, "trail": tr_m,
                                    "trail_act": ta_val, "cd": cd,
                                    **m
                                }
                                results.append(r)

                                if best is None or m["net_pnl"] > best_metrics["net_pnl"]:
                                    best = r
                                    best_metrics = m

                            if count % 200 == 0:
                                print(f"    {count}/{total_combos} tested...", end="\r", flush=True)

    print(f"    {count}/{total_combos} done!                    ")
    return results, best


# ---- Main ----

def main():
    print("=" * 60)
    print("BTC 15m Composite Strategy V2 (Improved)")
    print("=" * 60)

    # 1. Load data
    print("\n[1/5] Loading data...")
    ohlcv = load_ohlcv_15m()
    print(f"  OHLCV: {len(ohlcv):,} candles")
    db_data = load_db_data()

    # 2. Build features (now includes 1H trend + hour)
    print("\n[2/5] Building features...")
    df = build_features(ohlcv, db_data)
    db_start = pd.Timestamp("2025-09-15")
    df = df[df["ts"] >= db_start].reset_index(drop=True)
    print(f"  Final dataset: {len(df):,} rows ({df['ts'].iloc[0]} to {df['ts'].iloc[-1]})")

    # Verify 1H features
    for col in ["ema9_1h", "ema21_1h", "hour"]:
        if col in df.columns:
            pct = df[col].notna().mean() * 100
            print(f"    {col}: {pct:.1f}% coverage")

    # Train/Test split
    split_date = pd.Timestamp("2026-02-01")
    df_train = df[df["ts"] < split_date].reset_index(drop=True)
    df_test = df[df["ts"] >= split_date].reset_index(drop=True)
    print(f"\n  Train: {len(df_train):,} bars | Test: {len(df_test):,} bars")

    def calc_buy_hold(d):
        return {
            "start_price": d["close"].iloc[0], "end_price": d["close"].iloc[-1],
            "return_pct": (d["close"].iloc[-1] / d["close"].iloc[0] - 1) * 100,
            "max_drawdown_pct": ((d["close"] / d["close"].cummax() - 1).min()) * 100,
        }

    bh_train = calc_buy_hold(df_train)
    bh_test = calc_buy_hold(df_test)
    bh_full = calc_buy_hold(df)
    print(f"  B&H Train: {bh_train['return_pct']:+.2f}% | Test: {bh_test['return_pct']:+.2f}% | Full: {bh_full['return_pct']:+.2f}%")

    # 3. Grid search on TRAIN
    print("\n[3/5] Grid search on TRAIN set...")
    all_results, best = grid_search(df_train, len(df_train))

    if best is None:
        print("ERROR: No valid results found.")
        sys.exit(1)

    print(f"\n  Best (train): filters={best['filters']} thr={best['threshold']} SL={best['sl']} TP={best['tp']} trail={best['trail']} cd={best['cd']}")
    print(f"  -> {best['total']} trades, WR={best['win_rate']:.1f}%, PnL=${best['net_pnl']:+,.2f}, PF={best['pf']:.3f}, R:R={best['rr']:.3f}")

    # 4. Validate on TEST
    print("\n[4/5] Validating on TEST set...")

    weight_set = {
        "w_oi_bull": 0.5, "w_oi_capit": 0.5, "w_oi_weak": 0.5, "w_oi_bear": 0.5,
        "w_taker_strong": 1.5, "w_taker_mild": 0.5, "w_ls_extreme": 1.5,
        "w_fr_neg": 2.0, "w_fr_pos": 2.0, "w_fg_fear": 2.0, "w_fg_mild_fear": 1.0,
        "w_fg_greed": 2.0, "w_fg_mild_greed": 1.0, "w_whale_bull": 1.5, "w_whale_bear": 1.5,
        "w_liq_bull": 2.0, "w_liq_bear": 2.0, "w_etf_bull": 1.5, "w_etf_bear": 1.5,
    }

    sig_params = {
        **weight_set,
        "threshold": best["threshold"],
        "use_1h_filter": "1h" in best["filters"],
        "use_time_filter": "time" in best["filters"],
    }

    def run_eval(d, label):
        signals, scores = generate_composite_signal(d, sig_params)
        trades = run_backtest(d, signals, scores,
                              sl_atr_mult=best["sl"], tp_atr_mult=best["tp"],
                              trail_atr_mult=best["trail"],
                              trail_activate_atr=best["trail_act"],
                              cooldown_bars=best["cd"],
                              use_dynamic_size=False)
        m = calc_metrics(trades, len(d))
        print(f"  {label}: {m['total']} trades, WR={m['win_rate']:.1f}%, PF={m['pf']:.3f}, PnL=${m['net_pnl']:+,.2f}, DD={m['max_dd']:.2f}%, R:R={m['rr']:.3f}")
        return trades, m

    trades_train, m_train = run_eval(df_train, "TRAIN")
    trades_test, m_test = run_eval(df_test, "TEST ")
    trades_full, m_full = run_eval(df, "FULL ")

    # [NEW] Also test with dynamic position sizing on full set
    print("\n  --- Dynamic Position Sizing Test ---")
    signals_dyn, scores_dyn = generate_composite_signal(df, sig_params)
    trades_dyn = run_backtest(df, signals_dyn, scores_dyn,
                              sl_atr_mult=best["sl"], tp_atr_mult=best["tp"],
                              trail_atr_mult=best["trail"],
                              trail_activate_atr=best["trail_act"],
                              cooldown_bars=best["cd"],
                              use_dynamic_size=True)
    m_dyn = calc_metrics(trades_dyn, len(df))
    print(f"  DYN:  {m_dyn['total']} trades, WR={m_dyn['win_rate']:.1f}%, PF={m_dyn['pf']:.3f}, PnL=${m_dyn['net_pnl']:+,.2f}, DD={m_dyn['max_dd']:.2f}%")

    # 5. Generate report
    print("\n[5/5] Generating report...")

    md = []
    md.append("# BTC 15m Composite Strategy V2 -- Backtest Report")
    md.append(f"\n**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    md.append(f"**Symbol:** {SYMBOL} (Binance Futures Perpetual)")
    md.append(f"**Timeframe:** 15m | **Equity:** ${INIT_EQUITY:,.0f} | **Position:** ${BUDGET_USDT:,.0f} | **Leverage:** {LEVERAGE}x")
    md.append(f"**Fees:** Maker {FEE_BPS} bps + Slippage {SLIP_BPS} bps (round-trip: {(FEE+SLIP)*2*100:.3f}%)")
    md.append(f"**Period:** {df['ts'].iloc[0]} to {df['ts'].iloc[-1]} ({len(df):,} bars)")

    md.append("\n---\n")
    md.append("## V2 Improvements over V1")
    md.append("1. **1H Trend Filter** -- only long in 1H uptrend (EMA9>EMA21), short in 1H downtrend")
    md.append("2. **Time-of-Day Filter** -- only trade during London/NY session (13:00-22:00 UTC)")
    md.append("3. **Dynamic Position Sizing** -- scale position 1.5x-2.0x for stronger signals")
    md.append("4. **Wider Trailing Stop Grid** -- tested trail 0.5, 0.8, 1.0, 1.5, 2.0 ATR to improve R:R")
    md.append("5. **Bias Fixes** -- ETF flow +1d, liquidation +1h timestamp shift")
    md.append("6. **Walk-Forward Validation** -- grid search on train only, validated on unseen test set")

    md.append("\n---\n")
    md.append("## Best Configuration")
    md.append("| Parameter | Value |")
    md.append("|-----------|-------|")
    md.append(f"| Weight Set | sentiment |")
    md.append(f"| Filters | {best['filters']} |")
    md.append(f"| Signal Threshold | {best['threshold']} |")
    md.append(f"| SL (ATR mult) | {best['sl']} |")
    md.append(f"| TP (ATR mult) | {best['tp']} |")
    md.append(f"| Trail (ATR mult) | {best['trail']} |")
    md.append(f"| Trail Activate | {best['trail_act']} ATR |")
    md.append(f"| Cooldown | {best['cd']} bars |")

    md.append("\n---\n")
    md.append("## Walk-Forward Validation")
    md.append(f"\n**Split Date:** {split_date.strftime('%Y-%m-%d')}")
    md.append(f"\n| Metric | TRAIN (in-sample) | TEST (out-of-sample) | FULL |")
    md.append(f"|--------|-------------------|----------------------|------|")
    md.append(f"| Period | Sep 2025 - Jan 2026 | Feb - Mar 2026 | Sep 2025 - Mar 2026 |")
    md.append(f"| Bars | {len(df_train):,} | {len(df_test):,} | {len(df):,} |")
    md.append(f"| Trades | {m_train['total']} | {m_test['total']} | {m_full['total']} |")
    md.append(f"| Win Rate | {m_train['win_rate']:.1f}% | {m_test['win_rate']:.1f}% | {m_full['win_rate']:.1f}% |")
    md.append(f"| Profit Factor | {m_train['pf']:.3f} | {m_test['pf']:.3f} | {m_full['pf']:.3f} |")
    md.append(f"| Net PnL | ${m_train['net_pnl']:+,.2f} | ${m_test['net_pnl']:+,.2f} | ${m_full['net_pnl']:+,.2f} |")
    md.append(f"| Max Drawdown | {m_train['max_dd']:.2f}% | {m_test['max_dd']:.2f}% | {m_full['max_dd']:.2f}% |")
    md.append(f"| Sharpe | {m_train['sharpe']:.3f} | {m_test['sharpe']:.3f} | {m_full['sharpe']:.3f} |")
    md.append(f"| R:R | {m_train['rr']:.3f} | {m_test['rr']:.3f} | {m_full['rr']:.3f} |")
    md.append(f"| Avg Win | ${m_train['avg_win']:.2f} | ${m_test['avg_win']:.2f} | ${m_full['avg_win']:.2f} |")
    md.append(f"| Avg Loss | -${m_train['avg_loss']:.2f} | -${m_test['avg_loss']:.2f} | -${m_full['avg_loss']:.2f} |")
    md.append(f"| Long (WR) | {m_train['n_long']} ({m_train['wr_long']:.1f}%) | {m_test['n_long']} ({m_test['wr_long']:.1f}%) | {m_full['n_long']} ({m_full['wr_long']:.1f}%) |")
    md.append(f"| Short (WR) | {m_train['n_short']} ({m_train['wr_short']:.1f}%) | {m_test['n_short']} ({m_test['wr_short']:.1f}%) | {m_full['n_short']} ({m_full['wr_short']:.1f}%) |")
    md.append(f"| Buy & Hold | {bh_train['return_pct']:+.2f}% | {bh_test['return_pct']:+.2f}% | {bh_full['return_pct']:+.2f}% |")

    oos_ok = m_test['net_pnl'] > 0
    md.append(f"\n**Out-of-Sample: {'PROFITABLE' if oos_ok else 'NOT PROFITABLE'}** -- ${m_test['net_pnl']:+,.2f}")

    # Dynamic sizing results
    md.append("\n---\n")
    md.append("## Dynamic Position Sizing (Full Period)")
    md.append(f"| Metric | Fixed $1,000 | Dynamic (1x/1.5x/2x) |")
    md.append(f"|--------|-------------|----------------------|")
    md.append(f"| Net PnL | ${m_full['net_pnl']:+,.2f} | ${m_dyn['net_pnl']:+,.2f} |")
    md.append(f"| PF | {m_full['pf']:.3f} | {m_dyn['pf']:.3f} |")
    md.append(f"| Max DD | {m_full['max_dd']:.2f}% | {m_dyn['max_dd']:.2f}% |")
    md.append(f"| Avg PnL | ${m_full['avg_pnl']:.2f} | ${m_dyn['avg_pnl']:.2f} |")

    # Exit analysis
    if not trades_full.empty:
        md.append("\n---\n")
        md.append("## Exit Analysis (Full Period)")
        md.append("| Exit Reason | Count | % | Avg PnL | WR | Total PnL |")
        md.append("|-------------|-------|---|---------|-----|-----------|")
        for reason, grp in trades_full.groupby("exit_reason"):
            cnt = len(grp)
            pct = cnt / len(trades_full) * 100
            avg = grp["pnl_net"].mean()
            wr = (grp["pnl_net"] > 0).mean() * 100
            total = grp["pnl_net"].sum()
            md.append(f"| {reason} | {cnt} | {pct:.1f}% | ${avg:+.2f} | {wr:.0f}% | ${total:+,.2f} |")

    # Top 10
    md.append("\n---\n")
    md.append("## Top 10 Grid Results (Train Set)")
    md.append("| # | Filters | Thr | SL | TP | Trail | CD | Trades | WR% | PF | PnL | DD% | R:R |")
    md.append("|---|---------|-----|----|----|-------|----|--------|-----|-----|-----|-----|-----|")
    sorted_r = sorted(all_results, key=lambda x: x["net_pnl"], reverse=True)[:10]
    for i, r in enumerate(sorted_r, 1):
        md.append(f"| {i} | {r['filters']} | {r['threshold']} | {r['sl']} | {r['tp']} | {r['trail']} | {r['cd']} | {r['total']} | {r['win_rate']:.1f}% | {r['pf']:.2f} | ${r['net_pnl']:+,.2f} | {r['max_dd']:.1f}% | {r['rr']:.2f} |")

    # V1 vs V2 comparison
    md.append("\n---\n")
    md.append("## V1 vs V2 Comparison")
    md.append("| Metric | V1 Baseline | V2 Improved |")
    md.append("|--------|------------|-------------|")
    md.append(f"| Filters | none | {best['filters']} |")
    md.append(f"| Train PnL | $+76.28 | ${m_train['net_pnl']:+,.2f} |")
    md.append(f"| Test PnL (OOS) | $+38.95 | ${m_test['net_pnl']:+,.2f} |")
    md.append(f"| Full PnL | $+115.23 | ${m_full['net_pnl']:+,.2f} |")
    md.append(f"| Full PF | 1.532 | {m_full['pf']:.3f} |")
    md.append(f"| Full R:R | 0.662 | {m_full['rr']:.3f} |")
    md.append(f"| Full Max DD | -0.39% | {m_full['max_dd']:.2f}% |")

    report = "\n".join(md)
    report_path = "backtest_15m_composite_v2_report.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\n  Report: {report_path}")

    if not trades_full.empty:
        trades_full.to_csv("backtest_details/trades_15m_v2_full.csv", index=False)
    if not trades_test.empty:
        trades_test.to_csv("backtest_details/trades_15m_v2_test.csv", index=False)
    pd.DataFrame(all_results).to_csv("backtest_details/grid_search_v2_results.csv", index=False)

    print("\nDone!")


if __name__ == "__main__":
    main()
