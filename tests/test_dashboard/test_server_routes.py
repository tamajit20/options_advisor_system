"""Coverage for remaining dashboard/server.py routes."""
from __future__ import annotations

import json
from datetime import date, datetime
from unittest.mock import MagicMock

import pytest

import dashboard.server as server


@pytest.fixture
def app(mocker):
    fake_conn = MagicMock()
    fake_conn.connect = MagicMock(return_value=None)
    fake_conn.close = MagicMock(return_value=None)
    fake_conn.commit = MagicMock(return_value=None)
    fake_conn.fetch_all = MagicMock(return_value=[])
    fake_conn.fetch_one = MagicMock(return_value=None)
    mocker.patch("dashboard.server.SQLServerConnection", return_value=fake_conn)
    app = server.create_app()
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    return app.test_client()


# ---------------------------------------------------------------------------
class TestIndexRoute:
    def test_index_renders_html(self, client):
        resp = client.get("/")
        assert resp.status_code == 200


class TestHealth:
    def test_health_ok(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "ok"


# ---------------------------------------------------------------------------
class TestTradeDetail:
    def test_404_when_missing(self, client, mocker):
        mocker.patch("dashboard.server.TradeRepo.get", return_value=None)
        resp = client.get("/api/trades/TRD-X")
        assert resp.status_code == 404

    def test_returns_trade_with_legs(self, client, mocker):
        mocker.patch("dashboard.server.TradeRepo.get",
                     return_value={"trade_id": "TRD-1",
                                   "executed_on": datetime(2026, 5, 4)})
        mocker.patch("dashboard.server.TradeRepo.legs",
                     return_value=[{"leg_order": 1}])
        resp = client.get("/api/trades/TRD-1")
        assert resp.status_code == 200
        assert resp.get_json()["trade"]["trade_id"] == "TRD-1"


class TestResuggest:
    def test_returns_400_on_value_error(self, client, mocker):
        mocker.patch("dashboard.server.generate_resuggestion",
                     side_effect=ValueError("Unknown trade"))
        resp = client.post("/api/trades/TRD-X/resuggest")
        assert resp.status_code == 400

    def test_returns_inserted_status(self, client, mocker):
        mocker.patch("dashboard.server.generate_resuggestion", return_value=True)
        resp = client.post("/api/trades/TRD-1/resuggest")
        assert resp.status_code == 200
        assert resp.get_json()["inserted"] is True


class TestRemainingExecutedLegs:
    def test_remaining_filters_unexecuted(self, client, mocker):
        mocker.patch("dashboard.server.TradeRepo.legs_with_suggestion_info",
                     return_value=[{"leg_order": 1, "executed": True},
                                   {"leg_order": 2, "executed": False}])
        resp = client.get("/api/trades/TRD-1/remaining-legs")
        assert resp.status_code == 200
        assert len(resp.get_json()["legs"]) == 1

    def test_executed_filters_executed(self, client, mocker):
        mocker.patch("dashboard.server.TradeRepo.legs_with_suggestion_info",
                     return_value=[{"leg_order": 1, "executed": True},
                                   {"leg_order": 2, "executed": False}])
        resp = client.get("/api/trades/TRD-1/executed-legs")
        assert resp.status_code == 200
        assert len(resp.get_json()["legs"]) == 1


class TestSupplementRoute:
    def test_returns_400_on_value_error(self, client, mocker):
        mocker.patch("dashboard.server.supplement_trade",
                     side_effect=ValueError("unknown"))
        resp = client.post("/api/trades/TRD-X/supplement",
                            data=json.dumps({"fills": []}),
                            content_type="application/json")
        assert resp.status_code == 400

    def test_ok_on_success(self, client, mocker):
        mocker.patch("dashboard.server.supplement_trade", return_value=None)
        resp = client.post("/api/trades/TRD-1/supplement",
                            data=json.dumps({"fills": [
                                {"leg_order": 1, "executed": True, "fill_price": 50}
                            ]}),
                            content_type="application/json")
        assert resp.status_code == 200


class TestCloseRoute:
    def test_returns_400_on_value_error(self, client, mocker):
        mocker.patch("dashboard.server.close_trade_with_fills",
                     side_effect=ValueError("no legs"))
        resp = client.post("/api/trades/TRD-X/close",
                            data=json.dumps({"exits": []}),
                            content_type="application/json")
        assert resp.status_code == 400

    def test_ok_on_success(self, client, mocker):
        mocker.patch("dashboard.server.close_trade_with_fills", return_value=None)
        resp = client.post("/api/trades/TRD-1/close",
                            data=json.dumps({"exits": [
                                {"leg_order": 1, "exit_price": 25.0}
                            ]}),
                            content_type="application/json")
        assert resp.status_code == 200


class TestVoidTrade:
    def test_404_when_missing(self, client, mocker):
        mocker.patch("dashboard.server.TradeRepo.get", return_value=None)
        resp = client.delete("/api/trades/TRD-X")
        assert resp.status_code == 404

    def test_voids_and_commits(self, client, mocker):
        mocker.patch("dashboard.server.TradeRepo.get",
                     return_value={"trade_id": "TRD-1"})
        void = mocker.patch("dashboard.server.TradeRepo.void_trade")
        resp = client.delete("/api/trades/TRD-1")
        assert resp.status_code == 200
        void.assert_called_once_with("TRD-1")


class TestMonitorPatch:
    def test_updates_monitor(self, client, mocker):
        upd = mocker.patch("dashboard.server.TradeRepo.update_monitor")
        resp = client.patch("/api/trades/TRD-1/monitor",
                             data=json.dumps({"actual_stop_loss_level": 23250.0,
                                              "spot_at_execution": 23000.0}),
                             content_type="application/json")
        assert resp.status_code == 200
        upd.assert_called_once()


class TestCloseSuggestion:
    def test_404_when_missing(self, client, mocker):
        mocker.patch("dashboard.server.TradeRepo.get", return_value=None)
        resp = client.get("/api/trades/TRD-X/close-suggestion")
        assert resp.status_code == 404

    def test_empty_when_no_executed_legs(self, client, mocker):
        mocker.patch("dashboard.server.TradeRepo.get",
                     return_value={"trade_id": "TRD-1"})
        mocker.patch("dashboard.server.TradeRepo.legs_with_suggestion_info",
                     return_value=[{"leg_order": 1, "executed": False}])
        resp = client.get("/api/trades/TRD-1/close-suggestion")
        assert resp.status_code == 200
        assert resp.get_json()["legs"] == []

    def test_computes_close_with_chain(self, client, mocker):
        mocker.patch("dashboard.server.TradeRepo.get",
                     return_value={"trade_id": "TRD-1"})
        mocker.patch("dashboard.server.TradeRepo.legs_with_suggestion_info",
                     return_value=[
                         {"leg_order": 1, "executed": True, "symbol": "NIFTY",
                          "expiry_date": date(2026, 5, 14),
                          "strike": 23200.0, "option_type": "CE",
                          "action": "SELL", "fill_price": 50.0,
                          "lots": 1, "lots_actual": 1, "lot_size": 75},
                     ])
        mocker.patch("dashboard.server.FoEodRepo.get_chain",
                     return_value=[{"strike": 23200.0, "option_type": "CE",
                                    "settle_price": 25.0}])
        resp = client.get("/api/trades/TRD-1/close-suggestion")
        assert resp.status_code == 200
        data = resp.get_json()
        # SELL @ 50, close @ 25 → est = (50-25)*75 = 1875
        assert data["est_gross_pnl"] == pytest.approx(1875.0)


class TestHistoryRoutes:
    def test_history_paired(self, client, mocker, app):
        # Override fetch_all on the underlying connection mock
        with app.app_context():
            pass
        # Patch the connection's fetch_all via SQLServerConnection patch
        fake = MagicMock()
        fake.connect = MagicMock()
        fake.close = MagicMock()
        fake.fetch_all = MagicMock(return_value=[])
        mocker.patch("dashboard.server.SQLServerConnection", return_value=fake)
        # Re-create app so the new mock takes effect
        new_app = server.create_app()
        new_app.config["TESTING"] = True
        c = new_app.test_client()
        resp = c.get("/api/history/paired")
        assert resp.status_code == 200

    def test_history_closed_trades(self, client, mocker):
        fake = MagicMock()
        fake.connect = MagicMock()
        fake.close = MagicMock()
        fake.fetch_all = MagicMock(return_value=[])
        mocker.patch("dashboard.server.SQLServerConnection", return_value=fake)
        new_app = server.create_app()
        new_app.config["TESTING"] = True
        c = new_app.test_client()
        resp = c.get("/api/history/closed-trades?days=30")
        assert resp.status_code == 200

    def test_history_closed_trades_with_dates(self, client, mocker):
        fake = MagicMock()
        fake.connect = MagicMock()
        fake.close = MagicMock()
        fake.fetch_all = MagicMock(return_value=[])
        mocker.patch("dashboard.server.SQLServerConnection", return_value=fake)
        new_app = server.create_app()
        new_app.config["TESTING"] = True
        c = new_app.test_client()
        resp = c.get("/api/history/closed-trades?from_date=2026-01-01&to_date=2026-04-30")
        assert resp.status_code == 200

    def test_history_closed_trades_invalid_dates(self, client, mocker):
        fake = MagicMock()
        fake.connect = MagicMock()
        fake.close = MagicMock()
        fake.fetch_all = MagicMock(return_value=[])
        mocker.patch("dashboard.server.SQLServerConnection", return_value=fake)
        new_app = server.create_app()
        new_app.config["TESTING"] = True
        c = new_app.test_client()
        resp = c.get("/api/history/closed-trades?from_date=BAD&to_date=BAD")
        assert resp.status_code == 200

    def test_history_simulation(self, client, mocker):
        mocker.patch("dashboard.server.SimulationRepo.get_summary", return_value=None)
        mocker.patch("dashboard.server.SimulationRepo.get_legs", return_value=[])
        resp = client.get("/api/history/simulation/SUG-1")
        assert resp.status_code == 200


class TestLogsLevelCounts:
    def test_returns_counts(self, client, mocker):
        mocker.patch("dashboard.server.LogRepo.counts_by_level",
                     return_value={"INFO": 10, "ERROR": 2})
        resp = client.get("/api/logs/level-counts?hours=24")
        assert resp.status_code == 200
        assert resp.get_json()["INFO"] == 10


class TestJobsLatest:
    def test_returns_latest(self, client, mocker):
        mocker.patch("dashboard.server.JobLogRepo.latest_status_per_job",
                     return_value=[{"job_name": "fo_bhav", "status": "SUCCESS"}])
        resp = client.get("/api/jobs/latest")
        assert resp.status_code == 200


class TestConfigRoutes:
    def test_list(self, client, mocker):
        mocker.patch("dashboard.server.ConfigRepo.get_all", return_value=[])
        resp = client.get("/api/config")
        assert resp.status_code == 200

    def test_get(self, client, mocker):
        mocker.patch("dashboard.server.ConfigRepo.get", return_value="x")
        resp = client.get("/api/config/foo")
        assert resp.status_code == 200
        assert resp.get_json()["value"] == "x"

    def test_set_400_when_missing_value(self, client, mocker):
        resp = client.put("/api/config/foo",
                           data=json.dumps({}),
                           content_type="application/json")
        assert resp.status_code == 400

    def test_set_ok(self, client, mocker):
        mocker.patch("dashboard.server.ConfigRepo.set")
        resp = client.put("/api/config/foo",
                           data=json.dumps({"value": "bar"}),
                           content_type="application/json")
        assert resp.status_code == 200


class TestNotifications:
    def test_recent(self, client, mocker):
        mocker.patch("dashboard.server.NotificationRepo.recent",
                     return_value=[])
        resp = client.get("/api/notifications")
        assert resp.status_code == 200

    def test_unread(self, client, mocker):
        mocker.patch("dashboard.server.NotificationRepo.unread", return_value=[])
        resp = client.get("/api/notifications?unread=1")
        assert resp.status_code == 200

    def test_mark_read(self, client, mocker):
        mocker.patch("dashboard.server.NotificationRepo.mark_read")
        resp = client.post("/api/notifications/5/read")
        assert resp.status_code == 200

    def test_read_all(self, client, mocker):
        mocker.patch("dashboard.server.NotificationRepo.mark_all_read")
        resp = client.post("/api/notifications/read-all")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
class TestJobsList:
    def test_returns_jobs(self, client, mocker):
        mocker.patch("dashboard.server.JobLogRepo.latest_status_per_job",
                     return_value=[])
        # Mock scheduler to return None (not running)
        import scheduler.scheduler as sched
        mocker.patch.object(sched, "_SCHEDULER", None)
        resp = client.get("/api/jobs/list")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "jobs" in data
        assert data["scheduler_running"] is False


class TestJobsTrigger:
    def test_unknown_job_returns_400(self, client):
        resp = client.post("/api/jobs/no_such_job/trigger")
        assert resp.status_code == 400

    def test_running_returns_409(self, client, mocker):
        mocker.patch("dashboard.server.JobLogRepo.last_status",
                     return_value="RUNNING")
        resp = client.post("/api/jobs/fo_bhav_download/trigger")
        assert resp.status_code == 409

    def test_503_when_scheduler_not_running(self, client, mocker):
        mocker.patch("dashboard.server.JobLogRepo.last_status", return_value=None)
        # trigger_job_now will raise RuntimeError
        mocker.patch("scheduler.scheduler.trigger_job_now",
                     side_effect=RuntimeError("not running"))
        resp = client.post("/api/jobs/fo_bhav_download/trigger")
        assert resp.status_code == 503

    def test_invalid_trade_date_returns_400(self, client, mocker):
        mocker.patch("dashboard.server.JobLogRepo.last_status", return_value=None)
        resp = client.post("/api/jobs/fo_bhav_download/trigger",
                            data=json.dumps({"trade_date": "BAD"}),
                            content_type="application/json")
        assert resp.status_code == 400

    def test_queued_on_success(self, client, mocker):
        mocker.patch("dashboard.server.JobLogRepo.last_status", return_value=None)
        mocker.patch("scheduler.scheduler.trigger_job_now", return_value=True)
        resp = client.post("/api/jobs/fo_bhav_download/trigger")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "queued"


class TestJobsHistory:
    def test_returns_runs(self, client, mocker):
        fake = MagicMock()
        fake.connect = MagicMock()
        fake.close = MagicMock()
        fake.fetch_all = MagicMock(return_value=[
            {"job_id": "fo-1", "job_name": "fo_bhav", "status": "SUCCESS"}
        ])
        mocker.patch("dashboard.server.SQLServerConnection", return_value=fake)
        new_app = server.create_app()
        new_app.config["TESTING"] = True
        c = new_app.test_client()
        resp = c.get("/api/jobs/fo_bhav_download/history")
        assert resp.status_code == 200
        assert len(resp.get_json()["runs"]) == 1


class TestSummarizeCron:
    def test_empty_returns_empty(self):
        assert server._summarize_cron({}) == ""

    def test_daily_with_time(self):
        out = server._summarize_cron({"hour": 9, "minute": 30})
        assert "Daily" in out and "09:30" in out

    def test_with_day_of_week(self):
        out = server._summarize_cron({"hour": 9, "minute": 30, "day_of_week": "mon,fri"})
        assert "Mon" in out and "Fri" in out
