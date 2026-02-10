"""Tests for src.data.alpha_signals."""

from __future__ import annotations

import asyncio
import os
import time
from collections import deque
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("BOT_PRIVATE_KEY", "0x" + "ab" * 32)

import pytest

from src.data.alpha_signals import (
    AlphaSignalProvider,
    AlphaSnapshot,
    FundingBias,
    FundingSignal,
    OISignal,
    OITrend,
    VolRegime,
    VolSignal,
)
from src.data.spot_buffer import SpotBuffer, SpotPriceUpdate

# ---------------------------------------------------------------------------
# Enum tests
# ---------------------------------------------------------------------------


class TestEnums:
    """Test enum values and string representation."""

    def test_funding_bias_values(self) -> None:
        assert FundingBias.BULLISH == "BULLISH"
        assert FundingBias.BEARISH == "BEARISH"
        assert FundingBias.NEUTRAL == "NEUTRAL"

    def test_oi_trend_values(self) -> None:
        assert OITrend.RISING == "RISING"
        assert OITrend.FALLING == "FALLING"
        assert OITrend.FLAT == "FLAT"

    def test_vol_regime_values(self) -> None:
        assert VolRegime.LOW == "LOW"
        assert VolRegime.MEDIUM == "MEDIUM"
        assert VolRegime.HIGH == "HIGH"


# ---------------------------------------------------------------------------
# Dataclass tests
# ---------------------------------------------------------------------------


class TestFundingSignal:
    def test_creation(self) -> None:
        sig = FundingSignal(
            symbol="BTCUSDT",
            rate=-0.0002,
            bias=FundingBias.BULLISH,
            timestamp=1700000000.0,
            next_funding_time=1700028800000,
        )
        assert sig.symbol == "BTCUSDT"
        assert sig.rate == -0.0002
        assert sig.bias == FundingBias.BULLISH

    def test_frozen(self) -> None:
        sig = FundingSignal("BTCUSDT", 0.0001, FundingBias.BEARISH, 1.0, 2)
        with pytest.raises(AttributeError):
            sig.rate = 0.0  # type: ignore[misc]


class TestOISignal:
    def test_creation(self) -> None:
        sig = OISignal(
            symbol="BTCUSDT",
            current_oi=50000.0,
            delta_pct=0.05,
            trend=OITrend.RISING,
            price_diverging=True,
            timestamp=1.0,
        )
        assert sig.current_oi == 50000.0
        assert sig.trend == OITrend.RISING
        assert sig.price_diverging is True


class TestVolSignal:
    def test_creation(self) -> None:
        sig = VolSignal(
            symbol="BTCUSDT",
            realized_vol=0.003,
            regime=VolRegime.HIGH,
            timestamp=1.0,
        )
        assert sig.regime == VolRegime.HIGH
        assert sig.realized_vol == 0.003


class TestAlphaSnapshot:
    def test_defaults_none(self) -> None:
        snap = AlphaSnapshot(symbol="BTCUSDT")
        assert snap.funding is None
        assert snap.oi is None
        assert snap.vol is None

    def test_with_signals(self) -> None:
        funding = FundingSignal("BTCUSDT", 0.0001, FundingBias.BEARISH, 1.0, 2)
        snap = AlphaSnapshot(symbol="BTCUSDT", funding=funding)
        assert snap.funding is funding


# ---------------------------------------------------------------------------
# Funding classification
# ---------------------------------------------------------------------------


