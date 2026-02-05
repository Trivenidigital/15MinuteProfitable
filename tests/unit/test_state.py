"""Comprehensive tests for src.core.state.StateManager."""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

# Ensure BOT_PRIVATE_KEY is set before importing Settings
os.environ.setdefault("BOT_PRIVATE_KEY", "0x" + "ab" * 32)

from src.config import Settings
from src.core.models import (
    DailyPnL,
    FillEstimate,
    Market,
    Opportunity,
    OrderStatus,
    Position,
    Side,
    StrategyType,
    TradeOrder,
)
from src.core.state import StateManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_market(
    condition_id: str = "cond_abc123",
    slug: str = "will-btc-hit-100k",
    yes_token_id: str = "yes_token_1",
    no_token_id: str = "no_token_1",
    asset: str = "BTC",
) -> Market:
    """Create a dummy Market for testing."""
    now = datetime.utcnow()
    return Market(
        condition_id=condition_id,
        slug=slug,
        question=f"Will {asset} hit 100k?",
        yes_token_id=yes_token_id,
        no_token_id=no_token_id,
        start_time=now,
        end_time=now + timedelta(minutes=15),
        asset=asset,
    )


def _make_position(
    market: Market | None = None,
    yes_shares: float = 0.0,
    no_shares: float = 0.0,
    yes_cost_basis: float = 0.0,
    no_cost_basis: float = 0.0,
    strategy: StrategyType = StrategyType.ARBITRAGE,
) -> Position:
    """Create a dummy Position for testing."""
    if market is None:
        market = _make_market()
    return Position(
        market=market,
        yes_shares=yes_shares,
        no_shares=no_shares,
        yes_cost_basis=yes_cost_basis,
        no_cost_basis=no_cost_basis,
        strategy=strategy,
        opened_at=datetime.utcnow(),
    )


@pytest.fixture()
def settings() -> Settings:
    """Create Settings with defaults for testing."""
    return Settings(
        private_key="0x" + "ab" * 32,
        dry_run=True,
        sim_balance=1000.0,
    )


@pytest.fixture()
def state(settings: Settings) -> StateManager:
    """Create a fresh StateManager."""
    return StateManager(settings)


@pytest.fixture()
def market_btc() -> Market:
    return _make_market(condition_id="cond_btc", asset="BTC")


@pytest.fixture()
def market_eth() -> Market:
    return _make_market(
        condition_id="cond_eth",
        slug="will-eth-hit-10k",
        yes_token_id="yes_eth",
        no_token_id="no_eth",
        asset="ETH",
    )


# ---------------------------------------------------------------------------
# add_position / get_position / get_all_positions
# ---------------------------------------------------------------------------


class TestPositionTracking:
    """Tests for add_position, get_position, get_all_positions."""

    def test_add_and_get_position(self, state: StateManager, market_btc: Market) -> None:
        pos = _make_position(market=market_btc, yes_shares=100, yes_cost_basis=45.0)
        state.add_position(pos)

        retrieved = state.get_position("cond_btc")
        assert retrieved is pos
        assert retrieved.yes_shares == 100
        assert retrieved.yes_cost_basis == 45.0

    def test_get_position_returns_none_for_missing(self, state: StateManager) -> None:
        assert state.get_position("nonexistent") is None

    def test_add_duplicate_raises(self, state: StateManager, market_btc: Market) -> None:
        pos = _make_position(market=market_btc)
        state.add_position(pos)
        with pytest.raises(ValueError, match="already exists"):
            state.add_position(pos)

    def test_get_all_positions_empty(self, state: StateManager) -> None:
        assert state.get_all_positions() == []

    def test_get_all_positions_multiple(
        self, state: StateManager, market_btc: Market, market_eth: Market
    ) -> None:
        pos1 = _make_position(market=market_btc, yes_shares=50)
        pos2 = _make_position(market=market_eth, no_shares=30)
        state.add_position(pos1)
        state.add_position(pos2)

        all_pos = state.get_all_positions()
        assert len(all_pos) == 2
        condition_ids = {p.market.condition_id for p in all_pos}
        assert condition_ids == {"cond_btc", "cond_eth"}


# ---------------------------------------------------------------------------
# update_position
# ---------------------------------------------------------------------------


