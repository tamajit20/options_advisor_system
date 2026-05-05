"""Tests for providers/ws_watchdog.py (Phase 3 — #7)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict

import pytest

from providers.ws_watchdog import WSWatchdog, _in_market_session


_IST = timezone(timedelta(hours=5, minutes=30))


class _StubNotifier:
    def __init__(self):
        self.events: list[dict] = []

    def notify(self, notif_type, severity, title, body="", **kw):
        self.events.append({"type": notif_type, "severity": severity,
                            "title": title, "body": body, "kw": kw})


def _make(*, last_tick_age_sec, weekday=0, hhmm=(10, 0), stale=60.0):
    base_day = datetime(2026, 5, 4, tzinfo=_IST)  # Monday
    base_day = base_day + timedelta(days=weekday)
    now = base_day.replace(hour=hhmm[0], minute=hhmm[1])
    if last_tick_age_sec is None:
        last_tick = None
    else:
        last_tick = (now - timedelta(seconds=last_tick_age_sec)).isoformat()
    snap: Dict[str, Any] = {
        "last_tick_at":     last_tick,
        "connection_state": "connected",
        "subscribed_count": 12,
        "reconnect_attempts": 0,
    }
    notif = _StubNotifier()
    w = WSWatchdog(
        snapshot_fn=lambda: snap,
        notifier=notif,
        stale_threshold_sec=stale,
        check_interval_sec=10,
        clock=lambda: now,
    )
    return w, notif, snap


class TestSessionGate:
    def test_outside_session_no_check(self):
        w, notif, _ = _make(last_tick_age_sec=999, hhmm=(8, 0))
        assert w.check_once() is None
        assert notif.events == []

    def test_after_close_no_check(self):
        w, notif, _ = _make(last_tick_age_sec=999, hhmm=(16, 0))
        assert w.check_once() is None
        assert notif.events == []

    def test_weekend_no_check(self):
        # weekday=5 → Saturday
        w, notif, _ = _make(last_tick_age_sec=999, weekday=5)
        assert w.check_once() is None
        assert notif.events == []

    def test_in_session_active(self):
        now = datetime(2026, 5, 4, 10, 0, tzinfo=_IST)
        assert _in_market_session(now, "09:15", "15:30") is True


class TestStaleDetection:
    def test_fresh_tick_no_alert(self):
        w, notif, _ = _make(last_tick_age_sec=10)
        assert w.check_once() is None
        assert notif.events == []

    def test_stale_fires_critical(self):
        w, notif, _ = _make(last_tick_age_sec=120)  # > 60
        assert w.check_once() == "stale"
        assert len(notif.events) == 1
        e = notif.events[0]
        assert e["type"] == "WS_DEAD_MAN"
        assert e["severity"] == "CRITICAL"
        assert e["kw"].get("bypass_flags") is True

    def test_missing_last_tick_treated_as_stale(self):
        w, notif, _ = _make(last_tick_age_sec=None)
        assert w.check_once() == "stale"
        assert len(notif.events) == 1

    def test_one_alert_per_incident(self):
        w, notif, _ = _make(last_tick_age_sec=120)
        w.check_once()
        w.check_once()
        w.check_once()
        assert len(notif.events) == 1  # second/third checks suppressed

    def test_recovery_rearms_watchdog(self, mocker):
        # Build watchdog manually so we can mutate the snapshot.
        snap = {"last_tick_at": None, "connection_state": "down"}
        now = {"v": datetime(2026, 5, 4, 10, 0, tzinfo=_IST)}
        notif = _StubNotifier()
        w = WSWatchdog(
            snapshot_fn=lambda: snap,
            notifier=notif,
            stale_threshold_sec=60,
            check_interval_sec=10,
            clock=lambda: now["v"],
        )
        # Stale fires.
        assert w.check_once() == "stale"
        # Tick recovers.
        snap["last_tick_at"] = (now["v"] - timedelta(seconds=5)).isoformat()
        assert w.check_once() == "recovered"
        # Goes stale again later.
        snap["last_tick_at"] = (now["v"] - timedelta(seconds=300)).isoformat()
        assert w.check_once() == "stale"
        assert len(notif.events) == 2


class TestConstruction:
    def test_zero_threshold_rejected(self):
        with pytest.raises(ValueError):
            WSWatchdog(snapshot_fn=lambda: {}, notifier=_StubNotifier(),
                       stale_threshold_sec=0, check_interval_sec=10)

    def test_negative_interval_rejected(self):
        with pytest.raises(ValueError):
            WSWatchdog(snapshot_fn=lambda: {}, notifier=_StubNotifier(),
                       stale_threshold_sec=60, check_interval_sec=-1)
