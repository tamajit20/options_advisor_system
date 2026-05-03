"""Tests for simulation/backtest_runner.py — pure helpers + run wrapper."""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from simulation import backtest_runner as br


# ---------------------------------------------------------------------------
class TestBuildChainLookup:
    def test_settle_preferred_over_close(self):
        rows = [
            {"strike": 23000.0, "option_type": "CE",
             "settle_price": 50.0, "close_price": 49.0},
            {"strike": 22800.0, "option_type": "PE",
             "settle_price": None, "close_price": 30.0},
        ]
        out = br._build_chain_lookup(rows)
        assert out[(23000.0, "CE")] == 50.0
        assert out[(22800.0, "PE")] == 30.0

    def test_zero_when_both_missing(self):
        rows = [{"strike": 23000.0, "option_type": "CE"}]
        out = br._build_chain_lookup(rows)
        assert out[(23000.0, "CE")] == 0.0


# ---------------------------------------------------------------------------
class TestLegsForEngine:
    def test_converts_with_suggested_price(self):
        legs = [{"action": "SELL", "strike": "23000",
                 "option_type": "CE", "lots": 2, "lot_size": 75,
                 "suggested_price": "50.5"}]
        out = br._legs_for_engine(legs)
        assert out[0]["action"] == "SELL"
        assert out[0]["fill_price"] == 50.5
        assert out[0]["lots"] == 2

    def test_handles_missing_lots_and_price(self):
        legs = [{"action": "BUY", "strike": 22000, "option_type": "PE"}]
        out = br._legs_for_engine(legs)
        assert out[0]["lots"] == 1
        assert out[0]["lot_size"] == 1
        assert out[0]["fill_price"] == 0.0


# ---------------------------------------------------------------------------
class TestNetCreditAtEntry:
    def test_short_leg_positive_credit(self):
        legs = [{"action": "SELL", "fill_price": 50.0,
                 "lots": 1, "lot_size": 75}]
        assert br._net_credit_at_entry(legs) == 3750.0

    def test_long_leg_negative_credit(self):
        legs = [{"action": "BUY", "fill_price": 50.0,
                 "lots": 1, "lot_size": 75}]
        assert br._net_credit_at_entry(legs) == -3750.0

    def test_iron_condor_net_credit(self):
        legs = [
            {"action": "SELL", "fill_price": 50.0, "lots": 1, "lot_size": 75},
            {"action": "BUY",  "fill_price": 25.0, "lots": 1, "lot_size": 75},
            {"action": "SELL", "fill_price": 50.0, "lots": 1, "lot_size": 75},
            {"action": "BUY",  "fill_price": 25.0, "lots": 1, "lot_size": 75},
        ]
        # 50-25 + 50-25 = 50 per share, * 75 = 3750
        assert br._net_credit_at_entry(legs) == 3750.0


# ---------------------------------------------------------------------------
class TestAggregate:
    def test_empty_results(self):
        assert br._aggregate([]) == {}

    def test_single_strategy_summary(self):
        rows = [
            {"strategy": "IRON_CONDOR", "exit_pnl": 1000.0,
             "days_held": 5, "exit_decision": "TARGET_HIT"},
            {"strategy": "IRON_CONDOR", "exit_pnl": -500.0,
             "days_held": 3, "exit_decision": "STOP_LOSS"},
            {"strategy": "IRON_CONDOR", "exit_pnl": 200.0,
             "days_held": 7, "exit_decision": "TARGET_HIT"},
        ]
        out = br._aggregate(rows)
        ic = out["IRON_CONDOR"]
        assert ic["trades"] == 3
        assert ic["win_rate"] == pytest.approx(2 / 3)
        assert ic["total_pnl"] == 700.0
        assert ic["best"] == 1000.0
        assert ic["worst"] == -500.0
        assert ic["exits"]["TARGET_HIT"] == 2
        assert ic["exits"]["STOP_LOSS"] == 1


# ---------------------------------------------------------------------------
class TestSimulateSuggestion:
    def test_returns_none_for_strategy_none(self, mock_db):
        sug = {"strategy": "NONE", "suggestion_id": "S-1"}
        assert br._simulate_suggestion(mock_db, sug, [{"symbol": "NIFTY",
                                                       "expiry_date": date(2026, 5, 14)}]) is None

    def test_returns_none_when_generated_on_missing(self, mock_db):
        sug = {"strategy": "IRON_CONDOR", "suggestion_id": "S-1",
               "generated_on": None}
        legs = [{"symbol": "NIFTY", "expiry_date": date(2026, 5, 14),
                 "action": "SELL", "strike": 23200.0, "option_type": "CE",
                 "lots": 1, "lot_size": 75, "suggested_price": 50.0}]
        assert br._simulate_suggestion(mock_db, sug, legs) is None


# ---------------------------------------------------------------------------
class TestRunBacktest:
    def test_empty_when_no_suggestions(self, mocker, tmp_path):
        fake_db = MagicMock()
        fake_db.fetch_all = MagicMock(return_value=[])
        fake_db.close = MagicMock()
        mocker.patch("simulation.backtest_runner.SQLServerConnection",
                     return_value=fake_db)
        result = br.run_backtest(date(2026, 1, 1), date(2026, 4, 30), tmp_path)
        assert result == {}


class TestPrintSummary:
    def test_no_results(self, capsys):
        br._print_summary({})
        out = capsys.readouterr().out
        assert "No results" in out

    def test_with_results(self, capsys):
        br._print_summary({
            "IRON_CONDOR": {
                "trades": 3, "win_rate": 0.66, "avg_pnl": 233.0,
                "total_pnl": 700.0, "best": 1000, "worst": -500,
                "avg_days": 5.0, "exits": {"TARGET_HIT": 2, "STOP_LOSS": 1},
            }
        })
        out = capsys.readouterr().out
        assert "IRON_CONDOR" in out
        assert "TARGET_HIT" in out


class TestMainCli:
    def test_main_with_args(self, mocker, tmp_path):
        mocker.patch("simulation.backtest_runner.run_backtest", return_value={})
        rc = br.main(["--start", "2026-01-01", "--end", "2026-04-30",
                      "--output-dir", str(tmp_path)])
        assert rc == 0
