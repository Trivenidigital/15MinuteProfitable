"""Tests for the dynamic strategy allocation system."""

import math
import time

import pytest

from src.data.trade_db import TradeDatabase, TradeResult
from src.risk.allocation import (
    AllocationManager,
    _HALF_LIFE_SECONDS,
    _MAX_MULTIPLIER,
    _MIN_MULTIPLIER,
    _MIN_TRADES_REQUIRED,
    _WINDOW_SECONDS,
)


@pytest.fixture
def db():
    """Create an in-memory TradeDatabase for testing."""
    database = TradeDatabase(":memory:")
    yield database
    database.close()


def _make_settings(**overrides):
    """Create a minimal Settings-like object for AllocationManager."""
    from unittest.mock import MagicMock

    s = MagicMock()
    s.enable_dynamic_allocation = overrides.get("enable_dynamic_allocation", True)
    return s


def _make_result(
    strategy: str = "fade_panic",
    net_profit: float = 10.0,
    timestamp: float | None = None,
) -> TradeResult:
    """Create a TradeResult with sensible defaults."""
    return TradeResult(
        timestamp=timestamp or time.time(),
        condition_id="cond_1",
        market_slug="btc-updown-15m",
        asset="BTC",
        strategy=strategy,
        was_hedged=True,
        yes_shares=100.0,
        no_shares=100.0,
        investment=50.0,
        gross_payout=60.0,
        net_profit=net_profit,
        outcome="YES",
    )


class TestExponentialWeight:
    def test_weight_at_zero_age(self):
        now = time.time()
        w = AllocationManager._exponential_weight(now, now)
        assert abs(w - 1.0) < 0.001

    def test_weight_at_half_life(self):
        now = time.time()
        trade_ts = now - _HALF_LIFE_SECONDS
        w = AllocationManager._exponential_weight(trade_ts, now)
        assert abs(w - 0.5) < 0.01

    def test_weight_at_two_half_lives(self):
        now = time.time()
        trade_ts = now - 2 * _HALF_LIFE_SECONDS
        w = AllocationManager._exponential_weight(trade_ts, now)
        assert abs(w - 0.25) < 0.01

    def test_weight_decreases_with_age(self):
        now = time.time()
        w1 = AllocationManager._exponential_weight(now - 60, now)
        w2 = AllocationManager._exponential_weight(now - 600, now)
        w3 = AllocationManager._exponential_weight(now - 3600, now)
        assert w1 > w2 > w3


class TestWeightedProfitFactor:
    def test_all_wins(self):
        now = time.time()
        results = [_make_result(net_profit=10.0, timestamp=now - i * 60) for i in range(5)]
        pf = AllocationManager._weighted_profit_factor(results, now)
        assert pf == 2.0  # all-wins default

    def test_all_losses(self):
        now = time.time()
        results = [_make_result(net_profit=-10.0, timestamp=now - i * 60) for i in range(5)]
        pf = AllocationManager._weighted_profit_factor(results, now)
        assert pf == 0.0  # no wins, only losses -> 0/losses = 0

    def test_mixed_results(self):
        now = time.time()
        results = [
            _make_result(net_profit=20.0, timestamp=now - 60),
            _make_result(net_profit=-10.0, timestamp=now - 120),
        ]
        pf = AllocationManager._weighted_profit_factor(results, now)
        # Recent win (20, weight ~0.977) vs older loss (10, weight ~0.954)
        # pf = (20 * 0.977) / (10 * 0.954) ≈ 2.05
        assert pf > 1.5

    def test_recent_trades_weighted_more(self):
        now = time.time()
        # Scenario A: recent win, old loss
        results_a = [
            _make_result(net_profit=10.0, timestamp=now - 60),      # recent win
            _make_result(net_profit=-10.0, timestamp=now - 3600),   # old loss
        ]
        # Scenario B: old win, recent loss
        results_b = [
            _make_result(net_profit=10.0, timestamp=now - 3600),    # old win
            _make_result(net_profit=-10.0, timestamp=now - 60),     # recent loss
        ]
        pf_a = AllocationManager._weighted_profit_factor(results_a, now)
        pf_b = AllocationManager._weighted_profit_factor(results_b, now)
        assert pf_a > pf_b  # recent win should score higher

    def test_empty_results(self):
        pf = AllocationManager._weighted_profit_factor([], time.time())
        assert pf == 1.0  # no data default


