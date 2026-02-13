# Lessons Learned

> Patterns, mistakes, and insights captured during development.
> Updated after every correction or discovery.

---

## Polymarket API Quirks

- **neg_risk auto-detection is broken** for BTC 15-min markets. The `/neg-risk` endpoint returns "Invalid token id". Always pre-provide `neg_risk` via `PartialCreateOrderOptions` read from config (`BOT_NEG_RISK`). Default is `False` — 15-min crypto markets return `neg_risk=false` from the CLOB API. Wrong neg_risk = invalid EIP-712 signature domain.
- **py-clob-client is synchronous only.** Must wrap with `asyncio.to_thread()` for async usage. Not thread-safe — one client instance per thread is safest.
- **Order signing latency (~1s)** is dominated by HTTP calls for tick_size and neg_risk. Pre-providing both in `PartialCreateOrderOptions` reduces signing to ~50ms.
- **Token prices/sizes in OrderBookSummary are strings**, not floats. Must cast explicitly.
- **USDC balance from API is in wei (6 decimals).** Divide by 1,000,000 for USD.
- **Magic.link accounts (signature_type=1):** The `funder` address must be the Polymarket proxy wallet, NOT the signer address. This is the #1 cause of "invalid signature" errors.
- **Every Polymarket account has THREE addresses:** (1) Login wallet (MetaMask/email address), (2) Magic.Link EOA (the signing key from your private key), (3) Proxy wallet (the "funder", holds USDC, shown in Settings > Profile > Address). The private key derives the EOA, but orders execute through the proxy wallet.
- **`funder` is ALWAYS the proxy wallet address** (the deposit address shown in your Polymarket profile), NOT the address derived from the private key. Getting this wrong causes "invalid signature" on every order.
- **`signature_type=1`** for all Magic.Link/email-based accounts; `signature_type=0` for MetaMask/EOA-connected accounts. Most accounts are type 1.
- **"invalid signature" is a catch-all error** from the CLOB API. It can mean: wrong private key, wrong funder address, wrong neg_risk value, wrong signature_type, or missing API creds. Debugging requires testing each independently.
- **Test a $0.01 limit order before going live.** A single tiny GTC limit order at $0.01 (will never fill) validates the entire signing pipeline: key, funder, sig_type, neg_risk, API creds. Would have caught all Feb 12 issues instantly.

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
- **Deep loss guard (stop_loss_floor_ratio=0.30) is defense layer 1.** Strategy-level `should_exit()` blocks all exits when `current_value / cost_basis < 0.30`. This prevents SL, time exit, and TP from selling positions that have lost >70% of value. Holding to resolution gives better EV than selling for $0.01/share.
- **Dust bid check (min_exit_bid=0.03) is defense layer 2.** Execution-level `_execute_exit()` refuses to sell when the best orderbook bid is below $0.03. Even if the strategy says "exit", the executor won't submit a sell into a dead market. This is defense-in-depth with the strategy-level guard.
- **7 catastrophic exits (<5% recovery) cost -$22.48 in one session.** Positions sold at $0.01/share into dead markets near expiry. The two guards above prevent this class of loss entirely.
- **fade_panic has 25.8% win rate — disable it.** While barely positive at resolution (+$5.63 on 64 trades), 2 catastrophic early exits wiped that out (-$5.25). The thesis (late-game odds shifts = panic) doesn't hold — they're actual price discovery.

## Strategy Co-location vs Separate Bots

