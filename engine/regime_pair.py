"""
engine/regime_pair.py
=====================

Sideways-market regime pairs: one range trade + one breakout trade.

The dashboard shows both so the operator can pick a thesis. ``pick_regime_pair_preferred``
marks which leg has better estimated success (PoP + edge) for the current data.
``complete_regime_pair`` always emits two tagged partners — a failed leg becomes a
``NoSuggestion`` in the same group rather than leaving the survivor alone.
"""

from __future__ import annotations

import json
from typing import Mapping, Optional, Sequence, Tuple, Union

from config import STRATEGY_CONFIG

from contracts import ConfidenceResult, NoSuggestion, Suggestion

# Matches options_suggestions.trigger_reason NVARCHAR(500) in database/schema.py
_TRIGGER_REASON_MAX_CHARS = 500

_PAIR_TYPES: Tuple[str, ...] = ("range", "breakout")
RegimePairMember = Union[Suggestion, NoSuggestion]

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
    suggestions: list[tuple[RegimePairMember, str]],
    *,
    group_id: str,
    iv_rank: float,
) -> None:
    """Mutate pair members in-place with grouping + preferred flag.

    A loner is left untagged — pairing requires both partners so the UI never
    shows a single "pick the scenario" card.
    """
    if len(suggestions) < 2:
        return

    live = {
        ptype: item
        for item, ptype in suggestions
        if isinstance(item, Suggestion)
    }
    if len(live) >= 2 and "range" in live and "breakout" in live:
        preferred, reason = pick_regime_pair_preferred(
            live["range"], live["breakout"], iv_rank=iv_rank,
        )
    elif len(live) == 1:
        preferred = next(iter(live))
        blocked_item = next(
            (item for item, ptype in suggestions if ptype != preferred),
            None,
        )
        blocked_reason = ""
        if isinstance(blocked_item, NoSuggestion) and blocked_item.reason:
            blocked_reason = blocked_item.reason
        if blocked_reason:
            reason = (
                f"System prefers the {preferred} trade — {blocked_reason}"
            )
        else:
            blocked_type = "breakout" if preferred == "range" else "range"
            reason = (
                f"System prefers the {preferred} trade — the {blocked_type} "
                f"scenario did not pass today's gates."
            )
    else:
        preferred = None
        reason = "Neither sideways scenario passed today's gates."

    for item, ptype in suggestions:
        item.regime_pair_group = group_id
        item.regime_pair_type = ptype
        item.regime_pair_preferred = bool(preferred) and ptype == preferred
        item.regime_pair_preference_reason = reason if ptype == preferred else None


def complete_regime_pair(
    built: Sequence[tuple[Suggestion, str]],
    *,
    missing_reasons: Mapping[str, str],
    group_id: str,
    iv_rank: float,
    underlying: str,
    confidence: ConfidenceResult,
    generated_on,
    intended_types: Sequence[str] = _PAIR_TYPES,
) -> tuple[list[Suggestion], list[NoSuggestion]]:
    """Always return two tagged partners for a sideways range+breakout pair.

    Legs that assembled become PENDING suggestions. Legs that failed gates
    become ``NoSuggestion`` rows in the same ``regime_pair_group``.
    """
    by_type = {ptype: sug for sug, ptype in built}
    members: list[tuple[RegimePairMember, str]] = []
    suggestions: list[Suggestion] = []
    no_suggestions: list[NoSuggestion] = []

    for ptype in intended_types:
        if ptype in by_type:
            members.append((by_type[ptype], ptype))
            suggestions.append(by_type[ptype])
            continue
        cause = missing_reasons.get(ptype) or "did not pass today's strategy gates"
        thesis = (
            "range (flat/stay-in-range) "
            if ptype == "range"
            else "breakout (sharp up or down) "
        )
        ns = NoSuggestion(
            generated_on=generated_on,
            underlying=underlying,
            confidence=confidence,
            reason=f"Sideways {thesis}scenario blocked: {cause}",
        )
        members.append((ns, ptype))
        no_suggestions.append(ns)

    apply_regime_pair_metadata(members, group_id=group_id, iv_rank=iv_rank)
    return suggestions, no_suggestions


def encode_regime_pair_trigger_reason(s: RegimePairMember) -> Optional[str]:
    """Serialize pair metadata for ``options_suggestions.trigger_reason``."""
    if not getattr(s, "regime_pair_group", None):
        return None
    payload = {
        "regime_pair_group": s.regime_pair_group,
        "regime_pair_type": s.regime_pair_type,
        "regime_pair_preferred": bool(getattr(s, "regime_pair_preferred", False)),
        "regime_pair_preference_reason": getattr(
            s, "regime_pair_preference_reason", None,
        ),
    }
    raw = json.dumps(payload)
    if len(raw) > _TRIGGER_REASON_MAX_CHARS:
        payload["regime_pair_preference_reason"] = None
        raw = json.dumps(payload)
    return raw[:_TRIGGER_REASON_MAX_CHARS]


def decode_regime_pair_trigger_reason(raw: Optional[str]) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(data, dict) or not data.get("regime_pair_group"):
        return {}
    return data
