"""Basic Arb Monitor API tests."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import dashboard.server as server
from database.arb_models import ArbConfigRepo


_DEFAULT_ARB_SETTINGS = {
    "enabled": True,
    "universe": "nifty50_dual",
    "tick_staleness_sec": 3.0,
    "leg_stale_close_sec": 5.0,
    "min_gap_store_pct": 0.0,
    "min_duration_store_sec": 0,
}


@pytest.fixture
def client(mocker):
    fake_conn = MagicMock()
    fake_conn.connect = MagicMock(return_value=None)
    fake_conn.close = MagicMock(return_value=None)
    fake_conn.fetch_one.return_value = {"n": 0}
    fake_conn.fetch_all.return_value = []
    mocker.patch("dashboard.server.SQLServerConnection", return_value=fake_conn)
    mocker.patch("arb.routes.SQLServerConnection", return_value=fake_conn)
    mocker.patch("database.arb_models.ArbPairRepo.count_active", return_value=0)
    mocker.patch("database.arb_models.ArbPairRepo.list_all", return_value=[])
    mocker.patch("database.arb_models.ArbGapRepo.list_gaps", return_value=[])
    mocker.patch("database.arb_models.ArbGapRepo.open_gaps", return_value=[])
    mocker.patch(
        "arb.routes.get_arb_settings",
        return_value=dict(_DEFAULT_ARB_SETTINGS),
    )
    app = server.create_app()
    app.config["TESTING"] = True
    return app.test_client()


def test_arb_status_route(client):
    rv = client.get("/api/arb/status")
    assert rv.status_code == 200
    data = rv.get_json()
    assert data["enabled"] is True
    assert "pairs_count" in data


def test_arb_pairs_route(client):
    rv = client.get("/api/arb/pairs")
    assert rv.status_code == 200
    data = rv.get_json()
    assert "pairs" in data


def test_arb_gaps_route(client):
    rv = client.get("/api/arb/gaps?min_gap_pct=0.1&min_duration_sec=5")
    assert rv.status_code == 200
    data = rv.get_json()
    assert "gaps" in data


def test_arb_live_route(client):
    rv = client.get("/api/arb/live")
    assert rv.status_code == 200
    data = rv.get_json()
    assert "gaps" in data


def test_arb_live_snapshot_route(client):
    rv = client.get("/api/arb/live/snapshot")
    assert rv.status_code == 200
    data = rv.get_json()
    assert "gaps" in data
    assert "source" in data


def test_arb_live_stream_route(client):
    rv = client.get("/api/arb/live/stream")
    assert rv.status_code == 200
    assert rv.mimetype == "text/event-stream"
    # First chunk should include SSE connected comment.
    chunk = next(rv.response)
    assert b": connected" in chunk


def test_arb_config_get(client, mocker):
    mocker.patch(
        "arb.routes.get_arb_settings",
        return_value={**_DEFAULT_ARB_SETTINGS, "min_gap_store_pct": 0.5},
    )
    rv = client.get("/api/arb/config")
    assert rv.status_code == 200
    data = rv.get_json()
    assert data["settings"]["min_gap_store_pct"] == 0.5
    assert "defaults" in data


def test_arb_config_put(client, mocker):
    mocker.patch(
        "arb.routes.reload_arb_settings",
        return_value=dict(_DEFAULT_ARB_SETTINGS),
    )
    mocker.patch(
        "arb.routes.set_arb_settings",
        return_value={**_DEFAULT_ARB_SETTINGS, "min_gap_store_pct": 0.25},
    )
    rv = client.put(
        "/api/arb/config",
        json={"min_gap_store_pct": 0.25, "min_duration_store_sec": 10},
    )
    assert rv.status_code == 200
    data = rv.get_json()
    assert data["status"] == "ok"
    assert data["settings"]["min_gap_store_pct"] == 0.25


def test_arb_config_put_rejects_non_object_body(client):
    rv = client.put("/api/arb/config", json=1)
    assert rv.status_code == 400


def test_arb_config_repo_get_set_settings_roundtrip():
    import json

    db = MagicMock()
    stored: dict = {}

    def _execute(query, params):
        if "MERGE arb_config" in query:
            key, val = params[0], params[1]
            stored[key] = json.loads(val)

    def _fetch_one(query, params):
        if params and params[0] == ArbConfigRepo.SETTINGS_KEY:
            raw = stored.get(ArbConfigRepo.SETTINGS_KEY)
            return {"config_value": json.dumps(raw)} if raw else None
        return None

    db.execute.side_effect = _execute
    db.fetch_one.side_effect = _fetch_one

    repo = ArbConfigRepo(db)
    assert repo.get_settings() is None
    payload = {"min_gap_store_pct": 0.5, "min_duration_store_sec": 5}
    repo.set_settings(payload)
    assert repo.get_settings() == payload
