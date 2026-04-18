"""Research engine configuration."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
RESEARCH_DIR = BASE_DIR / "research"
EXPERIMENTS_DIR = BASE_DIR / "experiments"
DATA_CACHE_DIR = BASE_DIR / "data_cache"

# Registry files
FACTOR_REGISTRY_PATH = RESEARCH_DIR / "factor_registry.json"
MODEL_REGISTRY_PATH = RESEARCH_DIR / "model_registry.json"
EXPERIMENT_REGISTRY_PATH = EXPERIMENTS_DIR / "registry.json"
MISSIONS_PATH = RESEARCH_DIR / "missions.json"
MISSIONS_DIR = BASE_DIR / "missions"

# Paper trading state
PAPER_TRADES_DB = BASE_DIR / "paper_trading" / "state" / "paper_trades.db"

# Research scheduler state
SCHEDULER_DB = RESEARCH_DIR / "state" / "scheduler.db"

# Backtest defaults (from paper_trading/config.py)
INIT_EQUITY = 10_000.0
BUDGET_USDT = 1_000.0
LEVERAGE = 2.0
FEE_BPS = 2.0
SLIP_BPS = 1.5
MAX_HOLD_BARS = 96
COOLDOWN_BARS = 4

# OOS periods
OOS_BEAR_START = "2025-01-01"
OOS_BEAR_END = "2025-12-31"
OOS_BULL_START = "2026-01-01"
OOS_BULL_END = "2026-03-31"

# Factor testing
WEIGHTS_TO_TEST = [0.5, 1.0, 1.5, 2.0]
MIN_IMPROVEMENT_PCT = 2.0  # minimum % improvement to keep factor

# Database
def get_pg_dsn():
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
    return (
        f"host={os.getenv('PG_HOST', 'localhost')} "
        f"port={os.getenv('PG_PORT', '5432')} "
        f"dbname={os.getenv('PG_DB', 'smart_trading')} "
        f"user={os.getenv('PG_USER', 'postgres')} "
        f"password={os.getenv('PG_PASS', '')}"
    )

def get_pg_conn():
    import psycopg2
    return psycopg2.connect(get_pg_dsn())
