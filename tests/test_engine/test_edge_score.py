"""Unit tests for engine.edge_score — composite trade-quality score (issue #10).

The score is display + ranking only and never gates a suggestion. Tests verify
that:
    1. grade_credit_to_width returns the right tier label.
    2. compute() emits a 0-100 score whose components sum equals the score.
    3. CREDIT strategies score better with high credit-to-width ratio.
    4. DEFINED_DEBIT strategies score better with cheaper debit relative to width.
    5. Buying strategies prefer low IV/HV (per-strategy threshold).
    6. Confidence headroom (extra soft passes) lifts the confidence component.
"""
from __future__ import annotations

import pytest

from contracts import ConfidenceCheck, ConfidenceResult
from engine import edge_score


def _conf(passes: int, total: int = 7) -> ConfidenceResult:
    """Build a fake ConfidenceResult with `passes` PASS and (total-passes) FAIL."""
    checks = [
        ConfidenceCheck(label=f"g{i}", status="PASS" if i < passes else "FAIL", detail="")
        for i in range(total)
    ]
    return ConfidenceResult(
        score=passes, total=total, all_passed=passes == total, checks=checks,
    )


# ──────────────────────────────────────────────────────────────────────────
# grade_credit_to_width
# ──────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestGradeCreditToWidth:
    def test_strong_at_or_above_threshold(self):
        assert edge_score.grade_credit_to_width(0.30) == "strong"
        assert edge_score.grade_credit_to_width(0.50) == "strong"

    def test_good_in_middle_band(self):
        assert edge_score.grade_credit_to_width(0.25) == "good"
        assert edge_score.grade_credit_to_width(0.29) == "good"

    def test_weak_below_good_threshold(self):
        assert edge_score.grade_credit_to_width(0.10) == "weak"
        assert edge_score.grade_credit_to_width(0.24) == "weak"


# ──────────────────────────────────────────────────────────────────────────
# compute() — overall behaviour
# ──────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestComputeScore:
    def test_components_sum_equals_score(self):
        score, comps = edge_score.compute(
            strategy="IRON_CONDOR", pop=70.0, legs=[],
            net_premium_per_share=60.0, spread_width=200.0,
            iv_rank=70.0, iv_premium=1.05, confidence=_conf(6),
        )
        assert 0.0 <= score <= 100.0
        assert round(sum(comps.values()), 1) == score

    def test_score_in_zero_to_hundred_range(self):
        # Worst-case inputs
        score, _ = edge_score.compute(
            strategy="IRON_CONDOR", pop=0.0, legs=[],
            net_premium_per_share=0.0, spread_width=200.0,
            iv_rank=10.0, iv_premium=0.5, confidence=_conf(0),
        )
        assert 0.0 <= score <= 100.0

        # Best-case inputs
        score, _ = edge_score.compute(
            strategy="IRON_CONDOR", pop=100.0, legs=[],
            net_premium_per_share=80.0, spread_width=200.0,
            iv_rank=80.0, iv_premium=1.20, confidence=_conf(7),
        )
        assert 0.0 <= score <= 100.0

    def test_credit_strategy_higher_credit_to_width_scores_higher(self):
        weak, _ = edge_score.compute(
            strategy="IRON_CONDOR", pop=70.0, legs=[],
            net_premium_per_share=20.0, spread_width=200.0,  # 10% — weak
            iv_rank=70.0, iv_premium=1.05, confidence=_conf(6),
        )
        strong, _ = edge_score.compute(
            strategy="IRON_CONDOR", pop=70.0, legs=[],
            net_premium_per_share=70.0, spread_width=200.0,  # 35% — strong
            iv_rank=70.0, iv_premium=1.05, confidence=_conf(6),
        )
        assert strong > weak

    def test_defined_debit_smaller_debit_scores_higher(self):
        # Bull call spread: net premium negative (debit)
        expensive, _ = edge_score.compute(
            strategy="BULL_CALL_SPREAD", pop=55.0, legs=[],
            net_premium_per_share=-140.0, spread_width=200.0,  # 70% debit
            iv_rank=25.0, iv_premium=0.85, confidence=_conf(6),
        )
        cheap, _ = edge_score.compute(
            strategy="BULL_CALL_SPREAD", pop=55.0, legs=[],
            net_premium_per_share=-60.0, spread_width=200.0,  # 30% debit
            iv_rank=25.0, iv_premium=0.85, confidence=_conf(6),
        )
        assert cheap > expensive

    def test_buying_strategy_prefers_low_iv_premium(self):
        cheap_iv, _ = edge_score.compute(
            strategy="LONG_STRADDLE", pop=55.0, legs=[],
            net_premium_per_share=-150.0, spread_width=0.0,
            iv_rank=25.0, iv_premium=0.80, confidence=_conf(6),  # well below 0.85 pass
        )
        expensive_iv, _ = edge_score.compute(
            strategy="LONG_STRADDLE", pop=55.0, legs=[],
            net_premium_per_share=-150.0, spread_width=0.0,
            iv_rank=25.0, iv_premium=1.30, confidence=_conf(6),  # overpaying
        )
        assert cheap_iv > expensive_iv

    def test_confidence_headroom_lifts_confidence_component(self):
        # 5 soft passes is at the floor (default min_soft_pass=5), 7 is full headroom.
        _, comps_min = edge_score.compute(
            strategy="IRON_CONDOR", pop=70.0, legs=[],
            net_premium_per_share=60.0, spread_width=200.0,
            iv_rank=70.0, iv_premium=1.05, confidence=_conf(5),
        )
        _, comps_max = edge_score.compute(
            strategy="IRON_CONDOR", pop=70.0, legs=[],
            net_premium_per_share=60.0, spread_width=200.0,
            iv_rank=70.0, iv_premium=1.05, confidence=_conf(7),
        )
        assert comps_max["confidence"] > comps_min["confidence"]

    def test_returns_all_four_components(self):
        _, comps = edge_score.compute(
            strategy="LONG_CALL", pop=50.0, legs=[],
            net_premium_per_share=-100.0, spread_width=0.0,
            iv_rank=25.0, iv_premium=0.90, confidence=_conf(5),
        )
        assert set(comps.keys()) == {"pop", "credit_or_debit", "iv_alignment", "confidence"}


