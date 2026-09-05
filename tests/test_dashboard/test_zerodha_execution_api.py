"""Dashboard API tests for Zerodha broker execution routes."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from lifecycle.zerodha_executor import ExecutionOutcome, ZerodhaExecutionError


@pytest.fixture
def exec_app(mocker):
    """App with dashboard auth enabled and mocked DB."""
    mocker.patch.dict(
        "config.DASHBOARD_CONFIG",
        {"api_key": "test-secret-key"},
        clear=False,
    )
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
def exec_client(exec_app):
    return exec_app.test_client()


@pytest.fixture
def api_app(mocker):
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
def api_client(api_app):
    return api_app.test_client()


def test_zerodha_execute_blocked_without_dashboard_api_key(mocker, api_client):
    mocker.patch(
        "lifecycle.zerodha_executor.zerodha_execution_config_enabled",
        return_value=True,
    )
    mocker.patch(
        "lifecycle.zerodha_executor.zerodha_execution_ready",
        return_value=True,
    )
    resp = api_client.post("/api/suggestion/SUG-1/zerodha-execute", json={})
    assert resp.status_code == 503
    assert "OPT_DASHBOARD_API_KEY" in resp.get_json()["error"]


def test_zerodha_execute_requires_auth_when_key_configured(exec_client, mocker):
    mocker.patch(
        "lifecycle.zerodha_executor.zerodha_execution_config_enabled",
        return_value=True,
    )
    mocker.patch(
        "lifecycle.zerodha_executor.zerodha_execution_ready",
        return_value=True,
    )
    resp = exec_client.post("/api/suggestion/SUG-1/zerodha-execute", json={})
    assert resp.status_code == 401


def test_zerodha_execute_success(exec_client, mocker):
    mocker.patch(
        "lifecycle.zerodha_executor.zerodha_execution_config_enabled",
        return_value=True,
    )
    mocker.patch(
        "lifecycle.zerodha_executor.zerodha_execution_ready",
        return_value=True,
    )
    mocker.patch(
        "lifecycle.zerodha_executor.execute_suggestion_in_zerodha",
        return_value=ExecutionOutcome(
            ok=True,
            trade_id="TRD-1",
            message="ok",
            leg_fills=[],
        ),
    )
    resp = exec_client.post(
        "/api/suggestion/SUG-1/zerodha-execute",
        json={"async": False},
        headers={"X-API-Key": "test-secret-key"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["trade_id"] == "TRD-1"


def test_zerodha_execute_returns_400_on_execution_error(exec_client, mocker):
    mocker.patch(
        "lifecycle.zerodha_executor.zerodha_execution_config_enabled",
        return_value=True,
    )
    mocker.patch(
        "lifecycle.zerodha_executor.zerodha_execution_ready",
        return_value=True,
    )
    mocker.patch(
        "lifecycle.zerodha_executor.execute_suggestion_in_zerodha",
        side_effect=ZerodhaExecutionError("Execution blocked: stale"),
    )
    resp = exec_client.post(
        "/api/suggestion/SUG-1/zerodha-execute",
        json={"async": False},
        headers={"X-API-Key": "test-secret-key"},
    )
    assert resp.status_code == 400
    assert "stale" in resp.get_json()["error"]


def test_zerodha_execute_not_ready_returns_403(exec_client, mocker):
    mocker.patch(
        "lifecycle.zerodha_executor.zerodha_execution_config_enabled",
        return_value=True,
    )
    mocker.patch(
        "lifecycle.zerodha_executor.zerodha_execution_ready",
        return_value=False,
    )
    resp = exec_client.post(
        "/api/suggestion/SUG-1/zerodha-execute",
        json={},
        headers={"X-API-Key": "test-secret-key"},
    )
    assert resp.status_code == 403


def test_open_trades_include_execution_channel(mocker, api_client):
    mocker.patch(
        "lifecycle.zerodha_executor.trade_execution_channel",
        return_value="zerodha",
    )
    mocker.patch(
        "dashboard.server.TradeRepo",
    )
    trade_repo = mocker.patch("dashboard.server.TradeRepo").return_value
    trade_repo.open_trades.return_value = [{
        "trade_id": "TRD-1",
        "trade_name": "Test",
        "suggestion_id": "SUG-1",
        "status": "OPEN",
        "daily_status": "HOLD",
    }]
    trade_repo.legs_with_suggestion_info.return_value = []
    mocker.patch("dashboard.server.SuggestionRepo").return_value.get.return_value = None
    mocker.patch("dashboard.server.NotificationRepo").return_value.latest_risk_alert_for_trade.return_value = None
    mocker.patch("dashboard.server._stored_mtm_payloads", return_value={})
    mocker.patch("dashboard.server._read_live_mtm_state", return_value={})
    mocker.patch("dashboard.server._trade_live_outlook", return_value={})

    resp = api_client.get("/api/trades/open")
    assert resp.status_code == 200
    trades = resp.get_json()["trades"]
    assert len(trades) == 1
    assert trades[0]["execution_channel"] == "zerodha"
