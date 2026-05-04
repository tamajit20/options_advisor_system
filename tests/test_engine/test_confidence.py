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
        # 7 soft + event + DTE = 9 gates total
        assert result.total == 9
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
        # DTE check (last) must be FAIL
        dte_check = result.checks[-1]
        assert dte_check.label == "DTE within target band"
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
