"""Comprehensive tests for src.data.market_manager.MarketManager."""

from __future__ import annotations

import os

os.environ.setdefault("BOT_PRIVATE_KEY", "0x" + "ab" * 32)

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import Settings
from src.core.models import Market
from src.data.market_manager import MarketManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_market(
    asset: str = "BTC",
    condition_id: str | None = None,
    slug: str | None = None,
    yes_token_id: str | None = None,
    no_token_id: str | None = None,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    minutes_remaining: float = 10.0,
    neg_risk: bool = True,
) -> Market:
    """Create a Market with sensible defaults for testing.

    By default the market started 5 minutes ago and ends in
    ``minutes_remaining`` minutes from now.
    """
    now = datetime.now(tz=timezone.utc)
    if start_time is None:
        start_time = now - timedelta(minutes=5)
    if end_time is None:
        end_time = now + timedelta(minutes=minutes_remaining)
    if condition_id is None:
        condition_id = f"cond_{asset.lower()}_{int(end_time.timestamp())}"
    if slug is None:
        slug = f"{asset.lower()}-updown-15m-{int(start_time.timestamp())}"
    if yes_token_id is None:
        yes_token_id = f"yes_{asset.lower()}_{int(end_time.timestamp())}"
    if no_token_id is None:
        no_token_id = f"no_{asset.lower()}_{int(end_time.timestamp())}"

    return Market(
        condition_id=condition_id,
        slug=slug,
        question=f"Will {asset} go up in the next 15 minutes?",
        yes_token_id=yes_token_id,
        no_token_id=no_token_id,
        start_time=start_time,
        end_time=end_time,
        asset=asset,
        neg_risk=neg_risk,
    )


def _make_expired_market(
    asset: str = "BTC",
    condition_id: str | None = None,
) -> Market:
    """Create a market that has already expired."""
    now = datetime.now(tz=timezone.utc)
    return _make_market(
        asset=asset,
        condition_id=condition_id or f"cond_expired_{asset.lower()}",
        start_time=now - timedelta(minutes=20),
        end_time=now - timedelta(minutes=5),
    )


def _make_soon_expiring_market(
    asset: str = "BTC",
    seconds_until_expiry: float = 30.0,
) -> Market:
    """Create a market that expires within the rollover buffer."""
    now = datetime.now(tz=timezone.utc)
    return _make_market(
        asset=asset,
        start_time=now - timedelta(minutes=14),
        end_time=now + timedelta(seconds=seconds_until_expiry),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        private_key="0x" + "ab" * 32,
        markets=["BTC", "ETH"],
        dry_run=True,
    )


@pytest.fixture()
def mock_discovery() -> MagicMock:
    discovery = MagicMock()
    discovery.find_active_markets = AsyncMock(return_value=[])
    discovery.find_market_for_asset = AsyncMock(return_value=None)
    return discovery


@pytest.fixture()
def mock_clob_ws() -> MagicMock:
    ws = MagicMock()
    ws.subscribe = AsyncMock()
    ws.unsubscribe = AsyncMock()
    return ws


@pytest.fixture()
def mock_book_manager() -> MagicMock:
    mgr = MagicMock()
    mgr.ensure_book = MagicMock()
    mgr.get_book = MagicMock(return_value=None)
    mgr.remove_book = MagicMock()
    return mgr


@pytest.fixture()
def manager(
    settings: Settings,
    mock_discovery: MagicMock,
    mock_clob_ws: MagicMock,
    mock_book_manager: MagicMock,
) -> MarketManager:
    return MarketManager(
        settings=settings,
        discovery=mock_discovery,
        clob_ws=mock_clob_ws,
        book_manager=mock_book_manager,
    )


# ---------------------------------------------------------------------------
# initialize
# ---------------------------------------------------------------------------


