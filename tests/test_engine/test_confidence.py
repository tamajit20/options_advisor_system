"""Unit tests for engine.confidence — 7-soft-gate + DTE hard-gate evaluator."""
from __future__ import annotations

from datetime import date

import pytest

from engine.confidence import evaluate


class TestEvaluate:
    def test_all_gates_pass(self, sample_indicators):
        # IV rank 60 → writing zone; default indicators are stable/neutral
        # Use an event-calendar row count > 0 to avoid PASS_WARN on event gate
        result = evaluate(
            iv_rank=60.0,
            indicators=sample_indicators,
            dte=14,
            has_high_impact_event_this_week=False,
            events_calendar_row_count=10,
        )
        assert result.all_passed is True
        # 7 soft + event + DTE + 3 trajectory gates = 12 total.
        # Trajectory gates are PASS_WARN when indicator fields are None
        # (default sample_indicators has no live trajectory).
        assert result.total == 12
        assert result.score >= 8

    def test_dte_below_band_hard_fails(self, sample_indicators):
        result = evaluate(
            iv_rank=60.0,
            indicators=sample_indicators,
            dte=3,  # below 7
            has_high_impact_event_this_week=False,
            events_calendar_row_count=10,
        )
        assert result.all_passed is False
        # DTE check is now in the middle (followed by trajectory gates).
        dte_check = next(c for c in result.checks if c.label == "DTE within target band")
        assert dte_check.status == "FAIL"

    def test_dte_above_band_hard_fails(self, sample_indicators):
        result = evaluate(
            iv_rank=60.0,
            indicators=sample_indicators,
            dte=30,
            has_high_impact_event_this_week=False,
            events_calendar_row_count=10,
        )
        assert result.all_passed is False

    def test_iv_rank_in_dead_zone_soft_fails(self, sample_indicators):
        result = evaluate(
            iv_rank=40.0,  # neither >50 nor <30
            indicators=sample_indicators,
            dte=14,
            has_high_impact_event_this_week=False,
            events_calendar_row_count=10,
        )
        iv_check = next(c for c in result.checks if c.label == "IV Rank in actionable zone")
        assert iv_check.status == "SOFT_FAIL"

    def test_iv_rank_none_yields_pass_warn(self, sample_indicators):
        result = evaluate(
            iv_rank=None,
            indicators=sample_indicators,
            dte=14,
            has_high_impact_event_this_week=False,
            events_calendar_row_count=10,
        )
        iv_check = next(c for c in result.checks if c.label == "IV Rank in actionable zone")
        assert iv_check.status == "PASS_WARN"
        assert iv_check.passed is True

    def test_event_gate_pass_warn_when_calendar_empty(self, sample_indicators):
        result = evaluate(
            iv_rank=60.0,
            indicators=sample_indicators,
            dte=14,
            has_high_impact_event_this_week=False,
            events_calendar_row_count=0,  # not seeded
        )
        ev = next(c for c in result.checks if "event" in c.label.lower())
        assert ev.status == "PASS_WARN"

    def test_high_impact_event_yields_soft_fail(self, sample_indicators):
        result = evaluate(
            iv_rank=60.0,
            indicators=sample_indicators,
            dte=14,
            has_high_impact_event_this_week=True,
            high_impact_event_description="RBI policy",
            events_calendar_row_count=10,
        )
        ev = next(c for c in result.checks if "event" in c.label.lower())
        assert ev.status == "SOFT_FAIL"
        assert "RBI" in ev.detail

    def test_too_many_soft_failures_blocks_all_passed(self, sample_indicators):
        # Force multiple soft fails: bad IV + bad PCR + bad VIX
        bad = sample_indicators
        bad.pcr = 2.5  # outside neutral band
        bad.vix_regime = "SPIKING"
        bad.oi_walls_call = []  # zero walls
        bad.oi_walls_put = []
        result = evaluate(
            iv_rank=40.0,
            indicators=bad,
            dte=14,
            has_high_impact_event_this_week=False,
            events_calendar_row_count=10,
        )
        # 4 soft fails > (7 - 5) = 2 allowed → all_passed must be False
        assert result.all_passed is False
        assert len(result.failed_reasons) >= 4


