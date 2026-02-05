"""Integration tests for Binance WebSocket connection.

Verifies that we can connect to Binance and receive price data.
Run with: pytest tests/integration/ --run-integration -v
"""

from __future__ import annotations

import asyncio
import json

import pytest
import websockets

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_binance_ws_connect():
    """Verify Binance WebSocket connects and receives a message."""
    uri = "wss://stream.binance.com:9443/ws/btcusdt@trade"

    async with websockets.connect(uri) as ws:
        # Should receive a trade message within 5 seconds
        msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
        assert msg is not None

        data = json.loads(msg)
        assert "e" in data  # event type
        assert data["e"] == "trade"
        assert "p" in data  # price
        assert "q" in data  # quantity


@pytest.mark.asyncio
async def test_binance_multiple_streams():
    """Verify Binance combined stream works for multiple symbols."""
    uri = "wss://stream.binance.com:9443/stream?streams=btcusdt@trade/ethusdt@trade"

    async with websockets.connect(uri) as ws:
        msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
        data = json.loads(msg)
        assert "stream" in data
        assert "data" in data
