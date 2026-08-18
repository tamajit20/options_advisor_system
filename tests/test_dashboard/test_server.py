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
    # fetch_one must return None (not a MagicMock) so routes that call it
    # directly (e.g. data_as_of provenance lookup) produce JSON-serialisable output.
    fake_conn.fetch_one.return_value = None
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


class TestExecutionGateLabel:
    def _gate(self, *, ok=False, vetoes=None):
        from types import SimpleNamespace
        return SimpleNamespace(ok=ok, vetoes=vetoes or [])

    def test_returns_none_when_gate_ok(self):
        assert server._execution_gate_label(self._gate(ok=True), {"status": "PENDING"}) is None

    def test_stale_suggestion_label(self):
        gate = self._gate(vetoes=["Suggestion generated 90 minutes ago"])
        assert server._execution_gate_label(gate, {"status": "PENDING"}) == "Stale"

    def test_circuit_breaker_label(self):
        gate = self._gate(vetoes=["daily P&L circuit breaker is active"])
        assert server._execution_gate_label(gate, {"status": "PENDING"}) == "Circuit breaker"

    def test_scenario_blocked_label(self):
        gate = self._gate(vetoes=["LONG_STRADDLE vetoed: IV rank 5 below 15"])
        gate.details = {"strategy_veto": True}
        assert server._execution_gate_label(
            gate, {"status": "PENDING", "strategy_veto": "LONG_STRADDLE vetoed"},
        ) == "Scenario blocked"
        gate = self._gate(vetoes=["validator failed"])
        row = {"status": "PENDING", "validator_status": "STALE_0935"}
        assert server._execution_gate_label(gate, row) == "Stale at open"

    def test_scenario_blocked_wins_over_stale_veto_text(self):
        gate = self._gate(vetoes=[
            "LONG_STRADDLE vetoed: IV rank 5 below 15",
            "suggestion generated 42m ago (max 30m)",
        ])
        gate.details = {"strategy_veto": True}
        assert server._execution_gate_label(
            gate, {"status": "PENDING", "strategy_veto": "LONG_STRADDLE vetoed"},
        ) == "Scenario blocked"


class TestHistoryFilterHelpers:
    def test_append_quality_band_excellent(self):
        params: list = []
        sql = server._append_quality_band_filter("WHERE 1=1", params, "excellent")
        assert "entry_quality_score >= ?" in sql
        assert params == [80]

    def test_append_quality_band_weak_is_bounded(self):
        params: list = []
        sql = server._append_quality_band_filter("WHERE 1=1", params, "weak")
        assert "entry_quality_score >= ? AND entry_quality_score <= ?" in sql
        assert params == [35, 49]

    def test_append_quality_band_poor(self):
        params: list = []
        sql = server._append_quality_band_filter("WHERE 1=1", params, "poor")
        assert "entry_quality_score < ?" in sql
        assert params == [35]

    def test_append_quality_band_unknown_is_noop(self):
        params: list = []
        sql = server._append_quality_band_filter("WHERE 1=1", params, "nope")
        assert sql == "WHERE 1=1"
        assert params == []

    def test_append_trade_pnl_filter(self):
        assert "net_pnl > 0" in server._append_trade_pnl_filter("WHERE 1=1", "profit")
        assert "net_pnl < 0" in server._append_trade_pnl_filter("WHERE 1=1", "loss")
        assert server._append_trade_pnl_filter("WHERE 1=1", "") == "WHERE 1=1"

    def test_normalize_quality_band_legacy_numeric(self):
        assert server._normalize_quality_band("35", "") == "weak"
        assert server._normalize_quality_band("", "65") == "good"
        assert server._normalize_quality_band("nope", "") == ""

    def test_parse_history_date_window_valid(self):
        f, t = server._parse_history_date_window("2026-01-01", "2026-04-30")
        assert f == "2026-01-01"
        assert t == "2026-04-30"

    def test_parse_history_date_window_invalid_falls_back(self, mocker):
        mocker.patch("dashboard.server.today_ist", return_value=date(2026, 5, 24))
        f, t = server._parse_history_date_window("BAD", "BAD", days_default=30)
        assert f == "2026-04-24"
        assert t == "2026-05-24"


# ---------------------------------------------------------------------------
# Routes — smoke + behaviour
# ---------------------------------------------------------------------------
class TestApiTheme:
    def test_returns_theme_config(self, client):
        resp = client.get("/api/theme")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, dict)


class TestApiIndicesSpot:
    def test_returns_live_quotes_when_ws_fresh(self, client, mocker):
        now = datetime(2026, 5, 25, 11, 0, 0)
        mocker.patch("dashboard.server.now_ist", return_value=now)
        mocker.patch("dashboard.server._ws_tick_age_seconds", return_value=5.0)
        mocker.patch("dashboard.server._load_ws_status_snapshot", return_value={
            "connection_state": "connected",
            "token_expired": False,
            "recent_events": [
                {"symbol": "NIFTY", "last_price": 24031.7, "ts": "2026-05-25T11:00:00"},
                {"symbol": "BANKNIFTY", "last_price": 55293.65, "ts": "2026-05-25T11:00:00"},
                {"symbol": "FINNIFTY", "last_price": 26102.15, "ts": "2026-05-25T11:00:00"},
                {"symbol": "VIX", "last_price": 16.7, "ts": "2026-05-25T11:00:00"},
            ],
        })
        resp = client.get("/api/indices/spot")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["feed"] == "live"
        syms = {i["symbol"]: i for i in data["indices"]}
        assert syms["NIFTY"]["source"] == "live"
        assert syms["NIFTY"]["price"] == 24031.7
        assert syms["VIX"]["price"] == 16.7

    def test_falls_back_to_eod_when_ws_missing(self, client, mocker):
        mocker.patch("dashboard.server._load_ws_status_snapshot", return_value=None)
        mocker.patch(
            "dashboard.server.SpotEodRepo.latest",
            side_effect=lambda sym: {
                "symbol": sym,
                "close_price": 24000.0,
                "trade_date": date(2026, 5, 23),
            },
        )
        mocker.patch(
            "dashboard.server.VixRepo.latest",
            return_value={"close_price": 15.5, "trade_date": date(2026, 5, 23)},
        )
        resp = client.get("/api/indices/spot")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["feed"] == "eod"
        nifty = next(i for i in data["indices"] if i["symbol"] == "NIFTY")
        assert nifty["source"] == "eod"
        assert nifty["price"] == 24000.0
        assert nifty["trade_date"] == "2026-05-23"


