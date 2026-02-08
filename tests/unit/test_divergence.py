"""Tests for src/utils/divergence.py — pure math, no side effects."""

from __future__ import annotations

import math

import pytest

from src.utils.divergence import (
    bregman_projection,
    compute_return_correlation,
    divergence_exit_signal,
    divergence_scaling_factor,
    kl_divergence_binary,
    market_mispricing_score,
    simplex_project,
)

# ---------------------------------------------------------------------------
# kl_divergence_binary
# ---------------------------------------------------------------------------


class TestKLDivergenceBinary:
    def test_identical_distributions_zero(self) -> None:
        assert kl_divergence_binary(0.5, 0.5) == pytest.approx(0.0, abs=1e-10)

    def test_identical_skewed(self) -> None:
        assert kl_divergence_binary(0.9, 0.9) == pytest.approx(0.0, abs=1e-10)

    def test_large_divergence_greater_than_small(self) -> None:
        large = kl_divergence_binary(0.9, 0.5)
        small = kl_divergence_binary(0.51, 0.5)
        assert large > small * 10  # much larger

    def test_non_negative(self) -> None:
        for p in [0.1, 0.3, 0.5, 0.7, 0.9]:
            for q in [0.1, 0.3, 0.5, 0.7, 0.9]:
                assert kl_divergence_binary(p, q) >= 0.0

    def test_asymmetric(self) -> None:
        kl_pq = kl_divergence_binary(0.8, 0.3)
        kl_qp = kl_divergence_binary(0.3, 0.8)
        assert kl_pq != pytest.approx(kl_qp, abs=0.01)

    def test_extreme_near_zero(self) -> None:
        # Should not raise or return inf due to epsilon clamping
        result = kl_divergence_binary(0.001, 0.999)
        assert math.isfinite(result)
        assert result > 0

    def test_exact_zero_one_clamped(self) -> None:
        result = kl_divergence_binary(0.0, 1.0)
        assert math.isfinite(result)


# ---------------------------------------------------------------------------
# simplex_project
# ---------------------------------------------------------------------------


class TestSimplexProject:
    def test_fair_market(self) -> None:
        # YES=0.50, NO=0.50 -> fair = 0.5
        assert simplex_project(0.50, 0.50) == pytest.approx(0.5)

    def test_overround_normalized(self) -> None:
        # YES=0.55, NO=0.50 -> total=1.05, fair = 0.55/1.05 ≈ 0.5238
        assert simplex_project(0.55, 0.50) == pytest.approx(0.55 / 1.05, abs=1e-4)

    def test_underround(self) -> None:
        # YES=0.45, NO=0.45 -> total=0.90, fair = 0.5
        assert simplex_project(0.45, 0.45) == pytest.approx(0.5)

    def test_skewed(self) -> None:
        # YES=0.90, NO=0.15 -> fair = 0.90/1.05 ≈ 0.857
        result = simplex_project(0.90, 0.15)
        assert 0.8 < result < 0.9

    def test_zero_total_fallback(self) -> None:
        assert simplex_project(0.0, 0.0) == 0.5


# ---------------------------------------------------------------------------
# market_mispricing_score
# ---------------------------------------------------------------------------


class TestMarketMispricingScore:
    def test_fair_market_low_divergence(self) -> None:
        score = market_mispricing_score(0.50, 0.50)
        assert score["p_fair"] == pytest.approx(0.5)
        assert score["kl_divergence"] == pytest.approx(0.0, abs=1e-10)
        assert score["overround"] == pytest.approx(0.0, abs=1e-10)

    def test_overround_positive(self) -> None:
        score = market_mispricing_score(0.55, 0.50)
        assert score["overround"] == pytest.approx(0.05)
        assert score["kl_divergence"] > 0

    def test_jensen_shannon_bounded(self) -> None:
        score = market_mispricing_score(0.80, 0.30)
        assert 0 <= score["jensen_shannon"] <= math.log(2) + 1e-6

    def test_all_keys_present(self) -> None:
        score = market_mispricing_score(0.60, 0.45)
        assert set(score.keys()) == {
            "p_fair", "kl_divergence", "kl_reverse", "jensen_shannon", "overround"
        }


# ---------------------------------------------------------------------------
# divergence_scaling_factor
# ---------------------------------------------------------------------------


class TestDivergenceScalingFactor:
    def test_below_low_returns_min(self) -> None:
        assert divergence_scaling_factor(0.0005) == pytest.approx(0.5)

    def test_above_high_returns_max(self) -> None:
        assert divergence_scaling_factor(0.1) == pytest.approx(2.0)

    def test_midpoint(self) -> None:
        mid_kl = (0.001 + 0.05) / 2
        result = divergence_scaling_factor(mid_kl)
        expected = (0.5 + 2.0) / 2
        assert result == pytest.approx(expected, abs=0.01)

    def test_at_low_boundary(self) -> None:
        assert divergence_scaling_factor(0.001) == pytest.approx(0.5)

    def test_at_high_boundary(self) -> None:
        assert divergence_scaling_factor(0.05) == pytest.approx(2.0)

    def test_custom_bounds(self) -> None:
        result = divergence_scaling_factor(
            0.5, low=0.0, high=1.0, min_scale=1.0, max_scale=3.0
        )
        assert result == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# compute_return_correlation
