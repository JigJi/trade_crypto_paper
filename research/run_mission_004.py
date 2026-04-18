"""
Mission #004: Option Greeks Deep Dive
======================================
Hypothesis: 26K rows of raw BTC option Greeks (Binance EAPI) contain
predictive signals for BTC price direction when aggregated into:
  1. ATM IV level (z-score) -- proxy for vol regime
  2. IV Skew (OTM put IV - OTM call IV) -- fear premium
  3. Put/Call OI ratio -- crowd positioning
  4. Net GEX (dealer gamma exposure) -- hedging flow direction

We also check the pre-computed Deribit options_data if available.
"""

import sys, json, logging
import numpy as np, pandas as pd
from pathlib import Path
from datetime import datetime, timedelta

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

import psycopg2
from research.config import get_pg_dsn

BKK_UTC_OFFSET = timedelta(hours=7)

# =============================================================================
# Phase 1: Data Exploration
# =============================================================================

def explore_data():
    """Probe option_greeks, option_instruments, option_quotes, options_data tables."""
    conn = psycopg2.connect(get_pg_dsn())
    results = {}

    queries = {
        "greeks_count": "SELECT count(*) FROM market_data.option_greeks",
        "greeks_range": "SELECT min(ts), max(ts) FROM market_data.option_greeks",
        "greeks_sample": """
            SELECT symbol, ts, iv_solved, delta, gamma, theta, vega, rho
            FROM market_data.option_greeks
            ORDER BY ts DESC LIMIT 5
        """,
        "greeks_daily_count": """
            SELECT date_trunc('day', ts) as day, count(*)
            FROM market_data.option_greeks
            GROUP BY 1 ORDER BY 1
        """,
        "instruments_count": "SELECT count(*) FROM market_data.option_instruments",
        "instruments_sample": """
            SELECT symbol, underlying, side, strike, expiry
            FROM market_data.option_instruments LIMIT 10
        """,
        "quotes_count": """
            SELECT count(*) FROM market_data.option_quotes
        """,
        "quotes_range": """
            SELECT min(ts), max(ts) FROM market_data.option_quotes
        """,
        "quotes_sample": """
            SELECT symbol, ts, mark_price, volume, oi
            FROM market_data.option_quotes
            ORDER BY ts DESC LIMIT 5
        """,
        "options_data_count": "SELECT count(*) FROM market_data.options_data",
        "options_data_range": "SELECT min(ts), max(ts) FROM market_data.options_data",
        "options_data_sample": """
            SELECT ts, dvol, skew_25d, put_call_ratio, max_pain, gex_net, spot_price
            FROM market_data.options_data
            ORDER BY ts DESC LIMIT 5
        """,
    }

    for name, sql in queries.items():
        try:
            df = pd.read_sql(sql, conn)
            results[name] = df
            log.info(f"{name}: {len(df)} rows")
            if len(df) > 0:
                log.info(f"  {df.to_string(index=False)[:300]}")
        except Exception as e:
            results[name] = f"ERROR: {e}"
            log.warning(f"{name}: {e}")

    conn.close()
    return results


# =============================================================================
# Phase 2: Load and aggregate option data
# =============================================================================

def load_option_greeks():
    """Load all option_greeks + instruments + quotes, return merged DataFrame."""
    conn = psycopg2.connect(get_pg_dsn())

    # Load greeks with instrument info via JOIN
    sql = """
        SELECT g.ts, g.symbol, g.iv_solved, g.delta, g.gamma, g.theta, g.vega,
               i.side, i.strike, i.expiry,
               q.oi, q.mark_price, q.volume as opt_volume
        FROM market_data.option_greeks g
        JOIN market_data.option_instruments i ON g.symbol = i.symbol
        LEFT JOIN market_data.option_quotes q ON g.symbol = q.symbol AND g.ts = q.ts
        ORDER BY g.ts
    """
    try:
        df = pd.read_sql(sql, conn, parse_dates=["ts", "expiry"])
        log.info(f"Loaded {len(df)} option_greeks rows with instruments+quotes")
    except Exception as e:
        log.error(f"Failed to load option_greeks: {e}")
        conn.close()
        return None

    conn.close()

    if len(df) == 0:
        return None

    # Fix timezone: DB stores Bangkok time
    for col in ["ts", "expiry"]:
        if df[col].dt.tz is not None:
            df[col] = df[col].dt.tz_localize(None)
        df[col] = df[col] - BKK_UTC_OFFSET

    return df


