"""Manages multi-interval market lifecycle: discovery, rollover, subscriptions.

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
    """Manages multi-interval market lifecycle: discovery, rollover, subscriptions.

    Responsible for:
    - Maintaining a list of currently active markets (across intervals)
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

        Discovers markets for all configured assets and intervals, subscribes
        to their token IDs on the WebSocket, returns the discovered markets.
        """
        assets = self._settings.markets
        intervals = self._settings.market_intervals
        self._log.info("initializing", assets=assets, intervals=intervals)

        markets = await self._discovery.find_active_markets(
            assets=assets, intervals=intervals
        )

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
        2. For (asset, interval) pairs with no active market, discover the next one
        3. Subscribe new token IDs, unsubscribe expired ones
        4. Return the current list of active markets

        Should be called periodically (e.g. every 30 seconds).
        """
        # Step 1: Remove expired markets (also unsubscribes their tokens)
        expired = await self._remove_expired()

        # Step 2: Determine which (asset, interval) pairs currently have active markets
        active_pairs = {
            (m.asset, m.interval) for m in self._active_markets.values()
            if m.end_time > datetime.now(tz=timezone.utc)
        }

        # Step 3: Discover markets for any pairs that are missing or need rollover
        new_markets = await self._discover_missing(active_pairs)

        # Prune stale orderbook entries from expired markets
        pruned = self._book_manager.remove_stale_books(threshold_s=120.0)

        if expired or new_markets:
            self._log.info(
                "rollover_complete",
                expired_count=len(expired),
                new_count=len(new_markets),
                active_count=len(self._active_markets),
                stale_books_pruned=pruned,
            )

        # Force WebSocket reconnect so the server sends fresh book
        # snapshots for the newly subscribed tokens.  The Polymarket CLOB
        # WebSocket does not send snapshots for incremental subscriptions
        # on an existing connection.
        if new_markets:
            await self._clob_ws.reconnect()

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

    async def _discover_missing(
        self, active_pairs: set[tuple[str, str]]
    ) -> list[Market]:
        """Discover markets for (asset, interval) pairs without an active market.

        Returns the list of newly discovered markets.
        Subscribes their token IDs to the WebSocket.
        """
        configured_pairs = {
            (asset, interval)
            for asset in self._settings.markets
            for interval in self._settings.market_intervals
        }
        missing_pairs = configured_pairs - active_pairs

        # Also include pairs whose markets are about to expire
        pairs_needing_rollover = self._pairs_needing_rollover()
        all_missing = missing_pairs | pairs_needing_rollover

        if not all_missing:
            return []

        self._log.info("discovering_missing", pairs=sorted(all_missing))

        new_markets: list[Market] = []
        token_ids_to_subscribe: list[str] = []

        for asset, interval in all_missing:
            market = await self._discovery.find_market_for_asset(
                asset, interval=interval
            )
            if market is None:
                self._log.warning(
                    "discovery_failed", asset=asset, interval=interval
                )
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
                interval=market.interval,
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

    def get_market_for_asset(
        self, asset: str, interval: str | None = None
    ) -> Market | None:
        """Get the active market for a specific asset (and optional interval).

        If *interval* is ``None``, returns the first active match for the
        asset (backward compatible).  If specified, filters by interval too.
        """
        now = datetime.now(tz=timezone.utc)
        for market in self._active_markets.values():
            if market.asset != asset or market.end_time <= now:
                continue
            if interval is not None and market.interval != interval:
                continue
            return market
        return None

    def _pairs_needing_rollover(self) -> set[tuple[str, str]]:
        """Return (asset, interval) pairs that need a new market.

        A pair needs rollover if:
        - It has no active market, OR
        - Its active market expires within rollover_buffer_seconds
        """
        now = time.time()
        configured_pairs = {
            (asset, interval)
            for asset in self._settings.markets
            for interval in self._settings.market_intervals
        }
        needs_rollover: set[tuple[str, str]] = set()

        for asset, interval in configured_pairs:
            market = self.get_market_for_asset(asset, interval=interval)
            if market is None:
                needs_rollover.add((asset, interval))
            elif market.end_time.timestamp() - now <= self._rollover_buffer_seconds:
                needs_rollover.add((asset, interval))

        return needs_rollover

    def _remove_book(self, token_id: str) -> None:
        """Remove an order book for a token, if the manager supports it.

        Falls back gracefully if OrderBookManager does not have a
        remove_book method.
        """
        if hasattr(self._book_manager, "remove_book"):
            self._book_manager.remove_book(token_id)
