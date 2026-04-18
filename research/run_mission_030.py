"""
Mission 030: Post-Surgery Health Check
========================================
ระบบผ่าตัดใหญ่ 04-04: ถอด v6, ปิด LONG, ตัดจาก 46→13 เหรียญ, trail widened, alt filter
คำถาม: ผ่าตัดสำเร็จไหม? pre vs post เป็นอย่างไร?

EXP1: Pre vs Post Surgery PnL/WR
EXP2: Daily PnL Trend (day-by-day)
EXP3: Alt Filter Effectiveness (611 skips since 04-03)
EXP4: LONG Disabled Impact (saved losses estimate)
EXP5: Per-Coin Health Post-Surgery (13 remaining coins)
EXP6: Exit Reason Distribution Pre vs Post
"""

import sys, json, logging
import numpy as np, pandas as pd
from pathlib import Path
from datetime import datetime, timedelta

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import sqlite3

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

# ---- Config ----
SURGERY_DATE = "2026-04-04"  # v6 removed, LONG disabled, coins trimmed
ALT_FILTER_DATE = "2026-04-03"  # alt filter + trail widened deployed
DB_PATH = BASE_DIR / "paper_trading" / "state" / "paper_trades.db"

# Coins that survived the surgery (from config.py)
SURVIVING_V3 = ["BTC", "XRP", "ADA", "SUI", "RENDER", "BEAT", "PIXEL", "AXS", "SOL", "ETH", "1000BONK"]
SURVIVING_V5 = ["ARIA", "AAVE"]
SURVIVING = SURVIVING_V3 + SURVIVING_V5
REMOVED_COINS = [
    "DOT", "FIL", "NEAR", "ARB",  # ex-v3
    "FARTCOIN", "GALA", "AVAX", "UNI", "SEI", "DOGE", "ONDO",
    "1000SHIB", "BNB", "WIF", "CRV", "TAO", "ACX",  # ex-v5
    "OGN", "SAHARA", "ASTER", "LTC", "ZRO", "NAORIS", "1000PEPE",
    "JCT", "DEGO", "HYPE", "PENGU", "LINK", "BARD", "BANANAS31", "PIPPIN",  # ex-v6
]

results = {}


def load_trades():
    conn = sqlite3.connect(str(DB_PATH))
    trades = pd.read_sql("SELECT * FROM trades ORDER BY entry_time", conn)
    conn.close()
    trades["entry_dt"] = pd.to_datetime(trades["entry_time"])
    trades["exit_dt"] = pd.to_datetime(trades["exit_time"])
    trades["entry_date"] = trades["entry_dt"].dt.strftime("%Y-%m-%d")
    trades["dir_label"] = trades["direction"].map({1: "LONG", -1: "SHORT"})
    return trades


def load_signal_log():
    conn = sqlite3.connect(str(DB_PATH))
    sl = pd.read_sql("SELECT * FROM signal_log ORDER BY ts", conn)
    conn.close()
    sl["ts_dt"] = pd.to_datetime(sl["ts"])
    sl["date"] = sl["ts_dt"].dt.strftime("%Y-%m-%d")
    return sl


def calc_stats(df, label=""):
    if len(df) == 0:
        return {"label": label, "trades": 0, "pnl": 0, "wr": 0, "avg_pnl": 0, "avg_win": 0, "avg_loss": 0}
    wins = df[df["pnl_net"] > 0]
    losses = df[df["pnl_net"] <= 0]
    return {
        "label": label,
        "trades": len(df),
        "pnl": round(df["pnl_net"].sum(), 2),
        "wr": round(100 * len(wins) / len(df), 1),
        "avg_pnl": round(df["pnl_net"].mean(), 2),
        "avg_win": round(wins["pnl_net"].mean(), 2) if len(wins) > 0 else 0,
        "avg_loss": round(losses["pnl_net"].mean(), 2) if len(losses) > 0 else 0,
        "profit_factor": round(wins["pnl_net"].sum() / abs(losses["pnl_net"].sum()), 2) if len(losses) > 0 and losses["pnl_net"].sum() != 0 else 999,
    }


