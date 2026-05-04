"""Tests for lifecycle/trade_executor.py — mark_executed + supplement + close."""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from contracts import TradeLegFill
from lifecycle import trade_executor as te


@pytest.fixture
def fake_suggestion():
    return {
        "trade_name": "N-IC-1",
        "max_profit": 6000.0, "max_loss": 14000.0,
        "upper_breakeven": 23300.0, "lower_breakeven": 22700.0,
        "stop_loss_level": 23250.0,
        # Required by engine.execution_validator pre-execution gate
        "status": "PENDING",
        "spot_at_generation": 23000.0,
        "validator_status": None,
        "data_as_of": None,
        "entry_date": None,
    }


@pytest.fixture
def fake_legs():
    # Strikes clear engine.execution_validator's 1.5% buffer vs spot 23000.
    return [
        {"id": 1, "leg_order": 1, "symbol": "NIFTY",
         "expiry_date": "2026-05-14",
         "strike": 23500.0, "option_type": "CE", "action": "SELL",
         "lots": 1, "lot_size": 75},
        {"id": 2, "leg_order": 2, "symbol": "NIFTY",
         "expiry_date": "2026-05-14",
         "strike": 23600.0, "option_type": "CE", "action": "BUY",
         "lots": 1, "lot_size": 75},
        {"id": 3, "leg_order": 3, "symbol": "NIFTY",
         "expiry_date": "2026-05-14",
         "strike": 22500.0, "option_type": "PE", "action": "SELL",
         "lots": 1, "lot_size": 75},
        {"id": 4, "leg_order": 4, "symbol": "NIFTY",
         "expiry_date": "2026-05-14",
         "strike": 22400.0, "option_type": "PE", "action": "BUY",
         "lots": 1, "lot_size": 75},
    ]


# ---------------------------------------------------------------------------
class TestMarkExecuted:
    def test_raises_when_suggestion_unknown(self, mock_db, mocker):
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.get",
                     return_value=None)
        with pytest.raises(ValueError, match="Unknown"):
            te.mark_executed(mock_db, "SUG-X", [])

    def test_raises_when_no_legs(self, mock_db, mocker, fake_suggestion):
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.get",
                     return_value=fake_suggestion)
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.legs",
                     return_value=[])
        with pytest.raises(ValueError, match="no legs"):
            te.mark_executed(mock_db, "SUG-X", [])

    def test_void_when_no_fills(self, mock_db, mocker, fake_suggestion, fake_legs):
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.get",
                     return_value=fake_suggestion)
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.legs",
                     return_value=fake_legs)
        update_status = mocker.patch(
            "lifecycle.trade_executor.SuggestionRepo.update_status")
        result = te.mark_executed(mock_db, "SUG-X", [])
        assert result is None
        update_status.assert_called_with("SUG-X", "IGNORED")
        mock_db.commit.assert_called()

    def test_full_valid_when_all_filled(self, mock_db, mocker, fake_suggestion, fake_legs):
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.get",
                     return_value=fake_suggestion)
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.legs",
                     return_value=fake_legs)
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.update_status")
        mocker.patch("lifecycle.trade_executor.TradeRepo.next_trade_id",
                     return_value="TRD-001")
        ins = mocker.patch("lifecycle.trade_executor.TradeRepo.insert")
        mocker.patch("lifecycle.trade_executor.TradeRepo.insert_legs")
        fills = [TradeLegFill(leg_order=i, executed=True, fill_price=50.0,
                              fill_time=datetime(2026, 5, 4, 9, 30))
                 for i in (1, 2, 3, 4)]
        tid = te.mark_executed(mock_db, "SUG-X", fills, spot_at_execution=23000.0)
        assert tid == "TRD-001"
        # The position_type should be FULL_VALID since all legs filled
        call_arg = ins.call_args[0][0]
        assert call_arg["position_type"] == "FULL_VALID"
        assert call_arg["broken_state_json"] is None

    def test_partial_records_broken_options(self, mock_db, mocker,
                                             fake_suggestion, fake_legs):
        """Only short legs filled — diagnose returns NAKED_SHORT."""
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.get",
                     return_value=fake_suggestion)
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.legs",
                     return_value=fake_legs)
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.update_status")
        mocker.patch("lifecycle.trade_executor.TradeRepo.next_trade_id",
                     return_value="TRD-002")
        ins = mocker.patch("lifecycle.trade_executor.TradeRepo.insert")
        mocker.patch("lifecycle.trade_executor.TradeRepo.insert_legs")
        # Only fills for short legs (1 + 3) — long legs not filled
        fills = [
            TradeLegFill(leg_order=1, executed=True, fill_price=60.0,
                         fill_time=datetime(2026, 5, 4, 9, 30)),
            TradeLegFill(leg_order=3, executed=True, fill_price=55.0,
                         fill_time=datetime(2026, 5, 4, 9, 30)),
        ]
        tid = te.mark_executed(mock_db, "SUG-X", fills)
        assert tid == "TRD-002"
        call_arg = ins.call_args[0][0]
        assert call_arg["position_type"] != "FULL_VALID"
        assert call_arg["broken_state_json"] is not None


