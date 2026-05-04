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


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------
class TestConstructorValidation:
    def test_rejects_zero_max_recent_events(self, tmp_path, bus):
        with pytest.raises(ValueError):
            WSMonitor(snapshot_path=tmp_path / "x.json", event_bus=bus,
                      max_recent_events=0)

    def test_rejects_negative_max_recent_events(self, tmp_path, bus):
        with pytest.raises(ValueError):
            WSMonitor(snapshot_path=tmp_path / "x.json", event_bus=bus,
                      max_recent_events=-1)

    def test_rejects_zero_event_retention(self, tmp_path, bus):
        with pytest.raises(ValueError):
            WSMonitor(snapshot_path=tmp_path / "x.json", event_bus=bus,
                      event_retention_seconds=0)

    def test_rejects_zero_rate_window(self, tmp_path, bus):
        with pytest.raises(ValueError):
            WSMonitor(snapshot_path=tmp_path / "x.json", event_bus=bus,
                      rate_window_seconds=0)

    def test_rejects_zero_snapshot_interval(self, tmp_path, bus):
        with pytest.raises(ValueError):
            WSMonitor(snapshot_path=tmp_path / "x.json", event_bus=bus,
                      snapshot_interval_seconds=0)


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
class TestDefaults:
    def test_snapshot_interval_default_is_half_second(self, tmp_path, bus):
        m = WSMonitor(snapshot_path=tmp_path / "x.json", event_bus=bus)
        assert m._snapshot_interval == 0.5

    def test_default_provider_is_zerodha(self, tmp_path, bus):
        m = WSMonitor(snapshot_path=tmp_path / "x.json", event_bus=bus,
                      snapshot_interval_seconds=60.0)
        snap = m.snapshot()
        assert snap["provider"] == "zerodha"

    def test_max_recent_events_default(self, tmp_path, bus):
        m = WSMonitor(snapshot_path=tmp_path / "x.json", event_bus=bus,
                      snapshot_interval_seconds=60.0)
        assert m._max_events == 200

    def test_retention_default_is_300s(self, tmp_path, bus):
        m = WSMonitor(snapshot_path=tmp_path / "x.json", event_bus=bus,
                      snapshot_interval_seconds=60.0)
        assert m._retention == 300.0

    def test_rate_window_default_is_60s(self, tmp_path, bus):
        m = WSMonitor(snapshot_path=tmp_path / "x.json", event_bus=bus,
                      snapshot_interval_seconds=60.0)
        assert m._rate_window == 60.0


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
class TestLifecycle:
    def test_double_start_is_idempotent(self, tmp_path, bus, clock):
        m = WSMonitor(snapshot_path=tmp_path / "x.json", event_bus=bus,
                      snapshot_interval_seconds=60.0, clock=clock)
        m.start()
        try:
            n_subs_after_first = bus.subscriber_count(TOPIC_TICK)
            m.start()  # no-op
            assert bus.subscriber_count(TOPIC_TICK) == n_subs_after_first
        finally:
            m.stop()

    def test_stop_unsubscribes_from_bus(self, tmp_path, bus, clock):
        m = WSMonitor(snapshot_path=tmp_path / "x.json", event_bus=bus,
                      snapshot_interval_seconds=60.0, clock=clock)
        m.start()
        assert bus.subscriber_count(TOPIC_TICK) >= 1
        m.stop()
        assert bus.subscriber_count(TOPIC_TICK) == 0
        assert bus.subscriber_count(TOPIC_CONNECTION_STATE) == 0
        assert bus.subscriber_count(TOPIC_TOKEN_EXPIRED) == 0

    def test_events_after_stop_are_ignored(self, tmp_path, bus, clock):
        m = WSMonitor(snapshot_path=tmp_path / "x.json", event_bus=bus,
                      snapshot_interval_seconds=60.0, clock=clock)
        m.start()
        m.stop()
        bus.publish(TOPIC_TICK, _make_quote())
        snap = m.snapshot()
        # After stop the handlers are detached; counter must not advance.
        assert snap["tick_count_total"] == 0

    def test_uses_singleton_bus_when_none_provided(self, tmp_path, monkeypatch):
        from providers import event_bus as eb
        eb.reset_event_bus()
        m = WSMonitor(snapshot_path=tmp_path / "x.json",
                      snapshot_interval_seconds=60.0)
        m.start()
        try:
            singleton = eb.get_event_bus()
            assert singleton.subscriber_count(TOPIC_TICK) >= 1
        finally:
            m.stop()
            eb.reset_event_bus()


