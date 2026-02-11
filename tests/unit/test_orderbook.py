"""Comprehensive tests for src.data.orderbook."""

from __future__ import annotations

import pytest

from src.data.orderbook import L2BookState, OrderBookManager
from src.core.models import Side


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SAMPLE_BIDS = [
    {"price": "0.55", "size": "100"},
    {"price": "0.54", "size": "200"},
    {"price": "0.53", "size": "150"},
]

SAMPLE_ASKS = [
    {"price": "0.56", "size": "80"},
    {"price": "0.57", "size": "120"},
    {"price": "0.58", "size": "300"},
]

TOKEN_ID = "token_abc_123"


# ---------------------------------------------------------------------------
# L2BookState
# ---------------------------------------------------------------------------


class TestL2BookState:
    """Tests for the single-token L2 book state."""

    def test_apply_snapshot(self) -> None:
        """A snapshot with 3 bid and 3 ask levels should produce a correctly
        sorted OrderBook."""
        book = L2BookState(TOKEN_ID)
        book.apply_snapshot(SAMPLE_BIDS, SAMPLE_ASKS)

        ob = book.to_orderbook()

        # Bids sorted descending
        assert len(ob.bids) == 3
        assert ob.bids[0].price == 0.55
        assert ob.bids[1].price == 0.54
        assert ob.bids[2].price == 0.53
        assert ob.bids[0].size == 100.0
        assert ob.bids[1].size == 200.0
        assert ob.bids[2].size == 150.0

        # Asks sorted ascending
        assert len(ob.asks) == 3
        assert ob.asks[0].price == 0.56
        assert ob.asks[1].price == 0.57
        assert ob.asks[2].price == 0.58
        assert ob.asks[0].size == 80.0
        assert ob.asks[1].size == 120.0
        assert ob.asks[2].size == 300.0

    def test_apply_snapshot_clears_previous(self) -> None:
        """Applying a second snapshot completely replaces the first."""
        book = L2BookState(TOKEN_ID)
        book.apply_snapshot(SAMPLE_BIDS, SAMPLE_ASKS)

        new_bids = [{"price": "0.60", "size": "50"}]
        new_asks = [{"price": "0.65", "size": "75"}]
        book.apply_snapshot(new_bids, new_asks)

        ob = book.to_orderbook()
        assert len(ob.bids) == 1
        assert len(ob.asks) == 1
        assert ob.bids[0].price == 0.60
        assert ob.bids[0].size == 50.0
        assert ob.asks[0].price == 0.65
        assert ob.asks[0].size == 75.0

    def test_apply_delta_add_level(self) -> None:
        """A delta with a new price level adds it to the book."""
        book = L2BookState(TOKEN_ID)
        book.apply_snapshot(SAMPLE_BIDS, SAMPLE_ASKS)

        book.apply_delta([
            {"asset_id": TOKEN_ID, "price": "0.52", "size": "50", "side": "BUY"},
        ])

        ob = book.to_orderbook()
        assert len(ob.bids) == 4
        # New level should be last (lowest bid)
        assert ob.bids[-1].price == 0.52
        assert ob.bids[-1].size == 50.0

    def test_apply_delta_update_level(self) -> None:
        """A delta at an existing price level updates its size."""
        book = L2BookState(TOKEN_ID)
        book.apply_snapshot(SAMPLE_BIDS, SAMPLE_ASKS)

        book.apply_delta([
            {"asset_id": TOKEN_ID, "price": "0.55", "size": "999", "side": "BUY"},
        ])

        ob = book.to_orderbook()
        assert ob.bids[0].price == 0.55
        assert ob.bids[0].size == 999.0

    def test_apply_delta_remove_level(self) -> None:
        """A delta with size=0 removes the level from the book."""
        book = L2BookState(TOKEN_ID)
        book.apply_snapshot(SAMPLE_BIDS, SAMPLE_ASKS)

        book.apply_delta([
            {"asset_id": TOKEN_ID, "price": "0.54", "size": "0", "side": "BUY"},
        ])

        ob = book.to_orderbook()
        assert len(ob.bids) == 2
        prices = [lvl.price for lvl in ob.bids]
        assert 0.54 not in prices

    def test_apply_snapshot_string_values(self) -> None:
        """Polymarket sends prices and sizes as strings; verify conversion."""
        book = L2BookState(TOKEN_ID)
        book.apply_snapshot(
            bids=[{"price": "0.42", "size": "300"}],
            asks=[{"price": "0.58", "size": "250"}],
        )

        ob = book.to_orderbook()
        assert ob.bids[0].price == pytest.approx(0.42)
        assert ob.bids[0].size == pytest.approx(300.0)
        assert ob.asks[0].price == pytest.approx(0.58)
        assert ob.asks[0].size == pytest.approx(250.0)

    def test_compute_fill_buy_sufficient(self) -> None:
        """Buying when there is enough ask liquidity returns sufficient=True."""
        book = L2BookState(TOKEN_ID)
        book.apply_snapshot(SAMPLE_BIDS, SAMPLE_ASKS)

        # Total ask liquidity = 80 + 120 + 300 = 500
        fill = book.compute_fill(Side.BUY, 50.0)
        assert fill.sufficient_liquidity is True
        assert fill.filled_size == pytest.approx(50.0)
        # All 50 shares filled at 0.56 (cheapest ask)
        assert fill.vwap == pytest.approx(0.56)
        assert fill.total_cost == pytest.approx(50.0 * 0.56)
        assert fill.best_price == pytest.approx(0.56)
        assert fill.worst_price == pytest.approx(0.56)
        assert fill.levels_consumed == 1

    def test_compute_fill_buy_insufficient(self) -> None:
        """Buying more than total ask depth returns sufficient=False."""
        book = L2BookState(TOKEN_ID)
        book.apply_snapshot(SAMPLE_BIDS, SAMPLE_ASKS)

        # Total ask liquidity = 80 + 120 + 300 = 500
        fill = book.compute_fill(Side.BUY, 600.0)
        assert fill.sufficient_liquidity is False
        assert fill.filled_size == pytest.approx(500.0)
        assert fill.levels_consumed == 3

    def test_compute_fill_buy_walks_multiple_levels(self) -> None:
        """Verify correct VWAP when walking multiple ask levels."""
        book = L2BookState(TOKEN_ID)
        book.apply_snapshot(SAMPLE_BIDS, SAMPLE_ASKS)

        # Want 150 shares.  Asks: 80 @ 0.56, 120 @ 0.57, 300 @ 0.58
        # Fill: 80 @ 0.56  +  70 @ 0.57  = 150 total
        fill = book.compute_fill(Side.BUY, 150.0)

        expected_cost = 80.0 * 0.56 + 70.0 * 0.57
        expected_vwap = expected_cost / 150.0

        assert fill.sufficient_liquidity is True
        assert fill.filled_size == pytest.approx(150.0)
        assert fill.total_cost == pytest.approx(expected_cost)
        assert fill.vwap == pytest.approx(expected_vwap)
        assert fill.best_price == pytest.approx(0.56)
        assert fill.worst_price == pytest.approx(0.57)
        assert fill.levels_consumed == 2

    def test_compute_fill_sell(self) -> None:
        """Selling walks the bids from highest to lowest."""
        book = L2BookState(TOKEN_ID)
        book.apply_snapshot(SAMPLE_BIDS, SAMPLE_ASKS)

        # Want to sell 250 shares.  Bids: 100 @ 0.55, 200 @ 0.54, 150 @ 0.53
        # Fill: 100 @ 0.55  +  150 @ 0.54  = 250 total
        fill = book.compute_fill(Side.SELL, 250.0)

        expected_cost = 100.0 * 0.55 + 150.0 * 0.54
        expected_vwap = expected_cost / 250.0

        assert fill.sufficient_liquidity is True
        assert fill.filled_size == pytest.approx(250.0)
        assert fill.total_cost == pytest.approx(expected_cost)
        assert fill.vwap == pytest.approx(expected_vwap)
        assert fill.best_price == pytest.approx(0.55)
        assert fill.worst_price == pytest.approx(0.54)
        assert fill.levels_consumed == 2

    def test_compute_fill_empty_book(self) -> None:
        """An empty book returns insufficient liquidity with zero fill."""
        book = L2BookState(TOKEN_ID)

        fill = book.compute_fill(Side.BUY, 100.0)
        assert fill.sufficient_liquidity is False
        assert fill.filled_size == 0.0
        assert fill.total_cost == 0.0
        assert fill.vwap == 0.0
        assert fill.levels_consumed == 0


