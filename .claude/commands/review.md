Review the current changes for production readiness:

1. Run `git diff` to see all changes
2. Check for:
   - Security issues (exposed secrets, injection vulnerabilities)
   - Error handling gaps (unhandled exceptions, missing retries)
   - Fee calculation correctness (taker + winner fees accounted for)
   - Position safety (unhedged exposure, missing unwind logic)
   - Type safety (no `Any` types, proper typing)
   - Test coverage for new code
3. Run tests: `pytest tests/ -v`
4. Run linter: `ruff check src/ tests/`
5. Run type checker: `mypy src/`
6. Provide summary of findings with severity ratings
