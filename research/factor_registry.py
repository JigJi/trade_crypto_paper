"""Factor Registry - catalog of all known factors with test results.

Tracks every factor's status: production, tested_positive, tested_negative, untested.
Bootstrapped from existing experiment results.
"""
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from research.config import FACTOR_REGISTRY_PATH

log = logging.getLogger(__name__)


class FactorRegistry:
    """In-memory registry backed by JSON file."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else FACTOR_REGISTRY_PATH
        self._factors: dict[str, dict] = {}
        if self.path.exists():
            self._load()
        else:
            self._bootstrap()
            self._save()

    # ── Queries ───────────────────────────────────────────────
    def get_all(self) -> list[dict]:
        return list(self._factors.values())

    def get(self, name: str) -> Optional[dict]:
        return self._factors.get(name)

    def get_by_status(self, status: str) -> list[dict]:
        return [f for f in self._factors.values() if f["status"] == status]

    def get_by_category(self, category: str) -> list[dict]:
        return [f for f in self._factors.values() if f["category"] == category]

    def get_production_factors(self) -> list[dict]:
        return self.get_by_status("production")

    def get_untested(self) -> list[dict]:
        return self.get_by_status("untested")

    # ── Mutations ─────────────────────────────────────────────
    def add_factor(self, definition: dict) -> None:
        name = definition["name"]
        if name in self._factors:
            log.warning(f"Factor '{name}' already exists, updating")
            self._factors[name].update(definition)
        else:
            self._factors[name] = definition
        self._save()

    def update_test_result(self, name: str, result: dict) -> None:
        """Update factor with backtest result.

        result keys: best_weight, best_delta_pnl, best_pnl, metrics, tested_date
        """
        if name not in self._factors:
            log.error(f"Factor '{name}' not in registry")
            return
        f = self._factors[name]
        delta = result.get("best_delta_pnl", 0)
        f["best_weight"] = result.get("best_weight")
        f["best_delta_pnl"] = delta
        f["best_pnl"] = result.get("best_pnl")
        f["last_tested"] = result.get("tested_date", datetime.utcnow().strftime("%Y-%m-%d"))
        f["test_metrics"] = result.get("metrics", {})
        # Auto-classify
        if delta > 0:
            if f["status"] not in ("production",):
                f["status"] = "tested_positive"
        else:
            if f["status"] not in ("production",):
                f["status"] = "tested_negative"
        self._save()

    def promote_to_production(self, name: str, weight: float) -> None:
        if name not in self._factors:
            return
        self._factors[name]["status"] = "production"
        self._factors[name]["production_weight"] = weight
        self._save()

    def demote_from_production(self, name: str) -> None:
        if name not in self._factors:
            return
        f = self._factors[name]
        f["status"] = "tested_positive" if f.get("best_delta_pnl", 0) > 0 else "tested_negative"
        f["production_weight"] = None
        self._save()

    # ── Scorer Resolution ─────────────────────────────────────
    def get_scorer(self, name: str):
        """Dynamically import the scorer function for a factor."""
        f = self._factors.get(name)
        if not f:
            raise ValueError(f"Unknown factor: {name}")
        module_name = f.get("scorer_module")
        func_name = f.get("scorer_function")
        if not module_name or not func_name:
            raise ValueError(f"Factor '{name}' has no scorer defined")
        import importlib
        mod = importlib.import_module(module_name)
        return getattr(mod, func_name)

    # ── Persistence ───────────────────────────────────────────
    def _load(self):
        with open(self.path, "r") as fh:
            data = json.load(fh)
        self._factors = {f["name"]: f for f in data}
        log.info(f"Loaded {len(self._factors)} factors from {self.path}")

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w") as fh:
            json.dump(list(self._factors.values()), fh, indent=2, default=str)

    # ── Bootstrap from known factors ──────────────────────────
    def _bootstrap(self):
        """Seed registry from existing experiment results."""
        factors = [
            # ── v3 PRODUCTION (8 factors) ──
            _f("liquidation", "derivatives", "mega_factor_discovery", "score_core_liq",
               status="production", prod_w=2.0, delta=10837, tested="2026-03-09"),
            _f("funding_rate", "derivatives", "mega_factor_discovery", "score_core_funding",
               status="production", prod_w=2.0, delta=1218, tested="2026-03-09"),
            _f("ob_combined", "orderbook", "test_orderbook_factor", "score_ob_combined",
               status="production", prod_w=2.0, delta=1274, tested="2026-03-09"),
            _f("etf_flows", "institutional", "mega_factor_discovery", "score_core_etf",
               status="production", prod_w=1.0, delta=1654, tested="2026-03-09"),
            _f("basis_contrarian", "derivatives", "test_new_factors", "score_basis",
               status="production", prod_w=1.5, delta=1709, tested="2026-03-09"),
            _f("tick_liq", "derivatives", "test_new_factors", "score_tick_liq",
               status="production", prod_w=2.0, delta=657, tested="2026-03-09"),
            _f("oi_divergence", "derivatives", "mega_factor_discovery", "score_core_oi",
               status="production", prod_w=0.5, delta=493, tested="2026-03-09"),
            _f("whale_alerts", "onchain", "mega_factor_discovery", "score_core_whale",
               status="production", prod_w=1.5, delta=214, tested="2026-03-09"),

            # ── TESTED POSITIVE (6 factors) ──
            _f("stable_supply", "onchain", "test_v4_factors", "score_stable_supply",
               status="tested_positive", delta=1337, tested="2026-03-09",
               notes="Best v4 addition, but overlaps short_bias. SKIP for now."),
            _f("cvd_contrarian", "derivatives", "test_phase1_factors", "score_cvd_contrarian",
               status="tested_positive", delta=667, tested="2026-03-09",
               notes="Modest lift, failed stepwise."),
            _f("macro_risk_off", "macro", "test_phase1_factors", "score_macro_risk_off",
               status="tested_positive", delta=785, tested="2026-03-09",
               notes="Modest lift, failed stepwise."),
            _f("dvol_level", "volatility", "test_v4_factors", "score_dvol_level",
               status="tested_positive", delta=343, tested="2026-03-09"),
            _f("dvol_change", "volatility", "test_v4_factors", "score_dvol_change",
               status="tested_positive", delta=297, tested="2026-03-09"),
            _f("hashrate", "onchain", "test_v4_factors", "score_hashrate",
               status="tested_positive", delta=345, tested="2026-03-09"),

            # ── TESTED NEGATIVE (8 factors) ──
            _f("fear_greed", "sentiment", "mega_factor_discovery", "score_core_fg",
               status="tested_negative", delta=-500, tested="2026-03-09",
               notes="Hurts v3. Daily data = noise on 15m."),
            _f("taker_ratio", "derivatives", "mega_factor_discovery", "score_core_taker",
               status="tested_negative", delta=-200, tested="2026-03-09",
               notes="Redundant with liquidation."),
            _f("ls_ratio", "derivatives", "mega_factor_discovery", "score_core_ls",
               status="tested_negative", delta=-150, tested="2026-03-09",
               notes="Redundant."),
            _f("active_addr", "onchain", "test_v4_factors", "score_active_addr",
               status="tested_negative", delta=-754, tested="2026-03-09"),
            _f("dex_ratio", "onchain", "test_v4_factors", "score_dex_ratio",
               status="tested_negative", delta=-53, tested="2026-03-09",
               notes="Was lookahead alpha, flipped negative after fix."),
            _f("basis_momentum", "derivatives", "test_new_factors", "score_basis_momentum",
               status="tested_negative", delta=-100, tested="2026-03-08"),
            _f("news_directional", "sentiment", "test_new_factors", "score_news",
               status="tested_negative", delta=-200, tested="2026-03-08"),
            _f("news_contrarian", "sentiment", "test_new_factors", "score_news_contrarian",
               status="tested_negative", delta=-100, tested="2026-03-08"),

            # ── TESTED NEGATIVE (structural) ──
            _f("displacement", "price_action", "test_new_factors", "score_displacement",
               status="tested_negative", delta=-300, tested="2026-03-08"),
            _f("fvg", "price_action", "test_new_factors", "score_fvg",
               status="tested_negative", delta=-250, tested="2026-03-08"),
            _f("sweep", "price_action", "test_new_factors", "score_sweep",
               status="tested_negative", delta=-350, tested="2026-03-08"),
            _f("btc_dominance", "macro", "test_phase1_factors", "score_btc_dominance",
               status="tested_negative", delta=-50, tested="2026-03-09"),

            # ── UNTESTED (ideas for future research) ──
            _f("skew_25d", "volatility", None, None,
               status="untested", notes="25-delta skew from Deribit options"),
            _f("put_call_ratio", "volatility", None, None,
               status="untested", notes="BTC options put/call ratio"),
            _f("gamma_exposure", "volatility", None, None,
               status="untested", notes="Market maker gamma exposure (GEX)"),
            _f("max_pain", "volatility", None, None,
               status="untested", notes="Options max pain strike price"),
        ]
        self._factors = {f["name"]: f for f in factors}
        log.info(f"Bootstrapped {len(self._factors)} factors")


def _f(name, category, scorer_module, scorer_function, *,
       status="untested", prod_w=None, delta=None, tested=None, notes=None):
    """Helper to build a factor definition."""
    return {
        "name": name,
        "category": category,
        "scorer_module": scorer_module,
        "scorer_function": scorer_function,
        "weight_range": [0.5, 1.0, 1.5, 2.0],
        "status": status,
        "production_weight": prod_w,
        "best_weight": prod_w,
        "best_delta_pnl": delta,
        "best_pnl": None,
        "last_tested": tested,
        "test_metrics": {},
        "notes": notes,
    }
