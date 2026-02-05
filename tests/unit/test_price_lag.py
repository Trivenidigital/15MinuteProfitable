"""Comprehensive tests for PriceLagStrategy."""

from __future__ import annotations

import os

# Set the required environment variable BEFORE any Settings import
os.environ.setdefault("BOT_PRIVATE_KEY", "0x" + "ab" * 32)

import time
from datetime import datetime, timezone
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
from src.data.spot_buffer import SpotMovement
from src.strategy.price_lag import ASSET_TO_BINANCE_SYMBOL, PriceLagStrategy


# ---------------------------------------------------------------------------
# Mock SpotBuffer (SpotBuffer may be built concurrently)
# ---------------------------------------------------------------------------


class MockSpotBuffer:
    """Minimal mock matching the SpotBuffer interface."""

    def __init__(self) -> None:
        self._prices: dict[str, float] = {}
        self._movement: SpotMovement | None = None
        self._has_data_val: bool = True

    def get_price(self, symbol: str) -> float | None:
        return self._prices.get(symbol)

    def has_data(self, symbol: str) -> bool:
        return self._has_data_val

    def detect_movement(
        self,
        symbol: str,
        window_seconds: int,
        threshold: float,
    ) -> SpotMovement | None:
        return self._movement


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


def _make_movement(
    direction: str = "UP",
    change_pct: float = 0.005,
    symbol: str = "BTCUSDT",
) -> SpotMovement:
    """Create a SpotMovement with defaults that exceed typical thresholds."""
    start_price = 100000.0
    if direction == "UP":
        end_price = start_price * (1 + change_pct)
    else:
        end_price = start_price * (1 - change_pct)
    return SpotMovement(
        symbol=symbol,
        direction=direction,
        change_pct=change_pct,
        start_price=start_price,
        end_price=end_price,
        window_seconds=15.0,
        timestamp=time.time(),
    )


def _make_orderbook(
    token_id: str,
    best_bid: float = 0.45,
    best_ask: float = 0.46,
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
    vwap: float = 0.46,
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        private_key="0x" + "ab" * 32,  # type: ignore[arg-type]
        order_size=50.0,
        spot_move_threshold=0.0015,
        spot_window_seconds=15,
        odds_lag_threshold=0.03,
        lag_entry_dead_zone_start=60.0,
        lag_entry_dead_zone_end=30.0,
        stop_loss_pct=0.05,
        take_profit_pct=0.10,
        time_exit_seconds=60.0,
        lag_confirmations=2,
    )


@pytest.fixture()
def spot_buffer() -> MockSpotBuffer:
    return MockSpotBuffer()


@pytest.fixture()
def book_manager() -> MockOrderBookManager:
    return MockOrderBookManager()


@pytest.fixture()
def strategy(
    settings: Settings,
    book_manager: MockOrderBookManager,
    spot_buffer: MockSpotBuffer,
) -> PriceLagStrategy:
    return PriceLagStrategy(
        settings=settings,
        book_manager=book_manager,  # type: ignore[arg-type]
        spot_buffer=spot_buffer,  # type: ignore[arg-type]
    )


@pytest.fixture()
def market() -> Market:
    """Market with start 5min ago, end 10min from now -- well within trading window."""
    return _make_market(start_offset=-300.0, end_offset=600.0)


def _setup_for_opportunity(
    spot_buffer: MockSpotBuffer,
    book_manager: MockOrderBookManager,
    direction: str = "UP",
    change_pct: float = 0.005,
    yes_ask: float = 0.46,
    no_ask: float = 0.46,
    fill_vwap: float = 0.46,
    fill_sufficient: bool = True,
) -> None:
    """Configure mocks so that evaluate() can reach the opportunity-building step."""
    spot_buffer._has_data_val = True
    spot_buffer._movement = _make_movement(direction=direction, change_pct=change_pct)

    book_manager.yes_book = _make_orderbook("YES_TOKEN", best_ask=yes_ask)
    book_manager.no_book = _make_orderbook("NO_TOKEN", best_ask=no_ask)
    book_manager.fill_estimate = _make_fill(vwap=fill_vwap, sufficient=fill_sufficient)


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


