"""
Dynamic Per-Coin Sizing — Total PnL Tiered
============================================
Scales BUDGET_PER_COIN based on each coin's cumulative net PnL.
Winners with fat-tail wins (ARIA) get more capital; negative coins de-risked.

Rationale: per-trade Sharpe fails for coins with tail-win distributions
(ARIA paper: $626 PnL but per-trade Sharpe ≈ 0 due to variance). Total PnL
matches the user's intent: "the coin that carries the port gets more size".

Added 2026-04-15 in port shrink pass.
Ground truth: paper trades table (closed trades only).
"""

import logging

from . import state_db as db

logger = logging.getLogger(__name__)

MIN_TRADES = 10

# total_net_pnl → multiplier (applied once >= MIN_TRADES)
_THRESHOLDS = [
    (300.0,  2.5),   # >= $300 cumulative → 2.5x (ARIA-tier)
    (100.0,  1.8),   # >= $100 → 1.8x
    (30.0,   1.3),   # >= $30  → 1.3x
    (0.0,    1.0),   # >= $0   → 1.0x (baseline)
    (-50.0,  0.7),   # >= -$50 → 0.7x (mild de-risk)
    (-1e9,   0.4),   #  <  -$50 → 0.4x (heavy de-risk)
]


def rolling_sharpe_multiplier(coin: str, base_budget: float) -> tuple[float, float, int]:
    """
    Compute dynamic budget for a coin from total closed-trade PnL.

    Name kept for API compat, but metric is now total PnL (not Sharpe).

    Returns: (budget, total_pnl, n_trades)
    """
    try:
        trades = db.get_trades_for_coin(coin)
    except Exception as e:
        logger.warning(f"[sizing] {coin} fetch failed: {e} -> base budget")
        return base_budget, float("nan"), 0

    n = len(trades)
    if n < MIN_TRADES:
        return base_budget, float("nan"), n

    total_pnl = sum(float(t.get("pnl_net", 0.0) or 0.0) for t in trades)

    mult = 0.4  # floor
    for thr, m in _THRESHOLDS:
        if total_pnl >= thr:
            mult = m
            break

    return base_budget * mult, total_pnl, n
