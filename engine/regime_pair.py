"""
engine/regime_pair.py
=====================

Sideways-market regime pairs: one range trade + one breakout trade.

The dashboard shows both so the operator can pick a thesis. ``pick_regime_pair_preferred``
marks which leg has better estimated success (PoP + edge) for the current data.
"""

from __future__ import annotations

from typing import Optional, Tuple

from config import STRATEGY_CONFIG

from contracts import Suggestion

_RANGE_STRATEGIES = frozenset({
    "IRON_CONDOR", "IRON_BUTTERFLY", "CALENDAR_SPREAD",
})
_BREAKOUT_STRATEGIES = frozenset({
    "LONG_STRADDLE", "LONG_STRANGLE",
})


def strategy_regime_pair_type(strategy: str) -> Optional[str]:
    """Return ``range`` | ``breakout`` or None if not part of a regime pair."""
    if strategy in _RANGE_STRATEGIES:
        return "range"
    if strategy in _BREAKOUT_STRATEGIES:
        return "breakout"
    return None


def resolve_regime_pair_strategies(
    *,
    iv_rank: float,
    has_long_vol_catalyst: bool = False,
) -> Tuple[str, Optional[str]]:
    """Pick (range_strategy, breakout_strategy) for a sideways market.

    Breakout leg is omitted when IV is already elevated (writing regime) —
    options are expensive and a long-vol bet is structurally weak.
    """
    writing_min = float(STRATEGY_CONFIG["iv_rank_writing_min"])
    butterfly_min = float(STRATEGY_CONFIG.get("iv_rank_butterfly_min", 70.0))

    if iv_rank > writing_min:
        if iv_rank > butterfly_min:
            return "IRON_BUTTERFLY", None
        return "IRON_CONDOR", None

    # Low / mid IV — calendar for range, straddle for breakout attempt.
    breakout = "LONG_STRADDLE"
    if has_long_vol_catalyst:
        breakout = "LONG_STRADDLE"
    return "CALENDAR_SPREAD", breakout


def regime_pair_group_id(
    *,
    underlying: str,
    expiry_type: str,
    entry_date,
) -> str:
    return f"{underlying}:{expiry_type}:{entry_date.isoformat()}"


def _success_score(sug: Suggestion) -> float:
    pop = float(sug.economics.probability_of_profit or 0.0)
    edge = float(getattr(sug.economics, "edge_score", 0.0) or 0.0)
    return 0.70 * pop + 0.30 * edge


def pick_regime_pair_preferred(
    range_sug: Suggestion,
    breakout_sug: Suggestion,
    *,
    iv_rank: float,
) -> Tuple[str, str]:
    """Return (preferred_type, reason) where preferred_type is ``range`` or ``breakout``."""
    range_score = _success_score(range_sug)
    breakout_score = _success_score(breakout_sug)
    range_pop = float(range_sug.economics.probability_of_profit or 0.0)
    breakout_pop = float(breakout_sug.economics.probability_of_profit or 0.0)

    writing_min = float(STRATEGY_CONFIG["iv_rank_writing_min"])
    buying_max = float(STRATEGY_CONFIG["iv_rank_buying_max"])

    if range_score >= breakout_score:
        preferred = "range"
        if iv_rank > writing_min:
            reason = (
                f"System prefers the range trade — IV rank is {iv_rank:.0f}% (elevated), "
                f"so selling premium has better odds ({range_pop:.0f}% vs {breakout_pop:.0f}% "
                f"estimated success) than betting on a bigger move."
            )
        else:
            reason = (
                f"System prefers the range trade — estimated success {range_pop:.0f}% "
                f"vs {breakout_pop:.0f}% for the breakout trade with current IV/VIX data."
            )
    else:
        preferred = "breakout"
        if iv_rank < buying_max:
            reason = (
                f"System prefers the breakout trade — options are relatively cheap "
                f"(IV rank {iv_rank:.0f}%) and the move scenario scores higher "
                f"({breakout_pop:.0f}% vs {range_pop:.0f}% estimated success)."
            )
        else:
            reason = (
                f"System prefers the breakout trade — estimated success {breakout_pop:.0f}% "
                f"vs {range_pop:.0f}% for the range trade on today's data."
            )
    return preferred, reason


def apply_regime_pair_metadata(
    suggestions: list[tuple[Suggestion, str]],
    *,
    group_id: str,
    iv_rank: float,
) -> None:
    """Mutate suggestions in-place with pair grouping + preferred flag."""
    if len(suggestions) == 1:
        sug, ptype = suggestions[0]
        sug.regime_pair_group = group_id
        sug.regime_pair_type = ptype
        sug.regime_pair_preferred = True
        sug.regime_pair_preference_reason = (
            "Only one sideways scenario passed today's risk checks."
        )
        return

    by_type = {ptype: sug for sug, ptype in suggestions}
    preferred, reason = pick_regime_pair_preferred(
        by_type["range"], by_type["breakout"], iv_rank=iv_rank,
    )
    for sug, ptype in suggestions:
        sug.regime_pair_group = group_id
        sug.regime_pair_type = ptype
        sug.regime_pair_preferred = ptype == preferred
        sug.regime_pair_preference_reason = reason if ptype == preferred else None


def encode_regime_pair_trigger_reason(s: Suggestion) -> Optional[str]:
    """Serialize pair metadata for ``options_suggestions.trigger_reason``."""
    if not s.regime_pair_group:
        return None
    import json
    return json.dumps({
        "regime_pair_group": s.regime_pair_group,
        "regime_pair_type": s.regime_pair_type,
        "regime_pair_preferred": s.regime_pair_preferred,
        "regime_pair_preference_reason": s.regime_pair_preference_reason,
    })


def decode_regime_pair_trigger_reason(raw: Optional[str]) -> dict:
    if not raw:
        return {}
    import json
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(data, dict) or not data.get("regime_pair_group"):
        return {}
    return data
