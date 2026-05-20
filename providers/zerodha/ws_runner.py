"""
providers/zerodha/ws_runner.py
==============================

Long-running WebSocket runner that streams Zerodha live ticks into our
in-process cache + event bus.

Lifecycle
---------
This module is loaded by `python main.py --ws-runner` (Phase 2b-iv adds the
docker-compose `stock_ws_runner` service that runs that command). It is
NEVER imported by the dashboard or scheduler containers — only one WS
connection is permitted per Kite api_key, and process-level isolation
prevents accidental dual-connect.

Singleton enforcement
---------------------
A module-level `_TICKER_INSTANCE` guard raises `RuntimeError` if anyone
tries to construct a second `KiteWSRunner` in the same process. Tests can
clear via `_reset_singleton_for_tests()`.

What it does
------------
1. Builds a `KiteTicker` against the saved daily access_token.
2. Subscribes to a caller-supplied set of `instrument_token` ints (Phase
   2b-ii will replace the static set with a DB-driven recompute loop).
3. On every tick → updates the shared `TTLCache` and publishes a
   `LiveQuote` on `event_bus` topic `TOPIC_TICK`.
4. On disconnect → exponential backoff reconnect (1, 2, 4, 8, 16, 32, 60s
   capped). After 5 min of continuous failure → publishes
   `connection_state=degraded` so downstream code can fall back to REST.
5. On `TokenException` (HTTP 403) → publishes `TOPIC_TOKEN_EXPIRED`,
   stops the runner, exits with code 2 (Docker restart policy can decide
   what to do — for our setup, we exit and rely on the user clicking
   "Re-login" on the dashboard).
6. Heartbeat watchdog: if no tick for `heartbeat_timeout` seconds during
   market hours → force reconnect once. 3 watchdog hits in 5 min →
   degraded.
7. Graceful shutdown on SIGTERM/SIGINT: unsubscribe → close → exit 0.

What it does NOT do
-------------------
- No order placement, no portfolio reads — only the `KiteTicker` streaming
  surface is used. The forbidden REST methods aren't even imported.
- No DB writes for ticks. Raw ticks live in memory only (`TTLCache`).
- No subscription discovery from DB (yet). 2b-i takes a static token list
  passed at startup; 2b-ii adds the dynamic recompute loop.
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Iterable, List, Optional, Sequence, Set

from providers.base import DataSource, LiveQuote
from providers.cache import TTLCache
from providers.event_bus import (
    EventBus,
    TOPIC_CONNECTION_STATE,
    TOPIC_TICK,
    TOPIC_TOKEN_EXPIRED,
    get_event_bus,
)


logger = logging.getLogger(__name__)


# Backoff schedule (seconds). Capped at 60s thereafter.
_BACKOFF_SCHEDULE: Sequence[float] = (1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0)
# After this much continuous-failure wall-time → publish degraded state.
_DEGRADED_AFTER_SECONDS = 5 * 60.0
# Watchdog: no-tick window during market hours that triggers a forced reconnect.
_DEFAULT_HEARTBEAT_TIMEOUT = 30.0
# Max watchdog-triggered reconnects in 5 min before degrading.
_WATCHDOG_DEGRADE_THRESHOLD = 3
_WATCHDOG_WINDOW_SECONDS = 5 * 60.0


# ---------------------------------------------------------------------------
# Singleton guard
# ---------------------------------------------------------------------------
_TICKER_INSTANCE: Optional["KiteWSRunner"] = None
_TICKER_LOCK = threading.Lock()


def _reset_singleton_for_tests() -> None:
    """Test-only helper. Production code must NEVER call this."""
    global _TICKER_INSTANCE
    with _TICKER_LOCK:
        _TICKER_INSTANCE = None


# ---------------------------------------------------------------------------
# Connection state enum (mirrored on event_bus payloads)
# ---------------------------------------------------------------------------
class ConnState(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DEGRADED = "degraded"            # repeated failures; downstream → REST fallback
    TOKEN_EXPIRED = "token_expired"  # terminal until re-login
    STOPPED = "stopped"              # graceful shutdown


@dataclass
class WSStatus:
    """Snapshot of runner state. Returned by `KiteWSRunner.status()`."""
    state: ConnState
    last_tick_at: Optional[datetime]
    last_error: Optional[str]
    subscribed_tokens: int
    reconnect_attempts: int
    failure_started_at: Optional[datetime]
    watchdog_resets_window: int = 0


# ---------------------------------------------------------------------------
# Tick parsing
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TokenMeta:
    """Minimal info needed to parse a tick into a `LiveQuote`. Populated by
    the caller via `set_token_meta()` so the runner doesn't have to load
    `InstrumentMaster` itself."""
    symbol: str
    expiry: Optional[datetime] = None
    strike: Optional[float] = None
    option_type: Optional[str] = None
    is_index: bool = False


# Backwards-compat private alias used internally.
_TokenMeta = TokenMeta


def _tick_to_quote(tick: dict, meta: Optional[_TokenMeta], provider_name: str) -> LiveQuote:
    """Convert a Kite tick dict to a `LiveQuote`.

    Kite tick shape (from kiteconnect.KiteTicker docs):
        - `instrument_token` (int)
        - `last_price` (float; rupees, the SDK already divides by 100)
        - `volume_traded` / `volume` (int) — instruments only
        - `oi` (int) — F&O only
        - `depth` -> `buy`/`sell` -> [{"price": ..., "quantity": ...}]
        - `exchange_timestamp` (datetime; full mode)
        - Index packets are smaller — no volume/oi/depth.
    """
    last_price = float(tick.get("last_price", 0.0) or 0.0)
    bid = ask = None
    depth = tick.get("depth")
    if depth:
        buy = depth.get("buy") or []
        sell = depth.get("sell") or []
        if buy:
            bid = float(buy[0].get("price", 0.0) or 0.0) or None
        if sell:
            ask = float(sell[0].get("price", 0.0) or 0.0) or None

    ts = tick.get("exchange_timestamp") or tick.get("timestamp")
    # Kite Connect sends exchange_timestamp / timestamp as naive IST datetimes —
    # do NOT tag them as UTC. Keep as naive IST to stay consistent with now_ist().
    if not isinstance(ts, datetime):
        ts = None
    elif ts.tzinfo is not None:
        # Rare: arrived timezone-aware — convert to naive IST.
        from datetime import timedelta as _td
        _IST = timezone(_td(hours=5, minutes=30))
        ts = ts.astimezone(_IST).replace(tzinfo=None)

    if meta is None:
        symbol = f"token:{tick.get('instrument_token', '?')}"
        expiry = strike = option_type = None
    else:
        symbol = meta.symbol
        expiry = meta.expiry.date() if isinstance(meta.expiry, datetime) else meta.expiry
        strike = meta.strike
        option_type = meta.option_type

    return LiveQuote(
        symbol=symbol,
        expiry=expiry,
        strike=strike,
        option_type=option_type,
        last_price=last_price,
        bid=bid,
        ask=ask,
        volume=tick.get("volume_traded") or tick.get("volume"),
        open_interest=tick.get("oi"),
        timestamp=ts,
        source=DataSource.LIVE,
        provider=provider_name,
        freshness_ms=0,
    )


# ---------------------------------------------------------------------------
# WS Runner
# ---------------------------------------------------------------------------
class KiteWSRunner:
    """Single-process WebSocket runner.

    Construction is enforced as a singleton — only one instance per process.

    Args:
        api_key, access_token: Kite credentials. Tests pass `ticker_factory`
            instead of relying on `kiteconnect`.
        cache:    shared `TTLCache` (`providers.cache`).
        event_bus: shared `EventBus` (default: process singleton).
        ticker_factory: zero-arg or `(api_key, access_token) -> KiteTickerLike`.
            If None, the real `kiteconnect.KiteTicker` is imported lazily.
        provider_name: stamped onto every `LiveQuote` as `.provider`.
        mode_full: subscribe in `full` mode (with depth) for option legs;
            `quote` mode for spot/VIX. We default to `full` so option ticks
            include bid/ask depth (no extra REST call needed).
        heartbeat_timeout: max no-tick window in seconds during market hours
            before forcing a reconnect.
        is_market_open_fn: callable returning True if NSE is currently open.
            Watchdog only fires during market hours (no-ticks pre/post-open
            is normal).
    """

    def __init__(
        self,
        *,
        api_key: str,
        access_token: str,
        cache: TTLCache,
        event_bus: Optional[EventBus] = None,
        ticker_factory: Optional[Callable[[str, str], Any]] = None,
        provider_name: str = "zerodha",
        mode_full: bool = True,
        heartbeat_timeout: float = _DEFAULT_HEARTBEAT_TIMEOUT,
        is_market_open_fn: Optional[Callable[[], bool]] = None,
    ):
        global _TICKER_INSTANCE
        with _TICKER_LOCK:
            if _TICKER_INSTANCE is not None:
                raise RuntimeError(
                    "KiteWSRunner is a process-level singleton; another "
                    "instance is already active. Only the ws_runner container "
                    "should construct one."
                )
            _TICKER_INSTANCE = self

        if not api_key or not access_token:
            raise ValueError("api_key and access_token are required")
        if heartbeat_timeout <= 0:
            raise ValueError("heartbeat_timeout must be positive")

        self._api_key = api_key
        self._access_token = access_token
        self._cache = cache
        self._event_bus = event_bus or get_event_bus()
        self._ticker_factory = ticker_factory
        self._provider_name = provider_name
        self._mode_full = mode_full
        self._heartbeat_timeout = float(heartbeat_timeout)
        self._is_market_open = is_market_open_fn or (lambda: True)

        self._ticker: Any = None
        self._stop_event = threading.Event()
        self._lock = threading.RLock()

        # Subscription state
        self._desired_tokens: Set[int] = set()
        self._subscribed_tokens: Set[int] = set()
        self._token_meta: dict[int, _TokenMeta] = {}

        # Health state
        self._state: ConnState = ConnState.DISCONNECTED
        self._last_tick_at: Optional[datetime] = None
        self._last_error: Optional[str] = None
        self._reconnect_attempts: int = 0
        self._failure_started_at: Optional[datetime] = None
        self._watchdog_events: deque[float] = deque(maxlen=16)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def set_token_meta(self, instrument_token: int, meta: _TokenMeta) -> None:
        """Register the symbol/expiry/strike/option_type metadata for a
        token so ticks can be converted to `LiveQuote` rows. Must be called
        BEFORE the token is added via `subscribe()` (subsequent ticks for
        unknown tokens are still cached but with placeholder symbols)."""
        with self._lock:
            self._token_meta[int(instrument_token)] = meta

    def desired_tokens(self) -> Set[int]:
        with self._lock:
            return set(self._desired_tokens)

    def subscribe(self, tokens: Iterable[int]) -> None:
        """Add tokens to the desired-set. Diff is applied to the live
        connection on the next reconcile (called automatically by the
        connect loop, or immediately if already connected)."""
        new = {int(t) for t in tokens}
        with self._lock:
            self._desired_tokens |= new
        self._reconcile_subscriptions()

    def unsubscribe(self, tokens: Iterable[int]) -> None:
        gone = {int(t) for t in tokens}
        with self._lock:
            self._desired_tokens -= gone
        self._reconcile_subscriptions()

    def replace_subscriptions(self, tokens: Iterable[int]) -> None:
        """Set the desired-set to exactly `tokens`. Used by the dynamic
        subscription manager (Phase 2b-ii)."""
        new = {int(t) for t in tokens}
        with self._lock:
            self._desired_tokens = new
        self._reconcile_subscriptions()

    def status(self) -> WSStatus:
        with self._lock:
            return WSStatus(
                state=self._state,
                last_tick_at=self._last_tick_at,
                last_error=self._last_error,
                subscribed_tokens=len(self._subscribed_tokens),
                reconnect_attempts=self._reconnect_attempts,
                failure_started_at=self._failure_started_at,
                watchdog_resets_window=len(self._watchdog_events),
            )

    def start(self) -> None:
        """Start the connect loop in the current thread. Returns when the
        runner has been stopped (via `stop()` or fatal token error)."""
        self._install_signal_handlers()
        self._connect_loop()

    def stop(self) -> None:
        """Request graceful shutdown. Safe to call from a signal handler."""
        self._stop_event.set()
        self._safe_close()
        self._set_state(ConnState.STOPPED, detail="stop() requested")

    # ------------------------------------------------------------------
    # Connect loop
    # ------------------------------------------------------------------
    def _connect_loop(self) -> None:
        backoff_idx = 0
        while not self._stop_event.is_set():
            try:
                self._set_state(ConnState.CONNECTING)
                self._build_ticker()
                self._wire_handlers()
                self._connect_blocking()
            except _TokenExpired as exc:
                self._handle_token_expiry(str(exc))
                return  # terminal — restart needed via re-login
            except Exception as exc:
                self._record_failure(exc)
                if self._failure_age_seconds() >= _DEGRADED_AFTER_SECONDS:
                    self._set_state(
                        ConnState.DEGRADED,
                        detail=f"continuous failures > {_DEGRADED_AFTER_SECONDS:.0f}s",
                    )

            if self._stop_event.is_set():
                break

            delay = self._next_backoff_delay(backoff_idx)
            backoff_idx += 1
            logger.warning(
                "WS reconnect in %.1fs (attempt %d, last_error=%s)",
                delay, self._reconnect_attempts, self._last_error,
            )
            self._stop_event.wait(timeout=delay)

        self._set_state(ConnState.STOPPED, detail="exit connect loop")

    def _next_backoff_delay(self, idx: int) -> float:
        if idx < len(_BACKOFF_SCHEDULE):
            return _BACKOFF_SCHEDULE[idx]
        return 60.0

    def _build_ticker(self) -> None:
        """Construct the underlying KiteTicker. Lazy-imports kiteconnect so
        unit tests don't need the SDK installed."""
        if self._ticker_factory is not None:
            self._ticker = self._ticker_factory(self._api_key, self._access_token)
            return
        try:
            from kiteconnect import KiteTicker  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "kiteconnect SDK not installed; pip install kiteconnect>=5.2"
            ) from exc
        self._ticker = KiteTicker(self._api_key, self._access_token)

    def _wire_handlers(self) -> None:
        t = self._ticker
        t.on_ticks = self._on_ticks
        t.on_connect = self._on_connect
        t.on_close = self._on_close
        t.on_error = self._on_error
        # Kite's reconnect callbacks (best-effort — not all SDK versions expose them).
        for cb_name in ("on_reconnect", "on_noreconnect"):
            if hasattr(t, cb_name):
                setattr(t, cb_name, self._on_kite_reconnect)

    def _connect_blocking(self) -> None:
        """Run the WS in this thread. `KiteTicker.connect()` blocks until
        the socket closes; on close we fall back to the connect loop which
        decides whether to retry."""
        # threaded=False → blocking call; we manage our own thread (the
        # caller's thread is already dedicated to this runner).
        self._ticker.connect(threaded=False, disable_ssl_verification=False)

    # ------------------------------------------------------------------
    # KiteTicker callbacks
    # ------------------------------------------------------------------
    def _on_connect(self, ws, response) -> None:  # noqa: ARG002 - SDK signature
        self._set_state(ConnState.CONNECTED, detail="WS handshake complete")
        self._reconnect_attempts = 0
        self._failure_started_at = None
        self._reconcile_subscriptions(force=True)

    def _on_ticks(self, ws, ticks) -> None:  # noqa: ARG002
        if not ticks:
            return
        now = datetime.now(tz=timezone.utc)
        with self._lock:
            self._last_tick_at = now
        for tick in ticks:
            try:
                self._handle_tick(tick)
            except Exception:
                logger.exception("error handling tick")

    def _on_close(self, ws, code, reason) -> None:  # noqa: ARG002
        msg = f"WS closed code={code} reason={reason}"
        logger.warning(msg)
        self._last_error = msg
        with self._lock:
            self._subscribed_tokens.clear()
        # 1006 / 4001 / 4002 etc. — let the connect_loop reconnect.

    def _on_error(self, ws, code, reason) -> None:  # noqa: ARG002
        msg = f"WS error code={code} reason={reason}"
        logger.error(msg)
        self._last_error = msg
        if _looks_like_token_error(code, reason):
            raise _TokenExpired(msg)

    def _on_kite_reconnect(self, *args, **kwargs) -> None:  # noqa: ARG002
        # KiteTicker has its own reconnect logic; we log but defer to our
        # own loop to keep state consistent.
        logger.info("KiteTicker auto-reconnect callback fired")

    # ------------------------------------------------------------------
    # Tick → cache + event bus
    # ------------------------------------------------------------------
    def _handle_tick(self, tick: dict) -> None:
        token = tick.get("instrument_token")
        if token is None:
            return
        meta = self._token_meta.get(int(token))
        quote = _tick_to_quote(tick, meta, self._provider_name)

        # Cache by instrument_token (numeric, fast lookup).
        try:
            self._cache.set(int(token), quote)
        except Exception:
            logger.exception("cache.set failed for token=%s", token)

        # Also cache by the option-key tuple so the REST provider can find
        # it without a token round-trip.
        if meta is not None and meta.expiry and meta.strike is not None and meta.option_type:
            key = (meta.symbol, meta.expiry.date() if isinstance(meta.expiry, datetime) else meta.expiry,
                   float(meta.strike), str(meta.option_type))
            try:
                self._cache.set(key, quote)
            except Exception:
                logger.exception("cache.set failed for key=%s", key)
        elif meta is not None and meta.is_index:
            try:
                self._cache.set(("spot", meta.symbol), quote)
            except Exception:
                logger.exception("cache.set failed for spot=%s", meta.symbol)

        try:
            self._event_bus.publish(TOPIC_TICK, quote)
        except Exception:
            logger.exception("event_bus.publish failed for tick")

    # ------------------------------------------------------------------
    # Subscription reconcile
    # ------------------------------------------------------------------
    def _reconcile_subscriptions(self, force: bool = False) -> None:
        """Diff `_desired_tokens` against `_subscribed_tokens` and apply
        the delta on the live ticker. No-op if WS not connected."""
        if self._ticker is None:
            return
        if self._state not in (ConnState.CONNECTED,) and not force:
            # Will be reconciled on next on_connect.
            return
        with self._lock:
            desired = set(self._desired_tokens)
            current = set(self._subscribed_tokens)
            to_add = desired - current
            to_remove = current - desired

        if to_remove:
            try:
                self._ticker.unsubscribe(list(to_remove))
            except Exception:
                logger.exception("unsubscribe failed for %d tokens", len(to_remove))

        if to_add:
            tokens = list(to_add)
            try:
                self._ticker.subscribe(tokens)
                # set_mode is the Kite API for switching subscription depth.
                mode = self._ticker.MODE_FULL if self._mode_full and hasattr(self._ticker, "MODE_FULL") else getattr(self._ticker, "MODE_QUOTE", "quote")
                try:
                    self._ticker.set_mode(mode, tokens)
                except Exception:
                    logger.exception("set_mode failed for %d tokens", len(tokens))
            except Exception:
                logger.exception("subscribe failed for %d tokens", len(to_add))
                return

        with self._lock:
            self._subscribed_tokens = (self._subscribed_tokens - to_remove) | to_add
            logger.info(
                "WS subscriptions reconciled: +%d -%d, total=%d",
                len(to_add), len(to_remove), len(self._subscribed_tokens),
            )

    # ------------------------------------------------------------------
    # Heartbeat watchdog (called by an external scheduler tick or a thread)
    # ------------------------------------------------------------------
    def watchdog_check(self, now: Optional[datetime] = None) -> bool:
        """Return True if the connection looks alive; False if a forced
        reconnect was triggered. Safe to call periodically (e.g. every 5s)
        from a separate thread."""
        if self._state != ConnState.CONNECTED:
            return True
        if not self._is_market_open():
            return True
        n = now or datetime.now(tz=timezone.utc)
        with self._lock:
            last = self._last_tick_at
        if last is None:
            return True
        idle = (n - last).total_seconds()
        if idle <= self._heartbeat_timeout:
            return True
        logger.warning("watchdog: no tick for %.1fs (>%.1fs); forcing reconnect", idle, self._heartbeat_timeout)
        self._record_watchdog_event(n)
        self._safe_close()
        if self._is_watchdog_overrun():
            self._set_state(
                ConnState.DEGRADED,
                detail=f"{_WATCHDOG_DEGRADE_THRESHOLD}+ watchdog resets in {_WATCHDOG_WINDOW_SECONDS:.0f}s",
            )
        return False

    def _record_watchdog_event(self, now: datetime) -> None:
        ts = now.timestamp()
        cutoff = ts - _WATCHDOG_WINDOW_SECONDS
        with self._lock:
            self._watchdog_events.append(ts)
            while self._watchdog_events and self._watchdog_events[0] < cutoff:
                self._watchdog_events.popleft()

    def _is_watchdog_overrun(self) -> bool:
        with self._lock:
            return len(self._watchdog_events) >= _WATCHDOG_DEGRADE_THRESHOLD

    # ------------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------------
    def _set_state(self, state: ConnState, detail: str = "") -> None:
        with self._lock:
            if self._state == state:
                return
            self._state = state
        try:
            self._event_bus.publish(
                TOPIC_CONNECTION_STATE,
                {"provider": self._provider_name, "state": state.value, "detail": detail},
            )
        except Exception:
            logger.exception("event_bus.publish failed for connection_state")

    def _record_failure(self, exc: Exception) -> None:
        self._reconnect_attempts += 1
        self._last_error = f"{type(exc).__name__}: {exc}"
        if self._failure_started_at is None:
            self._failure_started_at = datetime.now(tz=timezone.utc)
        logger.warning("WS connect attempt failed: %s", self._last_error)

    def _failure_age_seconds(self) -> float:
        if self._failure_started_at is None:
            return 0.0
        return (datetime.now(tz=timezone.utc) - self._failure_started_at).total_seconds()

    def _handle_token_expiry(self, detail: str) -> None:
        logger.error("WS token expired/invalid — re-login required (%s)", detail)
        self._safe_close()
        self._set_state(ConnState.TOKEN_EXPIRED, detail=detail)
        try:
            self._event_bus.publish(TOPIC_TOKEN_EXPIRED, {"provider": self._provider_name})
        except Exception:
            logger.exception("event_bus.publish failed for token_expired")
        self._stop_event.set()

    def _safe_close(self) -> None:
        t = self._ticker
        self._ticker = None
        if t is None:
            return
        for fn_name in ("close", "stop"):
            fn = getattr(t, fn_name, None)
            if callable(fn):
                try:
                    fn()
                except Exception:
                    logger.exception("ticker.%s() raised during shutdown", fn_name)

    # ------------------------------------------------------------------
    # Signal handling
    # ------------------------------------------------------------------
    def _install_signal_handlers(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            # signal.signal can only be called from the main thread.
            return
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, self._on_signal)
            except (ValueError, OSError):
                # Not supported on this platform / context (e.g. inside threads).
                pass

    def _on_signal(self, signum, frame) -> None:  # noqa: ARG002
        logger.info("received signal %s — shutting down WS runner", signum)
        self.stop()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
class _TokenExpired(RuntimeError):
    """Raised inside `on_error` to break out of the connect loop."""


def _looks_like_token_error(code: Any, reason: Any) -> bool:
    """Best-effort detection — KiteTicker error codes are not strictly
    documented across SDK versions. We check for common signals."""
    s = f"{code} {reason}".lower()
    if "403" in s:
        return True
    if "token" in s and ("invalid" in s or "expired" in s):
        return True
    return False
