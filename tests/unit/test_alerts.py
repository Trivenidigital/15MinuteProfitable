"""Tests for src.monitoring.alerts — Telegram, Discord, AlertDispatcher."""

from __future__ import annotations

import os

os.environ.setdefault("BOT_PRIVATE_KEY", "0x" + "ab" * 32)

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from src.monitoring.alerts import (
    AlertDispatcher,
    AlertLevel,
    DiscordSink,
    TelegramSink,
)


# ---------------------------------------------------------------------------
# TelegramSink
# ---------------------------------------------------------------------------


class TestTelegramSink:
    @pytest.mark.asyncio
    async def test_send_success(self) -> None:
        sink = TelegramSink("fake_token", "123")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        sink._client = AsyncMock()
        sink._client.post = AsyncMock(return_value=mock_resp)

        result = await sink.send("test message")
        assert result is True
        sink._client.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_failure_status(self) -> None:
        sink = TelegramSink("fake_token", "123")
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = "Bad Request"
        sink._client = AsyncMock()
        sink._client.post = AsyncMock(return_value=mock_resp)

        result = await sink.send("test message")
        assert result is False

    @pytest.mark.asyncio
    async def test_send_http_error(self) -> None:
        sink = TelegramSink("fake_token", "123")
        sink._client = AsyncMock()
        sink._client.post = AsyncMock(side_effect=httpx.ConnectError("fail"))

        result = await sink.send("test message")
        assert result is False

    @pytest.mark.asyncio
    async def test_send_with_level_prefix(self) -> None:
        sink = TelegramSink("fake_token", "123")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        sink._client = AsyncMock()
        sink._client.post = AsyncMock(return_value=mock_resp)

        await sink.send("alert!", AlertLevel.ERROR)
        call_args = sink._client.post.call_args
        payload = call_args[1]["json"] if "json" in call_args[1] else call_args[0][1]
        assert "[ERROR]" in payload["text"]

    @pytest.mark.asyncio
    async def test_info_no_prefix(self) -> None:
        sink = TelegramSink("fake_token", "123")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        sink._client = AsyncMock()
        sink._client.post = AsyncMock(return_value=mock_resp)

        await sink.send("hello", AlertLevel.INFO)
        call_args = sink._client.post.call_args
        payload = call_args[1]["json"] if "json" in call_args[1] else call_args[0][1]
        assert payload["text"] == "hello"

    @pytest.mark.asyncio
    async def test_close(self) -> None:
        sink = TelegramSink("fake_token", "123")
        sink._client = AsyncMock()
        await sink.close()
        sink._client.aclose.assert_called_once()


# ---------------------------------------------------------------------------
# DiscordSink
# ---------------------------------------------------------------------------


class TestDiscordSink:
    @pytest.mark.asyncio
    async def test_send_success_200(self) -> None:
        sink = DiscordSink("https://discord.com/api/webhooks/fake")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        sink._client = AsyncMock()
        sink._client.post = AsyncMock(return_value=mock_resp)

        result = await sink.send("test")
        assert result is True

    @pytest.mark.asyncio
    async def test_send_success_204(self) -> None:
        sink = DiscordSink("https://discord.com/api/webhooks/fake")
        mock_resp = MagicMock()
        mock_resp.status_code = 204
        sink._client = AsyncMock()
        sink._client.post = AsyncMock(return_value=mock_resp)

        result = await sink.send("test")
        assert result is True

    @pytest.mark.asyncio
    async def test_send_failure(self) -> None:
        sink = DiscordSink("https://discord.com/api/webhooks/fake")
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_resp.text = "Rate limited"
        sink._client = AsyncMock()
        sink._client.post = AsyncMock(return_value=mock_resp)

        result = await sink.send("test")
        assert result is False

    @pytest.mark.asyncio
    async def test_send_http_error(self) -> None:
        sink = DiscordSink("https://discord.com/api/webhooks/fake")
        sink._client = AsyncMock()
        sink._client.post = AsyncMock(side_effect=httpx.ConnectError("fail"))

        result = await sink.send("test")
        assert result is False

    @pytest.mark.asyncio
    async def test_embed_color_varies_by_level(self) -> None:
        sink = DiscordSink("https://discord.com/api/webhooks/fake")
        mock_resp = MagicMock()
        mock_resp.status_code = 204
        sink._client = AsyncMock()
        sink._client.post = AsyncMock(return_value=mock_resp)

        await sink.send("test", AlertLevel.ERROR)
        call_args = sink._client.post.call_args
        payload = call_args[1]["json"] if "json" in call_args[1] else call_args[0][1]
        assert payload["embeds"][0]["color"] == 15158332  # red

    @pytest.mark.asyncio
    async def test_close(self) -> None:
        sink = DiscordSink("https://discord.com/api/webhooks/fake")
        sink._client = AsyncMock()
        await sink.close()
        sink._client.aclose.assert_called_once()