def load_deribit_options():
    """Load pre-computed Deribit options_data."""
    conn = psycopg2.connect(get_pg_dsn())
    try:
        df = pd.read_sql("""
            SELECT ts, dvol, skew_25d, put_call_ratio, max_pain, gex_net,
                   spot_price, total_oi_calls, total_oi_puts
            FROM market_data.options_data ORDER BY ts
        """, conn, parse_dates=["ts"])
        log.info(f"Loaded {len(df)} Deribit options_data rows")
    except Exception as e:
        log.error(f"Failed to load options_data: {e}")
        conn.close()
        return None

    conn.close()

    if len(df) == 0:
        return None

    if df["ts"].dt.tz is not None:
        df["ts"] = df["ts"].dt.tz_localize(None)
    df["ts"] = df["ts"] - BKK_UTC_OFFSET

    return df


def aggregate_greeks(df):
    """
    Aggregate per-strike Greeks into time-series signals.
    Groups by ts, computes ATM IV, IV skew, put/call OI ratio, net GEX.
    """
    if df is None or len(df) == 0:
        return None

    # Need spot price to determine ATM -- use mark_price of ATM option or infer
    # We don't have spot directly, but we can estimate from put-call parity or
    # use the strike closest to where delta ~ 0.5 for calls
    records = []

    for ts, grp in df.groupby("ts"):
        if len(grp) < 4:
            continue

        calls = grp[grp["side"] == "CALL"].copy()
        puts = grp[grp["side"] == "PUT"].copy()

        if len(calls) == 0 or len(puts) == 0:
            continue

        # Estimate spot: ATM call has delta ~ 0.5
        atm_call = calls.iloc[(calls["delta"] - 0.5).abs().argsort()[:1]]
        spot_est = atm_call["strike"].values[0] if len(atm_call) > 0 else np.nan

        if np.isnan(spot_est):
            continue

        # 1. ATM IV: average IV of options with delta closest to +/- 0.5
        atm_call_iv = calls.iloc[(calls["delta"] - 0.5).abs().argsort()[:1]]["iv_solved"].values
        atm_put_iv = puts.iloc[(puts["delta"].abs() - 0.5).abs().argsort()[:1]]["iv_solved"].values
        atm_iv = np.nanmean([*atm_call_iv, *atm_put_iv]) if len(atm_call_iv) > 0 else np.nan

        # 2. 25-delta skew: IV(25d put) - IV(25d call)
        # 25-delta call: delta ~ 0.25, 25-delta put: delta ~ -0.25
        call_25d = calls.iloc[(calls["delta"] - 0.25).abs().argsort()[:1]]
        put_25d = puts.iloc[(puts["delta"] + 0.25).abs().argsort()[:1]]
        skew_25d = np.nan
        if len(put_25d) > 0 and len(call_25d) > 0:
            put_iv = put_25d["iv_solved"].values[0]
            call_iv = call_25d["iv_solved"].values[0]
            if put_iv > 0 and call_iv > 0:
                skew_25d = put_iv - call_iv

        # 3. Put/Call OI ratio (if OI available)
        call_oi = calls["oi"].sum() if "oi" in calls.columns and calls["oi"].notna().any() else np.nan
        put_oi = puts["oi"].sum() if "oi" in puts.columns and puts["oi"].notna().any() else np.nan
        pc_ratio = put_oi / call_oi if call_oi > 0 and not np.isnan(call_oi) else np.nan

        # 4. Net GEX (dealer perspective: -call_gamma + put_gamma, weighted by OI)
        gex_net = np.nan
        if "oi" in grp.columns and grp["oi"].notna().any():
            call_gex = (calls["gamma"] * calls["oi"] * spot_est**2 * 0.01).sum()
            put_gex = (puts["gamma"] * puts["oi"] * spot_est**2 * 0.01).sum()
            gex_net = -call_gex + put_gex  # dealer perspective

        # 5. Total gamma (unsigned, as vol indicator)
        total_gamma = grp["gamma"].abs().sum()

        # 6. Weighted vega (OI-weighted if available)
        if "oi" in grp.columns and grp["oi"].notna().any():
            weighted_vega = (grp["vega"] * grp["oi"]).sum()
        else:
            weighted_vega = grp["vega"].sum()

        records.append({
            "ts": ts,
            "spot_est": spot_est,
            "atm_iv": atm_iv,
            "skew_25d": skew_25d,
            "pc_ratio": pc_ratio,
            "gex_net": gex_net,
            "total_gamma": total_gamma,
            "weighted_vega": weighted_vega,
            "n_options": len(grp),
            "n_calls": len(calls),
            "n_puts": len(puts),
        })

    if not records:
        return None

    agg = pd.DataFrame(records)
    log.info(f"Aggregated to {len(agg)} timestamps, date range: {agg['ts'].min()} to {agg['ts'].max()}")
    return agg


