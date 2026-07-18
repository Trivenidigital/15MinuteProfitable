"""Tests for the LiquidityQuoter (maker-side rewards farming)."""

from __future__ import annotations

import os

# Set the required environment variable BEFORE any Settings import
os.environ["BOT_PRIVATE_KEY"] = "0x" + "a" * 64

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import Settings
from src.core.models import Market, OrderBook, OrderBookLevel, StrategyType
from src.data.spot_buffer import SpotMovement
from src.data.trade_db import TradeDatabase
from src.strategy.lp_quoter import LiquidityQuoter, MarketQuotes

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_NOW = datetime.now(tz=UTC)


def _make_market(
    condition_id: str = "cond_lp_1",
    asset: str = "BTC",
    end_offset: float = 600.0,
) -> Market:
    return Market(
        condition_id=condition_id,
        slug=f"{asset.lower()}-updown-15m-test",
        question=f"Will {asset} go up?",
        yes_token_id=f"{condition_id}_YES",
        no_token_id=f"{condition_id}_NO",
        start_time=datetime.fromtimestamp(_NOW.timestamp() - 300, tz=UTC),
        end_time=datetime.fromtimestamp(_NOW.timestamp() + end_offset, tz=UTC),
        asset=asset,
        neg_risk=False,
    )


def _make_book(token_id: str, best_bid: float, best_ask: float) -> OrderBook:
    return OrderBook(
        token_id=token_id,
        bids=[OrderBookLevel(price=best_bid, size=100.0)],
        asks=[OrderBookLevel(price=best_ask, size=100.0)],
    )


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        private_key="0x" + "a" * 64,  # type: ignore[arg-type]
        dry_run=True,
        enable_lp_quoter=True,
        lp_assets="BTC",
        lp_order_size=20.0,
        lp_quote_offset=0.01,
        lp_reprice_tolerance=0.01,
        lp_quote_stop_seconds=90.0,
        lp_min_mid=0.15,
        lp_max_mid=0.85,
        lp_max_inventory_shares=60.0,
    )


def _make_quoter(
    settings: Settings,
    books: dict[str, OrderBook],
    markets: list[Market],
    trade_db: TradeDatabase | None = None,
    spot_movement: SpotMovement | None = None,
    breaker_active: bool = False,
) -> LiquidityQuoter:
    book_manager = MagicMock()
    book_manager.get_book.side_effect = lambda tid: books.get(tid)
    book_manager.is_stale.return_value = False

    market_manager = MagicMock()
    market_manager.active_markets = markets

    spot_buffer = MagicMock()
    spot_buffer.detect_movement.return_value = spot_movement

    executor = MagicMock()
    executor.cancel_order = AsyncMock(return_value=True)
    executor.sign_order = AsyncMock()
    executor.submit_order = AsyncMock()
    executor.check_order_status = AsyncMock()

    state_manager = MagicMock()
    state_manager.record_trade = AsyncMock()

    risk_manager = MagicMock()
    risk_manager.is_circuit_breaker_active.return_value = breaker_active

    return LiquidityQuoter(
        settings=settings,
        book_manager=book_manager,
        market_manager=market_manager,
        spot_buffer=spot_buffer,
        executor=executor,
        state_manager=state_manager,
        risk_manager=risk_manager,
        trade_db=trade_db,
    )


# ---------------------------------------------------------------------------
# Quote placement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_places_two_sided_quotes(settings: Settings) -> None:
    market = _make_market()
    books = {
        market.yes_token_id: _make_book(market.yes_token_id, 0.49, 0.51),
        market.no_token_id: _make_book(market.no_token_id, 0.49, 0.51),
    }
    quoter = _make_quoter(settings, books, [market])

    await quoter._reconcile_cycle()

    mq = quoter._quotes[market.condition_id]
    assert mq.yes_quote is not None
    assert mq.no_quote is not None
    # mid = 0.50 -> YES bid at 0.49, NO bid at (1 - 0.50) - 0.01 = 0.49
    assert mq.yes_quote.price == pytest.approx(0.49)
    assert mq.no_quote.price == pytest.approx(0.49)


