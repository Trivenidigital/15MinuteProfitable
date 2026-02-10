"""External alpha signals: Funding Rate, Open Interest delta, Volatility Regime.

Polls Binance Futures public API (no auth) for funding rate and open interest,
and computes volatility regime from SpotBuffer data.  All signals are injected
into strategy opportunity metadata for offline analysis.  Vol regime actively
gates dip_buyer (block in HIGH vol).

Architecture follows SpotBuffer pattern: created once in _run_bot(), injected
into strategies via constructor.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum

import aiohttp

from src.data.spot_buffer import SpotBuffer
from src.monitoring.logger import get_logger

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class FundingBias(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


class OITrend(str, Enum):
    RISING = "RISING"
    FALLING = "FALLING"
    FLAT = "FLAT"


class VolRegime(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


# ---------------------------------------------------------------------------
# Signal dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FundingSignal:
    symbol: str
    rate: float
    bias: FundingBias
    timestamp: float
    next_funding_time: int  # epoch ms from Binance


@dataclass(frozen=True)
class OISignal:
    symbol: str
    current_oi: float
    delta_pct: float
    trend: OITrend
    price_diverging: bool
    timestamp: float


@dataclass(frozen=True)
class VolSignal:
    symbol: str
    realized_vol: float
    regime: VolRegime
    timestamp: float


@dataclass(frozen=True)
class AlphaSnapshot:
    symbol: str
    funding: FundingSignal | None = None
    oi: OISignal | None = None
    vol: VolSignal | None = None


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

_BINANCE_FUTURES_BASE = "https://fapi.binance.com"


class AlphaSignalProvider:
    """Background provider for external alpha signals.

    Polls Binance Futures public endpoints for funding rate and open interest,
    and computes volatility regime on-demand from SpotBuffer data.

    Parameters
    ----------
    symbols : list[str]
        Binance futures symbols to track (e.g. ["BTCUSDT", "ETHUSDT"]).
    spot_buffer : SpotBuffer
        Shared spot price buffer for vol regime computation.
    funding_poll_seconds : float
        Interval between funding rate polls (default 8h).
    oi_poll_seconds : float
        Interval between open interest polls (default 60s).
    vol_window_seconds : int
        Window for realized vol computation (default 600s = 10 min).
    vol_min_data_points : int
        Minimum data points for vol computation.
    vol_low_threshold : float
        Sigma below this → LOW regime.
    vol_high_threshold : float
        Sigma above this → HIGH regime.
    funding_bullish_threshold : float
        Rate below this → BULLISH (negative = shorts paying longs).
    funding_bearish_threshold : float
        Rate above this → BEARISH (longs paying shorts).
    oi_rising_threshold : float
        Delta % above this → RISING.
    oi_falling_threshold : float
        Delta % below this → FALLING.
    """

    def __init__(
        self,
        symbols: list[str],
        spot_buffer: SpotBuffer,
        funding_poll_seconds: float = 28800.0,
        oi_poll_seconds: float = 60.0,
        vol_window_seconds: int = 600,
        vol_min_data_points: int = 10,
        vol_low_threshold: float = 0.0005,
        vol_high_threshold: float = 0.0020,
        funding_bullish_threshold: float = -0.0001,
        funding_bearish_threshold: float = 0.0001,
        oi_rising_threshold: float = 0.02,
        oi_falling_threshold: float = -0.02,
    ) -> None:
        self._symbols = symbols
        self._spot_buffer = spot_buffer
        self._funding_poll_seconds = funding_poll_seconds
        self._oi_poll_seconds = oi_poll_seconds
        self._vol_window_seconds = vol_window_seconds
        self._vol_min_data_points = vol_min_data_points
        self._vol_low_threshold = vol_low_threshold
        self._vol_high_threshold = vol_high_threshold
        self._funding_bullish_threshold = funding_bullish_threshold
        self._funding_bearish_threshold = funding_bearish_threshold
        self._oi_rising_threshold = oi_rising_threshold
        self._oi_falling_threshold = oi_falling_threshold

        self._log = get_logger("alpha_signals")
        self._session: aiohttp.ClientSession | None = None

        # State
        self._funding: dict[str, FundingSignal] = {}
        self._oi: dict[str, OISignal] = {}
        # OI history: symbol -> deque of (timestamp, oi_value)
        self._oi_history: dict[str, deque[tuple[float, float]]] = {}
        self._OI_HISTORY_MAX = 300  # ~5 min at 1s intervals

    # ------------------------------------------------------------------
    # Public query API
    # ------------------------------------------------------------------

    def get_funding(self, symbol: str) -> FundingSignal | None:
        return self._funding.get(symbol)

    def get_oi(self, symbol: str) -> OISignal | None:
        return self._oi.get(symbol)

    def get_vol_regime(self, symbol: str) -> VolSignal | None:
        """Compute volatility regime on-demand from SpotBuffer."""
        history = self._spot_buffer.get_price_history(symbol, self._vol_window_seconds)
        if len(history) < self._vol_min_data_points:
            return None

        sigma = self._compute_realized_vol(history)
        if sigma is None:
            return None

        if sigma < self._vol_low_threshold:
            regime = VolRegime.LOW
        elif sigma > self._vol_high_threshold:
            regime = VolRegime.HIGH
        else:
            regime = VolRegime.MEDIUM

        return VolSignal(
            symbol=symbol,
            realized_vol=sigma,
            regime=regime,
            timestamp=time.time(),
        )

    def get_snapshot(self, symbol: str) -> AlphaSnapshot:
        return AlphaSnapshot(
            symbol=symbol,
            funding=self.get_funding(symbol),
            oi=self.get_oi(symbol),
            vol=self.get_vol_regime(symbol),
        )

    def is_funding_stale(self, symbol: str, max_age_seconds: float = 57600.0) -> bool:
        """Check if funding data is stale (default: 16h = 2x poll interval)."""
        signal = self._funding.get(symbol)
        if signal is None:
            return True
        return (time.time() - signal.timestamp) > max_age_seconds

    def is_oi_stale(self, symbol: str, max_age_seconds: float = 300.0) -> bool:
        """Check if OI data is stale (default: 5 min = 5x poll interval)."""
        signal = self._oi.get(symbol)
        if signal is None:
            return True
        return (time.time() - signal.timestamp) > max_age_seconds

    # ------------------------------------------------------------------
    # Background loops
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Start background polling loops for funding and OI."""
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10),
        )
        self._log.info(
            "alpha_signals_starting",
            symbols=self._symbols,
            funding_interval=self._funding_poll_seconds,
            oi_interval=self._oi_poll_seconds,
        )
        try:
            await asyncio.gather(
                self._funding_loop(),
                self._oi_loop(),
            )
        except asyncio.CancelledError:
            self._log.info("alpha_signals_cancelled")
        finally:
            await self._close_session()

    async def close(self) -> None:
        """Clean up HTTP session."""
        await self._close_session()

    async def _close_session(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _funding_loop(self) -> None:
        """Poll funding rate for each symbol on schedule."""
        # Fetch immediately on startup, then sleep
        while True:
            for symbol in self._symbols:
                await self._fetch_funding(symbol)
            await asyncio.sleep(self._funding_poll_seconds)

    async def _oi_loop(self) -> None:
        """Poll open interest for each symbol on schedule."""
        while True:
            for symbol in self._symbols:
                await self._fetch_oi(symbol)
            await asyncio.sleep(self._oi_poll_seconds)

    # ------------------------------------------------------------------
    # Funding rate fetch
    # ------------------------------------------------------------------

    async def _fetch_funding(self, symbol: str) -> None:
        """Fetch latest funding rate from Binance Futures."""
        if self._session is None or self._session.closed:
            return
        url = f"{_BINANCE_FUTURES_BASE}/fapi/v1/fundingRate"
        params = {"symbol": symbol, "limit": "1"}
        try:
            async with self._session.get(url, params=params) as resp:
                if resp.status != 200:
                    self._log.warning(
                        "funding_fetch_error",
                        symbol=symbol,
                        status=resp.status,
                    )
                    return
                data = await resp.json()

            if not data or not isinstance(data, list) or len(data) == 0:
                return

            entry = data[0]
            rate = float(entry["fundingRate"])
            next_time = int(entry.get("fundingTime", 0))

            if rate < self._funding_bullish_threshold:
                bias = FundingBias.BULLISH
            elif rate > self._funding_bearish_threshold:
                bias = FundingBias.BEARISH
            else:
                bias = FundingBias.NEUTRAL

            signal = FundingSignal(
                symbol=symbol,
                rate=rate,
                bias=bias,
                timestamp=time.time(),
                next_funding_time=next_time,
            )
            self._funding[symbol] = signal
            self._log.info(
                "funding_updated",
                symbol=symbol,
                rate=rate,
                bias=bias.value,
            )

        except (TimeoutError, aiohttp.ClientError, KeyError, ValueError) as exc:
            self._log.warning(
                "funding_fetch_exception",
                symbol=symbol,
                error=str(exc),
            )

    # ------------------------------------------------------------------
    # Open Interest fetch
    # ------------------------------------------------------------------

    async def _fetch_oi(self, symbol: str) -> None:
        """Fetch open interest and compute delta/trend."""
        if self._session is None or self._session.closed:
            return
        url = f"{_BINANCE_FUTURES_BASE}/fapi/v1/openInterest"
        params = {"symbol": symbol}
        try:
            async with self._session.get(url, params=params) as resp:
                if resp.status != 200:
                    self._log.warning(
                        "oi_fetch_error",
                        symbol=symbol,
                        status=resp.status,
                    )
                    return
                data = await resp.json()

            current_oi = float(data["openInterest"])
            now = time.time()

            # Store in history
            if symbol not in self._oi_history:
                self._oi_history[symbol] = deque(maxlen=self._OI_HISTORY_MAX)
            self._oi_history[symbol].append((now, current_oi))

            # Compute delta from oldest entry in history
            history = self._oi_history[symbol]
            if len(history) < 2:
                # First data point — store signal with zero delta
                self._oi[symbol] = OISignal(
                    symbol=symbol,
                    current_oi=current_oi,
                    delta_pct=0.0,
                    trend=OITrend.FLAT,
                    price_diverging=False,
                    timestamp=now,
                )
                self._log.info(
                    "oi_updated",
                    symbol=symbol,
                    oi=current_oi,
                    delta_pct=0.0,
                    trend="FLAT",
                )
                return

            oldest_oi = history[0][1]
            if oldest_oi <= 0:
                return

            delta_pct = (current_oi - oldest_oi) / oldest_oi

            # Classify trend
            if delta_pct > self._oi_rising_threshold:
                trend = OITrend.RISING
            elif delta_pct < self._oi_falling_threshold:
                trend = OITrend.FALLING
            else:
                trend = OITrend.FLAT

            # Detect price divergence: OI rising but price falling, or vice versa
            price_diverging = self._detect_price_divergence(
                symbol,
                trend,
            )

            signal = OISignal(
                symbol=symbol,
                current_oi=current_oi,
                delta_pct=delta_pct,
                trend=trend,
                price_diverging=price_diverging,
                timestamp=now,
            )
            self._oi[symbol] = signal
            self._log.info(
                "oi_updated",
                symbol=symbol,
                oi=current_oi,
                delta_pct=round(delta_pct, 4),
                trend=trend.value,
                price_diverging=price_diverging,
            )

        except (TimeoutError, aiohttp.ClientError, KeyError, ValueError) as exc:
            self._log.warning(
                "oi_fetch_exception",
                symbol=symbol,
                error=str(exc),
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _detect_price_divergence(self, symbol: str, oi_trend: OITrend) -> bool:
        """Detect OI vs price divergence using SpotBuffer data.

        Divergence = OI rising + price falling, or OI falling + price rising.
        """
        if oi_trend == OITrend.FLAT:
            return False

        history = self._spot_buffer.get_price_history(symbol, self._vol_window_seconds)
        if len(history) < 2:
            return False

        start_price = history[0][1]
        end_price = history[-1][1]
        if start_price <= 0:
            return False

        price_change = (end_price - start_price) / start_price

        if oi_trend == OITrend.RISING and price_change < -0.001:
            return True  # OI rising, price falling
        return oi_trend == OITrend.FALLING and price_change > 0.001

    @staticmethod
    def _compute_realized_vol(
        history: list[tuple[float, float]],
    ) -> float | None:
        """Compute realized 1-minute volatility from price history.

        Same log-return stddev approach as sniper's _estimate_realized_vol().
        """
        if len(history) < 2:
            return None

        log_returns: list[float] = []
        for i in range(1, len(history)):
            prev_price = history[i - 1][1]
            curr_price = history[i][1]
            if prev_price > 0:
                log_returns.append(math.log(curr_price / prev_price))

        if not log_returns:
            return None

        n = len(log_returns)
        mean = sum(log_returns) / n
        variance = sum((r - mean) ** 2 for r in log_returns) / n
        stddev = math.sqrt(variance)

        # Scale to per-minute
        total_time = history[-1][0] - history[0][0]
        if total_time <= 0:
            return stddev

        ticks_per_minute = (len(history) - 1) / (total_time / 60.0)
        sigma = stddev * math.sqrt(max(ticks_per_minute, 1.0))

        return sigma
