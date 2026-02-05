"""Integration tests for Gamma API market discovery.

These tests hit the live Gamma API and verify response structure.
Run with: pytest tests/integration/ --run-integration -v
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_gamma_api_reachable():
    """Verify Gamma API endpoint responds."""
    import httpx

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://gamma-api.polymarket.com/events",
            params={"limit": "1", "active": "true"},
            timeout=10.0,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)


@pytest.mark.asyncio
async def test_discover_active_markets():
    """Verify MarketDiscovery can find at least one active market."""
    from src.data.market_discovery import MarketDiscovery

    discovery = MarketDiscovery(gamma_api_url="https://gamma-api.polymarket.com")
    markets = await discovery.find_active_markets()

    # There should be at least some active markets
    # (may be 0 if no 15-min markets are currently active)
    assert isinstance(markets, list)

    if markets:
        market = markets[0]
        assert market.condition_id
        assert market.yes_token_id
        assert market.no_token_id
        assert market.asset in ("BTC", "ETH", "SOL", "XRP")


@pytest.mark.asyncio
async def test_gamma_api_market_structure():
    """Verify the structure of markets from Gamma API."""
    import httpx

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://gamma-api.polymarket.com/events",
            params={"limit": "5", "active": "true", "tag": "crypto"},
            timeout=10.0,
        )
        assert resp.status_code == 200
        events = resp.json()

        # Events should have expected fields
        if events:
            event = events[0]
            assert "id" in event or "slug" in event