class TestUpdatePosition:
    """Tests for update_position with deltas."""

    def test_update_yes_shares(self, state: StateManager, market_btc: Market) -> None:
        pos = _make_position(market=market_btc, yes_shares=100, yes_cost_basis=45.0)
        state.add_position(pos)

        state.update_position("cond_btc", yes_shares_delta=50, yes_cost_delta=22.5)
        updated = state.get_position("cond_btc")
        assert updated.yes_shares == 150
        assert updated.yes_cost_basis == pytest.approx(67.5)

    def test_update_no_shares(self, state: StateManager, market_btc: Market) -> None:
        pos = _make_position(market=market_btc, no_shares=80, no_cost_basis=40.0)
        state.add_position(pos)

        state.update_position("cond_btc", no_shares_delta=20, no_cost_delta=10.0)
        updated = state.get_position("cond_btc")
        assert updated.no_shares == 100
        assert updated.no_cost_basis == pytest.approx(50.0)

    def test_update_both_sides(self, state: StateManager, market_btc: Market) -> None:
        pos = _make_position(market=market_btc)
        state.add_position(pos)

        state.update_position(
            "cond_btc",
            yes_shares_delta=100,
            no_shares_delta=100,
            yes_cost_delta=45.0,
            no_cost_delta=47.0,
        )
        updated = state.get_position("cond_btc")
        assert updated.yes_shares == 100
        assert updated.no_shares == 100
        assert updated.total_investment == pytest.approx(92.0)

    def test_update_missing_position_raises(self, state: StateManager) -> None:
        with pytest.raises(KeyError, match="No position found"):
            state.update_position("nonexistent", yes_shares_delta=10)

    def test_negative_deltas(self, state: StateManager, market_btc: Market) -> None:
        pos = _make_position(market=market_btc, yes_shares=100, yes_cost_basis=45.0)
        state.add_position(pos)

        state.update_position("cond_btc", yes_shares_delta=-30, yes_cost_delta=-13.5)
        updated = state.get_position("cond_btc")
        assert updated.yes_shares == 70
        assert updated.yes_cost_basis == pytest.approx(31.5)


# ---------------------------------------------------------------------------
# close_position
# ---------------------------------------------------------------------------


class TestClosePosition:
    """Tests for close_position with P&L calculation."""

    def test_profitable_close(self, state: StateManager, market_btc: Market) -> None:
        """Hedged position: 100 YES @ 0.45 + 100 NO @ 0.47 = $92 cost.
        Payout at $1/share for 200 shares = $200. Profit = $108."""
        pos = _make_position(
            market=market_btc,
            yes_shares=100,
            no_shares=100,
            yes_cost_basis=45.0,
            no_cost_basis=47.0,
        )
        state.add_position(pos)

        net = state.close_position("cond_btc", payout_per_share=1.0)
        assert net == pytest.approx(108.0)
        assert state.get_position("cond_btc") is None

    def test_losing_close(self, state: StateManager, market_btc: Market) -> None:
        """Position costs $92, payout is $0. Loss = -$92."""
        pos = _make_position(
            market=market_btc,
            yes_shares=100,
            no_shares=100,
            yes_cost_basis=45.0,
            no_cost_basis=47.0,
        )
        state.add_position(pos)

        net = state.close_position("cond_btc", payout_per_share=0.0)
        assert net == pytest.approx(-92.0)

    def test_close_updates_daily_pnl(self, state: StateManager, market_btc: Market) -> None:
        pos = _make_position(
            market=market_btc,
            yes_shares=100,
            no_shares=100,
            yes_cost_basis=45.0,
            no_cost_basis=47.0,
        )
        state.add_position(pos)
        state.close_position("cond_btc", payout_per_share=1.0)

        pnl = state.daily_pnl()
        assert pnl.gross_profit == pytest.approx(108.0)
        assert pnl.net_profit == pytest.approx(108.0)

    def test_close_missing_raises(self, state: StateManager) -> None:
        with pytest.raises(KeyError, match="No position found"):
            state.close_position("nonexistent", payout_per_share=1.0)

    def test_close_removes_position(self, state: StateManager, market_btc: Market) -> None:
        pos = _make_position(market=market_btc, yes_shares=50, yes_cost_basis=25.0)
        state.add_position(pos)
        state.close_position("cond_btc", payout_per_share=0.5)
        assert state.get_all_positions() == []

    def test_close_tracks_drawdown(self, state: StateManager, market_btc: Market) -> None:
        """A losing close should update max_drawdown if net_profit goes negative."""
        pos = _make_position(
            market=market_btc,
            yes_shares=100,
            no_shares=0,
            yes_cost_basis=90.0,
        )
        state.add_position(pos)
        state.close_position("cond_btc", payout_per_share=0.0)

        pnl = state.daily_pnl()
        assert pnl.max_drawdown == pytest.approx(-90.0)


