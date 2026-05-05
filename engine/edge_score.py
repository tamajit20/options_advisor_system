"""
engine/edge_score.py
====================

Numeric trade-quality score (0–100) — display + ranking only, never gates a
suggestion. Pure function of contract values + config; no I/O.

Components (weights live in `config.STRATEGY_CONFIG["edge_score_weights"]`):

  pop             — probability of profit (0–100 scaled to weight)
  credit_or_debit — credit-to-width grade for CREDIT strategies, or
                    debit-discount (1 − debit/spread_width) for defined-debit
                    strategies, or PoP-as-tie-breaker for naked longs
  iv_alignment    — IV/HV ratio aligned with regime (writing wants high, buying low)
  confidence      — soft-pass count above the strategy's required minimum

Strategy isolation: every threshold pulled per-strategy with a default fallback,
so retuning one strategy cannot affect another.
"""

from __future__ import annotations

from typing import Optional, Sequence, Tuple

from config import STRATEGY_CONFIG
from contracts import ConfidenceResult, SuggestionLeg

# Strategy classification (kept local so callers don't have to import the
# selector module — avoids a back-edge in the import graph).
_CREDIT_STRATEGIES = frozenset({
    "IRON_CONDOR", "BULL_PUT_SPREAD", "BEAR_CALL_SPREAD",
    "IRON_BUTTERFLY", "JADE_LIZARD",
})
_DEFINED_DEBIT_STRATEGIES = frozenset({
    "BULL_CALL_SPREAD", "BEAR_PUT_SPREAD",
})
_NAKED_LONG_STRATEGIES = frozenset({
    "LONG_STRADDLE", "LONG_STRANGLE", "LONG_CALL", "LONG_PUT",
})


def grade_credit_to_width(ratio: float) -> str:
    """Return 'weak' | 'good' | 'strong' for a credit-to-width ratio."""
    tiers = STRATEGY_CONFIG.get("credit_to_width_grade_thresholds", {}) or {}
    good = float(tiers.get("good", 0.25))
    strong = float(tiers.get("strong", 0.30))
    if ratio >= strong:
        return "strong"
    if ratio >= good:
        return "good"
    return "weak"


def _credit_or_debit_score(
    strategy: str,
    net_premium_per_share: float,
    spread_width: float,
    pop: float,
) -> float:
    """0–1 score for the 'credit_or_debit' component."""
    if strategy in _CREDIT_STRATEGIES:
        if spread_width <= 0:
            return 0.0
        ratio = max(0.0, net_premium_per_share) / spread_width
        # Linear within tiers: weak floor → strong ceiling
        tiers = STRATEGY_CONFIG.get("credit_to_width_grade_thresholds", {}) or {}
        good = float(tiers.get("good", 0.25))
        strong = float(tiers.get("strong", 0.30))
        if ratio >= strong:
            return 1.0
        if ratio >= good:
            # Good tier maps to 0.6–1.0
            return 0.6 + 0.4 * (ratio - good) / max(strong - good, 1e-9)
        # Weak tier maps to 0.0–0.6
        return max(0.0, 0.6 * ratio / max(good, 1e-9))

    if strategy in _DEFINED_DEBIT_STRATEGIES:
        # Lower debit / wider width = better score.
        if spread_width <= 0:
            return 0.0
        debit = max(0.0, -net_premium_per_share)  # debit stored as negative net_premium
        # Best when debit is small fraction of width (room to profit).
        # debit/width ~ 0.30 → 0.7 score; 0.50 → 0.5 score; 0.70 → 0.3 score.
        return max(0.0, min(1.0, 1.0 - debit / spread_width))

    if strategy in _NAKED_LONG_STRATEGIES:
        # No spread width — use PoP as tie-breaker so high-PoP longs rank above
        # low-PoP longs even before the dedicated PoP component is applied.
        return max(0.0, min(1.0, pop / 100.0))

    return 0.0


