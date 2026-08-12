"""Tests for scout tick classification on the shared WS bus."""

from __future__ import annotations

from datetime import datetime

from providers.base import DataSource, LiveQuote
from scout.tick_filter import is_scout_equity_tick


def _eq(symbol: str, price: float = 100.0) -> LiveQuote:
    return LiveQuote(
        symbol=symbol,
        expiry=None,
        strike=None,
        option_type=None,
        last_price=price,
        timestamp=datetime(2026, 8, 12, 10, 0, 0),
        source=DataSource.LIVE,
        provider="test",
    )


def test_watchlist_equity_is_scout_tick():
    assert is_scout_equity_tick(_eq("RELIANCE"), {"RELIANCE", "TCS"}) is True


def test_non_watchlist_equity_rejected():
    assert is_scout_equity_tick(_eq("INFY"), {"RELIANCE"}) is False


def test_index_symbol_rejected():
    assert is_scout_equity_tick(_eq("NIFTY"), {"NIFTY"}) is False


def test_option_leg_rejected():
    q = LiveQuote(
        symbol="RELIANCE",
        expiry=datetime(2026, 8, 28).date(),
        strike=2500.0,
        option_type="CE",
        last_price=50.0,
        source=DataSource.LIVE,
        provider="test",
    )
    assert is_scout_equity_tick(q, {"RELIANCE"}) is False
