"""Async token bucket rate limiter for Polymarket API calls.

Polymarket allows 60 requests/minute.  We default to 55/minute (~8%
safety margin) so that bursts and timing jitter never breach the hard
limit.
"""

from __future__ import annotations

import asyncio
import time

from src.monitoring.logger import get_logger


class RateLimiter:
    """Async token bucket rate limiter for API calls.

    Uses a token bucket algorithm where tokens refill at a constant rate.
    Callers await `acquire()` before making API calls. If the bucket is
    empty, acquire() blocks until enough tokens are available.

    Parameters
    ----------
    max_per_minute : int
        Maximum requests allowed per minute (default 55 of 60 limit).
    burst : int | None
        Maximum burst size. Defaults to max_per_minute (full bucket).
    timeout : float
        Maximum time (seconds) to wait for a token. Raises TimeoutError
        if exceeded. Default 30.0.
    """

    def __init__(
        self,
        max_per_minute: int = 55,
        burst: int | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._max_per_minute = max_per_minute
        self._burst = burst if burst is not None else max_per_minute
        self._timeout = timeout
        self._tokens: float = float(self._burst)  # start with a full bucket
        self._refill_rate: float = max_per_minute / 60.0  # tokens per second
        self._last_refill: float = time.monotonic()
        self._lock = asyncio.Lock()
        self._log = get_logger("rate_limiter")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def acquire(self, tokens: int = 1) -> None:
        """Acquire token(s), blocking if necessary.

        If not enough tokens are available, waits for the bucket to refill.
        Raises ``asyncio.TimeoutError`` if waiting exceeds *self._timeout*.

        Parameters
        ----------
        tokens : int
            Number of tokens to acquire (default 1).
        """
        deadline = time.monotonic() + self._timeout
        max_iterations = 10  # prevent infinite retry loops

        for _ in range(max_iterations):
            async with self._lock:
                self._refill()

                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return

                # Not enough tokens -- figure out how long to wait.
                wait = (tokens - self._tokens) / self._refill_rate
                remaining_budget = deadline - time.monotonic()

                if wait > remaining_budget:
                    raise asyncio.TimeoutError(
                        f"Rate limiter: would need to wait {wait:.2f}s "
                        f"but only {remaining_budget:.2f}s remain before timeout"
                    )

                self._log.warning(
                    "rate_limiter_waiting",
                    wait_seconds=round(wait, 3),
                    tokens_requested=tokens,
                    tokens_available=round(self._tokens, 3),
                )

            # Release the lock while sleeping so other coroutines can proceed.
            await asyncio.sleep(wait)

        # Should be unreachable in practice, but guard against bugs.
        raise asyncio.TimeoutError(  # pragma: no cover
            f"Rate limiter: exceeded {max_iterations} retry iterations"
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _refill(self) -> None:
        """Add tokens based on elapsed time since last refill.

        Called internally before checking available tokens.
        Caps at *self._burst*.
        """
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._burst, self._tokens + elapsed * self._refill_rate)
        self._last_refill = now

    # ------------------------------------------------------------------
    # Read-only properties / pre-flight checks
    # ------------------------------------------------------------------

    @property
    def available(self) -> float:
        """Current number of available tokens (approximate, no lock)."""
        elapsed = time.monotonic() - self._last_refill
        return min(self._burst, self._tokens + elapsed * self._refill_rate)

    @property
    def max_per_minute(self) -> int:
        """The configured rate limit."""
        return self._max_per_minute

    def has_capacity(self, tokens: int = 1) -> bool:
        """Check if enough tokens are available without acquiring.

        This is approximate (no lock) and useful for pre-flight checks.
        """
        return self.available >= tokens
