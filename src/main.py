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
from datetime import datetime, timezone

from src.config import Settings
from src.core.models import OrderStatus, Position, Side, StrategyType, TradeOrder
from src.core.state import StateManager
from src.data.alpha_signals import AlphaSignalProvider
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
from src.risk.allocation import AllocationManager
from src.risk.manager import RiskManager
from src.risk.sizing import PositionSizer
from src.strategy.arbitrage import ArbitrageStrategy
from src.strategy.asymmetric import AsymmetricStrategy
from src.strategy.base import BaseStrategy
from src.strategy.dip_buyer import DipBuyerStrategy
from src.strategy.fade_panic import FadePanicStrategy
from src.strategy.hedged_mm import HedgedMMStrategy, HMMPair
from src.strategy.maker_arbitrage import ArbPair, MakerArbitrageStrategy
from src.strategy.price_lag import ASSET_TO_BINANCE_SYMBOL, PriceLagStrategy
from src.strategy.resolution_sniper import ResolutionSniperStrategy
from src.strategy.cross_asset import CrossAssetCorrelationStrategy
from src.strategy.scanner import MarketScanner
from src.utils.fee_verifier import verify_fees
from src.utils.pid_lock import PidLock
from src.utils.rate_limiter import RateLimiter
from src.dashboard.app import configure_dashboard, create_app, start_dashboard
from src.data.decision_logger import DecisionLogger
from src.data.trade_db import (
    DailySnapshot,
    MarketOutcome,
    SpotSnapshot,
    TradeDatabase,
    TradeResult,
)

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------

_shutdown_event: asyncio.Event | None = None
_log = get_logger("main")
_pending_gtc_orders: list[dict] = []
_pending_maker_arb_pairs: list[dict] = []
_pending_hmm_pairs: list[dict] = []
_alerts: AlertDispatcher | None = None


# ---------------------------------------------------------------------------
# Time-based strategy schedule
# ---------------------------------------------------------------------------

_last_schedule_period: str | None = None  # track for transition logging


def _filter_strategies_by_schedule(
    strategies: list[BaseStrategy],
    settings: Settings,
) -> list[BaseStrategy]:
    """Filter strategies based on time-of-day schedule.

    When ``enable_strategy_schedule`` is True, only strategies whose name
    appears in the current period's allowed list are returned.  Logs a
    message on period transitions (day ↔ night).
    """
    global _last_schedule_period

    if not settings.enable_strategy_schedule:
        return strategies

    now = datetime.now(tz=timezone.utc)
    hour = now.hour

    day_start = settings.schedule_day_start_utc
    night_start = settings.schedule_night_start_utc

    # Determine if current hour is in "day" period (handles midnight wrap)
    if day_start < night_start:
        is_day = day_start <= hour < night_start
    else:
        # Wraps around midnight: e.g., day=13, night=1
        is_day = hour >= day_start or hour < night_start

    if is_day:
        period = "day"
        allowed = {s.strip() for s in settings.schedule_day_strategies.split(",")}
    else:
        period = "night"
        allowed = {s.strip() for s in settings.schedule_night_strategies.split(",")}

    if period != _last_schedule_period:
        _log.info(
            "strategy_schedule_switch",
            period=period,
            allowed_strategies=sorted(allowed),
            utc_hour=hour,
        )
        _last_schedule_period = period

    return [s for s in strategies if s.name in allowed]


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
    decision_logger: DecisionLogger | None = None,
    allocation_manager: AllocationManager | None = None,
    hedge_manager: object | None = None,
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
        cycle_id = decision_logger.next_cycle() if decision_logger else 0

        # Apply time-based strategy schedule filter
        active_strategies = _filter_strategies_by_schedule(
            strategies or [], settings,
        )
        scanner._strategies = active_strategies

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
                decision_logger=decision_logger,
                cycle_id=cycle_id,
                allocation_manager=allocation_manager,
                hedge_manager=hedge_manager,
            )
        else:
            # Normal mode: scan all, log, then pick best
            all_opps = await scanner.scan_all(markets)

            if decision_logger:
                if not all_opps:
                    decision_logger.log_no_opportunities(cycle_id)
                else:
                    for opp in all_opps:
                        decision_logger.log_opportunity(cycle_id, opp)

            # Try opportunities in ranked order until one passes risk checks
            for opp in all_opps:
                market = opp.market

                # Risk check
                approved, reason = risk_manager.check_opportunity(opp)
                if decision_logger:
                    decision_logger.log_risk_decision(
                        cycle_id, opp, approved, reason
                    )

                if not approved:
                    _log.debug(
                        "opportunity_rejected",
                        market=market.slug,
                        reason=reason,
                    )
                    continue

                # Adjust size (use strategy-specific size if available)
                base_size = opp.requested_size if opp.requested_size > 0 else settings.order_size
                if allocation_manager is not None:
                    base_size = allocation_manager.get_allocated_size(
                        opp.strategy, base_size, asset=opp.market.asset,
                    )
                adjusted_size = risk_manager.adjust_size(
                    opp, base_size
                )
                if adjusted_size <= 0:
                    _log.debug("size_adjusted_to_zero", market=market.slug)
                    continue

                try:
                    await _execute_opportunity(
                        opp=opp,
                        adjusted_size=adjusted_size,
                        executor=executor,
                        state_manager=state_manager,
                        risk_manager=risk_manager,
                        rate_limiter=rate_limiter,
                        settings=settings,
                        strategies=strategies or [],
                        hedge_manager=hedge_manager,
                    )
                    if decision_logger:
                        decision_logger.log_execution(
                            cycle_id, opp, success=True
                        )
                except Exception:
                    if decision_logger:
                        decision_logger.log_execution(
                            cycle_id, opp, success=False
                        )
                    raise
                break  # Only execute one opportunity per cycle

        # Wait before next evaluation cycle
        try:
            await asyncio.wait_for(
                _shutdown_event.wait(),
                timeout=2.0,
            )
        except asyncio.TimeoutError:
            pass


def _resolve_strategy_conflicts(
    best_per_strategy: dict[StrategyType, object],
) -> dict[StrategyType, object]:
    """Allow only one directional strategy per market (highest confidence wins).

    When multiple strategies target the same condition_id, keeps only the
    highest-confidence one — regardless of whether they agree or disagree on
    direction.  This prevents both same-direction pileups (compounding losses)
    and opposite-direction fee drains.

    Strategies without 'direction' metadata (arbitrage, asymmetric, maker_arb)
    are never suppressed.
    """
    from collections import defaultdict

    # Group directional opportunities by condition_id
    by_market: dict[str, list[tuple[StrategyType, object]]] = defaultdict(list)

    for strat_type, opp in best_per_strategy.items():
        direction = opp.metadata.get("direction")
        if direction is None:
            continue  # non-directional strategies are never in conflict
        by_market[opp.market.condition_id].append((strat_type, opp))

    suppressed: set[StrategyType] = set()

    for condition_id, entries in by_market.items():
        if len(entries) < 2:
            continue

        # Multiple strategies on same market — keep only highest confidence
        entries.sort(key=lambda x: x[1].confidence, reverse=True)

        winner_strat, winner_opp = entries[0]
        for strat_type, opp in entries[1:]:
            suppressed.add(strat_type)
            _log.warning(
                "strategy_conflict_suppressed",
                market=opp.market.slug,
                suppressed_strategy=strat_type.value,
                suppressed_direction=opp.metadata.get("direction"),
                suppressed_confidence=round(opp.confidence, 4),
                kept_strategy=winner_strat.value,
                kept_direction=winner_opp.metadata.get("direction"),
                kept_confidence=round(winner_opp.confidence, 4),
            )

    if not suppressed:
        return best_per_strategy

    return {k: v for k, v in best_per_strategy.items() if k not in suppressed}


