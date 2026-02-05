"""SQLite persistence layer for trade recording and analytics.

Stores individual fills, daily P&L snapshots, and portfolio state summaries
so the dashboard can render historical data across sessions.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from src.monitoring.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class TradeRecord:
    """Flat, serializable row for a single fill."""

    id: Optional[int] = None
    timestamp: float = 0.0
    condition_id: str = ""
    market_slug: str = ""
    asset: str = ""
    strategy: str = ""
    side: str = ""  # BUY / SELL
    token_side: str = ""  # YES / NO
    price: float = 0.0
    size: float = 0.0
    cost: float = 0.0
    order_type: str = ""
    order_id: str = ""
    status: str = ""
    fees: float = 0.0
    expected_profit: float = 0.0
    metadata_json: str = "{}"


@dataclass
class DailySnapshot:
    """Daily P&L snapshot row."""

    id: Optional[int] = None
    date: str = ""
    trades: int = 0
    gross_profit: float = 0.0
    net_profit: float = 0.0
    total_fees: float = 0.0
    win_count: int = 0
    loss_count: int = 0
    max_drawdown: float = 0.0
    sim_balance: float = 0.0
    opportunities_seen: int = 0
    opportunities_taken: int = 0


@dataclass
class PortfolioState:
    """Single-row portfolio summary."""

    id: int = 1
    updated_at: float = 0.0
    total_equity: float = 0.0
    total_exposure: float = 0.0
    open_positions: int = 0
    total_trades: int = 0
    total_pnl: float = 0.0


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    condition_id TEXT NOT NULL,
    market_slug TEXT NOT NULL DEFAULT '',
    asset TEXT NOT NULL DEFAULT '',
    strategy TEXT NOT NULL DEFAULT '',
    side TEXT NOT NULL DEFAULT '',
    token_side TEXT NOT NULL DEFAULT '',
    price REAL NOT NULL DEFAULT 0.0,
    size REAL NOT NULL DEFAULT 0.0,
    cost REAL NOT NULL DEFAULT 0.0,
    order_type TEXT NOT NULL DEFAULT '',
    order_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    fees REAL NOT NULL DEFAULT 0.0,
    expected_profit REAL NOT NULL DEFAULT 0.0,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy);
CREATE INDEX IF NOT EXISTS idx_trades_condition ON trades(condition_id);

CREATE TABLE IF NOT EXISTS daily_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL UNIQUE,
    trades INTEGER NOT NULL DEFAULT 0,
    gross_profit REAL NOT NULL DEFAULT 0.0,
    net_profit REAL NOT NULL DEFAULT 0.0,
    total_fees REAL NOT NULL DEFAULT 0.0,
    win_count INTEGER NOT NULL DEFAULT 0,
    loss_count INTEGER NOT NULL DEFAULT 0,
    max_drawdown REAL NOT NULL DEFAULT 0.0,
    sim_balance REAL NOT NULL DEFAULT 0.0,
    opportunities_seen INTEGER NOT NULL DEFAULT 0,
    opportunities_taken INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS portfolio_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    updated_at REAL NOT NULL DEFAULT 0.0,
    total_equity REAL NOT NULL DEFAULT 0.0,
    total_exposure REAL NOT NULL DEFAULT 0.0,
    open_positions INTEGER NOT NULL DEFAULT 0,
    total_trades INTEGER NOT NULL DEFAULT 0,
    total_pnl REAL NOT NULL DEFAULT 0.0
);
"""


# ---------------------------------------------------------------------------
# Database class
# ---------------------------------------------------------------------------


