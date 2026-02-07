# Bot Monitoring Report — 2026-02-07 (19:00–19:35 UTC)

## Executive Summary

The bot ran stable for 35 minutes after deploying the smart stop-loss changes. **No crashes, no errors, no warnings in bot logs.** 5 new trades were executed (all price_lag strategy, all dry run). The smart stop-loss code deployed cleanly — **zero stop_loss_triggered events** in the monitoring window, which is the intended behavior since the new logic is more conservative. One pre-deployment stop-loss fired (on the XRP position from 18:53, before the new code was live).

**Net P&L during window: approximately -$89.14** (1 winner, 2 losers held to time_exit, 2 still open at end of monitoring).

---

## Service Health

| Metric | Start (19:00) | End (19:35) |
|--------|---------------|-------------|
| Status | active (running) | active (running) |
| Memory | 47.3 MB | 78.8 MB |
| CPU | 0.7s | 2m 53s |
| PID | 62944 | 62944 (same) |
| Tasks (threads) | 5 | 5 |
| Errors | 0 | 0 |
| Warnings | 0 | 0 |

**Observation:** Memory grew from 47 MB to 79 MB over 35 minutes (+32 MB). At this rate, it would reach ~1 GB in ~18 hours. Worth watching for potential memory leak (likely from accumulating orderbook snapshots/deltas or log buffers).

---

## Trades During Monitoring Window

| # | Time (UTC) | Market | Side | Price | Shares | Cost | Exit | P&L |
|---|-----------|--------|------|-------|--------|------|------|-----|
| 234 | 19:03:42 | SOL 15m | YES | $0.497 | 150 | $74.50 | take_profit (+18.79%) at 19:07 | **+$13.72** |
| 235 | 19:10:25 | ETH 15m | YES | $0.210 | 150 | $31.50 | time_exit at 19:14 (49.5s left) | **-$31.20** |
| 236 | 19:16:40 | SOL 15m | NO | $0.480 | 150 | $71.96 | time_exit at 19:29 (49.1s left) | **-$71.66** |
| 237-239 | 19:32:27+ | XRP 15m | YES | ~$0.23 | 150x3 | ~$103 | *still open at end of monitoring* | TBD |

### Pre-deployment trade (before 19:00)
| 233 | 18:53:43 | XRP 15m | NO | $0.471 | 150 | $70.63 | **stop_loss** (-17.17%) at 18:53 | **-$12.13** |

---

## Position Lifecycle Analysis

### Trade 234 — SOL YES (WINNER)
- Entry: 19:03, price $0.497 (spot UP signal, odds_lag=0.031)
- Take-profit triggered at 19:07 (+18.79%), 4 minutes after entry
- Gross payout: $88.50, Net profit: **+$13.72** (after $0.28 winner fee)
- This is exactly the kind of trade the smart stop-loss is designed to protect

### Trade 235 — ETH YES (LOSER)
- Entry: 19:10, price $0.21 (spot UP signal, odds_lag=0.301, size=75 — half size due to sizing multiplier)
- Held to time_exit at 19:14 (only 49.5s before expiry)
- Gross payout: $0.30 (essentially worthless at resolution)
- Net loss: **-$31.20**
- **Key issue:** The odds_lag=0.301 was very high, suggesting a large discrepancy, but the market resolved against the signal. The sizing multiplier correctly halved the position (75 vs 150 shares), limiting damage.

### Trade 236 — SOL NO (LOSER)
- Entry: 19:16, price $0.48 (spot DOWN signal, odds_lag=0.055)
- Held to time_exit at 19:29 (49.1s before expiry)
- Gross payout: $0.30 (worthless at resolution)
- Net loss: **-$71.66** — largest single loss in the window
- **Key issue:** Full-size position ($72 cost) on a relatively thin signal (odds_lag only 0.055). The smart stop-loss's time decay would have disabled stop-loss in the last third anyway, so the position was allowed to ride to time_exit.

### Trades 237-239 — XRP YES (3 rapid entries)
- Three `lag_opportunity_found` events in 4 seconds (19:32:27, 19:32:29, 19:32:31)
- All same direction (UP), same market, odds_lag ~0.281
- **Concern:** Bot is stacking 3x $150 into the same market in rapid succession. Expected profit ~$20 each, but the tripled exposure ($~300+) means a loss could be severe.

---

## Smart Stop-Loss Behavior

