"""Coordination layer for CEX perp hedging on Binance Futures.

Decides WHEN and HOW MUCH to hedge based on Polymarket trade parameters.
Maps Polymarket directional trades to corresponding Binance Futures positions.
"""

from __future__ import annotations

from src.config import Settings
from src.core.models import Opportunity, TradeOrder
from src.core.state import StateManager
from src.data.spot_buffer import SpotBuffer
from src.execution.binance_futures import (
    MIN_NOTIONAL,
    BinanceFuturesClient,
    HedgeOrder,
)
from src.monitoring.logger import get_logger

_log = get_logger("hedge_manager")

# Polymarket asset -> Binance Futures symbol
ASSET_TO_FUTURES_SYMBOL: dict[str, str] = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
    "XRP": "XRPUSDT",
}


class HedgeManager:
    """Manages CEX perp hedges for directional Polymarket trades.

    When a directional strategy (e.g. fade_panic) executes on Polymarket,
    this manager opens a corresponding hedge on Binance Futures to reduce
    variance and drawdown.

    Hedge logic:
    - Polymarket buys NO (betting price DOWN) -> Binance SHORT
    - Polymarket buys YES (betting price UP) -> Binance LONG
    - Quantity = hedge_ratio * polymarket_investment / spot_price
    """

    def __init__(
        self,
        settings: Settings,
        binance_client: BinanceFuturesClient,
        state_manager: StateManager,
        spot_buffer: SpotBuffer | None = None,
    ) -> None:
        self._settings = settings
        self._client = binance_client
        self._state = state_manager
        self._spot_buffer = spot_buffer
        self._hedge_ratio = settings.cex_hedge_ratio
        self._hedgeable_strategies = {
            s.strip()
            for s in settings.cex_hedgeable_strategies.split(",")
            if s.strip()
        }
        # Active hedges keyed by Polymarket condition_id
        self._active_hedges: dict[str, HedgeOrder] = {}

        _log.info(
            "hedge_manager_initialized",
            hedge_ratio=self._hedge_ratio,
            hedgeable_strategies=list(self._hedgeable_strategies),
            testnet=settings.binance_futures_testnet,
        )

    def _should_hedge(self, opp: Opportunity) -> bool:
        """Determine if this trade should be hedged."""
        if not self._settings.enable_cex_hedging:
            return False

        # Only hedge strategies in the allowed list
        if opp.strategy.value not in self._hedgeable_strategies:
            return False

        # Only hedge directional trades (not arb/asymmetric)
        is_arb = opp.yes_fill is not None and opp.no_fill is not None
        is_asymmetric = opp.metadata.get("order_type") == "GTC"
        if is_arb or is_asymmetric:
            return False

        # Must have an asset we can hedge
        return opp.market.asset in ASSET_TO_FUTURES_SYMBOL

    def _determine_hedge_side(self, opp: Opportunity) -> str:
        """Determine Binance Futures side based on Polymarket trade direction.

        - Polymarket buys NO (betting DOWN) -> Binance SHORT (SELL)
        - Polymarket buys YES (betting UP) -> Binance LONG (BUY)
        """
        direction = opp.metadata.get("direction", "UP")
        target_token_id = opp.metadata.get("target_token_id", "")

        # If target_token_id matches NO token, we're betting DOWN -> SHORT
        if target_token_id == opp.market.no_token_id or direction == "DOWN":
            return "SELL"
        return "BUY"

    def _calculate_quantity(
        self,
        opp: Opportunity,
        fill_result: TradeOrder,
        spot_price: float,
    ) -> float:
        """Calculate hedge quantity in base asset units.

        quantity = hedge_ratio * polymarket_investment / spot_price
        """
        investment = fill_result.fill_price * fill_result.fill_size
        notional = self._hedge_ratio * investment
        if spot_price <= 0:
            return 0.0
        return notional / spot_price

    async def hedge_trade(
        self,
        opp: Opportunity,
        fill_result: TradeOrder,
    ) -> HedgeOrder | None:
        """Open a hedge for a filled Polymarket directional trade.

        Called after a successful directional fill on Polymarket.

        Returns:
            HedgeOrder if hedge was placed, None if skipped.
        """
        if not self._should_hedge(opp):
            return None

        symbol = ASSET_TO_FUTURES_SYMBOL[opp.market.asset]
        side = self._determine_hedge_side(opp)

        # Get current spot price for quantity calculation
        binance_symbol = f"{opp.market.asset}USDT"
        spot_price = 0.0
        if self._spot_buffer is not None:
            spot_price = self._spot_buffer.get_price(binance_symbol) or 0.0

        if spot_price <= 0:
            _log.warning(
                "hedge_no_spot_price",
                symbol=symbol,
                condition_id=opp.market.condition_id,
            )
            return None

        quantity = self._calculate_quantity(opp, fill_result, spot_price)

        # Check minimum notional
        min_notional = MIN_NOTIONAL.get(symbol, 5.0)
        hedge_notional = quantity * spot_price
        if hedge_notional < min_notional:
            _log.info(
                "hedge_below_min_notional",
                symbol=symbol,
                hedge_notional=round(hedge_notional, 2),
                min_notional=min_notional,
            )
            return None

        hedge_order = await self._client.open_hedge(
            symbol=symbol,
            side=side,
            quantity=quantity,
            dry_run=self._settings.dry_run,
        )

        if hedge_order.status in ("FILLED", "SIMULATED"):
            self._active_hedges[opp.market.condition_id] = hedge_order
            _log.info(
                "hedge_tracked",
                condition_id=opp.market.condition_id,
                symbol=symbol,
                side=side,
                quantity=round(quantity, 6),
                active_hedges=len(self._active_hedges),
            )

        return hedge_order

    async def close_hedge_for_position(
        self,
        condition_id: str,
    ) -> HedgeOrder | None:
        """Close the hedge associated with a Polymarket position.

        Called when a Polymarket position resolves (market expires).

        Returns:
            HedgeOrder if hedge was closed, None if no hedge existed.
        """
        hedge = self._active_hedges.pop(condition_id, None)
        if hedge is None:
            return None

        _log.info(
            "closing_hedge",
            condition_id=condition_id,
            symbol=hedge.symbol,
            side=hedge.side,
            quantity=hedge.quantity,
        )

        close_order = await self._client.close_hedge(
            symbol=hedge.symbol,
            side=hedge.side,
            quantity=hedge.quantity,
        )

        _log.info(
            "hedge_closed",
            condition_id=condition_id,
            symbol=close_order.symbol,
            status=close_order.status,
            active_hedges=len(self._active_hedges),
        )

        return close_order

    @property
    def active_hedge_count(self) -> int:
        """Number of currently open hedges."""
        return len(self._active_hedges)

    def get_hedge(self, condition_id: str) -> HedgeOrder | None:
        """Get the active hedge for a condition_id, if any."""
        return self._active_hedges.get(condition_id)
