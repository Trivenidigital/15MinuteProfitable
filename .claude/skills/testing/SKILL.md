# Testing Skill

## Framework
- pytest with pytest-asyncio for async test support
- pytest-cov for coverage reporting

## Commands
- Run all tests: `pytest tests/ -v`
- Run with coverage: `pytest tests/ -v --cov=src --cov-report=html`
- Run specific module: `pytest tests/unit/test_fees.py -v`
- Run integration tests: `pytest tests/integration/ -v -m integration`

## Conventions
- Unit tests in `tests/unit/`, integration in `tests/integration/`
- Test files mirror source structure: `src/utils/fees.py` → `tests/unit/test_fees.py`
- Use `@pytest.mark.asyncio` for async tests
- Use `@pytest.mark.integration` for tests hitting live APIs
- Mock external APIs in unit tests, use real APIs only in integration tests
- Every strategy must have tests with known orderbook states verifying correct opportunity detection
- Fee calculations must be tested at: 0.01, 0.10, 0.25, 0.40, 0.50, 0.60, 0.75, 0.90, 0.99
