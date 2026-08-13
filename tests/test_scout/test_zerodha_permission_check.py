"""Tests for Zerodha permission check and scout_zerodha_log."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from providers.zerodha.permission_check import (
    overlay_live_websocket_check,
    permissions_ok_for_live,
    run_zerodha_permission_check,
    run_and_persist_check,
)


def test_run_check_fails_without_credentials():
    with patch("providers.zerodha.permission_check.ZERODHA_API_CONFIG", {
        "api_key": "", "api_secret": "", "enabled": True,
    }), patch("providers.zerodha.permission_check.load_session", return_value=None):
        summary = run_zerodha_permission_check(include_ws=False)
    assert summary["overall_ok"] is False
    assert any(c["check_id"] == "api_credentials" and not c["ok"] for c in summary["checks"])


def test_run_check_passes_with_mocked_kite():
    mock_client = MagicMock()
    mock_client._kite.profile.return_value = {"user_name": "Test User"}
    mock_client.margins.return_value = {
        "equity": {"available": {"live_balance": 20000}, "net": 20000},
    }
    mock_client.order_margins.return_value = [{"total": 5000}]

    sess = MagicMock()
    sess.access_token = "tok"
    sess.user_id = "AB1234"

    with patch("providers.zerodha.permission_check.ZERODHA_API_CONFIG", {
        "api_key": "key", "api_secret": "secret", "enabled": True,
    }), patch("providers.zerodha.permission_check.load_session", return_value=sess), \
         patch("providers.zerodha.permission_check.is_token_valid", return_value=True), \
         patch("providers.zerodha.permission_check._kite_client_or_error", return_value=(mock_client, None)):
        summary = run_zerodha_permission_check(include_ws=False)
    assert summary["overall_ok"] is True


def test_persist_check_writes_to_db():
    fake_db = MagicMock()
    repo = MagicMock()
    repo.insert.return_value = 1
    summary = {
        "overall_ok": False,
        "checks": [
            {"check_id": "margins", "label": "Margins", "ok": False, "error": "denied"},
        ],
        "failed_count": 1,
        "user_id": "X1",
    }
    with patch("providers.zerodha.permission_check.run_zerodha_permission_check", return_value=summary), \
         patch("database.scout_models.ScoutZerodhaLogRepo", return_value=repo):
        out = run_and_persist_check(fake_db, trigger="manual", include_ws=False)
    assert out["trigger"] == "manual"
    assert repo.insert.call_count >= 2


def test_overlay_live_websocket_check_replaces_stale_row():
    stale = {
        "overall_ok": True,
        "checks": [
            {"check_id": "session", "label": "Session", "ok": True},
            {
                "check_id": "websocket", "label": "WebSocket runner", "ok": False,
                "error": "ws snapshot stale (0s old)",
            },
        ],
        "failed_count": 1,
    }
    live_ws = {
        "check_id": "websocket", "label": "WebSocket runner", "ok": True,
        "detail": "connected · snapshot 1.2s old",
    }
    with patch("providers.zerodha.permission_check._probe_websocket", return_value=live_ws):
        out = overlay_live_websocket_check(stale)
    ws = [c for c in out["checks"] if c["check_id"] == "websocket"][0]
    assert ws["ok"] is True
    assert out["failed_count"] == 0


def test_permissions_ok_for_live_requires_websocket_when_stale():
    summary = {
        "overall_ok": True,
        "checks": [
            {"check_id": "session", "ok": True},
            {"check_id": "websocket", "ok": False, "error": "stale"},
        ],
    }
    with patch("providers.zerodha.permission_check.last_permission_summary", return_value=summary):
        assert permissions_ok_for_live(require_websocket=True) is False
        assert permissions_ok_for_live(require_websocket=False) is True
