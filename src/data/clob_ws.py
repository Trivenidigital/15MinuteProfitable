"""WebSocket client for the Polymarket CLOB orderbook feed.

Connects to the Polymarket CLOB WebSocket, subscribes to token IDs,
and feeds book snapshots and deltas into an OrderBookManager.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import AsyncGenerator

import websockets

from src.data.orderbook import OrderBookManager
from src.monitoring.logger import get_logger


class ClobWebSocket:
    """WebSocket client for CLOB orderbook feed.

    Connects to wss://ws-subscriptions-clob.polymarket.com/ws/market,
    subscribes to token IDs, and feeds book snapshots and deltas
    into an OrderBookManager.
    """

    def __init__(
        self,
        ws_url: str,
        book_manager: OrderBookManager,
        on_update: Callable[[str, str], None] | None = None,
    ) -> None:
        self._ws_url = ws_url
        self._book_manager = book_manager
        self._on_update = on_update  # callback(token_id, event_type)
        self._subscribed_tokens: set[str] = set()
        self._ws = None  # the active websocket connection
        self._running = False
        self._log = get_logger("clob_ws")
        self._delta_count: int = 0
        self._delta_log_interval: int = 100

    # -- subscription management ------------------------------------------------

    async def subscribe(self, token_ids: list[str]) -> None:
        """Add token_ids to the subscription set.

        If connected, send subscribe message immediately.
        """
        self._subscribed_tokens.update(token_ids)

        for token_id in token_ids:
            self._book_manager.ensure_book(token_id)

        if self._ws is not None:
            msg = {"assets_ids": token_ids, "type": "MARKET"}
            try:
                await self._ws.send(json.dumps(msg))
                self._log.info("subscribed", token_ids=token_ids)
            except Exception as exc:
                self._log.warning(
                    "subscribe_send_failed", error=str(exc), token_ids=token_ids
                )

    async def unsubscribe(self, token_ids: list[str]) -> None:
        """Remove token_ids from subscription set."""
        for token_id in token_ids:
            self._subscribed_tokens.discard(token_id)
        self._log.info("unsubscribed", token_ids=token_ids)

    # -- main loop --------------------------------------------------------------

    async def run(self) -> None:
        """Main loop: connect, subscribe, process messages.

        Auto-reconnects with exponential backoff on disconnect.
        Backoff: 1s, 2s, 4s, 8s, ... max 60s. Reset on successful connect.
        """
        self._running = True
        backoff = 1.0

        while self._running:
            try:
                async with websockets.connect(
                    self._ws_url,
                    ping_interval=10,
                    ping_timeout=10,
                    open_timeout=10,
                    close_timeout=5,
                ) as ws:
                    self._ws = ws
                    self._log.info("connected", url=self._ws_url)
                    backoff = 1.0  # reset on successful connect

                    # Send subscription for all tracked tokens
                    if self._subscribed_tokens:
                        msg = {
                            "assets_ids": list(self._subscribed_tokens),
                            "type": "MARKET",
                        }
                        await ws.send(json.dumps(msg))
                        self._log.info(
                            "resubscribed",
                            token_count=len(self._subscribed_tokens),
                        )

                    # Process messages
                    async for raw in ws:
                        self._process_message(raw)

            except (websockets.ConnectionClosed, ConnectionError, OSError) as exc:
                self._ws = None
                if not self._running:
                    break
                self._log.warning(
                    "disconnected", error=str(exc), reconnect_in=backoff
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

            except Exception as exc:
                self._ws = None
                self._log.error("unexpected_error", error=str(exc))
                if not self._running:
                    break
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

        self._log.info("run_loop_exited")

    async def stop(self) -> None:
        """Signal the run loop to stop and close the active connection."""
        self._running = False
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass

    # -- message processing -----------------------------------------------------

    def _process_message(self, raw: str | bytes) -> None:
        """Parse a WebSocket message and update the book manager.

        Messages can be single JSON objects or JSON arrays.
        Event types:
        - "book": full snapshot -> book.apply_snapshot(bids, asks)
        - "price_change": incremental -> book.apply_delta(changes)
        - Others: ignored

        After processing, call self._on_update(asset_id, event_type) if set.
        """
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            self._log.warning("json_parse_error", error=str(exc))
            return

        # Normalise to a list so both single-object and array payloads
        # are handled uniformly.
        events: list[dict] = data if isinstance(data, list) else [data]

        for event in events:
            if not isinstance(event, dict):
                continue
            self._handle_event(event)

    def _handle_event(self, msg: dict) -> None:
        """Route a single event dict to the appropriate handler."""
        event_type = msg.get("event_type") or msg.get("type", "")
        asset_id = msg.get("asset_id") or msg.get("market", "")

        if event_type == "book":
            self._handle_book_snapshot(msg, asset_id)
        elif event_type == "price_change":
            self._handle_price_change(msg, asset_id)
        # Silently ignore unknown event types (e.g. heartbeats)

        if self._on_update is not None and asset_id:
            try:
                self._on_update(asset_id, event_type)
            except Exception as exc:
                self._log.warning(
                    "on_update_callback_error",
                    error=str(exc),
                    asset_id=asset_id,
                    event_type=event_type,
                )

    def _handle_book_snapshot(self, msg: dict, asset_id: str) -> None:
        """Apply a full book snapshot to the OrderBookManager.

        Handles both old format (buys/sells) and new format (bids/asks).
        """
        # Support both field name conventions
        bids = msg.get("bids") or msg.get("buys", [])
        asks = msg.get("asks") or msg.get("sells", [])

        if not asset_id:
            self._log.warning("snapshot_missing_asset_id")
            return

        book = self._book_manager.ensure_book(asset_id)
        book.apply_snapshot(bids, asks)

        # Update metadata if present
        if "timestamp" in msg:
            book.last_timestamp_ms = int(msg["timestamp"])
        if "hash" in msg:
            book.last_hash = str(msg["hash"])

        self._log.info(
            "snapshot_applied",
            asset_id=asset_id,
            bid_levels=len(bids),
            ask_levels=len(asks),
        )

    def _handle_price_change(self, msg: dict, asset_id: str) -> None:
        """Apply incremental price changes to the OrderBookManager.

        Accepts changes from either the "changes" or "price_changes" key.
        """
        changes = msg.get("changes") or msg.get("price_changes", [])

        if not changes:
            return

        # Group changes by asset_id so each book only gets its own deltas.
        # Individual change entries may contain their own asset_id that
        # overrides the top-level one.
        changes_by_asset: dict[str, list[dict]] = {}
        for change in changes:
            change_asset = change.get("asset_id") or asset_id
            if not change_asset:
                continue
            changes_by_asset.setdefault(change_asset, []).append(change)

        for token_id, token_changes in changes_by_asset.items():
            book = self._book_manager.ensure_book(token_id)
            book.apply_delta(token_changes)

        # Update metadata if present
        if asset_id:
            book_state = self._book_manager.ensure_book(asset_id)
            if "timestamp" in msg:
                book_state.last_timestamp_ms = int(msg["timestamp"])
            if "hash" in msg:
                book_state.last_hash = str(msg["hash"])

        # Periodic delta logging for observability
        self._delta_count += len(changes)
        if self._delta_count >= self._delta_log_interval:
            self._log.info(
                "deltas_applied",
                total_deltas=self._delta_count,
                assets_affected=list(changes_by_asset.keys()),
            )
            self._delta_count = 0
