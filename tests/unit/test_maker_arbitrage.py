"""Comprehensive tests for MakerArbitrageStrategy."""

from __future__ import annotations

import os

# Set the required environment variable BEFORE any Settings import
os.environ["BOT_PRIVATE_KEY"] = "0x" + "a" * 64

import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.config import Settings
from src.core.models import (
    Market,
    Opportunity,
    OrderBook,
    OrderBookLevel,
    Position,
    StrategyType,
)
from src.data.orderbook import OrderBookManager
from src.strategy.maker_arbitrage import ArbPair, MakerArbitrageStrategy
from src.utils.fees import net_maker_arb_profit, winner_fee_amount


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_NOW = datetime.now(tz=timezone.utc)
_START = datetime.fromtimestamp(_NOW.timestamp() - 300, tz=timezone.utc)
_END = datetime.fromtimestamp(_NOW.timestamp() + 600, tz=timezone.utc)


def _make_market(
    yes_token: str = "YES_TOKEN",
    no_token: str = "NO_TOKEN",
    slug: str = "btc-updown-15m-test",
    start: datetime | None = None,
    end: datetime | None = None,
) -> Market:
    return Market(
        condition_id="cond_123",
        slug=slug,
        question="Will BTC go up?",
        yes_token_id=yes_token,
        no_token_id=no_token,
        start_time=start or _START,
        end_time=end or _END,
        asset="BTC",
        neg_risk=True,
    )


def _make_orderbook(
    token_id: str,
    best_bid: float | None = None,
    best_ask: float | None = None,
) -> OrderBook:
    bids = [OrderBookLevel(price=best_bid, size=100.0)] if best_bid else []
    asks = [OrderBookLevel(price=best_ask, size=100.0)] if best_ask else []
    return OrderBook(token_id=token_id, bids=bids, asks=asks)


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        private_key="0x" + "a" * 64,  # type: ignore[arg-type]
        order_size=50.0,
        maker_target_pair_cost=0.98,
        maker_price_offset=0.005,
        maker_pair_timeout_seconds=180.0,
        maker_max_pending_pairs=5,
        maker_min_profit_margin=0.005,
    )


@pytest.fixture()
def book_manager() -> MagicMock:
    return MagicMock(spec=OrderBookManager)


@pytest.fixture()
def strategy(settings: Settings, book_manager: MagicMock) -> MakerArbitrageStrategy:
    return MakerArbitrageStrategy(settings=settings, book_manager=book_manager)


@pytest.fixture()
def market() -> Market:
    return _make_market()


# ---------------------------------------------------------------------------
# ArbPair tests
# ---------------------------------------------------------------------------


class TestArbPair:
    def test_pair_creation(self) -> None:
        pair = ArbPair(
            pair_id="abc123",
            condition_id="cond_123",
            yes_price=0.45,
            no_price=0.50,
            size=50.0,
        )
        assert pair.pair_id == "abc123"
        assert pair.condition_id == "cond_123"
        assert pair.yes_price == 0.45
        assert pair.no_price == 0.50
        assert pair.size == 50.0
        assert pair.status == "pending"
        assert not pair.yes_filled
        assert not pair.no_filled

    def test_combined_cost(self) -> None:
        pair = ArbPair(
            pair_id="abc123",
            condition_id="cond_123",
            yes_price=0.45,
            no_price=0.50,
            size=50.0,
        )
        assert pair.combined_cost == pytest.approx(0.95)

    def test_is_complete_false_when_pending(self) -> None:
        pair = ArbPair(
            pair_id="abc123",
            condition_id="cond_123",
            yes_price=0.45,
            no_price=0.50,
            size=50.0,
        )
        assert not pair.is_complete

    def test_is_complete_true_when_both_filled(self) -> None:
        pair = ArbPair(
            pair_id="abc123",
            condition_id="cond_123",
            yes_price=0.45,
            no_price=0.50,
            size=50.0,
            yes_filled=True,
            no_filled=True,
        )
        assert pair.is_complete

    def test_is_partial_false_when_pending(self) -> None:
        pair = ArbPair(
            pair_id="abc123",
            condition_id="cond_123",
        )
        assert not pair.is_partial

    def test_is_partial_true_when_one_filled(self) -> None:
        pair = ArbPair(
            pair_id="abc123",
            condition_id="cond_123",
            yes_filled=True,
            no_filled=False,
        )
        assert pair.is_partial

        pair2 = ArbPair(
            pair_id="abc123",
            condition_id="cond_123",
            yes_filled=False,
            no_filled=True,
        )
        assert pair2.is_partial

    def test_is_partial_false_when_complete(self) -> None:
        pair = ArbPair(
            pair_id="abc123",
            condition_id="cond_123",
            yes_filled=True,
            no_filled=True,
        )
        assert not pair.is_partial

    def test_age_seconds(self) -> None:
        pair = ArbPair(
            pair_id="abc123",
            condition_id="cond_123",
            created_at=time.time() - 60.0,
        )
        assert pair.age_seconds >= 60.0
        assert pair.age_seconds < 65.0


# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------


class TestProperties:
    def test_name(self, strategy: MakerArbitrageStrategy) -> None:
        assert strategy.name == "maker_arbitrage"

    def test_strategy_type(self, strategy: MakerArbitrageStrategy) -> None:
        assert strategy.strategy_type == StrategyType.MAKER_ARBITRAGE


# ---------------------------------------------------------------------------
# evaluate -- no opportunity scenarios
# ---------------------------------------------------------------------------


class TestNoOpportunity:
    """Tests where evaluate() must return None."""

    async def test_no_opportunity_when_books_missing(
        self,
        strategy: MakerArbitrageStrategy,
        book_manager: MagicMock,
        market: Market,
    ) -> None:
        """When the OrderBookManager has no book for either token, return None."""
        book_manager.get_book.return_value = None

        result = await strategy.evaluate(market)

        assert result is None
        assert book_manager.get_book.call_count == 2

    async def test_no_opportunity_when_yes_book_empty(
        self,
        strategy: MakerArbitrageStrategy,
        book_manager: MagicMock,
        market: Market,
    ) -> None:
        """When YES book returns None, return None."""
        book_manager.get_book.side_effect = [
            None,
            _make_orderbook("NO_TOKEN", best_ask=0.45),
        ]

        result = await strategy.evaluate(market)

        assert result is None

    async def test_no_opportunity_when_no_ask(
        self,
        strategy: MakerArbitrageStrategy,
        book_manager: MagicMock,
        market: Market,
    ) -> None:
        """When no ask is available, return None."""
        yes_book = _make_orderbook("YES_TOKEN", best_ask=0.45)
        no_book = _make_orderbook("NO_TOKEN", best_ask=None)

        book_manager.get_book.side_effect = [yes_book, no_book]

        result = await strategy.evaluate(market)

        assert result is None

    @patch("src.strategy.maker_arbitrage.is_in_dead_zone", return_value=False)
    async def test_no_opportunity_when_combined_cost_too_high(
        self,
        _mock_dz: MagicMock,
        strategy: MakerArbitrageStrategy,
        book_manager: MagicMock,
        market: Market,
    ) -> None:
        """Combined cost >= maker_target_pair_cost means no opportunity."""
        # With price offset of 0.005:
        # 0.505 - 0.005 = 0.50, 0.505 - 0.005 = 0.50
        # Combined = 1.00 > 0.98
        yes_book = _make_orderbook("YES_TOKEN", best_ask=0.505)
        no_book = _make_orderbook("NO_TOKEN", best_ask=0.505)

        book_manager.get_book.side_effect = [yes_book, no_book]

        result = await strategy.evaluate(market)

        assert result is None

    @patch("src.strategy.maker_arbitrage.is_in_dead_zone", return_value=False)
    async def test_no_opportunity_when_profit_below_margin(
        self,
        _mock_dz: MagicMock,
        strategy: MakerArbitrageStrategy,
        book_manager: MagicMock,
        market: Market,
    ) -> None:
        """Even if combined cost < target, if net profit per share < min_margin, reject."""
        # Set up a scenario where combined passes but margin doesn't
        strategy._settings = Settings(
            private_key="0x" + "a" * 64,  # type: ignore[arg-type]
            order_size=50.0,
            maker_target_pair_cost=0.98,
            maker_price_offset=0.005,
            maker_min_profit_margin=0.10,  # Require 10% per share -- unreachable
        )

        yes_book = _make_orderbook("YES_TOKEN", best_ask=0.48)
        no_book = _make_orderbook("NO_TOKEN", best_ask=0.48)

        book_manager.get_book.side_effect = [yes_book, no_book]

        result = await strategy.evaluate(market)

        assert result is None

    @patch("src.strategy.maker_arbitrage.is_in_dead_zone", return_value=True)
    async def test_dead_zone_prevents_opportunity(
        self,
        _mock_dz: MagicMock,
        strategy: MakerArbitrageStrategy,
        book_manager: MagicMock,
        market: Market,
    ) -> None:
        """Even with a great spread, dead zone returns None."""
        yes_book = _make_orderbook("YES_TOKEN", best_ask=0.30)
        no_book = _make_orderbook("NO_TOKEN", best_ask=0.30)

        book_manager.get_book.side_effect = [yes_book, no_book]

        result = await strategy.evaluate(market)

        assert result is None

    @patch("src.strategy.maker_arbitrage.is_in_dead_zone", return_value=False)
    async def test_max_pending_pairs_prevents_opportunity(
        self,
        _mock_dz: MagicMock,
        strategy: MakerArbitrageStrategy,
        book_manager: MagicMock,
        market: Market,
    ) -> None:
        """When max pending pairs reached for a market, return None."""
        # Create max_pending_pairs pairs for this market
        for i in range(strategy._settings.maker_max_pending_pairs):
            pair = strategy.create_pair(
                condition_id=market.condition_id,
                yes_price=0.45,
                no_price=0.50,
                size=50.0,
            )
            pair.status = "pending"

        yes_book = _make_orderbook("YES_TOKEN", best_ask=0.30)
        no_book = _make_orderbook("NO_TOKEN", best_ask=0.30)

        book_manager.get_book.side_effect = [yes_book, no_book]

        result = await strategy.evaluate(market)

        assert result is None