class TestInitialize:
    """Tests for MarketManager.initialize()."""

    async def test_discovers_and_subscribes_markets(
        self,
        manager: MarketManager,
        mock_discovery: MagicMock,
        mock_clob_ws: MagicMock,
    ) -> None:
        """initialize discovers markets for all configured assets and
        subscribes to their token IDs on the WebSocket."""
        btc_market = _make_market(asset="BTC")
        eth_market = _make_market(asset="ETH")
        mock_discovery.find_active_markets.return_value = [btc_market, eth_market]

        result = await manager.initialize()

        assert len(result) == 2
        assert btc_market in result
        assert eth_market in result

        # Verify discovery was called with configured assets
        mock_discovery.find_active_markets.assert_awaited_once_with(
            assets=["BTC", "ETH"],
        )

        # Verify subscription includes all 4 token IDs
        mock_clob_ws.subscribe.assert_awaited_once()
        subscribed_ids = mock_clob_ws.subscribe.call_args[0][0]
        assert btc_market.yes_token_id in subscribed_ids
        assert btc_market.no_token_id in subscribed_ids
        assert eth_market.yes_token_id in subscribed_ids
        assert eth_market.no_token_id in subscribed_ids

    async def test_no_markets_found_returns_empty(
        self,
        manager: MarketManager,
        mock_discovery: MagicMock,
        mock_clob_ws: MagicMock,
    ) -> None:
        """initialize returns an empty list when no markets are found."""
        mock_discovery.find_active_markets.return_value = []

        result = await manager.initialize()

        assert result == []
        mock_clob_ws.subscribe.assert_not_awaited()

    async def test_single_market_found(
        self,
        manager: MarketManager,
        mock_discovery: MagicMock,
        mock_clob_ws: MagicMock,
    ) -> None:
        """initialize works correctly when only one market is found."""
        btc_market = _make_market(asset="BTC")
        mock_discovery.find_active_markets.return_value = [btc_market]

        result = await manager.initialize()

        assert len(result) == 1
        assert result[0] is btc_market

        subscribed_ids = mock_clob_ws.subscribe.call_args[0][0]
        assert len(subscribed_ids) == 2
        assert btc_market.yes_token_id in subscribed_ids
        assert btc_market.no_token_id in subscribed_ids


# ---------------------------------------------------------------------------
# active_markets property
# ---------------------------------------------------------------------------


class TestActiveMarkets:
    """Tests for the active_markets property."""

    async def test_returns_non_expired_markets(
        self,
        manager: MarketManager,
        mock_discovery: MagicMock,
    ) -> None:
        """active_markets returns only markets that have not expired."""
        btc_market = _make_market(asset="BTC", minutes_remaining=10.0)
        eth_market = _make_market(asset="ETH", minutes_remaining=5.0)
        mock_discovery.find_active_markets.return_value = [btc_market, eth_market]

        await manager.initialize()

        active = manager.active_markets
        assert len(active) == 2
        assert btc_market in active
        assert eth_market in active

    async def test_filters_out_expired_markets(
        self,
        manager: MarketManager,
    ) -> None:
        """active_markets filters out markets whose end_time has passed."""
        active_market = _make_market(asset="BTC", minutes_remaining=10.0)
        expired_market = _make_expired_market(asset="ETH")

        # Manually insert both into internal state
        manager._active_markets[active_market.condition_id] = active_market
        manager._active_markets[expired_market.condition_id] = expired_market

        active = manager.active_markets
        assert len(active) == 1
        assert active[0] is active_market

    async def test_empty_when_no_markets(
        self,
        manager: MarketManager,
    ) -> None:
        """active_markets returns empty list when no markets are tracked."""
        assert manager.active_markets == []


# ---------------------------------------------------------------------------
# active_token_ids property
# ---------------------------------------------------------------------------


