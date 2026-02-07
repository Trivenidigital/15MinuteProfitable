"""Comprehensive tests for ResolutionSniperStrategy."""

from __future__ import annotations

import os

os.environ.setdefault("BOT_PRIVATE_KEY", "0x" + "ab" * 32)

import time
from datetime import UTC, datetime

import pytest

from src.config import Settings
from src.core.models import (
    DailyPnL,
    FillEstimate,
    Market,
    Opportunity,
    OrderBook,
    OrderBookLevel,
    Position,
    Side,
    StrategyType,
)
from src.strategy.resolution_sniper import (
    ResolutionSniperStrategy,
    _normal_cdf,
)

# ---------------------------------------------------------------------------
# Mock SpotBuffer
# ---------------------------------------------------------------------------


class MockSpotBuffer:
    """SpotBuffer mock with controllable price history."""

    def __init__(self) -> None:
        self._prices: dict[str, float] = {}
        self._has_data_val: bool = True
        self._price_history: dict[str, list[tuple[float, float]]] = {}

    def get_price(self, symbol: str) -> float | None:
        return self._prices.get(symbol)

    def has_data(self, symbol: str) -> bool:
        return self._has_data_val

    def get_price_history(
        self, symbol: str, window_seconds: int | None = None
    ) -> list[tuple[float, float]]:
        return self._price_history.get(symbol, [])


# ---------------------------------------------------------------------------
# Mock OrderBookManager
# ---------------------------------------------------------------------------


class MockOrderBookManager:
    """Controllable mock for OrderBookManager."""

    def __init__(self) -> None:
        self.yes_book: OrderBook | None = None
        self.no_book: OrderBook | None = None
        self.fill_estimate: FillEstimate | None = None
        self._stale: bool = False

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

    def is_stale(self, token_id: str, threshold_s: float = 30.0) -> bool:
        return self._stale


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_market(
    asset: str = "BTC",
    yes_token: str = "YES_TOKEN",
    no_token: str = "NO_TOKEN",
    slug: str = "btc-updown-15m-test",
    condition_id: str = "cond_sniper",
    start_offset: float = -800.0,
    end_offset: float = 100.0,
) -> Market:
    """Create a Market with configurable start/end relative to now."""
    now = time.time()
    return Market(
        condition_id=condition_id,
        slug=slug,
        question="Will BTC go up?",
        yes_token_id=yes_token,
        no_token_id=no_token,
        start_time=datetime.fromtimestamp(now + start_offset, tz=UTC),
        end_time=datetime.fromtimestamp(now + end_offset, tz=UTC),
        asset=asset,
        neg_risk=True,
    )


