"""Time utilities for multi-interval market windows.

Polymarket crypto Up/Down markets operate on fixed windows (5m, 15m, 1h)
aligned to the Unix epoch.  This module provides helpers for computing
window boundaries, slugs, dead zones, and remaining time.
"""

from __future__ import annotations

import time

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

INTERVAL_SECONDS: dict[str, int] = {"5m": 300, "15m": 900, "1h": 3600}

WINDOW_SECONDS = 900  # 15 minutes — kept for backward compatibility

# ---------------------------------------------------------------------------
# Interval-aware alignment
# ---------------------------------------------------------------------------


def align_to_interval(unix_ts: float, interval: str = "15m") -> int:
    """Round *unix_ts* down to the nearest boundary for *interval*."""
    secs = INTERVAL_SECONDS[interval]
    return int(unix_ts // secs) * secs


def align_to_window(unix_ts: float) -> int:
    """Round *unix_ts* down to the nearest 15-minute boundary."""
    return align_to_interval(unix_ts, "15m")


def compute_slug(asset: str, unix_ts: float, interval: str = "15m") -> str:
    """Return a market slug like ``btc-updown-{interval}-{aligned_ts}``.

    The *asset* is lowercased automatically.
    """
    aligned = align_to_interval(unix_ts, interval)
    return f"{asset.lower()}-updown-{interval}-{aligned}"


def window_seconds_for_interval(interval: str) -> int:
    """Return the window duration in seconds for the given *interval*."""
    return INTERVAL_SECONDS[interval]


# ---------------------------------------------------------------------------
# Time queries
# ---------------------------------------------------------------------------


def time_remaining_seconds(market_end_ts: float) -> float:
    """Return seconds until *market_end_ts* from the current time.

    Can be negative if the market has already ended.
    """
    return market_end_ts - time.time()


def is_in_dead_zone(
    market_start_ts: float,
    market_end_ts: float,
    start_buffer: float = 60.0,
    end_buffer: float = 30.0,
) -> bool:
    """Return ``True`` if the current time falls within a dead zone.

    A dead zone is defined as:
      - Within *start_buffer* seconds **after** market start, **or**
      - Within *end_buffer* seconds **before** market end.

    Trading during dead zones is risky because of thin liquidity and
    rapid price changes.
    """
    now = time.time()
    near_start = now < market_start_ts + start_buffer
    near_end = now > market_end_ts - end_buffer
    return near_start or near_end


# ---------------------------------------------------------------------------
# Window helpers
# ---------------------------------------------------------------------------


def current_window_timestamps(interval: str = "15m") -> tuple[int, int]:
    """Return ``(start_ts, end_ts)`` of the current window for *interval*."""
    secs = INTERVAL_SECONDS[interval]
    start = align_to_interval(time.time(), interval)
    return start, start + secs


def next_window_timestamps(interval: str = "15m") -> tuple[int, int]:
    """Return ``(start_ts, end_ts)`` of the *next* window for *interval*."""
    secs = INTERVAL_SECONDS[interval]
    current_start = align_to_interval(time.time(), interval)
    next_start = current_start + secs
    return next_start, next_start + secs
