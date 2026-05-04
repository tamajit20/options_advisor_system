"""
tests/test_providers/test_cache.py
==================================

Unit tests for `providers.cache.TTLCache`. No external dependencies — uses
`time.monotonic` patched via a fake clock fixture.
"""

from __future__ import annotations

import time

import pytest

from providers.cache import TTLCache


def test_set_and_get_returns_value():
    c = TTLCache(default_ttl_seconds=10)
    c.set("k", "v")
    assert c.get("k") == "v"


def test_missing_key_returns_none():
    c = TTLCache()
    assert c.get("nope") is None


def test_entry_expires_after_ttl(monkeypatch):
    fake = {"t": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: fake["t"])
    c = TTLCache(default_ttl_seconds=5)
    c.set("k", "v")
    fake["t"] = 1004.999
    assert c.get("k") == "v"  # still fresh
    fake["t"] = 1006.0
    assert c.get("k") is None  # expired


def test_per_key_ttl_override(monkeypatch):
    fake = {"t": 100.0}
    monkeypatch.setattr(time, "monotonic", lambda: fake["t"])
    c = TTLCache(default_ttl_seconds=5)
    c.set("short", "x", ttl_seconds=1)
    c.set("long", "y", ttl_seconds=60)
    fake["t"] = 102.0
    assert c.get("short") is None
    assert c.get("long") == "y"


def test_invalidate_drops_entry():
    c = TTLCache()
    c.set("k", "v")
    c.invalidate("k")
    assert c.get("k") is None


def test_clear_drops_all():
    c = TTLCache()
    c.set("a", 1)
    c.set("b", 2)
    c.clear()
    assert c.size() == 0


def test_purge_expired_returns_count(monkeypatch):
    fake = {"t": 0.0}
    monkeypatch.setattr(time, "monotonic", lambda: fake["t"])
    c = TTLCache(default_ttl_seconds=5)
    c.set("a", 1)
    c.set("b", 2)
    fake["t"] = 100.0
    assert c.purge_expired() == 2
    assert c.size() == 0


def test_get_with_age(monkeypatch):
    fake = {"t": 50.0}
    monkeypatch.setattr(time, "monotonic", lambda: fake["t"])
    c = TTLCache(default_ttl_seconds=10)
    c.set("k", "v")
    fake["t"] = 53.0
    val, age = c.get_with_age("k")
    assert val == "v"
    assert age == pytest.approx(3.0, abs=1e-6)


def test_max_entries_evicts_oldest(monkeypatch):
    fake = {"t": 0.0}
    monkeypatch.setattr(time, "monotonic", lambda: fake["t"])
    c = TTLCache(default_ttl_seconds=1000, max_entries=3)
    for i, key in enumerate(["a", "b", "c"]):
        fake["t"] = float(i)
        c.set(key, i)
    fake["t"] = 10.0
    c.set("d", 99)
    # "a" was the oldest by written_at and should be evicted.
    assert c.get("a") is None
    assert c.get("d") == 99
    assert c.size() == 3


def test_invalid_ttl_raises():
    with pytest.raises(ValueError):
        TTLCache(default_ttl_seconds=0)
    with pytest.raises(ValueError):
        TTLCache(default_ttl_seconds=-1)
    c = TTLCache()
    with pytest.raises(ValueError):
        c.set("k", "v", ttl_seconds=0)
