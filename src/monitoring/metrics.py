"""Performance metrics and daily summary formatting."""

from __future__ import annotations

from src.core.models import DailyPnL
from src.monitoring.logger import get_logger

logger = get_logger(__name__)


class MetricsCollector:
    """Compute and format trading performance metrics."""

    def compute_dashboard(
        self,
        pnl: DailyPnL,
        sim_balance: float,
    ) -> dict[str, float]:
        """Compute a dashboard of key metrics from today's P&L.

        Returns:
            Dictionary with: win_rate, avg_profit_per_trade, take_rate,
            net_profit, total_fees, max_drawdown, sim_balance,
            trades, win_count, loss_count, opportunities_seen,
            opportunities_taken.
        """
        total_resolved = pnl.win_count + pnl.loss_count
        win_rate = (pnl.win_count / total_resolved) if total_resolved > 0 else 0.0

        avg_profit = (pnl.net_profit / pnl.trades) if pnl.trades > 0 else 0.0

        take_rate = (
            (pnl.opportunities_taken / pnl.opportunities_seen)
            if pnl.opportunities_seen > 0
            else 0.0
        )

        return {
            "win_rate": win_rate,
            "avg_profit_per_trade": avg_profit,
            "take_rate": take_rate,
            "net_profit": pnl.net_profit,
            "gross_profit": pnl.gross_profit,
            "total_fees": pnl.total_fees,
            "max_drawdown": pnl.max_drawdown,
            "sim_balance": sim_balance,
            "trades": float(pnl.trades),
            "win_count": float(pnl.win_count),
            "loss_count": float(pnl.loss_count),
            "opportunities_seen": float(pnl.opportunities_seen),
            "opportunities_taken": float(pnl.opportunities_taken),
        }

    def format_daily_summary(self, dashboard: dict[str, float]) -> str:
        """Format the dashboard dict into a human-readable summary string.

        Suitable for Telegram/Discord alerts or console logging.
        """
        lines = [
            f"Trades: {int(dashboard['trades'])}",
            f"Win/Loss: {int(dashboard['win_count'])}/{int(dashboard['loss_count'])}",
            f"Win Rate: {dashboard['win_rate']:.1%}",
            f"Net Profit: ${dashboard['net_profit']:.2f}",
            f"Gross Profit: ${dashboard['gross_profit']:.2f}",
            f"Total Fees: ${dashboard['total_fees']:.2f}",
            f"Avg Profit/Trade: ${dashboard['avg_profit_per_trade']:.4f}",
            f"Max Drawdown: ${dashboard['max_drawdown']:.2f}",
            f"Take Rate: {dashboard['take_rate']:.1%}",
            f"Opps Seen/Taken: {int(dashboard['opportunities_seen'])}/{int(dashboard['opportunities_taken'])}",
            f"Balance: ${dashboard['sim_balance']:.2f}",
        ]
        return "\n".join(lines)

    def log_dashboard(self, dashboard: dict[str, float]) -> None:
        """Log all dashboard metrics as a structured log entry."""
        logger.info("metrics_dashboard", **dashboard)