# =============================================================================
# Phase 3: Compute factor scores (z-score based, contrarian)
# =============================================================================

def compute_option_scores(agg_df, lookback=480):
    """
    Compute z-score based signals from aggregated option data.
    lookback=480 = 5 days at 15min resolution (adjust if data is 5min).
    """
    df = agg_df.copy()

    # Detect resolution and adjust lookback
    if len(df) > 1:
        median_gap = df["ts"].diff().dropna().median().total_seconds() / 60
        log.info(f"Data resolution: ~{median_gap:.0f} min")
        # Adjust lookback: we want ~5 days of history
        # At ~627 min resolution, 5 days = 5*24*60/627 = ~11 points
        # Use 30 points (~12 days) for more stable z-scores
        points_per_day = 24 * 60 / max(median_gap, 1)
        lookback = max(int(points_per_day * 12), 10)  # 12 days of data
        min_periods = max(int(points_per_day * 3), 5)  # 3 days minimum
        log.info(f"Adjusted lookback={lookback}, min_periods={min_periods}")
    else:
        log.warning("Only 1 data point, cannot compute scores")
        return df

    for col in ["atm_iv", "skew_25d", "pc_ratio", "gex_net"]:
        if col in df.columns:
            series = df[col].astype(float)
            roll_mean = series.rolling(lookback, min_periods=min_periods).mean()
            roll_std = series.rolling(lookback, min_periods=min_periods).std()
            df[f"{col}_zscore"] = (series - roll_mean) / roll_std.replace(0, np.nan)

    # Signal scores (contrarian logic):
    # ATM IV: high IV z-score -> market stressed -> contrarian LONG
    if "atm_iv_zscore" in df.columns:
        z = df["atm_iv_zscore"]
        df["score_atm_iv"] = np.where(z > 2.0, 1.0,
                             np.where(z > 1.0, 0.5,
                             np.where(z < -2.0, -0.5,
                             np.where(z < -1.0, -0.25, 0.0))))

    # Skew: high skew (put premium) -> fear -> contrarian LONG
    if "skew_25d_zscore" in df.columns:
        z = df["skew_25d_zscore"]
        df["score_skew"] = np.where(z > 2.0, 1.0,
                           np.where(z > 1.0, 0.5,
                           np.where(z < -2.0, -1.0,
                           np.where(z < -1.0, -0.5, 0.0))))

    # P/C ratio: high -> fear -> contrarian LONG
    if "pc_ratio_zscore" in df.columns:
        z = df["pc_ratio_zscore"]
        df["score_pc_ratio"] = np.where(z > 2.0, 1.0,
                               np.where(z > 1.0, 0.5,
                               np.where(z < -2.0, -1.0,
                               np.where(z < -1.0, -0.5, 0.0))))

    # GEX: positive GEX -> dealers dampen vol (mean-revert); negative -> amplify (momentum)
    # Not a directional signal -- marks regime. For now: extreme negative GEX -> vol expansion -> contrarian LONG
    if "gex_net_zscore" in df.columns:
        z = df["gex_net_zscore"]
        df["score_gex"] = np.where(z < -2.0, 1.0,
                          np.where(z < -1.0, 0.5,
                          np.where(z > 2.0, -0.5,
                          np.where(z > 1.0, -0.25, 0.0))))

    return df


