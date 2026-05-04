"""Tests for dashboard/server.py — Flask test client with patched DB.

We patch SQLServerConnection at the module level so each request gets a
MagicMock instead of a real connection. Repo methods are then patched per-test
to return fixture data.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from unittest.mock import MagicMock

import pytest

import dashboard.server as server


@pytest.fixture
def app(mocker):
    """Patch SQLServerConnection so _with_db never opens a real DB connection."""
    fake_conn = MagicMock()
    fake_conn.connect = MagicMock(return_value=None)
    fake_conn.close = MagicMock(return_value=None)
    mocker.patch("dashboard.server.SQLServerConnection", return_value=fake_conn)
    app = server.create_app()
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    return app.test_client()


# ---------------------------------------------------------------------------
# Helpers and JSON encoders
# ---------------------------------------------------------------------------
class TestJsonHelpers:
    def test_ist_iso_handles_datetime(self):
        out = server._ist_iso(datetime(2026, 5, 4, 10, 30, 0))
        assert out == "2026-05-04 10:30:00"

    def test_ist_iso_handles_date(self):
        assert server._ist_iso(date(2026, 5, 4)) == "2026-05-04"

    def test_ist_iso_handles_none(self):
        assert server._ist_iso(None) is None

    def test_row_serialises_datetimes(self):
        out = server._row({
            "trade_date": date(2026, 5, 4),
            "generated_on": datetime(2026, 5, 4, 9, 30),
            "name": "x",
            "score": 75,
        })
        assert out["trade_date"] == "2026-05-04"
        assert out["generated_on"] == "2026-05-04 09:30:00"
        assert out["name"] == "x"
        assert out["score"] == 75


# ---------------------------------------------------------------------------
# Routes — smoke + behaviour
# ---------------------------------------------------------------------------
class TestApiTheme:
    def test_returns_theme_config(self, client):
        resp = client.get("/api/theme")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, dict)


class TestApiSuggestionToday:
    def test_empty_suggestions(self, client, mocker):
        mocker.patch("dashboard.server.SuggestionRepo.active_pending", return_value=[])
        resp = client.get("/api/suggestion/today")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data == {"suggestions": []}

    def test_returns_suggestion_with_legs(self, client, mocker):
        sug_row = {
            "suggestion_id": "SUG-1", "underlying": "NIFTY",
            "strategy": "IRON_CONDOR", "status": "PENDING",
            "generated_on": datetime(2026, 5, 4, 9, 0),
            "expiry_date": date(2026, 5, 14),
            "net_credit_suggested": 250.0,
        }
        leg_row = {
            "leg_order": 1, "strike": 23200.0, "option_type": "CE",
            "action": "SELL", "lots": 1, "lot_size": 75,
            "suggested_price": 50.0,
        }
        mocker.patch("dashboard.server.SuggestionRepo.active_pending",
                     return_value=[sug_row])
        mocker.patch("dashboard.server.SuggestionRepo.legs", return_value=[leg_row])
        resp = client.get("/api/suggestion/today")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["suggestions"]) == 1
        s = data["suggestions"][0]
        assert s["suggestion_id"] == "SUG-1"
        # net_credit_suggested renamed to net_credit
        assert "net_credit" in s
        assert "net_credit_suggested" not in s
        assert len(s["legs"]) == 1


class TestApiTradesOpen:
    def test_empty(self, client, mocker):
        mocker.patch("dashboard.server.TradeRepo.open_trades", return_value=[])
        resp = client.get("/api/trades/open")
        assert resp.status_code == 200
        # may return {"trades": []} or similar
        data = resp.get_json()
        assert data is not None


class TestApiHistorySuggestions:
    def test_returns_array(self, client, mocker):
        mocker.patch("dashboard.server.SuggestionRepo.by_date", return_value=[])
        resp = client.get("/api/history/suggestions")
        assert resp.status_code == 200


class TestApiLogs:
    def test_logs_endpoint_returns_200(self, client, mocker):
        mocker.patch("dashboard.server.LogRepo.fetch", return_value=[])
        resp = client.get("/api/logs")
        assert resp.status_code == 200


class TestApiMarkExecuted:
    def test_400_on_invalid_payload(self, client, mocker):
        """If mark_executed raises ValueError, we expect 400."""
        mocker.patch("dashboard.server.mark_executed",
                     side_effect=ValueError("missing fills"))
        resp = client.post(
            "/api/suggestion/SUG-1/mark-executed",
            data=json.dumps({"fills": [{"leg_order": 1, "executed": True,
                                        "fill_price": 50.0}]}),
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert "error" in resp.get_json()

    def test_returns_trade_id_on_success(self, client, mocker):
        mocker.patch("dashboard.server.mark_executed", return_value="TRD-001")
        resp = client.post(
            "/api/suggestion/SUG-1/mark-executed",
            data=json.dumps({
                "fills": [{"leg_order": 1, "executed": True, "fill_price": 50.0}],
                "spot_at_execution": 23000.0,
            }),
            content_type="application/json",
        )
        assert resp.status_code == 200
        assert resp.get_json()["trade_id"] == "TRD-001"


class TestApiSystemStatus:
    def test_returns_status_keys(self, client, mocker):
        # Stub RuntimeFlagsRepo to avoid touching the DB
        mocker.patch(
            "database.runtime_flags.RuntimeFlagsRepo.get_bool",
            side_effect=lambda key, default=False: {
                "circuit_breaker_active": True,
                "kill_switch": False,
                "trade_execution_enabled": True,
            }.get(key, default),
        )
        resp = client.get("/api/system-status")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["circuit_breaker_active"] is True
        assert body["kill_switch"] is False
        assert body["trade_execution_enabled"] is True
        assert "scheduler_running" in body

    def test_fail_open_when_runtime_flags_raise(self, client, mocker):
        mocker.patch(
            "database.runtime_flags.RuntimeFlagsRepo.get_bool",
            side_effect=RuntimeError("table missing"),
        )
        resp = client.get("/api/system-status")
        assert resp.status_code == 200
        body = resp.get_json()
        # Defaults applied — endpoint must not 500
        assert body["circuit_breaker_active"] is False
        assert body["kill_switch"] is False
        assert body["trade_execution_enabled"] is True


# ---------------------------------------------------------------------------
# Future-scope placeholders for routes not yet covered
# ---------------------------------------------------------------------------
@pytest.mark.future
@pytest.mark.skip(reason="future: dashboard close-trade flow with leg fills "
                  "(FUTURE_ENHANCEMENT_SCOPES.md → Code Quality)")
def test_close_trade_persists_exit_fills():
    """POST /api/trades/<id>/close should persist exit fills + transition status."""
    pass


@pytest.mark.future
@pytest.mark.skip(reason="future: dashboard supplement-trade flow "
                  "(FUTURE_ENHANCEMENT_SCOPES.md → Code Quality)")
def test_supplement_adds_remaining_legs():
    """POST /api/trades/<id>/supplement adds previously-unfilled legs."""
    pass


@pytest.mark.future
@pytest.mark.skip(reason="future: dashboard config GET/PATCH endpoints "
                  "(FUTURE_ENHANCEMENT_SCOPES.md → Code Quality)")
def test_config_get_and_patch():
    """Config tab: GET returns current overrides, PATCH writes a new one."""
    pass
