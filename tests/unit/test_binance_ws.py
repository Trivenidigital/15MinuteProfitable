"""Comprehensive tests for src.data.binance_ws."""

from __future__ import annotations

import json
import os
import time

os.environ.setdefault("BOT_PRIVATE_KEY", "0x" + "ab" * 32)

import pytest

from src.data.binance_ws import BinanceWebSocket
from src.data.spot_buffer import SpotBuffer


def _now_ms() -> int:
    """Return current time as milliseconds (Binance trade timestamp format)."""
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# Helper: create a BinanceWebSocket with default settings
# ---------------------------------------------------------------------------


def _make_ws(
    symbols: list[str] | None = None,
    ws_url: str = "wss://stream.binance.com:9443",
) -> tuple[BinanceWebSocket, SpotBuffer]:
    """Create a BinanceWebSocket and its SpotBuffer for testing."""
    buf = SpotBuffer(window_seconds=3600)
    ws = BinanceWebSocket(
        ws_url=ws_url,
        symbols=symbols or ["BTCUSDT"],
        spot_buffer=buf,
    )
    return ws, buf


# ---------------------------------------------------------------------------
# _build_stream_url
# ---------------------------------------------------------------------------


class TestBuildStreamUrl:
    """Tests for _build_stream_url()."""

    def test_single_symbol(self) -> None:
        """Single symbol produces correct URL."""
        ws, _ = _make_ws(symbols=["BTCUSDT"])
        url = ws._build_stream_url()
        assert url == "wss://stream.binance.com:9443/stream?streams=btcusdt@trade"

    def test_multiple_symbols(self) -> None:
        """Multiple symbols are joined with /."""
        ws, _ = _make_ws(symbols=["BTCUSDT", "ETHUSDT", "SOLUSDT"])
        url = ws._build_stream_url()
        assert url == (
            "wss://stream.binance.com:9443/stream?streams="
            "btcusdt@trade/ethusdt@trade/solusdt@trade"
        )

    def test_lowercase_input(self) -> None:
        """Lowercase symbol input is handled (uppercased internally, lowered in URL)."""
        ws, _ = _make_ws(symbols=["btcusdt"])
        url = ws._build_stream_url()
        assert "btcusdt@trade" in url

    def test_trailing_slash_in_base_url(self) -> None:
        """Trailing slash in base URL is stripped."""
        ws, _ = _make_ws(
            symbols=["BTCUSDT"],
            ws_url="wss://stream.binance.com:9443/",
        )
        url = ws._build_stream_url()
        assert url == "wss://stream.binance.com:9443/stream?streams=btcusdt@trade"

    def test_base_url_with_ws_suffix(self) -> None:
        """Base URL ending with /ws is handled correctly."""
        ws, _ = _make_ws(
            symbols=["BTCUSDT"],
            ws_url="wss://stream.binance.com:9443/ws",
        )
        url = ws._build_stream_url()
        assert url == "wss://stream.binance.com:9443/stream?streams=btcusdt@trade"

    def test_base_url_with_ws_trailing_slash(self) -> None:
        """Base URL ending with /ws/ is handled correctly."""
        ws, _ = _make_ws(
            symbols=["BTCUSDT"],
            ws_url="wss://stream.binance.com:9443/ws/",
        )
        url = ws._build_stream_url()
        assert url == "wss://stream.binance.com:9443/stream?streams=btcusdt@trade"


# ---------------------------------------------------------------------------
# _process_message: combined stream format
# ---------------------------------------------------------------------------


