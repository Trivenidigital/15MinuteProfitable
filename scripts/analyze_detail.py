"""Detailed analysis of resolution_sniper and price_lag trades."""
import sqlite3
import time
from datetime import datetime, timezone

db = sqlite3.connect("data/trades.db")
db.row_factory = sqlite3.Row

one_hour_ago = time.time() - 3600

print("=== RESOLUTION SNIPER DETAIL ===")
rows = db.execute(
    "SELECT t.timestamp, t.market_slug, t.token_side, t.price, t.size, t.cost, "
    "r.outcome, r.net_profit, r.gross_payout, r.investment "
    "FROM trades t LEFT JOIN trade_results r ON t.condition_id = r.condition_id "
    "WHERE t.timestamp > ? AND t.strategy = 'resolution_sniper' ORDER BY t.timestamp DESC",
    (one_hour_ago,),
).fetchall()
for r in rows:
    ts = datetime.fromtimestamp(r["timestamp"], tz=timezone.utc).strftime("%H:%M:%S")
    outcome = r["outcome"] or "pending"
    net = r["net_profit"] or 0
    print(
        f"  {ts} | bought {r['token_side']} @ {r['price']:.4f} "
        f"x{r['size']:.1f} = cost ${r['cost']:.2f} "
        f"| outcome={outcome} net=${net:.4f} | {r['market_slug'][:50]}"
    )

print()
print("=== SNIPER TIMING (minutes before window end) ===")
sniper_trades = db.execute(
    "SELECT t.timestamp, t.market_slug, t.price, t.token_side, "
    "mo.window_end, mo.outcome "
    "FROM trades t JOIN market_outcomes mo ON t.condition_id = mo.condition_id "
    "WHERE t.timestamp > ? AND t.strategy = 'resolution_sniper' ORDER BY t.timestamp DESC",
    (one_hour_ago,),
).fetchall()
for r in sniper_trades:
    mins_before = (r["window_end"] - r["timestamp"]) / 60
    ts = datetime.fromtimestamp(r["timestamp"], tz=timezone.utc).strftime("%H:%M:%S")
    correct = (
        "CORRECT"
        if (r["token_side"] == "YES" and r["outcome"] == "YES")
        or (r["token_side"] == "NO" and r["outcome"] == "NO")
        else "WRONG"
    )
    print(
        f"  {ts} | {mins_before:.1f}min before end | bought {r['token_side']} "
        f"@ {r['price']:.4f} | actual={r['outcome']} | {correct} "
        f"| {r['market_slug'][:45]}"
    )

print()
print("=== PRICE LAG DETAIL ===")
rows = db.execute(
    "SELECT t.timestamp, t.market_slug, t.token_side, t.price, t.size, t.cost, "
    "r.outcome, r.net_profit, r.gross_payout, r.investment, r.was_hedged "
    "FROM trades t LEFT JOIN trade_results r ON t.condition_id = r.condition_id "
    "WHERE t.timestamp > ? AND t.strategy = 'price_lag' ORDER BY t.timestamp DESC",
    (one_hour_ago,),
).fetchall()
for r in rows:
    ts = datetime.fromtimestamp(r["timestamp"], tz=timezone.utc).strftime("%H:%M:%S")
    outcome = r["outcome"] or "pending"
    net = r["net_profit"] or 0
    hedged = "H" if r["was_hedged"] else "D"
    print(
        f"  {ts} | {hedged} | bought {r['token_side']} @ {r['price']:.4f} "
        f"x{r['size']:.1f} = ${r['cost']:.2f} | outcome={outcome} "
        f"net=${net:.4f} | {r['market_slug'][:45]}"
    )

# Get the current config/settings for sniper
print()
print("=== CURRENT .env SNIPER SETTINGS ===")
try:
    with open(".env") as f:
        for line in f:
            line = line.strip()
            if any(k in line.upper() for k in ["SNIPER", "CONFIDENCE", "PRICE_LAG", "RESOLUTION"]):
                if not line.startswith("#"):
                    print(f"  {line}")
except FileNotFoundError:
    print("  .env not found")

db.close()