# ===============================================
# EXP1: Pre vs Post Surgery
# ===============================================
def exp1_pre_post_surgery(trades):
    log.info("=" * 60)
    log.info("EXP1: Pre vs Post Surgery (cutoff: %s)", SURGERY_DATE)

    pre = trades[trades["entry_date"] < SURGERY_DATE]
    post = trades[trades["entry_date"] >= SURGERY_DATE]

    pre_stats = calc_stats(pre, "PRE (03-17 to 04-03)")
    post_stats = calc_stats(post, "POST (04-04 to now)")

    # Also break down post by direction
    post_short = post[post["direction"] == -1]
    post_long = post[post["direction"] == 1]
    post_short_stats = calc_stats(post_short, "POST SHORT")
    post_long_stats = calc_stats(post_long, "POST LONG")

    # Pre: only surviving coins (fair comparison)
    pre_surv = pre[pre["coin"].isin(SURVIVING)]
    pre_surv_stats = calc_stats(pre_surv, "PRE (surviving coins only)")

    # Pre: removed coins
    pre_removed = pre[pre["coin"].isin(REMOVED_COINS)]
    pre_removed_stats = calc_stats(pre_removed, "PRE (removed coins)")

    # Pre: only short trades on surviving coins
    pre_surv_short = pre_surv[pre_surv["direction"] == -1]
    pre_surv_short_stats = calc_stats(pre_surv_short, "PRE surviving SHORT-only")

    log.info("PRE  all:        %d trades, $%.0f PnL, WR %.1f%%", pre_stats["trades"], pre_stats["pnl"], pre_stats["wr"])
    log.info("PRE  surviving:  %d trades, $%.0f PnL, WR %.1f%%", pre_surv_stats["trades"], pre_surv_stats["pnl"], pre_surv_stats["wr"])
    log.info("PRE  removed:    %d trades, $%.0f PnL, WR %.1f%%", pre_removed_stats["trades"], pre_removed_stats["pnl"], pre_removed_stats["wr"])
    log.info("PRE  surv SHORT: %d trades, $%.0f PnL, WR %.1f%%", pre_surv_short_stats["trades"], pre_surv_short_stats["pnl"], pre_surv_short_stats["wr"])
    log.info("POST all:        %d trades, $%.0f PnL, WR %.1f%%", post_stats["trades"], post_stats["pnl"], post_stats["wr"])
    log.info("POST SHORT:      %d trades, $%.0f PnL, WR %.1f%%", post_short_stats["trades"], post_short_stats["pnl"], post_short_stats["wr"])
    log.info("POST LONG:       %d trades, $%.0f PnL, WR %.1f%%", post_long_stats["trades"], post_long_stats["pnl"], post_long_stats["wr"])

    # PnL per day comparison
    pre_days = max(1, (pd.to_datetime(SURGERY_DATE) - pre["entry_dt"].min()).days)
    post_days = max(1, (post["entry_dt"].max() - pd.to_datetime(SURGERY_DATE)).days + 1)
    pre_pnl_per_day = pre_surv_stats["pnl"] / pre_days
    post_pnl_per_day = post_stats["pnl"] / post_days

    log.info("\nPnL per day: PRE surviving $%.1f/day (%d days), POST $%.1f/day (%d days)",
             pre_pnl_per_day, pre_days, post_pnl_per_day, post_days)

    results["exp1"] = {
        "pre_all": pre_stats,
        "pre_surviving": pre_surv_stats,
        "pre_removed": pre_removed_stats,
        "pre_surviving_short": pre_surv_short_stats,
        "post_all": post_stats,
        "post_short": post_short_stats,
        "post_long": post_long_stats,
        "pre_days": pre_days,
        "post_days": post_days,
        "pre_pnl_per_day": round(pre_pnl_per_day, 2),
        "post_pnl_per_day": round(post_pnl_per_day, 2),
    }


