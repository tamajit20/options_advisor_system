"""
engine/confidence.py
====================

7-condition tiered confidence gate.

Gate tiers
----------
  HARD gate  (must PASS):        DTE in band
  SOFT gates (need ≥ soft_gate_min_pass of 5):
      IV Rank | VIX stable | PCR neutral | OI walls visible | Trend identifiable
  WARNING gate (never blocks):   High-impact event this week → SOFT_FAIL shown
                                 but suggestion always proceeds

Soft gate failure uses SOFT_FAIL (not FAIL) so the suggestion still proceeds
when only one soft gate misses, but the dashboard surfaces it as a visible note.

Data-unavailability statuses
-----------------------------
  PASS_WARN  — gate passed by default because required data is absent
  PASS_ERROR — unhandled exception inside gate logic; treated as pass
"""

from __future__ import annotations

from typing import List, Optional

from config import STRATEGY_CONFIG
from contracts import ConfidenceCheck, ConfidenceResult, MarketIndicators

_PASS       = "PASS"
_FAIL       = "FAIL"
_SOFT_FAIL  = "SOFT_FAIL"
_PASS_WARN  = "PASS_WARN"
_PASS_ERROR = "PASS_ERROR"


def _gate(label: str, fn) -> ConfidenceCheck:
    """Run *fn()* → (status, detail); wrap any exception as PASS_ERROR."""
    try:
        status, detail = fn()
        return ConfidenceCheck(label=label, status=status, detail=detail)
    except Exception as exc:  # noqa: BLE001
        return ConfidenceCheck(
            label=label,
            status=_PASS_ERROR,
            detail=f"Error evaluating gate: {exc}",
        )


