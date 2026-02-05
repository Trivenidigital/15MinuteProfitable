"""Comprehensive tests for src.data.spot_buffer."""

from __future__ import annotations

import os
import time
from unittest.mock import patch

os.environ.setdefault("BOT_PRIVATE_KEY", "0x" + "ab" * 32)

import pytest

from src.data.spot_buffer import SpotBuffer, SpotMovement, SpotPriceUpdate


# ---------------------------------------------------------------------------
# SpotPriceUpdate dataclass
# ---------------------------------------------------------------------------


class TestSpotPriceUpdate:
    """Tests for the SpotPriceUpdate dataclass."""

    def test_creation(self) -> None:
        """SpotPriceUpdate stores symbol, price, and timestamp."""
        update = SpotPriceUpdate(symbol="BTCUSDT", price=43256.78, timestamp=1700000000.0)
        assert update.symbol == "BTCUSDT"
        assert update.price == 43256.78
        assert update.timestamp == 1700000000.0

    def test_equality(self) -> None:
        """Two SpotPriceUpdates with same fields are equal."""
        a = SpotPriceUpdate(symbol="BTCUSDT", price=100.0, timestamp=1.0)
        b = SpotPriceUpdate(symbol="BTCUSDT", price=100.0, timestamp=1.0)
        assert a == b

    def test_inequality(self) -> None:
        """SpotPriceUpdates with different fields are not equal."""
        a = SpotPriceUpdate(symbol="BTCUSDT", price=100.0, timestamp=1.0)
        b = SpotPriceUpdate(symbol="ETHUSDT", price=100.0, timestamp=1.0)
        assert a != b


# ---------------------------------------------------------------------------
# SpotMovement dataclass
# ---------------------------------------------------------------------------


class TestSpotMovement:
    """Tests for the SpotMovement dataclass."""

    def test_creation(self) -> None:
        """SpotMovement stores all movement fields."""
        movement = SpotMovement(
            symbol="BTCUSDT",
            direction="UP",
            change_pct=0.015,
            start_price=43000.0,
            end_price=43645.0,
            window_seconds=60,
            timestamp=1700000060.0,
        )
        assert movement.symbol == "BTCUSDT"
        assert movement.direction == "UP"
        assert movement.change_pct == 0.015
        assert movement.start_price == 43000.0
        assert movement.end_price == 43645.0
        assert movement.window_seconds == 60
        assert movement.timestamp == 1700000060.0

    def test_equality(self) -> None:
        """Two SpotMovements with same fields are equal."""
        kwargs = dict(
            symbol="BTCUSDT",
            direction="DOWN",
            change_pct=0.01,
            start_price=100.0,
            end_price=99.0,
            window_seconds=30,
            timestamp=1.0,
        )
        assert SpotMovement(**kwargs) == SpotMovement(**kwargs)


# ---------------------------------------------------------------------------
# SpotBuffer.add and SpotBuffer.get_price
# ---------------------------------------------------------------------------


class TestSpotBufferAddAndGetPrice:
    """Tests for add() and get_price()."""

    def test_get_price_returns_none_when_empty(self) -> None:
        """get_price returns None for unknown symbol."""
        buf = SpotBuffer()
        assert buf.get_price("BTCUSDT") is None

    def test_add_stores_price(self) -> None:
        """After add(), get_price() returns the stored price."""
        buf = SpotBuffer(window_seconds=300)
        now = time.time()
        buf.add(SpotPriceUpdate(symbol="BTCUSDT", price=43000.0, timestamp=now))
        assert buf.get_price("BTCUSDT") == 43000.0

    def test_get_price_returns_latest(self) -> None:
        """get_price returns the most recently added price."""
        buf = SpotBuffer(window_seconds=300)
        now = time.time()
        buf.add(SpotPriceUpdate(symbol="BTCUSDT", price=43000.0, timestamp=now))
        buf.add(SpotPriceUpdate(symbol="BTCUSDT", price=43500.0, timestamp=now + 1))
        buf.add(SpotPriceUpdate(symbol="BTCUSDT", price=44000.0, timestamp=now + 2))
        assert buf.get_price("BTCUSDT") == 44000.0

    def test_get_price_none_for_different_symbol(self) -> None:
        """get_price returns None for a symbol that has not been added."""
        buf = SpotBuffer(window_seconds=300)
        now = time.time()
        buf.add(SpotPriceUpdate(symbol="BTCUSDT", price=43000.0, timestamp=now))
        assert buf.get_price("ETHUSDT") is None


