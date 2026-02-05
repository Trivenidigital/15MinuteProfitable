"""Fee calculation utilities for Polymarket trading.

Polymarket charges three types of fees:
  - Taker fee: charged on market orders, varies with price (max at 50/50 odds).
  - Winner fee: 2% on profits when a market resolves in your favor.
  - Maker fee: 0% for limit orders.

Reference: https://docs.polymarket.com/#fees
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TAKER_FEE_MAX_RATE = 0.0315  # 3.15% at 50/50 odds
WINNER_FEE_RATE = 0.02       # 2% on profits at resolution
MAKER_FEE_RATE = 0.0         # 0% for limit orders

# ---------------------------------------------------------------------------
# Fee helpers
# ---------------------------------------------------------------------------


def taker_fee_rate(price: float) -> float:
    """Return the taker fee *rate* (not dollar amount) for a given price.

    The rate follows a parabolic curve that peaks at ``price = 0.50``
    (equal odds) where it equals ``TAKER_FEE_MAX_RATE`` and approaches
    zero near the extremes (0 or 1).

    Formula:  ``TAKER_FEE_MAX_RATE * 4.0 * price * (1.0 - price)``

    The *price* is clamped to [0.01, 0.99] before calculation.
    """
    price = max(0.01, min(0.99, price))
    return TAKER_FEE_MAX_RATE * 4.0 * price * (1.0 - price)


def taker_fee_amount(price: float, size: float) -> float:
    """Return the absolute taker fee in USD for buying *size* shares at *price*.

    ``fee = taker_fee_rate(price) * price * size``
    """
    return taker_fee_rate(price) * price * size


def winner_fee_amount(cost_basis: float, payout: float) -> float:
    """Return the winner fee charged at resolution.

    The fee is ``WINNER_FEE_RATE`` applied to net profit only.  If the
    position is not profitable (payout <= cost_basis), no fee is charged.
    """
    return WINNER_FEE_RATE * max(0.0, payout - cost_basis)


def net_arb_profit(
    yes_price: float,
    no_price: float,
    size: float,
) -> float:
    """Return the net profit for a YES + NO arbitrage after *all* fees.

    Strategy: buy *size* YES shares at ``yes_price`` and *size* NO shares
    at ``no_price``.  Regardless of outcome, one side pays out $1/share.

    Deductions:
      1. Taker fees on both legs.
      2. Winner fee on the profitable (cheaper) leg at resolution.
    """
    gross = (1.0 - yes_price - no_price) * size

    taker_yes = taker_fee_amount(yes_price, size)
    taker_no = taker_fee_amount(no_price, size)

    # The winning side is whichever was bought cheaper (higher profit margin).
    winner_cost = winner_fee_amount(min(yes_price, no_price), 1.0) * size

    return gross - taker_yes - taker_no - winner_cost


def min_combined_cost_for_profit(
    target_profit_per_share: float = 0.0,
) -> float:
    """Find the maximum combined YES + NO cost (at 50/50) that still yields
    at least ``target_profit_per_share`` per share of net arb profit.

    Uses binary search over the combined cost in [0.0, 1.0].  The result
    is rounded to 4 decimal places.

    At 50/50 the YES and NO prices are each ``combined / 2``.
    """
    lo = 0.0
    hi = 1.0

    for _ in range(200):  # plenty of iterations for <1e-10 precision
        mid = (lo + hi) / 2.0
        half = mid / 2.0
        profit = net_arb_profit(half, half, 1.0)
        if profit >= target_profit_per_share:
            lo = mid  # can afford a higher combined cost
        else:
            hi = mid

    return round(lo, 4)
