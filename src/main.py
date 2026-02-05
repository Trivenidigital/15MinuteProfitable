"""Entry point for the Polymarket 15-minute trading bot.

Loads configuration, discovers active markets, subscribes to the
CLOB WebSocket, evaluates arbitrage opportunities, and executes
trades (or logs them in dry-run mode).
"""

from __future__ import annotations

import asyncio
import signal
import time

from src.config import Settings
from src.core.models import Market, Side, TradeOrder
from src.core.state import StateManager
from src.data.clob_ws import ClobWebSocket
from src.data.market_discovery import MarketDiscovery
from src.data.orderbook import OrderBookManager
from src.execution.executor import OrderExecutor
from src.monitoring.logger import get_logger, setup_logging
from src.risk.manager import RiskManager
from src.strategy.arbitrage import ArbitrageStrategy
from src.utils.time_utils import time_remaining_seconds

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------

_shutdown_event: asyncio.Event | None = None
_log = get_logger("main")


# ---------------------------------------------------------------------------
# Strategy evaluation loop
# ---------------------------------------------------------------------------


async def _strategy_loop(
    markets: list[Market],
    strategy: ArbitrageStrategy,
    risk_manager: RiskManager,
    executor: OrderExecutor,
    state_manager: StateManager,
    book_manager: OrderBookManager,
    settings: Settings,
) -> None:
    """Continuously evaluate markets for arbitrage opportunities.

    Runs every 2 seconds until the shutdown event is set.
    """
    while _shutdown_event is not None and not _shutdown_event.is_set():
        for market in markets:
            # Skip expired markets
            remaining = time_remaining_seconds(market.end_time.timestamp())
            if remaining <= 0:
                continue

            # Evaluate
            opp = await strategy.evaluate(market)
            if opp is None:
                continue

            # Risk check
            approved, reason = risk_manager.check_opportunity(opp)
            if not approved:
                _log.debug(
                    "opportunity_rejected",
                    market=market.slug,
                    reason=reason,
                )
                continue

            # Adjust size
            adjusted_size = risk_manager.adjust_size(opp, settings.order_size)
            if adjusted_size <= 0:
                _log.debug("size_adjusted_to_zero", market=market.slug)
                continue

            # Build orders
            yes_order = TradeOrder(
                token_id=market.yes_token_id,
                side=Side.BUY,
                price=opp.yes_fill.vwap if opp.yes_fill else 0.0,
                size=adjusted_size,
                order_type=settings.order_type,
            )
            no_order = TradeOrder(
                token_id=market.no_token_id,
                side=Side.BUY,
                price=opp.no_fill.vwap if opp.no_fill else 0.0,
                size=adjusted_size,
                order_type=settings.order_type,
            )

            # Execute
            _log.info(
                "executing_arb",
                market=market.slug,
                yes_price=yes_order.price,
                no_price=no_order.price,
                size=adjusted_size,
                expected_profit=round(opp.expected_profit, 4),
                dry_run=settings.dry_run,
            )

            try:
                yes_result, no_result = await executor.execute_arb(
                    yes_order, no_order
                )

                # Record results
                state_manager.record_trade(opp, [yes_result, no_result])
                risk_manager.record_execution_success(market.condition_id)

                _log.info(
                    "arb_complete",
                    market=market.slug,
                    yes_status=yes_result.status.value,
                    no_status=no_result.status.value,
                    daily_pnl=round(state_manager.daily_pnl().net_profit, 4),
                )
            except Exception as exc:
                risk_manager.record_execution_failure()
                _log.error(
                    "execution_error",
                    market=market.slug,
                    error=str(exc),
                )

        # Wait before next evaluation cycle
        try:
            await asyncio.wait_for(
                _shutdown_event.wait(),
                timeout=2.0,
            )
        except asyncio.TimeoutError:
            pass


# ---------------------------------------------------------------------------
# Monitoring loop
# ---------------------------------------------------------------------------