# =============================================================================
# Phase 4: Backtest each option factor against v3 baseline
# =============================================================================

def backtest_option_factor(factor_name, option_scores_df, weight=1.0):
    """
    Run v3 backtest with an additional option-based factor.
    Returns (trades_df, total_pnl, delta_pnl vs baseline).
    """
    import backtest_15m_btc_led_alts as bt
    from paper_trading.config import COMPOSITE_WEIGHTS, V3_EXTRA_WEIGHTS, COIN_CONFIGS
    from paper_trading.strategy import score_ob_combined, score_basis_contrarian, score_tick_liq

    # Build BTC features and v3 score
    btc_ohlcv = bt.fetch_binance_15m("BTCUSDT", years=3)
    if "date_time" in btc_ohlcv.columns:
        btc_ohlcv = btc_ohlcv.rename(columns={"date_time": "ts"})

    db_data = bt.load_btc_db_data()
    btc_df = bt.build_btc_features(btc_ohlcv, db_data)

    btc_score = bt.compute_btc_composite_score(btc_df, COMPOSITE_WEIGHTS)
    btc_score = btc_score + score_ob_combined(btc_df, weight=V3_EXTRA_WEIGHTS["ob_combined"])
    btc_score = btc_score + score_basis_contrarian(btc_df, weight=V3_EXTRA_WEIGHTS["basis_contrarian"])
    btc_score = btc_score + score_tick_liq(btc_df, weight=V3_EXTRA_WEIGHTS["tick_liq"])

    # Extract numpy arrays to avoid pandas index alignment issues
    btc_score_vals = btc_score.values if hasattr(btc_score, 'values') else np.array(btc_score)
    btc_ts_vals = btc_df["ts"].values

    # Add option factor
    score_col = f"score_{factor_name}"
    if score_col not in option_scores_df.columns:
        log.error(f"Column {score_col} not found in option_scores_df")
        return None, 0, 0

    opt_ts = option_scores_df[["ts", score_col]].dropna().copy()
    opt_ts = opt_ts.sort_values("ts")

    # Resample to 15min (forward-fill from sparse ~10h data)
    opt_15m = opt_ts.set_index("ts").resample("15min").last().ffill().reset_index()

    # Merge with btc_df using generous tolerance (data is ~10h sparse)
    merged = pd.merge_asof(
        btc_df[["ts"]].sort_values("ts"),
        opt_15m.sort_values("ts"),
        on="ts",
        direction="backward",
        tolerance=pd.Timedelta("24h")  # 24h tolerance for ~10h resolution data
    )

    option_signal = merged[score_col].fillna(0).values * weight
    btc_score_with_option = btc_score_vals + option_signal

    btc_score_ts = pd.Series(btc_score_with_option, index=btc_ts_vals, name="btc_score")

    # Also compute baseline (without option factor)
    btc_score_baseline = pd.Series(btc_score_vals, index=btc_ts_vals, name="btc_score")

    # Backtest
    coins = ["BTCUSDT", "XRPUSDT", "ADAUSDT", "DOTUSDT", "SUIUSDT", "FILUSDT"]
    oos_start, oos_end = "2026-01-01", "2026-03-15"

    def run_for_score(score_series):
        all_trades = []
        for symbol in coins:
            coin = symbol.replace("USDT", "")
            ohlcv = bt.fetch_binance_15m(symbol, years=3)
            if "date_time" in ohlcv.columns:
                ohlcv = ohlcv.rename(columns={"date_time": "ts"})
            alt_df = bt.build_alt_technicals(ohlcv)
            oos_mask = (alt_df["ts"] >= oos_start) & (alt_df["ts"] <= oos_end)

            cfg = COIN_CONFIGS.get(coin, {})
            signals, alt_merged = bt.generate_btc_led_signal(
                score_series, alt_df[oos_mask],
                threshold=cfg.get("threshold", 3.0),
                use_alt_pa_filter=cfg.get("use_alt_pa_filter", False))
            trades = bt.run_backtest(alt_merged, signals,
                                     sl_atr_mult=cfg.get("sl_atr_mult", 2.5),
                                     tp_atr_mult=cfg.get("tp_atr_mult", 4.0),
                                     cooldown_bars=cfg.get("cooldown_bars", 4))
            if len(trades) > 0:
                trades["coin"] = coin
                all_trades.append(trades)

        if all_trades:
            return pd.concat(all_trades, ignore_index=True)
        return pd.DataFrame()

    trades_test = run_for_score(btc_score_ts)
    trades_base = run_for_score(btc_score_baseline)

    pnl_test = trades_test["pnl_net"].sum() if len(trades_test) > 0 else 0
    pnl_base = trades_base["pnl_net"].sum() if len(trades_base) > 0 else 0
    delta = pnl_test - pnl_base

    return trades_test, pnl_test, delta, pnl_base, trades_base