class TestFundingClassification:
    """Test that funding rates are classified correctly."""

    def _make_provider(self) -> AlphaSignalProvider:
        buf = SpotBuffer()
        return AlphaSignalProvider(
            symbols=["BTCUSDT"],
            spot_buffer=buf,
            funding_bullish_threshold=-0.0001,
            funding_bearish_threshold=0.0001,
        )

    @pytest.mark.asyncio
    async def test_bullish_funding(self) -> None:
        """Negative funding rate (shorts paying longs) → BULLISH."""
        provider = self._make_provider()
        # Simulate a funding fetch with mocked response
        mock_session = AsyncMock()
        mock_session.closed = False
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(
            return_value=[
                {
                    "fundingRate": "-0.0005",
                    "fundingTime": 1700028800000,
                }
            ]
        )
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = MagicMock(return_value=mock_response)
        provider._session = mock_session

        await provider._fetch_funding("BTCUSDT")
        sig = provider.get_funding("BTCUSDT")
        assert sig is not None
        assert sig.bias == FundingBias.BULLISH
        assert sig.rate == -0.0005

    @pytest.mark.asyncio
    async def test_bearish_funding(self) -> None:
        """Positive funding rate (longs paying shorts) → BEARISH."""
        provider = self._make_provider()
        mock_session = AsyncMock()
        mock_session.closed = False
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(
            return_value=[
                {
                    "fundingRate": "0.0005",
                    "fundingTime": 1700028800000,
                }
            ]
        )
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = MagicMock(return_value=mock_response)
        provider._session = mock_session

        await provider._fetch_funding("BTCUSDT")
        sig = provider.get_funding("BTCUSDT")
        assert sig is not None
        assert sig.bias == FundingBias.BEARISH

    @pytest.mark.asyncio
    async def test_neutral_funding(self) -> None:
        """Funding rate near zero → NEUTRAL."""
        provider = self._make_provider()
        mock_session = AsyncMock()
        mock_session.closed = False
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(
            return_value=[
                {
                    "fundingRate": "0.00005",
                    "fundingTime": 1700028800000,
                }
            ]
        )
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = MagicMock(return_value=mock_response)
        provider._session = mock_session

        await provider._fetch_funding("BTCUSDT")
        sig = provider.get_funding("BTCUSDT")
        assert sig is not None
        assert sig.bias == FundingBias.NEUTRAL

    @pytest.mark.asyncio
    async def test_funding_at_exact_threshold(self) -> None:
        """Funding rate exactly at threshold → NEUTRAL (not > or <)."""
        provider = self._make_provider()
        mock_session = AsyncMock()
        mock_session.closed = False
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(
            return_value=[
                {
                    "fundingRate": "-0.0001",
                    "fundingTime": 1700028800000,
                }
            ]
        )
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = MagicMock(return_value=mock_response)
        provider._session = mock_session

        await provider._fetch_funding("BTCUSDT")
        sig = provider.get_funding("BTCUSDT")
        assert sig is not None
        # -0.0001 is not < -0.0001 so it's NEUTRAL
        assert sig.bias == FundingBias.NEUTRAL


# ---------------------------------------------------------------------------
# OI delta and trend classification
# ---------------------------------------------------------------------------


