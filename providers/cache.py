"""
providers/cache.py
==================

In-memory TTL cache used by live providers (Zerodha) to coalesce repeated
reads of the same instrument within a short window. Avoids hammering Kite REST
when several callers ask for the same chain in quick succession.

This cache lives in process memory only — it is NEVER persisted. The
canonical historical store is the existing EOD tables (`options_fo_eod`,
`options_spot_eod`, etc.). See plan §"Data layering — EOD stays, live layered
on top, never merged" and §"Tick retention policy".

Thread-safe via a single `threading.RLock`. Performance is fine for our scale
(< few thousand live keys at any moment).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple


@dataclass
class _Entry:
    value: Any
    expires_at: float    # monotonic seconds
    written_at: float = field(default_factory=time.monotonic)


class TTLCache:
    """Tiny TTL cache. Default TTL is 5 seconds (matches plan's "live cache
    ≤5s old" precedence rule). Per-key TTL override allowed at write time.

    Eviction is lazy — expired entries linger until next read or until
    `purge_expired()` is called explicitly.
    """

    def __init__(self, default_ttl_seconds: float = 5.0, max_entries: int = 10_000):
        if default_ttl_seconds <= 0:
            raise ValueError("default_ttl_seconds must be positive")
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self._default_ttl = float(default_ttl_seconds)
        self._max_entries = int(max_entries)
        self._lock = threading.RLock()
        self._store: Dict[Any, _Entry] = {}

    # ---- internal ----
    def _is_fresh(self, entry: _Entry, now: float) -> bool:
        return entry.expires_at > now

    def _evict_if_oversize(self) -> None:
        """If we exceed `max_entries`, drop the oldest-written entries first.
        Cheap O(n) — we don't care about LRU precision at this scale."""
        if len(self._store) <= self._max_entries:
            return
        # Sort by written_at ascending; drop the oldest (len - max_entries) items.
        items = sorted(self._store.items(), key=lambda kv: kv[1].written_at)
        excess = len(self._store) - self._max_entries
        for key, _ in items[:excess]:
            self._store.pop(key, None)

    # ---- public ----
    def get(self, key: Any) -> Optional[Any]:
        """Return the cached value if still fresh, else None."""
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            if not self._is_fresh(entry, time.monotonic()):
                self._store.pop(key, None)
                return None
            return entry.value

    def get_with_age(self, key: Any) -> Tuple[Optional[Any], Optional[float]]:
        """Return (value, age_seconds) or (None, None) if missing/expired.
        Useful when callers need to populate `freshness_ms` on a `LiveQuote`.
        """
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None, None
            now = time.monotonic()
            if not self._is_fresh(entry, now):
                self._store.pop(key, None)
                return None, None
            return entry.value, max(0.0, now - entry.written_at)

    def set(self, key: Any, value: Any, ttl_seconds: Optional[float] = None) -> None:
        ttl = float(ttl_seconds) if ttl_seconds is not None else self._default_ttl
        if ttl <= 0:
            raise ValueError("ttl_seconds must be positive")
        now = time.monotonic()
        with self._lock:
            self._store[key] = _Entry(value=value, expires_at=now + ttl, written_at=now)
            self._evict_if_oversize()

    def invalidate(self, key: Any) -> None:
        with self._lock:
            self._store.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def purge_expired(self) -> int:
        """Drop all expired entries. Returns number of entries removed."""
        now = time.monotonic()
        with self._lock:
            stale = [k for k, e in self._store.items() if not self._is_fresh(e, now)]
            for k in stale:
                self._store.pop(k, None)
            return len(stale)

    def size(self) -> int:
        with self._lock:
            return len(self._store)
