"""Tests for src.monitoring.metrics.MetricsCollector."""

from __future__ import annotations

import os

os.environ.setdefault("BOT_PRIVATE_KEY", "0x" + "ab" * 32)

import pytest

from src.core.models import DailyPnL
from src.monitoring.metrics import MetricsCollector


@pytest.fixture()
def collector() -> MetricsCollector:
    return MetricsCollector()


# ---------------------------------------------------------------------------
# compute_dashboard
# ---------------------------------------------------------------------------


class TestComputeDashboard:
    def test_basic_metrics(self, collector: MetricsCollector) -> None:
        pnl = DailyPnL(
            date="2024-01-01",
            trades=10,
            gross_profit=50.0,
            total_fees=5.0,
            net_profit=45.0,
            opportunities_seen=100,
            opportunities_taken=10,
            max_drawdown=-10.0,
            win_count=7,
            loss_count=3,
        )
        d = collector.compute_dashboard(pnl, sim_balance=1000.0)

        assert d["win_rate"] == pytest.approx(0.7)
        assert d["avg_profit_per_trade"] == pytest.approx(4.5)
        assert d["take_rate"] == pytest.approx(0.1)
        assert d["net_profit"] == pytest.approx(45.0)
        assert d["total_fees"] == pytest.approx(5.0)
        assert d["max_drawdown"] == pytest.approx(-10.0)
        assert d["sim_balance"] == pytest.approx(1000.0)

    def test_zero_trades(self, collector: MetricsCollector) -> None:
        pnl = DailyPnL(date="2024-01-01")
        d = collector.compute_dashboard(pnl, sim_balance=500.0)

        assert d["win_rate"] == 0.0
        assert d["avg_profit_per_trade"] == 0.0
        assert d["take_rate"] == 0.0
        assert d["trades"] == 0.0

    def test_zero_opportunities_seen(self, collector: MetricsCollector) -> None:
        pnl = DailyPnL(date="2024-01-01", opportunities_seen=0)
        d = collector.compute_dashboard(pnl, sim_balance=500.0)
        assert d["take_rate"] == 0.0

    def test_all_wins(self, collector: MetricsCollector) -> None:
        pnl = DailyPnL(
            date="2024-01-01",
            trades=5,
            win_count=5,
            loss_count=0,
            net_profit=100.0,
        )
        d = collector.compute_dashboard(pnl, sim_balance=1100.0)
        assert d["win_rate"] == pytest.approx(1.0)

    def test_all_losses(self, collector: MetricsCollector) -> None:
        pnl = DailyPnL(
            date="2024-01-01",
            trades=3,
            win_count=0,
            loss_count=3,
            net_profit=-30.0,
        )
        d = collector.compute_dashboard(pnl, sim_balance=970.0)
        assert d["win_rate"] == pytest.approx(0.0)

    def test_dashboard_keys(self, collector: MetricsCollector) -> None:
        pnl = DailyPnL(date="2024-01-01")
        d = collector.compute_dashboard(pnl, sim_balance=500.0)
        expected_keys = {
            "win_rate", "avg_profit_per_trade", "take_rate",
            "net_profit", "gross_profit", "total_fees", "max_drawdown",
            "sim_balance", "trades", "win_count", "loss_count",
            "opportunities_seen", "opportunities_taken",
            "avg_win", "avg_loss",
        }
        assert set(d.keys()) == expected_keys


# ---------------------------------------------------------------------------
# format_daily_summary
# ---------------------------------------------------------------------------


