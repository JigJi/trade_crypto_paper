"""
Mission 029: Coin Performance Triage
=====================================
วิเคราะห์ per-coin paper trading 21 วัน (2026-03-17 to 04-06)
จัดกลุ่ม KEEP / WATCH / DROP เพื่อปรับ portfolio
"""
import sys, json, sqlite3
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

# ---- Load paper trades ----
conn = sqlite3.connect(str(BASE_DIR / "paper_trading/state/paper_trades.db"))
trades = pd.read_sql("SELECT * FROM trades ORDER BY entry_time", conn)
conn.close()

trades["entry_dt"] = pd.to_datetime(trades["entry_time"])
trades["date"] = trades["entry_dt"].dt.date
trades["dir_label"] = trades["direction"].map({1: "LONG", -1: "SHORT"})
trades["week_start"] = trades["entry_dt"].dt.to_period("W").apply(lambda x: x.start_time.date())

# ---- Per-coin analysis ----
coin_stats = []
for coin in trades["coin"].unique():
    ct = trades[trades["coin"] == coin]
    n = len(ct)
    pnl = ct["pnl_net"].sum()
    wr = (ct["pnl_net"] > 0).mean() * 100
    avg_pnl = ct["pnl_net"].mean()
    avg_bars = ct["bars_held"].mean()

    flip = ct[ct["exit_reason"] == "SIGNAL_FLIP"]
    trail = ct[ct["exit_reason"] == "TRAIL"]
    tp = ct[ct["exit_reason"] == "TP"]
    sl = ct[ct["exit_reason"].isin(["SL", "SL/TP"])]

    flip_pct = len(flip) / n * 100
    flip_wr = (flip["pnl_net"] > 0).mean() * 100 if len(flip) > 0 else 50
    trail_wr = (trail["pnl_net"] > 0).mean() * 100 if len(trail) > 0 else 0

    weekly = ct.groupby("week_start")["pnl_net"].sum()
    neg_weeks = int((weekly < 0).sum())
    total_weeks = len(weekly)
    consistency = 1 - (neg_weeks / total_weeks) if total_weeks > 0 else 0

    short_sub = ct[ct["dir_label"] == "SHORT"]
    long_sub = ct[ct["dir_label"] == "LONG"]
    short_pnl = short_sub["pnl_net"].sum() if len(short_sub) > 0 else 0
    long_pnl = long_sub["pnl_net"].sum() if len(long_sub) > 0 else 0
    short_wr = (short_sub["pnl_net"] > 0).mean() * 100 if len(short_sub) > 0 else 0
    long_wr = (long_sub["pnl_net"] > 0).mean() * 100 if len(long_sub) > 0 else 0

    # Triage score
    score = 0
    score += min(pnl / 10, 30)
    score += (wr - 40) * 0.5
    score += consistency * 10
    score += (flip_wr - 20) * 0.3
    score -= max(0, flip_pct - 50) * 0.2

    tier = "KEEP" if score > 5 else ("WATCH" if score > -5 else "DROP")

    coin_stats.append({
        "coin": coin, "n": n, "pnl": round(pnl, 2), "wr": round(wr, 1),
        "avg_pnl": round(avg_pnl, 2), "avg_bars": round(avg_bars, 1),
        "flip_pct": round(flip_pct, 1), "flip_wr": round(flip_wr, 1),
        "trail_n": len(trail), "trail_wr": round(trail_wr, 1),
        "tp_n": len(tp), "sl_n": len(sl), "flip_n": len(flip),
        "neg_weeks": neg_weeks, "total_weeks": total_weeks,
        "consistency": round(consistency, 2),
        "short_pnl": round(short_pnl, 2), "long_pnl": round(long_pnl, 2),
        "short_wr": round(short_wr, 1), "long_wr": round(long_wr, 1),
        "score": round(score, 1), "tier": tier,
    })

df = pd.DataFrame(coin_stats).sort_values("score", ascending=False)

# ---- Summary ----
keep = df[df["tier"] == "KEEP"]
watch = df[df["tier"] == "WATCH"]
drop = df[df["tier"] == "DROP"]

overall_short = trades[trades["dir_label"] == "SHORT"]
overall_long = trades[trades["dir_label"] == "LONG"]

