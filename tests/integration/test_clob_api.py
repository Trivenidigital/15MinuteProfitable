"""Integration tests for CLOB REST API.

These tests verify the CLOB API is reachable and returns expected structures.
Run with: pytest tests/integration/ --run-integration -v
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_clob_api_reachable():
    """Verify CLOB API endpoint responds."""
    import httpx

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://clob.polymarket.com/time",
            timeout=10.0,
        )
        # Should return server time
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_clob_markets_endpoint():
    """Verify CLOB markets endpoint returns data."""
    import httpx

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://clob.polymarket.com/markets",
            params={"limit": "1"},
            timeout=10.0,
        )
        assert resp.status_code == 200
        data = resp.json()
        # Should be a list or dict with market data
        assert data is not None
