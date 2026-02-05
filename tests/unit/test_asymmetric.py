"""Comprehensive tests for AsymmetricStrategy and AccumulationState."""

from __future__ import annotations

import os

# Set the required environment variable BEFORE any Settings import
os.environ.setdefault("BOT_PRIVATE_KEY", "0x" + "ab" * 32)

import time
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from src.config import Settings
from src.core.models import (
    FillEstimate,
    Market,
    Opportunity,
    OrderBook,
    OrderBookLevel,
    Position,
    Side,
    StrategyType,
)
from src.strategy.asymmetric import AccumulationState, AsymmetricStrategy


# ---------------------------------------------------------------------------
# Mock OrderBookManager
# ---------------------------------------------------------------------------


class MockOrderBookManager:
    """Controllable mock for OrderBookManager.

    Set ``yes_book``, ``no_book`` to control ``get_book()`` results.
    Set ``fill_estimate`` to control ``get_fill_estimate()`` result.
    """

    def __init__(self) -> None:
        self.yes_book: OrderBook | None = None
        self.no_book: OrderBook | None = None
        self.fill_estimate: FillEstimate | None = None

    def get_book(self, token_id: str) -> OrderBook | None:
        if token_id == "YES_TOKEN":
            return self.yes_book
        elif token_id == "NO_TOKEN":
            return self.no_book
        return None

    def get_fill_estimate(
        self, token_id: str, side: Side, size: float
    ) -> FillEstimate | None:
        return self.fill_estimate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_market(
    asset: str = "BTC",
    yes_token: str = "YES_TOKEN",
    no_token: str = "NO_TOKEN",
    slug: str = "btc-updown-15m-test",
    condition_id: str = "cond_123",
    start_offset: float = -300.0,
    end_offset: float = 600.0,
) -> Market:
    """Create a Market with configurable start/end times relative to now."""
    now = time.time()
    return Market(
        condition_id=condition_id,
        slug=slug,
        question="Will BTC go up?",
        yes_token_id=yes_token,
        no_token_id=no_token,
        start_time=datetime.fromtimestamp(now + start_offset, tz=timezone.utc),
        end_time=datetime.fromtimestamp(now + end_offset, tz=timezone.utc),
        asset=asset,
        neg_risk=True,
    )


def _make_orderbook(
    token_id: str,
    best_bid: float = 0.35,
    best_ask: float = 0.38,
    bid_size: float = 500.0,
    ask_size: float = 500.0,
) -> OrderBook:
    """Create an OrderBook with a single level on each side."""
    return OrderBook(
        token_id=token_id,
        bids=[OrderBookLevel(price=best_bid, size=bid_size)],
        asks=[OrderBookLevel(price=best_ask, size=ask_size)],
        timestamp_ms=int(time.time() * 1000),
        hash="test_hash",
    )


def _make_fill(
    vwap: float = 0.38,
    size: float = 10.0,
    sufficient: bool = True,
) -> FillEstimate:
    return FillEstimate(
        filled_size=size,
        total_cost=vwap * size,
        vwap=vwap,
        worst_price=vwap + 0.01,
        best_price=vwap - 0.01,
        levels_consumed=2,
        sufficient_liquidity=sufficient,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        private_key="0x" + "ab" * 32,  # type: ignore[arg-type]
        yes_cheap_threshold=0.42,
        no_cheap_threshold=0.42,
        accumulation_size=10.0,
        max_accumulation_per_side=200.0,
        target_avg_combined=0.90,
        stale_order_seconds=120.0,
        time_exit_seconds=60.0,
    )


@pytest.fixture()
def book_manager() -> MockOrderBookManager:
    return MockOrderBookManager()


@pytest.fixture()
def strategy(
    settings: Settings,
    book_manager: MockOrderBookManager,
) -> AsymmetricStrategy:
    return AsymmetricStrategy(
        settings=settings,
        book_manager=book_manager,  # type: ignore[arg-type]
    )


@pytest.fixture()
def market() -> Market:
    """Market with start 5min ago, end 10min from now -- well within trading window."""
    return _make_market(start_offset=-300.0, end_offset=600.0)


# ===========================================================================
# AccumulationState tests (unit test the data structure)
# ===========================================================================