class TestActiveTokenIds:
    """Tests for the active_token_ids property."""

    async def test_returns_all_yes_no_token_ids(
        self,
        manager: MarketManager,
    ) -> None:
        """active_token_ids returns yes and no tokens for all active markets."""
        btc_market = _make_market(asset="BTC")
        eth_market = _make_market(asset="ETH")
        manager._active_markets[btc_market.condition_id] = btc_market
        manager._active_markets[eth_market.condition_id] = eth_market

        token_ids = manager.active_token_ids

        assert len(token_ids) == 4
        assert btc_market.yes_token_id in token_ids
        assert btc_market.no_token_id in token_ids
        assert eth_market.yes_token_id in token_ids
        assert eth_market.no_token_id in token_ids

    async def test_excludes_expired_market_tokens(
        self,
        manager: MarketManager,
    ) -> None:
        """active_token_ids excludes tokens from expired markets."""
        active_market = _make_market(asset="BTC")
        expired_market = _make_expired_market(asset="ETH")
        manager._active_markets[active_market.condition_id] = active_market
        manager._active_markets[expired_market.condition_id] = expired_market

        token_ids = manager.active_token_ids

        assert len(token_ids) == 2
        assert active_market.yes_token_id in token_ids
        assert active_market.no_token_id in token_ids
        assert expired_market.yes_token_id not in token_ids
        assert expired_market.no_token_id not in token_ids

    async def test_empty_when_no_active_markets(
        self,
        manager: MarketManager,
    ) -> None:
        """active_token_ids returns empty list when no active markets."""
        assert manager.active_token_ids == []


# ---------------------------------------------------------------------------
# check_rollover
# ---------------------------------------------------------------------------


class TestCheckRollover:
    """Tests for check_rollover()."""

    async def test_removes_expired_markets(
        self,
        manager: MarketManager,
        mock_clob_ws: MagicMock,
    ) -> None:
        """check_rollover removes markets that have expired."""
        expired_market = _make_expired_market(asset="BTC")
        manager._active_markets[expired_market.condition_id] = expired_market

        # No replacement available
        await manager.check_rollover()

        assert expired_market.condition_id not in manager._active_markets

    async def test_discovers_replacement_markets(
        self,
        manager: MarketManager,
        mock_discovery: MagicMock,
    ) -> None:
        """check_rollover discovers new markets for assets without one."""
        new_btc = _make_market(asset="BTC")
        new_eth = _make_market(asset="ETH")
        mock_discovery.find_market_for_asset.side_effect = (
            lambda asset: new_btc if asset == "BTC" else new_eth
        )

        result = await manager.check_rollover()

        assert len(result) == 2
        assert new_btc.condition_id in manager._active_markets
        assert new_eth.condition_id in manager._active_markets

    async def test_unsubscribes_expired_token_ids(
        self,
        manager: MarketManager,
        mock_clob_ws: MagicMock,
    ) -> None:
        """check_rollover unsubscribes token IDs of expired markets."""
        expired = _make_expired_market(asset="BTC")
        manager._active_markets[expired.condition_id] = expired

        await manager.check_rollover()

        mock_clob_ws.unsubscribe.assert_awaited_once()
        unsubscribed_ids = mock_clob_ws.unsubscribe.call_args[0][0]
        assert expired.yes_token_id in unsubscribed_ids
        assert expired.no_token_id in unsubscribed_ids

    async def test_subscribes_new_token_ids(
        self,
        manager: MarketManager,
        mock_discovery: MagicMock,
        mock_clob_ws: MagicMock,
    ) -> None:
        """check_rollover subscribes token IDs of newly discovered markets."""
        new_btc = _make_market(asset="BTC")
        new_eth = _make_market(asset="ETH")
        mock_discovery.find_market_for_asset.side_effect = (
            lambda asset: new_btc if asset == "BTC" else new_eth
        )

        await manager.check_rollover()

        # subscribe should be called for the new markets
        mock_clob_ws.subscribe.assert_awaited_once()
        subscribed_ids = mock_clob_ws.subscribe.call_args[0][0]
        assert new_btc.yes_token_id in subscribed_ids
        assert new_btc.no_token_id in subscribed_ids
        assert new_eth.yes_token_id in subscribed_ids
        assert new_eth.no_token_id in subscribed_ids

    async def test_full_rollover_cycle(
        self,
        manager: MarketManager,
        mock_discovery: MagicMock,
        mock_clob_ws: MagicMock,
    ) -> None:
        """Full rollover: expire old BTC market, discover new BTC market."""
        old_btc = _make_expired_market(asset="BTC")
        manager._active_markets[old_btc.condition_id] = old_btc

        # ETH still active
        active_eth = _make_market(asset="ETH", minutes_remaining=10.0)
        manager._active_markets[active_eth.condition_id] = active_eth

        new_btc = _make_market(asset="BTC")
        mock_discovery.find_market_for_asset.return_value = new_btc

        result = await manager.check_rollover()

        # Old BTC removed, new BTC added, ETH still there
        assert old_btc.condition_id not in manager._active_markets
        assert new_btc.condition_id in manager._active_markets
        assert active_eth.condition_id in manager._active_markets
        assert len(result) == 2

    async def test_no_changes_when_all_active(
        self,
        manager: MarketManager,
        mock_discovery: MagicMock,
        mock_clob_ws: MagicMock,
    ) -> None:
        """check_rollover does nothing when all markets are still active."""
        btc_market = _make_market(asset="BTC", minutes_remaining=10.0)
        eth_market = _make_market(asset="ETH", minutes_remaining=10.0)
        manager._active_markets[btc_market.condition_id] = btc_market
        manager._active_markets[eth_market.condition_id] = eth_market

        result = await manager.check_rollover()

        assert len(result) == 2
        mock_clob_ws.unsubscribe.assert_not_awaited()
        # No subscribe call because no new markets were needed
        mock_clob_ws.subscribe.assert_not_awaited()

    async def test_removes_order_books_for_expired(
        self,
        manager: MarketManager,
        mock_book_manager: MagicMock,
    ) -> None:
        """check_rollover removes order books for expired market tokens."""
        expired = _make_expired_market(asset="BTC")
        manager._active_markets[expired.condition_id] = expired

        await manager.check_rollover()

        # remove_book should be called for both yes and no token IDs
        remove_calls = [call[0][0] for call in mock_book_manager.remove_book.call_args_list]
        assert expired.yes_token_id in remove_calls
        assert expired.no_token_id in remove_calls


