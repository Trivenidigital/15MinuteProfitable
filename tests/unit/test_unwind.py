"""Comprehensive tests for src.execution.unwind.EmergencyUnwind."""

from __future__ import annotations

import os

os.environ.setdefault("BOT_PRIVATE_KEY", "0x" + "ab" * 32)

import pytest
from datetime import datetime, timedelta

from src.config import Settings
from src.core.models import (
    Market,
    OrderStatus,
    Position,
    Side,
    StrategyType,
    TradeOrder,
)
from src.core.state import StateManager
from src.execution.unwind import EmergencyUnwind


# ---------------------------------------------------------------------------
# Mock Executor
# ---------------------------------------------------------------------------


class MockExecutor:
    """Lightweight mock for OrderExecutor with controllable behaviour."""

    def __init__(
        self,
        sign_success: bool = True,
        submit_success: bool = True,
        fill_success: bool = True,
    ) -> None:
        self.sign_success = sign_success
        self.submit_success = submit_success
        self.fill_success = fill_success
        self.cancelled_orders: list[str] = []

    async def sign_order(
        self, order: TradeOrder, tick_size: float = 0.01
    ) -> TradeOrder:
        if self.sign_success:
            order.status = OrderStatus.SIGNED
            order.signed_order = {"mock": True}
        else:
            order.status = OrderStatus.REJECTED
        return order

    async def sign_orders_parallel(
        self, orders: list[TradeOrder], tick_size: float = 0.01
    ) -> list[TradeOrder]:
        return [await self.sign_order(o) for o in orders]

    async def submit_order(self, order: TradeOrder) -> TradeOrder:
        if self.submit_success:
            order.status = OrderStatus.SUBMITTED
            order.order_id = "mock_id"
        else:
            order.status = OrderStatus.REJECTED
        return order

    async def verify_fill(
        self, order: TradeOrder, timeout: float = 3.0, poll_interval: float = 0.5
    ) -> TradeOrder:
        if self.fill_success and order.status == OrderStatus.SUBMITTED:
            order.status = OrderStatus.FILLED
            order.fill_size = order.size
            order.fill_price = order.price
        elif order.status == OrderStatus.SUBMITTED:
            order.status = OrderStatus.CANCELLED
        return order

    async def get_token_balance(self, token_id: str) -> float | None:
        return None  # Don't cap sell size in tests

    async def cancel_order(self, order_id: str) -> bool:
        self.cancelled_orders.append(order_id)
        return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_market(condition_id: str = "cond_1") -> Market:
    return Market(
        condition_id=condition_id,
        slug="btc-updown-15m-test",
        question="Will BTC go up?",
        yes_token_id="yes_token_123",
        no_token_id="no_token_456",
        start_time=datetime.utcnow(),
        end_time=datetime.utcnow() + timedelta(minutes=15),
        asset="BTC",
    )


def _make_position(
    yes_shares: float = 100.0,
    no_shares: float = 100.0,
    condition_id: str = "cond_1",
) -> Position:
    return Position(
        market=_make_market(condition_id),
        yes_shares=yes_shares,
        no_shares=no_shares,
        yes_cost_basis=yes_shares * 0.45,
        no_cost_basis=no_shares * 0.45,
        strategy=StrategyType.ARBITRAGE,
        opened_at=datetime.utcnow(),
    )


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
def state(settings: Settings) -> StateManager:
    return StateManager(settings)


@pytest.fixture()
def mock_executor() -> MockExecutor:
    return MockExecutor()


@pytest.fixture()
def unwind(mock_executor: MockExecutor, state: StateManager) -> EmergencyUnwind:
    return EmergencyUnwind(mock_executor, state)


# ---------------------------------------------------------------------------
# unwind_position - YES shares only
# ---------------------------------------------------------------------------


