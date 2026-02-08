"""Information-theoretic divergence utilities for Polymarket trading.

Provides Bregman / KL divergence functions for:
- Binary mispricing scoring
- Divergence-scaled position sizing
- Cross-asset correlation projection
- Divergence-based exit signals

All functions are pure math — no side effects, no I/O.
"""

from __future__ import annotations

import math

# ---------------------------------------------------------------------------
# KL divergence for binary distributions
# ---------------------------------------------------------------------------

_EPS = 1e-12  # clamp to avoid log(0)


def kl_divergence_binary(p: float, q: float) -> float:
    """Compute D_KL(p || q) for two Bernoulli distributions.

    D_KL = p * ln(p/q) + (1-p) * ln((1-p)/(1-q))

    Args:
        p: "true" probability (e.g. fair value)
        q: "model" probability (e.g. market-implied)

    Returns:
        Non-negative KL divergence in nats.
    """
    p = max(_EPS, min(1.0 - _EPS, p))
    q = max(_EPS, min(1.0 - _EPS, q))
    return p * math.log(p / q) + (1.0 - p) * math.log((1.0 - p) / (1.0 - q))


# ---------------------------------------------------------------------------
# Simplex projection
# ---------------------------------------------------------------------------


def simplex_project(yes_ask: float, no_ask: float) -> float:
    """Normalize YES/NO asks onto the probability simplex.

    Returns the implied fair YES probability: yes_ask / (yes_ask + no_ask).
    When the market has overround (yes+no > 1), this strips it out.

    Args:
        yes_ask: Best ask price for YES token.
        no_ask: Best ask price for NO token.

    Returns:
        Fair YES probability in (0, 1).
    """
    total = yes_ask + no_ask
    if total <= 0:
        return 0.5
    return yes_ask / total


# ---------------------------------------------------------------------------
# Composite mispricing score
# ---------------------------------------------------------------------------


def market_mispricing_score(yes_ask: float, no_ask: float) -> dict[str, float]:
    """Compute information-theoretic mispricing metrics for a binary market.

    Returns a dict with:
        p_fair: simplex-projected fair YES probability
        kl_divergence: D_KL(fair || market_yes)
        kl_reverse: D_KL(market_yes || fair)
        jensen_shannon: symmetric JS divergence
        overround: yes_ask + no_ask - 1.0

    Args:
        yes_ask: Best ask price for YES token.
        no_ask: Best ask price for NO token.

    Returns:
        Dict of mispricing metrics.
    """
    p_fair = simplex_project(yes_ask, no_ask)
    q_market = max(_EPS, min(1.0 - _EPS, yes_ask))

    kl_fwd = kl_divergence_binary(p_fair, q_market)
    kl_rev = kl_divergence_binary(q_market, p_fair)

    # Jensen-Shannon: symmetric, bounded [0, ln(2)]
    m = 0.5 * (p_fair + q_market)
    js = 0.5 * kl_divergence_binary(p_fair, m) + 0.5 * kl_divergence_binary(q_market, m)

    overround = yes_ask + no_ask - 1.0

    return {
        "p_fair": p_fair,
        "kl_divergence": kl_fwd,
        "kl_reverse": kl_rev,
        "jensen_shannon": js,
        "overround": overround,
    }


# ---------------------------------------------------------------------------
# Divergence scaling factor (for Kelly sizing)
# ---------------------------------------------------------------------------


def divergence_scaling_factor(
    kl: float,
    low: float = 0.001,
    high: float = 0.05,
    min_scale: float = 0.5,
    max_scale: float = 2.0,
) -> float:
    """Map KL divergence to a position-size scaling factor.

    Piecewise linear:
        kl <= low  -> min_scale
        kl >= high -> max_scale
        between    -> linear interpolation

    Higher KL = more information = larger position.

    Args:
        kl: KL divergence value.
        low: KL below which min_scale applies.
        high: KL above which max_scale applies.
        min_scale: Scaling factor at low end.
        max_scale: Scaling factor at high end.

    Returns:
        Scaling multiplier in [min_scale, max_scale].
    """
    if kl <= low:
        return min_scale
    if kl >= high:
        return max_scale
    # Linear interpolation
    t = (kl - low) / (high - low)
    return min_scale + t * (max_scale - min_scale)


# ---------------------------------------------------------------------------
# Return correlation matrix
# ---------------------------------------------------------------------------


