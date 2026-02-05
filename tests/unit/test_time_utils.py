"""Comprehensive tests for src.utils.time_utils."""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from src.utils.time_utils import (
    WINDOW_SECONDS,
    align_to_window,
    compute_slug,
    time_remaining_seconds,
    is_in_dead_zone,
    current_window_timestamps,
    next_window_timestamps,
)

# ---------------------------------------------------------------------------
# align_to_window
# ---------------------------------------------------------------------------


class TestAlignToWindow:
    """Tests for align_to_window(unix_ts)."""

    def test_exact_boundary(self) -> None:
        """A timestamp already on a boundary should stay unchanged."""
        ts = 900 * 100  # 90_000
        assert align_to_window(ts) == ts

    def test_rounds_down(self) -> None:
        """Timestamp mid-window should round down to the start."""
        ts = 900 * 100 + 450  # 450 seconds into the window
        assert align_to_window(ts) == 900 * 100

    def test_one_second_before_boundary(self) -> None:
        """One second before the next boundary still belongs to current window."""
        ts = 900 * 101 - 1  # 90_899
        assert align_to_window(ts) == 900 * 100

    def test_returns_int(self) -> None:
        """Result should always be an int."""
        assert isinstance(align_to_window(12345.6789), int)

    def test_zero(self) -> None:
        """Unix epoch should align to 0."""
        assert align_to_window(0.0) == 0

    def test_large_timestamp(self) -> None:
        """Sanity check with a realistic Unix timestamp (2024-01-01 00:00 UTC)."""
        ts = 1704067200.0  # 2024-01-01T00:00:00Z
        aligned = align_to_window(ts)
        assert aligned == int(ts)  # already on a boundary (divisible by 900)
        assert aligned % WINDOW_SECONDS == 0


# ---------------------------------------------------------------------------
# compute_slug
# ---------------------------------------------------------------------------


class TestComputeSlug:
    """Tests for compute_slug(asset, unix_ts)."""

    def test_format(self) -> None:
        """Slug must follow the expected pattern."""
        slug = compute_slug("BTC", 90_450.0)
        assert slug == "btc-updown-15m-90000"

    def test_lowercase_asset(self) -> None:
        """Asset portion must be lowercased."""
        slug = compute_slug("ETH", 0.0)
        assert slug.startswith("eth-")

    def test_aligned_timestamp_in_slug(self) -> None:
        """The timestamp in the slug should be window-aligned."""
        slug = compute_slug("btc", 90_899.0)
        # 90_899 aligns down to 90_000
        assert slug.endswith("-90000")

    def test_boundary_timestamp(self) -> None:
        """Exact boundary timestamp appears directly."""
        slug = compute_slug("btc", 1800.0)
        assert slug == "btc-updown-15m-1800"


# ---------------------------------------------------------------------------
# time_remaining_seconds
# ---------------------------------------------------------------------------


class TestTimeRemainingSeconds:
    """Tests for time_remaining_seconds(market_end_ts)."""

    def test_future_end(self) -> None:
        """Positive remaining time when market end is in the future."""
        future = time.time() + 300.0
        remaining = time_remaining_seconds(future)
        assert 299.0 < remaining <= 300.5  # allow small timing drift

    def test_past_end(self) -> None:
        """Negative remaining time when market has already ended."""
        past = time.time() - 100.0
        remaining = time_remaining_seconds(past)
        assert remaining < 0.0


# ---------------------------------------------------------------------------
# is_in_dead_zone
# ---------------------------------------------------------------------------


