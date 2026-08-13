"""Tests for Zerodha permission check and scout_zerodha_log."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from providers.zerodha.permission_check import (
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
