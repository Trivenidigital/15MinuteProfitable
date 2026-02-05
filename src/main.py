"""Entry point for the Polymarket 15-minute trading bot.

Loads configuration, discovers active markets, subscribes to the
CLOB WebSocket, evaluates arbitrage opportunities across multiple
markets, handles 15-minute market rollovers, and executes trades
(or logs them in dry-run mode).
"""

from __future__ import annotations

import asyncio
import signal
import time

from src.config import Settings
from src.core.models import OrderStatus, Side, StrategyType, TradeOrder
from src.core.state import StateManager
from src.data.binance_ws import BinanceWebSocket
from src.data.clob_ws import ClobWebSocket
from src.data.market_discovery import MarketDiscovery
from src.data.market_manager import MarketManager
from src.data.orderbook import OrderBookManager
from src.data.spot_buffer import SpotBuffer
from src.execution.executor import OrderExecutor
from src.monitoring.logger import get_logger, setup_logging
from src.risk.manager import RiskManager
from src.strategy.arbitrage import ArbitrageStrategy
from src.strategy.asymmetric import AsymmetricStrategy
from src.strategy.base import BaseStrategy
from src.strategy.price_lag import ASSET_TO_BINANCE_SYMBOL, PriceLagStrategy
from src.strategy.scanner import MarketScanner
from src.utils.rate_limiter import RateLimiter

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------

_shutdown_event: asyncio.Event | None = None
_log = get_logger("main")
_pending_gtc_orders: list[dict] = []


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
    strategies: list[BaseStrategy] | None = None,
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
                            strategies=strategies or [],
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
    strategies: list[BaseStrategy] | None = None,
) -> None:
    """Build orders, acquire rate limit tokens, and execute.

    Handles arbitrage (two-leg YES+NO), asymmetric (GTC limit), and
    directional (single-leg FOK) strategies.
    """
    market = opp.market
    is_arb = opp.yes_fill is not None and opp.no_fill is not None
    is_asymmetric = opp.metadata.get("order_type") == "GTC"

    if is_arb:
        await _execute_arb_trade(
            opp, adjusted_size, executor, state_manager,
            risk_manager, rate_limiter, settings,
        )
    elif is_asymmetric:
        await _execute_asymmetric_trade(
            opp, adjusted_size, executor, state_manager,
            risk_manager, rate_limiter, settings, strategies or [],
        )
    else:
        await _execute_directional_trade(
            opp, adjusted_size, executor, state_manager,
            risk_manager, rate_limiter, settings,
        )


