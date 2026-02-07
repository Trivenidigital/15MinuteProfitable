"""Tests for Phase 1 observability: DecisionLogger, spot snapshots, market outcomes."""

import time
from datetime import UTC, datetime

import pytest

from src.core.models import (
    Market,
    Opportunity,
    StrategyType,
)
from src.data.decision_logger import DecisionLogger
from src.data.trade_db import (
    MarketOutcome,
    SpotSnapshot,
    StrategyDecision,
    TradeDatabase,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db():
    """Create an in-memory TradeDatabase for testing."""
    database = TradeDatabase(":memory:")
    yield database
    database.close()


@pytest.fixture
def logger(db: TradeDatabase) -> DecisionLogger:
    """Create a DecisionLogger backed by in-memory DB."""
    return DecisionLogger(db)


def _make_market(**overrides) -> Market:
    """Create a Market with sensible defaults."""
    defaults = dict(
        condition_id="cond_test",
        slug="will-btc-go-up-2026-02-07",
        question="Will BTC go up?",
        yes_token_id="yes_token_123",
        no_token_id="no_token_456",
        start_time=datetime(2026, 2, 7, 12, 0, tzinfo=UTC),
        end_time=datetime(2026, 2, 7, 12, 15, tzinfo=UTC),
        asset="BTC",
    )
    defaults.update(overrides)
    return Market(**defaults)


def _make_opportunity(**overrides) -> Opportunity:
    """Create an Opportunity with sensible defaults."""
    market = overrides.pop("market", _make_market())
    defaults = dict(
        strategy=StrategyType.ARBITRAGE,
        market=market,
        timestamp=datetime.now(tz=UTC),
        expected_profit=1.50,
        expected_profit_pct=0.015,
        confidence=0.85,
        metadata={"source": "test", "pair_cost": 0.94},
    )
    defaults.update(overrides)
    return Opportunity(**defaults)


# ---------------------------------------------------------------------------
# TradeDatabase: strategy_decisions table
# ---------------------------------------------------------------------------


class TestSaveDecision:
    def test_save_and_retrieve(self, db: TradeDatabase) -> None:
        d = StrategyDecision(
            timestamp=time.time(),
            cycle_id=1,
            condition_id="cond_1",
            market_slug="test-market",
            asset="BTC",
            strategy="arbitrage",
            decision="opportunity",
            confidence=0.9,
            expected_profit=1.5,
            expected_profit_pct=0.015,
            metadata_json='{"key": "val"}',
        )
        row_id = db.save_decision(d)
        assert row_id >= 1

        decisions = db.get_decisions(limit=10)
        assert len(decisions) == 1
        assert decisions[0].condition_id == "cond_1"
        assert decisions[0].strategy == "arbitrage"
        assert decisions[0].decision == "opportunity"
        assert decisions[0].confidence == pytest.approx(0.9)

    def test_get_decisions_with_strategy_filter(self, db: TradeDatabase) -> None:
        db.save_decision(StrategyDecision(
            timestamp=time.time(), cycle_id=1,
            strategy="arbitrage", decision="opportunity",
        ))
        db.save_decision(StrategyDecision(
            timestamp=time.time(), cycle_id=1,
            strategy="price_lag", decision="opportunity",
        ))
        db.save_decision(StrategyDecision(
            timestamp=time.time(), cycle_id=1,
            strategy="arbitrage", decision="risk_approved",
        ))

        arb = db.get_decisions(strategy="arbitrage")
        assert len(arb) == 2
        assert all(d.strategy == "arbitrage" for d in arb)

        lag = db.get_decisions(strategy="price_lag")
        assert len(lag) == 1

    def test_round_trip_all_fields(self, db: TradeDatabase) -> None:
        d = StrategyDecision(
            timestamp=1000.0,
            cycle_id=42,
            condition_id="cond_xyz",
            market_slug="slug-test",
            asset="ETH",
            strategy="price_lag",
            decision="risk_rejected",
            rejection_reason="max_exposure",
            confidence=0.72,
            expected_profit=0.5,
            expected_profit_pct=0.005,
            metadata_json='{"direction": "UP"}',
        )
        db.save_decision(d)
        r = db.get_decisions(limit=1)[0]
        assert r.cycle_id == 42
        assert r.condition_id == "cond_xyz"
        assert r.asset == "ETH"
        assert r.decision == "risk_rejected"
        assert r.rejection_reason == "max_exposure"
        assert r.metadata_json == '{"direction": "UP"}'


# ---------------------------------------------------------------------------
# TradeDatabase: spot_snapshots table
# ---------------------------------------------------------------------------


class TestSpotSnapshots:
    def test_save_and_retrieve(self, db: TradeDatabase) -> None:
        s = SpotSnapshot(timestamp=1000.0, symbol="BTCUSDT", price=97500.0)
        row_id = db.save_spot_snapshot(s)
        assert row_id >= 1

        snapshots = db.get_spot_snapshots("BTCUSDT")
        assert len(snapshots) == 1
        assert snapshots[0].symbol == "BTCUSDT"
        assert snapshots[0].price == pytest.approx(97500.0)

    def test_get_spot_at_time_finds_closest(self, db: TradeDatabase) -> None:
        for i in range(5):
            db.save_spot_snapshot(SpotSnapshot(
                timestamp=1000.0 + i * 10, symbol="BTCUSDT", price=97500.0 + i,
            ))

        # Target at 1025 -> closest is 1020 (price 97502) or 1030 (price 97503)
        price = db.get_spot_at_time("BTCUSDT", 1025.0, tolerance_s=10.0)
        assert price is not None
        assert price in (97502.0, 97503.0)

    def test_get_spot_at_time_none_outside_tolerance(self, db: TradeDatabase) -> None:
        db.save_spot_snapshot(SpotSnapshot(
            timestamp=1000.0, symbol="BTCUSDT", price=97500.0,
        ))
        # Target far away
        price = db.get_spot_at_time("BTCUSDT", 2000.0, tolerance_s=30.0)
        assert price is None

    def test_multiple_symbols(self, db: TradeDatabase) -> None:
        db.save_spot_snapshot(SpotSnapshot(timestamp=1000.0, symbol="BTCUSDT", price=97500.0))
        db.save_spot_snapshot(SpotSnapshot(timestamp=1000.0, symbol="ETHUSDT", price=3200.0))

        btc = db.get_spot_snapshots("BTCUSDT")
        assert len(btc) == 1
        assert btc[0].price == pytest.approx(97500.0)

        eth = db.get_spot_snapshots("ETHUSDT")
        assert len(eth) == 1
        assert eth[0].price == pytest.approx(3200.0)


# ---------------------------------------------------------------------------
# TradeDatabase: market_outcomes table
# ---------------------------------------------------------------------------


class TestMarketOutcomes:
    def test_save_and_retrieve(self, db: TradeDatabase) -> None:
        o = MarketOutcome(
            timestamp=time.time(),
            condition_id="cond_1",
            asset="BTC",
            market_slug="will-btc-go-up",
            window_start=1000.0,
            window_end=1900.0,
            outcome="YES",
            spot_open=97500.0,
            spot_close=97600.0,
            price_change_pct=0.1026,
            was_traded=True,
        )
        row_id = db.save_market_outcome(o)
        assert row_id >= 1

        outcomes = db.get_market_outcomes()
        assert len(outcomes) == 1
        assert outcomes[0].condition_id == "cond_1"
        assert outcomes[0].outcome == "YES"
        assert outcomes[0].was_traded is True

    def test_get_market_outcomes_with_asset_filter(self, db: TradeDatabase) -> None:
        db.save_market_outcome(MarketOutcome(
            timestamp=time.time(), condition_id="c1",
            asset="BTC", outcome="YES", window_end=1000.0,
        ))
        db.save_market_outcome(MarketOutcome(
            timestamp=time.time(), condition_id="c2",
            asset="ETH", outcome="NO", window_end=1000.0,
        ))
        db.save_market_outcome(MarketOutcome(
            timestamp=time.time(), condition_id="c3",
            asset="BTC", outcome="NO", window_end=1100.0,
        ))

        btc = db.get_market_outcomes(asset="BTC")
        assert len(btc) == 2
        assert all(o.asset == "BTC" for o in btc)

    def test_get_outcome_stats(self, db: TradeDatabase) -> None:
        db.save_market_outcome(MarketOutcome(
            timestamp=time.time(), condition_id="c1",
            asset="BTC", outcome="YES", price_change_pct=0.1,
        ))
        db.save_market_outcome(MarketOutcome(
            timestamp=time.time(), condition_id="c2",
            asset="BTC", outcome="YES", price_change_pct=0.2,
        ))
        db.save_market_outcome(MarketOutcome(
            timestamp=time.time(), condition_id="c3",
            asset="BTC", outcome="NO", price_change_pct=-0.1,
        ))
        db.save_market_outcome(MarketOutcome(
            timestamp=time.time(), condition_id="c4",
            asset="BTC", outcome="FLAT", price_change_pct=0.0,
        ))

        stats = db.get_outcome_stats(asset="BTC")
        assert stats["count"] == 4
        assert stats["yes_count"] == 2
        assert stats["no_count"] == 1
        assert stats["flat_count"] == 1
        assert stats["win_rate"] == pytest.approx(0.5)
        assert stats["avg_change_pct"] == pytest.approx(0.05)

    def test_get_outcome_stats_empty(self, db: TradeDatabase) -> None:
        stats = db.get_outcome_stats()
        assert stats["count"] == 0
        assert stats["win_rate"] == 0.0

    def test_upsert_on_duplicate_condition_id(self, db: TradeDatabase) -> None:
        db.save_market_outcome(MarketOutcome(
            timestamp=1000.0, condition_id="c1",
            asset="BTC", outcome="YES", price_change_pct=0.1,
        ))
        # Same condition_id -> should replace
        db.save_market_outcome(MarketOutcome(
            timestamp=2000.0, condition_id="c1",
            asset="BTC", outcome="NO", price_change_pct=-0.05,
        ))
        outcomes = db.get_market_outcomes()
        assert len(outcomes) == 1
        assert outcomes[0].outcome == "NO"


# ---------------------------------------------------------------------------
# DecisionLogger
# ---------------------------------------------------------------------------


class TestDecisionLogger:
    def test_next_cycle_increments(self, logger: DecisionLogger) -> None:
        assert logger.next_cycle() == 1
        assert logger.next_cycle() == 2
        assert logger.next_cycle() == 3

    def test_log_opportunity_saves(self, logger: DecisionLogger, db: TradeDatabase) -> None:
        opp = _make_opportunity()
        logger.log_opportunity(1, opp)

        decisions = db.get_decisions()
        assert len(decisions) == 1
        assert decisions[0].decision == "opportunity"
        assert decisions[0].strategy == "arbitrage"
        assert decisions[0].condition_id == "cond_test"

    def test_log_risk_approved(self, logger: DecisionLogger, db: TradeDatabase) -> None:
        opp = _make_opportunity()
        logger.log_risk_decision(1, opp, approved=True, reason="approved")

        decisions = db.get_decisions()
        assert len(decisions) == 1
        assert decisions[0].decision == "risk_approved"
        assert decisions[0].rejection_reason == ""

    def test_log_risk_rejected(self, logger: DecisionLogger, db: TradeDatabase) -> None:
        opp = _make_opportunity()
        logger.log_risk_decision(1, opp, approved=False, reason="max_exposure")

        decisions = db.get_decisions()
        assert len(decisions) == 1
        assert decisions[0].decision == "risk_rejected"
        assert decisions[0].rejection_reason == "max_exposure"

    def test_log_execution_success(self, logger: DecisionLogger, db: TradeDatabase) -> None:
        opp = _make_opportunity()
        logger.log_execution(1, opp, success=True)

        decisions = db.get_decisions()
        assert len(decisions) == 1
        assert decisions[0].decision == "executed"

    def test_log_execution_failure(self, logger: DecisionLogger, db: TradeDatabase) -> None:
        opp = _make_opportunity()
        logger.log_execution(1, opp, success=False)

        decisions = db.get_decisions()
        assert len(decisions) == 1
        assert decisions[0].decision == "exec_failed"

    def test_log_no_opportunities(self, logger: DecisionLogger, db: TradeDatabase) -> None:
        logger.log_no_opportunities(1)

        decisions = db.get_decisions()
        assert len(decisions) == 1
        assert decisions[0].decision == "no_opportunities"
        assert decisions[0].cycle_id == 1

    def test_full_cycle_logs_three_rows(
        self, logger: DecisionLogger, db: TradeDatabase
    ) -> None:
        """A full cycle: opportunity -> risk_approved -> executed = 3 rows."""
        opp = _make_opportunity()
        cycle = logger.next_cycle()
        logger.log_opportunity(cycle, opp)
        logger.log_risk_decision(cycle, opp, approved=True, reason="approved")
        logger.log_execution(cycle, opp, success=True)

        decisions = db.get_decisions()
        assert len(decisions) == 3
        decision_types = {d.decision for d in decisions}
        assert decision_types == {"opportunity", "risk_approved", "executed"}

    def test_metadata_json_serialized(
        self, logger: DecisionLogger, db: TradeDatabase
    ) -> None:
        opp = _make_opportunity(metadata={"direction": "UP", "score": 0.95})
        logger.log_opportunity(1, opp)

        decisions = db.get_decisions()
        assert '"direction": "UP"' in decisions[0].metadata_json
        assert '"score": 0.95' in decisions[0].metadata_json


# ---------------------------------------------------------------------------
# Schema migration (new tables alongside existing)
# ---------------------------------------------------------------------------


class TestSchemaMigration:
    def test_new_tables_created_on_fresh_db(self) -> None:
        """All new tables exist on a freshly created database."""
        db = TradeDatabase(":memory:")
        try:
            # Should be able to query all new tables without error
            db.get_decisions()
            db.get_spot_snapshots("BTCUSDT")
            db.get_market_outcomes()
        finally:
            db.close()

    def test_new_tables_alongside_existing(self) -> None:
        """New tables don't conflict with existing ones."""
        db = TradeDatabase(":memory:")
        try:
            # Write to old table
            from src.data.trade_db import TradeRecord
            db.save_trade(TradeRecord(
                timestamp=time.time(), condition_id="c1",
                side="BUY", token_side="YES",
            ))
            assert db.get_trade_count() == 1

            # Write to new tables
            db.save_decision(StrategyDecision(
                timestamp=time.time(), cycle_id=1, decision="test",
            ))
            db.save_spot_snapshot(SpotSnapshot(
                timestamp=time.time(), symbol="BTCUSDT", price=97500.0,
            ))
            db.save_market_outcome(MarketOutcome(
                timestamp=time.time(), condition_id="c_out",
                outcome="YES",
            ))

            # All coexist
            assert len(db.get_decisions()) == 1
            assert len(db.get_spot_snapshots("BTCUSDT")) == 1
            assert len(db.get_market_outcomes()) == 1
            assert db.get_trade_count() == 1
        finally:
            db.close()
