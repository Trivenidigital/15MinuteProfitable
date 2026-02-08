"""Quick monitoring check for bot health."""
import sqlite3
import time

db = sqlite3.connect("data/trades.db")
c = db.cursor()

# Trade results (resolved positions)
total_results = c.execute("SELECT COUNT(*) FROM trade_results").fetchone()[0]
wins = c.execute("SELECT COUNT(*) FROM trade_results WHERE outcome='win'").fetchone()[0]
losses = c.execute("SELECT COUNT(*) FROM trade_results WHERE outcome='loss'").fetchone()[0]
pnl = c.execute("SELECT COALESCE(SUM(net_profit),0) FROM trade_results").fetchone()[0]

decided = wins + losses
wr = (wins / decided * 100) if decided > 0 else 0
print(f"Results: {total_results} total | W:{wins} L:{losses} | WR:{wr:.1f}% | PnL:${pnl:.2f}")

# Raw trades (executions)
total_trades = c.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
print(f"Executions: {total_trades} total")

# Last 5 trades
print("\n--- Last 5 Executions ---")
recent = c.execute(
    "SELECT strategy, side, asset, size, price, status "
    "FROM trades ORDER BY timestamp DESC LIMIT 5"
).fetchall()
for r in recent:
    print(f"  {r[0]:15s} {r[1]:4s} {r[2]:4s} sz=${r[3]:.0f} px={r[4]:.4f} {r[5]}")

# Trades in last 10 minutes
cutoff = time.time() - 600
recent_count = c.execute(
    "SELECT COUNT(*) FROM trades WHERE timestamp > ?", (cutoff,)
).fetchone()[0]
print(f"\nTrades in last 10min: {recent_count}")

# Strategy breakdown for recent trades (last 30min)
cutoff30 = time.time() - 1800
print("\n--- Strategy Activity (last 30min) ---")
strats = c.execute(
    "SELECT strategy, COUNT(*), SUM(size) FROM trades WHERE timestamp > ? GROUP BY strategy",
    (cutoff30,),
).fetchall()
if strats:
    for s in strats:
        print(f"  {s[0]:15s} trades={s[1]} total_size=${s[2]:.0f}")
else:
    print("  (no trades in last 30min)")

# Check sizes (verify new $50 min) and prices (verify $0.10 floor)
print("\n--- Last 10 Trade Sizes & Prices ---")
sizes = c.execute(
    "SELECT strategy, size, price FROM trades ORDER BY timestamp DESC LIMIT 10"
).fetchall()
for s in sizes:
    flags = ""
    if s[1] < 50:
        flags += " <-- BELOW $50!"
    if s[2] < 0.10:
        flags += " <-- BELOW $0.10!"
    print(f"  {s[0]:15s} size=${s[1]:.0f} price={s[2]:.4f}{flags}")

# Last 5 results with P&L
print("\n--- Last 5 Results ---")
results = c.execute(
    "SELECT strategy, asset, outcome, net_profit, investment "
    "FROM trade_results ORDER BY timestamp DESC LIMIT 5"
).fetchall()
for r in results:
    print(f"  {r[0]:15s} {r[1]:4s} {r[2]:6s} pnl=${r[3]:.2f} inv=${r[4]:.2f}")

db.close()
