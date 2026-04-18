"""
Mission 033: News Volume Spike as Volatility / Trade Quality Signal

สมมติฐาน: เมื่อจำนวนข่าว crypto spike ขึ้น (volume สูง)
- ตลาดมีความสนใจสูง → volatility เพิ่ม
- ข่าวเยอะ = uncertainty สูง → ควรหลีกเลี่ยงหรือปรับ SL/TP
- ข่าว sentiment (directional) fail แล้ว แต่ volume ยังไม่ได้ลอง

6 Experiments:
1. News volume distribution & time patterns
2. News volume → BTC realized volatility correlation
3. News volume → v3 trade WR / PnL
4. News volume spike as entry filter
5. Sentiment clustering during volume spikes
6. News volume as SL/TP adjuster
"""

import sys, logging, json, warnings
import numpy as np, pandas as pd
from pathlib import Path
from datetime import datetime, timedelta

warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import backtest_15m_btc_led_alts as bt
from paper_trading.config import COMPOSITE_WEIGHTS, V3_EXTRA_WEIGHTS, COIN_CONFIGS
from paper_trading.strategy import score_ob_combined, score_basis_contrarian, score_tick_liq
import psycopg2
from research.config import get_pg_dsn

BKK_UTC_OFFSET = timedelta(hours=7)
OOS_START = "2025-01-01"
OOS_END = "2026-03-31"

results = {}

# ============================================================
# LOAD DATA
# ============================================================
log.info("Loading BTC data and computing v3 score...")
btc_ohlcv = bt.fetch_binance_15m("BTCUSDT", years=3)
if "date_time" in btc_ohlcv.columns:
    btc_ohlcv = btc_ohlcv.rename(columns={"date_time": "ts"})

db_data = bt.load_btc_db_data()
btc_df = bt.build_btc_features(btc_ohlcv, db_data)

btc_score = bt.compute_btc_composite_score(btc_df, COMPOSITE_WEIGHTS)
btc_score = btc_score + score_ob_combined(btc_df, weight=V3_EXTRA_WEIGHTS["ob_combined"])
btc_score = btc_score + score_basis_contrarian(btc_df, weight=V3_EXTRA_WEIGHTS["basis_contrarian"])
btc_score = btc_score + score_tick_liq(btc_df, weight=V3_EXTRA_WEIGHTS["tick_liq"])

btc_score_ts = pd.Series(btc_score.values, index=btc_df["ts"].values, name="btc_score")

log.info("Loading news data...")
conn = psycopg2.connect(get_pg_dsn())
news_df = pd.read_sql("SELECT news_time, prefix, sentiment FROM public.news_crypto ORDER BY news_time", conn)
conn.close()

news_df["news_time"] = pd.to_datetime(news_df["news_time"], utc=True).dt.tz_localize(None) - BKK_UTC_OFFSET
news_df = news_df[(news_df["news_time"] >= OOS_START) & (news_df["news_time"] <= OOS_END)]
log.info(f"News in OOS period: {len(news_df)} rows")

# ============================================================
# EXP1: News Volume Distribution & Time Patterns
# ============================================================
log.info("=== EXP1: News Volume Distribution ===")

news_df["ts_15m"] = news_df["news_time"].dt.floor("15min")
news_df["ts_1h"] = news_df["news_time"].dt.floor("1h")
news_df["ts_4h"] = news_df["news_time"].dt.floor("4h")
news_df["hour"] = news_df["news_time"].dt.hour

news_per_15m = news_df.groupby("ts_15m").size().reset_index(name="news_count_15m")
news_per_1h = news_df.groupby("ts_1h").size().reset_index(name="news_count_1h")
news_per_4h = news_df.groupby("ts_4h").size().reset_index(name="news_count_4h")

hourly_pattern = news_df.groupby("hour").size()
hourly_pattern_norm = hourly_pattern / hourly_pattern.sum() * 100

