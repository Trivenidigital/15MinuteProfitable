"""Dynamic strategy allocation based on rolling performance.

Scores each strategy using exponential-decay-weighted profit factor
over a rolling window. Strategies with higher recent profit factors
get larger order sizes (up to 3x base), while losing strategies get
reduced sizes (down to 0.1x base).

Recalculates every 15 minutes. Falls back to static config sizes
when insufficient data (< 5 trades per strategy in the window).
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from src.config import Settings
from src.core.models import StrategyType
from src.data.trade_db import TradeDatabase, TradeResult
from src.monitoring.logger import get_logger

_MIN_MULTIPLIER = 0.1  # 10% of base size floor
_MAX_MULTIPLIER = 3.0  # 300% of base size ceiling
_MIN_TRADES_REQUIRED = 5  # per strategy before overriding static sizing
_WINDOW_SECONDS = 7200.0  # 2 hours
_HALF_LIFE_SECONDS = 1800.0  # 30 minutes
_RECALC_INTERVAL_SECONDS = 900.0  # 15 minutes


@dataclass
class StrategyAllocation:
    """Per-strategy allocation state."""

    strategy: str
    score: float  # weighted profit factor
    multiplier: float  # applied to base_size
    trade_count: int  # trades in the window
    sufficient_data: bool


class AllocationManager:
    """Dynamically adjusts order sizes based on recent strategy performance.

    Uses exponential-decay-weighted profit factor to score each strategy,
    then allocates proportionally. Multipliers are clamped between
    _MIN_MULTIPLIER and _MAX_MULTIPLIER.
    """

    def __init__(self, settings: Settings, trade_db: TradeDatabase) -> None:
        self._settings = settings
        self._trade_db = trade_db
        self._allocations: dict[str, StrategyAllocation] = {}
        self._last_recalc: float = 0.0
        self._log = get_logger("allocation")

    @property
    def enabled(self) -> bool:
        return self._settings.enable_dynamic_allocation

    def get_allocated_size(
        self, strategy_type: StrategyType, base_size: float
    ) -> float:
        """Return the dynamically adjusted size for a strategy.

        If dynamic allocation is disabled or insufficient data exists,
        returns base_size unchanged.
        """
        if not self.enabled:
            return base_size

        self._maybe_recalculate()

        alloc = self._allocations.get(strategy_type.value)
        if alloc is None or not alloc.sufficient_data:
            return base_size

        adjusted = base_size * alloc.multiplier
        self._log.debug(
            "allocation_applied",
            strategy=strategy_type.value,
            base_size=base_size,
            multiplier=round(alloc.multiplier, 3),
            adjusted_size=round(adjusted, 2),
        )
        return adjusted

    def get_all_allocations(self) -> dict[str, StrategyAllocation]:
        """Return current allocations (for dashboard/logging)."""
        return dict(self._allocations)

    def _maybe_recalculate(self) -> None:
        """Recalculate allocations if the interval has elapsed."""
        now = time.time()
        if now - self._last_recalc < _RECALC_INTERVAL_SECONDS:
            return
        self._recalculate(now)
        self._last_recalc = now

    def _recalculate(self, now: float) -> None:
        """Recompute allocation multipliers from recent trade results."""
        since_ts = now - _WINDOW_SECONDS
        results = self._trade_db.get_trade_results_since(since_ts)

        # Group by strategy
        by_strategy: dict[str, list[TradeResult]] = {}
        for r in results:
            by_strategy.setdefault(r.strategy, []).append(r)

        # Compute weighted profit factor per strategy
        scores: dict[str, float] = {}
        trade_counts: dict[str, int] = {}
        for strat, strat_results in by_strategy.items():
            trade_counts[strat] = len(strat_results)
            if len(strat_results) < _MIN_TRADES_REQUIRED:
                continue  # insufficient data, will use static size
            scores[strat] = self._weighted_profit_factor(strat_results, now)

        if not scores:
            self._allocations = {}
            self._log.info("allocation_recalc_no_data")
            return

        # Proportional allocation: score / sum(scores) * num_strategies
        total_score = sum(scores.values())
        num_scored = len(scores)

        new_allocations: dict[str, StrategyAllocation] = {}
        for strat, score in scores.items():
            # If all strategies scored equally, multiplier = 1.0
            raw_multiplier = (
                (score / total_score) * num_scored if total_score > 0 else 1.0
            )
            clamped = max(_MIN_MULTIPLIER, min(_MAX_MULTIPLIER, raw_multiplier))

            new_allocations[strat] = StrategyAllocation(
                strategy=strat,
                score=score,
                multiplier=clamped,
                trade_count=trade_counts.get(strat, 0),
                sufficient_data=True,
            )

        # Log changes
        for strat, alloc in new_allocations.items():
            old = self._allocations.get(strat)
            old_mult = old.multiplier if old else 1.0
            if abs(alloc.multiplier - old_mult) > 0.01:
                self._log.info(
                    "allocation_changed",
                    strategy=strat,
                    old_multiplier=round(old_mult, 3),
                    new_multiplier=round(alloc.multiplier, 3),
                    score=round(alloc.score, 3),
                    trades=alloc.trade_count,
                )

        self._allocations = new_allocations

    @staticmethod
    def _exponential_weight(trade_ts: float, now: float) -> float:
        """Weight that decays to 0.5 at half-life (30 min default)."""
        age = now - trade_ts
        return math.exp(-0.693 * age / _HALF_LIFE_SECONDS)

    @classmethod
    def _weighted_profit_factor(
        cls, results: list[TradeResult], now: float
    ) -> float:
        """Compute exponential-decay-weighted profit factor.

        profit_factor = weighted_gross_wins / weighted_gross_losses.
        Returns 2.0 for all-wins, 1.0 for no data.
        """
        weighted_wins = 0.0
        weighted_losses = 0.0
        for r in results:
            w = cls._exponential_weight(r.timestamp, now)
            if r.net_profit > 0:
                weighted_wins += r.net_profit * w
            elif r.net_profit < 0:
                weighted_losses += abs(r.net_profit) * w
        if weighted_losses == 0:
            return 2.0 if weighted_wins > 0 else 1.0
        return weighted_wins / weighted_losses
