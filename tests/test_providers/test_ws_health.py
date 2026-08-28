"""Tests for providers.ws_health — WS health gating for SL fallback."""
from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import patch

from providers.ws_health import is_ws_unhealthy, load_ws_status


def test_is_ws_healthy_when_connected_and_fresh():
    now = datetime(2026, 5, 5, 11, 0)
    snap = {
        "connection_state": "connected",
        "last_tick_at": now.isoformat(),
        "generated_at": now.isoformat(),
        "subscribed_tokens": 42,
    }
    with patch("providers.ws_health.load_ws_status", return_value=snap):
        unhealthy, reason = is_ws_unhealthy(now)
    assert unhealthy is False
    assert reason == "ws_healthy"


def test_is_ws_unhealthy_when_status_missing():
    now = datetime(2026, 5, 5, 11, 0)
    with patch("providers.ws_health.load_ws_status", return_value=None):
        unhealthy, reason = is_ws_unhealthy(now)
    assert unhealthy is True
    assert "missing" in reason


def test_skips_outside_session():
    now = datetime(2026, 5, 5, 18, 0)  # after close
    unhealthy, reason = is_ws_unhealthy(now)
    assert unhealthy is False
    assert reason == "outside_session"