- **New strategies belong in the same bot** unless they need a different API key, wallet, chain, or event loop. The shared infrastructure cost of a second bot (rate limit coordination, WebSocket duplication, state synchronization) far outweighs the isolation benefit.
- **Key shared resources that force co-location:** Rate limiter (60 orders/min per API key), WebSocket connections (~5 per IP), risk manager (needs global exposure view), orderbook manager, spot buffer, executor.
- **The BaseStrategy interface is the right abstraction.** Any strategy that follows evaluate() → Opportunity | None + should_exit() → bool fits cleanly. Don't split unless the pattern fundamentally doesn't fit.
- **Time-domain partitioning avoids conflicts naturally.** Price-Lag winds down at <30s, Sniper activates at T-120s. The scanner picks the best opportunity — no manual conflict resolution needed.
- **Per-strategy overrides in risk manager are the right pattern** for strategies with different risk profiles (e.g., sniper needs 3 entries per market vs default 2). Don't fork the risk manager.
- **"Hold to resolution" strategies are fine** — should_exit() returning False is trivially handled by the exit loop.
- **When to actually separate:** Different API keys/wallets, different chains, strategy needs its own event loop, or strategy is experimental enough that crashes are expected.

## Stop-Loss and Take-Profit

- **Widened SL/TP thresholds can silently disable them.** Changing SL from 8%→15% and TP from 15%→25% resulted in zero SL/TP triggers over 7+ hours. Combined with time decay (doubles thresholds mid-window, disables them in last third), the effective thresholds became unreachable. Always verify that SL/TP are actually firing after changing thresholds by checking log event counts.
- **DO NOT disable stop-loss time decay.** Experiment #2 (2026-02-08): disabled SL time decay → 0 winners in 15 trades, -$239 in 1h. Late-window prices on 15-min markets are extremely volatile; a -15% dip often reverses before resolution. The time decay (disable SL in last third) correctly models this. Disabling it kills recovery plays and converts potential asymmetric winners into guaranteed small losses. REVERTED after 1h15m.
- **The real bottleneck is base SL/TP thresholds, not time decay.** SL at 15% and TP at 25% are too wide to trigger in the first/middle portions of the window where time decay isn't interfering. Future tuning should lower base thresholds (try SL 8%, TP 15%) rather than changing the decay mechanism.
- **Triple-stacking race condition:** The 2s cooldown in RiskManager races with async order execution. The scanner evaluates the same market multiple times before `record_trade()` runs. Fix: track `_entry_counts` in StateManager (incremented inside the async lock in `record_trade()`), checked synchronously in `RiskManager.check_opportunity()` before allowing entries. `max_entries_per_market` config controls the cap (default: 2).
- **Take-profit time decay was inverted (FIXED).** The TP decay was copied from stop-loss logic: middle third doubled the threshold (0.17 → 0.34). For SL, doubling = protective (harder to stop out). For TP, doubling = can never capture gains (34% unreachable on 15-min markets). Fix: middle third now uses `* 0.6` (0.17 → 0.102), making TP easier to trigger when positions are at peak profit. Last third disabled (let winners ride) remains correct.
- **Don't copy SL patterns to TP without inverting the logic.** SL and TP have opposite goals: SL protects by being harder to trigger over time, TP captures by being easier to trigger at peak. Always check if the decay direction makes sense for the specific exit type.
- **Smart stop-loss layers:** Three layers work together: (1) cheap contract bypass (skip stop-loss for avg entry < $0.10), (2) time-decayed threshold (doubles in middle third, disabled in last third), (3) confirmation counter (3 consecutive triggers before exit). This prevents premature exits on temporary dips.
- **StateProvider protocol pattern:** When adding new state queries needed by RiskManager, extend the `StateProvider` protocol in `src/risk/manager.py`, implement in `StateManager`, and update `MockState` in tests. All 4 position deletion sites in StateManager must clean up new tracking dicts.

## Memory and Observability

- **OrderBook memory grows unboundedly** without cleanup. Expired 15-min market token IDs accumulate in `OrderBookManager._books`. Fix: `remove_stale_books()` called during market rollover, removes books not updated within 120s.
- **`snapshot_applied` dominated log output** at ~58% of all lines when at INFO level. Demoting to DEBUG reduced log volume by ~95% (from ~18,000 lines/min to ~878).
- **Stop-loss debug events are invisible in production.** Promote key stop-loss events (skipped_cheap, disabled_late_market, pending_confirmation) to INFO for rollout observability. These are low-volume events.

