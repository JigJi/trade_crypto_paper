"""Experiment Engine - thin wrapper around existing backtest functions.

Reuses the existing backtest infrastructure, adds experiment tracking.
"""
import json
import logging
import random
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from research.config import (
    EXPERIMENTS_DIR, EXPERIMENT_REGISTRY_PATH,
    WEIGHTS_TO_TEST, INIT_EQUITY, BUDGET_USDT, LEVERAGE,
)

log = logging.getLogger(__name__)


class ExperimentEngine:
    """Run and track experiments using existing backtest code."""

    def __init__(self):
        self._registry: list[dict] = []
        self._load_registry()
        # Lazy-loaded modules
        self._bt = None
        self._db_data = None
        self._btc_ohlcv = None
        self._btc_df = None
        self._btc_score = None
        self._progress_callback = None  # for dashboard updates

    def set_progress_callback(self, fn):
        """Set callback for real-time progress updates: fn(pct, message)."""
        self._progress_callback = fn

    def _emit(self, pct: float, msg: str):
        if self._progress_callback:
            self._progress_callback(pct, msg)

    # ── Data Loading (reuses backtest engine) ─────────────────
    def _ensure_backtest_module(self):
        if self._bt is None:
            import importlib
            import sys
            from research.config import BASE_DIR
            if str(BASE_DIR) not in sys.path:
                sys.path.insert(0, str(BASE_DIR))
            self._bt = importlib.import_module("backtest_15m_btc_led_alts")

    def load_data(self, force=False):
        """Load BTC OHLCV + DB data + build features + compute v3 score."""
        if self._btc_score is not None and not force:
            return
        self._ensure_backtest_module()
        bt = self._bt

        self._emit(5, "Loading BTC OHLCV data...")
        self._btc_ohlcv = bt.fetch_binance_15m("BTCUSDT", years=3)

        self._emit(15, "Loading DB factor data...")
        self._db_data = bt.load_btc_db_data()

        self._emit(25, "Building BTC features...")
        self._btc_df = bt.build_btc_features(self._btc_ohlcv, self._db_data)

        self._emit(30, "Computing v3 composite score...")
        try:
            from paper_trading.config import COMPOSITE_WEIGHTS, V3_EXTRA_WEIGHTS
            from paper_trading.strategy import score_ob_combined, score_basis_contrarian, score_tick_liq
            self._btc_score = bt.compute_btc_composite_score(self._btc_df, COMPOSITE_WEIGHTS)
            self._btc_score = self._btc_score + score_ob_combined(self._btc_df, weight=V3_EXTRA_WEIGHTS["ob_combined"])
            self._btc_score = self._btc_score + score_basis_contrarian(self._btc_df, weight=V3_EXTRA_WEIGHTS["basis_contrarian"])
            self._btc_score = self._btc_score + score_tick_liq(self._btc_df, weight=V3_EXTRA_WEIGHTS["tick_liq"])
        except ImportError:
            self._btc_score = bt.compute_btc_composite_score(self._btc_df, {})

        self._emit(35, "Data loaded.")
        log.info(f"Data loaded: {len(self._btc_df)} bars, score range [{self._btc_score.min():.1f}, {self._btc_score.max():.1f}]")

    def get_btc_data(self):
        """Return (btc_df, btc_score) after ensuring loaded."""
        self.load_data()
        return self._btc_df, self._btc_score

    # ── Test Single Factor ────────────────────────────────────
    def test_factor(self, factor_name: str, scorer_fn, weights: list[float] = None,
                    coins: list[str] = None, oos_start: str = "2026-01-01",
                    oos_end: str = "2026-03-31") -> dict:
        """Test a single factor at multiple weights against v3 baseline.

        Returns: {factor, results: [{weight, total_pnl, delta, coins: {}}], best_weight, best_delta}
        """
        self.load_data()
        bt = self._bt
        weights = weights or WEIGHTS_TO_TEST
        if coins is None:
            coins = self._pick_diverse_coins(8)

        # Baseline (no addon)
        baseline_pnl = self._run_all_coins(self._btc_score, coins, oos_start, oos_end)
        self._emit(40, f"Baseline PnL: ${baseline_pnl:.0f}")

        results = []
        for i, w in enumerate(weights):
            self._emit(40 + 50 * (i + 1) / len(weights),
                       f"Testing {factor_name} w={w}...")
            addon_score = scorer_fn(self._btc_df, weight=w)
            total_score = self._btc_score + addon_score
            pnl = self._run_all_coins(total_score, coins, oos_start, oos_end)
            results.append({
                "weight": w,
                "total_pnl": round(pnl, 2),
                "delta": round(pnl - baseline_pnl, 2),
            })

        best = max(results, key=lambda r: r["delta"])
        experiment = {
            "factor": factor_name,
            "baseline_pnl": round(baseline_pnl, 2),
            "results": results,
            "best_weight": best["weight"],
            "best_delta": best["delta"],
            "best_pnl": best["total_pnl"],
            "oos_period": f"{oos_start} to {oos_end}",
            "coins": [c.replace("USDT", "") for c in coins],
        }
        self._emit(95, f"Best: w={best['weight']}, delta=${best['delta']:.0f}")
        return experiment

    def test_factor_batch(self, factor_specs: list[dict],
                          coins: list[str] = None) -> dict:
        """Test multiple factors, return comparison table.

        factor_specs: [{"name": "xxx", "scorer_fn": callable}, ...]
        """
        self.load_data()
        comparison = []
        for i, spec in enumerate(factor_specs):
            self._emit(10 + 80 * i / len(factor_specs),
                       f"Testing {spec['name']} ({i+1}/{len(factor_specs)})...")
            result = self.test_factor(
                spec["name"], spec["scorer_fn"], coins=coins)
            comparison.append({
                "factor": spec["name"],
                "best_weight": result["best_weight"],
                "best_delta": result["best_delta"],
                "best_pnl": result["best_pnl"],
            })
        comparison.sort(key=lambda x: x["best_delta"], reverse=True)
        return {"comparison": comparison, "tested": len(factor_specs)}

    # ── Coin Screening ────────────────────────────────────────
    def run_coin_screening(self, top_n: int = 50,
                           oos_start: str = "2026-01-01",
                           oos_end: str = None) -> dict:
        """Screen top coins using v3 BTC score. Returns ranked list."""
        self.load_data()
        bt = self._bt

        self._emit(10, f"Fetching top {top_n} USDT perps...")
        try:
            from test_100_coins import get_top_usdt_perps
            top_coins = get_top_usdt_perps(top_n)
        except ImportError:
            log.error("test_100_coins.py not importable")
            return {"error": "test_100_coins not available"}

        btc_score_ts = pd.DataFrame({
            "ts": self._btc_df["date_time"],
            "btc_score": self._btc_score.values,
        })

        results = []
        for i, coin_info in enumerate(top_coins):
            symbol = coin_info["symbol"]
            self._emit(10 + 80 * (i + 1) / len(top_coins),
                       f"Screening {symbol} ({i+1}/{len(top_coins)})...")
            try:
                ohlcv = bt.fetch_binance_15m(symbol, years=1)
                alt_df = bt.build_alt_technicals(ohlcv)

                oos_mask = alt_df["date_time"] >= oos_start
                if oos_end:
                    oos_mask &= alt_df["date_time"] <= oos_end
                alt_oos = alt_df[oos_mask]
                if len(alt_oos) < 100:
                    continue

                signals, alt_merged = bt.generate_btc_led_signal(
                    btc_score_ts, alt_oos,
                    threshold=3.0,
                    use_alt_pa_filter=False)
                trades = bt.run_backtest(alt_merged, signals,
                                        sl_atr_mult=2.5, tp_atr_mult=4.0,
                                        cooldown_bars=4)
                if len(trades) > 0:
                    metrics = bt.calc_metrics(trades, len(alt_oos))
                    metrics["symbol"] = symbol
                    results.append(metrics)
            except Exception as e:
                log.warning(f"Skip {symbol}: {e}")

        results.sort(key=lambda x: x.get("net_pnl", 0), reverse=True)
        self._emit(95, f"Screened {len(results)} coins")
        return {"coins": results, "total_screened": len(top_coins)}

    # ── Internal Helpers ──────────────────────────────────────
    @staticmethod
    def _pick_diverse_coins(n: int = 8) -> list[str]:
        """Pick a diverse random sample of coins across v3/v4/v5.

        Always includes BTCUSDT, then samples from each version pool
        to ensure representation across all coin groups.
        """
        try:
            from paper_trading.config import COINS_V3, COINS_V4, COINS_V5
        except ImportError:
            return ["BTCUSDT", "XRPUSDT", "ADAUSDT", "DOTUSDT", "SUIUSDT", "FILUSDT"]

        # Always include BTC as anchor
        picked = ["BTCUSDT"]
        remaining = n - 1

        # Proportional sampling from each version pool (excluding BTC)
        pools = [
            [f"{c}USDT" for c in COINS_V3 if c != "BTC"],
            [f"{c}USDT" for c in COINS_V4],
            [f"{c}USDT" for c in COINS_V5],
        ]
        # At least 1 from each pool if possible, rest random from all
        for pool in pools:
            if pool and remaining > 0:
                picked.append(random.choice(pool))
                remaining -= 1

        # Fill remaining from all coins not yet picked
        all_coins = [f"{c}USDT" for c in COINS_V3 + COINS_V4 + COINS_V5 if f"{c}USDT" not in picked]
        if remaining > 0 and all_coins:
            picked.extend(random.sample(all_coins, min(remaining, len(all_coins))))

        log.info(f"Diverse coin sample ({len(picked)}): {[c.replace('USDT','') for c in picked]}")
        return picked

    def _run_all_coins(self, btc_score_series, coins, oos_start, oos_end):
        """Run backtest on all coins, sum PnL. Uses per-coin configs from paper_trading."""
        bt = self._bt
        total_pnl = 0
        btc_score_ts = pd.DataFrame({
            "ts": self._btc_df["date_time"],
            "btc_score": btc_score_series.values,
        })

        try:
            from paper_trading.config import COIN_CONFIGS
        except ImportError:
            COIN_CONFIGS = {}

        for symbol in coins:
            try:
                ohlcv = bt.fetch_binance_15m(symbol, years=3)
                alt_df = bt.build_alt_technicals(ohlcv)
                oos_mask = (alt_df["date_time"] >= oos_start) & (alt_df["date_time"] <= oos_end)
                alt_oos = alt_df[oos_mask]
                if len(alt_oos) < 100:
                    continue

                coin = symbol.replace("USDT", "")
                cfg = COIN_CONFIGS.get(coin, {})
                threshold = cfg.get("threshold", 3.0)
                use_pa = cfg.get("use_alt_pa_filter", False)
                sl = cfg.get("sl_atr_mult", 2.5)
                tp = cfg.get("tp_atr_mult", 4.0)
                cooldown = cfg.get("cooldown_bars", 4)

                signals, alt_merged = bt.generate_btc_led_signal(
                    btc_score_ts, alt_oos,
                    threshold=threshold,
                    use_alt_pa_filter=use_pa)
                trades = bt.run_backtest(alt_merged, signals,
                                        sl_atr_mult=sl, tp_atr_mult=tp,
                                        cooldown_bars=cooldown)
                if len(trades) > 0:
                    total_pnl += trades["pnl_net"].sum()
            except Exception as e:
                log.warning(f"Backtest {symbol} failed: {e}")

        return total_pnl

    # ── Experiment Tracking ───────────────────────────────────
    def save_experiment(self, experiment_type: str, result: dict,
                        description: str = "") -> str:
        """Save experiment to registry. Returns experiment_id."""
        exp_id = f"{experiment_type}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
        entry = {
            "experiment_id": exp_id,
            "date": datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
            "type": experiment_type,
            "description": description,
            "result": result,
        }
        self._registry.append(entry)
        self._save_registry()

        # Also save detailed JSON
        detail_path = EXPERIMENTS_DIR / f"{exp_id}.json"
        with open(detail_path, "w") as fh:
            json.dump(entry, fh, indent=2, default=str)

        log.info(f"Saved experiment {exp_id}")
        return exp_id

    def get_experiments(self, limit: int = 50) -> list[dict]:
        return self._registry[-limit:]

    def get_experiment(self, exp_id: str) -> Optional[dict]:
        for e in self._registry:
            if e["experiment_id"] == exp_id:
                return e
        return None

    def _load_registry(self):
        if EXPERIMENT_REGISTRY_PATH.exists():
            with open(EXPERIMENT_REGISTRY_PATH, "r") as fh:
                self._registry = json.load(fh)

    def _save_registry(self):
        EXPERIMENTS_DIR.mkdir(parents=True, exist_ok=True)
        with open(EXPERIMENT_REGISTRY_PATH, "w") as fh:
            json.dump(self._registry, fh, indent=2, default=str)