async def _monitor_loop(
    book_manager: OrderBookManager,
    state_manager: StateManager,
    token_ids: list[str],
    interval: float = 10.0,
) -> None:
    """Log orderbook state and P&L at regular intervals."""
    while _shutdown_event is not None and not _shutdown_event.is_set():
        for token_id in token_ids:
            book = book_manager.get_book(token_id)
            if book is None:
                continue
            _log.info(
                "orderbook",
                token_id=token_id[:12],
                best_bid=book.best_bid,
                best_ask=book.best_ask,
                spread=book.spread,
                bid_levels=len(book.bids),
                ask_levels=len(book.asks),
            )

        pnl = state_manager.daily_pnl()
        _log.info(
            "daily_summary",
            trades=pnl.trades,
            net_profit=round(pnl.net_profit, 4),
            total_fees=round(pnl.total_fees, 4),
            opportunities_seen=pnl.opportunities_seen,
            opportunities_taken=pnl.opportunities_taken,
            sim_balance=round(state_manager.sim_balance, 2),
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
) -> list[Market]:
    """Discover active markets and subscribe to their token IDs.

    Returns the list of discovered Market objects.
    """
    discovery = MarketDiscovery(gamma_api_url=settings.gamma_api_url)

    _log.info("discovering_markets", assets=settings.markets)

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
            remaining_s=round(
                time_remaining_seconds(market.end_time.timestamp()), 1
            ),
        )
        token_ids.extend([market.yes_token_id, market.no_token_id])

    await clob_ws.subscribe(token_ids)

    _log.info(
        "subscribed",
        token_count=len(token_ids),
        market_count=len(markets),
    )

    return markets


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
    """Async entry point: discover, subscribe, evaluate, execute."""
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
        enable_arbitrage=settings.enable_arbitrage,
    )

    # Core components
    book_manager = OrderBookManager()
    state_manager = StateManager(settings)
    risk_manager = RiskManager(settings, state_manager)
    executor = OrderExecutor(settings)

    clob_ws = ClobWebSocket(
        ws_url=settings.clob_ws_url,
        book_manager=book_manager,
    )

    # Load any saved state
    state_manager.load_snapshot()

    # Discover markets and subscribe
    markets = await _discover_and_subscribe(settings, book_manager, clob_ws)
    if not markets:
        _log.warning("no_tokens_to_track", msg="Exiting - no active markets found.")
        return

    token_ids = []
    for m in markets:
        token_ids.extend([m.yes_token_id, m.no_token_id])

    # Strategy
    arb_strategy = ArbitrageStrategy(
        settings=settings, book_manager=book_manager
    )

    # Start concurrent tasks
    ws_task = asyncio.create_task(clob_ws.run())
    monitor_task = asyncio.create_task(
        _monitor_loop(book_manager, state_manager, token_ids, interval=10.0),
    )

    strategy_task = None
    if settings.enable_arbitrage:
        strategy_task = asyncio.create_task(
            _strategy_loop(
                markets=markets,
                strategy=arb_strategy,
                risk_manager=risk_manager,
                executor=executor,
                state_manager=state_manager,
                book_manager=book_manager,
                settings=settings,
            ),
        )

    _log.info("bot_running", msg="Press Ctrl+C to stop.")

    # Wait for shutdown signal
    await _shutdown_event.wait()

    # Graceful shutdown
    _log.info("shutting_down")
    clob_ws.stop()

    # Save state before exiting
    state_manager.save_snapshot()

    # Cancel tasks
    try:
        await asyncio.wait_for(ws_task, timeout=5.0)
    except asyncio.TimeoutError:
        ws_task.cancel()

    for task in [monitor_task, strategy_task]:
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    pnl = state_manager.daily_pnl()
    _log.info(
        "bot_stopped",
        trades=pnl.trades,
        net_profit=round(pnl.net_profit, 4),
        sim_balance=round(state_manager.sim_balance, 2),
    )


def main() -> None:
    """Synchronous entry point."""
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