class TestAccumulationStateInitial:
    """1. Initial state: all zeros, not completed."""

    def test_initial_state(self) -> None:
        acc = AccumulationState(condition_id="test_cond")
        assert acc.yes_shares == 0.0
        assert acc.no_shares == 0.0
        assert acc.yes_total_cost == 0.0
        assert acc.no_total_cost == 0.0
        assert acc.completed is False


class TestAccumulationStateYesAvgCost:
    """2-3. yes_avg_cost with 0 shares returns 0 and calculation correct."""

    def test_yes_avg_cost_zero_shares(self) -> None:
        acc = AccumulationState(condition_id="test_cond")
        assert acc.yes_avg_cost == 0.0

    def test_yes_avg_cost_calculation(self) -> None:
        acc = AccumulationState(condition_id="test_cond", yes_shares=10.0, yes_total_cost=4.0)
        assert acc.yes_avg_cost == pytest.approx(0.4)

    def test_yes_avg_cost_multiple_buys(self) -> None:
        acc = AccumulationState(condition_id="test_cond", yes_shares=30.0, yes_total_cost=10.5)
        assert acc.yes_avg_cost == pytest.approx(0.35)


class TestAccumulationStateNoAvgCost:
    """4. no_avg_cost calculation correct."""

    def test_no_avg_cost_zero_shares(self) -> None:
        acc = AccumulationState(condition_id="test_cond")
        assert acc.no_avg_cost == 0.0

    def test_no_avg_cost_calculation(self) -> None:
        acc = AccumulationState(condition_id="test_cond", no_shares=20.0, no_total_cost=7.0)
        assert acc.no_avg_cost == pytest.approx(0.35)


class TestAccumulationStateCombinedAvgCost:
    """5. combined_avg_cost correct."""

    def test_combined_avg_cost(self) -> None:
        acc = AccumulationState(
            condition_id="test_cond",
            yes_shares=10.0,
            yes_total_cost=4.0,
            no_shares=10.0,
            no_total_cost=4.5,
        )
        # yes_avg = 0.40, no_avg = 0.45, combined = 0.85
        assert acc.combined_avg_cost == pytest.approx(0.85)

    def test_combined_avg_cost_one_side_empty(self) -> None:
        acc = AccumulationState(
            condition_id="test_cond", yes_shares=10.0, yes_total_cost=4.0
        )
        # yes_avg = 0.40, no_avg = 0.0, combined = 0.40
        assert acc.combined_avg_cost == pytest.approx(0.40)


class TestAccumulationStateMinShares:
    """6. min_shares returns min of both sides."""

    def test_min_shares_equal(self) -> None:
        acc = AccumulationState(
            condition_id="test_cond", yes_shares=50.0, no_shares=50.0
        )
        assert acc.min_shares == 50.0

    def test_min_shares_yes_fewer(self) -> None:
        acc = AccumulationState(
            condition_id="test_cond", yes_shares=30.0, no_shares=50.0
        )
        assert acc.min_shares == 30.0

    def test_min_shares_no_fewer(self) -> None:
        acc = AccumulationState(
            condition_id="test_cond", yes_shares=50.0, no_shares=20.0
        )
        assert acc.min_shares == 20.0


class TestAccumulationStateCanComplete:
    """7-9. can_complete tests."""

    def test_can_complete_true(self) -> None:
        """7. Both sides present and combined < target."""
        acc = AccumulationState(
            condition_id="test_cond",
            yes_shares=10.0,
            yes_total_cost=4.0,
            no_shares=10.0,
            no_total_cost=4.0,
        )
        # combined = 0.40 + 0.40 = 0.80 < 0.90
        assert acc.can_complete(0.90) is True

    def test_can_complete_false_one_side_empty(self) -> None:
        """8. One side empty returns False."""
        acc = AccumulationState(
            condition_id="test_cond",
            yes_shares=10.0,
            yes_total_cost=4.0,
        )
        assert acc.can_complete(0.90) is False

    def test_can_complete_false_no_side_empty(self) -> None:
        """8b. NO side empty returns False."""
        acc = AccumulationState(
            condition_id="test_cond",
            no_shares=10.0,
            no_total_cost=4.0,
        )
        assert acc.can_complete(0.90) is False

    def test_can_complete_false_combined_above_target(self) -> None:
        """9. Combined >= target returns False."""
        acc = AccumulationState(
            condition_id="test_cond",
            yes_shares=10.0,
            yes_total_cost=5.0,
            no_shares=10.0,
            no_total_cost=5.0,
        )
        # combined = 0.50 + 0.50 = 1.00 >= 0.90
        assert acc.can_complete(0.90) is False

    def test_can_complete_false_combined_exactly_at_target(self) -> None:
        """Boundary: combined == target is not < target, so False."""
        acc = AccumulationState(
            condition_id="test_cond",
            yes_shares=10.0,
            yes_total_cost=4.5,
            no_shares=10.0,
            no_total_cost=4.5,
        )
        # combined = 0.45 + 0.45 = 0.90 == 0.90
        assert acc.can_complete(0.90) is False