## Server/Deploy

- **SSH key auth is set up.** `~/.ssh/id_ed25519` → `root@89.167.55.176`. No more password prompts.
- **Server may have local changes.** When deploying, if `git checkout` fails with "local changes would be overwritten", run `git stash` first.
- **Server lacks `pgrep`.** Use `pidof python` or `systemctl status 15minuteprofitable` instead of `pgrep -f src.main` for process checks.
- **Large log file queries hang.** Don't `grep` the entire bot.log (3.5M+ lines). Use `tail -N` to limit input, or `awk '/timestamp/,0'` to scope to a time range.
- **Log rotation:** Consider setting up logrotate — the log file grows continuously and is already 3.6M+ lines.

## Observability Pipeline

- **TradeDatabase is gated on `dashboard_enabled`, but decision logging needs it always.** The trade_db initialization check was `if settings.dashboard_enabled`. Phase 1 observability extended this to `if settings.dashboard_enabled or settings.enable_decision_logging` so the DB is available even without the dashboard. Any future feature that needs SQLite should add its own config gate to this check.
- **Module-level mutable state for cross-loop coordination.** `_market_start_prices` and `_recorded_outcomes` are module-level dicts/sets used by `_market_outcome_loop` to track open prices across market lifecycles. This follows the same pattern as `_pending_gtc_orders` and `_pending_maker_arb_pairs`. Keep this pattern for state shared between loops rather than threading it through function params.
- **Market close price is approximate.** When a market expires, the `_market_outcome_loop` captures the current spot price as the close. Since detection runs every 10s, this can be up to 10s late. For higher accuracy, query `trade_db.get_spot_at_time()` using the market's `end_time` (relies on the 5s spot snapshot loop). This is good enough for directional win/loss determination.
- **ruff import sorting is strict.** Comments between import groups (like `# Dashboard (lazy)`) break isort formatting. Either remove inline comments or ensure imports are organized into clean standard/third-party/local groups without interleaving comments.

## Resolution Sniper Deployment

- **Circuit breaker persists across restarts.** The state snapshot saves daily PnL, and if the circuit breaker was activated (24h timer), restarting the bot doesn't clear it. Even changing `max_daily_loss` from $150 to $1000 didn't help because `is_circuit_breaker_active()` short-circuits before the daily loss check re-evaluates. Fix required a second restart to clear the in-memory flag. Consider: re-evaluate breaker against current settings on startup.
- **Admin dashboard must be restarted after SSH `.env` changes.** Pydantic's settings loader reads `os.environ` (higher priority) before the `.env` file (lower priority). When you edit `.env` via SSH/sed, the admin dashboard process's in-memory `os.environ` still holds the old values, so the dashboard shows stale settings. Always restart the admin dashboard after manual `.env` edits: `kill $(pidof -s python -m src.admin)` then relaunch.
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

## Documentation Discipline

- **ALWAYS update `docs/self-learning-lessons-strategies.html` after every change.** This is the living experiment log. Every config tweak, code fix, parameter change, or insight MUST be recorded before considering a task complete. Include: experiment entry, config table update, new insights, code change proposals, daily log, and timestamp.
- **Update `tasks/lessons.md` after every correction or discovery.** This captures development patterns and mistakes to prevent repeats.
- **Two docs serve different purposes.** The HTML experiment log tracks bot tuning (what changed, what happened, what we learned). The lessons.md tracks development patterns (coding mistakes, deployment gotchas, API quirks).

## Dynamic Allocation System