def compute_return_correlation(
    price_series: list[list[tuple[float, float]]],
    window_seconds: int = 3600,
) -> list[list[float]]:
    """Compute Pearson correlation matrix from multiple spot price histories.

    Each element in price_series is a list of (timestamp, price) tuples for
    one asset.  The function aligns timestamps across assets, computes
    log-returns, and returns the correlation matrix.

    Args:
        price_series: List of N price histories, each a list of (ts, price).
        window_seconds: Only use data within this window from the latest
            timestamp (used for filtering, not resampling).

    Returns:
        N x N correlation matrix as list-of-lists. Identity matrix if
        insufficient data.
    """
    n = len(price_series)
    if n == 0:
        return []

    # Identity fallback
    identity = [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]

    if n == 1:
        return [[1.0]]

    # Compute log-returns for each series
    all_returns: list[list[float]] = []
    for series in price_series:
        if len(series) < 3:
            return identity
        returns: list[float] = []
        for i in range(1, len(series)):
            prev_price = series[i - 1][1]
            curr_price = series[i][1]
            if prev_price > 0:
                returns.append(math.log(curr_price / prev_price))
        if not returns:
            return identity
        all_returns.append(returns)

    # Truncate to shortest series
    min_len = min(len(r) for r in all_returns)
    if min_len < 3:
        return identity

    for i in range(n):
        all_returns[i] = all_returns[i][-min_len:]

    # Compute correlation matrix
    means = [sum(r) / len(r) for r in all_returns]
    stddevs: list[float] = []
    for i in range(n):
        var = sum((x - means[i]) ** 2 for x in all_returns[i]) / len(all_returns[i])
        stddevs.append(math.sqrt(var) if var > 0 else 0.0)

    corr = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            if i == j:
                corr[i][j] = 1.0
                continue
            if stddevs[i] == 0 or stddevs[j] == 0:
                corr[i][j] = 0.0
                continue
            cov = sum(
                (all_returns[i][k] - means[i]) * (all_returns[j][k] - means[j])
                for k in range(min_len)
            ) / min_len
            corr[i][j] = cov / (stddevs[i] * stddevs[j])
            # Clamp to [-1, 1] for numerical safety
            corr[i][j] = max(-1.0, min(1.0, corr[i][j]))

    return corr


# ---------------------------------------------------------------------------
# Bregman projection (KL onto correlation-consistent set)
# ---------------------------------------------------------------------------


def bregman_projection(
    observed: list[float],
    correlation_matrix: list[list[float]],
    num_iterations: int = 50,
) -> list[float]:
    """KL-projection of observed probabilities onto a correlation-consistent set.

    Uses iterative Bregman projection (IPFP-style):
    For each iteration, adjust each probability toward the correlation-weighted
    mean of its neighbors, using a KL-penalized step.

    Args:
        observed: Vector of N observed probabilities (e.g. YES prices).
        correlation_matrix: N x N Pearson correlation matrix.
        num_iterations: Number of projection iterations.

    Returns:
        Projected probability vector (same length as observed).
    """
    n = len(observed)
    if n == 0:
        return []
    if n == 1:
        return list(observed)

    # Check correlation_matrix is compatible
    if len(correlation_matrix) != n:
        return list(observed)

    projected = [max(_EPS, min(1.0 - _EPS, p)) for p in observed]

    for _ in range(num_iterations):
        new_projected = list(projected)
        for i in range(n):
            # Correlation-weighted target
            total_weight = 0.0
            weighted_sum = 0.0
            for j in range(n):
                if i == j:
                    continue
                w = abs(correlation_matrix[i][j])
                weighted_sum += w * projected[j]
                total_weight += w

            if total_weight <= 0:
                continue

            target = weighted_sum / total_weight

            # KL-penalized step: move toward target but don't overshoot.
            # Use a soft blend (learning rate decays with iteration).
            alpha = 0.1  # conservative step size
            blended = projected[i] * (1.0 - alpha) + target * alpha
            new_projected[i] = max(_EPS, min(1.0 - _EPS, blended))

        projected = new_projected

    return projected


# ---------------------------------------------------------------------------
# Divergence-based exit signal
# ---------------------------------------------------------------------------


def divergence_exit_signal(
    entry_kl: float,
    current_kl: float,
    entry_direction: str,
    threshold_ratio: float = 0.5,
) -> bool:
    """Signal an exit when KL divergence has decayed below a ratio of entry KL.

    The thesis: we entered because the market was mispriced (high KL).
    If KL has dropped significantly, the mispricing has largely corrected
    and the remaining edge is too small to justify holding.

    Args:
        entry_kl: KL divergence at position entry time.
        current_kl: Current KL divergence.
        entry_direction: "UP" or "DOWN" (unused currently, reserved for
            directional refinement).
        threshold_ratio: Exit when current_kl < entry_kl * threshold_ratio.

    Returns:
        True if exit is signaled.
    """
    if entry_kl <= 0:
        return False
    return current_kl < entry_kl * threshold_ratio
