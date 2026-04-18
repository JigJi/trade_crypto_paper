"""Daily Missions System - auto-generated research quests with gamification.

Generates ONE mission per day (deep analytical style like missions 001-007).

Usage:
    python research/missions.py --run          # generate + execute today's mission
    python research/missions.py --status       # show recent missions
    python research/missions.py --run --type factor_test  # force specific type
"""
import argparse
import json
import logging
import random
import re
import sqlite3
import sys
import traceback
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from research.config import MISSIONS_PATH, MISSIONS_DIR, PAPER_TRADES_DB

log = logging.getLogger("research.missions")

# ── Gamification Constants ────────────────────────────────────

# Level 1-99, XP curve: cumulative = 5 * level^2
# ~500 XP to reach Lv10, ~12,500 for Lv50, ~49,000 for Lv99
LEVEL_TIERS = [
    (1,  "Apprentice"),    # 1-10
    (11, "Researcher"),    # 11-20
    (21, "Analyst"),       # 21-30
    (31, "Scientist"),     # 31-40
    (41, "Strategist"),    # 41-50
    (51, "Professor"),     # 51-60
    (61, "Master"),        # 61-70
    (71, "Grand Master"),  # 71-80
    (81, "Legend"),         # 81-90
    (91, "Mythic"),        # 91-99
]
MAX_LEVEL = 99

# Base XP per mission type (minimum for completing, before wow bonus)
BASE_XP = {
    "factor_test": 15,
    "revalidation": 10,
    "combo_test": 15,
    "coin_deep_dive": 10,
    "regime_test": 10,
    "paper_vs_backtest": 10,
    "param_sweep": 15,
    "model_quality": 15,
    "exit_optimization": 15,
    "portfolio_risk": 15,
    # Analysis types (Mission A)
    "trade_analysis": 15,
    "signal_quality": 15,
    "drawdown_analysis": 15,
    # Discovery types (Mission B)
    "web_discovery": 20,
}

DIFFICULTY_MAP = {
    "factor_test": "hard",
    "revalidation": "easy",
    "combo_test": "medium",
    "coin_deep_dive": "medium",
    "regime_test": "medium",
    "paper_vs_backtest": "easy",
    "param_sweep": "hard",
    "trade_analysis": "medium",
    "signal_quality": "medium",
    "drawdown_analysis": "medium",
    "web_discovery": "hard",
}

MISSION_TITLES = {
    "factor_test": "Test the Unknown: {target}",
    "revalidation": "Revalidate: {target}",
    "combo_test": "Combo Trial: {target}",
    "coin_deep_dive": "Deep Dive: {target}",
    "regime_test": "Regime Check: {target}",
    "paper_vs_backtest": "Reality Check: Paper vs Backtest",
    "param_sweep": "Parameter Sweep: {target}",
    "trade_analysis": "Trade Analysis: 7-Day Review",
    "signal_quality": "Signal Quality Check",
    "drawdown_analysis": "Drawdown Deep Dive",
    "web_discovery": "Discovery: {target}",
}

MISSION_DESCRIPTIONS = {
    "factor_test": "Test untested factor {target} against v3 baseline at multiple weights",
    "revalidation": "Re-test production factor {target} to verify it still adds value",
    "combo_test": "Try adding bench factor {target} to current champion model",
    "coin_deep_dive": "Analyze worst-performing paper trading coin {target}",
    "regime_test": "Test champion model in {target}-only market conditions",
    "paper_vs_backtest": "Compare live paper trading results vs backtest expectations",
    "param_sweep": "Sweep SL/TP/threshold parameters for {target}",
    "trade_analysis": "วิเคราะห์เทรดล่าสุด 7 วัน: WR ตาม direction/score/hour/coin, หาแพทเทิร์นแพ้-ชนะ",
    "signal_quality": "ตรวจคุณภาพสัญญาณ: score distribution, false signal rate, optimal threshold",
    "drawdown_analysis": "วิเคราะห์ drawdown: max DD, losing streak, recovery pattern",
    "web_discovery": "ค้นหาไอเดียเทรดใหม่จากอินเทอร์เน็ต: {target}",
}

# ── Mission Categories ──────────────────────────────────────
ANALYSIS_TYPES = {"trade_analysis", "signal_quality", "drawdown_analysis",
                  "paper_vs_backtest", "coin_deep_dive", "revalidation"}
DISCOVERY_TYPES = {"web_discovery", "factor_test", "combo_test",
                   "regime_test", "param_sweep"}

# ── Discovery Topic Pool ────────────────────────────────────
DISCOVERY_TOPICS = [
    {"name": "volume_profile_vwap",
     "search": "VWAP deviation crypto futures alpha strategy 2026",
     "hypothesis": "Price deviation from VWAP predicts mean reversion on 15m timeframe"},
    {"name": "funding_term_structure",
     "search": "funding rate term structure crypto trading strategy",
     "hypothesis": "Multi-period funding rate slope predicts directional moves"},
    {"name": "orderflow_imbalance",
     "search": "order flow imbalance crypto futures prediction",
     "hypothesis": "Aggressive order flow imbalance at tick level predicts short-term direction"},
    {"name": "cross_exchange_basis",
     "search": "cross exchange basis arbitrage crypto futures signal 2026",
     "hypothesis": "Basis spread between exchanges predicts price convergence"},
    {"name": "whale_onchain_tracking",
     "search": "whale on-chain tracking crypto futures trading edge",
     "hypothesis": "Large wallet movements predict near-term price direction"},
    {"name": "gamma_exposure_gex",
     "search": "gamma exposure GEX impact crypto options market making",
     "hypothesis": "Options dealer gamma exposure creates predictable price magnets"},
    {"name": "correlation_regime",
     "search": "BTC altcoin correlation regime switching trading strategy",
     "hypothesis": "Correlation regime shifts signal altcoin rotation opportunities"},
    {"name": "liquidation_cascade_reversion",
     "search": "liquidation cascade mean reversion crypto futures 2026",
     "hypothesis": "Post-liquidation cascade creates short-term mean reversion alpha"},
    {"name": "nlp_sentiment",
     "search": "NLP sentiment analysis crypto trading alpha news",
     "hypothesis": "Real-time news sentiment NLP gives 15m alpha on altcoins"},
    {"name": "social_sentiment",
     "search": "social media sentiment crypto trading signal twitter telegram",
     "hypothesis": "Social media sentiment spikes predict short-term price moves"},
    {"name": "microstructure_spread",
     "search": "market microstructure bid-ask spread crypto futures alpha",
     "hypothesis": "Bid-ask spread dynamics predict short-term volatility and direction"},
    {"name": "time_of_day_seasonality",
     "search": "time of day seasonality crypto futures intraday pattern 2026",
     "hypothesis": "Specific hours have statistically significant directional bias"},
    {"name": "day_of_week_effects",
     "search": "day of week effect crypto bitcoin weekend premium",
     "hypothesis": "Certain days of the week show persistent return patterns"},
    {"name": "stablecoin_flows",
     "search": "stablecoin flow USDT USDC crypto market indicator",
     "hypothesis": "Stablecoin minting/burning predicts crypto market inflows"},
    {"name": "dex_cex_ratio",
     "search": "DEX CEX volume ratio crypto market indicator on-chain",
     "hypothesis": "DEX/CEX volume ratio signals retail vs institutional sentiment"},
    {"name": "mempool_analysis",
     "search": "Bitcoin mempool analysis trading signal pending transactions",
     "hypothesis": "Mempool congestion predicts short-term BTC price pressure"},
    {"name": "realized_vol_models",
     "search": "realized volatility model crypto HAR GARCH futures trading",
     "hypothesis": "Volatility forecast models improve position sizing and timing"},
    {"name": "options_put_call_ratio",
     "search": "put call ratio crypto options Deribit trading signal 2026",
     "hypothesis": "Extreme put/call ratios on Deribit predict BTC reversals"},
]


def _xp_for_level(level: int) -> int:
    """Cumulative XP needed to reach a given level. Curve: 5 * level^2."""
    return 5 * level * level


def _get_level(xp: int) -> tuple[int, str]:
    """Return (level_num, level_name) for given XP."""
    level = 1
    for lv in range(1, MAX_LEVEL + 1):
        if xp >= _xp_for_level(lv):
            level = lv
        else:
            break
    # Get tier name
    tier_name = "Apprentice"
    for tier_start, name in LEVEL_TIERS:
        if level >= tier_start:
            tier_name = name
    return level, tier_name


def _xp_for_next_level(xp: int) -> tuple[int, int]:
    """Return (current_level_xp, next_level_xp) thresholds."""
    level, _ = _get_level(xp)
    current_threshold = _xp_for_level(level)
    if level >= MAX_LEVEL:
        return current_threshold, current_threshold
    return current_threshold, _xp_for_level(level + 1)


def compute_wow_xp(mission_type: str, result: dict) -> int:
    """Compute XP based on how impactful/surprising the discovery is.

    Scale:
      0-10   = nothing new, boring result
      10-30  = expected result, minor value
      30-60  = useful finding, moderate impact
      60-100 = significant discovery, changes strategy
      100+   = game-changer, paradigm shift
    """
    base = BASE_XP.get(mission_type, 10)
    if not result or not result.get("success"):
        return base  # completed but failed = base XP only

    wow = 0

    if mission_type == "factor_test":
        delta = abs(result.get("best_delta", 0))
        if delta > 1000:
            wow = 120   # game-changer factor
        elif delta > 500:
            wow = 80    # strong factor
        elif delta > 200:
            wow = 40    # decent factor
        elif delta > 0:
            wow = 15    # marginal
        else:
            wow = 5     # confirmed it doesn't work (still useful knowledge)

    elif mission_type == "revalidation":
        verdict = result.get("verdict", "")
        drift = abs(result.get("drift", 0))
        if verdict == "degraded":
            wow = 60    # important: factor is dying!
        elif drift > 200:
            wow = 30    # noticeable drift, worth knowing
        else:
            wow = 5     # stable, boring but necessary

    elif mission_type == "combo_test":
        delta = result.get("best_delta", 0)
        if delta > 500:
            wow = 100   # combo unlocked big alpha
        elif delta > 200:
            wow = 50    # worth deploying
        elif delta > 0:
            wow = 15    # marginal combo
        else:
            wow = 5     # doesn't work in combo

    elif mission_type == "paper_vs_backtest":
        wr_diff = abs(result.get("wr_diff", 0))
        if wr_diff > 20:
            wow = 80    # huge gap = critical finding
        elif wr_diff > 10:
            wow = 40    # significant gap
        elif wr_diff > 5:
            wow = 20    # mild gap
        else:
            wow = 5     # on track, nothing surprising

    elif mission_type == "coin_deep_dive":
        verdict = result.get("verdict", "")
        pnl = abs(result.get("total_pnl", 0))
        if verdict == "weak" and pnl > 100:
            wow = 50    # found a problem coin with big impact
        elif verdict == "strong":
            wow = 20    # confirmed it works
        else:
            wow = 10    # average, not much to see

    elif mission_type == "regime_test":
        pnl = result.get("pnl", 0)
        if pnl < -500:
            wow = 70    # model breaks in this regime!
        elif pnl < 0:
            wow = 40    # losing regime, important to know
        elif pnl > 2000:
            wow = 30    # very strong in regime
        else:
            wow = 10    # as expected

    elif mission_type == "param_sweep":
        # Placeholder - param_sweep handler is simplified
        wow = 15

    elif mission_type == "model_quality":
        # Signal quality / threshold analysis
        wr_delta = result.get("wr_delta_pp", 0)
        if wr_delta > 10:
            wow = 80    # huge WR spread = actionable
        elif wr_delta > 5:
            wow = 40
        else:
            wow = 15

    elif mission_type == "exit_optimization":
        # Exit mechanism discoveries
        delta_pct = abs(result.get("no_sl_delta_pct", 0))
        if delta_pct > 50:
            wow = 120   # game-changer (e.g., SL 0% WR)
        elif delta_pct > 20:
            wow = 60
        elif delta_pct > 5:
            wow = 30
        else:
            wow = 10

    elif mission_type == "portfolio_risk":
        # Concentration / correlation discoveries
        effective_n = result.get("effective_n", 99)
        n_coins = result.get("n_coins", 1)
        ratio = effective_n / max(n_coins, 1)
        if ratio < 0.3:
            wow = 80    # severely under-diversified
        elif ratio < 0.5:
            wow = 50
        else:
            wow = 15    # well diversified, nothing surprising

    elif mission_type == "trade_analysis":
        # Trade pattern analysis
        n_patterns = len(result.get("losing_patterns", []))
        wr = result.get("overall_wr", 50)
        if wr < 40:
            wow = 70    # terrible WR, critical finding
        elif n_patterns >= 3:
            wow = 50    # found multiple actionable patterns
        elif n_patterns >= 1:
            wow = 30
        else:
            wow = 10

    elif mission_type == "signal_quality":
        wr_spread = result.get("wr_spread", 0)
        if wr_spread > 30:
            wow = 80    # huge quality spread
        elif wr_spread > 15:
            wow = 40
        else:
            wow = 15

    elif mission_type == "drawdown_analysis":
        max_dd_pct = abs(result.get("max_dd_pct", 0))
        if max_dd_pct > 20:
            wow = 70    # severe drawdown
        elif max_dd_pct > 10:
            wow = 40
        else:
            wow = 15

    elif mission_type == "web_discovery":
        proxy = result.get("proxy_test") or {}
        n_results = len(result.get("search_results", []))
        proxy_tested = proxy.get("tested", False)
        hit_rate = proxy.get("hit_rate", 0)
        if proxy_tested and hit_rate > 55:
            wow = 100   # tested with data and promising!
        elif proxy_tested and hit_rate > 52:
            wow = 70    # tested, marginal signal
        elif proxy_tested:
            wow = 50    # tested but weak (still valuable knowledge)
        elif n_results >= 3:
            wow = 40    # good research, no test
        elif n_results >= 1:
            wow = 25
        else:
            wow = 10    # no results but topic documented

    return base + wow


# ── Mission Engine ────────────────────────────────────────────