class TestOIClassification:
    """Test OI delta computation and trend classification."""

    def _make_provider(self) -> AlphaSignalProvider:
        buf = SpotBuffer()
        return AlphaSignalProvider(
            symbols=["BTCUSDT"],
            spot_buffer=buf,
            oi_rising_threshold=0.02,
            oi_falling_threshold=-0.02,
        )

    @pytest.mark.asyncio
    async def test_oi_rising(self) -> None:
        """OI increased >2% → RISING trend."""
        provider = self._make_provider()
        mock_session = AsyncMock()
        mock_session.closed = False
        provider._session = mock_session

        # Seed history with initial value
        provider._oi_history["BTCUSDT"] = deque(maxlen=300)
        provider._oi_history["BTCUSDT"].append((time.time() - 60, 100000.0))

        # Mock response with 5% higher OI
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={"openInterest": "105000.0"})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = MagicMock(return_value=mock_response)

        await provider._fetch_oi("BTCUSDT")
        sig = provider.get_oi("BTCUSDT")
        assert sig is not None
        assert sig.trend == OITrend.RISING
        assert sig.delta_pct == pytest.approx(0.05, abs=0.001)

    @pytest.mark.asyncio
    async def test_oi_falling(self) -> None:
        """OI decreased >2% → FALLING trend."""
        provider = self._make_provider()
        mock_session = AsyncMock()
        mock_session.closed = False
        provider._session = mock_session

        provider._oi_history["BTCUSDT"] = deque(maxlen=300)
        provider._oi_history["BTCUSDT"].append((time.time() - 60, 100000.0))

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={"openInterest": "95000.0"})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = MagicMock(return_value=mock_response)

        await provider._fetch_oi("BTCUSDT")
        sig = provider.get_oi("BTCUSDT")
        assert sig is not None
        assert sig.trend == OITrend.FALLING
        assert sig.delta_pct == pytest.approx(-0.05, abs=0.001)

    @pytest.mark.asyncio
    async def test_oi_flat(self) -> None:
        """OI changed <2% → FLAT trend."""
        provider = self._make_provider()
        mock_session = AsyncMock()
        mock_session.closed = False
        provider._session = mock_session

        provider._oi_history["BTCUSDT"] = deque(maxlen=300)
        provider._oi_history["BTCUSDT"].append((time.time() - 60, 100000.0))

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={"openInterest": "100500.0"})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = MagicMock(return_value=mock_response)

        await provider._fetch_oi("BTCUSDT")
        sig = provider.get_oi("BTCUSDT")
        assert sig is not None
        assert sig.trend == OITrend.FLAT

    @pytest.mark.asyncio
    async def test_oi_first_data_point(self) -> None:
        """First OI fetch stores FLAT with zero delta."""
        provider = self._make_provider()
        mock_session = AsyncMock()
        mock_session.closed = False
        provider._session = mock_session

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={"openInterest": "50000.0"})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = MagicMock(return_value=mock_response)

        await provider._fetch_oi("BTCUSDT")
        sig = provider.get_oi("BTCUSDT")
        assert sig is not None
        assert sig.trend == OITrend.FLAT
        assert sig.delta_pct == 0.0

    @pytest.mark.asyncio
    async def test_oi_history_pruning(self) -> None:
        """OI history is pruned to _OI_HISTORY_MAX entries."""
        provider = self._make_provider()
        provider._OI_HISTORY_MAX = 5  # small for testing
        # Re-create history deque with small maxlen
        provider._oi_history["BTCUSDT"] = deque(maxlen=5)

        mock_session = AsyncMock()
        mock_session.closed = False
        provider._session = mock_session

        # Add 10 entries via fetch
        for i in range(10):
            mock_response = AsyncMock()
            mock_response.status = 200
            mock_response.json = AsyncMock(return_value={"openInterest": str(50000 + i * 100)})
            mock_response.__aenter__ = AsyncMock(return_value=mock_response)
            mock_response.__aexit__ = AsyncMock(return_value=False)
            mock_session.get = MagicMock(return_value=mock_response)
            await provider._fetch_oi("BTCUSDT")

        assert len(provider._oi_history["BTCUSDT"]) == 5


# ---------------------------------------------------------------------------
# OI price divergence
# ---------------------------------------------------------------------------


