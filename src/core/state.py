"""Global state manager for positions, P&L, and market tracking."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from src.config import Settings
from src.core.models import (
    DailyPnL,
    Market,
    Opportunity,
    OrderStatus,
    Position,
    Side,
    StrategyType,
    TradeOrder,
)
from src.data.trade_db import TradeDatabase, TradeRecord
from src.monitoring.logger import get_logger

logger = get_logger(__name__)


def _market_to_dict(market: Market) -> dict[str, Any]:
    """Serialize a Market dataclass to a plain dictionary."""
    return {
        "condition_id": market.condition_id,
        "slug": market.slug,
        "question": market.question,
        "yes_token_id": market.yes_token_id,
        "no_token_id": market.no_token_id,
        "start_time": market.start_time.isoformat(),
        "end_time": market.end_time.isoformat(),
        "asset": market.asset,
        "neg_risk": market.neg_risk,
    }


class StateManager:
    """Global state for positions, P&L, and market tracking."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._positions: dict[str, Position] = {}
        self._daily_pnl: dict[str, DailyPnL] = {}
        self._sim_balance_value: float = settings.sim_balance
        self._trade_db: TradeDatabase | None = None
        logger.info(
            "state_manager_initialized",
            sim_balance=self._sim_balance_value,
            dry_run=settings.dry_run,
        )

    def set_trade_db(self, trade_db: TradeDatabase) -> None:
        """Attach a TradeDatabase for persistent trade recording."""
        self._trade_db = trade_db
        logger.info("trade_db_attached")

    @property
    def trade_db(self) -> TradeDatabase | None:
        """Return the attached TradeDatabase, if any."""
        return self._trade_db

    # ------------------------------------------------------------------
    # Position tracking (keyed by market condition_id)
    # ------------------------------------------------------------------

    def add_position(self, position: Position) -> None:
        """Add a new position. Raises ValueError if position already exists."""
        cid = position.market.condition_id
        if cid in self._positions:
            raise ValueError(f"Position already exists for market {cid}")
        self._positions[cid] = position
        logger.info(
            "position_added",
            condition_id=cid,
            strategy=position.strategy.value,
            yes_shares=position.yes_shares,
            no_shares=position.no_shares,
        )

    def update_position(
        self,
        condition_id: str,
        yes_shares_delta: float = 0,
        no_shares_delta: float = 0,
        yes_cost_delta: float = 0,
        no_cost_delta: float = 0,
    ) -> None:
        """Update an existing position with share/cost deltas."""
        pos = self._positions.get(condition_id)
        if pos is None:
            raise KeyError(f"No position found for market {condition_id}")

        pos.yes_shares += yes_shares_delta
        pos.no_shares += no_shares_delta
        pos.yes_cost_basis += yes_cost_delta
        pos.no_cost_basis += no_cost_delta

        logger.debug(
            "position_updated",
            condition_id=condition_id,
            yes_shares=pos.yes_shares,
            no_shares=pos.no_shares,
            total_investment=pos.total_investment,
        )

    def get_position(self, condition_id: str) -> Position | None:
        """Return the position for a market, or None if not tracked."""
        return self._positions.get(condition_id)

    def get_all_positions(self) -> list[Position]:
        """Return all tracked positions."""
        return list(self._positions.values())

    def close_position(self, condition_id: str, payout_per_share: float) -> float:
        """Close a position, calculate net profit, update daily P&L.

        The payout is applied to ALL shares (yes + no) at the given rate.
        For arbitrage positions that hold both sides, the winning side
        pays out at 1.0 and the losing side at 0.0 -- the caller should
        pass 1.0 as ``payout_per_share`` because one side always wins.

        Returns:
            The net profit (payout - total_investment).
        """
        pos = self._positions.get(condition_id)
        if pos is None:
            raise KeyError(f"No position found for market {condition_id}")

        total_shares = pos.yes_shares + pos.no_shares
        gross_payout = total_shares * payout_per_share
        net_profit = gross_payout - pos.total_investment

        # Update daily P&L
        pnl = self._get_or_create_daily_pnl()
        pnl.gross_profit += net_profit
        pnl.net_profit += net_profit
        if pnl.net_profit < pnl.max_drawdown:
            pnl.max_drawdown = pnl.net_profit
        if net_profit >= 0:
            pnl.win_count += 1
            pnl.total_win_amount += net_profit
        else:
            pnl.loss_count += 1
            pnl.total_loss_amount += abs(net_profit)

        # Credit sim balance with the payout
        if self._settings.dry_run:
            self._sim_balance_value += gross_payout

        del self._positions[condition_id]

        logger.info(
            "position_closed",
            condition_id=condition_id,
            net_profit=net_profit,
            gross_payout=gross_payout,
            total_investment=pos.total_investment,
        )
        return net_profit

    # ------------------------------------------------------------------
    # Exposure queries
    # ------------------------------------------------------------------

    def total_exposure(self) -> float:
        """Sum of total_investment across all positions."""
        return sum(p.total_investment for p in self._positions.values())

    def market_exposure(self, condition_id: str) -> float:
        """total_investment for a specific market. Returns 0.0 if no position."""
        pos = self._positions.get(condition_id)
        return pos.total_investment if pos else 0.0

    def total_unhedged_exposure(self) -> float:
        """Sum of abs(net_directional_exposure * avg_price) across all positions.

        For each position, avg_price is total_investment / total_shares (or 0 if
        no shares are held).
        """
        total = 0.0
        for pos in self._positions.values():
            total_shares = pos.yes_shares + pos.no_shares
            if total_shares > 0:
                avg_price = pos.total_investment / total_shares
            else:
                avg_price = 0.0
            total += abs(pos.net_directional_exposure) * avg_price
        return total

    # ------------------------------------------------------------------
    # P&L tracking
    # ------------------------------------------------------------------

    def record_trade(self, opportunity: Opportunity, orders: list[TradeOrder]) -> None:
        """Record a completed trade: update position, increment trade count, record fees.

        Only FILLED or PARTIALLY_FILLED orders are counted.
        """
        pnl = self._get_or_create_daily_pnl()
        pnl.opportunities_seen += 1

        filled_orders = [
            o for o in orders
            if o.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED)
        ]
        if not filled_orders:
            return

        pnl.opportunities_taken += 1
        pnl.trades += len(filled_orders)

        # Record fees from the opportunity
        if opportunity.total_fees > 0:
            pnl.total_fees += opportunity.total_fees
            pnl.net_profit -= opportunity.total_fees

        # Update or create position
        cid = opportunity.market.condition_id
        pos = self._positions.get(cid)
        if pos is None:
            pos = Position(
                market=opportunity.market,
                strategy=opportunity.strategy,
                opened_at=opportunity.timestamp,
            )
            self._positions[cid] = pos

        for order in filled_orders:
            is_yes = order.token_id == opportunity.market.yes_token_id
            cost = order.fill_price * order.fill_size

            if order.side == Side.BUY:
                if is_yes:
                    pos.yes_shares += order.fill_size
                    pos.yes_cost_basis += cost
                else:
                    pos.no_shares += order.fill_size
                    pos.no_cost_basis += cost
            else:
                # SELL reduces shares
                if is_yes:
                    pos.yes_shares -= order.fill_size
                    pos.yes_cost_basis -= cost
                else:
                    pos.no_shares -= order.fill_size
                    pos.no_cost_basis -= cost

            # Debit sim balance for buys
            if self._settings.dry_run and order.side == Side.BUY:
                self._sim_balance_value -= cost

        # Persist to SQLite if trade_db is attached
        if self._trade_db is not None:
            self._persist_trades(opportunity, filled_orders)

        logger.info(
            "trade_recorded",
            condition_id=cid,
            filled_orders=len(filled_orders),
            total_fees=opportunity.total_fees,
        )

    def _persist_trades(
        self, opportunity: Opportunity, filled_orders: list[TradeOrder]
    ) -> None:
        """Write filled orders to the attached TradeDatabase."""
        import json
        import time as _time

        assert self._trade_db is not None
        market = opportunity.market
        per_order_fee = (
            opportunity.total_fees / len(filled_orders) if filled_orders else 0.0
        )
        for order in filled_orders:
            is_yes = order.token_id == market.yes_token_id
            record = TradeRecord(
                timestamp=_time.time(),
                condition_id=market.condition_id,
                market_slug=market.slug,
                asset=market.asset,
                strategy=opportunity.strategy.value,
                side=order.side.value,
                token_side="YES" if is_yes else "NO",
                price=order.fill_price,
                size=order.fill_size,
                cost=order.fill_price * order.fill_size,
                order_type=order.order_type,
                order_id=order.order_id or "",
                status=order.status.value,
                fees=per_order_fee,
                expected_profit=opportunity.expected_profit,
                metadata_json=json.dumps(opportunity.metadata),
            )
            try:
                self._trade_db.save_trade(record)
            except Exception as exc:
                logger.error("trade_persist_failed", error=str(exc))

    def record_fee(self, amount: float, fee_type: str) -> None:
        """Record a fee in the daily P&L."""
        pnl = self._get_or_create_daily_pnl()
        pnl.total_fees += amount
        pnl.net_profit -= amount
        logger.debug("fee_recorded", amount=amount, fee_type=fee_type)

    def daily_pnl(self) -> DailyPnL:
        """Return the P&L record for today."""
        return self._get_or_create_daily_pnl()

    def _get_or_create_daily_pnl(self) -> DailyPnL:
        """Get or lazily create today's DailyPnL record."""
        today = date.today().isoformat()
        if today not in self._daily_pnl:
            self._daily_pnl[today] = DailyPnL(date=today)
        return self._daily_pnl[today]

    # ------------------------------------------------------------------
    # Simulation balance
    # ------------------------------------------------------------------

    def sim_debit(self, amount: float) -> bool:
        """Debit sim balance. Returns False if insufficient funds."""
        if amount < 0:
            raise ValueError("Debit amount must be non-negative")
        if self._sim_balance_value < amount:
            logger.warning(
                "sim_debit_insufficient",
                requested=amount,
                available=self._sim_balance_value,
            )
            return False
        self._sim_balance_value -= amount
        logger.debug("sim_debited", amount=amount, balance=self._sim_balance_value)
        return True

    def sim_credit(self, amount: float) -> None:
        """Credit sim balance."""
        if amount < 0:
            raise ValueError("Credit amount must be non-negative")
        self._sim_balance_value += amount
        logger.debug("sim_credited", amount=amount, balance=self._sim_balance_value)

    @property
    def sim_balance(self) -> float:
        """Current simulation balance."""
        return self._sim_balance_value

    # ------------------------------------------------------------------
    # Persistence (JSON file for crash recovery)
    # ------------------------------------------------------------------

    def save_snapshot(self, path: str = "state_snapshot.json") -> None:
        """Save current state to a JSON file."""
        snapshot = self._to_dict()
        filepath = Path(path)
        filepath.write_text(json.dumps(snapshot, indent=2, default=str))
        logger.info("snapshot_saved", path=str(filepath))

    def load_snapshot(self, path: str = "state_snapshot.json") -> bool:
        """Load state from a JSON file. Returns False if file not found or invalid."""
        filepath = Path(path)
        if not filepath.exists():
            logger.warning("snapshot_not_found", path=str(filepath))
            return False
        try:
            data = json.loads(filepath.read_text())
            self._from_dict(data)
            logger.info("snapshot_loaded", path=str(filepath))
            return True
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.error("snapshot_load_failed", path=str(filepath), error=str(exc))
            return False

    def _to_dict(self) -> dict[str, Any]:
        """Serialize state to a dictionary."""
        positions = {}
        for cid, pos in self._positions.items():
            positions[cid] = {
                "market": _market_to_dict(pos.market),
                "yes_shares": pos.yes_shares,
                "no_shares": pos.no_shares,
                "yes_cost_basis": pos.yes_cost_basis,
                "no_cost_basis": pos.no_cost_basis,
                "strategy": pos.strategy.value,
                "opened_at": pos.opened_at.isoformat() if pos.opened_at else None,
            }

        daily_pnl = {}
        for d, pnl in self._daily_pnl.items():
            daily_pnl[d] = asdict(pnl)

        return {
            "positions": positions,
            "daily_pnl": daily_pnl,
            "sim_balance": self._sim_balance_value,
        }

    def _from_dict(self, data: dict[str, Any]) -> None:
        """Deserialize state from a dictionary."""
        self._sim_balance_value = data["sim_balance"]

        self._positions = {}
        for cid, pdata in data.get("positions", {}).items():
            mdata = pdata["market"]
            market = Market(
                condition_id=mdata["condition_id"],
                slug=mdata["slug"],
                question=mdata["question"],
                yes_token_id=mdata["yes_token_id"],
                no_token_id=mdata["no_token_id"],
                start_time=datetime.fromisoformat(mdata["start_time"]),
                end_time=datetime.fromisoformat(mdata["end_time"]),
                asset=mdata["asset"],
                neg_risk=mdata.get("neg_risk", True),
            )
            opened = pdata.get("opened_at")
            self._positions[cid] = Position(
                market=market,
                yes_shares=pdata["yes_shares"],
                no_shares=pdata["no_shares"],
                yes_cost_basis=pdata["yes_cost_basis"],
                no_cost_basis=pdata["no_cost_basis"],
                strategy=StrategyType(pdata["strategy"]),
                opened_at=datetime.fromisoformat(opened) if opened else None,
            )

        self._daily_pnl = {}
        for d, pnl_data in data.get("daily_pnl", {}).items():
            self._daily_pnl[d] = DailyPnL(**pnl_data)

    # ------------------------------------------------------------------
    # Position Resolution (for expired markets)
    # ------------------------------------------------------------------

    def resolve_expired_positions(
        self,
        outcome_resolver: "Callable[[Position], float | None] | None" = None,
    ) -> list[dict[str, Any]]:
        """Check for positions on expired markets and resolve them.

        For hedged (arbitrage) positions, the outcome is deterministic:
        one side wins $1, net profit = $1 - total_cost.

        For unhedged (directional) positions, uses the provided
        ``outcome_resolver`` callback to determine the payout. If no
        resolver is provided, unhedged positions on expired markets
        are closed with their cost basis returned (break-even assumption
        for paper trading).

        Args:
            outcome_resolver: Optional callback that takes a Position and
                returns the payout per winning share (1.0 if YES wins,
                0.0 if NO wins), or None to skip resolution.

        Returns:
            List of resolution reports with keys:
                condition_id, slug, strategy, was_hedged, payout,
                investment, net_profit
        """
        now = datetime.now(timezone.utc)
        resolved: list[dict[str, Any]] = []

        def _is_expired(pos: Position) -> bool:
            """Check if position's market has expired, handling tz-naive datetimes."""
            end_time = pos.market.end_time
            if end_time.tzinfo is None:
                end_time = end_time.replace(tzinfo=timezone.utc)
            return end_time <= now

        # Find expired positions
        expired_cids = [
            cid for cid, pos in self._positions.items()
            if _is_expired(pos)
        ]

        for cid in expired_cids:
            pos = self._positions[cid]
            report: dict[str, Any] = {
                "condition_id": cid,
                "slug": pos.market.slug,
                "strategy": pos.strategy.value,
                "was_hedged": pos.is_hedged,
                "yes_shares": pos.yes_shares,
                "no_shares": pos.no_shares,
                "investment": pos.total_investment,
            }

            if pos.is_hedged:
                # Hedged position: guaranteed $1 per share pair
                # The winning side gets $1, losing side gets $0
                # Net payout = min(yes_shares, no_shares) * $1
                paired_shares = min(pos.yes_shares, pos.no_shares)
                gross_payout = paired_shares * 1.0

                # Handle any unpaired shares (shouldn't happen in pure arb)
                unpaired_yes = pos.yes_shares - paired_shares
                unpaired_no = pos.no_shares - paired_shares

                # For unpaired shares, assume 50/50 for paper trading
                if unpaired_yes > 0 or unpaired_no > 0:
                    # Use resolver if available, else assume loss
                    if outcome_resolver is not None:
                        payout_rate = outcome_resolver(pos)
                        if payout_rate is not None:
                            if payout_rate > 0.5:  # YES won
                                gross_payout += unpaired_yes * 1.0
                            else:  # NO won
                                gross_payout += unpaired_no * 1.0
                    else:
                        # Conservative: assume unpaired shares lost
                        logger.warning(
                            "unpaired_shares_in_hedged_position",
                            condition_id=cid,
                            unpaired_yes=unpaired_yes,
                            unpaired_no=unpaired_no,
                        )

                net_profit = gross_payout - pos.total_investment
                report["gross_payout"] = gross_payout
                report["net_profit"] = net_profit

                # Update daily P&L
                pnl = self._get_or_create_daily_pnl()
                pnl.gross_profit += net_profit
                pnl.net_profit += net_profit
                if net_profit >= 0:
                    pnl.win_count += 1
                    pnl.total_win_amount += net_profit
                else:
                    pnl.loss_count += 1
                    pnl.total_loss_amount += abs(net_profit)

                # Credit sim balance
                if self._settings.dry_run:
                    self._sim_balance_value += gross_payout

                del self._positions[cid]
                logger.info(
                    "position_resolved_hedged",
                    condition_id=cid,
                    slug=pos.market.slug,
                    paired_shares=paired_shares,
                    gross_payout=round(gross_payout, 4),
                    net_profit=round(net_profit, 4),
                )

            else:
                # Unhedged (directional) position
                if outcome_resolver is not None:
                    payout_rate = outcome_resolver(pos)
                    if payout_rate is None:
                        # Resolver couldn't determine outcome, skip
                        logger.warning(
                            "unhedged_resolution_skipped",
                            condition_id=cid,
                            reason="resolver_returned_none",
                        )
                        continue

                    # Calculate payout based on which side won
                    if payout_rate > 0.5:  # YES won
                        gross_payout = pos.yes_shares * 1.0
                    else:  # NO won
                        gross_payout = pos.no_shares * 1.0

                    net_profit = gross_payout - pos.total_investment
                    report["outcome"] = "YES" if payout_rate > 0.5 else "NO"
                else:
                    # No resolver: for paper trading, use simple heuristic
                    # Return the investment (break-even) to avoid fake P&L
                    gross_payout = pos.total_investment
                    net_profit = 0.0
                    report["outcome"] = "unknown_breakeven"
                    logger.warning(
                        "unhedged_position_breakeven",
                        condition_id=cid,
                        slug=pos.market.slug,
                        reason="no_outcome_resolver",
                    )

                report["gross_payout"] = gross_payout
                report["net_profit"] = net_profit

                # Update daily P&L
                pnl = self._get_or_create_daily_pnl()
                pnl.gross_profit += net_profit
                pnl.net_profit += net_profit
                if net_profit > 0:
                    pnl.win_count += 1
                    pnl.total_win_amount += net_profit
                elif net_profit < 0:
                    pnl.loss_count += 1
                    pnl.total_loss_amount += abs(net_profit)

                # Credit sim balance
                if self._settings.dry_run:
                    self._sim_balance_value += gross_payout

                del self._positions[cid]
                logger.info(
                    "position_resolved_unhedged",
                    condition_id=cid,
                    slug=pos.market.slug,
                    yes_shares=pos.yes_shares,
                    no_shares=pos.no_shares,
                    gross_payout=round(gross_payout, 4),
                    net_profit=round(net_profit, 4),
                )

            resolved.append(report)

        if resolved:
            logger.info(
                "positions_resolved",
                count=len(resolved),
                total_net_profit=round(sum(r["net_profit"] for r in resolved), 4),
            )

        return resolved

    # ------------------------------------------------------------------
    # Startup recovery
    # ------------------------------------------------------------------

    def startup_recovery(self, path: str = "state_snapshot.json") -> dict[str, Any]:
        """Load snapshot and clean up orphaned positions (expired markets).

        Returns a report dict with keys:
            loaded: bool — whether a snapshot was loaded
            orphaned_removed: list[str] — condition_ids of removed positions
            positions_restored: int — count of valid positions kept
        """
        report: dict[str, Any] = {
            "loaded": False,
            "orphaned_removed": [],
            "positions_restored": 0,
        }

        loaded = self.load_snapshot(path)
        report["loaded"] = loaded
        if not loaded:
            return report

        # Detect orphaned positions whose markets have already expired
        now = datetime.now(timezone.utc)
        orphaned: list[str] = []
        for cid, pos in list(self._positions.items()):
            # Handle both naive and aware datetimes
            end_time = pos.market.end_time
            if end_time.tzinfo is None:
                end_time = end_time.replace(tzinfo=timezone.utc)
            if end_time < now:
                orphaned.append(cid)

        for cid in orphaned:
            del self._positions[cid]
            logger.warning("orphaned_position_removed", condition_id=cid)

        report["orphaned_removed"] = orphaned
        report["positions_restored"] = len(self._positions)

        logger.info(
            "startup_recovery_complete",
            loaded=True,
            orphaned=len(orphaned),
            restored=report["positions_restored"],
        )
        return report
