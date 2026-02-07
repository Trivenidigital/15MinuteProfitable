"""Structured decision logging for strategy scan cycles.

Captures every opportunity found, risk approval/rejection, and execution
outcome so that offline analysis can compute win rates and identify
parameter tuning opportunities.
"""

from __future__ import annotations

import json
import time

from src.core.models import Opportunity
from src.data.trade_db import StrategyDecision, TradeDatabase
from src.monitoring.logger import get_logger


class DecisionLogger:
    """Lightweight wrapper around TradeDatabase for decision capture.

    Used by the strategy loop in main.py to record the full lifecycle
    of each scan cycle: opportunities found, risk decisions, execution
    outcomes.
    """

    def __init__(self, trade_db: TradeDatabase) -> None:
        self._db = trade_db
        self._cycle_id = 0
        self._log = get_logger("decision_logger")

    @property
    def cycle_id(self) -> int:
        """Current scan cycle counter."""
        return self._cycle_id

    def next_cycle(self) -> int:
        """Increment and return the scan cycle counter."""
        self._cycle_id += 1
        return self._cycle_id

    def log_opportunity(self, cycle_id: int, opp: Opportunity) -> None:
        """Log an opportunity found by scanner."""
        decision = self._build_decision(cycle_id, opp, decision="opportunity")
        self._db.save_decision(decision)
        self._log.debug(
            "decision_logged",
            cycle_id=cycle_id,
            decision="opportunity",
            strategy=opp.strategy.value,
            market=opp.market.slug,
        )

    def log_risk_decision(
        self,
        cycle_id: int,
        opp: Opportunity,
        approved: bool,
        reason: str,
    ) -> None:
        """Log a risk manager approval/rejection."""
        if approved:
            dec = self._build_decision(
                cycle_id, opp, decision="risk_approved", rejection_reason=""
            )
        else:
            dec = self._build_decision(
                cycle_id, opp, decision="risk_rejected", rejection_reason=reason
            )
        self._db.save_decision(dec)
        self._log.debug(
            "decision_logged",
            cycle_id=cycle_id,
            decision="risk_approved" if approved else "risk_rejected",
            strategy=opp.strategy.value,
            market=opp.market.slug,
        )

    def log_execution(
        self, cycle_id: int, opp: Opportunity, success: bool
    ) -> None:
        """Log execution outcome."""
        decision = "executed" if success else "exec_failed"
        dec = self._build_decision(cycle_id, opp, decision=decision)
        self._db.save_decision(dec)
        self._log.debug(
            "decision_logged",
            cycle_id=cycle_id,
            decision=decision,
            strategy=opp.strategy.value,
            market=opp.market.slug,
        )

    def log_no_opportunities(self, cycle_id: int) -> None:
        """Log a scan cycle with 0 opportunities (lightweight, no per-market row)."""
        dec = StrategyDecision(
            timestamp=time.time(),
            cycle_id=cycle_id,
            decision="no_opportunities",
        )
        self._db.save_decision(dec)

    # -- internals ---------------------------------------------------------

    def _build_decision(
        self,
        cycle_id: int,
        opp: Opportunity,
        decision: str,
        rejection_reason: str = "",
    ) -> StrategyDecision:
        """Build a StrategyDecision from an Opportunity."""
        try:
            metadata_str = json.dumps(opp.metadata, default=str)
        except (TypeError, ValueError):
            metadata_str = "{}"

        return StrategyDecision(
            timestamp=time.time(),
            cycle_id=cycle_id,
            condition_id=opp.market.condition_id,
            market_slug=opp.market.slug,
            asset=opp.market.asset,
            strategy=opp.strategy.value,
            decision=decision,
            rejection_reason=rejection_reason,
            confidence=opp.confidence,
            expected_profit=opp.expected_profit,
            expected_profit_pct=opp.expected_profit_pct,
            metadata_json=metadata_str,
        )
