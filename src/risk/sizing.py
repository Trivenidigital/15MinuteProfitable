"""Kelly criterion position sizing for the Polymarket trading bot."""

from __future__ import annotations

from src.monitoring.logger import get_logger

logger = get_logger(__name__)


class PositionSizer:
    """Compute position sizes using fractional Kelly criterion.

    Uses quarter-Kelly by default to reduce variance while maintaining
    positive expected growth.
    """

    def __init__(
        self,
        kelly_fraction: float = 0.25,
        min_size: float = 10.0,
        max_size: float = 500.0,
    ) -> None:
        if not 0.0 < kelly_fraction <= 1.0:
            raise ValueError(f"kelly_fraction must be in (0, 1], got {kelly_fraction}")
        self._kelly_fraction = kelly_fraction
        self._min_size = min_size
        self._max_size = max_size

    @property
    def kelly_fraction(self) -> float:
        return self._kelly_fraction

    def kelly_fraction_arb(self, edge: float, bankroll: float) -> float:
        """Compute Kelly-optimal size for an arbitrage (near-certain) bet.

        For arb, the 'probability of winning' is ~1.0, so the Kelly formula
        simplifies to: f* = edge / odds, and with binary markets where
        the payout is $1, f* ≈ edge.

        Args:
            edge: Expected profit per dollar risked (e.g. 0.02 for 2% edge).
            bankroll: Current available capital.

        Returns:
            Position size in dollars, clamped to [min_size, max_size].
        """
        if edge <= 0 or bankroll <= 0:
            return 0.0

        # For near-certain bets: Kelly fraction ≈ edge
        full_kelly = edge * bankroll
        sized = full_kelly * self._kelly_fraction
        return self._clamp(sized)

    def kelly_fraction_directional(
        self,
        win_rate: float,
        avg_win: float,
        avg_loss: float,
        bankroll: float,
    ) -> float:
        """Compute Kelly-optimal size for a directional (uncertain) bet.

        Uses the generalized Kelly formula:
            f* = (win_rate / avg_loss) - ((1 - win_rate) / avg_win)

        This is then multiplied by `kelly_fraction` (quarter-Kelly default)
        and the bankroll.

        Args:
            win_rate: Historical probability of winning (0-1).
            avg_win: Average profit on winning trades (positive number).
            avg_loss: Average loss on losing trades (positive number).
            bankroll: Current available capital.

        Returns:
            Position size in dollars, clamped to [min_size, max_size].
            Returns 0.0 if no edge or invalid inputs.
        """
        if win_rate <= 0 or win_rate >= 1:
            return 0.0
        if avg_win <= 0 or avg_loss <= 0 or bankroll <= 0:
            return 0.0

        # Generalized Kelly: f* = p/a - q/b
        # where p = win_rate, q = 1-win_rate, b = avg_win, a = avg_loss
        full_kelly_frac = (win_rate / avg_loss) - ((1 - win_rate) / avg_win)

        if full_kelly_frac <= 0:
            return 0.0

        sized = full_kelly_frac * bankroll * self._kelly_fraction
        return self._clamp(sized)

    def _clamp(self, size: float) -> float:
        """Clamp size to [min_size, max_size]. Returns 0.0 if below min."""
        if size < self._min_size:
            return 0.0
        return min(size, self._max_size)
