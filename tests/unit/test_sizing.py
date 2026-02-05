"""Tests for src.risk.sizing.PositionSizer (Kelly criterion)."""

from __future__ import annotations

import os

os.environ.setdefault("BOT_PRIVATE_KEY", "0x" + "ab" * 32)

import pytest

from src.risk.sizing import PositionSizer


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

class TestConstruction:
    def test_default_quarter_kelly(self) -> None:
        sizer = PositionSizer()
        assert sizer.kelly_fraction == 0.25

    def test_custom_fraction(self) -> None:
        sizer = PositionSizer(kelly_fraction=0.5)
        assert sizer.kelly_fraction == 0.5

    def test_fraction_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="kelly_fraction"):
            PositionSizer(kelly_fraction=0.0)

    def test_fraction_negative_raises(self) -> None:
        with pytest.raises(ValueError, match="kelly_fraction"):
            PositionSizer(kelly_fraction=-0.1)

    def test_fraction_above_one_raises(self) -> None:
        with pytest.raises(ValueError, match="kelly_fraction"):
            PositionSizer(kelly_fraction=1.5)

    def test_fraction_exactly_one_ok(self) -> None:
        sizer = PositionSizer(kelly_fraction=1.0)
        assert sizer.kelly_fraction == 1.0


# ---------------------------------------------------------------------------
# kelly_fraction_arb
# ---------------------------------------------------------------------------

class TestKellyArb:
    def test_positive_edge(self) -> None:
        sizer = PositionSizer(kelly_fraction=0.25, min_size=5.0, max_size=500.0)
        # 2% edge, $10000 bankroll -> full kelly = 200, quarter = 50
        size = sizer.kelly_fraction_arb(0.02, 10_000.0)
        assert size == pytest.approx(50.0)

    def test_zero_edge_returns_zero(self) -> None:
        sizer = PositionSizer()
        assert sizer.kelly_fraction_arb(0.0, 10_000.0) == 0.0

    def test_negative_edge_returns_zero(self) -> None:
        sizer = PositionSizer()
        assert sizer.kelly_fraction_arb(-0.01, 10_000.0) == 0.0

    def test_zero_bankroll_returns_zero(self) -> None:
        sizer = PositionSizer()
        assert sizer.kelly_fraction_arb(0.05, 0.0) == 0.0

    def test_clamped_to_max(self) -> None:
        sizer = PositionSizer(kelly_fraction=1.0, max_size=100.0)
        # 50% edge, $10000 = full kelly 5000, clamped to 100
        size = sizer.kelly_fraction_arb(0.5, 10_000.0)
        assert size == 100.0

    def test_below_min_returns_zero(self) -> None:
        sizer = PositionSizer(kelly_fraction=0.25, min_size=10.0)
        # 0.1% edge, $1000 = full kelly 1.0, quarter = 0.25 -> below min
        size = sizer.kelly_fraction_arb(0.001, 1_000.0)
        assert size == 0.0

    def test_half_kelly(self) -> None:
        sizer = PositionSizer(kelly_fraction=0.5, min_size=5.0)
        # 5% edge, $2000 = full 100, half = 50
        size = sizer.kelly_fraction_arb(0.05, 2_000.0)
        assert size == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# kelly_fraction_directional
# ---------------------------------------------------------------------------

class TestKellyDirectional:
    def test_profitable_strategy(self) -> None:
        sizer = PositionSizer(kelly_fraction=0.25, min_size=5.0, max_size=500.0)
        # 60% win rate, avg_win=10, avg_loss=8
        # f* = 0.6/8 - 0.4/10 = 0.075 - 0.04 = 0.035
        # quarter kelly = 0.035 * 0.25 = 0.00875
        # size = 0.00875 * 10000 = 87.5
        size = sizer.kelly_fraction_directional(0.60, 10.0, 8.0, 10_000.0)
        assert size == pytest.approx(87.5)

    def test_no_edge_returns_zero(self) -> None:
        sizer = PositionSizer()
        # 40% win, avg_win=10, avg_loss=8 -> f* = 0.4/8 - 0.6/10 = 0.05-0.06 = -0.01
        size = sizer.kelly_fraction_directional(0.40, 10.0, 8.0, 10_000.0)
        assert size == 0.0

    def test_zero_win_rate_returns_zero(self) -> None:
        sizer = PositionSizer()
        assert sizer.kelly_fraction_directional(0.0, 10.0, 8.0, 10_000.0) == 0.0

    def test_win_rate_one_returns_zero(self) -> None:
        sizer = PositionSizer()
        assert sizer.kelly_fraction_directional(1.0, 10.0, 8.0, 10_000.0) == 0.0

    def test_zero_avg_win_returns_zero(self) -> None:
        sizer = PositionSizer()
        assert sizer.kelly_fraction_directional(0.6, 0.0, 8.0, 10_000.0) == 0.0

    def test_zero_avg_loss_returns_zero(self) -> None:
        sizer = PositionSizer()
        assert sizer.kelly_fraction_directional(0.6, 10.0, 0.0, 10_000.0) == 0.0

    def test_zero_bankroll_returns_zero(self) -> None:
        sizer = PositionSizer()
        assert sizer.kelly_fraction_directional(0.6, 10.0, 8.0, 0.0) == 0.0

    def test_clamped_to_max(self) -> None:
        sizer = PositionSizer(kelly_fraction=1.0, max_size=50.0)
        size = sizer.kelly_fraction_directional(0.60, 10.0, 8.0, 100_000.0)
        assert size == 50.0
