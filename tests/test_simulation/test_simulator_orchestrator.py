"""Coverage for simulator's run_simulation_update orchestrator."""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest

from simulation import simulator as sim_mod


class TestRunSimulationUpdate:
    def test_returns_zero_when_no_rows(self, mock_db):
        mock_db.fetch_all.return_value = []
        assert sim_mod.run_simulation_update(mock_db, date(2026, 5, 4)) == 0

    def test_swallows_per_suggestion_errors(self, mock_db, mocker):
        mock_db.fetch_all.return_value = [{"suggestion_id": "S-1"},
                                          {"suggestion_id": "S-2"}]
        mocker.patch("simulation.simulator.update_simulation",
                     side_effect=[True, RuntimeError("bad")])
        # Returns count of successful updates; errors logged not raised
        assert sim_mod.run_simulation_update(mock_db, date(2026, 5, 4)) == 1

    def test_uses_today_when_no_date_given(self, mock_db, mocker):
        mock_db.fetch_all.return_value = []
        mocker.patch("simulation.simulator.today_ist",
                     return_value=date(2026, 5, 4))
        sim_mod.run_simulation_update(mock_db)