class TestProcessMessageCombinedStream:
    """Tests for _process_message() with combined stream format (has 'stream' and 'data')."""

    def test_valid_trade_updates_buffer(self) -> None:
        """Valid combined stream trade message feeds price into buffer."""
        ws, buf = _make_ws(symbols=["BTCUSDT"])
        now_ms = _now_ms()
        msg = json.dumps({
            "stream": "btcusdt@trade",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "p": "43256.78",
                "T": now_ms,
            },
        })
        ws._process_message(msg)
        assert buf.get_price("BTCUSDT") == 43256.78

    def test_timestamp_converted_from_millis(self) -> None:
        """Trade time in milliseconds is converted to seconds."""
        ws, buf = _make_ws(symbols=["BTCUSDT"])
        now_ms = _now_ms()
        msg = json.dumps({
            "stream": "btcusdt@trade",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "p": "43256.78",
                "T": now_ms,
            },
        })
        ws._process_message(msg)
        history = buf.get_price_history("BTCUSDT")
        assert len(history) == 1
        assert history[0][0] == pytest.approx(now_ms / 1000.0, abs=0.001)

    def test_multiple_updates_accumulate(self) -> None:
        """Multiple trade messages accumulate in buffer."""
        ws, buf = _make_ws(symbols=["BTCUSDT"])
        now_ms = _now_ms()
        for i, price in enumerate([43000.0, 43100.0, 43200.0]):
            msg = json.dumps({
                "stream": "btcusdt@trade",
                "data": {
                    "e": "trade",
                    "s": "BTCUSDT",
                    "p": str(price),
                    "T": now_ms + i * 1000,
                },
            })
            ws._process_message(msg)
        assert buf.get_price("BTCUSDT") == 43200.0
        assert len(buf.get_price_history("BTCUSDT")) == 3


# ---------------------------------------------------------------------------
# _process_message: direct stream format
# ---------------------------------------------------------------------------


class TestProcessMessageDirectStream:
    """Tests for _process_message() with direct stream format (no 'stream' key)."""

    def test_valid_direct_trade(self) -> None:
        """Direct stream trade message (no 'stream'/'data' wrapper) is handled."""
        ws, buf = _make_ws(symbols=["ETHUSDT"])
        now_ms = _now_ms()
        msg = json.dumps({
            "e": "trade",
            "s": "ETHUSDT",
            "p": "3500.25",
            "T": now_ms,
        })
        ws._process_message(msg)
        assert buf.get_price("ETHUSDT") == 3500.25

    def test_direct_stream_timestamp(self) -> None:
        """Direct stream with seconds timestamp is not divided."""
        ws, buf = _make_ws(symbols=["BTCUSDT"])
        now_s = int(time.time())
        msg = json.dumps({
            "e": "trade",
            "s": "BTCUSDT",
            "p": "43000.0",
            "T": now_s,  # already in seconds (< 1 trillion)
        })
        ws._process_message(msg)
        history = buf.get_price_history("BTCUSDT")
        assert history[0][0] == pytest.approx(float(now_s), abs=0.001)


# ---------------------------------------------------------------------------
# _process_message: invalid / edge cases
# ---------------------------------------------------------------------------


class TestProcessMessageEdgeCases:
    """Tests for _process_message() with invalid or edge-case input."""

    def test_invalid_json(self) -> None:
        """Invalid JSON does not crash."""
        ws, buf = _make_ws()
        ws._process_message("not valid json {{{")
        assert buf.get_price("BTCUSDT") is None

    def test_empty_string(self) -> None:
        """Empty string does not crash."""
        ws, buf = _make_ws()
        ws._process_message("")
        assert buf.get_price("BTCUSDT") is None

    def test_bytes_input(self) -> None:
        """Bytes input is handled (json.loads accepts bytes)."""
        ws, buf = _make_ws(symbols=["BTCUSDT"])
        now_ms = _now_ms()
        msg = json.dumps({
            "e": "trade",
            "s": "BTCUSDT",
            "p": "43000.0",
            "T": now_ms,
        }).encode("utf-8")
        ws._process_message(msg)
        assert buf.get_price("BTCUSDT") == 43000.0

    def test_non_trade_event_ignored(self) -> None:
        """Non-trade events (e.g. 'kline') are ignored."""
        ws, buf = _make_ws()
        msg = json.dumps({
            "e": "kline",
            "s": "BTCUSDT",
            "p": "43000.0",
            "T": 1704067200000,
        })
        ws._process_message(msg)
        assert buf.get_price("BTCUSDT") is None

    def test_missing_event_type_ignored(self) -> None:
        """Message without 'e' field is ignored."""
        ws, buf = _make_ws()
        msg = json.dumps({
            "s": "BTCUSDT",
            "p": "43000.0",
            "T": 1704067200000,
        })
        ws._process_message(msg)
        assert buf.get_price("BTCUSDT") is None

    def test_missing_symbol_ignored(self) -> None:
        """Message without 's' field is ignored."""
        ws, buf = _make_ws()
        msg = json.dumps({
            "e": "trade",
            "p": "43000.0",
            "T": 1704067200000,
        })
        ws._process_message(msg)
        assert buf.get_price("BTCUSDT") is None

    def test_missing_price_ignored(self) -> None:
        """Message without 'p' field is ignored."""
        ws, buf = _make_ws()
        msg = json.dumps({
            "e": "trade",
            "s": "BTCUSDT",
            "T": 1704067200000,
        })
        ws._process_message(msg)
        assert buf.get_price("BTCUSDT") is None

    def test_invalid_price_string(self) -> None:
        """Non-numeric price string does not crash."""
        ws, buf = _make_ws()
        msg = json.dumps({
            "e": "trade",
            "s": "BTCUSDT",
            "p": "not_a_number",
            "T": 1704067200000,
        })
        ws._process_message(msg)
        assert buf.get_price("BTCUSDT") is None

    def test_none_input(self) -> None:
        """None input does not crash (TypeError caught by json.loads)."""
        ws, buf = _make_ws()
        ws._process_message(None)
        assert buf.get_price("BTCUSDT") is None

    def test_combined_stream_with_non_trade_data(self) -> None:
        """Combined stream with non-trade event in data is ignored."""
        ws, buf = _make_ws()
        msg = json.dumps({
            "stream": "btcusdt@kline_1m",
            "data": {
                "e": "kline",
                "s": "BTCUSDT",
            },
        })
        ws._process_message(msg)
        assert buf.get_price("BTCUSDT") is None


