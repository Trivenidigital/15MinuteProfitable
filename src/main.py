"""Entry point for the Polymarket 15-minute trading bot.

Loads configuration, discovers active markets, subscribes to the
CLOB WebSocket, evaluates arbitrage opportunities across multiple
markets, handles 15-minute market rollovers, and executes trades
(or logs them in dry-run mode).
"""

from __future__ import annotations

import asyncio
import signal

from src.config import Settings
from src.core.models import Side, TradeOrder
from src.core.state import StateManager
from src.data.clob_ws import ClobWebSocket
from src.data.market_discovery import MarketDiscovery
from src.data.market_manager import MarketManager
from src.data.orderbook import OrderBookManager
from src.execution.executor import OrderExecutor
from src.monitoring.logger import get_logger, setup_logging
from src.risk.manager import RiskManager
from src.strategy.arbitrage import ArbitrageStrategy
from src.strategy.base import BaseStrategy
from src.strategy.scanner import MarketScanner
from src.utils.rate_limiter import RateLimiter

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------

_shutdown_event: asyncio.Event | None = None
_log = get_logger("main")


# ---------------------------------------------------------------------------
# Strategy evaluation loop (multi-market via MarketScanner)
# ---------------------------------------------------------------------------


async def _strategy_loop(
    market_manager: MarketManager,
    scanner: MarketScanner,
    risk_manager: RiskManager,
    executor: OrderExecutor,
    state_manager: StateManager,
    rate_limiter: RateLimiter,
    settings: Settings,
) -> None:
    """Continuously scan all active markets for the best opportunity.

    Each cycle:
    1. Get current active markets from MarketManager
    2. Use MarketScanner to find the single best opportunity
    3. Risk-check and size-adjust
    4. Acquire rate limiter tokens and execute

    Runs every 2 seconds until the shutdown event is set.
    """
    while _shutdown_event is not None and not _shutdown_event.is_set():
        markets = market_manager.active_markets

        if not markets:
            _log.debug("no_active_markets")
        else:
            # Scanner picks the best opportunity across all markets + strategies
            opp = await scanner.scan(markets)

            if opp is not None:
                market = opp.market

                # Risk check
                approved, reason = risk_manager.check_opportunity(opp)
                if not approved:
                    _log.debug(
                        "opportunity_rejected",
                        market=market.slug,
                        reason=reason,
                    )
                else:
                    # Adjust size
                    adjusted_size = risk_manager.adjust_size(
                        opp, settings.order_size
                    )
                    if adjusted_size <= 0:
                        _log.debug("size_adjusted_to_zero", market=market.slug)
                    else:
                        await _execute_opportunity(
                            opp=opp,
                            adjusted_size=adjusted_size,
                            executor=executor,
                            state_manager=state_manager,
                            risk_manager=risk_manager,
                            rate_limiter=rate_limiter,
                            settings=settings,
                        )

        # Wait before next evaluation cycle
        try:
            await asyncio.wait_for(
                _shutdown_event.wait(),
                timeout=2.0,
            )
        except asyncio.TimeoutError:
            pass


async def _execute_opportunity(
    opp,
    adjusted_size: float,
    executor: OrderExecutor,
    state_manager: StateManager,
    risk_manager: RiskManager,
    rate_limiter: RateLimiter,
    settings: Settings,
) -> None:
    """Build orders, acquire rate limit tokens, and execute."""
    market = opp.market

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

    _log.info(
        "executing_arb",
        market=market.slug,
        strategy=opp.strategy.value,
        yes_price=yes_order.price,
        no_price=no_order.price,
        size=adjusted_size,
        expected_profit=round(opp.expected_profit, 4),
        dry_run=settings.dry_run,
    )

    try:
        # Acquire rate limiter tokens (2 for sign + 2 for submit)
        await rate_limiter.acquire(4)

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
    except asyncio.TimeoutError:
        _log.warning(
            "rate_limit_timeout",
            market=market.slug,
            msg="Could not acquire rate limiter tokens in time",
        )
    except Exception as exc:
        risk_manager.record_execution_failure()
        _log.error(
            "execution_error",
            market=market.slug,
            error=str(exc),
        )


# ---------------------------------------------------------------------------
# Rollover loop (market lifecycle management)
# ---------------------------------------------------------------------------