exp1 = {
    "total_news_oos": int(len(news_df)),
    "news_per_15m_mean": round(news_per_15m["news_count_15m"].mean(), 2),
    "news_per_15m_median": int(news_per_15m["news_count_15m"].median()),
    "news_per_15m_p90": int(news_per_15m["news_count_15m"].quantile(0.90)),
    "news_per_15m_p95": int(news_per_15m["news_count_15m"].quantile(0.95)),
    "news_per_15m_max": int(news_per_15m["news_count_15m"].max()),
    "news_per_1h_mean": round(news_per_1h["news_count_1h"].mean(), 2),
    "news_per_1h_p90": int(news_per_1h["news_count_1h"].quantile(0.90)),
    "news_per_1h_max": int(news_per_1h["news_count_1h"].max()),
    "busiest_hour_utc": int(hourly_pattern.idxmax()),
    "quietest_hour_utc": int(hourly_pattern.idxmin()),
    "hourly_pattern": {int(k): round(v, 1) for k, v in hourly_pattern_norm.items()},
}
results["exp1_volume_distribution"] = exp1
log.info(f"  News/15m: mean={exp1['news_per_15m_mean']}, P90={exp1['news_per_15m_p90']}, max={exp1['news_per_15m_max']}")
log.info(f"  Busiest hour UTC: {exp1['busiest_hour_utc']}, Quietest: {exp1['quietest_hour_utc']}")

# ============================================================
# EXP2: News Volume → BTC Realized Volatility
# ============================================================
log.info("=== EXP2: News Volume → Volatility Correlation ===")

btc_oos = btc_df[(btc_df["ts"] >= OOS_START) & (btc_df["ts"] <= OOS_END)].copy()
btc_oos["return_abs"] = btc_oos["close"].pct_change().abs()
btc_oos["vol_4bar"] = btc_oos["return_abs"].rolling(4).std()
btc_oos["vol_16bar"] = btc_oos["return_abs"].rolling(16).std()
btc_oos["range_pct"] = (btc_oos["high"] - btc_oos["low"]) / btc_oos["close"] * 100

btc_oos["ts_15m"] = btc_oos["ts"].dt.floor("15min")
merged = btc_oos.merge(news_per_15m, on="ts_15m", how="left")
merged["news_count_15m"] = merged["news_count_15m"].fillna(0)

news_1h_renamed = news_per_1h.rename(columns={"ts_1h": "ts_1h_key"})
merged["ts_1h_key"] = merged["ts"].dt.floor("1h")
merged = merged.merge(news_1h_renamed, on="ts_1h_key", how="left")
merged["news_count_1h"] = merged["news_count_1h"].fillna(0)

news_4h_renamed = news_per_4h.rename(columns={"ts_4h": "ts_4h_key"})
merged["ts_4h_key"] = merged["ts"].dt.floor("4h")
merged = merged.merge(news_4h_renamed, on="ts_4h_key", how="left")
merged["news_count_4h"] = merged["news_count_4h"].fillna(0)

valid = merged.dropna(subset=["vol_4bar", "range_pct"])

corr_15m_vol = valid["news_count_15m"].corr(valid["vol_4bar"])
corr_1h_vol = valid["news_count_1h"].corr(valid["vol_4bar"])
corr_4h_vol = valid["news_count_4h"].corr(valid["vol_4bar"])
corr_15m_range = valid["news_count_15m"].corr(valid["range_pct"])
corr_1h_range = valid["news_count_1h"].corr(valid["range_pct"])

# Forward-looking: news volume now → volatility next 1h (4 bars)
merged["vol_next_4bar"] = merged["return_abs"].shift(-1).rolling(4).std().shift(-4)
merged["range_next_1h"] = merged["range_pct"].shift(-1).rolling(4).mean().shift(-4)
valid_fwd = merged.dropna(subset=["vol_next_4bar"])

corr_1h_fwd_vol = valid_fwd["news_count_1h"].corr(valid_fwd["vol_next_4bar"])
corr_4h_fwd_vol = valid_fwd["news_count_4h"].corr(valid_fwd["vol_next_4bar"])

# Bucketed analysis
valid["news_bucket"] = pd.cut(valid["news_count_1h"], bins=[-0.1, 0, 1, 3, 999],
                              labels=["0_none", "1_low", "2_mid", "3_high"])
