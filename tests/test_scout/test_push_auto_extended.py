"""Extended push_engine and auto_trader tests."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from providers.base import DataSource, LiveQuote
from scout.auto_trader import on_signals_committed, try_auto_close_trades, try_auto_execute_signal
from scout.push_engine import ScoutPushEngine


def test_push_engine_disabled_when_config_off(mocker):
    mocker.patch("scout.push_engine.SCOUT_CONFIG", {"enabled": False, "push_enabled": True})
    engine = ScoutPushEngine(db=MagicMock(), spot_lookup=lambda s: None)
    engine.start()
    assert engine._unsub_scout is None


def test_push_engine_nifty_index_tick_seeds_open(mocker):
    mocker.patch("scout.push_engine.is_scout_equity_tick", return_value=False)
    engine = ScoutPushEngine(db=MagicMock(), spot_lookup=lambda s: None)
    engine._watchlist = {"RELIANCE"}
    q = LiveQuote(
        symbol="NIFTY",
        expiry=None,
        strike=None,
        option_type=None,
        last_price=23000.0,
        timestamp=datetime(2026, 8, 7, 9, 30, 0),
        source=DataSource.LIVE,
        provider="test",
    )
    engine._on_tick(q)
    assert engine._nifty_open == 23000.0


def test_on_signals_committed_invokes_auto_execute(mocker):
    db = MagicMock()
    mocker.patch(
        "scout.auto_trader.get_scout_settings",
        return_value={"auto_execute_signals": True},
    )
    mock_exec = mocker.patch("scout.auto_trader.try_auto_execute_signal", return_value={"trade_id": 1})
    on_signals_committed(db, [10, 11], spot_lookup=lambda s: 100.0)
    assert mock_exec.call_count == 2


def test_try_auto_close_stop_hit(mocker):
    settings = {
        "auto_close_trades": True,
        "auto_execute_signals": False,
    }
    mocker.patch("scout.auto_trader.get_scout_settings", return_value=settings)
    mocker.patch("scout.auto_trader.SCOUT_CONFIG", {"enabled": True})
    mocker.patch("scout.auto_trader.is_market_open", return_value=True)
    trade_repo = MagicMock()
    trade_repo.open_trades.return_value = [{
        "id": 8,
        "symbol": "TCS",
        "action": "BUY",
        "signal_id": 3,
        "entry_price": 4000.0,
        "quantity": 1,
        "status": "OPEN",
        "executed_at": datetime(2026, 8, 12, 10, 0, 0),
    }]
    sig_repo = MagicMock()
    sig_repo.get.return_value = {
        "action": "BUY",
        "invalidation": 3950.0,
        "signal_type": "OR_BREAK",
        "meta": {},
    }
    mocker.patch("scout.auto_trader.ScoutTradeRepo", return_value=trade_repo)
    mocker.patch("scout.auto_trader.ScoutSignalRepo", return_value=sig_repo)
    mocker.patch("scout.auto_trader.zerodha_execute_enabled", return_value=False)
    mocker.patch(
        "scout.auto_trader.paper_close_if_triggered",
        return_value={"id": 8, "pnl": -30.0, "exit_price": 3940.0, "exit_reason": "stop_hit"},
    )
    closed = try_auto_close_trades(MagicMock(), spot_lookup=lambda s: 3940.0)
    assert len(closed) == 1
    assert closed[0]["exit_reason"] == "stop_hit"


def test_try_auto_execute_skipped_when_market_closed(mocker):
    db = MagicMock()
    mocker.patch(
        "scout.auto_trader.get_scout_settings",
        return_value={"auto_execute_signals": True},
    )
    mocker.patch("scout.auto_trader.is_market_open", return_value=False)
    assert try_auto_execute_signal(db, signal_id=1, spot_lookup=lambda s: 100.0) is None


def test_try_auto_execute_skipped_when_scout_disabled(mocker):
    db = MagicMock()
    mocker.patch(
        "scout.auto_trader.get_scout_settings",
        return_value={"auto_execute_signals": True},
    )
    mocker.patch("scout.auto_trader.is_market_open", return_value=True)
    mocker.patch("scout.auto_trader.SCOUT_CONFIG", {"enabled": False})
    assert try_auto_execute_signal(db, signal_id=1, spot_lookup=lambda s: 100.0) is None