class TestAllocationManager:
    def test_disabled_returns_base_size(self, db):
        settings = _make_settings(enable_dynamic_allocation=False)
        am = AllocationManager(settings, db)
        from src.core.models import StrategyType

        result = am.get_allocated_size(StrategyType.FADE_PANIC, 50.0)
        assert result == 50.0

    def test_insufficient_data_returns_base_size(self, db):
        settings = _make_settings()
        now = time.time()
        # Insert fewer than MIN_TRADES_REQUIRED
        for i in range(_MIN_TRADES_REQUIRED - 1):
            db.save_trade_result(_make_result(
                strategy="fade_panic",
                net_profit=10.0,
                timestamp=now - i * 60,
            ))
        am = AllocationManager(settings, db)
        from src.core.models import StrategyType

        result = am.get_allocated_size(StrategyType.FADE_PANIC, 50.0)
        assert result == 50.0

    def test_winning_strategy_gets_higher_allocation(self, db):
        settings = _make_settings()
        now = time.time()
        # fade_panic: 8 wins
        for i in range(8):
            db.save_trade_result(_make_result(
                strategy="fade_panic",
                net_profit=40.0,
                timestamp=now - i * 60,
            ))
        # asymmetric: 8 losses
        for i in range(8):
            db.save_trade_result(_make_result(
                strategy="asymmetric",
                net_profit=-60.0,
                timestamp=now - i * 60,
            ))
        am = AllocationManager(settings, db)
        from src.core.models import StrategyType

        fade_size = am.get_allocated_size(StrategyType.FADE_PANIC, 50.0)
        asym_size = am.get_allocated_size(StrategyType.ASYMMETRIC, 10.0)

        # fade_panic should get more than base, asymmetric less
        assert fade_size > 50.0
        assert asym_size < 10.0

    def test_multiplier_bounds_min(self, db):
        settings = _make_settings()
        now = time.time()
        # One strategy all wins, another all losses
        for i in range(10):
            db.save_trade_result(_make_result(
                strategy="fade_panic", net_profit=100.0, timestamp=now - i * 60,
            ))
            db.save_trade_result(_make_result(
                strategy="asymmetric", net_profit=-100.0, timestamp=now - i * 60,
            ))
        am = AllocationManager(settings, db)
        from src.core.models import StrategyType

        asym_size = am.get_allocated_size(StrategyType.ASYMMETRIC, 10.0)
        # Should be at floor: 10.0 * 0.1 = 1.0
        assert asym_size == pytest.approx(10.0 * _MIN_MULTIPLIER, abs=0.1)

    def test_multiplier_bounds_max(self, db):
        settings = _make_settings()
        now = time.time()
        # 4 strategies: 1 all wins, 3 all losses -> winner gets 4x raw multiplier
        # which exceeds _MAX_MULTIPLIER (3.0), so it should be clamped
        for i in range(10):
            db.save_trade_result(_make_result(
                strategy="fade_panic", net_profit=100.0, timestamp=now - i * 60,
            ))
            db.save_trade_result(_make_result(
                strategy="asymmetric", net_profit=-100.0, timestamp=now - i * 60,
            ))
            db.save_trade_result(_make_result(
                strategy="price_lag", net_profit=-100.0, timestamp=now - i * 60,
            ))
            db.save_trade_result(_make_result(
                strategy="resolution_sniper", net_profit=-100.0, timestamp=now - i * 60,
            ))
        am = AllocationManager(settings, db)
        from src.core.models import StrategyType

        fade_size = am.get_allocated_size(StrategyType.FADE_PANIC, 50.0)
        # fade_panic score=2.0, others score=0.0. Only fade_panic has sufficient data with wins.
        # With 4 strategies scored: raw_multiplier = (2.0/2.0)*4 = 4.0, clamped to 3.0
        assert fade_size == pytest.approx(50.0 * _MAX_MULTIPLIER, abs=1.0)

    def test_get_all_allocations(self, db):
        settings = _make_settings()
        now = time.time()
        for i in range(6):
            db.save_trade_result(_make_result(
                strategy="fade_panic", net_profit=10.0, timestamp=now - i * 60,
            ))
        am = AllocationManager(settings, db)
        # Trigger recalculation
        from src.core.models import StrategyType
        am.get_allocated_size(StrategyType.FADE_PANIC, 50.0)

        allocs = am.get_all_allocations()
        assert "fade_panic" in allocs
        assert allocs["fade_panic"].sufficient_data is True
        assert allocs["fade_panic"].trade_count == 6


