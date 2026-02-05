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