class TestApiSuggestionToday:
    def test_empty_suggestions(self, client, mocker):
        mocker.patch("dashboard.server.SuggestionRepo.active_pending", return_value=[])
        mocker.patch("dashboard.server.SuggestionRepo.active_sit_out_today", return_value=[])
        resp = client.get("/api/suggestion/today")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["suggestions"] == []
        assert data["sit_out"] == []
        assert data["market_summary"] is None
        assert "freshness_minutes" in data

    def test_surfaces_sit_out_when_no_pending(self, client, mocker):
        sit_row = {
            "suggestion_id": "SUG-NS-1",
            "underlying": "NIFTY",
            "strategy": "NONE",
            "status": "NO_SUGGESTION",
            "generated_on": datetime(2026, 5, 4, 20, 30),
            "confidence_score": 13,
            "no_suggestion_reason": "Strategy veto: IV/HV too rich",
            "conditions_json": (
                '[{"label":"IV Rank in actionable zone",'
                '"detail":"IV Rank 38.0 (need >50 or <30)"}]'
            ),
        }
        mocker.patch("dashboard.server.SuggestionRepo.active_pending", return_value=[])
        mocker.patch(
            "dashboard.server.SuggestionRepo.active_sit_out_today",
            return_value=[sit_row],
        )
        resp = client.get("/api/suggestion/today")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["sit_out"]) == 1
        assert data["sit_out"][0]["underlying"] == "NIFTY"
        assert data["sit_out"][0]["market_regime"]["id"] == "dead_zone"
        assert data["market_summary"] is not None

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
        mocker.patch("dashboard.server.SuggestionRepo.active_sit_out_today",
                     return_value=[])
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
        assert "execution_gate" in s
        assert s["execution_gate"]["ok"] is True

    def test_merges_paired_sit_out_into_suggestions(self, client, mocker):
        """Failed breakout partner stays visible next to the range PENDING card."""
        import json as _json
        group = "BANKNIFTY:Weekly:2026-08-19"
        sug_row = {
            "suggestion_id": "SUG-20260818-003",
            "underlying": "BANKNIFTY",
            "strategy": "CALENDAR_SPREAD",
            "status": "PENDING",
            "generated_on": datetime(2026, 8, 18, 9, 0),
            "expiry_date": date(2026, 8, 27),
            "net_credit_suggested": -80.0,
            "trigger_reason": _json.dumps({
                "regime_pair_group": group,
                "regime_pair_type": "range",
                "regime_pair_preferred": True,
                "regime_pair_preference_reason": "System prefers the range trade",
            }),
        }
        ns_row = {
            "suggestion_id": "SUG-20260818-004",
            "underlying": "BANKNIFTY",
            "strategy": "NONE",
            "status": "NO_SUGGESTION",
            "generated_on": datetime(2026, 8, 18, 9, 1),
            "confidence_score": 7,
            "no_suggestion_reason": "Sideways breakout scenario blocked: LONG_STRADDLE veto",
            "trigger_reason": _json.dumps({
                "regime_pair_group": group,
                "regime_pair_type": "breakout",
                "regime_pair_preferred": False,
            }),
        }
        mocker.patch("dashboard.server.SuggestionRepo.active_pending",
                     return_value=[sug_row])
        mocker.patch("dashboard.server.SuggestionRepo.active_sit_out_today",
                     return_value=[ns_row])
        mocker.patch("dashboard.server.SuggestionRepo.legs", return_value=[{
            "leg_order": 1, "strike": 55000.0, "option_type": "CE",
            "action": "SELL", "lots": 1, "lot_size": 35,
            "suggested_price": 120.0,
        }])
        resp = client.get("/api/suggestion/today")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["suggestions"]) == 2
        types = {s["regime_pair_type"] for s in data["suggestions"]}
        assert types == {"range", "breakout"}
        groups = {s["regime_pair_group"] for s in data["suggestions"]}
        assert groups == {group}
        preferred = [s for s in data["suggestions"] if s.get("regime_pair_preferred")]
        assert len(preferred) == 1
        assert preferred[0]["status"] == "PENDING"
        assert data["sit_out"] == []

    def test_vetoed_breakout_pending_has_legs_and_blocked_gate(self, client, mocker):
        """Constructed-but-vetoed breakout is a full PENDING card, not sit-out."""
        import json as _json
        group = "BANKNIFTY:Weekly:2026-08-19"
        range_row = {
            "suggestion_id": "SUG-20260818-011",
            "underlying": "BANKNIFTY",
            "strategy": "CALENDAR_SPREAD",
            "status": "PENDING",
            "generated_on": datetime(2026, 8, 18, 9, 0),
            "expiry_date": date(2026, 8, 27),
            "net_credit_suggested": -80.0,
            "trigger_reason": _json.dumps({
                "regime_pair_group": group,
                "regime_pair_type": "range",
                "regime_pair_preferred": True,
                "regime_pair_preference_reason": "System prefers the range trade",
            }),
        }
        breakout_row = {
            "suggestion_id": "SUG-20260818-012",
            "underlying": "BANKNIFTY",
            "strategy": "LONG_STRADDLE",
            "status": "PENDING",
            "generated_on": datetime(2026, 8, 18, 9, 1),
            "expiry_date": date(2026, 8, 27),
            "net_credit_suggested": -200.0,
            "no_suggestion_reason": "LONG_STRADDLE vetoed: IV rank 5 below 15",
            "trigger_reason": _json.dumps({
                "regime_pair_group": group,
                "regime_pair_type": "breakout",
                "regime_pair_preferred": False,
                "strategy_veto": "LONG_STRADDLE vetoed: IV rank 5 below 15",
            }),
        }
        legs_by_sid = {
            "SUG-20260818-011": [{
                "leg_order": 1, "strike": 55000.0, "option_type": "CE",
                "action": "SELL", "lots": 1, "lot_size": 35,
                "suggested_price": 120.0,
            }],
            "SUG-20260818-012": [
                {
                    "leg_order": 1, "strike": 55000.0, "option_type": "CE",
                    "action": "BUY", "lots": 1, "lot_size": 35,
                    "suggested_price": 400.0,
                },
                {
                    "leg_order": 2, "strike": 55000.0, "option_type": "PE",
                    "action": "BUY", "lots": 1, "lot_size": 35,
                    "suggested_price": 380.0,
                },
            ],
        }
        mocker.patch("dashboard.server.SuggestionRepo.active_pending",
                     return_value=[range_row, breakout_row])
        mocker.patch("dashboard.server.SuggestionRepo.active_sit_out_today",
                     return_value=[])
        mocker.patch(
            "dashboard.server.SuggestionRepo.legs",
            side_effect=lambda sid: legs_by_sid.get(sid, []),
        )
        resp = client.get("/api/suggestion/today")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["sit_out"] == []
        assert len(data["suggestions"]) == 2
        by_type = {s["regime_pair_type"]: s for s in data["suggestions"]}
        assert by_type["range"]["strategy"] == "CALENDAR_SPREAD"
        br = by_type["breakout"]
        assert br["strategy"] == "LONG_STRADDLE"
        assert br["status"] == "PENDING"
        assert len(br["legs"]) == 2
        assert br["execution_gate"]["ok"] is False
        assert br["execution_gate"]["label"] == "Scenario blocked"
        assert any("IV rank" in v for v in br["execution_gate"]["vetoes"])
        assert {s["regime_pair_group"] for s in data["suggestions"]} == {group}

    def test_stale_range_and_vetoed_breakout_keep_distinct_gate_labels(self, client, mocker):
        import json as _json
        from datetime import timedelta
        from utils import now_ist

        now = now_ist()
        group = "BANKNIFTY:Weekly:2026-08-19"
        range_row = {
            "suggestion_id": "SUG-R",
            "underlying": "BANKNIFTY",
            "strategy": "CALENDAR_SPREAD",
            "status": "PENDING",
            "data_source": "LIVE",
            "trigger_type": "LIVE_RUN",
            "generated_on": now - timedelta(minutes=42),
            "entry_date": now.date(),
            "spot_at_generation": 57335.0,
            "trigger_reason": _json.dumps({
                "regime_pair_group": group,
                "regime_pair_type": "range",
                "regime_pair_preferred": True,
            }),
        }
        breakout_row = {
            "suggestion_id": "SUG-B",
            "underlying": "BANKNIFTY",
            "strategy": "LONG_STRADDLE",
            "status": "PENDING",
            "data_source": "LIVE",
            "trigger_type": "LIVE_RUN",
            "generated_on": now - timedelta(minutes=5),
            "entry_date": now.date(),
            "spot_at_generation": 57335.0,
            "trigger_reason": _json.dumps({
                "regime_pair_group": group,
                "regime_pair_type": "breakout",
                "regime_pair_preferred": False,
                "strategy_veto": "LONG_STRADDLE vetoed: IV rank 5 below 15",
            }),
        }
        mocker.patch("dashboard.server.SuggestionRepo.active_pending",
                     return_value=[range_row, breakout_row])
        mocker.patch("dashboard.server.SuggestionRepo.active_sit_out_today",
                     return_value=[])
        mocker.patch(
            "dashboard.server.SuggestionRepo.legs",
            side_effect=lambda sid: [
                {"leg_order": 1, "strike": 55000.0, "option_type": "CE",
                 "action": "BUY", "lots": 1, "lot_size": 35,
                 "suggested_price": 400.0},
                {"leg_order": 2, "strike": 55000.0, "option_type": "PE",
                 "action": "BUY", "lots": 1, "lot_size": 35,
                 "suggested_price": 380.0},
            ] if sid == "SUG-B" else [
                {"leg_order": 1, "strike": 56000.0, "option_type": "CE",
                 "action": "SELL", "lots": 1, "lot_size": 35,
                 "suggested_price": 120.0},
                {"leg_order": 2, "strike": 56000.0, "option_type": "CE",
                 "action": "BUY", "lots": 1, "lot_size": 35,
                 "suggested_price": 400.0, "expiry_date": now.date() + timedelta(days=35)},
            ],
        )
        data = client.get("/api/suggestion/today").get_json()
        by_type = {s["regime_pair_type"]: s for s in data["suggestions"]}
        assert by_type["range"]["execution_gate"]["label"] == "Stale"
        assert by_type["breakout"]["execution_gate"]["label"] == "Scenario blocked"
        assert len(by_type["breakout"]["legs"]) == 2
        assert data["sit_out"] == []

    def test_does_not_merge_sit_out_when_pending_partner_exists(self, client, mocker):
        """A constructed PENDING breakout wins over a leftover NO_SUGGESTION row."""
        import json as _json
        group = "BANKNIFTY:Weekly:2026-08-19"
        range_row = {
            "suggestion_id": "SUG-R",
            "underlying": "BANKNIFTY",
            "strategy": "CALENDAR_SPREAD",
            "status": "PENDING",
            "generated_on": datetime(2026, 8, 18, 9, 0),
            "trigger_reason": _json.dumps({
                "regime_pair_group": group,
                "regime_pair_type": "range",
                "regime_pair_preferred": True,
            }),
        }
        breakout_row = {
            "suggestion_id": "SUG-B",
            "underlying": "BANKNIFTY",
            "strategy": "LONG_STRADDLE",
            "status": "PENDING",
            "generated_on": datetime(2026, 8, 18, 9, 1),
            "trigger_reason": _json.dumps({
                "regime_pair_group": group,
                "regime_pair_type": "breakout",
                "regime_pair_preferred": False,
                "strategy_veto": "LONG_STRADDLE vetoed: IV rank 5 below 15",
            }),
        }
        ns_row = {
            "suggestion_id": "SUG-NS",
            "underlying": "BANKNIFTY",
            "strategy": "NONE",
            "status": "NO_SUGGESTION",
            "generated_on": datetime(2026, 8, 18, 8, 0),
            "no_suggestion_reason": "Sideways breakout scenario blocked: LONG_STRADDLE veto",
            "trigger_reason": _json.dumps({
                "regime_pair_group": group,
                "regime_pair_type": "breakout",
                "regime_pair_preferred": False,
            }),
        }
        mocker.patch("dashboard.server.SuggestionRepo.active_pending",
                     return_value=[range_row, breakout_row])
        mocker.patch("dashboard.server.SuggestionRepo.active_sit_out_today",
                     return_value=[ns_row])
        mocker.patch(
            "dashboard.server.SuggestionRepo.legs",
            return_value=[{
                "leg_order": 1, "strike": 55000.0, "option_type": "CE",
                "action": "BUY", "lots": 1, "lot_size": 35,
                "suggested_price": 400.0,
            }],
        )
        data = client.get("/api/suggestion/today").get_json()
        assert data["sit_out"] == []
        assert len(data["suggestions"]) == 2
        types = [s["regime_pair_type"] for s in data["suggestions"]]
        assert types.count("breakout") == 1
        assert types.count("range") == 1
        br = next(s for s in data["suggestions"] if s["regime_pair_type"] == "breakout")
        assert br["strategy"] == "LONG_STRADDLE"
        assert br["status"] == "PENDING"

    def test_includes_blocked_pending_with_execution_gate(self, client, mocker):
        from datetime import timedelta
        from utils import now_ist

        now = now_ist()
        old_gen = now - timedelta(minutes=90)
        sug_row = {
            "suggestion_id": "SUG-STALE",
            "underlying": "NIFTY",
            "strategy": "BULL_PUT_SPREAD",
            "status": "PENDING",
            "data_source": "LIVE",
            "generated_on": old_gen,
            "entry_date": now.date(),
            "spot_at_generation": 23000.0,
            "net_credit_suggested": 100.0,
        }
        leg_row = {
            "leg_order": 1, "strike": 22800.0, "option_type": "PE",
            "action": "SELL", "lots": 1, "lot_size": 75,
            "suggested_price": 50.0,
        }
        mocker.patch(
            "dashboard.server.SuggestionRepo.active_pending",
            return_value=[sug_row],
        )
        mocker.patch(
            "dashboard.server.SuggestionRepo.active_sit_out_today",
            return_value=[],
        )
        mocker.patch(
            "dashboard.server.SuggestionRepo.legs",
            return_value=[leg_row],
        )
        mocker.patch(
            "database.runtime_flags.RuntimeFlagsRepo",
            return_value=mocker.Mock(get_bool=mocker.Mock(return_value=False)),
        )
        resp = client.get("/api/suggestion/today")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["suggestions"]) == 1
        gate = data["suggestions"][0]["execution_gate"]
        assert gate["ok"] is False
        assert gate["vetoes"]

    def test_includes_age_minutes_and_is_stale_flags(self, client, mocker):
        from datetime import timedelta
        from utils import now_ist

        now = now_ist()
        old_gen = now - timedelta(minutes=90)
        sug_row = {
            "suggestion_id": "SUG-AGE",
            "underlying": "NIFTY",
            "strategy": "BULL_PUT_SPREAD",
            "status": "PENDING",
            "data_source": "LIVE",
            "generated_on": old_gen,
            "entry_date": now.date(),
            "spot_at_generation": 23000.0,
            "net_credit_suggested": 100.0,
        }
        leg_row = {
            "leg_order": 1, "strike": 22800.0, "option_type": "PE",
            "action": "SELL", "lots": 1, "lot_size": 75,
            "suggested_price": 50.0,
        }
        mocker.patch("dashboard.server.SuggestionRepo.active_pending", return_value=[sug_row])
        mocker.patch("dashboard.server.SuggestionRepo.active_sit_out_today", return_value=[])
        mocker.patch("dashboard.server.SuggestionRepo.legs", return_value=[leg_row])
        mocker.patch(
            "database.runtime_flags.RuntimeFlagsRepo",
            return_value=mocker.Mock(get_bool=mocker.Mock(return_value=False)),
        )
        resp = client.get("/api/suggestion/today")
        s = resp.get_json()["suggestions"][0]
        assert s["age_minutes"] > 30
        assert s["is_stale"] is True
        assert s["execution_gate"]["label"] is not None

    def test_blocked_by_circuit_breaker_active(self, client, mocker):
        from utils import now_ist

        now = now_ist()
        sug_row = {
            "suggestion_id": "SUG-CB",
            "underlying": "NIFTY",
            "strategy": "BULL_PUT_SPREAD",
            "status": "PENDING",
            "data_source": "LIVE",
            "generated_on": now,
            "entry_date": now.date(),
            "spot_at_generation": 23000.0,
            "net_credit_suggested": 100.0,
        }
        leg_row = {
            "leg_order": 1, "strike": 22800.0, "option_type": "PE",
            "action": "SELL", "lots": 1, "lot_size": 75,
            "suggested_price": 50.0,
        }
        mocker.patch("dashboard.server.SuggestionRepo.active_pending", return_value=[sug_row])
        mocker.patch("dashboard.server.SuggestionRepo.active_sit_out_today", return_value=[])
        mocker.patch("dashboard.server.SuggestionRepo.legs", return_value=[leg_row])
        mocker.patch(
            "database.runtime_flags.RuntimeFlagsRepo",
            return_value=mocker.Mock(get_bool=mocker.Mock(return_value=True)),
        )
        resp = client.get("/api/suggestion/today")
        gate = resp.get_json()["suggestions"][0]["execution_gate"]
        assert gate["ok"] is False
        assert any("circuit breaker" in v.lower() for v in gate["vetoes"])
        assert gate["label"] == "Circuit breaker"

    def test_non_pending_rows_excluded_from_suggestions(self, client, mocker):
        sug_row = {
            "suggestion_id": "SUG-EXEC",
            "underlying": "NIFTY",
            "strategy": "BULL_PUT_SPREAD",
            "status": "EXECUTED",
            "generated_on": datetime(2026, 5, 4, 9, 0),
            "entry_date": date(2026, 5, 4),
            "net_credit_suggested": 100.0,
        }
        mocker.patch("dashboard.server.SuggestionRepo.active_pending", return_value=[sug_row])
        mocker.patch("dashboard.server.SuggestionRepo.active_sit_out_today", return_value=[])
        mocker.patch("dashboard.server.SuggestionRepo.legs", return_value=[])
        mocker.patch(
            "database.runtime_flags.RuntimeFlagsRepo",
            return_value=mocker.Mock(get_bool=mocker.Mock(return_value=False)),
        )
        resp = client.get("/api/suggestion/today")
        assert resp.get_json()["suggestions"] == []