@pytest.mark.asyncio
async def test_skips_non_configured_assets(settings: Settings) -> None:
    market = _make_market(asset="SOL")
    books = {
        market.yes_token_id: _make_book(market.yes_token_id, 0.49, 0.51),
        market.no_token_id: _make_book(market.no_token_id, 0.49, 0.51),
    }
    quoter = _make_quoter(settings, books, [market])

    await quoter._reconcile_cycle()

    assert market.condition_id not in quoter._quotes


# ---------------------------------------------------------------------------
# Quote-pulling guards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pulls_quotes_near_resolution(settings: Settings) -> None:
    market = _make_market(end_offset=60.0)  # inside the 90s stop window
    books = {
        market.yes_token_id: _make_book(market.yes_token_id, 0.49, 0.51),
        market.no_token_id: _make_book(market.no_token_id, 0.49, 0.51),
    }
    quoter = _make_quoter(settings, books, [market])

    await quoter._reconcile_cycle()

    mq = quoter._quotes[market.condition_id]
    assert mq.yes_quote is None
    assert mq.no_quote is None


@pytest.mark.asyncio
async def test_spot_guard_pulls_quotes(settings: Settings) -> None:
    market = _make_market()
    books = {
        market.yes_token_id: _make_book(market.yes_token_id, 0.49, 0.51),
        market.no_token_id: _make_book(market.no_token_id, 0.49, 0.51),
    }
    movement = SpotMovement(
        symbol="BTCUSDT",
        direction="UP",
        change_pct=0.002,
        start_price=100000.0,
        end_price=100200.0,
        window_seconds=10,
        timestamp=_NOW.timestamp(),
    )
    quoter = _make_quoter(settings, books, [market], spot_movement=movement)

    await quoter._reconcile_cycle()

    mq = quoter._quotes[market.condition_id]
    assert mq.yes_quote is None
    assert mq.no_quote is None


@pytest.mark.asyncio
async def test_extreme_mid_no_quotes(settings: Settings) -> None:
    market = _make_market()
    books = {
        market.yes_token_id: _make_book(market.yes_token_id, 0.93, 0.95),
        market.no_token_id: _make_book(market.no_token_id, 0.05, 0.07),
    }
    quoter = _make_quoter(settings, books, [market])

    await quoter._reconcile_cycle()

    mq = quoter._quotes[market.condition_id]
    assert mq.yes_quote is None
    assert mq.no_quote is None


@pytest.mark.asyncio
async def test_circuit_breaker_pulls_quotes(settings: Settings) -> None:
    market = _make_market()
    books = {
        market.yes_token_id: _make_book(market.yes_token_id, 0.49, 0.51),
        market.no_token_id: _make_book(market.no_token_id, 0.49, 0.51),
    }
    quoter = _make_quoter(settings, books, [market], breaker_active=True)

    await quoter._reconcile_cycle()

    mq = quoter._quotes[market.condition_id]
    assert mq.yes_quote is None
    assert mq.no_quote is None


@pytest.mark.asyncio
async def test_market_rollover_cancels_quotes(settings: Settings) -> None:
    market = _make_market()
    books = {
        market.yes_token_id: _make_book(market.yes_token_id, 0.49, 0.51),
        market.no_token_id: _make_book(market.no_token_id, 0.49, 0.51),
    }
    quoter = _make_quoter(settings, books, [market])
    await quoter._reconcile_cycle()
    assert quoter._quotes[market.condition_id].yes_quote is not None

    # Market disappears (rollover)
    quoter._markets.active_markets = []
    await quoter._reconcile_cycle()

    assert market.condition_id not in quoter._quotes


# ---------------------------------------------------------------------------
# Repricing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reprices_when_mid_moves(settings: Settings) -> None:
    market = _make_market()
    books = {
        market.yes_token_id: _make_book(market.yes_token_id, 0.49, 0.51),
        market.no_token_id: _make_book(market.no_token_id, 0.49, 0.51),
    }
    quoter = _make_quoter(settings, books, [market])
    await quoter._reconcile_cycle()
    assert quoter._quotes[market.condition_id].yes_quote.price == pytest.approx(0.49)

    # Mid moves 0.50 -> 0.56
    books[market.yes_token_id] = _make_book(market.yes_token_id, 0.55, 0.57)
    books[market.no_token_id] = _make_book(market.no_token_id, 0.43, 0.45)
    await quoter._reconcile_cycle()

    mq = quoter._quotes[market.condition_id]
    assert mq.yes_quote.price == pytest.approx(0.55)
    assert mq.no_quote.price == pytest.approx(0.43)


