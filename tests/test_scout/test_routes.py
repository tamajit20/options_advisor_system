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
