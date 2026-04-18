"""
BTC Futures Backtest -- 1m / 5m / 15m Timeframe Comparison
=========================================================
ดึงข้อมูล BTCUSDT Futures จาก Binance ย้อนหลัง 1 ปี
ทดสอบ 4 กลยุทธ์ x 3 TF  แล้วสรุปเปรียบเทียบเป็น .md

กลยุทธ์:
  1. EMA Crossover (21/50)
  2. RSI Mean-Reversion
  3. FVG (Fair Value Gap)
  4. Combined (EMA + RSI + Volume)

Realistic settings:
  - Taker fee 0.05% per side
  - Slippage 0.025% per side
  - Signal lag 1 bar (ไม่ look-ahead)
  - Entry at next bar open
  - TP/SL + Trailing stop
"""

import os, sys, time, math, warnings
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pandas_ta as ta
from binance.client import Client
from binance.enums import HistoricalKlinesType
from dotenv import load_dotenv

warnings.filterwarnings("ignore")

# ─── Config ──────────────────────────────────────────────────────
load_dotenv()
BINANCE_KEY = os.getenv("BINANCE_KEY", "")
BINANCE_SECRET = os.getenv("BINANCE_SECRET", "")

TIMEFRAMES = ["1m", "5m", "15m"]
TF_MINUTES = {"1m": 1, "5m": 5, "15m": 15}
SYMBOL = "BTCUSDT"
LOOKBACK_DAYS = 365

# Realistic costs
FEE_BPS = 5.0       # 0.05% taker fee per side
SLIP_BPS = 2.5       # 0.025% slippage per side
FEE = FEE_BPS / 10_000
SLIP = SLIP_BPS / 10_000

INIT_EQUITY = 10_000.0
BUDGET_USDT = 1_000.0   # position size per trade
LEVERAGE = 1.0

# ─── Data Fetching ───────────────────────────────────────────────
def fetch_binance_futures(symbol: str, interval: str, days: int) -> pd.DataFrame:
    """Fetch historical klines from Binance Futures in batches."""
    client = Client(BINANCE_KEY, BINANCE_SECRET)

    end_dt = datetime.utcnow()
    start_dt = end_dt - timedelta(days=days)

    all_klines = []
    current_start = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    batch_size = 1500
    interval_ms = TF_MINUTES[interval] * 60 * 1000

    print(f"  Fetching {symbol} {interval} ({days} days)...", end="", flush=True)
    batch_count = 0

    while current_start < end_ms:
        try:
            klines = client.get_historical_klines(
                symbol, interval,
                start_str=str(current_start),
                end_str=str(end_ms),
                limit=batch_size,
                klines_type=HistoricalKlinesType.FUTURES
            )
        except Exception as e:
            print(f"\n  API error: {e}, retrying in 5s...")
            time.sleep(5)
            continue

        if not klines:
            break

        all_klines.extend(klines)
        last_ts = int(klines[-1][0])
        current_start = last_ts + interval_ms
        batch_count += 1

        if batch_count % 10 == 0:
            print(f" {batch_count}b", end="", flush=True)

        # Rate limiting
        time.sleep(0.15)

    if not all_klines:
        print(" EMPTY!")
        return pd.DataFrame()

    df = pd.DataFrame(all_klines, columns=[
        "t", "o", "h", "l", "c", "v", "ct", "qv", "n", "tb", "tq", "ig"
    ])

    # Drop duplicates by timestamp
    df = df.drop_duplicates(subset=["t"])

    for col in ["o", "h", "l", "c", "v", "qv"]:
        df[col] = df[col].astype(float)

    df["date_time"] = pd.to_datetime(df["t"], unit="ms")
    df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}, inplace=True)
    df = df[["date_time", "open", "high", "low", "close", "volume"]].reset_index(drop=True)
    df = df.sort_values("date_time").reset_index(drop=True)

    print(f" Done! {len(df):,} candles ({df['date_time'].iloc[0]} to {df['date_time'].iloc[-1]})")
    return df


def load_or_fetch(symbol: str, interval: str, days: int, cache_dir: str = "data_cache") -> pd.DataFrame:
    """Load from cache or fetch from Binance."""
    Path(cache_dir).mkdir(exist_ok=True)
    cache_file = os.path.join(cache_dir, f"{symbol}_{interval}_{days}d.parquet")

    if os.path.exists(cache_file):
        df = pd.read_parquet(cache_file)
        # Check if cache is recent enough (within 1 day)
        file_age = time.time() - os.path.getmtime(cache_file)
        if file_age < 86400:
            print(f"  Loaded from cache: {cache_file} ({len(df):,} candles)")
            return df
        else:
            print(f"  Cache stale ({file_age/3600:.1f}h old), re-fetching...")

    df = fetch_binance_futures(symbol, interval, days)
    if not df.empty:
        df.to_parquet(cache_file, index=False)
    return df