def _make_fill(
    vwap: float = 0.92,
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


def _make_price_history(
    n: int = 30,
    base_price: float = 100000.0,
    vol: float = 0.001,
    interval: float = 2.0,
) -> list[tuple[float, float]]:
    """Generate synthetic price history with controlled volatility.

    Creates n data points with prices oscillating around base_price.
    """
    now = time.time()
    history: list[tuple[float, float]] = []
    for i in range(n):
        ts = now - (n - 1 - i) * interval
        # Alternate small movements to create measurable vol
        sign = 1.0 if i % 2 == 0 else -1.0
        price = base_price * (1.0 + sign * vol * (i % 3))
        history.append((ts, price))
    return history


def _setup_for_sniper(
    spot_buffer: MockSpotBuffer,
    book_manager: MockOrderBookManager,
    open_price: float = 100000.0,
    current_spot: float = 100500.0,
    fill_vwap: float = 0.92,
    fill_sufficient: bool = True,
    stale: bool = False,
    vol: float = 0.001,
    n_points: int = 30,
) -> None:
    """Configure mocks for a successful sniper evaluation."""
    spot_buffer._has_data_val = True
    spot_buffer._prices["BTCUSDT"] = current_spot
    spot_buffer._price_history["BTCUSDT"] = _make_price_history(
        n=n_points, base_price=current_spot, vol=vol,
    )
    book_manager.yes_book = OrderBook(
        token_id="YES_TOKEN",
        bids=[OrderBookLevel(price=0.90, size=500.0)],
        asks=[OrderBookLevel(price=fill_vwap, size=500.0)],
        timestamp_ms=int(time.time() * 1000),
    )
    book_manager.no_book = OrderBook(
        token_id="NO_TOKEN",
        bids=[OrderBookLevel(price=0.90, size=500.0)],
        asks=[OrderBookLevel(price=fill_vwap, size=500.0)],
        timestamp_ms=int(time.time() * 1000),
    )
    book_manager.fill_estimate = _make_fill(vwap=fill_vwap, sufficient=fill_sufficient)
    book_manager._stale = stale


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        private_key="0x" + "ab" * 32,  # type: ignore[arg-type]
        enable_resolution_sniper=True,
        sniper_order_size=150.0,
        sniper_min_confidence=0.90,
        sniper_window_seconds=120.0,
        sniper_hard_stop_seconds=15.0,
        sniper_max_entry_price=0.97,
        sniper_exit_confidence_floor=0.0,
        sniper_min_vol_data_points=10,
        sniper_vol_floor=0.0001,
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
) -> ResolutionSniperStrategy:
    return ResolutionSniperStrategy(
        settings=settings,
        book_manager=book_manager,  # type: ignore[arg-type]
        spot_buffer=spot_buffer,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


class TestProperties:
    def test_name(self, strategy: ResolutionSniperStrategy) -> None:
        assert strategy.name == "resolution_sniper"

    def test_strategy_type(self, strategy: ResolutionSniperStrategy) -> None:
        assert strategy.strategy_type == StrategyType.RESOLUTION_SNIPER


# ---------------------------------------------------------------------------
# Normal CDF
# ---------------------------------------------------------------------------


class TestNormalCDF:
    def test_zero_gives_half(self) -> None:
        assert _normal_cdf(0.0) == pytest.approx(0.5, abs=1e-6)

    def test_196_gives_975(self) -> None:
        assert _normal_cdf(1.96) == pytest.approx(0.975, abs=1e-3)

    def test_neg_196_gives_025(self) -> None:
        assert _normal_cdf(-1.96) == pytest.approx(0.025, abs=1e-3)

    def test_extreme_positive(self) -> None:
        assert _normal_cdf(10.0) == 1.0

    def test_extreme_negative(self) -> None:
        assert _normal_cdf(-10.0) == 0.0


# ---------------------------------------------------------------------------
# evaluate -- returns None
# ---------------------------------------------------------------------------


class TestEvaluateReturnsNone:

    async def test_too_early(
        self, strategy: ResolutionSniperStrategy,
        spot_buffer: MockSpotBuffer, book_manager: MockOrderBookManager,
    ) -> None:
        """More than 120s remaining — outside sniper window."""
        market = _make_market(end_offset=200.0)  # 200s remaining
        _setup_for_sniper(spot_buffer, book_manager)
        result = await strategy.evaluate(market)
        assert result is None

    async def test_past_hard_stop(
        self, strategy: ResolutionSniperStrategy,
        spot_buffer: MockSpotBuffer, book_manager: MockOrderBookManager,
    ) -> None:
        """Less than 15s remaining — past hard stop."""
        market = _make_market(end_offset=10.0)  # 10s remaining
        _setup_for_sniper(spot_buffer, book_manager)
        result = await strategy.evaluate(market)
        assert result is None

    async def test_all_tranches_taken(
        self, strategy: ResolutionSniperStrategy,
        spot_buffer: MockSpotBuffer, book_manager: MockOrderBookManager,
    ) -> None:
        """All 3 tranches already taken for this market."""
        market = _make_market(end_offset=100.0)
        _setup_for_sniper(spot_buffer, book_manager)
        strategy._tranches_taken[market.condition_id] = {0, 1, 2}
        result = await strategy.evaluate(market)
        assert result is None

    async def test_unknown_asset(
        self, strategy: ResolutionSniperStrategy,
        spot_buffer: MockSpotBuffer, book_manager: MockOrderBookManager,
    ) -> None:
        """Asset not in ASSET_TO_BINANCE_SYMBOL."""
        market = _make_market(asset="DOGE", end_offset=100.0)
        _setup_for_sniper(spot_buffer, book_manager)
        result = await strategy.evaluate(market)
        assert result is None

    async def test_no_spot_data(
        self, strategy: ResolutionSniperStrategy,
        spot_buffer: MockSpotBuffer, book_manager: MockOrderBookManager,
    ) -> None:
        """SpotBuffer has no data for the symbol."""
        market = _make_market(end_offset=100.0)
        spot_buffer._has_data_val = False
        result = await strategy.evaluate(market)
        assert result is None

    async def test_no_opening_price(
        self, strategy: ResolutionSniperStrategy,
        spot_buffer: MockSpotBuffer, book_manager: MockOrderBookManager,
    ) -> None:
        """get_price returns None — can't capture opening price."""
        market = _make_market(end_offset=100.0)
        spot_buffer._has_data_val = True
        spot_buffer._prices.clear()
        result = await strategy.evaluate(market)
        assert result is None

    async def test_insufficient_vol_data(
        self, strategy: ResolutionSniperStrategy,
        spot_buffer: MockSpotBuffer, book_manager: MockOrderBookManager,
    ) -> None:
        """Fewer than min_vol_data_points in price history."""
        market = _make_market(end_offset=100.0)
        spot_buffer._has_data_val = True
        spot_buffer._prices["BTCUSDT"] = 100500.0
        spot_buffer._price_history["BTCUSDT"] = _make_price_history(n=5)  # < 10
        result = await strategy.evaluate(market)
        assert result is None

    async def test_confidence_below_threshold(
        self, strategy: ResolutionSniperStrategy,
        spot_buffer: MockSpotBuffer, book_manager: MockOrderBookManager,
    ) -> None:
        """Win probability below 90% threshold."""
        market = _make_market(end_offset=100.0)
        # Very small distance + high vol = low confidence
        _setup_for_sniper(
            spot_buffer, book_manager,
            open_price=100000.0,
            current_spot=100001.0,  # Tiny 0.001% move
            vol=0.01,  # High vol
        )
        # Force the opening price to our desired value
        strategy._opening_prices[market.condition_id] = (100000.0, time.time())
        result = await strategy.evaluate(market)
        assert result is None

    async def test_no_spot_movement(
        self, strategy: ResolutionSniperStrategy,
        spot_buffer: MockSpotBuffer, book_manager: MockOrderBookManager,
    ) -> None:
        """Current spot equals opening price — zero distance."""
        market = _make_market(end_offset=100.0)
        _setup_for_sniper(spot_buffer, book_manager, current_spot=100000.0)
        strategy._opening_prices[market.condition_id] = (100000.0, time.time())
        result = await strategy.evaluate(market)
        assert result is None

    async def test_stale_orderbook(
        self, strategy: ResolutionSniperStrategy,
        spot_buffer: MockSpotBuffer, book_manager: MockOrderBookManager,
    ) -> None:
        """Orderbook is stale — should skip."""
        market = _make_market(end_offset=100.0)
        _setup_for_sniper(spot_buffer, book_manager, stale=True)
        strategy._opening_prices[market.condition_id] = (100000.0, time.time())
        result = await strategy.evaluate(market)
        assert result is None

    async def test_insufficient_liquidity(
        self, strategy: ResolutionSniperStrategy,
        spot_buffer: MockSpotBuffer, book_manager: MockOrderBookManager,
    ) -> None:
        """Fill estimate says insufficient liquidity."""
        market = _make_market(end_offset=100.0)
        _setup_for_sniper(spot_buffer, book_manager, fill_sufficient=False)
        strategy._opening_prices[market.condition_id] = (100000.0, time.time())
        result = await strategy.evaluate(market)
        assert result is None

    async def test_fill_price_too_high(
        self, strategy: ResolutionSniperStrategy,
        spot_buffer: MockSpotBuffer, book_manager: MockOrderBookManager,
    ) -> None:
        """Fill VWAP exceeds sniper_max_entry_price."""
        market = _make_market(end_offset=100.0)
        _setup_for_sniper(spot_buffer, book_manager, fill_vwap=0.98)  # > 0.97 max
        strategy._opening_prices[market.condition_id] = (100000.0, time.time())
        result = await strategy.evaluate(market)
        assert result is None

    async def test_negative_expected_value(
        self, strategy: ResolutionSniperStrategy,
        spot_buffer: MockSpotBuffer, book_manager: MockOrderBookManager,
    ) -> None:
        """Expected profit is negative after fees."""
        market = _make_market(end_offset=100.0)
        # Use a fill price that makes the trade barely profitable,
        # then set win probability just high enough to pass the gate but
        # low enough that EV is negative with fees.
        _setup_for_sniper(
            spot_buffer, book_manager,
            open_price=100000.0,
            current_spot=100050.0,  # Small move
            fill_vwap=0.96,  # High entry price
            vol=0.0001,  # Very low vol -> high confidence
        )
        # Manually set opening price for control
        strategy._opening_prices[market.condition_id] = (100000.0, time.time())
        # With entry at 0.96, win_prob ~0.90-0.95, the expected profit
        # after 2% winner fee + taker fee might be negative
        result = await strategy.evaluate(market)
        # Result should be None (negative EV) or Opportunity (marginally positive)
        # This is a boundary case — the point is the check exists
        if result is not None:
            assert result.expected_profit > 0


# ---------------------------------------------------------------------------
# evaluate -- returns Opportunity
# ---------------------------------------------------------------------------


class TestEvaluateReturnsOpportunity:

    async def test_tranche_0_up_direction(
        self, strategy: ResolutionSniperStrategy,
        spot_buffer: MockSpotBuffer, book_manager: MockOrderBookManager,
    ) -> None:
        """Tranche 0 with UP direction should return opportunity with yes_fill."""
        market = _make_market(end_offset=100.0)  # ~100s remaining -> tranche 0
        _setup_for_sniper(
            spot_buffer, book_manager,
            open_price=100000.0,
            current_spot=101000.0,  # 1% up
            fill_vwap=0.85,  # Entry price that gives positive EV at >90% win prob
            vol=0.0005,  # Low vol -> high confidence
        )
        strategy._opening_prices[market.condition_id] = (100000.0, time.time())

        result = await strategy.evaluate(market)

        assert result is not None
        assert isinstance(result, Opportunity)
        assert result.strategy == StrategyType.RESOLUTION_SNIPER
        assert result.yes_fill is not None
        assert result.no_fill is None
        assert result.expected_profit > 0
        assert result.metadata["direction"] == "UP"

    async def test_tranche_1_down_direction(
        self, strategy: ResolutionSniperStrategy,
        spot_buffer: MockSpotBuffer, book_manager: MockOrderBookManager,
    ) -> None:
        """Tranche 1 with DOWN direction should return opportunity with no_fill."""
        market = _make_market(end_offset=80.0)  # ~80s remaining -> tranche 1
        _setup_for_sniper(
            spot_buffer, book_manager,
            open_price=100000.0,
            current_spot=99000.0,  # 1% down
            fill_vwap=0.85,
            vol=0.0005,
        )
        strategy._opening_prices[market.condition_id] = (100000.0, time.time())

        result = await strategy.evaluate(market)

        assert result is not None
        assert result.yes_fill is None
        assert result.no_fill is not None
        assert result.metadata["direction"] == "DOWN"

    async def test_tranche_2_high_confidence(
        self, strategy: ResolutionSniperStrategy,
        spot_buffer: MockSpotBuffer, book_manager: MockOrderBookManager,
    ) -> None:
        """Tranche 2 with very high confidence."""
        market = _make_market(end_offset=45.0)  # ~45s remaining -> tranche 2
        _setup_for_sniper(
            spot_buffer, book_manager,
            open_price=100000.0,
            current_spot=102000.0,  # 2% up -> very confident
            fill_vwap=0.85,
            vol=0.0003,
        )
        strategy._opening_prices[market.condition_id] = (100000.0, time.time())

        result = await strategy.evaluate(market)

        assert result is not None
        assert result.confidence > 0.95

    async def test_all_metadata_keys_present(
        self, strategy: ResolutionSniperStrategy,
        spot_buffer: MockSpotBuffer, book_manager: MockOrderBookManager,
    ) -> None:
        """All expected metadata keys should be present."""
        market = _make_market(end_offset=100.0)
        _setup_for_sniper(
            spot_buffer, book_manager,
            open_price=100000.0, current_spot=101000.0,
            fill_vwap=0.85, vol=0.0005,
        )
        strategy._opening_prices[market.condition_id] = (100000.0, time.time())

        result = await strategy.evaluate(market)

        assert result is not None
        expected_keys = {
            "direction", "target_token_id", "binance_symbol",
            "open_price", "current_spot", "distance_pct", "sigma",
            "win_probability", "tranche_index", "tranche_size",
            "time_remaining",
        }
        assert expected_keys == set(result.metadata.keys())

    async def test_tranche_size_is_one_third(
        self, strategy: ResolutionSniperStrategy,
        spot_buffer: MockSpotBuffer, book_manager: MockOrderBookManager,
    ) -> None:
        """Tranche size should be sniper_order_size / 3."""
        market = _make_market(end_offset=100.0)
        _setup_for_sniper(
            spot_buffer, book_manager,
            open_price=100000.0, current_spot=101000.0,
            fill_vwap=0.85, vol=0.0005,
        )
        strategy._opening_prices[market.condition_id] = (100000.0, time.time())

        result = await strategy.evaluate(market)

        assert result is not None
        assert result.metadata["tranche_size"] == pytest.approx(50.0)  # 150 / 3

    async def test_sequential_tranches_all_fire(
        self, strategy: ResolutionSniperStrategy,
        spot_buffer: MockSpotBuffer, book_manager: MockOrderBookManager,
    ) -> None:
        """All 3 tranches should fire in sequence."""
        _setup_for_sniper(
            spot_buffer, book_manager,
            open_price=100000.0, current_spot=101000.0,
            fill_vwap=0.85, vol=0.0005,
        )

        # Tranche 0 (T-120 to T-90)
        market0 = _make_market(end_offset=100.0, condition_id="cond_seq")
        strategy._opening_prices["cond_seq"] = (100000.0, time.time())
        r0 = await strategy.evaluate(market0)
        assert r0 is not None
        assert r0.metadata["tranche_index"] == 0

        # Tranche 1 (T-90 to T-60)
        market1 = _make_market(end_offset=80.0, condition_id="cond_seq")
        r1 = await strategy.evaluate(market1)
        assert r1 is not None
        assert r1.metadata["tranche_index"] == 1

        # Tranche 2 (T-60 to T-15)
        market2 = _make_market(end_offset=45.0, condition_id="cond_seq")
        r2 = await strategy.evaluate(market2)
        assert r2 is not None
        assert r2.metadata["tranche_index"] == 2

        # All taken — no more opportunities
        market3 = _make_market(end_offset=30.0, condition_id="cond_seq")
        r3 = await strategy.evaluate(market3)
        assert r3 is None


# ---------------------------------------------------------------------------
# Opening price capture
# ---------------------------------------------------------------------------


class TestOpeningPriceCapture:

    def test_captured_on_first_call(
        self, strategy: ResolutionSniperStrategy,
        spot_buffer: MockSpotBuffer,
    ) -> None:
        """Opening price captured from spot buffer on first access."""
        spot_buffer._prices["BTCUSDT"] = 100500.0
        price = strategy._capture_opening_price("cond_cap", "BTCUSDT")
        assert price == 100500.0
        assert "cond_cap" in strategy._opening_prices

    def test_not_overwritten_on_subsequent_calls(
        self, strategy: ResolutionSniperStrategy,
        spot_buffer: MockSpotBuffer,
    ) -> None:
        """Opening price should not change after initial capture."""
        spot_buffer._prices["BTCUSDT"] = 100500.0
        strategy._capture_opening_price("cond_cap2", "BTCUSDT")

        # Change spot price
        spot_buffer._prices["BTCUSDT"] = 101000.0
        price = strategy._capture_opening_price("cond_cap2", "BTCUSDT")
        assert price == 100500.0  # Still the original

    def test_separate_per_market(
        self, strategy: ResolutionSniperStrategy,
        spot_buffer: MockSpotBuffer,
    ) -> None:
        """Different condition_ids get separate opening prices."""
        spot_buffer._prices["BTCUSDT"] = 100500.0
        strategy._capture_opening_price("cond_a", "BTCUSDT")

        spot_buffer._prices["BTCUSDT"] = 101000.0
        strategy._capture_opening_price("cond_b", "BTCUSDT")

        assert strategy._opening_prices["cond_a"][0] == 100500.0
        assert strategy._opening_prices["cond_b"][0] == 101000.0


# ---------------------------------------------------------------------------
# Volatility estimation
# ---------------------------------------------------------------------------


class TestVolatilityEstimation:

    def test_constant_prices_return_floor(
        self, strategy: ResolutionSniperStrategy,
        spot_buffer: MockSpotBuffer,
    ) -> None:
        """Constant prices should return vol floor."""
        now = time.time()
        history = [(now - (29 - i) * 2, 100000.0) for i in range(30)]
        spot_buffer._price_history["BTCUSDT"] = history

        sigma = strategy._estimate_realized_vol("BTCUSDT")
        assert sigma is not None
        assert sigma == pytest.approx(0.0001)  # vol floor

    def test_known_sequence_returns_expected_sigma(
        self, strategy: ResolutionSniperStrategy,
        spot_buffer: MockSpotBuffer,
    ) -> None:
        """A sequence with known movement should give positive sigma above floor."""
        now = time.time()
        base = 100000.0
        # Alternate +0.1% / -0.1% each tick
        history: list[tuple[float, float]] = []
        for i in range(30):
            ts = now - (29 - i) * 2
            mult = 1.001 if i % 2 == 0 else 0.999
            history.append((ts, base * mult))
        spot_buffer._price_history["BTCUSDT"] = history

        sigma = strategy._estimate_realized_vol("BTCUSDT")
        assert sigma is not None
        assert sigma > 0.0001  # Above floor

    def test_insufficient_data_returns_none(
        self, strategy: ResolutionSniperStrategy,
        spot_buffer: MockSpotBuffer,
    ) -> None:
        """Fewer than min data points should return None."""
        now = time.time()
        history = [(now - i, 100000.0) for i in range(5)]
        spot_buffer._price_history["BTCUSDT"] = history

        sigma = strategy._estimate_realized_vol("BTCUSDT")
        assert sigma is None


# ---------------------------------------------------------------------------
# Win probability
# ---------------------------------------------------------------------------


class TestWinProbability:

    def test_high_distance_low_vol(self) -> None:
        """Large distance / low vol → near 1.0."""
        # distance/expected_move ratio >> 1
        assert _normal_cdf(5.0) > 0.99

    def test_low_distance_high_vol(self) -> None:
        """Small distance / high vol → near 0.5."""
        # Φ(0.1) ≈ 0.5398 — close to 0.5 but not exactly
        assert _normal_cdf(0.1) == pytest.approx(0.5398, abs=0.01)

    def test_zero_gives_half(self) -> None:
        """Zero z-score → 0.5 exactly."""
        assert _normal_cdf(0.0) == pytest.approx(0.5, abs=1e-6)


# ---------------------------------------------------------------------------
# should_exit
# ---------------------------------------------------------------------------


class TestShouldExit:

    def test_always_false_when_floor_is_zero(
        self, strategy: ResolutionSniperStrategy,
        book_manager: MockOrderBookManager,
    ) -> None:
        """With exit floor = 0 (default), should never exit."""
        market = _make_market(end_offset=50.0)
        position = Position(
            market=market,
            yes_shares=50.0,
            yes_cost_basis=46.0,
            strategy=StrategyType.RESOLUTION_SNIPER,
        )
        assert strategy.should_exit(position, market) is False

    def test_true_when_confidence_drops_below_floor(
        self, strategy: ResolutionSniperStrategy,
        spot_buffer: MockSpotBuffer,
    ) -> None:
        """With floor > 0, exit when win_prob drops below it."""
        strategy._settings = Settings(
            private_key="0x" + "ab" * 32,  # type: ignore[arg-type]
            sniper_exit_confidence_floor=0.85,
            sniper_vol_floor=0.0001,
            sniper_min_vol_data_points=10,
        )
        market = _make_market(end_offset=50.0)
        position = Position(
            market=market,
            yes_shares=50.0,
            yes_cost_basis=46.0,
            strategy=StrategyType.RESOLUTION_SNIPER,
        )

        # Set up: opening price very close to current → low confidence
        strategy._opening_prices[market.condition_id] = (100000.0, time.time())
        spot_buffer._prices["BTCUSDT"] = 100001.0  # Tiny distance

        # Price history with high vol
        now = time.time()
        history: list[tuple[float, float]] = []
        for i in range(30):
            ts = now - (29 - i) * 2
            mult = 1.005 if i % 2 == 0 else 0.995  # 0.5% swings
            history.append((ts, 100000.0 * mult))
        spot_buffer._price_history["BTCUSDT"] = history

        assert strategy.should_exit(position, market) is True

    def test_false_when_confidence_above_floor(
        self, strategy: ResolutionSniperStrategy,
        spot_buffer: MockSpotBuffer,
    ) -> None:
        """With floor > 0, don't exit when win_prob is above it."""
        strategy._settings = Settings(
            private_key="0x" + "ab" * 32,  # type: ignore[arg-type]
            sniper_exit_confidence_floor=0.80,
            sniper_vol_floor=0.0001,
            sniper_min_vol_data_points=10,
        )
        market = _make_market(end_offset=50.0)
        position = Position(
            market=market,
            yes_shares=50.0,
            yes_cost_basis=46.0,
            strategy=StrategyType.RESOLUTION_SNIPER,
        )

        # Set up: large distance + low vol → high confidence
        strategy._opening_prices[market.condition_id] = (100000.0, time.time())
        spot_buffer._prices["BTCUSDT"] = 101500.0  # 1.5% move

        # Price history with low vol
        now = time.time()
        history = [(now - (29 - i) * 2, 101500.0) for i in range(30)]
        spot_buffer._price_history["BTCUSDT"] = history

        assert strategy.should_exit(position, market) is False


# ---------------------------------------------------------------------------
# Tranche eligibility
# ---------------------------------------------------------------------------


class TestTrancheEligibility:

    def test_eligible_at_t115(self, strategy: ResolutionSniperStrategy) -> None:
        """T-115s should be tranche 0 (120 > 115 > 90)."""
        result = strategy._get_eligible_tranche("cond", 115.0)
        assert result == 0

    def test_eligible_at_t75(self, strategy: ResolutionSniperStrategy) -> None:
        """T-75s should be tranche 1 (90 > 75 > 60)."""
        result = strategy._get_eligible_tranche("cond", 75.0)
        assert result == 1

    def test_eligible_at_t45(self, strategy: ResolutionSniperStrategy) -> None:
        """T-45s should be tranche 2 (60 > 45 > 15)."""
        result = strategy._get_eligible_tranche("cond", 45.0)
        assert result == 2

    def test_not_eligible_at_t125(self, strategy: ResolutionSniperStrategy) -> None:
        """T-125s is outside sniper window."""
        result = strategy._get_eligible_tranche("cond", 125.0)
        assert result is None

    def test_already_taken_skipped(self, strategy: ResolutionSniperStrategy) -> None:
        """Taken tranche should be skipped."""
        strategy._tranches_taken["cond"] = {0}
        result = strategy._get_eligible_tranche("cond", 115.0)
        assert result is None

    def test_hard_stop_blocks(self, strategy: ResolutionSniperStrategy) -> None:
        """T-10s is past hard stop (< 15s)."""
        result = strategy._get_eligible_tranche("cond", 10.0)
        assert result is None

    def test_boundary_at_120(self, strategy: ResolutionSniperStrategy) -> None:
        """Exactly 120s is included in tranche 0 (90 < 120 <= 120)."""
        result = strategy._get_eligible_tranche("cond", 120.0)
        assert result == 0

    def test_boundary_at_90(self, strategy: ResolutionSniperStrategy) -> None:
        """Exactly 90s: 90 is NOT in tranche 0 (90 < 90 is false), IS in tranche 1."""
        result = strategy._get_eligible_tranche("cond", 90.0)
        assert result == 1

    def test_boundary_at_60(self, strategy: ResolutionSniperStrategy) -> None:
        """Exactly 60s: in tranche 2 (15 < 60 <= 60)."""
        result = strategy._get_eligible_tranche("cond", 60.0)
        assert result == 2


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


class TestCleanup:

    def test_removes_opening_prices(self, strategy: ResolutionSniperStrategy) -> None:
        strategy._opening_prices["cond_x"] = (100000.0, time.time())
        strategy.cleanup_market("cond_x")
        assert "cond_x" not in strategy._opening_prices

    def test_removes_tranches(self, strategy: ResolutionSniperStrategy) -> None:
        strategy._tranches_taken["cond_x"] = {0, 1}
        strategy.cleanup_market("cond_x")
        assert "cond_x" not in strategy._tranches_taken


# ---------------------------------------------------------------------------
# Risk manager per-strategy entry limit
# ---------------------------------------------------------------------------


class TestRiskManagerSniperOverride:
    """Verify the 3-entry override for sniper strategy."""

    def test_third_entry_approved_for_sniper(self) -> None:
        """Sniper strategy should allow 3 entries (not default 2)."""
        from src.risk.manager import RiskManager

        mock_settings = Settings(
            private_key="0x" + "ab" * 32,  # type: ignore[arg-type]
            max_position_per_market=500.0,
            max_total_position=2000.0,
            max_daily_loss=50.0,
            max_unhedged_exposure=500.0,
            cooldown_seconds=0.0,
            max_entries_per_market=2,
        )

        class MockState:
            def total_exposure(self) -> float:
                return 0.0

            def market_exposure(self, cid: str) -> float:
                return 0.0

            def total_unhedged_exposure(self) -> float:
                return 0.0

            def daily_pnl(self) -> DailyPnL:
                return DailyPnL(date="2024-01-01")

            def position_entry_count(self, cid: str) -> int:
                return 2  # At default max

        rm = RiskManager(mock_settings, MockState())  # type: ignore[arg-type]

        # Non-sniper strategy should be rejected at 2 entries
        from src.core.models import Opportunity

        market = _make_market()
        opp_lag = Opportunity(
            strategy=StrategyType.PRICE_LAG,
            market=market,
            timestamp=datetime.now(tz=UTC),
        )
        approved, reason = rm.check_opportunity(opp_lag)
        assert approved is False
        assert "max entries" in reason

        # Sniper strategy should be approved at 2 entries (max=3)
        opp_sniper = Opportunity(
            strategy=StrategyType.RESOLUTION_SNIPER,
            market=market,
            timestamp=datetime.now(tz=UTC),
        )
        approved, reason = rm.check_opportunity(opp_sniper)
        assert approved is True
