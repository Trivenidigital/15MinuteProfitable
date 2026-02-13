"""Pre-trade risk checks and exposure monitoring for the Polymarket trading bot."""

from __future__ import annotations

import json
import time
from typing import Protocol

from src.config import Settings
from src.core.models import DailyPnL, Opportunity, StrategyType
from src.monitoring.logger import get_logger
from src.utils.time_utils import is_in_dead_zone, time_remaining_seconds


# ---------------------------------------------------------------------------
# Protocol for decoupled state access
# ---------------------------------------------------------------------------


class StateProvider(Protocol):
    """Duck-typed interface for querying positions and P&L.

    Any object implementing these four methods (e.g. ``StateManager``) can be
    passed to :class:`RiskManager` without a hard import dependency.
    """

    def total_exposure(self) -> float: ...

    def market_exposure(self, condition_id: str) -> float: ...

    def total_unhedged_exposure(self) -> float: ...

    def daily_pnl(self) -> DailyPnL: ...

    def position_entry_count(self, condition_id: str) -> int: ...

    def strategy_entry_count(self, condition_id: str, strategy: str) -> int: ...


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_HEDGED_STRATEGIES: frozenset[StrategyType] = frozenset({
    StrategyType.ARBITRAGE,
    StrategyType.HEDGED_MM,
})

# Late-game strategies that deliberately trade near market close.
# They have their own hard-stop logic, so skip dead-zone and
# time-remaining risk checks for them.
_LATE_GAME_STRATEGIES: frozenset[StrategyType] = frozenset({
    StrategyType.RESOLUTION_SNIPER,
    StrategyType.FADE_PANIC,
})

# Minimum trade size in shares.  Sizes below this are rejected to prevent
# dust trades caused by floating-point capacity drift.
_MIN_TRADE_SIZE: float = 1.0

_MIN_TIME_REMAINING: float = 30.0  # seconds

_CIRCUIT_BREAKER_FAILURE_THRESHOLD: int = 3

_DEFAULT_CIRCUIT_BREAKER_DURATION: float = 300.0  # 5 minutes

_DAILY_LOSS_BREAKER_DURATION: float = 86400.0  # 24 hours

_DISCONNECT_THRESHOLD: float = 30.0  # seconds


# ---------------------------------------------------------------------------
# RiskManager
# ---------------------------------------------------------------------------


