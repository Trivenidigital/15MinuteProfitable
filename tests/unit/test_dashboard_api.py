"""Tests for the FastAPI dashboard endpoints."""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.testclient import TestClient

# Ensure BOT_PRIVATE_KEY is set before importing Settings
os.environ.setdefault("BOT_PRIVATE_KEY", "0x" + "a" * 64)

from src.config import Settings
from src.core.models import (
    DailyPnL,
    Market,
    OrderBook,
    OrderBookLevel,
    Position,
    StrategyType,
)
from src.core.state import StateManager
from src.dashboard.app import configure_dashboard, create_app
from src.data.orderbook import OrderBookManager
from src.data.spot_buffer import SpotBuffer, SpotPriceUpdate
from src.data.trade_db import DailySnapshot, TradeDatabase, TradeRecord
from src.monitoring.metrics import MetricsCollector


# ---------------------------------------------------------------------------
# Mock classes
# ---------------------------------------------------------------------------


class MockMarketManager:
    """Minimal MarketManager stand-in for tests."""

    def __init__(self) -> None:
        self._markets: list[Market] = []

    @property
    def active_markets(self) -> list[Market]:
        return self._markets

    @property
    def active_token_ids(self) -> list[str]:
        tokens: list[str] = []
        for m in self._markets:
            tokens.append(m.yes_token_id)
            tokens.append(m.no_token_id)
        return tokens


class MockRiskManager:
    """Minimal RiskManager stand-in for tests."""

    def __init__(self) -> None:
        self._circuit_breaker_active = False
        self._circuit_breaker_reason = ""
        self._settings = Settings(
            private_key="0x" + "a" * 64,
            dry_run=True,
        )

    def is_circuit_breaker_active(self) -> bool:
        return self._circuit_breaker_active


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_market(
    condition_id: str = "cond_abc123",
    slug: str = "btc-15m-up",
    asset: str = "BTC",
) -> Market:
    now = datetime.now(tz=timezone.utc)
    return Market(
        condition_id=condition_id,
        slug=slug,
        question="Will BTC go up?",
        yes_token_id="yes_tok_1",
        no_token_id="no_tok_1",
        start_time=now,
        end_time=now + timedelta(minutes=15),
        asset=asset,
        neg_risk=True,
    )


