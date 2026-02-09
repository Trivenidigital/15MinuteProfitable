"""Abstract base class for all trading strategies."""

from __future__ import annotations

from abc import ABC, abstractmethod

from src.config import Settings
from src.core.models import Market, Opportunity, Position, StrategyType
from src.data.orderbook import OrderBookManager
from src.monitoring.logger import get_logger


class BaseStrategy(ABC):
    """Abstract base for all trading strategies.

    Every concrete strategy must implement:

    * :meth:`evaluate` -- scan a market and return an :class:`Opportunity`
      if one exists (``None`` otherwise).
    * :meth:`should_exit` -- decide whether an existing position should be
      closed early.
    * :attr:`name` -- human-readable identifier (used for logging).
    * :attr:`strategy_type` -- the :class:`StrategyType` enum value.
    """

    def __init__(
        self,
        settings: Settings,
        book_manager: OrderBookManager,
    ) -> None:
        self._settings = settings
        self._book_manager = book_manager
        self._log = get_logger(self.name)

    # -- abstract interface ---------------------------------------------------

    @abstractmethod
    async def evaluate(self, market: Market) -> Opportunity | None:
        """Evaluate *market* for a trading opportunity.

        Returns an :class:`Opportunity` when one is found, or ``None``
        when no viable opportunity exists at this time.
        """

    @abstractmethod
    def should_exit(self, position: Position, market: Market) -> bool:
        """Return ``True`` if *position* in *market* should be exited early."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable strategy name (e.g. ``"arbitrage"``)."""

    @property
    @abstractmethod
    def strategy_type(self) -> StrategyType:
        """The :class:`StrategyType` enum member for this strategy."""

    # -- multi-market evaluation (override for cross-market strategies) ------

    async def evaluate_all(self, markets: list[Market]) -> list[Opportunity]:
        """Evaluate ALL markets simultaneously.

        Override for cross-market strategies that need to see all markets
        at once (e.g. cross-asset correlation).  Default implementation
        evaluates each market independently — identical to per-market calls.
        """
        results: list[Opportunity] = []
        for market in markets:
            opp = await self.evaluate(market)
            if opp is not None:
                results.append(opp)
        return results

    # -- helpers --------------------------------------------------------------

    def _maybe_invert(
        self, direction: str, market: Market,
    ) -> tuple[str, str]:
        """If ``invert_signals`` is enabled, flip direction and token.

        Returns ``(direction, target_token_id)``.
        """
        if self._settings.invert_signals:
            if direction == "UP":
                return "DOWN", market.no_token_id
            return "UP", market.yes_token_id
        if direction == "UP":
            return direction, market.yes_token_id
        return direction, market.no_token_id

    def _is_book_stale(self, token_id: str, threshold_s: float = 30.0) -> bool:
        """Return True if the orderbook for *token_id* is stale or missing."""
        return self._book_manager.is_stale(token_id, threshold_s)
