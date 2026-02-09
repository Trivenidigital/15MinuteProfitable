"""Comprehensive tests for ArbitrageStrategy."""

from __future__ import annotations

import os

# Set the required environment variable BEFORE any Settings import
os.environ["BOT_PRIVATE_KEY"] = "0x" + "a" * 64

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.config import Settings
from src.core.models import (
    FillEstimate,
    Market,
    Opportunity,
    Position,
    Side,
    StrategyType,
)
from src.data.orderbook import OrderBookManager
from src.strategy.arbitrage import ArbitrageStrategy
from src.utils.fees import taker_fee_amount, winner_fee_amount


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


def _make_fill(
    vwap: float,
    size: float = 50.0,
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


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        private_key="0x" + "a" * 64,  # type: ignore[arg-type]
        order_size=50.0,
        target_pair_cost=0.94,
        min_profit_margin=0.005,
    )


@pytest.fixture()
def book_manager() -> MagicMock:
    bm = MagicMock(spec=OrderBookManager)
    bm.is_stale.return_value = False  # default: books are fresh
    return bm


@pytest.fixture()
def strategy(settings: Settings, book_manager: MagicMock) -> ArbitrageStrategy:
    return ArbitrageStrategy(settings=settings, book_manager=book_manager)


@pytest.fixture()
def market() -> Market:
    return _make_market()


# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------


class TestProperties:
    def test_name(self, strategy: ArbitrageStrategy) -> None:
        assert strategy.name == "arbitrage"

    def test_strategy_type(self, strategy: ArbitrageStrategy) -> None:
        assert strategy.strategy_type == StrategyType.ARBITRAGE


# ---------------------------------------------------------------------------
# evaluate -- no opportunity scenarios
# ---------------------------------------------------------------------------


