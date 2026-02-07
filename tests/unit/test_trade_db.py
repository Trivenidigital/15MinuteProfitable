"""Tests for the SQLite trade database persistence layer."""

import time

import pytest

from src.data.trade_db import (
    DailySnapshot,
    PortfolioState,
    TradeDatabase,
    TradeRecord,
    TradeResult,
)


@pytest.fixture
def db():
    """Create an in-memory TradeDatabase for testing."""
    database = TradeDatabase(":memory:")
    yield database
    database.close()


def _make_trade(**overrides) -> TradeRecord:
    """Create a TradeRecord with sensible defaults."""
    defaults = dict(
        timestamp=time.time(),
        condition_id="cond_1",
        market_slug="will-btc-go-up",
        asset="BTC",
        strategy="arbitrage",
        side="BUY",
        token_side="YES",
        price=0.55,
        size=100.0,
        cost=55.0,
        order_type="FOK",
        order_id="ord_1",
        status="filled",
        fees=0.50,
        expected_profit=1.50,
        metadata_json='{"source": "test"}',
    )
    defaults.update(overrides)
    return TradeRecord(**defaults)


# ---------------------------------------------------------------------------
# Trades CRUD
# ---------------------------------------------------------------------------


class TestSaveTrade:
    def test_save_and_retrieve(self, db: TradeDatabase) -> None:
        trade = _make_trade()
        row_id = db.save_trade(trade)
        assert row_id >= 1

        trades = db.get_trades(limit=10)
        assert len(trades) == 1
        assert trades[0].condition_id == "cond_1"
        assert trades[0].price == 0.55
        assert trades[0].size == 100.0

    def test_save_multiple(self, db: TradeDatabase) -> None:
        for i in range(5):
            db.save_trade(_make_trade(order_id=f"ord_{i}", timestamp=time.time() + i))
        assert db.get_trade_count() == 5

    def test_round_trip_all_fields(self, db: TradeDatabase) -> None:
        trade = _make_trade(
            condition_id="cond_xyz",
            market_slug="slug-test",
            asset="ETH",
            strategy="price_lag",
            side="SELL",
            token_side="NO",
            price=0.42,
            size=50.0,
            cost=21.0,
            order_type="GTC",
            order_id="ord_abc",
            status="partially_filled",
            fees=0.25,
            expected_profit=2.0,
            metadata_json='{"key": "val"}',
        )
        db.save_trade(trade)
        result = db.get_trades(limit=1)[0]
        assert result.condition_id == "cond_xyz"
        assert result.asset == "ETH"
        assert result.strategy == "price_lag"
        assert result.side == "SELL"
        assert result.token_side == "NO"
        assert result.price == pytest.approx(0.42)
        assert result.order_type == "GTC"
        assert result.status == "partially_filled"
        assert result.metadata_json == '{"key": "val"}'


class TestGetTrades:
    def test_empty(self, db: TradeDatabase) -> None:
        trades = db.get_trades()
        assert trades == []

    def test_limit(self, db: TradeDatabase) -> None:
        for i in range(10):
            db.save_trade(_make_trade(order_id=f"ord_{i}", timestamp=time.time() + i))
        trades = db.get_trades(limit=3)
        assert len(trades) == 3

    def test_offset(self, db: TradeDatabase) -> None:
        for i in range(10):
            db.save_trade(_make_trade(order_id=f"ord_{i}", timestamp=1000.0 + i))
        trades = db.get_trades(limit=3, offset=7)
        assert len(trades) == 3
        # Offset 7 in descending order means we get the 3 oldest
        assert trades[0].timestamp == pytest.approx(1002.0)

    def test_strategy_filter(self, db: TradeDatabase) -> None:
        db.save_trade(_make_trade(strategy="arbitrage", order_id="a"))
        db.save_trade(_make_trade(strategy="price_lag", order_id="b"))
        db.save_trade(_make_trade(strategy="arbitrage", order_id="c"))

        arb = db.get_trades(strategy="arbitrage")
        assert len(arb) == 2
        assert all(t.strategy == "arbitrage" for t in arb)

        lag = db.get_trades(strategy="price_lag")
        assert len(lag) == 1

    def test_reverse_chronological_order(self, db: TradeDatabase) -> None:
        db.save_trade(_make_trade(timestamp=100.0, order_id="old"))
        db.save_trade(_make_trade(timestamp=200.0, order_id="new"))
        trades = db.get_trades()
        assert trades[0].timestamp > trades[1].timestamp


class TestGetTradeCount:
    def test_empty(self, db: TradeDatabase) -> None:
        assert db.get_trade_count() == 0

    def test_count(self, db: TradeDatabase) -> None:
        for i in range(7):
            db.save_trade(_make_trade(order_id=f"ord_{i}"))
        assert db.get_trade_count() == 7

    def test_count_with_strategy(self, db: TradeDatabase) -> None:
        db.save_trade(_make_trade(strategy="arbitrage", order_id="a"))
        db.save_trade(_make_trade(strategy="price_lag", order_id="b"))
        db.save_trade(_make_trade(strategy="arbitrage", order_id="c"))
        assert db.get_trade_count(strategy="arbitrage") == 2
        assert db.get_trade_count(strategy="price_lag") == 1
        assert db.get_trade_count(strategy="asymmetric") == 0


