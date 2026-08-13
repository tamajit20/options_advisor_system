"""Tests for Scout execution engine (paper mode — no Kite calls)."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from scout.execution_engine import (
    _apply_step2_result,
    execute_entry,
    execution_mode_label,
    paper_close_if_triggered,
    place_protection_and_target,
    zerodha_execute_enabled,
)


@pytest.fixture
def sample_signal():
    return {
        "id": 1,
        "symbol": "RELIANCE",
        "action": "BUY",
        "signal_type": "OR_BREAK_UP",
        "invalidation": 98.0,
        "ltp": 100.0,
        "meta": {"or_high": 101.0, "or_low": 99.0},
    }


def test_zerodha_execute_disabled_by_default():
    from scout.config_loader import invalidate_settings_cache
    invalidate_settings_cache()
    assert zerodha_execute_enabled() is False
    assert zerodha_execute_enabled({"zerodha_execute_orders": True}) is True
    assert execution_mode_label({"zerodha_execute_orders": True}) == "zerodha"


def test_paper_execute_entry_creates_orders(sample_signal):
    fake_db = MagicMock()
    trade_repo = MagicMock()
    trade_repo.mark_taken.return_value = 42
    trade_repo.get.return_value = {
        "id": 42,
        "symbol": "RELIANCE",
        "action": "BUY",
        "quantity": 10,
        "entry_price": 100.5,
        "status": "OPEN",
        "execution_mode": "paper",
    }
    order_repo = MagicMock()
    order_repo.get_leg.return_value = None
    order_repo.leg_placed.return_value = False
    order_repo.insert.return_value = 1

    with patch("scout.execution_engine.ScoutTradeRepo", return_value=trade_repo), \
         patch("scout.execution_engine.ScoutTradeOrderRepo", return_value=order_repo), \
         patch("scout.execution_engine.build_entry_audit", return_value="audit"):
        out = execute_entry(
            fake_db,
            signal_id=1,
            sig=sample_signal,
            entry_price=100.5,
            quantity=10,
            settings={"min_target_r": 2.0, "breakeven_at_r": 1.0, "trail_stop_r_fraction": 0.5},
            mode="paper",
        )

    assert out["trade_id"] == 42
    assert out["execution_mode"] == "paper"
    assert trade_repo.mark_taken.called
    # Entry + stop + target orders recorded
    assert order_repo.insert.call_count >= 3


def test_paper_close_on_stop_hit(sample_signal):
    fake_db = MagicMock()
    trade = {
        "id": 7,
        "symbol": "RELIANCE",
        "action": "BUY",
        "entry_price": 100.0,
        "quantity": 5,
        "status": "OPEN",
        "execution_mode": "paper",
        "peak_price": None,
    }
    trade_repo = MagicMock()
    trade_repo.close.return_value = {**trade, "status": "CLOSED", "exit_price": 97.5, "pnl": -12.5}
    order_repo = MagicMock()
    order_repo.get_leg.return_value = None

    with patch("scout.execution_engine.ScoutTradeRepo", return_value=trade_repo), \
         patch("scout.execution_engine.ScoutTradeOrderRepo", return_value=order_repo):
        result = paper_close_if_triggered(
            fake_db,
            trade=trade,
            signal=sample_signal,
            live_ltp=97.5,
            settings={"min_target_r": 2.0, "breakeven_at_r": 1.0, "trail_stop_r_fraction": 0.5},
        )

    assert result is not None
    trade_repo.close.assert_called_once()


def test_apply_step2_marks_unprotected_on_stop_fail():
    trade_repo = MagicMock()
    trade_repo.get.return_value = {"id": 1, "status": "OPEN"}
    _apply_step2_result(trade_repo, 1, {"stop_ok": False, "target_ok": False}, live=True)
    trade_repo.set_status.assert_called_once_with(1, "UNPROTECTED")


def test_apply_step2_restores_open_when_stop_ok():
    trade_repo = MagicMock()
    trade_repo.get.return_value = {"id": 1, "status": "UNPROTECTED"}
    _apply_step2_result(trade_repo, 1, {"stop_ok": True, "target_ok": True}, live=True)
    trade_repo.set_status.assert_called_once_with(1, "OPEN")


def test_place_protection_records_failed_stop():
    fake_db = MagicMock()
    order_repo = MagicMock()
    order_repo.leg_placed.return_value = False
    trade_repo = MagicMock()
    trade = {"id": 9, "symbol": "RELIANCE", "action": "BUY", "quantity": 5}
    signal = {"invalidation": 98.0, "meta": {}}

    with patch("scout.execution_engine.ScoutTradeOrderRepo", return_value=order_repo), \
         patch("scout.execution_engine.ScoutTradeRepo", return_value=trade_repo), \
         patch("scout.execution_engine._place_stop_order", side_effect=RuntimeError("kite down")), \
         patch("scout.execution_engine.build_exit_plan", return_value={"stop_price": 97.0, "target_price": 105.0}), \
         patch("scout.execution_engine._record_order") as rec:
        result = place_protection_and_target(
            fake_db, trade=trade, signal=signal, entry_price=100.0,
            settings={"min_target_r": 2.0}, live=True,
        )
    assert result["stop_ok"] is False
    assert rec.called


def test_process_pending_entries_cancels_stale_order(sample_signal):
    from scout.execution_engine import process_pending_entries

    fake_db = MagicMock()
    trade_repo = MagicMock()
    order_repo = MagicMock()
    trade_repo.pending_entry_trades.return_value = [{
        "id": 7,
        "signal_id": 1,
        "symbol": "RELIANCE",
        "action": "BUY",
        "signal_type": "OR_BREAK_UP",
        "entry_price": 100.5,
        "quantity": 10,
        "executed_at": datetime(2026, 7, 30, 9, 0, 0),
    }]
    order_repo.get_leg.return_value = {
        "id": 1,
        "trade_id": 7,
        "leg": "ENTRY",
        "status": "OPEN",
        "kite_order_id": "OID1",
    }

    with patch("scout.execution_engine.ScoutTradeRepo", return_value=trade_repo), \
         patch("scout.execution_engine.ScoutTradeOrderRepo", return_value=order_repo), \
         patch("database.scout_models.ScoutSignalRepo") as sig_cls, \
         patch("scout.execution_engine._sync_order_status", side_effect=lambda repo, row, live: row), \
         patch("scout.execution_engine._cancel_broker_order") as cancel, \
         patch("scout.execution_engine.zerodha_execute_enabled", return_value=True), \
         patch("scout.signal_enrichment.evaluate_signal_status", return_value="FAILED_BREAKOUT"):
        sig_cls.return_value.get.return_value = sample_signal
        out = process_pending_entries(
            fake_db,
            spot_lookup=lambda s: 100.0,
            settings={"entry_pending_max_minutes": 15, "signal_valid_minutes": 30},
        )

    assert out[0]["event"] == "entry_cancelled"
    assert "failed_breakout" in out[0]["reason"]
    cancel.assert_called_once()
    trade_repo.mark_failed.assert_called_once()


def test_sync_order_status_persists_filled_quantity_on_partial():
    from scout.execution_engine import _sync_order_status

    order_repo = MagicMock()
    order_row = {
        "id": 7,
        "trade_id": 3,
        "leg": "ENTRY",
        "quantity": 10,
        "kite_order_id": "OID123",
    }
    order_repo.get_leg.return_value = {
        **order_row,
        "status": "OPEN",
        "filled_quantity": 8,
        "price": 100.5,
    }

    with patch("providers.zerodha.order_client.KiteOrderClient") as kite_cls:
        kite_cls.return_value.order_history.return_value = [{"status": "OPEN"}]
        kite_cls.return_value.latest_status.return_value = {
            "status": "OPEN",
            "average_price": 100.5,
            "filled_quantity": 8,
        }
        out = _sync_order_status(order_repo, order_row, live=True)

    order_repo.update_status.assert_called_once()
    kwargs = order_repo.update_status.call_args.kwargs
    assert kwargs["status"] == "OPEN"
    assert kwargs["filled_quantity"] == 8
    assert kwargs["price"] == 100.5
    assert out["filled_quantity"] == 8


def test_sync_order_status_updates_filled_quantity_on_later_poll():
    from scout.execution_engine import _sync_order_status

    order_repo = MagicMock()
    order_row = {
        "id": 7,
        "trade_id": 3,
        "leg": "ENTRY",
        "quantity": 10,
        "kite_order_id": "OID123",
        "filled_quantity": 3,
    }
    order_repo.get_leg.return_value = {
        **order_row,
        "status": "OPEN",
        "filled_quantity": 8,
        "price": 100.5,
    }

    with patch("providers.zerodha.order_client.KiteOrderClient") as kite_cls:
        kite_cls.return_value.order_history.return_value = [{"status": "OPEN"}]
        kite_cls.return_value.latest_status.return_value = {
            "status": "OPEN",
            "average_price": 100.5,
            "filled_quantity": 8,
        }
        _sync_order_status(order_repo, order_row, live=True)

    assert order_repo.update_status.call_args.kwargs["filled_quantity"] == 8


def test_sync_order_status_complete_without_filled_qty_uses_order_quantity():
    from scout.execution_engine import _sync_order_status

    order_repo = MagicMock()
    order_row = {
        "id": 7,
        "trade_id": 3,
        "leg": "ENTRY",
        "quantity": 10,
        "kite_order_id": "OID123",
    }
    order_repo.get_leg.return_value = {**order_row, "status": "COMPLETE", "filled_quantity": 10}

    with patch("providers.zerodha.order_client.KiteOrderClient") as kite_cls:
        kite_cls.return_value.order_history.return_value = [{"status": "COMPLETE"}]
        kite_cls.return_value.latest_status.return_value = {
            "status": "COMPLETE",
            "average_price": 100.5,
        }
        _sync_order_status(order_repo, order_row, live=True)

    assert order_repo.update_status.call_args.kwargs["filled_quantity"] == 10
