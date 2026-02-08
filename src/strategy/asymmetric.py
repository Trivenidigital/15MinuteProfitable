"""Asymmetric entry strategy: accumulate cheap shares via GTC limit orders.

Places limit orders at 0% maker fee to accumulate YES or NO shares when
they're unusually cheap. When the combined average cost basis makes a
profitable pair, the position is locked in for resolution.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from src.config import Settings
from src.core.models import (
    Market,
    Opportunity,
    Position,
    Side,
    StrategyType,
)
from src.data.orderbook import OrderBookManager
from src.monitoring.logger import get_logger
from src.strategy.base import BaseStrategy
from src.utils.fees import winner_fee_amount
from src.utils.time_utils import is_in_dead_zone, time_remaining_seconds


@dataclass
class AccumulationState:
    """Tracks per-market accumulation progress."""

    condition_id: str
    yes_shares: float = 0.0
    yes_total_cost: float = 0.0
    no_shares: float = 0.0
    no_total_cost: float = 0.0
    completed: bool = False

    @property
    def yes_avg_cost(self) -> float:
        """Average cost per YES share."""
        if self.yes_shares <= 0:
            return 0.0
        return self.yes_total_cost / self.yes_shares

    @property
    def no_avg_cost(self) -> float:
        """Average cost per NO share."""
        if self.no_shares <= 0:
            return 0.0
        return self.no_total_cost / self.no_shares

    @property
    def combined_avg_cost(self) -> float:
        """Combined average cost (YES avg + NO avg).

        Only meaningful when both sides have shares.
        """
        return self.yes_avg_cost + self.no_avg_cost

    @property
    def min_shares(self) -> float:
        """The minimum shares on either side (determines pairable amount)."""
        return min(self.yes_shares, self.no_shares)

    def can_complete(self, target_combined: float) -> bool:
        """Check if accumulated shares form a profitable pair.

        Both sides must have shares, and the combined average cost
        must be below the target.
        """
        if self.yes_shares <= 0 or self.no_shares <= 0:
            return False
        return self.combined_avg_cost < target_combined

    def record_buy(self, side: str, shares: float, cost: float) -> None:
        """Record a fill on one side."""
        if side == "YES":
            self.yes_shares += shares
            self.yes_total_cost += cost
        else:
            self.no_shares += shares
            self.no_total_cost += cost


class AsymmetricStrategy(BaseStrategy):
    """Accumulate cheap shares via GTC limit orders for 0% maker fee.

    Places small GTC buy orders when YES or NO asks are below configured
    thresholds. Tracks running cost basis per side per market. Detects
    when accumulated shares form a profitable pair.
    """

    def __init__(
        self,
        settings: Settings,
        book_manager: OrderBookManager,
    ) -> None:
        super().__init__(settings, book_manager)
        self._accumulations: dict[str, AccumulationState] = {}  # condition_id -> state

    @property
    def name(self) -> str:
        return "asymmetric"

    @property
    def strategy_type(self) -> StrategyType:
        return StrategyType.ASYMMETRIC

    def get_accumulation(self, condition_id: str) -> AccumulationState:
        """Get or create accumulation state for a market."""
        if condition_id not in self._accumulations:
            self._accumulations[condition_id] = AccumulationState(
                condition_id=condition_id
            )
        return self._accumulations[condition_id]

    async def evaluate(self, market: Market) -> Opportunity | None:
        """Evaluate market for asymmetric accumulation opportunity.

        Steps:
        1. Skip if market is in dead zone
        2. Get accumulation state for this market
        3. Skip if pair already completed
        4. Check if YES or NO side is cheap enough to accumulate
        5. Skip if already at max accumulation for that side
        6. Check pair completion (if both sides now have shares)
        7. Get fill estimate for the cheap side
        8. Build opportunity with GTC order type in metadata
        """
        # Staleness check
        if self._is_book_stale(market.yes_token_id) or self._is_book_stale(market.no_token_id):
            self._log.debug("stale_orderbook", market=market.slug)
            return None

        # 1. Dead zone check
        start_ts = market.start_time.timestamp()
        end_ts = market.end_time.timestamp()
        if is_in_dead_zone(start_ts, end_ts):
            return None

        # 2. Get accumulation state
        acc = self.get_accumulation(market.condition_id)

        # 3. Skip if already completed
        if acc.completed:
            return None

        # 4. Check both books for cheap asks
        yes_book = self._book_manager.get_book(market.yes_token_id)
        no_book = self._book_manager.get_book(market.no_token_id)

        if yes_book is None or no_book is None:
            return None

        yes_ask = yes_book.best_ask
        no_ask = no_book.best_ask

        # Determine which side to accumulate (prefer the cheaper one)
        buy_side: str | None = None
        buy_price: float = 0.0
        target_token_id: str = ""

        yes_cheap = yes_ask is not None and yes_ask < self._settings.yes_cheap_threshold
        no_cheap = no_ask is not None and no_ask < self._settings.no_cheap_threshold

        if yes_cheap and no_cheap:
            # Both cheap -- prefer the one we have fewer shares of (balance accumulation)
            if acc.yes_shares <= acc.no_shares:
                buy_side = "YES"
                buy_price = yes_ask
                target_token_id = market.yes_token_id
            else:
                buy_side = "NO"
                buy_price = no_ask
                target_token_id = market.no_token_id
        elif yes_cheap:
            buy_side = "YES"
            buy_price = yes_ask
            target_token_id = market.yes_token_id
        elif no_cheap:
            buy_side = "NO"
            buy_price = no_ask
            target_token_id = market.no_token_id
        else:
            # Check if we can now complete a pair (both sides accumulated)
            if acc.can_complete(self._settings.target_avg_combined):
                acc.completed = True
                self._log.info(
                    "pair_completed",
                    market=market.slug,
                    yes_avg=round(acc.yes_avg_cost, 4),
                    no_avg=round(acc.no_avg_cost, 4),
                    combined=round(acc.combined_avg_cost, 4),
                    min_shares=acc.min_shares,
                )
            return None

        # 5. Check max accumulation
        if buy_side == "YES" and acc.yes_shares >= self._settings.max_accumulation_per_side:
            self._log.debug("yes_max_reached", market=market.slug, shares=acc.yes_shares)
            # Still check pair completion
            if acc.can_complete(self._settings.target_avg_combined):
                acc.completed = True
            return None

        if buy_side == "NO" and acc.no_shares >= self._settings.max_accumulation_per_side:
            self._log.debug("no_max_reached", market=market.slug, shares=acc.no_shares)
            if acc.can_complete(self._settings.target_avg_combined):
                acc.completed = True
            return None

        # 6. Check pair completion before placing more orders
        if acc.can_complete(self._settings.target_avg_combined):
            acc.completed = True
            self._log.info(
                "pair_completed",
                market=market.slug,
                yes_avg=round(acc.yes_avg_cost, 4),
                no_avg=round(acc.no_avg_cost, 4),
                combined=round(acc.combined_avg_cost, 4),
            )
            return None

        # 7. Get fill estimate
        size = self._settings.accumulation_size
        fill = self._book_manager.get_fill_estimate(
            target_token_id,
            Side.BUY,
            size,
        )

        if fill is None or not fill.sufficient_liquidity:
            return None

        # 8. Build opportunity
        # Expected profit per share: what we'd make if pair completes at current costs
        # Hypothetical combined cost after this accumulation
        hypo_total_cost = buy_price * size
        if buy_side == "YES":
            hypo_yes_avg = (acc.yes_total_cost + hypo_total_cost) / (acc.yes_shares + size)
            hypo_combined = (
                hypo_yes_avg + acc.no_avg_cost if acc.no_shares > 0 else hypo_yes_avg + 0.5
            )
        else:
            hypo_no_avg = (acc.no_total_cost + hypo_total_cost) / (acc.no_shares + size)
            hypo_combined = (
                acc.yes_avg_cost + hypo_no_avg if acc.yes_shares > 0 else 0.5 + hypo_no_avg
            )

        # Winner fee only on profit at resolution
        winner_fee = winner_fee_amount(hypo_combined, 1.0) if hypo_combined < 1.0 else 0.0
        expected_profit_per_share = max(0, 1.0 - hypo_combined - winner_fee)
        expected_profit = expected_profit_per_share * min(
            acc.min_shares + size, acc.min_shares + size
        )
        profit_pct = expected_profit_per_share  # per share is already normalized

        confidence = (
            min(1.0, (self._settings.target_avg_combined - hypo_combined) / 0.10)
            if hypo_combined < self._settings.target_avg_combined
            else 0.3
        )

        if buy_side == "YES":
            yes_fill = fill
            no_fill = None
        else:
            yes_fill = None
            no_fill = fill

        opp = Opportunity(
            strategy=self.strategy_type,
            market=market,
            timestamp=datetime.now(tz=timezone.utc),
            yes_fill=yes_fill,
            no_fill=no_fill,
            expected_profit=expected_profit,
            expected_profit_pct=profit_pct,
            total_fees=winner_fee * size,
            confidence=confidence,
            requested_size=size,
            metadata={
                "buy_side": buy_side,
                "buy_price": buy_price,
                "target_token_id": target_token_id,
                "accumulation_size": size,
                "order_type": "GTC",
                "yes_accumulated": acc.yes_shares,
                "no_accumulated": acc.no_shares,
                "yes_avg_cost": acc.yes_avg_cost,
                "no_avg_cost": acc.no_avg_cost,
                "hypothetical_combined": hypo_combined,
            },
        )

        self._log.info(
            "accumulation_opportunity",
            market=market.slug,
            side=buy_side,
            price=buy_price,
            size=size,
            yes_acc=round(acc.yes_shares, 1),
            no_acc=round(acc.no_shares, 1),
            hypo_combined=round(hypo_combined, 4),
        )

        return opp

    def record_fill(self, condition_id: str, side: str, shares: float, cost: float) -> None:
        """Record a GTC order fill. Called after order fills are confirmed.

        Updates the accumulation state for the market.
        """
        acc = self.get_accumulation(condition_id)
        acc.record_buy(side, shares, cost)
        self._log.info(
            "fill_recorded",
            condition_id=condition_id[:12],
            side=side,
            shares=shares,
            cost=round(cost, 4),
            yes_total=round(acc.yes_shares, 1),
            no_total=round(acc.no_shares, 1),
            combined=(
                round(acc.combined_avg_cost, 4)
                if acc.yes_shares > 0 and acc.no_shares > 0
                else None
            ),
        )

    def should_exit(self, position: Position, market: Market) -> bool:
        """Asymmetric positions are held to resolution once completed.

        Returns True only if the market is about to expire and the pair
        is NOT completed (unhedged exposure that should be unwound).
        """
        acc = self._accumulations.get(market.condition_id)
        if acc is None:
            return False

        # Completed pairs are held to resolution
        if acc.completed:
            return False

        # If market expires soon and we have unhedged accumulation, exit
        remaining = time_remaining_seconds(market.end_time.timestamp())
        if remaining < self._settings.time_exit_seconds:
            if acc.yes_shares > 0 or acc.no_shares > 0:
                self._log.info(
                    "unhedged_exit",
                    market=market.slug,
                    yes_shares=acc.yes_shares,
                    no_shares=acc.no_shares,
                    time_remaining=round(remaining, 1),
                )
                return True

        return False

    def reset_market(self, condition_id: str) -> None:
        """Clear accumulation state for an expired market."""
        self._accumulations.pop(condition_id, None)

    def cleanup_market(self, condition_id: str) -> None:
        """Remove accumulation state for an expired market."""
        self._accumulations.pop(condition_id, None)
