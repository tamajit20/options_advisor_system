"""Shared entry limit price for auto-enter and profit gates."""

from __future__ import annotations

from typing import Optional


def entry_limit_price(
    enriched: dict,
    signal: dict,
    ltp: Optional[float] = None,
) -> float:
    """Limit price for Step 1 — conservative band edge (BUY at entry_max, SELL at entry_min)."""
    action = str(signal.get("action") or "BUY").upper()
    ref = ltp
    if ref is None or float(ref) <= 0:
        ref = float(signal.get("ltp") or 0)
    try:
        if action == "BUY":
            return float(enriched.get("entry_max") or ref)
        return float(enriched.get("entry_min") or ref)
    except (TypeError, ValueError):
        return float(ref or 0)