- **Exponential-decay weighted profit factor is the right metric.** Using `exp(-0.693 * age / half_life)` with a 30-min half-life over a 2-hour window naturally prioritizes recent performance while keeping some memory. Simple win rate or raw P&L would over-react to individual trades.
- **Multiplier bounds (0.1x–3.0x) prevent starvation and over-concentration.** Without a floor, a brief losing streak would zero out a strategy permanently. Without a ceiling, a hot streak could put 100% of capital in one strategy. These bounds keep all strategies alive while still meaningfully differentiating.
- **Cold start protection (5+ trades minimum) avoids noisy early signals.** With fewer than 5 trades, the profit factor is statistically meaningless. Return base_size unchanged until sufficient data accumulates.
- **Lazy recalculation (every 15 min) avoids per-trade overhead.** The allocation multipliers don't need to update on every trade. Recalculating once per market window is frequent enough to adapt while keeping the hot path fast.
- **Proportional normalization preserves relative ranking.** Each strategy's weighted profit factor is divided by the mean across all strategies with sufficient data, then multiplied by the number of scored strategies. This makes the multipliers a zero-sum rebalancing: capital flows from underperformers to outperformers without changing the total.

## Asymmetric Strategy Fixes

- **Unhedged resolution wipeout is the #1 loss pattern.** 54 unhedged trades lost -$2,970 total. Single-side accumulation going to resolution loses 100% of investment when the outcome goes against you. The fix: `asymmetric_require_hedge=True` ensures both YES and NO sides must be cheap before entering.
- **Never skip exit on heavy losses.** The old logic skipped time-based exit when `value_ratio < 0.30` ("already lost too much, hold for recovery"). This is wrong — it converts a known loss into a guaranteed total loss. Always exit unhedged positions before resolution, regardless of current loss severity.
- **Static allocation is a silent capital drain.** When losing strategies get the same order size as winners, the bot systematically transfers capital from profitable strategies to unprofitable ones. The allocation flip (fade_panic 30→50, losers 25→10) immediately improved capital efficiency.

## Overnight Monitoring Lessons (Feb 9 02:00-07:20 UTC, 5h20m)

- **Dead zone and time-remaining checks silently block late-game strategies.** `is_in_dead_zone(end_buffer=30)` and `MIN_TIME_REMAINING=30s` both prevent trades within 30s of market end. But sniper (T-120s) and fade_panic (T-120s) are designed to trade in the final 2 minutes. Symptom: strategies evaluate and find signals but risk manager rejects 100% after T-30s. Fix: add `_LATE_GAME_STRATEGIES` exemption in risk/manager.py. These strategies have their own hard stops at T-15s.
- **Sniper vol estimation is 4-6x too low in quiet markets.** The 10-minute rolling spot window captures calm periods, but BTC/ETH can have regime changes near market close. sigma=0.000126 produced 99.95% win confidence, but actual vol was 6x higher (BTC reversed 0.19% in 74s). Fix: raise `sniper_vol_floor` from 0.0001 to 0.0005 and `sniper_vol_multiplier` from 2.0 to 3.0.
- **`logger` vs `self._log` in class methods.** RiskManager's `adjust_size()` used `logger.warning()` but the class uses `self._log = get_logger("risk")`. This would crash at runtime when dust trades are rejected. Always verify the logger variable name when adding log calls to existing classes.
- **Multi-strategy attribution bug.** When fade_panic and sniper both trade the same condition_id, Position stores a single `strategy` field. Resolution creates one trade_result attributed to whichever strategy opened the position first. Fix: `get_position_strategy_breakdown()` queries the trades table for per-strategy shares, and `_save_attributed_results()` splits the trade_result into per-strategy records.
- **All strategies can go negative simultaneously.** Fade panic was the only profitable strategy (+$216) but crashed to -$172 overnight on XRP NO bets. With 31% win rate and symmetric payoffs ($64 avg win vs $64 avg loss), profitability is structurally impossible. Need either higher win rate or asymmetric payoff structure.
- **Position accumulation is the biggest risk factor.** Without `max_entries_per_market` cap, fade_panic entered 18 times in one window at $50 each = $900 unhedged. When market went wrong direction, entire $290 lost in one shot. Fix: capped at 5 entries * $25 = $125 max per market.
- **Fade panic threshold calibration matters enormously.** At 8% odds_shift_threshold, model predicted 54% WR but actual WR was 20%. At 15% threshold, no false signals fired during 5 consecutive quiet windows (06:15-07:00). Higher threshold = fewer but higher-quality signals.
- **Vol floor validation: post-fix probabilities are realistic.** After raising floor to 0.0005 and multiplier to 3.0: sniper reported 76.5% on ETH (won), 86.3% on SOL (lost). These are much more calibrated than 99.95% pre-fix. The floor prevents overconfident CDF in quiet markets.
- **ETH is the only profitable asset overnight.** ETH: 2 trades, 100% WR, +$329. All other assets negative. This suggests potential asset-specific strategy filtering.

