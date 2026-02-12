"""Order signing, submission, and fill verification for Polymarket.

Wraps py-clob-client's synchronous API with ``asyncio.to_thread()``
for non-blocking operation.  Supports ``dry_run`` mode where no real
orders are placed.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING

from src.config import Settings
from src.core.models import OrderStatus, Side, TradeOrder
from src.data.orderbook import OrderBookManager
from src.monitoring.logger import get_logger

if TYPE_CHECKING:
    from py_clob_client.client import ClobClient


class OrderExecutor:
    """Handles order signing, submission, and fill verification."""

    def __init__(
        self,
        settings: Settings,
        book_manager: OrderBookManager | None = None,
    ) -> None:
        self._settings = settings
        self._dry_run = settings.dry_run
        self._log = get_logger("executor")
        self._client: ClobClient | None = None  # Lazily initialized
        self._book_manager = book_manager

    # ------------------------------------------------------------------
    # Client initialization
    # ------------------------------------------------------------------

    def _get_client(self) -> ClobClient:
        """Lazily create py-clob-client ``ClobClient`` instance."""
        if self._client is None:
            from py_clob_client.client import ClobClient

            self._client = ClobClient(
                host=self._settings.clob_host,
                key=self._settings.private_key.get_secret_value(),
                chain_id=137,
                signature_type=int(self._settings.signature_type),
                funder=self._settings.funder or None,
            )
            # Derive and set API credentials (HMAC keys for authenticated endpoints)
            creds = self._client.create_or_derive_api_creds()
            self._client.set_api_creds(creds)
            self._log.info("clob_client_initialized", host=self._settings.clob_host)
        return self._client

    # ------------------------------------------------------------------
    # Signing
    # ------------------------------------------------------------------

    async def sign_order(
        self, order: TradeOrder, tick_size: float = 0.01
    ) -> TradeOrder:
        """Sign a single order.

        In dry-run mode a mock signed payload is produced.  In live mode
        the synchronous ``client.create_order()`` is called via
        ``asyncio.to_thread()``, pre-providing *tick_size* and
        *neg_risk* to eliminate HTTP round-trips.
        """
        if self._dry_run:
            order.signed_order = {
                "token_id": order.token_id,
                "side": order.side.value,
                "price": order.price,
                "size": order.size,
                "order_type": order.order_type,
                "dry_run": True,
            }
            order.status = OrderStatus.SIGNED
            self._log.debug(
                "order_signed_dry",
                token_id=order.token_id[:12],
                side=order.side.value,
                price=order.price,
                size=order.size,
            )
            return order

        signed = await asyncio.to_thread(
            self._sign_order_sync, order, tick_size
        )
        return signed

    def _sign_order_sync(
        self, order: TradeOrder, tick_size: float
    ) -> TradeOrder:
        """Synchronous signing via py-clob-client (runs in thread)."""
        from py_clob_client.clob_types import OrderArgs, PartialCreateOrderOptions
        from py_clob_client.order_builder.constants import BUY, SELL

        client = self._get_client()

        order_args = OrderArgs(
            token_id=order.token_id,
            price=order.price,
            size=order.size,
            side=BUY if order.side == Side.BUY else SELL,
        )

        # Pre-provide tick_size and neg_risk to skip HTTP lookups.
        # neg_risk read from config (15-min crypto markets use neg_risk=False).
        options = PartialCreateOrderOptions(
            tick_size=str(tick_size),
            neg_risk=self._settings.neg_risk,
        )

        try:
            signed = client.create_order(order_args, options)
            order.signed_order = signed
            order.status = OrderStatus.SIGNED
            self._log.debug(
                "order_signed_live",
                token_id=order.token_id[:12],
                side=order.side.value,
            )
        except Exception as exc:
            self._log.error(
                "sign_order_failed",
                token_id=order.token_id[:12],
                error=str(exc),
            )
            order.status = OrderStatus.REJECTED

        return order

    async def sign_orders_parallel(
        self, orders: list[TradeOrder], tick_size: float = 0.01
    ) -> list[TradeOrder]:
        """Sign multiple orders concurrently."""
        tasks = [self.sign_order(order, tick_size) for order in orders]
        return list(await asyncio.gather(*tasks))

    # ------------------------------------------------------------------
    # Market quality checks
    # ------------------------------------------------------------------

    def _check_market_quality(self, order: TradeOrder) -> TradeOrder | None:
        """Check liquidity, slippage, and book depth for a BUY order.

        Returns the rejected TradeOrder if checks fail, None if OK.
        """
        if self._book_manager is None:
            return None

        fill_est = self._book_manager.get_fill_estimate(
            order.token_id, order.side, order.size
        )
        if fill_est is None or not fill_est.sufficient_liquidity:
            order.status = OrderStatus.REJECTED
            order.market_condition_rejection = True
            self._log.info(
                "order_rejected_no_liquidity",
                order_id=order.order_id or "pre-submit",
                token_id=order.token_id[:12],
                side=order.side.value,
                size=order.size,
            )
            return order

        # Slippage guard
        max_slippage = self._settings.max_fill_slippage
        if fill_est.best_price > 0 and fill_est.vwap > fill_est.best_price * (1 + max_slippage):
            order.status = OrderStatus.REJECTED
            order.market_condition_rejection = True
            self._log.info(
                "order_rejected_slippage",
                order_id=order.order_id or "pre-submit",
                token_id=order.token_id[:12],
                vwap=round(fill_est.vwap, 4),
                best_price=round(fill_est.best_price, 4),
                slippage_pct=round((fill_est.vwap / fill_est.best_price - 1) * 100, 2),
            )
            return order

        # Book depth guard
        max_levels = self._settings.max_levels_consumed
        if fill_est.levels_consumed > max_levels:
            order.status = OrderStatus.REJECTED
            order.market_condition_rejection = True
            self._log.info(
                "order_rejected_thin_book",
                order_id=order.order_id or "pre-submit",
                token_id=order.token_id[:12],
                levels_consumed=fill_est.levels_consumed,
                max_levels=max_levels,
            )
            return order

        # Store VWAP for dry-run fill simulation
        order._fill_vwap = fill_est.vwap  # type: ignore[attr-defined]
        return None  # All checks passed

    # ------------------------------------------------------------------
    # Submission
    # ------------------------------------------------------------------

    async def submit_order(self, order: TradeOrder) -> TradeOrder:
        """Submit a pre-signed order.

        In dry-run mode the order is immediately marked as filled.
        """
        if order.status != OrderStatus.SIGNED:
            self._log.warning(
                "submit_order_not_signed",
                status=order.status.value,
                token_id=order.token_id[:12],
            )
            return order

        # Market quality checks for non-GTC BUY orders (applies to both live and dry-run)
        if order.order_type != "GTC" and order.side == Side.BUY:
            rejected = self._check_market_quality(order)
            if rejected is not None:
                return rejected

        if self._dry_run:
            order.order_id = f"dry_{uuid.uuid4().hex[:8]}"

            # GTC (maker) orders fill at their limit price, not VWAP.
            # Makers sit on the book and get filled at the price they posted.
            if order.order_type == "GTC":
                order.fill_price = order.price
                order.status = OrderStatus.FILLED
                order.fill_size = order.size
                self._log.info(
                    "order_submitted_dry_gtc",
                    order_id=order.order_id,
                    token_id=order.token_id[:12],
                    side=order.side.value,
                    price=order.price,
                    size=order.size,
                )
                return order

            # Use VWAP from quality check if available
            if hasattr(order, '_fill_vwap'):
                order.fill_price = order._fill_vwap  # type: ignore[attr-defined]
            elif order.side == Side.SELL and self._book_manager is not None:
                book = self._book_manager.get_book(order.token_id)
                if book and book.best_bid:
                    order.fill_price = book.best_bid
                else:
                    order.fill_price = order.price
            else:
                order.fill_price = order.price

            order.status = OrderStatus.FILLED
            order.fill_size = order.size
            self._log.info(
                "order_submitted_dry",
                order_id=order.order_id,
                token_id=order.token_id[:12],
                side=order.side.value,
                price=order.price,
                fill_price=order.fill_price,
                size=order.size,
            )
            return order

        return await asyncio.to_thread(self._submit_order_sync, order)

    def _submit_order_sync(self, order: TradeOrder) -> TradeOrder:
        """Synchronous order submission (runs in thread)."""
        client = self._get_client()

        try:
            resp = client.post_order(order.signed_order)
            # The response may be a dict or object with an orderID field
            if isinstance(resp, dict):
                order.order_id = resp.get("orderID") or resp.get("id", "")
            else:
                order.order_id = getattr(resp, "orderID", "") or getattr(
                    resp, "id", ""
                )

            order.status = OrderStatus.SUBMITTED
            self._log.info(
                "order_submitted_live",
                order_id=order.order_id,
                token_id=order.token_id[:12],
            )
        except Exception as exc:
            error_str = str(exc).lower()
            if "429" in error_str or "rate limit" in error_str:
                self._log.warning(
                    "rate_limit_detected",
                    token_id=order.token_id[:12],
                    error=str(exc),
                )
                order.market_condition_rejection = True
            else:
                self._log.error(
                    "submit_order_failed",
                    token_id=order.token_id[:12],
                    error=str(exc),
                )
            order.status = OrderStatus.REJECTED

        return order

    async def submit_batch(self, orders: list[TradeOrder]) -> list[TradeOrder]:
        """Submit multiple pre-signed orders sequentially."""
        results = []
        for order in orders:
            result = await self.submit_order(order)
            results.append(result)
        return results

    # ------------------------------------------------------------------
    # Fill verification
    # ------------------------------------------------------------------

    async def verify_fill(
        self,
        order: TradeOrder,
        timeout: float | None = None,
        poll_interval: float | None = None,
    ) -> TradeOrder:
        """Poll order status until terminal or timeout.

        In dry-run mode returns immediately (already FILLED).
        """
        if self._dry_run:
            return order

        timeout = timeout if timeout is not None else self._settings.fill_verify_timeout
        poll_interval = (
            poll_interval if poll_interval is not None
            else self._settings.fill_verify_poll_interval
        )

        if not order.order_id:
            return order

        import time as _time

        start = _time.monotonic()
        while (_time.monotonic() - start) < timeout:
            try:
                status = await asyncio.to_thread(
                    self._get_client().get_order, order.order_id
                )
                state = ""
                if isinstance(status, dict):
                    state = status.get("status", "")
                    fill_size = float(status.get("size_matched", 0))
                    fill_price = float(status.get("price", order.price))
                else:
                    state = getattr(status, "status", "")
                    fill_size = float(getattr(status, "size_matched", 0))
                    fill_price = float(getattr(status, "price", order.price))

                if state in ("MATCHED", "FILLED"):
                    order.status = OrderStatus.FILLED
                    order.fill_size = fill_size
                    order.fill_price = fill_price
                    return order
                elif state in ("CANCELLED", "EXPIRED"):
                    order.status = OrderStatus.CANCELLED
                    return order
            except Exception as exc:
                self._log.warning(
                    "verify_fill_error",
                    order_id=order.order_id,
                    error=str(exc),
                )

            await asyncio.sleep(poll_interval)

        # Timeout — assume not filled for FOK orders
        if order.order_type == "FOK":
            order.status = OrderStatus.CANCELLED
        else:
            order.status = OrderStatus.PARTIALLY_FILLED

        self._log.warning(
            "verify_fill_timeout",
            order_id=order.order_id,
            timeout=timeout,
        )
        return order

    # ------------------------------------------------------------------
    # Cancellation
    # ------------------------------------------------------------------

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a specific order."""
        if self._dry_run:
            self._log.info("cancel_order_dry", order_id=order_id)
            return True

        try:
            await asyncio.to_thread(self._get_client().cancel, order_id)
            self._log.info("order_cancelled", order_id=order_id)
            return True
        except Exception as exc:
            self._log.error(
                "cancel_order_failed", order_id=order_id, error=str(exc)
            )
            return False

    async def cancel_all(self) -> bool:
        """Cancel all open orders."""
        if self._dry_run:
            self._log.info("cancel_all_dry")
            return True

        try:
            await asyncio.to_thread(self._get_client().cancel_all)
            self._log.info("all_orders_cancelled")
            return True
        except Exception as exc:
            self._log.error("cancel_all_failed", error=str(exc))
            return False

    # ------------------------------------------------------------------
    # Convenience: full arb execution
    # ------------------------------------------------------------------

    async def execute_arb(
        self,
        yes_order: TradeOrder,
        no_order: TradeOrder,
        tick_size: float = 0.01,
    ) -> tuple[TradeOrder, TradeOrder]:
        """Sign both legs in parallel, submit both, verify both.

        This is the primary method for arbitrage execution.
        """
        # Sign in parallel
        yes_order, no_order = await self.sign_orders_parallel(
            [yes_order, no_order], tick_size
        )

        # Check both signed successfully
        if (
            yes_order.status != OrderStatus.SIGNED
            or no_order.status != OrderStatus.SIGNED
        ):
            self._log.warning(
                "arb_sign_failed",
                yes_status=yes_order.status.value,
                no_status=no_order.status.value,
            )
            return yes_order, no_order

        # Submit both
        yes_order, no_order = await self.submit_batch([yes_order, no_order])

        # Verify fills
        yes_order = await self.verify_fill(yes_order)
        no_order = await self.verify_fill(no_order)

        # Detect partial fill (one leg filled, other didn't)
        yes_filled = yes_order.status == OrderStatus.FILLED
        no_filled = no_order.status == OrderStatus.FILLED

        if yes_filled != no_filled:
            self._log.warning(
                "partial_arb_fill",
                yes_status=yes_order.status.value,
                no_status=no_order.status.value,
                yes_fill_size=yes_order.fill_size,
                no_fill_size=no_order.fill_size,
            )

        self._log.info(
            "arb_executed",
            yes_status=yes_order.status.value,
            no_status=no_order.status.value,
            yes_fill=yes_order.fill_size,
            no_fill=no_order.fill_size,
        )

        return yes_order, no_order