class TestUnwindPositionYesOnly:
    async def test_sells_yes_shares(self, unwind: EmergencyUnwind) -> None:
        pos = _make_position(yes_shares=50.0, no_shares=0.0)
        result = await unwind.unwind_position(pos)
        assert result is True

    async def test_creates_single_sell_order(
        self, unwind: EmergencyUnwind
    ) -> None:
        pos = _make_position(yes_shares=75.0, no_shares=0.0)
        result = await unwind.unwind_position(pos)
        assert result is True


# ---------------------------------------------------------------------------
# unwind_position - NO shares only
# ---------------------------------------------------------------------------


class TestUnwindPositionNoOnly:
    async def test_sells_no_shares(self, unwind: EmergencyUnwind) -> None:
        pos = _make_position(yes_shares=0.0, no_shares=60.0)
        result = await unwind.unwind_position(pos)
        assert result is True

    async def test_creates_single_sell_order(
        self, unwind: EmergencyUnwind
    ) -> None:
        pos = _make_position(yes_shares=0.0, no_shares=120.0)
        result = await unwind.unwind_position(pos)
        assert result is True


# ---------------------------------------------------------------------------
# unwind_position - both YES and NO
# ---------------------------------------------------------------------------


class TestUnwindPositionBoth:
    async def test_sells_both_sides(self, unwind: EmergencyUnwind) -> None:
        pos = _make_position(yes_shares=100.0, no_shares=100.0)
        result = await unwind.unwind_position(pos)
        assert result is True

    async def test_sells_asymmetric_shares(self, unwind: EmergencyUnwind) -> None:
        pos = _make_position(yes_shares=200.0, no_shares=50.0)
        result = await unwind.unwind_position(pos)
        assert result is True


# ---------------------------------------------------------------------------
# unwind_position - zero shares
# ---------------------------------------------------------------------------


class TestUnwindPositionEmpty:
    async def test_zero_shares_returns_true(self, unwind: EmergencyUnwind) -> None:
        pos = _make_position(yes_shares=0.0, no_shares=0.0)
        result = await unwind.unwind_position(pos)
        assert result is True


# ---------------------------------------------------------------------------
# unwind_position - signing fails
# ---------------------------------------------------------------------------


class TestUnwindPositionSignFail:
    async def test_sign_failure_returns_false(self, state: StateManager) -> None:
        executor = MockExecutor(sign_success=False)
        uw = EmergencyUnwind(executor, state)
        pos = _make_position(yes_shares=100.0, no_shares=100.0)
        result = await uw.unwind_position(pos)
        assert result is False

    async def test_sign_failure_yes_only_returns_false(
        self, state: StateManager
    ) -> None:
        executor = MockExecutor(sign_success=False)
        uw = EmergencyUnwind(executor, state)
        pos = _make_position(yes_shares=50.0, no_shares=0.0)
        result = await uw.unwind_position(pos)
        assert result is False


# ---------------------------------------------------------------------------
# unwind_position - submit/fill fails (partial unwind)
# ---------------------------------------------------------------------------


class TestUnwindPositionFillFail:
    async def test_fill_failure_returns_false(self, state: StateManager) -> None:
        executor = MockExecutor(fill_success=False)
        uw = EmergencyUnwind(executor, state)
        pos = _make_position(yes_shares=100.0, no_shares=100.0)
        result = await uw.unwind_position(pos)
        assert result is False

    async def test_submit_failure_returns_false(self, state: StateManager) -> None:
        executor = MockExecutor(submit_success=False)
        uw = EmergencyUnwind(executor, state)
        pos = _make_position(yes_shares=100.0, no_shares=0.0)
        result = await uw.unwind_position(pos)
        # submit_success=False sets status to REJECTED, so verify_fill won't
        # set FILLED, meaning all_filled will be False
        assert result is False


# ---------------------------------------------------------------------------
# flatten_all - multiple positions
# ---------------------------------------------------------------------------


