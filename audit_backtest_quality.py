"""
Backtest Entry Quality Audit
=============================
Runs backtest and analyzes EACH trade's entry quality:
- RSI at entry (overbought/oversold?)
- Position in 20-bar range (top/bottom?)
- Volume ratio (momentum?)
- Price change 2h before (FOLLOW vs CONTRA?)
- Timing classification (EARLY/MID/LATE in move)
"""

import sys, io, warnings, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
import pandas_ta as ta

from backtest_15m_btc_led_alts import (
    fetch_binance_15m, load_btc_db_data, build_btc_features,
    compute_btc_composite_score, build_alt_technicals,
    run_backtest, calc_metrics,
    BKK_UTC_OFFSET, INIT_EQUITY, COMPOSITE_WEIGHTS
)
from test_v12_improvements import V11_CONFIGS

TEST_START = pd.Timestamp("2026-01-01")
COINS = ["BTC", "XRP", "ADA", "DOT", "SUI", "FIL"]


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


print("=== LOADING DATA ===")
btc_ohlcv = fetch_binance_15m("BTCUSDT")
db_data = load_btc_db_data()

print("\n=== BUILDING BTC FEATURES ===")
btc_df = build_btc_features(btc_ohlcv, db_data)
btc_score = compute_btc_composite_score(btc_df)  # uses v3 COMPOSITE_WEIGHTS
btc_score_ts = btc_score.copy()
btc_score_ts.index = btc_df["ts"]

all_trades = []

for coin in COINS:
    print(f"\n--- {coin} ---")
    cfg = V11_CONFIGS[coin]
    symbol = f"{coin}USDT"

    alt_ohlcv = fetch_binance_15m(symbol) if coin != "BTC" else btc_ohlcv
    alt_df = build_alt_technicals(alt_ohlcv)

    # Add entry quality indicators
    alt_df["rsi"] = ta.rsi(alt_df["close"], length=14)
    alt_df["ema50"] = alt_df["close"].ewm(span=50).mean()
    alt_df["ema50_dist"] = (alt_df["close"] - alt_df["ema50"]) / alt_df["ema50"] * 100
    alt_df["high_20"] = alt_df["high"].rolling(20).max()
    alt_df["low_20"] = alt_df["low"].rolling(20).min()
    alt_df["range_20"] = alt_df["high_20"] - alt_df["low_20"]
    alt_df["pos_in_range"] = (alt_df["close"] - alt_df["low_20"]) / alt_df["range_20"].clip(lower=1e-10)
    alt_df["vol_r"] = alt_df["volume"] / alt_df["volume"].rolling(20).mean()
    alt_df["chg_2h_before"] = alt_df["close"].pct_change(8) * 100

    signals, alt_merged = gen_signal(btc_score_ts, alt_df, cfg["threshold"], cfg["alt_pa"])

    # Filter OOS
    oos_mask = alt_merged["ts"] >= TEST_START
    df_oos = alt_merged[oos_mask].reset_index(drop=True)
    sig_oos = signals[oos_mask].reset_index(drop=True)

    trades = run_backtest(df_oos, sig_oos,
                          sl_atr_mult=cfg["sl"], tp_atr_mult=cfg["tp"],
                          trail_atr_mult=cfg["trail"], trail_activate_atr=cfg["trail_act"],
                          cooldown_bars=cfg["cd"])

    if trades.empty:
        continue

    # Add entry quality metrics
    for idx, row in trades.iterrows():
        ei = int(row["entry_idx"])
        if ei < len(df_oos):
            trades.loc[idx, "rsi_entry"] = df_oos.loc[ei, "rsi"]
            trades.loc[idx, "pos_range_entry"] = df_oos.loc[ei, "pos_in_range"]
            trades.loc[idx, "vol_ratio_entry"] = df_oos.loc[ei, "vol_r"]
            trades.loc[idx, "chg_2h_before"] = df_oos.loc[ei, "chg_2h_before"]
            trades.loc[idx, "ema50_dist"] = df_oos.loc[ei, "ema50_dist"]

            chg_before = df_oos.loc[ei, "chg_2h_before"]
            direction = 1 if row["dir"] == "L" else -1

            if direction == 1:
                trades.loc[idx, "entry_type"] = "FOLLOW" if chg_before > 0 else "CONTRA"
            else:
                trades.loc[idx, "entry_type"] = "FOLLOW" if chg_before < 0 else "CONTRA"

            pos = df_oos.loc[ei, "pos_in_range"]
            rsi = df_oos.loc[ei, "rsi"]
            if pd.isna(pos) or pd.isna(rsi):
                trades.loc[idx, "timing"] = "UNKNOWN"
            elif direction == 1:  # LONG
                if pos < 0.35 and rsi < 45:
                    trades.loc[idx, "timing"] = "EARLY"
                elif pos > 0.7 or rsi > 65:
                    trades.loc[idx, "timing"] = "LATE"
                else:
                    trades.loc[idx, "timing"] = "MID"
            else:  # SHORT
                if pos > 0.65 and rsi > 55:
                    trades.loc[idx, "timing"] = "EARLY"
                elif pos < 0.3 or rsi < 35:
                    trades.loc[idx, "timing"] = "LATE"
                else:
                    trades.loc[idx, "timing"] = "MID"

    trades["coin"] = coin
    all_trades.append(trades)
    m = calc_metrics(trades, len(df_oos))
    print(f"  {m['total']} trades, WR {m['win_rate']:.1f}%, PnL ${m['net_pnl']:,.0f}")

