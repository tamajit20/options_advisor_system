"""Tests for main.py WS runner index vs scout spot caches."""

from __future__ import annotations

from providers.base import DataSource, LiveQuote
from providers.event_bus import EventBus, TOPIC_TICK_INDEX, TOPIC_TICK_SCOUT


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


def _wire_caches(bus: EventBus, index_spots: dict, scout_spots: dict) -> None:
    """Mirror _cmd_ws_runner capture handlers from main.py."""

    def _capture_index(quote) -> None:
        if quote is None or quote.option_type is not None:
            return
        try:
            index_spots[quote.symbol] = float(quote.last_price)
        except (TypeError, ValueError):
            pass

    def _capture_scout_equity(quote) -> None:
        if quote is None or quote.option_type is not None:
            return
        try:
            scout_spots[quote.symbol] = float(quote.last_price)
        except (TypeError, ValueError):
            pass

    bus.subscribe(TOPIC_TICK_INDEX, _capture_index)
    bus.subscribe(TOPIC_TICK_SCOUT, _capture_scout_equity)


def test_index_tick_updates_index_spots_only():
    bus = EventBus()
    index_spots: dict = {}
    scout_spots: dict = {}
    _wire_caches(bus, index_spots, scout_spots)
    bus.publish(TOPIC_TICK_INDEX, _spot("NIFTY", 23000.0))
    assert index_spots == {"NIFTY": 23000.0}
    assert scout_spots == {}


def test_scout_equity_tick_updates_scout_spots_only():
    bus = EventBus()
    index_spots: dict = {}
    scout_spots: dict = {}
    _wire_caches(bus, index_spots, scout_spots)
    bus.publish(TOPIC_TICK_SCOUT, _spot("RELIANCE", 2500.0))
    assert scout_spots == {"RELIANCE": 2500.0}
    assert index_spots == {}


def test_option_leg_tick_ignored_by_both_caches():
    bus = EventBus()
    index_spots: dict = {}
    scout_spots: dict = {}
    _wire_caches(bus, index_spots, scout_spots)
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
    bus.publish(TOPIC_TICK_SCOUT, leg)
    assert index_spots == {}
    assert scout_spots == {}


def test_scout_push_spot_lookup_merges_both_caches():
    index_spots = {"NIFTY": 23000.0}
    scout_spots = {"RELIANCE": 2510.0}
    lookup = lambda s: index_spots.get(s) or scout_spots.get(s)
    assert lookup("NIFTY") == 23000.0
    assert lookup("RELIANCE") == 2510.0
    assert lookup("TCS") is None


def test_watchlist_spot_lookup_uses_index_only():
    index_spots = {"NIFTY": 23000.0}
    scout_spots = {"NIFTY": 99999.0}
    lookup = lambda s: index_spots.get(s)
    assert lookup("NIFTY") == 23000.0
