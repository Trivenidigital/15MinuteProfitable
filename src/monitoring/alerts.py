"""Telegram and Discord alert dispatcher for the Polymarket trading bot.

Sends notifications about trades, errors, and daily summaries to configured
channels. Uses httpx.AsyncClient for non-blocking HTTP requests.
"""

from __future__ import annotations

import asyncio
from enum import Enum

import httpx

from src.monitoring.logger import get_logger

logger = get_logger(__name__)

_HTTP_TIMEOUT = 10.0


class AlertLevel(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class TelegramSink:
    """Send messages to a Telegram chat via the Bot API."""

    def __init__(self, bot_token: str, chat_id: str) -> None:
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self._chat_id = chat_id
        self._client = httpx.AsyncClient(timeout=_HTTP_TIMEOUT)

    async def send(self, text: str, level: AlertLevel = AlertLevel.INFO) -> bool:
        """Send a message. Returns True on success."""
        prefix = f"[{level.value}] " if level != AlertLevel.INFO else ""
        payload = {
            "chat_id": self._chat_id,
            "text": f"{prefix}{text}",
            "parse_mode": "HTML",
        }
        try:
            resp = await self._client.post(self._url, json=payload)
            if resp.status_code == 200:
                return True
            logger.warning(
                "telegram_send_failed",
                status=resp.status_code,
                body=resp.text[:200],
            )
            return False
        except httpx.HTTPError as exc:
            logger.error("telegram_error", error=str(exc))
            return False

    async def close(self) -> None:
        await self._client.aclose()


class DiscordSink:
    """Send messages to a Discord channel via webhook."""

    def __init__(self, webhook_url: str) -> None:
        self._url = webhook_url
        self._client = httpx.AsyncClient(timeout=_HTTP_TIMEOUT)

    async def send(self, text: str, level: AlertLevel = AlertLevel.INFO) -> bool:
        """Send a message via Discord webhook embed. Returns True on success."""
        color_map = {
            AlertLevel.INFO: 3447003,
            AlertLevel.WARNING: 16776960,
            AlertLevel.ERROR: 15158332,
        }
        payload = {
            "embeds": [{
                "title": f"BTC15MinuteBot - {level.value}",
                "description": text,
                "color": color_map.get(level, 3447003),
            }],
        }
        try:
            resp = await self._client.post(self._url, json=payload)
            if resp.status_code in (200, 204):
                return True
            logger.warning(
                "discord_send_failed",
                status=resp.status_code,
                body=resp.text[:200],
            )
            return False
        except httpx.HTTPError as exc:
            logger.error("discord_error", error=str(exc))
            return False

    async def close(self) -> None:
        await self._client.aclose()


class AlertDispatcher:
    """Routes alerts to all configured sinks.

    Only sinks with valid configuration are added. If no sinks are
    configured, alerts are silently dropped (logged at debug level).
    """

    def __init__(self) -> None:
        self._sinks: list[TelegramSink | DiscordSink] = []

    def add_sink(self, sink: TelegramSink | DiscordSink) -> None:
        self._sinks.append(sink)

    @property
    def sink_count(self) -> int:
        return len(self._sinks)

    @classmethod
    def from_settings(cls, settings: object) -> AlertDispatcher:
        """Build an AlertDispatcher from a Settings-like object.

        Adds sinks only when their config fields are non-empty.
        """
        dispatcher = cls()

        tg_token = getattr(settings, "telegram_bot_token", "")
        tg_chat = getattr(settings, "telegram_chat_id", "")
        if tg_token and tg_chat:
            dispatcher.add_sink(TelegramSink(tg_token, tg_chat))
            logger.info("alert_sink_added", sink="telegram")

        discord_url = getattr(settings, "discord_webhook_url", "")
        if discord_url:
            dispatcher.add_sink(DiscordSink(discord_url))
            logger.info("alert_sink_added", sink="discord")

        if not dispatcher._sinks:
            logger.info("no_alert_sinks_configured")

        return dispatcher

    async def send(self, text: str, level: AlertLevel = AlertLevel.INFO) -> None:
        """Send a message to all configured sinks concurrently."""
        if not self._sinks:
            logger.debug("alert_no_sinks", text=text[:50])
            return

        results = await asyncio.gather(
            *(sink.send(text, level) for sink in self._sinks),
            return_exceptions=True,
        )
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(
                    "alert_sink_error",
                    sink_index=i,
                    error=str(result),
                )

    async def send_trade(self, summary: str) -> None:
        """Convenience: send a trade notification at INFO level."""
        await self.send(summary, AlertLevel.INFO)

    async def send_error(self, error_msg: str) -> None:
        """Convenience: send an error notification at ERROR level."""
        await self.send(error_msg, AlertLevel.ERROR)

    async def send_daily_summary(self, summary: str) -> None:
        """Convenience: send a daily summary at INFO level."""
        await self.send(f"Daily Summary\n{summary}", AlertLevel.INFO)

    async def close(self) -> None:
        """Close all sink HTTP clients."""
        for sink in self._sinks:
            await sink.close()
