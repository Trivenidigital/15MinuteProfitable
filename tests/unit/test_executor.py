"""Comprehensive tests for src.execution.executor.OrderExecutor."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

os.environ.setdefault("BOT_PRIVATE_KEY", "0x" + "ab" * 32)

import pytest

from src.config import Settings
from src.core.models import OrderStatus, Side, TradeOrder
from src.execution.executor import OrderExecutor


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        private_key="0x" + "ab" * 32,
        dry_run=True,
        sim_balance=1000.0,
    )


@pytest.fixture()
def executor(settings: Settings) -> OrderExecutor:
    return OrderExecutor(settings)


def _make_order(
    token_id: str = "token_yes_123",
    side: Side = Side.BUY,
    price: float = 0.45,
    size: float = 50.0,
    order_type: str = "FOK",
) -> TradeOrder:
    return TradeOrder(
        token_id=token_id,
        side=side,
        price=price,
        size=size,
        order_type=order_type,
    )


# ---------------------------------------------------------------------------
# sign_order (dry run)
# ---------------------------------------------------------------------------


class TestSignOrderDryRun:
    """Tests for sign_order in dry_run mode."""

    async def test_sign_order_sets_signed_status(self, executor: OrderExecutor) -> None:
        order = _make_order()
        result = await executor.sign_order(order)
        assert result.status == OrderStatus.SIGNED

    async def test_sign_order_creates_signed_payload(self, executor: OrderExecutor) -> None:
        order = _make_order()
        result = await executor.sign_order(order)
        assert result.signed_order is not None
        assert result.signed_order["dry_run"] is True
        assert result.signed_order["token_id"] == order.token_id
        assert result.signed_order["price"] == order.price
        assert result.signed_order["size"] == order.size

    async def test_sign_order_preserves_order_fields(self, executor: OrderExecutor) -> None:
        order = _make_order(side=Side.SELL, price=0.60, size=100.0)
        result = await executor.sign_order(order)
        assert result.side == Side.SELL
        assert result.price == 0.60
        assert result.size == 100.0


# ---------------------------------------------------------------------------
# sign_orders_parallel (dry run)
# ---------------------------------------------------------------------------


class TestSignOrdersParallelDryRun:
    async def test_all_orders_signed(self, executor: OrderExecutor) -> None:
        orders = [
            _make_order(token_id="yes_tok"),
            _make_order(token_id="no_tok", price=0.47),
        ]
        results = await executor.sign_orders_parallel(orders)
        assert len(results) == 2
        assert all(o.status == OrderStatus.SIGNED for o in results)

    async def test_returns_correct_order(self, executor: OrderExecutor) -> None:
        orders = [
            _make_order(token_id="a", price=0.30),
            _make_order(token_id="b", price=0.50),
        ]
        results = await executor.sign_orders_parallel(orders)
        assert results[0].token_id == "a"
        assert results[1].token_id == "b"


# ---------------------------------------------------------------------------
# submit_order (dry run)
# ---------------------------------------------------------------------------


class TestSubmitOrderDryRun:
    async def test_submit_sets_filled(self, executor: OrderExecutor) -> None:
        order = _make_order()
        await executor.sign_order(order)
        result = await executor.submit_order(order)
        assert result.status == OrderStatus.FILLED

    async def test_submit_sets_fill_size_and_price(self, executor: OrderExecutor) -> None:
        order = _make_order(price=0.45, size=50.0)
        await executor.sign_order(order)
        result = await executor.submit_order(order)
        assert result.fill_size == 50.0
        assert result.fill_price == 0.45

    async def test_submit_creates_order_id(self, executor: OrderExecutor) -> None:
        order = _make_order()
        await executor.sign_order(order)
        result = await executor.submit_order(order)
        assert result.order_id is not None
        assert result.order_id.startswith("dry_")

    async def test_submit_unsigned_order_skipped(self, executor: OrderExecutor) -> None:
        """Submitting an unsigned order should not change its status."""
        order = _make_order()
        assert order.status == OrderStatus.PENDING
        result = await executor.submit_order(order)
        assert result.status == OrderStatus.PENDING


# ---------------------------------------------------------------------------
# submit_batch (dry run)
# ---------------------------------------------------------------------------


class TestSubmitBatchDryRun:
    async def test_batch_all_filled(self, executor: OrderExecutor) -> None:
        orders = [_make_order(token_id="a"), _make_order(token_id="b")]
        signed = await executor.sign_orders_parallel(orders)
        results = await executor.submit_batch(signed)
        assert len(results) == 2
        assert all(o.status == OrderStatus.FILLED for o in results)


# ---------------------------------------------------------------------------
# verify_fill (dry run)
# ---------------------------------------------------------------------------


class TestVerifyFillDryRun:
    async def test_returns_immediately(self, executor: OrderExecutor) -> None:
        order = _make_order()
        await executor.sign_order(order)
        await executor.submit_order(order)
        result = await executor.verify_fill(order)
        assert result.status == OrderStatus.FILLED


# ---------------------------------------------------------------------------
# execute_arb (dry run)
# ---------------------------------------------------------------------------


class TestExecuteArbDryRun:
    async def test_both_orders_filled(self, executor: OrderExecutor) -> None:
        yes = _make_order(token_id="yes_tok", price=0.45)
        no = _make_order(token_id="no_tok", price=0.47)
        yes_result, no_result = await executor.execute_arb(yes, no)
        assert yes_result.status == OrderStatus.FILLED
        assert no_result.status == OrderStatus.FILLED

    async def test_fill_sizes_correct(self, executor: OrderExecutor) -> None:
        yes = _make_order(token_id="yes_tok", price=0.45, size=100.0)
        no = _make_order(token_id="no_tok", price=0.47, size=100.0)
        yes_r, no_r = await executor.execute_arb(yes, no)
        assert yes_r.fill_size == 100.0
        assert no_r.fill_size == 100.0
        assert yes_r.fill_price == 0.45
        assert no_r.fill_price == 0.47

    async def test_both_get_order_ids(self, executor: OrderExecutor) -> None:
        yes = _make_order(token_id="yes_tok")
        no = _make_order(token_id="no_tok")
        yes_r, no_r = await executor.execute_arb(yes, no)
        assert yes_r.order_id is not None
        assert no_r.order_id is not None
        assert yes_r.order_id != no_r.order_id


# ---------------------------------------------------------------------------
# cancel (dry run)
# ---------------------------------------------------------------------------


class TestCancelDryRun:
    async def test_cancel_order_returns_true(self, executor: OrderExecutor) -> None:
        result = await executor.cancel_order("some_order_id")
        assert result is True

    async def test_cancel_all_returns_true(self, executor: OrderExecutor) -> None:
        result = await executor.cancel_all()
        assert result is True


# ---------------------------------------------------------------------------
# execute_arb partial fill detection
# ---------------------------------------------------------------------------


class TestExecuteArbPartialFill:
    """Tests for partial fill detection in execute_arb."""

    async def _make_verify_fill_side_effect(
        self,
        yes_status: OrderStatus,
        no_status: OrderStatus,
        yes_token_id: str,
        no_token_id: str,
    ) -> AsyncMock:
        """Create a mock verify_fill that returns different statuses per token."""

        async def _verify(order: TradeOrder, **kwargs: object) -> TradeOrder:
            if order.token_id == yes_token_id:
                order.status = yes_status
                if yes_status == OrderStatus.FILLED:
                    order.fill_size = order.size
                    order.fill_price = order.price
                else:
                    order.fill_size = 0.0
                    order.fill_price = 0.0
            else:
                order.status = no_status
                if no_status == OrderStatus.FILLED:
                    order.fill_size = order.size
                    order.fill_price = order.price
                else:
                    order.fill_size = 0.0
                    order.fill_price = 0.0
            return order

        return _verify

    async def test_execute_arb_partial_fill_yes_only(
        self, executor: OrderExecutor
    ) -> None:
        """YES fills but NO gets cancelled -- partial fill detected."""
        yes = _make_order(token_id="yes_tok", price=0.45)
        no = _make_order(token_id="no_tok", price=0.47)

        verify_side_effect = await self._make_verify_fill_side_effect(
            OrderStatus.FILLED, OrderStatus.CANCELLED, "yes_tok", "no_tok"
        )

        with patch.object(executor, "verify_fill", side_effect=verify_side_effect):
            yes_r, no_r = await executor.execute_arb(yes, no)

        assert yes_r.status == OrderStatus.FILLED
        assert no_r.status == OrderStatus.CANCELLED
        assert yes_r.fill_size == 50.0
        assert no_r.fill_size == 0.0

    async def test_execute_arb_partial_fill_no_only(
        self, executor: OrderExecutor
    ) -> None:
        """NO fills but YES gets cancelled -- partial fill detected."""
        yes = _make_order(token_id="yes_tok", price=0.45)
        no = _make_order(token_id="no_tok", price=0.47)

        verify_side_effect = await self._make_verify_fill_side_effect(
            OrderStatus.CANCELLED, OrderStatus.FILLED, "yes_tok", "no_tok"
        )

        with patch.object(executor, "verify_fill", side_effect=verify_side_effect):
            yes_r, no_r = await executor.execute_arb(yes, no)

        assert yes_r.status == OrderStatus.CANCELLED
        assert no_r.status == OrderStatus.FILLED
        assert yes_r.fill_size == 0.0
        assert no_r.fill_size == 50.0

    async def test_execute_arb_both_filled(self, executor: OrderExecutor) -> None:
        """Both legs fill successfully -- no partial fill."""
        yes = _make_order(token_id="yes_tok", price=0.45)
        no = _make_order(token_id="no_tok", price=0.47)

        # Dry run: both fill by default
        yes_r, no_r = await executor.execute_arb(yes, no)

        assert yes_r.status == OrderStatus.FILLED
        assert no_r.status == OrderStatus.FILLED
        assert yes_r.fill_size == 50.0
        assert no_r.fill_size == 50.0

    async def test_execute_arb_both_cancelled(self, executor: OrderExecutor) -> None:
        """Both legs cancelled (FOK rejected) -- no partial fill."""
        yes = _make_order(token_id="yes_tok", price=0.45)
        no = _make_order(token_id="no_tok", price=0.47)

        verify_side_effect = await self._make_verify_fill_side_effect(
            OrderStatus.CANCELLED, OrderStatus.CANCELLED, "yes_tok", "no_tok"
        )

        with patch.object(executor, "verify_fill", side_effect=verify_side_effect):
            yes_r, no_r = await executor.execute_arb(yes, no)

        assert yes_r.status == OrderStatus.CANCELLED
        assert no_r.status == OrderStatus.CANCELLED
        assert yes_r.fill_size == 0.0
        assert no_r.fill_size == 0.0
