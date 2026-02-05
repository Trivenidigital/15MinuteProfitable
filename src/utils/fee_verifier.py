"""Startup fee constant sanity checks.

Verifies that fee constants in src.utils.fees are within expected bounds.
Logs warnings if they look suspicious (e.g. changed by Polymarket).
"""

from __future__ import annotations

from src.monitoring.logger import get_logger
from src.utils.fees import TAKER_FEE_MAX_RATE, WINNER_FEE_RATE, MAKER_FEE_RATE

logger = get_logger(__name__)

# Known bounds for fee constants
_TAKER_MAX_RATE_LOWER = 0.001  # 0.1% absolute minimum
_TAKER_MAX_RATE_UPPER = 0.05   # 5% absolute maximum
_WINNER_FEE_LOWER = 0.005      # 0.5% absolute minimum
_WINNER_FEE_UPPER = 0.05       # 5% absolute maximum
_MAKER_FEE_UPPER = 0.01        # Maker fee should be near 0


class FeeVerificationError:
    """Result of a fee verification check."""

    def __init__(
        self,
        constant_name: str,
        value: float,
        expected_range: str,
        severity: str = "warning",
    ) -> None:
        self.constant_name = constant_name
        self.value = value
        self.expected_range = expected_range
        self.severity = severity

    def __repr__(self) -> str:
        return (
            f"FeeVerificationError({self.constant_name}={self.value}, "
            f"expected={self.expected_range}, severity={self.severity})"
        )


def verify_fees() -> list[FeeVerificationError]:
    """Check all fee constants are within expected bounds.

    Returns a list of FeeVerificationError for any issues found.
    Empty list means all checks passed.
    """
    errors: list[FeeVerificationError] = []

    # Check taker fee max rate
    if not (_TAKER_MAX_RATE_LOWER <= TAKER_FEE_MAX_RATE <= _TAKER_MAX_RATE_UPPER):
        errors.append(
            FeeVerificationError(
                constant_name="TAKER_FEE_MAX_RATE",
                value=TAKER_FEE_MAX_RATE,
                expected_range=f"[{_TAKER_MAX_RATE_LOWER}, {_TAKER_MAX_RATE_UPPER}]",
                severity="error",
            )
        )

    # Check winner fee rate
    if not (_WINNER_FEE_LOWER <= WINNER_FEE_RATE <= _WINNER_FEE_UPPER):
        errors.append(
            FeeVerificationError(
                constant_name="WINNER_FEE_RATE",
                value=WINNER_FEE_RATE,
                expected_range=f"[{_WINNER_FEE_LOWER}, {_WINNER_FEE_UPPER}]",
                severity="error",
            )
        )

    # Check maker fee is near zero
    if MAKER_FEE_RATE > _MAKER_FEE_UPPER:
        errors.append(
            FeeVerificationError(
                constant_name="MAKER_FEE_RATE",
                value=MAKER_FEE_RATE,
                expected_range=f"[0, {_MAKER_FEE_UPPER}]",
                severity="warning",
            )
        )

    if MAKER_FEE_RATE < 0:
        errors.append(
            FeeVerificationError(
                constant_name="MAKER_FEE_RATE",
                value=MAKER_FEE_RATE,
                expected_range="[0, ...]",
                severity="error",
            )
        )

    # Cross-check: at 50/50 odds, taker fee rate = MAX_RATE
    # (4 * 0.5 * 0.5 = 1.0, so rate == MAX_RATE)
    from src.utils.fees import taker_fee_rate

    fee_at_50 = taker_fee_rate(0.5)
    if abs(fee_at_50 - TAKER_FEE_MAX_RATE) > 1e-9:
        errors.append(
            FeeVerificationError(
                constant_name="taker_fee_rate(0.5)",
                value=fee_at_50,
                expected_range=f"\u2248 {TAKER_FEE_MAX_RATE}",
                severity="error",
            )
        )

    # Cross-check: taker fee rate at the clamped extremes should be small
    # (prices are clamped to [0.01, 0.99], so fee at 0.01 and 0.99 is near zero)
    fee_at_low = taker_fee_rate(0.01)
    fee_at_high = taker_fee_rate(0.99)
    extreme_threshold = 0.005  # fee rate at extremes should be < 0.5%
    if fee_at_low >= extreme_threshold:
        errors.append(
            FeeVerificationError(
                constant_name="taker_fee_rate(0.01)",
                value=fee_at_low,
                expected_range=f"< {extreme_threshold}",
                severity="error",
            )
        )

    if fee_at_high >= extreme_threshold:
        errors.append(
            FeeVerificationError(
                constant_name="taker_fee_rate(0.99)",
                value=fee_at_high,
                expected_range=f"< {extreme_threshold}",
                severity="error",
            )
        )

    # Log results
    if errors:
        for err in errors:
            if err.severity == "error":
                logger.error(
                    "fee_verification_failed",
                    constant=err.constant_name,
                    value=err.value,
                    expected=err.expected_range,
                )
            else:
                logger.warning(
                    "fee_verification_warning",
                    constant=err.constant_name,
                    value=err.value,
                    expected=err.expected_range,
                )
    else:
        logger.info(
            "fee_verification_passed",
            taker_max_rate=TAKER_FEE_MAX_RATE,
            winner_rate=WINNER_FEE_RATE,
            maker_rate=MAKER_FEE_RATE,
        )

    return errors