async def _execute_parallel_strategies(
    scanner: MarketScanner,
    markets: list,
    risk_manager: RiskManager,
    executor: OrderExecutor,
    state_manager: StateManager,
    rate_limiter: RateLimiter,
    settings: Settings,
    strategies: list[BaseStrategy],
    decision_logger: DecisionLogger | None = None,
    cycle_id: int = 0,
    allocation_manager: AllocationManager | None = None,
    hedge_manager: object | None = None,
) -> None:
    """Execute the best opportunity from each strategy type (A/B test mode).

    This allows multiple strategies to trade in the same cycle, enabling
    fair comparison of strategy performance over time.
    """
    best_per_strategy = await scanner.scan_best_per_strategy(markets)

    if not best_per_strategy:
        if decision_logger:
            decision_logger.log_no_opportunities(cycle_id)
        return

    # Resolve conflicts where strategies bet opposite sides of the same market
    best_per_strategy = _resolve_strategy_conflicts(best_per_strategy)

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

        if decision_logger:
            decision_logger.log_opportunity(cycle_id, opp)

        # Risk check
        approved, reason = risk_manager.check_opportunity(opp)
        if decision_logger:
            decision_logger.log_risk_decision(cycle_id, opp, approved, reason)

        if not approved:
            _log.debug(
                "opportunity_rejected",
                strategy=strat_type.value,
                market=market.slug,
                reason=reason,
            )
            continue

        # Adjust size (use strategy-specific size if available)
        base_size = opp.requested_size if opp.requested_size > 0 else settings.order_size
        if allocation_manager is not None:
            base_size = allocation_manager.get_allocated_size(
                opp.strategy, base_size, asset=opp.market.asset,
            )
        adjusted_size = risk_manager.adjust_size(opp, base_size)
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

        try:
            await _execute_opportunity(
                opp=opp,
                adjusted_size=adjusted_size,
                executor=executor,
                state_manager=state_manager,
                risk_manager=risk_manager,
                rate_limiter=rate_limiter,
                settings=settings,
                strategies=strategies,
                hedge_manager=hedge_manager,
            )
            if decision_logger:
                decision_logger.log_execution(cycle_id, opp, success=True)
        except Exception:
            if decision_logger:
                decision_logger.log_execution(cycle_id, opp, success=False)
            raise


async def _execute_opportunity(
    opp,
    adjusted_size: float,
    executor: OrderExecutor,
    state_manager: StateManager,
    risk_manager: RiskManager,
    rate_limiter: RateLimiter,
    settings: Settings,
    strategies: list[BaseStrategy] | None = None,
    hedge_manager: object | None = None,
) -> None:
    """Build orders, acquire rate limit tokens, and execute.

    Handles arbitrage (two-leg YES+NO), asymmetric (GTC limit),
    maker arbitrage (paired GTC), and directional (single-leg FOK) strategies.
    """
    is_hmm = opp.metadata.get("hmm", False)
    if is_hmm:
        await _execute_hedged_mm_trade(
            opp, adjusted_size, executor, state_manager,
            risk_manager, rate_limiter, settings, strategies or [],
        )
        return

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
            hedge_manager=hedge_manager,
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

    # Track pending orders for crash recovery
    if state_manager.trade_db is not None:
        for _pend_order in [yes_order, no_order]:
            state_manager.trade_db.save_pending_order({
                "condition_id": market.condition_id,
                "token_id": _pend_order.token_id,
                "side": _pend_order.side.value,
                "price": _pend_order.price,
                "size": _pend_order.size,
                "order_type": _pend_order.order_type,
                "strategy": opp.strategy.value,
            })

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
            unwind = EmergencyUnwind(executor, state_manager, risk_manager=risk_manager)
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

        # Clear pending orders after successful recording
        if state_manager.trade_db is not None:
            for _done_order in [yes_result, no_result]:
                if _done_order.order_id:
                    state_manager.trade_db.clear_pending_order(_done_order.order_id)

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
            # Post-fill combined cost validation
            actual_combined = (yes_result.fill_price or 0) + (no_result.fill_price or 0)
            if actual_combined > settings.maker_max_combined_fill_cost:
                _log.warning(
                    "maker_arb_post_fill_rejected",
                    market=market.slug,
                    yes_fill=yes_result.fill_price,
                    no_fill=no_result.fill_price,
                    combined=round(actual_combined, 4),
                    limit=settings.maker_max_combined_fill_cost,
                )
                pair.status = "rejected"
                return

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
                combined_cost=round(actual_combined, 4),
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


async def _execute_hedged_mm_trade(
    opp,
    adjusted_size: float,
    executor: OrderExecutor,
    state_manager: StateManager,
    risk_manager: RiskManager,
    rate_limiter: RateLimiter,
    settings: Settings,
    strategies: list[BaseStrategy],
) -> None:
    """Execute a hedged market-maker trade (entry, scalp, or time_exit).

    Routes on metadata["action"]:
    - "entry": place GTC BUY for both YES and NO
    - "scalp": FOK SELL the appreciated side
    - "time_exit": FOK SELL the remaining side
    """
    action = opp.metadata.get("action", "")
    market = opp.market

    # Find the HedgedMMStrategy instance
    hmm_strat: HedgedMMStrategy | None = None
    for strat in strategies:
        if isinstance(strat, HedgedMMStrategy):
            hmm_strat = strat
            break

    if hmm_strat is None:
        _log.error("hmm_strategy_not_found", market=market.slug)
        return

    if action == "entry":
        await _execute_hmm_entry(
            opp, adjusted_size, executor, state_manager,
            risk_manager, rate_limiter, settings, hmm_strat,
        )
    elif action in ("scalp", "time_exit"):
        await _execute_hmm_scalp(
            opp, adjusted_size, executor, state_manager,
            risk_manager, rate_limiter, settings, hmm_strat,
            is_time_exit=(action == "time_exit"),
        )
    else:
        _log.error("hmm_unknown_action", action=action, market=market.slug)


