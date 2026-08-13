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
    mocker.patch("scout.routes.SQLServerConnection", return_value=fake_conn)
    mocker.patch("database.scout_models.ScoutSignalRepo.last_signal", return_value=None)
    app = server.create_app()
    app.config["TESTING"] = True
    return app


def test_api_blocked_without_key(authed_app):
    client = authed_app.test_client()
    rv = client.get("/api/scout/status")
    assert rv.status_code == 401


def test_api_allowed_with_header(authed_app):
    client = authed_app.test_client()
    rv = client.get("/api/scout/status", headers={"X-API-Key": "test-secret-key"})
    assert rv.status_code == 200


def test_health_always_public(authed_app):
    client = authed_app.test_client()
    rv = client.get("/health")
    assert rv.status_code == 200
