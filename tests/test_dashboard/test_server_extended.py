"""Extended dashboard server route tests (stats, history, runtime flags, ws monitor)."""

from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import MagicMock

import pytest

import dashboard.server as server


@pytest.fixture
def client_with_db(mocker):
    fake_conn = MagicMock()
    fake_conn.fetch_one.return_value = None
    fake_conn.fetch_all.return_value = []
    mocker.patch("dashboard.server.SQLServerConnection", return_value=fake_conn)
    app = server.create_app()
    app.config["TESTING"] = True
    return app.test_client(), fake_conn


class TestHistoryAndStatsRoutes:
    def test_history_trades_empty(self, client_with_db):
        client, conn = client_with_db
        conn.fetch_all.return_value = []
        resp = client.get("/api/history/trades")
        assert resp.status_code == 200
        assert resp.get_json()["trades"] == []

    def test_pnl_timeline_empty(self, client_with_db):
        client, conn = client_with_db
        conn.fetch_all.return_value = []
        resp = client.get("/api/stats/pnl-timeline")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["trades"] == []
        sql, params = conn.fetch_all.call_args[0]
        assert "CONVERT(date" not in sql
        assert params == []

    def test_pnl_timeline_applies_date_filters(self, client_with_db):
        client, conn = client_with_db
        conn.fetch_all.return_value = []
        resp = client.get("/api/stats/pnl-timeline?from_date=2026-08-01&to_date=2026-08-15")
        assert resp.status_code == 200
        sql, params = conn.fetch_all.call_args[0]
        assert "CONVERT(date, t.closed_on) >= ?" in sql
        assert "CONVERT(date, t.closed_on) <= ?" in sql
        assert params == ["2026-08-01", "2026-08-15"]

    def test_pnl_timeline_swaps_inverted_dates(self, client_with_db):
        client, conn = client_with_db
        conn.fetch_all.return_value = []
        client.get("/api/stats/pnl-timeline?from_date=2026-08-20&to_date=2026-08-01")
        _sql, params = conn.fetch_all.call_args[0]
        assert params == ["2026-08-01", "2026-08-20"]

    def test_strategy_performance_empty(self, client_with_db):
        client, conn = client_with_db
        conn.fetch_all.return_value = []
        resp = client.get("/api/stats/strategy-performance")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["strategies"] == []
        assert data["overall"]["total"] == 0

    def test_strategy_performance_applies_date_filters(self, client_with_db):
        client, conn = client_with_db
        conn.fetch_all.return_value = []
        resp = client.get("/api/stats/strategy-performance?from_date=2026-07-01&to_date=2026-07-31")
        assert resp.status_code == 200
        sql, params = conn.fetch_all.call_args[0]
        assert "CONVERT(date, COALESCE(t.closed_on, t.executed_on)) >= ?" in sql
        assert params == ["2026-07-01", "2026-07-31"]


class TestRuntimeFlagsRoutes:
    def test_runtime_flags_list(self, client_with_db, mocker):
        client, _ = client_with_db
        flag = MagicMock()
        flag.key = "kill_switch"
        flag.value = False
        flag.type = "bool"
        flag.description = "test"
        flag.last_modified = datetime(2026, 8, 12, 10, 0, 0)
        flag.modified_by = "ui"
        mock_repo = MagicMock()
        mock_repo.all.return_value = [flag]
        mocker.patch("database.runtime_flags.RuntimeFlagsRepo", return_value=mock_repo)
        resp = client.get("/api/runtime-flags")
        assert resp.status_code == 200
        flags = resp.get_json()["flags"]
        assert flags[0]["key"] == "kill_switch"

    def test_runtime_flags_set_missing_value(self, client_with_db):
        client, _ = client_with_db
        resp = client.post("/api/runtime-flags/kill_switch", json={})
        assert resp.status_code == 400

    def test_runtime_flags_set_success(self, client_with_db, mocker):
        client, _ = client_with_db
        mock_repo = MagicMock()
        mocker.patch("database.runtime_flags.RuntimeFlagsRepo", return_value=mock_repo)
        resp = client.post("/api/runtime-flags/kill_switch", json={"value": True})
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True


class TestWsMonitorRoute:
    def test_ws_monitor_filters_by_topic(self, client_with_db, tmp_path, monkeypatch):
        client, _ = client_with_db
        snap = {
            "connection_state": "connected",
            "recent_events": [
                {"topic": "tick.options", "symbol": "NIFTY", "last_price": 150},
                {"topic": "tick.index", "symbol": "BANKNIFTY", "last_price": 48000},
                {"topic": "tick.index", "symbol": "NIFTY", "last_price": 23000},
            ],
        }
        path = tmp_path / "ws_status.json"
        path.write_text(json.dumps(snap), encoding="utf-8")
        monkeypatch.setattr("providers.ws_monitor.default_snapshot_path", lambda: path)
        resp = client.get("/api/ws/monitor?topic=tick.options")
        assert resp.status_code == 200
        events = resp.get_json()["recent_events"]
        assert len(events) == 1
        assert events[0]["symbol"] == "NIFTY"

    def test_ws_monitor_unavailable_when_no_file(self, client_with_db, tmp_path, monkeypatch):
        client, _ = client_with_db
        monkeypatch.setattr(
            "providers.ws_monitor.default_snapshot_path",
            lambda: tmp_path / "missing.json",
        )
        resp = client.get("/api/ws/monitor")
        assert resp.status_code == 200
        assert resp.get_json()["available"] is False
