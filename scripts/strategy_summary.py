"""Quick strategy summary from trades DB."""
import sqlite3

db = sqlite3.connect("data/trades.db")
c = db.cursor()

print("--- Unique Strategies Trading (all time) ---")
rows = c.execute(
    "SELECT strategy, COUNT(*), MIN(size), MAX(size) "
    "FROM trades GROUP BY strategy ORDER BY COUNT(*) DESC"
).fetchall()
for r in rows:
    print(f"  {r[0]:20s} trades={r[1]:5d}  size_range=${r[2]:.0f}-${r[3]:.0f}")

print("\n--- Recent Strategy Mix (last 1h) ---")
import time
cutoff = time.time() - 3600
rows2 = c.execute(
    "SELECT strategy, COUNT(*), SUM(size) "
    "FROM trades WHERE timestamp > ? GROUP BY strategy ORDER BY COUNT(*) DESC",
    (cutoff,),
).fetchall()
for r in rows2:
    print(f"  {r[0]:20s} trades={r[1]:5d}  volume=${r[2]:.0f}")

if not rows2:
    print("  (no trades in last 1h)")

db.close()
