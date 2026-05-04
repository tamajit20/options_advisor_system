"""
tests/test_providers/test_zerodha/test_rate_limiter.py
======================================================

Tests for the `TokenBucket` rate limiter.
"""

from __future__ import annotations

import time

import pytest

from providers.zerodha.rate_limiter import TokenBucket


def test_initial_capacity_full():
    b = TokenBucket(rate_per_sec=10.0)
    assert b.try_acquire(10) is True


def test_try_acquire_returns_false_when_empty():
    b = TokenBucket(rate_per_sec=1.0, capacity=1)
    assert b.try_acquire(1) is True
    assert b.try_acquire(1) is False


def test_invalid_rate_raises():
    with pytest.raises(ValueError):
        TokenBucket(rate_per_sec=0)
    with pytest.raises(ValueError):
        TokenBucket(rate_per_sec=-1)


def test_acquire_more_than_capacity_raises():
    b = TokenBucket(rate_per_sec=10.0, capacity=10)
    with pytest.raises(ValueError):
        b.acquire(11)


def test_refill_over_time(monkeypatch):
    fake = {"t": 0.0}
    monkeypatch.setattr(time, "monotonic", lambda: fake["t"])
    b = TokenBucket(rate_per_sec=1.0, capacity=1.0)
    assert b.try_acquire(1) is True
    assert b.try_acquire(1) is False
    fake["t"] = 1.0
    assert b.try_acquire(1) is True


def test_refill_caps_at_capacity(monkeypatch):
    fake = {"t": 0.0}
    monkeypatch.setattr(time, "monotonic", lambda: fake["t"])
    b = TokenBucket(rate_per_sec=10.0, capacity=10.0)
    fake["t"] = 100.0   # huge gap
    assert b.available == pytest.approx(10.0)


def test_acquire_blocks_then_succeeds(monkeypatch):
    """Verify acquire() sleeps the right amount when the bucket is empty."""
    fake = {"t": 0.0}
    sleeps: list = []
    monkeypatch.setattr(time, "monotonic", lambda: fake["t"])

    def fake_sleep(d):
        sleeps.append(d)
        fake["t"] += d

    monkeypatch.setattr(time, "sleep", fake_sleep)
    b = TokenBucket(rate_per_sec=2.0, capacity=1.0)
    assert b.try_acquire(1) is True   # bucket empty now
    assert b.acquire(1) is True
    # rate=2/s → need 0.5s to refill 1 token
    assert sum(sleeps) == pytest.approx(0.5, abs=1e-9)


def test_acquire_with_timeout_returns_false(monkeypatch):
    fake = {"t": 0.0}
    monkeypatch.setattr(time, "monotonic", lambda: fake["t"])

    def fake_sleep(d):
        fake["t"] += d

    monkeypatch.setattr(time, "sleep", fake_sleep)
    b = TokenBucket(rate_per_sec=0.1, capacity=1.0)
    b.try_acquire(1)  # drain
    assert b.acquire(1, timeout=1.0) is False  # need 10s, only allow 1s
