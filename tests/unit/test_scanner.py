"""Comprehensive tests for MarketScanner."""

from __future__ import annotations

import os

# Set the required environment variable BEFORE any Settings import
os.environ.setdefault("BOT_PRIVATE_KEY", "0x" + "ab" * 32)

from datetime import datetime, timezone

import pytest

from src.core.models import Market, Opportunity, Position, StrategyType
from src.strategy.base import BaseStrategy
from src.strategy.scanner import MarketScanner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime.now(tz=timezone.utc)
_START = datetime.fromtimestamp(_NOW.timestamp() - 300, tz=timezone.utc)
_END = datetime.fromtimestamp(_NOW.timestamp() + 600, tz=timezone.utc)


def _make_market(
    condition_id: str = "cond_123",
    slug: str = "btc-updown-15m-test",
    asset: str = "BTC",
) -> Market:
    return Market(
        condition_id=condition_id,
        slug=slug,
        question="Will BTC go up?",
        yes_token_id="YES_TOKEN",
        no_token_id="NO_TOKEN",
        start_time=_START,
        end_time=_END,
        asset=asset,
        neg_risk=True,
    )


def _make_opportunity(
    market: Market,
    expected_profit_pct: float = 0.05,
    strategy_type: StrategyType = StrategyType.ARBITRAGE,
) -> Opportunity:
    return Opportunity(
        strategy=strategy_type,
        market=market,
        timestamp=_NOW,
        expected_profit=expected_profit_pct * 50.0,
        expected_profit_pct=expected_profit_pct,
        total_fees=0.5,
        confidence=0.8,
    )


# ---------------------------------------------------------------------------
# Mock strategies
# ---------------------------------------------------------------------------


class MockStrategy(BaseStrategy):
    """A simple mock strategy that returns a fixed result for any market."""

    def __init__(self, name: str, result: Opportunity | None = None) -> None:
        self._name = name
        self._result = result
        # Don't call super().__init__() - we're a mock

    @property
    def name(self) -> str:
        return self._name

    @property
    def strategy_type(self) -> StrategyType:
        return StrategyType.ARBITRAGE

    async def evaluate(self, market: Market) -> Opportunity | None:
        return self._result

    def should_exit(self, position: Position, market: Market) -> bool:
        return False


class MockStrategyPerMarket(BaseStrategy):
    """Mock strategy that returns different results per market slug."""

    def __init__(
        self,
        name: str,
        results: dict[str, Opportunity | None],
        strategy_type: StrategyType = StrategyType.ARBITRAGE,
    ) -> None:
        self._name = name
        self._results = results
        self._strategy_type = strategy_type
        # Don't call super().__init__()

    @property
    def name(self) -> str:
        return self._name

    @property
    def strategy_type(self) -> StrategyType:
        return self._strategy_type

    async def evaluate(self, market: Market) -> Opportunity | None:
        return self._results.get(market.slug)

    def should_exit(self, position: Position, market: Market) -> bool:
        return False


class FailingStrategy(BaseStrategy):
    """A mock strategy that always raises an exception during evaluate()."""

    def __init__(self, name: str = "failing") -> None:
        self._name = name
        # Don't call super().__init__()

    @property
    def name(self) -> str:
        return self._name

    @property
    def strategy_type(self) -> StrategyType:
        return StrategyType.ARBITRAGE

    async def evaluate(self, market: Market) -> Opportunity | None:
        msg = f"Strategy {self._name} blew up"
        raise RuntimeError(msg)

    def should_exit(self, position: Position, market: Market) -> bool:
        return False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def market() -> Market:
    return _make_market()


@pytest.fixture()
def market_b() -> Market:
    return _make_market(condition_id="cond_456", slug="eth-updown-15m-test", asset="ETH")


@pytest.fixture()
def market_c() -> Market:
    return _make_market(condition_id="cond_789", slug="sol-updown-15m-test", asset="SOL")


# ---------------------------------------------------------------------------
# Tests: scan returns None
# ---------------------------------------------------------------------------


class TestScanReturnsNone:
    """Tests where scan() must return None."""

    async def test_no_strategies(self, market: Market) -> None:
        """scan returns None when scanner has no strategies."""
        scanner = MarketScanner(strategies=[])
        result = await scanner.scan([market])
        assert result is None

    async def test_no_markets(self) -> None:
        """scan returns None when no markets are provided."""
        strategy = MockStrategy("arb", result=None)
        scanner = MarketScanner(strategies=[strategy])
        result = await scanner.scan([])
        assert result is None

    async def test_all_strategies_return_none(self, market: Market) -> None:
        """scan returns None when all strategies return None for all markets."""
        s1 = MockStrategy("arb", result=None)
        s2 = MockStrategy("asym", result=None)
        scanner = MarketScanner(strategies=[s1, s2])
        result = await scanner.scan([market])
        assert result is None

    async def test_empty_strategies_and_empty_markets(self) -> None:
        """scan returns None when both strategies and markets are empty."""
        scanner = MarketScanner(strategies=[])
        result = await scanner.scan([])
        assert result is None