# ---------------------------------------------------------------------------
# Exposure queries
# ---------------------------------------------------------------------------


class TestExposure:
    """Tests for total_exposure, market_exposure, total_unhedged_exposure."""

    def test_total_exposure_empty(self, state: StateManager) -> None:
        assert state.total_exposure() == 0.0

    def test_total_exposure_single(self, state: StateManager, market_btc: Market) -> None:
        pos = _make_position(
            market=market_btc, yes_cost_basis=45.0, no_cost_basis=47.0
        )
        state.add_position(pos)
        assert state.total_exposure() == pytest.approx(92.0)

    def test_total_exposure_multiple(
        self, state: StateManager, market_btc: Market, market_eth: Market
    ) -> None:
        state.add_position(_make_position(market=market_btc, yes_cost_basis=45.0))
        state.add_position(_make_position(market=market_eth, no_cost_basis=30.0))
        assert state.total_exposure() == pytest.approx(75.0)

    def test_market_exposure(self, state: StateManager, market_btc: Market) -> None:
        pos = _make_position(
            market=market_btc, yes_cost_basis=45.0, no_cost_basis=47.0
        )
        state.add_position(pos)
        assert state.market_exposure("cond_btc") == pytest.approx(92.0)

    def test_market_exposure_missing(self, state: StateManager) -> None:
        assert state.market_exposure("nonexistent") == 0.0

    def test_unhedged_exposure_hedged(self, state: StateManager, market_btc: Market) -> None:
        """Fully hedged: 100 YES + 100 NO. Net directional = 0."""
        pos = _make_position(
            market=market_btc,
            yes_shares=100,
            no_shares=100,
            yes_cost_basis=45.0,
            no_cost_basis=47.0,
        )
        state.add_position(pos)
        assert state.total_unhedged_exposure() == pytest.approx(0.0)

    def test_unhedged_exposure_one_sided(self, state: StateManager, market_btc: Market) -> None:
        """100 YES shares only, cost $45. Avg price = 0.45, net directional = 100.
        Unhedged = 100 * 0.45 = 45."""
        pos = _make_position(
            market=market_btc,
            yes_shares=100,
            no_shares=0,
            yes_cost_basis=45.0,
        )
        state.add_position(pos)
        assert state.total_unhedged_exposure() == pytest.approx(45.0)

    def test_unhedged_exposure_partial_hedge(self, state: StateManager, market_btc: Market) -> None:
        """80 YES + 20 NO. Net directional = 60. Total shares = 100. Cost = 92.
        Avg price = 0.92. Unhedged = 60 * 0.92 = 55.2."""
        pos = _make_position(
            market=market_btc,
            yes_shares=80,
            no_shares=20,
            yes_cost_basis=45.0,
            no_cost_basis=47.0,
        )
        state.add_position(pos)
        expected = 60 * (92.0 / 100)
        assert state.total_unhedged_exposure() == pytest.approx(expected)

    def test_unhedged_exposure_no_shares(self, state: StateManager, market_btc: Market) -> None:
        """Position with zero shares should contribute 0 unhedged exposure."""
        pos = _make_position(market=market_btc)
        state.add_position(pos)
        assert state.total_unhedged_exposure() == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# record_trade
# ---------------------------------------------------------------------------