# ---------------------------------------------------------------------------
# IV/HV tiered gate (FIX #2 — regime-wide gate is permissive; per-strategy
# stricter caps live in engine/strategy_selector.py)
# ---------------------------------------------------------------------------
class TestIvHvTieredGate:
    """Buying regime tiers (regime-wide, applies to ALL strategies):
        ≤ 1.00 → PASS       (real edge — IV at-or-below realised vol)
        ≤ 1.20 → PASS_WARN  (neutral)
        ≤ 1.50 → PASS_WARN  (elevated, but not blocked here)
        >  1.50 → SOFT_FAIL (overpaying badly)
    Per-strategy stricter caps (e.g. naked longs at 1.20) are enforced in
    strategy_selector via STRATEGY_CONFIG['strategy_iv_premium_buy_max'].
    """

    def _iv_check(self, result):
        for c in result.checks:
            if "IV premium" in c.label:
                return c
        raise AssertionError("IV premium gate missing from result")

    def test_buying_regime_iv_below_realised_passes(self, sample_indicators):
        sample_indicators.iv_premium = 0.85
        result = evaluate(
            iv_rank=20.0,  # buying zone
            indicators=sample_indicators, dte=14,
            has_high_impact_event_this_week=False,
            events_calendar_row_count=10,
        )
        check = self._iv_check(result)
        assert check.status == "PASS"
        assert "real buying edge" in check.detail

    def test_buying_regime_neutral_zone_warns(self, sample_indicators):
        sample_indicators.iv_premium = 1.10
        result = evaluate(
            iv_rank=20.0,
            indicators=sample_indicators, dte=14,
            has_high_impact_event_this_week=False,
            events_calendar_row_count=10,
        )
        check = self._iv_check(result)
        assert check.status == "PASS_WARN"
        assert "no buying edge" in check.detail

    def test_buying_regime_elevated_warns_but_not_blocks(self, sample_indicators):
        # 1.35 sits in the new (warn, max] band — PASS_WARN, not SOFT_FAIL.
        # This preserves yesterday's behaviour for spreads/credit strategies.
        sample_indicators.iv_premium = 1.35
        result = evaluate(
            iv_rank=20.0,
            indicators=sample_indicators, dte=14,
            has_high_impact_event_this_week=False,
            events_calendar_row_count=10,
        )
        check = self._iv_check(result)
        assert check.status == "PASS_WARN"
        assert "elevated" in check.detail

    def test_buying_regime_overpaying_badly_soft_fails(self, sample_indicators):
        sample_indicators.iv_premium = 1.65  # > 1.50 ceiling
        result = evaluate(
            iv_rank=20.0,
            indicators=sample_indicators, dte=14,
            has_high_impact_event_this_week=False,
            events_calendar_row_count=10,
        )
        check = self._iv_check(result)
        assert check.status == "SOFT_FAIL"
        assert "overpaying" in check.detail

    def test_writing_regime_unchanged(self, sample_indicators):
        sample_indicators.iv_premium = 1.20  # well above 0.90 floor
        result = evaluate(
            iv_rank=60.0,  # writing zone
            indicators=sample_indicators, dte=14,
            has_high_impact_event_this_week=False,
            events_calendar_row_count=10,
        )
        check = self._iv_check(result)
        assert check.status == "PASS"

    def test_writing_regime_below_floor_soft_fails(self, sample_indicators):
        sample_indicators.iv_premium = 0.80
        result = evaluate(
            iv_rank=60.0,
            indicators=sample_indicators, dte=14,
            has_high_impact_event_this_week=False,
            events_calendar_row_count=10,
        )
        check = self._iv_check(result)
        assert check.status == "SOFT_FAIL"


