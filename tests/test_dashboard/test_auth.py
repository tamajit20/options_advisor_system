"""Tests for dashboard multi-user / legacy API-key auth."""

from __future__ import annotations

import pytest

from tests.conftest import DASHBOARD_ADMIN_USER


@pytest.fixture
def authed_app(mocker):
    mocker.patch.dict(
        "config.DASHBOARD_CONFIG",
        {"api_key": "test-secret-key", "require_app_login": True},
        clear=False,
    )
    mocker.patch("dashboard.auth.users_exist", return_value=True)
    import dashboard.server as server
    fake_conn = mocker.MagicMock()
    fake_conn.connect = mocker.MagicMock(return_value=None)
    fake_conn.close = mocker.MagicMock(return_value=None)
    fake_conn.fetch_one.return_value = None
    fake_conn.fetch_all.return_value = []
    mocker.patch("dashboard.server.SQLServerConnection", return_value=fake_conn)
    mocker.patch("database.connection.SQLServerConnection", return_value=fake_conn)
    app = server.create_app()
    app.config["TESTING"] = True
    return app


def test_api_blocked_without_key(authed_app, mocker):
    mocker.patch("dashboard.auth._load_user_from_cookie", return_value=None)
    mocker.patch("dashboard.auth._admin_break_glass_user", return_value=None)
    client = authed_app.test_client()
    rv = client.get("/api/runtime-flags")
    assert rv.status_code == 401


def test_api_allowed_with_header(authed_app, mocker):
    # Legacy X-API-Key maps to admin via break-glass.
    mocker.patch("dashboard.auth._load_user_from_cookie", return_value=None)
    mocker.patch(
        "dashboard.auth._admin_break_glass_user",
        return_value=dict(DASHBOARD_ADMIN_USER),
    )
    flag = mocker.MagicMock()
    flag.key = "kill_switch"
    flag.value = False
    flag.type = "bool"
    flag.description = "test"
    flag.last_modified = None
    flag.modified_by = "ui"
    mock_repo = mocker.MagicMock()
    mock_repo.all.return_value = [flag]
    mocker.patch("database.runtime_flags.RuntimeFlagsRepo", return_value=mock_repo)
    client = authed_app.test_client()
    rv = client.get("/api/runtime-flags", headers={"X-API-Key": "test-secret-key"})
    assert rv.status_code == 200


def test_health_always_public(authed_app):
    client = authed_app.test_client()
    rv = client.get("/health")
    assert rv.status_code == 200


def test_login_form_works_with_app_user(authed_app, mocker):
    mocker.patch("dashboard.auth._load_user_from_cookie", return_value=None)
    mocker.patch("dashboard.auth._admin_break_glass_user", return_value=None)
    mocker.patch(
        "lifecycle.bootstrap_admin.ensure_multi_user_bootstrap",
        return_value=None,
    )
    mocker.patch(
        "database.app_user_repo.AppUserRepo.authenticate",
        return_value=dict(DASHBOARD_ADMIN_USER),
    )
    client = authed_app.test_client()
    rv = client.post(
        "/dashboard-login",
        data={"login": "admin", "password": "secret"},
        follow_redirects=False,
    )
    assert rv.status_code == 302
