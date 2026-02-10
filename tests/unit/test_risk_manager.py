"""Comprehensive tests for src.risk.manager.RiskManager."""

from __future__ import annotations

import os
import time

os.environ.setdefault("BOT_PRIVATE_KEY", "0x" + "ab" * 32)

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from src.config import Settings
from src.core.models import (
    DailyPnL,
    FillEstimate,
    Market,
    Opportunity,
    StrategyType,
)
from src.risk.manager import RiskManager


# ---------------------------------------------------------------------------
# Mock StateProvider
# ---------------------------------------------------------------------------


class MockState:
    """Minimal StateProvider implementation for testing."""

    def __init__(
        self,
        total_exp: float = 0.0,
        market_exp: float = 0.0,
        unhedged_exp: float = 0.0,
        daily_loss: float = 0.0,
        entry_count: int = 0,
    ) -> None:
        self._total_exp = total_exp
        self._market_exp = market_exp
        self._unhedged_exp = unhedged_exp
        self._daily_loss = daily_loss
        self._entry_count = entry_count

    def total_exposure(self) -> float:
        return self._total_exp

    def market_exposure(self, condition_id: str) -> float:
        return self._market_exp

    def total_unhedged_exposure(self) -> float:
        return self._unhedged_exp

    def daily_pnl(self) -> DailyPnL:
        return DailyPnL(date="2024-01-01", net_profit=-self._daily_loss)

    def position_entry_count(self, condition_id: str) -> int:
        return self._entry_count

    def strategy_entry_count(self, condition_id: str, strategy: str) -> int:
        return 0


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_NOW = datetime.now(tz=timezone.utc)


def _make_market(
    start: datetime | None = None,
    end: datetime | None = None,
    condition_id: str = "cond_123",
) -> Market:
    return Market(
        condition_id=condition_id,
        slug="btc-updown-15m-test",
        question="Will BTC go up?",
        yes_token_id="yes_tok",
        no_token_id="no_tok",
        start_time=start or (_NOW - timedelta(minutes=5)),
        end_time=end or (_NOW + timedelta(minutes=10)),
        asset="BTC",
    )


def _make_opportunity(
    market: Market | None = None,
    strategy: StrategyType = StrategyType.ARBITRAGE,
) -> Opportunity:
    if market is None:
        market = _make_market()
    return Opportunity(
        strategy=strategy,
        market=market,
        timestamp=_NOW,
        expected_profit=5.0,
        total_fees=1.0,
    )


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        private_key="0x" + "ab" * 32,
        order_size=250.0,
        max_position_per_market=500.0,
        max_total_position=2000.0,
        max_daily_loss=50.0,
        max_unhedged_exposure=100.0,
        cooldown_seconds=5.0,
    )


# ---------------------------------------------------------------------------
# check_opportunity -- approved
# ---------------------------------------------------------------------------


class TestApproved:
    def test_approved_when_all_clear(self, settings: Settings) -> None:
        state = MockState()
        rm = RiskManager(settings, state)
        opp = _make_opportunity()
        approved, reason = rm.check_opportunity(opp)
        assert approved is True
        assert reason == "approved"


# ---------------------------------------------------------------------------
# check_opportunity -- rejected scenarios
# ---------------------------------------------------------------------------