class TestAccumulationStateRecordBuy:
    """10-12. record_buy tests."""

    def test_record_buy_yes(self) -> None:
        """10. record_buy YES updates yes side."""
        acc = AccumulationState(condition_id="test_cond")
        acc.record_buy("YES", 10.0, 3.8)
        assert acc.yes_shares == 10.0
        assert acc.yes_total_cost == pytest.approx(3.8)
        assert acc.no_shares == 0.0
        assert acc.no_total_cost == 0.0

    def test_record_buy_no(self) -> None:
        """11. record_buy NO updates no side."""
        acc = AccumulationState(condition_id="test_cond")
        acc.record_buy("NO", 15.0, 5.25)
        assert acc.no_shares == 15.0
        assert acc.no_total_cost == pytest.approx(5.25)
        assert acc.yes_shares == 0.0
        assert acc.yes_total_cost == 0.0

    def test_multiple_buys_running_totals(self) -> None:
        """12. Multiple buys update running totals correctly."""
        acc = AccumulationState(condition_id="test_cond")
        acc.record_buy("YES", 10.0, 3.8)
        acc.record_buy("YES", 10.0, 4.2)
        acc.record_buy("NO", 20.0, 7.0)

        assert acc.yes_shares == 20.0
        assert acc.yes_total_cost == pytest.approx(8.0)
        assert acc.yes_avg_cost == pytest.approx(0.40)
        assert acc.no_shares == 20.0
        assert acc.no_total_cost == pytest.approx(7.0)
        assert acc.no_avg_cost == pytest.approx(0.35)
        assert acc.combined_avg_cost == pytest.approx(0.75)


# ===========================================================================
# AsymmetricStrategy.evaluate tests
# ===========================================================================