# ---------------------------------------------------------------------------
# evaluate -- opportunity found
# ---------------------------------------------------------------------------


class TestOpportunityFound:
    """Tests where evaluate() must return a valid Opportunity."""

    @patch("src.strategy.maker_arbitrage.is_in_dead_zone", return_value=False)
    async def test_opportunity_found_with_wide_spread(
        self,
        _mock_dz: MagicMock,
        strategy: MakerArbitrageStrategy,
        book_manager: MagicMock,
        market: Market,
    ) -> None:
        """yes_ask=0.30, no_ask=0.30 (limit prices ~0.295 each) -- huge profit."""
        yes_book = _make_orderbook("YES_TOKEN", best_ask=0.30)
        no_book = _make_orderbook("NO_TOKEN", best_ask=0.30)

        book_manager.get_book.side_effect = [yes_book, no_book]

        result = await strategy.evaluate(market)

        assert result is not None
        assert isinstance(result, Opportunity)
        assert result.strategy == StrategyType.MAKER_ARBITRAGE
        assert result.market is market
        assert result.expected_profit > 0.0
        assert result.total_fees > 0.0
        assert result.confidence > 0.0
        assert result.metadata["order_type"] == "GTC"
        assert result.metadata["paired"] is True

    @patch("src.strategy.maker_arbitrage.is_in_dead_zone", return_value=False)
    async def test_opportunity_found_with_realistic_spread(
        self,
        _mock_dz: MagicMock,
        strategy: MakerArbitrageStrategy,
        book_manager: MagicMock,
        market: Market,
    ) -> None:
        """yes_ask=0.48, no_ask=0.48 -- realistic scenario for maker arb."""
        yes_book = _make_orderbook("YES_TOKEN", best_ask=0.48)
        no_book = _make_orderbook("NO_TOKEN", best_ask=0.48)

        book_manager.get_book.side_effect = [yes_book, no_book]

        result = await strategy.evaluate(market)

        assert result is not None
        assert isinstance(result, Opportunity)
        # Limit prices: 0.48 - 0.005 = 0.475, rounded to 0.47 (tick size)
        # Combined: 0.94 < 0.98 threshold
        assert result.metadata["yes_price"] == pytest.approx(0.47)
        assert result.metadata["no_price"] == pytest.approx(0.47)
        assert result.metadata["combined_cost"] == pytest.approx(0.94)

    @patch("src.strategy.maker_arbitrage.is_in_dead_zone", return_value=False)
    async def test_price_offset_applied(
        self,
        _mock_dz: MagicMock,
        strategy: MakerArbitrageStrategy,
        book_manager: MagicMock,
        market: Market,
    ) -> None:
        """Verify price offset is correctly applied to limit prices."""
        yes_book = _make_orderbook("YES_TOKEN", best_ask=0.50)
        no_book = _make_orderbook("NO_TOKEN", best_ask=0.45)

        book_manager.get_book.side_effect = [yes_book, no_book]

        result = await strategy.evaluate(market)

        assert result is not None
        # Offset of 0.005 applied and rounded to tick size (0.01)
        # 0.50 - 0.005 = 0.495 -> round to 0.49
        # 0.45 - 0.005 = 0.445 -> round to 0.45 (closest to 0.45)
        assert result.metadata["yes_price"] == pytest.approx(0.49)
        assert result.metadata["no_price"] == pytest.approx(0.45)

    @patch("src.strategy.maker_arbitrage.is_in_dead_zone", return_value=False)
    async def test_price_floor_at_001(
        self,
        _mock_dz: MagicMock,
        strategy: MakerArbitrageStrategy,
        book_manager: MagicMock,
        market: Market,
    ) -> None:
        """Verify limit price doesn't go below 0.01."""
        yes_book = _make_orderbook("YES_TOKEN", best_ask=0.01)
        no_book = _make_orderbook("NO_TOKEN", best_ask=0.50)

        book_manager.get_book.side_effect = [yes_book, no_book]

        result = await strategy.evaluate(market)

        assert result is not None
        # 0.01 - 0.005 would be 0.005, but floor is 0.01
        assert result.metadata["yes_price"] == pytest.approx(0.01)


