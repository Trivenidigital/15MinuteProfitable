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

## Common Mistakes

- **Heredoc in SSH:** Copy-pasting heredocs (`cat << 'EOF'`) over SSH often fails. Use multiple `printf` or `echo` commands instead.
- **Forgot to create venv:** Always `source .venv/bin/activate` before running `pip install -e .`
- **Wrong working directory:** Always `cd /opt/btc15minutebot` before running bot commands.
- **Port already in use:** Kill old process before starting new one. Check with `netstat -ano | findstr :8080`
