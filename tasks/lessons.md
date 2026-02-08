# Lessons Learned

> Patterns, mistakes, and insights captured during development.
> Updated after every correction or discovery.

---

## Polymarket API Quirks

- **neg_risk auto-detection is broken** for BTC 15-min markets. The `/neg-risk` endpoint returns "Invalid token id". Always hardcode `neg_risk=True` in `PartialCreateOrderOptions`.
- **py-clob-client is synchronous only.** Must wrap with `asyncio.to_thread()` for async usage. Not thread-safe — one client instance per thread is safest.
- **Order signing latency (~1s)** is dominated by HTTP calls for tick_size and neg_risk. Pre-providing both in `PartialCreateOrderOptions` reduces signing to ~50ms.
- **Token prices/sizes in OrderBookSummary are strings**, not floats. Must cast explicitly.
- **USDC balance from API is in wei (6 decimals).** Divide by 1,000,000 for USD.
- **Magic.link accounts (signature_type=1):** The `funder` address must be the Polymarket proxy wallet, NOT the signer address. This is the #1 cause of "invalid signature" errors.

## Fee Structure

- **Conflicting fee data exists.** Some sources say max taker fee is 50 bps (0.5%), others say 3.15%. The user's spec says 3.15%. Must empirically verify with a test trade before going live.
- **Maker fee is 0%.** Using GTC limit orders (Strategy B) avoids taker fees entirely.
- **Winner fee (2%) is on profit only**, not total payout. Often forgotten in profit calculations.
- **Pure arb is nearly impossible at 50/50 odds** with the current fee structure. Need combined cost well below $0.94 to profit.

## Architecture Decisions

- **Dashboard in same event loop:** FastAPI/Uvicorn runs in same asyncio loop via `uvicorn.Server.serve()` - no separate process needed.
- **SQLite with WAL mode:** Enables concurrent reads during bot operation, safe for single-writer pattern.
- **systemd for production:** Use systemd service for auto-restart, logging, and process management.

## Deployment Lessons

- **GitHub auth changed:** Password authentication no longer works for git clone. Use Personal Access Token or make repo public.
- **Ubuntu 24.04 has Python 3.12:** No need to install Python 3.11, the default Python 3.12 works fine.
- **systemd User= must exist:** If service file specifies `User=botuser`, that user must exist. Either create it or change to `User=root`.
- **Hetzner Helsinki for EU:** Amsterdam not available on Hetzner Cloud. Helsinki is closest to London AWS (Polymarket servers).
- **.env parsing is strict:** No extra whitespace, no quotes around values, no trailing spaces. Use `printf` instead of heredocs.
- **BOT_MARKETS format:** Don't include BOT_MARKETS in .env if using default. The comma-separated format can cause parsing issues.

## Risk Manager Architecture

- **Pre-trade checks are strong, runtime monitoring is weak.** The RiskManager runs 8 sequential checks before every trade, but never re-checks positions after creation. Circuit breaker only blocks new trades — it doesn't unwind existing positions.
- **In-flight orders are invisible to risk limits.** Pending GTC orders (asymmetric strategy) and pending maker arb pairs consume real exposure but aren't tracked by `total_exposure()` or `market_exposure()`. This means parallel strategy mode can overshoot limits.
- **Failure recording is inconsistent across strategies.** Arbitrage calls `record_execution_failure()` on partial fills, but directional FOK failures and GTC rejections silently pass. This means the 3-failure circuit breaker may not trip when it should.
- **DailyPnL only updates on position close, not on open position movement.** `max_drawdown` tracks realized drawdown, not unrealized. A directional position could be -20% but the risk manager won't see it until close/resolution.
- **Circuit breaker events are only logged, not alerted.** No Telegram/Discord notification when breaker trips. AlertDispatcher exists but isn't wired to risk events.

## Exit Logic

- **Never sell near-worthless positions for dust.** When a position has lost >95% of value, the salvage from selling is negligible but the upside of holding to expiry could be full recovery. A $31.50 position sold for $0.30 saves $0.30 max downside but forfeits the chance of $31.50 payout. Even 1% win probability makes holding +EV. Time-based exits must check value ratio before dumping.

## Strategy Co-location vs Separate Bots

- **New strategies belong in the same bot** unless they need a different API key, wallet, chain, or event loop. The shared infrastructure cost of a second bot (rate limit coordination, WebSocket duplication, state synchronization) far outweighs the isolation benefit.
- **Key shared resources that force co-location:** Rate limiter (60 orders/min per API key), WebSocket connections (~5 per IP), risk manager (needs global exposure view), orderbook manager, spot buffer, executor.
- **The BaseStrategy interface is the right abstraction.** Any strategy that follows evaluate() → Opportunity | None + should_exit() → bool fits cleanly. Don't split unless the pattern fundamentally doesn't fit.
- **Time-domain partitioning avoids conflicts naturally.** Price-Lag winds down at <30s, Sniper activates at T-120s. The scanner picks the best opportunity — no manual conflict resolution needed.
- **Per-strategy overrides in risk manager are the right pattern** for strategies with different risk profiles (e.g., sniper needs 3 entries per market vs default 2). Don't fork the risk manager.
- **"Hold to resolution" strategies are fine** — should_exit() returning False is trivially handled by the exit loop.
- **When to actually separate:** Different API keys/wallets, different chains, strategy needs its own event loop, or strategy is experimental enough that crashes are expected.

