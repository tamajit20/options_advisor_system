"""
engine/exit_pricing.py
======================

Shared helpers for computing realistic option close prices when settling /
closing a trade. Sanitises raw EOD chain rows that occasionally carry a
spot-value-shaped ``settle_price`` (observed in prod on NIFTY 24300 PE where
``settle_price`` came back as ~23,618, equal to NIFTY spot) so that downstream
P&L estimates do not balloon by 3-4 orders of magnitude.

Both the batch EOD ``lifecycle.exit_orchestrator`` and the interactive
``dashboard.server`` close-suggestion endpoint go through this module.
"""

from __future__ import annotations

from typing import Optional, Tuple


def intrinsic_value(option_type: str, strike: float, spot: float) -> float:
    """Cash-settlement value of an option at expiry.

    CE = max(0, spot - strike); PE = max(0, strike - spot).
    """
    if (option_type or "").upper() == "CE":
        return max(0.0, float(spot) - float(strike))
    return max(0.0, float(strike) - float(spot))


def sanitized_close_price(
    *,
    option_type: str,
    strike: float,
    raw_mid: float,
    spot: Optional[float],
) -> Tuple[float, str]:
    """Return a defensible close-price for the leg, plus a source tag.

    Sanity rule: an option premium has a theoretical upper bound (CE ≤ spot,
    PE ≤ strike) but in practice even deep-ITM index options rarely exceed
    ~30% of the underlying value, and an ATM short-dated option is closer to
    5-10%. We flag anything above 50% of ``max(strike, spot)`` as bogus —
    this catches the production bug where ``settle_price`` was being written
    as the spot value — while still permitting any realistic premium
    including deep ITM.

    Parameters
    ----------
    option_type:
        ``"CE"`` or ``"PE"``.
    strike:
        Leg strike (positive number).
    raw_mid:
        The raw close / settle price as read from the EOD row.
    spot:
        Underlying close on the same trade date. When ``None`` we cannot
        compute intrinsic and the raw value is passed through untouched.

    Returns
    -------
    (price, source) where ``source`` is one of ``"mid"`` (raw value used) or
    ``"intrinsic_fallback"`` (raw flagged as bogus → replaced with intrinsic).
    """
    try:
        mid = float(raw_mid or 0.0)
    except (TypeError, ValueError):
        mid = 0.0
    if spot is None:
        return mid, "mid"
    upper_cap = max(float(strike), float(spot)) * 0.5
    if mid > upper_cap or mid < 0.0:
        return intrinsic_value(option_type, float(strike), float(spot)), "intrinsic_fallback"
    return mid, "mid"