# ──────────────────────────────────────────────────────────────────────────
# Strategy isolation — config tweaks for one strategy must not bleed
# ──────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestStrategyIsolation:
    def test_per_strategy_buy_pass_threshold_used(self):
        """LONG_CALL has a higher iv_premium_buy_pass (0.90) than LONG_STRADDLE (0.85).
        At iv_premium=0.88, LONG_CALL still scores full credit on iv_alignment but
        LONG_STRADDLE has already started tapering."""
        _, c_call = edge_score.compute(
            strategy="LONG_CALL", pop=50.0, legs=[],
            net_premium_per_share=-100.0, spread_width=0.0,
            iv_rank=25.0, iv_premium=0.88, confidence=_conf(5),
        )
        _, c_strad = edge_score.compute(
            strategy="LONG_STRADDLE", pop=50.0, legs=[],
            net_premium_per_share=-100.0, spread_width=0.0,
            iv_rank=25.0, iv_premium=0.88, confidence=_conf(5),
        )
        # LONG_CALL still inside its pass band → ≥ LONG_STRADDLE which has tapered
        assert c_call["iv_alignment"] >= c_strad["iv_alignment"]


# ──────────────────────────────────────────────────────────────────────────
# Future-scope stub — issue #8 same-direction concentration
# ──────────────────────────────────────────────────────────────────────────

@pytest.mark.future
@pytest.mark.skip(reason="future: same-direction concentration penalty across underlyings (FUTURE_ENHANCEMENT_SCOPES.md → 🟡 Strategy & Regime Coverage)")
def test_same_direction_concentration_demotes_weaker_suggestion():
    """Two BULL_PUT_SPREAD candidates on correlated underlyings should not both
    persist; the lower edge_score one should be dropped to NoSuggestion with a
    'Concentration cap' reason. Will require orchestration-level coordination
    in lifecycle/suggestion_engine.py before per-underlying persistence."""
    pass