class TestProperties:
    def test_name(self, strategy: PriceLagStrategy) -> None:
        assert strategy.name == "price_lag"

    def test_strategy_type(self, strategy: PriceLagStrategy) -> None:
        assert strategy.strategy_type == StrategyType.PRICE_LAG


# ---------------------------------------------------------------------------
# evaluate -- returns None scenarios
# ---------------------------------------------------------------------------


class TestEvaluateReturnsNone:
    """Tests where evaluate() must return None."""

    async def test_dead_zone_too_close_to_start(
        self,
        strategy: PriceLagStrategy,
        spot_buffer: MockSpotBuffer,
        book_manager: MockOrderBookManager,
    ) -> None:
        """Market opened 10 seconds ago -- within lag_entry_dead_zone_start (60s)."""
        market = _make_market(start_offset=-10.0, end_offset=890.0)
        _setup_for_opportunity(spot_buffer, book_manager)
        # Pre-fill confirmations so the test isolates the dead zone check
        strategy._consecutive_signals[market.condition_id] = 10
        strategy._last_signal_direction[market.condition_id] = "UP"

        result = await strategy.evaluate(market)
        assert result is None

    async def test_dead_zone_too_close_to_end(
        self,
        strategy: PriceLagStrategy,
        spot_buffer: MockSpotBuffer,
        book_manager: MockOrderBookManager,
    ) -> None:
        """Market ends in 15 seconds -- within lag_entry_dead_zone_end (30s)."""
        market = _make_market(start_offset=-870.0, end_offset=15.0)
        _setup_for_opportunity(spot_buffer, book_manager)
        strategy._consecutive_signals[market.condition_id] = 10
        strategy._last_signal_direction[market.condition_id] = "UP"

        result = await strategy.evaluate(market)
        assert result is None

    async def test_no_spot_data_available(
        self,
        strategy: PriceLagStrategy,
        spot_buffer: MockSpotBuffer,
        book_manager: MockOrderBookManager,
        market: Market,
    ) -> None:
        """SpotBuffer has no data for the asset's Binance symbol."""
        spot_buffer._has_data_val = False

        result = await strategy.evaluate(market)
        assert result is None

    async def test_spot_movement_below_threshold(
        self,
        strategy: PriceLagStrategy,
        spot_buffer: MockSpotBuffer,
        book_manager: MockOrderBookManager,
        market: Market,
    ) -> None:
        """SpotBuffer.detect_movement returns None (movement below threshold)."""
        spot_buffer._has_data_val = True
        spot_buffer._movement = None

        result = await strategy.evaluate(market)
        assert result is None

    async def test_not_enough_confirmations(
        self,
        strategy: PriceLagStrategy,
        spot_buffer: MockSpotBuffer,
        book_manager: MockOrderBookManager,
        market: Market,
    ) -> None:
        """First signal -- only 1 confirmation, need 2."""
        spot_buffer._has_data_val = True
        spot_buffer._movement = _make_movement(direction="UP", change_pct=0.005)
        # No prior confirmations
        strategy._consecutive_signals.clear()
        strategy._last_signal_direction.clear()

        result = await strategy.evaluate(market)
        assert result is None
        # Should have recorded 1 confirmation
        assert strategy._consecutive_signals[market.condition_id] == 1

    async def test_odds_lag_below_threshold(
        self,
        strategy: PriceLagStrategy,
        spot_buffer: MockSpotBuffer,
        book_manager: MockOrderBookManager,
        market: Market,
    ) -> None:
        """Spot moved but odds already reflect it -- no lag."""
        # change_pct=0.002 -> odds_lag = max(0, 0.5 + 0.002*10 - 0.55) = max(0, -0.03) = 0
        _setup_for_opportunity(
            spot_buffer, book_manager,
            direction="UP", change_pct=0.002, yes_ask=0.55,
        )
        strategy._consecutive_signals[market.condition_id] = 10
        strategy._last_signal_direction[market.condition_id] = "UP"

        result = await strategy.evaluate(market)
        assert result is None

    async def test_no_books_available(
        self,
        strategy: PriceLagStrategy,
        spot_buffer: MockSpotBuffer,
        book_manager: MockOrderBookManager,
        market: Market,
    ) -> None:
        """OrderBookManager returns None for the books."""
        spot_buffer._has_data_val = True
        spot_buffer._movement = _make_movement(direction="UP", change_pct=0.005)
        strategy._consecutive_signals[market.condition_id] = 10
        strategy._last_signal_direction[market.condition_id] = "UP"
        book_manager.yes_book = None
        book_manager.no_book = None

        result = await strategy.evaluate(market)
        assert result is None

    async def test_no_yes_book_available(
        self,
        strategy: PriceLagStrategy,
        spot_buffer: MockSpotBuffer,
        book_manager: MockOrderBookManager,
        market: Market,
    ) -> None:
        """Only the YES book is missing."""
        spot_buffer._has_data_val = True
        spot_buffer._movement = _make_movement(direction="UP", change_pct=0.005)
        strategy._consecutive_signals[market.condition_id] = 10
        strategy._last_signal_direction[market.condition_id] = "UP"
        book_manager.yes_book = None
        book_manager.no_book = _make_orderbook("NO_TOKEN")

        result = await strategy.evaluate(market)
        assert result is None

    async def test_insufficient_liquidity(
        self,
        strategy: PriceLagStrategy,
        spot_buffer: MockSpotBuffer,
        book_manager: MockOrderBookManager,
        market: Market,
    ) -> None:
        """Fill estimate has insufficient liquidity."""
        _setup_for_opportunity(
            spot_buffer, book_manager,
            direction="UP", change_pct=0.005, yes_ask=0.46,
            fill_sufficient=False,
        )
        strategy._consecutive_signals[market.condition_id] = 10
        strategy._last_signal_direction[market.condition_id] = "UP"

        result = await strategy.evaluate(market)
        assert result is None

    async def test_unknown_asset_returns_none(
        self,
        strategy: PriceLagStrategy,
        spot_buffer: MockSpotBuffer,
        book_manager: MockOrderBookManager,
    ) -> None:
        """Market with an asset not in ASSET_TO_BINANCE_SYMBOL returns None."""
        market = _make_market(asset="DOGE")
        spot_buffer._has_data_val = True

        result = await strategy.evaluate(market)
        assert result is None

    async def test_books_with_no_asks_returns_none(
        self,
        strategy: PriceLagStrategy,
        spot_buffer: MockSpotBuffer,
        book_manager: MockOrderBookManager,
        market: Market,
    ) -> None:
        """Books exist but have no ask levels."""
        spot_buffer._has_data_val = True
        spot_buffer._movement = _make_movement(direction="UP", change_pct=0.005)
        strategy._consecutive_signals[market.condition_id] = 10
        strategy._last_signal_direction[market.condition_id] = "UP"
        # Books with empty asks
        book_manager.yes_book = OrderBook(token_id="YES_TOKEN", bids=[], asks=[])
        book_manager.no_book = OrderBook(token_id="NO_TOKEN", bids=[], asks=[])

        result = await strategy.evaluate(market)
        assert result is None


