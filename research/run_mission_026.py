"""
Mission 026: Coin Selection Audit — Per-Coin & Per-Model Paper Trading Deep Dive
==================================================================================
สมมติฐาน: จาก 46 เหรียญที่เทรดอยู่ มีบางเหรียญที่เป็น "พิษ" ทำให้ portfolio เสีย
ถ้า drop เหรียญที่แย่ที่สุดออก จะเพิ่ม net PnL ได้

Experiments:
1. Per-model (v3/v5/v6) aggregate performance
2. Per-coin ranking — PnL, WR, FLIP rate, avg loss
3. Coin toxicity score — composite metric for worst performers
4. Direction analysis per model (LONG vs SHORT)
5. Time split: week 1 vs week 2 vs week 3 (improvement/decay?)
6. Recommendation: keep/drop/watch list
"""

import sys, json, sqlite3
import numpy as np, pandas as pd
from pathlib import Path
from datetime import datetime, timedelta

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from paper_trading.config import (
    COINS_V3, COINS_V5, COINS_V6, COIN_CONFIGS,
    _DEFAULT_CONFIG, _V5_DEFAULT_CONFIG, _V6_DEFAULT_CONFIG,
)

# ──────────────────────────────────────────────
# Load paper trades
# ──────────────────────────────────────────────
conn = sqlite3.connect(str(BASE_DIR / "paper_trading/state/paper_trades.db"))
trades = pd.read_sql("SELECT * FROM trades ORDER BY entry_time", conn)
signal_log = pd.read_sql("SELECT ts, coin, btc_score, signal, action, model FROM signal_log", conn)
conn.close()

trades["entry_time"] = pd.to_datetime(trades["entry_time"])
trades["exit_time"] = pd.to_datetime(trades["exit_time"])

# Map coin → model
coin_model = {}
for c in COINS_V3:
    coin_model[c] = "v3"
for c in COINS_V5:
    coin_model[c] = "v5"
for c in COINS_V6:
    coin_model[c] = "v6"

trades["model"] = trades["coin"].map(coin_model).fillna("unknown")

print(f"Total trades: {len(trades)}")
print(f"Date range: {trades['entry_time'].min()} to {trades['exit_time'].max()}")
print(f"Coins: {trades['coin'].nunique()}")
print(f"Models: {trades['model'].value_counts().to_dict()}")
print()

results = {}

# ══════════════════════════════════════════════════════════════
# EXP 1: Per-Model Aggregate Performance
# ══════════════════════════════════════════════════════════════
print("=" * 70)
print("EXP 1: Per-Model Aggregate Performance")
print("=" * 70)

model_stats = []
for model in ["v3", "v5", "v6"]:
    mt = trades[trades["model"] == model]
    if len(mt) == 0:
        continue
    n = len(mt)
    pnl = mt["pnl_net"].sum()
    wr = 100 * (mt["pnl_net"] > 0).mean()
    avg_pnl = mt["pnl_net"].mean()
    avg_win = mt.loc[mt["pnl_net"] > 0, "pnl_net"].mean() if (mt["pnl_net"] > 0).any() else 0
    avg_loss = mt.loc[mt["pnl_net"] <= 0, "pnl_net"].mean() if (mt["pnl_net"] <= 0).any() else 0
    flip_pct = 100 * (mt["exit_reason"] == "SIGNAL_FLIP").mean()
    tp_pct = 100 * (mt["exit_reason"].isin(["TP", "SL/TP"])).mean()
    trail_pct = 100 * (mt["exit_reason"] == "TRAIL").mean()
    sl_pct = 100 * (mt["exit_reason"] == "SL").mean()

    # SHORT vs LONG
    short_t = mt[mt["direction"] == -1]
    long_t = mt[mt["direction"] == 1]
    short_wr = 100 * (short_t["pnl_net"] > 0).mean() if len(short_t) > 0 else 0
    long_wr = 100 * (long_t["pnl_net"] > 0).mean() if len(long_t) > 0 else 0
    short_pnl = short_t["pnl_net"].sum() if len(short_t) > 0 else 0
    long_pnl = long_t["pnl_net"].sum() if len(long_t) > 0 else 0

    coins_in_model = mt["coin"].nunique()

    stat = {
        "model": model, "trades": n, "coins": coins_in_model,
        "pnl": round(pnl, 2), "wr": round(wr, 1), "avg_pnl": round(avg_pnl, 2),
        "avg_win": round(avg_win, 2), "avg_loss": round(avg_loss, 2),
        "flip_pct": round(flip_pct, 1), "tp_pct": round(tp_pct, 1),
        "trail_pct": round(trail_pct, 1), "sl_pct": round(sl_pct, 1),
        "short_wr": round(short_wr, 1), "long_wr": round(long_wr, 1),
        "short_pnl": round(short_pnl, 2), "long_pnl": round(long_pnl, 2),
    }
    model_stats.append(stat)
    print(f"\n{model}: {n} trades, {coins_in_model} coins")
    print(f"  PnL: ${pnl:.2f}, WR: {wr:.1f}%, Avg: ${avg_pnl:.2f}")
    print(f"  Avg Win: ${avg_win:.2f}, Avg Loss: ${avg_loss:.2f}")
    print(f"  FLIP: {flip_pct:.1f}%, TP: {tp_pct:.1f}%, TRAIL: {trail_pct:.1f}%, SL: {sl_pct:.1f}%")
    print(f"  SHORT: WR {short_wr:.1f}%, PnL ${short_pnl:.2f} | LONG: WR {long_wr:.1f}%, PnL ${long_pnl:.2f}")