async def _execute_arb_trade(
    opp,
    adjusted_size: float,
    executor: OrderExecutor,
    state_manager: StateManager,
    risk_manager: RiskManager,
    rate_limiter: RateLimiter,
    settings: Settings,
) -> None:
    """Execute a two-leg arbitrage trade (YES + NO)."""
    market = opp.market

    yes_order = TradeOrder(
        token_id=market.yes_token_id,
        side=Side.BUY,
        price=opp.yes_fill.vwap,
        size=adjusted_size,
        order_type=settings.order_type,
    )
    no_order = TradeOrder(
        token_id=market.no_token_id,
        side=Side.BUY,
        price=opp.no_fill.vwap,
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
        await rate_limiter.acquire(4)
        yes_result, no_result = await executor.execute_arb(yes_order, no_order)
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
        _log.warning("rate_limit_timeout", market=market.slug)
    except Exception as exc:
        risk_manager.record_execution_failure()
        _log.error("execution_error", market=market.slug, error=str(exc))


async def _execute_directional_trade(
    opp,
    adjusted_size: float,
    executor: OrderExecutor,
    state_manager: StateManager,
    risk_manager: RiskManager,
    rate_limiter: RateLimiter,
    settings: Settings,
) -> None:
    """Execute a single-leg directional trade (price-lag, etc.)."""
    market = opp.market
    fill = opp.yes_fill or opp.no_fill
    if fill is None:
        return

    direction = opp.metadata.get("direction", "UP")
    target_token_id = opp.metadata.get("target_token_id", "")
    if not target_token_id:
        target_token_id = market.yes_token_id if direction == "UP" else market.no_token_id

    order = TradeOrder(
        token_id=target_token_id,
        side=Side.BUY,
        price=fill.vwap,
        size=adjusted_size,
        order_type=settings.order_type,
    )

    _log.info(
        "executing_directional",
        market=market.slug,
        strategy=opp.strategy.value,
        direction=direction,
        price=order.price,
        size=adjusted_size,
        expected_profit=round(opp.expected_profit, 4),
        dry_run=settings.dry_run,
    )

    try:
        await rate_limiter.acquire(2)  # 1 sign + 1 submit
        await executor.sign_order(order)
        result = await executor.submit_order(order)
        result = await executor.verify_fill(result)
        state_manager.record_trade(opp, [result])
        risk_manager.record_execution_success(market.condition_id)
        _log.info(
            "directional_complete",
            market=market.slug,
            direction=direction,
            status=result.status.value,
            daily_pnl=round(state_manager.daily_pnl().net_profit, 4),
        )
    except asyncio.TimeoutError:
        _log.warning("rate_limit_timeout", market=market.slug)
    except Exception as exc:
        risk_manager.record_execution_failure()
        _log.error("execution_error", market=market.slug, error=str(exc))


async def _execute_asymmetric_trade(
    opp,
    adjusted_size: float,
    executor: OrderExecutor,
    state_manager: StateManager,
    risk_manager: RiskManager,
    rate_limiter: RateLimiter,
    settings: Settings,
    strategies: list[BaseStrategy],
) -> None:
    """Place a GTC limit order for asymmetric accumulation (0% maker fee)."""
    market = opp.market
    fill = opp.yes_fill or opp.no_fill
    if fill is None:
        return

    buy_side = opp.metadata.get("buy_side", "YES")
    target_token_id = opp.metadata.get("target_token_id", "")
    buy_price = opp.metadata.get("buy_price", fill.vwap)

    order = TradeOrder(
        token_id=target_token_id,
        side=Side.BUY,
        price=buy_price,
        size=adjusted_size,
        order_type="GTC",
    )

    _log.info(
        "executing_asymmetric",
        market=market.slug,
        side=buy_side,
        price=order.price,
        size=adjusted_size,
        dry_run=settings.dry_run,
    )

    try:
        await rate_limiter.acquire(2)
        await executor.sign_order(order)
        result = await executor.submit_order(order)

        if result.status == OrderStatus.FILLED:
            # Dry-run: immediate fill
            _record_asymmetric_fill(
                strategies, market.condition_id, buy_side,
                result.fill_size or adjusted_size,
                (result.fill_price or buy_price) * (result.fill_size or adjusted_size),
            )
            state_manager.record_trade(opp, [result])
            risk_manager.record_execution_success(market.condition_id)
        elif result.status == OrderStatus.SUBMITTED and result.order_id:
            # Live: GTC order on the book, track for fill checking
            _pending_gtc_orders.append({
                "order": result,
                "condition_id": market.condition_id,
                "buy_side": buy_side,
                "price": buy_price,
                "size": adjusted_size,
                "submitted_at": time.time(),
                "opportunity": opp,
            })
            _log.info(
                "gtc_order_placed",
                order_id=result.order_id,
                market=market.slug,
                side=buy_side,
            )
        else:
            risk_manager.record_execution_failure()
    except asyncio.TimeoutError:
        _log.warning("rate_limit_timeout", market=market.slug)
    except Exception as exc:
        risk_manager.record_execution_failure()
        _log.error("execution_error", market=market.slug, error=str(exc))


def _record_asymmetric_fill(
    strategies: list[BaseStrategy],
    condition_id: str,
    side: str,
    shares: float,
    cost: float,
) -> None:
    """Find the AsymmetricStrategy and record a fill."""
    for strat in strategies:
        if isinstance(strat, AsymmetricStrategy):
            strat.record_fill(condition_id, side, shares, cost)
            break


# ---------------------------------------------------------------------------
# GTC order monitoring loop (asymmetric strategy)
# ---------------------------------------------------------------------------


async def _gtc_monitor_loop(
    executor: OrderExecutor,
    state_manager: StateManager,
    risk_manager: RiskManager,
    strategies: list[BaseStrategy],
    settings: Settings,
    interval: float = 5.0,
) -> None:
    """Periodically check pending GTC orders for fills and cancel stale ones."""
    while _shutdown_event is not None and not _shutdown_event.is_set():
        try:
            to_remove = []
            now = time.time()

            for entry in _pending_gtc_orders:
                order = entry["order"]
                age = now - entry["submitted_at"]

                # Check if filled (quick poll)
                checked = await executor.verify_fill(order, timeout=1.0, poll_interval=0.5)

                if checked.status == OrderStatus.FILLED:
                    to_remove.append(entry)
                    _record_asymmetric_fill(
                        strategies,
                        entry["condition_id"],
                        entry["buy_side"],
                        checked.fill_size or entry["size"],
                        (checked.fill_price or entry["price"])
                        * (checked.fill_size or entry["size"]),
                    )
                    state_manager.record_trade(entry["opportunity"], [checked])
                    risk_manager.record_execution_success(entry["condition_id"])
                    _log.info(
                        "gtc_fill_confirmed",
                        order_id=order.order_id,
                        side=entry["buy_side"],
                    )
                elif age > settings.stale_order_seconds:
                    to_remove.append(entry)
                    await executor.cancel_order(order.order_id)
                    _log.info(
                        "gtc_order_cancelled_stale",
                        order_id=order.order_id,
                        age=round(age, 1),
                    )

            for entry in to_remove:
                _pending_gtc_orders.remove(entry)

        except Exception as exc:
            _log.error("gtc_monitor_error", error=str(exc))

        try:
            await asyncio.wait_for(_shutdown_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


# ---------------------------------------------------------------------------
# Exit-check loop (for directional positions)
# ---------------------------------------------------------------------------


async def _exit_check_loop(
    strategies: list[BaseStrategy],
    market_manager: MarketManager,
    state_manager: StateManager,
    executor: OrderExecutor,
    rate_limiter: RateLimiter,
    settings: Settings,
    interval: float = 2.0,
) -> None:
    """Fast loop to check if directional positions should be exited.

    Runs every 2 seconds.  For each open position with a non-hedged
    strategy (e.g. price_lag), calls ``strategy.should_exit()`` and
    triggers a sell if True.
    """
    # Build a lookup from strategy_type to strategy instance
    strat_by_type: dict[StrategyType, BaseStrategy] = {
        s.strategy_type: s for s in strategies
    }

    while _shutdown_event is not None and not _shutdown_event.is_set():
        try:
            positions = state_manager.get_all_positions()
            for position in positions:
                # Only check exits for directional (non-hedged) positions
                if position.is_hedged:
                    continue

                strategy = strat_by_type.get(position.strategy)
                if strategy is None:
                    continue

                market = position.market
                # Verify market is still active
                if market_manager.get_market_for_asset(market.asset) is None:
                    continue

                if strategy.should_exit(position, market):
                    await _execute_exit(
                        position, market, executor, state_manager,
                        rate_limiter, settings,
                    )
        except Exception as exc:
            _log.error("exit_check_error", error=str(exc))

        try:
            await asyncio.wait_for(_shutdown_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def _execute_exit(
    position,
    market,
    executor: OrderExecutor,
    state_manager: StateManager,
    rate_limiter: RateLimiter,
    settings: Settings,
) -> None:
    """Sell all shares in a directional position."""
    orders = []
    if position.yes_shares > 0:
        orders.append(TradeOrder(
            token_id=market.yes_token_id,
            side=Side.SELL,
            price=0.01,  # market sell (lowest acceptable price)
            size=position.yes_shares,
            order_type="FOK",
        ))

    if position.no_shares > 0:
        orders.append(TradeOrder(
            token_id=market.no_token_id,
            side=Side.SELL,
            price=0.01,  # market sell
            size=position.no_shares,
            order_type="FOK",
        ))

    if not orders:
        return

    _log.info(
        "exiting_position",
        market=market.slug,
        strategy=position.strategy.value,
        yes_shares=position.yes_shares,
        no_shares=position.no_shares,
    )

    try:
        await rate_limiter.acquire(len(orders) * 2)
        signed = await executor.sign_orders_parallel(orders)
        results = await executor.submit_batch(signed)
        for result in results:
            await executor.verify_fill(result)

        _log.info(
            "position_exited",
            market=market.slug,
            results=[r.status.value for r in results],
        )
    except Exception as exc:
        _log.error("exit_execution_error", market=market.slug, error=str(exc))


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
    settings: Settings,
    book_manager: OrderBookManager,
    spot_buffer: SpotBuffer | None = None,
) -> list[BaseStrategy]:
    """Build the list of enabled strategies based on settings."""
    strategies: list[BaseStrategy] = []

    if settings.enable_arbitrage:
        strategies.append(ArbitrageStrategy(settings=settings, book_manager=book_manager))

    if settings.enable_price_lag and spot_buffer is not None:
        strategies.append(PriceLagStrategy(
            settings=settings, book_manager=book_manager, spot_buffer=spot_buffer,
        ))

    if settings.enable_asymmetric:
        strategies.append(AsymmetricStrategy(settings=settings, book_manager=book_manager))

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
        enable_price_lag=settings.enable_price_lag,
        enable_asymmetric=settings.enable_asymmetric,
        enable_multi_market=settings.enable_multi_market,
    )

    # Core components
    book_manager = OrderBookManager()
    state_manager = StateManager(settings)
    risk_manager = RiskManager(settings, state_manager)
    executor = OrderExecutor(settings)
    rate_limiter = RateLimiter(max_per_minute=55)
    spot_buffer = SpotBuffer(window_seconds=60)

    clob_ws = ClobWebSocket(
        ws_url=settings.clob_ws_url,
        book_manager=book_manager,
    )

    # Binance WebSocket for spot price feeds (needed by price-lag strategy)
    binance_symbols = [
        ASSET_TO_BINANCE_SYMBOL[a]
        for a in settings.markets
        if a in ASSET_TO_BINANCE_SYMBOL
    ]
    binance_ws = BinanceWebSocket(
        ws_url=settings.binance_ws_url,
        symbols=binance_symbols,
        spot_buffer=spot_buffer,
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
    strategies = _build_strategies(settings, book_manager, spot_buffer)
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
    binance_task = asyncio.create_task(binance_ws.run())

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
            strategies=strategies,
        ),
    )

    # GTC order monitoring (asymmetric strategy fill checking)
    gtc_task = asyncio.create_task(
        _gtc_monitor_loop(
            executor=executor,
            state_manager=state_manager,
            risk_manager=risk_manager,
            strategies=strategies,
            settings=settings,
            interval=5.0,
        ),
    )

    # Exit-check loop for directional positions (price-lag, etc.)
    exit_task = asyncio.create_task(
        _exit_check_loop(
            strategies=strategies,
            market_manager=market_manager,
            state_manager=state_manager,
            executor=executor,
            rate_limiter=rate_limiter,
            settings=settings,
            interval=2.0,
        ),
    )

    _log.info(
        "bot_running",
        active_markets=len(markets),
        strategies=scanner.strategy_names,
        binance_symbols=binance_symbols,
        msg="Press Ctrl+C to stop.",
    )

    # Wait for shutdown signal
    await _shutdown_event.wait()

    # Graceful shutdown
    _log.info("shutting_down")
    clob_ws.stop()
    binance_ws.stop()

    # Save state before exiting
    state_manager.save_snapshot()

    # Cancel tasks
    for ws in [ws_task, binance_task]:
        try:
            await asyncio.wait_for(ws, timeout=5.0)
        except asyncio.TimeoutError:
            ws.cancel()

    for task in [monitor_task, strategy_task, rollover_task, exit_task, gtc_task]:
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
