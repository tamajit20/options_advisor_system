"""
providers/event_bus.py
======================

Tiny in-process pub/sub bus.

Used by:
    - `providers/zerodha/ws_runner.py` (publisher) — emits `tick` and
      `connection_state` events as ticks arrive over the WebSocket.
    - `lifecycle/intraday_monitor.py` (subscriber) — consumes ticks and
      decides whether to fire SL_TRIGGER / PERFECT_ENTRY / PERFECT_CLOSURE
      notifications (Phase 2b).

Design:
    - Synchronous dispatch (subscriber callbacks run on the publisher's thread).
      Subscribers must return quickly — heavy work should be queued.
    - One `EventBus` instance per process; module-level singleton via
      `get_event_bus()` for convenience.
    - Topics are arbitrary strings; we use enum-style constants below for
      well-known events to avoid typos.
    - Safe to call from multiple threads.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict, List, Optional


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Well-known topics
# ---------------------------------------------------------------------------
TOPIC_TICK = "tick"                          # payload: LiveQuote
TOPIC_CONNECTION_STATE = "connection_state"  # payload: {"provider": str, "state": "connected"/"disconnected"/"degraded", "detail": str}
TOPIC_TOKEN_EXPIRED = "token_expired"        # payload: {"provider": str}
TOPIC_TRADE_OPENED = "trade_opened"          # payload: {"trade_id": str}
TOPIC_TRADE_CLOSED = "trade_closed"          # payload: {"trade_id": str}
TOPIC_TRADE_MTM = "trade_mtm"                # payload: {"trade_id": str, "mtm": float, "dte": int, "breach_state": str|None, "as_of": "iso"}


Handler = Callable[[Any], None]


class EventBus:
    """Synchronous in-process pub/sub bus."""

    def __init__(self) -> None:
        self._subs: Dict[str, List[Handler]] = {}
        self._lock = threading.RLock()

    def subscribe(self, topic: str, handler: Handler) -> Callable[[], None]:
        """Register `handler` for `topic`. Returns an unsubscribe function."""
        if not callable(handler):
            raise TypeError("handler must be callable")
        with self._lock:
            self._subs.setdefault(topic, []).append(handler)

        def _unsubscribe() -> None:
            with self._lock:
                if topic in self._subs:
                    try:
                        self._subs[topic].remove(handler)
                    except ValueError:
                        pass
                    if not self._subs[topic]:
                        self._subs.pop(topic, None)

        return _unsubscribe

    def publish(self, topic: str, payload: Any) -> int:
        """Dispatch `payload` to all subscribers of `topic`. Returns the
        number of handlers invoked. Exceptions inside handlers are caught
        and logged so one bad subscriber cannot break delivery to others."""
        with self._lock:
            handlers = list(self._subs.get(topic, ()))
        if not handlers:
            return 0
        for h in handlers:
            try:
                h(payload)
            except Exception:
                logger.exception("event_bus handler raised on topic=%s", topic)
        return len(handlers)

    def clear(self) -> None:
        with self._lock:
            self._subs.clear()

    def subscriber_count(self, topic: Optional[str] = None) -> int:
        with self._lock:
            if topic is None:
                return sum(len(hs) for hs in self._subs.values())
            return len(self._subs.get(topic, ()))


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
_BUS_LOCK = threading.Lock()
_BUS_INSTANCE: Optional[EventBus] = None


def get_event_bus() -> EventBus:
    """Return the process-wide event bus singleton."""
    global _BUS_INSTANCE
    if _BUS_INSTANCE is None:
        with _BUS_LOCK:
            if _BUS_INSTANCE is None:
                _BUS_INSTANCE = EventBus()
    return _BUS_INSTANCE


def reset_event_bus() -> None:
    """Test-only: drop the singleton so a clean instance is created next call."""
    global _BUS_INSTANCE
    with _BUS_LOCK:
        _BUS_INSTANCE = None
