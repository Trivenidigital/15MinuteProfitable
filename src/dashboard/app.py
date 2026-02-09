"""FastAPI dashboard application for the BTC15MinuteBot Polymarket trading bot.

Provides REST endpoints, WebSocket live updates, and an HTML dashboard
for monitoring positions, P&L, orderbooks, trades, and risk status.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import SecretStr

import uvicorn
from fastapi import APIRouter, FastAPI, HTTPException, Query, Request, WebSocket, status
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.websockets import WebSocketDisconnect

from src.config import Settings
from src.core.state import StateManager
from src.data.market_manager import MarketManager
from src.data.orderbook import OrderBookManager
from src.data.spot_buffer import SpotBuffer
from src.data.trade_db import TradeDatabase
from src.monitoring.logger import get_logger
from src.monitoring.metrics import MetricsCollector
from src.risk.manager import RiskManager

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Template setup
# ---------------------------------------------------------------------------

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# Module-level start time; set when configure_dashboard is called
_start_time: float = 0.0

# Fields that must never be exposed via /api/config
_SECRET_FIELD_NAMES = frozenset({
    "private_key",
    "telegram_bot_token",
    "discord_webhook_url",
    "funder",
})


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------




def create_app() -> FastAPI:
    """Create the FastAPI application instance with all route handlers."""
    global _app_ref

    app = FastAPI(
        title="BTC15MinuteBot Dashboard",
        description="Polymarket 15-minute crypto trading bot monitoring dashboard",
        version="1.0.0",
    )
    _app_ref = app

    # Router for all HTTP endpoints — monitoring dashboard is read-only,
    # no authentication required.
    router = APIRouter()

    # ------------------------------------------------------------------
    # HTML dashboard
    # ------------------------------------------------------------------

    @router.get("/")
    async def root(request: Request):  # type: ignore[no-untyped-def]
        """Serve the main HTML dashboard."""
        return templates.TemplateResponse(request, "dashboard.html")

    # ------------------------------------------------------------------
    # /api/status
    # ------------------------------------------------------------------

    @router.get("/api/status")
    async def api_status() -> JSONResponse:
        """Return bot uptime and mode information."""
        settings: Settings = app.state.settings
        now = time.time()
        return JSONResponse({
            "uptime": now - _start_time,
            "dry_run": settings.dry_run,
            "dashboard_enabled": settings.dashboard_enabled,
            "start_time": datetime.fromtimestamp(_start_time, tz=timezone.utc).isoformat(),
        })

    # ------------------------------------------------------------------
    # /api/positions
    # ------------------------------------------------------------------

    @router.get("/api/positions")
    async def api_positions() -> JSONResponse:
        """Return all open positions."""
        state: StateManager = app.state.state_manager
        positions = state.get_all_positions()
        result = []
        for pos in positions:
            result.append({
                "condition_id": pos.market.condition_id,
                "slug": pos.market.slug,
                "asset": pos.market.asset,
                "strategy": pos.strategy.value,
                "yes_shares": pos.yes_shares,
                "no_shares": pos.no_shares,
                "total_investment": pos.total_investment,
                "is_hedged": pos.is_hedged,
                "net_directional_exposure": pos.net_directional_exposure,
                "opened_at": pos.opened_at.isoformat() if pos.opened_at else None,
            })
        return JSONResponse(result)

    # ------------------------------------------------------------------
    # /api/pnl
    # ------------------------------------------------------------------

    @router.get("/api/pnl")
    async def api_pnl() -> JSONResponse:
        """Return today's P&L and historical daily snapshots."""
        state: StateManager = app.state.state_manager
        pnl = state.daily_pnl()
        pnl_dict = asdict(pnl)

        # Attach historical snapshots from SQLite if available
        trade_db: Optional[TradeDatabase] = getattr(app.state, "trade_db", None)
        snapshots: list[dict] = []
        if trade_db is not None:
            raw_snapshots = trade_db.get_daily_snapshots(limit=30)
            for snap in raw_snapshots:
                snapshots.append({
                    "date": snap.date,
                    "trades": snap.trades,
                    "gross_profit": snap.gross_profit,
                    "net_profit": snap.net_profit,
                    "total_fees": snap.total_fees,
                    "win_count": snap.win_count,
                    "loss_count": snap.loss_count,
                    "max_drawdown": snap.max_drawdown,
                    "sim_balance": snap.sim_balance,
                    "opportunities_seen": snap.opportunities_seen,
                    "opportunities_taken": snap.opportunities_taken,
                })

        return JSONResponse({
            "today": pnl_dict,
            "lifetime_net_profit": state.lifetime_net_profit,
            "history": snapshots,
        })

    # ------------------------------------------------------------------
    # /api/orderbooks
    # ------------------------------------------------------------------

    @router.get("/api/orderbooks")
    async def api_orderbooks() -> JSONResponse:
        """Return top 5 bid/ask levels for each active token."""
        market_mgr: MarketManager = app.state.market_manager
        book_mgr: OrderBookManager = app.state.book_manager
        token_ids = market_mgr.active_token_ids

        result: dict = {}
        for token_id in token_ids:
            book = book_mgr.get_book(token_id)
            if book is None:
                continue
            result[token_id] = {
                "bids": [
                    {"price": level.price, "size": level.size}
                    for level in book.bids[:5]
                ],
                "asks": [
                    {"price": level.price, "size": level.size}
                    for level in book.asks[:5]
                ],
                "best_bid": book.best_bid,
                "best_ask": book.best_ask,
                "spread": book.spread,
            }
        return JSONResponse(result)

    # ------------------------------------------------------------------
    # /api/trades
    # ------------------------------------------------------------------

    @router.get("/api/trades")
    async def api_trades(
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        strategy: Optional[str] = Query(default=None),
    ) -> JSONResponse:
        """Return paginated trade history from SQLite."""
        trade_db: Optional[TradeDatabase] = getattr(app.state, "trade_db", None)
        if trade_db is None:
            return JSONResponse({
                "trades": [],
                "total": 0,
                "limit": limit,
                "offset": offset,
            })

        trades = trade_db.get_trades(limit=limit, offset=offset, strategy=strategy)
        total = trade_db.get_trade_count(strategy=strategy)

        trade_list = []
        for t in trades:
            trade_list.append({
                "id": t.id,
                "timestamp": t.timestamp,
                "condition_id": t.condition_id,
                "market_slug": t.market_slug,
                "asset": t.asset,
                "strategy": t.strategy,
                "side": t.side,
                "token_side": t.token_side,
                "price": t.price,
                "size": t.size,
                "cost": t.cost,
                "order_type": t.order_type,
                "order_id": t.order_id,
                "status": t.status,
                "fees": t.fees,
                "expected_profit": t.expected_profit,
            })

        return JSONResponse({
            "trades": trade_list,
            "total": total,
            "limit": limit,
            "offset": offset,
        })

    # ------------------------------------------------------------------
    # /api/trade-results
    # ------------------------------------------------------------------

    @router.get("/api/trade-results")
    async def api_trade_results(
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> JSONResponse:
        """Return paginated trade resolution results from SQLite."""
        trade_db: Optional[TradeDatabase] = getattr(app.state, "trade_db", None)
        if trade_db is None:
            return JSONResponse({
                "results": [],
                "total": 0,
                "limit": limit,
                "offset": offset,
            })

        results = trade_db.get_trade_results(limit=limit, offset=offset)
        total = trade_db.get_trade_result_count()

        result_list = []
        for r in results:
            result_list.append({
                "id": r.id,
                "timestamp": r.timestamp,
                "condition_id": r.condition_id,
                "market_slug": r.market_slug,
                "asset": r.asset,
                "strategy": r.strategy,
                "was_hedged": r.was_hedged,
                "yes_shares": r.yes_shares,
                "no_shares": r.no_shares,
                "investment": r.investment,
                "gross_payout": r.gross_payout,
                "net_profit": r.net_profit,
                "outcome": r.outcome,
            })

        return JSONResponse({
            "results": result_list,
            "total": total,
            "limit": limit,
            "offset": offset,
        })

    # ------------------------------------------------------------------
    # /api/performance-summary
    # ------------------------------------------------------------------

    @router.get("/api/performance-summary")
    async def api_performance_summary() -> JSONResponse:
        """Return strategy and asset performance across time windows."""
        trade_db: Optional[TradeDatabase] = getattr(app.state, "trade_db", None)
        if trade_db is None:
            return JSONResponse({"strategies": {}, "assets": {}})

        results = trade_db.get_performance_summary()
        now = time.time()
        windows = {
            "1h": now - 3600,
            "6h": now - 6 * 3600,
            "24h": now - 24 * 3600,
            "all": 0.0,
        }

        def compute_metrics(
            trades: list,
        ) -> dict[str, int | float]:
            count = len(trades)
            if count == 0:
                return {"trades": 0, "wins": 0, "win_rate": 0.0, "roi": 0.0, "pnl": 0.0}
            wins = sum(1 for t in trades if t.net_profit > 0)
            total_investment = sum(t.investment for t in trades)
            total_pnl = sum(t.net_profit for t in trades)
            roi = (total_pnl / total_investment * 100) if total_investment > 0 else 0.0
            return {
                "trades": count,
                "wins": wins,
                "win_rate": round(wins / count * 100, 1),
                "roi": round(roi, 2),
                "pnl": round(total_pnl, 4),
            }

        # Group by strategy
        strategies: dict[str, dict] = {}
        strategy_names = sorted({r.strategy for r in results})
        for name in strategy_names:
            strategy_trades = [r for r in results if r.strategy == name]
            strategies[name] = {}
            for window_key, cutoff in windows.items():
                filtered = [t for t in strategy_trades if t.timestamp >= cutoff]
                strategies[name][window_key] = compute_metrics(filtered)

        # Group by asset
        assets: dict[str, dict] = {}
        asset_names = sorted({r.asset for r in results})
        for name in asset_names:
            asset_trades = [r for r in results if r.asset == name]
            assets[name] = {}
            for window_key, cutoff in windows.items():
                filtered = [t for t in asset_trades if t.timestamp >= cutoff]
                assets[name][window_key] = compute_metrics(filtered)

        return JSONResponse({"strategies": strategies, "assets": assets})

    # ------------------------------------------------------------------
    # /api/strategies
    # ------------------------------------------------------------------

    @router.get("/api/strategies")
    async def api_strategies() -> JSONResponse:
        """Return strategy names and enabled/disabled flags."""
        settings: Settings = app.state.settings
        strategies = [
            {"name": "arbitrage", "enabled": settings.enable_arbitrage},
            {"name": "maker_arbitrage", "enabled": settings.enable_maker_arbitrage},
            {"name": "asymmetric", "enabled": settings.enable_asymmetric},
            {"name": "price_lag", "enabled": settings.enable_price_lag},
            {"name": "multi_market", "enabled": settings.enable_multi_market},
        ]

        # Include strategy breakdown from SQLite if available
        trade_db: Optional[TradeDatabase] = getattr(app.state, "trade_db", None)
        breakdown: dict = {}
        if trade_db is not None:
            breakdown = trade_db.get_strategy_breakdown()

        return JSONResponse({
            "strategies": strategies,
            "breakdown": breakdown,
            "parallel_mode": settings.enable_parallel_strategies,
        })

    # ------------------------------------------------------------------
    # /api/risk
    # ------------------------------------------------------------------

    @router.get("/api/risk")
    async def api_risk() -> JSONResponse:
        """Return circuit breaker status, exposure limits and current values."""
        risk_mgr: RiskManager = app.state.risk_manager
        state: StateManager = app.state.state_manager
        settings: Settings = app.state.settings

        pnl = state.daily_pnl()

        return JSONResponse({
            "circuit_breaker_active": risk_mgr.is_circuit_breaker_active(),
            "circuit_breaker_reason": risk_mgr._circuit_breaker_reason,
            "total_exposure": state.total_exposure(),
            "max_total_position": settings.max_total_position,
            "unhedged_exposure": state.total_unhedged_exposure(),
            "max_unhedged_exposure": settings.max_unhedged_exposure,
            "max_position_per_market": settings.max_position_per_market,
            "daily_loss": pnl.net_profit,
            "max_daily_loss": settings.max_daily_loss,
        })

    # ------------------------------------------------------------------
    # /api/markets
    # ------------------------------------------------------------------

    @router.get("/api/markets")
    async def api_markets() -> JSONResponse:
        """Return active markets with expiry countdown."""
        market_mgr: MarketManager = app.state.market_manager
        markets = market_mgr.active_markets
        now = datetime.now(tz=timezone.utc)

        result = []
        for m in markets:
            # Handle both tz-aware and naive end_time
            end = m.end_time
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            expiry_seconds = max(0.0, (end - now).total_seconds())
            result.append({
                "condition_id": m.condition_id,
                "slug": m.slug,
                "question": m.question,
                "asset": m.asset,
                "yes_token_id": m.yes_token_id,
                "no_token_id": m.no_token_id,
                "start_time": m.start_time.isoformat(),
                "end_time": m.end_time.isoformat(),
                "expiry_seconds": expiry_seconds,
                "neg_risk": m.neg_risk,
            })
        return JSONResponse(result)

    # ------------------------------------------------------------------
    # /api/config
    # ------------------------------------------------------------------

    @router.get("/api/config")
    async def api_config() -> JSONResponse:
        """Return safe configuration fields (no secrets)."""
        settings: Settings = app.state.settings
        safe_config: dict = {}

        for field_name, field_info in Settings.model_fields.items():
            # Skip known secret fields
            if field_name in _SECRET_FIELD_NAMES:
                continue
            value = getattr(settings, field_name)
            # Skip SecretStr values regardless of field name
            if isinstance(value, SecretStr):
                continue
            # Convert enums and other non-serializable types
            if hasattr(value, "value"):
                value = value.value
            safe_config[field_name] = value

        return JSONResponse(safe_config)

    # ------------------------------------------------------------------
    # /api/metrics
    # ------------------------------------------------------------------

    @router.get("/api/metrics")
    async def api_metrics() -> JSONResponse:
        """Return MetricsCollector dashboard dict."""
        metrics_collector: MetricsCollector = app.state.metrics
        state: StateManager = app.state.state_manager
        pnl = state.daily_pnl()
        dashboard = metrics_collector.compute_dashboard(pnl, state.sim_balance)
        return JSONResponse(dashboard)

    # ------------------------------------------------------------------
    # /api/equity-curve
    # ------------------------------------------------------------------

    @router.get("/api/equity-curve")
    async def api_equity_curve() -> JSONResponse:
        """Return time-series equity data from SQLite."""
        trade_db: Optional[TradeDatabase] = getattr(app.state, "trade_db", None)
        if trade_db is None:
            return JSONResponse([])
        curve = trade_db.get_equity_curve(limit=90)
        return JSONResponse(curve)

    # ------------------------------------------------------------------
    # /api/spot-prices
    # ------------------------------------------------------------------

    @router.get("/api/spot-prices")
    async def api_spot_prices() -> JSONResponse:
        """Return current spot prices from SpotBuffer for all tracked symbols."""
        spot: Optional[SpotBuffer] = getattr(app.state, "spot_buffer", None)
        if spot is None:
            return JSONResponse({})

        prices: dict[str, float | None] = {}
        for symbol in spot.symbols:
            prices[symbol] = spot.get_price(symbol)
        return JSONResponse(prices)

    # Register all HTTP routes
    app.include_router(router)

    # ------------------------------------------------------------------
    # WebSocket /ws (no auth dependency — WS doesn't support HTTP Basic)
    # ------------------------------------------------------------------

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket) -> None:
        """Push live updates every 3 seconds."""
        await ws.accept()
        try:
            while True:
                state: StateManager = app.state.state_manager
                risk_mgr: RiskManager = app.state.risk_manager
                pnl = state.daily_pnl()

                payload = {
                    "positions_count": len(state.get_all_positions()),
                    "total_exposure": state.total_exposure(),
                    "daily_pnl_net": pnl.net_profit,
                    "lifetime_pnl_net": state.lifetime_net_profit,
                    "sim_balance": state.sim_balance,
                    "circuit_breaker_active": risk_mgr.is_circuit_breaker_active(),
                    "uptime": time.time() - _start_time,
                }
                await ws.send_json(payload)
                await asyncio.sleep(3)
        except WebSocketDisconnect:
            logger.debug("websocket_disconnected")
        except Exception as exc:
            logger.warning("websocket_error", error=str(exc))

    return app


