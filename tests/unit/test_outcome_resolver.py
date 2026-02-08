"""Tests for outcome resolver logic in src.main.resolve_outcome."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("BOT_PRIVATE_KEY", "0x" + "ab" * 32)

from src.core.models import Market, Position, StrategyType
from src.main import resolve_outcome
from src.utils.time_utils import WINDOW_SECONDS

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_END_TS = 1700000000.0  # arbitrary fixed epoch
_START_TS = _END_TS - WINDOW_SECONDS  # 900s before end


def _make_market(
    asset: str = "BTC",
    start_time: datetime | None = None,
    end_time: datetime | None = None,
) -> Market:
    """Create a Market with controllable timestamps.

    start_time defaults to 24h before end_time (mimicking Gamma API's
    startDate) to verify the resolver does NOT use it.
    """
    end = end_time or datetime.fromtimestamp(_END_TS, tz=UTC)
    # Deliberately set start_time ~24h before end to simulate the bug scenario
    start = start_time or datetime.fromtimestamp(
        _END_TS - 86400, tz=UTC
    )
    return Market(
        condition_id="cond_test_123",
        slug=f"{asset.lower()}-updown-15m-test",
        question=f"Will {asset} go up?",
        yes_token_id="YES_TOK",
        no_token_id="NO_TOK",
        start_time=start,
        end_time=end,
        asset=asset,
        neg_risk=True,
    )


def _make_position(market: Market | None = None) -> Position:
    return Position(
        market=market or _make_market(),
        yes_shares=10.0,
        strategy=StrategyType.PRICE_LAG,
    )


def _mock_trade_db(prices: dict[tuple[str, float], float]) -> MagicMock:
    """Create a mock TradeDatabase whose get_spot_at_time returns from a dict.

    Keys are (symbol, target_ts) rounded to int for tolerance matching.
    """
    db = MagicMock()

    def _get_spot(symbol: str, target_ts: float, tolerance_s: float = 30.0) -> float | None:
        # Match by checking if any key is within tolerance
        for (s, ts), price in prices.items():
            if s == symbol and abs(ts - target_ts) <= tolerance_s:
                return price
        return None

    db.get_spot_at_time = MagicMock(side_effect=_get_spot)
    return db


def _mock_spot_buffer(prices: dict[str, float]) -> MagicMock:
    buf = MagicMock()
    buf.get_price = MagicMock(side_effect=lambda sym: prices.get(sym))
    return buf


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestResolveOutcome:
    """Tests for the resolve_outcome function."""

    def test_uses_window_end_minus_900_not_start_time(self) -> None:
        """Key regression test: resolver must compute start_ts from
        end_time - 900s, NOT from market.start_time (Gamma API startDate).
        """
        market = _make_market(asset="BTC")
        pos = _make_position(market)

        db = _mock_trade_db({
            ("BTCUSDT", _START_TS): 95000.0,  # correct: end - 900
            ("BTCUSDT", _END_TS): 95500.0,
        })
        buf = _mock_spot_buffer({"BTCUSDT": 95500.0})

        result = resolve_outcome(pos, db, buf)

        # Verify DB was queried with end_ts - 900, NOT market.start_time
        calls = db.get_spot_at_time.call_args_list
        start_call = calls[0]
        queried_ts = start_call[0][1]  # second positional arg
        assert queried_ts == pytest.approx(_START_TS, abs=1.0), (
            f"Resolver queried start_ts={queried_ts}, expected {_START_TS} "
            f"(end_time - 900s). It should NOT use market.start_time "
            f"({market.start_time.timestamp()})."
        )
        assert result == 1.0  # price went up

    def test_uses_db_for_start_price(self) -> None:
        """Resolver must use trade_db.get_spot_at_time for the start price,
        not the in-memory SpotBuffer history scan.
        """
        db = _mock_trade_db({
            ("ETHUSDT", _START_TS): 3200.0,
            ("ETHUSDT", _END_TS): 3150.0,
        })
        buf = _mock_spot_buffer({"ETHUSDT": 3150.0})
        pos = _make_position(_make_market(asset="ETH"))

        result = resolve_outcome(pos, db, buf)

        # DB must have been called for start price
        db.get_spot_at_time.assert_called()
        first_call = db.get_spot_at_time.call_args_list[0]
        assert first_call[0][0] == "ETHUSDT"
        assert first_call[0][1] == pytest.approx(_START_TS, abs=1.0)
        # Price went down -> NO
        assert result == 0.0

    def test_returns_none_when_no_spot_data(self) -> None:
        """Graceful degradation: returns None when DB has no data."""
        db = _mock_trade_db({})  # empty — no spot data
        buf = _mock_spot_buffer({})  # empty buffer too

        pos = _make_position()
        result = resolve_outcome(pos, db, buf)
        assert result is None

    def test_returns_none_when_no_start_price(self) -> None:
        """Returns None if start price missing but end price exists."""
        db = _mock_trade_db({
            ("BTCUSDT", _END_TS): 95000.0,
        })
        buf = _mock_spot_buffer({"BTCUSDT": 95000.0})

        pos = _make_position()
        result = resolve_outcome(pos, db, buf)
        assert result is None

    def test_returns_none_when_trade_db_is_none(self) -> None:
        """Returns None if trade_db is not available."""
        buf = _mock_spot_buffer({"BTCUSDT": 95000.0})
        pos = _make_position()
        result = resolve_outcome(pos, None, buf)
        assert result is None

    def test_correct_outcome_price_up(self) -> None:
        """YES (1.0) when end price > start price."""
        db = _mock_trade_db({
            ("BTCUSDT", _START_TS): 94000.0,
            ("BTCUSDT", _END_TS): 94500.0,
        })
        buf = _mock_spot_buffer({"BTCUSDT": 94500.0})
        pos = _make_position()

        assert resolve_outcome(pos, db, buf) == 1.0

    def test_correct_outcome_price_down(self) -> None:
        """NO (0.0) when end price < start price."""
        db = _mock_trade_db({
            ("BTCUSDT", _START_TS): 94500.0,
            ("BTCUSDT", _END_TS): 94000.0,
        })
        buf = _mock_spot_buffer({"BTCUSDT": 94000.0})
        pos = _make_position()

        assert resolve_outcome(pos, db, buf) == 0.0

    def test_flat_price_returns_no(self) -> None:
        """When start == end price, price_went_up is False -> NO (0.0)."""
        db = _mock_trade_db({
            ("BTCUSDT", _START_TS): 94000.0,
            ("BTCUSDT", _END_TS): 94000.0,
        })
        buf = _mock_spot_buffer({"BTCUSDT": 94000.0})
        pos = _make_position()

        assert resolve_outcome(pos, db, buf) == 0.0

    def test_end_price_falls_back_to_db(self) -> None:
        """When spot buffer has no price, end price falls back to DB."""
        db = _mock_trade_db({
            ("BTCUSDT", _START_TS): 94000.0,
            ("BTCUSDT", _END_TS): 94500.0,
        })
        buf = _mock_spot_buffer({})  # buffer empty

        pos = _make_position()
        result = resolve_outcome(pos, db, buf)
        assert result == 1.0

    def test_end_price_prefers_buffer_over_db(self) -> None:
        """When buffer has a price, it's used for end price (not DB)."""
        db = _mock_trade_db({
            ("BTCUSDT", _START_TS): 94000.0,
            ("BTCUSDT", _END_TS): 93500.0,  # DB says down
        })
        # Buffer says up (buffer should win for end price)
        buf = _mock_spot_buffer({"BTCUSDT": 94500.0})
        pos = _make_position()

        result = resolve_outcome(pos, db, buf)
        assert result == 1.0  # buffer end price wins