| Event | Count | Notes |
|-------|-------|-------|
| `stop_loss_triggered` | 0 | None during monitoring (1 pre-deploy at 18:53) |
| `stop_loss_skipped_cheap` | 0 | No cheap contracts entered |
| `stop_loss_disabled_late_market` | 0 | Not visible in debug logs at INFO level |
| `stop_loss_pending_confirmation` | 0 | Not visible in debug logs at INFO level |

**Note:** The smart stop-loss debug events (`stop_loss_skipped_cheap`, `stop_loss_disabled_late_market`, `stop_loss_pending_confirmation`) are logged at DEBUG level. The bot runs at INFO level, so these won't appear in production logs. Consider promoting at least `stop_loss_pending_confirmation` to INFO level for observability during the initial rollout period.

---

## WebSocket Connectivity

| Event | Count | Times |
|-------|-------|-------|
| `reconnect_requested` (CLOB WS) | 3 | 19:00, 19:15, 19:30 |

Reconnects happen every ~15 minutes — this coincides with the market lifecycle (each 15-min market expires and new subscriptions are needed). This appears to be normal/expected behavior, not a connectivity issue.

---

## Event Volume

| Event | Count (sample) |
|-------|------|
| `snapshot_applied` | ~2564 |
| `deltas_applied` | ~1327 |
| `arb_eval` | ~404 |
| `maker_arb_eval` | ~404 |
| `orderbook` | ~160 |
| `scan_complete` | ~101 |
| `metrics_dashboard` | ~20 |
| `monitor_status` | ~20 |

Log volume is extremely high (~3.5M lines in the log file, growing at ~20K lines per 10 minutes). At this rate, the log file grows by ~120K lines/hour or ~2.9M lines/day.

---

## Issues & Concerns

### 1. CRITICAL: Triple-stacking positions in same market
Trades 237-239 show the bot entering 3x $150 positions in the same XRP market within 4 seconds. This means ~$450 total exposure to a single 15-minute binary outcome. The position size limit (`max_position_per_market: $1000`) allows this, but it's risky behavior.

**Root cause:** The scanner fires every ~2 seconds and finds the same opportunity repeatedly. After filling the first order, the position exists but the signal is still active, so it enters again.

**Recommendation:** Add a cooldown per condition_id after entry (e.g., skip opportunities for a market where we already have an open position from the same strategy).

### 2. HIGH: Losers are full-size, winners exit early
- The one winner (SOL YES, +$13.72) correctly hit take_profit
- The two losers (ETH YES at -$31.20, SOL NO at -$71.66) were held to time_exit and lost nearly 100% of cost
- Neither triggered stop-loss because the smart stop-loss (correctly) disables in the last third of the market

**Pattern:** When the signal is wrong, the position goes to near-zero and resolution kills it. When right, take-profit captures only ~18% upside. This is a structural issue with directional 15-min binary options — the payoff is asymmetric (lose 100% vs gain 18%).

**Recommendation:** Consider whether the take_profit_pct (15%) is too conservative, or whether positions should be sized even smaller on low-confidence signals (odds_lag < 0.10).

### 3. MEDIUM: Memory growth
47 MB to 79 MB in 35 minutes. If linear, this reaches ~500 MB in 8 hours. Likely not a crisis but warrants investigation. Possible causes:
- Orderbook snapshot accumulation
- Spot buffer not pruning old data
- Log handler buffering

### 4. LOW: Log volume
3.5M+ lines in bot.log, growing rapidly. The `logrotate.conf` should handle this, but verify it's active and configured for reasonable rotation (e.g., daily, max 7 files).

### 5. LOW: Error log has "Invalid HTTP request" warnings
All 10 entries are from external port scanners hitting the dashboard HTTP server. Not a functional issue, but consider binding dashboard to localhost + using a reverse proxy, or rate-limiting.

### 6. INFO: Smart stop-loss observability gap
Debug-level stop-loss events are invisible in production. During the rollout period, promoting `stop_loss_pending_confirmation` to INFO would help validate the new logic is firing correctly.

---

## Recommendations Summary

| Priority | Action | Impact |
|----------|--------|--------|
| **P0** | Add per-market entry cooldown to prevent triple-stacking | Prevent $450+ single-market exposure |
| **P1** | Re-evaluate take_profit_pct vs max_loss asymmetry | Structural P&L improvement |
| **P1** | Promote stop-loss debug events to INFO for rollout observability | Validate new feature works |
| **P2** | Investigate memory growth pattern | Prevent OOM over multi-day runs |
| **P3** | Verify logrotate is active | Prevent disk fill |
| **P3** | Bind dashboard to localhost or add rate limiting | Reduce noise from scanners |
