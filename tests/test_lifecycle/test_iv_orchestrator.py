"""Tests for lifecycle/iv_orchestrator.py — IV computation skip behaviour."""
from __future__ import annotations

from datetime import date

import pytest

import lifecycle.iv_orchestrator as orch


class TestRunIvCalculation:
    def test_no_fo_data_returns_zero(self, mock_db, mocker):
        mocker.patch.object(orch.FoEodRepo, "latest_trade_date", return_value=None)
        n = orch.run_iv_calculation(mock_db, date(2026, 4, 30))
        assert n == 0

    def test_falls_back_to_latest_trade_date_when_requested_unavailable(
            self, mock_db, mocker):
        # latest_trade_date returns 2026-04-29 (not the requested 2026-04-30)
        mocker.patch.object(orch.FoEodRepo, "latest_trade_date",
                            return_value=date(2026, 4, 29))
        mocker.patch.object(orch.SpotEodRepo, "latest", return_value=None)
        # No spot for any symbol → loops over all but adds nothing
        n = orch.run_iv_calculation(mock_db, date(2026, 4, 30))
        assert n == 0

    def test_skips_symbol_without_spot(self, mock_db, mocker):
        mocker.patch.object(orch.FoEodRepo, "latest_trade_date",
                            return_value=date(2026, 4, 30))
        mocker.patch.object(orch.SpotEodRepo, "latest", return_value=None)
        n = orch.run_iv_calculation(mock_db, date(2026, 4, 30))
        assert n == 0

    def test_skips_symbol_with_zero_spot(self, mock_db, mocker):
        mocker.patch.object(orch.FoEodRepo, "latest_trade_date",
                            return_value=date(2026, 4, 30))
        mocker.patch.object(orch.SpotEodRepo, "latest",
                            return_value={"close_price": 0})
        mocker.patch.object(orch.FoEodRepo, "expiries_for", return_value=[])
        n = orch.run_iv_calculation(mock_db, date(2026, 4, 30))
        assert n == 0