# ---------------------------------------------------------------------------
# get_market_for_asset
# ---------------------------------------------------------------------------


class TestGetMarketForAsset:
    """Tests for get_market_for_asset()."""

    async def test_returns_correct_market(
        self,
        manager: MarketManager,
    ) -> None:
        """get_market_for_asset returns the active market for the given asset."""
        btc_market = _make_market(asset="BTC")
        eth_market = _make_market(asset="ETH")
        manager._active_markets[btc_market.condition_id] = btc_market
        manager._active_markets[eth_market.condition_id] = eth_market

        result = manager.get_market_for_asset("BTC")
        assert result is btc_market

        result = manager.get_market_for_asset("ETH")
        assert result is eth_market

    async def test_returns_none_when_not_found(
        self,
        manager: MarketManager,
    ) -> None:
        """get_market_for_asset returns None when no market exists for asset."""
        assert manager.get_market_for_asset("BTC") is None

    async def test_returns_none_for_expired_market(
        self,
        manager: MarketManager,
    ) -> None:
        """get_market_for_asset returns None if the only market for asset is expired."""
        expired = _make_expired_market(asset="BTC")
        manager._active_markets[expired.condition_id] = expired

        assert manager.get_market_for_asset("BTC") is None

    async def test_returns_active_not_expired(
        self,
        manager: MarketManager,
    ) -> None:
        """When both active and expired markets exist for an asset,
        only the active one is returned."""
        expired = _make_expired_market(asset="BTC", condition_id="expired_btc")
        active = _make_market(asset="BTC", condition_id="active_btc")
        manager._active_markets[expired.condition_id] = expired
        manager._active_markets[active.condition_id] = active

        result = manager.get_market_for_asset("BTC")
        assert result is active


