"""Comprehensive async tests for src.utils.rate_limiter.RateLimiter."""

from __future__ import annotations

import asyncio
import os
import time
from unittest.mock import patch

import pytest

os.environ.setdefault("BOT_PRIVATE_KEY", "0x" + "ab" * 32)

from src.utils.rate_limiter import RateLimiter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MONO_MODULE = "time.monotonic"


def _make_limiter(**kwargs) -> RateLimiter:
    """Shortcut that creates a RateLimiter with test-friendly defaults."""
    return RateLimiter(**kwargs)


# ---------------------------------------------------------------------------
# Initial state
# ---------------------------------------------------------------------------


class TestInitialState:
    """Verify the limiter starts with a full bucket."""

    def test_full_bucket(self) -> None:
        rl = _make_limiter(max_per_minute=10, burst=10)
        assert rl.available == pytest.approx(10.0, abs=0.5)

    def test_has_capacity_true(self) -> None:
        rl = _make_limiter(max_per_minute=10)
        assert rl.has_capacity(1) is True

    def test_has_capacity_for_full_burst(self) -> None:
        rl = _make_limiter(max_per_minute=10, burst=10)
        assert rl.has_capacity(10) is True


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


class TestProperties:
    """Test read-only properties."""

    def test_max_per_minute(self) -> None:
        rl = _make_limiter(max_per_minute=42)
        assert rl.max_per_minute == 42

    def test_available_approximate(self) -> None:
        rl = _make_limiter(max_per_minute=60, burst=60)
        # Right after construction, available should be close to burst.
        assert 59.0 <= rl.available <= 60.0

    def test_default_max_per_minute(self) -> None:
        rl = _make_limiter()
        assert rl.max_per_minute == 55


# ---------------------------------------------------------------------------
# acquire() - immediate success
# ---------------------------------------------------------------------------


class TestAcquireImmediate:
    """Acquire should succeed immediately when tokens are available."""

    async def test_single_acquire(self) -> None:
        rl = _make_limiter(max_per_minute=60, burst=60)
        await rl.acquire(1)
        # Should have consumed one token.
        assert rl.available < 60.0

    async def test_acquire_n_tokens(self) -> None:
        rl = _make_limiter(max_per_minute=60, burst=10)
        await rl.acquire(5)
        assert rl.available == pytest.approx(5.0, abs=0.5)

    async def test_deplete_bucket(self) -> None:
        """Acquire all tokens one-by-one."""
        rl = _make_limiter(max_per_minute=600, burst=5)
        for _ in range(5):
            await rl.acquire(1)
        # Bucket should be near-empty now.
        assert rl.available < 1.0


# ---------------------------------------------------------------------------
# has_capacity after depletion
# ---------------------------------------------------------------------------


class TestHasCapacityAfterDepletion:
    """has_capacity should return False once the bucket is empty."""

    async def test_no_capacity_after_drain(self) -> None:
        rl = _make_limiter(max_per_minute=600, burst=3)
        await rl.acquire(3)
        assert rl.has_capacity(1) is False

    async def test_partial_capacity(self) -> None:
        rl = _make_limiter(max_per_minute=600, burst=5)
        await rl.acquire(3)
        assert rl.has_capacity(2) is True
        assert rl.has_capacity(3) is False


# ---------------------------------------------------------------------------
# Token refill over time (mocked monotonic)
# ---------------------------------------------------------------------------


class TestRefill:
    """Tokens should refill based on elapsed time."""

    async def test_refill_after_time(self) -> None:
        """Using mocked time.monotonic, confirm tokens refill."""
        clock = 1000.0

        def fake_monotonic():
            return clock

        with patch(_MONO_MODULE, side_effect=fake_monotonic):
            # 60 per minute = 1 per second refill rate
            rl = _make_limiter(max_per_minute=60, burst=60)

        # Drain the bucket.
        with patch(_MONO_MODULE, side_effect=fake_monotonic):
            await rl.acquire(60)
            assert rl.available < 1.0

        # Advance time by 10 seconds => 10 tokens should be refilled.
        clock = 1010.0
        with patch(_MONO_MODULE, side_effect=fake_monotonic):
            # Trigger refill via available property.
            avail = rl.available
            assert avail == pytest.approx(10.0, abs=0.5)

    async def test_refill_caps_at_burst(self) -> None:
        """Tokens should never exceed burst size, even after long idle."""
        clock = 1000.0

        def fake_monotonic():
            return clock

        with patch(_MONO_MODULE, side_effect=fake_monotonic):
            rl = _make_limiter(max_per_minute=60, burst=10)

        # Advance time by 120 seconds (would give 120 tokens if uncapped).
        clock = 1120.0
        with patch(_MONO_MODULE, side_effect=fake_monotonic):
            assert rl.available == pytest.approx(10.0, abs=0.01)


# ---------------------------------------------------------------------------
# acquire() - blocking / waiting for refill
# ---------------------------------------------------------------------------


class TestAcquireBlocking:
    """acquire() should block when tokens are insufficient, then succeed."""

    async def test_acquire_waits_for_refill(self) -> None:
        """With a high refill rate, acquire should succeed after a brief wait."""
        # 6000 per minute = 100 per second => 1 token in 10ms
        rl = _make_limiter(max_per_minute=6000, burst=1, timeout=5.0)
        await rl.acquire(1)  # drain the bucket

        t0 = time.monotonic()
        await rl.acquire(1)
        elapsed = time.monotonic() - t0
        # Should have waited roughly 10ms (give generous tolerance).
        assert elapsed < 2.0  # definitely not stuck

    async def test_acquire_multiple_after_wait(self) -> None:
        """Request more tokens than available; should block then succeed."""
        # 3000/min = 50/sec => 3 tokens in 60ms
        rl = _make_limiter(max_per_minute=3000, burst=2, timeout=5.0)
        await rl.acquire(2)  # drain

        t0 = time.monotonic()
        await rl.acquire(1)
        elapsed = time.monotonic() - t0
        assert elapsed < 2.0


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------