vol_by_news_q = valid.groupby("news_bucket")["range_pct"].agg(["mean", "std", "count"])

exp2 = {
    "corr_news15m_vol4bar": round(corr_15m_vol, 4),
    "corr_news1h_vol4bar": round(corr_1h_vol, 4),
    "corr_news4h_vol4bar": round(corr_4h_vol, 4),
    "corr_news15m_range": round(corr_15m_range, 4),
    "corr_news1h_range": round(corr_1h_range, 4),
    "corr_news1h_fwd_vol": round(corr_1h_fwd_vol, 4),
    "corr_news4h_fwd_vol": round(corr_4h_fwd_vol, 4),
    "vol_by_news_quartile": {str(k): {"mean_range_pct": round(v["mean"], 4), "count": int(v["count"])}
                             for k, v in vol_by_news_q.iterrows()},
}
results["exp2_news_vol_correlation"] = exp2
log.info(f"  Corr news_1h → vol: {corr_1h_vol:.4f}, fwd: {corr_1h_fwd_vol:.4f}")
log.info(f"  Corr news_4h → vol: {corr_4h_vol:.4f}, fwd: {corr_4h_fwd_vol:.4f}")

# ============================================================
# EXP3: News Volume → v3 Trade WR / PnL
# ============================================================
log.info("=== EXP3: News Volume → Trade Quality ===")

