"""Tests for failed-breakout exit on open trades."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

from scout.execution_engine import manage_open_trade_step3, paper_close_if_triggered


def _open_trade(**kw):
    base = {
        "id": 12,
        "symbol": "RELIANCE",
        "action": "BUY",
        "entry_price": 101.0,
        "quantity": 5,
        "status": "OPEN",
        "execution_mode": "paper",
        "peak_price": 102.0,
        "executed_at": None,
    }
    base.update(kw)
    return base


def _or_signal(**kw):
    base = {
        "action": "BUY",
        "signal_type": "OR_BREAK_UP",
        "invalidation": 99.0,
        "meta": {"or_high": 101.0, "or_low": 99.0},
    }
    base.update(kw)
    return base


def test_manage_open_trade_closes_on_failed_breakout_paper():
    fake_db = MagicMock()
    trade_repo = MagicMock()
    trade_repo.close.return_value = {"id": 12, "status": "CLOSED", "exit_reason": "failed_breakout"}
    order_repo = MagicMock()
    order_repo.get_leg.return_value = None

    with patch("scout.execution_engine.ScoutTradeRepo", return_value=trade_repo), \
         patch("scout.execution_engine.ScoutTradeOrderRepo", return_value=order_repo):
        result = manage_open_trade_step3(
            fake_db,
            trade=_open_trade(),
            signal=_or_signal(),
            live_ltp=100.5,
            settings={"min_target_r": 2.5},
            live=False,
        )

    assert result is not None
    trade_repo.close.assert_called_once()
    assert trade_repo.close.call_args.kwargs.get("exit_reason") == "failed_breakout"


def test_manage_open_trade_holds_when_breakout_intact():
    fake_db = MagicMock()
    trade_repo = MagicMock()
    order_repo = MagicMock()
    order_repo.get_leg.return_value = None
    frozen = datetime(2026, 7, 30, 11, 0, 0)

    with patch("scout.execution_engine.ScoutTradeRepo", return_value=trade_repo), \
         patch("scout.execution_engine.ScoutTradeOrderRepo", return_value=order_repo), \
         patch("scout.execution_engine.now_ist", return_value=frozen):
        result = manage_open_trade_step3(
            fake_db,
            trade=_open_trade(),
            signal=_or_signal(),
            live_ltp=101.5,
            settings={"min_target_r": 2.5, "square_off_time": "15:10"},
            live=False,
        )

    assert result is None
    trade_repo.close.assert_not_called()


def test_paper_close_if_triggered_exits_failed_breakout_before_stop():
    fake_db = MagicMock()
    trade = _open_trade()
    trade_repo = MagicMock()
    trade_repo.close.return_value = {**trade, "status": "CLOSED"}
    order_repo = MagicMock()
    order_repo.get_leg.return_value = None

    with patch("scout.execution_engine.ScoutTradeRepo", return_value=trade_repo), \
         patch("scout.execution_engine.ScoutTradeOrderRepo", return_value=order_repo):
        result = paper_close_if_triggered(
            fake_db,
            trade=trade,
            signal=_or_signal(),
            live_ltp=100.2,
            settings={"min_target_r": 2.5},
        )

    assert result is not None
    trade_repo.close.assert_called_once()


def test_failed_breakout_short_or_down():
    fake_db = MagicMock()
    trade_repo = MagicMock()
    trade_repo.close.return_value = {"id": 12, "status": "CLOSED"}
    order_repo = MagicMock()
    order_repo.get_leg.return_value = None
    signal = {
        "action": "SELL",
        "signal_type": "OR_BREAK_DOWN",
        "invalidation": 101.0,
        "meta": {"or_high": 101.0, "or_low": 99.0},
    }

    with patch("scout.execution_engine.ScoutTradeRepo", return_value=trade_repo), \
         patch("scout.execution_engine.ScoutTradeOrderRepo", return_value=order_repo):
        result = manage_open_trade_step3(
            fake_db,
            trade=_open_trade(action="SELL", entry_price=99.0),
            signal=signal,
            live_ltp=99.5,
            settings={"min_target_r": 2.5},
            live=False,
        )

    assert result is not None
    trade_repo.close.assert_called_once()


def test_manage_open_trade_range_break_failed_breakout():
    fake_db = MagicMock()
    trade_repo = MagicMock()
    trade_repo.close.return_value = {"id": 12, "status": "CLOSED", "exit_reason": "failed_breakout"}
    order_repo = MagicMock()
    order_repo.get_leg.return_value = None
    signal = {
        "action": "BUY",
        "signal_type": "RANGE_BREAK_UP",
        "invalidation": 99.0,
        "meta": {"box_high": 104.0, "box_low": 100.0},
    }

    with patch("scout.execution_engine.ScoutTradeRepo", return_value=trade_repo), \
         patch("scout.execution_engine.ScoutTradeOrderRepo", return_value=order_repo):
        result = manage_open_trade_step3(
            fake_db,
            trade=_open_trade(entry_price=104.5),
            signal=signal,
            live_ltp=103.5,
            settings={"min_target_r": 2.5},
            live=False,
        )

    assert result is not None
    trade_repo.close.assert_called_once()
    assert trade_repo.close.call_args.kwargs.get("exit_reason") == "failed_breakout"


def test_manage_unprotected_trade_closes_on_failed_breakout():
    fake_db = MagicMock()
    trade_repo = MagicMock()
    trade_repo.close.return_value = {"id": 12, "status": "CLOSED", "exit_reason": "failed_breakout"}
    order_repo = MagicMock()
    order_repo.get_leg.return_value = None

    with patch("scout.execution_engine.ScoutTradeRepo", return_value=trade_repo), \
         patch("scout.execution_engine.ScoutTradeOrderRepo", return_value=order_repo):
        result = manage_open_trade_step3(
            fake_db,
            trade=_open_trade(status="UNPROTECTED"),
            signal=_or_signal(),
            live_ltp=100.5,
            settings={"min_target_r": 2.5, "square_off_time": "15:10"},
            live=False,
        )

    assert result is not None
    trade_repo.close.assert_called_once()
    assert trade_repo.close.call_args.kwargs.get("exit_reason") == "failed_breakout"


def test_live_manage_open_trade_failed_breakout_places_exit_order():
    fake_db = MagicMock()
    trade_repo = MagicMock()
    trade_repo.close.return_value = {"id": 12, "status": "CLOSED"}
    order_repo = MagicMock()
    order_repo.get_leg.side_effect = lambda tid, leg: {"id": 1, "kite_order_id": "SL"} if leg == "STOP_LOSS" else {"id": 2, "kite_order_id": "TG"}

    with patch("scout.execution_engine.ScoutTradeRepo", return_value=trade_repo), \
         patch("scout.execution_engine.ScoutTradeOrderRepo", return_value=order_repo), \
         patch("scout.execution_engine._cancel_broker_order") as cancel, \
         patch("scout.execution_engine._place_exit_market", return_value=("EXIT-OID", "COMPLETE")), \
         patch("scout.execution_engine._record_order"), \
         patch("scout.execution_engine._close_trade", return_value={"id": 12, "status": "CLOSED"}):
        result = manage_open_trade_step3(
            fake_db,
            trade=_open_trade(execution_mode="live"),
            signal=_or_signal(),
            live_ltp=100.5,
            settings={"min_target_r": 2.5},
            live=True,
        )

    assert result is not None
    assert cancel.call_count == 2