results["exp1_model_stats"] = model_stats

# ══════════════════════════════════════════════════════════════
# EXP 2: Per-Coin Performance Ranking
# ══════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("EXP 2: Per-Coin Performance Ranking (sorted by PnL)")
print("=" * 70)

coin_stats = []
for coin in sorted(trades["coin"].unique()):
    ct = trades[trades["coin"] == coin]
    n = len(ct)
    pnl = ct["pnl_net"].sum()
    wr = 100 * (ct["pnl_net"] > 0).mean()
    avg_pnl = ct["pnl_net"].mean()
    flip_pct = 100 * (ct["exit_reason"] == "SIGNAL_FLIP").mean()
    flip_avg_loss = ct.loc[(ct["exit_reason"] == "SIGNAL_FLIP") & (ct["pnl_net"] <= 0), "pnl_net"].mean()
    flip_avg_loss = round(flip_avg_loss, 2) if pd.notna(flip_avg_loss) else 0
    tp_count = len(ct[ct["exit_reason"].isin(["TP", "SL/TP"])])
    trail_count = len(ct[ct["exit_reason"] == "TRAIL"])

    short_t = ct[ct["direction"] == -1]
    long_t = ct[ct["direction"] == 1]
    short_pnl = short_t["pnl_net"].sum() if len(short_t) > 0 else 0
    long_pnl = long_t["pnl_net"].sum() if len(long_t) > 0 else 0

    model = coin_model.get(coin, "unknown")

    coin_stats.append({
        "coin": coin, "model": model, "trades": n,
        "pnl": round(pnl, 2), "wr": round(wr, 1), "avg_pnl": round(avg_pnl, 2),
        "flip_pct": round(flip_pct, 1), "flip_avg_loss": flip_avg_loss,
        "tp_count": tp_count, "trail_count": trail_count,
        "short_pnl": round(short_pnl, 2), "long_pnl": round(long_pnl, 2),
    })

coin_df = pd.DataFrame(coin_stats).sort_values("pnl", ascending=False)
print("\nTop 15 coins:")
print(f"{'Coin':<12} {'Model':<5} {'Trades':>6} {'PnL':>9} {'WR%':>6} {'FLIP%':>6} {'FlipLoss':>9} {'SHORT$':>9} {'LONG$':>9}")
for _, r in coin_df.head(15).iterrows():
    print(f"{r['coin']:<12} {r['model']:<5} {r['trades']:>6} ${r['pnl']:>7.2f} {r['wr']:>5.1f}% {r['flip_pct']:>5.1f}% ${r['flip_avg_loss']:>7.2f} ${r['short_pnl']:>7.2f} ${r['long_pnl']:>7.2f}")

print("\nBottom 15 coins:")
for _, r in coin_df.tail(15).iterrows():
    print(f"{r['coin']:<12} {r['model']:<5} {r['trades']:>6} ${r['pnl']:>7.2f} {r['wr']:>5.1f}% {r['flip_pct']:>5.1f}% ${r['flip_avg_loss']:>7.2f} ${r['short_pnl']:>7.2f} ${r['long_pnl']:>7.2f}")

