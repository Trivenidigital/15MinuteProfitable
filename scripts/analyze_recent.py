"""Analyze trades from the past hour."""
import sqlite3
import time
from datetime import datetime, timezone

db = sqlite3.connect("data/trades.db")
db.row_factory = sqlite3.Row

one_hour_ago = time.time() - 3600

print("=== TRADES (last 1h) ===")
rows = db.execute(
    "SELECT * FROM trades WHERE timestamp > ? ORDER BY timestamp DESC",
    (one_hour_ago,),
).fetchall()
for r in rows:
    ts = datetime.fromtimestamp(r["timestamp"], tz=timezone.utc).strftime("%H:%M:%S")
    print(
        f"  {ts} | {r['strategy']:18s} | {r['side']:4s} {r['token_side']:3s} "
        f"| price={r['price']:.4f} size={r['size']:.1f} cost=${r['cost']:.2f} "
        f"| {r['market_slug'][:60]}"
    )
print(f"Total: {len(rows)} trades\n")

print("=== TRADE RESULTS (last 1h) ===")
results = db.execute(
    "SELECT * FROM trade_results WHERE timestamp > ? ORDER BY timestamp DESC",
    (one_hour_ago,),
).fetchall()
for r in results:
    ts = datetime.fromtimestamp(r["timestamp"], tz=timezone.utc).strftime("%H:%M:%S")
    sign = "+" if r["net_profit"] >= 0 else ""
    print(
        f"  {ts} | {r['strategy']:18s} | hedged={r['was_hedged']} "
        f"| invest=${r['investment']:.2f} payout=${r['gross_payout']:.2f} "
        f"net={sign}${r['net_profit']:.4f} | {r['outcome']:3s} "
        f"| {r['market_slug'][:50]}"
    )
print(f"Total: {len(results)} results\n")

print("=== STRATEGY DECISIONS (last 1h) ===")
decisions = db.execute(
    "SELECT * FROM strategy_decisions WHERE timestamp > ? ORDER BY timestamp DESC",
    (one_hour_ago,),
).fetchall()
decision_summary: dict[tuple[str, str], int] = {}
for d in decisions:
    key = (d["strategy"], d["decision"])
    decision_summary[key] = decision_summary.get(key, 0) + 1
for (strat, dec), count in sorted(decision_summary.items()):
    print(f"  {strat:18s} | {dec:20s} | count={count}")
print(f"Total decisions: {len(decisions)}\n")

print("=== REJECTION REASONS (last 1h) ===")
rejections = db.execute(
    "SELECT rejection_reason, COUNT(*) as cnt FROM strategy_decisions "
    "WHERE timestamp > ? AND rejection_reason != '' GROUP BY rejection_reason "
    "ORDER BY cnt DESC",
    (one_hour_ago,),
).fetchall()
for r in rejections:
    print(f"  {r['cnt']:4d}x  {r['rejection_reason']}")
print()

print("=== P&L SUMMARY ===")
if results:
    total_invest = sum(r["investment"] for r in results)
    total_payout = sum(r["gross_payout"] for r in results)
    total_net = sum(r["net_profit"] for r in results)
    wins = [r for r in results if r["net_profit"] > 0]
    losses = [r for r in results if r["net_profit"] < 0]
    breakeven = [r for r in results if r["net_profit"] == 0]
    print(f"  Invested:  ${total_invest:.2f}")
    print(f"  Payout:    ${total_payout:.2f}")
    print(f"  Net P&L:   ${total_net:.4f}")
    print(f"  Win/Loss:  {len(wins)}W / {len(losses)}L / {len(breakeven)}BE")
    if wins:
        avg_win = sum(r["net_profit"] for r in wins) / len(wins)
        print(f"  Avg win:   ${avg_win:.4f}")
    if losses:
        avg_loss = sum(r["net_profit"] for r in losses) / len(losses)
        print(f"  Avg loss:  ${avg_loss:.4f}")
else:
    print("  No resolved trades in last hour")
print()

print("=== MARKET OUTCOMES (last 1h) ===")
outcomes = db.execute(
    "SELECT * FROM market_outcomes WHERE timestamp > ? ORDER BY window_end DESC",
    (one_hour_ago,),
).fetchall()
for o in outcomes:
    ts = datetime.fromtimestamp(o["window_end"], tz=timezone.utc).strftime("%H:%M:%S")
    traded = "TRADED" if o["was_traded"] else "skipped"
    print(
        f"  {ts} | {o['asset']:6s} | {o['outcome']:4s} "
        f"| change={o['price_change_pct']:+.4f}% "
        f"| open=${o['spot_open']:.2f} close=${o['spot_close']:.2f} | {traded}"
    )
print(f"Total: {len(outcomes)} outcomes\n")

# Per-strategy P&L breakdown
print("=== PER-STRATEGY BREAKDOWN (last 1h) ===")
if results:
    strat_stats: dict[str, dict] = {}
    for r in results:
        s = r["strategy"]
        if s not in strat_stats:
            strat_stats[s] = {"count": 0, "net": 0.0, "wins": 0, "losses": 0, "invested": 0.0}
        strat_stats[s]["count"] += 1
        strat_stats[s]["net"] += r["net_profit"]
        strat_stats[s]["invested"] += r["investment"]
        if r["net_profit"] > 0:
            strat_stats[s]["wins"] += 1
        elif r["net_profit"] < 0:
            strat_stats[s]["losses"] += 1
    for s, st in sorted(strat_stats.items()):
        wr = st["wins"] / st["count"] * 100 if st["count"] else 0
        print(
            f"  {s:18s} | {st['count']:3d} trades | "
            f"net=${st['net']:.4f} | WR={wr:.0f}% ({st['wins']}W/{st['losses']}L) "
            f"| invested=${st['invested']:.2f}"
        )

db.close()
