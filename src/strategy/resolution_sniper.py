"""Resolution Sniper strategy for Polymarket.

Enters positions in the last 60-120 seconds of a 15-minute market when the
outcome is near-certain (based on spot price distance from open) but
Polymarket odds have not fully converged.  Holds to resolution — never
exits early.

Uses 3 tranches at T-120s, T-90s, T-60s (each 1/3 of total size), gated
by a minimum win-probability threshold derived from realized volatility
and spot distance.
"""

from __future__ import annotations

import math
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
from src.utils.fees import WINNER_FEE_RATE, taker_fee_amount

# ---------------------------------------------------------------------------
# Normal CDF approximation (Abramowitz & Stegun, max error 7.5e-8)
# ---------------------------------------------------------------------------

def _normal_cdf(x: float) -> float:
    """Approximate the standard normal CDF Φ(x).

    Uses the Abramowitz & Stegun rational approximation (formula 26.2.17)
    for erfc, transformed to the normal CDF.  Max error ~7.5e-8.
    """
    if x < -8.0:
        return 0.0
    if x > 8.0:
        return 1.0

    # Constants for the erfc approximation
    a1 = 0.254829592
    a2 = -0.284496736
    a3 = 1.421413741
    a4 = -1.453152027
    a5 = 1.061405429
    p = 0.3275911

    sign = 1.0
    if x < 0:
        sign = -1.0
        x = -x

    # Transform: Φ(x) = 0.5 * erfc(-x/√2), using erfc approximation
    z = x / math.sqrt(2.0)
    t = 1.0 / (1.0 + p * z)
    y = 1.0 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * math.exp(-z * z)

    return 0.5 * (1.0 + sign * y)


# ---------------------------------------------------------------------------
# Tranche definitions
# ---------------------------------------------------------------------------

# (min_time_remaining, max_time_remaining) — tranche fires when time is
# *between* these bounds.
_TRANCHE_WINDOWS: list[tuple[float, float]] = [
    (90.0, 120.0),   # Tranche 0: T-120s to T-90s
    (60.0, 90.0),    # Tranche 1: T-90s  to T-60s
    (15.0, 60.0),    # Tranche 2: T-60s  to T-15s (hard stop)
]


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------