class TestRecordTrade:
    """Tests for record_trade incrementing counters."""

    def _make_opportunity(self, market: Market) -> Opportunity:
        return Opportunity(
            strategy=StrategyType.ARBITRAGE,
            market=market,
            timestamp=datetime.utcnow(),
            total_fees=1.50,
        )

    def _make_filled_order(
        self,
        token_id: str,
        side: Side = Side.BUY,
        fill_size: float = 100.0,
        fill_price: float = 0.45,
    ) -> TradeOrder:
        return TradeOrder(
            token_id=token_id,
            side=side,
            price=fill_price,
            size=fill_size,
            status=OrderStatus.FILLED,
            fill_size=fill_size,
            fill_price=fill_price,
        )

    def test_record_trade_creates_position(self, state: StateManager, market_btc: Market) -> None:
        opp = self._make_opportunity(market_btc)
        orders = [
            self._make_filled_order(market_btc.yes_token_id, Side.BUY, 100, 0.45),
            self._make_filled_order(market_btc.no_token_id, Side.BUY, 100, 0.47),
        ]
        state.record_trade(opp, orders)

        pos = state.get_position("cond_btc")
        assert pos is not None
        assert pos.yes_shares == 100
        assert pos.no_shares == 100
        assert pos.yes_cost_basis == pytest.approx(45.0)
        assert pos.no_cost_basis == pytest.approx(47.0)

    def test_record_trade_increments_counters(
        self, state: StateManager, market_btc: Market
    ) -> None:
        opp = self._make_opportunity(market_btc)
        orders = [
            self._make_filled_order(market_btc.yes_token_id),
            self._make_filled_order(market_btc.no_token_id),
        ]
        state.record_trade(opp, orders)

        pnl = state.daily_pnl()
        assert pnl.trades == 2
        assert pnl.opportunities_seen == 1
        assert pnl.opportunities_taken == 1

    def test_record_trade_records_fees(self, state: StateManager, market_btc: Market) -> None:
        opp = self._make_opportunity(market_btc)
        opp.total_fees = 2.50
        orders = [self._make_filled_order(market_btc.yes_token_id)]
        state.record_trade(opp, orders)

        pnl = state.daily_pnl()
        assert pnl.total_fees == pytest.approx(2.50)
        assert pnl.net_profit == pytest.approx(-2.50)

    def test_record_trade_no_filled_orders(
        self, state: StateManager, market_btc: Market
    ) -> None:
        """Unfilled orders should still increment opportunities_seen but not trades."""
        opp = self._make_opportunity(market_btc)
        cancelled_order = TradeOrder(
            token_id=market_btc.yes_token_id,
            side=Side.BUY,
            price=0.45,
            size=100,
            status=OrderStatus.CANCELLED,
        )
        state.record_trade(opp, [cancelled_order])

        pnl = state.daily_pnl()
        assert pnl.opportunities_seen == 1
        assert pnl.opportunities_taken == 0
        assert pnl.trades == 0

    def test_record_trade_sell_order(self, state: StateManager, market_btc: Market) -> None:
        """SELL orders should reduce shares."""
        # First create a position with shares
        pos = _make_position(
            market=market_btc,
            yes_shares=100,
            yes_cost_basis=45.0,
        )
        state.add_position(pos)

        opp = self._make_opportunity(market_btc)
        opp.total_fees = 0.0
        sell_order = self._make_filled_order(
            market_btc.yes_token_id, Side.SELL, 50, 0.50
        )
        state.record_trade(opp, [sell_order])

        updated = state.get_position("cond_btc")
        assert updated.yes_shares == 50
        assert updated.yes_cost_basis == pytest.approx(20.0)  # 45.0 - (0.50 * 50)

    def test_record_trade_debits_sim_balance(
        self, state: StateManager, market_btc: Market
    ) -> None:
        """In dry_run mode, buying should debit sim balance."""
        initial = state.sim_balance
        opp = self._make_opportunity(market_btc)
        opp.total_fees = 0.0
        orders = [self._make_filled_order(market_btc.yes_token_id, Side.BUY, 100, 0.45)]
        state.record_trade(opp, orders)

        assert state.sim_balance == pytest.approx(initial - 45.0)


# ---------------------------------------------------------------------------
# record_fee / daily_pnl
# ---------------------------------------------------------------------------