## Afternoon Session Lessons (Feb 9 14:00-15:05 UTC)

- **Zero-pnl attribution bug root cause.** When multi-strategy positions (e.g., fade_panic + sniper on same condition_id) are resolved, the combined position is `is_hedged=True` (both yes_shares > 0 and no_shares > 0). The hedged branch in `state.py` NEVER set `report["outcome"]`, only the unhedged branch did. When `_save_attributed_results()` split per-strategy with empty outcome, unhedged sub-positions got `s_payout = s_investment` (fake breakeven). Fix: Always call `outcome_resolver` in hedged branch and set outcome. Changed fallback from breakeven to total loss.
- **Chainlink price feeds on Polygon: SOL is missing.** Only BTC/USD, ETH/USD, XRP/USD are available on Polygon mainnet. SOL/USD exists on Ethereum mainnet and Base but not Polygon. Design oracle filters to be permissive when feed is unavailable.
- **Raw JSON-RPC for Chainlink saves a dependency.** Instead of adding web3.py (~30MB), a simple `eth_call` with ABI encoding reads `latestRoundData()` in 3 lines. Chainlink price feeds always use 8 decimals. The function selector for `latestRoundData()` is `0xfeaf968c`.
- **Aggressive sizing amplifies losses.** $50/entry with 10 max entries produced -$67.88 in a single window (14:45-15:00). The 3.6:1 win/loss ratio observed at $25 may not hold at $50 if fill quality degrades at larger sizes.
- **Attribution accuracy matters for learning.** 10 fake-breakeven trades masked real P&L data. When computing strategy performance metrics, any systematic attribution error compounds through allocation multipliers, leading to suboptimal capital allocation.

## Fade Panic Window Tuning Options

Three options were evaluated for making fade_panic more aggressive. Option #2 was chosen.

### Option 1: Shrink window to 90s (compromise)
- Change `fade_panic_window_seconds` from 120 to 90
- **Pro:** Filters out noisier early signals (T-120 to T-90 often has weak panic signals)
- **Pro:** Still 30s of odds history at the midpoint for shift detection
- **Con:** Loses ~25% of opportunities from the first 30s of the current window
- **When to use:** If false positives in the T-120 to T-90 range are the dominant loss pattern

### Option 2: Keep 120s window + shrink hard stop to 10s (CHOSEN)
- Change `fade_panic_hard_stop_seconds` from 15 to 10
- **Pro:** Keeps full 120s odds history for signal quality
- **Pro:** Buys 5 more seconds at the most information-rich moment (closer to resolution = higher conviction)
- **Pro:** Resolution uncertainty decreases exponentially in the final seconds
- **Con:** Slightly higher execution risk (less time for order to fill before expiry)
- **When to use:** When signal detection is good but we're leaving money on the table by stopping too early

### Option 3: Shrink window to 60s (aggressive filtering)
- Change `fade_panic_window_seconds` from 120 to 60
- **CRITICAL PROBLEM:** Odds history is only recorded when inside the active window. With 60s window + 60s odds_window, at T-45s you'd only have 15s of odds data — most panics build over 30-60s and would be missed
- **Pro:** Only trades very late-game (higher probability of true panic)
- **Pro:** Less time exposed before resolution
- **Con:** Dramatically fewer opportunities due to odds history starvation
- **Con:** Would need to also shrink `fade_panic_odds_window_seconds` to match
- **When to use:** Only if you also restructure odds recording to happen outside the window (e.g., record always, gate execution only)