results["exp2_coin_ranking"] = coin_df.to_dict("records")

# ══════════════════════════════════════════════════════════════
# EXP 3: Coin Toxicity Score
# ══════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("EXP 3: Coin Toxicity Score")
print("=" * 70)

# Toxicity = combination of: negative PnL, high FLIP%, deep FLIP avg loss, low WR
# Normalize each to 0-1 then average
coin_df2 = coin_df.copy()
# Only score coins with >= 10 trades (need meaningful sample)
coin_df2 = coin_df2[coin_df2["trades"] >= 10].copy()

if len(coin_df2) > 0:
    # Higher = worse for each metric (normalize)
    coin_df2["pnl_score"] = 1 - (coin_df2["pnl"] - coin_df2["pnl"].min()) / (coin_df2["pnl"].max() - coin_df2["pnl"].min() + 1e-9)
    coin_df2["wr_score"] = 1 - (coin_df2["wr"] - coin_df2["wr"].min()) / (coin_df2["wr"].max() - coin_df2["wr"].min() + 1e-9)
    coin_df2["flip_score"] = (coin_df2["flip_pct"] - coin_df2["flip_pct"].min()) / (coin_df2["flip_pct"].max() - coin_df2["flip_pct"].min() + 1e-9)
    # flip_avg_loss: more negative = worse → invert
    coin_df2["flip_loss_score"] = 1 - (coin_df2["flip_avg_loss"] - coin_df2["flip_avg_loss"].min()) / (coin_df2["flip_avg_loss"].max() - coin_df2["flip_avg_loss"].min() + 1e-9)

    coin_df2["toxicity"] = (coin_df2["pnl_score"] + coin_df2["wr_score"] + coin_df2["flip_score"] + coin_df2["flip_loss_score"]) / 4
    coin_df2 = coin_df2.sort_values("toxicity", ascending=False)

    print("\nMost TOXIC coins (≥10 trades):")
    print(f"{'Coin':<12} {'Model':<5} {'Trades':>6} {'PnL':>9} {'WR%':>6} {'FLIP%':>6} {'Toxicity':>9}")
    for _, r in coin_df2.head(10).iterrows():
        print(f"{r['coin']:<12} {r['model']:<5} {r['trades']:>6} ${r['pnl']:>7.2f} {r['wr']:>5.1f}% {r['flip_pct']:>5.1f}% {r['toxicity']:>8.3f}")

    print("\nLeast TOXIC (best) coins:")
    for _, r in coin_df2.tail(10).iterrows():
        print(f"{r['coin']:<12} {r['model']:<5} {r['trades']:>6} ${r['pnl']:>7.2f} {r['wr']:>5.1f}% {r['flip_pct']:>5.1f}% {r['toxicity']:>8.3f}")

    results["exp3_toxic_top10"] = coin_df2.head(10)[["coin", "model", "trades", "pnl", "wr", "flip_pct", "toxicity"]].to_dict("records")
    results["exp3_healthy_top10"] = coin_df2.tail(10)[["coin", "model", "trades", "pnl", "wr", "flip_pct", "toxicity"]].to_dict("records")

# ══════════════════════════════════════════════════════════════
# EXP 4: Direction Analysis Per Model
# ══════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("EXP 4: Direction Analysis Per Model")
print("=" * 70)

dir_stats = []
for model in ["v3", "v5", "v6"]:
    mt = trades[trades["model"] == model]
    for d_val, d_name in [(1, "LONG"), (-1, "SHORT")]:
        dt = mt[mt["direction"] == d_val]
        if len(dt) == 0:
            continue
        n = len(dt)
        pnl = dt["pnl_net"].sum()
        wr = 100 * (dt["pnl_net"] > 0).mean()
        flip_pct = 100 * (dt["exit_reason"] == "SIGNAL_FLIP").mean()
        avg_pnl = dt["pnl_net"].mean()

        stat = {"model": model, "dir": d_name, "trades": n,
                "pnl": round(pnl, 2), "wr": round(wr, 1),
                "flip_pct": round(flip_pct, 1), "avg_pnl": round(avg_pnl, 2)}
        dir_stats.append(stat)
        print(f"  {model} {d_name}: {n} trades, PnL=${pnl:.2f}, WR={wr:.1f}%, FLIP={flip_pct:.1f}%")

results["exp4_direction"] = dir_stats

