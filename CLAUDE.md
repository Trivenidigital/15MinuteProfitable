# Project: Polymarket 15-Minute Crypto Trading Bot

## Quick Context
- **Language:** Python 3.11+
- **Framework:** asyncio + aiohttp
- **Package Manager:** pip (requirements.txt) / uv
- **Key SDK:** py-clob-client (Polymarket official Python CLOB client)
- **Chain:** Polygon Mainnet (chain_id=137)
- **Collateral:** USDC.e (`0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174`)

## Cloud Server
- **IP:** `46.62.206.192` (Hetzner VPS, Helsinki)
- **SSH:** `ssh root@46.62.206.192` (key-based auth, no password needed)
- **SSH Key:** `~/.ssh/id_ed25519` (ed25519, configured via ssh-copy-id)
- **Bot path:** `/opt/btc15minutebot/`
- **Config:** `/opt/btc15minutebot/.env`
- **Branch on server:** `feat/risk-manager` (active development branch)
- **Service:** `systemctl {start|stop|restart|status} btc15minutebot`
- **Logs:** `/var/log/btc15minutebot/bot.log` (stdout) and `error.log` (stderr)
- **Database:** `/opt/btc15minutebot/data/trades.db` (SQLite)
- **Dashboard:** `http://46.62.206.192:8080`
- **Note:** Server does not have `pgrep` installed — use `pidof` or `systemctl` for process checks

## Key Commands
- Run bot: `python -m src.main`
- Run tests: `pytest tests/ -v`
- Lint: `ruff check src/ tests/`
- Format: `ruff format src/ tests/`
- Type check: `mypy src/`
- Simulation: `DRY_RUN=true python -m src.main`

## Architecture
```
src/
├── main.py              # Entry point, async event loop orchestration
├── config.py            # Pydantic settings, env var loading
├── core/                # Core domain models and state
│   ├── models.py        # Trade, Position, Opportunity, Market dataclasses
│   └── state.py         # Global state manager (positions, P&L, exposure, recovery)
├── data/                # Data layer (feeds, orderbook, persistence)
│   ├── clob_ws.py       # CLOB WebSocket client (orderbook deltas)
│   ├── rtds_ws.py       # RTDS WebSocket client (market lifecycle)
│   ├── binance_ws.py    # Binance WebSocket (spot price feeds)
│   ├── orderbook.py     # L2 orderbook state management
│   ├── spot_buffer.py   # Rolling spot price buffer
│   ├── market_discovery.py  # Gamma API market scanner
│   ├── market_manager.py    # Market lifecycle + rollover management
│   ├── trade_db.py      # SQLite persistence (trades, decisions, spots, outcomes)
│   └── decision_logger.py   # Strategy decision capture per scan cycle
├── strategy/            # Strategy engine
│   ├── base.py          # Abstract strategy interface
│   ├── arbitrage.py     # Fee-adjusted pure arbitrage (Strategy A)
│   ├── asymmetric.py    # Asymmetric entry accumulation (Strategy B)
│   ├── price_lag.py     # Price-lag exploitation (Strategy C)
│   └── scanner.py       # Multi-market opportunity scanner (Strategy D)
├── execution/           # Order execution engine
│   ├── executor.py      # Order signing, submission, fill tracking + parallel signing
│   └── unwind.py        # Emergency position flattening
├── risk/                # Risk management
│   ├── manager.py       # Position limits, exposure, circuit breaker, Kelly integration
│   └── sizing.py        # Kelly criterion position sizing
├── monitoring/          # Logging, alerts, metrics
│   ├── logger.py        # Structured logging setup
│   ├── alerts.py        # Telegram/Discord alert dispatcher
│   └── metrics.py       # Performance metrics + daily summary
└── utils/               # Shared utilities
    ├── fees.py          # Fee calculation (taker + winner)
    ├── fee_verifier.py  # Startup fee sanity check
    ├── pid_lock.py      # PID lock file management
    ├── rate_limiter.py  # Token bucket rate limiter
    └── time_utils.py    # 15-minute window alignment helpers
deploy/
├── btc15minutebot.service  # systemd unit file
├── logrotate.conf          # Log rotation config
└── README.md               # VPS deployment guide
```

## API Endpoints
| Service | URL | Auth |
|---------|-----|------|
| CLOB REST | `https://clob.polymarket.com` | L2 API key (HMAC) |
| CLOB WebSocket | `wss://ws-subscriptions-clob.polymarket.com/ws/market` | None for market data |
| RTDS | `wss://ws-live-data.polymarket.com` | None |
| Gamma API | `https://gamma-api.polymarket.com` | None |
| Binance WS | `wss://stream.binance.com:9443/ws` | None |

## Rate Limits
- **CLOB public REST:** 100 requests/min per IP
- **Order placement:** 60 orders/min per API key
- **Order cancellation:** 200 cancels/min per API key
- **WebSocket:** ~5 concurrent connections per IP

## Fee Model
- **Taker fee:** `MAX_RATE * 4 * price * (1 - price)` where MAX_RATE ≈ 3.15%. Peaks at 50/50 odds.
- **Maker fee:** 0%
- **Winner fee:** 2% on profits at resolution
- **Implication:** Pure YES+NO arb needs spreads below ~$0.94 combined at mid-range to profit

## Critical Rules
IMPORTANT: Always run tests before marking tasks complete
IMPORTANT: Create feature branch before any implementation work
IMPORTANT: All fees (taker + winner) must be accounted for in profit calculations
IMPORTANT: Never leave positions unhedged unless explicitly in directional strategy
IMPORTANT: Order signing takes ~1s; always pre-sign in parallel where possible
IMPORTANT: neg_risk=True must be hardcoded for BTC/ETH/SOL 15-min markets
NEVER: Commit directly to main
NEVER: Use `Any` type in Python (use proper typing)
NEVER: Skip error handling on WebSocket reconnections
NEVER: Store private keys or API secrets in code
NEVER: Submit market orders without checking orderbook depth first

## Self-Learning System
- **Vision:** Autonomous Analyze → Tune → Measure loop until profitable in DRY_RUN, then go live
- **Agent:** Claude Code SSHs into Hetzner VPS to pull data, analyze, update `.env`, restart bot
- **Autonomy:** Parameter changes are fully autonomous; strategy code changes require human approval
- **Sprint:** 7-day observation period. Deployed Phase 1 observability 2026-02-07 22:25 UTC. Baseline collection in progress.
- **Key finding (pre-sprint):** Arbitrage alone produces zero opportunities — spreads $1.01-$1.05 vs $0.94 target. Must enable directional strategies (price_lag first).
- **Three memory files:**
  - `docs/strategy-self-learn.html` — System plan, cadence, parameter priority, 7-day game plan (rarely changes)
  - `docs/self-learning-lessons-strategies.html` — Living experiment log, insights, market patterns (updated every cycle)
  - `tasks/lessons.md` — Development lessons and patterns (updated after corrections/discoveries)
- **Safety:** One parameter per cycle, max 50% change, min 20 observations, revert on degradation
- **Deploy workflow:** `git push` → SSH `git pull` → `systemctl restart btc15minutebot`. No pip install needed for pure Python changes.

## Conventions
- Commit format: conventional commits (feat:, fix:, refactor:)
- Branch naming: feat/description, fix/description
- File naming: snake_case for all Python files
- Class naming: PascalCase
- Async functions: prefix with no special convention, use `async def`
- Config: all via environment variables loaded through Pydantic BaseSettings
- Logging: structured JSON logging via `structlog`
