"""Dashboard tests for Zerodha profile / margin in status and execution APIs."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def dash_app(mocker):
    import dashboard.server as server

    fake_conn = MagicMock()
    fake_conn.connect = MagicMock(return_value=None)
    fake_conn.close = MagicMock(return_value=None)
    fake_conn.fetch_one.return_value = None
    fake_conn.fetch_all.return_value = []
    mocker.patch("dashboard.server.SQLServerConnection", return_value=fake_conn)
    app = server.create_app()
    app.config["TESTING"] = True
    return app


@pytest.fixture
def dash_client(dash_app):
    return dash_app.test_client()


def _valid_session():
    s = MagicMock()
    s.user_id = "AB1234"
    s.generated_at = datetime(2026, 9, 5, 8, 0, 0)
    s.access_token = "tok"
    return s


def test_zerodha_status_includes_account_when_valid(dash_client, mocker):
    mocker.patch("providers.zerodha.session.load_session", return_value=_valid_session())
    mocker.patch("providers.zerodha.session.is_token_valid", return_value=True)
    mocker.patch("providers.zerodha.session.token_valid_until", return_value=datetime(2026, 9, 6, 6, 0, 0))
    mocker.patch(
        "dashboard.server._zerodha_account_for_api",
        return_value={
            "available": True,
            "user_name": "Test User",
            "usable_balance": 118500.25,
            "available_cash": 120000.0,
            "net": 150000.0,
        },
    )
    resp = dash_client.get("/api/zerodha/status")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["account"]["available"] is True
    assert body["account"]["user_name"] == "Test User"
    assert body["account"]["usable_balance"] == 118500.25


def test_zerodha_status_refresh_account_query(dash_client, mocker):
    mocker.patch("providers.zerodha.session.load_session", return_value=_valid_session())
    mocker.patch("providers.zerodha.session.is_token_valid", return_value=True)
    mocker.patch("providers.zerodha.session.token_valid_until", return_value=datetime(2026, 9, 6, 6, 0, 0))
    account_fn = mocker.patch(
        "dashboard.server._zerodha_account_for_api",
        return_value={"available": True, "usable_balance": 100.0},
    )
    dash_client.get("/api/zerodha/status?refresh_account=1")
    account_fn.assert_called_once_with(force_refresh=True)
