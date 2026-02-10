"""Dip Buyer / Mean Reversion strategy for Polymarket.

Detects sharp spot price spikes or crashes (>0.15% in 3-5 seconds) that are
likely noise/overreactions, and bets against them expecting mean reversion.

Signal logic:
1. Detect sharp move exceeding dip_spot_threshold in a short window.
2. Confirm it's an outlier (>2x the rolling 60s average move).
3. Bet AGAINST the spike: spike UP → buy NO, crash DOWN → buy YES.
4. Use tight stop-loss and time exit (60s before close).
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
from src.utils.fees import taker_fee_amount


class DipBuyerStrategy(BaseStrategy):
    """Mean reversion strategy that fades sharp spot price spikes.

    Enters directionally against short-lived spot anomalies, expecting
    the price to revert to its recent mean.
    """

    def __init__(
        self,
        settings: Settings,
        book_manager: OrderBookManager,
        spot_buffer: SpotBuffer,
    ) -> None:
        super().__init__(settings, book_manager)
        self._spot_buffer = spot_buffer
        # Divergence exit tracking
        self._entry_kl: dict[str, float] = {}  # condition_id -> entry KL divergence
        self._entry_direction: dict[str, str] = {}  # condition_id -> "UP"/"DOWN"

    @property
    def name(self) -> str:
        return "dip_buyer"

    @property
    def strategy_type(self) -> StrategyType:
        return StrategyType.DIP_BUYER

    def _maybe_invert(
        self, direction: str, market: Market,
    ) -> tuple[str, str]:
        """Dip buyer always trades original direction (no inversion)."""
        if direction == "UP":
            return direction, market.yes_token_id
        return direction, market.no_token_id

    async def evaluate(self, market: Market) -> Opportunity | None:
        """Evaluate market for a mean-reversion dip opportunity.

        Steps:
        1. Check orderbook staleness
        2. Resolve Binance symbol + check spot data
        3. Detect sharp move in short window (dip_spot_window_seconds)
        4. Compare to rolling average move over longer window (dip_mean_window_seconds)
        5. If outlier ratio met, bet against the spike
        6. Get fill estimate and build opportunity
        """
        # 1. Staleness check
        if self._is_book_stale(market.yes_token_id) or self._is_book_stale(
            market.no_token_id
        ):
            return None

        # Time check — don't trade too close to expiry
        now_ts = time.time()
        end_ts = market.end_time.timestamp()
        time_to_close = end_ts - now_ts
        if time_to_close < self._settings.dip_time_exit_seconds + 30:
            return None  # too close to expiry for a mean-reversion play

        # 2. Resolve Binance symbol
        binance_symbol = ASSET_TO_BINANCE_SYMBOL.get(market.asset)
        if binance_symbol is None:
            return None

        if not self._spot_buffer.has_data(binance_symbol):
            return None

        # 3. Detect sharp move in short window
        short_movement = self._spot_buffer.detect_movement(
            symbol=binance_symbol,
            window_seconds=self._settings.dip_spot_window_seconds,
            threshold=self._settings.dip_spot_threshold,
        )
        if short_movement is None:
            return None

        # 4. Compare to rolling average move over longer window
        mean_history = self._spot_buffer.get_price_history(
            binance_symbol, self._settings.dip_mean_window_seconds
        )
        if len(mean_history) < 10:
            return None  # not enough data

        # Compute average absolute move between consecutive ticks
        total_abs_change = 0.0
        count = 0
        for i in range(1, len(mean_history)):
            prev_price = mean_history[i - 1][1]
            curr_price = mean_history[i][1]
            if prev_price > 0:
                total_abs_change += abs(curr_price - prev_price) / prev_price
                count += 1

        if count == 0:
            return None

        avg_move = total_abs_change / count

        # Scale to same window size for comparison
        # The short move is over dip_spot_window_seconds, avg_move is per-tick.
        # Estimate ticks in the short window.
        total_time = mean_history[-1][0] - mean_history[0][0]
        if total_time <= 0:
            return None
        ticks_per_second = (len(mean_history) - 1) / total_time
        ticks_in_short_window = ticks_per_second * self._settings.dip_spot_window_seconds
        expected_window_move = avg_move * max(ticks_in_short_window, 1.0)

        if expected_window_move <= 0:
            return None

        outlier_ratio = short_movement.change_pct / expected_window_move
        if outlier_ratio < self._settings.dip_outlier_ratio:
            self._log.debug(
                "dip_not_outlier",
                market=market.slug,
                outlier_ratio=round(outlier_ratio, 2),
                required=self._settings.dip_outlier_ratio,
            )
            return None

        # 5. Bet AGAINST the spike
        # Spike UP → buy NO (expect reversion DOWN)
        # Crash DOWN → buy YES (expect reversion UP)
        if short_movement.direction == "UP":
            raw_direction = "DOWN"
        else:
            raw_direction = "UP"

        direction, target_token_id = self._maybe_invert(
            raw_direction, market,
        )

        # 6. Get fill estimate
        size = self._settings.dip_order_size

        fill = self._book_manager.get_fill_estimate(
            target_token_id,
            Side.BUY,
            size,
        )
        if fill is None or not fill.sufficient_liquidity:
            self._log.debug("dip_insufficient_liquidity", market=market.slug)
            return None

        if fill.vwap < self._settings.min_entry_price:
            self._log.debug("dip_price_floor_rejected", market=market.slug, vwap=round(fill.vwap, 4))
            return None

        # Expected profit: conservative — capture half the spike as reversion
        taker_fee = taker_fee_amount(fill.vwap, size)
        expected_reversion = short_movement.change_pct * 0.3  # 30% reversion
        expected_profit = expected_reversion * size - taker_fee
        profit_pct = (
            expected_profit / (fill.vwap * size) if fill.vwap > 0 else 0.0
        )

        if expected_profit <= 0:
            return None

        # Confidence: based on outlier ratio strength
        confidence = min(1.0, outlier_ratio / (self._settings.dip_outlier_ratio * 2))

        if direction == "UP":
            yes_fill = fill
            no_fill = None
        else:
            yes_fill = None
            no_fill = fill

        # KL divergence scoring (optional)
        kl_meta: dict[str, float] = {}
        if self._settings.enable_divergence_scoring:
            from src.utils.divergence import market_mispricing_score

            yes_book = self._book_manager.get_book(market.yes_token_id)
            no_book = self._book_manager.get_book(market.no_token_id)
            if yes_book and no_book and yes_book.best_ask and no_book.best_ask:
                kl_meta = {
                    f"kl_{k}": v
                    for k, v in market_mispricing_score(yes_book.best_ask, no_book.best_ask).items()
                }

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
                "spike_direction": short_movement.direction,
                "spike_change_pct": short_movement.change_pct,
                "outlier_ratio": round(outlier_ratio, 2),
                "binance_symbol": binance_symbol,
                **kl_meta,
            },
        )

        # Track entry KL for divergence exit signals
        if self._settings.divergence_exit_signals and kl_meta:
            entry_kl = kl_meta.get("kl_kl_divergence", 0.0)
            if isinstance(entry_kl, (int, float)) and entry_kl > 0:
                self._entry_kl[market.condition_id] = entry_kl
                self._entry_direction[market.condition_id] = direction

        self._log.info(
            "dip_opportunity_found",
            market=market.slug,
            spike_dir=short_movement.direction,
            bet_dir=direction,
            spike_pct=round(short_movement.change_pct * 100, 3),
            outlier_ratio=round(outlier_ratio, 2),
            expected_profit=round(expected_profit, 4),
            size=size,
        )

        return opp

    def should_exit(self, position: Position, market: Market) -> bool:
        """Check if a dip-buyer position should be exited.

        Exit conditions:
        1. Time exit: within dip_time_exit_seconds of market close
        2. Stop-loss: position loses more than dip_stop_loss_pct
        3. Take-profit: position gains more than dip_take_profit_pct
        """
        now_ts = time.time()
        end_ts = market.end_time.timestamp()
        time_to_close = end_ts - now_ts

        cost_basis = position.total_investment
        if cost_basis <= 0:
            return False

        # Skip early exit for small positions — hold to resolution
        if cost_basis < 10.0:
            return False

        # Compute current value
        current_value = 0.0
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

        # 1. Time exit — but skip if position has lost >70%
        if time_to_close <= self._settings.dip_time_exit_seconds:
            value_ratio = current_value / cost_basis
            if value_ratio < 0.30:
                self._log.info(
                    "dip_time_exit_skipped_heavy_loss",
                    market=market.slug,
                    value_ratio=round(value_ratio, 4),
                    current_value=round(current_value, 2),
                    cost_basis=round(cost_basis, 2),
                )
                return False
            self._log.info(
                "dip_time_exit",
                market=market.slug,
                time_to_close=round(time_to_close, 1),
            )
            return True

        # 2. Stop-loss — skip for cheap tokens (single tick = huge % swing)
        avg_price = cost_basis / max(position.yes_shares + position.no_shares, 1.0)
        if avg_price >= self._settings.stop_loss_cheap_threshold:
            if pnl_pct <= -self._settings.dip_stop_loss_pct:
                self._log.info(
                    "dip_stop_loss",
                    market=market.slug,
                    pnl_pct=round(pnl_pct * 100, 2),
                )
                return True

        # 3. Take-profit
        if pnl_pct >= self._settings.dip_take_profit_pct:
            self._log.info(
                "dip_take_profit",
                market=market.slug,
                pnl_pct=round(pnl_pct * 100, 2),
            )
            return True

        # 4. Divergence-based exit: KL dropped below ratio of entry KL
        cid = market.condition_id
        if self._settings.divergence_exit_signals and cid in self._entry_kl:
            d_yes_book = self._book_manager.get_book(market.yes_token_id)
            d_no_book = self._book_manager.get_book(market.no_token_id)
            if d_yes_book and d_no_book and d_yes_book.best_ask and d_no_book.best_ask:
                from src.utils.divergence import divergence_exit_signal, market_mispricing_score

                current_kl_data = market_mispricing_score(d_yes_book.best_ask, d_no_book.best_ask)
                current_kl = current_kl_data["kl_divergence"]
                entry_dir = self._entry_direction.get(cid, "UP")
                if divergence_exit_signal(
                    self._entry_kl[cid],
                    current_kl,
                    entry_dir,
                    threshold_ratio=self._settings.divergence_exit_threshold,
                ):
                    self._log.info(
                        "dip_divergence_exit",
                        market=market.slug,
                        entry_kl=round(self._entry_kl[cid], 6),
                        current_kl=round(current_kl, 6),
                        threshold=self._settings.divergence_exit_threshold,
                    )
                    return True

        return False