class TestEvaluateReturnsNone:
    """Tests where evaluate() must return None."""

    @patch("src.strategy.asymmetric.is_in_dead_zone", return_value=True)
    async def test_returns_none_in_dead_zone(
        self,
        _mock_dz: object,
        strategy: AsymmetricStrategy,
        book_manager: MockOrderBookManager,
        market: Market,
    ) -> None:
        """13. Returns None when in dead zone."""
        book_manager.yes_book = _make_orderbook("YES_TOKEN", best_ask=0.35)
        book_manager.no_book = _make_orderbook("NO_TOKEN", best_ask=0.35)
        book_manager.fill_estimate = _make_fill()

        result = await strategy.evaluate(market)
        assert result is None

    @patch("src.strategy.asymmetric.is_in_dead_zone", return_value=False)
    async def test_returns_none_books_missing(
        self,
        _mock_dz: object,
        strategy: AsymmetricStrategy,
        book_manager: MockOrderBookManager,
        market: Market,
    ) -> None:
        """14. Returns None when books missing."""
        book_manager.yes_book = None
        book_manager.no_book = None

        result = await strategy.evaluate(market)
        assert result is None

    @patch("src.strategy.asymmetric.is_in_dead_zone", return_value=False)
    async def test_returns_none_yes_book_missing(
        self,
        _mock_dz: object,
        strategy: AsymmetricStrategy,
        book_manager: MockOrderBookManager,
        market: Market,
    ) -> None:
        """14b. Returns None when YES book missing."""
        book_manager.yes_book = None
        book_manager.no_book = _make_orderbook("NO_TOKEN", best_ask=0.35)

        result = await strategy.evaluate(market)
        assert result is None

    @patch("src.strategy.asymmetric.is_in_dead_zone", return_value=False)
    async def test_returns_none_no_side_cheap(
        self,
        _mock_dz: object,
        strategy: AsymmetricStrategy,
        book_manager: MockOrderBookManager,
        market: Market,
    ) -> None:
        """15. Returns None when no side is cheap enough."""
        # Both asks are above threshold (0.42)
        book_manager.yes_book = _make_orderbook("YES_TOKEN", best_ask=0.50)
        book_manager.no_book = _make_orderbook("NO_TOKEN", best_ask=0.50)

        result = await strategy.evaluate(market)
        assert result is None

    @patch("src.strategy.asymmetric.is_in_dead_zone", return_value=False)
    async def test_returns_none_when_completed(
        self,
        _mock_dz: object,
        strategy: AsymmetricStrategy,
        book_manager: MockOrderBookManager,
        market: Market,
    ) -> None:
        """19. Returns None when completed."""
        acc = strategy.get_accumulation(market.condition_id)
        acc.completed = True

        book_manager.yes_book = _make_orderbook("YES_TOKEN", best_ask=0.35)
        book_manager.no_book = _make_orderbook("NO_TOKEN", best_ask=0.35)

        result = await strategy.evaluate(market)
        assert result is None

    @patch("src.strategy.asymmetric.is_in_dead_zone", return_value=False)
    async def test_returns_none_yes_max_accumulation_reached(
        self,
        _mock_dz: object,
        strategy: AsymmetricStrategy,
        book_manager: MockOrderBookManager,
        market: Market,
    ) -> None:
        """20. Returns None when max accumulation reached on YES side."""
        acc = strategy.get_accumulation(market.condition_id)
        acc.yes_shares = 200.0  # at max
        acc.yes_total_cost = 76.0

        # YES is cheap, NO is not
        book_manager.yes_book = _make_orderbook("YES_TOKEN", best_ask=0.38)
        book_manager.no_book = _make_orderbook("NO_TOKEN", best_ask=0.50)

        result = await strategy.evaluate(market)
        assert result is None

    @patch("src.strategy.asymmetric.is_in_dead_zone", return_value=False)
    async def test_returns_none_no_max_accumulation_reached(
        self,
        _mock_dz: object,
        strategy: AsymmetricStrategy,
        book_manager: MockOrderBookManager,
        market: Market,
    ) -> None:
        """20b. Returns None when max accumulation reached on NO side."""
        acc = strategy.get_accumulation(market.condition_id)
        acc.no_shares = 200.0  # at max
        acc.no_total_cost = 76.0

        # NO is cheap, YES is not
        book_manager.yes_book = _make_orderbook("YES_TOKEN", best_ask=0.50)
        book_manager.no_book = _make_orderbook("NO_TOKEN", best_ask=0.38)

        result = await strategy.evaluate(market)
        assert result is None

    @patch("src.strategy.asymmetric.is_in_dead_zone", return_value=False)
    async def test_returns_none_fill_estimate_insufficient(
        self,
        _mock_dz: object,
        strategy: AsymmetricStrategy,
        book_manager: MockOrderBookManager,
        market: Market,
    ) -> None:
        """23. Fill estimate insufficient returns None."""
        book_manager.yes_book = _make_orderbook("YES_TOKEN", best_ask=0.38)
        book_manager.no_book = _make_orderbook("NO_TOKEN", best_ask=0.50)
        book_manager.fill_estimate = _make_fill(sufficient=False)

        result = await strategy.evaluate(market)
        assert result is None

    @patch("src.strategy.asymmetric.is_in_dead_zone", return_value=False)
    async def test_returns_none_fill_estimate_none(
        self,
        _mock_dz: object,
        strategy: AsymmetricStrategy,
        book_manager: MockOrderBookManager,
        market: Market,
    ) -> None:
        """23b. Fill estimate is None returns None."""
        book_manager.yes_book = _make_orderbook("YES_TOKEN", best_ask=0.38)
        book_manager.no_book = _make_orderbook("NO_TOKEN", best_ask=0.50)
        book_manager.fill_estimate = None

        result = await strategy.evaluate(market)
        assert result is None


