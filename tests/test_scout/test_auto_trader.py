"""Tests for Scout automation (auto-enter / auto-close)."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import json
import pytest

from scout.auto_trader import (
    on_signals_committed,
    try_auto_close_trades,
    try_auto_execute_signal,
)


@pytest.fixture
def db():
    return MagicMock()


def test_try_auto_execute_skipped_when_disabled(db, mocker):
    mocker.patch("scout.auto_trader.get_automation", return_value={"auto_execute_signals": False})
    assert try_auto_execute_signal(db, signal_id=1, spot_lookup=lambda s: 100.0) is None


def test_try_auto_execute_marks_taken(db, mocker):
    mocker.patch(
        "scout.auto_trader.get_automation",
        return_value={"auto_execute_signals": True, "auto_trade_quantity": 2},
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
    }
    trade_repo = MagicMock()
    trade_repo.open_signal_ids.return_value = []
    trade_repo.open_trades.return_value = []
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


def test_try_auto_close_skipped_when_disabled(db, mocker):
    mocker.patch("scout.auto_trader.get_automation", return_value={"auto_close_trades": False})
    assert try_auto_close_trades(db, spot_lookup=lambda s: 100.0) == []


def test_try_auto_close_on_target_hit(db, mocker):
    mocker.patch("scout.auto_trader.get_automation", return_value={"auto_close_trades": True})
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
        "scout.auto_trader.get_automation",
        return_value={"auto_execute_signals": False},
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
