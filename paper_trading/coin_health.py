"""
Coin Health Monitor
====================
Monitors per-coin trading health and auto-pauses coins showing edge decay.

6 metrics → health score (0-100):
  rolling_pnl (0.30), win_rate_trend (0.20), exit_quality (0.15),
  consecutive_losses (0.15), avg_pnl_per_trade (0.10), score_effectiveness (0.10)

Status: HEALTHY (>=50), WARNING (30-49), PAUSED (<30), COLD_START (<5 trades)
Resume requires score >= HEALTH_RESUME_THRESHOLD (hysteresis) + 24h min pause.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import state_db as db
from .config import (
    HEALTH_PAUSE_THRESHOLD,
    HEALTH_RESUME_THRESHOLD,
    HEALTH_MIN_TRADES,
    HEALTH_SHORT_WINDOW,
    HEALTH_MIN_PAUSE_HOURS,
)

logger = logging.getLogger("paper_trading.health")

# Metric weights (sum = 1.0)
METRIC_WEIGHTS = {
    "rolling_pnl": 0.30,
    "win_rate_trend": 0.20,
    "exit_quality": 0.15,
    "consecutive_losses": 0.15,
    "avg_pnl_per_trade": 0.10,
    "score_effectiveness": 0.10,
}


@dataclass
class CoinHealth:
    coin: str
    status: str  # HEALTHY, WARNING, PAUSED, COLD_START
    health_score: float
    metrics: dict = field(default_factory=dict)
    diagnosis: str = ""  # temporary_streak, edge_decay, or ""
    paused_at: str | None = None
    total_trades: int = 0


class CoinHealthMonitor:
    """Monitors per-coin health and decides whether to pause trading."""

    def __init__(self):
        self._cache: dict[str, CoinHealth] = {}

    def should_trade(self, coin: str) -> tuple[bool, str]:
        """Guard function: can this coin open new positions?

        Returns (allowed, reason).
        Paused coins return (False, "HEALTH_PAUSED: {diagnosis}").
        Cold start and healthy coins return (True, "").
        """
        health = self._cache.get(coin)
        if health is None:
            return True, ""  # no data yet

        if health.status == "PAUSED":
            return False, f"HEALTH_PAUSED: {health.diagnosis} (score={health.health_score:.0f})"

        return True, ""

    def update_all(self, coins: list[str]):
        """Recalculate health for all coins. Call once per cycle."""
        paused = []
        for coin in coins:
            health = self._compute_health(coin)
            self._cache[coin] = health

            # Persist to DB
            db.upsert_coin_health(
                coin=coin,
                status=health.status,
                health_score=health.health_score,
                metrics=health.metrics,
                diagnosis=health.diagnosis,
                paused_at=health.paused_at,
            )
            db.insert_health_history(
                coin=coin,
                health_score=health.health_score,
                status=health.status,
                metrics=health.metrics,
            )

            if health.status == "PAUSED":
                paused.append(coin)

        if paused:
            logger.warning(f"HEALTH PAUSED ({len(paused)}): {', '.join(paused)}")

    def get_health(self, coin: str) -> CoinHealth | None:
        return self._cache.get(coin)

    def get_paused_coins(self) -> list[str]:
        return [c for c, h in self._cache.items() if h.status == "PAUSED"]

    def get_all_health(self) -> dict[str, CoinHealth]:
        return dict(self._cache)

    # ── Private: compute health for one coin ──────────────────

    def _compute_health(self, coin: str) -> CoinHealth:
        """Compute health score from trade history."""
        all_trades = db.get_trades_for_coin(coin)  # newest first
        total = len(all_trades)

        if total < HEALTH_MIN_TRADES:
            return CoinHealth(
                coin=coin,
                status="COLD_START",
                health_score=100.0,  # assume healthy until proven otherwise
                metrics={"total_trades": total},
                total_trades=total,
            )

        # Reverse to chronological order for calculations
        trades = list(reversed(all_trades))
        recent = trades[-HEALTH_SHORT_WINDOW:]  # last N trades

        # Compute 6 metrics (each 0-100)
        m = {}
        m["rolling_pnl"] = self._compute_rolling_pnl(trades, recent)
        m["win_rate_trend"] = self._compute_win_rate_trend(trades, recent)
        m["exit_quality"] = self._compute_exit_quality(trades, recent)
        m["consecutive_losses"] = self._compute_consecutive_losses(trades)
        m["avg_pnl_per_trade"] = self._compute_avg_pnl_per_trade(trades, recent)
        m["score_effectiveness"] = self._compute_score_effectiveness(recent)
        m["total_trades"] = total

        # Weighted score
        health_score = sum(
            m[k] * METRIC_WEIGHTS[k]
            for k in METRIC_WEIGHTS
        )
        health_score = max(0.0, min(100.0, health_score))

        # Diagnosis
        diagnosis = self._diagnose(trades, recent, m)

        # Determine status (with hysteresis for pause/resume)
        status = self._determine_status(coin, health_score, diagnosis)

        # Get paused_at from existing DB record if still paused
        paused_at = None
        if status == "PAUSED":
            existing = db.get_coin_health(coin)
            if existing and existing["status"] == "PAUSED":
                paused_at = existing.get("paused_at")
            else:
                paused_at = datetime.now().isoformat()

        return CoinHealth(
            coin=coin,
            status=status,
            health_score=health_score,
            metrics=m,
            diagnosis=diagnosis,
            paused_at=paused_at,
            total_trades=total,
        )

    def _determine_status(self, coin: str, score: float, diagnosis: str) -> str:
        """Status with hysteresis: pause at <30, resume at >=50 + 24h min."""
        existing = db.get_coin_health(coin)

        if existing and existing["status"] == "PAUSED":
            # Currently paused -- check resume conditions
            if score >= HEALTH_RESUME_THRESHOLD:
                # Check minimum pause duration
                paused_at = existing.get("paused_at")
                if paused_at:
                    paused_dt = datetime.fromisoformat(paused_at)
                    elapsed = datetime.now() - paused_dt
                    if elapsed < timedelta(hours=HEALTH_MIN_PAUSE_HOURS):
                        return "PAUSED"  # still too early to resume
                return "HEALTHY"  # resumed
            return "PAUSED"  # still below resume threshold

        # Not currently paused
        if score < HEALTH_PAUSE_THRESHOLD:
            return "PAUSED"
        elif score < HEALTH_RESUME_THRESHOLD:
            return "WARNING"
        return "HEALTHY"

    # ── Metric functions (each returns 0-100) ─────────────────

    def _compute_rolling_pnl(self, all_trades: list, recent: list) -> float:
        """Recent PnL vs all-time. Positive recent = good, negative = bad."""
        recent_pnl = sum(t["pnl_net"] for t in recent)
        all_pnl = sum(t["pnl_net"] for t in all_trades)

        if not recent:
            return 50.0

        avg_all = all_pnl / len(all_trades) if all_trades else 0
        avg_recent = recent_pnl / len(recent)

        if avg_all == 0:
            # No baseline -- score based on absolute recent PnL
            if avg_recent > 0:
                return 70.0
            elif avg_recent < -1.0:
                return 20.0
            return 50.0

        # Ratio: recent/all-time average PnL per trade
        ratio = avg_recent / abs(avg_all) if avg_all != 0 else 0
        # Map ratio [-2, 2] → [0, 100]
        score = 50.0 + ratio * 25.0
        return max(0.0, min(100.0, score))

    def _compute_win_rate_trend(self, all_trades: list, recent: list) -> float:
        """Recent WR vs all-time WR. WR drop = warning."""
        all_wr = sum(1 for t in all_trades if t["pnl_net"] > 0) / len(all_trades) if all_trades else 0.5
        recent_wr = sum(1 for t in recent if t["pnl_net"] > 0) / len(recent) if recent else 0.5

        # Difference in percentage points
        diff = recent_wr - all_wr  # positive = improving, negative = declining

        # Map: -0.3 → 0, 0 → 50, +0.3 → 100
        score = 50.0 + diff * (50.0 / 0.3)
        return max(0.0, min(100.0, score))

    def _compute_exit_quality(self, all_trades: list, recent: list) -> float:
        """TP rate and SIGNAL_FLIP penalty."""
        if not recent:
            return 50.0

        tp_count = sum(1 for t in recent if t.get("exit_reason") == "TP")
        sl_count = sum(1 for t in recent if t.get("exit_reason") == "SL")
        flip_count = sum(1 for t in recent if t.get("exit_reason") == "SIGNAL_FLIP")
        n = len(recent)

        # TP rate is good, FLIP is bad, SL is worst
        tp_rate = tp_count / n
        flip_penalty = flip_count / n * 0.3  # each flip costs 30% penalty
        sl_penalty = sl_count / n * 0.5  # each SL costs 50% penalty

        score = tp_rate * 100.0 - flip_penalty * 100.0 - sl_penalty * 100.0
        return max(0.0, min(100.0, score))

    def _compute_consecutive_losses(self, all_trades: list) -> float:
        """How many consecutive losses from the end. 0=100, 3=50, 6+=0."""
        streak = 0
        for t in reversed(all_trades):
            if t["pnl_net"] <= 0:
                streak += 1
            else:
                break

        # Map: 0→100, 3→50, 6+→0
        if streak == 0:
            return 100.0
        elif streak <= 6:
            return max(0.0, 100.0 - streak * (100.0 / 6.0))
        return 0.0

    def _compute_avg_pnl_per_trade(self, all_trades: list, recent: list) -> float:
        """Recent avg PnL per trade vs all-time."""
        if not recent:
            return 50.0

        avg_all = sum(t["pnl_net"] for t in all_trades) / len(all_trades) if all_trades else 0
        avg_recent = sum(t["pnl_net"] for t in recent) / len(recent)

        if avg_all == 0:
            return 70.0 if avg_recent > 0 else 30.0

        ratio = avg_recent / abs(avg_all) if avg_all != 0 else 0
        score = 50.0 + ratio * 25.0
        return max(0.0, min(100.0, score))

    def _compute_score_effectiveness(self, recent: list) -> float:
        """Correlation between btc_score_entry and PnL. High = model still works."""
        if len(recent) < 3:
            return 50.0  # not enough data

        scores = [t.get("btc_score_entry") for t in recent]
        pnls = [t["pnl_net"] for t in recent]

        # Filter out None scores
        pairs = [(s, p) for s, p in zip(scores, pnls) if s is not None]
        if len(pairs) < 3:
            return 50.0

        # Simple correlation (Pearson)
        n = len(pairs)
        sx = sum(s for s, _ in pairs)
        sy = sum(p for _, p in pairs)
        sxy = sum(s * p for s, p in pairs)
        sx2 = sum(s * s for s, _ in pairs)
        sy2 = sum(p * p for _, p in pairs)

        denom = ((n * sx2 - sx * sx) * (n * sy2 - sy * sy)) ** 0.5
        if denom == 0:
            return 50.0

        r = (n * sxy - sx * sy) / denom
        # For our strategy: higher absolute score should predict higher PnL
        # r > 0 means model works. r ~ 0 means random. r < 0 means broken.
        # Map: -1→0, 0→50, 1→100
        score = 50.0 + r * 50.0
        return max(0.0, min(100.0, score))

    # ── Diagnosis ─────────────────────────────────────────────

    def _diagnose(self, all_trades: list, recent: list, metrics: dict) -> str:
        """Classify: temporary_streak vs edge_decay."""
        if not recent:
            return ""

        # Count consecutive losses from the end
        streak = 0
        for t in reversed(all_trades):
            if t["pnl_net"] <= 0:
                streak += 1
            else:
                break

        all_wr = sum(1 for t in all_trades if t["pnl_net"] > 0) / len(all_trades) if all_trades else 0.5
        recent_wr = sum(1 for t in recent if t["pnl_net"] > 0) / len(recent) if recent else 0.5
        wr_drop = all_wr - recent_wr

        # Edge decay: long losing streak OR big WR drop
        if streak >= 5 or wr_drop > 0.20:
            return "edge_decay"

        # Temporary streak: short losing streak, WR still reasonable
        if streak >= 2:
            return "temporary_streak"

        return ""
