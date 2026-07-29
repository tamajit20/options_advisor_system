"""Tests for sideways regime pair (range vs breakout) helpers."""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import Mock

import pytest

from contracts import ChargeBreakdown, ConfidenceResult, Suggestion, SuggestionEconomics
from engine.regime_pair import (
    apply_regime_pair_metadata,
    decode_regime_pair_trigger_reason,
    encode_regime_pair_trigger_reason,
    pick_regime_pair_preferred,
    resolve_regime_pair_strategies,
)


def _sug(strategy: str, pop: float, edge: float = 50.0) -> Suggestion:
    econ = SuggestionEconomics(
        net_credit=100.0,
        max_profit=100.0,
        max_loss=500.0,
        upper_breakeven=25000.0,
        lower_breakeven=24000.0,
        stop_loss_level=None,
        probability_of_profit=pop,
        estimated_charges=ChargeBreakdown(0, 0, 0, 0, 0, 0, 0),
        estimated_net_pnl=80.0,
        edge_score=edge,
    )
    conf = ConfidenceResult(score=7, total=7, all_passed=True, checks=[])
    return Suggestion(
        suggestion_id="SUG-TEST",
        trade_name="TEST",
        generated_on=datetime.now(),
        strategy=strategy,
        strategy_type="WRITING",
        underlying="NIFTY",
        expiry_date=date.today(),
        expiry_type="Weekly",
        dte=10,
        spot_at_generation=24500.0,
        confidence=conf,
        legs=[],
        economics=econ,
        execution_window="test",
        plain_english="test",
    )


def test_resolve_high_iv_only_range():
    r, b = resolve_regime_pair_strategies(iv_rank=60.0)
    assert r == "IRON_CONDOR"
    assert b is None


def test_resolve_low_iv_both_legs():
    r, b = resolve_regime_pair_strategies(iv_rank=20.0)
    assert r == "CALENDAR_SPREAD"
    assert b == "LONG_STRADDLE"


def test_pick_preferred_range_on_higher_pop():
    range_sug = _sug("CALENDAR_SPREAD", pop=55.0, edge=40.0)
    breakout_sug = _sug("LONG_STRADDLE", pop=35.0, edge=45.0)
    preferred, reason = pick_regime_pair_preferred(range_sug, breakout_sug, iv_rank=22.0)
    assert preferred == "range"
    assert "prefers the range trade" in reason


def test_apply_metadata_marks_preferred():
    range_sug = _sug("IRON_CONDOR", pop=62.0)
    breakout_sug = _sug("LONG_STRADDLE", pop=38.0)
    items = [(range_sug, "range"), (breakout_sug, "breakout")]
    apply_regime_pair_metadata(items, group_id="NIFTY:Weekly:2026-07-30", iv_rank=55.0)
    assert range_sug.regime_pair_preferred is True
    assert breakout_sug.regime_pair_preferred is False
    assert range_sug.regime_pair_group == "NIFTY:Weekly:2026-07-30"


def test_trigger_reason_roundtrip():
    sug = _sug("IRON_CONDOR", pop=60.0)
    sug.regime_pair_group = "G1"
    sug.regime_pair_type = "range"
    sug.regime_pair_preferred = True
    sug.regime_pair_preference_reason = "test reason"
    raw = encode_regime_pair_trigger_reason(sug)
    assert raw
    decoded = decode_regime_pair_trigger_reason(raw)
    assert decoded["regime_pair_group"] == "G1"
    assert decoded["regime_pair_preferred"] is True
