"""Unit tests for shared tick-feed status payload + login/logout publish."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def server_mod(monkeypatch, tmp_path):
    import dashboard.server as server

    signal_path = tmp_path / "tick_feed_signal.json"
    monkeypatch.setattr(server, "_TICK_FEED_SIGNAL_PATH", str(signal_path))
    return server


def _admin(ok=True):
    if not ok:
        return None
    return SimpleNamespace(access_token="tok", user_id="AB123")


def test_tick_feed_offline_when_admin_session_missing(server_mod, monkeypatch):
    monkeypatch.setattr(
        "providers.ws_health.in_nse_session", lambda now: True,
    )
    monkeypatch.setattr(
        "providers.ws_health.is_ws_unhealthy", lambda now: (True, "ws_status_missing"),
    )
    monkeypatch.setattr(
        "providers.ws_health.load_ws_status",
        lambda: {"connection_state": "waiting_for_login"},
    )
    monkeypatch.setattr(
        "providers.zerodha.session.load_admin_session", lambda: None,
    )
    monkeypatch.setattr(
        "providers.zerodha.session.is_token_valid", lambda s: False,
    )

    out = server_mod._tick_feed_status_payload()
    assert out["mode"] == "feed_off"
    assert out["label"] == "Offline"
    assert out["alive"] is False
    assert out["admin_session"] is False
    assert "admin_session_missing" in (out["reason"] or "")


def test_tick_feed_market_off_when_admin_logged_in_outside_session(server_mod, monkeypatch):
    monkeypatch.setattr(
        "providers.ws_health.in_nse_session", lambda now: False,
    )
    monkeypatch.setattr(
        "providers.ws_health.is_ws_unhealthy", lambda now: (False, "outside_session"),
    )
    # Stale waiting_for_login must not keep Offline after admin login.
    monkeypatch.setattr(
        "providers.ws_health.load_ws_status",
        lambda: {"connection_state": "waiting_for_login"},
    )
    monkeypatch.setattr(
        "providers.zerodha.session.load_admin_session", lambda: _admin(),
    )
    monkeypatch.setattr(
        "providers.zerodha.session.is_token_valid", lambda s: True,
    )

    out = server_mod._tick_feed_status_payload()
    assert out["mode"] == "market_off"
    assert out["label"] == "Market off"
    assert out["alive"] is True
    assert out["admin_session"] is True


def test_tick_feed_connecting_after_admin_login_before_ticks(server_mod, monkeypatch):
    monkeypatch.setattr(
        "providers.ws_health.in_nse_session", lambda now: True,
    )
    monkeypatch.setattr(
        "providers.ws_health.is_ws_unhealthy",
        lambda now: (True, "last_tick_stale age=None"),
    )
    monkeypatch.setattr(
        "providers.ws_health.load_ws_status",
        lambda: {"connection_state": "connecting"},
    )
    monkeypatch.setattr(
        "providers.zerodha.session.load_admin_session", lambda: _admin(),
    )
    monkeypatch.setattr(
        "providers.zerodha.session.is_token_valid", lambda s: True,
    )

    out = server_mod._tick_feed_status_payload()
    assert out["mode"] == "connecting"
    assert out["label"] == "Connecting"
    assert out["alive"] is True


def test_tick_feed_live_when_healthy_in_session(server_mod, monkeypatch):
    monkeypatch.setattr(
        "providers.ws_health.in_nse_session", lambda now: True,
    )
    monkeypatch.setattr(
        "providers.ws_health.is_ws_unhealthy", lambda now: (False, "ws_healthy"),
    )
    monkeypatch.setattr(
        "providers.ws_health.load_ws_status",
        lambda: {"connection_state": "connected", "last_tick_at": datetime.now().isoformat()},
    )
    monkeypatch.setattr(
        "providers.zerodha.session.load_admin_session", lambda: _admin(),
    )
    monkeypatch.setattr(
        "providers.zerodha.session.is_token_valid", lambda s: True,
    )

    out = server_mod._tick_feed_status_payload()
    assert out["mode"] == "live"
    assert out["label"] == "Live"
    assert out["alive"] is True


def test_bump_tick_feed_signal_writes_event(server_mod, monkeypatch):
    payload = server_mod._bump_tick_feed_signal("admin_login")
    assert payload["event"] == "admin_login"
    assert payload.get("at")
    read = server_mod._read_tick_feed_signal()
    assert read["event"] == "admin_login"
    # Signal rides on status payload so SSE clients see a signature change.
    monkeypatch.setattr(
        "providers.ws_health.in_nse_session", lambda now: False,
    )
    monkeypatch.setattr(
        "providers.ws_health.is_ws_unhealthy", lambda now: (False, "outside_session"),
    )
    monkeypatch.setattr(
        "providers.ws_health.load_ws_status", lambda: {},
    )
    monkeypatch.setattr(
        "providers.zerodha.session.load_admin_session", lambda: _admin(),
    )
    monkeypatch.setattr(
        "providers.zerodha.session.is_token_valid", lambda s: True,
    )
    out = server_mod._tick_feed_status_payload()
    assert out["signal"]["event"] == "admin_login"


def test_publish_logout_bumps_signal(server_mod, monkeypatch):
    idle = MagicMock()
    monkeypatch.setattr(
        "providers.ws_monitor.default_snapshot_path",
        lambda: MagicMock(parent=MagicMock()),
    )
    monkeypatch.setattr(
        "providers.ws_monitor.write_idle_snapshot", idle,
    )
    server_mod._publish_tick_feed_logout()
    idle.assert_called_once()
    assert server_mod._read_tick_feed_signal()["event"] == "admin_logout"


def test_publish_login_bumps_signal(server_mod, monkeypatch, tmp_path):
    ws_path = tmp_path / "ws_status.json"
    monkeypatch.setattr(
        "providers.ws_monitor.default_snapshot_path", lambda: ws_path,
    )
    monkeypatch.setattr(
        "providers.ws_health.load_ws_status",
        lambda: {"connection_state": "waiting_for_login"},
    )
    server_mod._publish_tick_feed_login()
    assert server_mod._read_tick_feed_signal()["event"] == "admin_login"
    assert ws_path.is_file()
    import json
    snap = json.loads(ws_path.read_text(encoding="utf-8"))
    assert snap["connection_state"] == "connecting"