class RiskManager:
    """Pre-trade risk checks and exposure monitoring."""

    def __init__(self, settings: Settings, state: StateProvider) -> None:
        self._settings = settings
        self._state = state
        self._log = get_logger("risk")

        # Circuit breaker state
        self._circuit_breaker_active = False
        self._circuit_breaker_reason = ""
        self._circuit_breaker_until: float = 0.0

        # Execution tracking
        self._consecutive_failures = 0
        self._last_trade_time: dict[str, float] = {}  # condition_id -> timestamp

        # Optional Kelly sizer
        self._sizer: object | None = None

        # Optional persistence
        self._trade_db: object | None = None

        # Optional orderbook manager for mark-to-market
        self._book_manager: object | None = None

        # Per-strategy consecutive loss cooldown
        self._strategy_consecutive_losses: dict[str, int] = {}
        self._strategy_cooldown_until: dict[str, float] = {}

        # Exit-blocked market tracking
        self._exit_blocked_markets: set[str] = set()
        self._exit_rejection_counts: dict[str, int] = {}
        self._exit_retry_after: dict[str, float] = {}

    def set_trade_db(self, trade_db: object) -> None:
        """Attach a TradeDatabase for persisting circuit breaker state."""
        self._trade_db = trade_db

    def set_book_manager(self, bm: object) -> None:
        """Attach an OrderBookManager for mark-to-market valuation."""
        self._book_manager = bm
        self._log.info("book_manager_attached")

    def unrealized_loss(self) -> float:
        """Compute total unrealized loss from open positions marked to market.

        Returns a negative number (loss) or 0.0 if no unrealized loss.
        """
        if self._book_manager is None:
            return 0.0

        total_loss = 0.0
        try:
            positions = self._state.get_all_positions()  # type: ignore[attr-defined]
        except AttributeError:
            return 0.0

        for pos in positions:
            current_value = 0.0
            if pos.yes_shares > 0:
                book = self._book_manager.get_book(pos.market.yes_token_id)  # type: ignore[attr-defined]
                if book and book.best_bid is not None:
                    current_value += pos.yes_shares * book.best_bid
            if pos.no_shares > 0:
                book = self._book_manager.get_book(pos.market.no_token_id)  # type: ignore[attr-defined]
                if book and book.best_bid is not None:
                    current_value += pos.no_shares * book.best_bid

            pnl = current_value - pos.total_investment
            if pnl < 0:
                total_loss += pnl

        return total_loss

    # ------------------------------------------------------------------
    # Pre-trade checks
    # ------------------------------------------------------------------

    def check_opportunity(self, opp: Opportunity) -> tuple[bool, str]:
        """Run all pre-trade checks.  Returns ``(approved, reason)``.

        Checks are evaluated in order; the first failure short-circuits and
        returns the rejection reason.

        Checks:
            1. Circuit breaker not active
            2. Daily loss limit not exceeded
            3. Market exposure < max_position_per_market
            4. Total exposure < max_total_position
            5. Unhedged exposure < max_unhedged_exposure (non-hedged strategies only)
            6. Market not in dead zone
            7. Cooldown period elapsed since last trade on this market
            8. Time remaining > 30 seconds
        """
        condition_id = opp.market.condition_id

        # 0. Exit-blocked market — never add exposure if exits are failing
        if condition_id in self._exit_blocked_markets:
            reason = (
                f"exit orders failing "
                f"({self._exit_rejection_counts.get(condition_id, 0)} rejections)"
            )
            self._log.warning("risk_rejected", check="exit_blocked", reason=reason)
            return False, reason

        # Snapshot state values for consistent reads within this check
        _market_exp = self._state.market_exposure(condition_id)
        _total_exp = self._state.total_exposure()
        _unhedged_exp = self._state.total_unhedged_exposure()
        _entry_count = self._state.position_entry_count(condition_id)
        _strat_count = self._state.strategy_entry_count(condition_id, opp.strategy.value)

        # 1. Circuit breaker (skipped in DRY_RUN or when explicitly disabled)
        skip_breaker = self._settings.dry_run or self._settings.disable_circuit_breaker
        if not skip_breaker:
            if self.is_circuit_breaker_active():
                reason = f"circuit breaker active: {self._circuit_breaker_reason}"
                self._log.warning("risk_rejected", check="circuit_breaker", reason=reason)
                return False, reason

            # 1b. Per-strategy cooldown
            if self.is_strategy_cooling_down(opp.strategy):
                remaining = self._strategy_cooldown_until.get(opp.strategy.value, 0.0) - time.time()
                reason = (
                    f"strategy cooldown active: {opp.strategy.value} "
                    f"({remaining:.0f}s remaining)"
                )
                self._log.warning("risk_rejected", check="strategy_cooldown", reason=reason)
                return False, reason

            # 2. Daily loss limit — also auto-trips circuit breaker for 24h
            pnl = self._state.daily_pnl()
            if pnl.net_profit < -self._settings.max_daily_loss:
                reason = (
                    f"daily loss limit exceeded: net_profit={pnl.net_profit:.2f}, "
                    f"limit=-{self._settings.max_daily_loss:.2f}"
                )
                self._log.warning("risk_rejected", check="daily_loss", reason=reason)
                if not self._circuit_breaker_active:
                    self.activate_circuit_breaker(
                        reason=f"daily loss limit: {pnl.net_profit:.2f}",
                        duration_seconds=_DAILY_LOSS_BREAKER_DURATION,
                    )
                return False, reason

            # 2b. Unrealized loss check
            if self._book_manager is not None:
                unreal_loss = self.unrealized_loss()
                if abs(unreal_loss) > 0.8 * self._settings.max_daily_loss:
                    reason = (
                        f"unrealized loss near daily limit: {unreal_loss:.2f}, "
                        f"threshold=-{0.8 * self._settings.max_daily_loss:.2f}"
                    )
                    self._log.warning("risk_rejected", check="unrealized_loss", reason=reason)
                    return False, reason

        # 3. Market exposure
        if _market_exp >= self._settings.max_position_per_market:
            reason = (
                f"market exposure limit reached: {_market_exp:.2f} >= "
                f"{self._settings.max_position_per_market:.2f}"
            )
            self._log.warning("risk_rejected", check="market_exposure", reason=reason)
            return False, reason

        # 3b. Max entries per market
        max_entries = self._settings.max_entries_per_market
        if _entry_count >= max_entries:
            reason = (
                f"max entries reached: {_entry_count} >= "
                f"{max_entries}"
            )
            self._log.warning("risk_rejected", check="max_entries", reason=reason)
            return False, reason

        # 3c. Max entries per strategy per market
        max_per_strat = self._settings.max_entries_per_strategy_per_market
        if _strat_count >= max_per_strat:
            reason = (
                f"max entries per strategy reached: {_strat_count} >= "
                f"{max_per_strat} ({opp.strategy.value})"
            )
            self._log.warning("risk_rejected", check="max_entries_per_strategy", reason=reason)
            return False, reason

        # 4. Total exposure
        if _total_exp >= self._settings.max_total_position:
            reason = (
                f"total exposure limit reached: {_total_exp:.2f} >= "
                f"{self._settings.max_total_position:.2f}"
            )
            self._log.warning("risk_rejected", check="total_exposure", reason=reason)
            return False, reason

        # 5. Unhedged exposure (skip for hedged strategies like arbitrage)
        if opp.strategy not in _HEDGED_STRATEGIES:
            if _unhedged_exp >= self._settings.max_unhedged_exposure:
                reason = (
                    f"unhedged exposure limit reached: {_unhedged_exp:.2f} >= "
                    f"{self._settings.max_unhedged_exposure:.2f}"
                )
                self._log.warning("risk_rejected", check="unhedged_exposure", reason=reason)
                return False, reason

        # 6. Dead zone (skip for late-game strategies — they trade near close by design)
        start_ts = opp.market.start_time.timestamp()
        end_ts = opp.market.end_time.timestamp()
        if opp.strategy not in _LATE_GAME_STRATEGIES:
            if is_in_dead_zone(start_ts, end_ts):
                reason = "market is in dead zone (too close to start or end)"
                self._log.warning("risk_rejected", check="dead_zone", reason=reason)
                return False, reason

        # 7. Cooldown
        last_trade = self._last_trade_time.get(condition_id, 0.0)
        elapsed = time.time() - last_trade
        if elapsed < self._settings.cooldown_seconds:
            reason = (
                f"cooldown not elapsed: {elapsed:.1f}s < "
                f"{self._settings.cooldown_seconds:.1f}s"
            )
            self._log.warning("risk_rejected", check="cooldown", reason=reason)
            return False, reason

        # 8. Time remaining (skip for late-game strategies — they have their own hard stops)
        remaining = time_remaining_seconds(end_ts)
        if opp.strategy not in _LATE_GAME_STRATEGIES:
            if remaining <= _MIN_TIME_REMAINING:
                reason = (
                    f"insufficient time remaining: {remaining:.1f}s "
                    f"<= {_MIN_TIME_REMAINING:.1f}s"
                )
                self._log.warning("risk_rejected", check="time_remaining", reason=reason)
                return False, reason

        self._log.debug(
            "risk_approved",
            condition_id=condition_id,
            strategy=opp.strategy.value,
        )
        return True, "approved"

    # ------------------------------------------------------------------
    # Size adjustment
    # ------------------------------------------------------------------

    def adjust_size(
        self,
        opp: Opportunity,
        requested_size: float,
        *,
        cached_market_exp: float | None = None,
        cached_total_exp: float | None = None,
        cached_unhedged_exp: float | None = None,
    ) -> float:
        """Adjust order size to stay within risk limits.

        Returns the minimum of:
          - *requested_size*
          - remaining capacity for this market
          - remaining total capacity
          - remaining unhedged capacity (for directional / non-hedged strategies)

        Optional cached_* kwargs avoid redundant state queries when called
        from ``check_opportunity`` which already snapshotted the values.

        Returns ``0.0`` if no capacity remains.
        """
        condition_id = opp.market.condition_id

        market_exp = (
            cached_market_exp if cached_market_exp is not None
            else self._state.market_exposure(condition_id)
        )
        total_exp = (
            cached_total_exp if cached_total_exp is not None
            else self._state.total_exposure()
        )

        # Market capacity
        market_remaining = self._settings.max_position_per_market - market_exp
        if market_remaining <= 0:
            return 0.0

        # Total capacity
        total_remaining = self._settings.max_total_position - total_exp
        if total_remaining <= 0:
            return 0.0

        size = min(requested_size, market_remaining, total_remaining)

        # Unhedged capacity (only constrain non-hedged strategies)
        if opp.strategy not in _HEDGED_STRATEGIES:
            unhedged_exp = (
                cached_unhedged_exp if cached_unhedged_exp is not None
                else self._state.total_unhedged_exposure()
            )
            unhedged_remaining = self._settings.max_unhedged_exposure - unhedged_exp
            if unhedged_remaining <= 0:
                return 0.0
            size = min(size, unhedged_remaining)

        size = max(size, 0.0)

        # Reject dust trades — sizes below the minimum are not worth executing
        # and can arise from floating-point drift in capacity calculations.
        if size < _MIN_TRADE_SIZE:
            self._log.warning(
                "size_below_minimum",
                requested=round(size, 2),
                minimum=_MIN_TRADE_SIZE,
                strategy=opp.strategy.value,
                market=opp.market.slug,
            )
            return 0.0

        return size

    # ------------------------------------------------------------------
    # Execution recording
    # ------------------------------------------------------------------

    def record_execution_success(self, condition_id: str) -> None:
        """Record a successful execution.

        Resets the consecutive failure counter and updates the last trade
        timestamp for cooldown tracking.
        """
        self._consecutive_failures = 0
        self._last_trade_time[condition_id] = time.time()
        self._log.debug("execution_success", condition_id=condition_id)

    def record_execution_failure(self) -> None:
        """Record an execution failure.

        Increments the consecutive failure counter.  If the counter reaches
        the threshold (3), the circuit breaker is automatically activated
        for the default duration (5 minutes).
        """
        self._consecutive_failures += 1
        self._log.warning(
            "execution_failure",
            consecutive_failures=self._consecutive_failures,
        )
        if self._consecutive_failures >= _CIRCUIT_BREAKER_FAILURE_THRESHOLD:
            self.activate_circuit_breaker(
                reason=f"{self._consecutive_failures} consecutive failures",
                duration_seconds=_DEFAULT_CIRCUIT_BREAKER_DURATION,
            )

    # ------------------------------------------------------------------
    # Exit-blocked market tracking
    # ------------------------------------------------------------------

    _EXIT_BACKOFF_BASE: float = 4.0
    _EXIT_BACKOFF_CAP: float = 60.0

    def mark_exit_blocked(self, condition_id: str) -> int:
        """Flag market as exit-blocked, increment rejection count, set backoff timer.

        Returns the new rejection count.
        """
        self._exit_blocked_markets.add(condition_id)
        count = self._exit_rejection_counts.get(condition_id, 0) + 1
        self._exit_rejection_counts[condition_id] = count

        # Exponential backoff: 4s → 8s → 16s → 32s → 60s (capped)
        delay = min(self._EXIT_BACKOFF_BASE * (2 ** (count - 1)), self._EXIT_BACKOFF_CAP)
        self._exit_retry_after[condition_id] = time.time() + delay

        self._log.warning(
            "exit_blocked",
            condition_id=condition_id,
            rejection_count=count,
            retry_after_seconds=delay,
        )
        return count

    def clear_exit_blocked(self, condition_id: str) -> None:
        """Remove exit-blocked flag, reset rejection count and retry timer."""
        self._exit_blocked_markets.discard(condition_id)
        self._exit_rejection_counts.pop(condition_id, None)
        self._exit_retry_after.pop(condition_id, None)

    def is_exit_blocked(self, condition_id: str) -> bool:
        """Check if a market is exit-blocked."""
        return condition_id in self._exit_blocked_markets

    def exit_rejection_count(self, condition_id: str) -> int:
        """Return consecutive rejection count for a market."""
        return self._exit_rejection_counts.get(condition_id, 0)

    def should_retry_exit(self, condition_id: str) -> bool:
        """True if backoff timer has elapsed and exit should be retried.

        Returns True if the market is not exit-blocked (no backoff needed)
        or if the backoff timer has elapsed.
        """
        retry_after = self._exit_retry_after.get(condition_id)
        if retry_after is None:
            return True
        return time.time() >= retry_after

    # ------------------------------------------------------------------
    # Circuit breaker
    # ------------------------------------------------------------------

    def is_circuit_breaker_active(self) -> bool:
        """Check whether the circuit breaker is currently active.

        The breaker automatically deactivates once its timeout has elapsed.
        """
        if not self._circuit_breaker_active:
            return False

        if time.time() >= self._circuit_breaker_until:
            self._log.info(
                "circuit_breaker_auto_deactivated",
                reason=self._circuit_breaker_reason,
            )
            self._circuit_breaker_active = False
            self._circuit_breaker_reason = ""
            return False

        return True

    def activate_circuit_breaker(
        self,
        reason: str,
        duration_seconds: float = _DEFAULT_CIRCUIT_BREAKER_DURATION,
    ) -> None:
        """Manually activate the circuit breaker.

        Args:
            reason: Human-readable explanation for why the breaker tripped.
            duration_seconds: How long the breaker should remain active
                (default 300 s / 5 min).
        """
        self._circuit_breaker_active = True
        self._circuit_breaker_reason = reason
        self._circuit_breaker_until = time.time() + duration_seconds
        self._log.error(
            "circuit_breaker_activated",
            reason=reason,
            duration_seconds=duration_seconds,
        )
        self._persist_circuit_breaker()

    def deactivate_circuit_breaker(self) -> None:
        """Manually deactivate the circuit breaker."""
        self._log.info(
            "circuit_breaker_deactivated",
            reason=self._circuit_breaker_reason,
        )
        self._circuit_breaker_active = False
        self._circuit_breaker_reason = ""
        self._circuit_breaker_until = 0.0
        self._persist_circuit_breaker()

    # ------------------------------------------------------------------
    # Per-strategy consecutive loss cooldown
    # ------------------------------------------------------------------

    def record_trade_outcome(self, strategy: StrategyType, is_win: bool) -> None:
        """Record a trade outcome for per-strategy cooldown tracking.

        On win: resets the consecutive loss counter to 0.
        On loss: increments the counter; if it reaches the threshold,
        activates a cooldown for that strategy.
        """
        key = strategy.value
        if is_win:
            self._strategy_consecutive_losses[key] = 0
            self._log.debug("strategy_loss_counter_reset", strategy=key)
        else:
            count = self._strategy_consecutive_losses.get(key, 0) + 1
            self._strategy_consecutive_losses[key] = count
            self._log.info(
                "strategy_consecutive_loss",
                strategy=key,
                consecutive_losses=count,
                threshold=self._settings.strategy_cooldown_consecutive_losses,
            )
            if count >= self._settings.strategy_cooldown_consecutive_losses:
                duration = self._settings.strategy_cooldown_duration
                self._strategy_cooldown_until[key] = time.time() + duration
                self._log.warning(
                    "strategy_cooldown_activated",
                    strategy=key,
                    consecutive_losses=count,
                    duration_seconds=duration,
                )
                self._persist_strategy_cooldowns()

    def is_strategy_cooling_down(self, strategy: StrategyType) -> bool:
        """Check whether a strategy is in cooldown due to consecutive losses.

        Auto-clears expired cooldowns and resets the loss counter.
        """
        key = strategy.value
        until = self._strategy_cooldown_until.get(key)
        if until is None:
            return False

        if time.time() >= until:
            # Cooldown expired — clear state
            del self._strategy_cooldown_until[key]
            self._strategy_consecutive_losses[key] = 0
            self._log.info("strategy_cooldown_expired", strategy=key)
            self._persist_strategy_cooldowns()
            return False

        return True

    def _persist_strategy_cooldowns(self) -> None:
        """Save strategy cooldown state to the database."""
        if self._trade_db is None:
            return
        try:
            state = {
                "consecutive_losses": self._strategy_consecutive_losses,
                "cooldown_until": self._strategy_cooldown_until,
            }
            self._trade_db.save_strategy_cooldown_state(json.dumps(state))  # type: ignore[attr-defined]
        except Exception as exc:
            self._log.warning("strategy_cooldown_persist_failed", error=str(exc))

    # ------------------------------------------------------------------
    # Disconnect tracking
    # ------------------------------------------------------------------

    def record_disconnect(self, duration_seconds: float) -> None:
        """Record a WebSocket disconnect.

        If the disconnect lasted longer than the threshold (30s), the
        circuit breaker is activated for the default duration.
        """
        self._log.warning(
            "disconnect_recorded",
            duration_seconds=duration_seconds,
        )
        if duration_seconds > _DISCONNECT_THRESHOLD:
            self.activate_circuit_breaker(
                reason=f"disconnect lasted {duration_seconds:.1f}s",
                duration_seconds=_DEFAULT_CIRCUIT_BREAKER_DURATION,
            )

    # ------------------------------------------------------------------
    # Kelly sizing integration
    # ------------------------------------------------------------------

    def set_sizer(self, sizer: object) -> None:
        """Attach a PositionSizer for Kelly-adjusted sizing."""
        self._sizer = sizer
        self._log.info("sizer_attached", sizer=type(sizer).__name__)

    def load_persisted_state(self) -> None:
        """Restore circuit breaker state from the database on startup."""
        if self._trade_db is None:
            return
        try:
            state = self._trade_db.load_circuit_breaker_state()  # type: ignore[attr-defined]
            if state is None:
                return
            if state["active"] and float(state["until_ts"]) > time.time():
                self._circuit_breaker_active = True
                self._circuit_breaker_reason = str(state["reason"])
                self._circuit_breaker_until = float(state["until_ts"])
                remaining = self._circuit_breaker_until - time.time()
                self._log.warning(
                    "circuit_breaker_restored",
                    reason=self._circuit_breaker_reason,
                    remaining_seconds=round(remaining, 0),
                )
            else:
                # Expired — clear persisted state
                self._trade_db.save_circuit_breaker_state(False, "", 0.0)  # type: ignore[attr-defined]
        except Exception as exc:
            self._log.warning("circuit_breaker_restore_failed", error=str(exc))

        # Restore strategy cooldown state
        try:
            cooldown_state = self._trade_db.load_strategy_cooldown_state()  # type: ignore[attr-defined]
            if cooldown_state is not None:
                now = time.time()
                losses = cooldown_state.get("consecutive_losses", {})
                cooldowns = cooldown_state.get("cooldown_until", {})
                # Only restore non-expired cooldowns
                for key, until_ts in cooldowns.items():
                    if float(until_ts) > now:
                        self._strategy_cooldown_until[key] = float(until_ts)
                        self._strategy_consecutive_losses[key] = int(losses.get(key, 0))
                        remaining = float(until_ts) - now
                        self._log.warning(
                            "strategy_cooldown_restored",
                            strategy=key,
                            remaining_seconds=round(remaining, 0),
                        )
                    else:
                        # Expired — don't restore
                        self._log.info(
                            "strategy_cooldown_expired_on_load",
                            strategy=key,
                        )
                # Restore loss counters for strategies without active cooldowns too
                for key, count in losses.items():
                    if key not in self._strategy_consecutive_losses:
                        self._strategy_consecutive_losses[key] = int(count)
        except Exception as exc:
            self._log.warning("strategy_cooldown_restore_failed", error=str(exc))

    def _persist_circuit_breaker(self) -> None:
        """Save current circuit breaker state to the database."""
        if self._trade_db is None:
            return
        try:
            self._trade_db.save_circuit_breaker_state(  # type: ignore[attr-defined]
                self._circuit_breaker_active,
                self._circuit_breaker_reason,
                self._circuit_breaker_until,
            )
        except Exception as exc:
            self._log.warning("circuit_breaker_persist_failed", error=str(exc))

    # ------------------------------------------------------------------
    # Kelly sizing integration
    # ------------------------------------------------------------------

    def kelly_adjusted_size(
        self,
        opp: Opportunity,
        bankroll: float,
        win_rate: float = 0.0,
        avg_win: float = 0.0,
        avg_loss: float = 0.0,
    ) -> float:
        """Compute Kelly-adjusted size, falling back to adjust_size if no sizer.

        For arbitrage, uses kelly_fraction_arb(edge, bankroll).
        For directional, uses kelly_fraction_directional(win_rate, avg_win, avg_loss, bankroll).
        """
        if self._sizer is None:
            return self.adjust_size(opp, self._settings.order_size)

        kelly_size = 0.0
        if opp.strategy in _HEDGED_STRATEGIES:
            edge = opp.expected_profit_pct if opp.expected_profit_pct > 0 else 0.0
            if edge > 0:
                kelly_size = self._sizer.kelly_fraction_arb(edge, bankroll)  # type: ignore[attr-defined]
        else:
            if win_rate > 0 and avg_win > 0 and avg_loss > 0:
                kelly_size = self._sizer.kelly_fraction_directional(  # type: ignore[attr-defined]
                    win_rate, avg_win, avg_loss, bankroll,
                )

        if kelly_size <= 0:
            return self.adjust_size(opp, self._settings.order_size)

        # Divergence-weighted scaling: higher KL → larger position
        if self._settings.divergence_kelly_scaling:
            kl = opp.metadata.get("kl_kl_divergence", 0.0)
            if isinstance(kl, (int, float)) and kl > 0:
                from src.utils.divergence import divergence_scaling_factor

                scale = divergence_scaling_factor(kl)
                kelly_size *= scale

        # Still apply risk limits on the Kelly-derived size
        return self.adjust_size(opp, kelly_size)