class TestRejected:
    def test_rejected_circuit_breaker_active(self, settings: Settings) -> None:
        state = MockState()
        rm = RiskManager(settings, state)
        rm.activate_circuit_breaker("test reason", duration_seconds=60.0)
        opp = _make_opportunity()
        approved, reason = rm.check_opportunity(opp)
        assert approved is False
        assert "circuit breaker" in reason

    def test_rejected_daily_loss_exceeded(self, settings: Settings) -> None:
        state = MockState(daily_loss=60.0)  # exceeds max_daily_loss=50
        rm = RiskManager(settings, state)
        opp = _make_opportunity()
        approved, reason = rm.check_opportunity(opp)
        assert approved is False
        assert "daily loss" in reason

    def test_rejected_market_exposure_exceeded(self, settings: Settings) -> None:
        state = MockState(market_exp=500.0)  # at max_position_per_market
        rm = RiskManager(settings, state)
        opp = _make_opportunity()
        approved, reason = rm.check_opportunity(opp)
        assert approved is False
        assert "market exposure" in reason

    def test_rejected_total_exposure_exceeded(self, settings: Settings) -> None:
        state = MockState(total_exp=2000.0)  # at max_total_position
        rm = RiskManager(settings, state)
        opp = _make_opportunity()
        approved, reason = rm.check_opportunity(opp)
        assert approved is False
        assert "total exposure" in reason

    def test_rejected_unhedged_exposure_for_directional_strategy(
        self, settings: Settings
    ) -> None:
        state = MockState(unhedged_exp=100.0)
        rm = RiskManager(settings, state)
        opp = _make_opportunity(strategy=StrategyType.PRICE_LAG)
        approved, reason = rm.check_opportunity(opp)
        assert approved is False
        assert "unhedged" in reason

    def test_hedged_strategy_skips_unhedged_check(self, settings: Settings) -> None:
        """Arbitrage (hedged) should not be rejected by unhedged exposure."""
        state = MockState(unhedged_exp=999.0)  # way over limit
        rm = RiskManager(settings, state)
        opp = _make_opportunity(strategy=StrategyType.ARBITRAGE)
        approved, _ = rm.check_opportunity(opp)
        assert approved is True

    @patch("src.risk.manager.is_in_dead_zone", return_value=True)
    def test_rejected_dead_zone(self, _mock_dz, settings: Settings) -> None:
        state = MockState()
        rm = RiskManager(settings, state)
        opp = _make_opportunity()
        approved, reason = rm.check_opportunity(opp)
        assert approved is False
        assert "dead zone" in reason

    def test_rejected_cooldown_not_elapsed(self, settings: Settings) -> None:
        state = MockState()
        rm = RiskManager(settings, state)
        # Record a recent trade
        rm.record_execution_success("cond_123")
        opp = _make_opportunity()
        approved, reason = rm.check_opportunity(opp)
        assert approved is False
        assert "cooldown" in reason

    @patch("src.risk.manager.is_in_dead_zone", return_value=False)
    def test_rejected_insufficient_time_remaining(
        self, _mock_dz, settings: Settings
    ) -> None:
        state = MockState()
        rm = RiskManager(settings, state)
        # Market ending in 20 seconds (< 30s minimum)
        market = _make_market(end=_NOW + timedelta(seconds=20))
        opp = _make_opportunity(market=market)
        approved, reason = rm.check_opportunity(opp)
        assert approved is False
        assert "time remaining" in reason


# ---------------------------------------------------------------------------
# adjust_size
# ---------------------------------------------------------------------------


class TestAdjustSize:
    def test_returns_requested_when_plenty_of_capacity(
        self, settings: Settings
    ) -> None:
        state = MockState()
        rm = RiskManager(settings, state)
        opp = _make_opportunity()
        size = rm.adjust_size(opp, 50.0)
        assert size == 50.0

    def test_caps_to_market_limit(self, settings: Settings) -> None:
        state = MockState(market_exp=420.0)  # only 80 remaining
        rm = RiskManager(settings, state)
        opp = _make_opportunity()
        size = rm.adjust_size(opp, 100.0)
        assert size == pytest.approx(80.0)

    def test_caps_to_total_limit(self, settings: Settings) -> None:
        state = MockState(total_exp=1930.0)  # only 70 remaining
        rm = RiskManager(settings, state)
        opp = _make_opportunity()
        size = rm.adjust_size(opp, 100.0)
        assert size == pytest.approx(70.0)

    def test_returns_zero_when_no_market_capacity(self, settings: Settings) -> None:
        state = MockState(market_exp=500.0)
        rm = RiskManager(settings, state)
        opp = _make_opportunity()
        size = rm.adjust_size(opp, 50.0)
        assert size == 0.0

    def test_returns_zero_when_no_total_capacity(self, settings: Settings) -> None:
        state = MockState(total_exp=2000.0)
        rm = RiskManager(settings, state)
        opp = _make_opportunity()
        size = rm.adjust_size(opp, 50.0)
        assert size == 0.0

    def test_caps_unhedged_for_directional_strategy(
        self, settings: Settings
    ) -> None:
        state = MockState(unhedged_exp=20.0)  # only 80 remaining
        rm = RiskManager(settings, state)
        opp = _make_opportunity(strategy=StrategyType.PRICE_LAG)
        size = rm.adjust_size(opp, 100.0)
        assert size == pytest.approx(80.0)

    def test_hedged_strategy_ignores_unhedged_cap(self, settings: Settings) -> None:
        state = MockState(unhedged_exp=999.0)
        rm = RiskManager(settings, state)
        opp = _make_opportunity(strategy=StrategyType.ARBITRAGE)
        size = rm.adjust_size(opp, 50.0)
        assert size == 50.0


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