# ---------------------------------------------------------------------------
# AlertDispatcher
# ---------------------------------------------------------------------------


class TestAlertDispatcher:
    @pytest.mark.asyncio
    async def test_send_no_sinks(self) -> None:
        dispatcher = AlertDispatcher()
        await dispatcher.send("test")

    @pytest.mark.asyncio
    async def test_send_routes_to_all_sinks(self) -> None:
        dispatcher = AlertDispatcher()
        sink1 = AsyncMock()
        sink1.send = AsyncMock(return_value=True)
        sink2 = AsyncMock()
        sink2.send = AsyncMock(return_value=True)
        dispatcher.add_sink(sink1)
        dispatcher.add_sink(sink2)

        await dispatcher.send("hello", AlertLevel.WARNING)
        sink1.send.assert_called_once_with("hello", AlertLevel.WARNING)
        sink2.send.assert_called_once_with("hello", AlertLevel.WARNING)

    def test_sink_count(self) -> None:
        dispatcher = AlertDispatcher()
        assert dispatcher.sink_count == 0
        dispatcher.add_sink(AsyncMock())
        assert dispatcher.sink_count == 1

    def test_from_settings_telegram_only(self) -> None:
        settings = MagicMock()
        settings.telegram_bot_token = "tok123"
        settings.telegram_chat_id = "456"
        settings.discord_webhook_url = ""

        dispatcher = AlertDispatcher.from_settings(settings)
        assert dispatcher.sink_count == 1

    def test_from_settings_discord_only(self) -> None:
        settings = MagicMock()
        settings.telegram_bot_token = ""
        settings.telegram_chat_id = ""
        settings.discord_webhook_url = "https://discord.com/webhook/fake"

        dispatcher = AlertDispatcher.from_settings(settings)
        assert dispatcher.sink_count == 1

    def test_from_settings_both(self) -> None:
        settings = MagicMock()
        settings.telegram_bot_token = "tok"
        settings.telegram_chat_id = "123"
        settings.discord_webhook_url = "https://discord.com/webhook/fake"

        dispatcher = AlertDispatcher.from_settings(settings)
        assert dispatcher.sink_count == 2

    def test_from_settings_none(self) -> None:
        settings = MagicMock()
        settings.telegram_bot_token = ""
        settings.telegram_chat_id = ""
        settings.discord_webhook_url = ""

        dispatcher = AlertDispatcher.from_settings(settings)
        assert dispatcher.sink_count == 0

    @pytest.mark.asyncio
    async def test_send_trade(self) -> None:
        dispatcher = AlertDispatcher()
        sink = AsyncMock()
        sink.send = AsyncMock(return_value=True)
        dispatcher.add_sink(sink)

        await dispatcher.send_trade("BTC trade filled")
        sink.send.assert_called_once_with("BTC trade filled", AlertLevel.INFO)

    @pytest.mark.asyncio
    async def test_send_error(self) -> None:
        dispatcher = AlertDispatcher()
        sink = AsyncMock()
        sink.send = AsyncMock(return_value=True)
        dispatcher.add_sink(sink)

        await dispatcher.send_error("Connection lost")
        sink.send.assert_called_once_with("Connection lost", AlertLevel.ERROR)

    @pytest.mark.asyncio
    async def test_close_all_sinks(self) -> None:
        dispatcher = AlertDispatcher()
        sink1 = TelegramSink("tok", "123")
        sink1._client = AsyncMock()
        sink2 = DiscordSink("https://example.com")
        sink2._client = AsyncMock()
        dispatcher.add_sink(sink1)
        dispatcher.add_sink(sink2)

        await dispatcher.close()
        sink1._client.aclose.assert_called_once()
        sink2._client.aclose.assert_called_once()

    @pytest.mark.asyncio
    async def test_sink_exception_handled(self) -> None:
        dispatcher = AlertDispatcher()
        sink = AsyncMock()
        sink.send = AsyncMock(side_effect=RuntimeError("boom"))
        dispatcher.add_sink(sink)

        # Should not raise
        await dispatcher.send("test")