# ---------------------------------------------------------------------------
# Profit calculation
# ---------------------------------------------------------------------------


class TestProfitCalculation:
    """Verify exact profit/fee numbers match the fees module."""

    @patch("src.strategy.maker_arbitrage.is_in_dead_zone", return_value=False)
    async def test_profit_calculation_accuracy(
        self,
        _mock_dz: MagicMock,
        strategy: MakerArbitrageStrategy,
        book_manager: MagicMock,
        market: Market,
    ) -> None:
        """Verify the returned profit matches hand-calculated values."""
        yes_book = _make_orderbook("YES_TOKEN", best_ask=0.35)
        no_book = _make_orderbook("NO_TOKEN", best_ask=0.35)

        book_manager.get_book.side_effect = [yes_book, no_book]

        result = await strategy.evaluate(market)
        assert result is not None

        # Limit prices after offset and rounding to tick size
        # 0.35 - 0.005 = 0.345 -> round(0.345, 2) = 0.34
        yes_price = 0.34
        no_price = 0.34
        size = 50.0

        # Hand-calculate expected values (no taker fee!)
        expected_gross = (1.0 - yes_price - no_price) * size
        expected_winner = winner_fee_amount(min(yes_price, no_price), 1.0) * size
        expected_net = expected_gross - expected_winner

        assert result.expected_profit == pytest.approx(expected_net, abs=1e-6)
        assert result.total_fees == pytest.approx(expected_winner, abs=1e-6)
        assert result.expected_profit_pct == pytest.approx(expected_net / size, abs=1e-6)


