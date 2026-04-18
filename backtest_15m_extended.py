"""
BTC 15m Composite Strategy -- Extended Backtest
=================================================
Extends data period to maximize available DB coverage:
  - OHLCV: 3 years from Binance (for proper indicator warmup)
  - DB data: from Jun 2025 (earliest available)
  - Effective backtest: Jun 2025 - Mar 2026 (~9 months)
  - Walk-forward: Train Jun-Dec 2025, Test Jan-Mar 2026

Uses best V2 strategy config as baseline.
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

SYMBOL = "BTCUSDT"
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


# ---- Fetch 3yr 15m OHLCV from Binance ----

def fetch_binance_15m(years=3):
    cache_file = f"data_cache/BTCUSDT_15m_{years}yr.parquet"
    if os.path.exists(cache_file):
        print(f"  Using cached {cache_file}")
        df = pd.read_parquet(cache_file)
        df["date_time"] = pd.to_datetime(df["date_time"])
        return df.sort_values("date_time").reset_index(drop=True)

    print(f"  Fetching {years}yr 15m data from Binance...")
    url = "https://fapi.binance.com/fapi/v1/klines"
    end_ts = int(datetime.now().timestamp() * 1000)
    start_ts = end_ts - years * 365 * 24 * 3600 * 1000

    all_data = []
    current = start_ts
    batch = 0
    while current < end_ts:
        params = {"symbol": SYMBOL, "interval": "15m", "startTime": current, "limit": 1500}
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


def load_db_data():
    conn = psycopg2.connect(**DB_PARAMS)
    data = {}

    print("  Loading OI...", end="", flush=True)
    data["oi"] = pd.read_sql("SELECT ts, oi_usdt FROM market_data.open_interest WHERE symbol='BTCUSDT' ORDER BY ts", conn, parse_dates=["ts"])
    data["oi"]["ts"] = data["oi"]["ts"].dt.tz_localize(None)
    print(f" {len(data['oi']):,}")

    print("  Loading taker volume...", end="", flush=True)
    data["taker"] = pd.read_sql("SELECT ts, buy_vol, sell_vol, buy_sell_ratio FROM market_data.taker_volume WHERE symbol='BTCUSDT' ORDER BY ts", conn, parse_dates=["ts"])
    print(f" {len(data['taker']):,}")

    print("  Loading long/short ratio...", end="", flush=True)
    data["ls_ratio"] = pd.read_sql("SELECT ts, gl_ac_ratio FROM market_data.long_short_ratio WHERE symbol='BTCUSDT' ORDER BY ts", conn, parse_dates=["ts"])
    print(f" {len(data['ls_ratio']):,}")

    print("  Loading premium index...", end="", flush=True)
    data["premium"] = pd.read_sql("SELECT ts, last_funding_rate, premium FROM market_data.premium_index WHERE symbol='BTCUSDT' ORDER BY ts", conn, parse_dates=["ts"])
    data["premium"]["ts"] = data["premium"]["ts"].dt.tz_localize(None)
    data["premium"]["last_funding_rate"] = data["premium"]["last_funding_rate"].astype(float)
    data["premium"]["premium"] = data["premium"]["premium"].astype(float)
    print(f" {len(data['premium']):,}")

    print("  Loading fear & greed...", end="", flush=True)
    data["fg"] = pd.read_sql("SELECT created_at as ts, score FROM public.fear_greed ORDER BY created_at", conn, parse_dates=["ts"])
    data["fg"]["ts"] = data["fg"]["ts"].dt.tz_localize(None)
    data["fg"] = data["fg"].rename(columns={"score": "fg_score"})
    print(f" {len(data['fg']):,}")

    print("  Loading whale alerts...", end="", flush=True)
    data["whale"] = pd.read_sql("SELECT alert_time as ts, usd_value, sentiment FROM public.whale_alert WHERE symbol='BTC' ORDER BY alert_time", conn, parse_dates=["ts"])
    data["whale"]["ts"] = data["whale"]["ts"].dt.tz_localize(None)
    data["whale"]["usd_value"] = data["whale"]["usd_value"].astype(float)
    print(f" {len(data['whale']):,}")

    print("  Loading liquidations...", end="", flush=True)
    data["liq"] = pd.read_sql("SELECT created_at as ts, liq_long_1h, liq_short_1h FROM public.liquidation WHERE coin='BTC' ORDER BY created_at", conn, parse_dates=["ts"])
    data["liq"]["ts"] = data["liq"]["ts"].dt.tz_localize(None)
    data["liq"]["ts"] = data["liq"]["ts"] + pd.Timedelta("1h")
    print(f" {len(data['liq']):,}")

    print("  Loading ETF flows...", end="", flush=True)
    data["etf"] = pd.read_sql("SELECT date as ts, total FROM public.etf_btc ORDER BY date", conn, parse_dates=["ts"])
    data["etf"]["ts"] = data["etf"]["ts"] + pd.Timedelta("1d")
    data["etf"] = data["etf"].rename(columns={"total": "etf_flow"})
    print(f" {len(data['etf']):,}")

    print("  Loading funding rate...", end="", flush=True)
    data["funding"] = pd.read_sql("SELECT date as ts, funding_rate FROM public.funding_rate WHERE symbol='BTCUSDT' ORDER BY date", conn, parse_dates=["ts"])
    data["funding"]["ts"] = data["funding"]["ts"].dt.tz_localize(None)
    data["funding"] = data["funding"].rename(columns={"funding_rate": "fr_8h"})
    print(f" {len(data['funding']):,}")

    conn.close()
    return data


def resample_to_15m(df, ts_col, value_cols, agg="last"):
    d = df.set_index(ts_col).sort_index()
    if agg == "last":
        return d[value_cols].resample("15min").last().dropna(how="all").reset_index()
    elif agg == "sum":
        return d[value_cols].resample("15min").sum().reset_index()
    return d[value_cols].resample("15min").last().dropna(how="all").reset_index()


def build_features(ohlcv, db_data):
    df = ohlcv.copy().rename(columns={"date_time": "ts"})

    # 15m indicators (uses full 3yr for proper warmup)
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

    df["hour"] = df["ts"].dt.hour

    # DB features
    if "oi" in db_data and len(db_data["oi"]) > 0:
        oi = resample_to_15m(db_data["oi"], "ts", ["oi_usdt"])
        oi["oi_chg"] = oi["oi_usdt"].pct_change()
        df = pd.merge_asof(df.sort_values("ts"), oi.sort_values("ts"),
                           on="ts", direction="backward", tolerance=pd.Timedelta("30min"))

    if "taker" in db_data and len(db_data["taker"]) > 0:
        taker = resample_to_15m(db_data["taker"], "ts", ["buy_vol", "sell_vol", "buy_sell_ratio"])
        df = pd.merge_asof(df.sort_values("ts"), taker.sort_values("ts"),
                           on="ts", direction="backward", tolerance=pd.Timedelta("30min"))

    if "ls_ratio" in db_data and len(db_data["ls_ratio"]) > 0:
        df = pd.merge_asof(df.sort_values("ts"),
                           db_data["ls_ratio"].sort_values("ts"),
                           on="ts", direction="backward", tolerance=pd.Timedelta("30min"))

    if "premium" in db_data and len(db_data["premium"]) > 0:
        prem = resample_to_15m(db_data["premium"], "ts", ["last_funding_rate", "premium"])
        df = pd.merge_asof(df.sort_values("ts"), prem.sort_values("ts"),
                           on="ts", direction="backward", tolerance=pd.Timedelta("30min"))

    if "fg" in db_data and len(db_data["fg"]) > 0:
        df = pd.merge_asof(df.sort_values("ts"), db_data["fg"].sort_values("ts"),
                           on="ts", direction="backward", tolerance=pd.Timedelta("2d"))

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

    return df.sort_values("ts").reset_index(drop=True)


def generate_composite_signal(df, params):
    score = pd.Series(0.0, index=df.index)

    if "oi_chg" in df.columns:
        oi_chg = df["oi_chg"].fillna(0)
        ret = df["ret"].fillna(0)
        score += np.where((ret > 0.001) & (oi_chg > 0.002), params.get("w_oi_bull", 0.5), 0)
        score += np.where((ret < -0.001) & (oi_chg < -0.002), params.get("w_oi_capit", 0.5), 0)
        score += np.where((ret > 0.001) & (oi_chg < -0.002), -params.get("w_oi_weak", 0.5), 0)
        score += np.where((ret < -0.001) & (oi_chg > 0.002), -params.get("w_oi_bear", 0.5), 0)

    if "buy_sell_ratio" in df.columns:
        bsr = df["buy_sell_ratio"].fillna(1.0)
        score += np.where(bsr > 1.5, params.get("w_taker_strong", 1.5), 0)
        score += np.where((bsr > 1.2) & (bsr <= 1.5), params.get("w_taker_mild", 0.5), 0)
        score += np.where(bsr < 0.7, -params.get("w_taker_strong", 1.5), 0)
        score += np.where((bsr >= 0.7) & (bsr < 0.85), -params.get("w_taker_mild", 0.5), 0)

    if "gl_ac_ratio" in df.columns:
        ls = df["gl_ac_ratio"].fillna(1.0)
        score += np.where(ls > 2.5, -params.get("w_ls_extreme", 1.5), 0)
        score += np.where(ls < 0.6, params.get("w_ls_extreme", 1.5), 0)

    if "fr_8h" in df.columns:
        fr = df["fr_8h"].fillna(0)
        score += np.where(fr < -0.0001, params.get("w_fr_neg", 2.0), 0)
        score += np.where(fr > 0.0003, -params.get("w_fr_pos", 2.0), 0)
    elif "last_funding_rate" in df.columns:
        fr = df["last_funding_rate"].fillna(0)
        score += np.where(fr < -0.00005, params.get("w_fr_neg", 2.0), 0)
        score += np.where(fr > 0.0002, -params.get("w_fr_pos", 2.0), 0)

    if "fg_score" in df.columns:
        fg = df["fg_score"].fillna(50)
        score += np.where(fg < 15, params.get("w_fg_fear", 2.0), 0)
        score += np.where((fg >= 15) & (fg < 25), params.get("w_fg_mild_fear", 1.0), 0)
        score += np.where(fg > 80, -params.get("w_fg_greed", 2.0), 0)
        score += np.where((fg > 65) & (fg <= 80), -params.get("w_fg_mild_greed", 1.0), 0)

    if "whale_net_ma" in df.columns:
        wn_ma = df["whale_net_ma"].fillna(0)
        score += np.where(wn_ma > 50_000_000, params.get("w_whale_bull", 1.5), 0)
        score += np.where(wn_ma < -50_000_000, -params.get("w_whale_bear", 1.5), 0)

    if "liq_net" in df.columns and "liq_total_ma" in df.columns:
        lt = df["liq_total"].fillna(0)
        lt_ma = df["liq_total_ma"].fillna(1)
        ln = df["liq_net"].fillna(0)
        cascade = lt > (lt_ma * 3)
        score += np.where(cascade & (ln > 0), params.get("w_liq_bull", 2.0), 0)
        score += np.where(cascade & (ln < 0), -params.get("w_liq_bear", 2.0), 0)

    if "etf_flow_ma" in df.columns:
        etf_ma = df["etf_flow_ma"].fillna(0)
        score += np.where(etf_ma > 50, params.get("w_etf_bull", 1.5), 0)
        score += np.where(etf_ma < -50, -params.get("w_etf_bear", 1.5), 0)

    bull_pa = (df["close"] > df["ema9"]) & (df["ema9"] > df["ema21"])
    bear_pa = (df["close"] < df["ema9"]) & (df["ema9"] < df["ema21"])
    vol_ok = df["vol_ratio"] > 0.8

    threshold = params.get("threshold", 3.0)
    signal = pd.Series(0, index=df.index)
    signal[(score >= threshold) & bull_pa & vol_ok] = 1
    signal[(score <= -threshold) & bear_pa & vol_ok] = -1
    return signal, score


def run_backtest(df, signals, sl_atr_mult=2.0, tp_atr_mult=3.0,
                 trail_atr_mult=0.5, trail_activate_atr=0.5,
                 max_hold_bars=96, cooldown_bars=4):
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
                    "qty": qty, "pnl_net": pnl_net,
                    "equity_after": equity, "exit_reason": exit_reason,
                    "holding_bars": i - entry_i,
                })
                last_exit_i = i
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


def grid_search(df, total_bars):
    """Grid search with V2 best config as center, explore nearby."""
    thresholds = [2.5, 3.0, 3.5, 4.0]
    sl_mults = [1.5, 2.0, 2.5]
    tp_mults = [3.0, 4.0]
    trail_mults = [0.5, 0.8, 1.0, 1.5, 99]
    cooldowns = [4, 8]

    # sentiment weights
    w = {
        "w_oi_bull": 0.5, "w_oi_capit": 0.5, "w_oi_weak": 0.5, "w_oi_bear": 0.5,
        "w_taker_strong": 1.5, "w_taker_mild": 0.5, "w_ls_extreme": 1.5,
        "w_fr_neg": 2.0, "w_fr_pos": 2.0, "w_fg_fear": 2.0, "w_fg_mild_fear": 1.0,
        "w_fg_greed": 2.0, "w_fg_mild_greed": 1.0, "w_whale_bull": 1.5, "w_whale_bear": 1.5,
        "w_liq_bull": 2.0, "w_liq_bear": 2.0, "w_etf_bull": 1.5, "w_etf_bear": 1.5,
    }

    total = len(thresholds) * len(sl_mults) * len(tp_mults) * len(trail_mults) * len(cooldowns)
    print(f"\n  Grid search: {total} combinations...")

    best = None
    best_m = None
    results = []
    count = 0

    for thr in thresholds:
        params = {**w, "threshold": thr}
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
                        ta_val = 0.5 if tr_m <= 0.5 else 0.8 if tr_m <= 1.0 else 1.5 if tr_m < 50 else 99
                        trades = run_backtest(df, signals,
                                              sl_atr_mult=sl_m, tp_atr_mult=tp_m,
                                              trail_atr_mult=tr_m, trail_activate_atr=ta_val,
                                              cooldown_bars=cd)
                        m = calc_metrics(trades, total_bars)
                        count += 1
                        if m["total"] >= 10:
                            r = {"threshold": thr, "sl": sl_m, "tp": tp_m,
                                 "trail": tr_m, "trail_act": ta_val, "cd": cd, **m}
                            results.append(r)
                            if best is None or m["net_pnl"] > best_m["net_pnl"]:
                                best = r
                                best_m = m
                        if count % 50 == 0:
                            print(f"    {count}/{total}...", end="\r", flush=True)

    print(f"    {count}/{total} done!          ")
    return results, best


def main():
    print("=" * 60)
    print("BTC 15m Composite -- Extended Backtest (~9 months)")
    print("=" * 60)

    # 1. Load 3yr OHLCV
    print("\n[1/5] Loading 3yr OHLCV...")
    ohlcv = fetch_binance_15m(years=3)
    print(f"  OHLCV: {len(ohlcv):,} candles ({ohlcv['date_time'].iloc[0]} to {ohlcv['date_time'].iloc[-1]})")

    # 2. Load DB data
    print("\n[2/5] Loading DB data...")
    db_data = load_db_data()

    # 3. Build features
    print("\n[3/5] Building features (3yr warmup)...")
    df = build_features(ohlcv, db_data)

    # Trim to Jun 2025+ (earliest DB data available)
    db_start = pd.Timestamp("2025-06-25")
    df = df[df["ts"] >= db_start].reset_index(drop=True)
    print(f"  Effective dataset: {len(df):,} rows ({df['ts'].iloc[0]} to {df['ts'].iloc[-1]})")

    # Coverage check
    for col in ["oi_usdt", "buy_sell_ratio", "gl_ac_ratio", "fg_score",
                "whale_net", "liq_net", "etf_flow", "fr_8h"]:
        if col in df.columns:
            pct = df[col].notna().mean() * 100
            print(f"    {col}: {pct:.1f}%")

    # Walk-Forward: Train Jun-Nov 2025, Test Dec 2025-Mar 2026
    split_date = pd.Timestamp("2025-12-01")
    df_train = df[df["ts"] < split_date].reset_index(drop=True)
    df_test = df[df["ts"] >= split_date].reset_index(drop=True)
    print(f"\n  Train: {len(df_train):,} bars ({df_train['ts'].iloc[0]} to {df_train['ts'].iloc[-1]})")
    print(f"  Test:  {len(df_test):,} bars ({df_test['ts'].iloc[0]} to {df_test['ts'].iloc[-1]})")

    def calc_bh(d):
        return {"return_pct": (d["close"].iloc[-1] / d["close"].iloc[0] - 1) * 100}
    bh_train = calc_bh(df_train)
    bh_test = calc_bh(df_test)
    bh_full = calc_bh(df)
    print(f"  B&H: Train {bh_train['return_pct']:+.2f}% | Test {bh_test['return_pct']:+.2f}% | Full {bh_full['return_pct']:+.2f}%")

    # 4. Grid search on train
    print("\n[4/5] Grid search on TRAIN...")
    all_results, best = grid_search(df_train, len(df_train))

    if best is None:
        print("ERROR: No results.")
        sys.exit(1)

    print(f"\n  Best: thr={best['threshold']} SL={best['sl']} TP={best['tp']} trail={best['trail']} cd={best['cd']}")
    print(f"  -> {best['total']} trades, WR={best['win_rate']:.1f}%, PnL=${best['net_pnl']:+,.2f}, PF={best['pf']:.3f}")

    # 5. Validate
    print("\n[5/5] Validating...")
    w = {
        "threshold": best["threshold"],
        "w_oi_bull": 0.5, "w_oi_capit": 0.5, "w_oi_weak": 0.5, "w_oi_bear": 0.5,
        "w_taker_strong": 1.5, "w_taker_mild": 0.5, "w_ls_extreme": 1.5,
        "w_fr_neg": 2.0, "w_fr_pos": 2.0, "w_fg_fear": 2.0, "w_fg_mild_fear": 1.0,
        "w_fg_greed": 2.0, "w_fg_mild_greed": 1.0, "w_whale_bull": 1.5, "w_whale_bear": 1.5,
        "w_liq_bull": 2.0, "w_liq_bear": 2.0, "w_etf_bull": 1.5, "w_etf_bear": 1.5,
    }

    def run_eval(d, label):
        signals, _ = generate_composite_signal(d, w)
        trades = run_backtest(d, signals,
                              sl_atr_mult=best["sl"], tp_atr_mult=best["tp"],
                              trail_atr_mult=best["trail"], trail_activate_atr=best["trail_act"],
                              cooldown_bars=best["cd"])
        m = calc_metrics(trades, len(d))
        print(f"  {label}: {m['total']} trades, WR={m['win_rate']:.1f}%, PF={m['pf']:.3f}, PnL=${m['net_pnl']:+,.2f}, DD={m['max_dd']:.2f}%, R:R={m['rr']:.3f}")

        # Monthly breakdown
        if not trades.empty:
            trades["month"] = pd.to_datetime(trades["entry_time"]).dt.to_period("M")
            print(f"    Monthly PnL:")
            for month, grp in trades.groupby("month"):
                n_t = len(grp)
                pnl = grp["pnl_net"].sum()
                wr_m = (grp["pnl_net"] > 0).mean() * 100
                print(f"      {month}: {n_t} trades, WR={wr_m:.0f}%, PnL=${pnl:+,.2f}")

        return trades, m

    trades_train, m_train = run_eval(df_train, "TRAIN")
    trades_test, m_test = run_eval(df_test, "TEST ")
    trades_full, m_full = run_eval(df, "FULL ")

    # Report
    md = []
    md.append("# BTC 15m Composite -- Extended Backtest Report")
    md.append(f"\n**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    md.append(f"**Period:** {df['ts'].iloc[0]} to {df['ts'].iloc[-1]} (~{(df['ts'].iloc[-1]-df['ts'].iloc[0]).days/30:.0f} months)")
    md.append(f"**OHLCV Warmup:** 3 years from Binance")
    md.append(f"**Fees:** Maker {FEE_BPS} bps + Slippage {SLIP_BPS} bps (RT: {(FEE+SLIP)*2*100:.3f}%)")

    md.append(f"\n## Config: thr={best['threshold']} SL={best['sl']} TP={best['tp']} trail={best['trail']} cd={best['cd']}")

    md.append(f"\n## Walk-Forward Results")
    md.append(f"\n| Metric | TRAIN (Jun-Nov 25) | TEST (Dec 25-Mar 26) | FULL |")
    md.append(f"|--------|-------------------|---------------------|------|")
    md.append(f"| Bars | {len(df_train):,} | {len(df_test):,} | {len(df):,} |")
    md.append(f"| Trades | {m_train['total']} | {m_test['total']} | {m_full['total']} |")
    md.append(f"| Win Rate | {m_train['win_rate']:.1f}% | {m_test['win_rate']:.1f}% | {m_full['win_rate']:.1f}% |")
    md.append(f"| PF | {m_train['pf']:.3f} | {m_test['pf']:.3f} | {m_full['pf']:.3f} |")
    md.append(f"| Net PnL | ${m_train['net_pnl']:+,.2f} | ${m_test['net_pnl']:+,.2f} | ${m_full['net_pnl']:+,.2f} |")
    md.append(f"| Max DD | {m_train['max_dd']:.2f}% | {m_test['max_dd']:.2f}% | {m_full['max_dd']:.2f}% |")
    md.append(f"| Sharpe | {m_train['sharpe']:.3f} | {m_test['sharpe']:.3f} | {m_full['sharpe']:.3f} |")
    md.append(f"| R:R | {m_train['rr']:.3f} | {m_test['rr']:.3f} | {m_full['rr']:.3f} |")
    md.append(f"| Avg Win | ${m_train['avg_win']:.2f} | ${m_test['avg_win']:.2f} | ${m_full['avg_win']:.2f} |")
    md.append(f"| Avg Loss | -${m_train['avg_loss']:.2f} | -${m_test['avg_loss']:.2f} | -${m_full['avg_loss']:.2f} |")
    md.append(f"| Long (WR) | {m_train['n_long']} ({m_train['wr_long']:.1f}%) | {m_test['n_long']} ({m_test['wr_long']:.1f}%) | {m_full['n_long']} ({m_full['wr_long']:.1f}%) |")
    md.append(f"| Short (WR) | {m_train['n_short']} ({m_train['wr_short']:.1f}%) | {m_test['n_short']} ({m_test['wr_short']:.1f}%) | {m_full['n_short']} ({m_full['wr_short']:.1f}%) |")
    md.append(f"| B&H | {bh_train['return_pct']:+.2f}% | {bh_test['return_pct']:+.2f}% | {bh_full['return_pct']:+.2f}% |")

    oos_ok = m_test['net_pnl'] > 0
    md.append(f"\n**OOS: {'PROFITABLE' if oos_ok else 'NOT PROFITABLE'}** -- ${m_test['net_pnl']:+,.2f}")

    # Monthly breakdown
    if not trades_full.empty:
        md.append("\n## Monthly Breakdown (Full)")
        md.append("| Month | Trades | WR | PnL | Cum PnL |")
        md.append("|-------|--------|-----|-----|---------|")
        trades_full["month"] = pd.to_datetime(trades_full["entry_time"]).dt.to_period("M")
        cum = 0
        for month, grp in trades_full.groupby("month"):
            pnl = grp["pnl_net"].sum()
            cum += pnl
            wr_m = (grp["pnl_net"] > 0).mean() * 100
            md.append(f"| {month} | {len(grp)} | {wr_m:.0f}% | ${pnl:+,.2f} | ${cum:+,.2f} |")

    # Exit analysis
    if not trades_full.empty:
        md.append("\n## Exit Analysis")
        md.append("| Exit | Count | % | Avg PnL | WR | Total |")
        md.append("|------|-------|---|---------|-----|-------|")
        for reason, grp in trades_full.groupby("exit_reason"):
            cnt = len(grp)
            md.append(f"| {reason} | {cnt} | {cnt/len(trades_full)*100:.1f}% | ${grp['pnl_net'].mean():+.2f} | {(grp['pnl_net']>0).mean()*100:.0f}% | ${grp['pnl_net'].sum():+,.2f} |")

    # Top 10
    md.append("\n## Top 10 Grid (Train)")
    md.append("| # | Thr | SL | TP | Trail | CD | Trades | WR% | PF | PnL | DD% |")
    md.append("|---|-----|----|----|-------|----|--------|-----|-----|-----|-----|")
    top = sorted(all_results, key=lambda x: x["net_pnl"], reverse=True)[:10]
    for i, r in enumerate(top, 1):
        md.append(f"| {i} | {r['threshold']} | {r['sl']} | {r['tp']} | {r['trail']} | {r['cd']} | {r['total']} | {r['win_rate']:.1f}% | {r['pf']:.2f} | ${r['net_pnl']:+,.2f} | {r['max_dd']:.1f}% |")

    # DB coverage note
    md.append("\n## DB Data Coverage")
    md.append("| Factor | Available From | Coverage |")
    md.append("|--------|---------------|----------|")
    md.append("| OI, Taker, L/S Ratio | Sep 2025 | ~65% of period |")
    md.append("| Funding Rate | Jun 2025 | ~100% |")
    md.append("| Fear & Greed | Jul 2025 | ~95% |")
    md.append("| Whale Alerts | Jun 2025 | ~100% |")
    md.append("| Liquidation | Jun 2025 | ~100% |")
    md.append("| ETF Flows | Jun 2025 | ~100% |")
    md.append("\nNote: Before Sep 2025, only 5/8 factors active (no OI, taker, L/S). Strategy still trades using available signals.")

    report_path = "backtest_15m_extended_report.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md))
    print(f"\n  Report: {report_path}")

    if not trades_full.empty:
        trades_full.to_csv("backtest_details/trades_15m_extended_full.csv", index=False)
    pd.DataFrame(all_results).to_csv("backtest_details/grid_search_extended.csv", index=False)

    print("\nDone!")


if __name__ == "__main__":
    main()
