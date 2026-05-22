"""
engine/stop_loss_levels.py
==========================

Strategy-aware Nifty spot stop bands for credit structures.

Returns ``(upper_stop, lower_stop)``:
  - Breach **upper** when spot >= upper_stop (rally hurts put credit / call spread tested).
  - Breach **lower** when spot <= lower_stop (fall hurts call credit / put spread tested).

Debit spreads and naked long premium return ``(None, None)`` — those use MTM loss
(``effective_sl_rs`` in ``exit_engine`` / ``live_risk_monitor``).

``options_suggestions.stop_loss_level`` stores **upper_stop** when present, else
**lower_stop** when only one band exists (bull put, jade lizard). Consumers that
need both bands (iron condor) must call ``compute_spot_stop_bands()`` with legs.
"""

from __future__ import annotations

from typing import Optional, Sequence, Tuple

from contracts import SuggestionLeg

# Mirror strategy_selector registries (avoid import cycle).
# CALENDAR_SPREAD is a net-debit structure — no spot trigger, exits on MTM loss.
_DEBIT_STRATEGIES = frozenset({
    "LONG_STRADDLE", "LONG_STRANGLE", "LONG_CALL", "LONG_PUT",
    "BULL_CALL_SPREAD", "BEAR_PUT_SPREAD", "CALENDAR_SPREAD",
})


def _leg(legs: Sequence[SuggestionLeg], action: str, opt: str) -> Optional[SuggestionLeg]:
    return next((l for l in legs if l.action == action and l.option_type == opt), None)


def compute_spot_stop_bands(
    legs: Sequence[SuggestionLeg],
    strategy: str,
    *,
    net_premium_per_share: float = 0.0,
) -> Tuple[Optional[float], Optional[float]]:
    """Compute upper/lower spot stop bands for a strategy.

    Parameters
    ----------
    legs:
        Suggestion legs (strikes + actions).
    strategy:
        Strategy code (e.g. ``IRON_CONDOR``).
    net_premium_per_share:
        Signed net premium per share (credit positive). Used for jade lizard
        downside band when there is no long-put wing.

    Returns
    -------
    (upper_stop, lower_stop) — either may be ``None``.
    """
    if strategy in _DEBIT_STRATEGIES or not legs:
        return None, None

    sc = _leg(legs, "SELL", "CE")
    sp = _leg(legs, "SELL", "PE")
    lc = _leg(legs, "BUY", "CE")
    lp = _leg(legs, "BUY", "PE")

    if strategy in ("IRON_CONDOR", "IRON_BUTTERFLY"):
        if sc is None or sp is None:
            return None, None
        wing = abs((lc.strike if lc else sc.strike) - sc.strike)
        half = wing * 0.5
        upper = round(sc.strike + half)
        lower = round(sp.strike - half)
        return upper, lower

    if strategy == "JADE_LIZARD":
        # Upside is hedged by the call spread; only downside short put matters.
        if sp is None:
            return None, None
        if lp is not None:
            half = abs(sp.strike - lp.strike) * 0.5
            return None, round(sp.strike - half)
        credit = max(net_premium_per_share, 0.0)
        return None, round(sp.strike - credit * 0.5)

    if strategy == "BEAR_CALL_SPREAD":
        if sc is None or lc is None:
            return None, None
        half = abs(lc.strike - sc.strike) * 0.5
        return round(sc.strike + half), None

    if strategy == "BULL_PUT_SPREAD":
        if sp is None or lp is None:
            return None, None
        half = abs(sp.strike - lp.strike) * 0.5
        return None, round(sp.strike - half)

    return None, None


def primary_stop_loss_level(
    legs: Sequence[SuggestionLeg],
    strategy: str,
    *,
    net_premium_per_share: float = 0.0,
) -> Optional[float]:
    """Single level persisted on ``options_suggestions.stop_loss_level``.

    - Two-sided credit (IC / IB): upper band (put side derived at runtime).
    - Bear call / bull put: the one active band.
    - Jade: lower band only.
    - Debit / long premium: ``None``.
    """
    upper, lower = compute_spot_stop_bands(
        legs, strategy, net_premium_per_share=net_premium_per_share,
    )
    if upper is not None:
        return upper
    return lower


def spot_stop_breached(
    *,
    strategy: str,
    spot: float,
    legs: Sequence[SuggestionLeg],
    stored_sl_level: Optional[float] = None,
    net_premium_per_share: float = 0.0,
) -> bool:
    """True if ``spot`` has crossed any applicable stop band."""
    if strategy in _DEBIT_STRATEGIES:
        return False
    upper, lower = compute_spot_stop_bands(
        legs, strategy, net_premium_per_share=net_premium_per_share,
    )
    if upper is not None and spot >= upper:
        return True
    if lower is not None and spot <= lower:
        return True
    # Legacy rows: only stored_sl_level — infer direction from strategy.
    if stored_sl_level is not None and upper is None and lower is None:
        if strategy in ("BEAR_CALL_SPREAD",):
            return spot >= stored_sl_level
        if strategy in ("BULL_PUT_SPREAD", "BEAR_PUT_SPREAD", "JADE_LIZARD"):
            return spot <= stored_sl_level
    return False