class TradeDatabase:
    """SQLite persistence for trade history and analytics.

    Parameters
    ----------
    db_path : str
        Path to the SQLite database file. Use ``:memory:`` for tests.
    """

    def __init__(self, db_path: str = "data/trades.db") -> None:
        self._db_path = db_path

        # Ensure parent directory exists (skip for :memory:)
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()
        logger.info("trade_db_initialized", db_path=db_path)

    def _init_schema(self) -> None:
        """Create tables and indexes if they don't exist."""
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()

    # ------------------------------------------------------------------
    # Trades
    # ------------------------------------------------------------------

    def save_trade(self, trade: TradeRecord) -> int:
        """Insert a trade record. Returns the row ID."""
        cursor = self._conn.execute(
            """INSERT INTO trades
               (timestamp, condition_id, market_slug, asset, strategy, side,
                token_side, price, size, cost, order_type, order_id, status,
                fees, expected_profit, metadata_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                trade.timestamp,
                trade.condition_id,
                trade.market_slug,
                trade.asset,
                trade.strategy,
                trade.side,
                trade.token_side,
                trade.price,
                trade.size,
                trade.cost,
                trade.order_type,
                trade.order_id,
                trade.status,
                trade.fees,
                trade.expected_profit,
                trade.metadata_json,
            ),
        )
        self._conn.commit()
        row_id = cursor.lastrowid
        assert row_id is not None
        logger.debug("trade_saved", id=row_id, condition_id=trade.condition_id)
        return row_id

    def get_trades(
        self,
        limit: int = 50,
        offset: int = 0,
        strategy: str | None = None,
    ) -> list[TradeRecord]:
        """Fetch trades in reverse chronological order with optional strategy filter."""
        if strategy:
            rows = self._conn.execute(
                "SELECT * FROM trades WHERE strategy = ? ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                (strategy, limit, offset),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM trades ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [self._row_to_trade(r) for r in rows]

    def get_trade_count(self, strategy: str | None = None) -> int:
        """Count total trades, optionally filtered by strategy."""
        if strategy:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM trades WHERE strategy = ?", (strategy,)
            ).fetchone()
        else:
            row = self._conn.execute("SELECT COUNT(*) FROM trades").fetchone()
        return row[0] if row else 0

    # ------------------------------------------------------------------
    # Daily snapshots
    # ------------------------------------------------------------------

    def save_daily_snapshot(self, snapshot: DailySnapshot) -> None:
        """Upsert a daily snapshot (insert or replace by date)."""
        self._conn.execute(
            """INSERT INTO daily_snapshots
               (date, trades, gross_profit, net_profit, total_fees,
                win_count, loss_count, max_drawdown, sim_balance,
                opportunities_seen, opportunities_taken)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(date) DO UPDATE SET
                 trades=excluded.trades,
                 gross_profit=excluded.gross_profit,
                 net_profit=excluded.net_profit,
                 total_fees=excluded.total_fees,
                 win_count=excluded.win_count,
                 loss_count=excluded.loss_count,
                 max_drawdown=excluded.max_drawdown,
                 sim_balance=excluded.sim_balance,
                 opportunities_seen=excluded.opportunities_seen,
                 opportunities_taken=excluded.opportunities_taken""",
            (
                snapshot.date,
                snapshot.trades,
                snapshot.gross_profit,
                snapshot.net_profit,
                snapshot.total_fees,
                snapshot.win_count,
                snapshot.loss_count,
                snapshot.max_drawdown,
                snapshot.sim_balance,
                snapshot.opportunities_seen,
                snapshot.opportunities_taken,
            ),
        )
        self._conn.commit()
        logger.debug("daily_snapshot_saved", date=snapshot.date)

    def get_daily_snapshots(self, limit: int = 30) -> list[DailySnapshot]:
        """Fetch recent daily snapshots in reverse chronological order."""
        rows = self._conn.execute(
            "SELECT * FROM daily_snapshots ORDER BY date DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._row_to_snapshot(r) for r in rows]

    # ------------------------------------------------------------------
    # Portfolio state
    # ------------------------------------------------------------------

    def save_portfolio_state(self, state: PortfolioState) -> None:
        """Upsert the single-row portfolio state."""
        self._conn.execute(
            """INSERT INTO portfolio_state
               (id, updated_at, total_equity, total_exposure,
                open_positions, total_trades, total_pnl)
               VALUES (1, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 updated_at=excluded.updated_at,
                 total_equity=excluded.total_equity,
                 total_exposure=excluded.total_exposure,
                 open_positions=excluded.open_positions,
                 total_trades=excluded.total_trades,
                 total_pnl=excluded.total_pnl""",
            (
                state.updated_at,
                state.total_equity,
                state.total_exposure,
                state.open_positions,
                state.total_trades,
                state.total_pnl,
            ),
        )
        self._conn.commit()

    def get_portfolio_state(self) -> PortfolioState | None:
        """Fetch the single-row portfolio state, or None if not yet saved."""
        row = self._conn.execute(
            "SELECT * FROM portfolio_state WHERE id = 1"
        ).fetchone()
        if row is None:
            return None
        return PortfolioState(
            id=row["id"],
            updated_at=row["updated_at"],
            total_equity=row["total_equity"],
            total_exposure=row["total_exposure"],
            open_positions=row["open_positions"],
            total_trades=row["total_trades"],
            total_pnl=row["total_pnl"],
        )

    # ------------------------------------------------------------------
    # Analytics
    # ------------------------------------------------------------------

    def get_strategy_breakdown(self) -> dict[str, dict[str, float]]:
        """Get trade count and total cost grouped by strategy.

        Returns dict like ``{"arbitrage": {"count": 10, "total_cost": 500.0}}``.
        """
        rows = self._conn.execute(
            "SELECT strategy, COUNT(*) as cnt, SUM(cost) as total_cost "
            "FROM trades GROUP BY strategy"
        ).fetchall()
        return {
            row["strategy"]: {
                "count": float(row["cnt"]),
                "total_cost": float(row["total_cost"] or 0),
            }
            for row in rows
        }

    def get_equity_curve(self, limit: int = 90) -> list[dict[str, float]]:
        """Get time-series equity data from daily snapshots.

        Returns list of ``{"date": "2025-01-01", "equity": 1050.0, "pnl": 50.0}``.
        """
        rows = self._conn.execute(
            "SELECT date, sim_balance, net_profit FROM daily_snapshots "
            "ORDER BY date ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "date": row["date"],
                "equity": float(row["sim_balance"]),
                "pnl": float(row["net_profit"]),
            }
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_trade(row: sqlite3.Row) -> TradeRecord:
        return TradeRecord(
            id=row["id"],
            timestamp=row["timestamp"],
            condition_id=row["condition_id"],
            market_slug=row["market_slug"],
            asset=row["asset"],
            strategy=row["strategy"],
            side=row["side"],
            token_side=row["token_side"],
            price=row["price"],
            size=row["size"],
            cost=row["cost"],
            order_type=row["order_type"],
            order_id=row["order_id"],
            status=row["status"],
            fees=row["fees"],
            expected_profit=row["expected_profit"],
            metadata_json=row["metadata_json"],
        )

    @staticmethod
    def _row_to_snapshot(row: sqlite3.Row) -> DailySnapshot:
        return DailySnapshot(
            id=row["id"],
            date=row["date"],
            trades=row["trades"],
            gross_profit=row["gross_profit"],
            net_profit=row["net_profit"],
            total_fees=row["total_fees"],
            win_count=row["win_count"],
            loss_count=row["loss_count"],
            max_drawdown=row["max_drawdown"],
            sim_balance=row["sim_balance"],
            opportunities_seen=row["opportunities_seen"],
            opportunities_taken=row["opportunities_taken"],
        )
