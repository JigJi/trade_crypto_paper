"""
Daily Report Generator
========================
Generates daily summary report with trades, equity, and performance metrics.
"""

import logging
from datetime import datetime, timedelta

import numpy as np

from .config import LOG_DIR, INIT_EQUITY, COINS, BINANCE_TESTNET_KEY, BINANCE_TESTNET_SECRET
from . import state_db as db
from .exchange import TestnetExchange

logger = logging.getLogger(__name__)


def generate_daily_report(date_str: str = None) -> str:
    """
    Generate daily summary report.
    date_str: YYYY-MM-DD (default: yesterday)
    Returns path to saved report file.
    """
    if date_str is None:
        yesterday = datetime.now() - timedelta(days=1)
        date_str = yesterday.strftime("%Y-%m-%d")

    today_start = f"{date_str}T00:00:00"
    today_end = f"{date_str}T23:59:59"
    tomorrow = (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")

    # Get today's trades
    all_trades = db.get_trades()
    today_trades = [t for t in all_trades if today_start <= t["exit_time"] <= today_end]

    # Get open positions from exchange
    open_positions = []
    try:
        if BINANCE_TESTNET_KEY and BINANCE_TESTNET_SECRET:
            ex = TestnetExchange(BINANCE_TESTNET_KEY, BINANCE_TESTNET_SECRET)
            raw_pos = ex.get_open_positions()
            for p in raw_pos:
                pos_amt = float(p.get("positionAmt", 0))
                open_positions.append({
                    "coin": p["symbol"].replace("USDT", ""),
                    "direction": 1 if pos_amt > 0 else -1,
                    "entry_price": float(p.get("entryPrice", 0)),
                    "unrealized_pnl": float(p.get("unRealizedProfit", 0)),
                    "leverage": p.get("leverage", ""),
                })
    except Exception as e:
        logger.warning(f"Failed to get exchange positions: {e}")

    # Get equity curve for today
    eq_curve = db.get_equity_curve(since=today_start)

    # Current equity
    current_equity = db.get_current_equity()

    # Build report
    md = []
    md.append(f"# Paper Trading Daily Report -- {date_str}")
    md.append(f"\n**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M BKK')}")
    md.append(f"**Strategy:** BTC-Led ({', '.join(COINS)})")
    md.append(f"**Current Equity:** ${current_equity:,.2f}")

    # Today's trades
    md.append(f"\n## Today's Trades ({len(today_trades)})")
    if today_trades:
        md.append("| Coin | Dir | Entry | Exit | PnL | Exit Reason | BTC Score |")
        md.append("|------|-----|-------|------|-----|-------------|-----------|")
        for t in today_trades:
            dir_str = "LONG" if t["direction"] == 1 else "SHORT"
            md.append(
                f"| {t['coin']} | {dir_str} | {t['entry_price']:.4f} | "
                f"{t['exit_price']:.4f} | ${t['pnl_net']:+.2f} | "
                f"{t['exit_reason']} | {t.get('btc_score_entry', 0):.1f} |"
            )
        daily_pnl = sum(t["pnl_net"] for t in today_trades)
        daily_wins = sum(1 for t in today_trades if t["pnl_net"] > 0)
        md.append(f"\n**Daily PnL:** ${daily_pnl:+.2f}")
        md.append(f"**Win Rate:** {daily_wins}/{len(today_trades)} "
                  f"({daily_wins/len(today_trades)*100:.0f}%)")
    else:
        md.append("*No trades closed today.*")

    # Open positions
    md.append(f"\n## Open Positions ({len(open_positions)})")
    if open_positions:
        md.append("| Coin | Dir | Entry Price | Unrealized PnL | Leverage |")
        md.append("|------|-----|-------------|----------------|----------|")
        for p in open_positions:
            dir_str = "LONG" if p["direction"] == 1 else "SHORT"
            md.append(
                f"| {p['coin']} | {dir_str} | {p['entry_price']:.4f} | "
                f"${p.get('unrealized_pnl', 0):+.2f} | {p.get('leverage', '')}x |"
            )
    else:
        md.append("*No open positions.*")

    # Cumulative performance
    md.append("\n## Cumulative Performance")
    if all_trades:
        total_trades = len(all_trades)
        total_wins = sum(1 for t in all_trades if t["pnl_net"] > 0)
        total_losses = sum(1 for t in all_trades if t["pnl_net"] <= 0)
        total_pnl = sum(t["pnl_net"] for t in all_trades)
        sum_wins = sum(t["pnl_net"] for t in all_trades if t["pnl_net"] > 0)
        sum_losses = abs(sum(t["pnl_net"] for t in all_trades if t["pnl_net"] <= 0))
        pf = sum_wins / sum_losses if sum_losses > 0 else float("inf") if sum_wins > 0 else 0

        # Sharpe
        pnls = [t["pnl_net"] for t in all_trades]
        if len(pnls) > 1:
            rets = [p / INIT_EQUITY for p in pnls]
            sharpe = (np.mean(rets) / np.std(rets)) * np.sqrt(len(rets)) if np.std(rets) > 0 else 0
        else:
            sharpe = 0

        # Max drawdown
        eq_series = [INIT_EQUITY]
        for t in all_trades:
            eq_series.append(eq_series[-1] + t["pnl_net"])
        peak_eq = eq_series[0]
        max_dd = 0
        for eq in eq_series:
            peak_eq = max(peak_eq, eq)
            dd = (eq - peak_eq) / peak_eq * 100
            max_dd = min(max_dd, dd)

        md.append(f"| Metric | Value |")
        md.append(f"|--------|-------|")
        md.append(f"| Total Trades | {total_trades} |")
        md.append(f"| Win Rate | {total_wins}/{total_trades} ({total_wins/total_trades*100:.1f}%) |")
        md.append(f"| Profit Factor | {pf:.3f} |")
        md.append(f"| Net PnL | ${total_pnl:+,.2f} |")
        md.append(f"| Sharpe | {sharpe:.3f} |")
        md.append(f"| Max Drawdown | {max_dd:.2f}% |")
        md.append(f"| Equity | ${current_equity:,.2f} |")

        # Per-coin breakdown
        md.append("\n### Per-Coin Breakdown")
        md.append("| Coin | Trades | WR | PnL | Avg PnL |")
        md.append("|------|--------|-----|-----|---------|")
        for coin in COINS:
            coin_trades = [t for t in all_trades if t["coin"] == coin]
            if coin_trades:
                ct = len(coin_trades)
                cw = sum(1 for t in coin_trades if t["pnl_net"] > 0)
                cp = sum(t["pnl_net"] for t in coin_trades)
                md.append(
                    f"| {coin} | {ct} | {cw/ct*100:.0f}% | "
                    f"${cp:+,.2f} | ${cp/ct:+.2f} |"
                )
    else:
        md.append("*No trades yet.*")

    # Backtest comparison
    md.append("\n## vs Backtest Expectations")
    md.append("| Coin | Backtest OOS PnL | Backtest Sharpe | Paper PnL | Status |")
    md.append("|------|------------------|-----------------|-----------|--------|")
    bt_expectations = {
        "BTC": {"pnl": 68, "sharpe": 0.994},
        "XRP": {"pnl": 312, "sharpe": 1.731},
        "ADA": {"pnl": 244, "sharpe": 1.432},
        "DOT": {"pnl": 362, "sharpe": 1.606},
        "SUI": {"pnl": 308, "sharpe": 1.660},
        "FIL": {"pnl": 281, "sharpe": 1.599},
    }
    for coin in [c for c in COINS if c in bt_expectations]:
        bt = bt_expectations[coin]
        coin_trades = [t for t in all_trades if t["coin"] == coin]
        paper_pnl = sum(t["pnl_net"] for t in coin_trades) if coin_trades else 0
        status = "ON TRACK" if len(coin_trades) < 5 else ("OK" if paper_pnl > 0 else "UNDERPERFORMING")
        md.append(
            f"| {coin} | ${bt['pnl']:+,.0f} | {bt['sharpe']:.3f} | "
            f"${paper_pnl:+,.2f} | {status} |"
        )

    report_text = "\n".join(md)

    # Save to file
    report_path = LOG_DIR / f"daily_{date_str}.md"
    report_path.write_text(report_text, encoding="utf-8")
    logger.info(f"Daily report saved: {report_path}")

    return str(report_path)