# Combine all
at = pd.concat(all_trades, ignore_index=True)
print(f"\n{'='*70}")
print(f"TOTAL: {len(at)} trades, PnL ${at['pnl_net'].sum():,.0f}")
print(f"{'='*70}")

# --- ANALYSIS ---

print("\n=== 1. ENTRY TYPE (FOLLOW vs CONTRA) ===")
for et in ["FOLLOW", "CONTRA"]:
    sub = at[at["entry_type"] == et]
    if len(sub) == 0:
        continue
    wins = (sub["pnl_net"] > 0).sum()
    total = len(sub)
    wr = wins / total * 100
    pnl = sub["pnl_net"].sum()
    print(f"  {et}: {total} trades ({total/len(at)*100:.0f}%), WR {wr:.1f}%, PnL ${pnl:,.0f}")

print("\n=== 2. ENTRY TIMING (EARLY / MID / LATE) ===")
for timing in ["EARLY", "MID", "LATE"]:
    sub = at[at["timing"] == timing]
    if len(sub) == 0:
        continue
    wins = (sub["pnl_net"] > 0).sum()
    total = len(sub)
    wr = wins / total * 100
    pnl = sub["pnl_net"].sum()
    avg = sub["pnl_net"].mean()
    print(f"  {timing}: {total} trades ({total/len(at)*100:.0f}%), WR {wr:.1f}%, PnL ${pnl:,.0f}, avg ${avg:.1f}")

print("\n=== 3. CROSS-TAB: ENTRY TYPE x TIMING ===")
for et in ["FOLLOW", "CONTRA"]:
    for timing in ["EARLY", "MID", "LATE"]:
        sub = at[(at["entry_type"] == et) & (at["timing"] == timing)]
        if len(sub) == 0:
            continue
        wins = (sub["pnl_net"] > 0).sum()
        wr = wins / len(sub) * 100
        pnl = sub["pnl_net"].sum()
        print(f"  {et:7s} + {timing:5s}: {len(sub):3d} trades, WR {wr:.1f}%, PnL ${pnl:>8,.0f}")

print("\n=== 4. BY DIRECTION + TIMING ===")
for d in ["L", "S"]:
    sub = at[at["dir"] == d]
    label = "LONG" if d == "L" else "SHORT"
    wins = (sub["pnl_net"] > 0).sum()
    wr = wins / len(sub) * 100
    pnl = sub["pnl_net"].sum()
    print(f"\n  {label}: {len(sub)} trades, WR {wr:.1f}%, PnL ${pnl:,.0f}")

    for timing in ["EARLY", "MID", "LATE"]:
        t_sub = sub[sub["timing"] == timing]
        if len(t_sub) == 0:
            continue
        t_wr = (t_sub["pnl_net"] > 0).sum() / len(t_sub) * 100
        t_pnl = t_sub["pnl_net"].sum()
        print(f"    {timing:5s}: {len(t_sub):3d} ({len(t_sub)/len(sub)*100:.0f}%), WR {t_wr:.0f}%, PnL ${t_pnl:>7,.0f}")

