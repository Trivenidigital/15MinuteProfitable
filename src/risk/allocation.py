"""Dynamic strategy allocation based on rolling performance.

Scores each strategy (and optionally each strategy+asset pair) using
exponential-decay-weighted profit factor over a rolling window.
Strategies with higher recent profit factors get larger order sizes
(up to 3x base), while losing strategies get reduced sizes (down to
0.1x base).

Supports per-asset allocation: when an asset is provided, the manager
looks up a (strategy, asset) multiplier first, falling back to the
strategy-level multiplier if per-asset data is insufficient.

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
        # Per-asset allocations keyed by "strategy:asset" (e.g. "fade_panic:ETH")
        self._asset_allocations: dict[str, StrategyAllocation] = {}
        self._last_recalc: float = 0.0
        self._log = get_logger("allocation")

    @property
    def enabled(self) -> bool:
        return self._settings.enable_dynamic_allocation

    def get_allocated_size(
        self,
        strategy_type: StrategyType,
        base_size: float,
        asset: str = "",
    ) -> float:
        """Return the dynamically adjusted size for a strategy (and asset).

        Lookup order:
        1. Per-(strategy, asset) multiplier if asset is provided and data exists
        2. Per-strategy multiplier (fallback)
        3. base_size unchanged (no data)

        If dynamic allocation is disabled or insufficient data exists,
        returns base_size unchanged.
        """
        if not self.enabled:
            return base_size

        self._maybe_recalculate()

        # Try per-asset allocation first
        alloc: StrategyAllocation | None = None
        if asset:
            asset_key = f"{strategy_type.value}:{asset}"
            alloc = self._asset_allocations.get(asset_key)

        # Fall back to strategy-level allocation
        if alloc is None or not alloc.sufficient_data:
            alloc = self._allocations.get(strategy_type.value)

        if alloc is None or not alloc.sufficient_data:
            return base_size

        adjusted = base_size * alloc.multiplier
        self._log.debug(
            "allocation_applied",
            strategy=strategy_type.value,
            asset=asset or "all",
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

        # Also group by (strategy, asset) for per-asset allocation
        by_strategy_asset: dict[str, list[TradeResult]] = {}
        for r in results:
            key = f"{r.strategy}:{r.asset}"
            by_strategy_asset.setdefault(key, []).append(r)

        # --- Strategy-level allocation ---
        scores: dict[str, float] = {}
        trade_counts: dict[str, int] = {}
        for strat, strat_results in by_strategy.items():
            trade_counts[strat] = len(strat_results)
            if len(strat_results) < _MIN_TRADES_REQUIRED:
                continue
            scores[strat] = self._weighted_profit_factor(strat_results, now)

        new_allocations: dict[str, StrategyAllocation] = {}
        if scores:
            total_score = sum(scores.values())
            num_scored = len(scores)
            for strat, score in scores.items():
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
        else:
            self._log.info("allocation_recalc_no_data")

        # Log strategy-level changes
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

        # --- Per-asset allocation ---
        asset_scores: dict[str, float] = {}
        asset_counts: dict[str, int] = {}
        for key, key_results in by_strategy_asset.items():
            asset_counts[key] = len(key_results)
            if len(key_results) < _MIN_TRADES_REQUIRED:
                continue
            asset_scores[key] = self._weighted_profit_factor(key_results, now)

        new_asset_allocs: dict[str, StrategyAllocation] = {}
        if asset_scores:
            total_asset_score = sum(asset_scores.values())
            num_asset_scored = len(asset_scores)
            for key, score in asset_scores.items():
                raw_multiplier = (
                    (score / total_asset_score) * num_asset_scored
                    if total_asset_score > 0
                    else 1.0
                )
                clamped = max(_MIN_MULTIPLIER, min(_MAX_MULTIPLIER, raw_multiplier))
                new_asset_allocs[key] = StrategyAllocation(
                    strategy=key,
                    score=score,
                    multiplier=clamped,
                    trade_count=asset_counts.get(key, 0),
                    sufficient_data=True,
                )

            # Log per-asset changes
            for key, alloc in new_asset_allocs.items():
                old = self._asset_allocations.get(key)
                old_mult = old.multiplier if old else 1.0
                if abs(alloc.multiplier - old_mult) > 0.01:
                    self._log.info(
                        "asset_allocation_changed",
                        strategy_asset=key,
                        old_multiplier=round(old_mult, 3),
                        new_multiplier=round(alloc.multiplier, 3),
                        score=round(alloc.score, 3),
                        trades=alloc.trade_count,
                    )

        self._asset_allocations = new_asset_allocs

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
