"""Async wrapper around Binance Futures REST API for CEX perp hedging.

Uses raw aiohttp requests (no python-binance SDK) to keep dependencies minimal.
Supports both mainnet and testnet endpoints.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode

import aiohttp

from src.monitoring.logger import get_logger

_log = get_logger("binance_futures")

# Binance Futures minimum notional per symbol (USD)
MIN_NOTIONAL: dict[str, float] = {
    "BTCUSDT": 5.0,
    "ETHUSDT": 5.0,
    "SOLUSDT": 5.0,
    "XRPUSDT": 5.0,
}

# Binance Futures quantity precision (decimal places)
QTY_PRECISION: dict[str, int] = {
    "BTCUSDT": 3,
    "ETHUSDT": 3,
    "SOLUSDT": 1,
    "XRPUSDT": 0,
}

MAINNET_URL = "https://fapi.binance.com"
TESTNET_URL = "https://testnet.binancefuture.com"


@dataclass
class HedgeOrder:
    """Result of a hedge order on Binance Futures."""

    symbol: str
    side: str  # "BUY" or "SELL"
    quantity: float
    avg_price: float
    status: str  # "FILLED", "SIMULATED", "FAILED"
    order_id: str = ""
    timestamp: float = field(default_factory=time.time)
    pnl: float = 0.0  # realized P&L (for close orders)


class BinanceFuturesClient:
    """Async client for Binance USDT-M Futures.

    Uses HMAC-SHA256 signed requests via aiohttp.
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        testnet: bool = False,
    ) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._base_url = TESTNET_URL if testnet else MAINNET_URL
        self._testnet = testnet
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"X-MBX-APIKEY": self._api_key},
            )
        return self._session

    def _sign_request(self, params: dict[str, str | int | float]) -> str:
        """Generate HMAC-SHA256 signature for Binance API."""
        query_string = urlencode(params)
        signature = hmac.new(
            self._api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return signature

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, str | int | float] | None = None,
    ) -> dict:
        """Send a signed request to Binance Futures API."""
        if params is None:
            params = {}

        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = 5000
        params["signature"] = self._sign_request(params)

        session = await self._get_session()
        url = f"{self._base_url}{path}"

        if method == "GET":
            async with session.get(url, params=params) as resp:
                data = await resp.json()
                if resp.status != 200:
                    raise RuntimeError(
                        f"Binance API error {resp.status}: {data}"
                    )
                return data
        else:
            async with session.post(url, params=params) as resp:
                data = await resp.json()
                if resp.status != 200:
                    raise RuntimeError(
                        f"Binance API error {resp.status}: {data}"
                    )
                return data

    async def set_leverage(self, symbol: str, leverage: int = 1) -> None:
        """Set leverage for a symbol (default 1x for hedging)."""
        try:
            await self._request("POST", "/fapi/v1/leverage", {
                "symbol": symbol,
                "leverage": leverage,
            })
            _log.info("leverage_set", symbol=symbol, leverage=leverage)
        except RuntimeError as exc:
            # Leverage already set is not fatal
            _log.warning("leverage_set_warning", symbol=symbol, error=str(exc))

    async def open_hedge(
        self,
        symbol: str,
        side: str,
        quantity: float,
        dry_run: bool = False,
    ) -> HedgeOrder:
        """Open a hedge position via market order.

        Args:
            symbol: Binance futures symbol (e.g. "BTCUSDT")
            side: "BUY" (long) or "SELL" (short)
            quantity: Position size in base asset units
            dry_run: If True, simulate without API call

        Returns:
            HedgeOrder with fill details
        """
        # Round quantity to symbol precision
        precision = QTY_PRECISION.get(symbol, 3)
        quantity = round(quantity, precision)

        if quantity <= 0:
            _log.warning("hedge_quantity_zero", symbol=symbol, side=side)
            return HedgeOrder(
                symbol=symbol,
                side=side,
                quantity=0.0,
                avg_price=0.0,
                status="FAILED",
            )

        if dry_run:
            _log.info(
                "hedge_simulated",
                symbol=symbol,
                side=side,
                quantity=quantity,
            )
            return HedgeOrder(
                symbol=symbol,
                side=side,
                quantity=quantity,
                avg_price=0.0,
                status="SIMULATED",
                order_id=f"SIM-{int(time.time() * 1000)}",
            )

        data = await self._request("POST", "/fapi/v1/order", {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": str(quantity),
        })

        avg_price = float(data.get("avgPrice", 0.0))
        if avg_price == 0.0 and data.get("fills"):
            # Calculate VWAP from fills
            total_qty = sum(float(f["qty"]) for f in data["fills"])
            total_cost = sum(
                float(f["qty"]) * float(f["price"]) for f in data["fills"]
            )
            avg_price = total_cost / total_qty if total_qty > 0 else 0.0

        _log.info(
            "hedge_opened",
            symbol=symbol,
            side=side,
            quantity=quantity,
            avg_price=avg_price,
            order_id=str(data.get("orderId", "")),
        )

        return HedgeOrder(
            symbol=symbol,
            side=side,
            quantity=quantity,
            avg_price=avg_price,
            status=data.get("status", "FILLED"),
            order_id=str(data.get("orderId", "")),
        )

    async def close_hedge(
        self,
        symbol: str,
        side: str,
        quantity: float,
    ) -> HedgeOrder:
        """Close a hedge position.

        To close a SHORT, we BUY. To close a LONG, we SELL.

        Args:
            symbol: Binance futures symbol
            side: Original hedge side ("BUY" or "SELL") — will be reversed
            quantity: Position size to close
        """
        close_side = "SELL" if side == "BUY" else "BUY"

        precision = QTY_PRECISION.get(symbol, 3)
        quantity = round(quantity, precision)

        if quantity <= 0:
            return HedgeOrder(
                symbol=symbol,
                side=close_side,
                quantity=0.0,
                avg_price=0.0,
                status="FAILED",
            )

        data = await self._request("POST", "/fapi/v1/order", {
            "symbol": symbol,
            "side": close_side,
            "type": "MARKET",
            "quantity": str(quantity),
            "reduceOnly": "true",
        })

        avg_price = float(data.get("avgPrice", 0.0))
        if avg_price == 0.0 and data.get("fills"):
            total_qty = sum(float(f["qty"]) for f in data["fills"])
            total_cost = sum(
                float(f["qty"]) * float(f["price"]) for f in data["fills"]
            )
            avg_price = total_cost / total_qty if total_qty > 0 else 0.0

        _log.info(
            "hedge_closed",
            symbol=symbol,
            close_side=close_side,
            quantity=quantity,
            avg_price=avg_price,
            order_id=str(data.get("orderId", "")),
        )

        return HedgeOrder(
            symbol=symbol,
            side=close_side,
            quantity=quantity,
            avg_price=avg_price,
            status=data.get("status", "FILLED"),
            order_id=str(data.get("orderId", "")),
        )

    async def get_position(self, symbol: str) -> dict:
        """Query current position for a symbol."""
        data = await self._request("GET", "/fapi/v2/positionRisk", {
            "symbol": symbol,
        })
        for pos in data:
            if pos.get("symbol") == symbol:
                return pos
        return {}

    async def close(self) -> None:
        """Close the aiohttp session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
