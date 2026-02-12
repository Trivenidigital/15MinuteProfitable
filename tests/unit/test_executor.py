"""Comprehensive tests for src.execution.executor.OrderExecutor."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

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


# ---------------------------------------------------------------------------
# Live signing path (non-dry-run) — mocked SDK
# ---------------------------------------------------------------------------


class TestLiveSigningPath:
    """Tests that verify the live (non-dry-run) signing path uses correct SDK types.

    These tests mock py_clob_client so no real signing occurs, but they verify
    the executor passes the right types (OrderArgs, PartialCreateOrderOptions)
    to client.create_order(), derives API creds, and reads neg_risk from settings.

    py_clob_client may not be installed in the test environment (it's only on
    the server), so we inject mock modules into sys.modules.
    """

    @pytest.fixture(autouse=True)
    def _mock_clob_sdk(self) -> None:
        """Inject fake py_clob_client modules into sys.modules.

        This lets the lazy ``from py_clob_client.client import ClobClient``
        inside executor.py resolve without the real package installed.
        We define real-ish OrderArgs / PartialCreateOrderOptions dataclasses
        so isinstance() checks work.
        """
        import sys
        import types
        from dataclasses import dataclass

        # Real-ish typed stubs so executor code works identically to prod
        @dataclass
        class OrderArgs:
            token_id: str = ""
            price: float = 0.0
            size: float = 0.0
            side: str = ""

        @dataclass
        class PartialCreateOrderOptions:
            tick_size: str = "0.01"
            neg_risk: bool = False

        # Build module tree
        pkg = types.ModuleType("py_clob_client")
        client_mod = types.ModuleType("py_clob_client.client")
        clob_types_mod = types.ModuleType("py_clob_client.clob_types")
        order_builder_mod = types.ModuleType("py_clob_client.order_builder")
        constants_mod = types.ModuleType("py_clob_client.order_builder.constants")

        # Assign types
        self._OrderArgs = OrderArgs
        self._PartialCreateOrderOptions = PartialCreateOrderOptions
        self._MockClobClient = MagicMock  # placeholder — overridden per-test

        client_mod.ClobClient = MagicMock  # will be replaced per test
        clob_types_mod.OrderArgs = OrderArgs
        clob_types_mod.PartialCreateOrderOptions = PartialCreateOrderOptions
        constants_mod.BUY = "BUY"
        constants_mod.SELL = "SELL"

        # Stash originals for cleanup
        saved = {}
        mod_names = [
            "py_clob_client",
            "py_clob_client.client",
            "py_clob_client.clob_types",
            "py_clob_client.order_builder",
            "py_clob_client.order_builder.constants",
        ]
        for name in mod_names:
            saved[name] = sys.modules.get(name)

        sys.modules["py_clob_client"] = pkg
        sys.modules["py_clob_client.client"] = client_mod
        sys.modules["py_clob_client.clob_types"] = clob_types_mod
        sys.modules["py_clob_client.order_builder"] = order_builder_mod
        sys.modules["py_clob_client.order_builder.constants"] = constants_mod

        self._client_mod = client_mod

        yield

        # Restore
        for name in mod_names:
            if saved[name] is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = saved[name]

    @pytest.fixture()
    def live_settings(self) -> Settings:
        return Settings(
            private_key="0x" + "ab" * 32,
            dry_run=False,
            sim_balance=1000.0,
        )

    def _set_mock_client(self, mock_instance: MagicMock) -> MagicMock:
        """Replace ClobClient in the fake module with a constructor returning mock_instance."""
        mock_cls = MagicMock(return_value=mock_instance)
        self._client_mod.ClobClient = mock_cls
        return mock_cls

    def test_get_client_derives_api_creds(self, live_settings: Settings) -> None:
        """ClobClient should have create_or_derive_api_creds() and set_api_creds() called."""
        mock_client_instance = MagicMock()
        mock_client_instance.create_or_derive_api_creds.return_value = {"apiKey": "test"}
        MockClient = self._set_mock_client(mock_client_instance)

        executor = OrderExecutor(live_settings)
        client = executor._get_client()

        # Verify ClobClient was constructed with correct args
        MockClient.assert_called_once_with(
            host=live_settings.clob_host,
            key=live_settings.private_key.get_secret_value(),
            chain_id=137,
            signature_type=int(live_settings.signature_type),
            funder=None,  # empty funder → None
        )

        # Verify API creds were derived and set
        mock_client_instance.create_or_derive_api_creds.assert_called_once()
        mock_client_instance.set_api_creds.assert_called_once_with({"apiKey": "test"})

        assert client is mock_client_instance

    def test_get_client_passes_funder_when_set(self) -> None:
        """ClobClient should receive the funder address when configured."""
        settings = Settings(
            private_key="0x" + "ab" * 32,
            dry_run=False,
            sim_balance=1000.0,
            funder="0x" + "cc" * 20,
        )
        mock_client_instance = MagicMock()
        mock_client_instance.create_or_derive_api_creds.return_value = {}
        MockClient = self._set_mock_client(mock_client_instance)

        executor = OrderExecutor(settings)
        executor._get_client()

        MockClient.assert_called_once()
        call_kwargs = MockClient.call_args
        assert call_kwargs.kwargs.get("funder") == "0x" + "cc" * 20

    def test_sign_order_uses_typed_order_args(self, live_settings: Settings) -> None:
        """create_order() should receive OrderArgs, not a plain dict."""
        mock_client_instance = MagicMock()
        mock_client_instance.create_or_derive_api_creds.return_value = {}
        mock_client_instance.create_order.return_value = {"signed": True}
        self._set_mock_client(mock_client_instance)

        executor = OrderExecutor(live_settings)
        order = _make_order()
        executor._sign_order_sync(order, tick_size=0.01)

        mock_client_instance.create_order.assert_called_once()
        args, _kwargs = mock_client_instance.create_order.call_args
        assert isinstance(args[0], self._OrderArgs)

    def test_sign_order_uses_typed_options(self, live_settings: Settings) -> None:
        """create_order() should receive PartialCreateOrderOptions, not a plain dict."""
        mock_client_instance = MagicMock()
        mock_client_instance.create_or_derive_api_creds.return_value = {}
        mock_client_instance.create_order.return_value = {"signed": True}
        self._set_mock_client(mock_client_instance)

        executor = OrderExecutor(live_settings)
        order = _make_order()
        executor._sign_order_sync(order, tick_size=0.01)

        args, _kwargs = mock_client_instance.create_order.call_args
        assert isinstance(args[1], self._PartialCreateOrderOptions)

    def test_neg_risk_reads_from_settings(self, live_settings: Settings) -> None:
        """neg_risk in PartialCreateOrderOptions should come from settings, not hardcoded."""
        mock_client_instance = MagicMock()
        mock_client_instance.create_or_derive_api_creds.return_value = {}
        mock_client_instance.create_order.return_value = {"signed": True}
        self._set_mock_client(mock_client_instance)

        # Test with neg_risk=False (default for 15-min crypto)
        executor = OrderExecutor(live_settings)
        order = _make_order()
        executor._sign_order_sync(order, tick_size=0.01)

        args, _kwargs = mock_client_instance.create_order.call_args
        options = args[1]
        assert options.neg_risk is False

        # Test with neg_risk=True
        live_settings_neg = Settings(
            private_key="0x" + "ab" * 32,
            dry_run=False,
            sim_balance=1000.0,
            neg_risk=True,
        )
        mock_client_instance.reset_mock()
        mock_client_instance.create_or_derive_api_creds.return_value = {}
        mock_client_instance.create_order.return_value = {"signed": True}
        self._set_mock_client(mock_client_instance)

        executor2 = OrderExecutor(live_settings_neg)
        order2 = _make_order()
        executor2._sign_order_sync(order2, tick_size=0.01)

        args2, _kwargs2 = mock_client_instance.create_order.call_args
        options2 = args2[1]
        assert options2.neg_risk is True

    def test_sign_order_sets_status_on_success(self, live_settings: Settings) -> None:
        """Successful signing should set status to SIGNED."""
        mock_client_instance = MagicMock()
        mock_client_instance.create_or_derive_api_creds.return_value = {}
        mock_client_instance.create_order.return_value = {"signed": True}
        self._set_mock_client(mock_client_instance)

        executor = OrderExecutor(live_settings)
        order = _make_order()
        result = executor._sign_order_sync(order, tick_size=0.01)
        assert result.status == OrderStatus.SIGNED
        assert result.signed_order == {"signed": True}

    def test_sign_order_sets_rejected_on_error(self, live_settings: Settings) -> None:
        """Failed signing should set status to REJECTED."""
        mock_client_instance = MagicMock()
        mock_client_instance.create_or_derive_api_creds.return_value = {}
        mock_client_instance.create_order.side_effect = RuntimeError("invalid signature")
        self._set_mock_client(mock_client_instance)

        executor = OrderExecutor(live_settings)
        order = _make_order()
        result = executor._sign_order_sync(order, tick_size=0.01)
        assert result.status == OrderStatus.REJECTED
