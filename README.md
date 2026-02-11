# Polymarket 15-Minute Crypto Trading Bot

An advanced trading bot for Polymarket's 15-minute cryptocurrency UP/DOWN markets (BTC, ETH, SOL, XRP).

## Strategies

| Strategy | Type | Description |
|----------|------|-------------|
| **Fee-Adjusted Arbitrage** | Market-neutral | Buy YES+NO when combined cost minus all fees yields profit |
| **Asymmetric Entry** | Market-neutral | Accumulate cheap shares over time using 0% maker-fee limit orders |
| **Price-Lag Exploitation** | Directional | Trade when Polymarket odds lag behind spot price movements |
| **Multi-Market Scanner** | Orchestration | Scan BTC/ETH/SOL/XRP markets simultaneously for best opportunities |

## Quick Start

```bash
# Clone and setup
git clone <repo-url>
cd 15MinuteProfitable
python -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env with your Polymarket credentials

# Run in simulation mode (default)
python -m src.main

# Run tests
pytest tests/ -v
```

## Configuration

All configuration is via environment variables (prefix `BOT_`). See `.env.example` for all options.

**Required:**
- `BOT_PRIVATE_KEY` - Your Ethereum private key
- `BOT_SIGNATURE_TYPE` - Wallet type (0=EOA, 1=Magic.link, 2=Gnosis)

**Important:** The bot starts in `DRY_RUN=true` (simulation mode) by default. Set `BOT_DRY_RUN=false` to enable live trading.

## Architecture

See `CLAUDE.md` for detailed architecture documentation and `tasks/todo.md` for the full implementation plan.

## Development

```bash
# Lint
ruff check src/ tests/

# Format
ruff format src/ tests/

# Type check
mypy src/

# Test with coverage
pytest tests/ -v --cov=src
```

## License

Private - All rights reserved.