class TestEvaluateOpportunityFound:
    """Tests where evaluate() must return a valid Opportunity."""

    @patch("src.strategy.asymmetric.is_in_dead_zone", return_value=False)
    async def test_opportunity_when_yes_cheap(
        self,
        _mock_dz: object,
        strategy: AsymmetricStrategy,
        book_manager: MockOrderBookManager,
        market: Market,
    ) -> None:
        """16. Returns opportunity when YES is cheap."""
        book_manager.yes_book = _make_orderbook("YES_TOKEN", best_ask=0.38)
        book_manager.no_book = _make_orderbook("NO_TOKEN", best_ask=0.50)
        book_manager.fill_estimate = _make_fill(vwap=0.38, size=10.0)

        result = await strategy.evaluate(market)

        assert result is not None
        assert isinstance(result, Opportunity)
        assert result.strategy == StrategyType.ASYMMETRIC
        assert result.market is market
        assert result.yes_fill is not None
        assert result.no_fill is None
        assert result.metadata["buy_side"] == "YES"
        assert result.metadata["buy_price"] == pytest.approx(0.38)
        assert result.metadata["target_token_id"] == "YES_TOKEN"

    @patch("src.strategy.asymmetric.is_in_dead_zone", return_value=False)
    async def test_opportunity_when_no_cheap(
        self,
        _mock_dz: object,
        strategy: AsymmetricStrategy,
        book_manager: MockOrderBookManager,
        market: Market,
    ) -> None:
        """17. Returns opportunity when NO is cheap."""
        book_manager.yes_book = _make_orderbook("YES_TOKEN", best_ask=0.50)
        book_manager.no_book = _make_orderbook("NO_TOKEN", best_ask=0.38)
        book_manager.fill_estimate = _make_fill(vwap=0.38, size=10.0)

        result = await strategy.evaluate(market)

        assert result is not None
        assert isinstance(result, Opportunity)
        assert result.strategy == StrategyType.ASYMMETRIC
        assert result.yes_fill is None
        assert result.no_fill is not None
        assert result.metadata["buy_side"] == "NO"
        assert result.metadata["buy_price"] == pytest.approx(0.38)
        assert result.metadata["target_token_id"] == "NO_TOKEN"

    @patch("src.strategy.asymmetric.is_in_dead_zone", return_value=False)
    async def test_prefers_side_with_fewer_shares_when_both_cheap(
        self,
        _mock_dz: object,
        strategy: AsymmetricStrategy,
        book_manager: MockOrderBookManager,
        market: Market,
    ) -> None:
        """18. Prefers the side we have fewer shares of when both are cheap."""
        acc = strategy.get_accumulation(market.condition_id)
        acc.yes_shares = 50.0
        acc.yes_total_cost = 24.0  # avg=0.48
        acc.no_shares = 20.0
        acc.no_total_cost = 9.6  # avg=0.48, combined=0.96 > target(0.90)

        # Both cheap
        book_manager.yes_book = _make_orderbook("YES_TOKEN", best_ask=0.38)
        book_manager.no_book = _make_orderbook("NO_TOKEN", best_ask=0.38)
        book_manager.fill_estimate = _make_fill(vwap=0.38, size=10.0)

        result = await strategy.evaluate(market)

        assert result is not None
        # NO has fewer shares (20 < 50), so prefer NO
        assert result.metadata["buy_side"] == "NO"

    @patch("src.strategy.asymmetric.is_in_dead_zone", return_value=False)
    async def test_prefers_yes_when_equal_shares_both_cheap(
        self,
        _mock_dz: object,
        strategy: AsymmetricStrategy,
        book_manager: MockOrderBookManager,
        market: Market,
    ) -> None:
        """18b. When shares are equal and both cheap, prefers YES (<=)."""
        acc = strategy.get_accumulation(market.condition_id)
        acc.yes_shares = 30.0
        acc.yes_total_cost = 14.4  # avg=0.48
        acc.no_shares = 30.0
        acc.no_total_cost = 14.4  # avg=0.48, combined=0.96 > target(0.90)

        book_manager.yes_book = _make_orderbook("YES_TOKEN", best_ask=0.38)
        book_manager.no_book = _make_orderbook("NO_TOKEN", best_ask=0.38)
        book_manager.fill_estimate = _make_fill(vwap=0.38, size=10.0)

        result = await strategy.evaluate(market)

        assert result is not None
        # Equal shares, <= prefers YES
        assert result.metadata["buy_side"] == "YES"

    @patch("src.strategy.asymmetric.is_in_dead_zone", return_value=False)
    async def test_opportunity_metadata_fields(
        self,
        _mock_dz: object,
        strategy: AsymmetricStrategy,
        book_manager: MockOrderBookManager,
        market: Market,
    ) -> None:
        """22. Opportunity metadata has correct fields."""
        book_manager.yes_book = _make_orderbook("YES_TOKEN", best_ask=0.38)
        book_manager.no_book = _make_orderbook("NO_TOKEN", best_ask=0.50)
        book_manager.fill_estimate = _make_fill(vwap=0.38, size=10.0)

        result = await strategy.evaluate(market)

        assert result is not None
        expected_keys = {
            "buy_side",
            "buy_price",
            "target_token_id",
            "accumulation_size",
            "order_type",
            "yes_accumulated",
            "no_accumulated",
            "yes_avg_cost",
            "no_avg_cost",
            "hypothetical_combined",
        }
        assert expected_keys == set(result.metadata.keys())
        assert result.metadata["order_type"] == "GTC"
        assert result.metadata["accumulation_size"] == 10.0

    @patch("src.strategy.asymmetric.is_in_dead_zone", return_value=False)
    async def test_expected_profit_reasonable(
        self,
        _mock_dz: object,
        strategy: AsymmetricStrategy,
        book_manager: MockOrderBookManager,
        market: Market,
    ) -> None:
        """24. Expected profit calculation is reasonable."""
        book_manager.yes_book = _make_orderbook("YES_TOKEN", best_ask=0.38)
        book_manager.no_book = _make_orderbook("NO_TOKEN", best_ask=0.50)
        book_manager.fill_estimate = _make_fill(vwap=0.38, size=10.0)

        result = await strategy.evaluate(market)

        assert result is not None
        # With yes at 0.38 and no side having nothing yet (uses 0.5 default),
        # hypothetical combined = 0.38 + 0.5 = 0.88
        # This is below 1.0 and below target_avg_combined (0.90), so profit should be positive
        assert result.expected_profit >= 0.0
        assert result.expected_profit_pct >= 0.0
        assert result.total_fees >= 0.0
        # The hypothetical combined should match
        assert result.metadata["hypothetical_combined"] == pytest.approx(0.88)


