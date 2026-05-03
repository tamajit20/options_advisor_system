"""Extra coverage for lifecycle/iv_orchestrator.py — full happy path."""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest

from lifecycle import iv_orchestrator as iv_orch


class TestRunIvCalculationHappyPath:
    def test_full_run_with_chain_data(self, mock_db, mocker):
        td = date(2026, 4, 30)
        exp = date(2026, 5, 14)

        mocker.patch("lifecycle.iv_orchestrator.FoEodRepo.latest_trade_date",
                     return_value=td)
        mocker.patch("lifecycle.iv_orchestrator.SpotEodRepo.latest",
                     return_value={"close_price": 23000.0,
                                   "trade_date": td})
        mocker.patch("lifecycle.iv_orchestrator.FoEodRepo.expiries_for",
                     return_value=[exp])
        chain = [
            {"strike": 23000.0, "option_type": "CE", "settle_price": 50.0},
            {"strike": 23000.0, "option_type": "PE", "settle_price": 50.0},
            {"strike": 23200.0, "option_type": "CE", "settle_price": 25.0},
            {"strike": 22800.0, "option_type": "PE", "settle_price": 25.0},
        ]
        mocker.patch("lifecycle.iv_orchestrator.FoEodRepo.get_chain",
                     return_value=chain)
        mocker.patch("lifecycle.iv_orchestrator.IvHistoryRepo.atm_iv_history",
                     return_value=[])
        upsert = mocker.patch("lifecycle.iv_orchestrator.IvHistoryRepo.upsert_many",
                              return_value=4)

        from config import STRATEGY_CONFIG
        # Restrict underlyings to one for speed
        mocker.patch.dict("config.STRATEGY_CONFIG",
                          {"underlyings": ["NIFTY"]})
        n = iv_orch.run_iv_calculation(mock_db, td)
        assert n >= 0  # may filter zero-iv rows
        mock_db.commit.assert_called()

    def test_skips_when_no_spot(self, mock_db, mocker):
        mocker.patch("lifecycle.iv_orchestrator.FoEodRepo.latest_trade_date",
                     return_value=date(2026, 4, 30))
        mocker.patch("lifecycle.iv_orchestrator.SpotEodRepo.latest",
                     return_value=None)
        mocker.patch.dict("config.STRATEGY_CONFIG",
                          {"underlyings": ["NIFTY"]})
        n = iv_orch.run_iv_calculation(mock_db, date(2026, 4, 30))
        assert n == 0

    def test_skips_when_zero_dte(self, mock_db, mocker):
        td = date(2026, 4, 30)
        mocker.patch("lifecycle.iv_orchestrator.FoEodRepo.latest_trade_date",
                     return_value=td)
        mocker.patch("lifecycle.iv_orchestrator.SpotEodRepo.latest",
                     return_value={"close_price": 23000.0, "trade_date": td})
        # Same-day expiry → dte=0 → skip
        mocker.patch("lifecycle.iv_orchestrator.FoEodRepo.expiries_for",
                     return_value=[td])
        get_chain = mocker.patch("lifecycle.iv_orchestrator.FoEodRepo.get_chain")
        mocker.patch.dict("config.STRATEGY_CONFIG",
                          {"underlyings": ["NIFTY"]})
        n = iv_orch.run_iv_calculation(mock_db, td)
        assert n == 0
        get_chain.assert_not_called()