# ---------------------------------------------------------------------------
# evaluate -- opportunity found
# ---------------------------------------------------------------------------


class TestEvaluateOpportunityFound:
    """Tests where evaluate() must return a valid Opportunity."""

    async def test_opportunity_for_up_movement(
        self,
        strategy: PriceLagStrategy,
        spot_buffer: MockSpotBuffer,
        book_manager: MockOrderBookManager,
        market: Market,
    ) -> None:
        """Spot UP with sufficient lag should produce an opportunity with yes_fill."""
        # change_pct=0.005, yes_ask=0.46
        # odds_lag = max(0, 0.5 + 0.005*10 - 0.46) = max(0, 0.09) = 0.09 > 0.03 threshold
        _setup_for_opportunity(
            spot_buffer, book_manager,
            direction="UP", change_pct=0.005, yes_ask=0.46, fill_vwap=0.46,
        )
        strategy._consecutive_signals[market.condition_id] = 10
        strategy._last_signal_direction[market.condition_id] = "UP"

        result = await strategy.evaluate(market)

        assert result is not None
        assert isinstance(result, Opportunity)
        assert result.strategy == StrategyType.PRICE_LAG
        assert result.market is market
        assert result.yes_fill is not None
        assert result.no_fill is None
        assert result.expected_profit > 0
        assert result.total_fees > 0
        assert result.confidence > 0

    async def test_opportunity_for_down_movement(
        self,
        strategy: PriceLagStrategy,
        spot_buffer: MockSpotBuffer,
        book_manager: MockOrderBookManager,
        market: Market,
    ) -> None:
        """Spot DOWN with sufficient lag should produce an opportunity with no_fill."""
        # change_pct=0.005, no_ask=0.46
        # odds_lag = max(0, 0.5 + 0.005*10 - 0.46) = 0.09 > 0.03
        _setup_for_opportunity(
            spot_buffer, book_manager,
            direction="DOWN", change_pct=0.005, no_ask=0.46, fill_vwap=0.46,
        )
        strategy._consecutive_signals[market.condition_id] = 10
        strategy._last_signal_direction[market.condition_id] = "DOWN"

        result = await strategy.evaluate(market)

        assert result is not None
        assert isinstance(result, Opportunity)
        assert result.strategy == StrategyType.PRICE_LAG
        assert result.yes_fill is None
        assert result.no_fill is not None

    async def test_opportunity_metadata_contains_required_fields(
        self,
        strategy: PriceLagStrategy,
        spot_buffer: MockSpotBuffer,
        book_manager: MockOrderBookManager,
        market: Market,
    ) -> None:
        """Metadata should contain direction, odds_lag, spot_change_pct and others."""
        _setup_for_opportunity(
            spot_buffer, book_manager,
            direction="UP", change_pct=0.005, yes_ask=0.46, fill_vwap=0.46,
        )
        strategy._consecutive_signals[market.condition_id] = 10
        strategy._last_signal_direction[market.condition_id] = "UP"

        result = await strategy.evaluate(market)

        assert result is not None
        meta = result.metadata
        assert meta["direction"] == "UP"
        assert meta["spot_change_pct"] == pytest.approx(0.005)
        assert meta["odds_lag"] == pytest.approx(0.09)
        assert meta["current_price"] == pytest.approx(0.46)
        assert meta["target_token_id"] == "YES_TOKEN"
        assert meta["sizing_multiplier"] == 1.0
        assert meta["confirmations"] == 11  # pre-set to 10, incremented to 11 during evaluate
        assert meta["binance_symbol"] == "BTCUSDT"

    async def test_metadata_down_direction(
        self,
        strategy: PriceLagStrategy,
        spot_buffer: MockSpotBuffer,
        book_manager: MockOrderBookManager,
        market: Market,
    ) -> None:
        """DOWN movement metadata should show direction=DOWN and target NO token."""
        _setup_for_opportunity(
            spot_buffer, book_manager,
            direction="DOWN", change_pct=0.005, no_ask=0.46, fill_vwap=0.46,
        )
        strategy._consecutive_signals[market.condition_id] = 10
        strategy._last_signal_direction[market.condition_id] = "DOWN"

        result = await strategy.evaluate(market)

        assert result is not None
        assert result.metadata["direction"] == "DOWN"
        assert result.metadata["target_token_id"] == "NO_TOKEN"

    async def test_opportunity_expected_keys_present(
        self,
        strategy: PriceLagStrategy,
        spot_buffer: MockSpotBuffer,
        book_manager: MockOrderBookManager,
        market: Market,
    ) -> None:
        """All expected metadata keys are present."""
        _setup_for_opportunity(
            spot_buffer, book_manager,
            direction="UP", change_pct=0.005, yes_ask=0.46, fill_vwap=0.46,
        )
        strategy._consecutive_signals[market.condition_id] = 10
        strategy._last_signal_direction[market.condition_id] = "UP"

        result = await strategy.evaluate(market)

        assert result is not None
        expected_keys = {
            "direction",
            "spot_change_pct",
            "odds_lag",
            "current_price",
            "target_token_id",
            "sizing_multiplier",
            "confirmations",
            "binance_symbol",
        }
        assert expected_keys == set(result.metadata.keys())