### Key insight
The window controls BOTH when the strategy activates AND when it records odds data. Shrinking the window doesn't just reduce the trading window — it starves the signal detector of historical data. The hard stop is a pure execution parameter with no signal-detection side effects.

## Strategy Machine-Gunning (Feb 9)

- **fade_panic fires every evaluation cycle** (~2s) when odds shift threshold is met. Each cycle creates a NEW order. On BTC: 13 trades, $34.64 deployed on one market. Fix: per-market investment cap (`fade_panic_max_per_market`).
- **Investment tracker must increment in evaluate()** not after execution. This is conservative (over-counts investment if opportunity is suppressed by conflict resolver), which is safer than under-counting.
- **Any strategy with a continuous trigger (not tranche-gated like sniper) needs a per-market cap.** Otherwise a persistent signal causes runaway position accumulation.

## Per-Strategy Inversion (Feb 9)

- **Global `invert_signals` flag inverts ALL strategies equally** — but each strategy has a different relationship with direction. Override `_maybe_invert()` per-strategy when needed.
- **Inversion mapping discovered empirically:**
  - price_lag: inverted = contrarian (fades spot noise) — profitable
  - fade_panic: original = fades panic — structurally correct
  - resolution_sniper: original = bets with CDF math — structurally correct
  - dip_buyer: original = mean reversion — structurally correct
- **Inverting to "hedge" two strategies against each other is a trap.** Both pay taker fees + winner fees. Net of fees, the guaranteed hedge is always negative EV.

## Strategy Conflict Detection (Feb 9)

- **Same-direction pileups are as bad as opposite-direction conflicts.** Two strategies betting NO on the same market doubles the loss when wrong. The conflict resolver must keep only highest-confidence per market regardless of direction.
- **Per-cycle conflict detection has a blind spot:** strategies firing in different cycles (e.g., sniper tranche at T-110, fade_panic at T-90) bypass the resolver. Need cross-cycle position tracking for full coverage.

## Sniper Confidence Calibration (Feb 9)

- **`sniper_min_confidence=0.50` is too low** — sniper traded at 0.567 (XRP) and 0.615 (ETH), lost $32 on ETH. Raised to 0.70. First post-fix trade: SOL at 94.96% confidence, won +$54.23.
- **The 3x high-confidence multiplier works well** — SOL trade used $93 instead of $10, turned a $0.68 profit into $6.86.

## Testing Patterns

- **AsyncMock `.closed` attribute is truthy by default.** When mocking `aiohttp.ClientSession`, `AsyncMock().closed` returns a `MagicMock` object (truthy), causing guards like `if self._session.closed` to trigger early return. Always set `mock_session.closed = False` explicitly. This bug silently skipped 9 tests without any assertion error — the mock just never reached the HTTP call.
- **Mock context managers need both `__aenter__` and `__aexit__`.** For `async with session.get(url) as resp:`, the mock needs: `mock_response.__aenter__ = AsyncMock(return_value=mock_response)` and `mock_response.__aexit__ = AsyncMock(return_value=False)`, plus `mock_session.get = MagicMock(return_value=mock_response)`.

## All-Time Performance Analysis Insights (Feb 10)

