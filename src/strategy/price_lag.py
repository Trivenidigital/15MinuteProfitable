"""Price-lag exploitation strategy for Polymarket.

Detects when Binance spot prices move faster than Polymarket odds update,
and enters directionally before odds catch up.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

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
from src.utils.fees import taker_fee_amount


# Map asset codes to Binance symbol pairs
ASSET_TO_BINANCE_SYMBOL = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
    "XRP": "XRPUSDT",
}


class PriceLagStrategy(BaseStrategy):
    """Directional strategy based on spot price leading Polymarket odds.

    Detects significant spot movements on Binance and compares the
    implied direction to current Polymarket YES/NO prices. If the odds
    haven't caught up, enters directionally.
    """

    def __init__(
        self,
        settings: Settings,
        book_manager: OrderBookManager,
        spot_buffer: SpotBuffer,
    ) -> None:
        super().__init__(settings, book_manager)
        self._spot_buffer = spot_buffer
        self._consecutive_signals: dict[str, int] = {}  # condition_id -> count
        self._last_signal_direction: dict[str, str] = {}  # condition_id -> "UP"/"DOWN"
        self._stop_loss_counts: dict[str, int] = {}  # condition_id -> consecutive trigger count

    @property
    def name(self) -> str:
        return "price_lag"

    @property
    def strategy_type(self) -> StrategyType:
        return StrategyType.PRICE_LAG

    async def evaluate(self, market: Market) -> Opportunity | None:
        """Evaluate market for price-lag opportunity.

        Steps:
        1. Check dead zone (too close to start or end)
        2. Check spot data availability for this asset
        3. Detect spot movement exceeding threshold
        4. Require N consecutive confirmations in same direction
        5. Get Polymarket YES price (best ask)
        6. Calculate implied odds discrepancy
        7. Check if discrepancy exceeds odds_lag_threshold
        8. Get fill estimate for the directional side
        9. Apply time-aware sizing multiplier
        10. Build and return Opportunity
        """
        # Staleness check
        if self._is_book_stale(market.yes_token_id) or self._is_book_stale(market.no_token_id):
            self._log.debug("stale_orderbook", market=market.slug)
            return None

        # 1. Dead zone check (custom for price-lag: wider zones)
        now_ts = time.time()
        start_ts = market.start_time.timestamp()
        end_ts = market.end_time.timestamp()
        time_since_open = now_ts - start_ts
        time_to_close = end_ts - now_ts

        if time_since_open < self._settings.lag_entry_dead_zone_start:
            self._log.debug("lag_dead_zone_start", market=market.slug)
            return None

        if time_to_close < self._settings.lag_entry_dead_zone_end:
            self._log.debug("lag_dead_zone_end", market=market.slug)
            return None

        # 2. Spot data availability
        binance_symbol = ASSET_TO_BINANCE_SYMBOL.get(market.asset)
        if binance_symbol is None:
            return None

        if not self._spot_buffer.has_data(binance_symbol):
            self._log.debug("no_spot_data", market=market.slug, symbol=binance_symbol)
            return None

        # 3. Detect spot movement
        movement = self._spot_buffer.detect_movement(
            symbol=binance_symbol,
            window_seconds=self._settings.spot_window_seconds,
            threshold=self._settings.spot_move_threshold,
        )

        if movement is None:
            # Reset consecutive signals on no movement
            self._consecutive_signals.pop(market.condition_id, None)
            self._last_signal_direction.pop(market.condition_id, None)
            return None

        # 4. Consecutive confirmations
        last_dir = self._last_signal_direction.get(market.condition_id)
        if last_dir == movement.direction:
            self._consecutive_signals[market.condition_id] = (
                self._consecutive_signals.get(market.condition_id, 0) + 1
            )
        else:
            self._consecutive_signals[market.condition_id] = 1
            self._last_signal_direction[market.condition_id] = movement.direction

        confirmations = self._consecutive_signals[market.condition_id]
        if confirmations < self._settings.lag_confirmations:
            self._log.debug(
                "awaiting_confirmation",
                market=market.slug,
                direction=movement.direction,
                confirmations=confirmations,
                required=self._settings.lag_confirmations,
            )
            return None

        # 5. Get Polymarket YES price (best ask = price to buy YES)
        yes_book = self._book_manager.get_book(market.yes_token_id)
        no_book = self._book_manager.get_book(market.no_token_id)

        if yes_book is None or no_book is None:
            self._log.debug("missing_books", market=market.slug)
            return None

        yes_ask = yes_book.best_ask
        no_ask = no_book.best_ask

        if yes_ask is None or no_ask is None:
            return None

        # 6. Calculate odds discrepancy
        # If spot moved UP, YES should be higher. The "fair" YES price
        # should have moved up but hasn't yet (lagging).
        # We measure: how much has the YES price NOT moved relative to
        # what the spot move implies.
        #
        # Simple approach: spot moved UP -> YES is currently "cheap"
        # if yes_ask is still low relative to what it should be.
        # We use odds_lag_threshold as the minimum price discrepancy.

        if movement.direction == "UP":
            # Spot says price going up -> want to buy YES
            # The opportunity exists if YES is still cheap
            target_token_id = market.yes_token_id
            current_price = yes_ask
            # The lag = how far YES is from reflecting the upward move
            # For a significant up move, YES should be higher
            odds_lag = max(0, 0.5 + movement.change_pct * 10 - current_price)
        else:
            # Spot says price going down -> want to buy NO
            target_token_id = market.no_token_id
            current_price = no_ask
            odds_lag = max(0, 0.5 + movement.change_pct * 10 - current_price)

        # 7. Check discrepancy threshold
        if odds_lag < self._settings.odds_lag_threshold:
            self._log.debug(
                "insufficient_lag",
                market=market.slug,
                direction=movement.direction,
                odds_lag=round(odds_lag, 4),
                threshold=self._settings.odds_lag_threshold,
            )
            return None

        # 8. Get fill estimate
        size = self._settings.order_size
        # Apply time-aware sizing
        sizing_multiplier = self._time_aware_sizing(time_to_close)
        adjusted_size = size * sizing_multiplier

        if adjusted_size <= 0:
            return None

        fill = self._book_manager.get_fill_estimate(
            target_token_id,
            Side.BUY,
            adjusted_size,
        )

        if fill is None or not fill.sufficient_liquidity:
            self._log.debug("insufficient_liquidity", market=market.slug)
            return None

        # 9. Calculate expected profit
        # For directional trades, expected profit depends on the odds moving
        # to reflect the spot movement. Estimate conservatively.
        taker_fee = taker_fee_amount(fill.vwap, adjusted_size)
        # Expected outcome: the token price moves toward the implied fair value
        expected_move = odds_lag * 0.5  # conservative: capture half the lag
        expected_profit = expected_move * adjusted_size - taker_fee
        profit_pct = (
            expected_profit / (fill.vwap * adjusted_size) if fill.vwap > 0 else 0.0
        )

        # Confidence based on spot movement strength and confirmations
        confidence = min(
            1.0,
            (movement.change_pct / self._settings.spot_move_threshold) * 0.5
            + (confirmations / (self._settings.lag_confirmations * 2)) * 0.5,
        )

        # 10. Build opportunity
        # Store fill in yes_fill or no_fill depending on direction
        if movement.direction == "UP":
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
            total_fees=taker_fee,
            confidence=confidence,
            metadata={
                "direction": movement.direction,
                "spot_change_pct": movement.change_pct,
                "odds_lag": odds_lag,
                "current_price": current_price,
                "target_token_id": target_token_id,
                "sizing_multiplier": sizing_multiplier,
                "confirmations": confirmations,
                "binance_symbol": binance_symbol,
            },
        )

        self._log.info(
            "lag_opportunity_found",
            market=market.slug,
            direction=movement.direction,
            spot_change=round(movement.change_pct * 100, 3),
            odds_lag=round(odds_lag, 4),
            expected_profit=round(expected_profit, 4),
            size=adjusted_size,
        )

        return opp

    def should_exit(self, position: Position, market: Market) -> bool:
        """Check if a price-lag position should be exited.

        Exit conditions:
        1. Time-based exit: within time_exit_seconds of market close
           (skipped for near-worthless positions — let them expire as lottery tickets)
        2. Stop-loss (smart): cheap contract bypass, time-decayed threshold,
           confirmation counter
        3. Take-profit: current value exceeds take_profit_pct above cost basis
        """
        now_ts = time.time()
        start_ts = market.start_time.timestamp()
        end_ts = market.end_time.timestamp()
        time_to_close = end_ts - now_ts
        cid = market.condition_id

        # Compute current value upfront (needed by time exit and stop-loss)
        cost_basis = position.total_investment
        if cost_basis <= 0:
            return False

        current_value = 0.0
        total_shares = position.yes_shares + position.no_shares

        if position.yes_shares > 0:
            yes_book = self._book_manager.get_book(market.yes_token_id)
            if yes_book and yes_book.best_bid is not None:
                current_value += position.yes_shares * yes_book.best_bid

        if position.no_shares > 0:
            no_book = self._book_manager.get_book(market.no_token_id)
            if no_book and no_book.best_bid is not None:
                current_value += position.no_shares * no_book.best_bid

        if current_value <= 0:
            return False

        pnl_pct = (current_value - cost_basis) / cost_basis

        # 1. Time-based exit — but skip for near-worthless positions.
        # When a position has lost >95% of its value, the salvage value from
        # selling is negligible. Better to let it expire: the max additional
        # downside is the salvage amount, but the upside is full recovery if
        # the market resolves favorably.
        if time_to_close <= self._settings.time_exit_seconds:
            value_ratio = current_value / cost_basis
            if value_ratio < 0.05:
                self._log.info(
                    "time_exit_skipped_lottery",
                    market=market.slug,
                    time_to_close=round(time_to_close, 1),
                    current_value=round(current_value, 2),
                    cost_basis=round(cost_basis, 2),
                    value_ratio=round(value_ratio, 4),
                )
                self._stop_loss_counts.pop(cid, None)
                return False
            self._log.info(
                "time_exit", market=market.slug, time_to_close=round(time_to_close, 1)
            )
            self._stop_loss_counts.pop(cid, None)
            return True

        # 2. Smart stop-loss
        stop_loss_exit = self._check_smart_stop_loss(
            pnl_pct=pnl_pct,
            cost_basis=cost_basis,
            total_shares=total_shares,
            now_ts=now_ts,
            start_ts=start_ts,
            end_ts=end_ts,
            market=market,
        )
        if stop_loss_exit:
            self._stop_loss_counts.pop(cid, None)
            return True

        # 4. Take-profit
        if pnl_pct >= self._settings.take_profit_pct:
            self._log.info(
                "take_profit_triggered",
                market=market.slug,
                pnl_pct=round(pnl_pct * 100, 2),
            )
            self._stop_loss_counts.pop(cid, None)
            return True

        return False

    def _check_smart_stop_loss(
        self,
        pnl_pct: float,
        cost_basis: float,
        total_shares: float,
        now_ts: float,
        start_ts: float,
        end_ts: float,
        market: Market,
    ) -> bool:
        """Evaluate smart stop-loss with cheap bypass, time decay, and confirmation.

        Returns True only when a confirmed stop-loss exit should occur.
        """
        cid = market.condition_id

        # Step A: Cheap contract bypass
        if total_shares > 0:
            avg_entry_price = cost_basis / total_shares
            if avg_entry_price < self._settings.stop_loss_cheap_threshold:
                self._log.debug(
                    "stop_loss_skipped_cheap",
                    market=market.slug,
                    avg_entry_price=round(avg_entry_price, 4),
                )
                self._stop_loss_counts.pop(cid, None)
                return False

        # Step B: Time-decayed threshold
        duration = end_ts - start_ts
        if duration > 0:
            elapsed = now_ts - start_ts
            progress = elapsed / duration  # 0.0 -> 1.0
        else:
            progress = 1.0

        if self._settings.stop_loss_time_decay:
            if progress >= 2 / 3:
                # Last third: disable stop-loss entirely
                self._log.debug(
                    "stop_loss_disabled_late_market",
                    market=market.slug,
                    progress=round(progress, 2),
                )
                self._stop_loss_counts.pop(cid, None)
                return False
            elif progress >= 1 / 3:
                # Middle third: double the threshold (wider)
                effective_threshold = self._settings.stop_loss_pct * 2
            else:
                # First third: use as-is
                effective_threshold = self._settings.stop_loss_pct
        else:
            effective_threshold = self._settings.stop_loss_pct

        # Check if loss exceeds effective threshold
        if pnl_pct <= -effective_threshold:
            # Step C: Confirmation counter
            count = self._stop_loss_counts.get(cid, 0) + 1
            self._stop_loss_counts[cid] = count

            if count >= self._settings.stop_loss_confirmations:
                self._log.info(
                    "stop_loss_triggered",
                    market=market.slug,
                    pnl_pct=round(pnl_pct * 100, 2),
                    confirmations=count,
                    effective_threshold=round(effective_threshold * 100, 2),
                )
                return True

            self._log.debug(
                "stop_loss_pending_confirmation",
                market=market.slug,
                pnl_pct=round(pnl_pct * 100, 2),
                confirmations=count,
                required=self._settings.stop_loss_confirmations,
            )
            return False

        # Not in stop-loss territory — reset counter
        self._stop_loss_counts.pop(cid, None)
        return False

    def cleanup_market(self, condition_id: str) -> None:
        """Remove tracking data for an expired market."""
        self._consecutive_signals.pop(condition_id, None)
        self._last_signal_direction.pop(condition_id, None)
        self._stop_loss_counts.pop(condition_id, None)

    def _time_aware_sizing(self, time_to_close: float) -> float:
        """Return sizing multiplier based on time remaining.

        >5min: 1.0 (full size)
        2-5min: 0.5 (half size)
        30s-2min: 0.25 (quarter size)
        <30s: 0.0 (do not trade)
        """
        if time_to_close > 300:  # > 5 minutes
            return 1.0
        elif time_to_close > 120:  # 2-5 minutes
            return 0.5
        elif time_to_close > 30:  # 30s - 2 minutes
            return 0.25
        else:  # < 30 seconds
            return 0.0
