"""Fade Panic strategy for Polymarket.

In the last 120 seconds of a market, detects when Polymarket odds have moved
sharply (>8% shift) but the actual spot price hasn't moved proportionally.
This means someone is panic-buying/selling on the market, creating a
mispricing. We fade (bet against) the panic.

Holds to resolution — never exits early (same as resolution sniper).
"""

from __future__ import annotations

import time
from collections import deque
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


class FadePanicStrategy(BaseStrategy):
    """Late-game strategy that fades panic-driven odds movements.

    Active only in the last 120 seconds (configurable) of a market.
    Detects when Polymarket YES odds shift >8% but spot price barely
    moved, indicating a panic mispricing rather than a fundamental move.
    """

    def __init__(
        self,
        settings: Settings,
        book_manager: OrderBookManager,
        spot_buffer: SpotBuffer,
    ) -> None:
        super().__init__(settings, book_manager)
        self._spot_buffer = spot_buffer
        # condition_id -> deque of (timestamp, yes_ask_price)
        self._odds_history: dict[str, deque[tuple[float, float]]] = {}
        # condition_id -> (open_price, capture_timestamp)
        self._window_open_prices: dict[str, tuple[float, float]] = {}

    @property
    def name(self) -> str:
        return "fade_panic"

    @property
    def strategy_type(self) -> StrategyType:
        return StrategyType.FADE_PANIC

    async def evaluate(self, market: Market) -> Opportunity | None:
        """Evaluate market for a panic-fading opportunity.

        Steps:
        1. Check we're in the late-game window (T-120s to T-15s)
        2. Record current YES ask in odds history
        3. Check for significant odds shift in the measurement window
        4. Compare odds shift to actual spot price change
        5. If odds moved but spot didn't → mispricing → fade the panic
        6. Get fill estimate, compute expected profit, build opportunity
        """
        now_ts = time.time()
        end_ts = market.end_time.timestamp()
        time_remaining = end_ts - now_ts

        # 1. Late-game window check
        if time_remaining > self._settings.fade_panic_window_seconds:
            return None
        if time_remaining < self._settings.fade_panic_hard_stop_seconds:
            return None

        # Staleness check
        if self._is_book_stale(market.yes_token_id) or self._is_book_stale(
            market.no_token_id
        ):
            return None

        # 2. Record current YES ask price
        yes_book = self._book_manager.get_book(market.yes_token_id)
        if yes_book is None or yes_book.best_ask is None:
            return None

        current_yes_ask = yes_book.best_ask
        cid = market.condition_id

        if cid not in self._odds_history:
            self._odds_history[cid] = deque(maxlen=500)

        self._odds_history[cid].append((now_ts, current_yes_ask))

        # Prune old entries beyond the measurement window
        odds_window = self._settings.fade_panic_odds_window_seconds
        cutoff = now_ts - odds_window
        while self._odds_history[cid] and self._odds_history[cid][0][0] < cutoff:
            self._odds_history[cid].popleft()

        # 3. Check for significant odds shift
        history = self._odds_history[cid]
        if len(history) < 2:
            return None

        oldest_odds = history[0][1]
        newest_odds = history[-1][1]
        odds_shift = newest_odds - oldest_odds  # positive = YES went up

        if abs(odds_shift) < self._settings.fade_panic_odds_shift_threshold:
            return None

        # 4. Compare to spot price change in the same window
        binance_symbol = ASSET_TO_BINANCE_SYMBOL.get(market.asset)
        if binance_symbol is None:
            return None

        if not self._spot_buffer.has_data(binance_symbol):
            return None

        spot_history = self._spot_buffer.get_price_history(
            binance_symbol, odds_window
        )
        if len(spot_history) < 2:
            return None

        spot_start = spot_history[0][1]
        spot_end = spot_history[-1][1]
        if spot_start <= 0:
            return None

        spot_change_pct = abs(spot_end - spot_start) / spot_start

        # If spot also moved significantly, it's NOT panic — it's fundamental
        if spot_change_pct > self._settings.fade_panic_spot_max_change:
            self._log.debug(
                "fade_panic_spot_moved",
                market=market.slug,
                spot_change_pct=round(spot_change_pct * 100, 4),
                max_allowed=round(self._settings.fade_panic_spot_max_change * 100, 4),
            )
            return None

        # 4a2. Check spot vs window open price — catches mean-reverting spikes
        #       that the 60s rolling window misses
        spot_vs_open_pct: float | None = None
        market_start_ts = market.start_time.timestamp()
        window_open_price = self._capture_window_open_price(
            market.condition_id, binance_symbol, market_start_ts
        )
        if window_open_price is not None and window_open_price > 0:
            spot_vs_open_pct = abs(spot_end - window_open_price) / window_open_price
            if spot_vs_open_pct > self._settings.fade_panic_spot_vs_open_max_change:
                self._log.info(
                    "fade_panic_spot_moved_from_open",
                    market=market.slug,
                    spot_vs_open_pct=round(spot_vs_open_pct * 100, 4),
                    max_allowed=round(
                        self._settings.fade_panic_spot_vs_open_max_change * 100, 4
                    ),
                    window_open_price=round(window_open_price, 4),
                    current_spot=round(spot_end, 4),
                )
                return None

        # 4b. Cross-validate Binance spot with Chainlink oracle
        oracle_price: float | None = None
        oracle_divergence: float | None = None
        if self._settings.enable_chainlink_filter:
            from src.utils.chainlink import validate_spot_price

            is_valid, oracle_price, oracle_divergence = await validate_spot_price(
                asset=market.asset,
                binance_price=spot_end,
                max_divergence_pct=self._settings.chainlink_max_divergence_pct,
                rpc_url=self._settings.chainlink_rpc_url,
            )
            if not is_valid:
                self._log.info(
                    "fade_panic_oracle_rejected",
                    market=market.slug,
                    binance_price=round(spot_end, 2),
                    oracle_price=round(oracle_price, 2) if oracle_price else None,
                    divergence_pct=round(oracle_divergence * 100, 4) if oracle_divergence else None,
                )
                return None

        # 5. Mispricing detected — fade the panic
        # If YES odds spiked UP (someone panic-bought YES), buy NO
        # If YES odds crashed DOWN (someone panic-sold YES), buy YES
        if odds_shift > 0:
            raw_direction = "DOWN"
        else:
            raw_direction = "UP"

        direction, target_token_id = self._maybe_invert(
            raw_direction, market,
        )

        # 6. Get fill estimate
        size = self._settings.fade_panic_order_size

        fill = self._book_manager.get_fill_estimate(
            target_token_id,
            Side.BUY,
            size,
        )
        if fill is None or not fill.sufficient_liquidity:
            self._log.debug("fade_panic_insufficient_liquidity", market=market.slug)
            return None

        # Reject if fill price too cheap (lottery tickets)
        if fill.vwap < self._settings.min_entry_price:
            self._log.debug("fade_panic_price_floor_rejected", market=market.slug, vwap=round(fill.vwap, 4))
            return None

        # Reject if fill price too expensive
        if fill.vwap > self._settings.fade_panic_max_entry_price:
            self._log.debug(
                "fade_panic_too_expensive",
                market=market.slug,
                vwap=round(fill.vwap, 4),
                max_price=self._settings.fade_panic_max_entry_price,
            )
            return None

        # Expected profit calculation (hold to resolution)
        # The thesis: the panic odds are wrong, the "true" odds are closer
        # to what they were before the panic spike.
        # Conservative estimate: 50% of the odds shift reverts by resolution
        reversion_estimate = abs(odds_shift) * 0.5

        taker_fee = taker_fee_amount(fill.vwap, size)
        # If we're right, our token resolves to $1
        gross_profit_if_win = (1.0 - fill.vwap) * size
        winner_fee = WINNER_FEE_RATE * gross_profit_if_win

        # Win probability: based on how much the odds are "wrong"
        # Higher odds shift with no spot move = higher confidence
        win_prob = min(0.95, 0.5 + reversion_estimate)

        expected_profit = (
            win_prob * (gross_profit_if_win - winner_fee)
            - (1.0 - win_prob) * fill.vwap * size
            - taker_fee
        )

        if expected_profit <= 0:
            self._log.debug(
                "fade_panic_negative_ev",
                market=market.slug,
                expected_profit=round(expected_profit, 4),
            )
            return None

        profit_pct = (
            expected_profit / (fill.vwap * size) if fill.vwap > 0 else 0.0
        )

        confidence = win_prob

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

            no_book = self._book_manager.get_book(market.no_token_id)
            no_ask = no_book.best_ask if no_book else None
            if no_ask is not None:
                kl_meta = {
                    f"kl_{k}": v
                    for k, v in market_mispricing_score(current_yes_ask, no_ask).items()
                }

        opp = Opportunity(
            strategy=self.strategy_type,
            market=market,
            timestamp=datetime.now(tz=UTC),
            yes_fill=yes_fill,
            no_fill=no_fill,
            expected_profit=expected_profit,
            expected_profit_pct=profit_pct,
            total_fees=taker_fee + winner_fee,
            confidence=confidence,
            requested_size=size,
            metadata={
                "direction": direction,
                "target_token_id": target_token_id,
                "odds_shift": round(odds_shift, 4),
                "spot_change_pct": round(spot_change_pct, 6),
                **(
                    {"spot_vs_open_pct": round(spot_vs_open_pct, 6)}
                    if spot_vs_open_pct is not None
                    else {}
                ),
                "win_probability": round(win_prob, 4),
                "time_remaining": round(time_remaining, 1),
                "binance_symbol": binance_symbol,
                **({"oracle_price": round(oracle_price, 2)} if oracle_price else {}),
                **({"oracle_divergence_pct": round(oracle_divergence * 100, 4)} if oracle_divergence is not None else {}),
                **kl_meta,
            },
        )

        self._log.info(
            "fade_panic_opportunity",
            market=market.slug,
            direction=direction,
            odds_shift=round(odds_shift * 100, 2),
            spot_change_pct=round(spot_change_pct * 100, 4),
            win_prob=round(win_prob, 4),
            expected_profit=round(expected_profit, 4),
            time_remaining=round(time_remaining, 1),
        )

        return opp

    def should_exit(self, position: Position, market: Market) -> bool:
        """Hold to resolution — never exit early.

        Fade Panic positions are late-game bets held to resolution.
        """
        return False

    def cleanup_market(self, condition_id: str) -> None:
        """Remove odds tracking state for an expired market."""
        self._odds_history.pop(condition_id, None)
        self._window_open_prices.pop(condition_id, None)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _capture_window_open_price(
        self, condition_id: str, binance_symbol: str, market_start_ts: float
    ) -> float | None:
        """Return the spot price closest to the market's 15-min window open.

        Caches the result so subsequent calls within the same window reuse it.
        Mirrors ResolutionSniperStrategy._capture_opening_price().
        """
        entry = self._window_open_prices.get(condition_id)
        if entry is not None:
            return entry[0]

        history = self._spot_buffer.get_price_history(binance_symbol)
        if not history:
            return None

        best_price: float | None = None
        best_diff = float("inf")
        for ts, price in history:
            diff = abs(ts - market_start_ts)
            if diff < best_diff:
                best_diff = diff
                best_price = price

        if best_price is None:
            return None

        self._window_open_prices[condition_id] = (best_price, time.time())
        return best_price