class ResolutionSniperStrategy(BaseStrategy):
    """Late-game directional strategy that buys near-certain outcomes.

    Enters in 3 tranches during the last 120 seconds of a 15-minute market,
    gated by a minimum win probability (default 90%).  Holds to resolution.
    """

    def __init__(
        self,
        settings: Settings,
        book_manager: OrderBookManager,
        spot_buffer: SpotBuffer,
        alpha_signals: object | None = None,
    ) -> None:
        super().__init__(settings, book_manager)
        self._spot_buffer = spot_buffer
        self._alpha_signals = alpha_signals
        # condition_id -> (open_price, capture_timestamp)
        self._opening_prices: dict[str, tuple[float, float]] = {}
        # condition_id -> set of tranche indices already taken
        self._tranches_taken: dict[str, set[int]] = {}

    @property
    def name(self) -> str:
        return "resolution_sniper"

    @property
    def strategy_type(self) -> StrategyType:
        return StrategyType.RESOLUTION_SNIPER

    def _maybe_invert(
        self, direction: str, market: Market,
    ) -> tuple[str, str]:
        """Sniper trades original direction (follow signal)."""
        if direction == "UP":
            return direction, market.yes_token_id
        return direction, market.no_token_id

    # ------------------------------------------------------------------
    # evaluate
    # ------------------------------------------------------------------

    async def evaluate(self, market: Market) -> Opportunity | None:
        """Evaluate market for a late-game sniper opportunity.

        Steps:
        1. Check time is in sniper window (T-120s to T-15s)
        2. Determine eligible tranche (or return None)
        3. Resolve Binance symbol + check spot data availability
        4. Capture/retrieve opening price
        5. Get current spot price
        6. Compute realized volatility (or return None if insufficient data)
        7. Compute win probability
        8. Gate on confidence >= sniper_min_confidence (0.90)
        9. Determine direction
        10. Check book staleness + get fill estimate
        11. Reject if fill price too high or expected profit <= 0
        12. Mark tranche taken, build and return Opportunity
        """
        now_ts = time.time()
        end_ts = market.end_time.timestamp()
        time_remaining = end_ts - now_ts

        # 1. Time window check
        if time_remaining > self._settings.sniper_window_seconds:
            return None
        if time_remaining < self._settings.sniper_hard_stop_seconds:
            return None

        # 2. Eligible tranche
        tranche_idx = self._get_eligible_tranche(market.condition_id, time_remaining)
        if tranche_idx is None:
            return None

        # 3. Resolve Binance symbol
        binance_symbol = ASSET_TO_BINANCE_SYMBOL.get(market.asset)
        if binance_symbol is None:
            return None

        if not self._spot_buffer.has_data(binance_symbol):
            return None

        # 4. Capture or retrieve opening price (using market open time)
        market_start_ts = market.start_time.timestamp()
        open_price = self._capture_opening_price(
            market.condition_id, binance_symbol, market_start_ts
        )
        if open_price is None:
            return None

        # 5. Current spot price
        current_spot = self._spot_buffer.get_price(binance_symbol)
        if current_spot is None:
            return None

        # 6. Realized volatility
        sigma = self._estimate_realized_vol(binance_symbol)
        if sigma is None:
            return None

        # 7. Win probability
        distance = abs(current_spot - open_price) / open_price
        if distance == 0.0:
            return None  # No movement — no directional edge

        # time_remaining in minutes for vol scaling
        # Apply vol_multiplier to correct for fat tails and mean reversion
        # in short-horizon crypto markets (raw CDF is overconfident)
        vol_mult = self._settings.sniper_vol_multiplier
        expected_move = sigma * vol_mult * math.sqrt(time_remaining / 60.0)
        if expected_move <= 0.0:
            return None

        win_prob = _normal_cdf(distance / expected_move)

        # 8. Confidence gate
        if win_prob < self._settings.sniper_min_confidence:
            self._log.debug(
                "sniper_low_confidence",
                market=market.slug,
                win_prob=round(win_prob, 4),
                threshold=self._settings.sniper_min_confidence,
            )
            return None

        # 9. Direction
        if current_spot > open_price:
            raw_direction = "UP"
        else:
            raw_direction = "DOWN"

        direction, target_token_id = self._maybe_invert(
            raw_direction, market,
        )

        # 9b. Momentum confirmation — reject if recent trend opposes direction
        momentum_window = self._settings.sniper_momentum_window_seconds
        if momentum_window > 0:
            recent = self._spot_buffer.get_price_history(
                binance_symbol, momentum_window
            )
            if len(recent) >= 2:
                first_price = recent[0][1]
                last_price = recent[-1][1]
                if direction == "UP" and last_price < first_price:
                    self._log.debug(
                        "sniper_momentum_reject",
                        market=market.slug,
                        direction=direction,
                        first_price=first_price,
                        last_price=last_price,
                    )
                    return None
                if direction == "DOWN" and last_price > first_price:
                    self._log.debug(
                        "sniper_momentum_reject",
                        market=market.slug,
                        direction=direction,
                        first_price=first_price,
                        last_price=last_price,
                    )
                    return None

        # 10. Book staleness + fill estimate
        if self._is_book_stale(target_token_id):
            self._log.debug("sniper_stale_book", market=market.slug)
            return None

        tranche_size = self._settings.sniper_order_size / 3.0

        # Boost tranche size for high-confidence entries
        if win_prob >= self._settings.sniper_high_confidence_threshold:
            tranche_size *= self._settings.sniper_high_confidence_multiplier

        fill = self._book_manager.get_fill_estimate(
            target_token_id,
            Side.BUY,
            tranche_size,
        )
        if fill is None or not fill.sufficient_liquidity:
            self._log.debug("sniper_insufficient_liquidity", market=market.slug)
            return None

        # 11. Price and profitability checks
        if fill.vwap < self._settings.sniper_min_entry_price:
            self._log.debug(
                "sniper_fill_too_cheap",
                market=market.slug,
                vwap=round(fill.vwap, 4),
                min_price=self._settings.sniper_min_entry_price,
            )
            return None

        if fill.vwap > self._settings.sniper_max_entry_price:
            self._log.debug(
                "sniper_fill_too_expensive",
                market=market.slug,
                vwap=round(fill.vwap, 4),
                max_price=self._settings.sniper_max_entry_price,
            )
            return None

        # Expected profit: win_prob * (1.0 - entry_price) - (1 - win_prob) * entry_price
        # minus taker fee and winner fee
        taker_fee = taker_fee_amount(fill.vwap, tranche_size)
        gross_profit_if_win = (1.0 - fill.vwap) * tranche_size
        winner_fee = WINNER_FEE_RATE * gross_profit_if_win
        expected_profit = (
            win_prob * (gross_profit_if_win - winner_fee)
            - (1.0 - win_prob) * fill.vwap * tranche_size
            - taker_fee
        )

        if expected_profit <= 0:
            self._log.debug(
                "sniper_negative_ev",
                market=market.slug,
                expected_profit=round(expected_profit, 4),
            )
            return None

        profit_pct = expected_profit / (fill.vwap * tranche_size) if fill.vwap > 0 else 0.0

        # 12. Mark tranche taken and build opportunity
        self._mark_tranche_taken(market.condition_id, tranche_idx)

        if direction == "UP":
            yes_fill = fill
            no_fill = None
        else:
            yes_fill = None
            no_fill = fill

        distance_pct = distance * 100.0

        # Alpha signal metadata (observe-only in Phase 1)
        alpha_meta: dict[str, object] = {}
        if self._alpha_signals is not None:
            from src.data.alpha_signals import AlphaSignalProvider

            if isinstance(self._alpha_signals, AlphaSignalProvider):
                snap = self._alpha_signals.get_snapshot(binance_symbol)
                if snap.funding is not None:
                    alpha_meta["alpha_funding_bias"] = snap.funding.bias.value
                    alpha_meta["alpha_funding_rate"] = snap.funding.rate
                if snap.oi is not None:
                    alpha_meta["alpha_oi_trend"] = snap.oi.trend.value
                    alpha_meta["alpha_oi_delta_pct"] = round(snap.oi.delta_pct, 4)
                    alpha_meta["alpha_oi_diverging"] = snap.oi.price_diverging
                if snap.vol is not None:
                    alpha_meta["alpha_vol_regime"] = snap.vol.regime.value
                    alpha_meta["alpha_vol_sigma"] = round(snap.vol.realized_vol, 8)

        # KL divergence scoring (optional)
        kl_meta: dict[str, float] = {}
        if self._settings.enable_divergence_scoring:
            from src.utils.divergence import market_mispricing_score

            sniper_yes_book = self._book_manager.get_book(market.yes_token_id)
            sniper_no_book = self._book_manager.get_book(market.no_token_id)
            if sniper_yes_book and sniper_no_book:
                s_yes_ask = sniper_yes_book.best_ask
                s_no_ask = sniper_no_book.best_ask
                if s_yes_ask is not None and s_no_ask is not None:
                    kl_data = market_mispricing_score(s_yes_ask, s_no_ask)
                    kl_meta = {f"kl_{k}": v for k, v in kl_data.items()}

        opp = Opportunity(
            strategy=self.strategy_type,
            market=market,
            timestamp=datetime.now(tz=UTC),
            yes_fill=yes_fill,
            no_fill=no_fill,
            expected_profit=expected_profit,
            expected_profit_pct=profit_pct,
            total_fees=taker_fee + winner_fee,
            confidence=win_prob,
            requested_size=tranche_size,
            metadata={
                "direction": direction,
                "target_token_id": target_token_id,
                "binance_symbol": binance_symbol,
                "open_price": open_price,
                "current_spot": current_spot,
                "distance_pct": round(distance_pct, 4),
                "sigma": round(sigma, 8),
                "win_probability": round(win_prob, 6),
                "tranche_index": tranche_idx,
                "tranche_size": tranche_size,
                "time_remaining": round(time_remaining, 1),
                **kl_meta,
                **alpha_meta,
            },
        )

        self._log.info(
            "sniper_opportunity",
            market=market.slug,
            direction=direction,
            win_prob=round(win_prob, 4),
            distance_pct=round(distance_pct, 3),
            sigma=round(sigma, 6),
            tranche=tranche_idx,
            expected_profit=round(expected_profit, 4),
            time_remaining=round(time_remaining, 1),
        )

        return opp

    # ------------------------------------------------------------------
    # should_exit
    # ------------------------------------------------------------------

    def should_exit(self, position: Position, market: Market) -> bool:
        """Almost always returns False — hold to resolution by design.

        Optional emergency exit: if sniper_exit_confidence_floor > 0 and
        current win probability drops below it, exit.  Disabled by default
        (floor=0.0).
        """
        floor = self._settings.sniper_exit_confidence_floor
        if floor <= 0.0:
            return False

        # Recompute win probability
        binance_symbol = ASSET_TO_BINANCE_SYMBOL.get(market.asset)
        if binance_symbol is None:
            return False

        open_entry = self._opening_prices.get(market.condition_id)
        if open_entry is None:
            return False
        open_price = open_entry[0]

        current_spot = self._spot_buffer.get_price(binance_symbol)
        if current_spot is None:
            return False

        sigma = self._estimate_realized_vol(binance_symbol)
        if sigma is None:
            return False

        end_ts = market.end_time.timestamp()
        time_remaining = end_ts - time.time()
        if time_remaining <= 0:
            return False

        distance = abs(current_spot - open_price) / open_price
        vol_mult = self._settings.sniper_vol_multiplier
        expected_move = sigma * vol_mult * math.sqrt(time_remaining / 60.0)
        if expected_move <= 0:
            return False

        win_prob = _normal_cdf(distance / expected_move)

        if win_prob < floor:
            self._log.info(
                "sniper_emergency_exit",
                market=market.slug,
                win_prob=round(win_prob, 4),
                floor=floor,
            )
            return True

        return False

    # ------------------------------------------------------------------
    # cleanup
    # ------------------------------------------------------------------

    def cleanup_market(self, condition_id: str) -> None:
        """Remove tracking state for an expired/rolled-over market."""
        self._opening_prices.pop(condition_id, None)
        self._tranches_taken.pop(condition_id, None)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _capture_opening_price(
        self, condition_id: str, binance_symbol: str, market_start_ts: float
    ) -> float | None:
        """Return the spot price closest to market open; cache for future calls.

        Uses the spot buffer's full history and picks the earliest price
        that falls within the market's lifetime, giving a much more
        accurate baseline than the first price seen at T-120s.
        """
        entry = self._opening_prices.get(condition_id)
        if entry is not None:
            return entry[0]

        # Pull full buffer history and find the price closest to market open
        history = self._spot_buffer.get_price_history(binance_symbol)
        if not history:
            return None

        # Find the entry closest to market_start_ts
        best_price: float | None = None
        best_diff = float("inf")
        for ts, price in history:
            diff = abs(ts - market_start_ts)
            if diff < best_diff:
                best_diff = diff
                best_price = price

        if best_price is None:
            return None

        self._opening_prices[condition_id] = (best_price, time.time())
        return best_price

    def _estimate_realized_vol(self, binance_symbol: str) -> float | None:
        """Compute realized 1-minute volatility from spot buffer history.

        Returns sigma (stddev of log-returns scaled to 1-minute) or None
        if insufficient data.
        """
        history = self._spot_buffer.get_price_history(
            binance_symbol, self._settings.sniper_vol_window_seconds
        )
        if len(history) < self._settings.sniper_min_vol_data_points:
            return None

        # Compute log-returns
        log_returns: list[float] = []
        for i in range(1, len(history)):
            prev_price = history[i - 1][1]
            curr_price = history[i][1]
            if prev_price > 0:
                log_returns.append(math.log(curr_price / prev_price))

        if not log_returns:
            return None

        # Standard deviation of log-returns
        n = len(log_returns)
        mean = sum(log_returns) / n
        variance = sum((r - mean) ** 2 for r in log_returns) / n
        stddev = math.sqrt(variance)

        # Scale to per-minute: estimate ticks per minute from timestamps
        total_time = history[-1][0] - history[0][0]
        if total_time <= 0:
            return max(stddev, self._settings.sniper_vol_floor)

        ticks_per_minute = (len(history) - 1) / (total_time / 60.0)
        sigma = stddev * math.sqrt(max(ticks_per_minute, 1.0))

        return max(sigma, self._settings.sniper_vol_floor)

    def _get_eligible_tranche(
        self, condition_id: str, time_remaining: float
    ) -> int | None:
        """Return the tranche index eligible at this time, or None."""
        taken = self._tranches_taken.get(condition_id, set())
        for idx, (min_t, max_t) in enumerate(_TRANCHE_WINDOWS):
            if min_t < time_remaining <= max_t and idx not in taken:
                return idx
        return None

    def _mark_tranche_taken(self, condition_id: str, tranche_idx: int) -> None:
        """Optimistically mark a tranche as taken."""
        if condition_id not in self._tranches_taken:
            self._tranches_taken[condition_id] = set()
        self._tranches_taken[condition_id].add(tranche_idx)