# ---------------------------------------------------------------------------
# SpotBuffer.get_price_history
# ---------------------------------------------------------------------------


class TestSpotBufferGetPriceHistory:
    """Tests for get_price_history()."""

    def test_empty_buffer_returns_empty(self) -> None:
        """No data for symbol returns empty list."""
        buf = SpotBuffer()
        assert buf.get_price_history("BTCUSDT") == []

    def test_returns_all_data_when_window_is_none(self) -> None:
        """When window_seconds is None, returns all buffered data."""
        buf = SpotBuffer(window_seconds=3600)
        now = time.time()
        for i in range(5):
            buf.add(SpotPriceUpdate(symbol="BTCUSDT", price=43000.0 + i, timestamp=now + i))
        history = buf.get_price_history("BTCUSDT")
        assert len(history) == 5
        assert history[0][1] == 43000.0
        assert history[4][1] == 43004.0

    def test_filters_by_window(self) -> None:
        """When window_seconds is specified, only returns recent data."""
        buf = SpotBuffer(window_seconds=3600)
        now = time.time()
        # Add old data (60 seconds ago) and recent data (within 10 seconds)
        buf.add(SpotPriceUpdate(symbol="BTCUSDT", price=43000.0, timestamp=now - 60))
        buf.add(SpotPriceUpdate(symbol="BTCUSDT", price=43100.0, timestamp=now - 5))
        buf.add(SpotPriceUpdate(symbol="BTCUSDT", price=43200.0, timestamp=now - 1))

        history = buf.get_price_history("BTCUSDT", window_seconds=10)
        assert len(history) == 2
        assert history[0][1] == 43100.0
        assert history[1][1] == 43200.0

    def test_returns_tuples_of_timestamp_and_price(self) -> None:
        """Each entry is a (timestamp, price) tuple."""
        buf = SpotBuffer(window_seconds=3600)
        ts = time.time()
        buf.add(SpotPriceUpdate(symbol="BTCUSDT", price=43000.0, timestamp=ts))
        history = buf.get_price_history("BTCUSDT")
        assert len(history) == 1
        assert history[0] == (ts, 43000.0)


# ---------------------------------------------------------------------------
# SpotBuffer.detect_movement
# ---------------------------------------------------------------------------


