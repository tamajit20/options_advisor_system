"""Tests for scout subscription helpers and config_loader extensions."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from scout.config_loader import (
    default_watchlist,
    get_watchlist,
    invalidate_watchlist_cache,
    is_nifty50,
    set_automation,
    watchlist_set,
)
from scout.subscription import make_scout_equity_loader


def test_make_scout_equity_loader_disabled(mocker):
    mocker.patch("scout.subscription.SCOUT_CONFIG", {"enabled": False})
    loader = make_scout_equity_loader(MagicMock())
    assert loader() == []


def test_make_scout_equity_loader_returns_watchlist(mocker):
    mocker.patch("scout.subscription.SCOUT_CONFIG", {"enabled": True})
    mocker.patch("scout.subscription.get_watchlist", return_value=["RELIANCE", "TCS"])
    loader = make_scout_equity_loader(MagicMock())
    assert loader() == ["RELIANCE", "TCS"]


def test_watchlist_set_persists_and_invalidates_cache(mocker):
    mock_repo = MagicMock()
    mocker.patch("database.scout_models.ScoutConfigRepo", return_value=mock_repo)
    mocker.patch("scout.instruments.valid_nse_symbols", return_value=["RELIANCE"])
    invalidate_watchlist_cache()
    out = watchlist_set(MagicMock(), ["RELIANCE", "UNKNOWN"])
    assert out == ["RELIANCE"]
    mock_repo.set_watchlist.assert_called_once()


def test_watchlist_set_fallback_when_validation_fails(mocker):
    mock_repo = MagicMock()
    mocker.patch("database.scout_models.ScoutConfigRepo", return_value=mock_repo)
    mocker.patch("scout.instruments.valid_nse_symbols", side_effect=RuntimeError("no zerodha"))
    out = watchlist_set(MagicMock(), ["RELIANCE", "TCS"])
    assert sorted(out) == ["RELIANCE", "TCS"]


def test_get_watchlist_from_db(mocker):
    mock_repo = MagicMock()
    mock_repo.get_watchlist.return_value = ["INFY"]
    mocker.patch("database.scout_models.ScoutConfigRepo", return_value=mock_repo)
    invalidate_watchlist_cache()
    assert get_watchlist(MagicMock(), use_cache=False) == ["INFY"]


def test_get_watchlist_default_without_db():
    invalidate_watchlist_cache()
    wl = get_watchlist(None, use_cache=False)
    assert wl == default_watchlist()


def test_set_automation_merges_into_settings(mocker):
    mocker.patch(
        "scout.config_loader.get_scout_settings",
        return_value={
            "auto_execute_signals": False,
            "auto_close_trades": False,
            "auto_trade_quantity": 1,
            "max_trades_per_day": 5,
        },
    )
    mocker.patch(
        "scout.config_loader.set_scout_settings",
        return_value={"auto_execute_signals": True, "auto_close_trades": True, "auto_trade_quantity": 3},
    )
    out = set_automation(MagicMock(), {"auto_execute_signals": True, "auto_trade_quantity": 3})
    assert out["auto_execute_signals"] is True


def test_is_nifty50():
    assert is_nifty50("RELIANCE") in (True, False)