# ---------------------------------------------------------------------------
# _assets_needing_rollover
# ---------------------------------------------------------------------------


class TestAssetsNeedingRollover:
    """Tests for _assets_needing_rollover()."""

    async def test_detects_missing_assets(
        self,
        manager: MarketManager,
    ) -> None:
        """Assets with no active market need rollover."""
        # settings.markets = ["BTC", "ETH"] but no markets are tracked
        result = manager._assets_needing_rollover()
        assert result == {"BTC", "ETH"}

    async def test_detects_soon_expiring_assets(
        self,
        manager: MarketManager,
    ) -> None:
        """Assets whose market expires within the buffer need rollover."""
        # Market expires in 30 seconds (less than the 60s buffer)
        soon_market = _make_soon_expiring_market(asset="BTC", seconds_until_expiry=30.0)
        manager._active_markets[soon_market.condition_id] = soon_market

        # ETH has plenty of time left
        eth_market = _make_market(asset="ETH", minutes_remaining=10.0)
        manager._active_markets[eth_market.condition_id] = eth_market

        result = manager._assets_needing_rollover()
        assert "BTC" in result
        assert "ETH" not in result

    async def test_no_rollover_needed_when_all_active(
        self,
        manager: MarketManager,
    ) -> None:
        """No assets need rollover when all have markets with plenty of time."""
        btc_market = _make_market(asset="BTC", minutes_remaining=10.0)
        eth_market = _make_market(asset="ETH", minutes_remaining=10.0)
        manager._active_markets[btc_market.condition_id] = btc_market
        manager._active_markets[eth_market.condition_id] = eth_market

        result = manager._assets_needing_rollover()
        assert result == set()

    async def test_expired_market_needs_rollover(
        self,
        manager: MarketManager,
    ) -> None:
        """An asset whose market already expired needs rollover."""
        expired = _make_expired_market(asset="BTC")
        manager._active_markets[expired.condition_id] = expired

        # ETH is fine
        eth_market = _make_market(asset="ETH", minutes_remaining=10.0)
        manager._active_markets[eth_market.condition_id] = eth_market

        result = manager._assets_needing_rollover()
        # BTC needs rollover because its market is expired (get_market_for_asset returns None)
        assert "BTC" in result
        assert "ETH" not in result


# ---------------------------------------------------------------------------
# Multiple assets handled correctly
# ---------------------------------------------------------------------------


