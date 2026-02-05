"""Manages 15-minute market lifecycle: discovery, rollover, subscriptions.

Responsible for maintaining a list of currently active markets, detecting
when markets expire, discovering next-window markets proactively, and
managing WebSocket subscribe/unsubscribe for token IDs.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from src.config import Settings
from src.core.models import Market
from src.data.clob_ws import ClobWebSocket
from src.data.market_discovery import MarketDiscovery
from src.data.orderbook import OrderBookManager
from src.monitoring.logger import get_logger


class MarketManager:
    """Manages 15-minute market lifecycle: discovery, rollover, subscriptions.

    Responsible for:
    - Maintaining a list of currently active markets
    - Detecting when markets expire
    - Discovering next-window markets proactively
    - Managing WebSocket subscribe/unsubscribe for token IDs
    - Cleaning up expired market state
    """

    def __init__(
        self,
        settings: Settings,
        discovery: MarketDiscovery,
        clob_ws: ClobWebSocket,
        book_manager: OrderBookManager,
    ) -> None:
        self._settings = settings
        self._discovery = discovery
        self._clob_ws = clob_ws
        self._book_manager = book_manager
        self._log = get_logger("market_manager")
        self._active_markets: dict[str, Market] = {}  # condition_id -> Market
        self._rollover_buffer_seconds: float = 60.0  # discover next market 60s before expiry

    # -- properties ----------------------------------------------------------

    @property
    def active_markets(self) -> list[Market]:
        """Return list of currently active (non-expired) markets."""
        now = datetime.now(tz=timezone.utc)
        return [m for m in self._active_markets.values() if m.end_time > now]

    @property
    def active_token_ids(self) -> list[str]:
        """Return flat list of all yes/no token IDs for active markets."""
        tokens: list[str] = []
        for market in self.active_markets:
            tokens.append(market.yes_token_id)
            tokens.append(market.no_token_id)
        return tokens

    # -- public API ----------------------------------------------------------

    async def initialize(self) -> list[Market]:
        """Initial market discovery. Called once at startup.

        Discovers markets for all configured assets, subscribes to their
        token IDs on the WebSocket, returns the discovered markets.
        """
        assets = self._settings.markets
        self._log.info("initializing", assets=assets)

        markets = await self._discovery.find_active_markets(assets=assets)

        if not markets:
            self._log.warning("no_markets_found", assets=assets)
            return []

        # Register all discovered markets
        token_ids_to_subscribe: list[str] = []
        for market in markets:
            self._active_markets[market.condition_id] = market
            token_ids_to_subscribe.append(market.yes_token_id)
            token_ids_to_subscribe.append(market.no_token_id)
            self._log.info(
                "market_discovered",
                asset=market.asset,
                condition_id=market.condition_id,
                slug=market.slug,
                end_time=market.end_time.isoformat(),
            )

        # Subscribe to WebSocket for all token IDs
        if token_ids_to_subscribe:
            await self._clob_ws.subscribe(token_ids_to_subscribe)
            self._log.info(
                "market_subscribed",
                token_count=len(token_ids_to_subscribe),
            )

        return list(self._active_markets.values())

    async def check_rollover(self) -> list[Market]:
        """Check for expired markets and discover replacements.

        1. Remove any markets that have expired (end_time <= now)
        2. For assets with no active market, discover the next one
        3. Subscribe new token IDs, unsubscribe expired ones
        4. Return the current list of active markets

        Should be called periodically (e.g. every 30 seconds).
        """
        # Step 1: Remove expired markets (also unsubscribes their tokens)
        expired = await self._remove_expired()

        # Step 2: Determine which configured assets currently have active markets
        active_assets = {m.asset for m in self._active_markets.values()}

        # Step 3: Discover markets for any assets that are missing or need rollover
        new_markets = await self._discover_missing(active_assets)

        if expired or new_markets:
            self._log.info(
                "rollover_complete",
                expired_count=len(expired),
                new_count=len(new_markets),
                active_count=len(self._active_markets),
            )

        return list(self._active_markets.values())

    async def _remove_expired(self) -> list[Market]:
        """Remove markets whose end_time has passed.

        Returns the list of removed markets.
        Unsubscribes their token IDs from the WebSocket.
        """
        now = datetime.now(tz=timezone.utc)
        expired: list[Market] = []
        token_ids_to_unsubscribe: list[str] = []

        for condition_id, market in list(self._active_markets.items()):
            if market.end_time <= now:
                expired.append(market)
                token_ids_to_unsubscribe.append(market.yes_token_id)
                token_ids_to_unsubscribe.append(market.no_token_id)
                del self._active_markets[condition_id]
                self._log.info(
                    "market_expired",
                    asset=market.asset,
                    condition_id=market.condition_id,
                    slug=market.slug,
                )

        # Unsubscribe expired token IDs from WebSocket
        if token_ids_to_unsubscribe:
            await self._clob_ws.unsubscribe(token_ids_to_unsubscribe)

        # Remove order books for expired tokens
        for token_id in token_ids_to_unsubscribe:
            self._remove_book(token_id)

        return expired

    async def _discover_missing(self, active_assets: set[str]) -> list[Market]:
        """Discover markets for assets that don't have an active market.

        Returns the list of newly discovered markets.
        Subscribes their token IDs to the WebSocket.
        """
        configured_assets = set(self._settings.markets)
        missing_assets = configured_assets - active_assets

        # Also include assets whose markets are about to expire
        assets_needing_rollover = self._assets_needing_rollover()
        all_missing = missing_assets | assets_needing_rollover

        if not all_missing:
            return []

        self._log.info("discovering_missing", assets=sorted(all_missing))

        new_markets: list[Market] = []
        token_ids_to_subscribe: list[str] = []

        for asset in all_missing:
            market = await self._discovery.find_market_for_asset(asset)
            if market is None:
                self._log.warning("discovery_failed", asset=asset)
                continue

            # Skip if we already track this exact market (avoid duplicates
            # when the same market is found for a soon-expiring asset)
            if market.condition_id in self._active_markets:
                continue

            self._active_markets[market.condition_id] = market
            token_ids_to_subscribe.append(market.yes_token_id)
            token_ids_to_subscribe.append(market.no_token_id)
            new_markets.append(market)
            self._log.info(
                "market_discovered",
                asset=market.asset,
                condition_id=market.condition_id,
                slug=market.slug,
                end_time=market.end_time.isoformat(),
            )

        # Subscribe new token IDs
        if token_ids_to_subscribe:
            await self._clob_ws.subscribe(token_ids_to_subscribe)
            self._log.info(
                "market_subscribed",
                token_count=len(token_ids_to_subscribe),
            )

        return new_markets

    def get_market_for_asset(self, asset: str) -> Market | None:
        """Get the active market for a specific asset, if any."""
        now = datetime.now(tz=timezone.utc)
        for market in self._active_markets.values():
            if market.asset == asset and market.end_time > now:
                return market
        return None

    def _assets_needing_rollover(self) -> set[str]:
        """Return set of configured assets that need a new market.

        An asset needs rollover if:
        - It has no active market, OR
        - Its active market expires within rollover_buffer_seconds
        """
        now = time.time()
        configured_assets = set(self._settings.markets)
        needs_rollover: set[str] = set()

        for asset in configured_assets:
            market = self.get_market_for_asset(asset)
            if market is None:
                needs_rollover.add(asset)
            elif market.end_time.timestamp() - now <= self._rollover_buffer_seconds:
                needs_rollover.add(asset)

        return needs_rollover

    def _remove_book(self, token_id: str) -> None:
        """Remove an order book for a token, if the manager supports it.

        Falls back gracefully if OrderBookManager does not have a
        remove_book method.
        """
        if hasattr(self._book_manager, "remove_book"):
            self._book_manager.remove_book(token_id)