# ===============================================
# EXP2: Daily PnL Trend
# ===============================================
def exp2_daily_pnl(trades):
    log.info("\n" + "=" * 60)
    log.info("EXP2: Daily PnL Trend")

    daily = trades.groupby("entry_date").agg(
        trades=("pnl_net", "count"),
        pnl=("pnl_net", "sum"),
        wr=("pnl_net", lambda x: 100 * (x > 0).sum() / len(x)),
        short_pnl=("pnl_net", lambda x: x[trades.loc[x.index, "direction"] == -1].sum()),
        long_pnl=("pnl_net", lambda x: x[trades.loc[x.index, "direction"] == 1].sum()),
    ).reset_index()
    daily["cum_pnl"] = daily["pnl"].cumsum()
    daily["period"] = daily["entry_date"].apply(lambda d: "POST" if d >= SURGERY_DATE else "PRE")

    log.info("\n%-12s %6s %8s %6s %8s", "Date", "Trades", "PnL", "WR%", "CumPnL")
    log.info("-" * 48)
    for _, row in daily.iterrows():
        marker = " ***" if row["entry_date"] == SURGERY_DATE else ""
        log.info("%-12s %6d %8.1f %5.1f%% %8.1f%s",
                 row["entry_date"], row["trades"], row["pnl"], row["wr"], row["cum_pnl"], marker)

    # Post-surgery stats
    post_daily = daily[daily["period"] == "POST"]
    if len(post_daily) > 0:
        win_days = (post_daily["pnl"] > 0).sum()
        lose_days = (post_daily["pnl"] <= 0).sum()
        log.info("\nPost-surgery: %d win days, %d lose days", win_days, lose_days)

    results["exp2"] = {
        "daily": daily[["entry_date", "trades", "pnl", "wr", "cum_pnl", "period"]].to_dict("records"),
        "post_win_days": int(win_days) if len(post_daily) > 0 else 0,
        "post_lose_days": int(lose_days) if len(post_daily) > 0 else 0,
    }


# ===============================================
# EXP3: Alt Filter Effectiveness
# ===============================================
def exp3_alt_filter(signal_log, trades):
    log.info("\n" + "=" * 60)
    log.info("EXP3: Alt Filter Effectiveness (since %s)", ALT_FILTER_DATE)

    # Alt filter skips
    alt_skips = signal_log[signal_log["action"] == "SKIP_ALT_FILTER"]
    alt_skips_post = alt_skips[alt_skips["date"] >= ALT_FILTER_DATE]

    log.info("Total SKIP_ALT_FILTER: %d (all time), %d (since %s)",
             len(alt_skips), len(alt_skips_post), ALT_FILTER_DATE)

    # How many signals per day were blocked?
    if len(alt_skips_post) > 0:
        skip_daily = alt_skips_post.groupby("date").size().reset_index(name="skips")
        log.info("\nAlt filter blocks per day:")
        for _, row in skip_daily.iterrows():
            log.info("  %s: %d entries blocked", row["date"], row["skips"])

        # By coin
        skip_by_coin = alt_skips_post.groupby("coin").size().sort_values(ascending=False)
        log.info("\nBlocked by coin (top 10):")
        for coin, cnt in skip_by_coin.head(10).items():
            log.info("  %-12s %d blocked", coin, cnt)

        # Compare: signal_log entries vs actual trades post-filter
        total_signals_post = signal_log[
            (signal_log["date"] >= ALT_FILTER_DATE) &
            (signal_log["action"].isin(["OPEN_SHORT", "OPEN_LONG"]))
        ]
        log.info("\nPost-filter: %d entries opened, %d blocked by alt filter",
                 len(total_signals_post), len(alt_skips_post))
        block_rate = len(alt_skips_post) / max(1, len(total_signals_post) + len(alt_skips_post)) * 100
        log.info("Block rate: %.1f%%", block_rate)

        results["exp3"] = {
            "total_skips": len(alt_skips),
            "skips_since_deploy": len(alt_skips_post),
            "daily_skips": skip_daily.to_dict("records"),
            "top_blocked_coins": skip_by_coin.head(10).to_dict(),
            "entries_opened_post": len(total_signals_post),
            "block_rate_pct": round(block_rate, 1),
        }
    else:
        results["exp3"] = {"total_skips": 0, "note": "No alt filter data found"}