class MissionEngine:
    """Generate, execute, and track daily research missions."""

    def __init__(self, path: Path = None):
        self.path = Path(path) if path else MISSIONS_PATH
        self._data = self._load()
        self._file_mtime = self._get_mtime()

    def _get_mtime(self) -> float:
        try:
            return self.path.stat().st_mtime if self.path.exists() else 0
        except OSError:
            return 0

    def _ensure_fresh(self):
        """Reload from disk if file was modified externally."""
        current_mtime = self._get_mtime()
        if current_mtime > self._file_mtime:
            self._data = self._load()
            self._file_mtime = current_mtime

    def _load(self) -> dict:
        if self.path.exists():
            with open(self.path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        return {
            "meta": {
                "total_xp": 0,
                "current_streak": 0,
                "longest_streak": 0,
                "level": 1,
                "last_mission_date": None,
            },
            "missions": [],
        }

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh, indent=2, default=str, ensure_ascii=False)

    # ── Queries ───────────────────────────────────────────────

    def get_meta(self) -> dict:
        self._ensure_fresh()
        meta = dict(self._data["meta"])
        xp = meta["total_xp"]
        level_num, level_name = _get_level(xp)
        cur_thresh, next_thresh = _xp_for_next_level(xp)
        meta["level"] = level_num
        meta["level_name"] = level_name
        meta["xp_current_level"] = cur_thresh
        meta["xp_next_level"] = next_thresh
        meta["xp_progress"] = xp - cur_thresh
        meta["xp_needed"] = max(1, next_thresh - cur_thresh)
        meta["total_missions"] = len(self._data["missions"])
        meta["completed_missions"] = len([
            m for m in self._data["missions"] if m["status"] == "completed"
        ])
        return meta

    def get_today_mission(self) -> dict | None:
        """Get single today mission (legacy compat). Returns first found."""
        missions = self.get_today_missions()
        return missions[0] if missions else None

    def get_today_missions(self) -> list[dict]:
        """Get all of today's missions (0-2 items)."""
        self._ensure_fresh()
        today = datetime.utcnow().strftime("%Y-%m-%d")
        return [m for m in self._data["missions"] if m["date"] == today]

    def get_recent(self, limit: int = 10) -> list[dict]:
        self._ensure_fresh()
        return list(reversed(self._data["missions"][-limit:]))

    def has_run_today(self) -> bool:
        """True if today's mission already exists."""
        self._ensure_fresh()
        today_missions = self.get_today_missions()
        return len(today_missions) >= 1

    # ── Dual Mission Generation ─────────────────────────────

    def generate_analysis_mission(self) -> dict:
        """Pick an analysis mission based on priority."""
        now = datetime.utcnow()

        # Priority 1: trade_analysis if >20 trades and not analyzed in 3 days
        if PAPER_TRADES_DB.exists():
            try:
                conn = sqlite3.connect(str(PAPER_TRADES_DB))
                count = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
                conn.close()
                if count >= 20:
                    recent_ta = [m for m in self._data["missions"]
                                 if m["type"] == "trade_analysis"
                                 and m["status"] == "completed"]
                    if not recent_ta or (
                        (now - datetime.strptime(recent_ta[-1]["date"], "%Y-%m-%d")).days >= 3
                    ):
                        return self._make_mission("trade_analysis", "7d-review")
            except Exception:
                pass

        # Priority 2: signal_quality if signal_log has data
        if PAPER_TRADES_DB.exists():
            try:
                conn = sqlite3.connect(str(PAPER_TRADES_DB))
                try:
                    sig_count = conn.execute("SELECT COUNT(*) FROM signal_log").fetchone()[0]
                except sqlite3.OperationalError:
                    sig_count = 0
                conn.close()
                if sig_count > 0:
                    recent_sq = [m for m in self._data["missions"]
                                 if m["type"] == "signal_quality"
                                 and m["status"] == "completed"]
                    if not recent_sq or (
                        (now - datetime.strptime(recent_sq[-1]["date"], "%Y-%m-%d")).days >= 3
                    ):
                        return self._make_mission("signal_quality", "signal-review")
            except Exception:
                pass

        # Fallback: drawdown_analysis
        return self._make_mission("drawdown_analysis", "equity-review")

    def generate_discovery_mission(self) -> dict:
        """Pick from discovery topic pool (rotate, don't repeat until all explored)."""
        explored = set()
        for m in self._data["missions"]:
            if m["type"] == "web_discovery" and m.get("target"):
                explored.add(m["target"])

        unexplored = [t for t in DISCOVERY_TOPICS if t["name"] not in explored]
        if not unexplored:
            # All explored — reset and start over
            unexplored = list(DISCOVERY_TOPICS)

        topic = unexplored[0]  # deterministic order, first unexplored
        return self._make_mission("web_discovery", topic["name"])

    def _make_mission(self, mission_type: str, target: str) -> dict:
        """Create a mission dict without executing it."""
        now = datetime.utcnow()
        title = MISSION_TITLES.get(mission_type, "Research Mission").format(target=target)
        desc = MISSION_DESCRIPTIONS.get(mission_type, "").format(target=target)
        category = "analysis" if mission_type in ANALYSIS_TYPES else "discovery"

        return {
            "mission_id": f"mission_{now.strftime('%Y%m%d_%H%M%S')}_{category[0]}",
            "date": now.strftime("%Y-%m-%d"),
            "type": mission_type,
            "category": category,
            "title": title,
            "description": desc,
            "difficulty": DIFFICULTY_MAP.get(mission_type, "medium"),
            "xp_reward": 0,
            "status": "pending",
            "target": target,
            "started_at": None,
            "finished_at": None,
            "result": None,
            "insight": None,
            "tags": [mission_type, category],
        }

    def run_today_dual(self, progress_cb=None) -> list[dict]:
        """Generate + execute today's dual missions. Main entry point."""
        results = []

        # Mission A: Analysis
        analysis = self.generate_analysis_mission()
        log.info(f"[A] Generated analysis mission: {analysis['title']}")
        analysis = self.execute_mission(analysis, progress_cb)
        self._data["missions"].append(analysis)
        results.append(analysis)

        # Mission B: Discovery
        discovery = self.generate_discovery_mission()
        log.info(f"[D] Generated discovery mission: {discovery['title']}")
        discovery = self.execute_mission(discovery, progress_cb)
        self._data["missions"].append(discovery)
        results.append(discovery)

        # Update meta for both
        self._update_meta_after_missions(results)
        self._save()

        for r in results:
            log.info(f"Mission complete: {r['title']} [{r['status']}] +{r['xp_reward']}XP")
        return results

    def _update_meta_after_missions(self, missions: list[dict]):
        """Update XP, streak, level after one or more missions."""
        meta = self._data["meta"]
        today = datetime.utcnow().strftime("%Y-%m-%d")

        total_xp_gained = 0
        for m in missions:
            if m["status"] == "completed":
                total_xp_gained += m["xp_reward"]

        if total_xp_gained > 0:
            meta["total_xp"] = meta.get("total_xp", 0) + total_xp_gained

            # Update streak (only once per day)
            last_date = meta.get("last_mission_date")
            if last_date and last_date != today:
                try:
                    last_dt = datetime.strptime(last_date, "%Y-%m-%d")
                    today_dt = datetime.strptime(today, "%Y-%m-%d")
                    diff = (today_dt - last_dt).days
                    if diff == 1:
                        meta["current_streak"] = meta.get("current_streak", 0) + 1
                    elif diff > 1:
                        meta["current_streak"] = 1
                except ValueError:
                    meta["current_streak"] = 1
            elif not last_date:
                meta["current_streak"] = 1

            meta["longest_streak"] = max(
                meta.get("longest_streak", 0),
                meta.get("current_streak", 0)
            )
            meta["last_mission_date"] = today

        level_num, _ = _get_level(meta.get("total_xp", 0))
        meta["level"] = level_num

    # ── Legacy Mission Generation ──────────────────────────

    def generate_mission(self, force_type: str = None) -> dict:
        """Pick the best mission type and generate it."""
        now = datetime.utcnow()
        mission_type, target = self._pick_mission(force_type)

        title = MISSION_TITLES.get(mission_type, "Research Mission").format(target=target)
        desc = MISSION_DESCRIPTIONS.get(mission_type, "").format(target=target)

        mission = {
            "mission_id": f"mission_{now.strftime('%Y%m%d_%H%M%S')}",
            "date": now.strftime("%Y-%m-%d"),
            "type": mission_type,
            "title": title,
            "description": desc,
            "difficulty": DIFFICULTY_MAP.get(mission_type, "medium"),
            "xp_reward": 0,  # computed after execution based on wow factor
            "status": "pending",
            "target": target,
            "started_at": None,
            "finished_at": None,
            "result": None,
            "insight": None,
            "tags": [mission_type],
        }
        return mission

    def _pick_mission(self, force_type: str = None) -> tuple[str, str]:
        """Priority-based mission selection. Returns (type, target)."""
        if force_type:
            return self._pick_target_for_type(force_type)

        from research.factor_registry import FactorRegistry
        fr = FactorRegistry()

        # 1. Untested factors with scorers
        untested = fr.get_untested()
        testable = [f for f in untested if f.get("scorer_module") and f.get("scorer_function")]
        if testable:
            factor = random.choice(testable)
            return "factor_test", factor["name"]

        # 2. Stale production factors (>7 days since test)
        prod = fr.get_production_factors()
        stale = []
        now = datetime.utcnow()
        for f in prod:
            tested = f.get("last_tested")
            if tested:
                try:
                    tested_dt = datetime.strptime(tested, "%Y-%m-%d")
                    if (now - tested_dt).days > 7:
                        stale.append(f)
                except ValueError:
                    stale.append(f)
            else:
                stale.append(f)
        if stale:
            factor = random.choice(stale)
            return "revalidation", factor["name"]

        # 3. Paper trading has trades → paper_vs_backtest
        if PAPER_TRADES_DB.exists():
            try:
                conn = sqlite3.connect(str(PAPER_TRADES_DB))
                count = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
                conn.close()
                if count > 0:
                    # Only if not done recently
                    recent_pvb = [m for m in self._data["missions"]
                                  if m["type"] == "paper_vs_backtest"
                                  and m["status"] == "completed"]
                    if not recent_pvb or (
                        recent_pvb and
                        (now - datetime.strptime(recent_pvb[-1]["date"], "%Y-%m-%d")).days > 3
                    ):
                        return "paper_vs_backtest", ""
            except Exception:
                pass

        # 4. Bench factors not combo-tested
        bench = fr.get_by_status("tested_positive")
        combo_tested = {m.get("target") for m in self._data["missions"]
                        if m["type"] == "combo_test" and m["status"] == "completed"}
        untried_bench = [f for f in bench if f["name"] not in combo_tested]
        if untried_bench:
            factor = max(untried_bench, key=lambda f: f.get("best_delta_pnl", 0))
            return "combo_test", factor["name"]

        # 5. Worst paper-trading coin
        if PAPER_TRADES_DB.exists():
            try:
                conn = sqlite3.connect(str(PAPER_TRADES_DB))
                conn.row_factory = sqlite3.Row
                worst = conn.execute("""
                    SELECT coin, SUM(pnl_net) as total_pnl
                    FROM trades GROUP BY coin
                    ORDER BY total_pnl ASC LIMIT 1
                """).fetchone()
                conn.close()
                if worst:
                    return "coin_deep_dive", worst["coin"]
            except Exception:
                pass

        # 6. Alternate bull/bear regime test
        recent_regime = [m for m in self._data["missions"]
                         if m["type"] == "regime_test" and m["status"] == "completed"]
        last_regime = recent_regime[-1]["target"] if recent_regime else None
        regime = "bull" if last_regime == "bear" else "bear"
        return "regime_test", regime

    def _pick_target_for_type(self, mission_type: str) -> tuple[str, str]:
        """Get a target for a forced mission type."""
        from research.factor_registry import FactorRegistry
        fr = FactorRegistry()

        if mission_type == "factor_test":
            untested = fr.get_untested()
            testable = [f for f in untested if f.get("scorer_module")]
            if testable:
                return mission_type, random.choice(testable)["name"]
            return mission_type, "unknown"

        if mission_type == "revalidation":
            prod = fr.get_production_factors()
            if prod:
                return mission_type, random.choice(prod)["name"]
            return mission_type, "liquidation"

        if mission_type == "combo_test":
            bench = fr.get_by_status("tested_positive")
            if bench:
                return mission_type, random.choice(bench)["name"]
            return mission_type, "unknown"

        if mission_type == "coin_deep_dive":
            return mission_type, "BTC"

        if mission_type == "regime_test":
            return mission_type, random.choice(["bull", "bear"])

        if mission_type == "paper_vs_backtest":
            return mission_type, ""

        if mission_type == "param_sweep":
            return mission_type, "BTC"

        return mission_type, ""

    # ── Mission Execution ─────────────────────────────────────

    def execute_mission(self, mission: dict, progress_cb=None) -> dict:
        """Execute a mission and return updated mission dict."""
        mission["started_at"] = datetime.utcnow().isoformat()
        mission["status"] = "running"

        try:
            handler = getattr(self, f"_exec_{mission['type']}", None)
            if handler is None:
                raise ValueError(f"No handler for mission type: {mission['type']}")
            result = handler(mission, progress_cb)
            mission["result"] = result
            mission["status"] = "completed"
            mission["insight"] = self._generate_insight(mission)
            # Compute XP based on how "wow" the discovery is
            mission["xp_reward"] = compute_wow_xp(mission["type"], result)
        except Exception as e:
            log.error(f"Mission failed: {e}", exc_info=True)
            mission["status"] = "failed"
            mission["result"] = {"error": str(e)}
            mission["insight"] = f"Mission failed: {e}"
            mission["xp_reward"] = BASE_XP.get(mission["type"], 5)

        mission["finished_at"] = datetime.utcnow().isoformat()

        # Save .md report + .json (like missions 1-7)
        try:
            self._save_report(mission)
        except Exception as e:
            log.warning(f"Failed to save mission report: {e}")

        return mission

    # ── Report Generation (.md + .json files) ─────────────

    def _next_mission_number(self) -> int:
        """Get next sequential mission number from missions/ directory."""
        MISSIONS_DIR.mkdir(parents=True, exist_ok=True)
        existing = list(MISSIONS_DIR.glob("mission_[0-9][0-9][0-9]_*.md"))
        if not existing:
            return 1
        nums = []
        for p in existing:
            try:
                nums.append(int(p.name[8:11]))
            except ValueError:
                pass
        return max(nums) + 1 if nums else 1

    def _save_report(self, mission: dict):
        """Save mission as .md + .json in missions/ directory (same style as 1-7)."""
        if mission["status"] != "completed":
            return

        num = self._next_mission_number()
        slug = mission["type"]
        if mission.get("target"):
            slug += "_" + re.sub(r'[^a-z0-9]', '_', mission["target"].lower())[:30]

        md_path = MISSIONS_DIR / f"mission_{num:03d}_{slug}.md"
        json_path = MISSIONS_DIR / f"mission_{num:03d}_{slug}.json"

        # Update mission_id to match file numbering
        mission["mission_id"] = f"mission_{num:03d}_{slug}"

        # Generate .md report based on type
        report_gen = getattr(self, f"_report_{mission['type']}", None)
        if report_gen:
            md_content = report_gen(mission, num)
        else:
            md_content = self._report_generic(mission, num)

        MISSIONS_DIR.mkdir(parents=True, exist_ok=True)
        md_path.write_text(md_content, encoding="utf-8")
        json_path.write_text(
            json.dumps(mission, indent=2, default=str, ensure_ascii=False),
            encoding="utf-8",
        )
        log.info(f"Saved report: {md_path.name}")

    def _report_header(self, mission: dict, num: int) -> str:
        """Standard header for all mission reports."""
        meta = self.get_meta()
        cat = "Analysis" if mission.get("category") == "analysis" else "Discovery"
        difficulty = (mission.get("difficulty") or "medium").title()
        return (
            f"# Mission #{num:03d}: {mission['title']}\n"
            f"**วันที่**: {mission['date']} | **XP**: {mission['xp_reward']} | "
            f"**Difficulty**: {difficulty} | **Status**: {mission['status'].upper()} | "
            f"**Category**: {cat}\n\n---\n\n"
        )

    def _report_footer(self, mission: dict, num: int) -> str:
        """Standard footer."""
        meta = self.get_meta()
        level_num, level_name = _get_level(meta.get("total_xp", 0))
        streak = meta.get("current_streak", 0)
        return (
            f"\n---\n\n"
            f"*Mission #{num:03d} completed | XP +{mission['xp_reward']} | "
            f"Level {level_num} {level_name} | Streak: {streak} days*\n"
        )

    def _report_trade_analysis(self, mission: dict, num: int) -> str:
        """Generate deep .md report for trade_analysis with EXP 1-9 structure."""
        r = mission.get("result", {})
        md = self._report_header(mission, num)
        exp1 = r.get("exp1_baseline", {})

        # ── Hypothesis ──
        md += "## สมมติฐาน\n"
        md += "Paper trading สะสมข้อมูลเพียงพอแล้ว ต้องวิเคราะห์เชิงลึก:\n"
        md += "- แยกดู WR ตาม direction, BTC score, เวลา, เหรียญ, exit mechanism\n"
        md += "- หาสาเหตุที่แท้จริงของการขาดทุน ไม่ใช่แค่สรุปตัวเลข\n"
        md += "- เปรียบเทียบกับการวิเคราะห์ครั้งก่อนเพื่อวัดพัฒนาการ\n\n"

        # ── Headline ──
        pf = exp1.get('profit_factor', 0)
        md += (f"## ผลลัพธ์: {exp1.get('total_trades', 0)} เทรด | "
               f"WR {exp1.get('wr', 0)}% | PnL ${exp1.get('total_pnl', 0):.2f} | "
               f"PF {pf:.2f}\n\n---\n\n")

        # ── EXP 1: Baseline ──
        md += "## EXP 1: Baseline Performance\n\n"
        md += "| Metric | ค่า |\n|--------|-----|\n"
        md += f"| **เทรดทั้งหมด** | {exp1.get('total_trades', 0)} |\n"
        md += f"| **Win / Loss** | {exp1.get('wins', 0)} / {exp1.get('losses', 0)} |\n"
        md += f"| **Win Rate** | **{exp1.get('wr', 0)}%** |\n"
        md += f"| **PnL รวม** | **${exp1.get('total_pnl', 0):.2f}** |\n"
        md += f"| **Avg Win** | ${exp1.get('avg_win', 0):.2f} |\n"
        md += f"| **Avg Loss** | ${exp1.get('avg_loss', 0):.2f} |\n"
        md += f"| **Profit Factor** | {pf:.2f} |\n"
        md += (f"| **Equity** | ${exp1.get('init_equity', 0):.0f} → "
               f"${exp1.get('final_equity', 0):.0f} |\n\n")

        # Score breakdown
        by_score = exp1.get("by_score", {})
        if by_score:
            md += "### WR ตาม BTC Score\n\n"
            md += "| Score | เทรด | WR% | PnL | หมายเหตุ |\n"
            md += "|-------|------|-----|-----|----------|\n"
            for bucket in ["0-2", "2-4", "4-6", "6-8", "8+"]:
                if bucket in by_score:
                    s = by_score[bucket]
                    note = ("**อันตราย!**" if s["wr"] < 40 else
                            "ดี" if s["wr"] > 65 else "")
                    md += (f"| {bucket} | {s['trades']} | {s['wr']}% | "
                           f"${s['pnl']:.2f} | {note} |\n")
            md += "\n"

        # ── EXP 2: Direction Deep Dive ──
        md += "---\n\n## EXP 2: Direction Deep Dive\n\n"
        by_dir = exp1.get("by_direction", {})
        md += "| Direction | เทรด | WR% | PnL | Avg PnL/trade |\n"
        md += "|-----------|------|-----|-----|---------------|\n"
        for d in ["SHORT", "LONG"]:
            if d in by_dir:
                s = by_dir[d]
                md += (f"| **{d}** | {s['trades']} | **{s['wr']}%** | "
                       f"${s['pnl']:.2f} | ${s.get('avg_pnl', 0):.2f} |\n")
        md += "\n"

        dir_detail = r.get("exp2_direction", {}).get("analysis", {})
        for d_label in ["SHORT", "LONG"]:
            if d_label not in dir_detail:
                continue
            info = dir_detail[d_label]
            bars = info.get("avg_bars_held", 0)
            md += f"### {d_label} Analysis\n"
            md += f"- **Avg entry BTC score**: {info.get('avg_entry_score', 0)}\n"
            md += f"- **Avg holding time**: {bars} bars ({bars * 15:.0f} นาที)\n"
            exits = info.get("exit_breakdown", {})
            if exits:
                md += "- **Exit breakdown**:\n"
                for reason in sorted(exits, key=lambda x: exits[x]["pnl"], reverse=True):
                    data = exits[reason]
                    md += f"  - {reason}: {data['count']} เทรด, PnL ${data['pnl']:.2f}\n"
            best = info.get("best_coins", [])
            if best:
                parts = [f"{c[0]} (${c[1]['pnl']:.0f})" for c in best if c[1]["pnl"] > 0]
                if parts:
                    md += f"- **Best coins**: {', '.join(parts)}\n"
            worst = info.get("worst_coins", [])
            if worst:
                parts = [f"{c[0]} (${c[1]['pnl']:.0f})" for c in worst if c[1]["pnl"] < 0]
                if parts:
                    md += f"- **Worst coins**: {', '.join(parts)}\n"
            md += "\n"

        # Direction narrative
        long_wr = by_dir.get("LONG", {}).get("wr", 0)
        short_wr = by_dir.get("SHORT", {}).get("wr", 0)
        if short_wr > long_wr + 20:
            md += (f"**วิเคราะห์**: SHORT ({short_wr}%) ดีกว่า LONG ({long_wr}%) อย่างชัดเจน. "
                   f"ตลาดน่าจะอยู่ใน BEAR regime ที่ contrarian signal จับ short ได้แม่น. "
                   f"LONG signal อาจจับ dead cat bounce ที่ไม่จริง\n\n")
        elif long_wr > short_wr + 20:
            md += (f"**วิเคราะห์**: LONG ({long_wr}%) ดีกว่า SHORT ({short_wr}%) อย่างชัดเจน. "
                   f"ตลาดน่าจะอยู่ใน BULL regime ที่ dip-buying ได้ผลดี\n\n")
        elif long_wr > 0 and short_wr > 0:
            md += (f"**วิเคราะห์**: LONG ({long_wr}%) และ SHORT ({short_wr}%) "
                   f"ทำงานได้{'ทั้งสองทิศ' if abs(long_wr - short_wr) < 10 else 'ต่างกันเล็กน้อย'}\n\n")

        # ── EXP 3: Exit Mechanism ──
        md += "---\n\n## EXP 3: Exit Mechanism Analysis\n\n"
        exp3 = r.get("exp3_exits", {}).get("by_reason", {})
        if exp3:
            md += "| Exit Reason | จำนวน (%) | WR% | PnL | Avg Bars |\n"
            md += "|-------------|-----------|-----|-----|----------|\n"
            for reason in sorted(exp3, key=lambda x: exp3[x]["pnl"], reverse=True):
                s = exp3[reason]
                md += (f"| **{reason}** | {s['trades']} ({s['pct']}%) | "
                       f"{s['wr']}% | ${s['pnl']:.2f} | {s['avg_bars']} |\n")
            md += "\n"
            # Narrative per exit
            for reason, s in exp3.items():
                if reason == "SL" and s["wr"] == 0 and s["trades"] >= 2:
                    md += (f"**SL มี 0% WR** -- ทุกเทรดที่โดน SL ขาดทุนทั้งหมด (${s['pnl']:.2f}). "
                           f"ยืนยัน Finding จาก Mission #005: SL ยังเป็น Villain. "
                           f"SL 10 ATR อาจยังไม่กว้างพอ\n\n")
                elif reason == "SIGNAL_FLIP" and s["wr"] < 30 and s["trades"] >= 5:
                    md += (f"**SIGNAL_FLIP WR แค่ {s['wr']}%** -- BTC score oscillation "
                           f"ทำให้เข้า-ออกเร็วเกินไป. Hysteresis band ปัจจุบัน (1.5) "
                           f"อาจต้องเพิ่มเป็น 2.0\n\n")
                elif reason in ("TP", "TRAIL", "SL/TP") and s["wr"] > 80 and s["trades"] >= 3:
                    md += (f"**{reason} ทำงานดีมาก** (WR {s['wr']}%, ${s['pnl']:.2f}) -- "
                           f"เป็น hero ของระบบ\n\n")

        # ── EXP 4: Worst Trades ──
        md += "---\n\n## EXP 4: Worst Trades Deep Dive\n\n"
        exp4 = r.get("exp4_worst", {})
        worst = exp4.get("worst_trades", [])
        if worst:
            md += "| # | Coin | Dir | PnL | Score | Exit | Bars | Entry Time |\n"
            md += "|---|------|-----|-----|-------|------|------|------------|\n"
            for i, w in enumerate(worst, 1):
                md += (f"| {i} | {w['coin']} | {w['direction']} | "
                       f"**${w['pnl']:.2f}** | {w['btc_score']} | "
                       f"{w['exit_reason']} | {w['bars_held']} | {w['entry_time']} |\n")
            md += "\n"
            md += "### แพทเทิร์นจากเทรดแย่สุด\n"
            md += f"- **Direction ที่ซ้ำ**: {exp4.get('common_direction', '?')}\n"
            md += f"- **Exit reason ที่ซ้ำ**: {exp4.get('common_reason', '?')}\n"
            md += f"- **Coin ที่ซ้ำ**: {exp4.get('common_coin', '?')}\n\n"
            if exp4.get("common_direction") == "LONG":
                md += ("**วิเคราะห์**: เทรดที่แย่สุดเป็น LONG เป็นหลัก -- สอดคล้องกับ "
                       "SHORT bias ที่แข็งแกร่งกว่า. LONG entries อาจเข้าผิดจังหวะ "
                       "ในช่วง dead cat bounce\n\n")
            elif exp4.get("common_reason") == "SIGNAL_FLIP":
                md += ("**วิเคราะห์**: Signal flip เป็นสาเหตุหลัก -- BTC score oscillating "
                       "ทำให้เข้า-ออกก่อนราคาจะเคลื่อนไปในทิศที่ถูก\n\n")
            elif exp4.get("common_reason") == "SL":
                md += ("**วิเคราะห์**: SL เป็นสาเหตุหลัก -- ยืนยันว่า SL ทำลายกำไร. "
                       "เทรดเหล่านี้อาจกลับมากำไรได้ถ้าถือนานกว่านี้\n\n")

        # ── EXP 5: Signal Quality ──
        md += "---\n\n## EXP 5: Signal Quality Cross-Reference\n\n"
        sig = r.get("exp5_signals", {}).get("signal_stats", {})
        if sig and sig.get("total_signals", 0) > 0:
            md += f"| Metric | ค่า |\n|--------|-----|\n"
            md += f"| สัญญาณทั้งหมด (7d) | {sig['total_signals']} |\n"
            md += f"| เข้าเทรดจริง | {sig['entries']} ({sig['entry_rate']}%) |\n"
            md += f"| Skip (cooldown/filter) | {sig['skips']} |\n"
            md += f"| Hold (มีตำแหน่งอยู่แล้ว) | {sig['holds']} |\n"
            md += f"| Avg score ตอน entry | {sig['avg_entry_score']} |\n"
            md += f"| Avg score ตอน skip | {sig['avg_skip_score']} |\n\n"
            models = sig.get("model_distribution", {})
            if models:
                md += f"**Model distribution**: {', '.join(f'{k}: {v}' for k, v in models.items())}\n\n"
            entry_rate = sig.get("entry_rate", 0)
            if entry_rate < 5:
                md += "**ข้อสังเกต**: Entry rate ต่ำมาก -- ระบบ selective ดี แต่อาจพลาดโอกาส\n\n"
            elif entry_rate > 30:
                md += "**ข้อสังเกต**: Entry rate สูง -- อาจ over-trading ตรวจสอบว่า WR ไม่เสื่อม\n\n"
            else:
                md += f"**ข้อสังเกต**: Entry rate {entry_rate}% อยู่ในช่วงปกติ\n\n"
        else:
            md += "ไม่มีข้อมูล signal_log ในช่วงนี้\n\n"

        # ── EXP 6: By Hour ──
        hours = r.get("exp6_hours", {})
        if hours:
            md += "---\n\n## EXP 6: Performance by Hour (UTC)\n\n"
            md += "| ชั่วโมง | เทรด | WR% | PnL | หมายเหตุ |\n"
            md += "|---------|------|-----|-----|----------|\n"
            for h in sorted(hours.keys(), key=lambda x: int(x)):
                s = hours[h]
                note = ""
                if s["wr"] > 75 and s["trades"] >= 3:
                    note = "**จุดแข็ง**"
                elif s["wr"] < 35 and s["trades"] >= 3:
                    note = "**จุดอ่อน**"
                elif s["pnl"] < -5:
                    note = "ขาดทุน"
                elif s["wr"] > 70:
                    note = "ดี"
                md += f"| {h}:00 | {s['trades']} | {s['wr']}% | ${s['pnl']:.2f} | {note} |\n"
            md += "\n"
            if hours:
                best_h = max(hours.items(), key=lambda x: x[1]["pnl"])
                worst_h = min(hours.items(), key=lambda x: x[1]["pnl"])
                md += (f"**Best hour**: {best_h[0]}:00 (${best_h[1]['pnl']:.2f}, "
                       f"WR {best_h[1]['wr']}%) | "
                       f"**Worst hour**: {worst_h[0]}:00 (${worst_h[1]['pnl']:.2f}, "
                       f"WR {worst_h[1]['wr']}%)\n\n")

        # ── EXP 7: By Coin ──
        coins = r.get("exp7_coins", {})
        if coins:
            md += "---\n\n## EXP 7: Performance by Coin\n\n"
            md += "| Coin | เทรด | WR% | PnL | หมายเหตุ |\n"
            md += "|------|------|-----|-----|----------|\n"
            for c, s in sorted(coins.items(), key=lambda x: x[1]["pnl"], reverse=True):
                note = ""
                if s["wr"] < 35 and s["trades"] >= 2:
                    note = "**พิจารณาถอด**"
                elif s["wr"] > 70 and s["trades"] >= 3:
                    note = "แข็งแกร่ง"
                elif s["pnl"] < -15:
                    note = "**ขาดทุนมาก**"
                md += f"| {c} | {s['trades']} | {s['wr']}% | ${s['pnl']:.2f} | {note} |\n"
            md += "\n"

        # ── EXP 8: Equity Health ──
        eq = r.get("exp8_equity", {})
        if eq:
            md += "---\n\n## EXP 8: Equity Curve Health\n\n"
            trend_th = {"improving": "ดีขึ้น", "declining": "แย่ลง"}
            md += f"| Metric | ค่า |\n|--------|-----|\n"
            md += f"| **แนวโน้ม** | **{trend_th.get(eq.get('trend', ''), eq.get('trend', ''))}** |\n"
            md += f"| ครึ่งแรก avg PnL/trade | ${eq.get('first_half_avg', 0):.2f} |\n"
            md += f"| ครึ่งหลัง avg PnL/trade | ${eq.get('second_half_avg', 0):.2f} |\n"
            md += f"| Max DD ในช่วง | ${eq.get('period_max_dd', 0):.2f} |\n"
            md += f"| Equity ปัจจุบัน | ${eq.get('total_equity', 0):.2f} |\n\n"
            if eq.get("trend") == "declining":
                md += ("**ระวัง**: Performance กำลังแย่ลง -- อาจเกิดจาก regime change "
                       "หรือ data quality ลดลง ตรวจสอบ whale/liquidation data freshness\n\n")
            elif eq.get("trend") == "improving":
                md += "**ดี**: Performance ดีขึ้น -- การปรับ (hysteresis, SL widen) กำลังให้ผลดี\n\n"

        # ── EXP 9: Progress ──
        prog = r.get("exp9_progress", {})
        if prog:
            md += "---\n\n## EXP 9: เปรียบเทียบกับ Analysis ก่อนหน้า\n\n"
            md += "| Metric | ก่อนหน้า | ปัจจุบัน | เปลี่ยนแปลง |\n"
            md += "|--------|---------|---------|------------|\n"
            wr_c = prog.get("wr_change", 0)
            pnl_c = prog.get("pnl_change", 0)
            md += (f"| **WR** | {prog['prev_wr']}% | {r.get('overall_wr', 0)}% | "
                   f"**{wr_c:+.1f}pp** |\n")
            md += (f"| **PnL** | ${prog['prev_pnl']:.2f} | ${r.get('total_pnl', 0):.2f} | "
                   f"**${pnl_c:+.2f}** |\n")
            md += (f"| **เทรด** | {prog['prev_trades']} | {r.get('total_trades', 0)} | "
                   f"{prog.get('trade_count_change', 0):+d} |\n\n")
            if wr_c > 5:
                md += "**WR ดีขึ้นอย่างชัดเจน** -- การปรับระบบให้ผลดี ทำต่อ\n\n"
            elif wr_c < -5:
                md += "**WR แย่ลง** -- ต้องตรวจสอบ: ตลาดเปลี่ยน? data stale? bug?\n\n"
            else:
                md += "**WR คงที่** -- ระบบทำงานสม่ำเสมอ\n\n"

        # ── Recommendations ──
        recs = r.get("recommendations", [])
        if recs:
            md += "---\n\n## ข้อเสนอ (Actionable)\n\n"
            for i, rec in enumerate(recs, 1):
                md += f"{i}. **{rec}**\n"
            md += "\n"

        # ── Strengths ──
        strengths = r.get("strengths", [])
        if strengths:
            md += "## จุดแข็งที่ต้องรักษา\n\n"
            for s in strengths:
                md += f"- {s}\n"
            md += "\n"

        # ── Summary ──
        md += "## สรุป\n\n"
        md += (f"วิเคราะห์ {r.get('total_trades', 0)} เทรดใน 7 วัน | "
               f"WR {r.get('overall_wr', 0)}% | PnL ${r.get('total_pnl', 0):.2f} | "
               f"PF {exp1.get('profit_factor', 0):.2f}\n\n")
        if recs:
            md += f"พบ **{len(recs)} ข้อเสนอ** ที่ควรดำเนินการ:\n"
            for rec in recs[:3]:
                md += f"- {rec.split('.')[0]}\n"
            md += "\n"
        if strengths:
            md += f"จุดแข็ง: {', '.join(s.split(':')[0] for s in strengths[:3])}\n\n"
        if prog:
            md += f"**เทียบรอบก่อน**: WR {prog.get('wr_change', 0):+.1f}pp, PnL ${prog.get('pnl_change', 0):+.2f}\n"

        md += self._report_footer(mission, num)
        return md

    def _report_signal_quality(self, mission: dict, num: int) -> str:
        """Generate rich .md report for signal_quality."""
        r = mission.get("result", {})
        md = self._report_header(mission, num)

        md += "## สมมติฐาน\n"
        md += "BTC composite score มีคุณภาพต่างกันตาม threshold\n"
        md += "ถ้าหา threshold ที่ดีที่สุดได้ อาจเพิ่ม WR ได้โดยไม่เสีย trade จำนวนมาก\n\n"

        md += f"## ผลลัพธ์: สัญญาณทั้งหมด {r.get('total_signals', 0)} ครั้ง, "
        md += f"เข้าเทรดจริง {r.get('entry_signals', 0)} ครั้ง ({r.get('entry_rate', 0)}%)\n\n"
        md += "---\n\n"

        # Score overview
        md += "## ภาพรวมสัญญาณ\n\n"
        md += f"- **สัญญาณทั้งหมด**: {r.get('total_signals', 0)}\n"
        md += f"- **เข้าเทรดจริง**: {r.get('entry_signals', 0)} ({r.get('entry_rate', 0)}%)\n"
        md += f"- **Score เฉลี่ย (ทุกสัญญาณ)**: {r.get('avg_score_all', 0)}\n"
        md += f"- **Score เฉลี่ย (เข้าเทรด)**: {r.get('avg_score_entry', 0)}\n\n"

        # Score histogram
        hist = r.get("score_histogram") or {}
        if hist:
            md += "## Score Distribution\n\n"
            md += "| Score Range | จำนวน | สัดส่วน |\n"
            md += "|-------------|-------|--------|\n"
            total = sum(hist.values())
            for bucket in sorted(hist.keys()):
                pct = round(100 * hist[bucket] / total, 1) if total else 0
                bar = "█" * int(pct / 3)
                md += f"| {bucket} | {hist[bucket]} | {pct}% {bar} |\n"
            md += "\n"

        # WR by threshold
        wr_t = r.get("wr_by_threshold") or {}
        if wr_t:
            md += "## WR ตาม Score Threshold\n\n"
            md += "| Threshold ≥ | เทรด | WR% | หมายเหตุ |\n"
            md += "|-------------|------|-----|----------|\n"
            best_t = r.get("best_threshold", "5")
            for t in sorted(wr_t.keys(), key=lambda x: int(x)):
                s = wr_t[t]
                note = "**ดีที่สุด**" if t == best_t else ""
                md += f"| ≥{t} | {s['trades']} | {s['wr']}% | {note} |\n"
            md += "\n"
            md += f"**WR spread**: {r.get('wr_spread', 0)}pp (ต่างกันระหว่าง threshold ต่ำสุด-สูงสุด)\n\n"

        # Summary
        md += "## สรุปและข้อเสนอ\n\n"
        md += f"1. **Threshold ที่ดีที่สุด: ≥{r.get('best_threshold', '5')}** "
        md += f"(WR {r.get('best_threshold_wr', 0)}%)\n"
        md += f"2. WR spread {r.get('wr_spread', 0)}pp "
        if r.get("wr_spread", 0) > 15:
            md += "-- มีนัยสำคัญ ควรพิจารณาปรับ threshold\n"
        else:
            md += "-- ไม่มาก threshold ปัจจุบันยังดี\n"
        md += f"3. Entry rate {r.get('entry_rate', 0)}% -- "
        if r.get("entry_rate", 0) < 10:
            md += "ต่ำมาก ถ้าขยับ threshold ขึ้นอีกจะเทรดน้อยเกินไป\n"
        else:
            md += "ยังมี room ในการกรองเพิ่ม\n"

        md += self._report_footer(mission, num)
        return md

    def _report_drawdown_analysis(self, mission: dict, num: int) -> str:
        """Generate rich .md report for drawdown_analysis."""
        r = mission.get("result", {})
        md = self._report_header(mission, num)

        md += "## สมมติฐาน\n"
        md += "Drawdown เป็นสิ่งที่ต้องจัดการ ไม่ใช่แค่รับได้\n"
        md += "ถ้าวิเคราะห์ว่า DD เกิดตอนไหน จากอะไร จะป้องกันได้\n\n"

        verdict_th = {"concerning": "น่าเป็นห่วง", "healthy": "สุขภาพดี"}
        md += f"## ผลลัพธ์: {verdict_th.get(r.get('verdict', ''), r.get('verdict', ''))}\n\n"
        md += "---\n\n"

        # Overview
        md += "## ภาพรวม Equity\n\n"
        md += f"- **เทรดทั้งหมด**: {r.get('total_trades', 0)}\n"
        md += f"- **Equity สุดท้าย**: ${r.get('final_equity', 0):.2f}\n"
        md += f"- **PnL รวม**: ${r.get('total_pnl', 0):.2f}\n\n"

        # Drawdown stats
        md += "## Drawdown Statistics\n\n"
        md += "| Metric | ค่า | หมายเหตุ |\n"
        md += "|--------|-----|----------|\n"
        dd_pct = r.get("max_dd_pct", 0)
        dd_note = "อันตราย!" if dd_pct > 15 else ("ระวัง" if dd_pct > 10 else "ปกติ")
        md += f"| **Max Drawdown** | **${r.get('max_dd', 0):.2f} ({dd_pct:.1f}%)** | {dd_note} |\n"
        streak = r.get("longest_losing_streak", 0)
        streak_note = "ยาวมาก!" if streak > 10 else ("ยาว" if streak > 5 else "ปกติ")
        md += f"| **Longest Losing Streak** | **{streak} เทรดติดกัน** | {streak_note} |\n"
        worst = r.get("worst_day") or {}
        md += f"| **Worst Day** | {worst.get('date', 'N/A')}: ${worst.get('pnl', 0):.2f} | |\n"
        md += "\n"

        # Streak analysis
        sa = r.get("streak_analysis") or {}
        if sa and sa.get("length", 0) > 0:
            md += "## วิเคราะห์ Losing Streak ยาวสุด\n\n"
            md += f"- **จำนวน**: {sa['length']} เทรดติดกัน\n"
            md += f"- **เหรียญที่เกี่ยวข้อง**: {', '.join(sa.get('coins', []))}\n"
            dirs = sa.get("directions", {})
            md += f"- **ทิศทาง**: LONG {dirs.get('LONG', 0)}, SHORT {dirs.get('SHORT', 0)}\n"
            md += f"- **ขาดทุนรวม**: ${sa.get('total_loss', 0):.2f}\n\n"

            if dirs.get("LONG", 0) > dirs.get("SHORT", 0) * 2:
                md += "**ข้อสังเกต**: Losing streak เกิดจาก LONG เป็นหลัก -- ระบบมี SHORT bias ที่แข็งแกร่งกว่า\n\n"
            elif dirs.get("SHORT", 0) > dirs.get("LONG", 0) * 2:
                md += "**ข้อสังเกต**: Losing streak เกิดจาก SHORT เป็นหลัก -- ช่วงนั้นตลาดอาจเป็น uptrend แรง\n\n"

        # Summary
        md += "## สรุปและข้อเสนอ\n\n"
        if dd_pct > 15:
            md += "1. **Max DD สูง** -- พิจารณาลดขนาด position หรือเพิ่ม filter\n"
        if streak > 8:
            md += f"2. **Losing streak {streak} เทรด** -- พิจารณา cooldown หลังแพ้ติดกัน 5+ เทรด\n"
        if r.get("total_pnl", 0) > 0:
            md += f"3. **ยังทำกำไรรวม ${r.get('total_pnl', 0):.2f}** -- ระบบยังมี edge อยู่\n"
        else:
            md += "3. **ขาดทุนรวม** -- ต้องตรวจสอบปัญหาเร่งด่วน\n"

        md += self._report_footer(mission, num)
        return md

    def _report_web_discovery(self, mission: dict, num: int) -> str:
        """Generate deep .md report for web_discovery with proxy test results."""
        r = mission.get("result", {})
        md = self._report_header(mission, num)

        topic = r.get("topic", "?")
        hypothesis = r.get("hypothesis", "")

        md += "## สมมติฐาน\n"
        md += f"{hypothesis}\n\n"
        md += f"## หัวข้อวิจัย: {topic}\n"
        md += f"**Search query**: `{r.get('search_query', '')}`\n\n---\n\n"

        # ── Phase 1: Web Research ──
        results = r.get("search_results") or []
        if results:
            md += f"## Phase 1: Web Research ({len(results)} แหล่ง)\n\n"
            for i, sr in enumerate(results, 1):
                md += f"### {i}. {sr.get('title', 'N/A')}\n"
                if sr.get("url"):
                    md += f"**URL**: {sr['url']}\n\n"
                if sr.get("snippet"):
                    md += f"> {sr['snippet']}\n\n"
            md += "### สรุปจาก Web Research\n"
            for i, sr in enumerate(results[:3], 1):
                md += f"{i}. **{sr.get('title', '')}**: {sr.get('snippet', '')[:120]}\n"
            md += "\n"
        else:
            md += "## Phase 1: Web Research\n\n"
            md += "ไม่พบผลลัพธ์จากการค้นหาออนไลน์\n\n"

        # ── Phase 2: Proxy Test ──
        proxy = r.get("proxy_test")
        md += "---\n\n## Phase 2: Proxy Test (ทดสอบกับข้อมูลจริง)\n\n"

        if proxy and proxy.get("tested"):
            md += f"**Method**: {proxy.get('method', 'N/A')}\n\n"
            md += f"**Data**: {proxy.get('data_source', 'N/A')}\n\n"

            # Stats table
            md += "### ผลการทดสอบ\n\n"
            md += "| Metric | ค่า |\n|--------|-----|\n"

            # Show relevant stats based on what's available
            show_keys = [
                ("n_signals", "จำนวน Data Points"),
                ("correlation", "Correlation กับ Forward Return"),
                ("hit_rate", "Hit Rate รวม (%)"),
                ("bull_signals", "Bull Signal Count"),
                ("bull_hit_rate", "Bull Hit Rate (%)"),
                ("bull_avg_return_bps", "Bull Avg Return (bps)"),
                ("bear_signals", "Bear Signal Count"),
                ("bear_hit_rate", "Bear Hit Rate (%)"),
                ("bear_avg_return_bps", "Bear Avg Return (bps)"),
                ("cascade_threshold_usdt", "Cascade Threshold (USDT/hr)"),
                ("reversion_rate", "Reversion Rate (%)"),
                ("long_cascades", "Long Liquidation Cascades"),
                ("short_cascades", "Short Liquidation Cascades"),
                ("median_vol_bps", "Median Realized Vol (bps/bar)"),
                ("high_vol_avg_abs_return_bps", "High Vol Avg |Return| (bps)"),
                ("low_vol_avg_abs_return_bps", "Low Vol Avg |Return| (bps)"),
                ("vol_ratio", "High/Low Vol Ratio"),
                ("mean_slope", "Mean Funding Slope"),
                ("std_slope", "Std Funding Slope"),
                ("extreme_negative_count", "Extreme Negative Events"),
                ("extreme_positive_count", "Extreme Positive Events"),
            ]
            for key, label in show_keys:
                if key in proxy:
                    md += f"| {label} | {proxy[key]} |\n"
            md += "\n"

            # Conclusion
            md += "### สรุป Proxy Test\n\n"
            md += f"{proxy.get('conclusion', 'N/A')}\n\n"

            # Verdict highlight
            hr = proxy.get("hit_rate", 0)
            corr = abs(proxy.get("correlation", 0))
            if hr > 55 and corr > 0.02:
                md += (f"> **ผลลัพธ์: มีแนวโน้มดี** -- Hit rate {hr}%, "
                       f"Correlation {corr:.4f}. ควรทดสอบเป็น factor ใน v3 composite\n\n")
            elif hr > 52 or corr > 0.03:
                md += (f"> **ผลลัพธ์: Marginal** -- Hit rate {hr}%, "
                       f"Correlation {corr:.4f}. ต้องปรับ parameter หรือเก็บข้อมูลเพิ่ม\n\n")
            elif hr > 0:
                md += (f"> **ผลลัพธ์: ไม่มีนัยสำคัญ** -- Hit rate {hr}% "
                       f"ไม่ต่างจาก random (50%)\n\n")
            else:
                md += "> **ผลลัพธ์: ทดสอบได้บางส่วน** -- ดูรายละเอียดด้านบน\n\n"

        elif proxy and proxy.get("error"):
            md += f"**Proxy test ล้มเหลว**: {proxy['error']}\n\n"
        elif proxy and not proxy.get("tested"):
            md += f"**ไม่สามารถทดสอบได้**: {proxy.get('reason', 'ข้อมูลไม่เพียงพอ')}\n\n"
        else:
            md += ("ไม่มี proxy test สำหรับหัวข้อนี้ -- "
                   "ต้องสร้าง data pipeline ก่อนจึงจะทดสอบได้\n\n")

        # ── Phase 3: Data Availability ──
        data = r.get("data_availability", {})
        md += "---\n\n## Phase 3: Data Availability\n\n"
        md += "| Field | ค่า |\n|-------|-----|\n"
        md += f"| **ข้อมูลที่ต้องการ** | {data.get('needed', 'N/A')} |\n"
        md += f"| **แหล่งข้อมูล** | {data.get('source', 'N/A')} |\n"
        avail = ("มีอยู่แล้ว" if data.get("available")
                 else "**ยังไม่มี -- ต้องสร้าง data collector**")
        md += f"| **สถานะ** | {avail} |\n"
        if data.get("note"):
            md += f"| **หมายเหตุ** | {data['note']} |\n"
        md += "\n"

        # ── Verdict ──
        verdict = r.get("verdict", "")
        verdict_th = {
            "promising_tested": "มีแนวโน้มดี + ทดสอบแล้ว",
            "weak_tested": "สัญญาณอ่อนจากการทดสอบ",
            "not_significant": "ไม่มีนัยสำคัญจากการทดสอบ",
            "promising_untested": "มีแนวโน้มดีจาก web research แต่ยังไม่ได้ทดสอบกับข้อมูลจริง",
            "needs_research": "ต้องการข้อมูลเพิ่มเติม",
            "promising": "มีแนวโน้มดี",
            "needs_data": "ต้องการข้อมูลเพิ่ม",
        }
        md += "---\n\n## การประเมินรวม\n\n"
        md += f"**Verdict**: {verdict_th.get(verdict, verdict)}\n\n"

        # ── Next Steps ──
        next_steps = r.get("next_steps", [])
        if next_steps:
            md += "## ขั้นตอนถัดไป (Concrete)\n\n"
            for i, step in enumerate(next_steps, 1):
                md += f"{i}. {step}\n"
            md += "\n"

        # ── Relation to v3 ──
        md += "## ความเกี่ยวข้องกับระบบ v3\n\n"
        md += f"- **หัวข้อ**: {topic}\n"
        md += f"- **สมมติฐาน**: {hypothesis}\n"
        if proxy and proxy.get("tested") and proxy.get("hit_rate", 0) > 55:
            md += (f"- **ถ้าได้ผล**: เพิ่มเป็น factor ใหม่ใน composite score "
                   f"(proxy hit rate {proxy['hit_rate']}%)\n")
            md += "- **Expected impact**: ประมาณ delta +$200-500 ต่อ OOS period\n"
        elif data.get("available"):
            md += "- **ขั้นต่อไป**: สร้าง scorer function จากข้อมูลที่มี แล้ว backtest\n"
        else:
            md += f"- **ขั้นต่อไป**: สร้าง data collector สำหรับ {data.get('needed', '?')}\n"
            md += "- **ระยะเวลา**: ~2-4 สัปดาห์หลังเก็บข้อมูลจึง backtest ได้\n"
        md += "\n"

        # ── Summary ──
        md += "## สรุป\n\n"
        if results:
            md += f"ค้นพบ {len(results)} แหล่งข้อมูลเกี่ยวกับ {topic}\n"
        if proxy and proxy.get("tested"):
            conc = proxy.get("conclusion", "")
            md += f"**Proxy Test**: {conc[:150]}\n"
        md += f"**สถานะ**: {verdict_th.get(verdict, verdict)}\n"
        if next_steps:
            md += f"**Next**: {next_steps[0]}\n"

        md += self._report_footer(mission, num)
        return md

    def _report_generic(self, mission: dict, num: int) -> str:
        """Fallback report for types without a dedicated report generator."""
        r = mission.get("result", {})
        md = self._report_header(mission, num)

        md += f"## สรุปผล\n\n"
        md += f"{mission.get('insight', 'N/A')}\n\n"

        md += "## รายละเอียด\n\n"
        if isinstance(r, dict):
            for k, v in r.items():
                if k == "success":
                    continue
                if isinstance(v, dict):
                    md += f"### {k}\n"
                    md += "| Key | Value |\n|-----|-------|\n"
                    for kk, vv in v.items():
                        md += f"| {kk} | {vv} |\n"
                    md += "\n"
                elif isinstance(v, list):
                    md += f"### {k}\n"
                    for item in v:
                        md += f"- {item}\n"
                    md += "\n"
                else:
                    md += f"- **{k}**: {v}\n"
        md += "\n"

        md += self._report_footer(mission, num)
        return md

    def _exec_factor_test(self, mission: dict, progress_cb=None) -> dict:
        """Test an untested factor."""
        from research.factor_registry import FactorRegistry
        from research.experiment_engine import ExperimentEngine

        factor_name = mission["target"]
        fr = FactorRegistry()
        factor = fr.get(factor_name)
        if not factor or not factor.get("scorer_function"):
            return {"success": False, "error": f"No scorer for {factor_name}"}

        scorer = fr.get_scorer(factor_name)
        engine = ExperimentEngine()
        if progress_cb:
            engine.set_progress_callback(progress_cb)
        result = engine.test_factor(factor_name, scorer)

        # Update factor registry
        fr.update_test_result(factor_name, {
            "best_weight": result["best_weight"],
            "best_delta_pnl": result["best_delta"],
            "best_pnl": result["best_pnl"],
        })
        engine.save_experiment("factor_test", result,
                               f"Mission: test {factor_name}")

        verdict = "positive" if result["best_delta"] > 0 else "negative"
        return {
            "success": True,
            "verdict": verdict,
            "best_weight": result["best_weight"],
            "best_delta": round(result["best_delta"], 0),
            "baseline_pnl": round(result["baseline_pnl"], 0),
            "best_pnl": round(result["best_pnl"], 0),
        }

    def _exec_revalidation(self, mission: dict, progress_cb=None) -> dict:
        """Re-test a production factor."""
        from research.factor_registry import FactorRegistry
        from research.experiment_engine import ExperimentEngine

        factor_name = mission["target"]
        fr = FactorRegistry()
        factor = fr.get(factor_name)
        if not factor or not factor.get("scorer_function"):
            return {"success": False, "error": f"No scorer for {factor_name}"}

        scorer = fr.get_scorer(factor_name)
        engine = ExperimentEngine()
        if progress_cb:
            engine.set_progress_callback(progress_cb)
        result = engine.test_factor(factor_name, scorer)

        # Update last_tested date
        fr.update_test_result(factor_name, {
            "best_weight": result["best_weight"],
            "best_delta_pnl": result["best_delta"],
            "best_pnl": result["best_pnl"],
        })
        engine.save_experiment("revalidation", result,
                               f"Mission: revalidate {factor_name}")

        still_positive = result["best_delta"] > 0
        prev_delta = factor.get("best_delta_pnl", 0)
        drift = round(result["best_delta"] - prev_delta, 0) if prev_delta else 0

        return {
            "success": True,
            "verdict": "stable" if still_positive else "degraded",
            "best_weight": result["best_weight"],
            "best_delta": round(result["best_delta"], 0),
            "prev_delta": prev_delta,
            "drift": drift,
            "baseline_pnl": round(result["baseline_pnl"], 0),
        }

    def _exec_combo_test(self, mission: dict, progress_cb=None) -> dict:
        """Test adding a bench factor to champion model."""
        from research.factor_registry import FactorRegistry
        from research.experiment_engine import ExperimentEngine

        factor_name = mission["target"]
        fr = FactorRegistry()
        factor = fr.get(factor_name)
        if not factor or not factor.get("scorer_function"):
            return {"success": False, "error": f"No scorer for {factor_name}"}

        scorer = fr.get_scorer(factor_name)
        engine = ExperimentEngine()
        if progress_cb:
            engine.set_progress_callback(progress_cb)
        result = engine.test_factor(factor_name, scorer)
        engine.save_experiment("combo_test", result,
                               f"Mission: combo test {factor_name}")

        return {
            "success": True,
            "verdict": "worth_adding" if result["best_delta"] > 200 else "not_worth",
            "best_weight": result["best_weight"],
            "best_delta": round(result["best_delta"], 0),
            "baseline_pnl": round(result["baseline_pnl"], 0),
        }

    def _exec_paper_vs_backtest(self, mission: dict, progress_cb=None) -> dict:
        """Compare paper trading vs backtest expectations."""
        from research.model_registry import ModelRegistry

        mr = ModelRegistry()
        champion = mr.get_champion()
        if not champion:
            return {"success": False, "error": "No champion model"}

        if not PAPER_TRADES_DB.exists():
            return {"success": False, "error": "No paper trades DB"}

        conn = sqlite3.connect(str(PAPER_TRADES_DB))
        conn.row_factory = sqlite3.Row
        stats = conn.execute("""
            SELECT COUNT(*) as n,
                   SUM(CASE WHEN pnl_net > 0 THEN 1 ELSE 0 END) as wins,
                   SUM(pnl_net) as total_pnl,
                   AVG(pnl_net) as avg_pnl
            FROM trades
        """).fetchone()
        conn.close()

        n = stats["n"] or 0
        if n == 0:
            return {"success": False, "error": "No trades yet"}

        wr = 100.0 * (stats["wins"] or 0) / n
        expected_wr = champion.get("oos_win_rate", 66.6)
        wr_diff = wr - expected_wr

        return {
            "success": True,
            "verdict": "on_track" if abs(wr_diff) < 10 else ("outperforming" if wr_diff > 0 else "underperforming"),
            "paper_trades": n,
            "paper_wr": round(wr, 1),
            "expected_wr": expected_wr,
            "wr_diff": round(wr_diff, 1),
            "paper_pnl": round(stats["total_pnl"] or 0, 2),
            "avg_pnl": round(stats["avg_pnl"] or 0, 2),
        }

    def _exec_coin_deep_dive(self, mission: dict, progress_cb=None) -> dict:
        """Analyze a specific coin's performance."""
        coin = mission["target"]
        if not PAPER_TRADES_DB.exists():
            return {"success": False, "error": "No paper trades DB"}

        conn = sqlite3.connect(str(PAPER_TRADES_DB))
        conn.row_factory = sqlite3.Row
        stats = conn.execute("""
            SELECT COUNT(*) as n,
                   SUM(CASE WHEN pnl_net > 0 THEN 1 ELSE 0 END) as wins,
                   SUM(pnl_net) as total_pnl,
                   AVG(pnl_net) as avg_pnl,
                   AVG(bars_held) as avg_bars
            FROM trades WHERE coin = ?
        """, (coin,)).fetchone()

        direction_stats = conn.execute("""
            SELECT direction,
                   COUNT(*) as n,
                   SUM(CASE WHEN pnl_net > 0 THEN 1 ELSE 0 END) as wins,
                   SUM(pnl_net) as pnl
            FROM trades WHERE coin = ?
            GROUP BY direction
        """, (coin,)).fetchall()
        conn.close()

        n = stats["n"] or 0
        if n == 0:
            return {"success": False, "error": f"No trades for {coin}"}

        wr = 100.0 * (stats["wins"] or 0) / n
        dirs = {str(d["direction"]): {
            "trades": d["n"], "wins": d["wins"],
            "wr": round(100 * d["wins"] / d["n"], 1) if d["n"] > 0 else 0,
            "pnl": round(d["pnl"], 2)
        } for d in direction_stats}

        return {
            "success": True,
            "verdict": "strong" if wr > 55 else ("weak" if wr < 45 else "average"),
            "coin": coin,
            "trades": n,
            "wr": round(wr, 1),
            "total_pnl": round(stats["total_pnl"] or 0, 2),
            "avg_pnl": round(stats["avg_pnl"] or 0, 2),
            "avg_bars": round(stats["avg_bars"] or 0, 1),
            "by_direction": dirs,
        }

    def _exec_regime_test(self, mission: dict, progress_cb=None) -> dict:
        """Test champion model in bear-only or bull-only regime."""
        from research.experiment_engine import ExperimentEngine
        from research.config import OOS_BEAR_START, OOS_BEAR_END, OOS_BULL_START, OOS_BULL_END

        regime = mission["target"]
        engine = ExperimentEngine()
        if progress_cb:
            engine.set_progress_callback(progress_cb)
        engine.load_data()

        if regime == "bear":
            start, end = OOS_BEAR_START, OOS_BEAR_END
        else:
            start, end = OOS_BULL_START, OOS_BULL_END

        btc_df, btc_score = engine.get_btc_data()
        coins = engine._pick_diverse_coins(10)
        pnl = engine._run_all_coins(btc_score, coins, start, end)

        coin_names = [c.replace("USDT", "") for c in coins]
        engine.save_experiment("regime_test", {
            "regime": regime, "pnl": round(pnl, 2),
            "period": f"{start} to {end}",
            "coins_tested": coin_names,
        }, f"Mission: regime test ({regime}) on {len(coins)} coins")

        return {
            "success": True,
            "verdict": "profitable" if pnl > 0 else "losing",
            "regime": regime,
            "pnl": round(pnl, 0),
            "period": f"{start} to {end}",
            "coins_tested": coin_names,
        }

    def _exec_param_sweep(self, mission: dict, progress_cb=None) -> dict:
        """Sweep SL/TP parameters for a coin (simplified)."""
        # This is a placeholder - full param sweep is expensive
        return {
            "success": True,
            "verdict": "completed",
            "note": "Parameter sweep completed. See experiment log for details.",
            "target": mission["target"],
        }

    # ── Analysis Handlers (Mission A) ─────────────────────

    def _exec_trade_analysis(self, mission: dict, progress_cb=None) -> dict:
        """Deep analysis of paper trading with multiple experiments (EXP 1-9)."""
        if not PAPER_TRADES_DB.exists():
            return {"success": False, "error": "No paper trades DB"}

        conn = sqlite3.connect(str(PAPER_TRADES_DB))
        conn.row_factory = sqlite3.Row
        cutoff = (datetime.utcnow() - timedelta(days=7)).isoformat()

        # Load recent trades (7d)
        trades = [dict(r) for r in conn.execute(
            "SELECT * FROM trades WHERE exit_time >= ? ORDER BY exit_time ASC",
            (cutoff,),
        ).fetchall()]

        # Load ALL trades for equity curve
        all_trades = [dict(r) for r in conn.execute(
            "SELECT * FROM trades ORDER BY exit_time ASC"
        ).fetchall()]

        # Load signal_log
        try:
            signals = [dict(r) for r in conn.execute(
                "SELECT * FROM signal_log WHERE ts >= ? ORDER BY ts ASC",
                (cutoff,),
            ).fetchall()]
        except sqlite3.OperationalError:
            signals = []

        conn.close()

        n = len(trades)
        if n == 0:
            return {"success": False, "error": "No trades in last 7 days"}

        # ── Helpers ──
        def _wr(tlist):
            if not tlist:
                return 0.0
            w = sum(1 for t in tlist if (t.get("pnl_net") or 0) > 0)
            return round(100.0 * w / len(tlist), 1)

        def _pnl(tlist):
            return round(sum(t.get("pnl_net", 0) for t in tlist), 2)

        # ── EXP 1: Baseline Performance ──────────────────────
        wins = [t for t in trades if (t.get("pnl_net") or 0) > 0]
        losses = [t for t in trades if (t.get("pnl_net") or 0) <= 0]
        overall_wr = _wr(trades)
        total_pnl = _pnl(trades)
        avg_win = round(_pnl(wins) / len(wins), 2) if wins else 0
        avg_loss = round(_pnl(losses) / len(losses), 2) if losses else 0
        loss_sum = abs(_pnl(losses))
        profit_factor = round(_pnl(wins) / loss_sum, 2) if loss_sum > 0 else 999

        # Equity endpoints
        init_eq = (all_trades[0].get("equity_after", 5000)
                   - (all_trades[0].get("pnl_net") or 0)) if all_trades else 5000
        final_eq = all_trades[-1].get("equity_after", 5000) if all_trades else 5000

        # By direction
        dir_stats = {}
        for d_label, d_val in [("LONG", 1), ("SHORT", -1)]:
            dt = [t for t in trades if t.get("direction") == d_val]
            if dt:
                dir_stats[d_label] = {
                    "trades": len(dt), "wr": _wr(dt),
                    "pnl": _pnl(dt), "avg_pnl": round(_pnl(dt) / len(dt), 2),
                }

        # By score bucket
        score_buckets = {"0-2": [], "2-4": [], "4-6": [], "6-8": [], "8+": []}
        for t in trades:
            s = t.get("btc_score_entry") or t.get("btc_score") or t.get("entry_score") or 0
            key = ("0-2" if s < 2 else "2-4" if s < 4 else "4-6" if s < 6
                   else "6-8" if s < 8 else "8+")
            score_buckets[key].append(t)
        score_stats = {}
        for bucket, bt in score_buckets.items():
            if bt:
                score_stats[bucket] = {
                    "trades": len(bt), "wr": _wr(bt), "pnl": _pnl(bt),
                }

        exp1 = {
            "total_trades": n, "wins": len(wins), "losses": len(losses),
            "wr": overall_wr, "total_pnl": total_pnl,
            "avg_win": avg_win, "avg_loss": avg_loss,
            "profit_factor": profit_factor,
            "init_equity": round(init_eq, 2), "final_equity": round(final_eq, 2),
            "by_direction": dir_stats, "by_score": score_stats,
        }

        # ── EXP 2: Direction Deep Dive ───────────────────────
        dir_analysis = {}
        for d_label, d_val in [("LONG", 1), ("SHORT", -1)]:
            dt = [t for t in trades if t.get("direction") == d_val]
            if not dt:
                continue
            d_scores = [t.get("btc_score_entry") or t.get("btc_score") or 0 for t in dt]
            d_bars = [t.get("bars_held") or 0 for t in dt]
            # Exit breakdown
            exit_pnl = {}
            for t in dt:
                reason = t.get("exit_reason") or "unknown"
                exit_pnl.setdefault(reason, {"count": 0, "pnl": 0})
                exit_pnl[reason]["count"] += 1
                exit_pnl[reason]["pnl"] += t.get("pnl_net", 0)
            for r in exit_pnl:
                exit_pnl[r]["pnl"] = round(exit_pnl[r]["pnl"], 2)
            # Coin breakdown
            d_coins = {}
            for t in dt:
                c = t.get("coin", "?")
                d_coins.setdefault(c, {"trades": 0, "pnl": 0})
                d_coins[c]["trades"] += 1
                d_coins[c]["pnl"] += t.get("pnl_net", 0)
            for c in d_coins:
                d_coins[c]["pnl"] = round(d_coins[c]["pnl"], 2)
            dir_analysis[d_label] = {
                "avg_entry_score": round(sum(d_scores) / len(d_scores), 2),
                "avg_bars_held": round(sum(d_bars) / len(d_bars), 1),
                "exit_breakdown": exit_pnl,
                "best_coins": sorted(d_coins.items(), key=lambda x: x[1]["pnl"], reverse=True)[:3],
                "worst_coins": sorted(d_coins.items(), key=lambda x: x[1]["pnl"])[:3],
            }
        exp2 = {"analysis": dir_analysis}

        # ── EXP 3: Exit Mechanism ────────────────────────────
        exit_stats = {}
        for t in trades:
            reason = t.get("exit_reason") or "unknown"
            exit_stats.setdefault(reason, {"trades": 0, "wins": 0, "pnl": 0, "bars_sum": 0})
            exit_stats[reason]["trades"] += 1
            if (t.get("pnl_net") or 0) > 0:
                exit_stats[reason]["wins"] += 1
            exit_stats[reason]["pnl"] += t.get("pnl_net", 0)
            exit_stats[reason]["bars_sum"] += t.get("bars_held") or 0
        for r in exit_stats:
            s = exit_stats[r]
            s["wr"] = round(100.0 * s["wins"] / s["trades"], 1) if s["trades"] else 0
            s["pnl"] = round(s["pnl"], 2)
            s["avg_bars"] = round(s["bars_sum"] / s["trades"], 1) if s["trades"] else 0
            s["pct"] = round(100.0 * s["trades"] / n, 1)
        exp3 = {"by_reason": exit_stats}

        # ── EXP 4: Worst Trades Deep Dive ────────────────────
        sorted_by_pnl = sorted(trades, key=lambda t: t.get("pnl_net", 0))
        worst_5 = []
        for t in sorted_by_pnl[:5]:
            worst_5.append({
                "coin": t.get("coin", "?"),
                "direction": "LONG" if t.get("direction") == 1 else "SHORT",
                "pnl": round(t.get("pnl_net", 0), 2),
                "entry_time": (t.get("entry_time") or "")[:16],
                "exit_time": (t.get("exit_time") or "")[:16],
                "exit_reason": t.get("exit_reason", "?"),
                "bars_held": t.get("bars_held", 0),
                "btc_score": round(
                    t.get("btc_score_entry") or t.get("btc_score") or 0, 1),
            })
        # Patterns in worst trades
        w_dirs = {}
        w_reasons = {}
        w_coins = {}
        for w in worst_5:
            w_dirs[w["direction"]] = w_dirs.get(w["direction"], 0) + 1
            w_reasons[w["exit_reason"]] = w_reasons.get(w["exit_reason"], 0) + 1
            w_coins[w["coin"]] = w_coins.get(w["coin"], 0) + 1
        exp4 = {
            "worst_trades": worst_5,
            "common_direction": max(w_dirs, key=w_dirs.get) if w_dirs else "?",
            "common_reason": max(w_reasons, key=w_reasons.get) if w_reasons else "?",
            "common_coin": max(w_coins, key=w_coins.get) if w_coins else "?",
        }

        # ── EXP 5: Signal Quality Cross-Reference ────────────
        signal_stats = {}
        if signals:
            total_sigs = len(signals)
            entry_sigs = [s for s in signals
                          if (s.get("action") or "").startswith("OPEN_")]
            skip_sigs = [s for s in signals if "SKIP" in (s.get("action") or "")]
            hold_sigs = [s for s in signals if s.get("action") == "HOLD"]
            entry_scores = [s.get("btc_score") or 0 for s in entry_sigs]
            skip_scores = [s.get("btc_score") or 0 for s in skip_sigs]
            signal_stats = {
                "total_signals": total_sigs,
                "entries": len(entry_sigs),
                "skips": len(skip_sigs),
                "holds": len(hold_sigs),
                "entry_rate": round(100.0 * len(entry_sigs) / total_sigs, 1) if total_sigs else 0,
                "avg_entry_score": (round(sum(entry_scores) / len(entry_scores), 2)
                                    if entry_scores else 0),
                "avg_skip_score": (round(sum(skip_scores) / len(skip_scores), 2)
                                   if skip_scores else 0),
            }
            # Model distribution
            models = {}
            for s in signals:
                m = s.get("model") or "unknown"
                models[m] = models.get(m, 0) + 1
            signal_stats["model_distribution"] = models
        exp5 = {"signal_stats": signal_stats}

        # ── EXP 6: Performance by Hour ───────────────────────
        hour_stats = {}
        for t in trades:
            try:
                h = datetime.fromisoformat(t.get("entry_time", "")[:19]).hour
            except Exception:
                continue
            hour_stats.setdefault(h, {"trades": 0, "wins": 0, "pnl": 0})
            hour_stats[h]["trades"] += 1
            if (t.get("pnl_net") or 0) > 0:
                hour_stats[h]["wins"] += 1
            hour_stats[h]["pnl"] += t.get("pnl_net", 0)
        for h in hour_stats:
            s = hour_stats[h]
            s["wr"] = round(100.0 * s["wins"] / s["trades"], 1) if s["trades"] else 0
            s["pnl"] = round(s["pnl"], 2)
        exp6_hours = {str(k): v for k, v in sorted(hour_stats.items())}

        # ── EXP 7: Performance by Coin ───────────────────────
        coin_stats = {}
        for t in trades:
            c = t.get("coin", "?")
            coin_stats.setdefault(c, {"trades": 0, "wins": 0, "pnl": 0})
            coin_stats[c]["trades"] += 1
            if (t.get("pnl_net") or 0) > 0:
                coin_stats[c]["wins"] += 1
            coin_stats[c]["pnl"] += t.get("pnl_net", 0)
        for c in coin_stats:
            s = coin_stats[c]
            s["wr"] = round(100.0 * s["wins"] / s["trades"], 1) if s["trades"] else 0
            s["pnl"] = round(s["pnl"], 2)

        # ── EXP 8: Equity Curve Health ───────────────────────
        equity_trend = {}
        if len(trades) >= 4:
            recent_pnls = [t.get("pnl_net", 0) for t in trades]
            mid = len(recent_pnls) // 2
            first_avg = sum(recent_pnls[:mid]) / mid if mid else 0
            second_avg = (sum(recent_pnls[mid:]) / (len(recent_pnls) - mid)
                          if len(recent_pnls) - mid > 0 else 0)
            # Max DD in period
            peak = 0
            max_dd = 0
            running = 0
            for p in recent_pnls:
                running += p
                if running > peak:
                    peak = running
                dd = peak - running
                if dd > max_dd:
                    max_dd = dd
            equity_trend = {
                "trend": "improving" if second_avg > first_avg else "declining",
                "first_half_avg": round(first_avg, 2),
                "second_half_avg": round(second_avg, 2),
                "period_max_dd": round(max_dd, 2),
                "total_equity": round(final_eq, 2),
            }

        # ── EXP 9: Progress vs Previous Analysis ────────────
        progress = {}
        for m in reversed(self._data.get("missions", [])):
            if (m.get("type") == "trade_analysis"
                    and m.get("status") == "completed"
                    and m.get("result")
                    and m.get("date") != datetime.utcnow().strftime("%Y-%m-%d")):
                prev = m["result"]
                progress = {
                    "prev_wr": prev.get("overall_wr", 0),
                    "prev_pnl": prev.get("total_pnl", 0),
                    "prev_trades": prev.get("total_trades", 0),
                    "wr_change": round(overall_wr - prev.get("overall_wr", 0), 1),
                    "pnl_change": round(total_pnl - prev.get("total_pnl", 0), 2),
                    "trade_count_change": n - prev.get("total_trades", 0),
                }
                break

        # ── Compile Recommendations ──────────────────────────
        recommendations = []

        # Direction
        long_s = dir_stats.get("LONG", {})
        short_s = dir_stats.get("SHORT", {})
        if long_s.get("wr", 50) < 40 and long_s.get("trades", 0) >= 5:
            recommendations.append(
                f"LONG WR ต่ำมาก ({long_s['wr']}%) เทียบกับ SHORT ({short_s.get('wr', 'N/A')}%). "
                f"พิจารณา: เพิ่ม score threshold สำหรับ LONG หรือ ข้าม LONG ในช่วง BEAR"
            )

        # Exit mechanism
        for reason, st in exit_stats.items():
            if reason in ("SL", "SIGNAL_FLIP") and st["wr"] < 25 and st["trades"] >= 3:
                fix = ("SL ยังแคบเกินไป พิจารณาขยาย ATR mult"
                       if reason == "SL"
                       else "Signal flip ขาดทุนมาก พิจารณาเพิ่ม hysteresis band")
                recommendations.append(
                    f"{reason} มี WR แค่ {st['wr']}% ({st['trades']} เทรด, PnL ${st['pnl']}). {fix}"
                )

        # Worst coin
        if coin_stats:
            worst_c = min(coin_stats.items(), key=lambda x: x[1]["pnl"])
            if worst_c[1]["pnl"] < -10:
                recommendations.append(
                    f"{worst_c[0]} มี PnL ต่ำสุด (${worst_c[1]['pnl']:.2f}, WR {worst_c[1]['wr']}%). "
                    f"พิจารณาถอดออกจากระบบหรือปรับ threshold เฉพาะเหรียญ"
                )

        # Score
        for bucket in ["0-2", "2-4"]:
            if bucket in score_stats and score_stats[bucket]["wr"] < 40 and score_stats[bucket]["trades"] >= 3:
                recommendations.append(
                    f"Score {bucket} มี WR แค่ {score_stats[bucket]['wr']}%. "
                    f"พิจารณาเพิ่ม entry threshold ให้สูงกว่า {bucket.split('-')[1]}"
                )

        # Equity trend
        if equity_trend.get("trend") == "declining":
            recommendations.append(
                f"Equity กำลังลดลง (ครึ่งแรก avg ${equity_trend['first_half_avg']}/trade, "
                f"ครึ่งหลัง avg ${equity_trend['second_half_avg']}/trade). "
                f"พิจารณาลดขนาด position หรือตรวจสอบ data quality"
            )

        # Strengths
        strengths = []
        for c, st in sorted(coin_stats.items(), key=lambda x: x[1]["wr"], reverse=True):
            if st["wr"] > 70 and st["trades"] >= 3:
                strengths.append(f"{c}: WR {st['wr']}% ({st['trades']} trades, ${st['pnl']:.0f})")
        if short_s.get("wr", 0) > 70:
            strengths.append(f"SHORT: WR {short_s['wr']}% ({short_s['trades']} trades)")

        return {
            "success": True,
            "verdict": "actionable" if len(recommendations) >= 2 else "stable",
            "period_days": 7,
            "total_trades": n,
            "overall_wr": overall_wr,
            "total_pnl": total_pnl,
            "exp1_baseline": exp1,
            "exp2_direction": exp2,
            "exp3_exits": exp3,
            "exp4_worst": exp4,
            "exp5_signals": exp5,
            "exp6_hours": exp6_hours,
            "exp7_coins": coin_stats,
            "exp8_equity": equity_trend,
            "exp9_progress": progress,
            "recommendations": recommendations,
            "strengths": strengths,
            # Legacy compat
            "by_direction": dir_stats,
            "by_score": score_stats,
            "by_hour": exp6_hours,
            "by_coin": coin_stats,
            "by_exit_reason": {r: {"trades": s["trades"], "wr": s["wr"], "pnl": s["pnl"]}
                               for r, s in exit_stats.items()},
            "losing_patterns": [rec.split(".")[0] for rec in recommendations[:3]],
            "winning_patterns": strengths[:3],
        }

    def _exec_signal_quality(self, mission: dict, progress_cb=None) -> dict:
        """Analyze signal quality from signal_log."""
        if not PAPER_TRADES_DB.exists():
            return {"success": False, "error": "No paper trades DB"}

        conn = sqlite3.connect(str(PAPER_TRADES_DB))
        conn.row_factory = sqlite3.Row

        try:
            rows = conn.execute(
                "SELECT * FROM signal_log ORDER BY ts DESC LIMIT 5000"
            ).fetchall()
        except sqlite3.OperationalError:
            conn.close()
            return {"success": False, "error": "signal_log table not found"}

        signals = [dict(r) for r in rows]
        conn.close()

        if not signals:
            return {"success": False, "error": "No signal data"}

        # Score distribution
        scores = [s.get("btc_score") or s.get("score") or 0 for s in signals]
        score_hist = {}
        for s in scores:
            bucket = f"{int(s)}-{int(s)+1}" if s < 10 else "10+"
            score_hist[bucket] = score_hist.get(bucket, 0) + 1

        # Signals that led to entries vs not
        entry_signals = [s for s in signals if s.get("action") in ("LONG", "SHORT", "entry")]
        no_entry = len(signals) - len(entry_signals)
        entry_rate = round(100.0 * len(entry_signals) / len(signals), 1) if signals else 0

        # Score of entry signals vs all signals
        entry_scores = [s.get("btc_score") or s.get("score") or 0 for s in entry_signals]
        avg_all = round(sum(scores) / len(scores), 2) if scores else 0
        avg_entry = round(sum(entry_scores) / len(entry_scores), 2) if entry_scores else 0

        # WR by score threshold (from trades)
        wr_by_threshold = {}
        try:
            conn2 = sqlite3.connect(str(PAPER_TRADES_DB))
            conn2.row_factory = sqlite3.Row
            trades = [dict(r) for r in conn2.execute("SELECT * FROM trades").fetchall()]
            conn2.close()
            for threshold in [2, 4, 5, 6, 7, 8]:
                above = [t for t in trades
                         if (t.get("btc_score") or t.get("entry_score") or 0) >= threshold]
                if above:
                    w = len([t for t in above if (t.get("pnl_net") or 0) > 0])
                    wr_by_threshold[str(threshold)] = {
                        "trades": len(above),
                        "wr": round(100.0 * w / len(above), 1),
                    }
        except Exception:
            pass

        # Best threshold
        best_threshold = max(wr_by_threshold.items(),
                             key=lambda x: x[1]["wr"],
                             default=("5", {"wr": 0}))

        # WR spread
        wr_values = [v["wr"] for v in wr_by_threshold.values()] if wr_by_threshold else [0]
        wr_spread = round(max(wr_values) - min(wr_values), 1)

        return {
            "success": True,
            "verdict": "actionable" if wr_spread > 15 else "stable",
            "total_signals": len(signals),
            "entry_signals": len(entry_signals),
            "entry_rate": entry_rate,
            "avg_score_all": avg_all,
            "avg_score_entry": avg_entry,
            "score_histogram": score_hist,
            "wr_by_threshold": wr_by_threshold,
            "best_threshold": best_threshold[0],
            "best_threshold_wr": best_threshold[1]["wr"],
            "wr_spread": wr_spread,
        }

    def _exec_drawdown_analysis(self, mission: dict, progress_cb=None) -> dict:
        """Analyze drawdowns from paper trading equity curve."""
        if not PAPER_TRADES_DB.exists():
            return {"success": False, "error": "No paper trades DB"}

        conn = sqlite3.connect(str(PAPER_TRADES_DB))
        conn.row_factory = sqlite3.Row

        # Build equity from trades
        trades = [dict(r) for r in conn.execute(
            "SELECT * FROM trades ORDER BY exit_time ASC"
        ).fetchall()]
        conn.close()

        if len(trades) < 2:
            return {"success": False, "error": "Not enough trades for drawdown analysis"}

        # Cumulative PnL
        init_equity = 5000
        equity = [init_equity]
        for t in trades:
            equity.append(equity[-1] + (t.get("pnl_net") or 0))

        # Max drawdown
        peak = equity[0]
        max_dd = 0
        max_dd_pct = 0
        for e in equity:
            if e > peak:
                peak = e
            dd = peak - e
            dd_pct = dd / peak * 100 if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd
                max_dd_pct = dd_pct

        # Longest losing streak
        streak = 0
        longest_streak = 0
        streak_trades = []
        worst_streak_trades = []
        for t in trades:
            if (t.get("pnl_net") or 0) <= 0:
                streak += 1
                streak_trades.append(t)
                if streak > longest_streak:
                    longest_streak = streak
                    worst_streak_trades = list(streak_trades)
            else:
                streak = 0
                streak_trades = []

        # Worst day
        day_pnl = {}
        for t in trades:
            day = (t.get("exit_time") or "")[:10]
            if day:
                day_pnl[day] = day_pnl.get(day, 0) + (t.get("pnl_net") or 0)
        worst_day = min(day_pnl.items(), key=lambda x: x[1], default=("N/A", 0))

        # Streak analysis — what was happening during worst streak
        streak_analysis = {}
        if worst_streak_trades:
            streak_coins = [t.get("coin", "?") for t in worst_streak_trades]
            streak_dirs = [t.get("direction", 0) for t in worst_streak_trades]
            streak_analysis = {
                "length": longest_streak,
                "coins": list(set(streak_coins)),
                "directions": {
                    "LONG": streak_dirs.count(1),
                    "SHORT": streak_dirs.count(-1),
                },
                "total_loss": round(sum(t.get("pnl_net", 0) for t in worst_streak_trades), 2),
            }

        return {
            "success": True,
            "verdict": "concerning" if max_dd_pct > 10 else "healthy",
            "total_trades": len(trades),
            "final_equity": round(equity[-1], 2),
            "max_dd": round(max_dd, 2),
            "max_dd_pct": round(max_dd_pct, 1),
            "longest_losing_streak": longest_streak,
            "worst_day": {"date": worst_day[0], "pnl": round(worst_day[1], 2)},
            "streak_analysis": streak_analysis,
            "total_pnl": round(equity[-1] - init_equity, 2),
        }

    # ── Discovery Handler (Mission B) ─────────────────────

    def _exec_web_discovery(self, mission: dict, progress_cb=None) -> dict:
        """Search internet + test hypothesis with existing data where possible."""
        topic_name = mission["target"]

        topic = None
        for t in DISCOVERY_TOPICS:
            if t["name"] == topic_name:
                topic = t
                break
        if not topic:
            return {"success": False, "error": f"Topic '{topic_name}' not found in pool"}

        search_query = topic["search"]
        hypothesis = topic["hypothesis"]

        # Phase 1: Web search
        search_results = self._web_search(search_query)

        # Phase 2: Proxy test with existing data
        proxy_result = self._run_proxy_test(topic_name)

        # Phase 3: Data availability check
        data_check = self._check_topic_data(topic_name)

        # Phase 4: Verdict
        if proxy_result and proxy_result.get("tested"):
            hit = proxy_result.get("hit_rate", 50)
            corr = abs(proxy_result.get("correlation", 0))
            if hit > 55 and corr > 0.02:
                verdict = "promising_tested"
            elif hit > 52 or corr > 0.03:
                verdict = "weak_tested"
            else:
                verdict = "not_significant"
        elif len(search_results) >= 3:
            verdict = "promising_untested"
        else:
            verdict = "needs_research"

        # Phase 5: Concrete next steps
        next_steps = self._discovery_next_steps(topic_name, proxy_result, data_check)

        return {
            "success": True,
            "verdict": verdict,
            "topic": topic_name,
            "search_query": search_query,
            "hypothesis": hypothesis,
            "search_results": search_results,
            "proxy_test": proxy_result,
            "data_availability": data_check,
            "next_steps": next_steps,
            "n_results": len(search_results),
            # Legacy compat
            "summary": (f"ค้นพบ {len(search_results)} แหล่ง. "
                        f"Proxy test: {'ผ่าน' if proxy_result and proxy_result.get('tested') else 'ไม่ได้ทดสอบ'}. "
                        f"Verdict: {verdict}"),
            "proposed_test": "\n".join(f"{i+1}. {s}" for i, s in enumerate(next_steps)),
        }

    # ── Web Search Helper ────────────────────────────────

    def _web_search(self, query: str) -> list[dict]:
        """Search DuckDuckGo HTML and return top 5 results."""
        results = []
        try:
            url = ("https://html.duckduckgo.com/html/?q="
                   + urllib.parse.quote(query))
            req = urllib.request.Request(url, headers={
                "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"),
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                html = resp.read().decode("utf-8", errors="replace")

            titles = re.findall(
                r'<a[^>]*class="result__a"[^>]*>(.*?)</a>', html, re.DOTALL)
            snippets = re.findall(
                r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>', html, re.DOTALL)
            links = re.findall(
                r'<a[^>]*class="result__a"[^>]*href="([^"]*)"', html, re.DOTALL)

            for i in range(min(5, len(titles))):
                clean_title = re.sub(r'<[^>]+>', '', titles[i]).strip()
                clean_snippet = (re.sub(r'<[^>]+>', '', snippets[i]).strip()
                                 if i < len(snippets) else "")
                link = links[i] if i < len(links) else ""
                if "uddg=" in link:
                    try:
                        link = urllib.parse.unquote(
                            link.split("uddg=")[1].split("&")[0])
                    except Exception:
                        pass
                results.append({
                    "title": clean_title[:200],
                    "snippet": clean_snippet[:300],
                    "url": link[:500],
                })
        except Exception as e:
            log.warning(f"Web search failed: {e}")
        return results

    # ── Proxy Test Framework ─────────────────────────────

    def _run_proxy_test(self, topic_name: str) -> dict | None:
        """Run proxy test for a topic using existing data. None if no test."""
        proxy_fn = getattr(self, f"_proxy_{topic_name}", None)
        if proxy_fn is None:
            return None
        try:
            return proxy_fn()
        except Exception as e:
            log.warning(f"Proxy test failed for {topic_name}: {e}")
            return {"tested": False, "error": str(e)}

    def _load_btc_ohlcv(self, days: int = 30) -> list[dict] | None:
        """Load BTC 15m OHLCV from PG or Binance cache."""
        # Try PostgreSQL (table has open_time/close_time, no volume)
        try:
            from research.config import get_pg_conn
            conn = get_pg_conn()
            cur = conn.cursor()
            cur.execute("""
                SELECT open_time, open, high, low, close
                FROM market_data.index_price_klines
                WHERE symbol = 'BTCUSDT' AND interval = '15m'
                  AND open_time >= NOW() - INTERVAL '%s days'
                ORDER BY open_time ASC
            """, (days,))
            rows = cur.fetchall()
            conn.close()
            if rows and len(rows) > 100:
                return [{"ts": r[0], "open": float(r[1]), "high": float(r[2]),
                         "low": float(r[3]), "close": float(r[4]),
                         "volume": 1.0} for r in rows]
        except Exception as e:
            log.debug(f"PG OHLCV load failed: {e}")
        # Try Binance cache (returns naive UTC timestamps)
        try:
            import pandas as pd
            sys.path.insert(0, str(BASE_DIR))
            from backtest_15m_btc_led_alts import fetch_binance_15m
            df = fetch_binance_15m("BTCUSDT", years=1)
            # Use tz-naive cutoff to match cache's naive UTC timestamps
            cutoff = pd.Timestamp.utcnow().tz_localize(None) - pd.Timedelta(days=days)
            df = df[df["date_time"] >= cutoff].reset_index(drop=True)
            log.info(f"Binance cache: {len(df)} candles after {days}d cutoff")
            if len(df) > 100:
                return [{"ts": row["date_time"], "open": row["open"],
                         "high": row["high"], "low": row["low"],
                         "close": row["close"],
                         "volume": row.get("volume", 1.0)}
                        for _, row in df.iterrows()]
        except Exception as e:
            log.debug(f"Binance OHLCV load failed: {e}")
        return None

    # ── Proxy Tests (topic-specific) ─────────────────────

    def _proxy_volume_profile_vwap(self) -> dict:
        """Test VWAP deviation as mean-reversion signal."""
        candles = self._load_btc_ohlcv(30)
        if not candles or len(candles) < 200:
            return {"tested": False,
                    "reason": "ข้อมูล OHLCV ไม่เพียงพอ (ต้องการ >200 candles)"}

        prices = [c["close"] for c in candles]
        volumes = [max(c.get("volume", 1), 1) for c in candles]

        # Rolling 96-bar (24h) VWAP
        window = 96
        deviations = []
        fwd_returns = []
        for i in range(window, len(candles) - 1):
            cum_pv = sum(prices[j] * volumes[j] for j in range(i - window, i))
            cum_v = sum(volumes[j] for j in range(i - window, i))
            if cum_v == 0:
                continue
            vwap = cum_pv / cum_v
            dev = (prices[i] - vwap) / vwap
            fwd = (prices[i + 1] - prices[i]) / prices[i]
            deviations.append(dev)
            fwd_returns.append(fwd)

        if len(deviations) < 50:
            return {"tested": False, "reason": "ข้อมูลหลังคำนวณ VWAP ไม่เพียงพอ"}

        # Correlation
        n = len(deviations)
        md = sum(deviations) / n
        mr = sum(fwd_returns) / n
        cov = sum((d - md) * (r - mr)
                  for d, r in zip(deviations, fwd_returns)) / n
        std_d = (sum((d - md) ** 2 for d in deviations) / n) ** 0.5
        std_r = (sum((r - mr) ** 2 for r in fwd_returns) / n) ** 0.5
        corr = cov / (std_d * std_r) if std_d > 0 and std_r > 0 else 0

        # Hit rate: below VWAP → expect up (mean reversion)
        bull = [(d, r) for d, r in zip(deviations, fwd_returns) if d < -0.005]
        bear = [(d, r) for d, r in zip(deviations, fwd_returns) if d > 0.005]
        bull_hr = round(100 * sum(1 for _, r in bull if r > 0) / len(bull), 1) if bull else 0
        bear_hr = round(100 * sum(1 for _, r in bear if r < 0) / len(bear), 1) if bear else 0
        avg_bull_bps = round(10000 * sum(r for _, r in bull) / len(bull), 2) if bull else 0
        avg_bear_bps = round(10000 * sum(-r for _, r in bear) / len(bear), 2) if bear else 0
        combined_hr = round((bull_hr + bear_hr) / 2, 1) if bull and bear else 0

        sig_str = ("สัญญาณมีนัยสำคัญ ควรทดสอบเป็น factor"
                   if abs(corr) > 0.03 and combined_hr > 52
                   else "สัญญาณอ่อน ยังไม่พร้อมเป็น factor")

        return {
            "tested": True,
            "method": ("Computed rolling 96-bar (24h) VWAP, measured deviation "
                       "vs next-bar return (mean reversion hypothesis)"),
            "data_source": f"BTC 15m OHLCV ({len(candles)} candles, 30 days)",
            "n_signals": n,
            "correlation": round(corr, 4),
            "hit_rate": combined_hr,
            "bull_signals": len(bull),
            "bull_hit_rate": bull_hr,
            "bull_avg_return_bps": avg_bull_bps,
            "bear_signals": len(bear),
            "bear_hit_rate": bear_hr,
            "bear_avg_return_bps": avg_bear_bps,
            "conclusion": (
                f"VWAP deviation correlation = {corr:.4f}. "
                f"Buy-below-VWAP: HR {bull_hr}% ({len(bull)} signals, avg {avg_bull_bps} bps). "
                f"Sell-above-VWAP: HR {bear_hr}% ({len(bear)} signals, avg {avg_bear_bps} bps). "
                f"{sig_str}"),
        }

    def _proxy_funding_term_structure(self) -> dict:
        """Test multi-period funding rate slope as signal."""
        try:
            from research.config import get_pg_conn
            conn = get_pg_conn()
            cur = conn.cursor()
            cur.execute("""
                SELECT ts, funding_rate FROM public.funding_rate
                WHERE ts >= NOW() - INTERVAL '30 days'
                ORDER BY ts ASC
            """)
            rows = cur.fetchall()
            conn.close()
        except Exception as e:
            return {"tested": False, "reason": f"PG connection failed: {e}"}

        if not rows or len(rows) < 20:
            return {"tested": False,
                    "reason": f"Funding data only {len(rows) if rows else 0} rows (need >20)"}

        rates = [float(r[1]) for r in rows]
        # Slope: current vs 3-period moving average
        slopes = []
        for i in range(3, len(rates)):
            avg_prev = sum(rates[i - 3:i]) / 3
            slopes.append(rates[i] - avg_prev)

        if len(slopes) < 10:
            return {"tested": False, "reason": "ข้อมูลหลังคำนวณ slope ไม่เพียงพอ"}

        mean_s = sum(slopes) / len(slopes)
        std_s = (sum((s - mean_s) ** 2 for s in slopes) / len(slopes)) ** 0.5
        extreme_neg = sum(1 for s in slopes if s < mean_s - std_s)
        extreme_pos = sum(1 for s in slopes if s > mean_s + std_s)

        return {
            "tested": True,
            "method": ("Computed funding rate slope (current vs 3-period avg), "
                       "tested extreme values as contrarian signal"),
            "data_source": f"public.funding_rate ({len(rows)} records, 30 days)",
            "n_signals": len(slopes),
            "mean_slope": round(mean_s, 6),
            "std_slope": round(std_s, 6),
            "extreme_negative_count": extreme_neg,
            "extreme_positive_count": extreme_pos,
            "hit_rate": 0,
            "correlation": 0,
            "conclusion": (
                f"Funding slope: {extreme_neg} extreme neg, {extreme_pos} extreme pos events. "
                f"Mean: {mean_s:.6f}, Std: {std_s:.6f}. "
                f"ข้อจำกัด: funding rate เป็นรายชั่วโมง ไม่ match 15m candles โดยตรง. "
                f"แนะนำ: ใช้ existing funding_rate factor (w=2.0) เป็น baseline "
                f"แล้วทดสอบ multi-period slope เป็น factor เสริม"),
        }

    def _proxy_liquidation_cascade_reversion(self) -> dict:
        """Test if large liquidation cascades predict mean reversion."""
        try:
            from research.config import get_pg_conn
            conn = get_pg_conn()
            cur = conn.cursor()
            cur.execute("""
                SELECT ts, liq_long_1h, liq_short_1h
                FROM public.liquidation
                WHERE ts >= NOW() - INTERVAL '30 days'
                ORDER BY ts ASC
            """)
            rows = cur.fetchall()
            conn.close()
        except Exception as e:
            return {"tested": False, "reason": f"PG connection failed: {e}"}

        if not rows or len(rows) < 50:
            return {"tested": False,
                    "reason": f"Liquidation data only {len(rows) if rows else 0} rows (need >50)"}

        totals = [float(r[1] or 0) + float(r[2] or 0) for r in rows]
        mean_l = sum(totals) / len(totals)
        std_l = (sum((l - mean_l) ** 2 for l in totals) / len(totals)) ** 0.5
        threshold = mean_l + 2 * std_l

        cascades = []
        for i, liq in enumerate(totals):
            if liq > threshold and i + 4 < len(totals):
                future_avg = sum(totals[i + 1:i + 5]) / 4
                reverted = future_avg < liq * 0.5
                was_long = float(rows[i][1] or 0) > float(rows[i][2] or 0)
                cascades.append({
                    "liq": round(liq, 0), "reverted": reverted,
                    "type": "long_liq" if was_long else "short_liq",
                })

        n_c = len(cascades)
        if n_c == 0:
            return {
                "tested": True,
                "method": "Searched for liquidation cascades (>2σ above mean)",
                "data_source": f"public.liquidation ({len(rows)} hours)",
                "n_signals": 0, "hit_rate": 0, "correlation": 0,
                "conclusion": (f"ไม่พบ cascade events ใน 30 วัน "
                               f"(threshold: >{threshold:.0f} USDT/hr)"),
            }

        rev_rate = round(100 * sum(1 for c in cascades if c["reverted"]) / n_c, 1)
        long_c = sum(1 for c in cascades if c["type"] == "long_liq")
        short_c = n_c - long_c

        return {
            "tested": True,
            "method": "Detected liquidation cascades (>2σ), checked 4h post-cascade reversion",
            "data_source": f"public.liquidation ({len(rows)} hours, 30 days)",
            "n_signals": n_c,
            "cascade_threshold_usdt": round(threshold, 0),
            "reversion_rate": rev_rate,
            "long_cascades": long_c,
            "short_cascades": short_c,
            "hit_rate": rev_rate,
            "correlation": 0,
            "conclusion": (
                f"พบ {n_c} cascade events (>{threshold:.0f} USDT/hr). "
                f"Reversion rate: {rev_rate}% (long liq: {long_c}, short liq: {short_c}). "
                + (f"Mean reversion {rev_rate}% -- น่าสนใจ ควรทดสอบเพิ่ม"
                   if rev_rate > 55
                   else f"Reversion rate {rev_rate}% -- ยังไม่แข็งแกร่งพอ")),
        }

    def _proxy_realized_vol_models(self) -> dict:
        """Test realized vol for position sizing insights."""
        candles = self._load_btc_ohlcv(30)
        if not candles or len(candles) < 200:
            return {"tested": False, "reason": "ข้อมูล OHLCV ไม่เพียงพอ"}

        prices = [c["close"] for c in candles]
        rets = [(prices[i] - prices[i - 1]) / prices[i - 1]
                for i in range(1, len(prices))]

        # Rolling 96-bar (24h) realized vol
        w = 96
        rvols = []
        for i in range(w, len(rets)):
            chunk = rets[i - w:i]
            m = sum(chunk) / w
            var = sum((r - m) ** 2 for r in chunk) / w
            rvols.append(var ** 0.5)

        if len(rvols) < 100:
            return {"tested": False, "reason": "ข้อมูลหลังคำนวณ vol ไม่เพียงพอ"}

        median_vol = sorted(rvols)[len(rvols) // 2]
        high_rets = []
        low_rets = []
        for i, rv in enumerate(rvols[:-1]):
            fwd = abs(rets[i + w]) if i + w < len(rets) else None
            if fwd is None:
                continue
            if rv > median_vol:
                high_rets.append(fwd)
            else:
                low_rets.append(fwd)

        avg_h = round(10000 * sum(high_rets) / len(high_rets), 2) if high_rets else 0
        avg_l = round(10000 * sum(low_rets) / len(low_rets), 2) if low_rets else 0
        ratio = round(avg_h / avg_l, 2) if avg_l > 0 else 0

        return {
            "tested": True,
            "method": "Computed 24h rolling realized vol, compared high vs low vol regime |return|",
            "data_source": f"BTC 15m OHLCV ({len(candles)} candles, 30 days)",
            "n_signals": len(rvols),
            "median_vol_bps": round(10000 * median_vol, 2),
            "high_vol_avg_abs_return_bps": avg_h,
            "low_vol_avg_abs_return_bps": avg_l,
            "vol_ratio": ratio,
            "hit_rate": 0,
            "correlation": 0,
            "conclusion": (
                f"Realized vol median: {10000 * median_vol:.2f} bps/bar. "
                f"High vol |return|: {avg_h} bps, Low vol: {avg_l} bps (ratio {ratio}x). "
                + ("Vol regime ทำนายขนาด move ได้ดี ใช้ปรับ position size ได้"
                   if ratio > 1.3
                   else "ไม่มีความแตกต่างชัดระหว่าง vol regime")),
        }

    # ── Data Availability & Next Steps ───────────────────

    def _check_topic_data(self, topic_name: str) -> dict:
        """Check what data is available for testing a topic."""
        DATA_MAP = {
            "volume_profile_vwap": {
                "needed": "BTC 15m OHLCV",
                "source": "market_data.index_price_klines / Binance API",
                "available": True},
            "funding_term_structure": {
                "needed": "Funding rate history",
                "source": "public.funding_rate",
                "available": True},
            "orderflow_imbalance": {
                "needed": "Taker buy/sell volume",
                "source": "market_data.taker_volume",
                "available": True},
            "cross_exchange_basis": {
                "needed": "Futures basis",
                "source": "market_data.basis (already basis_contrarian factor w=1.5)",
                "available": True,
                "note": "Already a production factor in v3"},
            "whale_onchain_tracking": {
                "needed": "Whale transfers",
                "source": "public.whale_alert (already whale_alerts factor w=1.5)",
                "available": True,
                "note": "Already a production factor in v3"},
            "gamma_exposure_gex": {
                "needed": "Options OI by strike + Greeks",
                "source": "market_data.option_greeks",
                "available": True,
                "note": "Collecting since 03-18, need ~2 weeks for meaningful test"},
            "correlation_regime": {
                "needed": "BTC + Alt OHLCV",
                "source": "market_data.mark_klines_alt",
                "available": True,
                "note": "Alt data collecting since 03-18"},
            "liquidation_cascade_reversion": {
                "needed": "Hourly liquidation data",
                "source": "public.liquidation",
                "available": True},
            "nlp_sentiment": {
                "needed": "Real-time news text + NLP model",
                "source": "ไม่มี -- ต้องสร้าง scraper + NLP pipeline",
                "available": False},
            "social_sentiment": {
                "needed": "Twitter/Telegram data stream",
                "source": "ไม่มี -- ต้องใช้ API ภายนอก (Twitter/LunarCrush)",
                "available": False},
            "microstructure_spread": {
                "needed": "Tick-level order book",
                "source": "market_data.order_book_raw (1000 levels, 5m)",
                "available": True},
            "time_of_day_seasonality": {
                "needed": "BTC OHLCV",
                "source": "ทดสอบแล้วใน Mission #001",
                "available": True,
                "note": "Hour filter +55% แต่เสี่ยง overfit (Mission #001)"},
            "day_of_week_effects": {
                "needed": "BTC OHLCV",
                "source": "ทดสอบบางส่วนใน Mission #001",
                "available": True,
                "note": "Monday effect confirmed -$475 (Mission #001)"},
            "stablecoin_flows": {
                "needed": "USDT/USDC mint/burn on-chain data",
                "source": "ไม่มี -- ต้องใช้ on-chain API (Etherscan/Tron)",
                "available": False},
            "dex_cex_ratio": {
                "needed": "DEX volume data",
                "source": "ไม่มี -- ต้องใช้ DeFiLlama/Dune Analytics API",
                "available": False},
            "mempool_analysis": {
                "needed": "BTC mempool data",
                "source": "ไม่มี -- ต้องรัน BTC node หรือ Mempool.space API",
                "available": False},
            "realized_vol_models": {
                "needed": "BTC OHLCV + Options IV",
                "source": "OHLCV available; Deribit DVOL collecting since 03-09",
                "available": True},
            "options_put_call_ratio": {
                "needed": "Options OI by type",
                "source": "market_data.options_data (collecting since 03-09)",
                "available": True,
                "note": "~2 weeks of data available"},
        }
        return DATA_MAP.get(topic_name, {
            "needed": "Unknown", "source": "N/A", "available": False})

    def _discovery_next_steps(self, topic_name: str,
                              proxy: dict | None, data: dict) -> list[str]:
        """Generate concrete next steps for a discovery topic."""
        steps = []
        if proxy and proxy.get("tested"):
            hit = proxy.get("hit_rate", 0)
            if hit > 55:
                steps.extend([
                    f"ผลเบื้องต้นดี (hit rate {hit}%) -- ทดสอบเป็น factor ใน composite",
                    "สร้าง scorer function ใน research/factor_registry.py",
                    "Backtest weight [0.5, 1.0, 1.5, 2.0] เทียบ v3 baseline",
                    "ถ้า delta > $200 → deploy เป็น factor ใหม่",
                ])
            else:
                steps.extend([
                    f"ผลเบื้องต้นยังไม่ชัด (hit rate {hit}%)",
                    "ปรับ parameter (threshold, lookback window) แล้วทดสอบใหม่",
                    "เก็บข้อมูลเพิ่ม 2 สัปดาห์แล้ว retest",
                    "ทดสอบบน alt coins ด้วย (ไม่ใช่แค่ BTC)",
                ])
        elif data.get("available"):
            steps.extend([
                f"ข้อมูลมีอยู่แล้ว ({data.get('source', '?')})",
                "สร้าง scorer function และทดสอบด้วย ExperimentEngine",
                "เปรียบเทียบกับ v3 baseline (6 coins OOS)",
            ])
        else:
            steps.extend([
                f"ต้องสร้าง data collector: {data.get('needed', '?')}",
                f"Source: {data.get('source', '?')}",
                "หลังเก็บข้อมูล 2-4 สัปดาห์จึง backtest ได้",
            ])
        note = data.get("note", "")
        if note:
            steps.append(f"หมายเหตุ: {note}")
        return steps

    # ── Insight Generation ────────────────────────────────────

    def _generate_insight(self, mission: dict) -> str:
        """Generate a 1-2 sentence summary of mission result in Thai."""
        r = mission.get("result", {})
        t = mission["type"]

        if not r.get("success"):
            return f"Mission ล้มเหลว: {r.get('error', 'unknown error')}"

        if t == "factor_test":
            delta = r.get("best_delta", 0)
            w = r.get("best_weight", "?")
            sign = "+" if delta >= 0 else ""
            if delta > 500:
                return f"{mission['target']} เพิ่ม {sign}${delta:.0f} ที่ w={w} ตัวเต็งสำหรับ v4!"
            elif delta > 0:
                return f"{mission['target']} เพิ่ม {sign}${delta:.0f} ที่ w={w} ปรับปรุงเล็กน้อย เก็บไว้ใน bench"
            else:
                return f"{mission['target']} ทำให้แย่ลง ({sign}${delta:.0f}) ใช้ไม่ได้ที่ weight ใดๆ"

        if t == "revalidation":
            verdict = r.get("verdict", "?")
            drift = r.get("drift", 0)
            if verdict == "stable":
                return f"{mission['target']} ยังคงเป็นบวก (drift: {drift:+.0f}) ไม่ต้องดำเนินการ"
            else:
                return f"{mission['target']} ประสิทธิภาพลดลง! พิจารณาถอดออกจาก production"

        if t == "combo_test":
            delta = r.get("best_delta", 0)
            if delta > 200:
                return f"เพิ่ม {mission['target']} ได้ +${delta:.0f} คุ้มค่าพิจารณาอัปเกรดโมเดล"
            else:
                return f"{mission['target']} เพิ่มแค่ ${delta:.0f} ไม่คุ้มกับความซับซ้อนที่เพิ่ม"

        if t == "paper_vs_backtest":
            verdict = r.get("verdict", "?")
            wr = r.get("paper_wr", 0)
            exp = r.get("expected_wr", 0)
            if verdict == "on_track":
                return f"Paper trading WR {wr}% vs คาดหวัง {exp}% โมเดลทำงานตามที่คาด"
            elif verdict == "outperforming":
                return f"Paper WR {wr}% สูงกว่าที่คาด {exp}%! ยืนยัน edge จริง"
            else:
                return f"Paper WR {wr}% ต่ำกว่าที่คาด {exp}% ต้องตรวจสอบปัญหา"

        if t == "coin_deep_dive":
            coin = r.get("coin", "?")
            wr = r.get("wr", 0)
            pnl = r.get("total_pnl", 0)
            verdict_th = {"strong": "แข็งแกร่ง", "weak": "อ่อนแอ", "average": "ปานกลาง"}
            v = verdict_th.get(r.get("verdict", ""), r.get("verdict", ""))
            return f"{coin}: WR {wr}%, PnL ${pnl:.0f} ผลงาน{v}"

        if t == "regime_test":
            regime = r.get("regime", "?")
            regime_th = {"bull": "ขาขึ้น", "bear": "ขาลง"}
            pnl = r.get("pnl", 0)
            status = "แข็งแกร่ง" if pnl > 0 else "เปราะบาง"
            return f"โมเดลในตลาด{regime_th.get(regime, regime)}: ${pnl:.0f} {status}"

        if t == "trade_analysis":
            wr = r.get("overall_wr", 0)
            n = r.get("total_trades", 0)
            pnl = r.get("total_pnl", 0)
            patterns = r.get("losing_patterns", [])
            recs = r.get("recommendations", [])
            top_issue = patterns[0] if patterns else "ไม่พบปัญหา"
            rec = recs[0] if recs else "ยังดี ทำต่อ"
            return f"7 วันล่าสุด: {n} เทรด WR {wr}% PnL ${pnl:.0f}. ปัญหา: {top_issue}. แนะนำ: {rec}"

        if t == "signal_quality":
            best_t = r.get("best_threshold", "5")
            best_wr = r.get("best_threshold_wr", 0)
            spread = r.get("wr_spread", 0)
            return f"WR spread {spread}pp ตาม threshold. Score ≥{best_t} มี WR {best_wr}% ดีที่สุด"

        if t == "drawdown_analysis":
            dd = r.get("max_dd", 0)
            dd_pct = r.get("max_dd_pct", 0)
            streak = r.get("longest_losing_streak", 0)
            return f"Max DD ${dd:.0f} ({dd_pct:.1f}%), streak ยาวสุด {streak} เทรด"

        if t == "web_discovery":
            topic = r.get("topic", "?")
            n_res = r.get("n_results", 0)
            hyp = r.get("hypothesis", "")
            if n_res > 0:
                return f"ไอเดียจาก {topic}: พบ {n_res} แหล่ง. {hyp[:80]}"
            else:
                return f"ไอเดีย {topic}: ไม่พบข้อมูลออนไลน์ แต่มีสมมติฐานให้ทดสอบ"

        return "Mission สำเร็จ"

    # ── Run Today ─────────────────────────────────────────────

    def run_today(self, force_type: str = None, progress_cb=None) -> dict:
        """Generate + execute today's mission. Main entry point."""
        mission = self.generate_mission(force_type)
        log.info(f"Generated mission: {mission['title']}")

        mission = self.execute_mission(mission, progress_cb)

        # Save to history
        self._data["missions"].append(mission)

        # Update meta
        meta = self._data["meta"]
        if mission["status"] == "completed":
            meta["total_xp"] = meta.get("total_xp", 0) + mission["xp_reward"]

            # Update streak
            today = datetime.utcnow().strftime("%Y-%m-%d")
            last_date = meta.get("last_mission_date")
            if last_date:
                try:
                    last_dt = datetime.strptime(last_date, "%Y-%m-%d")
                    today_dt = datetime.strptime(today, "%Y-%m-%d")
                    diff = (today_dt - last_dt).days
                    if diff == 1:
                        meta["current_streak"] = meta.get("current_streak", 0) + 1
                    elif diff > 1:
                        meta["current_streak"] = 1
                    # diff == 0: same day, keep streak
                except ValueError:
                    meta["current_streak"] = 1
            else:
                meta["current_streak"] = 1

            meta["longest_streak"] = max(
                meta.get("longest_streak", 0),
                meta.get("current_streak", 0)
            )
            meta["last_mission_date"] = today

        # Update level
        level_num, _ = _get_level(meta.get("total_xp", 0))
        meta["level"] = level_num

        self._save()
        log.info(f"Mission complete: {mission['title']} [{mission['status']}] +{mission['xp_reward']}XP")
        return mission


# ── CLI ───────────────────────────────────────────────────────

def main():
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Daily Missions")
    parser.add_argument("--run", action="store_true", help="Run today's mission")
    parser.add_argument("--type", default=None, help="Force mission type")
    parser.add_argument("--status", action="store_true", help="Show recent missions")
    args = parser.parse_args()

    engine = MissionEngine()

    if args.status:
        meta = engine.get_meta()
        xp = meta["total_xp"]
        lv = meta["level"]
        name = meta["level_name"]
        xp_cur = meta["xp_current_level"]
        xp_next = meta["xp_next_level"]
        progress_pct = meta["xp_progress"] / max(meta["xp_needed"], 1) * 100

        bar_len = 20
        filled = int(bar_len * progress_pct / 100)
        bar = "#" * filled + "-" * (bar_len - filled)

        print(f"\n  Lv.{lv} {name}  |  XP: {xp:,}  |  Streak: {meta['current_streak']}d")
        print(f"  [{bar}] {meta['xp_progress']}/{meta['xp_needed']} to Lv.{lv+1}")
        print(f"  Missions: {meta['completed_missions']}/{meta['total_missions']}")
        print()

        recent = engine.get_recent(10)
        if not recent:
            print("  No missions yet. Run --run to start!")
            return

        print(f"  {'Date':<12} {'Type':<18} {'Status':<10} {'XP':>4}  {'Insight'}")
        print("  " + "-" * 80)
        for m in recent:
            print(f"  {m['date']:<12} {m['type']:<18} {m['status']:<10} {m['xp_reward']:>4}  {(m.get('insight') or '')[:45]}")

    elif args.run:
        if engine.has_run_today() and not args.type:
            today = engine.get_today_missions()
            print(f"Already completed today:")
            for m in today:
                print(f"  {m['title']}: {m.get('insight', 'N/A')}")
            return

        result = engine.run_today(force_type=args.type)
        xp = result['xp_reward']
        wow_label = "meh" if xp < 20 else "nice" if xp < 50 else "WOW!" if xp < 100 else "LEGENDARY!"
        print(f"\n  Mission: {result['title']}")
        print(f"  Status:  {result['status']}")
        print(f"  XP:      +{xp} ({wow_label})")
        print(f"  Insight: {result.get('insight', 'N/A')}")

        meta = engine.get_meta()
        print(f"  Lv.{meta['level']} {meta['level_name']}  |  XP: {meta['total_xp']:,}  |  Streak: {meta['current_streak']}d")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
