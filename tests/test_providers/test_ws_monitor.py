"""Tests for `providers.ws_monitor.WSMonitor`."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from providers.event_bus import (
    EventBus,
    TOPIC_CONNECTION_STATE,
    TOPIC_TICK,
    TOPIC_TOKEN_EXPIRED,
)
from providers.ws_monitor import WSMonitor


class _Clock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


def _make_quote(symbol="NIFTY", strike=22500, opt="CE", price=12.5):
    return SimpleNamespace(
        symbol=symbol,
        strike=strike,
        option_type=opt,
        last_price=price,
    )


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def clock() -> _Clock:
    return _Clock(datetime(2025, 4, 1, 9, 30, 0, tzinfo=timezone.utc))


@pytest.fixture
def monitor(tmp_path: Path, bus: EventBus, clock: _Clock):
    snap = tmp_path / "ws_status.json"
    m = WSMonitor(
        snapshot_path=snap,
        event_bus=bus,
        provider="zerodha",
        max_recent_events=10,
        event_retention_seconds=30.0,
        rate_window_seconds=10.0,
        snapshot_interval_seconds=60.0,  # large so writer thread doesn't fire mid-test
        clock=clock,
    )
    m.start()
    yield m
    m.stop()


# ---------------------------------------------------------------------------
# Tick handling
# ---------------------------------------------------------------------------
class TestTickHandling:
    def test_tick_increments_total_and_per_symbol(self, monitor, bus):
        bus.publish(TOPIC_TICK, _make_quote("NIFTY", 22500, "CE", 12.5))
        bus.publish(TOPIC_TICK, _make_quote("NIFTY", 22500, "CE", 12.6))
        bus.publish(TOPIC_TICK, _make_quote("BANKNIFTY", 49000, "PE", 88.0))

        snap = monitor.snapshot()
        assert snap["tick_count_total"] == 3
        sym_map = {row["symbol"]: row["ticks"] for row in snap["top_symbols"]}
        assert sym_map["NIFTY"] == 2
        assert sym_map["BANKNIFTY"] == 1

    def test_tick_appears_in_recent_events(self, monitor, bus):
        bus.publish(TOPIC_TICK, _make_quote("NIFTY", 22500, "CE", 12.5))
        events = monitor.snapshot()["recent_events"]
        assert len(events) == 1
        assert events[0]["topic"] == "tick"
        assert events[0]["symbol"] == "NIFTY"
        assert events[0]["last_price"] == 12.5

    def test_tick_handler_swallows_exceptions(self, monitor, bus):
        # An object whose attribute access raises should not crash the handler.
        class _Boom:
            def __getattr__(self, name):
                raise RuntimeError("boom")
        bus.publish(TOPIC_TICK, _Boom())  # must not raise
        # Counter unchanged.
        assert monitor.snapshot()["tick_count_total"] == 0


# ---------------------------------------------------------------------------
# Rolling rate window
# ---------------------------------------------------------------------------
class TestRateWindow:
    def test_rate_reflects_window(self, monitor, bus, clock):
        # 5 ticks in 1 second, window = 10s → rate = 0.5/s
        for _ in range(5):
            bus.publish(TOPIC_TICK, _make_quote())
        snap = monitor.snapshot()
        assert snap["tick_rate_per_sec"] == pytest.approx(0.5, abs=1e-6)

    def test_old_ticks_drop_out_of_window(self, monitor, bus, clock):
        for _ in range(5):
            bus.publish(TOPIC_TICK, _make_quote())
        clock.advance(20.0)  # window is 10s, so all 5 ticks expire
        snap = monitor.snapshot()
        assert snap["tick_rate_per_sec"] == pytest.approx(0.0)
        # cumulative counter is unaffected
        assert snap["tick_count_total"] == 5


# ---------------------------------------------------------------------------
# Connection state / token expired
# ---------------------------------------------------------------------------
class TestConnectionState:
    def test_state_change_recorded(self, monitor, bus):
        bus.publish(TOPIC_CONNECTION_STATE,
                    {"provider": "zerodha", "state": "connected", "detail": None})
        snap = monitor.snapshot()
        assert snap["connection_state"] == "connected"
        assert any(e["topic"] == "connection_state" for e in snap["recent_events"])

    def test_degraded_records_last_error(self, monitor, bus):
        bus.publish(TOPIC_CONNECTION_STATE,
                    {"provider": "zerodha", "state": "degraded",
                     "detail": "no heartbeat for 35s"})
        snap = monitor.snapshot()
        assert snap["connection_state"] == "degraded"
        assert snap["last_error"] == "no heartbeat for 35s"

    def test_token_expired_flag(self, monitor, bus):
        bus.publish(TOPIC_TOKEN_EXPIRED, {"provider": "zerodha"})
        snap = monitor.snapshot()
        assert snap["token_expired"] is True
        assert snap["connection_state"] == "token_expired"


# ---------------------------------------------------------------------------
# Auto-prune (TTL)
# ---------------------------------------------------------------------------
class TestAutoPrune:
    def test_events_older_than_retention_are_dropped(self, monitor, bus, clock):
        bus.publish(TOPIC_TICK, _make_quote("NIFTY"))
        clock.advance(60.0)  # retention = 30s
        bus.publish(TOPIC_TICK, _make_quote("BANKNIFTY"))
        events = monitor.snapshot()["recent_events"]
        assert len(events) == 1
        assert events[0]["symbol"] == "BANKNIFTY"

    def test_ring_buffer_capped(self, monitor, bus):
        # max_recent_events=10
        for i in range(25):
            bus.publish(TOPIC_TICK, _make_quote(f"SYM{i:02d}"))
        events = monitor.snapshot()["recent_events"]
        assert len(events) == 10
        assert events[0]["symbol"] == "SYM15"
        assert events[-1]["symbol"] == "SYM24"


# ---------------------------------------------------------------------------
# Snapshot file write
# ---------------------------------------------------------------------------
class TestSnapshotFile:
    def test_writes_atomic_json_on_stop(self, tmp_path, bus, clock):
        path = tmp_path / "out" / "ws_status.json"  # nested dir auto-created
        m = WSMonitor(
            snapshot_path=path,
            event_bus=bus,
            provider="zerodha",
            snapshot_interval_seconds=60.0,
            clock=clock,
        )
        m.start()
        try:
            bus.publish(TOPIC_TICK, _make_quote())
        finally:
            m.stop()
        assert path.exists()
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        assert data["provider"] == "zerodha"
        assert data["tick_count_total"] >= 1
        assert "recent_events" in data
        assert "tick_rate_per_sec" in data


# ---------------------------------------------------------------------------
# status_fn integration
# ---------------------------------------------------------------------------
class TestStatusFn:
    def test_status_fn_fields_appear_in_snapshot(self, tmp_path, bus, clock):
        runner_status = SimpleNamespace(
            state=SimpleNamespace(value="connected"),
            subscribed_tokens=42,
            reconnect_attempts=1,
            watchdog_resets_window=0,
            last_tick_at=None,
            last_error=None,
            failure_started_at=None,
        )
        m = WSMonitor(
            snapshot_path=tmp_path / "ws.json",
            event_bus=bus,
            provider="zerodha",
            status_fn=lambda: runner_status,
            snapshot_interval_seconds=60.0,
            clock=clock,
        )
        m.start()
        try:
            snap = m.snapshot()
        finally:
            m.stop()
        assert snap["runner_state"] == "connected"
        assert snap["subscribed_tokens"] == 42
        assert snap["reconnect_attempts"] == 1

    def test_status_fn_exception_is_swallowed(self, tmp_path, bus, clock):
        def _raises():
            raise RuntimeError("kite died")
        m = WSMonitor(
            snapshot_path=tmp_path / "ws.json",
            event_bus=bus,
            provider="zerodha",
            status_fn=_raises,
            snapshot_interval_seconds=60.0,
            clock=clock,
        )
        m.start()
        try:
            snap = m.snapshot()  # must not raise
        finally:
            m.stop()
        assert snap["provider"] == "zerodha"