class TestPnLTracking:
    """Tests for record_fee and daily_pnl."""

    def test_record_fee(self, state: StateManager) -> None:
        state.record_fee(1.25, "taker")
        pnl = state.daily_pnl()
        assert pnl.total_fees == pytest.approx(1.25)
        assert pnl.net_profit == pytest.approx(-1.25)

    def test_record_multiple_fees(self, state: StateManager) -> None:
        state.record_fee(1.0, "taker")
        state.record_fee(0.5, "winner")
        pnl = state.daily_pnl()
        assert pnl.total_fees == pytest.approx(1.5)
        assert pnl.net_profit == pytest.approx(-1.5)

    def test_daily_pnl_returns_today(self, state: StateManager) -> None:
        pnl = state.daily_pnl()
        assert pnl.date == date.today().isoformat()

    def test_daily_pnl_default_values(self, state: StateManager) -> None:
        pnl = state.daily_pnl()
        assert pnl.trades == 0
        assert pnl.gross_profit == 0.0
        assert pnl.total_fees == 0.0
        assert pnl.net_profit == 0.0
        assert pnl.opportunities_seen == 0
        assert pnl.opportunities_taken == 0
        assert pnl.max_drawdown == 0.0


# ---------------------------------------------------------------------------
# sim_debit / sim_credit / sim_balance
# ---------------------------------------------------------------------------


class TestSimBalance:
    """Tests for sim_debit, sim_credit, and sim_balance property."""

    def test_initial_balance(self, state: StateManager) -> None:
        assert state.sim_balance == pytest.approx(1000.0)

    def test_sim_debit_success(self, state: StateManager) -> None:
        result = state.sim_debit(100.0)
        assert result is True
        assert state.sim_balance == pytest.approx(900.0)

    def test_sim_debit_insufficient(self, state: StateManager) -> None:
        result = state.sim_debit(2000.0)
        assert result is False
        assert state.sim_balance == pytest.approx(1000.0)  # unchanged

    def test_sim_debit_exact_balance(self, state: StateManager) -> None:
        result = state.sim_debit(1000.0)
        assert result is True
        assert state.sim_balance == pytest.approx(0.0)

    def test_sim_debit_negative_raises(self, state: StateManager) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            state.sim_debit(-10.0)

    def test_sim_credit(self, state: StateManager) -> None:
        state.sim_credit(250.0)
        assert state.sim_balance == pytest.approx(1250.0)

    def test_sim_credit_negative_raises(self, state: StateManager) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            state.sim_credit(-10.0)

    def test_debit_then_credit(self, state: StateManager) -> None:
        state.sim_debit(300.0)
        state.sim_credit(100.0)
        assert state.sim_balance == pytest.approx(800.0)


# ---------------------------------------------------------------------------
# save_snapshot / load_snapshot round-trip
# ---------------------------------------------------------------------------


