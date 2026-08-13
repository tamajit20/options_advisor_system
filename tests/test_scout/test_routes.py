"""Scout API blueprint smoke tests."""

from __future__ import annotations


def test_scout_status_route(client):
    rv = client.get("/api/scout/status")
    assert rv.status_code == 200
    data = rv.get_json()
    assert "zerodha_ok" in data
    assert "market_open" in data
    assert "watchlist_count" in data
    assert "push_enabled" in data
    assert data.get("mode") == "websocket"


def test_scout_signals_route(client):
    rv = client.get("/api/scout/signals")
    assert rv.status_code == 200
    data = rv.get_json()
    assert "signals" in data
    assert isinstance(data["signals"], list)
    assert "market_open" in data


def test_scout_flow_route_smoke(client, mocker):
    mocker.patch("scout.routes.get_scout_settings", return_value={})
    mocker.patch("scout.routes.build_flow_items", return_value=[])
    mocker.patch("scout.routes.is_market_open", return_value=False)
    mocker.patch("scout.routes.execution_mode_label", return_value="paper")
    mocker.patch("scout.routes.zerodha_execute_enabled", return_value=False)
    rv = client.get("/api/scout/flow")
    assert rv.status_code == 200
    assert "items" in rv.get_json()