class TestOIPriceDivergence:
    """Test OI vs price divergence detection."""

    def test_oi_rising_price_falling(self) -> None:
        """OI rising + price falling → divergence."""
        buf = SpotBuffer(window_seconds=900)
        now = time.time()
        buf.add(SpotPriceUpdate("BTCUSDT", 50000.0, now - 300))
        buf.add(SpotPriceUpdate("BTCUSDT", 49900.0, now))  # -0.2%

        provider = AlphaSignalProvider(["BTCUSDT"], buf)
        assert provider._detect_price_divergence("BTCUSDT", OITrend.RISING) is True

    def test_oi_falling_price_rising(self) -> None:
        """OI falling + price rising → divergence."""
        buf = SpotBuffer(window_seconds=900)
        now = time.time()
        buf.add(SpotPriceUpdate("BTCUSDT", 50000.0, now - 300))
        buf.add(SpotPriceUpdate("BTCUSDT", 50100.0, now))  # +0.2%

        provider = AlphaSignalProvider(["BTCUSDT"], buf)
        assert provider._detect_price_divergence("BTCUSDT", OITrend.FALLING) is True

    def test_oi_rising_price_rising_no_divergence(self) -> None:
        """OI rising + price rising → no divergence."""
        buf = SpotBuffer(window_seconds=900)
        now = time.time()
        buf.add(SpotPriceUpdate("BTCUSDT", 50000.0, now - 300))
        buf.add(SpotPriceUpdate("BTCUSDT", 50200.0, now))

        provider = AlphaSignalProvider(["BTCUSDT"], buf)
        assert provider._detect_price_divergence("BTCUSDT", OITrend.RISING) is False

    def test_oi_flat_no_divergence(self) -> None:
        """FLAT OI → no divergence regardless of price."""
        buf = SpotBuffer(window_seconds=900)
        now = time.time()
        buf.add(SpotPriceUpdate("BTCUSDT", 50000.0, now - 300))
        buf.add(SpotPriceUpdate("BTCUSDT", 49000.0, now))

        provider = AlphaSignalProvider(["BTCUSDT"], buf)
        assert provider._detect_price_divergence("BTCUSDT", OITrend.FLAT) is False

    def test_no_spot_data(self) -> None:
        """No spot data → no divergence."""
        buf = SpotBuffer(window_seconds=900)
        provider = AlphaSignalProvider(["BTCUSDT"], buf)
        assert provider._detect_price_divergence("BTCUSDT", OITrend.RISING) is False


# ---------------------------------------------------------------------------
# Volatility regime
# ---------------------------------------------------------------------------


class TestVolRegime:
    """Test vol regime computation from SpotBuffer data."""

    def _make_buffer_with_vol(
        self, base_price: float, stddev_per_tick: float, n_ticks: int = 50
    ) -> SpotBuffer:
        """Create a SpotBuffer with synthetic price data at a known vol level."""
        buf = SpotBuffer(window_seconds=900, max_size=10000)
        now = time.time()
        price = base_price
        for i in range(n_ticks):
            # Alternate up/down to create known volatility
            if i % 2 == 0:
                price = base_price * (1 + stddev_per_tick)
            else:
                price = base_price * (1 - stddev_per_tick)
            buf.add(SpotPriceUpdate("BTCUSDT", price, now - n_ticks + i))
        return buf

    def test_low_vol_regime(self) -> None:
        """Very low volatility → LOW regime."""
        buf = SpotBuffer(window_seconds=900, max_size=10000)
        now = time.time()
        # Constant price → near-zero vol
        for i in range(20):
            buf.add(SpotPriceUpdate("BTCUSDT", 50000.0, now - 20 + i))

        provider = AlphaSignalProvider(
            ["BTCUSDT"],
            buf,
            vol_low_threshold=0.0005,
            vol_high_threshold=0.002,
        )
        sig = provider.get_vol_regime("BTCUSDT")
        assert sig is not None
        assert sig.regime == VolRegime.LOW

    def test_high_vol_regime(self) -> None:
        """High volatility → HIGH regime."""
        buf = SpotBuffer(window_seconds=900, max_size=10000)
        now = time.time()
        # Large alternating moves → high vol
        for i in range(30):
            price = 50000.0 if i % 2 == 0 else 50500.0  # 1% swings
            buf.add(SpotPriceUpdate("BTCUSDT", price, now - 30 + i))

        provider = AlphaSignalProvider(
            ["BTCUSDT"],
            buf,
            vol_low_threshold=0.0005,
            vol_high_threshold=0.002,
        )
        sig = provider.get_vol_regime("BTCUSDT")
        assert sig is not None
        assert sig.regime == VolRegime.HIGH

    def test_insufficient_data_returns_none(self) -> None:
        """Not enough data points → returns None."""
        buf = SpotBuffer(window_seconds=900)
        now = time.time()
        buf.add(SpotPriceUpdate("BTCUSDT", 50000.0, now))

        provider = AlphaSignalProvider(
            ["BTCUSDT"],
            buf,
            vol_min_data_points=10,
        )
        sig = provider.get_vol_regime("BTCUSDT")
        assert sig is None

    def test_no_data_returns_none(self) -> None:
        """No data at all → returns None."""
        buf = SpotBuffer()
        provider = AlphaSignalProvider(["BTCUSDT"], buf)
        assert provider.get_vol_regime("BTCUSDT") is None