# ---------------------------------------------------------------------------
# Snapshot shape
# ---------------------------------------------------------------------------
class TestSnapshotShape:
    def test_snapshot_has_all_required_keys(self, monitor):
        snap = monitor.snapshot()
        for key in (
            "provider", "generated_at", "started_at", "uptime_seconds",
            "connection_state", "token_expired", "last_tick_at",
            "last_state_change_at", "last_error", "tick_count_total",
            "tick_rate_per_sec", "rate_window_seconds", "top_symbols",
            "recent_events", "max_recent_events", "event_retention_seconds",
        ):
            assert key in snap, f"missing key: {key}"

    def test_initial_snapshot_has_unknown_state(self, monitor):
        snap = monitor.snapshot()
        assert snap["connection_state"] == "unknown"
        assert snap["token_expired"] is False
        assert snap["tick_count_total"] == 0
        assert snap["last_tick_at"] is None
        assert snap["last_error"] is None

    def test_uptime_increases_with_clock(self, monitor, clock):
        clock.advance(7.0)
        snap = monitor.snapshot()
        assert snap["uptime_seconds"] == pytest.approx(7.0, abs=1e-6)

    def test_top_symbols_are_sorted_descending(self, monitor, bus):
        for _ in range(3):
            bus.publish(TOPIC_TICK, _make_quote("NIFTY"))
        for _ in range(7):
            bus.publish(TOPIC_TICK, _make_quote("BANKNIFTY"))
        for _ in range(1):
            bus.publish(TOPIC_TICK, _make_quote("FINNIFTY"))
        top = monitor.snapshot()["top_symbols"]
        counts = [row["ticks"] for row in top]
        assert counts == sorted(counts, reverse=True)
        assert top[0]["symbol"] == "BANKNIFTY"
        assert top[0]["ticks"] == 7

    def test_top_symbols_capped_at_20(self, monitor, bus):
        for i in range(50):
            bus.publish(TOPIC_TICK, _make_quote(f"SYM{i:02d}"))
        top = monitor.snapshot()["top_symbols"]
        assert len(top) <= 20

    def test_snapshot_is_json_serialisable(self, monitor, bus):
        bus.publish(TOPIC_TICK, _make_quote())
        bus.publish(TOPIC_CONNECTION_STATE,
                    {"provider": "zerodha", "state": "connected"})
        snap = monitor.snapshot()
        # Should not raise.
        s = json.dumps(snap, default=str)
        assert isinstance(s, str)
        assert len(s) > 0


# ---------------------------------------------------------------------------
# Tick payload variations
# ---------------------------------------------------------------------------
class TestTickPayloadVariants:
    def test_tick_with_none_strike_and_price(self, monitor, bus):
        q = SimpleNamespace(symbol="VIX", strike=None,
                            option_type=None, last_price=None)
        bus.publish(TOPIC_TICK, q)
        ev = monitor.snapshot()["recent_events"][0]
        assert ev["symbol"] == "VIX"
        assert ev["strike"] is None
        assert ev["last_price"] is None

    def test_tick_without_symbol_attribute(self, monitor, bus):
        q = SimpleNamespace(strike=100, option_type="CE", last_price=10.0)
        bus.publish(TOPIC_TICK, q)
        # symbol falls back to "?"
        ev = monitor.snapshot()["recent_events"][0]
        assert ev["symbol"] == "?"

    def test_tick_with_falsy_symbol_coerces_to_question_mark(self, monitor, bus):
        q = SimpleNamespace(symbol="", strike=1, option_type="CE", last_price=1.0)
        bus.publish(TOPIC_TICK, q)
        ev = monitor.snapshot()["recent_events"][0]
        assert ev["symbol"] == "?"

    def test_strike_is_coerced_to_float(self, monitor, bus):
        q = SimpleNamespace(symbol="NIFTY", strike="22500",
                            option_type="CE", last_price="12.5")
        bus.publish(TOPIC_TICK, q)
        ev = monitor.snapshot()["recent_events"][0]
        assert ev["strike"] == 22500.0
        assert ev["last_price"] == 12.5
        assert isinstance(ev["strike"], float)
        assert isinstance(ev["last_price"], float)

    def test_last_tick_at_updates(self, monitor, bus, clock):
        bus.publish(TOPIC_TICK, _make_quote())
        first = monitor.snapshot()["last_tick_at"]
        clock.advance(3.0)
        bus.publish(TOPIC_TICK, _make_quote())
        second = monitor.snapshot()["last_tick_at"]
        assert second > first