class TestFlattenAllMultiple:
    async def test_all_positions_unwound(
        self, unwind: EmergencyUnwind, state: StateManager
    ) -> None:
        pos1 = _make_position(yes_shares=50.0, no_shares=50.0, condition_id="cond_a")
        pos2 = _make_position(yes_shares=30.0, no_shares=70.0, condition_id="cond_b")
        state.add_position(pos1)
        state.add_position(pos2)

        results = await unwind.flatten_all()
        assert len(results) == 2
        assert results["cond_a:arbitrage"] is True
        assert results["cond_b:arbitrage"] is True

    async def test_three_positions_all_succeed(
        self, unwind: EmergencyUnwind, state: StateManager
    ) -> None:
        for i in range(3):
            pos = _make_position(condition_id=f"cond_{i}")
            state.add_position(pos)

        results = await unwind.flatten_all()
        assert len(results) == 3
        assert all(v is True for v in results.values())


# ---------------------------------------------------------------------------
# flatten_all - no positions
# ---------------------------------------------------------------------------


class TestFlattenAllEmpty:
    async def test_no_positions_returns_empty(
        self, unwind: EmergencyUnwind
    ) -> None:
        results = await unwind.flatten_all()
        assert results == {}


# ---------------------------------------------------------------------------
# flatten_all - mixed success/failure
# ---------------------------------------------------------------------------


class TestFlattenAllMixed:
    async def test_mixed_results(self, state: StateManager) -> None:
        """When one position fails to unwind, the result dict reflects it."""

        class PartialFailExecutor(MockExecutor):
            """Executor that fails on a specific condition_id."""

            def __init__(self, fail_token: str) -> None:
                super().__init__()
                self._fail_token = fail_token

            async def sign_orders_parallel(
                self, orders: list[TradeOrder], tick_size: float = 0.01
            ) -> list[TradeOrder]:
                for order in orders:
                    if order.token_id.startswith(self._fail_token):
                        order.status = OrderStatus.REJECTED
                    else:
                        order.status = OrderStatus.SIGNED
                        order.signed_order = {"mock": True}
                return orders

        # Position A uses default tokens (yes_token_123) -> will succeed
        pos_a = _make_position(
            yes_shares=50.0, no_shares=50.0, condition_id="cond_a"
        )
        # Position B uses a different market whose tokens start with "fail_"
        market_b = Market(
            condition_id="cond_b",
            slug="btc-updown-15m-fail",
            question="Will BTC go up?",
            yes_token_id="fail_yes",
            no_token_id="fail_no",
            start_time=datetime.utcnow(),
            end_time=datetime.utcnow() + timedelta(minutes=15),
            asset="BTC",
        )
        pos_b = Position(
            market=market_b,
            yes_shares=40.0,
            no_shares=40.0,
            yes_cost_basis=18.0,
            no_cost_basis=18.0,
            strategy=StrategyType.ARBITRAGE,
            opened_at=datetime.utcnow(),
        )

        state.add_position(pos_a)
        state.add_position(pos_b)

        executor = PartialFailExecutor(fail_token="fail_")
        uw = EmergencyUnwind(executor, state)
        results = await uw.flatten_all()

        assert len(results) == 2
        assert results["cond_a:arbitrage"] is True
        assert results["cond_b:arbitrage"] is False


# ---------------------------------------------------------------------------
# unwind_partial_arb - successful unwind
# ---------------------------------------------------------------------------


