"""Model Registry - track model versions and promotion history.

Maintains the evolution from v1 → v3 and any future candidates.
"""
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from research.config import MODEL_REGISTRY_PATH

log = logging.getLogger(__name__)


class ModelRegistry:
    """Track model versions, their configs, and performance."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else MODEL_REGISTRY_PATH
        self._models: list[dict] = []
        if self.path.exists():
            self._load()
        else:
            self._bootstrap()
            self._save()

    # ── Queries ───────────────────────────────────────────────
    def get_all(self) -> list[dict]:
        return self._models

    def get_champion(self) -> Optional[dict]:
        for m in self._models:
            if m.get("is_champion"):
                return m
        return None

    def get_challengers(self) -> list[dict]:
        return [m for m in self._models if m.get("status") == "challenger"]

    def get_by_version(self, version: str) -> Optional[dict]:
        for m in self._models:
            if m["version"] == version:
                return m
        return None

    def get_leaderboard(self) -> list[dict]:
        """Return models sorted by OOS PnL (realistic portfolio)."""
        ranked = sorted(self._models, key=lambda m: m.get("oos_pnl", 0), reverse=True)
        for i, m in enumerate(ranked):
            m["rank"] = i + 1
        return ranked

    # ── Mutations ─────────────────────────────────────────────
    def add_model(self, version: str, factors: list[dict], metrics: dict,
                  description: str = "", status: str = "challenger") -> dict:
        """Register a new model version."""
        model = {
            "version": version,
            "created": datetime.utcnow().strftime("%Y-%m-%d"),
            "description": description,
            "status": status,  # champion, challenger, retired, rejected
            "is_champion": False,
            "factors": factors,  # [{name, weight}, ...]
            "oos_pnl": metrics.get("oos_pnl"),
            "oos_sharpe": metrics.get("oos_sharpe"),
            "oos_win_rate": metrics.get("oos_win_rate"),
            "oos_trades": metrics.get("oos_trades"),
            "oos_max_dd": metrics.get("oos_max_dd"),
            "realistic_return_pct": metrics.get("realistic_return_pct"),
            "realistic_max_dd": metrics.get("realistic_max_dd"),
            "metrics": metrics,
        }
        self._models.append(model)
        self._save()
        return model

    def promote_champion(self, version: str) -> None:
        """Make version the champion, retire previous champion."""
        for m in self._models:
            if m.get("is_champion"):
                m["is_champion"] = False
                m["status"] = "retired"
        target = self.get_by_version(version)
        if target:
            target["is_champion"] = True
            target["status"] = "champion"
            target["promoted_date"] = datetime.utcnow().strftime("%Y-%m-%d")
        self._save()

    def reject_model(self, version: str, reason: str = "") -> None:
        m = self.get_by_version(version)
        if m:
            m["status"] = "rejected"
            m["reject_reason"] = reason
            self._save()

    # ── Persistence ───────────────────────────────────────────
    def _load(self):
        with open(self.path, "r") as fh:
            self._models = json.load(fh)

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w") as fh:
            json.dump(self._models, fh, indent=2, default=str)

    # ── Bootstrap ─────────────────────────────────────────────
    def _bootstrap(self):
        self._models = [
            {
                "version": "v1",
                "created": "2026-03-08",
                "description": "Initial 8 factors, grid-searched",
                "status": "retired",
                "is_champion": False,
                "factors": [
                    {"name": "oi_divergence", "weight": 1.0},
                    {"name": "taker_ratio", "weight": 1.0},
                    {"name": "ls_ratio", "weight": 1.0},
                    {"name": "funding_rate", "weight": 1.0},
                    {"name": "fear_greed", "weight": 1.0},
                    {"name": "whale_alerts", "weight": 1.0},
                    {"name": "liquidation", "weight": 1.0},
                    {"name": "etf_flows", "weight": 1.0},
                ],
                "oos_pnl": 1575,
                "oos_sharpe": None,
                "oos_win_rate": None,
                "oos_trades": None,
                "metrics": {"oos_pnl": 1575},
            },
            {
                "version": "v1.1",
                "created": "2026-03-08",
                "description": "TZ fix, 6 coins, pa=False, 2x leverage",
                "status": "retired",
                "is_champion": False,
                "factors": [
                    {"name": "oi_divergence", "weight": 1.0},
                    {"name": "taker_ratio", "weight": 1.0},
                    {"name": "ls_ratio", "weight": 1.0},
                    {"name": "funding_rate", "weight": 1.0},
                    {"name": "fear_greed", "weight": 1.0},
                    {"name": "whale_alerts", "weight": 1.0},
                    {"name": "liquidation", "weight": 1.0},
                    {"name": "etf_flows", "weight": 1.0},
                ],
                "oos_pnl": 7486,
                "oos_sharpe": None,
                "oos_win_rate": None,
                "oos_trades": None,
                "metrics": {"oos_pnl": 7486},
            },
            {
                "version": "v1.2",
                "created": "2026-03-08",
                "description": "Short bias (threshold-0.5)",
                "status": "retired",
                "is_champion": False,
                "factors": [
                    {"name": "oi_divergence", "weight": 1.0},
                    {"name": "taker_ratio", "weight": 1.0},
                    {"name": "ls_ratio", "weight": 1.0},
                    {"name": "funding_rate", "weight": 1.0},
                    {"name": "fear_greed", "weight": 1.0},
                    {"name": "whale_alerts", "weight": 1.0},
                    {"name": "liquidation", "weight": 1.0},
                    {"name": "etf_flows", "weight": 1.0},
                ],
                "oos_pnl": 9819,
                "oos_sharpe": None,
                "oos_win_rate": None,
                "oos_trades": None,
                "metrics": {"oos_pnl": 9819, "notes": "+31.2% vs v1.1"},
            },
            {
                "version": "v2",
                "created": "2026-03-08",
                "description": "Dead zone + re-grid-search",
                "status": "rejected",
                "is_champion": False,
                "factors": [],
                "oos_pnl": None,
                "oos_sharpe": None,
                "oos_win_rate": None,
                "oos_trades": None,
                "reject_reason": "Overfit to in-sample",
                "metrics": {},
            },
            {
                "version": "v3",
                "created": "2026-03-09",
                "description": "8 optimal factors from scratch (mega discovery)",
                "status": "champion",
                "is_champion": True,
                "promoted_date": "2026-03-09",
                "factors": [
                    {"name": "liquidation", "weight": 2.0},
                    {"name": "funding_rate", "weight": 2.0},
                    {"name": "ob_combined", "weight": 2.0},
                    {"name": "etf_flows", "weight": 1.0},
                    {"name": "basis_contrarian", "weight": 1.5},
                    {"name": "tick_liq", "weight": 2.0},
                    {"name": "oi_divergence", "weight": 0.5},
                    {"name": "whale_alerts", "weight": 1.5},
                ],
                "oos_pnl": 14121,
                "oos_sharpe": 4.97,
                "oos_win_rate": 66.6,
                "oos_trades": 946,
                "oos_max_dd": -6.0,
                "realistic_return_pct": 145.4,
                "realistic_max_dd": -6.0,
                "metrics": {
                    "oos_pnl": 14121,
                    "full_pnl": 18056,
                    "oos_sharpe_range": "4.97-6.83",
                    "per_coin": {
                        "BTC": {"trades": 206, "wr": 64.1, "sharpe": 4.97, "pnl": 1922},
                        "XRP": {"trades": 146, "wr": 69.2, "sharpe": 6.30, "pnl": 2865},
                        "ADA": {"trades": 149, "wr": 69.1, "sharpe": 6.65, "pnl": 3247},
                        "DOT": {"trades": 137, "wr": 69.3, "sharpe": 6.83, "pnl": 3420},
                        "SUI": {"trades": 161, "wr": 64.0, "sharpe": 6.07, "pnl": 3513},
                        "FIL": {"trades": 147, "wr": 64.0, "sharpe": 5.50, "pnl": 3089},
                    },
                },
            },
        ]
        log.info(f"Bootstrapped {len(self._models)} models")