# ===============================================
# EXP4: LONG Disabled Impact
# ===============================================
def exp4_long_disabled(signal_log, trades):
    log.info("\n" + "=" * 60)
    log.info("EXP4: LONG Disabled Impact")

    # LONG disabled skips
    long_skips = signal_log[signal_log["action"] == "SKIP_LONG_DISABLED"]
    log.info("Total SKIP_LONG_DISABLED: %d", len(long_skips))

    if len(long_skips) > 0:
        skip_daily = long_skips.groupby("date").size().reset_index(name="skips")
        log.info("\nLONG blocked per day:")
        for _, row in skip_daily.iterrows():
            log.info("  %s: %d entries blocked", row["date"], row["skips"])

    # Estimate: what if LONG was still on? Use pre-surgery LONG stats
    pre_long = trades[
        (trades["entry_date"] < SURGERY_DATE) &
        (trades["direction"] == 1) &
        (trades["coin"].isin(SURVIVING))
    ]
    pre_long_stats = calc_stats(pre_long, "PRE LONG (surviving coins)")

    # Estimated lost PnL if LONG was still active
    if len(long_skips) > 0 and len(pre_long) > 0:
        avg_long_pnl = pre_long["pnl_net"].mean()
        estimated_lost = len(long_skips) * avg_long_pnl
        log.info("\nPre-surgery LONG stats (surviving coins):")
        log.info("  Trades: %d, WR: %.1f%%, Avg PnL: $%.2f, Total: $%.2f",
                 pre_long_stats["trades"], pre_long_stats["wr"],
                 avg_long_pnl, pre_long_stats["pnl"])
        log.info("  Estimated savings from disabling LONG: $%.2f (avoided %d bad entries)",
                 -estimated_lost, len(long_skips))

    results["exp4"] = {
        "long_skips": len(long_skips),
        "pre_long_surviving_stats": pre_long_stats,
        "estimated_savings": round(-estimated_lost, 2) if len(long_skips) > 0 and len(pre_long) > 0 else 0,
    }


# ===============================================
# EXP5: Per-Coin Health Post-Surgery
# ===============================================
def exp5_per_coin_health(trades):
    log.info("\n" + "=" * 60)
    log.info("EXP5: Per-Coin Health Post-Surgery")

    post = trades[trades["entry_date"] >= SURGERY_DATE]

    coin_stats = []
    for coin in SURVIVING:
        ct = post[post["coin"] == coin]
        if len(ct) == 0:
            coin_stats.append({"coin": coin, "trades": 0, "pnl": 0, "wr": 0, "avg_pnl": 0})
            continue
        wins = ct[ct["pnl_net"] > 0]
        coin_stats.append({
            "coin": coin,
            "model": "v5" if coin in SURVIVING_V5 else "v3",
            "trades": len(ct),
            "pnl": round(ct["pnl_net"].sum(), 2),
            "wr": round(100 * len(wins) / len(ct), 1),
            "avg_pnl": round(ct["pnl_net"].mean(), 2),
            "avg_bars": round(ct["bars_held"].mean(), 1),
        })

    coin_df = pd.DataFrame(coin_stats).sort_values("pnl", ascending=False)

    log.info("\n%-12s %5s %6s %8s %6s %6s %6s", "Coin", "Model", "Trades", "PnL", "WR%", "AvgPnL", "AvgBars")
    log.info("-" * 58)
    for _, row in coin_df.iterrows():
        if row["trades"] == 0:
            log.info("%-12s %5s %6d %8s", row["coin"], row.get("model", "?"), 0, "no trades")
            continue
        log.info("%-12s %5s %6d %8.1f %5.1f%% %6.2f %6.1f",
                 row["coin"], row.get("model", "?"), row["trades"], row["pnl"], row["wr"], row["avg_pnl"], row["avg_bars"])

    profitable = coin_df[coin_df["pnl"] > 0]
    unprofitable = coin_df[(coin_df["pnl"] <= 0) & (coin_df["trades"] > 0)]
    log.info("\nProfitable: %d coins ($%.1f)", len(profitable), profitable["pnl"].sum())
    log.info("Unprofitable: %d coins ($%.1f)", len(unprofitable), unprofitable["pnl"].sum())

    results["exp5"] = {
        "coin_stats": coin_df.to_dict("records"),
        "profitable_coins": len(profitable),
        "unprofitable_coins": len(unprofitable),
        "profitable_pnl": round(profitable["pnl"].sum(), 2),
        "unprofitable_pnl": round(unprofitable["pnl"].sum(), 2) if len(unprofitable) > 0 else 0,
    }


