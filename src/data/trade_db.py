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
class TradeResult:
    """Resolution outcome for a closed position."""

    id: int | None = None
    timestamp: float = 0.0
    condition_id: str = ""
    market_slug: str = ""
    asset: str = ""
    strategy: str = ""
    was_hedged: bool = False
    yes_shares: float = 0.0
    no_shares: float = 0.0
    investment: float = 0.0
    gross_payout: float = 0.0
    net_profit: float = 0.0
    outcome: str = ""


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


@dataclass
class StrategyDecision:
    """One row per opportunity found (or per scan cycle with 0 opps)."""

    id: int | None = None
    timestamp: float = 0.0
    cycle_id: int = 0
    condition_id: str = ""
    market_slug: str = ""
    asset: str = ""
    strategy: str = ""
    decision: str = ""  # "opportunity", "risk_approved", "risk_rejected", "executed", "exec_failed"
    rejection_reason: str = ""
    confidence: float = 0.0
    expected_profit: float = 0.0
    expected_profit_pct: float = 0.0
    metadata_json: str = "{}"


@dataclass
class SpotSnapshot:
    """Periodic spot price capture."""

    id: int | None = None
    timestamp: float = 0.0
    symbol: str = ""  # e.g. "BTCUSDT"
    price: float = 0.0


@dataclass
class MarketOutcome:
    """Outcome of a 15-minute market window (traded or not)."""

    id: int | None = None
    timestamp: float = 0.0  # when recorded
    condition_id: str = ""
    asset: str = ""
    market_slug: str = ""
    window_start: float = 0.0  # market start_time epoch
    window_end: float = 0.0  # market end_time epoch
    outcome: str = ""  # "YES" (up) or "NO" (down) or "FLAT"
    spot_open: float = 0.0
    spot_close: float = 0.0
    price_change_pct: float = 0.0  # (close - open) / open * 100
    was_traded: bool = False


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

CREATE TABLE IF NOT EXISTS trade_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    condition_id TEXT NOT NULL,
    market_slug TEXT NOT NULL DEFAULT '',
    asset TEXT NOT NULL DEFAULT '',
    strategy TEXT NOT NULL DEFAULT '',
    was_hedged INTEGER NOT NULL DEFAULT 0,
    yes_shares REAL NOT NULL DEFAULT 0.0,
    no_shares REAL NOT NULL DEFAULT 0.0,
    investment REAL NOT NULL DEFAULT 0.0,
    gross_payout REAL NOT NULL DEFAULT 0.0,
    net_profit REAL NOT NULL DEFAULT 0.0,
    outcome TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_trade_results_timestamp ON trade_results(timestamp DESC);

CREATE TABLE IF NOT EXISTS portfolio_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    updated_at REAL NOT NULL DEFAULT 0.0,
    total_equity REAL NOT NULL DEFAULT 0.0,
    total_exposure REAL NOT NULL DEFAULT 0.0,
    open_positions INTEGER NOT NULL DEFAULT 0,
    total_trades INTEGER NOT NULL DEFAULT 0,
    total_pnl REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS strategy_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    cycle_id INTEGER NOT NULL DEFAULT 0,
    condition_id TEXT NOT NULL DEFAULT '',
    market_slug TEXT NOT NULL DEFAULT '',
    asset TEXT NOT NULL DEFAULT '',
    strategy TEXT NOT NULL DEFAULT '',
    decision TEXT NOT NULL DEFAULT '',
    rejection_reason TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0.0,
    expected_profit REAL NOT NULL DEFAULT 0.0,
    expected_profit_pct REAL NOT NULL DEFAULT 0.0,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_decisions_timestamp ON strategy_decisions(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_decisions_strategy ON strategy_decisions(strategy);
CREATE INDEX IF NOT EXISTS idx_decisions_cycle ON strategy_decisions(cycle_id);

CREATE TABLE IF NOT EXISTS spot_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    symbol TEXT NOT NULL DEFAULT '',
    price REAL NOT NULL DEFAULT 0.0
);

