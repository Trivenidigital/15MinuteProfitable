"""WebSocket client for Binance spot price feeds.

Connects to Binance's combined stream endpoint and subscribes to trade
streams for configured symbols (e.g. btcusdt@trade, ethusdt@trade).
Feeds updates into a SpotBuffer for downstream consumption.
"""

from __future__ import annotations

import asyncio
import json
import math

import websockets

from src.data.spot_buffer import SpotBuffer, SpotPriceUpdate
from src.monitoring.logger import get_logger


# Binance combined stream URL format:
# wss://stream.binance.com:9443/stream?streams=btcusdt@trade/ethusdt@trade


class BinanceWebSocket:
    """WebSocket client for Binance spot trade feeds.

    Connects to the Binance combined stream and feeds trade
    prices into a SpotBuffer.
    """

    def __init__(
        self,
        ws_url: str,
        symbols: list[str],
        spot_buffer: SpotBuffer,
    ) -> None:
        """
        Parameters
        ----------
        ws_url : str
            Base WebSocket URL (e.g. "wss://stream.binance.com:9443").
        symbols : list[str]
            Trading pairs to subscribe (e.g. ["BTCUSDT", "ETHUSDT", "SOLUSDT"]).
        spot_buffer : SpotBuffer
            Buffer to feed price updates into.
        """
        self._base_url = ws_url
        self._symbols = [s.upper() for s in symbols]
        self._spot_buffer = spot_buffer
        self._running = False
        self._ws = None
        self._log = get_logger("binance_ws")
        self._update_count: int = 0
        self._update_log_interval: int = 100

    def _build_stream_url(self) -> str:
        """Build the combined stream URL for all symbols.

        Format: wss://stream.binance.com:9443/stream?streams=btcusdt@trade/ethusdt@trade
        """
        streams = "/".join(f"{s.lower()}@trade" for s in self._symbols)
        # Handle URL with or without trailing /ws
        base = self._base_url.rstrip("/")
        if base.endswith("/ws"):
            base = base[:-3]
        return f"{base}/stream?streams={streams}"

    async def run(self) -> None:
        """Main loop: connect, receive trade messages, feed to buffer.

        Auto-reconnects with exponential backoff.
        """
        self._running = True
        backoff = 1.0
        url = self._build_stream_url()

        while self._running:
            try:
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=20,
                    open_timeout=10,
                    close_timeout=5,
                ) as ws:
                    self._ws = ws
                    self._log.info(
                        "binance_connected",
                        symbols=self._symbols,
                        url=url[:60],
                    )
                    backoff = 1.0

                    async for raw in ws:
                        self._process_message(raw)

            except (websockets.ConnectionClosed, ConnectionError, OSError) as exc:
                self._ws = None
                if not self._running:
                    break
                self._log.warning(
                    "binance_disconnected",
                    error=str(exc),
                    reconnect_in=backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

            except Exception as exc:
                self._ws = None
                self._log.error("binance_unexpected_error", error=str(exc))
                if not self._running:
                    break
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

        self._log.info("binance_run_loop_exited")

    async def stop(self) -> None:
        """Signal the run loop to stop and close the active connection."""
        self._running = False
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass

    def _process_message(self, raw: str | bytes) -> None:
        """Parse a Binance combined stream message and feed to buffer.

        Combined stream format:
        {"stream": "btcusdt@trade", "data": {"s": "BTCUSDT", "p": "43256.78", "T": 1704067200000, ...}}
        """
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return

        data = msg.get("data")
        if data is None:
            # Direct stream (not combined) -- msg IS the data
            data = msg

        event_type = data.get("e", "")
        if event_type != "trade":
            return

        symbol = data.get("s", "")
        price_str = data.get("p", "")
        trade_time = data.get("T", 0)

        if not symbol or not price_str:
            return

        try:
            price = float(price_str)
            timestamp = trade_time / 1000.0 if trade_time > 1_000_000_000_000 else float(trade_time)
        except (ValueError, TypeError):
            return

        if not math.isfinite(price) or price <= 0:
            return

        update = SpotPriceUpdate(
            symbol=symbol,
            price=price,
            timestamp=timestamp,
        )
        self._spot_buffer.add(update)

        # Periodic logging
        self._update_count += 1
        if self._update_count >= self._update_log_interval:
            self._log.debug(
                "binance_updates",
                total=self._update_count,
                symbol=symbol,
                price=price,
            )
            self._update_count = 0
