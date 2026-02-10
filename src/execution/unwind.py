"""Emergency position flattening for unhedged or shutdown scenarios."""

from __future__ import annotations

from src.core.models import OrderStatus, Position, Side, TradeOrder
from src.core.state import StateManager
from src.monitoring.logger import get_logger

logger = get_logger(__name__)


class EmergencyUnwind:
    """Handles emergency position flattening."""

    def __init__(self, executor: object, state_manager: StateManager) -> None:
        # executor is OrderExecutor but we use duck typing to avoid circular imports
        self._executor = executor
        self._state = state_manager

    async def unwind_position(self, position: Position) -> bool:
        """Attempt to sell all shares in a position at best available prices.

        Creates FOK sell orders at price 0.01 (market sell - lowest acceptable)
        for both YES and NO shares. Returns True if all orders filled successfully.
        """
        orders: list[TradeOrder] = []
        market = position.market

        if position.yes_shares > 0:
            orders.append(
                TradeOrder(
                    token_id=market.yes_token_id,
                    side=Side.SELL,
                    price=0.02,  # Near-market sell (safety floor above 0.01)
                    size=position.yes_shares,
                    order_type="FOK",
                )
            )

        if position.no_shares > 0:
            orders.append(
                TradeOrder(
                    token_id=market.no_token_id,
                    side=Side.SELL,
                    price=0.02,  # Near-market sell (safety floor above 0.01)
                    size=position.no_shares,
                    order_type="FOK",
                )
            )

        if not orders:
            return True  # Nothing to unwind

        logger.warning(
            "unwinding_position",
            condition_id=market.condition_id,
            yes_shares=position.yes_shares,
            no_shares=position.no_shares,
            order_count=len(orders),
        )

        try:
            # Sign all orders in parallel
            signed = await self._executor.sign_orders_parallel(orders)

            # Submit and verify
            all_filled = True
            for order in signed:
                if order.status != OrderStatus.SIGNED:
                    all_filled = False
                    continue
                result = await self._executor.submit_order(order)
                result = await self._executor.verify_fill(result)
                if result.status != OrderStatus.FILLED:
                    all_filled = False
                    logger.error(
                        "unwind_order_not_filled",
                        token_id=order.token_id[:12],
                        status=result.status.value,
                    )

            if all_filled:
                logger.info("position_unwound", condition_id=market.condition_id)
            else:
                logger.error("partial_unwind", condition_id=market.condition_id)

            return all_filled
        except Exception as exc:
            logger.error(
                "unwind_failed",
                condition_id=market.condition_id,
                error=str(exc),
            )
            return False

    async def flatten_all(self) -> dict[str, bool]:
        """Emergency: flatten every open position. Returns {condition_id: success}."""
        positions = self._state.get_all_positions()
        if not positions:
            logger.info("flatten_all_no_positions")
            return {}

        logger.warning("flatten_all_start", position_count=len(positions))

        results: dict[str, bool] = {}
        for position in positions:
            key = f"{position.market.condition_id}:{position.strategy.value}"
            success = await self.unwind_position(position)
            results[key] = success

        successes = sum(1 for v in results.values() if v)
        failures = sum(1 for v in results.values() if not v)
        logger.info(
            "flatten_all_complete",
            total=len(results),
            successes=successes,
            failures=failures,
        )

        return results

    async def unwind_partial_arb(
        self,
        filled_order: TradeOrder,
        unfilled_order: TradeOrder,
    ) -> bool:
        """Unwind a partial arbitrage fill.

        When one leg of an arb fills but the other doesn't, sell the filled
        leg to close exposure.

        Args:
            filled_order: The order that was filled (has shares to sell)
            unfilled_order: The order that was NOT filled (cancel if possible)

        Returns:
            True if the unwind sell was filled.
        """
        # Cancel the unfilled order if it has an order_id
        if unfilled_order.order_id:
            await self._executor.cancel_order(unfilled_order.order_id)

        if filled_order.fill_size <= 0:
            return True  # Nothing to unwind

        # Create a sell order for the filled shares
        sell_order = TradeOrder(
            token_id=filled_order.token_id,
            side=Side.SELL,
            price=0.01,  # Market sell
            size=filled_order.fill_size,
            order_type="FOK",
        )

        logger.warning(
            "unwinding_partial_arb",
            token_id=filled_order.token_id[:12],
            fill_size=filled_order.fill_size,
        )

        try:
            await self._executor.sign_order(sell_order)
            if sell_order.status != OrderStatus.SIGNED:
                return False
            result = await self._executor.submit_order(sell_order)
            result = await self._executor.verify_fill(result)

            success = result.status == OrderStatus.FILLED
            if success:
                logger.info("partial_arb_unwound", token_id=filled_order.token_id[:12])
            else:
                logger.error(
                    "partial_arb_unwind_failed",
                    token_id=filled_order.token_id[:12],
                    status=result.status.value,
                )
            return success
        except Exception as exc:
            logger.error("partial_arb_unwind_error", error=str(exc))
            return False