class TestTrajectoryGates:
    """New trajectory-driven gates: IV traj (SOFT_FAIL), OI momentum (SOFT_FAIL),
    spread quality (hard FAIL). All emit PASS_WARN when fields are None."""

    def _find(self, result, label):
        return next(c for c in result.checks if c.label == label)

    def test_all_traj_gates_pass_warn_when_no_data(self, sample_indicators):
        # Default sample_indicators has no trajectory fields populated -> all PASS_WARN.
        result = evaluate(
            iv_rank=60.0,
            indicators=sample_indicators,
            dte=14,
            has_high_impact_event_this_week=False,
            events_calendar_row_count=10,
        )
        assert self._find(result, "ATM IV trajectory benign").status == "PASS_WARN"
        assert self._find(result, "OI PCR momentum neutral").status == "PASS_WARN"
        assert self._find(result, "ATM strikes liquid (spread within budget)").status == "PASS_WARN"
        # Suggestion should still be all-pass (PASS_WARN doesn't fail anything).
        assert result.all_passed is True

    def test_iv_traj_soft_fails_on_sustained_rise(self, sample_indicators):
        from dataclasses import replace
        ind = replace(sample_indicators, atm_iv_slope_5min=1.5, atm_iv_persistence=0.85)
        result = evaluate(
            iv_rank=60.0, indicators=ind, dte=14,
            has_high_impact_event_this_week=False, events_calendar_row_count=10,
        )
        assert self._find(result, "ATM IV trajectory benign").status == "SOFT_FAIL"
        # SOFT_FAIL on a non-counted gate -> still all_passed True.
        assert result.all_passed is True

    def test_iv_traj_pass_when_persistence_low(self, sample_indicators):
        from dataclasses import replace
        ind = replace(sample_indicators, atm_iv_slope_5min=1.5, atm_iv_persistence=0.4)
        result = evaluate(
            iv_rank=60.0, indicators=ind, dte=14,
            has_high_impact_event_this_week=False, events_calendar_row_count=10,
        )
        assert self._find(result, "ATM IV trajectory benign").status == "PASS"

    def test_oi_momentum_soft_fails_on_sustained_drift(self, sample_indicators):
        from dataclasses import replace
        ind = replace(sample_indicators, oi_pcr_slope_5min=-2.5, oi_pcr_persistence=0.8)
        result = evaluate(
            iv_rank=60.0, indicators=ind, dte=14,
            has_high_impact_event_this_week=False, events_calendar_row_count=10,
        )
        assert self._find(result, "OI PCR momentum neutral").status == "SOFT_FAIL"
        assert result.all_passed is True

    def test_spread_quality_hard_fails_when_too_wide(self, sample_indicators):
        from dataclasses import replace
        ind = replace(sample_indicators, atm_call_spread_bps=40.0, atm_put_spread_bps=40.0)
        result = evaluate(
            iv_rank=60.0, indicators=ind, dte=14,
            has_high_impact_event_this_week=False, events_calendar_row_count=10,
        )
        check = self._find(result, "ATM strikes liquid (spread within budget)")
        assert check.status == "FAIL"
        # Hard fail -> all_passed False.
        assert result.all_passed is False

    def test_spread_quality_pass_when_within_budget(self, sample_indicators):
        from dataclasses import replace
        ind = replace(sample_indicators, atm_call_spread_bps=15.0, atm_put_spread_bps=20.0)
        result = evaluate(
            iv_rank=60.0, indicators=ind, dte=14,
            has_high_impact_event_this_week=False, events_calendar_row_count=10,
        )
        assert self._find(result, "ATM strikes liquid (spread within budget)").status == "PASS"
        assert result.all_passed is True


@pytest.mark.future
@pytest.mark.skip(reason="future: promote IV trajectory gate SOFT_FAIL -> FAIL after 2-3 weeks accuracy review (FUTURE_ENHANCEMENT_SCOPES.md -> Risk & Monitoring)")
def test_iv_trajectory_gate_hardens_to_fail():
    """After review window, sustained rising IV (slope>0.5%/5min, persist>=0.7) should
    HARD-FAIL the suggestion (all_passed False, hard_failed >=1)."""
    pass


@pytest.mark.future
@pytest.mark.skip(reason="future: promote OI PCR momentum gate SOFT_FAIL -> FAIL after accuracy review (FUTURE_ENHANCEMENT_SCOPES.md -> Risk & Monitoring)")
def test_oi_momentum_gate_hardens_to_fail():
    """After review window, sustained directional OI PCR drift should HARD-FAIL."""
    pass
