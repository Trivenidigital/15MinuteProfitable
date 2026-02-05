"""Entry point for the Polymarket 15-minute trading bot.

Loads configuration, discovers active markets, subscribes to the
CLOB WebSocket, and runs a monitoring loop that periodically logs
orderbook state.  Strategy / execution / risk layers are wired in
during later phases.
"""

from __future__ import annotations

import asyncio
import signal
import sys
import time

from src.config import Settings
from src.data.clob_ws import ClobWebSocket
from src.data.market_discovery import MarketDiscovery
from src.data.orderbook import OrderBookManager
from src.monitoring.logger import get_logger, setup_logging
from src.utils.time_utils import time_remaining_seconds

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------

_shutdown_event: asyncio.Event | None = None
_log = get_logger("main")


# ---------------------------------------------------------------------------
# Monitoring loop
# ---------------------------------------------------------------------------


async def _monitor_loop(
    book_manager: OrderBookManager,
    token_ids: list[str],
    interval: float = 5.0,
) -> None:
    """Log orderbook state at *interval* seconds.

    Runs until the global ``_shutdown_event`` is set.
    """
    while _shutdown_event is not None and not _shutdown_event.is_set():
        for token_id in token_ids:
            book = book_manager.get_book(token_id)
            if book is None:
                continue
            _log.info(
                "orderbook_snapshot",
                token_id=token_id[:12],
                best_bid=book.best_bid,
                best_ask=book.best_ask,
                spread=book.spread,
                bid_levels=len(book.bids),
                ask_levels=len(book.asks),
            )
        try:
            await asyncio.wait_for(
                _shutdown_event.wait(),
                timeout=interval,
            )
        except asyncio.TimeoutError:
            pass


# ---------------------------------------------------------------------------
# Market discovery + subscription
# ---------------------------------------------------------------------------


async def _discover_and_subscribe(
    settings: Settings,
    book_manager: OrderBookManager,
    clob_ws: ClobWebSocket,
) -> list[str]:
    """Discover active markets and subscribe to their token IDs.

    Returns the list of token IDs that were subscribed.
    """
    discovery = MarketDiscovery(gamma_api_url=settings.gamma_api_url)

    _log.info(
        "discovering_markets",
        assets=settings.markets,
    )

    markets = await discovery.find_active_markets(settings.markets)

    if not markets:
        _log.warning("no_markets_found", assets=settings.markets)
        return []

    token_ids: list[str] = []
    for market in markets:
        _log.info(
            "market_found",
            asset=market.asset,
            slug=market.slug,
            condition_id=market.condition_id[:12],
            yes_token=market.yes_token_id[:12],
            no_token=market.no_token_id[:12],
            end_time=market.end_time.isoformat(),
            remaining_s=round(time_remaining_seconds(market.end_time.timestamp()), 1),
        )
        token_ids.extend([market.yes_token_id, market.no_token_id])

    await clob_ws.subscribe(token_ids)

    _log.info(
        "subscribed",
        token_count=len(token_ids),
        market_count=len(markets),
    )

    return token_ids


# ---------------------------------------------------------------------------
# Shutdown handling
# ---------------------------------------------------------------------------


def _request_shutdown() -> None:
    """Signal the main loop to exit gracefully."""
    _log.info("shutdown_requested")
    if _shutdown_event is not None:
        _shutdown_event.set()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def async_main() -> None:
    """Async entry point: discover markets, start WebSocket, run monitor."""
    global _shutdown_event
    _shutdown_event = asyncio.Event()

    # Register signal handlers for graceful shutdown
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_shutdown)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler for SIGTERM
            pass

    # Load configuration
    try:
        settings = Settings()  # type: ignore[call-arg]
    except Exception as exc:
        _log.error("config_load_failed", error=str(exc))
        _log.info(
            "config_hint",
            msg="Set BOT_PRIVATE_KEY env var or create .env file. See .env.example.",
        )
        return

    # Initialize logging
    setup_logging(log_level=settings.log_level, log_format=settings.log_format)

    _log.info(
        "bot_starting",
        dry_run=settings.dry_run,
        markets=settings.markets,
        order_size=settings.order_size,
        target_pair_cost=settings.target_pair_cost,
    )

    # Core components
    book_manager = OrderBookManager()
    clob_ws = ClobWebSocket(
        ws_url=settings.clob_ws_url,
        book_manager=book_manager,
    )

    # Discover markets and subscribe
    token_ids = await _discover_and_subscribe(settings, book_manager, clob_ws)
    if not token_ids:
        _log.warning("no_tokens_to_track", msg="Exiting — no active markets found.")
        return

    # Start WebSocket and monitoring as concurrent tasks
    ws_task = asyncio.create_task(clob_ws.run())
    monitor_task = asyncio.create_task(
        _monitor_loop(book_manager, token_ids, interval=5.0),
    )

    _log.info("bot_running", msg="Press Ctrl+C to stop.")

    # Wait for shutdown signal
    await _shutdown_event.wait()

    # Graceful shutdown
    _log.info("shutting_down")
    clob_ws.stop()

    # Give the WS task a moment to close cleanly
    try:
        await asyncio.wait_for(ws_task, timeout=5.0)
    except asyncio.TimeoutError:
        ws_task.cancel()

    monitor_task.cancel()

    try:
        await monitor_task
    except asyncio.CancelledError:
        pass

    _log.info("bot_stopped")


def main() -> None:
    """Synchronous entry point."""
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