# ---------------------------------------------------------------------------
# Staleness checks
# ---------------------------------------------------------------------------


class TestStaleness:
    """Test staleness detection for funding and OI signals."""

    def test_funding_stale_no_data(self) -> None:
        """No funding data → stale."""
        buf = SpotBuffer()
        provider = AlphaSignalProvider(["BTCUSDT"], buf)
        assert provider.is_funding_stale("BTCUSDT") is True

    def test_funding_not_stale(self) -> None:
        """Recent funding data → not stale."""
        buf = SpotBuffer()
        provider = AlphaSignalProvider(["BTCUSDT"], buf)
        provider._funding["BTCUSDT"] = FundingSignal(
            "BTCUSDT",
            0.0001,
            FundingBias.BEARISH,
            time.time(),
            0,
        )
        assert provider.is_funding_stale("BTCUSDT") is False

    def test_funding_stale_old_data(self) -> None:
        """Old funding data → stale."""
        buf = SpotBuffer()
        provider = AlphaSignalProvider(["BTCUSDT"], buf)
        provider._funding["BTCUSDT"] = FundingSignal(
            "BTCUSDT",
            0.0001,
            FundingBias.BEARISH,
            time.time() - 100000,
            0,  # >16h ago
        )
        assert provider.is_funding_stale("BTCUSDT") is True

    def test_oi_stale_no_data(self) -> None:
        """No OI data → stale."""
        buf = SpotBuffer()
        provider = AlphaSignalProvider(["BTCUSDT"], buf)
        assert provider.is_oi_stale("BTCUSDT") is True

    def test_oi_not_stale(self) -> None:
        """Recent OI data → not stale."""
        buf = SpotBuffer()
        provider = AlphaSignalProvider(["BTCUSDT"], buf)
        provider._oi["BTCUSDT"] = OISignal(
            "BTCUSDT",
            50000,
            0.01,
            OITrend.FLAT,
            False,
            time.time(),
        )
        assert provider.is_oi_stale("BTCUSDT") is False

    def test_oi_stale_old_data(self) -> None:
        """Old OI data → stale."""
        buf = SpotBuffer()
        provider = AlphaSignalProvider(["BTCUSDT"], buf)
        provider._oi["BTCUSDT"] = OISignal(
            "BTCUSDT",
            50000,
            0.01,
            OITrend.FLAT,
            False,
            time.time() - 600,  # >5 min ago
        )
        assert provider.is_oi_stale("BTCUSDT") is True


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


class TestSnapshot:
    """Test the get_snapshot() convenience method."""

    def test_snapshot_empty(self) -> None:
        buf = SpotBuffer()
        provider = AlphaSignalProvider(["BTCUSDT"], buf)
        snap = provider.get_snapshot("BTCUSDT")
        assert snap.symbol == "BTCUSDT"
        assert snap.funding is None
        assert snap.oi is None
        assert snap.vol is None

    def test_snapshot_with_funding(self) -> None:
        buf = SpotBuffer()
        provider = AlphaSignalProvider(["BTCUSDT"], buf)
        provider._funding["BTCUSDT"] = FundingSignal(
            "BTCUSDT",
            -0.001,
            FundingBias.BULLISH,
            time.time(),
            0,
        )
        snap = provider.get_snapshot("BTCUSDT")
        assert snap.funding is not None
        assert snap.funding.bias == FundingBias.BULLISH