class TestIsInDeadZone:
    """Tests for is_in_dead_zone(market_start_ts, market_end_ts, ...)."""

    def test_near_start_is_dead(self) -> None:
        """Within start_buffer of start should be a dead zone."""
        now = time.time()
        start = now - 30.0  # started 30 s ago, buffer is 60 s
        end = now + 870.0
        assert is_in_dead_zone(start, end, start_buffer=60.0, end_buffer=30.0) is True

    def test_near_end_is_dead(self) -> None:
        """Within end_buffer of end should be a dead zone."""
        now = time.time()
        start = now - 850.0
        end = now + 20.0  # ends in 20 s, buffer is 30 s
        assert is_in_dead_zone(start, end, start_buffer=60.0, end_buffer=30.0) is True

    def test_middle_is_safe(self) -> None:
        """Well inside the window should not be a dead zone."""
        now = time.time()
        start = now - 300.0  # started 5 min ago
        end = now + 300.0    # ends in 5 min
        assert is_in_dead_zone(start, end, start_buffer=60.0, end_buffer=30.0) is False

    def test_custom_buffers(self) -> None:
        """Custom buffer values should be respected."""
        now = time.time()
        start = now - 5.0  # started 5 s ago
        end = now + 895.0
        # With a 3-second start buffer, 5 s after start is NOT dead.
        assert is_in_dead_zone(start, end, start_buffer=3.0, end_buffer=30.0) is False
        # With a 10-second start buffer, 5 s after start IS dead.
        assert is_in_dead_zone(start, end, start_buffer=10.0, end_buffer=30.0) is True

    @patch("src.utils.time_utils.time.time", return_value=1000.0)
    def test_with_mocked_time(self, mock_time) -> None:
        """Deterministic test using mocked time."""
        # now = 1000, start = 950, end = 1850
        # 1000 < 950 + 60 = 1010 => near start => dead zone
        assert is_in_dead_zone(950.0, 1850.0) is True
        # now = 1000, start = 100, end = 1025
        # 1000 > 1025 - 30 = 995 => near end => dead zone
        assert is_in_dead_zone(100.0, 1025.0) is True
        # now = 1000, start = 100, end = 1500
        # 1000 >= 100 + 60 and 1000 <= 1500 - 30 => safe
        assert is_in_dead_zone(100.0, 1500.0) is False


# ---------------------------------------------------------------------------
# current_window_timestamps / next_window_timestamps
# ---------------------------------------------------------------------------


class TestWindowTimestamps:
    """Tests for current_window_timestamps and next_window_timestamps."""

    def test_current_window_valid_range(self) -> None:
        """(start, end) should span exactly WINDOW_SECONDS."""
        start, end = current_window_timestamps()
        assert end - start == WINDOW_SECONDS

    def test_current_window_contains_now(self) -> None:
        """Current time should fall within [start, end)."""
        start, end = current_window_timestamps()
        now = time.time()
        assert start <= now < end

    def test_current_window_aligned(self) -> None:
        """Start must be divisible by WINDOW_SECONDS."""
        start, _ = current_window_timestamps()
        assert start % WINDOW_SECONDS == 0

    def test_next_window_is_900_after_current(self) -> None:
        """Next window should start exactly WINDOW_SECONDS after current."""
        c_start, c_end = current_window_timestamps()
        n_start, n_end = next_window_timestamps()
        assert n_start == c_start + WINDOW_SECONDS
        assert n_end == c_end + WINDOW_SECONDS

    def test_next_window_valid_range(self) -> None:
        """Next window should also span exactly WINDOW_SECONDS."""
        start, end = next_window_timestamps()
        assert end - start == WINDOW_SECONDS

    def test_next_window_aligned(self) -> None:
        """Next window start must be divisible by WINDOW_SECONDS."""
        start, _ = next_window_timestamps()
        assert start % WINDOW_SECONDS == 0

    @patch("src.utils.time_utils.time.time", return_value=90_450.0)
    def test_deterministic_current_window(self, mock_time) -> None:
        """With mocked time at 90450, current window is [90000, 90900)."""
        start, end = current_window_timestamps()
        assert start == 90_000
        assert end == 90_900

    @patch("src.utils.time_utils.time.time", return_value=90_450.0)
    def test_deterministic_next_window(self, mock_time) -> None:
        """With mocked time at 90450, next window is [90900, 91800)."""
        start, end = next_window_timestamps()
        assert start == 90_900
        assert end == 91_800
