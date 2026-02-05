"""Comprehensive tests for src.utils.fees."""

from __future__ import annotations

import pytest

from src.utils.fees import (
    TAKER_FEE_MAX_RATE,
    WINNER_FEE_RATE,
    taker_fee_rate,
    taker_fee_amount,
    winner_fee_amount,
    net_arb_profit,
    min_combined_cost_for_profit,
)

# ---------------------------------------------------------------------------
# taker_fee_rate
# ---------------------------------------------------------------------------


class TestTakerFeeRate:
    """Tests for taker_fee_rate(price)."""

    @pytest.mark.parametrize(
        "price",
        [0.01, 0.10, 0.25, 0.40, 0.50, 0.60, 0.75, 0.90, 0.99],
    )
    def test_non_negative(self, price: float) -> None:
        """Fee rate must always be >= 0."""
        assert taker_fee_rate(price) >= 0.0

    @pytest.mark.parametrize(
        "price",
        [0.01, 0.10, 0.25, 0.40, 0.50, 0.60, 0.75, 0.90, 0.99],
    )
    def test_at_most_max(self, price: float) -> None:
        """Fee rate must never exceed the maximum rate."""
        assert taker_fee_rate(price) <= TAKER_FEE_MAX_RATE + 1e-12

    @pytest.mark.parametrize(
        "price",
        [0.10, 0.25, 0.40, 0.50, 0.60, 0.75, 0.90],
    )
    def test_symmetry(self, price: float) -> None:
        """taker_fee_rate(p) == taker_fee_rate(1 - p)."""
        assert taker_fee_rate(price) == pytest.approx(
            taker_fee_rate(1.0 - price), abs=1e-12
        )

    def test_max_at_fifty_fifty(self) -> None:
        """The peak fee rate occurs at price = 0.50 and equals TAKER_FEE_MAX_RATE."""
        rate = taker_fee_rate(0.50)
        # At 0.50: 4 * 0.5 * 0.5 = 1.0, so rate == max.
        assert rate == pytest.approx(TAKER_FEE_MAX_RATE, abs=1e-12)

    def test_near_zero_at_extremes(self) -> None:
        """Rates at 0.01 and 0.99 should be very small."""
        assert taker_fee_rate(0.01) < 0.005
        assert taker_fee_rate(0.99) < 0.005

    def test_clamping_below_range(self) -> None:
        """Prices below 0.01 are clamped to 0.01."""
        assert taker_fee_rate(-0.5) == taker_fee_rate(0.01)
        assert taker_fee_rate(0.0) == taker_fee_rate(0.01)

    def test_clamping_above_range(self) -> None:
        """Prices above 0.99 are clamped to 0.99."""
        assert taker_fee_rate(1.5) == taker_fee_rate(0.99)
        assert taker_fee_rate(1.0) == taker_fee_rate(0.99)

    @pytest.mark.parametrize(
        "price, expected_rate",
        [
            (0.50, TAKER_FEE_MAX_RATE * 4.0 * 0.50 * 0.50),
            (0.25, TAKER_FEE_MAX_RATE * 4.0 * 0.25 * 0.75),
            (0.10, TAKER_FEE_MAX_RATE * 4.0 * 0.10 * 0.90),
        ],
    )
    def test_exact_values(self, price: float, expected_rate: float) -> None:
        """Spot-check known calculations."""
        assert taker_fee_rate(price) == pytest.approx(expected_rate, abs=1e-12)


# ---------------------------------------------------------------------------
# taker_fee_amount
# ---------------------------------------------------------------------------


class TestTakerFeeAmount:
    """Tests for taker_fee_amount(price, size)."""

    def test_basic_calculation(self) -> None:
        """fee = rate * price * size."""
        price, size = 0.50, 100.0
        expected = taker_fee_rate(price) * price * size
        assert taker_fee_amount(price, size) == pytest.approx(expected, abs=1e-12)

    def test_zero_size(self) -> None:
        """Zero size means zero fee."""
        assert taker_fee_amount(0.50, 0.0) == 0.0

    def test_scales_linearly_with_size(self) -> None:
        """Doubling size should double the fee."""
        fee1 = taker_fee_amount(0.40, 50.0)
        fee2 = taker_fee_amount(0.40, 100.0)
        assert fee2 == pytest.approx(2.0 * fee1, abs=1e-12)


