"""Fee-adjusted pure arbitrage strategy for Polymarket.

Buys YES + NO tokens simultaneously when the combined cost (after all
fees) is low enough that the guaranteed $1 resolution payout produces
a net profit.
"""

from __future__ import annotations

from datetime import datetime, timezone

from src.config import Settings
from src.core.models import (
    FillEstimate,
    Market,
    Opportunity,
    Position,
    Side,
    StrategyType,
)
from src.data.orderbook import OrderBookManager
from src.monitoring.logger import get_logger
from src.strategy.base import BaseStrategy
from src.utils.fees import taker_fee_amount, winner_fee_amount
from src.utils.time_utils import is_in_dead_zone


class ArbitrageStrategy(BaseStrategy):
    """Fee-adjusted pure arbitrage: buy YES + NO when combined cost < threshold.

    The strategy guarantees profit regardless of the market outcome
    because one side always resolves to $1.  Profitability depends on
    the spread minus taker fees and the winner fee at resolution.
    """

    @property
    def name(self) -> str:  # noqa: D102
        return "arbitrage"

    @property
    def strategy_type(self) -> StrategyType:  # noqa: D102
        return StrategyType.ARBITRAGE

    # -- core logic -----------------------------------------------------------

    async def evaluate(self, market: Market) -> Opportunity | None:
        """Evaluate *market* for an arbitrage opportunity.

        Steps
        -----
        1. Get fill estimates for BUY on both the YES and NO tokens.
        2. Verify both sides have sufficient liquidity.
        3. Compute the combined cost using VWAP prices.
        4. Reject if combined cost >= ``target_pair_cost`` (hard ceiling).
        5. Compute net profit accounting for taker and winner fees.
        6. Reject if per-share net profit < ``min_profit_margin``.
        7. Reject if the market is in a dead zone.
        8. Return the :class:`Opportunity` with all details populated.
        """
        size = self._settings.order_size

        # 0. Staleness check
        if self._is_book_stale(market.yes_token_id) or self._is_book_stale(market.no_token_id):
            self._log.debug("stale_orderbook", market=market.slug)
            return None

        # 1. Fill estimates for both legs
        yes_fill = self._book_manager.get_fill_estimate(
            market.yes_token_id, Side.BUY, size,
        )
        no_fill = self._book_manager.get_fill_estimate(
            market.no_token_id, Side.BUY, size,
        )

        # 2. Both sides must have sufficient liquidity
        if yes_fill is None or no_fill is None:
            self._log.debug("missing_book", market=market.slug)
            return None

        if not yes_fill.sufficient_liquidity or not no_fill.sufficient_liquidity:
            self._log.debug(
                "insufficient_liquidity",
                market=market.slug,
                yes_liq=yes_fill.sufficient_liquidity,
                no_liq=no_fill.sufficient_liquidity,
            )
            return None

        # 3. Combined cost using VWAP
        yes_vwap = yes_fill.vwap
        no_vwap = no_fill.vwap
        combined_cost = yes_vwap + no_vwap

        # Log every evaluation at INFO level for visibility
        self._log.info(
            "arb_eval",
            market=market.slug,
            yes_ask=round(yes_vwap, 4),
            no_ask=round(no_vwap, 4),
            combined=round(combined_cost, 4),
            target=self._settings.target_pair_cost,
            gap=round(combined_cost - self._settings.target_pair_cost, 4),
        )

        # 4. Hard ceiling check
        if combined_cost >= self._settings.target_pair_cost:
            return None

        # 4b. Reject if either leg is below min entry price
        if yes_vwap < self._settings.min_entry_price or no_vwap < self._settings.min_entry_price:
            self._log.debug(
                "arb_price_floor_rejected",
                market=market.slug,
                yes_vwap=round(yes_vwap, 4),
                no_vwap=round(no_vwap, 4),
            )
            return None

        # 5. Net profit after all fees
        taker_yes = taker_fee_amount(yes_vwap, size)
        taker_no = taker_fee_amount(no_vwap, size)
        gross = (1.0 - yes_vwap - no_vwap) * size
        winner = winner_fee_amount(min(yes_vwap, no_vwap), 1.0) * size
        total_fees = taker_yes + taker_no + winner
        net_profit = gross - total_fees

        # 6. Per-share margin check
        profit_per_share = net_profit / size
        if profit_per_share < self._settings.min_profit_margin:
            self._log.debug(
                "profit_below_margin",
                market=market.slug,
                profit_per_share=profit_per_share,
                min_margin=self._settings.min_profit_margin,
            )
            return None

        # 7. Dead zone check
        start_ts = market.start_time.timestamp()
        end_ts = market.end_time.timestamp()
        if is_in_dead_zone(start_ts, end_ts):
            self._log.debug("dead_zone", market=market.slug)
            return None

        # 8. Build opportunity
        confidence = min(1.0, profit_per_share / self._settings.min_profit_margin)

        # KL divergence scoring (optional)
        kl_meta: dict[str, float] = {}
        if self._settings.enable_divergence_scoring:
            from src.utils.divergence import market_mispricing_score

            kl_meta = {f"kl_{k}": v for k, v in market_mispricing_score(yes_vwap, no_vwap).items()}

        opportunity = Opportunity(
            strategy=self.strategy_type,
            market=market,
            timestamp=datetime.now(tz=timezone.utc),
            yes_fill=yes_fill,
            no_fill=no_fill,
            expected_profit=net_profit,
            expected_profit_pct=profit_per_share,
            total_fees=total_fees,
            confidence=confidence,
            requested_size=size,
            metadata={
                "yes_vwap": yes_vwap,
                "no_vwap": no_vwap,
                "combined_cost": combined_cost,
                "gross": gross,
                "taker_yes": taker_yes,
                "taker_no": taker_no,
                "winner_fee": winner,
                **kl_meta,
            },
        )

        self._log.info(
            "opportunity_found",
            market=market.slug,
            combined_cost=round(combined_cost, 4),
            net_profit=round(net_profit, 4),
            profit_pct=round(profit_per_share, 6),
        )

        return opportunity

    # -- exit logic -----------------------------------------------------------

    def should_exit(self, position: Position, market: Market) -> bool:
        """Arb positions are held to resolution -- always return ``False``."""
        return False
