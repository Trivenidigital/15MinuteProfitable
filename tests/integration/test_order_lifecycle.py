"""Integration tests for order signing (dry-run mode).

These tests verify the executor can sign orders correctly in dry-run mode.
They exercise the full order lifecycle without hitting live trading APIs.
Run with: pytest tests/integration/ --run-integration -v
"""

from __future__ import annotations

import os

os.environ.setdefault("BOT_PRIVATE_KEY", "0x" + "a" * 64)

import pytest

from src.config import Settings
from src.core.models import OrderStatus, Side, TradeOrder
from src.execution.executor import OrderExecutor

pytestmark = pytest.mark.integration


def _make_settings() -> Settings:
    return Settings(
        private_key="0x" + "a" * 64,
        dry_run=True,
        sim_balance=1000.0,
    )


def _make_order(
    token_id: str = "test_token_id_12345",
    side: Side = Side.BUY,
    price: float = 0.50,
    size: float = 10.0,
    order_type: str = "FOK",
) -> TradeOrder:
    return TradeOrder(
        token_id=token_id,
        side=side,
        price=price,
        size=size,
        order_type=order_type,
    )


@pytest.mark.asyncio
async def test_dry_run_order_sign_and_submit():
    """Verify executor can sign and submit in dry-run mode."""
    settings = _make_settings()
    executor = OrderExecutor(settings)

    order = _make_order()

    # Sign
    signed = await executor.sign_order(order)
    assert signed.status == OrderStatus.SIGNED
    assert signed.signed_order is not None

    # Submit
    result = await executor.submit_order(signed)
    assert result.status == OrderStatus.FILLED
    assert result.fill_size == 10.0
    assert result.fill_price == 0.50


@pytest.mark.asyncio
async def test_dry_run_parallel_sign():
    """Verify parallel signing works in dry-run mode."""
    settings = _make_settings()
    executor = OrderExecutor(settings)

    orders = [
        _make_order(token_id=f"token_{i}", side=Side.BUY, price=0.45, size=50.0)
        for i in range(4)
    ]

    signed = await executor.sign_orders_parallel(orders)
    assert len(signed) == 4
    assert all(o.status == OrderStatus.SIGNED for o in signed)


@pytest.mark.asyncio
async def test_dry_run_execute_arb():
    """Verify full arb execution in dry-run mode."""
    settings = _make_settings()
    executor = OrderExecutor(settings)

    yes_order = _make_order(
        token_id="yes_token",
        side=Side.BUY,
        price=0.45,
        size=50.0,
    )
    no_order = _make_order(
        token_id="no_token",
        side=Side.BUY,
        price=0.48,
        size=50.0,
    )

    yes_result, no_result = await executor.execute_arb(yes_order, no_order)
    assert yes_result.status == OrderStatus.FILLED
    assert no_result.status == OrderStatus.FILLED