class TestPerAssetAllocation:
    """Tests for per-(strategy, asset) allocation."""

    def test_winning_asset_gets_higher_allocation(self, db):
        settings = _make_settings()
        now = time.time()
        # fade_panic on ETH: 8 wins
        for i in range(8):
            db.save_trade_result(TradeResult(
                timestamp=now - i * 60,
                condition_id=f"cond_eth_{i}",
                market_slug="eth-updown-15m",
                asset="ETH",
                strategy="fade_panic",
                was_hedged=False,
                yes_shares=0.0, no_shares=50.0,
                investment=25.0, gross_payout=50.0,
                net_profit=25.0, outcome="NO",
            ))
        # fade_panic on BTC: 8 losses
        for i in range(8):
            db.save_trade_result(TradeResult(
                timestamp=now - i * 60,
                condition_id=f"cond_btc_{i}",
                market_slug="btc-updown-15m",
                asset="BTC",
                strategy="fade_panic",
                was_hedged=False,
                yes_shares=0.0, no_shares=50.0,
                investment=25.0, gross_payout=0.0,
                net_profit=-25.0, outcome="YES",
            ))
        am = AllocationManager(settings, db)
        from src.core.models import StrategyType

        eth_size = am.get_allocated_size(StrategyType.FADE_PANIC, 50.0, asset="ETH")
        btc_size = am.get_allocated_size(StrategyType.FADE_PANIC, 50.0, asset="BTC")

        # ETH should get more than BTC
        assert eth_size > btc_size
        assert eth_size > 50.0  # boosted
        assert btc_size < 50.0  # penalized

    def test_unknown_asset_falls_back_to_strategy(self, db):
        settings = _make_settings()
        now = time.time()
        # Only strategy-level data for fade_panic (mix of assets)
        for i in range(8):
            db.save_trade_result(_make_result(
                strategy="fade_panic", net_profit=10.0, timestamp=now - i * 60,
            ))
        am = AllocationManager(settings, db)
        from src.core.models import StrategyType

        # XRP has no per-asset data -> falls back to strategy-level
        xrp_size = am.get_allocated_size(StrategyType.FADE_PANIC, 50.0, asset="XRP")
        no_asset = am.get_allocated_size(StrategyType.FADE_PANIC, 50.0)

        # Both should use the strategy-level multiplier
        assert xrp_size == no_asset

    def test_no_asset_still_works(self, db):
        """Backward compatibility: no asset param uses strategy-level."""
        settings = _make_settings()
        now = time.time()
        for i in range(8):
            db.save_trade_result(_make_result(
                strategy="fade_panic", net_profit=10.0, timestamp=now - i * 60,
            ))
        am = AllocationManager(settings, db)
        from src.core.models import StrategyType

        result = am.get_allocated_size(StrategyType.FADE_PANIC, 50.0)
        assert result > 0  # should work without asset


class TestTradeResultsSince:
    def test_filters_by_timestamp(self, db):
        now = time.time()
        # One old, one recent
        db.save_trade_result(_make_result(timestamp=now - 10000))
        db.save_trade_result(_make_result(timestamp=now - 100))

        results = db.get_trade_results_since(now - 500)
        assert len(results) == 1
        assert results[0].timestamp == pytest.approx(now - 100, abs=1.0)

    def test_filters_by_strategy(self, db):
        now = time.time()
        db.save_trade_result(_make_result(strategy="fade_panic", timestamp=now - 100))
        db.save_trade_result(_make_result(strategy="asymmetric", timestamp=now - 100))

        results = db.get_trade_results_since(now - 500, strategy="fade_panic")
        assert len(results) == 1
        assert results[0].strategy == "fade_panic"

    def test_returns_empty_for_future_timestamp(self, db):
        now = time.time()
        db.save_trade_result(_make_result(timestamp=now - 100))
        results = db.get_trade_results_since(now + 1000)
        assert len(results) == 0
