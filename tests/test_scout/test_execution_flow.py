"""Tests for scout execution flow builder."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from scout.execution_flow import (
    _format_order,
    _order_status_class,
    _step_status,
    build_flow_items,
    build_trade_execution_flow,
    signal_eligible_for_execution_flow,
)


@pytest.mark.parametrize(
    "status,expected",
    [
        ("COMPLETE", "done"),
        ("FILLED", "done"),
        ("SIMULATED", "done"),
        ("PLACED", "active"),
        ("OPEN", "active"),
        ("TRIGGER PENDING", "active"),
        ("CANCELLED", "failed"),
        ("REJECTED", "failed"),
        ("FAILED", "failed"),
        ("", "pending"),
        ("UNKNOWN", "pending"),
    ],
)
def test_order_status_class(status, expected):
    assert _order_status_class(status) == expected


@pytest.mark.parametrize(
    "trade_status,step,orders,expected",
    [
        ("PENDING_ENTRY", 1, [], "active"),
        ("OPEN", 1, [], "done"),
        ("CLOSED", 1, [], "done"),
        ("FAILED", 1, [], "failed"),
        ("PENDING_ENTRY", 2, [], "pending"),
        ("OPEN", 2, [{"step_num": 2, "status": "COMPLETE"}], "done"),
        ("OPEN", 2, [{"step_num": 2, "status": "PLACED"}], "active"),
        ("OPEN", 3, [], "active"),
        ("CLOSED", 3, [], "done"),
        ("PENDING_ENTRY", 3, [], "pending"),
    ],
)
def test_step_status(trade_status, step, orders, expected):
    assert _step_status(trade_status, step, orders) == expected


def test_format_order_leg_labels():
    out = _format_order({
        "id": 1,
        "leg": "STOP_LOSS",
        "step_num": 2,
        "order_type": "SL-M",
        "transaction_type": "SELL",
        "status": "PLACED",
        "quantity": 10,
        "filled_quantity": 8,
    })
    assert out["leg_label"] == "Stop loss"
    assert out["status_class"] == "active"
    assert out["filled_quantity"] == 8


@pytest.mark.parametrize(
    "market_open,validity,expected",
    [
        (True, "ACTIVE", True),
        (True, "EXPIRED", False),
        (True, "INVALIDATED", False),
        (True, "OUT_OF_RANGE", False),
        (True, "FAILED_BREAKOUT", False),
        (False, "ACTIVE", False),
        (False, "EXPIRED", False),
    ],
)
def test_signal_eligible_for_execution_flow(market_open, validity, expected):
    assert signal_eligible_for_execution_flow(
        market_open=market_open,
        validity_status=validity,
    ) is expected


def _patch_flow_repos(mocker, *, sig_repo, trade_repo, order_repo):
    mocker.patch("scout.execution_flow.ScoutSignalRepo", return_value=sig_repo)
    mocker.patch("scout.execution_flow.ScoutTradeRepo", return_value=trade_repo)
    mocker.patch("scout.execution_flow.ScoutTradeOrderRepo", return_value=order_repo)
    mocker.patch("scout.execution_flow.latest_equity_ltps", return_value={})


def test_build_flow_items_hides_stale_signals_off_market(mocker):
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
    _patch_flow_repos(mocker, sig_repo=sig_repo, trade_repo=trade_repo, order_repo=order_repo)
    mocker.patch("scout.execution_flow.is_market_open", return_value=False)
    mocker.patch(
        "scout.execution_flow.enrich_signal",
        return_value={**sig, "validity_status": "EXPIRED"},
    )

    items = build_flow_items(MagicMock(), settings={})
    assert items == []


def test_build_flow_items_includes_active_signals_when_market_open(mocker):
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
    _patch_flow_repos(mocker, sig_repo=sig_repo, trade_repo=trade_repo, order_repo=order_repo)
    mocker.patch("scout.execution_flow.latest_equity_ltps", return_value={"RELIANCE": {"ltp": 2500.0}})
    mocker.patch("scout.execution_flow.is_market_open", return_value=True)
    mocker.patch("scout.execution_flow.enrich_signal", return_value=enriched)
    mocker.patch("scout.execution_flow.now_ist", return_value=datetime(2026, 8, 13, 11, 0, 0))

    items = build_flow_items(MagicMock(), settings={})

    assert len(items) == 1
    assert items[0]["kind"] == "signal"
    assert items[0]["signal"]["validity_status"] == "ACTIVE"


def test_build_flow_items_skips_expired_signal_when_market_open(mocker):
    sig = {"id": 101, "symbol": "TCS", "action": "BUY", "ltp": 4000.0}
    sig_repo = MagicMock()
    sig_repo.recent.return_value = [sig]
    trade_repo = MagicMock()
    trade_repo.open_trades.return_value = []
    order_repo = MagicMock()
    _patch_flow_repos(mocker, sig_repo=sig_repo, trade_repo=trade_repo, order_repo=order_repo)
    mocker.patch("scout.execution_flow.is_market_open", return_value=True)
    mocker.patch(
        "scout.execution_flow.enrich_signal",
        return_value={**sig, "validity_status": "EXPIRED"},
    )

    assert build_flow_items(MagicMock(), settings={}) == []


def test_build_flow_items_keeps_open_trades_when_market_closed(mocker):
    trade = {
        "id": 5,
        "signal_id": 50,
        "symbol": "HDFCBANK",
        "status": "OPEN",
        "action": "BUY",
        "entry_price": 1700.0,
        "execution_mode": "paper",
    }
    sig = {"id": 50, "symbol": "HDFCBANK", "action": "BUY"}
    sig_repo = MagicMock()
    sig_repo.recent.return_value = [sig]
    sig_repo.get.return_value = sig
    trade_repo = MagicMock()
    trade_repo.open_trades.return_value = [trade]
    order_repo = MagicMock()
    order_repo.for_trade.return_value = [
        {"step_num": 1, "leg": "ENTRY", "status": "COMPLETE"},
    ]
    _patch_flow_repos(mocker, sig_repo=sig_repo, trade_repo=trade_repo, order_repo=order_repo)
    mocker.patch("scout.execution_flow.is_market_open", return_value=False)
    mocker.patch("scout.execution_flow.latest_equity_ltps", return_value={"HDFCBANK": {"ltp": 1710.0}})
    mocker.patch(
        "scout.execution_flow.build_trade_execution_flow",
        return_value={"trade_status": "OPEN", "steps": []},
    )

    items = build_flow_items(MagicMock(), settings={})

    assert len(items) == 1
    assert items[0]["kind"] == "trade"
    assert items[0]["trade"]["id"] == 5


def test_build_flow_items_does_not_duplicate_signal_with_open_trade(mocker):
    sig = {"id": 60, "symbol": "INFY", "action": "SELL", "ltp": 1800.0}
    trade = {
        "id": 8,
        "signal_id": 60,
        "symbol": "INFY",
        "status": "OPEN",
        "action": "SELL",
        "entry_price": 1800.0,
    }
    sig_repo = MagicMock()
    sig_repo.recent.return_value = [sig]
    sig_repo.get.return_value = sig
    trade_repo = MagicMock()
    trade_repo.open_trades.return_value = [trade]
    order_repo = MagicMock()
    order_repo.for_trade.return_value = []
    _patch_flow_repos(mocker, sig_repo=sig_repo, trade_repo=trade_repo, order_repo=order_repo)
    mocker.patch("scout.execution_flow.is_market_open", return_value=True)
    mocker.patch(
        "scout.execution_flow.enrich_signal",
        return_value={**sig, "validity_status": "ACTIVE"},
    )
    mocker.patch(
        "scout.execution_flow.build_trade_execution_flow",
        return_value={"trade_status": "OPEN", "steps": []},
    )

    items = build_flow_items(MagicMock(), settings={})

    assert len(items) == 1
    assert items[0]["kind"] == "trade"


def test_build_flow_items_executed_trades_before_waiting(mocker):
    open_trade = {
        "id": 10,
        "signal_id": 100,
        "symbol": "RELIANCE",
        "status": "OPEN",
        "action": "BUY",
        "entry_price": 2500.0,
    }
    pending_trade = {
        "id": 5,
        "signal_id": 50,
        "symbol": "TCS",
        "status": "PENDING_ENTRY",
        "action": "BUY",
        "entry_price": 3500.0,
    }
    sig = {"id": 200, "symbol": "INFY", "action": "BUY", "ltp": 1800.0}
    sig_repo = MagicMock()
    sig_repo.recent.return_value = [sig]
    sig_repo.get.side_effect = lambda sid: {
        100: {"id": 100, "symbol": "RELIANCE"},
        50: {"id": 50, "symbol": "TCS"},
    }.get(int(sid))
    trade_repo = MagicMock()
    trade_repo.open_trades.return_value = [pending_trade, open_trade]
    order_repo = MagicMock()
    order_repo.for_trade.return_value = []
    _patch_flow_repos(mocker, sig_repo=sig_repo, trade_repo=trade_repo, order_repo=order_repo)
    mocker.patch("scout.execution_flow.is_market_open", return_value=True)
    mocker.patch("scout.execution_flow.latest_equity_ltps", return_value={})
    mocker.patch("scout.execution_flow.now_ist", return_value=datetime(2026, 8, 13, 11, 0, 0))
    mocker.patch(
        "scout.execution_flow.enrich_signal",
        return_value={**sig, "validity_status": "ACTIVE"},
    )
    mocker.patch(
        "scout.execution_flow.build_trade_execution_flow",
        side_effect=lambda **kw: {"trade_status": kw["trade"]["status"], "steps": []},
    )

    items = build_flow_items(MagicMock(), settings={})

    assert [it.get("kind") for it in items] == ["trade", "trade", "signal"]
    assert items[0]["trade"]["id"] == 10
    assert items[0]["trade"]["status"] == "OPEN"
    assert items[1]["trade"]["id"] == 5
    assert items[1]["trade"]["status"] == "PENDING_ENTRY"
    assert items[2]["signal"]["id"] == 200


def test_build_trade_execution_flow_open_trade(mocker):
    trade = {
        "id": 1,
        "signal_id": 10,
        "symbol": "RELIANCE",
        "status": "OPEN",
        "action": "BUY",
        "entry_price": 2500.0,
        "execution_mode": "paper",
    }
    orders = [
        {"step_num": 1, "leg": "ENTRY", "status": "COMPLETE", "price": 2500.0},
        {"step_num": 2, "leg": "STOP_LOSS", "status": "PLACED", "trigger_price": 2480.0},
    ]
    mocker.patch(
        "scout.execution_flow.build_exit_plan",
        return_value={"dashboard": {"prices": {"entry": 2500, "stop": 2480, "target": 2550}}},
    )
    mocker.patch(
        "scout.execution_flow.evaluate_exit_alerts",
        return_value={"urgency": "none", "alerts": []},
    )
    mocker.patch(
        "scout.execution_flow.scout_trade_mtm",
        return_value={"mtm": 25.0},
    )
    mocker.patch("scout.execution_flow.zerodha_execute_enabled", return_value=False)
    mocker.patch("scout.execution_flow.format_square_off_time", return_value="15:20 IST")

    flow = build_trade_execution_flow(
        trade=trade,
        signal={"action": "BUY", "meta": {}},
        orders=orders,
        live_ltp=2525.0,
        settings={},
    )

    assert flow["trade_status"] == "OPEN"
    assert flow["current_step"] == 3
    assert flow["mtm"]["mtm"] == 25.0
    assert len(flow["steps"]) == 3
    assert flow["steps"][0]["status"] == "done"
    assert flow["steps"][2]["status"] == "active"
    assert flow["zerodha_live"] is False


def test_build_trade_execution_flow_pending_entry(mocker):
    trade = {
        "id": 2,
        "symbol": "TCS",
        "status": "PENDING_ENTRY",
        "action": "BUY",
        "entry_price": 0,
        "execution_mode": "zerodha",
    }
    mocker.patch("scout.execution_flow.build_exit_plan", return_value={"dashboard": {"prices": {}}})
    mocker.patch("scout.execution_flow.evaluate_exit_alerts", return_value={"urgency": "none", "alerts": []})
    mocker.patch("scout.execution_flow.scout_trade_mtm", return_value={"mtm": None})
    mocker.patch("scout.execution_flow.zerodha_execute_enabled", return_value=True)
    mocker.patch("scout.execution_flow.format_square_off_time", return_value="15:20 IST")

    flow = build_trade_execution_flow(
        trade=trade,
        signal=None,
        orders=[],
        live_ltp=None,
        settings={"zerodha_execute_orders": True},
    )

    assert flow["current_step"] == 1
    assert flow["steps"][0]["status"] == "active"
    assert flow["steps"][1]["status"] == "pending"
    assert flow["zerodha_live"] is True
