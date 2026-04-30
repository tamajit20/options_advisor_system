"""
engine/iv_rank.py
=================

IV Rank and IV Percentile calculations.

IV Rank      = (current − 52w low) / (52w high − 52w low) × 100
IV Percentile = % of days in last 52 weeks where IV was below current

Pure functions. Caller supplies the historical ATM IV series.
"""

from __future__ import annotations

from typing import Sequence


def iv_rank(current_iv: float, history: Sequence[float]) -> float:
    if not history:
        return 0.0
    lo = min(history)
    hi = max(history)
    if hi <= lo:
        return 50.0
    return max(0.0, min(100.0, (current_iv - lo) / (hi - lo) * 100.0))


def iv_percentile(current_iv: float, history: Sequence[float]) -> float:
    if not history:
        return 0.0
    below = sum(1 for v in history if v < current_iv)
    return below / len(history) * 100.0


def pick_atm_iv(strikes_with_iv: list[tuple[float, str, float]], spot: float) -> float | None:
    """From a list of (strike, option_type, iv), return the ATM (closest-strike)
    IV averaged across CE+PE."""
    if not strikes_with_iv or spot <= 0:
        return None
    # Find the strike closest to spot
    distinct_strikes = sorted({s for s, _t, _ in strikes_with_iv}, key=lambda s: abs(s - spot))
    if not distinct_strikes:
        return None
    atm = distinct_strikes[0]
    ivs = [iv for s, _t, iv in strikes_with_iv if s == atm and iv > 0]
    if not ivs:
        return None
    return sum(ivs) / len(ivs)
