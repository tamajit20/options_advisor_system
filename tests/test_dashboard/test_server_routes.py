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
        html = resp.get_data(as_text=True)
        assert "window.__PNL_RULES__" in html
        assert "window.__CACHE_BUST__" in html
        assert "long_premium_target_base" in html
        assert "CALENDAR_SPREAD" in html
        assert "strategy_guide.js" in html
        assert 'data-tab="learn"' in html
        assert "arb.js" not in html
        assert "basis.js" not in html
        assert "Arb Monitor" not in html
        assert "Basis Monitor" not in html
        assert 'id="perf-from"' in html
        assert 'id="chart-from"' in html


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


class TestGapReplay:
    def test_404_when_missing(self, client, mocker):
        mocker.patch("dashboard.server.replay_gap_for_trade",
                     return_value={"error": "not_found"})
        resp = client.get("/api/trades/TRD-X/gap-replay")
        assert resp.status_code == 404

    def test_returns_replay_payload(self, client, mocker):
        mocker.patch("dashboard.server.replay_gap_for_trade", return_value={
            "trade_id": "TRD-1",
            "has_gap": True,
            "days": [{"date": "2026-06-22", "decision": "SL_HIT"}],
        })
        resp = client.get("/api/trades/TRD-1/gap-replay")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["trade_id"] == "TRD-1"
        assert body["days"][0]["decision"] == "SL_HIT"


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
        mocker.patch("dashboard.server.SpotEodRepo.for_date",
                     return_value={"close_price": 23200.0})
        resp = client.get("/api/trades/TRD-1/close-suggestion")
        assert resp.status_code == 200
        data = resp.get_json()
        # SELL @ 50, close @ 25 → est = (50-25)*75 = 1875
        assert data["est_gross_pnl"] == pytest.approx(1875.0)
        assert data["legs"][0]["price_source"] == "mid"

    def test_substitutes_intrinsic_when_settle_corrupt(self, client, mocker):
        """The exact production bug: TRD-20260506-002 LONG_STRADDLE on NIFTY
        showed est. P&L ₹35 lakh because both 24300 CE and 24300 PE chain
        rows had ``settle_price`` ≈ 23,618 (the spot value, not the option
        premium). The dashboard MUST substitute intrinsic settlement value
        when the chain row is clearly bogus."""
        mocker.patch("dashboard.server.TradeRepo.get",
                     return_value={"trade_id": "TRD-20260506-002"})
        mocker.patch("dashboard.server.TradeRepo.legs_with_suggestion_info",
                     return_value=[
                         {"leg_order": 1, "executed": True, "symbol": "NIFTY",
                          "expiry_date": date(2026, 5, 14),
                          "strike": 24300.0, "option_type": "CE",
                          "action": "BUY", "fill_price": 361.80,
                          "lots": 1, "lots_actual": 1, "lot_size": 75},
                         {"leg_order": 2, "executed": True, "symbol": "NIFTY",
                          "expiry_date": date(2026, 5, 14),
                          "strike": 24300.0, "option_type": "PE",
                          "action": "BUY", "fill_price": 224.25,
                          "lots": 1, "lots_actual": 1, "lot_size": 75},
                     ])
        # Both rows have a corrupt settle ≈ spot.
        mocker.patch("dashboard.server.FoEodRepo.get_chain",
                     return_value=[
                         {"strike": 24300.0, "option_type": "CE",
                          "settle_price": 23618.0},
                         {"strike": 24300.0, "option_type": "PE",
                          "settle_price": 23618.0},
                     ])
        mocker.patch("dashboard.server.SpotEodRepo.for_date",
                     return_value={"close_price": 23618.0})
        resp = client.get("/api/trades/TRD-20260506-002/close-suggestion")
        assert resp.status_code == 200
        data = resp.get_json()
        legs = data["legs"]
        ce_leg = next(l for l in legs if l["option_type"] == "CE")
        pe_leg = next(l for l in legs if l["option_type"] == "PE")
        # CE 24300 with spot 23618 → OTM → intrinsic 0
        assert ce_leg["suggested_close"] == pytest.approx(0.0)
        assert ce_leg["price_source"] == "intrinsic_fallback"
        # PE 24300 with spot 23618 → ITM → intrinsic 24300-23618 = 682
        assert pe_leg["suggested_close"] == pytest.approx(682.0)
        assert pe_leg["price_source"] == "intrinsic_fallback"
        # Gross P&L: CE (0-361.80)*75 + PE (682-224.25)*75 = -27135 + 34331.25 = +7196.25
        assert data["est_gross_pnl"] == pytest.approx(7196.25)

    def test_calendar_spread_uses_per_expiry_chains(self, client, mocker):
        """Same strike CE on two expiries must not share one mid (TRD-20260817-005)."""
        near, far = date(2026, 8, 25), date(2026, 9, 29)
        mocker.patch("dashboard.server.TradeRepo.get",
                     return_value={"trade_id": "TRD-20260817-005"})
        mocker.patch("dashboard.server.TradeRepo.legs_with_suggestion_info",
                     return_value=[
                         {"leg_order": 1, "executed": True, "symbol": "BANKNIFTY",
                          "expiry_date": near, "strike": 57600.0, "option_type": "CE",
                          "action": "SELL", "fill_price": 535.6,
                          "lots": 1, "lots_actual": 1, "lot_size": 35},
                         {"leg_order": 2, "executed": True, "symbol": "BANKNIFTY",
                          "expiry_date": far, "strike": 57600.0, "option_type": "CE",
                          "action": "BUY", "fill_price": 1177.2,
                          "lots": 1, "lots_actual": 1, "lot_size": 35},
                     ])

        def _chain(_sym, _as_of, expiry):
            if expiry == near:
                return [{"strike": 57600.0, "option_type": "CE", "settle_price": 500.0}]
            if expiry == far:
                return [{"strike": 57600.0, "option_type": "CE", "settle_price": 1100.0}]
            return []

        mocker.patch("dashboard.server.FoEodRepo.get_chain", side_effect=_chain)
        mocker.patch("dashboard.server.SpotEodRepo.for_date",
                     return_value={"close_price": 57000.0})
        resp = client.get("/api/trades/TRD-20260817-005/close-suggestion")
        assert resp.status_code == 200
        data = resp.get_json()
        by_order = {l["leg_order"]: l for l in data["legs"]}
        assert by_order[1]["suggested_close"] == pytest.approx(500.0)
        assert by_order[2]["suggested_close"] == pytest.approx(1100.0)
        assert by_order[1]["expiry_date"] == "2026-08-25"
        assert by_order[2]["expiry_date"] == "2026-09-29"
        assert data["est_gross_pnl"] == pytest.approx(-1456.0)

    def test_iron_condor_same_expiry_keeps_distinct_strikes(self, client, mocker):
        exp = date(2026, 8, 27)
        mocker.patch("dashboard.server.TradeRepo.get",
                     return_value={"trade_id": "TRD-IC"})
        mocker.patch("dashboard.server.TradeRepo.legs_with_suggestion_info",
                     return_value=[
                         {"leg_order": 1, "executed": True, "symbol": "NIFTY",
                          "expiry_date": exp, "strike": 22800.0, "option_type": "PE",
                          "action": "BUY", "fill_price": 40.0,
                          "lots": 1, "lots_actual": 1, "lot_size": 75},
                         {"leg_order": 2, "executed": True, "symbol": "NIFTY",
                          "expiry_date": exp, "strike": 23000.0, "option_type": "PE",
                          "action": "SELL", "fill_price": 80.0,
                          "lots": 1, "lots_actual": 1, "lot_size": 75},
                         {"leg_order": 3, "executed": True, "symbol": "NIFTY",
                          "expiry_date": exp, "strike": 23600.0, "option_type": "CE",
                          "action": "SELL", "fill_price": 70.0,
                          "lots": 1, "lots_actual": 1, "lot_size": 75},
                         {"leg_order": 4, "executed": True, "symbol": "NIFTY",
                          "expiry_date": exp, "strike": 23800.0, "option_type": "CE",
                          "action": "BUY", "fill_price": 35.0,
                          "lots": 1, "lots_actual": 1, "lot_size": 75},
                     ])
        mocker.patch("dashboard.server.FoEodRepo.get_chain", return_value=[
            {"strike": 22800.0, "option_type": "PE", "settle_price": 30.0},
            {"strike": 23000.0, "option_type": "PE", "settle_price": 55.0},
            {"strike": 23600.0, "option_type": "CE", "settle_price": 50.0},
            {"strike": 23800.0, "option_type": "CE", "settle_price": 22.0},
        ])
        mocker.patch("dashboard.server.SpotEodRepo.for_date",
                     return_value={"close_price": 23300.0})
        resp = client.get("/api/trades/TRD-IC/close-suggestion")
        data = resp.get_json()
        closes = [l["suggested_close"] for l in data["legs"]]
        assert closes == [30.0, 55.0, 50.0, 22.0]
        assert len({l["expiry_date"] for l in data["legs"]}) == 1


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
        captured: list[tuple[str, list]] = []

        def _fetch_all(sql, params=None):
            captured.append((sql, params or []))
            return []

        fake.fetch_all = MagicMock(side_effect=_fetch_all)
        mocker.patch("dashboard.server.SQLServerConnection", return_value=fake)
        mocker.patch("dashboard.server.TradeRepo")
        mocker.patch("dashboard.server.SuggestionRepo")
        new_app = server.create_app()
        new_app.config["TESTING"] = True
        c = new_app.test_client()
        resp = c.get("/api/history/closed-trades?from_date=2026-01-01&to_date=2026-04-30&quality_band=good&pnl=loss")
        assert resp.status_code == 200
        assert captured, "expected fetch_all to run"
        main_sql, main_params = captured[0]
        assert "CONVERT(date, COALESCE(t.closed_on, t.executed_on))" in main_sql
        assert "s.entry_quality_score >= ? AND s.entry_quality_score <= ?" in main_sql
        assert "t.net_pnl < 0" in main_sql
        assert main_params[:2] == ["2026-01-01", "2026-04-30"]
        assert 65 in main_params and 79 in main_params

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
        body = resp.get_json()
        assert "groups" in body
        pnl = next(g for g in body["groups"] if g["id"] == "pnl")
        keys = {i["key"] for i in pnl["items"]}
        assert "long_premium_target_base" in keys
        assert "strategy_sl_limits" in keys
        assert "flags" in body
        charges = next(g for g in body["groups"] if g["id"] == "charges")
        charge_keys = {i["key"] for i in charges["items"]}
        assert "zerodha_charges.gst_pct" in charge_keys
        scheduler = next(g for g in body["groups"] if g["id"] == "scheduler")
        assert any(i["key"] == "scheduler.jobs" for i in scheduler["items"])

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
        mocker.patch("dashboard.server.ConfigRepo.get_all", return_value=[])
        mocker.patch("database.config_overlay.apply_config_overrides")
        resp = client.put("/api/config/take_profit_fraction",
                           data=json.dumps({"value": 0.8}),
                           content_type="application/json")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        assert body["key"] == "take_profit_fraction"

    def test_set_namespaced_key(self, client, mocker):
        mocker.patch("dashboard.server.ConfigRepo.set")
        mocker.patch("dashboard.server.ConfigRepo.get_all", return_value=[])
        mocker.patch("database.config_overlay.apply_config_overrides")
        resp = client.put("/api/config/zerodha_charges.gst_pct",
                           data=json.dumps({"value": 0.18}),
                           content_type="application/json")
        assert resp.status_code == 200
        assert resp.get_json()["key"] == "zerodha_charges.gst_pct"

    def test_set_locked_key(self, client, mocker):
        mocker.patch("dashboard.server.ConfigRepo.get_all", return_value=[{
            "config_key": "take_profit_fraction",
            "is_locked": 1,
        }])
        resp = client.put("/api/config/take_profit_fraction",
                           data=json.dumps({"value": 0.8}),
                           content_type="application/json")
        assert resp.status_code == 403

    def test_set_unknown_key(self, client):
        resp = client.put("/api/config/not_a_real_key",
                           data=json.dumps({"value": 1}),
                           content_type="application/json")
        assert resp.status_code == 400

    def test_bulk_save(self, client, mocker):
        mocker.patch("dashboard.server.ConfigRepo.set")
        mocker.patch("dashboard.server.ConfigRepo.get_all", return_value=[])
        mocker.patch("database.config_overlay.apply_config_overrides")
        mock_flags = mocker.patch("database.runtime_flags.RuntimeFlagsRepo")
        mock_flags.return_value.set.return_value = None
        resp = client.put(
            "/api/config/bulk",
            data=json.dumps({
                "configs": [{"key": "take_profit_fraction", "value": 0.7}],
                "flags": [{"key": "trade_execution_enabled", "value": True}],
            }),
            content_type="application/json",
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        assert "take_profit_fraction" in body["saved_configs"]
        assert "trade_execution_enabled" in body["saved_flags"]

    def test_bulk_save_empty(self, client):
        resp = client.put(
            "/api/config/bulk",
            data=json.dumps({}),
            content_type="application/json",
        )
        assert resp.status_code == 400


class TestNotifications:
    def test_recent(self, client, mocker):
        mocker.patch("dashboard.server.NotificationRepo.filtered", return_value=[])
        mocker.patch("dashboard.server.NotificationRepo.count_filtered", return_value=0)
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

    def test_pipeline_steps_inherit_next_run(self, client, mocker):
        from datetime import timezone
        from zoneinfo import ZoneInfo

        mocker.patch("dashboard.server.JobLogRepo.latest_status_per_job",
                     return_value=[])
        mocker.patch("scheduler.scheduler._eod_pipeline_enabled", return_value=True)

        ist = ZoneInfo("Asia/Kolkata")
        pipeline_when = datetime(2026, 7, 24, 20, 35, tzinfo=ist)

        mock_pipeline_job = MagicMock()
        mock_pipeline_job.id = "eod_nightly_pipeline"
        mock_pipeline_job.next_run_time = pipeline_when

        mock_sch = MagicMock()
        mock_sch.running = True
        mock_sch.get_jobs.return_value = [mock_pipeline_job]

        import scheduler.scheduler as sched
        mocker.patch.object(sched, "_SCHEDULER", mock_sch)

        resp = client.get("/api/jobs/list")
        data = resp.get_json()
        fo = next(j for j in data["jobs"] if j["job_name"] == "fo_bhav_download")
        assert fo["via_pipeline"] is True
        assert fo["next_run"] is not None
        assert "20:35" in fo["next_run"] or "20:35" in fo["schedule"]
        assert fo["manual_enabled"] is True

    def test_morning_pipeline_steps_not_manual_only(self, client, mocker):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        mocker.patch("dashboard.server.JobLogRepo.latest_status_per_job",
                     return_value=[])
        mocker.patch("scheduler.scheduler._eod_pipeline_enabled", return_value=False)
        mocker.patch("scheduler.scheduler._morning_eod_catchup_enabled", return_value=True)

        ist = ZoneInfo("Asia/Kolkata")
        pipeline_when = datetime(2026, 8, 4, 9, 0, tzinfo=ist)

        mock_pipeline_job = MagicMock()
        mock_pipeline_job.id = "morning_eod_catchup"
        mock_pipeline_job.next_run_time = pipeline_when

        mock_sch = MagicMock()
        mock_sch.running = True
        mock_sch.get_jobs.return_value = [mock_pipeline_job]

        import scheduler.scheduler as sched
        mocker.patch.object(sched, "_SCHEDULER", mock_sch)

        resp = client.get("/api/jobs/list")
        data = resp.get_json()
        fo = next(j for j in data["jobs"] if j["job_name"] == "fo_bhav_download")
        assert fo["via_pipeline"] is True
        assert fo["pipeline_parent"] == "morning_eod_catchup"
        assert fo["enabled"] is True
        assert "09:00" in fo["schedule"] or "Morning EOD" in fo["schedule"]
        nightly = next(j for j in data["jobs"] if j["job_name"] == "eod_nightly_pipeline")
        assert nightly["via_pipeline"] is False
        assert nightly["enabled"] is False

    def test_jobs_sorted_chronologically_with_groups(self, client, mocker):
        mocker.patch("dashboard.server.JobLogRepo.latest_status_per_job",
                     return_value=[])
        mocker.patch("scheduler.scheduler._eod_pipeline_enabled", return_value=True)
        import scheduler.scheduler as sched
        mocker.patch.object(sched, "_SCHEDULER", None)

        resp = client.get("/api/jobs/list")
        jobs = resp.get_json()["jobs"]
        names = [j["job_name"] for j in jobs]

        assert names.index("events_seed") < names.index("intraday_validator")
        assert names.index("intraday_validator") < names.index("live_suggestion_engine")
        assert names.index("live_suggestion_engine") < names.index("event_eve_review")
        assert names.index("event_eve_review") < names.index("intraday_close_snapshot")
        assert names.index("morning_eod_catchup") < names.index("weekly_cleanup")
        assert names.index("weekly_cleanup") < names.index("intraday_validator")
        assert names.index("intraday_validator") < names.index("intraday_close_snapshot")

        assert jobs[0]["display_group"] == "Monday & weekly"
        assert all(j.get("display_group") for j in jobs)

    def test_no_data_status_and_enriched_error(self, client, mocker):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        finished = datetime(2026, 7, 30, 11, 11, tzinfo=ZoneInfo("Asia/Kolkata"))
        mocker.patch(
            "dashboard.server.JobLogRepo.latest_status_per_job",
            return_value=[{
                "job_name": "fo_bhav_download",
                "status": "NO_DATA",
                "started_at": finished,
                "finished_at": finished,
                "error_message": (
                    "FO bhavcopy not available for 2026-07-30 — "
                    "market holiday or NSE has not published the file yet"
                ),
                "rows_processed": 0,
            }],
        )
        mocker.patch(
            "lifecycle.no_data_messages.latest_trade_date_for_job",
            return_value=__import__("datetime").date(2026, 7, 29),
        )
        import scheduler.scheduler as sched
        mocker.patch.object(sched, "_SCHEDULER", None)

        resp = client.get("/api/jobs/list")
        fo = next(j for j in resp.get_json()["jobs"] if j["job_name"] == "fo_bhav_download")
        assert fo["status"] == "NO_DATA"
        assert "Latest available in DB: 2026-07-29" in fo["error_message"]

    def test_morning_no_data_rewritten_on_jobs_list(self, client, mocker):
        from datetime import datetime

        started = datetime(2026, 8, 4, 9, 0, 54)
        mocker.patch(
            "dashboard.server.JobLogRepo.latest_status_per_job",
            return_value=[{
                "job_name": "fo_bhav_download",
                "status": "NO_DATA",
                "started_at": started,
                "finished_at": started,
                "error_message": (
                    "FO bhavcopy not available for 2026-08-04 — "
                    "market holiday or NSE has not published the file yet"
                ),
                "rows_processed": 0,
            }],
        )
        mocker.patch(
            "lifecycle.no_data_messages.latest_trade_date_for_job",
            return_value=__import__("datetime").date(2026, 8, 3),
        )
        import scheduler.scheduler as sched
        mocker.patch.object(sched, "_SCHEDULER", None)

        resp = client.get("/api/jobs/list")
        fo = next(j for j in resp.get_json()["jobs"] if j["job_name"] == "fo_bhav_download")
        assert "2026-08-03" in fo["error_message"]
        assert "prior trading session" in fo["error_message"]
        assert "Latest available in DB: 2026-08-03" in fo["error_message"]


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


class TestSystemStatusFull:
    """Phase 3 — #9 comprehensive /api/system/status aggregator."""

    def test_returns_all_top_level_sections(self, client, mocker):
        mocker.patch("database.runtime_flags.RuntimeFlagsRepo.get_bool",
                     return_value=False)
        mocker.patch("database.log_repo.JobLogRepo.latest_status_per_job",
                     return_value=[])
        mocker.patch("database.models.FoEodRepo.latest_trade_date",
                     return_value=date(2026, 5, 4))
        mocker.patch("database.models.IvHistoryRepo.latest_trade_date",
                     return_value=date(2026, 5, 4))
        mocker.patch("database.models.SpotEodRepo.latest",
                     return_value={"trade_date": date(2026, 5, 4)})
        mocker.patch("database.models.VixRepo.latest",
                     return_value={"trade_date": date(2026, 5, 4)})
        mocker.patch("database.models.SuggestionRepo.active_pending",
                     return_value=[])
        mocker.patch("database.models.TradeRepo.open_trades",
                     return_value=[])
        resp = client.get("/api/system/status")
        assert resp.status_code == 200
        data = resp.get_json()
        for k in ("as_of", "today", "runtime_flags", "scheduler",
                  "jobs_last_status", "data_freshness", "counts",
                  "websocket"):
            assert k in data, f"missing {k}"
        assert data["runtime_flags"]["circuit_breaker_active"] is False
        assert data["counts"]["open_trades"] == 0

    def test_endpoint_never_500s_on_internal_failures(self, client, mocker):
        mocker.patch("database.runtime_flags.RuntimeFlagsRepo.get_bool",
                     side_effect=RuntimeError("boom"))
        mocker.patch("database.log_repo.JobLogRepo.latest_status_per_job",
                     side_effect=RuntimeError("boom"))
        mocker.patch("database.models.FoEodRepo.latest_trade_date",
                     side_effect=RuntimeError("boom"))
        mocker.patch("database.models.SuggestionRepo.active_pending",
                     side_effect=RuntimeError("boom"))
        resp = client.get("/api/system/status")
        assert resp.status_code == 200
        data = resp.get_json()
        for section in ("runtime_flags", "data_freshness", "counts"):
            v = data[section]
            if isinstance(v, dict):
                assert v.get("available") is False or "reason" in v


class TestLiveMTMStream:
    """Phase 3 — #3 Live MTM SSE endpoint."""

    def test_endpoint_returns_event_stream_mime(self, client):
        resp = client.get("/api/live/mtm", buffered=False)
        assert resp.status_code == 200
        assert resp.mimetype == "text/event-stream"
        # Read just the initial connect comment so the generator emits at
        # least one chunk; close immediately to avoid blocking on heartbeat.
        first = next(resp.response)
        assert b"connected" in first
        resp.close()

    def test_publish_propagates_to_client(self, client, mocker):
        import json
        import os

        path = "data/live_mtm_state.json"
        os.makedirs("data", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({
                "trades": {
                    "T-001": {"trade_id": "T-001", "mtm": 1234.0, "dte": 5},
                },
            }, fh)
        mocker.patch("time.sleep", return_value=None)
        resp = client.get("/api/live/mtm", buffered=False)
        # Drain the connect comment.
        next(resp.response)
        chunk = next(resp.response)
        assert b"T-001" in chunk and b"1234" in chunk
        resp.close()


class TestIndicesSpotStream:
    def test_endpoint_returns_event_stream_mime(self, client):
        resp = client.get("/api/indices/spot/stream", buffered=False)
        assert resp.status_code == 200
        assert resp.mimetype == "text/event-stream"
        first = next(resp.response)
        assert b"connected" in first
        resp.close()


class TestWsMonitorStream:
    def test_endpoint_returns_event_stream_mime(self, client):
        resp = client.get("/api/ws/monitor/stream", buffered=False)
        assert resp.status_code == 200
        assert resp.mimetype == "text/event-stream"
        first = next(resp.response)
        assert b"connected" in first
        resp.close()


class TestAlertsStream:
    def test_endpoint_returns_event_stream_mime(self, client, mocker):
        mocker.patch(
            "dashboard.server._build_alerts_stream_payload",
            return_value={
                "system_status": {"circuit_breaker_active": False},
                "stats": {"total_unread": 0, "by_severity": {}, "by_category": {}},
                "notifications": [],
            },
        )
        mocker.patch("time.sleep", return_value=None)
        resp = client.get("/api/alerts/stream", buffered=False)
        assert resp.status_code == 200
        assert resp.mimetype == "text/event-stream"
        first = next(resp.response)
        assert b"connected" in first
        chunk = next(resp.response)
        assert b"system_status" in chunk
        resp.close()


class TestJobsStream:
    def test_endpoint_returns_event_stream_mime(self, client, mocker):
        mocker.patch(
            "dashboard.server._build_jobs_list_payload",
            return_value={"jobs": [], "scheduler_running": False, "generated_at": None},
        )
        mocker.patch("time.sleep", return_value=None)
        resp = client.get("/api/jobs/stream", buffered=False)
        assert resp.status_code == 200
        assert resp.mimetype == "text/event-stream"
        first = next(resp.response)
        assert b"connected" in first
        chunk = next(resp.response)
        assert b"jobs" in chunk
        resp.close()