async def _rollover_loop(
    market_manager: MarketManager,
    interval: float = 30.0,
) -> None:
    """Periodically check for expired markets and discover replacements.

    Runs every *interval* seconds (default 30s) until shutdown.
    """
    while _shutdown_event is not None and not _shutdown_event.is_set():
        try:
            await market_manager.check_rollover()
        except Exception as exc:
            _log.error("rollover_error", error=str(exc))

        try:
            await asyncio.wait_for(
                _shutdown_event.wait(),
                timeout=interval,
            )
        except asyncio.TimeoutError:
            pass


# ---------------------------------------------------------------------------
# Monitoring loop
# ---------------------------------------------------------------------------


async def _monitor_loop(
    market_manager: MarketManager,
    book_manager: OrderBookManager,
    state_manager: StateManager,
    rate_limiter: RateLimiter,
    interval: float = 10.0,
) -> None:
    """Log orderbook state and P&L at regular intervals."""
    while _shutdown_event is not None and not _shutdown_event.is_set():
        # Log orderbooks for all active tokens
        for token_id in market_manager.active_token_ids:
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
            active_markets=len(market_manager.active_markets),
            rate_limit_available=round(rate_limiter.available, 1),
        )

        try:
            await asyncio.wait_for(
                _shutdown_event.wait(),
                timeout=interval,
            )
        except asyncio.TimeoutError:
            pass


# ---------------------------------------------------------------------------
# Shutdown handling
# ---------------------------------------------------------------------------


def _request_shutdown() -> None:
    """Signal the main loop to exit gracefully."""
    _log.info("shutdown_requested")
    if _shutdown_event is not None:
        _shutdown_event.set()


# ---------------------------------------------------------------------------
# Strategy factory
# ---------------------------------------------------------------------------


def _build_strategies(
    settings: Settings, book_manager: OrderBookManager
) -> list[BaseStrategy]:
    """Build the list of enabled strategies based on settings."""
    strategies: list[BaseStrategy] = []

    if settings.enable_arbitrage:
        strategies.append(ArbitrageStrategy(settings=settings, book_manager=book_manager))

    # Future strategies will be added here:
    # if settings.enable_price_lag:
    #     strategies.append(PriceLagStrategy(...))
    # if settings.enable_asymmetric:
    #     strategies.append(AsymmetricStrategy(...))

    return strategies


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
        enable_multi_market=settings.enable_multi_market,
    )

    # Core components
    book_manager = OrderBookManager()
    state_manager = StateManager(settings)
    risk_manager = RiskManager(settings, state_manager)
    executor = OrderExecutor(settings)
    rate_limiter = RateLimiter(max_per_minute=55)

    clob_ws = ClobWebSocket(
        ws_url=settings.clob_ws_url,
        book_manager=book_manager,
    )

    # Market lifecycle manager
    discovery = MarketDiscovery(gamma_api_url=settings.gamma_api_url)
    market_manager = MarketManager(
        settings=settings,
        discovery=discovery,
        clob_ws=clob_ws,
        book_manager=book_manager,
    )

    # Load any saved state
    state_manager.load_snapshot()

    # Initial market discovery via MarketManager
    markets = await market_manager.initialize()
    if not markets:
        _log.warning("no_tokens_to_track", msg="Exiting - no active markets found.")
        return

    # Build enabled strategies and scanner
    strategies = _build_strategies(settings, book_manager)
    if not strategies:
        _log.warning("no_strategies_enabled", msg="Enable at least one strategy.")
        return

    scanner = MarketScanner(strategies=strategies)
    _log.info(
        "strategies_loaded",
        count=scanner.strategy_count,
        names=scanner.strategy_names,
    )

    # Start concurrent tasks
    ws_task = asyncio.create_task(clob_ws.run())

    rollover_task = asyncio.create_task(
        _rollover_loop(market_manager, interval=30.0),
    )

    monitor_task = asyncio.create_task(
        _monitor_loop(
            market_manager, book_manager, state_manager, rate_limiter,
            interval=10.0,
        ),
    )

    strategy_task = asyncio.create_task(
        _strategy_loop(
            market_manager=market_manager,
            scanner=scanner,
            risk_manager=risk_manager,
            executor=executor,
            state_manager=state_manager,
            rate_limiter=rate_limiter,
            settings=settings,
        ),
    )

    _log.info(
        "bot_running",
        active_markets=len(markets),
        msg="Press Ctrl+C to stop.",
    )

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

    for task in [monitor_task, strategy_task, rollover_task]:
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
