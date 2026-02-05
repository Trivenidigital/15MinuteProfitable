# Project: Polymarket 15-Minute Crypto Trading Bot

## Quick Context
- **Language:** Python 3.11+
- **Framework:** asyncio + aiohttp
- **Package Manager:** pip (requirements.txt) / uv
- **Key SDK:** py-clob-client (Polymarket official Python CLOB client)
- **Chain:** Polygon Mainnet (chain_id=137)
- **Collateral:** USDC.e (`0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174`)

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
│   └── state.py         # Global state manager (positions, P&L, exposure)
├── data/                # Data layer (feeds, orderbook)
│   ├── clob_ws.py       # CLOB WebSocket client (orderbook deltas)
│   ├── rtds_ws.py       # RTDS WebSocket client (market lifecycle)
│   ├── binance_ws.py    # Binance WebSocket (spot price feeds)
│   ├── orderbook.py     # L2 orderbook state management
│   └── market_discovery.py  # Gamma API market scanner
├── strategy/            # Strategy engine
│   ├── base.py          # Abstract strategy interface
│   ├── arbitrage.py     # Fee-adjusted pure arbitrage (Strategy A)
│   ├── asymmetric.py    # Asymmetric entry accumulation (Strategy B)
│   ├── price_lag.py     # Price-lag exploitation (Strategy C)
│   └── scanner.py       # Multi-market opportunity scanner (Strategy D)
├── execution/           # Order execution engine
│   ├── executor.py      # Order signing, submission, fill tracking
│   ├── parallel_signer.py  # Parallel order pre-signing
│   └── unwind.py        # Emergency position flattening
├── risk/                # Risk management
│   ├── manager.py       # Position limits, exposure monitoring
│   ├── sizing.py        # Kelly criterion position sizing
│   └── circuit_breaker.py  # Loss limits, kill switches
├── monitoring/          # Logging, alerts, metrics
│   ├── logger.py        # Structured logging setup
│   ├── alerts.py        # Telegram/Discord notifications
│   └── metrics.py       # P&L tracking, performance metrics
└── utils/               # Shared utilities
    ├── fees.py          # Fee calculation (taker + winner)
    └── time_utils.py    # 15-minute window alignment helpers
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

## Conventions
- Commit format: conventional commits (feat:, fix:, refactor:)
- Branch naming: feat/description, fix/description
- File naming: snake_case for all Python files
- Class naming: PascalCase
- Async functions: prefix with no special convention, use `async def`
- Config: all via environment variables loaded through Pydantic BaseSettings
- Logging: structured JSON logging via `structlog`
