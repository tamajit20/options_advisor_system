"""Tests for sideways regime pair (range vs breakout) helpers."""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import Mock

import pytest

from contracts import ChargeBreakdown, ConfidenceResult, NoSuggestion, Suggestion, SuggestionEconomics
from engine.regime_pair import (
    apply_regime_pair_metadata,
    complete_regime_pair,
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


def test_apply_metadata_does_not_tag_loner():
    """A single survivor must not become a lonely 'pick the scenario' card."""
    range_sug = _sug("CALENDAR_SPREAD", pop=55.0)
    apply_regime_pair_metadata(
        [(range_sug, "range")],
        group_id="BANKNIFTY:Weekly:2026-08-19",
        iv_rank=22.0,
    )
    assert range_sug.regime_pair_group is None
    assert range_sug.regime_pair_type is None
    assert range_sug.regime_pair_preferred is False


def test_complete_pair_both_pass_tags_preferred():
    range_sug = _sug("CALENDAR_SPREAD", pop=55.0, edge=40.0)
    breakout_sug = _sug("LONG_STRADDLE", pop=35.0, edge=45.0)
    conf = range_sug.confidence
    sugs, ns = complete_regime_pair(
        [(range_sug, "range"), (breakout_sug, "breakout")],
        missing_reasons={},
        group_id="NIFTY:Weekly:2026-08-19",
        iv_rank=22.0,
        underlying="NIFTY",
        confidence=conf,
        generated_on=datetime.now(),
    )
    assert len(sugs) == 2
    assert ns == []
    assert {s.regime_pair_type for s in sugs} == {"range", "breakout"}
    assert sum(1 for s in sugs if s.regime_pair_preferred) == 1
    assert all(s.regime_pair_group == "NIFTY:Weekly:2026-08-19" for s in sugs)
    preferred = next(s for s in sugs if s.regime_pair_preferred)
    assert preferred.regime_pair_type == "range"


def test_complete_pair_breakout_vetoed_still_two_rows():
    range_sug = _sug("CALENDAR_SPREAD", pop=55.0)
    sugs, ns = complete_regime_pair(
        [(range_sug, "range")],
        missing_reasons={"breakout": "LONG_STRADDLE veto: IV/HV exceeds long-vol ceiling"},
        group_id="BANKNIFTY:Weekly:2026-08-19",
        iv_rank=22.0,
        underlying="BANKNIFTY",
        confidence=range_sug.confidence,
        generated_on=datetime.now(),
    )
    assert len(sugs) == 1
    assert len(ns) == 1
    assert sugs[0].regime_pair_group == ns[0].regime_pair_group == "BANKNIFTY:Weekly:2026-08-19"
    assert sugs[0].regime_pair_type == "range"
    assert ns[0].regime_pair_type == "breakout"
    assert sugs[0].regime_pair_preferred is True
    assert ns[0].regime_pair_preferred is False
    assert "blocked" in ns[0].reason.lower()
    assert "long-vol" in ns[0].reason.lower()


def test_complete_pair_vetoed_breakout_with_legs_prefers_range():
    """Entry-gate veto with constructed legs stays a Suggestion, Range preferred."""
    range_sug = _sug("CALENDAR_SPREAD", pop=55.0, edge=40.0)
    breakout_sug = _sug("LONG_STRADDLE", pop=42.0, edge=60.0)
    breakout_sug.strategy_veto_reason = (
        "LONG_STRADDLE vetoed: IV rank 5 below 15 with no HIGH-impact catalyst"
    )
    sugs, ns = complete_regime_pair(
        [(range_sug, "range"), (breakout_sug, "breakout")],
        missing_reasons={},
        group_id="BANKNIFTY:Weekly:2026-08-19",
        iv_rank=5.0,
        underlying="BANKNIFTY",
        confidence=range_sug.confidence,
        generated_on=datetime.now(),
    )
    assert ns == []
    assert len(sugs) == 2
    by_type = {s.regime_pair_type: s for s in sugs}
    assert by_type["range"].regime_pair_preferred is True
    assert by_type["breakout"].regime_pair_preferred is False
    assert by_type["breakout"].strategy_veto_reason
    assert "IV rank" in (by_type["range"].regime_pair_preference_reason or "")
    raw = encode_regime_pair_trigger_reason(by_type["breakout"])
    decoded = decode_regime_pair_trigger_reason(raw)
    assert decoded["strategy_veto"]
    assert decoded["regime_pair_type"] == "breakout"


def test_complete_pair_both_fail_two_no_suggestions():
    conf = ConfidenceResult(score=7, total=7, all_passed=True, checks=[])
    sugs, ns = complete_regime_pair(
        [],
        missing_reasons={
            "range": "CALENDAR_SPREAD veto: empty chain",
            "breakout": "LONG_STRADDLE veto: max loss cap",
        },
        group_id="NIFTY:Weekly:2026-08-19",
        iv_rank=22.0,
        underlying="NIFTY",
        confidence=conf,
        generated_on=datetime.now(),
    )
    assert sugs == []
    assert len(ns) == 2
    assert {n.regime_pair_type for n in ns} == {"range", "breakout"}
    assert all(n.regime_pair_group == "NIFTY:Weekly:2026-08-19" for n in ns)
    assert all(n.regime_pair_preferred is False for n in ns)


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


def test_trigger_reason_roundtrip_no_suggestion():
    conf = ConfidenceResult(score=7, total=7, all_passed=True, checks=[])
    ns = NoSuggestion(
        generated_on=datetime.now(),
        underlying="BANKNIFTY",
        confidence=conf,
        reason="breakout blocked",
        regime_pair_group="G1",
        regime_pair_type="breakout",
        regime_pair_preferred=False,
    )
    raw = encode_regime_pair_trigger_reason(ns)
    decoded = decode_regime_pair_trigger_reason(raw)
    assert decoded["regime_pair_group"] == "G1"
    assert decoded["regime_pair_type"] == "breakout"
    assert decoded["regime_pair_preferred"] is False
