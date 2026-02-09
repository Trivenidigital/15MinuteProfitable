"""Unit tests for CEX perp hedging (Binance Futures integration)."""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from unittest.mock import MagicMock
from urllib.parse import urlencode

import pytest

from src.core.models import (
    FillEstimate,
    Market,
    Opportunity,
    OrderStatus,
    StrategyType,
    TradeOrder,
)
from src.execution.binance_futures import (
    QTY_PRECISION,
    BinanceFuturesClient,
)
from src.execution.hedge_manager import HedgeManager

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def market() -> Market:
    return Market(
        condition_id="test-cid-001",
        slug="btc-15min-up-down",
        question="Will BTC go up?",
        yes_token_id="yes-token-001",
        no_token_id="no-token-001",
        start_time=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        end_time=datetime(2026, 1, 1, 12, 15, tzinfo=UTC),
        asset="BTC",
    )


@pytest.fixture
def binance_client() -> BinanceFuturesClient:
    return BinanceFuturesClient(
        api_key="test-api-key",
        api_secret="test-api-secret",
        testnet=True,
    )


# ---------------------------------------------------------------------------
# HMAC Signing Tests
# ---------------------------------------------------------------------------

class TestHMACSigning:
    def test_sign_request_produces_correct_signature(
        self, binance_client: BinanceFuturesClient
    ) -> None:
        params = {"symbol": "BTCUSDT", "side": "SELL", "timestamp": 1700000000000}
        signature = binance_client._sign_request(params)

        # Verify manually
        query = urlencode(params)
        expected = hmac.new(
            b"test-api-secret", query.encode(), hashlib.sha256
        ).hexdigest()
        assert signature == expected

    def test_sign_request_deterministic(
        self, binance_client: BinanceFuturesClient
    ) -> None:
        params = {"symbol": "ETHUSDT", "quantity": "0.5", "timestamp": 123}
        sig1 = binance_client._sign_request(params)
        sig2 = binance_client._sign_request(params)
        assert sig1 == sig2


# ---------------------------------------------------------------------------
# Open Hedge Tests
# ---------------------------------------------------------------------------

class TestOpenHedge:
    @pytest.mark.asyncio
    async def test_dry_run_returns_simulated(
        self, binance_client: BinanceFuturesClient
    ) -> None:
        result = await binance_client.open_hedge(
            symbol="BTCUSDT", side="SELL", quantity=0.001, dry_run=True
        )
        assert result.status == "SIMULATED"
        assert result.symbol == "BTCUSDT"
        assert result.side == "SELL"
        assert result.quantity == 0.001
        assert result.order_id.startswith("SIM-")

    @pytest.mark.asyncio
    async def test_zero_quantity_returns_failed(
        self, binance_client: BinanceFuturesClient
    ) -> None:
        result = await binance_client.open_hedge(
            symbol="BTCUSDT", side="SELL", quantity=0.0, dry_run=True
        )
        assert result.status == "FAILED"
        assert result.quantity == 0.0

    @pytest.mark.asyncio
    async def test_quantity_rounded_to_precision(
        self, binance_client: BinanceFuturesClient
    ) -> None:
        result = await binance_client.open_hedge(
            symbol="BTCUSDT", side="BUY", quantity=0.00036789, dry_run=True
        )
        # BTCUSDT has 3 decimal places
        assert result.quantity == round(0.00036789, QTY_PRECISION["BTCUSDT"])

    @pytest.mark.asyncio
    async def test_sol_quantity_precision(
        self, binance_client: BinanceFuturesClient
    ) -> None:
        result = await binance_client.open_hedge(
            symbol="SOLUSDT", side="SELL", quantity=1.567, dry_run=True
        )
        # SOLUSDT has 1 decimal place
        assert result.quantity == round(1.567, QTY_PRECISION["SOLUSDT"])


# ---------------------------------------------------------------------------
# Hedge Side Mapping Tests
# ---------------------------------------------------------------------------