class TestNetMakerArbProfit:
    """Test the net_maker_arb_profit helper function."""

    def test_basic_profit_calculation(self) -> None:
        """Basic maker arb profit (0% maker fee)."""
        yes_price = 0.40
        no_price = 0.40
        size = 100.0

        profit = net_maker_arb_profit(yes_price, no_price, size)

        # Gross: (1 - 0.40 - 0.40) * 100 = $20
        # Winner fee on cheaper leg (both 0.40): 2% of (1 - 0.40) * 100 = $1.20
        gross = 20.0
        winner = winner_fee_amount(0.40, 1.0) * size
        expected = gross - winner

        assert profit == pytest.approx(expected, abs=1e-10)

    def test_no_taker_fee_deducted(self) -> None:
        """Verify no taker fee is deducted for maker orders."""
        from src.utils.fees import net_arb_profit

        yes_price = 0.45
        no_price = 0.45
        size = 50.0

        maker_profit = net_maker_arb_profit(yes_price, no_price, size)
        taker_profit = net_arb_profit(yes_price, no_price, size)

        # Maker profit should be higher (no taker fees)
        assert maker_profit > taker_profit

    def test_asymmetric_prices(self) -> None:
        """Test with asymmetric YES/NO prices."""
        yes_price = 0.30
        no_price = 0.60
        size = 50.0

        profit = net_maker_arb_profit(yes_price, no_price, size)

        # Gross: (1 - 0.30 - 0.60) * 50 = $5
        # Winner fee on cheaper leg (YES at 0.30): 2% of (1 - 0.30) * 50 = $0.70
        gross = 5.0
        winner = winner_fee_amount(0.30, 1.0) * size
        expected = gross - winner

        assert profit == pytest.approx(expected, abs=1e-10)


# ---------------------------------------------------------------------------
# Pair management
# ---------------------------------------------------------------------------