# ---------------------------------------------------------------------------
class TestSupplementTrade:
    def test_raises_when_trade_unknown(self, mock_db, mocker):
        mocker.patch("lifecycle.trade_executor.TradeRepo.get", return_value=None)
        with pytest.raises(ValueError, match="Unknown trade"):
            te.supplement_trade(mock_db, "TRD-X", [])

    def test_applies_new_fills_and_recomputes(self, mock_db, mocker, fake_legs):
        mocker.patch("lifecycle.trade_executor.TradeRepo.get",
                     return_value={"trade_id": "TRD-1"})
        # First call: existing legs (executed=True for shorts only)
        # Second call: recomputed (after update)
        legs_with_state = [
            {**l, "executed": (l["leg_order"] in (1, 3)),
             "fill_price": 50.0 if l["leg_order"] in (1, 3) else None,
             "lots_actual": l["lots"] if l["leg_order"] in (1, 3) else None}
            for l in fake_legs
        ]
        # After supplementing leg 2:
        legs_after = [
            {**l, "executed": (l["leg_order"] in (1, 2, 3)),
             "fill_price": 50.0 if l["leg_order"] in (1, 2, 3) else None,
             "lots_actual": l["lots"] if l["leg_order"] in (1, 2, 3) else None}
            for l in fake_legs
        ]
        mocker.patch("lifecycle.trade_executor.TradeRepo.legs_with_suggestion_info",
                     side_effect=[legs_with_state, legs_after])
        mocker.patch("lifecycle.trade_executor.TradeRepo.update_leg_fill")
        upd = mocker.patch("lifecycle.trade_executor.TradeRepo.update_position")
        new_fills = [TradeLegFill(leg_order=2, executed=True, fill_price=30.0,
                                   fill_time=datetime(2026, 5, 4, 10, 0))]
        te.supplement_trade(mock_db, "TRD-1", new_fills)
        upd.assert_called_once()


# ---------------------------------------------------------------------------
class TestCloseTradeWithFills:
    def test_raises_when_trade_unknown(self, mock_db, mocker):
        mocker.patch("lifecycle.trade_executor.TradeRepo.get", return_value=None)
        with pytest.raises(ValueError, match="Unknown trade"):
            te.close_trade_with_fills(mock_db, "TRD-X", [])

    def test_raises_when_no_executed_legs(self, mock_db, mocker):
        mocker.patch("lifecycle.trade_executor.TradeRepo.get",
                     return_value={"trade_id": "TRD-1"})
        mocker.patch("lifecycle.trade_executor.TradeRepo.legs_with_suggestion_info",
                     return_value=[{"executed": False, "leg_order": 1}])
        with pytest.raises(ValueError, match="no executed legs"):
            te.close_trade_with_fills(mock_db, "TRD-1", [])

    def test_computes_pnl_and_closes(self, mock_db, mocker, fake_legs):
        mocker.patch("lifecycle.trade_executor.TradeRepo.get",
                     return_value={"trade_id": "TRD-1"})
        # All 4 legs executed at 50/25/50/25
        prices = {1: 50.0, 2: 25.0, 3: 50.0, 4: 25.0}
        legs_open = [
            {**l, "executed": True, "fill_price": prices[l["leg_order"]],
             "lots_actual": l["lots"]} for l in fake_legs
        ]
        mocker.patch("lifecycle.trade_executor.TradeRepo.legs_with_suggestion_info",
                     return_value=legs_open)
        upd_exit = mocker.patch("lifecycle.trade_executor.TradeRepo.update_leg_exit")
        close = mocker.patch("lifecycle.trade_executor.TradeRepo.close_trade")
        # Exit at half — SELL gain = (50−25)*75 each, BUY loss = (25−12.5)*75 each
        exits = [
            {"leg_order": 1, "exit_price": 25.0, "exit_time": datetime(2026, 5, 7, 15, 0)},
            {"leg_order": 2, "exit_price": 12.5, "exit_time": datetime(2026, 5, 7, 15, 0)},
            {"leg_order": 3, "exit_price": 25.0, "exit_time": datetime(2026, 5, 7, 15, 0)},
            {"leg_order": 4, "exit_price": 12.5, "exit_time": datetime(2026, 5, 7, 15, 0)},
        ]
        te.close_trade_with_fills(mock_db, "TRD-1", exits)
        # Each leg exit recorded
        assert upd_exit.call_count == 4
        # close_trade called with computed gross_pnl
        close.assert_called_once()
        gross = close.call_args[0][1]  # positional: trade_id, gross, charges, net
        # SELL: (50-25)*75 = 1875 × 2 = 3750
        # BUY:  -(25-12.5)*75 = -937.5 × 2 = -1875
        # gross = 3750 - 1875 = 1875
        assert gross == pytest.approx(1875.0)
