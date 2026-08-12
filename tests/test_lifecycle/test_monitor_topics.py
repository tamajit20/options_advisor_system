"""Lifecycle monitor scoped topic subscription tests."""

from __future__ import annotations

from datetime import date, datetime

from lifecycle.intraday_monitor import IntradayMonitor, _Snapshot
from lifecycle.live_risk_monitor import LiveRiskMonitor, _Snapshot as RiskSnapshot
from providers.base import LiveQuote
from providers.event_bus import EventBus, TOPIC_TICK_INDEX, TOPIC_TICK_OPTIONS, TOPIC_TICK_SCOUT


def _option_quote(ltp=100.0):
    return LiveQuote(
        symbol="NIFTY",
        expiry=date(2026, 5, 28),
        strike=22000.0,
        option_type="CE",
        last_price=ltp,
    )


def _spot_quote(symbol="NIFTY", ltp=23000.0):
    return LiveQuote(symbol=symbol, expiry=None, strike=None, option_type=None, last_price=ltp)


class TestIntradayMonitorTopics:
    def test_start_subscribes_options_topic_only(self):
        bus = EventBus()
        mon = IntradayMonitor(
            notifier=MagicMockStub(),
            snapshot_loader=lambda: _Snapshot(),
            event_bus=bus,
        )
        mon.start()
        try:
            assert bus.subscriber_count(TOPIC_TICK_OPTIONS) >= 1
            assert bus.subscriber_count(TOPIC_TICK_INDEX) == 0
            assert bus.subscriber_count(TOPIC_TICK_SCOUT) == 0
        finally:
            mon.stop()

    def test_scout_equity_via_bus_ignored(self):
        from lifecycle.intraday_monitor import _SuggestionLegRef

        bus = EventBus()
        notif = MagicMockStub()
        leg = _SuggestionLegRef(
            "SUG-1", "x", 1, "SELL", 90.0, 85.0, 95.0,
            ("NIFTY", date(2026, 5, 28), 22000.0, "CE"),
        )
        snap = _Snapshot()
        snap.suggestions["SUG-1"] = [leg]
        snap.suggestion_index[leg.key] = [leg]
        mon = IntradayMonitor(
            notifier=notif,
            snapshot_loader=lambda: snap,
            event_bus=bus,
            reload_interval_seconds=3600,
        )
        mon.start()
        try:
            mon._reload_locked()
            bus.publish(TOPIC_TICK_SCOUT, _spot_quote("RELIANCE", 2500))
            bus.publish(TOPIC_TICK_INDEX, _spot_quote("NIFTY", 23000))
        finally:
            mon.stop()
        assert notif.events == []


class TestLiveRiskMonitorTopics:
    def test_start_subscribes_options_and_index_not_scout(self):
        bus = EventBus()
        mon = LiveRiskMonitor(
            notifier=MagicMockStub(),
            snapshot_loader=lambda: RiskSnapshot(),
            event_bus=bus,
            config={"enabled": True, "reload_interval_sec": 9999},
            clock=lambda: datetime(2026, 5, 5, 11, 0),
        )
        mon.start()
        try:
            assert bus.subscriber_count(TOPIC_TICK_OPTIONS) >= 1
            assert bus.subscriber_count(TOPIC_TICK_INDEX) >= 1
            assert bus.subscriber_count(TOPIC_TICK_SCOUT) == 0
        finally:
            mon.stop()

    def test_scout_equity_spot_tick_does_not_update_index_spot_index(self):
        bus = EventBus()
        mon = LiveRiskMonitor(
            notifier=MagicMockStub(),
            snapshot_loader=lambda: RiskSnapshot(),
            event_bus=bus,
            config={"enabled": True, "reload_interval_sec": 9999},
            clock=lambda: datetime(2026, 5, 5, 11, 0),
        )
        mon.start()
        try:
            mon._on_tick(_spot_quote("BPCL", 312.0))
        finally:
            mon.stop()
        assert mon.stats()["ticks_in"] == 1
        assert not mon._snapshot.spot_index.get("BPCL")


class MagicMockStub:
    def __init__(self):
        self.events = []

    def notify(self, *a, **kw):
        self.events.append({"args": a, "kw": kw})
