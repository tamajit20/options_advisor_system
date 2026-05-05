"""Tests for engine/em_calibration.py — pure logic, no I/O."""
from __future__ import annotations

import math

import pytest

from engine.em_calibration import (
    band_dte,
    compute_calibration_warning,
    compute_realised_ratio,
)


# ---------------------------------------------------------------------------
class TestComputeRealisedRatio:
    def test_returns_ratio_for_positive_em(self):
        # |25100 - 25000| / 200 = 0.5
        assert compute_realised_ratio(25000.0, 25100.0, 200.0) == pytest.approx(0.5)

    def test_uses_absolute_value(self):
        # Move down should give the same magnitude.
        assert compute_realised_ratio(25000.0, 24900.0, 200.0) == pytest.approx(0.5)

    def test_zero_expected_move_returns_none(self):
        assert compute_realised_ratio(25000.0, 25100.0, 0.0) is None

    def test_negative_expected_move_returns_none(self):
        assert compute_realised_ratio(25000.0, 25100.0, -10.0) is None

    def test_none_expected_move_returns_none(self):
        assert compute_realised_ratio(25000.0, 25100.0, None) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
class TestBandDte:
    @pytest.mark.parametrize("dte,band", [
        (0, "0-7"),
        (1, "0-7"),
        (7, "0-7"),
        (8, "8-21"),
        (14, "8-21"),
        (21, "8-21"),
        (22, "22+"),
        (60, "22+"),
    ])
    def test_boundaries(self, dte, band):
        assert band_dte(dte) == band

    def test_none_returns_unknown(self):
        assert band_dte(None) == "unknown"  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
class TestComputeCalibrationWarning:
    def _kw(self, **over):
        kw = dict(underlying="NIFTY", dte=14, min_samples=4, deviation_threshold=0.25)
        kw.update(over)
        return kw

    def test_empty_sample_returns_none(self):
        assert compute_calibration_warning([], **self._kw()) is None

    def test_below_min_samples_returns_none(self):
        # 3 samples that would otherwise warn — but min_samples=4 suppresses.
        assert compute_calibration_warning([1.5, 1.6, 1.7], **self._kw()) is None

    def test_calm_cohort_returns_none(self):
        # Median 1.0 ± noise, well within the 0.25 tolerance.
        ratios = [0.95, 1.02, 1.07, 0.98, 1.04, 1.01]
        assert compute_calibration_warning(ratios, **self._kw()) is None

    def test_overshoot_emits_warning(self):
        ratios = [1.55, 1.42, 1.48, 1.61, 1.50, 1.45]
        msg = compute_calibration_warning(ratios, **self._kw())
        assert msg is not None
        assert "over" in msg.lower()
        assert "NIFTY" in msg
        assert "8-21" in msg  # dte=14 → "8-21"

    def test_undershoot_emits_warning(self):
        ratios = [0.50, 0.55, 0.60, 0.45, 0.52]
        msg = compute_calibration_warning(ratios, **self._kw())
        assert msg is not None
        assert "under" in msg.lower()

    def test_non_finite_samples_skipped(self):
        # NaN/inf/None entries are dropped; remaining cohort is too small.
        ratios = [float("nan"), float("inf"), None, 1.5]
        assert compute_calibration_warning(ratios, **self._kw()) is None

    def test_negative_samples_dropped(self):
        # Realised distance can't be negative; treat as malformed and skip.
        ratios = [-0.5, -1.0, -0.3, -0.8]
        assert compute_calibration_warning(ratios, **self._kw()) is None

    def test_just_below_threshold_does_not_trigger(self):
        # Median 1.24 → deviation 0.24 < 0.25 threshold → suppressed.
        ratios = [1.24, 1.24, 1.24, 1.24]
        assert compute_calibration_warning(ratios, **self._kw()) is None

    def test_message_includes_sample_count(self):
        ratios = [1.50, 1.55, 1.45, 1.60]
        msg = compute_calibration_warning(ratios, **self._kw())
        assert msg is not None
        assert "4 expiries" in msg

    def test_band_changes_with_dte(self):
        ratios = [1.50, 1.55, 1.45, 1.60]
        short = compute_calibration_warning(ratios, **self._kw(dte=3))
        long = compute_calibration_warning(ratios, **self._kw(dte=30))
        assert "0-7" in short
        assert "22+" in long