# ---------------------------------------------------------------------------


class TestComputeReturnCorrelation:
    def test_identity_for_identical_series(self) -> None:
        ts = [(float(i), 100.0 + i * 0.1) for i in range(100)]
        corr = compute_return_correlation([ts, ts])
        assert corr[0][0] == pytest.approx(1.0)
        assert corr[1][1] == pytest.approx(1.0)
        assert corr[0][1] == pytest.approx(1.0, abs=0.01)

    def test_identity_for_single_series(self) -> None:
        ts = [(float(i), 100.0 + i) for i in range(50)]
        corr = compute_return_correlation([ts])
        assert corr == [[1.0]]

    def test_empty_input(self) -> None:
        assert compute_return_correlation([]) == []

    def test_insufficient_data_returns_identity(self) -> None:
        ts_short = [(0.0, 100.0), (1.0, 101.0)]
        corr = compute_return_correlation([ts_short, ts_short])
        # Should return identity due to insufficient data
        assert corr[0][0] == 1.0
        assert corr[0][1] == 0.0

    def test_negatively_correlated(self) -> None:
        # Create anti-correlated return series by inverting the price changes
        import random

        random.seed(42)
        base_price = 100.0
        ts_up: list[tuple[float, float]] = [(0.0, base_price)]
        ts_down: list[tuple[float, float]] = [(0.0, base_price)]
        for i in range(1, 100):
            change = random.gauss(0, 0.5)
            ts_up.append((float(i), ts_up[-1][1] + change))
            ts_down.append((float(i), ts_down[-1][1] - change))
        corr = compute_return_correlation([ts_up, ts_down])
        assert corr[0][1] < 0  # negative correlation

    def test_three_assets(self) -> None:
        ts1 = [(float(i), 100.0 + i * 0.5) for i in range(50)]
        ts2 = [(float(i), 200.0 + i * 0.3) for i in range(50)]
        ts3 = [(float(i), 50.0 + i * 0.8) for i in range(50)]
        corr = compute_return_correlation([ts1, ts2, ts3])
        assert len(corr) == 3
        assert len(corr[0]) == 3
        # Diagonal should be 1.0
        for i in range(3):
            assert corr[i][i] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# bregman_projection
# ---------------------------------------------------------------------------


class TestBregmanProjection:
    def test_identity_correlation_unchanged(self) -> None:
        observed = [0.5, 0.6, 0.4, 0.7]
        identity = [[1 if i == j else 0 for j in range(4)] for i in range(4)]
        result = bregman_projection(observed, identity)
        # With identity correlation, no neighbor influence -> stays close
        for obs, proj in zip(observed, result, strict=True):
            assert proj == pytest.approx(obs, abs=0.01)

    def test_single_element(self) -> None:
        result = bregman_projection([0.6], [[1.0]])
        assert result == pytest.approx([0.6], abs=1e-6)

    def test_empty(self) -> None:
        assert bregman_projection([], []) == []

    def test_high_correlation_convergence(self) -> None:
        # All assets highly correlated -> should pull toward mean
        observed = [0.3, 0.8, 0.5, 0.6]
        corr = [[1.0, 0.95, 0.95, 0.95],
                [0.95, 1.0, 0.95, 0.95],
                [0.95, 0.95, 1.0, 0.95],
                [0.95, 0.95, 0.95, 1.0]]
        result = bregman_projection(observed, corr, num_iterations=200)
        # Should converge somewhat toward each other
        spread_before = max(observed) - min(observed)
        spread_after = max(result) - min(result)
        assert spread_after < spread_before

    def test_mismatched_dimensions_returns_copy(self) -> None:
        result = bregman_projection([0.5, 0.6], [[1.0]])
        assert result == [0.5, 0.6]

    def test_output_bounded(self) -> None:
        observed = [0.01, 0.99, 0.5]
        corr = [[1.0, 0.8, 0.8], [0.8, 1.0, 0.8], [0.8, 0.8, 1.0]]
        result = bregman_projection(observed, corr)
        for v in result:
            assert 0.0 < v < 1.0


# ---------------------------------------------------------------------------
# divergence_exit_signal
# ---------------------------------------------------------------------------


class TestDivergenceExitSignal:
    def test_decayed_below_threshold_exits(self) -> None:
        assert divergence_exit_signal(0.05, 0.02, "UP", threshold_ratio=0.5) is True

    def test_still_high_no_exit(self) -> None:
        assert divergence_exit_signal(0.05, 0.04, "UP", threshold_ratio=0.5) is False

    def test_zero_entry_no_exit(self) -> None:
        assert divergence_exit_signal(0.0, 0.01, "UP") is False

    def test_exact_threshold_no_exit(self) -> None:
        # current = entry * ratio -> not strictly less than
        assert divergence_exit_signal(0.10, 0.05, "DOWN", threshold_ratio=0.5) is False

    def test_just_below_exits(self) -> None:
        assert divergence_exit_signal(0.10, 0.049, "DOWN", threshold_ratio=0.5) is True
