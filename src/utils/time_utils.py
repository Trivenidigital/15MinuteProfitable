"""Time utilities for 15-minute market windows.

Every BTC Up/Down market on Polymarket operates on fixed 15-minute
(900-second) windows aligned to the Unix epoch.  This module provides
helpers for computing window boundaries, slugs, dead zones, and
remaining time.
"""

from __future__ import annotations

import time

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WINDOW_SECONDS = 900  # 15 minutes

# ---------------------------------------------------------------------------
# Window alignment
# ---------------------------------------------------------------------------


def align_to_window(unix_ts: float) -> int:
    """Round *unix_ts* down to the nearest 15-minute boundary."""
    return int(unix_ts // WINDOW_SECONDS) * WINDOW_SECONDS


def compute_slug(asset: str, unix_ts: float) -> str:
    """Return a market slug like ``btc-updown-15m-{aligned_ts}``.

    The *asset* is lowercased automatically.
    """
    aligned = align_to_window(unix_ts)
    return f"{asset.lower()}-updown-15m-{aligned}"


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


def current_window_timestamps() -> tuple[int, int]:
    """Return ``(start_ts, end_ts)`` of the current 15-minute window."""
    start = align_to_window(time.time())
    return start, start + WINDOW_SECONDS


def next_window_timestamps() -> tuple[int, int]:
    """Return ``(start_ts, end_ts)`` of the *next* 15-minute window."""
    current_start = align_to_window(time.time())
    next_start = current_start + WINDOW_SECONDS
    return next_start, next_start + WINDOW_SECONDS
