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
from src.core.models import OrderStatus, Position, Side, StrategyType, TradeOrder
from src.core.state import StateManager
from src.data.binance_ws import BinanceWebSocket
from src.data.clob_ws import ClobWebSocket
from src.data.market_discovery import MarketDiscovery
from src.data.market_manager import MarketManager
from src.data.orderbook import OrderBookManager
from src.data.spot_buffer import SpotBuffer
from src.execution.executor import OrderExecutor
from src.execution.unwind import EmergencyUnwind
from src.monitoring.alerts import AlertDispatcher
from src.monitoring.logger import get_logger, setup_logging
from src.monitoring.metrics import MetricsCollector
from src.risk.manager import RiskManager
from src.risk.sizing import PositionSizer
from src.strategy.arbitrage import ArbitrageStrategy
from src.strategy.asymmetric import AsymmetricStrategy
from src.strategy.base import BaseStrategy
from src.strategy.maker_arbitrage import ArbPair, MakerArbitrageStrategy
from src.strategy.price_lag import ASSET_TO_BINANCE_SYMBOL, PriceLagStrategy
from src.strategy.scanner import MarketScanner
from src.utils.fee_verifier import verify_fees
from src.utils.pid_lock import PidLock
from src.utils.rate_limiter import RateLimiter

# Dashboard (lazy — only used when dashboard_enabled)
from src.dashboard.app import configure_dashboard, create_app, start_dashboard
from src.data.trade_db import DailySnapshot, TradeDatabase, TradeResult

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------

_shutdown_event: asyncio.Event | None = None
_log = get_logger("main")
_pending_gtc_orders: list[dict] = []
_pending_maker_arb_pairs: list[dict] = []
_alerts: AlertDispatcher | None = None


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
    """Continuously scan all active markets for opportunities.

    Each cycle:
    1. Get current active markets from MarketManager
    2. Use MarketScanner to find opportunities
    3. Risk-check and size-adjust each opportunity
    4. Execute approved opportunities

    In normal mode: executes only the single best opportunity.
    In parallel mode (enable_parallel_strategies=True): executes
    the best opportunity from EACH strategy type for A/B testing.

    Runs every 2 seconds until the shutdown event is set.
    """
    while _shutdown_event is not None and not _shutdown_event.is_set():
        markets = market_manager.active_markets

        if not markets:
            _log.debug("no_active_markets")
        elif settings.enable_parallel_strategies:
            # A/B test mode: execute best opportunity from each strategy type
            await _execute_parallel_strategies(
                scanner=scanner,
                markets=markets,
                risk_manager=risk_manager,
                executor=executor,
                state_manager=state_manager,
                rate_limiter=rate_limiter,
                settings=settings,
                strategies=strategies or [],
            )
        else:
            # Normal mode: execute only the single best opportunity
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