async def _execute_hmm_entry(
    opp,
    adjusted_size: float,
    executor: OrderExecutor,
    state_manager: StateManager,
    risk_manager: RiskManager,
    rate_limiter: RateLimiter,
    settings: Settings,
    hmm_strat: HedgedMMStrategy,
) -> None:
    """Place GTC BUY orders for both YES and NO tokens."""
    market = opp.market
    yes_price = opp.metadata.get("yes_price", 0.0)
    no_price = opp.metadata.get("no_price", 0.0)

    if yes_price <= 0 or no_price <= 0:
        _log.error("hmm_invalid_prices", market=market.slug)
        return

    # Create GTC BUY orders
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
        "executing_hmm_entry",
        market=market.slug,
        yes_price=yes_price,
        no_price=no_price,
        size=adjusted_size,
        combined=round(yes_price + no_price, 4),
        dry_run=settings.dry_run,
    )

    try:
        await rate_limiter.acquire(4)

        # Sign both orders in parallel
        yes_order, no_order = await executor.sign_orders_parallel(
            [yes_order, no_order]
        )

        if (
            yes_order.status != OrderStatus.SIGNED
            or no_order.status != OrderStatus.SIGNED
        ):
            _log.warning(
                "hmm_sign_failed",
                market=market.slug,
                yes_status=yes_order.status.value,
                no_status=no_order.status.value,
            )
            risk_manager.record_execution_failure()
            return

        # Submit YES
        yes_result = await executor.submit_order(yes_order)
        if yes_result.status == OrderStatus.REJECTED:
            _log.warning("hmm_yes_rejected", market=market.slug)
            risk_manager.record_execution_failure()
            return

        # Submit NO
        no_result = await executor.submit_order(no_order)
        if no_result.status == OrderStatus.REJECTED:
            _log.warning("hmm_no_rejected_cancelling_yes", market=market.slug)
            if yes_result.order_id:
                await executor.cancel_order(yes_result.order_id)
            risk_manager.record_execution_failure()
            return

        # Create HMM pair in strategy
        pair = hmm_strat.create_pair(
            condition_id=market.condition_id,
            market_slug=market.slug,
            yes_price=yes_price,
            no_price=no_price,
            size=adjusted_size,
        )
        pair.yes_order_id = yes_result.order_id
        pair.no_order_id = no_result.order_id

        # Dry-run: GTC fills immediately
        if settings.dry_run:
            pair.yes_filled = True
            pair.no_filled = True
            pair.yes_fill_size = adjusted_size
            pair.no_fill_size = adjusted_size
            hmm_strat.mark_entry_complete(pair.pair_id)
            await state_manager.record_trade(opp, [yes_result, no_result])
            risk_manager.record_execution_success(market.condition_id)
            _log.info(
                "hmm_entry_complete_dry",
                market=market.slug,
                pair_id=pair.pair_id,
                combined=round(yes_price + no_price, 4),
            )
            if _alerts and settings.alert_on_trade:
                await _alerts.send_trade(
                    f"HMM entry: {market.slug} YES@{yes_price:.2f}+NO@{no_price:.2f} "
                    f"x{adjusted_size:.0f}"
                )
            return

        # Live mode: track for monitoring
        _pending_hmm_pairs.append({
            "pair": pair,
            "yes_order": yes_result,
            "no_order": no_result,
            "opportunity": opp,
            "market": market,
        })
        _log.info(
            "hmm_entry_submitted",
            market=market.slug,
            pair_id=pair.pair_id,
            yes_order_id=yes_result.order_id,
            no_order_id=no_result.order_id,
        )

    except asyncio.TimeoutError:
        _log.warning("rate_limit_timeout", market=market.slug)
    except Exception as exc:
        risk_manager.record_execution_failure()
        _log.error("hmm_entry_error", market=market.slug, error=str(exc))
        if _alerts and settings.alert_on_error:
            await _alerts.send_error(f"HMM entry error {market.slug}: {exc}")


async def _execute_hmm_scalp(
    opp,
    adjusted_size: float,
    executor: OrderExecutor,
    state_manager: StateManager,
    risk_manager: RiskManager,
    rate_limiter: RateLimiter,
    settings: Settings,
    hmm_strat: HedgedMMStrategy,
    is_time_exit: bool = False,
) -> None:
    """SELL the appreciated (or remaining) side via FOK order."""
    market = opp.market
    scalp_side = opp.metadata.get("scalp_side", "")
    scalp_token_id = opp.metadata.get("scalp_token_id", "")
    scalp_price = opp.metadata.get("scalp_price", 0.0)
    pair_id = opp.metadata.get("pair_id", "")
    entry_price = opp.metadata.get("entry_price", 0.0)

    if not scalp_token_id or not pair_id:
        _log.error("hmm_scalp_missing_metadata", market=market.slug)
        return

    action_name = "time_exit" if is_time_exit else "scalp"

    # Create FOK SELL order (sell at market — price 0.01 for FOK means sell at best bid)
    sell_order = TradeOrder(
        token_id=scalp_token_id,
        side=Side.SELL,
        price=0.01,  # FOK SELL: floor price, fills at best bid
        size=adjusted_size,
        order_type="FOK",
    )

    _log.info(
        f"executing_hmm_{action_name}",
        market=market.slug,
        pair_id=pair_id,
        side=scalp_side,
        entry_price=round(entry_price, 4),
        target_price=round(scalp_price, 4),
        size=adjusted_size,
        dry_run=settings.dry_run,
    )

    try:
        await rate_limiter.acquire(2)

        await executor.sign_order(sell_order)
        sell_result = await executor.submit_order(sell_order)
        sell_result = await executor.verify_fill(sell_result)

        if sell_result.status not in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
            _log.warning(
                f"hmm_{action_name}_not_filled",
                market=market.slug,
                pair_id=pair_id,
                status=sell_result.status.value,
            )
            return

        # Record the sell in state (decrements shares/cost for that side)
        await state_manager.record_trade(opp, [sell_result])

        actual_price = sell_result.fill_price or scalp_price
        profit = (actual_price - entry_price) * (sell_result.fill_size or adjusted_size)

        if is_time_exit:
            hmm_strat.mark_closed(pair_id)
        else:
            hmm_strat.mark_scalped(pair_id, scalp_side, actual_price)

        risk_manager.record_execution_success(market.condition_id)

        _log.info(
            f"hmm_{action_name}_complete",
            market=market.slug,
            pair_id=pair_id,
            side=scalp_side,
            entry_price=round(entry_price, 4),
            sell_price=round(actual_price, 4),
            profit=round(profit, 4),
            daily_pnl=round(state_manager.daily_pnl().net_profit, 4),
        )

        if _alerts and settings.alert_on_trade:
            await _alerts.send_trade(
                f"HMM {action_name}: {market.slug} SELL {scalp_side}@{actual_price:.2f} "
                f"(entry@{entry_price:.2f}) profit={profit:.4f}"
            )

    except asyncio.TimeoutError:
        _log.warning("rate_limit_timeout", market=market.slug)
    except Exception as exc:
        risk_manager.record_execution_failure()
        _log.error(f"hmm_{action_name}_error", market=market.slug, error=str(exc))
        if _alerts and settings.alert_on_error:
            await _alerts.send_error(f"HMM {action_name} error {market.slug}: {exc}")