# ══════════════════════════════════════════════════════════════
# EXP 5: Time Split — Week-by-Week Evolution
# ══════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("EXP 5: Time Split — Week-by-Week Evolution")
print("=" * 70)

trades["week"] = trades["entry_time"].dt.isocalendar().week.astype(int)
week_stats = []
for wk in sorted(trades["week"].unique()):
    wt = trades[trades["week"] == wk]
    n = len(wt)
    pnl = wt["pnl_net"].sum()
    wr = 100 * (wt["pnl_net"] > 0).mean()
    flip_pct = 100 * (wt["exit_reason"] == "SIGNAL_FLIP").mean()
    date_range = f"{wt['entry_time'].min().strftime('%m-%d')} to {wt['entry_time'].max().strftime('%m-%d')}"

    stat = {"week": int(wk), "date_range": date_range, "trades": n,
            "pnl": round(pnl, 2), "wr": round(wr, 1), "flip_pct": round(flip_pct, 1)}
    week_stats.append(stat)
    print(f"  Week {wk} ({date_range}): {n} trades, PnL=${pnl:.2f}, WR={wr:.1f}%, FLIP={flip_pct:.1f}%")

    # Per-model within week
    for model in ["v3", "v5", "v6"]:
        mt = wt[wt["model"] == model]
        if len(mt) > 0:
            print(f"    {model}: {len(mt)} trades, PnL=${mt['pnl_net'].sum():.2f}, WR={100*(mt['pnl_net']>0).mean():.1f}%")

results["exp5_weekly"] = week_stats

# ══════════════════════════════════════════════════════════════
# EXP 6: Drop Simulation — What if we drop toxic coins?
# ══════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("EXP 6: Drop Simulation — Impact of removing toxic coins")
print("=" * 70)

baseline_pnl = trades["pnl_net"].sum()
baseline_trades = len(trades)
print(f"Baseline: {baseline_trades} trades, PnL=${baseline_pnl:.2f}")

# Find coins with negative PnL and >= 10 trades
neg_coins = coin_df[(coin_df["pnl"] < 0) & (coin_df["trades"] >= 10)].sort_values("pnl")
print(f"\nNegative PnL coins (≥10 trades): {len(neg_coins)}")

drop_simulations = []
# Simulate dropping bottom N toxic coins
if len(coin_df2) > 0:
    toxic_coins_sorted = coin_df2.sort_values("toxicity", ascending=False)
    for n_drop in [3, 5, 8, 10]:
        drop_list = toxic_coins_sorted.head(n_drop)["coin"].tolist()
        remaining = trades[~trades["coin"].isin(drop_list)]
        new_pnl = remaining["pnl_net"].sum()
        new_trades = len(remaining)
        delta = new_pnl - baseline_pnl
        new_wr = 100 * (remaining["pnl_net"] > 0).mean()

        sim = {"n_drop": n_drop, "dropped": drop_list,
               "remaining_trades": new_trades, "new_pnl": round(new_pnl, 2),
               "delta": round(delta, 2), "new_wr": round(new_wr, 1)}
        drop_simulations.append(sim)
        print(f"\n  Drop {n_drop} most toxic: {drop_list}")
        print(f"    → {new_trades} trades, PnL=${new_pnl:.2f} (Δ${delta:+.2f}), WR={new_wr:.1f}%")

results["exp6_drop_sims"] = drop_simulations

# ══════════════════════════════════════════════════════════════
# EXP 7: Exit Reason Distribution Per Model
# ══════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("EXP 7: Exit Reason Distribution Per Model")
print("=" * 70)

exit_stats = []
for model in ["v3", "v5", "v6"]:
    mt = trades[trades["model"] == model]
    if len(mt) == 0:
        continue
    print(f"\n  {model} ({len(mt)} trades):")
    for reason in ["SIGNAL_FLIP", "TP", "SL/TP", "TRAIL", "SL", "TIMEOUT"]:
        rt = mt[mt["exit_reason"] == reason]
        if len(rt) > 0:
            pct = 100 * len(rt) / len(mt)
            pnl = rt["pnl_net"].sum()
            wr = 100 * (rt["pnl_net"] > 0).mean()
            print(f"    {reason:<14}: {len(rt):>4} ({pct:>5.1f}%), PnL=${pnl:>8.2f}, WR={wr:.1f}%")
            exit_stats.append({"model": model, "reason": reason, "count": len(rt),
                              "pct": round(pct, 1), "pnl": round(pnl, 2), "wr": round(wr, 1)})

