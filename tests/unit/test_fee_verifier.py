"""Tests for src.utils.fee_verifier startup sanity checks."""

from __future__ import annotations

import pytest

from src.utils.fee_verifier import verify_fees, FeeVerificationError


# ---------------------------------------------------------------------------
# FeeVerificationError
# ---------------------------------------------------------------------------


class TestFeeVerificationError:
    """Tests for the FeeVerificationError data class."""

    def test_repr(self) -> None:
        err = FeeVerificationError(
            constant_name="TAKER_FEE_MAX_RATE",
            value=0.10,
            expected_range="[0.001, 0.05]",
            severity="error",
        )
        r = repr(err)
        assert "TAKER_FEE_MAX_RATE" in r
        assert "0.1" in r
        assert "error" in r
        assert "0.001, 0.05" in r

    def test_default_severity_is_warning(self) -> None:
        err = FeeVerificationError(
            constant_name="X", value=0.0, expected_range="[0, 1]"
        )
        assert err.severity == "warning"

    def test_attributes(self) -> None:
        err = FeeVerificationError(
            constant_name="MAKER_FEE_RATE",
            value=-0.01,
            expected_range="[0, ...]",
            severity="error",
        )
        assert err.constant_name == "MAKER_FEE_RATE"
        assert err.value == -0.01
        assert err.expected_range == "[0, ...]"
        assert err.severity == "error"


# ---------------------------------------------------------------------------
# verify_fees with default (valid) constants
# ---------------------------------------------------------------------------


class TestVerifyFeesDefaults:
    """Tests that verify_fees passes with the real fee constants."""

    def test_passes_with_defaults(self) -> None:
        """Current fee constants should all pass verification."""
        errors = verify_fees()
        assert errors == []

    def test_returns_list(self) -> None:
        """Return type is always a list."""
        result = verify_fees()
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# TAKER_FEE_MAX_RATE boundary checks
# ---------------------------------------------------------------------------


