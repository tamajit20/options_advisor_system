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


def test_scout_live_quotes_stream(client):
    rv = client.get("/api/scout/live-quotes/stream")
    assert rv.status_code == 200
    assert rv.mimetype == "text/event-stream"
    chunk = next(rv.response)
    assert b": connected" in chunk
    rv.close()


def test_scout_signals_stream(client, mocker):
    mocker.patch(
        "scout.routes._build_scout_signals_payload",
        return_value={"signals": [], "count": 0, "market_open": False},
    )
    rv = client.get("/api/scout/signals/stream")
    assert rv.status_code == 200
    assert rv.mimetype == "text/event-stream"
    next(rv.response)
    chunk = next(rv.response)
    assert b"signals" in chunk
    rv.close()


def test_scout_flow_stream(client, mocker):
    mocker.patch(
        "scout.routes._build_scout_flow_payload",
        return_value={"items": [], "count": 0, "market_open": False},
    )
    mocker.patch("providers.ws_monitor.default_snapshot_path")
    rv = client.get("/api/scout/flow/stream")
    assert rv.status_code == 200
    assert rv.mimetype == "text/event-stream"
    next(rv.response)
    rv.close()

