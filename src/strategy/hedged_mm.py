"""Hedged market-maker strategy for Polymarket.

Inspired by @distinct-baguette ($555K profit, 32K markets):
1. Buy BOTH YES and NO early in the 15-min window via GTC limit orders (0% maker fee).
2. When odds shift mid-window, SELL the appreciated side at a profit (scalp).
3. Hold the remaining cheap side to resolution ($1.00 payout or $0.00).

Key advantages:
- GTC entries = 0% maker fee (vs 3-5% taker fee on FOK).
- Scalp profit on the winning side is locked in.
- Remaining side has asymmetric upside: costs ~$0.48, pays $1.00 or $0.00.
- Worst case (no scalp): degenerates to maker arb (~$0.02/pair profit).
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
from src.monitoring.logger import get_logger
from src.strategy.base import BaseStrategy
from src.utils.time_utils import time_remaining_seconds


# ---------------------------------------------------------------------------
# HMMPair dataclass
# ---------------------------------------------------------------------------


@dataclass
class HMMPair:
    """Tracks a paired YES+NO position through its lifecycle.

    Lifecycle:
        pending_entry -> hedged -> scalped -> closed (or cancelled)
    """

    pair_id: str
    condition_id: str
    market_slug: str
    size: float

    yes_order_id: str | None = None
    no_order_id: str | None = None
    yes_entry_price: float = 0.0
    no_entry_price: float = 0.0
    yes_filled: bool = False
    no_filled: bool = False
    yes_fill_size: float = 0.0
    no_fill_size: float = 0.0

    created_at: float = field(default_factory=time.time)
    status: str = "pending_entry"  # pending_entry | hedged | scalped | closed | cancelled

    scalp_side: str | None = None   # "YES" or "NO"
    scalp_price: float = 0.0

    @property
    def is_entry_complete(self) -> bool:
        """Return True if both legs are filled."""
        return self.yes_filled and self.no_filled

    @property
    def combined_entry_cost(self) -> float:
        """Return the combined YES + NO entry price."""
        return self.yes_entry_price + self.no_entry_price

    @property
    def is_scalped(self) -> bool:
        """Return True if one side has been scalped."""
        return self.scalp_side is not None

    @property
    def remaining_side(self) -> str | None:
        """Return the side that remains after scalp, or None if not scalped."""
        if self.scalp_side == "YES":
            return "NO"
        elif self.scalp_side == "NO":
            return "YES"
        return None

    @property
    def age_seconds(self) -> float:
        """Return the age of this pair in seconds."""
        return time.time() - self.created_at


# ---------------------------------------------------------------------------
# HedgedMMStrategy
# ---------------------------------------------------------------------------

_log = get_logger("hedged_mm")


class HedgedMMStrategy(BaseStrategy):
    """Hedged market-maker: buy YES+NO with GTC, scalp the appreciated side.

    Three phases per pair:
    1. Entry: place GTC limit orders for both YES and NO below best ask.
    2. Scalp: when one side appreciates by hmm_scalp_min_profit_pct, SELL it.
    3. Hold: remaining side is held to resolution (or time-exited).
    """

    def __init__(
        self,
        settings: Settings,
        book_manager: OrderBookManager,
    ) -> None:
        super().__init__(settings, book_manager)
        self._active_pairs: dict[str, HMMPair] = {}
        self._market_pair_count: dict[str, int] = {}  # condition_id -> active count

    @property
    def name(self) -> str:
        return "hedged_mm"

    @property
    def strategy_type(self) -> StrategyType:
        return StrategyType.HEDGED_MM

    # -- core logic -----------------------------------------------------------

    async def evaluate(self, market: Market) -> Opportunity | None:
        """Evaluate market for HMM opportunity (entry, scalp, or time_exit).

        Routes to the appropriate phase based on active pair state.
        """
        active_pair = self.get_active_pair_for_market(market.condition_id)

        if active_pair is None:
            return self._evaluate_entry(market)
        elif active_pair.status == "hedged":
            return self._evaluate_scalp(active_pair, market)
        elif active_pair.status == "scalped":
            return self._evaluate_time_exit(active_pair, market)

        return None

    def _evaluate_entry(self, market: Market) -> Opportunity | None:
        """Phase 1: check if we should enter a new hedged pair."""
        settings = self._settings

        # Check pending pairs limit
        pending_count = len(self.get_pending_pairs())
        if pending_count >= settings.hmm_max_pending_pairs:
            self._log.debug("hmm_skip_max_pending", market=market.slug, pending=pending_count)
            return None

        # Check per-market limit
        market_count = self._market_pair_count.get(market.condition_id, 0)
        if market_count >= settings.hmm_max_per_market:
            self._log.debug("hmm_skip_market_limit", market=market.slug, count=market_count)
            return None

        # Check entry window (only in first N seconds of window)
        # NOTE: market.start_time is Gamma API's startDate (~24h before window).
        # The actual 15-min window starts at end_time - 900s.
        end_ts = market.end_time.timestamp()
        window_start_ts = end_ts - 900.0  # actual 15-min window start
        now = time.time()
        elapsed = now - window_start_ts
        if elapsed < 0 or elapsed > settings.hmm_entry_window_seconds:
            self._log.debug(
                "hmm_skip_entry_window",
                market=market.slug,
                elapsed_s=round(elapsed, 0),
                window_s=settings.hmm_entry_window_seconds,
            )
            return None

        # Must have enough time remaining
        remaining = time_remaining_seconds(end_ts)
        if remaining < 60.0:
            self._log.debug("hmm_skip_time_remaining", market=market.slug, remaining_s=round(remaining, 1))
            return None

        # Staleness check
        if self._is_book_stale(market.yes_token_id) or self._is_book_stale(market.no_token_id):
            self._log.debug("hmm_skip_stale_book", market=market.slug)
            return None

        # Get orderbooks
        yes_book = self._book_manager.get_book(market.yes_token_id)
        no_book = self._book_manager.get_book(market.no_token_id)
        if yes_book is None or no_book is None:
            self._log.debug("hmm_skip_no_book", market=market.slug)
            return None

        yes_best_ask = yes_book.best_ask
        no_best_ask = no_book.best_ask
        if yes_best_ask is None or no_best_ask is None:
            self._log.debug("hmm_skip_no_ask", market=market.slug)
            return None

        # Apply price offset (place limit below best ask for maker status)
        offset = settings.hmm_price_offset
        yes_limit = round(yes_best_ask - offset, 2)
        no_limit = round(no_best_ask - offset, 2)

        # Floor at 0.01
        yes_limit = max(0.01, yes_limit)
        no_limit = max(0.01, no_limit)

        # Check combined cost
        combined = yes_limit + no_limit
        if combined >= settings.hmm_max_combined_cost:
            self._log.debug(
                "hmm_combined_too_high",
                market=market.slug,
                combined=round(combined, 4),
                limit=settings.hmm_max_combined_cost,
            )
            return None

        # Guaranteed spread profit if both resolve: $1.00 - combined
        spread_profit = 1.0 - combined
        size = settings.hmm_order_size

        self._log.info(
            "hmm_entry_opportunity",
            market=market.slug,
            yes_ask=round(yes_best_ask, 4),
            no_ask=round(no_best_ask, 4),
            yes_limit=round(yes_limit, 4),
            no_limit=round(no_limit, 4),
            combined=round(combined, 4),
            spread_profit=round(spread_profit, 4),
            elapsed_s=round(elapsed, 0),
        )

        return Opportunity(
            strategy=self.strategy_type,
            market=market,
            timestamp=datetime.now(tz=timezone.utc),
            expected_profit=spread_profit * size,
            expected_profit_pct=spread_profit,
            total_fees=0.0,  # Maker fee = 0%
            confidence=min(1.0, spread_profit / 0.02),  # $0.02 spread = 1.0
            requested_size=size,
            metadata={
                "action": "entry",
                "hmm": True,
                "yes_price": yes_limit,
                "no_price": no_limit,
                "combined_cost": combined,
                "order_type": "GTC",
            },
        )

    def _evaluate_scalp(self, pair: HMMPair, market: Market) -> Opportunity | None:
        """Phase 2: check if either side has appreciated enough to scalp."""
        settings = self._settings

        # Get current best bids for both sides
        yes_book = self._book_manager.get_book(market.yes_token_id)
        no_book = self._book_manager.get_book(market.no_token_id)
        if yes_book is None or no_book is None:
            return None

        yes_bid = yes_book.best_bid
        no_bid = no_book.best_bid
        if yes_bid is None or no_bid is None:
            return None

        # Calculate appreciation on each side
        yes_appreciation = 0.0
        no_appreciation = 0.0
        if pair.yes_entry_price > 0:
            yes_appreciation = (yes_bid - pair.yes_entry_price) / pair.yes_entry_price
        if pair.no_entry_price > 0:
            no_appreciation = (no_bid - pair.no_entry_price) / pair.no_entry_price

        min_profit = settings.hmm_scalp_min_profit_pct

        # Determine which side to scalp (if any)
        scalp_side: str | None = None
        scalp_price = 0.0
        scalp_token_id = ""

        if yes_appreciation >= min_profit and no_appreciation >= min_profit:
            # Both appreciated — sell the higher one
            if yes_appreciation >= no_appreciation:
                scalp_side = "YES"
                scalp_price = yes_bid
                scalp_token_id = market.yes_token_id
            else:
                scalp_side = "NO"
                scalp_price = no_bid
                scalp_token_id = market.no_token_id
        elif yes_appreciation >= min_profit:
            scalp_side = "YES"
            scalp_price = yes_bid
            scalp_token_id = market.yes_token_id
        elif no_appreciation >= min_profit:
            scalp_side = "NO"
            scalp_price = no_bid
            scalp_token_id = market.no_token_id

        if scalp_side is None:
            return None

        entry_price = pair.yes_entry_price if scalp_side == "YES" else pair.no_entry_price
        scalp_profit = (scalp_price - entry_price) * pair.size

        self._log.info(
            "hmm_scalp_opportunity",
            market=market.slug,
            pair_id=pair.pair_id,
            scalp_side=scalp_side,
            entry_price=round(entry_price, 4),
            scalp_price=round(scalp_price, 4),
            appreciation_pct=round(max(yes_appreciation, no_appreciation) * 100, 2),
            scalp_profit=round(scalp_profit, 4),
        )

        return Opportunity(
            strategy=self.strategy_type,
            market=market,
            timestamp=datetime.now(tz=timezone.utc),
            expected_profit=scalp_profit,
            expected_profit_pct=(scalp_price - entry_price) / entry_price if entry_price > 0 else 0,
            total_fees=0.0,
            confidence=1.0,
            requested_size=pair.size,
            metadata={
                "action": "scalp",
                "hmm": True,
                "scalp_side": scalp_side,
                "scalp_token_id": scalp_token_id,
                "scalp_price": scalp_price,
                "pair_id": pair.pair_id,
                "entry_price": entry_price,
            },
        )

    def _evaluate_time_exit(self, pair: HMMPair, market: Market) -> Opportunity | None:
        """Phase 3: force-sell remaining side near window end."""
        if self._settings.hmm_hold_to_resolution:
            # Hold to resolution — the resolution loop handles cleanup
            return None

        end_ts = market.end_time.timestamp()
        remaining = time_remaining_seconds(end_ts)

        if remaining > self._settings.hmm_time_exit_seconds:
            return None

        remaining_side = pair.remaining_side
        if remaining_side is None:
            return None

        # Get best bid for the remaining side
        token_id = (
            market.yes_token_id if remaining_side == "YES" else market.no_token_id
        )
        book = self._book_manager.get_book(token_id)
        if book is None or book.best_bid is None:
            return None

        exit_price = book.best_bid
        entry_price = (
            pair.yes_entry_price if remaining_side == "YES" else pair.no_entry_price
        )

        self._log.info(
            "hmm_time_exit",
            market=market.slug,
            pair_id=pair.pair_id,
            remaining_side=remaining_side,
            exit_price=round(exit_price, 4),
            remaining_seconds=round(remaining, 1),
        )

        return Opportunity(
            strategy=self.strategy_type,
            market=market,
            timestamp=datetime.now(tz=timezone.utc),
            expected_profit=(exit_price - entry_price) * pair.size,
            expected_profit_pct=(exit_price - entry_price) / entry_price if entry_price > 0 else 0,
            total_fees=0.0,
            confidence=1.0,
            requested_size=pair.size,
            metadata={
                "action": "time_exit",
                "hmm": True,
                "scalp_side": remaining_side,
                "scalp_token_id": token_id,
                "scalp_price": exit_price,
                "pair_id": pair.pair_id,
                "entry_price": entry_price,
            },
        )

    # -- pair management (mirrors MakerArbitrageStrategy pattern) -------------

    def create_pair(
        self,
        condition_id: str,
        market_slug: str,
        yes_price: float,
        no_price: float,
        size: float,
    ) -> HMMPair:
        """Create and register a new HMMPair."""
        pair = HMMPair(
            pair_id=uuid.uuid4().hex[:12],
            condition_id=condition_id,
            market_slug=market_slug,
            size=size,
            yes_entry_price=yes_price,
            no_entry_price=no_price,
        )
        self._active_pairs[pair.pair_id] = pair
        self._market_pair_count[condition_id] = (
            self._market_pair_count.get(condition_id, 0) + 1
        )
        self._log.info(
            "hmm_pair_created",
            pair_id=pair.pair_id,
            condition_id=condition_id,
            yes_price=round(yes_price, 4),
            no_price=round(no_price, 4),
            combined=round(yes_price + no_price, 4),
        )
        return pair

    def get_pair(self, pair_id: str) -> HMMPair | None:
        """Get a pair by ID."""
        return self._active_pairs.get(pair_id)

    def get_active_pair_for_market(self, condition_id: str) -> HMMPair | None:
        """Get the active (non-closed, non-cancelled) pair for a market."""
        for pair in self._active_pairs.values():
            if (
                pair.condition_id == condition_id
                and pair.status not in ("closed", "cancelled")
            ):
                return pair
        return None

    def mark_entry_complete(self, pair_id: str) -> None:
        """Transition pair from pending_entry to hedged."""
        pair = self._active_pairs.get(pair_id)
        if pair is not None:
            pair.status = "hedged"
            self._log.info("hmm_pair_hedged", pair_id=pair_id)

    def mark_scalped(self, pair_id: str, side: str, price: float) -> None:
        """Record that one side was scalped."""
        pair = self._active_pairs.get(pair_id)
        if pair is not None:
            pair.scalp_side = side
            pair.scalp_price = price
            pair.status = "scalped"
            self._log.info(
                "hmm_pair_scalped",
                pair_id=pair_id,
                side=side,
                price=round(price, 4),
            )

    def mark_closed(self, pair_id: str) -> None:
        """Mark a pair as fully closed."""
        pair = self._active_pairs.get(pair_id)
        if pair is not None:
            pair.status = "closed"
            cid = pair.condition_id
            count = self._market_pair_count.get(cid, 0)
            if count > 0:
                self._market_pair_count[cid] = count - 1
            self._log.info("hmm_pair_closed", pair_id=pair_id)

    def cancel_pair(self, pair_id: str) -> None:
        """Mark a pair as cancelled."""
        pair = self._active_pairs.get(pair_id)
        if pair is not None:
            pair.status = "cancelled"
            cid = pair.condition_id
            count = self._market_pair_count.get(cid, 0)
            if count > 0:
                self._market_pair_count[cid] = count - 1
            self._log.info("hmm_pair_cancelled", pair_id=pair_id)

    def get_pending_pairs(self) -> list[HMMPair]:
        """Return all pairs still in pending_entry or hedged status."""
        return [
            p for p in self._active_pairs.values()
            if p.status in ("pending_entry", "hedged", "scalped")
        ]

    def cleanup_closed_pairs(self) -> int:
        """Remove closed/cancelled pairs from tracking. Returns count removed."""
        dead = [
            pid for pid, p in self._active_pairs.items()
            if p.status in ("closed", "cancelled")
        ]
        for pid in dead:
            del self._active_pairs[pid]
        return len(dead)

    # -- exit logic -----------------------------------------------------------

    def should_exit(self, position: Position, market: Market) -> bool:
        """HMM manages its own exits via evaluate(). Always return False."""
        return False
