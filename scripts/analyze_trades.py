"""Analyze recent trade performance from the SQLite database."""
import sqlite3
import sys
from datetime import datetime, timezone

db_path = sys.argv[1] if len(sys.argv) > 1 else "data/trades.db"
db = sqlite3.connect(db_path)
db.row_factory = sqlite3.Row

print("=== LAST 30 TRADES ===")
for r in db.execute(
    "SELECT timestamp,market_slug,strategy,side,token_side,price,size,cost,status "
    "FROM trades ORDER BY timestamp DESC LIMIT 30"
).fetchall():
    slug = (r["market_slug"] or "?")[:30]
    raw_ts = r["timestamp"]
    if isinstance(raw_ts, (int, float)):
        ts = datetime.fromtimestamp(raw_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    else:
        ts = str(raw_ts)[:19]
    print(
        f"{ts} | {r['strategy']:12s} | {r['side']:4s} {r['token_side']:3s} "
        f"| px={r['price']:.2f} sz={r['size']:.0f} cost={r['cost']:.1f} | {r['status']}"
    )

print("\n=== STRATEGY BREAKDOWN (trade_results) ===")
for r in db.execute(
    "SELECT strategy, COUNT(*) as trades,"
    " SUM(CASE WHEN net_profit > 0 THEN 1 ELSE 0 END) as wins,"
    " SUM(CASE WHEN net_profit < 0 THEN 1 ELSE 0 END) as losses,"
    " ROUND(SUM(net_profit), 2) as total_pl,"
    " ROUND(AVG(CASE WHEN net_profit > 0 THEN net_profit END), 2) as avg_win,"
    " ROUND(AVG(CASE WHEN net_profit < 0 THEN net_profit END), 2) as avg_loss,"
    " ROUND(SUM(investment), 2) as total_invested,"
    " ROUND(AVG(investment), 2) as avg_investment"
    " FROM trade_results GROUP BY strategy ORDER BY total_pl DESC"
).fetchall():
    wr = r["wins"] / max(r["wins"] + r["losses"], 1) * 100
    roi = r["total_pl"] / r["total_invested"] * 100 if r["total_invested"] else 0
    aw = r["avg_win"] or 0
    al = r["avg_loss"] or 0
    ai = r["avg_investment"]
    print(
        f"  {r['strategy']:15s} | pos={r['trades']:3d} | W={r['wins']:3d} L={r['losses']:3d} "
        f"({wr:5.1f}%) | PL={r['total_pl']:+8.2f} | ROI={roi:+.1f}% "
        f"| avg_win={aw:+.2f} avg_loss={al:+.2f} | avg_inv={ai:.0f}"
    )

print("\n=== ASSET BREAKDOWN ===")
for r in db.execute(
    "SELECT asset, COUNT(*) as pos,"
    " SUM(CASE WHEN net_profit > 0 THEN 1 ELSE 0 END) as wins,"
    " SUM(CASE WHEN net_profit < 0 THEN 1 ELSE 0 END) as losses,"
    " ROUND(SUM(net_profit), 2) as total_pl,"
    " ROUND(SUM(investment), 2) as total_inv"
    " FROM trade_results GROUP BY asset ORDER BY total_pl DESC"
).fetchall():
    wr = r["wins"] / max(r["wins"] + r["losses"], 1) * 100
    roi = r["total_pl"] / r["total_inv"] * 100 if r["total_inv"] else 0
    print(
        f"  {r['asset']:5s} | pos={r['pos']:3d} | W={r['wins']:3d} L={r['losses']:3d} "
        f"({wr:5.1f}%) | PL={r['total_pl']:+8.2f} | ROI={roi:+.1f}%"
    )

print("\n=== HEDGED vs UNHEDGED ===")
for r in db.execute(
    "SELECT was_hedged, COUNT(*) as pos,"
    " SUM(CASE WHEN net_profit > 0 THEN 1 ELSE 0 END) as wins,"
    " SUM(CASE WHEN net_profit < 0 THEN 1 ELSE 0 END) as losses,"
    " ROUND(SUM(net_profit), 2) as total_pl,"
    " ROUND(AVG(net_profit), 2) as avg_pl"
    " FROM trade_results GROUP BY was_hedged"
).fetchall():
    h = "HEDGED" if r["was_hedged"] else "UNHEDGED"
    wr = r["wins"] / max(r["wins"] + r["losses"], 1) * 100
    print(
        f"  {h:10s} | pos={r['pos']:3d} | W={r['wins']:3d} L={r['losses']:3d} "
        f"({wr:5.1f}%) | PL={r['total_pl']:+8.2f} | avg={r['avg_pl']:+.2f}"
    )

print("\n=== OUTCOME DISTRIBUTION ===")
for r in db.execute(
    "SELECT outcome, COUNT(*) as cnt, ROUND(SUM(net_profit),2) as pl"
    " FROM trade_results GROUP BY outcome ORDER BY cnt DESC"
).fetchall():
    print(f"  {r['outcome']:20s} | count={r['cnt']:3d} | PL={r['pl']:+8.2f}")

print("\n=== HOURLY P&L (LAST 24H) ===")
import time as _time
_24h_ago = _time.time() - 86400
for r in db.execute(
    "SELECT strftime('%Y-%m-%d %H:00', timestamp, 'unixepoch') as hour,"
    " COUNT(*) as pos, ROUND(SUM(net_profit),2) as pl"
    " FROM trade_results WHERE timestamp > ?"
    " GROUP BY hour ORDER BY hour",
    (_24h_ago,),
).fetchall():
    pl = r["pl"]
    if pl >= 0:
        bar = "+" * min(20, int(pl / 3))
    else:
        bar = "-" * min(20, int(-pl / 3))
    print(f"  {r['hour']} | pos={r['pos']:3d} | PL={pl:+8.2f} {bar}")

print("\n=== BUY PRICE BUCKETS ===")
for r in db.execute(
    "SELECT CASE"
    " WHEN price<0.15 THEN '<0.15' WHEN price<0.30 THEN '0.15-0.30'"
    " WHEN price<0.50 THEN '0.30-0.50' WHEN price<0.70 THEN '0.50-0.70'"
    " WHEN price<0.85 THEN '0.70-0.85' ELSE '0.85+' END as bucket,"
    " COUNT(*) as cnt, ROUND(AVG(size),1) as avg_sz, ROUND(AVG(cost),1) as avg_cost"
    " FROM trades WHERE side='BUY' GROUP BY bucket ORDER BY bucket"
).fetchall():
    print(
        f"  {r['bucket']:10s} | buys={r['cnt']:4d} | avg_sz={r['avg_sz']:.1f} "
        f"| avg_cost={r['avg_cost']:.1f}"
    )

print("\n=== RECENT WIN/LOSS STREAK (last 20 positions) ===")
for r in db.execute(
    "SELECT timestamp, market_slug, strategy, net_profit, investment, outcome"
    " FROM trade_results ORDER BY timestamp DESC LIMIT 20"
).fetchall():
    slug = (r["market_slug"] or "?")[:25]
    marker = "WIN" if r["net_profit"] > 0 else "LOSS" if r["net_profit"] < 0 else "FLAT"
    roi = r["net_profit"] / r["investment"] * 100 if r["investment"] else 0
    raw_ts = r["timestamp"]
    if isinstance(raw_ts, (int, float)):
        ts = datetime.fromtimestamp(raw_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    else:
        ts = str(raw_ts)[:19]
    print(
        f"  {ts} | {r['strategy']:12s} | {slug:25s} "
        f"| {marker:4s} {r['net_profit']:+7.2f} (ROI {roi:+.0f}%)"
    )

db.close()