class TestCircuitBreaker:
    def test_not_active_by_default(self, settings: Settings) -> None:
        rm = RiskManager(settings, MockState())
        assert rm.is_circuit_breaker_active() is False

    def test_activate_and_check(self, settings: Settings) -> None:
        rm = RiskManager(settings, MockState())
        rm.activate_circuit_breaker("test", duration_seconds=60.0)
        assert rm.is_circuit_breaker_active() is True

    def test_deactivate(self, settings: Settings) -> None:
        rm = RiskManager(settings, MockState())
        rm.activate_circuit_breaker("test", duration_seconds=60.0)
        rm.deactivate_circuit_breaker()
        assert rm.is_circuit_breaker_active() is False

    @patch("src.risk.manager.time.time")
    def test_auto_deactivate_after_timeout(
        self, mock_time, settings: Settings
    ) -> None:
        mock_time.return_value = 1000.0
        rm = RiskManager(settings, MockState())
        rm.activate_circuit_breaker("test", duration_seconds=60.0)
        assert rm.is_circuit_breaker_active() is True

        # Advance past timeout
        mock_time.return_value = 1061.0
        assert rm.is_circuit_breaker_active() is False

    def test_3_failures_activates_breaker(self, settings: Settings) -> None:
        rm = RiskManager(settings, MockState())
        rm.record_execution_failure()
        rm.record_execution_failure()
        assert rm.is_circuit_breaker_active() is False
        rm.record_execution_failure()  # 3rd failure
        assert rm.is_circuit_breaker_active() is True

    def test_success_resets_failure_counter(self, settings: Settings) -> None:
        rm = RiskManager(settings, MockState())
        rm.record_execution_failure()
        rm.record_execution_failure()
        rm.record_execution_success("cond_123")
        rm.record_execution_failure()  # only 1 now, not 3
        assert rm.is_circuit_breaker_active() is False


# ---------------------------------------------------------------------------
# record_execution_success
# ---------------------------------------------------------------------------


class TestRecordSuccess:
    def test_updates_last_trade_time(self, settings: Settings) -> None:
        rm = RiskManager(settings, MockState())
        before = time.time()
        rm.record_execution_success("cond_abc")
        # The last trade time should be set
        assert rm._last_trade_time.get("cond_abc", 0) >= before


# ---------------------------------------------------------------------------
# Daily loss auto-breaker
# ---------------------------------------------------------------------------


class TestDailyLossBreaker:
    def test_daily_loss_activates_circuit_breaker(self, settings: Settings) -> None:
        """When daily loss exceeds limit, circuit breaker should auto-activate."""
        state = MockState(daily_loss=60.0)
        rm = RiskManager(settings, state)
        opp = _make_opportunity()
        approved, reason = rm.check_opportunity(opp)
        assert approved is False
        assert rm.is_circuit_breaker_active() is True

    def test_daily_loss_breaker_not_duplicated(self, settings: Settings) -> None:
        """If breaker is already active, don't re-activate on daily loss."""
        state = MockState(daily_loss=60.0)
        rm = RiskManager(settings, state)
        rm.activate_circuit_breaker("manual", duration_seconds=60.0)
        opp = _make_opportunity()
        rm.check_opportunity(opp)
        # Should still be the manual reason, not overwritten
        assert "manual" in rm._circuit_breaker_reason


# ---------------------------------------------------------------------------
# Disconnect tracking
# ---------------------------------------------------------------------------


