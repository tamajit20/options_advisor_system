"""Scout entry profitability gates — expected net vs Zerodha intraday costs."""

from __future__ import annotations

from typing import Optional, Set

from engine.equity_charges import estimate_equity_intraday_charges_for_target
from scout.settings_schema import default_scout_settings

VALID_AUTO_SIGNAL_TYPES = frozenset({
    "OR_BREAK_UP",
    "OR_BREAK_DOWN",
    "RANGE_BREAK_UP",
    "RANGE_BREAK_DOWN",
    "PULLBACK_UP",
    "PULLBACK_DOWN",
})


def signal_type_allowed(settings: dict, signal_type: Optional[str]) -> bool:
    allowed: Set[str] = {
        str(s).upper() for s in (settings.get("auto_enter_signal_types") or [])
    }
    if not allowed:
        return True
    return str(signal_type or "").upper() in allowed


def entry_profit_block_reason(
    *,
    signal: dict,
    entry: float,
    qty: int,
    settings: dict,
) -> Optional[str]:
    """None if entry passes cost gate; else human-readable skip reason."""
    from scout.signal_enrichment import build_exit_plan

    plan = build_exit_plan(signal, entry_price=float(entry), settings=settings)
    target = plan.get("target_price")
    stop = plan.get("stop_price")
    if target is None or stop is None:
        return "missing stop or target for profitability check"

    action = str(signal.get("action") or "").upper()
    qty = max(int(qty), 1)
    entry_f = float(entry)
    target_f = float(target)
    if action == "BUY":
        expected_gross = (target_f - entry_f) * qty
    else:
        expected_gross = (entry_f - target_f) * qty

    charges = estimate_equity_intraday_charges_for_target(
        entry=entry_f, target=target_f, qty=qty,
    ).total
    min_net = float(settings.get("min_net_profit_inr", 100.0))
    expected_net = expected_gross - charges
    if expected_net < min_net:
        r_mult = plan.get("target_r", settings.get("min_target_r", 2.0))
        return (
            f"expected net ₹{expected_net:.0f} at {target_f:.2f} ({r_mult}R target) "
            f"(gross ₹{expected_gross:.0f} − charges ₹{charges:.0f}) "
            f"< min ₹{min_net:.0f}"
        )
    return None