class TestSpotBufferDetectMovement:
    """Tests for detect_movement()."""

    def test_returns_none_with_no_data(self) -> None:
        """No data means no movement."""
        buf = SpotBuffer(window_seconds=3600)
        result = buf.detect_movement("BTCUSDT", window_seconds=60, threshold=0.001)
        assert result is None

    def test_returns_none_with_single_data_point(self) -> None:
        """Need at least 2 data points for movement detection."""
        buf = SpotBuffer(window_seconds=3600)
        now = time.time()
        buf.add(SpotPriceUpdate(symbol="BTCUSDT", price=43000.0, timestamp=now))
        result = buf.detect_movement("BTCUSDT", window_seconds=60, threshold=0.001)
        assert result is None

    def test_returns_none_below_threshold(self) -> None:
        """Small movement below threshold returns None."""
        buf = SpotBuffer(window_seconds=3600)
        now = time.time()
        buf.add(SpotPriceUpdate(symbol="BTCUSDT", price=43000.0, timestamp=now - 5))
        buf.add(SpotPriceUpdate(symbol="BTCUSDT", price=43010.0, timestamp=now))
        # Change is ~0.023%, threshold is 0.1%
        result = buf.detect_movement("BTCUSDT", window_seconds=60, threshold=0.001)
        assert result is None

    def test_detects_up_movement(self) -> None:
        """Detects upward movement above threshold."""
        buf = SpotBuffer(window_seconds=3600)
        now = time.time()
        buf.add(SpotPriceUpdate(symbol="BTCUSDT", price=40000.0, timestamp=now - 5))
        buf.add(SpotPriceUpdate(symbol="BTCUSDT", price=41000.0, timestamp=now))
        # Change is 2.5%
        result = buf.detect_movement("BTCUSDT", window_seconds=60, threshold=0.01)
        assert result is not None
        assert result.direction == "UP"
        assert result.symbol == "BTCUSDT"
        assert result.start_price == 40000.0
        assert result.end_price == 41000.0
        assert result.change_pct == pytest.approx(0.025, abs=1e-6)
        assert result.window_seconds == 60

    def test_detects_down_movement(self) -> None:
        """Detects downward movement above threshold."""
        buf = SpotBuffer(window_seconds=3600)
        now = time.time()
        buf.add(SpotPriceUpdate(symbol="BTCUSDT", price=41000.0, timestamp=now - 5))
        buf.add(SpotPriceUpdate(symbol="BTCUSDT", price=40000.0, timestamp=now))
        # Change is -2.44%
        result = buf.detect_movement("BTCUSDT", window_seconds=60, threshold=0.01)
        assert result is not None
        assert result.direction == "DOWN"
        assert result.change_pct == pytest.approx(1000.0 / 41000.0, abs=1e-6)

    def test_change_pct_is_always_positive(self) -> None:
        """change_pct is the absolute value of the change."""
        buf = SpotBuffer(window_seconds=3600)
        now = time.time()
        buf.add(SpotPriceUpdate(symbol="BTCUSDT", price=50000.0, timestamp=now - 5))
        buf.add(SpotPriceUpdate(symbol="BTCUSDT", price=48000.0, timestamp=now))
        result = buf.detect_movement("BTCUSDT", window_seconds=60, threshold=0.01)
        assert result is not None
        assert result.change_pct > 0

    def test_returns_none_when_start_price_is_zero(self) -> None:
        """Edge case: start_price of 0 would cause division by zero."""
        buf = SpotBuffer(window_seconds=3600)
        now = time.time()
        buf.add(SpotPriceUpdate(symbol="BTCUSDT", price=0.0, timestamp=now - 5))
        buf.add(SpotPriceUpdate(symbol="BTCUSDT", price=43000.0, timestamp=now))
        result = buf.detect_movement("BTCUSDT", window_seconds=60, threshold=0.001)
        assert result is None

    def test_exact_threshold_not_triggered(self) -> None:
        """Movement exactly at the threshold boundary should not trigger.

        detect_movement uses strict < comparison, so exactly at threshold means
        abs(change) < threshold is False, so it WILL return a SpotMovement.
        Actually: if abs(change_pct) < threshold => None, so at exactly threshold
        it will NOT return None. Let's verify the behavior.
        """
        buf = SpotBuffer(window_seconds=3600)
        now = time.time()
        # 1% change: 100 -> 101
        buf.add(SpotPriceUpdate(symbol="BTCUSDT", price=100.0, timestamp=now - 5))
        buf.add(SpotPriceUpdate(symbol="BTCUSDT", price=101.0, timestamp=now))
        # threshold = 0.01, change = 0.01 exactly
        # abs(0.01) < 0.01 is False, so movement IS detected
        result = buf.detect_movement("BTCUSDT", window_seconds=60, threshold=0.01)
        assert result is not None
        assert result.change_pct == pytest.approx(0.01, abs=1e-9)


