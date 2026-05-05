"""Tests for simulation/simulator.py — pure-logic helpers + orchestration."""
from __future__ import annotations

from datetime import date

import pytest

from simulation.simulator import _classify_day1, _compute_day_pnl, update_simulation


# ---------------------------------------------------------------------------
class TestClassifyDay1:
    def test_full_valid_when_open_within_band(self):
        quality, _, entry = _classify_day1(suggested=100.0, sug_low=98.0,
                                            sug_high=102.0, actual_open=100.5)
        assert quality == "FULL_VALID"
        assert entry == 100.0  # uses suggested price

    def test_full_valid_at_band_low_edge(self):
        q, _, e = _classify_day1(100.0, 98.0, 102.0, 98.0)
        assert q == "FULL_VALID" and e == 100.0

    def test_full_valid_at_band_high_edge(self):
        q, _, e = _classify_day1(100.0, 98.0, 102.0, 102.0)
        assert q == "FULL_VALID" and e == 100.0

    def test_adjusted_when_outside_band_within_max_gap(self):
        """Open is outside band but within adjusted_max_gap_pct% — use actual."""
        # 5% gap (default max is typically 10%)
        q, note, e = _classify_day1(100.0, 98.0, 102.0, 105.0)
        assert q == "ADJUSTED"
        assert e == 105.0
        assert "Gap" in note

    def test_void_when_gap_exceeds_threshold(self):
        """Open is wildly off — sim entire trade as VOID."""
        q, note, e = _classify_day1(100.0, 98.0, 102.0, 200.0)
        assert q == "VOID"
        assert e is None
        assert "exceed" in note.lower()

    def test_void_when_suggested_price_zero(self):
        q, _, e = _classify_day1(suggested=0.0, sug_low=0.0, sug_high=0.0,
                                  actual_open=10.0)
        assert q == "VOID"
        assert e is None

    def test_band_zero_falls_through_to_gap_check(self):
        """If band is missing but suggested>0, fall through to gap classification."""
        q, _, e = _classify_day1(100.0, 0.0, 0.0, 100.0)
        # With band 0, it shouldn't match the inside-band branch; goes to gap check
        # gap = 0% → ADJUSTED (still ≤ max_gap)
        assert q == "ADJUSTED"
        assert e == 100.0


# ---------------------------------------------------------------------------
class TestComputeDayPnl:
    def test_zero_when_no_legs(self):
        assert _compute_day_pnl([], []) == 0.0

    def test_short_leg_profit_when_premium_decays(self):
        """SELL @ 50, settle @ 30 → profit = (50−30) × qty."""
        legs = [{"strike": 23000.0, "option_type": "CE", "action": "SELL",
                 "lots": 1, "lot_size": 75, "sim_entry_price": 50.0}]
        chain = [{"strike": 23000.0, "option_type": "CE", "settle_price": 30.0}]
        pnl = _compute_day_pnl(legs, chain)
        assert pnl == (50.0 - 30.0) * 75

    def test_long_leg_loss_when_premium_decays(self):
        """BUY @ 50, settle @ 30 → loss = -(50−30) × qty = -1500."""
        legs = [{"strike": 23000.0, "option_type": "CE", "action": "BUY",
                 "lots": 1, "lot_size": 75, "sim_entry_price": 50.0}]
        chain = [{"strike": 23000.0, "option_type": "CE", "settle_price": 30.0}]
        pnl = _compute_day_pnl(legs, chain)
        assert pnl == -(50.0 - 30.0) * 75

    def test_skips_legs_without_sim_entry(self):
        legs = [{"strike": 23000.0, "option_type": "CE", "action": "SELL",
                 "lots": 1, "lot_size": 75, "sim_entry_price": None}]
        chain = [{"strike": 23000.0, "option_type": "CE", "settle_price": 30.0}]
        assert _compute_day_pnl(legs, chain) == 0.0

    def test_strike_not_in_chain_treated_as_zero(self):
        """Missing strike → mid=0 → SELL leg shows full premium as profit."""
        legs = [{"strike": 99999.0, "option_type": "CE", "action": "SELL",
                 "lots": 1, "lot_size": 75, "sim_entry_price": 50.0}]
        chain = [{"strike": 23000.0, "option_type": "CE", "settle_price": 30.0}]
        # Missing strike → mid 0 → profit = (50-0)*75
        pnl = _compute_day_pnl(legs, chain)
        assert pnl == 50.0 * 75

    def test_uses_close_price_when_settle_missing(self):
        legs = [{"strike": 23000.0, "option_type": "CE", "action": "SELL",
                 "lots": 1, "lot_size": 75, "sim_entry_price": 50.0}]
        chain = [{"strike": 23000.0, "option_type": "CE", "close_price": 40.0}]
        pnl = _compute_day_pnl(legs, chain)
        assert pnl == (50.0 - 40.0) * 75

    def test_multi_leg_iron_condor_zero_pnl_at_entry(self):
        """4-leg IC with all settles == sim_entry → PnL ~ 0."""
        legs = [
            {"strike": 23200, "option_type": "CE", "action": "SELL",
             "lots": 1, "lot_size": 75, "sim_entry_price": 50.0},
            {"strike": 23300, "option_type": "CE", "action": "BUY",
             "lots": 1, "lot_size": 75, "sim_entry_price": 25.0},
            {"strike": 22800, "option_type": "PE", "action": "SELL",
             "lots": 1, "lot_size": 75, "sim_entry_price": 50.0},
            {"strike": 22700, "option_type": "PE", "action": "BUY",
             "lots": 1, "lot_size": 75, "sim_entry_price": 25.0},
        ]
        chain = [
            {"strike": 23200, "option_type": "CE", "settle_price": 50.0},
            {"strike": 23300, "option_type": "CE", "settle_price": 25.0},
            {"strike": 22800, "option_type": "PE", "settle_price": 50.0},
            {"strike": 22700, "option_type": "PE", "settle_price": 25.0},
        ]
        assert _compute_day_pnl(legs, chain) == 0.0