# ---------------------------------------------------------------------------
# OrderBookManager
# ---------------------------------------------------------------------------


class TestOrderBookManager:
    """Tests for the multi-token OrderBookManager."""

    def test_ensure_book_creates_new(self) -> None:
        """ensure_book creates a fresh L2BookState for a new token."""
        mgr = OrderBookManager()
        state = mgr.ensure_book(TOKEN_ID)

        assert isinstance(state, L2BookState)
        assert state.token_id == TOKEN_ID

    def test_ensure_book_returns_existing(self) -> None:
        """Calling ensure_book twice returns the same instance."""
        mgr = OrderBookManager()
        first = mgr.ensure_book(TOKEN_ID)
        second = mgr.ensure_book(TOKEN_ID)

        assert first is second

    def test_get_book_returns_none_for_unknown(self) -> None:
        """get_book returns None for a token that has never been tracked."""
        mgr = OrderBookManager()
        assert mgr.get_book("unknown_token") is None

    def test_get_book_returns_orderbook(self) -> None:
        """get_book returns an OrderBook after data has been loaded."""
        mgr = OrderBookManager()
        state = mgr.ensure_book(TOKEN_ID)
        state.apply_snapshot(SAMPLE_BIDS, SAMPLE_ASKS)

        ob = mgr.get_book(TOKEN_ID)
        assert ob is not None
        assert ob.token_id == TOKEN_ID
        assert len(ob.bids) == 3
        assert len(ob.asks) == 3

    def test_get_fill_estimate(self) -> None:
        """get_fill_estimate delegates to the underlying book."""
        mgr = OrderBookManager()
        state = mgr.ensure_book(TOKEN_ID)
        state.apply_snapshot(SAMPLE_BIDS, SAMPLE_ASKS)

        estimate = mgr.get_fill_estimate(TOKEN_ID, Side.BUY, 50.0)
        assert estimate is not None
        assert estimate.sufficient_liquidity is True
        assert estimate.filled_size == pytest.approx(50.0)

    def test_get_fill_estimate_returns_none_for_unknown(self) -> None:
        """get_fill_estimate returns None for an untracked token."""
        mgr = OrderBookManager()
        assert mgr.get_fill_estimate("unknown_token", Side.BUY, 10.0) is None