class TestApiTradesOpen:
    def test_empty(self, client, mocker):
        mocker.patch("dashboard.server.TradeRepo.open_trades", return_value=[])
        resp = client.get("/api/trades/open")
        assert resp.status_code == 200
        # may return {"trades": []} or similar
        data = resp.get_json()
        assert data is not None

    def test_surfaces_entry_quality_score_from_suggestion(self, client, mocker):
        trade_row = {
            "trade_id": "TRD-1",
            "suggestion_id": "SUG-1",
            "trade_name": "NIFTY-TEST",
            "status": "ACTIVE",
            "executed_on": datetime(2026, 5, 20, 10, 0),
        }
        sug_row = {
            "suggestion_id": "SUG-1",
            "net_credit_suggested": 100.0,
            "entry_quality_score": 71,
            "edge_score": 72,
            "confidence_score": 10,
            "probability_of_profit": 68,
        }
        mocker.patch("dashboard.server.TradeRepo.open_trades", return_value=[trade_row])
        mocker.patch("dashboard.server.TradeRepo.legs_with_suggestion_info", return_value=[])
        mocker.patch("dashboard.server.NotificationRepo.latest_risk_alert_for_trade", return_value=None)
        mocker.patch("dashboard.server.SuggestionRepo.get", return_value=sug_row)
        mocker.patch("dashboard.server.SuggestionRepo.legs", return_value=[])
        resp = client.get("/api/trades/open")
        assert resp.status_code == 200
        trade = resp.get_json()["trades"][0]
        assert trade["entry_quality_score"] == 71
        assert trade["suggestion"]["entry_quality_score"] == 71


