"""Research Scheduler - automated research job runner.

Runs experiments on schedule using APScheduler daemon.

Usage:
    python research/scheduler.py                     # daemon mode
    python research/scheduler.py --once validate     # run one job
    python research/scheduler.py --once test skew_25d # test specific factor
    python research/scheduler.py --status            # show job history
"""
import argparse
import json
import logging
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from research.config import SCHEDULER_DB

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("research.scheduler")

# ── State DB ──────────────────────────────────────────────────

def _init_state_db():
    SCHEDULER_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(SCHEDULER_DB))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS job_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_name TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT DEFAULT 'running',
            result_summary TEXT,
            error TEXT
        )
    """)
    conn.commit()
    return conn


def _log_job_start(conn, job_name):
    cur = conn.execute(
        "INSERT INTO job_runs (job_name, started_at, status) VALUES (?, ?, 'running')",
        (job_name, datetime.utcnow().isoformat()),
    )
    conn.commit()
    return cur.lastrowid


def _log_job_end(conn, run_id, status, summary="", error=""):
    conn.execute(
        "UPDATE job_runs SET finished_at=?, status=?, result_summary=?, error=? WHERE id=?",
        (datetime.utcnow().isoformat(), status, summary, error, run_id),
    )
    conn.commit()


def _get_job_history(conn, limit=20):
    rows = conn.execute(
        "SELECT * FROM job_runs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return rows


# ── Job Definitions ───────────────────────────────────────────

def job_validate_model():
    """Compare paper trading PnL vs backtest expectation."""
    log.info("[validate_model] Starting model validation...")
    from research.model_registry import ModelRegistry
    from research.config import PAPER_TRADES_DB

    mr = ModelRegistry()
    champion = mr.get_champion()
    if not champion:
        return "No champion model found"

    # Load paper trading stats
    if not PAPER_TRADES_DB.exists():
        return "No paper trades DB"

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

    n = stats["n"]
    if n == 0:
        return "No trades yet"

    wr = 100.0 * stats["wins"] / n
    total_pnl = stats["total_pnl"]
    avg_pnl = stats["avg_pnl"]

    expected_wr = champion.get("oos_win_rate", 66.6)

    result = (
        f"Champion: {champion['version']} | "
        f"Paper: {n} trades, WR {wr:.1f}% (expected {expected_wr}%), "
        f"PnL ${total_pnl:.2f}, Avg ${avg_pnl:.2f}"
    )

    if wr < expected_wr - 15:
        result += " | WARNING: WR significantly below expectation!"
    elif wr < expected_wr - 5:
        result += " | Note: WR below expectation"
    else:
        result += " | OK: within expected range"

    log.info(f"[validate_model] {result}")
    return result


def job_test_untested_factors():
    """Test all untested factors from registry."""
    log.info("[test_untested] Starting untested factor batch test...")
    from research.factor_registry import FactorRegistry
    from research.experiment_engine import ExperimentEngine

    fr = FactorRegistry()
    untested = fr.get_untested()

    if not untested:
        return "No untested factors"

    # Only test factors that have scorers defined
    testable = [f for f in untested if f.get("scorer_module") and f.get("scorer_function")]
    if not testable:
        return f"{len(untested)} untested factors but none have scorers defined"

    engine = ExperimentEngine()
    engine.load_data()

    results = []
    for f in testable:
        try:
            scorer = fr.get_scorer(f["name"])
            result = engine.test_factor(f["name"], scorer)
            fr.update_test_result(f["name"], {
                "best_weight": result["best_weight"],
                "best_delta_pnl": result["best_delta"],
                "best_pnl": result["best_pnl"],
            })
            results.append(f"{f['name']}: delta=${result['best_delta']:.0f}")
            engine.save_experiment("factor_test", result,
                                   f"Auto-test: {f['name']}")
        except Exception as e:
            log.error(f"Failed to test {f['name']}: {e}")
            results.append(f"{f['name']}: ERROR {e}")

    summary = f"Tested {len(results)} factors: " + "; ".join(results)
    log.info(f"[test_untested] {summary}")
    return summary


def job_coin_screening():
    """Screen top coins for trading candidates."""
    log.info("[coin_screening] Starting coin screening...")
    from research.experiment_engine import ExperimentEngine

    engine = ExperimentEngine()
    result = engine.run_coin_screening(top_n=50)

    if "error" in result:
        return f"Error: {result['error']}"

    coins = result.get("coins", [])
    top10 = coins[:10]
    summary = f"Screened {result['total_screened']} coins, {len(coins)} profitable. "
    summary += "Top 10: " + ", ".join(
        f"{c.get('symbol', '?')}(${c.get('net_pnl', 0):.0f})" for c in top10
    )

    engine.save_experiment("coin_screening", result, "Weekly coin screening")
    log.info(f"[coin_screening] {summary}")
    return summary


def job_update_leaderboard():
    """Rebuild model leaderboard."""
    log.info("[leaderboard] Updating leaderboard...")
    from research.leaderboard import build_leaderboard
    from research.model_registry import ModelRegistry

    lb = build_leaderboard(ModelRegistry())
    summary = f"Leaderboard: {len(lb)} models ranked. "
    if lb:
        summary += f"#1: {lb[0]['version']} (${lb[0]['oos_pnl']})"
    log.info(f"[leaderboard] {summary}")
    return summary


def job_daily_missions():
    """Generate and execute today's research mission."""
    log.info("[daily_missions] Starting daily mission...")
    from research.missions import MissionEngine

    engine = MissionEngine()
    if engine.has_run_today():
        return "Already completed today"

    result = engine.run_today()
    return f"Mission: {result['title']} (+{result['xp_reward']}XP)"


