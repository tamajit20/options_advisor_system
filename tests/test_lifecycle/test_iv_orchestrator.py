"""Tests for lifecycle/iv_orchestrator.py — IV computation skip behaviour."""
from __future__ import annotations

from datetime import date

import pytest

import lifecycle.iv_orchestrator as orch


class TestRunIvCalculation:
    def test_no_fo_data_returns_zero(self, mock_db, mocker):
        mocker.patch.object(orch.FoEodRepo, "has_trade_date", return_value=False)
        n = orch.run_iv_calculation(mock_db, date(2026, 4, 30))
        assert n == 0

    def test_skips_when_fo_missing_for_explicit_date(self, mock_db, mocker):
        mocker.patch.object(orch.FoEodRepo, "has_trade_date", return_value=False)
        mocker.patch.object(orch.SpotEodRepo, "for_date", return_value=None)
        n = orch.run_iv_calculation(mock_db, date(2026, 4, 30))
        assert n == 0

    def test_skips_symbol_without_spot(self, mock_db, mocker):
        mocker.patch.object(orch.FoEodRepo, "has_trade_date", return_value=True)
        mocker.patch.object(orch.SpotEodRepo, "for_date", return_value=None)
        n = orch.run_iv_calculation(mock_db, date(2026, 4, 30))
        assert n == 0

    def test_skips_symbol_with_zero_spot(self, mock_db, mocker):
        mocker.patch.object(orch.FoEodRepo, "has_trade_date", return_value=True)
        mocker.patch.object(orch.SpotEodRepo, "for_date",
                            return_value={"close_price": 0})
        mocker.patch.object(orch.FoEodRepo, "expiries_for", return_value=[])
        n = orch.run_iv_calculation(mock_db, date(2026, 4, 30))
        assert n == 0