coins = ["BTCUSDT", "XRPUSDT", "ADAUSDT", "DOTUSDT", "SUIUSDT", "FILUSDT"]
all_trades = []
for symbol in coins:
    coin = symbol.replace("USDT", "")
    ohlcv = bt.fetch_binance_15m(symbol, years=3)
    if "date_time" in ohlcv.columns:
        ohlcv = ohlcv.rename(columns={"date_time": "ts"})
    alt_df = bt.build_alt_technicals(ohlcv)
    oos_mask = (alt_df["ts"] >= OOS_START) & (alt_df["ts"] <= OOS_END)
    cfg = COIN_CONFIGS.get(coin, {})
    signals, alt_merged = bt.generate_btc_led_signal(
        btc_score_ts, alt_df[oos_mask],
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
log.info(f"  Total OOS trades: {len(trades_df)}")

trades_df["entry_ts_1h"] = pd.to_datetime(trades_df["entry_time"]).dt.floor("1h")
trades_df["entry_ts_4h"] = pd.to_datetime(trades_df["entry_time"]).dt.floor("4h")

trades_with_news = trades_df.merge(
    news_per_1h.rename(columns={"ts_1h": "entry_ts_1h"}), on="entry_ts_1h", how="left")
trades_with_news["news_count_1h"] = trades_with_news["news_count_1h"].fillna(0)

trades_with_news = trades_with_news.merge(
    news_per_4h.rename(columns={"ts_4h": "entry_ts_4h"}), on="entry_ts_4h", how="left")
trades_with_news["news_count_4h"] = trades_with_news["news_count_4h"].fillna(0)

trades_with_news["win"] = trades_with_news["pnl_net"] > 0

# Quartile analysis
try:
    trades_with_news["news_q_1h"] = pd.qcut(trades_with_news["news_count_1h"], q=4,
                                              labels=["Q1_quiet", "Q2", "Q3", "Q4_busy"], duplicates="drop")
except ValueError:
    trades_with_news["news_q_1h"] = pd.cut(trades_with_news["news_count_1h"], bins=4,
                                             labels=["Q1_quiet", "Q2", "Q3", "Q4_busy"])

try:
    trades_with_news["news_q_4h"] = pd.qcut(trades_with_news["news_count_4h"], q=4,
                                              labels=["Q1_quiet", "Q2", "Q3", "Q4_busy"], duplicates="drop")
except ValueError:
    trades_with_news["news_q_4h"] = pd.cut(trades_with_news["news_count_4h"], bins=4,
                                             labels=["Q1_quiet", "Q2", "Q3", "Q4_busy"])

wr_by_1h = trades_with_news.groupby("news_q_1h").agg(
    trades=("win", "count"), wr=("win", "mean"), pnl=("pnl_net", "sum"), avg_pnl=("pnl_net", "mean")
).round(4)

wr_by_4h = trades_with_news.groupby("news_q_4h").agg(
    trades=("win", "count"), wr=("win", "mean"), pnl=("pnl_net", "sum"), avg_pnl=("pnl_net", "mean")
).round(4)

exp3 = {
    "total_trades": int(len(trades_with_news)),
    "wr_by_news_1h_quartile": {str(k): {"trades": int(v["trades"]), "wr": round(v["wr"]*100, 1),
                                          "pnl": round(v["pnl"], 1), "avg_pnl": round(v["avg_pnl"], 2)}
                                for k, v in wr_by_1h.iterrows()},
    "wr_by_news_4h_quartile": {str(k): {"trades": int(v["trades"]), "wr": round(v["wr"]*100, 1),
                                          "pnl": round(v["pnl"], 1), "avg_pnl": round(v["avg_pnl"], 2)}
                                for k, v in wr_by_4h.iterrows()},
    "corr_news1h_win": round(trades_with_news["news_count_1h"].corr(trades_with_news["win"].astype(float)), 4),
    "corr_news1h_pnl": round(trades_with_news["news_count_1h"].corr(trades_with_news["pnl_net"]), 4),
}
results["exp3_trade_quality"] = exp3
log.info(f"  WR by 1h news: {json.dumps(exp3['wr_by_news_1h_quartile'], indent=2)}")

# ============================================================
# EXP4: News Volume Spike as Entry Filter
# ============================================================
log.info("=== EXP4: News Volume Spike Entry Filter ===")

baseline_pnl = trades_with_news["pnl_net"].sum()
baseline_wr = trades_with_news["win"].mean()
baseline_count = len(trades_with_news)

filter_results = []
for threshold in [2, 3, 4, 5, 6, 8]:
    filtered = trades_with_news[trades_with_news["news_count_1h"] <= threshold]
    if len(filtered) < 10:
        continue
    f_pnl = filtered["pnl_net"].sum()
    f_wr = filtered["win"].mean()
    f_count = len(filtered)
    filter_results.append({
        "max_news_1h": threshold,
        "trades": int(f_count),
        "trades_pct": round(f_count / baseline_count * 100, 1),
        "pnl": round(f_pnl, 1),
        "delta_pnl": round(f_pnl - baseline_pnl, 1),
        "wr": round(f_wr * 100, 1),
        "delta_wr_pp": round((f_wr - baseline_wr) * 100, 1),
    })

# Also test: only trade during high news volume
for threshold in [3, 4, 5, 6]:
    high_news = trades_with_news[trades_with_news["news_count_1h"] >= threshold]
    if len(high_news) < 10:
        continue
    filter_results.append({
        "min_news_1h": threshold,
        "trades": int(len(high_news)),
        "trades_pct": round(len(high_news) / baseline_count * 100, 1),
        "pnl": round(high_news["pnl_net"].sum(), 1),
        "delta_pnl": round(high_news["pnl_net"].sum() - baseline_pnl, 1),
        "wr": round(high_news["win"].mean() * 100, 1),
        "delta_wr_pp": round((high_news["win"].mean() - baseline_wr) * 100, 1),
    })

exp4 = {
    "baseline_pnl": round(baseline_pnl, 1),
    "baseline_wr": round(baseline_wr * 100, 1),
    "baseline_trades": baseline_count,
    "filter_results": filter_results,
}
results["exp4_entry_filter"] = exp4
log.info(f"  Baseline: {baseline_count} trades, PnL ${baseline_pnl:.0f}, WR {baseline_wr*100:.1f}%")
for fr in filter_results:
    label = f"max_news<={fr.get('max_news_1h','')}" if "max_news_1h" in fr else f"min_news>={fr.get('min_news_1h','')}"
    log.info(f"  {label}: {fr['trades']} trades, PnL ${fr['pnl']:.0f}, ΔPnL ${fr['delta_pnl']:.0f}, WR {fr['wr']}%")

# ============================================================
# EXP5: Sentiment Clustering During Volume Spikes
# ============================================================
log.info("=== EXP5: Sentiment During Volume Spikes ===")

news_1h_sent = news_df.groupby("ts_1h").agg(
    total=("sentiment", "count"),
    bullish=("sentiment", lambda x: (x == "bullish").sum()),
    bearish=("sentiment", lambda x: (x == "bearish").sum()),
    neutral=("sentiment", lambda x: (x == "neutral").sum()),
).reset_index()

news_1h_sent["bull_ratio"] = news_1h_sent["bullish"] / news_1h_sent["total"]
news_1h_sent["bear_ratio"] = news_1h_sent["bearish"] / news_1h_sent["total"]
news_1h_sent["net_sentiment"] = (news_1h_sent["bullish"] - news_1h_sent["bearish"]) / news_1h_sent["total"]

high_vol_news = news_1h_sent[news_1h_sent["total"] >= news_1h_sent["total"].quantile(0.90)]
low_vol_news = news_1h_sent[news_1h_sent["total"] <= news_1h_sent["total"].quantile(0.25)]

# Merge with BTC returns
btc_oos_1h = btc_oos.copy()
btc_oos_1h["ts_1h"] = btc_oos_1h["ts"].dt.floor("1h")
btc_hourly = btc_oos_1h.groupby("ts_1h").agg(
    ret_1h=("return_abs", lambda x: x.iloc[-1] if len(x) > 0 else np.nan),
    close=("close", "last"),
).reset_index()
btc_hourly["ret_1h_signed"] = btc_hourly["close"].pct_change()

sent_with_btc = news_1h_sent.merge(btc_hourly[["ts_1h", "ret_1h_signed"]], on="ts_1h", how="inner")

# During high news volume: does sentiment predict direction?
high_sent = sent_with_btc[sent_with_btc["total"] >= sent_with_btc["total"].quantile(0.90)]
corr_sent_ret = high_sent["net_sentiment"].corr(high_sent["ret_1h_signed"])

# Contrarian test: when news very bullish → BTC goes down?
very_bullish = sent_with_btc[sent_with_btc["bull_ratio"] >= 0.5]
very_bearish = sent_with_btc[sent_with_btc["bear_ratio"] >= 0.3]

exp5 = {
    "high_vol_news_hours": int(len(high_vol_news)),
    "high_vol_avg_bull_ratio": round(high_vol_news["bull_ratio"].mean(), 3),
    "high_vol_avg_bear_ratio": round(high_vol_news["bear_ratio"].mean(), 3),
    "low_vol_avg_bull_ratio": round(low_vol_news["bull_ratio"].mean(), 3),
    "corr_sentiment_btc_return_high_vol": round(corr_sent_ret, 4),
    "very_bullish_hours": int(len(very_bullish)),
    "very_bullish_avg_btc_return": round(very_bullish["ret_1h_signed"].mean() * 100, 4) if len(very_bullish) > 0 else None,
    "very_bearish_hours": int(len(very_bearish)),
    "very_bearish_avg_btc_return": round(very_bearish["ret_1h_signed"].mean() * 100, 4) if len(very_bearish) > 0 else None,
}
results["exp5_sentiment_clustering"] = exp5
log.info(f"  High vol news: bull_ratio={exp5['high_vol_avg_bull_ratio']}, bear_ratio={exp5['high_vol_avg_bear_ratio']}")
log.info(f"  Corr sentiment→BTC return (high vol): {corr_sent_ret:.4f}")

# ============================================================
# EXP6: News Volume as Adaptive SL/TP Signal
# ============================================================
log.info("=== EXP6: Adaptive SL/TP by News Volume ===")

# Split trades into quiet vs busy news periods and test different SL/TP
median_news = trades_with_news["news_count_1h"].median()
quiet_trades = trades_with_news[trades_with_news["news_count_1h"] <= median_news]
busy_trades = trades_with_news[trades_with_news["news_count_1h"] > median_news]

quiet_stats = {
    "trades": int(len(quiet_trades)),
    "wr": round(quiet_trades["win"].mean() * 100, 1) if len(quiet_trades) > 0 else 0,
    "pnl": round(quiet_trades["pnl_net"].sum(), 1),
    "avg_pnl": round(quiet_trades["pnl_net"].mean(), 2) if len(quiet_trades) > 0 else 0,
}
busy_stats = {
    "trades": int(len(busy_trades)),
    "wr": round(busy_trades["win"].mean() * 100, 1) if len(busy_trades) > 0 else 0,
    "pnl": round(busy_trades["pnl_net"].sum(), 1),
    "avg_pnl": round(busy_trades["pnl_net"].mean(), 2) if len(busy_trades) > 0 else 0,
}

# Analyze exit reasons by news volume
quiet_exits = quiet_trades.groupby("exit_reason").agg(
    count=("win", "count"), wr=("win", "mean"), pnl=("pnl_net", "sum")
).round(3)
busy_exits = busy_trades.groupby("exit_reason").agg(
    count=("win", "count"), wr=("win", "mean"), pnl=("pnl_net", "sum")
).round(3)

# Direction analysis: SHORT vs LONG during quiet vs busy
for direction_label, dir_val in [("SHORT", "S"), ("LONG", "L")]:
    for period_label, period_df in [("quiet", quiet_trades), ("busy", busy_trades)]:
        dir_trades = period_df[period_df["dir"] == dir_val]
        if len(dir_trades) > 0:
            log.info(f"  {direction_label} {period_label}: {len(dir_trades)} trades, WR {dir_trades['win'].mean()*100:.1f}%, PnL ${dir_trades['pnl_net'].sum():.0f}")

exp6 = {
    "median_news_1h": float(median_news),
    "quiet_period": quiet_stats,
    "busy_period": busy_stats,
    "quiet_exit_reasons": {str(k): {"count": int(v["count"]), "wr": round(v["wr"]*100, 1), "pnl": round(v["pnl"], 1)}
                           for k, v in quiet_exits.iterrows()},
    "busy_exit_reasons": {str(k): {"count": int(v["count"]), "wr": round(v["wr"]*100, 1), "pnl": round(v["pnl"], 1)}
                          for k, v in busy_exits.iterrows()},
}
results["exp6_adaptive_sltp"] = exp6
log.info(f"  Quiet: {quiet_stats['trades']} trades, WR {quiet_stats['wr']}%, PnL ${quiet_stats['pnl']}")
log.info(f"  Busy: {busy_stats['trades']} trades, WR {busy_stats['wr']}%, PnL ${busy_stats['pnl']}")

# ============================================================
# SAVE RESULTS
# ============================================================
log.info("Saving results...")

# Save raw JSON
output_json = BASE_DIR / "missions" / "mission_033_news_volume_spike.json"
with open(output_json, "w", encoding="utf-8") as f:
    json.dump(results, f, indent=2, ensure_ascii=False, default=str)

log.info(f"Saved: {output_json}")
log.info("=== Mission 033 Complete ===")

# Print summary for report
print("\n" + "="*60)
print("MISSION 033 SUMMARY")
print("="*60)
print(f"\nEXP1: News volume - mean {exp1['news_per_15m_mean']}/15m, P90={exp1['news_per_15m_p90']}, max={exp1['news_per_15m_max']}")
print(f"EXP2: News→Vol corr: 1h={corr_1h_vol:.4f}, 4h={corr_4h_vol:.4f}, fwd_1h={corr_1h_fwd_vol:.4f}")
print(f"EXP3: Trade quality - corr news→WR: {exp3['corr_news1h_win']}, corr news→PnL: {exp3['corr_news1h_pnl']}")
print(f"EXP4: Best filter result:")
if filter_results:
    best = max(filter_results, key=lambda x: x.get("delta_pnl", -99999))
    print(f"  {best}")
print(f"EXP5: Sentiment corr high-vol hours: {corr_sent_ret:.4f}")
print(f"EXP6: Quiet WR={quiet_stats['wr']}% PnL=${quiet_stats['pnl']} | Busy WR={busy_stats['wr']}% PnL=${busy_stats['pnl']}")