class TestApiHistorySuggestions:
    def test_returns_array(self, client, mocker):
        mocker.patch("dashboard.server.SuggestionRepo.by_date", return_value=[])
        resp = client.get("/api/history/suggestions")
        assert resp.status_code == 200

    def test_confidence_display_uses_conditions_json_length(self, mocker):
        from dashboard.server import _confidence_display

        checks = [{"label": f"g{i}", "status": "PASS"} for i in range(11)]
        checks.extend([
            {"label": "bad1", "status": "SOFT_FAIL"},
            {"label": "bad2", "status": "SOFT_FAIL"},
            {"label": "bad3", "status": "SOFT_FAIL"},
        ])
        row = {"confidence_score": 11, "conditions_json": json.dumps(checks)}
        assert _confidence_display(row) == "11/14"

    def test_confidence_display_legacy_score_only(self):
        from dashboard.server import _confidence_display

        assert _confidence_display({"confidence_score": 6}) == "6/7"
        assert _confidence_display({"confidence_score": 11}) == "11/14"


class TestApiLogs:
    def test_logs_endpoint_returns_200(self, client, mocker):
        mocker.patch("dashboard.server.LogRepo.fetch", return_value=[])
        resp = client.get("/api/logs")
        assert resp.status_code == 200


class TestZerodhaCallback:
    def test_public_base_url_from_request_host(self, client):
        with client.application.test_request_context(
            "/",
            base_url="http://52.230.104.81:5001/",
        ):
            assert server.public_base_url() == "http://52.230.104.81:5001"
            assert server.zerodha_callback_url() == "http://52.230.104.81:5001/zerodha/callback"

    def test_public_base_url_honors_forwarded_headers(self, client):
        with client.application.test_request_context(
            "/",
            base_url="http://127.0.0.1:5001/",
            headers={
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "options.example.com",
            },
        ):
            assert server.public_base_url() == "https://options.example.com"
            assert server.zerodha_callback_url() == "https://options.example.com/zerodha/callback"

    def test_public_base_url_uses_config_override(self, client, mocker):
        mocker.patch.dict(
            server.DASHBOARD_CONFIG,
            {"public_base_url": "http://azure-vm.example:5001"},
            clear=False,
        )
        with client.application.test_request_context("/", base_url="http://localhost:5001/"):
            assert server.public_base_url() == "http://azure-vm.example:5001"
            assert server.zerodha_callback_url() == "http://azure-vm.example:5001/zerodha/callback"

    def test_zerodha_status_includes_redirect_url(self, client, mocker):
        mocker.patch("providers.zerodha.session.load_session", return_value=None)
        resp = client.get(
            "/api/zerodha/status",
            base_url="http://52.230.104.81:5001/",
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["redirect_url"] == "http://52.230.104.81:5001/zerodha/callback"
        assert body["public_base_url"] == "http://52.230.104.81:5001"

    def test_zerodha_status_manual_paste_on_http_ip(self, client, mocker):
        mocker.patch("providers.zerodha.session.load_session", return_value=None)
        resp = client.get(
            "/api/zerodha/status",
            base_url="http://52.230.104.81:5001/",
        )
        body = resp.get_json()
        assert body["kite_manual_paste_flow"] is True
        assert body["kite_console_redirect_url"] == "http://127.0.0.1:5001/zerodha/callback"

    def test_zerodha_status_flags_http_ip_as_https_required(self, client, mocker):
        mocker.patch("providers.zerodha.session.load_session", return_value=None)
        resp = client.get(
            "/api/zerodha/status",
            base_url="http://52.230.104.81:5001/",
        )
        assert resp.get_json()["kite_https_required"] is True

    def test_zerodha_status_https_url_not_flagged(self, client, mocker):
        mocker.patch.dict(
            server.DASHBOARD_CONFIG,
            {"public_base_url": "https://options.example.com"},
            clear=False,
        )
        mocker.patch("providers.zerodha.session.load_session", return_value=None)
        resp = client.get("/api/zerodha/status")
        body = resp.get_json()
        assert body["kite_https_required"] is False
        assert body["redirect_url"] == "https://options.example.com/zerodha/callback"

    def test_callback_exchanges_token_and_redirects(self, client, mocker):
        mocker.patch(
            "providers.zerodha.session.exchange_request_token",
            return_value=mocker.Mock(user_id="AB12", generated_at=None),
        )
        resp = client.get(
            "/zerodha/callback?status=success&request_token=abc123",
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert "tab=wsmon" in resp.headers["Location"]
        assert "zerodha=ok" in resp.headers["Location"]

    def test_callback_missing_token_redirects_with_error(self, client):
        resp = client.get("/zerodha/callback?status=success", follow_redirects=False)
        assert resp.status_code == 302
        assert "zerodha_error=missing_request_token" in resp.headers["Location"]


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

    def test_execute_at_suggested_flag_forwarded(self, client, mocker):
        mark = mocker.patch("dashboard.server.mark_executed", return_value="TRD-002")
        resp = client.post(
            "/api/suggestion/SUG-2/mark-executed",
            data=json.dumps({"execute_at_suggested": True, "fills": []}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        mark.assert_called_once()
        assert mark.call_args.kwargs.get("execute_at_suggested") is True

    def test_skip_execution_gate_flag_forwarded(self, client, mocker):
        mark = mocker.patch("dashboard.server.mark_executed", return_value="TRD-003")
        resp = client.post(
            "/api/suggestion/SUG-3/mark-executed",
            data=json.dumps({
                "skip_execution_gate": True,
                "fills": [{"leg_order": 1, "executed": True, "fill_price": 18.5}],
            }),
            content_type="application/json",
        )
        assert resp.status_code == 200
        assert mark.call_args.kwargs.get("skip_execution_gate") is True


class TestTradeSignalKind:
    def test_sl_hit_daily_status(self):
        assert server._signal_kind_for_open_trade({"daily_status": "SL_HIT"}) == "sl"

    def test_loss_limit_is_sl(self):
        assert server._signal_kind_for_open_trade({"daily_status": "LOSS_LIMIT_HIT"}) == "sl"

    def test_thesis_fail(self):
        assert server._signal_kind_for_open_trade({"daily_status": "THESIS_FAIL"}) == "thesis"

    def test_exit_instruction_sl_hit(self):
        assert server._signal_kind_for_open_trade({
            "daily_status": "OPEN",
            "exit_instruction": "SL_HIT — close pending",
        }) == "sl"

    def test_take_profit_daily_status(self):
        assert server._signal_kind_for_open_trade({"daily_status": "TAKE_PROFIT"}) == "profit"

    def test_target_hit_beats_generic_close_pending(self):
        assert server._signal_kind_for_open_trade({
            "daily_status": "TAKE_PROFIT",
            "exit_instruction": "TAKE_PROFIT — close pending",
        }) == "profit"

    def test_live_target_hit_risk_type(self):
        assert server._signal_kind_for_open_trade(
            {"daily_status": "OPEN", "exit_instruction": None},
            risk_type="TARGET_HIT",
        ) == "profit"

    def test_loss_beats_profit_when_both_present(self):
        assert server._signal_kind_for_open_trade(
            {"daily_status": "TAKE_PROFIT"},
            risk_type="LOSS_LIMIT_HIT",
        ) == "sl"

    def test_close_pending_without_sl(self):
        assert server._signal_kind_for_open_trade({
            "daily_status": "OPEN",
            "exit_instruction": "Close pending — target locked",
        }) == "exit"

    def test_quiet_open_trade(self):
        assert server._signal_kind_for_open_trade({
            "daily_status": "OPEN",
            "exit_instruction": None,
        }) is None

    def test_in_loss_when_mtm_negative_without_sl(self):
        assert server._signal_kind_for_open_trade(
            {"daily_status": "OPEN", "exit_instruction": None},
            mtm=-120.0,
        ) == "in_loss"

    def test_in_profit_when_mtm_positive_without_target(self):
        assert server._signal_kind_for_open_trade(
            {"daily_status": "OPEN", "exit_instruction": None},
            mtm=80.0,
        ) == "in_profit"

    def test_flat_mtm_stays_quiet(self):
        assert server._signal_kind_for_open_trade(
            {"daily_status": "OPEN"},
            mtm=0.2,
        ) is None

    def test_sl_beats_mtm_status(self):
        assert server._signal_kind_for_open_trade(
            {"daily_status": "SL_HIT"},
            mtm=-50.0,
        ) == "sl"

    def test_take_profit_beats_mtm_status(self):
        assert server._signal_kind_for_open_trade(
            {"daily_status": "TAKE_PROFIT"},
            mtm=400.0,
        ) == "profit"


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
        assert "trade_signals" in body
        assert body["trade_signals"] == []
        assert "pnl_rules" in body
        rules = body["pnl_rules"]
        assert "strategy_sl_limits" in rules
        assert "long_premium_target_base" in rules
        assert rules["strategy_take_profit_fraction"]["IRON_BUTTERFLY"] == pytest.approx(0.75)

    def test_trade_signals_follow_open_trade_state(self, client, mocker):
        mocker.patch(
            "database.runtime_flags.RuntimeFlagsRepo.get_bool",
            return_value=False,
        )
        mocker.patch("dashboard.server._live_mtm_by_trade", return_value={})
        mocker.patch(
            "dashboard.server.TradeRepo.open_trades",
            return_value=[
                {
                    "trade_id": "TRD-20260818-001",
                    "trade_name": "NIFTY calendar",
                    "daily_status": "SL_HIT",
                    "exit_instruction": "SL_HIT — close pending",
                },
                {
                    "trade_id": "TRD-20260818-002",
                    "trade_name": "BANKNIFTY strangle",
                    "daily_status": "THESIS_FAIL",
                    "exit_instruction": None,
                },
                {
                    "trade_id": "TRD-20260818-003",
                    "trade_name": "Quiet",
                    "daily_status": "OPEN",
                    "exit_instruction": None,
                },
                {
                    "trade_id": "TRD-20260818-004",
                    "trade_name": "NIFTY iron condor",
                    "daily_status": "TAKE_PROFIT",
                    "exit_instruction": "Captured ≥50% of max profit — close pending",
                },
            ],
        )
        resp = client.get("/api/system-status")
        assert resp.status_code == 200
        signals = resp.get_json()["trade_signals"]
        assert [s["kind"] for s in signals] == ["sl", "thesis", "profit"]
        assert signals[0]["trade_id"] == "TRD-20260818-001"
        assert signals[1]["trade_id"] == "TRD-20260818-002"
        assert signals[2]["trade_id"] == "TRD-20260818-004"

    def test_trade_signals_include_mtm_profit_and_loss(self, client, mocker):
        mocker.patch(
            "database.runtime_flags.RuntimeFlagsRepo.get_bool",
            return_value=False,
        )
        mocker.patch(
            "dashboard.server.TradeRepo.open_trades",
            return_value=[
                {
                    "trade_id": "TRD-LOSS",
                    "trade_name": "Underwater IC",
                    "daily_status": "OPEN",
                    "exit_instruction": None,
                },
                {
                    "trade_id": "TRD-WIN",
                    "trade_name": "Working calendar",
                    "daily_status": "OPEN",
                    "exit_instruction": None,
                },
                {
                    "trade_id": "TRD-SL",
                    "trade_name": "Already SL",
                    "daily_status": "SL_HIT",
                    "exit_instruction": None,
                },
            ],
        )
        mocker.patch("dashboard.server._live_mtm_by_trade", return_value={
            "TRD-LOSS": {"mtm": -850.0, "trade_name": "Underwater IC"},
            "TRD-WIN": {"mtm": 420.0, "trade_name": "Working calendar"},
            "TRD-SL": {"mtm": -2000.0, "trade_name": "Already SL"},
        })
        resp = client.get("/api/system-status")
        assert resp.status_code == 200
        signals = resp.get_json()["trade_signals"]
        assert [s["kind"] for s in signals] == ["in_loss", "in_profit", "sl"]

    def test_trade_signals_fail_open_when_open_trades_raise(self, client, mocker):
        mocker.patch(
            "database.runtime_flags.RuntimeFlagsRepo.get_bool",
            return_value=False,
        )
        mocker.patch(
            "dashboard.server.TradeRepo.open_trades",
            side_effect=RuntimeError("db down"),
        )
        resp = client.get("/api/system-status")
        assert resp.status_code == 200
        assert resp.get_json()["trade_signals"] == []

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


# ---------------------------------------------------------------------------
# /api/ws/monitor
# ---------------------------------------------------------------------------
class TestWsMonitorEndpoint:
    def _write_snapshot(self, tmp_path, payload):
        path = tmp_path / "ws_status.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_returns_unavailable_when_file_missing(self, client, mocker, tmp_path):
        mocker.patch(
            "providers.ws_monitor.default_snapshot_path",
            return_value=tmp_path / "absent.json",
        )
        resp = client.get("/api/ws/monitor")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["available"] is False
        assert "reason" in body

    def test_returns_snapshot_when_file_exists(self, client, mocker, tmp_path):
        path = self._write_snapshot(tmp_path, {
            "provider": "zerodha",
            "connection_state": "connected",
            "tick_count_total": 42,
            "tick_rate_per_sec": 1.5,
            "rate_window_seconds": 60,
            "recent_events": [
                {"ts": "2025-04-01T09:30:00", "topic": "tick", "symbol": "NIFTY"},
                {"ts": "2025-04-01T09:30:01", "topic": "tick", "symbol": "BANKNIFTY"},
                {"ts": "2025-04-01T09:30:02", "topic": "connection_state",
                 "state": "connected", "provider": "zerodha"},
            ],
        })
        mocker.patch("providers.ws_monitor.default_snapshot_path", return_value=path)
        resp = client.get("/api/ws/monitor")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["available"] is True
        assert body["provider"] == "zerodha"
        assert body["tick_count_total"] == 42
        # most-recent first
        assert body["recent_events"][0]["topic"] == "connection_state"

    def test_filters_by_topic(self, client, mocker, tmp_path):
        path = self._write_snapshot(tmp_path, {
            "provider": "zerodha",
            "recent_events": [
                {"ts": "2025-04-01T09:30:00", "topic": "tick", "symbol": "NIFTY"},
                {"ts": "2025-04-01T09:30:01", "topic": "connection_state", "state": "connected"},
            ],
        })
        mocker.patch("providers.ws_monitor.default_snapshot_path", return_value=path)
        resp = client.get("/api/ws/monitor?topic=tick")
        body = resp.get_json()
        assert len(body["recent_events"]) == 1
        assert body["recent_events"][0]["topic"] == "tick"

    def test_filters_by_symbol(self, client, mocker, tmp_path):
        path = self._write_snapshot(tmp_path, {
            "provider": "zerodha",
            "recent_events": [
                {"ts": "2025-04-01T09:30:00", "topic": "tick", "symbol": "NIFTY"},
                {"ts": "2025-04-01T09:30:01", "topic": "tick", "symbol": "BANKNIFTY"},
            ],
        })
        mocker.patch("providers.ws_monitor.default_snapshot_path", return_value=path)
        resp = client.get("/api/ws/monitor?symbol=banknifty")
        body = resp.get_json()
        assert len(body["recent_events"]) == 1
        assert body["recent_events"][0]["symbol"] == "BANKNIFTY"

    def test_limit_caps_results(self, client, mocker, tmp_path):
        events = [
            {"ts": f"2025-04-01T09:30:{i:02d}", "topic": "tick", "symbol": "NIFTY"}
            for i in range(50)
        ]
        path = self._write_snapshot(tmp_path, {"provider": "zerodha",
                                                "recent_events": events})
        mocker.patch("providers.ws_monitor.default_snapshot_path", return_value=path)
        resp = client.get("/api/ws/monitor?limit=5")
        body = resp.get_json()
        assert len(body["recent_events"]) == 5

    def test_corrupt_json_returns_unavailable(self, client, mocker, tmp_path):
        path = tmp_path / "ws_status.json"
        path.write_text("not json", encoding="utf-8")
        mocker.patch("providers.ws_monitor.default_snapshot_path", return_value=path)
        resp = client.get("/api/ws/monitor")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["available"] is False


class TestJsRegimePairContracts:
    def test_pair_group_renders_full_suggestion_and_two_scenarios(self):
        from pathlib import Path
        js = Path(server.__file__).resolve().parent.joinpath(
            "static", "dashboard.js",
        ).read_text(encoding="utf-8")
        assert "TWO SCENARIOS" in js
        assert "renderSuggestion(s, false, items" in js
        assert "isPairMember && !hasLegs" in js
        assert "This scenario is blocked" in js
        assert "Mark Executed at suggested prices" in js
        assert "parts.length === 4" in js
        assert "matches.length === 1" in js
        assert "_lookupLegLtp" in js