class TestEvaluatePairCompletion:
    """21. Detects pair completion and marks completed."""

    @patch("src.strategy.asymmetric.is_in_dead_zone", return_value=False)
    async def test_pair_completion_detected_when_neither_cheap(
        self,
        _mock_dz: object,
        strategy: AsymmetricStrategy,
        book_manager: MockOrderBookManager,
        market: Market,
    ) -> None:
        """When neither side is cheap but pair can complete, mark completed."""
        acc = strategy.get_accumulation(market.condition_id)
        acc.yes_shares = 50.0
        acc.yes_total_cost = 19.0  # avg 0.38
        acc.no_shares = 50.0
        acc.no_total_cost = 19.0  # avg 0.38

        # Neither cheap (both above threshold)
        book_manager.yes_book = _make_orderbook("YES_TOKEN", best_ask=0.50)
        book_manager.no_book = _make_orderbook("NO_TOKEN", best_ask=0.50)

        result = await strategy.evaluate(market)

        assert result is None
        assert acc.completed is True

    @patch("src.strategy.asymmetric.is_in_dead_zone", return_value=False)
    async def test_pair_completion_detected_before_placing_order(
        self,
        _mock_dz: object,
        strategy: AsymmetricStrategy,
        book_manager: MockOrderBookManager,
        market: Market,
    ) -> None:
        """Even when a side is cheap, if pair already completes, stop accumulating."""
        acc = strategy.get_accumulation(market.condition_id)
        acc.yes_shares = 50.0
        acc.yes_total_cost = 19.0  # avg 0.38
        acc.no_shares = 50.0
        acc.no_total_cost = 19.0  # avg 0.38
        # combined = 0.76 < 0.90 target

        # YES is cheap but pair is already complete
        book_manager.yes_book = _make_orderbook("YES_TOKEN", best_ask=0.38)
        book_manager.no_book = _make_orderbook("NO_TOKEN", best_ask=0.50)
        book_manager.fill_estimate = _make_fill(vwap=0.38, size=10.0)

        result = await strategy.evaluate(market)

        assert result is None
        assert acc.completed is True

    @patch("src.strategy.asymmetric.is_in_dead_zone", return_value=False)
    async def test_pair_not_completed_when_combined_too_high(
        self,
        _mock_dz: object,
        strategy: AsymmetricStrategy,
        book_manager: MockOrderBookManager,
        market: Market,
    ) -> None:
        """If combined avg is above target, pair is not completed."""
        acc = strategy.get_accumulation(market.condition_id)
        acc.yes_shares = 50.0
        acc.yes_total_cost = 24.0  # avg 0.48
        acc.no_shares = 50.0
        acc.no_total_cost = 24.0  # avg 0.48
        # combined = 0.96 > 0.90 target

        # Neither cheap
        book_manager.yes_book = _make_orderbook("YES_TOKEN", best_ask=0.50)
        book_manager.no_book = _make_orderbook("NO_TOKEN", best_ask=0.50)

        result = await strategy.evaluate(market)

        assert result is None
        assert acc.completed is False

    @patch("src.strategy.asymmetric.is_in_dead_zone", return_value=False)
    async def test_max_accumulation_triggers_pair_completion_check(
        self,
        _mock_dz: object,
        strategy: AsymmetricStrategy,
        book_manager: MockOrderBookManager,
        market: Market,
    ) -> None:
        """When max accumulation is reached, pair completion is still checked."""
        acc = strategy.get_accumulation(market.condition_id)
        acc.yes_shares = 200.0
        acc.yes_total_cost = 76.0  # avg 0.38
        acc.no_shares = 50.0
        acc.no_total_cost = 19.0  # avg 0.38
        # combined = 0.76 < 0.90

        # YES is cheap but maxed out
        book_manager.yes_book = _make_orderbook("YES_TOKEN", best_ask=0.38)
        book_manager.no_book = _make_orderbook("NO_TOKEN", best_ask=0.50)

        result = await strategy.evaluate(market)

        assert result is None
        assert acc.completed is True


