"""Basic Basis Monitor API tests."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import dashboard.server as server


_DEFAULT_BASIS_SETTINGS = {
    "enabled": True,
    "universe": "nifty50_fo",
    "tick_staleness_sec": 3.0,
    "min_basis_store_pct": 0.0,
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
    mocker.patch("basis.routes.SQLServerConnection", return_value=fake_conn)
    mocker.patch("database.basis_models.BasisPairRepo.count_active", return_value=0)
    mocker.patch("database.basis_models.BasisPairRepo.list_all", return_value=[])
    mocker.patch("database.basis_models.BasisEpisodeRepo.list_episodes", return_value=[])
    mocker.patch("database.basis_models.BasisEpisodeRepo.open_episodes", return_value=[])
    mocker.patch(
        "basis.routes.get_basis_settings",
        return_value=dict(_DEFAULT_BASIS_SETTINGS),
    )
    app = server.create_app()
    app.config["TESTING"] = True
    return app.test_client()


def test_basis_status_route(client):
    rv = client.get("/api/basis/status")
    assert rv.status_code == 200
    data = rv.get_json()
    assert data["enabled"] is True
    assert "pairs_count" in data


def test_basis_pairs_route(client):
    rv = client.get("/api/basis/pairs")
    assert rv.status_code == 200
    data = rv.get_json()
    assert "pairs" in data


def test_basis_episodes_history_route(client):
    rv = client.get("/api/basis/episodes/history?min_basis_pct=0.1&min_duration_sec=5")
    assert rv.status_code == 200
    data = rv.get_json()
    assert "episodes" in data


def test_basis_live_route(client):
    rv = client.get("/api/basis/live")
    assert rv.status_code == 200
    data = rv.get_json()
    assert "basis" in data


def test_basis_live_stream_route(client):
    rv = client.get("/api/basis/live/stream")
    assert rv.status_code == 200
    assert rv.mimetype == "text/event-stream"
    chunk = next(rv.response)
    assert b": connected" in chunk


def test_basis_config_get(client, mocker):
    mocker.patch(
        "basis.routes.get_basis_settings",
        return_value={**_DEFAULT_BASIS_SETTINGS, "min_basis_store_pct": 0.5},
    )
    rv = client.get("/api/basis/config")
    assert rv.status_code == 200
    data = rv.get_json()
    assert data["settings"]["min_basis_store_pct"] == 0.5
    assert "defaults" in data


def test_basis_config_put(client, mocker):
    mocker.patch(
        "basis.routes.reload_basis_settings",
        return_value=dict(_DEFAULT_BASIS_SETTINGS),
    )
    mocker.patch(
        "basis.routes.set_basis_settings",
        return_value={**_DEFAULT_BASIS_SETTINGS, "min_basis_store_pct": 0.25},
    )
    rv = client.put(
        "/api/basis/config",
        json={"min_basis_store_pct": 0.25, "min_duration_store_sec": 10},
    )
    assert rv.status_code == 200
    data = rv.get_json()
    assert data["status"] == "ok"
    assert data["settings"]["min_basis_store_pct"] == 0.25