class TestMultipleAssets:
    """Tests for handling multiple assets simultaneously."""

    async def test_initialize_multiple_assets(
        self,
        manager: MarketManager,
        mock_discovery: MagicMock,
        mock_clob_ws: MagicMock,
    ) -> None:
        """Initialize correctly handles BTC + ETH markets simultaneously."""
        btc = _make_market(asset="BTC")
        eth = _make_market(asset="ETH")
        mock_discovery.find_active_markets.return_value = [btc, eth]

        result = await manager.initialize()

        assert len(result) == 2
        assets = {m.asset for m in result}
        assert assets == {"BTC", "ETH"}

        # All 4 token IDs subscribed
        subscribed_ids = mock_clob_ws.subscribe.call_args[0][0]
        assert len(subscribed_ids) == 4

    async def test_independent_rollover_per_asset(
        self,
        manager: MarketManager,
        mock_discovery: MagicMock,
        mock_clob_ws: MagicMock,
    ) -> None:
        """Only the expired asset gets rolled over; the other stays."""
        old_btc = _make_expired_market(asset="BTC", condition_id="old_btc")
        active_eth = _make_market(asset="ETH", minutes_remaining=10.0)
        manager._active_markets[old_btc.condition_id] = old_btc
        manager._active_markets[active_eth.condition_id] = active_eth

        new_btc = _make_market(asset="BTC", condition_id="new_btc")
        mock_discovery.find_market_for_asset.return_value = new_btc

        result = await manager.check_rollover()

        # Old BTC gone, new BTC added
        assert "old_btc" not in manager._active_markets
        assert "new_btc" in manager._active_markets
        # ETH unchanged
        assert active_eth.condition_id in manager._active_markets

        # Unsubscribe was called for old BTC tokens
        mock_clob_ws.unsubscribe.assert_awaited_once()
        unsub_ids = mock_clob_ws.unsubscribe.call_args[0][0]
        assert old_btc.yes_token_id in unsub_ids

        # Subscribe was called for new BTC tokens
        mock_clob_ws.subscribe.assert_awaited_once()
        sub_ids = mock_clob_ws.subscribe.call_args[0][0]
        assert new_btc.yes_token_id in sub_ids

    async def test_both_assets_expire_simultaneously(
        self,
        manager: MarketManager,
        mock_discovery: MagicMock,
        mock_clob_ws: MagicMock,
    ) -> None:
        """Both BTC and ETH can expire and be replaced in one rollover."""
        old_btc = _make_expired_market(asset="BTC", condition_id="old_btc")
        old_eth = _make_expired_market(asset="ETH", condition_id="old_eth")
        manager._active_markets[old_btc.condition_id] = old_btc
        manager._active_markets[old_eth.condition_id] = old_eth

        new_btc = _make_market(asset="BTC", condition_id="new_btc")
        new_eth = _make_market(asset="ETH", condition_id="new_eth")
        mock_discovery.find_market_for_asset.side_effect = (
            lambda asset: new_btc if asset == "BTC" else new_eth
        )

        result = await manager.check_rollover()

        assert "old_btc" not in manager._active_markets
        assert "old_eth" not in manager._active_markets
        assert "new_btc" in manager._active_markets
        assert "new_eth" in manager._active_markets
        assert len(result) == 2

    async def test_active_token_ids_multiple_assets(
        self,
        manager: MarketManager,
    ) -> None:
        """active_token_ids returns tokens for all active assets."""
        btc = _make_market(asset="BTC")
        eth = _make_market(asset="ETH")
        manager._active_markets[btc.condition_id] = btc
        manager._active_markets[eth.condition_id] = eth

        ids = manager.active_token_ids
        assert len(ids) == 4
        expected = {btc.yes_token_id, btc.no_token_id, eth.yes_token_id, eth.no_token_id}
        assert set(ids) == expected


# ---------------------------------------------------------------------------
# _remove_expired
# ---------------------------------------------------------------------------


class TestRemoveExpired:
    """Tests for _remove_expired() internal method."""

    async def test_returns_removed_markets(
        self,
        manager: MarketManager,
        mock_clob_ws: MagicMock,
    ) -> None:
        """_remove_expired returns the list of markets that were removed."""
        expired = _make_expired_market(asset="BTC")
        manager._active_markets[expired.condition_id] = expired

        removed = await manager._remove_expired()

        assert len(removed) == 1
        assert removed[0] is expired

    async def test_does_not_remove_active_markets(
        self,
        manager: MarketManager,
        mock_clob_ws: MagicMock,
    ) -> None:
        """_remove_expired leaves active markets intact."""
        active = _make_market(asset="BTC", minutes_remaining=10.0)
        manager._active_markets[active.condition_id] = active

        removed = await manager._remove_expired()

        assert len(removed) == 0
        assert active.condition_id in manager._active_markets

    async def test_mixed_expired_and_active(
        self,
        manager: MarketManager,
        mock_clob_ws: MagicMock,
    ) -> None:
        """_remove_expired only removes expired, keeping active ones."""
        expired = _make_expired_market(asset="BTC")
        active = _make_market(asset="ETH", minutes_remaining=10.0)
        manager._active_markets[expired.condition_id] = expired
        manager._active_markets[active.condition_id] = active

        removed = await manager._remove_expired()

        assert len(removed) == 1
        assert removed[0] is expired
        assert active.condition_id in manager._active_markets
        assert expired.condition_id not in manager._active_markets