# ===========================================================================
# should_exit tests
# ===========================================================================


class TestShouldExit:
    """Tests for should_exit method."""

    def test_returns_false_no_accumulation_state(
        self,
        strategy: AsymmetricStrategy,
        market: Market,
    ) -> None:
        """25. Returns False when no accumulation state exists."""
        position = Position(
            market=market,
            yes_shares=50.0,
            yes_cost_basis=19.0,
            strategy=StrategyType.ASYMMETRIC,
        )

        assert strategy.should_exit(position, market) is False

    def test_returns_false_when_completed(
        self,
        strategy: AsymmetricStrategy,
        market: Market,
    ) -> None:
        """26. Returns False when completed (held to resolution)."""
        acc = strategy.get_accumulation(market.condition_id)
        acc.yes_shares = 50.0
        acc.yes_total_cost = 19.0
        acc.no_shares = 50.0
        acc.no_total_cost = 19.0
        acc.completed = True

        position = Position(
            market=market,
            yes_shares=50.0,
            no_shares=50.0,
            yes_cost_basis=19.0,
            no_cost_basis=19.0,
            strategy=StrategyType.ASYMMETRIC,
        )

        assert strategy.should_exit(position, market) is False

    @patch("src.strategy.asymmetric.time_remaining_seconds", return_value=30.0)
    def test_returns_true_unhedged_near_expiry(
        self,
        _mock_time: object,
        strategy: AsymmetricStrategy,
    ) -> None:
        """27. Returns True for unhedged position near expiry."""
        market = _make_market(start_offset=-870.0, end_offset=30.0)
        acc = strategy.get_accumulation(market.condition_id)
        acc.yes_shares = 50.0
        acc.yes_total_cost = 19.0
        # NO side is empty -- unhedged
        acc.completed = False

        position = Position(
            market=market,
            yes_shares=50.0,
            yes_cost_basis=19.0,
            strategy=StrategyType.ASYMMETRIC,
        )

        assert strategy.should_exit(position, market) is True

    @patch("src.strategy.asymmetric.time_remaining_seconds", return_value=300.0)
    def test_returns_false_enough_time_remaining(
        self,
        _mock_time: object,
        strategy: AsymmetricStrategy,
    ) -> None:
        """28. Returns False when enough time remaining."""
        market = _make_market(start_offset=-600.0, end_offset=300.0)
        acc = strategy.get_accumulation(market.condition_id)
        acc.yes_shares = 50.0
        acc.yes_total_cost = 19.0
        acc.completed = False

        position = Position(
            market=market,
            yes_shares=50.0,
            yes_cost_basis=19.0,
            strategy=StrategyType.ASYMMETRIC,
        )

        assert strategy.should_exit(position, market) is False

    @patch("src.strategy.asymmetric.time_remaining_seconds", return_value=30.0)
    def test_returns_false_near_expiry_but_no_shares(
        self,
        _mock_time: object,
        strategy: AsymmetricStrategy,
    ) -> None:
        """Near expiry but 0 shares on both sides returns False."""
        market = _make_market(start_offset=-870.0, end_offset=30.0)
        acc = strategy.get_accumulation(market.condition_id)
        acc.yes_shares = 0.0
        acc.no_shares = 0.0
        acc.completed = False

        position = Position(
            market=market,
            strategy=StrategyType.ASYMMETRIC,
        )

        assert strategy.should_exit(position, market) is False