class TestSnapshotPersistence:
    """Tests for save_snapshot and load_snapshot."""

    def test_round_trip(
        self, state: StateManager, market_btc: Market, tmp_path: Path
    ) -> None:
        """Saving and loading should restore identical state."""
        pos = _make_position(
            market=market_btc,
            yes_shares=100,
            no_shares=80,
            yes_cost_basis=45.0,
            no_cost_basis=38.0,
        )
        state.add_position(pos)
        state.sim_debit(50.0)
        state.record_fee(1.25, "taker")

        snapshot_path = str(tmp_path / "snapshot.json")
        state.save_snapshot(snapshot_path)

        # Create a new StateManager and load
        new_state = StateManager(Settings(
            private_key="0x" + "ab" * 32,
            dry_run=True,
            sim_balance=0.0,  # will be overwritten by load
        ))
        result = new_state.load_snapshot(snapshot_path)
        assert result is True

        # Verify positions restored
        restored = new_state.get_position("cond_btc")
        assert restored is not None
        assert restored.yes_shares == 100
        assert restored.no_shares == 80
        assert restored.yes_cost_basis == pytest.approx(45.0)
        assert restored.no_cost_basis == pytest.approx(38.0)
        assert restored.market.condition_id == "cond_btc"
        assert restored.market.asset == "BTC"
        assert restored.strategy == StrategyType.ARBITRAGE

        # Verify sim balance restored
        assert new_state.sim_balance == pytest.approx(950.0)

        # Verify daily P&L restored
        pnl = new_state.daily_pnl()
        assert pnl.total_fees == pytest.approx(1.25)

    def test_load_nonexistent_file(self, state: StateManager, tmp_path: Path) -> None:
        result = state.load_snapshot(str(tmp_path / "does_not_exist.json"))
        assert result is False

    def test_load_invalid_json(self, state: StateManager, tmp_path: Path) -> None:
        bad_file = tmp_path / "bad.json"
        bad_file.write_text("not valid json {{{{")
        result = state.load_snapshot(str(bad_file))
        assert result is False

    def test_load_missing_keys(self, state: StateManager, tmp_path: Path) -> None:
        bad_file = tmp_path / "incomplete.json"
        bad_file.write_text(json.dumps({"positions": {}}))
        result = state.load_snapshot(str(bad_file))
        assert result is False

    def test_snapshot_file_is_valid_json(
        self, state: StateManager, market_btc: Market, tmp_path: Path
    ) -> None:
        pos = _make_position(market=market_btc, yes_shares=50, yes_cost_basis=25.0)
        state.add_position(pos)

        snapshot_path = str(tmp_path / "test.json")
        state.save_snapshot(snapshot_path)

        data = json.loads(Path(snapshot_path).read_text())
        assert "positions" in data
        assert "daily_pnl" in data
        assert "sim_balance" in data
        assert "cond_btc" in data["positions"]

    def test_round_trip_empty_state(self, state: StateManager, tmp_path: Path) -> None:
        """Empty state should round-trip cleanly."""
        snapshot_path = str(tmp_path / "empty.json")
        state.save_snapshot(snapshot_path)

        new_state = StateManager(Settings(
            private_key="0x" + "ab" * 32,
            dry_run=True,
        ))
        assert new_state.load_snapshot(snapshot_path) is True
        assert new_state.get_all_positions() == []
        assert new_state.sim_balance == pytest.approx(1000.0)

    def test_round_trip_preserves_strategy(
        self, state: StateManager, tmp_path: Path
    ) -> None:
        """Strategy type should survive serialization."""
        market = _make_market(condition_id="cond_asym")
        pos = _make_position(
            market=market,
            yes_shares=50,
            yes_cost_basis=25.0,
            strategy=StrategyType.ASYMMETRIC,
        )
        state.add_position(pos)

        snapshot_path = str(tmp_path / "strat.json")
        state.save_snapshot(snapshot_path)

        new_state = StateManager(Settings(
            private_key="0x" + "ab" * 32,
            dry_run=True,
        ))
        new_state.load_snapshot(snapshot_path)
        restored = new_state.get_position("cond_asym")
        assert restored.strategy == StrategyType.ASYMMETRIC

    def test_round_trip_position_without_opened_at(
        self, state: StateManager, tmp_path: Path
    ) -> None:
        """Positions with opened_at=None should round-trip cleanly."""
        market = _make_market(condition_id="cond_none")
        pos = Position(market=market, yes_shares=10, yes_cost_basis=5.0)
        state.add_position(pos)

        snapshot_path = str(tmp_path / "no_opened.json")
        state.save_snapshot(snapshot_path)

        new_state = StateManager(Settings(
            private_key="0x" + "ab" * 32,
            dry_run=True,
        ))
        new_state.load_snapshot(snapshot_path)
        restored = new_state.get_position("cond_none")
        assert restored.opened_at is None
        assert restored.yes_shares == 10


# ---------------------------------------------------------------------------
# close_position win/loss tracking
# ---------------------------------------------------------------------------