# ---------------------------------------------------------------------------
# API error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    """Test graceful handling of API errors."""

    @pytest.mark.asyncio
    async def test_funding_http_error(self) -> None:
        """Non-200 status code → no crash, no signal stored."""
        buf = SpotBuffer()
        provider = AlphaSignalProvider(["BTCUSDT"], buf)

        mock_session = AsyncMock()
        mock_response = AsyncMock()
        mock_response.status = 429  # Rate limited
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = MagicMock(return_value=mock_response)
        provider._session = mock_session

        await provider._fetch_funding("BTCUSDT")
        assert provider.get_funding("BTCUSDT") is None

    @pytest.mark.asyncio
    async def test_funding_empty_response(self) -> None:
        """Empty JSON array → no crash, no signal stored."""
        buf = SpotBuffer()
        provider = AlphaSignalProvider(["BTCUSDT"], buf)

        mock_session = AsyncMock()
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=[])
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = MagicMock(return_value=mock_response)
        provider._session = mock_session

        await provider._fetch_funding("BTCUSDT")
        assert provider.get_funding("BTCUSDT") is None

    @pytest.mark.asyncio
    async def test_oi_http_error(self) -> None:
        """Non-200 status code for OI → no crash."""
        buf = SpotBuffer()
        provider = AlphaSignalProvider(["BTCUSDT"], buf)

        mock_session = AsyncMock()
        mock_response = AsyncMock()
        mock_response.status = 500
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = MagicMock(return_value=mock_response)
        provider._session = mock_session

        await provider._fetch_oi("BTCUSDT")
        assert provider.get_oi("BTCUSDT") is None

    @pytest.mark.asyncio
    async def test_funding_session_closed(self) -> None:
        """Closed session → early return, no crash."""
        buf = SpotBuffer()
        provider = AlphaSignalProvider(["BTCUSDT"], buf)
        provider._session = None

        # Should not raise
        await provider._fetch_funding("BTCUSDT")
        assert provider.get_funding("BTCUSDT") is None

    @pytest.mark.asyncio
    async def test_oi_session_closed(self) -> None:
        """Closed session → early return, no crash."""
        buf = SpotBuffer()
        provider = AlphaSignalProvider(["BTCUSDT"], buf)
        provider._session = None

        await provider._fetch_oi("BTCUSDT")
        assert provider.get_oi("BTCUSDT") is None


# ---------------------------------------------------------------------------
# Realized vol computation
# ---------------------------------------------------------------------------


class TestRealizedVol:
    """Test the static _compute_realized_vol method."""

    def test_constant_price(self) -> None:
        """Constant price → zero vol."""
        history = [(float(i), 100.0) for i in range(20)]
        vol = AlphaSignalProvider._compute_realized_vol(history)
        assert vol is not None
        assert vol == pytest.approx(0.0, abs=1e-10)

    def test_insufficient_data(self) -> None:
        """Single data point → None."""
        vol = AlphaSignalProvider._compute_realized_vol([(1.0, 100.0)])
        assert vol is None

    def test_empty_data(self) -> None:
        """Empty history → None."""
        vol = AlphaSignalProvider._compute_realized_vol([])
        assert vol is None

    def test_positive_vol(self) -> None:
        """Alternating prices produce positive vol."""
        history = []
        for i in range(30):
            price = 100.0 if i % 2 == 0 else 101.0
            history.append((float(i), price))
        vol = AlphaSignalProvider._compute_realized_vol(history)
        assert vol is not None
        assert vol > 0

    def test_zero_time_span(self) -> None:
        """All timestamps identical → still returns a value."""
        history = [(1.0, 100.0), (1.0, 101.0), (1.0, 100.5)]
        vol = AlphaSignalProvider._compute_realized_vol(history)
        assert vol is not None
        # With zero total_time, returns raw stddev
        assert vol > 0


# ---------------------------------------------------------------------------
# Medium vol regime
# ---------------------------------------------------------------------------


