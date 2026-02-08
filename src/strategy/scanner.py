"""Market scanner that evaluates all markets against all enabled strategies.

Picks the single best opportunity (highest ``expected_profit_pct``) across
every (strategy, market) combination, or returns ``None`` when nothing is
viable.

Also supports parallel A/B testing mode where the best opportunity from
each strategy type is returned for simultaneous execution.

When divergence scoring is enabled, opportunities are ranked using a
composite score: ``alpha * profit_pct + (1 - alpha) * kl_divergence``.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict

from src.config import Settings
from src.core.models import Market, Opportunity, StrategyType
from src.monitoring.logger import get_logger
from src.strategy.base import BaseStrategy


class MarketScanner:
    """Evaluate all active markets against all enabled strategies.

    Returns the single best opportunity (highest expected_profit_pct)
    across all market-strategy combinations, or None if nothing viable.

    For A/B testing, use scan_best_per_strategy() to get the best
    opportunity from each strategy type for parallel execution.
    """

    def __init__(
        self,
        strategies: list[BaseStrategy],
        settings: Settings | None = None,
    ) -> None:
        self._strategies = strategies
        self._settings = settings
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
        """Return ALL opportunities sorted by ranking score descending.

        Uses ``evaluate_all(markets)`` for each strategy, which allows
        cross-market strategies to see all markets at once.  Default
        ``evaluate_all`` falls back to per-market ``evaluate`` calls.

        When divergence scoring is enabled, ranking uses a composite score:
        ``alpha * profit_pct + (1 - alpha) * kl_divergence``.
        Otherwise falls back to pure ``expected_profit_pct``.
        """
        if not self._strategies or not markets:
            return []

        # Evaluate each strategy against all markets via evaluate_all
        tasks: list[asyncio.Task[list[Opportunity]]] = []
        for strategy in self._strategies:
            tasks.append(
                asyncio.ensure_future(self._safe_evaluate_all(strategy, markets)),
            )

        results = await asyncio.gather(*tasks)

        # Flatten results
        opportunities: list[Opportunity] = []
        for strategy, opps in zip(self._strategies, results, strict=True):
            for opp in opps:
                self._log.debug(
                    "opportunity_found",
                    strategy=strategy.name,
                    market=opp.market.slug,
                    profit_pct=round(opp.expected_profit_pct, 6),
                )
                opportunities.append(opp)

        # Sort by ranking score
        opportunities.sort(key=self._ranking_key, reverse=True)

        return opportunities

    async def scan_best_per_strategy(
        self, markets: list[Market]
    ) -> dict[StrategyType, Opportunity]:
        """Return the best opportunity for EACH strategy type.

        This enables A/B testing where multiple strategies can execute
        in parallel. Each strategy type gets its own "best" opportunity
        rather than competing for a single slot.

        Returns a dict mapping StrategyType to the best Opportunity for
        that strategy (empty dict if no opportunities found).
        """
        all_opps = await self.scan_all(markets)
        if not all_opps:
            self._log.debug("scan_per_strategy_complete", strategies_with_opps=0)
            return {}

        # Group by strategy type
        by_strategy: dict[StrategyType, list[Opportunity]] = defaultdict(list)
        for opp in all_opps:
            by_strategy[opp.strategy].append(opp)

        # Pick the best from each group (already sorted by ranking score desc)
        best_per_strategy: dict[StrategyType, Opportunity] = {}
        for strat_type, opps in by_strategy.items():
            if opps:
                best_per_strategy[strat_type] = opps[0]

        self._log.info(
            "scan_per_strategy_complete",
            strategies_with_opps=len(best_per_strategy),
            strategy_types=[s.value for s in best_per_strategy],
        )

        return best_per_strategy

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

    def _ranking_key(self, opp: Opportunity) -> float:
        """Compute the ranking key for an opportunity.

        When divergence scoring is enabled:
            alpha * profit_pct + (1 - alpha) * kl_divergence

        Otherwise: pure expected_profit_pct.
        """
        if (
            self._settings is not None
            and self._settings.enable_divergence_scoring
        ):
            alpha = self._settings.divergence_ranking_alpha
            kl = opp.metadata.get("kl_kl_divergence", 0.0)
            if isinstance(kl, (int, float)):
                return alpha * opp.expected_profit_pct + (1.0 - alpha) * kl
        return opp.expected_profit_pct

    async def _safe_evaluate_all(
        self,
        strategy: BaseStrategy,
        markets: list[Market],
    ) -> list[Opportunity]:
        """Call ``strategy.evaluate_all(markets)`` with exception handling.

        If the strategy raises, log the error and return an empty list so
        that one failing strategy does not prevent the rest from being
        evaluated.
        """
        try:
            return await strategy.evaluate_all(markets)
        except Exception:
            self._log.exception(
                "strategy_evaluate_all_error",
                strategy=strategy.name,
            )
            return []