# ---------------------------------------------------------------------------
# Tests: scan returns best opportunity
# ---------------------------------------------------------------------------


class TestScanReturnsBest:
    """Tests where scan() must return the best opportunity."""

    async def test_single_strategy_single_market(self, market: Market) -> None:
        """Single strategy, single market -- returns that opportunity."""
        opp = _make_opportunity(market, expected_profit_pct=0.05)
        strategy = MockStrategy("arb", result=opp)
        scanner = MarketScanner(strategies=[strategy])

        result = await scanner.scan([market])

        assert result is not None
        assert result is opp
        assert result.expected_profit_pct == 0.05

    async def test_picks_highest_profit_pct(self, market: Market) -> None:
        """When multiple strategies return opportunities, picks highest profit_pct."""
        opp_low = _make_opportunity(market, expected_profit_pct=0.02)
        opp_high = _make_opportunity(market, expected_profit_pct=0.10)
        s1 = MockStrategy("low", result=opp_low)
        s2 = MockStrategy("high", result=opp_high)
        scanner = MarketScanner(strategies=[s1, s2])

        result = await scanner.scan([market])

        assert result is not None
        assert result is opp_high

    async def test_multiple_strategies_multiple_markets(
        self, market: Market, market_b: Market, market_c: Market,
    ) -> None:
        """Multiple strategies x multiple markets -- picks the global best."""
        opp_arb_btc = _make_opportunity(market, expected_profit_pct=0.03)
        opp_arb_eth = _make_opportunity(market_b, expected_profit_pct=0.07)
        opp_asym_btc = _make_opportunity(market, expected_profit_pct=0.01)
        opp_asym_sol = _make_opportunity(market_c, expected_profit_pct=0.09)

        s_arb = MockStrategyPerMarket("arb", {
            market.slug: opp_arb_btc,
            market_b.slug: opp_arb_eth,
        })
        s_asym = MockStrategyPerMarket(
            "asym",
            {
                market.slug: opp_asym_btc,
                market_c.slug: opp_asym_sol,
            },
            strategy_type=StrategyType.ASYMMETRIC,
        )
        scanner = MarketScanner(strategies=[s_arb, s_asym])

        result = await scanner.scan([market, market_b, market_c])

        assert result is not None
        assert result is opp_asym_sol
        assert result.expected_profit_pct == 0.09

    async def test_tie_broken_by_insertion_order(self, market: Market, market_b: Market) -> None:
        """When two opportunities have the same profit_pct, the first found wins.

        With Python's stable sort, the first one encountered in the
        (strategy, market) iteration order is kept first.
        """
        opp_a = _make_opportunity(market, expected_profit_pct=0.05)
        opp_b = _make_opportunity(market_b, expected_profit_pct=0.05)
        # s1 is first in strategy list, so opp_a should come first
        s1 = MockStrategyPerMarket("first", {market.slug: opp_a})
        s2 = MockStrategyPerMarket("second", {market_b.slug: opp_b})
        scanner = MarketScanner(strategies=[s1, s2])

        result = await scanner.scan([market, market_b])

        assert result is not None
        assert result is opp_a


# ---------------------------------------------------------------------------
# Tests: scan_all
# ---------------------------------------------------------------------------