# ---------------------------------------------------------------------------
# Time-aware sizing
# ---------------------------------------------------------------------------


class TestTimeAwareSizing:
    """Verify _time_aware_sizing returns correct multipliers."""

    def test_more_than_5_minutes(self, strategy: PriceLagStrategy) -> None:
        assert strategy._time_aware_sizing(301.0) == 1.0
        assert strategy._time_aware_sizing(600.0) == 1.0

    def test_2_to_5_minutes(self, strategy: PriceLagStrategy) -> None:
        assert strategy._time_aware_sizing(121.0) == 0.5
        assert strategy._time_aware_sizing(300.0) == 0.5

    def test_30s_to_2_minutes(self, strategy: PriceLagStrategy) -> None:
        assert strategy._time_aware_sizing(31.0) == 0.25
        assert strategy._time_aware_sizing(120.0) == 0.25

    def test_less_than_30_seconds(self, strategy: PriceLagStrategy) -> None:
        assert strategy._time_aware_sizing(30.0) == 0.0
        assert strategy._time_aware_sizing(10.0) == 0.0
        assert strategy._time_aware_sizing(0.0) == 0.0

    def test_boundary_at_300(self, strategy: PriceLagStrategy) -> None:
        """300 seconds is exactly 5 minutes -- falls in 2-5min bucket."""
        assert strategy._time_aware_sizing(300.0) == 0.5

    def test_boundary_at_120(self, strategy: PriceLagStrategy) -> None:
        """120 seconds is exactly 2 minutes -- falls in 30s-2min bucket."""
        assert strategy._time_aware_sizing(120.0) == 0.25

    def test_boundary_at_30(self, strategy: PriceLagStrategy) -> None:
        """30 seconds -- falls in <30s bucket (not > 30)."""
        assert strategy._time_aware_sizing(30.0) == 0.0


