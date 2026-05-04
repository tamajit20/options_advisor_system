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