class TestScanAll:
    """Tests for scan_all() returning the full ranked list."""

    async def test_returns_sorted_list(self, market: Market) -> None:
        """scan_all returns all opportunities sorted by profit_pct desc."""
        opp_low = _make_opportunity(market, expected_profit_pct=0.01)
        opp_mid = _make_opportunity(market, expected_profit_pct=0.05)
        opp_high = _make_opportunity(market, expected_profit_pct=0.10)
        s1 = MockStrategy("low", result=opp_low)
        s2 = MockStrategy("mid", result=opp_mid)
        s3 = MockStrategy("high", result=opp_high)
        scanner = MarketScanner(strategies=[s1, s2, s3])

        result = await scanner.scan_all([market])

        assert len(result) == 3
        assert result[0] is opp_high
        assert result[1] is opp_mid
        assert result[2] is opp_low

    async def test_returns_empty_list_when_no_opportunities(self, market: Market) -> None:
        """scan_all returns [] when all strategies return None."""
        s1 = MockStrategy("arb", result=None)
        scanner = MarketScanner(strategies=[s1])

        result = await scanner.scan_all([market])

        assert result == []

    async def test_returns_empty_list_for_empty_markets(self) -> None:
        """scan_all returns [] when markets list is empty."""
        s1 = MockStrategy("arb", result=None)
        scanner = MarketScanner(strategies=[s1])

        result = await scanner.scan_all([])

        assert result == []

    async def test_returns_empty_list_for_empty_strategies(self, market: Market) -> None:
        """scan_all returns [] when strategies list is empty."""
        scanner = MarketScanner(strategies=[])

        result = await scanner.scan_all([market])

        assert result == []

    async def test_filters_out_none_results(self, market: Market) -> None:
        """scan_all excludes None results from strategies that found nothing."""
        opp = _make_opportunity(market, expected_profit_pct=0.05)
        s_good = MockStrategy("good", result=opp)
        s_none = MockStrategy("none", result=None)
        scanner = MarketScanner(strategies=[s_good, s_none])

        result = await scanner.scan_all([market])

        assert len(result) == 1
        assert result[0] is opp

    async def test_multi_market_multi_strategy(
        self, market: Market, market_b: Market,
    ) -> None:
        """scan_all with multiple strategies and multiple markets returns all matches."""
        opp_a = _make_opportunity(market, expected_profit_pct=0.03)
        opp_b = _make_opportunity(market_b, expected_profit_pct=0.08)
        opp_c = _make_opportunity(market, expected_profit_pct=0.06)

        s1 = MockStrategyPerMarket("arb", {market.slug: opp_a, market_b.slug: opp_b})
        s2 = MockStrategyPerMarket("asym", {market.slug: opp_c})
        scanner = MarketScanner(strategies=[s1, s2])

        result = await scanner.scan_all([market, market_b])

        assert len(result) == 3
        assert result[0] is opp_b  # 0.08
        assert result[1] is opp_c  # 0.06
        assert result[2] is opp_a  # 0.03


# ---------------------------------------------------------------------------
# Tests: exception handling
# ---------------------------------------------------------------------------


class TestExceptionHandling:
    """Tests for graceful exception handling in strategy.evaluate()."""

    async def test_failing_strategy_is_skipped(self, market: Market) -> None:
        """A strategy that raises is caught and skipped -- scan returns None."""
        scanner = MarketScanner(strategies=[FailingStrategy()])

        result = await scanner.scan([market])

        assert result is None

    async def test_failing_strategy_does_not_block_others(self, market: Market) -> None:
        """A failing strategy doesn't prevent other strategies from succeeding."""
        opp = _make_opportunity(market, expected_profit_pct=0.05)
        s_good = MockStrategy("good", result=opp)
        s_fail = FailingStrategy("bad")
        scanner = MarketScanner(strategies=[s_fail, s_good])

        result = await scanner.scan([market])

        assert result is not None
        assert result is opp

    async def test_scan_all_with_failing_strategy(self, market: Market) -> None:
        """scan_all gracefully handles a failing strategy."""
        opp = _make_opportunity(market, expected_profit_pct=0.05)
        s_good = MockStrategy("good", result=opp)
        s_fail = FailingStrategy("bad")
        scanner = MarketScanner(strategies=[s_fail, s_good])

        result = await scanner.scan_all([market])

        assert len(result) == 1
        assert result[0] is opp

    async def test_all_strategies_fail(self, market: Market) -> None:
        """When every strategy raises, scan returns None gracefully."""
        scanner = MarketScanner(strategies=[FailingStrategy("a"), FailingStrategy("b")])

        result = await scanner.scan([market])

        assert result is None


# ---------------------------------------------------------------------------
# Tests: properties
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Tests: scan_best_per_strategy (A/B test mode)
# ---------------------------------------------------------------------------