results["exp7_exit_per_model"] = exit_stats

# ══════════════════════════════════════════════════════════════
# Summary & Recommendations
# ══════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("SUMMARY & RECOMMENDATIONS")
print("=" * 70)

# Best model
best_model = max(model_stats, key=lambda x: x["pnl"])
worst_model = min(model_stats, key=lambda x: x["pnl"])
print(f"\nBest model: {best_model['model']} (PnL=${best_model['pnl']:.2f}, WR={best_model['wr']:.1f}%)")
print(f"Worst model: {worst_model['model']} (PnL=${worst_model['pnl']:.2f}, WR={worst_model['wr']:.1f}%)")

# Profitable vs unprofitable coins
profitable = coin_df[coin_df["pnl"] > 0]
unprofitable = coin_df[coin_df["pnl"] <= 0]
print(f"\nProfitable coins: {len(profitable)}/{len(coin_df)}")
print(f"Unprofitable coins: {len(unprofitable)}/{len(coin_df)}")

# Model breakdown of unprofitable
for model in ["v3", "v5", "v6"]:
    unp_model = unprofitable[unprofitable["model"] == model]
    tot_model = coin_df[coin_df["model"] == model]
    print(f"  {model}: {len(unp_model)}/{len(tot_model)} coins unprofitable (PnL: ${unp_model['pnl'].sum():.2f})")

results["summary"] = {
    "total_trades": int(len(trades)),
    "total_pnl": round(float(trades["pnl_net"].sum()), 2),
    "total_coins": int(trades["coin"].nunique()),
    "profitable_coins": int(len(profitable)),
    "unprofitable_coins": int(len(unprofitable)),
    "best_model": best_model["model"],
    "best_model_pnl": best_model["pnl"],
    "worst_model": worst_model["model"],
    "worst_model_pnl": worst_model["pnl"],
    "best_coin": coin_df.iloc[0]["coin"],
    "best_coin_pnl": float(coin_df.iloc[0]["pnl"]),
    "worst_coin": coin_df.iloc[-1]["coin"],
    "worst_coin_pnl": float(coin_df.iloc[-1]["pnl"]),
}

# ══════════════════════════════════════════════════════════════
# Save results
# ══════════════════════════════════════════════════════════════
# JSON
json_path = BASE_DIR / "missions" / "mission_026_coin_selection_audit.json"
with open(json_path, "w", encoding="utf-8") as f:
    json.dump(results, f, indent=2, ensure_ascii=False, default=str)
print(f"\nSaved: {json_path}")

# ── Update missions.json ──
from research.missions import MissionEngine, _get_level

engine = MissionEngine()
mission_entry = {
    "mission_id": "mission_026_coin_selection_audit",
    "date": datetime.utcnow().strftime("%Y-%m-%d"),
    "type": "coin_selection",
    "title": "Coin Selection Audit — Per-Coin & Per-Model Paper Trading Deep Dive",
    "description": "วิเคราะห์ 46 เหรียญ x 3 โมเดล (v3/v5/v6) จาก paper trading 18 วัน เพื่อหาเหรียญที่ควร drop/keep",
    "difficulty": "hard",
    "xp_reward": 100,
    "status": "completed",
    "target": "coin_selection",
    "started_at": datetime.utcnow().isoformat(),
    "finished_at": datetime.utcnow().isoformat(),
    "result": results["summary"],
    "insight": "PLACEHOLDER",
    "tags": ["coin_selection", "paper_trading", "v3_v5_v6", "toxicity", "drop_simulation"],
}
engine._data["missions"].append(mission_entry)
engine._data["meta"]["total_xp"] += mission_entry["xp_reward"]
engine._data["meta"]["current_streak"] += 1
engine._data["meta"]["longest_streak"] = max(
    engine._data["meta"]["longest_streak"],
    engine._data["meta"]["current_streak"])
engine._data["meta"]["last_mission_date"] = mission_entry["date"]
lvl, _ = _get_level(engine._data["meta"]["total_xp"])
engine._data["meta"]["level"] = lvl
# Don't save yet — will update insight after analyzing results

print("\n✓ Mission 026 complete!")
print(f"  XP: {engine._data['meta']['total_xp']} (Level {lvl})")
print(f"  Streak: {engine._data['meta']['current_streak']}")