# ---------------------------------------------------------------------------
# Configure app with component references
# ---------------------------------------------------------------------------


def configure_dashboard(
    app: FastAPI,
    state_manager: StateManager,
    book_manager: OrderBookManager,
    market_manager: MarketManager,
    risk_manager: RiskManager,
    metrics: MetricsCollector,
    settings: Settings,
    trade_db: TradeDatabase | None = None,
    spot_buffer: SpotBuffer | None = None,
) -> None:
    """Store all component references on app.state for endpoint access."""
    global _start_time
    _start_time = time.time()

    app.state.state_manager = state_manager
    app.state.book_manager = book_manager
    app.state.market_manager = market_manager
    app.state.risk_manager = risk_manager
    app.state.metrics = metrics
    app.state.settings = settings
    app.state.trade_db = trade_db
    app.state.spot_buffer = spot_buffer

    logger.info(
        "dashboard_configured",
        dry_run=settings.dry_run,
        trade_db="enabled" if trade_db is not None else "disabled",
        spot_buffer="enabled" if spot_buffer is not None else "disabled",
    )


# ---------------------------------------------------------------------------
# Non-blocking server startup
# ---------------------------------------------------------------------------


async def start_dashboard(app: FastAPI, settings: Settings) -> uvicorn.Server:
    """Create a uvicorn Server for non-blocking serving in the existing event loop.

    Usage in main.py::

        server = await start_dashboard(app, settings)
        asyncio.create_task(server.serve())
        # ... later ...
        server.should_exit = True

    Parameters:
        app: The already-configured FastAPI application instance.
        settings: Bot settings for host/port configuration.

    Returns:
        The uvicorn.Server object. Call ``await server.serve()`` to start,
        and set ``server.should_exit = True`` to gracefully shut down.
    """
    config = uvicorn.Config(
        app=app,
        host=settings.dashboard_host,
        port=settings.dashboard_port,
        log_level="warning",
        loop="asyncio",
    )
    server = uvicorn.Server(config)
    logger.info(
        "dashboard_server_created",
        host=settings.dashboard_host,
        port=settings.dashboard_port,
    )
    return server