# ---------------------------------------------------------------------------
# should_exit
# ---------------------------------------------------------------------------


class TestShouldExit:
    """Tests for the should_exit method."""

    def test_time_based_exit(
        self,
        strategy: PriceLagStrategy,
        book_manager: MockOrderBookManager,
    ) -> None:
        """Should exit when within time_exit_seconds of market close."""
        # Market ends in 30 seconds -- time_exit_seconds is 60
        market = _make_market(start_offset=-870.0, end_offset=30.0)
        position = Position(
            market=market,
            yes_shares=50.0,
            yes_cost_basis=23.0,
            strategy=StrategyType.PRICE_LAG,
        )

        assert strategy.should_exit(position, market) is True

    def test_stop_loss_triggered(
        self,
        strategy: PriceLagStrategy,
        book_manager: MockOrderBookManager,
    ) -> None:
        """Should exit when loss exceeds stop_loss_pct."""
        market = _make_market(start_offset=-300.0, end_offset=600.0)
        # Cost basis = 23.0, current bid = 0.40 -> value = 50 * 0.40 = 20.0
        # pnl_pct = (20.0 - 23.0) / 23.0 = -0.1304 -> exceeds -0.05 stop_loss
        position = Position(
            market=market,
            yes_shares=50.0,
            yes_cost_basis=23.0,
            strategy=StrategyType.PRICE_LAG,
        )
        book_manager.yes_book = _make_orderbook("YES_TOKEN", best_bid=0.40)

        assert strategy.should_exit(position, market) is True

    def test_take_profit_triggered(
        self,
        strategy: PriceLagStrategy,
        book_manager: MockOrderBookManager,
    ) -> None:
        """Should exit when profit exceeds take_profit_pct."""
        market = _make_market(start_offset=-300.0, end_offset=600.0)
        # Cost basis = 23.0, current bid = 0.55 -> value = 50 * 0.55 = 27.5
        # pnl_pct = (27.5 - 23.0) / 23.0 = 0.1957 -> exceeds 0.10 take_profit
        position = Position(
            market=market,
            yes_shares=50.0,
            yes_cost_basis=23.0,
            strategy=StrategyType.PRICE_LAG,
        )
        book_manager.yes_book = _make_orderbook("YES_TOKEN", best_bid=0.55)

        assert strategy.should_exit(position, market) is True

    def test_no_exit_condition_met(
        self,
        strategy: PriceLagStrategy,
        book_manager: MockOrderBookManager,
    ) -> None:
        """Should return False when no exit condition is met."""
        market = _make_market(start_offset=-300.0, end_offset=600.0)
        # Cost basis = 23.0, current bid = 0.47 -> value = 50 * 0.47 = 23.5
        # pnl_pct = (23.5 - 23.0) / 23.0 = 0.0217 -> within [-0.05, 0.10]
        position = Position(
            market=market,
            yes_shares=50.0,
            yes_cost_basis=23.0,
            strategy=StrategyType.PRICE_LAG,
        )
        book_manager.yes_book = _make_orderbook("YES_TOKEN", best_bid=0.47)

        assert strategy.should_exit(position, market) is False

    def test_no_exit_with_zero_cost_basis(
        self,
        strategy: PriceLagStrategy,
        book_manager: MockOrderBookManager,
    ) -> None:
        """Zero cost basis should return False (can't compute pnl_pct)."""
        market = _make_market(start_offset=-300.0, end_offset=600.0)
        position = Position(
            market=market,
            yes_shares=0.0,
            yes_cost_basis=0.0,
            strategy=StrategyType.PRICE_LAG,
        )

        assert strategy.should_exit(position, market) is False

    def test_stop_loss_with_no_shares(
        self,
        strategy: PriceLagStrategy,
        book_manager: MockOrderBookManager,
    ) -> None:
        """Stop-loss on a NO position (bought NO, price dropped)."""
        market = _make_market(start_offset=-300.0, end_offset=600.0)
        # Cost basis = 23.0 (in no_cost_basis), NO bid = 0.40 -> value = 50 * 0.40 = 20.0
        # pnl_pct = (20.0 - 23.0) / 23.0 = -0.1304 -> exceeds stop
        position = Position(
            market=market,
            no_shares=50.0,
            no_cost_basis=23.0,
            strategy=StrategyType.PRICE_LAG,
        )
        book_manager.no_book = _make_orderbook("NO_TOKEN", best_bid=0.40)

        assert strategy.should_exit(position, market) is True

    def test_time_exit_at_exact_boundary(
        self,
        strategy: PriceLagStrategy,
        book_manager: MockOrderBookManager,
    ) -> None:
        """Market ends in ~time_exit_seconds -- should exit (<=)."""
        market = _make_market(start_offset=-840.0, end_offset=59.0)
        position = Position(
            market=market,
            yes_shares=50.0,
            yes_cost_basis=23.0,
            strategy=StrategyType.PRICE_LAG,
        )

        assert strategy.should_exit(position, market) is True

    def test_no_exit_with_zero_current_value(
        self,
        strategy: PriceLagStrategy,
        book_manager: MockOrderBookManager,
    ) -> None:
        """If current value is 0 (no book data), should not exit on pnl check."""
        market = _make_market(start_offset=-300.0, end_offset=600.0)
        position = Position(
            market=market,
            yes_shares=50.0,
            yes_cost_basis=23.0,
            strategy=StrategyType.PRICE_LAG,
        )
        # No book data -> current_value stays 0
        book_manager.yes_book = None

        assert strategy.should_exit(position, market) is False