class TestDisconnectTracking:
    def test_short_disconnect_no_breaker(self, settings: Settings) -> None:
        rm = RiskManager(settings, MockState())
        rm.record_disconnect(10.0)  # Under 30s threshold
        assert rm.is_circuit_breaker_active() is False

    def test_long_disconnect_triggers_breaker(self, settings: Settings) -> None:
        rm = RiskManager(settings, MockState())
        rm.record_disconnect(45.0)  # Over 30s threshold
        assert rm.is_circuit_breaker_active() is True

    def test_disconnect_at_threshold_no_breaker(self, settings: Settings) -> None:
        rm = RiskManager(settings, MockState())
        rm.record_disconnect(30.0)  # Exactly at threshold (not over)
        assert rm.is_circuit_breaker_active() is False


# ---------------------------------------------------------------------------
# Kelly sizing integration
# ---------------------------------------------------------------------------


class TestKellySizing:
    def test_no_sizer_falls_back_to_adjust_size(self, settings: Settings) -> None:
        state = MockState()
        rm = RiskManager(settings, state)
        opp = _make_opportunity()
        size = rm.kelly_adjusted_size(opp, bankroll=10_000.0)
        # Should fall back to adjust_size with settings.order_size (250.0)
        assert size == 250.0

    def test_arb_with_sizer(self, settings: Settings) -> None:
        from src.risk.sizing import PositionSizer

        state = MockState()
        rm = RiskManager(settings, state)
        sizer = PositionSizer(kelly_fraction=0.25, min_size=5.0, max_size=500.0)
        rm.set_sizer(sizer)

        opp = _make_opportunity(strategy=StrategyType.ARBITRAGE)
        opp.expected_profit_pct = 0.02  # 2% edge
        size = rm.kelly_adjusted_size(opp, bankroll=10_000.0)
        # Kelly: 0.02 * 10000 * 0.25 = 50, capped by risk limits
        assert size == pytest.approx(50.0)

    def test_directional_with_sizer(self, settings: Settings) -> None:
        from src.risk.sizing import PositionSizer

        state = MockState()
        rm = RiskManager(settings, state)
        sizer = PositionSizer(kelly_fraction=0.25, min_size=5.0, max_size=500.0)
        rm.set_sizer(sizer)

        opp = _make_opportunity(strategy=StrategyType.PRICE_LAG)
        size = rm.kelly_adjusted_size(
            opp, bankroll=10_000.0,
            win_rate=0.6, avg_win=10.0, avg_loss=8.0,
        )
        # Should produce a valid non-zero size
        assert size > 0

    def test_arb_zero_edge_falls_back(self, settings: Settings) -> None:
        from src.risk.sizing import PositionSizer

        state = MockState()
        rm = RiskManager(settings, state)
        sizer = PositionSizer(kelly_fraction=0.25)
        rm.set_sizer(sizer)

        opp = _make_opportunity(strategy=StrategyType.ARBITRAGE)
        opp.expected_profit_pct = 0.0  # No edge
        size = rm.kelly_adjusted_size(opp, bankroll=10_000.0)
        # Falls back to adjust_size with settings.order_size (250.0)
        assert size == 250.0

    def test_directional_no_stats_falls_back(self, settings: Settings) -> None:
        from src.risk.sizing import PositionSizer

        state = MockState()
        rm = RiskManager(settings, state)
        sizer = PositionSizer(kelly_fraction=0.25)
        rm.set_sizer(sizer)

        opp = _make_opportunity(strategy=StrategyType.PRICE_LAG)
        size = rm.kelly_adjusted_size(opp, bankroll=10_000.0)
        # No win_rate/avg_win/avg_loss → falls back to settings.order_size (250.0)
        # But capped by max_unhedged_exposure (100.0) for directional strategy
        assert size == 100.0

    def test_kelly_respects_risk_limits(self, settings: Settings) -> None:
        from src.risk.sizing import PositionSizer

        state = MockState(market_exp=440.0)  # only 60 remaining
        rm = RiskManager(settings, state)
        sizer = PositionSizer(kelly_fraction=1.0, min_size=50.0, max_size=500.0)
        rm.set_sizer(sizer)

        opp = _make_opportunity(strategy=StrategyType.ARBITRAGE)
        opp.expected_profit_pct = 0.10  # Large edge → big Kelly size
        size = rm.kelly_adjusted_size(opp, bankroll=100_000.0)
        # Capped by market remaining (60)
        assert size == pytest.approx(60.0)


