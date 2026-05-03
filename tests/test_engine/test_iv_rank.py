"""Unit tests for engine.iv_rank — pure scaling math."""
from __future__ import annotations

import pytest

from engine.iv_rank import iv_rank, iv_percentile, pick_atm_iv


class TestIvRank:
    def test_empty_history_returns_zero(self):
        assert iv_rank(0.20, []) == 0.0

    def test_flat_history_returns_fifty(self):
        # hi == lo edge case → safe midpoint
        assert iv_rank(0.20, [0.20, 0.20, 0.20]) == 50.0

    def test_at_low_returns_zero(self):
        assert iv_rank(0.10, [0.10, 0.20, 0.30]) == 0.0

    def test_at_high_returns_hundred(self):
        assert iv_rank(0.30, [0.10, 0.20, 0.30]) == 100.0

    def test_midpoint_returns_fifty(self):
        assert iv_rank(0.20, [0.10, 0.20, 0.30]) == pytest.approx(50.0)

    def test_below_low_clamps_to_zero(self):
        assert iv_rank(0.05, [0.10, 0.20, 0.30]) == 0.0

    def test_above_high_clamps_to_hundred(self):
        assert iv_rank(0.50, [0.10, 0.20, 0.30]) == 100.0


class TestIvPercentile:
    def test_empty_history_returns_zero(self):
        assert iv_percentile(0.20, []) == 0.0

    def test_all_below_returns_hundred(self):
        assert iv_percentile(0.30, [0.10, 0.15, 0.20]) == 100.0

    def test_all_above_returns_zero(self):
        assert iv_percentile(0.05, [0.10, 0.15, 0.20]) == 0.0

    def test_half_below_returns_fifty(self):
        assert iv_percentile(0.20, [0.10, 0.15, 0.25, 0.30]) == 50.0


class TestPickAtmIv:
    def test_empty_input_returns_none(self):
        assert pick_atm_iv([], 23000.0) is None

    def test_zero_spot_returns_none(self):
        assert pick_atm_iv([(23000.0, "CE", 0.18)], 0.0) is None

    def test_picks_strike_closest_to_spot_and_averages(self):
        data = [
            (22950.0, "CE", 0.20), (22950.0, "PE", 0.22),
            (23000.0, "CE", 0.18), (23000.0, "PE", 0.20),  # closest to spot 23010
            (23050.0, "CE", 0.19), (23050.0, "PE", 0.21),
        ]
        result = pick_atm_iv(data, 23010.0)
        assert result == pytest.approx(0.19)  # mean of 0.18, 0.20

    def test_filters_zero_iv(self):
        data = [(23000.0, "CE", 0.0), (23000.0, "PE", 0.20)]
        assert pick_atm_iv(data, 23000.0) == pytest.approx(0.20)

    def test_all_zero_iv_returns_none(self):
        data = [(23000.0, "CE", 0.0), (23000.0, "PE", 0.0)]
        assert pick_atm_iv(data, 23000.0) is None