- **Only 1 of 6 strategies is profitable lifetime.** dip_buyer (+$27.94) is the only net positive strategy across 1400+ trades over 3 days. asymmetric (-$972), price_lag (-$795), fade_panic (-$425), resolution_sniper (-$186) are all losing. Don't keep unprofitable strategies running hoping they'll turn around — disable after sufficient data (100+ trades, 24h+ runtime).
- **Asymmetric payoff ratios matter more than win rates.** dip_buyer has 74% WR but also 25:1 win/loss ratio ($47 avg win vs $1.85 avg loss). Even at 50% WR it would be profitable. When evaluating strategies, look at the asymmetry of payoffs, not just win rate.
- **"Spray cheap contracts" is a losing approach.** Resolution sniper entries below $0.30 had terrible win rates — the average losing entry was $0.21 vs winning entry $0.53. Cheap isn't the same as good value. Set meaningful min entry price floors based on actual win-rate-by-price-bucket analysis.
- **Loosened filters accumulate losses silently.** Experiment #29b loosened fade_panic thresholds (odds shift 0.15→0.06, spot max change 0.0005→0.002) for "max signal throughput." The result: -$425 lifetime. More signals ≠ more profit. Tight filters with fewer but higher-quality entries beat loose filters with volume.
- **Scale winners, kill losers — don't allocate uniformly.** When you identify a strategy with proven edge (dip_buyer: +$27.94, 74% WR, 25:1 payoff), scale it up aggressively. When a strategy has no edge in any asset/timeframe/condition (asymmetric: -$972), disable it entirely. Don't split capital equally.
- **All-time data > single-session data.** The Feb 10 8-hour analysis showed fade_panic at +$158 and dip_buyer at +$59. The all-time analysis (3 days) showed fade_panic at -$425 and dip_buyer at +$27.94. Single sessions can be misleading due to regime effects. Always check all-time performance before making parameter decisions.

## 8-Day Full Audit & Radical Simplification (Feb 10)

- **Taker fees are 58.5% of total losses.** Over 8 days, taker fees were $1,492 out of $2,550 total losses. The fee structure (MAX_RATE * 4 * price * (1-price), peaks at 3.15% at 50/50 odds) is the primary structural barrier to profitability. Any strategy taking FOK orders must overcome ~5.6% drag on invested capital. Maker orders (GTC limit) pay 0% fees — this is the path to profitability.
- **price_lag would be profitable as maker.** The 8-day audit showed price_lag at -$795 with taker fees, but +$174 hypothetical without fees. Converting to maker (GTC limit) orders would flip it profitable. This is the strongest signal in the entire dataset.
- **SOL and XRP are bleeding assets.** SOL (-$1,571) and XRP (-$1,015) account for 100%+ of total losses. BTC (-$111) and ETH (+$148) are near breakeven. Asset selection matters as much as strategy selection.
- **CDF model is catastrophically overconfident.** Resolution sniper predicted 95%+ confidence but actual win rate was 20.7%. Root causes: vol floor too low (sigma < 0.0005 = 0% WR), vol multiplier insufficient (3.0x when reality needs 8.0x+), normal distribution doesn't model crypto mean-reversion and fat tails. Market price is a better probability estimator than the CDF model.
- **Radical simplification beats incremental tuning.** After 8 days of tuning 6 strategies × 4 assets (24 combinations), the winning move was reducing to 1 strategy × 2 assets. Fewer moving parts = faster learning, easier attribution, lower fee drag.
- **DRY_RUN saved real money.** All $2,550 in losses were simulated. The learning was free. Never go live until a strategy proves profitable in simulation for an extended period.
- **Multi-tranche position accumulation compounds losses.** Sniper's 3-tranche system meant a losing market got 3x the exposure. Single-tranche (max_tranches=1) limits damage on wrong calls.
- **Per-strategy signal inversion doesn't work for CDF-based strategies.** Experiment #37 (invert sniper) failed because the underlying model is miscalibrated — inverting a broken signal is still a broken signal. Fix the model first, then consider inversion.

## Planned Risk Rules

- **4 consecutive losses → 3-hour strategy cooldown.** If any single strategy accumulates 4 consecutive losses, that strategy enters a 3-hour cooldown (no new entries). Other strategies continue trading. Counter resets on any win. Rationale: prevents a strategy from bleeding capital during an unfavorable market regime. Implementation pending.

## Vault / Secrets Management

