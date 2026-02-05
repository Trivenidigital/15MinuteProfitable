# Git Workflow Skill

## Branch Strategy
- `main` - stable, production-ready code
- `feat/*` - new features (e.g., `feat/price-lag-strategy`)
- `fix/*` - bug fixes (e.g., `fix/websocket-reconnect`)
- `refactor/*` - code improvements

## Commit Format
Conventional commits:
- `feat: add price-lag strategy detection`
- `fix: handle partial fill in arbitrage execution`
- `refactor: extract fee calculation to utils module`
- `test: add unit tests for orderbook fill computation`
- `docs: update strategy documentation`
- `chore: update dependencies`

## Workflow
1. Always create feature branch before work: `git checkout -b feat/task-name`
2. Commit after each meaningful change
3. Run tests before marking complete
4. Never force push to main
5. PR from feature branch to main