def _iv_alignment_score(
    strategy: str,
    iv_rank: Optional[float],
    iv_premium: Optional[float],
) -> float:
    """0–1 score for IV regime alignment.

    Writing strategies want high IV/HV. Buying/debit strategies want low IV/HV
    (per-strategy 'real edge' threshold from `strategy_iv_premium_buy_pass`).
    """
    if iv_premium is None or iv_rank is None:
        return 0.5  # neutral when data unavailable

    iv_writing_min = float(STRATEGY_CONFIG.get("iv_rank_writing_min", 50.0))
    iv_buying_max = float(STRATEGY_CONFIG.get("iv_rank_buying_max", 30.0))

    if strategy in _CREDIT_STRATEGIES:
        # Writing: best when IV/HV ≥ sell_min (1.0+), worst when < 0.9.
        sell_min = float(STRATEGY_CONFIG.get("iv_premium_sell_min", 0.90))
        # Map iv_premium ∈ [sell_min, sell_min+0.5] → [0.5, 1.0]; below sell_min taper to 0.
        if iv_premium >= sell_min:
            return min(1.0, 0.5 + (iv_premium - sell_min) / 1.0)
        return max(0.0, 0.5 * iv_premium / sell_min)

    if strategy in _DEFINED_DEBIT_STRATEGIES or strategy in _NAKED_LONG_STRATEGIES:
        # Buying: per-strategy "real edge" threshold (default 1.00).
        per_strat = STRATEGY_CONFIG.get("strategy_iv_premium_buy_pass", {}) or {}
        edge_thr = float(per_strat.get(strategy, STRATEGY_CONFIG.get("iv_premium_buy_pass", 1.00)))
        # ≤ edge_thr → 1.0; up to buy_max → linear taper to 0.
        buy_max = float(STRATEGY_CONFIG.get("iv_premium_buy_max", 1.50))
        if iv_premium <= edge_thr:
            return 1.0
        if iv_premium >= buy_max:
            return 0.0
        return max(0.0, 1.0 - (iv_premium - edge_thr) / max(buy_max - edge_thr, 1e-9))

    return 0.5


def _confidence_score(strategy: str, confidence: ConfidenceResult) -> float:
    """0–1 score from soft-pass count above the strategy's required minimum."""
    overrides = STRATEGY_CONFIG.get("strategy_min_soft_pass", {}) or {}
    required = int(overrides.get(strategy, STRATEGY_CONFIG.get("soft_gate_min_pass", 5)))
    soft_total = 7  # gates 1–7 in confidence.evaluate
    soft_passed = sum(
        1 for c in list(confidence.checks)[:soft_total]
        if c.status not in ("FAIL", "SOFT_FAIL")
    )
    headroom = max(0, soft_passed - required)
    max_headroom = max(1, soft_total - required)
    return min(1.0, headroom / max_headroom)


def compute(
    *,
    strategy: str,
    pop: float,
    legs: Sequence[SuggestionLeg],
    net_premium_per_share: float,
    spread_width: float,
    iv_rank: Optional[float],
    iv_premium: Optional[float],
    confidence: ConfidenceResult,
) -> Tuple[float, dict]:
    """Compute edge_score (0–100) and per-component breakdown.

    Returns
    -------
    score : float        0–100, rounded to 1 decimal
    components : dict    component-name → contributed points (sums to score)
    """
    weights = STRATEGY_CONFIG.get("edge_score_weights", {}) or {}

    pop_unit = max(0.0, min(1.0, pop / 100.0))
    cd_unit = _credit_or_debit_score(strategy, net_premium_per_share, spread_width, pop)
    iv_unit = _iv_alignment_score(strategy, iv_rank, iv_premium)
    cf_unit = _confidence_score(strategy, confidence)

    components = {
        "pop":             round(pop_unit * float(weights.get("pop", 40.0)), 2),
        "credit_or_debit": round(cd_unit * float(weights.get("credit_or_debit", 25.0)), 2),
        "iv_alignment":    round(iv_unit * float(weights.get("iv_alignment", 20.0)), 2),
        "confidence":      round(cf_unit * float(weights.get("confidence", 15.0)), 2),
    }
    score = round(sum(components.values()), 1)
    return score, components
