"""Tests for stale PENDING_ENTRY cancellation."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from scout.execution_engine import (
    _pending_entry_cancel_reason,
    process_pending_entries,
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
        "triggered_at": datetime(2026, 7, 30, 10, 0, 0),
        "meta": {"or_high": 101.0, "or_low": 99.0},
    }


def test_pending_entry_cancel_reason_none_when_active(sample_signal):
    trade = {"symbol": "RELIANCE", "entry_price": 100.5, "executed_at": datetime(2026, 7, 30, 10, 1, 0)}
    frozen = datetime(2026, 7, 30, 10, 5, 0)
    with patch("scout.signal_enrichment.evaluate_signal_status", return_value="ACTIVE"), \
         patch("scout.signal_enrichment.enrich_signal", return_value={"validity_status": "ACTIVE"}), \
         patch("scout.execution_engine.now_ist", return_value=frozen):
        reason = _pending_entry_cancel_reason(
            trade=trade,
            signal=sample_signal,
            spot_lookup=lambda s: 100.5,
            settings={"entry_pending_max_minutes": 15, "signal_valid_minutes": 30},
        )
    assert reason is None


def test_pending_entry_cancel_reason_expired(sample_signal):
    trade = {"symbol": "RELIANCE", "entry_price": 100.5}
    with patch("scout.signal_enrichment.evaluate_signal_status", return_value="EXPIRED"):
        reason = _pending_entry_cancel_reason(
            trade=trade,
            signal=sample_signal,
            spot_lookup=lambda s: 100.5,
            settings={"entry_pending_max_minutes": 15},
        )
    assert reason == "entry_cancel_expired"


def test_pending_entry_cancel_reason_timeout(sample_signal):
    trade = {
        "symbol": "RELIANCE",
        "entry_price": 100.5,
        "executed_at": datetime(2026, 7, 30, 9, 0, 0),
    }
    frozen = datetime(2026, 7, 30, 9, 20, 0)
    with patch("scout.signal_enrichment.evaluate_signal_status", return_value="ACTIVE"), \
         patch("scout.signal_enrichment.enrich_signal", return_value={"validity_status": "ACTIVE"}), \
         patch("scout.execution_engine.now_ist", return_value=frozen):
        reason = _pending_entry_cancel_reason(
            trade=trade,
            signal=sample_signal,
            spot_lookup=lambda s: 100.5,
            settings={"entry_pending_max_minutes": 15},
        )
    assert reason == "entry_cancel_timeout"


def test_pending_entry_cancel_reason_out_of_range(sample_signal):
    trade = {"symbol": "RELIANCE", "entry_price": 100.5}
    with patch("scout.signal_enrichment.evaluate_signal_status", return_value="OUT_OF_RANGE"):
        reason = _pending_entry_cancel_reason(
            trade=trade,
            signal=sample_signal,
            spot_lookup=lambda s: 105.0,
            settings={"entry_pending_max_minutes": 15},
        )
    assert reason == "entry_cancel_out_of_range"


def test_process_pending_entries_keeps_working_order(sample_signal):
    fake_db = MagicMock()
    trade_repo = MagicMock()
    order_repo = MagicMock()
    trade_repo.pending_entry_trades.return_value = [{
        "id": 3,
        "signal_id": 1,
        "symbol": "RELIANCE",
        "action": "BUY",
        "signal_type": "OR_BREAK_UP",
        "entry_price": 100.5,
        "quantity": 10,
        "executed_at": datetime(2026, 7, 30, 10, 0, 0),
    }]
    order_repo.get_leg.return_value = {
        "id": 1, "trade_id": 3, "leg": "ENTRY", "status": "OPEN", "kite_order_id": "OID",
    }

    with patch("scout.execution_engine.ScoutTradeRepo", return_value=trade_repo), \
         patch("scout.execution_engine.ScoutTradeOrderRepo", return_value=order_repo), \
         patch("database.scout_models.ScoutSignalRepo") as sig_cls, \
         patch("scout.execution_engine._sync_order_status", side_effect=lambda r, row, live: row), \
         patch("scout.execution_engine._pending_entry_cancel_reason", return_value=None), \
         patch("scout.execution_engine.zerodha_execute_enabled", return_value=True):
        sig_cls.return_value.get.return_value = sample_signal
        out = process_pending_entries(
            fake_db,
            spot_lookup=lambda s: 100.5,
            settings={"entry_pending_max_minutes": 15},
        )
    assert out == []
    trade_repo.mark_failed.assert_not_called()


def test_process_pending_entries_fills_without_profit_flatten(sample_signal):
    """Filled entries proceed to protection — no post-fill profit gate flatten."""
    fake_db = MagicMock()
    trade_repo = MagicMock()
    order_repo = MagicMock()
    trade_repo.pending_entry_trades.return_value = [{
        "id": 8,
        "signal_id": 1,
        "symbol": "RELIANCE",
        "action": "BUY",
        "signal_type": "OR_BREAK_UP",
        "entry_price": 100.5,
        "quantity": 10,
        "executed_at": datetime(2026, 7, 30, 10, 0, 0),
    }]
    order_repo.get_leg.return_value = {
        "id": 1, "trade_id": 8, "leg": "ENTRY", "status": "COMPLETE",
        "price": 100.5, "kite_order_id": "OID",
    }
    trade_repo.get.return_value = {
        "id": 8, "symbol": "RELIANCE", "action": "BUY", "quantity": 10,
        "status": "OPEN", "entry_price": 100.5,
    }

    with patch("scout.execution_engine.ScoutTradeRepo", return_value=trade_repo), \
         patch("scout.execution_engine.ScoutTradeOrderRepo", return_value=order_repo), \
         patch("database.scout_models.ScoutSignalRepo") as sig_cls, \
         patch("scout.execution_engine._sync_order_status", side_effect=lambda r, row, live: row), \
         patch("scout.execution_engine.zerodha_execute_enabled", return_value=True), \
         patch("scout.execution_engine.place_protection_and_target", return_value={"stop_ok": True, "target_ok": True}), \
         patch("scout.execution_engine._apply_step2_result"), \
         patch("scout.execution_engine.run_catch_up_watch"):
        sig_cls.return_value.get.return_value = sample_signal
        out = process_pending_entries(
            fake_db,
            spot_lookup=lambda s: 100.5,
            settings={"min_target_r": 2.5},
        )
    assert out[0]["event"] == "entry_filled"
    assert "flattened" not in out[0]["event"]
    trade_repo.activate_from_fill.assert_called_once()


def test_pending_entry_cancel_reason_failed_breakout(sample_signal):
    trade = {"symbol": "RELIANCE", "entry_price": 100.5}
    with patch("scout.signal_enrichment.evaluate_signal_status", return_value="FAILED_BREAKOUT"):
        reason = _pending_entry_cancel_reason(
            trade=trade,
            signal=sample_signal,
            spot_lookup=lambda s: 100.2,
            settings={"entry_pending_max_minutes": 15},
        )
    assert reason == "entry_cancel_failed_breakout"


def test_process_pending_entries_cancels_stale_order(sample_signal):
    fake_db = MagicMock()
    trade_repo = MagicMock()
    order_repo = MagicMock()
    trade_repo.pending_entry_trades.return_value = [{
        "id": 5,
        "signal_id": 1,
        "symbol": "RELIANCE",
        "action": "BUY",
        "signal_type": "OR_BREAK_UP",
        "entry_price": 100.5,
        "quantity": 10,
        "executed_at": datetime(2026, 7, 30, 10, 0, 0),
    }]
    order_repo.get_leg.return_value = {
        "id": 2, "trade_id": 5, "leg": "ENTRY", "status": "OPEN", "kite_order_id": "OID",
    }

    with patch("scout.execution_engine.ScoutTradeRepo", return_value=trade_repo), \
         patch("scout.execution_engine.ScoutTradeOrderRepo", return_value=order_repo), \
         patch("database.scout_models.ScoutSignalRepo") as sig_cls, \
         patch("scout.execution_engine._sync_order_status", side_effect=lambda r, row, live: row), \
         patch("scout.execution_engine._pending_entry_cancel_reason", return_value="entry_cancel_timeout"), \
         patch("scout.execution_engine._cancel_broker_order") as cancel_order, \
         patch("scout.execution_engine.zerodha_execute_enabled", return_value=True):
        sig_cls.return_value.get.return_value = sample_signal
        out = process_pending_entries(
            fake_db,
            spot_lookup=lambda s: 100.5,
            settings={"entry_pending_max_minutes": 15},
        )

    assert len(out) == 1
    assert out[0]["event"] == "entry_cancelled"
    assert out[0]["reason"] == "entry_cancel_timeout"
    cancel_order.assert_called_once()
    trade_repo.mark_failed.assert_called_once_with(5, reason="entry_cancel_timeout")
