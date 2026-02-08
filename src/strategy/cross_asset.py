"""Cross-asset correlation strategy using Bregman projection.

Detects mispricings across BTC/ETH/SOL/XRP by projecting observed market
probabilities onto a correlation-consistent set via iterative KL projection.
When projected probabilities diverge significantly from observed, the market
is mispriced relative to its correlated peers.

This strategy requires ``enable_cross_asset_strategy=True`` and overrides
``evaluate_all()`` to see all markets simultaneously.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from src.config import Settings
from src.core.models import (
    Market,
    Opportunity,
    Position,
    Side,
    StrategyType,
)
from src.data.orderbook import OrderBookManager
from src.data.spot_buffer import SpotBuffer
from src.strategy.base import BaseStrategy
from src.strategy.price_lag import ASSET_TO_BINANCE_SYMBOL
from src.utils.divergence import (
    bregman_projection,
    compute_return_correlation,
    market_mispricing_score,
)
from src.utils.fees import taker_fee_amount

# How often to recompute the correlation matrix (seconds)
_CORRELATION_REFRESH_INTERVAL = 900.0  # 15 minutes


class CrossAssetCorrelationStrategy(BaseStrategy):
    """Detects mispricings across BTC/ETH/SOL/XRP via Bregman projection.

    Uses historical spot-price correlations to project "fair" probabilities,
    then trades the largest deviations from the projection.
    """

    def __init__(
        self,
        settings: Settings,
        book_manager: OrderBookManager,
        spot_buffer: SpotBuffer,
        trade_db: object | None = None,
    ) -> None:
        super().__init__(settings, book_manager)
        self._spot_buffer = spot_buffer
        self._trade_db = trade_db

        # Cached correlation matrix and refresh timestamp
        self._correlation_matrix: list[list[float]] | None = None
        self._correlation_updated_at: float = 0.0

    @property
    def name(self) -> str:
        return "cross_asset"

    @property
    def strategy_type(self) -> StrategyType:
        return StrategyType.CROSS_ASSET

    async def evaluate(self, market: Market) -> Opportunity | None:
        """Single-market evaluation is a no-op for cross-asset strategy."""
        return None

    async def evaluate_all(self, markets: list[Market]) -> list[Opportunity]:
        """Evaluate ALL markets simultaneously for cross-asset mispricings.

        Steps:
        1. Refresh correlation matrix if stale (from spot buffer histories)
        2. Build odds vector from orderbooks: one entry per market
        3. Bregman-project onto correlation-consistent set
        4. Trade largest deviations (projected - observed > min_divergence)
        """
        if len(markets) < 2:
            return []

        # 1. Refresh correlation matrix
        self._maybe_refresh_correlation(markets)
        if self._correlation_matrix is None:
            return []

        # 2. Build observed odds vector from orderbooks
        observed_odds: list[float] = []
        valid_markets: list[Market] = []
        yes_asks: list[float] = []
        no_asks: list[float] = []

        for market in markets:
            yes_book = self._book_manager.get_book(market.yes_token_id)
            no_book = self._book_manager.get_book(market.no_token_id)

            if yes_book is None or no_book is None:
                continue
            if yes_book.best_ask is None or no_book.best_ask is None:
                continue
            if self._is_book_stale(market.yes_token_id) or self._is_book_stale(
                market.no_token_id
            ):
                continue

            ya = yes_book.best_ask
            na = no_book.best_ask
            # Normalize to probability simplex
            total = ya + na
            if total <= 0:
                continue

            observed_odds.append(ya / total)
            valid_markets.append(market)
            yes_asks.append(ya)
            no_asks.append(na)

        n = len(valid_markets)
        if n < 2:
            return []

        # Ensure correlation matrix dimension matches
        corr = self._correlation_matrix
        if len(corr) != n:
            # Rebuild with matching dimension
            corr = self._build_correlation_matrix(valid_markets)
            if corr is None or len(corr) != n:
                return []

        # 3. Bregman-project
        projected = bregman_projection(observed_odds, corr, num_iterations=50)

        # 4. Find largest deviations
        min_div = self._settings.cross_asset_min_divergence
        opportunities: list[Opportunity] = []

        for i in range(n):
            deviation = projected[i] - observed_odds[i]
            if abs(deviation) < min_div:
                continue

            market = valid_markets[i]
            ya = yes_asks[i]
            na = no_asks[i]

            # Positive deviation: projected > observed → YES is underpriced → buy YES
            # Negative deviation: projected < observed → NO is underpriced → buy NO
            if deviation > 0:
                direction = "UP"
                target_token_id = market.yes_token_id
            else:
                direction = "DOWN"
                target_token_id = market.no_token_id

            size = self._settings.cross_asset_order_size

            fill = self._book_manager.get_fill_estimate(
                target_token_id, Side.BUY, size
            )
            if fill is None or not fill.sufficient_liquidity:
                continue

            # Expected profit: conservative — capture 30% of deviation
            taker_fee = taker_fee_amount(fill.vwap, size)
            expected_profit = abs(deviation) * 0.3 * size - taker_fee
            if expected_profit <= 0:
                continue

            profit_pct = (
                expected_profit / (fill.vwap * size) if fill.vwap > 0 else 0.0
            )
            confidence = min(1.0, abs(deviation) / (min_div * 3))

            # KL mispricing score
            kl_data = market_mispricing_score(ya, na)

            if direction == "UP":
                yes_fill = fill
                no_fill = None
            else:
                yes_fill = None
                no_fill = fill

            opp = Opportunity(
                strategy=self.strategy_type,
                market=market,
                timestamp=datetime.now(tz=UTC),
                yes_fill=yes_fill,
                no_fill=no_fill,
                expected_profit=expected_profit,
                expected_profit_pct=profit_pct,
                total_fees=taker_fee,
                confidence=confidence,
                requested_size=size,
                metadata={
                    "direction": direction,
                    "target_token_id": target_token_id,
                    "observed_odds": round(observed_odds[i], 6),
                    "projected_odds": round(projected[i], 6),
                    "deviation": round(deviation, 6),
                    "num_assets": n,
                    **{f"kl_{k}": v for k, v in kl_data.items()},
                },
            )

            self._log.info(
                "cross_asset_opportunity",
                market=market.slug,
                direction=direction,
                deviation=round(deviation, 6),
                observed=round(observed_odds[i], 4),
                projected=round(projected[i], 4),
                expected_profit=round(expected_profit, 4),
            )

            opportunities.append(opp)

        return opportunities

    def should_exit(self, position: Position, market: Market) -> bool:
        """Hold cross-asset positions to resolution."""
        return False

    # -- internal helpers -----------------------------------------------------

    def _maybe_refresh_correlation(self, markets: list[Market]) -> None:
        """Refresh the correlation matrix if stale."""
        now = time.time()
        if (
            self._correlation_matrix is not None
            and now - self._correlation_updated_at < _CORRELATION_REFRESH_INTERVAL
        ):
            return

        corr = self._build_correlation_matrix(markets)
        if corr is not None:
            self._correlation_matrix = corr
            self._correlation_updated_at = now

    def _build_correlation_matrix(
        self, markets: list[Market]
    ) -> list[list[float]] | None:
        """Build correlation matrix from spot buffer price histories.

        Tries the spot buffer first.  Falls back to trade_db if the spot
        buffer doesn't have enough data.
        """
        window = self._settings.cross_asset_correlation_window

        # Collect price series from spot buffer
        price_series: list[list[tuple[float, float]]] = []
        for market in markets:
            symbol = ASSET_TO_BINANCE_SYMBOL.get(market.asset)
            if symbol is None:
                return None

            history = self._spot_buffer.get_price_history(symbol, window)
            if len(history) < 10:
                # Try DB fallback
                db_series = self._get_db_spot_series(symbol, window)
                if db_series is not None and len(db_series) >= 10:
                    history = db_series
                else:
                    self._log.debug(
                        "cross_asset_insufficient_data",
                        symbol=symbol,
                        points=len(history),
                    )
                    return None

            price_series.append(history)

        if len(price_series) < 2:
            return None

        corr = compute_return_correlation(price_series, window)
        self._log.debug(
            "cross_asset_correlation_computed",
            assets=len(price_series),
            matrix_size=f"{len(corr)}x{len(corr)}",
        )
        return corr

    def _get_db_spot_series(
        self, symbol: str, window_seconds: int
    ) -> list[tuple[float, float]] | None:
        """Fetch aligned spot histories from DB for correlation computation."""
        if self._trade_db is None:
            return None

        try:
            snapshots = self._trade_db.get_spot_series_for_correlation(  # type: ignore[attr-defined]
                [symbol], window_seconds
            )
            if symbol in snapshots:
                return snapshots[symbol]
        except (AttributeError, Exception):
            pass

        return None