CREATE INDEX IF NOT EXISTS idx_spot_timestamp ON spot_snapshots(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_spot_symbol ON spot_snapshots(symbol);

CREATE TABLE IF NOT EXISTS market_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    condition_id TEXT NOT NULL UNIQUE,
    asset TEXT NOT NULL DEFAULT '',
    market_slug TEXT NOT NULL DEFAULT '',
    window_start REAL NOT NULL DEFAULT 0.0,
    window_end REAL NOT NULL DEFAULT 0.0,
    outcome TEXT NOT NULL DEFAULT '',
    spot_open REAL NOT NULL DEFAULT 0.0,
    spot_close REAL NOT NULL DEFAULT 0.0,
    price_change_pct REAL NOT NULL DEFAULT 0.0,
    was_traded INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_outcomes_asset ON market_outcomes(asset);
CREATE INDEX IF NOT EXISTS idx_outcomes_window ON market_outcomes(window_end DESC);

CREATE TABLE IF NOT EXISTS kelly_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    updated_at REAL NOT NULL DEFAULT 0.0,
    win_rate REAL NOT NULL DEFAULT 0.0,
    avg_win REAL NOT NULL DEFAULT 0.0,
    avg_loss REAL NOT NULL DEFAULT 0.0,
    sample_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS circuit_breaker_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    active INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '',
    until_ts REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS strategy_cooldown_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    state_json TEXT NOT NULL DEFAULT '{}'
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
    # Trade results (resolution outcomes)
    # ------------------------------------------------------------------

    def save_trade_result(self, result: TradeResult) -> int:
        """Insert a trade result record. Returns the row ID."""
        cursor = self._conn.execute(
            """INSERT INTO trade_results
               (timestamp, condition_id, market_slug, asset, strategy,
                was_hedged, yes_shares, no_shares, investment,
                gross_payout, net_profit, outcome)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                result.timestamp,
                result.condition_id,
                result.market_slug,
                result.asset,
                result.strategy,
                1 if result.was_hedged else 0,
                result.yes_shares,
                result.no_shares,
                result.investment,
                result.gross_payout,
                result.net_profit,
                result.outcome,
            ),
        )
        self._conn.commit()
        row_id = cursor.lastrowid
        assert row_id is not None
        logger.debug(
            "trade_result_saved", id=row_id, condition_id=result.condition_id
        )
        return row_id

    def get_trade_results(
        self,
        limit: int = 50,
        offset: int = 0,
    ) -> list[TradeResult]:
        """Fetch trade results in reverse chronological order."""
        rows = self._conn.execute(
            "SELECT * FROM trade_results ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [self._row_to_trade_result(r) for r in rows]

    def get_trade_results_since(
        self,
        since_ts: float,
        strategy: str | None = None,
    ) -> list[TradeResult]:
        """Fetch trade results since a given timestamp, optionally by strategy."""
        if strategy:
            rows = self._conn.execute(
                "SELECT * FROM trade_results WHERE timestamp >= ? AND strategy = ? "
                "ORDER BY timestamp DESC",
                (since_ts, strategy),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM trade_results WHERE timestamp >= ? "
                "ORDER BY timestamp DESC",
                (since_ts,),
            ).fetchall()
        return [self._row_to_trade_result(r) for r in rows]

    def get_trade_result_count(self) -> int:
        """Count total trade results."""
        row = self._conn.execute(
            "SELECT COUNT(*) FROM trade_results"
        ).fetchone()
        return row[0] if row else 0

    def get_lifetime_net_profit_from_results(self) -> float:
        """Sum net_profit from trade_results table (ground truth).

        This queries actual resolution outcomes, not in-memory-derived
        daily_snapshots, making it the authoritative source for lifetime P&L.
        """
        row = self._conn.execute(
            "SELECT COALESCE(SUM(net_profit), 0.0) FROM trade_results"
        ).fetchone()
        return float(row[0]) if row else 0.0

    def get_position_strategy_breakdown(
        self, condition_id: str
    ) -> dict[str, dict[str, float]]:
        """Get per-strategy share/cost breakdown for a condition_id.

        Queries the ``trades`` table and groups by strategy, computing net
        YES/NO shares and costs (BUY adds, SELL subtracts).

        Returns a dict keyed by strategy name::

            {
                "fade_panic": {
                    "yes_shares": 0.0, "no_shares": 700.0,
                    "yes_cost": 0.0,   "no_cost": 103.0,
                },
                "resolution_sniper": {
                    "yes_shares": 20.0, "no_shares": 0.0,
                    "yes_cost": 3.40,   "no_cost": 0.0,
                },
            }

        Returns an empty dict if no trades found for the condition.
        """
        rows = self._conn.execute(
            "SELECT strategy, side, token_side, size, cost "
            "FROM trades WHERE condition_id = ?",
            (condition_id,),
        ).fetchall()

        if not rows:
            return {}

        breakdown: dict[str, dict[str, float]] = {}
        for row in rows:
            strat = row["strategy"]
            if strat not in breakdown:
                breakdown[strat] = {
                    "yes_shares": 0.0,
                    "no_shares": 0.0,
                    "yes_cost": 0.0,
                    "no_cost": 0.0,
                }
            entry = breakdown[strat]
            side = row["side"]       # BUY or SELL
            token = row["token_side"]  # YES or NO
            size = float(row["size"])
            cost = float(row["cost"])

            if side == "BUY":
                if token == "YES":
                    entry["yes_shares"] += size
                    entry["yes_cost"] += cost
                else:
                    entry["no_shares"] += size
                    entry["no_cost"] += cost
            else:
                # SELL reduces position
                if token == "YES":
                    entry["yes_shares"] -= size
                    entry["yes_cost"] -= cost
                else:
                    entry["no_shares"] -= size
                    entry["no_cost"] -= cost

        return breakdown

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

    def get_historical_net_profit(self, exclude_date: str | None = None) -> float:
        """Sum net_profit from all daily_snapshots, optionally excluding a date.

        Used to compute lifetime P&L: historical (from DB) + today (in-memory).
        """
        if exclude_date:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(net_profit), 0.0) FROM daily_snapshots WHERE date != ?",
                (exclude_date,),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(net_profit), 0.0) FROM daily_snapshots",
            ).fetchone()
        return float(row[0]) if row else 0.0

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
    # Strategy decisions
    # ------------------------------------------------------------------

    def save_decision(self, d: StrategyDecision) -> int:
        """Insert a strategy decision record. Returns the row ID."""
        cursor = self._conn.execute(
            """INSERT INTO strategy_decisions
               (timestamp, cycle_id, condition_id, market_slug, asset, strategy,
                decision, rejection_reason, confidence, expected_profit,
                expected_profit_pct, metadata_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                d.timestamp,
                d.cycle_id,
                d.condition_id,
                d.market_slug,
                d.asset,
                d.strategy,
                d.decision,
                d.rejection_reason,
                d.confidence,
                d.expected_profit,
                d.expected_profit_pct,
                d.metadata_json,
            ),
        )
        self._conn.commit()
        row_id = cursor.lastrowid
        assert row_id is not None
        return row_id

    def get_decisions(
        self,
        limit: int = 50,
        offset: int = 0,
        strategy: str | None = None,
    ) -> list[StrategyDecision]:
        """Fetch decisions in reverse chronological order with optional strategy filter."""
        if strategy:
            rows = self._conn.execute(
                "SELECT * FROM strategy_decisions WHERE strategy = ?"
                " ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                (strategy, limit, offset),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM strategy_decisions ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [self._row_to_decision(r) for r in rows]

    # ------------------------------------------------------------------
    # Spot snapshots
    # ------------------------------------------------------------------

    def save_spot_snapshot(self, s: SpotSnapshot) -> int:
        """Insert a spot snapshot record. Returns the row ID."""
        cursor = self._conn.execute(
            """INSERT INTO spot_snapshots (timestamp, symbol, price)
               VALUES (?, ?, ?)""",
            (s.timestamp, s.symbol, s.price),
        )
        self._conn.commit()
        row_id = cursor.lastrowid
        assert row_id is not None
        return row_id

    def get_spot_snapshots(
        self,
        symbol: str,
        limit: int = 100,
    ) -> list[SpotSnapshot]:
        """Fetch spot snapshots for a symbol in reverse chronological order."""
        rows = self._conn.execute(
            "SELECT * FROM spot_snapshots WHERE symbol = ? ORDER BY timestamp DESC LIMIT ?",
            (symbol, limit),
        ).fetchall()
        return [self._row_to_spot_snapshot(r) for r in rows]

    def get_spot_at_time(
        self,
        symbol: str,
        target_ts: float,
        tolerance_s: float = 30.0,
    ) -> float | None:
        """Find the closest spot snapshot within tolerance of target_ts.

        Returns the price if found, None if no snapshot within tolerance.
        """
        row = self._conn.execute(
            """SELECT price, ABS(timestamp - ?) AS diff
               FROM spot_snapshots
               WHERE symbol = ? AND ABS(timestamp - ?) <= ?
               ORDER BY diff ASC LIMIT 1""",
            (target_ts, symbol, target_ts, tolerance_s),
        ).fetchone()
        if row is None:
            return None
        return float(row["price"])

    # ------------------------------------------------------------------
    # Market outcomes
    # ------------------------------------------------------------------

    def save_market_outcome(self, o: MarketOutcome) -> int:
        """Insert a market outcome record. Returns the row ID.

        Uses INSERT OR REPLACE to handle duplicate condition_ids
        (e.g. if the same market is recorded twice).
        """
        cursor = self._conn.execute(
            """INSERT OR REPLACE INTO market_outcomes
               (timestamp, condition_id, asset, market_slug, window_start,
                window_end, outcome, spot_open, spot_close,
                price_change_pct, was_traded)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                o.timestamp,
                o.condition_id,
                o.asset,
                o.market_slug,
                o.window_start,
                o.window_end,
                o.outcome,
                o.spot_open,
                o.spot_close,
                o.price_change_pct,
                1 if o.was_traded else 0,
            ),
        )
        self._conn.commit()
        row_id = cursor.lastrowid
        assert row_id is not None
        return row_id

    def get_market_outcomes(
        self,
        limit: int = 50,
        asset: str | None = None,
    ) -> list[MarketOutcome]:
        """Fetch market outcomes in reverse chronological order."""
        if asset:
            rows = self._conn.execute(
                "SELECT * FROM market_outcomes WHERE asset = ? ORDER BY window_end DESC LIMIT ?",
                (asset, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM market_outcomes ORDER BY window_end DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_market_outcome(r) for r in rows]

    def get_outcome_stats(self, asset: str | None = None) -> dict[str, float | int]:
        """Compute aggregate stats from market outcomes.

        Returns dict with keys: count, yes_count, no_count, flat_count,
        win_rate, avg_change_pct.
        """
        if asset:
            rows = self._conn.execute(
                "SELECT outcome, price_change_pct FROM market_outcomes WHERE asset = ?",
                (asset,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT outcome, price_change_pct FROM market_outcomes",
            ).fetchall()

        count = len(rows)
        if count == 0:
            return {
                "count": 0,
                "yes_count": 0,
                "no_count": 0,
                "flat_count": 0,
                "win_rate": 0.0,
                "avg_change_pct": 0.0,
            }

        yes_count = sum(1 for r in rows if r["outcome"] == "YES")
        no_count = sum(1 for r in rows if r["outcome"] == "NO")
        flat_count = sum(1 for r in rows if r["outcome"] == "FLAT")
        avg_change = sum(float(r["price_change_pct"]) for r in rows) / count

        return {
            "count": count,
            "yes_count": yes_count,
            "no_count": no_count,
            "flat_count": flat_count,
            "win_rate": yes_count / count if count > 0 else 0.0,
            "avg_change_pct": avg_change,
        }

    # ------------------------------------------------------------------
    # Kelly state persistence
    # ------------------------------------------------------------------

    def save_kelly_state(
        self,
        win_rate: float,
        avg_win: float,
        avg_loss: float,
        sample_count: int,
    ) -> None:
        """Upsert the Kelly sizing state (single row)."""
        self._conn.execute(
            """INSERT INTO kelly_state (id, updated_at, win_rate, avg_win, avg_loss, sample_count)
               VALUES (1, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 updated_at=excluded.updated_at,
                 win_rate=excluded.win_rate,
                 avg_win=excluded.avg_win,
                 avg_loss=excluded.avg_loss,
                 sample_count=excluded.sample_count""",
            (time.time(), win_rate, avg_win, avg_loss, sample_count),
        )
        self._conn.commit()

    def load_kelly_state(self) -> dict[str, float] | None:
        """Load the persisted Kelly state, or None if not yet saved."""
        row = self._conn.execute(
            "SELECT * FROM kelly_state WHERE id = 1"
        ).fetchone()
        if row is None:
            return None
        return {
            "win_rate": float(row["win_rate"]),
            "avg_win": float(row["avg_win"]),
            "avg_loss": float(row["avg_loss"]),
            "sample_count": int(row["sample_count"]),
        }

    # ------------------------------------------------------------------
    # Circuit breaker state persistence
    # ------------------------------------------------------------------

    def save_circuit_breaker_state(
        self,
        active: bool,
        reason: str,
        until_ts: float,
    ) -> None:
        """Upsert the circuit breaker state (single row)."""
        self._conn.execute(
            """INSERT INTO circuit_breaker_state (id, active, reason, until_ts)
               VALUES (1, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 active=excluded.active,
                 reason=excluded.reason,
                 until_ts=excluded.until_ts""",
            (1 if active else 0, reason, until_ts),
        )
        self._conn.commit()

    def load_circuit_breaker_state(self) -> dict[str, float | str | bool] | None:
        """Load the persisted circuit breaker state, or None if not yet saved."""
        row = self._conn.execute(
            "SELECT * FROM circuit_breaker_state WHERE id = 1"
        ).fetchone()
        if row is None:
            return None
        return {
            "active": bool(row["active"]),
            "reason": str(row["reason"]),
            "until_ts": float(row["until_ts"]),
        }

    # ------------------------------------------------------------------
    # Strategy cooldown state persistence
    # ------------------------------------------------------------------

    def save_strategy_cooldown_state(self, state_json: str) -> None:
        """Upsert the strategy cooldown state (single row, JSON blob)."""
        self._conn.execute(
            """INSERT INTO strategy_cooldown_state (id, state_json)
               VALUES (1, ?)
               ON CONFLICT(id) DO UPDATE SET
                 state_json=excluded.state_json""",
            (state_json,),
        )
        self._conn.commit()

    def load_strategy_cooldown_state(self) -> dict | None:
        """Load the persisted strategy cooldown state, or None if not yet saved."""
        import json

        row = self._conn.execute(
            "SELECT state_json FROM strategy_cooldown_state WHERE id = 1"
        ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["state_json"])
        except (json.JSONDecodeError, TypeError):
            return None

    # ------------------------------------------------------------------
    # Spot series for correlation computation
    # ------------------------------------------------------------------

    def get_spot_series_for_correlation(
        self,
        symbols: list[str],
        window_seconds: int = 3600,
    ) -> dict[str, list[tuple[float, float]]]:
        """Fetch aligned spot histories for cross-asset correlation computation.

        Returns a dict mapping each symbol to a list of (timestamp, price)
        tuples within the last ``window_seconds``.

        Args:
            symbols: List of Binance symbols (e.g. ["BTCUSDT", "ETHUSDT"]).
            window_seconds: How far back to look (default 1 hour).

        Returns:
            Dict of symbol -> [(timestamp, price), ...] sorted by timestamp.
        """
        cutoff = time.time() - window_seconds
        result: dict[str, list[tuple[float, float]]] = {}

        for symbol in symbols:
            rows = self._conn.execute(
                "SELECT timestamp, price FROM spot_snapshots "
                "WHERE symbol = ? AND timestamp >= ? "
                "ORDER BY timestamp ASC",
                (symbol, cutoff),
            ).fetchall()
            result[symbol] = [(float(r["timestamp"]), float(r["price"])) for r in rows]

        return result

    # ------------------------------------------------------------------
    # Performance summary
    # ------------------------------------------------------------------

    def get_performance_summary(self) -> list[TradeResult]:
        """Fetch all trade results for performance summary computation."""
        rows = self._conn.execute(
            "SELECT * FROM trade_results ORDER BY timestamp DESC"
        ).fetchall()
        return [self._row_to_trade_result(r) for r in rows]

    def get_aggregate_metrics(self) -> dict[str, float]:
        """Compute aggregate win/loss metrics from trade_results (source of truth)."""
        row = self._conn.execute(
            """SELECT
                   COUNT(*)                                          AS trades,
                   SUM(CASE WHEN net_profit > 0 THEN 1 ELSE 0 END)  AS win_count,
                   SUM(CASE WHEN net_profit <= 0 THEN 1 ELSE 0 END) AS loss_count,
                   SUM(CASE WHEN net_profit > 0 THEN net_profit ELSE 0 END)  AS total_win_amount,
                   SUM(CASE WHEN net_profit <= 0 THEN net_profit ELSE 0 END) AS total_loss_amount,
                   SUM(net_profit)                                   AS net_profit,
                   SUM(gross_payout - investment)                    AS gross_profit,
                   SUM((gross_payout - investment) - net_profit)     AS total_fees
               FROM trade_results"""
        ).fetchone()

        trades = int(row["trades"] or 0)
        win_count = int(row["win_count"] or 0)
        loss_count = int(row["loss_count"] or 0)
        total_win_amount = float(row["total_win_amount"] or 0.0)
        total_loss_amount = float(row["total_loss_amount"] or 0.0)
        net_profit = float(row["net_profit"] or 0.0)
        gross_profit = float(row["gross_profit"] or 0.0)
        total_fees = float(row["total_fees"] or 0.0)

        win_rate = (win_count / trades) if trades > 0 else 0.0
        avg_win = (total_win_amount / win_count) if win_count > 0 else 0.0
        avg_loss = (total_loss_amount / loss_count) if loss_count > 0 else 0.0

        return {
            "trades": float(trades),
            "win_count": float(win_count),
            "loss_count": float(loss_count),
            "win_rate": win_rate,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "net_profit": net_profit,
            "gross_profit": gross_profit,
            "total_fees": total_fees,
        }

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
    def _row_to_trade_result(row: sqlite3.Row) -> TradeResult:
        return TradeResult(
            id=row["id"],
            timestamp=row["timestamp"],
            condition_id=row["condition_id"],
            market_slug=row["market_slug"],
            asset=row["asset"],
            strategy=row["strategy"],
            was_hedged=bool(row["was_hedged"]),
            yes_shares=row["yes_shares"],
            no_shares=row["no_shares"],
            investment=row["investment"],
            gross_payout=row["gross_payout"],
            net_profit=row["net_profit"],
            outcome=row["outcome"],
        )

    @staticmethod
    def _row_to_decision(row: sqlite3.Row) -> StrategyDecision:
        return StrategyDecision(
            id=row["id"],
            timestamp=row["timestamp"],
            cycle_id=row["cycle_id"],
            condition_id=row["condition_id"],
            market_slug=row["market_slug"],
            asset=row["asset"],
            strategy=row["strategy"],
            decision=row["decision"],
            rejection_reason=row["rejection_reason"],
            confidence=row["confidence"],
            expected_profit=row["expected_profit"],
            expected_profit_pct=row["expected_profit_pct"],
            metadata_json=row["metadata_json"],
        )

    @staticmethod
    def _row_to_spot_snapshot(row: sqlite3.Row) -> SpotSnapshot:
        return SpotSnapshot(
            id=row["id"],
            timestamp=row["timestamp"],
            symbol=row["symbol"],
            price=row["price"],
        )

    @staticmethod
    def _row_to_market_outcome(row: sqlite3.Row) -> MarketOutcome:
        return MarketOutcome(
            id=row["id"],
            timestamp=row["timestamp"],
            condition_id=row["condition_id"],
            asset=row["asset"],
            market_slug=row["market_slug"],
            window_start=row["window_start"],
            window_end=row["window_end"],
            outcome=row["outcome"],
            spot_open=row["spot_open"],
            spot_close=row["spot_close"],
            price_change_pct=row["price_change_pct"],
            was_traded=bool(row["was_traded"]),
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