# ---------------------------------------------------------------------------
# Consecutive confirmations tracking
# ---------------------------------------------------------------------------


class TestConsecutiveConfirmations:
    """Tests for confirmation counting logic."""

    async def test_confirmations_increment_same_direction(
        self,
        strategy: PriceLagStrategy,
        spot_buffer: MockSpotBuffer,
        book_manager: MockOrderBookManager,
        market: Market,
    ) -> None:
        """Repeated same-direction signals increment the counter."""
        spot_buffer._has_data_val = True
        spot_buffer._movement = _make_movement(direction="UP", change_pct=0.005)

        # First call -- counter goes to 1
        await strategy.evaluate(market)
        assert strategy._consecutive_signals[market.condition_id] == 1
        assert strategy._last_signal_direction[market.condition_id] == "UP"

        # Second call -- counter goes to 2
        await strategy.evaluate(market)
        assert strategy._consecutive_signals[market.condition_id] == 2

        # Third call -- counter goes to 3
        await strategy.evaluate(market)
        assert strategy._consecutive_signals[market.condition_id] == 3

    async def test_confirmations_reset_on_direction_change(
        self,
        strategy: PriceLagStrategy,
        spot_buffer: MockSpotBuffer,
        book_manager: MockOrderBookManager,
        market: Market,
    ) -> None:
        """Changing direction resets the counter to 1."""
        spot_buffer._has_data_val = True

        # Two UP signals
        spot_buffer._movement = _make_movement(direction="UP", change_pct=0.005)
        await strategy.evaluate(market)
        await strategy.evaluate(market)
        assert strategy._consecutive_signals[market.condition_id] == 2
        assert strategy._last_signal_direction[market.condition_id] == "UP"

        # Switch to DOWN
        spot_buffer._movement = _make_movement(direction="DOWN", change_pct=0.005)
        await strategy.evaluate(market)
        assert strategy._consecutive_signals[market.condition_id] == 1
        assert strategy._last_signal_direction[market.condition_id] == "DOWN"

    async def test_confirmations_reset_on_no_movement(
        self,
        strategy: PriceLagStrategy,
        spot_buffer: MockSpotBuffer,
        book_manager: MockOrderBookManager,
        market: Market,
    ) -> None:
        """No movement clears the confirmation state entirely."""
        spot_buffer._has_data_val = True

        # Build up some confirmations
        spot_buffer._movement = _make_movement(direction="UP", change_pct=0.005)
        await strategy.evaluate(market)
        await strategy.evaluate(market)
        assert strategy._consecutive_signals[market.condition_id] == 2

        # No movement
        spot_buffer._movement = None
        await strategy.evaluate(market)
        assert market.condition_id not in strategy._consecutive_signals
        assert market.condition_id not in strategy._last_signal_direction

    async def test_exact_confirmation_threshold_triggers(
        self,
        strategy: PriceLagStrategy,
        spot_buffer: MockSpotBuffer,
        book_manager: MockOrderBookManager,
        market: Market,
    ) -> None:
        """At exactly lag_confirmations (2), the strategy should proceed past the check."""
        _setup_for_opportunity(
            spot_buffer, book_manager,
            direction="UP", change_pct=0.005, yes_ask=0.46, fill_vwap=0.46,
        )
        # Pre-set to 1 confirmation in UP direction, next call makes 2
        strategy._consecutive_signals[market.condition_id] = 1
        strategy._last_signal_direction[market.condition_id] = "UP"

        result = await strategy.evaluate(market)
        # confirmations=2 meets lag_confirmations=2, so should proceed
        assert strategy._consecutive_signals[market.condition_id] == 2
        # Should get an opportunity (all other conditions are met)
        assert result is not None