class TestFormatDailySummary:
    def test_formatted_output_contains_key_fields(self, collector: MetricsCollector) -> None:
        dashboard = {
            "trades": 10.0,
            "win_count": 7.0,
            "loss_count": 3.0,
            "win_rate": 0.7,
            "net_profit": 45.0,
            "gross_profit": 50.0,
            "total_fees": 5.0,
            "avg_profit_per_trade": 4.5,
            "avg_win": 8.0,
            "avg_loss": 5.0,
            "max_drawdown": -10.0,
            "take_rate": 0.1,
            "opportunities_seen": 100.0,
            "opportunities_taken": 10.0,
            "sim_balance": 1000.0,
        }
        text = collector.format_daily_summary(dashboard)

        assert "Trades: 10" in text
        assert "Win/Loss: 7/3" in text
        assert "Win Rate: 70.0%" in text
        assert "Net Profit: $45.00" in text
        assert "Balance: $1000.00" in text

    def test_format_zero_state(self, collector: MetricsCollector) -> None:
        dashboard = {
            "trades": 0.0,
            "win_count": 0.0,
            "loss_count": 0.0,
            "win_rate": 0.0,
            "net_profit": 0.0,
            "gross_profit": 0.0,
            "total_fees": 0.0,
            "avg_profit_per_trade": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "max_drawdown": 0.0,
            "take_rate": 0.0,
            "opportunities_seen": 0.0,
            "opportunities_taken": 0.0,
            "sim_balance": 500.0,
        }
        text = collector.format_daily_summary(dashboard)
        assert "Trades: 0" in text
        assert "Win Rate: 0.0%" in text

    def test_format_negative_profit(self, collector: MetricsCollector) -> None:
        dashboard = {
            "trades": 5.0,
            "win_count": 1.0,
            "loss_count": 4.0,
            "win_rate": 0.2,
            "net_profit": -25.0,
            "gross_profit": -20.0,
            "total_fees": 5.0,
            "avg_profit_per_trade": -5.0,
            "avg_win": 10.0,
            "avg_loss": 8.75,
            "max_drawdown": -30.0,
            "take_rate": 0.5,
            "opportunities_seen": 10.0,
            "opportunities_taken": 5.0,
            "sim_balance": 475.0,
        }
        text = collector.format_daily_summary(dashboard)
        assert "Net Profit: $-25.00" in text
        assert "Max Drawdown: $-30.00" in text

    def test_format_returns_multiline_string(self, collector: MetricsCollector) -> None:
        pnl = DailyPnL(date="2024-01-01", trades=1, win_count=1, net_profit=5.0)
        dashboard = collector.compute_dashboard(pnl, sim_balance=1005.0)
        text = collector.format_daily_summary(dashboard)
        assert text.count("\n") >= 5  # Multiple lines


# ---------------------------------------------------------------------------
# log_dashboard (smoke test)
# ---------------------------------------------------------------------------


class TestDashboardAvgWinLoss:
    """Tests for avg_win and avg_loss in compute_dashboard."""

    def test_dashboard_avg_win_loss(self, collector: MetricsCollector) -> None:
        pnl = DailyPnL(
            date="2024-01-01",
            trades=10,
            win_count=3,
            loss_count=2,
            total_win_amount=30.0,  # avg_win = 10.0
            total_loss_amount=20.0,  # avg_loss = 10.0
            net_profit=10.0,
        )
        d = collector.compute_dashboard(pnl, sim_balance=1000.0)
        assert d["avg_win"] == pytest.approx(10.0)
        assert d["avg_loss"] == pytest.approx(10.0)

    def test_dashboard_avg_win_loss_zero_counts(self, collector: MetricsCollector) -> None:
        pnl = DailyPnL(date="2024-01-01")
        d = collector.compute_dashboard(pnl, sim_balance=500.0)
        assert d["avg_win"] == 0.0
        assert d["avg_loss"] == 0.0

    def test_dashboard_avg_win_only(self, collector: MetricsCollector) -> None:
        pnl = DailyPnL(
            date="2024-01-01",
            trades=5,
            win_count=5,
            loss_count=0,
            total_win_amount=50.0,
            total_loss_amount=0.0,
            net_profit=50.0,
        )
        d = collector.compute_dashboard(pnl, sim_balance=1050.0)
        assert d["avg_win"] == pytest.approx(10.0)
        assert d["avg_loss"] == 0.0

    def test_dashboard_avg_loss_only(self, collector: MetricsCollector) -> None:
        pnl = DailyPnL(
            date="2024-01-01",
            trades=4,
            win_count=0,
            loss_count=4,
            total_win_amount=0.0,
            total_loss_amount=80.0,
            net_profit=-80.0,
        )
        d = collector.compute_dashboard(pnl, sim_balance=920.0)
        assert d["avg_win"] == 0.0
        assert d["avg_loss"] == pytest.approx(20.0)


class TestLogDashboard:
    def test_log_dashboard_does_not_raise(self, collector: MetricsCollector) -> None:
        pnl = DailyPnL(date="2024-01-01", trades=1)
        dashboard = collector.compute_dashboard(pnl, sim_balance=1000.0)
        # Should not raise
        collector.log_dashboard(dashboard)