class TestVolRegimeMedium:
    """Test MEDIUM vol regime (between LOW and HIGH thresholds)."""

    def test_medium_vol_regime(self) -> None:
        """Moderate volatility → MEDIUM regime."""
        buf = SpotBuffer(window_seconds=900, max_size=10000)
        now = time.time()
        # Small but non-trivial oscillation → medium vol
        for i in range(30):
            # ~0.1% alternation → produces a sigma in the MEDIUM range
            price = 50000.0 if i % 2 == 0 else 50050.0  # 0.1% swing
            buf.add(SpotPriceUpdate("BTCUSDT", price, now - 30 + i))

        provider = AlphaSignalProvider(
            ["BTCUSDT"],
            buf,
            vol_low_threshold=0.0001,
            vol_high_threshold=0.01,
        )
        sig = provider.get_vol_regime("BTCUSDT")
        assert sig is not None
        assert sig.regime == VolRegime.MEDIUM


# ---------------------------------------------------------------------------
# Lifecycle: run() and close()
# ---------------------------------------------------------------------------


class TestLifecycle:
    """Test run() startup and close() cleanup."""

    @pytest.mark.asyncio
    async def test_close_idempotent(self) -> None:
        """Calling close() multiple times does not crash."""
        buf = SpotBuffer()
        provider = AlphaSignalProvider(["BTCUSDT"], buf)
        # close before run — no session to close
        await provider.close()
        await provider.close()  # second call is no-op

    @pytest.mark.asyncio
    async def test_close_after_session_created(self) -> None:
        """close() properly tears down an active session."""
        buf = SpotBuffer()
        provider = AlphaSignalProvider(["BTCUSDT"], buf)

        # Manually set up a mock session
        mock_session = AsyncMock()
        mock_session.closed = False

        async def mark_closed() -> None:
            mock_session.closed = True

        mock_session.close = mark_closed
        provider._session = mock_session

        await provider.close()
        assert provider._session is None

    @pytest.mark.asyncio
    async def test_run_cancellation(self) -> None:
        """run() exits cleanly on CancelledError."""
        buf = SpotBuffer()
        provider = AlphaSignalProvider(
            ["BTCUSDT"],
            buf,
            funding_poll_seconds=0.01,
            oi_poll_seconds=0.01,
        )

        # Start run() and cancel it almost immediately
        task = asyncio.create_task(provider.run())
        await asyncio.sleep(0.05)
        task.cancel()
        # Should not raise
        import contextlib

        with contextlib.suppress(asyncio.CancelledError):
            await task
        # Session should be cleaned up
        assert provider._session is None


# ---------------------------------------------------------------------------
# Timeout and network error handling
# ---------------------------------------------------------------------------


class TestNetworkErrors:
    """Test handling of network timeouts and client errors."""

    @pytest.mark.asyncio
    async def test_funding_timeout(self) -> None:
        """asyncio.TimeoutError → no crash, no signal."""
        buf = SpotBuffer()
        provider = AlphaSignalProvider(["BTCUSDT"], buf)
        mock_session = AsyncMock()
        mock_session.closed = False
        mock_session.get = MagicMock(side_effect=TimeoutError())
        provider._session = mock_session

        await provider._fetch_funding("BTCUSDT")
        assert provider.get_funding("BTCUSDT") is None

    @pytest.mark.asyncio
    async def test_oi_timeout(self) -> None:
        """asyncio.TimeoutError on OI → no crash, no signal."""
        buf = SpotBuffer()
        provider = AlphaSignalProvider(["BTCUSDT"], buf)
        mock_session = AsyncMock()
        mock_session.closed = False
        mock_session.get = MagicMock(side_effect=TimeoutError())
        provider._session = mock_session

        await provider._fetch_oi("BTCUSDT")
        assert provider.get_oi("BTCUSDT") is None

    @pytest.mark.asyncio
    async def test_funding_malformed_json(self) -> None:
        """Missing 'fundingRate' key → caught by KeyError handler."""
        buf = SpotBuffer()
        provider = AlphaSignalProvider(["BTCUSDT"], buf)
        mock_session = AsyncMock()
        mock_session.closed = False
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=[{"wrongKey": "value"}])
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = MagicMock(return_value=mock_response)
        provider._session = mock_session

        await provider._fetch_funding("BTCUSDT")
        assert provider.get_funding("BTCUSDT") is None

    @pytest.mark.asyncio
    async def test_oi_malformed_json(self) -> None:
        """Missing 'openInterest' key → caught by KeyError handler."""
        buf = SpotBuffer()
        provider = AlphaSignalProvider(["BTCUSDT"], buf)
        mock_session = AsyncMock()
        mock_session.closed = False
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={"wrongKey": "value"})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = MagicMock(return_value=mock_response)
        provider._session = mock_session

        await provider._fetch_oi("BTCUSDT")
        assert provider.get_oi("BTCUSDT") is None


