"""Maker arbitrage strategy for Polymarket.

Uses GTC limit orders (0% maker fee) instead of FOK market orders.
This allows profitable arbitrage with combined YES+NO prices up to ~$0.98
instead of requiring < $0.94 with taker fees.

The trade-off is that orders may not fill immediately.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from src.config import Settings
from src.core.models import (
    Market,
    Opportunity,
    Position,
    StrategyType,
)
from src.data.orderbook import OrderBookManager
from src.strategy.base import BaseStrategy
from src.utils.fees import net_maker_arb_profit
from src.utils.time_utils import is_in_dead_zone


# ---------------------------------------------------------------------------
# ArbPair dataclass
# ---------------------------------------------------------------------------


@dataclass
class ArbPair:
    """Tracks a paired YES+NO maker arbitrage attempt.

    Lifecycle:
        pending  -> both orders submitted, waiting for fills
        partial  -> one leg filled, other pending
        complete -> both legs filled (success!)
        cancelled -> timed out or manually cancelled
        unwound   -> partial fill was unwound (sold the filled leg)
    """

    pair_id: str
    condition_id: str
    yes_order_id: str | None = None
    no_order_id: str | None = None
    yes_price: float = 0.0
    no_price: float = 0.0
    size: float = 0.0
    yes_filled: bool = False
    no_filled: bool = False
    yes_fill_size: float = 0.0
    no_fill_size: float = 0.0
    created_at: float = field(default_factory=time.time)
    status: str = "pending"

    @property
    def is_complete(self) -> bool:
        """Return True if both legs are filled."""
        return self.yes_filled and self.no_filled

    @property
    def is_partial(self) -> bool:
        """Return True if exactly one leg is filled."""
        return self.yes_filled != self.no_filled

    @property
    def age_seconds(self) -> float:
        """Return the age of this pair in seconds."""
        return time.time() - self.created_at

    @property
    def combined_cost(self) -> float:
        """Return the combined YES + NO price."""
        return self.yes_price + self.no_price


# ---------------------------------------------------------------------------
# MakerArbitrageStrategy
# ---------------------------------------------------------------------------


class MakerArbitrageStrategy(BaseStrategy):
    """Maker arbitrage: buy YES + NO with GTC limit orders (0% maker fee).

    Key differences from taker arbitrage:
    - Uses GTC limit orders instead of FOK market orders
    - Places orders slightly below best ask (price_offset)
    - Higher profit threshold (~$0.98 vs ~$0.94 combined)
    - Orders may sit unfilled, requiring monitoring
    """

    def __init__(
        self,
        settings: Settings,
        book_manager: OrderBookManager,
    ) -> None:
        super().__init__(settings, book_manager)
        self._pending_pairs: dict[str, ArbPair] = {}

    @property
    def name(self) -> str:  # noqa: D102
        return "maker_arbitrage"

    @property
    def strategy_type(self) -> StrategyType:  # noqa: D102
        return StrategyType.MAKER_ARBITRAGE

    # -- core logic -----------------------------------------------------------

    async def evaluate(self, market: Market) -> Opportunity | None:
        """Evaluate *market* for a maker arbitrage opportunity.

        Steps
        -----
        1. Check if we already have max pending pairs.
        2. Get best ask prices for YES and NO tokens.
        3. Apply price offset (place slightly below best ask).
        4. Reject if combined cost >= maker_target_pair_cost.
        5. Compute net profit (no taker fee, only winner fee).
        6. Reject if per-share net profit < maker_min_profit_margin.
        7. Reject if the market is in a dead zone.
        8. Return the Opportunity with GTC metadata.
        """
        # Staleness check
        if self._is_book_stale(market.yes_token_id) or self._is_book_stale(market.no_token_id):
            self._log.debug("stale_orderbook", market=market.slug)
            return None

        # 1. Check pending pairs limit
        active_pairs = sum(
            1 for p in self._pending_pairs.values()
            if p.condition_id == market.condition_id and p.status == "pending"
        )
        if active_pairs >= self._settings.maker_max_pending_pairs:
            self._log.debug(
                "max_pending_pairs_reached",
                market=market.slug,
                active=active_pairs,
            )
            return None

        # 2. Get orderbooks
        yes_book = self._book_manager.get_book(market.yes_token_id)
        no_book = self._book_manager.get_book(market.no_token_id)

        if yes_book is None or no_book is None:
            self._log.debug("missing_book", market=market.slug)
            return None

        yes_best_ask = yes_book.best_ask
        no_best_ask = no_book.best_ask

        if yes_best_ask is None or no_best_ask is None:
            self._log.debug("no_ask_available", market=market.slug)
            return None

        # 3. Apply price offset (place limit order below best ask)
        offset = self._settings.maker_price_offset
        yes_price = round(yes_best_ask - offset, 2)
        no_price = round(no_best_ask - offset, 2)

        # Ensure prices are at least 0.01
        yes_price = max(0.01, yes_price)
        no_price = max(0.01, no_price)

        # Combined cost
        combined_cost = yes_price + no_price

        # Log every evaluation at INFO level for visibility
        self._log.info(
            "maker_arb_eval",
            market=market.slug,
            yes_ask=round(yes_best_ask, 4),
            no_ask=round(no_best_ask, 4),
            yes_limit=round(yes_price, 4),
            no_limit=round(no_price, 4),
            combined=round(combined_cost, 4),
            target=self._settings.maker_target_pair_cost,
            gap=round(combined_cost - self._settings.maker_target_pair_cost, 4),
        )

        # 4. Hard ceiling check (higher threshold than taker arb)
        if combined_cost >= self._settings.maker_target_pair_cost:
            return None

        # 5. Net profit (no taker fee, only winner fee)
        size = self._settings.order_size
        net_profit = net_maker_arb_profit(yes_price, no_price, size)
        gross = (1.0 - yes_price - no_price) * size
        winner_fee = gross - net_profit

        # 6. Per-share margin check
        profit_per_share = net_profit / size
        if profit_per_share < self._settings.maker_min_profit_margin:
            self._log.debug(
                "maker_profit_below_margin",
                market=market.slug,
                profit_per_share=profit_per_share,
                min_margin=self._settings.maker_min_profit_margin,
            )
            return None

        # 7. Dead zone check
        start_ts = market.start_time.timestamp()
        end_ts = market.end_time.timestamp()
        if is_in_dead_zone(start_ts, end_ts):
            self._log.debug("dead_zone", market=market.slug)
            return None

        # 8. Build opportunity with GTC metadata
        confidence = min(1.0, profit_per_share / self._settings.maker_min_profit_margin)

        # KL divergence scoring (optional)
        kl_meta: dict[str, float] = {}
        if self._settings.enable_divergence_scoring:
            from src.utils.divergence import market_mispricing_score

            kl_meta = {f"kl_{k}": v for k, v in market_mispricing_score(yes_price, no_price).items()}

        opportunity = Opportunity(
            strategy=self.strategy_type,
            market=market,
            timestamp=datetime.now(tz=timezone.utc),
            yes_fill=None,  # No fill estimate for maker orders
            no_fill=None,
            expected_profit=net_profit,
            expected_profit_pct=profit_per_share,
            total_fees=winner_fee,
            confidence=confidence,
            requested_size=size,
            metadata={
                "order_type": "GTC",
                "paired": True,
                "yes_price": yes_price,
                "no_price": no_price,
                "combined_cost": combined_cost,
                "gross": gross,
                "winner_fee": winner_fee,
                **kl_meta,
            },
        )

        self._log.info(
            "maker_arb_opportunity_found",
            market=market.slug,
            combined_cost=round(combined_cost, 4),
            net_profit=round(net_profit, 4),
            profit_pct=round(profit_per_share, 6),
        )

        return opportunity

    # -- pair management ------------------------------------------------------

    def create_pair(
        self,
        condition_id: str,
        yes_price: float,
        no_price: float,
        size: float,
    ) -> ArbPair:
        """Create and register a new ArbPair."""
        pair = ArbPair(
            pair_id=uuid.uuid4().hex[:12],
            condition_id=condition_id,
            yes_price=yes_price,
            no_price=no_price,
            size=size,
        )
        self._pending_pairs[pair.pair_id] = pair
        self._log.debug(
            "pair_created",
            pair_id=pair.pair_id,
            condition_id=condition_id,
        )
        return pair

    def get_pair(self, pair_id: str) -> ArbPair | None:
        """Get a pair by ID."""
        return self._pending_pairs.get(pair_id)

    def get_pending_pairs(self) -> list[ArbPair]:
        """Return all pairs that are still pending (not complete/cancelled)."""
        return [
            p for p in self._pending_pairs.values()
            if p.status in ("pending", "partial")
        ]

    def record_fill(
        self,
        pair_id: str,
        side: str,
        fill_size: float,
    ) -> bool:
        """Record a fill for one leg of a pair.

        Args:
            pair_id: The pair ID
            side: "YES" or "NO"
            fill_size: Size that was filled

        Returns:
            True if the pair is now complete (both legs filled)
        """
        pair = self._pending_pairs.get(pair_id)
        if pair is None:
            self._log.warning("record_fill_unknown_pair", pair_id=pair_id)
            return False

        if side == "YES":
            pair.yes_filled = True
            pair.yes_fill_size = fill_size
        elif side == "NO":
            pair.no_filled = True
            pair.no_fill_size = fill_size

        # Update status
        if pair.is_complete:
            pair.status = "complete"
            self._log.info(
                "maker_arb_pair_complete",
                pair_id=pair_id,
                condition_id=pair.condition_id,
            )
            return True
        elif pair.is_partial:
            pair.status = "partial"
            self._log.info(
                "maker_arb_pair_partial",
                pair_id=pair_id,
                side=side,
            )

        return False

    def cancel_pair(self, pair_id: str, status: str = "cancelled") -> None:
        """Mark a pair as cancelled or unwound."""
        pair = self._pending_pairs.get(pair_id)
        if pair is not None:
            pair.status = status
            self._log.info(
                "maker_arb_pair_cancelled",
                pair_id=pair_id,
                status=status,
            )

    def remove_pair(self, pair_id: str) -> None:
        """Remove a completed/cancelled pair from tracking."""
        if pair_id in self._pending_pairs:
            del self._pending_pairs[pair_id]

    def get_timed_out_pairs(self) -> list[ArbPair]:
        """Return pairs that have exceeded the timeout."""
        timeout = self._settings.maker_pair_timeout_seconds
        return [
            p for p in self._pending_pairs.values()
            if p.status in ("pending", "partial") and p.age_seconds > timeout
        ]

    def cleanup_completed_pairs(self) -> int:
        """Remove completed/cancelled pairs. Returns count removed."""
        dead = [pid for pid, p in self._pending_pairs.items() if p.status in ("complete", "cancelled", "timed_out")]
        for pid in dead:
            del self._pending_pairs[pid]
        return len(dead)

    # -- exit logic -----------------------------------------------------------

    def should_exit(self, position: Position, market: Market) -> bool:
        """Maker arb positions are held to resolution -- always return False."""
        return False
