"""Tests for Chainlink oracle utility."""

from __future__ import annotations

import pytest

from src.utils.chainlink import _decode_latest_round_data, validate_spot_price


class TestDecodeLatestRoundData:
    """Test ABI decoding of latestRoundData()."""

    def test_decode_btc_price(self) -> None:
        """Decode a realistic BTC/USD price (~$43,250.12345678)."""
        # Encode: roundId=1, answer=4325012345678, startedAt=..., updatedAt=..., answeredInRound=...
        answer = 4325012345678  # 8 decimals → $43,250.12345678
        hex_data = "0x" + (
            "0000000000000000000000000000000000000000000000000000000000000001"  # roundId
            + hex(answer)[2:].zfill(64)  # answer
            + "0000000000000000000000000000000000000000000000000000000065f0a000"  # startedAt
            + "0000000000000000000000000000000000000000000000000000000065f0a000"  # updatedAt
            + "0000000000000000000000000000000000000000000000000000000000000001"  # answeredInRound
        )
        result = _decode_latest_round_data(hex_data)
        assert result is not None
        assert abs(result - 43250.12345678) < 0.01

    def test_decode_eth_price(self) -> None:
        """Decode a realistic ETH/USD price (~$2,650.50)."""
        answer = 265050000000  # 8 decimals → $2,650.50
        hex_data = "0x" + (
            "0000000000000000000000000000000000000000000000000000000000000001"
            + hex(answer)[2:].zfill(64)
            + "0000000000000000000000000000000000000000000000000000000065f0a000"
            + "0000000000000000000000000000000000000000000000000000000065f0a000"
            + "0000000000000000000000000000000000000000000000000000000000000001"
        )
        result = _decode_latest_round_data(hex_data)
        assert result is not None
        assert abs(result - 2650.50) < 0.01

    def test_decode_zero_answer(self) -> None:
        """Zero answer should return None."""
        hex_data = "0x" + "00" * 160
        result = _decode_latest_round_data(hex_data)
        assert result is None

    def test_decode_short_data(self) -> None:
        """Too-short data should return None."""
        result = _decode_latest_round_data("0x1234")
        assert result is None


class TestValidateSpotPrice:
    """Test Binance-Chainlink price validation."""

    @pytest.mark.asyncio
    async def test_oracle_unavailable_passes(self) -> None:
        """When oracle is unavailable, validation passes (don't block trades)."""
        # Use a non-existent asset to trigger None return
        is_valid, oracle_price, divergence = await validate_spot_price(
            asset="FAKE",
            binance_price=43000.0,
        )
        assert is_valid is True
        assert oracle_price is None
        assert divergence is None

    @pytest.mark.asyncio
    async def test_zero_binance_price_passes(self) -> None:
        """Zero Binance price should pass (edge case protection)."""
        is_valid, _, _ = await validate_spot_price(
            asset="BTC",
            binance_price=0.0,
            rpc_url="http://localhost:1",  # won't connect
        )
        # Should pass since oracle call will fail/timeout
        assert is_valid is True