def _make_settings() -> Settings:
    return Settings(
        private_key="0x" + "a" * 64,
        dry_run=True,
        dashboard_enabled=True,
        sim_balance=1000.0,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app():
    application = create_app()
    settings = _make_settings()
    state_mgr = StateManager(settings)
    book_mgr = OrderBookManager()
    market_mgr = MockMarketManager()
    risk_mgr = MockRiskManager()
    metrics = MetricsCollector()
    trade_db = TradeDatabase(":memory:")
    spot_buffer = SpotBuffer(window_seconds=60)

    configure_dashboard(
        app=application,
        state_manager=state_mgr,
        book_manager=book_mgr,
        market_manager=market_mgr,
        risk_manager=risk_mgr,
        metrics=metrics,
        settings=settings,
        trade_db=trade_db,
        spot_buffer=spot_buffer,
    )
    return application


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_root_returns_html(client: AsyncClient) -> None:
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "15MinuteProfitable Dashboard" in resp.text


@pytest.mark.asyncio
async def test_status_endpoint(client: AsyncClient) -> None:
    resp = await client.get("/api/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "uptime" in data
    assert data["dry_run"] is True
    assert "start_time" in data


@pytest.mark.asyncio
async def test_positions_empty(client: AsyncClient) -> None:
    resp = await client.get("/api/positions")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_positions_with_data(app, client: AsyncClient) -> None:
    state_mgr: StateManager = app.state.state_manager
    market = _make_market()
    pos = Position(
        market=market,
        yes_shares=100.0,
        no_shares=50.0,
        yes_cost_basis=45.0,
        no_cost_basis=25.0,
        strategy=StrategyType.ARBITRAGE,
        opened_at=datetime.now(tz=timezone.utc),
    )
    state_mgr.add_position(pos)

    resp = await client.get("/api/positions")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["condition_id"] == "cond_abc123"
    assert data[0]["yes_shares"] == 100.0
    assert data[0]["no_shares"] == 50.0
    assert data[0]["is_hedged"] is True
    assert data[0]["strategy"] == "arbitrage"


@pytest.mark.asyncio
async def test_pnl_endpoint(client: AsyncClient) -> None:
    resp = await client.get("/api/pnl")
    assert resp.status_code == 200
    data = resp.json()
    assert "today" in data
    assert "history" in data
    assert "net_profit" in data["today"]


@pytest.mark.asyncio
async def test_orderbooks_empty(client: AsyncClient) -> None:
    resp = await client.get("/api/orderbooks")
    assert resp.status_code == 200
    assert resp.json() == {}


@pytest.mark.asyncio
async def test_trades_empty(client: AsyncClient) -> None:
    resp = await client.get("/api/trades")
    assert resp.status_code == 200
    data = resp.json()
    assert data["trades"] == []
    assert data["total"] == 0


@pytest.mark.asyncio
async def test_trades_with_data(app, client: AsyncClient) -> None:
    trade_db: TradeDatabase = app.state.trade_db
    trade_db.save_trade(TradeRecord(
        timestamp=time.time(),
        condition_id="cond_1",
        market_slug="btc-up",
        asset="BTC",
        strategy="arbitrage",
        side="BUY",
        token_side="YES",
        price=0.55,
        size=100.0,
        cost=55.0,
        order_type="FOK",
        order_id="ord_1",
        status="filled",
    ))

    resp = await client.get("/api/trades")
    data = resp.json()
    assert len(data["trades"]) == 1
    assert data["total"] == 1
    assert data["trades"][0]["strategy"] == "arbitrage"


@pytest.mark.asyncio
async def test_trades_pagination(app, client: AsyncClient) -> None:
    trade_db: TradeDatabase = app.state.trade_db
    for i in range(5):
        trade_db.save_trade(TradeRecord(
            timestamp=time.time() + i,
            condition_id=f"cond_{i}",
            market_slug="btc-up",
            asset="BTC",
            strategy="arbitrage",
            side="BUY",
            token_side="YES",
            price=0.55,
            size=10.0,
            cost=5.5,
            status="filled",
        ))

    resp = await client.get("/api/trades?limit=2&offset=0")
    data = resp.json()
    assert len(data["trades"]) == 2
    assert data["total"] == 5
    assert data["limit"] == 2
    assert data["offset"] == 0

    resp2 = await client.get("/api/trades?limit=2&offset=2")
    data2 = resp2.json()
    assert len(data2["trades"]) == 2
    assert data2["offset"] == 2


@pytest.mark.asyncio
async def test_trades_strategy_filter(app, client: AsyncClient) -> None:
    trade_db: TradeDatabase = app.state.trade_db
    trade_db.save_trade(TradeRecord(
        timestamp=time.time(),
        condition_id="cond_a",
        strategy="arbitrage",
        side="BUY",
        token_side="YES",
        price=0.5,
        size=10.0,
        cost=5.0,
        status="filled",
    ))
    trade_db.save_trade(TradeRecord(
        timestamp=time.time(),
        condition_id="cond_b",
        strategy="price_lag",
        side="BUY",
        token_side="NO",
        price=0.4,
        size=20.0,
        cost=8.0,
        status="filled",
    ))

    resp = await client.get("/api/trades?strategy=arbitrage")
    data = resp.json()
    assert data["total"] == 1
    assert data["trades"][0]["strategy"] == "arbitrage"

    resp2 = await client.get("/api/trades?strategy=price_lag")
    data2 = resp2.json()
    assert data2["total"] == 1
    assert data2["trades"][0]["strategy"] == "price_lag"


@pytest.mark.asyncio
async def test_strategies_endpoint(client: AsyncClient) -> None:
    resp = await client.get("/api/strategies")
    assert resp.status_code == 200
    data = resp.json()
    assert "strategies" in data
    names = [s["name"] for s in data["strategies"]]
    assert "arbitrage" in names
    assert "asymmetric" in names


@pytest.mark.asyncio
async def test_risk_endpoint(client: AsyncClient) -> None:
    resp = await client.get("/api/risk")
    assert resp.status_code == 200
    data = resp.json()
    assert "circuit_breaker_active" in data
    assert data["circuit_breaker_active"] is False
    assert "total_exposure" in data
    assert "max_total_position" in data
    assert "daily_loss" in data


@pytest.mark.asyncio
async def test_markets_empty(client: AsyncClient) -> None:
    resp = await client.get("/api/markets")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_config_hides_secrets(client: AsyncClient) -> None:
    resp = await client.get("/api/config")
    assert resp.status_code == 200
    data = resp.json()
    assert "private_key" not in data
    assert "telegram_bot_token" not in data
    assert "discord_webhook_url" not in data
    assert "funder" not in data
    # Ensure no raw secret values leaked
    text = resp.text
    assert "a" * 64 not in text


@pytest.mark.asyncio
async def test_config_returns_safe_fields(client: AsyncClient) -> None:
    resp = await client.get("/api/config")
    assert resp.status_code == 200
    data = resp.json()
    assert data["dry_run"] is True
    assert "order_size" in data
    assert "max_daily_loss" in data
    assert "dashboard_enabled" in data


@pytest.mark.asyncio
async def test_metrics_endpoint(client: AsyncClient) -> None:
    resp = await client.get("/api/metrics")
    assert resp.status_code == 200
    data = resp.json()
    assert "win_rate" in data
    assert "net_profit" in data
    assert "sim_balance" in data
    assert "trades" in data


@pytest.mark.asyncio
async def test_equity_curve_empty(client: AsyncClient) -> None:
    resp = await client.get("/api/equity-curve")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_equity_curve_with_data(app, client: AsyncClient) -> None:
    trade_db: TradeDatabase = app.state.trade_db
    trade_db.save_daily_snapshot(DailySnapshot(
        date="2025-01-01",
        trades=10,
        gross_profit=5.0,
        net_profit=3.0,
        total_fees=2.0,
        sim_balance=1003.0,
    ))
    trade_db.save_daily_snapshot(DailySnapshot(
        date="2025-01-02",
        trades=15,
        gross_profit=8.0,
        net_profit=6.0,
        total_fees=2.0,
        sim_balance=1009.0,
    ))

    resp = await client.get("/api/equity-curve")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    assert data[0]["date"] == "2025-01-01"
    assert data[0]["equity"] == 1003.0
    assert data[1]["pnl"] == 6.0


@pytest.mark.asyncio
async def test_spot_prices_empty(client: AsyncClient) -> None:
    resp = await client.get("/api/spot-prices")
    assert resp.status_code == 200
    assert resp.json() == {}


@pytest.mark.asyncio
async def test_spot_prices_with_data(app, client: AsyncClient) -> None:
    spot: SpotBuffer = app.state.spot_buffer
    spot.add(SpotPriceUpdate(symbol="BTCUSDT", price=97500.0, timestamp=time.time()))
    spot.add(SpotPriceUpdate(symbol="ETHUSDT", price=3200.0, timestamp=time.time()))

    resp = await client.get("/api/spot-prices")
    assert resp.status_code == 200
    data = resp.json()
    assert data["BTCUSDT"] == 97500.0
    assert data["ETHUSDT"] == 3200.0


def test_websocket_connect(app) -> None:
    """Test WebSocket connection using Starlette's synchronous TestClient."""
    with TestClient(app) as tc:
        with tc.websocket_connect("/ws") as websocket:
            data = websocket.receive_json()
            assert "positions_count" in data
            assert "total_exposure" in data
            assert "daily_pnl_net" in data
            assert "sim_balance" in data
            assert "circuit_breaker_active" in data
            assert "uptime" in data


@pytest.mark.asyncio
async def test_strategy_breakdown(app, client: AsyncClient) -> None:
    trade_db: TradeDatabase = app.state.trade_db
    for i in range(3):
        trade_db.save_trade(TradeRecord(
            timestamp=time.time() + i,
            condition_id=f"cond_{i}",
            strategy="arbitrage",
            side="BUY",
            token_side="YES",
            price=0.5,
            size=10.0,
            cost=5.0,
            status="filled",
        ))
    trade_db.save_trade(TradeRecord(
        timestamp=time.time(),
        condition_id="cond_lag",
        strategy="price_lag",
        side="BUY",
        token_side="NO",
        price=0.4,
        size=20.0,
        cost=8.0,
        status="filled",
    ))

    resp = await client.get("/api/strategies")
    data = resp.json()
    breakdown = data["breakdown"]
    assert "arbitrage" in breakdown
    assert breakdown["arbitrage"]["count"] == 3
    assert "price_lag" in breakdown
    assert breakdown["price_lag"]["count"] == 1


@pytest.mark.asyncio
async def test_trades_count(app, client: AsyncClient) -> None:
    trade_db: TradeDatabase = app.state.trade_db
    for i in range(7):
        trade_db.save_trade(TradeRecord(
            timestamp=time.time() + i,
            condition_id=f"cond_{i}",
            strategy="arbitrage",
            side="BUY",
            token_side="YES",
            price=0.5,
            size=10.0,
            cost=5.0,
            status="filled",
        ))

    resp = await client.get("/api/trades?limit=3")
    data = resp.json()
    assert data["total"] == 7
    assert len(data["trades"]) == 3
