"""Discover active 15-minute crypto markets on Polymarket.

Uses a 3-tier fallback strategy:
1. Computed slugs (derived from the current timestamp)
2. Gamma API query (search for open markets)
3. Page scrape (last resort -- not yet implemented)
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timezone

import httpx

from src.core.models import Market
from src.monitoring.logger import get_logger
from src.utils.time_utils import WINDOW_SECONDS, align_to_window, compute_slug

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_USER_AGENT = "BTC15MinuteBot/1.0"
_HTTP_TIMEOUT = 15.0
_SLUG_LOOKAHEAD = 7  # current window + next 6


# ---------------------------------------------------------------------------
# MarketDiscovery
# ---------------------------------------------------------------------------


class MarketDiscovery:
    """Locate active 15-minute Up/Down markets via the Polymarket Gamma API."""

    def __init__(
        self,
        gamma_api_url: str = "https://gamma-api.polymarket.com",
    ) -> None:
        self._gamma_api_url = gamma_api_url
        self._log = get_logger("market_discovery")

    # -- public API ---------------------------------------------------------

    async def find_active_markets(
        self,
        assets: list[str] | None = None,
    ) -> list[Market]:
        """Find all active 15-minute markets for the given assets.

        Defaults to ``["BTC"]`` if *assets* is ``None``.  Each asset is
        queried concurrently; results are collected into a flat list.

        Returns:
            A list of :class:`Market` objects for every asset with an
            active market.  May be empty if no markets are found.
        """
        if assets is None:
            assets = ["BTC"]

        results = await asyncio.gather(
            *(self.find_market_for_asset(asset) for asset in assets),
        )

        return [market for market in results if market is not None]

    async def find_market_for_asset(self, asset: str) -> Market | None:
        """Find the current active market for a single *asset*.

        Tries computed slugs first (fast, deterministic) and falls back
        to a broader Gamma API search if slug lookup fails.
        """
        market = await self._try_computed_slugs(asset)
        if market is not None:
            return market

        self._log.info(
            "computed_slug_miss",
            asset=asset,
            msg="Falling back to Gamma API search",
        )
        return await self._try_gamma_api(asset)

    # -- tier 1: computed slugs ---------------------------------------------

    async def _try_computed_slugs(self, asset: str) -> Market | None:
        """Try up to ``_SLUG_LOOKAHEAD`` computed slugs.

        Starts from the current 15-minute window and checks successive
        future windows.  Returns the first market whose window is still
        open (i.e. ``end_time`` is in the future).
        """
        now = time.time()
        base_ts = align_to_window(now)

        for i in range(_SLUG_LOOKAHEAD):
            window_ts = base_ts + i * WINDOW_SECONDS
            slug = compute_slug(asset, window_ts)
            market = await self._fetch_market_by_slug(slug)
            if market is not None and market.end_time.timestamp() > now:
                self._log.debug(
                    "computed_slug_hit",
                    slug=slug,
                    asset=asset,
                )
                return market

        return None

    # -- tier 2: gamma API search -------------------------------------------

    async def _try_gamma_api(self, asset: str) -> Market | None:
        """Query the Gamma API for open markets matching *asset*.

        Fetches up to 100 non-closed markets and filters them by a slug
        pattern like ``btc-updown-15m-<timestamp>``.  Returns the market
        with the latest start timestamp that is still open.
        """
        url = f"{self._gamma_api_url}/markets"
        params = {"closed": "false", "limit": "100"}
        pattern = re.compile(rf"^{asset.lower()}-updown-15m-(\d+)$")

        try:
            async with httpx.AsyncClient(
                timeout=_HTTP_TIMEOUT,
                headers={"User-Agent": _USER_AGENT},
            ) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
        except (httpx.HTTPError, Exception) as exc:
            self._log.warning(
                "gamma_api_search_failed",
                asset=asset,
                error=str(exc),
            )
            return None

        if not isinstance(data, list):
            self._log.warning("gamma_api_unexpected_response", asset=asset)
            return None

        now = time.time()
        best: Market | None = None
        best_ts: int = 0

        for item in data:
            slug = item.get("slug", "")
            match = pattern.match(slug)
            if match is None:
                continue

            market = self._parse_gamma_market(item)
            if market is None:
                continue

            if market.end_time.timestamp() <= now:
                continue  # already expired

            ts = int(match.group(1))
            if ts > best_ts:
                best_ts = ts
                best = market

        if best is not None:
            self._log.debug(
                "gamma_api_hit",
                slug=best.slug,
                asset=asset,
            )

        return best

    # -- helpers ------------------------------------------------------------

    async def _fetch_market_by_slug(self, slug: str) -> Market | None:
        """Fetch a single market from the Gamma API by its *slug*.

        Returns ``None`` when the slug is not found or on any HTTP error.
        """
        url = f"{self._gamma_api_url}/markets"
        params = {"slug": slug}

        try:
            async with httpx.AsyncClient(
                timeout=_HTTP_TIMEOUT,
                headers={"User-Agent": _USER_AGENT},
            ) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
        except (httpx.HTTPError, Exception) as exc:
            self._log.warning(
                "fetch_market_by_slug_failed",
                slug=slug,
                error=str(exc),
            )
            return None

        # The API may return a list or a single object.
        if isinstance(data, list):
            if len(data) == 0:
                return None
            data = data[0]

        if not isinstance(data, dict):
            return None

        return self._parse_gamma_market(data)

    def _parse_gamma_market(self, data: dict) -> Market | None:
        """Parse a Gamma API market dict into a :class:`Market`.

        Returns ``None`` if any required field is missing or cannot be
        parsed.
        """
        try:
            # API uses camelCase, handle both for compatibility
            condition_id = data.get("conditionId") or data.get("condition_id")
            if not condition_id:
                raise KeyError("conditionId")
            slug = data["slug"]
            question = data.get("question", "")

            # clobTokenIds is stored as a JSON-encoded string.
            clob_raw = data["clobTokenIds"]
            if isinstance(clob_raw, str):
                clob_tokens = json.loads(clob_raw)
            else:
                clob_tokens = clob_raw

            if not isinstance(clob_tokens, list) or len(clob_tokens) < 2:
                self._log.warning(
                    "parse_market_invalid_tokens",
                    slug=slug,
                )
                return None

            yes_token_id = clob_tokens[0]
            no_token_id = clob_tokens[1]

            start_time = datetime.fromisoformat(data["startDate"]).replace(
                tzinfo=timezone.utc,
            ) if data["startDate"][-1] != "Z" else datetime.fromisoformat(
                data["startDate"].replace("Z", "+00:00"),
            )

            end_time = datetime.fromisoformat(data["endDate"]).replace(
                tzinfo=timezone.utc,
            ) if data["endDate"][-1] != "Z" else datetime.fromisoformat(
                data["endDate"].replace("Z", "+00:00"),
            )

            # Determine asset from the slug (e.g. "btc-updown-15m-..." -> "BTC")
            asset = slug.split("-")[0].upper() if "-" in slug else "BTC"

            return Market(
                condition_id=condition_id,
                slug=slug,
                question=question,
                yes_token_id=yes_token_id,
                no_token_id=no_token_id,
                start_time=start_time,
                end_time=end_time,
                asset=asset,
            )

        except (KeyError, ValueError, IndexError, TypeError) as exc:
            self._log.warning(
                "parse_gamma_market_failed",
                error=str(exc),
                slug=data.get("slug", "unknown"),
            )
            return None