class TestTimeout:
    """acquire() should raise asyncio.TimeoutError when it cannot fulfill."""

    async def test_timeout_raised(self) -> None:
        """With a tiny rate and short timeout, the limiter must time out."""
        # 1 per minute = 0.0167/sec. Timeout of 0.05s is far too short
        # to accumulate even a single token after draining.
        rl = _make_limiter(max_per_minute=1, burst=1, timeout=0.05)
        await rl.acquire(1)  # drain

        with pytest.raises(asyncio.TimeoutError):
            await rl.acquire(1)

    async def test_timeout_with_large_request(self) -> None:
        """Requesting many more tokens than possible should timeout fast."""
        rl = _make_limiter(max_per_minute=1, burst=1, timeout=0.05)
        with pytest.raises(asyncio.TimeoutError):
            await rl.acquire(100)


# ---------------------------------------------------------------------------
# Burst parameter
# ---------------------------------------------------------------------------


class TestBurst:
    """The burst parameter limits the maximum number of instant tokens."""

    def test_burst_limits_initial_tokens(self) -> None:
        rl = _make_limiter(max_per_minute=100, burst=5)
        assert rl.available == pytest.approx(5.0, abs=0.5)

    async def test_cannot_acquire_more_than_burst(self) -> None:
        """Acquiring more than burst at once should block or time out."""
        rl = _make_limiter(max_per_minute=60, burst=3, timeout=0.05)
        with pytest.raises(asyncio.TimeoutError):
            await rl.acquire(10)

    def test_burst_defaults_to_max_per_minute(self) -> None:
        rl = _make_limiter(max_per_minute=42)
        assert rl.available == pytest.approx(42.0, abs=0.5)


# ---------------------------------------------------------------------------
# Concurrent access
# ---------------------------------------------------------------------------


class TestConcurrency:
    """Multiple concurrent acquire() calls should be serialized by the lock."""

    async def test_concurrent_acquires_no_overdraw(self) -> None:
        """Launch many concurrent acquires; total consumed must not exceed
        what was available + what refilled during the test."""
        # 6000/min = 100/sec.  Burst=20 so 20 instant tokens.
        rl = _make_limiter(max_per_minute=6000, burst=20, timeout=5.0)

        results: list[bool] = []

        async def try_acquire() -> None:
            await rl.acquire(1)
            results.append(True)

        tasks = [asyncio.create_task(try_acquire()) for _ in range(20)]
        await asyncio.gather(*tasks)

        assert len(results) == 20
        # After 20 acquires from a burst-20 bucket, available should be low.
        # (Some refill may have happened during the test, so just sanity check.)
        assert rl.available < 5.0

    async def test_concurrent_acquires_some_timeout(self) -> None:
        """With tight burst and timeout, some concurrent acquires should fail."""
        rl = _make_limiter(max_per_minute=60, burst=3, timeout=0.05)

        succeeded = 0
        failed = 0

        async def try_acquire() -> None:
            nonlocal succeeded, failed
            try:
                await rl.acquire(1)
                succeeded += 1
            except asyncio.TimeoutError:
                failed += 1

        tasks = [asyncio.create_task(try_acquire()) for _ in range(10)]
        await asyncio.gather(*tasks)

        assert succeeded >= 3  # at least the burst tokens
        assert failed > 0  # some must have timed out


# ---------------------------------------------------------------------------
# Custom parameters
# ---------------------------------------------------------------------------


class TestCustomParameters:
    """RateLimiter should respect non-default construction args."""

    def test_custom_max_per_minute(self) -> None:
        rl = _make_limiter(max_per_minute=120)
        assert rl.max_per_minute == 120
        # refill rate = 120/60 = 2 tokens/sec
        assert rl._refill_rate == pytest.approx(2.0)

    def test_custom_burst(self) -> None:
        rl = _make_limiter(max_per_minute=60, burst=5)
        assert rl._burst == 5
        assert rl.available == pytest.approx(5.0, abs=0.5)

    def test_custom_timeout(self) -> None:
        rl = _make_limiter(max_per_minute=60, timeout=99.9)
        assert rl._timeout == pytest.approx(99.9)

    async def test_high_rate_fast_refill(self) -> None:
        """A very high rate should refill almost instantly."""
        rl = _make_limiter(max_per_minute=60000, burst=1, timeout=2.0)
        await rl.acquire(1)  # drain
        # 60000/min = 1000/sec => 1 token in 1ms
        await rl.acquire(1)  # should succeed near-instantly


# ---------------------------------------------------------------------------
# acquire(N) for N > 1
# ---------------------------------------------------------------------------


class TestAcquireMultiple:
    """Test acquiring more than one token at a time."""

    async def test_acquire_exact_burst(self) -> None:
        rl = _make_limiter(max_per_minute=60, burst=10)
        await rl.acquire(10)
        assert rl.available < 1.0

    async def test_acquire_leaves_remainder(self) -> None:
        rl = _make_limiter(max_per_minute=60, burst=10)
        await rl.acquire(7)
        assert rl.available == pytest.approx(3.0, abs=0.5)

    async def test_acquire_zero_is_noop(self) -> None:
        """Acquiring 0 tokens should succeed without consuming anything."""
        rl = _make_limiter(max_per_minute=60, burst=10)
        await rl.acquire(0)
        assert rl.available == pytest.approx(10.0, abs=0.5)
