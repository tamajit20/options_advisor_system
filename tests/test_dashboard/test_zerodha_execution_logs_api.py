"""Dashboard API tests for /api/zerodha/execution-logs retention and listing."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def logs_app(mocker):
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
def logs_client(logs_app):
    return logs_app.test_client()


def test_execution_logs_returns_hot_archive_retention(logs_client, mocker):
    list_since = mocker.patch(
        "database.broker_order_repo.BrokerOrderRepo.list_since",
        return_value=[],
    )
    resp = logs_client.get("/api/zerodha/execution-logs")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["retention_days"] == 365
    assert data["executions"] == []
    assert "since" in data
    list_since.assert_called_once()


def test_execution_logs_queries_since_hot_archive_window(logs_client, mocker):
    from datetime import timedelta

    from utils import now_ist

    list_since = mocker.patch(
        "database.broker_order_repo.BrokerOrderRepo.list_since",
        return_value=[],
    )
    before = now_ist()
    logs_client.get("/api/zerodha/execution-logs")
    since = list_since.call_args[0][0]
    after = now_ist()

    min_since = before - timedelta(days=366)
    max_since = after - timedelta(days=364)
    assert min_since <= since <= max_since


def test_execution_logs_groups_rows(logs_client, mocker):
    mocker.patch(
        "database.broker_order_repo.BrokerOrderRepo.list_since",
        return_value=[
            {
                "id": 1,
                "operation": "ENTRY",
                "suggestion_id": "SUG-1",
                "trade_id": "TRD-1",
                "leg_order": 1,
                "status": "COMPLETE",
                "tradingsymbol": "NIFTY26MAR25000CE",
                "transaction_type": "SELL",
                "quantity": 50,
                "created_at": "2026-09-01T10:00:00",
            },
        ],
    )
    mocker.patch(
        "database.models.TradeRepo.get",
        return_value={"trade_name": "Test trade"},
    )
    resp = logs_client.get("/api/zerodha/execution-logs")
    data = resp.get_json()
    assert len(data["executions"]) == 1
    assert data["executions"][0]["trade_name"] == "Test trade"