# ─── Technical Indicators ───────────────────────────────────────
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add all technical indicators needed by strategies."""
    d = df.copy()

    # EMAs
    d["ema9"] = d["close"].ewm(span=9, adjust=False).mean()
    d["ema21"] = d["close"].ewm(span=21, adjust=False).mean()
    d["ema50"] = d["close"].ewm(span=50, adjust=False).mean()
    d["ema200"] = d["close"].ewm(span=200, adjust=False).mean()

    # RSI
    d["rsi"] = ta.rsi(d["close"], length=14)

    # ATR for dynamic SL/TP
    d["atr"] = ta.atr(d["high"], d["low"], d["close"], length=14)

    # Volume
    d["vol_ma"] = d["volume"].rolling(20).mean()
    d["vol_ratio"] = d["volume"] / d["vol_ma"]

    # Returns
    d["ret"] = d["close"].pct_change()

    # FVG detection (vectorized)
    d["bull_fvg"] = (d["high"].shift(2) < d["low"]) & (d["close"].shift(1) > d["open"].shift(1))
    d["bear_fvg"] = (d["low"].shift(2) > d["high"]) & (d["close"].shift(1) < d["open"].shift(1))

    # Bull FVG zone
    d["bull_fvg_high"] = np.where(d["bull_fvg"], d["low"], np.nan)
    d["bull_fvg_low"] = np.where(d["bull_fvg"], d["high"].shift(2), np.nan)

    # Bear FVG zone
    d["bear_fvg_high"] = np.where(d["bear_fvg"], d["low"].shift(2), np.nan)
    d["bear_fvg_low"] = np.where(d["bear_fvg"], d["high"], np.nan)

    return d


# ─── Strategy Signal Generators ─────────────────────────────────

def strategy_ema_trend_follow(df: pd.DataFrame) -> pd.Series:
    """
    Trend Following: Only trade in direction of higher-TF trend.
    Long: EMA9 > EMA21 > EMA50, pullback touches EMA21 then bounces.
    Short: EMA9 < EMA21 < EMA50, rally touches EMA21 then rejects.
    Requires volume confirmation and ATR gate.
    """
    signal = pd.Series(0, index=df.index)

    # Full trend alignment
    bull_align = (df["ema9"] > df["ema21"]) & (df["ema21"] > df["ema50"])
    bear_align = (df["ema9"] < df["ema21"]) & (df["ema21"] < df["ema50"])

    # Pullback to EMA21: low touched EMA21 (within 0.15%) but closed above
    pullback_long = (df["low"] <= df["ema21"] * 1.0015) & (df["close"] > df["ema21"]) & (df["close"] > df["open"])
    pullback_short = (df["high"] >= df["ema21"] * 0.9985) & (df["close"] < df["ema21"]) & (df["close"] < df["open"])

    # Volume above average
    vol_ok = df["vol_ratio"] > 1.2

    # ATR gate: meaningful volatility
    atr_pct = df["atr"] / df["close"]
    atr_ok = atr_pct > atr_pct.rolling(200, min_periods=50).quantile(0.3)

    signal[bull_align & pullback_long & vol_ok & atr_ok] = 1
    signal[bear_align & pullback_short & vol_ok & atr_ok] = -1
    return signal


def strategy_momentum_breakout(df: pd.DataFrame) -> pd.Series:
    """
    Momentum Breakout: Enter on strong candles that break recent range.
    Uses Donchian Channel (20-period high/low) + volume spike.
    """
    signal = pd.Series(0, index=df.index)

    lookback = 20

    # Donchian breakout
    highest = df["high"].rolling(lookback).max().shift(1)
    lowest = df["low"].rolling(lookback).min().shift(1)

    breakout_long = df["close"] > highest
    breakout_short = df["close"] < lowest

    # Strong candle (body > 60% of range)
    body = abs(df["close"] - df["open"])
    wick = df["high"] - df["low"]
    strong_candle = body > (wick * 0.6)

    # Volume spike
    vol_spike = df["vol_ratio"] > 2.0

    # ATR filter
    atr_pct = df["atr"] / df["close"]
    atr_ok = atr_pct > atr_pct.rolling(200, min_periods=50).quantile(0.4)

    signal[breakout_long & strong_candle & vol_spike & atr_ok] = 1
    signal[breakout_short & strong_candle & vol_spike & atr_ok] = -1
    return signal


def strategy_fvg_with_structure(df: pd.DataFrame) -> pd.Series:
    """
    FVG + Market Structure: FVG in trend direction with displacement candle.
    More selective than basic FVG - requires trend alignment, large FVG, and
    displacement confirmation.
    """
    signal = pd.Series(0, index=df.index)

    # EMA trend filter
    trend_up = (df["ema21"] > df["ema50"]) & (df["close"] > df["ema50"])
    trend_down = (df["ema21"] < df["ema50"]) & (df["close"] < df["ema50"])

    # FVG size: gap >= 0.15% of price
    bull_gap_size = (df["low"] - df["high"].shift(2)) / df["close"]
    bear_gap_size = (df["low"].shift(2) - df["high"]) / df["close"]
    bull_size_ok = bull_gap_size > 0.0015
    bear_size_ok = bear_gap_size > 0.0015

    # Displacement: middle candle body >= 1.5x ATR
    body = abs(df["close"].shift(1) - df["open"].shift(1))
    displacement_ok = body > (df["atr"].shift(1) * 1.5)

    # Volume on displacement candle > 2x average
    vol_strong = df["vol_ratio"].shift(1) > 2.0

    signal[df["bull_fvg"] & trend_up & bull_size_ok & displacement_ok & vol_strong] = 1
    signal[df["bear_fvg"] & trend_down & bear_size_ok & displacement_ok & vol_strong] = -1
    return signal


def strategy_rsi_extreme(df: pd.DataFrame) -> pd.Series:
    """
    RSI Extreme Reversal: Only trade at RSI extremes (< 20 or > 80).
    Counter-trend with quick target. Very selective.
    """
    signal = pd.Series(0, index=df.index)

    # Extreme RSI with recovery confirmation
    rsi_extreme_low = (df["rsi"].shift(1) < 20) & (df["rsi"] > df["rsi"].shift(1))
    rsi_extreme_high = (df["rsi"].shift(1) > 80) & (df["rsi"] < df["rsi"].shift(1))

    # Volume spike on reversal
    vol_spike = df["vol_ratio"] > 1.5

    # Bullish/bearish candle confirmation
    bull_candle = df["close"] > df["open"]
    bear_candle = df["close"] < df["open"]

    signal[rsi_extreme_low & vol_spike & bull_candle] = 1
    signal[rsi_extreme_high & vol_spike & bear_candle] = -1
    return signal


def strategy_multi_ema_momentum(df: pd.DataFrame) -> pd.Series:
    """
    Multi-EMA Momentum: All 4 EMAs aligned + momentum + volume.
    The most selective strategy - requires full EMA stack alignment.
    """
    signal = pd.Series(0, index=df.index)

    # Full stack alignment (all 4 EMAs)
    bull_stack = (df["ema9"] > df["ema21"]) & (df["ema21"] > df["ema50"]) & (df["ema50"] > df["ema200"])
    bear_stack = (df["ema9"] < df["ema21"]) & (df["ema21"] < df["ema50"]) & (df["ema50"] < df["ema200"])

    # RSI in momentum zone
    rsi_bull = (df["rsi"] > 55) & (df["rsi"] < 75)
    rsi_bear = (df["rsi"] < 45) & (df["rsi"] > 25)

    # Strong volume
    vol_spike = df["vol_ratio"] > 1.8

    # Recent strong move (close-to-close return in last 3 bars)
    recent_ret = df["close"].pct_change(3)
    bull_momentum = recent_ret > 0.003
    bear_momentum = recent_ret < -0.003

    signal[bull_stack & rsi_bull & vol_spike & bull_momentum] = 1
    signal[bear_stack & rsi_bear & vol_spike & bear_momentum] = -1
    return signal


STRATEGIES = {
    "EMA Trend Follow": strategy_ema_trend_follow,
    "Breakout": strategy_momentum_breakout,
    "FVG Structure": strategy_fvg_with_structure,
    "RSI Extreme": strategy_rsi_extreme,
    "Multi-EMA Momentum": strategy_multi_ema_momentum,
}


# ─── Backtest Engine (standalone, adapted from existing) ─────────

def run_backtest(
    df: pd.DataFrame,
    signals: pd.Series,
    tf_minutes: int,
    # ATR-based TP/SL multipliers
    sl_atr_mult: float = 1.5,     # SL = entry +/- sl_atr_mult * ATR
    tp_atr_mult: float = 3.0,     # TP = entry +/- tp_atr_mult * ATR (0 = no TP, trail only)
    trail_atr_mult: float = 2.0,  # Trail distance = trail_atr_mult * ATR from peak/trough
    trail_activate_atr: float = 1.0,  # Activate trailing after 1 ATR profit
    max_hold_bars: int = None,
    cooldown_bars: int = 2,
) -> pd.DataFrame:
    """
    Backtest engine with ATR-based dynamic TP/SL/Trailing.
    - Entry at next bar open (1-bar lag for signal)
    - Fee + slippage deducted
    - Dynamic levels based on ATR at entry time
    """

    if max_hold_bars is None:
        max_hold_bars = max(96, int(48 * 60 / tf_minutes))

    # Lagged signals (signal at bar i -> entry at bar i+1 open)
    sig = signals.shift(1).fillna(0).astype(int).values
    atrs = df["atr"].values

    opens = df["open"].values
    highs = df["high"].values
    lows = df["low"].values
    closes = df["close"].values
    times = df["date_time"].values

    n = len(df)
    records = []
    equity = INIT_EQUITY

    position = 0
    entry_i = 0
    entry_px = 0.0
    entry_atr = 0.0
    qty = 0.0
    fee_in = 0.0
    peak = 0.0
    trough = 1e18
    trl_active = False
    last_exit_i = -cooldown_bars - 1

    for i in range(n):
        # ── Open new position ──
        if position == 0 and sig[i] != 0 and (i - last_exit_i) > cooldown_bars:
            raw_px = opens[i]
            cur_atr = atrs[i] if not np.isnan(atrs[i]) else 0
            if raw_px <= 0 or cur_atr <= 0:
                continue

            # Minimum ATR threshold (at least 0.05% of price)
            if cur_atr / raw_px < 0.0005:
                continue

            notional = BUDGET_USDT * LEVERAGE
            qty = notional / raw_px

            if sig[i] == 1:
                entry_px = raw_px * (1 + SLIP)
            else:
                entry_px = raw_px * (1 - SLIP)

            entry_atr = cur_atr
            fee_in = entry_px * qty * FEE
            position = sig[i]
            entry_i = i
            peak = entry_px
            trough = entry_px
            trl_active = False
            continue

        # ── Manage open position ──
        if position != 0:
            h, l, c, o = highs[i], lows[i], closes[i], opens[i]

            if position == 1:
                peak = max(peak, h)
            else:
                trough = min(trough, l)

            # Dynamic levels based on ATR at entry
            atr = entry_atr
            if position == 1:
                sl_level = entry_px - sl_atr_mult * atr
                tp_level = entry_px + tp_atr_mult * atr if tp_atr_mult > 0 else None
            else:
                sl_level = entry_px + sl_atr_mult * atr
                tp_level = entry_px - tp_atr_mult * atr if tp_atr_mult > 0 else None

            # Dynamic trailing stop
            trail_stop = None
            trail_dist = trail_atr_mult * atr
            activate_dist = trail_activate_atr * atr

            if position == 1:
                if (peak - entry_px) >= activate_dist:
                    trl_active = True
                    trail_stop = peak - trail_dist
            else:
                if (entry_px - trough) >= activate_dist:
                    trl_active = True
                    trail_stop = trough + trail_dist

            # Check exits (priority: SL > Trail > TP > Timeout)
            exit_px = None
            exit_reason = None

            if position == 1:
                if l <= sl_level:
                    exit_px = sl_level
                    exit_reason = "SL"
                elif trl_active and trail_stop and l <= trail_stop:
                    exit_px = trail_stop
                    exit_reason = "TRAIL"
                elif tp_level and h >= tp_level:
                    exit_px = tp_level
                    exit_reason = "TP"
            else:
                if h >= sl_level:
                    exit_px = sl_level
                    exit_reason = "SL"
                elif trl_active and trail_stop and h >= trail_stop:
                    exit_px = trail_stop
                    exit_reason = "TRAIL"
                elif tp_level and l <= tp_level:
                    exit_px = tp_level
                    exit_reason = "TP"

            # Timeout
            if exit_px is None and (i - entry_i) >= max_hold_bars:
                exit_px = c
                exit_reason = "TIMEOUT"

            # Opposite signal
            if exit_px is None and sig[i] != 0 and sig[i] != position:
                exit_px = o
                exit_reason = "SIGNAL_FLIP"

            if exit_px is not None:
                if position == 1:
                    exit_px_final = exit_px * (1 - SLIP)
                else:
                    exit_px_final = exit_px * (1 + SLIP)

                fee_out = exit_px_final * qty * FEE
                pnl_gross = (exit_px_final - entry_px) * qty * position
                pnl_net = pnl_gross - fee_in - fee_out
                equity += pnl_net

                records.append({
                    "entry_idx": entry_i,
                    "exit_idx": i,
                    "entry_time": times[entry_i],
                    "exit_time": times[i],
                    "dir": "L" if position == 1 else "S",
                    "entry_price": entry_px,
                    "exit_price": exit_px_final,
                    "qty": qty,
                    "fee_in": fee_in,
                    "fee_out": fee_out,
                    "pnl_gross": pnl_gross,
                    "pnl_net": pnl_net,
                    "equity_after": equity,
                    "exit_reason": exit_reason,
                    "holding_bars": i - entry_i,
                })

                last_exit_i = i
                position = 0
                entry_i = 0
                entry_px = 0.0
                entry_atr = 0.0
                qty = 0.0

    return pd.DataFrame(records)


# ─── Metrics Calculation ─────────────────────────────────────────

def calc_metrics(trades: pd.DataFrame, tf_minutes: int, total_bars: int) -> dict:
    """Calculate comprehensive trading metrics."""
    if trades.empty:
        return {
            "total_trades": 0, "win_rate": 0, "profit_factor": 0,
            "net_pnl": 0, "net_pnl_pct": 0, "final_equity": INIT_EQUITY,
            "max_drawdown_pct": 0, "sharpe": 0, "sortino": 0,
            "avg_pnl_per_trade": 0, "avg_win": 0, "avg_loss": 0,
            "rr_ratio": 0, "best_trade": 0, "worst_trade": 0,
            "avg_holding_bars": 0, "avg_holding_hours": 0,
            "exposure_pct": 0, "trades_per_day": 0,
            "max_win_streak": 0, "max_loss_streak": 0,
            "long_trades": 0, "short_trades": 0,
            "long_win_rate": 0, "short_win_rate": 0,
        }

    n = len(trades)
    wins = trades[trades["pnl_net"] > 0]
    losses = trades[trades["pnl_net"] < 0]

    win_rate = len(wins) / n * 100

    sum_wins = wins["pnl_net"].sum() if len(wins) else 0
    sum_losses = abs(losses["pnl_net"].sum()) if len(losses) else 0
    profit_factor = sum_wins / sum_losses if sum_losses > 0 else (float("inf") if sum_wins > 0 else 0)

    net_pnl = trades["pnl_net"].sum()
    final_equity = INIT_EQUITY + net_pnl

    # Drawdown from equity curve
    eq = INIT_EQUITY + trades["pnl_net"].cumsum()
    eq_full = pd.concat([pd.Series([INIT_EQUITY]), eq]).reset_index(drop=True)
    peak = eq_full.cummax()
    dd = (eq_full - peak) / peak
    max_dd = dd.min() * 100

    # Sharpe & Sortino (per-trade returns)
    rets = trades["pnl_net"] / BUDGET_USDT
    if len(rets) > 1 and rets.std() > 0:
        sharpe = rets.mean() / rets.std() * np.sqrt(n)
        neg_rets = rets[rets < 0]
        sortino = rets.mean() / neg_rets.std() * np.sqrt(n) if len(neg_rets) > 1 and neg_rets.std() > 0 else 0
    else:
        sharpe = 0
        sortino = 0

    avg_win = wins["pnl_net"].mean() if len(wins) else 0
    avg_loss = abs(losses["pnl_net"].mean()) if len(losses) else 0
    rr = avg_win / avg_loss if avg_loss > 0 else 0

    # Holding time
    avg_bars = trades["holding_bars"].mean()
    avg_hours = avg_bars * tf_minutes / 60

    # Exposure
    total_bars_held = trades["holding_bars"].sum()
    total_span = total_bars
    exposure = total_bars_held / total_span * 100 if total_span > 0 else 0

    # Trades per day
    total_minutes = total_bars * tf_minutes
    total_days = total_minutes / (60 * 24)
    trades_per_day = n / total_days if total_days > 0 else 0

    # Streaks
    is_win = (trades["pnl_net"] > 0).values
    max_win_streak = _max_streak(is_win, True)
    max_loss_streak = _max_streak(is_win, False)

    # Long/Short breakdown
    longs = trades[trades["dir"] == "L"]
    shorts = trades[trades["dir"] == "S"]
    long_wr = len(longs[longs["pnl_net"] > 0]) / len(longs) * 100 if len(longs) > 0 else 0
    short_wr = len(shorts[shorts["pnl_net"] > 0]) / len(shorts) * 100 if len(shorts) > 0 else 0

    return {
        "total_trades": n,
        "win_rate": round(win_rate, 2),
        "profit_factor": round(profit_factor, 3),
        "net_pnl": round(net_pnl, 2),
        "net_pnl_pct": round(net_pnl / INIT_EQUITY * 100, 2),
        "final_equity": round(final_equity, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "sharpe": round(sharpe, 3),
        "sortino": round(sortino, 3),
        "avg_pnl_per_trade": round(trades["pnl_net"].mean(), 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "rr_ratio": round(rr, 3),
        "best_trade": round(trades["pnl_net"].max(), 2),
        "worst_trade": round(trades["pnl_net"].min(), 2),
        "avg_holding_bars": round(avg_bars, 1),
        "avg_holding_hours": round(avg_hours, 2),
        "exposure_pct": round(exposure, 2),
        "trades_per_day": round(trades_per_day, 2),
        "max_win_streak": max_win_streak,
        "max_loss_streak": max_loss_streak,
        "long_trades": len(longs),
        "short_trades": len(shorts),
        "long_win_rate": round(long_wr, 2),
        "short_win_rate": round(short_wr, 2),
    }


def _max_streak(is_win: np.ndarray, want_win: bool) -> int:
    target = is_win if want_win else ~is_win
    max_s = 0
    current = 0
    for v in target:
        if v:
            current += 1
            max_s = max(max_s, current)
        else:
            current = 0
    return max_s


# ─── Strategy Parameters per TF ─────────────────────────────────

# ATR-based TP/SL -- FIXED TP/SL only (no trailing)
# Analysis showed trailing cuts winners too short (avg win $3.54 vs avg loss $6.81)
# Clean R:R approach: TP = 2x SL, needs 40% WR to break even after fees
STRATEGY_PARAMS = {
    "1m": {
        "EMA Trend Follow":   {"sl_atr_mult": 1.0, "tp_atr_mult": 2.0, "trail_atr_mult": 99, "trail_activate_atr": 99, "max_hold_bars": 360, "cooldown_bars": 30},
        "Breakout":           {"sl_atr_mult": 1.0, "tp_atr_mult": 2.0, "trail_atr_mult": 99, "trail_activate_atr": 99, "max_hold_bars": 300, "cooldown_bars": 30},
        "FVG Structure":      {"sl_atr_mult": 1.0, "tp_atr_mult": 2.0, "trail_atr_mult": 99, "trail_activate_atr": 99, "max_hold_bars": 240, "cooldown_bars": 30},
        "RSI Extreme":        {"sl_atr_mult": 1.0, "tp_atr_mult": 2.0, "trail_atr_mult": 99, "trail_activate_atr": 99, "max_hold_bars": 180, "cooldown_bars": 30},
        "Multi-EMA Momentum": {"sl_atr_mult": 1.0, "tp_atr_mult": 2.0, "trail_atr_mult": 99, "trail_activate_atr": 99, "max_hold_bars": 360, "cooldown_bars": 20},
    },
    "5m": {
        "EMA Trend Follow":   {"sl_atr_mult": 1.0, "tp_atr_mult": 2.0, "trail_atr_mult": 99, "trail_activate_atr": 99, "max_hold_bars": 180, "cooldown_bars": 12},
        "Breakout":           {"sl_atr_mult": 1.0, "tp_atr_mult": 2.0, "trail_atr_mult": 99, "trail_activate_atr": 99, "max_hold_bars": 144, "cooldown_bars": 12},
        "FVG Structure":      {"sl_atr_mult": 1.0, "tp_atr_mult": 2.0, "trail_atr_mult": 99, "trail_activate_atr": 99, "max_hold_bars": 180, "cooldown_bars": 15},
        "RSI Extreme":        {"sl_atr_mult": 1.0, "tp_atr_mult": 2.0, "trail_atr_mult": 99, "trail_activate_atr": 99, "max_hold_bars": 120, "cooldown_bars": 12},
        "Multi-EMA Momentum": {"sl_atr_mult": 1.0, "tp_atr_mult": 2.0, "trail_atr_mult": 99, "trail_activate_atr": 99, "max_hold_bars": 180, "cooldown_bars": 10},
    },
    "15m": {
        "EMA Trend Follow":   {"sl_atr_mult": 1.0, "tp_atr_mult": 2.0, "trail_atr_mult": 99, "trail_activate_atr": 99, "max_hold_bars": 96, "cooldown_bars": 4},
        "Breakout":           {"sl_atr_mult": 1.0, "tp_atr_mult": 2.0, "trail_atr_mult": 99, "trail_activate_atr": 99, "max_hold_bars": 96, "cooldown_bars": 4},
        "FVG Structure":      {"sl_atr_mult": 1.0, "tp_atr_mult": 2.0, "trail_atr_mult": 99, "trail_activate_atr": 99, "max_hold_bars": 96, "cooldown_bars": 6},
        "RSI Extreme":        {"sl_atr_mult": 1.0, "tp_atr_mult": 2.0, "trail_atr_mult": 99, "trail_activate_atr": 99, "max_hold_bars": 64, "cooldown_bars": 4},
        "Multi-EMA Momentum": {"sl_atr_mult": 1.0, "tp_atr_mult": 2.0, "trail_atr_mult": 99, "trail_activate_atr": 99, "max_hold_bars": 96, "cooldown_bars": 4},
    },
}


# ─── Report Generation ──────────────────────────────────────────

def calc_buy_and_hold(df: pd.DataFrame) -> dict:
    """Calculate Buy & Hold benchmark."""
    start_px = df["close"].iloc[0]
    end_px = df["close"].iloc[-1]
    ret = (end_px - start_px) / start_px
    # Max drawdown
    eq = df["close"] / start_px * INIT_EQUITY
    peak = eq.cummax()
    dd = ((eq - peak) / peak).min()
    return {
        "start_price": start_px,
        "end_price": end_px,
        "return_pct": ret * 100,
        "return_usd": ret * BUDGET_USDT,
        "max_drawdown_pct": dd * 100,
    }


def generate_report(all_results: dict, data_info: dict, buy_hold: dict = None) -> str:
    """Generate comprehensive .md report."""

    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    md = []
    md.append("# BTC Futures Backtest -- Timeframe Comparison Report")
    md.append(f"\n**Generated:** {now}")
    md.append(f"**Symbol:** {SYMBOL} (Binance Futures)")
    md.append(f"**Period:** {LOOKBACK_DAYS} days (~1 year)")
    md.append(f"**Initial Equity:** ${INIT_EQUITY:,.0f}")
    md.append(f"**Position Size:** ${BUDGET_USDT:,.0f} per trade (x{LEVERAGE} leverage)")
    md.append(f"**Fees:** Taker {FEE_BPS} bps + Slippage {SLIP_BPS} bps per side")
    md.append(f"**Signal Lag:** 1 bar (entry at next bar open)")

    # Data summary
    md.append("\n## Data Summary\n")
    md.append("| Timeframe | Candles | Date Range | BTC Price Range |")
    md.append("|-----------|---------|------------|-----------------|")
    for tf in TIMEFRAMES:
        info = data_info.get(tf, {})
        md.append(f"| {tf} | {info.get('count', 0):,} | {info.get('start', 'N/A')} -> {info.get('end', 'N/A')} | ${info.get('low', 0):,.0f} -- ${info.get('high', 0):,.0f} |")

    # Buy & Hold benchmark
    if buy_hold:
        md.append("\n## Benchmark: Buy & Hold\n")
        md.append("| Metric | Value |")
        md.append("|--------|-------|")
        md.append(f"| Start Price | ${buy_hold['start_price']:,.2f} |")
        md.append(f"| End Price | ${buy_hold['end_price']:,.2f} |")
        md.append(f"| Return | {buy_hold['return_pct']:+.2f}% (${buy_hold['return_usd']:+,.2f} per $1,000) |")
        md.append(f"| Max Drawdown | {buy_hold['max_drawdown_pct']:.2f}% |")
        md.append("")

    # ── Grand comparison table ──
    md.append("\n## Overall Comparison -- All Strategies x All Timeframes\n")
    md.append("| Strategy | TF | Trades | Win% | PF | Net PnL | Net% | MaxDD% | Sharpe | Sortino | R:R | Trades/Day |")
    md.append("|----------|-----|--------|------|----|---------|------|--------|--------|---------|-----|------------|")

    # Collect and sort by net PnL
    rows = []
    for (tf, strat_name), m in all_results.items():
        rows.append((tf, strat_name, m))
    rows.sort(key=lambda x: x[2].get("net_pnl", 0), reverse=True)

    for tf, strat_name, m in rows:
        pnl_str = f"${m['net_pnl']:+,.2f}"
        dd_str = f"{m['max_drawdown_pct']:.2f}%"
        md.append(
            f"| {strat_name} | {tf} | {m['total_trades']} | {m['win_rate']:.1f}% | "
            f"{m['profit_factor']:.2f} | {pnl_str} | {m['net_pnl_pct']:+.2f}% | {dd_str} | "
            f"{m['sharpe']:.3f} | {m['sortino']:.3f} | {m['rr_ratio']:.2f} | {m['trades_per_day']:.1f} |"
        )

    # ── Per-TF detailed sections ──
    for tf in TIMEFRAMES:
        md.append(f"\n---\n## Timeframe: {tf}\n")

        tf_results = {k[1]: v for k, v in all_results.items() if k[0] == tf}
        if not tf_results:
            md.append("*No results*\n")
            continue

        # Sort strategies by net PnL
        sorted_strats = sorted(tf_results.items(), key=lambda x: x[1].get("net_pnl", 0), reverse=True)

        for rank, (strat_name, m) in enumerate(sorted_strats, 1):
            emoji_rank = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th"}.get(rank, f"{rank}th")
            md.append(f"### {emoji_rank} -- {strat_name}\n")

            md.append("| Metric | Value |")
            md.append("|--------|-------|")
            md.append(f"| Total Trades | {m['total_trades']} |")
            md.append(f"| Win Rate | {m['win_rate']:.2f}% |")
            md.append(f"| Profit Factor | {m['profit_factor']:.3f} |")
            md.append(f"| Net PnL | ${m['net_pnl']:+,.2f} ({m['net_pnl_pct']:+.2f}%) |")
            md.append(f"| Final Equity | ${m['final_equity']:,.2f} |")
            md.append(f"| Max Drawdown | {m['max_drawdown_pct']:.2f}% |")
            md.append(f"| Sharpe Ratio | {m['sharpe']:.3f} |")
            md.append(f"| Sortino Ratio | {m['sortino']:.3f} |")
            md.append(f"| Risk/Reward | {m['rr_ratio']:.3f} |")
            md.append(f"| Avg PnL/Trade | ${m['avg_pnl_per_trade']:.2f} |")
            md.append(f"| Avg Win | ${m['avg_win']:.2f} |")
            md.append(f"| Avg Loss | -${m['avg_loss']:.2f} |")
            md.append(f"| Best Trade | ${m['best_trade']:.2f} |")
            md.append(f"| Worst Trade | ${m['worst_trade']:.2f} |")
            md.append(f"| Avg Holding | {m['avg_holding_bars']:.1f} bars ({m['avg_holding_hours']:.1f}h) |")
            md.append(f"| Exposure | {m['exposure_pct']:.2f}% |")
            md.append(f"| Trades/Day | {m['trades_per_day']:.2f} |")
            md.append(f"| Max Win Streak | {m['max_win_streak']} |")
            md.append(f"| Max Loss Streak | {m['max_loss_streak']} |")
            md.append(f"| Long Trades | {m['long_trades']} (WR: {m['long_win_rate']:.1f}%) |")
            md.append(f"| Short Trades | {m['short_trades']} (WR: {m['short_win_rate']:.1f}%) |")
            md.append("")

    # ── Timeframe Recommendation ──
    md.append("\n---\n## Timeframe Recommendation\n")

    # Best by net PnL
    best_pnl = max(rows, key=lambda x: x[2]["net_pnl"])
    # Best by Sharpe
    best_sharpe = max(rows, key=lambda x: x[2]["sharpe"])
    # Best by win rate (min 10 trades)
    qualified = [r for r in rows if r[2]["total_trades"] >= 10]
    best_wr = max(qualified, key=lambda x: x[2]["win_rate"]) if qualified else best_pnl
    # Best by profit factor (min 10 trades)
    best_pf = max(qualified, key=lambda x: x[2]["profit_factor"]) if qualified else best_pnl
    # Lowest drawdown (profitable only)
    profitable = [r for r in rows if r[2]["net_pnl"] > 0]
    best_dd = max(profitable, key=lambda x: x[2]["max_drawdown_pct"]) if profitable else best_pnl  # max because DD is negative

    md.append("| Category | TF | Strategy | Value |")
    md.append("|----------|-----|----------|-------|")
    md.append(f"| Highest Net PnL | {best_pnl[0]} | {best_pnl[1]} | ${best_pnl[2]['net_pnl']:+,.2f} |")
    md.append(f"| Best Sharpe | {best_sharpe[0]} | {best_sharpe[1]} | {best_sharpe[2]['sharpe']:.3f} |")
    md.append(f"| Highest Win Rate | {best_wr[0]} | {best_wr[1]} | {best_wr[2]['win_rate']:.1f}% |")
    md.append(f"| Best Profit Factor | {best_pf[0]} | {best_pf[1]} | {best_pf[2]['profit_factor']:.3f} |")
    if profitable:
        md.append(f"| Lowest Drawdown | {best_dd[0]} | {best_dd[1]} | {best_dd[2]['max_drawdown_pct']:.2f}% |")

    # TF aggregate analysis
    md.append("\n### Aggregate by Timeframe\n")
    md.append("| TF | Avg Win Rate | Avg PF | Avg Net PnL | Total Trades | Avg Sharpe |")
    md.append("|----|-------------|--------|-------------|--------------|------------|")
    for tf in TIMEFRAMES:
        tf_rows = [r for r in rows if r[0] == tf]
        if tf_rows:
            avg_wr = np.mean([r[2]["win_rate"] for r in tf_rows])
            avg_pf = np.mean([r[2]["profit_factor"] for r in tf_rows])
            avg_pnl = np.mean([r[2]["net_pnl"] for r in tf_rows])
            total_tr = sum(r[2]["total_trades"] for r in tf_rows)
            avg_sh = np.mean([r[2]["sharpe"] for r in tf_rows])
            md.append(f"| {tf} | {avg_wr:.1f}% | {avg_pf:.3f} | ${avg_pnl:+,.2f} | {total_tr} | {avg_sh:.3f} |")

    # Key findings
    md.append("\n### Key Findings\n")

    # Determine overall best TF
    tf_scores = {}
    for tf in TIMEFRAMES:
        tf_rows = [r for r in rows if r[0] == tf and r[2]["total_trades"] >= 5]
        if tf_rows:
            avg_pnl = np.mean([r[2]["net_pnl"] for r in tf_rows])
            avg_sharpe = np.mean([r[2]["sharpe"] for r in tf_rows])
            avg_pf = np.mean([r[2]["profit_factor"] for r in tf_rows])
            # Composite score
            score = avg_sharpe * 40 + (1 if avg_pnl > 0 else -1) * 30 + min(avg_pf, 3) * 10
            tf_scores[tf] = score

    if tf_scores:
        best_tf = max(tf_scores, key=tf_scores.get)
        md.append(f"- **Best overall timeframe: {best_tf}** (composite score based on Sharpe, PnL, and Profit Factor)")

    md.append(f"- **Best single strategy: {best_pnl[1]} on {best_pnl[0]}** (${best_pnl[2]['net_pnl']:+,.2f} net PnL)")

    if best_sharpe[2]["sharpe"] > 0:
        md.append(f"- **Most risk-adjusted: {best_sharpe[1]} on {best_sharpe[0]}** (Sharpe {best_sharpe[2]['sharpe']:.3f})")

    # Warnings
    md.append("\n### Important Notes\n")
    md.append("- This is a **backtest simulation** -- real trading involves additional risks (latency, partial fills, market impact)")
    md.append("- Lower timeframes (1m) generate more trades but face higher fee impact per trade")
    md.append("- Higher timeframes (15m) have fewer trades but wider TP/SL, reducing noise")
    md.append("- Results assume consistent market conditions -- regime changes can invalidate strategies")
    md.append("- **No leverage optimization** was done -- results use 1x leverage")
    md.append("- Past performance does not guarantee future results")

    # Execution details
    md.append("\n---\n## Execution Details\n")
    md.append("| Parameter | Value |")
    md.append("|-----------|-------|")
    md.append(f"| Taker Fee | {FEE_BPS} bps ({FEE*100:.3f}%) per side |")
    md.append(f"| Slippage | {SLIP_BPS} bps ({SLIP*100:.4f}%) per side |")
    md.append(f"| Total Cost per Round-Trip | {(FEE+SLIP)*2*100:.3f}% |")
    md.append(f"| Signal Lag | 1 bar |")
    md.append(f"| Entry | Next bar open after signal |")
    md.append(f"| Position Sizing | Fixed notional (${BUDGET_USDT:,.0f}) |")
    md.append(f"| Leverage | {LEVERAGE}x |")
    md.append(f"| Data Source | Binance Futures (USDM) |")

    return "\n".join(md)


# ─── Main ────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("BTC Futures Backtest -- 1m / 5m / 15m Comparison")
    print("=" * 60)

    # ── 1) Fetch data ──
    print("\n[1/3] Fetching data from Binance Futures...")
    data = {}
    data_info = {}

    for tf in TIMEFRAMES:
        df = load_or_fetch(SYMBOL, tf, LOOKBACK_DAYS)
        if df.empty:
            print(f"  WARNING: No data for {tf}, skipping.")
            continue
        df = add_indicators(df)
        data[tf] = df
        data_info[tf] = {
            "count": len(df),
            "start": str(df["date_time"].iloc[0])[:16],
            "end": str(df["date_time"].iloc[-1])[:16],
            "low": df["low"].min(),
            "high": df["high"].max(),
        }

    if not data:
        print("ERROR: No data fetched for any timeframe. Check API keys.")
        sys.exit(1)

    # ── 2) Run backtests ──
    print(f"\n[2/3] Running backtests ({len(data)} TFs x {len(STRATEGIES)} strategies)...")
    all_results = {}
    all_trades = {}

    for tf in TIMEFRAMES:
        if tf not in data:
            continue
        df = data[tf]
        tf_min = TF_MINUTES[tf]

        print(f"\n  --- {tf} ({len(df):,} candles) ---")

        for strat_name, strat_fn in STRATEGIES.items():
            signals = strat_fn(df)
            n_signals = (signals != 0).sum()
            print(f"    {strat_name}: {n_signals} signals", end="")

            params = STRATEGY_PARAMS[tf][strat_name]
            trades = run_backtest(df, signals, tf_min, **params)

            metrics = calc_metrics(trades, tf_min, len(df))
            all_results[(tf, strat_name)] = metrics
            all_trades[(tf, strat_name)] = trades

            print(f" -> {metrics['total_trades']} trades, WR={metrics['win_rate']:.1f}%, PnL=${metrics['net_pnl']:+,.2f}")

    # ── Buy & Hold benchmark ──
    buy_hold = None
    # Use 15m data for benchmark (or first available)
    bench_tf = "15m" if "15m" in data else list(data.keys())[0]
    buy_hold = calc_buy_and_hold(data[bench_tf])
    print(f"\n  Buy & Hold: {buy_hold['return_pct']:+.2f}% (${buy_hold['start_price']:,.0f} -> ${buy_hold['end_price']:,.0f})")

    # ── 3) Generate report ──
    print(f"\n[3/3] Generating report...")
    report = generate_report(all_results, data_info, buy_hold)

    report_path = os.path.join(os.path.dirname(__file__), "backtest_tf_comparison.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)

    print(f"\nReport saved to: {report_path}")

    # Also save trade details
    for (tf, strat_name), trades in all_trades.items():
        if not trades.empty:
            detail_dir = os.path.join(os.path.dirname(__file__), "backtest_details")
            Path(detail_dir).mkdir(exist_ok=True)
            safe_name = strat_name.replace(" ", "_").lower()
            trades.to_csv(os.path.join(detail_dir, f"trades_{tf}_{safe_name}.csv"), index=False)

    print("Trade details saved to backtest_details/")
    print("\nDone!")


if __name__ == "__main__":
    main()
