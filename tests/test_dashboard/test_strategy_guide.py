"""Shared strategy learning copy — JSON completeness + dashboard wiring."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[2]
GUIDE_PATH = ROOT / "dashboard" / "static" / "strategy_guide.json"

REQUIRED_CODES = [
    "IRON_CONDOR",
    "IRON_BUTTERFLY",
    "BULL_PUT_SPREAD",
    "BEAR_CALL_SPREAD",
    "JADE_LIZARD",
    "CALENDAR_SPREAD",
    "BULL_CALL_SPREAD",
    "BEAR_PUT_SPREAD",
    "LONG_STRADDLE",
    "LONG_STRANGLE",
    "LONG_CALL",
    "LONG_PUT",
]
FAMILIES = {"credit", "debit_spread", "long_premium"}
TARGET_KINDS = {"credit", "debit_spread", "long_premium"}


def _guide():
    return json.loads(GUIDE_PATH.read_text(encoding="utf-8"))


class TestStrategyGuideJson:
    def test_file_exists(self):
        assert GUIDE_PATH.is_file()

    def test_order_covers_all_twelve(self):
        guide = _guide()
        assert guide["order"] == REQUIRED_CODES
        assert set(guide["strategies"]) == set(REQUIRED_CODES)

    def test_intro_has_families_and_signals(self):
        intro = _guide()["intro"]
        assert intro["lede"]
        fam_ids = {f["id"] for f in intro["families"]}
        assert fam_ids == FAMILIES
        assert len(intro["signals"]) >= 6
        assert len(intro["on_card"]) >= 6
        for row in intro["on_card"]:
            assert row.get("place")
            assert row.get("why")

    def test_each_strategy_has_required_fields(self):
        strategies = _guide()["strategies"]
        for code in REQUIRED_CODES:
            s = strategies[code]
            assert s["name"], code
            assert s["family"] in FAMILIES, code
            assert s["target_kind"] in TARGET_KINDS, code
            assert s["what"], code
            assert isinstance(s["when"], list) and s["when"], code
            assert isinstance(s["checks"], list) and s["checks"], code
            pnl = s["pnl"]
            for key in ("wins_when", "loses_when", "dte"):
                assert pnl.get(key), f"{code}.pnl.{key}"
            look = s["look"]
            for page in ("suggestion", "trade"):
                rows = look[page]
                assert rows, f"{code}.look.{page}"
                for row in rows:
                    assert row.get("place"), f"{code}.look.{page} place"
                    assert row.get("why"), f"{code}.look.{page} why"


@pytest.fixture
def client(mocker):
    import dashboard.server as server

    fake_conn = MagicMock()
    fake_conn.connect = MagicMock(return_value=None)
    fake_conn.close = MagicMock(return_value=None)
    fake_conn.fetch_one.return_value = None
    mocker.patch("dashboard.server.SQLServerConnection", return_value=fake_conn)
    app = server.create_app()
    app.config["TESTING"] = True
    return app.test_client()


class TestStrategyGuideServing:
    def test_static_json_served(self, client):
        resp = client.get("/static/strategy_guide.json")
        assert resp.status_code == 200
        data = json.loads(resp.get_data(as_text=True))
        assert set(data["strategies"]) == set(REQUIRED_CODES)

    def test_index_wires_learn_tab_and_shared_script(self, client):
        html = client.get("/").get_data(as_text=True)
        assert 'data-tab="learn"' in html
        assert 'id="panel-learn"' in html
        assert 'id="learn-container"' in html
        assert "strategy_guide.js" in html
        assert "window.__CACHE_BUST__" in html
        assert "window.__PNL_RULES__" in html