class TestNoOpportunity:
    """Tests where evaluate() must return None."""

    async def test_no_opportunity_when_books_empty(
        self,
        strategy: ArbitrageStrategy,
        book_manager: MagicMock,
        market: Market,
    ) -> None:
        """When the OrderBookManager has no book for either token, return None."""
        book_manager.get_fill_estimate.return_value = None

        result = await strategy.evaluate(market)

        assert result is None
        assert book_manager.get_fill_estimate.call_count == 2

    async def test_no_opportunity_when_yes_book_empty(
        self,
        strategy: ArbitrageStrategy,
        book_manager: MagicMock,
        market: Market,
    ) -> None:
        """When only the YES book returns None, return None."""
        book_manager.get_fill_estimate.side_effect = [
            None,
            _make_fill(vwap=0.45),
        ]

        result = await strategy.evaluate(market)

        assert result is None

    async def test_no_opportunity_when_no_book_empty(
        self,
        strategy: ArbitrageStrategy,
        book_manager: MagicMock,
        market: Market,
    ) -> None:
        """When only the NO book returns None, return None."""
        book_manager.get_fill_estimate.side_effect = [
            _make_fill(vwap=0.45),
            None,
        ]

        result = await strategy.evaluate(market)

        assert result is None

    async def test_no_opportunity_when_insufficient_liquidity(
        self,
        strategy: ArbitrageStrategy,
        book_manager: MagicMock,
        market: Market,
    ) -> None:
        """When one or both fills lack sufficient liquidity, return None."""
        book_manager.get_fill_estimate.side_effect = [
            _make_fill(vwap=0.40, sufficient=False),
            _make_fill(vwap=0.40, sufficient=True),
        ]

        result = await strategy.evaluate(market)

        assert result is None

    async def test_no_opportunity_when_both_insufficient_liquidity(
        self,
        strategy: ArbitrageStrategy,
        book_manager: MagicMock,
        market: Market,
    ) -> None:
        """When both fills lack sufficient liquidity, return None."""
        book_manager.get_fill_estimate.side_effect = [
            _make_fill(vwap=0.40, sufficient=False),
            _make_fill(vwap=0.40, sufficient=False),
        ]

        result = await strategy.evaluate(market)

        assert result is None

    @patch("src.strategy.arbitrage.is_in_dead_zone", return_value=False)
    async def test_no_opportunity_when_combined_cost_too_high(
        self,
        _mock_dz: MagicMock,
        strategy: ArbitrageStrategy,
        book_manager: MagicMock,
        market: Market,
    ) -> None:
        """Combined VWAP cost >= target_pair_cost means no opportunity."""
        # 0.48 + 0.48 = 0.96 > 0.94 target
        book_manager.get_fill_estimate.side_effect = [
            _make_fill(vwap=0.48),
            _make_fill(vwap=0.48),
        ]

        result = await strategy.evaluate(market)

        assert result is None

    @patch("src.strategy.arbitrage.is_in_dead_zone", return_value=False)
    async def test_no_opportunity_when_combined_cost_exactly_at_threshold(
        self,
        _mock_dz: MagicMock,
        strategy: ArbitrageStrategy,
        book_manager: MagicMock,
        market: Market,
    ) -> None:
        """Combined cost == target_pair_cost (boundary) means no opportunity."""
        # 0.47 + 0.47 = 0.94 == target (uses >= so rejected)
        book_manager.get_fill_estimate.side_effect = [
            _make_fill(vwap=0.47),
            _make_fill(vwap=0.47),
        ]

        result = await strategy.evaluate(market)

        assert result is None

    @patch("src.strategy.arbitrage.is_in_dead_zone", return_value=False)
    async def test_no_opportunity_when_profit_below_margin(
        self,
        _mock_dz: MagicMock,
        strategy: ArbitrageStrategy,
        book_manager: MagicMock,
        market: Market,
    ) -> None:
        """Even if combined cost < target, if net profit per share < min_margin, reject."""
        # 0.46 + 0.46 = 0.92 < 0.94 target, but fees may eat the profit.
        # With very tight margin, use values that pass the combined cost
        # check but fail the profit margin check.
        # Use 0.465 + 0.465 = 0.93, gross per share = 0.07.
        # Fees will eat most of it.  Let's set min_profit_margin high
        # to ensure rejection.
        strategy._settings = Settings(
            private_key="0x" + "a" * 64,  # type: ignore[arg-type]
            order_size=50.0,
            target_pair_cost=0.94,
            min_profit_margin=0.10,  # Require 10% per share -- unreachable
        )

        book_manager.get_fill_estimate.side_effect = [
            _make_fill(vwap=0.46),
            _make_fill(vwap=0.46),
        ]

        result = await strategy.evaluate(market)

        assert result is None

    @patch("src.strategy.arbitrage.is_in_dead_zone", return_value=True)
    async def test_dead_zone_prevents_opportunity(
        self,
        _mock_dz: MagicMock,
        strategy: ArbitrageStrategy,
        book_manager: MagicMock,
        market: Market,
    ) -> None:
        """Even with a great spread, dead zone returns None."""
        book_manager.get_fill_estimate.side_effect = [
            _make_fill(vwap=0.30),
            _make_fill(vwap=0.30),
        ]

        result = await strategy.evaluate(market)

        assert result is None


# ---------------------------------------------------------------------------
# evaluate -- opportunity found
# ---------------------------------------------------------------------------


