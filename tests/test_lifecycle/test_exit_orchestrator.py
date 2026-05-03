"""Tests for lifecycle/exit_orchestrator.py — daily exit decisions for open trades."""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest

import lifecycle.exit_orchestrator as orch


def _open_trade(trade_id: str = "TRD-1", suggestion_id: str = "SUG-1") -> dict:
    return {
        "trade_id": trade_id,
        "suggestion_id": suggestion_id,
        "trade_name": "TEST",
        "executed_on": date(2026, 4, 28),
        "position_type": "FULL",
        "net_credit_actual": 2250.0,
        "actual_max_profit": 2250.0,
        "actual_max_loss": 5250.0,
        "actual_stop_loss_level": None,
        "total_charges": 60.0,
        "status": "ACTIVE",
    }


def _sug_legs() -> list[dict]:
    """Bear call spread: short 23200 CE, long 23300 CE."""
    return [
        {"leg_order": 1, "symbol": "NIFTY", "expiry_date": date(2026, 5, 14),
         "strike": 23200.0, "option_type": "CE", "action": "SELL",
         "lots": 1, "lot_size": 75},
        {"leg_order": 2, "symbol": "NIFTY", "expiry_date": date(2026, 5, 14),
         "strike": 23300.0, "option_type": "CE", "action": "BUY",
         "lots": 1, "lot_size": 75},
    ]


def _trade_legs(filled: bool = True) -> list[dict]:
    return [
        {"leg_order": 1, "executed": 1 if filled else 0, "fill_price": 50.0},
        {"leg_order": 2, "executed": 1 if filled else 0, "fill_price": 20.0},
    ]


def _chain() -> list[dict]:
    return [
        {"strike": 23200.0, "option_type": "CE", "settle_price": 30.0, "close_price": 30.0},
        {"strike": 23300.0, "option_type": "CE", "settle_price": 12.0, "close_price": 12.0},
    ]


class TestRunExitEngine:
    def test_no_open_trades_returns_zero(self, mock_db, mocker):
        mocker.patch.object(orch.TradeRepo, "open_trades", return_value=[])
        n = orch.run_exit_engine(mock_db, date(2026, 5, 4))
        assert n == 0

    def test_skips_trade_with_no_chain_data(self, mock_db, mocker):
        """No chain means market closed — must skip, not fire spurious decisions."""
        mocker.patch.object(orch.TradeRepo, "open_trades", return_value=[_open_trade()])
        mocker.patch.object(orch.TradeRepo, "legs", return_value=_trade_legs())
        mock_db.fetch_all.return_value = _sug_legs()
        mock_db.fetch_one.return_value = {"strategy": "BEAR_CALL_SPREAD"}
        # Chain query → empty
        mocker.patch.object(orch.FoEodRepo, "get_chain", return_value=[])
        n = orch.run_exit_engine(mock_db, date(2026, 5, 4))
        assert n == 0  # skipped, no decisions made

    def test_holds_when_engine_returns_hold(self, mock_db, mocker):
        mocker.patch.object(orch.TradeRepo, "open_trades", return_value=[_open_trade()])
        mocker.patch.object(orch.TradeRepo, "legs", return_value=_trade_legs())
        mock_db.fetch_all.return_value = _sug_legs()
        mock_db.fetch_one.return_value = {"strategy": "BEAR_CALL_SPREAD"}
        mocker.patch.object(orch.FoEodRepo, "get_chain", return_value=_chain())
        update_status = mocker.patch.object(orch.TradeRepo, "update_status")
        # evaluate_exit returns HOLD
        mocker.patch("lifecycle.exit_orchestrator.evaluate_exit",
                     return_value=MagicMock(decision="HOLD", reason="ok"))
        n = orch.run_exit_engine(mock_db, date(2026, 5, 4))
        assert n == 1
        # status set to ACTIVE/OPEN
        call_args = update_status.call_args
        assert call_args[0][1] == "ACTIVE"
        assert call_args[0][2] == "OPEN"

    def test_skips_legs_that_were_not_filled(self, mock_db, mocker):
        """If all legs are unfilled, evaluate_exit must not be called."""
        mocker.patch.object(orch.TradeRepo, "open_trades", return_value=[_open_trade()])
        mocker.patch.object(orch.TradeRepo, "legs", return_value=_trade_legs(filled=False))
        mock_db.fetch_all.return_value = _sug_legs()
        mock_db.fetch_one.return_value = {"strategy": "BEAR_CALL_SPREAD"}
        mocker.patch.object(orch.FoEodRepo, "get_chain", return_value=_chain())
        eval_mock = mocker.patch("lifecycle.exit_orchestrator.evaluate_exit")
        n = orch.run_exit_engine(mock_db, date(2026, 5, 4))
        assert n == 0
        eval_mock.assert_not_called()