class TestHedgeSideMapping:
    def test_no_buy_maps_to_short(self, market: Market) -> None:
        """Buying NO on Polymarket (betting DOWN) -> SHORT on Binance."""
        settings = _make_settings()
        manager = HedgeManager(
            settings=settings,
            binance_client=MagicMock(),
            state_manager=MagicMock(),
        )
        opp = Opportunity(
            strategy=StrategyType.FADE_PANIC,
            market=market,
            timestamp=datetime.now(tz=UTC),
            no_fill=FillEstimate(
                filled_size=50, total_cost=25, vwap=0.50,
                worst_price=0.52, best_price=0.50, levels_consumed=1,
                sufficient_liquidity=True,
            ),
            metadata={"direction": "DOWN", "target_token_id": "no-token-001"},
        )
        side = manager._determine_hedge_side(opp)
        assert side == "SELL"

    def test_yes_buy_maps_to_long(self, market: Market) -> None:
        """Buying YES on Polymarket (betting UP) -> LONG on Binance."""
        settings = _make_settings()
        manager = HedgeManager(
            settings=settings,
            binance_client=MagicMock(),
            state_manager=MagicMock(),
        )
        opp = Opportunity(
            strategy=StrategyType.FADE_PANIC,
            market=market,
            timestamp=datetime.now(tz=UTC),
            yes_fill=FillEstimate(
                filled_size=50, total_cost=25, vwap=0.50,
                worst_price=0.52, best_price=0.50, levels_consumed=1,
                sufficient_liquidity=True,
            ),
            metadata={"direction": "UP", "target_token_id": "yes-token-001"},
        )
        side = manager._determine_hedge_side(opp)
        assert side == "BUY"


# ---------------------------------------------------------------------------
# Quantity Calculation Tests
# ---------------------------------------------------------------------------

class TestQuantityCalculation:
    def test_quantity_at_69000_btc(self, market: Market) -> None:
        """$50 Polymarket bet at 0.5 hedge ratio -> ~0.00036 BTC."""
        settings = _make_settings(cex_hedge_ratio=0.5)
        manager = HedgeManager(
            settings=settings,
            binance_client=MagicMock(),
            state_manager=MagicMock(),
        )
        opp = Opportunity(
            strategy=StrategyType.FADE_PANIC,
            market=market,
            timestamp=datetime.now(tz=UTC),
        )
        fill = TradeOrder(
            token_id="no-token-001",
            side="BUY",
            price=0.50,
            size=100,  # 100 shares at $0.50 = $50 investment
            fill_price=0.50,
            fill_size=100,
            status=OrderStatus.FILLED,
        )
        qty = manager._calculate_quantity(opp, fill, spot_price=69000.0)
        expected = 0.5 * 50.0 / 69000.0  # ~0.000362
        assert abs(qty - expected) < 1e-8

    def test_quantity_at_3500_eth(self, market: Market) -> None:
        """$30 bet on ETH at 0.5 hedge ratio."""
        settings = _make_settings(cex_hedge_ratio=0.5)
        manager = HedgeManager(
            settings=settings,
            binance_client=MagicMock(),
            state_manager=MagicMock(),
        )
        opp = Opportunity(
            strategy=StrategyType.FADE_PANIC,
            market=market,
            timestamp=datetime.now(tz=UTC),
        )
        fill = TradeOrder(
            token_id="yes-token-001",
            side="BUY",
            price=0.60,
            size=50,  # 50 shares at $0.60 = $30
            fill_price=0.60,
            fill_size=50,
            status=OrderStatus.FILLED,
        )
        qty = manager._calculate_quantity(opp, fill, spot_price=3500.0)
        expected = 0.5 * 30.0 / 3500.0
        assert abs(qty - expected) < 1e-8

    def test_zero_spot_returns_zero(self, market: Market) -> None:
        settings = _make_settings()
        manager = HedgeManager(
            settings=settings,
            binance_client=MagicMock(),
            state_manager=MagicMock(),
        )
        opp = Opportunity(
            strategy=StrategyType.FADE_PANIC,
            market=market,
            timestamp=datetime.now(tz=UTC),
        )
        fill = TradeOrder(
            token_id="no-token-001",
            side="BUY",
            price=0.50,
            size=100,
            fill_price=0.50,
            fill_size=100,
            status=OrderStatus.FILLED,
        )
        assert manager._calculate_quantity(opp, fill, spot_price=0.0) == 0.0


