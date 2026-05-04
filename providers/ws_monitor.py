"""
providers/ws_monitor.py
=======================

Lightweight, cross-process telemetry for the Zerodha WebSocket runner.

Why this module exists
----------------------
The dashboard runs in a different container from `ws_runner`, so it cannot
subscribe to the in-process `EventBus` directly. `WSMonitor` solves that:

1. Subscribes to `TOPIC_TICK` / `TOPIC_CONNECTION_STATE` / `TOPIC_TOKEN_EXPIRED`
   *inside the ws_runner process*. Counts and a ring-buffer of recent events
   are kept in memory only — no DB writes per tick, no extra Zerodha calls.
2. A background writer atomically snapshots the in-memory state to
   `data/ws_status.json` at a configurable cadence (default 1 s).
3. The dashboard reads that JSON via `/api/ws/monitor` — a pure file read,
   safe to call as often as the UI wants.

Auto-pruning
------------
Recent events older than `event_retention_seconds` (default 300) are dropped
on every snapshot tick, and the ring buffer is hard-capped at
`max_recent_events` (default 200). This keeps the JSON small even on busy
days and matches the user's "live logs that auto-delete" expectation.

This module performs NO Zerodha API calls. It only observes events the
runner is already publishing.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional

from providers.event_bus import (
    EventBus,
    TOPIC_CONNECTION_STATE,
    TOPIC_TICK,
    TOPIC_TOKEN_EXPIRED,
    get_event_bus,
)

logger = logging.getLogger(__name__)


# Hard limits so a busy market session never balloons the snapshot file.
_DEFAULT_MAX_EVENTS = 200
_DEFAULT_EVENT_RETENTION_SECONDS = 300.0      # 5 min
_DEFAULT_RATE_WINDOW_SECONDS = 60.0           # rolling tick rate
_DEFAULT_SNAPSHOT_INTERVAL_SECONDS = 0.5
_DEFAULT_PROVIDER = "zerodha"


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


class WSMonitor:
    """Telemetry collector for a single WebSocket provider.

    Parameters
    ----------
    snapshot_path:
        File the snapshot writer atomically writes to. Read by the dashboard.
    event_bus:
        Optional `EventBus` (defaults to the process-wide singleton).
    provider:
        Stamped into every snapshot so the dashboard can show "third party = zerodha".
    status_fn:
        Optional zero-arg callable returning a `WSStatus`-like object — usually
        the bound method `KiteWSRunner.status`. Lets the snapshot include
        connection state, subscribed token count, last error, etc.
    max_recent_events / event_retention_seconds / rate_window_seconds /
    snapshot_interval_seconds:
        See module docstring.
    clock:
        Override the wall-clock for tests.
    """

    def __init__(
        self,
        snapshot_path: Path | str,
        *,
        event_bus: Optional[EventBus] = None,
        provider: str = _DEFAULT_PROVIDER,
        status_fn: Optional[Callable[[], Any]] = None,
        max_recent_events: int = _DEFAULT_MAX_EVENTS,
        event_retention_seconds: float = _DEFAULT_EVENT_RETENTION_SECONDS,
        rate_window_seconds: float = _DEFAULT_RATE_WINDOW_SECONDS,
        snapshot_interval_seconds: float = _DEFAULT_SNAPSHOT_INTERVAL_SECONDS,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        if max_recent_events <= 0:
            raise ValueError("max_recent_events must be positive")
        if event_retention_seconds <= 0:
            raise ValueError("event_retention_seconds must be positive")
        if rate_window_seconds <= 0:
            raise ValueError("rate_window_seconds must be positive")
        if snapshot_interval_seconds <= 0:
            raise ValueError("snapshot_interval_seconds must be positive")

        self._snapshot_path = Path(snapshot_path)
        self._bus = event_bus
        self._provider = provider
        self._status_fn = status_fn
        self._max_events = int(max_recent_events)
        self._retention = float(event_retention_seconds)
        self._rate_window = float(rate_window_seconds)
        self._snapshot_interval = float(snapshot_interval_seconds)
        self._clock = clock

        self._lock = threading.RLock()
        self._started_at = self._clock()
        self._tick_count_total = 0
        self._tick_count_by_symbol: Dict[str, int] = {}
        self._tick_timestamps: Deque[float] = deque()  # epoch seconds, for rolling rate
        self._last_tick_at: Optional[datetime] = None
        self._last_state_change_at: Optional[datetime] = None
        self._connection_state: str = "unknown"
        self._last_error: Optional[str] = None
        self._token_expired: bool = False
        self._recent: Deque[Dict[str, Any]] = deque(maxlen=self._max_events)

        self._unsubs: List[Callable[[], None]] = []
        self._writer_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._started = False

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._started:
            return
        bus = self._bus or get_event_bus()
        self._bus = bus
        self._unsubs.append(bus.subscribe(TOPIC_TICK, self._on_tick))
        self._unsubs.append(bus.subscribe(TOPIC_CONNECTION_STATE, self._on_state))
        self._unsubs.append(bus.subscribe(TOPIC_TOKEN_EXPIRED, self._on_token_expired))

        self._writer_thread = threading.Thread(
            target=self._writer_loop,
            name="ws-monitor-writer",
            daemon=True,
        )
        self._writer_thread.start()
        self._started = True
        logger.info(
            "WSMonitor started — snapshot=%s, max_events=%d, retention=%.0fs",
            self._snapshot_path, self._max_events, self._retention,
        )

    def stop(self) -> None:
        for u in self._unsubs:
            try:
                u()
            except Exception:
                logger.exception("WSMonitor: unsubscribe failed")
        self._unsubs.clear()
        self._stop_event.set()
        if self._writer_thread is not None and self._writer_thread.is_alive():
            self._writer_thread.join(timeout=2.0)
        # Final snapshot so the dashboard sees the terminal state.
        try:
            self._write_snapshot()
        except Exception:
            logger.exception("WSMonitor: final snapshot write failed")
        self._started = False

    # ------------------------------------------------------------------ event handlers
    def _on_tick(self, payload: Any) -> None:
        """`payload` is a `LiveQuote`. Never raise — runner thread depends on us."""
        try:
            now = self._clock()
            symbol = str(getattr(payload, "symbol", "") or "?")
            opt = getattr(payload, "option_type", None)
            strike = getattr(payload, "strike", None)
            last_price = getattr(payload, "last_price", None)
            with self._lock:
                self._tick_count_total += 1
                self._tick_count_by_symbol[symbol] = (
                    self._tick_count_by_symbol.get(symbol, 0) + 1
                )
                self._last_tick_at = now
                self._tick_timestamps.append(now.timestamp())
                self._prune_rate_window_locked(now)
                self._recent.append({
                    "ts":          now.isoformat(),
                    "topic":       "tick",
                    "symbol":      symbol,
                    "option_type": opt,
                    "strike":      float(strike) if strike is not None else None,
                    "last_price":  float(last_price) if last_price is not None else None,
                })
        except Exception:
            logger.exception("WSMonitor._on_tick swallowed exception")

    def _on_state(self, payload: Any) -> None:
        try:
            now = self._clock()
            state = str(payload.get("state", "")) if isinstance(payload, dict) else str(payload)
            detail = payload.get("detail") if isinstance(payload, dict) else None
            provider = (payload.get("provider") if isinstance(payload, dict) else None) or self._provider
            with self._lock:
                self._connection_state = state or self._connection_state
                self._last_state_change_at = now
                if state in ("disconnected", "degraded") and detail:
                    self._last_error = str(detail)
                self._recent.append({
                    "ts":       now.isoformat(),
                    "topic":    "connection_state",
                    "provider": provider,
                    "state":    state,
                    "detail":   detail,
                })
        except Exception:
            logger.exception("WSMonitor._on_state swallowed exception")

    def _on_token_expired(self, payload: Any) -> None:
        try:
            now = self._clock()
            with self._lock:
                self._token_expired = True
                self._connection_state = "token_expired"
                self._last_state_change_at = now
                self._recent.append({
                    "ts":       now.isoformat(),
                    "topic":    "token_expired",
                    "provider": (payload.get("provider")
                                 if isinstance(payload, dict) else self._provider),
                })
        except Exception:
            logger.exception("WSMonitor._on_token_expired swallowed exception")

    # ------------------------------------------------------------------ snapshot
    def snapshot(self) -> Dict[str, Any]:
        """Return a JSON-serialisable snapshot. Safe to call from any thread."""
        now = self._clock()
        with self._lock:
            self._prune_rate_window_locked(now)
            self._prune_recent_locked(now)
            tick_rate = len(self._tick_timestamps) / self._rate_window
            top_symbols = sorted(
                self._tick_count_by_symbol.items(),
                key=lambda kv: kv[1],
                reverse=True,
            )[:20]
            status_extra: Dict[str, Any] = {}
            if self._status_fn is not None:
                try:
                    s = self._status_fn()
                    status_extra = {
                        "runner_state":          getattr(getattr(s, "state", None), "value", None)
                                                  or str(getattr(s, "state", "") or ""),
                        "subscribed_tokens":     getattr(s, "subscribed_tokens", None),
                        "reconnect_attempts":    getattr(s, "reconnect_attempts", None),
                        "watchdog_resets_window": getattr(s, "watchdog_resets_window", None),
                        "runner_last_tick_at":   _iso_or_none(getattr(s, "last_tick_at", None)),
                        "runner_last_error":     getattr(s, "last_error", None),
                        "failure_started_at":    _iso_or_none(getattr(s, "failure_started_at", None)),
                    }
                except Exception:
                    logger.exception("WSMonitor: status_fn raised")
            return {
                "provider":               self._provider,
                "generated_at":           now.isoformat(),
                "started_at":             self._started_at.isoformat(),
                "uptime_seconds":         (now - self._started_at).total_seconds(),
                "connection_state":       self._connection_state,
                "token_expired":          self._token_expired,
                "last_tick_at":           _iso_or_none(self._last_tick_at),
                "last_state_change_at":   _iso_or_none(self._last_state_change_at),
                "last_error":              self._last_error,
                "tick_count_total":       self._tick_count_total,
                "tick_rate_per_sec":      round(tick_rate, 3),
                "rate_window_seconds":    self._rate_window,
                "top_symbols":            [
                    {"symbol": s, "ticks": n} for s, n in top_symbols
                ],
                "recent_events":          list(self._recent),
                "max_recent_events":      self._max_events,
                "event_retention_seconds": self._retention,
                **status_extra,
            }

    # ------------------------------------------------------------------ writer thread
    def _writer_loop(self) -> None:
        # Write an immediate snapshot so the dashboard has data on cold start.
        try:
            self._write_snapshot()
        except Exception:
            logger.exception("WSMonitor: initial snapshot write failed")
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=self._snapshot_interval)
            if self._stop_event.is_set():
                break
            try:
                self._write_snapshot()
            except Exception:
                logger.exception("WSMonitor: snapshot write failed")

    def _write_snapshot(self) -> None:
        snap = self.snapshot()
        path = self._snapshot_path
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".ws_status_", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(snap, f, default=str)
            os.replace(tmp_name, path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------------ pruning
    def _prune_rate_window_locked(self, now: datetime) -> None:
        cutoff = now.timestamp() - self._rate_window
        while self._tick_timestamps and self._tick_timestamps[0] < cutoff:
            self._tick_timestamps.popleft()

    def _prune_recent_locked(self, now: datetime) -> None:
        cutoff_iso = (now - _seconds_delta(self._retention)).isoformat()
        # `deque` doesn't support efficient front-prune-while-condition without
        # popleft loop — recent items live at the right.
        while self._recent and self._recent[0].get("ts", "") < cutoff_iso:
            self._recent.popleft()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _iso_or_none(dt: Any) -> Optional[str]:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.isoformat()
    return str(dt)


def _seconds_delta(seconds: float):
    from datetime import timedelta
    return timedelta(seconds=seconds)


def default_snapshot_path() -> Path:
    """Default location for the JSON snapshot. Honours `OPT_DATA_DIR`."""
    from config import PATHS
    data_dir = Path(PATHS.get("data_dir", "data"))
    if not data_dir.is_absolute():
        data_dir = Path(__file__).resolve().parents[1] / data_dir
    return data_dir / "ws_status.json"