# ---------------------------------------------------------------------------
# Price divergence edge cases
# ---------------------------------------------------------------------------


class TestPriceDivergenceEdgeCases:
    """Edge cases for _detect_price_divergence."""

    def test_zero_start_price(self) -> None:
        """Zero start price in spot buffer → no divergence (division guard)."""
        buf = SpotBuffer(window_seconds=900)
        now = time.time()
        buf.add(SpotPriceUpdate("BTCUSDT", 0.0, now - 300))
        buf.add(SpotPriceUpdate("BTCUSDT", 50000.0, now))

        provider = AlphaSignalProvider(["BTCUSDT"], buf)
        assert provider._detect_price_divergence("BTCUSDT", OITrend.RISING) is False

    def test_tiny_price_change_no_divergence(self) -> None:
        """Price moved <0.1% while OI rose → no divergence (below 0.1% threshold)."""
        buf = SpotBuffer(window_seconds=900)
        now = time.time()
        buf.add(SpotPriceUpdate("BTCUSDT", 50000.0, now - 300))
        buf.add(SpotPriceUpdate("BTCUSDT", 49980.0, now))  # -0.04%, below 0.1%

        provider = AlphaSignalProvider(["BTCUSDT"], buf)
        assert provider._detect_price_divergence("BTCUSDT", OITrend.RISING) is False

    def test_unknown_symbol_no_data(self) -> None:
        """Symbol not in buffer → no divergence."""
        buf = SpotBuffer(window_seconds=900)
        provider = AlphaSignalProvider(["BTCUSDT"], buf)
        assert provider._detect_price_divergence("ETHUSDT", OITrend.RISING) is False


# ---------------------------------------------------------------------------
# Multi-symbol support
# ---------------------------------------------------------------------------


class TestMultiSymbol:
    """Test that provider works with multiple symbols independently."""

    def test_separate_funding_per_symbol(self) -> None:
        """Funding signals are stored independently per symbol."""
        buf = SpotBuffer()
        provider = AlphaSignalProvider(["BTCUSDT", "ETHUSDT"], buf)
        provider._funding["BTCUSDT"] = FundingSignal(
            "BTCUSDT",
            -0.001,
            FundingBias.BULLISH,
            time.time(),
            0,
        )
        provider._funding["ETHUSDT"] = FundingSignal(
            "ETHUSDT",
            0.002,
            FundingBias.BEARISH,
            time.time(),
            0,
        )

        btc = provider.get_funding("BTCUSDT")
        eth = provider.get_funding("ETHUSDT")
        assert btc is not None and btc.bias == FundingBias.BULLISH
        assert eth is not None and eth.bias == FundingBias.BEARISH

    def test_snapshot_per_symbol(self) -> None:
        """Snapshots return the correct data for each symbol."""
        buf = SpotBuffer()
        provider = AlphaSignalProvider(["BTCUSDT", "ETHUSDT"], buf)
        provider._funding["BTCUSDT"] = FundingSignal(
            "BTCUSDT",
            0.0,
            FundingBias.NEUTRAL,
            time.time(),
            0,
        )

        btc_snap = provider.get_snapshot("BTCUSDT")
        eth_snap = provider.get_snapshot("ETHUSDT")
        assert btc_snap.funding is not None
        assert eth_snap.funding is None  # no data for ETH yet
