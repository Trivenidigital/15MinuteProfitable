"""Market scanner that evaluates all markets against all enabled strategies.

Picks the single best opportunity (highest ``expected_profit_pct``) across
every (strategy, market) combination, or returns ``None`` when nothing is
viable.
"""

from __future__ import annotations

import asyncio

from src.core.models import Market, Opportunity
from src.monitoring.logger import get_logger
from src.strategy.base import BaseStrategy


class MarketScanner:
    """Evaluate all active markets against all enabled strategies.

    Returns the single best opportunity (highest expected_profit_pct)
    across all market-strategy combinations, or None if nothing viable.
    """

    def __init__(self, strategies: list[BaseStrategy]) -> None:
        self._strategies = strategies
        self._log = get_logger("scanner")

    # -- public API -----------------------------------------------------------

    async def scan(self, markets: list[Market]) -> Opportunity | None:
        """Evaluate all markets against all strategies.

        For each (strategy, market) pair, call strategy.evaluate(market).
        Collect all non-None opportunities, sort by expected_profit_pct
        descending, return the best one.

        Returns None if no opportunities found.
        """
        opportunities = await self.scan_all(markets)
        if not opportunities:
            self._log.info("scan_complete", opportunities_found=0, best=None)
            return None

        best = opportunities[0]
        self._log.info(
            "scan_complete",
            opportunities_found=len(opportunities),
            best_strategy=best.strategy.value,
            best_market=best.market.slug,
            best_profit_pct=round(best.expected_profit_pct, 6),
        )
        return best

    async def scan_all(self, markets: list[Market]) -> list[Opportunity]:
        """Return ALL opportunities sorted by expected_profit_pct descending.

        Unlike scan() which returns only the best, this returns the full
        ranked list for logging/monitoring purposes.
        """
        if not self._strategies or not markets:
            return []

        # Build a coroutine for every (strategy, market) pair
        tasks: list[asyncio.Task[Opportunity | None]] = []
        task_labels: list[tuple[str, str]] = []
        for strategy in self._strategies:
            for market in markets:
                tasks.append(
                    asyncio.ensure_future(self._safe_evaluate(strategy, market)),
                )
                task_labels.append((strategy.name, market.slug))

        results = await asyncio.gather(*tasks)

        # Collect non-None results
        opportunities: list[Opportunity] = []
        for (strat_name, market_slug), result in zip(task_labels, results):
            if result is not None:
                self._log.debug(
                    "opportunity_found",
                    strategy=strat_name,
                    market=market_slug,
                    profit_pct=round(result.expected_profit_pct, 6),
                )
                opportunities.append(result)

        # Sort by expected_profit_pct descending (stable sort preserves
        # insertion order for equal values, ensuring deterministic tie-breaking)
        opportunities.sort(key=lambda o: o.expected_profit_pct, reverse=True)

        return opportunities

    # -- properties -----------------------------------------------------------

    @property
    def strategy_count(self) -> int:
        """Number of registered strategies."""
        return len(self._strategies)

    @property
    def strategy_names(self) -> list[str]:
        """Names of registered strategies."""
        return [s.name for s in self._strategies]

    # -- internals ------------------------------------------------------------

    async def _safe_evaluate(
        self,
        strategy: BaseStrategy,
        market: Market,
    ) -> Opportunity | None:
        """Call ``strategy.evaluate(market)`` with exception handling.

        If the strategy raises, log the error and return ``None`` so that
        one failing strategy does not prevent the rest from being evaluated.
        """
        try:
            return await strategy.evaluate(market)
        except Exception:
            self._log.exception(
                "strategy_evaluate_error",
                strategy=strategy.name,
                market=market.slug,
            )
            return None
