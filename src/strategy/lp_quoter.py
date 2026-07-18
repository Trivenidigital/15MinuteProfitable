"""Liquidity Rewards Quoter — maker-side rewards farming for Polymarket.

Continuously rests two-sided BUY limit orders (a YES bid and a NO bid)
near the midpoint of 15-minute crypto markets. Revenue comes from three
maker-side sources, none of which pay taker fees:

1. **Liquidity rewards** — Polymarket samples the book every minute and
   pays a daily pool share to makers with competitive resting orders
   (closer to midpoint and two-sided = higher score).
2. **Spread capture** — a YES bid at ``mid - offset`` plus a NO bid at
   ``(1 - mid) - offset`` costs ``1 - 2*offset`` combined if both fill,
   and the merged pair pays exactly $1.00 at resolution.
3. **Maker rebates** — a share of taker fees paid when our resting
   orders are filled.

Why this is NOT a BaseStrategy: the evaluate()->Opportunity pipeline is
built for point-in-time entries. A quoter's job is continuous
reconciliation (place / hold / cancel-replace) of resting orders, so it
runs as its own loop, like ``_rollover_loop``. Fills are still recorded
through ``StateManager.record_trade`` so the existing resolution loop
handles P&L and the risk manager sees exposure.

Adverse-selection guards (the reason naive MM loses on 15-min markets):

* all quotes pulled at T-``lp_quote_stop_seconds`` before window end;
* quotes pulled when spot moves faster than ``lp_spot_guard_threshold``
  over ``lp_spot_guard_window`` seconds;
* no quoting when the YES mid is outside [``lp_min_mid``, ``lp_max_mid``];
* per-market net-inventory cap suppresses the accumulating side;
* circuit breaker active -> all quotes pulled.

Rate-limit math (order placement cap is 60/min per API key): with the
default 6s refresh, 1 asset and 2 sides, the worst case of a full
cancel+replace every cycle is 20 placements + 20 cancels per minute —
comfortably inside the 60 orders/min and 200 cancels/min limits.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from src.config import Settings
from src.core.models import (
    Market,
    Opportunity,
    OrderStatus,
    Side,
    StrategyType,
    TradeOrder,
)
from src.core.state import StateManager
from src.data.market_manager import MarketManager
from src.data.orderbook import OrderBookManager
from src.data.spot_buffer import SpotBuffer
from src.execution.executor import OrderExecutor
from src.monitoring.logger import get_logger
from src.risk.manager import RiskManager
from src.utils.time_utils import time_remaining_seconds

_ASSET_TO_SYMBOL = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
    "XRP": "XRPUSDT",
}


@dataclass
class RestingQuote:
    """One resting BUY limit order managed by the quoter."""

    token_side: str  # "YES" or "NO" (which token we are bidding on)
    token_id: str
    price: float
    size: float
    order: TradeOrder
    placed_at: float = field(default_factory=time.time)
    recorded_fill: float = 0.0  # shares already recorded via record_trade


@dataclass
class MarketQuotes:
    """Quoter state for a single market window."""

    market: Market
    yes_quote: RestingQuote | None = None
    no_quote: RestingQuote | None = None
    yes_filled_shares: float = 0.0
    no_filled_shares: float = 0.0

    @property
    def net_inventory(self) -> float:
        """Net directional exposure in shares (positive = long YES)."""
        return self.yes_filled_shares - self.no_filled_shares


class LiquidityQuoter:
    """Continuous two-sided maker quoting engine.

    Owns its resting orders end-to-end: placement, repricing, fill
    detection, inventory caps and shutdown cancellation.
    """

    def __init__(
        self,
        settings: Settings,
        book_manager: OrderBookManager,
        market_manager: MarketManager,
        spot_buffer: SpotBuffer,
        executor: OrderExecutor,
        state_manager: StateManager,
        risk_manager: RiskManager,
        trade_db: object | None = None,
    ) -> None:
        self._settings = settings
        self._books = book_manager
        self._markets = market_manager
        self._spot = spot_buffer
        self._executor = executor
        self._state = state_manager
        self._risk = risk_manager
        self._trade_db = trade_db
        self._log = get_logger("lp_quoter")

        self._quotes: dict[str, MarketQuotes] = {}  # condition_id -> state
        self._assets = {a.strip().upper() for a in settings.lp_assets.split(",") if a.strip()}
        self._last_sample_ts = 0.0

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Reconcile quotes forever; cancel all resting orders on exit."""
        self._log.info(
            "lp_quoter_started",
            assets=sorted(self._assets),
            order_size=self._settings.lp_order_size,
            offset=self._settings.lp_quote_offset,
            refresh_s=self._settings.lp_refresh_seconds,
        )
        try:
            while True:
                try:
                    await self._reconcile_cycle()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._log.error("lp_cycle_error", error=str(exc))
                await asyncio.sleep(self._settings.lp_refresh_seconds)
        except asyncio.CancelledError:
            await self.cancel_all_quotes()
            raise

    async def _reconcile_cycle(self) -> None:
        """One reconciliation pass over all active markets."""
        active = {
            m.condition_id: m
            for m in self._markets.active_markets
            if m.asset.upper() in self._assets
        }

        # Cancel quotes for markets that rolled over / disappeared
        for cid in list(self._quotes):
            if cid not in active:
                await self._cancel_market_quotes(cid, reason="market_gone")
                del self._quotes[cid]

        # Detect fills first so inventory caps use fresh numbers
        for cid in list(self._quotes):
            await self._detect_fills(self._quotes[cid])

        for cid, market in active.items():
            mq = self._quotes.setdefault(cid, MarketQuotes(market=market))
            mq.market = market
            await self._reconcile_market(mq)

        # Periodic rewards Q-score sampling
        now = time.time()
        if now - self._last_sample_ts >= self._settings.lp_sample_interval:
            self._last_sample_ts = now
            self._sample_reward_scores()

    # ------------------------------------------------------------------
    # Per-market reconciliation
    # ------------------------------------------------------------------

    async def _reconcile_market(self, mq: MarketQuotes) -> None:
        market = mq.market
        s = self._settings

        # Guard 1: circuit breaker
        if self._risk.is_circuit_breaker_active():
            await self._cancel_market_quotes(market.condition_id, reason="circuit_breaker")
            return

        # Guard 2: too close to resolution (adverse selection is deadly late)
        remaining = time_remaining_seconds(market.end_time.timestamp())
        if remaining <= s.lp_quote_stop_seconds:
            await self._cancel_market_quotes(market.condition_id, reason="quote_stop_window")
            return

        # Guard 3: stale books
        if self._books.is_stale(market.yes_token_id) or self._books.is_stale(market.no_token_id):
            await self._cancel_market_quotes(market.condition_id, reason="stale_book")
            return

        # Guard 4: spot velocity — pull quotes during fast moves
        if self._spot_moving_fast(market.asset):
            await self._cancel_market_quotes(market.condition_id, reason="spot_guard")
            return

        mid = self._yes_midpoint(market)
        if mid is None:
            await self._cancel_market_quotes(market.condition_id, reason="no_book")
            return

        # Guard 5: extreme odds — thin rewards, fat inventory risk
        if mid < s.lp_min_mid or mid > s.lp_max_mid:
            await self._cancel_market_quotes(market.condition_id, reason="extreme_mid")
            return

        # Desired quotes (bid YES below mid; bid NO below complement-mid)
        yes_price = self._clamp_price(round(mid - s.lp_quote_offset, 2))
        no_price = self._clamp_price(round((1.0 - mid) - s.lp_quote_offset, 2))

        # Inventory skew: suppress the side that would grow net exposure
        want_yes = mq.net_inventory < s.lp_max_inventory_shares
        want_no = -mq.net_inventory < s.lp_max_inventory_shares

        mq.yes_quote = await self._reconcile_side(
            mq,
            "YES",
            market.yes_token_id,
            yes_price if want_yes else None,
            mq.yes_quote,
        )
        mq.no_quote = await self._reconcile_side(
            mq,
            "NO",
            market.no_token_id,
            no_price if want_no else None,
            mq.no_quote,
        )

    async def _reconcile_side(
        self,
        mq: MarketQuotes,
        token_side: str,
        token_id: str,
        desired_price: float | None,
        current: RestingQuote | None,
    ) -> RestingQuote | None:
        """Bring one side's resting order in line with the desired price.

        ``desired_price=None`` means this side should not be quoted.
        """
        s = self._settings

        # Side suppressed -> cancel any resting order
        if desired_price is None:
            if current is not None:
                await self._cancel_quote(current, reason="side_suppressed")
            return None

        # Order already resting close enough -> keep it
        if current is not None:
            if abs(current.price - desired_price) < s.lp_reprice_tolerance:
                return current
            await self._cancel_quote(current, reason="reprice")

        return await self._place_quote(mq, token_side, token_id, desired_price)

    async def _place_quote(
        self,
        mq: MarketQuotes,
        token_side: str,
        token_id: str,
        price: float,
    ) -> RestingQuote | None:
        """Sign and submit a resting GTC BUY at *price*."""
        size = self._settings.lp_order_size
        order = TradeOrder(
            token_id=token_id,
            side=Side.BUY,
            price=price,
            size=size,
            order_type="GTC",
        )

        if self._settings.dry_run:
            # Executor dry-run instantly "fills" GTC orders, which is wrong
            # for resting quotes — simulate the resting book entry locally.
            order.order_id = f"lp_dry_{int(time.time() * 1000)}_{token_side}"
            order.status = OrderStatus.SUBMITTED
        else:
            order = await self._executor.sign_order(order)
            if order.status != OrderStatus.SIGNED:
                self._log.warning(
                    "lp_sign_failed",
                    market=mq.market.slug,
                    side=token_side,
                )
                return None
            order = await self._executor.submit_order(order)
            if order.status not in (OrderStatus.SUBMITTED, OrderStatus.FILLED):
                self._log.warning(
                    "lp_submit_failed",
                    market=mq.market.slug,
                    side=token_side,
                    status=order.status.value,
                )
                return None

        quote = RestingQuote(
            token_side=token_side,
            token_id=token_id,
            price=price,
            size=size,
            order=order,
        )
        self._log.info(
            "lp_quote_placed",
            market=mq.market.slug,
            side=token_side,
            price=price,
            size=size,
            order_id=order.order_id or "",
        )
        return quote

    async def _cancel_quote(self, quote: RestingQuote, reason: str) -> None:
        """Cancel one resting order (best-effort)."""
        if quote.order.order_id and not self._settings.dry_run:
            await self._executor.cancel_order(quote.order.order_id)
        self._log.info(
            "lp_quote_cancelled",
            side=quote.token_side,
            price=quote.price,
            reason=reason,
        )

    async def _cancel_market_quotes(self, condition_id: str, reason: str) -> None:
        """Cancel both sides for a market."""
        mq = self._quotes.get(condition_id)
        if mq is None:
            return
        if mq.yes_quote is not None:
            await self._cancel_quote(mq.yes_quote, reason)
            mq.yes_quote = None
        if mq.no_quote is not None:
            await self._cancel_quote(mq.no_quote, reason)
            mq.no_quote = None

    async def cancel_all_quotes(self) -> None:
        """Cancel every resting quote (shutdown path)."""
        for cid in list(self._quotes):
            await self._cancel_market_quotes(cid, reason="shutdown")
        self._log.info("lp_all_quotes_cancelled")

    # ------------------------------------------------------------------
    # Fill detection
    # ------------------------------------------------------------------

    async def _detect_fills(self, mq: MarketQuotes) -> None:
        """Poll (live) or simulate (dry-run) fills on resting quotes."""
        for attr in ("yes_quote", "no_quote"):
            quote: RestingQuote | None = getattr(mq, attr)
            if quote is None:
                continue

            if self._settings.dry_run:
                filled = self._simulate_fill(quote)
            else:
                await self._executor.check_order_status(quote.order)
                filled = quote.order.fill_size > quote.recorded_fill

            if filled:
                await self._record_fill(mq, quote)

            # Fully filled or externally cancelled -> stop tracking it
            if quote.order.status in (OrderStatus.FILLED, OrderStatus.CANCELLED):
                setattr(mq, attr, None)

    def _simulate_fill(self, quote: RestingQuote) -> bool:
        """Dry-run: our bid fills when the best ask crosses down into it."""
        book = self._books.get_book(quote.token_id)
        if book is None or book.best_ask is None:
            return False
        if book.best_ask <= quote.price:
            quote.order.fill_size = quote.size
            quote.order.fill_price = quote.price
            quote.order.status = OrderStatus.FILLED
            return True
        return False

    async def _record_fill(self, mq: MarketQuotes, quote: RestingQuote) -> None:
        """Record the incremental fill into StateManager (and thus the DB)."""
        delta = quote.order.fill_size - quote.recorded_fill
        if delta <= 0:
            return
        quote.recorded_fill = quote.order.fill_size

        if quote.token_side == "YES":
            mq.yes_filled_shares += delta
        else:
            mq.no_filled_shares += delta

        fill_order = TradeOrder(
            token_id=quote.token_id,
            side=Side.BUY,
            price=quote.price,
            size=delta,
            order_type="GTC",
            order_id=quote.order.order_id,
            status=OrderStatus.FILLED,
            fill_size=delta,
            fill_price=quote.order.fill_price or quote.price,
        )
        opp = Opportunity(
            strategy=StrategyType.LP_QUOTER,
            market=mq.market,
            timestamp=datetime.now(tz=UTC),
            expected_profit=0.0,
            total_fees=0.0,  # maker fee = 0%
            confidence=1.0,
            requested_size=delta,
            metadata={
                "order_type": "GTC",
                "lp_quote": True,
                "token_side": quote.token_side,
                "net_inventory": mq.net_inventory,
            },
        )
        await self._state.record_trade(opp, [fill_order])
        self._log.info(
            "lp_fill_recorded",
            market=mq.market.slug,
            side=quote.token_side,
            price=quote.price,
            shares=round(delta, 2),
            net_inventory=round(mq.net_inventory, 2),
        )

    # ------------------------------------------------------------------
    # Rewards Q-score sampling
    # ------------------------------------------------------------------

    def _sample_reward_scores(self) -> None:
        """Estimate our per-minute liquidity-rewards score per market.

        Mirrors Polymarket's published scoring shape: each qualifying
        resting order scores ``((max_spread - dist) / max_spread)^2 * size``
        where *dist* is its distance from the YES midpoint (a NO bid at p
        is a YES ask at 1-p). Two-sided books score
        ``max(min(Qbid, Qask), max(Qbid, Qask) / divisor)`` inside the
        0.10-0.90 mid band, and ``min(Qbid, Qask)`` outside it.

        This is a *relative* score — actual dollars depend on competing
        makers' scores, so we persist raw Q for later reconciliation
        against real reward payouts.
        """
        if self._trade_db is None:
            return
        from src.data.trade_db import LPRewardSample

        s = self._settings
        max_spread = s.lp_rewards_max_spread

        for mq in self._quotes.values():
            if mq.yes_quote is None and mq.no_quote is None:
                continue
            mid = self._yes_midpoint(mq.market)
            if mid is None:
                continue

            q_bid = 0.0
            q_ask = 0.0
            if mq.yes_quote is not None and mq.yes_quote.size >= s.lp_rewards_min_size:
                dist = abs(mid - mq.yes_quote.price)
                if dist <= max_spread:
                    q_bid = ((max_spread - dist) / max_spread) ** 2 * mq.yes_quote.size
            if mq.no_quote is not None and mq.no_quote.size >= s.lp_rewards_min_size:
                # NO bid at p == YES ask at (1 - p)
                dist = abs((1.0 - mq.no_quote.price) - mid)
                if dist <= max_spread:
                    q_ask = ((max_spread - dist) / max_spread) ** 2 * mq.no_quote.size

            if 0.10 <= mid <= 0.90:
                q_score = max(
                    min(q_bid, q_ask),
                    max(q_bid, q_ask) / s.lp_rewards_two_sided_divisor,
                )
            else:
                q_score = min(q_bid, q_ask)

            try:
                self._trade_db.save_lp_reward_sample(  # type: ignore[attr-defined]
                    LPRewardSample(
                        timestamp=time.time(),
                        condition_id=mq.market.condition_id,
                        asset=mq.market.asset,
                        market_slug=mq.market.slug,
                        midpoint=mid,
                        q_bid=q_bid,
                        q_ask=q_ask,
                        q_score=q_score,
                        yes_bid_price=(mq.yes_quote.price if mq.yes_quote is not None else 0.0),
                        no_bid_price=(mq.no_quote.price if mq.no_quote is not None else 0.0),
                        dry_run=self._settings.dry_run,
                    )
                )
            except Exception as exc:
                self._log.warning("lp_sample_save_failed", error=str(exc))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _yes_midpoint(self, market: Market) -> float | None:
        """Midpoint of the YES token book, or None if unavailable."""
        book = self._books.get_book(market.yes_token_id)
        if book is None or book.best_bid is None or book.best_ask is None:
            return None
        return (book.best_bid + book.best_ask) / 2.0

    def _spot_moving_fast(self, asset: str) -> bool:
        """True when spot moved more than the guard threshold recently."""
        symbol = _ASSET_TO_SYMBOL.get(asset.upper())
        if symbol is None:
            return False
        s = self._settings
        movement = self._spot.detect_movement(
            symbol, s.lp_spot_guard_window, s.lp_spot_guard_threshold
        )
        return movement is not None

    @staticmethod
    def _clamp_price(price: float) -> float:
        """Clamp to Polymarket's valid [0.01, 0.99] price range."""
        return max(0.01, min(0.99, price))