async def _execute_parallel_strategies(
    scanner: MarketScanner,
    markets: list,
    risk_manager: RiskManager,
    executor: OrderExecutor,
    state_manager: StateManager,
    rate_limiter: RateLimiter,
    settings: Settings,
    strategies: list[BaseStrategy],
) -> None:
    """Execute the best opportunity from each strategy type (A/B test mode).

    This allows multiple strategies to trade in the same cycle, enabling
    fair comparison of strategy performance over time.
    """
    best_per_strategy = await scanner.scan_best_per_strategy(markets)

    if not best_per_strategy:
        return

    _log.info(
        "parallel_strategies_found",
        strategies=[s.value for s in best_per_strategy.keys()],
        count=len(best_per_strategy),
    )

    # Execute each strategy's best opportunity
    for strat_type, opp in best_per_strategy.items():
        market = opp.market

        # Risk check
        approved, reason = risk_manager.check_opportunity(opp)
        if not approved:
            _log.debug(
                "opportunity_rejected",
                strategy=strat_type.value,
                market=market.slug,
                reason=reason,
            )
            continue

        # Adjust size
        adjusted_size = risk_manager.adjust_size(opp, settings.order_size)
        if adjusted_size <= 0:
            _log.debug(
                "size_adjusted_to_zero",
                strategy=strat_type.value,
                market=market.slug,
            )
            continue

        _log.info(
            "executing_parallel_strategy",
            strategy=strat_type.value,
            market=market.slug,
            profit_pct=round(opp.expected_profit_pct, 6),
        )

        await _execute_opportunity(
            opp=opp,
            adjusted_size=adjusted_size,
            executor=executor,
            state_manager=state_manager,
            risk_manager=risk_manager,
            rate_limiter=rate_limiter,
            settings=settings,
            strategies=strategies,
        )


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

    Handles arbitrage (two-leg YES+NO), asymmetric (GTC limit),
    maker arbitrage (paired GTC), and directional (single-leg FOK) strategies.
    """
    is_arb = opp.yes_fill is not None and opp.no_fill is not None
    is_maker_arb = opp.metadata.get("paired", False)
    is_asymmetric = opp.metadata.get("order_type") == "GTC" and not is_maker_arb

    if is_maker_arb:
        await _execute_maker_arb_trade(
            opp, adjusted_size, executor, state_manager,
            risk_manager, rate_limiter, settings, strategies or [],
        )
    elif is_arb:
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

        # Detect partial fill — one leg filled, the other didn't
        yes_filled = yes_result.status == OrderStatus.FILLED
        no_filled = no_result.status == OrderStatus.FILLED
        if yes_filled != no_filled:
            _log.warning(
                "partial_arb_detected",
                market=market.slug,
                yes_status=yes_result.status.value,
                no_status=no_result.status.value,
            )
            unwind = EmergencyUnwind(executor, state_manager)
            filled = yes_result if yes_filled else no_result
            unfilled = no_result if yes_filled else yes_result
            await unwind.unwind_partial_arb(filled, unfilled)
            risk_manager.record_execution_failure()
            if _alerts and settings.alert_on_error:
                await _alerts.send_error(
                    f"Partial arb fill {market.slug}: unwind triggered"
                )
            return

        await state_manager.record_trade(opp, [yes_result, no_result])
        risk_manager.record_execution_success(market.condition_id)
        _log.info(
            "arb_complete",
            market=market.slug,
            yes_status=yes_result.status.value,
            no_status=no_result.status.value,
            daily_pnl=round(state_manager.daily_pnl().net_profit, 4),
        )
        if _alerts and settings.alert_on_trade:
            await _alerts.send_trade(
                f"Arb: {market.slug} YES@{yes_order.price:.2f}+NO@{no_order.price:.2f} "
                f"x{adjusted_size:.0f} profit={opp.expected_profit:.4f}"
            )
    except asyncio.TimeoutError:
        _log.warning("rate_limit_timeout", market=market.slug)
    except Exception as exc:
        risk_manager.record_execution_failure()
        _log.error("execution_error", market=market.slug, error=str(exc))
        if _alerts and settings.alert_on_error:
            await _alerts.send_error(f"Arb exec error {market.slug}: {exc}")


async def _execute_maker_arb_trade(
    opp,
    adjusted_size: float,
    executor: OrderExecutor,
    state_manager: StateManager,
    risk_manager: RiskManager,
    rate_limiter: RateLimiter,
    settings: Settings,
    strategies: list[BaseStrategy],
) -> None:
    """Execute a paired GTC limit order arbitrage (0% maker fee).

    Places YES and NO limit orders below best ask. Orders are tracked
    and monitored by _maker_arb_monitor_loop().
    """
    market = opp.market
    yes_price = opp.metadata.get("yes_price", 0.0)
    no_price = opp.metadata.get("no_price", 0.0)

    if yes_price <= 0 or no_price <= 0:
        _log.error("maker_arb_invalid_prices", market=market.slug)
        return

    # Find the MakerArbitrageStrategy to register the pair
    maker_strat: MakerArbitrageStrategy | None = None
    for strat in strategies:
        if isinstance(strat, MakerArbitrageStrategy):
            maker_strat = strat
            break

    if maker_strat is None:
        _log.error("maker_arb_strategy_not_found", market=market.slug)
        return

    # Create YES and NO orders with GTC type
    yes_order = TradeOrder(
        token_id=market.yes_token_id,
        side=Side.BUY,
        price=yes_price,
        size=adjusted_size,
        order_type="GTC",
    )
    no_order = TradeOrder(
        token_id=market.no_token_id,
        side=Side.BUY,
        price=no_price,
        size=adjusted_size,
        order_type="GTC",
    )

    _log.info(
        "executing_maker_arb",
        market=market.slug,
        yes_price=yes_price,
        no_price=no_price,
        size=adjusted_size,
        combined=round(yes_price + no_price, 4),
        expected_profit=round(opp.expected_profit, 4),
        dry_run=settings.dry_run,
    )

    try:
        # Acquire rate limit tokens (4: 2 signs + 2 submits)
        await rate_limiter.acquire(4)

        # Sign both orders in parallel
        yes_order, no_order = await executor.sign_orders_parallel(
            [yes_order, no_order]
        )

        # Check both signed successfully
        if (
            yes_order.status != OrderStatus.SIGNED
            or no_order.status != OrderStatus.SIGNED
        ):
            _log.warning(
                "maker_arb_sign_failed",
                market=market.slug,
                yes_status=yes_order.status.value,
                no_status=no_order.status.value,
            )
            risk_manager.record_execution_failure()
            return

        # Submit YES order first
        yes_result = await executor.submit_order(yes_order)
        if yes_result.status == OrderStatus.REJECTED:
            _log.warning("maker_arb_yes_rejected", market=market.slug)
            risk_manager.record_execution_failure()
            return

        # Submit NO order
        no_result = await executor.submit_order(no_order)
        if no_result.status == OrderStatus.REJECTED:
            # YES was submitted but NO rejected - need to cancel YES
            _log.warning(
                "maker_arb_no_rejected_cancelling_yes",
                market=market.slug,
                yes_order_id=yes_result.order_id,
            )
            if yes_result.order_id:
                await executor.cancel_order(yes_result.order_id)
            risk_manager.record_execution_failure()
            return

        # Create and track the arb pair
        pair = maker_strat.create_pair(
            condition_id=market.condition_id,
            yes_price=yes_price,
            no_price=no_price,
            size=adjusted_size,
        )
        pair.yes_order_id = yes_result.order_id
        pair.no_order_id = no_result.order_id

        # Handle dry-run immediate fills
        if settings.dry_run:
            pair.yes_filled = True
            pair.no_filled = True
            pair.yes_fill_size = adjusted_size
            pair.no_fill_size = adjusted_size
            pair.status = "complete"
            await state_manager.record_trade(opp, [yes_result, no_result])
            risk_manager.record_execution_success(market.condition_id)
            _log.info(
                "maker_arb_complete_dry",
                market=market.slug,
                pair_id=pair.pair_id,
            )
            if _alerts and settings.alert_on_trade:
                await _alerts.send_trade(
                    f"MakerArb: {market.slug} YES@{yes_price:.2f}+NO@{no_price:.2f} "
                    f"x{adjusted_size:.0f} profit={opp.expected_profit:.4f}"
                )
            return

        # Live mode: track for monitoring
        _pending_maker_arb_pairs.append({
            "pair": pair,
            "yes_order": yes_result,
            "no_order": no_result,
            "opportunity": opp,
            "market": market,
        })

        _log.info(
            "maker_arb_submitted",
            market=market.slug,
            pair_id=pair.pair_id,
            yes_order_id=yes_result.order_id,
            no_order_id=no_result.order_id,
        )

    except asyncio.TimeoutError:
        _log.warning("rate_limit_timeout", market=market.slug)
    except Exception as exc:
        risk_manager.record_execution_failure()
        _log.error("maker_arb_execution_error", market=market.slug, error=str(exc))
        if _alerts and settings.alert_on_error:
            await _alerts.send_error(f"Maker arb exec error {market.slug}: {exc}")


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
        if order.status == OrderStatus.REJECTED:
            risk_manager.record_execution_failure()
            _log.warning("directional_sign_rejected", market=market.slug)
            return
        result = await executor.submit_order(order)
        result = await executor.verify_fill(result)
        await state_manager.record_trade(opp, [result])
        if result.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
            risk_manager.record_execution_success(market.condition_id)
        else:
            risk_manager.record_execution_failure()
        _log.info(
            "directional_complete",
            market=market.slug,
            direction=direction,
            status=result.status.value,
            daily_pnl=round(state_manager.daily_pnl().net_profit, 4),
        )
        if _alerts and settings.alert_on_trade and result.status == OrderStatus.FILLED:
            await _alerts.send_trade(
                f"Directional: {market.slug} {direction} @{order.price:.2f} "
                f"x{adjusted_size:.0f}"
            )
    except asyncio.TimeoutError:
        risk_manager.record_execution_failure()
        _log.warning("rate_limit_timeout", market=market.slug)
    except Exception as exc:
        risk_manager.record_execution_failure()
        _log.error("execution_error", market=market.slug, error=str(exc))
        if _alerts and settings.alert_on_error:
            await _alerts.send_error(f"Directional exec error {market.slug}: {exc}")


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
            await state_manager.record_trade(opp, [result])
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
                    await state_manager.record_trade(entry["opportunity"], [checked])
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
# Maker arbitrage monitoring loop
# ---------------------------------------------------------------------------


async def _maker_arb_monitor_loop(
    executor: OrderExecutor,
    state_manager: StateManager,
    risk_manager: RiskManager,
    strategies: list[BaseStrategy],
    settings: Settings,
    interval: float = 5.0,
) -> None:
    """Monitor pending maker arb pairs for fills and handle timeouts.

    Checks each pending pair every interval seconds:
    - If both legs filled: complete, record trade
    - If timeout with no fills: cancel both orders
    - If timeout with partial fill: cancel unfilled, unwind filled
    """
    # Find the MakerArbitrageStrategy instance
    maker_strat: MakerArbitrageStrategy | None = None
    for strat in strategies:
        if isinstance(strat, MakerArbitrageStrategy):
            maker_strat = strat
            break

    if maker_strat is None:
        _log.debug("maker_arb_monitor_no_strategy")
        return

    while _shutdown_event is not None and not _shutdown_event.is_set():
        try:
            to_remove: list[dict] = []

            for entry in _pending_maker_arb_pairs:
                pair: ArbPair = entry["pair"]
                yes_order: TradeOrder = entry["yes_order"]
                no_order: TradeOrder = entry["no_order"]
                opp = entry["opportunity"]
                market = entry["market"]

                # Check YES fill status
                if not pair.yes_filled and yes_order.order_id:
                    checked = await executor.verify_fill(
                        yes_order, timeout=1.0, poll_interval=0.5
                    )
                    if checked.status == OrderStatus.FILLED:
                        maker_strat.record_fill(
                            pair.pair_id, "YES",
                            checked.fill_size or pair.size,
                        )
                        yes_order.status = OrderStatus.FILLED
                        yes_order.fill_size = checked.fill_size or pair.size
                        yes_order.fill_price = checked.fill_price or pair.yes_price

                # Check NO fill status
                if not pair.no_filled and no_order.order_id:
                    checked = await executor.verify_fill(
                        no_order, timeout=1.0, poll_interval=0.5
                    )
                    if checked.status == OrderStatus.FILLED:
                        maker_strat.record_fill(
                            pair.pair_id, "NO",
                            checked.fill_size or pair.size,
                        )
                        no_order.status = OrderStatus.FILLED
                        no_order.fill_size = checked.fill_size or pair.size
                        no_order.fill_price = checked.fill_price or pair.no_price

                # If both filled: complete
                if pair.is_complete:
                    to_remove.append(entry)
                    await state_manager.record_trade(opp, [yes_order, no_order])
                    risk_manager.record_execution_success(market.condition_id)
                    _log.info(
                        "maker_arb_complete",
                        market=market.slug,
                        pair_id=pair.pair_id,
                        daily_pnl=round(state_manager.daily_pnl().net_profit, 4),
                    )
                    if _alerts and settings.alert_on_trade:
                        await _alerts.send_trade(
                            f"MakerArb: {market.slug} "
                            f"YES@{pair.yes_price:.2f}+NO@{pair.no_price:.2f} "
                            f"x{pair.size:.0f} profit={opp.expected_profit:.4f}"
                        )
                    continue

                # Check for timeout
                if pair.age_seconds > settings.maker_pair_timeout_seconds:
                    to_remove.append(entry)
                    await _handle_maker_arb_timeout(
                        pair, yes_order, no_order, executor,
                        maker_strat, state_manager, risk_manager, market,
                    )

            # Clean up processed entries
            for entry in to_remove:
                _pending_maker_arb_pairs.remove(entry)
                maker_strat.remove_pair(entry["pair"].pair_id)

        except Exception as exc:
            _log.error("maker_arb_monitor_error", error=str(exc))

        try:
            await asyncio.wait_for(_shutdown_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def _handle_maker_arb_timeout(
    pair: ArbPair,
    yes_order: TradeOrder,
    no_order: TradeOrder,
    executor: OrderExecutor,
    maker_strat: MakerArbitrageStrategy,
    state_manager: StateManager,
    risk_manager: RiskManager,
    market,
) -> None:
    """Handle a timed-out maker arb pair.

    Scenarios:
    - Neither filled: cancel both
    - YES filled, NO not: cancel NO, sell YES (unwind)
    - NO filled, YES not: cancel YES, sell NO (unwind)
    """
    _log.warning(
        "maker_arb_timeout",
        pair_id=pair.pair_id,
        market=market.slug,
        yes_filled=pair.yes_filled,
        no_filled=pair.no_filled,
        age=round(pair.age_seconds, 1),
    )

    if not pair.yes_filled and not pair.no_filled:
        # Neither filled - just cancel both
        if yes_order.order_id:
            await executor.cancel_order(yes_order.order_id)
        if no_order.order_id:
            await executor.cancel_order(no_order.order_id)
        maker_strat.cancel_pair(pair.pair_id, status="cancelled")
        _log.info("maker_arb_cancelled_no_fills", pair_id=pair.pair_id)
        return

    # Partial fill - need to unwind
    if pair.yes_filled and not pair.no_filled:
        # YES filled, NO not filled - cancel NO, sell YES
        if no_order.order_id:
            await executor.cancel_order(no_order.order_id)
        filled_side = "YES"
        filled_token_id = market.yes_token_id
        filled_size = pair.yes_fill_size or pair.size
    else:
        # NO filled, YES not filled - cancel YES, sell NO
        if yes_order.order_id:
            await executor.cancel_order(yes_order.order_id)
        filled_side = "NO"
        filled_token_id = market.no_token_id
        filled_size = pair.no_fill_size or pair.size

    _log.warning(
        "maker_arb_partial_unwind",
        pair_id=pair.pair_id,
        market=market.slug,
        filled_side=filled_side,
        filled_size=filled_size,
    )

    # Create FOK sell order to unwind
    sell_order = TradeOrder(
        token_id=filled_token_id,
        side=Side.SELL,
        price=0.01,  # Market sell (lowest acceptable)
        size=filled_size,
        order_type="FOK",
    )

    try:
        await executor.sign_order(sell_order)
        if sell_order.status == OrderStatus.SIGNED:
            result = await executor.submit_order(sell_order)
            result = await executor.verify_fill(result)

            if result.status == OrderStatus.FILLED:
                _log.info(
                    "maker_arb_unwind_complete",
                    pair_id=pair.pair_id,
                    filled_side=filled_side,
                )
            else:
                _log.error(
                    "maker_arb_unwind_failed",
                    pair_id=pair.pair_id,
                    status=result.status.value,
                )
                if _alerts:
                    await _alerts.send_error(
                        f"Maker arb unwind failed: {market.slug} {filled_side}"
                    )
    except Exception as exc:
        _log.error(
            "maker_arb_unwind_error",
            pair_id=pair.pair_id,
            error=str(exc),
        )

    maker_strat.cancel_pair(pair.pair_id, status="unwound")
    risk_manager.record_execution_failure()


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
    book_manager: OrderBookManager,
    trade_db: TradeDatabase | None = None,
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
                        rate_limiter, settings, book_manager, trade_db,
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
    book_manager: OrderBookManager,
    trade_db: TradeDatabase | None = None,
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

        # Compute sell proceeds from orderbook best bids (accurate for
        # both live and DRY_RUN; the FOK fill_price of $0.01 is just the
        # floor, not the real execution price).
        sell_proceeds = 0.0
        if position.yes_shares > 0:
            yes_book = book_manager.get_book(market.yes_token_id)
            bid = yes_book.best_bid if yes_book and yes_book.best_bid else 0.0
            sell_proceeds += position.yes_shares * bid
        if position.no_shares > 0:
            no_book = book_manager.get_book(market.no_token_id)
            bid = no_book.best_bid if no_book and no_book.best_bid else 0.0
            sell_proceeds += position.no_shares * bid

        total_shares = position.yes_shares + position.no_shares
        payout_per_share = sell_proceeds / total_shares if total_shares > 0 else 0.0

        # Close the position in state to prevent repeated exit signals
        # and double-counting at market resolution.
        net_profit = sell_proceeds - position.total_investment
        from src.utils.fees import WINNER_FEE_RATE
        actual_winner_fee = WINNER_FEE_RATE * max(0.0, net_profit)
        net_profit -= actual_winner_fee

        try:
            state_manager.close_position(market.condition_id, payout_per_share)
        except KeyError:
            pass  # Already closed by resolution loop

        # Persist early exit as a trade_result so it appears on the dashboard
        if trade_db is not None:
            trade_db.save_trade_result(TradeResult(
                timestamp=time.time(),
                condition_id=market.condition_id,
                market_slug=market.slug,
                asset=market.asset,
                strategy=position.strategy.value,
                was_hedged=position.is_hedged,
                yes_shares=position.yes_shares,
                no_shares=position.no_shares,
                investment=position.total_investment,
                gross_payout=sell_proceeds,
                net_profit=net_profit,
                outcome="early_exit",
            ))

        _log.info(
            "position_exited",
            market=market.slug,
            results=[r.status.value for r in results],
            sell_proceeds=round(sell_proceeds, 4),
            net_profit=round(net_profit, 4),
            payout_per_share=round(payout_per_share, 4),
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
    metrics: MetricsCollector,
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
        dashboard = metrics.compute_dashboard(pnl, state_manager.sim_balance)
        metrics.log_dashboard(dashboard)

        _log.info(
            "monitor_status",
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
# Daily summary loop
# ---------------------------------------------------------------------------


async def _daily_summary_loop(
    state_manager: StateManager,
    metrics: MetricsCollector,
    settings: Settings,
    interval: float = 60.0,
) -> None:
    """Send daily summary at the configured UTC hour."""
    last_summary_date = ""
    while _shutdown_event is not None and not _shutdown_event.is_set():
        try:
            from datetime import datetime, timezone

            now = datetime.now(tz=timezone.utc)
            today = now.strftime("%Y-%m-%d")
            if now.hour == settings.daily_summary_hour and today != last_summary_date:
                pnl = state_manager.daily_pnl()
                dashboard = metrics.compute_dashboard(pnl, state_manager.sim_balance)
                summary = metrics.format_daily_summary(dashboard)
                if _alerts:
                    await _alerts.send_daily_summary(summary)
                _log.info("daily_summary_sent", date=today)
                last_summary_date = today
        except Exception as exc:
            _log.error("daily_summary_error", error=str(exc))

        try:
            await asyncio.wait_for(_shutdown_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


# ---------------------------------------------------------------------------
# Dashboard snapshot loop
# ---------------------------------------------------------------------------


async def _snapshot_loop(
    state_manager: StateManager,
    trade_db: TradeDatabase,
    interval: float = 60.0,
) -> None:
    """Periodically save daily P&L snapshots to SQLite for the equity curve."""
    while _shutdown_event is not None and not _shutdown_event.is_set():
        try:
            from datetime import date as _date

            pnl = state_manager.daily_pnl()
            snapshot = DailySnapshot(
                date=_date.today().isoformat(),
                trades=pnl.trades,
                gross_profit=pnl.gross_profit,
                net_profit=pnl.net_profit,
                total_fees=pnl.total_fees,
                win_count=pnl.win_count,
                loss_count=pnl.loss_count,
                max_drawdown=pnl.max_drawdown,
                sim_balance=state_manager.sim_balance,
                opportunities_seen=pnl.opportunities_seen,
                opportunities_taken=pnl.opportunities_taken,
            )
            trade_db.save_daily_snapshot(snapshot)
        except Exception as exc:
            _log.error("snapshot_loop_error", error=str(exc))

        try:
            await asyncio.wait_for(_shutdown_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


# ---------------------------------------------------------------------------
# Position Resolution loop
# ---------------------------------------------------------------------------


async def _resolution_loop(
    state_manager: StateManager,
    spot_buffer: SpotBuffer | None = None,
    trade_db: TradeDatabase | None = None,
    interval: float = 15.0,
) -> None:
    """Periodically check for and resolve expired positions.

    Runs every *interval* seconds (default 15s) to detect positions
    whose markets have expired and resolve them appropriately.

    For hedged positions: guaranteed $1 per share pair.
    For unhedged positions: uses spot price data to infer outcome.
    """

    def _outcome_resolver(pos: Position) -> float | None:
        """Determine if YES or NO won based on spot price movement.

        Returns >0.5 if YES won (price went up), <0.5 if NO won (price went down).
        Returns None if unable to determine.
        """
        if spot_buffer is None:
            return None

        asset = pos.market.asset
        symbol = f"{asset}USDT"

        # Get price at market start and end
        start_ts = pos.market.start_time.timestamp()
        end_ts = pos.market.end_time.timestamp()

        # Get historical prices from buffer
        history = spot_buffer.get_price_history(symbol)
        if not history:
            _log.warning(
                "no_spot_history_for_resolution",
                symbol=symbol,
                condition_id=pos.market.condition_id,
            )
            return None

        # Find prices closest to start and end times
        start_price = None
        end_price = None

        for ts, price in history:
            if start_price is None or abs(ts - start_ts) < abs(start_price[0] - start_ts):
                start_price = (ts, price)
            if end_price is None or abs(ts - end_ts) < abs(end_price[0] - end_ts):
                end_price = (ts, price)

        if start_price is None or end_price is None:
            _log.warning(
                "incomplete_spot_history",
                symbol=symbol,
                has_start=start_price is not None,
                has_end=end_price is not None,
            )
            return None

        # Determine outcome: YES wins if price went up
        price_went_up = end_price[1] > start_price[1]

        _log.debug(
            "outcome_resolved_from_spot",
            symbol=symbol,
            start_price=start_price[1],
            end_price=end_price[1],
            outcome="YES" if price_went_up else "NO",
        )

        return 1.0 if price_went_up else 0.0

    while _shutdown_event is not None and not _shutdown_event.is_set():
        try:
            resolved = await state_manager.resolve_expired_positions(
                outcome_resolver=_outcome_resolver,
            )
            if resolved:
                if trade_db is not None:
                    for report in resolved:
                        trade_db.save_trade_result(TradeResult(
                            timestamp=time.time(),
                            condition_id=report["condition_id"],
                            market_slug=report.get("slug", ""),
                            asset=report.get("asset", ""),
                            strategy=report.get("strategy", ""),
                            was_hedged=report.get("was_hedged", False),
                            yes_shares=report.get("yes_shares", 0.0),
                            no_shares=report.get("no_shares", 0.0),
                            investment=report.get("investment", 0.0),
                            gross_payout=report.get("gross_payout", 0.0),
                            net_profit=report.get("net_profit", 0.0),
                            outcome=report.get("outcome", ""),
                        ))
                _log.info(
                    "resolution_loop_completed",
                    resolved_count=len(resolved),
                    positions_remaining=len(state_manager.get_all_positions()),
                )
        except Exception as exc:
            _log.error("resolution_loop_error", error=str(exc))

        try:
            await asyncio.wait_for(_shutdown_event.wait(), timeout=interval)
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

    if settings.enable_maker_arbitrage:
        strategies.append(MakerArbitrageStrategy(settings=settings, book_manager=book_manager))

    return strategies


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def async_main() -> None:
    """Async entry point: discover, subscribe, evaluate, execute."""
    global _shutdown_event, _alerts
    _shutdown_event = asyncio.Event()
    pid_lock: PidLock | None = None

    # Register signal handlers for graceful shutdown
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_shutdown)
        except NotImplementedError:
            # Windows: fall back to signal.signal for graceful shutdown
            signal.signal(sig, lambda s, f: _request_shutdown())

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

    # PID lock — prevent duplicate instances
    pid_lock = PidLock(settings.pid_lock_path)
    if not pid_lock.acquire():
        _log.error("pid_lock_failed", msg="Another instance is already running.")
        return

    try:
        await _run_bot(settings, pid_lock)
    finally:
        # Always release PID lock
        if pid_lock is not None:
            pid_lock.release()
        # Close alert clients
        if _alerts is not None:
            await _alerts.close()
            _alerts = None


async def _run_bot(settings: Settings, pid_lock: PidLock) -> None:
    """Core bot logic, separated for clean PID lock management."""
    global _alerts

    _log.info(
        "bot_starting",
        dry_run=settings.dry_run,
        markets=settings.markets,
        order_size=settings.order_size,
        target_pair_cost=settings.target_pair_cost,
        enable_arbitrage=settings.enable_arbitrage,
        enable_price_lag=settings.enable_price_lag,
        enable_asymmetric=settings.enable_asymmetric,
        enable_maker_arbitrage=settings.enable_maker_arbitrage,
        enable_multi_market=settings.enable_multi_market,
        enable_parallel_strategies=settings.enable_parallel_strategies,
    )

    # Alert dispatcher
    _alerts = AlertDispatcher.from_settings(settings)

    # Fee verification at startup
    fee_errors = verify_fees()
    if any(e.severity == "error" for e in fee_errors):
        _log.error("fee_verification_has_errors", count=len(fee_errors))
        if _alerts:
            await _alerts.send_error(
                f"Fee verification: {len(fee_errors)} issue(s) detected at startup"
            )

    # Metrics collector
    metrics = MetricsCollector()

    # Core components
    book_manager = OrderBookManager()
    state_manager = StateManager(settings)
    risk_manager = RiskManager(settings, state_manager)
    executor = OrderExecutor(settings)
    rate_limiter = RateLimiter(max_per_minute=55)
    spot_buffer = SpotBuffer(window_seconds=60)

    # Trade database + dashboard (if enabled)
    trade_db: TradeDatabase | None = None
    dashboard_server = None
    if settings.dashboard_enabled:
        trade_db = TradeDatabase(settings.db_path)
        state_manager.set_trade_db(trade_db)

    # Kelly position sizer
    sizer = PositionSizer(
        kelly_fraction=settings.kelly_fraction,
        min_size=5.0,
        max_size=settings.max_position_per_market,
    )
    risk_manager.set_sizer(sizer)

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

    # Startup recovery (replaces load_snapshot)
    recovery_report = state_manager.startup_recovery(settings.state_snapshot_path)
    _log.info("startup_recovery", **recovery_report)

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

    # Dashboard setup (if enabled)
    dashboard_task = None
    snapshot_task = None
    if settings.dashboard_enabled:
        dashboard_app = create_app()
        configure_dashboard(
            app=dashboard_app,
            state_manager=state_manager,
            book_manager=book_manager,
            market_manager=market_manager,
            risk_manager=risk_manager,
            metrics=metrics,
            settings=settings,
            trade_db=trade_db,
            spot_buffer=spot_buffer,
        )
        dashboard_server = await start_dashboard(dashboard_app, settings)
        dashboard_task = asyncio.create_task(dashboard_server.serve())
        _log.info(
            "dashboard_started",
            url=f"http://{settings.dashboard_host}:{settings.dashboard_port}",
        )

        if trade_db is not None:
            snapshot_task = asyncio.create_task(
                _snapshot_loop(state_manager, trade_db, interval=60.0)
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
            metrics=metrics,
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

    # Maker arbitrage monitoring (paired GTC orders)
    maker_arb_task = asyncio.create_task(
        _maker_arb_monitor_loop(
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
            book_manager=book_manager,
            trade_db=trade_db,
            interval=2.0,
        ),
    )

    # Daily summary loop
    summary_task = asyncio.create_task(
        _daily_summary_loop(
            state_manager=state_manager,
            metrics=metrics,
            settings=settings,
            interval=60.0,
        ),
    )

    # Position resolution loop (handles expired markets)
    resolution_task = asyncio.create_task(
        _resolution_loop(
            state_manager=state_manager,
            spot_buffer=spot_buffer,
            trade_db=trade_db,
            interval=15.0,  # Check every 15 seconds
        ),
    )

    _log.info(
        "bot_running",
        active_markets=len(markets),
        strategies=scanner.strategy_names,
        binance_symbols=binance_symbols,
        alert_sinks=_alerts.sink_count if _alerts else 0,
        msg="Press Ctrl+C to stop.",
    )

    # Wait for shutdown signal
    assert _shutdown_event is not None
    await _shutdown_event.wait()

    # Graceful shutdown
    _log.info("shutting_down")
    await clob_ws.stop()
    await binance_ws.stop()

    # Cancel all pending GTC orders (asymmetric)
    for entry in _pending_gtc_orders:
        try:
            order = entry["order"]
            if order.order_id:
                await executor.cancel_order(order.order_id)
        except Exception as exc:
            _log.warning("gtc_cancel_error", error=str(exc))
    _pending_gtc_orders.clear()

    # Cancel all pending maker arb pairs
    for entry in _pending_maker_arb_pairs:
        try:
            pair: ArbPair = entry["pair"]
            yes_order = entry["yes_order"]
            no_order = entry["no_order"]
            if yes_order.order_id:
                await executor.cancel_order(yes_order.order_id)
            if no_order.order_id:
                await executor.cancel_order(no_order.order_id)
            _log.info("maker_arb_shutdown_cancel", pair_id=pair.pair_id)
        except Exception as exc:
            _log.warning("maker_arb_cancel_error", error=str(exc))
    _pending_maker_arb_pairs.clear()

    # Flatten unhedged directional positions on shutdown
    unhedged = [p for p in state_manager.get_all_positions() if not p.is_hedged]
    if unhedged:
        _log.warning("shutdown_unwind_unhedged", count=len(unhedged))
        unwinder = EmergencyUnwind(executor, state_manager)
        for pos in unhedged:
            try:
                await unwinder.unwind_position(pos)
            except Exception as exc:
                _log.error(
                    "shutdown_unwind_error",
                    condition_id=pos.market.condition_id,
                    error=str(exc),
                )

    # Shut down dashboard server
    if dashboard_server is not None:
        dashboard_server.should_exit = True

    # Cancel non-WS tasks first so no trades are in-flight during snapshot
    all_tasks = [
        monitor_task, strategy_task, rollover_task,
        exit_task, gtc_task, maker_arb_task, summary_task,
        dashboard_task, snapshot_task, resolution_task,
    ]
    for task in all_tasks:
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    # Save state after all strategy/execution tasks are cancelled
    state_manager.save_snapshot(settings.state_snapshot_path)

    # Close WS connections
    for ws in [ws_task, binance_task]:
        try:
            await asyncio.wait_for(ws, timeout=5.0)
        except asyncio.TimeoutError:
            ws.cancel()

    # Close HTTP clients and trade database
    await discovery.close()
    if trade_db is not None:
        trade_db.close()

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