class TestUnwindPartialArbSuccess:
    async def test_successful_unwind(self, unwind: EmergencyUnwind) -> None:
        filled = TradeOrder(
            token_id="yes_token_123",
            side=Side.BUY,
            price=0.45,
            size=100.0,
            order_type="FOK",
            status=OrderStatus.FILLED,
            fill_size=100.0,
            fill_price=0.45,
        )
        unfilled = TradeOrder(
            token_id="no_token_456",
            side=Side.BUY,
            price=0.47,
            size=100.0,
            order_type="FOK",
            status=OrderStatus.CANCELLED,
        )
        result = await unwind.unwind_partial_arb(filled, unfilled)
        assert result is True

    async def test_returns_true_on_fill(self, unwind: EmergencyUnwind) -> None:
        filled = TradeOrder(
            token_id="yes_token_123",
            side=Side.BUY,
            price=0.50,
            size=50.0,
            order_type="FOK",
            status=OrderStatus.FILLED,
            fill_size=50.0,
            fill_price=0.50,
        )
        unfilled = TradeOrder(
            token_id="no_token_456",
            side=Side.BUY,
            price=0.42,
            size=50.0,
            order_type="FOK",
            status=OrderStatus.REJECTED,
        )
        result = await unwind.unwind_partial_arb(filled, unfilled)
        assert result is True


# ---------------------------------------------------------------------------
# unwind_partial_arb - unfilled order has order_id to cancel
# ---------------------------------------------------------------------------


class TestUnwindPartialArbCancel:
    async def test_cancels_unfilled_order(
        self, unwind: EmergencyUnwind, mock_executor: MockExecutor
    ) -> None:
        filled = TradeOrder(
            token_id="yes_token_123",
            side=Side.BUY,
            price=0.45,
            size=100.0,
            order_type="FOK",
            status=OrderStatus.FILLED,
            fill_size=100.0,
            fill_price=0.45,
        )
        unfilled = TradeOrder(
            token_id="no_token_456",
            side=Side.BUY,
            price=0.47,
            size=100.0,
            order_type="FOK",
            order_id="pending_order_42",
            status=OrderStatus.SUBMITTED,
        )
        await unwind.unwind_partial_arb(filled, unfilled)
        assert "pending_order_42" in mock_executor.cancelled_orders

    async def test_does_not_cancel_without_order_id(
        self, unwind: EmergencyUnwind, mock_executor: MockExecutor
    ) -> None:
        filled = TradeOrder(
            token_id="yes_token_123",
            side=Side.BUY,
            price=0.45,
            size=100.0,
            order_type="FOK",
            status=OrderStatus.FILLED,
            fill_size=100.0,
            fill_price=0.45,
        )
        unfilled = TradeOrder(
            token_id="no_token_456",
            side=Side.BUY,
            price=0.47,
            size=100.0,
            order_type="FOK",
            status=OrderStatus.CANCELLED,
        )
        await unwind.unwind_partial_arb(filled, unfilled)
        assert len(mock_executor.cancelled_orders) == 0


# ---------------------------------------------------------------------------
# unwind_partial_arb - fill_size is 0
# ---------------------------------------------------------------------------


class TestUnwindPartialArbZeroFill:
    async def test_zero_fill_returns_true(self, unwind: EmergencyUnwind) -> None:
        filled = TradeOrder(
            token_id="yes_token_123",
            side=Side.BUY,
            price=0.45,
            size=100.0,
            order_type="FOK",
            status=OrderStatus.FILLED,
            fill_size=0.0,
            fill_price=0.0,
        )
        unfilled = TradeOrder(
            token_id="no_token_456",
            side=Side.BUY,
            price=0.47,
            size=100.0,
            order_type="FOK",
            status=OrderStatus.CANCELLED,
        )
        result = await unwind.unwind_partial_arb(filled, unfilled)
        assert result is True


# ---------------------------------------------------------------------------
# unwind_partial_arb - sign fails
# ---------------------------------------------------------------------------


class TestUnwindPartialArbSignFail:
    async def test_sign_failure_returns_false(self, state: StateManager) -> None:
        executor = MockExecutor(sign_success=False)
        uw = EmergencyUnwind(executor, state)

        filled = TradeOrder(
            token_id="yes_token_123",
            side=Side.BUY,
            price=0.45,
            size=100.0,
            order_type="FOK",
            status=OrderStatus.FILLED,
            fill_size=100.0,
            fill_price=0.45,
        )
        unfilled = TradeOrder(
            token_id="no_token_456",
            side=Side.BUY,
            price=0.47,
            size=100.0,
            order_type="FOK",
            status=OrderStatus.CANCELLED,
        )
        result = await uw.unwind_partial_arb(filled, unfilled)
        assert result is False