print("\n=== 5. RSI DISTRIBUTION AT ENTRY ===")
for d in ["L", "S"]:
    sub = at[at["dir"] == d]
    label = "LONG" if d == "L" else "SHORT"
    rsi = sub["rsi_entry"].dropna()
    if len(rsi) == 0:
        continue
    print(f"  {label}: RSI mean={rsi.mean():.1f}, median={rsi.median():.1f}, "
          f"<35={(rsi < 35).sum()}, 35-65={((rsi >= 35) & (rsi <= 65)).sum()}, >65={(rsi > 65).sum()}")

print("\n=== 6. POS IN RANGE AT ENTRY ===")
for d in ["L", "S"]:
    sub = at[at["dir"] == d]
    label = "LONG" if d == "L" else "SHORT"
    pos = sub["pos_range_entry"].dropna()
    if len(pos) == 0:
        continue
    print(f"  {label}: pos mean={pos.mean():.2f}, "
          f"<0.3={(pos < 0.3).sum()}, 0.3-0.7={((pos >= 0.3) & (pos <= 0.7)).sum()}, >0.7={(pos > 0.7).sum()}")

print("\n=== 7. EXIT REASON BREAKDOWN ===")
for reason in at["exit_reason"].unique():
    sub = at[at["exit_reason"] == reason]
    wr = (sub["pnl_net"] > 0).sum() / len(sub) * 100
    pnl = sub["pnl_net"].sum()
    print(f"  {reason:12s}: {len(sub):3d} trades, WR {wr:.0f}%, PnL ${pnl:>8,.0f}")

# === 8. REGIME ANALYSIS (BULL/BEAR/FLAT weeks) ===
print("\n=== 8. REGIME ANALYSIS (weekly BTC return) ===")
# Classify each trade's week as BULL/BEAR/FLAT based on BTC weekly return
btc_weekly = btc_df.set_index("ts")["close"].resample("W").last().pct_change() * 100
at["entry_time"] = pd.to_datetime(at["entry_time"])
at["week"] = at["entry_time"].dt.to_period("W").dt.start_time

for _, row in at.iterrows():
    week_start = row["week"]
    # Find closest week in btc_weekly
    diffs = abs(btc_weekly.index - week_start)
    if len(diffs) == 0:
        at.loc[_, "regime"] = "UNKNOWN"
        continue
    closest_idx = diffs.argmin()
    wk_ret = btc_weekly.iloc[closest_idx]
    if wk_ret > 2:
        at.loc[_, "regime"] = "BULL"
    elif wk_ret < -2:
        at.loc[_, "regime"] = "BEAR"
    else:
        at.loc[_, "regime"] = "FLAT"

for regime in ["BULL", "BEAR", "FLAT"]:
    sub = at[at["regime"] == regime]
    if len(sub) == 0:
        continue
    wins = (sub["pnl_net"] > 0).sum()
    wr = wins / len(sub) * 100
    pnl = sub["pnl_net"].sum()
    n_long = len(sub[sub["dir"] == "L"])
    n_short = len(sub[sub["dir"] == "S"])
    long_wr = (sub[(sub["dir"] == "L") & (sub["pnl_net"] > 0)].shape[0] / max(n_long, 1)) * 100
    short_wr = (sub[(sub["dir"] == "S") & (sub["pnl_net"] > 0)].shape[0] / max(n_short, 1)) * 100
    print(f"  {regime:4s}: {len(sub):3d} trades, WR {wr:.1f}%, PnL ${pnl:>8,.0f} | "
          f"LONG {n_long} ({long_wr:.0f}%) SHORT {n_short} ({short_wr:.0f}%)")

# Save
at.to_csv("experiments/backtest_entry_quality_audit_v3.csv", index=False)
print("\nSaved to experiments/backtest_entry_quality_audit_v3.csv")
