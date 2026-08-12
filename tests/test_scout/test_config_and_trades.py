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


def test_scout_settings_api_get(client, mocker):
    mocker.patch(
        "scout.routes.get_scout_settings",
        return_value={"max_trades_per_day": 5, "investment_per_trade_inr": 20000},
    )
    mock_repo = MagicMock()
    mock_repo.count_trades_opened_today.return_value = 2
    mocker.patch("scout.routes.ScoutTradeRepo", return_value=mock_repo)
    rv = client.get("/api/scout/settings")
    assert rv.status_code == 200
    data = rv.get_json()
    assert data["settings"]["max_trades_per_day"] == 5
    assert data["trades_opened_today"] == 2


def test_scout_settings_api_put(client, mocker):
    mocker.patch(
        "scout.routes.get_scout_settings",
        return_value={"max_trades_per_day": 5},
    )
    mocker.patch(
        "scout.routes.set_scout_settings",
        return_value={"max_trades_per_day": 3, "investment_per_trade_inr": 25000},
    )
    rv = client.put(
        "/api/scout/settings",
        json={"max_trades_per_day": 3, "investment_per_trade_inr": 25000},
    )
    assert rv.status_code == 200
    data = rv.get_json()
    assert data["status"] == "ok"
    assert data["settings"]["max_trades_per_day"] == 3


def test_scout_trades_open(client, mocker):
    mock_repo = MagicMock()
    mock_repo.open_trades.return_value = []
    mocker.patch("scout.routes.ScoutTradeRepo", return_value=mock_repo)
    rv = client.get("/api/scout/trades/open")
    assert rv.status_code == 200
    assert rv.get_json()["count"] == 0


def test_scout_config_repo_get_set_settings_roundtrip():
    import json

    db = MagicMock()
    stored: dict = {}

    def _execute(query, params):
        if "MERGE scout_config" in query:
            key, val = params[0], params[1]
            stored[key] = json.loads(val)

    def _fetch_one(query, params):
        if params and params[0] == ScoutConfigRepo.SETTINGS_KEY:
            raw = stored.get(ScoutConfigRepo.SETTINGS_KEY)
            return {"config_value": json.dumps(raw)} if raw else None
        return None

    db.execute.side_effect = _execute
    db.fetch_one.side_effect = _fetch_one

    repo = ScoutConfigRepo(db)
    assert repo.get_settings() is None
    payload = {"max_trades_per_day": 3, "investment_per_trade_inr": 15000}
    repo.set_settings(payload)
    assert repo.get_settings() == payload


def test_scout_trade_repo_count_trades_opened_today(mocker):
    from datetime import date

    from database.scout_models import ScoutTradeRepo

    mocker.patch("utils.today_ist", return_value=date(2026, 8, 12))
    db = MagicMock()
    db.fetch_one.return_value = {"n": 4}
    assert ScoutTradeRepo(db).count_trades_opened_today() == 4


def test_scout_trade_repo_symbol_has_trade_today(mocker):
    from datetime import date

    from database.scout_models import ScoutTradeRepo

    mocker.patch("utils.today_ist", return_value=date(2026, 8, 12))
    db = MagicMock()
    db.fetch_one.return_value = {"id": 9}
    assert ScoutTradeRepo(db).symbol_has_trade_today("RELIANCE") is True


def test_get_scout_settings_loads_from_db(mocker):
    from scout.config_loader import get_scout_settings, invalidate_settings_cache

    invalidate_settings_cache()
    mock_repo = MagicMock()
    mock_repo.get_settings.return_value = {"max_trades_per_day": 7}
    mock_repo.get_automation.return_value = None
    mocker.patch("database.scout_models.ScoutConfigRepo", return_value=mock_repo)

    settings = get_scout_settings(MagicMock(), use_cache=False)
    assert settings["max_trades_per_day"] == 7
    assert settings["investment_per_trade_inr"] == 20_000


def test_get_scout_settings_legacy_automation_fallback(mocker):
    from scout.config_loader import get_scout_settings, invalidate_settings_cache

    invalidate_settings_cache()
    mock_repo = MagicMock()
    mock_repo.get_settings.return_value = None
    mock_repo.get_automation.return_value = {
        "auto_execute_signals": True,
        "auto_trade_quantity": 3,
    }
    mocker.patch("database.scout_models.ScoutConfigRepo", return_value=mock_repo)

    settings = get_scout_settings(MagicMock(), use_cache=False)
    assert settings["auto_execute_signals"] is True
    assert settings["auto_trade_quantity"] == 3


def test_set_scout_settings_invalidates_cache(mocker):
    from scout import config_loader
    from scout.config_loader import set_scout_settings

    config_loader._SETTINGS_CACHE = {"max_trades_per_day": 99}
    mock_repo = MagicMock()
    mocker.patch("database.scout_models.ScoutConfigRepo", return_value=mock_repo)

    set_scout_settings(MagicMock(), {"max_trades_per_day": 2})
    assert config_loader._SETTINGS_CACHE is None
    mock_repo.set_settings.assert_called_once()


def test_scout_settings_api_put_rejects_non_object_body(client):
    rv = client.put("/api/scout/settings", json=1)
    assert rv.status_code == 400
    assert "JSON object" in rv.get_json()["error"]
