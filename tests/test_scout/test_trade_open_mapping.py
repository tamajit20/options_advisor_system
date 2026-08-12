"""Tests for per-signal trade_open mapping and symbol guard on mark-taken."""

from __future__ import annotations

from datetime import datetime
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
    assert signals[10]["can_mark_taken"] is False
    assert signals[11]["trade_open"] is False
    assert signals[11]["trade_id"] is None
    assert signals[11]["symbol_trade_blocked"] is True
    assert signals[11]["can_mark_taken"] is False
    assert signals[11]["blocking_trade_id"] == 7
    assert signals[11]["blocking_signal_id"] == 10


def test_signals_sorted_open_opportunities_first(client, mocker):
    sig_rows = [
        {"id": 10, "symbol": "RELIANCE", "action": "SELL", "ltp": 100.0,
         "invalidation": 102.0, "signal_type": "OR_BREAK_DOWN", "reason": "r1",
         "triggered_at": "2026-08-12 10:10:00", "meta_json": None, "strength": "WEAK"},
        {"id": 11, "symbol": "RELIANCE", "action": "SELL", "ltp": 101.0,
         "invalidation": 103.0, "signal_type": "PULLBACK_DOWN", "reason": "r2",
         "triggered_at": "2026-08-12 10:05:00", "meta_json": None, "strength": "WEAK"},
        {"id": 12, "symbol": "TCS", "action": "BUY", "ltp": 4000.0,
         "invalidation": 3950.0, "signal_type": "OR_BREAK_UP", "reason": "r3",
         "triggered_at": "2026-08-12 10:00:00", "meta_json": None, "strength": "WEAK"},
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
    ids = [s["id"] for s in rv.get_json()["signals"]]
    assert ids == [12, 10, 11]


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


def _signals_setup(mocker, *, settings=None):
    settings = settings or {
        "use_investment_sizing": False,
        "auto_trade_quantity": 5,
        "investment_per_trade_inr": 20_000,
    }
    sig_rows = [
        {
            "id": 10,
            "symbol": "RELIANCE",
            "action": "BUY",
            "ltp": 2500.0,
            "invalidation": 2450.0,
            "signal_type": "OR_BREAK_UP",
            "reason": "r1",
            "triggered_at": "2026-08-12 10:00:00",
            "meta_json": None,
            "strength": "MEDIUM",
        },
    ]
    mock_sig = MagicMock()
    mock_sig.recent.return_value = sig_rows
    mock_trade = MagicMock()
    mock_trade.open_trades.return_value = []
    mocker.patch("scout.routes.ScoutSignalRepo", return_value=mock_sig)
    mocker.patch("scout.routes.ScoutTradeRepo", return_value=mock_trade)
    mocker.patch("scout.routes.get_scout_settings", return_value=settings)
    mocker.patch("scout.routes.latest_equity_ltps", return_value={"RELIANCE": {"ltp": 2510.0}})
    mocker.patch("scout.routes.is_market_open", return_value=True)
    mocker.patch(
        "scout.routes.enrich_signal",
        side_effect=lambda row, **kw: {**row, "validity_status": "ACTIVE", "dashboard": {}},
    )
    return mock_sig, mock_trade


def test_signals_include_suggested_quantity_fixed_qty(client, mocker):
    _signals_setup(mocker, settings={
        "use_investment_sizing": False,
        "auto_trade_quantity": 5,
    })
    rv = client.get("/api/scout/signals")
    sig = rv.get_json()["signals"][0]
    assert sig["suggested_quantity"] == 5


def test_signals_suggested_quantity_investment_sizing(client, mocker):
    _signals_setup(mocker, settings={
        "use_investment_sizing": True,
        "investment_per_trade_inr": 20_000,
        "auto_trade_quantity": 1,
    })
    rv = client.get("/api/scout/signals")
    sig = rv.get_json()["signals"][0]
    assert sig["suggested_quantity"] == 7  # floor(20000 / 2510)


def test_mark_taken_defaults_quantity_from_settings(client, mocker):
    sig = {
        "id": 10,
        "symbol": "RELIANCE",
        "action": "BUY",
        "ltp": 2500.0,
        "invalidation": 2450.0,
        "signal_type": "OR_BREAK_UP",
        "triggered_at": "2026-08-12 10:00:00",
        "meta": {},
    }
    mock_sig = MagicMock()
    mock_sig.get.return_value = sig
    mock_trade = MagicMock()
    mock_trade.open_signal_ids.return_value = set()
    mock_trade.open_trades.return_value = []
    mock_trade.mark_taken.return_value = 42
    mock_trade.get.return_value = {
        "id": 42,
        "symbol": "RELIANCE",
        "action": "BUY",
        "entry_price": 2500.0,
        "quantity": 5,
        "executed_at": "2026-08-12 10:01:00",
    }
    mocker.patch("scout.routes.ScoutSignalRepo", return_value=mock_sig)
    mocker.patch("scout.routes.ScoutTradeRepo", return_value=mock_trade)
    mocker.patch("scout.routes.get_scout_settings", return_value={
        "use_investment_sizing": False,
        "auto_trade_quantity": 5,
    })
    mocker.patch("scout.routes.latest_equity_ltps", return_value={"RELIANCE": {"ltp": 2500.0}})
    mocker.patch("scout.routes.enrich_signal", return_value={"validity_status": "ACTIVE"})
    mocker.patch("scout.routes.build_entry_audit", return_value='{"mode":"manual"}')
    mocker.patch("scout.routes.build_exit_plan", return_value={"dashboard": {}})
    mocker.patch("scout.routes.evaluate_exit_alerts", return_value={"close_now": False, "alerts": []})
    mocker.patch("scout.routes.now_ist", return_value=datetime(2026, 8, 12, 10, 1, 0))

    rv = client.post("/api/scout/signals/10/mark-taken", json={"entry_price": 2500})
    assert rv.status_code == 200
    assert mock_trade.mark_taken.call_args.kwargs["quantity"] == 5


def test_mark_taken_investment_sizing_when_quantity_omitted(client, mocker):
    sig = {
        "id": 10,
        "symbol": "RELIANCE",
        "action": "BUY",
        "ltp": 2500.0,
        "invalidation": 2450.0,
        "signal_type": "OR_BREAK_UP",
        "triggered_at": "2026-08-12 10:00:00",
        "meta": {},
    }
    mock_sig = MagicMock()
    mock_sig.get.return_value = sig
    mock_trade = MagicMock()
    mock_trade.open_signal_ids.return_value = set()
    mock_trade.open_trades.return_value = []
    mock_trade.mark_taken.return_value = 43
    mock_trade.get.return_value = {"id": 43, "executed_at": "2026-08-12 10:01:00"}
    mocker.patch("scout.routes.ScoutSignalRepo", return_value=mock_sig)
    mocker.patch("scout.routes.ScoutTradeRepo", return_value=mock_trade)
    mocker.patch("scout.routes.get_scout_settings", return_value={
        "use_investment_sizing": True,
        "investment_per_trade_inr": 20_000,
    })
    mocker.patch("scout.routes.latest_equity_ltps", return_value={"RELIANCE": {"ltp": 2500.0}})
    mocker.patch("scout.routes.enrich_signal", return_value={"validity_status": "ACTIVE"})
    mocker.patch("scout.routes.build_entry_audit", return_value='{"mode":"manual"}')
    mocker.patch("scout.routes.build_exit_plan", return_value={"dashboard": {}})
    mocker.patch("scout.routes.evaluate_exit_alerts", return_value={"close_now": False, "alerts": []})
    mocker.patch("scout.routes.now_ist", return_value=datetime(2026, 8, 12, 10, 1, 0))

    rv = client.post("/api/scout/signals/10/mark-taken", json={"entry_price": 2500})
    assert rv.status_code == 200
    assert mock_trade.mark_taken.call_args.kwargs["quantity"] == 8


def test_mark_taken_uses_explicit_quantity_override(client, mocker):
    sig = {
        "id": 10,
        "symbol": "RELIANCE",
        "action": "BUY",
        "ltp": 2500.0,
        "invalidation": 2450.0,
        "signal_type": "OR_BREAK_UP",
        "triggered_at": "2026-08-12 10:00:00",
        "meta": {},
    }
    mock_sig = MagicMock()
    mock_sig.get.return_value = sig
    mock_trade = MagicMock()
    mock_trade.open_signal_ids.return_value = set()
    mock_trade.open_trades.return_value = []
    mock_trade.mark_taken.return_value = 44
    mock_trade.get.return_value = {"id": 44, "executed_at": "2026-08-12 10:01:00"}
    mocker.patch("scout.routes.ScoutSignalRepo", return_value=mock_sig)
    mocker.patch("scout.routes.ScoutTradeRepo", return_value=mock_trade)
    mocker.patch("scout.routes.get_scout_settings", return_value={
        "use_investment_sizing": True,
        "investment_per_trade_inr": 20_000,
    })
    mocker.patch("scout.routes.latest_equity_ltps", return_value={"RELIANCE": {"ltp": 2500.0}})
    mocker.patch("scout.routes.enrich_signal", return_value={"validity_status": "ACTIVE"})
    mocker.patch("scout.routes.build_entry_audit", return_value='{"mode":"manual"}')
    mocker.patch("scout.routes.build_exit_plan", return_value={"dashboard": {}})
    mocker.patch("scout.routes.evaluate_exit_alerts", return_value={"close_now": False, "alerts": []})
    mocker.patch("scout.routes.now_ist", return_value=datetime(2026, 8, 12, 10, 1, 0))

    rv = client.post(
        "/api/scout/signals/10/mark-taken",
        json={"entry_price": 2500, "quantity": 12},
    )
    assert rv.status_code == 200
    assert mock_trade.mark_taken.call_args.kwargs["quantity"] == 12
