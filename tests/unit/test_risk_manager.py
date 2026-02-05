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
    ) -> None:
        self._total_exp = total_exp
        self._market_exp = market_exp
        self._unhedged_exp = unhedged_exp
        self._daily_loss = daily_loss

    def total_exposure(self) -> float:
        return self._total_exp

    def market_exposure(self, condition_id: str) -> float:
        return self._market_exp

    def total_unhedged_exposure(self) -> float:
        return self._unhedged_exp

    def daily_pnl(self) -> DailyPnL:
        return DailyPnL(date="2024-01-01", net_profit=-self._daily_loss)


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
        state = MockState(market_exp=480.0)  # only 20 remaining
        rm = RiskManager(settings, state)
        opp = _make_opportunity()
        size = rm.adjust_size(opp, 50.0)
        assert size == pytest.approx(20.0)

    def test_caps_to_total_limit(self, settings: Settings) -> None:
        state = MockState(total_exp=1990.0)  # only 10 remaining
        rm = RiskManager(settings, state)
        opp = _make_opportunity()
        size = rm.adjust_size(opp, 50.0)
        assert size == pytest.approx(10.0)

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
        state = MockState(unhedged_exp=80.0)  # only 20 remaining
        rm = RiskManager(settings, state)
        opp = _make_opportunity(strategy=StrategyType.PRICE_LAG)
        size = rm.adjust_size(opp, 50.0)
        assert size == pytest.approx(20.0)

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