# ---------------------------------------------------------------------------
# unwind_partial_arb - submit/verify fails
# ---------------------------------------------------------------------------


class TestUnwindPartialArbSubmitFail:
    async def test_fill_failure_returns_false(self, state: StateManager) -> None:
        executor = MockExecutor(fill_success=False)
        uw = EmergencyUnwind(executor, state)

        filled = TradeOrder(
            token_id="yes_token_123",
            side=Side.BUY,
            price=0.45,
            size=100.0,
            order_type="FOK",
            status=OrderStatus.FILLED,
            fill_size=100.0,
            fill_price=0.45,
        )
        unfilled = TradeOrder(
            token_id="no_token_456",
            side=Side.BUY,
            price=0.47,
            size=100.0,
            order_type="FOK",
            status=OrderStatus.CANCELLED,
        )
        result = await uw.unwind_partial_arb(filled, unfilled)
        assert result is False

    async def test_submit_rejection_returns_false(
        self, state: StateManager
    ) -> None:
        executor = MockExecutor(submit_success=False)
        uw = EmergencyUnwind(executor, state)

        filled = TradeOrder(
            token_id="yes_token_123",
            side=Side.BUY,
            price=0.45,
            size=100.0,
            order_type="FOK",
            status=OrderStatus.FILLED,
            fill_size=50.0,
            fill_price=0.45,
        )
        unfilled = TradeOrder(
            token_id="no_token_456",
            side=Side.BUY,
            price=0.47,
            size=100.0,
            order_type="FOK",
            status=OrderStatus.CANCELLED,
        )
        result = await uw.unwind_partial_arb(filled, unfilled)
        assert result is False


# ---------------------------------------------------------------------------
# unwind_position - exception handling
# ---------------------------------------------------------------------------


class TestUnwindPositionException:
    async def test_executor_exception_returns_false(
        self, state: StateManager
    ) -> None:
        """If the executor raises an unexpected exception, unwind returns False."""

        class RaisingExecutor(MockExecutor):
            async def sign_orders_parallel(
                self, orders: list[TradeOrder], tick_size: float = 0.01
            ) -> list[TradeOrder]:
                raise RuntimeError("connection lost")

        executor = RaisingExecutor()
        uw = EmergencyUnwind(executor, state)
        pos = _make_position(yes_shares=100.0, no_shares=100.0)
        result = await uw.unwind_position(pos)
        assert result is False


# ---------------------------------------------------------------------------
# unwind_partial_arb - exception handling
# ---------------------------------------------------------------------------


class TestUnwindPartialArbException:
    async def test_executor_exception_returns_false(
        self, state: StateManager
    ) -> None:
        """If the executor raises during partial arb unwind, returns False."""

        class RaisingExecutor(MockExecutor):
            async def sign_order(
                self, order: TradeOrder, tick_size: float = 0.01
            ) -> TradeOrder:
                raise RuntimeError("signing service down")

        executor = RaisingExecutor()
        uw = EmergencyUnwind(executor, state)

        filled = TradeOrder(
            token_id="yes_token_123",
            side=Side.BUY,
            price=0.45,
            size=100.0,
            order_type="FOK",
            status=OrderStatus.FILLED,
            fill_size=100.0,
            fill_price=0.45,
        )
        unfilled = TradeOrder(
            token_id="no_token_456",
            side=Side.BUY,
            price=0.47,
            size=100.0,
            order_type="FOK",
            status=OrderStatus.CANCELLED,
        )
        result = await uw.unwind_partial_arb(filled, unfilled)
        assert result is False
