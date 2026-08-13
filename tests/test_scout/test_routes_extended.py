"""Extended scout API route tests (close, void, history, live-quotes)."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock


def test_scout_live_quotes_api(client, mocker):
    mocker.patch(
        "scout.routes.latest_equity_ltps",
        return_value={"RELIANCE": {"ltp": 2500.0, "as_of": "2026-08-12T10:00:00"}},
    )
    rv = client.get("/api/scout/live-quotes?symbols=RELIANCE")
    assert rv.status_code == 200
    data = rv.get_json()
    assert data["quotes"]["RELIANCE"]["ltp"] == 2500.0


def test_scout_trade_close_success(client, mocker):
    mock_repo = MagicMock()
    mock_repo.close.return_value = {
        "id": 7,
        "status": "CLOSED",
        "exit_price": 2550.0,
        "pnl": 50.0,
    }
    mocker.patch("scout.routes.ScoutTradeRepo", return_value=mock_repo)
    rv = client.post("/api/scout/trades/7/close", json={"exit_price": 2550})
    assert rv.status_code == 200
    assert rv.get_json()["trade"]["status"] == "CLOSED"


def test_scout_trade_close_requires_exit_price(client):
    rv = client.post("/api/scout/trades/7/close", json={})
    assert rv.status_code == 400


def test_scout_trade_close_not_found(client, mocker):
    mock_repo = MagicMock()
    mock_repo.close.return_value = None
    mocker.patch("scout.routes.ScoutTradeRepo", return_value=mock_repo)
    rv = client.post("/api/scout/trades/7/close", json={"exit_price": 100})
    assert rv.status_code == 404


def test_scout_trade_void_success(client, mocker):
    mock_repo = MagicMock()
    mock_repo.void.return_value = True
    mocker.patch("scout.routes.ScoutTradeRepo", return_value=mock_repo)
    rv = client.delete("/api/scout/trades/7")
    assert rv.status_code == 200
    assert rv.get_json()["status"] == "ok"


def test_scout_trade_void_not_found(client, mocker):
    mock_repo = MagicMock()
    mock_repo.void.return_value = False
    mocker.patch("scout.routes.ScoutTradeRepo", return_value=mock_repo)
    rv = client.delete("/api/scout/trades/7")
    assert rv.status_code == 404


def test_scout_history_trades(client, mocker):
    mock_repo = MagicMock()
    mock_repo.closed_trades.return_value = [{
        "id": 1,
        "symbol": "TCS",
        "action": "BUY",
        "entry_price": 4000.0,
        "exit_price": 4050.0,
        "pnl": 50.0,
        "status": "CLOSED",
    }]
    mocker.patch("scout.routes.ScoutTradeRepo", return_value=mock_repo)
    mocker.patch(
        "scout.routes.enrich_history_trade",
        side_effect=lambda r: {**r, "flow": "manual"},
    )
    rv = client.get("/api/scout/history/trades?days=7")
    assert rv.status_code == 200
    data = rv.get_json()
    assert data["count"] == 1
    assert data["trades"][0]["symbol"] == "TCS"


def test_scout_history_stats(client, mocker):
    mock_repo = MagicMock()
    mock_repo.performance_stats.return_value = {
        "total_trades": 3,
        "wins": 2,
        "losses": 1,
        "total_pnl": 120.0,
        "automation": {"auto_entry_count": 1, "manual_entry_count": 2},
    }
    mocker.patch("scout.routes.ScoutTradeRepo", return_value=mock_repo)
    rv = client.get("/api/scout/history/stats?days=30")
    assert rv.status_code == 200
    data = rv.get_json()
    assert data["total_trades"] == 3
    assert "from_date" in data


def test_scout_watchlist_refresh(client, mocker):
    mocker.patch("scout.routes.refresh_nse_equity_master", return_value=1500)
    rv = client.post("/api/scout/watchlist/refresh-instruments")
    assert rv.status_code == 200
    assert rv.get_json()["instrument_count"] == 1500


def test_scout_flow_route(client, mocker):
    mocker.patch(
        "scout.routes.get_scout_settings",
        return_value={"zerodha_execute_orders": False},
    )
    mocker.patch(
        "scout.routes.build_flow_items",
        return_value=[
            {"kind": "trade", "trade": {"id": 1, "symbol": "TCS", "status": "OPEN"}},
        ],
    )
    mocker.patch("scout.routes.is_market_open", return_value=False)
    mocker.patch("scout.routes.execution_mode_label", return_value="paper")
    mocker.patch("scout.routes.zerodha_execute_enabled", return_value=False)

    rv = client.get("/api/scout/flow")
    assert rv.status_code == 200
    data = rv.get_json()
    assert data["count"] == 1
    assert data["items"][0]["kind"] == "trade"
    assert data["market_open"] is False
    assert "poll_seconds" in data


def test_scout_flow_route_empty_off_market(client, mocker):
    mocker.patch("scout.routes.get_scout_settings", return_value={})
    mocker.patch("scout.routes.build_flow_items", return_value=[])
    mocker.patch("scout.routes.is_market_open", return_value=False)
    mocker.patch("scout.routes.execution_mode_label", return_value="paper")
    mocker.patch("scout.routes.zerodha_execute_enabled", return_value=False)

    rv = client.get("/api/scout/flow")
    data = rv.get_json()
    assert data["count"] == 0
    assert data["items"] == []


def test_scout_zerodha_log_route(client, mocker):
    mock_repo = MagicMock()
    mock_repo.fetch.return_value = [
        {
            "id": 1,
            "severity": "error",
            "message": "Margins denied",
            "created_at": "2026-08-13 10:00:00",
        },
    ]
    mock_repo.count.return_value = 1
    mocker.patch("scout.routes.ScoutZerodhaLogRepo", return_value=mock_repo)

    rv = client.get("/api/scout/zerodha-log?days=7")
    assert rv.status_code == 200
    data = rv.get_json()
    assert data["count"] == 1
    assert data["entries"][0]["severity"] == "error"
    assert "from_date" in data

