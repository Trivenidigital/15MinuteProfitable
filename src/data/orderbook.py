"""L2 orderbook state management for Polymarket tokens.

Receives WebSocket snapshots and deltas and maintains an in-memory
representation of each token's order book.
"""

from __future__ import annotations

from src.core.models import FillEstimate, OrderBook, OrderBookLevel, Side


# ---------------------------------------------------------------------------
# Single-token book state
# ---------------------------------------------------------------------------


class L2BookState:
    """Manages a single token's L2 orderbook as dicts {price -> size}."""

    def __init__(self, token_id: str) -> None:
        self.token_id = token_id
        self._bids: dict[float, float] = {}  # price -> size
        self._asks: dict[float, float] = {}
        self.last_timestamp_ms: int = 0
        self.last_hash: str = ""

    # -- mutations -----------------------------------------------------------

    def apply_snapshot(self, bids: list[dict], asks: list[dict]) -> None:
        """Replace the entire book.

        Each entry is ``{"price": "0.55", "size": "100"}``.
        Values may arrive as strings or floats; both are handled.
        """
        self._bids.clear()
        self._asks.clear()

        for level in bids:
            price = float(level["price"])
            size = float(level["size"])
            if size > 0:
                self._bids[price] = size

        for level in asks:
            price = float(level["price"])
            size = float(level["size"])
            if size > 0:
                self._asks[price] = size

    def apply_delta(self, changes: list[dict]) -> None:
        """Apply incremental updates to the book.

        Each *change* dict must contain:
        - ``asset_id`` (str) -- ignored here (caller filters by token)
        - ``price`` (str | float)
        - ``size`` (str | float)
        - ``side`` ("BUY" | "SELL")

        If the resulting size is ``<= 0`` the level is removed.
        """
        for change in changes:
            price = float(change["price"])
            size = float(change["size"])
            side = change["side"]

            book = self._bids if side == "BUY" else self._asks

            if size <= 0:
                book.pop(price, None)
            else:
                book[price] = size

    # -- queries -------------------------------------------------------------

    def to_orderbook(self) -> OrderBook:
        """Return an :class:`OrderBook` model with properly sorted levels.

        Bids are sorted descending by price (best bid first).
        Asks are sorted ascending by price (best ask first).
        """
        sorted_bids = [
            OrderBookLevel(price=p, size=s)
            for p, s in sorted(self._bids.items(), key=lambda x: x[0], reverse=True)
        ]
        sorted_asks = [
            OrderBookLevel(price=p, size=s)
            for p, s in sorted(self._asks.items(), key=lambda x: x[0])
        ]

        return OrderBook(
            token_id=self.token_id,
            bids=sorted_bids,
            asks=sorted_asks,
            timestamp_ms=self.last_timestamp_ms,
            hash=self.last_hash,
        )

    def compute_fill(self, side: Side, target_size: float) -> FillEstimate:
        """Walk the book to estimate filling *target_size* shares.

        For :pyattr:`Side.BUY` we consume the **asks** (cheapest first).
        For :pyattr:`Side.SELL` we consume the **bids** (highest first).

        Returns a :class:`FillEstimate`.  If the book does not contain
        enough depth, ``sufficient_liquidity`` is ``False`` and the
        estimate reflects how much *could* be filled.
        """
        if side == Side.BUY:
            levels = sorted(self._asks.items(), key=lambda x: x[0])
        else:
            levels = sorted(self._bids.items(), key=lambda x: x[0], reverse=True)

        filled_size = 0.0
        total_cost = 0.0
        levels_consumed = 0
        best_price = 0.0
        worst_price = 0.0
        remaining = target_size

        for price, size in levels:
            if remaining <= 0:
                break

            take = min(size, remaining)
            total_cost += take * price
            filled_size += take
            remaining -= take
            levels_consumed += 1

            if levels_consumed == 1:
                best_price = price
            worst_price = price

        sufficient = filled_size >= target_size

        vwap = total_cost / filled_size if filled_size > 0 else 0.0

        return FillEstimate(
            filled_size=filled_size,
            total_cost=total_cost,
            vwap=vwap,
            worst_price=worst_price,
            best_price=best_price,
            levels_consumed=levels_consumed,
            sufficient_liquidity=sufficient,
        )


# ---------------------------------------------------------------------------
# Multi-token manager
# ---------------------------------------------------------------------------


class OrderBookManager:
    """Registry of :class:`L2BookState` instances keyed by token id."""

    def __init__(self) -> None:
        self._books: dict[str, L2BookState] = {}

    def ensure_book(self, token_id: str) -> L2BookState:
        """Return the existing book for *token_id*, creating one if needed."""
        if token_id not in self._books:
            self._books[token_id] = L2BookState(token_id)
        return self._books[token_id]

    def get_book(self, token_id: str) -> OrderBook | None:
        """Return the :class:`OrderBook` model, or ``None`` if not tracked."""
        state = self._books.get(token_id)
        if state is None:
            return None
        return state.to_orderbook()

    def get_fill_estimate(
        self, token_id: str, side: Side, size: float
    ) -> FillEstimate | None:
        """Estimate filling *size* shares, or ``None`` if not tracked."""
        state = self._books.get(token_id)
        if state is None:
            return None
        return state.compute_fill(side, size)
