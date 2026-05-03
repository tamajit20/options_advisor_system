"""Tests for lifecycle/resuggestion_engine.py."""
from __future__ import annotations

from datetime import date

import pytest

from lifecycle import resuggestion_engine as re_eng


class TestGenerateResuggestion:
    def test_raises_when_trade_unknown(self, mock_db, mocker):
        mocker.patch("lifecycle.resuggestion_engine.TradeRepo.get",
                     return_value=None)
        with pytest.raises(ValueError, match="Unknown trade"):
            re_eng.generate_resuggestion(mock_db, "TRD-X")

    def test_skips_when_already_resuggested(self, mock_db, mocker):
        mocker.patch("lifecycle.resuggestion_engine.TradeRepo.get",
                     return_value={"trade_id": "TRD-1", "suggestion_id": "SUG-1"})
        mocker.patch(
            "lifecycle.resuggestion_engine.ResuggestionRepo.for_suggestion",
            return_value={"original_suggestion_id": "SUG-1"})
        assert re_eng.generate_resuggestion(mock_db, "TRD-1") is False

    def test_returns_false_when_all_legs_executed(self, mock_db, mocker):
        mocker.patch("lifecycle.resuggestion_engine.TradeRepo.get",
                     return_value={"trade_id": "TRD-1", "suggestion_id": "SUG-1"})
        mocker.patch(
            "lifecycle.resuggestion_engine.ResuggestionRepo.for_suggestion",
            return_value=None)
        sug_legs = [{"leg_order": 1, "symbol": "NIFTY",
                     "expiry_date": date(2026, 5, 14),
                     "strike": 23000.0, "option_type": "CE", "action": "SELL",
                     "lots": 1, "lot_size": 75}]
        mocker.patch("lifecycle.resuggestion_engine.SuggestionRepo.legs",
                     return_value=sug_legs)
        mocker.patch("lifecycle.resuggestion_engine.TradeRepo.legs",
                     return_value=[{"leg_order": 1, "executed": True,
                                    "fill_price": 50.0}])
        assert re_eng.generate_resuggestion(mock_db, "TRD-1") is False
