"""Pre-trade risk checks and exposure monitoring for the Polymarket trading bot."""

from __future__ import annotations

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


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_HEDGED_STRATEGIES: frozenset[StrategyType] = frozenset({StrategyType.ARBITRAGE})

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

        # 1. Circuit breaker
        if self.is_circuit_breaker_active():
            reason = f"circuit breaker active: {self._circuit_breaker_reason}"
            self._log.warning("risk_rejected", check="circuit_breaker", reason=reason)
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

        # 3. Market exposure
        market_exp = self._state.market_exposure(condition_id)
        if market_exp >= self._settings.max_position_per_market:
            reason = (
                f"market exposure limit reached: {market_exp:.2f} >= "
                f"{self._settings.max_position_per_market:.2f}"
            )
            self._log.warning("risk_rejected", check="market_exposure", reason=reason)
            return False, reason

        # 4. Total exposure
        total_exp = self._state.total_exposure()
        if total_exp >= self._settings.max_total_position:
            reason = (
                f"total exposure limit reached: {total_exp:.2f} >= "
                f"{self._settings.max_total_position:.2f}"
            )
            self._log.warning("risk_rejected", check="total_exposure", reason=reason)
            return False, reason

        # 5. Unhedged exposure (skip for hedged strategies like arbitrage)
        if opp.strategy not in _HEDGED_STRATEGIES:
            unhedged = self._state.total_unhedged_exposure()
            if unhedged >= self._settings.max_unhedged_exposure:
                reason = (
                    f"unhedged exposure limit reached: {unhedged:.2f} >= "
                    f"{self._settings.max_unhedged_exposure:.2f}"
                )
                self._log.warning("risk_rejected", check="unhedged_exposure", reason=reason)
                return False, reason

        # 6. Dead zone
        start_ts = opp.market.start_time.timestamp()
        end_ts = opp.market.end_time.timestamp()
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

        # 8. Time remaining
        remaining = time_remaining_seconds(end_ts)
        if remaining <= _MIN_TIME_REMAINING:
            reason = f"insufficient time remaining: {remaining:.1f}s <= {_MIN_TIME_REMAINING:.1f}s"
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

    def adjust_size(self, opp: Opportunity, requested_size: float) -> float:
        """Adjust order size to stay within risk limits.

        Returns the minimum of:
          - *requested_size*
          - remaining capacity for this market
          - remaining total capacity
          - remaining unhedged capacity (for directional / non-hedged strategies)

        Returns ``0.0`` if no capacity remains.
        """
        condition_id = opp.market.condition_id

        # Market capacity
        market_remaining = (
            self._settings.max_position_per_market
            - self._state.market_exposure(condition_id)
        )
        if market_remaining <= 0:
            return 0.0

        # Total capacity
        total_remaining = (
            self._settings.max_total_position - self._state.total_exposure()
        )
        if total_remaining <= 0:
            return 0.0

        size = min(requested_size, market_remaining, total_remaining)

        # Unhedged capacity (only constrain non-hedged strategies)
        if opp.strategy not in _HEDGED_STRATEGIES:
            unhedged_remaining = (
                self._settings.max_unhedged_exposure
                - self._state.total_unhedged_exposure()
            )
            if unhedged_remaining <= 0:
                return 0.0
            size = min(size, unhedged_remaining)

        return max(size, 0.0)

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

    def deactivate_circuit_breaker(self) -> None:
        """Manually deactivate the circuit breaker."""
        self._log.info(
            "circuit_breaker_deactivated",
            reason=self._circuit_breaker_reason,
        )
        self._circuit_breaker_active = False
        self._circuit_breaker_reason = ""
        self._circuit_breaker_until = 0.0

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

        # Still apply risk limits on the Kelly-derived size
        return self.adjust_size(opp, kelly_size)
