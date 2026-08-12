"""Tests for per-signal trade_open mapping and symbol guard on mark-taken."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def test_signals_trade_open_only_for_linked_signal(client, mocker):
    sig_rows = [
        {"id": 10, "symbol": "RELIANCE", "action": "SELL", "ltp": 100.0,
         "invalidation": 102.0, "signal_type": "OR_BREAK_DOWN", "reason": "r1",
         "triggered_at": "2026-08-12 10:00:00", "meta_json": None, "strength": "WEAK"},
        {"id": 11, "symbol": "RELIANCE", "action": "SELL", "ltp": 101.0,
         "invalidation": 103.0, "signal_type": "PULLBACK_DOWN", "reason": "r2",
         "triggered_at": "2026-08-12 10:05:00", "meta_json": None, "strength": "WEAK"},
    ]
    mock_sig = MagicMock()
    mock_sig.recent.return_value = sig_rows

    mock_trade = MagicMock()
    mock_trade.open_trades.return_value = [
        {"id": 7, "signal_id": 10, "symbol": "RELIANCE", "action": "SELL"},
    ]

    mocker.patch("scout.routes.ScoutSignalRepo", return_value=mock_sig)
    mocker.patch("scout.routes.ScoutTradeRepo", return_value=mock_trade)
    mocker.patch("scout.routes.latest_equity_ltps", return_value={})
    mocker.patch("scout.routes.is_market_open", return_value=True)
    mocker.patch(
        "scout.routes.enrich_signal",
        side_effect=lambda row, **kw: {**row, "validity_status": "ACTIVE", "dashboard": {}},
    )

    rv = client.get("/api/scout/signals")
    assert rv.status_code == 200
    signals = {s["id"]: s for s in rv.get_json()["signals"]}
    assert signals[10]["trade_open"] is True
    assert signals[10]["trade_id"] == 7
    assert signals[11]["trade_open"] is False
    assert signals[11]["trade_id"] is None


def test_mark_taken_rejects_second_open_trade_same_symbol(client, mocker):
    sig = {
        "id": 11,
        "symbol": "RELIANCE",
        "action": "SELL",
        "ltp": 100.0,
        "invalidation": 102.0,
        "signal_type": "PULLBACK_DOWN",
        "triggered_at": "2026-08-12 10:05:00",
        "meta": {},
    }
    mock_sig = MagicMock()
    mock_sig.get.return_value = sig

    mock_trade = MagicMock()
    mock_trade.open_signal_ids.return_value = set()
    mock_trade.open_trades.return_value = [
        {"id": 7, "signal_id": 10, "symbol": "RELIANCE"},
    ]

    mocker.patch("scout.routes.ScoutSignalRepo", return_value=mock_sig)
    mocker.patch("scout.routes.ScoutTradeRepo", return_value=mock_trade)
    mocker.patch("scout.routes.latest_equity_ltps", return_value={"RELIANCE": {"ltp": 100.0}})
    mocker.patch("scout.routes.enrich_signal", return_value={"validity_status": "ACTIVE"})
    mocker.patch("scout.routes.now_ist")

    rv = client.post("/api/scout/signals/11/mark-taken", json={"entry_price": 100, "quantity": 1})
    assert rv.status_code == 409
    assert "TRD #7" in rv.get_json()["error"]