# ===============================================
# EXP6: Exit Reason Distribution Pre vs Post
# ===============================================
def exp6_exit_reasons(trades):
    log.info("\n" + "=" * 60)
    log.info("EXP6: Exit Reason Distribution Pre vs Post")

    pre = trades[(trades["entry_date"] < SURGERY_DATE) & (trades["coin"].isin(SURVIVING))]
    post = trades[trades["entry_date"] >= SURGERY_DATE]

    for label, df in [("PRE (surviving)", pre), ("POST", post)]:
        if len(df) == 0:
            continue
        log.info("\n%s:", label)
        exit_stats = df.groupby("exit_reason").agg(
            trades=("pnl_net", "count"),
            pnl=("pnl_net", "sum"),
            wr=("pnl_net", lambda x: 100 * (x > 0).sum() / len(x)),
            avg_pnl=("pnl_net", "mean"),
        ).reset_index()
        exit_stats["pct"] = 100 * exit_stats["trades"] / exit_stats["trades"].sum()
        exit_stats = exit_stats.sort_values("trades", ascending=False)

        log.info("  %-15s %6s %5s %8s %6s %6s", "Exit", "Trades", "%", "PnL", "WR%", "AvgPnL")
        for _, row in exit_stats.iterrows():
            log.info("  %-15s %6d %4.1f%% %8.1f %5.1f%% %6.2f",
                     row["exit_reason"], row["trades"], row["pct"],
                     row["pnl"], row["wr"], row["avg_pnl"])

    # Post exit stats
    post_exit = post.groupby("exit_reason").agg(
        trades=("pnl_net", "count"),
        pnl=("pnl_net", "sum"),
        wr=("pnl_net", lambda x: 100 * (x > 0).sum() / len(x)),
    ).reset_index()
    post_exit["pct"] = 100 * post_exit["trades"] / post_exit["trades"].sum()

    results["exp6"] = {
        "post_exit_stats": post_exit.to_dict("records"),
    }


# ===============================================
# MAIN
# ===============================================
def main():
    log.info("Mission 030: Post-Surgery Health Check")
    log.info("Surgery date: %s", SURGERY_DATE)
    log.info("Alt filter date: %s\n", ALT_FILTER_DATE)

    trades = load_trades()
    signal_log = load_signal_log()

    log.info("Loaded %d trades, %d signal_log entries", len(trades), len(signal_log))

    exp1_pre_post_surgery(trades)
    exp2_daily_pnl(trades)
    exp3_alt_filter(signal_log, trades)
    exp4_long_disabled(signal_log, trades)
    exp5_per_coin_health(trades)
    exp6_exit_reasons(trades)

    # ---- Summary ----
    log.info("\n" + "=" * 60)
    log.info("SUMMARY")
    log.info("=" * 60)

    e1 = results["exp1"]
    log.info("Pre-surgery (surviving coins): $%.0f PnL, WR %.1f%%, $%.1f/day",
             e1["pre_surviving"]["pnl"], e1["pre_surviving"]["wr"], e1["pre_pnl_per_day"])
    log.info("Post-surgery:                  $%.0f PnL, WR %.1f%%, $%.1f/day",
             e1["post_all"]["pnl"], e1["post_all"]["wr"], e1["post_pnl_per_day"])

    if e1["post_pnl_per_day"] > e1["pre_pnl_per_day"]:
        log.info("VERDICT: Surgery IMPROVED daily PnL by $%.1f/day",
                 e1["post_pnl_per_day"] - e1["pre_pnl_per_day"])
    else:
        log.info("VERDICT: Surgery has NOT improved daily PnL yet (delta: $%.1f/day)",
                 e1["post_pnl_per_day"] - e1["pre_pnl_per_day"])

    # Save results
    out_json = BASE_DIR / "missions" / "mission_030_post_surgery_health.json"
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False, default=str)
    log.info("\nSaved: %s", out_json)

    return results


if __name__ == "__main__":
    main()
