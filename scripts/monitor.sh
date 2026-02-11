#!/bin/bash
START_TS=$(date +%s)
echo "=== BOT MONITOR START: $(date -u +'%Y-%m-%d %H:%M:%S UTC') ==="
echo "Checking every 5 minutes for 30 minutes"
echo "Change: stop_loss_time_decay=false at 14:02 UTC"
echo ""

for i in 1 2 3 4 5 6 7; do
  if [ $i -gt 1 ]; then
    sleep 300
  fi

  echo "========================================"
  echo "CHECK #$i — $(date -u +'%Y-%m-%d %H:%M:%S UTC')"
  echo "========================================"

  echo ""
  echo "--- Service ---"
  systemctl is-active 15minuteprofitable

  echo ""
  echo "--- Memory ---"
  systemctl status 15minuteprofitable --no-pager 2>&1 | grep Memory

  echo ""
  echo "--- Trades Since Restart ---"
  sqlite3 -header -column /opt/15minuteprofitable/data/trades.db \
    "SELECT COUNT(*) as total_trades, SUM(CASE WHEN status='filled' THEN 1 ELSE 0 END) as filled, round(SUM(cost), 2) as total_cost FROM trades WHERE timestamp >= $START_TS;"

  echo ""
  echo "--- Results Since Restart ---"
  sqlite3 -header -column /opt/15minuteprofitable/data/trades.db \
    "SELECT COUNT(*) as results, SUM(CASE WHEN net_profit > 0 THEN 1 ELSE 0 END) as wins, SUM(CASE WHEN net_profit <= 0 THEN 1 ELSE 0 END) as losses, round(SUM(net_profit), 2) as net_pnl, round(SUM(investment), 2) as invested FROM trade_results WHERE timestamp >= $START_TS;"

  echo ""
  echo "--- P&L by Outcome ---"
  sqlite3 -header -column /opt/15minuteprofitable/data/trades.db \
    "SELECT outcome, COUNT(*) as cnt, round(SUM(net_profit), 2) as pnl, round(AVG(net_profit), 2) as avg_pnl FROM trade_results WHERE timestamp >= $START_TS GROUP BY outcome;"

  echo ""
  echo "--- Exit Events Since Restart (KEY METRIC) ---"
  SL=$(tail -50000 /var/log/15minuteprofitable/bot.log 2>/dev/null | grep -c 'stop_loss_triggered' || echo 0)
  TP=$(tail -50000 /var/log/15minuteprofitable/bot.log 2>/dev/null | grep -c 'take_profit_triggered' || echo 0)
  TE=$(tail -50000 /var/log/15minuteprofitable/bot.log 2>/dev/null | grep -c '"event": "time_exit"' || echo 0)
  LT=$(tail -50000 /var/log/15minuteprofitable/bot.log 2>/dev/null | grep -c 'time_exit_skipped_lottery' || echo 0)
  echo "Stop-loss triggers: $SL"
  echo "Take-profit triggers: $TP"
  echo "Time exits: $TE"
  echo "Lottery skips: $LT"

  echo ""
  echo "--- Recent Results ---"
  sqlite3 -header -column /opt/15minuteprofitable/data/trades.db \
    "SELECT datetime(timestamp, 'unixepoch') as time, substr(market_slug, 1, 25) as market, strategy, outcome, round(net_profit, 2) as pnl FROM trade_results WHERE timestamp >= $START_TS ORDER BY timestamp DESC LIMIT 5;"

  echo ""
  echo "--- Errors ---"
  ERR=$(tail -5000 /var/log/15minuteprofitable/bot.log 2>/dev/null | grep -c '"level": "error"' || echo 0)
  CB=$(tail -5000 /var/log/15minuteprofitable/bot.log 2>/dev/null | grep -c 'circuit_breaker' || echo 0)
  echo "Recent errors: $ERR"
  echo "Circuit breaker events: $CB"
  echo ""
done

echo "=== MONITOR COMPLETE: $(date -u +'%Y-%m-%d %H:%M:%S UTC') ==="
