"""Query Binance Testnet API directly for real PnL verification."""
import os
from collections import defaultdict
from datetime import datetime, timezone

from dotenv import load_dotenv
from binance.client import Client

load_dotenv()
client = Client(
    os.getenv("BINANCE_TESTNET_KEY"),
    os.getenv("BINANCE_TESTNET_SECRET"),
    testnet=True,
)

# 1. Account balance
print("=== BINANCE TESTNET ACCOUNT ===")
balances = client.futures_account_balance()
for b in balances:
    if b["asset"] == "USDT":
        print(f"Balance:   ${float(b['balance']):,.2f}")
        print(f"Available: ${float(b['availableBalance']):,.2f}")
        print(f"Cross PnL: ${float(b['crossUnPnl']):,.2f}")
print()

# 2. Account info
acct = client.futures_account()
print(f"Total Wallet Balance:  ${float(acct['totalWalletBalance']):,.2f}")
print(f"Total Unrealized PnL:  ${float(acct['totalUnrealizedProfit']):,.2f}")
print(f"Total Margin Balance:  ${float(acct['totalMarginBalance']):,.2f}")
print()

# 3. Realized PnL today
print("=== REALIZED PNL TODAY (from income API) ===")
today_start = datetime(2026, 3, 16, 0, 0, 0, tzinfo=timezone.utc)
start_ms = int(today_start.timestamp() * 1000)

income = client.futures_income_history(
    incomeType="REALIZED_PNL", startTime=start_ms, limit=500
)
total_realized = sum(float(i["income"]) for i in income)
print(f"Entries: {len(income)} | Total: ${total_realized:+,.2f}")
print()

by_sym = defaultdict(float)
for i in income:
    by_sym[i["symbol"]] += float(i["income"])
print("Per symbol:")
for sym, pnl in sorted(by_sym.items(), key=lambda x: x[1], reverse=True):
    print(f"  {sym:16s} ${pnl:+8.2f}")
print()

# 4. Commissions today
comm = client.futures_income_history(
    incomeType="COMMISSION", startTime=start_ms, limit=500
)
total_comm = sum(float(c["income"]) for c in comm)
print(f"Commissions: {len(comm)} entries, ${total_comm:+,.2f}")

# 5. Funding fees today
funding = client.futures_income_history(
    incomeType="FUNDING_FEE", startTime=start_ms, limit=500
)
total_funding = sum(float(f["income"]) for f in funding)
print(f"Funding fees: {len(funding)} entries, ${total_funding:+,.2f}")
print()

# 6. Net PnL
net = total_realized + total_comm + total_funding
print("=== NET PNL TODAY ===")
print(f"Realized PnL:  ${total_realized:+,.2f}")
print(f"Commissions:   ${total_comm:+,.2f}")
print(f"Funding fees:  ${total_funding:+,.2f}")
print(f"NET:           ${net:+,.2f}")
print()

# 7. Compare with bot's internal DB
import sqlite3
from pathlib import Path

db = Path("paper_trading/state/paper_trades.db")
conn = sqlite3.connect(str(db))
conn.row_factory = sqlite3.Row
bot_trades = conn.execute(
    "SELECT * FROM trades WHERE exit_time >= '2026-03-16T00:00:00'"
).fetchall()
bot_pnl = sum(t["pnl_net"] for t in bot_trades)
bot_fees = sum(t["fee_total"] for t in bot_trades)

print("=== BOT DB vs BINANCE API ===")
print(f"Bot realized PnL:     ${bot_pnl:+,.2f} ({len(bot_trades)} trades)")
print(f"Bot fees:             ${bot_fees:+,.2f}")
print(f"Binance realized PnL: ${total_realized:+,.2f} ({len(income)} entries)")
print(f"Binance commissions:  ${total_comm:+,.2f}")
print(f"Binance funding:      ${total_funding:+,.2f}")
print(f"DIFF (Binance - Bot): ${(total_realized + total_comm) - (bot_pnl):+,.2f}")
print()

# 8. Open positions
print("=== OPEN POSITIONS ===")
positions = client.futures_position_information()
open_pos = [p for p in positions if abs(float(p.get("positionAmt", 0))) > 0]
total_upnl = 0
for p in sorted(open_pos, key=lambda x: float(x["unRealizedProfit"]), reverse=True):
    amt = float(p["positionAmt"])
    d = "LONG" if amt > 0 else "SHORT"
    entry = float(p["entryPrice"])
    upnl = float(p["unRealizedProfit"])
    notional = abs(entry * abs(amt))
    total_upnl += upnl
    sym = p["symbol"]
    print(
        f"{sym:16s} {d:5s} | Entry ${entry:>10.4f} | "
        f"Qty {abs(amt):>12} | Notional ${notional:>10.2f} | uPnL ${upnl:+8.2f}"
    )
print(f"\nTotal unrealized: ${total_upnl:+,.2f}")
print(f"Open positions: {len(open_pos)}")

# 9. All-time income summary
print()
print("=== ALL-TIME INCOME (since start) ===")
all_realized = client.futures_income_history(incomeType="REALIZED_PNL", limit=1000)
all_comm = client.futures_income_history(incomeType="COMMISSION", limit=1000)
all_funding = client.futures_income_history(incomeType="FUNDING_FEE", limit=1000)
ar = sum(float(i["income"]) for i in all_realized)
ac = sum(float(c["income"]) for c in all_comm)
af = sum(float(f["income"]) for f in all_funding)
print(f"All realized PnL:  ${ar:+,.2f} ({len(all_realized)} entries)")
print(f"All commissions:   ${ac:+,.2f} ({len(all_comm)} entries)")
print(f"All funding fees:  ${af:+,.2f} ({len(all_funding)} entries)")
print(f"All-time NET:      ${ar + ac + af:+,.2f}")