async def _execute_directional_trade(
    opp,
    adjusted_size: float,
    executor: OrderExecutor,
    state_manager: StateManager,
    risk_manager: RiskManager,
    rate_limiter: RateLimiter,
    settings: Settings,
    hedge_manager: object | None = None,
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
            if not order.market_condition_rejection:
                risk_manager.record_execution_failure()
            _log.warning("directional_sign_rejected", market=market.slug)
            return
        result = await executor.submit_order(order)
        result = await executor.verify_fill(result)
        await state_manager.record_trade(opp, [result])
        if result.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
            risk_manager.record_execution_success(market.condition_id)
            # Open CEX perp hedge if configured
            if hedge_manager is not None and result.status == OrderStatus.FILLED:
                try:
                    hedge_result = await hedge_manager.hedge_trade(opp, result)
                    if hedge_result:
                        _log.info(
                            "cex_hedge_opened",
                            symbol=hedge_result.symbol,
                            side=hedge_result.side,
                            qty=hedge_result.quantity,
                            avg_price=hedge_result.avg_price,
                        )
                except Exception as hedge_exc:
                    _log.error("cex_hedge_failed", error=str(hedge_exc))
                    # Hedge failure is non-fatal — Polymarket trade already executed
        elif not result.market_condition_rejection:
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

                # If both filled: validate combined cost then complete
                if pair.is_complete:
                    to_remove.append(entry)
                    actual_combined = (
                        (yes_order.fill_price or pair.yes_price)
                        + (no_order.fill_price or pair.no_price)
                    )
                    if actual_combined > settings.maker_max_combined_fill_cost:
                        _log.warning(
                            "maker_arb_post_fill_rejected",
                            market=market.slug,
                            pair_id=pair.pair_id,
                            yes_fill=yes_order.fill_price,
                            no_fill=no_order.fill_price,
                            combined=round(actual_combined, 4),
                            limit=settings.maker_max_combined_fill_cost,
                        )
                        pair.status = "rejected"
                        continue
                    await state_manager.record_trade(opp, [yes_order, no_order])
                    risk_manager.record_execution_success(market.condition_id)
                    _log.info(
                        "maker_arb_complete",
                        market=market.slug,
                        pair_id=pair.pair_id,
                        combined_cost=round(actual_combined, 4),
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
# HMM monitor loop (hedged market-maker GTC fill tracking)
# ---------------------------------------------------------------------------


async def _hmm_monitor_loop(
    executor: OrderExecutor,
    state_manager: StateManager,
    risk_manager: RiskManager,
    strategies: list[BaseStrategy],
    settings: Settings,
    interval: float = 5.0,
) -> None:
    """Monitor pending HMM pairs for GTC fills and handle timeouts.

    In DRY_RUN mode this loop is mostly idle (GTC fills immediately).
    In live mode, it checks each pending pair every interval seconds:
    - If both legs filled: transition to hedged, record trade
    - If timeout with partial fill: cancel unfilled, unwind filled
    - If timeout with no fills: cancel both orders
    """
    hmm_strat: HedgedMMStrategy | None = None
    for strat in strategies:
        if isinstance(strat, HedgedMMStrategy):
            hmm_strat = strat
            break

    if hmm_strat is None:
        _log.debug("hmm_monitor_no_strategy")
        return

    while _shutdown_event is not None and not _shutdown_event.is_set():
        try:
            to_remove: list[dict] = []

            for entry in _pending_hmm_pairs:
                pair: HMMPair = entry["pair"]
                yes_order: TradeOrder = entry["yes_order"]
                no_order: TradeOrder = entry["no_order"]
                opp = entry["opportunity"]
                market = entry["market"]

                # Check YES fill
                if not pair.yes_filled and yes_order.order_id:
                    checked = await executor.verify_fill(
                        yes_order, timeout=1.0, poll_interval=0.5
                    )
                    if checked.status == OrderStatus.FILLED:
                        pair.yes_filled = True
                        pair.yes_fill_size = checked.fill_size or pair.size
                        yes_order.status = OrderStatus.FILLED
                        yes_order.fill_size = checked.fill_size or pair.size
                        yes_order.fill_price = checked.fill_price or pair.yes_entry_price

                # Check NO fill
                if not pair.no_filled and no_order.order_id:
                    checked = await executor.verify_fill(
                        no_order, timeout=1.0, poll_interval=0.5
                    )
                    if checked.status == OrderStatus.FILLED:
                        pair.no_filled = True
                        pair.no_fill_size = checked.fill_size or pair.size
                        no_order.status = OrderStatus.FILLED
                        no_order.fill_size = checked.fill_size or pair.size
                        no_order.fill_price = checked.fill_price or pair.no_entry_price

                # Both filled: transition to hedged
                if pair.is_entry_complete and pair.status == "pending_entry":
                    to_remove.append(entry)
                    hmm_strat.mark_entry_complete(pair.pair_id)
                    await state_manager.record_trade(opp, [yes_order, no_order])
                    risk_manager.record_execution_success(market.condition_id)
                    _log.info(
                        "hmm_entry_complete_live",
                        market=market.slug,
                        pair_id=pair.pair_id,
                    )
                    if _alerts and settings.alert_on_trade:
                        await _alerts.send_trade(
                            f"HMM entry filled: {market.slug} "
                            f"YES@{pair.yes_entry_price:.2f}+NO@{pair.no_entry_price:.2f} "
                            f"x{pair.size:.0f}"
                        )
                    continue

                # Check for timeout
                if pair.age_seconds > settings.hmm_pair_fill_timeout:
                    to_remove.append(entry)
                    await _handle_hmm_entry_timeout(
                        pair, yes_order, no_order, executor,
                        hmm_strat, state_manager, risk_manager, market,
                    )

            # Clean up processed entries
            for entry in to_remove:
                if entry in _pending_hmm_pairs:
                    _pending_hmm_pairs.remove(entry)

        except Exception as exc:
            _log.error("hmm_monitor_error", error=str(exc))

        try:
            await asyncio.wait_for(_shutdown_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def _handle_hmm_entry_timeout(
    pair: HMMPair,
    yes_order: TradeOrder,
    no_order: TradeOrder,
    executor: OrderExecutor,
    hmm_strat: HedgedMMStrategy,
    state_manager: StateManager,
    risk_manager: RiskManager,
    market,
) -> None:
    """Handle a timed-out HMM entry pair.

    - Neither filled: cancel both
    - One filled: cancel unfilled, sell filled to unwind
    """
    _log.warning(
        "hmm_entry_timeout",
        pair_id=pair.pair_id,
        market=market.slug,
        yes_filled=pair.yes_filled,
        no_filled=pair.no_filled,
        age=round(pair.age_seconds, 1),
    )

    if not pair.yes_filled and not pair.no_filled:
        if yes_order.order_id:
            await executor.cancel_order(yes_order.order_id)
        if no_order.order_id:
            await executor.cancel_order(no_order.order_id)
        hmm_strat.cancel_pair(pair.pair_id)
        _log.info("hmm_cancelled_no_fills", pair_id=pair.pair_id)
        return

    # Partial fill — unwind the filled side
    if pair.yes_filled and not pair.no_filled:
        if no_order.order_id:
            await executor.cancel_order(no_order.order_id)
        filled_token_id = market.yes_token_id
        filled_size = pair.yes_fill_size or pair.size
        filled_side = "YES"
    else:
        if yes_order.order_id:
            await executor.cancel_order(yes_order.order_id)
        filled_token_id = market.no_token_id
        filled_size = pair.no_fill_size or pair.size
        filled_side = "NO"

    _log.warning(
        "hmm_partial_unwind",
        pair_id=pair.pair_id,
        market=market.slug,
        filled_side=filled_side,
    )

    sell_order = TradeOrder(
        token_id=filled_token_id,
        side=Side.SELL,
        price=0.01,
        size=filled_size,
        order_type="FOK",
    )

    try:
        await executor.sign_order(sell_order)
        if sell_order.status == OrderStatus.SIGNED:
            result = await executor.submit_order(sell_order)
            result = await executor.verify_fill(result)
            if result.status == OrderStatus.FILLED:
                _log.info("hmm_unwind_complete", pair_id=pair.pair_id, side=filled_side)
            else:
                _log.error("hmm_unwind_failed", pair_id=pair.pair_id, status=result.status.value)
                if _alerts:
                    await _alerts.send_error(f"HMM unwind failed: {market.slug} {filled_side}")
    except Exception as exc:
        _log.error("hmm_unwind_error", pair_id=pair.pair_id, error=str(exc))

    hmm_strat.cancel_pair(pair.pair_id)
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
    hedge_manager: object | None = None,
    risk_manager: RiskManager | None = None,
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

                # Backoff: skip exit retry if timer hasn't elapsed
                if risk_manager is not None and not risk_manager.should_retry_exit(market.condition_id):
                    continue

                if strategy.should_exit(position, market):
                    await _execute_exit(
                        position, market, executor, state_manager,
                        rate_limiter, settings, book_manager, trade_db,
                        hedge_manager=hedge_manager,
                        risk_manager=risk_manager,
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
    hedge_manager: object | None = None,
    risk_manager: RiskManager | None = None,
) -> None:
    """Sell all shares in a directional position."""
    # Check orderbook bid before attempting exit — don't sell for dust
    min_exit_bid = settings.min_exit_bid
    if position.yes_shares > 0:
        yes_book = book_manager.get_book(market.yes_token_id)
        if yes_book is None or yes_book.best_bid is None or yes_book.best_bid < min_exit_bid:
            _log.info(
                "exit_skipped_dust_bid",
                market=market.slug,
                side="YES",
                best_bid=yes_book.best_bid if yes_book else None,
                threshold=min_exit_bid,
            )
            return
    if position.no_shares > 0:
        no_book = book_manager.get_book(market.no_token_id)
        if no_book is None or no_book.best_bid is None or no_book.best_bid < min_exit_bid:
            _log.info(
                "exit_skipped_dust_bid",
                market=market.slug,
                side="NO",
                best_bid=no_book.best_bid if no_book else None,
                threshold=min_exit_bid,
            )
            return

    orders = []
    if position.yes_shares > 0:
        yes_sell_size = position.yes_shares
        # Query actual on-chain balance to prevent "not enough balance" errors
        # (CLOB matching can round down, leaving fewer shares than recorded)
        actual_bal = await executor.get_token_balance(market.yes_token_id)
        if actual_bal is not None and actual_bal < yes_sell_size:
            _log.warning(
                "sell_size_capped_to_balance",
                side="YES",
                recorded=yes_sell_size,
                on_chain=actual_bal,
                market=market.slug,
            )
            yes_sell_size = actual_bal
        if yes_sell_size > 0:
            orders.append(TradeOrder(
                token_id=market.yes_token_id,
                side=Side.SELL,
                price=0.01,  # market sell (lowest acceptable price)
                size=yes_sell_size,
                order_type="FOK",
            ))

    if position.no_shares > 0:
        no_sell_size = position.no_shares
        actual_bal = await executor.get_token_balance(market.no_token_id)
        if actual_bal is not None and actual_bal < no_sell_size:
            _log.warning(
                "sell_size_capped_to_balance",
                side="NO",
                recorded=no_sell_size,
                on_chain=actual_bal,
                market=market.slug,
            )
            no_sell_size = actual_bal
        if no_sell_size > 0:
            orders.append(TradeOrder(
                token_id=market.no_token_id,
                side=Side.SELL,
                price=0.01,  # market sell
                size=no_sell_size,
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

        # Check if any orders actually filled — rejected orders must not
        # trigger state close or revenue recording (phantom P&L bug).
        filled_results = [r for r in results if r.status == OrderStatus.FILLED]
        if not filled_results:
            _log.warning(
                "exit_orders_rejected",
                market=market.slug,
                results=[r.status.value for r in results],
            )
            if risk_manager is not None:
                risk_manager.mark_exit_blocked(market.condition_id)
            return  # Leave position open for retry on next exit-check cycle

        # Compute sell proceeds from actual fill data (not orderbook bids).
        sell_proceeds = sum(r.fill_price * r.fill_size for r in filled_results)

        total_shares = position.yes_shares + position.no_shares
        payout_per_share = sell_proceeds / total_shares if total_shares > 0 else 0.0

        # Close the position in state to prevent repeated exit signals
        # and double-counting at market resolution.
        net_profit = sell_proceeds - position.total_investment
        from src.utils.fees import WINNER_FEE_RATE
        actual_winner_fee = WINNER_FEE_RATE * max(0.0, net_profit)
        net_profit -= actual_winner_fee

        try:
            state_manager.close_position(market.condition_id, payout_per_share, position.strategy)
        except KeyError:
            pass  # Already closed by resolution loop

        # Successful exit — clear any exit-blocked flag for this market
        if risk_manager is not None:
            risk_manager.clear_exit_blocked(market.condition_id)

        # Close CEX hedge if one exists for this position
        if hedge_manager is not None:
            try:
                close_result = await hedge_manager.close_hedge_for_position(
                    market.condition_id,
                )
                if close_result:
                    _log.info(
                        "cex_hedge_closed_on_exit",
                        condition_id=market.condition_id,
                        symbol=close_result.symbol,
                        status=close_result.status,
                    )
            except Exception as hedge_exc:
                _log.error(
                    "cex_hedge_close_failed",
                    condition_id=market.condition_id,
                    error=str(hedge_exc),
                )

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

        # Record outcome for per-strategy cooldown tracking
        if risk_manager is not None:
            risk_manager.record_trade_outcome(position.strategy, net_profit >= 0)

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
        if risk_manager is not None:
            risk_manager.mark_exit_blocked(market.condition_id)


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
        dashboard = metrics.compute_dashboard(
            pnl,
            state_manager.sim_balance,
            lifetime_net_profit=state_manager.lifetime_net_profit,
        )
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
                dashboard = metrics.compute_dashboard(
                    pnl,
                    state_manager.sim_balance,
                    lifetime_net_profit=state_manager.lifetime_net_profit,
                )
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


def resolve_outcome(
    pos: Position,
    trade_db: TradeDatabase | None,
    spot_buffer: SpotBuffer | None,
) -> float | None:
    """Determine if YES or NO won based on spot price movement.

    Returns 1.0 if YES won (price went up), 0.0 if NO won (price went down).
    Returns None if unable to determine.

    Uses DB-persisted spot snapshots (5s intervals) for the start price
    to avoid SpotBuffer overflow issues (raw Binance ticks overflow
    the deque in ~20-100s for liquid pairs).
    """
    # Derive actual 15-min window start from end_time (not market.start_time
    # which is Gamma API's startDate, ~24h before the actual window)
    end_ts = pos.market.end_time.timestamp()
    start_ts = end_ts - pos.market.window_seconds  # actual window start

    asset = pos.market.asset
    symbol = f"{asset}USDT"

    # Use persisted spot snapshots for start price (immune to buffer overflow)
    start_price = None
    if trade_db is not None:
        start_price = trade_db.get_spot_at_time(
            symbol, start_ts, tolerance_s=30.0
        )

    # End price: current buffer price is fine (just the latest tick)
    end_price = spot_buffer.get_price(symbol) if spot_buffer else None
    # Fallback to DB if buffer unavailable
    if end_price is None and trade_db is not None:
        end_price = trade_db.get_spot_at_time(
            symbol, end_ts, tolerance_s=30.0
        )

    if start_price is None or end_price is None:
        _log.warning(
            "incomplete_spot_data_for_resolution",
            symbol=symbol,
            condition_id=pos.market.condition_id,
            has_start=start_price is not None,
            has_end=end_price is not None,
        )
        return None

    price_went_up = end_price > start_price

    _log.info(
        "outcome_resolved_from_spot",
        symbol=symbol,
        start_price=start_price,
        end_price=end_price,
        start_ts=start_ts,
        end_ts=end_ts,
        outcome="YES" if price_went_up else "NO",
    )

    return 1.0 if price_went_up else 0.0


def _update_kelly_state(
    trade_db: TradeDatabase | None,
    state_manager: StateManager,
) -> None:
    """Compute and persist Kelly inputs (win_rate, avg_win, avg_loss) from daily P&L."""
    if trade_db is None:
        return
    try:
        pnl = state_manager.daily_pnl()
        total = pnl.win_count + pnl.loss_count
        if total < 5:
            return  # not enough data yet today
        win_rate = pnl.win_count / total
        avg_win = pnl.total_win_amount / pnl.win_count if pnl.win_count > 0 else 0.0
        avg_loss = pnl.total_loss_amount / pnl.loss_count if pnl.loss_count > 0 else 0.0
        trade_db.save_kelly_state(win_rate, avg_win, avg_loss, total)
    except Exception as exc:
        _log.warning("kelly_persist_failed", error=str(exc))


def _save_attributed_results(
    trade_db: TradeDatabase,
    report: dict[str, object],
    risk_manager: RiskManager | None = None,
) -> None:
    """Save trade results with correct per-strategy attribution.

    When multiple strategies traded the same condition_id, each strategy
    gets its own trade_result record with correctly attributed shares,
    investment, payout, and P&L — instead of lumping everything under
    whichever strategy created the position first.
    """
    from src.utils.fees import WINNER_FEE_RATE

    condition_id = str(report["condition_id"])
    breakdown = trade_db.get_position_strategy_breakdown(condition_id)

    # Fall back to single record if no trade data or only one strategy
    if len(breakdown) <= 1:
        strategy = str(report.get("strategy", ""))
        # If breakdown has one entry, use its strategy (more accurate than
        # the Position's strategy which may have been overwritten)
        if len(breakdown) == 1:
            strategy = next(iter(breakdown))
        net_profit = float(report.get("net_profit", 0.0))
        trade_db.save_trade_result(TradeResult(
            timestamp=time.time(),
            condition_id=condition_id,
            market_slug=str(report.get("slug", "")),
            asset=str(report.get("asset", "")),
            strategy=strategy,
            was_hedged=bool(report.get("was_hedged", False)),
            yes_shares=float(report.get("yes_shares", 0.0)),
            no_shares=float(report.get("no_shares", 0.0)),
            investment=float(report.get("investment", 0.0)),
            gross_payout=float(report.get("gross_payout", 0.0)),
            net_profit=net_profit,
            outcome=str(report.get("outcome", "")),
        ))
        if risk_manager is not None and strategy:
            try:
                from src.core.models import StrategyType
                risk_manager.record_trade_outcome(
                    StrategyType(strategy), net_profit >= 0,
                )
            except ValueError:
                pass  # Unknown strategy name — skip cooldown tracking
        return

    # Multiple strategies contributed — split into per-strategy results
    outcome = str(report.get("outcome", ""))
    now = time.time()
    slug = str(report.get("slug", ""))
    asset = str(report.get("asset", ""))

    for strat_name, shares in breakdown.items():
        s_yes = max(shares["yes_shares"], 0.0)
        s_no = max(shares["no_shares"], 0.0)
        s_investment = max(shares["yes_cost"] + shares["no_cost"], 0.0)
        s_hedged = s_yes > 0 and s_no > 0

        # Compute per-strategy payout based on outcome
        if outcome == "YES":
            s_payout = s_yes * 1.0
        elif outcome == "NO":
            s_payout = s_no * 1.0
        elif s_hedged:
            # Hedged: guaranteed payout = min(yes, no) pairs
            s_payout = min(s_yes, s_no) * 1.0
        else:
            # Unknown outcome for unhedged strategy: assume total loss
            # (better to show real losses than mask them with fake breakeven)
            s_payout = 0.0
            _log.warning(
                "attribution_unknown_outcome",
                strategy=strat_name,
                condition_id=condition_id,
                yes_shares=s_yes,
                no_shares=s_no,
                investment=s_investment,
            )

        raw_profit = s_payout - s_investment
        winner_fee = WINNER_FEE_RATE * max(0.0, raw_profit)
        s_net_profit = raw_profit - winner_fee

        trade_db.save_trade_result(TradeResult(
            timestamp=now,
            condition_id=condition_id,
            market_slug=slug,
            asset=asset,
            strategy=strat_name,
            was_hedged=s_hedged,
            yes_shares=s_yes,
            no_shares=s_no,
            investment=s_investment,
            gross_payout=s_payout,
            net_profit=s_net_profit,
            outcome=outcome,
        ))

        if risk_manager is not None:
            try:
                from src.core.models import StrategyType
                risk_manager.record_trade_outcome(
                    StrategyType(strat_name), s_net_profit >= 0,
                )
            except ValueError:
                pass  # Unknown strategy name — skip cooldown tracking

    _log.info(
        "multi_strategy_attribution",
        condition_id=condition_id,
        strategies=list(breakdown.keys()),
        slug=slug,
    )


async def _resolution_loop(
    state_manager: StateManager,
    spot_buffer: SpotBuffer | None = None,
    trade_db: TradeDatabase | None = None,
    interval: float = 15.0,
    hedge_manager: object | None = None,
    risk_manager: RiskManager | None = None,
) -> None:
    """Periodically check for and resolve expired positions.

    Runs every *interval* seconds (default 15s) to detect positions
    whose markets have expired and resolve them appropriately.

    For hedged positions: guaranteed $1 per share pair.
    For unhedged positions: uses spot price data to infer outcome.
    """

    def _outcome_resolver(pos: Position) -> float | None:
        return resolve_outcome(pos, trade_db, spot_buffer)

    while _shutdown_event is not None and not _shutdown_event.is_set():
        try:
            resolved = await state_manager.resolve_expired_positions(
                outcome_resolver=_outcome_resolver,
            )
            if resolved:
                if trade_db is not None:
                    for report in resolved:
                        _save_attributed_results(trade_db, report, risk_manager)
                # Clear exit-blocked flags for resolved markets
                if risk_manager is not None:
                    for report in resolved:
                        risk_manager.clear_exit_blocked(str(report["condition_id"]))
                # Close CEX hedges for resolved positions
                if hedge_manager is not None:
                    for report in resolved:
                        cid = str(report["condition_id"])
                        try:
                            close_result = await hedge_manager.close_hedge_for_position(cid)
                            if close_result:
                                _log.info(
                                    "cex_hedge_closed_on_resolution",
                                    condition_id=cid,
                                    symbol=close_result.symbol,
                                    status=close_result.status,
                                )
                        except Exception as hedge_exc:
                            _log.error(
                                "cex_hedge_close_failed",
                                condition_id=cid,
                                error=str(hedge_exc),
                            )
                # Persist Kelly inputs after each resolution batch
                _update_kelly_state(trade_db, state_manager)

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
# Spot snapshot loop (periodic spot price persistence)
# ---------------------------------------------------------------------------


async def _spot_snapshot_loop(
    spot_buffer: SpotBuffer,
    trade_db: TradeDatabase,
    interval: float = 5.0,
) -> None:
    """Periodically save spot prices to SQLite for post-hoc analysis."""
    while _shutdown_event is not None and not _shutdown_event.is_set():
        try:
            for symbol in spot_buffer.symbols:
                price = spot_buffer.get_price(symbol)
                if price is not None:
                    trade_db.save_spot_snapshot(
                        SpotSnapshot(
                            timestamp=time.time(),
                            symbol=symbol,
                            price=price,
                        )
                    )
        except Exception as exc:
            _log.error("spot_snapshot_error", error=str(exc))

        try:
            await asyncio.wait_for(_shutdown_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


# ---------------------------------------------------------------------------
# Market outcome loop (track all 15-min window results)
# ---------------------------------------------------------------------------

_market_start_prices: dict[str, float] = {}  # condition_id -> spot at start
_recorded_outcomes: set[str] = set()  # condition_ids already recorded


async def _market_outcome_loop(
    market_manager: MarketManager,
    spot_buffer: SpotBuffer,
    state_manager: StateManager,
    trade_db: TradeDatabase,
    interval: float = 10.0,
) -> None:
    """Track market open prices and record outcomes on expiry."""
    known_markets: dict[str, Market] = {}  # condition_id -> Market

    while _shutdown_event is not None and not _shutdown_event.is_set():
        try:
            # 1. Capture open prices for newly discovered markets
            for market in market_manager.active_markets:
                if market.condition_id not in known_markets:
                    symbol = f"{market.asset}USDT"
                    price = spot_buffer.get_price(symbol)
                    if price is not None:
                        _market_start_prices[market.condition_id] = price
                    known_markets[market.condition_id] = market

            # 2. Detect expired markets
            active_ids = {m.condition_id for m in market_manager.active_markets}
            expired_ids = (
                set(known_markets.keys()) - active_ids - _recorded_outcomes
            )

            for cid in list(expired_ids):
                market = known_markets.get(cid)
                if market is None:
                    continue

                symbol = f"{market.asset}USDT"

                # Correct window start: end_time - window_seconds (not
                # market.start_time which is Gamma API's startDate)
                window_start = market.end_time.timestamp() - market.window_seconds

                # Use DB for open price (reliable 5s snapshots)
                open_price = (
                    trade_db.get_spot_at_time(
                        symbol, window_start, tolerance_s=30.0
                    )
                    or 0.0
                )

                # Try to get close price from spot buffer or db
                close_price = spot_buffer.get_price(symbol)
                if close_price is None:
                    db_price = trade_db.get_spot_at_time(
                        symbol, market.end_time.timestamp(), tolerance_s=30.0
                    )
                    close_price = db_price if db_price is not None else 0.0

                # Determine outcome
                if open_price > 0 and close_price > 0:
                    change_pct = (close_price - open_price) / open_price * 100
                    if close_price > open_price:
                        outcome = "YES"
                    elif close_price < open_price:
                        outcome = "NO"
                    else:
                        outcome = "FLAT"
                else:
                    change_pct = 0.0
                    outcome = "FLAT"

                # Check if bot had a position
                was_traded = any(
                    p.market.condition_id == cid
                    for p in state_manager.get_all_positions()
                )

                trade_db.save_market_outcome(
                    MarketOutcome(
                        timestamp=time.time(),
                        condition_id=cid,
                        asset=market.asset,
                        market_slug=market.slug,
                        window_start=window_start,
                        window_end=market.end_time.timestamp(),
                        outcome=outcome,
                        spot_open=open_price,
                        spot_close=close_price,
                        price_change_pct=change_pct,
                        was_traded=was_traded,
                    )
                )

                _recorded_outcomes.add(cid)
                _log.info(
                    "market_outcome_recorded",
                    condition_id=cid[:12],
                    asset=market.asset,
                    outcome=outcome,
                    change_pct=round(change_pct, 4),
                    was_traded=was_traded,
                )

                # Cleanup start price
                _market_start_prices.pop(cid, None)

        except Exception as exc:
            _log.error("market_outcome_error", error=str(exc))

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
    trade_db: object | None = None,
    alpha_signals: object | None = None,
    state_manager: StateManager | None = None,
) -> list[BaseStrategy]:
    """Build the list of enabled strategies based on settings."""
    alpha: AlphaSignalProvider | None = (
        alpha_signals if isinstance(alpha_signals, AlphaSignalProvider) else None
    )
    strategies: list[BaseStrategy] = []

    if settings.enable_arbitrage:
        strategies.append(ArbitrageStrategy(settings=settings, book_manager=book_manager))

    if settings.enable_price_lag and spot_buffer is not None:
        strategies.append(PriceLagStrategy(
            settings=settings, book_manager=book_manager, spot_buffer=spot_buffer,
            alpha_signals=alpha,
        ))

    if settings.enable_asymmetric:
        strategies.append(AsymmetricStrategy(settings=settings, book_manager=book_manager))

    if settings.enable_maker_arbitrage:
        strategies.append(MakerArbitrageStrategy(settings=settings, book_manager=book_manager))

    if settings.enable_dip_buyer and spot_buffer is not None:
        strategies.append(DipBuyerStrategy(
            settings=settings, book_manager=book_manager, spot_buffer=spot_buffer,
            alpha_signals=alpha,
        ))

    if settings.enable_fade_panic and spot_buffer is not None:
        strategies.append(FadePanicStrategy(
            settings=settings, book_manager=book_manager, spot_buffer=spot_buffer,
            alpha_signals=alpha,
        ))

    if settings.enable_resolution_sniper and spot_buffer is not None:
        strategies.append(ResolutionSniperStrategy(
            settings=settings, book_manager=book_manager, spot_buffer=spot_buffer,
            alpha_signals=alpha,
        ))

    if settings.enable_cross_asset_strategy and spot_buffer is not None:
        strategies.append(CrossAssetCorrelationStrategy(
            settings=settings, book_manager=book_manager,
            spot_buffer=spot_buffer, trade_db=trade_db,
        ))

    if settings.enable_hedged_mm:
        strategies.append(HedgedMMStrategy(
            settings=settings, book_manager=book_manager,
            state_provider=state_manager,
        ))

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


async def _shutdown_sequence(
    clob_ws: ClobWebSocket,
    binance_ws: BinanceWebSocket,
    executor: OrderExecutor,
    state_manager: StateManager,
    risk_manager: RiskManager,
    settings: Settings,
    dashboard_server,
    trade_db: TradeDatabase | None,
    discovery: MarketDiscovery,
    alpha_signals: AlphaSignalProvider | None,
    hedge_manager,
    ws_task: asyncio.Task,
    binance_task: asyncio.Task,
    monitor_task: asyncio.Task,
    strategy_task: asyncio.Task,
    rollover_task: asyncio.Task,
    exit_task: asyncio.Task,
    gtc_task: asyncio.Task,
    maker_arb_task: asyncio.Task,
    summary_task: asyncio.Task,
    dashboard_task: asyncio.Task | None,
    snapshot_task: asyncio.Task | None,
    resolution_task: asyncio.Task,
    spot_snapshot_task: asyncio.Task | None,
    market_outcome_task: asyncio.Task | None,
    alpha_signals_task: asyncio.Task | None,
) -> None:
    """Execute full graceful shutdown sequence.

    Extracted so it can be wrapped in ``asyncio.wait_for`` with a timeout.
    """
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

    # Cancel all pending HMM pairs
    for entry in _pending_hmm_pairs:
        try:
            hmm_pair: HMMPair = entry["pair"]
            yes_order = entry["yes_order"]
            no_order = entry["no_order"]
            if yes_order.order_id:
                await executor.cancel_order(yes_order.order_id)
            if no_order.order_id:
                await executor.cancel_order(no_order.order_id)
            _log.info("hmm_shutdown_cancel", pair_id=hmm_pair.pair_id)
        except Exception as exc:
            _log.warning("hmm_cancel_error", error=str(exc))
    _pending_hmm_pairs.clear()

    # Flatten unhedged directional positions on shutdown
    unhedged = [p for p in state_manager.get_all_positions() if not p.is_hedged]
    if unhedged:
        _log.warning("shutdown_unwind_unhedged", count=len(unhedged))
        unwinder = EmergencyUnwind(
            executor, state_manager, risk_manager=risk_manager,
            trade_db=trade_db,
        )
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
        spot_snapshot_task, market_outcome_task,
        alpha_signals_task,
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

    # Close HTTP clients, alpha signals, Binance Futures, and trade database
    await discovery.close()
    if alpha_signals is not None:
        await alpha_signals.close()
    if hedge_manager is not None and hasattr(hedge_manager, '_client'):
        await hedge_manager._client.close()
    if trade_db is not None:
        trade_db.close()


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
        enable_resolution_sniper=settings.enable_resolution_sniper,
        enable_dip_buyer=settings.enable_dip_buyer,
        enable_fade_panic=settings.enable_fade_panic,
        enable_divergence_scoring=settings.enable_divergence_scoring,
        enable_cross_asset_strategy=settings.enable_cross_asset_strategy,
    )

    # Live mode safety checks
    warnings = settings.validate_live_mode()
    for w in warnings:
        _log.warning("live_mode_warning", warning=w)

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
    executor = OrderExecutor(settings, book_manager=book_manager)
    rate_limiter = RateLimiter(max_per_minute=55)
    spot_buffer = SpotBuffer(window_seconds=settings.spot_buffer_window, max_size=10000)

    # Trade database (needed for dashboard, decision logging, and state persistence)
    trade_db: TradeDatabase | None = None
    dashboard_server = None
    if settings.dashboard_enabled or settings.enable_decision_logging:
        trade_db = TradeDatabase(settings.db_path)
        state_manager.set_trade_db(trade_db)
        risk_manager.set_trade_db(trade_db)

    # Restore circuit breaker state from DB (survives restarts)
    risk_manager.load_persisted_state()

    # Kelly position sizer
    sizer = PositionSizer(
        kelly_fraction=settings.kelly_fraction,
        min_size=10.0,
        max_size=500.0,
    )
    risk_manager.set_sizer(sizer)
    risk_manager.set_book_manager(book_manager)

    # Restore Kelly inputs from DB if available
    if trade_db is not None:
        kelly_state = trade_db.load_kelly_state()
        if kelly_state and kelly_state["sample_count"] >= 10:
            _log.info(
                "kelly_state_restored",
                win_rate=round(kelly_state["win_rate"], 4),
                avg_win=round(kelly_state["avg_win"], 2),
                avg_loss=round(kelly_state["avg_loss"], 2),
                samples=kelly_state["sample_count"],
            )

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

    # Reconcile sim_balance against trade_results ground truth
    delta = state_manager.reconcile_sim_balance()
    if abs(delta) > 0.01:
        _log.warning(
            "sim_balance_corrected",
            delta=delta,
            new_balance=state_manager.sim_balance,
        )

    # Initial market discovery via MarketManager
    markets = await market_manager.initialize()
    if not markets:
        _log.warning("no_tokens_to_track", msg="Exiting - no active markets found.")
        return

    # Alpha signals (Binance Futures public API: funding rate, OI, vol regime)
    alpha_signals: AlphaSignalProvider | None = None
    if settings.enable_alpha_signals and spot_buffer is not None:
        alpha_signals = AlphaSignalProvider(
            symbols=binance_symbols,
            spot_buffer=spot_buffer,
            funding_poll_seconds=settings.alpha_funding_poll_seconds,
            oi_poll_seconds=settings.alpha_oi_poll_seconds,
            vol_window_seconds=settings.alpha_vol_window_seconds,
            vol_min_data_points=settings.alpha_vol_min_data_points,
            vol_low_threshold=settings.alpha_vol_low_threshold,
            vol_high_threshold=settings.alpha_vol_high_threshold,
            funding_bullish_threshold=settings.alpha_funding_bullish_threshold,
            funding_bearish_threshold=settings.alpha_funding_bearish_threshold,
            oi_rising_threshold=settings.alpha_oi_rising_threshold,
            oi_falling_threshold=settings.alpha_oi_falling_threshold,
        )
        _log.info("alpha_signals_configured", symbols=binance_symbols)

    # Build enabled strategies and scanner
    strategies = _build_strategies(
        settings, book_manager, spot_buffer, trade_db, alpha_signals,
        state_manager=state_manager,
    )
    if not strategies:
        _log.warning("no_strategies_enabled", msg="Enable at least one strategy.")
        return

    scanner = MarketScanner(strategies=strategies, settings=settings)
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

    alpha_signals_task = None
    if alpha_signals is not None:
        alpha_signals_task = asyncio.create_task(alpha_signals.run())

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

    # Decision logger (Phase 1 observability)
    decision_logger: DecisionLogger | None = None
    if trade_db is not None and settings.enable_decision_logging:
        decision_logger = DecisionLogger(trade_db)
        _log.info("decision_logging_enabled")

    # Dynamic allocation (performance-based sizing)
    allocation_manager: AllocationManager | None = None
    if trade_db is not None and settings.enable_dynamic_allocation:
        allocation_manager = AllocationManager(settings, trade_db)
        _log.info("dynamic_allocation_enabled")

    # CEX perp hedging (Binance Futures)
    hedge_manager: HedgeManager | None = None
    if settings.enable_cex_hedging:
        from src.execution.binance_futures import BinanceFuturesClient
        from src.execution.hedge_manager import HedgeManager

        binance_futures_client = BinanceFuturesClient(
            api_key=settings.binance_futures_api_key.get_secret_value(),
            api_secret=settings.binance_futures_api_secret.get_secret_value(),
            testnet=settings.binance_futures_testnet,
        )
        await binance_futures_client.set_leverage(
            "BTCUSDT", settings.cex_hedge_leverage,
        )
        hedge_manager = HedgeManager(
            settings, binance_futures_client, state_manager, spot_buffer,
        )
        _log.info(
            "cex_hedging_enabled",
            testnet=settings.binance_futures_testnet,
            hedge_ratio=settings.cex_hedge_ratio,
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
            decision_logger=decision_logger,
            allocation_manager=allocation_manager,
            hedge_manager=hedge_manager,
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

    # Hedged market-maker monitoring (HMM GTC fill tracking)
    hmm_task = asyncio.create_task(
        _hmm_monitor_loop(
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
            hedge_manager=hedge_manager,
            risk_manager=risk_manager,
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
            hedge_manager=hedge_manager,
            risk_manager=risk_manager,
        ),
    )

    # Spot snapshot loop (periodic price persistence for offline analysis)
    spot_snapshot_task = None
    if trade_db is not None:
        spot_snapshot_task = asyncio.create_task(
            _spot_snapshot_loop(
                spot_buffer=spot_buffer,
                trade_db=trade_db,
                interval=settings.spot_snapshot_interval,
            ),
        )

    # Market outcome loop (track all 15-min window results)
    market_outcome_task = None
    if trade_db is not None:
        market_outcome_task = asyncio.create_task(
            _market_outcome_loop(
                market_manager=market_manager,
                spot_buffer=spot_buffer,
                state_manager=state_manager,
                trade_db=trade_db,
                interval=10.0,
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

    # Graceful shutdown with timeout
    _log.info("shutting_down")
    try:
        await asyncio.wait_for(
            _shutdown_sequence(
                clob_ws, binance_ws, executor, state_manager, risk_manager,
                settings, dashboard_server, trade_db, discovery, alpha_signals,
                hedge_manager, ws_task, binance_task,
                monitor_task, strategy_task, rollover_task, exit_task,
                gtc_task, maker_arb_task, summary_task, dashboard_task,
                snapshot_task, resolution_task, spot_snapshot_task,
                market_outcome_task, alpha_signals_task,
            ),
            timeout=30.0,
        )
    except asyncio.TimeoutError:
        _log.error("shutdown_timeout", msg="Shutdown took >30s, forcing exit")
    except Exception as exc:
        _log.error("shutdown_error", error=str(exc))

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