# ---------------------------------------------------------------------------
# Connection state edge cases
# ---------------------------------------------------------------------------
class TestConnectionStateEdgeCases:
    def test_disconnected_with_detail_records_error(self, monitor, bus):
        bus.publish(TOPIC_CONNECTION_STATE,
                    {"provider": "zerodha", "state": "disconnected",
                     "detail": "socket EOF"})
        snap = monitor.snapshot()
        assert snap["connection_state"] == "disconnected"
        assert snap["last_error"] == "socket EOF"

    def test_connected_does_not_overwrite_last_error(self, monitor, bus):
        bus.publish(TOPIC_CONNECTION_STATE,
                    {"provider": "zerodha", "state": "degraded",
                     "detail": "no heartbeat"})
        bus.publish(TOPIC_CONNECTION_STATE,
                    {"provider": "zerodha", "state": "connected",
                     "detail": None})
        snap = monitor.snapshot()
        assert snap["connection_state"] == "connected"
        # Historical error is preserved for diagnostics.
        assert snap["last_error"] == "no heartbeat"

    def test_state_payload_can_be_string(self, monitor, bus):
        bus.publish(TOPIC_CONNECTION_STATE, "connected")
        snap = monitor.snapshot()
        assert snap["connection_state"] == "connected"

    def test_state_change_handler_swallows_exceptions(self, monitor, bus):
        # A dict subclass whose .get() raises — exercises the except path.
        class _BadDict(dict):
            def get(self, k, d=None):
                raise RuntimeError("boom")
        bus.publish(TOPIC_CONNECTION_STATE, _BadDict())  # must not raise
        # State should remain at the prior value (unknown).
        assert monitor.snapshot()["connection_state"] == "unknown"

    def test_state_uses_provider_from_payload(self, monitor, bus):
        bus.publish(TOPIC_CONNECTION_STATE,
                    {"provider": "test_provider", "state": "connected"})
        ev = [e for e in monitor.snapshot()["recent_events"]
              if e["topic"] == "connection_state"][0]
        assert ev["provider"] == "test_provider"

    def test_state_falls_back_to_default_provider(self, monitor, bus):
        bus.publish(TOPIC_CONNECTION_STATE, {"state": "connected"})
        ev = [e for e in monitor.snapshot()["recent_events"]
              if e["topic"] == "connection_state"][0]
        assert ev["provider"] == "zerodha"


# ---------------------------------------------------------------------------
# Token expired edge cases
# ---------------------------------------------------------------------------
class TestTokenExpiredEdgeCases:
    def test_token_expired_handler_swallows_bad_payload(self, monitor, bus):
        class _BadDict(dict):
            def get(self, k, d=None):
                raise RuntimeError("boom")
        # Should still set token_expired=True without raising.
        bus.publish(TOPIC_TOKEN_EXPIRED, _BadDict())
        assert monitor.snapshot()["token_expired"] is True

    def test_token_expired_records_event_with_provider(self, monitor, bus):
        bus.publish(TOPIC_TOKEN_EXPIRED, {"provider": "zerodha"})
        ev = [e for e in monitor.snapshot()["recent_events"]
              if e["topic"] == "token_expired"][0]
        assert ev["provider"] == "zerodha"


# ---------------------------------------------------------------------------
# Atomic snapshot file writes
# ---------------------------------------------------------------------------
class TestAtomicWrites:
    def test_writer_thread_writes_file_periodically(self, tmp_path, bus, clock):
        path = tmp_path / "ws.json"
        m = WSMonitor(snapshot_path=path, event_bus=bus, clock=clock,
                      snapshot_interval_seconds=0.05)
        m.start()
        try:
            # Wait for at least one writer cycle.
            import time
            for _ in range(30):
                if path.exists():
                    break
                time.sleep(0.05)
        finally:
            m.stop()
        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["provider"] == "zerodha"

    def test_no_temp_files_left_in_directory(self, tmp_path, bus, clock):
        path = tmp_path / "ws.json"
        m = WSMonitor(snapshot_path=path, event_bus=bus, clock=clock,
                      snapshot_interval_seconds=60.0)
        m.start()
        m.stop()
        leftovers = list(tmp_path.glob(".ws_status_*"))
        assert leftovers == []

    def test_creates_parent_directory(self, tmp_path, bus, clock):
        path = tmp_path / "deep" / "nested" / "ws.json"
        m = WSMonitor(snapshot_path=path, event_bus=bus, clock=clock,
                      snapshot_interval_seconds=60.0)
        m.start()
        m.stop()
        assert path.exists()

    def test_snapshot_path_accepts_string(self, tmp_path, bus, clock):
        path_str = str(tmp_path / "ws.json")
        m = WSMonitor(snapshot_path=path_str, event_bus=bus, clock=clock,
                      snapshot_interval_seconds=60.0)
        m.start()
        m.stop()
        assert Path(path_str).exists()


# ---------------------------------------------------------------------------
# default_snapshot_path helper
# ---------------------------------------------------------------------------
class TestDefaultSnapshotPath:
    def test_returns_absolute_path(self):
        from providers.ws_monitor import default_snapshot_path
        p = default_snapshot_path()
        assert p.is_absolute()
        assert p.name == "ws_status.json"