def evaluate(
    *,
    iv_rank: Optional[float],
    indicators: MarketIndicators,
    dte: int,
    has_high_impact_event_this_week: bool,
    high_impact_event_description: str = "",
    events_calendar_row_count: int = 0,
) -> ConfidenceResult:
    """Run all 7 gates and return the aggregate result.

    Parameters
    ----------
    iv_rank:
        IV rank 0–100, or None when IV history is not yet loaded.
    events_calendar_row_count:
        Total rows in options_events_calendar.  0 = table never seeded
        → event gate uses PASS_WARN instead of evaluating.
    """
    checks: List[ConfidenceCheck] = []

    # ══════════════════════════════════════════════════════════════
    # SOFT GATES — failure → SOFT_FAIL (not a hard block)
    # ══════════════════════════════════════════════════════════════

    # 1. IV Rank — must be either >50 (writing) or <30 (buying)
    iv_writing_min = STRATEGY_CONFIG["iv_rank_writing_min"]
    iv_buying_max  = STRATEGY_CONFIG["iv_rank_buying_max"]

    def _iv_gate():
        if iv_rank is None:
            return _PASS_WARN, "IV history not yet loaded — cannot evaluate IV Rank"
        iv_ok = (iv_rank > iv_writing_min) or (iv_rank < iv_buying_max)
        return (
            _PASS if iv_ok else _SOFT_FAIL,
            f"IV Rank {iv_rank:.1f} (need >{iv_writing_min:.0f} or <{iv_buying_max:.0f})",
        )

    checks.append(_gate("IV Rank in actionable zone", _iv_gate))

    # 2. VIX stable or falling
    def _vix_gate():
        if indicators.vix_close is None:
            return _PASS_WARN, "VIX data not available for today — cannot evaluate VIX regime"
        vix_ok = indicators.vix_regime == "STABLE"
        return (
            _PASS if vix_ok else _SOFT_FAIL,
            f"VIX regime: {indicators.vix_regime} (close {indicators.vix_close:.2f})",
        )

    checks.append(_gate("VIX stable or falling", _vix_gate))

    # 3. PCR in neutral band
    pcr_lo = STRATEGY_CONFIG["pcr_neutral_low"]
    pcr_hi = STRATEGY_CONFIG["pcr_neutral_high"]

    def _pcr_gate():
        if indicators.pcr is None:
            return _PASS_WARN, "OI data absent (call OI = 0) — cannot compute PCR"
        pcr_ok = pcr_lo <= indicators.pcr <= pcr_hi
        return (
            _PASS if pcr_ok else _SOFT_FAIL,
            f"PCR {indicators.pcr:.2f} (need {pcr_lo:.1f}–{pcr_hi:.1f})",
        )

    checks.append(_gate("PCR in neutral band", _pcr_gate))

    # 4. OI walls visible (at least 2 each side)
    def _walls_gate():
        if indicators.pcr is None:
            return _PASS_WARN, "OI data absent — cannot identify OI walls"
        nc = len(indicators.oi_walls_call)
        np = len(indicators.oi_walls_put)
        walls_ok = nc >= 2 and np >= 2
        return (
            _PASS if walls_ok else _SOFT_FAIL,
            (
                f"Call walls: {nc}, Put walls: {np}"
                f" · Max-pain: \u20b9{indicators.max_pain:,.0f}"
            ),
        )

    checks.append(_gate("OI walls visible", _walls_gate))

    # 5. Trend identifiable
    def _trend_gate():
        if indicators.atr_14 is None:
            return _PASS_WARN, "Insufficient spot history — ATR-14 unavailable, trend unverifiable"
        trend_ok = indicators.trend in ("BULLISH", "BEARISH", "SIDEWAYS")
        return (
            _PASS if trend_ok else _SOFT_FAIL,
            f"Trend: {indicators.trend} · ATR-14: {indicators.atr_14:.0f} pts",
        )

    checks.append(_gate("Trend identifiable", _trend_gate))

    # ══════════════════════════════════════════════════════════════
    # HARD GATES — failure → FAIL (always blocks)
    # ══════════════════════════════════════════════════════════════

    # 6. High-impact event this week — WARNING only, never blocks suggestion
    def _event_gate():
        if events_calendar_row_count == 0:
            return (
                _PASS_WARN,
                "options_events_calendar is empty — event risk unknown, "
                "run events seed job to populate",
            )
        event_ok = not has_high_impact_event_this_week
        if event_ok:
            detail = "No HIGH-impact event in options_events_calendar this week"
        else:
            detail = (
                f"HIGH-impact event this week: {high_impact_event_description}"
                if high_impact_event_description
                else "HIGH-impact event scheduled this week"
            )
        # SOFT_FAIL: visible amber warning in dashboard but never blocks
        return _PASS if event_ok else _SOFT_FAIL, detail

    checks.append(_gate("No high-impact event this week", _event_gate))

    # 7. DTE in band — always computable, hard block
    dte_min = STRATEGY_CONFIG["dte_min"]
    dte_max = STRATEGY_CONFIG["dte_max"]

    def _dte_gate():
        dte_ok = dte_min <= dte <= dte_max
        return (
            _PASS if dte_ok else _FAIL,
            f"DTE {dte} (need {dte_min}–{dte_max})",
        )

    checks.append(_gate("DTE within target band", _dte_gate))

    # ══════════════════════════════════════════════════════════════
    # Score + all_passed
    # ══════════════════════════════════════════════════════════════
    soft_min   = STRATEGY_CONFIG["soft_gate_min_pass"]   # default 4
    soft_total = 5  # gates 1–5

    hard_failed = sum(1 for c in checks[5:] if c.status == _FAIL)
    soft_failed = sum(1 for c in checks[:5] if c.status == _SOFT_FAIL)

    all_passed = hard_failed == 0 and soft_failed <= (soft_total - soft_min)

    score = sum(1 for c in checks if c.passed)
    total = len(checks)

    failed_reasons = [c.detail for c in checks if c.status in (_FAIL, _SOFT_FAIL)]

    return ConfidenceResult(
        score=score,
        total=total,
        all_passed=all_passed,
        checks=checks,
        failed_reasons=failed_reasons,
    )
