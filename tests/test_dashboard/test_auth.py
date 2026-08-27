"""Tests for dashboard API-key auth."""

from __future__ import annotations

import pytest


@pytest.fixture
def authed_app(mocker):
    mocker.patch.dict(
        "config.DASHBOARD_CONFIG",
        {"api_key": "test-secret-key"},
        clear=False,
    )
    import dashboard.server as server
    fake_conn = mocker.MagicMock()
    fake_conn.connect = mocker.MagicMock(return_value=None)
    fake_conn.close = mocker.MagicMock(return_value=None)
    fake_conn.fetch_one.return_value = None
    fake_conn.fetch_all.return_value = []
    mocker.patch("dashboard.server.SQLServerConnection", return_value=fake_conn)
    app = server.create_app()
    app.config["TESTING"] = True
    return app


def test_api_blocked_without_key(authed_app):
    client = authed_app.test_client()
    rv = client.get("/api/runtime-flags")
    assert rv.status_code == 401


def test_api_allowed_with_header(authed_app):
    client = authed_app.test_client()
    rv = client.get("/api/runtime-flags", headers={"X-API-Key": "test-secret-key"})
    assert rv.status_code == 200


def test_health_always_public(authed_app):
    client = authed_app.test_client()
    rv = client.get("/health")
    assert rv.status_code == 200


def test_login_form_works_with_special_chars(authed_app):
    client = authed_app.test_client()
    rv = client.post(
        "/dashboard-login",
        data={"api_key": "test-secret-key"},
        follow_redirects=False,
    )
    assert rv.status_code == 302
    assert client.get("/").status_code == 200