# ---------------------------------------------------------------------------
# HedgeManager.hedge_trade Tests
# ---------------------------------------------------------------------------

class TestHedgeManagerDecisions:
    def test_skips_non_hedgeable_strategy(self, market: Market) -> None:
        """Arbitrage strategy should NOT be hedged."""
        settings = _make_settings()
        manager = HedgeManager(
            settings=settings,
            binance_client=MagicMock(),
            state_manager=MagicMock(),
        )
        opp = Opportunity(
            strategy=StrategyType.ARBITRAGE,
            market=market,
            timestamp=datetime.now(tz=UTC),
            yes_fill=FillEstimate(
                filled_size=50, total_cost=25, vwap=0.50,
                worst_price=0.52, best_price=0.50, levels_consumed=1,
                sufficient_liquidity=True,
            ),
            no_fill=FillEstimate(
                filled_size=50, total_cost=25, vwap=0.50,
                worst_price=0.52, best_price=0.50, levels_consumed=1,
                sufficient_liquidity=True,
            ),
            metadata={"direction": "UP"},
        )
        assert manager._should_hedge(opp) is False

    def test_skips_price_lag_by_default(self, market: Market) -> None:
        """price_lag is NOT in default hedgeable strategies."""
        settings = _make_settings()
        manager = HedgeManager(
            settings=settings,
            binance_client=MagicMock(),
            state_manager=MagicMock(),
        )
        opp = Opportunity(
            strategy=StrategyType.PRICE_LAG,
            market=market,
            timestamp=datetime.now(tz=UTC),
            yes_fill=FillEstimate(
                filled_size=50, total_cost=25, vwap=0.50,
                worst_price=0.52, best_price=0.50, levels_consumed=1,
                sufficient_liquidity=True,
            ),
            metadata={"direction": "UP"},
        )
        assert manager._should_hedge(opp) is False

    def test_allows_fade_panic(self, market: Market) -> None:
        """fade_panic IS in default hedgeable strategies."""
        settings = _make_settings()
        manager = HedgeManager(
            settings=settings,
            binance_client=MagicMock(),
            state_manager=MagicMock(),
        )
        opp = Opportunity(
            strategy=StrategyType.FADE_PANIC,
            market=market,
            timestamp=datetime.now(tz=UTC),
            no_fill=FillEstimate(
                filled_size=50, total_cost=25, vwap=0.50,
                worst_price=0.52, best_price=0.50, levels_consumed=1,
                sufficient_liquidity=True,
            ),
            metadata={"direction": "DOWN"},
        )
        assert manager._should_hedge(opp) is True

    def test_skips_when_disabled(self, market: Market) -> None:
        """Should skip when enable_cex_hedging is False."""
        settings = _make_settings(enable_cex_hedging=False)
        manager = HedgeManager(
            settings=settings,
            binance_client=MagicMock(),
            state_manager=MagicMock(),
        )
        opp = Opportunity(
            strategy=StrategyType.FADE_PANIC,
            market=market,
            timestamp=datetime.now(tz=UTC),
            no_fill=FillEstimate(
                filled_size=50, total_cost=25, vwap=0.50,
                worst_price=0.52, best_price=0.50, levels_consumed=1,
                sufficient_liquidity=True,
            ),
            metadata={"direction": "DOWN"},
        )
        assert manager._should_hedge(opp) is False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_settings(**overrides) -> MagicMock:
    """Create a mock Settings with hedging defaults."""
    s = MagicMock()
    s.enable_cex_hedging = overrides.get("enable_cex_hedging", True)
    s.binance_futures_testnet = overrides.get("binance_futures_testnet", True)
    s.cex_hedge_ratio = overrides.get("cex_hedge_ratio", 0.5)
    s.cex_hedge_leverage = overrides.get("cex_hedge_leverage", 1)
    s.cex_hedgeable_strategies = overrides.get(
        "cex_hedgeable_strategies", "fade_panic,resolution_sniper"
    )
    s.dry_run = overrides.get("dry_run", True)
    return s