@pytest.mark.asyncio
async def test_keeps_quote_within_tolerance(settings: Settings) -> None:
    market = _make_market()
    books = {
        market.yes_token_id: _make_book(market.yes_token_id, 0.49, 0.51),
        market.no_token_id: _make_book(market.no_token_id, 0.49, 0.51),
    }
    quoter = _make_quoter(settings, books, [market])
    await quoter._reconcile_cycle()
    original = quoter._quotes[market.condition_id].yes_quote

    # Tiny mid move: 0.500 -> 0.503, desired price still rounds to 0.49
    books[market.yes_token_id] = _make_book(market.yes_token_id, 0.493, 0.513)
    await quoter._reconcile_cycle()

    assert quoter._quotes[market.condition_id].yes_quote is original


# ---------------------------------------------------------------------------
# Fills and inventory
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_fill_recorded(settings: Settings) -> None:
    market = _make_market()
    books = {
        market.yes_token_id: _make_book(market.yes_token_id, 0.49, 0.51),
        market.no_token_id: _make_book(market.no_token_id, 0.49, 0.51),
    }
    quoter = _make_quoter(settings, books, [market])
    await quoter._reconcile_cycle()

    # Ask crosses down into our YES bid -> simulated fill
    books[market.yes_token_id] = _make_book(market.yes_token_id, 0.47, 0.49)
    await quoter._reconcile_cycle()

    mq = quoter._quotes[market.condition_id]
    assert mq.yes_filled_shares == pytest.approx(20.0)
    quoter._state.record_trade.assert_awaited()
    opp = quoter._state.record_trade.await_args.args[0]
    assert opp.strategy == StrategyType.LP_QUOTER
    assert opp.total_fees == 0.0


@pytest.mark.asyncio
async def test_inventory_cap_suppresses_loaded_side(settings: Settings) -> None:
    market = _make_market()
    books = {
        market.yes_token_id: _make_book(market.yes_token_id, 0.49, 0.51),
        market.no_token_id: _make_book(market.no_token_id, 0.49, 0.51),
    }
    quoter = _make_quoter(settings, books, [market])

    # Pre-load net YES inventory at the cap
    mq = MarketQuotes(market=market)
    mq.yes_filled_shares = 60.0
    quoter._quotes[market.condition_id] = mq

    await quoter._reconcile_cycle()

    assert mq.yes_quote is None  # accumulating side suppressed
    assert mq.no_quote is not None  # reducing side still quoted


# ---------------------------------------------------------------------------
# Rewards sampling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reward_sample_saved(settings: Settings) -> None:
    market = _make_market()
    books = {
        market.yes_token_id: _make_book(market.yes_token_id, 0.49, 0.51),
        market.no_token_id: _make_book(market.no_token_id, 0.49, 0.51),
    }
    db = TradeDatabase(":memory:")
    quoter = _make_quoter(settings, books, [market], trade_db=db)

    await quoter._reconcile_cycle()  # places quotes and samples immediately

    scores = db.get_lp_daily_q_score(since_ts=0.0)
    assert "BTC" in scores
    # Symmetric two-sided book at equal distance: q_score = q_bid = q_ask
    expected = ((0.035 - 0.01) / 0.035) ** 2 * 20.0
    assert scores["BTC"] == pytest.approx(expected, rel=1e-6)
    db.close()


@pytest.mark.asyncio
async def test_shutdown_cancels_all_quotes(settings: Settings) -> None:
    market = _make_market()
    books = {
        market.yes_token_id: _make_book(market.yes_token_id, 0.49, 0.51),
        market.no_token_id: _make_book(market.no_token_id, 0.49, 0.51),
    }
    quoter = _make_quoter(settings, books, [market])
    await quoter._reconcile_cycle()

    await quoter.cancel_all_quotes()

    mq = quoter._quotes[market.condition_id]
    assert mq.yes_quote is None
    assert mq.no_quote is None


def test_clamp_price_bounds() -> None:
    assert LiquidityQuoter._clamp_price(-0.5) == 0.01
    assert LiquidityQuoter._clamp_price(0.005) == 0.01
    assert LiquidityQuoter._clamp_price(0.5) == 0.5
    assert LiquidityQuoter._clamp_price(1.2) == 0.99
