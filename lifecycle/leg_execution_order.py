"""
lifecycle/leg_execution_order.py
================================

Deterministic leg sequencing for multi-leg option structures — mirrors the
dashboard `executionOrder()` rules so Zerodha placement matches the UI banners.

Entry: BUY hedges first, then SELL shorts (Jade Lizard has a custom sequence).
Close: buy back shorts first, then sell longs (Jade Lizard reversed).
"""

from __future__ import annotations

from typing import Dict, Iterable, List


def leg_execution_order(
    legs: Iterable[dict],
    strategy: str,
    *,
    mode: str = "entry",
) -> Dict[int, int]:
    """Return ``{leg_order: step}`` where step is 1..N. Empty for single-leg."""
    items = list(legs)
    if len(items) <= 1:
        return {}

    is_jade = (strategy or "").upper() == "JADE_LIZARD"
    sorted_legs = list(items)

    if mode == "entry":
        if is_jade:
            def _rank(leg: dict) -> int:
                act = (leg.get("action") or "").upper()
                opt = (leg.get("option_type") or "").upper()
                if act == "BUY" and opt == "CE":
                    return 0
                if act == "SELL" and opt == "CE":
                    return 1
                if act == "SELL" and opt == "PE":
                    return 2
                return 3

            sorted_legs.sort(key=lambda l: (_rank(l), int(l.get("leg_order") or 0)))
        else:
            sorted_legs.sort(
                key=lambda l: (
                    0 if (l.get("action") or "").upper() == "BUY" else 1,
                    int(l.get("leg_order") or 0),
                )
            )
    else:
        if is_jade:
            def _rank_close(leg: dict) -> int:
                act = (leg.get("action") or "").upper()
                opt = (leg.get("option_type") or "").upper()
                if act == "SELL" and opt == "PE":
                    return 0
                if act == "SELL" and opt == "CE":
                    return 1
                if act == "BUY" and opt == "CE":
                    return 2
                return 3

            sorted_legs.sort(key=lambda l: (_rank_close(l), int(l.get("leg_order") or 0)))
        else:
            sorted_legs.sort(
                key=lambda l: (
                    0 if (l.get("action") or "").upper() == "SELL" else 1,
                    int(l.get("leg_order") or 0),
                )
            )

    return {int(l["leg_order"]): i + 1 for i, l in enumerate(sorted_legs)}


def legs_in_execution_order(
    legs: Iterable[dict],
    strategy: str,
    *,
    mode: str = "entry",
) -> List[dict]:
    order = leg_execution_order(legs, strategy, mode=mode)
    if not order:
        return list(legs)
    return sorted(legs, key=lambda l: order.get(int(l["leg_order"]), 99))
