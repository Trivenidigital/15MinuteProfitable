"""Rolling price buffer for spot price tracking and movement detection."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Optional

from src.monitoring.logger import get_logger


@dataclass
class SpotPriceUpdate:
    """A single spot price tick."""

    symbol: str  # e.g. "BTCUSDT"
    price: float  # current price
    timestamp: float  # time.time() epoch seconds


@dataclass
class SpotMovement:
    """Detected spot price movement."""

    symbol: str
    direction: str  # "UP" or "DOWN"
    change_pct: float  # absolute percentage change (always positive)
    start_price: float  # price at beginning of window
    end_price: float  # price at end of window
    window_seconds: float  # time window measured
    timestamp: float  # when movement was detected


class SpotBuffer:
    """Rolling price buffer with configurable window and movement detection.

    Stores recent price updates and provides:
    - Current price for a symbol
    - Price history over a time window
    - Spot movement detection (threshold-based)

    Parameters
    ----------
    window_seconds : int
        Maximum age of prices to keep in buffer (default 60).
    max_size : int
        Maximum buffer entries per symbol (default 1000).
    """

    def __init__(
        self,
        window_seconds: int = 900,
        max_size: int = 10000,
    ) -> None:
        self._window_seconds = window_seconds
        self._max_size = max_size
        self._buffers: dict[str, deque[tuple[float, float]]] = {}  # symbol -> deque of (ts, price)
        self._log = get_logger("spot_buffer")

    def add(self, update: SpotPriceUpdate) -> None:
        """Add a price update to the buffer.

        Also prunes entries older than window_seconds.
        """
        if update.symbol not in self._buffers:
            self._buffers[update.symbol] = deque(maxlen=self._max_size)

        self._buffers[update.symbol].append((update.timestamp, update.price))
        self._prune(update.symbol)

    def get_price(self, symbol: str) -> Optional[float]:
        """Get the most recent price for a symbol, or None if not available."""
        buf = self._buffers.get(symbol)
        if not buf:
            return None
        return buf[-1][1]

    def get_price_history(
        self, symbol: str, window_seconds: int | None = None
    ) -> list[tuple[float, float]]:
        """Get price history as list of (timestamp, price) tuples.

        If window_seconds is None, returns all data in buffer.
        Otherwise returns data within the specified window from now.
        """
        buf = self._buffers.get(symbol)
        if not buf:
            return []

        if window_seconds is None:
            return list(buf)

        cutoff = time.time() - window_seconds
        return [(ts, price) for ts, price in buf if ts >= cutoff]

    def detect_movement(
        self,
        symbol: str,
        window_seconds: int,
        threshold: float,
    ) -> SpotMovement | None:
        """Detect if spot price has moved beyond threshold in the given window.

        Compares the oldest price in the window to the latest price.
        Returns a SpotMovement if |change| >= threshold, None otherwise.

        Parameters
        ----------
        symbol : str
            The trading pair (e.g. "BTCUSDT").
        window_seconds : int
            Time window to check for movement.
        threshold : float
            Minimum percentage change (as decimal, e.g. 0.0015 for 0.15%).
        """
        history = self.get_price_history(symbol, window_seconds)
        if len(history) < 2:
            return None

        start_price = history[0][1]
        end_price = history[-1][1]

        if start_price <= 0:
            return None

        change_pct = (end_price - start_price) / start_price

        if abs(change_pct) < threshold:
            return None

        direction = "UP" if change_pct > 0 else "DOWN"

        return SpotMovement(
            symbol=symbol,
            direction=direction,
            change_pct=abs(change_pct),
            start_price=start_price,
            end_price=end_price,
            window_seconds=window_seconds,
            timestamp=time.time(),
        )

    def has_data(self, symbol: str) -> bool:
        """Check if any data exists for symbol."""
        buf = self._buffers.get(symbol)
        return buf is not None and len(buf) > 0

    @property
    def symbols(self) -> list[str]:
        """Return list of symbols with data."""
        return [s for s, buf in self._buffers.items() if buf]

    def _prune(self, symbol: str) -> None:
        """Remove entries older than window_seconds."""
        buf = self._buffers.get(symbol)
        if not buf:
            return
        cutoff = time.time() - self._window_seconds
        while buf and buf[0][0] < cutoff:
            buf.popleft()