# ---------------------------------------------------------------------------
# Staleness checks
# ---------------------------------------------------------------------------


class TestStaleness:
    """Tests for L2BookState.is_stale and related timestamp updates."""

    def test_book_is_stale_when_never_updated(self) -> None:
        """A newly created L2BookState should be stale (never updated)."""
        book = L2BookState(TOKEN_ID)
        assert book.is_stale() is True

    def test_book_not_stale_after_snapshot(self) -> None:
        """Applying a snapshot should update the last-update timestamp,
        making the book non-stale."""
        book = L2BookState(TOKEN_ID)
        book.apply_snapshot(SAMPLE_BIDS, SAMPLE_ASKS)
        # With default 30s threshold, a just-updated book should not be stale
        assert book.is_stale() is False

    def test_book_not_stale_after_delta(self) -> None:
        """Applying a delta should update the last-update timestamp,
        making the book non-stale."""
        book = L2BookState(TOKEN_ID)
        # Start fresh -- the book is stale
        assert book.is_stale() is True
        # Apply a delta
        book.apply_delta([
            {"asset_id": TOKEN_ID, "price": "0.55", "size": "100", "side": "BUY"},
        ])
        assert book.is_stale() is False


# ---------------------------------------------------------------------------
# remove_book
# ---------------------------------------------------------------------------


class TestRemoveBook:
    """Tests for OrderBookManager.remove_book."""

    def test_remove_book(self) -> None:
        """Adding a book then removing it should make it unavailable via get_book."""
        mgr = OrderBookManager()
        state = mgr.ensure_book(TOKEN_ID)
        state.apply_snapshot(SAMPLE_BIDS, SAMPLE_ASKS)

        # Book exists before removal
        assert mgr.get_book(TOKEN_ID) is not None

        result = mgr.remove_book(TOKEN_ID)
        assert result is True

        # Book should be gone
        assert mgr.get_book(TOKEN_ID) is None

        # Removing a second time should return False
        assert mgr.remove_book(TOKEN_ID) is False


# ---------------------------------------------------------------------------
# Price validation
# ---------------------------------------------------------------------------