# ---------------------------------------------------------------------------
# stop()
# ---------------------------------------------------------------------------


class TestStop:
    """Tests for stop()."""

    def test_stop_sets_running_to_false(self) -> None:
        """stop() sets _running to False."""
        ws, _ = _make_ws()
        ws._running = True
        ws.stop()
        assert ws._running is False

    def test_stop_idempotent(self) -> None:
        """Calling stop() multiple times does not raise."""
        ws, _ = _make_ws()
        ws.stop()
        ws.stop()
        assert ws._running is False


# ---------------------------------------------------------------------------
# Symbol normalization
# ---------------------------------------------------------------------------


class TestSymbolNormalization:
    """Tests for symbol normalization in constructor."""

    def test_symbols_uppercased(self) -> None:
        """Symbols are stored uppercase internally."""
        ws, _ = _make_ws(symbols=["btcusdt", "ethusdt"])
        assert ws._symbols == ["BTCUSDT", "ETHUSDT"]

    def test_mixed_case_symbols(self) -> None:
        """Mixed case symbols are uppercased."""
        ws, _ = _make_ws(symbols=["BtcUsdt"])
        assert ws._symbols == ["BTCUSDT"]


# ---------------------------------------------------------------------------
# Update counter
# ---------------------------------------------------------------------------


class TestUpdateCounter:
    """Tests for periodic logging counter."""

    def test_update_count_increments(self) -> None:
        """_update_count increments with each processed trade."""
        ws, buf = _make_ws(symbols=["BTCUSDT"])
        assert ws._update_count == 0
        now_ms = _now_ms()
        msg = json.dumps({
            "e": "trade",
            "s": "BTCUSDT",
            "p": "43000.0",
            "T": now_ms,
        })
        ws._process_message(msg)
        assert ws._update_count == 1

    def test_update_count_resets_at_interval(self) -> None:
        """_update_count resets to 0 after reaching the log interval."""
        ws, buf = _make_ws(symbols=["BTCUSDT"])
        ws._update_log_interval = 3  # small interval for testing
        now_ms = _now_ms()
        for i in range(3):
            msg = json.dumps({
                "e": "trade",
                "s": "BTCUSDT",
                "p": str(43000.0 + i),
                "T": now_ms + i * 1000,
            })
            ws._process_message(msg)
        assert ws._update_count == 0

    def test_invalid_messages_dont_increment_counter(self) -> None:
        """Invalid messages should not increment the update counter."""
        ws, _ = _make_ws()
        ws._process_message("invalid json")
        assert ws._update_count == 0
