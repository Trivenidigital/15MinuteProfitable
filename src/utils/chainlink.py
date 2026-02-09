"""Lightweight Chainlink price oracle reader for Polygon.

Reads `latestRoundData()` from Chainlink aggregator contracts using raw
JSON-RPC ``eth_call`` — no web3.py dependency required. Uses aiohttp
(already a project dependency) for async HTTP.

Prices are cached for a configurable TTL to avoid hammering the RPC.
"""

from __future__ import annotations

import asyncio
import struct
import time

import aiohttp
import structlog

logger = structlog.get_logger("chainlink")

# Polygon Mainnet Chainlink Price Feed addresses
# See: https://docs.chain.link/data-feeds/price-feeds/addresses?network=polygon
CHAINLINK_FEEDS: dict[str, str] = {
    "BTC": "0xc907E116054Ad103354f2D350FD2514433D57F6f",   # BTC/USD
    "ETH": "0xF9680D99D6C9589e2a93a78A04A279e509205945",   # ETH/USD
    # SOL/USD: no Chainlink feed on Polygon mainnet — filter is permissive (passes)
    "XRP": "0x785ba89291f676b5386652eB12b30CF361020694",   # XRP/USD
}

# `latestRoundData()` function selector (keccak256 first 4 bytes)
LATEST_ROUND_DATA_SELECTOR = "0xfeaf968c"

# Default Polygon RPC (free, public — used only for price reads)
DEFAULT_RPC_URL = "https://polygon-rpc.com"

# Cache: asset -> (timestamp, price_usd)
_price_cache: dict[str, tuple[float, float]] = {}
_CACHE_TTL = 10.0  # seconds


def _decode_latest_round_data(hex_data: str) -> float | None:
    """Decode the ABI-encoded return of latestRoundData().

    Returns: (roundId, answer, startedAt, updatedAt, answeredInRound)
    We only need `answer` (index 1) which is the price in 8-decimal format.
    """
    data = bytes.fromhex(hex_data.removeprefix("0x"))
    if len(data) < 160:  # 5 * 32 bytes
        return None

    # answer is at offset 32 (second slot), int256
    answer_bytes = data[32:64]
    # Unpack as signed 256-bit integer (big-endian)
    answer = int.from_bytes(answer_bytes, byteorder="big", signed=True)

    if answer <= 0:
        return None

    # Chainlink BTC/USD, ETH/USD feeds use 8 decimals
    return answer / 1e8


async def get_chainlink_price(
    asset: str,
    rpc_url: str = DEFAULT_RPC_URL,
    session: aiohttp.ClientSession | None = None,
) -> float | None:
    """Fetch the latest Chainlink oracle price for an asset.

    Returns the USD price or None if unavailable. Results are cached
    for ``_CACHE_TTL`` seconds.
    """
    # Check cache
    cached = _price_cache.get(asset)
    if cached is not None:
        ts, price = cached
        if time.time() - ts < _CACHE_TTL:
            return price

    feed_address = CHAINLINK_FEEDS.get(asset)
    if feed_address is None:
        return None

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [
            {
                "to": feed_address,
                "data": LATEST_ROUND_DATA_SELECTOR,
            },
            "latest",
        ],
    }

    owns_session = session is None
    if owns_session:
        session = aiohttp.ClientSession()

    try:
        async with session.post(
            rpc_url,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=3.0),
        ) as resp:
            if resp.status != 200:
                logger.warning("chainlink_rpc_error", status=resp.status, asset=asset)
                return None
            result = await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        logger.warning("chainlink_rpc_timeout", asset=asset, error=str(exc))
        return None
    finally:
        if owns_session:
            await session.close()

    hex_data = result.get("result")
    if not hex_data or hex_data == "0x":
        logger.warning("chainlink_empty_result", asset=asset)
        return None

    price = _decode_latest_round_data(hex_data)
    if price is not None:
        _price_cache[asset] = (time.time(), price)

    return price


async def validate_spot_price(
    asset: str,
    binance_price: float,
    max_divergence_pct: float = 0.003,
    rpc_url: str = DEFAULT_RPC_URL,
    session: aiohttp.ClientSession | None = None,
) -> tuple[bool, float | None, float | None]:
    """Validate Binance spot price against Chainlink oracle.

    Returns:
        (is_valid, chainlink_price, divergence_pct)
        - is_valid: True if prices agree within tolerance
        - chainlink_price: Oracle price (or None if unavailable)
        - divergence_pct: Absolute percentage divergence (or None)

    If the oracle is unavailable, returns (True, None, None) — we don't
    block trades when the oracle is down.
    """
    oracle_price = await get_chainlink_price(asset, rpc_url, session)

    if oracle_price is None:
        return True, None, None

    if oracle_price <= 0 or binance_price <= 0:
        return True, oracle_price, None

    divergence = abs(binance_price - oracle_price) / oracle_price

    is_valid = divergence <= max_divergence_pct

    if not is_valid:
        logger.info(
            "chainlink_divergence_rejected",
            asset=asset,
            binance_price=round(binance_price, 2),
            chainlink_price=round(oracle_price, 2),
            divergence_pct=round(divergence * 100, 4),
            max_allowed_pct=round(max_divergence_pct * 100, 4),
        )

    return is_valid, oracle_price, divergence