class TestPairManagement:
    def test_create_pair(self, strategy: MakerArbitrageStrategy) -> None:
        pair = strategy.create_pair(
            condition_id="cond_123",
            yes_price=0.45,
            no_price=0.50,
            size=50.0,
        )

        assert pair.condition_id == "cond_123"
        assert pair.yes_price == 0.45
        assert pair.no_price == 0.50
        assert pair.size == 50.0
        assert pair.status == "pending"
        assert len(pair.pair_id) == 12

    def test_get_pair(self, strategy: MakerArbitrageStrategy) -> None:
        pair = strategy.create_pair(
            condition_id="cond_123",
            yes_price=0.45,
            no_price=0.50,
            size=50.0,
        )

        retrieved = strategy.get_pair(pair.pair_id)
        assert retrieved is pair

        assert strategy.get_pair("nonexistent") is None

    def test_get_pending_pairs(self, strategy: MakerArbitrageStrategy) -> None:
        pair1 = strategy.create_pair("cond_1", 0.45, 0.50, 50.0)
        pair2 = strategy.create_pair("cond_2", 0.45, 0.50, 50.0)
        pair3 = strategy.create_pair("cond_3", 0.45, 0.50, 50.0)

        pair2.status = "complete"
        pair3.status = "cancelled"

        pending = strategy.get_pending_pairs()
        assert len(pending) == 1
        assert pending[0] is pair1

    def test_get_pending_pairs_includes_partial(self, strategy: MakerArbitrageStrategy) -> None:
        pair = strategy.create_pair("cond_1", 0.45, 0.50, 50.0)
        pair.status = "partial"

        pending = strategy.get_pending_pairs()
        assert len(pending) == 1
        assert pending[0] is pair

    def test_record_fill_yes(self, strategy: MakerArbitrageStrategy) -> None:
        pair = strategy.create_pair("cond_1", 0.45, 0.50, 50.0)

        complete = strategy.record_fill(pair.pair_id, "YES", 50.0)

        assert not complete
        assert pair.yes_filled
        assert not pair.no_filled
        assert pair.yes_fill_size == 50.0
        assert pair.status == "partial"

    def test_record_fill_no(self, strategy: MakerArbitrageStrategy) -> None:
        pair = strategy.create_pair("cond_1", 0.45, 0.50, 50.0)

        complete = strategy.record_fill(pair.pair_id, "NO", 50.0)

        assert not complete
        assert not pair.yes_filled
        assert pair.no_filled
        assert pair.no_fill_size == 50.0
        assert pair.status == "partial"

    def test_record_fill_completes_pair(self, strategy: MakerArbitrageStrategy) -> None:
        pair = strategy.create_pair("cond_1", 0.45, 0.50, 50.0)

        strategy.record_fill(pair.pair_id, "YES", 50.0)
        complete = strategy.record_fill(pair.pair_id, "NO", 50.0)

        assert complete
        assert pair.is_complete
        assert pair.status == "complete"

    def test_record_fill_unknown_pair(self, strategy: MakerArbitrageStrategy) -> None:
        result = strategy.record_fill("nonexistent", "YES", 50.0)
        assert not result

    def test_cancel_pair(self, strategy: MakerArbitrageStrategy) -> None:
        pair = strategy.create_pair("cond_1", 0.45, 0.50, 50.0)

        strategy.cancel_pair(pair.pair_id)

        assert pair.status == "cancelled"

    def test_cancel_pair_with_status(self, strategy: MakerArbitrageStrategy) -> None:
        pair = strategy.create_pair("cond_1", 0.45, 0.50, 50.0)

        strategy.cancel_pair(pair.pair_id, status="unwound")

        assert pair.status == "unwound"

    def test_remove_pair(self, strategy: MakerArbitrageStrategy) -> None:
        pair = strategy.create_pair("cond_1", 0.45, 0.50, 50.0)
        pair_id = pair.pair_id

        strategy.remove_pair(pair_id)

        assert strategy.get_pair(pair_id) is None

    def test_get_timed_out_pairs(self, strategy: MakerArbitrageStrategy) -> None:
        pair1 = strategy.create_pair("cond_1", 0.45, 0.50, 50.0)
        pair2 = strategy.create_pair("cond_2", 0.45, 0.50, 50.0)

        # Make pair1 appear old
        pair1.created_at = time.time() - 200.0  # 200s > 180s timeout

        timed_out = strategy.get_timed_out_pairs()
        assert len(timed_out) == 1
        assert timed_out[0] is pair1


# ---------------------------------------------------------------------------
# should_exit
# ---------------------------------------------------------------------------


class TestShouldExit:
    def test_should_exit_always_false(
        self,
        strategy: MakerArbitrageStrategy,
        market: Market,
    ) -> None:
        """Maker arb positions are held to resolution, so should_exit returns False."""
        position = Position(
            market=market,
            yes_shares=50.0,
            no_shares=50.0,
            yes_cost_basis=22.5,
            no_cost_basis=22.5,
            strategy=StrategyType.MAKER_ARBITRAGE,
            opened_at=_NOW,
        )

        assert strategy.should_exit(position, market) is False

    def test_should_exit_false_even_with_zero_shares(
        self,
        strategy: MakerArbitrageStrategy,
        market: Market,
    ) -> None:
        """Even degenerate positions always return False for exit."""
        position = Position(
            market=market,
            yes_shares=0.0,
            no_shares=0.0,
            strategy=StrategyType.MAKER_ARBITRAGE,
        )

        assert strategy.should_exit(position, market) is False
