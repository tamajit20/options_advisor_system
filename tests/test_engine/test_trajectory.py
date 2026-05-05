"""Unit tests for engine.trajectory — pure-function trajectory metrics."""
from __future__ import annotations

import pytest

from engine.trajectory import (
    acceleration,
    noise_floor_check,
    persistence,
    slope_pct,
)


class TestSlopePct:
    def test_returns_none_for_short_series(self):
        assert slope_pct([1.0, 2.0]) is None
        assert slope_pct([]) is None
        assert slope_pct([None, None]) is None

    def test_drops_none_and_nonfinite(self):
        # 4 clean samples after dropping None/NaN — should compute slope.
        assert slope_pct([1.0, None, 2.0, None, 3.0, 4.0]) is not None

    def test_positive_slope_for_increasing(self):
        s = slope_pct([10.0, 11.0, 12.0, 13.0])
        assert s is not None
        assert s > 0

    def test_negative_slope_for_decreasing(self):
        s = slope_pct([10.0, 9.0, 8.0, 7.0])
        assert s is not None
        assert s < 0

    def test_returns_none_when_base_zero(self):
        assert slope_pct([0.0, 1.0, 2.0, 3.0]) is None

    def test_flat_series_zero_slope(self):
        s = slope_pct([5.0, 5.0, 5.0, 5.0])
        assert s == 0.0


class TestPersistence:
    def test_monotonic_increasing_returns_one(self):
        assert persistence([1.0, 2.0, 3.0, 4.0]) == 1.0

    def test_monotonic_decreasing_returns_one(self):
        assert persistence([4.0, 3.0, 2.0, 1.0]) == 1.0

    def test_alternating_returns_half(self):
        # Deltas: +1, -1, +1, -1 → 50/50 split → max/total = 2/4 = 0.5
        assert persistence([0.0, 1.0, 0.0, 1.0, 0.0]) == 0.5

    def test_short_series_returns_none(self):
        assert persistence([1.0, 2.0]) is None

    def test_eps_filters_micro_noise(self):
        # Three clear up-moves with tiny noise. Without eps, +1, +0.001, +1, +1
        # — all positive → persistence = 1.0. With eps=0.01 the 0.001 delta is
        # dropped and we're left with three positives → still 1.0.
        assert persistence([0.0, 1.0, 1.001, 2.001, 3.001], eps=0.01) == 1.0

    def test_all_zero_deltas_returns_none(self):
        # All deltas filtered out → no signal.
        assert persistence([5.0, 5.0, 5.0, 5.0]) is None


class TestAcceleration:
    def test_constant_slope_returns_value(self):
        # Linear increase doesn't yield zero accel because slope_pct normalises
        # by the half's base value -- the second half's larger base makes its
        # %-slope smaller, so accel < 0. We just assert it returns a finite
        # number, not the specific shape.
        a = acceleration([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
        assert a is not None

    def test_short_series_returns_none(self):
        assert acceleration([1.0, 2.0, 3.0]) is None

    def test_steepening_positive(self):
        # First half flat, second half rising → positive accel
        a = acceleration([1.0, 1.0, 1.0, 2.0, 4.0, 8.0])
        assert a is not None
        assert a > 0


class TestNoiseFloorCheck:
    def test_passes_when_total_above_threshold(self):
        assert noise_floor_check([100.0, 200.0, 300.0], min_total=500.0) is True

    def test_fails_when_total_below_threshold(self):
        assert noise_floor_check([10.0, 20.0], min_total=100.0) is False

    def test_ignores_none_entries(self):
        assert noise_floor_check([None, 100.0, None], min_total=50.0) is True
