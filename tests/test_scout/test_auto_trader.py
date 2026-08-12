"""Tests for Scout automation (auto-enter / auto-close)."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import json
import pytest

from scout.auto_trader import (
    _auto_enter_block_reason,
    on_signals_committed,
    try_auto_close_trades,
    try_auto_execute_signal,
)


@pytest.fixture
def db():
    return MagicMock()


def _scout_settings(**overrides):
    base = {
        "auto_execute_signals": True,
        "auto_close_trades": False,
        "use_investment_sizing": False,
        "auto_trade_quantity": 2,
        "max_trades_per_day": 10,
        "one_trade_per_symbol_per_day": False,
        "auto_enter_strengths": ["WEAK", "MEDIUM", "HIGH"],
        "trade_window_start": "09:15",
        "trade_window_end": "15:30",
    }
    base.update(overrides)
    return base


def test_try_auto_execute_skipped_when_disabled(db, mocker):
    mocker.patch(
        "scout.auto_trader.get_scout_settings",
        return_value=_scout_settings(auto_execute_signals=False),
    )
    assert try_auto_execute_signal(db, signal_id=1, spot_lookup=lambda s: 100.0) is None


def test_try_auto_execute_marks_taken(db, mocker):
    mocker.patch(
        "scout.auto_trader.get_scout_settings",
        return_value=_scout_settings(auto_execute_signals=True, auto_trade_quantity=2),
    )
    mocker.patch("scout.auto_trader.is_market_open", return_value=True)
    mocker.patch("scout.auto_trader.SCOUT_CONFIG", {"enabled": True})
    mocker.patch(
        "scout.auto_trader.enrich_signal",
        return_value={"validity_status": "ACTIVE"},
    )
    sig_repo = MagicMock()
    sig_repo.get.return_value = {
        "id": 5,
        "symbol": "RELIANCE",
        "action": "BUY",
        "signal_type": "OR_BREAK",
        "ltp": 2500.0,
        "strength": "MEDIUM",
    }
    trade_repo = MagicMock()
    trade_repo.open_signal_ids.return_value = []
    trade_repo.open_trades.return_value = []
    trade_repo.count_trades_opened_today.return_value = 0
    trade_repo.symbol_has_trade_today.return_value = False
    trade_repo.mark_taken.return_value = 99
    mocker.patch("scout.auto_trader.ScoutSignalRepo", return_value=sig_repo)
    mocker.patch("scout.auto_trader.ScoutTradeRepo", return_value=trade_repo)

    out = try_auto_execute_signal(db, signal_id=5, spot_lookup=lambda s: 2510.0)
    assert out["trade_id"] == 99
    trade_repo.mark_taken.assert_called_once()
    kwargs = trade_repo.mark_taken.call_args.kwargs
    assert kwargs["signal_id"] == 5
    assert kwargs["entry_price"] == 2510.0
    assert kwargs["quantity"] == 2
    notes = kwargs["notes"]
    assert "auto_execute" in notes
    assert json.loads(notes)["mode"] == "auto"


def test_try_auto_execute_skipped_outside_window(db, mocker):
    mocker.patch(
        "scout.auto_trader.get_scout_settings",
        return_value=_scout_settings(
            auto_execute_signals=True,
            trade_window_start="09:45",
            trade_window_end="10:00",
        ),
    )
    mocker.patch("scout.auto_trader.is_market_open", return_value=True)
    mocker.patch("scout.auto_trader.SCOUT_CONFIG", {"enabled": True})
    mocker.patch("scout.auto_trader.in_trading_window", return_value=False)
    sig_repo = MagicMock()
    sig_repo.get.return_value = {"id": 1, "symbol": "TCS", "action": "BUY", "strength": "HIGH", "ltp": 100}
    trade_repo = MagicMock()
    trade_repo.open_signal_ids.return_value = []
    mocker.patch("scout.auto_trader.ScoutSignalRepo", return_value=sig_repo)
    mocker.patch("scout.auto_trader.ScoutTradeRepo", return_value=trade_repo)
    assert try_auto_execute_signal(db, signal_id=1, spot_lookup=lambda s: 100.0) is None


def test_try_auto_execute_investment_sizing(db, mocker):
    mocker.patch(
        "scout.auto_trader.get_scout_settings",
        return_value=_scout_settings(
            auto_execute_signals=True,
            use_investment_sizing=True,
            investment_per_trade_inr=20_000,
        ),
    )
    mocker.patch("scout.auto_trader.is_market_open", return_value=True)
    mocker.patch("scout.auto_trader.SCOUT_CONFIG", {"enabled": True})
    mocker.patch(
        "scout.auto_trader.enrich_signal",
        return_value={"validity_status": "ACTIVE"},
    )
    sig_repo = MagicMock()
    sig_repo.get.return_value = {
        "id": 5,
        "symbol": "RELIANCE",
        "action": "BUY",
        "signal_type": "OR_BREAK",
        "ltp": 2500.0,
        "strength": "HIGH",
    }
    trade_repo = MagicMock()
    trade_repo.open_signal_ids.return_value = []
    trade_repo.open_trades.return_value = []
    trade_repo.count_trades_opened_today.return_value = 0
    trade_repo.symbol_has_trade_today.return_value = False
    trade_repo.mark_taken.return_value = 100
    mocker.patch("scout.auto_trader.ScoutSignalRepo", return_value=sig_repo)
    mocker.patch("scout.auto_trader.ScoutTradeRepo", return_value=trade_repo)

    try_auto_execute_signal(db, signal_id=5, spot_lookup=lambda s: 2510.0)
    qty = trade_repo.mark_taken.call_args.kwargs["quantity"]
    assert qty == 7  # floor(20000 / 2510)


def test_try_auto_close_skipped_when_disabled(db, mocker):
    mocker.patch(
        "scout.auto_trader.get_scout_settings",
        return_value=_scout_settings(auto_close_trades=False),
    )
    assert try_auto_close_trades(db, spot_lookup=lambda s: 100.0) == []


def test_try_auto_close_on_target_hit(db, mocker):
    mocker.patch(
        "scout.auto_trader.get_scout_settings",
        return_value=_scout_settings(auto_close_trades=True),
    )
    mocker.patch("scout.auto_trader.SCOUT_CONFIG", {"enabled": True})
    trade_repo = MagicMock()
    trade_repo.open_trades.return_value = [{
        "id": 7,
        "symbol": "TCS",
        "action": "BUY",
        "signal_id": 3,
        "entry_price": 4000.0,
        "quantity": 1,
        "executed_at": datetime(2026, 8, 12, 10, 0, 0),
    }]
    trade_repo.close.return_value = {"id": 7, "pnl": 50.0}
    sig_repo = MagicMock()
    sig_repo.get.return_value = {
        "action": "BUY",
        "invalidation": 3950.0,
        "signal_type": "OR_BREAK",
        "meta": {},
    }
    mocker.patch("scout.auto_trader.ScoutTradeRepo", return_value=trade_repo)
    mocker.patch("scout.auto_trader.ScoutSignalRepo", return_value=sig_repo)
    mocker.patch(
        "scout.auto_trader.build_exit_plan",
        return_value={"dashboard": {"prices": {"target": 4100.0, "stop": 3950.0}, "timer_secs": 3600}},
    )
    mocker.patch(
        "scout.auto_trader.evaluate_exit_alerts",
        return_value={"close_now": True, "alerts": [{"code": "TARGET_HIT"}]},
    )

    closed = try_auto_close_trades(db, spot_lookup=lambda s: 4110.0)
    assert len(closed) == 1
    assert closed[0]["trade_id"] == 7
    trade_repo.close.assert_called_once()


def test_on_signals_committed_respects_toggle(db, mocker):
    mocker.patch(
        "scout.auto_trader.get_scout_settings",
        return_value=_scout_settings(auto_execute_signals=False),
    )
    mock_exec = mocker.patch("scout.auto_trader.try_auto_execute_signal")
    on_signals_committed(db, [1, 2], spot_lookup=lambda s: 100.0)
    mock_exec.assert_not_called()


def test_scout_automation_api_get(client, mocker):
    mocker.patch(
        "scout.routes.get_automation",
        return_value={
            "auto_execute_signals": True,
            "auto_close_trades": False,
            "auto_trade_quantity": 1,
        },
    )
    rv = client.get("/api/scout/automation")
    assert rv.status_code == 200
    data = rv.get_json()
    assert data["auto_execute_signals"] is True
    assert data["auto_close_trades"] is False


def test_scout_automation_api_put(client, mocker):
    mocker.patch(
        "scout.routes.set_automation",
        return_value={"auto_execute_signals": True, "auto_close_trades": True, "auto_trade_quantity": 1},
    )
    rv = client.put(
        "/api/scout/automation",
        json={"auto_execute_signals": True, "auto_close_trades": True},
    )
    assert rv.status_code == 200
    data = rv.get_json()
    assert data["status"] == "ok"
    assert data["automation"]["auto_close_trades"] is True


def _block_settings(**overrides):
    return _scout_settings(auto_execute_signals=True, **overrides)


def _setup_signal_mocks(mocker, *, strength="HIGH", symbol="TCS", ltp=100.0):
    mocker.patch("scout.auto_trader.is_market_open", return_value=True)
    mocker.patch("scout.auto_trader.SCOUT_CONFIG", {"enabled": True})
    mocker.patch(
        "scout.auto_trader.enrich_signal",
        return_value={"validity_status": "ACTIVE"},
    )
    sig_repo = MagicMock()
    sig_repo.get.return_value = {
        "id": 1,
        "symbol": symbol,
        "action": "BUY",
        "signal_type": "OR_BREAK",
        "ltp": ltp,
        "strength": strength,
    }
    trade_repo = MagicMock()
    trade_repo.open_signal_ids.return_value = []
    trade_repo.open_trades.return_value = []
    trade_repo.count_trades_opened_today.return_value = 0
    trade_repo.symbol_has_trade_today.return_value = False
    mocker.patch("scout.auto_trader.ScoutSignalRepo", return_value=sig_repo)
    mocker.patch("scout.auto_trader.ScoutTradeRepo", return_value=trade_repo)
    return sig_repo, trade_repo


def test_auto_enter_block_reason_daily_cap():
    trade_repo = MagicMock()
    trade_repo.count_trades_opened_today.return_value = 5
    reason = _auto_enter_block_reason(
        trade_repo,
        _block_settings(max_trades_per_day=5),
        signal_id=1,
        symbol="TCS",
        strength="HIGH",
    )
    assert reason == "daily trade cap (5) reached"


def test_auto_enter_block_reason_symbol_already_traded_today():
    trade_repo = MagicMock()
    trade_repo.count_trades_opened_today.return_value = 1
    trade_repo.symbol_has_trade_today.return_value = True
    reason = _auto_enter_block_reason(
        trade_repo,
        _block_settings(one_trade_per_symbol_per_day=True),
        signal_id=2,
        symbol="RELIANCE",
        strength="HIGH",
    )
    assert "symbol already traded today" in reason


def test_auto_enter_block_reason_strength_not_allowed():
    trade_repo = MagicMock()
    trade_repo.count_trades_opened_today.return_value = 0
    trade_repo.symbol_has_trade_today.return_value = False
    reason = _auto_enter_block_reason(
        trade_repo,
        _block_settings(auto_enter_strengths=["MEDIUM", "HIGH"]),
        signal_id=3,
        symbol="TCS",
        strength="WEAK",
    )
    assert "strength WEAK not in auto-enter list" in reason


def test_auto_enter_block_reason_open_trade_same_symbol():
    trade_repo = MagicMock()
    trade_repo.count_trades_opened_today.return_value = 0
    trade_repo.symbol_has_trade_today.return_value = False
    trade_repo.open_trades.return_value = [
        {"id": 7, "signal_id": 10, "symbol": "TCS"},
    ]
    reason = _auto_enter_block_reason(
        trade_repo,
        _block_settings(),
        signal_id=11,
        symbol="TCS",
        strength="HIGH",
    )
    assert "open trade already exists for TCS" in reason


def test_try_auto_execute_skipped_at_daily_cap(db, mocker):
    mocker.patch(
        "scout.auto_trader.get_scout_settings",
        return_value=_block_settings(max_trades_per_day=3),
    )
    _, trade_repo = _setup_signal_mocks(mocker)
    trade_repo.count_trades_opened_today.return_value = 3
    assert try_auto_execute_signal(db, signal_id=1, spot_lookup=lambda s: 100.0) is None


def test_try_auto_execute_skipped_symbol_already_traded_today(db, mocker):
    mocker.patch(
        "scout.auto_trader.get_scout_settings",
        return_value=_block_settings(one_trade_per_symbol_per_day=True),
    )
    _, trade_repo = _setup_signal_mocks(mocker)
    trade_repo.symbol_has_trade_today.return_value = True
    assert try_auto_execute_signal(db, signal_id=1, spot_lookup=lambda s: 100.0) is None


def test_try_auto_execute_skipped_weak_strength(db, mocker):
    mocker.patch(
        "scout.auto_trader.get_scout_settings",
        return_value=_block_settings(auto_enter_strengths=["MEDIUM", "HIGH"]),
    )
    _setup_signal_mocks(mocker, strength="WEAK")
    assert try_auto_execute_signal(db, signal_id=1, spot_lookup=lambda s: 100.0) is None


def test_try_auto_execute_skipped_open_trade_same_symbol(db, mocker):
    mocker.patch(
        "scout.auto_trader.get_scout_settings",
        return_value=_block_settings(),
    )
    _, trade_repo = _setup_signal_mocks(mocker)
    trade_repo.open_trades.return_value = [
        {"id": 7, "signal_id": 10, "symbol": "TCS"},
    ]
    assert try_auto_execute_signal(db, signal_id=1, spot_lookup=lambda s: 100.0) is None


def test_try_auto_execute_skipped_non_active_signal(db, mocker):
    mocker.patch(
        "scout.auto_trader.get_scout_settings",
        return_value=_block_settings(),
    )
    _setup_signal_mocks(mocker)
    mocker.patch(
        "scout.auto_trader.enrich_signal",
        return_value={"validity_status": "EXPIRED"},
    )
    assert try_auto_execute_signal(db, signal_id=1, spot_lookup=lambda s: 100.0) is None
