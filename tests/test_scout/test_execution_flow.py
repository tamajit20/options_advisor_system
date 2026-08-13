"""Tests for scout execution flow builder."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest


def test_build_flow_items_hides_stale_signals_off_market(mocker):
    from scout.execution_flow import build_flow_items

    sig = {
        "id": 99,
        "symbol": "RELIANCE",
        "action": "BUY",
        "ltp": 2500.0,
        "triggered_at": "2026-08-13 10:00:00",
    }
    sig_repo = MagicMock()
    sig_repo.recent.return_value = [sig]
    sig_repo.get.return_value = sig

    trade_repo = MagicMock()
    trade_repo.open_trades.return_value = []

    order_repo = MagicMock()

    db = MagicMock()
    mocker.patch("scout.execution_flow.ScoutSignalRepo", return_value=sig_repo)
    mocker.patch("scout.execution_flow.ScoutTradeRepo", return_value=trade_repo)
    mocker.patch("scout.execution_flow.ScoutTradeOrderRepo", return_value=order_repo)
    mocker.patch("scout.execution_flow.latest_equity_ltps", return_value={})
    mocker.patch("scout.execution_flow.is_market_open", return_value=False)
    mocker.patch(
        "scout.execution_flow.enrich_signal",
        return_value={**sig, "validity_status": "EXPIRED"},
    )

    items = build_flow_items(db, settings={})

    assert items == []


def test_build_flow_items_includes_active_signals_when_market_open(mocker):
    from scout.execution_flow import build_flow_items

    sig = {
        "id": 100,
        "symbol": "RELIANCE",
        "action": "BUY",
        "ltp": 2500.0,
        "triggered_at": "2026-08-13 10:00:00",
    }
    enriched = {**sig, "validity_status": "ACTIVE", "entry_min": 2490, "entry_max": 2510}

    sig_repo = MagicMock()
    sig_repo.recent.return_value = [sig]
    sig_repo.get.return_value = sig

    trade_repo = MagicMock()
    trade_repo.open_trades.return_value = []

    order_repo = MagicMock()

    db = MagicMock()
    mocker.patch("scout.execution_flow.ScoutSignalRepo", return_value=sig_repo)
    mocker.patch("scout.execution_flow.ScoutTradeRepo", return_value=trade_repo)
    mocker.patch("scout.execution_flow.ScoutTradeOrderRepo", return_value=order_repo)
    mocker.patch("scout.execution_flow.latest_equity_ltps", return_value={"RELIANCE": {"ltp": 2500.0}})
    mocker.patch("scout.execution_flow.is_market_open", return_value=True)
    mocker.patch("scout.execution_flow.enrich_signal", return_value=enriched)
    mocker.patch("scout.execution_flow.now_ist", return_value=datetime(2026, 8, 13, 11, 0, 0))

    items = build_flow_items(db, settings={})

    assert len(items) == 1
    assert items[0]["kind"] == "signal"
    assert items[0]["signal"]["validity_status"] == "ACTIVE"