class TestOpportunityFound:
    """Tests where evaluate() must return a valid Opportunity."""

    @patch("src.strategy.arbitrage.is_in_dead_zone", return_value=False)
    async def test_opportunity_found_with_wide_spread(
        self,
        _mock_dz: MagicMock,
        strategy: ArbitrageStrategy,
        book_manager: MagicMock,
        market: Market,
    ) -> None:
        """yes=0.30, no=0.30, combined=0.60 -- huge profit."""
        yes_fill = _make_fill(vwap=0.30)
        no_fill = _make_fill(vwap=0.30)
        book_manager.get_fill_estimate.side_effect = [yes_fill, no_fill]

        result = await strategy.evaluate(market)

        assert result is not None
        assert isinstance(result, Opportunity)
        assert result.strategy == StrategyType.ARBITRAGE
        assert result.market is market
        assert result.yes_fill is yes_fill
        assert result.no_fill is no_fill
        assert result.expected_profit > 0.0
        assert result.total_fees > 0.0
        assert result.confidence > 0.0
        assert result.metadata["combined_cost"] == pytest.approx(0.60)

    @patch("src.strategy.arbitrage.is_in_dead_zone", return_value=False)
    async def test_opportunity_found_with_realistic_spread(
        self,
        _mock_dz: MagicMock,
        strategy: ArbitrageStrategy,
        book_manager: MagicMock,
        market: Market,
    ) -> None:
        """yes=0.45, no=0.45, combined=0.90 -- realistic scenario."""
        yes_fill = _make_fill(vwap=0.45)
        no_fill = _make_fill(vwap=0.45)
        book_manager.get_fill_estimate.side_effect = [yes_fill, no_fill]

        result = await strategy.evaluate(market)

        assert result is not None
        assert isinstance(result, Opportunity)
        assert result.expected_profit > 0.0
        assert result.metadata["combined_cost"] == pytest.approx(0.90)
        assert result.metadata["yes_vwap"] == pytest.approx(0.45)
        assert result.metadata["no_vwap"] == pytest.approx(0.45)

    @patch("src.strategy.arbitrage.is_in_dead_zone", return_value=False)
    async def test_opportunity_found_with_asymmetric_prices(
        self,
        _mock_dz: MagicMock,
        strategy: ArbitrageStrategy,
        book_manager: MagicMock,
        market: Market,
    ) -> None:
        """yes=0.35, no=0.50, combined=0.85 -- asymmetric pricing."""
        yes_fill = _make_fill(vwap=0.35)
        no_fill = _make_fill(vwap=0.50)
        book_manager.get_fill_estimate.side_effect = [yes_fill, no_fill]

        result = await strategy.evaluate(market)

        assert result is not None
        assert result.expected_profit > 0.0
        assert result.metadata["combined_cost"] == pytest.approx(0.85)

    @patch("src.strategy.arbitrage.is_in_dead_zone", return_value=False)
    async def test_fill_estimates_passed_correct_args(
        self,
        _mock_dz: MagicMock,
        strategy: ArbitrageStrategy,
        book_manager: MagicMock,
        market: Market,
    ) -> None:
        """Verify the OrderBookManager is called with correct token IDs, side, and size."""
        book_manager.get_fill_estimate.side_effect = [
            _make_fill(vwap=0.30),
            _make_fill(vwap=0.30),
        ]

        await strategy.evaluate(market)

        calls = book_manager.get_fill_estimate.call_args_list
        assert len(calls) == 2

        # First call: YES token, BUY, order_size
        assert calls[0].args == (market.yes_token_id, Side.BUY, 50.0) or \
               calls[0].kwargs == {} and calls[0][0] == (market.yes_token_id, Side.BUY, 50.0)

        # Second call: NO token, BUY, order_size
        assert calls[1].args == (market.no_token_id, Side.BUY, 50.0) or \
               calls[1].kwargs == {} and calls[1][0] == (market.no_token_id, Side.BUY, 50.0)


# ---------------------------------------------------------------------------
# Profit calculation accuracy
# ---------------------------------------------------------------------------