## Stop-Loss and Take-Profit

- **Triple-stacking race condition:** The 2s cooldown in RiskManager races with async order execution. The scanner evaluates the same market multiple times before `record_trade()` runs. Fix: track `_entry_counts` in StateManager (incremented inside the async lock in `record_trade()`), checked synchronously in `RiskManager.check_opportunity()` before allowing entries. `max_entries_per_market` config controls the cap (default: 2).
- **Asymmetric payoff with flat take-profit:** Flat 15% take-profit exits winners early while losers go to near-zero at resolution. Fix: dynamic take-profit with time-based curve (first third: base, middle: 2x, last: disabled). In the last 5 minutes, a winning position has high probability of going to $1.00 — don't sell at $0.575.
- **Smart stop-loss layers:** Three layers work together: (1) cheap contract bypass (skip stop-loss for avg entry < $0.10), (2) time-decayed threshold (doubles in middle third, disabled in last third), (3) confirmation counter (3 consecutive triggers before exit). This prevents premature exits on temporary dips.
- **StateProvider protocol pattern:** When adding new state queries needed by RiskManager, extend the `StateProvider` protocol in `src/risk/manager.py`, implement in `StateManager`, and update `MockState` in tests. All 4 position deletion sites in StateManager must clean up new tracking dicts.

## Memory and Observability

- **OrderBook memory grows unboundedly** without cleanup. Expired 15-min market token IDs accumulate in `OrderBookManager._books`. Fix: `remove_stale_books()` called during market rollover, removes books not updated within 120s.
- **`snapshot_applied` dominated log output** at ~58% of all lines when at INFO level. Demoting to DEBUG reduced log volume by ~95% (from ~18,000 lines/min to ~878).
- **Stop-loss debug events are invisible in production.** Promote key stop-loss events (skipped_cheap, disabled_late_market, pending_confirmation) to INFO for rollout observability. These are low-volume events.

## Server/Deploy

- **SSH key auth is set up.** `~/.ssh/id_ed25519` → `root@46.62.206.192`. No more password prompts.
- **Server may have local changes.** When deploying, if `git checkout` fails with "local changes would be overwritten", run `git stash` first.
- **Server lacks `pgrep`.** Use `pidof python` or `systemctl status btc15minutebot` instead of `pgrep -f src.main` for process checks.
- **Large log file queries hang.** Don't `grep` the entire bot.log (3.5M+ lines). Use `tail -N` to limit input, or `awk '/timestamp/,0'` to scope to a time range.
- **Log rotation:** Consider setting up logrotate — the log file grows continuously and is already 3.6M+ lines.

## Observability Pipeline

- **TradeDatabase is gated on `dashboard_enabled`, but decision logging needs it always.** The trade_db initialization check was `if settings.dashboard_enabled`. Phase 1 observability extended this to `if settings.dashboard_enabled or settings.enable_decision_logging` so the DB is available even without the dashboard. Any future feature that needs SQLite should add its own config gate to this check.
- **Module-level mutable state for cross-loop coordination.** `_market_start_prices` and `_recorded_outcomes` are module-level dicts/sets used by `_market_outcome_loop` to track open prices across market lifecycles. This follows the same pattern as `_pending_gtc_orders` and `_pending_maker_arb_pairs`. Keep this pattern for state shared between loops rather than threading it through function params.
- **Market close price is approximate.** When a market expires, the `_market_outcome_loop` captures the current spot price as the close. Since detection runs every 10s, this can be up to 10s late. For higher accuracy, query `trade_db.get_spot_at_time()` using the market's `end_time` (relies on the 5s spot snapshot loop). This is good enough for directional win/loss determination.
- **ruff import sorting is strict.** Comments between import groups (like `# Dashboard (lazy)`) break isort formatting. Either remove inline comments or ensure imports are organized into clean standard/third-party/local groups without interleaving comments.

## Resolution Sniper Deployment