# ---------------------------------------------------------------------------
# Concurrency smoke
# ---------------------------------------------------------------------------
class TestConcurrency:
    def test_concurrent_publishes_dont_lose_count(self, tmp_path, bus, clock):
        import threading
        m = WSMonitor(snapshot_path=tmp_path / "ws.json", event_bus=bus,
                      clock=clock, snapshot_interval_seconds=60.0,
                      max_recent_events=10000)
        m.start()
        N_THREADS, PER = 4, 250

        def _producer():
            for _ in range(PER):
                bus.publish(TOPIC_TICK, _make_quote("NIFTY"))

        threads = [threading.Thread(target=_producer) for _ in range(N_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        m.stop()
        assert m.snapshot()["tick_count_total"] == N_THREADS * PER


# ---------------------------------------------------------------------------
# _iso_or_none / _seconds_delta helpers
# ---------------------------------------------------------------------------
class TestHelpers:
    def test_iso_or_none_with_none(self):
        from providers.ws_monitor import _iso_or_none
        assert _iso_or_none(None) is None

    def test_iso_or_none_with_datetime(self):
        from providers.ws_monitor import _iso_or_none
        dt = datetime(2025, 4, 1, 9, 30, tzinfo=timezone.utc)
        assert _iso_or_none(dt) == dt.isoformat()

    def test_iso_or_none_with_non_datetime_falls_back_to_str(self):
        from providers.ws_monitor import _iso_or_none
        assert _iso_or_none("already-a-string") == "already-a-string"
        assert _iso_or_none(12345) == "12345"

    def test_seconds_delta_returns_timedelta(self):
        from providers.ws_monitor import _seconds_delta
        td = _seconds_delta(60.0)
        assert td == timedelta(seconds=60)


# ---------------------------------------------------------------------------
# Defensive paths
# ---------------------------------------------------------------------------
class TestDefensivePaths:
    def test_unsubscribe_failure_during_stop_is_swallowed(
        self, tmp_path, bus, clock, mocker
    ):
        m = WSMonitor(snapshot_path=tmp_path / "ws.json", event_bus=bus,
                      clock=clock, snapshot_interval_seconds=60.0)
        m.start()
        # Replace one of the unsubscribe callables with a raising one.
        m._unsubs[0] = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        # stop() must still complete cleanly.
        m.stop()
        assert m._started is False

    def test_final_snapshot_write_failure_is_swallowed(
        self, tmp_path, bus, clock, mocker
    ):
        m = WSMonitor(snapshot_path=tmp_path / "ws.json", event_bus=bus,
                      clock=clock, snapshot_interval_seconds=60.0)
        m.start()
        # Patch _write_snapshot on this instance to raise during stop's final write.
        mocker.patch.object(m, "_write_snapshot",
                            side_effect=RuntimeError("disk full"))
        m.stop()  # must not raise
        assert m._started is False

    def test_writer_loop_swallows_periodic_write_errors(
        self, tmp_path, bus, clock, mocker
    ):
        import time
        m = WSMonitor(snapshot_path=tmp_path / "ws.json", event_bus=bus,
                      clock=clock, snapshot_interval_seconds=0.05)
        # First call (initial) raises; subsequent calls also raise.
        # Writer thread must keep looping despite this.
        mocker.patch.object(m, "_write_snapshot",
                            side_effect=RuntimeError("disk error"))
        m.start()
        try:
            time.sleep(0.2)  # let writer loop iterate at least twice
        finally:
            m.stop()
        # The mock should have been called multiple times (initial + periodic).
        assert m._write_snapshot.call_count >= 2

    def test_write_snapshot_cleans_tmpfile_on_failure(
        self, tmp_path, bus, clock, mocker
    ):
        m = WSMonitor(snapshot_path=tmp_path / "ws.json", event_bus=bus,
                      clock=clock, snapshot_interval_seconds=60.0)
        # Force os.replace to fail so the tmpfile cleanup path runs.
        mocker.patch("providers.ws_monitor.os.replace",
                     side_effect=OSError("rename failed"))
        with pytest.raises(OSError):
            m._write_snapshot()
        # No leftover tmpfiles.
        assert list(tmp_path.glob(".ws_status_*")) == []


# ---------------------------------------------------------------------------
# Recent events ordering
# ---------------------------------------------------------------------------
class TestRecentEventOrdering:
    def test_events_appended_in_chronological_order(self, monitor, bus, clock):
        bus.publish(TOPIC_TICK, _make_quote("A"))
        clock.advance(1)
        bus.publish(TOPIC_CONNECTION_STATE,
                    {"provider": "zerodha", "state": "connected"})
        clock.advance(1)
        bus.publish(TOPIC_TICK, _make_quote("B"))
        events = monitor.snapshot()["recent_events"]
        # Snapshot returns oldest-first.
        symbols_or_topics = [e.get("symbol", e.get("topic")) for e in events]
        assert symbols_or_topics == ["A", "connection_state", "B"]