# ---------------------------------------------------------------------------
# ASSET_TO_BINANCE_SYMBOL mapping
# ---------------------------------------------------------------------------


class TestAssetMapping:
    def test_btc_maps_to_btcusdt(self) -> None:
        assert ASSET_TO_BINANCE_SYMBOL["BTC"] == "BTCUSDT"

    def test_eth_maps_to_ethusdt(self) -> None:
        assert ASSET_TO_BINANCE_SYMBOL["ETH"] == "ETHUSDT"

    def test_sol_maps_to_solusdt(self) -> None:
        assert ASSET_TO_BINANCE_SYMBOL["SOL"] == "SOLUSDT"

    def test_xrp_maps_to_xrpusdt(self) -> None:
        assert ASSET_TO_BINANCE_SYMBOL["XRP"] == "XRPUSDT"


# ---------------------------------------------------------------------------
# Sizing applied in evaluate
# ---------------------------------------------------------------------------


class TestSizingInEvaluate:
    """Verify that time-aware sizing is applied within evaluate."""

    async def test_sizing_multiplier_in_metadata(
        self,
        strategy: PriceLagStrategy,
        spot_buffer: MockSpotBuffer,
        book_manager: MockOrderBookManager,
    ) -> None:
        """With >5min remaining, sizing_multiplier should be 1.0."""
        market = _make_market(start_offset=-300.0, end_offset=600.0)
        _setup_for_opportunity(
            spot_buffer, book_manager,
            direction="UP", change_pct=0.005, yes_ask=0.46, fill_vwap=0.46,
        )
        strategy._consecutive_signals[market.condition_id] = 10
        strategy._last_signal_direction[market.condition_id] = "UP"

        result = await strategy.evaluate(market)

        assert result is not None
        assert result.metadata["sizing_multiplier"] == 1.0

    async def test_sizing_half_size_2_to_5_min(
        self,
        strategy: PriceLagStrategy,
        spot_buffer: MockSpotBuffer,
        book_manager: MockOrderBookManager,
    ) -> None:
        """With 3 minutes remaining (180s), sizing_multiplier should be 0.5."""
        market = _make_market(start_offset=-720.0, end_offset=180.0)
        _setup_for_opportunity(
            spot_buffer, book_manager,
            direction="UP", change_pct=0.005, yes_ask=0.46, fill_vwap=0.46,
        )
        strategy._consecutive_signals[market.condition_id] = 10
        strategy._last_signal_direction[market.condition_id] = "UP"

        result = await strategy.evaluate(market)

        assert result is not None
        assert result.metadata["sizing_multiplier"] == 0.5

    async def test_sizing_zero_prevents_opportunity(
        self,
        strategy: PriceLagStrategy,
        spot_buffer: MockSpotBuffer,
        book_manager: MockOrderBookManager,
    ) -> None:
        """With < 30s remaining and > dead_zone_end, sizing=0 means no trade.

        Note: For this test the dead zone end is 30s, so time_to_close between
        30 and 31 seconds is the only window where sizing=0 while not in dead zone.
        We need to set lag_entry_dead_zone_end to a smaller value.
        """
        strategy._settings = Settings(
            private_key="0x" + "ab" * 32,  # type: ignore[arg-type]
            order_size=50.0,
            lag_entry_dead_zone_start=60.0,
            lag_entry_dead_zone_end=10.0,  # Tighter dead zone to allow < 30s
            lag_confirmations=2,
            odds_lag_threshold=0.03,
            spot_move_threshold=0.0015,
            spot_window_seconds=15,
        )
        # Market ends in 25 seconds -- past dead zone (10s) but sizing = 0 (< 30s)
        market = _make_market(start_offset=-875.0, end_offset=25.0)
        _setup_for_opportunity(
            spot_buffer, book_manager,
            direction="UP", change_pct=0.005, yes_ask=0.46, fill_vwap=0.46,
        )
        strategy._consecutive_signals[market.condition_id] = 10
        strategy._last_signal_direction[market.condition_id] = "UP"

        result = await strategy.evaluate(market)
        assert result is None