- **Circuit breaker persists across restarts.** The state snapshot saves daily PnL, and if the circuit breaker was activated (24h timer), restarting the bot doesn't clear it. Even changing `max_daily_loss` from $150 to $1000 didn't help because `is_circuit_breaker_active()` short-circuits before the daily loss check re-evaluates. Fix required a second restart to clear the in-memory flag. Consider: re-evaluate breaker against current settings on startup.
- **Pydantic `env_prefix="BOT_"` means ALL env vars need the prefix.** `ENABLE_RESOLUTION_SNIPER=true` silently does nothing. Must be `BOT_ENABLE_RESOLUTION_SNIPER=true`. Triple-check the prefix when adding new env vars.
- **Sniper fires every 15-minute window, not just once.** Each market cycle (BTC, ETH, SOL, XRP) gets its own sniper evaluation in the last 120s. At 87% confidence, all 4 assets can trigger simultaneously during coordinated moves.
- **87% confidence threshold is the sweet spot.** At 90%, about 25% of valid opportunities were filtered out. At 87%, the sniper catches entries like ETH at 87.1% and SOL at 87.4% that still have strong positive EV after fees.
- **XRP sniper entries at high prices ($0.93) have poor risk/reward.** Expected profit of $0.33 on $150 means a single loss wipes out ~420 wins. Consider adding a minimum expected profit filter ($5+) or lowering `sniper_max_entry_price`.
- **Entry cap and per-strategy override may conflict.** The risk manager overrides `max_entries_per_market` to 3 for sniper, but rejections showed `2 >= 2`, suggesting price-lag entries on the same condition_id count toward the cap. Entries from different strategies sharing a market need careful accounting.
- **Monitoring background tasks must use current PID.** If the bot is restarted mid-monitoring, `/proc/{old_pid}/status` fails and cascades grep failures. Always capture the PID fresh or use `systemctl status` for memory checks.

## Self-Learning System

- **Plans must match the user's actual vision, not what seems logical.** I wrote Phase 2-5 as generic offline analysis scripts + dashboard integration. The user's actual vision was an autonomous self-learning loop (Analyze → Tune → Measure). Always verify plans against prior discussions before writing them up. Generic "next steps" are a red flag.
- **Two separate living documents, not one.** `strategy-self-learn.html` is the system plan (methodology, cadence, parameter priority, 7-day game plan). `self-learning-lessons-strategies.html` is the experiment log (every config change, results, insights). The plan rarely changes; the log changes every cycle. Mixing them makes the log hard to scan.
- **The self-learning agent is an operational workflow, not bot code.** No new Python modules needed. The Claude agent SSHs into the server, queries SQLite, updates `.env`, restarts via systemctl. The observability pipeline (Phase 1) provides the data; the agent provides the intelligence.
- **Parameter tuning is autonomous; strategy code changes require human approval.** This boundary prevents runaway code mutations while allowing rapid iteration on thresholds. The agent documents code change proposals in the HTML experiment log for async human review.
- **Baseline data needs 24h before first analysis.** The bot needs to run with current config for a full day to capture enough market windows (~384/day across 4 assets) for statistically meaningful baseline metrics. Don't start tuning on partial data.
- **"no_opportunities" dominates early decision logs.** With only arbitrage enabled and spreads too wide (combined YES+NO > $1.01), every scan cycle returns zero opportunities. This confirms the first self-learning action should be enabling price_lag or lowering arb thresholds.
- **Deployment is fast when no new dependencies.** Pure Python file additions (new modules, new tables via CREATE IF NOT EXISTS) only need `git pull` + `systemctl restart`. No `pip install` needed. SQLite schema auto-migrates on startup.
- **Spot snapshots start immediately, market outcomes need a full 15-min window.** After restart, `spot_snapshots` gets rows within 5s and `strategy_decisions` within 2s. But `market_outcomes` stays empty until the first market expires (~15 min). Don't panic about zero outcome rows right after deploy.
- **One parameter change per cycle with minimum 20 observations.** This is a hard rule for the self-learning loop. Changing multiple parameters simultaneously makes it impossible to attribute improvement/degradation. 20 observations is the minimum for any conclusion.

## Outcome Resolution Bugs

- **Gamma API `startDate` ≠ actual window start.** `market.start_time` from Gamma API is the market creation time (~24h before the actual 15-min window). The actual window start is `end_time - 900s`. Using `start_time` directly for spot price lookups returns a price from a day ago, producing phantom outcomes.
- **SpotBuffer deque overflow for liquid pairs.** `deque(maxlen=10000)` with raw Binance `@trade` ticks (hundreds/sec for BTC/ETH) means the buffer holds only ~20-100s of history, not 900s. Searching for "closest to start_time" finds a price from the recent tiny window, not the actual window start. Fix: use DB-persisted spot snapshots (5s intervals, immune to overflow) for historical lookups. The buffer is fine for "current price" only.
- **Combined effect produces phantom P&L.** Wrong start_time + overflow buffer = wrong outcome determination. Positions get credited/debited based on incorrect YES/NO resolution. Always use `trade_db.get_spot_at_time()` for historical prices and `spot_buffer.get_price()` only for the current (latest) price.

## Common Mistakes

- **Heredoc in SSH:** Copy-pasting heredocs (`cat << 'EOF'`) over SSH often fails. Use multiple `printf` or `echo` commands instead.
- **Forgot to create venv:** Always `source .venv/bin/activate` before running `pip install -e .`
- **Wrong working directory:** Always `cd /opt/btc15minutebot` before running bot commands.
- **Port already in use:** Kill old process before starting new one. Check with `netstat -ano | findstr :8080`
- **MockState must match StateProvider protocol.** When adding new methods to StateProvider, always update MockState in test_risk_manager.py or tests will fail with AttributeError.
- **Test market timing matters with dynamic thresholds.** When tests use `start_offset=-300, end_offset=600` (progress = 1/3), they hit the boundary between first and middle third. Choose offsets clearly within a phase (e.g., `-200/700` for first third, `-450/450` for middle).
