"""
BTC 15m Enhanced Composite Strategy -- A/B Test New Data Sources
================================================================
Tests each new data source individually + combined:
  A) Baseline (V2: existing DB data only)
  B) + Extended Funding Rate (3yr Binance historical)
  C) + DVOL (Deribit implied volatility)
  D) + DXY (Dollar Index inverse correlation)
  E) All combined (B+C+D)

Walk-forward: Train Jun-Nov 2025, Test Dec 2025-Mar 2026
Uses 3yr OHLCV for indicator warmup.
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


def load_all_data():
    """Load OHLCV + DB data + new data sources."""
    # 3yr OHLCV
    ohlcv = pd.read_parquet("data_cache/BTCUSDT_15m_3yr.parquet")
    ohlcv["date_time"] = pd.to_datetime(ohlcv["date_time"])
    ohlcv = ohlcv.sort_values("date_time").reset_index(drop=True)
    print(f"  OHLCV: {len(ohlcv):,} candles")

    # DB data
    conn = psycopg2.connect(**DB_PARAMS)
    db = {}
    for name, sql, tz_fix in [
        ("oi", "SELECT ts, oi_usdt FROM market_data.open_interest WHERE symbol='BTCUSDT' ORDER BY ts", True),
        ("taker", "SELECT ts, buy_vol, sell_vol, buy_sell_ratio FROM market_data.taker_volume WHERE symbol='BTCUSDT' ORDER BY ts", False),
        ("ls_ratio", "SELECT ts, gl_ac_ratio FROM market_data.long_short_ratio WHERE symbol='BTCUSDT' ORDER BY ts", False),
        ("premium", "SELECT ts, last_funding_rate, premium FROM market_data.premium_index WHERE symbol='BTCUSDT' ORDER BY ts", True),
    ]:
        db[name] = pd.read_sql(sql, conn, parse_dates=["ts"])
        if tz_fix:
            db[name]["ts"] = db[name]["ts"].dt.tz_localize(None)
    # Special tables
    db["fg"] = pd.read_sql("SELECT created_at as ts, score as fg_score FROM public.fear_greed ORDER BY created_at", conn, parse_dates=["ts"])
    db["fg"]["ts"] = db["fg"]["ts"].dt.tz_localize(None)

    whale = pd.read_sql("SELECT alert_time as ts, usd_value, sentiment FROM public.whale_alert WHERE symbol='BTC' ORDER BY alert_time", conn, parse_dates=["ts"])
    whale["ts"] = whale["ts"].dt.tz_localize(None)
    whale["usd_value"] = whale["usd_value"].astype(float)
    db["whale"] = whale

    liq = pd.read_sql("SELECT created_at as ts, liq_long_1h, liq_short_1h FROM public.liquidation WHERE coin='BTC' ORDER BY created_at", conn, parse_dates=["ts"])
    liq["ts"] = liq["ts"].dt.tz_localize(None) + pd.Timedelta("1h")
    db["liq"] = liq

    etf = pd.read_sql("SELECT date as ts, total as etf_flow FROM public.etf_btc ORDER BY date", conn, parse_dates=["ts"])
    etf["ts"] = etf["ts"] + pd.Timedelta("1d")
    db["etf"] = etf

    conn.close()
    print(f"  DB tables loaded")

    # New data sources
    new = {}
    new["funding"] = pd.read_parquet("data_cache/binance_funding_rate_hist.parquet")
    new["funding"]["ts"] = pd.to_datetime(new["funding"]["ts"])
    print(f"  Funding rate (extended): {len(new['funding']):,} rows")

    dvol_path = "data_cache/deribit_dvol.parquet"
    if os.path.exists(dvol_path):
        new["dvol"] = pd.read_parquet(dvol_path)
        new["dvol"]["ts"] = pd.to_datetime(new["dvol"]["ts"])
        print(f"  DVOL: {len(new['dvol']):,} rows")
    else:
        new["dvol"] = pd.DataFrame()

    dxy_path = "data_cache/dxy_daily.parquet"
    if os.path.exists(dxy_path):
        new["dxy"] = pd.read_parquet(dxy_path)
        new["dxy"]["ts"] = pd.to_datetime(new["dxy"]["ts"])
        print(f"  DXY: {len(new['dxy']):,} rows")
    else:
        new["dxy"] = pd.DataFrame()

    return ohlcv, db, new


def resample_to_15m(df, ts_col, cols, agg="last"):
    d = df.set_index(ts_col).sort_index()
    if agg == "last":
        return d[cols].resample("15min").last().dropna(how="all").reset_index()
    elif agg == "sum":
        return d[cols].resample("15min").sum().reset_index()
    return d[cols].resample("15min").last().dropna(how="all").reset_index()


def build_features(ohlcv, db, new_data, use_ext_funding=False, use_dvol=False, use_dxy=False):
    df = ohlcv.copy().rename(columns={"date_time": "ts"})

    # Technical indicators
    df["ema9"] = df["close"].ewm(span=9, adjust=False).mean()
    df["ema21"] = df["close"].ewm(span=21, adjust=False).mean()
    df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=14)
    df["vol_ma"] = df["volume"].rolling(20).mean()
    df["vol_ratio"] = df["volume"] / df["vol_ma"]
    df["ret"] = df["close"].pct_change()

    # DB features
    if len(db.get("oi", [])) > 0:
        oi = resample_to_15m(db["oi"], "ts", ["oi_usdt"])
        oi["oi_chg"] = oi["oi_usdt"].pct_change()
        df = pd.merge_asof(df.sort_values("ts"), oi.sort_values("ts"), on="ts", direction="backward", tolerance=pd.Timedelta("30min"))

    if len(db.get("taker", [])) > 0:
        taker = resample_to_15m(db["taker"], "ts", ["buy_sell_ratio"])
        df = pd.merge_asof(df.sort_values("ts"), taker.sort_values("ts"), on="ts", direction="backward", tolerance=pd.Timedelta("30min"))

    if len(db.get("ls_ratio", [])) > 0:
        df = pd.merge_asof(df.sort_values("ts"), db["ls_ratio"].sort_values("ts"), on="ts", direction="backward", tolerance=pd.Timedelta("30min"))

    if len(db.get("fg", [])) > 0:
        df = pd.merge_asof(df.sort_values("ts"), db["fg"].sort_values("ts"), on="ts", direction="backward", tolerance=pd.Timedelta("2d"))

    if len(db.get("whale", [])) > 0:
        whale = db["whale"].copy()
        whale["bull_val"] = np.where(whale["sentiment"] == "bullish", whale["usd_value"], 0)
        whale["bear_val"] = np.where(whale["sentiment"] == "bearish", whale["usd_value"], 0)
        wa = whale.set_index("ts").resample("15min").agg({"bull_val": "sum", "bear_val": "sum"}).reset_index()
        wa["whale_net"] = wa["bull_val"] - wa["bear_val"]
        wa["whale_net_ma"] = wa["whale_net"].rolling(8).mean()
        df = pd.merge_asof(df.sort_values("ts"), wa.sort_values("ts"), on="ts", direction="backward", tolerance=pd.Timedelta("30min"))

    if len(db.get("liq", [])) > 0:
        liq = db["liq"].copy()
        liq["liq_net"] = liq["liq_short_1h"] - liq["liq_long_1h"]
        liq["liq_total"] = liq["liq_long_1h"] + liq["liq_short_1h"]
        liq["liq_total_ma"] = liq["liq_total"].rolling(24).mean()
        df = pd.merge_asof(df.sort_values("ts"), liq.sort_values("ts"), on="ts", direction="backward", tolerance=pd.Timedelta("2h"))

    if len(db.get("etf", [])) > 0:
        etf = db["etf"].copy()
        etf["etf_flow_ma"] = etf["etf_flow"].rolling(5).mean()
        df = pd.merge_asof(df.sort_values("ts"), etf.sort_values("ts"), on="ts", direction="backward", tolerance=pd.Timedelta("3d"))

    # --- NEW: Extended Funding Rate ---
    if use_ext_funding and len(new_data.get("funding", [])) > 0:
        fr = new_data["funding"][["ts", "funding_rate"]].copy()
        fr = fr.rename(columns={"funding_rate": "fr_ext"})
        fr["fr_ext_ma"] = fr["fr_ext"].rolling(3).mean()  # 24h avg
        fr["fr_ext_zscore"] = (fr["fr_ext"] - fr["fr_ext"].rolling(90).mean()) / fr["fr_ext"].rolling(90).std().clip(lower=1e-8)
        df = pd.merge_asof(df.sort_values("ts"), fr.sort_values("ts"), on="ts", direction="backward", tolerance=pd.Timedelta("12h"))

    # --- NEW: DVOL (Implied Volatility) ---
    if use_dvol and len(new_data.get("dvol", [])) > 0:
        dvol = new_data["dvol"][["ts", "dvol_close"]].copy()
        dvol["dvol_ma"] = dvol["dvol_close"].rolling(24).mean()  # 24h MA
        dvol["dvol_zscore"] = (dvol["dvol_close"] - dvol["dvol_close"].rolling(24*7).mean()) / dvol["dvol_close"].rolling(24*7).std().clip(lower=1e-8)
        df = pd.merge_asof(df.sort_values("ts"), dvol.sort_values("ts"), on="ts", direction="backward", tolerance=pd.Timedelta("2h"))

    # --- NEW: DXY ---
    if use_dxy and len(new_data.get("dxy", [])) > 0:
        dxy = new_data["dxy"][["ts", "dxy_close"]].copy()
        dxy["dxy_ret5d"] = dxy["dxy_close"].pct_change(5)  # 5-day return
        dxy["dxy_ema9"] = dxy["dxy_close"].ewm(span=9, adjust=False).mean()
        dxy["dxy_ema21"] = dxy["dxy_close"].ewm(span=21, adjust=False).mean()
        dxy["dxy_trend"] = np.where(dxy["dxy_ema9"] > dxy["dxy_ema21"], 1, -1)  # 1=DXY up, -1=DXY down
        df = pd.merge_asof(df.sort_values("ts"), dxy.sort_values("ts"), on="ts", direction="backward", tolerance=pd.Timedelta("3d"))

    return df.sort_values("ts").reset_index(drop=True)


def generate_signal(df, params):
    score = pd.Series(0.0, index=df.index)

    # Original 8 factors (sentiment weights)
    if "oi_chg" in df.columns:
        oi = df["oi_chg"].fillna(0); ret = df["ret"].fillna(0)
        score += np.where((ret > 0.001) & (oi > 0.002), 0.5, 0)
        score += np.where((ret < -0.001) & (oi < -0.002), 0.5, 0)
        score += np.where((ret > 0.001) & (oi < -0.002), -0.5, 0)
        score += np.where((ret < -0.001) & (oi > 0.002), -0.5, 0)

    if "buy_sell_ratio" in df.columns:
        bsr = df["buy_sell_ratio"].fillna(1.0)
        score += np.where(bsr > 1.5, 1.5, 0)
        score += np.where((bsr > 1.2) & (bsr <= 1.5), 0.5, 0)
        score += np.where(bsr < 0.7, -1.5, 0)
        score += np.where((bsr >= 0.7) & (bsr < 0.85), -0.5, 0)

    if "gl_ac_ratio" in df.columns:
        ls = df["gl_ac_ratio"].fillna(1.0)
        score += np.where(ls > 2.5, -1.5, 0)
        score += np.where(ls < 0.6, 1.5, 0)

    if "fg_score" in df.columns:
        fg = df["fg_score"].fillna(50)
        score += np.where(fg < 15, 2.0, 0)
        score += np.where((fg >= 15) & (fg < 25), 1.0, 0)
        score += np.where(fg > 80, -2.0, 0)
        score += np.where((fg > 65) & (fg <= 80), -1.0, 0)

    if "whale_net_ma" in df.columns:
        wn = df["whale_net_ma"].fillna(0)
        score += np.where(wn > 50_000_000, 1.5, 0)
        score += np.where(wn < -50_000_000, -1.5, 0)

    if "liq_net" in df.columns and "liq_total_ma" in df.columns:
        lt = df["liq_total"].fillna(0); lt_ma = df["liq_total_ma"].fillna(1); ln = df["liq_net"].fillna(0)
        cascade = lt > (lt_ma * 3)
        score += np.where(cascade & (ln > 0), 2.0, 0)
        score += np.where(cascade & (ln < 0), -2.0, 0)

    if "etf_flow_ma" in df.columns:
        etf = df["etf_flow_ma"].fillna(0)
        score += np.where(etf > 50, 1.5, 0)
        score += np.where(etf < -50, -1.5, 0)

    # --- NEW Factor: Extended Funding Rate ---
    if params.get("use_ext_funding") and "fr_ext" in df.columns:
        fr = df["fr_ext"].fillna(0)
        fr_z = df["fr_ext_zscore"].fillna(0) if "fr_ext_zscore" in df.columns else fr * 10000
        w = params.get("w_fr_ext", 2.0)
        score += np.where(fr < -0.0001, w, 0)       # negative funding = bullish
        score += np.where(fr > 0.0003, -w, 0)        # high positive = bearish
        score += np.where(fr_z < -2.0, w * 0.5, 0)   # extreme z-score bonus
        score += np.where(fr_z > 2.0, -w * 0.5, 0)
    elif "last_funding_rate" in df.columns:
        # Fallback to DB premium data
        fr = df["last_funding_rate"].fillna(0).astype(float)
        score += np.where(fr < -0.00005, 2.0, 0)
        score += np.where(fr > 0.0002, -2.0, 0)

    # --- NEW Factor: DVOL (Implied Volatility) ---
    if params.get("use_dvol") and "dvol_close" in df.columns:
        dvol = df["dvol_close"].fillna(50)
        dvol_z = df["dvol_zscore"].fillna(0) if "dvol_zscore" in df.columns else pd.Series(0, index=df.index)
        w = params.get("w_dvol", 1.5)
        # High IV (fear spike) = contrarian buy opportunity
        score += np.where(dvol > 80, w, 0)
        score += np.where(dvol_z > 2.0, w * 0.5, 0)
        # Very low IV (complacency) = potential reversal warning
        score += np.where(dvol < 30, -w * 0.5, 0)

    # --- NEW Factor: DXY (inverse correlation) ---
    if params.get("use_dxy") and "dxy_trend" in df.columns:
        dxy_t = df["dxy_trend"].fillna(0)
        dxy_ret = df["dxy_ret5d"].fillna(0)
        w = params.get("w_dxy", 1.5)
        # DXY down trend = bullish for BTC
        score += np.where((dxy_t == -1) & (dxy_ret < -0.005), w, 0)
        # DXY up trend = bearish for BTC
        score += np.where((dxy_t == 1) & (dxy_ret > 0.005), -w, 0)

    # Price confirmation
    bull_pa = (df["close"] > df["ema9"]) & (df["ema9"] > df["ema21"])
    bear_pa = (df["close"] < df["ema9"]) & (df["ema9"] < df["ema21"])
    vol_ok = df["vol_ratio"] > 0.8

    threshold = params.get("threshold", 3.0)
    signal = pd.Series(0, index=df.index)
    signal[(score >= threshold) & bull_pa & vol_ok] = 1
    signal[(score <= -threshold) & bear_pa & vol_ok] = -1
    return signal, score


def run_backtest(df, signals, sl=2.0, tp=3.0, trail=0.5, trail_act=0.5, cd=4):
    sig = signals.shift(1).fillna(0).astype(int).values
    atrs, opens, highs, lows, closes, times = df["atr"].values, df["open"].values, df["high"].values, df["low"].values, df["close"].values, df["ts"].values
    n = len(df); records = []; equity = INIT_EQUITY
    position = 0; entry_i = entry_px = entry_atr = qty = fee_in = 0
    peak = trough = 0.0; trl_active = False; last_exit_i = -cd - 1

    for i in range(n):
        if position == 0 and sig[i] != 0 and (i - last_exit_i) > cd:
            raw_px = opens[i]; cur_atr = atrs[i] if not np.isnan(atrs[i]) else 0
            if raw_px <= 0 or cur_atr <= 0 or cur_atr / raw_px < 0.0005: continue
            qty = (BUDGET_USDT * LEVERAGE) / raw_px
            entry_px = raw_px * (1 + SLIP) if sig[i] == 1 else raw_px * (1 - SLIP)
            entry_atr = cur_atr; fee_in = entry_px * qty * FEE
            position = sig[i]; entry_i = i; peak = entry_px; trough = entry_px; trl_active = False
            continue
        if position != 0:
            h, l, c = highs[i], lows[i], closes[i]; atr = entry_atr
            if position == 1: peak = max(peak, h); sl_lv = entry_px - sl * atr; tp_lv = entry_px + tp * atr
            else: trough = min(trough, l); sl_lv = entry_px + sl * atr; tp_lv = entry_px - tp * atr
            trail_stop = None
            if trail < 50:
                if position == 1 and (peak - entry_px) >= trail_act * atr: trl_active = True; trail_stop = peak - trail * atr
                elif position == -1 and (entry_px - trough) >= trail_act * atr: trl_active = True; trail_stop = trough + trail * atr
            exit_px = exit_reason = None
            if position == 1:
                if l <= sl_lv: exit_px, exit_reason = sl_lv, "SL"
                elif trl_active and trail_stop and l <= trail_stop: exit_px, exit_reason = trail_stop, "TRAIL"
                elif h >= tp_lv: exit_px, exit_reason = tp_lv, "TP"
            else:
                if h >= sl_lv: exit_px, exit_reason = sl_lv, "SL"
                elif trl_active and trail_stop and h >= trail_stop: exit_px, exit_reason = trail_stop, "TRAIL"
                elif l <= tp_lv: exit_px, exit_reason = tp_lv, "TP"
            if exit_px is None and (i - entry_i) >= 96: exit_px, exit_reason = c, "TIMEOUT"
            if exit_px is None and sig[i] != 0 and sig[i] != position: exit_px, exit_reason = opens[i], "SIGNAL_FLIP"
            if exit_px is not None:
                exit_f = exit_px * (1 - SLIP) if position == 1 else exit_px * (1 + SLIP)
                fee_out = exit_f * qty * FEE
                pnl = (exit_f - entry_px) * qty * position - fee_in - fee_out
                equity += pnl
                records.append({"entry_time": times[entry_i], "exit_time": times[i], "dir": "L" if position == 1 else "S", "pnl_net": pnl, "equity_after": equity, "exit_reason": exit_reason, "holding_bars": i - entry_i})
                last_exit_i = i; position = 0
    return pd.DataFrame(records)


def calc_metrics(trades, total_bars):
    if trades.empty:
        return {"total": 0, "win_rate": 0, "pf": 0, "net_pnl": 0, "max_dd": 0, "sharpe": 0, "rr": 0}
    n = len(trades); wins = trades[trades["pnl_net"] > 0]; losses = trades[trades["pnl_net"] < 0]
    wr = len(wins) / n * 100
    sw = wins["pnl_net"].sum() if len(wins) else 0; sl = abs(losses["pnl_net"].sum()) if len(losses) else 0
    pf = sw / sl if sl > 0 else 99
    net = trades["pnl_net"].sum()
    eq = pd.concat([pd.Series([INIT_EQUITY]), INIT_EQUITY + trades["pnl_net"].cumsum()]).reset_index(drop=True)
    dd = ((eq - eq.cummax()) / eq.cummax()).min() * 100
    rets = trades["pnl_net"] / BUDGET_USDT
    sharpe = rets.mean() / rets.std() * np.sqrt(n) if len(rets) > 1 and rets.std() > 0 else 0
    aw = wins["pnl_net"].mean() if len(wins) else 0; al = abs(losses["pnl_net"].mean()) if len(losses) else 0
    rr = aw / al if al > 0 else 0
    return {"total": n, "win_rate": round(wr, 1), "pf": round(pf, 3), "net_pnl": round(net, 2), "max_dd": round(dd, 2), "sharpe": round(sharpe, 3), "rr": round(rr, 3)}


def run_experiment(label, df_train, df_test, params, sl=2.0, tp=3.0, trail=0.5, trail_act=0.5, cd=4):
    """Run train+test for one experiment."""
    sig_tr, _ = generate_signal(df_train, params)
    trades_tr = run_backtest(df_train, sig_tr, sl, tp, trail, trail_act, cd)
    m_tr = calc_metrics(trades_tr, len(df_train))

    sig_te, _ = generate_signal(df_test, params)
    trades_te = run_backtest(df_test, sig_te, sl, tp, trail, trail_act, cd)
    m_te = calc_metrics(trades_te, len(df_test))

    sig_f, _ = generate_signal(pd.concat([df_train, df_test]).reset_index(drop=True), params)
    df_full = pd.concat([df_train, df_test]).reset_index(drop=True)
    trades_f = run_backtest(df_full, sig_f, sl, tp, trail, trail_act, cd)
    m_f = calc_metrics(trades_f, len(df_full))

    print(f"  {label:25s} | Train: {m_tr['total']:3d} trades PnL=${m_tr['net_pnl']:+7.2f} PF={m_tr['pf']:.2f} | Test: {m_te['total']:3d} trades PnL=${m_te['net_pnl']:+7.2f} PF={m_te['pf']:.2f} | Full: PnL=${m_f['net_pnl']:+7.2f}")
    return {"label": label, "train": m_tr, "test": m_te, "full": m_f, "trades_full": trades_f}


def main():
    print("=" * 60)
    print("BTC 15m Enhanced -- A/B Test New Data Sources")
    print("=" * 60)

    print("\n[1/3] Loading all data...")
    ohlcv, db, new_data = load_all_data()

    print("\n[2/3] Building feature sets...")

    # Build features for each experiment
    db_start = pd.Timestamp("2025-06-25")
    split_date = pd.Timestamp("2025-12-01")

    configs = [
        ("A: Baseline (V2)", False, False, False),
        ("B: +Ext Funding Rate", True, False, False),
        ("C: +DVOL (IV)", False, True, False),
        ("D: +DXY", False, False, True),
        ("E: All Combined", True, True, True),
    ]

    # Best V2 config
    best_sl, best_tp, best_trail, best_trail_act, best_cd = 2.0, 3.0, 0.5, 0.5, 4

    # Also test different thresholds for combined
    thresholds_to_test = [2.5, 3.0, 3.5]

    results = []

    print("\n[3/3] Running A/B tests...")
    print(f"  {'Experiment':25s} | {'Train (Jun-Nov 25)':35s} | {'Test (Dec 25-Mar 26)':35s} | Full")
    print(f"  {'-'*25} | {'-'*35} | {'-'*35} | ----")

    for label, use_fr, use_dvol, use_dxy in configs:
        df = build_features(ohlcv, db, new_data, use_ext_funding=use_fr, use_dvol=use_dvol, use_dxy=use_dxy)
        df = df[df["ts"] >= db_start].reset_index(drop=True)
        df_train = df[df["ts"] < split_date].reset_index(drop=True)
        df_test = df[df["ts"] >= split_date].reset_index(drop=True)

        # Test with threshold 3.0 (V2 best)
        params = {"threshold": 3.0, "use_ext_funding": use_fr, "use_dvol": use_dvol, "use_dxy": use_dxy,
                  "w_fr_ext": 2.0, "w_dvol": 1.5, "w_dxy": 1.5}
        r = run_experiment(label, df_train, df_test, params, best_sl, best_tp, best_trail, best_trail_act, best_cd)
        results.append(r)

    # Test combined with different thresholds
    print(f"\n  --- Combined (E) with different thresholds ---")
    df = build_features(ohlcv, db, new_data, use_ext_funding=True, use_dvol=True, use_dxy=True)
    df = df[df["ts"] >= db_start].reset_index(drop=True)
    df_train = df[df["ts"] < split_date].reset_index(drop=True)
    df_test = df[df["ts"] >= split_date].reset_index(drop=True)

    for thr in [2.0, 2.5, 3.5, 4.0]:
        params = {"threshold": thr, "use_ext_funding": True, "use_dvol": True, "use_dxy": True,
                  "w_fr_ext": 2.0, "w_dvol": 1.5, "w_dxy": 1.5}
        r = run_experiment(f"E: thr={thr}", df_train, df_test, params, best_sl, best_tp, best_trail, best_trail_act, best_cd)
        results.append(r)

    # Test combined with different weights for new factors
    print(f"\n  --- Weight sensitivity (threshold=3.0) ---")
    weight_tests = [
        ("E: FR=3.0 DVOL=1.5 DXY=1.5", 3.0, 1.5, 1.5),
        ("E: FR=2.0 DVOL=2.0 DXY=1.5", 2.0, 2.0, 1.5),
        ("E: FR=2.0 DVOL=1.5 DXY=2.0", 2.0, 1.5, 2.0),
        ("E: FR=2.5 DVOL=2.0 DXY=2.0", 2.5, 2.0, 2.0),
    ]
    for label, w_fr, w_dv, w_dx in weight_tests:
        params = {"threshold": 3.0, "use_ext_funding": True, "use_dvol": True, "use_dxy": True,
                  "w_fr_ext": w_fr, "w_dvol": w_dv, "w_dxy": w_dx}
        r = run_experiment(label, df_train, df_test, params, best_sl, best_tp, best_trail, best_trail_act, best_cd)
        results.append(r)

    # Generate report
    print("\n  Generating report...")
    md = []
    md.append("# BTC 15m Enhanced -- A/B Test Report")
    md.append(f"\n**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    md.append(f"**Period:** Jun 2025 - Mar 2026 (~9 months)")
    md.append(f"**Train:** Jun - Nov 2025 | **Test (OOS):** Dec 2025 - Mar 2026")
    md.append(f"**Config:** SL={best_sl} TP={best_tp} trail={best_trail} cd={best_cd}")

    md.append("\n---\n")
    md.append("## A/B Test Results")
    md.append("\n| Experiment | Train Trades | Train PnL | Train PF | OOS Trades | OOS PnL | OOS PF | Full PnL |")
    md.append("|------------|-------------|-----------|----------|------------|---------|--------|----------|")
    for r in results:
        md.append(f"| {r['label']} | {r['train']['total']} | ${r['train']['net_pnl']:+,.2f} | {r['train']['pf']:.2f} | {r['test']['total']} | ${r['test']['net_pnl']:+,.2f} | {r['test']['pf']:.2f} | ${r['full']['net_pnl']:+,.2f} |")

    # Highlight winner
    oos_results = [(r['label'], r['test']['net_pnl'], r['test']['pf']) for r in results]
    oos_results.sort(key=lambda x: x[1], reverse=True)
    md.append(f"\n**Best OOS PnL:** {oos_results[0][0]} -- ${oos_results[0][1]:+,.2f} (PF={oos_results[0][2]:.2f})")

    # Analysis per factor
    md.append("\n---\n")
    md.append("## Factor Impact Analysis")

    # Compare A vs B, A vs C, A vs D
    a = next(r for r in results if r['label'] == 'A: Baseline (V2)')
    b = next(r for r in results if r['label'] == 'B: +Ext Funding Rate')
    c = next(r for r in results if r['label'] == 'C: +DVOL (IV)')
    d = next(r for r in results if r['label'] == 'D: +DXY')
    e = next(r for r in results if r['label'] == 'E: All Combined')

    md.append("\n| Factor | OOS PnL Change | OOS PF Change | Verdict |")
    md.append("|--------|---------------|---------------|---------|")

    for label, exp in [("Ext Funding Rate", b), ("DVOL (IV)", c), ("DXY", d), ("All Combined", e)]:
        pnl_diff = exp['test']['net_pnl'] - a['test']['net_pnl']
        pf_diff = exp['test']['pf'] - a['test']['pf']
        verdict = "HELPS" if pnl_diff > 0 and exp['test']['net_pnl'] > 0 else "HURTS" if pnl_diff < 0 else "NEUTRAL"
        md.append(f"| {label} | ${pnl_diff:+,.2f} | {pf_diff:+.2f} | **{verdict}** |")

    md.append("\n---\n")
    md.append("## New Data Sources")
    md.append("| Source | Period | Rows | Resolution |")
    md.append("|--------|--------|------|------------|")
    md.append(f"| Binance Funding Rate | {str(new_data['funding']['ts'].iloc[0])[:10]} to {str(new_data['funding']['ts'].iloc[-1])[:10]} | {len(new_data['funding']):,} | 8h |")
    if not new_data['dvol'].empty:
        md.append(f"| Deribit DVOL (IV) | {str(new_data['dvol']['ts'].iloc[0])[:10]} to {str(new_data['dvol']['ts'].iloc[-1])[:10]} | {len(new_data['dvol']):,} | 1h |")
    if not new_data['dxy'].empty:
        md.append(f"| DXY Dollar Index | {str(new_data['dxy']['ts'].iloc[0])[:10]} to {str(new_data['dxy']['ts'].iloc[-1])[:10]} | {len(new_data['dxy']):,} | 1d |")

    report_path = "backtest_15m_enhanced_report.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md))
    print(f"  Report: {report_path}")
    print("\nDone!")


if __name__ == "__main__":
    main()