class TestPriceValidation:
    """Tests for invalid price/size filtering in apply_snapshot."""

    def test_invalid_price_nan_rejected(self) -> None:
        """apply_snapshot with NaN price should not add that level."""
        book = L2BookState(TOKEN_ID)
        book.apply_snapshot(
            bids=[{"price": "nan", "size": "100"}],
            asks=[{"price": "0.55", "size": "50"}],
        )
        ob = book.to_orderbook()
        # NaN bid should be filtered out
        assert len(ob.bids) == 0
        # Valid ask should remain
        assert len(ob.asks) == 1
        assert ob.asks[0].price == pytest.approx(0.55)

    def test_invalid_price_negative_rejected(self) -> None:
        """apply_snapshot with negative price should not add that level."""
        book = L2BookState(TOKEN_ID)
        book.apply_snapshot(
            bids=[{"price": "-0.10", "size": "100"}],
            asks=[{"price": "0.60", "size": "80"}],
        )
        ob = book.to_orderbook()
        # Negative price bid should be filtered out
        assert len(ob.bids) == 0
        # Valid ask should remain
        assert len(ob.asks) == 1
        assert ob.asks[0].price == pytest.approx(0.60)


# ---------------------------------------------------------------------------
# Cached orderbook invalidation
# ---------------------------------------------------------------------------


class TestCachedOrderbook:
    """Tests for orderbook caching and invalidation on delta."""

    def test_cached_orderbook_invalidated_on_mark_stale(self) -> None:
        """mark_all_stale clears the cached orderbook."""
        book = L2BookState(TOKEN_ID)
        book.apply_snapshot(SAMPLE_BIDS, SAMPLE_ASKS)

        ob1 = book.to_orderbook()
        assert ob1 is book.to_orderbook()  # cached

        # Simulate manager-level stale marking
        book._last_update_epoch = 0.0
        book._cached_orderbook = None

        ob2 = book.to_orderbook()
        assert ob2 is not ob1

    def test_cached_orderbook_invalidated_on_delta(self) -> None:
        """get_book called twice without changes should return the same object;
        after a delta it should return a new object."""
        book = L2BookState(TOKEN_ID)
        book.apply_snapshot(SAMPLE_BIDS, SAMPLE_ASKS)

        ob1 = book.to_orderbook()
        ob2 = book.to_orderbook()
        # Same cached object
        assert ob1 is ob2

        # Apply a delta to invalidate cache
        book.apply_delta([
            {"asset_id": TOKEN_ID, "price": "0.52", "size": "25", "side": "BUY"},
        ])

        ob3 = book.to_orderbook()
        # Should be a new object after delta
        assert ob3 is not ob1
        # Verify the delta is reflected
        assert len(ob3.bids) == 4


# ---------------------------------------------------------------------------
# mark_all_stale
# ---------------------------------------------------------------------------


class TestMarkAllStale:
    """Tests for OrderBookManager.mark_all_stale()."""

    def test_mark_all_stale_makes_books_stale(self) -> None:
        """All books should appear stale after mark_all_stale()."""
        mgr = OrderBookManager()
        s1 = mgr.ensure_book("token_1")
        s2 = mgr.ensure_book("token_2")
        s1.apply_snapshot(SAMPLE_BIDS, SAMPLE_ASKS)
        s2.apply_snapshot(SAMPLE_BIDS, SAMPLE_ASKS)

        assert not mgr.is_stale("token_1")
        assert not mgr.is_stale("token_2")

        count = mgr.mark_all_stale()
        assert count == 2
        assert mgr.is_stale("token_1")
        assert mgr.is_stale("token_2")

    def test_mark_all_stale_empty_manager(self) -> None:
        """mark_all_stale on an empty manager returns 0."""
        mgr = OrderBookManager()
        assert mgr.mark_all_stale() == 0

    def test_books_recover_after_mark_stale(self) -> None:
        """A fresh snapshot after mark_all_stale recovers the book."""
        mgr = OrderBookManager()
        s1 = mgr.ensure_book("token_1")
        s1.apply_snapshot(SAMPLE_BIDS, SAMPLE_ASKS)
        mgr.mark_all_stale()
        assert mgr.is_stale("token_1")

        # Fresh snapshot recovers the book
        s1.apply_snapshot(SAMPLE_BIDS, SAMPLE_ASKS)
        assert not mgr.is_stale("token_1")
