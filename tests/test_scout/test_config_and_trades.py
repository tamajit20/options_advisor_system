"""Tests for scout config, trades, and API routes."""

from __future__ import annotations

from unittest.mock import MagicMock

from database.scout_models import ScoutConfigRepo


def test_scout_config_watchlist_roundtrip():
    db = MagicMock()
    db.fetch_one.return_value = None
    repo = ScoutConfigRepo(db)
    assert repo.get_watchlist() is None
    repo.set_watchlist(["RELIANCE", "TCS"])
    assert db.execute.called


def test_trade_pnl_buy_and_sell():
    from database.scout_models import _trade_pnl

    pnl, pct = _trade_pnl("BUY", 100.0, 105.0, 1)
    assert pnl == 5.0
    assert pct == 5.0
    pnl2, _ = _trade_pnl("SELL", 100.0, 95.0, 1)
    assert pnl2 == 5.0


def test_config_loader_default_without_db():
    from scout.config_loader import default_watchlist, get_watchlist, invalidate_watchlist_cache

    invalidate_watchlist_cache()
    wl = get_watchlist(None, use_cache=False)
    assert wl == default_watchlist()


def test_config_loader_automation_defaults():
    from scout.config_loader import default_automation, get_automation, invalidate_automation_cache

    invalidate_automation_cache()
    auto = get_automation(None, use_cache=False)
    assert auto == default_automation()
    assert auto["auto_execute_signals"] is False
    assert auto["auto_close_trades"] is False


def test_scout_watchlist_api(client, mocker):
    mocker.patch(
        "scout.routes.get_watchlist",
        return_value=["RELIANCE", "TCS"],
    )
    mocker.patch("scout.routes.default_watchlist", return_value=["RELIANCE"])
    mocker.patch("scout.routes.nifty50_symbols", return_value=["RELIANCE", "TCS", "INFY"])
    mocker.patch("scout.routes.nifty_bank_symbols", return_value=["HDFCBANK", "ICICIBANK"])
    mocker.patch(
        "scout.routes.nse_equity_universe",
        return_value=(
            [
                {"symbol": "RELIANCE", "name": "Reliance", "is_nifty50": True, "index_tags": ["nifty50"]},
                {"symbol": "TCS", "name": "TCS", "is_nifty50": True, "index_tags": ["nifty50"]},
                {"symbol": "INFY", "name": "Infosys", "is_nifty50": True, "index_tags": ["nifty50"]},
            ],
            3,
            "2026-08-09 10:00:00",
        ),
    )
    rv = client.get("/api/scout/watchlist")
    assert rv.status_code == 200
    data = rv.get_json()
    assert data["selected_count"] == 2
    assert data["total_equity_count"] == 3
    assert len(data["nifty50"]) == 3
    assert len(data["nifty_bank"]) == 2
    assert data["zerodha_ok"] is True


def test_scout_watchlist_api_without_zerodha(client, mocker):
    from scout.instruments import ScoutInstrumentError

    mocker.patch("scout.routes.get_watchlist", return_value=[])
    mocker.patch("scout.routes.default_watchlist", return_value=["RELIANCE"])
    mocker.patch("scout.routes.nifty50_symbols", return_value=["RELIANCE", "TCS"])
    mocker.patch("scout.routes.nifty_bank_symbols", return_value=["HDFCBANK", "ICICIBANK"])
    mocker.patch(
        "scout.routes.nse_equity_universe",
        side_effect=ScoutInstrumentError("Zerodha login required"),
    )
    rv = client.get("/api/scout/watchlist")
    assert rv.status_code == 200
    data = rv.get_json()
    assert data["zerodha_ok"] is False
    assert data["notice"]
    assert len(data["stocks"]) == 2
    assert data["stocks"][0]["symbol"] == "RELIANCE"
    assert data["stocks"][0]["index_tags"] == ["nifty50"]
    assert data["total_equity_count"] == 2
    assert "nifty50" in data["index_groups"]


def test_scout_watchlist_put(client, mocker):
    mocker.patch(
        "scout.routes.watchlist_set",
        return_value=["RELIANCE", "TCS"],
    )
    rv = client.put(
        "/api/scout/watchlist",
        json={"symbols": ["RELIANCE", "TCS"]},
    )
    assert rv.status_code == 200
    data = rv.get_json()
    assert data["selected_count"] == 2


def test_scout_trades_open(client, mocker):
    mock_repo = MagicMock()
    mock_repo.open_trades.return_value = []
    mocker.patch("scout.routes.ScoutTradeRepo", return_value=mock_repo)
    rv = client.get("/api/scout/trades/open")
    assert rv.status_code == 200
    assert rv.get_json()["count"] == 0