# ---------------------------------------------------------------------------
class TestUpdateSimulation:
    def test_returns_false_when_suggestion_missing(self, mock_db, mocker):
        mocker.patch("simulation.simulator.SuggestionRepo.get", return_value=None)
        assert update_simulation(mock_db, "SUG-X", date(2026, 5, 4)) is False

    def test_returns_false_when_no_legs(self, mock_db, mocker):
        mocker.patch("simulation.simulator.SuggestionRepo.get",
                     return_value={"strategy": "IRON_CONDOR"})
        mocker.patch("simulation.simulator.SuggestionRepo.legs", return_value=[])
        assert update_simulation(mock_db, "SUG-X", date(2026, 5, 4)) is False

    def test_skips_no_suggestion_strategy(self, mock_db, mocker):
        mocker.patch("simulation.simulator.SuggestionRepo.get",
                     return_value={"strategy": "NONE"})
        mocker.patch("simulation.simulator.SuggestionRepo.legs", return_value=[
            {"leg_order": 1, "symbol": "NIFTY", "expiry_date": date(2026, 5, 14)}
        ])
        assert update_simulation(mock_db, "SUG-X", date(2026, 5, 4)) is False

    def test_skips_already_closed_or_void(self, mock_db, mocker):
        mocker.patch("simulation.simulator.SuggestionRepo.get",
                     return_value={"strategy": "IRON_CONDOR"})
        mocker.patch("simulation.simulator.SuggestionRepo.legs", return_value=[
            {"leg_order": 1, "symbol": "NIFTY", "expiry_date": date(2026, 5, 14),
             "strike": 23000.0, "option_type": "CE", "action": "SELL",
             "lots": 1, "lot_size": 75, "suggested_price": 50.0,
             "suggested_price_low": 48.0, "suggested_price_high": 52.0}
        ])
        mocker.patch("simulation.simulator.SimulationRepo.ensure_simulation_row")
        mocker.patch("simulation.simulator.SimulationRepo.get_summary",
                     return_value={"overall_quality": "VOID"})
        assert update_simulation(mock_db, "SUG-X", date(2026, 5, 4)) is False

    def test_skips_when_no_chain_data(self, mock_db, mocker):
        mocker.patch("simulation.simulator.SuggestionRepo.get",
                     return_value={"strategy": "IRON_CONDOR"})
        mocker.patch("simulation.simulator.SuggestionRepo.legs", return_value=[
            {"leg_order": 1, "symbol": "NIFTY", "expiry_date": date(2026, 5, 14),
             "strike": 23000.0, "option_type": "CE", "action": "SELL",
             "lots": 1, "lot_size": 75, "suggested_price": 50.0,
             "suggested_price_low": 48.0, "suggested_price_high": 52.0}
        ])
        mocker.patch("simulation.simulator.SimulationRepo.ensure_simulation_row")
        mocker.patch("simulation.simulator.SimulationRepo.get_summary", return_value=None)
        mocker.patch("simulation.simulator.FoEodRepo.get_chain", return_value=[])
        assert update_simulation(mock_db, "SUG-X", date(2026, 5, 4)) is False


# ---------------------------------------------------------------------------
# Future-scope placeholders
# ---------------------------------------------------------------------------
@pytest.mark.future
@pytest.mark.skip(reason="future: full multi-day simulation walk through expiry "
                  "(FUTURE_ENHANCEMENT_SCOPES.md → Simulation)")
def test_full_simulation_walk_to_expiry():
    """End-to-end: 14-day IC simulation, day-by-day P&L progression, expiry close."""
    pass


@pytest.mark.future
@pytest.mark.skip(reason="future: include charges in sim_net_pnl "
                  "(FUTURE_ENHANCEMENT_SCOPES.md → Simulation)")
def test_simulation_includes_charges_in_net_pnl():
    """Currently sim_charges is hardcoded 0.0. Should compute estimated charges
    using engine.charges.estimate_charges and subtract from gross."""
    pass


@pytest.mark.future
@pytest.mark.skip(reason="future: time-series replay simulator over 5-min chain history (FUTURE_ENHANCEMENT_SCOPES.md -> Simulation / Backtesting)")
def test_timeseries_replay_runner_reconstructs_trajectory():
    """simulation/timeseries_replay.py should reconstruct ChainTrajectory from
    options_chain_5min/options_atm_iv_5min at any past snapshot_at and feed it
    through engine.confidence.evaluate() to tabulate gate-firing frequencies."""
    pass
