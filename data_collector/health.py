"""
Health Monitor
===============
Track collector success/failure, detect stale sources, log status summaries.
"""

import logging
from datetime import datetime, timedelta

from .config import STALENESS_THRESHOLDS

logger = logging.getLogger("data_collector.health")


class HealthMonitor:
    """Track last_success timestamp per collector. Log warnings for stale sources."""

    def __init__(self):
        self.last_success: dict[str, datetime] = {}
        self.last_error: dict[str, str] = {}
        self.last_result: dict[str, dict] = {}
        self.consecutive_failures: dict[str, int] = {}
        self.total_runs: dict[str, int] = {}
        self.total_successes: dict[str, int] = {}

    def record_success(self, name: str, result: dict):
        now = datetime.utcnow()
        self.last_success[name] = now
        self.last_result[name] = result
        self.consecutive_failures[name] = 0
        self.total_runs[name] = self.total_runs.get(name, 0) + 1
        self.total_successes[name] = self.total_successes.get(name, 0) + 1
        # Clear last error on success
        self.last_error.pop(name, None)

    def record_failure(self, name: str, error: str):
        self.last_error[name] = error
        self.consecutive_failures[name] = self.consecutive_failures.get(name, 0) + 1
        self.total_runs[name] = self.total_runs.get(name, 0) + 1

    def check_all(self) -> dict:
        """Return dict of stale/failed collectors with details."""
        now = datetime.utcnow()
        issues = {}

        for name, threshold_min in STALENESS_THRESHOLDS.items():
            if name not in self.last_success:
                issues[name] = "NEVER_RUN"
                continue

            age = now - self.last_success[name]
            if age > timedelta(minutes=threshold_min):
                issues[name] = f"STALE ({age} > {threshold_min}min)"
            elif self.consecutive_failures.get(name, 0) >= 3:
                issues[name] = f"FAILING ({self.consecutive_failures[name]}x: {self.last_error.get(name, '?')})"

        return issues

    def log_status(self):
        """Log full status summary."""
        now = datetime.utcnow()
        issues = self.check_all()

        lines = ["=" * 50, "DATA COLLECTOR STATUS"]
        for name in sorted(set(list(STALENESS_THRESHOLDS.keys()) + list(self.last_success.keys()))):
            runs = self.total_runs.get(name, 0)
            ok = self.total_successes.get(name, 0)

            if name in self.last_success:
                age = now - self.last_success[name]
                age_str = f"{int(age.total_seconds())}s ago"
                result = self.last_result.get(name, {})
                rows = result.get("rows", "?")
                ms = result.get("elapsed_ms", "?")
                status = f"OK ({age_str}, {rows} rows, {ms}ms)"
            elif name in self.last_error:
                status = f"ERROR: {self.last_error[name]}"
            else:
                status = "WAITING"

            if name in issues:
                status = f"!! {issues[name]} !! (last ok: {status})"

            lines.append(f"  {name:20s} [{ok}/{runs}] {status}")

        lines.append("=" * 50)
        for line in lines:
            logger.info(line)

        if issues:
            logger.warning(f"Issues detected: {issues}")

        return issues
