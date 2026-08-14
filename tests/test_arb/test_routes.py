"""Basic Arb Monitor API tests."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import dashboard.server as server


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
    mocker.patch("database.arb_models.ArbConfigRepo.get_enabled", return_value=True)
    mocker.patch("database.arb_models.ArbConfigRepo.get_universe", return_value="nifty50_dual")
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