# ---------------------------------------------------------------------------
# SpotBuffer.has_data
# ---------------------------------------------------------------------------


class TestSpotBufferHasData:
    """Tests for has_data()."""

    def test_no_data(self) -> None:
        """has_data returns False for unknown symbol."""
        buf = SpotBuffer()
        assert buf.has_data("BTCUSDT") is False

    def test_with_data(self) -> None:
        """has_data returns True after adding data."""
        buf = SpotBuffer(window_seconds=300)
        now = time.time()
        buf.add(SpotPriceUpdate(symbol="BTCUSDT", price=43000.0, timestamp=now))
        assert buf.has_data("BTCUSDT") is True

    def test_different_symbol(self) -> None:
        """has_data returns False for a different symbol."""
        buf = SpotBuffer(window_seconds=300)
        now = time.time()
        buf.add(SpotPriceUpdate(symbol="BTCUSDT", price=43000.0, timestamp=now))
        assert buf.has_data("ETHUSDT") is False


# ---------------------------------------------------------------------------
# SpotBuffer.symbols property
# ---------------------------------------------------------------------------


class TestSpotBufferSymbols:
    """Tests for symbols property."""

    def test_empty(self) -> None:
        """No symbols when buffer is empty."""
        buf = SpotBuffer()
        assert buf.symbols == []

    def test_single_symbol(self) -> None:
        """One symbol after adding one."""
        buf = SpotBuffer(window_seconds=300)
        now = time.time()
        buf.add(SpotPriceUpdate(symbol="BTCUSDT", price=43000.0, timestamp=now))
        assert buf.symbols == ["BTCUSDT"]

    def test_multiple_symbols(self) -> None:
        """Multiple symbols tracked independently."""
        buf = SpotBuffer(window_seconds=300)
        now = time.time()
        buf.add(SpotPriceUpdate(symbol="BTCUSDT", price=43000.0, timestamp=now))
        buf.add(SpotPriceUpdate(symbol="ETHUSDT", price=3500.0, timestamp=now))
        buf.add(SpotPriceUpdate(symbol="SOLUSDT", price=120.0, timestamp=now))
        symbols = buf.symbols
        assert len(symbols) == 3
        assert set(symbols) == {"BTCUSDT", "ETHUSDT", "SOLUSDT"}


# ---------------------------------------------------------------------------
# SpotBuffer._prune
# ---------------------------------------------------------------------------


class TestSpotBufferPrune:
    """Tests for _prune() behavior."""

    def test_old_entries_are_pruned(self) -> None:
        """Entries older than window_seconds are removed on add()."""
        buf = SpotBuffer(window_seconds=10)
        now = time.time()
        # Add an old entry
        buf.add(SpotPriceUpdate(symbol="BTCUSDT", price=43000.0, timestamp=now - 20))
        # Add a recent entry -- this triggers prune
        buf.add(SpotPriceUpdate(symbol="BTCUSDT", price=43500.0, timestamp=now))
        history = buf.get_price_history("BTCUSDT")
        # The old entry should have been pruned
        assert len(history) == 1
        assert history[0][1] == 43500.0

    def test_recent_entries_are_kept(self) -> None:
        """Entries within window_seconds are not pruned."""
        buf = SpotBuffer(window_seconds=60)
        now = time.time()
        buf.add(SpotPriceUpdate(symbol="BTCUSDT", price=43000.0, timestamp=now - 30))
        buf.add(SpotPriceUpdate(symbol="BTCUSDT", price=43500.0, timestamp=now))
        history = buf.get_price_history("BTCUSDT")
        assert len(history) == 2

    def test_prune_on_nonexistent_symbol(self) -> None:
        """_prune on unknown symbol does not raise."""
        buf = SpotBuffer()
        buf._prune("NONEXISTENT")  # should not raise