class TestTakerFeeMaxRate:
    """Tests for TAKER_FEE_MAX_RATE boundary verification."""

    def test_too_high(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """TAKER_FEE_MAX_RATE above 5% should produce an error."""
        import src.utils.fee_verifier as mod

        monkeypatch.setattr(mod, "TAKER_FEE_MAX_RATE", 0.10)
        errors = verify_fees()
        taker_errors = [e for e in errors if e.constant_name == "TAKER_FEE_MAX_RATE"]
        assert len(taker_errors) >= 1
        assert taker_errors[0].severity == "error"
        assert taker_errors[0].value == 0.10

    def test_too_low(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """TAKER_FEE_MAX_RATE below 0.1% should produce an error."""
        import src.utils.fee_verifier as mod

        monkeypatch.setattr(mod, "TAKER_FEE_MAX_RATE", 0.0001)
        errors = verify_fees()
        taker_errors = [e for e in errors if e.constant_name == "TAKER_FEE_MAX_RATE"]
        assert len(taker_errors) >= 1
        assert taker_errors[0].severity == "error"
        assert taker_errors[0].value == 0.0001

    def test_at_lower_bound(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Exactly at lower bound (0.001) should NOT trigger an error for the range check."""
        import src.utils.fee_verifier as mod

        monkeypatch.setattr(mod, "TAKER_FEE_MAX_RATE", 0.001)
        errors = verify_fees()
        # Range check passes, but cross-check will fail since taker_fee_rate(0.5)
        # still uses the real TAKER_FEE_MAX_RATE (0.0315) from fees module.
        range_errors = [
            e
            for e in errors
            if e.constant_name == "TAKER_FEE_MAX_RATE"
        ]
        assert len(range_errors) == 0

    def test_at_upper_bound(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Exactly at upper bound (0.05) should NOT trigger a range error."""
        import src.utils.fee_verifier as mod

        monkeypatch.setattr(mod, "TAKER_FEE_MAX_RATE", 0.05)
        errors = verify_fees()
        range_errors = [
            e
            for e in errors
            if e.constant_name == "TAKER_FEE_MAX_RATE"
        ]
        assert len(range_errors) == 0


# ---------------------------------------------------------------------------
# WINNER_FEE_RATE boundary checks
# ---------------------------------------------------------------------------


class TestWinnerFeeRate:
    """Tests for WINNER_FEE_RATE boundary verification."""

    def test_too_high(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """WINNER_FEE_RATE above 5% should produce an error."""
        import src.utils.fee_verifier as mod

        monkeypatch.setattr(mod, "WINNER_FEE_RATE", 0.10)
        errors = verify_fees()
        winner_errors = [e for e in errors if e.constant_name == "WINNER_FEE_RATE"]
        assert len(winner_errors) >= 1
        assert winner_errors[0].severity == "error"
        assert winner_errors[0].value == 0.10

    def test_too_low(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """WINNER_FEE_RATE below 0.5% should produce an error."""
        import src.utils.fee_verifier as mod

        monkeypatch.setattr(mod, "WINNER_FEE_RATE", 0.001)
        errors = verify_fees()
        winner_errors = [e for e in errors if e.constant_name == "WINNER_FEE_RATE"]
        assert len(winner_errors) >= 1
        assert winner_errors[0].severity == "error"
        assert winner_errors[0].value == 0.001

    def test_at_lower_bound(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Exactly at lower bound (0.005) should pass."""
        import src.utils.fee_verifier as mod

        monkeypatch.setattr(mod, "WINNER_FEE_RATE", 0.005)
        errors = verify_fees()
        winner_errors = [e for e in errors if e.constant_name == "WINNER_FEE_RATE"]
        assert len(winner_errors) == 0

    def test_at_upper_bound(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Exactly at upper bound (0.05) should pass."""
        import src.utils.fee_verifier as mod

        monkeypatch.setattr(mod, "WINNER_FEE_RATE", 0.05)
        errors = verify_fees()
        winner_errors = [e for e in errors if e.constant_name == "WINNER_FEE_RATE"]
        assert len(winner_errors) == 0


# ---------------------------------------------------------------------------
# MAKER_FEE_RATE boundary checks
# ---------------------------------------------------------------------------


class TestMakerFeeRate:
    """Tests for MAKER_FEE_RATE boundary verification."""

    def test_negative_produces_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Negative MAKER_FEE_RATE should produce an error-level issue."""
        import src.utils.fee_verifier as mod

        monkeypatch.setattr(mod, "MAKER_FEE_RATE", -0.01)
        errors = verify_fees()
        maker_errors = [e for e in errors if e.constant_name == "MAKER_FEE_RATE"]
        assert len(maker_errors) >= 1
        assert any(e.severity == "error" for e in maker_errors)

    def test_above_threshold_produces_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MAKER_FEE_RATE above 1% should produce a warning (not error)."""
        import src.utils.fee_verifier as mod

        monkeypatch.setattr(mod, "MAKER_FEE_RATE", 0.02)
        errors = verify_fees()
        maker_errors = [e for e in errors if e.constant_name == "MAKER_FEE_RATE"]
        assert len(maker_errors) == 1
        assert maker_errors[0].severity == "warning"
        assert maker_errors[0].value == 0.02

    def test_zero_is_valid(self) -> None:
        """Zero maker fee (the current value) should not produce any error."""
        errors = verify_fees()
        maker_errors = [e for e in errors if e.constant_name == "MAKER_FEE_RATE"]
        assert len(maker_errors) == 0

    def test_small_positive_is_valid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A small positive maker fee (below threshold) should pass."""
        import src.utils.fee_verifier as mod

        monkeypatch.setattr(mod, "MAKER_FEE_RATE", 0.005)
        errors = verify_fees()
        maker_errors = [e for e in errors if e.constant_name == "MAKER_FEE_RATE"]
        assert len(maker_errors) == 0


# ---------------------------------------------------------------------------
# Cross-check: taker_fee_rate(0.5) == TAKER_FEE_MAX_RATE
# ---------------------------------------------------------------------------


class TestCrossChecks:
    """Tests for the cross-check validations in verify_fees."""

    def test_taker_at_50_matches_max_rate(self) -> None:
        """With default constants, taker_fee_rate(0.5) should equal TAKER_FEE_MAX_RATE."""
        from src.utils.fees import taker_fee_rate, TAKER_FEE_MAX_RATE as real_max

        assert abs(taker_fee_rate(0.5) - real_max) < 1e-12
        # And verify_fees should not flag this
        errors = verify_fees()
        cross_errors = [e for e in errors if "taker_fee_rate(0.5)" in e.constant_name]
        assert len(cross_errors) == 0

    def test_taker_at_50_mismatch_detected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If TAKER_FEE_MAX_RATE is changed but taker_fee_rate uses the original,
        the cross-check should detect the mismatch."""
        import src.utils.fee_verifier as mod

        # Set the constant in fee_verifier to a value different from the real one.
        # taker_fee_rate(0.5) still returns 0.0315 (from fees module), but the
        # verifier thinks TAKER_FEE_MAX_RATE is 0.04.
        monkeypatch.setattr(mod, "TAKER_FEE_MAX_RATE", 0.04)
        errors = verify_fees()
        cross_errors = [e for e in errors if "taker_fee_rate(0.5)" in e.constant_name]
        assert len(cross_errors) == 1
        assert cross_errors[0].severity == "error"

    def test_extreme_fee_rates_are_small(self) -> None:
        """With defaults, fees at 0.01 and 0.99 should be small (cross-check passes)."""
        errors = verify_fees()
        extreme_errors = [
            e
            for e in errors
            if "taker_fee_rate(0.01)" in e.constant_name
            or "taker_fee_rate(0.99)" in e.constant_name
        ]
        assert len(extreme_errors) == 0


# ---------------------------------------------------------------------------
# Multiple errors at once
# ---------------------------------------------------------------------------


class TestMultipleErrors:
    """Test that multiple issues are all reported together."""

    def test_all_bad_constants(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When all constants are out of range, all errors are returned."""
        import src.utils.fee_verifier as mod

        monkeypatch.setattr(mod, "TAKER_FEE_MAX_RATE", 0.99)
        monkeypatch.setattr(mod, "WINNER_FEE_RATE", 0.99)
        monkeypatch.setattr(mod, "MAKER_FEE_RATE", -1.0)
        errors = verify_fees()
        constant_names = {e.constant_name for e in errors}
        assert "TAKER_FEE_MAX_RATE" in constant_names
        assert "WINNER_FEE_RATE" in constant_names
        assert "MAKER_FEE_RATE" in constant_names
        # Should have at least 3 errors (range violations) plus potential cross-check
        assert len(errors) >= 3
