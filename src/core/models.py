"""Domain model dataclasses for the Polymarket trading bot."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class StrategyType(str, Enum):
    ARBITRAGE = "arbitrage"
    ASYMMETRIC = "asymmetric"
    PRICE_LAG = "price_lag"


class OrderStatus(str, Enum):
    PENDING = "pending"
    SIGNED = "signed"
    SUBMITTED = "submitted"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


# ---------------------------------------------------------------------------
# Market
# ---------------------------------------------------------------------------

@dataclass
class Market:
    condition_id: str
    slug: str
    question: str
    yes_token_id: str
    no_token_id: str
    start_time: datetime
    end_time: datetime
    asset: str
    neg_risk: bool = True


# ---------------------------------------------------------------------------
# Order Book
# ---------------------------------------------------------------------------

@dataclass
class OrderBookLevel:
    price: float
    size: float


@dataclass
class OrderBook:
    token_id: str
    bids: list[OrderBookLevel] = field(default_factory=list)  # sorted desc by price
    asks: list[OrderBookLevel] = field(default_factory=list)  # sorted asc by price
    timestamp_ms: int = 0
    hash: str = ""

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    @property
    def spread(self) -> Optional[float]:
        if self.best_bid is not None and self.best_ask is not None:
            return self.best_ask - self.best_bid
        return None


# ---------------------------------------------------------------------------
# Fill Estimate
# ---------------------------------------------------------------------------

@dataclass
class FillEstimate:
    filled_size: float
    total_cost: float
    vwap: float
    worst_price: float
    best_price: float
    levels_consumed: int
    sufficient_liquidity: bool


# ---------------------------------------------------------------------------
# Opportunity
# ---------------------------------------------------------------------------

@dataclass
class Opportunity:
    strategy: StrategyType
    market: Market
    timestamp: datetime
    yes_fill: Optional[FillEstimate] = None
    no_fill: Optional[FillEstimate] = None
    expected_profit: float = 0.0
    expected_profit_pct: float = 0.0
    total_fees: float = 0.0
    confidence: float = 0.0
    metadata: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Trade Order
# ---------------------------------------------------------------------------

@dataclass
class TradeOrder:
    token_id: str
    side: Side
    price: float
    size: float
    order_type: str = "FOK"
    order_id: Optional[str] = None
    status: OrderStatus = OrderStatus.PENDING
    signed_order: Optional[dict] = None
    fill_size: float = 0.0
    fill_price: float = 0.0


# ---------------------------------------------------------------------------
# Position
# ---------------------------------------------------------------------------

@dataclass
class Position:
    market: Market
    yes_shares: float = 0.0
    no_shares: float = 0.0
    yes_cost_basis: float = 0.0
    no_cost_basis: float = 0.0
    strategy: StrategyType = StrategyType.ARBITRAGE
    opened_at: Optional[datetime] = None

    @property
    def total_investment(self) -> float:
        return self.yes_cost_basis + self.no_cost_basis

    @property
    def is_hedged(self) -> bool:
        return self.yes_shares > 0 and self.no_shares > 0

    @property
    def net_directional_exposure(self) -> float:
        return self.yes_shares - self.no_shares


# ---------------------------------------------------------------------------
# Daily PnL
# ---------------------------------------------------------------------------

@dataclass
class DailyPnL:
    date: str
    trades: int = 0
    gross_profit: float = 0.0
    total_fees: float = 0.0
    net_profit: float = 0.0
    opportunities_seen: int = 0
    opportunities_taken: int = 0
    max_drawdown: float = 0.0
    win_count: int = 0
    loss_count: int = 0