def job_push_dashboard():
    """Push update to dashboard via WebSocket."""
    try:
        import requests
        # Trigger dashboard refresh by hitting the API
        requests.get("http://localhost:5000/api/trading/stats", timeout=5)
        return "Dashboard ping OK"
    except Exception:
        return "Dashboard not running"


# ── Job Registry ──────────────────────────────────────────────

JOBS = {
    "daily_missions": {
        "func": job_daily_missions,
        "description": "Daily research mission",
        "schedule": {"trigger": "cron", "hour": 6, "minute": 30},
    },
    "validate": {
        "func": job_validate_model,
        "description": "Compare paper PnL vs backtest expectation",
        "schedule": {"trigger": "cron", "hour": 6, "minute": 0},
    },
    "test_untested": {
        "func": job_test_untested_factors,
        "description": "Test all untested factors",
        "schedule": {"trigger": "cron", "day_of_week": "sun", "hour": 2, "minute": 0},
    },
    "coin_screening": {
        "func": job_coin_screening,
        "description": "Screen top 50 coins",
        "schedule": {"trigger": "cron", "day_of_week": "sat", "hour": 2, "minute": 0},
    },
    "leaderboard": {
        "func": job_update_leaderboard,
        "description": "Rebuild model leaderboard",
        "schedule": {"trigger": "cron", "hour": 7, "minute": 0},
    },
    "push_dashboard": {
        "func": job_push_dashboard,
        "description": "Push updates to dashboard",
        "schedule": {"trigger": "interval", "minutes": 15},
    },
}


def run_job(job_name, state_conn=None):
    """Run a single job with logging."""
    if job_name not in JOBS:
        log.error(f"Unknown job: {job_name}")
        return

    job = JOBS[job_name]
    own_conn = state_conn is None
    if own_conn:
        state_conn = _init_state_db()

    run_id = _log_job_start(state_conn, job_name)
    log.info(f"Running job: {job_name} (run_id={run_id})")

    try:
        result = job["func"]()
        _log_job_end(state_conn, run_id, "success", summary=str(result))
        log.info(f"Job {job_name} completed: {result}")
    except Exception as e:
        _log_job_end(state_conn, run_id, "error", error=str(e))
        log.error(f"Job {job_name} failed: {e}", exc_info=True)
    finally:
        if own_conn:
            state_conn.close()


# ── Daemon Mode ───────────────────────────────────────────────

def run_daemon():
    """Run all scheduled jobs as a daemon."""
    from apscheduler.schedulers.blocking import BlockingScheduler

    scheduler = BlockingScheduler()
    state_conn = _init_state_db()

    for name, job in JOBS.items():
        sched = job["schedule"].copy()
        trigger = sched.pop("trigger")
        scheduler.add_job(
            run_job, trigger,
            args=[name, state_conn],
            id=name,
            name=job["description"],
            **sched,
        )
        log.info(f"Scheduled: {name} ({trigger}, {sched})")

    log.info("=" * 60)
    log.info("  RESEARCH SCHEDULER STARTED")
    log.info(f"  Jobs: {len(JOBS)}")
    log.info("=" * 60)

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler stopped.")
    finally:
        state_conn.close()


def show_status():
    """Show recent job history."""
    conn = _init_state_db()
    rows = _get_job_history(conn, limit=20)
    conn.close()

    if not rows:
        print("No job history.")
        return

    print(f"{'ID':>4}  {'Job':<20}  {'Status':<8}  {'Started':<20}  {'Summary'}")
    print("-" * 90)
    for r in rows:
        print(f"{r[0]:>4}  {r[1]:<20}  {r[4]:<8}  {r[2]:<20}  {(r[5] or '')[:40]}")


# ── CLI ───────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Research Scheduler")
    parser.add_argument("--once", nargs="+",
                        help="Run a single job: --once <job_name> [args]")
    parser.add_argument("--status", action="store_true",
                        help="Show job history")
    args = parser.parse_args()

    if args.status:
        show_status()
    elif args.once:
        job_name = args.once[0]
        if job_name == "test" and len(args.once) > 1:
            # Special case: test specific factor
            factor_name = args.once[1]
            log.info(f"Testing specific factor: {factor_name}")
            from research.factor_registry import FactorRegistry
            from research.experiment_engine import ExperimentEngine
            fr = FactorRegistry()
            factor = fr.get(factor_name)
            if not factor:
                log.error(f"Factor '{factor_name}' not in registry")
                return
            if not factor.get("scorer_function"):
                log.error(f"Factor '{factor_name}' has no scorer")
                return
            engine = ExperimentEngine()
            scorer = fr.get_scorer(factor_name)
            result = engine.test_factor(factor_name, scorer)
            print(json.dumps(result, indent=2, default=str))
            fr.update_test_result(factor_name, {
                "best_weight": result["best_weight"],
                "best_delta_pnl": result["best_delta"],
                "best_pnl": result["best_pnl"],
            })
            engine.save_experiment("factor_test", result,
                                   f"Manual test: {factor_name}")
        else:
            run_job(job_name)
    else:
        run_daemon()


if __name__ == "__main__":
    main()