# =============================================================================
# Main
# =============================================================================

def main():
    log.info("=" * 60)
    log.info("Mission #004: Option Greeks Deep Dive")
    log.info("=" * 60)

    mission_result = {
        "mission_id": "mission_004_option_greeks",
        "started_at": datetime.utcnow().isoformat(),
        "phases": {}
    }

    # ---- Phase 1: Data Exploration ----
    log.info("\n--- Phase 1: Data Exploration ---")
    exploration = explore_data()

    # Extract key metrics
    phase1 = {}
    for key in ["greeks_count", "greeks_range", "instruments_count",
                "quotes_count", "quotes_range", "options_data_count", "options_data_range"]:
        val = exploration.get(key, "N/A")
        if isinstance(val, pd.DataFrame) and len(val) > 0:
            phase1[key] = val.values.tolist()
        else:
            phase1[key] = str(val)
    mission_result["phases"]["exploration"] = phase1

    # Check daily distribution
    daily = exploration.get("greeks_daily_count")
    if isinstance(daily, pd.DataFrame) and len(daily) > 0:
        log.info(f"Greeks data covers {len(daily)} days")
        log.info(f"  Avg rows/day: {daily.iloc[:, 1].mean():.0f}")
        log.info(f"  Min rows/day: {daily.iloc[:, 1].min()}")
        log.info(f"  Max rows/day: {daily.iloc[:, 1].max()}")
        phase1["days_covered"] = len(daily)
        phase1["avg_rows_per_day"] = float(daily.iloc[:, 1].mean())
    else:
        log.warning("No daily distribution data available")

    # ---- Phase 2: Load + Aggregate Greeks ----
    log.info("\n--- Phase 2: Load & Aggregate Option Greeks ---")
    greeks_df = load_option_greeks()

    if greeks_df is not None and len(greeks_df) > 0:
        log.info(f"Loaded {len(greeks_df)} rows")
        log.info(f"Columns: {list(greeks_df.columns)}")
        log.info(f"Date range: {greeks_df['ts'].min()} to {greeks_df['ts'].max()}")
        log.info(f"Unique timestamps: {greeks_df['ts'].nunique()}")
        log.info(f"Unique symbols: {greeks_df['symbol'].nunique()}")
        log.info(f"Sides: {greeks_df['side'].value_counts().to_dict()}")

        # Check OI availability
        oi_available = "oi" in greeks_df.columns and greeks_df["oi"].notna().sum() > 0
        log.info(f"OI data available: {oi_available} ({greeks_df['oi'].notna().sum()} non-null)")

        agg_df = aggregate_greeks(greeks_df)

        if agg_df is not None and len(agg_df) > 0:
            log.info(f"\nAggregated signals ({len(agg_df)} timestamps):")
            for col in ["atm_iv", "skew_25d", "pc_ratio", "gex_net"]:
                if col in agg_df.columns:
                    s = agg_df[col].dropna()
                    log.info(f"  {col}: {len(s)} valid, mean={s.mean():.4f}, std={s.std():.4f}")

            mission_result["phases"]["aggregation"] = {
                "n_timestamps": len(agg_df),
                "date_range": [str(agg_df["ts"].min()), str(agg_df["ts"].max())],
                "oi_available": oi_available,
                "metrics": {}
            }
            for col in ["atm_iv", "skew_25d", "pc_ratio", "gex_net"]:
                if col in agg_df.columns:
                    s = agg_df[col].dropna()
                    mission_result["phases"]["aggregation"]["metrics"][col] = {
                        "valid_count": int(len(s)),
                        "mean": float(s.mean()) if len(s) > 0 else None,
                        "std": float(s.std()) if len(s) > 0 else None,
                        "min": float(s.min()) if len(s) > 0 else None,
                        "max": float(s.max()) if len(s) > 0 else None,
                    }

            # ---- Phase 3: Compute Scores ----
            log.info("\n--- Phase 3: Compute Option Scores ---")
            scored_df = compute_option_scores(agg_df)
            score_cols = [c for c in scored_df.columns if c.startswith("score_")]
            log.info(f"Score columns: {score_cols}")

            for sc in score_cols:
                nz = (scored_df[sc] != 0).sum()
                log.info(f"  {sc}: {nz} non-zero signals ({nz/len(scored_df)*100:.1f}%)")

            mission_result["phases"]["scoring"] = {
                "score_columns": score_cols,
                "signal_density": {
                    sc: float((scored_df[sc] != 0).sum() / len(scored_df))
                    for sc in score_cols
                }
            }

            # ---- Phase 4: Backtest ----
            log.info("\n--- Phase 4: Backtest Option Factors ---")

            # Only backtest if we have enough data (at least 30 days)
            date_span = (agg_df["ts"].max() - agg_df["ts"].min()).days
            if date_span < 30:
                log.warning(f"Only {date_span} days of data -- too short for reliable backtest")
                log.info("Running backtest anyway for exploratory purposes...")

            backtest_results = {}
            weights_to_test = [0.5, 1.0, 1.5, 2.0]

            for factor in ["atm_iv", "skew", "pc_ratio", "gex"]:
                sc = f"score_{factor}"
                if sc not in scored_df.columns:
                    continue
                nz = (scored_df[sc] != 0).sum()
                if nz < 10:
                    log.info(f"Skipping {factor}: only {nz} non-zero signals")
                    backtest_results[factor] = {"skipped": True, "reason": f"only {nz} signals"}
                    continue

                log.info(f"\nBacktesting: {factor}")
                best_delta = -99999
                best_weight = None
                factor_results = []

                for w in weights_to_test:
                    try:
                        trades, pnl, delta, pnl_base, _ = backtest_option_factor(
                            factor, scored_df, weight=w)
                        n_trades = len(trades) if trades is not None else 0
                        wr = (trades["pnl_net"] > 0).mean() * 100 if n_trades > 0 else 0

                        log.info(f"  w={w}: PnL=${pnl:.0f}, delta=${delta:.0f}, "
                                f"trades={n_trades}, WR={wr:.1f}%, baseline=${pnl_base:.0f}")

                        factor_results.append({
                            "weight": w,
                            "pnl": float(pnl),
                            "delta": float(delta),
                            "baseline": float(pnl_base),
                            "trades": n_trades,
                            "wr": float(wr),
                        })

                        if delta > best_delta:
                            best_delta = delta
                            best_weight = w
                    except Exception as e:
                        log.error(f"  w={w}: ERROR - {e}")
                        factor_results.append({"weight": w, "error": str(e)})

                backtest_results[factor] = {
                    "best_weight": best_weight,
                    "best_delta": float(best_delta) if best_delta > -99999 else None,
                    "results": factor_results,
                    "verdict": "positive" if best_delta > 0 else "negative"
                }
                log.info(f"  BEST: w={best_weight}, delta=${best_delta:.0f}")

            mission_result["phases"]["backtest"] = backtest_results

        else:
            log.warning("Aggregation returned no data")
            mission_result["phases"]["aggregation"] = {"error": "no data after aggregation"}
    else:
        log.warning("No option_greeks data loaded")
        mission_result["phases"]["greeks"] = {"error": "no data"}

    # ---- Also check Deribit data ----
    log.info("\n--- Bonus: Check Deribit pre-computed data ---")
    deribit_df = load_deribit_options()
    if deribit_df is not None and len(deribit_df) > 0:
        log.info(f"Deribit data: {len(deribit_df)} rows, "
                f"{deribit_df['ts'].min()} to {deribit_df['ts'].max()}")
        mission_result["phases"]["deribit"] = {
            "rows": len(deribit_df),
            "date_range": [str(deribit_df["ts"].min()), str(deribit_df["ts"].max())],
            "columns": list(deribit_df.columns),
            "skew_25d_stats": {
                "mean": float(deribit_df["skew_25d"].mean()) if deribit_df["skew_25d"].notna().any() else None,
                "std": float(deribit_df["skew_25d"].std()) if deribit_df["skew_25d"].notna().any() else None,
            },
            "note": "Too short for backtest but validates signal structure"
        }
    else:
        mission_result["phases"]["deribit"] = {"rows": 0, "note": "no data"}

    # ---- Save Results ----
    mission_result["finished_at"] = datetime.utcnow().isoformat()

    # Determine overall verdict
    bt_results = mission_result.get("phases", {}).get("backtest", {})
    positive_factors = [f for f, r in bt_results.items()
                       if isinstance(r, dict) and r.get("verdict") == "positive"]
    negative_factors = [f for f, r in bt_results.items()
                       if isinstance(r, dict) and r.get("verdict") == "negative"]
    skipped_factors = [f for f, r in bt_results.items()
                      if isinstance(r, dict) and r.get("skipped")]

    mission_result["summary"] = {
        "positive_factors": positive_factors,
        "negative_factors": negative_factors,
        "skipped_factors": skipped_factors,
        "overall_verdict": "significant" if len(positive_factors) > 0 else
                          "no_data" if not bt_results else "negative"
    }

    # Save JSON
    json_path = BASE_DIR / "missions" / "mission_004_option_greeks.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(mission_result, f, indent=2, default=str, ensure_ascii=False)
    log.info(f"\nSaved results to {json_path}")

    # Save experiment copy
    exp_path = BASE_DIR / "experiments" / "mission_004_option_greeks.json"
    with open(exp_path, "w", encoding="utf-8") as f:
        json.dump(mission_result, f, indent=2, default=str, ensure_ascii=False)

    # Print summary
    log.info("\n" + "=" * 60)
    log.info("MISSION #004 SUMMARY")
    log.info("=" * 60)
    log.info(f"Positive factors: {positive_factors}")
    log.info(f"Negative factors: {negative_factors}")
    log.info(f"Skipped factors: {skipped_factors}")
    log.info(f"Overall: {mission_result['summary']['overall_verdict']}")

    return mission_result


if __name__ == "__main__":
    result = main()