# ---------------------------------------------------------------------------
# Daily Snapshots
# ---------------------------------------------------------------------------


class TestDailySnapshots:
    def test_save_and_retrieve(self, db: TradeDatabase) -> None:
        snap = DailySnapshot(
            date="2025-01-15",
            trades=10,
            gross_profit=5.0,
            net_profit=4.0,
            total_fees=1.0,
            win_count=7,
            loss_count=3,
            max_drawdown=-2.0,
            sim_balance=1004.0,
            opportunities_seen=50,
            opportunities_taken=10,
        )
        db.save_daily_snapshot(snap)
        snapshots = db.get_daily_snapshots()
        assert len(snapshots) == 1
        assert snapshots[0].date == "2025-01-15"
        assert snapshots[0].trades == 10
        assert snapshots[0].net_profit == pytest.approx(4.0)
        assert snapshots[0].sim_balance == pytest.approx(1004.0)

    def test_upsert(self, db: TradeDatabase) -> None:
        snap1 = DailySnapshot(date="2025-01-15", trades=5, net_profit=2.0)
        snap2 = DailySnapshot(date="2025-01-15", trades=10, net_profit=4.0)
        db.save_daily_snapshot(snap1)
        db.save_daily_snapshot(snap2)
        snapshots = db.get_daily_snapshots()
        assert len(snapshots) == 1
        assert snapshots[0].trades == 10
        assert snapshots[0].net_profit == pytest.approx(4.0)

    def test_multiple_days(self, db: TradeDatabase) -> None:
        for day in range(1, 6):
            db.save_daily_snapshot(
                DailySnapshot(date=f"2025-01-{day:02d}", net_profit=float(day))
            )
        snapshots = db.get_daily_snapshots(limit=3)
        assert len(snapshots) == 3
        # Reverse chronological
        assert snapshots[0].date == "2025-01-05"

    def test_limit(self, db: TradeDatabase) -> None:
        for day in range(1, 11):
            db.save_daily_snapshot(DailySnapshot(date=f"2025-01-{day:02d}"))
        snapshots = db.get_daily_snapshots(limit=5)
        assert len(snapshots) == 5


# ---------------------------------------------------------------------------
# Portfolio State
# ---------------------------------------------------------------------------


class TestPortfolioState:
    def test_initially_none(self, db: TradeDatabase) -> None:
        assert db.get_portfolio_state() is None

    def test_save_and_retrieve(self, db: TradeDatabase) -> None:
        state = PortfolioState(
            updated_at=time.time(),
            total_equity=1050.0,
            total_exposure=200.0,
            open_positions=3,
            total_trades=15,
            total_pnl=50.0,
        )
        db.save_portfolio_state(state)
        result = db.get_portfolio_state()
        assert result is not None
        assert result.total_equity == pytest.approx(1050.0)
        assert result.open_positions == 3

    def test_upsert(self, db: TradeDatabase) -> None:
        db.save_portfolio_state(PortfolioState(total_equity=1000.0))
        db.save_portfolio_state(PortfolioState(total_equity=1100.0))
        result = db.get_portfolio_state()
        assert result is not None
        assert result.total_equity == pytest.approx(1100.0)


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------


class TestStrategyBreakdown:
    def test_empty(self, db: TradeDatabase) -> None:
        breakdown = db.get_strategy_breakdown()
        assert breakdown == {}

    def test_breakdown(self, db: TradeDatabase) -> None:
        db.save_trade(_make_trade(strategy="arbitrage", cost=50.0, order_id="a"))
        db.save_trade(_make_trade(strategy="arbitrage", cost=60.0, order_id="b"))
        db.save_trade(_make_trade(strategy="price_lag", cost=30.0, order_id="c"))

        breakdown = db.get_strategy_breakdown()
        assert "arbitrage" in breakdown
        assert breakdown["arbitrage"]["count"] == 2
        assert breakdown["arbitrage"]["total_cost"] == pytest.approx(110.0)
        assert breakdown["price_lag"]["count"] == 1
        assert breakdown["price_lag"]["total_cost"] == pytest.approx(30.0)


class TestEquityCurve:
    def test_empty(self, db: TradeDatabase) -> None:
        curve = db.get_equity_curve()
        assert curve == []

    def test_equity_curve(self, db: TradeDatabase) -> None:
        for day in range(1, 6):
            db.save_daily_snapshot(
                DailySnapshot(
                    date=f"2025-01-{day:02d}",
                    sim_balance=1000.0 + day * 10,
                    net_profit=float(day * 10),
                )
            )
        curve = db.get_equity_curve()
        assert len(curve) == 5
        # Ascending order
        assert curve[0]["date"] == "2025-01-01"
        assert curve[0]["equity"] == pytest.approx(1010.0)
        assert curve[4]["equity"] == pytest.approx(1050.0)

    def test_equity_curve_limit(self, db: TradeDatabase) -> None:
        for day in range(1, 20):
            db.save_daily_snapshot(
                DailySnapshot(date=f"2025-01-{day:02d}", sim_balance=1000.0 + day)
            )
        curve = db.get_equity_curve(limit=5)
        assert len(curve) == 5