# ===========================================================================
# record_fill tests
# ===========================================================================


class TestRecordFill:
    """Tests for record_fill method."""

    def test_updates_accumulation_state(
        self,
        strategy: AsymmetricStrategy,
    ) -> None:
        """29. Updates accumulation state correctly."""
        strategy.record_fill("cond_abc", "YES", 10.0, 3.8)

        acc = strategy.get_accumulation("cond_abc")
        assert acc.yes_shares == 10.0
        assert acc.yes_total_cost == pytest.approx(3.8)
        assert acc.no_shares == 0.0

    def test_multiple_fills_accumulate(
        self,
        strategy: AsymmetricStrategy,
    ) -> None:
        """30. Multiple fills accumulate."""
        strategy.record_fill("cond_abc", "YES", 10.0, 3.8)
        strategy.record_fill("cond_abc", "YES", 10.0, 4.0)
        strategy.record_fill("cond_abc", "NO", 20.0, 7.6)

        acc = strategy.get_accumulation("cond_abc")
        assert acc.yes_shares == 20.0
        assert acc.yes_total_cost == pytest.approx(7.8)
        assert acc.no_shares == 20.0
        assert acc.no_total_cost == pytest.approx(7.6)
        assert acc.yes_avg_cost == pytest.approx(0.39)
        assert acc.no_avg_cost == pytest.approx(0.38)


# ===========================================================================
# reset_market tests
# ===========================================================================


class TestResetMarket:
    """Tests for reset_market method."""

    def test_clears_accumulation_state(
        self,
        strategy: AsymmetricStrategy,
    ) -> None:
        """31. Clears accumulation state."""
        acc = strategy.get_accumulation("cond_abc")
        acc.yes_shares = 50.0
        acc.yes_total_cost = 19.0

        strategy.reset_market("cond_abc")

        # After reset, get_accumulation creates a fresh state
        new_acc = strategy.get_accumulation("cond_abc")
        assert new_acc.yes_shares == 0.0
        assert new_acc.no_shares == 0.0
        assert new_acc.completed is False

    def test_reset_nonexistent_market_safe(
        self,
        strategy: AsymmetricStrategy,
    ) -> None:
        """32. Reset nonexistent market is safe (no error)."""
        strategy.reset_market("nonexistent_condition_id")
        # Should not raise


# ===========================================================================
# get_accumulation tests
# ===========================================================================


class TestGetAccumulation:
    """Tests for get_accumulation method."""

    def test_creates_new_state_on_first_access(
        self,
        strategy: AsymmetricStrategy,
    ) -> None:
        """33. Creates new state on first access."""
        acc = strategy.get_accumulation("brand_new_cond")

        assert acc.condition_id == "brand_new_cond"
        assert acc.yes_shares == 0.0
        assert acc.no_shares == 0.0
        assert acc.completed is False

    def test_returns_same_state_on_subsequent_access(
        self,
        strategy: AsymmetricStrategy,
    ) -> None:
        """34. Returns same state on subsequent access."""
        acc1 = strategy.get_accumulation("cond_xyz")
        acc1.yes_shares = 42.0

        acc2 = strategy.get_accumulation("cond_xyz")
        assert acc2 is acc1
        assert acc2.yes_shares == 42.0


# ===========================================================================
# Properties
# ===========================================================================


class TestProperties:
    def test_name(self, strategy: AsymmetricStrategy) -> None:
        assert strategy.name == "asymmetric"

    def test_strategy_type(self, strategy: AsymmetricStrategy) -> None:
        assert strategy.strategy_type == StrategyType.ASYMMETRIC