# ---------------------------------------------------------------------------
# _discover_missing
# ---------------------------------------------------------------------------


class TestDiscoverMissing:
    """Tests for _discover_missing() internal method."""

    async def test_discovers_for_missing_assets(
        self,
        manager: MarketManager,
        mock_discovery: MagicMock,
        mock_clob_ws: MagicMock,
    ) -> None:
        """_discover_missing finds markets for assets without an active one."""
        new_btc = _make_market(asset="BTC")
        new_eth = _make_market(asset="ETH")
        mock_discovery.find_market_for_asset.side_effect = (
            lambda asset: new_btc if asset == "BTC" else new_eth
        )

        # No active assets
        result = await manager._discover_missing(set())

        assert len(result) == 2

    async def test_skips_assets_already_active(
        self,
        manager: MarketManager,
        mock_discovery: MagicMock,
        mock_clob_ws: MagicMock,
    ) -> None:
        """_discover_missing does not re-discover assets that are active
        and not near expiry."""
        # BTC is active with plenty of time
        btc = _make_market(asset="BTC", minutes_remaining=10.0)
        manager._active_markets[btc.condition_id] = btc

        new_eth = _make_market(asset="ETH")
        mock_discovery.find_market_for_asset.return_value = new_eth

        # BTC is in active_assets
        result = await manager._discover_missing({"BTC"})

        # Only ETH should be discovered
        assert len(result) == 1
        assert result[0].asset == "ETH"

    async def test_handles_discovery_failure(
        self,
        manager: MarketManager,
        mock_discovery: MagicMock,
        mock_clob_ws: MagicMock,
    ) -> None:
        """_discover_missing gracefully handles when discovery returns None."""
        mock_discovery.find_market_for_asset.return_value = None

        result = await manager._discover_missing(set())

        assert result == []
        mock_clob_ws.subscribe.assert_not_awaited()


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge case tests."""

    async def test_remove_book_fallback_without_method(
        self,
        settings: Settings,
        mock_discovery: MagicMock,
        mock_clob_ws: MagicMock,
    ) -> None:
        """If OrderBookManager has no remove_book, _remove_book is a no-op."""
        book_mgr = MagicMock(spec=["ensure_book", "get_book"])
        # No remove_book attribute
        mgr = MarketManager(
            settings=settings,
            discovery=mock_discovery,
            clob_ws=mock_clob_ws,
            book_manager=book_mgr,
        )

        expired = _make_expired_market(asset="BTC")
        mgr._active_markets[expired.condition_id] = expired

        # Should not raise even though remove_book is missing
        await mgr.check_rollover()

    async def test_duplicate_market_not_added_twice(
        self,
        manager: MarketManager,
        mock_discovery: MagicMock,
        mock_clob_ws: MagicMock,
    ) -> None:
        """If discovery returns a market we already track, don't duplicate it."""
        existing = _make_market(asset="BTC", condition_id="existing_btc")
        manager._active_markets[existing.condition_id] = existing

        # Set up a soon-expiring market so rollover triggers discovery for BTC
        manager._active_markets[existing.condition_id] = _make_soon_expiring_market(
            asset="BTC", seconds_until_expiry=30.0,
        )
        # Overwrite condition_id to match
        manager._active_markets[existing.condition_id].condition_id = existing.condition_id

        # Discovery returns the same market (same condition_id)
        same_market = _make_market(asset="BTC", condition_id=existing.condition_id)
        mock_discovery.find_market_for_asset.return_value = same_market

        # ETH needs discovery too
        eth = _make_market(asset="ETH")
        mock_discovery.find_market_for_asset.side_effect = (
            lambda asset: same_market if asset == "BTC" else eth
        )

        result = await manager.check_rollover()

        # Should not have duplicate condition IDs
        condition_ids = [m.condition_id for m in result]
        assert len(condition_ids) == len(set(condition_ids))