# ---------------------------------------------------------------------------
# Trade Results
# ---------------------------------------------------------------------------


def _make_trade_result(**overrides) -> TradeResult:
    """Create a TradeResult with sensible defaults."""
    defaults = dict(
        timestamp=time.time(),
        condition_id="cond_1",
        market_slug="will-btc-go-up",
        asset="BTC",
        strategy="arbitrage",
        was_hedged=True,
        yes_shares=100.0,
        no_shares=100.0,
        investment=95.0,
        gross_payout=100.0,
        net_profit=5.0,
        outcome="",
    )
    defaults.update(overrides)
    return TradeResult(**defaults)


class TestSaveTradeResult:
    def test_save_and_retrieve(self, db: TradeDatabase) -> None:
        result = _make_trade_result()
        row_id = db.save_trade_result(result)
        assert row_id >= 1

        results = db.get_trade_results(limit=10)
        assert len(results) == 1
        assert results[0].condition_id == "cond_1"
        assert results[0].net_profit == pytest.approx(5.0)
        assert results[0].was_hedged is True

    def test_round_trip_all_fields(self, db: TradeDatabase) -> None:
        result = _make_trade_result(
            condition_id="cond_xyz",
            market_slug="slug-test",
            asset="ETH",
            strategy="price_lag",
            was_hedged=False,
            yes_shares=50.0,
            no_shares=0.0,
            investment=25.0,
            gross_payout=50.0,
            net_profit=25.0,
            outcome="YES",
        )
        db.save_trade_result(result)
        r = db.get_trade_results(limit=1)[0]
        assert r.condition_id == "cond_xyz"
        assert r.market_slug == "slug-test"
        assert r.asset == "ETH"
        assert r.strategy == "price_lag"
        assert r.was_hedged is False
        assert r.yes_shares == pytest.approx(50.0)
        assert r.no_shares == pytest.approx(0.0)
        assert r.investment == pytest.approx(25.0)
        assert r.gross_payout == pytest.approx(50.0)
        assert r.net_profit == pytest.approx(25.0)
        assert r.outcome == "YES"


class TestGetTradeResults:
    def test_empty(self, db: TradeDatabase) -> None:
        results = db.get_trade_results()
        assert results == []

    def test_reverse_chronological_order(self, db: TradeDatabase) -> None:
        db.save_trade_result(_make_trade_result(timestamp=100.0, condition_id="old"))
        db.save_trade_result(_make_trade_result(timestamp=200.0, condition_id="new"))
        results = db.get_trade_results()
        assert results[0].timestamp > results[1].timestamp

    def test_limit(self, db: TradeDatabase) -> None:
        for i in range(10):
            db.save_trade_result(
                _make_trade_result(condition_id=f"cond_{i}", timestamp=time.time() + i)
            )
        results = db.get_trade_results(limit=3)
        assert len(results) == 3

    def test_offset(self, db: TradeDatabase) -> None:
        for i in range(10):
            db.save_trade_result(
                _make_trade_result(condition_id=f"cond_{i}", timestamp=1000.0 + i)
            )
        results = db.get_trade_results(limit=3, offset=7)
        assert len(results) == 3
        # Offset 7 in descending order means we get the 3 oldest
        assert results[0].timestamp == pytest.approx(1002.0)

    def test_pagination_full_sweep(self, db: TradeDatabase) -> None:
        for i in range(7):
            db.save_trade_result(
                _make_trade_result(condition_id=f"cond_{i}", timestamp=1000.0 + i)
            )
        page1 = db.get_trade_results(limit=3, offset=0)
        page2 = db.get_trade_results(limit=3, offset=3)
        page3 = db.get_trade_results(limit=3, offset=6)
        assert len(page1) == 3
        assert len(page2) == 3
        assert len(page3) == 1
        # All unique condition_ids
        all_cids = [r.condition_id for r in page1 + page2 + page3]
        assert len(set(all_cids)) == 7


class TestGetTradeResultCount:
    def test_empty(self, db: TradeDatabase) -> None:
        assert db.get_trade_result_count() == 0

    def test_count(self, db: TradeDatabase) -> None:
        for i in range(5):
            db.save_trade_result(
                _make_trade_result(condition_id=f"cond_{i}")
            )
        assert db.get_trade_result_count() == 5


# ---------------------------------------------------------------------------
# Database lifecycle
# ---------------------------------------------------------------------------


class TestDatabaseLifecycle:
    def test_close_and_reopen(self, tmp_path) -> None:
        db_path = str(tmp_path / "test.db")
        db = TradeDatabase(db_path)
        db.save_trade(_make_trade())
        db.close()

        db2 = TradeDatabase(db_path)
        assert db2.get_trade_count() == 1
        db2.close()

    def test_creates_parent_directory(self, tmp_path) -> None:
        db_path = str(tmp_path / "subdir" / "deep" / "test.db")
        db = TradeDatabase(db_path)
        db.save_trade(_make_trade())
        assert db.get_trade_count() == 1
        db.close()