# ---------------------------------------------------------------------------
# Rate limit timeout recording (behavioral test for execution fix)
# ---------------------------------------------------------------------------


class TestRateLimitTimeoutRecording:
    """Documents the fix: rate limit timeouts (asyncio.TimeoutError) must
    trigger record_execution_failure() so they count toward the circuit
    breaker threshold.

    Previously, some code paths caught TimeoutError without recording the
    failure, meaning repeated rate-limit timeouts would never trip the
    circuit breaker.  The fix ensures record_execution_failure() is called
    in every TimeoutError handler.
    """

    def test_rate_limit_timeout_records_failure(self, settings: Settings) -> None:
        """Verify that calling record_execution_failure (as the timeout handler
        now does) increments the failure counter and eventually trips the
        circuit breaker after 3 consecutive timeouts."""
        state = MockState()
        rm = RiskManager(settings, state)

        # Simulate 3 consecutive rate-limit timeouts, each calling
        # record_execution_failure as the fixed code does.
        assert rm.is_circuit_breaker_active() is False

        rm.record_execution_failure()  # timeout 1
        assert rm._consecutive_failures == 1
        assert rm.is_circuit_breaker_active() is False

        rm.record_execution_failure()  # timeout 2
        assert rm._consecutive_failures == 2
        assert rm.is_circuit_breaker_active() is False

        rm.record_execution_failure()  # timeout 3 -- should trip breaker
        assert rm._consecutive_failures == 3
        assert rm.is_circuit_breaker_active() is True

        # The circuit breaker reason should mention consecutive failures
        assert "3 consecutive failures" in rm._circuit_breaker_reason

        # New opportunities should now be rejected
        opp = _make_opportunity()
        approved, reason = rm.check_opportunity(opp)
        assert approved is False
        assert "circuit breaker" in reason


# ---------------------------------------------------------------------------
# Max entries per market (stacking prevention)
# ---------------------------------------------------------------------------


class TestMaxEntriesPerMarket:
    """Tests for the entry cap that prevents triple-stacking."""

    def test_rejected_when_entry_count_at_max(self, settings: Settings) -> None:
        """Should reject when entries >= max_entries_per_market."""
        settings.max_entries_per_market = 3
        state = MockState(entry_count=3)
        rm = RiskManager(settings, state)
        opp = _make_opportunity()
        approved, reason = rm.check_opportunity(opp)
        assert approved is False
        assert "max entries" in reason

    def test_rejected_when_entry_count_above_max(self, settings: Settings) -> None:
        """Should reject when entries > max_entries_per_market."""
        settings.max_entries_per_market = 3
        state = MockState(entry_count=5)
        rm = RiskManager(settings, state)
        opp = _make_opportunity()
        approved, reason = rm.check_opportunity(opp)
        assert approved is False
        assert "max entries" in reason

    def test_approved_when_entry_count_below_max(self, settings: Settings) -> None:
        """Should approve when entries < max_entries_per_market."""
        settings.max_entries_per_market = 3
        state = MockState(entry_count=1)
        rm = RiskManager(settings, state)
        opp = _make_opportunity()
        approved, reason = rm.check_opportunity(opp)
        assert approved is True
        assert reason == "approved"

    def test_approved_when_zero_entries(self, settings: Settings) -> None:
        """First entry should always be approved (0 < 2)."""
        state = MockState(entry_count=0)
        rm = RiskManager(settings, state)
        opp = _make_opportunity()
        approved, reason = rm.check_opportunity(opp)
        assert approved is True
        assert reason == "approved"

    def test_custom_max_entries_setting(self) -> None:
        """Respect custom max_entries_per_market value."""
        custom_settings = Settings(
            private_key="0x" + "ab" * 32,
            max_position_per_market=500.0,
            max_total_position=2000.0,
            max_daily_loss=50.0,
            max_unhedged_exposure=100.0,
            cooldown_seconds=5.0,
            max_entries_per_market=1,
        )
        state = MockState(entry_count=1)
        rm = RiskManager(custom_settings, state)
        opp = _make_opportunity()
        approved, reason = rm.check_opportunity(opp)
        assert approved is False
        assert "max entries" in reason