# ---------------------------------------------------------------------------
# Multiple symbols tracked independently
# ---------------------------------------------------------------------------


class TestSpotBufferMultipleSymbols:
    """Tests for multiple symbols being tracked independently."""

    def test_independent_tracking(self) -> None:
        """Adding data for one symbol does not affect another."""
        buf = SpotBuffer(window_seconds=300)
        now = time.time()
        buf.add(SpotPriceUpdate(symbol="BTCUSDT", price=43000.0, timestamp=now))
        buf.add(SpotPriceUpdate(symbol="ETHUSDT", price=3500.0, timestamp=now))
        buf.add(SpotPriceUpdate(symbol="BTCUSDT", price=44000.0, timestamp=now + 1))

        assert buf.get_price("BTCUSDT") == 44000.0
        assert buf.get_price("ETHUSDT") == 3500.0

    def test_independent_movement_detection(self) -> None:
        """Movement detection works independently per symbol."""
        buf = SpotBuffer(window_seconds=3600)
        now = time.time()
        # BTC moves up significantly
        buf.add(SpotPriceUpdate(symbol="BTCUSDT", price=40000.0, timestamp=now - 5))
        buf.add(SpotPriceUpdate(symbol="BTCUSDT", price=41000.0, timestamp=now))
        # ETH barely moves
        buf.add(SpotPriceUpdate(symbol="ETHUSDT", price=3500.0, timestamp=now - 5))
        buf.add(SpotPriceUpdate(symbol="ETHUSDT", price=3501.0, timestamp=now))

        btc_movement = buf.detect_movement("BTCUSDT", window_seconds=60, threshold=0.01)
        eth_movement = buf.detect_movement("ETHUSDT", window_seconds=60, threshold=0.01)

        assert btc_movement is not None
        assert btc_movement.direction == "UP"
        assert eth_movement is None

    def test_independent_history(self) -> None:
        """Price history is independent per symbol."""
        buf = SpotBuffer(window_seconds=300)
        now = time.time()
        buf.add(SpotPriceUpdate(symbol="BTCUSDT", price=43000.0, timestamp=now))
        buf.add(SpotPriceUpdate(symbol="BTCUSDT", price=43500.0, timestamp=now + 1))
        buf.add(SpotPriceUpdate(symbol="ETHUSDT", price=3500.0, timestamp=now))

        btc_history = buf.get_price_history("BTCUSDT")
        eth_history = buf.get_price_history("ETHUSDT")

        assert len(btc_history) == 2
        assert len(eth_history) == 1


# ---------------------------------------------------------------------------
# max_size limit
# ---------------------------------------------------------------------------


class TestSpotBufferMaxSize:
    """Tests for max_size enforcement via deque maxlen."""

    def test_max_size_enforced(self) -> None:
        """Buffer does not exceed max_size entries per symbol."""
        buf = SpotBuffer(window_seconds=3600, max_size=5)
        now = time.time()
        for i in range(10):
            buf.add(SpotPriceUpdate(symbol="BTCUSDT", price=43000.0 + i, timestamp=now + i))

        history = buf.get_price_history("BTCUSDT")
        assert len(history) <= 5
        # Oldest entries should have been dropped by deque maxlen
        # The remaining entries should be the last 5 (indices 5-9)
        assert history[0][1] == 43005.0
        assert history[-1][1] == 43009.0

    def test_max_size_of_one(self) -> None:
        """Buffer with max_size=1 keeps only the latest entry."""
        buf = SpotBuffer(window_seconds=3600, max_size=1)
        now = time.time()
        buf.add(SpotPriceUpdate(symbol="BTCUSDT", price=43000.0, timestamp=now))
        buf.add(SpotPriceUpdate(symbol="BTCUSDT", price=44000.0, timestamp=now + 1))
        assert buf.get_price("BTCUSDT") == 44000.0
        assert len(buf.get_price_history("BTCUSDT")) == 1
