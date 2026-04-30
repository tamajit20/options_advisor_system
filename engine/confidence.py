"""
engine/confidence.py
====================

The 7-condition confidence gate. ALL must pass; 6/7 = no suggestion.

Pure function. Inputs are simple values; output is a `ConfidenceResult`.
"""

from __future__ import annotations

from datetime import date
from typing import List

from config import STRATEGY_CONFIG
from contracts import ConfidenceCheck, ConfidenceResult, MarketIndicators


def evaluate(
    *,
    iv_rank: float,
    indicators: MarketIndicators,
    dte: int,
    has_high_impact_event_this_week: bool,
) -> ConfidenceResult:
    """Run all 7 gates and return the aggregate result."""
    checks: List[ConfidenceCheck] = []

    # 1. IV Rank — must be either >50 (writing) or <30 (buying)
    iv_writing_min = STRATEGY_CONFIG["iv_rank_writing_min"]
    iv_buying_max  = STRATEGY_CONFIG["iv_rank_buying_max"]
    iv_ok = (iv_rank > iv_writing_min) or (iv_rank < iv_buying_max)
    checks.append(ConfidenceCheck(
        label="IV Rank in actionable zone",
        passed=iv_ok,
        detail=f"IV Rank {iv_rank:.1f} (need >{iv_writing_min:.0f} or <{iv_buying_max:.0f})",
    ))

    # 2. VIX stable or falling
    vix_ok = indicators.vix_regime == "STABLE"
    checks.append(ConfidenceCheck(
        label="VIX stable or falling",
        passed=vix_ok,
        detail=f"VIX regime: {indicators.vix_regime} (close {indicators.vix_close:.2f})",
    ))

    # 3. PCR in neutral band
    pcr_lo = STRATEGY_CONFIG["pcr_neutral_low"]
    pcr_hi = STRATEGY_CONFIG["pcr_neutral_high"]
    pcr_ok = pcr_lo <= indicators.pcr <= pcr_hi
    checks.append(ConfidenceCheck(
        label="PCR in neutral band",
        passed=pcr_ok,
        detail=f"PCR {indicators.pcr:.2f} (need {pcr_lo:.1f}–{pcr_hi:.1f})",
    ))

    # 4. OI walls visible (at least 2 each side)
    walls_ok = len(indicators.oi_walls_call) >= 2 and len(indicators.oi_walls_put) >= 2
    checks.append(ConfidenceCheck(
        label="OI walls visible",
        passed=walls_ok,
        detail=f"Call walls: {len(indicators.oi_walls_call)}, Put walls: {len(indicators.oi_walls_put)}",
    ))

    # 5. Trend clear
    trend_ok = indicators.trend in ("BULLISH", "BEARISH", "SIDEWAYS")
    # All three are "clear" — what we really reject is a NaN/UNKNOWN trend
    # which doesn't currently occur. We treat SIDEWAYS as clear for credit
    # spreads, BULLISH/BEARISH for directional spreads.
    checks.append(ConfidenceCheck(
        label="Trend identifiable",
        passed=trend_ok,
        detail=f"Trend: {indicators.trend}",
    ))

    # 6. No major event this week
    event_ok = not has_high_impact_event_this_week
    checks.append(ConfidenceCheck(
        label="No high-impact event this week",
        passed=event_ok,
        detail="No HIGH-impact event in events calendar this week" if event_ok
               else "HIGH-impact event scheduled this week",
    ))

    # 7. DTE in band
    dte_min = STRATEGY_CONFIG["dte_min"]
    dte_max = STRATEGY_CONFIG["dte_max"]
    dte_ok = dte_min <= dte <= dte_max
    checks.append(ConfidenceCheck(
        label="DTE within target band",
        passed=dte_ok,
        detail=f"DTE {dte} (need {dte_min}–{dte_max})",
    ))

    score = sum(1 for c in checks if c.passed)
    total = len(checks)
    threshold = STRATEGY_CONFIG["confidence_min_pass_count"]
    all_passed = score >= threshold

    failed_reasons = [c.detail for c in checks if not c.passed]
    return ConfidenceResult(
        score=score,
        total=total,
        all_passed=all_passed,
        checks=checks,
        failed_reasons=failed_reasons,
    )