result = {
    "total_trades": len(trades),
    "total_coins": len(df),
    "total_pnl": round(trades["pnl_net"].sum(), 2),
    "overall_wr": round((trades["pnl_net"] > 0).mean() * 100, 1),
    "short_pnl": round(overall_short["pnl_net"].sum(), 2),
    "short_wr": round((overall_short["pnl_net"] > 0).mean() * 100, 1),
    "long_pnl": round(overall_long["pnl_net"].sum(), 2),
    "long_wr": round((overall_long["pnl_net"] > 0).mean() * 100, 1),
    "keep_coins": len(keep),
    "keep_pnl": round(keep["pnl"].sum(), 2),
    "watch_coins": len(watch),
    "watch_pnl": round(watch["pnl"].sum(), 2),
    "drop_coins": len(drop),
    "drop_pnl": round(drop["pnl"].sum(), 2),
    "if_drop_removed_pnl": round(keep["pnl"].sum() + watch["pnl"].sum(), 2),
    "drop_list": list(drop["coin"].values),
    "keep_list": list(keep["coin"].values),
    "coin_details": df.to_dict(orient="records"),
}

# ---- Save JSON ----
out_json = BASE_DIR / "missions" / "mission_029_coin_triage.json"
with open(out_json, "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=2, default=str)

# Also save to experiments
exp_json = BASE_DIR / "experiments" / "mission_029_coin_triage.json"
with open(exp_json, "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=2, default=str)

print(f"Saved: {out_json}")
print(f"Saved: {exp_json}")
print(f"\nKEEP: {len(keep)} coins, PnL=${keep['pnl'].sum():+.2f}")
print(f"WATCH: {len(watch)} coins, PnL=${watch['pnl'].sum():+.2f}")
print(f"DROP: {len(drop)} coins, PnL=${drop['pnl'].sum():+.2f}")
print(f"If DROP removed: ${keep['pnl'].sum() + watch['pnl'].sum():+.2f}")

# ---- Update missions.json ----
from research.missions import MissionEngine, _get_level

engine = MissionEngine()
mission_entry = {
    "mission_id": "mission_029_coin_triage",
    "date": "2026-04-07",
    "type": "portfolio_analysis",
    "title": "Coin Performance Triage -- 21-Day Paper Trading Per-Coin Deep Dive",
    "description": "วิเคราะห์ 46 เหรียญ 2,067 trades แบ่ง KEEP/WATCH/DROP ตาม PnL, WR, FLIP ratio, consistency, direction bias",
    "difficulty": "hard",
    "xp_reward": 100,
    "status": "completed",
    "target": "coin_selection",
    "started_at": datetime.utcnow().isoformat(),
    "finished_at": datetime.utcnow().isoformat(),
    "result": {
        "success": True,
        "verdict": "significant",
        "total_coins": len(df),
        "keep_coins": len(keep),
        "watch_coins": len(watch),
        "drop_coins": len(drop),
        "drop_list": list(drop["coin"].values),
        "keep_pnl": round(keep["pnl"].sum(), 2),
        "drop_pnl": round(drop["pnl"].sum(), 2),
        "pnl_improvement": round(-drop["pnl"].sum(), 2),
        "long_is_dead": True,
        "short_wr": round((overall_short["pnl_net"] > 0).mean() * 100, 1),
        "long_wr": round((overall_long["pnl_net"] > 0).mean() * 100, 1),
    },
    "insight": "46 เหรียญ 21 วัน: 13 KEEP (+$1,121), 23 WATCH (-$264), 10 DROP (-$520) ถอด 10 ตัวจะเพิ่ม PnL $520. SHORT WR=48.9% vs LONG WR=31.0% ยืนยัน LONG ตายจริง. Bottom 5 (OGN,SAHARA,CRV,JCT,LTC) WR<25% ทุกสัปดาห์ขาดทุน. ARIA outlier +$688 (71.9% SHORT WR)",
    "tags": ["portfolio", "coin_selection", "paper_trading", "triage", "direction_analysis"],
}

engine._data["missions"].append(mission_entry)
engine._data["meta"]["total_xp"] += mission_entry["xp_reward"]
engine._data["meta"]["current_streak"] += 1
engine._data["meta"]["longest_streak"] = max(
    engine._data["meta"]["longest_streak"],
    engine._data["meta"]["current_streak"],
)
engine._data["meta"]["last_mission_date"] = mission_entry["date"]
lvl, _ = _get_level(engine._data["meta"]["total_xp"])
engine._data["meta"]["level"] = lvl
engine._save()

print(f"\nmissions.json updated! XP={engine._data['meta']['total_xp']}, Streak={engine._data['meta']['current_streak']}")
