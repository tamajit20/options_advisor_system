"""Tests for lifecycle/zerodha_relogin_reminder.py — daily re-login reminder."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from lifecycle import zerodha_relogin_reminder as mod


@pytest.fixture
def db_mock():
    db = MagicMock()
    db.commit = MagicMock(return_value=None)
    return db


class TestNoOpPaths:
    def test_skips_when_provider_not_zerodha(self, db_mock, mocker):
        notif_repo = mocker.patch("lifecycle.zerodha_relogin_reminder.NotificationRepo")
        rc = mod.run_zerodha_relogin_reminder(
            db_mock, config_provider="", config_enabled=True,
        )
        assert rc == 0
        notif_repo.assert_not_called()

    def test_skips_when_zerodha_disabled(self, db_mock, mocker):
        notif_repo = mocker.patch("lifecycle.zerodha_relogin_reminder.NotificationRepo")
        rc = mod.run_zerodha_relogin_reminder(
            db_mock, config_provider="zerodha", config_enabled=False,
        )
        assert rc == 0
        notif_repo.assert_not_called()


class TestSessionCheck:
    def test_no_reminder_when_session_valid(self, db_mock, mocker):
        mocker.patch(
            "providers.zerodha.session.load_session",
            return_value=MagicMock(),
        )
        mocker.patch(
            "providers.zerodha.session.is_token_valid", return_value=True,
        )
        notif_repo = mocker.patch("lifecycle.zerodha_relogin_reminder.NotificationRepo")
        rc = mod.run_zerodha_relogin_reminder(
            db_mock, config_provider="zerodha", config_enabled=True,
        )
        assert rc == 0
        notif_repo.assert_not_called()

    def test_fires_reminder_when_no_session_persisted(self, db_mock, mocker):
        mocker.patch(
            "providers.zerodha.session.load_session", return_value=None,
        )
        mocker.patch(
            "providers.zerodha.session.is_token_valid", return_value=False,
        )
        repo_instance = MagicMock()
        mocker.patch(
            "lifecycle.zerodha_relogin_reminder.NotificationRepo",
            return_value=repo_instance,
        )
        rc = mod.run_zerodha_relogin_reminder(
            db_mock, config_provider="zerodha", config_enabled=True,
        )
        assert rc == 1
        repo_instance.insert.assert_called_once()
        # Inspect the inserted Notification
        notif = repo_instance.insert.call_args[0][0]
        assert notif.notif_type == "ZERODHA_RELOGIN_REQUIRED"
        assert notif.severity == "CRITICAL"
        assert "--zerodha-login" in notif.body
        db_mock.commit.assert_called_once()

    def test_fires_reminder_when_session_expired(self, db_mock, mocker):
        mocker.patch(
            "providers.zerodha.session.load_session",
            return_value=MagicMock(),
        )
        mocker.patch(
            "providers.zerodha.session.is_token_valid", return_value=False,
        )
        repo_instance = MagicMock()
        mocker.patch(
            "lifecycle.zerodha_relogin_reminder.NotificationRepo",
            return_value=repo_instance,
        )
        rc = mod.run_zerodha_relogin_reminder(
            db_mock, config_provider="zerodha", config_enabled=True,
        )
        assert rc == 1
        repo_instance.insert.assert_called_once()
