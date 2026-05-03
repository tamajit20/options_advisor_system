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