- **VAULT_PASSWORD and VAULT_PATH must NOT go in .env with BOT_ prefix settings.** Pydantic BaseSettings with `env_prefix="BOT_"` and `extra="forbid"` will reject any env var without the BOT_ prefix if loaded from the same env file. Solution: put vault config in a separate `.vault-password.env` loaded via a second systemd `EnvironmentFile=` directive.
- **Shell escaping of special chars in passwords.** Bash `!` expands in double-quoted strings and sometimes even in single-quoted heredocs depending on context. Avoid special characters in vault passwords, or write the file using Python (`python3 -c "open(...).write(...)"`) to bypass shell escaping entirely.
- **VaultSettingsSource priority is lower than env vars.** If both `.env` has `BOT_PRIVATE_KEY` and the vault has `private_key`, the env var wins. Must fully remove the env var for vault to take effect.

## Server Migration

- **Cross-contamination between bots sharing a server is hard to fully clean.** When two bots share a VPS, git remotes, systemd service files, cron jobs, and env files can reference the wrong bot. The nuclear option (new server) is often faster and cleaner than surgical cleanup.
- **Hetzner kernel upgrades can hang the server.** `apt upgrade` on Ubuntu 24.04 can install a new linux-image that requires reboot. The server may become unresponsive — power cycle from Hetzner Cloud console.
- **SCP between two remote servers from Windows doesn't work directly.** Use pipe relay: `ssh server1 "cat file" | ssh server2 "cat > file"` to transfer files via the local machine.

## Test Environment Pollution

- **`os.environ` mutations bypass monkeypatch cleanup.** If application code does `os.environ["KEY"] = value` (e.g., admin save settings handler), monkeypatch won't restore it unless you pre-register the key with `monkeypatch.delenv("KEY", raising=False)`. This caused 5 test failures where `BOT_DRY_RUN=true` leaked from admin tests into risk manager tests.
- **Test execution order matters for env pollution.** Tests pass individually but fail in suite when earlier test modules pollute `os.environ`. Always run the full suite (`pytest tests/`) to catch this, not just individual files.

## Live Trading Pre-Flight (Feb 12, 2026)

- **py-clob-client expects typed objects, not dicts.** `create_order()` expects `OrderArgs` and `PartialCreateOrderOptions` dataclasses (attribute access like `order_args.token_id`), not plain dicts (dict access like `order_args["token_id"]`). Using dicts causes `AttributeError` on first live order.
- **ClobClient needs `create_or_derive_api_creds()` + `set_api_creds()` after construction.** Without this, all authenticated API requests fail. The `scripts/setup_api_key.py` does it correctly but the executor didn't.
- **Risk limits must be proportional to bankroll.** Absolute limits ($500 daily loss, $10k position) are meaningless for a $132 bankroll. Added bankroll-proportional validation: max_daily_loss < 15% bankroll, max_total_position < 50%, max_position_per_market < 25%, max_unhedged_exposure < 25%, order_size < 15%.
- **Trades table needs dry_run column.** Without it, dry-run data contaminates live metrics. Added `dry_run INTEGER NOT NULL DEFAULT 0` with migration for existing DBs.

## Common Mistakes

- **Heredoc in SSH:** Copy-pasting heredocs (`cat << 'EOF'`) over SSH often fails. Use multiple `printf` or `echo` commands instead.
- **Forgot to create venv:** Always `source .venv/bin/activate` before running `pip install -e .`
- **Wrong working directory:** Always `cd /opt/15minuteprofitable` before running bot commands.
- **Port already in use:** Kill old process before starting new one. Check with `netstat -ano | findstr :8080`
- **MockState must match StateProvider protocol.** When adding new methods to StateProvider, always update MockState in test_risk_manager.py or tests will fail with AttributeError.
- **Test market timing matters with dynamic thresholds.** When tests use `start_offset=-300, end_offset=600` (progress = 1/3), they hit the boundary between first and middle third. Choose offsets clearly within a phase (e.g., `-200/700` for first third, `-450/450` for middle).
