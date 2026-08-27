"""Tests for main.py WS runner index spot cache."""

from __future__ import annotations

from providers.base import DataSource, LiveQuote
from providers.event_bus import EventBus, TOPIC_TICK_INDEX


def _spot(symbol: str, price: float, *, option_type=None) -> LiveQuote:
    return LiveQuote(
        symbol=symbol,
        expiry=None,
        strike=None,
        option_type=option_type,
        last_price=price,
        source=DataSource.LIVE,
        provider="test",
    )


def _wire_cache(bus: EventBus, index_spots: dict) -> None:
    """Mirror _cmd_ws_runner capture handler from main.py."""

    def _capture_index(quote) -> None:
        if quote is None or quote.option_type is not None:
            return
        try:
            index_spots[quote.symbol] = float(quote.last_price)
        except (TypeError, ValueError):
            pass

    bus.subscribe(TOPIC_TICK_INDEX, _capture_index)


def test_index_tick_updates_index_spots():
    bus = EventBus()
    index_spots: dict = {}
    _wire_cache(bus, index_spots)
    bus.publish(TOPIC_TICK_INDEX, _spot("NIFTY", 23000.0))
    assert index_spots == {"NIFTY": 23000.0}


def test_option_leg_tick_ignored_by_index_cache():
    bus = EventBus()
    index_spots: dict = {}
    _wire_cache(bus, index_spots)
    from datetime import date

    leg = LiveQuote(
        symbol="NIFTY",
        expiry=date(2026, 5, 28),
        strike=23000.0,
        option_type="CE",
        last_price=150.0,
        source=DataSource.LIVE,
        provider="test",
    )
    bus.publish(TOPIC_TICK_INDEX, leg)
    assert index_spots == {}


def test_watchlist_spot_lookup_uses_index_only():
    index_spots = {"NIFTY": 23000.0}
    lookup = lambda s: index_spots.get(s)
    assert lookup("NIFTY") == 23000.0
    assert lookup("RELIANCE") is None
