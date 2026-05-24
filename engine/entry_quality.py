"""
engine/entry_quality.py
=======================

Composite suggestion quality score (0–100) for display and post-hoc analysis.

Computed once at suggestion generation from edge_score, confidence gate count,
and probability_of_profit. Stored on options_suggestions.entry_quality_score.
"""

from __future__ import annotations

from typing import Optional

# Normalizer for confidence_score → 0–100 unit (matches dashboard legacy formula).
_CONFIDENCE_NORMALIZER = 14


def compute(
    *,
    edge_score: Optional[float],
    confidence_score: Optional[int],
    probability_of_profit: Optional[float],
) -> Optional[int]:
    """Return rounded 0–100 quality score, or None if all inputs are missing."""
    if edge_score is None and confidence_score is None and probability_of_profit is None:
        return None
    e = float(edge_score or 0.0)
    if confidence_score is not None:
        c = min(float(confidence_score) / _CONFIDENCE_NORMALIZER * 100.0, 100.0)
    else:
        c = 50.0  # neutral when confidence unavailable
    p = float(probability_of_profit or 0.0)
    return int(round(e * 0.50 + c * 0.30 + p * 0.20))