class TestWinLossTracking:
    """Tests for win_count/loss_count in close_position."""

    def test_winning_trade_increments_win_count(
        self, state: StateManager, market_btc: Market
    ) -> None:
        pos = _make_position(
            market=market_btc, yes_shares=100, no_shares=100,
            yes_cost_basis=45.0, no_cost_basis=47.0,
        )
        state.add_position(pos)
        state.close_position("cond_btc", payout_per_share=1.0)  # profit
        pnl = state.daily_pnl()
        assert pnl.win_count == 1
        assert pnl.loss_count == 0

    def test_losing_trade_increments_loss_count(
        self, state: StateManager, market_btc: Market
    ) -> None:
        pos = _make_position(
            market=market_btc, yes_shares=100, no_shares=0,
            yes_cost_basis=90.0,
        )
        state.add_position(pos)
        state.close_position("cond_btc", payout_per_share=0.0)  # loss
        pnl = state.daily_pnl()
        assert pnl.win_count == 0
        assert pnl.loss_count == 1

    def test_breakeven_counts_as_win(
        self, state: StateManager, market_btc: Market
    ) -> None:
        pos = _make_position(
            market=market_btc, yes_shares=100, no_shares=0,
            yes_cost_basis=50.0,
        )
        state.add_position(pos)
        state.close_position("cond_btc", payout_per_share=0.5)  # exactly even
        pnl = state.daily_pnl()
        assert pnl.win_count == 1
        assert pnl.loss_count == 0

    def test_multiple_closes_accumulate(
        self, state: StateManager, market_btc: Market, market_eth: Market
    ) -> None:
        pos1 = _make_position(
            market=market_btc, yes_shares=100, no_shares=100,
            yes_cost_basis=45.0, no_cost_basis=47.0,
        )
        pos2 = _make_position(
            market=market_eth, yes_shares=50, no_shares=0,
            yes_cost_basis=45.0,
        )
        state.add_position(pos1)
        state.add_position(pos2)
        state.close_position("cond_btc", payout_per_share=1.0)  # win
        state.close_position("cond_eth", payout_per_share=0.0)  # loss
        pnl = state.daily_pnl()
        assert pnl.win_count == 1
        assert pnl.loss_count == 1


# ---------------------------------------------------------------------------
# startup_recovery
# ---------------------------------------------------------------------------


class TestStartupRecovery:
    """Tests for startup_recovery."""

    def test_recovery_no_snapshot(self, state: StateManager, tmp_path: Path) -> None:
        report = state.startup_recovery(str(tmp_path / "missing.json"))
        assert report["loaded"] is False
        assert report["orphaned_removed"] == []
        assert report["positions_restored"] == 0

    def test_recovery_removes_expired_positions(
        self, state: StateManager, tmp_path: Path
    ) -> None:
        # Create a position with an expired market
        now = datetime.utcnow()
        expired_market = Market(
            condition_id="cond_expired",
            slug="expired-test",
            question="Expired?",
            yes_token_id="yes_exp",
            no_token_id="no_exp",
            start_time=now - timedelta(hours=2),
            end_time=now - timedelta(hours=1),  # Expired 1h ago
            asset="BTC",
        )
        pos = _make_position(market=expired_market, yes_shares=50, yes_cost_basis=25.0)
        state.add_position(pos)

        snapshot_path = str(tmp_path / "recovery.json")
        state.save_snapshot(snapshot_path)

        new_state = StateManager(Settings(
            private_key="0x" + "ab" * 32, dry_run=True,
        ))
        report = new_state.startup_recovery(snapshot_path)
        assert report["loaded"] is True
        assert "cond_expired" in report["orphaned_removed"]
        assert report["positions_restored"] == 0
        assert new_state.get_position("cond_expired") is None

    def test_recovery_keeps_active_positions(
        self, state: StateManager, tmp_path: Path
    ) -> None:
        now = datetime.utcnow()
        active_market = Market(
            condition_id="cond_active",
            slug="active-test",
            question="Active?",
            yes_token_id="yes_act",
            no_token_id="no_act",
            start_time=now - timedelta(minutes=5),
            end_time=now + timedelta(minutes=10),
            asset="BTC",
        )
        pos = _make_position(market=active_market, yes_shares=50, yes_cost_basis=25.0)
        state.add_position(pos)

        snapshot_path = str(tmp_path / "recovery2.json")
        state.save_snapshot(snapshot_path)

        new_state = StateManager(Settings(
            private_key="0x" + "ab" * 32, dry_run=True,
        ))
        report = new_state.startup_recovery(snapshot_path)
        assert report["loaded"] is True
        assert report["orphaned_removed"] == []
        assert report["positions_restored"] == 1
        assert new_state.get_position("cond_active") is not None

    def test_recovery_mixed_positions(
        self, state: StateManager, tmp_path: Path
    ) -> None:
        now = datetime.utcnow()
        expired = Market(
            condition_id="cond_old",
            slug="old",
            question="Old?",
            yes_token_id="y1",
            no_token_id="n1",
            start_time=now - timedelta(hours=2),
            end_time=now - timedelta(hours=1),
            asset="BTC",
        )
        active = Market(
            condition_id="cond_new",
            slug="new",
            question="New?",
            yes_token_id="y2",
            no_token_id="n2",
            start_time=now - timedelta(minutes=5),
            end_time=now + timedelta(minutes=10),
            asset="ETH",
        )
        state.add_position(_make_position(market=expired, yes_shares=30, yes_cost_basis=15.0))
        state.add_position(_make_position(market=active, yes_shares=50, yes_cost_basis=25.0))

        snapshot_path = str(tmp_path / "recovery3.json")
        state.save_snapshot(snapshot_path)

        new_state = StateManager(Settings(
            private_key="0x" + "ab" * 32, dry_run=True,
        ))
        report = new_state.startup_recovery(snapshot_path)
        assert report["loaded"] is True
        assert len(report["orphaned_removed"]) == 1
        assert "cond_old" in report["orphaned_removed"]
        assert report["positions_restored"] == 1

    def test_recovery_preserves_sim_balance(
        self, state: StateManager, tmp_path: Path
    ) -> None:
        state.sim_debit(100.0)
        snapshot_path = str(tmp_path / "recovery4.json")
        state.save_snapshot(snapshot_path)

        new_state = StateManager(Settings(
            private_key="0x" + "ab" * 32, dry_run=True, sim_balance=0.0,
        ))
        report = new_state.startup_recovery(snapshot_path)
        assert report["loaded"] is True
        assert new_state.sim_balance == pytest.approx(900.0)


