"""
providers/ws_watchdog.py
========================

Phase 3 — #7: dead-man WebSocket watchdog.

During market hours, periodically checks how long since the WS runner
last produced a tick. If the gap exceeds ``stale_threshold_sec`` we
fire ONE ``WS_DEAD_MAN`` CRITICAL notification (and publish
``TOPIC_WS_STALE``). The watchdog re-arms only after a recovery —
i.e. a fresh tick comes through and clears the stale state — so we
don't spam alerts every check interval while the runner is down.

This module owns NO state about Zerodha connections itself. It is a
pure observer over ``WSMonitor.snapshot()`` (or any equivalent
callable returning ``last_tick_at`` as a UTC datetime).

Usage (production wiring in main.py):
    monitor = WSMonitor(...)
    monitor.start()
    watchdog = WSWatchdog(
        snapshot_fn=monitor.snapshot,
        notifier=notifier,
    )
    watchdog.start()
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, time as dtime
from typing import Any, Callable, Dict, Optional

from config import STRATEGY_CONFIG
from providers.event_bus import EventBus, get_event_bus
from utils import now_ist

logger = logging.getLogger(__name__)


# Topic published when the WS feed crosses the staleness threshold during
# market hours. Listeners (e.g. a kill-switch arbiter) can subscribe.
TOPIC_WS_STALE = "ws_stale"


def _parse_hhmm(s: str) -> dtime:
    h, m = (int(x) for x in str(s).split(":"))
    return dtime(hour=h, minute=m)


def _in_market_session(now: datetime, start: str, end: str) -> bool:
    """True iff ``now`` falls within [start, end] on a weekday (Mon–Fri)."""
    if now.weekday() >= 5:  # 5=Sat, 6=Sun
        return False
    t0 = _parse_hhmm(start)
    t1 = _parse_hhmm(end)
    return t0 <= now.time() <= t1


@dataclass
class _State:
    last_seen_tick_at: Optional[datetime] = None
    stale_fired:        bool = False
    last_alert_at:      Optional[datetime] = None


class WSWatchdog:
    """Background thread polling a snapshot fn for tick staleness."""

    def __init__(
        self,
        snapshot_fn: Callable[[], Dict[str, Any]],
        notifier,
        *,
        stale_threshold_sec: Optional[float] = None,
        check_interval_sec:  Optional[float] = None,
        session_start:       Optional[str] = None,
        session_end:         Optional[str] = None,
        clock:               Callable[[], datetime] = now_ist,
        event_bus:           Optional[EventBus] = None,
    ):
        cfg = STRATEGY_CONFIG.get("ws_watchdog", {}) or {}
        self._snapshot_fn = snapshot_fn
        self._notifier    = notifier
        self._clock       = clock
        self._stale       = float(
            stale_threshold_sec
            if stale_threshold_sec is not None
            else cfg.get("stale_threshold_sec", 60.0)
        )
        self._interval = float(
            check_interval_sec
            if check_interval_sec is not None
            else cfg.get("check_interval_sec", 30.0)
        )
        if self._stale <= 0 or self._interval <= 0:
            raise ValueError("ws_watchdog thresholds must be positive")
        self._session_start = str(
            session_start if session_start is not None
            else cfg.get("session_start", "09:15")
        )
        self._session_end = str(
            session_end if session_end is not None
            else cfg.get("session_end", "15:30")
        )
        self._bus = event_bus
        self._state = _State()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ thread
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, name="WSWatchdog", daemon=True,
        )
        self._thread.start()
        logger.info(
            "WSWatchdog started (stale=%.0fs, interval=%.0fs, session=%s..%s)",
            self._stale, self._interval, self._session_start, self._session_end,
        )

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.check_once()
            except Exception:
                logger.exception("WSWatchdog: check_once swallowed exception")
            self._stop_event.wait(timeout=self._interval)

    # ------------------------------------------------------------------ logic
    def check_once(self) -> Optional[str]:
        """Run one staleness check. Returns one of ``"stale"``,
        ``"recovered"`` or ``None`` (no transition). Pure function over
        the injected snapshot + clock — safe to unit-test."""
        now = self._clock()
        if not _in_market_session(now, self._session_start, self._session_end):
            return None
        snap = self._snapshot_fn() or {}
        last_tick_iso = snap.get("last_tick_at")
        last_tick_dt = _parse_iso(last_tick_iso) if last_tick_iso else None

        # Normalise tz so the subtraction never raises. Either side may
        # be naive depending on how the snapshot was written.
        if last_tick_dt is not None:
            if last_tick_dt.tzinfo is None and now.tzinfo is not None:
                last_tick_dt = last_tick_dt.replace(tzinfo=now.tzinfo)
            elif last_tick_dt.tzinfo is not None and now.tzinfo is None:
                now = now.replace(tzinfo=last_tick_dt.tzinfo)

        if last_tick_dt is None:
            age = float("inf")
        else:
            age = max(0.0, (now - last_tick_dt).total_seconds())

        if age > self._stale:
            if self._state.stale_fired:
                return None  # already alerted; wait for recovery
            self._state.stale_fired = True
            self._state.last_alert_at = now
            self._fire_dead_man(age, snap)
            return "stale"

        # Tick is fresh again.
        if self._state.stale_fired:
            self._state.stale_fired = False
            logger.info("WSWatchdog: feed recovered; tick age %.1fs", age)
            return "recovered"
        return None

    # ------------------------------------------------------------------ dispatch
    def _fire_dead_man(self, age_sec: float, snap: dict) -> None:
        title = "WS feed dead-man — no ticks for %.0fs" % age_sec
        body = (
            f"WebSocket runner has not delivered a tick for {age_sec:.0f}s "
            f"(threshold {self._stale:.0f}s). Live monitoring (LiveRiskMonitor, "
            f"OpportunityRegenWatcher) is now operating on stale data. "
            f"Investigate the ws_runner container/process. "
            f"connection_state={snap.get('connection_state')!r} "
            f"subscribed={snap.get('subscribed_count')} "
            f"reconnects={snap.get('reconnect_attempts')}"
        )
        try:
            self._notifier.notify(
                "WS_DEAD_MAN",
                "CRITICAL",
                title,
                body,
                bypass_flags=True,
            )
        except Exception:
            logger.exception("WSWatchdog: notifier raised on WS_DEAD_MAN")
        try:
            (self._bus or get_event_bus()).publish(TOPIC_WS_STALE, {
                "age_sec": age_sec,
                "snapshot": snap,
            })
        except Exception:
            logger.exception("WSWatchdog: event_bus publish failed")


def _parse_iso(s: str) -> Optional[datetime]:
    try:
        # Python's fromisoformat handles offsets like '+05:30'.
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None