# ---------------------------------------------------------------------------
# winner_fee_amount
# ---------------------------------------------------------------------------


class TestWinnerFeeAmount:
    """Tests for winner_fee_amount(cost_basis, payout)."""

    def test_profitable_position(self) -> None:
        """2% fee on profit when payout > cost_basis."""
        fee = winner_fee_amount(cost_basis=0.40, payout=1.00)
        assert fee == pytest.approx(WINNER_FEE_RATE * 0.60, abs=1e-12)

    def test_no_profit(self) -> None:
        """No fee when payout <= cost_basis."""
        assert winner_fee_amount(cost_basis=0.60, payout=0.60) == 0.0

    def test_loss_position(self) -> None:
        """No fee on a losing position (payout < cost_basis)."""
        assert winner_fee_amount(cost_basis=0.80, payout=0.0) == 0.0

    def test_tiny_profit(self) -> None:
        """Very small profit still incurs the 2% fee."""
        fee = winner_fee_amount(cost_basis=0.99, payout=1.00)
        assert fee == pytest.approx(WINNER_FEE_RATE * 0.01, abs=1e-12)


# ---------------------------------------------------------------------------
# net_arb_profit
# ---------------------------------------------------------------------------


class TestNetArbProfit:
    """Tests for net_arb_profit(yes_price, no_price, size)."""

    def test_negative_at_fair_odds(self) -> None:
        """At 50/50 with no spread, arb should be deeply negative."""
        profit = net_arb_profit(0.50, 0.50, 100.0)
        assert profit < 0.0

    def test_negative_with_small_spread(self) -> None:
        """A typical small spread (0.48 + 0.48 = 0.96) should still be negative
        once fees are subtracted."""
        profit = net_arb_profit(0.48, 0.48, 100.0)
        assert profit < 0.0

    def test_positive_with_wide_spread(self) -> None:
        """A wide enough spread should produce positive arb profit.
        0.30 + 0.30 = 0.60 combined => 0.40 gross per share."""
        profit = net_arb_profit(0.30, 0.30, 100.0)
        assert profit > 0.0

    def test_scales_with_size(self) -> None:
        """Profit should scale linearly with size."""
        p1 = net_arb_profit(0.30, 0.30, 100.0)
        p2 = net_arb_profit(0.30, 0.30, 200.0)
        assert p2 == pytest.approx(2.0 * p1, abs=1e-8)

    def test_symmetric_prices(self) -> None:
        """net_arb_profit(a, b, s) == net_arb_profit(b, a, s) when
        the cheaper side is the same cost."""
        profit1 = net_arb_profit(0.40, 0.45, 100.0)
        profit2 = net_arb_profit(0.45, 0.40, 100.0)
        assert profit1 == pytest.approx(profit2, abs=1e-12)

    def test_combined_one_dollar_is_zero_gross(self) -> None:
        """When yes + no = 1.0, gross is zero so profit is negative (fees)."""
        profit = net_arb_profit(0.55, 0.45, 100.0)
        assert profit < 0.0


# ---------------------------------------------------------------------------
# min_combined_cost_for_profit
# ---------------------------------------------------------------------------


class TestMinCombinedCostForProfit:
    """Tests for min_combined_cost_for_profit(target)."""

    def test_zero_target_returns_below_one(self) -> None:
        """Break-even combined cost must be less than $1.00."""
        result = min_combined_cost_for_profit(0.0)
        assert 0.0 < result < 1.0

    def test_reasonable_range(self) -> None:
        """Break-even at 50/50 should be somewhere around 0.92-0.98
        (empirical sanity check)."""
        result = min_combined_cost_for_profit(0.0)
        assert 0.85 < result < 1.0

    def test_higher_target_means_lower_cost(self) -> None:
        """Requiring more profit per share should lower the max combined cost."""
        c0 = min_combined_cost_for_profit(0.0)
        c1 = min_combined_cost_for_profit(0.01)
        assert c1 < c0

    def test_rounded_to_four_decimals(self) -> None:
        """Result must be rounded to 4 decimal places."""
        result = min_combined_cost_for_profit(0.0)
        assert result == round(result, 4)