class TestScanBestPerStrategy:
    """Tests for scan_best_per_strategy() - A/B testing mode."""

    async def test_returns_empty_dict_when_no_opportunities(self, market: Market) -> None:
        """Returns empty dict when no strategies find opportunities."""
        s1 = MockStrategy("arb", result=None)
        scanner = MarketScanner(strategies=[s1])

        result = await scanner.scan_best_per_strategy([market])

        assert result == {}

    async def test_returns_empty_dict_for_empty_markets(self) -> None:
        """Returns empty dict when markets list is empty."""
        opp = _make_opportunity(_make_market(), expected_profit_pct=0.05)
        s1 = MockStrategy("arb", result=opp)
        scanner = MarketScanner(strategies=[s1])

        result = await scanner.scan_best_per_strategy([])

        assert result == {}

    async def test_returns_one_opp_per_strategy_type(
        self, market: Market, market_b: Market
    ) -> None:
        """Returns best opportunity for each strategy type."""
        opp_arb = _make_opportunity(market, expected_profit_pct=0.05, strategy_type=StrategyType.ARBITRAGE)
        opp_asym = _make_opportunity(market_b, expected_profit_pct=0.03, strategy_type=StrategyType.ASYMMETRIC)

        s_arb = MockStrategyPerMarket("arb", {market.slug: opp_arb}, strategy_type=StrategyType.ARBITRAGE)
        s_asym = MockStrategyPerMarket("asym", {market_b.slug: opp_asym}, strategy_type=StrategyType.ASYMMETRIC)
        scanner = MarketScanner(strategies=[s_arb, s_asym])

        result = await scanner.scan_best_per_strategy([market, market_b])

        assert len(result) == 2
        assert result[StrategyType.ARBITRAGE] is opp_arb
        assert result[StrategyType.ASYMMETRIC] is opp_asym

    async def test_picks_best_within_each_strategy_type(
        self, market: Market, market_b: Market
    ) -> None:
        """When multiple opportunities exist for same strategy type, picks best."""
        opp_arb_low = _make_opportunity(market, expected_profit_pct=0.02, strategy_type=StrategyType.ARBITRAGE)
        opp_arb_high = _make_opportunity(market_b, expected_profit_pct=0.08, strategy_type=StrategyType.ARBITRAGE)

        s_arb = MockStrategyPerMarket(
            "arb",
            {market.slug: opp_arb_low, market_b.slug: opp_arb_high},
            strategy_type=StrategyType.ARBITRAGE,
        )
        scanner = MarketScanner(strategies=[s_arb])

        result = await scanner.scan_best_per_strategy([market, market_b])

        assert len(result) == 1
        assert result[StrategyType.ARBITRAGE] is opp_arb_high

    async def test_maker_vs_taker_arbitrage_parallel(
        self, market: Market, market_b: Market
    ) -> None:
        """Test A/B mode with both arbitrage and maker_arbitrage strategies."""
        opp_taker = _make_opportunity(market, expected_profit_pct=0.04, strategy_type=StrategyType.ARBITRAGE)
        opp_maker = _make_opportunity(market, expected_profit_pct=0.06, strategy_type=StrategyType.MAKER_ARBITRAGE)

        s_taker = MockStrategyPerMarket("arbitrage", {market.slug: opp_taker}, strategy_type=StrategyType.ARBITRAGE)
        s_maker = MockStrategyPerMarket("maker_arbitrage", {market.slug: opp_maker}, strategy_type=StrategyType.MAKER_ARBITRAGE)
        scanner = MarketScanner(strategies=[s_taker, s_maker])

        result = await scanner.scan_best_per_strategy([market])

        # Both strategies should return their best
        assert len(result) == 2
        assert result[StrategyType.ARBITRAGE] is opp_taker
        assert result[StrategyType.MAKER_ARBITRAGE] is opp_maker

    async def test_handles_failing_strategy(self, market: Market) -> None:
        """Failing strategies are skipped, others still return."""
        opp = _make_opportunity(market, expected_profit_pct=0.05, strategy_type=StrategyType.ARBITRAGE)
        s_good = MockStrategyPerMarket("arb", {market.slug: opp}, strategy_type=StrategyType.ARBITRAGE)
        s_fail = FailingStrategy("bad")
        scanner = MarketScanner(strategies=[s_fail, s_good])

        result = await scanner.scan_best_per_strategy([market])

        assert len(result) == 1
        assert result[StrategyType.ARBITRAGE] is opp


class TestProperties:
    """Tests for strategy_count and strategy_names properties."""

    def test_strategy_count_zero(self) -> None:
        scanner = MarketScanner(strategies=[])
        assert scanner.strategy_count == 0

    def test_strategy_count_multiple(self) -> None:
        s1 = MockStrategy("arb")
        s2 = MockStrategy("asym")
        s3 = MockStrategy("lag")
        scanner = MarketScanner(strategies=[s1, s2, s3])
        assert scanner.strategy_count == 3

    def test_strategy_names_empty(self) -> None:
        scanner = MarketScanner(strategies=[])
        assert scanner.strategy_names == []

    def test_strategy_names_returns_correct_names(self) -> None:
        s1 = MockStrategy("arb")
        s2 = MockStrategy("asym")
        scanner = MarketScanner(strategies=[s1, s2])
        assert scanner.strategy_names == ["arb", "asym"]

    def test_strategy_names_order_matches_insertion(self) -> None:
        s1 = MockStrategy("zulu")
        s2 = MockStrategy("alpha")
        scanner = MarketScanner(strategies=[s1, s2])
        # Order should be insertion order, not alphabetical
        assert scanner.strategy_names == ["zulu", "alpha"]