# ---------------------------------------------------------------------------
# close_position win/loss amount tracking
# ---------------------------------------------------------------------------


class TestWinLossAmountTracking:
    """Tests for total_win_amount/total_loss_amount in close_position."""

    def test_close_position_tracks_win_amount(
        self, state: StateManager, market_btc: Market
    ) -> None:
        """Profitable close should accumulate total_win_amount."""
        pos = _make_position(
            market=market_btc,
            yes_shares=100,
            no_shares=100,
            yes_cost_basis=45.0,
            no_cost_basis=47.0,
        )
        state.add_position(pos)
        # Payout = 200 * 1.0 = 200, cost = 92, profit = 108
        state.close_position("cond_btc", payout_per_share=1.0)

        pnl = state.daily_pnl()
        assert pnl.win_count == 1
        assert pnl.total_win_amount == pytest.approx(108.0)
        assert pnl.total_loss_amount == pytest.approx(0.0)

    def test_close_position_tracks_loss_amount(
        self, state: StateManager, market_btc: Market
    ) -> None:
        """Losing close should accumulate total_loss_amount (absolute value)."""
        pos = _make_position(
            market=market_btc,
            yes_shares=100,
            no_shares=0,
            yes_cost_basis=90.0,
        )
        state.add_position(pos)
        # Payout = 100 * 0.0 = 0, cost = 90, profit = -90
        state.close_position("cond_btc", payout_per_share=0.0)

        pnl = state.daily_pnl()
        assert pnl.loss_count == 1
        assert pnl.total_loss_amount == pytest.approx(90.0)
        assert pnl.total_win_amount == pytest.approx(0.0)

    def test_multiple_close_accumulates_amounts(
        self, state: StateManager, market_btc: Market, market_eth: Market
    ) -> None:
        """Multiple closes should sum win and loss amounts correctly."""
        # Win: cost 92, payout 200, profit = 108
        pos1 = _make_position(
            market=market_btc,
            yes_shares=100,
            no_shares=100,
            yes_cost_basis=45.0,
            no_cost_basis=47.0,
        )
        # Loss: cost 45, payout 0, loss = -45
        pos2 = _make_position(
            market=market_eth,
            yes_shares=50,
            no_shares=0,
            yes_cost_basis=45.0,
        )
        state.add_position(pos1)
        state.add_position(pos2)

        state.close_position("cond_btc", payout_per_share=1.0)  # win +108
        state.close_position("cond_eth", payout_per_share=0.0)  # loss -45

        pnl = state.daily_pnl()
        assert pnl.win_count == 1
        assert pnl.loss_count == 1
        assert pnl.total_win_amount == pytest.approx(108.0)
        assert pnl.total_loss_amount == pytest.approx(45.0)
