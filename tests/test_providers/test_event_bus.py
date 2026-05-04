"""
tests/test_providers/test_event_bus.py
======================================

Unit tests for `providers.event_bus.EventBus`.
"""

from __future__ import annotations

import pytest

from providers.event_bus import (
    EventBus,
    TOPIC_TICK,
    get_event_bus,
    reset_event_bus,
)


def test_subscribe_and_publish_invokes_handler():
    bus = EventBus()
    seen = []
    bus.subscribe(TOPIC_TICK, lambda p: seen.append(p))
    n = bus.publish(TOPIC_TICK, {"price": 100})
    assert n == 1
    assert seen == [{"price": 100}]


def test_publish_to_topic_with_no_subscribers_returns_zero():
    bus = EventBus()
    assert bus.publish("ghost", "x") == 0


def test_unsubscribe_stops_delivery():
    bus = EventBus()
    seen = []
    unsub = bus.subscribe("t", seen.append)
    bus.publish("t", 1)
    unsub()
    bus.publish("t", 2)
    assert seen == [1]


def test_handler_exception_does_not_break_others():
    bus = EventBus()
    seen = []

    def bad(_p):
        raise RuntimeError("boom")

    bus.subscribe("t", bad)
    bus.subscribe("t", seen.append)
    n = bus.publish("t", 42)
    assert n == 2
    assert seen == [42]


def test_subscribe_rejects_non_callable():
    bus = EventBus()
    with pytest.raises(TypeError):
        bus.subscribe("t", "not-callable")  # type: ignore[arg-type]


def test_subscriber_count():
    bus = EventBus()
    bus.subscribe("a", lambda _: None)
    bus.subscribe("a", lambda _: None)
    bus.subscribe("b", lambda _: None)
    assert bus.subscriber_count("a") == 2
    assert bus.subscriber_count("b") == 1
    assert bus.subscriber_count() == 3


def test_clear_drops_all_subscribers():
    bus = EventBus()
    bus.subscribe("t", lambda _: None)
    bus.clear()
    assert bus.subscriber_count() == 0


def test_singleton_get_event_bus_returns_same_instance():
    reset_event_bus()
    a = get_event_bus()
    b = get_event_bus()
    assert a is b
    reset_event_bus()
    c = get_event_bus()
    assert c is not a
