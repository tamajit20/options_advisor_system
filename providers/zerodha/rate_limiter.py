"""
providers/zerodha/rate_limiter.py
=================================

Token-bucket rate limiter for Kite REST calls.

Kite limits (verified 2026-05-04):
    /quote            1 req/sec    (very tight; full quote with 5-level depth)
    /quote/ltp        10 req/sec   (bulk LTP — up to 1000 instruments per call)
    /quote/ohlc       10 req/sec   (bulk OHLC)
    historical_data   3 req/sec
    everything else   10 req/sec

We expose one `TokenBucket` per endpoint class. The provider acquires a token
before each call; if the bucket is empty, `acquire()` sleeps just long enough
to refill 1 token.

Implementation notes:
    - `time.monotonic()` for clock — never wall-clock.
    - One `threading.Lock` per bucket; safe for multi-threaded REST callers.
    - Buckets are tiny (capacity = rate_per_sec) — Kite penalises bursts more
      than steady traffic, so over-bursting just to hit the cap is unwise.
"""

from __future__ import annotations

import threading
import time


class TokenBucket:
    """A simple thread-safe token bucket.

    Refills at `rate_per_sec` tokens/second up to `capacity` tokens.
    `acquire(n=1)` blocks (sleeps) until n tokens are available, then debits
    them.
    """

    def __init__(self, rate_per_sec: float, capacity: float | None = None):
        if rate_per_sec <= 0:
            raise ValueError("rate_per_sec must be positive")
        self._rate = float(rate_per_sec)
        self._capacity = float(capacity) if capacity is not None else float(rate_per_sec)
        if self._capacity <= 0:
            raise ValueError("capacity must be positive")
        self._tokens = self._capacity
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def _refill_locked(self, now: float) -> None:
        elapsed = now - self._last_refill
        if elapsed > 0:
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
            self._last_refill = now

    def try_acquire(self, n: float = 1.0) -> bool:
        """Non-blocking; returns True if `n` tokens were debited, False otherwise."""
        with self._lock:
            self._refill_locked(time.monotonic())
            if self._tokens >= n:
                self._tokens -= n
                return True
            return False

    def acquire(self, n: float = 1.0, timeout: float | None = None) -> bool:
        """Block until `n` tokens can be debited, or until `timeout` seconds
        have elapsed. Returns True on success, False on timeout."""
        if n > self._capacity:
            raise ValueError("n cannot exceed capacity")
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                now = time.monotonic()
                self._refill_locked(now)
                if self._tokens >= n:
                    self._tokens -= n
                    return True
                # How long until we'd have n tokens?
                deficit = n - self._tokens
                wait = deficit / self._rate
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                wait = min(wait, remaining)
            time.sleep(wait)

    @property
    def available(self) -> float:
        with self._lock:
            self._refill_locked(time.monotonic())
            return self._tokens


class SlidingWindowCap:
    """Cap events to ``max_per_window`` inside any rolling ``window_sec``.

    Used for Kite order placement (broker limit 10 orders/sec). When the cap
    is full, ``acquire()`` sleeps until the oldest event leaves the window,
    then records the new event.
    """

    def __init__(self, max_per_window: int, window_sec: float = 1.0):
        if max_per_window < 1:
            raise ValueError("max_per_window must be >= 1")
        if window_sec <= 0:
            raise ValueError("window_sec must be positive")
        self._max = int(max_per_window)
        self._window = float(window_sec)
        self._times: list[float] = []
        self._lock = threading.Lock()

    def set_max(self, max_per_window: int) -> None:
        if max_per_window < 1:
            raise ValueError("max_per_window must be >= 1")
        with self._lock:
            self._max = int(max_per_window)

    @property
    def max_per_window(self) -> int:
        with self._lock:
            return self._max

    def _prune_locked(self, now: float) -> None:
        cutoff = now - self._window
        # Drop timestamps that have left the rolling window.
        i = 0
        n = len(self._times)
        while i < n and self._times[i] <= cutoff:
            i += 1
        if i:
            del self._times[:i]

    def try_acquire(self) -> bool:
        with self._lock:
            now = time.monotonic()
            self._prune_locked(now)
            if len(self._times) >= self._max:
                return False
            self._times.append(now)
            return True

    def acquire(self) -> None:
        """Block until a slot is free in the current window, then take it."""
        while True:
            with self._lock:
                now = time.monotonic()
                self._prune_locked(now)
                if len(self._times) < self._max:
                    self._times.append(now)
                    return
                wait = self._times[0] + self._window - now
            time.sleep(max(wait, 0.001))

    @property
    def used(self) -> int:
        with self._lock:
            self._prune_locked(time.monotonic())
            return len(self._times)