class TestProfitCalculation:
    """Verify exact profit/fee numbers match the fees module."""

    @patch("src.strategy.arbitrage.is_in_dead_zone", return_value=False)
    async def test_profit_calculation_accuracy(
        self,
        _mock_dz: MagicMock,
        strategy: ArbitrageStrategy,
        book_manager: MagicMock,
        market: Market,
    ) -> None:
        """Verify the returned profit matches hand-calculated values."""
        yes_vwap = 0.30
        no_vwap = 0.30
        size = 50.0

        book_manager.get_fill_estimate.side_effect = [
            _make_fill(vwap=yes_vwap, size=size),
            _make_fill(vwap=no_vwap, size=size),
        ]

        result = await strategy.evaluate(market)
        assert result is not None

        # Hand-calculate expected values
        expected_taker_yes = taker_fee_amount(yes_vwap, size)
        expected_taker_no = taker_fee_amount(no_vwap, size)
        expected_gross = (1.0 - yes_vwap - no_vwap) * size
        expected_winner = winner_fee_amount(min(yes_vwap, no_vwap), 1.0) * size
        expected_total_fees = expected_taker_yes + expected_taker_no + expected_winner
        expected_net = expected_gross - expected_total_fees

        assert result.expected_profit == pytest.approx(expected_net, abs=1e-10)
        assert result.total_fees == pytest.approx(expected_total_fees, abs=1e-10)
        assert result.expected_profit_pct == pytest.approx(expected_net / size, abs=1e-10)
        assert result.metadata["taker_yes"] == pytest.approx(expected_taker_yes, abs=1e-10)
        assert result.metadata["taker_no"] == pytest.approx(expected_taker_no, abs=1e-10)
        assert result.metadata["winner_fee"] == pytest.approx(expected_winner, abs=1e-10)
        assert result.metadata["gross"] == pytest.approx(expected_gross, abs=1e-10)

    @patch("src.strategy.arbitrage.is_in_dead_zone", return_value=False)
    async def test_profit_calculation_asymmetric(
        self,
        _mock_dz: MagicMock,
        strategy: ArbitrageStrategy,
        book_manager: MagicMock,
        market: Market,
    ) -> None:
        """Verify profit calculation with asymmetric YES/NO prices."""
        yes_vwap = 0.35
        no_vwap = 0.50
        size = 50.0

        book_manager.get_fill_estimate.side_effect = [
            _make_fill(vwap=yes_vwap, size=size),
            _make_fill(vwap=no_vwap, size=size),
        ]

        result = await strategy.evaluate(market)
        assert result is not None

        expected_taker_yes = taker_fee_amount(yes_vwap, size)
        expected_taker_no = taker_fee_amount(no_vwap, size)
        expected_gross = (1.0 - yes_vwap - no_vwap) * size
        # Winner fee uses the cheaper side (YES at 0.35)
        expected_winner = winner_fee_amount(min(yes_vwap, no_vwap), 1.0) * size
        expected_total_fees = expected_taker_yes + expected_taker_no + expected_winner
        expected_net = expected_gross - expected_total_fees

        assert result.expected_profit == pytest.approx(expected_net, abs=1e-10)
        assert result.total_fees == pytest.approx(expected_total_fees, abs=1e-10)

    @patch("src.strategy.arbitrage.is_in_dead_zone", return_value=False)
    async def test_confidence_calculation(
        self,
        _mock_dz: MagicMock,
        strategy: ArbitrageStrategy,
        book_manager: MagicMock,
        market: Market,
    ) -> None:
        """Confidence is capped at 1.0 and scales with profit_per_share / min_margin."""
        book_manager.get_fill_estimate.side_effect = [
            _make_fill(vwap=0.30),
            _make_fill(vwap=0.30),
        ]

        result = await strategy.evaluate(market)
        assert result is not None

        # With a wide spread the confidence should be capped at 1.0
        assert result.confidence <= 1.0
        assert result.confidence > 0.0

    @patch("src.strategy.arbitrage.is_in_dead_zone", return_value=False)
    async def test_metadata_contains_all_fields(
        self,
        _mock_dz: MagicMock,
        strategy: ArbitrageStrategy,
        book_manager: MagicMock,
        market: Market,
    ) -> None:
        """The opportunity metadata should contain all expected keys."""
        book_manager.get_fill_estimate.side_effect = [
            _make_fill(vwap=0.30),
            _make_fill(vwap=0.30),
        ]

        result = await strategy.evaluate(market)
        assert result is not None

        expected_keys = {
            "yes_vwap", "no_vwap", "combined_cost",
            "gross", "taker_yes", "taker_no", "winner_fee",
        }
        assert expected_keys <= set(result.metadata.keys())


# ---------------------------------------------------------------------------
# should_exit
# ---------------------------------------------------------------------------


class TestShouldExit:
    def test_should_exit_always_false(
        self,
        strategy: ArbitrageStrategy,
        market: Market,
    ) -> None:
        """Arb positions are held to resolution, so should_exit returns False."""
        position = Position(
            market=market,
            yes_shares=50.0,
            no_shares=50.0,
            yes_cost_basis=22.5,
            no_cost_basis=22.5,
            strategy=StrategyType.ARBITRAGE,
            opened_at=_NOW,
        )

        assert strategy.should_exit(position, market) is False

    def test_should_exit_false_even_with_zero_shares(
        self,
        strategy: ArbitrageStrategy,
        market: Market,
    ) -> None:
        """Even degenerate positions always return False for exit."""
        position = Position(
            market=market,
            yes_shares=0.0,
            no_shares=0.0,
            strategy=StrategyType.ARBITRAGE,
        )

        assert strategy.should_exit(position, market) is False

    def test_should_exit_false_unhedged_position(
        self,
        strategy: ArbitrageStrategy,
        market: Market,
    ) -> None:
        """Even an unhedged position returns False (held to resolution)."""
        position = Position(
            market=market,
            yes_shares=100.0,
            no_shares=0.0,
            strategy=StrategyType.ARBITRAGE,
        )

        assert strategy.should_exit(position, market) is False
